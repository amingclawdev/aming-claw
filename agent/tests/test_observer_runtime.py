from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agent.ai_invocation import RoutePromptContract
from agent.governance.parallel_branch_runtime import (
    BranchTaskRuntimeContext,
    STATE_WORKTREE_READY,
    branch_runtime_allocation_evidence,
    plan_branch_runtime_context,
)
from agent.observer_runtime import (
    DogfoodObserverPlanRequest,
    EXECUTE_BACKLOG_ROW_COMMAND_TYPE,
    OBSERVER_POLL_LOOP_SCHEMA_VERSION,
    OBSERVER_POLL_SCHEMA_VERSION,
    OBSERVER_POLL_TIMELINE_PAYLOAD_SCHEMA_VERSION,
    ObserverRuntimeTextPrepareRequest,
    ObserverPollLoopConfig,
    ObserverPollRequest,
    build_dogfood_observer_run_plan,
    build_observer_runtime_text_context,
    build_observer_poll_loop_metadata,
    build_observer_poll_plan,
    build_observer_prompt,
    observer_poll_timeline_payload,
    ObserverRunRequest,
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


def _git(cwd, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _dogfood_request_with_worker(tmp_path):
    root = tmp_path.resolve()
    main = root / "main"
    main.mkdir()
    _git(main, "init")
    _git(main, "checkout", "-b", "main")
    (main / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(main, "add", "README.md")
    _git(
        main,
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "fixture",
    )
    head = _git(main, "rev-parse", "HEAD")
    context = plan_branch_runtime_context(
        project_id="aming-claw",
        task_id="task-a3",
        workspace_root=str(root),
        backlog_id="AC-ROUTE-GATE-FIXTURE-PARITY-20260531",
        chain_id="AC-ROUTE-GATE-FIXTURE-PARITY-20260531",
        root_task_id="AC-ROUTE-GATE-FIXTURE-PARITY-20260531",
        stage_type="observer_dogfood",
        agent_id="dogfood_observer",
        worker_id="worker-a3",
        allocation_owner="dogfood_observer",
        worker_slot_id="worker-a3",
        attempt=1,
        branch_prefix="dogfood",
        worktree_root=".worktrees",
        base_commit=head,
        target_head_commit=head,
        merge_queue_id="mq-route-gate-fixture-parity-a3",
        fence_token="fence-route-gate-fixture-parity-a3",
        status=STATE_WORKTREE_READY,
    )
    worker = Path(context.worktree_path)
    worker.parent.mkdir(parents=True)
    branch_name = context.branch_ref.removeprefix("refs/heads/")
    _git(main, "worktree", "add", "-b", branch_name, str(worker), head)
    allocation_evidence = branch_runtime_allocation_evidence(
        context,
        source_ref="/api/graph-governance/aming-claw/parallel-branches/allocate",
    )
    request = DogfoodObserverPlanRequest(
        project_id="aming-claw",
        backlog_id="AC-ROUTE-GATE-FIXTURE-PARITY-20260531",
        route=RoutePromptContract(
            route_context_hash="sha256:route-a3",
            prompt_contract_id="rprompt-a3",
            prompt_contract_hash="sha256:prompt-a3",
            route_token_ref="route-token-a3",
        ),
        provider="openai",
        backend_mode="codex_cli",
        main_worktree=str(main),
        workspace_root=str(root),
        owned_files=(
            "agent/observer_runtime.py",
            "agent/tests/test_observer_runtime.py",
        ),
        task_id="task-a3",
        worker_id="worker-a3",
        merge_queue_id="mq-route-gate-fixture-parity-a3",
        fence_token="fence-route-gate-fixture-parity-a3",
        graph_trace_ids=("gqt-route-a3",),
        branch_runtime_registration_ref=allocation_evidence["source_ref"],
        branch_runtime_evidence=allocation_evidence,
        runtime_context_id=allocation_evidence["runtime_context_id"],
        base_commit=head,
        target_head_commit=head,
        route_id="route-20260605-a3",
        visible_injection_manifest_hash="sha256:visible-a3",
        timeout_sec=30,
        early_progress_timeout_sec=0.25,
    )
    return request, allocation_evidence


def _patch_dogfood_no_progress(monkeypatch):
    def fake_run_observer(observer_request, *, execute=False):
        assert execute is True
        return {
            "ok": False,
            "status": "blocked",
            "invocation": {
                "auth_status": "cli_no_progress",
                "blocker_id": "codex_cli_worker_no_progress_no_read_receipt",
                "output_empty": True,
                "runtime_monitor": {
                    "schema_version": "codex_cli_runtime_monitor.v1",
                    "early_progress_timeout_sec": 0.25,
                    "heartbeat_enabled": False,
                    "heartbeat_count": 0,
                    "heartbeat_failures": 0,
                    "progress_observed": False,
                    "early_progress": {
                        "progress_observed": False,
                        "stdout_bytes": 0,
                        "stderr_bytes": 0,
                        "dirty_files": [],
                        "changed_files": [],
                    },
                },
            },
        }

    monkeypatch.setattr("agent.observer_runtime.run_observer", fake_run_observer)


def test_runtime_text_prepare_accepts_supplied_registered_allocation_evidence(tmp_path):
    from agent.governance.server import _observer_runtime_text_contract_revision_payload

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
                route_token_ref="rtok-runtime-text-a1",
            ),
            main_worktree=str(main),
            owned_files=("agent/observer_runtime.py",),
            observer_command_id="cmd-a1",
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
    assert prepared["observer_command_id"] == "cmd-a1"
    assert prepared["route_identity"] == {
        "route_id": "route-a1",
        "route_context_hash": "sha256:route-a1",
        "prompt_contract_id": "rprompt-a1",
        "prompt_contract_hash": "sha256:prompt-a1",
        "route_token_ref": "rtok-runtime-text-a1",
        "visible_injection_manifest_hash": "sha256:visible-a1",
    }
    revision_payload = _observer_runtime_text_contract_revision_payload(
        {"owned_files": ["agent/observer_runtime.py"]},
        prepared,
    )
    assert revision_payload["route_identity"] == prepared["route_identity"]
    assert prepared["runtime_context"]["worktree_path"] == str(worktree)
    assert prepared["runtime_context"]["fence_token"] == "fence-runtime-text-a1"
    assert prepared["runtime_context"]["base_commit"] == "base-a1"
    assert prepared["runtime_context"]["target_head_commit"] == "target-a1"
    assert prepared["runtime_context"]["merge_queue_id"] == "mq-runtime-text-a1"
    assert prepared["branch_identity"]["runtime_context_id"] == "mfrctx-runtime-text-a1"
    assert prepared["branch_identity"]["task_id"] == "task-a1"
    assert prepared["branch_identity"]["parent_task_id"] == "AC-RUNTIME-TEXT-A1"
    assert prepared["branch_identity"]["worker_role"] == "mf_sub"
    assert prepared["mf_subagent_input"]["runtime_identity"]["parent_task_id"] == (
        "AC-RUNTIME-TEXT-A1"
    )
    self_lookup = prepared["self_contract_lookup"]
    assert self_lookup["required"] is True
    assert self_lookup["query_identity"] == {
        "project_id": "aming-claw",
        "observer_command_id": "cmd-a1",
        "governance_project_id": "aming-claw",
        "target_project_id": "aming-claw",
        "target_project_root": "",
        "task_id": "task-a1",
        "parent_task_id": "AC-RUNTIME-TEXT-A1",
        "worker_role": "mf_sub",
        "fence_token": "fence-runtime-text-a1",
        "runtime_context_id": "mfrctx-runtime-text-a1",
    }
    assert set(self_lookup["required_query_fields"]) == {
        "task_id",
        "observer_command_id",
        "parent_task_id",
        "worker_role",
        "fence_token",
        "runtime_context_id",
    }
    cli_example = self_lookup["cli_examples"]["current_state_by_runtime_context_id"]
    assert cli_example == [
        "python",
        "-m",
        "agent.cli",
        "runtime-context",
        "current",
        "--project-id",
        "aming-claw",
        "--runtime-context-id",
        "mfrctx-runtime-text-a1",
        "--observer-command-id",
        "cmd-a1",
        "--parent-task-id",
        "AC-RUNTIME-TEXT-A1",
        "--fence-token",
        "fence-runtime-text-a1",
        "--view",
        "worker_view",
        "--json-output",
    ]
    assert "--role" not in cli_example
    assert "{task_id}/runtime-contract" in prepared["launch_text"]
    assert "runtime-contexts/{runtime_context_id}/runtime-contract" in prepared[
        "launch_text"
    ]
    assert "--role" not in prepared["launch_text"]
    assert "parent_task_id" in prepared["launch_text"]
    assert "Echo parent_task_id in mf_subagent_read_receipt" in prepared["launch_text"]
    assert prepared["branch_runtime_evidence"]["status"] == STATE_WORKTREE_READY
    assert prepared["branch_runtime_evidence"]["registered"] is True
    assert prepared["dispatch_gate_validation"]["allowed"] is True
    assert prepared["startup_intent_event"]["artifact_refs"]["runtime_context_id"] == (
        "mfrctx-runtime-text-a1"
    )
    assert prepared["first_progress_contract"]["startup_is_progress"] is False
    assert (
        "first_progress_evidence"
        in prepared["mf_subagent_input"]["parent_route_lineage"]["required_evidence"]
    )
    assert (
        "audited_graph_query_with_task_and_fence_identity"
        in prepared["first_progress_contract"]["observer_progress_sources"]
    )


def test_runtime_text_prepare_hydrates_persisted_allocation_from_runtime_context_id(
    monkeypatch,
    tmp_path,
):
    main = tmp_path / "main"
    main.mkdir()
    worktree = tmp_path / ".worktrees" / "persisted-worker" / "task-a2"
    allocation_context = BranchTaskRuntimeContext(
        project_id="aming-claw",
        task_id="task-a2",
        runtime_context_id="mfrctx-runtime-text-a2",
        backlog_id="AC-RUNTIME-TEXT-A2",
        root_task_id="AC-RUNTIME-TEXT-A2",
        stage_task_id="task-a2",
        stage_type="mf_sub",
        agent_id="persisted-owner",
        allocation_owner="persisted-owner",
        worker_id="persisted-worker",
        worker_slot_id="persisted-worker",
        fence_token="fence-runtime-text-a2",
        branch_ref="refs/heads/codex/task-a2",
        worktree_id="wt-task-a2",
        worktree_path=str(worktree),
        base_commit="base-a2",
        target_head_commit="target-a2",
        merge_queue_id="mq-runtime-text-a2",
        status=STATE_WORKTREE_READY,
    )
    seen = {}

    def fake_lookup(*, project_id, runtime_context_id="", task_id=""):
        seen["project_id"] = project_id
        seen["runtime_context_id"] = runtime_context_id
        seen["task_id"] = task_id
        return allocation_context

    monkeypatch.setattr(
        "agent.observer_runtime._runtime_text_get_persisted_branch_context",
        fake_lookup,
    )
    monkeypatch.setattr(
        "agent.observer_runtime._runtime_text_get_service_branch_runtime_evidence",
        lambda **_kwargs: {},
    )

    prepared = build_observer_runtime_text_context(
        ObserverRuntimeTextPrepareRequest(
            project_id="aming-claw",
            backlog_id="AC-RUNTIME-TEXT-A2",
            route=RoutePromptContract(
                route_context_hash="sha256:route-a2",
                prompt_contract_id="rprompt-a2",
                prompt_contract_hash="sha256:prompt-a2",
                route_token_ref="rtok-runtime-text-a2",
            ),
            main_worktree=str(main),
            owned_files=("agent/observer_runtime.py",),
            observer_command_id="cmd-a2",
            task_id="task-a2",
            parent_task_id="AC-RUNTIME-TEXT-A2",
            worker_id="planned-worker",
            graph_trace_ids=("gqt-runtime-text-a2",),
            runtime_context_id="mfrctx-runtime-text-a2",
            route_id="route-a2",
            visible_injection_manifest_hash="sha256:visible-a2",
        )
    )

    assert seen == {
        "project_id": "aming-claw",
        "runtime_context_id": "mfrctx-runtime-text-a2",
        "task_id": "task-a2",
    }
    assert prepared["ok"] is True
    assert prepared["runtime_context_id"] == "mfrctx-runtime-text-a2"
    assert prepared["runtime_context"]["allocation_owner"] == "persisted-owner"
    assert prepared["runtime_context"]["worker_id"] == "persisted-worker"
    assert prepared["runtime_context"]["worktree_path"] == str(worktree)
    assert prepared["runtime_context"]["fence_token"] == "fence-runtime-text-a2"
    assert prepared["runtime_context"]["base_commit"] == "base-a2"
    assert prepared["runtime_context"]["target_head_commit"] == "target-a2"
    assert prepared["runtime_context"]["merge_queue_id"] == "mq-runtime-text-a2"
    assert prepared["branch_runtime_evidence"]["registered"] is True
    assert prepared["branch_runtime_evidence"]["registration_source"] == (
        "persisted_branch_runtime_context"
    )
    assert prepared["dispatch_gate"]["allocation_owner"] == "persisted-owner"
    assert prepared["dispatch_gate_validation"]["allowed"] is True


def test_runtime_text_prepare_rejects_projection_missing_startup_finish_field(tmp_path):
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
    supplied_projection = {
        "schema_version": "runtime_context.current.v1",
        "worker_view": {
            "schema_version": "runtime_context.worker_view.v1",
            "runtime_context_id": "mfrctx-runtime-text-a1",
            "observer_command_id": "cmd-a1",
            "task_id": "task-a1",
            "parent_task_id": "AC-RUNTIME-TEXT-A1",
            "worker_role": "mf_sub",
            "fence_token": "fence-runtime-text-a1",
            "branch_ref": "refs/heads/codex/task-a1",
            "worktree_path": str(worktree),
            "base_commit": "base-a1",
            "target_head_commit": "target-a1",
            "merge_queue_id": "mq-runtime-text-a1",
            "owned_files": ["agent/observer_runtime.py"],
            "graph_query_identity": {
                "query_source": "mf_subagent",
                "query_purpose": "subagent_context_build",
                "parent_task_id": "AC-RUNTIME-TEXT-A1",
            },
        },
        "gate_inputs": {
            "schema_version": "runtime_context.gate_inputs.v1",
            "observer_command_id": "cmd-a1",
            "route_context_hash": "sha256:route-a1",
            "prompt_contract_id": "rprompt-a1",
        },
    }

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
            observer_command_id="cmd-a1",
            task_id="task-a1",
            parent_task_id="AC-RUNTIME-TEXT-A1",
            worker_id="worker-a1",
            graph_trace_ids=("gqt-runtime-text-a1",),
            branch_runtime_evidence=allocation_evidence,
            runtime_context_projection=supplied_projection,
            route_id="route-a1",
            visible_injection_manifest_hash="sha256:visible-a1",
        )
    )

    assert prepared["ok"] is False
    assert prepared["status"] == "rejected"
    assert prepared["startup_intent_event"] == {}
    diagnostics = prepared["runtime_context_projection_diagnostics"]
    assert diagnostics["status"] == "missing_required_projection_fields"
    assert "prompt_contract_hash" in diagnostics["missing_fields"]
    missing = [
        item
        for item in diagnostics["missing"]
        if item["field"] == "prompt_contract_hash"
    ]
    assert missing
    assert {item["gate"] for item in missing} == {"mf_subagent.startup"}
    assert missing[0]["expected_source"] == (
        "runtime_context.gate_inputs.v1.prompt_contract_hash"
    )
    assert missing[0]["producer"] == "runtime_context_service"
    assert missing[0]["consumer"] == "observer_runtime_text_prepare"
    validation = prepared["dispatch_gate_validation"]
    assert validation["allowed"] is False
    assert validation["status"] == "missing_runtime_context_projection_fields"


