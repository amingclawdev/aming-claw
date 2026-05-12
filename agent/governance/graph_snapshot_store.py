"""Commit-indexed graph snapshot state store.

This module is intentionally state-only: it stores graph snapshots, indexes,
drift rows, and pending scope-reconcile rows. It does not modify source,
documentation, or test files.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


GRAPH_SNAPSHOT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_snapshots (
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  parent_snapshot_id TEXT NOT NULL DEFAULT '',
  snapshot_kind TEXT NOT NULL,
  graph_sha256 TEXT NOT NULL DEFAULT '',
  inventory_sha256 TEXT NOT NULL DEFAULT '',
  drift_sha256 TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(project_id, snapshot_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_snapshots_commit
  ON graph_snapshots(project_id, commit_sha);

CREATE INDEX IF NOT EXISTS idx_graph_snapshots_status
  ON graph_snapshots(project_id, status, commit_sha);

CREATE TABLE IF NOT EXISTS graph_snapshot_refs (
  project_id TEXT NOT NULL,
  ref_name TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(project_id, ref_name)
);

CREATE TABLE IF NOT EXISTS graph_nodes_index (
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  layer TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT '',
  primary_files_json TEXT NOT NULL DEFAULT '[]',
  secondary_files_json TEXT NOT NULL DEFAULT '[]',
  test_files_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(project_id, snapshot_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_nodes_primary
  ON graph_nodes_index(project_id, snapshot_id, node_id);

CREATE TABLE IF NOT EXISTS graph_edges_index (
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  edge_type TEXT NOT NULL,
  direction TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(project_id, snapshot_id, src, dst, edge_type, direction)
);

CREATE INDEX IF NOT EXISTS idx_graph_edges_dst
  ON graph_edges_index(project_id, snapshot_id, dst);

CREATE TABLE IF NOT EXISTS graph_drift_ledger (
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  path TEXT NOT NULL,
  node_id TEXT NOT NULL DEFAULT '',
  target_symbol TEXT NOT NULL DEFAULT '',
  drift_type TEXT NOT NULL,
  status TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL,
  PRIMARY KEY(project_id, snapshot_id, path, drift_type, target_symbol)
);

CREATE INDEX IF NOT EXISTS idx_graph_drift_status
  ON graph_drift_ledger(project_id, status, drift_type);

CREATE TABLE IF NOT EXISTS pending_scope_reconcile (
  project_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  parent_commit_sha TEXT NOT NULL DEFAULT '',
  queued_at TEXT NOT NULL,
  status TEXT NOT NULL,
  retry_count INTEGER NOT NULL DEFAULT 0,
  snapshot_id TEXT NOT NULL DEFAULT '',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(project_id, commit_sha)
);

CREATE INDEX IF NOT EXISTS idx_pending_scope_status
  ON pending_scope_reconcile(project_id, status, queued_at);
"""

SNAPSHOT_STATUS_CANDIDATE = "candidate"
SNAPSHOT_STATUS_FINALIZING = "finalizing"
SNAPSHOT_STATUS_ACTIVE = "active"
SNAPSHOT_STATUS_SUPERSEDED = "superseded"
SNAPSHOT_STATUS_ABANDONED = "abandoned"

ALLOWED_SNAPSHOT_STATUSES = {
    SNAPSHOT_STATUS_CANDIDATE,
    SNAPSHOT_STATUS_FINALIZING,
    SNAPSHOT_STATUS_ACTIVE,
    SNAPSHOT_STATUS_SUPERSEDED,
    SNAPSHOT_STATUS_ABANDONED,
}

PENDING_STATUS_QUEUED = "queued"
PENDING_STATUS_RUNNING = "running"
PENDING_STATUS_MATERIALIZED = "materialized"
PENDING_STATUS_FAILED = "failed"
PENDING_STATUS_WAIVED = "waived"

ALLOWED_PENDING_STATUSES = {
    PENDING_STATUS_QUEUED,
    PENDING_STATUS_RUNNING,
    PENDING_STATUS_MATERIALIZED,
    PENDING_STATUS_FAILED,
    PENDING_STATUS_WAIVED,
}


class GraphSnapshotConflictError(RuntimeError):
    """Raised when snapshot activation loses its compare-and-swap race."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(GRAPH_SNAPSHOT_SCHEMA_SQL)


def _json(data: Any) -> str:
    return json.dumps(data if data is not None else {}, sort_keys=True, ensure_ascii=False)


def _json_list(data: Any) -> str:
    if data is None:
        return "[]"
    if isinstance(data, list):
        return json.dumps(data, sort_keys=True, ensure_ascii=False)
    return json.dumps([data], sort_keys=True, ensure_ascii=False)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_id_for(snapshot_kind: str, commit_sha: str, suffix: str | None = None) -> str:
    clean_kind = (snapshot_kind or "snapshot").strip().replace("_", "-").lower()
    short = (commit_sha or "unknown").strip()[:7] or "unknown"
    tail = suffix or uuid.uuid4().hex[:4]
    return f"{clean_kind}-{short}-{tail}"


def _snapshot_root(project_id: str, snapshot_id: str) -> Path:
    from .db import _governance_root

    return _governance_root() / project_id / "graph-snapshots" / snapshot_id


def snapshot_companion_dir(project_id: str, snapshot_id: str) -> Path:
    return _snapshot_root(project_id, snapshot_id)


def snapshot_graph_path(project_id: str, snapshot_id: str) -> Path:
    return snapshot_companion_dir(project_id, snapshot_id) / "graph.json"


def write_companion_files(
    project_id: str,
    snapshot_id: str,
    *,
    graph_json: dict[str, Any] | None = None,
    file_inventory: list[dict[str, Any]] | None = None,
    drift_ledger: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    base_dir = _snapshot_root(project_id, snapshot_id)
    base_dir.mkdir(parents=True, exist_ok=True)

    graph_bytes = _json(graph_json or {}).encode("utf-8")
    inventory_bytes = _json(file_inventory or []).encode("utf-8")
    drift_bytes = _json(drift_ledger or []).encode("utf-8")

    graph_sha = _sha256_bytes(graph_bytes)
    inventory_sha = _sha256_bytes(inventory_bytes)
    drift_sha = _sha256_bytes(drift_bytes)

    (base_dir / "graph.json").write_bytes(graph_bytes)
    (base_dir / "file_inventory.json").write_bytes(inventory_bytes)
    (base_dir / "drift_ledger.json").write_bytes(drift_bytes)

    manifest = {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "graph_sha256": graph_sha,
        "inventory_sha256": inventory_sha,
        "drift_sha256": drift_sha,
        "created_at": utc_now(),
    }
    (base_dir / "manifest.json").write_text(_json(manifest), encoding="utf-8")
    return {
        "graph_sha256": graph_sha,
        "inventory_sha256": inventory_sha,
        "drift_sha256": drift_sha,
        "path": str(base_dir),
    }


def _graph_nodes(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    deps = graph_json.get("deps_graph") if isinstance(graph_json, dict) else {}
    if isinstance(deps, dict) and isinstance(deps.get("nodes"), list):
        return [n for n in deps.get("nodes", []) if isinstance(n, dict)]
    nodes = graph_json.get("nodes") if isinstance(graph_json, dict) else []
    if isinstance(nodes, list):
        return [n for n in nodes if isinstance(n, dict)]
    if isinstance(nodes, dict):
        result = []
        for node_id, node in nodes.items():
            item = dict(node) if isinstance(node, dict) else {}
            item.setdefault("id", str(node_id))
            result.append(item)
        return result
    return []


def graph_payload_edges(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Return normalized hierarchy/evidence/dependency edges from a graph payload."""
    if not isinstance(graph_json, dict):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    sections = [
        ("hierarchy_graph", "hierarchy"),
        ("evidence_graph", "evidence"),
        ("deps_graph", "dependency"),
        ("gates_graph", "gate"),
    ]
    for section_name, default_direction in sections:
        section = graph_json.get(section_name)
        if not isinstance(section, dict):
            continue
        raw_edges = section.get("edges") if "edges" in section else section.get("links")
        for edge in raw_edges or []:
            if not isinstance(edge, dict):
                continue
            src = str(edge.get("src") or edge.get("source") or "")
            dst = str(edge.get("dst") or edge.get("target") or "")
            edge_type = str(edge.get("edge_type") or edge.get("type") or "depends_on")
            direction = str(edge.get("direction") or default_direction)
            if not src or not dst:
                continue
            key = (src, dst, edge_type, direction)
            if key in seen:
                continue
            item = dict(edge)
            item["src"] = src
            item["dst"] = dst
            item["edge_type"] = edge_type
            item["direction"] = direction
            evidence = item.get("evidence")
            metadata = item.get("metadata")
            if metadata and "evidence" not in item:
                item["evidence"] = metadata
            elif evidence and "metadata" not in item:
                item.setdefault("metadata", {"evidence": evidence})
            item.setdefault("section", section_name)
            result.append(item)
            seen.add(key)
    if result:
        return result
    edges = graph_json.get("edges") if isinstance(graph_json, dict) else []
    if isinstance(edges, list):
        return [e for e in edges if isinstance(e, dict)]
    return []


