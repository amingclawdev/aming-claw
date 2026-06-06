"""Tests for observer runtime text preparation."""

from __future__ import annotations

from pathlib import Path
import sys


_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from agent.ai_invocation import RoutePromptContract
from agent.observer_runtime import (
    ObserverRuntimeTextPrepareRequest,
    build_observer_runtime_text_context,
)


def _branch_runtime_evidence(tmp_path: Path, **context_overrides: object) -> dict[str, object]:
    context: dict[str, object] = {
        "runtime_context_id": "mfrctx-runtime-text",
        "task_id": "AC-RUNTIME-TEXT-impl-1",
        "parent_task_id": "AC-RUNTIME-TEXT",
        "backlog_id": "AC-RUNTIME-TEXT",
        "fence_token": "fence-runtime-text",
        "worktree_path": str(
            tmp_path / "workers" / ".worktrees" / "worker-1" / "ac-runtime-text-impl-1"
        ),
        "base_commit": "base123",
        "target_head_commit": "target123",
        "merge_queue_id": "mq-runtime-text",
        "branch_ref": "refs/heads/runtime-text/ac-runtime-text-impl-1",
        "worktree_id": "wt-ac-runtime-text-impl-1",
    }
    context.update(context_overrides)
    return {
        "schema_version": "mf_subagent_branch_runtime.v1",
        "status": "worktree_ready",
        "ok": True,
        "present": True,
        "registered": True,
        "allocation_required": False,
        "source_ref": "/api/graph-governance/aming-claw/parallel-branches/allocate",
        "registration_ref": "/api/graph-governance/aming-claw/parallel-branches/allocate",
        "allocation_source_ref": "/api/graph-governance/aming-claw/parallel-branches/allocate",
        "registration_source": "parallel_branch_allocate",
        "runtime_context_id": context["runtime_context_id"],
        "context": context,
    }


def _runtime_text_request(tmp_path: Path, **overrides: object) -> ObserverRuntimeTextPrepareRequest:
    main = tmp_path / "main"
    main.mkdir(parents=True, exist_ok=True)
    values: dict[str, object] = {
        "project_id": "aming-claw",
        "backlog_id": "AC-RUNTIME-TEXT",
        "route": RoutePromptContract(
            route_context_hash="sha256:route",
            prompt_contract_id="rprompt-runtime",
            prompt_contract_hash="sha256:prompt",
            route_token_ref="route-token-ref",
        ),
        "main_worktree": str(main),
        "workspace_root": str(tmp_path / "workers"),
        "owned_files": ("agent/observer_runtime.py", "agent/cli.py"),
        "task_id": "AC-RUNTIME-TEXT-impl-1",
        "parent_task_id": "AC-RUNTIME-TEXT",
        "worker_id": "worker-1",
        "attempt": 1,
        "worktree_root": ".worktrees",
        "branch_prefix": "runtime-text",
        "merge_queue_id": "mq-runtime-text",
        "fence_token": "fence-runtime-text",
        "graph_trace_ids": ("gqt-runtime-text",),
        "branch_runtime_registration_ref": (
            "/api/graph-governance/aming-claw/parallel-branches/allocate"
        ),
        "branch_runtime_evidence": _branch_runtime_evidence(tmp_path),
        "base_commit": "base123",
        "target_head_commit": "target123",
        "route_id": "route-20260603-runtime",
        "precheck_run_id": "precheck-runtime",
        "visible_injection_manifest_hash": "sha256:visible",
        "acceptance_criteria": ("prepare runtime launch text",),
        "test_commands": ("python -m pytest agent/tests/test_observer_runtime_text.py -q",),
    }
    values.update(overrides)
    return ObserverRuntimeTextPrepareRequest(**values)


