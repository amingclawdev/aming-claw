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
from typing import Any, Iterable, Mapping


HEARTBEAT_INTERVAL_SEC = 30
IDLE_AFTER_SEC = HEARTBEAT_INTERVAL_SEC * 2
STALE_AFTER_SEC = HEARTBEAT_INTERVAL_SEC * 4
CLAIMED_TO_STARTUP_TIMEOUT_SEC = STALE_AFTER_SEC
CLAIMED_TO_STARTUP_TIMEOUT_STATUS = "claimed_to_startup_timeout"
CLAIMED_TO_PROGRESS_TIMEOUT_SEC = STALE_AFTER_SEC
CLAIMED_TO_PROGRESS_TIMEOUT_STATUS = "claimed_to_progress_timeout"
NOTIFIED_UNCLAIMED_RECOVERY_THRESHOLD_SEC = HEARTBEAT_INTERVAL_SEC
OBSERVER_COMMAND_CONSUMER_RECOVERY_SCHEMA_VERSION = "observer_command_consumer_recovery.v1"
OBSERVER_COMMAND_CLAIM_EVIDENCE_SCHEMA_VERSION = "observer_command_claim_evidence.v1"
OBSERVER_COMMAND_CONTRACT_PROJECTION_SCHEMA_VERSION = "observer_command_contract_projection.v1"
OBSERVER_COMMAND_FIRST_PROGRESS_SCHEMA_VERSION = "observer_command_first_progress_evidence.v1"
OBSERVER_COMMAND_NO_PROGRESS_TIMEOUT_SCHEMA_VERSION = "observer_command_no_progress_timeout.v1"
OBSERVER_COMMAND_DURABLE_MF_SUB_EVIDENCE_SCHEMA_VERSION = (
    "observer_command_durable_mf_sub_evidence.v1"
)
OBSERVER_COMMAND_OBSERVER_ONLY_MONITOR_EVIDENCE_SCHEMA_VERSION = (
    "observer_command_observer_only_monitor_terminal_evidence.v1"
)

SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_CLOSED = "closed"
SESSION_STATUS_REVOKED = "revoked"
TARGET_SESSION_RECOVERY_STATUSES = {
    "missing",
    "stale",
    SESSION_STATUS_CLOSED,
    SESSION_STATUS_REVOKED,
}

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