def _graph_edges(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    return graph_payload_edges(graph_json)


def graph_payload_stats(graph_json: dict[str, Any]) -> dict[str, int]:
    return {"nodes": len(_graph_nodes(graph_json)), "edges": len(_graph_edges(graph_json))}


def _decode_json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (list, dict)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return default
    return default


def _row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    if key not in row.keys():
        return default
    return row[key]


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _semantic_hash_state(status: str, feature_hash: str, payload: dict[str, Any]) -> str:
    status_norm = str(status or "").strip().lower()
    validation = payload.get("semantic_state_validation")
    if isinstance(validation, dict):
        validation_status = str(validation.get("status") or "").lower()
        if validation_status in {"stale_hash_mismatch", "hash_mismatch", "stale"}:
            return "stale"
        if validation.get("valid") is True:
            return "current"
        if validation.get("valid") is False:
            return "stale"

    flags = payload.get("quality_flags")
    if isinstance(flags, list):
        flag_set = {str(flag or "").strip().lower() for flag in flags}
        if flag_set.intersection({"semantic_hash_mismatch", "source_hash_changed", "semantic_stale"}):
            return "stale"

    if status_norm in {"pending_review", "review_pending"}:
        return "pending"
    if status_norm in {"ai_complete", "semantic_graph_state", "reviewed"} and feature_hash:
        return "current"
    if status_norm in {"pending_ai", "ai_pending", "running", "ai_running"}:
        return "pending"
    if status_norm in {"ai_failed", "failed"}:
        return "failed"
    return "unknown"


def _semantic_overlay_from_node_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = _decode_json(_row_value(row, "semantic_json", ""), {})
    if not isinstance(payload, dict):
        payload = {}
    file_hashes = _decode_json(_row_value(row, "semantic_file_hashes_json", ""), {})
    if not isinstance(file_hashes, dict):
        file_hashes = {}

    node_status = str(_row_value(row, "semantic_status", "") or "")
    job_status = str(_row_value(row, "semantic_job_status", "") or "")
    job_status_norm = job_status.lower()
    payload_status = str(payload.get("status") or "")
    status = node_status or payload_status or "structure_only"
    if not node_status and not payload_status and job_status_norm in {
        "pending_ai",
        "ai_pending",
        "running",
        "ai_running",
        "ai_failed",
        "failed",
        "cancelled",
        "canceled",
        "rejected",
    }:
        status = job_status
    api_status = "review_pending" if status == "pending_review" else status
    feature_hash = str(
        _row_value(row, "semantic_feature_hash", "")
        or payload.get("feature_hash")
        or ""
    )
    updated_at = str(
        _row_value(row, "semantic_updated_at", "")
        or _row_value(row, "semantic_job_updated_at", "")
        or payload.get("updated_at")
        or ""
    )

    overlay = dict(payload)
    overlay.update({
        "status": api_status,
        "node_status": node_status,
        "job_status": job_status,
        "feature_hash": feature_hash,
        "file_hashes": file_hashes,
        "feedback_round": _row_value(row, "semantic_feedback_round", payload.get("feedback_round", 0)) or 0,
        "batch_index": _row_value(row, "semantic_batch_index", payload.get("batch_index")),
        "updated_at": updated_at,
        "hash_state": _semantic_hash_state(status, feature_hash, payload),
        "has_semantic_payload": bool(node_status and payload),
    })

    if job_status:
        overlay["job"] = {
            "status": job_status,
            "feature_hash": str(_row_value(row, "semantic_job_feature_hash", "") or ""),
            "attempt_count": int(_row_value(row, "semantic_job_attempt_count", 0) or 0),
            "last_error": str(_row_value(row, "semantic_job_last_error", "") or ""),
            "worker_id": str(_row_value(row, "semantic_job_worker_id", "") or ""),
            "claim_id": str(_row_value(row, "semantic_job_claim_id", "") or ""),
            "claimed_at": str(_row_value(row, "semantic_job_claimed_at", "") or ""),
            "lease_expires_at": str(_row_value(row, "semantic_job_lease_expires_at", "") or ""),
            "claimed_by": str(_row_value(row, "semantic_job_claimed_by", "") or ""),
            "updated_at": str(_row_value(row, "semantic_job_updated_at", "") or ""),
        }
    return overlay


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_json_artifact(path: Path, default: Any) -> Any:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return payload if payload is not None else default


def _current_graph_path(project_id: str) -> Path:
    from .db import _governance_root

    return _governance_root() / project_id / "graph.json"


def _baseline_graph_path(project_id: str, baseline_id: int) -> Path:
    from .db import _governance_root

    return _governance_root() / project_id / "baselines" / str(baseline_id) / "graph.json"


def _resolve_import_commit(conn: sqlite3.Connection, project_id: str, explicit: str = "") -> str:
    if explicit:
        return explicit
    try:
        row = conn.execute(
            "SELECT chain_version, git_head FROM project_version WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row:
        # chain_version is the last governed/service version; git_head may include
        # advisory MF commits that the graph has not materialized yet.
        if hasattr(row, "keys"):
            chain_version = row["chain_version"] if "chain_version" in row.keys() else ""
            git_head = row["git_head"] if "git_head" in row.keys() else ""
        else:
            chain_version = row[0] if len(row) > 0 else ""
            git_head = row[1] if len(row) > 1 else ""
        return chain_version or git_head or "unknown"
    return "unknown"


def select_existing_graph_source(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    extra_graph_paths: Iterable[str | Path] | None = None,
) -> dict[str, Any] | None:
    """Select the best existing graph payload to import.

    Empty baseline companion graphs are skipped because older scan-only
    baselines often wrote `{}` while the active graph still lived at the
    shared-volume graph path.
    """
    ensure_schema(conn)
    try:
        rows = conn.execute(
            """
            SELECT baseline_id FROM version_baselines
            WHERE project_id = ?
            ORDER BY baseline_id DESC
            """,
            (project_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    if rows:
        from .baseline_service import read_companion_file

        for row in rows:
            baseline_id = row["baseline_id"] if hasattr(row, "keys") else row[0]
            try:
                graph_json = read_companion_file(project_id, int(baseline_id), "graph.json")
            except Exception:
                continue
            stats = graph_payload_stats(graph_json)
            if stats["nodes"] > 0:
                path = _baseline_graph_path(project_id, int(baseline_id))
                return {
                    "source_kind": "baseline_companion",
                    "source_path": str(path),
                    "source_ref": str(baseline_id),
                    "graph_json": graph_json,
                    "stats": stats,
                }

    candidates: list[tuple[str, Path]] = [("shared_volume_current", _current_graph_path(project_id))]
    for path in extra_graph_paths or []:
        candidates.append(("explicit_path", Path(path)))

    for source_kind, path in candidates:
        if not path.exists():
            continue
        graph_json = _read_json_file(path)
        stats = graph_payload_stats(graph_json)
        if stats["nodes"] > 0:
            return {
                "source_kind": source_kind,
                "source_path": str(path),
                "source_ref": "",
                "graph_json": graph_json,
                "stats": stats,
            }
    return None


def create_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    commit_sha: str,
    snapshot_kind: str,
    snapshot_id: str | None = None,
    parent_snapshot_id: str = "",
    graph_json: dict[str, Any] | None = None,
    file_inventory: list[dict[str, Any]] | None = None,
    drift_ledger: list[dict[str, Any]] | None = None,
    status: str = SNAPSHOT_STATUS_CANDIDATE,
    created_by: str = "",
    notes: str = "",
) -> dict[str, Any]:
    ensure_schema(conn)
    if status not in ALLOWED_SNAPSHOT_STATUSES:
        raise ValueError(f"invalid graph snapshot status: {status}")
    sid = snapshot_id or snapshot_id_for(snapshot_kind, commit_sha)
    shas = write_companion_files(
        project_id,
        sid,
        graph_json=graph_json,
        file_inventory=file_inventory,
        drift_ledger=drift_ledger,
    )
    now = utc_now()
    conn.execute(
        """
        INSERT INTO graph_snapshots
          (project_id, snapshot_id, commit_sha, parent_snapshot_id, snapshot_kind,
           graph_sha256, inventory_sha256, drift_sha256, status, created_at,
           created_by, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            sid,
            commit_sha,
            parent_snapshot_id,
            snapshot_kind,
            shas["graph_sha256"],
            shas["inventory_sha256"],
            shas["drift_sha256"],
            status,
            now,
            created_by,
            notes,
        ),
    )
    return {
        "project_id": project_id,
        "snapshot_id": sid,
        "commit_sha": commit_sha,
        "snapshot_kind": snapshot_kind,
        "status": status,
        "path": shas["path"],
        "graph_sha256": shas["graph_sha256"],
        "inventory_sha256": shas["inventory_sha256"],
        "drift_sha256": shas["drift_sha256"],
    }


def index_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    nodes: Iterable[dict[str, Any]] | None = None,
    edges: Iterable[dict[str, Any]] | None = None,
) -> dict[str, int]:
    ensure_schema(conn)
    node_count = 0
    for node in nodes or []:
        node_id = str(node.get("id") or node.get("node_id") or "")
        if not node_id:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO graph_nodes_index
              (project_id, snapshot_id, node_id, layer, title, kind,
               primary_files_json, secondary_files_json, test_files_json, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                snapshot_id,
                node_id,
                str(node.get("layer") or ""),
                str(node.get("title") or ""),
                str(node.get("kind") or node.get("metadata", {}).get("kind") or ""),
                _json_list(node.get("primary") or node.get("primary_files")),
                _json_list(node.get("secondary") or node.get("secondary_files")),
                _json_list(node.get("test") or node.get("test_files")),
                _json(node.get("metadata") or {}),
            ),
        )
        node_count += 1

    edge_count = 0
    for edge in edges or []:
        src = str(edge.get("src") or edge.get("source") or "")
        dst = str(edge.get("dst") or edge.get("target") or "")
        edge_type = str(edge.get("edge_type") or edge.get("type") or "depends_on")
        direction = str(edge.get("direction") or "dependency")
        if not src or not dst:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO graph_edges_index
              (project_id, snapshot_id, src, dst, edge_type, direction, evidence_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                snapshot_id,
                src,
                dst,
                edge_type,
                direction,
                _json(edge.get("evidence") or edge.get("evidence_json") or {}),
            ),
        )
        edge_count += 1
    return {"nodes": node_count, "edges": edge_count}


def activate_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    expected_old_snapshot_id: str | None = None,
    ref_name: str = "active",
    actor: str = "activate_hook",
    auto_rebuild_projection: bool = True,
) -> dict[str, Any]:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM graph_snapshots WHERE project_id = ? AND snapshot_id = ?",
        (project_id, snapshot_id),
    ).fetchone()
    if not row:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    snapshot = dict(row)
    old = conn.execute(
        "SELECT snapshot_id FROM graph_snapshot_refs WHERE project_id = ? AND ref_name = ?",
        (project_id, ref_name),
    ).fetchone()
    old_id = old["snapshot_id"] if old else ""
    if expected_old_snapshot_id is not None and old_id != expected_old_snapshot_id:
        raise GraphSnapshotConflictError(
            f"active snapshot changed: expected {expected_old_snapshot_id!r}, got {old_id!r}"
        )

    now = utc_now()
    conn.execute(
        """
        INSERT INTO graph_snapshot_refs(project_id, ref_name, snapshot_id, commit_sha, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(project_id, ref_name) DO UPDATE SET
          snapshot_id = excluded.snapshot_id,
          commit_sha = excluded.commit_sha,
          updated_at = excluded.updated_at
        """,
        (project_id, ref_name, snapshot_id, snapshot["commit_sha"], now),
    )
    conn.execute(
        "UPDATE graph_snapshots SET status = ? WHERE project_id = ? AND snapshot_id = ?",
        (SNAPSHOT_STATUS_ACTIVE, project_id, snapshot_id),
    )
    if old_id and old_id != snapshot_id:
        conn.execute(
            "UPDATE graph_snapshots SET status = ? WHERE project_id = ? AND snapshot_id = ?",
            (SNAPSHOT_STATUS_SUPERSEDED, project_id, old_id),
        )
    result: dict[str, Any] = {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "commit_sha": snapshot["commit_sha"],
        "previous_snapshot_id": old_id,
        "ref_name": ref_name,
    }

    # MF-2026-05-10-012: dashboard derives feature counters from
    # graph_semantic_projections (per-snapshot cache), not from raw events.
    # Reconcile and admin recovery can both leave a freshly created snapshot
    # without a projection, which manifests as "Node semantic 0/0" the moment
    # it becomes active. Auto-rebuild on activate is idempotent — if the
    # target snapshot already has a projection, skip. If projection rebuild
    # fails (advisory only), we still report the activation as successful.
    projection_status = "skipped"
    if auto_rebuild_projection and ref_name == "active":
        try:
            from . import graph_events  # local import to avoid module cycle

            existing = graph_events.get_semantic_projection(conn, project_id, snapshot_id)
            if not existing or existing.get("status") in (None, "", "missing"):
                graph_events.materialize_events(conn, project_id, snapshot_id, actor=actor)
                graph_events.build_semantic_projection(conn, project_id, snapshot_id, actor=actor)
                projection_status = "rebuilt"
            else:
                projection_status = "already_present"
        except Exception as exc:  # noqa: BLE001 - advisory; activation already committed
            projection_status = f"rebuild_failed: {exc}"
    result["projection_status"] = projection_status
    # MF 2026-05-11: snapshot activation is an in-process hook (no HTTP),
    # so _emit_dashboard_changed never fires for it. Publish here so the
    # dashboard's SSE subscribers refetch when a new snapshot becomes
    # active (reconcile / pending-scope materialize, etc.).
    try:
        from . import event_bus
        event_bus.publish("snapshot.activated", {
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "previous_snapshot_id": old_id,
            "commit_sha": snapshot["commit_sha"],
            "ref_name": ref_name,
            "projection_status": projection_status,
            "source": "activate_graph_snapshot",
        })
        event_bus.publish("dashboard.changed", {
            "project_id": project_id,
            "path": "/internal/snapshot/activate",
            "method": "WORKER",
            "source": "activate_graph_snapshot",
        })
    except Exception:  # noqa: BLE001 - advisory
        pass
    return result


def finalize_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    target_commit_sha: str = "",
    expected_old_snapshot_id: str | None = None,
    ref_name: str = "active",
    actor: str = "observer",
    materialize_pending: bool = True,
    covered_commit_shas: Iterable[str] | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Activate a candidate graph snapshot and settle matching pending scope rows.

    This is the explicit signoff bridge from a state-only reconcile candidate to
    the active graph ref. It performs the same compare-and-swap check as
    ``activate_graph_snapshot`` and only marks pending rows for the exact
    snapshot commit as materialized.
    """
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM graph_snapshots WHERE project_id = ? AND snapshot_id = ?",
        (project_id, snapshot_id),
    ).fetchone()
    if not row:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    snapshot = dict(row)
    status = str(snapshot.get("status") or "")
    if status not in {
        SNAPSHOT_STATUS_CANDIDATE,
        SNAPSHOT_STATUS_FINALIZING,
        SNAPSHOT_STATUS_ACTIVE,
    }:
        raise ValueError(f"cannot finalize graph snapshot in status {status!r}")
    commit_sha = str(snapshot.get("commit_sha") or "")
    if target_commit_sha and commit_sha != target_commit_sha:
        raise ValueError(
            f"snapshot commit mismatch: expected {target_commit_sha}, got {commit_sha}"
        )

    activation = activate_graph_snapshot(
        conn,
        project_id,
        snapshot_id,
        expected_old_snapshot_id=expected_old_snapshot_id,
        ref_name=ref_name,
    )
    materialized_count = 0
    if materialize_pending:
        commit_targets = sorted({
            str(item or "").strip()
            for item in (covered_commit_shas or [commit_sha])
            if str(item or "").strip()
        })
        if not commit_targets:
            commit_targets = [commit_sha]
        pending_evidence = {
            "source": "graph_snapshot_finalizer",
            "actor": actor,
            "snapshot_id": snapshot_id,
            "ref_name": ref_name,
            "covered_commit_shas": commit_targets,
            **(evidence or {}),
        }
        placeholders = ",".join("?" for _ in commit_targets)
        cur = conn.execute(
            f"""
            UPDATE pending_scope_reconcile
            SET status = ?,
                snapshot_id = ?,
                evidence_json = ?
            WHERE project_id = ?
              AND commit_sha IN ({placeholders})
              AND status IN (?, ?, ?)
            """,
            (
                PENDING_STATUS_MATERIALIZED,
                snapshot_id,
                _json(pending_evidence),
                project_id,
                *commit_targets,
                PENDING_STATUS_QUEUED,
                PENDING_STATUS_RUNNING,
                PENDING_STATUS_FAILED,
            ),
        )
        materialized_count = int(cur.rowcount or 0)
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "commit_sha": commit_sha,
        "activation": activation,
        "pending_materialized_count": materialized_count,
        "ref_name": ref_name,
    }


def get_active_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    ref_name: str = "active",
) -> dict[str, Any] | None:
    ensure_schema(conn)
    row = conn.execute(
        """
        SELECT s.*
        FROM graph_snapshot_refs r
        JOIN graph_snapshots s
          ON s.project_id = r.project_id AND s.snapshot_id = r.snapshot_id
        WHERE r.project_id = ? AND r.ref_name = ?
        """,
        (project_id, ref_name),
    ).fetchone()
    return dict(row) if row else None


def get_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> dict[str, Any] | None:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM graph_snapshots WHERE project_id = ? AND snapshot_id = ?",
        (project_id, snapshot_id),
    ).fetchone()
    return dict(row) if row else None


def list_graph_snapshots(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    statuses: Iterable[str] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id]
    sql = "SELECT * FROM graph_snapshots WHERE project_id = ?"
    status_values = [str(s) for s in statuses or [] if s]
    if status_values:
        placeholders = ",".join("?" for _ in status_values)
        sql += f" AND status IN ({placeholders})"
        params.extend(status_values)
    sql += " ORDER BY created_at DESC, snapshot_id DESC LIMIT ?"
    params.append(max(1, min(int(limit or 50), 500)))
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def list_graph_snapshots_for_commit(
    conn: sqlite3.Connection,
    project_id: str,
    commit_sha: str,
    *,
    statuses: Iterable[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id, commit_sha]
    sql = "SELECT * FROM graph_snapshots WHERE project_id = ? AND commit_sha = ?"
    status_values = [str(s) for s in statuses or [] if s]
    if status_values:
        placeholders = ",".join("?" for _ in status_values)
        sql += f" AND status IN ({placeholders})"
        params.extend(status_values)
    sql += """
        ORDER BY
          CASE status
            WHEN 'active' THEN 0
            WHEN 'superseded' THEN 1
            WHEN 'candidate' THEN 2
            WHEN 'finalizing' THEN 3
            ELSE 4
          END,
          created_at DESC,
          snapshot_id DESC
        LIMIT ?
    """
    params.append(max(1, min(int(limit or 20), 100)))
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def get_graph_snapshot_for_commit(
    conn: sqlite3.Connection,
    project_id: str,
    commit_sha: str,
) -> dict[str, Any] | None:
    rows = list_graph_snapshots_for_commit(
        conn,
        project_id,
        commit_sha,
        statuses=[
            SNAPSHOT_STATUS_ACTIVE,
            SNAPSHOT_STATUS_SUPERSEDED,
            SNAPSHOT_STATUS_CANDIDATE,
            SNAPSHOT_STATUS_FINALIZING,
        ],
        limit=1,
    )
    return rows[0] if rows else None


def list_graph_snapshot_nodes(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    limit: int = 200,
    offset: int = 0,
    layer: str = "",
    kind: str = "",
    include_semantic: bool = True,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id, snapshot_id]
    semantic_join = include_semantic and _table_exists(conn, "graph_semantic_nodes")
    semantic_job_join = include_semantic and _table_exists(conn, "graph_semantic_jobs")
    select_columns = """
        n.node_id, n.layer, n.title, n.kind, n.primary_files_json,
        n.secondary_files_json, n.test_files_json, n.metadata_json
    """
    joins = ""
    if semantic_join:
        select_columns += """,
        s.status AS semantic_status,
        s.feature_hash AS semantic_feature_hash,
        s.file_hashes_json AS semantic_file_hashes_json,
        s.semantic_json AS semantic_json,
        s.feedback_round AS semantic_feedback_round,
        s.batch_index AS semantic_batch_index,
        s.updated_at AS semantic_updated_at
        """
        joins += """
        LEFT JOIN graph_semantic_nodes s
          ON s.project_id = n.project_id
         AND s.snapshot_id = n.snapshot_id
         AND s.node_id = n.node_id
        """
    if semantic_job_join:
        select_columns += """,
        j.status AS semantic_job_status,
        j.feature_hash AS semantic_job_feature_hash,
        j.attempt_count AS semantic_job_attempt_count,
        j.worker_id AS semantic_job_worker_id,
        j.claim_id AS semantic_job_claim_id,
        j.claimed_at AS semantic_job_claimed_at,
        j.lease_expires_at AS semantic_job_lease_expires_at,
        j.claimed_by AS semantic_job_claimed_by,
        j.last_error AS semantic_job_last_error,
        j.updated_at AS semantic_job_updated_at
        """
        joins += """
        LEFT JOIN graph_semantic_jobs j
          ON j.project_id = n.project_id
         AND j.snapshot_id = n.snapshot_id
         AND j.node_id = n.node_id
        """
    sql = f"""
        SELECT {select_columns}
        FROM graph_nodes_index n
        {joins}
        WHERE n.project_id = ? AND n.snapshot_id = ?
    """
    if layer:
        sql += " AND n.layer = ?"
        params.append(layer)
    if kind:
        sql += " AND n.kind = ?"
        params.append(kind)
    sql += " ORDER BY n.node_id LIMIT ? OFFSET ?"
    params.extend([max(1, min(int(limit or 200), 1000)), max(0, int(offset or 0))])
    rows = conn.execute(sql, params).fetchall()
    nodes: list[dict[str, Any]] = []
    for row in rows:
        node = {
            "node_id": row["node_id"],
            "layer": row["layer"],
            "title": row["title"],
            "kind": row["kind"],
            "primary_files": _decode_json(row["primary_files_json"], []),
            "secondary_files": _decode_json(row["secondary_files_json"], []),
            "test_files": _decode_json(row["test_files_json"], []),
            "metadata": _decode_json(row["metadata_json"], {}),
        }
        if include_semantic:
            node["semantic"] = _semantic_overlay_from_node_row(row)
        nodes.append(node)
    return nodes


def list_graph_snapshot_edges(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    limit: int = 500,
    offset: int = 0,
    edge_type: str = "",
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id, snapshot_id]
    sql = """
        SELECT src, dst, edge_type, direction, evidence_json
        FROM graph_edges_index
        WHERE project_id = ? AND snapshot_id = ?
    """
    if edge_type:
        sql += " AND edge_type = ?"
        params.append(edge_type)
    sql += " ORDER BY src, dst, edge_type LIMIT ? OFFSET ?"
    params.extend([max(1, min(int(limit or 500), 2000)), max(0, int(offset or 0))])
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "src": row["src"],
            "dst": row["dst"],
            "edge_type": row["edge_type"],
            "direction": row["direction"],
            "evidence": _decode_json(row["evidence_json"], {}),
        }
        for row in rows
    ]


