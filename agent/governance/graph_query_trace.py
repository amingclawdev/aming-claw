"""Audited graph query substrate for dashboard and AI review.

The graph is the governance memory; this module makes graph reads traceable.
It records who queried, why, the budget used, and hashes for each query/result
without forcing every caller to stuff the full graph into a prompt.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import graph_snapshot_store as store
from . import reconcile_feedback


GRAPH_QUERY_TRACE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_query_traces (
  trace_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT '',
  query_source TEXT NOT NULL,
  query_purpose TEXT NOT NULL,
  run_id TEXT NOT NULL DEFAULT '',
  parent_task_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  budget_json TEXT NOT NULL DEFAULT '{}',
  usage_json TEXT NOT NULL DEFAULT '{}',
  artifact_path TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_graph_query_traces_project
  ON graph_query_traces(project_id, snapshot_id, query_source, status);

CREATE TABLE IF NOT EXISTS graph_query_events (
  trace_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  tool TEXT NOT NULL,
  args_hash TEXT NOT NULL DEFAULT '',
  result_hash TEXT NOT NULL DEFAULT '',
  result_count INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  PRIMARY KEY(trace_id, seq)
);
"""


QUERY_SOURCES = {
    "dashboard",
    "observer",
    "ai_semantic_review",
    "ai_global_review",
    "chain_runtime_context",
    "chain_graph_gate",
    "api_debug",
}

QUERY_PURPOSES = {
    "inspect_node",
    "health_score",
    "global_architecture_review",
    "semantic_enrichment",
    "prompt_context_build",
    "gate_validation",
    "user_feedback",
    "backlog_filing",
    "api_debug",
}

DEFAULT_BUDGETS: dict[str, dict[str, int]] = {
    "dashboard": {
        "max_queries": 20,
        "max_result_nodes": 500,
        "max_result_chars": 50_000,
        "max_file_excerpt_chars": 10_000,
    },
    "observer": {
        "max_queries": 100,
        "max_result_nodes": 2_000,
        "max_result_chars": 200_000,
        "max_file_excerpt_chars": 50_000,
    },
    "ai_semantic_review": {
        "max_queries": 30,
        "max_result_nodes": 800,
        "max_result_chars": 80_000,
        "max_file_excerpt_chars": 20_000,
    },
    "ai_global_review": {
        "max_queries": 120,
        "max_result_nodes": 3_000,
        "max_result_chars": 250_000,
        "max_file_excerpt_chars": 50_000,
    },
    "chain_runtime_context": {
        "max_queries": 20,
        "max_result_nodes": 500,
        "max_result_chars": 60_000,
        "max_file_excerpt_chars": 10_000,
    },
    "chain_graph_gate": {
        "max_queries": 50,
        "max_result_nodes": 1_000,
        "max_result_chars": 120_000,
        "max_file_excerpt_chars": 0,
    },
    "api_debug": {
        "max_queries": 20,
        "max_result_nodes": 500,
        "max_result_chars": 50_000,
        "max_file_excerpt_chars": 10_000,
    },
}

