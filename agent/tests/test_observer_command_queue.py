from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.governance import (
    observer_session,
    parallel_branch_runtime,
    raw_requirement,
    task_timeline,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    observer_session.ensure_schema(conn)
    raw_requirement.ensure_schema(conn)
    return conn


def _register(conn: sqlite3.Connection, project_id: str = "demo") -> dict:
    return observer_session.register_session(conn, project_id=project_id)


def _execute_backlog_row_payload() -> dict:
    return {
        "backlog_id": "AC-ROUTE-HANDOFF",
        "route_id": "route-20260602-9cbbd7a9fd",
        "route_context_hash": "sha256:f1641a8d28b2a9211a14d90fed8dda4c40bb87380557f64a81e29e332568c27b",
        "prompt_contract_id": "rprompt-7417905f707deac2",
        "visible_injection_manifest_hash": "sha256:30e229df0e1948f6c206d954c8226acd9272816a4168216a4258a8ebf0328810",
        "subsystem": "observer",
    }


def _actual_startup_result() -> dict:
    return {
        "ok": True,
        "startup_gate": {
            "schema_version": "mf_subagent_startup_gate.v1",
            "gate_kind": "mf_subagent.startup",
            "status": "passed",
            "actual_startup_recorded": True,
            "worker_role": "mf_sub",
            "worker_id": "worker-1",
            "agent_id": "agent-1",
            "session_token_hash": "sha256:startup-token",
            "fence_token": "fence-1",
            "actual_cwd": "/repo/.worktrees/worker-1",
            "actual_git_root": "/repo/.worktrees/worker-1",
            "branch": "refs/heads/codex/worker-1",
            "head_commit": "head-1",
        },
    }


def _first_progress_result() -> dict:
    return {
        "graph_trace_evidence": {
            "schema_version": "mf_subagent_graph_trace.v1",
            "query_source": "mf_subagent",
            "query_purpose": "subagent_context_build",
            "trace_ids": ["gqt-progress-1"],
            "task_id": "mf-sub-task-1",
            "parent_task_id": "observer-task-1",
            "worker_role": "mf_sub",
            "fence_token": "fence-1",
        },
        "worktree_diff_scope": {
            "schema_version": "mf_subagent_worktree_diff_scope.v1",
            "worktree": "/repo/.worktrees/worker-1",
            "base_commit": "base-1",
            "head_commit": "head-1",
            "implementation_changed_files": ["agent/example.py"],
            "dirty_files": [],
            "no_diff": False,
        },
    }


STALE_BOOTSTRAP_ROUTE = {
    "route_id": "route-repair-01c5a0404ba10777",
    "route_context_hash": "sha256:stale-bootstrap-route-context",
    "prompt_contract_id": "rprompt-repair-01c5a0404ba10777",
    "prompt_contract_hash": "sha256:stale-bootstrap-prompt-contract",
    "visible_injection_manifest_hash": "sha256:stale-visible-manifest",
}

CANONICAL_A3_ROUTE = {
    "route_id": "route-repair-e97d980211e2dc1c",
    "route_context_hash": "sha256:6fff2f7365b877da0d6130365c4a5d96c7abb5d151ebb0960e4fc1abc65cec46",
    "prompt_contract_id": "rprompt-repair-e97d980211e2dc1c",
    "prompt_contract_hash": "sha256:ad98e3b14698b479dbb6d2c82d91f11758c1e1a26ab3b222b4d6ea9c8962b245",
    "visible_injection_manifest_hash": "sha256:86e97fdf869553c3a339c7aefbe8dfa548e9899627209c070e541df11d0c69e7",
}

CANONICAL_CONTRACT_HASH = (
    "sha256:091f3bd50e9ad762979b8bc46092a31d18572f3d7f32e2e7649de76e4e23db51"
)


def _route_event(kind: str, event_id: int, route: dict, *, status: str = "passed") -> dict:
    payload_key = {
        "route_context": "route_context",
        "route_action_precheck": "route_action_precheck",
        "mf_subagent_dispatch": "mf_subagent_dispatch_gate",
        "mf_subagent_startup": "mf_subagent_startup_gate",
    }[kind]
    body = {
        **route,
        "worker_id": "mf-sub-a3",
        "bounded": True,
        "visible_injection_manifest_hash": route["visible_injection_manifest_hash"],
    }
    if kind == "mf_subagent_startup":
        body.update({
            "fence_token": "fence-a3",
            "actual_cwd": "/repo/.worktrees/mf-sub-a3",
            "actual_git_root": "/repo/.worktrees/mf-sub-a3",
            "branch": "refs/heads/codex/a3",
            "head_commit": "0f1f83b33251a43066ecdca26427be4fc23aa5f8",
        })
    return {
        "id": event_id,
        "event_kind": kind,
        "phase": "startup_gate" if kind == "mf_subagent_startup" else "dispatch",
        "status": status,
        "payload": {
            payload_key: body,
            "visible_injection_manifest_hash": route["visible_injection_manifest_hash"],
        },
    }


def _canonical_close_evidence(*, include_close_ready: bool = True, include_cleanup: bool = True) -> dict:
    events = [
        {
            "id": 1810,
            "event_kind": "mf_subagent_read_receipt",
            "phase": "startup",
            "status": "accepted",
            "payload": {
                **CANONICAL_A3_ROUTE,
                "read_receipt_hash": "sha256:a3-read",
                "canonical_visible_contract_text_hash": CANONICAL_CONTRACT_HASH,
            },
        },
        {
            "id": 1811,
            "event_kind": "implementation",
            "phase": "implementation",
            "status": "accepted",
            "payload": {
                **CANONICAL_A3_ROUTE,
                "canonical_visible_contract_text_hash": CANONICAL_CONTRACT_HASH,
            },
        },
        {
            "id": 1817,
            "event_kind": "verification",
            "phase": "verification",
            "status": "passed",
            "verification": {
                **CANONICAL_A3_ROUTE,
                "contract_evidence": [
                    {
                        "requirement_id": "independent_verification_lane",
                        "status": "passed",
                        "reviewer_role": "qa",
                    }
                ],
            },
        },
        {
            "id": 1819,
            "event_kind": "architecture_review",
            "phase": "architecture_review",
            "status": "passed",
            "verification": CANONICAL_A3_ROUTE,
        },
        {"id": 1821, "event_kind": "merge_gate", "phase": "merge_gate", "status": "passed"},
        {"id": 1823, "event_kind": "live_merge", "phase": "live_merge", "status": "passed"},
        _route_event("route_context", 1801, STALE_BOOTSTRAP_ROUTE),
        _route_event("route_action_precheck", 1802, STALE_BOOTSTRAP_ROUTE),
        _route_event("mf_subagent_dispatch", 1803, STALE_BOOTSTRAP_ROUTE),
        _route_event("route_context", 1825, CANONICAL_A3_ROUTE),
        _route_event("route_action_precheck", 1826, CANONICAL_A3_ROUTE),
        _route_event("mf_subagent_dispatch", 1827, CANONICAL_A3_ROUTE),
        _route_event("mf_subagent_startup", 1828, CANONICAL_A3_ROUTE),
        {
            "id": 1833,
            "event_kind": "contract_projection_reconciled",
            "phase": "projection",
            "status": "accepted",
            "payload": {"canonical_visible_contract_text_hash": CANONICAL_CONTRACT_HASH},
        },
    ]
    if include_cleanup:
        events.append({
            "id": 1824,
            "event_kind": "route_identity_cleanup",
            "phase": "identity_recovery",
            "status": "accepted",
            "payload": {
                "route_identity_cleanup": {
                    **CANONICAL_A3_ROUTE,
                    "reason": "Supersede stale bootstrap route with canonical A3 route evidence.",
                }
            },
        })
    if include_close_ready:
        events.append({
            "id": 1835,
            "event_kind": "close_ready",
            "phase": "close",
            "status": "accepted",
            "payload": {
                **CANONICAL_A3_ROUTE,
                "canonical_visible_contract_text_hash": CANONICAL_CONTRACT_HASH,
            },
        })
    return {
        "canonical_close_evidence": {
            "timeline_events": events,
            "contract": {
                "template_id": "mf_parallel.v1",
                "contract_instance_id": "AC-OBSERVER-OWNED-AGENT-TASK-CONTRACT-QUEUE-20260604",
                "canonical_visible_contract_text_hash": CANONICAL_CONTRACT_HASH,
            },
            "canonical_route_identity": CANONICAL_A3_ROUTE,
            "backlog_close": {
                "ok": True,
                "request_id": "req-97cd668efd14",
                "backlog_status": "FIXED",
            },
        }
    }


def _insert_legacy_execute_command(
    conn: sqlite3.Connection,
    *,
    payload: dict,
    command_id: str = "cmd-legacy-execute",
    project_id: str = "demo",
    target_session_id: str = "",
    created_at: str = "2026-06-03T00:00:00Z",
) -> dict:
    conn.execute(
        """
        INSERT INTO observer_command_queue (
            command_id, project_id, command_type, payload_json, status,
            target_session_id, claimed_by_session_id, created_by, created_at,
            notified_at, claimed_at, completed_at, result_json, error
        ) VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, '', '', '{}', '')
        """,
        (
            command_id,
            project_id,
            observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
            observer_session._json_dumps(payload),
            observer_session.COMMAND_STATUS_NOTIFIED,
            target_session_id,
            "legacy_dashboard",
            created_at,
            created_at,
        ),
    )
    conn.commit()
    return observer_session.get_command(
        conn,
        project_id=project_id,
        command_id=command_id,
    )


def test_command_enqueue_and_list_preserve_business_payload_in_db():
    conn = _conn()

    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_ANALYZE_REQUIREMENTS,
        payload={"raw_id": "raw-1", "source": "dashboard"},
        created_by="dashboard",
    )
    listed = observer_session.list_commands(conn, project_id="demo")

    assert command["status"] == observer_session.COMMAND_STATUS_QUEUED
    assert command["payload"] == {"raw_id": "raw-1", "source": "dashboard"}
    assert listed[0]["command_id"] == command["command_id"]
    assert listed[0]["payload"]["raw_id"] == "raw-1"
    assert observer_session.command_pending_reminder("demo") == {
        "kind": "observer_command_pending",
        "project_id": "demo",
        "message": "pending observer commands exist; call observer_command_next",
        "payload_included": False,
        "next_action": {
            "tool": "observer_command_next",
            "description": "claim the next pending observer command",
        },
    }