def summarize_file_inventory_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Compact dashboard summary for snapshot file inventory rows."""
    row_list = list(rows)
    by_kind: dict[str, int] = {}
    by_scan_status: dict[str, int] = {}
    by_graph_status: dict[str, int] = {}
    by_decision: dict[str, int] = {}
    pending: list[str] = []
    for row in row_list:
        kind = str(row.get("file_kind") or "")
        scan = str(row.get("scan_status") or "")
        graph = str(row.get("graph_status") or "")
        decision = str(row.get("decision") or "")
        if kind:
            by_kind[kind] = by_kind.get(kind, 0) + 1
        if scan:
            by_scan_status[scan] = by_scan_status.get(scan, 0) + 1
        if graph:
            by_graph_status[graph] = by_graph_status.get(graph, 0) + 1
        if decision:
            by_decision[decision] = by_decision.get(decision, 0) + 1
        if scan in {"orphan", "pending_decision", "error"} or graph in {"unmapped", "error"}:
            path = str(row.get("path") or "")
            if path:
                pending.append(path)
    return {
        "total": len(row_list),
        "by_kind": dict(sorted(by_kind.items())),
        "by_scan_status": dict(sorted(by_scan_status.items())),
        "by_graph_status": dict(sorted(by_graph_status.items())),
        "by_decision": dict(sorted(by_decision.items())),
        "pending_count": len(pending),
        "pending_sample": pending[:25],
    }


def list_graph_snapshot_files(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    limit: int = 200,
    offset: int = 0,
    file_kind: str = "",
    scan_status: str = "",
    graph_status: str = "",
    decision: str = "",
    path_contains: str = "",
    sort: str = "",
) -> dict[str, Any]:
    """List file inventory rows stored with a snapshot companion artifact."""
    ensure_schema(conn)
    snapshot = get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    raw = _read_json_artifact(snapshot_companion_dir(project_id, snapshot_id) / "file_inventory.json", [])
    rows = [dict(row) for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []

    def _matches(row: dict[str, Any]) -> bool:
        if file_kind and str(row.get("file_kind") or "") != file_kind:
            return False
        if scan_status and str(row.get("scan_status") or "") != scan_status:
            return False
        if graph_status and str(row.get("graph_status") or "") != graph_status:
            return False
        if decision and str(row.get("decision") or "") != decision:
            return False
        if path_contains and path_contains not in str(row.get("path") or ""):
            return False
        return True

    filtered = [row for row in rows if _matches(row)]
    normalized_sort = str(sort or "").strip().lower().replace("-", "_")
    if normalized_sort:
        if normalized_sort in {"path", "path_asc"}:
            filtered = sorted(filtered, key=lambda row: str(row.get("path") or ""))
        elif normalized_sort == "size_desc":
            filtered = sorted(
                filtered,
                key=lambda row: (-int(row.get("size_bytes") or 0), str(row.get("path") or "")),
            )
        elif normalized_sort == "size_asc":
            filtered = sorted(
                filtered,
                key=lambda row: (int(row.get("size_bytes") or 0), str(row.get("path") or "")),
            )
        else:
            raise ValueError(f"unsupported file inventory sort: {sort}")
    start = max(0, int(offset or 0))
    end = start + max(1, min(int(limit or 200), 1000))
    return {
        "snapshot": snapshot,
        "summary": summarize_file_inventory_rows(rows),
        "total_count": len(rows),
        "filtered_count": len(filtered),
        "sort": normalized_sort,
        "files": filtered[start:end],
    }


def _count_rows(
    conn: sqlite3.Connection,
    table: str,
    project_id: str,
    snapshot_id: str,
) -> int:
    if not _table_exists(conn, table):
        return 0
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM {table} WHERE project_id = ? AND snapshot_id = ?",
        (project_id, snapshot_id),
    ).fetchone()
    return int(row["count"] if row else 0)


def _group_counts(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    project_id: str,
    snapshot_id: str,
) -> dict[str, int]:
    if not _table_exists(conn, table):
        return {}
    rows = conn.execute(
        f"""
        SELECT {column} AS key, COUNT(*) AS count
        FROM {table}
        WHERE project_id = ? AND snapshot_id = ?
        GROUP BY {column}
        ORDER BY {column}
        """,
        (project_id, snapshot_id),
    ).fetchall()
    return {str(row["key"] or ""): int(row["count"]) for row in rows if str(row["key"] or "")}


def _snapshot_notes(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not snapshot:
        return {}
    notes = _decode_json(snapshot.get("notes"), {})
    return notes if isinstance(notes, dict) else {}


def _latest_global_review_from_notes(notes: dict[str, Any]) -> dict[str, Any]:
    review_meta = notes.get("global_semantic_review")
    if not isinstance(review_meta, dict):
        return {}
    path = str(review_meta.get("latest_full_review_path") or "").strip()
    if not path:
        return {}
    payload = _read_json_artifact(Path(path), {})
    return payload if isinstance(payload, dict) else {}


def _as_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _node_metadata(node: dict[str, Any]) -> dict[str, Any]:
    metadata = node.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, Iterable):
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


def _is_governed_feature_node(node: dict[str, Any]) -> bool:
    if str(node.get("layer") or "").upper() != "L7":
        return False
    metadata = _node_metadata(node)
    if metadata.get("exclude_as_feature") is True:
        return False
    file_role = str(metadata.get("file_role") or node.get("kind") or "").strip().lower()
    if file_role == "package_marker":
        return False
    return True


def _feature_coverage_picture(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    raw_features = [
        node for node in nodes
        if str(node.get("layer") or "").upper() == "L7"
    ]
    governed_features = [node for node in raw_features if _is_governed_feature_node(node)]
    doc_bound = sum(1 for node in governed_features if _string_list(node.get("secondary_files")))
    test_bound = sum(1 for node in governed_features if _string_list(node.get("test_files")))
    config_bound = sum(
        1 for node in governed_features
        if _string_list(_node_metadata(node).get("config_files"))
    )
    return {
        "raw_feature_count": len(raw_features),
        "governed_feature_count": len(governed_features),
        "excluded_feature_count": max(0, len(raw_features) - len(governed_features)),
        "doc_bound_count": doc_bound,
        "doc_coverage_ratio": _ratio(doc_bound, len(governed_features)),
        "test_bound_count": test_bound,
        "test_coverage_ratio": _ratio(test_bound, len(governed_features)),
        "config_bound_count": config_bound,
        "config_coverage_ratio": _ratio(config_bound, len(governed_features)),
    }


def _l4_asset_picture(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    l4_nodes = [
        node for node in nodes
        if str(node.get("layer") or "").upper() == "L4"
    ]
    by_kind: dict[str, int] = {}
    by_file_role: dict[str, int] = {}
    aggregate_count = 0
    no_primary_count = 0
    for node in l4_nodes:
        metadata = _node_metadata(node)
        kind = str(node.get("kind") or metadata.get("kind") or "asset").strip() or "asset"
        role = str(metadata.get("file_role") or "asset").strip() or "asset"
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_file_role[role] = by_file_role.get(role, 0) + 1
        if metadata.get("aggregate_asset") is True:
            aggregate_count += 1
        if not _string_list(node.get("primary_files")):
            no_primary_count += 1
    return {
        "score_version": "l4_asset_contract_v1_role_aware",
        "score": 100.0,
        "asset_count": len(l4_nodes),
        "aggregate_asset_count": aggregate_count,
        "no_primary_asset_count": no_primary_count,
        "by_kind": dict(sorted(by_kind.items())),
        "by_file_role": dict(sorted(by_file_role.items())),
        "policy": "L4 nodes are state/contract/asset nodes; direct files may be intentionally empty and are not scored as L7 feature coverage gaps.",
    }


def _structure_health_picture(
    *,
    nodes: list[dict[str, Any]],
    counts: dict[str, Any],
    file_summary: dict[str, Any],
    graph_corrections: dict[str, Any],
) -> dict[str, Any]:
    coverage = _feature_coverage_picture(nodes)
    feature_count = int(coverage["governed_feature_count"])
    missing_docs = max(0, feature_count - int(coverage["doc_bound_count"]))
    missing_tests = max(0, feature_count - int(coverage["test_bound_count"]))
    file_total = max(1, int(counts.get("files") or 0))
    orphan_files = int(counts.get("orphan_files") or 0)
    pending_files = int(counts.get("pending_decision_files") or 0)
    cleanup_candidates = int(counts.get("cleanup_candidates") or 0)
    proposed_patches = int(graph_corrections.get("proposed_count") or 0)
    high_risk_patches = int(graph_corrections.get("high_risk_proposed_count") or 0)

    coverage_penalty = 0.0
    if feature_count:
        coverage_penalty = ((missing_docs * 6.0) + (missing_tests * 8.0)) / feature_count
    file_penalty = min(
        12.0,
        ((orphan_files * 4.0) + (pending_files * 0.5) + (cleanup_candidates * 0.5)) / file_total * 100.0,
    )
    correction_penalty = min(8.0, proposed_patches * 1.5 + high_risk_patches * 3.0)
    score = round(max(0.0, 100.0 - coverage_penalty - file_penalty - correction_penalty), 2)
    return {
        "score_version": "structure_health_v1_algorithmic_coverage_inventory",
        "score": score,
        "status": "current",
        **coverage,
        "file_hygiene": {
            "total_files": counts.get("files", 0),
            "orphan_files": orphan_files,
            "pending_decision_files": pending_files,
            "cleanup_candidates": cleanup_candidates,
            "summary": file_summary,
        },
        "graph_correction_patches": {
            "proposed_count": proposed_patches,
            "high_risk_proposed_count": high_risk_patches,
        },
        "penalties": {
            "coverage": round(coverage_penalty, 2),
            "file_hygiene": round(file_penalty, 2),
            "graph_corrections": round(correction_penalty, 2),
        },
        "l4_asset_health": _l4_asset_picture(nodes),
    }


def _latest_projection_health(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> dict[str, Any]:
    if not _table_exists(conn, "graph_semantic_projections"):
        return {}
    try:
        row = conn.execute(
            """
            SELECT projection_id, health_json, created_at
            FROM graph_semantic_projections
            WHERE project_id = ? AND snapshot_id = ?
            ORDER BY event_watermark DESC, created_at DESC
            LIMIT 1
            """,
            (project_id, snapshot_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    health = _decode_json(row["health_json"], {})
    if not isinstance(health, dict):
        health = {}
    return {
        **health,
        "projection_id": row["projection_id"],
        "projection_created_at": row["created_at"],
    }


def _semantic_health_picture(
    *,
    projection_health: dict[str, Any],
    legacy_health: dict[str, Any],
    review_meta: dict[str, Any],
) -> dict[str, Any]:
    if projection_health:
        score = _as_float(projection_health.get("project_health_score"), None)
        return {
            "score_version": projection_health.get("score_version") or "semantic_projection",
            "score": score,
            "status": "current",
            "source": "semantic_projection",
            "projection_id": projection_health.get("projection_id", ""),
            "feature_count": projection_health.get("feature_count"),
            "semantic_current_count": projection_health.get("semantic_current_count"),
            "semantic_missing_count": projection_health.get("semantic_missing_count"),
            "semantic_stale_count": projection_health.get("semantic_stale_count"),
            "semantic_unverified_hash_count": projection_health.get("semantic_unverified_hash_count"),
            "semantic_current_ratio": projection_health.get("semantic_current_ratio"),
            "semantic_trusted_count": projection_health.get("semantic_trusted_count"),
            "semantic_trusted_ratio": projection_health.get("semantic_trusted_ratio"),
            "semantic_review_debt_count": projection_health.get("semantic_review_debt_count"),
            "semantic_review_debt_ratio": projection_health.get("semantic_review_debt_ratio"),
            "doc_coverage_ratio": projection_health.get("doc_coverage_ratio"),
            "test_coverage_ratio": projection_health.get("test_coverage_ratio"),
            "semantic_debt_penalty": projection_health.get("semantic_debt_penalty"),
            "binding_context_penalty": projection_health.get("binding_context_penalty"),
            "open_issue_penalty": projection_health.get("open_issue_penalty"),
            "semantic_open_issue_count": projection_health.get("semantic_open_issue_count"),
            "low_health_count": projection_health.get("low_health_count"),
            "edge_semantic_eligible_count": projection_health.get("edge_semantic_eligible_count"),
            "edge_semantic_requested_count": projection_health.get("edge_semantic_requested_count"),
            "edge_semantic_current_count": projection_health.get("edge_semantic_current_count"),
            "edge_semantic_rule_count": projection_health.get("edge_semantic_rule_count"),
            "edge_semantic_missing_count": projection_health.get("edge_semantic_missing_count"),
            "edge_semantic_unqueued_count": projection_health.get("edge_semantic_unqueued_count"),
            "edge_semantic_needs_ai_count": projection_health.get("edge_semantic_needs_ai_count"),
            "edge_semantic_payload_current_count": projection_health.get("edge_semantic_payload_current_count"),
            "edge_semantic_coverage_ratio": projection_health.get("edge_semantic_coverage_ratio"),
            "edge_semantic_payload_coverage_ratio": projection_health.get("edge_semantic_payload_coverage_ratio"),
        }
    coverage = legacy_health.get("semantic_coverage_ratio")
    if coverage is None:
        coverage = review_meta.get("latest_full_semantic_coverage_ratio")
    if coverage is not None:
        return {
            "score_version": "semantic_metadata_fallback_v1",
            "score": _as_float(legacy_health.get("governance_observability_score"), None),
            "status": "metadata_only",
            "source": "snapshot_notes",
            "semantic_current_ratio": coverage,
            "semantic_coverage_ratio": coverage,
        }
    return {
        "score_version": "semantic_health_v1",
        "score": None,
        "status": "pending",
        "source": "none",
    }


def _project_insight_health_picture(
    *,
    latest_review: dict[str, Any],
    review_meta: dict[str, Any],
) -> dict[str, Any]:
    health = latest_review.get("health_picture") if isinstance(latest_review, dict) else {}
    if isinstance(health, dict) and health:
        file_hygiene = health.get("file_hygiene") if isinstance(health.get("file_hygiene"), dict) else {}
        return {
            "score_version": "project_insight_health_v1_global_review",
            "score": _as_float(health.get("project_health_score"), None),
            "status": "reviewed",
            "source": "global_semantic_review",
            "latest_run_id": review_meta.get("latest_full_run_id", ""),
            "latest_status": review_meta.get("latest_full_status", ""),
            "low_health_count": health.get("low_health_count"),
            "issue_counts": health.get("project_health_issue_counts", {}),
            "file_hygiene_score": health.get("file_hygiene_score"),
            "file_hygiene": {
                "available": bool(file_hygiene.get("available")),
                "run_id": file_hygiene.get("run_id", ""),
                "total_files": file_hygiene.get("total_files"),
                "review_required_count": file_hygiene.get("review_required_count"),
                "orphan_count": file_hygiene.get("orphan_count"),
                "pending_decision_count": file_hygiene.get("pending_decision_count"),
                "error_count": file_hygiene.get("error_count"),
                "cleanup_candidate_count": file_hygiene.get("cleanup_candidate_count"),
                "cleanup_candidate_bytes": file_hygiene.get("cleanup_candidate_bytes"),
                "cleanup_candidate_mb": file_hygiene.get("cleanup_candidate_mb"),
                "by_kind": file_hygiene.get("by_kind", {}),
                "by_scan_status": file_hygiene.get("by_scan_status", {}),
                "by_graph_status": file_hygiene.get("by_graph_status", {}),
                "review_required_sample": file_hygiene.get("review_required_sample", []),
                "cleanup_candidate_sample": file_hygiene.get("cleanup_candidate_sample", []),
            },
        }
    if review_meta:
        return {
            "score_version": "project_insight_health_v1_global_review",
            "score": None,
            "status": "metadata_only",
            "source": "snapshot_notes",
            "latest_run_id": review_meta.get("latest_full_run_id", ""),
            "latest_status": review_meta.get("latest_full_status", ""),
        }
    return {
        "score_version": "project_insight_health_v1_global_review",
        "score": None,
        "status": "pending",
        "source": "none",
    }


def _legacy_health_from_review(
    latest_review: dict[str, Any],
    review_meta: dict[str, Any],
) -> dict[str, Any]:
    health = latest_review.get("health_picture") if isinstance(latest_review, dict) else {}
    if not isinstance(health, dict):
        health = {}
    return {
        "project_health_score": health.get("project_health_score"),
        "raw_project_health_score": health.get("raw_project_health_score"),
        "file_hygiene_score": health.get("file_hygiene_score"),
        "artifact_binding_score": health.get("artifact_binding_score"),
        "governance_observability_score": health.get("governance_observability_score"),
        "doc_coverage_ratio": health.get("doc_coverage_ratio"),
        "test_coverage_ratio": health.get("test_coverage_ratio"),
        "semantic_coverage_ratio": (
            health.get("semantic_coverage_ratio")
            if health.get("semantic_coverage_ratio") is not None
            else review_meta.get("latest_full_semantic_coverage_ratio")
        ),
    }


def _health_from_snapshot_notes(notes: dict[str, Any]) -> dict[str, Any]:
    latest_review = _latest_global_review_from_notes(notes)
    review_meta = notes.get("global_semantic_review") if isinstance(notes.get("global_semantic_review"), dict) else {}
    return _legacy_health_from_review(latest_review, review_meta)


def _dashboard_health(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    nodes: list[dict[str, Any]],
    counts: dict[str, Any],
    file_summary: dict[str, Any],
    graph_corrections: dict[str, Any],
    notes: dict[str, Any],
) -> dict[str, Any]:
    latest_review = _latest_global_review_from_notes(notes)
    review_meta = notes.get("global_semantic_review") if isinstance(notes.get("global_semantic_review"), dict) else {}
    legacy = _legacy_health_from_review(latest_review, review_meta)
    structure = _structure_health_picture(
        nodes=nodes,
        counts=counts,
        file_summary=file_summary,
        graph_corrections=graph_corrections,
    )
    projection = _latest_projection_health(conn, project_id, snapshot_id)
    semantic = _semantic_health_picture(
        projection_health=projection,
        legacy_health=legacy,
        review_meta=review_meta,
    )
    insight = _project_insight_health_picture(
        latest_review=latest_review,
        review_meta=review_meta,
    )
    legacy_score = (
        legacy.get("project_health_score")
        if legacy.get("project_health_score") is not None
        else semantic.get("score")
        if semantic.get("score") is not None
        else structure.get("score")
    )
    return {
        **legacy,
        "project_health_score": legacy_score,
        "structure_health_score": structure.get("score"),
        "semantic_health_score": semantic.get("score"),
        "project_insight_health_score": insight.get("score"),
        "structure_health": structure,
        "semantic_health": semantic,
        "project_insight_health": insight,
    }


def _semantic_counts(conn: sqlite3.Connection, project_id: str, snapshot_id: str) -> dict[str, Any]:
    return {
        "nodes_by_status": _group_counts(conn, "graph_semantic_nodes", "status", project_id, snapshot_id),
        "jobs_by_status": _group_counts(conn, "graph_semantic_jobs", "status", project_id, snapshot_id),
        "semantic_node_count": _count_rows(conn, "graph_semantic_nodes", project_id, snapshot_id),
        "semantic_job_count": _count_rows(conn, "graph_semantic_jobs", project_id, snapshot_id),
    }


def summarize_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> dict[str, Any]:
    """Return a compact dashboard-safe summary for one graph snapshot."""
    ensure_schema(conn)
    snapshot = get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")

    nodes_by_layer = _group_counts(conn, "graph_nodes_index", "layer", project_id, snapshot_id)
    edges_by_type = _group_counts(conn, "graph_edges_index", "edge_type", project_id, snapshot_id)
    semantic = _semantic_counts(conn, project_id, snapshot_id)
    try:
        from .graph_correction_patches import correction_patch_summary

        graph_corrections = correction_patch_summary(conn, project_id)
    except Exception:
        graph_corrections = {
            "total": 0,
            "by_status": {},
            "by_type": {},
            "by_risk": {},
            "last_apply_status": {},
            "proposed_count": 0,
            "accepted_count": 0,
            "rejected_count": 0,
            "stale_count": 0,
            "replayable_count": 0,
            "high_risk_proposed_count": 0,
        }
    try:
        files = list_graph_snapshot_files(conn, project_id, snapshot_id, limit=1)
        file_summary = files["summary"]
        file_total = int(files["total_count"])
    except Exception:
        file_summary = {}
        file_total = 0
    try:
        summary_nodes = list_graph_snapshot_nodes(
            conn,
            project_id,
            snapshot_id,
            limit=100000,
            include_semantic=False,
        )
    except Exception:
        summary_nodes = []

    notes = _snapshot_notes(snapshot)
    semantic_state = {}
    semantic_enrichment = notes.get("semantic_enrichment")
    if isinstance(semantic_enrichment, dict):
        semantic_state = semantic_enrichment.get("semantic_graph_state") or {}
        if not isinstance(semantic_state, dict):
            semantic_state = {}

    by_scan = file_summary.get("by_scan_status", {}) if isinstance(file_summary, dict) else {}
    by_kind = file_summary.get("by_kind", {}) if isinstance(file_summary, dict) else {}
    counts = {
        "nodes": _count_rows(conn, "graph_nodes_index", project_id, snapshot_id),
        "nodes_by_layer": nodes_by_layer,
        "edges": _count_rows(conn, "graph_edges_index", project_id, snapshot_id),
        "edges_by_type": edges_by_type,
        "features": int(nodes_by_layer.get("L7", 0)),
        "files": file_total,
        "orphan_files": int(by_scan.get("orphan", 0)),
        "pending_decision_files": int(by_scan.get("pending_decision", 0)),
        "cleanup_candidates": int(by_kind.get("generated", 0)),
        "ai_review_feedback": int(semantic_state.get("open_issue_count") or 0),
    }
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "commit_sha": snapshot["commit_sha"],
        "snapshot_kind": snapshot["snapshot_kind"],
        "snapshot_status": snapshot["status"],
        "created_at": snapshot["created_at"],
        "created_by": snapshot.get("created_by", ""),
        "graph_sha256": snapshot.get("graph_sha256", ""),
        "inventory_sha256": snapshot.get("inventory_sha256", ""),
        "drift_sha256": snapshot.get("drift_sha256", ""),
        "counts": counts,
        "health": _dashboard_health(
            conn,
            project_id,
            snapshot_id,
            nodes=summary_nodes,
            counts=counts,
            file_summary=file_summary,
            graph_corrections=graph_corrections,
            notes=notes,
        ),
        "semantic": semantic,
        "graph_correction_patches": graph_corrections,
        "file_inventory_summary": file_summary,
    }


def _backlog_counts_for_commits(
    conn: sqlite3.Connection,
    commits: Iterable[str],
) -> dict[str, dict[str, int]]:
    selected = [str(commit or "").strip() for commit in commits if str(commit or "").strip()]
    if not selected or not _table_exists(conn, "backlog_bugs"):
        return {}
    placeholders = ",".join("?" for _ in selected)
    try:
        rows = conn.execute(
            f"""
            SELECT "commit" AS commit_sha, status, mf_type, COUNT(*) AS count
            FROM backlog_bugs
            WHERE "commit" IN ({placeholders})
            GROUP BY "commit", status, mf_type
            """,
            selected,
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, dict[str, int]] = {
        commit: {"total": 0, "open": 0, "fixed": 0, "manual_fix": 0, "chain": 0}
        for commit in selected
    }
    for row in rows:
        commit = str(row["commit_sha"] or "")
        status = str(row["status"] or "").lower()
        mf_type = str(row["mf_type"] or "")
        count = int(row["count"] or 0)
        bucket = out.setdefault(commit, {"total": 0, "open": 0, "fixed": 0, "manual_fix": 0, "chain": 0})
        bucket["total"] += count
        if status == "open":
            bucket["open"] += count
        if status == "fixed":
            bucket["fixed"] += count
        if mf_type:
            bucket["manual_fix"] += count
        else:
            bucket["chain"] += count
    return out


def _pending_by_commit(conn: sqlite3.Connection, project_id: str) -> dict[str, dict[str, Any]]:
    pending = list_pending_scope_reconcile(
        conn,
        project_id,
        statuses=[PENDING_STATUS_QUEUED, PENDING_STATUS_RUNNING, PENDING_STATUS_FAILED],
    )
    return {str(row.get("commit_sha") or ""): row for row in pending}


def list_commit_timeline(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    limit: int = 50,
    include_backlog: bool = True,
) -> list[dict[str, Any]]:
    """Return latest snapshot-backed commits for commit-anchored dashboard navigation."""
    snapshots = list_graph_snapshots(conn, project_id, limit=limit)
    active = get_active_graph_snapshot(conn, project_id)
    active_snapshot_id = str(active.get("snapshot_id") or "") if active else ""
    pending = _pending_by_commit(conn, project_id)
    by_commit: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        commit = str(snapshot.get("commit_sha") or "")
        if not commit:
            continue
        existing = by_commit.get(commit)
        if existing and existing.get("snapshot_status") == SNAPSHOT_STATUS_ACTIVE:
            existing["snapshot_count"] += 1
            continue
        if existing and snapshot.get("status") != SNAPSHOT_STATUS_ACTIVE:
            existing["snapshot_count"] += 1
            continue
        summary = summarize_graph_snapshot(conn, project_id, snapshot["snapshot_id"])
        by_commit[commit] = {
            "commit_sha": commit,
            "short_sha": commit[:7],
            "subject": "",
            "created_at": snapshot.get("created_at", ""),
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_kind": snapshot["snapshot_kind"],
            "snapshot_status": snapshot["status"],
            "snapshot_count": int((existing or {}).get("snapshot_count") or 0) + 1,
            "graph_resolution": "exact",
            "is_active": snapshot["snapshot_id"] == active_snapshot_id,
            "pending_scope_reconcile": commit in pending,
            "pending_scope_status": pending.get(commit, {}).get("status", ""),
            "counts": summary["counts"],
            "health": summary["health"],
        }
    if include_backlog:
        backlog = _backlog_counts_for_commits(conn, by_commit.keys())
        for commit, row in by_commit.items():
            row["backlog"] = backlog.get(commit, {"total": 0, "open": 0, "fixed": 0, "manual_fix": 0, "chain": 0})
    return list(by_commit.values())[: max(1, min(int(limit or 50), 500))]


def resolve_commit_graph_state(
    conn: sqlite3.Connection,
    project_id: str,
    commit_sha: str,
) -> dict[str, Any]:
    """Resolve a commit to the graph snapshot dashboard should display."""
    ensure_schema(conn)
    commit_sha = str(commit_sha or "").strip()
    if not commit_sha:
        raise ValueError("commit_sha is required")

    active = get_active_graph_snapshot(conn, project_id)
    active_snapshot_id = str(active.get("snapshot_id") or "") if active else ""
    exact = get_graph_snapshot_for_commit(conn, project_id, commit_sha)
    pending_rows = list_pending_scope_reconcile(conn, project_id, commit_shas=[commit_sha])
    pending_active = [
        row for row in pending_rows
        if row.get("status") in {PENDING_STATUS_QUEUED, PENDING_STATUS_RUNNING, PENDING_STATUS_FAILED}
    ]
    if exact:
        return {
            "project_id": project_id,
            "commit_sha": commit_sha,
            "resolved_snapshot_id": exact["snapshot_id"],
            "resolution": "exact",
            "snapshot_status": exact["status"],
            "snapshot_kind": exact["snapshot_kind"],
            "has_graph": True,
            "has_semantic_review": bool(_snapshot_notes(exact).get("global_semantic_review")),
            "pending_scope_reconcile": bool(pending_active),
            "pending_scope_status": pending_active[0]["status"] if pending_active else "",
            "is_active": exact["snapshot_id"] == active_snapshot_id,
            "warnings": [],
        }
    if pending_active:
        return {
            "project_id": project_id,
            "commit_sha": commit_sha,
            "resolved_snapshot_id": "",
            "resolution": "pending",
            "snapshot_status": "",
            "snapshot_kind": "",
            "has_graph": False,
            "has_semantic_review": False,
            "pending_scope_reconcile": True,
            "pending_scope_status": pending_active[0]["status"],
            "is_active": False,
            "warnings": ["scope reconcile is pending for this commit"],
        }
    if active:
        return {
            "project_id": project_id,
            "commit_sha": commit_sha,
            "resolved_snapshot_id": active["snapshot_id"],
            "resolution": "advisory_latest",
            "snapshot_status": active["status"],
            "snapshot_kind": active["snapshot_kind"],
            "has_graph": True,
            "has_semantic_review": bool(_snapshot_notes(active).get("global_semantic_review")),
            "pending_scope_reconcile": False,
            "pending_scope_status": "",
            "is_active": True,
            "warnings": ["no exact graph snapshot for commit; showing latest active graph as advisory context"],
        }
    return {
        "project_id": project_id,
        "commit_sha": commit_sha,
        "resolved_snapshot_id": "",
        "resolution": "missing",
        "snapshot_status": "",
        "snapshot_kind": "",
        "has_graph": False,
        "has_semantic_review": False,
        "pending_scope_reconcile": False,
        "pending_scope_status": "",
        "is_active": False,
        "warnings": ["no graph snapshot is available"],
    }


def export_graph_snapshot_cache(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    project_root: str | Path,
    cache_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Export a non-authoritative graph cache into a project's .aming-claw/cache."""
    ensure_schema(conn)
    snapshot = get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    graph_path = snapshot_graph_path(project_id, snapshot_id)
    graph_json = _read_json_artifact(graph_path, {})
    if not isinstance(graph_json, dict) or not graph_json:
        raise ValueError(f"snapshot graph companion is empty or unreadable: {graph_path}")

    root = Path(project_root).resolve()
    base = Path(cache_dir).resolve() if cache_dir else root / ".aming-claw" / "cache"
    base.mkdir(parents=True, exist_ok=True)
    out_graph = base / "graph.current.json"
    out_manifest = base / "graph.current.manifest.json"
    graph_bytes = (
        json.dumps(graph_json, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n"
    ).encode("utf-8")
    graph_sha = _sha256_bytes(graph_bytes)
    out_graph.write_bytes(graph_bytes)
    manifest = {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "commit_sha": snapshot["commit_sha"],
        "snapshot_kind": snapshot["snapshot_kind"],
        "exported_at": utc_now(),
        "non_authoritative": True,
        "source_graph_sha256": snapshot["graph_sha256"],
        "export_graph_sha256": graph_sha,
        "graph_path": str(out_graph),
    }
    out_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "commit_sha": snapshot["commit_sha"],
        "cache_dir": str(base),
        "graph_path": str(out_graph),
        "manifest_path": str(out_manifest),
        "manifest": manifest,
    }


