"""Observer session registration and durable observer command queue.

Dashboard actions enqueue business payloads here. Hooks may remind an AI
observer that commands exist, but the command payload remains in governance DB
until a token-authenticated observer session claims it.
"""
from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Iterable


HEARTBEAT_INTERVAL_SEC = 30
IDLE_AFTER_SEC = HEARTBEAT_INTERVAL_SEC * 2
STALE_AFTER_SEC = HEARTBEAT_INTERVAL_SEC * 4
CLAIMED_TO_STARTUP_TIMEOUT_SEC = STALE_AFTER_SEC
CLAIMED_TO_STARTUP_TIMEOUT_STATUS = "claimed_to_startup_timeout"

SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_CLOSED = "closed"
SESSION_STATUS_REVOKED = "revoked"

COMMAND_STATUS_QUEUED = "queued"
COMMAND_STATUS_NOTIFIED = "notified"
COMMAND_STATUS_CLAIMED = "claimed"
COMMAND_STATUS_RUNNING = "running"
COMMAND_STATUS_COMPLETED = "completed"
COMMAND_STATUS_FAILED = "failed"
COMMAND_STATUS_CANCELLED = "cancelled"

CLAIMABLE_COMMAND_STATUSES = {COMMAND_STATUS_QUEUED, COMMAND_STATUS_NOTIFIED}
OWNED_COMMAND_STATUSES = {COMMAND_STATUS_CLAIMED, COMMAND_STATUS_RUNNING}
TERMINAL_COMMAND_STATUSES = {
    COMMAND_STATUS_COMPLETED,
    COMMAND_STATUS_FAILED,
    COMMAND_STATUS_CANCELLED,
}

COMMAND_TYPE_ANALYZE_REQUIREMENTS = "analyze_requirements"
COMMAND_TYPE_CONFIRM_REQUIREMENT = "confirm_requirement"
COMMAND_TYPE_MOVE_TO_EXECUTION_QUEUE = "move_to_execution_queue"
COMMAND_TYPE_EXECUTE_BACKLOG_ROW = "execute_backlog_row"
COMMAND_TYPE_PAUSE_WORKER = "pause_worker"
COMMAND_TYPE_CONTINUE_WORKER = "continue_worker"
COMMAND_TYPE_CANCEL_WORKER = "cancel_worker"

VALID_COMMAND_TYPES = {
    COMMAND_TYPE_ANALYZE_REQUIREMENTS,
    COMMAND_TYPE_CONFIRM_REQUIREMENT,
    COMMAND_TYPE_MOVE_TO_EXECUTION_QUEUE,
    COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
    COMMAND_TYPE_PAUSE_WORKER,
    COMMAND_TYPE_CONTINUE_WORKER,
    COMMAND_TYPE_CANCEL_WORKER,
}

EXECUTE_BACKLOG_ROW_REQUIRED_PAYLOAD_FIELDS = (
    "backlog_id",
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "visible_injection_manifest_hash",
)

ACTION_SESSION_HEARTBEAT = "observer_session_heartbeat"
ACTION_SESSION_CLOSE = "observer_session_close"
ACTION_SESSION_REVOKE = "observer_session_revoke"
ACTION_COMMAND_CLAIM = "observer_command_claim"
ACTION_COMMAND_TAKEOVER = "observer_command_takeover"
ACTION_COMMAND_COMPLETE = "observer_command_complete"
ACTION_COMMAND_FAIL = "observer_command_fail"