def test_runtime_text_builder_hashes_launch_text_and_does_not_persist_raw(tmp_path):
    result = build_observer_runtime_text_context(_runtime_text_request(tmp_path))

    assert result["ok"] is True
    assert result["status"] == "prepared"
    assert result["runtime_context_id"] == "mfrctx-runtime-text"
    assert result["runtime_context"]["worktree_path"].endswith(
        ".worktrees/worker-1/ac-runtime-text-impl-1"
    )
    assert result["launch_text"]
    assert result["launch_text_hash"].startswith("sha256:")
    assert result["raw_launch_text_persisted"] is False
    persistent = result["persistent_evidence"]
    assert persistent["runtime_context_id"] == result["runtime_context_id"]
    assert persistent["launch_text_hash"] == result["launch_text_hash"]
    assert persistent["raw_launch_text_persisted"] is False
    assert persistent["dispatch_ready"] is True
    assert persistent["allocation_required"] is False
    assert persistent["startup_intent_event_generated"] is True
    assert persistent["actual_startup_required"] is True
    assert persistent["actual_startup_recorded"] is False
    assert persistent["close_ready"] is False
    assert persistent["startup_intent_event"] == result["startup_intent_event"]
    assert "launch_text" not in result["persistent_evidence"]
    assert "Judgment Brain" not in result["launch_text"]
    assert "raw private route/context-pack content" in result["launch_text"]

    gate = result["dispatch_gate_validation"]
    assert gate["allowed"] is True
    assert gate["startup_intent_event_generated"] is True
    assert gate["actual_startup_required"] is True
    assert gate["actual_startup_recorded"] is False
    assert gate["close_ready"] is False
    assert gate["governed_evidence_required"] is True
    assert gate["dispatch_graph_obligation"]["query_source"] == "mf_subagent"
    assert gate["dispatch_graph_obligation"]["query_purpose"] == "subagent_context_build"
    assert gate["dispatch_graph_obligation"]["counts_as_worker_graph_trace_evidence"] is False
    assert gate["dispatch_graph_obligation"]["finish_gate_requires_worker_graph_trace"] is True
    assert "graph_trace_evidence" not in result["dispatch_gate"]
    assert result["prelaunch_graph_context"]["trace_ids"] == ["gqt-runtime-text"]
    assert (
        result["prelaunch_graph_context"]["counts_as_worker_graph_trace_evidence"]
        is False
    )
    assert gate["branch_runtime_evidence"]["registered"] is True
    assert gate["branch_runtime_evidence"]["runtime_context_id"] == "mfrctx-runtime-text"
    assert gate["service_dispatch_evidence"]["documented_host_adapter_boundary"] is True

    startup_event = result["startup_intent_event"]
    assert startup_event["event_type"] == "mf_subagent.startup_intent"
    assert startup_event["event_kind"] == "mf_subagent_startup_intent"
    assert startup_event["phase"] == "startup_intent"
    assert startup_event["status"] == "planned"
    assert startup_event["close_satisfying"] is False
    assert startup_event["actual_startup_required"] is True
    startup_intent = startup_event["payload"]["mf_subagent_startup_intent"]
    assert startup_intent["schema_version"] == "mf_subagent_startup_intent.v1"
    assert startup_intent["runtime_context_id"] == result["runtime_context_id"]
    assert startup_intent["launch_text_hash"] == result["launch_text_hash"]
    assert startup_intent["raw_launch_text_persisted"] is False
    assert startup_intent["close_satisfying"] is False
    assert startup_intent["actual_startup_required"] is True
    assert startup_intent["project_id"] == "aming-claw"
    assert startup_intent["governance_project_id"] == "aming-claw"
    assert startup_intent["target_project_id"] == "aming-claw"
    assert startup_intent["task_id"] == "AC-RUNTIME-TEXT-impl-1"
    assert startup_intent["parent_task_id"] == "AC-RUNTIME-TEXT"
    assert startup_intent["worker_role"] == "mf_sub"
    assert startup_intent["worker_slot_id"] == "worker-1"
    assert startup_intent["allocation_owner"] == "observer_runtime_text"
    assert "actual_host_worker_id" in startup_intent["actual_startup_must_include"]
    assert startup_intent["fence_token"] == "fence-runtime-text"
    assert startup_intent["assigned_worktree"] == result["runtime_context"]["worktree_path"]
    assert startup_intent["branch"] == result["runtime_context"]["branch_ref"]
    assert startup_intent["head_commit"] == "target123"
    assert startup_intent["base_commit"] == "base123"
    assert startup_intent["target_head_commit"] == "target123"
    assert startup_intent["route_context_hash"] == "sha256:route"
    assert startup_intent["prompt_contract_id"] == "rprompt-runtime"
    assert startup_intent["graph_trace_ids"] == ["gqt-runtime-text"]
    assert "same_as_expected_worker" not in startup_intent
    assert "fence_token_matches" not in startup_intent
    assert "actual_cwd" not in startup_intent
    assert "actual_git_root" not in startup_intent
    assert "launch_text" not in startup_intent
    assert result["startup_recording"]["close_ready"] is False
    assert result["startup_recording"]["actual_startup_required"] is True

    assert result["mf_subagent_input"]["role"] == "mf_sub"
    assert result["startup_echo_contract"]["required"] is True
    assert result["graph_first_obligations"]["query"]["query_source"] == "mf_subagent"
    assert result["graph_first_obligations"]["query"]["governance_project_id"] == "aming-claw"
    assert result["graph_first_obligations"]["query"]["target_project_id"] == "aming-claw"
    assert result["graph_first_obligations"]["read_receipt_required_before"] == [
        "graph_query",
        "startup",
        "implementation",
        "verification",
        "close_ready",
    ]
    assert (
        result["graph_first_obligations"]["read_receipt_timeline_event_kind"]
        == "mf_subagent_read_receipt"
    )
    assert result["graph_first_obligations"]["post_hoc_read_receipt_satisfies_gate"] is False
    assert "mf_subagent_read_receipt" in result["launch_text"]
    assert "post-hoc read receipt after counted evidence does not satisfy" in result[
        "launch_text"
    ]
    assert result["finish_gate_contract"]["required"] is True
    assert result["finish_gate_contract"]["close_sensitive_precheck"][
        "parent_main_status_short_must_be_clean"
    ] is True
    assert result["finish_gate_contract"]["worker_graph_trace_evidence"]["required"] is True
    assert (
        result["finish_gate_contract"]["worker_graph_trace_evidence"]["query_purpose"]
        == "subagent_gate_validation"
    )
    assert result["mf_subagent_input"]["runtime_identity"]["worker_slot_id"] == "worker-1"
    assert (
        result["mf_subagent_input"]["runtime_identity"]["allocation_owner"]
        == "observer_runtime_text"
    )