def test_dogfood_no_progress_terminal_blocker_appends_timeline(monkeypatch, tmp_path):
    request, allocation_evidence = _dogfood_request_with_worker(tmp_path)
    monkeypatch.setenv("AMING_WORKER_SESSION_TOKEN", "worker-session-token-test")
    _patch_dogfood_no_progress(monkeypatch)
    recorded_events = []

    def fake_record_task_timeline_event(*, project_id, event):
        event_id = 17 + len(recorded_events)
        recorded_events.append((project_id, event))
        return {
            "id": event_id,
            "project_id": project_id,
            **event,
            "created_at": "2026-06-05T00:00:00Z",
        }

    monkeypatch.setattr(
        "agent.observer_runtime._record_task_timeline_event",
        fake_record_task_timeline_event,
    )

    result = build_dogfood_observer_run_plan(request, execute=True)

    assert result["ok"] is False
    assert result["status"] == "blocked"
    blocker = result["cli_timeout_blocker"]
    assert blocker["schema_version"] == "observer_cli_no_progress_blocker.v1"
    assert blocker["blocker_id"] == "codex_cli_worker_no_progress_no_read_receipt"
    assert blocker["invocation_blocker_id"] == "codex_cli_worker_no_progress_no_read_receipt"
    assert blocker["failure_evidence_appended"] is True
    assert blocker["failure_evidence_append"]["event_id"] == 17
    assert blocker["route_identity"]["route_id"] == "route-20260605-a3"
    assert blocker["route_identity"]["route_context_hash"] == "sha256:route-a3"
    assert blocker["route_identity"]["prompt_contract_hash"] == "sha256:prompt-a3"
    assert blocker["runtime_context_id"] == allocation_evidence["runtime_context_id"]
    assert blocker["worktree_diff_scope"]["no_diff"] is True
    assert blocker["runtime_monitor_summary"]["present"] is True
    assert blocker["runtime_monitor_summary"]["progress_observed"] is False
    executable_launch = blocker["executable_worker_launch"]
    assert executable_launch["schema_version"] == "observer_executable_worker_launch.v1"
    assert executable_launch["status"] == "ready"
    assert executable_launch["executable"] is True
    assert executable_launch["payload"]["route_context_hash"] == "sha256:route-a3"
    assert executable_launch["payload"]["prompt_contract_id"] == "rprompt-a3"
    assert executable_launch["payload"]["owned_files"] == [
        "agent/observer_runtime.py",
        "agent/tests/test_observer_runtime.py",
    ]
    assert executable_launch["payload"]["runtime_context_id"] == (
        allocation_evidence["runtime_context_id"]
    )
    assert executable_launch["stdin"]["source"] == "response.launch_text"
    assert "codex exec" in executable_launch["command_display"]
    assert "AMING_WORKER_SESSION_TOKEN" in executable_launch["command_display"]

    startup_status = blocker["startup_read_receipt_recording_status"]
    assert startup_status["startup_prepared"] is False
    assert startup_status["startup_recorded"] is False
    assert startup_status["startup_close_satisfying"] is False
    assert startup_status["startup_counts_as_real_worker_evidence"] is False
    assert startup_status["startup_surrogate_not_close_satisfying"] is False
    assert startup_status["read_receipt_prepared"] is False
    assert startup_status["read_receipt_recorded"] is False
    assert startup_status["read_receipt_recorded_before_implementation_wait"] is False
    assert startup_status["read_receipt_timeline_event_id"] == ""
    assert startup_status["startup_timeline_event_id"] == ""
    assert startup_status["implementation_evidence_recorded"] is False
    assert blocker["implementation_evidence_recorded"] is False
    assert blocker["close_ready"] is False
    assert blocker["read_receipt_timeline_event_id"] == ""
    assert blocker["startup_timeline_event_id"] == ""
    assert "read_receipt" not in result
    assert "startup_timeline_event" not in result

    assert len(recorded_events) == 1
    project_id, event = recorded_events[0]
    assert project_id == "aming-claw"
    assert event["event_type"] == "observer_dogfood_terminal_blocker"
    assert event["event_kind"] == "observer_cli_terminal_blocker"
    assert event["event_kind"] not in {"implementation", "verification", "close_ready"}
    assert event["status"] == "blocked"
    assert event["task_id"] == "task-a3"
    assert event["backlog_id"] == "AC-ROUTE-GATE-FIXTURE-PARITY-20260531"
    payload = event["payload"]
    assert payload["route_identity"]["route_id"] == "route-20260605-a3"
    assert payload["route_identity"]["route_token_ref"] == "route-token-a3"
    assert payload["branch_identity"]["runtime_context_id"] == (
        allocation_evidence["runtime_context_id"]
    )
    assert payload["branch_identity"]["fence_token"] == (
        "fence-route-gate-fixture-parity-a3"
    )
    assert payload["timeout_no_progress"]["blocker_id"] == (
        "codex_cli_worker_no_progress_no_read_receipt"
    )
    assert (
        "executable_worker_launch_payload"
        in payload["command_projection"]["terminal_evidence_refs"]
    )
    assert (
        "mf_subagent_startup_not_recorded"
        in payload["command_projection"]["terminal_evidence_refs"]
    )
    assert payload["executable_worker_launch"]["payload"]["task_id"] == "task-a3"
    assert payload["command_projection"]["command_projection_status"] == "failed"
    assert payload["worktree_diff_scope"]["no_diff"] is True
    assert payload["startup_read_receipt_recording_status"]["startup_recorded"] is False
    assert payload["startup_read_receipt_recording_status"]["read_receipt_recorded"] is False
    assert event["verification"]["passed"] is False
    assert event["verification"]["implementation_evidence_recorded"] is False