def test_execute_backlog_row_claim_complete_preserves_payload_without_reminder_leak():
    conn = _conn()
    session = _register(conn)
    payload = _execute_backlog_row_payload()

    assert observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW in observer_session.VALID_COMMAND_TYPES
    assert (
        observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW
        in observer_session.DEFAULT_CAPABILITIES["command_types"]
    )

    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=payload,
        created_by="judgment_brain",
        notify=True,
    )
    claimed = observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
    )
    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        result={
            **_actual_startup_result(),
            **_first_progress_result(),
            "backlog_id": payload["backlog_id"],
        },
    )

    reminder = observer_session.command_pending_reminder("demo")

    assert command["payload"] == payload
    assert claimed["command"]["payload"] == payload
    assert completed["command"]["status"] == observer_session.COMMAND_STATUS_COMPLETED
    assert completed["command"]["payload"] == payload
    assert completed["command"]["result"]["ok"] is True
    assert completed["command"]["result"]["backlog_id"] == payload["backlog_id"]
    assert completed["command"]["result"]["startup_gate"]["actual_startup_recorded"] is True
    assert completed["command"]["result"]["graph_trace_evidence"]["trace_ids"] == [
        "gqt-progress-1"
    ]
    assert reminder["payload_included"] is False
    assert "payload" not in reminder
    assert payload["backlog_id"] not in str(reminder)


def test_execute_backlog_row_complete_with_startup_only_fails_no_progress():
    conn = _conn()
    session = _register(conn)
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=_execute_backlog_row_payload(),
        created_by="judgment_brain",
        notify=True,
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        now="2026-06-03T00:00:02Z",
    )

    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        result=_actual_startup_result(),
        now="2026-06-03T00:00:03Z",
    )

    assert completed["command"]["status"] == observer_session.COMMAND_STATUS_FAILED
    assert completed["command"]["error"] == "startup_without_first_progress_evidence"
    watchdog = completed["command"]["result"]["progress_watchdog"]
    assert watchdog["present"] is False
    assert watchdog["startup_evidence_present"] is True
    assert "mf_subagent_startup" in watchdog["excluded_as_progress"]


def test_execute_backlog_row_complete_without_startup_fails_truthfully():
    conn = _conn()
    session = _register(conn)
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=_execute_backlog_row_payload(),
        created_by="judgment_brain",
        notify=True,
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        now="2026-06-03T00:00:02Z",
    )

    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        result={
            "ok": True,
            "branch_runtime_evidence": {"registered": True},
            "startup_intent_event": {
                "event_kind": "mf_subagent_startup_intent",
                "actual_startup_required": True,
            },
        },
        now="2026-06-03T00:00:03Z",
    )

    command_after = completed["command"]
    blocker = command_after["result"]["startup_surface_blocker"]
    assert command_after["status"] == observer_session.COMMAND_STATUS_FAILED
    assert command_after["error"] == "no_truthful_bounded_mf_sub_startup_surface_available"
    assert blocker["terminal_dispatch_blocker"] is True
    assert "runtime-text startup intent" in blocker["reason"]


