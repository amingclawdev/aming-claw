from __future__ import annotations

import json

from agent.ai_invocation import RoutePromptContract
from agent.governance.parallel_branch_runtime import (
    BranchTaskRuntimeContext,
    STATE_WORKTREE_READY,
    branch_runtime_allocation_evidence,
)
from agent.observer_runtime import (
    EXECUTE_BACKLOG_ROW_COMMAND_TYPE,
    OBSERVER_POLL_LOOP_SCHEMA_VERSION,
    OBSERVER_POLL_SCHEMA_VERSION,
    OBSERVER_POLL_TIMELINE_PAYLOAD_SCHEMA_VERSION,
    ObserverRuntimeTextPrepareRequest,
    ObserverPollLoopConfig,
    ObserverPollRequest,
    build_observer_runtime_text_context,
    build_observer_poll_loop_metadata,
    build_observer_poll_plan,
    observer_poll_timeline_payload,
)


def _execute_backlog_command(payload: dict | None = None) -> dict:
    return {
        "command_id": "cmd-route-1",
        "command_type": EXECUTE_BACKLOG_ROW_COMMAND_TYPE,
        "status": "claimed",
        "payload": payload
        or {
            "backlog_id": "AC-ROUTE-HANDOFF",
            "route_id": "route-20260603-test",
            "route_context_hash": "sha256:route",
            "prompt_contract_id": "rprompt-test",
            "prompt_contract_hash": "sha256:prompt",
            "route_token_ref": "route-token-ref",
            "visible_injection_manifest_hash": "sha256:visible",
        },
    }


