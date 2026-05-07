"""State-only semantic enrichment for graph reconcile snapshots.

The structural graph remains the source of truth.  Semantic enrichment is a
retryable companion artifact attached to a snapshot, so full and scope
reconcile can reuse the same review/feedback loop without mutating project
source files or graph topology.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Callable

from .graph_snapshot_store import (
    ensure_schema,
    get_graph_snapshot,
    snapshot_companion_dir,
    snapshot_graph_path,
    utc_now,
)
from .reconcile_semantic_config import load_semantic_enrichment_config


SEMANTIC_ENRICHMENT_SCHEMA_VERSION = 1
SEMANTIC_ARTIFACT_DIR = "semantic-enrichment"
SEMANTIC_INDEX_NAME = "semantic-index.json"
SEMANTIC_REVIEW_REPORT_NAME = "semantic-review-report.json"
REVIEW_FEEDBACK_NAME = "review-feedback.jsonl"

FeedbackAiCall = Callable[[str, dict[str, Any]], Any]


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _read_json(path: Path, default: Any) -> Any:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return payload if payload is not None else default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json(payload), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(_json(row) + "\n")


def _decode_notes(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}


def _update_snapshot_notes(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    patch: dict[str, Any],
) -> dict[str, Any]:
    snapshot = get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    notes = _decode_notes(snapshot.get("notes"))
    notes.update(patch)
    conn.execute(
        """
        UPDATE graph_snapshots
        SET notes = ?
        WHERE project_id = ? AND snapshot_id = ?
        """,
        (_json(notes), project_id, snapshot_id),
    )
    return notes


def _semantic_base_dir(project_id: str, snapshot_id: str) -> Path:
    return snapshot_companion_dir(project_id, snapshot_id) / SEMANTIC_ARTIFACT_DIR


def _feedback_path(project_id: str, snapshot_id: str) -> Path:
    return _semantic_base_dir(project_id, snapshot_id) / REVIEW_FEEDBACK_NAME


def _round_dir(project_id: str, snapshot_id: str, feedback_round: int) -> Path:
    return _semantic_base_dir(project_id, snapshot_id) / "rounds" / f"round-{feedback_round:03d}"


def _path_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = [raw]
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        path = str(item or "").replace("\\", "/").strip("/")
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _graph_nodes(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    deps = graph_json.get("deps_graph") if isinstance(graph_json, dict) else {}
    nodes = deps.get("nodes") if isinstance(deps, dict) else None
    return [dict(node) for node in nodes or [] if isinstance(node, dict)]


def _load_feature_index(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    notes = _decode_notes(snapshot.get("notes"))
    path = (
        notes.get("governance_index", {})
        .get("artifacts", {})
        .get("feature_index_path", "")
    )
    if not path:
        return {}
    payload = _read_json(Path(path), {})
    features = payload.get("features") if isinstance(payload, dict) else None
    out: dict[str, dict[str, Any]] = {}
    for feature in features or []:
        if not isinstance(feature, dict):
            continue
        node_id = str(feature.get("node_id") or "")
        if node_id:
            out[node_id] = dict(feature)
    return out


def _hash_payload(payload: Any) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _node_id(node: dict[str, Any]) -> str:
    return str(node.get("id") or node.get("node_id") or "")


def _feature_context_from_node(
    node: dict[str, Any],
    *,
    feature_index: dict[str, dict[str, Any]],
    project_root: Path | None,
    max_excerpt_chars: int,
) -> dict[str, Any]:
    node_id = _node_id(node)
    metadata = dict(node.get("metadata") or {})
    primary = _path_list(node.get("primary") or node.get("primary_files"))
    secondary = _path_list(node.get("secondary") or node.get("secondary_files"))
    tests = _path_list(node.get("test") or node.get("test_files"))
    indexed = feature_index.get(node_id, {})
    source_excerpt: dict[str, str] = {}
    if project_root is not None and max_excerpt_chars > 0:
        budget = max_excerpt_chars
        for rel in primary + tests + secondary:
            if budget <= 0:
                break
            path = project_root / rel
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            excerpt = text[: min(len(text), budget)]
            source_excerpt[rel] = excerpt
            budget -= len(excerpt)
    fallback_hash_payload = {
        "node_id": node_id,
        "title": node.get("title") or "",
        "primary": primary,
        "secondary": secondary,
        "test": tests,
        "functions": metadata.get("functions") or [],
    }
    return {
        "node_id": node_id,
        "title": str(node.get("title") or node_id),
        "layer": str(node.get("layer") or ""),
        "kind": str(node.get("kind") or metadata.get("kind") or ""),
        "primary": primary,
        "secondary": secondary,
        "test": tests,
        "metadata": metadata,
        "file_hashes": indexed.get("file_hashes") or {},
        "feature_hash": indexed.get("feature_hash") or _hash_payload(fallback_hash_payload),
        "symbol_refs": indexed.get("symbol_refs") or [],
        "doc_refs": indexed.get("doc_refs") or [],
        "source_excerpt": source_excerpt,
    }


def normalize_feedback_item(
    item: dict[str, Any],
    *,
    created_by: str = "observer",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Normalize one append-only review feedback item."""
    if not isinstance(item, dict):
        raise ValueError("feedback item must be an object")
    target_type = str(item.get("target_type") or "snapshot").strip() or "snapshot"
    if target_type not in {"snapshot", "node", "path", "edge"}:
        raise ValueError(f"invalid feedback target_type: {target_type}")
    priority = str(item.get("priority") or "P2").upper()
    if priority not in {"P0", "P1", "P2", "P3"}:
        priority = "P2"
    issue = str(item.get("issue") or item.get("comment") or "").strip()
    expected_change = str(item.get("expected_change") or item.get("suggestion") or "").strip()
    if not issue and not expected_change:
        raise ValueError("feedback item requires issue or expected_change")
    now = created_at or utc_now()
    raw_identity = {
        "target_type": target_type,
        "target_id": item.get("target_id") or item.get("node_id") or item.get("path") or "",
        "issue": issue,
        "expected_change": expected_change,
        "created_at": now,
    }
    feedback_id = str(item.get("feedback_id") or item.get("id") or "")
    if not feedback_id:
        feedback_id = f"fb-{uuid.uuid4().hex[:8]}"
    return {
        "feedback_id": feedback_id,
        "target_type": target_type,
        "target_id": str(
            item.get("target_id")
            or item.get("node_id")
            or item.get("path")
            or item.get("edge_id")
            or ""
        ),
        "node_id": str(item.get("node_id") or ""),
        "path": str(item.get("path") or ""),
        "edge": item.get("edge") if isinstance(item.get("edge"), dict) else {},
        "priority": priority,
        "issue": issue,
        "expected_change": expected_change,
        "status": str(item.get("status") or "open"),
        "created_by": str(item.get("created_by") or created_by or "observer"),
        "created_at": now,
        "evidence": item.get("evidence") if isinstance(item.get("evidence"), dict) else {},
        "fingerprint": _hash_payload(raw_identity)[:16],
    }