DEFAULT_CAPABILITIES = {
    "actions": [
        ACTION_SESSION_HEARTBEAT,
        ACTION_SESSION_CLOSE,
        ACTION_SESSION_REVOKE,
        ACTION_COMMAND_CLAIM,
        ACTION_COMMAND_TAKEOVER,
        ACTION_COMMAND_COMPLETE,
        ACTION_COMMAND_FAIL,
    ],
    "command_types": sorted(VALID_COMMAND_TYPES),
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS observer_sessions (
    session_id          TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    observer_kind       TEXT NOT NULL DEFAULT '',
    session_label       TEXT NOT NULL DEFAULT '',
    pid                 INTEGER NOT NULL DEFAULT 0,
    cwd                 TEXT NOT NULL DEFAULT '',
    capabilities_json   TEXT NOT NULL DEFAULT '{}',
    token_hash          TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL DEFAULT 'active',
    registered_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    closed_at           TEXT NOT NULL DEFAULT '',
    revoked_at          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_observer_sessions_project_status
    ON observer_sessions(project_id, status);
CREATE INDEX IF NOT EXISTS idx_observer_sessions_last_seen
    ON observer_sessions(project_id, last_seen_at);

CREATE TABLE IF NOT EXISTS observer_command_queue (
    command_id              TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL,
    command_type            TEXT NOT NULL,
    payload_json            TEXT NOT NULL DEFAULT '{}',
    status                  TEXT NOT NULL DEFAULT 'queued',
    target_session_id       TEXT NOT NULL DEFAULT '',
    claimed_by_session_id   TEXT NOT NULL DEFAULT '',
    created_by              TEXT NOT NULL DEFAULT '',
    created_at              TEXT NOT NULL,
    notified_at             TEXT NOT NULL DEFAULT '',
    claimed_at              TEXT NOT NULL DEFAULT '',
    completed_at            TEXT NOT NULL DEFAULT '',
    result_json             TEXT NOT NULL DEFAULT '{}',
    error                   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_observer_commands_project_status
    ON observer_command_queue(project_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_observer_commands_target
    ON observer_command_queue(project_id, target_session_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_observer_commands_claimed_by
    ON observer_command_queue(project_id, claimed_by_session_id, status);
"""


class ObserverSessionError(Exception):
    """Base error for observer session and command queue operations."""


class ObserverAuthError(ObserverSessionError):
    """Raised when a session token is missing or invalid."""


class ObserverPermissionError(ObserverSessionError):
    """Raised when a valid session is not allowed to perform an action."""


class ObserverCommandConflict(ObserverSessionError):
    """Raised when a command is no longer claimable by the caller."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _json_loads_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _startup_text(value: Any) -> str:
    return str(value or "").strip()


def _startup_fence_present(value: dict[str, Any]) -> bool:
    return bool(
        _startup_text(value.get("fence_token"))
        or _startup_text(value.get("worker_fence_token"))
        or _startup_text(value.get("actual_fence_token"))
        or _startup_text(value.get("fence_token_hash"))
        or value.get("fence_token_matches") is True
    )


def _startup_token_present(value: dict[str, Any]) -> bool:
    return bool(
        _startup_text(value.get("session_token_hash"))
        or _startup_text(value.get("session_token_surrogate"))
        or value.get("session_token_present") is True
    )


def _actual_startup_identity_present(value: dict[str, Any]) -> bool:
    actual_runtime = value.get("actual_runtime") if isinstance(value.get("actual_runtime"), dict) else {}
    actual_cwd = _startup_text(value.get("actual_cwd") or actual_runtime.get("cwd"))
    actual_git_root = _startup_text(
        value.get("actual_git_root") or actual_runtime.get("git_root")
    )
    branch = _startup_text(
        value.get("branch")
        or value.get("branch_ref")
        or actual_runtime.get("branch")
        or actual_runtime.get("branch_ref")
    )
    head_commit = _startup_text(
        value.get("head_commit")
        or value.get("branch_head")
        or actual_runtime.get("head_commit")
        or actual_runtime.get("branch_head")
    )
    return bool(
        (actual_cwd or actual_git_root)
        and branch
        and head_commit
        and _startup_fence_present(value)
        and _startup_token_present(value)
    )


def _contains_actual_startup_evidence(value: Any) -> bool:
    if isinstance(value, dict):
        lowered = {str(key).lower(): item for key, item in value.items()}
        if isinstance(lowered.get("startup_recording"), dict):
            startup_recording = lowered["startup_recording"]
            if (
                startup_recording.get("recorded") is True
                and _actual_startup_identity_present(startup_recording)
            ):
                return True

        event_kind = str(
            lowered.get("event_kind") or lowered.get("event_type") or ""
        ).strip()
        if event_kind == "mf_subagent_startup":
            if lowered.get("recorded") is False or lowered.get("actual_startup_recorded") is False:
                return False
            payload = lowered.get("payload") if isinstance(lowered.get("payload"), dict) else {}
            gate = (
                payload.get("mf_subagent_startup_gate")
                if isinstance(payload.get("mf_subagent_startup_gate"), dict)
                else {}
            )
            return _actual_startup_identity_present(gate) or _actual_startup_identity_present(lowered)

        if lowered.get("actual_startup_recorded") is True and _actual_startup_identity_present(lowered):
            return True

        return any(_contains_actual_startup_evidence(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_actual_startup_evidence(item) for item in value)
    return False


def _contains_terminal_dispatch_blocker(value: Any) -> bool:
    if isinstance(value, dict):
        lowered = {str(key).lower(): item for key, item in value.items()}
        for key in ("dispatch_blocker", "terminal_dispatch_blocker"):
            if key in lowered and bool(lowered[key]):
                return True

        gate = lowered.get("dispatch_gate_validation")
        if isinstance(gate, dict) and gate.get("allowed") is False:
            if gate.get("error") or gate.get("status") or gate.get("blocker"):
                return True

        event_kind = str(
            lowered.get("event_kind") or lowered.get("event_type") or ""
        ).strip()
        if event_kind in {"dispatch_blocker", "terminal_dispatch_blocker"}:
            return True

        return any(_contains_terminal_dispatch_blocker(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_terminal_dispatch_blocker(item) for item in value)
    return False


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def hash_session_token(session_token: str) -> str:
    token = (session_token or "").strip()
    if not token:
        raise ValueError("session_token is required")
    return "sha256:" + sha256(token.encode("utf-8")).hexdigest()


def _normalize_capabilities(capabilities: Any) -> dict[str, Any]:
    if capabilities is None:
        return dict(DEFAULT_CAPABILITIES)
    if isinstance(capabilities, list):
        return {
            "actions": [str(item) for item in capabilities],
            "command_types": list(DEFAULT_CAPABILITIES["command_types"]),
        }
    if not isinstance(capabilities, dict):
        return dict(DEFAULT_CAPABILITIES)

    normalized = dict(capabilities)
    if "actions" not in normalized:
        normalized["actions"] = list(DEFAULT_CAPABILITIES["actions"])
    if "command_types" not in normalized:
        normalized["command_types"] = list(DEFAULT_CAPABILITIES["command_types"])
    return normalized


def _list_allows(values: Iterable[Any], required: str) -> bool:
    value_set = {str(item) for item in values}
    return "*" in value_set or required in value_set


def capabilities_allow(
    capabilities: dict[str, Any],
    action: str,
    *,
    command_type: str | None = None,
) -> bool:
    actions = capabilities.get("actions")
    if not isinstance(actions, list) or not _list_allows(actions, action):
        return False
    if command_type:
        command_types = capabilities.get("command_types")
        if not isinstance(command_types, list):
            return False
        return _list_allows(command_types, command_type)
    return True


def _session_row_to_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = {key: row[key] for key in row.keys()} if isinstance(row, sqlite3.Row) else dict(row)
    data["capabilities"] = _json_loads_object(data.get("capabilities_json"))
    data.pop("token_hash", None)
    return data


def _command_row_to_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = {key: row[key] for key in row.keys()} if isinstance(row, sqlite3.Row) else dict(row)
    data["payload"] = _json_loads_object(data.get("payload_json"))
    data["result"] = _json_loads_object(data.get("result_json"))
    return data


def _command_result(command: dict[str, Any]) -> dict[str, Any]:
    result = command.get("result")
    if isinstance(result, dict):
        return dict(result)
    return _json_loads_object(str(command.get("result_json") or "{}"))


def _command_has_startup_or_blocker_evidence(command: dict[str, Any]) -> bool:
    result = _command_result(command)
    if _result_has_startup_or_blocker_evidence(result):
        return True
    error = str(command.get("error") or "").lower()
    return "dispatch_blocker" in error or "dispatch blocker" in error


def _result_has_startup_or_blocker_evidence(result: dict[str, Any]) -> bool:
    if _contains_actual_startup_evidence(result):
        return True
    if _contains_terminal_dispatch_blocker(result):
        return True
    return False


def _missing_startup_surface_blocker(command: dict[str, Any], *, now: str) -> dict[str, Any]:
    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
    return {
        "blocker_id": "no_truthful_bounded_mf_sub_startup_surface_available",
        "dispatch_blocker": True,
        "terminal_dispatch_blocker": True,
        "status": "blocked",
        "observer_command_id": str(command.get("command_id") or ""),
        "backlog_id": str(payload.get("backlog_id") or ""),
        "route_id": str(payload.get("route_id") or ""),
        "route_context_hash": str(payload.get("route_context_hash") or ""),
        "prompt_contract_id": str(payload.get("prompt_contract_id") or ""),
        "visible_injection_manifest_hash": str(
            payload.get("visible_injection_manifest_hash") or ""
        ),
        "claimed_at": str(command.get("claimed_at") or ""),
        "failed_at": now,
        "reason": (
            "execute_backlog_row completion requires actual bounded mf_sub startup "
            "evidence or an explicit terminal dispatch blocker; branch allocation "
            "and runtime-text startup intent are not startup"
        ),
    }


def _command_claim_age_sec(command: dict[str, Any], *, now: str) -> float | None:
    claimed_at = _parse_utc(str(command.get("claimed_at") or ""))
    now_dt = _parse_utc(now)
    if not claimed_at or not now_dt:
        return None
    return max(0.0, (now_dt - claimed_at).total_seconds())


def _claimed_execute_startup_timeout_status(
    command: dict[str, Any] | None,
    *,
    now: str,
) -> str:
    if not command:
        return ""
    if str(command.get("command_type") or "") != COMMAND_TYPE_EXECUTE_BACKLOG_ROW:
        return ""
    if str(command.get("status") or "") not in OWNED_COMMAND_STATUSES:
        return ""
    if _command_has_startup_or_blocker_evidence(command):
        return ""
    age = _command_claim_age_sec(command, now=now)
    if age is None or age < CLAIMED_TO_STARTUP_TIMEOUT_SEC:
        return ""
    return CLAIMED_TO_STARTUP_TIMEOUT_STATUS


def _merge_result_with_durable_takeover(
    command: dict[str, Any],
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    incoming = dict(result or {})
    existing = _command_result(command)
    for key in ("takeover", "takeover_status"):
        if key not in incoming and key in existing:
            incoming[key] = existing[key]
    return incoming


def _validate_command_payload(command_type: str, payload: Any) -> None:
    if command_type != COMMAND_TYPE_EXECUTE_BACKLOG_ROW:
        return
    if not isinstance(payload, dict):
        missing = ", ".join(EXECUTE_BACKLOG_ROW_REQUIRED_PAYLOAD_FIELDS)
        raise ValueError(
            "execute_backlog_row payload must be an object with required fields: "
            + missing
        )
    missing = [
        field
        for field in EXECUTE_BACKLOG_ROW_REQUIRED_PAYLOAD_FIELDS
        if not str(payload.get(field) or "").strip()
    ]
    if missing:
        raise ValueError(
            "execute_backlog_row payload missing required fields: "
            + ", ".join(missing)
        )


def computed_session_status(
    session: sqlite3.Row | dict[str, Any],
    *,
    now: str | None = None,
) -> str:
    status = str(session["status"] if isinstance(session, sqlite3.Row) else session.get("status") or "")
    if status == SESSION_STATUS_REVOKED:
        return SESSION_STATUS_REVOKED
    if status == SESSION_STATUS_CLOSED:
        return SESSION_STATUS_CLOSED

    now_dt = _parse_utc(now or _utc_now()) or datetime.now(timezone.utc)
    last_seen_raw = session["last_seen_at"] if isinstance(session, sqlite3.Row) else session.get("last_seen_at")
    last_seen = _parse_utc(str(last_seen_raw or ""))
    if not last_seen:
        return "stale"
    age = max(0.0, (now_dt - last_seen).total_seconds())
    if age >= STALE_AFTER_SEC:
        return "stale"
    if age >= IDLE_AFTER_SEC:
        return "idle"
    return SESSION_STATUS_ACTIVE


def register_session(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    observer_kind: str = "codex",
    session_label: str = "",
    pid: int | None = None,
    cwd: str = "",
    capabilities: dict[str, Any] | list[Any] | None = None,
    session_id: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    pid_value = (project_id or "").strip()
    if not pid_value:
        raise ValueError("project_id is required")

    sid = (session_id or "").strip() or f"obs-{uuid.uuid4().hex[:12]}"
    token = secrets.token_urlsafe(32)
    token_hash = hash_session_token(token)
    registered_at = now or _utc_now()
    caps = _normalize_capabilities(capabilities)

    conn.execute(
        """
        INSERT INTO observer_sessions (
            session_id, project_id, observer_kind, session_label, pid, cwd,
            capabilities_json, token_hash, status, registered_at, last_seen_at,
            closed_at, revoked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '')
        """,
        (
            sid,
            pid_value,
            (observer_kind or "").strip(),
            (session_label or "").strip(),
            int(pid or 0),
            (cwd or "").strip(),
            _json_dumps(caps),
            token_hash,
            SESSION_STATUS_ACTIVE,
            registered_at,
            registered_at,
        ),
    )
    conn.commit()

    row = conn.execute("SELECT * FROM observer_sessions WHERE session_id = ?", (sid,)).fetchone()
    session = _session_row_to_dict(row)
    session["computed_status"] = computed_session_status(row, now=registered_at)
    return {
        "ok": True,
        "observer_session_id": sid,
        "session_id": sid,
        "session_token": token,
        "heartbeat_interval_sec": HEARTBEAT_INTERVAL_SEC,
        "session": session,
    }


def get_session(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    session_id: str,
    now: str | None = None,
) -> dict[str, Any] | None:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM observer_sessions WHERE project_id = ? AND session_id = ?",
        ((project_id or "").strip(), (session_id or "").strip()),
    ).fetchone()
    if not row:
        return None
    data = _session_row_to_dict(row)
    data["computed_status"] = computed_session_status(row, now=now)
    return data


def _raw_session_row(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM observer_sessions WHERE session_id = ?",
        ((session_id or "").strip(),),
    ).fetchone()


def authenticate_session(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    session_id: str,
    session_token: str,
    action: str,
    command_type: str | None = None,
    allow_stale: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    pid = (project_id or "").strip()
    sid = (session_id or "").strip()
    token = (session_token or "").strip()
    if not sid or not token:
        raise ObserverAuthError("session_id and session_token are required")

    row = _raw_session_row(conn, sid)
    if row is None:
        raise ObserverAuthError("observer session not found")
    if str(row["project_id"]) != pid:
        raise ObserverPermissionError("observer session belongs to a different project")

    expected = str(row["token_hash"] or "")
    try:
        actual = hash_session_token(token)
    except ValueError as exc:
        raise ObserverAuthError(str(exc)) from exc
    if not hmac.compare_digest(expected, actual):
        raise ObserverAuthError("invalid observer session token")

    computed = computed_session_status(row, now=now)
    if computed in {SESSION_STATUS_REVOKED, SESSION_STATUS_CLOSED}:
        raise ObserverPermissionError(f"observer session is {computed}")
    if computed == "stale" and not allow_stale:
        raise ObserverPermissionError("observer session is stale")

    capabilities = _json_loads_object(row["capabilities_json"])
    if not capabilities_allow(capabilities, action, command_type=command_type):
        raise ObserverPermissionError("observer session lacks required capability")

    data = _session_row_to_dict(row)
    data["computed_status"] = computed
    return data


def heartbeat_session(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    session_id: str,
    session_token: str,
    now: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    timestamp = now or _utc_now()
    authenticate_session(
        conn,
        project_id=project_id,
        session_id=session_id,
        session_token=session_token,
        action=ACTION_SESSION_HEARTBEAT,
        allow_stale=True,
        now=timestamp,
    )
    conn.execute(
        "UPDATE observer_sessions SET last_seen_at = ?, status = ? WHERE project_id = ? AND session_id = ?",
        (timestamp, SESSION_STATUS_ACTIVE, (project_id or "").strip(), (session_id or "").strip()),
    )
    conn.commit()
    session = get_session(conn, project_id=project_id, session_id=session_id, now=timestamp)
    return {
        "ok": True,
        "project_id": (project_id or "").strip(),
        "observer_session_id": (session_id or "").strip(),
        "heartbeat_interval_sec": HEARTBEAT_INTERVAL_SEC,
        "session": session,
    }


def close_session(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    session_id: str,
    session_token: str,
    now: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    timestamp = now or _utc_now()
    authenticate_session(
        conn,
        project_id=project_id,
        session_id=session_id,
        session_token=session_token,
        action=ACTION_SESSION_CLOSE,
        allow_stale=True,
        now=timestamp,
    )
    conn.execute(
        """UPDATE observer_sessions
              SET status = ?, closed_at = ?, last_seen_at = ?
            WHERE project_id = ? AND session_id = ?""",
        (SESSION_STATUS_CLOSED, timestamp, timestamp, (project_id or "").strip(), (session_id or "").strip()),
    )
    conn.commit()
    return {
        "ok": True,
        "project_id": (project_id or "").strip(),
        "observer_session_id": (session_id or "").strip(),
        "status": SESSION_STATUS_CLOSED,
    }


def revoke_session(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    session_id: str,
    session_token: str,
    now: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    timestamp = now or _utc_now()
    authenticate_session(
        conn,
        project_id=project_id,
        session_id=session_id,
        session_token=session_token,
        action=ACTION_SESSION_REVOKE,
        allow_stale=True,
        now=timestamp,
    )
    conn.execute(
        """UPDATE observer_sessions
              SET status = ?, revoked_at = ?, last_seen_at = ?
            WHERE project_id = ? AND session_id = ?""",
        (SESSION_STATUS_REVOKED, timestamp, timestamp, (project_id or "").strip(), (session_id or "").strip()),
    )
    conn.commit()
    return {
        "ok": True,
        "project_id": (project_id or "").strip(),
        "observer_session_id": (session_id or "").strip(),
        "status": SESSION_STATUS_REVOKED,
    }


def list_sessions(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    limit: int = 100,
    now: str | None = None,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT * FROM observer_sessions
            WHERE project_id = ?
            ORDER BY registered_at DESC
            LIMIT ?""",
        ((project_id or "").strip(), max(1, min(int(limit or 100), 1000))),
    ).fetchall()
    result = []
    for row in rows:
        item = _session_row_to_dict(row)
        item["computed_status"] = computed_session_status(row, now=now)
        result.append(item)
    return result


def connection_summary(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    now: str | None = None,
) -> dict[str, Any]:
    sessions = list_sessions(conn, project_id=project_id, limit=100, now=now)
    connected = [s for s in sessions if s.get("computed_status") in {SESSION_STATUS_ACTIVE, "idle"}]
    active = [s for s in sessions if s.get("computed_status") == SESSION_STATUS_ACTIVE]
    return {
        "connected": bool(connected),
        "connected_count": len(connected),
        "active_count": len(active),
        "stale_count": len([s for s in sessions if s.get("computed_status") == "stale"]),
        "sessions": sessions,
        "heartbeat_interval_sec": HEARTBEAT_INTERVAL_SEC,
    }


def enqueue_command(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    command_type: str,
    payload: dict[str, Any] | None = None,
    target_session_id: str = "",
    created_by: str = "",
    command_id: str | None = None,
    notify: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    pid = (project_id or "").strip()
    ctype = (command_type or "").strip()
    if not pid:
        raise ValueError("project_id is required")
    if ctype not in VALID_COMMAND_TYPES:
        raise ValueError(f"invalid command_type: {ctype!r}")
    _validate_command_payload(ctype, payload)

    target = (target_session_id or "").strip()
    if target:
        row = _raw_session_row(conn, target)
        if row is None or str(row["project_id"]) != pid:
            raise ValueError("target_session_id is not registered for this project")

    timestamp = now or _utc_now()
    cid = (command_id or "").strip() or f"cmd-{uuid.uuid4().hex[:12]}"
    status = COMMAND_STATUS_NOTIFIED if notify else COMMAND_STATUS_QUEUED
    notified_at = timestamp if notify else ""
    conn.execute(
        """
        INSERT INTO observer_command_queue (
            command_id, project_id, command_type, payload_json, status,
            target_session_id, claimed_by_session_id, created_by, created_at,
            notified_at, claimed_at, completed_at, result_json, error
        ) VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, '', '', '{}', '')
        """,
        (
            cid,
            pid,
            ctype,
            _json_dumps(payload or {}),
            status,
            target,
            (created_by or "").strip(),
            timestamp,
            notified_at,
        ),
    )
    conn.commit()
    return get_command(conn, project_id=pid, command_id=cid)  # type: ignore[return-value]


def get_command(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    command_id: str,
) -> dict[str, Any] | None:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM observer_command_queue WHERE project_id = ? AND command_id = ?",
        ((project_id or "").strip(), (command_id or "").strip()),
    ).fetchone()
    return _command_row_to_dict(row) if row else None


def list_commands(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    status: str | Iterable[str] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    pid = (project_id or "").strip()
    params: list[Any] = [pid]
    where = ["project_id = ?"]
    if status:
        statuses = [status] if isinstance(status, str) else [s for s in status if s]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where.append(f"status IN ({placeholders})")
            params.extend(statuses)
    params.append(max(1, min(int(limit or 100), 1000)))
    rows = conn.execute(
        "SELECT * FROM observer_command_queue WHERE "
        + " AND ".join(where)
        + " ORDER BY created_at DESC LIMIT ?",
        params,
    ).fetchall()
    return [_command_row_to_dict(row) for row in rows]


def command_summary(conn: sqlite3.Connection, *, project_id: str, limit: int = 50) -> dict[str, Any]:
    commands = list_commands(conn, project_id=project_id, limit=limit)
    counts = {status: 0 for status in [
        COMMAND_STATUS_QUEUED,
        COMMAND_STATUS_NOTIFIED,
        COMMAND_STATUS_CLAIMED,
        COMMAND_STATUS_RUNNING,
        COMMAND_STATUS_COMPLETED,
        COMMAND_STATUS_FAILED,
        COMMAND_STATUS_CANCELLED,
    ]}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM observer_command_queue WHERE project_id = ? GROUP BY status",
        ((project_id or "").strip(),),
    ).fetchall():
        key = row["status"] if isinstance(row, sqlite3.Row) else row[0]
        value = row["n"] if isinstance(row, sqlite3.Row) else row[1]
        counts[str(key)] = int(value)
    return {
        "count": sum(counts.values()),
        "counts": counts,
        "items": commands,
    }


def _command_target_allows(command: dict[str, Any], session_id: str) -> bool:
    target = str(command.get("target_session_id") or "")
    return not target or target == session_id


def _command_target_allows_takeover(
    command: dict[str, Any],
    *,
    session_id: str,
    previous_session_id: str,
) -> bool:
    target = str(command.get("target_session_id") or "")
    return not target or target in {session_id, previous_session_id}


def _find_next_claimable_command(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    session: dict[str, Any],
) -> dict[str, Any] | None:
    session_id = str(session["session_id"])
    capabilities = session.get("capabilities") if isinstance(session.get("capabilities"), dict) else {}
    rows = conn.execute(
        """SELECT * FROM observer_command_queue
            WHERE project_id = ?
              AND status IN (?, ?)
              AND (target_session_id = '' OR target_session_id = ?)
            ORDER BY created_at ASC
            LIMIT 50""",
        (
            (project_id or "").strip(),
            COMMAND_STATUS_QUEUED,
            COMMAND_STATUS_NOTIFIED,
            session_id,
        ),
    ).fetchall()
    for row in rows:
        command = _command_row_to_dict(row)
        if capabilities_allow(
            capabilities,
            ACTION_COMMAND_CLAIM,
            command_type=str(command.get("command_type") or ""),
        ):
            return command
    return None


def claim_command(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    session_id: str,
    session_token: str,
    command_id: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    pid = (project_id or "").strip()
    sid = (session_id or "").strip()
    timestamp = now or _utc_now()

    # A reconnecting observer gets its in-flight command back instead of
    # double-claiming a fresh one.
    if not command_id:
        owned = conn.execute(
            """SELECT * FROM observer_command_queue
                WHERE project_id = ?
                  AND claimed_by_session_id = ?
                  AND status IN (?, ?)
                ORDER BY claimed_at ASC
                LIMIT 1""",
            (pid, sid, COMMAND_STATUS_CLAIMED, COMMAND_STATUS_RUNNING),
        ).fetchone()
        if owned:
            command = _command_row_to_dict(owned)
            authenticate_session(
                conn,
                project_id=pid,
                session_id=sid,
                session_token=session_token,
                action=ACTION_COMMAND_CLAIM,
                command_type=str(command.get("command_type") or ""),
                now=timestamp,
            )
            return command

    if command_id:
        row = conn.execute(
            "SELECT * FROM observer_command_queue WHERE project_id = ? AND command_id = ?",
            (pid, (command_id or "").strip()),
        ).fetchone()
        if not row:
            raise LookupError("observer command not found")
        command = _command_row_to_dict(row)
        session = authenticate_session(
            conn,
            project_id=pid,
            session_id=sid,
            session_token=session_token,
            action=ACTION_COMMAND_CLAIM,
            command_type=str(command.get("command_type") or ""),
            now=timestamp,
        )
    else:
        session = authenticate_session(
            conn,
            project_id=pid,
            session_id=sid,
            session_token=session_token,
            action=ACTION_COMMAND_CLAIM,
            now=timestamp,
        )
        command = _find_next_claimable_command(conn, project_id=pid, session=session)
        if not command:
            return {
                "ok": True,
                "project_id": pid,
                "observer_session_id": sid,
                "command": None,
                "empty": True,
            }

    if not _command_target_allows(command, sid):
        raise ObserverPermissionError("observer command targets a different session")

    if str(command.get("claimed_by_session_id") or "") == sid and command.get("status") in OWNED_COMMAND_STATUSES:
        return {
            "ok": True,
            "project_id": pid,
            "observer_session_id": sid,
            "command": command,
            "empty": False,
        }

    if command.get("status") in TERMINAL_COMMAND_STATUSES:
        raise ObserverCommandConflict("observer command is already terminal")
    if command.get("status") not in CLAIMABLE_COMMAND_STATUSES:
        raise ObserverCommandConflict("observer command is already claimed")

    cursor = conn.execute(
        """UPDATE observer_command_queue
              SET status = ?, claimed_by_session_id = ?, claimed_at = ?
            WHERE project_id = ?
              AND command_id = ?
              AND status IN (?, ?)
              AND (target_session_id = '' OR target_session_id = ?)""",
        (
            COMMAND_STATUS_CLAIMED,
            sid,
            timestamp,
            pid,
            command["command_id"],
            COMMAND_STATUS_QUEUED,
            COMMAND_STATUS_NOTIFIED,
            sid,
        ),
    )
    conn.commit()
    if cursor.rowcount != 1:
        raise ObserverCommandConflict("observer command was claimed by another session")

    return {
        "ok": True,
        "project_id": pid,
        "observer_session_id": sid,
        "command": get_command(conn, project_id=pid, command_id=command["command_id"]),
        "empty": False,
    }


def _owner_session_takeover_status(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    owner_session_id: str,
    now: str,
    command: dict[str, Any] | None = None,
) -> str:
    if not owner_session_id:
        return "missing"
    row = _raw_session_row(conn, owner_session_id)
    if row is None or str(row["project_id"]) != project_id:
        return "missing"
    owner_status = computed_session_status(row, now=now)
    if owner_status not in {"missing", "stale", SESSION_STATUS_CLOSED, SESSION_STATUS_REVOKED}:
        startup_timeout = _claimed_execute_startup_timeout_status(command, now=now)
        if startup_timeout:
            return startup_timeout
    return owner_status


def takeover_command(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    session_id: str,
    session_token: str,
    command_id: str,
    reason: str,
    now: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    pid = (project_id or "").strip()
    sid = (session_id or "").strip()
    cid = (command_id or "").strip()
    takeover_reason = (reason or "").strip()
    if not takeover_reason:
        raise ValueError("takeover reason is required")

    timestamp = now or _utc_now()
    command = get_command(conn, project_id=pid, command_id=cid)
    if not command:
        raise LookupError("observer command not found")
    authenticate_session(
        conn,
        project_id=pid,
        session_id=sid,
        session_token=session_token,
        action=ACTION_COMMAND_TAKEOVER,
        command_type=str(command.get("command_type") or ""),
        now=timestamp,
    )

    previous_session_id = str(command.get("claimed_by_session_id") or "")
    if command.get("status") in TERMINAL_COMMAND_STATUSES:
        raise ObserverCommandConflict("observer command is already terminal")
    if command.get("status") not in OWNED_COMMAND_STATUSES:
        raise ObserverCommandConflict("observer command is not claimed")
    if previous_session_id == sid:
        raise ObserverCommandConflict("observer command is already owned by this session")
    if not _command_target_allows_takeover(
        command,
        session_id=sid,
        previous_session_id=previous_session_id,
    ):
        raise ObserverPermissionError("observer command targets a different session")

    previous_status = _owner_session_takeover_status(
        conn,
        project_id=pid,
        owner_session_id=previous_session_id,
        now=timestamp,
        command=command,
    )
    takeover_allowed_statuses = {
        "missing",
        "stale",
        SESSION_STATUS_CLOSED,
        SESSION_STATUS_REVOKED,
        CLAIMED_TO_STARTUP_TIMEOUT_STATUS,
    }
    if previous_status not in takeover_allowed_statuses:
        raise ObserverCommandConflict(
            f"observer command owner is not stale: {previous_status}"
        )

    takeover = {
        "previous_session_id": previous_session_id,
        "previous_session_status": previous_status,
        "reason": takeover_reason,
        "taken_over_at": timestamp,
    }
    if previous_status == CLAIMED_TO_STARTUP_TIMEOUT_STATUS:
        takeover.update(
            {
                "claimed_at": str(command.get("claimed_at") or ""),
                "timeout_sec": CLAIMED_TO_STARTUP_TIMEOUT_SEC,
                "startup_evidence": "missing",
            }
        )
    result_payload = _command_result(command)
    result_payload["takeover"] = takeover
    result_payload["takeover_status"] = {
        "status": previous_status,
        "takeover_eligible": True,
        "taken_over_at": timestamp,
        "reason": takeover_reason,
    }

    cursor = conn.execute(
        """UPDATE observer_command_queue
              SET status = ?, claimed_by_session_id = ?, claimed_at = ?, result_json = ?
            WHERE project_id = ?
              AND command_id = ?
              AND status IN (?, ?)
              AND claimed_by_session_id = ?""",
        (
            COMMAND_STATUS_CLAIMED,
            sid,
            timestamp,
            _json_dumps(result_payload),
            pid,
            cid,
            COMMAND_STATUS_CLAIMED,
            COMMAND_STATUS_RUNNING,
            previous_session_id,
        ),
    )
    conn.commit()
    if cursor.rowcount != 1:
        raise ObserverCommandConflict("observer command takeover lost race")

    return {
        "ok": True,
        "project_id": pid,
        "observer_session_id": sid,
        "command": get_command(conn, project_id=pid, command_id=cid),
        "takeover": takeover,
    }


def _ensure_command_owned_by_session(
    command: dict[str, Any],
    *,
    session_id: str,
    action: str,
) -> None:
    if str(command.get("claimed_by_session_id") or "") != session_id:
        raise ObserverPermissionError(f"{action} requires the same claimed session")
    if command.get("status") not in OWNED_COMMAND_STATUSES:
        raise ObserverCommandConflict(f"observer command is not claim-owned: {command.get('status')}")


def _apply_analyze_requirement_result(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    command: dict[str, Any],
    result: dict[str, Any],
) -> None:
    if command.get("command_type") != COMMAND_TYPE_ANALYZE_REQUIREMENTS:
        return
    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
    raw_id = str(payload.get("raw_id") or result.get("raw_id") or "").strip()
    if not raw_id:
        return

    from . import raw_requirement

    raw_requirement.ensure_schema(conn)
    note_parts = []
    interpretation = str(result.get("ai_interpretation") or result.get("interpretation") or "").strip()
    mapping = result.get("proposed_backlog_mapping")
    if interpretation:
        note_parts.append(f"AI interpretation: {interpretation}")
    if isinstance(mapping, dict):
        title = str(mapping.get("title") or "").strip()
        bug_id = str(mapping.get("bug_id") or "").strip()
        if title or bug_id:
            note_parts.append("Proposed backlog mapping: " + " / ".join(x for x in [bug_id, title] if x))
    note = "\n".join(note_parts)
    try:
        raw_requirement.update_status(
            conn,
            project_id=project_id,
            raw_id=raw_id,
            new_status=raw_requirement.STATUS_NEEDS_CONFIRMATION,
            note=note or None,
        )
    except LookupError:
        return


def complete_command(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    session_id: str,
    session_token: str,
    command_id: str,
    result: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    pid = (project_id or "").strip()
    sid = (session_id or "").strip()
    timestamp = now or _utc_now()
    command = get_command(conn, project_id=pid, command_id=command_id)
    if not command:
        raise LookupError("observer command not found")
    authenticate_session(
        conn,
        project_id=pid,
        session_id=sid,
        session_token=session_token,
        action=ACTION_COMMAND_COMPLETE,
        command_type=str(command.get("command_type") or ""),
        now=timestamp,
    )
    _ensure_command_owned_by_session(command, session_id=sid, action="complete")
    result_payload = _merge_result_with_durable_takeover(command, result)
    if (
        str(command.get("command_type") or "") == COMMAND_TYPE_EXECUTE_BACKLOG_ROW
        and not _result_has_startup_or_blocker_evidence(result_payload)
    ):
        blocker = _missing_startup_surface_blocker(command, now=timestamp)
        result_payload["ok"] = False
        result_payload["startup_surface_blocker"] = blocker
        conn.execute(
            """UPDATE observer_command_queue
                  SET status = ?, completed_at = ?, result_json = ?, error = ?
                WHERE project_id = ? AND command_id = ?""",
            (
                COMMAND_STATUS_FAILED,
                timestamp,
                _json_dumps(result_payload),
                blocker["blocker_id"],
                pid,
                command_id,
            ),
        )
        conn.commit()
        return {
            "ok": True,
            "project_id": pid,
            "observer_session_id": sid,
            "command": get_command(conn, project_id=pid, command_id=command_id),
            "startup_surface_blocker": blocker,
        }
    conn.execute(
        """UPDATE observer_command_queue
              SET status = ?, completed_at = ?, result_json = ?, error = ''
            WHERE project_id = ? AND command_id = ?""",
        (COMMAND_STATUS_COMPLETED, timestamp, _json_dumps(result_payload), pid, command_id),
    )
    _apply_analyze_requirement_result(conn, project_id=pid, command=command, result=result_payload)
    conn.commit()
    return {
        "ok": True,
        "project_id": pid,
        "observer_session_id": sid,
        "command": get_command(conn, project_id=pid, command_id=command_id),
    }


def fail_command(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    session_id: str,
    session_token: str,
    command_id: str,
    error: str,
    result: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    pid = (project_id or "").strip()
    sid = (session_id or "").strip()
    timestamp = now or _utc_now()
    command = get_command(conn, project_id=pid, command_id=command_id)
    if not command:
        raise LookupError("observer command not found")
    authenticate_session(
        conn,
        project_id=pid,
        session_id=sid,
        session_token=session_token,
        action=ACTION_COMMAND_FAIL,
        command_type=str(command.get("command_type") or ""),
        now=timestamp,
    )
    _ensure_command_owned_by_session(command, session_id=sid, action="fail")
    result_payload = _merge_result_with_durable_takeover(command, result)
    conn.execute(
        """UPDATE observer_command_queue
              SET status = ?, completed_at = ?, result_json = ?, error = ?
            WHERE project_id = ? AND command_id = ?""",
        (
            COMMAND_STATUS_FAILED,
            timestamp,
            _json_dumps(result_payload),
            (error or "").strip(),
            pid,
            command_id,
        ),
    )
    conn.commit()
    return {
        "ok": True,
        "project_id": pid,
        "observer_session_id": sid,
        "command": get_command(conn, project_id=pid, command_id=command_id),
    }


def command_pending_reminder(project_id: str) -> dict[str, Any]:
    """Return the reminder-only hook payload shape for pending commands."""
    return {
        "kind": "observer_command_pending",
        "project_id": (project_id or "").strip(),
        "message": "pending observer commands exist; call observer_command_next",
        "payload_included": False,
        "next_action": {
            "tool": "observer_command_next",
            "description": "claim the next pending observer command",
        },
    }
