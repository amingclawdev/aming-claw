"""Audit service — append-only event log + SQLite query index.

Layer 3: Every operation produces an immutable audit record.
  - Raw events appended to daily JSONL files
  - Query index maintained in SQLite for filtering/search
"""

import json
import uuid
import time
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from .db import _governance_root


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gen_event_id() -> str:
    ts = int(time.time() * 1000)
    short = uuid.uuid4().hex[:6]
    return f"aud-{ts}-{short}"


def _audit_dir(project_id: str) -> Path:
    d = _governance_root() / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _audit_file(project_id: str) -> Path:
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    return _audit_dir(project_id) / f"audit-{date_str}.jsonl"


def record(
    conn: sqlite3.Connection,
    project_id: str,
    event: str,
    actor: str = "",
    ok: bool = True,
    node_ids: list[str] = None,
    request_id: str = "",
    **kwargs,
) -> dict:
    """Record an audit event.

    Writes to both JSONL file (immutable) and SQLite index (queryable).

    Returns the audit entry dict.
    """
    event_id = _gen_event_id()
    ts = _utc_iso()

    entry = {
        "event_id": event_id,
        "project_id": project_id,
        "event": event,
        "actor": actor,
        "ok": ok,
        "ts": ts,
        "node_ids": node_ids or [],
        "request_id": request_id,
    }
    entry.update(kwargs)

    # 1. Append to JSONL (immutable log)
    audit_path = _audit_file(project_id)
    with open(str(audit_path), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # 2. Insert into SQLite index
    conn.execute(
        """INSERT INTO audit_index
           (event_id, project_id, event, actor, ok, ts, node_ids)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            project_id,
            event,
            actor,
            1 if ok else 0,
            ts,
            json.dumps(node_ids or []),
        ),
    )

    return entry


def read_log(
    conn: sqlite3.Connection,
    project_id: str,
    limit: int = 100,
    offset: int = 0,
    event_filter: str = None,
    since: str = None,
    until: str = None,
) -> list[dict]:
    """Query audit log from SQLite index.

    Args:
        project_id: Project to query.
        limit: Max entries to return.
        offset: Skip first N entries.
        event_filter: Filter by event type (e.g., "verify_update").
        since: ISO timestamp, only events after this.
        until: ISO timestamp, only events before this.

    Returns:
        List of audit entry dicts (most recent first).
    """
    conditions = ["project_id = ?"]
    params: list = [project_id]

    if event_filter:
        conditions.append("event = ?")
        params.append(event_filter)
    if since:
        conditions.append("ts >= ?")
        params.append(since)
    if until:
        conditions.append("ts <= ?")
        params.append(until)

    where = " AND ".join(conditions)
    params.extend([limit, offset])

    rows = conn.execute(
        f"SELECT * FROM audit_index WHERE {where} ORDER BY ts DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()

    results = []
    for row in rows:
        entry = dict(row)
        if entry.get("node_ids"):
            try:
                entry["node_ids"] = json.loads(entry["node_ids"])
            except (json.JSONDecodeError, TypeError):
                pass
        entry["ok"] = bool(entry.get("ok", 1))
        results.append(entry)

    return results


def read_violations(
    conn: sqlite3.Connection,
    project_id: str,
    limit: int = 100,
    since: str = None,
) -> list[dict]:
    """Query audit violations (ok=false) from SQLite index."""
    return read_log(
        conn, project_id, limit=limit,
        event_filter=None, since=since,
    )  # We need to filter ok=false explicitly

    # Actually, let's do it properly:


def read_violations(
    conn: sqlite3.Connection,
    project_id: str,
    limit: int = 100,
    since: str = None,
) -> list[dict]:
    """Query audit violations (ok=false) from SQLite index."""
    conditions = ["project_id = ?", "ok = 0"]
    params: list = [project_id]

    if since:
        conditions.append("ts >= ?")
        params.append(since)

    where = " AND ".join(conditions)
    params.append(limit)

    rows = conn.execute(
        f"SELECT * FROM audit_index WHERE {where} ORDER BY ts DESC LIMIT ?",
        params,
    ).fetchall()

    results = []
    for row in rows:
        entry = dict(row)
        if entry.get("node_ids"):
            try:
                entry["node_ids"] = json.loads(entry["node_ids"])
            except (json.JSONDecodeError, TypeError):
                pass
        entry["ok"] = False
        results.append(entry)

    return results