def test_execute_backlog_row_complete_resolves_persisted_mf_sub_evidence_refs():
    conn = _conn()
    task_timeline.ensure_schema(conn)
    session = _register(conn)
    task_id = "mf-sub-durable-complete"
    runtime_context_id = "mfrctx-durable-complete"
    checkpoint_id = "ckpt-durable-complete"
    payload = {
        **_execute_backlog_row_payload(),
        "task_id": task_id,
        "runtime_context_id": runtime_context_id,
    }
    parallel_branch_runtime.upsert_branch_context(
        conn,
        parallel_branch_runtime.BranchTaskRuntimeContext(
            project_id="demo",
            task_id=task_id,
            runtime_context_id=runtime_context_id,
            backlog_id=payload["backlog_id"],
            root_task_id=payload["backlog_id"],
            branch_ref="refs/heads/codex/durable-complete",
            status=parallel_branch_runtime.STATE_RUNNING,
            worker_id="worker-1",
            agent_id="agent-1",
            worker_slot_id="worker-1",
            fence_token="fence-1",
            worktree_path="/repo/.worktrees/worker-1",
            base_commit="base-1",
            head_commit="head-1",
            target_head_commit="base-1",
            merge_queue_id="mq-durable-complete",
            checkpoint_id=checkpoint_id,
            replay_source="mf_sub_finish_gate",
        ),
        now_iso="2026-06-03T00:00:01Z",
    )
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=payload,
        created_by="judgment_brain",
        notify=True,
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        now="2026-06-03T00:00:02Z",
    )
    startup_gate = dict(_actual_startup_result()["startup_gate"])
    startup_gate.update(
        {
            "task_id": task_id,
            "parent_task_id": payload["backlog_id"],
            "runtime_context_id": runtime_context_id,
            "branch": "refs/heads/codex/durable-complete",
            "branch_ref": "refs/heads/codex/durable-complete",
            "worktree_path": "/repo/.worktrees/worker-1",
            "base_commit": "base-1",
            "target_head_commit": "base-1",
            "merge_queue_id": "mq-durable-complete",
            "route_id": payload["route_id"],
            "route_context_hash": payload["route_context_hash"],
            "prompt_contract_id": payload["prompt_contract_id"],
            "prompt_contract_hash": "sha256:prompt-durable-complete",
            "visible_injection_manifest_hash": payload["visible_injection_manifest_hash"],
            "owned_files": ["agent/example.py"],
        }
    )
    startup_event = task_timeline.record_event(
        conn,
        project_id="demo",
        backlog_id=payload["backlog_id"],
        task_id=task_id,
        attempt_num=1,
        event_type="mf_subagent.startup",
        phase="startup_gate",
        event_kind="mf_subagent_startup",
        status="passed",
        payload={"mf_subagent_startup_gate": startup_gate},
    )
    read_receipt = task_timeline.record_event(
        conn,
        project_id="demo",
        backlog_id=payload["backlog_id"],
        task_id=task_id,
        attempt_num=1,
        event_type="mf_subagent.read_receipt",
        phase="startup_read_receipt",
        event_kind="mf_subagent_read_receipt",
        status="accepted",
        payload={
            "task_id": task_id,
            "runtime_context_id": runtime_context_id,
            "fence_token": "fence-1",
            "read_receipt_hash": "sha256:read-durable-complete",
        },
    )
    verification = task_timeline.record_event(
        conn,
        project_id="demo",
        backlog_id=payload["backlog_id"],
        task_id=task_id,
        attempt_num=1,
        event_type="verification",
        phase="verification",
        event_kind="verification",
        status="passed",
        payload={
            "task_id": task_id,
            "runtime_context_id": runtime_context_id,
            "fence_token": "fence-1",
        },
    )
    finish = task_timeline.record_event(
        conn,
        project_id="demo",
        backlog_id=payload["backlog_id"],
        task_id=task_id,
        attempt_num=1,
        event_type="mf_subagent.finish_gate",
        phase="finish_gate",
        event_kind="mf_subagent_finish_gate",
        status="review_ready",
        payload={
            "task_id": task_id,
            "runtime_context_id": runtime_context_id,
            "fence_token": "fence-1",
            "checkpoint_id": checkpoint_id,
        },
    )

    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        result={
            "ok": True,
            "status": "review_ready",
            "task_id": task_id,
            "runtime_context_id": runtime_context_id,
            "fence_token": "fence-1",
            "checkpoint_id": checkpoint_id,
            "timeline_refs": {
                "startup_event_ref": f"timeline:{startup_event['id']}",
                "read_receipt_event_ref": f"timeline:{read_receipt['id']}",
                "verification_event_refs": [f"timeline:{verification['id']}"],
                "finish_gate_ref": f"timeline:{finish['id']}",
            },
        },
        now="2026-06-03T00:00:04Z",
    )

    command_after = completed["command"]
    durable = command_after["result"]["durable_mf_sub_evidence"]
    assert command_after["status"] == observer_session.COMMAND_STATUS_COMPLETED
    assert command_after["error"] == ""
    assert "startup_surface_blocker" not in command_after["result"]
    assert durable["startup_event_ref"] == f"timeline:{startup_event['id']}"
    assert durable["read_receipt_event_ref"] == f"timeline:{read_receipt['id']}"
    assert durable["finish_gate_ref"] == f"timeline:{finish['id']}"
    assert durable["verification_event_refs"] == [f"timeline:{verification['id']}"]


def test_execute_backlog_row_complete_allocation_only_runtime_context_still_blocks():
    conn = _conn()
    session = _register(conn)
    task_id = "mf-sub-allocation-only"
    runtime_context_id = "mfrctx-allocation-only"
    checkpoint_id = "ckpt-allocation-only"
    payload = {
        **_execute_backlog_row_payload(),
        "task_id": task_id,
        "runtime_context_id": runtime_context_id,
    }
    parallel_branch_runtime.upsert_branch_context(
        conn,
        parallel_branch_runtime.BranchTaskRuntimeContext(
            project_id="demo",
            task_id=task_id,
            runtime_context_id=runtime_context_id,
            backlog_id=payload["backlog_id"],
            root_task_id=payload["backlog_id"],
            branch_ref="refs/heads/codex/allocation-only",
            status=parallel_branch_runtime.STATE_ALLOCATED,
            worker_id="worker-1",
            agent_id="agent-1",
            worker_slot_id="worker-1",
            fence_token="fence-1",
            worktree_path="/repo/.worktrees/worker-1",
            base_commit="base-1",
            head_commit="base-1",
            target_head_commit="base-1",
            merge_queue_id="mq-allocation-only",
            checkpoint_id=checkpoint_id,
            replay_source="mf_sub_finish_gate",
        ),
        now_iso="2026-06-03T00:00:01Z",
    )
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=payload,
        created_by="judgment_brain",
        notify=True,
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        now="2026-06-03T00:00:02Z",
    )

    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        result={
            "ok": True,
            "status": "review_ready",
            "task_id": task_id,
            "runtime_context_id": runtime_context_id,
            "fence_token": "fence-1",
            "checkpoint_id": checkpoint_id,
            "branch_runtime_evidence": {"registered": True},
        },
        now="2026-06-03T00:00:04Z",
    )

    command_after = completed["command"]
    assert command_after["status"] == observer_session.COMMAND_STATUS_FAILED
    assert command_after["error"] == "no_truthful_bounded_mf_sub_startup_surface_available"
    assert "durable_mf_sub_evidence" not in command_after["result"]


def test_execute_backlog_row_prepared_startup_event_does_not_complete():
    conn = _conn()
    session = _register(conn)
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=_execute_backlog_row_payload(),
        created_by="judgment_brain",
        notify=True,
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        now="2026-06-03T00:00:02Z",
    )

    prepared_startup_event = {
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "prepared",
        "payload": {
            "mf_subagent_startup_gate": {
                "schema_version": "mf_subagent_startup_gate.v1",
                "gate_kind": "mf_subagent.startup",
                "status": "prepared",
                "actual_startup_recorded": False,
                "appendable": True,
                "worker_role": "mf_sub",
                "worker_id": "worker-1",
                "agent_id": "agent-1",
                "session_token_hash": "sha256:prepared-token",
                "fence_token": "fence-1",
                "actual_cwd": "/repo/.worktrees/worker-1",
                "actual_git_root": "/repo/.worktrees/worker-1",
                "branch": "refs/heads/codex/worker-1",
                "head_commit": "head-1",
            },
        },
    }

    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        result={"ok": True, "prepared_startup_event": prepared_startup_event},
        now="2026-06-03T00:00:03Z",
    )

    command_after = completed["command"]
    blocker = command_after["result"]["startup_surface_blocker"]
    assert command_after["status"] == observer_session.COMMAND_STATUS_FAILED
    assert command_after["error"] == "no_truthful_bounded_mf_sub_startup_surface_available"
    assert blocker["status"] == "blocked"
    assert blocker["terminal_dispatch_blocker"] is True
    assert command_after["result"]["prepared_startup_event"] == prepared_startup_event


def test_execute_backlog_row_cli_timeout_blocks_even_with_startup_evidence():
    conn = _conn()
    session = _register(conn)
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=_execute_backlog_row_payload(),
        created_by="judgment_brain",
        notify=True,
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
    )

    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        result={
            **_actual_startup_result(),
            "ok": False,
            "status": "blocked",
            "cli_timeout_blocker": {
                "status": "blocked",
                "blocker_id": "codex_cli_timeout_no_output_no_finish",
                "no_output": True,
                "no_finish_evidence": True,
            },
            "terminal_contract_projection": {
                "canonical_contract_state": "blocked",
                "command_projection_status": "blocked",
                "divergence_reason": "codex_cli_timeout_no_output_no_finish",
            },
        },
    )

    command_after = completed["command"]
    assert command_after["status"] == observer_session.COMMAND_STATUS_FAILED
    assert command_after["error"] == "codex_cli_timeout_no_output_no_finish"
    assert command_after["result"]["status"] == "blocked"
    assert command_after["result"]["command_projection_status"] == "blocked"
    assert command_after["result"]["canonical_contract_state"] == "blocked"


