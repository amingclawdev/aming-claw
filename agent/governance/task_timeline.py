"""Append-only task implementation timeline.

Backlog rows describe the intended work. Task timeline rows describe execution
facts proposed by agents, verified by executors/gates, and accepted by
observers. This module centralizes writes so parallel agents do not scatter
SQLite mutations across the codebase.
"""

from __future__ import annotations

import json
import logging
import queue
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


def _scenario_spec(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("test_scenario_spec", "test_scenario", "scenario"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _insert_event(conn: sqlite3.Connection, event: dict[str, Any]) -> dict[str, Any]:
    from .db import sqlite_write_lock

    created_at = event.get("created_at") or _utc_iso()
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
                _json(event.get("payload"), {}),
                _json(event.get("verification"), {}),
                _json(event.get("artifact_refs"), {}),
                _text(event.get("trace_id")),
                _text(event.get("commit_sha")),
                created_at,
            ),
        )
    return {
        "id": cur.lastrowid,
        "project_id": _text(event.get("project_id")),
        "task_id": _text(event.get("task_id")),
        "event_type": _text(event.get("event_type")),
        "phase": _text(event.get("phase")),
        "event_kind": _text(event.get("event_kind")),
        "scenario_id": _text(event.get("scenario_id")),
        "correlation_id": _text(event.get("correlation_id")),
        "schema_version": int(event.get("schema_version") or TIMELINE_SCHEMA_VERSION),
        "created_at": created_at,
    }


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
    policy = str(payload.get("test_scenario_policy") or "").strip()
    verification_notes = _string_list(payload.get("verification_notes"))
    tests_run = _string_list(payload.get("tests_run"))
    scenario_id = str(payload.get("scenario_id") or "").strip()
    scenario = _scenario_spec(payload)
    if not scenario_id and scenario:
        scenario_id = str(scenario.get("id") or "").strip()

    errors: list[str] = []
    warnings: list[str] = []

    if policy not in MF_TEST_SCENARIO_POLICIES:
        errors.append(
            "test_scenario_policy must be one of: "
            + ", ".join(sorted(MF_TEST_SCENARIO_POLICIES))
        )
    elif policy == "none":
        if not verification_notes and not tests_run:
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
        "scenario_id": scenario_id,
        "warnings": warnings,
        "errors": errors,
        "checks": {
            "has_explicit_policy": policy in MF_TEST_SCENARIO_POLICIES,
            "has_verification_notes": bool(verification_notes),
            "has_tests_run": bool(tests_run),
            "has_scenario_id": bool(scenario_id),
            "has_new_scenario_spec": has_new_scenario_spec,
        },
    }


def mf_close_gate_verification(events: list[dict[str, Any]] | None) -> dict[str, Any]:
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
    return {
        "schema_version": "mf_close_timeline_gate.v1",
        "passed": not missing,
        "status": "passed" if not missing else "failed",
        "required_event_kinds": sorted(MF_CLOSE_REQUIRED_EVENT_KINDS),
        "present_event_kinds": sorted(present),
        "missing_event_kinds": missing,
        "event_count": len(rows),
        "ignored_required_events": ignored,
        "checks": {
            "has_implementation": "implementation" in present,
            "has_verification": "verification" in present,
            "has_close_ready": "close_ready" in present,
        },
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
