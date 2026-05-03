"""Idempotency key management for write APIs.

All mutating endpoints support an Idempotency-Key header. If the same key
is sent twice, the second request returns the cached response without
re-executing the operation.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta

DEFAULT_TTL_HOURS = 24


def check_idempotency(conn: sqlite3.Connection, project_id: str, idem_key: str) -> dict | None:
    """Check if an idempotency key exists and is still valid.

    Returns cached response dict if found, None otherwise.
    """
    if not idem_key:
        return None

    row = conn.execute(
        "SELECT response_json, expires_at FROM idempotency_keys WHERE idem_key = ? AND project_id = ?",
        (idem_key, project_id),
    ).fetchone()

    if row is None:
        return None

    expires_at = row["expires_at"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if expires_at < now:
        # Expired — clean up and return None
        conn.execute("DELETE FROM idempotency_keys WHERE idem_key = ?", (idem_key,))
        return None

    return json.loads(row["response_json"])


def store_idempotency(
    conn: sqlite3.Connection,
    project_id: str,
    idem_key: str,
    response: dict,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> None:
    """Store an idempotency key with its response."""
    if not idem_key:
        return

    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=ttl_hours)

    conn.execute(
        """INSERT OR REPLACE INTO idempotency_keys
           (idem_key, project_id, response_json, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            idem_key,
            project_id,
            json.dumps(response, ensure_ascii=False),
            now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )


def cleanup_expired(conn: sqlite3.Connection) -> int:
    """Remove expired idempotency keys. Returns count of removed keys."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor = conn.execute(
        "DELETE FROM idempotency_keys WHERE expires_at < ?", (now,)
    )
    return cursor.rowcount