def append_review_feedback(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    feedback_items: list[dict[str, Any]] | dict[str, Any],
    *,
    created_by: str = "observer",
) -> dict[str, Any]:
    """Append review feedback to a snapshot companion JSONL artifact."""
    ensure_schema(conn)
    snapshot = get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    raw_items = feedback_items if isinstance(feedback_items, list) else [feedback_items]
    normalized = [
        normalize_feedback_item(item, created_by=created_by)
        for item in raw_items
        if isinstance(item, dict)
    ]
    path = _feedback_path(project_id, snapshot_id)
    _append_jsonl(path, normalized)
    all_feedback = _read_jsonl(path)
    _update_snapshot_notes(
        conn,
        project_id,
        snapshot_id,
        {
            "semantic_feedback": {
                "schema_version": SEMANTIC_ENRICHMENT_SCHEMA_VERSION,
                "feedback_path": str(path),
                "feedback_count": len(all_feedback),
                "latest_feedback_at": normalized[-1]["created_at"] if normalized else "",
            }
        },
    )
    return {
        "ok": True,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "feedback_path": str(path),
        "added_count": len(normalized),
        "feedback_count": len(all_feedback),
        "feedback": normalized,
    }


def load_review_feedback(project_id: str, snapshot_id: str) -> list[dict[str, Any]]:
    return _read_jsonl(_feedback_path(project_id, snapshot_id))