def test_execute_backlog_row_completion_projects_terminal_from_canonical_close_evidence():
    conn = _conn()
    session = _register(conn)
    payload = {"backlog_id": "AC-OBSERVER-OWNED-AGENT-TASK-CONTRACT-QUEUE-20260604", **STALE_BOOTSTRAP_ROUTE}
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=payload,
        command_id="cmd-d0e3e3bf7893",
        created_by="judgment_brain",
        notify=True,
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
    )

    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        result={"ok": True, **_canonical_close_evidence()},
        now="2026-06-04T06:20:00Z",
    )

    command_after = completed["command"]
    projection = command_after["result"]["terminal_contract_projection"]
    assert command_after["status"] == observer_session.COMMAND_STATUS_COMPLETED
    assert command_after["error"] == ""
    assert "startup_surface_blocker" not in command_after["result"]
    assert projection["source_of_truth"] == "Contract/Revision/Event"
    assert projection["canonical_contract_state"] == "closed"
    assert projection["command_projection_status"] == "completed"
    assert projection["divergence_reason"] == "superseded_route_identity_reconciled"
    assert projection["canonical_route_identity"]["route_id"] == CANONICAL_A3_ROUTE["route_id"]
    assert projection["superseded_route_identity"]["route_id"] == STALE_BOOTSTRAP_ROUTE["route_id"]
    assert projection["backlog_close_request_id"] == "req-97cd668efd14"
    assert command_after["command_projection_status"] == "completed"
    assert command_after["canonical_route_identity"]["route_context_hash"] == (
        CANONICAL_A3_ROUTE["route_context_hash"]
    )


def test_execute_backlog_row_completion_does_not_project_without_closed_backlog_state():
    conn = _conn()
    session = _register(conn)
    payload = {"backlog_id": "AC-NO-CLOSE", **STALE_BOOTSTRAP_ROUTE}
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=payload,
        created_by="judgment_brain",
        notify=True,
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
    )
    evidence = _canonical_close_evidence()
    evidence["canonical_close_evidence"]["backlog_close"]["backlog_status"] = "IN_PROGRESS"

    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        result={"ok": True, **evidence},
    )

    command_after = completed["command"]
    projection = command_after["result"]["terminal_contract_projection"]
    assert command_after["status"] == observer_session.COMMAND_STATUS_FAILED
    assert command_after["error"] == "no_truthful_bounded_mf_sub_startup_surface_available"
    assert projection["command_projection_status"] == "unresolved"
    assert "canonical_backlog_fixed_or_closed" in projection["missing_requirement_ids"]


def test_execute_backlog_row_completion_keeps_blocker_without_superseding_route_relation():
    conn = _conn()
    session = _register(conn)
    payload = {"backlog_id": "AC-NO-SUPERSESSION", **STALE_BOOTSTRAP_ROUTE}
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=payload,
        created_by="judgment_brain",
        notify=True,
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
    )

    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        result={"ok": True, **_canonical_close_evidence(include_cleanup=False)},
    )

    command_after = completed["command"]
    projection = command_after["result"]["terminal_contract_projection"]
    assert command_after["status"] == observer_session.COMMAND_STATUS_FAILED
    assert command_after["error"] == "no_truthful_bounded_mf_sub_startup_surface_available"
    assert projection["command_projection_status"] == "unresolved"
    assert "superseding_route_or_contract_relation" in projection["missing_requirement_ids"]


def test_execute_backlog_row_rejects_missing_route_payload_fields():
    conn = _conn()

    with pytest.raises(ValueError, match="payload must be an object.*backlog_id"):
        observer_session.enqueue_command(
            conn,
            project_id="demo",
            command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
            payload=None,
        )

    with pytest.raises(
        ValueError,
        match=(
            "missing required fields: route_id, route_context_hash, "
            "prompt_contract_id, visible_injection_manifest_hash"
        ),
    ):
        observer_session.enqueue_command(
            conn,
            project_id="demo",
            command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
            payload={"backlog_id": "AC-ROUTE-HANDOFF"},
        )


def test_notified_execute_recovery_reports_no_active_consumer_without_claiming():
    conn = _conn()
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=_execute_backlog_row_payload(),
        created_by="judgment_brain",
        notify=True,
        now="2026-06-03T00:00:00Z",
    )

    recovery = observer_session.observer_command_consumer_recovery(
        conn,
        project_id="demo",
        now="2026-06-03T00:00:45Z",
    )
    persisted = observer_session.get_command(
        conn,
        project_id="demo",
        command_id=command["command_id"],
    )
    summary = observer_session.command_summary(
        conn,
        project_id="demo",
        now="2026-06-03T00:00:45Z",
    )

    assert recovery["schema_version"] == (
        observer_session.OBSERVER_COMMAND_CONSUMER_RECOVERY_SCHEMA_VERSION
    )
    assert recovery["recovery_required"] is True
    assert recovery["status"] == "blocked"
    assert recovery["classification"] == "no_active_consumer_session"
    assert recovery["latest_notified_command_age_sec"] == 45.0
    assert recovery["target_session_id"] == ""
    assert recovery["eligible_consumer_count"] == 0
    assert recovery["next_legal_action"]["tool"] == "observer_session_register"
    assert persisted["status"] == observer_session.COMMAND_STATUS_NOTIFIED
    assert summary["observer_consumer_recovery"]["classification"] == (
        "no_active_consumer_session"
    )
    projection = summary["observer_consumer_recovery"]["latest_notified_command"][
        "contract_handoff_projection"
    ]
    assert projection["source_of_truth"] == "Contract/Revision/Event"
    assert projection["projected_surface"] == "observer_command_queue"
    assert projection["contract_derived_status"] == observer_session.COMMAND_STATUS_NOTIFIED
    assert projection["stale"] is False
    assert projection["divergent"] is False


def test_active_consumer_claim_records_route_and_precheck_evidence():
    conn = _conn()
    payload = {
        **_execute_backlog_row_payload(),
        "prompt_contract_hash": "sha256:prompt-contract",
        "precheck_run_id": "precheck-route-action",
    }
    session = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-active",
        now="2026-06-03T00:00:30Z",
    )
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=payload,
        created_by="judgment_brain",
        notify=True,
        now="2026-06-03T00:00:00Z",
    )
    recovery = observer_session.observer_command_consumer_recovery(
        conn,
        project_id="demo",
        now="2026-06-03T00:00:45Z",
    )

    assert recovery["status"] == "action_required"
    assert recovery["classification"] == "eligible_consumer_available"
    assert recovery["eligible_session_ids"] == [session["session_id"]]
    assert observer_session.get_command(
        conn,
        project_id="demo",
        command_id=command["command_id"],
    )["status"] == observer_session.COMMAND_STATUS_NOTIFIED

    claimed = observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        now="2026-06-03T00:00:46Z",
    )
    evidence = claimed["command"]["result"]["observer_claim_evidence"]

    assert claimed["command"]["status"] == observer_session.COMMAND_STATUS_CLAIMED
    assert evidence["schema_version"] == (
        observer_session.OBSERVER_COMMAND_CLAIM_EVIDENCE_SCHEMA_VERSION
    )
    assert evidence["route_identity"]["route_context_hash"] == payload["route_context_hash"]
    assert evidence["route_identity"]["prompt_contract_hash"] == "sha256:prompt-contract"
    assert evidence["precheck_evidence"]["precheck_run_id"] == "precheck-route-action"
    assert evidence["precheck_evidence"]["present"] is True
    projection = evidence["contract_handoff_projection"]
    assert projection["source_of_truth"] == "Contract/Revision/Event"
    assert projection["projected_surface"] == "observer_command_queue"
    assert projection["contract_derived_status"] == observer_session.COMMAND_STATUS_CLAIMED
    assert projection["projection_watermark"] == "2026-06-03T00:00:46Z"
    assert projection["contract_hash"].startswith("sha256:")

    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        result={**_actual_startup_result(), **_first_progress_result()},
        now="2026-06-03T00:00:47Z",
    )

    assert completed["command"]["status"] == observer_session.COMMAND_STATUS_COMPLETED
    assert completed["command"]["result"]["observer_claim_evidence"] == evidence


