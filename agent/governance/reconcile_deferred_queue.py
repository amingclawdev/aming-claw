"""Reconcile CR3 — persistent deferred queue for cluster-driven reconcile work.

Durable state machine:
    queued -> filing -> in_chain -> {resolved | failed_retryable | failed_terminal | skipped | expired}
    observer_hold / observer_takeover pause auto-flow for manual fix ownership.
    patch_accepted and superseded_bad_run are audit-safe terminal outcomes.

Public API (R2):
    * enqueue_or_lookup(project_id, cluster_fingerprint, payload, run_id) -> dict
    * get_next_batch(project_id, batch_size) -> list[dict]
    * mark_filing(project_id, fingerprint)
    * mark_in_chain(project_id, fingerprint, root_task_id)
    * mark_terminal(project_id, fingerprint, terminal_status, reason)
    * requeue_after_failure(project_id, fingerprint, retry_count_delta=1)
    * escalate(project_id, fingerprint) -> str

The module owns its own table-creation SQL so it works against any sqlite3
connection (production governance.db OR in-memory test DB).  Composite primary
key (project_id, cluster_fingerprint) provides multi-project isolation (R8).

Auto-session lifecycle (R5):
    * On the first cluster's enqueue (no active reconcile_session) the queue
      lazily calls ``reconcile_session.start_session``.
    * On the last in-flight row reaching a terminal status the queue calls
      ``reconcile_session.transition_to_finalizing``.

Both hooks are best-effort: a missing/inactive ``reconcile_session`` module
(or a connection without the ``reconcile_sessions`` table) is logged at
DEBUG and never raises.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

__all__ = [
    "RECONCILE_MAX_RETRIES",
    "DEFAULT_TTL_HOURS",
    "QUEUE_SCHEMA_SQL",
    "ensure_schema",
    "enqueue_or_lookup",
    "register_feature_clusters",
    "completion_summary",
    "sync_session_counts",
    "get_next_batch",
    "mark_filing",
    "mark_in_chain",
    "mark_observer_hold",
    "mark_observer_takeover",
    "release_observer_takeover",
    "mark_patch_accepted",
    "mark_superseded_bad_run",
    "mark_terminal",
    "requeue_after_failure",
    "escalate",
    "expire_stale_rows",
    "TERMINAL_STATUSES",
    "ACTIVE_STATUSES",
]

# R3: max retry budget; once exceeded -> failed_terminal + escalate().
RECONCILE_MAX_RETRIES = 3

# Default TTL between first_seen_at and expires_at (24h).  Rows still queued
# past expires_at get auto-promoted to 'expired' on the next get_next_batch().
DEFAULT_TTL_HOURS = 24

SAFE_TERMINAL_STATUSES = frozenset(
    {"resolved", "patch_accepted", "skipped", "skipped_explicit", "superseded_bad_run"}
)
UNRESOLVED_TERMINAL_STATUSES = frozenset({"failed_terminal", "expired"})
TERMINAL_STATUSES = SAFE_TERMINAL_STATUSES | UNRESOLVED_TERMINAL_STATUSES
ACTIVE_STATUSES = frozenset(
    {
        "queued",
        "filing",
        "in_chain",
        "failed_retryable",
        "observer_hold",
        "observer_takeover",
    }
)
QUEUE_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES

# R1: schema — composite PK + durable status CHECK + terminal-tracking columns +
# 4 indexes (UNIQUE, status, priority+age, project+status).
QUEUE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reconcile_deferred_clusters (
    project_id              TEXT NOT NULL,
    cluster_fingerprint     TEXT NOT NULL,
    payload_json            TEXT NOT NULL DEFAULT '{}',
    payload_sha256          TEXT NOT NULL DEFAULT '',
    run_id                  TEXT,
    status                  TEXT NOT NULL DEFAULT 'queued'
                              CHECK (status IN (
                                  'queued','filing','in_chain',
                                  'resolved','failed_retryable',
                                  'failed_terminal','skipped','expired',
                                  'observer_hold','observer_takeover',
                                  'patch_accepted','skipped_explicit',
                                  'superseded_bad_run'
                              )),
    priority                INTEGER NOT NULL DEFAULT 100,
    retry_count             INTEGER NOT NULL DEFAULT 0,
    first_seen_at           TEXT NOT NULL,
    last_seen_at            TEXT NOT NULL,
    expires_at              TEXT,
    bug_id                  TEXT,
    root_task_id            TEXT,
    filed_at                TEXT,
    last_terminal_status    TEXT,
    terminal_reason         TEXT,
    resolved_at             TEXT,
    skipped_reason          TEXT,
    next_retry_at           TEXT,
    candidate_hash          TEXT,
    candidate_node_refs_json TEXT NOT NULL DEFAULT '{}',
    accepted_patch_id       TEXT,
    takeover_by             TEXT,
    takeover_reason         TEXT,
    takeover_at             TEXT,
    PRIMARY KEY (project_id, cluster_fingerprint)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reconcile_deferred_unique
    ON reconcile_deferred_clusters (project_id, cluster_fingerprint);

CREATE INDEX IF NOT EXISTS idx_reconcile_deferred_status
    ON reconcile_deferred_clusters (status);

CREATE INDEX IF NOT EXISTS idx_reconcile_deferred_priority_age
    ON reconcile_deferred_clusters (priority ASC, first_seen_at ASC);

CREATE INDEX IF NOT EXISTS idx_reconcile_deferred_project_status
    ON reconcile_deferred_clusters (project_id, status);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_iso(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _payload_sha(payload: Any) -> str:
    try:
        blob = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = repr(payload)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cluster_fingerprint(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("cluster_fingerprint", "cluster_id", "fingerprint"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
    return _payload_sha(payload)[:16]


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    if row is None:
        return {}
    out: Dict[str, Any] = {}
    for key in row.keys():
        out[key] = row[key]
    if isinstance(out.get("payload_json"), str):
        try:
            out["payload"] = json.loads(out["payload_json"]) if out["payload_json"] else {}
        except (TypeError, ValueError):
            out["payload"] = {}
    return out


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _table_columns(conn: sqlite3.Connection, table_name: str) -> List[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({_quote_ident(table_name)})").fetchall()
    except sqlite3.OperationalError:
        return []
    return [str(r["name"] if isinstance(r, sqlite3.Row) else r[1]) for r in rows]


def _queue_schema_needs_migration(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'table' AND name = 'reconcile_deferred_clusters'"
    ).fetchone()
    if row is None:
        return False
    sql = str(row["sql"] if isinstance(row, sqlite3.Row) else row[0])
    if any(status not in sql for status in QUEUE_STATUSES):
        return True
    columns = set(_table_columns(conn, "reconcile_deferred_clusters"))
    required = {
        "candidate_hash",
        "candidate_node_refs_json",
        "accepted_patch_id",
        "takeover_by",
        "takeover_reason",
        "takeover_at",
    }
    return not required.issubset(columns)


def _migrate_queue_schema(conn: sqlite3.Connection) -> None:
    """Recreate the queue table when SQLite CHECK constraints are stale."""
    suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    legacy = f"reconcile_deferred_clusters_legacy_{suffix}"
    for idx in (
        "idx_reconcile_deferred_unique",
        "idx_reconcile_deferred_status",
        "idx_reconcile_deferred_priority_age",
        "idx_reconcile_deferred_project_status",
    ):
        conn.execute(f"DROP INDEX IF EXISTS {_quote_ident(idx)}")
    conn.execute(
        "ALTER TABLE reconcile_deferred_clusters RENAME TO "
        f"{_quote_ident(legacy)}"
    )
    conn.executescript(QUEUE_SCHEMA_SQL)
    old_cols = set(_table_columns(conn, legacy))
    new_cols = _table_columns(conn, "reconcile_deferred_clusters")
    common = [col for col in new_cols if col in old_cols]
    insert_cols = []
    select_exprs = []
    allowed = ",".join(f"'{s}'" for s in sorted(QUEUE_STATUSES))
    for col in common:
        insert_cols.append(_quote_ident(col))
        if col == "status":
            select_exprs.append(
                f"CASE WHEN status IN ({allowed}) THEN status ELSE 'failed_retryable' END"
            )
        elif col == "candidate_node_refs_json":
            select_exprs.append("COALESCE(candidate_node_refs_json, '{}')")
        else:
            select_exprs.append(_quote_ident(col))
    if insert_cols:
        conn.execute(
            "INSERT OR IGNORE INTO reconcile_deferred_clusters ("
            + ", ".join(insert_cols)
            + ") SELECT "
            + ", ".join(select_exprs)
            + f" FROM {_quote_ident(legacy)}"
        )
    conn.execute(f"DROP TABLE {_quote_ident(legacy)}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create or migrate the queue table + 4 indexes (idempotent)."""
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row
    if _queue_schema_needs_migration(conn):
        _migrate_queue_schema(conn)
    conn.executescript(QUEUE_SCHEMA_SQL)
    conn.commit()


