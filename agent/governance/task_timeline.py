"""Append-only task implementation timeline.

Backlog rows describe the intended work. Task timeline rows describe execution
facts proposed by agents, verified by executors/gates, and accepted by
observers. This module centralizes writes so parallel agents do not scatter
SQLite mutations across the codebase.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import sqlite3
import threading
import time
from typing import Any, Mapping

log = logging.getLogger(__name__)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS task_timeline_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id           TEXT NOT NULL,
    backlog_id           TEXT NOT NULL DEFAULT '',
    mf_id                TEXT NOT NULL DEFAULT '',
    task_id              TEXT NOT NULL DEFAULT '',
    attempt_num          INTEGER NOT NULL DEFAULT 0,
    event_type           TEXT NOT NULL,
    phase                TEXT NOT NULL DEFAULT '',
    event_kind           TEXT NOT NULL DEFAULT '',
    scenario_id          TEXT NOT NULL DEFAULT '',
    parent_event_id      INTEGER NOT NULL DEFAULT 0,
    correlation_id       TEXT NOT NULL DEFAULT '',
    severity             TEXT NOT NULL DEFAULT '',
    decision             TEXT NOT NULL DEFAULT '',
    schema_version       INTEGER NOT NULL DEFAULT 2,
    actor                TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL DEFAULT '',
    payload_json         TEXT NOT NULL DEFAULT '{}',
    verification_json    TEXT NOT NULL DEFAULT '{}',
    artifact_refs_json   TEXT NOT NULL DEFAULT '{}',
    trace_id             TEXT NOT NULL DEFAULT '',
    commit_sha           TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL
);
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_task_timeline_task
    ON task_timeline_events(project_id, task_id, attempt_num, id);
CREATE INDEX IF NOT EXISTS idx_task_timeline_backlog
    ON task_timeline_events(project_id, backlog_id, id);
CREATE INDEX IF NOT EXISTS idx_task_timeline_trace
    ON task_timeline_events(project_id, trace_id, id);
CREATE INDEX IF NOT EXISTS idx_task_timeline_scenario
    ON task_timeline_events(project_id, scenario_id, id);
CREATE INDEX IF NOT EXISTS idx_task_timeline_correlation
    ON task_timeline_events(project_id, correlation_id, id);
CREATE INDEX IF NOT EXISTS idx_task_timeline_kind
    ON task_timeline_events(project_id, event_kind, phase, id);
"""

TIMELINE_SCHEMA_VERSION = 2

_V2_COLUMNS = {
    "phase": "TEXT NOT NULL DEFAULT ''",
    "event_kind": "TEXT NOT NULL DEFAULT ''",
    "scenario_id": "TEXT NOT NULL DEFAULT ''",
    "parent_event_id": "INTEGER NOT NULL DEFAULT 0",
    "correlation_id": "TEXT NOT NULL DEFAULT ''",
    "severity": "TEXT NOT NULL DEFAULT ''",
    "decision": "TEXT NOT NULL DEFAULT ''",
    "schema_version": f"INTEGER NOT NULL DEFAULT {TIMELINE_SCHEMA_VERSION}",
}

MF_TEST_SCENARIO_POLICIES = {
    "none",
    "reuse_existing",
    "new_scenario_required",
}

MF_TEST_SCENARIO_POLICY_MODE = "observer_configured"

MF_TEST_SCENARIO_E2E_DECISIONS = {
    "e2e_current",
    "e2e_added",
    "e2e_deferred",
    "e2e_not_applicable",
}

MF_CLOSE_REQUIRED_EVENT_KINDS = {
    "implementation",
    "verification",
    "close_ready",
}

MF_CLOSE_PASS_STATUSES = {
    "accepted",
    "ok",
    "passed",
    "succeeded",
}

MF_CONTRACT_SCHEMA_VERSION = "mf_contract_gate.v1"
MF_CONTRACT_PROJECTION_SCHEMA_VERSION = "mf_contract_projection.v1"
MF_SUBAGENT_FINISH_GATE_CLOSE_PROJECTION_SCHEMA_VERSION = (
    "mf_subagent_finish_gate_close_projection.v1"
)
MF_OBSERVER_COMMAND_TERMINAL_PROJECTION_SCHEMA_VERSION = "observer_command_terminal_projection.v1"
MF_SUBAGENT_READ_RECEIPT_GATE_SCHEMA_VERSION = "mf_subagent_read_receipt_gate.v1"
MF_LANE_OWNERSHIP_SCHEMA_VERSION = "mf_lane_ownership_gate.v1"
MF_BOUNDED_SUBAGENT_LANE_ID = "bounded_implementation_subagent"
MF_BOUNDED_SUBAGENT_DISPATCH_ID = f"{MF_BOUNDED_SUBAGENT_LANE_ID}.dispatch"
MF_BOUNDED_SUBAGENT_REVIEW_READY_ID = f"{MF_BOUNDED_SUBAGENT_LANE_ID}.review_ready"

MF_BLOCKER_RESOLUTION_GATE_SCHEMA_VERSION = "mf_blocker_resolution_gate.v1"
MF_CROSS_REF_GATE_SCHEMA_VERSION = "mf_close_cross_ref_gate.v1"
MF_STALE_ROUTE_EVIDENCE_GATE_SCHEMA_VERSION = "mf_stale_route_evidence_gate.v1"
MF_APPROVAL_SCOPE_GATE_SCHEMA_VERSION = "mf_close_approval_scope_gate.v1"
MF_COMMAND_DISPOSITION_GATE_SCHEMA_VERSION = "mf_close_command_disposition_gate.v1"
MF_FIXED_CLOSE_WAIVER_ALERT_SCHEMA_VERSION = "mf_fixed_close_waiver_alert.v1"
MF_TIMELINE_REPAIR_SUMMARY_SCHEMA_VERSION = "mf_close_timeline_repair_summary.v1"

# An explicit, recorded close-waiver marker. Distinct from a route_context
# waiver: this authorizes the backlog_close action itself despite a failing
# precheck. It must be visible on the timeline (it is never inferred).
MF_CLOSE_WAIVER_EVENT_TOKENS = (
    "backlog_close_waiver",
    "close_gate_waiver",
    "close_waiver",
    "mf_close_waiver",
)
# Tokens, found in cited human-approval text, that explicitly EXCLUDE the
# backlog_close action. An approval whose own scope forbids close must not
# authorize a close.
MF_APPROVAL_CLOSE_EXCLUSION_TOKENS = (
    "does_not_authorize_backlog_close",
    "does_not_authorize_close",
    "not_authorize_backlog_close",
    "review_ready_only",
    "review_ready_not_close",
    "no_backlog_close",
    "not_for_backlog_close",
    "excludes_backlog_close",
    "close_not_authorized",
    "backlog_close_not_authorized",
)
# Fields on an approval/close-evidence event that carry the human-approval scope
# text we scan for an explicit close exclusion.
MF_APPROVAL_SCOPE_FIELDS = {
    "approval_scope",
    "approval_text",
    "human_approval",
    "human_approval_text",
    "human_approval_scope",
    "approved_scope",
    "operator_approval_text",
    "operator_approval_scope",
    "authorizes",
    "authorized_actions",
    "approval_note",
    "approval_notes",
    "scope_note",
}
# Event kinds that may cite a human approval for the close.
MF_APPROVAL_BEARING_KINDS = (
    "close_ready",
    "human_approval",
    "operator_approval",
    "backlog_close",
    "close_approval",
)
# Observer-command disposition tracking for the close gate (criterion 3): the
# originating command must be terminal (completed / co-resolved) before close.
MF_COMMAND_TERMINAL_STATUSES = {
    "completed",
    "complete",
    "failed",
    "cancelled",
    "canceled",
    "resolved",
    "co_resolved",
    "co_resolved_with_close",
    "terminal",
    "disposed",
}
MF_COMMAND_CLAIMED_STATUSES = {
    "claimed",
    "running",
    "in_progress",
    "notified",
    "queued",
}
# Event kinds that carry the originating-observer-command disposition.
MF_COMMAND_DISPOSITION_KINDS = (
    "observer_command",
    "observer_command_claim",
    "observer_command_complete",
    "observer_command_fail",
    "observer_command_disposition",
    "observer_command_terminal",
)

# Statuses an observer must NOT apply to a judge-finding blocker resolution by
# fiat. Only a judge actor may accept/resolve such a finding.
MF_JUDGE_BLOCKER_ACCEPT_STATUSES = {
    "accepted",
    "resolved",
    "cleared",
    "closed",
    "approved",
    "passed",
    "ok",
}
MF_JUDGE_ACTOR_TOKENS = ("judge", "judge_review", "judger")
MF_OBSERVER_ACTOR_TOKENS = ("observer", "observer_coordinator", "coordinator")
# When an observer touches a judge finding, the only legal recorded state is a
# proposal pending independent judge review.
MF_OBSERVER_FORCED_BLOCKER_STATUS = "pending_judge_review"
# Evidence kinds whose acceptance must be invalidated when recorded under a
# superseded/stale route identity (route repair forces re-recording).
MF_STALE_ROUTE_EVIDENCE_KINDS = (
    "mf_subagent_read_receipt",
    "mf_subagent_startup",
    "mf_subagent_startup_adoption",
    "mf_subagent_dispatch",
    "bounded_implementation_worker_dispatch",
    "close_ready",
)
# Identity dimensions a close-evidence ref must share with the row it closes.
# (Route-identity supersession/repair changes route_id/prompt_contract_id under
# the same backlog/scope; that lineage is handled by the stale-route gate, so
# the cross-ref gate inferring identity from evidence keys only on backlog/scope.
# An explicit row_identity may still constrain route_id/prompt_contract_id.)
MF_CROSS_REF_IDENTITY_FIELDS = (
    "backlog_id",
    "route_id",
    "prompt_contract_id",
    "scope",
)
MF_CROSS_REF_INFERRED_IDENTITY_FIELDS = (
    "backlog_id",
    "scope",
)

MF_ROUTE_CONTEXT_GATE_SCHEMA_VERSION = "mf_route_context_consumption_gate.v1"
MF_ROUTE_OWNED_SOURCE_EVENT_GATE_SCHEMA_VERSION = "mf_route_owned_source_event_gate.v1"
MF_CLOSE_MISSING_GROUPS_SCHEMA_VERSION = "mf_close_missing_evidence_groups.v1"
MF_ROUTE_CONTEXT_REMINDER_SCHEMA_VERSION = "mf_route_context_reminder.v1"
MF_ROUTE_GUIDANCE_TEMPLATE_ID = "mf_workflow_runtime.v1"
MF_ROUTE_GUIDANCE_ALLOWED_STAGES = (
    "dispatch",
    "startup_gate",
    "implementation_wait",
    "handoff_gate",
)
MF_ROUTE_IDENTITY_FIELDS = (
    "route_context_hash",
    "prompt_contract_id",
)
MF_ROUTE_OPTIONAL_IDENTITY_FIELDS = ("prompt_contract_hash",)
MF_ROUTE_ATTEMPT_LINEAGE_FIELDS = (
    "runtime_context_id",
    "task_id",
    "parent_task_id",
    "worker_slot_id",
    "fence_token",
)
MF_ROUTE_ATTEMPT_LINEAGE_FILTER_FIELDS = (
    "runtime_context_id",
    "task_id",
    "parent_task_id",
)
RUNTIME_CONTEXT_TIMELINE_IDENTITY_FIELDS = (
    "runtime_context_id",
    "task_id",
    "parent_task_id",
    "worker_slot_id",
    "fence_token",
)
RUNTIME_CONTEXT_TIMELINE_ROUTE_FIELDS = (
    "route_context_hash",
    "prompt_contract_id",
    "prompt_contract_hash",
)
RUNTIME_CONTEXT_TIMELINE_STARTUP_FIELDS = (
    *RUNTIME_CONTEXT_TIMELINE_IDENTITY_FIELDS,
    *RUNTIME_CONTEXT_TIMELINE_ROUTE_FIELDS,
    "actual_cwd",
    "actual_git_root",
    "branch",
    "head_commit",
)
RUNTIME_CONTEXT_TIMELINE_READ_RECEIPT_FIELDS = (
    *RUNTIME_CONTEXT_TIMELINE_IDENTITY_FIELDS,
    *RUNTIME_CONTEXT_TIMELINE_ROUTE_FIELDS,
    "read_receipt_hash",
)
MF_ROUTE_CONTEXT_REQUIRED_EVIDENCE_IDS = (
    "route_context",
    "route_action_precheck",
    "bounded_implementation_worker_dispatch",
    "mf_subagent_startup",
)
MF_ROUTE_CONTEXT_INDEPENDENT_VERIFICATION_ID = "independent_verification_lane"
MF_ROUTE_CONTEXT_ARCHITECTURE_REVIEW_ID = "architecture_review_lane"
MF_ROUTE_CONTEXT_PASS_STATUSES = {
    *MF_CLOSE_PASS_STATUSES,
    "allow",
    "allowed",
    "approved",
}
MF_ROUTE_SERVICE_REQUIREMENTS = (
    "route_context",
    "route_action_precheck",
)
MF_ROUTE_WORKER_REQUIREMENTS = (
    "bounded_implementation_worker_dispatch",
    "mf_subagent_startup",
)
# Existing-worktree adoption equivalence for the mf_subagent_startup
# requirement (AC-CLOSE-LINEAGE-EXISTING-WORKTREE-ADOPTION-MODE-20260610).
# Markers are compared in _route_marker-normalized form.
MF_STARTUP_ADOPTION_EVENT_MARKERS = {
    "mf_subagent_startup_adoption",
    "mf_subagent_startup_adoption_gate",
}
MF_BRANCH_ADOPTION_MODES = {"existing_branch", "adopt_existing_branch"}
MF_ROUTE_IDENTITY_REQUIREMENTS = (
    "route_identity_mismatch",
    "same_route_identity",
    "route_identity_cleanup",
)
MF_ROUTE_IDENTITY_CLEANUP_MARKERS = {
    "route_identity_cleanup",
    "route_identity_recovery",
    "route_identity_supersede",
    "route_identity_superseded",
}
MF_ROUTE_SOURCE_PASS_STATUSES = {
    *MF_ROUTE_CONTEXT_PASS_STATUSES,
    "complete",
    "completed",
    "succeeded",
    "success",
}


def is_protected_close_evidence(event: dict[str, Any] | None) -> bool:
    """Return true when a timeline append can satisfy MF close evidence."""

    if not isinstance(event, dict):
        return False
    tokens = {
        _text(event.get("event_kind")).lower().replace("-", "_"),
        _text(event.get("phase")).lower().replace("-", "_"),
    }
    event_type = _text(event.get("event_type")).lower().replace("-", "_")
    if event_type:
        tokens.add(event_type)
        tokens.update(part for part in re.split(r"[._:/]+", event_type) if part)
    protected = {item.lower().replace("-", "_") for item in MF_CLOSE_REQUIRED_EVENT_KINDS}
    protected.update(
        {
            "bounded_implementation_worker_dispatch",
            "checkpoint",
            "checkpoint_branch_task",
            "evidence_checkpoint",
            "evidence_export",
            "export",
            "independent_verification",
            "mf_subagent_dispatch",
            "mf_subagent_dispatch_gate",
            "mf_subagent_startup",
            "mf_subagent_startup_adoption",
            "mf_subagent_startup_adoption_gate",
            "mf_subagent_startup_gate",
            "qa_verification",
        }
    )
    return bool(tokens & protected)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    existing = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(task_timeline_events)").fetchall()
    }
    for column, ddl in _V2_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE task_timeline_events ADD COLUMN {column} {ddl}")
    conn.executescript(INDEX_SQL)


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json(value: Any, default: Any) -> str:
    if value is None:
        value = default
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return json.dumps({"unserializable": repr(value)}, ensure_ascii=False)


def _text(value: Any) -> str:
    return str(value or "")


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item or "").strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _normalize_token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "accepted",
            "approved",
            "ok",
            "passed",
            "succeeded",
        }
    return bool(value)