def test_invalid_legacy_execute_payload_reports_validation_blocker_and_fails_on_claim():
    conn = _conn()
    session = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-active",
        now="2026-06-03T00:00:30Z",
    )
    command = _insert_legacy_execute_command(
        conn,
        payload={"backlog_id": "AC-ROUTE-HANDOFF"},
        created_at="2026-06-03T00:00:00Z",
    )

    recovery = observer_session.observer_command_consumer_recovery(
        conn,
        project_id="demo",
        now="2026-06-03T00:01:00Z",
    )

    assert recovery["status"] == "blocked"
    assert recovery["classification"] == "claim_validation_error"
    assert recovery["blocker"]["blocker_id"] == "execute_backlog_row_invalid_route_payload"
    assert "route_id" in recovery["blocker"]["missing_required_fields"]
    assert recovery["next_legal_action"]["tool"] == "observer_command_fail"

    claimed = observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        now="2026-06-03T00:01:01Z",
    )

    assert claimed["command"]["status"] == observer_session.COMMAND_STATUS_FAILED
    assert claimed["command"]["claimed_by_session_id"] == session["session_id"]
    assert claimed["command"]["error"] == "execute_backlog_row_invalid_route_payload"
    assert claimed["claim_blocker"]["missing_required_fields"] == (
        claimed["command"]["result"]["claim_blocker"]["missing_required_fields"]
    )


def test_targeted_notified_execute_diagnostic_reports_target_unavailable_then_claimable():
    conn = _conn()
    target = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-target",
        now="2026-06-03T00:00:00Z",
    )
    wrong = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-wrong",
        now="2026-06-03T00:02:30Z",
    )
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=_execute_backlog_row_payload(),
        target_session_id=target["session_id"],
        created_by="judgment_brain",
        notify=True,
        now="2026-06-03T00:00:01Z",
    )

    blocked = observer_session.observer_command_consumer_recovery(
        conn,
        project_id="demo",
        now="2026-06-03T00:03:00Z",
    )

    assert blocked["classification"] == "target_session_unavailable"
    assert blocked["target_session_id"] == target["session_id"]
    assert blocked["target_session"]["computed_status"] == "stale"
    assert blocked["eligible_consumer_count"] == 0
    assert blocked["next_legal_action"]["tool"] == "observer_session_heartbeat"
    assert [
        item for item in blocked["consumer_sessions"] if item["session_id"] == wrong["session_id"]
    ][0]["target_allowed"] is False

    observer_session.heartbeat_session(
        conn,
        project_id="demo",
        session_id=target["session_id"],
        session_token=target["session_token"],
        now="2026-06-03T00:03:01Z",
    )
    claimable = observer_session.observer_command_consumer_recovery(
        conn,
        project_id="demo",
        now="2026-06-03T00:03:02Z",
    )

    assert claimable["classification"] == "eligible_consumer_available"
    assert claimable["eligible_session_ids"] == [target["session_id"]]
    assert observer_session.get_command(
        conn,
        project_id="demo",
        command_id=command["command_id"],
    )["status"] == observer_session.COMMAND_STATUS_NOTIFIED


def test_api_command_list_exposes_observer_consumer_recovery_summary():
    from agent.governance import server

    conn = _conn()
    observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=_execute_backlog_row_payload(),
        created_by="judgment_brain",
        notify=True,
        now="2026-06-03T00:00:00Z",
    )
    ctx = SimpleNamespace(
        path_params={"project_id": "demo"},
        query={"status": "notified"},
        body={},
        get_project_id=lambda: "demo",
    )

    with patch("agent.governance.server.get_connection", return_value=conn):
        response = server.handle_observer_command_list(ctx)

    assert response["ok"] is True
    assert response["observer_consumer_recovery"] == (
        response["summary"]["observer_consumer_recovery"]
    )
    assert response["observer_consumer_recovery"]["schema_version"] == (
        observer_session.OBSERVER_COMMAND_CONSUMER_RECOVERY_SCHEMA_VERSION
    )


def test_mcp_observer_command_enqueue_schema_accepts_execute_backlog_row():
    from agent.mcp.tools import TOOLS

    enqueue_tool = next(tool for tool in TOOLS if tool.get("name") == "observer_command_enqueue")
    command_type = enqueue_tool["inputSchema"]["properties"]["command_type"]

    assert observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW in command_type["enum"]


def test_claim_requires_valid_token_and_project_match():
    conn = _conn()
    session = _register(conn)
    observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_CONFIRM_REQUIREMENT,
        payload={"raw_id": "raw-1"},
    )

    with pytest.raises(observer_session.ObserverAuthError):
        observer_session.claim_command(
            conn,
            project_id="demo",
            session_id=session["session_id"],
            session_token="wrong",
        )

    with pytest.raises(observer_session.ObserverPermissionError):
        observer_session.claim_command(
            conn,
            project_id="other",
            session_id=session["session_id"],
            session_token=session["session_token"],
        )


def test_claim_is_idempotent_for_same_session_and_rejects_double_claim():
    conn = _conn()
    session = _register(conn)
    other = _register(conn)
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_PAUSE_WORKER,
        payload={"task_id": "task-1"},
    )

    claimed = observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
    )
    repeated = observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
    )

    assert claimed["command"]["status"] == observer_session.COMMAND_STATUS_CLAIMED
    assert repeated["command"]["command_id"] == command["command_id"]

    with pytest.raises(observer_session.ObserverCommandConflict):
        observer_session.claim_command(
            conn,
            project_id="demo",
            session_id=other["session_id"],
            session_token=other["session_token"],
            command_id=command["command_id"],
        )


def test_stale_claimed_command_can_be_taken_over_by_fallback_session():
    conn = _conn()
    owner = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-owner",
        now="2026-06-03T00:00:00Z",
    )
    fallback = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-fallback",
        now="2026-06-03T00:03:00Z",
    )
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=_execute_backlog_row_payload(),
        created_by="judgment_brain",
        now="2026-06-03T00:00:01Z",
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=owner["session_id"],
        session_token=owner["session_token"],
        command_id=command["command_id"],
        now="2026-06-03T00:00:02Z",
    )

    takeover = observer_session.takeover_command(
        conn,
        project_id="demo",
        session_id=fallback["session_id"],
        session_token=fallback["session_token"],
        command_id=command["command_id"],
        reason="fallback observer resolves stale claimed command",
        now="2026-06-03T00:03:01Z",
    )

    assert takeover["command"]["status"] == observer_session.COMMAND_STATUS_CLAIMED
    assert takeover["command"]["claimed_by_session_id"] == fallback["session_id"]
    assert takeover["takeover"]["previous_session_id"] == owner["session_id"]
    assert takeover["takeover"]["previous_session_status"] == "stale"

    observer_session.heartbeat_session(
        conn,
        project_id="demo",
        session_id=owner["session_id"],
        session_token=owner["session_token"],
        now="2026-06-03T00:03:02Z",
    )
    with pytest.raises(observer_session.ObserverPermissionError, match="same claimed session"):
        observer_session.complete_command(
            conn,
            project_id="demo",
            session_id=owner["session_id"],
            session_token=owner["session_token"],
            command_id=command["command_id"],
            result={"ok": True},
            now="2026-06-03T00:03:03Z",
        )

    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=fallback["session_id"],
        session_token=fallback["session_token"],
        command_id=command["command_id"],
        result={
            **_actual_startup_result(),
            **_first_progress_result(),
            "takeover": takeover["takeover"],
        },
        now="2026-06-03T00:03:04Z",
    )
    assert completed["command"]["status"] == observer_session.COMMAND_STATUS_COMPLETED
    assert completed["command"]["result"]["takeover"]["previous_session_status"] == "stale"