TERMINAL_TRACE_STATUSES = {"complete", "failed", "cancelled", "budget_exceeded"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_schema(conn: sqlite3.Connection) -> None:
    store.ensure_schema(conn)
    conn.executescript(GRAPH_QUERY_TRACE_SCHEMA_SQL)


def _json(data: Any) -> str:
    return json.dumps(data if data is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _decode(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except Exception:
            return default
    return default


def _string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = [raw]
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip().replace("\\", "/")
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _hash(data: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(data).encode("utf-8")).hexdigest()


def _trace_id() -> str:
    return f"gqt-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:10]}"


def _normalize_source(value: str) -> str:
    source = str(value or "").strip().lower().replace("-", "_")
    if source not in QUERY_SOURCES:
        raise ValueError(f"invalid query_source: {value!r}")
    return source


def _normalize_purpose(value: str) -> str:
    purpose = str(value or "").strip().lower().replace("-", "_")
    if purpose not in QUERY_PURPOSES:
        raise ValueError(f"invalid query_purpose: {value!r}")
    return purpose


def _budget_for(source: str, override: dict[str, Any] | None = None) -> dict[str, int]:
    base = dict(DEFAULT_BUDGETS.get(source) or DEFAULT_BUDGETS["api_debug"])
    if isinstance(override, dict):
        for key in ("max_queries", "max_result_nodes", "max_result_chars", "max_file_excerpt_chars"):
            if key in override and override[key] is not None:
                base[key] = max(0, int(override[key]))
    return base


def _empty_usage() -> dict[str, int]:
    return {
        "query_count": 0,
        "result_nodes": 0,
        "result_chars": 0,
        "file_excerpt_chars": 0,
    }


def _artifact_path(project_id: str, snapshot_id: str, trace_id: str) -> Path:
    return store.snapshot_companion_dir(project_id, snapshot_id) / "query-traces" / f"{trace_id}.jsonl"


def _append_artifact(path: str | Path, row: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_json(row) + "\n")


def start_trace(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    actor: str = "observer",
    query_source: str,
    query_purpose: str,
    run_id: str = "",
    parent_task_id: str = "",
    budget: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    source = _normalize_source(query_source)
    purpose = _normalize_purpose(query_purpose)
    tid = str(trace_id or "").strip() or _trace_id()
    now = utc_now()
    budget_json = _budget_for(source, budget)
    usage = _empty_usage()
    artifact = _artifact_path(project_id, snapshot_id, tid)
    conn.execute(
        """
        INSERT INTO graph_query_traces
          (trace_id, project_id, snapshot_id, actor, query_source, query_purpose,
           run_id, parent_task_id, status, budget_json, usage_json, artifact_path,
           created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tid,
            project_id,
            snapshot_id,
            str(actor or ""),
            source,
            purpose,
            str(run_id or ""),
            str(parent_task_id or ""),
            "running",
            _json(budget_json),
            _json(usage),
            str(artifact),
            now,
            now,
        ),
    )
    _append_artifact(artifact, {
        "event": "trace_started",
        "trace_id": tid,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "query_source": source,
        "query_purpose": purpose,
        "budget": budget_json,
        "ts": now,
    })
    return get_trace(conn, project_id, tid)


def get_trace(conn: sqlite3.Connection, project_id: str, trace_id: str) -> dict[str, Any]:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM graph_query_traces WHERE project_id = ? AND trace_id = ?",
        (project_id, trace_id),
    ).fetchone()
    if not row:
        raise KeyError(f"graph query trace not found: {trace_id}")
    trace = dict(row)
    trace["budget"] = _decode(trace.pop("budget_json", "{}"), {})
    trace["usage"] = _decode(trace.pop("usage_json", "{}"), {})
    events = conn.execute(
        """
        SELECT seq, tool, args_hash, result_hash, result_count, duration_ms, created_at
        FROM graph_query_events
        WHERE trace_id = ?
        ORDER BY seq
        """,
        (trace_id,),
    ).fetchall()
    trace["events"] = [dict(event) for event in events]
    trace["event_count"] = len(trace["events"])
    return {"ok": True, "trace": trace}


def finish_trace(
    conn: sqlite3.Connection,
    project_id: str,
    trace_id: str,
    *,
    status: str = "complete",
    reason: str = "",
) -> dict[str, Any]:
    ensure_schema(conn)
    status = str(status or "complete").strip().lower().replace("-", "_")
    if status not in TERMINAL_TRACE_STATUSES:
        raise ValueError(f"invalid trace status: {status}")
    current = get_trace(conn, project_id, trace_id)["trace"]
    now = utc_now()
    conn.execute(
        """
        UPDATE graph_query_traces
        SET status = ?, updated_at = ?
        WHERE project_id = ? AND trace_id = ?
        """,
        (status, now, project_id, trace_id),
    )
    _append_artifact(current.get("artifact_path", ""), {
        "event": "trace_finished",
        "trace_id": trace_id,
        "status": status,
        "reason": reason,
        "usage": current.get("usage", {}),
        "ts": now,
    })
    return get_trace(conn, project_id, trace_id)


def _next_seq(conn: sqlite3.Connection, trace_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM graph_query_events WHERE trace_id = ?",
        (trace_id,),
    ).fetchone()
    return int(row["seq"] if hasattr(row, "keys") else row[0])


def _result_count(result: Any) -> int:
    if isinstance(result, dict):
        for key in ("count", "result_count", "node_count", "edge_count", "match_count", "feedback_count"):
            if key in result:
                try:
                    return int(result[key])
                except Exception:
                    pass
        total = 0
        for key in ("nodes", "edges", "features", "matches", "items", "files", "feedback"):
            value = result.get(key)
            if isinstance(value, list):
                total += len(value)
        return total
    if isinstance(result, list):
        return len(result)
    return 1 if result is not None else 0


def _file_excerpt_chars(result: Any) -> int:
    if not isinstance(result, dict):
        return 0
    total = 0
    for key in ("excerpt", "matches_excerpt"):
        value = result.get(key)
        if isinstance(value, str):
            total += len(value)
    for item in result.get("matches") or []:
        if isinstance(item, dict):
            total += len(str(item.get("line") or ""))
    return total


def _budget_exceeded(usage: dict[str, int], budget: dict[str, int]) -> str:
    checks = [
        ("max_queries", "query_count"),
        ("max_result_nodes", "result_nodes"),
        ("max_result_chars", "result_chars"),
        ("max_file_excerpt_chars", "file_excerpt_chars"),
    ]
    for budget_key, usage_key in checks:
        if int(budget.get(budget_key, 0)) >= 0 and int(usage.get(usage_key, 0)) > int(budget.get(budget_key, 0)):
            return budget_key
    return ""


def _load_trace_for_query(conn: sqlite3.Connection, project_id: str, trace_id: str) -> dict[str, Any]:
    trace = get_trace(conn, project_id, trace_id)["trace"]
    if trace.get("status") in TERMINAL_TRACE_STATUSES:
        raise ValueError(f"graph query trace is terminal: {trace.get('status')}")
    return trace


def _node_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "node_id": row["node_id"],
        "layer": row["layer"],
        "title": row["title"],
        "kind": row["kind"],
        "primary_files": _decode(row["primary_files_json"], []),
        "secondary_files": _decode(row["secondary_files_json"], []),
        "test_files": _decode(row["test_files_json"], []),
        "metadata": _decode(row["metadata_json"], {}),
    }


def _compact_node(node: dict[str, Any]) -> dict[str, Any]:
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    graph_metrics = metadata.get("graph_metrics") if isinstance(metadata.get("graph_metrics"), dict) else {}
    functions = metadata.get("functions") if isinstance(metadata.get("functions"), list) else []
    config_files = _string_list(metadata.get("config_files"))
    return {
        "node_id": node.get("node_id", ""),
        "layer": node.get("layer", ""),
        "title": node.get("title", ""),
        "kind": node.get("kind", ""),
        "primary_files": _string_list(node.get("primary_files"))[:5],
        "secondary_count": len(_string_list(node.get("secondary_files"))),
        "test_count": len(_string_list(node.get("test_files"))),
        "config_count": len(config_files),
        "metadata": {
            "hierarchy_parent": metadata.get("hierarchy_parent", ""),
            "area_key": metadata.get("area_key", ""),
            "subsystem_key": metadata.get("subsystem_key", ""),
            "module": metadata.get("module", ""),
            "file_role": metadata.get("file_role", ""),
            "function_count": metadata.get("function_count", len(functions)),
            "quality_flags": _string_list(metadata.get("quality_flags"))[:12],
            "graph_metrics": {
                "fan_in": graph_metrics.get("fan_in", 0),
                "fan_out": graph_metrics.get("fan_out", 0),
                "hierarchy_in": graph_metrics.get("hierarchy_in", 0),
                "hierarchy_out": graph_metrics.get("hierarchy_out", 0),
            },
        },
    }


def _compact_semantic(semantic: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": semantic.get("status", ""),
        "feature_name": semantic.get("feature_name", ""),
        "domain_label": semantic.get("domain_label", ""),
        "intent": semantic.get("intent", ""),
        "quality_flags": _string_list(semantic.get("quality_flags"))[:12],
        "open_issue_count": len(semantic.get("open_issues") or []),
        "doc_status": semantic.get("doc_status", ""),
        "test_status": semantic.get("test_status", ""),
        "config_status": semantic.get("config_status", ""),
        "feature_hash": semantic.get("feature_hash", ""),
        "feedback_round": semantic.get("feedback_round", 0),
        "updated_at": semantic.get("updated_at", ""),
    }


def _get_node_row(conn: sqlite3.Connection, project_id: str, snapshot_id: str, node_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT node_id, layer, title, kind, primary_files_json,
               secondary_files_json, test_files_json, metadata_json
        FROM graph_nodes_index
        WHERE project_id = ? AND snapshot_id = ? AND node_id = ?
        """,
        (project_id, snapshot_id, node_id),
    ).fetchone()
    return _node_from_row(row) if row else None


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _semantic_for_node(conn: sqlite3.Connection, project_id: str, snapshot_id: str, node_id: str) -> dict[str, Any]:
    if not _table_exists(conn, "graph_semantic_nodes"):
        return {}
    try:
        row = conn.execute(
            """
            SELECT status, feature_hash, file_hashes_json, semantic_json,
                   feedback_round, batch_index, updated_at
            FROM graph_semantic_nodes
            WHERE project_id = ? AND snapshot_id = ? AND node_id = ?
            """,
            (project_id, snapshot_id, node_id),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if not row:
        return {}
    semantic = _decode(row["semantic_json"], {})
    semantic.update({
        "status": row["status"],
        "feature_hash": row["feature_hash"],
        "file_hashes": _decode(row["file_hashes_json"], {}),
        "feedback_round": row["feedback_round"],
        "batch_index": row["batch_index"],
        "updated_at": row["updated_at"],
    })
    return semantic


def _feedback_for_node(project_id: str, snapshot_id: str, node_id: str, *, limit: int = 20) -> dict[str, Any]:
    items = reconcile_feedback.list_feedback_items(
        project_id,
        snapshot_id,
        node_id=node_id,
        limit=limit,
    )
    return {
        "feedback_count": len(items),
        "feedback": [
            {
                "feedback_id": item.get("feedback_id", ""),
                "status": item.get("status", ""),
                "feedback_kind": item.get("final_feedback_kind") or item.get("feedback_kind", ""),
                "priority": item.get("priority", ""),
                "target_type": item.get("target_type", ""),
                "target_id": item.get("target_id", ""),
                "issue": item.get("issue", ""),
                "requires_human_signoff": bool(item.get("requires_human_signoff")),
            }
            for item in items
        ],
    }


def _query_list_layers(conn: sqlite3.Connection, project_id: str, snapshot_id: str, args: dict[str, Any]) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT layer, COUNT(*) AS count
        FROM graph_nodes_index
        WHERE project_id = ? AND snapshot_id = ?
        GROUP BY layer
        ORDER BY layer
        """,
        (project_id, snapshot_id),
    ).fetchall()
    return {"layers": [dict(row) for row in rows], "count": len(rows)}


def _query_list_subsystems(conn: sqlite3.Connection, project_id: str, snapshot_id: str, args: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(args.get("limit") or 200), 1000))
    nodes = store.list_graph_snapshot_nodes(
        conn,
        project_id,
        snapshot_id,
        layer="L3",
        limit=limit,
        include_semantic=not bool(args.get("compact")),
    )
    if bool(args.get("compact")):
        nodes = [_compact_node(node) for node in nodes]
    return {"subsystems": nodes, "count": len(nodes)}


def _query_list_features(conn: sqlite3.Connection, project_id: str, snapshot_id: str, args: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(args.get("limit") or 200), 1000))
    l3_id = str(args.get("l3_id") or args.get("parent_id") or "").strip()
    nodes = store.list_graph_snapshot_nodes(conn, project_id, snapshot_id, layer=str(args.get("layer") or "L7"), limit=1000)
    if l3_id:
        nodes = [
            node for node in nodes
            if str((node.get("metadata") or {}).get("hierarchy_parent") or "") == l3_id
        ]
    nodes = nodes[:limit]
    return {"features": nodes, "count": len(nodes), "l3_id": l3_id}


def _query_get_node(conn: sqlite3.Connection, project_id: str, snapshot_id: str, args: dict[str, Any]) -> dict[str, Any]:
    node_id = str(args.get("node_id") or args.get("id") or "").strip()
    if not node_id:
        raise ValueError("node_id is required")
    node = _get_node_row(conn, project_id, snapshot_id, node_id)
    if not node:
        return {"ok": False, "error": "node_not_found", "node_id": node_id, "count": 0}
    compact = bool(args.get("compact"))
    result = {"ok": True, "node": _compact_node(node) if compact else node, "count": 1}
    if bool(args.get("include_semantic", True)):
        semantic = _semantic_for_node(conn, project_id, snapshot_id, node_id)
        result["semantic"] = _compact_semantic(semantic) if compact else semantic
    if bool(args.get("include_feedback")):
        result["feedback"] = _feedback_for_node(project_id, snapshot_id, node_id, limit=int(args.get("feedback_limit") or 20))
    return result


def _query_get_neighbors(conn: sqlite3.Connection, project_id: str, snapshot_id: str, args: dict[str, Any]) -> dict[str, Any]:
    node_id = str(args.get("node_id") or args.get("id") or "").strip()
    if not node_id:
        raise ValueError("node_id is required")
    direction = str(args.get("direction") or "both").strip().lower()
    limit = max(1, min(int(args.get("limit") or 100), 500))
    params: list[Any] = [project_id, snapshot_id]
    if direction == "in":
        where = "dst = ?"
        params.append(node_id)
    elif direction == "out":
        where = "src = ?"
        params.append(node_id)
    else:
        where = "(src = ? OR dst = ?)"
        params.extend([node_id, node_id])
    rows = conn.execute(
        f"""
        SELECT src, dst, edge_type, direction, evidence_json
        FROM graph_edges_index
        WHERE project_id = ? AND snapshot_id = ? AND {where}
        ORDER BY edge_type, src, dst
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    edges = [
        {
            "src": row["src"],
            "dst": row["dst"],
            "edge_type": row["edge_type"],
            "direction": row["direction"],
            "evidence": _decode(row["evidence_json"], {}),
        }
        for row in rows
    ]
    neighbor_ids = sorted({
        edge["src"] if edge["src"] != node_id else edge["dst"]
        for edge in edges
        if edge.get("src") or edge.get("dst")
    })
    nodes = [
        node for node in (
            _get_node_row(conn, project_id, snapshot_id, nid)
            for nid in neighbor_ids
        )
        if node
    ]
    return {"node_id": node_id, "edges": edges, "nodes": nodes, "count": len(edges)}


def _query_search_semantic(conn: sqlite3.Connection, project_id: str, snapshot_id: str, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or args.get("q") or "").strip()
    if not query:
        raise ValueError("query is required")
    limit = max(1, min(int(args.get("limit") or 20), 100))
    needle = query.lower()
    if _table_exists(conn, "graph_semantic_nodes"):
        rows = conn.execute(
            """
            SELECT n.node_id, n.layer, n.title, n.kind, n.primary_files_json,
                   n.secondary_files_json, n.test_files_json, n.metadata_json,
                   s.status, s.feature_hash, s.semantic_json
            FROM graph_nodes_index n
            LEFT JOIN graph_semantic_nodes s
              ON s.project_id = n.project_id
             AND s.snapshot_id = n.snapshot_id
             AND s.node_id = n.node_id
            WHERE n.project_id = ? AND n.snapshot_id = ?
            """,
            (project_id, snapshot_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT node_id, layer, title, kind, primary_files_json,
                   secondary_files_json, test_files_json, metadata_json
            FROM graph_nodes_index
            WHERE project_id = ? AND snapshot_id = ?
            """,
            (project_id, snapshot_id),
        ).fetchall()
    matches: list[dict[str, Any]] = []
    for row in rows:
        node = _node_from_row(row)
        semantic = _decode(row["semantic_json"], {}) if "semantic_json" in row.keys() else {}
        haystack = " ".join([
            node.get("node_id", ""),
            node.get("title", ""),
            str(semantic.get("feature_name") or ""),
            str(semantic.get("domain_label") or ""),
            str(semantic.get("intent") or ""),
            str(semantic.get("semantic_summary") or ""),
        ]).lower()
        if needle not in haystack:
            continue
        matches.append({
            "node": node,
            "semantic": {
                "status": row["status"] if "status" in row.keys() else "",
                "feature_hash": row["feature_hash"] if "feature_hash" in row.keys() else "",
                "feature_name": semantic.get("feature_name", ""),
                "domain_label": semantic.get("domain_label", ""),
                "intent": semantic.get("intent", ""),
                "semantic_summary": semantic.get("semantic_summary", ""),
                "quality_flags": semantic.get("quality_flags", []),
            },
        })
        if len(matches) >= limit:
            break
    return {"query": query, "matches": matches, "count": len(matches)}


def _query_search_docs(conn: sqlite3.Connection, project_id: str, snapshot_id: str, args: dict[str, Any], project_root: str | Path | None) -> dict[str, Any]:
    query = str(args.get("query") or args.get("q") or "").strip()
    if not query:
        raise ValueError("query is required")
    limit = max(1, min(int(args.get("limit") or 20), 100))
    files = store.list_graph_snapshot_files(
        conn,
        project_id,
        snapshot_id,
        limit=1000,
    ).get("files", [])
    doc_paths = [
        str(row.get("path") or "")
        for row in files
        if str(row.get("file_kind") or "") in {"doc", "index_doc"} and str(row.get("path") or "")
    ]
    grep = reconcile_feedback.grep_in_scope(
        project_id,
        snapshot_id,
        project_root=project_root,
        pattern=query,
        paths=doc_paths,
        max_matches=limit,
        max_chars=int(args.get("max_chars") or 8000),
    )
    return {"query": query, **grep, "count": len(grep.get("matches") or [])}


def _files_for_node(conn: sqlite3.Connection, project_id: str, snapshot_id: str, args: dict[str, Any], key: str) -> dict[str, Any]:
    node_id = str(args.get("node_id") or args.get("id") or "").strip()
    if not node_id:
        raise ValueError("node_id is required")
    node = _get_node_row(conn, project_id, snapshot_id, node_id)
    if not node:
        return {"ok": False, "error": "node_not_found", "node_id": node_id, "files": [], "count": 0}
    files = list(node.get(key) or [])
    if key == "metadata_config_files":
        metadata = node.get("metadata") or {}
        files = list(metadata.get("config_files") or [])
    return {"ok": True, "node_id": node_id, "files": files, "count": len(files)}


def _query_list_unresolved_feedback(conn: sqlite3.Connection, project_id: str, snapshot_id: str, args: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(args.get("limit") or 50), 200))
    items = reconcile_feedback.list_feedback_items(project_id, snapshot_id, limit=1000)
    unresolved = [
        item for item in items
        if str(item.get("status") or "") not in {
            reconcile_feedback.STATUS_REVIEWED,
            reconcile_feedback.STATUS_ACCEPTED,
            reconcile_feedback.STATUS_REJECTED,
            reconcile_feedback.STATUS_BACKLOG_FILED,
        }
    ]
    return {
        "feedback": unresolved[:limit],
        "count": len(unresolved[:limit]),
        "total_unresolved_count": len(unresolved),
    }


def _query_list_low_health_nodes(conn: sqlite3.Connection, project_id: str, snapshot_id: str, args: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(args.get("limit") or 50), 200))
    if _table_exists(conn, "graph_semantic_nodes"):
        rows = conn.execute(
            """
            SELECT n.node_id, n.layer, n.title, n.kind, n.primary_files_json,
                   n.secondary_files_json, n.test_files_json, n.metadata_json,
                   s.status, s.semantic_json
            FROM graph_nodes_index n
            LEFT JOIN graph_semantic_nodes s
              ON s.project_id = n.project_id
             AND s.snapshot_id = n.snapshot_id
             AND s.node_id = n.node_id
            WHERE n.project_id = ? AND n.snapshot_id = ? AND n.layer = 'L7'
            ORDER BY n.node_id
            """,
            (project_id, snapshot_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT node_id, layer, title, kind, primary_files_json,
                   secondary_files_json, test_files_json, metadata_json
            FROM graph_nodes_index
            WHERE project_id = ? AND snapshot_id = ? AND layer = 'L7'
            ORDER BY node_id
            """,
            (project_id, snapshot_id),
        ).fetchall()
    feedback = reconcile_feedback.list_feedback_items(project_id, snapshot_id, limit=5000)
    feedback_by_node: dict[str, list[dict[str, Any]]] = {}
    for item in feedback:
        for node_id in item.get("source_node_ids") or []:
            feedback_by_node.setdefault(str(node_id), []).append(item)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        node = _node_from_row(row)
        semantic = _decode(row["semantic_json"], {}) if "semantic_json" in row.keys() else {}
        issues = list(semantic.get("quality_flags") or [])
        if not node.get("secondary_files"):
            issues.append("missing_doc_binding")
        if not node.get("test_files"):
            issues.append("missing_test_binding")
        signoff = [
            item for item in feedback_by_node.get(node["node_id"], [])
            if item.get("requires_human_signoff") or item.get("status") == reconcile_feedback.STATUS_NEEDS_HUMAN_SIGNOFF
        ]
        if signoff:
            issues.append("needs_human_signoff")
        function_count = int((node.get("metadata") or {}).get("function_count") or 0)
        if function_count >= 50:
            issues.append("high_function_count")
        if not issues:
            continue
        score = 100
        score -= 8 * issues.count("missing_test_binding")
        score -= 6 * issues.count("missing_doc_binding")
        score -= 10 * issues.count("needs_human_signoff")
        score -= 5 if "high_function_count" in issues else 0
        candidates.append({
            "node": _compact_node(node) if bool(args.get("compact")) else node,
            "semantic_status": row["status"] if "status" in row.keys() else "",
            "issues": sorted(set(issues)),
            "approx_health_score": max(0, score),
        })
    candidates.sort(key=lambda item: (item["approx_health_score"], item["node"]["node_id"]))
    return {"nodes": candidates[:limit], "count": len(candidates[:limit]), "total_low_health_count": len(candidates)}


def _query_file_excerpt(args: dict[str, Any], project_root: str | Path | None) -> dict[str, Any]:
    path = str(args.get("path") or args.get("rel_path") or "").strip()
    if not path:
        raise ValueError("path is required")
    return reconcile_feedback.read_project_excerpt(
        project_root,
        path,
        line_start=int(args.get("line_start") or 1),
        line_end=int(args["line_end"]) if args.get("line_end") is not None else None,
        max_lines=int(args.get("max_lines") or 80),
        max_chars=int(args.get("max_chars") or 8000),
    )


def run_tool(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    tool: str,
    args: dict[str, Any] | None = None,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    tool = str(tool or "").strip().lower()
    args = dict(args or {})
    if tool == "list_layers":
        return _query_list_layers(conn, project_id, snapshot_id, args)
    if tool == "list_subsystems":
        return _query_list_subsystems(conn, project_id, snapshot_id, args)
    if tool == "list_features":
        return _query_list_features(conn, project_id, snapshot_id, args)
    if tool == "get_node":
        return _query_get_node(conn, project_id, snapshot_id, args)
    if tool == "get_neighbors":
        return _query_get_neighbors(conn, project_id, snapshot_id, args)
    if tool == "search_semantic":
        return _query_search_semantic(conn, project_id, snapshot_id, args)
    if tool == "search_docs":
        return _query_search_docs(conn, project_id, snapshot_id, args, project_root)
    if tool == "get_docs":
        return _files_for_node(conn, project_id, snapshot_id, args, "secondary_files")
    if tool == "get_tests":
        return _files_for_node(conn, project_id, snapshot_id, args, "test_files")
    if tool == "get_config":
        return _files_for_node(conn, project_id, snapshot_id, args, "metadata_config_files")
    if tool == "list_unresolved_feedback":
        return _query_list_unresolved_feedback(conn, project_id, snapshot_id, args)
    if tool == "list_low_health_nodes":
        return _query_list_low_health_nodes(conn, project_id, snapshot_id, args)
    if tool == "get_file_excerpt":
        return _query_file_excerpt(args, project_root)
    raise ValueError(f"unsupported graph query tool: {tool}")


def traced_query(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    tool: str,
    args: dict[str, Any] | None = None,
    trace_id: str = "",
    actor: str = "observer",
    query_source: str = "api_debug",
    query_purpose: str = "api_debug",
    run_id: str = "",
    parent_task_id: str = "",
    budget: dict[str, Any] | None = None,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    if not trace_id:
        trace = start_trace(
            conn,
            project_id,
            snapshot_id,
            actor=actor,
            query_source=query_source,
            query_purpose=query_purpose,
            run_id=run_id,
            parent_task_id=parent_task_id,
            budget=budget,
        )["trace"]
        trace_id = trace["trace_id"]
    else:
        trace = _load_trace_for_query(conn, project_id, trace_id)
    usage = dict(trace.get("usage") or _empty_usage())
    budget_json = dict(trace.get("budget") or _budget_for(trace.get("query_source") or query_source, budget))
    if usage.get("query_count", 0) >= budget_json.get("max_queries", 0):
        finish_trace(conn, project_id, trace_id, status="budget_exceeded", reason="max_queries")
        return {
            "ok": False,
            "error": "query_budget_exceeded",
            "budget_key": "max_queries",
            "trace_id": trace_id,
            "usage": usage,
            "budget": budget_json,
        }

    started = time.perf_counter()
    args = dict(args or {})
    result: dict[str, Any]
    error = ""
    try:
        result = run_tool(conn, project_id, snapshot_id, tool=tool, args=args, project_root=project_root)
        result.setdefault("ok", True)
    except Exception as exc:
        error = str(exc)
        result = {"ok": False, "error": error}
    duration_ms = int((time.perf_counter() - started) * 1000)
    result_count = _result_count(result)
    result_chars = len(_json(result))
    excerpt_chars = _file_excerpt_chars(result)
    next_usage = {
        "query_count": int(usage.get("query_count", 0)) + 1,
        "result_nodes": int(usage.get("result_nodes", 0)) + result_count,
        "result_chars": int(usage.get("result_chars", 0)) + result_chars,
        "file_excerpt_chars": int(usage.get("file_excerpt_chars", 0)) + excerpt_chars,
    }
    budget_key = _budget_exceeded(next_usage, budget_json)
    status = "budget_exceeded" if budget_key else ("error" if error else "ok")
    if budget_key:
        result = {
            "ok": False,
            "error": "query_budget_exceeded",
            "budget_key": budget_key,
            "trace_id": trace_id,
            "usage": next_usage,
            "budget": budget_json,
        }
        result_count = 0
        result_chars = len(_json(result))

    seq = _next_seq(conn, trace_id)
    args_hash = _hash(args)
    result_hash = _hash(result)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO graph_query_events
          (trace_id, seq, tool, args_hash, result_hash, result_count, duration_ms, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (trace_id, seq, tool, args_hash, result_hash, int(result_count), int(duration_ms), now),
    )
    conn.execute(
        """
        UPDATE graph_query_traces
        SET usage_json = ?, status = CASE WHEN ? = 'budget_exceeded' THEN 'budget_exceeded' ELSE status END,
            updated_at = ?
        WHERE project_id = ? AND trace_id = ?
        """,
        (_json(next_usage), status, now, project_id, trace_id),
    )
    _append_artifact(trace.get("artifact_path", ""), {
        "event": "query",
        "trace_id": trace_id,
        "seq": seq,
        "tool": tool,
        "args": args,
        "args_hash": args_hash,
        "result": result,
        "result_hash": result_hash,
        "result_count": result_count,
        "duration_ms": duration_ms,
        "status": status,
        "ts": now,
    })
    return {
        "ok": bool(result.get("ok", False)),
        "trace_id": trace_id,
        "seq": seq,
        "tool": tool,
        "result": result,
        "result_count": result_count,
        "duration_ms": duration_ms,
        "usage": next_usage,
        "budget": budget_json,
        "budget_exceeded": bool(budget_key),
        "budget_key": budget_key,
        "args_hash": args_hash,
        "result_hash": result_hash,
    }


__all__ = [
    "QUERY_PURPOSES",
    "QUERY_SOURCES",
    "ensure_schema",
    "finish_trace",
    "get_trace",
    "run_tool",
    "start_trace",
    "traced_query",
]