def _scenario_spec(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("test_scenario_spec", "test_scenario", "scenario"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _test_scenario_policy(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    raw = payload.get("test_scenario_policy")
    if isinstance(raw, dict):
        policy = str(raw.get("decision") or raw.get("policy") or "").strip()
        return policy, raw
    return str(raw or "").strip(), {}


def _insert_event(conn: sqlite3.Connection, event: dict[str, Any]) -> dict[str, Any]:
    from .db import sqlite_write_lock

    created_at = event.get("created_at") or _utc_iso()
    payload = event.get("payload") or {}
    verification = event.get("verification") or {}
    artifact_refs = event.get("artifact_refs") or {}
    payload = dict(payload) if isinstance(payload, Mapping) else {}
    verification = dict(verification) if isinstance(verification, Mapping) else {}
    artifact_refs = dict(artifact_refs) if isinstance(artifact_refs, Mapping) else {}
    validation_payload = dict(payload)
    validation_payload.pop("meta_contract_gate", None)
    _ensure_cross_ref_bridge_meta_contract_aliases()
    from .mf_subagent_contract import validate_meta_contract_timeline_event

    meta_contract_gate = validate_meta_contract_timeline_event(
        {
            "event_type": event.get("event_type", ""),
            "phase": event.get("phase", ""),
            "event_kind": event.get("event_kind", ""),
            "actor": event.get("actor", ""),
            "status": event.get("status", ""),
            "decision": event.get("decision", ""),
            "payload": validation_payload,
            "verification": verification,
            "artifact_refs": artifact_refs,
            "backlog_id": event.get("backlog_id", ""),
            "task_id": event.get("task_id", ""),
        }
    )
    payload["meta_contract_gate"] = meta_contract_gate
    with sqlite_write_lock():
        cur = conn.execute(
            """INSERT INTO task_timeline_events
               (project_id, backlog_id, mf_id, task_id, attempt_num, event_type,
                phase, event_kind, scenario_id, parent_event_id, correlation_id,
                severity, decision, schema_version, actor, status, payload_json,
                verification_json, artifact_refs_json, trace_id, commit_sha, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _text(event.get("project_id")),
                _text(event.get("backlog_id")),
                _text(event.get("mf_id")),
                _text(event.get("task_id")),
                int(event.get("attempt_num") or 0),
                _text(event.get("event_type")),
                _text(event.get("phase")),
                _text(event.get("event_kind")),
                _text(event.get("scenario_id")),
                int(event.get("parent_event_id") or 0),
                _text(event.get("correlation_id")),
                _text(event.get("severity")),
                _text(event.get("decision")),
                int(event.get("schema_version") or TIMELINE_SCHEMA_VERSION),
                _text(event.get("actor")),
                _text(event.get("status")),
                _json(payload, {}),
                _json(verification, {}),
                _json(artifact_refs, {}),
                _text(event.get("trace_id")),
                _text(event.get("commit_sha")),
                created_at,
            ),
        )
    inserted = {
        "id": cur.lastrowid,
        "project_id": _text(event.get("project_id")),
        "backlog_id": _text(event.get("backlog_id")),
        "mf_id": _text(event.get("mf_id")),
        "task_id": _text(event.get("task_id")),
        "attempt_num": int(event.get("attempt_num") or 0),
        "event_type": _text(event.get("event_type")),
        "phase": _text(event.get("phase")),
        "event_kind": _text(event.get("event_kind")),
        "scenario_id": _text(event.get("scenario_id")),
        "parent_event_id": int(event.get("parent_event_id") or 0),
        "correlation_id": _text(event.get("correlation_id")),
        "severity": _text(event.get("severity")),
        "decision": _text(event.get("decision")),
        "schema_version": int(event.get("schema_version") or TIMELINE_SCHEMA_VERSION),
        "actor": _text(event.get("actor")),
        "status": _text(event.get("status")),
        "payload": payload,
        "verification": verification,
        "artifact_refs": artifact_refs,
        "trace_id": _text(event.get("trace_id")),
        "commit_sha": _text(event.get("commit_sha")),
        "created_at": created_at,
    }
    _run_service_router_hook(conn, inserted)
    return inserted


def _run_service_router_hook(conn: sqlite3.Connection, inserted_event: dict[str, Any]) -> None:
    event_type = _text(inserted_event.get("event_type"))
    payload = _mapping(inserted_event.get("payload"))
    if event_type.startswith("service.route.") or payload.get("service_router_suppress") is True:
        return
    try:
        from agent.governance.service_router import route_timeline_event

        route_timeline_event(conn, inserted_event, record=True)
    except Exception:
        log.debug("service router timeline hook failed", exc_info=True)


def _publish_timeline_event(inserted_event: dict[str, Any]) -> None:
    try:
        from agent.governance import event_bus

        payload = {
            "project_id": _text(inserted_event.get("project_id")),
            "backlog_id": _text(inserted_event.get("backlog_id")),
            "task_id": _text(inserted_event.get("task_id")),
            "event_id": inserted_event.get("id", ""),
            "event_type": _text(inserted_event.get("event_type")),
            "event_kind": _text(inserted_event.get("event_kind")),
            "phase": _text(inserted_event.get("phase")),
            "status": _text(inserted_event.get("status")),
        }
        event_bus._bus.publish("task_timeline.appended", payload)
        event_bus._bus.publish("current_task.changed", {
            **payload,
            "source": "task_timeline.record_event",
            "runtime_state": payload["status"],
        })
    except Exception:
        log.debug("task timeline event publish failed", exc_info=True)


def record_event(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    event_type: str,
    task_id: str = "",
    backlog_id: str = "",
    mf_id: str = "",
    attempt_num: int = 0,
    phase: str = "",
    event_kind: str = "",
    scenario_id: str = "",
    parent_event_id: int = 0,
    correlation_id: str = "",
    severity: str = "",
    decision: str = "",
    schema_version: int = TIMELINE_SCHEMA_VERSION,
    actor: str = "",
    status: str = "",
    payload: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    artifact_refs: dict[str, Any] | None = None,
    trace_id: str = "",
    commit_sha: str = "",
) -> dict[str, Any]:
    """Append a timeline event using the caller's transaction."""

    if not project_id or not event_type:
        raise ValueError("project_id and event_type are required")
    inserted = _insert_event(
        conn,
        {
            "project_id": project_id,
            "backlog_id": backlog_id,
            "mf_id": mf_id,
            "task_id": task_id,
            "attempt_num": attempt_num,
            "event_type": event_type,
            "phase": phase,
            "event_kind": event_kind,
            "scenario_id": scenario_id,
            "parent_event_id": parent_event_id,
            "correlation_id": correlation_id,
            "severity": severity,
            "decision": decision,
            "schema_version": schema_version,
            "actor": actor,
            "status": status,
            "payload": payload or {},
            "verification": verification or {},
            "artifact_refs": artifact_refs or {},
            "trace_id": trace_id,
            "commit_sha": commit_sha,
        },
    )
    _publish_timeline_event(inserted)
    return inserted


class _TimelineWriteQueue:
    """Small process-local serialized writer for executor-side evidence."""

    def __init__(self) -> None:
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure_started(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run,
                name="task-timeline-writer",
                daemon=True,
            )
            self._thread.start()

    def enqueue(self, event: dict[str, Any], *, wait: bool = True, timeout: float = 10.0) -> dict[str, Any]:
        if not event.get("project_id") or not event.get("event_type"):
            raise ValueError("project_id and event_type are required")
        self._ensure_started()
        done = threading.Event()
        item = {"event": event, "done": done, "result": None, "error": None}
        self._queue.put(item)
        if not wait:
            return {"queued": True}
        if not done.wait(timeout):
            raise TimeoutError("task timeline write queue timed out")
        if item["error"] is not None:
            raise item["error"]
        return item["result"] or {"queued": True}

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            event = item["event"]
            try:
                from .db import get_connection

                conn = get_connection(event["project_id"])
                try:
                    inserted = _insert_event(conn, event)
                    conn.commit()
                    _publish_timeline_event(inserted)
                    item["result"] = inserted
                finally:
                    conn.close()
            except Exception as exc:
                log.debug("task timeline write failed", exc_info=True)
                item["error"] = exc
            finally:
                item["done"].set()
                self._queue.task_done()


_WRITE_QUEUE = _TimelineWriteQueue()


def enqueue_event(
    project_id: str,
    *,
    event_type: str,
    task_id: str = "",
    backlog_id: str = "",
    mf_id: str = "",
    attempt_num: int = 0,
    phase: str = "",
    event_kind: str = "",
    scenario_id: str = "",
    parent_event_id: int = 0,
    correlation_id: str = "",
    severity: str = "",
    decision: str = "",
    schema_version: int = TIMELINE_SCHEMA_VERSION,
    actor: str = "",
    status: str = "",
    payload: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    artifact_refs: dict[str, Any] | None = None,
    trace_id: str = "",
    commit_sha: str = "",
    wait: bool = True,
) -> dict[str, Any]:
    """Queue a timeline write from executor/worker code.

    The default waits until the event is durable. Callers that cannot block may
    set wait=False and accept best-effort delivery.
    """

    return _WRITE_QUEUE.enqueue(
        {
            "project_id": project_id,
            "backlog_id": backlog_id,
            "mf_id": mf_id,
            "task_id": task_id,
            "attempt_num": attempt_num,
            "event_type": event_type,
            "phase": phase,
            "event_kind": event_kind,
            "scenario_id": scenario_id,
            "parent_event_id": parent_event_id,
            "correlation_id": correlation_id,
            "severity": severity,
            "decision": decision,
            "schema_version": schema_version,
            "actor": actor,
            "status": status,
            "payload": payload or {},
            "verification": verification or {},
            "artifact_refs": artifact_refs or {},
            "trace_id": trace_id,
            "commit_sha": commit_sha,
        },
        wait=wait,
    )


def completion_verification(status: str, result: dict[str, Any] | None) -> dict[str, Any]:
    """Gate-style checks for task completion evidence.

    These checks do not prove correctness. They make implementation evidence
    explicit and machine-visible before later merge/review gates consume it.
    """

    result = result if isinstance(result, dict) else {}
    warnings: list[str] = []
    errors: list[str] = []

    changed_files = result.get("changed_files", [])
    if "changed_files" in result and not isinstance(changed_files, list):
        errors.append("changed_files must be a list when present")
    if status == "succeeded" and "changed_files" not in result:
        warnings.append("changed_files missing")

    evidence = result.get("implementation_evidence", [])
    if evidence and not isinstance(evidence, list):
        errors.append("implementation_evidence must be a list when present")
    elif status == "succeeded" and not evidence:
        warnings.append("implementation_evidence missing")

    self_check = result.get("self_check", {})
    if self_check and not isinstance(self_check, dict):
        errors.append("self_check must be an object when present")
    elif status == "succeeded" and not self_check:
        warnings.append("self_check missing")

    artifacts = result.get("_artifacts", {})
    if artifacts and not isinstance(artifacts, dict):
        errors.append("_artifacts must be an object when present")

    failure = result.get("failure") or {}
    if status in {"failed", "timed_out"} and not failure:
        warnings.append("synthetic failure envelope missing")

    return {
        "passed": not errors,
        "status": "passed" if not errors else "failed",
        "warnings": warnings,
        "errors": errors,
        "checks": {
            "has_structured_result": isinstance(result, dict),
            "has_changed_files": isinstance(changed_files, list),
            "has_implementation_evidence": isinstance(evidence, list) and bool(evidence),
            "has_self_check": isinstance(self_check, dict) and bool(self_check),
            "has_artifact_refs": isinstance(artifacts, dict) and bool(artifacts),
            "has_failure_envelope": bool(failure),
        },
    }


def mf_test_scenario_verification(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Validate the MF test-scenario decision shape.

    MF work can choose no new test, reuse an existing scenario, or require a new
    scenario. The helper does not judge coverage quality; it makes the decision
    explicit enough for later gates and observers to inspect.
    """

    payload = payload if isinstance(payload, dict) else {}
    policy, policy_object = _test_scenario_policy(payload)
    verification_notes = _string_list(payload.get("verification_notes"))
    tests_run = _string_list(payload.get("tests_run"))
    scenario_id = str(payload.get("scenario_id") or "").strip()
    scenario = _scenario_spec(payload)
    if not scenario_id and scenario:
        scenario_id = str(scenario.get("id") or "").strip()
    policy_mode = str(policy_object.get("mode") or "").strip()
    reason = str(policy_object.get("reason") or "").strip()
    allowed_decisions = _string_list(policy_object.get("allowed_decisions"))
    required_evidence_ids = _string_list(policy_object.get("required_evidence_ids"))
    e2e_decision = str(policy_object.get("e2e_decision") or "").strip()
    followup_backlog_id = str(policy_object.get("followup_backlog_id") or "").strip()

    errors: list[str] = []
    warnings: list[str] = []

    if policy_object:
        if policy_mode != MF_TEST_SCENARIO_POLICY_MODE:
            errors.append(
                f"test_scenario_policy.mode must be {MF_TEST_SCENARIO_POLICY_MODE}"
            )
        if not allowed_decisions:
            errors.append("test_scenario_policy.allowed_decisions must be non-empty")
        else:
            unsupported = sorted(set(allowed_decisions) - MF_TEST_SCENARIO_POLICIES)
            if unsupported:
                errors.append(
                    "test_scenario_policy.allowed_decisions contains unsupported "
                    "decision(s): " + ", ".join(unsupported)
                )
            if policy and policy not in allowed_decisions:
                errors.append("test_scenario_policy.decision must be allowed")
        if not reason:
            errors.append("test_scenario_policy.reason is required")
        if not required_evidence_ids:
            errors.append("test_scenario_policy.required_evidence_ids must be non-empty")
        if e2e_decision not in MF_TEST_SCENARIO_E2E_DECISIONS:
            errors.append(
                "test_scenario_policy.e2e_decision must be one of: "
                + ", ".join(sorted(MF_TEST_SCENARIO_E2E_DECISIONS))
            )
        elif e2e_decision == "e2e_deferred" and not followup_backlog_id:
            errors.append(
                "test_scenario_policy.followup_backlog_id is required when "
                "e2e_decision=e2e_deferred"
            )

    if policy not in MF_TEST_SCENARIO_POLICIES:
        errors.append(
            "test_scenario_policy must be one of: "
            + ", ".join(sorted(MF_TEST_SCENARIO_POLICIES))
        )
    elif policy == "none":
        if not verification_notes and not tests_run and not reason:
            errors.append("policy=none requires verification_notes or tests_run explaining why no scenario is needed")
    elif policy == "reuse_existing":
        if not scenario_id and not tests_run and not verification_notes:
            errors.append("policy=reuse_existing requires scenario_id, tests_run, or verification_notes")
    elif policy == "new_scenario_required":
        steps = _string_list(scenario.get("steps")) if scenario else []
        expected = _string_list(scenario.get("expected")) if scenario else []
        if not scenario:
            errors.append("policy=new_scenario_required requires test_scenario_spec")
        else:
            if not steps:
                errors.append("test_scenario_spec.steps must be non-empty")
            if not expected:
                errors.append("test_scenario_spec.expected must be non-empty")
        if not scenario_id:
            warnings.append("test_scenario_spec.id missing")

    has_new_scenario_spec = bool(
        scenario
        and _string_list(scenario.get("steps"))
        and _string_list(scenario.get("expected"))
    )
    return {
        "passed": not errors,
        "status": "passed" if not errors else "failed",
        "policy": policy,
        "effective_decision": policy,
        "policy_mode": policy_mode,
        "reason": reason,
        "allowed_decisions": allowed_decisions,
        "required_evidence_ids": required_evidence_ids,
        "e2e_decision": e2e_decision,
        "followup_backlog_id": followup_backlog_id,
        "scenario_id": scenario_id,
        "warnings": warnings,
        "errors": errors,
        "checks": {
            "has_explicit_policy": policy in MF_TEST_SCENARIO_POLICIES,
            "has_observer_configured_policy": policy_mode == MF_TEST_SCENARIO_POLICY_MODE,
            "has_decision_reason": bool(reason),
            "has_required_evidence_ids": bool(required_evidence_ids),
            "has_e2e_decision": e2e_decision in MF_TEST_SCENARIO_E2E_DECISIONS,
            "has_verification_notes": bool(verification_notes),
            "has_tests_run": bool(tests_run),
            "has_scenario_id": bool(scenario_id),
            "has_new_scenario_spec": has_new_scenario_spec,
        },
    }


def _contract_root(contract: dict[str, Any] | None) -> dict[str, Any]:
    data = _mapping(contract)
    for key in ("parallel_contract", "mf_contract", "contract_instance", "contract"):
        nested = data.get(key)
        if isinstance(nested, dict):
            return nested
    return data


def _policy_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "required", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "optional", "disabled"}:
            return False
    return default


def _governance_policy(contract: dict[str, Any] | None = None) -> dict[str, Any]:
    root = _contract_root(contract)
    policy = _mapping(root.get("governance_policy"))
    profile = _text(policy.get("profile")) or _text(root.get("governance_policy_profile"))
    if not profile:
        project_id = _text(root.get("project_id") or root.get("target_project_id"))
        profile = "aming-claw" if project_id == "aming-claw" else "third-party-public"
    requirements = _mapping(policy.get("requirements"))
    strict = profile == "aming-claw"
    return {
        "schema_version": "governance_policy.v1",
        "profile": profile,
        "source": _text(policy.get("source")) or "project_default",
        "public_safe": bool(policy.get("public_safe", True)) if policy else True,
        "requirements": {
            "graph_first_evidence": _policy_bool(
                requirements.get("graph_first_evidence"),
                True,
            ),
            "worker_graph_trace": _policy_bool(
                requirements.get("worker_graph_trace"),
                strict,
            ),
            "independent_qa": _policy_bool(
                requirements.get("independent_qa"),
                strict,
            ),
            "single_active_task": _policy_bool(
                requirements.get("single_active_task"),
                strict,
            ),
            "close_timeline": _policy_bool(
                requirements.get("close_timeline"),
                True,
            ),
        },
    }


def _policy_requires(policy: Mapping[str, Any], key: str) -> bool:
    return bool(_mapping(policy.get("requirements")).get(key))


def _event_graph_trace_ids(event: Mapping[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in ("trace_id", "graph_trace_id"):
        text = _text(event.get(key)).strip()
        if text:
            ids.add(text)
    for key in ("graph_trace_ids", "graph_query_trace_ids"):
        ids.update(_string_list(event.get(key)))
    for key in ("payload", "verification", "artifact_refs"):
        nested = _mapping(event.get(key))
        for trace_key in ("trace_id", "graph_trace_id"):
            text = _text(nested.get(trace_key)).strip()
            if text:
                ids.add(text)
        for trace_key in ("graph_trace_ids", "graph_query_trace_ids"):
            ids.update(_string_list(nested.get(trace_key)))
    return ids


def _worker_graph_trace_event_gate(event: Mapping[str, Any]) -> tuple[bool, str]:
    status = _text(event.get("status") or event.get("decision")).strip().lower()
    if status and status not in MF_CLOSE_PASS_STATUSES:
        return False, "non_passing_status"
    kind = _route_marker(
        event.get("event_kind") or event.get("event_type") or event.get("phase")
    )
    if kind not in {
        "implementation",
        "merge",
        "merge_evidence",
        "mf_subagent_read_receipt",
        "runtime_context_read_receipt",
        "worker_read_receipt",
        "read_receipt",
        "graph_query_trace",
        "mf_subagent_graph_query_trace",
    }:
        return False, "unsupported_worker_graph_trace_event_kind"
    actor = _text(event.get("actor")).strip().lower()
    query_source = _first_deep_text(event, "query_source").lower()
    worker_role = (
        _first_deep_text(event, "worker_role")
        or _first_deep_text(event, "role")
    ).lower()
    if actor in {"observer", "mf_observer", "route_observer", "observer_runtime_text"}:
        return False, "observer_substituted_worker_graph_trace"
    if actor in {
        "qa",
        "independent_qa",
        "qa_verifier",
        "qa_reviewer",
        "independent_verifier",
    }:
        return False, "qa_substituted_worker_graph_trace"
    if query_source and query_source != "mf_subagent":
        return False, "query_source_not_mf_subagent"
    if worker_role and worker_role != "mf_sub":
        return False, "worker_role_not_mf_sub"
    if query_source == "mf_subagent" or worker_role == "mf_sub":
        return True, ""
    return False, "missing_mf_subagent_identity"


def _worker_graph_trace_gate(
    rows: list[dict[str, Any]],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    required = _policy_requires(policy, "worker_graph_trace")
    evidence_events: list[dict[str, Any]] = []
    rejected_events: list[dict[str, Any]] = []
    trace_ids: set[str] = set()
    for event in rows:
        ids = _event_graph_trace_ids(_mapping(event))
        if not ids:
            continue
        ok, reason = _worker_graph_trace_event_gate(_mapping(event))
        if not ok:
            rejected_events.append({
                "id": event.get("id"),
                "event_kind": event.get("event_kind"),
                "phase": event.get("phase"),
                "status": event.get("status"),
                "actor": event.get("actor"),
                "reason": reason,
                "graph_trace_ids": sorted(ids),
            })
            continue
        trace_ids.update(ids)
        evidence_events.append({
            "id": event.get("id"),
            "event_kind": event.get("event_kind"),
            "phase": event.get("phase"),
            "status": event.get("status"),
            "graph_trace_ids": sorted(ids),
        })
    passed = bool(trace_ids) or not required
    process_gap = bool(required and not trace_ids and rejected_events)
    return {
        "schema_version": "worker_graph_trace_gate.v1",
        "required": required,
        "passed": passed,
        "status": "passed" if passed else ("process_gap" if process_gap else "failed"),
        "trace_ids": sorted(trace_ids),
        "missing_requirement_ids": [] if passed else ["worker_graph_trace"],
        "evidence_events": evidence_events,
        "rejected_evidence_events": rejected_events,
        "failure_reason": (
            "observer_or_unsupported_graph_trace_cannot_satisfy_worker_evidence"
            if process_gap
            else ("" if passed else "missing_mf_subagent_graph_trace")
        ),
    }


def _independent_qa_reviewer_identity(event: dict[str, Any]) -> str:
    """Derive the effective reviewer identity for independence checking.

    For direct evidence (qa_reviewer, qa_verification, independent_verification),
    the reviewer is the actor.  For observer-on-behalf transport events the
    transport actor is "observer-on-behalf-of:<reviewer>" or the payload carries
    a "reviewer" field; in both cases the *reviewer* identity is what matters for
    the independence test, not the transport actor.

    Returns the reviewer identity string (non-empty) or "" if none can be derived.
    """
    actor = _text(event.get("actor")).strip()
    # Observer-on-behalf transport: "observer-on-behalf-of:<reviewer-id>"
    on_behalf_prefix = "observer-on-behalf-of:"
    if actor.lower().startswith(on_behalf_prefix):
        reviewer = actor[len(on_behalf_prefix):].strip()
        if reviewer:
            return reviewer
    # payload.reviewer or verification.reviewer set explicitly
    for container_key in ("payload", "verification", "artifact_refs"):
        container = _mapping(event.get(container_key))
        reviewer = _text(container.get("reviewer")).strip()
        if reviewer:
            return reviewer
    # Direct case: the actor IS the reviewer (non-observer, non-worker transport)
    return actor


_INDEPENDENT_QA_PLAIN_OBSERVER_TOKENS = {"observer", "mf_observer", "route_observer"}


def _independent_qa_ref_aliases(value: Any) -> set[str]:
    refs: set[str] = set()
    raw = _text(value).strip()
    if not raw:
        return refs
    refs.add(raw)
    bare = raw
    for prefix in ("timeline:", "event:"):
        if bare.lower().startswith(prefix):
            bare = bare[len(prefix):].strip()
            break
    if bare.startswith("#"):
        bare = bare[1:].strip()
    if bare:
        refs.add(bare)
        refs.add(f"timeline:{bare}")
        refs.add(f"event:{bare}")
        refs.add(f"#{bare}")
    return refs


def _independent_qa_event_ref_tokens(event: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for key in ("id", "event_id"):
        refs.update(_independent_qa_ref_aliases(event.get(key)))
    return refs


def _independent_qa_verdict_refs(event: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for container_key in ("payload", "verification", "artifact_refs"):
        container = _mapping(event.get(container_key))
        for key in ("qa_verdict_refs", "verdict_refs"):
            refs.extend(_string_list(container.get(key)))
    return list(dict.fromkeys(refs))


def _independent_qa_event_kind_matches(event: dict[str, Any]) -> bool:
    for key in ("event_kind", "event_type", "phase"):
        token = _normalize_token(event.get(key))
        if any(
            marker in token
            for marker in ("qa_review", "qa_verification", "independent_verification")
        ):
            return True
    return False


def _independent_qa_same_timeline_scope(
    source: dict[str, Any],
    target: dict[str, Any],
) -> bool:
    for field in ("project_id", "backlog_id"):
        source_value = _text(source.get(field)).strip()
        target_value = _text(target.get(field)).strip()
        if source_value and target_value and source_value != target_value:
            return False
    return True


def _independent_qa_resolved_verdict_refs(
    event: dict[str, Any],
    events_by_ref: dict[str, dict[str, Any]],
    worker_slot_ids: set[str],
) -> list[str]:
    resolved: list[str] = []
    for ref in _independent_qa_verdict_refs(event):
        target = events_by_ref.get(ref)
        if not target:
            continue
        status = _text(target.get("status") or target.get("decision")).lower()
        if status not in MF_CLOSE_PASS_STATUSES:
            continue
        if not _independent_qa_event_kind_matches(target):
            continue
        if not _independent_qa_same_timeline_scope(event, target):
            continue
        target_reviewer_identity = _independent_qa_reviewer_identity(target)
        target_reviewer = target_reviewer_identity.lower()
        if (
            not target_reviewer_identity
            or target_reviewer in _INDEPENDENT_QA_PLAIN_OBSERVER_TOKENS
            or target_reviewer_identity in worker_slot_ids
        ):
            continue
        resolved.append(ref)
    return resolved


def _independent_qa_gate(
    rows: list[dict[str, Any]],
    policy: Mapping[str, Any],
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Gate that requires passing independent QA verification evidence.

    Required when:
    - The governance policy profile flags independent_qa=true (e.g. aming-claw
      profile), OR
    - The row's topology policy declares independent_verification_required=true
      or includes an independent_verification/qa lane in required_lanes.

    Independence-by-identity rule (AC BUG-ROUTE-CONTEXT-CLOSE-GATE-QA-20260531):
    - Evidence whose *reviewer identity* is the same as any known worker_slot_id
      in the timeline does NOT count (live incidents #3750/#3811: workers
      self-appended IV lane events).
    - Plain observer verification events (actor="observer" or similar) without an
      independent reviewer identity (no on-behalf suffix, no payload.reviewer, no
      qa_verdict_refs pointing at real qa_review events) do NOT count.
    - Observer-on-behalf transport DOES count when the reviewer identity
      (derived from the actor suffix or payload.reviewer) is independent of the
      known workers.  This is the established daily pattern for observer-recorded
      verdicts and must not be broken.
    - Direct qa_review / qa_verification / independent_verification events with a
      non-worker, non-plain-observer actor count normally.
    """
    # Determine required from both governance policy and topology policy.
    policy_required = _policy_requires(policy, "independent_qa")
    topology_required = False
    if contract is not None:
        topology_policy = _route_topology_policy(contract)
        topology_required = _route_independent_verification_required(topology_policy)
    required = policy_required or topology_required

    # Collect worker identities from startup events in this timeline so we can
    # apply the independence-by-identity test.
    worker_slot_ids: set[str] = set()
    for raw in rows:
        ev = _mapping(raw)
        startup_marker = _route_marker(ev.get("event_kind") or ev.get("event_type") or "")
        if startup_marker in {
            "mf_subagent_startup",
            "mf_subagent_startup_gate",
            "mf_subagent_startup_adoption",
            "mf_subagent_startup_adoption_gate",
        }:
            for key in ("actor", "worker_slot_id", "worker_id", "agent_id", "allocation_owner"):
                wid = _text(ev.get(key)).strip()
                if wid:
                    worker_slot_ids.add(wid)
            # Also look inside the payload for startup gate fields.
            for container_key in ("payload", "verification", "artifact_refs"):
                container = _mapping(ev.get(container_key))
                for key in ("worker_slot_id", "worker_id", "agent_id", "allocation_owner"):
                    wid = _text(container.get(key)).strip()
                    if wid:
                        worker_slot_ids.add(wid)
                startup_gate = _mapping(container.get("mf_subagent_startup_gate"))
                for key in ("worker_slot_id", "worker_id", "agent_id", "allocation_owner"):
                    wid = _text(startup_gate.get(key)).strip()
                    if wid:
                        worker_slot_ids.add(wid)

    # Plain-observer identity tokens that, without an independent reviewer, do
    # not constitute independent verification.
    evidence_events: list[dict[str, Any]] = []
    rejected_events: list[dict[str, Any]] = []
    events_by_ref: dict[str, dict[str, Any]] = {}
    for raw in rows:
        event = _mapping(raw)
        for ref in _independent_qa_event_ref_tokens(event):
            events_by_ref.setdefault(ref, event)

    for raw_event in rows:
        event = _mapping(raw_event)
        status = _text(event.get("status") or event.get("decision")).lower()
        kind_lower = _text(event.get("event_kind")).lower()
        type_lower = _text(event.get("event_type")).lower()
        phase_lower = _text(event.get("phase")).lower()
        actor_lower = _text(event.get("actor")).lower()

        if status not in MF_CLOSE_PASS_STATUSES:
            continue

        # Only consider QA/independent-verification event kinds.
        marker_tokens = {kind_lower, type_lower, phase_lower, actor_lower}
        if not any(
            tok
            for tok in marker_tokens
            if "qa" in tok or "independent_verification" in tok
        ):
            continue

        # Derive the effective reviewer identity.
        reviewer_identity = _independent_qa_reviewer_identity(event)
        reviewer_lower = reviewer_identity.lower()

        # REJECT: reviewer is a known worker (self-appended IV — incidents #3750/#3811).
        if reviewer_identity and reviewer_identity in worker_slot_ids:
            rejected_events.append({
                "id": event.get("id"),
                "event_kind": event.get("event_kind"),
                "actor": event.get("actor"),
                "status": event.get("status"),
                "reason": "reviewer_is_known_worker",
                "reviewer_identity": reviewer_identity,
            })
            continue

        # REJECT: plain observer token without an independent reviewer identity.
        # This catches raw "observer" verification events that are NOT on-behalf
        # transports (no on-behalf suffix, no payload.reviewer, no verdict refs).
        resolved_verdict_refs: list[str] = []
        if reviewer_lower in _INDEPENDENT_QA_PLAIN_OBSERVER_TOKENS:
            # Only same-timeline refs to real passing QA/IV events elevate a
            # plain-observer transport to a legitimate verdict relay.
            unresolved_verdict_refs = _independent_qa_verdict_refs(event)
            resolved_verdict_refs = _independent_qa_resolved_verdict_refs(
                event,
                events_by_ref,
                worker_slot_ids,
            )
            if not resolved_verdict_refs:
                rejected_events.append({
                    "id": event.get("id"),
                    "event_kind": event.get("event_kind"),
                    "actor": event.get("actor"),
                    "status": event.get("status"),
                    "reason": (
                        "plain_observer_no_resolving_qa_verdict_ref"
                        if unresolved_verdict_refs
                        else "plain_observer_no_independent_reviewer"
                    ),
                    "verdict_refs": unresolved_verdict_refs,
                })
                continue

        evidence = {
            "id": event.get("id"),
            "event_kind": event.get("event_kind"),
            "phase": event.get("phase"),
            "actor": event.get("actor"),
            "reviewer_identity": reviewer_identity,
            "status": event.get("status"),
        }
        if resolved_verdict_refs:
            evidence["resolved_qa_verdict_refs"] = resolved_verdict_refs
        evidence_events.append(evidence)

    passed = bool(evidence_events) or not required
    missing_ids = [] if passed else ["independent_qa"]
    reason = (
        ""
        if passed
        else (
            "independent_qa_required_but_all_evidence_rejected_or_missing"
            if rejected_events
            else "independent_qa_required_but_no_evidence_found"
        )
    )
    return {
        "schema_version": "independent_qa_gate.v1",
        "required": required,
        "topology_required": topology_required,
        "policy_required": policy_required,
        "passed": passed,
        "status": "passed" if passed else "failed",
        "missing_requirement_ids": missing_ids,
        "reason": reason,
        "next_action": (
            ""
            if passed
            else (
                "Record a passing qa_review/qa_verification/independent_verification event "
                "from an independent reviewer (not the implementation worker). "
                "Observer-on-behalf transport is accepted when actor contains "
                "'observer-on-behalf-of:<reviewer-id>' or payload.reviewer is set "
                "to an independent reviewer identity."
            )
        ),
        "evidence_events": evidence_events,
        "rejected_evidence_events": rejected_events,
        "known_worker_slot_ids": sorted(worker_slot_ids),
    }


def _canonical_contract_hash(value: Any) -> str:
    try:
        body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except TypeError:
        body = json.dumps(repr(value), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _event_numeric_id(event: dict[str, Any]) -> int:
    value = event.get("id", event.get("event_id"))
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _event_marker(event: dict[str, Any]) -> str:
    return _normalize_token(
        " ".join(
            str(event.get(key) or "")
            for key in ("event_type", "event_kind", "phase", "actor", "status")
        )
    )


def _read_receipt_hash_from_container(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("read_receipt_hash", "worker_read_receipt_hash"):
        found = value.get(key)
        if str(found or "").strip():
            return str(found).strip()
    for key in ("read_receipt", "worker_contract", "evidence", "payload"):
        nested = value.get(key)
        if isinstance(nested, dict):
            found = _read_receipt_hash_from_container(nested)
            if found:
                return found
    return ""


def _is_mf_subagent_read_receipt_event(event: dict[str, Any]) -> bool:
    marker = _event_marker(event)
    return "read_receipt" in marker and (
        "mf_sub" in marker
        or "subagent" in marker
        or bool(_read_receipt_hash_from_container(_mapping(event.get("payload"))))
    )


# Projection schema name used in actionable rejection errors so callers can
# look up what the close gate expects.
_RECEIPT_PROJECTION_SCHEMA = "runtime_context.timeline_evidence_fields.v1"
# Fields the close gate requires on a read-receipt event.
_RECEIPT_LINEAGE_REQUIRED = ("runtime_context_id", "task_id", "parent_task_id", "fence_token")
# At least one of these hash fields must be present.
_RECEIPT_HASH_FIELDS = ("read_receipt_hash", "launch_text_hash")


def validate_and_normalize_mf_read_receipt_append(
    event_type: str,
    event_kind: str,
    actor: str,
    status: str,
    payload: dict[str, Any] | None,
) -> tuple[str, str, dict[str, Any]]:
    """Validate and normalize an mf_subagent read-receipt append at write time.

    Called in the server handler before record_event so that malformed receipts
    are rejected immediately with an actionable error rather than silently
    accepted and discovered unusable at close.

    Returns (normalized_event_kind, normalized_status, normalized_payload) when
    the event is not a read-receipt (passthrough) or when the event is a valid
    read-receipt after normalization.

    Raises ValueError with an actionable message naming the missing projection
    fields when the event is a read-receipt but is invalid.

    Normalization rules (only applied to detected receipts):
    - event_kind is set to "mf_subagent_read_receipt" when the event_type marks
      a receipt but event_kind is absent.
    - worker_slot_id is set from worker_id in the payload when worker_slot_id is
      absent and worker_id is present.

    Rejection rules (only applied to detected receipts):
    - status must be non-empty and a passing status (see MF_CLOSE_PASS_STATUSES).
    - at least one of read_receipt_hash or launch_text_hash must be present in
      the payload.
    - runtime_context_id, task_id, parent_task_id, and fence_token must all be
      present in the payload (lineage required by the close-gate projection).
    """
    p = dict(payload or {})
    _ensure_cross_ref_bridge_meta_contract_aliases()
    # Build a synthetic event dict so we can reuse _is_mf_subagent_read_receipt_event.
    probe = {
        "event_type": event_type,
        "event_kind": event_kind,
        "actor": actor,
        "status": status,
        "payload": p,
    }
    if not _is_mf_subagent_read_receipt_event(probe):
        # Not a read-receipt — pass through unchanged.
        return event_kind, status, p

    # --- Normalization ---
    normalized_kind = event_kind
    if not str(normalized_kind or "").strip():
        normalized_kind = "mf_subagent_read_receipt"

    # Normalize worker_slot_id from worker_id when absent.
    if not str(p.get("worker_slot_id") or "").strip():
        worker_id = str(p.get("worker_id") or "").strip()
        if worker_id:
            p = dict(p)
            p["worker_slot_id"] = worker_id

    # --- Validation ---
    missing: list[str] = []

    # status must be present and passing.
    normalized_status_val = str(status or "").strip()
    if not normalized_status_val or normalized_status_val not in MF_CLOSE_PASS_STATUSES:
        missing.append("status (must be one of: " + ", ".join(sorted(MF_CLOSE_PASS_STATUSES)) + ")")

    # At least one of read_receipt_hash or launch_text_hash must be present.
    has_hash = any(str(p.get(f) or "").strip() for f in _RECEIPT_HASH_FIELDS)
    if not has_hash:
        missing.append("read_receipt_hash or launch_text_hash")

    # Lineage fields required for close-gate projection.
    for field in _RECEIPT_LINEAGE_REQUIRED:
        if not str(p.get(field) or "").strip():
            missing.append(field)

    # worker_slot_id must now be present (after normalization attempt).
    if not str(p.get("worker_slot_id") or "").strip():
        missing.append("worker_slot_id (or worker_id for normalization)")

    if missing:
        raise ValueError(
            "mf_subagent read-receipt append rejected: missing required projection fields "
            f"for {_RECEIPT_PROJECTION_SCHEMA}: {', '.join(missing)}. "
            "Required fields: status (passing), "
            + ", ".join(_RECEIPT_LINEAGE_REQUIRED)
            + ", worker_slot_id, and at least one of "
            + "/".join(_RECEIPT_HASH_FIELDS)
            + "."
        )

    return normalized_kind, status, p


def _container_has_marker_key(value: Any, marker_keys: set[str], *, depth: int = 0) -> bool:
    if depth > 5:
        return False
    if isinstance(value, dict):
        for key, child in value.items():
            if _normalize_token(key) in marker_keys:
                return True
            if _container_has_marker_key(child, marker_keys, depth=depth + 1):
                return True
    elif isinstance(value, list):
        return any(
            _container_has_marker_key(child, marker_keys, depth=depth + 1)
            for child in value
        )
    return False


def _is_observer_planning_or_dispatch_event(event: dict[str, Any]) -> bool:
    marker = _event_marker(event)
    marker_fragments = (
        "route_context",
        "route_action",
        "route_token_gate",
        "route_gate",
        "pre_mutation",
        "mf_subagent_dispatch",
        "bounded_implementation_worker_dispatch",
        "dispatch_gate",
        "startup_intent",
        "observer_runtime_text",
    )
    if any(fragment in marker for fragment in marker_fragments):
        return True
    planning_keys = {
        "route_context",
        "route_prompt_bundle",
        "prompt_alert_bundle",
        "visible_injection_manifest",
        "route_action_gate",
        "route_action_precheck",
        "route_token_gate",
        "mf_subagent_dispatch_gate",
        "bounded_implementation_worker_dispatch",
        "dispatch_evidence",
        "mf_subagent_startup_intent",
        "startup_intent",
    }
    return any(
        _container_has_marker_key(_mapping(event.get(key)), planning_keys)
        for key in ("payload", "verification", "artifact_refs")
    )


def _is_counted_mf_subagent_evidence_event(event: dict[str, Any]) -> bool:
    if _is_mf_subagent_read_receipt_event(event):
        return False
    marker = _event_marker(event)
    if "progress" in marker:
        return False
    if _is_observer_planning_or_dispatch_event(event):
        return False
    evidence_markers = (
        "startup",
        "graph",
        "query",
        "implementation",
        "verification",
        "close_ready",
        "finish",
        "checkpoint",
        "handoff",
        "review_ready",
        "write",
        "test",
    )
    if any(item in marker for item in evidence_markers):
        return True
    payload = _mapping(event.get("payload"))
    return bool(payload.get("graph_trace_ids") or payload.get("changed_files"))


def _read_receipt_gate_event_ref(
    event: dict[str, Any],
    *,
    reason: str = "",
) -> dict[str, Any]:
    ref = {
        "id": event.get("id") or event.get("event_id"),
        "event_kind": event.get("event_kind"),
        "event_type": event.get("event_type"),
        "phase": event.get("phase"),
        "status": event.get("status") or event.get("decision"),
    }
    if reason:
        ref["reason"] = reason
    return {key: value for key, value in ref.items() if value not in (None, "")}


def _read_receipt_gate_route_identity(event: dict[str, Any]) -> dict[str, str]:
    identity = _route_identity(event)
    if identity and "mf_subagent_startup" in _route_event_categories(event):
        identity, _lineage = _route_parent_child_startup_identity(event, identity)
    return identity


def _read_receipt_gate_attempt_lineage(event: dict[str, Any]) -> dict[str, str]:
    return _route_attempt_lineage(event)


def _read_receipt_route_identities_compatible(
    first: dict[str, str],
    second: dict[str, str],
) -> bool:
    if not first or not second:
        return False
    if _route_identity_key(first) != _route_identity_key(second):
        return False
    first_prompt_hash = first.get("prompt_contract_hash", "")
    second_prompt_hash = second.get("prompt_contract_hash", "")
    return not (
        first_prompt_hash
        and second_prompt_hash
        and first_prompt_hash != second_prompt_hash
    )


def _read_receipt_startup_order_is_harmless(
    startup_event: dict[str, Any],
    read_receipt_event: dict[str, Any],
    *,
    identity_filter: dict[str, str],
    attempt_lineage_filter: dict[str, str],
) -> bool:
    if "mf_subagent_startup" not in _route_event_categories(startup_event):
        return False
    if not _route_actual_startup_identity_present(startup_event):
        return False
    if not identity_filter and not attempt_lineage_filter:
        return False

    startup_identity = _read_receipt_gate_route_identity(startup_event)
    read_identity = _read_receipt_gate_route_identity(read_receipt_event)
    if identity_filter:
        if startup_identity and not _route_identity_matches_filter(
            startup_identity,
            identity_filter,
        ):
            return False
        if read_identity and not _route_identity_matches_filter(
            read_identity,
            identity_filter,
        ):
            return False
    elif not _read_receipt_route_identities_compatible(
        startup_identity,
        read_identity,
    ):
        return False

    startup_lineage = _read_receipt_gate_attempt_lineage(startup_event)
    read_lineage = _read_receipt_gate_attempt_lineage(read_receipt_event)
    if attempt_lineage_filter:
        if startup_lineage and not _attempt_lineage_matches_filter(
            startup_lineage,
            attempt_lineage_filter,
        ):
            return False
        if read_lineage and not _attempt_lineage_matches_filter(
            read_lineage,
            attempt_lineage_filter,
        ):
            return False
    elif startup_lineage and read_lineage:
        for field in MF_ROUTE_ATTEMPT_LINEAGE_FILTER_FIELDS:
            left = startup_lineage.get(field, "")
            right = read_lineage.get(field, "")
            if left and right and left != right:
                return False

    for field in ("actual_cwd", "actual_git_root", "branch", "head_commit"):
        startup_value = _first_deep_text(startup_event, field)
        read_value = _first_deep_text(read_receipt_event, field)
        if startup_value and read_value and startup_value != read_value:
            return False

    return True


def _read_receipt_lineage_filter_from_route_gate(
    route_context_gate: dict[str, Any] | None,
) -> dict[str, str]:
    gate = _mapping(route_context_gate)
    cleanup = _mapping(gate.get("route_identity_cleanup"))
    attempt = _mapping(_mapping(gate.get("attempt_lineage")).get("lineage"))
    if not any(
        str(attempt.get(field) or "").strip()
        for field in MF_ROUTE_ATTEMPT_LINEAGE_FILTER_FIELDS
    ):
        attempt = {}
    if not cleanup.get("applied") and not attempt:
        return {}
    if cleanup.get("applied"):
        identity_source = cleanup.get("route_identity")
    else:
        identity_source = gate.get("route_identity")
    identity = _mapping(identity_source)
    lineage_filter = {
        field: str(identity.get(field) or "").strip()
        for field in (*MF_ROUTE_IDENTITY_FIELDS, *MF_ROUTE_OPTIONAL_IDENTITY_FIELDS)
        if str(identity.get(field) or "").strip()
    }
    lineage_filter.update({
        field: str(attempt.get(field) or "").strip()
        for field in MF_ROUTE_ATTEMPT_LINEAGE_FILTER_FIELDS
        if str(attempt.get(field) or "").strip()
    })
    return lineage_filter


def read_receipt_lineage_filter_from_route_gate(
    route_context_gate: dict[str, Any] | None,
) -> dict[str, str]:
    return _read_receipt_lineage_filter_from_route_gate(route_context_gate)


def _read_receipt_filter_route_identity(
    lineage_filter: dict[str, str],
) -> dict[str, str]:
    return {
        field: str(lineage_filter.get(field) or "").strip()
        for field in (*MF_ROUTE_IDENTITY_FIELDS, *MF_ROUTE_OPTIONAL_IDENTITY_FIELDS)
        if str(lineage_filter.get(field) or "").strip()
    }


def _read_receipt_filter_attempt_lineage(
    lineage_filter: dict[str, str],
) -> dict[str, str]:
    return {
        field: str(lineage_filter.get(field) or "").strip()
        for field in MF_ROUTE_ATTEMPT_LINEAGE_FILTER_FIELDS
        if str(lineage_filter.get(field) or "").strip()
    }


def _attempt_lineage_matches_filter(
    lineage: dict[str, str],
    filter_lineage: dict[str, str],
) -> bool:
    if not filter_lineage:
        return True
    if not lineage:
        return False
    return all(
        lineage.get(field, "") == expected
        for field, expected in filter_lineage.items()
        if expected
    )


def mf_subagent_read_receipt_gate_verification(
    events: list[dict[str, Any]] | None,
    *,
    route_identity_filter: dict[str, str] | None = None,
) -> dict[str, Any]:
    rows = [event for event in (events or []) if isinstance(event, dict)]
    lineage_filter = _mapping(route_identity_filter)
    identity_filter = _read_receipt_filter_route_identity(lineage_filter)
    attempt_lineage_filter = _read_receipt_filter_attempt_lineage(lineage_filter)
    read_receipts: list[tuple[int, int, dict[str, Any]]] = []
    counted: list[tuple[int, int, dict[str, Any]]] = []
    lineage_ignored: list[dict[str, Any]] = []
    lineage_matched: list[dict[str, Any]] = []
    for index, event in enumerate(rows):
        order = _event_numeric_id(event) or index + 1
        read_receipt_event = _is_mf_subagent_read_receipt_event(event)
        counted_evidence_event = _is_counted_mf_subagent_evidence_event(event)
        if not read_receipt_event and not counted_evidence_event:
            continue
        attempt_lineage = _read_receipt_gate_attempt_lineage(event)
        attempt_lineage_matches = bool(
            attempt_lineage_filter
            and _attempt_lineage_matches_filter(
                attempt_lineage,
                attempt_lineage_filter,
            )
        )
        if identity_filter:
            identity = _read_receipt_gate_route_identity(event)
            if not identity:
                if attempt_lineage_matches:
                    pass
                else:
                    lineage_ignored.append(
                        _read_receipt_gate_event_ref(
                            event,
                            reason="missing_route_identity_for_current_lineage",
                        )
                    )
                    continue
            elif not _route_identity_matches_filter(identity, identity_filter):
                lineage_ignored.append(
                    _read_receipt_gate_event_ref(
                        event,
                        reason="superseded_route_identity",
                    )
                )
                continue
        if attempt_lineage_filter:
            if not attempt_lineage:
                lineage_ignored.append(
                    _read_receipt_gate_event_ref(
                        event,
                        reason="missing_attempt_lineage_for_current_route",
                    )
                )
                continue
            if not _attempt_lineage_matches_filter(
                attempt_lineage,
                attempt_lineage_filter,
            ):
                lineage_ignored.append(
                    _read_receipt_gate_event_ref(
                        event,
                        reason="superseded_attempt_lineage",
                    )
                )
                continue
        if lineage_filter:
            lineage_matched.append(_read_receipt_gate_event_ref(event))
        if read_receipt_event:
            read_receipts.append((order, index, event))
        elif counted_evidence_event:
            counted.append((order, index, event))
    first_read = min(read_receipts, default=None, key=lambda item: (item[0], item[1]))
    harmless_startup_event_ids: list[Any] = []
    order_counted = counted
    if first_read is not None:
        order_counted = []
        for item in counted:
            if (
                (item[0], item[1]) < (first_read[0], first_read[1])
                and _read_receipt_startup_order_is_harmless(
                    item[2],
                    first_read[2],
                    identity_filter=identity_filter,
                    attempt_lineage_filter=attempt_lineage_filter,
                )
            ):
                event_id = item[2].get("id")
                if event_id is not None:
                    harmless_startup_event_ids.append(event_id)
                continue
            order_counted.append(item)
    first_counted = min(order_counted, default=None, key=lambda item: (item[0], item[1]))
    required = bool(counted)
    read_receipt_order = (first_read[0], first_read[1]) if first_read else None
    first_counted_order = (first_counted[0], first_counted[1]) if first_counted else None
    missing_receipt = bool(required and first_read is None)
    out_of_order = bool(
        required
        and read_receipt_order is not None
        and first_counted_order is not None
        and read_receipt_order > first_counted_order
    )
    passed = not required or (
        first_read is not None
        and first_counted is not None
        and read_receipt_order <= first_counted_order
    )
    status = "passed"
    failure_reason = ""
    if not passed:
        if missing_receipt:
            status = "missing"
            failure_reason = "worker_read_receipt_missing_before_counted_evidence"
        elif out_of_order:
            status = "out_of_order"
            failure_reason = "worker_read_receipt_recorded_after_counted_evidence"
        else:
            status = "failed"
            failure_reason = "worker_read_receipt_order_gate_failed"
    return {
        "schema_version": MF_SUBAGENT_READ_RECEIPT_GATE_SCHEMA_VERSION,
        "required": required,
        "passed": passed,
        "status": status,
        "read_receipt_event_id": first_read[2].get("id") if first_read else None,
        "read_receipt_hash": (
            _read_receipt_hash_from_container(_mapping(first_read[2].get("payload")))
            if first_read
            else ""
        ),
        "first_counted_evidence_event_id": (
            first_counted[2].get("id") if first_counted else None
        ),
        "counted_evidence_event_ids": [
            item[2].get("id") for item in counted if item[2].get("id") is not None
        ],
        "missing_reason": (
            ""
            if passed
            else "worker_read_receipt_must_precede_graph_query_write_startup_evidence"
        ),
        "failure_reason": failure_reason,
        "read_receipt_precedes_counted_evidence": bool(passed and required),
        "harmless_startup_before_read_receipt_event_ids": harmless_startup_event_ids,
        "read_receipt_order": list(read_receipt_order) if read_receipt_order else [],
        "first_counted_evidence_order": list(first_counted_order)
        if first_counted_order
        else [],
        "lineage_filter_applied": bool(lineage_filter),
        "lineage_route_identity": lineage_filter,
        "lineage_identity_filter": identity_filter,
        "lineage_attempt_filter": attempt_lineage_filter,
        "runtime_context_projection_evidence_fields": {
            "schema_version": "runtime_context.timeline_evidence_fields.v1",
            "read_receipt": list(RUNTIME_CONTEXT_TIMELINE_READ_RECEIPT_FIELDS),
            "attempt_lineage_filter": list(MF_ROUTE_ATTEMPT_LINEAGE_FILTER_FIELDS),
            "route_identity_filter": list(
                (*MF_ROUTE_IDENTITY_FIELDS, *MF_ROUTE_OPTIONAL_IDENTITY_FIELDS)
            ),
            "ordering": [
                "read_receipt_order",
                "first_counted_evidence_order",
                "read_receipt_precedes_counted_evidence",
            ],
        },
        "lineage_matched_event_ids": [
            item.get("id") for item in lineage_matched if item.get("id") is not None
        ],
        "lineage_ignored_event_ids": [
            item.get("id") for item in lineage_ignored if item.get("id") is not None
        ],
        "lineage_ignored_events": lineage_ignored,
    }


_CONTRACT_HASH_FIELD_NAMES = {
    "canonical_visible_contract_text_hash",
    "visible_contract_text_hash",
    "contract_hash",
    "previous_revision_hash",
}


def _collect_contract_hashes(value: Any, hashes: set[str], *, depth: int = 0) -> None:
    if depth > 5:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _CONTRACT_HASH_FIELD_NAMES and str(child or "").startswith("sha256:"):
                hashes.add(str(child))
            _collect_contract_hashes(child, hashes, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            _collect_contract_hashes(child, hashes, depth=depth + 1)


def mf_contract_projection(
    events: list[dict[str, Any]] | None,
    contract: dict[str, Any] | None = None,
    route_context_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = [event for event in (events or []) if isinstance(event, dict)]
    root = _contract_root(contract)
    explicit_contract_hash = str(
        root.get("canonical_visible_contract_text_hash")
        or root.get("visible_contract_text_hash")
        or root.get("contract_hash")
        or ""
    ).strip()
    contract_hash = explicit_contract_hash
    contract_hash_source = "explicit" if explicit_contract_hash else ""
    if root and not contract_hash:
        contract_hash = _canonical_contract_hash(root)
        contract_hash_source = "generated"
    observed_hashes: set[str] = set()
    for event in rows:
        _collect_contract_hashes(_mapping(event.get("payload")), observed_hashes)
        _collect_contract_hashes(_mapping(event.get("verification")), observed_hashes)
        _collect_contract_hashes(_mapping(event.get("artifact_refs")), observed_hashes)
    watermark = max((_event_numeric_id(event) for event in rows), default=0) or len(rows)
    route_gate = _mapping(route_context_gate)
    if not route_gate:
        route_gate = mf_route_context_gate_verification(rows, contract)
    read_receipt_gate = mf_subagent_read_receipt_gate_verification(
        rows,
        route_identity_filter=_read_receipt_lineage_filter_from_route_gate(route_gate),
    )
    divergent = bool(
        explicit_contract_hash
        and observed_hashes
        and explicit_contract_hash not in observed_hashes
    )
    stale = bool(
        divergent
        or (root and not rows)
        or (
            read_receipt_gate.get("required")
            and not read_receipt_gate.get("passed")
        )
    )
    status = "no_contract"
    if root:
        status = "divergent" if divergent else ("stale" if stale else "current")
    return {
        "schema_version": MF_CONTRACT_PROJECTION_SCHEMA_VERSION,
        "source_of_truth": "Contract/Revision/Event",
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
        "stale": stale,
        "divergent": divergent,
        "contract_hash": contract_hash,
        "contract_hash_explicit": bool(explicit_contract_hash),
        "contract_hash_source": contract_hash_source,
        "observed_contract_hashes": sorted(observed_hashes),
        "read_receipt_gate": read_receipt_gate,
    }


def _projection_close_gate_required(
    contract_projection: dict[str, Any],
    route_context_gate: dict[str, Any],
) -> bool:
    return bool(
        contract_projection.get("contract_hash_explicit")
        or contract_projection.get("observed_contract_hashes")
    )


def mf_contract_projection_close_gate_verification(
    contract_projection: dict[str, Any] | None,
    route_context_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    projection = _mapping(contract_projection)
    route_gate = _mapping(route_context_gate)
    required = _projection_close_gate_required(projection, route_gate)
    read_receipt_gate = _mapping(projection.get("read_receipt_gate"))
    missing: list[str] = []
    if required and projection.get("stale"):
        missing.append("contract_projection_current")
    if required and projection.get("divergent"):
        missing.append("contract_projection_not_divergent")
    if required and read_receipt_gate.get("required") and not read_receipt_gate.get("passed"):
        missing.append("mf_subagent_read_receipt_gate")
    passed = not required or not missing
    return {
        "schema_version": "mf_contract_projection_close_gate.v1",
        "required": required,
        "passed": passed,
        "status": "passed" if passed else "failed",
        "missing_requirement_ids": missing,
        "contract_projection_status": str(projection.get("status") or ""),
        "contract_projection_stale": bool(projection.get("stale")),
        "contract_projection_divergent": bool(projection.get("divergent")),
        "read_receipt_gate_status": str(read_receipt_gate.get("status") or ""),
        "read_receipt_gate_required": bool(read_receipt_gate.get("required")),
    }


def _post_verification_impact_policy(contract: dict[str, Any] | None) -> dict[str, Any]:
    root = _contract_root(contract)
    policy = _mapping(root.get("verification_route_policy"))
    impact = _mapping(
        policy.get("post_verification_impact_actions")
        or policy.get("impact_actions")
        or root.get("post_verification_impact_actions")
        or root.get("post_verification_actions")
    )
    actions = _string_list(impact.get("actions") or impact.get("required_actions"))
    required = bool(impact.get("required")) or bool(actions)
    return {
        "required": required,
        "actions": actions or (["post_verification_impact_actions"] if required else []),
        "requires_observer": bool(impact.get("requires_observer", True)),
    }


def _post_verification_actions_from_event(event: dict[str, Any]) -> set[str]:
    actions: set[str] = set()
    if not _event_passed(event):
        return actions
    for value in _event_field_values(
        event,
        {
            "post_verification_impact_actions",
            "post_verification_actions",
        },
    ):
        for item in _list(value) or [value]:
            item_map = _mapping(item)
            if item_map:
                status = str(item_map.get("status") or event.get("status") or "").lower()
                follow_up = _truthy(item_map.get("follow_up_filed")) or _truthy(
                    item_map.get("follow_up_recorded")
                )
                if status not in MF_CLOSE_PASS_STATUSES and not follow_up:
                    continue
                item_actions = _string_list(
                    item_map.get("actions") or item_map.get("action_ids")
                )
                action = str(item_map.get("action") or item_map.get("id") or "").strip()
                if action:
                    item_actions.append(action)
                actions.update(item_actions or ["post_verification_impact_actions"])
            elif isinstance(item, str) and item.strip():
                actions.add(item.strip())
    return actions


def mf_post_verification_actions_gate_verification(
    events: list[dict[str, Any]] | None,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = _post_verification_impact_policy(contract)
    required_actions = list(policy["actions"])
    present: set[str] = set()
    evidence_events: list[dict[str, Any]] = []
    for event in events if isinstance(events, list) else []:
        event = _mapping(event)
        event_actions = _post_verification_actions_from_event(event)
        if not event_actions:
            continue
        present.update(event_actions)
        evidence_events.append({
            "id": event.get("id") or event.get("event_id"),
            "event_kind": event.get("event_kind"),
            "phase": event.get("phase"),
            "status": event.get("status"),
            "actions": sorted(event_actions),
        })
    missing = [action for action in required_actions if action not in present]
    passed = not policy["required"] or not missing
    return {
        "schema_version": "mf_post_verification_actions_gate.v1",
        "required": bool(policy["required"]),
        "passed": passed,
        "status": "passed" if passed else "follow_up_required",
        "required_actions": required_actions,
        "present_actions": sorted(present),
        "missing_actions": missing,
        "requires_observer": bool(policy["requires_observer"]),
        "evidence_events": evidence_events,
        "follow_up": {
            "required": bool(policy["required"] and missing),
            "actions": missing,
            "next_action": "record observer-owned post-verification action or follow-up evidence",
        },
    }


def _copy_route_payload_fields(source: dict[str, Any], payload: dict[str, Any]) -> None:
    for key in (
        "priority",
        "selected_topology",
        "recommended_topology",
        "topology",
        "target_files",
        "test_files",
        "changed_files",
        "owned_files",
        "risk_class",
        "summary",
        "task_summary",
        "title",
        "caller_role",
        "observer_direct_mutation",
        "same_observer_direct_mutation",
        "direct_mutation",
        "implementation_mutation_requested",
    ):
        if key in source and source.get(key) not in (None, "", [], {}):
            payload[key] = source[key]


def _route_topology_policy(contract: dict[str, Any] | None) -> dict[str, Any]:
    data = _mapping(contract)
    root = _contract_root(contract)
    payload: dict[str, Any] = {}

    for source in (
        data,
        _mapping(data.get("close_context")),
        root,
        _mapping(root.get("close_context")),
    ):
        _copy_route_payload_fields(source, payload)

    route_policy = _mapping(root.get("route_topology_policy"))
    _copy_route_payload_fields(route_policy, payload)
    if str(root.get("template_id") or "").strip() == "mf_parallel.v1":
        payload.setdefault("selected_topology", "observer_led_parallel_lanes")
        payload.setdefault("recommended_topology", "mf_parallel.v1")
        _copy_route_payload_fields(_mapping(route_policy.get("high_risk")), payload)

    try:
        from .service_registry import classify_route_topology

        return classify_route_topology(payload)
    except Exception:
        selected = str(
            payload.get("selected_topology")
            or payload.get("recommended_topology")
            or payload.get("topology")
            or ""
        ).strip()
        high_risk = selected in {
            "observer_led_parallel_lanes",
            "mf_parallel.v1",
            "mf_parallel",
            "parallel",
        }
        return {
            "schema_version": "route_topology_selection.v1",
            "selected_topology": (
                "observer_led_parallel_lanes" if high_risk else "lightweight_single_lane"
            ),
            "recommended_topology": "mf_parallel.v1" if high_risk else "single_lane.v1",
            "required_lanes": (
                [
                    "observer_coordinator",
                    "bounded_implementation_worker",
                    "independent_verification_lane",
                    "observer_merge_close_gate",
                ]
                if high_risk
                else ["single_bounded_worker"]
            ),
            "reason_codes": ["explicit_parallel_topology"] if high_risk else ["small_deterministic"],
            "independent_verification_required": high_risk,
        }


def _route_context_required(topology_policy: dict[str, Any]) -> bool:
    selected = str(topology_policy.get("selected_topology") or "").strip()
    recommended = str(topology_policy.get("recommended_topology") or "").strip()
    required_lanes = {str(item).strip() for item in _list(topology_policy.get("required_lanes"))}
    return (
        selected == "observer_led_parallel_lanes"
        or recommended == "mf_parallel.v1"
        or "bounded_implementation_worker" in required_lanes
    )


def _route_marker(value: Any) -> str:
    return re.sub(r"[\s.\-]+", "_", str(value or "").strip().lower())


def _route_independent_verification_required(topology_policy: dict[str, Any]) -> bool:
    required_lanes: set[str] = set()
    for item in _list(topology_policy.get("required_lanes")):
        if isinstance(item, dict):
            required_lanes.update(
                _route_marker(item.get(key))
                for key in ("id", "requirement_id", "role", "lane", "kind", "type", "name")
                if item.get(key)
            )
        else:
            required_lanes.add(_route_marker(item))
    return bool(topology_policy.get("independent_verification_required")) or bool(
        required_lanes.intersection(
            {
                "independent_verification_lane",
                "independent_verification",
                "qa",
                "qa_lane",
                "qa_role",
                "qa_verification",
                "independent_qa",
                "independent_qa_lane",
            }
        )
    )


def _route_architecture_review_required(
    topology_policy: dict[str, Any],
    contract: dict[str, Any] | None,
) -> bool:
    architecture_markers = {
        "architecture_review_lane",
        "architecture_review",
        "architecture_data_continuity_review",
        "architecture_lane",
        "arch_review",
    }
    for key in (
        "architecture_review_required",
        "require_architecture_review",
        "architecture_data_continuity_review_required",
    ):
        if _truthy(topology_policy.get(key)):
            return True

    for item in _list(topology_policy.get("required_lanes")):
        if isinstance(item, dict) and item.get("required") is False:
            continue
        if isinstance(item, dict):
            markers = {
                _route_marker(item.get(key))
                for key in (
                    "id",
                    "requirement_id",
                    "role",
                    "lane",
                    "kind",
                    "type",
                    "name",
                )
                if item.get(key)
            }
        else:
            markers = {_route_marker(item)}
        if markers.intersection(architecture_markers):
            return True

    for obj in _contract_walk(_mapping(contract), max_depth=5):
        for key in (
            "architecture_review_required",
            "require_architecture_review",
            "architecture_data_continuity_review_required",
        ):
            if _truthy(obj.get(key)):
                return True
        for key in (
            "required_lanes",
            "required_evidence",
            "required_evidence_ids",
            "evidence_requirements",
            "contract_evidence",
        ):
            for item in _list(obj.get(key)):
                if isinstance(item, dict) and item.get("required") is False:
                    continue
                if isinstance(item, dict):
                    markers = {
                        _route_marker(item.get(field))
                        for field in (
                            "id",
                            "requirement_id",
                            "role",
                            "lane",
                            "kind",
                            "type",
                            "name",
                        )
                        if item.get(field)
                    }
                else:
                    markers = {_route_marker(item)}
                if markers.intersection(architecture_markers):
                    return True
    return False


def _first_deep_mapping(value: Any, key: str) -> dict[str, Any]:
    if isinstance(value, dict):
        if key in value and isinstance(value.get(key), dict):
            return value.get(key) or {}
        for child in value.values():
            found = _first_deep_mapping(child, key)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_deep_mapping(child, key)
            if found:
                return found
    return {}


def _first_deep_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value and value.get(key) not in (None, "", [], {}):
            return value.get(key)
        for child in value.values():
            found = _first_deep_value(child, key)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_deep_value(child, key)
            if found not in (None, "", [], {}):
                return found
    return None


def _first_deep_text(value: Any, key: str) -> str:
    if isinstance(value, dict):
        if key in value and str(value.get(key) or "").strip():
            return str(value.get(key) or "").strip()
        for child in value.values():
            found = _first_deep_text(child, key)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_deep_text(child, key)
            if found:
                return found
    return ""


def _route_identity(value: Any) -> dict[str, str]:
    identity = {field: _first_deep_text(value, field) for field in MF_ROUTE_IDENTITY_FIELDS}
    for field in MF_ROUTE_OPTIONAL_IDENTITY_FIELDS:
        optional = _first_deep_text(value, field)
        if optional:
            identity[field] = optional
    return identity if all(identity.values()) else {}


def _route_attempt_lineage(value: Any) -> dict[str, str]:
    runtime_context_id = _first_deep_text(value, "runtime_context_id")
    task_id = _first_deep_text(value, "task_id")
    parent_task_id = _first_deep_text(value, "parent_task_id")
    if not parent_task_id and runtime_context_id and task_id and isinstance(value, dict):
        parent_task_id = str(value.get("backlog_id") or "").strip()
    lineage = {
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "worker_slot_id": (
            _first_deep_text(value, "worker_slot_id")
            or _first_deep_text(value, "worker_id")
        ),
        "fence_token": _first_deep_text(value, "fence_token"),
    }
    return {
        field: token
        for field, token in lineage.items()
        if field in MF_ROUTE_ATTEMPT_LINEAGE_FIELDS and token
    }


def _runtime_dispatch_evidence(value: Any) -> dict[str, Any]:
    owned_files = _string_list(
        _first_deep_value(value, "owned_files")
        or _first_deep_value(value, "target_files")
        or []
    )
    return {
        "route_id": _first_deep_text(value, "route_id"),
        "route_context_hash": _first_deep_text(value, "route_context_hash"),
        "prompt_contract_id": _first_deep_text(value, "prompt_contract_id"),
        "prompt_contract_hash": _first_deep_text(value, "prompt_contract_hash"),
        "visible_injection_manifest_hash": _first_deep_text(
            value,
            "visible_injection_manifest_hash",
        ),
        "runtime_context_id": _first_deep_text(value, "runtime_context_id"),
        "task_id": _first_deep_text(value, "task_id"),
        "parent_task_id": _first_deep_text(value, "parent_task_id"),
        "worker_slot_id": (
            _first_deep_text(value, "worker_slot_id")
            or _first_deep_text(value, "worker_id")
        ),
        "fence_token": _first_deep_text(value, "fence_token"),
        "worktree_path": (
            _first_deep_text(value, "worktree_path")
            or _first_deep_text(value, "assigned_worktree")
            or _first_deep_text(value, "worktree")
        ),
        "branch": (
            _first_deep_text(value, "branch")
            or _first_deep_text(value, "branch_ref")
        ),
        "base_commit": _first_deep_text(value, "base_commit"),
        "target_head_commit": _first_deep_text(value, "target_head_commit"),
        "merge_queue_id": _first_deep_text(value, "merge_queue_id"),
        "owned_files": owned_files,
        "read_receipt_event_id": (
            _first_deep_text(value, "read_receipt_event_id")
            or _first_deep_text(value, "read_receipt_event_ref")
        ),
        "startup_event_id": (
            _first_deep_text(value, "startup_event_id")
            or _first_deep_text(value, "startup_event_ref")
        ),
    }


def _route_identity_key(identity: dict[str, str]) -> tuple[str, ...]:
    return tuple(identity.get(field, "") for field in MF_ROUTE_IDENTITY_FIELDS)


def _route_identity_matches_filter(
    identity: dict[str, str],
    filter_identity: dict[str, str],
) -> bool:
    if not filter_identity:
        return True
    if _route_identity_key(identity) != _route_identity_key(filter_identity):
        return False
    expected_prompt_hash = filter_identity.get("prompt_contract_hash", "")
    return not expected_prompt_hash or identity.get("prompt_contract_hash", "") == expected_prompt_hash


def _route_child_identity_join_verified(value: Any) -> bool:
    join = _first_deep_mapping(value, "identity_join")
    return bool(
        _truthy(join.get("route_identity_matches_latest_contract"))
        and _truthy(join.get("read_receipt_lineage_present"))
    )


def _route_parent_child_startup_identity(
    value: Any,
    identity: dict[str, str],
    *,
    parent_identity_hint: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Normalize a truthful child startup event back to its parent route identity."""

    if not identity:
        return identity, {}
    parent_lineage = _first_deep_mapping(value, "parent_route_lineage")
    child_lineage = _first_deep_mapping(value, "child_route_lineage")
    parent_prompt_contract_id = (
        _first_deep_text(value, "parent_prompt_contract_id")
        or str(parent_lineage.get("prompt_contract_id") or "").strip()
    )
    child_prompt_contract_id = (
        _first_deep_text(value, "child_prompt_contract_id")
        or str(child_lineage.get("prompt_contract_id") or "").strip()
        or identity.get("prompt_contract_id", "")
    )
    parent_route_context_hash = (
        _first_deep_text(value, "parent_route_context_hash")
        or str(parent_lineage.get("route_context_hash") or "").strip()
        or identity.get("route_context_hash", "")
    )
    child_route_context_hash = (
        _first_deep_text(value, "child_route_context_hash")
        or str(child_lineage.get("route_context_hash") or "").strip()
        or identity.get("route_context_hash", "")
    )
    parent_visible_manifest_hash = (
        _first_deep_text(value, "parent_visible_injection_manifest_hash")
        or str(parent_lineage.get("visible_injection_manifest_hash") or "").strip()
    )
    child_visible_manifest_hash = (
        _first_deep_text(value, "child_visible_injection_manifest_hash")
        or str(child_lineage.get("visible_injection_manifest_hash") or "").strip()
        or _first_deep_text(value, "visible_injection_manifest_hash")
    )
    parent_prompt_contract_hash = (
        _first_deep_text(value, "parent_prompt_contract_hash")
        or str(parent_lineage.get("prompt_contract_hash") or "").strip()
    )

    lineage_valid = bool(
        parent_prompt_contract_id
        and child_prompt_contract_id
        and parent_prompt_contract_id != child_prompt_contract_id
        and parent_route_context_hash
        and child_route_context_hash
        and parent_route_context_hash == child_route_context_hash
        and parent_route_context_hash == identity.get("route_context_hash", "")
        and parent_visible_manifest_hash
        and child_visible_manifest_hash
        and parent_visible_manifest_hash == child_visible_manifest_hash
    )
    if not lineage_valid:
        parent_hint = {
            field: str(_mapping(parent_identity_hint).get(field) or "").strip()
            for field in (*MF_ROUTE_IDENTITY_FIELDS, *MF_ROUTE_OPTIONAL_IDENTITY_FIELDS)
            if str(_mapping(parent_identity_hint).get(field) or "").strip()
        }
        if parent_hint and _route_child_identity_join_verified(value):
            normalized = dict(parent_hint)
            return normalized, {
                "schema_version": "mf_subagent_startup_lineage_acceptance.v1",
                "accepted": True,
                "acceptance_source": "identity_join_latest_contract",
                "parent_prompt_contract_id": parent_hint.get("prompt_contract_id", ""),
                "child_prompt_contract_id": child_prompt_contract_id,
                "route_context_hash": parent_hint.get("route_context_hash", ""),
                "child_route_context_hash": child_route_context_hash,
                "read_receipt_lineage_present": True,
                "route_identity_matches_latest_contract": True,
            }
        return identity, {}

    normalized = {
        "route_context_hash": parent_route_context_hash,
        "prompt_contract_id": parent_prompt_contract_id,
    }
    if parent_prompt_contract_hash:
        normalized["prompt_contract_hash"] = parent_prompt_contract_hash
    return normalized, {
        "schema_version": "mf_subagent_startup_lineage_acceptance.v1",
        "accepted": True,
        "parent_prompt_contract_id": parent_prompt_contract_id,
        "child_prompt_contract_id": child_prompt_contract_id,
        "route_context_hash": parent_route_context_hash,
        "visible_injection_manifest_hash": parent_visible_manifest_hash,
    }


def _route_parent_child_dispatch_identity(
    value: Any,
    identity: dict[str, str],
    *,
    parent_identity_hint: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Normalize a service-generated child dispatch back to its parent route.

    This is deliberately narrower than startup normalization: only runtime-text
    dispatch evidence generated by the service has enough lineage to bridge a
    child route identity back to the observer's parent route.
    """

    if not identity:
        return identity, {}
    parent_hint = {
        field: str(_mapping(parent_identity_hint).get(field) or "").strip()
        for field in (*MF_ROUTE_IDENTITY_FIELDS, *MF_ROUTE_OPTIONAL_IDENTITY_FIELDS)
        if str(_mapping(parent_identity_hint).get(field) or "").strip()
    }
    if not parent_hint or _route_identity_key(identity) == _route_identity_key(parent_hint):
        return identity, {}
    source = _first_deep_text(value, "source")
    service_generated = _truthy(_first_deep_value(value, "service_generated"))
    runtime_context_id = _first_deep_text(value, "runtime_context_id")
    task_id = _first_deep_text(value, "task_id")
    parent_task_id = _first_deep_text(value, "parent_task_id")
    route_token_ref = _first_deep_text(value, "route_token_ref")
    worker_role = _first_deep_text(value, "worker_role").lower()
    worker_slot_id = (
        _first_deep_text(value, "worker_slot_id")
        or _first_deep_text(value, "worker_id")
    )
    child_route_id = _first_deep_text(value, "route_id")
    strict_lineage = bool(
        service_generated
        and source == "observer_runtime_text_prepare"
        and runtime_context_id
        and task_id
        and parent_task_id
        and task_id != parent_task_id
        and route_token_ref
        and worker_slot_id
        and child_route_id
        and identity.get("route_context_hash")
        and identity.get("prompt_contract_id")
        and worker_role in {"", "mf_sub", "implementation_worker"}
    )
    if not strict_lineage:
        return identity, {}
    return dict(parent_hint), {
        "schema_version": "bounded_worker_dispatch_child_route_lineage.v1",
        "accepted": True,
        "acceptance_source": "service_generated_runtime_text_dispatch",
        "source": source,
        "service_generated": True,
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "worker_slot_id": worker_slot_id,
        "route_token_ref": route_token_ref,
        "parent_route_context_hash": parent_hint.get("route_context_hash", ""),
        "child_route_context_hash": identity.get("route_context_hash", ""),
        "parent_prompt_contract_id": parent_hint.get("prompt_contract_id", ""),
        "child_prompt_contract_id": identity.get("prompt_contract_id", ""),
        "child_route_id": child_route_id,
    }


def _route_action_scoped_verification_identity(
    value: Any,
    identity: dict[str, str],
    *,
    parent_identity_hint: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Normalize protected QA action-scope evidence back to its parent route."""

    if not identity:
        return identity, {}
    parent_hint = {
        field: str(_mapping(parent_identity_hint).get(field) or "").strip()
        for field in (*MF_ROUTE_IDENTITY_FIELDS, *MF_ROUTE_OPTIONAL_IDENTITY_FIELDS)
        if str(_mapping(parent_identity_hint).get(field) or "").strip()
    }
    if not parent_hint or _route_identity_key(identity) == _route_identity_key(parent_hint):
        return identity, {}

    route_token_ref = _first_deep_text(value, "route_token_ref")
    route_identity = _first_deep_mapping(value, "route_identity")
    allowed_action = str(
        route_identity.get("allowed_action")
        or route_identity.get("action")
        or ""
    ).strip()
    meta_gate = _first_deep_mapping(value, "meta_contract_gate")
    meta_role = str(meta_gate.get("role") or "").strip().lower()
    meta_action = str(meta_gate.get("action") or "").strip().lower()
    reviewer_role = _first_deep_text(value, "reviewer_role").lower()
    protected_qa_append = bool(
        route_token_ref
        and allowed_action == "task_timeline_append"
        and meta_role == "qa"
        and meta_action in {"qa_verification", "independent_verification", "qa_review"}
        and reviewer_role in {"qa", "independent_qa", "independent_verification"}
    )
    if not protected_qa_append:
        return identity, {}

    return dict(parent_hint), {
        "schema_version": "independent_verification_action_scope_lineage.v1",
        "accepted": True,
        "acceptance_source": "protected_route_token_ref_action_scope",
        "route_token_ref": route_token_ref,
        "allowed_action": allowed_action,
        "parent_route_context_hash": parent_hint.get("route_context_hash", ""),
        "child_route_context_hash": identity.get("route_context_hash", ""),
        "parent_prompt_contract_id": parent_hint.get("prompt_contract_id", ""),
        "child_prompt_contract_id": identity.get("prompt_contract_id", ""),
    }


def _route_visible_manifest_present(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return bool(
        _first_deep_text(value, "visible_injection_manifest_hash")
        or _first_deep_text(value, "visible_injection_manifest")
    )


def _route_event_passed(event: dict[str, Any]) -> bool:
    status = str(event.get("status") or event.get("decision") or "").strip().lower()
    return bool(event.get("passed")) or status in MF_ROUTE_CONTEXT_PASS_STATUSES


def _route_startup_fence_evidence_present(event: dict[str, Any]) -> bool:
    for key in (
        "fence_token",
        "worker_fence_token",
        "route_fence_token",
        "actual_fence_token",
        "reported_fence_token",
        "fence_token_hash",
    ):
        if _first_deep_text(event, key):
            return True
    for key in (
        "fence_token_present",
        "actual_fence_token_present",
        "fence_token_matches",
    ):
        if _truthy(_first_deep_text(event, key)):
            return True
    return False


def _route_actual_startup_identity_present(event: dict[str, Any]) -> bool:
    actual_runtime = _mapping(_first_deep_mapping(event, "actual_runtime"))
    actual_cwd = _first_deep_text(event, "actual_cwd") or str(
        actual_runtime.get("cwd") or ""
    ).strip()
    actual_git_root = _first_deep_text(event, "actual_git_root") or str(
        actual_runtime.get("git_root") or ""
    ).strip()
    branch = (
        _first_deep_text(event, "branch")
        or _first_deep_text(event, "branch_ref")
        or str(actual_runtime.get("branch") or actual_runtime.get("branch_ref") or "").strip()
    )
    head_commit = (
        _first_deep_text(event, "head_commit")
        or _first_deep_text(event, "branch_head")
        or str(
            actual_runtime.get("head_commit") or actual_runtime.get("branch_head") or ""
        ).strip()
    )
    return bool(
        (actual_cwd or actual_git_root)
        and branch
        and head_commit
        and _route_startup_fence_evidence_present(event)
    )


def _mf_subagent_finish_gate_projection(event: dict[str, Any]) -> dict[str, Any]:
    """Return close-gate-consumable finish evidence only after upstream gates pass."""

    if not isinstance(event, dict):
        return {}
    gate = (
        _mapping(_first_deep_mapping(event, "mf_subagent_finish_gate"))
        or _mapping(_first_deep_mapping(event, "finish_gate"))
        or _mapping(_first_deep_mapping(event, "finish_gate_result"))
    )
    if not gate:
        return {}
    if not _route_event_passed(event):
        return {}
    if _route_marker(event.get("event_kind") or event.get("event_type")) not in {
        "mf_subagent_finish_gate",
        "mf_subagent.finish_gate",
    }:
        marker = _route_marker(
            gate.get("source_event_kind")
            or gate.get("schema_version")
            or event.get("phase")
        )
        if "mf_subagent_finish_gate" not in marker:
            return {}
    if not _truthy(gate.get("close_ready")):
        return {}
    receipt_gate = _mapping(gate.get("receipt_gate"))
    startup_identity_gate = _mapping(gate.get("startup_worker_identity_gate"))
    worker_attestation_gate = _mapping(gate.get("worker_self_attestation_gate"))
    if str(receipt_gate.get("status") or "").strip().lower() not in MF_CLOSE_PASS_STATUSES:
        return {}
    if not (
        _truthy(receipt_gate.get("read_receipt_present"))
        and _truthy(receipt_gate.get("read_receipt_event_id_present"))
        and _truthy(receipt_gate.get("startup_present"))
        and _truthy(receipt_gate.get("observer_command_id_present"))
    ):
        return {}
    if not _truthy(startup_identity_gate.get("passed")):
        return {}
    if not _truthy(worker_attestation_gate.get("passed")):
        return {}
    if not _string_list(gate.get("changed_files")):
        return {}
    if not (
        _first_deep_text(gate, "head_commit")
        or _first_deep_text(gate, "validated_head_commit")
        or _first_deep_text(gate, "commit")
    ):
        return {}
    startup_evidence = _mapping(gate.get("startup_evidence"))
    if not startup_evidence or not _route_actual_startup_identity_present(
        {"payload": {"startup_evidence": startup_evidence}}
    ):
        return {}
    return gate


def _mf_subagent_finish_gate_projected_event_kinds(
    event: dict[str, Any],
) -> set[str]:
    gate = _mf_subagent_finish_gate_projection(event)
    if not gate:
        return set()
    projected = {"implementation", "close_ready"}
    test_results = _mapping(gate.get("test_results"))
    if str(test_results.get("status") or "").strip().lower() in MF_CLOSE_PASS_STATUSES:
        projected.add("verification")
    return projected


def _route_event_is_startup_adoption(event: dict[str, Any]) -> bool:
    """Detect an existing-worktree adoption startup-equivalent event."""

    markers = {_route_marker(marker) for marker in _route_event_markers(event)}
    if markers.intersection(MF_STARTUP_ADOPTION_EVENT_MARKERS):
        return True
    adoption_mode = _route_marker(_first_deep_text(event, "branch_adoption_mode"))
    if adoption_mode in MF_BRANCH_ADOPTION_MODES:
        return True
    return bool(_first_deep_mapping(event, "branch_adoption_evidence"))


def _route_startup_adoption_evidence_valid(event: dict[str, Any]) -> bool:
    """Require a truthful adoption attestation bound to the recorded head.

    Adoption never satisfies the startup requirement when evidence is missing
    or the attested adopted head does not equal the event's ACTUAL recorded
    head: that would accept a false started-now/at-base claim.
    """

    evidence = _first_deep_mapping(event, "branch_adoption_evidence")
    if not evidence:
        return False
    adopted_branch_ref = str(evidence.get("adopted_branch_ref") or "").strip()
    adopted_base_commit = str(evidence.get("adopted_base_commit") or "").strip()
    adopted_head_commit = str(evidence.get("adopted_head_commit") or "").strip()
    attestation_source = str(
        evidence.get("attestation_source")
        or evidence.get("attested_by")
        or evidence.get("attestation")
        or ""
    ).strip()
    if not (
        adopted_branch_ref
        and adopted_base_commit
        and adopted_head_commit
        and attestation_source
    ):
        return False
    actual_head = (
        _first_deep_text(event, "head_commit")
        or _first_deep_text(event, "branch_head")
    )
    return bool(actual_head) and actual_head == adopted_head_commit


def _route_event_is_identity_cleanup(event: dict[str, Any]) -> bool:
    markers = {_route_marker(marker) for marker in _route_event_markers(event)}
    return bool(markers.intersection(MF_ROUTE_IDENTITY_CLEANUP_MARKERS))


def _route_event_markers(event: dict[str, Any]) -> set[str]:
    markers: set[str] = set()
    for key in ("event_kind", "event_type", "phase", "schema_version"):
        value = str(event.get(key) or "").strip().lower()
        if value:
            markers.add(value)
    for key in event.keys():
        markers.add(str(key).strip().lower())
    for key in ("payload", "verification", "artifact_refs"):
        container = _mapping(event.get(key))
        for marker in container.keys():
            markers.add(str(marker).strip().lower())
        for nested_key in (
            "route_context",
            "route_prompt_bundle",
            "prompt_alert_bundle",
            "visible_injection_manifest",
            "route_action_gate",
            "route_action_precheck",
            "mf_subagent_dispatch_gate",
            "bounded_implementation_worker_dispatch",
            "mf_subagent_startup_gate",
            "mf_subagent_finish_gate",
            "finish_gate",
            "finish_gate_result",
            "dispatch_evidence",
            "startup_evidence",
            "lane_ownership_projection",
            "route_prompt_contract",
            "contract_evidence",
            "route_identity_cleanup",
            "route_identity_recovery",
            "route_identity_supersede",
            "architecture_review_lane",
            "architecture_review",
            "architecture_data_continuity_review",
            "qa_evidence_gate_review",
            "route_evidence",
        ):
            nested = container.get(nested_key)
            if isinstance(nested, dict):
                markers.add(nested_key)
                for marker in nested.keys():
                    markers.add(str(marker).strip().lower())
            for item in _list(nested):
                item = _mapping(item)
                for item_key in ("id", "requirement_id", "kind", "event_kind"):
                    value = str(item.get(item_key) or "").strip().lower()
                    if value:
                        markers.add(value)
    return markers


def _route_event_categories(event: dict[str, Any]) -> set[str]:
    markers = _route_event_markers(event)
    normalized_markers = {_route_marker(marker) for marker in markers}
    categories: set[str] = set()
    read_receipt_marker = bool(
        normalized_markers.intersection(
            {
                "mf_subagent_read_receipt",
                "mf_subagent.read_receipt",
                "read_receipt",
            }
        )
    )
    if markers.intersection(
        {
            "route_context",
            "route_prompt_bundle",
            "prompt_alert_bundle",
            "visible_injection_manifest",
            "visible_injection_manifest_hash",
        }
    ) and not read_receipt_marker:
        categories.add("route_context")
    if markers.intersection(
        {
            "route_action",
            "route_action_gate",
            "route_action_precheck",
            "action_precheck",
            "pre_mutation",
            "route.action",
            "route.action.pre_mutation",
            "route.action.requested",
            "route_action_allowed",
            "route_action_requested",
        }
    ):
        categories.add("route_action_precheck")
    if markers.intersection(
        {
            "mf_subagent_dispatch",
            "mf_subagent.dispatch",
            "mf_subagent_dispatch_gate",
            "bounded_implementation_worker_dispatch",
            "dispatch_evidence",
        }
    ):
        categories.add("bounded_implementation_worker_dispatch")
    if _mf_subagent_finish_gate_projection(event):
        categories.add("bounded_implementation_worker_dispatch")
        categories.add("mf_subagent_startup")
    if markers.intersection(
        {
            "mf_subagent_startup",
            "mf_subagent.startup",
            "mf_subagent_startup_gate",
            "startup_gate",
            "startup_evidence",
            "mf_subagent_startup_adoption",
            "mf_subagent.startup_adoption",
            "mf_subagent_startup_adoption_gate",
        }
    ):
        categories.add("mf_subagent_startup")
    if normalized_markers.intersection(
        {
            "independent_verification_lane",
            "independent_verification",
            "qa",
            "qa_lane",
            "qa_role",
            "qa_verification",
            "independent_qa",
            "independent_qa_lane",
        }
    ):
        categories.add(MF_ROUTE_CONTEXT_INDEPENDENT_VERIFICATION_ID)
    if normalized_markers.intersection(
        {
            "architecture_review_lane",
            "architecture_review",
            "architecture_data_continuity_review",
            "architecture_lane",
            "arch_review",
        }
    ):
        categories.add(MF_ROUTE_CONTEXT_ARCHITECTURE_REVIEW_ID)
    return categories


def route_context_consumption_event_summary(
    event: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return route-consumption categories and identity for one timeline event."""

    row = _mapping(event)
    if not row:
        return {
            "categories": [],
            "route_identity": {},
            "attempt_lineage": {},
            "runtime_dispatch_evidence": {},
            "passed": False,
        }
    return {
        "categories": sorted(_route_event_categories(row)),
        "route_identity": _route_identity(row),
        "attempt_lineage": _route_attempt_lineage(row),
        "runtime_dispatch_evidence": _runtime_dispatch_evidence(row),
        "passed": _route_event_passed(row),
    }


def _is_mf_subagent_finish_gate_event(event: dict[str, Any]) -> bool:
    marker = _event_marker(event)
    if any(
        token in marker
        for token in (
            "mf_subagent_finish_gate",
            "mf_subagent_finish",
            "finish_gate",
            "handoff_gate",
        )
    ):
        return "subagent" in marker or "mf_sub" in marker or "finish_gate" in marker
    return _container_has_marker_key(
        event,
        {
            "mf_subagent_finish_gate",
            "finish_gate",
            "finish_gate_result",
        },
    )


def _event_deep_string_list(event: dict[str, Any], keys: set[str]) -> list[str]:
    values: list[str] = []
    for value in _event_field_values(event, keys):
        if isinstance(value, list):
            for item in value:
                item_map = _mapping(item)
                if item_map:
                    text = (
                        str(
                            item_map.get("path")
                            or item_map.get("file")
                            or item_map.get("name")
                            or ""
                        )
                        .strip()
                    )
                    if text:
                        values.append(text)
                elif str(item or "").strip():
                    values.append(str(item).strip())
        elif str(value or "").strip():
            values.append(str(value).strip())
    return list(dict.fromkeys(values))


def _event_deep_truthy(event: dict[str, Any], keys: set[str]) -> bool:
    return any(_truthy(value) for value in _event_field_values(event, keys))


def _finish_gate_review_ready(event: dict[str, Any]) -> bool:
    if _event_deep_truthy(event, {"review_ready"}):
        return True
    for value in _event_field_values(event, {"stop_state", "worker_status", "state"}):
        if _normalize_token(value) in {"review_ready", "waiting_merge"}:
            return True
    lane_ids = _event_deep_string_list(
        event,
        {
            "present_lane_ownership_ids",
            "present_requirement_ids",
            "evidence_ids",
        },
    )
    return any(
        _normalize_token(item)
        in {
            "bounded_implementation_subagent.review_ready",
            "bounded_implementation_subagent_review_ready",
            "review_ready",
            "waiting_merge",
        }
        for item in lane_ids
    )


def _finish_gate_close_ready(event: dict[str, Any]) -> bool:
    if _event_deep_truthy(
        event,
        {
            "close_ready",
            "close_satisfying",
            "close_ready_projection",
        },
    ):
        return True
    projected = _event_deep_string_list(
        event,
        {"present_event_kinds", "projected_event_kinds", "close_evidence_kinds"},
    )
    return "close_ready" in {_normalize_token(item) for item in projected}


def _finish_gate_commit(event: dict[str, Any]) -> str:
    return (
        str(event.get("commit_sha") or "").strip()
        or _first_event_string(
            event,
            {
                "implementation_commit",
                "commit_sha",
                "commit",
            },
        )
    )


def _finish_gate_changed_files(event: dict[str, Any]) -> list[str]:
    return _event_deep_string_list(
        event,
        {
            "changed_files",
            "owned_changed_files",
            "modified_files",
            "files_changed",
        },
    )


def _finish_gate_observer_command_id(event: dict[str, Any]) -> str:
    return _first_event_string(
        event,
        {"observer_command_id", "command_id", "originating_command_id"},
    )


def _finish_gate_fence_proof_present(event: dict[str, Any]) -> bool:
    if _first_deep_text(event, "fence_token"):
        return True
    return _event_deep_truthy(
        event,
        {
            "fence_token_matches",
            "fence_token_present",
            "actual_fence_token_present",
        },
    )


def _finish_gate_missing_fields(
    event: dict[str, Any],
    *,
    route_context_gate: Mapping[str, Any] | None = None,
) -> list[str]:
    missing: list[str] = []
    if not _route_event_passed(event):
        missing.append("finish_gate_passed")
    identity = _route_identity(event)
    if not identity:
        missing.append("finish_gate_route_identity")
    else:
        gate = _mapping(route_context_gate)
        cleanup = _mapping(gate.get("route_identity_cleanup"))
        canonical = _mapping(
            cleanup.get("route_identity")
            if cleanup.get("applied")
            else gate.get("route_identity")
        )
        normalized_identity = identity
        if canonical and "mf_subagent_startup" in _route_event_categories(event):
            normalized_identity, _lineage = _route_parent_child_startup_identity(
                event,
                identity,
                parent_identity_hint=canonical,
            )
        if canonical and not _route_identity_matches_filter(normalized_identity, canonical):
            missing.append("finish_gate_route_identity_matches_canonical_route")
    lineage = _route_attempt_lineage(event)
    for field in MF_ROUTE_ATTEMPT_LINEAGE_FIELDS:
        if field == "fence_token" and _finish_gate_fence_proof_present(event):
            continue
        if not lineage.get(field):
            missing.append(f"finish_gate_{field}")
    if not _finish_gate_observer_command_id(event):
        missing.append("finish_gate_observer_command_id")
    if not _finish_gate_commit(event):
        missing.append("finish_gate_implementation_commit")
    if not _finish_gate_changed_files(event):
        missing.append("finish_gate_changed_files")
    if not _finish_gate_review_ready(event):
        missing.append("finish_gate_review_ready")
    if not _finish_gate_close_ready(event):
        missing.append("finish_gate_close_ready")
    return missing


def _finish_gate_event_ref(
    event: dict[str, Any],
    missing: list[str] | None = None,
) -> dict[str, Any]:
    ref = {
        "id": event.get("id") or event.get("event_id"),
        "event_kind": event.get("event_kind"),
        "event_type": event.get("event_type"),
        "phase": event.get("phase"),
        "status": event.get("status") or event.get("decision"),
    }
    if missing:
        ref["missing_fields"] = list(missing)
    return {key: value for key, value in ref.items() if value not in (None, "", [])}


def _finish_gate_projection_event(
    source: dict[str, Any],
    *,
    event_kind: str,
) -> dict[str, Any]:
    finish_gate = _mf_subagent_finish_gate_projection(source)
    identity = _route_identity(source)
    lineage = _route_attempt_lineage(source)
    changed_files = _finish_gate_changed_files(source)
    commit = _finish_gate_commit(source)
    observer_command_id = _finish_gate_observer_command_id(source)
    graph_trace_ids = sorted(_event_graph_trace_ids(source))
    source_event_id = source.get("id") or source.get("event_id")
    payload = {
        "schema_version": MF_SUBAGENT_FINISH_GATE_CLOSE_PROJECTION_SCHEMA_VERSION,
        "source_event_kind": source.get("event_kind"),
        "source_event_id": source_event_id,
        "projected_from": "mf_subagent_finish_gate",
        "observer_command_id": observer_command_id,
        "commit_sha": commit,
        "changed_files": changed_files,
        "worker_role": _first_deep_text(source, "worker_role") or "mf_sub",
        **identity,
        **lineage,
    }
    if graph_trace_ids:
        payload["graph_trace_ids"] = graph_trace_ids
        payload["query_source"] = _first_deep_text(source, "query_source") or "mf_subagent"
    if finish_gate:
        payload["mf_subagent_finish_gate"] = finish_gate
    return {
        "id": (
            f"{source_event_id}:finish_projection:{event_kind}"
            if source_event_id not in ("", None)
            else f"finish_projection:{event_kind}"
        ),
        "event_type": f"mf.finish_gate_projection.{event_kind}",
        "event_kind": event_kind,
        "phase": "close" if event_kind == "close_ready" else event_kind,
        "status": "accepted",
        "actor": _first_deep_text(source, "worker_slot_id")
        or _first_deep_text(source, "worker_id")
        or source.get("actor")
        or "mf_subagent_finish_gate",
        "backlog_id": source.get("backlog_id") or lineage.get("parent_task_id", ""),
        "project_id": source.get("project_id") or _first_deep_text(source, "project_id"),
        "task_id": source.get("task_id") or lineage.get("task_id", ""),
        "commit_sha": commit,
        "trace_id": source.get("trace_id") or "",
        "payload": payload,
        "verification": {
            "projection_source": "mf_subagent_finish_gate",
            "review_ready": True,
            "close_ready": _finish_gate_close_ready(source),
        },
    }


def mf_subagent_finish_gate_close_projection(
    events: list[dict[str, Any]] | None,
    *,
    needed_event_kinds: set[str] | None = None,
    route_context_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project worker-authored finish-gate evidence into close-counting facts."""

    needed = {
        str(item)
        for item in (needed_event_kinds or set())
        if str(item) in {"implementation", "close_ready"}
    }
    candidates = [
        _mapping(event)
        for event in (events or [])
        if _mapping(event) and _is_mf_subagent_finish_gate_event(_mapping(event))
    ]
    required = bool(candidates and needed)
    evaluated: list[dict[str, Any]] = []
    for event in candidates:
        missing = _finish_gate_missing_fields(
            event,
            route_context_gate=route_context_gate,
        )
        evaluated.append({
            "event": _finish_gate_event_ref(event, missing),
            "missing_fields": missing,
            "source": event,
        })

    accepted = next((item for item in evaluated if not item["missing_fields"]), None)
    if accepted is None and evaluated:
        accepted = min(evaluated, key=lambda item: len(item["missing_fields"]))

    passed = bool((not required) or (accepted and not accepted["missing_fields"]))
    projected_event_kinds = sorted(needed) if required and passed else []
    projected_events: list[dict[str, Any]] = []
    if required and passed:
        source = _mapping(accepted.get("source")) if accepted else candidates[0]
        projected_events = [
            _finish_gate_projection_event(source, event_kind=kind)
            for kind in projected_event_kinds
        ]

    missing_fields = [] if passed else (
        list(accepted.get("missing_fields") or []) if accepted else ["mf_subagent_finish_gate"]
    )
    return {
        "schema_version": MF_SUBAGENT_FINISH_GATE_CLOSE_PROJECTION_SCHEMA_VERSION,
        "required": required,
        "passed": passed,
        "status": (
            "passed"
            if passed and required
            else ("not_applicable" if not required else "failed")
        ),
        "needed_event_kinds": sorted(needed),
        "projected_event_kinds": projected_event_kinds,
        "missing_requirement_ids": missing_fields,
        "missing_fields": missing_fields,
        "evidence_events": [
            item["event"]
            for item in evaluated
            if item.get("event")
        ],
        "projected_events": projected_events,
        "checks": {
            "finish_gate_event_present": bool(candidates),
            "finish_gate_projection_needed": bool(required),
            "has_projected_implementation": "implementation" in projected_event_kinds,
            "has_projected_close_ready": "close_ready" in projected_event_kinds,
        },
    }


def _route_owned_event_ref(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": event.get("id") or event.get("event_id"),
        "event_type": event.get("event_type"),
        "event_kind": event.get("event_kind"),
        "phase": event.get("phase"),
        "status": event.get("status") or event.get("decision"),
        "correlation_id": event.get("correlation_id"),
    }


def _route_owned_event_matches_identity(
    event: dict[str, Any],
    route_identity: Mapping[str, Any],
) -> bool:
    if not route_identity:
        return True
    identity = _route_identity(event)
    if not identity:
        return False
    expected = {
        field: str(route_identity.get(field) or "").strip()
        for field in (*MF_ROUTE_IDENTITY_FIELDS, *MF_ROUTE_OPTIONAL_IDENTITY_FIELDS)
        if str(route_identity.get(field) or "").strip()
    }
    return _route_identity_matches_filter(identity, expected)


def _is_route_owned_source_event(event: dict[str, Any]) -> bool:
    event_type = str(event.get("event_type") or "").strip().lower()
    event_kind = _route_marker(event.get("event_kind"))
    return event_type.startswith("route.") or event_kind in {
        "route_source_event",
        "route_action_source_event",
        "route_context_source_event",
    }


def _is_route_service_completion_event(event: dict[str, Any]) -> bool:
    event_type = str(event.get("event_type") or "").strip().lower()
    event_kind = _route_marker(event.get("event_kind"))
    if event_type.startswith("service.route."):
        return True
    return event_kind == "service_route"


def _route_source_event_accepted(event: dict[str, Any]) -> bool:
    status = str(event.get("status") or event.get("decision") or "").strip().lower()
    if status in MF_ROUTE_SOURCE_PASS_STATUSES:
        return True
    payload = _mapping(event.get("payload"))
    decision = str(
        payload.get("decision")
        or payload.get("status")
        or _mapping(payload.get("route_evidence")).get("decision")
        or ""
    ).strip().lower()
    return decision in MF_ROUTE_SOURCE_PASS_STATUSES or decision == "allow"


def route_owned_source_event_gate_verification(
    events: list[dict[str, Any]] | None,
    *,
    route_identity: Mapping[str, Any] | None = None,
    protected_lane: str = "",
) -> dict[str, Any]:
    """Verify an accepted route-owned source event exists for a route identity."""

    rows = [_mapping(event) for event in (events or []) if _mapping(event)]
    identity_filter = _mapping(route_identity)
    source_events: dict[str, dict[str, Any]] = {}
    source_refs: set[str] = set()
    accepted_direct: list[dict[str, Any]] = []
    accepted_lineage: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []

    for event in rows:
        if not _is_route_owned_source_event(event):
            continue
        if not _route_owned_event_matches_identity(event, identity_filter):
            ignored.append({
                **_route_owned_event_ref(event),
                "reason": "route_identity_mismatch",
            })
            continue
        ref = str(event.get("id") or event.get("event_id") or "").strip()
        correlation_id = str(event.get("correlation_id") or "").strip()
        source_event_id = str(_first_deep_text(event, "source_event_id") or "").strip()
        for key in (ref, correlation_id, source_event_id):
            if key:
                source_events[key] = event
                source_refs.add(key)
        if _route_source_event_accepted(event):
            accepted_direct.append(_route_owned_event_ref(event))

    for event in rows:
        if not _is_route_service_completion_event(event):
            continue
        if not _route_owned_event_matches_identity(event, identity_filter):
            ignored.append({
                **_route_owned_event_ref(event),
                "reason": "route_identity_mismatch",
            })
            continue
        parent_ref = str(event.get("parent_event_id") or "").strip()
        correlation_id = str(event.get("correlation_id") or "").strip()
        source_event_id = str(_first_deep_text(event, "source_event_id") or "").strip()
        lineage_refs = [ref for ref in (parent_ref, correlation_id, source_event_id) if ref]
        source_event = next(
            (source_events.get(ref) for ref in lineage_refs if source_events.get(ref)),
            None,
        )
        if not source_event:
            ignored.append({
                **_route_owned_event_ref(event),
                "reason": "missing_route_source_parent",
            })
            continue
        if not _route_source_event_accepted(event):
            ignored.append({
                **_route_owned_event_ref(event),
                "reason": "non_passing_route_service_result",
            })
            continue
        accepted_lineage.append({
            "source_event": _route_owned_event_ref(source_event),
            "service_event": _route_owned_event_ref(event),
        })

    passed = bool(accepted_direct or accepted_lineage)
    route_identity_summary = {
        field: str(identity_filter.get(field) or "").strip()
        for field in (
            "route_id",
            "route_context_hash",
            "prompt_contract_id",
            "prompt_contract_hash",
            "route_token_ref",
            "visible_injection_manifest_hash",
        )
        if str(identity_filter.get(field) or "").strip()
    }
    return {
        "schema_version": MF_ROUTE_OWNED_SOURCE_EVENT_GATE_SCHEMA_VERSION,
        "passed": passed,
        "status": "passed" if passed else "failed",
        "protected_lane": str(protected_lane or ""),
        "route_identity": route_identity_summary,
        "source_event_refs": sorted(source_refs),
        "accepted_direct_source_events": accepted_direct,
        "accepted_source_lineage": accepted_lineage,
        "ignored_source_events": ignored,
        "next_legal_action": (
            "append_the_protected_timeline_evidence_with_this_route_owned_source_event_gate"
            if passed
            else "record_or_reuse_an_accepted_route_owned_source_event_for_the_claimed_route_identity"
        ),
    }


def mf_route_context_gate_verification(
    events: list[dict[str, Any]] | None,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify route context was consumed by route, dispatch, and startup gates."""

    rows = events if isinstance(events, list) else []
    topology_policy = _route_topology_policy(contract)
    route_context_required = _route_context_required(topology_policy)
    independent_verification_required = _route_independent_verification_required(
        topology_policy
    )
    architecture_review_required = _route_architecture_review_required(
        topology_policy,
        contract,
    )
    required = (
        route_context_required
        or independent_verification_required
        or architecture_review_required
    )
    required_requirement_ids = list(MF_ROUTE_CONTEXT_REQUIRED_EVIDENCE_IDS)
    if independent_verification_required:
        required_requirement_ids.append(MF_ROUTE_CONTEXT_INDEPENDENT_VERIFICATION_ID)
    if architecture_review_required:
        required_requirement_ids.append(MF_ROUTE_CONTEXT_ARCHITECTURE_REVIEW_ID)
    present: dict[str, list[dict[str, Any]]] = {
        req_id: [] for req_id in required_requirement_ids
    }
    identities: dict[str, list[dict[str, str]]] = {
        req_id: [] for req_id in required_requirement_ids
    }
    ignored: list[dict[str, Any]] = []
    cleanup_event: dict[str, Any] = {}
    cleanup_identity: dict[str, str] = {}
    accepted_dispatch_lineages: list[dict[str, Any]] = []
    accepted_startup_lineages: list[dict[str, Any]] = []
    accepted_action_scope_lineages: list[dict[str, Any]] = []
    attempt_lineage_candidates: list[dict[str, Any]] = []
    parent_route_identity_hint: dict[str, str] = {}

    for raw_event in rows:
        event = _mapping(raw_event)
        if not event or not _route_event_is_identity_cleanup(event):
            continue
        identity = _route_identity(event)
        if identity and _route_event_passed(event):
            cleanup_identity = identity
            cleanup_event = {
                "id": event.get("id") or event.get("event_id"),
                "event_kind": event.get("event_kind"),
                "phase": event.get("phase"),
                "status": event.get("status") or event.get("decision"),
            }
        else:
            ignored.append({
                "id": event.get("id") or event.get("event_id"),
                "event_kind": event.get("event_kind"),
                "status": event.get("status") or event.get("decision"),
                "reason": "invalid_route_identity_cleanup",
                "categories": ["route_identity_cleanup"],
            })
    parent_route_identity_hint = dict(cleanup_identity)
    if not parent_route_identity_hint:
        for raw_event in rows:
            event = _mapping(raw_event)
            if not event or parent_route_identity_hint:
                continue
            categories = _route_event_categories(event)
            if "route_context" not in categories:
                continue
            identity = _route_identity(event)
            if identity and _route_event_passed(event):
                parent_route_identity_hint = identity
    superseded_event_count = 0

    for index, raw_event in enumerate(rows):
        event = _mapping(raw_event)
        if not event:
            continue
        if _route_event_is_identity_cleanup(event):
            continue
        identity = _route_identity(event)
        categories = _route_event_categories(event)
        if not categories:
            continue
        if not identity:
            ignored.append({
                "id": event.get("id") or event.get("event_id"),
                "event_kind": event.get("event_kind"),
                "status": event.get("status") or event.get("decision"),
                "reason": "missing_route_identity",
                "categories": sorted(categories),
            })
            continue
        normalized_identity = identity
        if "bounded_implementation_worker_dispatch" in categories:
            normalized_identity, lineage = _route_parent_child_dispatch_identity(
                event,
                normalized_identity,
                parent_identity_hint=parent_route_identity_hint,
            )
            if lineage:
                accepted_dispatch_lineages.append({
                    **lineage,
                    "event": {
                        "id": event.get("id") or event.get("event_id"),
                        "event_kind": event.get("event_kind"),
                        "phase": event.get("phase"),
                        "status": event.get("status") or event.get("decision"),
                    },
                })
        if "mf_subagent_startup" in categories:
            normalized_identity, lineage = _route_parent_child_startup_identity(
                event,
                identity,
                parent_identity_hint=parent_route_identity_hint,
            )
            if lineage:
                accepted_startup_lineages.append({
                    **lineage,
                    "event": {
                        "id": event.get("id") or event.get("event_id"),
                        "event_kind": event.get("event_kind"),
                        "phase": event.get("phase"),
                        "status": event.get("status") or event.get("decision"),
                    },
                })
        if MF_ROUTE_CONTEXT_INDEPENDENT_VERIFICATION_ID in categories:
            normalized_identity, lineage = _route_action_scoped_verification_identity(
                event,
                normalized_identity,
                parent_identity_hint=parent_route_identity_hint,
            )
            if lineage:
                accepted_action_scope_lineages.append({
                    **lineage,
                    "event": {
                        "id": event.get("id") or event.get("event_id"),
                        "event_kind": event.get("event_kind"),
                        "phase": event.get("phase"),
                        "status": event.get("status") or event.get("decision"),
                    },
                })
        if cleanup_identity and not _route_identity_matches_filter(
            normalized_identity,
            cleanup_identity,
        ):
            superseded_event_count += 1
            ignored.append({
                "id": event.get("id") or event.get("event_id"),
                "event_kind": event.get("event_kind"),
                "status": event.get("status") or event.get("decision"),
                "reason": "superseded_route_identity",
                "categories": sorted(categories),
            })
            continue
        if "route_context" in categories and not _route_visible_manifest_present(event):
            ignored.append({
                "id": event.get("id") or event.get("event_id"),
                "event_kind": event.get("event_kind"),
                "status": event.get("status") or event.get("decision"),
                "reason": "missing_visible_injection_manifest",
                "categories": sorted(categories),
            })
            continue
        if not _route_event_passed(event):
            ignored.append({
                "id": event.get("id") or event.get("event_id"),
                "event_kind": event.get("event_kind"),
                "status": event.get("status") or event.get("decision"),
                "reason": "non_passing_route_evidence",
                "categories": sorted(categories),
            })
            continue
        if (
            "mf_subagent_startup" in categories
            and not _route_actual_startup_identity_present(event)
        ):
            ignored.append({
                "id": event.get("id") or event.get("event_id"),
                "event_kind": event.get("event_kind"),
                "status": event.get("status") or event.get("decision"),
                "reason": "missing_actual_startup_identity",
                "categories": ["mf_subagent_startup"],
            })
            categories = set(categories)
            categories.discard("mf_subagent_startup")
            if not categories:
                continue
        if (
            "mf_subagent_startup" in categories
            and _route_event_is_startup_adoption(event)
            and not _route_startup_adoption_evidence_valid(event)
        ):
            ignored.append({
                "id": event.get("id") or event.get("event_id"),
                "event_kind": event.get("event_kind"),
                "status": event.get("status") or event.get("decision"),
                "reason": "invalid_branch_adoption_evidence",
                "categories": ["mf_subagent_startup"],
            })
            categories = set(categories)
            categories.discard("mf_subagent_startup")
            if not categories:
                continue
        event_ref = {
            "id": event.get("id") or event.get("event_id"),
            "event_kind": event.get("event_kind"),
            "phase": event.get("phase"),
            "status": event.get("status") or event.get("decision"),
        }
        attempt_lineage = _route_attempt_lineage(event)
        if attempt_lineage and categories.intersection(MF_ROUTE_WORKER_REQUIREMENTS):
            attempt_lineage_candidates.append({
                "_order": _event_numeric_id(event) or index + 1,
                "event": event_ref,
                "categories": sorted(categories),
                "lineage": attempt_lineage,
            })
        for category in categories:
            if category in present:
                present[category].append(event_ref)
                identities[category].append(normalized_identity)

    missing = [req_id for req_id in required_requirement_ids if required and not present[req_id]]
    identity_keys = {
        _route_identity_key(identity)
        for category_id in required_requirement_ids
        for identity in identities[category_id]
        if identity
    }
    prompt_hashes = {
        identity.get("prompt_contract_hash", "")
        for category_id in required_requirement_ids
        for identity in identities[category_id]
        if identity.get("prompt_contract_hash")
    }
    same_route_identity = len(identity_keys) <= 1
    same_optional_prompt_contract_hash = len(prompt_hashes) <= 1
    if required and identity_keys and not (same_route_identity and same_optional_prompt_contract_hash):
        missing.append("route_identity_mismatch")
    passed = (not required) or (
        not missing and same_route_identity and same_optional_prompt_contract_hash
    )
    route_identity: dict[str, str] = {}
    if len(identity_keys) == 1:
        identity_key = next(iter(identity_keys))
        route_identity = {
            field: identity_key[idx]
            for idx, field in enumerate(MF_ROUTE_IDENTITY_FIELDS)
        }
        if len(prompt_hashes) == 1:
            route_identity["prompt_contract_hash"] = next(iter(prompt_hashes))
    current_attempt_lineage: dict[str, Any] = {}
    if attempt_lineage_candidates:
        selected_attempt = max(
            attempt_lineage_candidates,
            key=lambda item: int(item.get("_order") or 0),
        )
        current_attempt_lineage = {
            key: value for key, value in selected_attempt.items() if key != "_order"
        }
    public_attempt_lineage_candidates = [
        {key: value for key, value in candidate.items() if key != "_order"}
        for candidate in attempt_lineage_candidates
    ]
    return {
        "schema_version": MF_ROUTE_CONTEXT_GATE_SCHEMA_VERSION,
        "passed": passed,
        "status": "passed" if passed else "failed",
        "required": required,
        "required_requirement_ids": required_requirement_ids if required else [],
        "present_requirement_ids": [req_id for req_id in required_requirement_ids if present[req_id]],
        "missing_requirement_ids": missing,
        "topology_policy": topology_policy,
        "route_identity": route_identity,
        "same_route_identity": same_route_identity,
        "route_identity_cleanup": {
            "applied": bool(cleanup_identity),
            "event": cleanup_event,
            "route_identity": cleanup_identity,
            "superseded_event_count": superseded_event_count,
        },
        "attempt_lineage": current_attempt_lineage,
        "attempt_lineage_candidates": public_attempt_lineage_candidates,
        "accepted_dispatch_lineages": accepted_dispatch_lineages,
        "accepted_startup_lineages": accepted_startup_lineages,
        "accepted_action_scope_lineages": accepted_action_scope_lineages,
        "runtime_context_projection_evidence_fields": {
            "schema_version": "runtime_context.timeline_evidence_fields.v1",
            "startup": list(RUNTIME_CONTEXT_TIMELINE_STARTUP_FIELDS),
            "read_receipt": list(RUNTIME_CONTEXT_TIMELINE_READ_RECEIPT_FIELDS),
            "attempt_lineage": list(RUNTIME_CONTEXT_TIMELINE_IDENTITY_FIELDS),
            "route_identity": list(
                (*MF_ROUTE_IDENTITY_FIELDS, *MF_ROUTE_OPTIONAL_IDENTITY_FIELDS)
            ),
            "required_evidence_ids": list(MF_ROUTE_CONTEXT_REQUIRED_EVIDENCE_IDS),
        },
        "evidence_events": {
            req_id: present[req_id] for req_id in required_requirement_ids
        },
        "ignored_route_events": ignored,
        "checks": {
            "route_context_present": bool(present["route_context"]),
            "route_action_precheck_present": bool(present["route_action_precheck"]),
            "bounded_implementation_worker_dispatch_present": bool(
                present["bounded_implementation_worker_dispatch"]
            ),
            "mf_subagent_startup_present": bool(present["mf_subagent_startup"]),
            "independent_verification_required": independent_verification_required,
            "independent_verification_lane_present": bool(
                present.get(MF_ROUTE_CONTEXT_INDEPENDENT_VERIFICATION_ID)
            ),
            "architecture_review_required": architecture_review_required,
            "architecture_review_lane_present": bool(
                present.get(MF_ROUTE_CONTEXT_ARCHITECTURE_REVIEW_ID)
            ),
            "same_route_identity": same_route_identity,
            "same_optional_prompt_contract_hash": same_optional_prompt_contract_hash,
            "route_identity_cleanup_applied": bool(cleanup_identity),
        },
    }


def _ordered_subset(values: list[str], allowed: tuple[str, ...] | set[str]) -> list[str]:
    allowed_set = set(allowed)
    return [value for value in values if value in allowed_set]


def mf_close_missing_evidence_groups(
    missing_event_kinds: list[str] | None,
    route_context_gate: dict[str, Any] | None,
) -> dict[str, Any]:
    """Group close-gate gaps into the operator action buckets shown in reminders."""

    close_missing = list(missing_event_kinds or [])
    route_gate = _mapping(route_context_gate)
    route_missing = [
        str(item)
        for item in _list(route_gate.get("missing_requirement_ids"))
        if str(item)
    ]
    identity_missing = _ordered_subset(route_missing, MF_ROUTE_IDENTITY_REQUIREMENTS)
    if (
        route_gate.get("same_route_identity") is False
        or _mapping(route_gate.get("checks")).get("same_route_identity") is False
    ) and "route_identity_mismatch" not in identity_missing:
        identity_missing.append("route_identity_mismatch")

    known_route = {
        *MF_ROUTE_SERVICE_REQUIREMENTS,
        *MF_ROUTE_WORKER_REQUIREMENTS,
        MF_ROUTE_CONTEXT_INDEPENDENT_VERIFICATION_ID,
        MF_ROUTE_CONTEXT_ARCHITECTURE_REVIEW_ID,
        *MF_ROUTE_IDENTITY_REQUIREMENTS,
    }
    other_route = [item for item in route_missing if item not in known_route]
    groups = {
        "timeline": {
            "label": "implementation / verification / close_ready",
            "missing": close_missing,
            "next_action": "append passing implementation, verification, then close_ready timeline evidence",
        },
        "route_service": {
            "label": "route service evidence",
            "missing": _ordered_subset(route_missing, MF_ROUTE_SERVICE_REQUIREMENTS),
            "next_actions": ["route.prompt_alert_bundle", "route.action_precheck"],
        },
        "bounded_worker": {
            "label": "bounded worker dispatch/startup",
            "missing": _ordered_subset(route_missing, MF_ROUTE_WORKER_REQUIREMENTS),
            "next_action": "dispatch and start a bounded mf_sub implementation worker",
        },
        "independent_verification": {
            "label": "independent verification lane",
            "missing": _ordered_subset(
                route_missing,
                {MF_ROUTE_CONTEXT_INDEPENDENT_VERIFICATION_ID},
            ),
            "next_action": "run independent QA verification before retrying close",
        },
        "architecture_review": {
            "label": "architecture review lane",
            "missing": _ordered_subset(
                route_missing,
                {MF_ROUTE_CONTEXT_ARCHITECTURE_REVIEW_ID},
            ),
            "next_action": "run architecture/data-continuity review before retrying close",
        },
        "route_identity": {
            "label": "route identity",
            "missing": identity_missing,
            "next_action": "supersede stale hand-written route evidence or start a fresh service-generated route attempt",
        },
    }
    if other_route:
        groups["other_route"] = {
            "label": "other route evidence",
            "missing": other_route,
            "next_action": "inspect route_context_gate.missing_requirement_ids",
        }
    return {
        "schema_version": MF_CLOSE_MISSING_GROUPS_SCHEMA_VERSION,
        "groups": groups,
    }


def mf_route_context_reminder(
    route_context_gate: dict[str, Any] | None,
    missing_evidence_groups: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the public-safe route workflow reminder consumed by MCP and dashboard."""

    route_gate = _mapping(route_context_gate)
    groups = _mapping(missing_evidence_groups).get("groups")
    topology_policy = _mapping(route_gate.get("topology_policy"))
    required = bool(route_gate.get("required"))
    passed = bool(route_gate.get("passed"))
    return {
        "schema_version": MF_ROUTE_CONTEXT_REMINDER_SCHEMA_VERSION,
        "required": required,
        "blocked": required and not passed,
        "status": str(route_gate.get("status") or ("passed" if passed else "blocked")),
        "contract_template_id": MF_ROUTE_GUIDANCE_TEMPLATE_ID,
        "allowed_stages": list(MF_ROUTE_GUIDANCE_ALLOWED_STAGES),
        "selected_topology": str(topology_policy.get("selected_topology") or ""),
        "recommended_topology": str(topology_policy.get("recommended_topology") or ""),
        "priority": str(topology_policy.get("priority") or ""),
        "next_actions": [
            {
                "id": "request_route_prompt_alert_bundle",
                "command": "route.prompt_alert_bundle",
                "detail": "request the service-generated route context bundle",
            },
            {
                "id": "run_route_action_precheck",
                "command": "route.action_precheck",
                "detail": "run the local action gate with an allowed stage before mutation",
            },
            {
                "id": "dispatch_bounded_worker",
                "command": "dispatch bounded implementation worker",
                "detail": "observer or judge coordination does not count as implementation worker evidence",
            },
            {
                "id": "start_worker",
                "command": "start worker",
                "detail": "record mf_subagent startup with matching route identity",
            },
            {
                "id": "run_independent_verification",
                "command": "run independent verification",
                "detail": "record QA verification separate from observer and implementation worker",
            },
            {
                "id": "run_architecture_review",
                "command": "run architecture review",
                "detail": "record architecture/data-continuity review only when the route or contract requires architecture_review_lane",
            },
            {
                "id": "retry_close",
                "command": "retry close",
                "detail": "retry only after implementation, verification, and close_ready evidence are present",
            },
        ],
        "missing_evidence_groups": groups if isinstance(groups, dict) else {},
        "identity_recovery": {
            "stale_or_mismatched_route_evidence": "supersede or start a fresh service-generated route attempt",
            "hand_written_route_text_counts_as_route_token": False,
        },
        "boundary": {
            "service_generated_route_identity_required": True,
            "supporting_context_not_route_token": [
                "private_route_provider_context",
                "hand_written_alert_text",
            ],
        },
    }


def _normalize_requirement(item: Any, *, default_required: bool = True) -> dict[str, Any] | None:
    if isinstance(item, str):
        req_id = item.strip()
        return {"id": req_id, "required": default_required} if req_id else None
    item = _mapping(item)
    req_id = str(item.get("id") or item.get("requirement_id") or "").strip()
    if not req_id:
        return None
    return {
        "id": req_id,
        "required": bool(item.get("required", default_required)),
        "phase": str(item.get("phase") or ""),
        "kind": str(item.get("kind") or item.get("type") or ""),
        "command": str(item.get("command") or ""),
        "label": str(item.get("label") or ""),
    }


def mf_contract_requirements(contract: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return evidence requirements from an instantiated MF contract."""

    root = _contract_root(contract)
    if not root:
        return []

    raw_requirements: list[Any] = []
    raw_requirements.extend(_list(root.get("evidence_requirements")))
    raw_requirements.extend(_list(root.get("required_evidence")))

    integration = _mapping(root.get("integration"))
    raw_requirements.extend(
        {**_mapping(item), "required": True}
        for item in _list(integration.get("required_evidence"))
    )
    raw_requirements.extend(
        {**_mapping(item), "required": False}
        for item in _list(integration.get("optional_evidence"))
    )

    e2e_contract = _mapping(root.get("e2e_contract"))
    if e2e_contract and bool(e2e_contract.get("required")):
        raw_requirements.append({
            "id": e2e_contract.get("requirement_id") or "e2e",
            "required": True,
            "phase": "integration",
            "kind": "e2e",
            "command": e2e_contract.get("command")
            or " && ".join(_string_list(e2e_contract.get("commands"))),
            "label": e2e_contract.get("label") or "E2E",
        })

    test_policy = _mapping(root.get("test_scenario_policy"))
    raw_requirements.extend(
        {
            "id": req_id,
            "required": True,
            "phase": "verification",
            "kind": "test_scenario_policy",
        }
        for req_id in _string_list(test_policy.get("required_evidence_ids"))
    )

    requirements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_requirements:
        normalized = _normalize_requirement(raw)
        if not normalized:
            continue
        if normalized["id"] in seen:
            for existing in requirements:
                if existing["id"] != normalized["id"]:
                    continue
                if normalized.get("required", True):
                    existing["required"] = True
                for key in ("phase", "kind", "command", "label"):
                    if not existing.get(key) and normalized.get(key):
                        existing[key] = normalized[key]
                break
            continue
        seen.add(normalized["id"])
        requirements.append(normalized)
    return requirements


def _requirement_ids_from_container(container: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in ("requirement_id", "contract_requirement_id"):
        value = str(container.get(key) or "").strip()
        if value:
            ids.add(value)
    for key in ("requirement_ids", "contract_requirement_ids"):
        ids.update(_string_list(container.get(key)))
    return ids


def _contract_evidence_ids(event: dict[str, Any]) -> set[str]:
    status = str(event.get("status") or "").strip().lower()
    event_passed = status in MF_CLOSE_PASS_STATUSES
    ids: set[str] = set()
    payload = _mapping(event.get("payload"))
    verification = _mapping(event.get("verification"))
    artifact_refs = _mapping(event.get("artifact_refs"))

    if event_passed:
        ids.update(_requirement_ids_from_container(payload))
        ids.update(_requirement_ids_from_container(verification))
        ids.update(_requirement_ids_from_container(artifact_refs))

    for container in (payload, verification, artifact_refs):
        for item in _list(container.get("contract_evidence")):
            evidence = _mapping(item)
            evidence_status = str(evidence.get("status") or status).strip().lower()
            if evidence_status not in MF_CLOSE_PASS_STATUSES:
                continue
            evidence_id = str(evidence.get("requirement_id") or evidence.get("id") or "").strip()
            if evidence_id:
                ids.add(evidence_id)
    return ids


def mf_contract_gate_verification(
    events: list[dict[str, Any]] | None,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate timeline evidence against an instantiated MF contract."""

    rows = events if isinstance(events, list) else []
    requirements = mf_contract_requirements(contract)
    required = [item for item in requirements if item.get("required", True)]
    required_ids = [item["id"] for item in required]
    all_requirement_ids = {item["id"] for item in requirements}
    present: set[str] = set()
    evidence_events: list[dict[str, Any]] = []
    for event in rows:
        event = _mapping(event)
        ids = _contract_evidence_ids(event)
        if not ids:
            continue
        present.update(ids)
        evidence_events.append({
            "id": event.get("id"),
            "event_kind": event.get("event_kind"),
            "phase": event.get("phase"),
            "status": event.get("status"),
            "requirement_ids": sorted(ids),
        })
    exception_ids, exception_event = _observer_direct_exception_contract_evidence(
        rows,
        contract,
        requirements,
    )
    if exception_ids:
        present.update(exception_ids)
        evidence_events.append({
            **exception_event,
            "requirement_ids": sorted(exception_ids),
            "source": "observer_direct_implementation_exception",
        })
    missing = [req_id for req_id in required_ids if req_id not in present]
    root = _contract_root(contract)
    return {
        "schema_version": MF_CONTRACT_SCHEMA_VERSION,
        "passed": not missing,
        "status": "passed" if not missing else "failed",
        "template_id": str(root.get("template_id") or ""),
        "contract_instance_id": str(root.get("contract_instance_id") or ""),
        "required_requirement_ids": required_ids,
        "optional_requirement_ids": [
            item["id"] for item in requirements if not item.get("required", True)
        ],
        "present_requirement_ids": sorted(req_id for req_id in present if req_id in all_requirement_ids),
        "missing_requirement_ids": missing,
        "evidence_events": evidence_events,
        "checks": {
            "has_contract": bool(root),
            "required_count": len(required_ids),
            "missing_count": len(missing),
        },
    }


def _contract_walk(value: Any, *, depth: int = 0, max_depth: int = 4):
    if depth > max_depth:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _contract_walk(child, depth=depth + 1, max_depth=max_depth)
    elif isinstance(value, list):
        for child in value:
            yield from _contract_walk(child, depth=depth + 1, max_depth=max_depth)


def _compact_contract_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return repr(value)


def _contract_signal(signals: list[dict[str, Any]], seen: set[tuple[str, str]], *, source: str, value: Any) -> None:
    compact = _compact_contract_value(value)
    key = (source, compact)
    if key in seen:
        return
    seen.add(key)
    signals.append({"source": source, "value": compact})


def _subagent_requirement_marker(value: Any) -> bool:
    text = _normalize_token(_compact_contract_value(value))
    if any(
        marker in text
        for marker in (
            "bounded_implementation_subagent",
            "bounded_subagent",
            "mf_sub",
            "mf_subagent",
        )
    ):
        return True
    return "subagent" in text and "implementation" in text


def _required_lane_requires_subagent(item: Any) -> bool:
    if isinstance(item, dict) and item.get("required") is False:
        return False
    if not _subagent_requirement_marker(item):
        return False
    item_map = _mapping(item)
    role = _normalize_token(
        item_map.get("role")
        or item_map.get("worker_role")
        or item_map.get("type")
        or item_map.get("kind")
    )
    return not role or role in {
        "implementation",
        "implementation_worker",
        "worker",
        "mf_sub",
        "subagent",
        "bounded_implementation_subagent",
    }


def _required_evidence_requires_subagent(item: Any) -> bool:
    item_map = _mapping(item)
    if item_map and item_map.get("required") is False:
        return False
    text = _normalize_token(_compact_contract_value(item))
    if any(
        marker in text
        for marker in (
            "bounded_implementation_subagent",
            "bounded_subagent_dispatch",
            "mf_subagent_dispatch",
            "mf_subagent_handoff",
            "mf_sub",
        )
    ):
        return True
    return "subagent_id" in text and "implementation" in text


def _lane_contract_requirement_id(value: Any) -> bool:
    text = _normalize_token(_compact_contract_value(value))
    return any(
        marker in text
        for marker in (
            "bounded_implementation_subagent",
            "bounded_subagent",
            "mf_subagent",
            "mf_sub",
            "subagent_dispatch",
            "subagent_handoff",
            "subagent_review_ready",
            "subagent_id",
            "implementation_worker",
            "worker_or_subagent_evidence",
        )
    )


def _lane_related_contract_requirement_ids(requirements: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for requirement in requirements:
        req_id = str(requirement.get("id") or "").strip()
        if not req_id:
            continue
        if (
            _lane_contract_requirement_id(req_id)
            or _required_evidence_requires_subagent(requirement)
        ):
            ids.add(req_id)
    return ids


def _blocked_action_requires_subagent(action: Any) -> bool:
    text = _normalize_token(action)
    return any(
        marker in text
        for marker in (
            "edit_files_as_observer_or_independent_reviewer",
            "close_without_worker_or_subagent_evidence",
            "observer_direct_implementation",
            "observer_direct_file_edit",
            "edit_files_as_observer",
        )
    )


def _policy_text_requires_subagent(value: Any) -> bool:
    text = str(value or "").strip().lower().replace("-", "_")
    if not text:
        return False
    has_subagent = (
        ("bounded" in text and "subagent" in text)
        or "bounded_implementation_subagent" in text
        or "mf_sub" in text
    )
    has_requirement_language = any(
        marker in text
        for marker in (
            "must",
            "required",
            "requires",
            "block",
            "blocked",
            "not directly",
            "not_directly",
        )
    )
    return has_subagent and has_requirement_language


def _mf_lane_ownership_requirements(contract: dict[str, Any] | None) -> dict[str, Any]:
    root = _contract_root(contract)
    data = _mapping(contract)
    if not root and not data:
        return {
            "subagent_required": False,
            "required_lane_ownership_ids": [],
            "requirement_signals": [],
        }

    signals: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for obj in _contract_walk(data or root):
        for item in _list(obj.get("required_lanes")):
            if _required_lane_requires_subagent(item):
                _contract_signal(signals, seen, source="required_lanes", value=item)
        for key in ("required_evidence", "evidence_requirements", "required_evidence_ids"):
            for item in _list(obj.get(key)):
                if _required_evidence_requires_subagent(item):
                    _contract_signal(signals, seen, source=key, value=item)
        for item in _string_list(obj.get("blocked_actions")):
            if _blocked_action_requires_subagent(item):
                _contract_signal(signals, seen, source="blocked_actions", value=item)
        for key in (
            "implementation_policy",
            "execution_policy",
            "lane_policy",
            "observer_policy",
        ):
            if _policy_text_requires_subagent(obj.get(key)):
                _contract_signal(signals, seen, source=key, value=obj.get(key))

    for requirement in mf_contract_requirements(contract):
        if requirement.get("required", True) and _required_evidence_requires_subagent(requirement):
            _contract_signal(signals, seen, source="contract_requirement", value=requirement)

    required = bool(signals)
    return {
        "subagent_required": required,
        "required_lane_ownership_ids": [MF_BOUNDED_SUBAGENT_LANE_ID] if required else [],
        "requirement_signals": signals,
    }


def _contract_route_identity(contract: dict[str, Any] | None) -> dict[str, list[str]]:
    route_ids: set[str] = set()
    route_hashes: set[str] = set()
    for obj in _contract_walk(_mapping(contract)):
        for key in ("route_id", "execution_route_id", "audit_route_id"):
            value = str(obj.get(key) or "").strip()
            if value:
                route_ids.add(value)
        for key in (
            "route_context_hash",
            "execution_route_context_hash",
            "audit_route_context_hash",
        ):
            value = str(obj.get(key) or "").strip()
            if value:
                route_hashes.add(value)
    return {
        "route_ids": sorted(route_ids),
        "route_context_hashes": sorted(route_hashes),
    }


def _event_passed(event: dict[str, Any]) -> bool:
    status = str(event.get("status") or "").strip().lower()
    return status in MF_CLOSE_PASS_STATUSES


def _field_values(value: Any, keys: set[str], *, depth: int = 0, max_depth: int = 4) -> list[Any]:
    if depth > max_depth:
        return []
    values: list[Any] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) in keys:
                values.append(child)
            values.extend(_field_values(child, keys, depth=depth + 1, max_depth=max_depth))
    elif isinstance(value, list):
        for child in value:
            values.extend(_field_values(child, keys, depth=depth + 1, max_depth=max_depth))
    return values


def _event_field_values(event: dict[str, Any], keys: set[str]) -> list[Any]:
    values: list[Any] = []
    for container in (
        _mapping(event),
        _mapping(event.get("payload")),
        _mapping(event.get("verification")),
        _mapping(event.get("artifact_refs")),
    ):
        values.extend(_field_values(container, keys))
    return values


def _first_event_string(event: dict[str, Any], keys: set[str]) -> str:
    for value in _event_field_values(event, keys):
        if isinstance(value, (dict, list)):
            continue
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _event_key_text(event: dict[str, Any]) -> str:
    keys: list[str] = []
    for container in (
        _mapping(event.get("payload")),
        _mapping(event.get("verification")),
        _mapping(event.get("artifact_refs")),
    ):
        keys.extend(str(key) for key in container.keys())
    return _normalize_token(" ".join(keys))


def _event_lane_text(event: dict[str, Any]) -> str:
    fields = [
        event.get("event_type"),
        event.get("phase"),
        event.get("event_kind"),
        event.get("actor"),
        event.get("decision"),
        event.get("task_id"),
        event.get("trace_id"),
    ]
    for key in (
        "worker_role",
        "role",
        "required_dispatch_key",
        "bounded_implementation_subagent_id",
        "subagent_id",
        "agent_id",
        "worker_id",
        "lane",
        "stop_state",
        "worker_status",
    ):
        fields.extend(
            str(value)
            for value in _event_field_values(event, {key})
            if not isinstance(value, (dict, list))
        )
    return _normalize_token(" ".join(str(field or "") for field in fields))


def _event_stage_text(event: dict[str, Any]) -> str:
    return _normalize_token(
        " ".join(
            str(event.get(key) or "")
            for key in ("event_type", "phase", "event_kind", "decision")
        )
    )


def _event_has_subagent_identity(event: dict[str, Any]) -> bool:
    text = _event_lane_text(event)
    if any(
        marker in text
        for marker in (
            "bounded_subagent",
            "bounded_implementation_subagent",
            "mf_sub",
            "mf_subagent",
            "subagent",
        )
    ):
        return True
    for value in _event_field_values(event, {"worker_role", "role"}):
        role = _normalize_token(value)
        if role in {"mf_sub", "implementation_worker", "subagent"}:
            return True
    for key in (
        "bounded_implementation_subagent_id",
        "subagent_id",
        "agent_id",
        "worker_id",
    ):
        if _first_event_string(event, {key}):
            return True
    return False


def _is_subagent_dispatch_event(event: dict[str, Any]) -> bool:
    if not _event_passed(event):
        return False
    text = _event_lane_text(event)
    if any(
        _normalize_token(value) == "bounded_subagent_dispatch"
        for value in _event_field_values(event, {"required_dispatch_key"})
    ):
        return True
    if any(
        marker in text
        for marker in (
            "mf_subagent.dispatch",
            "mf_subagent_dispatch",
            "bounded_subagent_dispatch",
            "observer_to_subagent_dispatch",
        )
    ):
        return True
    return "dispatch" in text and _event_has_subagent_identity(event)


def _is_subagent_review_ready_event(event: dict[str, Any]) -> bool:
    if not _event_passed(event) or not _event_has_subagent_identity(event):
        return False
    if _is_subagent_dispatch_event(event):
        return False
    text = _event_stage_text(event)
    if any(marker in text for marker in ("handoff", "review_ready", "waiting_merge")):
        return True
    if any(_truthy(value) for value in _event_field_values(event, {"review_ready"})):
        return True
    for value in _event_field_values(event, {"stop_state", "worker_status", "state"}):
        if _normalize_token(value) in {"review_ready", "waiting_merge"} and any(
            marker in text for marker in ("handoff", "review", "waiting_merge")
        ):
            return True
    return False


def _lane_event_summary(event: dict[str, Any], evidence_id: str) -> dict[str, Any]:
    return {
        "id": event.get("id"),
        "event_type": event.get("event_type"),
        "event_kind": event.get("event_kind"),
        "phase": event.get("phase"),
        "actor": event.get("actor"),
        "status": event.get("status"),
        "task_id": event.get("task_id"),
        "trace_id": event.get("trace_id"),
        "evidence_id": evidence_id,
    }


def _has_dirty_scope_evidence(event: dict[str, Any]) -> bool:
    for value in _event_field_values(event, {"dirty_scope", "dirty_scope_check"}):
        if value not in (None, "", {}, []):
            return True
    return False


def _has_operator_approval(event: dict[str, Any]) -> bool:
    if _first_event_string(event, {"approved_by"}):
        return True
    if any(_truthy(value) for value in _event_field_values(event, {"operator_approved"})):
        return True
    for value in _event_field_values(event, {"operator_approval", "approval"}):
        if isinstance(value, dict):
            if str(value.get("approved_by") or "").strip():
                return True
            if _truthy(value.get("operator_approved") or value.get("approved")):
                return True
        elif _truthy(value):
            return True
    return False


def _observer_direct_exception_event(
    event: dict[str, Any],
    route_identity: dict[str, list[str]],
) -> dict[str, Any]:
    event_name = _normalize_token(
        " ".join(
            str(event.get(key) or "")
            for key in ("event_type", "event_kind", "phase", "decision")
        )
    )
    event_name = f"{event_name} {_event_key_text(event)}"
    if "observer_direct" not in event_name or not any(
        marker in event_name for marker in ("exception", "waiver")
    ):
        return {"accepted": False, "missing_fields": ["observer_direct_exception_event"]}

    if not _event_passed(event):
        return {"accepted": False, "missing_fields": ["passing_status"]}

    route_ids = [
        str(value).strip()
        for value in _event_field_values(event, {"route_id", "execution_route_id"})
        if str(value or "").strip()
    ]
    route_hashes = [
        str(value).strip()
        for value in _event_field_values(event, {"route_context_hash", "execution_route_context_hash"})
        if str(value or "").strip()
    ]
    expected_route_ids = set(route_identity.get("route_ids") or [])
    expected_route_hashes = set(route_identity.get("route_context_hashes") or [])
    has_route = bool(route_ids or route_hashes)
    route_matches = True
    if has_route and (expected_route_ids or expected_route_hashes):
        route_matches = bool(
            expected_route_ids.intersection(route_ids)
            or expected_route_hashes.intersection(route_hashes)
        )
    reason = _first_event_string(event, {"reason", "exception_reason", "waiver_reason"})
    has_dirty_scope = _has_dirty_scope_evidence(event)
    has_operator_approval = _has_operator_approval(event)

    missing_fields: list[str] = []
    if not has_route:
        missing_fields.append("route_id_or_route_context_hash")
    elif not route_matches:
        missing_fields.append("matching_route_id_or_route_context_hash")
    if not reason:
        missing_fields.append("reason")
    if not has_dirty_scope:
        missing_fields.append("dirty_scope_or_dirty_scope_check")
    if not has_operator_approval:
        missing_fields.append("operator_approval")

    accepted = not missing_fields
    return {
        "accepted": accepted,
        "missing_fields": missing_fields,
        "event": _lane_event_summary(event, "observer_direct_implementation_exception"),
        "accepted_fields": [
            field
            for field, present in (
                ("route_id_or_route_context_hash", has_route and route_matches),
                ("reason", bool(reason)),
                ("dirty_scope_or_dirty_scope_check", has_dirty_scope),
                ("operator_approval", has_operator_approval),
            )
            if present
        ],
    }


def _observer_direct_exception_contract_evidence(
    events: list[dict[str, Any]],
    contract: dict[str, Any] | None,
    requirements: list[dict[str, Any]],
) -> tuple[set[str], dict[str, Any]]:
    lane_requirement_ids = _lane_related_contract_requirement_ids(requirements)
    if not lane_requirement_ids:
        return set(), {}

    route_identity = _contract_route_identity(contract)
    for raw in events:
        event = _mapping(raw)
        if not event:
            continue
        exception = _observer_direct_exception_event(event, route_identity)
        if not exception.get("accepted"):
            continue
        return lane_requirement_ids, {
            "id": event.get("id"),
            "event_kind": event.get("event_kind"),
            "phase": event.get("phase"),
            "status": event.get("status"),
        }
    return set(), {}


def mf_lane_ownership_gate_verification(
    events: list[dict[str, Any]] | None,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate MF lane ownership when a route requires bounded subagent work."""

    rows = events if isinstance(events, list) else []
    requirements = _mf_lane_ownership_requirements(contract)
    route_identity = _contract_route_identity(contract)
    if not requirements["subagent_required"]:
        return {
            "schema_version": MF_LANE_OWNERSHIP_SCHEMA_VERSION,
            "passed": True,
            "status": "not_applicable",
            "subagent_required": False,
            "required_lane_ownership_ids": [],
            "present_lane_ownership_ids": [],
            "missing_lane_ownership_ids": [],
            "requirement_signals": [],
            "route_identity": route_identity,
            "evidence_events": [],
            "observer_direct_exception": {"accepted": False},
            "checks": {
                "has_subagent_requirement": False,
                "has_subagent_dispatch": True,
                "has_subagent_review_ready": True,
                "has_observer_direct_exception": False,
            },
        }

    dispatch_events: list[dict[str, Any]] = []
    review_ready_events: list[dict[str, Any]] = []
    rejected_exceptions: list[dict[str, Any]] = []
    accepted_exception: dict[str, Any] | None = None
    for raw in rows:
        event = _mapping(raw)
        if not event:
            continue
        if _is_subagent_dispatch_event(event):
            dispatch_events.append(_lane_event_summary(event, MF_BOUNDED_SUBAGENT_DISPATCH_ID))
        if _is_subagent_review_ready_event(event):
            review_ready_events.append(
                _lane_event_summary(event, MF_BOUNDED_SUBAGENT_REVIEW_READY_ID)
            )
        exception = _observer_direct_exception_event(event, route_identity)
        if exception.get("accepted"):
            accepted_exception = exception
        elif exception.get("missing_fields") != ["observer_direct_exception_event"]:
            rejected_exceptions.append(exception)

    present: list[str] = []
    if dispatch_events:
        present.append(MF_BOUNDED_SUBAGENT_DISPATCH_ID)
    if review_ready_events:
        present.append(MF_BOUNDED_SUBAGENT_REVIEW_READY_ID)
    if accepted_exception:
        present.append("observer_direct_implementation_exception")

    missing: list[str] = []
    if not accepted_exception:
        if not dispatch_events:
            missing.append(MF_BOUNDED_SUBAGENT_DISPATCH_ID)
        if not review_ready_events:
            missing.append(MF_BOUNDED_SUBAGENT_REVIEW_READY_ID)

    passed = not missing
    return {
        "schema_version": MF_LANE_OWNERSHIP_SCHEMA_VERSION,
        "passed": passed,
        "status": "passed" if passed else "failed",
        "subagent_required": True,
        "required_lane_ownership_ids": requirements["required_lane_ownership_ids"],
        "present_lane_ownership_ids": present,
        "missing_lane_ownership_ids": missing,
        "requirement_signals": requirements["requirement_signals"],
        "route_identity": route_identity,
        "evidence_events": [*dispatch_events, *review_ready_events],
        "observer_direct_exception": accepted_exception or {"accepted": False},
        "rejected_observer_direct_exceptions": rejected_exceptions,
        "checks": {
            "has_subagent_requirement": True,
            "has_subagent_dispatch": bool(dispatch_events),
            "has_subagent_review_ready": bool(review_ready_events),
            "has_observer_direct_exception": bool(accepted_exception),
        },
    }


def _actor_is_judge(actor: str) -> bool:
    token = _normalize_token(actor)
    return any(judge in token for judge in MF_JUDGE_ACTOR_TOKENS)


def _actor_is_observer(actor: str) -> bool:
    token = _normalize_token(actor)
    if _actor_is_judge(token):
        return False
    return any(obs in token for obs in MF_OBSERVER_ACTOR_TOKENS)


def _event_targets_judge_finding(event: dict[str, Any]) -> bool:
    """Detect a blocker-resolution event that targets a judge finding."""

    marker = _event_key_text(event)
    finding_kind = _normalize_token(
        _first_event_string(
            event,
            {"finding_kind", "blocker_kind", "blocker_source", "finding_source"},
        )
    )
    if any(judge in finding_kind for judge in MF_JUDGE_ACTOR_TOKENS):
        return True
    return "judge" in marker


def _is_blocker_resolution_event(event: dict[str, Any]) -> bool:
    marker = _normalize_token(
        " ".join(
            str(event.get(key) or "")
            for key in ("event_kind", "event_type", "phase")
        )
    )
    return "blocker_resolution" in marker or "blocker_clearance" in marker


def _safety_priority_downgrade(event: dict[str, Any]) -> dict[str, str]:
    """Return {from,to} when an event lowers a safety-relevant priority."""

    from_priority = _normalize_token(
        _first_event_string(event, {"from_priority", "previous_priority", "prior_priority"})
    )
    to_priority = _normalize_token(
        _first_event_string(event, {"to_priority", "new_priority", "priority"})
    )
    rank = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}
    if from_priority in rank and to_priority in rank and rank[to_priority] > rank[from_priority]:
        return {"from": from_priority, "to": to_priority}
    return {}


def mf_blocker_resolution_gate_verification(
    events: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Reject observer self-clearance of judge findings. [regression #3092]

    An observer-actor blocker_resolution / blocker-clearance event that targets a
    judge finding is forced to ``pending_judge_review``; an observer attempt to
    set accepted/resolved/cleared, or to downgrade a safety-relevant priority by
    fiat, is rejected. Only a judge-actor may accept/resolve.
    """

    rows = events if isinstance(events, list) else []
    forced: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    accepted_by_judge: list[dict[str, Any]] = []
    for event in rows:
        event = _mapping(event)
        if not event or not _is_blocker_resolution_event(event):
            continue
        if not _event_targets_judge_finding(event):
            continue
        actor = _text(event.get("actor"))
        status = _normalize_token(event.get("status") or event.get("decision"))
        ref = {
            "id": event.get("id") or event.get("event_id"),
            "event_kind": event.get("event_kind"),
            "actor": actor,
            "status": event.get("status") or event.get("decision"),
        }
        if _actor_is_judge(actor):
            if status in MF_JUDGE_BLOCKER_ACCEPT_STATUSES:
                accepted_by_judge.append(ref)
            continue
        if _actor_is_observer(actor) or not actor:
            downgrade = _safety_priority_downgrade(event)
            if status in MF_JUDGE_BLOCKER_ACCEPT_STATUSES or downgrade:
                rejected.append({
                    **ref,
                    "reason": (
                        "observer_safety_priority_downgrade_by_fiat"
                        if downgrade
                        else "observer_self_cleared_judge_blocker"
                    ),
                    "forced_status": MF_OBSERVER_FORCED_BLOCKER_STATUS,
                    "safety_priority_downgrade": downgrade,
                })
            else:
                forced.append({
                    **ref,
                    "forced_status": MF_OBSERVER_FORCED_BLOCKER_STATUS,
                })
    passed = not rejected
    return {
        "schema_version": MF_BLOCKER_RESOLUTION_GATE_SCHEMA_VERSION,
        "passed": passed,
        "status": "passed" if passed else "blocked",
        "rejected_observer_resolutions": rejected,
        "forced_pending_judge_review": forced,
        "judge_accepted_resolutions": accepted_by_judge,
        "forced_status": MF_OBSERVER_FORCED_BLOCKER_STATUS,
    }


def _close_evidence_ref_identity(event: dict[str, Any]) -> dict[str, str]:
    identity: dict[str, str] = {}
    for field in MF_CROSS_REF_IDENTITY_FIELDS:
        value = _first_deep_text(event, field)
        if value:
            identity[field] = value
    return identity


def _event_declares_accepted_bridge(event: dict[str, Any]) -> bool:
    marker = _normalize_token(
        " ".join(
            str(event.get(key) or "")
            for key in ("event_kind", "event_type", "phase")
        )
    )
    if not ("bridge" in marker or "lineage" in marker):
        return False
    status = _normalize_token(event.get("status") or event.get("decision"))
    return status in {"accepted", "passed", "ok", "approved", "succeeded", "reconciled"}


def _ensure_cross_ref_bridge_meta_contract_aliases() -> None:
    """Register bridge timeline actions with the meta-contract parser.

    The meta-contract whitelist lives in ``mf_subagent_contract`` while bridge
    evidence is timeline-native. Keep this compatibility shim here so the
    storage event_kind remains ``cross_ref_lineage_bridge`` and the whitelist
    gate records the bridge-specific action rather than laundering it through a
    broader observer action.
    """

    try:
        from . import mf_subagent_contract
    except Exception:
        return
    aliases = getattr(mf_subagent_contract, "_META_ACTION_ALIASES", None)
    if not isinstance(aliases, dict):
        return
    aliases.setdefault("cross_ref_lineage_bridge", "cross_ref_lineage_bridge")
    aliases.setdefault("mf_cross_ref_lineage_bridge", "cross_ref_lineage_bridge")
    aliases.setdefault("lineage_bridge", "lineage_bridge")
    aliases.setdefault("mf_lineage_bridge", "lineage_bridge")


# Fields that pin a single bounded-lane identity within one backlog row. A row
# implemented by >=2 mf_sub lanes shares backlog_id + project_id but differs by
# task_id; the row-level aggregate is task_id="".
MF_CROSS_REF_LANE_FIELDS = ("backlog_id", "project_id", "task_id")
# Route + originating-command dimensions a bridge must share with the row before
# its declared sibling identities are honored (scoped bridge authority).
MF_CROSS_REF_BRIDGE_ROUTE_FIELDS = (
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
)
MF_CROSS_REF_ROUTE_SCOPE_FIELDS = (
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "prompt_contract_hash",
    "route_token_ref",
    "visible_injection_manifest_hash",
)
MF_CROSS_REF_BRIDGE_COMMAND_FIELDS = {
    "observer_command_id",
    "command_id",
    "originating_command_id",
}


def _cross_ref_lane_identity(value: Any) -> dict[str, str]:
    """Extract a {backlog_id, project_id, task_id} lane tuple from an event."""

    return {
        field: _first_deep_text(value, field)
        for field in MF_CROSS_REF_LANE_FIELDS
    }


def _cross_ref_lane_key(identity: dict[str, str]) -> tuple[str, ...]:
    return tuple(str(identity.get(field) or "").strip() for field in MF_CROSS_REF_LANE_FIELDS)


def _cross_ref_bridge_route_scope(event: dict[str, Any]) -> tuple[str, ...]:
    """Route-identity tuple a bridge (or the row) is anchored to."""

    return tuple(
        _first_deep_text(event, field) for field in MF_CROSS_REF_BRIDGE_ROUTE_FIELDS
    )


def _cross_ref_bridge_command(event: dict[str, Any]) -> str:
    return _first_event_string(event, MF_CROSS_REF_BRIDGE_COMMAND_FIELDS) or str(
        event.get("correlation_id") or ""
    ).strip()


def _cross_ref_public_route_scope(value: Any) -> dict[str, str]:
    return {
        field: token
        for field in MF_CROSS_REF_ROUTE_SCOPE_FIELDS
        if (token := _first_deep_text(value, field))
    }


def _cross_ref_merge_public_route_scope(
    base: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, str]:
    merged = {
        field: str(base.get(field) or "").strip()
        for field in MF_CROSS_REF_ROUTE_SCOPE_FIELDS
        if str(base.get(field) or "").strip()
    }
    for field in MF_CROSS_REF_ROUTE_SCOPE_FIELDS:
        if not merged.get(field):
            token = str(candidate.get(field) or "").strip()
            if token:
                merged[field] = token
    return merged


def _cross_ref_canonical_route_scope(
    rows: list[Any],
    route_context_gate: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Return the canonical parent route scope, preferring cleanup evidence."""

    gate = _mapping(route_context_gate)
    cleanup = _mapping(gate.get("route_identity_cleanup"))
    cleanup_scope = _cross_ref_public_route_scope(cleanup.get("route_identity") or {})
    cleanup_event = _mapping(cleanup.get("event"))
    cleanup_event_id = str(cleanup_event.get("id") or cleanup_event.get("event_id") or "").strip()

    selected_cleanup_scope: dict[str, str] = {}
    for raw in rows:
        event = _mapping(raw)
        if not event or not _route_event_is_identity_cleanup(event):
            continue
        if cleanup_event_id and str(event.get("id") or event.get("event_id") or "").strip() != cleanup_event_id:
            continue
        if not _route_event_passed(event):
            continue
        selected_cleanup_scope = _cross_ref_public_route_scope(event)
        if selected_cleanup_scope:
            break

    scope = _cross_ref_merge_public_route_scope(cleanup_scope, selected_cleanup_scope)
    gate_identity = _cross_ref_public_route_scope(gate.get("route_identity") or {})
    scope = _cross_ref_merge_public_route_scope(scope, gate_identity)
    return scope


def _cross_ref_row_anchor(
    rows: list[Any],
    trusted: dict[str, str] | None = None,
    route_context_gate: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Derive the row's backlog_id/project_id + accepted route + originating
    observer command. Used to SCOPE which bridge-declared sibling identities are
    honored (#3090 boundary).

    The TRUSTED caller-supplied identity (``trusted``, from ``row_identity`` /
    ``expected``) takes PRIORITY for the backlog_id/project_id row anchor. These
    two fields define the foreign-row floor and must NOT be derivable from
    attacker-supplied close evidence: if the caller pins them, that value wins
    and close-evidence values can never redefine the anchor. Route/command
    dimensions (which legitimately supersede under one row) are still seeded from
    protected close evidence to scope bridge authority."""

    trusted = trusted or {}
    anchor: dict[str, str] = {
        "backlog_id": "",
        "project_id": "",
        "route_id": "",
        "route_context_hash": "",
        "prompt_contract_id": "",
        "command": "",
        "task_id": "",
    }
    # Trusted identity wins for the backlog/project floor BEFORE any event is
    # consulted; once set from the trusted source it is never overwritten below.
    for field in ("backlog_id", "project_id"):
        trusted_value = str(trusted.get(field) or "").strip()
        if trusted_value:
            anchor[field] = trusted_value
    canonical_route_scope = _cross_ref_canonical_route_scope(rows, route_context_gate)
    for field in MF_CROSS_REF_BRIDGE_ROUTE_FIELDS:
        if canonical_route_scope.get(field):
            anchor[field] = canonical_route_scope[field]
    for raw in rows:
        event = _mapping(raw)
        if not is_protected_close_evidence(event):
            continue
        for field in ("backlog_id", "project_id"):
            if not anchor[field]:
                anchor[field] = _first_deep_text(event, field)
        for field in MF_CROSS_REF_BRIDGE_ROUTE_FIELDS:
            if not anchor[field]:
                anchor[field] = _first_deep_text(event, field)
        if not anchor["command"]:
            anchor["command"] = _cross_ref_bridge_command(event)
        if (
            not anchor["task_id"]
            and _normalize_token(event.get("event_kind")) == "close_ready"
        ):
            anchor["task_id"] = _first_deep_text(event, "task_id")
    if not anchor["task_id"]:
        for raw in rows:
            event = _mapping(raw)
            if is_protected_close_evidence(event):
                anchor["task_id"] = _first_deep_text(event, "task_id")
                if anchor["task_id"]:
                    break
    return anchor


def _cross_ref_route_token_child_lineage_diagnosis(
    event: dict[str, Any],
    anchor: Mapping[str, Any],
    canonical_route_scope: Mapping[str, Any],
) -> dict[str, Any]:
    lane = _cross_ref_lane_identity(event)
    row_backlog = str(anchor.get("backlog_id") or "").strip()
    row_project = str(anchor.get("project_id") or "").strip()
    parent_task_id = _first_deep_text(event, "parent_task_id")
    runtime_context_id = _first_deep_text(event, "runtime_context_id")
    worker_slot_id = (
        _first_deep_text(event, "worker_slot_id")
        or _first_deep_text(event, "worker_id")
    )
    route_scope = _cross_ref_public_route_scope(event)
    command = _cross_ref_bridge_command(event)
    missing: list[str] = []

    if row_backlog and lane.get("backlog_id") and lane["backlog_id"] != row_backlog:
        missing.append("row_backlog_id_match")
    if row_project and lane.get("project_id") and lane["project_id"] != row_project:
        missing.append("row_project_id_match")
    if not route_scope.get("route_token_ref"):
        missing.append("route_token_ref")
    if not route_scope.get("route_id"):
        missing.append("child_route_id")
    if not route_scope.get("route_context_hash"):
        missing.append("child_route_context_hash")
    if not route_scope.get("prompt_contract_id"):
        missing.append("child_prompt_contract_id")
    if not parent_task_id:
        missing.append("parent_task_id")
    elif row_backlog and parent_task_id != row_backlog:
        missing.append("parent_task_id_matches_root_backlog")
    if not runtime_context_id:
        missing.append("runtime_context_id")
    if not worker_slot_id:
        missing.append("worker_slot_id")
    if not command:
        missing.append("observer_command_id")
    if not canonical_route_scope.get("route_context_hash"):
        missing.append("canonical_parent_route_context_hash")
    if not canonical_route_scope.get("prompt_contract_id"):
        missing.append("canonical_parent_prompt_contract_id")
    # Event payloads can describe child lineage but cannot prove the route-token
    # registry state. Close-satisfying admission needs a server-side registry
    # check that the active, non-superseded token ref belongs to this canonical
    # parent and matches the worker lane.
    missing.append("route_token_registry_proof")
    event_scope_complete = missing == ["route_token_registry_proof"]

    return {
        "schema_version": "mf_cross_ref_route_token_child_lineage.v1",
        "event_id": event.get("id") or event.get("event_id"),
        "event_kind": event.get("event_kind"),
        "accepted": False,
        "registry_verified": False,
        "event_scope_complete": event_scope_complete,
        "advisory_only": True,
        "missing_fields": missing,
        "registry_proof_required": [
            "active_non_superseded_route_token_ref",
            "canonical_parent_backlog_id",
            "worker_task_id",
            "runtime_context_id",
            "fence_token",
            "worker_slot_id",
            "allowed_action",
            "canonical_parent_route_identity",
            "child_route_identity",
        ],
        "lane_identity": lane,
        "parent_task_id": parent_task_id,
        "runtime_context_id": runtime_context_id,
        "worker_slot_id": worker_slot_id,
        "child_route_scope": route_scope,
        "canonical_parent_route_scope": {
            field: str(canonical_route_scope.get(field) or "").strip()
            for field in MF_CROSS_REF_ROUTE_SCOPE_FIELDS
            if str(canonical_route_scope.get(field) or "").strip()
        },
        "command_scope": {
            key: value
            for key, value in {
                "observer_command_id": command,
                "row_command": str(anchor.get("command") or "").strip(),
            }.items()
            if value
        },
        "reason": (
            "route_token_child_lineage_unproven_registry"
            if event_scope_complete
            else "route_token_child_lineage_missing_required_scope"
        ),
        "close_satisfying_by_itself": False,
    }


def _cross_ref_route_token_child_diagnostics(
    rows: list[Any],
    anchor: dict[str, str],
    route_context_gate: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return route-token child diagnostics without granting close membership.

    ``task_timeline`` receives timeline events, not the authoritative route-token
    registry. Self-declared child-route fields are therefore advisory until a
    server-side registry check proves active/non-superseded token ownership.
    """

    gate = _mapping(route_context_gate)
    cleanup = _mapping(gate.get("route_identity_cleanup"))
    if not cleanup.get("applied"):
        return []

    canonical_route_scope = _cross_ref_canonical_route_scope(rows, gate)
    if not canonical_route_scope:
        return []

    diagnostics: list[dict[str, Any]] = []
    row_backlog = str(anchor.get("backlog_id") or "").strip()
    row_project = str(anchor.get("project_id") or "").strip()

    for raw in rows:
        event = _mapping(raw)
        if not event or not is_protected_close_evidence(event):
            continue
        lane = _cross_ref_lane_identity(event)
        if row_backlog and lane.get("backlog_id") and lane["backlog_id"] != row_backlog:
            continue
        if row_project and lane.get("project_id") and lane["project_id"] != row_project:
            continue
        route_scope = _cross_ref_public_route_scope(event)
        parent_task_id = _first_deep_text(event, "parent_task_id")
        if not route_scope.get("route_token_ref") and not parent_task_id:
            continue
        diagnosis = _cross_ref_route_token_child_lineage_diagnosis(
            event,
            anchor,
            canonical_route_scope,
        )
        diagnostics.append(diagnosis)
    return diagnostics


def _cross_ref_bridge_scope_membership(
    rows: list[Any],
    anchor: dict[str, str],
) -> set[tuple[str, ...]]:
    """Build the accepted-lane membership set from accepted bridge events.

    Consumes each accepted bridge event's ``payload.bridged_identities[]`` and,
    for every declared sibling that shares the row's backlog_id + project_id, adds
    that sibling lane PLUS the row-level aggregate (task_id="") to the membership
    set. Bridge authority is SCOPED: a bridge is only honored when it shares the
    row's accepted route identity (route_id / route_context_hash /
    prompt_contract_id, where both sides declare them) AND the row's originating
    observer command. This makes the row identity an equivalence class over the
    declared lane set rather than a winner-take-all latest lineage.
    """

    membership: set[tuple[str, ...]] = set()
    row_backlog = str(anchor.get("backlog_id") or "").strip()
    row_project = str(anchor.get("project_id") or "").strip()
    row_route = _cross_ref_bridge_route_scope(anchor)
    row_command = str(anchor.get("command") or "").strip()

    for raw in rows:
        event = _mapping(raw)
        if not _event_declares_accepted_bridge(event):
            continue
        # Scoped authority: only honor a bridge whose declared route identity and
        # originating observer command match the row's. Where a dimension is
        # absent on either side we do not treat it as a mismatch (events do not
        # always re-declare every route field), but any declared-and-differing
        # dimension disqualifies the bridge.
        bridge_route = _cross_ref_bridge_route_scope(event)
        route_ok = all(
            (not want) or (not got) or want == got
            for want, got in zip(row_route, bridge_route)
        )
        if not route_ok:
            continue
        bridge_command = _cross_ref_bridge_command(event)
        if row_command and bridge_command and bridge_command != row_command:
            continue

        for declared in _field_values(event, {"bridged_identities"}):
            for sibling in _list(declared):
                sibling = _mapping(sibling)
                if not sibling:
                    continue
                sib_backlog = str(sibling.get("backlog_id") or "").strip()
                sib_project = str(sibling.get("project_id") or "").strip()
                sib_task = str(sibling.get("task_id") or "").strip()
                # HARD FOREIGN-ROW FLOOR (#3090): a sibling may only vary from the
                # trusted row anchor by task_id. Any declared backlog_id/project_id
                # that differs from the trusted anchor is a foreign row and is
                # NEVER bridged in, regardless of what the bridge declares. The
                # membership lane is always pinned to the TRUSTED row anchor, so a
                # bridge cannot inject a foreign backlog/project even by omitting or
                # mismatching the field.
                if sib_backlog and sib_backlog != row_backlog:
                    continue
                if sib_project and sib_project != row_project:
                    continue
                membership.add((row_backlog, row_project, sib_task))
                # Row-level aggregate scope (task_id="") for this lane set.
                membership.add((row_backlog, row_project, ""))
    return membership


def mf_close_cross_ref_gate_verification(
    events: list[dict[str, Any]] | None,
    row_identity: dict[str, Any] | None = None,
    route_context_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reject close evidence refs from a different row. [regression #3090]

    Close evidence whose backlog_id/route_id/prompt_contract_id/scope differs
    from the row's is rejected unless an explicit accepted bridge/lineage event
    links the two. The row identity is inferred from the row_identity argument or
    from the dominant identity across the supplied evidence.
    """

    rows = events if isinstance(events, list) else []
    expected = {
        field: str((row_identity or {}).get(field) or "").strip()
        for field in MF_CROSS_REF_IDENTITY_FIELDS
        if str((row_identity or {}).get(field) or "").strip()
    }
    # Trusted backlog/project floor (#3090): the caller-supplied row_identity is
    # the ONLY trusted source for the backlog_id/project_id anchor. It is computed
    # up front so it can constrain BOTH the legacy bridged-skip scraper below and
    # the membership consumer further down. Inferred values (when no row_identity
    # is supplied) are filled in after the stable-dimension inference.
    trusted_floor = {
        field: str((row_identity or {}).get(field) or "").strip()
        for field in ("backlog_id", "project_id")
        if str((row_identity or {}).get(field) or "").strip()
    }

    bridged: set[str] = set()
    if any(_event_declares_accepted_bridge(_mapping(event)) for event in rows):
        # An accepted bridge/lineage event links cross-row evidence; collect the
        # identities it authorizes so they are not rejected below.
        for event in rows:
            event = _mapping(event)
            if not _event_declares_accepted_bridge(event):
                continue
            for field in MF_CROSS_REF_IDENTITY_FIELDS:
                # EXPLICIT bridge authorization: a top-level `bridged_<field>` key
                # is the deliberate legacy bridge declaration and may authorize a
                # foreign value for that single field (e.g. bridged_backlog_id).
                for value in _field_values(event, {f"bridged_{field}"}):
                    text = str(value or "").strip()
                    if text:
                        bridged.add(f"{field}={text}")
                # IMPLICIT scrape of the plain `<field>` recurses into nested
                # structures including bridged_identities[]. HARD FOREIGN-ROW FLOOR
                # (#3090): a foreign backlog_id/project_id buried in a declared
                # sibling must NEVER be injected into the skip set, or it would
                # bypass the per-evidence mismatch check. Only admit a plain
                # backlog_id/project_id that equals the trusted floor; other
                # dimensions (route_id, prompt_contract_id, scope) legitimately
                # vary under one row and are unaffected.
                for value in _field_values(event, {field}):
                    text = str(value or "").strip()
                    if not text:
                        continue
                    if (
                        field in trusted_floor
                        and text != trusted_floor[field]
                    ):
                        continue
                    bridged.add(f"{field}={text}")

    # If no explicit row identity is supplied, infer it only from the stable
    # cross-reference dimensions (backlog_id / scope). Route-identity supersession
    # legitimately changes route_id/prompt_contract_id under the same row, so we
    # do not infer those — the stale-route gate owns that lineage.
    if not expected:
        best: dict[str, str] = {}
        for event in rows:
            event = _mapping(event)
            if not is_protected_close_evidence(event):
                continue
            candidate = {
                field: value
                for field, value in _close_evidence_ref_identity(event).items()
                if field in MF_CROSS_REF_INFERRED_IDENTITY_FIELDS
            }
            if len(candidate) > len(best):
                best = candidate
        expected = best

    # Treat the row identity as an equivalence class over the lane set declared
    # by accepted, in-scope bridge events. Each honored sibling lane (and the
    # row-level aggregate, task_id="") is admitted into the accepted-scope
    # membership so evidence from a non-canonical lane is not rejected.
    #
    # The backlog/project floor for the anchor comes from the TRUSTED row
    # identity with priority over any value scraped from close evidence: a
    # caller-supplied row_identity pins it directly, and otherwise we fall back to
    # the identity inferred from the stable cross-ref dimensions (NOT from a bridge
    # or a single foreign event). Attacker-supplied close evidence can therefore
    # never redefine the row's backlog/project anchor (#3090).
    trusted_anchor = dict(trusted_floor)
    for field in ("backlog_id", "project_id"):
        if not trusted_anchor.get(field) and expected.get(field):
            trusted_anchor[field] = str(expected.get(field) or "").strip()
    anchor = _cross_ref_row_anchor(
        rows,
        trusted_anchor,
        route_context_gate=route_context_gate,
    )
    lane_membership = _cross_ref_bridge_scope_membership(rows, anchor)
    advisory_route_token_child_lineages = _cross_ref_route_token_child_diagnostics(
        rows,
        anchor,
        route_context_gate,
    )
    child_diagnostics_by_event_id = {
        str(item.get("event_id") or ""): item
        for item in advisory_route_token_child_lineages
        if item.get("event_id") not in ("", None)
    }

    rejected: list[dict[str, Any]] = []
    for event in rows:
        event = _mapping(event)
        if not is_protected_close_evidence(event):
            continue
        identity = _close_evidence_ref_identity(event)
        # If this evidence's lane {backlog_id, project_id, task_id} is covered by
        # an accepted in-scope bridge (or its row-level aggregate), it is part of
        # the row's equivalence class and is not a cross-ref mismatch.
        lane = _cross_ref_lane_identity(event)
        lane_key = _cross_ref_lane_key(lane)
        if lane_key in lane_membership:
            continue
        aggregate_key = (lane_key[0], lane_key[1], "")
        if aggregate_key in lane_membership and lane_key[2] == "":
            continue
        mismatches: dict[str, dict[str, str]] = {}
        anchor_task = str(anchor.get("task_id") or "").strip()
        if lane_key[2] and anchor_task and lane_key[2] != anchor_task:
            mismatches["task_id"] = {
                "expected": anchor_task,
                "actual": lane_key[2],
            }
        for field, want in expected.items():
            got = identity.get(field, "")
            if got and got != want and f"{field}={got}" not in bridged:
                mismatches[field] = {"expected": want, "actual": got}
        if mismatches:
            rejected.append({
                "id": event.get("id") or event.get("event_id"),
                "event_kind": event.get("event_kind"),
                "status": event.get("status") or event.get("decision"),
                "reason": "cross_ref_identity_mismatch",
                "mismatches": mismatches,
                "event_ref_identity": identity,
                "lane_identity": lane,
                "event_route_scope": _cross_ref_public_route_scope(event),
                "command_scope": {
                    key: value
                    for key, value in {
                        "observer_command_id": _cross_ref_bridge_command(event),
                        "row_command": str(anchor.get("command") or "").strip(),
                    }.items()
                    if value
                },
                "route_token_child_lineage": child_diagnostics_by_event_id.get(
                    str(event.get("id") or event.get("event_id") or ""),
                    {},
                ),
            })
    passed = not rejected
    return {
        "schema_version": MF_CROSS_REF_GATE_SCHEMA_VERSION,
        "passed": passed,
        "status": "passed" if passed else "blocked",
        "row_identity": expected,
        "row_anchor": anchor,
        "canonical_route_scope": _cross_ref_canonical_route_scope(rows, route_context_gate),
        "bridged_identities": sorted(bridged),
        "bridged_lane_membership": sorted(
            {"|".join(key) for key in lane_membership}
        ),
        "accepted_route_token_child_lineages": [],
        "advisory_route_token_child_lineages": advisory_route_token_child_lineages,
        "ignored_route_token_child_lineages": advisory_route_token_child_lineages,
        "rejected_cross_ref_evidence": rejected,
    }


def mf_stale_route_evidence_gate_verification(
    events: list[dict[str, Any]] | None,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Invalidate close evidence recorded under a superseded route. [#3093/#3094]

    Builds on the existing route_identity_cleanup handling: when a route identity
    has been superseded/repaired, prior read_receipt/startup/dispatch/close
    evidence recorded under the stale identity does NOT count for close and must
    be re-recorded under the canonical (cleanup) route identity.

    Rerecorded-under-canonical exemption: if a passing event of the same
    stale-evidence kind has been recorded under the canonical identity, the
    old-identity event is reported in ``rerecorded_close_evidence`` (as an
    audit pair) instead of blocking in ``superseded_close_evidence``.  Only
    kinds for which every matched stale token has a canonical counterpart are
    exempted; partial coverage still blocks.
    """

    rows = events if isinstance(events, list) else []
    route_gate = mf_route_context_gate_verification(rows, contract)
    cleanup = _mapping(route_gate.get("route_identity_cleanup"))
    canonical_identity = _mapping(cleanup.get("route_identity"))
    superseded: list[dict[str, Any]] = []
    rerecorded: list[dict[str, Any]] = []

    if cleanup.get("applied") and canonical_identity:
        # Build map: stale-kind token -> canonical event id, for each
        # passing event recorded under the canonical route identity.
        # An event may match multiple stale tokens (e.g. a kind that contains
        # both "startup" and "dispatch" substrings) – all matched tokens are
        # recorded so the superseded loop can require full coverage.
        canonical_kinds: dict[str, Any] = {}
        for event in rows:
            event = _mapping(event)
            if not event or _route_event_is_identity_cleanup(event):
                continue
            identity = _route_identity(event)
            if not identity:
                continue
            if not _route_identity_matches_filter(identity, canonical_identity):
                continue
            # Only passing events count as canonical rerecords.
            status = str(event.get("status") or event.get("decision") or "").strip().lower()
            if not (bool(event.get("passed")) or status in MF_ROUTE_CONTEXT_PASS_STATUSES):
                continue
            kind = _normalize_token(event.get("event_kind") or event.get("event_type"))
            event_id = event.get("id") or event.get("event_id")
            for stale_token in MF_STALE_ROUTE_EVIDENCE_KINDS:
                if stale_token in kind:
                    # First canonical match wins per token.
                    if stale_token not in canonical_kinds:
                        canonical_kinds[stale_token] = event_id

        for event in rows:
            event = _mapping(event)
            if not event or _route_event_is_identity_cleanup(event):
                continue
            kind = _normalize_token(event.get("event_kind") or event.get("event_type"))
            matched_tokens = [t for t in MF_STALE_ROUTE_EVIDENCE_KINDS if t in kind]
            if not matched_tokens:
                continue
            identity = _route_identity(event)
            if not identity:
                continue
            if _route_identity_matches_filter(identity, canonical_identity):
                continue
            # All matched stale tokens must have canonical counterparts for exemption.
            if all(t in canonical_kinds for t in matched_tokens):
                rerecorded.append({
                    "superseded_id": event.get("id") or event.get("event_id"),
                    "event_kind": event.get("event_kind"),
                    "canonical_event_ids": [canonical_kinds[t] for t in matched_tokens],
                    "reason": "superseded_route_identity_evidence_rerecorded",
                })
            else:
                superseded.append({
                    "id": event.get("id") or event.get("event_id"),
                    "event_kind": event.get("event_kind"),
                    "status": event.get("status") or event.get("decision"),
                    "reason": "superseded_route_identity_evidence",
                    "next_action": "re-record under canonical route identity",
                })
    passed = not superseded
    return {
        "schema_version": MF_STALE_ROUTE_EVIDENCE_GATE_SCHEMA_VERSION,
        "passed": passed,
        "status": "passed" if passed else "blocked",
        "route_identity_cleanup_applied": bool(cleanup.get("applied")),
        "canonical_route_identity": canonical_identity,
        "superseded_close_evidence": superseded,
        "rerecorded_close_evidence": rerecorded,
    }


def _event_has_close_waiver(event: dict[str, Any]) -> bool:
    """Detect an explicit, recorded close-waiver marker on a timeline event.

    A close waiver is never inferred — it must be present as an event-kind /
    event-type token, or as a truthy ``close_waiver`` style field. It must also
    carry a passing status so a merely-proposed waiver does not authorize close.
    """

    if not _event_passed(event):
        return False
    marker = _normalize_token(
        " ".join(
            str(event.get(key) or "")
            for key in ("event_kind", "event_type", "phase", "decision")
        )
    )
    if any(token in marker for token in MF_CLOSE_WAIVER_EVENT_TOKENS):
        return True
    for value in _event_field_values(
        event, {"close_waiver", "backlog_close_waiver", "close_gate_waiver"}
    ):
        if isinstance(value, dict):
            if _truthy(value.get("waived") or value.get("approved") or value.get("granted")):
                return True
        elif _truthy(value):
            return True
    return False


def mf_close_waiver_state(events: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Project whether an explicit, visible close-waiver state exists."""

    rows = events if isinstance(events, list) else []
    waiver_events: list[dict[str, Any]] = []
    for raw in rows:
        event = _mapping(raw)
        if not event:
            continue
        if _event_has_close_waiver(event):
            waiver_events.append(
                {
                    "id": event.get("id") or event.get("event_id"),
                    "event_kind": event.get("event_kind"),
                    "event_type": event.get("event_type"),
                    "status": event.get("status") or event.get("decision"),
                    "reason": _first_event_string(
                        event, {"reason", "waiver_reason", "close_waiver_reason"}
                    ),
                }
            )
    return {"has_close_waiver": bool(waiver_events), "waiver_events": waiver_events}


def _approval_scope_text(event: dict[str, Any]) -> str:
    parts: list[str] = []
    for value in _event_field_values(event, MF_APPROVAL_SCOPE_FIELDS):
        if isinstance(value, (dict, list)):
            continue
        text = str(value or "").strip()
        if text:
            parts.append(text)
    return _normalize_token(" ".join(parts))


def _event_cites_approval(event: dict[str, Any]) -> bool:
    marker = _normalize_token(
        " ".join(
            str(event.get(key) or "")
            for key in ("event_kind", "event_type", "phase")
        )
    )
    if any(kind in marker for kind in MF_APPROVAL_BEARING_KINDS):
        return True
    return bool(_approval_scope_text(event)) or _has_operator_approval(event)


def mf_close_approval_scope_gate_verification(
    events: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Reject a close whose own cited human approval excludes backlog_close.

    Criterion 1: when the cited approval text explicitly excludes the
    backlog_close action (e.g. "review_ready only / does not authorize
    backlog_close"), the close is blocked unless an explicit recorded
    close-waiver state is also present. A close whose own cited approval forbids
    it must not succeed silently.
    """

    rows = events if isinstance(events, list) else []
    waiver_state = mf_close_waiver_state(rows)
    has_waiver = bool(waiver_state.get("has_close_waiver"))
    excluding: list[dict[str, Any]] = []
    for raw in rows:
        event = _mapping(raw)
        if not event or not _event_cites_approval(event):
            continue
        scope = _approval_scope_text(event)
        if not scope:
            continue
        matched = [
            token
            for token in MF_APPROVAL_CLOSE_EXCLUSION_TOKENS
            if token in scope
        ]
        if matched:
            excluding.append(
                {
                    "id": event.get("id") or event.get("event_id"),
                    "event_kind": event.get("event_kind"),
                    "event_type": event.get("event_type"),
                    "status": event.get("status") or event.get("decision"),
                    "matched_exclusions": matched,
                    "reason": "cited_approval_excludes_backlog_close",
                }
            )
    # An explicit close waiver converts the block into a visible, recorded waiver
    # state rather than a silent success.
    passed = not excluding or has_waiver
    return {
        "schema_version": MF_APPROVAL_SCOPE_GATE_SCHEMA_VERSION,
        "passed": passed,
        "status": (
            "passed"
            if not excluding
            else ("waived" if has_waiver else "blocked")
        ),
        "has_close_waiver": has_waiver,
        "close_waiver_state": waiver_state,
        "approvals_excluding_close": excluding,
    }


def _command_disposition_events(
    events: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Reduce observer-command events to the latest disposition per command id."""

    latest: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(events):
        event = _mapping(raw)
        if not event:
            continue
        marker = _normalize_token(
            " ".join(
                str(event.get(key) or "")
                for key in ("event_kind", "event_type", "phase")
            )
        )
        if not any(kind in marker for kind in MF_COMMAND_DISPOSITION_KINDS):
            continue
        command_id = _first_event_string(
            event, {"observer_command_id", "command_id", "originating_command_id"}
        )
        if not command_id:
            continue
        # Prefer the explicit command-disposition fields over the generic event
        # status/decision: a complete/fail event commonly carries
        # status="accepted" at the event level while the command's own
        # disposition lives in command_status/disposition.
        status = _normalize_token(
            _first_event_string(event, {"command_status", "disposition"})
        )
        if not status:
            status = _normalize_token(
                _first_event_string(event, {"status", "decision"})
            )
        # Last write per command id wins (events are ordered oldest→newest).
        latest[command_id] = {
            "command_id": command_id,
            "status": status,
            "order": index,
            "event_kind": event.get("event_kind"),
            "event_type": event.get("event_type"),
            "id": event.get("id") or event.get("event_id"),
        }
    return latest


def mf_close_command_disposition_gate_verification(
    events: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Require the originating observer command to be terminal before close.

    Criterion 3: a still-"claimed" (or otherwise non-terminal) originating
    observer command blocks the close, unless it is co-resolved with the close
    (a terminal/co_resolved disposition event recorded on the timeline).
    """

    rows = events if isinstance(events, list) else []
    dispositions = _command_disposition_events(rows)
    blocking: list[dict[str, Any]] = []
    terminal: list[dict[str, Any]] = []
    for command_id, disposition in dispositions.items():
        status = disposition.get("status") or ""
        if status in MF_COMMAND_TERMINAL_STATUSES:
            terminal.append(disposition)
        elif status in MF_COMMAND_CLAIMED_STATUSES or not status:
            blocking.append(
                {
                    **disposition,
                    "reason": "originating_command_not_terminal",
                    "next_action": (
                        "complete or co-resolve the originating observer command "
                        "before/at backlog close"
                    ),
                }
            )
        else:
            # Unknown disposition token: treat as non-terminal to fail safe.
            blocking.append(
                {
                    **disposition,
                    "reason": "originating_command_disposition_unknown",
                    "next_action": (
                        "record a terminal/co-resolved disposition for the "
                        "originating observer command"
                    ),
                }
            )
    passed = not blocking
    return {
        "schema_version": MF_COMMAND_DISPOSITION_GATE_SCHEMA_VERSION,
        "passed": passed,
        "status": "passed" if passed else "blocked",
        "terminal_commands": terminal,
        "blocking_commands": blocking,
    }


def mf_fixed_close_waiver_alert(
    status: Any,
    can_close: Any,
    events: list[dict[str, Any]] | None,
    *,
    applicable: Any = True,
) -> dict[str, Any]:
    """Governance alert for FIXED rows lacking close authorization.

    Criterion 2: a row in FIXED status with can_close=false and no explicit,
    visible close-waiver marker is a governance integrity alert (FIXED implies
    can_close=true OR a visible waiver marker) when the MF close gate applies.
    """

    normalized_status = _normalize_token(status)
    is_fixed = normalized_status == "fixed"
    applicable_bool = _truthy(applicable)
    can_close_bool = _truthy(can_close)
    waiver_state = mf_close_waiver_state(events)
    has_waiver = bool(waiver_state.get("has_close_waiver"))
    alert = applicable_bool and is_fixed and not can_close_bool and not has_waiver
    return {
        "schema_version": MF_FIXED_CLOSE_WAIVER_ALERT_SCHEMA_VERSION,
        "alert": alert,
        "status": "alert" if alert else "ok",
        "is_fixed": is_fixed,
        "can_close": can_close_bool,
        "has_close_waiver": has_waiver,
        "close_waiver_state": waiver_state,
        "reason": (
            "fixed_row_without_can_close_or_close_waiver" if alert else ""
        ),
    }


def mf_close_gate_verification(
    events: list[dict[str, Any]] | None,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the minimum observer/MF timeline evidence before backlog close."""

    raw_rows = events if isinstance(events, list) else []
    governance_policy = _governance_policy(contract)
    close_timeline_required = _policy_requires(governance_policy, "close_timeline")
    required_event_kinds = (
        set(MF_CLOSE_REQUIRED_EVENT_KINDS) if close_timeline_required else set()
    )

    def _close_present(
        close_rows: list[dict[str, Any]],
    ) -> tuple[set[str], list[dict[str, Any]]]:
        close_present: set[str] = set()
        close_ignored: list[dict[str, Any]] = []
        for event in close_rows:
            if not isinstance(event, dict):
                continue
            kind = str(event.get("event_kind") or "").strip()
            phase = str(event.get("phase") or "").strip()
            status = str(event.get("status") or "").strip().lower()
            key = kind or phase
            if (
                key not in MF_CLOSE_REQUIRED_EVENT_KINDS
                and phase == "verification"
                and kind in {"qa_verification", "independent_verification"}
            ):
                key = "verification"
            if key in required_event_kinds and status in MF_CLOSE_PASS_STATUSES:
                close_present.add(key)
            elif key in required_event_kinds:
                close_ignored.append({
                    "event_kind": kind,
                    "phase": phase,
                    "status": status,
                    "id": event.get("id"),
                })
        return close_present, close_ignored

    raw_present, _raw_ignored = _close_present(raw_rows)
    route_context_gate = mf_route_context_gate_verification(raw_rows, contract)
    finish_gate_projection = mf_subagent_finish_gate_close_projection(
        raw_rows,
        needed_event_kinds=required_event_kinds - raw_present,
        route_context_gate=route_context_gate,
    )
    rows = [
        *raw_rows,
        *_list(finish_gate_projection.get("projected_events")),
    ]
    try:
        from .mf_subagent_contract import close_timeline_startup_event_gate

        close_timeline_startup_gate = close_timeline_startup_event_gate(rows)
    except Exception as exc:
        log.debug("close timeline startup gate projection failed", exc_info=True)
        close_timeline_startup_gate = {
            "schema_version": "mf_close_timeline_startup_gate.v1",
            "passed": False,
            "status": "error",
            "accepted_startup_events": [],
            "demoted_startup_events": [],
            "demoted_startup_event_indexes": [],
            "reason": "close_timeline_startup_gate_error",
            "error": str(exc),
        }
    present, ignored = _close_present(rows)
    missing = sorted(required_event_kinds - present)
    contract_gate = mf_contract_gate_verification(rows, contract)
    lane_ownership_gate = mf_lane_ownership_gate_verification(rows, contract)
    worker_graph_trace_gate = _worker_graph_trace_gate(rows, governance_policy)
    independent_qa_gate = _independent_qa_gate(rows, governance_policy, contract=contract)
    contract_projection = mf_contract_projection(
        rows,
        contract,
        route_context_gate=route_context_gate,
    )
    contract_projection_gate = mf_contract_projection_close_gate_verification(
        contract_projection,
        route_context_gate,
    )
    post_verification_actions_gate = mf_post_verification_actions_gate_verification(
        rows,
        contract,
    )
    blocker_resolution_gate = mf_blocker_resolution_gate_verification(rows)
    cross_ref_gate = mf_close_cross_ref_gate_verification(
        rows,
        route_context_gate=route_context_gate,
    )
    stale_route_evidence_gate = mf_stale_route_evidence_gate_verification(rows, contract)
    approval_scope_gate = mf_close_approval_scope_gate_verification(rows)
    command_disposition_gate = mf_close_command_disposition_gate_verification(rows)
    missing_evidence_groups = mf_close_missing_evidence_groups(
        missing,
        route_context_gate,
    )
    groups = _mapping(missing_evidence_groups.get("groups"))
    if contract_projection_gate.get("required") and not contract_projection_gate.get("passed"):
        groups["contract_projection"] = {
            "label": "contract projection",
            "missing": contract_projection_gate.get("missing_requirement_ids", []),
            "next_action": "repair stale/divergent contract projection or record the worker read receipt before close",
        }
    if finish_gate_projection.get("required") and not finish_gate_projection.get("passed"):
        groups["finish_gate_projection"] = {
            "label": "finish-gate close projection",
            "missing": finish_gate_projection.get("missing_fields", []),
            "next_action": (
                "record a passed mf_subagent_finish_gate with route identity, "
                "worker lineage, observer command id, commit, changed files, "
                "review_ready, and close_ready evidence"
            ),
        }
    if (
        post_verification_actions_gate.get("required")
        and not post_verification_actions_gate.get("passed")
    ):
        groups["post_verification_actions"] = {
            "label": "post-verification impact actions",
            "missing": post_verification_actions_gate.get("missing_actions", []),
            "next_action": "record observer-owned post-verification action or follow-up evidence",
        }
    if worker_graph_trace_gate.get("required") and not worker_graph_trace_gate.get("passed"):
        groups["worker_graph_trace"] = {
            "label": "worker graph trace",
            "missing": worker_graph_trace_gate.get("missing_requirement_ids", []),
            "next_action": "record audited graph_query trace ids from the worker lane",
        }
    if independent_qa_gate.get("required") and not independent_qa_gate.get("passed"):
        groups["independent_qa"] = {
            "label": "independent QA",
            "missing": independent_qa_gate.get("missing_requirement_ids", []),
            "reason": independent_qa_gate.get("reason", ""),
            "rejected_evidence_events": independent_qa_gate.get("rejected_evidence_events", []),
            "next_action": independent_qa_gate.get(
                "next_action",
                "record a passing independent QA verification timeline event",
            ),
        }
    if not blocker_resolution_gate.get("passed"):
        groups["judge_blocker_resolution"] = {
            "label": "judge blocker resolution",
            "missing": blocker_resolution_gate.get("rejected_observer_resolutions", []),
            "next_action": "observer cannot self-clear a judge blocker; route to independent judge review",
        }
    if not cross_ref_gate.get("passed"):
        groups["cross_ref_evidence"] = {
            "label": "cross-reference evidence",
            "missing": cross_ref_gate.get("rejected_cross_ref_evidence", []),
            "next_action": "remove evidence from other backlog/scope or record an accepted bridge/lineage event",
        }
    if not approval_scope_gate.get("passed"):
        groups["approval_scope"] = {
            "label": "human-approval scope",
            "missing": approval_scope_gate.get("approvals_excluding_close", []),
            "next_action": (
                "cited approval excludes backlog_close; obtain an approval that "
                "authorizes close or record an explicit close-waiver state"
            ),
        }
    if not command_disposition_gate.get("passed"):
        groups["command_disposition"] = {
            "label": "originating observer command disposition",
            "missing": command_disposition_gate.get("blocking_commands", []),
            "next_action": (
                "complete or co-resolve the originating observer command "
                "before/at backlog close"
            ),
        }
    if close_timeline_startup_gate and not close_timeline_startup_gate.get("passed"):
        groups["startup_close_satisfying"] = {
            "label": "actual worker startup close-satisfying evidence",
            "missing": close_timeline_startup_gate.get("demoted_startup_events", []),
            "next_action": (
                "record a valid finish-time worker attestation/finish gate, or "
                "start a fresh bounded worker when the old fence is no longer current"
            ),
        }
    missing_evidence_groups["groups"] = groups
    route_context_reminder = mf_route_context_reminder(
        route_context_gate,
        missing_evidence_groups,
    )
    passed = (
        not missing
        and bool(contract_gate.get("passed"))
        and bool(route_context_gate.get("passed"))
        and bool(lane_ownership_gate.get("passed"))
        and bool(worker_graph_trace_gate.get("passed"))
        and bool(independent_qa_gate.get("passed"))
        and bool(contract_projection_gate.get("passed"))
        and bool(post_verification_actions_gate.get("passed"))
        and bool(blocker_resolution_gate.get("passed"))
        and bool(cross_ref_gate.get("passed"))
        and bool(approval_scope_gate.get("passed"))
        and bool(command_disposition_gate.get("passed"))
        and (
            not close_timeline_startup_gate
            or bool(close_timeline_startup_gate.get("passed"))
        )
        # Stale-route evidence invalidation is already enforced by the route
        # context gate (it ignores superseded-identity evidence and requires
        # canonical re-recording). The stale_route_evidence_gate below is the
        # explicit, observable projection of that rule; it does not independently
        # block a close that already carries canonical replacement evidence.
    )
    return {
        "schema_version": "mf_close_timeline_gate.v1",
        "passed": passed,
        "status": "passed" if passed else "failed",
        "required_event_kinds": sorted(required_event_kinds),
        "close_timeline_required": close_timeline_required,
        "present_event_kinds": sorted(present),
        "missing_event_kinds": missing,
        "event_count": len(raw_rows),
        "projected_event_count": len(
            _list(finish_gate_projection.get("projected_events"))
        ),
        "ignored_required_events": ignored,
        "governance_policy": governance_policy,
        "contract_gate": contract_gate,
        "route_context_gate": route_context_gate,
        "finish_gate_projection": finish_gate_projection,
        "lane_ownership_gate": lane_ownership_gate,
        "worker_graph_trace_gate": worker_graph_trace_gate,
        "independent_qa_gate": independent_qa_gate,
        "contract_projection": contract_projection,
        "contract_projection_gate": contract_projection_gate,
        "post_verification_actions_gate": post_verification_actions_gate,
        "blocker_resolution_gate": blocker_resolution_gate,
        "cross_ref_gate": cross_ref_gate,
        "stale_route_evidence_gate": stale_route_evidence_gate,
        "approval_scope_gate": approval_scope_gate,
        "command_disposition_gate": command_disposition_gate,
        "close_timeline_startup_gate": close_timeline_startup_gate,
        "missing_evidence_groups": missing_evidence_groups,
        "route_context_reminder": route_context_reminder,
        "checks": {
            "has_implementation": "implementation" in present,
            "has_verification": "verification" in present,
            "has_close_ready": "close_ready" in present,
            "has_contract_evidence": bool(contract_gate.get("passed")),
            "has_route_context_consumption": bool(route_context_gate.get("passed")),
            "has_finish_gate_projection": bool(
                finish_gate_projection.get("projected_event_kinds")
            ),
            "has_lane_ownership": bool(lane_ownership_gate.get("passed")),
            "has_worker_graph_trace": bool(worker_graph_trace_gate.get("passed")),
            "has_independent_qa": bool(independent_qa_gate.get("passed")),
            "has_contract_projection": bool(contract_projection.get("schema_version")),
            "has_current_contract_projection": bool(
                contract_projection_gate.get("passed")
            ),
            "has_post_verification_actions": bool(
                post_verification_actions_gate.get("passed")
            ),
            "no_observer_self_cleared_judge_blocker": bool(
                blocker_resolution_gate.get("passed")
            ),
            "no_cross_ref_evidence": bool(cross_ref_gate.get("passed")),
            "no_stale_route_evidence": bool(stale_route_evidence_gate.get("passed")),
            "approval_authorizes_close": bool(approval_scope_gate.get("passed")),
            "originating_command_terminal": bool(
                command_disposition_gate.get("passed")
            ),
            "mf_subagent_startup_close_satisfying": (
                True
                if not close_timeline_startup_gate
                else bool(close_timeline_startup_gate.get("passed"))
            ),
            "mf_subagent_read_receipt_gate": str(
                _mapping(contract_projection.get("read_receipt_gate")).get("status") or ""
            ),
        },
    }


def compact_gate_summary(
    full_result: dict[str, Any],
    request_id: str = "",
) -> dict[str, Any]:
    """Derive the compact precheck/gate summary from a full mf_close_gate_verification result.

    Returns only the fields needed to understand close-readiness:
    ok, project_id, bug_id, applicable, can_close, missing_event_kinds,
    failed_gates (gates with passed=false only), route_identity (canonical 3 hashes),
    event_count, and request_id.

    The full gate tree is NOT re-evaluated; this is a projection of the existing result.
    """
    status = str(full_result.get("status") or "").strip().lower()
    not_applicable = full_result.get("applicable") is False or status == "not_applicable"
    passed = bool(full_result.get("passed") or full_result.get("can_close"))
    if not_applicable:
        passed = False
    can_close = passed

    missing_event_kinds = list(full_result.get("missing_event_kinds") or [])

    gate_keys = [
        "contract_gate",
        "route_context_gate",
        "finish_gate_projection",
        "lane_ownership_gate",
        "worker_graph_trace_gate",
        "independent_qa_gate",
        "contract_projection_gate",
        "post_verification_actions_gate",
        "blocker_resolution_gate",
        "cross_ref_gate",
        "approval_scope_gate",
        "command_disposition_gate",
        "close_timeline_startup_gate",
    ]
    failed_gates = []
    for key in gate_keys:
        gate = _mapping(full_result.get(key))
        if not gate:
            continue
        if not bool(gate.get("passed")):
            missing_req = list(gate.get("missing_requirement_ids") or [])
            failed_gate = {
                "gate": key,
                "status": str(gate.get("status") or "failed"),
                "missing_requirement_ids": missing_req,
            }
            try:
                repair = _gate_repair_summary(key, gate)
            except Exception:
                repair = {}
            if repair:
                repair = _gate_repair_with_cross_ref_context(key, repair, full_result)
                compact_repair = _compact_gate_repair_projection(repair)
                for field in (
                    "diagnosis",
                    "reasons",
                    "missing_fields",
                    "rejected_event_ids",
                    "relevant_event_ids",
                    "recommended_legal_action",
                    "suggested_event_kind",
                    "append_payload_hint",
                    "repair_view_hint",
                    "advisory_only",
                ):
                    value = compact_repair.get(field)
                    if value not in ("", [], {}, None):
                        failed_gate[field] = value
            failed_gates.append(failed_gate)

    # Route identity from route_context_gate
    route_ctx_gate = _mapping(full_result.get("route_context_gate"))
    route_identity: dict[str, Any] = {}
    for hash_key in ("route_context_hash", "prompt_contract_hash", "visible_injection_manifest_hash"):
        val = str(route_ctx_gate.get(hash_key) or full_result.get(hash_key) or "").strip()
        if val:
            route_identity[hash_key] = val

    summary: dict[str, Any] = {
        "ok": True,
        "can_close": can_close,
        "missing_event_kinds": missing_event_kinds,
        "failed_gates": failed_gates,
        "event_count": int(full_result.get("event_count") or 0),
    }
    repair_reasons = list(full_result.get("repair_reasons") or [])
    next_legal_actions = list(full_result.get("next_legal_actions") or [])
    if not_applicable and not repair_reasons:
        repair_reasons = [
            {
                "code": "timeline_gate_not_applicable",
                "reason": str(full_result.get("reason") or ""),
                "message": (
                    "This row is not applicable to ordinary MF timeline close; "
                    "can_close remains false."
                ),
                "next_legal_action": (
                    "Use the row's appropriate workflow or a separately audited "
                    "recovery path without fabricating close_ready evidence."
                ),
            }
        ]
        next_legal_actions = [repair_reasons[0]["next_legal_action"]]
    if repair_reasons:
        summary["repair_reasons"] = repair_reasons
    if next_legal_actions:
        summary["next_legal_actions"] = next_legal_actions
    for opt_key in ("project_id", "bug_id", "applicable"):
        if full_result.get(opt_key) is not None:
            summary[opt_key] = full_result[opt_key]
    if route_identity:
        summary["route_identity"] = route_identity
    if request_id:
        summary["request_id"] = request_id
    return summary


MF_REPAIR_GATE_KEYS = [
    "contract_gate",
    "route_context_gate",
    "finish_gate_projection",
    "lane_ownership_gate",
    "worker_graph_trace_gate",
    "independent_qa_gate",
    "contract_projection_gate",
    "post_verification_actions_gate",
    "blocker_resolution_gate",
    "cross_ref_gate",
    "approval_scope_gate",
    "command_disposition_gate",
    "close_timeline_startup_gate",
]


def _unique_compact_values(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for value in values:
        if value in ("", None):
            continue
        marker = json.dumps(value, sort_keys=True, default=str)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


def _event_ids_from_items(items: Any) -> list[Any]:
    event_ids: list[Any] = []
    for item in _list(items):
        if not isinstance(item, dict):
            continue
        event_id = item.get("id") or item.get("event_id")
        if event_id not in ("", None):
            event_ids.append(event_id)
    return _unique_compact_values(event_ids)


def _repair_append_skeleton(
    event_kind: str,
    *,
    phase: str = "repair",
    payload: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    status: str = "accepted",
) -> dict[str, Any]:
    skeleton: dict[str, Any] = {
        "event_type": f"mf.{event_kind}",
        "event_kind": event_kind,
        "phase": phase,
        "status": status,
        "advisory_only": True,
        "close_satisfying_by_itself": False,
    }
    if payload is not None:
        skeleton["payload"] = payload
    if verification is not None:
        skeleton["verification"] = verification
    return skeleton


def _generic_repair_append_skeleton(gate_key: str, missing: list[str]) -> dict[str, Any]:
    first_missing = missing[0] if missing else "<required_evidence_id>"
    if gate_key == "contract_gate":
        return _repair_append_skeleton(
            "verification",
            phase="verification",
            verification={
                "contract_evidence": [
                    {"requirement_id": first_missing, "status": "passed"}
                ]
            },
        )
    if gate_key == "route_context_gate":
        return _repair_append_skeleton(
            "route_context",
            phase="dispatch",
            payload={
                "route_context": {
                    "route_context_hash": "<canonical_route_context_hash>",
                    "prompt_contract_hash": "<canonical_prompt_contract_hash>",
                    "visible_injection_manifest_hash": "<canonical_visible_manifest_hash>",
                }
            },
        )
    if gate_key == "finish_gate_projection":
        return _repair_append_skeleton(
            "mf_subagent_finish_gate",
            phase="handoff_gate",
            payload={
                "close_ready": True,
                "review_ready": True,
                "observer_command_id": "<originating_observer_command_id>",
                "commit_sha": "<implementation_commit_sha>",
                "changed_files": [],
                "route_prompt_contract": {
                    "route_context_hash": "<canonical_route_context_hash>",
                    "prompt_contract_id": "<canonical_prompt_contract_id>",
                },
                "startup_evidence": {
                    "runtime_context_id": "<mfrctx-...>",
                    "task_id": "<worker_task_id>",
                    "parent_task_id": "<backlog_id>",
                    "worker_slot_id": "<worker_slot_id>",
                    "fence_token": "<fence_token>",
                },
            },
        )
    if gate_key == "lane_ownership_gate":
        return _repair_append_skeleton(
            "bounded_implementation_worker_dispatch",
            phase="dispatch",
            payload={"worker_task_id": "<mf_sub_task_id>", "owned_files": []},
        )
    if gate_key == "worker_graph_trace_gate":
        return _repair_append_skeleton(
            "worker_graph_trace",
            phase="verification",
            payload={"graph_trace_ids": ["<gqt-...>"]},
        )
    if gate_key == "independent_qa_gate":
        return _repair_append_skeleton(
            "qa_verification",
            phase="verification",
            verification={
                "reviewer": "<independent_reviewer_id>",
                "contract_evidence": [
                    {
                        "requirement_id": first_missing,
                        "status": "passed",
                        "reviewer_role": "qa",
                    }
                ],
            },
        )
    if gate_key == "contract_projection_gate":
        return _repair_append_skeleton(
            "mf_subagent_read_receipt",
            phase="startup",
            payload={"read_receipt_hash": "<runtime_context_read_receipt_hash>"},
        )
    if gate_key == "post_verification_actions_gate":
        return _repair_append_skeleton(
            "post_verification_action",
            phase="verification",
            payload={"action_id": first_missing, "status": "completed"},
        )
    if gate_key == "blocker_resolution_gate":
        return _repair_append_skeleton(
            "blocker_resolution",
            phase="judge_review",
            payload={"resolution": "<judge_accepted_resolution>"},
            verification={"reviewer": "<independent_judge_id>"},
        )
    if gate_key == "approval_scope_gate":
        return _repair_append_skeleton(
            "close_approval",
            phase="close",
            payload={"approval_text": "approved for backlog_close after review"},
        )
    if gate_key == "command_disposition_gate":
        return _repair_append_skeleton(
            "observer_command_disposition",
            phase="close",
            payload={"command_id": "<originating_observer_command_id>", "status": "completed"},
        )
    if gate_key == "close_timeline_startup_gate":
        return _repair_append_skeleton(
            "mf_subagent_startup",
            phase="startup",
            payload={
                "worker_session_id": "<actual_host_worker_session_id>",
                "worker_transcript_ref": "<host_transcript_ref>",
                "worker_transcript_path": "<host_transcript_path_if_available>",
                "harness_type": "codex",
                "filer_principal": "<same_as_worker_session_id>",
                "runtime_context_id": "<mfrctx-...>",
                "route_token_ref": "<rtok-...>",
            },
        )
    return _repair_append_skeleton(
        "repair_evidence",
        payload={"gate": gate_key, "requirement_id": first_missing},
    )


def _finish_gate_facade_payload_skeleton() -> dict[str, Any]:
    path = (
        "/api/graph-governance/{project_id}/runtime-contexts/"
        "{runtime_context_id}/finish-gate"
    )
    return {
        "schema_version": "runtime_context.finish_gate_submission.repair_skeleton.v1",
        "action": "record_finish_gate",
        "name": "record_finish_gate",
        "next_legal_action": "record_finish_gate",
        "method": "POST",
        "endpoint": "runtime_context.finish_gate",
        "path": path,
        "body": {
            "runtime_context_id": "<same_mfrctx_as_startup_and_attestation>",
            "task_id": "<same_worker_task_id_as_startup_and_attestation>",
            "parent_task_id": "<same_parent_task_id_as_startup_and_attestation>",
            "fence_token": "<assigned_lane_fence_token>",
            "session_token": "<current_runtime_context_session_token>",
            "target_project_root": "<assigned_worker_worktree_or_project_root>",
            "checkpoint_id": "<finish-gate-checkpoint-id>",
            "head_commit": "<worker_worktree_head_commit>",
            "changed_files": ["<owned-file>"],
            "status": "review_ready",
            "test_results": {"status": "passed", "passed": True},
            "graph_trace_ids": ["<worker_owned_gqt_id>"],
            "read_receipt_event_id": "<accepted_read_receipt_event_id>",
            "read_receipt_hash": "<accepted_read_receipt_hash>",
            "finish_time_worker_self_attestation": (
                "<accepted_finish_time_worker_self_attestation>"
            ),
        },
        "reminders": {
            "canonical_finish_gate_required": True,
            "raw_finish_time_attestation_alone_close_satisfying": False,
            "use_runtime_context_facade": True,
        },
    }


def _startup_identity_missing_fields(gate: dict[str, Any]) -> list[str]:
    missing = _list(gate.get("missing_fields") or gate.get("missing_required_fields") or [])
    blocker_values: list[Any] = [
        *_list(gate.get("blockers")),
        *_list(_mapping(gate.get("worker_self_attestation")).get("blockers")),
        *_list(_mapping(gate.get("startup_worker_identity_gate")).get("blockers")),
        *_list(_mapping(gate.get("worker_identity_gate")).get("blockers")),
    ]
    for item in _list(gate.get("demoted_startup_events")):
        event_ref = _mapping(item)
        worker_gate = _mapping(event_ref.get("worker_self_attestation_gate"))
        startup_gate = _mapping(event_ref.get("startup_worker_identity_gate"))
        attestation = _mapping(worker_gate.get("attestation"))
        blocker_values.extend(_list(event_ref.get("worker_self_attestation_blockers")))
        blocker_values.extend(_list(worker_gate.get("blockers")))
        blocker_values.extend(_list(worker_gate.get("finish_time_blockers")))
        blocker_values.extend(_list(attestation.get("blockers")))
        blocker_values.extend(_list(attestation.get("finish_time_blockers")))
        blocker_values.extend(_list(startup_gate.get("blockers")))
    blockers = _unique_compact_values(blocker_values)
    blocker_to_field = {
        "missing_worker_session_id": "worker_session_id",
        "missing_worker_transcript_path": "worker_transcript_path",
        "missing_worker_transcript_ref_or_path": "worker_transcript_ref_or_worker_transcript_path",
        "missing_or_unsupported_harness_type": "harness_type",
        "unsupported_or_missing_harness_type": "harness_type",
        "graph_trace_ids_not_db_verified": "graph_trace_ids",
        "missing_mf_subagent_graph_trace_ids": "graph_trace_ids",
        "missing_graph_trace_ids": "graph_trace_ids",
        "graph_trace_evidence_missing": "graph_trace_ids",
        "missing_worker_self_attestation": "finish_time_worker_self_attestation",
        "missing_finish_time_worker_self_attestation": "finish_time_worker_self_attestation",
        "worker_self_attestation_not_passed": "finish_time_worker_self_attestation",
        "finish_time_self_attestation_not_passed": "finish_time_worker_self_attestation",
        "finish_time_blockers_present": "finish_time_worker_self_attestation",
        "attestation_phase_startup_not_finish": "finish_time_worker_self_attestation",
    }
    for blocker in blockers:
        field = blocker_to_field.get(str(blocker))
        if field:
            missing.append(field)
    return _unique_compact_values([str(item) for item in missing if str(item or "").strip()])


def _cross_ref_repair_diagnosis(
    rejected: list[dict[str, Any]],
) -> tuple[str, list[str], str]:
    if not rejected:
        return (
            "missing lineage bridge",
            [
                "Cross-reference gate failed without rejected evidence details; inspect protected close evidence for a missing bridge or stale lineage."
            ],
            "lineage_bridge",
        )

    mismatch_fields: set[str] = set()
    route_token_lineages: list[dict[str, Any]] = []
    for item in rejected:
        mismatch_fields.update(_mapping(item.get("mismatches")).keys())
        lineage = _mapping(item.get("route_token_child_lineage"))
        if lineage:
            route_token_lineages.append(lineage)

    if "task_id" in mismatch_fields and route_token_lineages:
        missing_fields = _unique_compact_values(
            [
                field
                for lineage in route_token_lineages
                for field in _list(lineage.get("missing_fields"))
                if str(field or "").strip()
            ]
        )
        reason = (
            "Rejected worker evidence declares route-token child lineage, but it is "
            "missing bridge-matching or registry-proof scope: " + ", ".join(missing_fields)
            if missing_fields
            else (
                "Rejected worker evidence declares route-token child lineage, but "
                "the lane is not registry-proven under the canonical parent route."
            )
        )
        return (
            "route-token child lineage scope mismatch",
            [
                reason,
                (
                    "A cross-ref bridge can document the lineage, but it is "
                    "advisory only; close still requires server-recorded worker "
                    "evidence with route_token_ref, parent_task_id, runtime_context_id, "
                    "worker_slot_id, observer command scope, canonical parent route "
                    "scope, and registry-backed active token proof."
                ),
            ],
            "cross_ref_lineage_bridge",
        )
    if "task_id" in mismatch_fields:
        return (
            "task_id mismatch; missing lineage bridge",
            [
                "Protected close evidence was recorded under a sibling task_id that is not bridged into the row's accepted lane set."
            ],
            "cross_ref_lineage_bridge",
        )
    if "backlog_id" in mismatch_fields:
        return (
            "backlog_id mismatch",
            [
                "Protected close evidence references a different backlog_id; remove or re-record the evidence for this row, or record an explicit lineage bridge if this is an intentional row lineage repair."
            ],
            "lineage_bridge",
        )
    if {"route_id", "prompt_contract_id"} & mismatch_fields:
        return (
            "route identity mismatch",
            [
                "Protected close evidence was recorded under a different route identity; re-record evidence under the canonical route or add accepted lineage evidence for the transition."
            ],
            "lineage_bridge",
        )
    if "scope" in mismatch_fields:
        return (
            "scope mismatch",
            [
                "Protected close evidence declares a different scope; re-record scope-correct evidence or bridge an intentional sibling lane."
            ],
            "cross_ref_lineage_bridge",
        )
    return (
        "rejected cross-ref evidence",
        ["Protected close evidence does not match the row identity and lacks accepted bridge evidence."],
        "lineage_bridge",
    )


def _cross_ref_append_payload_skeleton(
    gate: dict[str, Any],
    rejected: list[dict[str, Any]],
    suggested_event_kind: str,
) -> dict[str, Any]:
    row_identity = _mapping(gate.get("row_identity"))
    first = rejected[0] if rejected else {}
    first_lane = _mapping(first.get("lane_identity"))
    mismatches = _mapping(first.get("mismatches"))
    row_anchor = _mapping(gate.get("row_anchor"))
    canonical_parent_route_scope = _mapping(gate.get("canonical_route_scope"))
    child_route_scope = _mapping(first.get("event_route_scope"))
    command_scope = _mapping(first.get("command_scope"))
    route_token_lineage = _mapping(first.get("route_token_child_lineage"))
    expected_task = str(_mapping(mismatches.get("task_id")).get("expected") or "").strip()
    actual_task = str(_mapping(mismatches.get("task_id")).get("actual") or "").strip()

    bridged_identities: list[dict[str, Any]] = []
    base_identity = {
        "backlog_id": row_identity.get("backlog_id") or first_lane.get("backlog_id") or "<backlog_id>",
        "project_id": first_lane.get("project_id") or "<project_id>",
    }
    if expected_task:
        bridged_identities.append({**base_identity, "task_id": expected_task})
    else:
        bridged_identities.append({**base_identity, "task_id": "<row_or_root_task_id>"})
    if actual_task:
        bridged_identities.append({**base_identity, "task_id": actual_task})
    elif first_lane:
        bridged_identities.append({**base_identity, "task_id": first_lane.get("task_id") or "<sibling_task_id>"})
    else:
        bridged_identities.append({**base_identity, "task_id": "<sibling_task_id>"})

    payload: dict[str, Any] = {
        "bridge_reason": "repair_cross_ref_gate",
        "rejected_event_ids": _event_ids_from_items(rejected),
        "bridged_identities": bridged_identities,
    }
    if row_identity.get("backlog_id"):
        payload["backlog_id"] = row_identity["backlog_id"]
    for field in MF_CROSS_REF_BRIDGE_ROUTE_FIELDS:
        value = (
            row_identity.get(field)
            or canonical_parent_route_scope.get(field)
            or row_anchor.get(field)
        )
        if value:
            payload[field] = value
    bridge_command = (
        command_scope.get("row_command")
        or command_scope.get("observer_command_id")
        or row_anchor.get("command")
    )
    if bridge_command:
        payload["observer_command_id"] = bridge_command
    payload["bridge_scope"] = {
        "row_anchor": {
            key: value
            for key, value in row_anchor.items()
            if value not in ("", None)
        },
        "canonical_parent_route_scope": {
            key: value
            for key, value in canonical_parent_route_scope.items()
            if value not in ("", None)
        },
        "child_route_scope": {
            key: value
            for key, value in child_route_scope.items()
            if value not in ("", None)
        },
        "command_scope": {
            key: value
            for key, value in command_scope.items()
            if value not in ("", None)
        },
        "route_token_child_lineage": route_token_lineage,
        "registry_proof_required": route_token_lineage.get("registry_proof_required")
        or [
            "active_non_superseded_route_token_ref",
            "canonical_parent_backlog_id",
            "worker_task_id",
            "runtime_context_id",
            "fence_token",
            "worker_slot_id",
            "allowed_action",
            "canonical_parent_route_identity",
            "child_route_identity",
        ],
        "manual_bridge_advisory_only": True,
        "close_satisfying_worker_evidence_required": True,
        "advisory_only_reason": (
            "The bridge can explain lane lineage but does not replace "
            "route-token-bound worker dispatch/startup/implementation evidence "
            "or registry-backed active token proof."
        ),
    }
    if suggested_event_kind == "lineage_bridge" and rejected:
        actual_backlog = str(
            _mapping(_mapping(first.get("mismatches")).get("backlog_id")).get("actual") or ""
        ).strip()
        if actual_backlog:
            payload["bridged_backlog_id"] = actual_backlog

    return _repair_append_skeleton(
        suggested_event_kind,
        phase="lineage",
        payload=payload,
    )


def _gate_repair_with_cross_ref_context(
    gate_key: str,
    repair: dict[str, Any],
    full_result: dict[str, Any],
) -> dict[str, Any]:
    if gate_key != "route_context_gate":
        return repair
    missing = {str(item) for item in _list(repair.get("missing_requirement_ids"))}
    if not {"bounded_implementation_worker_dispatch", "mf_subagent_startup"} & missing:
        return repair
    cross_ref_gate = _mapping(full_result.get("cross_ref_gate"))
    if not cross_ref_gate or bool(cross_ref_gate.get("passed")):
        return repair
    rejected = [_mapping(item) for item in _list(cross_ref_gate.get("rejected_cross_ref_evidence"))]
    if not rejected:
        return repair

    diagnosis, cross_ref_reasons, _ = _cross_ref_repair_diagnosis(rejected)
    rejected_event_ids = _event_ids_from_items(rejected)
    event_label = ", ".join(str(item) for item in rejected_event_ids) or "the rejected worker events"
    next_reasons = [
        (
            "Worker close evidence appears in timeline event(s) "
            f"{event_label}, but it is not close-satisfying because cross_ref_gate "
            f"rejected it: {diagnosis}."
        ),
        *cross_ref_reasons,
        *[
            str(item)
            for item in _list(repair.get("reasons"))
            if str(item or "").strip()
        ],
    ]
    contextual = dict(repair)
    contextual["diagnosis"] = "worker evidence present but rejected by cross_ref_gate"
    contextual["reasons"] = _unique_compact_values(next_reasons)
    contextual["rejected_event_ids"] = _unique_compact_values(
        [*_list(repair.get("rejected_event_ids")), *rejected_event_ids]
    )
    contextual["relevant_event_ids"] = _unique_compact_values(
        [*_list(repair.get("relevant_event_ids")), *rejected_event_ids]
    )
    contextual["recommended_legal_action"] = (
        "Do not append route_context, close_ready, or an advisory bridge as a normal-close "
        "substitute. Re-run or re-record the bounded worker through the runtime context "
        "facades so dispatch/startup/implementation carry route_token_ref, parent_task_id, "
        "runtime_context_id, worker_slot_id, observer_command_id, canonical parent route "
        "scope, and registry-backed active token proof. If the implementation is already "
        "accepted but historical worker evidence cannot be legally reconstructed, use "
        "backlog_audit_archive as the audited WAIVED recovery path; do not claim can_close, "
        "close_ready, or reconstructed worker evidence."
    )
    contextual["suggested_event_kind"] = "bounded_worker_rerun"
    contextual.pop("append_payload_skeleton", None)
    return contextual


def _audit_archive_recovery_summary(
    full_result: dict[str, Any],
    *,
    request_id: str = "",
) -> dict[str, Any]:
    """Return the audited recovery path for rows that cannot normal-close."""

    if bool(full_result.get("passed") or full_result.get("can_close")):
        return {}

    missing_event_kinds = [
        str(item)
        for item in _list(full_result.get("missing_event_kinds"))
        if str(item or "").strip()
    ]
    failed_gates = []
    for key in MF_REPAIR_GATE_KEYS:
        gate = _mapping(full_result.get(key))
        if gate and not bool(gate.get("passed")):
            failed_gates.append(key)
    repair_reasons = _list(full_result.get("repair_reasons"))
    if not missing_event_kinds and not failed_gates and not repair_reasons:
        return {}

    timeline_precheck: dict[str, Any] = {
        "can_close": False,
        "missing_event_kinds": missing_event_kinds,
        "failed_gates": failed_gates,
    }
    if request_id:
        timeline_precheck["request_id"] = request_id

    return {
        "schema_version": "mf_audit_archive_recovery.v1",
        "recommended_legal_action": (
            "When the implementation is accepted but the normal MF close evidence is "
            "non-reconstructable, call backlog_audit_archive. This records a WAIVED "
            "audit archive and must not claim can_close, close_ready, startup, or worker "
            "evidence was reconstructed."
        ),
        "mcp_tool": "backlog_audit_archive",
        "http_method": "POST",
        "http_path": "/api/backlog/{project_id}/{bug_id}/audit-archive",
        "row_status": "WAIVED",
        "normal_close_gate_can_close": False,
        "close_satisfying_by_itself": False,
        "requires_independent_qa": True,
        "requires_non_reconstructable_evidence_reason": True,
        "body_skeleton": {
            "commit": "<implementation_commit_sha>",
            "reason": (
                "<why ordinary MF close cannot legally pass and why audit archive "
                "is being used>"
            ),
            "timeline_precheck": timeline_precheck,
            "failure_audit": {
                "what_happened": "<what happened during the failed normal close>",
                "non_reconstructable_evidence_reason": (
                    "<why startup/worker/close_ready evidence cannot be reconstructed "
                    "without fabricating append-only facts>"
                ),
                "historical_evidence_reconstructed": False,
                "startup_or_close_ready_backfilled": False,
            },
            "qa_acceptance": {
                "passed": True,
                "reviewer": "<independent_qa_reviewer>",
                "reviewer_role": "qa",
                "tests": ["<focused test command>"],
                "artifacts": ["<artifact or timeline ref>"],
            },
            "verification": {
                "tests": ["<focused test command>"],
                "artifacts": ["<artifact or timeline ref>"],
            },
            "graph_snapshot": {
                "snapshot_id": "<current_graph_snapshot_id>",
                "commit_sha": "<current_graph_commit_sha>",
            },
            "runtime_context": {
                "route_token_ref": "<route_token_ref for backlog_audit_archive>",
                "route_context_hash": "<route_context_hash or empty if unavailable>",
                "prompt_contract_id": "<prompt_contract_id or empty if unavailable>",
            },
            "actor": "observer",
        },
    }


def _compact_append_payload_hint(skeleton: dict[str, Any]) -> dict[str, Any]:
    if not skeleton:
        return {}
    payload = _mapping(skeleton.get("payload"))
    bridge_scope = _mapping(payload.get("bridge_scope"))
    lineage = _mapping(bridge_scope.get("route_token_child_lineage"))
    hint: dict[str, Any] = {
        "event_kind": skeleton.get("event_kind"),
        "advisory_only": bool(skeleton.get("advisory_only")),
        "close_satisfying_by_itself": bool(skeleton.get("close_satisfying_by_itself")),
    }
    for field in (
        "bridge_reason",
        "rejected_event_ids",
        "backlog_id",
        "observer_command_id",
    ):
        value = payload.get(field)
        if value not in ("", [], {}, None):
            hint[field] = value
    bridged = _list(payload.get("bridged_identities"))
    if bridged:
        hint["bridged_identity_count"] = len(bridged)
    missing_fields = _list(lineage.get("missing_fields"))
    if missing_fields:
        hint["missing_fields"] = _unique_compact_values(
            [str(item) for item in missing_fields if str(item or "").strip()]
        )
    registry_required = _list(bridge_scope.get("registry_proof_required"))
    if registry_required:
        hint["requires_registry_proof"] = True
    return {key: value for key, value in hint.items() if value not in ("", [], {}, None)}


def _compact_gate_repair_projection(repair: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for field in (
        "diagnosis",
        "reasons",
        "missing_fields",
        "rejected_event_ids",
        "relevant_event_ids",
        "recommended_legal_action",
        "suggested_event_kind",
        "advisory_only",
    ):
        value = repair.get(field)
        if value not in ("", [], {}, None):
            compact[field] = value
    hint = _compact_append_payload_hint(_mapping(repair.get("append_payload_skeleton")))
    if hint:
        compact["append_payload_hint"] = hint
        compact["repair_view_hint"] = (
            "Call mf_timeline_precheck(view='repair') for the advisory append payload skeleton."
        )
    facade = _mapping(repair.get("finish_gate_facade_payload_skeleton"))
    if facade:
        compact["finish_gate_facade_payload_hint"] = {
            "action": facade.get("action"),
            "method": facade.get("method"),
            "path": facade.get("path"),
            "endpoint": facade.get("endpoint"),
            "canonical_finish_gate_required": True,
        }
        compact["repair_view_hint"] = (
            "Call mf_timeline_precheck(view='repair') for the runtime-context "
            "finish-gate facade payload skeleton."
        )
    return compact


def _gate_repair_summary(gate_key: str, gate: dict[str, Any]) -> dict[str, Any]:
    missing = list(gate.get("missing_requirement_ids") or [])
    reasons: list[str] = []
    relevant_event_ids: list[Any] = []
    rejected_event_ids: list[Any] = []
    recommended_action = str(gate.get("next_action") or "").strip()
    suggested_event_kind = ""

    if gate_key == "close_timeline_startup_gate":
        startup_missing = _startup_identity_missing_fields(gate)
        demoted_events = [_mapping(item) for item in _list(gate.get("demoted_startup_events"))]
        rejected_event_ids = _event_ids_from_items(demoted_events)
        relevant_event_ids = list(rejected_event_ids)
        demoted_blockers = _unique_compact_values(
            [
                *[
                    blocker
                    for item in demoted_events
                    for blocker in _list(item.get("worker_self_attestation_blockers"))
                ],
                *[
                    blocker
                    for item in demoted_events
                    for blocker in _list(
                        _mapping(item.get("worker_self_attestation_gate")).get("blockers")
                    )
                ],
                *[
                    blocker
                    for item in demoted_events
                    for blocker in _list(
                        _mapping(
                            _mapping(item.get("worker_self_attestation_gate")).get(
                                "attestation"
                            )
                        ).get("blockers")
                    )
                ],
            ]
        )
        finish_time_fields = {
            "finish_time_worker_self_attestation",
            "graph_trace_ids",
        }
        needs_finish_time_projection = bool(
            finish_time_fields.intersection(set(startup_missing))
        )
        reasons = []
        if startup_missing:
            reasons.append(
                "Startup evidence is missing close-sensitive worker fields: "
                + ", ".join(startup_missing)
            )
        if rejected_event_ids:
            reasons.append(
                "Startup event ids rejected for close satisfaction: "
                + ", ".join(str(item) for item in rejected_event_ids)
            )
        if demoted_blockers:
            reasons.append(
                "Startup close blockers: "
                + ", ".join(str(item) for item in demoted_blockers)
            )
        for field in ("reason", "missing_reason", "failure_reason", "message"):
            value = str(gate.get(field) or "").strip()
            if value:
                reasons.append(value)
        if not reasons:
            reasons.append("Startup evidence is not close-satisfying.")
        if needs_finish_time_projection:
            recommended_action = (
                "An accepted finish-time worker attestation is an input to the "
                "finish gate, but it is not close-satisfying by itself. Record "
                "the canonical runtime-context finish-gate facade under the same "
                "runtime_context_id, task_id, worker_session_id, read receipt, "
                "route identity, and worker-owned graph_trace_ids so close can "
                "consume the mf_subagent_finish_gate projection. Do not record "
                "another startup, observer-backfill worker evidence, or use audit "
                "archive to pretend finish evidence exists. If accepted "
                "implementation evidence cannot normal-close because historical "
                "finish evidence is non-reconstructable, use backlog_audit_archive "
                "as WAIVED recovery with independent QA."
            )
            suggested_event_kind = "mf_subagent_finish_gate"
            reasons.append(
                "Accepted finish-time attestation alone remains non-close-satisfying "
                "until mf_subagent_finish_gate is recorded and validated."
            )
            append_skeleton = _repair_append_skeleton(
                suggested_event_kind,
                phase="finish_gate",
                payload={
                    "mf_subagent_finish_gate": {
                        "runtime_context_id": "<same_mfrctx_as_startup>",
                        "task_id": "<same_worker_task_id_as_startup>",
                        "parent_task_id": "<same_parent_task_id_as_startup>",
                        "startup_evidence": "<server_verified_startup_event_payload>",
                        "graph_trace_ids": ["<worker_owned_gqt_id>"],
                        "test_results": {"status": "passed", "passed": True},
                        "startup_worker_identity_gate": {"status": "passed", "passed": True},
                        "worker_self_attestation_gate": {
                            "status": "passed",
                            "passed": True,
                            "attestation": {
                                "attestation_phase": "finish",
                                "status": "passed",
                                "worker_self_attesting": True,
                                "finish_time_self_attesting": True,
                                "finish_time_blockers": [],
                                "worker_session_id": "<same_worker_session_id_as_startup>",
                                "filer_principal": "<same_worker_session_id>",
                                "worker_transcript_ref": "<host_transcript_ref>",
                                "harness_type": "codex",
                            },
                        },
                    },
                },
            )
            finish_gate_facade_skeleton = _finish_gate_facade_payload_skeleton()
            diagnosis = "startup evidence needs later worker finish-time proof"
        else:
            recommended_action = (
                "Record a new worker-authored mf_subagent_startup through the "
                "runtime/parallel startup facade with worker_session_id, "
                "worker_transcript_ref or worker_transcript_path, harness_type, "
                "and filer_principal. Do not observer-backfill or use audit archive "
                "to pretend startup evidence exists. If accepted implementation "
                "evidence cannot normal-close because startup evidence is "
                "non-reconstructable, use backlog_audit_archive as WAIVED recovery "
                "with independent QA."
            )
            suggested_event_kind = "mf_subagent_startup"
            append_skeleton = _generic_repair_append_skeleton(
                gate_key,
                startup_missing or missing,
            )
            finish_gate_facade_skeleton = {}
            diagnosis = "startup evidence missing real worker identity fields"
        result = {
            "gate": gate_key,
            "failed_gate_name": gate_key,
            "status": str(gate.get("status") or "failed"),
            "missing_requirement_ids": missing,
            "missing_fields": startup_missing,
            "diagnosis": diagnosis,
            "reasons": _unique_compact_values(reasons),
            "rejected_event_ids": rejected_event_ids,
            "relevant_event_ids": relevant_event_ids,
            "recommended_legal_action": recommended_action,
            "suggested_event_kind": suggested_event_kind,
            "append_payload_skeleton": append_skeleton,
            "advisory_only": True,
        }
        if finish_gate_facade_skeleton:
            result["finish_gate_facade_payload_skeleton"] = finish_gate_facade_skeleton
        return result

    if gate_key == "cross_ref_gate":
        rejected = [_mapping(item) for item in _list(gate.get("rejected_cross_ref_evidence"))]
        diagnosis, reasons, suggested_event_kind = _cross_ref_repair_diagnosis(rejected)
        rejected_event_ids = _event_ids_from_items(rejected)
        relevant_event_ids = list(rejected_event_ids)
        recommended_action = (
            "Prefer route-token canonical child lineage: re-record rejected worker "
            "evidence with route_token_ref, parent_task_id/root backlog, runtime_context_id, "
            "worker_slot_id, observer command scope, canonical parent route scope, "
            "and registry-backed active token proof. If the rejected worker evidence "
            "is finish or handoff evidence and the lane can still legally act, prefer "
            "re-recording through the canonical runtime-context finish-gate facade "
            "before using a manual lineage bridge. "
            "A cross-ref bridge skeleton is advisory and is not close-satisfying "
            "worker evidence by itself."
        )
        append_payload_skeleton = _cross_ref_append_payload_skeleton(
            gate,
            rejected,
            suggested_event_kind,
        )
        return {
            "gate": gate_key,
            "failed_gate_name": gate_key,
            "status": str(gate.get("status") or "failed"),
            "missing_requirement_ids": missing,
            "diagnosis": diagnosis,
            "reasons": reasons,
            "rejected_event_ids": rejected_event_ids,
            "relevant_event_ids": relevant_event_ids,
            "recommended_legal_action": recommended_action,
            "suggested_event_kind": suggested_event_kind,
            "append_payload_skeleton": append_payload_skeleton,
            "advisory_only": True,
        }

    for field in ("reason", "missing_reason", "failure_reason", "message"):
        value = str(gate.get(field) or "").strip()
        if value:
            reasons.append(value)
    if missing:
        reasons.append("Missing required evidence: " + ", ".join(missing))
    missing_fields = [
        str(item)
        for item in _list(gate.get("missing_fields"))
        if str(item or "").strip()
    ]
    if missing_fields:
        reasons.append("Missing fields: " + ", ".join(missing_fields))
    rejected_sources = {
        "rejected_evidence_events": gate.get("rejected_evidence_events"),
        "rejected_observer_resolutions": gate.get("rejected_observer_resolutions"),
        "approvals_excluding_close": gate.get("approvals_excluding_close"),
        "blocking_commands": gate.get("blocking_commands"),
        "missing_actions": gate.get("missing_actions"),
    }
    for items in rejected_sources.values():
        ids = _event_ids_from_items(items)
        rejected_event_ids.extend(ids)
        relevant_event_ids.extend(ids)
    if not reasons:
        reasons.append(f"{gate_key} status is {str(gate.get('status') or 'failed')}")
    if not recommended_action:
        recommended_action = "Record valid timeline evidence for this gate, then rerun the precheck."
    return {
        "gate": gate_key,
        "failed_gate_name": gate_key,
        "status": str(gate.get("status") or "failed"),
        "missing_requirement_ids": missing,
        "missing_fields": missing_fields,
        "reasons": _unique_compact_values(reasons),
        "rejected_event_ids": _unique_compact_values(rejected_event_ids),
        "relevant_event_ids": _unique_compact_values(relevant_event_ids),
        "recommended_legal_action": recommended_action,
        "append_payload_skeleton": _generic_repair_append_skeleton(gate_key, missing),
        "advisory_only": True,
    }


def repair_gate_summary(
    full_result: dict[str, Any],
    request_id: str = "",
) -> dict[str, Any]:
    """Project failed MF close gates into compact, advisory repair steps."""

    status = str(full_result.get("status") or "").strip().lower()
    not_applicable = full_result.get("applicable") is False or status == "not_applicable"
    passed = bool(full_result.get("passed") or full_result.get("can_close"))
    if not_applicable:
        passed = False
    missing_event_kinds = list(full_result.get("missing_event_kinds") or [])
    failed_gate_repairs = []
    for key in MF_REPAIR_GATE_KEYS:
        gate = _mapping(full_result.get(key))
        if not gate or bool(gate.get("passed")):
            continue
        repair = _gate_repair_summary(key, gate)
        failed_gate_repairs.append(
            _gate_repair_with_cross_ref_context(key, repair, full_result)
        )

    missing_event_repairs = [
        {
            "event_kind": kind,
            "recommended_legal_action": f"Record accepted {kind} evidence for this MF row.",
            "append_payload_skeleton": _repair_append_skeleton(
                kind,
                phase="close" if kind == "close_ready" else kind,
                payload={"requirement_id": kind},
            ),
            "advisory_only": True,
        }
        for kind in missing_event_kinds
    ]

    summary: dict[str, Any] = {
        "schema_version": MF_TIMELINE_REPAIR_SUMMARY_SCHEMA_VERSION,
        "ok": True,
        "can_close": passed,
        "advisory_only": True,
        "missing_event_kinds": missing_event_kinds,
        "missing_event_repairs": missing_event_repairs,
        "failed_gate_repairs": failed_gate_repairs,
        "failed_gate_count": len(failed_gate_repairs),
        "event_count": int(full_result.get("event_count") or 0),
    }
    repair_reasons = list(full_result.get("repair_reasons") or [])
    next_legal_actions = list(full_result.get("next_legal_actions") or [])
    if not_applicable and not repair_reasons:
        repair_reasons = [
            {
                "code": "timeline_gate_not_applicable",
                "reason": str(full_result.get("reason") or ""),
                "message": (
                    "This row is not applicable to ordinary MF timeline close; "
                    "can_close remains false."
                ),
                "next_legal_action": (
                    "Use the row's appropriate workflow or a separately audited "
                    "recovery path without fabricating close_ready evidence."
                ),
            }
        ]
        next_legal_actions = [repair_reasons[0]["next_legal_action"]]
    if repair_reasons:
        summary["repair_reasons"] = repair_reasons
    if next_legal_actions:
        summary["next_legal_actions"] = next_legal_actions
    audit_archive_recovery = _audit_archive_recovery_summary(
        full_result,
        request_id=request_id,
    )
    if audit_archive_recovery:
        summary["audit_archive_recovery"] = audit_archive_recovery
    for opt_key in ("project_id", "bug_id", "applicable"):
        if full_result.get(opt_key) is not None:
            summary[opt_key] = full_result[opt_key]
    if request_id:
        summary["request_id"] = request_id
    return summary


def _observer_command_route_identity(payload: dict[str, Any] | None) -> dict[str, str]:
    source = _mapping(payload)
    identity = {
        "route_id": str(source.get("route_id") or "").strip(),
        "route_context_hash": str(source.get("route_context_hash") or "").strip(),
        "prompt_contract_id": str(source.get("prompt_contract_id") or "").strip(),
        "prompt_contract_hash": str(source.get("prompt_contract_hash") or "").strip(),
        "visible_injection_manifest_hash": str(
            source.get("visible_injection_manifest_hash") or ""
        ).strip(),
    }
    return {key: value for key, value in identity.items() if value}


def _observer_command_close_evidence_root(
    result_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    result = _mapping(result_payload)
    for key in (
        "canonical_close_evidence",
        "canonical_contract_close_evidence",
        "contract_close_projection",
        "task_contract_close_projection",
    ):
        value = _mapping(result.get(key))
        if value:
            return value
    return result


def _observer_command_terminal_events(
    evidence: dict[str, Any],
    result_payload: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    result = _mapping(result_payload)
    for source in (evidence, result):
        for key in ("timeline_events", "events", "task_timeline_events"):
            values = [_mapping(item) for item in _list(source.get(key))]
            values = [item for item in values if item]
            if values:
                return values
        for key in (
            "mf_timeline_precheck",
            "timeline_precheck",
            "timeline_gate_precheck",
            "timeline_gate_result",
        ):
            nested = _mapping(source.get(key))
            for nested_key in ("timeline_events", "events", "task_timeline_events"):
                values = [_mapping(item) for item in _list(nested.get(nested_key))]
                values = [item for item in values if item]
                if values:
                    return values
    return []


def _observer_command_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


_OBSERVER_COMMAND_CLOSED_BACKLOG_STATUSES = {
    "fixed",
    "closed",
    "complete",
    "completed",
    "done",
}

_OBSERVER_COMMAND_AUTHORITATIVE_CLOSE_GATE_SCHEMAS = {
    "mf_close_timeline_gate.v1",
}

_OBSERVER_COMMAND_AUTHORITATIVE_TIMELINE_SOURCE_KEYS = {
    "mf_timeline_precheck",
    "timeline_precheck",
    "timeline_gate",
    "timeline_gate_precheck",
    "timeline_gate_result",
}

_OBSERVER_COMMAND_AUTHORITATIVE_TIMELINE_SOURCE_TOKENS = {
    "mf_close_timeline_gate",
    "mf_timeline_precheck",
    "timeline_precheck",
    "timeline_gate",
    "timeline_gate_precheck",
    "timeline_gate_result",
}


def _observer_command_close_status(source: dict[str, Any]) -> str:
    return (
        str(
            source.get("backlog_status")
            or source.get("new_status")
            or source.get("bug_status")
            or ""
        )
        .strip()
        .lower()
    )


def _observer_command_explicit_close_gate_source(
    source_name: str,
    source: dict[str, Any],
) -> bool:
    schema = str(source.get("schema_version") or "").strip()
    if schema in _OBSERVER_COMMAND_AUTHORITATIVE_CLOSE_GATE_SCHEMAS:
        return True
    source_parts = {
        _normalize_token(part)
        for part in source_name.split(".")
        if str(part or "").strip()
    }
    if source_parts & _OBSERVER_COMMAND_AUTHORITATIVE_TIMELINE_SOURCE_KEYS:
        return True
    for key in ("source", "gate_source", "evidence_source", "precheck_source", "kind"):
        token = _normalize_token(source.get(key))
        if token in _OBSERVER_COMMAND_AUTHORITATIVE_TIMELINE_SOURCE_TOKENS:
            return True
    return False


def _observer_command_authoritative_backlog_close_source(
    source_name: str,
    source: dict[str, Any],
) -> bool:
    source_parts = {
        _normalize_token(part)
        for part in source_name.split(".")
        if str(part or "").strip()
    }
    return bool(
        source_parts & {"backlog_close", "backlog_close_result", "close_result"}
        and _truthy(source.get("ok"))
        and _observer_command_close_status(source)
        in _OBSERVER_COMMAND_CLOSED_BACKLOG_STATUSES
    )


def _observer_command_close_gate_passed_source(
    evidence: dict[str, Any],
    result_payload: dict[str, Any] | None,
    close_gate: dict[str, Any],
) -> dict[str, Any]:
    result = _mapping(result_payload)
    candidates: list[tuple[str, dict[str, Any]]] = []
    if close_gate:
        candidates.append(("close_gate", close_gate))
    roots: list[tuple[str, dict[str, Any]]]
    if evidence is result:
        roots = [("result", result)]
    else:
        roots = [("canonical_close_evidence", evidence), ("result", result)]
    for root_label, root in roots:
        if not root:
            continue
        if _observer_command_explicit_close_gate_source(root_label, root):
            candidates.append((root_label, root))
        for key in (
            "close_gate",
            "timeline_gate",
            "mf_timeline_precheck",
            "timeline_precheck",
            "timeline_gate_precheck",
            "timeline_gate_result",
            "repair_summary",
            "backlog_close",
            "backlog_close_result",
            "close_result",
        ):
            nested = _mapping(root.get(key))
            if nested:
                candidates.append((f"{root_label}.{key}", nested))
                repair_summary = _mapping(nested.get("repair_summary"))
                if repair_summary:
                    candidates.append((f"{root_label}.{key}.repair_summary", repair_summary))

    for source_name, source in candidates:
        status = str(source.get("status") or "").strip().lower()
        if (
            _observer_command_explicit_close_gate_source(source_name, source)
            and status != "failed"
            and (bool(source.get("passed")) or _truthy(source.get("can_close")))
        ):
            return {"source": source_name, "status": status or "passed"}
        if _observer_command_authoritative_backlog_close_source(source_name, source):
            return {
                "source": source_name,
                "status": status or "passed",
                "backlog_status": str(
                    source.get("backlog_status")
                    or source.get("new_status")
                    or source.get("bug_status")
                    or ""
                ),
                "request_id": str(source.get("request_id") or ""),
            }
    return {}


def _observer_command_event_ref_from_value(kind: str, value: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    if isinstance(value, Mapping):
        ref_id = value.get("id") or value.get("event_id")
        if ref_id not in (None, ""):
            ref = {
                "event_kind": str(value.get("event_kind") or kind),
                "status": str(value.get("status") or value.get("decision") or "accepted"),
            }
            parsed_id = _observer_command_int(ref_id)
            if parsed_id is not None:
                ref["id"] = parsed_id
            else:
                ref["ref"] = str(ref_id)
            refs.append(ref)
        return refs
    if isinstance(value, (list, tuple)):
        for item in value:
            refs.extend(_observer_command_event_ref_from_value(kind, item))
        return refs
    text = str(value or "").strip()
    if not text:
        return refs
    if text.startswith("timeline:"):
        parsed_id = _observer_command_int(text.split(":", 1)[1])
    else:
        parsed_id = _observer_command_int(text)
    ref: dict[str, Any] = {"event_kind": kind, "status": "accepted"}
    if parsed_id is not None:
        ref["id"] = parsed_id
    else:
        ref["ref"] = text
    refs.append(ref)
    return refs


def _observer_command_compact_event_refs(
    evidence: dict[str, Any],
    result_payload: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    result = _mapping(result_payload)
    refs: list[dict[str, Any]] = []
    key_to_kind = {
        "accepted_close_ready": "close_ready",
        "close_ready": "close_ready",
        "close_ready_event_id": "close_ready",
        "post_merge_verification": "verification",
        "accepted_verification": "verification",
        "verification": "verification",
        "verification_event_id": "verification",
        "backlog_close": "backlog_close",
        "backlog_close_event_id": "backlog_close",
        "route_identity_cleanup": "route_identity_cleanup",
        "route_identity_cleanup_event_id": "route_identity_cleanup",
    }
    for root in (evidence, result):
        if not root:
            continue
        timeline_events = _mapping(root.get("timeline_events"))
        for key, value in timeline_events.items():
            kind = key_to_kind.get(str(key), str(key))
            refs.extend(_observer_command_event_ref_from_value(kind, value))
        for key, kind in key_to_kind.items():
            if key in root:
                refs.extend(_observer_command_event_ref_from_value(kind, root.get(key)))
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        ref_key = json.dumps(ref, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if ref_key in seen:
            continue
        seen.add(ref_key)
        deduped.append(ref)
    return deduped


def _observer_command_has_accepted_close_ready(
    *,
    close_gate: dict[str, Any],
    events: list[dict[str, Any]],
) -> bool:
    if "close_ready" in set(close_gate.get("present_event_kinds") or []):
        return True
    for event in events:
        kind = str(event.get("event_kind") or event.get("event_type") or "").strip()
        status = str(event.get("status") or event.get("decision") or "").strip().lower()
        if kind == "close_ready" and status in MF_CLOSE_PASS_STATUSES:
            return True
    return False


def _observer_command_public_route_identity(value: Any) -> dict[str, str]:
    identity = _route_identity(value)
    if not identity:
        return {}
    for field in ("route_id", "visible_injection_manifest_hash", "route_token_ref"):
        token = _first_deep_text(value, field)
        if token:
            identity[field] = token
    return identity


def _observer_command_canonical_route_identity(
    *,
    evidence: dict[str, Any],
    result_payload: dict[str, Any] | None,
    route_context_gate: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, str]:
    result = _mapping(result_payload)
    candidates: list[dict[str, str]] = []

    def add(value: Any) -> None:
        identity = _observer_command_public_route_identity(value)
        if identity:
            candidates.append(identity)

    add(evidence.get("canonical_route_identity"))
    add(route_context_gate.get("route_identity"))
    add(_mapping(route_context_gate.get("route_identity_cleanup")).get("route_identity"))
    for event in sorted(
        events,
        key=lambda item: _observer_command_int(item.get("id") or item.get("event_id")) or 0,
        reverse=True,
    ):
        kind = str(event.get("event_kind") or event.get("event_type") or "")
        if kind == "route_identity_cleanup":
            add(event)
    for root in (evidence, result):
        for key in (
            "canonical_route_identity",
            "observer_claim_evidence",
            "durable_mf_sub_evidence",
            "route_token_gate",
            "close_gate",
            "backlog_close",
            "backlog_close_result",
        ):
            add(_mapping(root.get(key)))

    return candidates[0] if candidates else {}


def _observer_command_missing_evidence_details(
    missing: list[str],
    *,
    close_gate_source: dict[str, Any],
    compact_refs: list[dict[str, Any]],
    events: list[dict[str, Any]],
    command_identity: dict[str, str],
    canonical_identity: dict[str, str],
) -> list[dict[str, Any]]:
    event_refs = [_observer_command_event_ref(event) for event in events]
    event_refs = [ref for ref in event_refs if ref]
    details: list[dict[str, Any]] = []
    for requirement_id in missing:
        detail: dict[str, Any] = {
            "requirement_id": requirement_id,
            "actionable": True,
        }
        if requirement_id == "canonical_close_gate_passed":
            detail.update({
                "expected": "mf_timeline_precheck.can_close true or mf_close_timeline_gate passed",
                "observed_close_gate_source": close_gate_source,
                "next_action": "rerun mf_timeline_precheck(view=repair) and include its gate/events in observer_command_complete",
            })
        elif requirement_id == "accepted_close_ready":
            detail.update({
                "expected_event_kind": "close_ready",
                "observed_event_refs": [
                    ref for ref in compact_refs + event_refs if ref.get("event_kind") == "close_ready"
                ],
                "next_action": "include the accepted close_ready timeline event or an authoritative timeline gate that already verified it",
            })
        elif requirement_id == "canonical_backlog_fixed_or_closed":
            detail.update({
                "expected": "backlog_close ok with backlog status FIXED/CLOSED/DONE",
                "next_action": "include backlog_close response with final backlog status",
            })
        elif requirement_id == "canonical_route_identity":
            detail.update({
                "expected_fields": list(MF_ROUTE_IDENTITY_FIELDS),
                "observed_event_refs": [
                    ref
                    for ref in compact_refs + event_refs
                    if ref.get("event_kind") in {"route_identity_cleanup", "route_context", "route_token_gate"}
                ],
                "next_action": "include canonical_route_identity or route_identity_cleanup/route-token evidence",
            })
        elif requirement_id == "superseding_route_or_contract_relation":
            detail.update({
                "command_route_identity": command_identity,
                "canonical_route_identity": canonical_identity,
                "observed_event_refs": [
                    ref
                    for ref in compact_refs + event_refs
                    if ref.get("event_kind") == "route_identity_cleanup"
                ],
                "next_action": "include accepted route_identity_cleanup or parent-child route lineage evidence",
            })
        details.append(detail)
    return details


def _observer_command_backlog_close_state(
    evidence: dict[str, Any],
    result_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    result = _mapping(result_payload)
    close = {}
    for source in (evidence, result):
        for key in ("backlog_close", "backlog_close_result", "close_result"):
            close = _mapping(source.get(key))
            if close:
                break
        if close:
            break
    status = (
        _first_deep_text(close, "backlog_status")
        or _first_deep_text(close, "new_status")
        or _first_deep_text(close, "bug_status")
        or _first_deep_text(evidence, "backlog_status")
        or _first_deep_text(evidence, "canonical_backlog_status")
    )
    request_id = (
        _first_deep_text(close, "request_id")
        or _first_deep_text(close, "backlog_close_request_id")
        or _first_deep_text(evidence, "backlog_close_request_id")
    )
    closed = status.strip().lower() in {
        "fixed",
        "closed",
        "complete",
        "completed",
        "done",
    } or _truthy(
        close.get("backlog_closed") or close.get("closed") or evidence.get("backlog_closed")
    )
    return {
        "status": status,
        "request_id": request_id,
        "closed": closed,
        "evidence": close,
    }


def _observer_command_identity_matches(
    left: dict[str, str],
    right: dict[str, str],
) -> bool:
    if not left or not right:
        return False
    for field in MF_ROUTE_IDENTITY_FIELDS:
        if left.get(field, "") != right.get(field, ""):
            return False
    left_prompt_hash = left.get("prompt_contract_hash", "")
    right_prompt_hash = right.get("prompt_contract_hash", "")
    return not left_prompt_hash or not right_prompt_hash or left_prompt_hash == right_prompt_hash


def _observer_command_supersession_relation_present(
    *,
    evidence: dict[str, Any],
    route_context_gate: dict[str, Any],
    command_identity: dict[str, str],
    canonical_identity: dict[str, str],
) -> bool:
    if _observer_command_identity_matches(command_identity, canonical_identity):
        return True
    cleanup = _mapping(route_context_gate.get("route_identity_cleanup"))
    if cleanup.get("applied") and int(cleanup.get("superseded_event_count") or 0) > 0:
        return True
    for key in (
        "route_identity_supersession",
        "route_identity_supersede",
        "route_identity_cleanup",
        "route_identity_reconciliation",
        "superseding_route_relation",
    ):
        relation = _mapping(evidence.get(key))
        if not relation:
            continue
        status = str(
            relation.get("status") or relation.get("decision") or relation.get("state") or ""
        ).strip().lower()
        if status in {"accepted", "passed", "reconciled", "superseded"} or _truthy(
            relation.get("accepted") or relation.get("reconciled")
        ):
            return True
    return False


def _observer_command_event_ref(event: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "id": event.get("id") or event.get("event_id"),
            "event_kind": event.get("event_kind"),
            "phase": event.get("phase"),
            "status": event.get("status") or event.get("decision"),
            "request_id": event.get("request_id"),
        }.items()
        if value not in (None, "")
    }


def _observer_command_terminal_evidence_refs(
    events: list[dict[str, Any]],
    route_context_gate: dict[str, Any],
    backlog_close_state: dict[str, Any],
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for event in events:
        kind = str(event.get("event_kind") or event.get("event_type") or "").strip()
        if kind in {
            "implementation",
            "verification",
            "close_ready",
            "backlog_close",
            "route_identity_cleanup",
        }:
            ref = _observer_command_event_ref(event)
            if ref:
                refs.append(ref)
    evidence_events = _mapping(route_context_gate.get("evidence_events"))
    for values in evidence_events.values():
        for event_ref in _list(values):
            ref = _mapping(event_ref)
            if ref:
                refs.append(ref)
    cleanup_event = _mapping(_mapping(route_context_gate.get("route_identity_cleanup")).get("event"))
    if cleanup_event:
        refs.append(cleanup_event)
    request_id = str(backlog_close_state.get("request_id") or "").strip()
    if request_id:
        refs.append({
            "event_kind": "backlog_close",
            "request_id": request_id,
            "status": str(backlog_close_state.get("status") or ""),
        })

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for ref in refs:
        key = json.dumps(ref, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ref)
    return deduped


def observer_command_terminal_projection_from_close_evidence(
    command_payload: dict[str, Any] | None,
    result_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project command terminal state from observer-owned task close evidence."""

    result = _mapping(result_payload)
    evidence = _observer_command_close_evidence_root(result)
    command_identity = _observer_command_route_identity(command_payload)
    events = _observer_command_terminal_events(evidence, result)
    contract = _mapping(evidence.get("contract") or evidence.get("task_contract"))
    provided_gate = _mapping(evidence.get("timeline_gate") or result.get("timeline_gate"))
    close_gate = (
        provided_gate
        if provided_gate.get("schema_version") == "mf_close_timeline_gate.v1"
        else mf_close_gate_verification(events, contract=contract)
    )
    route_context_gate = _mapping(close_gate.get("route_context_gate"))
    compact_refs = _observer_command_compact_event_refs(evidence, result)
    close_gate_source = _observer_command_close_gate_passed_source(
        evidence,
        result,
        close_gate,
    )
    close_gate_passed = bool(close_gate_source)
    canonical_identity = _observer_command_canonical_route_identity(
        evidence=evidence,
        result_payload=result,
        route_context_gate=route_context_gate,
        events=events,
    )
    explicit_canonical = _mapping(evidence.get("canonical_route_identity"))
    if canonical_identity and explicit_canonical.get("route_id"):
        canonical_identity = {
            **canonical_identity,
            "route_id": str(explicit_canonical.get("route_id") or ""),
        }
    superseded_identity = _mapping(evidence.get("superseded_route_identity"))
    if not superseded_identity and command_identity and not _observer_command_identity_matches(
        command_identity,
        canonical_identity,
    ):
        superseded_identity = command_identity
    backlog_close_state = _observer_command_backlog_close_state(evidence, result)
    terminal_refs = _observer_command_terminal_evidence_refs(
        events,
        route_context_gate,
        backlog_close_state,
    )
    for ref in compact_refs:
        if ref not in terminal_refs:
            terminal_refs.append(ref)

    missing: list[str] = []
    if not close_gate_passed:
        missing.append("canonical_close_gate_passed")
    if not _observer_command_has_accepted_close_ready(
        close_gate=close_gate,
        events=events,
    ):
        missing.append("accepted_close_ready")
    if not backlog_close_state["closed"]:
        missing.append("canonical_backlog_fixed_or_closed")
    if not canonical_identity:
        missing.append("canonical_route_identity")
    if command_identity and canonical_identity and not _observer_command_supersession_relation_present(
        evidence=evidence,
        route_context_gate=route_context_gate,
        command_identity=command_identity,
        canonical_identity=canonical_identity,
    ):
        missing.append("superseding_route_or_contract_relation")

    passed = not missing
    divergence_reason = ""
    if passed and superseded_identity:
        divergence_reason = "superseded_route_identity_reconciled"
    elif missing:
        divergence_reason = "missing_" + "_and_".join(missing)
    return {
        "schema_version": MF_OBSERVER_COMMAND_TERMINAL_PROJECTION_SCHEMA_VERSION,
        "source_of_truth": "Contract/Revision/Event",
        "projected_surface": "observer_command_queue",
        "projected_surfaces": [
            "observer_command_queue",
            "task_timeline",
            "backlog_runtime_state",
            "dashboard_cards",
        ],
        "passed": passed,
        "status": "projected_completed" if passed else "unresolved",
        "canonical_contract_state": "closed" if passed else "unresolved",
        "command_projection_status": "completed" if passed else "unresolved",
        "divergence_reason": divergence_reason,
        "canonical_route_identity": canonical_identity,
        "superseded_route_identity": superseded_identity,
        "terminal_evidence_refs": terminal_refs,
        "missing_requirement_ids": missing,
        "missing_evidence": _observer_command_missing_evidence_details(
            missing,
            close_gate_source=close_gate_source,
            compact_refs=compact_refs,
            events=events,
            command_identity=command_identity,
            canonical_identity=canonical_identity,
        ),
        "close_gate_passed_source": close_gate_source,
        "close_gate_status": str(close_gate.get("status") or ""),
        "backlog_close_request_id": str(backlog_close_state.get("request_id") or ""),
        "backlog_status": str(backlog_close_state.get("status") or ""),
        "contract_projection": _mapping(close_gate.get("contract_projection")),
        "route_context_gate": route_context_gate,
    }


def synthetic_failure_envelope(
    *,
    failure_class: str,
    phase: str,
    summary: str,
    session_result: dict[str, Any] | None = None,
    retryable: bool = True,
    recommended_next_action: str = "retry_or_observer_takeover",
) -> dict[str, Any]:
    session_result = session_result if isinstance(session_result, dict) else {}
    return {
        "failure": {
            "failure_class": failure_class,
            "phase": phase,
            "summary": summary,
            "session_id": session_result.get("session_id", ""),
            "exit_code": session_result.get("exit_code"),
            "elapsed_sec": session_result.get("elapsed_sec"),
            "stdout_bytes": len(session_result.get("stdout", "") or ""),
            "stderr_bytes": len(session_result.get("stderr", "") or ""),
            "retryable": retryable,
            "recommended_next_action": recommended_next_action,
        }
    }


def list_events(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    task_id: str = "",
    backlog_id: str = "",
    trace_id: str = "",
    phase: str = "",
    event_kind: str = "",
    scenario_id: str = "",
    correlation_id: str = "",
    severity: str = "",
    decision: str = "",
    parent_event_id: int = 0,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if task_id:
        clauses.append("task_id = ?")
        params.append(task_id)
    if backlog_id:
        clauses.append("backlog_id = ?")
        params.append(backlog_id)
    if trace_id:
        clauses.append("trace_id = ?")
        params.append(trace_id)
    if phase:
        clauses.append("phase = ?")
        params.append(phase)
    if event_kind:
        clauses.append("event_kind = ?")
        params.append(event_kind)
    if scenario_id:
        clauses.append("scenario_id = ?")
        params.append(scenario_id)
    if correlation_id:
        clauses.append("correlation_id = ?")
        params.append(correlation_id)
    if severity:
        clauses.append("severity = ?")
        params.append(severity)
    if decision:
        clauses.append("decision = ?")
        params.append(decision)
    if parent_event_id:
        clauses.append("parent_event_id = ?")
        params.append(int(parent_event_id))
    params.append(max(1, min(int(limit or 200), 1000)))
    rows = conn.execute(
        f"""SELECT * FROM task_timeline_events
            WHERE {' AND '.join(clauses)}
            ORDER BY id ASC
            LIMIT ?""",
        params,
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for key in ("payload_json", "verification_json", "artifact_refs_json"):
        try:
            result[key[:-5]] = json.loads(result.get(key) or "{}")
        except Exception:
            result[key[:-5]] = {}
    return result
