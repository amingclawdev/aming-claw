"""Backlog insert triage gate. Actions: admit, reject_dup, supersede, merge_into."""
from __future__ import annotations
import json, logging, re
log = logging.getLogger(__name__)

_GENERIC_TITLE_TOKENS = {
    "agent",
    "api",
    "audit",
    "backlog",
    "bug",
    "code",
    "codex",
    "file",
    "files",
    "graph",
    "governance",
    "hardening",
    "issue",
    "issues",
    "need",
    "needs",
    "opt",
    "performance",
    "query",
    "queries",
    "row",
    "rows",
    "server",
    "test",
    "tests",
}

_COMMON_CONTEXT_FILES = {
    "agent/governance/server.py",
    "content-system/render-pipeline.md",
}


def _parse_tf(v):
    if isinstance(v, str):
        try: return json.loads(v)
        except Exception: return []
    return v or []

def _title_tokens(title: str) -> set[str]:
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", str(title or "").lower())
        if len(token) >= 3
    }
    return {token for token in tokens if token not in _GENERIC_TITLE_TOKENS}

def _decision(action: str, reason: str, related_bug_ids: list[str], confidence: float, **evidence) -> dict:
    result = {
        "action": action,
        "reason": reason,
        "related_bug_ids": related_bug_ids,
        "confidence": confidence,
    }
    if evidence:
        result["evidence"] = evidence
    return result

def _candidate(row: dict, **evidence) -> dict:
    result = {
        "bug_id": row.get("bug_id", ""),
        "title": row.get("title", ""),
        "target_files": _parse_tf(row.get("target_files", [])),
    }
    if evidence:
        result["evidence"] = evidence
    return result

def _is_common_context_file(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/").lower()
    if not normalized:
        return False
    if normalized in _COMMON_CONTEXT_FILES:
        return True
    basename = normalized.rsplit("/", 1)[-1]
    if basename in {"readme.md", "readme.mdx"}:
        return True
    return bool(re.fullmatch(r"scripts/[^/]+_mcp\.py", normalized))

def _single_common_context_overlap(files: set[str]) -> bool:
    return len(files) == 1 and all(_is_common_context_file(path) for path in files)

def _title_match_strength(left: set[str], right: set[str]) -> tuple[list[str], bool, float]:
    overlap = sorted(left & right)
    if not overlap:
        return [], False, 0.0
    union = left | right
    min_size = min(len(left), len(right))
    min_ratio = (len(overlap) / min_size) if min_size else 0.0
    jaccard = (len(overlap) / len(union)) if union else 0.0
    strong = len(overlap) >= 2 and (min_ratio >= 0.5 or jaccard >= 0.35)
    return overlap, strong, round(max(min_ratio, jaccard), 3)

def _explicit_lineage_match(payload: dict, row: dict) -> bool:
    rid = str(row.get("bug_id") or "").strip()
    if not rid:
        return False
    refs = " ".join(
        str(payload.get(key) or "")
        for key in ("details_md", "provenance_paths", "chain_trigger_json")
    )
    return rid in refs

def triage_backlog_insert(payload: dict, open_rows: list[dict]) -> dict:
    """Classify a new backlog filing against existing OPEN rows.
    Returns dict with keys: action, reason, related_bug_ids, confidence."""
    title = payload.get("title", "")
    tf = _parse_tf(payload.get("target_files", []))
    if not open_rows:
        return {"action": "admit", "reason": "no open rows", "related_bug_ids": [], "confidence": 1.0}
    title_tokens = _title_tokens(title)
    for row in open_rows:
        rid, rt, rtf = row.get("bug_id", ""), row.get("title", ""), _parse_tf(row.get("target_files", []))
        if title and rt and title.strip().lower() == rt.strip().lower():
            return _decision(
                "reject_dup",
                "duplicate of %s" % rid,
                [rid],
                0.9,
                candidates=[_candidate(row, title_exact_match=True)],
            )
        if tf and rtf:
            ov = set(tf) & set(rtf)
            row_title_tokens = _title_tokens(rt)
            title_overlap, strong_title_match, title_similarity = _title_match_strength(title_tokens, row_title_tokens)
            lineage_match = _explicit_lineage_match(payload, row)
            common_context_only = _single_common_context_overlap(ov)
            if len(ov) >= len(tf) and len(ov) >= len(rtf):
                if common_context_only and not (strong_title_match or lineage_match):
                    continue
                evidence = {
                    "overlap_files": sorted(ov),
                    "overlap_count": len(ov),
                    "title_token_overlap": title_overlap,
                    "title_similarity": title_similarity,
                    "lineage_match": lineage_match,
                    "common_context_overlap": common_context_only,
                }
                return _decision(
                    "supersede",
                    "supersedes %s" % rid,
                    [rid],
                    0.8,
                    **evidence,
                    candidates=[_candidate(row, **evidence)],
                )
            if ov and (len(ov) >= 2 or strong_title_match or lineage_match):
                evidence = {
                    "overlap_files": sorted(ov),
                    "overlap_count": len(ov),
                    "title_token_overlap": title_overlap,
                    "title_similarity": title_similarity,
                    "lineage_match": lineage_match,
                    "common_context_overlap": common_context_only,
                }
                return _decision(
                    "merge_into",
                    "merge into %s" % rid,
                    [rid],
                    0.7,
                    **evidence,
                    candidates=[_candidate(row, **evidence)],
                )
    return {"action": "admit", "reason": "no significant overlap", "related_bug_ids": [], "confidence": 0.8}