def _feedback_matches_feature(feedback: dict[str, Any], feature: dict[str, Any]) -> bool:
    target_type = str(feedback.get("target_type") or "")
    if target_type == "snapshot":
        return True
    node_id = str(feature.get("node_id") or "")
    if target_type == "node":
        return str(feedback.get("target_id") or feedback.get("node_id") or "") == node_id
    paths = set(feature.get("primary") or [])
    paths.update(feature.get("secondary") or [])
    paths.update(feature.get("test") or [])
    if target_type == "path":
        target = str(feedback.get("target_id") or feedback.get("path") or "").replace("\\", "/").strip("/")
        return target in paths
    if target_type == "edge":
        edge = feedback.get("edge") if isinstance(feedback.get("edge"), dict) else {}
        return str(edge.get("src") or edge.get("source") or "") == node_id or str(
            edge.get("dst") or edge.get("target") or ""
        ) == node_id
    return False


def _quality_flags(feature: dict[str, Any], feedback: list[dict[str, Any]]) -> list[str]:
    flags: list[str] = []
    if not feature.get("secondary"):
        flags.append("missing_doc_binding")
    if not feature.get("test"):
        flags.append("missing_test_binding")
    if feedback:
        flags.append("has_review_feedback")
    if not feature.get("symbol_refs"):
        flags.append("missing_symbol_refs")
    return flags