def _get_conn(conn: Optional[sqlite3.Connection], project_id: str) -> sqlite3.Connection:
    if conn is not None:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        return conn
    # Lazy import to avoid pulling governance.db at module import time
    # (tests use isolated in-memory DB via monkeypatch).
    from .db import get_connection  # noqa: WPS433
    c = get_connection(project_id)
    ensure_schema(c)
    return c


def _has_active_session(conn: sqlite3.Connection, project_id: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM reconcile_sessions "
            "WHERE project_id = ? AND status IN ('active','finalizing','finalize_failed') LIMIT 1",
            (project_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _maybe_start_session(
    conn: sqlite3.Connection, project_id: str, run_id: Optional[str]
) -> None:
    """R5 hook — start a reconcile_session if none is active."""
    if _has_active_session(conn, project_id):
        return
    try:
        from . import reconcile_session  # local import — circular safe

        reconcile_session.start_session(
            conn, project_id, run_id=run_id, started_by="reconcile-deferred-queue",
        )
    except Exception as exc:
        log.debug(
            "reconcile_deferred_queue: start_session hook skipped: %s", exc,
        )


def _maybe_finalize_session(conn: sqlite3.Connection, project_id: str) -> None:
    # Find the active session to transition.
    try:
        row = conn.execute(
            "SELECT session_id, run_id FROM reconcile_sessions "
            "WHERE project_id = ? AND status = 'active' LIMIT 1",
            (project_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return
    if not row:
        return
    run_id = row["run_id"] if isinstance(row, sqlite3.Row) else row[1]
    summary = sync_session_counts(
        project_id,
        run_id=run_id,
        session_id=row["session_id"] if isinstance(row, sqlite3.Row) else row[0],
        conn=conn,
    )
    if not summary.get("ready_for_finalize"):
        return
    try:
        from . import reconcile_session  # local import — circular safe

        reconcile_session.transition_to_finalizing(
            conn,
            project_id,
            row["session_id"] if isinstance(row, sqlite3.Row) else row[0],
        )
    except Exception as exc:
        log.debug(
            "reconcile_deferred_queue: finalize hook skipped: %s", exc,
        )


def _sync_backlog_runtime_for_cluster(
    conn: sqlite3.Connection,
    project_id: str,
    cluster_fingerprint: str,
    *,
    stage: str,
    runtime_state: str,
    reason: str = "",
    takeover: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort mirror from cluster queue to its linked backlog row."""
    try:
        row = conn.execute(
            "SELECT bug_id, root_task_id FROM reconcile_deferred_clusters "
            "WHERE project_id = ? AND cluster_fingerprint = ?",
            (project_id, cluster_fingerprint),
        ).fetchone()
    except sqlite3.OperationalError:
        return
    if not row:
        return
    bug_id = row["bug_id"] if isinstance(row, sqlite3.Row) else row[0]
    root_task_id = row["root_task_id"] if isinstance(row, sqlite3.Row) else row[1]
    if not bug_id:
        return
    try:
        from . import backlog_runtime  # noqa: WPS433

        backlog_runtime.update_backlog_runtime(
            conn,
            str(bug_id),
            stage,
            project_id=project_id,
            failure_reason=reason,
            runtime_state=runtime_state,
            root_task_id=str(root_task_id or ""),
            takeover=takeover,
        )
    except Exception:
        log.debug(
            "reconcile_deferred_queue: backlog runtime sync failed for %s",
            bug_id,
            exc_info=True,
        )


def _refresh_session_after_status(
    conn: sqlite3.Connection,
    project_id: str,
    cluster_fingerprint: str,
    status: str,
) -> None:
    row = conn.execute(
        "SELECT run_id FROM reconcile_deferred_clusters "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (project_id, cluster_fingerprint),
    ).fetchone()
    run_id = row["run_id"] if row is not None else None
    sync_session_counts(project_id, run_id=run_id, conn=conn)
    if status in TERMINAL_STATUSES:
        _maybe_finalize_session(conn, project_id)


# ---------------------------------------------------------------------------
# Public API (R2)
# ---------------------------------------------------------------------------

def enqueue_or_lookup(
    project_id: str,
    cluster_fingerprint: str,
    payload: Optional[dict] = None,
    run_id: Optional[str] = None,
    *,
    conn: Optional[sqlite3.Connection] = None,
    priority: int = 100,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> Dict[str, Any]:
    """Insert a new row OR return existing row metadata.

    Per §4.6.3 — when an existing row's payload deltas changed (sha256
    mismatch), the row is re-queued with retry_count reset to 0.  When the
    payload is unchanged, the existing row is returned as-is (with
    last_seen_at refreshed).

    Triggers R5 auto-session-start on the first ever enqueue.
    """
    c = _get_conn(conn, project_id)
    payload = payload or {}
    sha = _payload_sha(payload)
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    now = _utc_iso()
    expires = _utc_iso(datetime.now(timezone.utc) + timedelta(hours=ttl_hours))

    existing = c.execute(
        "SELECT * FROM reconcile_deferred_clusters "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (project_id, cluster_fingerprint),
    ).fetchone()

    if existing is None:
        # First sighting for this cluster — auto-start session if needed (R5).
        _maybe_start_session(c, project_id, run_id)
        c.execute(
            "INSERT INTO reconcile_deferred_clusters ("
            "  project_id, cluster_fingerprint, payload_json, payload_sha256, "
            "  run_id, status, priority, retry_count, "
            "  first_seen_at, last_seen_at, expires_at"
            ") VALUES (?, ?, ?, ?, ?, 'queued', ?, 0, ?, ?, ?)",
            (project_id, cluster_fingerprint, blob, sha, run_id,
             priority, now, now, expires),
        )
        c.commit()
        row = c.execute(
            "SELECT * FROM reconcile_deferred_clusters "
            "WHERE project_id = ? AND cluster_fingerprint = ?",
            (project_id, cluster_fingerprint),
        ).fetchone()
        out = _row_to_dict(row)
        out["existed"] = False
        out["deltas_changed"] = False
        return out

    existed_dict = _row_to_dict(existing)
    deltas_changed = existed_dict.get("payload_sha256") != sha
    run_changed = bool(run_id) and str(existed_dict.get("run_id") or "") != str(run_id)
    if deltas_changed or run_changed:
        # Per §4.6.3: deltas-change detection re-queues with retry_count = 0.
        # A new Phase Z run for the same fingerprint is also re-queued so stale
        # root_task_id/bug_id terminal state from a prior run cannot masquerade
        # as current-run chain progress.
        c.execute(
            "UPDATE reconcile_deferred_clusters SET "
            "  payload_json = ?, payload_sha256 = ?, run_id = ?, "
            "  status = 'queued', retry_count = 0, last_seen_at = ?, "
            "  expires_at = ?, bug_id = NULL, root_task_id = NULL, "
            "  filed_at = NULL, last_terminal_status = NULL, "
            "  terminal_reason = NULL, resolved_at = NULL, "
            "  skipped_reason = NULL, next_retry_at = NULL, "
            "  accepted_patch_id = NULL, takeover_by = NULL, "
            "  takeover_reason = NULL, takeover_at = NULL "
            "WHERE project_id = ? AND cluster_fingerprint = ?",
            (blob, sha, run_id, now, expires, project_id, cluster_fingerprint),
        )
    else:
        if str(existed_dict.get("status") or "") == "queued":
            c.execute(
                "UPDATE reconcile_deferred_clusters SET last_seen_at = ?, "
                "  bug_id = NULL, root_task_id = NULL, filed_at = NULL, "
                "  last_terminal_status = NULL, terminal_reason = NULL, "
                "  resolved_at = NULL, skipped_reason = NULL, next_retry_at = NULL, "
                "  accepted_patch_id = NULL, takeover_by = NULL, "
                "  takeover_reason = NULL, takeover_at = NULL "
                "WHERE project_id = ? AND cluster_fingerprint = ?",
                (now, project_id, cluster_fingerprint),
            )
        else:
            c.execute(
                "UPDATE reconcile_deferred_clusters SET last_seen_at = ? "
                "WHERE project_id = ? AND cluster_fingerprint = ?",
                (now, project_id, cluster_fingerprint),
            )
    c.commit()
    row = c.execute(
        "SELECT * FROM reconcile_deferred_clusters "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (project_id, cluster_fingerprint),
    ).fetchone()
    out = _row_to_dict(row)
    out["existed"] = True
    out["deltas_changed"] = deltas_changed
    out["run_changed"] = run_changed
    return out


def register_feature_clusters(
    project_id: str,
    run_id: str,
    feature_clusters: List[Dict[str, Any]],
    *,
    conn: Optional[sqlite3.Connection] = None,
    priority: int = 100,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> Dict[str, Any]:
    """Register every FeatureCluster from one Phase Z run in the queue.

    This is the durable handoff between discovery and chain execution: orphan
    analysis/finalize can later ask whether the run's clusters all reached a
    safe terminal state instead of inferring progress from ad-hoc backlog rows.
    """
    c = _get_conn(conn, project_id)
    clusters = feature_clusters if isinstance(feature_clusters, list) else []
    if clusters:
        _maybe_start_session(c, project_id, run_id)
    result: Dict[str, Any] = {
        "project_id": project_id,
        "run_id": run_id,
        "expected": len(clusters),
        "registered": 0,
        "created": 0,
        "existing": 0,
        "changed": 0,
        "requeued": 0,
        "errors": [],
        "fingerprints": [],
    }
    for idx, cluster in enumerate(clusters):
        if not isinstance(cluster, dict):
            result["errors"].append({"index": idx, "reason": "invalid_cluster"})
            continue
        fingerprint = _cluster_fingerprint(cluster)
        if not fingerprint:
            result["errors"].append({"index": idx, "reason": "missing_fingerprint"})
            continue
        payload = dict(cluster)
        payload.setdefault("cluster_fingerprint", fingerprint)
        try:
            row = enqueue_or_lookup(
                project_id,
                fingerprint,
                payload=payload,
                run_id=run_id,
                conn=c,
                priority=priority,
                ttl_hours=ttl_hours,
            )
        except Exception as exc:  # noqa: BLE001
            result["errors"].append({
                "index": idx,
                "cluster_fingerprint": fingerprint,
                "reason": str(exc),
            })
            continue
        result["registered"] += 1
        result["fingerprints"].append(fingerprint)
        if row.get("existed"):
            result["existing"] += 1
            if row.get("deltas_changed"):
                result["changed"] += 1
            if row.get("deltas_changed") or row.get("run_changed"):
                result["requeued"] += 1
        else:
            result["created"] += 1

    sync_session_counts(project_id, run_id=run_id, conn=c)
    result["summary"] = completion_summary(project_id, run_id=run_id, conn=c)
    return result


def completion_summary(
    project_id: str,
    *,
    run_id: Optional[str] = None,
    expected_fingerprints: Optional[List[str]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Return queue completion state for a project/run.

    ``ready_for_orphan_pass`` is intentionally stricter than "all rows are
    terminal": failed/expired rows must be retried or explicitly skipped before
    graph finalize or orphan batching can proceed.
    """
    c = _get_conn(conn, project_id)
    sql = "SELECT cluster_fingerprint, status FROM reconcile_deferred_clusters WHERE project_id = ?"
    args: List[Any] = [project_id]
    if run_id:
        sql += " AND run_id = ?"
        args.append(run_id)
    rows = c.execute(sql, tuple(args)).fetchall()
    status_counts: Dict[str, int] = {}
    seen = set()
    for row in rows:
        status = str(row["status"] or "")
        status_counts[status] = status_counts.get(status, 0) + 1
        seen.add(str(row["cluster_fingerprint"] or ""))

    expected = {str(fp) for fp in (expected_fingerprints or []) if str(fp)}
    missing = sorted(expected - seen)
    active_count = sum(status_counts.get(s, 0) for s in ACTIVE_STATUSES)
    terminal_count = sum(status_counts.get(s, 0) for s in TERMINAL_STATUSES)
    unresolved_terminal_count = sum(
        status_counts.get(s, 0) for s in UNRESOLVED_TERMINAL_STATUSES
    )
    total = len(rows)
    blocking_count = active_count + unresolved_terminal_count + len(missing)
    all_terminal = total > 0 and active_count == 0 and not missing
    ready = total > 0 and blocking_count == 0
    return {
        "project_id": project_id,
        "run_id": run_id or "",
        "total": total,
        "status_counts": status_counts,
        "terminal_count": terminal_count,
        "active_count": active_count,
        "unresolved_terminal_count": unresolved_terminal_count,
        "missing_count": len(missing),
        "missing_fingerprints": missing,
        "all_terminal": all_terminal,
        "ready_for_orphan_pass": ready,
        "ready_for_finalize": ready,
        "blocking_count": blocking_count,
    }


def sync_session_counts(
    project_id: str,
    *,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Mirror queue counts into reconcile_sessions.cluster_count_* columns."""
    c = _get_conn(conn, project_id)
    summary = completion_summary(project_id, run_id=run_id, conn=c)
    failed = (
        summary["status_counts"].get("failed_retryable", 0)
        + summary["status_counts"].get("failed_terminal", 0)
        + summary["status_counts"].get("expired", 0)
    )
    resolved = (
        summary["status_counts"].get("resolved", 0)
        + summary["status_counts"].get("patch_accepted", 0)
        + summary["status_counts"].get("superseded_bad_run", 0)
    )
    sql = (
        "UPDATE reconcile_sessions SET "
        "cluster_count_total = ?, cluster_count_resolved = ?, cluster_count_failed = ? "
        "WHERE project_id = ?"
    )
    args: List[Any] = [
        int(summary["total"]),
        int(resolved),
        int(failed),
        project_id,
    ]
    if session_id:
        sql += " AND session_id = ?"
        args.append(session_id)
    elif run_id:
        sql += " AND run_id = ?"
        args.append(run_id)
    else:
        sql += " AND status IN ('active','finalizing')"
    try:
        c.execute(sql, tuple(args))
        c.commit()
    except sqlite3.OperationalError:
        pass
    return summary


def expire_stale_rows(
    project_id: str, *, conn: Optional[sqlite3.Connection] = None
) -> int:
    """Promote any queued rows past expires_at to 'expired'.

    Returns the number of rows promoted.  Called automatically by
    get_next_batch() so callers rarely need this directly.
    """
    c = _get_conn(conn, project_id)
    now = _utc_iso()
    cur = c.execute(
        "UPDATE reconcile_deferred_clusters SET status = 'expired', "
        "  last_terminal_status = 'expired', terminal_reason = 'ttl_elapsed', "
        "  resolved_at = ? "
        "WHERE project_id = ? AND status = 'queued' AND expires_at IS NOT NULL "
        "  AND expires_at < ?",
        (now, project_id, now),
    )
    c.commit()
    return cur.rowcount or 0


def get_next_batch(
    project_id: str,
    batch_size: int = 10,
    *,
    run_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """Return up to batch_size queued rows ordered by priority+age.

    Side effect: promotes any rows past TTL to 'expired' before fetching.
    """
    c = _get_conn(conn, project_id)
    expire_stale_rows(project_id, conn=c)
    sql = (
        "SELECT * FROM reconcile_deferred_clusters "
        "WHERE project_id = ? AND status = 'queued' "
    )
    args: List[Any] = [project_id]
    if run_id:
        sql += "AND run_id = ? "
        args.append(run_id)
    sql += "ORDER BY priority ASC, first_seen_at ASC LIMIT ?"
    args.append(max(1, int(batch_size or 1)))
    rows = c.execute(sql, tuple(args)).fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_filing(
    project_id: str,
    cluster_fingerprint: str,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """queued -> filing transition.  Returns True on a state change."""
    c = _get_conn(conn, project_id)
    now = _utc_iso()
    cur = c.execute(
        "UPDATE reconcile_deferred_clusters SET status = 'filing', filed_at = ? "
        "WHERE project_id = ? AND cluster_fingerprint = ? AND status IN ('queued','failed_retryable')",
        (now, project_id, cluster_fingerprint),
    )
    c.commit()
    return (cur.rowcount or 0) > 0


def mark_in_chain(
    project_id: str,
    cluster_fingerprint: str,
    root_task_id: str,
    *,
    bug_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """filing -> in_chain transition.  Records root_task_id and (optional) bug_id."""
    c = _get_conn(conn, project_id)
    cur = c.execute(
        "UPDATE reconcile_deferred_clusters SET status = 'in_chain', "
        "  root_task_id = ?, bug_id = COALESCE(?, bug_id) "
        "WHERE project_id = ? AND cluster_fingerprint = ? AND status IN ('filing','queued')",
        (root_task_id, bug_id, project_id, cluster_fingerprint),
    )
    c.commit()
    if (cur.rowcount or 0) > 0:
        _sync_backlog_runtime_for_cluster(
            c,
            project_id,
            cluster_fingerprint,
            stage="chain_in_progress",
            runtime_state="running",
        )
    return (cur.rowcount or 0) > 0


def mark_observer_hold(
    project_id: str,
    cluster_fingerprint: str,
    *,
    reason: str = "",
    actor: str = "observer",
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Pause a cluster before automatic filing/dispatch can continue."""
    c = _get_conn(conn, project_id)
    cur = c.execute(
        "UPDATE reconcile_deferred_clusters SET status = 'observer_hold', "
        "  takeover_by = ?, takeover_reason = ?, takeover_at = ? "
        "WHERE project_id = ? AND cluster_fingerprint = ? "
        "  AND status NOT IN ('resolved','patch_accepted','skipped',"
        "                     'skipped_explicit','superseded_bad_run')",
        (actor, reason, _utc_iso(), project_id, cluster_fingerprint),
    )
    c.commit()
    changed = (cur.rowcount or 0) > 0
    if changed:
        _sync_backlog_runtime_for_cluster(
            c,
            project_id,
            cluster_fingerprint,
            stage="observer_hold",
            runtime_state="observer_hold",
            reason=reason,
            takeover={"mode": "observer_hold", "actor": actor, "reason": reason},
        )
        _refresh_session_after_status(c, project_id, cluster_fingerprint, "observer_hold")
    return changed


def mark_observer_takeover(
    project_id: str,
    cluster_fingerprint: str,
    *,
    reason: str = "",
    actor: str = "observer",
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Mark a chain-owned cluster as manually owned by observer/MF."""
    c = _get_conn(conn, project_id)
    cur = c.execute(
        "UPDATE reconcile_deferred_clusters SET status = 'observer_takeover', "
        "  takeover_by = ?, takeover_reason = ?, takeover_at = ? "
        "WHERE project_id = ? AND cluster_fingerprint = ? "
        "  AND status NOT IN ('resolved','patch_accepted','skipped',"
        "                     'skipped_explicit','superseded_bad_run')",
        (actor, reason, _utc_iso(), project_id, cluster_fingerprint),
    )
    c.commit()
    changed = (cur.rowcount or 0) > 0
    if changed:
        _sync_backlog_runtime_for_cluster(
            c,
            project_id,
            cluster_fingerprint,
            stage="observer_takeover",
            runtime_state="observer_takeover",
            reason=reason,
            takeover={"mode": "observer_takeover", "actor": actor, "reason": reason},
        )
        _refresh_session_after_status(
            c, project_id, cluster_fingerprint, "observer_takeover",
        )
    return changed


def release_observer_takeover(
    project_id: str,
    cluster_fingerprint: str,
    *,
    next_status: str = "queued",
    reason: str = "",
    actor: str = "observer",
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Return an observer-owned cluster to auto-flow or a safe terminal state."""
    if next_status not in {
        "queued",
        "failed_retryable",
        "patch_accepted",
        "skipped_explicit",
        "superseded_bad_run",
    }:
        raise ValueError(
            "release_observer_takeover: next_status must be queued, "
            "failed_retryable, patch_accepted, skipped_explicit, or superseded_bad_run"
        )
    c = _get_conn(conn, project_id)
    now = _utc_iso()
    terminal_reason = reason if next_status in TERMINAL_STATUSES else None
    resolved_at = now if next_status in TERMINAL_STATUSES else None
    cur = c.execute(
        "UPDATE reconcile_deferred_clusters SET status = ?, "
        "  terminal_reason = COALESCE(?, terminal_reason), "
        "  last_terminal_status = CASE WHEN ? != '' THEN ? ELSE last_terminal_status END, "
        "  resolved_at = COALESCE(?, resolved_at), "
        "  takeover_by = ?, takeover_reason = ?, takeover_at = ? "
        "WHERE project_id = ? AND cluster_fingerprint = ? "
        "  AND status IN ('observer_hold','observer_takeover')",
        (
            next_status,
            terminal_reason,
            next_status if next_status in TERMINAL_STATUSES else "",
            next_status if next_status in TERMINAL_STATUSES else "",
            resolved_at,
            actor,
            reason,
            now,
            project_id,
            cluster_fingerprint,
        ),
    )
    c.commit()
    changed = (cur.rowcount or 0) > 0
    if changed:
        _sync_backlog_runtime_for_cluster(
            c,
            project_id,
            cluster_fingerprint,
            stage=f"observer_release_{next_status}",
            runtime_state=next_status,
            reason=reason,
            takeover={"mode": "observer_release", "actor": actor, "reason": reason},
        )
        _refresh_session_after_status(c, project_id, cluster_fingerprint, next_status)
    return changed


def mark_patch_accepted(
    project_id: str,
    cluster_fingerprint: str,
    *,
    patch_id: str = "",
    reason: str = "observer_patch_accepted",
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Terminal marker for observer/MF repaired clusters."""
    c = _get_conn(conn, project_id)
    now = _utc_iso()
    cur = c.execute(
        "UPDATE reconcile_deferred_clusters SET status = 'patch_accepted', "
        "  accepted_patch_id = ?, last_terminal_status = 'patch_accepted', "
        "  terminal_reason = ?, resolved_at = ? "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (patch_id, reason, now, project_id, cluster_fingerprint),
    )
    c.commit()
    changed = (cur.rowcount or 0) > 0
    if changed:
        _sync_backlog_runtime_for_cluster(
            c,
            project_id,
            cluster_fingerprint,
            stage="observer_patch_accepted",
            runtime_state="patch_accepted",
            reason=reason,
        )
        _refresh_session_after_status(c, project_id, cluster_fingerprint, "patch_accepted")
    return changed


def mark_superseded_bad_run(
    project_id: str,
    cluster_fingerprint: str,
    *,
    reason: str = "superseded_bad_run",
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Quarantine a bad cluster attempt so finalize ignores that run output."""
    c = _get_conn(conn, project_id)
    now = _utc_iso()
    cur = c.execute(
        "UPDATE reconcile_deferred_clusters SET status = 'superseded_bad_run', "
        "  last_terminal_status = 'superseded_bad_run', terminal_reason = ?, "
        "  resolved_at = ? "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (reason, now, project_id, cluster_fingerprint),
    )
    c.commit()
    changed = (cur.rowcount or 0) > 0
    if changed:
        _sync_backlog_runtime_for_cluster(
            c,
            project_id,
            cluster_fingerprint,
            stage="superseded_bad_run",
            runtime_state="superseded_bad_run",
            reason=reason,
        )
        _refresh_session_after_status(
            c, project_id, cluster_fingerprint, "superseded_bad_run",
        )
    return changed


def mark_terminal(
    project_id: str,
    cluster_fingerprint: str,
    terminal_status: str,
    reason: str = "",
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """{any active} -> terminal transition.

    terminal_status must be one of the durable queue statuses used as terminal
    or retry handoff states.
    """
    if terminal_status not in TERMINAL_STATUSES | {"failed_retryable"}:
        raise ValueError(
            f"reconcile_deferred_queue.mark_terminal: invalid status {terminal_status!r}"
        )
    c = _get_conn(conn, project_id)
    now = _utc_iso()
    skipped_reason = (
        reason if terminal_status in {"skipped", "skipped_explicit"} else None
    )
    cur = c.execute(
        "UPDATE reconcile_deferred_clusters SET status = ?, "
        "  last_terminal_status = ?, terminal_reason = ?, "
        "  skipped_reason = COALESCE(?, skipped_reason), resolved_at = ? "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (terminal_status, terminal_status, reason, skipped_reason, now,
         project_id, cluster_fingerprint),
    )
    c.commit()
    changed = (cur.rowcount or 0) > 0
    if changed:
        _sync_backlog_runtime_for_cluster(
            c,
            project_id,
            cluster_fingerprint,
            stage=f"queue_{terminal_status}",
            runtime_state=terminal_status,
            reason=reason,
        )
        _refresh_session_after_status(c, project_id, cluster_fingerprint, terminal_status)
    return changed


def requeue_after_failure(
    project_id: str,
    cluster_fingerprint: str,
    retry_count_delta: int = 1,
    *,
    reason: str = "",
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Increment retry_count + apply 2^retry_count hour backoff (R3).

    When retry_count would exceed RECONCILE_MAX_RETRIES, transitions to
    'failed_terminal' and calls escalate().  Otherwise transitions to
    'failed_retryable' with next_retry_at = now + 2^retry_count hours.

    Returns a dict describing the outcome:
        {"status": ..., "retry_count": ..., "next_retry_at": ...,
         "escalated_bug_id": <id or None>}
    """
    c = _get_conn(conn, project_id)
    row = c.execute(
        "SELECT retry_count FROM reconcile_deferred_clusters "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (project_id, cluster_fingerprint),
    ).fetchone()
    if row is None:
        return {"status": "missing", "retry_count": 0, "next_retry_at": None,
                "escalated_bug_id": None}
    new_count = int(row["retry_count"] or 0) + max(0, int(retry_count_delta or 0))
    if new_count > RECONCILE_MAX_RETRIES:
        # Exhausted — terminal failure + escalate.
        mark_terminal(
            project_id, cluster_fingerprint, "failed_terminal",
            reason=reason or "retry_count exhausted", conn=c,
        )
        c.execute(
            "UPDATE reconcile_deferred_clusters SET retry_count = ? "
            "WHERE project_id = ? AND cluster_fingerprint = ?",
            (new_count, project_id, cluster_fingerprint),
        )
        c.commit()
        bug_id = escalate(project_id, cluster_fingerprint, conn=c)
        return {"status": "failed_terminal", "retry_count": new_count,
                "next_retry_at": None, "escalated_bug_id": bug_id}
    # Still within budget — schedule retry.
    backoff_h = max(1, int(math.pow(2, max(0, new_count))))
    next_retry = _utc_iso(datetime.now(timezone.utc) + timedelta(hours=backoff_h))
    c.execute(
        "UPDATE reconcile_deferred_clusters SET status = 'failed_retryable', "
        "  retry_count = ?, next_retry_at = ?, terminal_reason = ? "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (new_count, next_retry, reason, project_id, cluster_fingerprint),
    )
    c.commit()
    return {"status": "failed_retryable", "retry_count": new_count,
            "next_retry_at": next_retry, "escalated_bug_id": None}


def escalate(
    project_id: str,
    cluster_fingerprint: str,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    """File a P0 OPT-BACKLOG row for observer attention when retries exhausted.

    Returns the bug_id (always returned even if best-effort backlog write fails).
    """
    bug_id = (
        "OPT-BACKLOG-RECONCILE-CLUSTER-"
        f"{(cluster_fingerprint or '')[:8]}-NEEDS-OBSERVER"
    )
    c = _get_conn(conn, project_id)
    now = _utc_iso()
    # Persist bug_id on the row so the UI can link it.
    try:
        c.execute(
            "UPDATE reconcile_deferred_clusters SET bug_id = ? "
            "WHERE project_id = ? AND cluster_fingerprint = ?",
            (bug_id, project_id, cluster_fingerprint),
        )
        c.commit()
    except sqlite3.OperationalError:
        pass
    # Best-effort: insert into backlog_bugs if the table exists (production
    # path).  Tests may run without it — swallow the error.
    try:
        c.execute(
            "INSERT OR IGNORE INTO backlog_bugs ("
            "  bug_id, title, status, priority, target_files, test_files, "
            "  acceptance_criteria, chain_task_id, \"commit\", discovered_at, "
            "  fixed_at, details_md, chain_trigger_json, required_docs, "
            "  provenance_paths, chain_stage, last_failure_reason, "
            "  stage_updated_at, created_at, updated_at"
            ") VALUES (?, ?, 'OPEN', 'P0', '[]', '[]', '[]', '', '', ?, '', "
            "         ?, '{}', '[]', '[]', '', '', '', ?, ?)",
            (bug_id,
             f"Reconcile cluster {cluster_fingerprint[:8]} needs observer review",
             now,
             f"Cluster {cluster_fingerprint} exceeded RECONCILE_MAX_RETRIES "
             f"({RECONCILE_MAX_RETRIES}); manual triage required.",
             now, now),
        )
        c.commit()
    except sqlite3.OperationalError:
        log.debug("escalate: backlog_bugs table missing — skipped insert")
    return bug_id
