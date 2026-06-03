from __future__ import annotations

import json

from agent.observer_runtime import (
    EXECUTE_BACKLOG_ROW_COMMAND_TYPE,
    OBSERVER_POLL_LOOP_SCHEMA_VERSION,
    OBSERVER_POLL_SCHEMA_VERSION,
    OBSERVER_POLL_TIMELINE_PAYLOAD_SCHEMA_VERSION,
    ObserverPollLoopConfig,
    ObserverPollRequest,
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