def _heuristic_semantic_entry(
    feature: dict[str, Any],
    feedback: list[dict[str, Any]],
    *,
    enrichment_status: str,
    ai_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ai_response = ai_response or {}
    feedback_ids = [str(item.get("feedback_id") or "") for item in feedback if item.get("feedback_id")]
    applied = _path_list(ai_response.get("applied_feedback_ids"))
    rejected = _path_list(ai_response.get("rejected_feedback_ids"))
    unresolved = [
        feedback_id
        for feedback_id in feedback_ids
        if feedback_id and feedback_id not in set(applied) and feedback_id not in set(rejected)
    ]
    summary = str(ai_response.get("semantic_summary") or ai_response.get("purpose") or "")
    if not summary:
        primary = ", ".join(feature.get("primary") or []) or "no primary files"
        summary = f"{feature.get('title') or feature.get('node_id')} covers {primary}."
    return {
        "node_id": feature.get("node_id") or "",
        "source_title": feature.get("title") or "",
        "feature_name": str(ai_response.get("feature_name") or feature.get("title") or feature.get("node_id") or ""),
        "semantic_summary": summary,
        "intent": str(ai_response.get("intent") or ai_response.get("purpose") or ""),
        "domain_label": str(ai_response.get("domain_label") or ""),
        "purpose": str(ai_response.get("purpose") or summary),
        "merge_suggestions": ai_response.get("merge_suggestions") or [],
        "split_suggestions": ai_response.get("split_suggestions") or [],
        "dependency_patch_suggestions": ai_response.get("dependency_patch_suggestions") or [],
        "doc_coverage_review": ai_response.get("doc_coverage_review") or {
            "bound": bool(feature.get("secondary")),
            "files": feature.get("secondary") or [],
        },
        "test_coverage_review": ai_response.get("test_coverage_review") or {
            "bound": bool(feature.get("test")),
            "files": feature.get("test") or [],
        },
        "dead_code_candidates": ai_response.get("dead_code_candidates") or [],
        "quality_flags": _quality_flags(feature, feedback),
        "applied_feedback_ids": applied,
        "rejected_feedback_ids": rejected,
        "unresolved_feedback_ids": unresolved,
        "feedback_count": len(feedback),
        "feature_hash": feature.get("feature_hash") or "",
        "file_hashes": feature.get("file_hashes") or {},
        "primary": feature.get("primary") or [],
        "secondary": feature.get("secondary") or [],
        "test": feature.get("test") or [],
        "symbol_refs": feature.get("symbol_refs") or [],
        "doc_refs": feature.get("doc_refs") or [],
        "enrichment_status": enrichment_status,
    }


def _call_ai(
    ai_call: FeedbackAiCall | None,
    *,
    stage: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if ai_call is None:
        return None
    response = ai_call(stage, payload)
    return response if isinstance(response, dict) else None


def run_semantic_enrichment(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    project_root: str | Path | None = None,
    *,
    feedback_items: list[dict[str, Any]] | dict[str, Any] | None = None,
    feedback_round: int | None = None,
    use_ai: bool | None = None,
    ai_call: FeedbackAiCall | None = None,
    created_by: str = "observer",
    max_excerpt_chars: int | None = None,
    semantic_config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Create semantic companion artifacts for a graph snapshot.

    The same function is intentionally snapshot-kind agnostic; callers can use
    it for full, scope, imported, or future graph snapshot kinds.
    """
    ensure_schema(conn)
    snapshot = get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    root = Path(project_root).resolve() if project_root else None
    semantic_config = load_semantic_enrichment_config(
        project_root=root,
        config_path=semantic_config_path,
    )
    effective_use_ai = semantic_config.use_ai_default if use_ai is None else bool(use_ai)
    effective_excerpt_chars = (
        semantic_config.input_policy.max_excerpt_chars
        if max_excerpt_chars is None
        else int(max_excerpt_chars)
    )
    if not semantic_config.input_policy.include_source_excerpt:
        effective_excerpt_chars = 0
    if feedback_items:
        append_review_feedback(
            conn,
            project_id,
            snapshot_id,
            feedback_items,
            created_by=created_by,
        )
    feedback = load_review_feedback(project_id, snapshot_id)
    graph_path = snapshot_graph_path(project_id, snapshot_id)
    graph_json = _read_json(graph_path, {})
    nodes = [
        node
        for node in _graph_nodes(graph_json)
        if _node_id(node) and (_path_list(node.get("primary") or node.get("primary_files")))
    ]
    feature_index = _load_feature_index(snapshot)
    existing_rounds = sorted((_semantic_base_dir(project_id, snapshot_id) / "rounds").glob("round-*"))
    round_number = int(feedback_round) if feedback_round is not None else len(existing_rounds)
    generated_at = utc_now()
    semantic_features: list[dict[str, Any]] = []
    ai_complete_count = 0
    ai_unavailable_count = 0
    for node in nodes:
        feature = _feature_context_from_node(
            node,
            feature_index=feature_index,
            project_root=root,
            max_excerpt_chars=effective_excerpt_chars,
        )
        relevant_feedback = [
            item for item in feedback if _feedback_matches_feature(item, feature)
        ]
        payload_feature = dict(feature)
        if not semantic_config.input_policy.include_symbol_refs:
            payload_feature["symbol_refs"] = []
        if not semantic_config.input_policy.include_doc_refs:
            payload_feature["doc_refs"] = []
        if not semantic_config.input_policy.include_file_hashes:
            payload_feature["file_hashes"] = {}
        payload_feedback = relevant_feedback if semantic_config.input_policy.include_review_feedback else []
        payload = {
            "schema_version": SEMANTIC_ENRICHMENT_SCHEMA_VERSION,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "snapshot_kind": snapshot.get("snapshot_kind") or "",
            "commit_sha": snapshot.get("commit_sha") or "",
            "feedback_round": round_number,
            "feature": payload_feature,
            "review_feedback": payload_feedback,
            "instructions": semantic_config.to_instruction_payload(),
        }
        ai_response = _call_ai(ai_call, stage="reconcile_semantic_feature", payload=payload) if effective_use_ai else None
        if ai_response is not None:
            status = "ai_complete"
            ai_complete_count += 1
        else:
            status = "ai_unavailable" if effective_use_ai else "heuristic"
            if effective_use_ai:
                ai_unavailable_count += 1
        semantic_features.append(
            _heuristic_semantic_entry(
                feature,
                relevant_feedback,
                enrichment_status=status,
                ai_response=ai_response,
            )
        )

    semantic_index = {
        "schema_version": SEMANTIC_ENRICHMENT_SCHEMA_VERSION,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "snapshot_kind": snapshot.get("snapshot_kind") or "",
        "commit_sha": snapshot.get("commit_sha") or "",
        "feedback_round": round_number,
        "generated_at": generated_at,
        "created_by": created_by,
        "ai_requested": bool(effective_use_ai),
        "semantic_config": semantic_config.summary(),
        "feature_count": len(semantic_features),
        "features": sorted(semantic_features, key=lambda item: str(item.get("node_id") or "")),
    }
    unresolved_feedback = sorted({
        feedback_id
        for item in semantic_features
        for feedback_id in (item.get("unresolved_feedback_ids") or [])
    })
    report = {
        "schema_version": SEMANTIC_ENRICHMENT_SCHEMA_VERSION,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "feedback_round": round_number,
        "generated_at": generated_at,
        "feature_count": len(semantic_features),
        "ai_complete_count": ai_complete_count,
        "ai_unavailable_count": ai_unavailable_count,
        "feedback_count": len(feedback),
        "semantic_config": semantic_config.summary(),
        "unresolved_feedback_count": len(unresolved_feedback),
        "unresolved_feedback_ids": unresolved_feedback,
        "quality_flag_counts": _count_quality_flags(semantic_features),
    }

    base = _semantic_base_dir(project_id, snapshot_id)
    rdir = _round_dir(project_id, snapshot_id, round_number)
    semantic_index_path = rdir / SEMANTIC_INDEX_NAME
    review_report_path = rdir / SEMANTIC_REVIEW_REPORT_NAME
    latest_semantic_path = base / SEMANTIC_INDEX_NAME
    latest_report_path = base / SEMANTIC_REVIEW_REPORT_NAME
    _write_json(semantic_index_path, semantic_index)
    _write_json(review_report_path, report)
    _write_json(latest_semantic_path, semantic_index)
    _write_json(latest_report_path, report)
    notes_patch = {
        "semantic_enrichment": {
            "schema_version": SEMANTIC_ENRICHMENT_SCHEMA_VERSION,
            "latest_round": round_number,
            "semantic_index_path": str(latest_semantic_path),
            "review_report_path": str(latest_report_path),
            "latest_round_semantic_index_path": str(semantic_index_path),
            "latest_round_review_report_path": str(review_report_path),
            "feature_count": len(semantic_features),
            "ai_complete_count": ai_complete_count,
            "feedback_count": len(feedback),
            "unresolved_feedback_count": len(unresolved_feedback),
            "updated_at": generated_at,
            "created_by": created_by,
        }
    }
    _update_snapshot_notes(conn, project_id, snapshot_id, notes_patch)
    return {
        "ok": True,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "feedback_round": round_number,
        "semantic_index_path": str(latest_semantic_path),
        "review_report_path": str(latest_report_path),
        "round_semantic_index_path": str(semantic_index_path),
        "round_review_report_path": str(review_report_path),
        "summary": report,
        "semantic_index": semantic_index,
    }


def _count_quality_flags(features: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for feature in features:
        for flag in feature.get("quality_flags") or []:
            key = str(flag)
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


__all__ = [
    "SEMANTIC_ENRICHMENT_SCHEMA_VERSION",
    "append_review_feedback",
    "load_review_feedback",
    "normalize_feedback_item",
    "run_semantic_enrichment",
]
