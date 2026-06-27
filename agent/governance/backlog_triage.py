"""Backlog insert triage gate. Actions: admit, reject_dup, supersede, merge_into."""
from __future__ import annotations
import json, logging, math, re
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
    "agent/governance/task_timeline.py",
    "content-system/render-pipeline.md",
    "docs/governance/manual-fix-sop.md",
    "Archive/skills/aming-claw/references/mf-sop.md",
}

# --- Hub-file downweighting constants ---
# A file is considered a "hub" when it appears in >= this many open rows.
# Its weight drops to near-zero on its own so that sharing only hub files
# cannot trigger an auto-merge.
_HUB_COUNT_THRESHOLD = 3
# Minimum non-hub weighted overlap required for an auto-merge decision when
# title similarity is below the strong-title threshold.
_MIN_WEIGHTED_OVERLAP_FOR_MERGE = 0.6
# Title similarity threshold required to corroborate a weak file-overlap merge.
_TITLE_SIMILARITY_FOR_WEAK_CORROBORATION = 0.25


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


def _build_file_doc_freq(open_rows: list[dict]) -> dict[str, int]:
    """Count how many open rows reference each file (document frequency)."""
    freq: dict[str, int] = {}
    for row in open_rows:
        for f in _parse_tf(row.get("target_files", [])):
            f = str(f or "").strip()
            if f:
                freq[f] = freq.get(f, 0) + 1
    return freq


def _file_weight(path: str, doc_freq: dict[str, int], n_docs: int) -> float:
    """IDF-style weight: files touched by many rows contribute less.

    Returns a value in (0, 1].  Hub files (appearing in >= _HUB_COUNT_THRESHOLD
    rows) get weight <= 1/threshold, so sharing only hub files contributes a
    very small overlap score.
    """
    if _is_common_context_file(path):
        return 0.05  # hard-coded near-zero for well-known hub files
    df = doc_freq.get(path, 0)
    if n_docs <= 0 or df <= 0:
        return 1.0
    # IDF: log((n+1)/(df+1)) + 1  (smoothed, always positive)
    weight = math.log((n_docs + 1) / (df + 1)) + 1
    # Normalise to (0, 1] relative to a file that appears in only 1 doc
    max_weight = math.log((n_docs + 1) / 2) + 1  # df=1 case
    if max_weight <= 0:
        return 1.0
    return min(1.0, weight / max_weight)


def _weighted_overlap(
    overlap_files: set[str],
    own_files: list[str],
    doc_freq: dict[str, int],
    n_docs: int,
) -> tuple[float, dict[str, float], bool]:
    """Return (weighted_overlap_score, per_file_weights, all_hub_overlap).

    weighted_overlap_score: sum of non-hub per-file weights for overlapping
    files, normalised by the sum of non-hub weights of the *new row's* own
    files.  Hub/common-context files are excluded from both numerator and
    denominator so their contribution cannot drive the score to 1.0.

    all_hub_overlap: True when every overlapping file is a hub/common-context
    file and there are no non-hub overlapping files.  Used to gate auto-merge.
    """
    per_file: dict[str, float] = {}
    non_hub_overlap_sum = 0.0
    all_hub = True
    for f in overlap_files:
        w = _file_weight(f, doc_freq, n_docs)
        per_file[f] = round(w, 4)
        is_hub = _is_common_context_file(f) or w < 0.2
        if not is_hub:
            non_hub_overlap_sum += w
            all_hub = False

    # Non-hub weight of own files (denominator)
    non_hub_own_sum = 0.0
    for f in (own_files or []):
        w = _file_weight(f, doc_freq, n_docs)
        is_hub = _is_common_context_file(f) or w < 0.2
        if not is_hub:
            non_hub_own_sum += w

    score = (non_hub_overlap_sum / non_hub_own_sum) if non_hub_own_sum > 0 else 0.0
    return round(score, 4), per_file, all_hub