def test_dogfood_timeout_blocker_uses_timeline_read_receipt_status(
    monkeypatch, tmp_path
):
    request, _allocation_evidence = _dogfood_request_with_worker(tmp_path)
    monkeypatch.setenv("AMING_WORKER_SESSION_TOKEN", "worker-session-token-test")
    _patch_dogfood_no_progress(monkeypatch)
    recorded_events = []

    def fake_timeline_status(**kwargs):
        assert kwargs["project_id"] == "aming-claw"
        assert kwargs["task_id"] == "task-a3"
        assert kwargs["runtime_context_id"]
        assert kwargs["route_identity"]["route_context_hash"] == "sha256:route-a3"
        return {
            "schema_version": "observer_startup_read_receipt_timeline_status.v1",
            "read_receipt_recorded": True,
            "read_receipt_recorded_before_implementation_wait": True,
            "read_receipt_timeline_event_id": "41",
            "read_receipt_hash": "sha256:receipt-a3",
            "read_receipt_prepared": True,
            "startup_recorded": False,
            "startup_timeline_event_id": "",
            "timeline_read_receipt_event_ids": [41],
            "timeline_startup_event_ids": [],
        }

    def fake_record_task_timeline_event(*, project_id, event):
        recorded_events.append((project_id, event))
        return {
            "id": 42,
            "project_id": project_id,
            **event,
            "created_at": "2026-06-20T00:00:00Z",
        }

    monkeypatch.setattr(
        "agent.observer_runtime._timeline_startup_read_receipt_recording_status",
        fake_timeline_status,
    )
    monkeypatch.setattr(
        "agent.observer_runtime._record_task_timeline_event",
        fake_record_task_timeline_event,
    )

    result = build_dogfood_observer_run_plan(request, execute=True)

    assert result["ok"] is False
    assert result["status"] == "blocked"
    blocker = result["cli_timeout_blocker"]
    assert blocker["schema_version"] == "observer_cli_timeout_blocker.v1"
    assert blocker["blocker_id"] == "codex_cli_timeout_no_output_no_finish"
    assert blocker["invocation_blocker_id"] == "codex_cli_worker_no_progress_no_read_receipt"
    assert blocker["read_receipt_recorded"] is True
    assert blocker["read_receipt_recorded_before_implementation_wait"] is True
    assert blocker["read_receipt_timeline_event_id"] == "41"
    assert blocker["startup_recorded"] is False
    projection = blocker["terminal_contract_projection"]
    assert "mf_subagent_read_receipt_recorded" in projection["terminal_evidence_refs"]
    assert (
        "mf_subagent_read_receipt_not_recorded"
        not in projection["terminal_evidence_refs"]
    )
    assert "mf_subagent_startup_not_recorded" in projection["terminal_evidence_refs"]
    assert recorded_events
    event_payload = recorded_events[0][1]["payload"]
    assert event_payload["startup_read_receipt_recording_status"][
        "read_receipt_recorded"
    ] is True
    assert event_payload["command_projection"]["divergence_reason"] == (
        "codex_cli_timeout_no_output_no_finish"
    )