def abandon_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    actor: str = "observer",
    reason: str = "",
) -> dict[str, Any]:
    ensure_schema(conn)
    row = get_graph_snapshot(conn, project_id, snapshot_id)
    if not row:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    if row["status"] == SNAPSHOT_STATUS_ACTIVE:
        raise ValueError("active graph snapshot cannot be abandoned")
    if row["status"] == SNAPSHOT_STATUS_SUPERSEDED:
        raise ValueError("superseded graph snapshot cannot be abandoned")
    notes = _decode_json(row.get("notes"), {})
    if not isinstance(notes, dict):
        notes = {"previous_notes": row.get("notes") or ""}
    notes["abandoned"] = {
        "actor": actor,
        "reason": reason,
        "ts": utc_now(),
    }
    conn.execute(
        "UPDATE graph_snapshots SET status = ?, notes = ? WHERE project_id = ? AND snapshot_id = ?",
        (SNAPSHOT_STATUS_ABANDONED, _json(notes), project_id, snapshot_id),
    )
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "previous_status": row["status"],
        "status": SNAPSHOT_STATUS_ABANDONED,
    }


def get_latest_scan_baseline(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    try:
        row = conn.execute(
            """
            SELECT baseline_id, chain_version, scope_value, created_at
            FROM version_baselines
            WHERE project_id = ? AND scope_kind = 'commit_sweep'
            ORDER BY baseline_id DESC LIMIT 1
            """,
            (project_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def list_pending_scope_reconcile(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    statuses: Iterable[str] | None = None,
    commit_shas: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id]
    sql = "SELECT * FROM pending_scope_reconcile WHERE project_id = ?"
    status_values = [str(s) for s in statuses or [] if s]
    if status_values:
        placeholders = ",".join("?" for _ in status_values)
        sql += f" AND status IN ({placeholders})"
        params.extend(status_values)
    commit_values = [str(s) for s in commit_shas or [] if s]
    if commit_values:
        placeholders = ",".join("?" for _ in commit_values)
        sql += f" AND commit_sha IN ({placeholders})"
        params.extend(commit_values)
    sql += " ORDER BY queued_at, commit_sha"
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def graph_governance_status(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    active = get_active_graph_snapshot(conn, project_id)
    scan = get_latest_scan_baseline(conn, project_id)
    pending = list_pending_scope_reconcile(
        conn,
        project_id,
        statuses=[
            PENDING_STATUS_QUEUED,
            PENDING_STATUS_RUNNING,
            PENDING_STATUS_FAILED,
        ],
    )
    return {
        "project_id": project_id,
        "active_snapshot_id": active.get("snapshot_id") if active else "",
        "graph_snapshot_commit": active.get("commit_sha") if active else "",
        "materialized_graph_baseline_commit": active.get("commit_sha") if active else "",
        "scan_baseline_commit": scan.get("chain_version") if scan else "",
        "scan_baseline_id": scan.get("baseline_id") if scan else None,
        "pending_scope_reconcile_count": len(pending),
        "pending_scope_reconcile": pending,
    }


def strict_graph_ready(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    target_commit: str,
) -> dict[str, Any]:
    status = graph_governance_status(conn, project_id)
    graph_commit = status.get("materialized_graph_baseline_commit") or ""
    ok = bool(target_commit and graph_commit == target_commit)
    reason = ""
    if not graph_commit:
        reason = "no_active_graph_snapshot"
    elif not target_commit:
        reason = "missing_target_commit"
    elif graph_commit != target_commit:
        reason = "graph_snapshot_commit_mismatch"
    return {
        "ok": ok,
        "reason": reason,
        "target_commit": target_commit,
        **status,
    }


def import_existing_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    commit_sha: str = "",
    snapshot_id: str | None = None,
    created_by: str = "observer",
    activate: bool = False,
    expected_old_snapshot_id: str | None = None,
    extra_graph_paths: Iterable[str | Path] | None = None,
) -> dict[str, Any]:
    source = select_existing_graph_source(
        conn,
        project_id,
        extra_graph_paths=extra_graph_paths,
    )
    if not source:
        raise FileNotFoundError(f"no non-empty graph source found for project {project_id}")

    selected_commit = _resolve_import_commit(conn, project_id, commit_sha)
    sid = snapshot_id or snapshot_id_for("imported", selected_commit)
    source_notes = {
        "source_kind": source["source_kind"],
        "source_path": source["source_path"],
        "source_ref": source.get("source_ref", ""),
        "source_stats": source["stats"],
        "selected_commit": selected_commit,
    }
    snapshot = create_graph_snapshot(
        conn,
        project_id,
        snapshot_id=sid,
        commit_sha=selected_commit,
        snapshot_kind="imported",
        graph_json=source["graph_json"],
        file_inventory=[],
        drift_ledger=[],
        created_by=created_by,
        notes=_json(source_notes),
    )
    counts = index_graph_snapshot(
        conn,
        project_id,
        sid,
        nodes=_graph_nodes(source["graph_json"]),
        edges=_graph_edges(source["graph_json"]),
    )
    result = {
        **snapshot,
        "source": {k: v for k, v in source.items() if k != "graph_json"},
        "index_counts": counts,
        "activation": None,
    }
    if activate:
        result["activation"] = activate_graph_snapshot(
            conn,
            project_id,
            sid,
            expected_old_snapshot_id=expected_old_snapshot_id,
        )
    return result


def record_drift(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    snapshot_id: str,
    commit_sha: str,
    path: str,
    drift_type: str,
    target_symbol: str = "",
    node_id: str = "",
    status: str = "open",
    evidence: dict[str, Any] | None = None,
) -> None:
    ensure_schema(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO graph_drift_ledger
          (project_id, snapshot_id, commit_sha, path, node_id, target_symbol,
           drift_type, status, evidence_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            snapshot_id,
            commit_sha,
            path,
            node_id,
            target_symbol,
            drift_type,
            status,
            _json(evidence or {}),
            utc_now(),
        ),
    )


def list_graph_drift(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    snapshot_id: str = "",
    status: str = "",
    drift_type: str = "",
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id]
    sql = """
        SELECT project_id, snapshot_id, commit_sha, path, node_id,
               target_symbol, drift_type, status, evidence_json, updated_at
        FROM graph_drift_ledger
        WHERE project_id = ?
    """
    if snapshot_id:
        sql += " AND snapshot_id = ?"
        params.append(snapshot_id)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if drift_type:
        sql += " AND drift_type = ?"
        params.append(drift_type)
    sql += " ORDER BY updated_at DESC, path, drift_type, target_symbol LIMIT ? OFFSET ?"
    params.extend([max(1, min(int(limit or 200), 1000)), max(0, int(offset or 0))])
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "project_id": row["project_id"],
            "snapshot_id": row["snapshot_id"],
            "commit_sha": row["commit_sha"],
            "path": row["path"],
            "node_id": row["node_id"],
            "target_symbol": row["target_symbol"],
            "drift_type": row["drift_type"],
            "status": row["status"],
            "evidence": _decode_json(row["evidence_json"], {}),
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def get_graph_drift(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    snapshot_id: str,
    path: str,
    drift_type: str,
    target_symbol: str | None = None,
) -> dict[str, Any]:
    """Fetch one drift row. If target_symbol is omitted, the match must be unique."""
    ensure_schema(conn)
    params: list[Any] = [project_id, snapshot_id, path, drift_type]
    sql = """
        SELECT project_id, snapshot_id, commit_sha, path, node_id,
               target_symbol, drift_type, status, evidence_json, updated_at
        FROM graph_drift_ledger
        WHERE project_id = ?
          AND snapshot_id = ?
          AND path = ?
          AND drift_type = ?
    """
    if target_symbol is not None:
        sql += " AND target_symbol = ?"
        params.append(target_symbol)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        raise KeyError(f"graph drift row not found: {snapshot_id}/{path}/{drift_type}")
    if target_symbol is None and len(rows) > 1:
        raise ValueError("multiple drift rows match; target_symbol is required")
    row = rows[0]
    return {
        "project_id": row["project_id"],
        "snapshot_id": row["snapshot_id"],
        "commit_sha": row["commit_sha"],
        "path": row["path"],
        "node_id": row["node_id"],
        "target_symbol": row["target_symbol"],
        "drift_type": row["drift_type"],
        "status": row["status"],
        "evidence": _decode_json(row["evidence_json"], {}),
        "updated_at": row["updated_at"],
    }


def update_graph_drift_status(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    snapshot_id: str,
    path: str,
    drift_type: str,
    target_symbol: str = "",
    status: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update one drift row status while preserving/augmenting its evidence."""
    row = get_graph_drift(
        conn,
        project_id,
        snapshot_id=snapshot_id,
        path=path,
        drift_type=drift_type,
        target_symbol=target_symbol,
    )
    merged_evidence = dict(row.get("evidence") or {})
    merged_evidence.update(evidence or {})
    now = utc_now()
    conn.execute(
        """
        UPDATE graph_drift_ledger
        SET status = ?,
            evidence_json = ?,
            updated_at = ?
        WHERE project_id = ?
          AND snapshot_id = ?
          AND path = ?
          AND drift_type = ?
          AND target_symbol = ?
        """,
        (
            status,
            _json(merged_evidence),
            now,
            project_id,
            snapshot_id,
            path,
            drift_type,
            target_symbol,
        ),
    )
    row["status"] = status
    row["evidence"] = merged_evidence
    row["updated_at"] = now
    return row


def queue_pending_scope_reconcile(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    commit_sha: str,
    parent_commit_sha: str = "",
    status: str = PENDING_STATUS_QUEUED,
    snapshot_id: str = "",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    if status not in ALLOWED_PENDING_STATUSES:
        raise ValueError(f"invalid pending scope reconcile status: {status}")
    now = utc_now()
    conn.execute(
        """
        INSERT INTO pending_scope_reconcile
          (project_id, commit_sha, parent_commit_sha, queued_at, status,
           retry_count, snapshot_id, evidence_json)
        VALUES (?, ?, ?, ?, ?, 0, ?, ?)
        ON CONFLICT(project_id, commit_sha) DO UPDATE SET
          parent_commit_sha = CASE
            WHEN pending_scope_reconcile.parent_commit_sha = '' THEN excluded.parent_commit_sha
            ELSE pending_scope_reconcile.parent_commit_sha
          END,
          status = CASE
            WHEN pending_scope_reconcile.status IN ('materialized', 'waived')
            THEN pending_scope_reconcile.status
            ELSE excluded.status
          END,
          snapshot_id = CASE
            WHEN excluded.snapshot_id != '' THEN excluded.snapshot_id
            ELSE pending_scope_reconcile.snapshot_id
          END,
          evidence_json = excluded.evidence_json
        """,
        (
            project_id,
            commit_sha,
            parent_commit_sha,
            now,
            status,
            snapshot_id,
            _json(evidence or {}),
        ),
    )
    row = conn.execute(
        "SELECT * FROM pending_scope_reconcile WHERE project_id = ? AND commit_sha = ?",
        (project_id, commit_sha),
    ).fetchone()
    return dict(row)


def waive_pending_scope_reconcile(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    commit_shas: Iterable[str] | None = None,
    snapshot_id: str = "",
    actor: str = "observer",
    reason: str = "",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark retryable pending scope rows as waived with explicit evidence."""
    ensure_schema(conn)
    selected = [
        str(commit or "").strip()
        for commit in (commit_shas or [])
        if str(commit or "").strip()
    ]
    params: list[Any] = [project_id]
    sql = """
        SELECT commit_sha FROM pending_scope_reconcile
        WHERE project_id = ?
          AND status IN (?, ?, ?)
    """
    params.extend([PENDING_STATUS_QUEUED, PENDING_STATUS_RUNNING, PENDING_STATUS_FAILED])
    if selected:
        placeholders = ",".join("?" for _ in selected)
        sql += f" AND commit_sha IN ({placeholders})"
        params.extend(selected)
    sql += " ORDER BY queued_at, commit_sha"
    rows = conn.execute(sql, params).fetchall()
    targets = [row["commit_sha"] for row in rows]
    if not targets:
        return {
            "project_id": project_id,
            "waived_count": 0,
            "commit_shas": [],
            "snapshot_id": snapshot_id,
        }

    waiver_evidence = {
        "source": "pending_scope_waiver",
        "actor": actor,
        "reason": reason,
        "snapshot_id": snapshot_id,
        "commit_shas": targets,
        **(evidence or {}),
    }
    placeholders = ",".join("?" for _ in targets)
    cur = conn.execute(
        f"""
        UPDATE pending_scope_reconcile
        SET status = ?,
            snapshot_id = CASE WHEN ? != '' THEN ? ELSE snapshot_id END,
            evidence_json = ?
        WHERE project_id = ?
          AND commit_sha IN ({placeholders})
          AND status IN (?, ?, ?)
        """,
        (
            PENDING_STATUS_WAIVED,
            snapshot_id,
            snapshot_id,
            _json(waiver_evidence),
            project_id,
            *targets,
            PENDING_STATUS_QUEUED,
            PENDING_STATUS_RUNNING,
            PENDING_STATUS_FAILED,
        ),
    )
    return {
        "project_id": project_id,
        "waived_count": int(cur.rowcount or 0),
        "commit_shas": targets,
        "snapshot_id": snapshot_id,
    }


__all__ = [
    "ALLOWED_PENDING_STATUSES",
    "ALLOWED_SNAPSHOT_STATUSES",
    "GRAPH_SNAPSHOT_SCHEMA_SQL",
    "GraphSnapshotConflictError",
    "activate_graph_snapshot",
    "create_graph_snapshot",
    "ensure_schema",
    "export_graph_snapshot_cache",
    "finalize_graph_snapshot",
    "get_active_graph_snapshot",
    "get_graph_drift",
    "get_graph_snapshot",
    "get_latest_scan_baseline",
    "graph_governance_status",
    "graph_payload_edges",
    "index_graph_snapshot",
    "list_graph_snapshot_edges",
    "list_graph_snapshot_files",
    "list_graph_snapshot_nodes",
    "list_graph_snapshots",
    "list_graph_drift",
    "graph_payload_stats",
    "import_existing_graph_snapshot",
    "abandon_graph_snapshot",
    "list_pending_scope_reconcile",
    "queue_pending_scope_reconcile",
    "record_drift",
    "select_existing_graph_source",
    "snapshot_companion_dir",
    "snapshot_graph_path",
    "snapshot_id_for",
    "strict_graph_ready",
    "summarize_file_inventory_rows",
    "update_graph_drift_status",
    "waive_pending_scope_reconcile",
    "write_companion_files",
]
