"""AI summary jobs for structural semantic memory.

Summary jobs reuse the graph_semantic_jobs and Review Queue substrate, but
their source evidence is accepted descendant semantics rather than source-file
feature evidence.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Callable, Mapping

from . import graph_snapshot_store as store
from . import reconcile_semantic_enrichment as semantic


STRUCTURAL_SUMMARY_LAYERS = {"L1", "L2", "L3"}
SUMMARY_OPERATION_TYPE = "ai_summary"
SUMMARY_SEMANTIC_KIND = "summary"
SummaryAiCall = Callable[[str, dict[str, Any]], Any]


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _hash_payload(payload: Any) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        parsed = json.loads(str(raw or ""))
    except (TypeError, json.JSONDecodeError):
        return default
    return parsed if parsed is not None else default


def _node_layer_by_id(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> dict[str, str]:
    rows = store.list_graph_snapshot_nodes(
        conn,
        project_id,
        snapshot_id,
        limit=1000,
        include_semantic=False,
    )
    return {
        str(row.get("node_id") or "").strip(): str(row.get("layer") or "").upper()
        for row in rows
        if str(row.get("node_id") or "").strip()
    }


def validate_summary_targets(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    target_scope: str,
    target_ids: list[str],
) -> list[str]:
    from .errors import ValidationError

    normalized_scope = str(target_scope or "").strip().lower().replace("-", "_")
    if normalized_scope in {"edge", "edges", "snapshot"}:
        raise ValidationError("semantic_summary supports only node or subtree targets")
    if normalized_scope not in {"node", "nodes", "subtree"}:
        raise ValidationError(f"unsupported semantic_summary target_scope: {target_scope}")
    clean_ids = [str(item or "").strip() for item in target_ids if str(item or "").strip()]
    if not clean_ids:
        raise ValidationError("target_ids is required for semantic_summary jobs")

    layers = _node_layer_by_id(conn, project_id, snapshot_id)
    missing = [node_id for node_id in clean_ids if node_id not in layers]
    if missing:
        raise ValidationError(f"semantic_summary target node not found: {', '.join(missing)}")
    unsupported = [
        f"{node_id}({layers[node_id] or 'unknown'})"
        for node_id in clean_ids
        if layers[node_id] not in STRUCTURAL_SUMMARY_LAYERS
    ]
    if unsupported:
        raise ValidationError(
            "semantic_summary MVP supports only L1/L2/L3 structural targets: "
            + ", ".join(unsupported)
        )
    return clean_ids


def _descendant_ids(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    target_id: str,
) -> list[str]:
    nodes = store.list_graph_snapshot_nodes(
        conn,
        project_id,
        snapshot_id,
        limit=1000,
        include_semantic=False,
    )
    children_by_parent: dict[str, set[str]] = {}
    for node in nodes:
        node_id = str(node.get("node_id") or "").strip()
        metadata = node.get("metadata") if isinstance(node.get("metadata"), Mapping) else {}
        parent_id = str(metadata.get("hierarchy_parent") or "").strip()
        if node_id and parent_id:
            children_by_parent.setdefault(parent_id, set()).add(node_id)
    for edge in store.list_graph_snapshot_edges(
        conn,
        project_id,
        snapshot_id,
        limit=2000,
    ):
        edge_type = str(edge.get("edge_type") or "").strip()
        if edge_type not in {"contains", "hierarchy"}:
            continue
        src = str(edge.get("src") or "").strip()
        dst = str(edge.get("dst") or "").strip()
        if src and dst:
            children_by_parent.setdefault(src, set()).add(dst)

    descendants: list[str] = []
    seen = {str(target_id or "").strip()}
    stack = sorted(children_by_parent.get(str(target_id or "").strip(), set()), reverse=True)
    while stack:
        node_id = stack.pop()
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        descendants.append(node_id)
        for child_id in sorted(children_by_parent.get(node_id, set()), reverse=True):
            if child_id not in seen:
                stack.append(child_id)
    return descendants


def collect_summary_source(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    target_id: str,
    *,
    require_current_children: bool = True,
) -> dict[str, Any]:
    descendant_ids = _descendant_ids(conn, project_id, snapshot_id, target_id)
    semantic._ensure_semantic_state_schema(conn)
    rows: list[dict[str, Any]] = []
    if descendant_ids:
        placeholders = ",".join("?" for _ in descendant_ids)
        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT node_id, status, feature_hash, semantic_json, payload_hash,
                       operation_type, updated_at
                FROM graph_semantic_nodes
                WHERE project_id = ?
                  AND snapshot_id = ?
                  AND status = 'ai_complete'
                  AND node_id IN ({placeholders})
                ORDER BY node_id
                """,
                [project_id, snapshot_id, *descendant_ids],
            ).fetchall()
        ]
    if require_current_children and not rows:
        raise ValueError(
            f"semantic_summary requires accepted child semantics for {target_id}; "
            "no descendant graph_semantic_nodes rows with status ai_complete were found"
        )
    child_semantics: list[dict[str, Any]] = []
    for row in rows:
        payload = _loads(row.get("semantic_json"), {})
        child_semantics.append({
            "node_id": str(row.get("node_id") or ""),
            "feature_hash": str(row.get("feature_hash") or ""),
            "payload_hash": str(row.get("payload_hash") or ""),
            "operation_type": str(row.get("operation_type") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "semantic": payload if isinstance(payload, dict) else {},
        })
    hash_payload = {
        "schema_version": 1,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "target_id": target_id,
        "summary_source": "child_semantics",
        "child_semantics": child_semantics,
    }
    source_hash = _hash_payload(hash_payload)
    return {
        "schema_version": 1,
        "summary_source": "child_semantics",
        "target_id": target_id,
        "descendant_ids": descendant_ids,
        "child_count": len(child_semantics),
        "child_semantics": child_semantics,
        "summary_source_hash": source_hash,
    }


def queue_summary_jobs(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    target_ids: list[str],
    created_by: str,
) -> list[dict[str, Any]]:
    semantic._ensure_semantic_state_schema(conn)
    now = semantic.utc_now()
    jobs: list[dict[str, Any]] = []
    for node_id in target_ids:
        conn.execute(
            """
            INSERT INTO graph_semantic_jobs
              (project_id, snapshot_id, node_id, status, feature_hash,
               file_hashes_json, branch_ref, operation_type, feedback_round,
               batch_index, attempt_count, worker_id, claim_id, claimed_at,
               lease_expires_at, claimed_by, last_error, updated_at, created_at)
            VALUES (?, ?, ?, 'pending_ai', '', '{}', '', ?, 0, NULL, 0, '', '',
                    '', '', '', '', ?, ?)
            ON CONFLICT(project_id, snapshot_id, node_id) DO UPDATE SET
              status = excluded.status,
              feature_hash = excluded.feature_hash,
              file_hashes_json = excluded.file_hashes_json,
              operation_type = excluded.operation_type,
              worker_id = '',
              claim_id = '',
              claimed_at = '',
              lease_expires_at = '',
              claimed_by = '',
              last_error = '',
              updated_at = excluded.updated_at
            """,
            (project_id, snapshot_id, node_id, SUMMARY_OPERATION_TYPE, now, now),
        )
        jobs.append({
            "node_id": node_id,
            "status": "pending_ai",
            "operation_type": SUMMARY_OPERATION_TYPE,
            "created_by": created_by,
            "updated_at": now,
        })
    return jobs


def build_summary_ai_payload(
    project_id: str,
    snapshot_id: str,
    target_id: str,
    summary_source: Mapping[str, Any],
    *,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "task": "semantic_summary",
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "target_id": target_id,
        "semantic_kind": SUMMARY_SEMANTIC_KIND,
        "operation_type": SUMMARY_OPERATION_TYPE,
        "summary_source": dict(summary_source),
        "options": dict(options or {}),
        "instructions": {
            "use_only_summary_source": True,
            "produce_pending_review_semantic_node_payload": True,
            "required_fields": [
                "node_id",
                "feature_name",
                "semantic_summary",
                "intent",
                "domain_label",
                "self_check",
                "graph_query_audit",
            ],
            "self_precheck": (
                "Run the semantic node self-check against your draft output, "
                "repair model-correctable contract errors once, and include "
                "self_check evidence in the final JSON."
            ),
        },
    }


def _normalize_ai_summary_payload(
    raw_output: Any,
    *,
    target_id: str,
    summary_source: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(raw_output) if isinstance(raw_output, Mapping) else {}
    if isinstance(payload.get("semantic_payload"), Mapping):
        payload = dict(payload["semantic_payload"])
    payload.setdefault("node_id", target_id)
    payload["semantic_kind"] = SUMMARY_SEMANTIC_KIND
    payload["operation_type"] = SUMMARY_OPERATION_TYPE
    payload["summary_source"] = {
        "source": str(summary_source.get("summary_source") or "child_semantics"),
        "hash": str(summary_source.get("summary_source_hash") or ""),
        "child_count": int(summary_source.get("child_count") or 0),
        "descendant_ids": list(summary_source.get("descendant_ids") or []),
    }
    return payload


def run_summary_job(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    target_id: str,
    ai_call: SummaryAiCall,
    options: Mapping[str, Any] | None = None,
    require_current_children: bool = True,
    created_by: str = "semantic_worker_inproc",
) -> dict[str, Any]:
    source = collect_summary_source(
        conn,
        project_id,
        snapshot_id,
        target_id,
        require_current_children=require_current_children,
    )
    ai_payload = build_summary_ai_payload(
        project_id,
        snapshot_id,
        target_id,
        source,
        options=options,
    )
    raw_output = ai_call("summary", ai_payload)
    semantic_payload = _normalize_ai_summary_payload(
        raw_output,
        target_id=target_id,
        summary_source=source,
    )
    now = semantic.utc_now()
    payload_hash = _hash_payload(semantic_payload)
    semantic._ensure_semantic_state_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash,
           file_hashes_json, semantic_json, branch_ref, operation_type,
           source_branch_ref, source_snapshot_id, source_event_id, payload_hash,
           feedback_round, batch_index, updated_at)
        VALUES (?, ?, ?, 'pending_review', ?, '{}', ?, '', ?, '', '', '', ?, 0, NULL, ?)
        ON CONFLICT(project_id, snapshot_id, node_id) DO UPDATE SET
          status = excluded.status,
          feature_hash = excluded.feature_hash,
          file_hashes_json = excluded.file_hashes_json,
          semantic_json = excluded.semantic_json,
          operation_type = excluded.operation_type,
          payload_hash = excluded.payload_hash,
          updated_at = excluded.updated_at
        """,
        (
            project_id,
            snapshot_id,
            target_id,
            str(source.get("summary_source_hash") or ""),
            _json(semantic_payload),
            SUMMARY_OPERATION_TYPE,
            payload_hash,
            now,
        ),
    )
    from . import graph_events

    backfill = graph_events.backfill_existing_semantic_events(
        conn,
        project_id,
        snapshot_id,
        actor=created_by,
    )
    return {
        "ok": True,
        "status": "pending_review",
        "node_id": target_id,
        "feature_hash": str(source.get("summary_source_hash") or ""),
        "payload_hash": payload_hash,
        "summary_source": source,
        "semantic_payload": semantic_payload,
        "backfill": backfill,
    }


__all__ = [
    "STRUCTURAL_SUMMARY_LAYERS",
    "SUMMARY_OPERATION_TYPE",
    "SUMMARY_SEMANTIC_KIND",
    "build_summary_ai_payload",
    "collect_summary_source",
    "queue_summary_jobs",
    "run_summary_job",
    "validate_summary_targets",
]
