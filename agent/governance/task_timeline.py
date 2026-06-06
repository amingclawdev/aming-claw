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
from typing import Any

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
MF_OBSERVER_COMMAND_TERMINAL_PROJECTION_SCHEMA_VERSION = "observer_command_terminal_projection.v1"
MF_SUBAGENT_READ_RECEIPT_GATE_SCHEMA_VERSION = "mf_subagent_read_receipt_gate.v1"
MF_LANE_OWNERSHIP_SCHEMA_VERSION = "mf_lane_ownership_gate.v1"
MF_BOUNDED_SUBAGENT_LANE_ID = "bounded_implementation_subagent"
MF_BOUNDED_SUBAGENT_DISPATCH_ID = f"{MF_BOUNDED_SUBAGENT_LANE_ID}.dispatch"
MF_BOUNDED_SUBAGENT_REVIEW_READY_ID = f"{MF_BOUNDED_SUBAGENT_LANE_ID}.review_ready"

MF_ROUTE_CONTEXT_GATE_SCHEMA_VERSION = "mf_route_context_consumption_gate.v1"
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
        "payload": payload if isinstance(payload, dict) else {},
        "verification": verification if isinstance(verification, dict) else {},
        "artifact_refs": artifact_refs if isinstance(artifact_refs, dict) else {},
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
    return _insert_event(
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
                    item["result"] = _insert_event(conn, event)
                    conn.commit()
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
        if identity_filter:
            identity = _read_receipt_gate_route_identity(event)
            if not identity:
                lineage_ignored.append(
                    _read_receipt_gate_event_ref(
                        event,
                        reason="missing_route_identity_for_current_lineage",
                    )
                )
                continue
            if not _route_identity_matches_filter(identity, identity_filter):
                lineage_ignored.append(
                    _read_receipt_gate_event_ref(
                        event,
                        reason="superseded_route_identity",
                    )
                )
                continue
        if attempt_lineage_filter:
            attempt_lineage = _read_receipt_gate_attempt_lineage(event)
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
    first_counted = min(counted, default=None, key=lambda item: (item[0], item[1]))
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


def _route_parent_child_startup_identity(
    value: Any,
    identity: dict[str, str],
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
            "dispatch_evidence",
            "startup_evidence",
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
    if markers.intersection(
        {
            "route_context",
            "route_prompt_bundle",
            "prompt_alert_bundle",
            "visible_injection_manifest",
            "visible_injection_manifest_hash",
        }
    ):
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
    if markers.intersection(
        {
            "mf_subagent_startup",
            "mf_subagent.startup",
            "mf_subagent_startup_gate",
            "startup_gate",
            "startup_evidence",
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
            "passed": False,
        }
    return {
        "categories": sorted(_route_event_categories(row)),
        "route_identity": _route_identity(row),
        "passed": _route_event_passed(row),
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
    accepted_startup_lineages: list[dict[str, Any]] = []
    attempt_lineage_candidates: list[dict[str, Any]] = []

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
        if "mf_subagent_startup" in categories:
            normalized_identity, lineage = _route_parent_child_startup_identity(
                event,
                identity,
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
        "accepted_startup_lineages": accepted_startup_lineages,
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


def mf_close_gate_verification(
    events: list[dict[str, Any]] | None,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the minimum observer/MF timeline evidence before backlog close."""

    rows = events if isinstance(events, list) else []
    present: set[str] = set()
    ignored: list[dict[str, Any]] = []
    for event in rows:
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
        if key in MF_CLOSE_REQUIRED_EVENT_KINDS and status in MF_CLOSE_PASS_STATUSES:
            present.add(key)
        elif key in MF_CLOSE_REQUIRED_EVENT_KINDS:
            ignored.append({
                "event_kind": kind,
                "phase": phase,
                "status": status,
                "id": event.get("id"),
            })
    missing = sorted(MF_CLOSE_REQUIRED_EVENT_KINDS - present)
    contract_gate = mf_contract_gate_verification(rows, contract)
    route_context_gate = mf_route_context_gate_verification(rows, contract)
    lane_ownership_gate = mf_lane_ownership_gate_verification(rows, contract)
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
    if (
        post_verification_actions_gate.get("required")
        and not post_verification_actions_gate.get("passed")
    ):
        groups["post_verification_actions"] = {
            "label": "post-verification impact actions",
            "missing": post_verification_actions_gate.get("missing_actions", []),
            "next_action": "record observer-owned post-verification action or follow-up evidence",
        }
    route_context_reminder = mf_route_context_reminder(
        route_context_gate,
        missing_evidence_groups,
    )
    passed = (
        not missing
        and bool(contract_gate.get("passed"))
        and bool(route_context_gate.get("passed"))
        and bool(lane_ownership_gate.get("passed"))
        and bool(contract_projection_gate.get("passed"))
        and bool(post_verification_actions_gate.get("passed"))
    )
    return {
        "schema_version": "mf_close_timeline_gate.v1",
        "passed": passed,
        "status": "passed" if passed else "failed",
        "required_event_kinds": sorted(MF_CLOSE_REQUIRED_EVENT_KINDS),
        "present_event_kinds": sorted(present),
        "missing_event_kinds": missing,
        "event_count": len(rows),
        "ignored_required_events": ignored,
        "contract_gate": contract_gate,
        "route_context_gate": route_context_gate,
        "lane_ownership_gate": lane_ownership_gate,
        "contract_projection": contract_projection,
        "contract_projection_gate": contract_projection_gate,
        "post_verification_actions_gate": post_verification_actions_gate,
        "missing_evidence_groups": missing_evidence_groups,
        "route_context_reminder": route_context_reminder,
        "checks": {
            "has_implementation": "implementation" in present,
            "has_verification": "verification" in present,
            "has_close_ready": "close_ready" in present,
            "has_contract_evidence": bool(contract_gate.get("passed")),
            "has_route_context_consumption": bool(route_context_gate.get("passed")),
            "has_lane_ownership": bool(lane_ownership_gate.get("passed")),
            "has_contract_projection": bool(contract_projection.get("schema_version")),
            "has_current_contract_projection": bool(
                contract_projection_gate.get("passed")
            ),
            "has_post_verification_actions": bool(
                post_verification_actions_gate.get("passed")
            ),
            "mf_subagent_read_receipt_gate": str(
                _mapping(contract_projection.get("read_receipt_gate")).get("status") or ""
            ),
        },
    }


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
    return []


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
    canonical_identity = _mapping(
        evidence.get("canonical_route_identity")
        or route_context_gate.get("route_identity")
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

    missing: list[str] = []
    if not bool(close_gate.get("passed")):
        missing.append("canonical_close_gate_passed")
    if "close_ready" not in set(close_gate.get("present_event_kinds") or []):
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
