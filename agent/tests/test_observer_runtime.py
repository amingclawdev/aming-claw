from __future__ import annotations

from agent.observer_runtime import (
    EXECUTE_BACKLOG_ROW_COMMAND_TYPE,
    OBSERVER_POLL_SCHEMA_VERSION,
    ObserverPollRequest,
    build_observer_poll_plan,
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
    assert result["observer_command_id"] == "cmd-route-1"
    assert result["backlog_id"] == "AC-ROUTE-HANDOFF"
    assert result["route_identity"]["route_context_hash"] == "sha256:route"
    assert result["route_identity"]["prompt_contract_id"] == "rprompt-test"
    assert result["route_identity"]["visible_injection_manifest_hash"] == "sha256:visible"
    assert result["route_identity"]["raw_private_context_exposed"] is False
    assert result["observer_run"]["invocation"]["calls_models"] is False


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
