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

from .reconcile_trace import write_json
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


def _string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        values = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = [raw]
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        value = str(item or "").strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


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
    out: list[str] = []
    seen: set[str] = set()
    for item in _string_list(raw):
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


def _semantic_selector(raw: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(raw or {})
    scope = str(payload.get("scope") or payload.get("semantic_ai_scope") or "all").strip().lower() or "all"
    if scope in {"full", "*"}:
        scope = "all"
    if scope in {"off", "disabled"}:
        scope = "none"
    node_ids = _string_list(payload.get("node_ids") or payload.get("semantic_node_ids"))
    layers = [item.upper() for item in _string_list(payload.get("layers") or payload.get("semantic_layers"))]
    quality_flags = _string_list(payload.get("quality_flags") or payload.get("semantic_quality_flags"))
    missing = [
        item.lower().replace("-", "_")
        for item in _string_list(payload.get("missing") or payload.get("semantic_missing"))
    ]
    changed_paths = _path_list(payload.get("changed_paths") or payload.get("semantic_changed_paths"))
    path_prefixes = _path_list(payload.get("path_prefixes") or payload.get("semantic_path_prefixes"))
    match_mode = str(payload.get("match_mode") or payload.get("semantic_selector_match") or "all").strip().lower()
    if match_mode not in {"all", "any"}:
        match_mode = "all"
    include_structural = bool(payload.get("include_structural") or payload.get("semantic_include_structural"))
    if layers and any(layer in {"L1", "L2", "L3", "L4", "L5", "L6"} for layer in layers):
        include_structural = True
    if node_ids and any(str(item).upper().startswith(("L1.", "L2.", "L3.", "L4.", "L5.", "L6.")) for item in node_ids):
        include_structural = True
    return {
        "scope": scope,
        "node_ids": node_ids,
        "layers": layers,
        "quality_flags": quality_flags,
        "missing": missing,
        "changed_paths": changed_paths,
        "path_prefixes": path_prefixes,
        "match_mode": match_mode,
        "include_structural": include_structural,
    }


def _selector_from_kwargs(
    *,
    semantic_ai_scope: str | None = None,
    semantic_node_ids: Any = None,
    semantic_layers: Any = None,
    semantic_quality_flags: Any = None,
    semantic_missing: Any = None,
    semantic_changed_paths: Any = None,
    semantic_path_prefixes: Any = None,
    semantic_selector_match: str | None = None,
    semantic_include_structural: bool = False,
) -> dict[str, Any]:
    return _semantic_selector({
        "semantic_ai_scope": semantic_ai_scope,
        "semantic_node_ids": semantic_node_ids,
        "semantic_layers": semantic_layers,
        "semantic_quality_flags": semantic_quality_flags,
        "semantic_missing": semantic_missing,
        "semantic_changed_paths": semantic_changed_paths,
        "semantic_path_prefixes": semantic_path_prefixes,
        "semantic_selector_match": semantic_selector_match,
        "semantic_include_structural": semantic_include_structural,
    })


def _node_has_primary(node: dict[str, Any]) -> bool:
    return bool(_path_list(node.get("primary") or node.get("primary_files")))


def _semantic_candidate_nodes(graph_json: dict[str, Any], selector: dict[str, Any]) -> list[dict[str, Any]]:
    include_structural = bool(selector.get("include_structural"))
    out: list[dict[str, Any]] = []
    for node in _graph_nodes(graph_json):
        if not _node_id(node):
            continue
        if _node_has_primary(node) or include_structural:
            out.append(node)
    return out


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
    config = _path_list(node.get("config") or node.get("config_files") or metadata.get("config_files"))
    indexed = feature_index.get(node_id, {})
    source_excerpt: dict[str, str] = {}
    if project_root is not None and max_excerpt_chars > 0:
        budget = max_excerpt_chars
        for rel in primary + tests + secondary + config:
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
        "config": config,
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
        "config": config,
        "metadata": metadata,
        "file_hashes": indexed.get("file_hashes") or {},
        "feature_hash": indexed.get("feature_hash") or _hash_payload(fallback_hash_payload),
        "symbol_refs": indexed.get("symbol_refs") or [],
        "doc_refs": indexed.get("doc_refs") or [],
        "config_refs": indexed.get("config_refs") or [
            {"path": path, "kind": "config"} for path in config
        ],
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
    paths.update(feature.get("config") or [])
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
    layer = str(feature.get("layer") or "")
    if not feature.get("secondary"):
        flags.append("missing_doc_binding")
    if layer == "L7" and not feature.get("test"):
        flags.append("missing_test_binding")
    if feedback:
        flags.append("has_review_feedback")
    if layer == "L7" and not feature.get("symbol_refs"):
        flags.append("missing_symbol_refs")
    return flags


def _feature_paths(feature: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("primary", "secondary", "test", "config"):
        paths.extend(_path_list(feature.get(key)))
    return paths


def _missing_matches(feature: dict[str, Any], missing: list[str]) -> bool:
    if not missing:
        return True
    checks = {
        "doc": not feature.get("secondary"),
        "docs": not feature.get("secondary"),
        "document": not feature.get("secondary"),
        "test": not feature.get("test"),
        "tests": not feature.get("test"),
        "config": not feature.get("config"),
        "symbol": not feature.get("symbol_refs"),
        "symbols": not feature.get("symbol_refs"),
    }
    return any(checks.get(item, False) for item in missing)


def _path_matches(feature: dict[str, Any], paths: list[str], prefixes: list[str]) -> bool:
    feature_paths = _feature_paths(feature)
    if paths:
        requested = {path.replace("\\", "/").strip("/") for path in paths}
        if not requested.intersection(feature_paths):
            return False
    if prefixes:
        if not any(
            path == prefix or path.startswith(prefix.rstrip("/") + "/")
            for path in feature_paths
            for prefix in prefixes
        ):
            return False
    return True


def _selector_decision(
    feature: dict[str, Any],
    flags: list[str],
    selector: dict[str, Any],
) -> tuple[bool, list[str]]:
    scope = str(selector.get("scope") or "all").lower()
    if scope == "none":
        return False, ["scope_none"]
    node_ids = set(selector.get("node_ids") or [])
    layers = set(selector.get("layers") or [])
    quality_flags = set(selector.get("quality_flags") or [])
    missing = list(selector.get("missing") or [])
    changed_paths = list(selector.get("changed_paths") or [])
    path_prefixes = list(selector.get("path_prefixes") or [])
    has_filters = bool(node_ids or layers or quality_flags or missing or changed_paths or path_prefixes)
    if scope == "all" and not has_filters:
        return True, ["scope_all"]
    if scope in {"selected", "partial", "issues", "changed"} and not has_filters:
        return False, [f"scope_{scope}_requires_filter"]

    checks: list[tuple[str, bool]] = []
    if node_ids:
        checks.append(("node_id", str(feature.get("node_id") or "") in node_ids))
    if layers:
        checks.append(("layer", str(feature.get("layer") or "").upper() in layers))
    if quality_flags:
        checks.append(("quality_flags", bool(set(flags).intersection(quality_flags))))
    if missing:
        checks.append(("missing", _missing_matches(feature, missing)))
    if changed_paths or path_prefixes:
        checks.append(("paths", _path_matches(feature, changed_paths, path_prefixes)))
    if not checks:
        return scope == "all", ["scope_all"] if scope == "all" else ["not_selected"]

    match_mode = str(selector.get("match_mode") or "all")
    matched = all(ok for _, ok in checks) if match_mode == "all" else any(ok for _, ok in checks)
    reasons = [name for name, ok in checks if ok] or ["selector_no_match"]
    return matched, reasons


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
        if feature.get("primary"):
            summary = f"{feature.get('title') or feature.get('node_id')} covers {primary}."
        else:
            summary = (
                f"{feature.get('title') or feature.get('node_id')} is a "
                f"{feature.get('layer') or 'graph'} structural governance node."
            )
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
        "config_coverage_review": ai_response.get("config_coverage_review") or {
            "bound": bool(feature.get("config")),
            "files": feature.get("config") or [],
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
        "config": feature.get("config") or [],
        "symbol_refs": feature.get("symbol_refs") or [],
        "doc_refs": feature.get("doc_refs") or [],
        "config_refs": feature.get("config_refs") or [],
        "enrichment_status": enrichment_status,
        "layer": feature.get("layer") or "",
        "kind": feature.get("kind") or "",
    }


def _call_ai(
    ai_call: FeedbackAiCall | None,
    *,
    stage: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if ai_call is None:
        return None
    try:
        response = ai_call(stage, payload)
    except Exception as exc:  # noqa: BLE001 - caller records unavailable AI evidence
        return {"_ai_error": str(exc)}
    return response if isinstance(response, dict) else None


def _normal_batch_size(raw: int | None) -> int:
    if raw is None:
        return 1
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, value)


def _batch_key(feature: dict[str, Any], batch_by: str) -> str:
    mode = (batch_by or "subsystem").strip().lower()
    metadata = feature.get("metadata") if isinstance(feature.get("metadata"), dict) else {}
    if mode in {"none", "order", "flat"}:
        return "all"
    if mode in {"layer", "layers"}:
        return str(feature.get("layer") or "unknown")
    if mode in {"kind", "role"}:
        return str(feature.get("kind") or metadata.get("kind") or "unknown")
    if mode in {"subsystem", "feature", "feature_group", "group"}:
        return str(
            metadata.get("subsystem")
            or metadata.get("hierarchy_parent")
            or metadata.get("parent")
            or metadata.get("cluster_parent")
            or metadata.get("feature_cluster")
            or metadata.get("cluster")
            or feature.get("kind")
            or feature.get("layer")
            or "unknown"
        )
    return str(metadata.get(mode) or "unknown")


def _batch_records(
    records: list[dict[str, Any]],
    *,
    batch_size: int,
    batch_by: str,
) -> list[list[dict[str, Any]]]:
    if batch_size <= 1:
        return [[record] for record in records]
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for record in records:
        key = _batch_key(record["feature"], batch_by)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(record)
    batches: list[list[dict[str, Any]]] = []
    for key in order:
        group = groups[key]
        for idx in range(0, len(group), batch_size):
            batches.append(group[idx: idx + batch_size])
    return batches


def _extract_batch_ai_responses(
    response: dict[str, Any] | None,
    records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    node_ids = [str(record["feature"].get("node_id") or "") for record in records]
    if not response:
        return {}
    if response.get("_ai_error"):
        return {node_id: {"_ai_error": response.get("_ai_error")} for node_id in node_ids}

    raw_items: Any = (
        response.get("features")
        or response.get("semantic_features")
        or response.get("nodes")
        or response.get("results")
    )
    if isinstance(raw_items, dict):
        items = []
        for node_id, payload in raw_items.items():
            if isinstance(payload, dict):
                item = dict(payload)
                item.setdefault("node_id", str(node_id))
                items.append(item)
        raw_items = items
    if not isinstance(raw_items, list):
        if len(node_ids) == 1:
            return {node_ids[0]: dict(response)}
        return {
            node_id: {"_ai_error": "semantic AI batch returned no features array"}
            for node_id in node_ids
        }

    route = response.get("_ai_route")
    elapsed = response.get("_ai_elapsed_ms")
    out: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("node_id") or item.get("id") or "")
        if not node_id:
            continue
        payload = dict(item)
        if route and not payload.get("_ai_route"):
            payload["_ai_route"] = route
        if elapsed is not None and payload.get("_ai_elapsed_ms") is None:
            payload["_ai_elapsed_ms"] = elapsed
        out[node_id] = payload
    for node_id in node_ids:
        out.setdefault(node_id, {"_ai_error": "semantic AI batch omitted node_id"})
    return out


def _safe_node_filename(node_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in node_id)
    return safe or "feature"


def _semantic_batch_memory_id(snapshot_id: str, round_number: int, explicit: str | None = None) -> str:
    if explicit:
        return _safe_node_filename(str(explicit))
    return f"semantic-{_safe_node_filename(snapshot_id)}-round-{int(round_number):03d}"


def _create_semantic_batch_memory(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    round_number: int,
    *,
    created_by: str,
    batch_id: str | None = None,
) -> tuple[dict[str, Any], str]:
    try:
        from . import reconcile_batch_memory as bm

        bid = _semantic_batch_memory_id(snapshot_id, round_number, batch_id)
        batch = bm.create_or_get_batch(
            conn,
            project_id,
            session_id=snapshot_id,
            batch_id=bid,
            created_by=created_by,
            initial_memory={
                "semantic_enrichment": {
                    "snapshot_id": snapshot_id,
                    "round": round_number,
                    "created_by": created_by,
                }
            },
        )
        return batch, ""
    except Exception as exc:  # noqa: BLE001 - semantic memory is advisory
        return {}, str(exc)


def _refresh_semantic_batch_memory(
    conn: sqlite3.Connection,
    project_id: str,
    batch_id: str,
) -> dict[str, Any]:
    if not batch_id:
        return {}
    try:
        from . import reconcile_batch_memory as bm

        return bm.get_batch(conn, project_id, batch_id)
    except Exception:  # noqa: BLE001 - keep semantic enrichment retryable
        return {}


def _semantic_batch_memory_summary(batch: dict[str, Any]) -> dict[str, Any]:
    memory = batch.get("memory") if isinstance(batch, dict) else {}
    memory = memory if isinstance(memory, dict) else {}
    accepted = memory.get("accepted_features") if isinstance(memory.get("accepted_features"), dict) else {}
    features: list[dict[str, Any]] = []
    for name in sorted(accepted):
        item = accepted.get(name) if isinstance(accepted.get(name), dict) else {}
        features.append({
            "feature_name": name,
            "purpose": str(item.get("purpose") or "")[:600],
            "clusters": _path_list(item.get("clusters")),
            "owned_files": _path_list(item.get("owned_files"))[:20],
            "shared_files": _path_list(item.get("shared_files"))[:20],
            "candidate_tests": _path_list(item.get("candidate_tests"))[:20],
            "candidate_docs": _path_list(item.get("candidate_docs"))[:20],
        })
    conflicts = memory.get("open_conflicts") if isinstance(memory.get("open_conflicts"), list) else []
    return {
        "schema_version": memory.get("schema_version") or 1,
        "batch_id": batch.get("batch_id") or memory.get("batch_id") or "",
        "session_id": batch.get("session_id") or memory.get("session_id") or "",
        "accepted_feature_count": len(features),
        "file_ownership_count": len(memory.get("file_ownership") or {}),
        "open_conflict_count": len(conflicts),
        "reserved_names": _path_list(memory.get("reserved_names"))[:200],
        "accepted_features": features,
        "open_conflicts": conflicts[-50:],
    }


def _semantic_memory_related_features(batch: dict[str, Any], feature: dict[str, Any]) -> list[dict[str, Any]]:
    if not batch:
        return []
    try:
        from . import reconcile_batch_memory as bm

        return bm.find_related_features(batch, {
            "primary_files": feature.get("primary") or [],
            "candidate_tests": feature.get("test") or [],
            "candidate_docs": feature.get("secondary") or [],
        })[:20]
    except Exception:  # noqa: BLE001 - advisory context only
        return []


def _semantic_memory_conflicts(ai_response: dict[str, Any]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for key in (
        "merge_suggestions",
        "split_suggestions",
        "dependency_patch_suggestions",
        "dead_code_candidates",
    ):
        value = ai_response.get(key)
        if not value:
            continue
        conflicts.append({
            "reason": key,
            "items": value if isinstance(value, list) else [value],
        })
    return conflicts


def _semantic_memory_decision_payload(
    feature: dict[str, Any],
    ai_response: dict[str, Any],
) -> dict[str, Any]:
    feature_name = str(ai_response.get("feature_name") or feature.get("title") or feature.get("node_id") or "")
    target_feature = str(ai_response.get("target_feature") or ai_response.get("merge_into") or "")
    return {
        "decision": "merge_into_existing_feature" if target_feature else "new_feature",
        "feature_name": feature_name,
        "target_feature": target_feature,
        "owned_files": feature.get("primary") or [],
        "candidate_tests": feature.get("test") or [],
        "candidate_docs": feature.get("secondary") or [],
        "reserved_names": [feature_name] if feature_name else [],
        "purpose": ai_response.get("intent")
        or ai_response.get("semantic_summary")
        or ai_response.get("purpose")
        or "",
        "reason": ai_response.get("semantic_summary") or ai_response.get("reason") or "",
        "conflicts": _semantic_memory_conflicts(ai_response),
        "decided_by": "semantic_ai",
        "actor": "reconcile_semantic_enrichment",
    }


def _record_semantic_memory_decision(
    conn: sqlite3.Connection,
    project_id: str,
    batch_id: str,
    snapshot_id: str,
    feature: dict[str, Any],
    ai_response: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    if not batch_id:
        return {}, ""
    try:
        from . import reconcile_batch_memory as bm

        node_id = str(feature.get("node_id") or "")
        feature_hash = str(feature.get("feature_hash") or "")
        fingerprint = f"semantic:{snapshot_id}:{node_id}:{feature_hash[:16]}"
        batch = bm.record_pm_decision(
            conn,
            project_id,
            batch_id,
            fingerprint,
            _semantic_memory_decision_payload(feature, ai_response),
        )
        return batch, ""
    except Exception as exc:  # noqa: BLE001 - report but keep enrichment alive
        return {}, str(exc)


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
    semantic_ai_provider: str | None = None,
    semantic_ai_model: str | None = None,
    semantic_ai_role: str | None = None,
    ai_feature_limit: int | None = None,
    semantic_ai_scope: str | None = None,
    semantic_node_ids: Any = None,
    semantic_layers: Any = None,
    semantic_quality_flags: Any = None,
    semantic_missing: Any = None,
    semantic_changed_paths: Any = None,
    semantic_path_prefixes: Any = None,
    semantic_selector_match: str | None = None,
    semantic_include_structural: bool = False,
    semantic_ai_batch_size: int | None = None,
    semantic_ai_batch_by: str = "subsystem",
    semantic_batch_memory: bool | None = None,
    semantic_batch_memory_id: str | None = None,
    trace_dir: str | Path | None = None,
    persist_feature_payloads: bool = True,
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
    if semantic_ai_provider is not None:
        semantic_config.provider = str(semantic_ai_provider or "")
    if semantic_ai_model is not None:
        semantic_config.model = str(semantic_ai_model or "")
    if semantic_ai_role is not None:
        semantic_config.role = str(semantic_ai_role or "")
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
    selector = _selector_from_kwargs(
        semantic_ai_scope=semantic_ai_scope,
        semantic_node_ids=semantic_node_ids,
        semantic_layers=semantic_layers,
        semantic_quality_flags=semantic_quality_flags,
        semantic_missing=semantic_missing,
        semantic_changed_paths=semantic_changed_paths,
        semantic_path_prefixes=semantic_path_prefixes,
        semantic_selector_match=semantic_selector_match,
        semantic_include_structural=semantic_include_structural,
    )
    nodes = _semantic_candidate_nodes(graph_json, selector)
    feature_index = _load_feature_index(snapshot)
    existing_rounds = sorted((_semantic_base_dir(project_id, snapshot_id) / "rounds").glob("round-*"))
    round_number = int(feedback_round) if feedback_round is not None else len(existing_rounds)
    generated_at = utc_now()
    semantic_features: list[dict[str, Any]] = []
    ai_complete_count = 0
    ai_unavailable_count = 0
    ai_error_count = 0
    ai_skipped_count = 0
    ai_selected_count = 0
    ai_skipped_selector_count = 0
    ai_batch_size = _normal_batch_size(semantic_ai_batch_size)
    ai_batch_count = 0
    ai_batch_complete_count = 0
    ai_batch_error_count = 0
    payload_input_paths: list[str] = []
    payload_output_paths: list[str] = []
    payload_trace_base = Path(trace_dir) if trace_dir else _round_dir(project_id, snapshot_id, round_number)
    records: list[dict[str, Any]] = []
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
        flags = _quality_flags(feature, relevant_feedback)
        selected_for_ai, selection_reasons = _selector_decision(feature, flags, selector)
        if selected_for_ai:
            ai_selected_count += 1
        payload_feature = dict(feature)
        if not semantic_config.input_policy.include_symbol_refs:
            payload_feature["symbol_refs"] = []
        if not semantic_config.input_policy.include_doc_refs:
            payload_feature["doc_refs"] = []
        if not semantic_config.input_policy.include_config_refs:
            payload_feature["config_refs"] = []
            payload_feature["config"] = []
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
            "semantic_selector": selector,
            "semantic_selection": {
                "status": "selected" if selected_for_ai else "not_selected",
                "reasons": selection_reasons,
            },
        }
        node_name = _safe_node_filename(str(feature.get("node_id") or "feature"))
        if persist_feature_payloads:
            payload_input_paths.append(write_json(
                payload_trace_base / "feature-inputs" / f"{node_name}.json",
                payload,
            ))
        records.append({
            "feature": feature,
            "feedback": relevant_feedback,
            "flags": flags,
            "payload": payload,
            "node_name": node_name,
            "selected_for_ai": selected_for_ai,
            "selection_reasons": selection_reasons,
        })

    selected_records = [
        record for record in records
        if bool(effective_use_ai) and record["selected_for_ai"]
    ]
    if ai_feature_limit is not None and ai_feature_limit >= 0:
        allowed_records = selected_records[: int(ai_feature_limit)]
    else:
        allowed_records = selected_records
    allowed_node_ids = {
        str(record["feature"].get("node_id") or "")
        for record in allowed_records
    }
    ai_responses: dict[str, dict[str, Any]] = {}
    memory_enabled = (
        bool(effective_use_ai and allowed_records and ai_batch_size > 1)
        if semantic_batch_memory is None
        else bool(semantic_batch_memory)
    )
    memory_batch: dict[str, Any] = {}
    memory_batch_id = ""
    memory_error = ""
    memory_decision_count = 0
    memory_update_error_count = 0
    if memory_enabled:
        memory_batch, memory_error = _create_semantic_batch_memory(
            conn,
            project_id,
            snapshot_id,
            round_number,
            created_by=created_by,
            batch_id=semantic_batch_memory_id,
        )
        memory_batch_id = str(memory_batch.get("batch_id") or _semantic_batch_memory_id(
            snapshot_id,
            round_number,
            semantic_batch_memory_id,
        ))
        if memory_error:
            memory_enabled = False
    if allowed_records:
        if ai_batch_size <= 1:
            for record in allowed_records:
                node_id = str(record["feature"].get("node_id") or "")
                if memory_enabled:
                    memory_batch = _refresh_semantic_batch_memory(conn, project_id, memory_batch_id) or memory_batch
                    record["payload"]["batch_memory"] = _semantic_batch_memory_summary(memory_batch)
                    record["payload"]["related_batch_features"] = _semantic_memory_related_features(
                        memory_batch,
                        record["feature"],
                    )
                    if persist_feature_payloads:
                        write_json(
                            payload_trace_base / "feature-inputs" / f"{record['node_name']}.json",
                            record["payload"],
                        )
                response = _call_ai(
                    ai_call,
                    stage="reconcile_semantic_feature",
                    payload=record["payload"],
                )
                if response is not None:
                    ai_responses[node_id] = response
                if memory_enabled and response is not None and not response.get("_ai_error"):
                    updated_batch, update_error = _record_semantic_memory_decision(
                        conn,
                        project_id,
                        memory_batch_id,
                        snapshot_id,
                        record["feature"],
                        response,
                    )
                    if update_error:
                        memory_update_error_count += 1
                    else:
                        memory_decision_count += 1
                        memory_batch = updated_batch or memory_batch
        else:
            for batch_index, batch in enumerate(
                _batch_records(
                    allowed_records,
                    batch_size=ai_batch_size,
                    batch_by=semantic_ai_batch_by,
                )
            ):
                ai_batch_count += 1
                batch_key = _batch_key(batch[0]["feature"], semantic_ai_batch_by) if batch else "all"
                if memory_enabled:
                    memory_batch = _refresh_semantic_batch_memory(conn, project_id, memory_batch_id) or memory_batch
                memory_summary = _semantic_batch_memory_summary(memory_batch) if memory_enabled else {}
                batch_payload = {
                    "schema_version": SEMANTIC_ENRICHMENT_SCHEMA_VERSION,
                    "project_id": project_id,
                    "snapshot_id": snapshot_id,
                    "snapshot_kind": snapshot.get("snapshot_kind") or "",
                    "commit_sha": snapshot.get("commit_sha") or "",
                    "feedback_round": round_number,
                    "batch_index": batch_index,
                    "batch_key": batch_key,
                    "batch_by": semantic_ai_batch_by,
                    "feature_count": len(batch),
                    "features": [
                        {
                            "feature": record["payload"]["feature"],
                            "review_feedback": record["payload"]["review_feedback"],
                            "semantic_selection": record["payload"]["semantic_selection"],
                            "quality_flags": record["flags"],
                            "related_batch_features": (
                                _semantic_memory_related_features(memory_batch, record["feature"])
                                if memory_enabled
                                else []
                            ),
                        }
                        for record in batch
                    ],
                    "batch_memory": memory_summary,
                    "instructions": {
                        **semantic_config.to_instruction_payload(),
                        "batch_mode": True,
                        "use_batch_memory": bool(memory_enabled),
                        "output_contract": (
                            "Return one JSON object with a features array. Each item must include "
                            "node_id and the same semantic fields used for single-feature enrichment."
                        ),
                    },
                    "semantic_selector": selector,
                }
                batch_name = f"batch-{batch_index:03d}-{_safe_node_filename(batch_key)}"
                if persist_feature_payloads:
                    write_json(
                        payload_trace_base / "batch-inputs" / f"{batch_name}.json",
                        batch_payload,
                    )
                batch_response = _call_ai(
                    ai_call,
                    stage="reconcile_semantic_feature_batch",
                    payload=batch_payload,
                )
                if batch_response and not batch_response.get("_ai_error"):
                    ai_batch_complete_count += 1
                else:
                    ai_batch_error_count += 1
                if persist_feature_payloads:
                    write_json(
                        payload_trace_base / "batch-outputs" / f"{batch_name}.json",
                        {
                            "batch_index": batch_index,
                            "batch_key": batch_key,
                            "node_ids": [
                                record["feature"].get("node_id") or ""
                                for record in batch
                            ],
                            "ai_response_present": bool(batch_response and not batch_response.get("_ai_error")),
                            "ai_error": (
                                batch_response.get("_ai_error")
                                if isinstance(batch_response, dict)
                                else ""
                            ),
                            "ai_response": batch_response if isinstance(batch_response, dict) else None,
                        },
                    )
                ai_responses.update(_extract_batch_ai_responses(batch_response, batch))
                for record in batch:
                    node_id = str(record["feature"].get("node_id") or "")
                    response = ai_responses.get(node_id)
                    if not (memory_enabled and response is not None and not response.get("_ai_error")):
                        continue
                    updated_batch, update_error = _record_semantic_memory_decision(
                        conn,
                        project_id,
                        memory_batch_id,
                        snapshot_id,
                        record["feature"],
                        response,
                    )
                    if update_error:
                        memory_update_error_count += 1
                    else:
                        memory_decision_count += 1
                        memory_batch = updated_batch or memory_batch

    for record in records:
        feature = record["feature"]
        relevant_feedback = record["feedback"]
        selected_for_ai = bool(record["selected_for_ai"])
        selection_reasons = record["selection_reasons"]
        node_id = str(feature.get("node_id") or "")
        node_name = record["node_name"]
        ai_allowed = node_id in allowed_node_ids
        ai_response = ai_responses.get(node_id)
        if ai_response is not None and not ai_response.get("_ai_error"):
            status = "ai_complete"
            ai_complete_count += 1
        elif ai_response is not None and ai_response.get("_ai_error"):
            status = "ai_unavailable"
            ai_unavailable_count += 1
            ai_error_count += 1
        elif effective_use_ai and not selected_for_ai:
            status = "ai_skipped_selector"
            ai_skipped_count += 1
            ai_skipped_selector_count += 1
        elif effective_use_ai and not ai_allowed:
            status = "ai_skipped_limit"
            ai_skipped_count += 1
        else:
            status = "ai_unavailable" if effective_use_ai else "heuristic"
            if effective_use_ai:
                ai_unavailable_count += 1
        semantic_entry = _heuristic_semantic_entry(
            feature,
            relevant_feedback,
            enrichment_status=status,
            ai_response=ai_response if ai_response and not ai_response.get("_ai_error") else None,
        )
        if ai_response and ai_response.get("_ai_error"):
            semantic_entry.setdefault("quality_flags", []).append("semantic_ai_error")
            semantic_entry["semantic_ai_error"] = ai_response.get("_ai_error")
        elif ai_response:
            if ai_response.get("_ai_route"):
                semantic_entry["semantic_ai_route"] = ai_response.get("_ai_route")
            if ai_response.get("_ai_elapsed_ms") is not None:
                semantic_entry["semantic_ai_elapsed_ms"] = ai_response.get("_ai_elapsed_ms")
        semantic_entry["semantic_selection_status"] = "selected" if selected_for_ai else "not_selected"
        semantic_entry["semantic_selection_reasons"] = selection_reasons
        if persist_feature_payloads:
            payload_output_paths.append(write_json(
                payload_trace_base / "feature-outputs" / f"{node_name}.json",
                {
                    "node_id": feature.get("node_id"),
                    "enrichment_status": status,
                    "ai_response_present": bool(ai_response and not ai_response.get("_ai_error")),
                    "ai_error": ai_response.get("_ai_error") if isinstance(ai_response, dict) else "",
                    "ai_response": ai_response if isinstance(ai_response, dict) else None,
                    "semantic_selector": selector,
                    "semantic_selection_status": semantic_entry["semantic_selection_status"],
                    "semantic_selection_reasons": selection_reasons,
                    "semantic_entry": semantic_entry,
                },
            ))
        semantic_features.append(semantic_entry)

    if memory_enabled and memory_batch_id:
        memory_batch = _refresh_semantic_batch_memory(conn, project_id, memory_batch_id) or memory_batch
    memory_summary = _semantic_batch_memory_summary(memory_batch) if memory_enabled else {}
    memory_report = {
        "enabled": bool(memory_enabled),
        "batch_id": memory_batch_id,
        "error": memory_error,
        "decision_count": memory_decision_count,
        "update_error_count": memory_update_error_count,
        "accepted_feature_count": memory_summary.get("accepted_feature_count", 0),
        "file_ownership_count": memory_summary.get("file_ownership_count", 0),
        "open_conflict_count": memory_summary.get("open_conflict_count", 0),
    }

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
        "semantic_selector": selector,
        "semantic_config": semantic_config.summary(),
        "semantic_batching": {
            "batch_size": ai_batch_size,
            "batch_by": semantic_ai_batch_by,
            "batch_count": ai_batch_count,
        },
        "semantic_batch_memory": memory_report,
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
        "ai_error_count": ai_error_count,
        "ai_skipped_count": ai_skipped_count,
        "ai_selected_count": ai_selected_count,
        "ai_skipped_selector_count": ai_skipped_selector_count,
        "ai_batch_size": ai_batch_size,
        "ai_batch_by": semantic_ai_batch_by,
        "ai_batch_count": ai_batch_count,
        "ai_batch_complete_count": ai_batch_complete_count,
        "ai_batch_error_count": ai_batch_error_count,
        "semantic_batch_memory": memory_report,
        "feedback_count": len(feedback),
        "semantic_selector": selector,
        "semantic_config": semantic_config.summary(),
        "unresolved_feedback_count": len(unresolved_feedback),
        "unresolved_feedback_ids": unresolved_feedback,
        "quality_flag_counts": _count_quality_flags(semantic_features),
        "feature_payload_input_count": len(payload_input_paths),
        "feature_payload_output_count": len(payload_output_paths),
        "feature_payload_input_dir": str(payload_trace_base / "feature-inputs") if persist_feature_payloads else "",
        "feature_payload_output_dir": str(payload_trace_base / "feature-outputs") if persist_feature_payloads else "",
        "batch_payload_input_dir": str(payload_trace_base / "batch-inputs") if persist_feature_payloads else "",
        "batch_payload_output_dir": str(payload_trace_base / "batch-outputs") if persist_feature_payloads else "",
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
            "ai_selected_count": ai_selected_count,
            "ai_skipped_selector_count": ai_skipped_selector_count,
            "ai_batch_size": ai_batch_size,
            "ai_batch_by": semantic_ai_batch_by,
            "ai_batch_count": ai_batch_count,
            "semantic_batch_memory": memory_report,
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
