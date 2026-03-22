"""Role service — Principal + Session model with heartbeat and auth.

Dual-write: SQLite (truth) + Redis (cache).
Auth flow: X-Gov-Token header → token_hash → session lookup → role extraction.
"""

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timezone, timedelta

from .enums import Role, SessionStatus
from .errors import (
    AuthError, TokenExpiredError, TokenInvalidError,
    DuplicateRoleError, ValidationError,
)
from .redis_client import get_redis
from . import audit_service

# Default timeouts (overridable via env)
import os
SESSION_TTL_HOURS = int(os.environ.get("GOVERNANCE_SESSION_TTL_HOURS", "24"))
HEARTBEAT_INTERVAL_SEC = int(os.environ.get("GOVERNANCE_HEARTBEAT_INTERVAL_SEC", "60"))
STALE_TIMEOUT_SEC = int(os.environ.get("GOVERNANCE_STALE_TIMEOUT_SEC", "180"))
EXPIRE_TIMEOUT_SEC = int(os.environ.get("GOVERNANCE_EXPIRE_TIMEOUT_SEC", "600"))


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _generate_token() -> str:
    return "gov-" + secrets.token_hex(32)


def register(
    conn: sqlite3.Connection,
    principal_id: str,
    project_id: str,
    role: str,
    scope: list[str] = None,
    metadata: dict = None,
    admin_secret: str = None,
) -> dict:
    """Register a new session for a principal.

    Returns session info including the plaintext token (only returned once).
    """
    # Validate role
    role_enum = Role.from_str(role)

    # Coordinator requires admin_secret
    if role_enum == Role.COORDINATOR:
        expected = os.environ.get("GOVERNANCE_ADMIN_SECRET", "")
        if not expected or admin_secret != expected:
            raise AuthError("Coordinator registration requires valid admin_secret", "auth_required")

    # Check for duplicate active session with different role in same project
    existing = conn.execute(
        "SELECT session_id, role FROM sessions WHERE principal_id = ? AND project_id = ? AND status = 'active'",
        (principal_id, project_id),
    ).fetchone()
    if existing and existing["role"] != role_enum.value:
        raise DuplicateRoleError(principal_id, existing["role"])

    # If same principal+project+role already active, return existing
    if existing and existing["role"] == role_enum.value:
        # Refresh token for existing session
        token = _generate_token()
        token_hash = _hash_token(token)
        now = _utc_iso()
        expires = (datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "UPDATE sessions SET token_hash = ?, expires_at = ?, last_heartbeat = ? WHERE session_id = ?",
            (token_hash, expires, now, existing["session_id"]),
        )
        # Update Redis cache
        rc = get_redis()
        session_data = dict(conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (existing["session_id"],)
        ).fetchone())
        rc.cache_session(existing["session_id"], session_data, SESSION_TTL_HOURS * 3600)
        rc.cache_token_session(token_hash, existing["session_id"], SESSION_TTL_HOURS * 3600)

        return {
            "session_id": existing["session_id"],
            "principal_id": principal_id,
            "project_id": project_id,
            "role": role_enum.value,
            "token": token,
            "refreshed": True,
        }

    # Create new session
    token = _generate_token()
    token_hash = _hash_token(token)
    now = _utc_iso()
    expires = (datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    import uuid, time
    session_id = f"ses-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}"

    conn.execute(
        """INSERT INTO sessions
           (session_id, principal_id, project_id, role, scope_json, token_hash,
            status, created_at, expires_at, last_heartbeat, metadata_json)
           VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
        (
            session_id, principal_id, project_id, role_enum.value,
            json.dumps(scope or []), token_hash,
            now, expires, now,
            json.dumps(metadata or {}),
        ),
    )

    # Audit
    audit_service.record(
        conn, project_id, "role.registered",
        actor=principal_id, node_ids=[],
        session_id=session_id, role=role_enum.value,
    )

    # Redis cache
    rc = get_redis()
    session_data = {
        "session_id": session_id,
        "principal_id": principal_id,
        "project_id": project_id,
        "role": role_enum.value,
        "scope_json": json.dumps(scope or []),
        "status": "active",
        "created_at": now,
        "expires_at": expires,
        "last_heartbeat": now,
    }
    rc.cache_session(session_id, session_data, SESSION_TTL_HOURS * 3600)
    rc.cache_token_session(token_hash, session_id, SESSION_TTL_HOURS * 3600)

    return {
        "session_id": session_id,
        "principal_id": principal_id,
        "project_id": project_id,
        "role": role_enum.value,
        "scope": scope or [],
        "token": token,
        "expires_at": expires,
        "permissions": _summarize_permissions(role_enum),
    }


def authenticate(conn: sqlite3.Connection, token: str) -> dict:
    """Authenticate a request by token. Returns session dict.

    Read path: Redis hit → return / Redis miss → SQLite → backfill Redis.
    """
    if not token:
        raise AuthError("X-Gov-Token header required")

    token_hash = _hash_token(token)
    rc = get_redis()

    # Try Redis first
    session_id = rc.get_session_by_token(token_hash)
    if session_id:
        cached = rc.get_cached_session(session_id)
        if cached and cached.get("status") == "active":
            # Check expiry
            if cached.get("expires_at", "") < _utc_iso():
                _expire_session(conn, session_id)
                raise TokenExpiredError()
            cached["scope"] = json.loads(cached.get("scope_json", "[]")) if isinstance(cached.get("scope_json"), str) else cached.get("scope", [])
            return cached

    # Fallback to SQLite
    row = conn.execute(
        "SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)
    ).fetchone()

    if row is None:
        raise TokenInvalidError()

    session = dict(row)
    if session["status"] != "active":
        raise TokenExpiredError()

    if session.get("expires_at", "") < _utc_iso():
        _expire_session(conn, session["session_id"])
        raise TokenExpiredError()

    # Parse scope
    try:
        session["scope"] = json.loads(session.get("scope_json", "[]"))
    except (json.JSONDecodeError, TypeError):
        session["scope"] = []

    # Backfill Redis
    rc.cache_session(session["session_id"], session, SESSION_TTL_HOURS * 3600)
    rc.cache_token_session(token_hash, session["session_id"], SESSION_TTL_HOURS * 3600)

    return session


def heartbeat(conn: sqlite3.Connection, session_id: str, status: str = "idle", current_task: str = None) -> dict:
    """Update session heartbeat."""
    now = _utc_iso()
    conn.execute(
        "UPDATE sessions SET last_heartbeat = ? WHERE session_id = ? AND status = 'active'",
        (now, session_id),
    )
    rc = get_redis()
    rc.update_heartbeat(session_id, EXPIRE_TIMEOUT_SEC)

    next_before = (datetime.now(timezone.utc) + timedelta(seconds=HEARTBEAT_INTERVAL_SEC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "session_id": session_id,
        "status": "active",
        "next_heartbeat_before": next_before,
        "server_time": now,
    }


def deregister(conn: sqlite3.Connection, session_id: str) -> dict:
    """Explicitly deregister a session."""
    conn.execute(
        "UPDATE sessions SET status = 'deregistered' WHERE session_id = ?",
        (session_id,),
    )
    rc = get_redis()
    rc.invalidate_session(session_id)
    return {"session_id": session_id, "status": "deregistered"}


def list_sessions(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    """List all active sessions for a project."""
    rows = conn.execute(
        "SELECT session_id, principal_id, role, status, last_heartbeat, created_at "
        "FROM sessions WHERE project_id = ? AND status IN ('active', 'stale') ORDER BY created_at",
        (project_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_active_roles(conn: sqlite3.Connection, project_id: str) -> dict[str, list[str]]:
    """Get active roles and their principals."""
    rows = conn.execute(
        "SELECT role, principal_id FROM sessions WHERE project_id = ? AND status = 'active'",
        (project_id,),
    ).fetchall()
    roles = {}
    for r in rows:
        roles.setdefault(r["role"], []).append(r["principal_id"])
    return roles


def check_role_available(conn: sqlite3.Connection, project_id: str, role: str) -> bool:
    """Check if at least one active session exists for the given role."""
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM sessions WHERE project_id = ? AND role = ? AND status = 'active'",
        (project_id, role),
    ).fetchone()
    return row["cnt"] > 0


def cleanup_expired(conn: sqlite3.Connection, project_id: str) -> int:
    """Mark expired/stale sessions. Returns count of affected sessions."""
    now = _utc_iso()
    # Expire sessions past their expiry time
    cursor = conn.execute(
        "UPDATE sessions SET status = 'expired' WHERE project_id = ? AND status IN ('active', 'stale') AND expires_at < ?",
        (project_id, now),
    )
    count = cursor.rowcount

    # Mark stale sessions (heartbeat too old but not expired)
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=STALE_TIMEOUT_SEC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor2 = conn.execute(
        "UPDATE sessions SET status = 'stale' WHERE project_id = ? AND status = 'active' AND last_heartbeat < ?",
        (project_id, stale_cutoff),
    )
    count += cursor2.rowcount
    return count


def _expire_session(conn: sqlite3.Connection, session_id: str):
    conn.execute("UPDATE sessions SET status = 'expired' WHERE session_id = ?", (session_id,))
    get_redis().invalidate_session(session_id)


def _summarize_permissions(role: Role) -> dict:
    """Build a permissions summary for the given role."""
    from .permissions import TRANSITION_RULES
    allowed = []
    for (from_s, to_s), roles in TRANSITION_RULES.items():
        if role in roles:
            allowed.append({"from": from_s.value, "to": to_s.value})
    return {"allowed_transitions": allowed}
