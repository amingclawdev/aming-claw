"""Incremental semantic/global review for scope-reconcile snapshots.

This module is state-only.  It catches a scope snapshot's semantic state up to
the changed files, compares it with the prior global picture, and records an
auditable graph query trace for the incremental review pass.
"""
from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import graph_query_trace
from . import graph_snapshot_store as store
from . import reconcile_feedback
from . import reconcile_semantic_enrichment as semantic


GlobalReviewAiCall = Callable[[str, dict[str, Any]], dict[str, Any]]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(data: Any) -> str:
    return json.dumps(data if data is not None else {}, ensure_ascii=False, sort_keys=True)


def _decode(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return default
    return default


def _write_json(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json(payload), encoding="utf-8")
    return str(path)


def _path_component(value: str, *, max_len: int = 32) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-._")
    if not text:
        text = "run"
    if len(text) <= max_len:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
    prefix = text[: max_len - len(digest) - 1].rstrip("-._")
    return f"{prefix}-{digest}"


def _string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = [raw]
    out: list[str] = []
    for item in values:
        text = str(item or "").strip().replace("\\", "/")
        if text and text not in out:
            out.append(text)
    return out


def _snapshot_notes(snapshot: dict[str, Any]) -> dict[str, Any]:
    notes = _decode(snapshot.get("notes"), {})
    return notes if isinstance(notes, dict) else {}


def _scope_delta(notes: dict[str, Any]) -> dict[str, Any]:
    pending = notes.get("pending_scope_reconcile") if isinstance(notes.get("pending_scope_reconcile"), dict) else {}
    delta = pending.get("scope_file_delta") if isinstance(pending.get("scope_file_delta"), dict) else {}
    return delta if isinstance(delta, dict) else {}


def _infer_base_snapshot_id(snapshot: dict[str, Any], explicit: str = "") -> str:
    if explicit:
        return explicit
    notes = _snapshot_notes(snapshot)
    pending = notes.get("pending_scope_reconcile") if isinstance(notes.get("pending_scope_reconcile"), dict) else {}
    for key in ("active_snapshot_id", "base_snapshot_id", "previous_snapshot_id"):
        value = str(pending.get(key) or "").strip()
        if value:
            return value
    return str(snapshot.get("parent_snapshot_id") or "")


def _infer_changed_paths(snapshot: dict[str, Any], explicit: Any = None) -> list[str]:
    explicit_paths = _string_list(explicit)
    if explicit_paths:
        return explicit_paths
    delta = _scope_delta(_snapshot_notes(snapshot))
    for key in ("impacted_files", "changed_files", "added_files", "hash_changed_files"):
        paths = _string_list(delta.get(key))
        if paths:
            return paths
    return []


def _node_paths(node: dict[str, Any]) -> set[str]:
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    paths = []
    for key in ("primary_files", "secondary_files", "test_files"):
        paths.extend(_string_list(node.get(key)))
    paths.extend(_string_list(metadata.get("config_files")))
    return set(paths)


def _changed_node_ids(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    changed_paths: list[str],
    explicit_node_ids: Any = None,
) -> list[str]:
    requested = set(_string_list(explicit_node_ids))
    changed = set(changed_paths)
    if not changed and requested:
        return sorted(requested)
    nodes = store.list_graph_snapshot_nodes(
        conn,
        project_id,
        snapshot_id,
        layer="L7",
        limit=1000,
    )
    out: set[str] = set(requested)
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        if not node_id:
            continue
        paths = _node_paths(node)
        if changed.intersection(paths):
            out.add(node_id)
    return sorted(out)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return bool(row)
    except sqlite3.Error:
        return False


def _load_semantic_nodes(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> dict[str, dict[str, Any]]:
    if not _table_exists(conn, "graph_semantic_nodes"):
        return {}
    rows = conn.execute(
        """
        SELECT node_id, status, feature_hash, file_hashes_json, semantic_json,
               feedback_round, batch_index, updated_at
        FROM graph_semantic_nodes
        WHERE project_id = ? AND snapshot_id = ?
        """,
        (project_id, snapshot_id),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = _decode(row["semantic_json"], {})
        if not isinstance(payload, dict):
            payload = {}
        out[str(row["node_id"])] = {
            **payload,
            "node_id": str(row["node_id"]),
            "status": str(row["status"] or payload.get("status") or ""),
            "feature_hash": str(row["feature_hash"] or payload.get("feature_hash") or ""),
            "file_hashes": _decode(row["file_hashes_json"], {}),
            "feedback_round": row["feedback_round"],
            "batch_index": row["batch_index"],
            "updated_at": row["updated_at"],
        }
    return out


def _load_semantic_jobs(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> dict[str, dict[str, Any]]:
    if not _table_exists(conn, "graph_semantic_jobs"):
        return {}
    rows = conn.execute(
        """
        SELECT node_id, status, feature_hash, feedback_round, batch_index,
               attempt_count, last_error, updated_at
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ?
        """,
        (project_id, snapshot_id),
    ).fetchall()
    return {str(row["node_id"]): dict(row) for row in rows}


def _semantic_picture(semantic_nodes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_domain: dict[str, int] = {}
    features: list[dict[str, Any]] = []
    for node_id, entry in sorted(semantic_nodes.items()):
        status = str(entry.get("status") or "")
        domain = str(entry.get("domain_label") or "")
        by_status[status] = by_status.get(status, 0) + 1
        if domain:
            by_domain[domain] = by_domain.get(domain, 0) + 1
        features.append({
            "node_id": node_id,
            "feature_name": entry.get("feature_name") or entry.get("source_title") or node_id,
            "domain_label": domain,
            "status": status,
            "feature_hash": entry.get("feature_hash") or "",
        })
    return {
        "semantic_node_count": len(semantic_nodes),
        "by_status": dict(sorted(by_status.items())),
        "by_domain": dict(sorted(by_domain.items())),
        "features": features[:200],
    }


def _call_ai(ai_call: GlobalReviewAiCall | None, payload: dict[str, Any]) -> dict[str, Any]:
    if ai_call is None:
        return {}
    try:
        response = ai_call("reconcile_global_semantic_review", payload)
    except Exception as exc:  # noqa: BLE001 - review remains advisory
        return {"_ai_error": str(exc)}
    return response if isinstance(response, dict) else {}


def _global_review_dir(project_id: str, snapshot_id: str) -> Path:
    return store.snapshot_companion_dir(project_id, snapshot_id) / "semantic-enrichment" / "global-review"


def _update_snapshot_global_review_notes(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    patch: dict[str, Any],
) -> None:
    row = store.get_graph_snapshot(conn, project_id, snapshot_id)
    if not row:
        return
    notes = _snapshot_notes(row)
    global_notes = notes.setdefault("global_semantic_review", {})
    if not isinstance(global_notes, dict):
        global_notes = {}
        notes["global_semantic_review"] = global_notes
    global_notes.update(patch)
    conn.execute(
        "UPDATE graph_snapshots SET notes = ? WHERE project_id = ? AND snapshot_id = ?",
        (_json(notes), project_id, snapshot_id),
    )


def _trace_incremental_context(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    actor: str,
    run_id: str,
    changed_node_ids: list[str],
    project_root: str | Path | None,
    budget: dict[str, int] | None,
) -> dict[str, Any]:
    trace = graph_query_trace.start_trace(
        conn,
        project_id,
        snapshot_id,
        actor=actor,
        query_source="ai_global_review",
        query_purpose="global_architecture_review",
        run_id=run_id,
        budget=budget,
    )["trace"]
    trace_id = trace["trace_id"]
    queries: list[dict[str, Any]] = []
    budget_exhausted = False

    def query(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        nonlocal budget_exhausted
        if budget_exhausted:
            queries.append({
                "tool": tool,
                "args": args,
                "skipped": True,
                "skip_reason": "query_budget_exhausted",
            })
            return {}
        result = graph_query_trace.traced_query(
            conn,
            project_id,
            snapshot_id,
            trace_id=trace_id,
            tool=tool,
            args=args,
            project_root=project_root,
        )
        queries.append({
            "tool": tool,
            "args": args,
            "result_count": result.get("result_count", 0),
            "budget_exceeded": result.get("budget_exceeded", False),
        })
        if result.get("budget_exceeded") or result.get("error") == "query_budget_exceeded":
            budget_exhausted = True
        return result.get("result") if isinstance(result.get("result"), dict) else {}

    query("list_subsystems", {"limit": 50})
    low_health = query("list_low_health_nodes", {"limit": 50})
    inspected: list[dict[str, Any]] = []
    for node_id in changed_node_ids[:20]:
        node = query("get_node", {"node_id": node_id, "include_semantic": True})
        neighbors = query("get_neighbors", {"node_id": node_id, "limit": 40})
        inspected.append({
            "node_id": node_id,
            "node": node.get("node") or {},
            "semantic": node.get("semantic") or {},
            "neighbor_count": neighbors.get("count", 0),
        })
    finished = graph_query_trace.finish_trace(
        conn,
        project_id,
        trace_id,
        status="budget_exceeded" if budget_exhausted else "complete",
        reason=(
            "incremental global semantic review context budget exceeded"
            if budget_exhausted
            else "incremental global semantic review context captured"
        ),
    )["trace"]
    return {
        "trace_id": trace_id,
        "trace": {
            "status": finished.get("status"),
            "usage": finished.get("usage") or {},
            "event_count": finished.get("event_count", 0),
            "artifact_path": finished.get("artifact_path", ""),
        },
        "queries": queries,
        "low_health": low_health,
        "inspected_nodes": inspected,
    }


def run_incremental_global_review(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    project_root: str | Path | None = None,
    *,
    base_snapshot_id: str = "",
    changed_paths: Any = None,
    changed_node_ids: Any = None,
    run_semantic: bool = True,
    semantic_use_ai: bool | None = None,
    semantic_ai_call: GlobalReviewAiCall | None = None,
    semantic_ai_feature_limit: int | None = None,
    semantic_ai_batch_size: int | None = None,
    semantic_ai_batch_by: str = "subsystem",
    semantic_ai_input_mode: str | None = None,
    semantic_config_path: str | Path | None = None,
    classify_feedback: bool = True,
    global_review_use_ai: bool = False,
    global_review_ai_call: GlobalReviewAiCall | None = None,
    actor: str = "observer",
    run_id: str = "",
    query_budget: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Run the post-scope incremental semantic review closure."""
    store.ensure_schema(conn)
    graph_query_trace.ensure_schema(conn)
    snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")

    base_snapshot = _infer_base_snapshot_id(snapshot, base_snapshot_id)
    paths = _infer_changed_paths(snapshot, changed_paths)
    node_ids = _changed_node_ids(
        conn,
        project_id,
        snapshot_id,
        paths,
        explicit_node_ids=changed_node_ids,
    )
    rid = run_id or f"incremental-global-review-{snapshot_id}"
    rid_path = _path_component(rid)
    semantic_result: dict[str, Any] = {}
    if run_semantic:
        semantic_result = semantic.run_semantic_enrichment(
            conn,
            project_id,
            snapshot_id,
            project_root,
            use_ai=semantic_use_ai,
            ai_call=semantic_ai_call,
            created_by=actor,
            ai_feature_limit=semantic_ai_feature_limit,
            semantic_ai_batch_size=semantic_ai_batch_size,
            semantic_ai_batch_by=semantic_ai_batch_by,
            semantic_ai_input_mode=semantic_ai_input_mode,
            semantic_ai_scope="changed" if paths else "selected",
            semantic_changed_paths=paths,
            semantic_node_ids=node_ids,
            semantic_selector_match="any",
            semantic_graph_state=True,
            semantic_skip_completed=True,
            semantic_base_snapshot_id=base_snapshot,
            semantic_config_path=semantic_config_path,
            trace_dir=(
                store.snapshot_companion_dir(project_id, snapshot_id)
                / "state-review-trace"
                / rid_path
                / "semantic-enrichment"
            ),
        )

    current_semantics = _load_semantic_nodes(conn, project_id, snapshot_id)
    base_semantics = _load_semantic_nodes(conn, project_id, base_snapshot) if base_snapshot else {}
    semantic_jobs = _load_semantic_jobs(conn, project_id, snapshot_id)
    changed_semantics = {
        node_id: current_semantics.get(node_id, {})
        for node_id in node_ids
    }
    complete_node_ids = sorted(
        node_id
        for node_id, entry in changed_semantics.items()
        if str(entry.get("status") or "") == "ai_complete"
    )
    pending_node_ids = sorted(
        node_id
        for node_id in node_ids
        if node_id not in set(complete_node_ids)
    )
    context = _trace_incremental_context(
        conn,
        project_id,
        snapshot_id,
        actor=actor,
        run_id=rid,
        changed_node_ids=node_ids,
        project_root=project_root,
        budget=query_budget,
    )

    semantic_summary_gate = semantic.feedback_review_gate(
        (semantic_result.get("summary") or {}) if semantic_result else {
            "ai_complete_count": len(complete_node_ids),
            "semantic_graph_state": {"hit_count": 0},
            "semantic_run_status": "ai_complete" if complete_node_ids else "ai_pending",
            "ai_selected_count": len(node_ids),
        }
    )
    if node_ids and pending_node_ids:
        review_gate = {
            "allowed": False,
            "reason": "changed_semantic_pending",
            "changed_node_count": len(node_ids),
            "changed_complete_count": len(complete_node_ids),
            "pending_node_count": len(pending_node_ids),
            "semantic_summary_gate": semantic_summary_gate,
        }
    elif node_ids:
        review_gate = {
            "allowed": True,
            "reason": "changed_semantic_complete",
            "changed_node_count": len(node_ids),
            "changed_complete_count": len(complete_node_ids),
            "pending_node_count": 0,
            "semantic_summary_gate": semantic_summary_gate,
        }
    else:
        review_gate = semantic_summary_gate
    blocked = bool(node_ids and pending_node_ids)
    ai_response: dict[str, Any] = {}
    ai_issues: list[dict[str, Any]] = []
    if global_review_use_ai and not blocked:
        payload = {
            "schema_version": 1,
            "mode": "incremental_global_semantic_review",
            "permissions": {
                "can": ["query_graph", "read_provided_context", "return_review_suggestions"],
                "cannot": ["modify_code", "modify_docs", "modify_tests", "mutate_graph_topology", "file_backlog"],
            },
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "base_snapshot_id": base_snapshot,
            "changed_paths": paths,
            "changed_node_ids": node_ids,
            "base_global_picture": _semantic_picture(base_semantics),
            "current_global_picture": _semantic_picture(current_semantics),
            "changed_semantics": changed_semantics,
            "graph_query_trace": context,
            "instructions": [
                "Review only the incremental change against the prior global picture.",
                "Return open_issues for graph corrections or project improvements only when evidence supports action.",
                "Represent coverage/doc/test drift as status observations unless the user explicitly requests backlog filing.",
            ],
        }
        ai_response = _call_ai(global_review_ai_call, payload)
        raw_issues = ai_response.get("open_issues") if isinstance(ai_response, dict) else []
        ai_issues = [item for item in (raw_issues or []) if isinstance(item, dict)]

    classification: dict[str, Any] = {}
    if classify_feedback and not blocked:
        if ai_issues:
            classification = reconcile_feedback.classify_semantic_open_issues(
                project_id,
                snapshot_id,
                source_round=f"global-incremental:{rid}",
                created_by=actor,
                issues=ai_issues,
                base_snapshot_id=base_snapshot,
            )
        else:
            classification = reconcile_feedback.classify_semantic_open_issues(
                project_id,
                snapshot_id,
                source_round=(semantic_result.get("feedback_round", "") if semantic_result else ""),
                created_by=actor,
                node_ids=node_ids,
                base_snapshot_id=base_snapshot,
            )

    generated_at = utc_now()
    report = {
        "schema_version": 1,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "base_snapshot_id": base_snapshot,
        "run_id": rid,
        "generated_at": generated_at,
        "changed_paths": paths,
        "changed_node_ids": node_ids,
        "complete_node_ids": complete_node_ids,
        "pending_node_ids": pending_node_ids,
        "semantic_job_statuses": {
            node_id: (semantic_jobs.get(node_id) or {}).get("status", "")
            for node_id in node_ids
        },
        "status": "blocked_semantic_pending" if blocked else "reviewed",
        "blocked": blocked,
        "review_gate": review_gate,
        "semantic_enrichment": {
            "ok": semantic_result.get("ok", False) if semantic_result else False,
            "feedback_round": semantic_result.get("feedback_round", ""),
            "summary": semantic_result.get("summary", {}) if semantic_result else {},
            "semantic_index_path": semantic_result.get("semantic_index_path", ""),
            "review_report_path": semantic_result.get("review_report_path", ""),
        },
        "base_global_picture": _semantic_picture(base_semantics),
        "current_global_picture": _semantic_picture(current_semantics),
        "graph_query_trace": context,
        "global_ai_review": {
            "requested": bool(global_review_use_ai),
            "response_present": bool(ai_response and not ai_response.get("_ai_error")),
            "error": ai_response.get("_ai_error", "") if isinstance(ai_response, dict) else "",
            "open_issue_count": len(ai_issues),
        },
        "feedback_classification": classification,
    }
    out_dir = _global_review_dir(project_id, snapshot_id)
    report_path = str(out_dir / f"{rid_path}.json")
    latest_path = str(out_dir / "latest-incremental-review.json")
    report["report_path"] = report_path
    report["latest_report_path"] = latest_path
    _write_json(Path(report_path), report)
    _write_json(Path(latest_path), report)
    _update_snapshot_global_review_notes(
        conn,
        project_id,
        snapshot_id,
        {
            "latest_incremental_review_path": latest_path,
            "latest_incremental_run_id": rid,
            "latest_incremental_status": report["status"],
            "base_snapshot_id": base_snapshot,
            "changed_node_count": len(node_ids),
            "pending_node_count": len(pending_node_ids),
            "updated_at": generated_at,
        },
    )
    return {"ok": True, **report}


__all__ = ["run_incremental_global_review"]