def triage_backlog_insert(payload: dict, open_rows: list[dict]) -> dict:
    """Classify a new backlog filing against existing OPEN rows.

    Returns dict with keys: action, reason, related_bug_ids, confidence.

    Hub-file downweighting (AC-BACKLOG-TRIAGE-FALSE-MERGE-UNRELATED):
    - Files referenced by many open rows (hub files like server.py) receive
      near-zero weight so sharing only hub files cannot auto-merge.
    - An auto-merge/dup-block requires either: strong title similarity AND any
      overlap, OR weighted overlap >= _MIN_WEIGHTED_OVERLAP_FOR_MERGE.
    - Low-confidence candidates (only hub overlap + weak title) are reported as
      advisory 'admit' with score breakdown, not as merge_into.
    """
    title = payload.get("title", "")
    tf = _parse_tf(payload.get("target_files", []))
    if not open_rows:
        return {"action": "admit", "reason": "no open rows", "related_bug_ids": [], "confidence": 1.0}

    title_tokens = _title_tokens(title)
    n_docs = len(open_rows)
    doc_freq = _build_file_doc_freq(open_rows)

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

            # Compute hub-weighted overlap score (excludes hub files from numerator/denominator)
            w_score, per_file_weights, all_hub_overlap = _weighted_overlap(ov, tf, doc_freq, n_docs)
            # hub_only_overlap: all overlapping files are hubs AND no strong corroborating signal
            hub_only_overlap = all_hub_overlap and not strong_title_match and not lineage_match

            # --- supersede: new row fully covers old row's files ---
            if len(ov) >= len(tf) and len(ov) >= len(rtf):
                if (common_context_only or hub_only_overlap) and not (strong_title_match or lineage_match):
                    continue
                evidence = {
                    "overlap_files": sorted(ov),
                    "overlap_count": len(ov),
                    "weighted_overlap_score": w_score,
                    "per_file_weights": per_file_weights,
                    "title_token_overlap": title_overlap,
                    "title_similarity": title_similarity,
                    "lineage_match": lineage_match,
                    "common_context_overlap": common_context_only,
                    "hub_only_overlap": hub_only_overlap,
                    "threshold_verdict": "passed",
                }
                return _decision(
                    "supersede",
                    "supersedes %s" % rid,
                    [rid],
                    0.8,
                    **evidence,
                    candidates=[_candidate(row, **evidence)],
                )

            if ov:
                # Calibrated confidence: require strong corroboration beyond hub files.
                # hub_only_overlap = all shared files are hub/common-context.
                # w_score = weighted overlap excluding hub files — 0.0 when only hubs overlap.
                has_corroboration = (
                    strong_title_match
                    or lineage_match
                    or (not all_hub_overlap and w_score >= _MIN_WEIGHTED_OVERLAP_FOR_MERGE)
                )
                evidence = {
                    "overlap_files": sorted(ov),
                    "overlap_count": len(ov),
                    "weighted_overlap_score": w_score,
                    "per_file_weights": per_file_weights,
                    "title_token_overlap": title_overlap,
                    "title_similarity": title_similarity,
                    "title_similarity_threshold": _TITLE_SIMILARITY_FOR_WEAK_CORROBORATION,
                    "lineage_match": lineage_match,
                    "common_context_overlap": common_context_only,
                    "hub_only_overlap": not has_corroboration,
                    "threshold_verdict": "passed" if has_corroboration else "below_threshold",
                }
                if has_corroboration:
                    return _decision(
                        "merge_into",
                        "merge into %s" % rid,
                        [rid],
                        0.7,
                        **evidence,
                        candidates=[_candidate(row, **evidence)],
                    )
                # Low-confidence: shared only hub/common files with weak title — admit with advisory
                evidence["advisory"] = (
                    "shared files are hub/high-frequency; title similarity %.3f below threshold %.3f"
                    % (title_similarity, _TITLE_SIMILARITY_FOR_WEAK_CORROBORATION)
                )
                log.debug(
                    "triage: hub-only overlap with %s (w_score=%.3f title_sim=%.3f) — admit with advisory",
                    rid, w_score, title_similarity,
                )
    return {"action": "admit", "reason": "no significant overlap", "related_bug_ids": [], "confidence": 0.8}