def test_runtime_text_builder_requires_supplied_branch_allocation_evidence(tmp_path):
    result = build_observer_runtime_text_context(
        _runtime_text_request(
            tmp_path,
            branch_runtime_registration_ref="",
            branch_runtime_evidence={},
        )
    )

    assert result["ok"] is False
    assert result["status"] == "allocation_required"
    assert result["launch_text"]
    assert result["launch_text_hash"].startswith("sha256:")
    assert result["raw_launch_text_persisted"] is False
    assert result["persistent_evidence"]["dispatch_ready"] is False
    assert result["persistent_evidence"]["allocation_required"] is True
    validation = result["dispatch_gate_validation"]
    assert validation["allowed"] is False
    assert validation["allocation_required"] is True
    assert validation["actual_startup_required"] is False
    assert validation["close_ready"] is False
    evidence = result["branch_runtime_evidence"]
    assert evidence["status"] == "allocation_required"
    assert evidence["registered"] is False
    assert evidence["allocation_required"] is True
    assert "/api/graph-governance/aming-claw/parallel-branches/allocate" in evidence["message"]


def test_runtime_text_builder_rejects_marker_only_branch_runtime_ref(tmp_path):
    result = build_observer_runtime_text_context(
        _runtime_text_request(
            tmp_path,
            branch_runtime_registration_ref=(
                "/api/graph-governance/aming-claw/parallel-branches/allocate"
            ),
            branch_runtime_evidence={},
        )
    )

    assert result["ok"] is False
    assert result["status"] == "allocation_required"
    evidence = result["branch_runtime_evidence"]
    assert evidence["registered"] is False
    assert evidence["allocation_required"] is True
    assert evidence["supplied_source_ref"].endswith("/parallel-branches/allocate")
    assert "runtime_context_id" in evidence["missing_fields"]
    assert "worktree_path" in evidence["missing_fields"]


