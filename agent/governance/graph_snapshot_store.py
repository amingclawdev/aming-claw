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


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


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
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "commit_sha": snapshot["commit_sha"],
        "previous_snapshot_id": old_id,
        "ref_name": ref_name,
    }


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


def list_graph_snapshot_nodes(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    limit: int = 200,
    offset: int = 0,
    layer: str = "",
    kind: str = "",
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id, snapshot_id]
    sql = """
        SELECT node_id, layer, title, kind, primary_files_json,
               secondary_files_json, test_files_json, metadata_json
        FROM graph_nodes_index
        WHERE project_id = ? AND snapshot_id = ?
    """
    if layer:
        sql += " AND layer = ?"
        params.append(layer)
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY node_id LIMIT ? OFFSET ?"
    params.extend([max(1, min(int(limit or 200), 1000)), max(0, int(offset or 0))])
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "node_id": row["node_id"],
            "layer": row["layer"],
            "title": row["title"],
            "kind": row["kind"],
            "primary_files": _decode_json(row["primary_files_json"], []),
            "secondary_files": _decode_json(row["secondary_files_json"], []),
            "test_files": _decode_json(row["test_files_json"], []),
            "metadata": _decode_json(row["metadata_json"], {}),
        }
        for row in rows
    ]


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
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id]
    sql = "SELECT * FROM pending_scope_reconcile WHERE project_id = ?"
    status_values = [str(s) for s in statuses or [] if s]
    if status_values:
        placeholders = ",".join("?" for _ in status_values)
        sql += f" AND status IN ({placeholders})"
        params.extend(status_values)
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
    "finalize_graph_snapshot",
    "get_active_graph_snapshot",
    "get_graph_snapshot",
    "get_latest_scan_baseline",
    "graph_governance_status",
    "graph_payload_edges",
    "index_graph_snapshot",
    "list_graph_snapshot_edges",
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
    "waive_pending_scope_reconcile",
    "write_companion_files",
]