FIRST_PROGRESS_PASS_STATUSES = {
    "accepted",
    "complete",
    "completed",
    "ok",
    "passed",
    "review_ready",
    "succeeded",
    "allow",
    "allowed",
    "approved",
}
FIRST_PROGRESS_EVENT_TOKENS = {
    "implementation",
    "implementation_progress",
    "review_ready",
    "verification",
    "independent_verification",
    "qa_verification",
    "close_ready",
    "checkpoint",
    "checkpoint_branch_task",
    "evidence_checkpoint",
    "finish_gate",
    "mf_subagent_finish_gate",
    "route_context",
    "route_context_consumed",
    "route_action_precheck",
    "precheck",
    "graph_query",
    "graph_trace",
    "worktree_dirty",
    "branch_head_advance",
}
FIRST_PROGRESS_BLOCKER_TOKENS = {
    "blocker",
    "blocked",
    "timeout",
    "no_progress_timeout",
    "terminal_blocker",
    "terminal_dispatch_blocker",
}
STARTUP_ONLY_EVENT_TOKENS = {
    "mf_subagent_startup",
    "mf_subagent_startup_gate",
    "mf_subagent_startup_intent",
    "mf_subagent_read_receipt",
    "bounded_implementation_worker_dispatch",
    "mf_subagent_dispatch",
    "mf_subagent_dispatch_gate",
}
OBSERVER_ONLY_MONITOR_KIND_TOKENS = {
    "dashboard_monitor",
    "monitor",
    "observer_monitor",
    "observer_only_dashboard_monitor",
    "observer_only_monitor",
    "dashboard_current_task_monitor",
    "current_task_monitor",
    "current_task_dashboard_monitor",
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
    "merge_queue_id",
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "route_token_ref",
    "visible_injection_manifest_hash",
)
EXECUTE_BACKLOG_ROW_STALE_ROUTE_STATUS_FIELDS = (
    "route_identity_status",
    "route_context_status",
    "route_evidence_status",
    "route_status",
    "prompt_contract_status",
)
EXECUTE_BACKLOG_ROW_SUPERSEDE_ROUTE_FIELDS = (
    "route_identity_superseded",
    "route_context_superseded",
    "route_evidence_superseded",
    "route_identity_supersede",
    "superseded_route_identity",
)
EXECUTE_BACKLOG_ROW_STALE_ROUTE_STATUSES = {
    "stale",
    "superseded",
    "supersede",
    "invalidated",
    "expired",
    "route_identity_supersede",
}
EXECUTE_BACKLOG_ROW_MERGE_QUEUE_CONTAINER_KEYS = (
    "corrected_replay_instructions",
    "branch_runtime_evidence",
    "branch_runtime_context",
    "graph_identity",
    "runtime_identity",
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


def _startup_evidence_is_prepared_or_appendable(value: dict[str, Any]) -> bool:
    status = str(value.get("status") or value.get("state") or "").strip().lower()
    return bool(
        value.get("prepared") is True
        or value.get("appendable") is True
        or status in {"prepared", "appendable"}
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
                and not _startup_evidence_is_prepared_or_appendable(startup_recording)
                and _actual_startup_identity_present(startup_recording)
            ):
                return True

        event_kind = str(
            lowered.get("event_kind") or lowered.get("event_type") or ""
        ).strip()
        if event_kind in {"mf_subagent_read_receipt", "mf_subagent_startup_intent"}:
            return False
        if event_kind == "mf_subagent_startup":
            payload = lowered.get("payload") if isinstance(lowered.get("payload"), dict) else {}
            gate = (
                payload.get("mf_subagent_startup_gate")
                if isinstance(payload.get("mf_subagent_startup_gate"), dict)
                else {}
            )
            if (
                lowered.get("recorded") is False
                or lowered.get("actual_startup_recorded") is False
                or gate.get("recorded") is False
                or gate.get("actual_startup_recorded") is False
                or _startup_evidence_is_prepared_or_appendable(lowered)
                or _startup_evidence_is_prepared_or_appendable(gate)
            ):
                return False
            return (
                (
                    lowered.get("actual_startup_recorded") is True
                    and _actual_startup_identity_present(lowered)
                )
                or (
                    gate.get("actual_startup_recorded") is True
                    and _actual_startup_identity_present(gate)
                )
            )

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
    projection = _command_terminal_projection_from_result(data, data["result"])
    if projection:
        _apply_command_terminal_projection_fields(data, projection)
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


def _result_has_canonical_close_evidence(result: dict[str, Any]) -> bool:
    return any(
        key in result
        for key in (
            "canonical_close_evidence",
            "canonical_contract_close_evidence",
            "contract_close_projection",
            "task_contract_close_projection",
            "timeline_gate",
            "timeline_events",
            "task_timeline_events",
            "backlog_close",
            "backlog_close_result",
        )
    )


def _result_is_terminal_blocked(result: dict[str, Any]) -> bool:
    status = str(result.get("status") or "").strip().lower()
    if status == "blocked":
        return True
    projection = result.get("terminal_contract_projection")
    if isinstance(projection, dict):
        projection_status = str(
            projection.get("command_projection_status") or ""
        ).strip().lower()
        contract_state = str(
            projection.get("canonical_contract_state") or ""
        ).strip().lower()
        if projection_status == "blocked" or contract_state == "blocked":
            return True
    for key in (
        "cli_timeout_blocker",
        "terminal_blocker",
        "terminal_dispatch_blocker",
        "startup_surface_blocker",
        "no_progress_timeout",
    ):
        value = result.get(key)
        if value is True:
            return True
        if isinstance(value, dict) and str(value.get("status") or "").lower() == "blocked":
            return True
    return False


def _terminal_blocker_from_result(result: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "cli_timeout_blocker",
        "terminal_blocker",
        "terminal_dispatch_blocker",
        "startup_surface_blocker",
        "no_progress_timeout",
    ):
        value = result.get(key)
        if isinstance(value, dict):
            blocker = dict(value)
            blocker.setdefault("status", "blocked")
            blocker.setdefault("blocker_id", key)
            return blocker
        if value is True:
            return {"status": "blocked", "blocker_id": key}
    return {"status": "blocked", "blocker_id": "blocked"}


def _observer_only_monitor_evidence(result: dict[str, Any]) -> dict[str, Any]:
    evidence = result.get("observer_only_monitor_evidence")
    if not isinstance(evidence, Mapping):
        return {}
    return dict(evidence)


def _observer_only_monitor_kind_present(evidence: Mapping[str, Any]) -> bool:
    tokens: set[str] = set()
    for key in (
        "command_kind",
        "command_mode",
        "command_classification",
        "monitor_kind",
        "mode",
        "purpose",
    ):
        token = _progress_token(evidence.get(key))
        if token:
            tokens.add(token)
    for key in ("monitor_command", "dashboard_monitor", "observer_only_monitor"):
        if evidence.get(key) is True:
            tokens.add("monitor" if key == "monitor_command" else key)
    return bool(tokens & OBSERVER_ONLY_MONITOR_KIND_TOKENS)


def _observer_only_monitor_evidence_refs(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for key in (
        "timeline_event_ref",
        "timeline_event_refs",
        "terminal_evidence_refs",
        "evidence_refs",
    ):
        value = evidence.get(key)
        if isinstance(value, str) and value.strip():
            refs.append({"kind": key, "ref": value.strip()})
        elif isinstance(value, (list, tuple)):
            for item in value:
                text = str(item or "").strip()
                if text:
                    refs.append({"kind": key, "ref": text})
    for key in ("timeline_event_id", "event_id"):
        event_id = _timeline_ref_id(evidence.get(key))
        if event_id:
            refs.append({"kind": key, "ref": f"timeline:{event_id}"})
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for ref in refs:
        identity = (str(ref.get("kind") or ""), str(ref.get("ref") or ""))
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(ref)
    return deduped


def _observer_only_monitor_terminal_projection(
    command: dict[str, Any],
    result: dict[str, Any],
    *,
    now: str,
) -> dict[str, Any]:
    if result.get("ok") is False or _result_is_terminal_blocked(result):
        return {}
    evidence = _observer_only_monitor_evidence(result)
    if not evidence:
        return {}
    if evidence.get("no_implementation_worker_required") is not True:
        return {}
    if evidence.get("implementation_worker_required") is True:
        return {}
    observer_only = bool(
        evidence.get("observer_only") is True
        or evidence.get("observer_only_command") is True
    )
    if not observer_only or not _observer_only_monitor_kind_present(evidence):
        return {}
    evidence_status = str(evidence.get("status") or "passed").strip().lower()
    if evidence_status and evidence_status not in FIRST_PROGRESS_PASS_STATUSES:
        return {}
    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
    for key in (
        "backlog_id",
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "visible_injection_manifest_hash",
    ):
        expected = str(payload.get(key) or "").strip()
        observed = str(evidence.get(key) or "").strip()
        if expected and observed != expected:
            return {}
    observed_command_id = str(evidence.get("observer_command_id") or "").strip()
    command_id = str(command.get("command_id") or "").strip()
    if observed_command_id and observed_command_id != command_id:
        return {}
    route_identity = _route_identity_from_payload(payload)
    return {
        "schema_version": "observer_command_terminal_projection.v1",
        "source_of_truth": "observer_command_queue/task_timeline",
        "projected_surface": "observer_command_queue",
        "projected_surfaces": [
            "observer_command_queue",
            "task_timeline",
            "dashboard_cards",
        ],
        "passed": True,
        "status": "projected_completed",
        "canonical_contract_state": "completed",
        "command_projection_status": "completed",
        "divergence_reason": "",
        "canonical_route_identity": route_identity,
        "superseded_route_identity": {},
        "terminal_evidence_refs": _observer_only_monitor_evidence_refs(evidence),
        "missing_requirement_ids": [],
        "observer_only_monitor": {
            "schema_version": OBSERVER_COMMAND_OBSERVER_ONLY_MONITOR_EVIDENCE_SCHEMA_VERSION,
            "no_implementation_worker_required": True,
            "observer_only": True,
            "monitor_kind": str(
                evidence.get("command_kind")
                or evidence.get("monitor_kind")
                or evidence.get("command_mode")
                or ""
            ),
            "observer_command_id": command_id,
            "backlog_id": str(payload.get("backlog_id") or ""),
            "completed_at": now,
        },
    }


def _progress_token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _progress_tokens_from_text(value: Any) -> set[str]:
    token = _progress_token(value)
    if not token:
        return set()
    tokens = {token}
    for part in token.replace(".", "_").replace(":", "_").replace("/", "_").split("_"):
        if part:
            tokens.add(part)
    return tokens


def _progress_event_tokens(value: Mapping[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for field in (
        "event_kind",
        "event_type",
        "phase",
        "schema_version",
        "gate_kind",
        "kind",
        "event",
        "blocker_id",
    ):
        tokens.update(_progress_tokens_from_text(value.get(field)))
    payload = value.get("payload") if isinstance(value.get("payload"), Mapping) else {}
    for field in ("event_kind", "event_type", "phase", "schema_version", "gate_kind", "kind"):
        tokens.update(_progress_tokens_from_text(payload.get(field)))
    return tokens


def _walk_mappings(value: Any) -> Iterable[dict[str, Any]]:
    seen: set[int] = set()
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, Mapping):
            item = dict(current)
            yield item
            stack.extend(item.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)


def _string_list_from_any(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item or "").strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _first_nested_text(value: Any, *keys: str) -> str:
    for mapping in _walk_mappings(value):
        for key in keys:
            text = str(mapping.get(key) or "").strip()
            if text:
                return text
    return ""


def _first_nested_int(value: Any, *keys: str) -> int:
    for mapping in _walk_mappings(value):
        for key in keys:
            try:
                raw = int(mapping.get(key) or 0)
            except (TypeError, ValueError):
                raw = 0
            if raw:
                return raw
    return 0


def _startup_lineage_from_result(
    command: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
    startup_source: dict[str, Any] = {}
    for mapping in _walk_mappings(result):
        if _actual_startup_identity_present(mapping):
            startup_source = mapping
            break
    source = startup_source or result
    actual_runtime = (
        source.get("actual_runtime") if isinstance(source.get("actual_runtime"), Mapping) else {}
    )
    return {
        "backlog_id": str(payload.get("backlog_id") or result.get("backlog_id") or ""),
        "task_id": str(
            source.get("task_id")
            or result.get("task_id")
            or payload.get("task_id")
            or ""
        ),
        "parent_task_id": str(
            source.get("parent_task_id")
            or result.get("parent_task_id")
            or payload.get("parent_task_id")
            or ""
        ),
        "runtime_context_id": str(
            source.get("runtime_context_id")
            or result.get("runtime_context_id")
            or payload.get("runtime_context_id")
            or ""
        ),
        "worker_id": str(
            source.get("worker_id")
            or source.get("worker_slot_id")
            or result.get("worker_id")
            or ""
        ),
        "fence_token": str(
            source.get("fence_token")
            or source.get("worker_fence_token")
            or source.get("actual_fence_token")
            or result.get("fence_token")
            or ""
        ),
        "worktree": str(
            source.get("worktree")
            or source.get("worktree_path")
            or source.get("actual_cwd")
            or actual_runtime.get("cwd")
            or ""
        ),
        "branch": str(source.get("branch") or source.get("branch_ref") or ""),
        "head_commit": str(
            source.get("head_commit")
            or source.get("branch_head")
            or actual_runtime.get("head_commit")
            or ""
        ),
        "base_commit": str(source.get("base_commit") or result.get("base_commit") or ""),
        "attempt_num": _first_nested_int(source, "attempt_num", "attempt"),
        "started_at": _first_nested_text(
            source,
            "created_at",
            "started_at",
            "recorded_at",
            "startup_recorded_at",
        ),
        "startup_event_id": _first_nested_int(
            source,
            "timeline_event_id",
            "event_id",
            "id",
        ),
    }


def _lineage_matches(value: Any, lineage: Mapping[str, Any]) -> bool:
    checks = {
        "task_id": str(lineage.get("task_id") or ""),
        "parent_task_id": str(lineage.get("parent_task_id") or ""),
        "runtime_context_id": str(lineage.get("runtime_context_id") or ""),
        "fence_token": str(lineage.get("fence_token") or ""),
    }
    checks = {key: expected for key, expected in checks.items() if expected}
    if not checks:
        return True
    observed_any = False
    for mapping in _walk_mappings(value):
        for key, expected in checks.items():
            actual = str(mapping.get(key) or "").strip()
            if not actual:
                continue
            observed_any = True
            if actual == expected:
                return True
    return not observed_any


_PRIVATE_DURABLE_EVIDENCE_KEY_MARKERS = (
    "observer_only",
    "private",
    "raw_context",
    "raw_memory",
    "raw_prompt",
    "raw_route",
    "unmanifested_prompt",
)


def _timeline_ref_id(value: Any) -> int:
    if isinstance(value, int):
        return max(0, value)
    text = str(value or "").strip()
    if not text:
        return 0
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    try:
        return max(0, int(text))
    except ValueError:
        return 0


def _timeline_ref_ids_from_any(value: Any) -> set[int]:
    if isinstance(value, Mapping):
        values = list(value.values())
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = [value]
    return {ref_id for item in values if (ref_id := _timeline_ref_id(item))}


def _nested_timeline_ref_ids(value: Any, *keys: str) -> set[int]:
    ids: set[int] = set()
    for mapping in _walk_mappings(value):
        for key in keys:
            if key in mapping:
                ids.update(_timeline_ref_ids_from_any(mapping.get(key)))
    return ids


def _timeline_event_id(event: Mapping[str, Any]) -> int:
    try:
        return int(event.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _timeline_event_ref(event: Mapping[str, Any]) -> str:
    event_id = _timeline_event_id(event)
    return f"timeline:{event_id}" if event_id else ""


def _timeline_event_haystack(event: Mapping[str, Any]) -> str:
    return " ".join(
        str(event.get(key) or "").strip().lower()
        for key in ("event_type", "event_kind", "phase", "schema_version", "status")
    )


def _timeline_event_status_passed(event: Mapping[str, Any]) -> bool:
    status = str(event.get("status") or "").strip().lower()
    return not status or status in FIRST_PROGRESS_PASS_STATUSES


def _public_durable_evidence_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        public: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            normalized = key_text.strip().lower().replace("-", "_")
            if any(marker in normalized for marker in _PRIVATE_DURABLE_EVIDENCE_KEY_MARKERS):
                continue
            public[key_text] = _public_durable_evidence_value(child)
        return public
    if isinstance(value, list):
        return [_public_durable_evidence_value(item) for item in value]
    if isinstance(value, tuple):
        return [_public_durable_evidence_value(item) for item in value]
    return value


def _public_timeline_event_evidence(event: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in (
        "id",
        "project_id",
        "backlog_id",
        "task_id",
        "attempt_num",
        "event_type",
        "phase",
        "event_kind",
        "actor",
        "status",
        "payload",
        "verification",
        "artifact_refs",
        "trace_id",
        "commit_sha",
        "created_at",
    ):
        if key in event:
            payload[key] = _public_durable_evidence_value(event.get(key))
    return payload


def _select_latest_timeline_event(
    events: list[dict[str, Any]],
    *,
    ref_ids: set[int],
    lineage: Mapping[str, Any],
    predicate: Any,
) -> dict[str, Any]:
    candidates = []
    for event in events:
        event_id = _timeline_event_id(event)
        if ref_ids and event_id not in ref_ids:
            continue
        if not _lineage_matches(event, lineage):
            continue
        if predicate(event):
            candidates.append(event)
    return max(candidates, key=_timeline_event_id, default={})


def _task_timeline_events_for_command(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    task_id: str,
    backlog_id: str,
    has_explicit_refs: bool,
) -> list[dict[str, Any]]:
    try:
        from . import task_timeline
    except Exception:
        return []

    events_by_id: dict[int, dict[str, Any]] = {}

    def add_events(rows: list[dict[str, Any]]) -> None:
        for event in rows:
            event_id = _timeline_event_id(event)
            if event_id:
                events_by_id[event_id] = event

    try:
        if task_id and backlog_id:
            add_events(
                task_timeline.list_events(
                    conn,
                    project_id,
                    task_id=task_id,
                    backlog_id=backlog_id,
                    limit=1000,
                )
            )
        if task_id:
            add_events(
                task_timeline.list_events(
                    conn,
                    project_id,
                    task_id=task_id,
                    limit=1000,
                )
            )
        if backlog_id:
            add_events(
                task_timeline.list_events(
                    conn,
                    project_id,
                    backlog_id=backlog_id,
                    limit=1000,
                )
            )
        if not events_by_id and has_explicit_refs:
            add_events(task_timeline.list_events(conn, project_id, limit=1000))
    except Exception:
        return []
    return [events_by_id[key] for key in sorted(events_by_id)]


def _command_branch_context_for_durable_evidence(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    runtime_context_id: str,
    task_id: str,
) -> Any:
    try:
        from .parallel_branch_runtime import (
            get_branch_context,
            get_branch_context_by_runtime_context_id,
        )
    except Exception:
        return None
    try:
        if runtime_context_id:
            context = get_branch_context_by_runtime_context_id(
                conn,
                project_id,
                runtime_context_id,
            )
            if context is not None:
                return context
        if task_id:
            return get_branch_context(conn, project_id, task_id)
    except Exception:
        return None
    return None


def _persisted_mf_sub_evidence_for_command(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    command: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
    backlog_id = str(payload.get("backlog_id") or result.get("backlog_id") or "")
    runtime_context_id = _first_nested_text(
        result,
        "runtime_context_id",
        "branch_runtime_registration_ref",
    ) or str(payload.get("runtime_context_id") or "")
    task_id = _first_nested_text(result, "task_id") or str(payload.get("task_id") or "")

    context = _command_branch_context_for_durable_evidence(
        conn,
        project_id=project_id,
        runtime_context_id=runtime_context_id,
        task_id=task_id,
    )
    if context is not None:
        runtime_context_id = runtime_context_id or str(
            getattr(context, "runtime_context_id", "") or ""
        )
        task_id = task_id or str(getattr(context, "task_id", "") or "")
        backlog_id = backlog_id or str(getattr(context, "backlog_id", "") or "")

    startup_ref_ids = _nested_timeline_ref_ids(
        result,
        "startup_event_ref",
        "startup_event_id",
        "startup_gate_ref",
    )
    read_ref_ids = _nested_timeline_ref_ids(
        result,
        "read_receipt_event_ref",
        "read_receipt_event_id",
        "read_receipt_ref",
    )
    finish_ref_ids = _nested_timeline_ref_ids(
        result,
        "finish_gate_ref",
        "finish_gate_event_ref",
        "finish_event_ref",
        "finish_event_id",
    )
    verification_ref_ids = _nested_timeline_ref_ids(
        result,
        "verification_event_refs",
        "verification_event_ids",
        "verification_event_ref",
        "verification_event_id",
    )
    has_explicit_refs = bool(
        startup_ref_ids or read_ref_ids or finish_ref_ids or verification_ref_ids
    )
    if not (task_id or backlog_id or runtime_context_id or has_explicit_refs):
        return {}

    fence_token = _first_nested_text(result, "fence_token", "worker_fence_token")
    parent_task_id = _first_nested_text(result, "parent_task_id")
    if context is not None:
        fence_token = fence_token or str(getattr(context, "fence_token", "") or "")
        parent_task_id = parent_task_id or str(
            getattr(context, "root_task_id", "")
            or getattr(context, "chain_id", "")
            or getattr(context, "stage_task_id", "")
            or getattr(context, "backlog_id", "")
            or ""
        )
    lineage = {
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "runtime_context_id": runtime_context_id,
        "fence_token": fence_token,
    }
    events = _task_timeline_events_for_command(
        conn,
        project_id=project_id,
        task_id=task_id,
        backlog_id=backlog_id,
        has_explicit_refs=has_explicit_refs,
    )
    if not events:
        return {}

    def startup_predicate(event: Mapping[str, Any]) -> bool:
        haystack = _timeline_event_haystack(event)
        return (
            "startup" in haystack
            and "intent" not in haystack
            and _timeline_event_status_passed(event)
            and _contains_actual_startup_evidence(dict(event))
        )

    def read_receipt_predicate(event: Mapping[str, Any]) -> bool:
        return (
            "read_receipt" in _timeline_event_haystack(event)
            and _timeline_event_status_passed(event)
        )

    def finish_predicate(event: Mapping[str, Any]) -> bool:
        return (
            "finish" in _timeline_event_haystack(event)
            and _timeline_event_status_passed(event)
        )

    def verification_predicate(event: Mapping[str, Any]) -> bool:
        haystack = _timeline_event_haystack(event)
        return "verification" in haystack and _timeline_event_status_passed(event)

    startup_event = _select_latest_timeline_event(
        events,
        ref_ids=startup_ref_ids,
        lineage=lineage,
        predicate=startup_predicate,
    )
    read_receipt_event = _select_latest_timeline_event(
        events,
        ref_ids=read_ref_ids,
        lineage=lineage,
        predicate=read_receipt_predicate,
    )
    if not startup_event or not read_receipt_event:
        return {}

    finish_event = _select_latest_timeline_event(
        events,
        ref_ids=finish_ref_ids,
        lineage=lineage,
        predicate=finish_predicate,
    )
    verification_events = [
        event
        for event in events
        if (not verification_ref_ids or _timeline_event_id(event) in verification_ref_ids)
        and _lineage_matches(event, lineage)
        and verification_predicate(event)
    ]
    checkpoint_id = _first_nested_text(result, "checkpoint_id")
    context_checkpoint = ""
    context_replay_source = ""
    if context is not None:
        context_checkpoint = str(getattr(context, "checkpoint_id", "") or "")
        context_replay_source = str(getattr(context, "replay_source", "") or "")
    finish_gate: dict[str, Any] = {}
    if finish_event:
        finish_gate = _public_timeline_event_evidence(finish_event)
    elif (
        context_checkpoint
        and context_replay_source == "mf_sub_finish_gate"
        and (not checkpoint_id or checkpoint_id == context_checkpoint)
    ):
        finish_gate = {
            "kind": "finish_gate",
            "event_kind": "finish_gate",
            "status": "passed",
            "source": "parallel_branch_runtime.context",
            "checkpoint_id": context_checkpoint,
            "runtime_context_id": runtime_context_id,
            "task_id": task_id,
            "fence_token": fence_token,
            "replay_source": context_replay_source,
        }

    evidence = {
        "schema_version": OBSERVER_COMMAND_DURABLE_MF_SUB_EVIDENCE_SCHEMA_VERSION,
        "source": "persisted_task_timeline_and_parallel_branch_runtime",
        "startup_event_ref": _timeline_event_ref(startup_event),
        "read_receipt_event_ref": _timeline_event_ref(read_receipt_event),
        "finish_gate_ref": _timeline_event_ref(finish_event)
        or (
            f"parallel_branch_runtime:{runtime_context_id}:{context_checkpoint}"
            if finish_gate and context_checkpoint
            else ""
        ),
        "verification_event_refs": [
            _timeline_event_ref(event)
            for event in verification_events
            if _timeline_event_ref(event)
        ],
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "backlog_id": backlog_id,
        "fence_token": fence_token,
        "startup_event": _public_timeline_event_evidence(startup_event),
        "read_receipt_event": _public_timeline_event_evidence(read_receipt_event),
        "finish_gate": finish_gate,
        "verification_events": [
            _public_timeline_event_evidence(event) for event in verification_events
        ],
    }
    if not _contains_actual_startup_evidence(evidence):
        return {}
    return evidence


def _hydrate_result_with_persisted_mf_sub_evidence(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    command: dict[str, Any],
    result: dict[str, Any],
) -> None:
    if _result_has_startup_or_blocker_evidence(result):
        return
    evidence = _persisted_mf_sub_evidence_for_command(
        conn,
        project_id=project_id,
        command=command,
        result=result,
    )
    if evidence:
        result["durable_mf_sub_evidence"] = evidence


def _mapping_has_graph_trace_progress(value: Mapping[str, Any]) -> dict[str, Any]:
    trace_ids = _string_list_from_any(
        value.get("trace_ids")
        or value.get("graph_trace_ids")
        or value.get("graph_query_trace_ids")
    )
    trace_id = str(value.get("trace_id") or "").strip()
    if trace_id.startswith("gqt-"):
        trace_ids.append(trace_id)
    if not trace_ids:
        return {}
    has_task_identity = bool(
        str(value.get("task_id") or "").strip()
        or str(value.get("parent_task_id") or "").strip()
    )
    has_fence_identity = bool(
        str(value.get("fence_token") or value.get("fence_token_hash") or "").strip()
    )
    if not (has_task_identity and has_fence_identity):
        return {}
    return {
        "source": "result.graph_trace",
        "kind": "graph_trace",
        "trace_ids": sorted(set(trace_ids)),
        "task_id": str(value.get("task_id") or ""),
        "parent_task_id": str(value.get("parent_task_id") or ""),
        "fence_token_present": has_fence_identity,
    }


def _mapping_has_worktree_progress(value: Mapping[str, Any]) -> dict[str, Any]:
    changed = _string_list_from_any(value.get("implementation_changed_files"))
    changed.extend(_string_list_from_any(value.get("changed_files")))
    dirty = _string_list_from_any(value.get("dirty_files"))
    tool_artifacts = {
        path
        for path in _string_list_from_any(value.get("tool_artifact_files"))
        if path
    }
    implementation_changed = [
        path
        for path in sorted(set(changed + dirty))
        if path and path not in tool_artifacts and not path.startswith(".aming-claw/")
    ]
    head_commit = str(value.get("head_commit") or "").strip()
    base_commit = str(
        value.get("base_commit") or value.get("target_head_commit") or ""
    ).strip()
    head_advanced = bool(head_commit and base_commit and head_commit != base_commit)
    if not implementation_changed and not head_advanced:
        return {}
    return {
        "source": "result.worktree",
        "kind": "worktree_or_head_progress",
        "implementation_changed_files": implementation_changed,
        "head_advanced": head_advanced,
        "head_commit": head_commit,
        "base_commit": base_commit,
    }


def _mapping_has_checkpoint_or_blocker_progress(value: Mapping[str, Any]) -> dict[str, Any]:
    tokens = _progress_event_tokens(value)
    if tokens & FIRST_PROGRESS_BLOCKER_TOKENS:
        return {
            "source": "result.blocker",
            "kind": "explicit_blocker",
            "tokens": sorted(tokens & FIRST_PROGRESS_BLOCKER_TOKENS),
        }
    checkpoint_tokens = tokens & {
        "checkpoint",
        "checkpoint_branch_task",
        "evidence_checkpoint",
        "finish_gate",
        "mf_subagent_finish_gate",
    }
    if checkpoint_tokens:
        return {
            "source": "result.checkpoint",
            "kind": "checkpoint_or_finish_gate",
            "tokens": sorted(checkpoint_tokens),
        }
    return {}


def _timeline_event_progress_evidence(
    event: Mapping[str, Any],
    *,
    lineage: Mapping[str, Any],
) -> dict[str, Any]:
    if not _lineage_matches(event, lineage):
        return {}
    tokens = _progress_event_tokens(event)
    if tokens & STARTUP_ONLY_EVENT_TOKENS:
        return {}
    blocker_tokens = tokens & FIRST_PROGRESS_BLOCKER_TOKENS
    progress_tokens = tokens & FIRST_PROGRESS_EVENT_TOKENS
    if not blocker_tokens and not progress_tokens:
        return {}
    status = str(event.get("status") or "").strip().lower()
    if not blocker_tokens and status and status not in FIRST_PROGRESS_PASS_STATUSES:
        return {}
    return {
        "source": "task_timeline",
        "kind": str(event.get("event_kind") or event.get("event_type") or ""),
        "event_id": int(event.get("id") or 0),
        "status": status,
        "tokens": sorted((blocker_tokens or progress_tokens)),
    }


def _result_first_progress_evidence(result: dict[str, Any]) -> dict[str, Any]:
    if _result_has_canonical_close_evidence(result):
        return {
            "source": "result.canonical_close_evidence",
            "kind": "canonical_close_evidence",
        }
    for mapping in _walk_mappings(result):
        event_progress = _timeline_event_progress_evidence(mapping, lineage={})
        if event_progress:
            event_progress["source"] = "result.timeline_event"
            return event_progress
        for checker in (
            _mapping_has_graph_trace_progress,
            _mapping_has_worktree_progress,
            _mapping_has_checkpoint_or_blocker_progress,
        ):
            progress = checker(mapping)
            if progress:
                return progress
    return {}


def _timeline_first_progress_evidence(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    command: dict[str, Any],
    lineage: Mapping[str, Any],
) -> dict[str, Any]:
    backlog_id = str(lineage.get("backlog_id") or "")
    if not backlog_id:
        return {}
    try:
        from . import task_timeline

        events = task_timeline.list_events(
            conn,
            project_id,
            backlog_id=backlog_id,
            limit=1000,
        )
    except Exception as exc:
        return {
            "present": False,
            "source": "task_timeline",
            "error": str(exc),
        }
    startup_events = [
        event
        for event in events
        if _lineage_matches(event, lineage)
        and (_progress_event_tokens(event) & {"mf_subagent_startup", "mf_subagent_startup_gate"})
    ]
    latest_startup = max(
        startup_events,
        key=lambda item: int(item.get("id") or 0),
        default=None,
    )
    latest_startup_id = int(latest_startup.get("id") or 0) if latest_startup else 0
    latest_startup_created_at = str(latest_startup.get("created_at") or "") if latest_startup else ""
    for event in events:
        event_id = int(event.get("id") or 0)
        if latest_startup_id and event_id <= latest_startup_id:
            continue
        progress = _timeline_event_progress_evidence(event, lineage=lineage)
        if not progress:
            continue
        route_or_precheck = set(progress.get("tokens") or []) & {
            "route_context",
            "route_context_consumed",
            "route_action_precheck",
            "precheck",
        }
        if route_or_precheck and not latest_startup_id:
            continue
        return {
            **progress,
            "present": True,
            "startup_event_id": latest_startup_id,
            "startup_event_created_at": latest_startup_created_at,
        }
    return {
        "present": False,
        "source": "task_timeline",
        "startup_event_id": latest_startup_id,
        "startup_event_created_at": latest_startup_created_at,
    }


def _command_first_progress_evidence(
    conn: sqlite3.Connection,
    command: dict[str, Any],
    *,
    now: str,
) -> dict[str, Any]:
    result = _command_result(command)
    lineage = _startup_lineage_from_result(command, result)
    result_progress = _result_first_progress_evidence(result)
    if result_progress:
        return {
            "schema_version": OBSERVER_COMMAND_FIRST_PROGRESS_SCHEMA_VERSION,
            "present": True,
            "generated_at": now,
            "startup_evidence_present": _contains_actual_startup_evidence(result),
            "startup_lineage": lineage,
            "evidence": result_progress,
            "excluded_as_progress": sorted(STARTUP_ONLY_EVENT_TOKENS),
        }
    timeline_progress = _timeline_first_progress_evidence(
        conn,
        project_id=str(command.get("project_id") or ""),
        command=command,
        lineage=lineage,
    )
    if timeline_progress.get("present"):
        return {
            "schema_version": OBSERVER_COMMAND_FIRST_PROGRESS_SCHEMA_VERSION,
            "present": True,
            "generated_at": now,
            "startup_evidence_present": _contains_actual_startup_evidence(result),
            "startup_lineage": lineage,
            "evidence": timeline_progress,
            "excluded_as_progress": sorted(STARTUP_ONLY_EVENT_TOKENS),
        }
    return {
        "schema_version": OBSERVER_COMMAND_FIRST_PROGRESS_SCHEMA_VERSION,
        "present": False,
        "generated_at": now,
        "startup_evidence_present": _contains_actual_startup_evidence(result),
        "startup_lineage": lineage,
        "timeline": timeline_progress,
        "expected_sources": [
            "accepted_graph_trace_with_task_and_fence_identity",
            "route_or_precheck_event_after_startup",
            "implementation_or_progress_timeline_event",
            "fenced_worktree_dirty_diff",
            "branch_head_advance",
            "checkpoint_or_finish_gate",
            "explicit_blocker",
        ],
        "excluded_as_progress": sorted(STARTUP_ONLY_EVENT_TOKENS),
    }


def _progress_watch_started_at(
    command: dict[str, Any],
    progress: Mapping[str, Any],
) -> str:
    timeline = progress.get("timeline") if isinstance(progress.get("timeline"), Mapping) else {}
    startup_lineage = (
        progress.get("startup_lineage")
        if isinstance(progress.get("startup_lineage"), Mapping)
        else {}
    )
    return str(
        timeline.get("startup_event_created_at")
        or startup_lineage.get("started_at")
        or command.get("claimed_at")
        or ""
    )


def _progress_watch_age_sec(
    command: dict[str, Any],
    progress: Mapping[str, Any],
    *,
    now: str,
) -> float | None:
    started_at = _parse_utc(_progress_watch_started_at(command, progress))
    now_dt = _parse_utc(now)
    if not started_at or not now_dt:
        return None
    return max(0.0, (now_dt - started_at).total_seconds())


def _claimed_execute_takeover_timeout(
    conn: sqlite3.Connection,
    command: dict[str, Any] | None,
    *,
    now: str,
) -> dict[str, Any]:
    if not command:
        return {}
    if str(command.get("command_type") or "") != COMMAND_TYPE_EXECUTE_BACKLOG_ROW:
        return {}
    if str(command.get("status") or "") not in OWNED_COMMAND_STATUSES:
        return {}
    if not _command_has_startup_or_blocker_evidence(command):
        age = _command_claim_age_sec(command, now=now)
        if age is None or age < CLAIMED_TO_STARTUP_TIMEOUT_SEC:
            return {}
        return {
            "status": CLAIMED_TO_STARTUP_TIMEOUT_STATUS,
            "timeout_kind": "startup_timeout",
            "age_sec": age,
            "timeout_sec": CLAIMED_TO_STARTUP_TIMEOUT_SEC,
        }
    result = _command_result(command)
    if _contains_terminal_dispatch_blocker(result) or _result_is_terminal_blocked(result):
        return {}
    progress = _command_first_progress_evidence(conn, command, now=now)
    if progress.get("present"):
        return {}
    age = _progress_watch_age_sec(command, progress, now=now)
    if age is None or age < CLAIMED_TO_PROGRESS_TIMEOUT_SEC:
        return {}
    return {
        "schema_version": OBSERVER_COMMAND_NO_PROGRESS_TIMEOUT_SCHEMA_VERSION,
        "status": CLAIMED_TO_PROGRESS_TIMEOUT_STATUS,
        "timeout_kind": "no_progress_timeout",
        "age_sec": age,
        "timeout_sec": CLAIMED_TO_PROGRESS_TIMEOUT_SEC,
        "progress_watchdog": progress,
        "startup_evidence": "present",
        "progress_evidence": "missing",
    }


def _record_no_progress_timeout_event(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    command: dict[str, Any],
    timeout: Mapping[str, Any],
    now: str,
) -> dict[str, Any]:
    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
    progress = timeout.get("progress_watchdog") if isinstance(timeout.get("progress_watchdog"), Mapping) else {}
    lineage = (
        progress.get("startup_lineage")
        if isinstance(progress.get("startup_lineage"), Mapping)
        else {}
    )
    try:
        from . import task_timeline

        return task_timeline.record_event(
            conn,
            project_id=project_id,
            backlog_id=str(payload.get("backlog_id") or lineage.get("backlog_id") or ""),
            task_id=str(lineage.get("task_id") or payload.get("task_id") or ""),
            attempt_num=int(lineage.get("attempt_num") or 0),
            event_type="observer_command.no_progress_timeout",
            phase="implementation_wait",
            event_kind="no_progress_timeout",
            actor="observer_command_watchdog",
            status="blocked",
            payload={
                "schema_version": OBSERVER_COMMAND_NO_PROGRESS_TIMEOUT_SCHEMA_VERSION,
                "observer_command_id": str(command.get("command_id") or ""),
                "claimed_by_session_id": str(command.get("claimed_by_session_id") or ""),
                "route_identity": _route_identity_from_payload(payload),
                "timeout": dict(timeout),
                "recorded_at": now,
            },
        )
    except Exception as exc:
        return {
            "recorded": False,
            "error": str(exc),
        }


def _command_terminal_projection_from_result(
    command: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    for key in (
        "terminal_contract_projection",
        "observer_command_terminal_projection",
        "command_terminal_projection",
    ):
        projection = result.get(key)
        if isinstance(projection, dict):
            return dict(projection)
    if not _result_has_canonical_close_evidence(result):
        return {}
    try:
        from . import task_timeline

        payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
        return task_timeline.observer_command_terminal_projection_from_close_evidence(
            payload,
            result,
        )
    except Exception:
        return {}


def _apply_command_terminal_projection_fields(
    target: dict[str, Any],
    projection: dict[str, Any],
) -> None:
    target["canonical_contract_state"] = projection.get("canonical_contract_state", "")
    target["command_projection_status"] = projection.get("command_projection_status", "")
    target["divergence_reason"] = projection.get("divergence_reason", "")
    target["canonical_route_identity"] = projection.get("canonical_route_identity", {})
    target["superseded_route_identity"] = projection.get("superseded_route_identity", {})
    target["terminal_evidence_refs"] = projection.get("terminal_evidence_refs", [])


def _attach_command_terminal_projection(
    result: dict[str, Any],
    projection: dict[str, Any],
) -> None:
    result["terminal_contract_projection"] = projection
    _apply_command_terminal_projection_fields(result, projection)


def _missing_startup_surface_blocker(command: dict[str, Any], *, now: str) -> dict[str, Any]:
    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
    return {
        "blocker_id": "no_truthful_bounded_mf_sub_startup_surface_available",
        "dispatch_blocker": True,
        "terminal_dispatch_blocker": True,
        "status": "blocked",
        "observer_command_id": str(command.get("command_id") or ""),
        "backlog_id": str(payload.get("backlog_id") or ""),
        "merge_queue_id": _execute_backlog_payload_required_value(payload, "merge_queue_id"),
        "route_id": str(payload.get("route_id") or ""),
        "route_context_hash": str(payload.get("route_context_hash") or ""),
        "prompt_contract_id": str(payload.get("prompt_contract_id") or ""),
        "route_token_ref": str(payload.get("route_token_ref") or ""),
        "visible_injection_manifest_hash": str(
            payload.get("visible_injection_manifest_hash") or ""
        ),
        "route_identity": _route_identity_from_payload(payload),
        "claimed_at": str(command.get("claimed_at") or ""),
        "failed_at": now,
        "next_legal_action": {
            "tool": "observer_runtime_text_prepare",
            "description": (
                "Prepare runtime text from the claimed execute_backlog_row command, "
                "record mf_subagent_read_receipt, then record actual startup before "
                "counting worker evidence."
            ),
            "observer_command_id": str(command.get("command_id") or ""),
        },
        "reason": (
            "execute_backlog_row completion requires actual bounded mf_sub startup "
            "evidence joined to the claimed observer_command_id and prior "
            "mf_subagent_read_receipt, or an explicit terminal dispatch blocker; "
            "branch allocation and runtime-text startup intent are not startup"
        ),
    }


def _missing_first_progress_blocker(
    command: dict[str, Any],
    *,
    now: str,
    progress_watchdog: Mapping[str, Any],
) -> dict[str, Any]:
    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
    return {
        "blocker_id": "startup_without_first_progress_evidence",
        "terminal_blocker": True,
        "status": "blocked",
        "observer_command_id": str(command.get("command_id") or ""),
        "backlog_id": str(payload.get("backlog_id") or ""),
        "merge_queue_id": _execute_backlog_payload_required_value(payload, "merge_queue_id"),
        "route_id": str(payload.get("route_id") or ""),
        "route_context_hash": str(payload.get("route_context_hash") or ""),
        "prompt_contract_id": str(payload.get("prompt_contract_id") or ""),
        "route_token_ref": str(payload.get("route_token_ref") or ""),
        "visible_injection_manifest_hash": str(
            payload.get("visible_injection_manifest_hash") or ""
        ),
        "route_identity": _route_identity_from_payload(payload),
        "claimed_at": str(command.get("claimed_at") or ""),
        "failed_at": now,
        "progress_watchdog": dict(progress_watchdog),
        "next_legal_action": {
            "tool": "task_timeline_append",
            "description": (
                "Append implementation, verification, checkpoint, finish-gate, "
                "or terminal-blocker evidence only after command/read-receipt/"
                "startup lineage is recorded."
            ),
            "observer_command_id": str(command.get("command_id") or ""),
        },
        "reason": (
            "execute_backlog_row completion cannot treat mf_subagent_startup as "
            "implementation progress; completion requires graph/precheck/route "
            "progress, worktree/head changes, checkpoint/finish evidence, close "
            "evidence, or an explicit terminal blocker"
        ),
    }


def _route_identity_from_payload(payload: Mapping[str, Any] | dict[str, Any]) -> dict[str, str]:
    return {
        "route_id": str(payload.get("route_id") or ""),
        "route_context_hash": str(payload.get("route_context_hash") or ""),
        "prompt_contract_id": str(payload.get("prompt_contract_id") or ""),
        "prompt_contract_hash": str(payload.get("prompt_contract_hash") or ""),
        "route_token_ref": str(payload.get("route_token_ref") or ""),
        "precheck_run_id": str(payload.get("precheck_run_id") or ""),
        "visible_injection_manifest_hash": str(
            payload.get("visible_injection_manifest_hash") or ""
        ),
    }


def _execute_backlog_route_payload_containers(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    containers: list[Mapping[str, Any]] = [payload]
    for key in (
        "route_identity",
        "claimed_route_identity",
        "route_context",
        "route_prompt_contract",
        "route_prompt_bundle",
        "prompt_contract",
        "bundle",
    ):
        value = payload.get(key)
        if isinstance(value, Mapping):
            containers.append(value)
    return containers


def _execute_backlog_route_identity_blockers(payload: Any) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    for container in _execute_backlog_route_payload_containers(payload):
        for field in EXECUTE_BACKLOG_ROW_STALE_ROUTE_STATUS_FIELDS:
            status = str(container.get(field) or "").strip().lower().replace("-", "_")
            if status in EXECUTE_BACKLOG_ROW_STALE_ROUTE_STATUSES:
                blockers.append(
                    {
                        "field": field,
                        "status": status,
                        "reason": "stale_or_superseded_route_identity",
                    }
                )
        for field in EXECUTE_BACKLOG_ROW_SUPERSEDE_ROUTE_FIELDS:
            value = container.get(field)
            if value is True or (isinstance(value, str) and value.strip()):
                blockers.append(
                    {
                        "field": field,
                        "status": "superseded",
                        "reason": "stale_or_superseded_route_identity",
                    }
                )
    return blockers


def _command_claim_age_sec(command: dict[str, Any], *, now: str) -> float | None:
    claimed_at = _parse_utc(str(command.get("claimed_at") or ""))
    now_dt = _parse_utc(now)
    if not claimed_at or not now_dt:
        return None
    return max(0.0, (now_dt - claimed_at).total_seconds())


def _command_notified_age_sec(command: dict[str, Any], *, now: str) -> float | None:
    notified_at = _parse_utc(str(command.get("notified_at") or command.get("created_at") or ""))
    now_dt = _parse_utc(now)
    if not notified_at or not now_dt:
        return None
    return max(0.0, (now_dt - notified_at).total_seconds())


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
    for key in (
        "takeover",
        "takeover_status",
        "target_session_recovery",
        "observer_claim_evidence",
        "claim_blocker",
        "terminal_contract_projection",
        "canonical_contract_state",
        "command_projection_status",
        "divergence_reason",
        "canonical_route_identity",
        "superseded_route_identity",
        "terminal_evidence_refs",
        "progress_watchdog",
        "no_progress_timeout",
    ):
        if key not in incoming and key in existing:
            incoming[key] = existing[key]
    return incoming


def _execute_backlog_payload_missing_fields(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return list(EXECUTE_BACKLOG_ROW_REQUIRED_PAYLOAD_FIELDS)
    return [
        field
        for field in EXECUTE_BACKLOG_ROW_REQUIRED_PAYLOAD_FIELDS
        if not _execute_backlog_payload_required_value(payload, field)
    ]


def _execute_backlog_payload_required_value(payload: Any, field: str) -> str:
    if not isinstance(payload, Mapping):
        return ""
    direct = str(payload.get(field) or "").strip()
    if direct:
        return direct
    if field != "merge_queue_id":
        return ""
    for key in EXECUTE_BACKLOG_ROW_MERGE_QUEUE_CONTAINER_KEYS:
        nested = payload.get(key)
        if not isinstance(nested, Mapping):
            continue
        token = str(nested.get("merge_queue_id") or "").strip()
        if token:
            return token
        context = nested.get("context")
        if isinstance(context, Mapping):
            token = str(context.get("merge_queue_id") or "").strip()
            if token:
                return token
    return ""


def _execute_backlog_payload_blocker(
    command: dict[str, Any],
    *,
    now: str,
    missing: list[str] | None = None,
    route_identity_blockers: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
    missing_fields = missing if missing is not None else _execute_backlog_payload_missing_fields(payload)
    identity_blockers = (
        route_identity_blockers
        if route_identity_blockers is not None
        else _execute_backlog_route_identity_blockers(payload)
    )
    blocker_id = (
        "execute_backlog_row_stale_route_identity"
        if identity_blockers and not missing_fields
        else "execute_backlog_row_invalid_route_payload"
    )
    return {
        "blocker_id": blocker_id,
        "dispatch_blocker": True,
        "terminal_dispatch_blocker": True,
        "status": "blocked",
        "observer_command_id": str(command.get("command_id") or ""),
        "command_status": str(command.get("status") or ""),
        "backlog_id": str(payload.get("backlog_id") or ""),
        "merge_queue_id": _execute_backlog_payload_required_value(payload, "merge_queue_id"),
        "target_session_id": str(command.get("target_session_id") or ""),
        "route_identity": _route_identity_from_payload(payload),
        "missing_required_fields": missing_fields,
        "route_identity_blockers": identity_blockers,
        "failed_at": now,
        "next_legal_action": {
            "tool": "observer_command_fail_or_supersede_then_enqueue",
            "description": (
                "Fail or supersede this command, then enqueue and claim a fresh "
                "backlog-specific execute_backlog_row command with active route "
                "identity, route_token_ref, merge_queue_id, and branch runtime "
                "allocation evidence before preparing runtime text or launching "
                "a worker."
            ),
            "required_payload_fields": list(EXECUTE_BACKLOG_ROW_REQUIRED_PAYLOAD_FIELDS),
        },
        "reason": (
            "execute_backlog_row command cannot be claimed because its route-bound "
            "payload is missing required route/backlog evidence or carries stale "
            "route identity"
        ),
    }


def _execute_backlog_claim_evidence(
    command: dict[str, Any],
    *,
    session_id: str,
    now: str,
) -> dict[str, Any]:
    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
    route_identity = _route_identity_from_payload(payload)
    return {
        "schema_version": OBSERVER_COMMAND_CLAIM_EVIDENCE_SCHEMA_VERSION,
        "observer_command_id": str(command.get("command_id") or ""),
        "observer_session_id": session_id,
        "claimed_at": now,
        "command_type": str(command.get("command_type") or ""),
        "backlog_id": str(payload.get("backlog_id") or ""),
        "merge_queue_id": _execute_backlog_payload_required_value(payload, "merge_queue_id"),
        "target_session_id": str(command.get("target_session_id") or ""),
        "route_identity": route_identity,
        "precheck_evidence": {
            "precheck_run_id": route_identity["precheck_run_id"],
            "present": bool(route_identity["precheck_run_id"]),
            "visible_injection_manifest_hash": route_identity[
                "visible_injection_manifest_hash"
            ],
        },
        "contract_handoff_projection": _observer_command_contract_projection(
            command,
            status=COMMAND_STATUS_CLAIMED,
            watermark=now,
        ),
        "next_expected_evidence": "mf_subagent_startup_or_terminal_dispatch_blocker",
    }


def _observer_command_contract_projection(
    command: dict[str, Any],
    *,
    status: str,
    watermark: str,
) -> dict[str, Any]:
    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
    route_identity = _route_identity_from_payload(payload)
    material = {
        "command_id": str(command.get("command_id") or ""),
        "command_type": str(command.get("command_type") or ""),
        "backlog_id": str(payload.get("backlog_id") or ""),
        "merge_queue_id": _execute_backlog_payload_required_value(payload, "merge_queue_id"),
        "route_identity": route_identity,
    }
    body = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "schema_version": OBSERVER_COMMAND_CONTRACT_PROJECTION_SCHEMA_VERSION,
        "source_of_truth": "Contract/Revision/Event",
        "projected_surface": "observer_command_queue",
        "projected_surfaces": [
            "observer_command_queue",
            "task_timeline",
            "backlog_runtime_state",
            "dashboard_cards",
            "branch_runtime",
        ],
        "contract_derived_status": status,
        "projection_watermark": watermark,
        "status": status,
        "stale": False,
        "divergent": False,
        "contract_hash": "sha256:" + sha256(body.encode("utf-8")).hexdigest(),
    }


def _validate_command_payload(command_type: str, payload: Any) -> None:
    if command_type != COMMAND_TYPE_EXECUTE_BACKLOG_ROW:
        return
    missing = _execute_backlog_payload_missing_fields(payload)
    if not isinstance(payload, dict):
        missing = ", ".join(EXECUTE_BACKLOG_ROW_REQUIRED_PAYLOAD_FIELDS)
        raise ValueError(
            "execute_backlog_row payload must be an object with required fields: "
            + missing
        )
    if missing:
        raise ValueError(
            "execute_backlog_row payload missing required fields: "
            + ", ".join(missing)
        )
    identity_blockers = _execute_backlog_route_identity_blockers(payload)
    if identity_blockers:
        blocked_fields = ", ".join(
            str(item.get("field") or "") for item in identity_blockers if item.get("field")
        )
        raise ValueError(
            "execute_backlog_row payload carries stale or superseded route identity: "
            + blocked_fields
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


def _consumer_session_diagnostic(
    session: dict[str, Any],
    command: dict[str, Any],
) -> dict[str, Any]:
    session_id = str(session.get("session_id") or "")
    computed_status = str(session.get("computed_status") or "")
    capabilities = session.get("capabilities") if isinstance(session.get("capabilities"), dict) else {}
    command_type = str(command.get("command_type") or "")
    connected = computed_status in {SESSION_STATUS_ACTIVE, "idle"}
    target_allowed = _command_target_allows(command, session_id)
    capability_allowed = capabilities_allow(
        capabilities,
        ACTION_COMMAND_CLAIM,
        command_type=command_type,
    )
    reasons: list[str] = []
    if not connected:
        reasons.append(f"session_{computed_status or 'unavailable'}")
    if not target_allowed:
        reasons.append("target_session_mismatch")
    if not capability_allowed:
        reasons.append("capability_missing")
    return {
        "session_id": session_id,
        "observer_kind": str(session.get("observer_kind") or ""),
        "session_label": str(session.get("session_label") or ""),
        "computed_status": computed_status,
        "last_seen_at": str(session.get("last_seen_at") or ""),
        "target_allowed": target_allowed,
        "capability_allowed": capability_allowed,
        "claim_eligible": connected and target_allowed and capability_allowed,
        "unavailable_reasons": reasons,
    }


def _target_session_recovery_status(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    target_session_id: str,
    now: str,
) -> str:
    target = (target_session_id or "").strip()
    if not target:
        return ""
    row = _raw_session_row(conn, target)
    if row is None or str(row["project_id"]) != (project_id or "").strip():
        return "missing"
    return computed_session_status(row, now=now)


def _active_recovery_session_ids(
    sessions: list[dict[str, Any]],
    command: dict[str, Any],
) -> list[str]:
    command_type = str(command.get("command_type") or "")
    active_ids: list[str] = []
    for session in sessions:
        if str(session.get("computed_status") or "") not in {SESSION_STATUS_ACTIVE, "idle"}:
            continue
        capabilities = (
            session.get("capabilities")
            if isinstance(session.get("capabilities"), dict)
            else {}
        )
        if capabilities_allow(
            capabilities,
            ACTION_COMMAND_TAKEOVER,
            command_type=command_type,
        ):
            active_ids.append(str(session.get("session_id") or ""))
    return active_ids


def _newer_notified_command_summaries(
    commands: list[dict[str, Any]],
    diagnosed: dict[str, Any],
    *,
    now: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    diagnosed_age = _command_notified_age_sec(diagnosed, now=now)
    if diagnosed_age is None:
        return []
    newer: list[dict[str, Any]] = []
    diagnosed_id = str(diagnosed.get("command_id") or "")
    for command in commands:
        if str(command.get("command_id") or "") == diagnosed_id:
            continue
        age = _command_notified_age_sec(command, now=now)
        if age is None or age >= diagnosed_age:
            continue
        summary = _observer_command_summary_item(command, now=now)
        newer.append(
            {
                "command_id": summary["command_id"],
                "command_type": summary["command_type"],
                "status": summary["status"],
                "backlog_id": summary["backlog_id"],
                "target_session_id": summary["target_session_id"],
                "notified_age_sec": summary["notified_age_sec"],
            }
        )
        if len(newer) >= max(1, int(limit or 10)):
            break
    return newer


def _observer_command_summary_item(command: dict[str, Any], *, now: str) -> dict[str, Any]:
    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
    return {
        "command_id": str(command.get("command_id") or ""),
        "command_type": str(command.get("command_type") or ""),
        "status": str(command.get("status") or ""),
        "backlog_id": str(payload.get("backlog_id") or ""),
        "target_session_id": str(command.get("target_session_id") or ""),
        "notified_at": str(command.get("notified_at") or ""),
        "created_at": str(command.get("created_at") or ""),
        "notified_age_sec": _command_notified_age_sec(command, now=now),
        "contract_handoff_projection": _observer_command_contract_projection(
            command,
            status=str(command.get("status") or ""),
            watermark=str(command.get("notified_at") or command.get("created_at") or now),
        ),
    }


def _latest_notified_execute_commands(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM observer_command_queue
            WHERE project_id = ?
              AND command_type = ?
              AND status = ?
            ORDER BY notified_at DESC, created_at DESC
            LIMIT ?""",
        (
            (project_id or "").strip(),
            COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
            COMMAND_STATUS_NOTIFIED,
            max(1, min(int(limit or 50), 1000)),
        ),
    ).fetchall()
    return [_command_row_to_dict(row) for row in rows]


def observer_command_consumer_recovery(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    now: str | None = None,
    threshold_sec: int = NOTIFIED_UNCLAIMED_RECOVERY_THRESHOLD_SEC,
    limit: int = 50,
) -> dict[str, Any]:
    """Diagnose route-bound execute commands that were notified but unclaimed."""
    ensure_schema(conn)
    pid = (project_id or "").strip()
    timestamp = now or _utc_now()
    threshold = max(0, int(threshold_sec))
    commands = _latest_notified_execute_commands(conn, project_id=pid, limit=limit)
    latest = commands[0] if commands else None
    stale_commands = [
        command
        for command in commands
        if (_command_notified_age_sec(command, now=timestamp) or 0.0) >= threshold
    ]
    diagnosed = max(
        stale_commands or ([latest] if latest else []),
        key=lambda item: _command_notified_age_sec(item, now=timestamp) or -1.0,
        default=None,
    )
    base: dict[str, Any] = {
        "schema_version": OBSERVER_COMMAND_CONSUMER_RECOVERY_SCHEMA_VERSION,
        "project_id": pid,
        "generated_at": timestamp,
        "threshold_sec": threshold,
        "notified_execute_backlog_row_count": len(commands),
        "stale_notified_execute_backlog_row_count": len(stale_commands),
        "latest_notified_command_age_sec": (
            _command_notified_age_sec(latest, now=timestamp) if latest else None
        ),
        "latest_notified_command": (
            _observer_command_summary_item(latest, now=timestamp) if latest else None
        ),
        "diagnosed_command": (
            _observer_command_summary_item(diagnosed, now=timestamp) if diagnosed else None
        ),
        "recovery_required": bool(diagnosed and diagnosed in stale_commands),
    }
    if not diagnosed:
        base.update(
            {
                "status": "idle",
                "classification": "no_notified_execute_backlog_row",
                "next_legal_action": {
                    "action": "none",
                    "description": "No notified execute_backlog_row command is waiting for an observer consumer.",
                },
            }
        )
        return base

    payload = diagnosed.get("payload") if isinstance(diagnosed.get("payload"), dict) else {}
    missing = _execute_backlog_payload_missing_fields(payload)
    route_identity_blockers = _execute_backlog_route_identity_blockers(payload)
    sessions = list_sessions(conn, project_id=pid, limit=100, now=timestamp)
    consumers = [_consumer_session_diagnostic(session, diagnosed) for session in sessions]
    connected = [
        consumer
        for consumer in consumers
        if consumer.get("computed_status") in {SESSION_STATUS_ACTIVE, "idle"}
    ]
    eligible = [consumer for consumer in consumers if consumer.get("claim_eligible")]
    target_session_id = str(diagnosed.get("target_session_id") or "")
    target_session = None
    target_session_status = ""
    if target_session_id:
        target_session_status = _target_session_recovery_status(
            conn,
            project_id=pid,
            target_session_id=target_session_id,
            now=timestamp,
        )
        target_session = next(
            (
                consumer
                for consumer in consumers
                if consumer.get("session_id") == target_session_id
            ),
            None,
        )
        if target_session is None:
            row = _raw_session_row(conn, target_session_id)
            if row is not None and str(row["project_id"]) == pid:
                item = _session_row_to_dict(row)
                item["computed_status"] = computed_session_status(row, now=timestamp)
                target_session = _consumer_session_diagnostic(item, diagnosed)
                target_session_status = str(item.get("computed_status") or target_session_status)

    base.update(
        {
            "target_session_id": target_session_id,
            "target_session": target_session,
            "target_session_computed_status": target_session_status,
            "connected_consumer_count": len(connected),
            "eligible_consumer_count": len(eligible),
            "eligible_session_ids": [str(item.get("session_id") or "") for item in eligible],
            "consumer_sessions": consumers,
        }
    )

    if missing or route_identity_blockers:
        blocker = _execute_backlog_payload_blocker(
            diagnosed,
            now=timestamp,
            missing=missing,
            route_identity_blockers=route_identity_blockers,
        )
        base.update(
            {
                "status": "blocked",
                "classification": "claim_validation_error",
                "blocker": blocker,
                "next_legal_action": {
                    **dict(blocker.get("next_legal_action") or {}),
                    "command_id": str(diagnosed.get("command_id") or ""),
                    "requires_session_token": True,
                },
            }
        )
        return base

    target_recovery_required = bool(
        target_session_id
        and str(diagnosed.get("command_type") or "") == COMMAND_TYPE_EXECUTE_BACKLOG_ROW
        and str(diagnosed.get("status") or "") == COMMAND_STATUS_NOTIFIED
        and not str(diagnosed.get("claimed_by_session_id") or "")
        and target_session_status in TARGET_SESSION_RECOVERY_STATUSES
    )
    if target_recovery_required:
        active_recovery_session_ids = _active_recovery_session_ids(sessions, diagnosed)
        notified_age = _command_notified_age_sec(diagnosed, now=timestamp)
        affected_newer = _newer_notified_command_summaries(
            commands,
            diagnosed,
            now=timestamp,
        )
        if active_recovery_session_ids:
            next_legal_action = {
                "tool": "observer_command_takeover",
                "action": "retarget_and_claim",
                "description": (
                    "An active observer may recover this stale targeted command "
                    "through takeover, then complete or fail it with evidence."
                ),
                "command_id": str(diagnosed.get("command_id") or ""),
                "target_session_id": target_session_id,
                "eligible_session_ids": active_recovery_session_ids,
                "requires_session_token": True,
            }
        else:
            next_legal_action = {
                "tool": "observer_session_register",
                "action": "register_active_observer_then_recover",
                "description": (
                    "Register or heartbeat an active observer session, then recover "
                    "this stale targeted command through observer_command_takeover."
                ),
                "followup_tool": "observer_command_takeover",
                "command_id": str(diagnosed.get("command_id") or ""),
                "target_session_id": target_session_id,
                "requires_session_token": True,
            }
        base.update(
            {
                "status": "blocked",
                "classification": "target_session_recovery_required",
                "recovery_required": True,
                "blocked_command_id": str(diagnosed.get("command_id") or ""),
                "affected_newer_notified_commands": affected_newer,
                "blocker": {
                    "blocker_id": "observer_command_target_session_recovery_required",
                    "observer_command_id": str(diagnosed.get("command_id") or ""),
                    "blocked_command_id": str(diagnosed.get("command_id") or ""),
                    "target_session_id": target_session_id,
                    "target_session_status": target_session_status,
                    "target_session": target_session,
                    "notified_age_sec": notified_age,
                    "threshold_sec": threshold,
                    "active_recovery_session_ids": active_recovery_session_ids,
                    "affected_newer_notified_command_count": len(affected_newer),
                    "reason": (
                        "The command targets an observer session that is unavailable; "
                        "a different active observer cannot claim it normally and "
                        "must recover it through observer_command_takeover."
                    ),
                },
                "next_legal_action": next_legal_action,
            }
        )
        return base

    if not base["recovery_required"]:
        base.update(
            {
                "status": "waiting_for_threshold",
                "classification": "notified_within_threshold",
                "next_legal_action": {
                    "tool": "observer_command_claim",
                    "description": (
                        "An eligible observer may claim now; otherwise keep watching "
                        "until the notified-unclaimed threshold is exceeded."
                    ),
                    "command_id": str(diagnosed.get("command_id") or ""),
                    "eligible_session_ids": [str(item.get("session_id") or "") for item in eligible],
                    "requires_session_token": True,
                },
            }
        )
        return base

    if not connected:
        base.update(
            {
                "status": "blocked",
                "classification": "no_active_consumer_session",
                "blocker": {
                    "blocker_id": "observer_command_no_active_consumer_session",
                    "observer_command_id": str(diagnosed.get("command_id") or ""),
                    "target_session_id": target_session_id,
                    "notified_age_sec": _command_notified_age_sec(diagnosed, now=timestamp),
                    "threshold_sec": threshold,
                    "reason": (
                        "No active or idle observer session is available to claim "
                        "the notified execute_backlog_row command."
                    ),
                },
                "next_legal_action": {
                    "tool": "observer_session_register",
                    "description": (
                        "Register or heartbeat an observer session, then call "
                        "observer_command_claim for the notified command."
                    ),
                    "followup_tool": "observer_command_claim",
                    "command_id": str(diagnosed.get("command_id") or ""),
                    "requires_session_token": True,
                },
            }
        )
        return base

    if target_session_id and not eligible:
        base.update(
            {
                "status": "blocked",
                "classification": "target_session_unavailable",
                "blocker": {
                    "blocker_id": "observer_command_target_session_unavailable",
                    "observer_command_id": str(diagnosed.get("command_id") or ""),
                    "target_session_id": target_session_id,
                    "target_session": target_session,
                    "reason": (
                        "The command is targeted, but the target session is not "
                        "currently eligible to claim it."
                    ),
                },
                "next_legal_action": {
                    "tool": "observer_session_heartbeat",
                    "description": (
                        "Recover the target observer session, or enqueue a new "
                        "untargeted/retargeted command with the same route evidence."
                    ),
                    "target_session_id": target_session_id,
                    "requires_session_token": True,
                },
            }
        )
        return base

    if not eligible:
        base.update(
            {
                "status": "blocked",
                "classification": "claim_validation_error",
                "blocker": {
                    "blocker_id": "observer_command_no_eligible_consumer_session",
                    "observer_command_id": str(diagnosed.get("command_id") or ""),
                    "reason": (
                        "Observer sessions exist, but none can claim this command "
                        "with the required action, command type, and target."
                    ),
                },
                "next_legal_action": {
                    "tool": "observer_session_register",
                    "description": (
                        "Register an observer session with observer_command_claim "
                        "and execute_backlog_row capability, then claim the command."
                    ),
                    "followup_tool": "observer_command_claim",
                    "command_id": str(diagnosed.get("command_id") or ""),
                    "requires_session_token": True,
                },
            }
        )
        return base

    base.update(
        {
            "status": "action_required",
            "classification": "eligible_consumer_available",
            "next_legal_action": {
                "tool": "observer_command_claim",
                "description": (
                    "An eligible observer session can claim this route-bound "
                    "execute_backlog_row command now."
                ),
                "command_id": str(diagnosed.get("command_id") or ""),
                "eligible_session_ids": [str(item.get("session_id") or "") for item in eligible],
                "requires_session_token": True,
            },
        }
    )
    return base


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


def command_summary(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    limit: int = 50,
    now: str | None = None,
) -> dict[str, Any]:
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
        "observer_consumer_recovery": observer_command_consumer_recovery(
            conn,
            project_id=project_id,
            now=now,
            limit=limit,
        ),
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
                "observer_consumer_recovery": observer_command_consumer_recovery(
                    conn,
                    project_id=pid,
                    now=timestamp,
                ),
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

    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
    missing = (
        _execute_backlog_payload_missing_fields(payload)
        if str(command.get("command_type") or "") == COMMAND_TYPE_EXECUTE_BACKLOG_ROW
        else []
    )
    route_identity_blockers = (
        _execute_backlog_route_identity_blockers(payload)
        if str(command.get("command_type") or "") == COMMAND_TYPE_EXECUTE_BACKLOG_ROW
        else []
    )
    if missing or route_identity_blockers:
        blocker = _execute_backlog_payload_blocker(
            command,
            now=timestamp,
            missing=missing,
            route_identity_blockers=route_identity_blockers,
        )
        result_payload = _command_result(command)
        result_payload["ok"] = False
        result_payload["claim_blocker"] = blocker
        cursor = conn.execute(
            """UPDATE observer_command_queue
                  SET status = ?,
                      claimed_by_session_id = ?,
                      claimed_at = ?,
                      completed_at = ?,
                      result_json = ?,
                      error = ?
                WHERE project_id = ?
                  AND command_id = ?
                  AND status IN (?, ?)
                  AND (target_session_id = '' OR target_session_id = ?)""",
            (
                COMMAND_STATUS_FAILED,
                sid,
                timestamp,
                timestamp,
                _json_dumps(result_payload),
                blocker["blocker_id"],
                pid,
                command["command_id"],
                COMMAND_STATUS_QUEUED,
                COMMAND_STATUS_NOTIFIED,
                sid,
            ),
        )
        conn.commit()
        if cursor.rowcount != 1:
            raise ObserverCommandConflict("observer command claim validation lost race")
        return {
            "ok": True,
            "project_id": pid,
            "observer_session_id": sid,
            "command": get_command(conn, project_id=pid, command_id=command["command_id"]),
            "empty": False,
            "claim_blocker": blocker,
        }

    result_payload = _command_result(command)
    if str(command.get("command_type") or "") == COMMAND_TYPE_EXECUTE_BACKLOG_ROW:
        result_payload["observer_claim_evidence"] = _execute_backlog_claim_evidence(
            command,
            session_id=sid,
            now=timestamp,
        )

    cursor = conn.execute(
        """UPDATE observer_command_queue
              SET status = ?, claimed_by_session_id = ?, claimed_at = ?, result_json = ?
            WHERE project_id = ?
              AND command_id = ?
              AND status IN (?, ?)
              AND (target_session_id = '' OR target_session_id = ?)""",
        (
            COMMAND_STATUS_CLAIMED,
            sid,
            timestamp,
            _json_dumps(result_payload),
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
        takeover_timeout = _claimed_execute_takeover_timeout(conn, command, now=now)
        if takeover_timeout:
            return str(takeover_timeout.get("status") or "")
    return owner_status


def _targeted_notified_takeover_status(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    command: dict[str, Any],
    now: str,
) -> str:
    if str(command.get("command_type") or "") != COMMAND_TYPE_EXECUTE_BACKLOG_ROW:
        return ""
    if str(command.get("status") or "") != COMMAND_STATUS_NOTIFIED:
        return ""
    if str(command.get("claimed_by_session_id") or ""):
        return ""
    target_session_id = str(command.get("target_session_id") or "")
    if not target_session_id:
        return ""
    target_status = _target_session_recovery_status(
        conn,
        project_id=project_id,
        target_session_id=target_session_id,
        now=now,
    )
    return target_status if target_status in TARGET_SESSION_RECOVERY_STATUSES else ""


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
    target_recovery_status = _targeted_notified_takeover_status(
        conn,
        project_id=pid,
        command=command,
        now=timestamp,
    )
    if command.get("status") in TERMINAL_COMMAND_STATUSES:
        raise ObserverCommandConflict("observer command is already terminal")
    if target_recovery_status:
        previous_target_session_id = str(command.get("target_session_id") or "")
        takeover = {
            "previous_session_id": previous_target_session_id,
            "previous_target_session_id": previous_target_session_id,
            "previous_session_status": target_recovery_status,
            "target_session_status": target_recovery_status,
            "new_target_session_id": sid,
            "recovery_kind": "target_session_reassignment",
            "reason": takeover_reason,
            "taken_over_at": timestamp,
        }
        result_payload = _command_result(command)
        result_payload["takeover"] = takeover
        result_payload["takeover_status"] = {
            "status": target_recovery_status,
            "takeover_eligible": True,
            "taken_over_at": timestamp,
            "reason": takeover_reason,
            "recovery_kind": "target_session_reassignment",
        }
        result_payload["target_session_recovery"] = {
            "schema_version": "observer_command_target_session_recovery.v1",
            "blocked_command_id": cid,
            "target_session_id": previous_target_session_id,
            "target_session_status": target_recovery_status,
            "recovered_by_session_id": sid,
            "recovered_at": timestamp,
            "next_expected_action": "complete_or_fail_with_evidence",
        }
        cursor = conn.execute(
            """UPDATE observer_command_queue
                  SET status = ?,
                      target_session_id = ?,
                      claimed_by_session_id = ?,
                      claimed_at = ?,
                      result_json = ?
                WHERE project_id = ?
                  AND command_id = ?
                  AND status = ?
                  AND claimed_by_session_id = ''
                  AND target_session_id = ?""",
            (
                COMMAND_STATUS_CLAIMED,
                sid,
                sid,
                timestamp,
                _json_dumps(result_payload),
                pid,
                cid,
                COMMAND_STATUS_NOTIFIED,
                previous_target_session_id,
            ),
        )
        conn.commit()
        if cursor.rowcount != 1:
            raise ObserverCommandConflict("observer command target recovery lost race")
        return {
            "ok": True,
            "project_id": pid,
            "observer_session_id": sid,
            "command": get_command(conn, project_id=pid, command_id=cid),
            "takeover": takeover,
        }
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
        CLAIMED_TO_PROGRESS_TIMEOUT_STATUS,
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
    takeover_timeout = _claimed_execute_takeover_timeout(conn, command, now=timestamp)
    if previous_status == CLAIMED_TO_STARTUP_TIMEOUT_STATUS:
        takeover.update(
            {
                "claimed_at": str(command.get("claimed_at") or ""),
                "timeout_sec": CLAIMED_TO_STARTUP_TIMEOUT_SEC,
                "startup_evidence": "missing",
            }
        )
    if previous_status == CLAIMED_TO_PROGRESS_TIMEOUT_STATUS:
        no_progress_timeout = dict(takeover_timeout)
        timeline_event = _record_no_progress_timeout_event(
            conn,
            project_id=pid,
            command=command,
            timeout=no_progress_timeout,
            now=timestamp,
        )
        no_progress_timeout["timeline_event"] = timeline_event
        takeover.update(
            {
                "claimed_at": str(command.get("claimed_at") or ""),
                "timeout_sec": CLAIMED_TO_PROGRESS_TIMEOUT_SEC,
                "timeout_kind": "no_progress_timeout",
                "startup_evidence": "present",
                "progress_evidence": "missing",
                "progress_watchdog": no_progress_timeout.get("progress_watchdog") or {},
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
    if previous_status == CLAIMED_TO_PROGRESS_TIMEOUT_STATUS:
        result_payload["progress_watchdog"] = takeover.get("progress_watchdog") or {}
        result_payload["no_progress_timeout"] = no_progress_timeout

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
    if str(command.get("command_type") or "") == COMMAND_TYPE_EXECUTE_BACKLOG_ROW:
        _hydrate_result_with_persisted_mf_sub_evidence(
            conn,
            project_id=pid,
            command=command,
            result=result_payload,
        )
    if (
        str(command.get("command_type") or "") == COMMAND_TYPE_EXECUTE_BACKLOG_ROW
        and not _result_has_startup_or_blocker_evidence(result_payload)
    ):
        terminal_projection = _command_terminal_projection_from_result(command, result_payload)
        if terminal_projection.get("passed"):
            _attach_command_terminal_projection(result_payload, terminal_projection)
        else:
            observer_only_projection = {}
            if not terminal_projection:
                observer_only_projection = _observer_only_monitor_terminal_projection(
                    command,
                    result_payload,
                    now=timestamp,
                )
            if observer_only_projection.get("passed"):
                result_payload["ok"] = True
                result_payload["status"] = "completed"
                _attach_command_terminal_projection(result_payload, observer_only_projection)
            else:
                if terminal_projection:
                    _attach_command_terminal_projection(result_payload, terminal_projection)
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
                    "terminal_contract_projection": terminal_projection,
                }
    if (
        str(command.get("command_type") or "") == COMMAND_TYPE_EXECUTE_BACKLOG_ROW
        and _result_has_canonical_close_evidence(result_payload)
        and "terminal_contract_projection" not in result_payload
    ):
        terminal_projection = _command_terminal_projection_from_result(command, result_payload)
        if terminal_projection:
            _attach_command_terminal_projection(result_payload, terminal_projection)
    if (
        str(command.get("command_type") or "") == COMMAND_TYPE_EXECUTE_BACKLOG_ROW
        and _contains_actual_startup_evidence(result_payload)
        and not _result_has_canonical_close_evidence(result_payload)
        and not _result_is_terminal_blocked(result_payload)
    ):
        progress_command = dict(command)
        progress_command["result"] = result_payload
        progress_command["result_json"] = _json_dumps(result_payload)
        progress_watchdog = _command_first_progress_evidence(
            conn,
            progress_command,
            now=timestamp,
        )
        if not progress_watchdog.get("present"):
            blocker = _missing_first_progress_blocker(
                command,
                now=timestamp,
                progress_watchdog=progress_watchdog,
            )
            result_payload["ok"] = False
            result_payload["progress_watchdog"] = progress_watchdog
            result_payload["terminal_blocker"] = blocker
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
                "terminal_blocker": blocker,
            }
    if (
        str(command.get("command_type") or "") == COMMAND_TYPE_EXECUTE_BACKLOG_ROW
        and _result_is_terminal_blocked(result_payload)
    ):
        projection = _command_terminal_projection_from_result(command, result_payload)
        if projection:
            _attach_command_terminal_projection(result_payload, projection)
        blocker = _terminal_blocker_from_result(result_payload)
        blocker_id = str(blocker.get("blocker_id") or "")
        result_payload["ok"] = False
        result_payload.setdefault("status", "blocked")
        conn.execute(
            """UPDATE observer_command_queue
                  SET status = ?, completed_at = ?, result_json = ?, error = ?
                WHERE project_id = ? AND command_id = ?""",
            (
                COMMAND_STATUS_FAILED,
                timestamp,
                _json_dumps(result_payload),
                blocker_id or "blocked",
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
            "terminal_blocker": blocker,
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