def test_runtime_text_builder_rejects_bare_runtime_context_id_without_server_resolution(tmp_path):
    result = build_observer_runtime_text_context(
        _runtime_text_request(
            tmp_path,
            branch_runtime_registration_ref="mfrctx-missing",
            branch_runtime_evidence={},
        )
    )

    assert result["ok"] is False
    assert result["status"] == "allocation_required"
    evidence = result["branch_runtime_evidence"]
    assert evidence["registered"] is False
    assert evidence["runtime_context_id"] == "mfrctx-missing"
    assert "branch runtime allocation" in evidence["message"]


def test_runtime_text_builder_rejects_weak_branch_runtime_evidence_without_ref(tmp_path):
    result = build_observer_runtime_text_context(
        _runtime_text_request(
            tmp_path,
            branch_runtime_registration_ref="",
            branch_runtime_evidence={
                "ok": True,
                "registered": True,
                "context": {
                    "task_id": "AC-RUNTIME-TEXT-impl-1",
                    "parent_task_id": "AC-RUNTIME-TEXT",
                    "fence_token": "fence-runtime-text",
                    "worktree_path": str(
                        tmp_path / "workers" / "worktrees" / "runtime-text"
                    ),
                    "base_commit": "base123",
                    "target_head_commit": "target123",
                    "merge_queue_id": "mq-runtime-text",
                },
            },
        )
    )

    assert result["ok"] is False
    assert result["status"] == "allocation_required"
    assert result["persistent_evidence"]["dispatch_ready"] is False
    validation = result["dispatch_gate_validation"]
    assert validation["allowed"] is False
    assert validation["allocation_required"] is True
    assert result["startup_intent_event"] == {}
    evidence = result["branch_runtime_evidence"]
    assert evidence["registered"] is False
    assert evidence["allocation_required"] is True
    assert evidence.get("supplied_source_ref", "") == ""
    assert "allocation source ref" in evidence["message"]


def test_runtime_text_builder_allows_dispatch_before_worker_graph_trace(tmp_path):
    result = build_observer_runtime_text_context(
        _runtime_text_request(tmp_path, graph_trace_ids=())
    )

    assert result["ok"] is True
    assert result["status"] == "prepared"
    assert result["raw_launch_text_persisted"] is False
    assert result["persistent_evidence"]["dispatch_ready"] is True
    assert result["dispatch_gate_validation"]["allowed"] is True
    assert result["startup_intent_event"]["close_satisfying"] is False
    assert result["prelaunch_graph_context"]["trace_ids"] == []
    assert "graph_trace_evidence" not in result["dispatch_gate"]
    obligation = result["dispatch_gate_validation"]["dispatch_graph_obligation"]
    assert obligation["present"] is True
    assert obligation["counts_as_worker_graph_trace_evidence"] is False
    assert obligation["finish_gate_requires_worker_graph_trace"] is True
    assert result["finish_gate_contract"]["worker_graph_trace_evidence"]["required"] is True
    assert "Finish gates require worker-owned mf_subagent graph trace evidence" in result[
        "launch_text"
    ]