def test_active_claimed_command_cannot_be_taken_over():
    conn = _conn()
    owner = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-owner",
        now="2026-06-03T00:00:00Z",
    )
    fallback = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-fallback",
        now="2026-06-03T00:00:00Z",
    )
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_CANCEL_WORKER,
        payload={"task_id": "task-1"},
        now="2026-06-03T00:00:01Z",
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=owner["session_id"],
        session_token=owner["session_token"],
        command_id=command["command_id"],
        now="2026-06-03T00:00:02Z",
    )

    with pytest.raises(observer_session.ObserverCommandConflict, match="not stale: active"):
        observer_session.takeover_command(
            conn,
            project_id="demo",
            session_id=fallback["session_id"],
            session_token=fallback["session_token"],
            command_id=command["command_id"],
            reason="fallback observer tries to steal active command",
            now="2026-06-03T00:00:03Z",
        )


def test_new_execute_claim_with_active_owner_cannot_be_taken_over_before_startup_timeout():
    conn = _conn()
    owner = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-owner",
        now="2026-06-03T00:00:00Z",
    )
    fallback = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-fallback",
        now="2026-06-03T00:00:00Z",
    )
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=_execute_backlog_row_payload(),
        now="2026-06-03T00:00:01Z",
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=owner["session_id"],
        session_token=owner["session_token"],
        command_id=command["command_id"],
        now="2026-06-03T00:00:02Z",
    )
    observer_session.heartbeat_session(
        conn,
        project_id="demo",
        session_id=owner["session_id"],
        session_token=owner["session_token"],
        now="2026-06-03T00:00:30Z",
    )

    with pytest.raises(observer_session.ObserverCommandConflict, match="not stale: active"):
        observer_session.takeover_command(
            conn,
            project_id="demo",
            session_id=fallback["session_id"],
            session_token=fallback["session_token"],
            command_id=command["command_id"],
            reason="fallback observer tries to steal new execute command",
            now="2026-06-03T00:00:31Z",
        )


def test_active_owner_old_execute_without_startup_can_be_taken_over_and_failed():
    conn = _conn()
    owner = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-owner",
        now="2026-06-03T00:00:00Z",
    )
    fallback = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-fallback",
        now="2026-06-03T00:03:00Z",
    )
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=_execute_backlog_row_payload(),
        now="2026-06-03T00:00:01Z",
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=owner["session_id"],
        session_token=owner["session_token"],
        command_id=command["command_id"],
        now="2026-06-03T00:00:02Z",
    )
    observer_session.heartbeat_session(
        conn,
        project_id="demo",
        session_id=owner["session_id"],
        session_token=owner["session_token"],
        now="2026-06-03T00:03:00Z",
    )

    takeover = observer_session.takeover_command(
        conn,
        project_id="demo",
        session_id=fallback["session_id"],
        session_token=fallback["session_token"],
        command_id=command["command_id"],
        reason="fallback observer resolves execute command with no startup evidence",
        now="2026-06-03T00:03:01Z",
    )
    persisted_takeover = observer_session.get_command(
        conn,
        project_id="demo",
        command_id=command["command_id"],
    )
    failed = observer_session.fail_command(
        conn,
        project_id="demo",
        session_id=fallback["session_id"],
        session_token=fallback["session_token"],
        command_id=command["command_id"],
        error="claimed execute command timed out before mf_subagent_startup",
        result={"ok": False},
        now="2026-06-03T00:03:02Z",
    )

    assert takeover["command"]["claimed_by_session_id"] == fallback["session_id"]
    assert takeover["takeover"]["previous_session_id"] == owner["session_id"]
    assert takeover["takeover"]["previous_session_status"] == (
        observer_session.CLAIMED_TO_STARTUP_TIMEOUT_STATUS
    )
    assert takeover["takeover"]["timeout_sec"] == observer_session.CLAIMED_TO_STARTUP_TIMEOUT_SEC
    assert takeover["command"]["result"]["takeover_status"]["status"] == (
        observer_session.CLAIMED_TO_STARTUP_TIMEOUT_STATUS
    )
    assert persisted_takeover["result"]["takeover"]["previous_session_status"] == (
        observer_session.CLAIMED_TO_STARTUP_TIMEOUT_STATUS
    )
    assert failed["command"]["status"] == observer_session.COMMAND_STATUS_FAILED
    assert failed["command"]["claimed_by_session_id"] == fallback["session_id"]
    assert failed["command"]["result"]["takeover"]["previous_session_status"] == (
        observer_session.CLAIMED_TO_STARTUP_TIMEOUT_STATUS
    )
    assert failed["command"]["result"]["takeover_status"]["status"] == (
        observer_session.CLAIMED_TO_STARTUP_TIMEOUT_STATUS
    )


def test_active_owner_old_execute_with_startup_and_progress_cannot_be_taken_over():
    conn = _conn()
    owner = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-owner",
        now="2026-06-03T00:00:00Z",
    )
    fallback = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-fallback",
        now="2026-06-03T00:03:00Z",
    )
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=_execute_backlog_row_payload(),
        now="2026-06-03T00:00:01Z",
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=owner["session_id"],
        session_token=owner["session_token"],
        command_id=command["command_id"],
        now="2026-06-03T00:00:02Z",
    )
    conn.execute(
        """UPDATE observer_command_queue
              SET result_json = ?
            WHERE command_id = ?""",
        (
            observer_session._json_dumps(
                {**_actual_startup_result(), **_first_progress_result()}
            ),
            command["command_id"],
        ),
    )
    conn.commit()
    observer_session.heartbeat_session(
        conn,
        project_id="demo",
        session_id=owner["session_id"],
        session_token=owner["session_token"],
        now="2026-06-03T00:03:00Z",
    )

    with pytest.raises(observer_session.ObserverCommandConflict, match="not stale: active"):
        observer_session.takeover_command(
            conn,
            project_id="demo",
            session_id=fallback["session_id"],
            session_token=fallback["session_token"],
            command_id=command["command_id"],
            reason="fallback observer tries to steal started worker",
            now="2026-06-03T00:03:01Z",
        )


def test_active_owner_execute_with_startup_but_no_progress_times_out_for_takeover():
    conn = _conn()
    owner = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-owner",
        now="2026-06-03T00:00:00Z",
    )
    fallback = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-fallback",
        now="2026-06-03T00:03:00Z",
    )
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=_execute_backlog_row_payload(),
        now="2026-06-03T00:00:01Z",
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=owner["session_id"],
        session_token=owner["session_token"],
        command_id=command["command_id"],
        now="2026-06-03T00:00:02Z",
    )
    conn.execute(
        """UPDATE observer_command_queue
              SET result_json = ?
            WHERE command_id = ?""",
        (
            observer_session._json_dumps(_actual_startup_result()),
            command["command_id"],
        ),
    )
    conn.commit()
    observer_session.heartbeat_session(
        conn,
        project_id="demo",
        session_id=owner["session_id"],
        session_token=owner["session_token"],
        now="2026-06-03T00:03:00Z",
    )

    takeover = observer_session.takeover_command(
        conn,
        project_id="demo",
        session_id=fallback["session_id"],
        session_token=fallback["session_token"],
        command_id=command["command_id"],
        reason="fallback observer resolves started worker with no observable progress",
        now="2026-06-03T00:03:01Z",
    )

    assert takeover["takeover"]["previous_session_status"] == (
        observer_session.CLAIMED_TO_PROGRESS_TIMEOUT_STATUS
    )
    assert takeover["takeover"]["timeout_kind"] == "no_progress_timeout"
    assert takeover["takeover"]["startup_evidence"] == "present"
    assert takeover["takeover"]["progress_evidence"] == "missing"
    result = takeover["command"]["result"]
    assert result["no_progress_timeout"]["timeout_kind"] == "no_progress_timeout"
    assert result["no_progress_timeout"]["timeline_event"]["event_kind"] == (
        "no_progress_timeout"
    )
    assert result["progress_watchdog"]["present"] is False


