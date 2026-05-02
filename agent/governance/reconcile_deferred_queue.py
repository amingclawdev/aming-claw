"""Reconcile CR3 — persistent deferred queue for cluster-driven reconcile work.

8-state machine:
    queued -> filing -> in_chain -> {resolved | failed_retryable | failed_terminal | skipped | expired}

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
    "get_next_batch",
    "mark_filing",
    "mark_in_chain",
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

TERMINAL_STATUSES = frozenset(
    {"resolved", "failed_terminal", "skipped", "expired"}
)
ACTIVE_STATUSES = frozenset({"queued", "filing", "in_chain", "failed_retryable"})

# R1: schema — composite PK + 8-state CHECK + terminal-tracking columns +
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
                                  'failed_terminal','skipped','expired'
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


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the queue table + 4 indexes (idempotent)."""
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row
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
            "WHERE project_id = ? AND status IN ('active','finalizing') LIMIT 1",
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
    """R5 hook — when no active rows remain, transition session to finalizing."""
    try:
        active = conn.execute(
            "SELECT COUNT(*) FROM reconcile_deferred_clusters "
            "WHERE project_id = ? AND status IN ('queued','filing','in_chain','failed_retryable')",
            (project_id,),
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return
    if int(active or 0) > 0:
        return
    # Find the active session to transition.
    try:
        row = conn.execute(
            "SELECT session_id FROM reconcile_sessions "
            "WHERE project_id = ? AND status = 'active' LIMIT 1",
            (project_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return
    if not row:
        return
    try:
        from . import reconcile_session  # local import — circular safe

        reconcile_session.transition_to_finalizing(conn, project_id, row[0])
    except Exception as exc:
        log.debug(
            "reconcile_deferred_queue: finalize hook skipped: %s", exc,
        )


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
    if deltas_changed:
        # Per §4.6.3: deltas-change detection re-queues with retry_count = 0.
        c.execute(
            "UPDATE reconcile_deferred_clusters SET "
            "  payload_json = ?, payload_sha256 = ?, run_id = ?, "
            "  status = 'queued', retry_count = 0, last_seen_at = ?, "
            "  expires_at = ?, next_retry_at = NULL "
            "WHERE project_id = ? AND cluster_fingerprint = ?",
            (blob, sha, run_id, now, expires, project_id, cluster_fingerprint),
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
    return out


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
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """Return up to batch_size queued rows ordered by priority+age.

    Side effect: promotes any rows past TTL to 'expired' before fetching.
    """
    c = _get_conn(conn, project_id)
    expire_stale_rows(project_id, conn=c)
    rows = c.execute(
        "SELECT * FROM reconcile_deferred_clusters "
        "WHERE project_id = ? AND status = 'queued' "
        "ORDER BY priority ASC, first_seen_at ASC LIMIT ?",
        (project_id, max(1, int(batch_size or 1))),
    ).fetchall()
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
    return (cur.rowcount or 0) > 0


def mark_terminal(
    project_id: str,
    cluster_fingerprint: str,
    terminal_status: str,
    reason: str = "",
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """{any active} -> terminal transition.

    terminal_status must be one of: resolved, failed_terminal, skipped, expired,
    or failed_retryable (for partial-failure path; not strictly terminal but
    bookkeeping-wise treated as a non-active rest state).
    """
    if terminal_status not in (
        "resolved",
        "failed_retryable",
        "failed_terminal",
        "skipped",
        "expired",
    ):
        raise ValueError(
            f"reconcile_deferred_queue.mark_terminal: invalid status {terminal_status!r}"
        )
    c = _get_conn(conn, project_id)
    now = _utc_iso()
    skipped_reason = reason if terminal_status == "skipped" else None
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
    if changed and terminal_status in TERMINAL_STATUSES:
        # R5 — finalize session when no in-flight rows remain.
        _maybe_finalize_session(c, project_id)
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