def test_runtime_text_prepare_accepts_supplied_registered_allocation_evidence(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    worktree = tmp_path / ".worktrees" / "worker-a1" / "task-a1"
    allocation_context = BranchTaskRuntimeContext(
        project_id="aming-claw",
        task_id="task-a1",
        runtime_context_id="mfrctx-runtime-text-a1",
        backlog_id="AC-RUNTIME-TEXT-A1",
        root_task_id="AC-RUNTIME-TEXT-A1",
        stage_task_id="task-a1",
        stage_type="mf_sub",
        worker_id="worker-a1",
        worker_slot_id="worker-a1",
        fence_token="fence-runtime-text-a1",
        branch_ref="refs/heads/codex/task-a1",
        worktree_id="wt-task-a1",
        worktree_path=str(worktree),
        base_commit="base-a1",
        target_head_commit="target-a1",
        merge_queue_id="mq-runtime-text-a1",
        status=STATE_WORKTREE_READY,
    )
    allocation_evidence = branch_runtime_allocation_evidence(
        allocation_context,
        source_ref="/api/graph-governance/aming-claw/parallel-branches/allocate",
    )

    prepared = build_observer_runtime_text_context(
        ObserverRuntimeTextPrepareRequest(
            project_id="aming-claw",
            backlog_id="AC-RUNTIME-TEXT-A1",
            route=RoutePromptContract(
                route_context_hash="sha256:route-a1",
                prompt_contract_id="rprompt-a1",
                prompt_contract_hash="sha256:prompt-a1",
            ),
            main_worktree=str(main),
            owned_files=("agent/observer_runtime.py",),
            task_id="task-a1",
            parent_task_id="AC-RUNTIME-TEXT-A1",
            worker_id="worker-a1",
            graph_trace_ids=("gqt-runtime-text-a1",),
            branch_runtime_evidence=allocation_evidence,
            route_id="route-a1",
            visible_injection_manifest_hash="sha256:visible-a1",
        )
    )

    assert allocation_evidence["status"] == STATE_WORKTREE_READY
    assert allocation_evidence["registered"] is True
    assert prepared["ok"] is True
    assert prepared["status"] == "prepared"
    assert prepared["runtime_context_id"] == "mfrctx-runtime-text-a1"
    assert prepared["runtime_context"]["worktree_path"] == str(worktree)
    assert prepared["runtime_context"]["fence_token"] == "fence-runtime-text-a1"
    assert prepared["runtime_context"]["base_commit"] == "base-a1"
    assert prepared["runtime_context"]["target_head_commit"] == "target-a1"
    assert prepared["runtime_context"]["merge_queue_id"] == "mq-runtime-text-a1"
    assert prepared["branch_runtime_evidence"]["status"] == STATE_WORKTREE_READY
    assert prepared["branch_runtime_evidence"]["registered"] is True
    assert prepared["dispatch_gate_validation"]["allowed"] is True
    assert prepared["startup_intent_event"]["artifact_refs"]["runtime_context_id"] == (
        "mfrctx-runtime-text-a1"
    )


def test_build_observer_poll_plan_turns_claimed_command_into_dry_run_plan(tmp_path):
    result = build_observer_poll_plan(
        ObserverPollRequest(
            project_id="aming-claw",
            observer_session_id="obs-1",
            command=_execute_backlog_command(),
            workspace=str(tmp_path),
            main_worktree=str(tmp_path),
        )
    )

    assert result["ok"] is True
    assert result["schema_version"] == OBSERVER_POLL_SCHEMA_VERSION
    assert result["status"] == "planned"
    assert result["execute"] is False
    assert result["calls_models"] is False
    assert result["service_manager_required"] is False
    assert result["executor_worker_required"] is False
    assert result["uses_task_create"] is False
    assert result["payload_free_reminder"] is True
    assert result["reminder_payload_required"] is False
    assert result["observer_command_id"] == "cmd-route-1"
    assert result["backlog_id"] == "AC-ROUTE-HANDOFF"
    assert result["route_identity"]["route_context_hash"] == "sha256:route"
    assert result["route_identity"]["prompt_contract_id"] == "rprompt-test"
    assert result["route_identity"]["visible_injection_manifest_hash"] == "sha256:visible"
    assert result["route_identity"]["raw_private_context_exposed"] is False
    assert result["observer_run"]["invocation"]["calls_models"] is False


def test_observer_poll_timeline_payload_preserves_route_identity_from_plan_and_result(tmp_path):
    command = _execute_backlog_command()
    plan = build_observer_poll_plan(
        ObserverPollRequest(
            project_id="aming-claw",
            observer_session_id="obs-1",
            command=command,
            workspace=str(tmp_path),
            main_worktree=str(tmp_path),
        )
    )
    result = {
        "observer_command_id": "cmd-route-1",
        "backlog_id": "AC-ROUTE-HANDOFF",
        "route_id": "route-from-result",
        "route_context_hash": "sha256:result-route",
        "prompt_contract_id": "rprompt-result",
        "visible_injection_manifest_hash": "sha256:result-visible",
        "execute": False,
        "calls_models": False,
        "service_manager_required": False,
        "executor_worker_required": False,
        "uses_task_create": False,
    }

    payload = observer_poll_timeline_payload(
        observer_command_id="cmd-route-1",
        command=command,
        plan=plan,
        result=result,
        event="complete",
    )

    assert payload["schema_version"] == OBSERVER_POLL_TIMELINE_PAYLOAD_SCHEMA_VERSION
    assert payload["event"] == "complete"
    assert payload["observer_command_id"] == "cmd-route-1"
    assert payload["backlog_id"] == "AC-ROUTE-HANDOFF"
    assert payload["route_id"] == "route-20260603-test"
    assert payload["route_context_hash"] == "sha256:route"
    assert payload["prompt_contract_id"] == "rprompt-test"
    assert payload["visible_injection_manifest_hash"] == "sha256:visible"
    assert payload["execute"] is False
    assert payload["calls_models"] is False
    assert payload["service_manager_required"] is False
    assert payload["executor_worker_required"] is False
    assert payload["uses_task_create"] is False
    assert payload["payload_free_reminder"] is True
    assert payload["reminder_payload_required"] is False


def test_observer_poll_timeline_payload_reads_payload_json_for_reconnect():
    command = _execute_backlog_command()
    command["payload_json"] = json_payload = json.dumps(command.pop("payload"))

    payload = observer_poll_timeline_payload(
        observer_command_id="cmd-route-1",
        command=command,
        event="claim",
    )

    assert json_payload
    assert payload["observer_command_id"] == "cmd-route-1"
    assert payload["backlog_id"] == "AC-ROUTE-HANDOFF"
    assert payload["route_id"] == "route-20260603-test"
    assert payload["route_context_hash"] == "sha256:route"
    assert payload["prompt_contract_id"] == "rprompt-test"
    assert payload["visible_injection_manifest_hash"] == "sha256:visible"
    assert payload["execute"] is False


def test_build_observer_poll_plan_rejects_missing_route_payload_fields():
    result = build_observer_poll_plan(
        ObserverPollRequest(
            project_id="aming-claw",
            observer_session_id="obs-1",
            command=_execute_backlog_command({"backlog_id": "AC-MISSING-ROUTE"}),
        )
    )

    assert result["ok"] is False
    assert result["status"] == "rejected"
    assert result["calls_models"] is False
    assert result["service_manager_required"] is False
    assert "route_context_hash" in result["missing"]
    assert "prompt_contract_id" in result["missing"]


def test_build_observer_poll_plan_reports_empty_queue_without_runtime_dependencies():
    result = build_observer_poll_plan(
        ObserverPollRequest(project_id="aming-claw", observer_session_id="obs-1")
    )

    assert result["ok"] is True
    assert result["status"] == "empty"
    assert result["empty"] is True
    assert result["calls_models"] is False
    assert result["service_manager_required"] is False
    assert result["executor_worker_required"] is False
    assert result["payload_free_reminder"] is True
    assert result["reminder_payload_required"] is False


def test_build_observer_poll_plan_rejects_non_execute_command_in_standalone_mode():
    result = build_observer_poll_plan(
        ObserverPollRequest(
            project_id="aming-claw",
            observer_session_id="obs-1",
            command={
                "command_id": "cmd-other",
                "command_type": "pause_worker",
                "status": "claimed",
                "payload": {"task_id": "task-1"},
            },
        )
    )

    assert result["ok"] is False
    assert result["status"] == "rejected"
    assert result["observer_command_id"] == "cmd-other"
    assert result["service_manager_required"] is False
    assert "execute_backlog_row" in result["error"]


def test_build_observer_poll_plan_marks_execute_timeout_terminal(monkeypatch, tmp_path):
    from agent.ai_invocation import AIInvocationResult

    def fake_invoke_ai(request):
        return AIInvocationResult(
            request=request,
            status="blocked",
            output_text="",
            error="fixture invocation timed out",
            command=["fixture", "exec"],
            returncode=124,
            provider_backed=False,
            calls_models=False,
            auth_status="cli_timeout",
        )

    monkeypatch.setattr("agent.observer_runtime.invoke_ai", fake_invoke_ai)

    result = build_observer_poll_plan(
        ObserverPollRequest(
            project_id="aming-claw",
            observer_session_id="obs-1",
            command=_execute_backlog_command(),
            provider="fixture",
            backend_mode="fixture",
            workspace=str(tmp_path),
            main_worktree=str(tmp_path),
            timeout_sec=1,
        ),
        execute=True,
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["terminal_dispatch_blocker"] is True
    assert result["command_projection_status"] == "failed"
    assert result["terminal_contract_projection"]["command_projection_status"] == "failed"
    assert result["failure_evidence"]["observer_command_id"] == "cmd-route-1"
    assert result["failure_evidence"]["read_receipt_recorded"] is False
    assert result["failure_evidence"]["route_context_hash"] == "sha256:route"


def test_build_observer_poll_loop_metadata_is_bounded_and_dependency_free():
    metadata = build_observer_poll_loop_metadata(
        ObserverPollLoopConfig(
            watch=True,
            max_commands=2,
            idle_timeout_sec=0,
            poll_interval_sec=-1,
        )
    )

    assert metadata["schema_version"] == OBSERVER_POLL_LOOP_SCHEMA_VERSION
    assert metadata["watch"] is True
    assert metadata["once"] is False
    assert metadata["effective_max_commands"] == 2
    assert metadata["idle_timeout_sec"] == 0
    assert metadata["poll_interval_sec"] == 0
    assert metadata["payload_free_reminder"] is True
    assert metadata["reminder_payload_required"] is False
    assert metadata["service_manager_required"] is False
    assert metadata["executor_worker_required"] is False
    assert metadata["uses_task_create"] is False