def test_active_owner_execute_with_timeline_progress_does_not_time_out():
    conn = _conn()
    task_timeline.ensure_schema(conn)
    owner = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-owner",
        now="2026-06-03T00:00:00Z",
    )
    fallback = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-fallback",
        now="2026-06-03T00:03:00Z",
    )
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=_execute_backlog_row_payload(),
        now="2026-06-03T00:00:01Z",
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=owner["session_id"],
        session_token=owner["session_token"],
        command_id=command["command_id"],
        now="2026-06-03T00:00:02Z",
    )
    startup = _actual_startup_result()
    startup["startup_gate"].update(
        {
            "task_id": "mf-sub-task-1",
            "parent_task_id": "observer-task-1",
            "runtime_context_id": "mfrctx-progress-1",
        }
    )
    conn.execute(
        """UPDATE observer_command_queue
              SET result_json = ?
            WHERE command_id = ?""",
        (
            observer_session._json_dumps(startup),
            command["command_id"],
        ),
    )
    task_timeline.record_event(
        conn,
        project_id="demo",
        backlog_id=_execute_backlog_row_payload()["backlog_id"],
        task_id="mf-sub-task-1",
        attempt_num=1,
        event_type="mf_subagent.startup",
        phase="startup_gate",
        event_kind="mf_subagent_startup",
        status="passed",
        payload={
            "mf_subagent_startup_gate": {
                **startup["startup_gate"],
                "runtime_context_id": "mfrctx-progress-1",
            }
        },
    )
    task_timeline.record_event(
        conn,
        project_id="demo",
        backlog_id=_execute_backlog_row_payload()["backlog_id"],
        task_id="mf-sub-task-1",
        attempt_num=1,
        event_type="implementation.progress",
        phase="implementation",
        event_kind="implementation",
        status="accepted",
        payload={
            "task_id": "mf-sub-task-1",
            "runtime_context_id": "mfrctx-progress-1",
            "fence_token": "fence-1",
            "changed_files": ["agent/example.py"],
        },
    )
    conn.commit()
    observer_session.heartbeat_session(
        conn,
        project_id="demo",
        session_id=owner["session_id"],
        session_token=owner["session_token"],
        now="2026-06-03T00:03:00Z",
    )

    with pytest.raises(observer_session.ObserverCommandConflict, match="not stale: active"):
        observer_session.takeover_command(
            conn,
            project_id="demo",
            session_id=fallback["session_id"],
            session_token=fallback["session_token"],
            command_id=command["command_id"],
            reason="fallback observer should not steal a worker with timeline progress",
            now="2026-06-03T00:03:01Z",
        )


def test_missing_owner_claimed_command_can_be_taken_over():
    conn = _conn()
    fallback = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-fallback",
        now="2026-06-03T00:00:00Z",
    )
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_PAUSE_WORKER,
        payload={"task_id": "task-1"},
        now="2026-06-03T00:00:01Z",
    )
    conn.execute(
        """UPDATE observer_command_queue
              SET status = ?, claimed_by_session_id = ?, claimed_at = ?
            WHERE command_id = ?""",
        (
            observer_session.COMMAND_STATUS_CLAIMED,
            "obs-missing",
            "2026-06-03T00:00:02Z",
            command["command_id"],
        ),
    )
    conn.commit()

    takeover = observer_session.takeover_command(
        conn,
        project_id="demo",
        session_id=fallback["session_id"],
        session_token=fallback["session_token"],
        command_id=command["command_id"],
        reason="fallback observer resolves missing owner",
        now="2026-06-03T00:00:03Z",
    )

    assert takeover["takeover"]["previous_session_id"] == "obs-missing"
    assert takeover["takeover"]["previous_session_status"] == "missing"
    assert takeover["command"]["claimed_by_session_id"] == fallback["session_id"]


def test_complete_and_fail_require_same_claimed_session():
    conn = _conn()
    session = _register(conn)
    other = _register(conn)
    complete_command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_CONTINUE_WORKER,
        payload={"task_id": "task-1"},
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=complete_command["command_id"],
    )

    with pytest.raises(observer_session.ObserverPermissionError, match="same claimed session"):
        observer_session.complete_command(
            conn,
            project_id="demo",
            session_id=other["session_id"],
            session_token=other["session_token"],
            command_id=complete_command["command_id"],
            result={"ok": True},
        )

    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=complete_command["command_id"],
        result={"ok": True},
    )
    assert completed["command"]["status"] == observer_session.COMMAND_STATUS_COMPLETED

    fail_command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_CANCEL_WORKER,
        payload={"task_id": "task-2"},
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=other["session_id"],
        session_token=other["session_token"],
        command_id=fail_command["command_id"],
    )
    with pytest.raises(observer_session.ObserverPermissionError, match="same claimed session"):
        observer_session.fail_command(
            conn,
            project_id="demo",
            session_id=session["session_id"],
            session_token=session["session_token"],
            command_id=fail_command["command_id"],
            error="wrong owner",
        )

    failed = observer_session.fail_command(
        conn,
        project_id="demo",
        session_id=other["session_id"],
        session_token=other["session_token"],
        command_id=fail_command["command_id"],
        error="cancel rejected",
    )
    assert failed["command"]["status"] == observer_session.COMMAND_STATUS_FAILED
    assert failed["command"]["error"] == "cancel rejected"


def test_actor_self_report_does_not_authorize_command_claim():
    from agent.governance import server

    conn = _conn()
    ctx = SimpleNamespace(
        path_params={"project_id": "demo"},
        query={},
        body={"actor": "observer"},
        get_project_id=lambda: "demo",
    )

    with patch("agent.governance.server.get_connection", return_value=conn):
        code, payload = server.handle_observer_command_claim(ctx)

    assert code == 401
    assert payload["error"] == "observer_auth_failed"


def test_api_enqueue_publishes_reminder_only_event_and_preserves_command_payload():
    from agent.governance import event_bus, server

    conn = _conn()
    captured: list[dict] = []

    def on_pending(payload: dict) -> None:
        captured.append(payload)

    ctx = SimpleNamespace(
        path_params={"project_id": "demo"},
        query={},
        body={
            "command_type": observer_session.COMMAND_TYPE_ANALYZE_REQUIREMENTS,
            "payload": {"raw_id": "raw-1", "source": "dashboard"},
            "created_by": "dashboard",
        },
        get_project_id=lambda: "demo",
    )

    bus = event_bus.get_event_bus()
    bus.subscribe("observer_command_pending", on_pending)
    try:
        with patch("agent.governance.server.get_connection", return_value=conn):
            code, payload = server.handle_observer_command_enqueue(ctx)
    finally:
        bus.unsubscribe("observer_command_pending", on_pending)

    expected_reminder = {
        "kind": "observer_command_pending",
        "project_id": "demo",
        "message": "pending observer commands exist; call observer_command_next",
        "payload_included": False,
        "next_action": {
            "tool": "observer_command_next",
            "description": "claim the next pending observer command",
        },
    }
    command = payload["observer_command"]

    assert code == 201
    assert payload["hook_reminder"] == expected_reminder
    assert captured == [expected_reminder]
    assert set(captured[0]) == {
        "kind",
        "project_id",
        "message",
        "payload_included",
        "next_action",
    }
    assert "raw_id" not in captured[0]
    assert "command_type" not in captured[0]
    assert "source" not in captured[0]
    assert "command_id" not in captured[0]
    assert command["payload"] == {"raw_id": "raw-1", "source": "dashboard"}
    assert command["status"] == observer_session.COMMAND_STATUS_NOTIFIED
    assert command["notified_at"]

    session = _register(conn)
    claimed = observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
    )
    assert claimed["command"]["status"] == observer_session.COMMAND_STATUS_CLAIMED