def test_dogfood_no_progress_terminal_blocker_reports_append_error(monkeypatch, tmp_path):
    request, _allocation_evidence = _dogfood_request_with_worker(tmp_path)
    monkeypatch.setenv("AMING_WORKER_SESSION_TOKEN", "worker-session-token-test")
    _patch_dogfood_no_progress(monkeypatch)

    def fail_record_task_timeline_event(*, project_id, event):
        if event["event_type"] == "observer_dogfood_terminal_blocker":
            raise RuntimeError("timeline append unavailable")
        raise AssertionError(f"observer must not append worker event {event['event_type']}")

    monkeypatch.setattr(
        "agent.observer_runtime._record_task_timeline_event",
        fail_record_task_timeline_event,
    )

    result = build_dogfood_observer_run_plan(request, execute=True)

    assert result["ok"] is False
    assert result["status"] == "blocked"
    blocker = result["cli_timeout_blocker"]
    assert blocker["failure_evidence_appended"] is False
    assert "timeline append unavailable" in blocker["failure_evidence_append_error"]
    append = blocker["failure_evidence_append"]
    assert append["ok"] is False
    assert append["event_type"] == "observer_dogfood_terminal_blocker"
    assert append["event_kind"] == "observer_cli_terminal_blocker"
    assert "timeline append unavailable" in append["error"]
    assert append["request"]["payload"]["route_identity"]["route_context_hash"] == (
        "sha256:route-a3"
    )
    assert append["request"]["payload"]["command_projection"][
        "command_projection_status"
    ] == "failed"
    startup_status = blocker["startup_read_receipt_recording_status"]
    assert startup_status["startup_recorded"] is False
    assert startup_status["read_receipt_recorded"] is False
    assert startup_status["read_receipt_recorded_before_implementation_wait"] is False
    assert startup_status["read_receipt_timeline_event_id"] == ""
    assert startup_status["startup_timeline_event_id"] == ""
    assert startup_status["implementation_evidence_recorded"] is False
    assert blocker["implementation_evidence_recorded"] is False


def test_dogfood_execute_blocks_missing_worker_session_token_env(tmp_path):
    request, _allocation_evidence = _dogfood_request_with_worker(tmp_path)

    result = build_dogfood_observer_run_plan(request, execute=True)

    assert result["ok"] is False
    assert result["status"] == "blocked"
    blocker = result["execute_env_blocker"]
    assert blocker["blocker_id"] == "worker_session_token_env_missing_before_cli_launch"
    assert blocker["missing_env"] == ["AMING_WORKER_SESSION_TOKEN"]
    assert blocker["raw_session_token_persisted"] is False
    assert result["calls_models"] is False
    assert result["auth_status"] == "not_invoked"


def test_observer_prompt_says_startup_is_not_progress(tmp_path):
    prompt = build_observer_prompt(
        ObserverRunRequest(
            project_id="aming-claw",
            backlog_id="AC-ROUTE-HANDOFF",
            route=RoutePromptContract(
                route_context_hash="sha256:route",
                prompt_contract_id="rprompt-test",
            ),
            workspace=str(tmp_path),
            main_worktree=str(tmp_path),
        )
    )

    assert "Actual startup evidence proves launch only" in prompt
    assert "first progress" in prompt


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