def test_analyze_complete_projects_raw_requirement_to_confirmation():
    conn = _conn()
    session = _register(conn)
    raw = raw_requirement.create_raw_requirement(
        conn,
        project_id="demo",
        raw_text="Let users drag captured requirements into execution",
        source="dashboard",
    )
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_ANALYZE_REQUIREMENTS,
        payload={"raw_id": raw["raw_id"]},
        created_by="dashboard",
    )

    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
    )
    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        result={
            "raw_id": raw["raw_id"],
            "ai_interpretation": "User wants a queue promotion control.",
            "proposed_backlog_mapping": {
                "bug_id": "REQ-QUEUE-PROMOTE",
                "title": "Promote raw requirements to execution queue",
            },
        },
    )

    updated = raw_requirement.get_raw_requirement(conn, project_id="demo", raw_id=raw["raw_id"])
    assert completed["command"]["status"] == observer_session.COMMAND_STATUS_COMPLETED
    assert updated["status"] == raw_requirement.STATUS_NEEDS_CONFIRMATION
    assert "AI interpretation" in updated["note"]
    assert "REQ-QUEUE-PROMOTE" in updated["note"]


def test_api_takeover_rejects_active_owner_then_allows_closed_owner_terminal_fail():
    from agent.governance import server

    conn = _conn()

    def ctx(path_params: dict, body: dict | None = None):
        return SimpleNamespace(
            path_params=path_params,
            query={},
            body=body or {},
            get_project_id=lambda: "demo",
        )

    with (
        patch("agent.governance.server.get_connection", return_value=conn),
        patch("agent.governance.server.audit_service.record", return_value={"ok": True}),
    ):
        owner_code, owner = server.handle_observer_session_register(
            ctx(
                {"project_id": "demo"},
                {
                    "observer_kind": "codex",
                    "session_id": "obs-owner",
                    "session_label": "owner",
                },
            )
        )
        fallback_code, fallback = server.handle_observer_session_register(
            ctx(
                {"project_id": "demo"},
                {
                    "observer_kind": "codex",
                    "session_id": "obs-fallback",
                    "session_label": "fallback",
                },
            )
        )
        enqueue_code, enqueue = server.handle_observer_command_enqueue(
            ctx(
                {"project_id": "demo"},
                {
                    "command_type": observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
                    "payload": _execute_backlog_row_payload(),
                    "created_by": "judgment_brain",
                },
            )
        )
        command_id = enqueue["observer_command"]["command_id"]

        claimed = server.handle_observer_command_claim(
            ctx(
                {"project_id": "demo"},
                {
                    "session_id": owner["session_id"],
                    "session_token": owner["session_token"],
                    "command_id": command_id,
                },
            )
        )
        active_takeover_code, active_takeover = server.handle_observer_command_takeover(
            ctx(
                {"project_id": "demo", "command_id": command_id},
                {
                    "session_id": fallback["session_id"],
                    "session_token": fallback["session_token"],
                    "reason": "fallback observer tries to steal active command",
                },
            )
        )

        closed = server.handle_observer_session_close(
            ctx(
                {"project_id": "demo", "session_id": owner["session_id"]},
                {
                    "session_token": owner["session_token"],
                },
            )
        )
        takeover = server.handle_observer_command_takeover(
            ctx(
                {"project_id": "demo", "command_id": command_id},
                {
                    "session_id": fallback["session_id"],
                    "session_token": fallback["session_token"],
                    "reason": "fallback observer resolves closed owner",
                },
            )
        )
        failed = server.handle_observer_command_fail(
            ctx(
                {"project_id": "demo", "command_id": command_id},
                {
                    "session_id": fallback["session_id"],
                    "session_token": fallback["session_token"],
                    "error": "fallback completed stale owner takeover",
                    "result": {"takeover": takeover["takeover"]},
                },
            )
        )

    assert owner_code == 201
    assert fallback_code == 201
    assert enqueue_code == 201
    assert claimed["command"]["status"] == observer_session.COMMAND_STATUS_CLAIMED
    assert claimed["command"]["claimed_by_session_id"] == owner["session_id"]

    assert active_takeover_code == 409
    assert active_takeover["error"] == "observer_command_conflict"
    assert "not stale: active" in active_takeover["message"]

    assert closed["status"] == observer_session.SESSION_STATUS_CLOSED
    assert takeover["ok"] is True
    assert takeover["observer_session_id"] == fallback["session_id"]
    assert takeover["command"]["status"] == observer_session.COMMAND_STATUS_CLAIMED
    assert takeover["command"]["claimed_by_session_id"] == fallback["session_id"]
    assert takeover["takeover"]["previous_session_id"] == owner["session_id"]
    assert takeover["takeover"]["previous_session_status"] == observer_session.SESSION_STATUS_CLOSED

    assert failed["observer_session_id"] == fallback["session_id"]
    assert failed["command"]["status"] == observer_session.COMMAND_STATUS_FAILED
    assert failed["command"]["claimed_by_session_id"] == fallback["session_id"]
    assert failed["command"]["result"]["takeover"]["previous_session_status"] == (
        observer_session.SESSION_STATUS_CLOSED
    )


def test_api_smoke_capture_enqueue_claim_complete_reflects_project_inbox():
    from agent.governance import server

    conn = _conn()

    def ctx(path_params: dict, body: dict | None = None):
        return SimpleNamespace(
            path_params=path_params,
            query={},
            body=body or {},
            get_project_id=lambda: "demo",
        )

    with patch("agent.governance.server.get_connection", return_value=conn):
        register_code, register_payload = server.handle_observer_session_register(
            ctx(
                {"project_id": "demo"},
                {"observer_kind": "codex", "session_label": "smoke"},
            )
        )
        create_code, create_payload = server.handle_project_raw_requirement_create(
            ctx(
                {"project_id": "demo"},
                {
                    "raw_text": "Add one button that asks the observer to analyze this",
                    "source": "dashboard_project_inbox",
                    "actor": "dashboard",
                },
            )
        )
        raw_id = create_payload["raw_requirement"]["raw_id"]
        enqueue_code, enqueue_payload = server.handle_observer_command_enqueue(
            ctx(
                {"project_id": "demo"},
                {
                    "command_type": "analyze_requirements",
                    "payload": {"raw_id": raw_id, "source": "project_inbox"},
                    "created_by": "dashboard",
                },
            )
        )
        command_id = enqueue_payload["observer_command"]["command_id"]
        claim_payload = server.handle_observer_command_claim(
            ctx(
                {"project_id": "demo"},
                {
                    "session_id": register_payload["session_id"],
                    "session_token": register_payload["session_token"],
                    "command_id": command_id,
                },
            )
        )
        complete_payload = server.handle_observer_command_complete(
            ctx(
                {"project_id": "demo", "command_id": command_id},
                {
                    "session_id": register_payload["session_id"],
                    "session_token": register_payload["session_token"],
                    "result": {
                        "raw_id": raw_id,
                        "ai_interpretation": "User wants dashboard command queue wiring.",
                        "proposed_backlog_mapping": {
                            "bug_id": "SMOKE-OBSERVER-COMMAND",
                            "title": "Wire AI Analyze to observer command queue",
                        },
                    },
                },
            )
        )
        inbox = server.handle_project_inbox(ctx({"project_id": "demo"}))

    assert register_code == 201
    assert create_code == 201
    assert enqueue_code == 201
    assert claim_payload["command"]["status"] == observer_session.COMMAND_STATUS_CLAIMED
    assert complete_payload["command"]["status"] == observer_session.COMMAND_STATUS_COMPLETED
    assert inbox["lanes"]["raw_inbox"]["count"] == 0
    assert inbox["lanes"]["needs_confirmation"]["count"] == 1
    assert inbox["observer"]["connected"] is True
    assert inbox["observer_commands"]["counts"]["completed"] == 1
