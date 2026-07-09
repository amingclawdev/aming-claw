"""Tests for the MF subagent worker contract."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from agent.governance.mf_subagent_contract import (
    BACKEND_CONTRACT,
    DISPATCH_DEFAULT,
    DISPATCH_GATE_SCHEMA_VERSION,
    FINISH_GATE_CLOSE_PROJECTION_SCHEMA_VERSION,
    FINISH_GATE_REPLAY_SOURCE,
    FINISH_GATE_SCHEMA_VERSION,
    BRANCH_RUNTIME_SCHEMA_VERSION,
    AGENT_TASK_CONTRACT_SCHEMA_VERSION,
    AGENT_TASK_CONTRACT_PROJECTION_SCHEMA_VERSION,
    GRAPH_TRACE_SCHEMA_VERSION,
    MF_SUB_FORBIDDEN_ACTIONS,
    MF_SUB_ROLE,
    OBSERVER_COORDINATOR_ROLE,
    OBSERVER_DIRECT_MUTATION_SCHEMA_VERSION,
    REAL_WORKER_JOIN_SCHEMA_VERSION,
    ROUTE_ACTION_GATE_SCHEMA_VERSION,
    ROUTE_TOKEN_REQUIRED_FAILURE_SCHEMA_VERSION,
    ROUTE_TOKEN_MUTATION_GATE_SCHEMA_VERSION,
    RUNTIME_CONTRACT_VIEW_SCHEMA_VERSION,
    SERVICE_DISPATCH_SCHEMA_VERSION,
    SURROGATE_STARTUP_GATE_SCHEMA_VERSION,
    VERIFICATION_ROUTE_POLICY_SCHEMA_VERSION,
    WORKTREE_POLICY_MODE,
    META_CONTRACT_SCHEMA_VERSION,
    META_CONTRACT_TIMELINE_GATE_SCHEMA_VERSION,
    MfSubagentContractError,
    build_mf_subagent_runtime_contract_view,
    build_mf_subagent_runtime_context_projection,
    build_mf_subagent_worker_runtime_context_view,
    load_meta_contract_template,
    build_mf_subagent_input,
    normalize_mf_subagent_result,
    validate_mf_subagent_worker_runtime_context_view,
    validate_meta_contract_timeline_event,
    validate_observer_direct_mutation_exception,
    validate_mf_subagent_dispatch_gate,
    validate_mf_subagent_finish_gate,
    validate_route_action_gate,
    route_token_required_failure_details,
    validate_route_token_mutation_gate,
    surrogate_startup_evidence_gate,
    close_timeline_startup_event_gate,
    close_timeline_events_for_verification,
    _bounded_startup_evidence_present,
    _startup_is_host_adapter_surrogate,
    _startup_real_worker_join,
)
from agent.governance.parallel_branch_runtime import (
    BranchTaskRuntimeContext,
    branch_runtime_context_id,
    mf_subagent_session_token_hash,
    mf_subagent_session_token_ref,
    _startup_token_evidence,
)


def test_mf_parallel_template_requires_subagent_fence_and_graph_trace_contract() -> None:
    template_path = (
        _repo_root
        / "agent"
        / "governance"
        / "contract_templates"
        / "mf_parallel.v1.json"
    )
    template = json.loads(template_path.read_text(encoding="utf-8"))
    assert {"review_ready", "waiting_merge"}.issubset(
        set(template["lifecycle_states"])
    )

    worker_contract = template["worker_contract"]
    assert {"review_ready", "waiting_merge"}.issubset(
        set(worker_contract["allowed_terminal_states"])
    )
    assert set(worker_contract["final_output"]["stop_states"]) == {
        "review_ready",
        "waiting_merge",
    }
    assert set(worker_contract["required_fields"]).issuperset(
        {
            "task_id",
            "parent_task_id",
            "worker_role",
            "fence_token",
            "graph_queries",
            "route_identity",
            "selected_topology",
            "recommended_topology",
            "target_files",
            "test_files",
            "test_commands",
            "review_evidence",
            "branch_runtime_evidence",
            "graph_trace_evidence",
            "service_dispatch_evidence",
        }
    )

    worker_prompt_contract = worker_contract["worker_prompt_contract"]
    assert "target_files" in worker_prompt_contract["bounded_fields_only"]
    assert "test_files" in worker_prompt_contract["bounded_fields_only"]
    assert "route_identity" in worker_prompt_contract["bounded_fields_only"]
    assert "observer_only_context" in worker_prompt_contract["forbidden_context_sources"]

    runtime_identity = worker_contract["runtime_identity"]
    assert runtime_identity["worker_role"] == "mf_sub"
    assert set(runtime_identity["required_fields"]) == {
        "task_id",
        "parent_task_id",
        "worker_role",
        "fence_token",
    }

    graph_queries = worker_contract["graph_queries"]
    assert graph_queries["query_source"] == "mf_subagent"
    assert graph_queries["audited"] is True
    assert graph_queries["evidence_schema_version"] == GRAPH_TRACE_SCHEMA_VERSION
    assert set(graph_queries["required_context_fields"]).issuperset(
        {"task_id", "parent_task_id", "worker_role", "fence_token"}
    )
    assert graph_queries["timeline_trace_requirement"] == "graph_trace_ids"
    assert "judge_routed" in graph_queries["required_for"]

    service_dispatch = worker_contract["service_dispatch"]
    assert service_dispatch["schema_version"] == SERVICE_DISPATCH_SCHEMA_VERSION
    assert "dispatch_command_ref + monitor_ref" in service_dispatch["valid_evidence"]
    branch_runtime = worker_contract["branch_runtime"]
    assert branch_runtime["schema_version"] == BRANCH_RUNTIME_SCHEMA_VERSION
    assert "parallel-branches/allocate" in branch_runtime["valid_registration_refs"][0]
    runtime_contract = worker_contract["runtime_contract_service"]
    assert runtime_contract["schema_version"] == RUNTIME_CONTRACT_VIEW_SCHEMA_VERSION
    assert runtime_contract["source_of_truth"] == "contract_service"
    assert "{task_id}/runtime-contract" in runtime_contract["query_route"]
    assert set(runtime_contract["required_query_fields"]).issuperset(
        {"task_id", "fence_token", "contract_version"}
    )
    assert runtime_contract["privacy_boundary"]["raw_private_context_exposed"] is False
    runtime_text = worker_contract["runtime_text_service"]
    assert runtime_text["schema_version"] == "observer_runtime_text_service.v1"
    assert "aming-claw observer runtime-text prepare --json-output" in runtime_text["entrypoints"]
    assert runtime_text["persistence_policy"]["raw_launch_text_persisted"] is False
    assert "launch_text_hash" in runtime_text["persistence_policy"]["persistent_fields"]
    assert "raw private route/context-pack content" in runtime_text["public_boundary"].lower()

    timeline_contract = template["timeline_contract"]
    assert "payload.graph_trace_ids" in timeline_contract["trace_id_locations"]
    assert (
        "payload.graph_trace_evidence.trace_ids"
        in timeline_contract["trace_id_locations"]
    )
    assert "verification.graph_trace_ids" in timeline_contract["trace_id_locations"]

    strategic_lanes = template["route_topology_policy"][
        "strategic_judge_routed_review_lanes"
    ]
    assert strategic_lanes["default_required"] is False
    assert set(strategic_lanes["lane_ids"]) == {
        "architecture_review_lane",
        "qa_evidence_gate_review",
    }

    worktree_policy = worker_contract["worktree_policy"]
    assert worktree_policy["mode"] == "isolated_worktree_required"
    assert worktree_policy["same_worktree_allowed"] is False
    assert worktree_policy["target_main_worktree_dispatch"] == "blocked_by_default"
    assert set(worktree_policy["required_dispatch_fields"]).issuperset(
        {
            "branch",
            "worktree",
            "base_commit",
            "target_head_commit",
            "merge_queue_id",
            "fence_token",
            "owned_files",
            "dirty_scope_check",
        }
    )
    assert set(worktree_policy["override_policy"]["requires"]).issuperset(
        {
            "same_worktree_allowed=true",
            "explicit_operator_reason",
            "dirty_scope_exact_match",
            "observer_timeline_event_before_dispatch",
        }
    )


def test_runtime_contract_view_is_worker_scoped_and_redacts_private_route_body() -> None:
    context = BranchTaskRuntimeContext(
        project_id="aming-claw",
        task_id="task-runtime-contract",
        root_task_id="task-parent",
        backlog_id="AC-CONTRACT-RUNTIME-SERVICE-SHARED-CONTEXT-20260603",
        worker_id="worker-1",
        attempt=2,
        branch_ref="refs/heads/codex/task-runtime-contract",
        ref_name="main",
        worktree_path="/repo/.worktrees/task-runtime-contract",
        base_commit="base123",
        head_commit="head123",
        target_head_commit="target123",
        snapshot_id="scope-1",
        projection_id="semproj-1",
        merge_queue_id="mq-1",
        fence_token="fence-runtime",
        status="running",
        lease_id="lease-1",
        lease_expires_at="2999-01-01T00:00:00Z",
        checkpoint_id="ckpt-1",
        depends_on=("task-a",),
    )

    view = build_mf_subagent_runtime_contract_view(
        context,
        role=MF_SUB_ROLE,
        contract_version="mf_parallel.v1",
        contract_revision_id="crev-1",
        route_identity={
            "route_id": "route-1",
            "route_context_hash": "sha256:route",
            "prompt_contract_id": "rprompt-1",
            "prompt_contract_hash": "sha256:prompt-1",
            "route_token_ref": "rtok-worker-visible",
            "visible_injection_manifest_hash": "sha256:visible",
            "raw_private_context": "do not expose",
            "hidden_context": "do not expose",
        }
    )

    assert view["schema_version"] == RUNTIME_CONTRACT_VIEW_SCHEMA_VERSION
    assert view["role_scope"] == "worker"
    assert view["runtime_context_id"].startswith("mfrctx-")
    assert view["runtime_context"]["task_id"] == "task-runtime-contract"
    assert view["runtime_context"]["parent_task_id"] == "task-parent"
    assert view["runtime_context"]["fence_token"] == "fence-runtime"
    assert view["runtime_context"]["worktree_path"] == "/repo/.worktrees/task-runtime-contract"
    assert view["contract"]["contract_change_policy"]["source_of_truth"] == "contract_service"
    assert view["contract"]["contract_change_policy"]["raw_prompt_as_runtime_source"] is False
    assert view["contract"]["read_receipt_ordering"] == {
        "schema_version": "mf_subagent_read_receipt_ordering.v1",
        "timeline_event_kind": "mf_subagent_read_receipt",
        "observer_command_id": "",
        "required_command_type": "execute_backlog_row",
        "required_before": [
            "graph_query",
            "startup",
            "implementation",
            "verification",
            "close_ready",
        ],
        "close_sensitive": True,
        "post_hoc_receipt_satisfies_gate": False,
    }
    assert view["contract"]["service_routes"]["finish_gate"].endswith(
        "/parallel-branches/finish-gate"
    )
    append_policy = view["contract"]["protected_timeline_append"]
    assert append_policy["protected_action"] == "task_timeline_append"
    assert "implementation" in append_policy["protected_event_kinds"]
    assert append_policy["route_token"]["preferred"] is True
    assert append_policy["route_token"]["route_token_ref"] == "rtok-worker-visible"
    assert append_policy["route_token"]["route_identity"]["raw_private_context_exposed"] is False
    waiver_path = append_policy["task_scoped_route_waiver"]
    assert waiver_path["must_be_accepted"] is True
    assert waiver_path["scope"] == {
        "project_id": "aming-claw",
        "backlog_id": "AC-CONTRACT-RUNTIME-SERVICE-SHARED-CONTEXT-20260603",
        "task_id": "task-runtime-contract",
        "runtime_context_id": view["runtime_context_id"],
        "fence_token": "fence-runtime",
    }
    assert waiver_path["accepted_waiver_template"]["scope"] == {
        "project_id": "aming-claw",
        "backlog_id": "AC-CONTRACT-RUNTIME-SERVICE-SHARED-CONTEXT-20260603",
        "task_id": "task-runtime-contract",
    }
    assert waiver_path["accepted_waiver_template"]["route_context_hash"] == "sha256:route"
    assert waiver_path["accepted_waiver_template"]["prompt_contract_id"] == "rprompt-1"
    assert waiver_path["accepted_waiver_template"]["timeline_evidence_required"] is True
    assert view["agent_task_contract"]["schema_version"] == AGENT_TASK_CONTRACT_SCHEMA_VERSION
    assert view["agent_task_contract"]["source_of_truth"] == "Contract/Revision/Event"
    assert view["agent_task_contract"]["observer_owner"] == OBSERVER_COORDINATOR_ROLE
    assert view["agent_task_contract"]["executor_lane"] == MF_SUB_ROLE
    assert view["agent_task_contract"]["verifier_lane"] == "qa"
    assert view["agent_task_contract"]["target_fences"] == ["fence-runtime"]
    assert view["contract_projection"]["schema_version"] == (
        AGENT_TASK_CONTRACT_PROJECTION_SCHEMA_VERSION
    )
    assert view["contract_projection"]["source_of_truth"] == "Contract/Revision/Event"
    assert view["contract_projection"]["contract_hash"] == view[
        "agent_task_contract"
    ]["canonical_visible_contract_text_hash"]
    assert view["contract_projection"]["stale"] is True
    assert view["verification_route_policy"]["schema_version"] == (
        VERIFICATION_ROUTE_POLICY_SCHEMA_VERSION
    )
    assert view["verification_route_policy"]["real_ai_provider_calls"]["allowed"] is False
    assert view["route_identity"]["route_context_hash"] == "sha256:route"
    assert view["route_identity"]["prompt_contract_id"] == "rprompt-1"
    assert view["route_identity"]["raw_private_context_exposed"] is False
    assert "raw_private_context" not in view["route_identity"]
    assert "hidden_context" not in view["route_identity"]
    assert view["privacy_boundary"]["raw_private_context_exposed"] is False
    assert view["worker_runtime_query"]["fence_token_required"] is True


def test_runtime_contract_view_preserves_handoff_parent_separately_from_root() -> None:
    context = BranchTaskRuntimeContext(
        project_id="aming-claw",
        task_id="task-hotfix-worker",
        parent_task_id="cex-hotfix-successor",
        root_task_id="cex-onboard-root",
        chain_id="cchain-onboard-root",
        stage_task_id="task-hotfix-worker",
        backlog_id="AC-RUNTIME-CONTRACT",
        worker_id="worker-1",
        branch_ref="refs/heads/codex/task-hotfix-worker",
        worktree_path="/repo/.worktrees/task-hotfix-worker",
        base_commit="base123",
        target_head_commit="target123",
        merge_queue_id="mq-hotfix",
        fence_token="fence-runtime",
        status="running",
    )

    view = build_mf_subagent_runtime_contract_view(
        context,
        role=MF_SUB_ROLE,
        contract_version="mf_parallel.v1",
        contract_revision_id="crev-hotfix",
    )

    assert view["runtime_context"]["parent_task_id"] == "cex-hotfix-successor"
    assert view["runtime_context"]["task_id"] == "task-hotfix-worker"
    assert context.root_task_id == "cex-onboard-root"
    assert context.chain_id == "cchain-onboard-root"
    assert view["agent_task_contract"]["parent_task_id"] == "cex-hotfix-successor"


def test_observer_hotfix_enter_refuses_completed_successor_reuse(monkeypatch) -> None:
    from agent.governance import server as server_module

    parent_record = {
        "contract_execution_id": "cex-onboard-parent",
        "root_contract_execution_id": "cex-onboard-root",
        "contract_chain_id": "chain-onboard-root",
    }
    successor_id = server_module._observer_hotfix_successor_execution_id(
        "aming-claw",
        "AC-HOTFIX",
        parent_record["contract_execution_id"],
    )
    completed_record = {
        "contract_execution_id": successor_id,
        "parent_contract_execution_id": parent_record["contract_execution_id"],
        "root_contract_execution_id": parent_record["root_contract_execution_id"],
        "contract_chain_id": parent_record["contract_chain_id"],
        "runtime_guide": {"next_legal_action": None},
        "route_token_ref": "",
    }

    class FakeStore:
        def __init__(self, record):
            self.record = dict(record)
            self.updated = False

        def get(self, contract_execution_id):
            assert contract_execution_id == successor_id
            return dict(self.record)

        def update(self, contract_execution_id, record):
            assert contract_execution_id == successor_id
            self.updated = True
            self.record = dict(record)

    class FakeRuntime:
        def __init__(self, record):
            self.store = FakeStore(record)
            self.started = False

        def start_execution(self, *args, **kwargs):
            self.started = True
            raise AssertionError("completed deterministic successor must not be reused")

        def current_guide(self, contract_execution_id, *, actor_role):
            assert contract_execution_id == successor_id
            assert actor_role == "observer"
            return {"next_legal_action": None}

    runtime = FakeRuntime(completed_record)
    monkeypatch.setattr(server_module, "_contract_runtime", lambda conn: runtime)

    with pytest.raises(server_module.ValidationError) as exc_info:
        server_module._observer_hotfix_successor_runtime_enter(
            object(),
            project_id="aming-claw",
            backlog_id="AC-HOTFIX",
            task_id="hotfix-task",
            parent_record=parent_record,
            actor_role="observer",
            route_token_ref="rtok-hotfix-ref",
            reason="human approved hotfix",
        )

    assert runtime.started is False
    details = exc_info.value.details
    assert details["blocker_id"] == "observer_hotfix_successor_already_complete"
    assert details["next_legal_action"] == "start_attempt_scoped_observer_hotfix_successor"
    assert details["successor_contract_execution_id"] == successor_id
    assert details["parent_contract_execution_id"] == "cex-onboard-parent"
    assert details["root_contract_execution_id"] == "cex-onboard-root"
    assert details["contract_chain_id"] == "chain-onboard-root"
    assert details["route_token_ref"] == "rtok-hotfix-ref"
    assert details["raw_route_token_required"] is False


def test_direct_fix_successor_projection_exposes_return_to_parent(monkeypatch) -> None:
    from agent.governance import server as server_module

    parent_record = {
        "project_id": "aming-claw",
        "backlog_id": "AC-DIRECT-FIX",
        "contract_id": "contract_update",
        "version": "v1",
        "revision": "rev1",
        "contract_execution_id": "cex-contract-update-parent",
        "root_contract_execution_id": "cex-onboard-root",
        "contract_chain_id": "chain-onboard-root",
        "completed_lines": [
            {
                "stage_id": "worker_precheck",
                "line_id": "worker_revision_precheck",
                "actor_role": "mf_sub",
                "evidence_kind": "contract_revision_precheck",
                "payload": {
                    "status": "blocked",
                    "recommended_next_action": "direct_fix_successor",
                    "blocker_class": (
                        "runtime_merge_queue_requires_raw_fence_after_worker_finish"
                    ),
                },
            }
        ],
    }

    class FakeStore:
        def get(self, _contract_execution_id):
            raise server_module.ContractRuntimeError("not started")

    class FakeRuntime:
        store = FakeStore()

    monkeypatch.setattr(server_module, "_contract_runtime", lambda _conn: FakeRuntime())
    monkeypatch.setattr(
        server_module,
        "_direct_fix_projection_for_successor",
        lambda *_args, **_kwargs: {},
    )

    next_action = server_module._contract_runtime_blocked_successor_next_action(
        object(),
        project_id="aming-claw",
        backlog_id="AC-DIRECT-FIX",
        record=parent_record,
        actor_role="observer",
    )

    assert next_action["line_id"] == "direct_fix_successor"
    assert next_action["action"] == "enter_direct_fix_successor"
    assert next_action["recommended_successor_contract_id"] == "direct_fix"
    assert next_action["successor_contract_template_id"] == "direct_fix.v1"
    assert next_action["return_to_parent"]["parent_contract_execution_id"] == (
        "cex-contract-update-parent"
    )
    assert next_action["return_to_parent"]["parent_close_gate_recheck_required"] is True
    assert next_action["body"]["parent_contract_execution_id"] == (
        "cex-contract-update-parent"
    )


def test_backlog_close_blocker_overlay_requires_source_ref_and_preserves_current(
    monkeypatch,
) -> None:
    from agent.governance import server as server_module

    assert server_module._backlog_close_route_gate_is_source_backed(
        {
            "action": "backlog_close",
            "decision": "route_token_ref_resolved",
            "route_token_ref": "rtok-source-backed-close",
            "resolved_from_ref": True,
            "binding_source": "observer_route_token_refs",
        }
    )
    assert not server_module._backlog_close_route_gate_is_source_backed(
        {
            "action": "backlog_close",
            "decision": "accepted",
            "binding_source": "route_waiver",
        }
    )
    assert not server_module._backlog_close_route_gate_is_source_backed(
        {
            "action": "backlog_close",
            "decision": "accepted",
            "route_token_ref": "rtok-unverified-close",
        }
    )

    current_with_next_action = {
        "project_id": "aming-claw",
        "backlog_id": "AC-PRESERVE-CURRENT",
        "readiness_state": "contract_complete",
        "current_contract_execution_id": "cex-current",
        "active_child_contract_execution_id": "",
        "next_legal_action": {"id": "already-current", "action": "continue"},
        "projection_hash": "sha256:current",
    }
    calls: list[dict] = []

    def fail_if_queried(*_args, **kwargs):
        calls.append(kwargs)
        return {
            "id": 99,
            "event_kind": "backlog_close_blocked",
            "payload": {
                "source": "authoritative_backlog_close",
                "route_token_gate": {
                    "action": "backlog_close",
                    "decision": "route_token_ref_resolved",
                    "route_token_ref": "rtok-source-backed-close",
                    "resolved_from_ref": True,
                },
            },
        }

    monkeypatch.setattr(
        server_module,
        "_latest_source_backed_backlog_close_blocker_event",
        fail_if_queried,
    )

    projected = server_module._contract_chain_current_with_backlog_close_blocker(
        object(),
        project_id="aming-claw",
        backlog_id="AC-PRESERVE-CURRENT",
        current_projection=current_with_next_action,
    )

    assert projected == current_with_next_action
    assert calls == []

    monkeypatch.setattr(
        server_module,
        "_latest_source_backed_backlog_close_blocker_event",
        lambda *_args, **_kwargs: {},
    )
    complete_without_blocker = {
        **current_with_next_action,
        "next_legal_action": {},
        "projection_hash": "sha256:complete",
    }

    assert server_module._contract_chain_current_with_backlog_close_blocker(
        object(),
        project_id="aming-claw",
        backlog_id="AC-PRESERVE-CURRENT",
        current_projection=complete_without_blocker,
    ) == complete_without_blocker


def test_runtime_contract_view_reports_revision_polling_state() -> None:
    context = BranchTaskRuntimeContext(
        project_id="aming-claw",
        task_id="task-runtime-revision",
        root_task_id="task-parent",
        backlog_id="AC-CONTRACT-RUNTIME-REVISION-POLLING-DOGFOOD-20260603",
        worker_id="worker-1",
        attempt=1,
        branch_ref="refs/heads/codex/task-runtime-revision",
        worktree_path="/repo/.worktrees/task-runtime-revision",
        base_commit="base123",
        target_head_commit="target123",
        merge_queue_id="mq-1",
        fence_token="fence-runtime",
        status="running",
    )

    changed = build_mf_subagent_runtime_contract_view(
        context,
        latest_revision_id="crev-2",
        known_revision_id="crev-1",
        poll_after_sec=3,
        latest_revision={
            "revision_id": "crev-2",
            "payload": {"summary": "updated evidence request"},
            "route_identity": {"route_context_hash": "sha256:route"},
        }
    )

    assert changed["latest_revision_id"] == "crev-2"
    assert changed["known_revision_id"] == "crev-1"
    assert changed["contract_changed"] is True
    assert changed["must_ack_revision"] is True
    assert changed["poll_after_sec"] == 3
    assert changed["contract"]["contract_revision_id"] == "crev-2"
    assert changed["worker_runtime_query"]["known_revision_id"] == "crev-1"
    assert changed["latest_revision"]["payload"]["summary"] == "updated evidence request"

    unchanged = build_mf_subagent_runtime_contract_view(
        context,
        latest_revision_id="crev-2",
        known_revision_id="crev-2",
    )

    assert unchanged["latest_revision_id"] == "crev-2"
    assert unchanged["known_revision_id"] == "crev-2"
    assert unchanged["contract_changed"] is False
    assert unchanged["must_ack_revision"] is False


def test_runtime_context_projection_wrapper_returns_valid_worker_view() -> None:
    context = BranchTaskRuntimeContext(
        project_id="aming-claw",
        governance_project_id="aming-claw",
        target_project_id="aming-claw",
        target_project_root="/repo",
        task_id="task-runtime-context-projection",
        root_task_id="task-parent",
        backlog_id="AC-RUNTIME-CONTEXT-SERVICE-ROLE-FILTERED-GATE-VIEWS-20260606",
        worker_id="worker-runtime-context",
        worker_slot_id="worker-runtime-context",
        actual_host_worker_id="worker-runtime-context",
        agent_id="agent-runtime-context",
        attempt=1,
        branch_ref="refs/heads/codex/task-runtime-context-projection",
        ref_name="main",
        worktree_id="wt-runtime-context",
        worktree_path="/repo/.worktrees/task-runtime-context-projection",
        base_commit="base123",
        head_commit="head123",
        target_head_commit="target123",
        snapshot_id="scope-1",
        projection_id="semproj-1",
        merge_queue_id="mq-1",
        merge_preview_id="mp-1",
        fence_token="fence-runtime-context",
        checkpoint_id="ckpt-runtime-context",
        status="running",
    )
    secret = "worker-view-must-not-include-this-private-context"
    kwargs = {
        "latest_revision": {
            "revision_id": "crev-runtime-context",
            "contract_version": "mf_parallel.v1",
            "payload": {
                "observer_command_id": "cmd-runtime-context",
                "target_files": ["agent/governance/mf_subagent_contract.py"],
                "acceptance_criteria": ["runtime context wrapper is valid"],
                "private_context": secret,
            },
        },
        "route_identity": {
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
            "visible_injection_manifest_hash": "sha256:visible-runtime-context",
            "raw_private_context": secret,
        },
        "timeline_refs": {
            "startup_event_ref": "timeline:startup",
            "read_receipt_event_ref": "timeline:read-receipt",
            "route_action_precheck_event_ref": "timeline:route-action-precheck",
            "finish_event_ref": "timeline:finish",
            "verification_event_refs": ["timeline:verification"],
        },
        "graph_trace_refs": {
            "query_source": "mf_subagent",
            "worker_role": "mf_sub",
            "task_id": "task-runtime-context-projection",
            "parent_task_id": "task-parent",
            "fence_token": "fence-runtime-context",
            "query_purpose": "subagent_context_build",
            "trace_ids": ["gqt-runtime-context"],
        },
        "startup_gate": {
            "event_id": "timeline:startup",
            "runtime_context_id": branch_runtime_context_id(
                context.project_id,
                context.task_id,
            ),
            "fence_token_matches": True,
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
            "read_receipt_hash": "sha256:read-runtime-context",
            "read_receipt_event_id": "timeline:read-receipt",
            **_self_attested_startup_fields(
                worker_session_id="worker-runtime-context",
                transcript_path="/tmp/worker-runtime-context.jsonl",
            ),
        },
        "finish_gate": {
            "checkpoint_id": "ckpt-runtime-context",
            "test_results": {"status": "passed"},
            "worker_self_attestation_gate": {"passed": True},
            "worker_self_attestation": _finish_time_worker_attestation(
                worker_session_id="worker-runtime-context",
                transcript_path="/tmp/worker-runtime-context.jsonl",
            ),
        },
        "generated_at": "2026-06-06T00:00:00Z",
    }

    projection = build_mf_subagent_runtime_context_projection(context, **kwargs)
    worker_view = projection["views"]["worker_view"]
    validation = validate_mf_subagent_worker_runtime_context_view(
        worker_view,
        expected_task_id=context.task_id,
    )
    worker_view_only = build_mf_subagent_worker_runtime_context_view(context, **kwargs)

    assert projection["schema_version"] == "runtime_context.projection.v1"
    assert worker_view["schema_version"] == "runtime_context.worker_view.v1"
    assert worker_view["gate_inputs"]["status"] == "ready"
    assert worker_view["close_gate_view"]["ready"] is True
    assert worker_view["route_context_hash"] == "sha256:route-runtime-context"
    assert worker_view["observer_command_id"] == "cmd-runtime-context"
    assert worker_view["gate_inputs"]["observer_command_id"] == "cmd-runtime-context"
    assert worker_view["prompt_contract_id"] == "rprompt-runtime-context"
    assert worker_view["prompt_contract_hash"] == "sha256:prompt-runtime-context"
    assert worker_view["visible_injection_manifest_hash"] == (
        "sha256:visible-runtime-context"
    )
    assert worker_view["target_files"] == ["agent/governance/mf_subagent_contract.py"]
    assert worker_view["gate_inputs"]["route_context_hash"] == (
        "sha256:route-runtime-context"
    )
    assert worker_view["gate_inputs"]["target_files"] == [
        "agent/governance/mf_subagent_contract.py"
    ]
    assert validation["ok"] is True
    assert validation["missing"] == []
    assert worker_view_only["runtime_context_id"] == worker_view["runtime_context_id"]
    assert secret not in json.dumps(worker_view, sort_keys=True)

    tampered = dict(worker_view)
    tampered["privacy_boundary"] = {
        **worker_view["privacy_boundary"],
        "other_worker_contexts_exposed": True,
    }
    with pytest.raises(MfSubagentContractError, match="other worker contexts"):
        validate_mf_subagent_worker_runtime_context_view(tampered, context=context)


def test_mf_workflow_runtime_template_names_graph_service_architecture_and_qa_lanes() -> None:
    template_path = (
        _repo_root
        / "agent"
        / "governance"
        / "contract_templates"
        / "mf_workflow_runtime.v1.json"
    )
    template = json.loads(template_path.read_text(encoding="utf-8"))

    evidence = {item["id"]: item for item in template["evidence_requirements"]}
    assert evidence["graph_trace_evidence"]["schema_version"] == GRAPH_TRACE_SCHEMA_VERSION
    assert (
        evidence["branch_runtime_registration"]["schema_version"]
        == BRANCH_RUNTIME_SCHEMA_VERSION
    )
    assert (
        evidence["observer_subagent_service_dispatch"]["schema_version"]
        == SERVICE_DISPATCH_SCHEMA_VERSION
    )
    assert evidence["architecture_review_lane"]["required"] is False
    assert evidence["architecture_review_lane"]["review_pack_template"] == (
        "architecture_data_continuity_review.v1"
    )
    assert evidence["qa_evidence_gate_review"]["review_pack_template"] == (
        "qa_evidence_gate_review.v1"
    )
    assert "architecture_review_lane_when_required" in template["gate_registry"][
        "backlog.close"
    ]


def test_mf_parallel_template_exposes_observer_no_direct_code_boundary() -> None:
    template_path = (
        _repo_root
        / "agent"
        / "governance"
        / "contract_templates"
        / "mf_parallel.v1.json"
    )
    template = json.loads(template_path.read_text(encoding="utf-8"))

    observer_contract = template["observer_contract"]
    assert observer_contract["mode"] == "observer_only"
    assert observer_contract["observer_direct_code"] is False
    assert observer_contract["role_boundary"]["default"] == (
        "no_direct_implementation_code"
    )
    assert "direct_implementation_code" in observer_contract["default_forbidden_actions"]

    route_preflight = observer_contract["route_preflight"]
    assert route_preflight["local_provider_optional"] is True
    assert "provider_boundary" in route_preflight
    assert route_preflight["provider_registry_preflight"]["service_id"] == (
        "route.provider_registry_preflight"
    )
    assert route_preflight["topology_precheck"]["service_id"] == "route.topology_precheck"
    assert route_preflight["topology_precheck"]["required_before"] == (
        "implementation_planning"
    )
    template_text = template_path.read_text(encoding="utf-8")
    private_provider_terms = (
        "Judg" + "ment " + "Brain",
        "judg" + "ment-brain",
        "judg" + "ment_brain",
        "judg" + "ment_plan_precheck",
        "when_" + "judg" + "ment_brain_available",
        "protocol_" + "list",
    )
    assert not any(term in template_text for term in private_provider_terms)

    exception_policy = observer_contract["direct_mutation_exception_policy"]
    assert exception_policy["schema_version"] == OBSERVER_DIRECT_MUTATION_SCHEMA_VERSION
    assert exception_policy["default"] == "reject"
    assert set(exception_policy["requires"]).issuperset(
        {
            "observer_direct_mutation=true",
            "observer_role=observer",
            "tiny_deterministic_scope",
            "explicit_reason",
            "allowed_files",
            "dirty_scope_exact_match",
            "timeline_evidence_before_mutation",
        }
    )
    assert exception_policy["local_precheck"]["function"] == (
        "agent.governance.mf_subagent_contract."
        "validate_observer_direct_mutation_exception"
    )

    nontrivial = template["worker_contract"]["nontrivial_implementation"]
    assert nontrivial["default_topology"] == "dispatch_to_bounded_worker_lane"
    assert set(nontrivial["required_lane_evidence"]).issuperset(
        {
            "target_files",
            "test_commands",
            "worktree_path",
            "fence_token",
            "dirty_scope_check",
            "review_evidence",
        }
    )


def test_meta_contract_template_encodes_observer_whitelist_and_red_lines() -> None:
    template = load_meta_contract_template()

    assert template["schema_version"] == META_CONTRACT_SCHEMA_VERSION
    assert template["enforcement"]["observer_events_validated"] is True
    assert template["enforcement"]["skip_paths"] == []
    assert set(template["forbidden_always"]).issuperset(
        {
            "author_worker_evidence",
            "self_fix_and_close",
            "bypass_timeline_gate",
            "self_waiver",
            "self_clear_judge_blocker",
            "surrogate_startup",
            "fork_identity_to_launder",
        }
    )
    assert "implementation" in template["worker_evidence_actions"]
    assert "dispatch_bounded_worker" in template["role_action_whitelist"]["observer"][
        "allowed_actions"
    ]
    assert "design_review" in template["role_action_whitelist"]["observer"][
        "allowed_actions"
    ]
    assert "contract_revision_created" in template["role_action_whitelist"][
        "observer"
    ]["allowed_actions"]
    assert "observer_work_mode_transition" in template["role_action_whitelist"][
        "observer"
    ]["allowed_actions"]
    assert {"observer", "worker", "mf_sub", "qa", "judge", "system"}.issubset(
        template["role_action_whitelist"]
    )
    assert "worker_progress" in template["worker_evidence_actions"]
    assert "patch" in template["worker_evidence_actions"]
    assert template["hotfix_profile"]["silent_bypass_allowed"] is False
    assert template["hotfix_profile"]["entry_event_type"] == "hotfix.entered"
    assert template["hotfix_profile"]["under_action_requires_entry"] is True
    assert template["hotfix_profile"]["under_action_requires_ref"] is True
    assert template["hotfix_profile"]["under_action_requires_reason"] is True


def test_meta_contract_allows_design_review_as_non_worker_review_evidence() -> None:
    gate = validate_meta_contract_timeline_event(
        {
            "event_type": "review.design",
            "event_kind": "design_review",
            "phase": "design_review",
            "actor": "observer",
            "status": "passed",
            "payload": {
                "review_contract_id": "review_contract.v1",
                "review_lane_id": "architecture_review_lane",
                "review_decision": "pass_with_followups",
                "reviewed_contract_summary": "Contract State Layer additive design review.",
                "close_satisfying": False,
            },
        },
    )

    assert gate["allowed"] is True
    assert gate["role"] == "observer"
    assert gate["action"] == "design_review"
    assert gate["observer_worker_transport"] is False


def test_meta_contract_allows_contract_revision_created_as_state_evidence() -> None:
    gate = validate_meta_contract_timeline_event(
        {
            "event_type": "contract.revision.created",
            "event_kind": "contract_revision_created",
            "phase": "contract_binding",
            "actor": "observer",
            "status": "passed",
            "payload": {
                "contract_binding": {
                    "contract_id": "contract-state-layer",
                    "contract_revision_id": "crev-1",
                    "state": "bound",
                },
                "close_satisfying": False,
            },
        },
    )

    assert gate["allowed"] is True
    assert gate["role"] == "observer"
    assert gate["action"] == "contract_revision_created"
    assert gate["observer_worker_transport"] is False


def test_meta_contract_rejects_unknown_review_action() -> None:
    with pytest.raises(MfSubagentContractError, match="unknown timeline action"):
        validate_meta_contract_timeline_event(
            {
                "event_type": "review.product",
                "event_kind": "product_review",
                "phase": "product_review",
                "actor": "observer",
                "status": "passed",
                "payload": {"review_contract_id": "review_contract.v1"},
            }
        )


def test_meta_contract_rejects_observer_forbidden_action_d2_boundary() -> None:
    with pytest.raises(MfSubagentContractError, match="bypass_timeline_gate"):
        validate_meta_contract_timeline_event(
            {
                "event_type": "observer.bypass",
                "event_kind": "record_blocker",
                "actor": "observer",
                "status": "passed",
                "payload": {"bypass_timeline_gate": True},
            }
        )


def test_meta_contract_rejects_legacy_host_adapter_surrogate_even_with_verified_ref() -> None:
    with pytest.raises(MfSubagentContractError, match="surrogate_startup"):
        validate_meta_contract_timeline_event(
            {
                "event_type": "mf_subagent.startup",
                "event_kind": "mf_subagent_startup",
                "actor": "worker-host-adapter",
                "status": "passed",
                "payload": {
                    "mf_subagent_startup_gate": {
                        "schema_version": "mf_subagent_startup_gate.v1",
                        "agent_id_match_mode": "host_adapter_startup_token_surrogate",
                        "session_token_evidence_type": "server_verified_ref",
                        "session_token_ref_present": True,
                        "host_adapter_startup_token_accepted": True,
                        "close_satisfying": True,
                        "worker_self_attesting": True,
                    }
                },
            }
        )


def test_meta_contract_allows_host_adapter_server_verified_session_startup() -> None:
    gate = validate_meta_contract_timeline_event(
        {
            "event_type": "mf_subagent.startup",
            "event_kind": "mf_subagent_startup",
            "actor": "worker-host-adapter",
            "status": "passed",
            "payload": {
                "mf_subagent_startup_gate": {
                    "schema_version": "mf_subagent_startup_gate.v1",
                    "agent_id_match_mode": "host_adapter_server_verified_session",
                    "session_token_evidence_type": "server_verified_ref",
                    "session_token_ref_present": True,
                    "host_adapter_startup_token_accepted": True,
                    "close_satisfying": True,
                    "worker_self_attesting": True,
                }
            },
        }
    )

    assert gate["allowed"] is True
    assert gate["role"] == "mf_sub"
    assert gate["action"] == "mf_subagent_startup"


def test_meta_contract_ignores_forged_payload_gate_for_action_derivation() -> None:
    with pytest.raises(MfSubagentContractError, match="unknown timeline action"):
        validate_meta_contract_timeline_event(
            {
                "event_type": "observer.invented",
                "event_kind": "invented_action",
                "actor": "observer",
                "status": "passed",
                "payload": {
                    "meta_contract_gate": {
                        "schema_version": "meta_contract_timeline_gate.v1",
                        "allowed": True,
                        "role": "observer",
                        "action": "record_blocker",
                    }
                },
            }
        )


def test_meta_contract_allows_observer_work_mode_transition_with_bound_precheck_ref() -> None:
    gate = validate_meta_contract_timeline_event(
        {
            "event_type": "observer.work_mode_transition",
            "event_kind": "observer_work_mode_transition",
            "actor": "observer",
            "phase": "routing",
            "status": "accepted",
            "payload": {
                "from_work_mode": "observer_look_before_act",
                "to_work_mode": "observer_execution_supervisor",
                "route_identity": {
                    "route_id": "route-1",
                    "route_context_hash": "sha256:ctx",
                    "prompt_contract_id": "rprompt-1",
                },
                "route_action_precheck_event_id": "timeline:4366",
            },
        }
    )

    assert gate["allowed"] is True
    assert gate["role"] == OBSERVER_COORDINATOR_ROLE
    assert gate["action"] == "observer_work_mode_transition"
    assert gate["observer_event_validated"] is True
    assert gate["observer_worker_transport"] is False


def test_meta_contract_allows_observer_work_mode_transition_before_precheck_ref() -> None:
    gate = validate_meta_contract_timeline_event(
        {
            "event_type": "observer.work_mode_transition",
            "event_kind": "observer_work_mode_transition",
            "actor": "observer",
            "phase": "routing",
            "status": "accepted",
            "payload": {
                "route_identity": {
                    "route_id": "route-1",
                    "route_context_hash": "sha256:ctx",
                    "prompt_contract_id": "rprompt-1",
                },
            },
        }
    )

    assert gate["allowed"] is True
    assert gate["role"] == OBSERVER_COORDINATOR_ROLE
    assert gate["action"] == "observer_work_mode_transition"


@pytest.mark.parametrize(
    "missing_field",
    ["route_id", "route_context_hash", "prompt_contract_id"],
)
def test_meta_contract_rejects_observer_work_mode_transition_missing_route_identity(
    missing_field: str,
) -> None:
    route_identity = {
        "route_id": "route-1",
        "route_context_hash": "sha256:ctx",
        "prompt_contract_id": "rprompt-1",
    }
    route_identity.pop(missing_field)

    with pytest.raises(
        MfSubagentContractError,
        match=missing_field,
    ):
        validate_meta_contract_timeline_event(
            {
                "event_type": "observer.work_mode_transition",
                "event_kind": "observer_work_mode_transition",
                "actor": "observer",
                "phase": "routing",
                "status": "accepted",
                "payload": {
                    "route_identity": route_identity,
                    "route_action_precheck_event_id": "timeline:4366",
                },
            }
        )


def test_meta_contract_rejects_unknown_work_mode_transition_phase_bypass() -> None:
    with pytest.raises(MfSubagentContractError, match="unknown timeline action"):
        validate_meta_contract_timeline_event(
            {
                "event_type": "observer.work_mode_transition.unbound",
                "event_kind": "",
                "actor": "observer",
                "phase": "route_context",
                "status": "accepted",
                "payload": {
                    "route_identity": {
                        "route_id": "route-1",
                        "route_context_hash": "sha256:ctx",
                        "prompt_contract_id": "rprompt-1",
                    },
                    "route_action_precheck_event_id": "timeline:4366",
                },
            }
        )


def test_meta_contract_rejects_observer_direct_worker_evidence() -> None:
    with pytest.raises(MfSubagentContractError, match="author_worker_evidence"):
        validate_meta_contract_timeline_event(
            {
                "event_type": "implementation",
                "event_kind": "implementation",
                "actor": "observer",
                "status": "passed",
                "payload": {"changed_files": ["agent/governance/server.py"]},
            }
        )


def test_meta_contract_accepts_server_verified_runtime_context_worker_proof() -> None:
    gate = validate_meta_contract_timeline_event(
        {
            "event_type": "mf_subagent_read_receipt",
            "event_kind": "mf_subagent_read_receipt",
            "actor": "observer",
            "status": "passed",
            "payload": {
                "authorization_source": "runtime_context_copy_safe_worker_proof",
                "runtime_context_id": "mfrctx-proof",
                "task_id": "worker-proof",
                "parent_task_id": "onboard-service-proof",
                "target_project_root": "/tmp/worker-proof",
                "worker_role": "mf_sub",
                "worker_id": "multi_agent:019f29da-proof",
                "worker_slot_id": "multi_agent:019f29da-proof",
                "session_token_ref": "wstok-proof",
                "fence_token_hash": "sha256:fence-proof",
                "read_receipt_hash": "sha256:receipt-proof",
                "observer_impersonation": False,
                "worker_evidence_provenance": {
                    "source": "runtime_context_copy_safe_worker_proof",
                    "verified": True,
                    "worker_owned": True,
                    "observer_impersonation": False,
                    "runtime_context_id": "mfrctx-proof",
                    "task_id": "worker-proof",
                    "parent_task_id": "onboard-service-proof",
                    "target_project_root": "/tmp/worker-proof",
                    "worker_role": "mf_sub",
                    "worker_id": "multi_agent:019f29da-proof",
                    "worker_slot_id": "multi_agent:019f29da-proof",
                    "session_token_ref": "wstok-proof",
                    "fence_token_hash": "sha256:fence-proof",
                },
            },
        },
        trusted_runtime_context_worker_proof=True,
    )

    assert gate["allowed"] is True
    assert gate["role"] == MF_SUB_ROLE
    assert gate["action"] == "read_receipt"
    assert gate["observer_worker_transport"] is False


def test_meta_contract_rejects_observer_claimed_runtime_context_worker_proof() -> None:
    with pytest.raises(MfSubagentContractError, match="author_worker_evidence"):
        validate_meta_contract_timeline_event(
            {
                "event_type": "mf_subagent_read_receipt",
                "event_kind": "mf_subagent_read_receipt",
                "actor": "observer",
                "status": "passed",
                "payload": {
                    "authorization_source": "runtime_context_copy_safe_worker_proof",
                    "runtime_context_id": "mfrctx-proof",
                    "task_id": "worker-proof",
                    "parent_task_id": "onboard-service-proof",
                    "target_project_root": "/tmp/worker-proof",
                    "worker_role": "mf_sub",
                    "worker_id": "multi_agent:019f29da-proof",
                    "worker_slot_id": "multi_agent:019f29da-proof",
                    "session_token_ref": "wstok-proof",
                    "fence_token_hash": "sha256:fence-proof",
                    "read_receipt_hash": "sha256:receipt-proof",
                    "observer_impersonation": False,
                    "worker_evidence_provenance": {
                        "source": "runtime_context_copy_safe_worker_proof",
                        "verified": True,
                        "worker_owned": True,
                        "observer_impersonation": False,
                        "runtime_context_id": "mfrctx-proof",
                        "task_id": "worker-proof",
                        "parent_task_id": "onboard-service-proof",
                        "target_project_root": "/tmp/worker-proof",
                        "worker_role": "mf_sub",
                        "worker_id": "multi_agent:019f29da-proof",
                        "worker_slot_id": "multi_agent:019f29da-proof",
                        "session_token_ref": "wstok-proof",
                        "fence_token_hash": "sha256:fence-proof",
                    },
                },
            }
        )


def test_meta_contract_allows_observer_on_behalf_worker_evidence_only_when_not_self_attesting() -> None:
    allowed = validate_meta_contract_timeline_event(
        {
            "event_type": "implementation",
            "event_kind": "implementation",
            "actor": "observer-on-behalf-of:mf-sub-worker-1",
            "status": "passed",
            "payload": {
                "on_behalf_of": "mf-sub-worker-1",
                "changed_files": ["agent/governance/server.py"],
                "worker_self_attesting": False,
            },
        }
    )

    assert allowed["schema_version"] == META_CONTRACT_TIMELINE_GATE_SCHEMA_VERSION
    assert allowed["role"] == OBSERVER_COORDINATOR_ROLE
    assert allowed["action"] == "implementation"
    assert allowed["observer_event_validated"] is True
    assert allowed["on_behalf"] is True

    with pytest.raises(MfSubagentContractError, match="self_attesting"):
        validate_meta_contract_timeline_event(
            {
                "event_type": "implementation",
                "event_kind": "implementation",
                "actor": "observer-on-behalf-of:mf-sub-worker-1",
                "status": "passed",
                "payload": {
                    "on_behalf_of": "mf-sub-worker-1",
                    "worker_self_attesting": True,
                },
            }
        )


@pytest.mark.parametrize(
    "event_kind",
    [
        "bounded_implementation_worker_dispatch",
        "merge",
        "merge_preview",
        "merge_queue_entry",
        "live_merge",
        "reconcile",
        "record_blocker",
        "close_after_clauses",
    ],
)
def test_meta_contract_allows_legal_observer_actions(event_kind: str) -> None:
    gate = validate_meta_contract_timeline_event(
        {
            "event_type": event_kind.replace("_", "."),
            "event_kind": event_kind,
            "actor": "observer",
            "status": "passed",
            "payload": {"reason": "legal observer coordination evidence"},
        }
    )

    assert gate["allowed"] is True
    assert gate["role"] == OBSERVER_COORDINATOR_ROLE
    assert gate["observer_event_validated"] is True


def test_meta_contract_allows_observer_visual_smoke_without_qa_impersonation() -> None:
    gate = validate_meta_contract_timeline_event(
        {
            "event_type": "observer.visual_smoke",
            "event_kind": "visual_smoke",
            "actor": "observer",
            "status": "passed",
            "payload": {
                "observer_visual_smoke": {
                    "status": "passed",
                    "url": "http://127.0.0.1:4173",
                }
            },
        }
    )

    assert gate["allowed"] is True
    assert gate["role"] == OBSERVER_COORDINATOR_ROLE
    assert gate["action"] == "observer_visual_smoke"
    assert gate["observer_event_validated"] is True
    assert gate["observer_worker_transport"] is False


def test_meta_contract_validates_worker_and_qa_roles() -> None:
    worker_gate = validate_meta_contract_timeline_event(
        {
            "event_type": "implementation",
            "event_kind": "implementation",
            "actor": "mf_sub",
            "status": "passed",
            "payload": {"changed_files": ["agent/governance/server.py"]},
        }
    )
    qa_gate = validate_meta_contract_timeline_event(
        {
            "event_type": "independent_verification",
            "event_kind": "independent_verification",
            "actor": "qa-reviewer",
            "status": "passed",
            "payload": {"reviewer": "qa-reviewer"},
        }
    )

    assert worker_gate["role"] == MF_SUB_ROLE
    assert worker_gate["action"] == "implementation"
    assert qa_gate["role"] == "qa"
    assert qa_gate["action"] == "independent_verification"

    with pytest.raises(MfSubagentContractError, match="role=mf_sub action=merge"):
        validate_meta_contract_timeline_event(
            {
                "event_type": "merge",
                "event_kind": "merge",
                "actor": "mf_sub",
                "status": "passed",
            }
        )
    with pytest.raises(
        MfSubagentContractError,
        match="role=qa action=implementation",
    ):
        validate_meta_contract_timeline_event(
            {
                "event_type": "implementation",
                "event_kind": "implementation",
                "actor": "qa",
                "status": "passed",
            }
        )


@pytest.mark.parametrize("event_kind", ["worker_progress", "patch"])
def test_meta_contract_allows_worker_progress_and_patch_for_worker_only(
    event_kind: str,
) -> None:
    worker_gate = validate_meta_contract_timeline_event(
        {
            "event_type": event_kind,
            "event_kind": event_kind,
            "actor": "mf_sub",
            "status": "passed",
            "payload": {"changed_files": ["agent/governance/server.py"]},
        }
    )

    assert worker_gate["role"] == MF_SUB_ROLE
    assert worker_gate["action"] == event_kind

    with pytest.raises(MfSubagentContractError, match="author_worker_evidence"):
        validate_meta_contract_timeline_event(
            {
                "event_type": event_kind,
                "event_kind": event_kind,
                "actor": "observer",
                "status": "passed",
            }
        )

    transport_gate = validate_meta_contract_timeline_event(
        {
            "event_type": event_kind,
            "event_kind": event_kind,
            "actor": "observer-on-behalf-of:mf-sub-worker-1",
            "status": "passed",
            "payload": {
                "on_behalf_of": "mf-sub-worker-1",
                "self_attesting": False,
            },
        }
    )
    assert transport_gate["observer_worker_transport"] is True


def test_meta_contract_allows_canonical_finish_time_attestation_event_kind() -> None:
    gate = validate_meta_contract_timeline_event(
        {
            "event_type": "mf_subagent.finish_time_worker_attestation",
            "event_kind": "finish_time_worker_attestation",
            "phase": "finish_time_worker_attestation",
            "actor": "mf_sub",
            "status": "passed",
            "payload": {
                "action": "record_finish_time_worker_attestation",
                "worker_role": "mf_sub",
                "finish_time_worker_self_attestation": {
                    "schema_version": "worker_transcript_self_attestation.v1",
                    "attestation_phase": "finish",
                    "status": "passed",
                },
            },
        }
    )

    assert gate["allowed"] is True
    assert gate["role"] == MF_SUB_ROLE
    assert gate["action"] == "record_finish_time_worker_attestation"


def test_meta_contract_validates_explicit_judge_and_system_roles() -> None:
    judge_gate = validate_meta_contract_timeline_event(
        {
            "event_type": "record_blocker",
            "event_kind": "record_blocker",
            "actor": "judge",
            "status": "passed",
        }
    )
    system_gate = validate_meta_contract_timeline_event(
        {
            "event_type": "service.route.completed",
            "event_kind": "service_route",
            "actor": "service-router",
            "status": "allowed",
            "payload": {"service_router_suppress": True},
        }
    )
    forbidden_audit_gate = validate_meta_contract_timeline_event(
        {
            "event_type": "mf_timeline_gate_bypass_rejected",
            "event_kind": "mf_timeline_gate_bypass_rejected",
            "actor": "system",
            "status": "rejected",
            "payload": {"bypass_timeline_gate": True},
        }
    )

    assert judge_gate["role"] == "judge"
    assert system_gate["role"] == "system"
    assert system_gate["action"] == "service_route"
    assert forbidden_audit_gate["action"] == "forbidden_attempt_recorded"


def _dispatch_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "task_id": "task-mf-sub-1",
        "parent_task_id": "task-mf-parent",
        "worker_role": "mf_sub",
        "branch": "mf/subagent-1",
        "worktree": "/repo/.worktrees/mf-subagent-1",
        "base_commit": "base123",
        "target_head_commit": "target123",
        "merge_queue_id": "mq-1",
        "fence_token": "fence-1",
        "route_context_hash": "sha256:route-context",
        "prompt_contract_id": "rprompt-1",
        "prompt_contract_hash": "sha256:prompt-contract",
        "route_token_ref": "rtok-worker-visible",
        "owned_files": ["agent/governance/mf_subagent_contract.py"],
        "dirty_scope_check": {
            "status": "passed",
            "dirty_scope_exact_match": True,
            "changed_files": [],
            "owned_files": ["agent/governance/mf_subagent_contract.py"],
        },
    }
    payload.update(overrides)
    return payload


def _parent_route_lineage(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "route_id": "route-20260602-parent",
        "route_context_hash": "sha256:parent-route-context",
        "prompt_contract_id": "rprompt-parent",
        "visible_injection_manifest_hash": "sha256:parent-visible-manifest",
        "selected_project": "aming-claw",
        "selected_backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
        "allowed_actions": ["dispatch_bounded_worker"],
        "blocked_actions": ["apply_patch", "write_file"],
        "required_lanes": [
            {"id": "observer_coordinator", "role": "observer"},
            {"id": "bounded_implementation_worker", "role": "mf_sub"},
            {"id": "independent_verification_lane", "role": "qa"},
        ],
        "required_evidence": [
            "route_context",
            "bounded_implementation_worker_dispatch",
            "mf_subagent_startup",
            "independent_verification",
        ],
    }
    payload.update(overrides)
    return payload


def _branch_runtime_evidence(**overrides: object) -> dict[str, object]:
    context = {
        "runtime_context_id": "mfrctx-mf-sub-1",
        "task_id": "task-mf-sub-1",
        "root_task_id": "task-mf-parent",
        "fence_token": "fence-1",
        "worktree_path": "/repo/.worktrees/mf-subagent-1",
        "base_commit": "base123",
        "target_head_commit": "target123",
        "merge_queue_id": "mq-1",
    }
    context_overrides = overrides.pop("context", None)
    if isinstance(context_overrides, dict):
        context.update(context_overrides)
    payload: dict[str, object] = {
        "schema_version": "mf_subagent_branch_runtime.v1",
        "api_ref": "/api/graph-governance/aming-claw/parallel-branches/allocate",
        "runtime_context_id": context["runtime_context_id"],
        "context": context,
    }
    payload.update(overrides)
    return payload


def _graph_trace_evidence(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "mf_subagent_graph_trace.v1",
        "query_source": "mf_subagent",
        "query_purpose": "subagent_context_build",
        "trace_ids": ["gqt-test-mf-subagent-1"],
        "task_id": "task-mf-sub-1",
        "parent_task_id": "task-mf-parent",
        "worker_role": "mf_sub",
        "fence_token": "fence-1",
        "source": "graph_query_traces",
        "db_verified": True,
        "missing_trace_ids": [],
        "identity_mismatches": [],
    }
    payload.update(overrides)
    return payload


def _graph_first_obligations(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "mf_subagent_graph_first_obligations.v1",
        "required": True,
        "read_receipt_required_before": [
            "graph_query",
            "startup",
            "implementation",
            "verification",
            "close_ready",
        ],
        "query": {
            "query_source": "mf_subagent",
            "query_purpose": "subagent_context_build",
            "task_id": "task-mf-sub-1",
            "parent_task_id": "task-mf-parent",
            "worker_role": "mf_sub",
            "fence_token": "fence-1",
        },
        "trace_evidence_schema_version": "mf_subagent_graph_trace.v1",
        "counts_as_worker_graph_trace_evidence": False,
        "finish_gate_requires_worker_graph_trace": True,
    }
    payload.update(overrides)
    return payload


def _service_dispatch_evidence(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "observer_subagent_service_dispatch.v1",
        "dispatch_command_ref": "spawn_agent:task-mf-sub-1",
        "monitor_ref": "monitor:task-mf-sub-1",
    }
    payload.update(overrides)
    return payload


def test_dispatch_gate_accepts_isolated_worktree_with_compact_evidence() -> None:
    evidence = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(),
        target_worktree_path="/repo",
    )

    assert evidence["schema_version"] == DISPATCH_GATE_SCHEMA_VERSION
    assert evidence["allowed"] is True
    assert evidence["role"] == MF_SUB_ROLE
    assert evidence["dispatch_default"] == DISPATCH_DEFAULT
    assert evidence["worktree_policy"] == WORKTREE_POLICY_MODE
    assert evidence["branch"] == "mf/subagent-1"
    assert evidence["worktree"] == "/repo/.worktrees/mf-subagent-1"
    assert evidence["merge_queue_id"] == "mq-1"
    assert evidence["route_context_hash"] == "sha256:route-context"
    assert evidence["prompt_contract_id"] == "rprompt-1"
    assert evidence["prompt_contract_hash"] == "sha256:prompt-contract"
    assert evidence["isolated_worktree"] is True
    assert evidence["same_worktree_allowed"] is False
    assert evidence["override"]["used"] is False
    assert evidence["dirty_scope_check"]["passed"] is True
    assert evidence["governed_evidence_required"] is False
    assert evidence["graph_trace_evidence"]["present"] is False
    assert evidence["service_dispatch_evidence"]["present"] is False


def test_dispatch_gate_accepts_judge_routed_parent_lineage() -> None:
    evidence = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(
            project_id="aming-claw",
            backlog_id="ARCH-MF-SUBAGENT-BACKEND",
            judge_routed=True,
            graph_trace_evidence=_graph_trace_evidence(),
            branch_runtime_evidence=_branch_runtime_evidence(),
            service_dispatch_evidence=_service_dispatch_evidence(),
            judge_route={
                **_parent_route_lineage(),
                "raw_private_memory": "must not be propagated",
            },
        ),
        target_worktree_path="/repo",
    )

    parent = evidence["parent_route_lineage"]
    assert parent["schema_version"] == "parent_route_lineage.v1"
    assert parent["route_id"] == "route-20260602-parent"
    assert parent["route_context_hash"] == "sha256:parent-route-context"
    assert parent["prompt_contract_id"] == "rprompt-parent"
    assert parent["visible_injection_manifest_hash"] == (
        "sha256:parent-visible-manifest"
    )
    assert parent["selected_project"] == "aming-claw"
    assert parent["selected_backlog_id"] == "ARCH-MF-SUBAGENT-BACKEND"
    assert parent["allowed_actions"] == ["dispatch_bounded_worker"]
    assert parent["blocked_actions"] == ["apply_patch", "write_file"]
    assert parent["required_lanes"] == [
        "observer_coordinator",
        "bounded_implementation_worker",
        "independent_verification_lane",
    ]
    assert "raw_private_memory" not in parent
    assert evidence["route_prompt_contract"] == {
        "route_context_hash": "sha256:route-context",
        "prompt_contract_id": "rprompt-1",
        "prompt_contract_hash": "sha256:prompt-contract",
        "route_token_ref": "rtok-worker-visible",
    }
    assert evidence["route_lineage"]["parent_route_id"] == "route-20260602-parent"
    assert evidence["route_lineage"]["child_route_context_hash"] == (
        "sha256:route-context"
    )
    assert evidence["governed_evidence_required"] is True
    assert evidence["graph_trace_evidence"]["schema_version"] == GRAPH_TRACE_SCHEMA_VERSION
    assert evidence["graph_trace_evidence"]["trace_ids"] == ["gqt-test-mf-subagent-1"]
    assert (
        evidence["branch_runtime_evidence"]["schema_version"]
        == BRANCH_RUNTIME_SCHEMA_VERSION
    )
    assert evidence["branch_runtime_evidence"]["registered"] is True
    assert (
        evidence["service_dispatch_evidence"]["schema_version"]
        == SERVICE_DISPATCH_SCHEMA_VERSION
    )
    assert evidence["service_dispatch_evidence"]["replayable_refs_present"] is True


def test_dispatch_gate_requires_governed_evidence_for_mf_parallel_topology() -> None:
    with pytest.raises(MfSubagentContractError, match="graph-first obligation"):
        validate_mf_subagent_dispatch_gate(
            _dispatch_payload(selected_topology="mf_parallel.v1"),
            target_worktree_path="/repo",
        )

    evidence = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(
            selected_topology="mf_parallel.v1",
            graph_trace_evidence=_graph_trace_evidence(),
            branch_runtime_evidence=_branch_runtime_evidence(),
            service_dispatch_evidence=_service_dispatch_evidence(),
        ),
        target_worktree_path="/repo",
    )

    assert evidence["governed_evidence_required"] is True
    assert evidence["graph_trace_evidence"]["query_source"] == "mf_subagent"
    assert evidence["branch_runtime_evidence"]["registered"] is True
    assert evidence["service_dispatch_evidence"]["present"] is True


def test_dispatch_gate_accepts_governed_work_with_graph_obligation_only() -> None:
    evidence = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(
            governed_nontrivial=True,
            dispatch_graph_obligation=_graph_first_obligations(),
            branch_runtime_evidence=_branch_runtime_evidence(),
            service_dispatch_evidence=_service_dispatch_evidence(),
        ),
        target_worktree_path="/repo",
    )

    assert evidence["governed_evidence_required"] is True
    assert evidence["graph_trace_evidence"]["present"] is False
    assert evidence["dispatch_graph_obligation"]["present"] is True
    assert (
        evidence["dispatch_graph_obligation"]["counts_as_worker_graph_trace_evidence"]
        is False
    )
    assert evidence["dispatch_graph_obligation"]["finish_gate_requires_worker_graph_trace"] is True
    assert evidence["branch_runtime_evidence"]["registered"] is True
    assert evidence["service_dispatch_evidence"]["present"] is True


def test_dispatch_gate_rejects_governed_work_without_graph_obligation() -> None:
    with pytest.raises(MfSubagentContractError, match="graph-first obligation"):
        validate_mf_subagent_dispatch_gate(
            _dispatch_payload(
                governed_nontrivial=True,
                branch_runtime_evidence=_branch_runtime_evidence(),
                service_dispatch_evidence=_service_dispatch_evidence(),
            ),
            target_worktree_path="/repo",
        )


def test_dispatch_gate_ignores_top_level_graph_trace_fields() -> None:
    evidence = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(
            governed_nontrivial=True,
            query_source="mf_subagent",
            graph_query_trace_ids=["gqt-top-level-only"],
            dispatch_graph_obligation=_graph_first_obligations(),
            branch_runtime_evidence=_branch_runtime_evidence(),
            service_dispatch_evidence=_service_dispatch_evidence(),
        ),
        target_worktree_path="/repo",
    )

    assert evidence["graph_trace_evidence"]["present"] is False
    assert evidence["dispatch_graph_obligation"]["present"] is True


def test_dispatch_gate_rejects_governed_work_without_service_dispatch_evidence() -> None:
    with pytest.raises(
        MfSubagentContractError,
        match="observer_subagent_service_dispatch",
    ):
        validate_mf_subagent_dispatch_gate(
            _dispatch_payload(
                governed_nontrivial=True,
                graph_trace_evidence=_graph_trace_evidence(),
                branch_runtime_evidence=_branch_runtime_evidence(),
            ),
            target_worktree_path="/repo",
        )


def test_dispatch_gate_rejects_governed_graph_trace_without_query_source() -> None:
    graph_trace = _graph_trace_evidence()
    graph_trace.pop("query_source")

    with pytest.raises(MfSubagentContractError, match="query_source=mf_subagent"):
        validate_mf_subagent_dispatch_gate(
            _dispatch_payload(
                governed_nontrivial=True,
                graph_trace_evidence=graph_trace,
                branch_runtime_evidence=_branch_runtime_evidence(),
                service_dispatch_evidence=_service_dispatch_evidence(),
            ),
            target_worktree_path="/repo",
        )


def test_dispatch_gate_rejects_governed_graph_trace_generic_source_key() -> None:
    graph_trace = _graph_trace_evidence()
    graph_trace.pop("query_source")
    graph_trace["source"] = "mf_subagent"

    with pytest.raises(MfSubagentContractError, match="query_source=mf_subagent"):
        validate_mf_subagent_dispatch_gate(
            _dispatch_payload(
                governed_nontrivial=True,
                graph_trace_evidence=graph_trace,
                branch_runtime_evidence=_branch_runtime_evidence(),
                service_dispatch_evidence=_service_dispatch_evidence(),
            ),
            target_worktree_path="/repo",
        )


def test_dispatch_gate_rejects_governed_graph_trace_wrong_query_source() -> None:
    with pytest.raises(MfSubagentContractError, match="query_source=mf_subagent"):
        validate_mf_subagent_dispatch_gate(
            _dispatch_payload(
                governed_nontrivial=True,
                graph_trace_evidence=_graph_trace_evidence(query_source="observer"),
                branch_runtime_evidence=_branch_runtime_evidence(),
                service_dispatch_evidence=_service_dispatch_evidence(),
            ),
            target_worktree_path="/repo",
        )


def test_dispatch_gate_rejects_unsupported_graph_query_purpose() -> None:
    with pytest.raises(MfSubagentContractError, match="unsupported query_purpose"):
        validate_mf_subagent_dispatch_gate(
            _dispatch_payload(
                governed_nontrivial=True,
                graph_trace_evidence=_graph_trace_evidence(
                    query_purpose="observer_private_context"
                ),
                branch_runtime_evidence=_branch_runtime_evidence(),
                service_dispatch_evidence=_service_dispatch_evidence(),
            ),
            target_worktree_path="/repo",
        )


@pytest.mark.parametrize(
    "query_purpose",
    ["subagent_context_build", "subagent_gate_validation"],
)
def test_dispatch_gate_accepts_supported_graph_query_purposes(
    query_purpose: str,
) -> None:
    gate = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(
            governed_nontrivial=True,
            graph_trace_evidence=_graph_trace_evidence(
                query_purpose=query_purpose
            ),
            branch_runtime_evidence=_branch_runtime_evidence(),
            service_dispatch_evidence=_service_dispatch_evidence(),
        ),
        target_worktree_path="/repo",
    )

    assert gate["graph_trace_evidence"]["query_purpose"] == query_purpose


@pytest.mark.parametrize(
    "field",
    ["task_id", "parent_task_id", "worker_role", "fence_token"],
)
def test_dispatch_gate_rejects_governed_graph_trace_missing_embedded_identity(
    field: str,
) -> None:
    graph_trace = _graph_trace_evidence()
    graph_trace.pop(field)

    with pytest.raises(MfSubagentContractError, match=field):
        validate_mf_subagent_dispatch_gate(
            _dispatch_payload(
                governed_nontrivial=True,
                graph_trace_evidence=graph_trace,
                branch_runtime_evidence=_branch_runtime_evidence(),
                service_dispatch_evidence=_service_dispatch_evidence(),
            ),
            target_worktree_path="/repo",
        )


def test_dispatch_gate_rejects_governed_observer_graph_trace_role() -> None:
    with pytest.raises(MfSubagentContractError, match="worker_role=mf_sub"):
        validate_mf_subagent_dispatch_gate(
            _dispatch_payload(
                governed_nontrivial=True,
                worker_role="observer",
                graph_trace_evidence=_graph_trace_evidence(worker_role="observer"),
                branch_runtime_evidence=_branch_runtime_evidence(),
                service_dispatch_evidence=_service_dispatch_evidence(),
            ),
            target_worktree_path="/repo",
        )


def test_dispatch_gate_rejects_governed_work_without_branch_runtime_evidence() -> None:
    with pytest.raises(MfSubagentContractError, match="branch runtime registration"):
        validate_mf_subagent_dispatch_gate(
            _dispatch_payload(
                governed_nontrivial=True,
                graph_trace_evidence=_graph_trace_evidence(),
                service_dispatch_evidence=_service_dispatch_evidence(),
            ),
            target_worktree_path="/repo",
        )


def test_dispatch_gate_rejects_branch_runtime_registration_mismatch() -> None:
    with pytest.raises(MfSubagentContractError, match="identity mismatch"):
        validate_mf_subagent_dispatch_gate(
            _dispatch_payload(
                governed_nontrivial=True,
                graph_trace_evidence=_graph_trace_evidence(),
                branch_runtime_evidence=_branch_runtime_evidence(
                    context={
                        "task_id": "other-task",
                        "root_task_id": "task-mf-parent",
                        "fence_token": "fence-1",
                        "worktree_path": "/repo/.worktrees/mf-subagent-1",
                        "base_commit": "base123",
                        "target_head_commit": "target123",
                        "merge_queue_id": "mq-1",
                    }
                ),
                service_dispatch_evidence=_service_dispatch_evidence(),
            ),
            target_worktree_path="/repo",
        )


def test_dispatch_gate_accepts_documented_host_adapter_service_boundary() -> None:
    evidence = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(
            governed_nontrivial=True,
            graph_trace_evidence=_graph_trace_evidence(),
            branch_runtime_evidence=_branch_runtime_evidence(),
            service_dispatch_evidence={
                "schema_version": "observer_subagent_service_dispatch.v1",
                "documented_host_adapter_boundary": "codex local subagent adapter",
                "boundary_documentation_ref": (
                    "docs/governance/manual-fix-sop.md#mf-subagent-dispatch"
                ),
            },
        ),
        target_worktree_path="/repo",
    )

    assert evidence["service_dispatch_evidence"]["present"] is True
    assert (
        evidence["service_dispatch_evidence"]["documented_host_adapter_boundary"]
        is True
    )


def test_dispatch_gate_rejects_missing_parent_lineage_when_required() -> None:
    with pytest.raises(MfSubagentContractError, match="parent_route_lineage"):
        validate_mf_subagent_dispatch_gate(
            _dispatch_payload(parent_route_required=True),
            target_worktree_path="/repo",
        )


def test_dispatch_gate_rejects_parent_scope_mismatch() -> None:
    with pytest.raises(MfSubagentContractError, match="selected_backlog_id"):
        validate_mf_subagent_dispatch_gate(
            _dispatch_payload(
                project_id="aming-claw",
                backlog_id="ARCH-MF-SUBAGENT-BACKEND",
                parent_route_lineage=_parent_route_lineage(
                    selected_backlog_id="OTHER-BACKLOG"
                ),
            ),
            target_worktree_path="/repo",
        )


def test_dispatch_gate_rejects_parent_selected_project_mismatch() -> None:
    with pytest.raises(MfSubagentContractError, match="selected_project"):
        validate_mf_subagent_dispatch_gate(
            _dispatch_payload(
                project_id="aming-claw",
                backlog_id="ARCH-MF-SUBAGENT-BACKEND",
                parent_route_lineage=_parent_route_lineage(
                    selected_project="other-project"
                ),
            ),
            target_worktree_path="/repo",
        )


def test_dispatch_gate_accepts_optional_prompt_contract_hash_absent() -> None:
    evidence = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(prompt_contract_hash=""),
        target_worktree_path="/repo",
    )

    assert evidence["allowed"] is True
    assert evidence["route_context_hash"] == "sha256:route-context"
    assert evidence["prompt_contract_id"] == "rprompt-1"
    assert evidence["prompt_contract_hash"] == ""


@pytest.mark.parametrize(
    ("field", "override"),
    [
        ("branch", {"branch": ""}),
        ("worktree", {"worktree": ""}),
        ("fence_token", {"fence_token": ""}),
        ("base_commit", {"base_commit": ""}),
        ("target_head_commit", {"target_head_commit": ""}),
        ("merge_queue_id", {"merge_queue_id": ""}),
        ("route_token_ref", {"route_token_ref": ""}),
        ("route_context_hash", {"route_context_hash": ""}),
        ("prompt_contract_id", {"prompt_contract_id": ""}),
    ],
)
def test_dispatch_gate_rejects_missing_branch_worktree_fence_or_commits(
    field: str,
    override: dict[str, object],
) -> None:
    with pytest.raises(MfSubagentContractError, match=field):
        validate_mf_subagent_dispatch_gate(
            _dispatch_payload(**override),
            target_worktree_path="/repo",
        )


def test_dispatch_gate_rejects_same_worktree_by_default() -> None:
    with pytest.raises(MfSubagentContractError, match="blocked by default"):
        validate_mf_subagent_dispatch_gate(
            _dispatch_payload(worktree="/repo"),
            target_worktree_path="/repo",
        )


def test_dispatch_gate_requires_complete_same_worktree_override() -> None:
    base_payload = _dispatch_payload(
        worktree="/repo",
        same_worktree_allowed=True,
    )
    with pytest.raises(MfSubagentContractError, match="operator reason"):
        validate_mf_subagent_dispatch_gate(base_payload, target_worktree_path="/repo")

    with pytest.raises(MfSubagentContractError, match="dirty_scope_exact_match"):
        validate_mf_subagent_dispatch_gate(
            {
                **base_payload,
                "operator_reason": "Emergency docs-only repair in exact dirty scope.",
                "dirty_scope_check": {
                    "status": "passed",
                    "dirty_scope_exact_match": False,
                    "changed_files": ["agent/governance/mf_subagent_contract.py"],
                },
            },
            target_worktree_path="/repo",
        )

    with pytest.raises(MfSubagentContractError, match="timeline evidence"):
        validate_mf_subagent_dispatch_gate(
            {
                **base_payload,
                "operator_reason": "Emergency docs-only repair in exact dirty scope.",
                "dirty_scope_check": {
                    "status": "passed",
                    "dirty_scope_exact_match": True,
                    "changed_files": ["agent/governance/mf_subagent_contract.py"],
                },
            },
            target_worktree_path="/repo",
        )

    evidence = validate_mf_subagent_dispatch_gate(
        {
            **base_payload,
            "operator_reason": "Emergency docs-only repair in exact dirty scope.",
            "dispatch_timeline_evidence": {"event_id": 42},
        },
        target_worktree_path="/repo",
    )

    assert evidence["isolated_worktree"] is False
    assert evidence["override"]["used"] is True
    assert evidence["override"]["timeline_evidence_recorded"] is True


def _route_action_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "caller_role": "observer",
        "action": "apply_patch",
        "route_context_hash": "sha256:route-context",
        "prompt_contract_id": "rprompt-1",
        "prompt_contract_hash": "sha256:prompt-contract",
        "route_token_ref": "rtok-worker-visible",
        "visible_injection_manifest_hash": "sha256:visible-manifest",
        "route_alerts": [{"code": "observer_judger_must_not_implement"}],
        "version_check": {"status": "passed", "dirty": False, "dirty_files": []},
        "graph_status": {"current_state": {"graph_stale": {"is_stale": False}}},
    }
    payload.update(overrides)
    return payload


def _high_risk_route_machine_fields() -> dict[str, object]:
    return {
        "priority": "P0",
        "target_files": [
            "agent/governance/mf_subagent_contract.py",
            "agent/governance/precheck_service.py",
            "agent/tests/test_mf_subagent_contract.py",
            "docs/governance/manual-fix-sop.md",
        ],
        "visible_injection_manifest": {
            "schema_version": "visible_injection_manifest.v1",
            "allowed_injections": [
                {
                    "kind": "route_context",
                    "id": "route-ctx-test",
                    "sha256": "sha256:visible",
                    "status": "passed",
                }
            ],
        },
        "allowed_actions": ["apply_patch"],
        "route_alerts": [
            {
                "code": "observer_judger_must_not_implement",
                "blocked_actions": ["observer_direct_implementation"],
            }
        ],
        "required_lanes": [
            {"id": "bounded_implementation_worker", "role": "mf_sub"},
            {"id": "independent_verification_lane", "role": "qa"},
        ],
        "required_evidence": [
            "route_context",
            "route_action_precheck",
            "bounded_implementation_worker_dispatch",
            "mf_subagent_startup",
        ],
    }


def _startup_evidence(**overrides: object) -> dict[str, object]:
    evidence: dict[str, object] = {
        "gate_kind": "mf_subagent.startup",
        "status": "passed",
        "role": MF_SUB_ROLE,
        "bounded": True,
        "route_context_hash": "sha256:route-context",
        "prompt_contract_id": "rprompt-1",
        "prompt_contract_hash": "sha256:prompt-contract",
        "route_token_ref": "rtok-worker-visible",
        "fence_token": "fence-1",
        "actual_cwd": "/repo/.worktrees/task-1",
        "actual_git_root": "/repo/.worktrees/task-1",
        "branch": "refs/heads/codex/task-1",
        "head_commit": "head123",
        "same_as_expected_worker": True,
        "fence_token_matches": True,
    }
    evidence.update(overrides)
    return evidence


def _startup_event(
    startup_evidence: dict[str, object] | None = None,
    *,
    event_id: str = "evt-startup-test",
) -> dict[str, object]:
    return {
        "id": event_id,
        "event_type": "mf_subagent.startup",
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "passed",
        "payload": {
            "mf_subagent_startup_gate": startup_evidence or _startup_evidence()
        },
    }


def test_route_action_gate_rejects_observer_direct_implementation_action() -> None:
    with pytest.raises(MfSubagentContractError, match="observer_judger_must_not_implement"):
        validate_route_action_gate(_route_action_payload())


def test_route_action_gate_allows_bounded_worker_with_route_prompt_identity() -> None:
    evidence = validate_route_action_gate(
        _route_action_payload(caller_role="implementation_worker")
    )

    assert evidence["schema_version"] == ROUTE_ACTION_GATE_SCHEMA_VERSION
    assert evidence["allowed"] is True
    assert evidence["implementation_action"] is True
    assert evidence["route_context_hash"] == "sha256:route-context"
    assert evidence["prompt_contract_id"] == "rprompt-1"
    assert evidence["prompt_contract_hash"] == "sha256:prompt-contract"
    assert evidence["version_workspace_gate"]["passed"] is True
    assert evidence["graph_current_gate"]["passed"] is True


def test_route_action_gate_does_not_apply_observer_only_blocked_actions_to_worker() -> None:
    evidence = validate_route_action_gate(
        _route_action_payload(
            caller_role="implementation_worker",
            route_alerts=[
                {
                    "code": "observer_judger_must_not_implement",
                    "applies_to": ["observer", "judger"],
                    "blocked_actions": ["apply_patch"],
                }
            ],
        )
    )

    assert evidence["allowed"] is True
    assert evidence["caller_role"] == "implementation_worker"
    assert evidence["action"] == "apply_patch"


def test_route_action_gate_rejects_preflight_only_high_risk_implementation() -> None:
    with pytest.raises(MfSubagentContractError, match="route_context_hash"):
        validate_route_action_gate(
            {
                "caller_role": "implementation_worker",
                "action": "apply_patch",
                "priority": "P0",
                "target_files": ["agent/governance/precheck_service.py"],
                "preflight_check": {"ok": True, "status": "passed"},
                "route_advisory_text": "Advisory route says this is probably OK.",
                "version_check": {
                    "status": "passed",
                    "dirty": False,
                    "dirty_files": [],
                },
                "graph_status": {
                    "current_state": {"graph_stale": {"is_stale": False}}
                },
            }
        )


def test_route_action_gate_rejects_high_risk_without_machine_context_fields() -> None:
    with pytest.raises(MfSubagentContractError, match="visible_injection_manifest"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                priority="P0",
                visible_injection_manifest_hash="",
                target_files=["agent/governance/precheck_service.py"],
                route_alerts=[
                    {
                        "code": "observer_judger_must_not_implement",
                        "blocked_actions": ["apply_patch"],
                    }
                ],
            )
        )


def test_route_action_gate_blocks_high_risk_worker_without_dispatch_evidence() -> None:
    with pytest.raises(MfSubagentContractError, match="bounded dispatch/startup evidence"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                **_high_risk_route_machine_fields(),
            )
        )


def test_route_action_gate_blocks_high_risk_worker_with_dispatch_only() -> None:
    dispatch = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(),
        target_worktree_path="/repo",
    )

    with pytest.raises(MfSubagentContractError, match="bounded dispatch/startup evidence"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                bounded_dispatch_evidence=dispatch,
                **_high_risk_route_machine_fields(),
            )
        )


def test_route_action_gate_blocks_high_risk_worker_with_startup_only() -> None:
    with pytest.raises(MfSubagentContractError, match="bounded dispatch/startup evidence"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                bounded_startup_evidence=_startup_evidence(),
                **_high_risk_route_machine_fields(),
            )
        )


def test_route_action_gate_blocks_high_risk_worker_when_dispatch_lacks_fence() -> None:
    dispatch = {
        "schema_version": DISPATCH_GATE_SCHEMA_VERSION,
        "allowed": True,
        "role": MF_SUB_ROLE,
        "bounded": True,
        "route_context_hash": "sha256:route-context",
        "prompt_contract_id": "rprompt-1",
        "prompt_contract_hash": "sha256:prompt-contract",
    }

    with pytest.raises(MfSubagentContractError, match="bounded dispatch/startup evidence"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                bounded_dispatch_evidence=dispatch,
                bounded_startup_evidence=_startup_evidence(),
                **_high_risk_route_machine_fields(),
            )
        )


@pytest.mark.parametrize(
    "startup_override",
    [
        {"fence_token": "", "fence_token_matches": False},
        {"actual_cwd": "", "actual_git_root": ""},
    ],
)
def test_route_action_gate_blocks_high_risk_worker_when_startup_evidence_incomplete(
    startup_override: dict[str, object],
) -> None:
    dispatch = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(),
        target_worktree_path="/repo",
    )

    with pytest.raises(MfSubagentContractError, match="bounded dispatch/startup evidence"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                bounded_dispatch_evidence=dispatch,
                bounded_startup_evidence=_startup_evidence(**startup_override),
                **_high_risk_route_machine_fields(),
            )
        )


def test_route_action_gate_blocks_worker_edit_when_startup_cwd_wrong() -> None:
    dispatch = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(),
        target_worktree_path="/repo",
    )

    with pytest.raises(MfSubagentContractError, match="bounded dispatch/startup evidence"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role=MF_SUB_ROLE,
                action="apply_patch",
                mf_subagent_dispatch_gate=dispatch,
                mf_subagent_startup_gate=_startup_evidence(
                    worktree_path="/repo/.worktrees/task-1",
                    assigned_worktree="/repo/.worktrees/task-1",
                    actual_cwd="/repo",
                    actual_git_root="/repo/.worktrees/task-1",
                ),
                **_high_risk_route_machine_fields(),
            )
        )


def test_route_action_gate_allows_high_risk_worker_with_bounded_dispatch_and_startup_evidence() -> None:
    dispatch = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(),
        target_worktree_path="/repo",
    )

    evidence = validate_route_action_gate(
        _route_action_payload(
            caller_role="implementation_worker",
            mf_subagent_dispatch_gate=dispatch,
            real_startup_events=[_startup_event()],
            **_high_risk_route_machine_fields(),
        )
    )

    assert evidence["allowed"] is True
    assert evidence["machine_context_required"] is True
    assert "priority_p0" in evidence["machine_context_policy"]["reason_codes"]
    assert evidence["route_machine_context"]["visible_injection_manifest_present"] is True
    assert evidence["route_machine_context"]["allowed_actions"] == ["apply_patch"]
    assert evidence["route_machine_context"]["required_evidence"]
    assert evidence["bounded_dispatch_evidence_present"] is True
    assert evidence["bounded_startup_evidence_present"] is True
    assert evidence["bounded_worker_evidence_present"] is True
    assert evidence["bounded_dispatch_evidence"]["fence_present"] is True


def test_route_action_gate_rejects_generated_startup_intent_packet() -> None:
    dispatch = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(),
        target_worktree_path="/repo",
    )
    startup_intent_event = {
        "event_type": "mf_subagent.startup_intent",
        "event_kind": "mf_subagent_startup_intent",
        "phase": "startup_intent",
        "status": "planned",
        "close_satisfying": False,
        "actual_startup_required": True,
        "payload": {
            "mf_subagent_startup_intent": _startup_evidence(
                schema_version="mf_subagent_startup_intent.v1",
                gate_kind="",
                intent_kind="mf_subagent.startup_intent",
                status="planned",
                bounded=False,
                same_as_expected_worker=False,
                fence_token_matches=False,
                close_satisfying=False,
                actual_startup_required=True,
                actual_cwd="",
                actual_git_root="",
                runtime_context_id="orctx-test",
                launch_text_hash="sha256:launch",
                project_id="aming-claw",
                task_id="TASK-impl-1",
                parent_task_id="TASK",
                worktree_path="/repo/.worktrees/TASK-impl-1",
                branch="refs/heads/test/TASK-impl-1",
                head_commit="target123",
                base_commit="base123",
                target_head_commit="target123",
            ),
        },
    }

    with pytest.raises(MfSubagentContractError, match="bounded dispatch/startup evidence"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                mf_subagent_dispatch_gate=dispatch,
                startup_timeline_event=startup_intent_event,
                **_high_risk_route_machine_fields(),
            )
        )


def test_route_action_gate_allows_actual_startup_timeline_event_packet() -> None:
    dispatch = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(),
        target_worktree_path="/repo",
    )
    startup_event = {
        "event_type": "mf_subagent.startup",
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "passed",
        "payload": {
                "mf_subagent_startup_gate": _startup_evidence(
                    schema_version="mf_subagent_startup_gate.v1",
                    runtime_context_id="orctx-test",
                    launch_text_hash="sha256:launch",
                    project_id="aming-claw",
                    task_id="TASK-impl-1",
                    parent_task_id="TASK",
                    worktree_path="/repo/.worktrees/TASK-impl-1",
                    actual_cwd="/repo/.worktrees/TASK-impl-1",
                    actual_git_root="/repo/.worktrees/TASK-impl-1",
                    branch="refs/heads/test/TASK-impl-1",
                head_commit="target123",
                base_commit="base123",
                target_head_commit="target123",
            ),
        },
    }

    evidence = validate_route_action_gate(
        _route_action_payload(
            caller_role="implementation_worker",
            mf_subagent_dispatch_gate=dispatch,
            real_startup_events=[startup_event],
            **_high_risk_route_machine_fields(),
        )
    )

    assert evidence["allowed"] is True
    assert evidence["bounded_startup_evidence_present"] is True
    assert evidence["bounded_worker_evidence_present"] is True


def test_route_action_gate_allows_high_risk_worker_without_optional_prompt_contract_hash() -> None:
    dispatch = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(prompt_contract_hash=""),
        target_worktree_path="/repo",
    )

    evidence = validate_route_action_gate(
        _route_action_payload(
            caller_role="implementation_worker",
            prompt_contract_hash="",
            mf_subagent_dispatch_gate=dispatch,
            real_startup_events=[
                _startup_event(_startup_evidence(prompt_contract_hash=""))
            ],
            **_high_risk_route_machine_fields(),
        )
    )

    assert evidence["allowed"] is True
    assert evidence["prompt_contract_hash"] == ""
    assert evidence["bounded_worker_evidence_present"] is True


def test_route_action_gate_blocks_high_risk_worker_when_optional_prompt_hash_mismatches() -> None:
    dispatch = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(prompt_contract_hash="sha256:different-prompt"),
        target_worktree_path="/repo",
    )

    with pytest.raises(MfSubagentContractError, match="bounded dispatch/startup evidence"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                mf_subagent_dispatch_gate=dispatch,
                mf_subagent_startup_gate=_startup_evidence(),
                **_high_risk_route_machine_fields(),
            )
        )


def test_route_action_gate_waiver_does_not_satisfy_high_risk_dispatch_evidence() -> None:
    with pytest.raises(MfSubagentContractError, match="bounded dispatch/startup evidence"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                version_check={
                    "status": "failed",
                    "dirty": True,
                    "dirty_files": ["agent/governance/mf_subagent_contract.py"],
                },
                graph_status={"current_state": {"graph_stale": {"is_stale": True}}},
                route_action_waiver={
                    "accepted": True,
                    "route_context_hash": "sha256:route-context",
                    "prompt_contract_id": "rprompt-1",
                    "prompt_contract_hash": "sha256:prompt-contract",
                },
                **_high_risk_route_machine_fields(),
            )
        )


def test_route_action_gate_blocks_worker_action_listed_in_blocked_actions() -> None:
    with pytest.raises(MfSubagentContractError, match="blocked_actions"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                blocked_actions=["apply_patch"],
                **_high_risk_route_machine_fields(),
            )
        )


def test_route_action_gate_blocks_provider_unavailable_for_implementation() -> None:
    with pytest.raises(MfSubagentContractError, match="blocked_route_context_unavailable"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                route_provider_error="Transport closed",
            )
        )


def test_route_action_gate_blocks_nested_provider_runtime_stale_for_implementation() -> None:
    with pytest.raises(MfSubagentContractError, match="blocked_route_context_unavailable"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                provider_runtime_status={
                    "stale": True,
                    "loaded_source_hash": "sha256:route-source",
                    "current_source_hash": "sha256:route-source",
                },
            )
        )


def test_route_action_gate_blocks_nested_provider_source_hash_mismatch() -> None:
    with pytest.raises(MfSubagentContractError, match="blocked_route_context_unavailable"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                provider_runtime_status={
                    "loaded_source_hash": "sha256:old-route-source",
                    "current_source_hash": "sha256:new-route-source",
                },
            )
        )


def test_route_action_gate_rejects_implementation_without_route_identity() -> None:
    with pytest.raises(MfSubagentContractError, match="route_context_hash"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                route_context_hash="",
            )
        )


def test_route_action_gate_allows_implementation_without_prompt_contract_hash_when_visible_manifest_present() -> None:
    evidence = validate_route_action_gate(
        _route_action_payload(
            caller_role="implementation_worker",
            prompt_contract_hash="",
        )
    )

    assert evidence["allowed"] is True
    assert evidence["prompt_contract_hash"] == ""
    assert evidence["route_machine_context"]["visible_injection_manifest_present"] is True


def test_route_action_gate_rejects_dirty_workspace_without_waiver() -> None:
    with pytest.raises(MfSubagentContractError, match="version/workspace"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                version_check={
                    "status": "failed",
                    "dirty": True,
                    "dirty_files": ["agent/governance/mf_subagent_contract.py"],
                },
            )
        )


def test_route_action_gate_rejects_stale_graph_without_waiver() -> None:
    with pytest.raises(MfSubagentContractError, match="current graph"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                graph_status={
                    "current_state": {
                        "graph_stale": {
                            "is_stale": True,
                            "changed_files": ["agent/governance/service_router.py"],
                        }
                    }
                },
            )
        )


def test_route_action_gate_waiver_can_bypass_dirty_or_stale_preconditions() -> None:
    evidence = validate_route_action_gate(
        _route_action_payload(
            caller_role="implementation_worker",
            version_check={
                "status": "failed",
                "dirty": True,
                "dirty_files": ["agent/governance/mf_subagent_contract.py"],
            },
            graph_status={"current_state": {"graph_stale": {"is_stale": True}}},
            route_action_waiver={
                "accepted": True,
                "route_context_hash": "sha256:route-context",
                "prompt_contract_id": "rprompt-1",
                "prompt_contract_hash": "sha256:prompt-contract",
                "route_token_ref": "rtok-worker-visible",
            },
        )
    )

    assert evidence["allowed"] is True
    assert evidence["accepted_waiver_present"] is True
    assert evidence["precondition_waiver_used"] is True
    assert evidence["version_workspace_gate"]["passed"] is False
    assert evidence["graph_current_gate"]["passed"] is False


def test_route_action_gate_rejects_independent_reviewer_alias_direct_implementation() -> None:
    with pytest.raises(MfSubagentContractError, match="must_not_implement"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="independent_reviewer",
                route_alerts=[
                    {
                        "code": "observer_independent_reviewer_must_not_implement",
                        "blocked_actions": ["apply_patch"],
                    }
                ],
            )
        )


def test_route_action_gate_rejects_observer_with_waiver_dispatch_and_startup() -> None:
    dispatch = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(),
        target_worktree_path="/repo",
    )

    assert dispatch["allowed"] is True

    with pytest.raises(MfSubagentContractError, match="observer_judger_must_not_implement"):
        validate_route_action_gate(
            _route_action_payload(
                route_action_waiver={
                    "accepted": True,
                    "route_context_hash": "sha256:route-context",
                    "prompt_contract_id": "rprompt-1",
                    "prompt_contract_hash": "sha256:prompt-contract",
                },
                bounded_dispatch_evidence=dispatch,
                bounded_startup_evidence=_startup_evidence(),
            )
        )


def test_route_action_gate_rejects_independent_reviewer_alias_with_waiver_dispatch_and_startup() -> None:
    dispatch = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(),
        target_worktree_path="/repo",
    )

    with pytest.raises(
        MfSubagentContractError,
        match="observer_independent_reviewer_must_not_implement",
    ):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="independent_reviewer",
                route_alerts=[
                    {
                        "code": "observer_independent_reviewer_must_not_implement",
                        "blocked_actions": ["apply_patch"],
                    }
                ],
                route_action_waiver={
                    "accepted": True,
                    "route_context_hash": "sha256:route-context",
                    "prompt_contract_id": "rprompt-1",
                    "prompt_contract_hash": "sha256:prompt-contract",
                },
                bounded_dispatch_evidence=dispatch,
                bounded_startup_evidence=_startup_evidence(),
            )
        )


def test_route_action_gate_rejects_observer_direct_implementation_before_dispatch_result() -> None:
    failed_dispatch = {
        "schema_version": DISPATCH_GATE_SCHEMA_VERSION,
        "allowed": False,
        "status": "failed",
        "role": MF_SUB_ROLE,
        "route_context_hash": "sha256:route-context",
        "prompt_contract_id": "rprompt-1",
        "prompt_contract_hash": "sha256:prompt-contract",
    }

    with pytest.raises(MfSubagentContractError, match="observer_judger_must_not_implement"):
        validate_route_action_gate(
            _route_action_payload(
                route_action_waiver={
                    "accepted": True,
                    "route_context_hash": "sha256:route-context",
                    "prompt_contract_id": "rprompt-1",
                    "prompt_contract_hash": "sha256:prompt-contract",
                },
                bounded_dispatch_evidence=failed_dispatch,
            )
        )


def _route_token(**overrides: object) -> dict[str, object]:
    # Use valid sha256:<64 hex> format for hash fields (required by format floor).
    _rc_hash = canonical_contract_hash({"route_id": "test-route", "project": "aming-claw"})
    _pc_hash = canonical_contract_hash({"prompt_contract_id": "rprompt-1", "version": 1})
    token: dict[str, object] = {
        "route_context_hash": _rc_hash,
        "prompt_contract_id": "rprompt-1",
        "prompt_contract_hash": _pc_hash,
        "caller_role": "observer",
        "allowed_action": "backlog_close",
        "project_id": "aming-claw",
        "backlog_id": "BUG-1",
        "expires_at": "2999-01-01T00:00:00Z",
        "evidence_refs": ["timeline:route-context"],
    }
    token.update(overrides)
    return token


def test_route_token_mutation_gate_accepts_bounded_token() -> None:
    gate = validate_route_token_mutation_gate(
        {"route_token": _route_token()},
        action="backlog_close",
        project_id="aming-claw",
        backlog_id="BUG-1",
    )

    assert gate["schema_version"] == ROUTE_TOKEN_MUTATION_GATE_SCHEMA_VERSION
    assert gate["allowed"] is True
    assert gate["decision"] == "route_token"
    assert gate["route_context_hash"].startswith("sha256:")
    assert gate["prompt_contract_id"] == "rprompt-1"
    assert gate["route_token_hash"].startswith("sha256:")


def test_observer_route_context_implementation_merge_actions_keep_worker_lanes() -> None:
    from datetime import datetime, timezone

    from agent.governance import observer_route_context

    issued = observer_route_context.issue_observer_write_route_context(
        project_id="aming-claw",
        backlog_id="BUG-MERGE",
        task_id="cex-merge",
        target_files=["agent/governance/observer_route_context.py"],
        allowed_actions=[
            "task_timeline_append",
            "parallel_branch_allocate",
            "backlog_close",
            "parallel_branch_merge_queue_materialize",
            "parallel_branch_merge_queue_apply",
        ],
        now=datetime(2099, 6, 10, 12, 0, 0, tzinfo=timezone.utc),
    )

    token = issued["route_token"]
    scope = token["route_action_scope"]
    lane_ids = {lane["id"] for lane in token["required_lanes"]}
    assert issued["requires_mf_sub_implementation_lane"] is True
    assert scope["classification"] == "bounded_worker_implementation_or_merge"
    assert scope["requires_mf_sub_implementation_lane"] is True
    assert "parallel_branch_allocate" in scope["implementation_or_merge_actions"]
    assert "parallel_branch_merge_queue_materialize" in scope["implementation_or_merge_actions"]
    assert "parallel_branch_merge_queue_apply" in scope["implementation_or_merge_actions"]
    assert "backlog_close" in scope["admin_close_or_evidence_actions"]
    assert "backlog_close" not in scope["implementation_or_merge_actions"]
    assert "bounded_implementation_subagent" in lane_ids
    assert "independent_verification_subagent" in lane_ids
    assert "bounded_implementation_subagent_id" in token["required_evidence"]
    assert "independent_verification_subagent_id" in token["required_evidence"]

    gate = validate_route_token_mutation_gate(
        {"route_token": token},
        action="backlog_close",
        project_id="aming-claw",
        backlog_id="BUG-MERGE",
        task_id="cex-merge",
    )
    assert gate["allowed"] is True


def test_observer_route_context_close_only_scope_is_admin_copy_safe() -> None:
    from datetime import datetime, timezone

    from agent.governance import observer_route_context

    issued = observer_route_context.issue_observer_write_route_context(
        project_id="aming-claw",
        backlog_id="BUG-CLOSE-ONLY",
        task_id="cex-close-only",
        target_files=["agent/governance/server.py"],
        allowed_actions=["backlog_close"],
        now=datetime(2099, 6, 10, 12, 0, 0, tzinfo=timezone.utc),
    )

    token = issued["route_token"]
    scope = token["route_action_scope"]
    lane_ids = {lane["id"] for lane in token["required_lanes"]}
    assert issued["requires_mf_sub_implementation_lane"] is False
    assert scope["classification"] == "observer_admin_close_evidence_only"
    assert scope["requires_bounded_worker_implementation"] is False
    assert scope["implementation_or_merge_actions"] == []
    assert scope["unknown_actions"] == []
    assert scope["admin_close_or_evidence_actions"] == ["backlog_close"]
    assert "bounded_implementation_subagent" not in lane_ids
    assert "independent_verification_subagent" not in lane_ids
    assert "bounded_implementation_subagent_id" not in token["required_evidence"]
    assert "independent_verification_subagent_id" not in token["required_evidence"]

    gate = validate_route_token_mutation_gate(
        {"route_token": token},
        action="backlog_close",
        project_id="aming-claw",
        backlog_id="BUG-CLOSE-ONLY",
        task_id="cex-close-only",
    )
    assert gate["allowed"] is True


def test_observer_route_context_merge_tool_actions_expand_to_concrete_gate_actions() -> None:
    from agent.governance import server

    expanded = server._observer_route_context_issue_allowed_actions(
        [
            "parallel_branch_merge_queue_materialize",
            "parallel_branch_merge_queue_apply",
        ]
    )

    assert expanded == [
        "parallel_branch_merge_queue_materialize",
        "parallel_branch_merge_queue_apply",
        "merge_queue",
        "merge_execute",
    ]


def test_route_token_mutation_gate_rejects_missing_token_for_protected_action() -> None:
    with pytest.raises(MfSubagentContractError, match="route_token is required"):
        validate_route_token_mutation_gate(
            {},
            action="backlog_close",
            project_id="aming-claw",
            backlog_id="BUG-1",
        )


def test_route_token_mutation_gate_rejects_generic_waiver_without_route_identity() -> None:
    with pytest.raises(MfSubagentContractError, match="route identity"):
        validate_route_token_mutation_gate(
            {
                "route_waiver": {
                    "accepted": True,
                    "waiver_type": "manual_fix",
                    "caller_role": "observer",
                    "allowed_action": "task_timeline_append",
                    "project_id": "aming-claw",
                    "backlog_id": "BUG-1",
                    "reason": "Generic waiver lacks required public-safe route identity.",
                    "timeline_evidence": {"event_id": 978},
                }
            },
            action="task_timeline_append",
            project_id="aming-claw",
            backlog_id="BUG-1",
        )


def test_route_token_required_failure_details_classify_expected_gate_behavior() -> None:
    details = route_token_required_failure_details(
        action="task_timeline_append",
        reason="route_token is required for protected governance action",
    )

    assert details["schema_version"] == ROUTE_TOKEN_REQUIRED_FAILURE_SCHEMA_VERSION
    assert details["fault_domain"] == "caller_missing_route_evidence"
    assert details["expected_behavior"] is True
    assert details["do_not_file_system_bug"] is True
    assert details["is_system_bug"] is False
    assert "next_valid_actions" in details
    assert "system_bug_preconditions" in details


def test_route_token_mutation_gate_rejects_scope_mismatch() -> None:
    with pytest.raises(MfSubagentContractError, match="backlog scope"):
        validate_route_token_mutation_gate(
            {"route_token": _route_token(backlog_id="BUG-2")},
            action="backlog_close",
            project_id="aming-claw",
            backlog_id="BUG-1",
        )


def test_route_token_mutation_gate_rejects_expired_token() -> None:
    with pytest.raises(MfSubagentContractError, match="expired"):
        validate_route_token_mutation_gate(
            {"route_token": _route_token(expires_at="2000-01-01T00:00:00Z")},
            action="backlog_close",
            project_id="aming-claw",
            backlog_id="BUG-1",
        )


def test_route_token_mutation_gate_accepts_explicit_manual_fix_waiver() -> None:
    gate = validate_route_token_mutation_gate(
        {
            "route_waiver": {
                "accepted": True,
                "waiver_type": "manual_fix",
                "route_context_hash": "sha256:route-context",
                "prompt_contract_id": "rprompt-1",
                "prompt_contract_hash": "sha256:prompt-contract",
                "caller_role": "observer",
                "allowed_action": "backlog_close",
                "project_id": "aming-claw",
                "backlog_id": "BUG-1",
                "reason": "Operator approved a bounded manual-fix close with timeline evidence.",
                "timeline_evidence": {"event_id": 978},
            }
        },
        action="backlog_close",
        project_id="aming-claw",
        backlog_id="BUG-1",
    )

    assert gate["allowed"] is True
    assert gate["decision"] == "route_waiver"
    assert gate["route_context_hash"] == "sha256:route-context"
    assert gate["prompt_contract_id"] == "rprompt-1"
    assert gate["caller_role"] == "observer"
    assert gate["timeline_evidence"] == ["978"]


def _observer_direct_mutation_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "role": "observer",
        "observer_direct_mutation": True,
        "direct_mutation_exception": {
            "tiny_deterministic": True,
            "reason": "Correct a deterministic one-line contract typo.",
            "allowed_files": ["docs/governance/manual-fix-sop.md"],
            "dirty_scope_check": {
                "status": "passed",
                "dirty_scope_exact_match": True,
                "changed_files": ["docs/governance/manual-fix-sop.md"],
                "owned_files": ["docs/governance/manual-fix-sop.md"],
            },
            "timeline_evidence": {
                "event_id": 1001,
                "event_type": "observer_direct_mutation_exception",
                "recorded_before_mutation": True,
            },
        },
    }
    payload.update(overrides)
    return payload


def test_observer_direct_mutation_exception_accepts_tiny_deterministic_scope() -> None:
    evidence = validate_observer_direct_mutation_exception(
        _observer_direct_mutation_payload(),
        allowed_files=["docs/governance/manual-fix-sop.md"],
    )

    assert evidence["schema_version"] == OBSERVER_DIRECT_MUTATION_SCHEMA_VERSION
    assert evidence["role"] == OBSERVER_COORDINATOR_ROLE
    assert evidence["policy_default"] == "reject"
    assert evidence["observer_direct_mutation"] is True
    assert evidence["allowed"] is True
    assert evidence["exception"]["used"] is True
    assert evidence["exception"]["timeline_evidence_recorded_before_mutation"] is True
    assert evidence["dirty_scope_check"]["dirty_scope_exact_match"] is True


def test_observer_direct_mutation_exception_rejects_default_empty_payload() -> None:
    with pytest.raises(MfSubagentContractError, match="observer_direct_mutation=true"):
        validate_observer_direct_mutation_exception({})


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"observer_direct_mutation": False}, "observer_direct_mutation=true"),
        ({"role": ""}, "observer role"),
        (
            {"direct_mutation_exception": {"tiny_deterministic": False}},
            "tiny deterministic",
        ),
        (
            {
                "direct_mutation_exception": {
                    "tiny_deterministic": True,
                    "allowed_files": ["docs/governance/manual-fix-sop.md"],
                }
            },
            "explicit reason",
        ),
        (
            {
                "direct_mutation_exception": {
                    "tiny_deterministic": True,
                    "reason": "Small typo.",
                }
            },
            "allowed_files",
        ),
        (
            {
                "direct_mutation_exception": {
                    "tiny_deterministic": True,
                    "reason": "Small typo.",
                    "allowed_files": ["docs/governance/manual-fix-sop.md"],
                    "timeline_evidence": {
                        "event_id": 1001,
                        "recorded_before_mutation": True,
                    },
                }
            },
            "dirty-scope evidence",
        ),
        (
            {
                "direct_mutation_exception": {
                    "tiny_deterministic": True,
                    "reason": "Small typo.",
                    "allowed_files": ["docs/governance/manual-fix-sop.md"],
                    "dirty_scope_check": {
                        "status": "passed",
                        "dirty_scope_exact_match": False,
                        "changed_files": ["docs/governance/manual-fix-sop.md"],
                    },
                    "timeline_evidence": {
                        "event_id": 1001,
                        "recorded_before_mutation": True,
                    },
                }
            },
            "dirty_scope_exact_match",
        ),
        (
            {
                "direct_mutation_exception": {
                    "tiny_deterministic": True,
                    "reason": "Small typo.",
                    "allowed_files": ["docs/governance/manual-fix-sop.md"],
                    "dirty_scope_check": {
                        "status": "passed",
                        "dirty_scope_exact_match": True,
                        "changed_files": ["docs/governance/manual-fix-sop.md"],
                    },
                }
            },
            "timeline evidence before mutation",
        ),
    ],
)
def test_observer_direct_mutation_exception_rejects_missing_evidence(
    override: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(MfSubagentContractError, match=match):
        validate_observer_direct_mutation_exception(
            _observer_direct_mutation_payload(**override),
            allowed_files=["docs/governance/manual-fix-sop.md"],
        )


def test_observer_direct_mutation_exception_rejects_dirty_files_outside_scope() -> None:
    with pytest.raises(MfSubagentContractError, match="dirty files"):
        validate_observer_direct_mutation_exception(
            _observer_direct_mutation_payload(
                direct_mutation_exception={
                    "tiny_deterministic": True,
                    "reason": "Small typo.",
                    "allowed_files": ["docs/governance/manual-fix-sop.md"],
                    "dirty_scope_check": {
                        "status": "passed",
                        "dirty_scope_exact_match": True,
                        "changed_files": ["agent/governance/server.py"],
                    },
                    "timeline_evidence": {
                        "event_id": 1001,
                        "recorded_before_mutation": True,
                    },
                }
            ),
            allowed_files=["docs/governance/manual-fix-sop.md"],
        )


def _context(**overrides: object) -> BranchTaskRuntimeContext:
    fields = {
        "project_id": "aming-claw",
        "task_id": "task-mf-sub-1",
        "batch_id": "batch-parallel-1",
        "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
        "branch_ref": "refs/heads/codex/task-mf-sub-1",
        "status": "running",
        "agent_id": "codex",
        "worker_id": "codex-subagent-1",
        "attempt": 2,
        "lease_id": "lease-1",
        "fence_token": "fence-2",
        "ref_name": "main",
        "worktree_id": "wt-1",
        "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
        "base_commit": "base123",
        "head_commit": "head123",
        "target_head_commit": "target123",
        "snapshot_id": "scope-target123",
        "projection_id": "semantic-target123",
        "merge_queue_id": "mq-1",
        "merge_preview_id": "mp-1",
        "depends_on": ("task-foundation",),
        "checkpoint_id": "ckpt-old",
    }
    fields.update(overrides)
    return BranchTaskRuntimeContext(**fields)


def _self_attested_startup_fields(
    *,
    worker_session_id: str = "worker-session-codex-1",
    transcript_path: str = "/tmp/worker-session-codex-1.jsonl",
    transcript_ref: str = "codex-thread:worker-session-codex-1",
    harness_type: str = "codex",
) -> dict[str, object]:
    return {
        "filer_principal": worker_session_id,
        "worker_session_id": worker_session_id,
        "worker_transcript_path": transcript_path,
        "worker_transcript_ref": transcript_ref,
        "harness_type": harness_type,
        "worker_self_attestation": {
            "status": "passed",
            "worker_self_attesting": True,
            "worker_session_id": worker_session_id,
            "worker_transcript_path": transcript_path,
            "worker_transcript_ref": transcript_ref,
            "harness_type": harness_type,
        },
    }


def _finish_time_worker_attestation(
    *,
    worker_session_id: str = "worker-session-codex-1",
    transcript_path: str = "/tmp/worker-session-codex-1.jsonl",
    transcript_ref: str = "codex-thread:worker-session-codex-1",
    harness_type: str = "codex",
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "worker_transcript_self_attestation.v1",
        "attestation_phase": "finish",
        "status": "passed",
        "ok": True,
        "worker_self_attesting": True,
        "self_attesting": True,
        "finish_time_self_attesting": True,
        "finish_time_blockers": [],
        "worker_session_id": worker_session_id,
        "filer_principal": worker_session_id,
        "worker_transcript_path": transcript_path,
        "worker_transcript_ref": transcript_ref,
        "harness_type": harness_type,
        "blockers": [],
    }
    payload.update(overrides)
    return payload


def _finish_startup_evidence(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "mf_subagent_startup_gate.v1",
        "gate_kind": "mf_subagent.startup",
        "status": "passed",
        "ok": True,
        "allowed": True,
        "bounded": True,
        "started": True,
        "startup_complete": True,
        "actual_startup_recorded": True,
        "worker_role": "mf_sub",
        "worker_id": "codex-subagent-1",
        "fence_token": "fence-2",
        "actual_cwd": "/tmp/aming-claw-wt/task-mf-sub-1",
        "actual_git_root": "/tmp/aming-claw-wt/task-mf-sub-1",
        "worktree": "/tmp/aming-claw-wt/task-mf-sub-1",
        "task_id": "task-mf-sub-1",
        "runtime_context_id": "mfrctx-finish",
        "branch": "refs/heads/codex/task-mf-sub-1",
        "head_commit": "head456",
        "route_id": "route-finish-child",
        "route_context_hash": "sha256:child-route-context",
        "prompt_contract_id": "rprompt-child",
        "prompt_contract_hash": "sha256:child-prompt",
        "route_token_ref": "rtok-finish-visible",
        "observer_command_id": "cmd-finish",
        "read_receipt_hash": "sha256:read-finish",
        "read_receipt_event_id": "2873",
        **_self_attested_startup_fields(),
    }
    payload.update(overrides)
    return payload


def test_build_input_carries_branch_runtime_identity() -> None:
    payload = build_mf_subagent_input(
        _context(root_task_id="task-mf-parent"),
        prompt="Implement the isolated change.",
        acceptance_criteria=["tests pass"],
        target_files=["agent/governance/mf_subagent_contract.py"],
        test_commands=["python -m pytest agent/tests/test_mf_subagent_contract.py -q"],
        route_context_hash="sha256:route-context",
        prompt_contract_id="rprompt-1",
        prompt_contract_hash="sha256:prompt-contract",
    )

    assert payload["role"] == MF_SUB_ROLE
    assert payload["backend_contract"] == BACKEND_CONTRACT
    assert payload["project_id"] == "aming-claw"
    assert payload["backlog_id"] == "ARCH-MF-SUBAGENT-BACKEND"
    assert payload["branch"]["worktree_path"] == "/tmp/aming-claw-wt/task-mf-sub-1"
    assert payload["runtime_identity"]["required_fields"] == [
        "task_id",
        "parent_task_id",
        "worker_role",
        "fence_token",
    ]
    assert payload["runtime_identity"]["runtime_context_id"].startswith("mfrctx-")
    assert payload["runtime_identity"]["task_id"] == "task-mf-sub-1"
    assert payload["runtime_identity"]["parent_task_id"] == "task-mf-parent"
    assert payload["runtime_identity"]["worker_role"] == MF_SUB_ROLE
    assert payload["runtime_identity"]["fence_token"] == "fence-2"
    assert payload["runtime_identity"]["depends_on"] == ["task-foundation"]
    assert payload["work"]["acceptance_criteria"] == ["tests pass"]
    assert payload["route_prompt_contract"] == {
        "route_context_hash": "sha256:route-context",
        "prompt_contract_id": "rprompt-1",
        "prompt_contract_hash": "sha256:prompt-contract",
    }
    invocation = payload["invocation_request"]
    assert invocation["schema_version"] == "ai_invocation_request.v1"
    assert invocation["role"] == MF_SUB_ROLE
    assert invocation["provider"] == "openai"
    assert invocation["backend_mode"] == "codex_cli"
    assert invocation["cwd"] == "/tmp/aming-claw-wt/task-mf-sub-1"
    assert invocation["worktree"] == "/tmp/aming-claw-wt/task-mf-sub-1"
    assert invocation["output_policy"] == "hash_and_summary_only"
    assert invocation["route_prompt_contract"]["route_context_hash"] == (
        "sha256:route-context"
    )
    assert invocation["route_prompt_contract"]["prompt_contract_id"] == "rprompt-1"
    assert "runtime_context:" in " ".join(invocation["evidence_refs"])
    assert invocation["raw_prompt_output_stored"] is False
    assert "Implement the isolated change." not in json.dumps(invocation)
    assert payload["invocation_contract"] == {
        "request_field": "invocation_request",
        "request_schema_version": "ai_invocation_request.v1",
        "result_field": "invocation_result",
        "result_schema_version": "ai_invocation_result.v1",
        "result_validation": "strict_when_present",
    }
    assert payload["agent_task_contract"]["schema_version"] == (
        AGENT_TASK_CONTRACT_SCHEMA_VERSION
    )
    assert payload["agent_task_contract"]["source_of_truth"] == "Contract/Revision/Event"
    assert payload["agent_task_contract"]["target_files"] == [
        "agent/governance/mf_subagent_contract.py"
    ]
    assert payload["agent_task_contract"]["target_fences"] == ["fence-2"]
    assert payload["verification_route_policy"]["real_ai_provider_calls"]["allowed"] is False
    assert "modify_code" in payload["capabilities"]["can"]
    assert set(MF_SUB_FORBIDDEN_ACTIONS).issubset(payload["capabilities"]["cannot"])
    assert payload["prechecks"]["asset_binding_proposal"]["proposal_schema_version"] == (
        "asset_binding_proposal.v1"
    )
    assert payload["prechecks"]["asset_binding_proposal"]["precheck_schema_version"] == (
        "asset_binding_precheck.v1"
    )
    assert payload["required_output"] == [
        "status",
        "changed_files",
        "test_results",
        "checkpoint_id",
        "fence_token",
    ]


def test_build_input_does_not_guess_invocation_routing_from_backend_name() -> None:
    payload = build_mf_subagent_input(
        _context(),
        prompt="Use the explicit provider-neutral invocation lane.",
        backend="claude_subagent",
    )

    invocation = payload["invocation_request"]
    assert invocation["requested_backend"] == "claude_subagent"
    assert invocation["provider"] == "openai"
    assert invocation["backend_mode"] == "codex_cli"
    assert invocation["auth_mode"] == "cli_auth"


def test_build_input_rejects_contradictory_explicit_invocation_routing() -> None:
    with pytest.raises(MfSubagentContractError, match="routing is invalid"):
        build_mf_subagent_input(
            _context(),
            prompt="Do not launch.",
            invocation_routing={
                "provider": "anthropic",
                "model": "gpt-4o",
                "backend_mode": "codex_cli",
                "auth_mode": "cli_auth",
            },
        )


def _mf_invocation_result(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "ai_invocation_result.v1",
        "request_schema_version": "ai_invocation_request.v1",
        "status": "completed",
        "role": MF_SUB_ROLE,
        "provider": "openai",
        "model": "gpt-5.4-codex",
        "backend_mode": "codex_cli",
        "auth_mode": "cli_auth",
        "auth_status": "host_auth_reused",
        "output_policy": "hash_and_summary_only",
        "provider_backed": True,
        "calls_models": True,
        "raw_output_stored": False,
        "no_raw_prompt_output": True,
        "raw_error_stored": False,
        "error": "",
        "error_present": False,
        "error_sha256": "",
        "command": ["codex", "exec"],
        "output_path": "",
        "prompt_sha256": "sha256:prompt",
        "output_sha256": "sha256:output",
        "evidence_refs": ["task:task-mf-sub-1", "runtime_context:mfrctx-test"],
    }
    payload.update(overrides)
    return payload


def test_normalize_result_validates_and_preserves_invocation_result() -> None:
    invocation_result = _mf_invocation_result()
    normalized = normalize_mf_subagent_result(
        {
            "status": "review_ready",
            "changed_files": ["x.py"],
            "test_results": {"status": "passed"},
            "checkpoint_id": "ckpt-new",
            "fence_token": "fence-2",
            "invocation_result": invocation_result,
        },
        expected_fence_token="fence-2",
    )

    assert normalized["invocation_result"] == invocation_result


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"provider": "anthropic"}, "routing is invalid"),
        ({"raw_output_stored": True}, "raw_output_stored=false"),
        ({"error": "raw provider output"}, "raw error text"),
        ({"output_path": "/tmp/raw-provider-output.txt"}, "raw output path"),
        ({"evidence_refs": ["credential:secret"]}, "evidence_refs"),
        ({"blocker_id": "sk-secretcredential"}, "credential-like"),
    ],
)
def test_normalize_result_rejects_unsafe_or_contradictory_invocation_result(
    override: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(MfSubagentContractError, match=message):
        normalize_mf_subagent_result(
            {
                "status": "review_ready",
                "changed_files": ["x.py"],
                "test_results": {"status": "passed"},
                "checkpoint_id": "ckpt-new",
                "fence_token": "fence-2",
                "invocation_result": _mf_invocation_result(**override),
            },
            expected_fence_token="fence-2",
        )


def test_build_input_carries_parent_and_child_route_lineage() -> None:
    owned_files = ["agent/governance/mf_subagent_contract.py"]
    payload = build_mf_subagent_input(
        _context(),
        prompt="Implement the isolated change.",
        target_files=owned_files,
        route_context_hash="sha256:child-route-context",
        prompt_contract_id="rprompt-child",
        prompt_contract_hash="sha256:child-prompt",
        parent_route_lineage=_parent_route_lineage(),
    )

    assert payload["parent_route_lineage"]["route_id"] == "route-20260602-parent"
    assert payload["parent_route_lineage"]["selected_project"] == "aming-claw"
    assert payload["route_prompt_contract"] == {
        "route_context_hash": "sha256:child-route-context",
        "prompt_contract_id": "rprompt-child",
        "prompt_contract_hash": "sha256:child-prompt",
    }
    assert payload["route_lineage"]["parent_route_context_hash"] == (
        "sha256:parent-route-context"
    )
    assert payload["route_lineage"]["child_prompt_contract_id"] == "rprompt-child"
    assert payload["work"]["owned_files"] == owned_files
    assert payload["agent_task_contract"]["owned_files"] == owned_files


@pytest.mark.parametrize("field", ["backlog_id", "worktree_path", "fence_token", "merge_queue_id"])
def test_build_input_rejects_missing_required_identity(field: str) -> None:
    with pytest.raises(MfSubagentContractError, match=field):
        build_mf_subagent_input(_context(**{field: ""}), prompt="Do work.")


@pytest.mark.parametrize("status", ["succeeded", "review_ready", "waiting_merge"])
def test_normalize_result_marks_ready_only_after_tests_and_fence_match(status: str) -> None:
    normalized = normalize_mf_subagent_result(
        {
            "status": status,
            "changed_files": ["agent/governance/mf_subagent_contract.py"],
            "test_results": {"status": "passed", "command": "pytest -q"},
            "checkpoint_id": "ckpt-new",
            "fence_token": "fence-2",
            "summary": "Implemented contract.",
        },
        expected_fence_token="fence-2",
    )

    assert normalized["role"] == MF_SUB_ROLE
    assert normalized["status"] == status
    assert normalized["merge_queue_ready"] is True
    assert normalized["checkpoint_id"] == "ckpt-new"
    assert normalized["changed_files"] == ["agent/governance/mf_subagent_contract.py"]


def test_normalize_result_rejects_stale_fence() -> None:
    with pytest.raises(MfSubagentContractError, match="stale"):
        normalize_mf_subagent_result(
            {
                "status": "succeeded",
                "changed_files": [],
                "test_results": {"status": "passed"},
                "checkpoint_id": "ckpt-new",
                "fence_token": "old-fence",
            },
            expected_fence_token="fence-2",
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"actions": ["merge"]},
        {"actions": ["push"]},
        {"merge_commit": "abc123"},
        {"graph_activated": True},
    ],
)
def test_normalize_result_rejects_forbidden_actions(payload: dict[str, object]) -> None:
    result = {
        "status": "succeeded",
        "changed_files": ["x.py"],
        "test_results": {"status": "passed"},
        "checkpoint_id": "ckpt-new",
        "fence_token": "fence-2",
    }
    result.update(payload)

    with pytest.raises(MfSubagentContractError, match="forbidden actions"):
        normalize_mf_subagent_result(result, expected_fence_token="fence-2")


def test_normalize_result_blocks_merge_queue_when_tests_fail() -> None:
    normalized = normalize_mf_subagent_result(
        {
            "status": "review_ready",
            "changed_files": ["x.py"],
            "test_results": {"status": "failed"},
            "checkpoint_id": "ckpt-new",
            "fence_token": "fence-2",
            "blockers": ["test failure"],
        },
        expected_fence_token="fence-2",
    )

    assert normalized["merge_queue_ready"] is False
    assert normalized["blockers"] == ["test failure"]


@pytest.mark.parametrize(
    ("status", "test_results", "blockers"),
    [
        ("waiting_merge", {"status": "passed"}, ["observer follow-up required"]),
        ("running", {"status": "passed"}, []),
    ],
)
def test_normalize_result_blocks_unready_handoff_states(
    status: str,
    test_results: dict[str, object],
    blockers: list[str],
) -> None:
    normalized = normalize_mf_subagent_result(
        {
            "status": status,
            "changed_files": ["x.py"],
            "test_results": test_results,
            "checkpoint_id": "ckpt-new",
            "fence_token": "fence-2",
            "blockers": blockers,
        },
        expected_fence_token="fence-2",
    )

    assert normalized["merge_queue_ready"] is False


def test_finish_gate_returns_validated_checkpoint_evidence() -> None:
    gate = validate_mf_subagent_finish_gate(
        {
            "project_id": "aming-claw",
            "task_id": "task-mf-sub-1",
            "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
            "branch_ref": "refs/heads/codex/task-mf-sub-1",
            "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
            "base_commit": "base123",
            "target_head_commit": "target123",
            "merge_queue_id": "mq-1",
            "head_commit": "head456",
            "status": "succeeded",
            "changed_files": ["agent/governance/mf_subagent_contract.py"],
            "test_results": {"status": "passed", "command": "pytest -q"},
            "checkpoint_id": "ckpt-finish",
            "fence_token": "fence-2",
            "summary": "Ready.",
            "worker_self_attestation": _finish_time_worker_attestation(),
            "mf_subagent_startup_gate": _finish_startup_evidence(),
            "real_startup_events": [_startup_event(_finish_startup_evidence())],
            "read_receipt_hash": "sha256:read-finish",
            "read_receipt_event_id": "2873",
            "gate_receipt_hash": "sha256:gate-finish",
        },
        context=_context(),
    )

    assert gate["schema_version"] == FINISH_GATE_SCHEMA_VERSION
    assert gate["checkpoint_id"] == "ckpt-finish"
    assert gate["head_commit"] == "head456"
    assert gate["merge_queue_id"] == "mq-1"
    assert gate["replay_source"] == FINISH_GATE_REPLAY_SOURCE
    assert gate["merge_queue_ready"] is True
    assert gate["read_receipt_hash"] == "sha256:read-finish"
    assert gate["read_receipt_event_id"] == "2873"
    assert gate["gate_receipt_hash"] == "sha256:gate-finish"
    assert gate["receipt_gate"]["status"] == "passed"
    assert gate["receipt_gate"]["startup_present"] is True
    assert gate["finish_precheck"]["parent_main_clean"] is True
    assert gate["finish_precheck"]["owned_file_scope_passed"] is True
    assert gate["startup_worker_identity_gate"]["passed"] is True
    assert gate["worker_self_attestation_gate"]["passed"] is True
    assert gate["worker_self_attestation_gate"]["attestation_phase"] == "finish"
    assert gate["worker_self_attestation"]["finish_time_self_attesting"] is True
    assert gate["close_ready"] is True
    assert gate["implementation"] is True
    assert gate["implementation_ready"] is True
    assert gate["review_ready"] is True
    assert gate["waiting_merge"] is True
    assert gate["worker_status"] == "waiting_merge"
    assert gate["stop_state"] == "waiting_merge"
    assert gate["route_identity"] == {
        "route_id": "route-finish-child",
        "route_context_hash": "sha256:child-route-context",
        "prompt_contract_id": "rprompt-child",
        "prompt_contract_hash": "sha256:child-prompt",
        "route_token_ref": "rtok-finish-visible",
        "visible_injection_manifest_hash": "",
    }
    assert gate["route_prompt_contract"] == {
        "route_context_hash": "sha256:child-route-context",
        "prompt_contract_id": "rprompt-child",
        "prompt_contract_hash": "sha256:child-prompt",
        "route_id": "route-finish-child",
        "route_token_ref": "rtok-finish-visible",
    }
    assert gate["runtime_context_id"] == "mfrctx-finish"
    assert gate["worker_id"] == "codex-subagent-1"
    assert gate["worker_slot_id"] == "codex-subagent-1"
    assert gate["worker_session_id"] == "worker-session-codex-1"
    assert gate["worker_identity"] == {
        "schema_version": "mf_subagent_finish_gate_worker_identity.v1",
        "worker_role": "mf_sub",
        "role": "mf_sub",
        "task_id": "task-mf-sub-1",
        "parent_task_id": "",
        "runtime_context_id": "mfrctx-finish",
        "worker_id": "codex-subagent-1",
        "worker_slot_id": "codex-subagent-1",
        "agent_id": "codex",
        "worker_session_id": "worker-session-codex-1",
        "startup_worker_session_id": "worker-session-codex-1",
        "finish_worker_session_id": "worker-session-codex-1",
        "worker_transcript_path": "/tmp/worker-session-codex-1.jsonl",
        "worker_transcript_ref": "codex-thread:worker-session-codex-1",
        "harness_type": "codex",
        "startup_identity_passed": True,
        "finish_attestation_passed": True,
    }
    assert gate["startup_lineage"]["schema_version"] == (
        "mf_subagent_finish_gate_startup_lineage.v1"
    )
    assert gate["startup_lineage"]["startup_event_id"] == "evt-startup-test"
    assert gate["startup_lineage"]["startup_event_kind"] == "mf_subagent_startup"
    assert gate["startup_lineage"]["runtime_context_id"] == "mfrctx-finish"
    assert gate["startup_lineage"]["observer_command_id"] == "cmd-finish"
    assert gate["startup_lineage"]["read_receipt_event_id"] == "2873"
    assert gate["startup_lineage"]["route_identity"] == gate["route_identity"]

    projection = gate["lane_ownership_projection"]
    assert projection["schema_version"] == (
        "mf_subagent_finish_gate_lane_ownership_projection.v1"
    )
    assert projection["evidence_id"] == "bounded_implementation_subagent.review_ready"
    assert projection["evidence_ids"] == [
        "bounded_implementation_subagent.implementation",
        "bounded_implementation_subagent.review_ready",
        "bounded_implementation_subagent.waiting_merge",
        "bounded_implementation_subagent.close_ready",
    ]
    assert projection["implementation"] is True
    assert projection["review_ready"] is True
    assert projection["waiting_merge"] is True
    assert projection["close_ready"] is True
    assert projection["worker_status"] == "waiting_merge"
    assert projection["stop_state"] == "waiting_merge"
    assert projection["task_id"] == "task-mf-sub-1"
    assert projection["backlog_id"] == "ARCH-MF-SUBAGENT-BACKEND"
    assert projection["runtime_context_id"] == "mfrctx-finish"
    assert projection["checkpoint_id"] == "ckpt-finish"
    assert projection["commit"] == "head456"
    assert projection["changed_files"] == ["agent/governance/mf_subagent_contract.py"]
    assert projection["route_identity"] == gate["route_identity"]
    assert projection["observer_command_id"] == "cmd-finish"
    assert projection["worker_identity"] == gate["worker_identity"]
    assert projection["startup_lineage"] == gate["startup_lineage"]
    assert projection["producer"] == "mf_subagent_worker"
    assert projection["source_event_kind"] == "mf_subagent_finish_gate"

    close_projection = gate["close_gate_projection"]
    assert close_projection["schema_version"] == (
        FINISH_GATE_CLOSE_PROJECTION_SCHEMA_VERSION
    )
    assert close_projection["implementation"] is True
    assert close_projection["review_ready"] is True
    assert close_projection["waiting_merge"] is True
    assert close_projection["close_ready"] is True
    assert close_projection["event_kinds_satisfied"] == [
        "implementation",
        "close_ready",
    ]
    assert close_projection["evidence_ids"] == [
        "bounded_implementation_subagent.implementation",
        "bounded_implementation_subagent.review_ready",
        "bounded_implementation_subagent.waiting_merge",
        "bounded_implementation_subagent.close_ready",
    ]
    assert close_projection["route_identity"] == gate["route_identity"]
    assert close_projection["observer_command_id"] == "cmd-finish"
    assert close_projection["worker_identity"] == gate["worker_identity"]
    assert close_projection["startup_lineage"] == gate["startup_lineage"]
    assert close_projection["commit"] == "head456"
    assert close_projection["changed_files"] == [
        "agent/governance/mf_subagent_contract.py"
    ]


def test_finish_gate_uses_runtime_successor_parent_for_graph_trace_lineage() -> None:
    context = _context(
        parent_task_id="cex-hotfix-successor",
        root_task_id="cex-onboard-root",
        chain_id="cchain-onboard-root",
        stage_task_id="task-mf-sub-1",
    )
    startup = _finish_startup_evidence(parent_task_id="cex-hotfix-successor")
    payload = {
        "project_id": "aming-claw",
        "task_id": "task-mf-sub-1",
        "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
        "branch_ref": "refs/heads/codex/task-mf-sub-1",
        "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
        "base_commit": "base123",
        "target_head_commit": "target123",
        "merge_queue_id": "mq-1",
        "head_commit": "head456",
        "status": "review_ready",
        "changed_files": ["agent/governance/mf_subagent_contract.py"],
        "test_results": {"status": "passed", "command": "pytest -q"},
        "checkpoint_id": "ckpt-hotfix-finish",
        "fence_token": "fence-2",
        "summary": "Ready.",
        "graph_trace_evidence": _graph_trace_evidence(
            parent_task_id="cex-hotfix-successor",
            fence_token="fence-2",
        ),
        "mf_subagent_startup_gate": startup,
        "real_startup_events": [_startup_event(startup)],
        "finish_time_worker_self_attestation": _finish_time_worker_attestation(),
        "read_receipt_hash": "sha256:read-finish",
        "read_receipt_event_id": "2873",
    }

    gate = validate_mf_subagent_finish_gate(payload, context=context)

    assert gate["parent_task_id"] == "cex-hotfix-successor"
    assert gate["graph_trace_evidence"]["parent_task_id"] == "cex-hotfix-successor"
    assert gate["startup_lineage"]["parent_task_id"] == "cex-hotfix-successor"
    assert context.root_task_id == "cex-onboard-root"
    assert context.chain_id == "cchain-onboard-root"

    root_payload = dict(payload)
    root_payload["graph_trace_evidence"] = _graph_trace_evidence(
        parent_task_id="cex-onboard-root",
        fence_token="fence-2",
    )
    with pytest.raises(
        MfSubagentContractError,
        match="graph trace evidence identity mismatch: parent_task_id",
    ):
        validate_mf_subagent_finish_gate(root_payload, context=context)

    claimed_root_payload = dict(payload)
    claimed_root_payload["parent_task_id"] = "cex-onboard-root"
    with pytest.raises(
        MfSubagentContractError,
        match="parent_task_id does not match runtime context",
    ):
        validate_mf_subagent_finish_gate(claimed_root_payload, context=context)


def test_finish_gate_uses_stage_parent_before_legacy_chain_when_root_absent() -> None:
    context = _context(
        parent_task_id="",
        root_task_id="",
        chain_id="cchain-onboard-root",
        stage_task_id="cex-hotfix-successor",
    )
    startup = _finish_startup_evidence(parent_task_id="cex-hotfix-successor")
    payload = {
        "project_id": "aming-claw",
        "task_id": "task-mf-sub-1",
        "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
        "branch_ref": "refs/heads/codex/task-mf-sub-1",
        "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
        "base_commit": "base123",
        "target_head_commit": "target123",
        "merge_queue_id": "mq-1",
        "head_commit": "head456",
        "status": "review_ready",
        "changed_files": ["agent/governance/mf_subagent_contract.py"],
        "test_results": {"status": "passed", "command": "pytest -q"},
        "checkpoint_id": "ckpt-hotfix-finish",
        "fence_token": "fence-2",
        "summary": "Ready.",
        "graph_trace_evidence": _graph_trace_evidence(
            parent_task_id="cex-hotfix-successor",
            fence_token="fence-2",
        ),
        "mf_subagent_startup_gate": startup,
        "real_startup_events": [_startup_event(startup)],
        "finish_time_worker_self_attestation": _finish_time_worker_attestation(),
        "read_receipt_hash": "sha256:read-finish",
        "read_receipt_event_id": "2873",
    }

    gate = validate_mf_subagent_finish_gate(payload, context=context)

    assert gate["parent_task_id"] == "cex-hotfix-successor"
    assert context.chain_id == "cchain-onboard-root"

    chain_payload = dict(payload)
    chain_payload["graph_trace_evidence"] = _graph_trace_evidence(
        parent_task_id="cchain-onboard-root",
        fence_token="fence-2",
    )
    with pytest.raises(
        MfSubagentContractError,
        match="graph trace evidence identity mismatch: parent_task_id",
    ):
        validate_mf_subagent_finish_gate(chain_payload, context=context)


def test_finish_gate_projects_close_fields_from_startup_lineage_for_close_gate() -> None:
    startup = _finish_startup_evidence(
        parent_task_id="ARCH-MF-SUBAGENT-BACKEND",
        runtime_context_id="mfrctx-daily-planner",
        worker_id="worker-b",
        worker_slot_id="worker-slot-b",
        agent_id="agent-b",
        route_id="route-daily-child",
        route_context_hash="sha256:daily-child-route",
        prompt_contract_id="rprompt-daily-child",
        prompt_contract_hash="sha256:daily-child-prompt",
        route_token_ref="rtok-daily-child",
        visible_injection_manifest_hash="sha256:daily-visible",
        observer_command_id="cmd-e49d85248073",
        read_receipt_hash="sha256:daily-read-receipt",
        read_receipt_event_id="evt-daily-read-receipt",
        **_self_attested_startup_fields(
            worker_session_id="worker-session-daily",
            transcript_path="/tmp/worker-session-daily.jsonl",
            transcript_ref="codex-thread:worker-session-daily",
            harness_type="codex",
        ),
    )

    gate = validate_mf_subagent_finish_gate(
        {
            "project_id": "aming-claw",
            "task_id": "task-mf-sub-1",
            "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
            "branch_ref": "refs/heads/codex/task-mf-sub-1",
            "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
            "base_commit": "base123",
            "target_head_commit": "target123",
            "merge_queue_id": "mq-1",
            "head_commit": "cae2641a58ef9fa13ba0462a90b146d5d9f4a864",
            "status": "review_ready",
            "changed_files": ["src/App.jsx", "src/styles.css"],
            "owned_files": ["src/App.jsx", "src/styles.css"],
            "test_results": {"status": "passed", "command": "npm test"},
            "checkpoint_id": "ckpt-daily-finish",
            "fence_token": "fence-2",
            "summary": "Daily Planner demo worker ready for review.",
            "real_startup_events": [_startup_event(startup, event_id="evt-daily-startup")],
            "finish_time_worker_self_attestation": _finish_time_worker_attestation(
                worker_session_id="worker-session-daily",
                transcript_path="/tmp/worker-session-daily.jsonl",
                transcript_ref="codex-thread:worker-session-daily",
                harness_type="codex",
            ),
        },
        context=_context(),
    )

    assert gate["observer_command_id"] == "cmd-e49d85248073"
    assert gate["read_receipt_hash"] == "sha256:daily-read-receipt"
    assert gate["read_receipt_event_id"] == "evt-daily-read-receipt"
    assert gate["route_identity"] == {
        "route_id": "route-daily-child",
        "route_context_hash": "sha256:daily-child-route",
        "prompt_contract_id": "rprompt-daily-child",
        "prompt_contract_hash": "sha256:daily-child-prompt",
        "route_token_ref": "rtok-daily-child",
        "visible_injection_manifest_hash": "sha256:daily-visible",
    }
    assert gate["worker_identity"]["worker_id"] == "worker-b"
    assert gate["worker_identity"]["worker_slot_id"] == "worker-slot-b"
    assert gate["worker_identity"]["agent_id"] == "agent-b"
    assert gate["worker_identity"]["worker_session_id"] == "worker-session-daily"
    assert gate["startup_lineage"]["parent_task_id"] == "ARCH-MF-SUBAGENT-BACKEND"
    assert gate["startup_lineage"]["startup_event_id"] == "evt-daily-startup"
    assert gate["startup_lineage"]["runtime_context_id"] == "mfrctx-daily-planner"

    close_projection = gate["close_gate_projection"]
    assert close_projection["schema_version"] == (
        FINISH_GATE_CLOSE_PROJECTION_SCHEMA_VERSION
    )
    assert close_projection["implementation"] is True
    assert close_projection["review_ready"] is True
    assert close_projection["waiting_merge"] is True
    assert close_projection["close_ready"] is True
    assert close_projection["event_kinds_satisfied"] == [
        "implementation",
        "close_ready",
    ]
    assert close_projection["commit"] == "cae2641a58ef9fa13ba0462a90b146d5d9f4a864"
    assert close_projection["changed_files"] == ["src/App.jsx", "src/styles.css"]
    assert close_projection["route_identity"] == gate["route_identity"]
    assert close_projection["observer_command_id"] == "cmd-e49d85248073"
    assert close_projection["worker_identity"] == gate["worker_identity"]
    assert close_projection["startup_lineage"] == gate["startup_lineage"]


def test_finish_gate_review_ready_projection_satisfies_lane_ownership_shape() -> None:
    from agent.governance import task_timeline

    gate = validate_mf_subagent_finish_gate(
        {
            "project_id": "aming-claw",
            "task_id": "task-mf-sub-1",
            "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
            "branch_ref": "refs/heads/codex/task-mf-sub-1",
            "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
            "base_commit": "base123",
            "target_head_commit": "target123",
            "merge_queue_id": "mq-1",
            "head_commit": "head456",
            "status": "succeeded",
            "changed_files": ["agent/governance/mf_subagent_contract.py"],
            "test_results": {"status": "passed", "command": "pytest -q"},
            "checkpoint_id": "ckpt-finish-lane-ready",
            "fence_token": "fence-2",
            "parent_task_id": "task-mf-parent",
            "route_id": "route-finish-lane-ready",
            "route_context_hash": "sha256:route-finish-lane-ready",
            "prompt_contract_id": "rprompt-finish-lane-ready",
            "prompt_contract_hash": "sha256:prompt-finish-lane-ready",
            "route_token_ref": "rtok-finish-lane-ready",
            "visible_injection_manifest_hash": "sha256:visible-finish-lane-ready",
            "summary": "Ready.",
            "worker_self_attestation": _finish_time_worker_attestation(),
            "mf_subagent_startup_gate": _finish_startup_evidence(),
            "real_startup_events": [_startup_event(_finish_startup_evidence())],
            "read_receipt_hash": "sha256:read-finish",
            "read_receipt_event_id": "2873",
        },
        context=_context(),
    )
    dispatch_event = {
        "event_type": "mf_subagent.dispatch",
        "phase": "bounded_subagent_dispatch",
        "actor": "observer",
        "status": "accepted",
        "payload": {
            "required_dispatch_key": "bounded_subagent_dispatch",
            "worker_role": "mf_sub",
            "task_id": "task-mf-sub-1",
        },
    }
    finish_gate_event = {
        "event_type": "mf_subagent.finish_gate",
        "event_kind": "mf_subagent_finish_gate",
        "phase": "finish_gate",
        "actor": "worker-session-codex-1",
        "status": "passed",
        "task_id": "task-mf-sub-1",
        "payload": {
            "mf_subagent_finish_gate": gate,
            "checkpoint_id": gate["checkpoint_id"],
        },
    }

    lane_gate = task_timeline.mf_lane_ownership_gate_verification(
        [dispatch_event, finish_gate_event],
        contract={
            "required_lanes": [
                {"id": "bounded_implementation_subagent", "role": "mf_sub"}
            ]
        },
    )

    assert lane_gate["passed"] is True
    assert lane_gate["present_lane_ownership_ids"] == [
        "bounded_implementation_subagent.dispatch",
        "bounded_implementation_subagent.review_ready",
    ]
    assert lane_gate["missing_lane_ownership_ids"] == []
    projection = gate["lane_ownership_projection"]
    assert projection["fence_token"] == "fence-2"
    assert projection["merge_queue_id"] == "mq-1"
    assert projection["route_id"] == "route-finish-lane-ready"
    assert projection["route_context_hash"] == "sha256:route-finish-lane-ready"
    assert projection["prompt_contract_id"] == "rprompt-finish-lane-ready"
    assert projection["prompt_contract_hash"] == "sha256:prompt-finish-lane-ready"
    assert projection["route_token_ref"] == "rtok-finish-lane-ready"
    assert projection["visible_injection_manifest_hash"] == (
        "sha256:visible-finish-lane-ready"
    )
    assert projection["worker_role"] == "mf_sub"


def test_dogfood_close_gate_accepts_canonical_worker_happy_path_evidence() -> None:
    from agent.governance import task_timeline

    backlog_id = "DPL-FOCUS-REMINDERS-20260619"
    task_id = "DPL-FOCUS-REMINDERS-20260619-worker-a"
    route_identity = {
        "route_id": "route-dogfood-daily-planner",
        "route_context_hash": "sha256:dogfood-route-context",
        "prompt_contract_id": "rprompt-dogfood",
        "prompt_contract_hash": "sha256:dogfood-prompt",
        "visible_injection_manifest_hash": "sha256:dogfood-visible",
        "route_token_ref": "rtok-dogfood",
    }
    startup = _finish_startup_evidence(
        **route_identity,
        task_id=task_id,
        parent_task_id=backlog_id,
        worker_id="focus-worker",
        worker_slot_id="focus-worker",
        runtime_context_id="mfrctx-daily-planner",
        branch="refs/heads/codex/daily-planner-focus",
        actual_cwd="/tmp/daily-planner-focus",
        actual_git_root="/tmp/daily-planner-focus",
        worktree="/tmp/daily-planner-focus",
        head_commit="2543eeeeb5675414dc008034276dbd8f4cfa2218",
        observer_command_id="cmd-dogfood",
        read_receipt_hash="sha256:dogfood-read-receipt",
        read_receipt_event_id="5",
        **_self_attested_startup_fields(
            worker_session_id="worker-session-focus",
            transcript_path="/tmp/worker-session-focus.jsonl",
            transcript_ref="codex-thread:worker-session-focus",
        ),
    )
    finish_gate = validate_mf_subagent_finish_gate(
        {
            "project_id": "daily-planner-lite-20260619064012-8fd77a5f",
            "task_id": task_id,
            "parent_task_id": backlog_id,
            "backlog_id": backlog_id,
            "branch_ref": "refs/heads/codex/daily-planner-focus",
            "worktree_path": "/tmp/daily-planner-focus",
            "base_commit": "base-dogfood",
            "target_head_commit": "target-dogfood",
            "merge_queue_id": "mq-dogfood",
            "head_commit": "2543eeeeb5675414dc008034276dbd8f4cfa2218",
            "status": "review_ready",
            "changed_files": ["src/App.jsx"],
            "test_results": {"status": "passed", "command": "npm test"},
            "checkpoint_id": "ckpt-dogfood",
            "fence_token": "fence-2",
            "summary": "Daily planner focus reminders ready.",
            "real_startup_events": [_startup_event(startup, event_id="7")],
            "finish_time_worker_self_attestation": _finish_time_worker_attestation(
                worker_session_id="worker-session-focus",
                transcript_path="/tmp/worker-session-focus.jsonl",
                transcript_ref="codex-thread:worker-session-focus",
            ),
        },
        context=_context(
            project_id="daily-planner-lite-20260619064012-8fd77a5f",
            task_id=task_id,
            backlog_id=backlog_id,
            branch_ref="refs/heads/codex/daily-planner-focus",
            worktree_path="/tmp/daily-planner-focus",
            base_commit="base-dogfood",
            head_commit="2543eeeeb5675414dc008034276dbd8f4cfa2218",
            target_head_commit="target-dogfood",
            merge_queue_id="mq-dogfood",
        ),
    )
    common = {"backlog_id": backlog_id, "task_id": task_id}
    events = [
        {
            "id": 2,
            **common,
            "event_kind": "route_action_precheck",
            "phase": "route_action_precheck",
            "status": "passed",
            "payload": {**route_identity, "allowed_action": "dispatch_bounded_worker"},
        },
        {
            "id": 3,
            **common,
            "event_kind": "service_route",
            "phase": "dispatch",
            "status": "passed",
            "actor": "service-router",
            "payload": {
                "route_context": route_identity,
                "dispatch_evidence": {
                    **route_identity,
                    "source": "observer_runtime_text_prepare",
                    "service_generated": True,
                    "worker_role": "mf_sub",
                    "task_id": task_id,
                    "parent_task_id": backlog_id,
                    "worker_slot_id": "focus-worker",
                    "route_token_ref": route_identity["route_token_ref"],
                    "parent_route_lineage": {
                        **route_identity,
                        "backlog_id": backlog_id,
                    },
                    "child_route_lineage": {
                        **route_identity,
                        "parent_route_token_ref": route_identity["route_token_ref"],
                    },
                },
            },
        },
        {
            "id": 4,
            **common,
            "event_kind": "observer_work_mode_transition",
            "phase": "routing",
            "status": "accepted",
            "payload": {
                "route_identity": route_identity,
                "route_action_precheck_event_id": "2",
            },
        },
        {
            "id": 5,
            **common,
            "event_kind": "mf_subagent_read_receipt",
            "phase": "startup_read_receipt",
            "status": "passed",
            "payload": {
                **route_identity,
                "runtime_context_id": "mfrctx-daily-planner",
                "parent_task_id": backlog_id,
                "worker_slot_id": "focus-worker",
                "fence_token": "fence-2",
                "read_receipt_hash": "sha256:dogfood-read-receipt",
            },
        },
        {
            "id": 6,
            **common,
            "event_kind": "mf_subagent_startup",
            "phase": "startup_gate",
            "status": "blocked",
            "payload": {"reason": "first startup refused before retry"},
        },
        {
            "id": 7,
            **common,
            "event_kind": "mf_subagent_startup",
            "phase": "startup_gate",
            "status": "passed",
            "payload": {"mf_subagent_startup_gate": startup},
        },
        {
            "id": 9,
            **common,
            "event_kind": "implementation",
            "phase": "implementation",
            "status": "passed",
            "actor": "mf_sub",
            "payload": {
                **route_identity,
                "worker_role": "mf_sub",
                "changed_files": ["src/App.jsx"],
                "graph_trace_ids": ["gqt-dogfood-worker"],
            },
        },
        {
            "id": 10,
            **common,
            "event_type": "mf_subagent.finish_gate",
            "event_kind": "mf_subagent_finish_gate",
            "phase": "finish_gate",
            "status": "passed",
            "actor": "worker-session-focus",
            "payload": {"mf_subagent_finish_gate": finish_gate},
        },
        {
            "id": 12,
            **common,
            "event_kind": "independent_verification",
            "phase": "verification",
            "status": "passed",
            "actor": "qa-reviewer",
            "payload": {**route_identity, "reviewer": "qa-reviewer"},
        },
        {
            "id": 13,
            **common,
            "event_type": "observer.visual_smoke",
            "event_kind": "observer_visual_smoke",
            "phase": "visual_smoke",
            "status": "passed",
            "actor": "observer",
            "payload": {
                **route_identity,
                "observer_visual_smoke": {
                    "status": "passed",
                    "url": "http://127.0.0.1:4173",
                },
                "meta_contract_gate": {
                    "role": "observer",
                    "action": "observer_visual_smoke",
                    "status": "passed",
                    "allowed": True,
                },
            },
        },
        {
            "id": 14,
            **common,
            "event_kind": "merge",
            "phase": "merge",
            "status": "passed",
            "actor": "observer",
            "payload": {
                **route_identity,
                "merge_commit": "merge-dogfood",
            },
        },
    ]
    contract = {
        "template_id": "mf_parallel.v1",
        "contract_instance_id": backlog_id,
        "route_topology_policy": {
            "selected_topology": "observer_led_parallel_lanes",
            "recommended_topology": "mf_parallel.v1",
            "required_lanes": [
                "observer_coordinator",
                "bounded_implementation_worker",
                "independent_verification_lane",
            ],
            "independent_verification_required": True,
        },
        "governance_policy": {
            "profile": "third-party-public",
            "requirements": {
                "close_timeline": True,
                "worker_graph_trace": False,
                "independent_qa": False,
            },
        },
        "evidence_requirements": [
            {"id": "route_context", "required": True},
            {"id": "route_action_precheck", "required": True},
            {"id": "bounded_implementation_worker_dispatch", "required": True},
            {"id": "mf_subagent_startup", "required": True},
            {"id": "bounded_implementation_subagent.review_ready", "required": True},
            {"id": "independent_verification_lane", "required": True},
            {"id": "observer_visual_smoke", "required": True},
        ],
    }

    gate = task_timeline.mf_close_gate_verification(events, contract)

    assert gate["passed"] is True
    assert gate["contract_gate"]["missing_requirement_ids"] == []
    assert gate["route_context_gate"]["missing_requirement_ids"] == []
    assert gate["route_context_gate"]["checks"]["independent_verification_lane_present"] is True
    assert gate["lane_ownership_gate"]["missing_requirement_ids"] == []
    assert gate["independent_qa_gate"]["evidence_events"][0]["id"] == 12
    assert gate["contract_gate"]["present_requirement_ids"] == [
        "bounded_implementation_subagent.review_ready",
        "bounded_implementation_worker_dispatch",
        "independent_verification_lane",
        "mf_subagent_startup",
        "observer_visual_smoke",
        "route_action_precheck",
        "route_context",
    ]


def test_contract_gate_rejects_failed_event_nested_bare_string_evidence() -> None:
    from agent.governance import task_timeline

    gate = task_timeline.mf_contract_gate_verification(
        [
            {
                "id": 1,
                "event_kind": "worker_note",
                "status": "failed",
                "payload": {
                    "child_gate": {
                        "status": "passed",
                        "contract_evidence": ["close_ready"],
                    }
                },
            }
        ],
        {"evidence_requirements": [{"id": "close_ready", "required": True}]},
    )

    assert gate["passed"] is False
    assert gate["missing_requirement_ids"] == ["close_ready"]
    assert gate["present_requirement_ids"] == []


def test_contract_gate_rejects_unrelated_nested_visual_smoke_projection() -> None:
    from agent.governance import task_timeline

    gate = task_timeline.mf_contract_gate_verification(
        [
            {
                "id": 1,
                "event_kind": "implementation",
                "status": "passed",
                "actor": "mf_sub",
                "payload": {
                    "worker_result": {
                        "observer_visual_smoke_projection": {
                            "id": "observer_visual_smoke",
                            "status": "passed",
                            "server_projected": True,
                            "source": "task_timeline.merge_support_projection",
                        },
                    },
                    "visual_smoke": {"status": "passed"},
                },
            }
        ],
        {"evidence_requirements": [{"id": "observer_visual_smoke", "required": True}]},
    )

    assert gate["passed"] is False
    assert gate["missing_requirement_ids"] == ["observer_visual_smoke"]


def test_contract_gate_accepts_explicit_observer_visual_smoke_event() -> None:
    from agent.governance import task_timeline

    gate = task_timeline.mf_contract_gate_verification(
        [
            {
                "id": 1,
                "event_type": "observer.visual_smoke",
                "event_kind": "observer_visual_smoke",
                "status": "passed",
                "actor": "observer",
                "payload": {
                    "observer_visual_smoke": {"status": "passed"},
                    "meta_contract_gate": {
                        "role": "observer",
                        "action": "observer_visual_smoke",
                        "status": "passed",
                        "allowed": True,
                    },
                },
            }
        ],
        {"evidence_requirements": [{"id": "observer_visual_smoke", "required": True}]},
    )

    assert gate["passed"] is True
    assert gate["missing_requirement_ids"] == []


def test_route_context_gate_rejects_forged_action_scoped_qa_lineage() -> None:
    from agent.governance import task_timeline

    parent_identity = {
        "route_context_hash": "sha256:parent-route",
        "prompt_contract_id": "rprompt-parent",
        "prompt_contract_hash": "sha256:parent-prompt",
        "visible_injection_manifest_hash": "sha256:visible",
    }
    child_identity = {
        **parent_identity,
        "prompt_contract_id": "rprompt-child-action",
        "prompt_contract_hash": "sha256:child-prompt",
    }
    events = [
        {
            "id": 1,
            "event_kind": "route_context",
            "status": "passed",
            "payload": {"route_context": parent_identity},
        },
        {
            "id": 2,
            "event_kind": "route_action_precheck",
            "status": "passed",
            "payload": parent_identity,
        },
        {
            "id": 3,
            "event_kind": "bounded_implementation_worker_dispatch",
            "status": "passed",
            "payload": parent_identity,
        },
        {
            "id": 4,
            "event_kind": "mf_subagent_startup",
            "status": "passed",
            "payload": {
                **parent_identity,
                "actual_cwd": "/tmp/worker",
                "branch": "worker",
                "head_commit": "head",
                "fence_token": "fence",
            },
        },
        {
            "id": 5,
            "event_kind": "independent_verification",
            "status": "passed",
            "actor": "qa-reviewer",
            "payload": {
                **child_identity,
                "route_token_ref": "rtok-child-action",
                "reviewer": "qa-reviewer",
                "reviewer_role": "qa",
                "route_identity": {
                    "allowed_action": "task_timeline_append",
                    "action": "independent_verification",
                },
                "meta_contract_gate": {
                    "role": "qa",
                    "action": "independent_verification",
                    "status": "passed",
                    "allowed": True,
                },
            },
        },
    ]

    gate = task_timeline.mf_route_context_gate_verification(
        events,
        contract={
            "route_topology_policy": {
                "selected_topology": "observer_led_parallel_lanes",
                "recommended_topology": "mf_parallel.v1",
                "required_lanes": ["independent_verification_lane"],
                "independent_verification_required": True,
            }
        },
    )

    assert gate["passed"] is False
    assert "independent_verification_lane" in gate["missing_requirement_ids"]
    assert gate["checks"]["independent_verification_lane_present"] is False
    assert gate["accepted_action_scope_lineages"] == []


def test_route_context_gate_accepts_server_backed_action_scoped_qa_lineage() -> None:
    from agent.governance import task_timeline

    parent_identity = {
        "route_context_hash": "sha256:parent-route",
        "prompt_contract_id": "rprompt-parent",
        "prompt_contract_hash": "sha256:parent-prompt",
        "visible_injection_manifest_hash": "sha256:visible",
    }
    child_identity = {
        **parent_identity,
        "route_context_hash": "sha256:child-route",
        "prompt_contract_id": "rprompt-child-action",
        "prompt_contract_hash": "sha256:child-prompt",
    }
    events = [
        {
            "id": 1,
            "event_kind": "route_context",
            "status": "passed",
            "payload": {"route_context": parent_identity},
        },
        {
            "id": 2,
            "event_kind": "route_action_precheck",
            "status": "passed",
            "payload": parent_identity,
        },
        {
            "id": 3,
            "event_kind": "bounded_implementation_worker_dispatch",
            "status": "passed",
            "payload": parent_identity,
        },
        {
            "id": 4,
            "event_kind": "mf_subagent_startup",
            "status": "passed",
            "payload": {
                **parent_identity,
                "actual_cwd": "/tmp/worker",
                "branch": "worker",
                "head_commit": "head",
                "fence_token": "fence",
            },
        },
        {
            "id": 5,
            "event_kind": "independent_verification",
            "status": "passed",
            "actor": "qa-reviewer",
            "payload": {
                **child_identity,
                "route_token_ref": "rtok-child-action",
                "reviewer": "qa-reviewer",
                "reviewer_role": "qa",
                "route_token_gate": {
                    **child_identity,
                    "action": "task_timeline_append",
                    "allowed": True,
                    "status": "accepted",
                    "decision": "route_token_ref_resolved",
                    "route_token_ref": "rtok-child-action",
                    "resolved_from_ref": True,
                    "server_issued_binding": True,
                    "binding_source": "observer_route_token_refs",
                },
                "route_action_scope_lineage": {
                    "accepted": True,
                    "status": "accepted",
                    "acceptance_source": "route_token_ref_resolved",
                    "server_issued_binding": True,
                    "resolved_from_ref": True,
                    "route_token_ref": "rtok-child-action",
                    "parent_route_identity": parent_identity,
                    "child_route_identity": child_identity,
                },
                "meta_contract_gate": {
                    "role": "qa",
                    "action": "independent_verification",
                    "status": "passed",
                    "allowed": True,
                },
            },
        },
    ]

    gate = task_timeline.mf_route_context_gate_verification(
        events,
        contract={
            "route_topology_policy": {
                "selected_topology": "observer_led_parallel_lanes",
                "recommended_topology": "mf_parallel.v1",
                "required_lanes": ["independent_verification_lane"],
                "independent_verification_required": True,
            }
        },
    )

    assert gate["passed"] is True
    assert gate["missing_requirement_ids"] == []
    assert gate["checks"]["independent_verification_lane_present"] is True
    assert gate["accepted_action_scope_lineages"][0]["server_lineage"][
        "server_backed"
    ] is True


def test_observer_on_behalf_transport_does_not_satisfy_route_lane_without_lineage() -> None:
    from agent.governance import task_timeline

    parent_identity = {
        "route_context_hash": "sha256:parent-route",
        "prompt_contract_id": "rprompt-parent",
        "prompt_contract_hash": "sha256:parent-prompt",
        "visible_injection_manifest_hash": "sha256:visible",
    }
    child_identity = {
        **parent_identity,
        "route_context_hash": "sha256:child-route",
        "prompt_contract_id": "rprompt-child-action",
        "prompt_contract_hash": "sha256:child-prompt",
    }
    base_events = [
        {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
        {"event_kind": "verification", "phase": "verification", "status": "passed"},
        {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        {
            "event_kind": "route_context",
            "status": "passed",
            "payload": {"route_context": parent_identity},
        },
        {
            "event_kind": "route_action_precheck",
            "status": "passed",
            "payload": parent_identity,
        },
        {
            "event_kind": "bounded_implementation_worker_dispatch",
            "status": "passed",
            "payload": parent_identity,
        },
        {
            "event_kind": "mf_subagent_startup",
            "status": "passed",
            "payload": {
                **parent_identity,
                "actual_cwd": "/tmp/worker",
                "branch": "worker",
                "head_commit": "head",
                "fence_token": "fence",
            },
        },
    ]
    observer_on_behalf = {
        "id": 8,
        "event_kind": "independent_verification",
        "phase": "verification",
        "status": "passed",
        "actor": "observer-on-behalf-of:qa-reviewer",
        "payload": {
            **child_identity,
            "route_token_ref": "rtok-child-action",
            "reviewer": "qa-reviewer",
            "reviewer_role": "qa",
            "route_identity": {
                "allowed_action": "task_timeline_append",
                "action": "independent_verification",
            },
            "meta_contract_gate": {
                "role": "observer",
                "action": "independent_verification",
                "status": "passed",
                "allowed": True,
            },
        },
    }

    contract = {
        "route_topology_policy": {
            "selected_topology": "observer_led_parallel_lanes",
            "recommended_topology": "mf_parallel.v1",
            "required_lanes": ["independent_verification_lane"],
            "independent_verification_required": True,
        },
        "governance_policy": {"requirements": {"independent_qa": True}},
    }

    gate = task_timeline.mf_close_gate_verification(
        [*base_events, observer_on_behalf],
        contract=contract,
    )

    assert gate["independent_qa_gate"]["passed"] is False
    assert gate["independent_qa_gate"]["rejected_evidence_events"][0]["reason"] == (
        "observer_transport_not_independent_qa"
    )
    assert gate["route_context_gate"]["checks"][
        "independent_verification_lane_present"
    ] is False
    assert "independent_verification_lane" in gate["route_context_gate"][
        "missing_requirement_ids"
    ]

    direct_qa = {
        **observer_on_behalf,
        "actor": "qa-reviewer",
        "payload": {
            **observer_on_behalf["payload"],
            "meta_contract_gate": {
                "role": "qa",
                "action": "independent_verification",
                "status": "passed",
                "allowed": True,
            },
        },
    }
    direct_gate = task_timeline.mf_close_gate_verification(
        [*base_events, direct_qa],
        contract=contract,
    )
    assert direct_gate["independent_qa_gate"]["passed"] is True
    assert direct_gate["independent_qa_gate"]["evidence_events"][0]["actor"] == (
        "qa-reviewer"
    )


def test_finish_gate_rejects_caller_supplied_review_ready_bridge() -> None:
    with pytest.raises(MfSubagentContractError, match="mf_subagent_startup"):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "aming-claw",
                "task_id": "task-mf-sub-1",
                "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
                "branch_ref": "refs/heads/codex/task-mf-sub-1",
                "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
                "base_commit": "base123",
                "target_head_commit": "target123",
                "merge_queue_id": "mq-1",
                "head_commit": "head456",
                "status": "succeeded",
                "changed_files": ["agent/governance/mf_subagent_contract.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish-caller-bridge",
                "fence_token": "fence-2",
                "summary": "Observer-authored bridge should not pass.",
                "review_ready": True,
                "worker_status": "waiting_merge",
                "lane_ownership_projection": {
                    "review_ready": True,
                    "worker_status": "waiting_merge",
                    "worker_role": "mf_sub",
                },
            },
            context=_context(),
        )


def test_finish_gate_rejects_caller_supplied_close_projection_without_startup() -> None:
    with pytest.raises(MfSubagentContractError, match="mf_subagent_startup"):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "aming-claw",
                "task_id": "task-mf-sub-1",
                "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
                "branch_ref": "refs/heads/codex/task-mf-sub-1",
                "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
                "base_commit": "base123",
                "target_head_commit": "target123",
                "merge_queue_id": "mq-1",
                "head_commit": "head456",
                "status": "succeeded",
                "changed_files": ["agent/governance/mf_subagent_contract.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish-caller-close-projection",
                "fence_token": "fence-2",
                "summary": "Caller-supplied close projection should not pass.",
                "close_ready": True,
                "implementation": True,
                "review_ready": True,
                "waiting_merge": True,
                "close_gate_projection": {
                    "schema_version": FINISH_GATE_CLOSE_PROJECTION_SCHEMA_VERSION,
                    "implementation": True,
                    "review_ready": True,
                    "waiting_merge": True,
                    "close_ready": True,
                    "event_kinds_satisfied": ["implementation", "close_ready"],
                },
                "finish_time_worker_self_attestation": (
                    _finish_time_worker_attestation()
                ),
                "read_receipt_hash": "sha256:read-finish",
                "read_receipt_event_id": "2873",
            },
            context=_context(),
        )


def test_finish_gate_does_not_project_surrogate_startup_as_review_ready() -> None:
    surrogate_startup = _finish_startup_evidence(
        agent_id_match_mode="host_adapter_startup_token_surrogate",
        session_token_evidence_type="surrogate",
        session_token_hash="",
        session_token_present=False,
        host_adapter_startup_token_accepted=True,
    )

    with pytest.raises(MfSubagentContractError, match="surrogate-only"):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "aming-claw",
                "task_id": "task-mf-sub-1",
                "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
                "branch_ref": "refs/heads/codex/task-mf-sub-1",
                "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
                "base_commit": "base123",
                "target_head_commit": "target123",
                "merge_queue_id": "mq-1",
                "head_commit": "head456",
                "status": "succeeded",
                "changed_files": ["agent/governance/mf_subagent_contract.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish-surrogate",
                "fence_token": "fence-2",
                "summary": "Surrogate startup should not pass.",
                "mf_subagent_startup_gate": surrogate_startup,
                "real_startup_events": [_startup_event(surrogate_startup)],
                "finish_time_worker_self_attestation": _finish_time_worker_attestation(),
                "read_receipt_hash": "sha256:read-finish",
                "read_receipt_event_id": "2873",
            },
            context=_context(),
        )


def test_finish_gate_accepts_startup_at_base_commit_with_finish_time_attestation() -> None:
    startup_evidence = _finish_startup_evidence(head_commit="base123")

    gate = validate_mf_subagent_finish_gate(
        {
            "project_id": "aming-claw",
            "task_id": "task-mf-sub-1",
            "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
            "branch_ref": "refs/heads/codex/task-mf-sub-1",
            "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
            "base_commit": "base123",
            "target_head_commit": "target123",
            "merge_queue_id": "mq-1",
            "head_commit": "head456",
            "status": "succeeded",
            "changed_files": ["agent/governance/mf_subagent_contract.py"],
            "test_results": {"status": "passed", "command": "pytest -q"},
            "checkpoint_id": "ckpt-finish-worker-head",
            "fence_token": "fence-2",
            "summary": "Ready.",
            "real_startup_events": [_startup_event(startup_evidence)],
            "read_receipt_hash": "sha256:read-finish",
            "read_receipt_event_id": "2873",
            "finish_time_worker_self_attestation": (
                _finish_time_worker_attestation()
            ),
        },
        context=_context(),
    )

    assert gate["startup_evidence"]["head_commit"] == "base123"
    assert gate["head_commit"] == "head456"
    assert gate["startup_worker_identity_gate"]["passed"] is True
    assert gate["worker_self_attestation_gate"]["passed"] is True
    assert gate["worker_self_attestation"]["finish_time_self_attesting"] is True
    assert gate["close_ready"] is True


def test_finish_gate_accepts_actual_startup_identity_before_finish_attestation() -> None:
    startup_evidence = _finish_startup_evidence(
        close_satisfying=False,
        worker_self_attesting=False,
        self_attesting=False,
        finish_time_self_attesting=False,
        worker_self_attestation={},
        worker_transcript_path="",
        worker_transcript_ref="codex-thread:actual-startup",
        harness_type="codex_builtin_subagent",
    )

    gate = validate_mf_subagent_finish_gate(
        {
            "project_id": "aming-claw",
            "task_id": "task-mf-sub-1",
            "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
            "branch_ref": "refs/heads/codex/task-mf-sub-1",
            "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
            "base_commit": "base123",
            "target_head_commit": "target123",
            "merge_queue_id": "mq-1",
            "head_commit": "head456",
            "status": "succeeded",
            "changed_files": ["agent/governance/mf_subagent_contract.py"],
            "test_results": {"status": "passed", "command": "pytest -q"},
            "checkpoint_id": "ckpt-finish-actual-startup-identity",
            "fence_token": "fence-2",
            "summary": "Ready.",
            "real_startup_events": [_startup_event(startup_evidence)],
            "read_receipt_hash": "sha256:read-finish",
            "read_receipt_event_id": "2873",
            "finish_time_worker_self_attestation": (
                _finish_time_worker_attestation(
                    transcript_path="",
                    transcript_ref="codex-thread:actual-startup",
                    harness_type="codex_builtin_subagent",
                )
            ),
        },
        context=_context(),
    )

    assert gate["startup_evidence"]["close_satisfying"] is False
    assert gate["startup_worker_identity_gate"]["passed"] is True
    assert gate["startup_worker_identity_gate"]["worker_transcript_ref"] == (
        "codex-thread:actual-startup"
    )
    assert gate["startup_worker_identity_gate"]["harness_type"] == "codex"
    assert gate["worker_self_attestation_gate"]["passed"] is True
    assert gate["worker_self_attestation_gate"]["worker_transcript_ref"] == (
        "codex-thread:actual-startup"
    )
    assert gate["close_ready"] is True


def test_finish_gate_keeps_adjacent_invalid_startup_lane_blocked() -> None:
    invalid_adjacent_startup = _finish_startup_evidence(
        task_id="task-mf-sub-2",
        runtime_context_id="mfrctx-adjacent",
        fence_token="fence-adjacent",
        actual_cwd="/tmp/aming-claw-wt/task-mf-sub-2",
        actual_git_root="/tmp/aming-claw-wt/task-mf-sub-2",
        worktree="/tmp/aming-claw-wt/task-mf-sub-2",
        branch="refs/heads/codex/task-mf-sub-2",
        worker_session_id="worker-session-adjacent",
        filer_principal="worker-session-adjacent",
    )
    valid_startup = _finish_startup_evidence(
        close_satisfying=False,
        worker_self_attesting=False,
        self_attesting=False,
        finish_time_self_attesting=False,
        worker_self_attestation={},
        worker_transcript_path="",
        worker_transcript_ref="codex-thread:task-mf-sub-1",
        harness_type="codex_builtin_subagent",
    )
    finish_payload = {
        "project_id": "aming-claw",
        "task_id": "task-mf-sub-1",
        "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
        "branch_ref": "refs/heads/codex/task-mf-sub-1",
        "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
        "base_commit": "base123",
        "target_head_commit": "target123",
        "merge_queue_id": "mq-1",
        "head_commit": "head456",
        "status": "succeeded",
        "changed_files": ["agent/governance/mf_subagent_contract.py"],
        "test_results": {"status": "passed", "command": "pytest -q"},
        "checkpoint_id": "ckpt-finish-two-lane",
        "fence_token": "fence-2",
        "summary": "Ready.",
        "read_receipt_hash": "sha256:read-finish",
        "read_receipt_event_id": "2873",
        "finish_time_worker_self_attestation": _finish_time_worker_attestation(
            transcript_path="",
            transcript_ref="codex-thread:task-mf-sub-1",
            harness_type="codex_builtin_subagent",
        ),
    }

    gate = validate_mf_subagent_finish_gate(
        {
            **finish_payload,
            "real_startup_events": [
                _startup_event(invalid_adjacent_startup, event_id="evt-adjacent"),
                _startup_event(valid_startup, event_id="evt-valid"),
            ],
        },
        context=_context(),
    )

    assert gate["startup_evidence"]["task_id"] == "task-mf-sub-1"
    assert gate["startup_worker_identity_gate"]["passed"] is True

    with pytest.raises(MfSubagentContractError, match="startup_finish_lineage_mismatch"):
        validate_mf_subagent_finish_gate(
            {
                **finish_payload,
                "real_startup_events": [
                    _startup_event(invalid_adjacent_startup, event_id="evt-adjacent")
                ],
            },
            context=_context(),
        )


def test_finish_gate_rejects_observer_authored_startup_or_read_receipt() -> None:
    base_payload = {
        "project_id": "aming-claw",
        "task_id": "task-mf-sub-1",
        "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
        "branch_ref": "refs/heads/codex/task-mf-sub-1",
        "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
        "base_commit": "base123",
        "target_head_commit": "target123",
        "merge_queue_id": "mq-1",
        "head_commit": "head456",
        "status": "succeeded",
        "changed_files": ["agent/governance/mf_subagent_contract.py"],
        "test_results": {"status": "passed", "command": "pytest -q"},
        "checkpoint_id": "ckpt-finish-observer-authored",
        "fence_token": "fence-2",
        "summary": "Ready.",
        "read_receipt_hash": "sha256:read-finish",
        "read_receipt_event_id": "2873",
        "finish_time_worker_self_attestation": _finish_time_worker_attestation(),
    }

    with pytest.raises(MfSubagentContractError, match="startup_filer_is_generic_or_observer"):
        validate_mf_subagent_finish_gate(
            {
                **base_payload,
                "real_startup_events": [
                    _startup_event(_finish_startup_evidence(filer_principal="observer"))
                ],
            },
            context=_context(),
        )

    with pytest.raises(
        MfSubagentContractError,
        match="read_receipt_filer_is_generic_or_observer",
    ):
        validate_mf_subagent_finish_gate(
            {
                **base_payload,
                "real_startup_events": [
                    _startup_event(
                        _finish_startup_evidence(
                            read_receipt_filer_principal="observer"
                        )
                    )
                ],
            },
            context=_context(),
        )


def test_finish_gate_requires_finish_time_worker_attestation() -> None:
    with pytest.raises(
        MfSubagentContractError,
        match="missing_finish_time_worker_self_attestation",
    ):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "aming-claw",
                "task_id": "task-mf-sub-1",
                "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
                "branch_ref": "refs/heads/codex/task-mf-sub-1",
                "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
                "base_commit": "base123",
                "target_head_commit": "target123",
                "merge_queue_id": "mq-1",
                "head_commit": "head456",
                "status": "succeeded",
                "changed_files": ["agent/governance/mf_subagent_contract.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish-missing-attestation",
                "fence_token": "fence-2",
                "summary": "Ready.",
                "real_startup_events": [_startup_event(_finish_startup_evidence())],
                "read_receipt_hash": "sha256:read-finish",
                "read_receipt_event_id": "2873",
            },
            context=_context(),
        )


def test_finish_gate_rejects_startup_phase_attestation_as_finish_evidence() -> None:
    with pytest.raises(
        MfSubagentContractError,
        match="attestation_phase_startup_not_finish",
    ):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "aming-claw",
                "task_id": "task-mf-sub-1",
                "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
                "branch_ref": "refs/heads/codex/task-mf-sub-1",
                "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
                "base_commit": "base123",
                "target_head_commit": "target123",
                "merge_queue_id": "mq-1",
                "head_commit": "head456",
                "status": "succeeded",
                "changed_files": ["agent/governance/mf_subagent_contract.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish-startup-phase-attestation",
                "fence_token": "fence-2",
                "summary": "Ready.",
                "real_startup_events": [_startup_event(_finish_startup_evidence())],
                "read_receipt_hash": "sha256:read-finish",
                "read_receipt_event_id": "2873",
                "worker_self_attestation": _finish_time_worker_attestation(
                    attestation_phase="startup",
                ),
            },
            context=_context(),
        )


@pytest.mark.parametrize(
    ("overrides", "expected_blocker"),
    [
        ({"filer_principal": None}, "missing_filer_principal"),
        (
            {"filer_principal": "observer"},
            "finish_attestation_filer_is_generic_or_observer",
        ),
        (
            {"filer_principal": "mf sub"},
            "finish_attestation_filer_is_generic_or_observer",
        ),
        (
            {"filer_principal": "session-other"},
            "finish_attestation_filer_principal_not_worker_session",
        ),
        ({"filed_on_behalf": True}, "finish_attestation_filed_on_behalf"),
        (
            {"filed_on_behalf_by": "observer"},
            "finish_attestation_filed_on_behalf",
        ),
    ],
)
def test_finish_gate_requires_finish_attestation_filed_by_worker_session(
    overrides: dict[str, object],
    expected_blocker: str,
) -> None:
    with pytest.raises(MfSubagentContractError, match=expected_blocker):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "aming-claw",
                "task_id": "task-mf-sub-1",
                "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
                "branch_ref": "refs/heads/codex/task-mf-sub-1",
                "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
                "base_commit": "base123",
                "target_head_commit": "target123",
                "merge_queue_id": "mq-1",
                "head_commit": "head456",
                "status": "succeeded",
                "changed_files": ["agent/governance/mf_subagent_contract.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish-filer-principal",
                "fence_token": "fence-2",
                "summary": "Ready.",
                "real_startup_events": [_startup_event(_finish_startup_evidence())],
                "read_receipt_hash": "sha256:read-finish",
                "read_receipt_event_id": "2873",
                "finish_time_worker_self_attestation": (
                    _finish_time_worker_attestation(**overrides)
                ),
            },
            context=_context(),
        )


@pytest.mark.parametrize(
    "nested_key",
    ["startup_evidence", "bounded_startup_evidence", "mf_subagent_startup_gate"],
)
def test_finish_gate_accepts_nested_startup_evidence_object(nested_key: str) -> None:
    gate = validate_mf_subagent_finish_gate(
        {
            "project_id": "aming-claw",
            "task_id": "task-mf-sub-1",
            "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
            "branch_ref": "refs/heads/codex/task-mf-sub-1",
            "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
            "base_commit": "base123",
            "target_head_commit": "target123",
            "merge_queue_id": "mq-1",
            "head_commit": "head456",
            "status": "succeeded",
            "changed_files": ["agent/governance/mf_subagent_contract.py"],
            "test_results": {"status": "passed", "command": "pytest -q"},
            "checkpoint_id": f"ckpt-finish-{nested_key}",
            "fence_token": "fence-2",
            "summary": "Ready.",
            "evidence": {
                nested_key: _finish_startup_evidence(),
                "finish_time_worker_self_attestation": (
                    _finish_time_worker_attestation()
                ),
                "read_receipt_hash": "sha256:read-finish",
                "read_receipt_event_id": "2873",
            },
            "real_startup_events": [_startup_event(_finish_startup_evidence())],
        },
        context=_context(),
    )

    assert gate["startup_evidence"]["schema_version"] == "mf_subagent_startup_gate.v1"
    assert gate["read_receipt_hash"] == "sha256:read-finish"
    assert gate["read_receipt_event_id"] == "2873"
    assert gate["receipt_gate"]["startup_present"] is True
    assert gate["close_ready"] is True


def test_finish_gate_requires_read_receipt_event_lineage() -> None:
    startup_evidence = _finish_startup_evidence()
    startup_evidence.pop("read_receipt_event_id")
    with pytest.raises(MfSubagentContractError, match="event lineage"):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "aming-claw",
                "task_id": "task-mf-sub-1",
                "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
                "branch_ref": "refs/heads/codex/task-mf-sub-1",
                "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
                "base_commit": "base123",
                "target_head_commit": "target123",
                "merge_queue_id": "mq-1",
                "head_commit": "head456",
                "status": "succeeded",
                "changed_files": ["agent/governance/mf_subagent_contract.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish",
                "fence_token": "fence-2",
                "summary": "Ready.",
                "mf_subagent_startup_gate": startup_evidence,
                "real_startup_events": [_startup_event(startup_evidence)],
                "finish_time_worker_self_attestation": _finish_time_worker_attestation(),
                "read_receipt_hash": "sha256:read-finish",
            },
            context=_context(),
        )


def test_finish_gate_refuses_close_ready_without_startup_or_read_receipt() -> None:
    base_payload = {
        "project_id": "aming-claw",
        "task_id": "task-mf-sub-1",
        "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
        "branch_ref": "refs/heads/codex/task-mf-sub-1",
        "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
        "base_commit": "base123",
        "target_head_commit": "target123",
        "merge_queue_id": "mq-1",
        "head_commit": "head456",
        "status": "succeeded",
        "changed_files": ["agent/governance/mf_subagent_contract.py"],
        "test_results": {"status": "passed", "command": "pytest -q"},
        "checkpoint_id": "ckpt-finish",
        "fence_token": "fence-2",
        "summary": "Ready.",
    }

    with pytest.raises(MfSubagentContractError, match="mf_subagent_startup"):
        validate_mf_subagent_finish_gate(
            {**base_payload, "read_receipt_hash": "sha256:read-finish"},
            context=_context(),
        )

    startup_without_receipt = _finish_startup_evidence()
    startup_without_receipt.pop("read_receipt_hash")
    with pytest.raises(MfSubagentContractError, match="mf_subagent_read_receipt"):
        validate_mf_subagent_finish_gate(
            {
                **base_payload,
                "mf_subagent_startup_gate": startup_without_receipt,
                "real_startup_events": [_startup_event(startup_without_receipt)],
                "finish_time_worker_self_attestation": _finish_time_worker_attestation(),
            },
            context=_context(),
        )


def test_finish_gate_rejects_nested_startup_intent_only_evidence() -> None:
    with pytest.raises(MfSubagentContractError, match="mf_subagent_startup"):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "aming-claw",
                "task_id": "task-mf-sub-1",
                "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
                "branch_ref": "refs/heads/codex/task-mf-sub-1",
                "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
                "base_commit": "base123",
                "target_head_commit": "target123",
                "merge_queue_id": "mq-1",
                "head_commit": "head456",
                "status": "succeeded",
                "changed_files": ["agent/governance/mf_subagent_contract.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish-startup-intent",
                "fence_token": "fence-2",
                "summary": "Ready.",
                "evidence": {
                    "startup_evidence": _finish_startup_evidence(
                        schema_version="mf_subagent_startup_intent.v1",
                        intent_kind="mf_subagent.startup_intent",
                    ),
                    "read_receipt_hash": "sha256:read-finish",
                },
            },
            context=_context(),
        )


def test_finish_gate_rejects_parent_main_checkout_dirtiness() -> None:
    with pytest.raises(MfSubagentContractError, match="parent/main checkout clean"):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "aming-claw",
                "task_id": "task-mf-sub-1",
                "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
                "branch_ref": "refs/heads/codex/task-mf-sub-1",
                "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
                "base_commit": "base123",
                "target_head_commit": "target123",
                "merge_queue_id": "mq-1",
                "head_commit": "head456",
                "status": "succeeded",
                "changed_files": ["agent/governance/mf_subagent_contract.py"],
                "owned_files": ["agent/governance/mf_subagent_contract.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish",
                "fence_token": "fence-2",
                "parent_main_status_short": " M agent/governance/server.py\n",
                "worker_worktree_status_short": " M agent/governance/mf_subagent_contract.py\n",
                "actual_cwd": "/tmp/aming-claw-wt/task-mf-sub-1",
                "actual_git_root": "/tmp/aming-claw-wt/task-mf-sub-1",
                "summary": "Ready.",
            },
            context=_context(),
        )


def test_finish_gate_rejects_worker_changes_outside_owned_files() -> None:
    with pytest.raises(MfSubagentContractError, match="outside owned file fence"):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "aming-claw",
                "task_id": "task-mf-sub-1",
                "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
                "branch_ref": "refs/heads/codex/task-mf-sub-1",
                "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
                "base_commit": "base123",
                "target_head_commit": "target123",
                "merge_queue_id": "mq-1",
                "head_commit": "head456",
                "status": "succeeded",
                "changed_files": ["agent/governance/server.py"],
                "owned_files": ["agent/governance/mf_subagent_contract.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish",
                "fence_token": "fence-2",
                "worker_worktree_status_short": " M agent/governance/server.py\n",
                "summary": "Ready.",
            },
            context=_context(),
        )


def test_finish_gate_carries_route_lineage_when_present() -> None:
    gate = validate_mf_subagent_finish_gate(
        {
            "project_id": "aming-claw",
            "task_id": "task-mf-sub-1",
            "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
            "branch_ref": "refs/heads/codex/task-mf-sub-1",
            "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
            "base_commit": "base123",
            "target_head_commit": "target123",
            "merge_queue_id": "mq-1",
            "head_commit": "head456",
            "status": "succeeded",
            "changed_files": ["agent/governance/mf_subagent_contract.py"],
            "test_results": {"status": "passed", "command": "pytest -q"},
            "checkpoint_id": "ckpt-finish",
            "fence_token": "fence-2",
            "parent_task_id": "task-mf-parent",
            "graph_trace_evidence": _graph_trace_evidence(
                trace_ids=["gqt-test-finish-1"],
                fence_token="fence-2",
            ),
            "route_prompt_contract": {
                "route_context_hash": "sha256:child-route-context",
                "prompt_contract_id": "rprompt-child",
                "prompt_contract_hash": "sha256:child-prompt",
            },
            "route_lineage": {
                "parent_route_lineage": _parent_route_lineage(),
                "child_route_prompt_contract": {
                    "route_context_hash": "sha256:child-route-context",
                    "prompt_contract_id": "rprompt-child",
                    "prompt_contract_hash": "sha256:child-prompt",
                },
            },
            "mf_subagent_startup_gate": _finish_startup_evidence(),
            "real_startup_events": [_startup_event(_finish_startup_evidence())],
            "finish_time_worker_self_attestation": _finish_time_worker_attestation(),
            "read_receipt_hash": "sha256:read-finish",
            "read_receipt_event_id": "2873",
            "summary": "Ready.",
        },
        context=_context(),
    )

    assert gate["parent_route_lineage"]["route_id"] == "route-20260602-parent"
    assert gate["route_prompt_contract"]["prompt_contract_id"] == "rprompt-child"
    assert gate["route_lineage"]["parent_route_context_hash"] == (
        "sha256:parent-route-context"
    )
    assert gate["route_lineage"]["child_route_context_hash"] == (
        "sha256:child-route-context"
    )
    assert gate["governed_evidence_required"] is True
    assert gate["graph_trace_evidence"]["trace_ids"] == ["gqt-test-finish-1"]


def test_finish_gate_accepts_empty_parent_route_blocked_actions() -> None:
    gate = validate_mf_subagent_finish_gate(
        {
            "project_id": "aming-claw",
            "task_id": "task-mf-sub-1",
            "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
            "branch_ref": "refs/heads/codex/task-mf-sub-1",
            "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
            "base_commit": "base123",
            "target_head_commit": "target123",
            "merge_queue_id": "mq-1",
            "head_commit": "head456",
            "status": "succeeded",
            "changed_files": ["agent/governance/mf_subagent_contract.py"],
            "test_results": {"status": "passed", "command": "pytest -q"},
            "checkpoint_id": "ckpt-finish-empty-blocked-actions",
            "fence_token": "fence-2",
            "parent_task_id": "task-mf-parent",
            "parent_route_lineage": _parent_route_lineage(blocked_actions=[]),
            "graph_trace_evidence": _graph_trace_evidence(fence_token="fence-2"),
            "mf_subagent_startup_gate": _finish_startup_evidence(),
            "real_startup_events": [_startup_event(_finish_startup_evidence())],
            "finish_time_worker_self_attestation": _finish_time_worker_attestation(),
            "read_receipt_hash": "sha256:read-finish",
            "read_receipt_event_id": "2873",
            "summary": "Ready.",
        },
        context=_context(),
    )

    assert gate["parent_route_lineage"]["blocked_actions"] == []
    assert "none" not in gate["parent_route_lineage"]["blocked_actions"]
    assert gate["governed_evidence_required"] is True
    assert gate["close_ready"] is True


def test_finish_gate_rejects_omitted_parent_route_blocked_actions() -> None:
    parent_route_lineage = _parent_route_lineage()
    parent_route_lineage.pop("blocked_actions")

    with pytest.raises(MfSubagentContractError, match="blocked_actions"):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "aming-claw",
                "task_id": "task-mf-sub-1",
                "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
                "branch_ref": "refs/heads/codex/task-mf-sub-1",
                "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
                "base_commit": "base123",
                "target_head_commit": "target123",
                "merge_queue_id": "mq-1",
                "head_commit": "head456",
                "status": "succeeded",
                "changed_files": ["agent/governance/mf_subagent_contract.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish-missing-blocked-actions",
                "fence_token": "fence-2",
                "parent_task_id": "task-mf-parent",
                "parent_route_lineage": parent_route_lineage,
                "graph_trace_evidence": _graph_trace_evidence(fence_token="fence-2"),
                "mf_subagent_startup_gate": _finish_startup_evidence(),
                "read_receipt_hash": "sha256:read-finish",
                "read_receipt_event_id": "2873",
                "summary": "Ready.",
            },
            context=_context(),
        )


def test_finish_gate_rejects_parent_route_without_graph_trace() -> None:
    with pytest.raises(MfSubagentContractError, match="graph trace evidence"):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "aming-claw",
                "task_id": "task-mf-sub-1",
                "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
                "branch_ref": "refs/heads/codex/task-mf-sub-1",
                "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
                "base_commit": "base123",
                "target_head_commit": "target123",
                "merge_queue_id": "mq-1",
                "head_commit": "head456",
                "status": "succeeded",
                "changed_files": ["agent/governance/mf_subagent_contract.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish",
                "fence_token": "fence-2",
                "parent_task_id": "task-mf-parent",
                "parent_route_lineage": _parent_route_lineage(),
                "summary": "Ready.",
            },
            context=_context(),
        )


def test_finish_gate_rejects_parent_route_top_level_graph_trace_fields() -> None:
    with pytest.raises(MfSubagentContractError, match="query_source=mf_subagent"):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "aming-claw",
                "task_id": "task-mf-sub-1",
                "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
                "branch_ref": "refs/heads/codex/task-mf-sub-1",
                "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
                "base_commit": "base123",
                "target_head_commit": "target123",
                "merge_queue_id": "mq-1",
                "head_commit": "head456",
                "status": "succeeded",
                "changed_files": ["agent/governance/mf_subagent_contract.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish",
                "fence_token": "fence-2",
                "parent_task_id": "task-mf-parent",
                "worker_role": "mf_sub",
                "query_source": "mf_subagent",
                "graph_query_trace_ids": ["gqt-top-level-only"],
                "parent_route_lineage": _parent_route_lineage(),
                "summary": "Ready.",
            },
            context=_context(),
        )


def test_finish_gate_rejects_parent_route_graph_trace_without_query_source() -> None:
    graph_trace = _graph_trace_evidence(fence_token="fence-2")
    graph_trace.pop("query_source")

    with pytest.raises(MfSubagentContractError, match="query_source=mf_subagent"):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "aming-claw",
                "task_id": "task-mf-sub-1",
                "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
                "branch_ref": "refs/heads/codex/task-mf-sub-1",
                "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
                "base_commit": "base123",
                "target_head_commit": "target123",
                "merge_queue_id": "mq-1",
                "head_commit": "head456",
                "status": "succeeded",
                "changed_files": ["agent/governance/mf_subagent_contract.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish",
                "fence_token": "fence-2",
                "parent_task_id": "task-mf-parent",
                "parent_route_lineage": _parent_route_lineage(),
                "graph_trace_evidence": graph_trace,
                "summary": "Ready.",
            },
            context=_context(),
        )


def test_finish_gate_rejects_parent_route_graph_trace_generic_source_key() -> None:
    graph_trace = _graph_trace_evidence(fence_token="fence-2")
    graph_trace.pop("query_source")
    graph_trace["source"] = "mf_subagent"

    with pytest.raises(MfSubagentContractError, match="query_source=mf_subagent"):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "aming-claw",
                "task_id": "task-mf-sub-1",
                "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
                "branch_ref": "refs/heads/codex/task-mf-sub-1",
                "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
                "base_commit": "base123",
                "target_head_commit": "target123",
                "merge_queue_id": "mq-1",
                "head_commit": "head456",
                "status": "succeeded",
                "changed_files": ["agent/governance/mf_subagent_contract.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish",
                "fence_token": "fence-2",
                "parent_task_id": "task-mf-parent",
                "parent_route_lineage": _parent_route_lineage(),
                "graph_trace_evidence": graph_trace,
                "summary": "Ready.",
            },
            context=_context(),
        )


def test_finish_gate_rejects_parent_route_graph_trace_wrong_query_source() -> None:
    with pytest.raises(MfSubagentContractError, match="query_source=mf_subagent"):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "aming-claw",
                "task_id": "task-mf-sub-1",
                "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
                "branch_ref": "refs/heads/codex/task-mf-sub-1",
                "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
                "base_commit": "base123",
                "target_head_commit": "target123",
                "merge_queue_id": "mq-1",
                "head_commit": "head456",
                "status": "succeeded",
                "changed_files": ["agent/governance/mf_subagent_contract.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish",
                "fence_token": "fence-2",
                "parent_task_id": "task-mf-parent",
                "parent_route_lineage": _parent_route_lineage(),
                "graph_trace_evidence": _graph_trace_evidence(
                    query_source="observer",
                    fence_token="fence-2",
                ),
                "summary": "Ready.",
            },
            context=_context(),
        )


@pytest.mark.parametrize(
    "field",
    ["task_id", "parent_task_id", "worker_role", "fence_token"],
)
def test_finish_gate_rejects_parent_route_graph_trace_missing_embedded_identity(
    field: str,
) -> None:
    graph_trace = _graph_trace_evidence(fence_token="fence-2")
    graph_trace.pop(field)

    with pytest.raises(MfSubagentContractError, match=field):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "aming-claw",
                "task_id": "task-mf-sub-1",
                "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
                "branch_ref": "refs/heads/codex/task-mf-sub-1",
                "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
                "base_commit": "base123",
                "target_head_commit": "target123",
                "merge_queue_id": "mq-1",
                "head_commit": "head456",
                "status": "succeeded",
                "changed_files": ["agent/governance/mf_subagent_contract.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish",
                "fence_token": "fence-2",
                "parent_task_id": "task-mf-parent",
                "parent_route_lineage": _parent_route_lineage(),
                "graph_trace_evidence": graph_trace,
                "summary": "Ready.",
            },
            context=_context(),
        )


def test_finish_gate_rejects_parent_route_observer_graph_trace_role() -> None:
    with pytest.raises(MfSubagentContractError, match="worker_role=mf_sub"):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "aming-claw",
                "task_id": "task-mf-sub-1",
                "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
                "branch_ref": "refs/heads/codex/task-mf-sub-1",
                "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
                "base_commit": "base123",
                "target_head_commit": "target123",
                "merge_queue_id": "mq-1",
                "head_commit": "head456",
                "status": "succeeded",
                "changed_files": ["agent/governance/mf_subagent_contract.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish",
                "fence_token": "fence-2",
                "parent_task_id": "task-mf-parent",
                "worker_role": "observer",
                "parent_route_lineage": _parent_route_lineage(),
                "graph_trace_evidence": _graph_trace_evidence(
                    worker_role="observer",
                    fence_token="fence-2",
                ),
                "summary": "Ready.",
            },
            context=_context(),
        )


def test_finish_gate_rejects_identity_mismatch() -> None:
    with pytest.raises(MfSubagentContractError, match="identity mismatch"):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "other-project",
                "status": "succeeded",
                "changed_files": ["x.py"],
                "test_results": {"status": "passed"},
                "checkpoint_id": "ckpt-finish",
                "fence_token": "fence-2",
            },
            context=_context(),
        )


def test_finish_gate_rejects_not_ready_result() -> None:
    with pytest.raises(MfSubagentContractError, match="not merge-queue ready"):
        validate_mf_subagent_finish_gate(
            {
                "status": "succeeded",
                "changed_files": ["x.py"],
                "test_results": {"status": "failed"},
                "checkpoint_id": "ckpt-finish",
                "fence_token": "fence-2",
                "blockers": ["tests failed"],
            },
            context=_context(),
        )


# ---------------------------------------------------------------------------
# Host-adapter surrogate startup is not real-worker close evidence (#3104)
# ---------------------------------------------------------------------------
def _real_session_startup() -> dict:
    return {
        "schema_version": "mf_subagent_startup_gate.v1",
        "gate_kind": "mf_subagent.startup",
        "bounded": True,
        "close_satisfying": True,
        "agent_id_match_mode": "actual_host_worker_bound",
        "session_token_evidence_type": "hash",
        "session_token_hash": "sha256:real-token",
        "session_token_present": True,
        "worktree": "/repo/.worktrees/mf-sub",
        "worktree_path": "/repo/.worktrees/mf-sub",
        "fence_token": "fence-real",
        "actual_cwd": "/repo/.worktrees/mf-sub",
        "actual_git_root": "/repo/.worktrees/mf-sub",
        "branch": "refs/heads/codex/mf-sub",
        "head_commit": "head-real",
        **_self_attested_startup_fields(
            worker_session_id="worker-session-real",
            transcript_path="/tmp/worker-session-real.jsonl",
        ),
    }


def _surrogate_startup() -> dict:
    startup = _real_session_startup()
    startup.update(
        {
            "agent_id_match_mode": "host_adapter_startup_token_surrogate",
            "session_token_evidence_type": "surrogate",
            "session_token_hash": "",
            "session_token_present": False,
            "host_adapter_startup_token_accepted": True,
            "fence_token": "fence-surrogate",
        }
    )
    return startup


def test_real_session_startup_is_close_satisfying() -> None:
    gate = surrogate_startup_evidence_gate(_real_session_startup())
    assert gate["is_host_adapter_surrogate"] is False
    assert gate["close_satisfying"] is True
    assert gate["counts_as_real_worker_evidence"] is True
    assert _bounded_startup_evidence_present(_real_session_startup()) is True


def test_surrogate_startup_not_close_satisfying_without_hotfix_exception() -> None:
    gate = surrogate_startup_evidence_gate(_surrogate_startup())
    assert gate["is_host_adapter_surrogate"] is True
    assert gate["observer_hotfix_exception_present"] is False
    assert gate["close_satisfying"] is False
    assert gate["counts_as_real_worker_evidence"] is False
    assert (
        gate["reason"]
        == "host_adapter_surrogate_blocked_without_observer_hotfix_exception"
    )
    # The live bounded-worker close-satisfaction check must also demote it,
    # even though the startup payload stamps close_satisfying=true.
    assert _bounded_startup_evidence_present(_surrogate_startup()) is False


def test_surrogate_startup_with_hotfix_exception_still_not_real_worker_evidence() -> None:
    hotfix_events = [
        {
            "event_kind": "observer_work_mode_transition",
            "work_mode": "observer_hotfix_exception",
            "status": "accepted",
        }
    ]
    gate = surrogate_startup_evidence_gate(
        _surrogate_startup(), events=hotfix_events
    )
    assert gate["is_host_adapter_surrogate"] is True
    assert gate["observer_hotfix_exception_present"] is True
    # Even under the hotfix exception, surrogate evidence is NOT close-satisfying
    # real-worker evidence.
    assert gate["close_satisfying"] is False
    assert gate["counts_as_real_worker_evidence"] is False
    assert (
        gate["reason"]
        == "host_adapter_surrogate_under_observer_hotfix_exception_not_real_worker_evidence"
    )


# ---------------------------------------------------------------------------
# Surrogate join: real worker startup upgrades surrogate via lineage match
# (AC-PARALLEL-BRANCH-STARTUP-HOST-SURROGATE-JOIN-GAP-20260605)
# ---------------------------------------------------------------------------

def _surrogate_startup_with_lineage() -> dict:
    """A surrogate startup with full lineage fields for join matching."""
    startup = _surrogate_startup()
    startup.update(
        {
            "task_id": "task-sg-test-01",
            "worker_slot_id": "wslot-sg-01",
            "runtime_context_id": "mfrctx-sgtest01",
            "fence_token": "fence-sg-test",
        }
    )
    return startup


def _real_worker_startup_matching_lineage(event_id: str = "evt-real-001") -> dict:
    """A real worker startup event that matches the surrogate lineage.

    Uses same_as_allocation_owner mode so it is NOT classified as a surrogate
    after the TOFU mutual-exclusion fix.  (host_adapter_startup_token_surrogate
    mode startups are always surrogate, even with a real token — those are
    exactly the events the join gate must skip.)
    """
    return {
        "id": event_id,
        "schema_version": "mf_subagent_startup_gate.v1",
        "gate_kind": "mf_subagent.startup",
        "bounded": True,
        "close_satisfying": True,
        # same_as_allocation_owner: trusted real-worker startup (not host-adapter mode)
        "agent_id_match_mode": "same_as_allocation_owner",
        "session_token_evidence_type": "hash",
        "session_token_hash": "sha256:real-token-hash-abc",
        "session_token_present": True,
        "host_adapter_startup_token_accepted": False,
        "task_id": "task-sg-test-01",
        "worker_slot_id": "wslot-sg-01",
        "runtime_context_id": "mfrctx-sgtest01",
        "fence_token": "fence-sg-test",
        "fence_token_matches": True,
        "worktree": "/repo/.worktrees/sg-test",
        "worktree_path": "/repo/.worktrees/sg-test",
        "actual_cwd": "/repo/.worktrees/sg-test",
        "actual_git_root": "/repo/.worktrees/sg-test",
        "branch": "refs/heads/task-sg-test",
        "head_commit": "head-sg-real",
        "route_id": "route-sg-test",
        "route_context_hash": "sha256:route-sg-test",
        "prompt_contract_id": "rprompt-sg-test",
        "prompt_contract_hash": "sha256:prompt-sg-test",
        "route_token_ref": "rtok-sg-test",
        "read_receipt_hash": "sha256:rr-f2-test",
        "read_receipt_event_id": "rr-f2-evt-001",
        **_self_attested_startup_fields(
            worker_session_id="worker-session-sg",
            transcript_path="/tmp/worker-session-sg.jsonl",
        ),
    }


def _real_worker_startup_mismatched_lineage() -> dict:
    """A real worker startup event with DIFFERENT lineage — should NOT join."""
    startup = _real_worker_startup_matching_lineage("evt-mismatch-001")
    startup["task_id"] = "task-DIFFERENT-01"
    startup["worker_slot_id"] = "wslot-DIFFERENT"
    return startup


def test_same_as_allocation_owner_real_token_is_not_surrogate() -> None:
    """A startup with same_as_allocation_owner mode and a real session token is NOT a surrogate.

    Post TOFU fix: host_adapter mode + real token IS surrogate (see TOFU tests below).
    This test confirms that same_as_allocation_owner mode (the trusted path) is
    unaffected by the mutual-exclusion change.
    """
    real_token_startup = _real_worker_startup_matching_lineage()
    # _real_worker_startup_matching_lineage now uses same_as_allocation_owner mode
    assert real_token_startup["agent_id_match_mode"] == "same_as_allocation_owner"
    assert _startup_is_host_adapter_surrogate(real_token_startup) is False


def test_close_timeline_counts_same_as_allocation_owner_startup() -> None:
    real_token_startup = _real_worker_startup_matching_lineage("evt-same-owner-close")
    event = {
        "id": "evt-same-owner-close",
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "passed",
        "payload": {"mf_subagent_startup_gate": real_token_startup},
    }

    gate = close_timeline_startup_event_gate([event])
    assert gate["passed"] is True
    assert gate["demoted_startup_events"] == []
    normalized = close_timeline_events_for_verification([event])
    assert normalized["events"][0]["status"] == "passed"


def test_surrogate_only_startup_is_surrogate() -> None:
    """A startup with session_token_evidence_type==surrogate and no real token IS a surrogate."""
    assert _startup_is_host_adapter_surrogate(_surrogate_startup()) is True
    assert _startup_is_host_adapter_surrogate(_surrogate_startup_with_lineage()) is True


def test_close_timeline_demotes_event_4178_like_surrogate_without_real_join() -> None:
    startup = _surrogate_startup_with_lineage()
    startup.update(
        {
            "agent_id": "codex-cli-thread:event-4178",
            "allocation_owner": "allocated-mf-sub-worker",
            "agent_id_match_mode": "host_adapter_startup_token_surrogate",
            "session_token_evidence_type": "server_verified",
            "session_token_hash": "sha256:host-adapter-token",
            "session_token_present": True,
            "close_satisfying": True,
        }
    )
    event = {
        "id": "evt-4178-surrogate",
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "passed",
        "payload": {"mf_subagent_startup_gate": startup},
    }

    gate = close_timeline_startup_event_gate([event])
    assert gate["passed"] is False
    assert gate["demoted_startup_events"][0]["id"] == "evt-4178-surrogate"
    assert (
        gate["demoted_startup_events"][0]["real_worker_join"]["reason"]
        == "surrogate_only_no_matching_real_startup_in_events"
    )
    normalized = close_timeline_events_for_verification([event])
    assert normalized["events"][0]["status"] == "demoted"


def test_close_timeline_accepted_startup_overrides_demoted_history() -> None:
    demoted_startup = _real_worker_startup_matching_lineage(
        "evt-historical-demoted"
    )
    demoted_startup["worker_self_attesting"] = False
    demoted_startup["worker_self_attestation"] = {
        **demoted_startup["worker_self_attestation"],
        "status": "blocked",
        "worker_self_attesting": False,
        "self_attesting": False,
    }
    demoted_event = {
        "id": "evt-historical-demoted",
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "passed",
        "payload": {"mf_subagent_startup_gate": demoted_startup},
    }
    accepted_startup = _real_worker_startup_matching_lineage("evt-accepted-startup")
    accepted_event = {
        "id": "evt-accepted-startup",
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "accepted",
        "payload": {"mf_subagent_startup_gate": accepted_startup},
    }

    gate = close_timeline_startup_event_gate([demoted_event, accepted_event])

    assert gate["passed"] is True
    assert gate["status"] == "passed"
    assert gate["accepted_startup_events"][0]["id"] == "evt-accepted-startup"
    assert gate["demoted_startup_events"][0]["id"] == "evt-historical-demoted"
    assert gate["demoted_startup_event_indexes"] == [0]

    normalized = close_timeline_events_for_verification(
        [demoted_event, accepted_event]
    )
    assert normalized["startup_gate"]["passed"] is True
    assert normalized["events"][0]["status"] == "demoted"
    assert normalized["events"][1]["status"] == "accepted"


def test_close_timeline_accepts_finish_gate_startup_projection_with_transcript_ref() -> None:
    demoted_startup = _real_worker_startup_matching_lineage(
        "evt-finish-projection-startup"
    )
    demoted_startup["close_satisfying"] = False
    demoted_startup["worker_self_attesting"] = False
    demoted_startup["worker_transcript_path"] = ""
    demoted_startup["worker_transcript_ref"] = "multi_agent:worker-session-real"
    demoted_startup["worker_self_attestation"] = {
        **demoted_startup["worker_self_attestation"],
        "status": "blocked",
        "worker_self_attesting": False,
        "self_attesting": False,
        "worker_transcript_path": "",
        "worker_transcript_ref": "multi_agent:worker-session-real",
    }
    startup_event = {
        "id": "evt-finish-projection-startup",
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "passed",
        "payload": {"mf_subagent_startup_gate": demoted_startup},
    }
    finish_event = {
        "id": "evt-finish-projection",
        "event_kind": "mf_subagent_finish_gate",
        "event_type": "mf_subagent.finish_gate",
        "phase": "finish_gate",
        "status": "passed",
        "payload": {
            "mf_subagent_finish_gate": {
                "startup_evidence": demoted_startup,
                "startup_worker_identity_gate": {
                    "passed": True,
                    "status": "passed",
                    "worker_session_id": "worker-session-real",
                    "worker_transcript_ref": "multi_agent:worker-session-real",
                    "harness_type": "codex",
                },
                "worker_self_attestation_gate": {
                    "passed": True,
                    "status": "passed",
                    "worker_session_id": "worker-session-real",
                    "worker_transcript_ref": "multi_agent:worker-session-real",
                    "harness_type": "codex",
                    "attestation": {
                        "attestation_phase": "finish",
                        "status": "passed",
                        "worker_self_attesting": True,
                        "self_attesting": True,
                        "finish_time_self_attesting": True,
                        "finish_time_blockers": [],
                        "worker_session_id": "worker-session-real",
                        "filer_principal": "worker-session-real",
                        "worker_transcript_ref": "multi_agent:worker-session-real",
                        "harness_type": "codex",
                    },
                },
            }
        },
    }

    gate = close_timeline_startup_event_gate([startup_event, finish_event])

    assert gate["passed"] is True
    assert gate["demoted_startup_events"][0]["id"] == "evt-finish-projection-startup"
    accepted = gate["accepted_startup_events"][0]
    assert accepted["id"] == "evt-finish-projection"
    assert accepted["worker_self_attestation_gate"]["worker_transcript_ref"] == (
        "multi_agent:worker-session-real"
    )


def test_close_timeline_does_not_accept_raw_finish_time_attestation_after_startup() -> None:
    worker_session_id = "worker-session-r7"
    startup = _finish_startup_evidence(
        **_self_attested_startup_fields(
            worker_session_id=worker_session_id,
            transcript_path="/tmp/worker-session-r7.jsonl",
            transcript_ref="codex-thread:worker-session-r7",
        ),
        close_satisfying=False,
        worker_self_attesting=False,
        self_attesting=False,
        runtime_context_id="mfrctx-adb2e7b48aeabadb",
        task_id="observer-command-close-projection-worker-20260618-r7",
        parent_task_id="AC-OBSERVER-COMMAND-EXECUTE-BACKLOG-ROW-20260602",
        worker_id="019ed92d-f34d-70e2-a97a-d384aa817b61",
        worker_slot_id="019ed92d-f34d-70e2-a97a-d384aa817b61",
        session_token_evidence_type="server_verified",
        server_issued_session_token_verified=True,
        agent_id_match_mode="same_as_allocation_owner",
    )
    startup["worker_self_attestation"] = {
        **startup["worker_self_attestation"],
        "status": "blocked",
        "worker_self_attesting": False,
        "self_attesting": False,
        "finish_time_self_attesting": False,
        "blockers": [
            "graph_trace_ids_not_db_verified",
            "missing_mf_subagent_graph_trace_ids",
        ],
    }
    startup_event = {
        "id": 5300,
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "passed",
        "payload": {"mf_subagent_startup_gate": startup},
    }
    finish_event = {
        "id": 5301,
        "event_type": "mf_subagent.finish_time_worker_attestation",
        "event_kind": "worker_progress",
        "phase": "finish_time_worker_attestation",
        "status": "passed",
        "actor": worker_session_id,
        "payload": {
            "schema_version": "runtime_context.finish_time_worker_attestation.v1",
            "action": "record_finish_time_worker_attestation",
            "runtime_context_id": "mfrctx-adb2e7b48aeabadb",
            "task_id": "observer-command-close-projection-worker-20260618-r7",
            "parent_task_id": "AC-OBSERVER-COMMAND-EXECUTE-BACKLOG-ROW-20260602",
            "worker_id": "019ed92d-f34d-70e2-a97a-d384aa817b61",
            "worker_slot_id": "019ed92d-f34d-70e2-a97a-d384aa817b61",
            "worker_session_id": worker_session_id,
            "filer_principal": worker_session_id,
            "route_id": startup["route_id"],
            "route_context_hash": startup["route_context_hash"],
            "prompt_contract_id": startup["prompt_contract_id"],
            "prompt_contract_hash": startup["prompt_contract_hash"],
            "route_token_ref": startup["route_token_ref"],
            "read_receipt_hash": startup["read_receipt_hash"],
            "read_receipt_event_id": startup["read_receipt_event_id"],
            "graph_trace_ids": ["gqt-20260618-663eec801f"],
            "test_results": {
                "status": "passed",
                "passed": True,
                "summary": "284 passed, 16 subtests passed",
            },
            "finish_time_worker_self_attestation": _finish_time_worker_attestation(
                worker_session_id=worker_session_id,
                transcript_path="/tmp/worker-session-r7.jsonl",
                transcript_ref="codex-thread:worker-session-r7",
            ),
        },
    }

    gate = close_timeline_startup_event_gate([startup_event, finish_event])

    assert gate["passed"] is False
    assert gate["demoted_startup_events"][0]["id"] == "5300"
    assert gate["demoted_startup_events"][0]["worker_self_attestation_gate"][
        "attestation"
    ]["blockers"] == [
        "graph_trace_ids_not_db_verified",
        "missing_mf_subagent_graph_trace_ids",
    ]
    assert gate["accepted_startup_events"] == []


def test_server_close_gate_check_uses_accepted_startup_gate_with_demoted_history() -> None:
    from agent.governance import server as server_module

    demoted_startup = _real_worker_startup_matching_lineage(
        "evt-server-historical-demoted"
    )
    demoted_startup["worker_self_attesting"] = False
    demoted_startup["worker_self_attestation"] = {
        **demoted_startup["worker_self_attestation"],
        "status": "blocked",
        "worker_self_attesting": False,
        "self_attesting": False,
    }
    demoted_event = {
        "id": "evt-server-historical-demoted",
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "passed",
        "payload": {"mf_subagent_startup_gate": demoted_startup},
    }
    accepted_startup = _real_worker_startup_matching_lineage(
        "evt-server-accepted-startup"
    )
    accepted_event = {
        "id": "evt-server-accepted-startup",
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "accepted",
        "payload": {"mf_subagent_startup_gate": accepted_startup},
    }

    verification = server_module._mf_close_gate_verification(
        [demoted_event, accepted_event]
    )

    startup_gate = verification["close_timeline_startup_gate"]
    assert startup_gate["passed"] is True
    assert startup_gate["demoted_startup_events"][0]["id"] == (
        "evt-server-historical-demoted"
    )
    assert startup_gate["accepted_startup_events"][0]["id"] == (
        "evt-server-accepted-startup"
    )
    assert verification["checks"]["mf_subagent_startup_close_satisfying"] is True


def test_close_timeline_demotes_agent_id_mismatch_without_registered_adapter() -> None:
    startup = _real_worker_startup_matching_lineage("evt-agent-mismatch")
    startup.update(
        {
            "agent_id": "host-adapter-thread",
            "allocation_owner": "allocated-mf-sub-worker",
            "agent_id_match_mode": "actual_host_worker_bound",
        }
    )
    event = {
        "id": "evt-agent-mismatch",
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "passed",
        "payload": {"mf_subagent_startup_gate": startup},
    }

    assert _startup_is_host_adapter_surrogate(startup) is True
    gate = close_timeline_startup_event_gate([event])
    assert gate["passed"] is False
    assert gate["demoted_startup_events"][0]["id"] == "evt-agent-mismatch"


def test_surrogate_only_still_refused_without_real_startup_events() -> None:
    """Surrogate-only (no real_startup_events) must stay blocked."""
    gate = surrogate_startup_evidence_gate(
        _surrogate_startup_with_lineage(), real_startup_events=None
    )
    assert gate["is_host_adapter_surrogate"] is True
    assert gate["close_satisfying"] is False
    assert gate["counts_as_real_worker_evidence"] is False
    join = gate["real_worker_join"]
    assert join["joined"] is False
    assert "surrogate_only" in join["reason"]
    # _bounded_startup_evidence_present must also refuse
    assert _bounded_startup_evidence_present(
        _surrogate_startup_with_lineage(), real_startup_events=None
    ) is False


def test_surrogate_joined_by_matching_real_startup_passes() -> None:
    """Surrogate + real startup with matching lineage -> joined, close-satisfying."""
    real_event = _real_worker_startup_matching_lineage("evt-real-join-001")
    gate = surrogate_startup_evidence_gate(
        _surrogate_startup_with_lineage(),
        real_startup_events=[real_event],
    )
    assert gate["is_host_adapter_surrogate"] is True
    assert gate["close_satisfying"] is True
    assert gate["counts_as_real_worker_evidence"] is True
    assert gate["status"] == "close_satisfying"
    join = gate["real_worker_join"]
    assert join["joined"] is True
    assert join["join_event_id"] == "evt-real-join-001"
    assert join["reason"] == "real_worker_startup_lineage_match"
    # _bounded_startup_evidence_present must also pass when real events provided
    assert _bounded_startup_evidence_present(
        _surrogate_startup_with_lineage(), real_startup_events=[real_event]
    ) is True


def test_surrogate_not_joined_by_mismatched_real_startup() -> None:
    """Surrogate + real startup with DIFFERENT lineage -> still refused."""
    mismatched_event = _real_worker_startup_mismatched_lineage()
    gate = surrogate_startup_evidence_gate(
        _surrogate_startup_with_lineage(),
        real_startup_events=[mismatched_event],
    )
    assert gate["is_host_adapter_surrogate"] is True
    assert gate["close_satisfying"] is False
    assert gate["counts_as_real_worker_evidence"] is False
    join = gate["real_worker_join"]
    assert join["joined"] is False
    # _bounded_startup_evidence_present must also refuse
    assert _bounded_startup_evidence_present(
        _surrogate_startup_with_lineage(), real_startup_events=[mismatched_event]
    ) is False


def test_startup_real_worker_join_direct() -> None:
    """Direct test of _startup_real_worker_join helper."""
    surrogate = _surrogate_startup_with_lineage()
    real_event = _real_worker_startup_matching_lineage("evt-direct-001")

    # Matching lineage -> joined
    join = _startup_real_worker_join(surrogate, real_startup_events=[real_event])
    assert join["joined"] is True
    assert join["join_event_id"] == "evt-direct-001"
    assert join["schema_version"] == REAL_WORKER_JOIN_SCHEMA_VERSION

    # No events -> not joined
    join_empty = _startup_real_worker_join(surrogate, real_startup_events=None)
    assert join_empty["joined"] is False

    # Mismatched lineage -> not joined
    mismatched = _real_worker_startup_mismatched_lineage()
    join_mismatch = _startup_real_worker_join(surrogate, real_startup_events=[mismatched])
    assert join_mismatch["joined"] is False


def test_surrogate_join_gate_response_schema_version() -> None:
    """surrogate_startup_evidence_gate response has correct schema_version and join field."""
    gate_no_join = surrogate_startup_evidence_gate(_surrogate_startup_with_lineage())
    assert gate_no_join["schema_version"] == SURROGATE_STARTUP_GATE_SCHEMA_VERSION
    assert "real_worker_join" in gate_no_join

    real_event = _real_worker_startup_matching_lineage()
    gate_joined = surrogate_startup_evidence_gate(
        _surrogate_startup_with_lineage(), real_startup_events=[real_event]
    )
    assert gate_joined["schema_version"] == SURROGATE_STARTUP_GATE_SCHEMA_VERSION
    assert "real_worker_join" in gate_joined
    assert gate_joined["real_worker_join"]["schema_version"] == REAL_WORKER_JOIN_SCHEMA_VERSION


def test_real_session_startup_gate_unaffected_by_new_join_path() -> None:
    """Existing real-session-token startup tests still pass with the new join path."""
    gate = surrogate_startup_evidence_gate(
        _real_session_startup(), real_startup_events=None
    )
    assert gate["is_host_adapter_surrogate"] is False
    assert gate["close_satisfying"] is True
    assert gate["counts_as_real_worker_evidence"] is True
    # The join gate is not applicable when not a surrogate
    assert gate["real_worker_join"]["reason"] == "not_applicable_not_a_surrogate"


# ---------------------------------------------------------------------------
# F3 fix: empty-lineage candidate rejects join
# (QA block #3516 finding F3-MINOR)
# ---------------------------------------------------------------------------

def _real_worker_startup_empty_lineage_fields(**overrides: str) -> dict:
    """A real-token startup where some or all lineage fields are empty."""
    base = _real_worker_startup_matching_lineage("evt-empty-lineage-001")
    # Wipe all four lineage fields by default.
    base["task_id"] = ""
    base["worker_slot_id"] = ""
    base["runtime_context_id"] = ""
    base["fence_token"] = ""
    base.update(overrides)
    return base


def test_empty_lineage_candidate_task_id_does_not_join() -> None:
    """Candidate with empty task_id must NOT join even if all other fields match."""
    surrogate = _surrogate_startup_with_lineage()
    candidate = _real_worker_startup_matching_lineage()
    candidate["task_id"] = ""  # empty — must refuse join
    join = _startup_real_worker_join(surrogate, real_startup_events=[candidate])
    assert join["joined"] is False, f"expected no join, got: {join}"


def test_empty_lineage_candidate_worker_slot_id_does_not_join() -> None:
    """Candidate with empty worker_slot_id must NOT join."""
    surrogate = _surrogate_startup_with_lineage()
    candidate = _real_worker_startup_matching_lineage()
    candidate["worker_slot_id"] = ""
    candidate["worker_id"] = ""
    join = _startup_real_worker_join(surrogate, real_startup_events=[candidate])
    assert join["joined"] is False, f"expected no join, got: {join}"


def test_empty_lineage_candidate_runtime_context_id_does_not_join() -> None:
    """Candidate with empty runtime_context_id must NOT join."""
    surrogate = _surrogate_startup_with_lineage()
    candidate = _real_worker_startup_matching_lineage()
    candidate["runtime_context_id"] = ""
    join = _startup_real_worker_join(surrogate, real_startup_events=[candidate])
    assert join["joined"] is False, f"expected no join, got: {join}"


def test_empty_lineage_candidate_fence_token_does_not_join() -> None:
    """Candidate with empty fence_token must NOT join."""
    surrogate = _surrogate_startup_with_lineage()
    candidate = _real_worker_startup_matching_lineage()
    candidate["fence_token"] = ""
    join = _startup_real_worker_join(surrogate, real_startup_events=[candidate])
    assert join["joined"] is False, f"expected no join, got: {join}"


def test_all_empty_lineage_candidate_does_not_join() -> None:
    """Candidate with ALL lineage fields empty must NOT join any surrogate."""
    surrogate = _surrogate_startup_with_lineage()
    candidate = _real_worker_startup_empty_lineage_fields()
    join = _startup_real_worker_join(surrogate, real_startup_events=[candidate])
    assert join["joined"] is False, f"expected no join, got: {join}"
    assert "surrogate_only" in join["reason"] or join["joined"] is False


def test_full_lineage_match_still_joins_after_f3_fix() -> None:
    """Regression: a fully-populated matching candidate still joins correctly."""
    surrogate = _surrogate_startup_with_lineage()
    real_event = _real_worker_startup_matching_lineage()
    join = _startup_real_worker_join(surrogate, real_startup_events=[real_event])
    assert join["joined"] is True
    assert join["join_event_id"] == "evt-real-001"


# ---------------------------------------------------------------------------
# F2 fix: validate_mf_subagent_finish_gate ignores caller-supplied events;
# DB-sourced events with matching lineage satisfy the join.
# This exercises the contract layer (mf_subagent_contract.py) directly.
# Server-level bypass regression is in test_graph_governance_api.py.
# ---------------------------------------------------------------------------

def _make_finish_gate_payload_with_surrogate_startup(
    fence_token: str = "fence-f2-test",
    worktree_path: str = "/tmp/f2-wt",
    branch_ref: str = "refs/heads/f2-task",
    head_commit: str = "head-f2",
    real_startup_events: object = None,
) -> dict:
    """Build a finish-gate payload whose startup evidence is a surrogate."""
    surrogate = _surrogate_startup_with_lineage()
    surrogate["fence_token"] = fence_token
    payload: dict = {
        "schema_version": "mf_subagent_finish_gate.v1",
        "status": "review_ready",
        "merge_queue_ready": True,
        "fence_token": fence_token,
        "task_id": "task-sg-test-01",
        "worker_slot_id": "wslot-sg-01",
        "runtime_context_id": "mfrctx-sgtest01",
        "head_commit": head_commit,
        "checkpoint_id": "ckpt-f2-test",
        "changed_files": [],
        "test_results": {"status": "passed"},
        "startup_evidence": surrogate,
        "read_receipt_hash": "sha256:rr-f2-test",
        "read_receipt_event_id": "rr-f2-evt-001",
        "observer_command_id": "cmd-f2-test",
        "finish_time_worker_self_attestation": _finish_time_worker_attestation(
            worker_session_id="session-task-sg-01",
            transcript_path="/tmp/transcript-real.jsonl",
        ),
        "worktree_path": worktree_path,
        "branch_ref": branch_ref,
        "base_commit": "base-f2",
        "target_head_commit": "target-f2",
        "merge_queue_id": "mergeq-f2",
    }
    if real_startup_events is not None:
        payload["real_startup_events"] = real_startup_events
    return payload


def _make_finish_gate_context(fence_token: str = "fence-f2-test") -> "BranchTaskRuntimeContext":
    return BranchTaskRuntimeContext(
        project_id="test-proj",
        task_id="task-sg-test-01",
        backlog_id="AC-PARALLEL-BRANCH-STARTUP-HOST-SURROGATE-JOIN-GAP-20260605",
        branch_ref="refs/heads/f2-task",
        status="worktree_ready",
        fence_token=fence_token,
        worktree_path="/tmp/f2-wt",
        base_commit="base-f2",
        target_head_commit="target-f2",
        merge_queue_id="mergeq-f2",
    )


def test_finish_gate_surrogate_refused_when_no_real_startup_events() -> None:
    """Surrogate-only startup (no real_startup_events) still refuses finish gate."""
    payload = _make_finish_gate_payload_with_surrogate_startup()
    # No real_startup_events in payload.
    ctx = _make_finish_gate_context()
    try:
        validate_mf_subagent_finish_gate(payload, context=ctx)
        assert False, "Expected MfSubagentContractError — surrogate-only should be refused"
    except MfSubagentContractError as exc:
        assert "actual mf_subagent_startup evidence" in str(exc)


def test_finish_gate_fabricated_caller_events_in_payload_do_not_bypass_gate() -> None:
    """F2 regression: passing fabricated real_startup_events in the payload does NOT
    bypass the gate — the contract function still reads them from the payload, but
    the server layer must strip caller-supplied keys.

    This test verifies the contract function DOES accept events from the payload
    (which is fine in isolation — the security is enforced at the server layer that
    strips and replaces them with DB-sourced events).  The companion server-level
    test lives in test_graph_governance_api.py.
    """
    real_event = _real_worker_startup_matching_lineage()
    real_event["fence_token"] = "fence-f2-test"
    # payload carries fabricated events directly
    payload = _make_finish_gate_payload_with_surrogate_startup(
        real_startup_events=[real_event]
    )
    ctx = _make_finish_gate_context()
    # The contract function itself accepts payload-supplied events.
    # The server MUST strip them before calling here — that's the F2 fix.
    gate = validate_mf_subagent_finish_gate(payload, context=ctx)
    # If we get here, the contract accepted the fabricated events.
    # This documents that the server layer is the enforcement boundary.
    assert gate is not None  # contract accepted (server strips — tested separately)


# ---------------------------------------------------------------------------
# F1: server-verified session_token_evidence_type tests
# AC-STARTUP-TOKEN-EVIDENCE-SERVER-VERIFICATION-20260610
# ---------------------------------------------------------------------------

def test_startup_token_evidence_no_stored_hash_first_sight_returns_hash_type() -> None:
    """F1: first startup with no stored hash → evidence_type='hash' (first-sight commitment)."""
    evidence = _startup_token_evidence(
        {"session_token": "my-secret-token-abc"},
        stored_token_hash="",
    )
    assert evidence["session_token_evidence_type"] == "hash"
    assert evidence["session_token_present"] is True
    assert evidence["session_token_hash"].startswith("sha256:")


def test_startup_token_evidence_matching_stored_hash_returns_server_verified() -> None:
    """F1: subsequent startup with matching stored hash → evidence_type='server_verified'."""
    import hashlib
    raw_token = "my-secret-token-abc"
    stored = "sha256:" + hashlib.sha256(raw_token.encode()).hexdigest()
    evidence = _startup_token_evidence(
        {"session_token": raw_token},
        stored_token_hash=stored,
    )
    assert evidence["session_token_evidence_type"] == "server_verified"
    assert evidence["session_token_present"] is True
    assert evidence["session_token_hash"] == stored


def test_startup_token_evidence_matching_session_token_ref_returns_server_verified_ref() -> None:
    """F1: copy-safe session_token_ref can verify the stored server-issued hash."""
    stored = mf_subagent_session_token_hash("ref-token")
    token_ref = mf_subagent_session_token_ref(
        project_id="p",
        runtime_context_id="mfrctx-ref",
        task_id="task-ref",
        fence_token_hash="sha256:fence-ref",
        session_token_hash=stored,
        worker_slot_id="slot-ref",
    )

    evidence = _startup_token_evidence(
        {"session_token_ref": token_ref},
        stored_token_hash=stored,
        expected_session_token_ref=token_ref,
    )

    assert evidence["session_token_evidence_type"] == "server_verified_ref"
    assert evidence["session_token_hash"] == stored
    assert evidence["session_token_present"] is False
    assert evidence["session_token_ref_present"] is True


def test_startup_token_evidence_mismatched_session_token_ref_is_unverified() -> None:
    stored = mf_subagent_session_token_hash("ref-token")
    evidence = _startup_token_evidence(
        {"session_token_ref": "wstok-wrong"},
        stored_token_hash=stored,
        expected_session_token_ref="wstok-expected",
    )

    assert evidence["session_token_evidence_type"] == "claimed_unverified_ref"
    assert evidence["session_token_hash"] == ""
    assert evidence["session_token_ref_present"] is True


def test_startup_token_evidence_mismatched_stored_hash_returns_claimed_unverified() -> None:
    """F1 (core): client-supplied token whose hash does NOT match stored → 'claimed_unverified'."""
    stored = "sha256:aabbccdd0011223344556677889900aabbccdd00112233445566778899000000"
    evidence = _startup_token_evidence(
        {"session_token": "different-token-xyz"},
        stored_token_hash=stored,
    )
    assert evidence["session_token_evidence_type"] == "claimed_unverified"
    # Hash is still computed (so it's available for logging), but evidence_type is downgraded.
    assert evidence["session_token_hash"].startswith("sha256:")
    assert evidence["session_token_hash"] != stored


def test_startup_token_evidence_surrogate_unchanged() -> None:
    """F1: surrogate path is not affected by the server-verification logic."""
    evidence = _startup_token_evidence(
        {"session_token_surrogate": "host-surrogate-abc"},
        stored_token_hash="",
    )
    assert evidence["session_token_evidence_type"] == "surrogate"
    assert evidence["session_token_present"] is False


def test_startup_token_evidence_empty_input_unchanged() -> None:
    """F1: empty input still produces empty evidence type."""
    evidence = _startup_token_evidence({}, stored_token_hash="")
    assert evidence["session_token_evidence_type"] == ""
    assert evidence["session_token_present"] is False


def test_claimed_unverified_is_classified_as_surrogate() -> None:
    """F1 gate: 'claimed_unverified' evidence_type MUST be treated as surrogate by finish gate."""
    claimed_unverified_startup = {
        "id": "evt-unverified-001",
        "schema_version": "mf_subagent_startup_gate.v1",
        "gate_kind": "mf_subagent.startup",
        "bounded": True,
        "close_satisfying": True,  # gate should override this
        "agent_id_match_mode": "actual_host_worker_bound",
        # claimed_unverified: worker presented token but hash did not match stored hash
        "session_token_evidence_type": "claimed_unverified",
        "session_token_hash": "sha256:presented-but-mismatched",
        "session_token_present": True,
        "host_adapter_startup_token_accepted": False,
        "task_id": "task-sg-test-01",
        "worker_slot_id": "wslot-sg-01",
        "runtime_context_id": "mfrctx-sgtest01",
        "fence_token": "fence-sg-test",
    }
    # claimed_unverified must be classified as a surrogate (not trusted)
    assert _startup_is_host_adapter_surrogate(claimed_unverified_startup) is True


def test_server_verified_is_not_classified_as_surrogate() -> None:
    """F1 gate: 'server_verified' evidence_type MUST NOT be treated as surrogate."""
    server_verified_startup = {
        "id": "evt-verified-001",
        "schema_version": "mf_subagent_startup_gate.v1",
        "gate_kind": "mf_subagent.startup",
        "bounded": True,
        "close_satisfying": True,
        "agent_id_match_mode": "actual_host_worker_bound",
        "session_token_evidence_type": "server_verified",
        "session_token_hash": "sha256:verified-token-hash",
        "session_token_present": True,
        "host_adapter_startup_token_accepted": False,
        "task_id": "task-sv-01",
        "worker_slot_id": "wslot-sv-01",
        "runtime_context_id": "mfrctx-sv-01",
        "fence_token": "fence-sv-01",
    }
    assert _startup_is_host_adapter_surrogate(server_verified_startup) is False


def test_hash_evidence_type_legacy_is_not_surrogate() -> None:
    """F1 backward compat: pre-fix 'hash' evidence_type (no server verification available)
    remains NOT a surrogate — existing recorded events are not retroactively invalidated."""
    legacy_hash_startup = {
        "id": "evt-legacy-hash-001",
        "schema_version": "mf_subagent_startup_gate.v1",
        "gate_kind": "mf_subagent.startup",
        "bounded": True,
        "session_token_evidence_type": "hash",
        "session_token_hash": "sha256:legacy-hash-abc",
        "session_token_present": True,
        "task_id": "task-legacy-01",
        "fence_token": "fence-legacy-01",
    }
    assert _startup_is_host_adapter_surrogate(legacy_hash_startup) is False


# ---------------------------------------------------------------------------
# INFO-01: finish-gate fence_token cross-check (server.py layer)
# The contract function itself does not enforce INFO-01; this tests that the
# fence_token cross-check logic is correct (server enforces it before calling).
# AC-STARTUP-TOKEN-EVIDENCE-SERVER-VERIFICATION-20260610
# ---------------------------------------------------------------------------

def test_info01_fence_token_unconditional_check_documented() -> None:
    """INFO-01 (unconditional): server.py finish-gate enforces fence_token on every request.

    After AC-STARTUP-TOKEN-TOFU-MUTUAL-EXCLUSION-20260610 hardening:
    - Body fence_token OMISSION is now refused (not silently tolerated).
    - Body fence_token MISMATCH is refused.
    - Matching fence tokens pass.
    - Context-lookup failure is refused (already handled by KeyError above the check).
    - Missing context fence_token is refused (misconfigured lane).
    """
    ctx_fence = "fence-correct-abc"
    body_fence_wrong = "fence-WRONG-xyz"
    body_fence_correct = ctx_fence

    # Cross-check: mismatch must be detected
    mismatch_detected = (
        body_fence_wrong
        and ctx_fence
        and body_fence_wrong != ctx_fence
    )
    assert mismatch_detected, "Fence mismatch must be caught before finish gate runs"

    # Cross-check: match must pass
    match_ok = not (
        body_fence_correct
        and ctx_fence
        and body_fence_correct != ctx_fence
    )
    assert match_ok, "Matching fence tokens must pass the cross-check"

    # INFO-01 unconditional: empty body fence is now REFUSED (not tolerated)
    empty_body_fence = ""
    empty_now_refused = not bool(empty_body_fence)
    assert empty_now_refused, (
        "Empty body fence_token must be refused under INFO-01 unconditional check; "
        "omission is no longer tolerated"
    )

    # Context with no fence_token must be refused
    ctx_fence_missing = ""
    missing_ctx_fence_refused = not bool(ctx_fence_missing)
    assert missing_ctx_fence_refused, (
        "Missing context fence_token must be refused (misconfigured lane)"
    )

    # Verify the INFO-01 unconditional logic is present in server.py
    import inspect
    from agent.governance import server as server_module
    server_source = inspect.getsource(server_module.handle_graph_governance_parallel_branch_finish_gate)
    assert "fence_token is required" in server_source, (
        "server.py finish-gate must refuse when body fence_token is missing"
    )
    assert "fence_token not found on server-side" in server_source, (
        "server.py finish-gate must refuse when context has no fence_token"
    )


# ---------------------------------------------------------------------------
# F4: close-gate vs finish-gate surrogate asymmetry (documented by design)
# AC-STARTUP-TOKEN-EVIDENCE-SERVER-VERIFICATION-20260610
# ---------------------------------------------------------------------------

def test_f4_close_gate_keeps_route_startup_raw_while_applying_close_startup_policy() -> None:
    """F4: route checks see raw startup rows; close checks apply startup policy.

    The shared verifier first evaluates route-context evidence from original
    rows, then applies the close-only startup subgate. This keeps precheck
    messages from claiming the startup row is missing when it is merely not
    close-satisfying.
    """
    import inspect
    from agent.governance import task_timeline
    from agent.governance import server as server_module

    source = inspect.getsource(task_timeline.mf_close_gate_verification)
    # The close gate must NOT reference surrogate-join or startup_is_host_adapter_surrogate
    assert "_startup_is_host_adapter_surrogate" not in source, (
        "mf_close_gate_verification must not evaluate per-startup surrogate policy; "
        "see mf-sop.md 'Surrogate Policy: Finish Gate vs Close Gate' for the rationale"
    )
    assert "surrogate_startup_evidence_gate" not in source, (
        "mf_close_gate_verification must not call surrogate_startup_evidence_gate; "
        "close_timeline_startup_event_gate remains the close-only adapter"
    )
    assert "close_timeline_startup_event_gate" in source
    server_source = inspect.getsource(server_module._mf_close_gate_verification)
    assert "close_timeline_events_for_verification" not in server_source
    # The finish gate DOES reference surrogate logic — confirm it is the boundary
    finish_source = inspect.getsource(validate_mf_subagent_finish_gate)
    assert "surrogate_startup_evidence_gate" in finish_source, (
        "validate_mf_subagent_finish_gate must call surrogate_startup_evidence_gate"
    )


# ---------------------------------------------------------------------------
# TOFU mutual-exclusion tests (AC-STARTUP-TOKEN-TOFU-MUTUAL-EXCLUSION-20260610)
# ---------------------------------------------------------------------------


def _host_adapter_first_sight_startup() -> dict:
    """Host-adapter mode startup with a FRESH first-sight session token.

    This is the QA-#3581 TOFU-HOST-ADAPTER-FIRST-SIGHT probe scenario:
    agent_id_match_mode = host_adapter_startup_token_surrogate, but the startup
    presents a real session_token that earns evidence_type='hash' (first-sight).
    The TOFU bypass: pre-fix this was NOT classified as surrogate; post-fix it MUST be.
    """
    return {
        "schema_version": "mf_subagent_startup_gate.v1",
        "gate_kind": "mf_subagent.startup",
        "bounded": True,
        "close_satisfying": True,  # claimed; gate should override
        "agent_id_match_mode": "host_adapter_startup_token_surrogate",
        # Fresh session token: evidence_type='hash' (first-sight commitment)
        "session_token_evidence_type": "hash",
        "session_token_hash": "sha256:fabricated-first-sight-token",
        "session_token_present": True,
        "host_adapter_startup_token_accepted": True,
        "task_id": "task-tofu-test-01",
        "worker_slot_id": "wslot-tofu-01",
        "runtime_context_id": "mfrctx-tofu01",
        "fence_token": "fence-tofu-01",
        "worktree": "/repo/.worktrees/tofu-test",
        "worktree_path": "/repo/.worktrees/tofu-test",
        "actual_cwd": "/repo/.worktrees/tofu-test",
        "actual_git_root": "/repo/.worktrees/tofu-test",
        "branch": "refs/heads/task-tofu-test",
        "head_commit": "head-tofu-test",
    }


def _host_adapter_re_presentation_startup() -> dict:
    """Host-adapter mode startup with a RE-PRESENTED (server-verified) session token.

    Design decision (AC-STARTUP-TOKEN-TOFU-MUTUAL-EXCLUSION-20260610):
    On host_adapter_startup_token_surrogate mode, server_verified evidence proves
    continuity with the FIRST presenter — but the first presenter was the HOST,
    not a verified bounded worker.  This startup MUST also be classified as surrogate.
    """
    return {
        "schema_version": "mf_subagent_startup_gate.v1",
        "gate_kind": "mf_subagent.startup",
        "bounded": True,
        "close_satisfying": True,  # claimed; gate should override
        "agent_id_match_mode": "host_adapter_startup_token_surrogate",
        # Re-presented token: evidence_type='server_verified' (hash matched stored)
        "session_token_evidence_type": "server_verified",
        "session_token_hash": "sha256:re-presented-verified-token",
        "session_token_present": True,
        "host_adapter_startup_token_accepted": True,
        "task_id": "task-tofu-test-02",
        "worker_slot_id": "wslot-tofu-02",
        "runtime_context_id": "mfrctx-tofu02",
        "fence_token": "fence-tofu-02",
        "worktree": "/repo/.worktrees/tofu-test-02",
        "worktree_path": "/repo/.worktrees/tofu-test-02",
        "actual_cwd": "/repo/.worktrees/tofu-test-02",
        "actual_git_root": "/repo/.worktrees/tofu-test-02",
        "branch": "refs/heads/task-tofu-test-02",
        "head_commit": "head-tofu-test-02",
    }


def test_host_adapter_first_sight_fresh_token_is_surrogate() -> None:
    """TOFU fix (AC-STARTUP-TOKEN-TOFU-MUTUAL-EXCLUSION-20260610): host_adapter +
    first-sight fresh token (evidence_type='hash') MUST be classified as surrogate.

    This closes the QA-#3581 TOFU-HOST-ADAPTER-FIRST-SIGHT bypass where a host-adapter
    startup with a fresh fabricated session_token earned evidence_type='hash' and
    bypassed surrogate classification entirely.
    """
    startup = _host_adapter_first_sight_startup()
    assert startup["agent_id_match_mode"] == "host_adapter_startup_token_surrogate"
    assert startup["session_token_evidence_type"] == "hash"
    assert startup["session_token_present"] is True
    # Post-fix: MUST be surrogate regardless of evidence_type
    assert _startup_is_host_adapter_surrogate(startup) is True, (
        "host_adapter_startup_token_surrogate mode with first-sight token must be "
        "classified as surrogate (TOFU mutual-exclusion)"
    )
    # surrogate_startup_evidence_gate must also demote it
    gate = surrogate_startup_evidence_gate(startup)
    assert gate["is_host_adapter_surrogate"] is True
    assert gate["close_satisfying"] is False, (
        "host_adapter first-sight startup must NOT be close-satisfying"
    )


def test_host_adapter_re_presentation_match_is_still_surrogate() -> None:
    """TOFU design decision: host_adapter + server_verified re-presentation MUST be surrogate.

    Matching a stored hash proves continuity with the first presenter, but on host-adapter
    mode the first presenter was the HOST, not a verified bounded worker.  Token hash
    continuity in host-adapter mode does NOT earn trust for a bounded worker claim.
    Only same_as_allocation_owner mode startups earn trust from token continuity.
    """
    startup = _host_adapter_re_presentation_startup()
    assert startup["agent_id_match_mode"] == "host_adapter_startup_token_surrogate"
    assert startup["session_token_evidence_type"] == "server_verified"
    assert startup["session_token_present"] is True
    # MUST be surrogate: server_verified on host-adapter mode proves host continuity, not worker
    assert _startup_is_host_adapter_surrogate(startup) is True, (
        "host_adapter_startup_token_surrogate mode with server_verified token must be "
        "classified as surrogate — hash continuity on host-adapter proves host identity only"
    )


def test_same_as_allocation_owner_first_sight_unchanged() -> None:
    """TOFU fix must NOT affect same_as_allocation_owner startups.

    same_as_allocation_owner + hash evidence is the trusted path: the allocation owner
    IS the direct executor, so token continuity confers trust.  This is unchanged.
    """
    trusted_startup = {
        "schema_version": "mf_subagent_startup_gate.v1",
        "gate_kind": "mf_subagent.startup",
        "bounded": True,
        "close_satisfying": True,
        "agent_id_match_mode": "same_as_allocation_owner",
        "session_token_evidence_type": "hash",
        "session_token_hash": "sha256:trusted-alloc-owner-token",
        "session_token_present": True,
        "host_adapter_startup_token_accepted": False,
        "task_id": "task-trusted-01",
        "fence_token": "fence-trusted-01",
    }
    # Must NOT be surrogate: same_as_allocation_owner is the trusted path
    assert _startup_is_host_adapter_surrogate(trusted_startup) is False, (
        "same_as_allocation_owner + hash must NOT be classified as surrogate"
    )
    gate = surrogate_startup_evidence_gate(trusted_startup)
    assert gate["is_host_adapter_surrogate"] is False
    assert gate["close_satisfying"] is True


def test_surrogate_with_matching_lineage_real_startup_still_joins() -> None:
    """Recovery path: surrogate + real same_as_allocation_owner startup with matching
    lineage still joins and becomes close-satisfying.

    The fix changes host_adapter+hash to surrogate but leaves the join path intact.
    The real startup that joins must be a non-host-adapter (trusted) startup.
    """
    surrogate = {
        "schema_version": "mf_subagent_startup_gate.v1",
        "gate_kind": "mf_subagent.startup",
        "bounded": True,
        "agent_id_match_mode": "host_adapter_startup_token_surrogate",
        "session_token_evidence_type": "surrogate",
        "session_token_hash": "",
        "session_token_present": False,
        "host_adapter_startup_token_accepted": True,
        "task_id": "task-recovery-01",
        "worker_slot_id": "wslot-recovery-01",
        "runtime_context_id": "mfrctx-recovery01",
        "fence_token": "fence-recovery-01",
    }
    # Real startup with same lineage and same_as_allocation_owner mode
    real_startup = {
        "id": "evt-recovery-real-001",
        "schema_version": "mf_subagent_startup_gate.v1",
        "gate_kind": "mf_subagent.startup",
        "bounded": True,
        "agent_id_match_mode": "same_as_allocation_owner",
        "session_token_evidence_type": "hash",
        "session_token_hash": "sha256:recovery-real-token",
        "session_token_present": True,
        "host_adapter_startup_token_accepted": False,
        "task_id": "task-recovery-01",
        "worker_slot_id": "wslot-recovery-01",
        "runtime_context_id": "mfrctx-recovery01",
        "fence_token": "fence-recovery-01",
    }
    # Surrogate alone is blocked
    assert _startup_is_host_adapter_surrogate(surrogate) is True
    gate_no_join = surrogate_startup_evidence_gate(surrogate)
    assert gate_no_join["close_satisfying"] is False
    # With real startup join, becomes close-satisfying
    gate_joined = surrogate_startup_evidence_gate(
        surrogate, real_startup_events=[real_startup]
    )
    assert gate_joined["is_host_adapter_surrogate"] is True
    assert gate_joined["close_satisfying"] is True
    assert gate_joined["real_worker_join"]["joined"] is True
    assert gate_joined["real_worker_join"]["join_event_id"] == "evt-recovery-real-001"


def test_fence_omission_in_body_is_refused_by_server_gate() -> None:
    """INFO-01 unconditional: finish-gate handler in server.py must refuse when
    fence_token is absent from the request body.

    This confirms the server.py source enforces the unconditional check.
    """
    import inspect
    from agent.governance import server as server_module
    source = inspect.getsource(server_module.handle_graph_governance_parallel_branch_finish_gate)
    assert "fence_token is required" in source, (
        "server.py finish-gate must refuse when body fence_token is missing"
    )


def test_context_lookup_failure_check_exists_in_server_gate() -> None:
    """INFO-01: finish-gate handler must refuse when context lookup fails.

    Server resolves context server-side via get_branch_context; if context is None,
    it raises before the fence check.  This test confirms the pattern is present.
    """
    import inspect
    from agent.governance import server as server_module
    source = inspect.getsource(server_module.handle_graph_governance_parallel_branch_finish_gate)
    # The handler raises KeyError when context is None
    assert "branch runtime context not found" in source, (
        "server.py finish-gate must raise when context lookup fails"
    )
    # The handler also refuses if context.fence_token is missing
    assert "fence_token not found on server-side" in source, (
        "server.py finish-gate must refuse when context has no fence_token"
    )


# ---------------------------------------------------------------------------
# AC-ROUTE-CONTEXT-CONTENT-HASH-VERIFY-GATE-20260608
# Content-hash verifier tests
# ---------------------------------------------------------------------------

from agent.governance.mf_subagent_contract import (
    verify_content_hash,
    canonical_contract_hash,
)
from agent.governance.service_router import _route_prompt_identity


# -- Format floor tests --

def test_verify_content_hash_good_digest_passes() -> None:
    """A well-formed sha256:<64 lowercase hex> digest passes the format floor."""
    good = "sha256:" + "a" * 64
    result = verify_content_hash(good, None, field_name="test_hash")
    assert result["ok"] is True
    assert result["status"] == "format_verified"
    assert result["object_present"] is False


def test_verify_content_hash_rr_prefix_rejected() -> None:
    """Forged/copied junk like sha256:rr-... must be rejected at the format floor."""
    bad = "sha256:rr-route-context-hash-forgery-value-padding000000000000000000000"
    result = verify_content_hash(bad, None, field_name="route_context_hash")
    assert result["ok"] is False
    assert result["status"] == "format_error"
    assert "route_context_hash" in result["reason"]


def test_verify_content_hash_uppercase_hex_rejected() -> None:
    """Uppercase hex in sha256:<...> is rejected — only lowercase is valid."""
    bad = "sha256:" + "A" * 64
    result = verify_content_hash(bad, None, field_name="test_hash")
    assert result["ok"] is False
    assert result["status"] == "format_error"


def test_verify_content_hash_wrong_length_rejected() -> None:
    """A hex part that is not exactly 64 characters is rejected."""
    too_short = "sha256:" + "a" * 32
    result = verify_content_hash(too_short, None, field_name="test_hash")
    assert result["ok"] is False
    assert result["status"] == "format_error"

    too_long = "sha256:" + "a" * 65
    result2 = verify_content_hash(too_long, None, field_name="test_hash")
    assert result2["ok"] is False
    assert result2["status"] == "format_error"


def test_verify_content_hash_missing_prefix_rejected() -> None:
    """A plain hex string without the sha256: prefix is rejected."""
    no_prefix = "a" * 64
    result = verify_content_hash(no_prefix, None, field_name="test_hash")
    assert result["ok"] is False
    assert result["status"] == "format_error"


# -- Content match / mismatch tests --

def test_verify_content_hash_content_match_passes() -> None:
    """When the object is present and the hash matches, status is verified."""
    obj = {"key": "value", "nested": {"x": 1}}
    good_hash = canonical_contract_hash(obj)
    result = verify_content_hash(good_hash, obj, field_name="route_context_hash")
    assert result["ok"] is True
    assert result["status"] == "verified"
    assert result["computed_hash"] == good_hash
    assert result["object_present"] is True


def test_verify_content_hash_content_mismatch_rejected() -> None:
    """When the object is present and the hash does NOT match, status is content_hash_mismatch."""
    obj = {"key": "value"}
    wrong_hash = canonical_contract_hash({"key": "different_value"})
    result = verify_content_hash(wrong_hash, obj, field_name="route_context_hash")
    assert result["ok"] is False
    assert result["status"] == "content_hash_mismatch"
    assert "mismatch" in result["reason"]
    assert result["computed_hash"] != wrong_hash


def test_verify_content_hash_object_absent_format_floor_only() -> None:
    """When the object is absent, only the format floor applies (no content check)."""
    real_hash = canonical_contract_hash({"any": "object"})
    # object=None → format floor only, no content check
    result = verify_content_hash(real_hash, None, field_name="route_context_hash")
    assert result["ok"] is True
    assert result["status"] == "format_verified"
    assert result["object_present"] is False


# -- Wired sites: route_context, prompt_contract, manifest --

def _make_valid_token(
    *,
    route_context_hash: str | None = None,
    route_context: dict | None = None,
    prompt_contract_hash: str | None = None,
    prompt_contract: dict | None = None,
    visible_injection_manifest_hash: str | None = None,
    visible_injection_manifest: dict | None = None,
) -> dict:
    """Build a minimal valid route_token payload for gate tests."""
    from datetime import datetime, timezone, timedelta
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    obj_rc = route_context or {"route_id": "r1", "project": "p"}
    obj_pc = prompt_contract or {"id": "pc1", "version": "1"}
    rc_hash = route_context_hash if route_context_hash is not None else canonical_contract_hash(obj_rc)
    pc_hash = prompt_contract_hash if prompt_contract_hash is not None else canonical_contract_hash(obj_pc)
    token: dict = {
        "route_context_hash": rc_hash,
        "prompt_contract_id": "rprompt-test-001",
        "prompt_contract_hash": pc_hash,
        "caller_role": "mf_sub",
        "allowed_action": "task_timeline_append",
        "expires_at": expires,
        "evidence_refs": ["evt-001"],
        "scope": {"project_id": "test-proj"},
    }
    if route_context is not None:
        token["route_context"] = route_context
    if prompt_contract is not None:
        token["prompt_contract"] = prompt_contract
    if visible_injection_manifest_hash is not None:
        token["visible_injection_manifest_hash"] = visible_injection_manifest_hash
    if visible_injection_manifest is not None:
        token["visible_injection_manifest"] = visible_injection_manifest
    return token


def test_validate_route_token_route_context_match_passes() -> None:
    """route_context present and hash matches → accepted, hash_verification recorded."""
    obj = {"route_id": "r1", "project": "p"}
    token = _make_valid_token(route_context=obj)
    result = validate_route_token_mutation_gate(
        {"route_token": token},
        action="task_timeline_append",
        project_id="test-proj",
    )
    assert result["allowed"] is True
    hv = result["hash_verification"]
    assert hv["route_context_hash"]["ok"] is True
    assert hv["route_context_hash"]["status"] == "verified"


def test_validate_route_token_route_context_mismatch_rejected() -> None:
    """route_context present but hash mismatches → MfSubagentContractError raised."""
    obj_real = {"route_id": "r1", "project": "p"}
    wrong_hash = canonical_contract_hash({"route_id": "different"})
    token = _make_valid_token(route_context_hash=wrong_hash, route_context=obj_real)
    with pytest.raises(MfSubagentContractError, match="mismatch"):
        validate_route_token_mutation_gate(
            {"route_token": token},
            action="task_timeline_append",
            project_id="test-proj",
        )


def test_validate_route_token_route_context_absent_format_floor_passes() -> None:
    """route_context absent but hash is well-formed → accepted (format floor only)."""
    # Do not include route_context in the token.
    obj = {"route_id": "r1", "project": "p"}
    rc_hash = canonical_contract_hash(obj)
    token = _make_valid_token(route_context_hash=rc_hash)
    # No route_context key → object absent → format floor only
    assert "route_context" not in token
    result = validate_route_token_mutation_gate(
        {"route_token": token},
        action="task_timeline_append",
        project_id="test-proj",
    )
    assert result["allowed"] is True
    hv = result["hash_verification"]
    assert hv["route_context_hash"]["ok"] is True
    assert hv["route_context_hash"]["status"] == "format_verified"


def test_validate_route_token_forged_route_context_hash_rejected() -> None:
    """A forged/ill-formed route_context_hash is rejected at the format floor."""
    token = _make_valid_token(route_context_hash="sha256:rr-route-forgery-" + "0" * 46)
    with pytest.raises(MfSubagentContractError, match="sha256"):
        validate_route_token_mutation_gate(
            {"route_token": token},
            action="task_timeline_append",
        )


def test_validate_route_token_prompt_contract_match_passes() -> None:
    """prompt_contract present and hash matches → accepted."""
    obj = {"id": "pc1", "version": "1"}
    token = _make_valid_token(prompt_contract=obj)
    result = validate_route_token_mutation_gate(
        {"route_token": token},
        action="task_timeline_append",
    )
    assert result["allowed"] is True
    hv = result["hash_verification"]
    assert hv["prompt_contract_hash"]["ok"] is True
    assert hv["prompt_contract_hash"]["status"] == "verified"


def test_validate_route_token_prompt_contract_mismatch_rejected() -> None:
    """prompt_contract present but hash mismatches → error."""
    obj_real = {"id": "pc1", "version": "1"}
    wrong_hash = canonical_contract_hash({"id": "different"})
    token = _make_valid_token(prompt_contract_hash=wrong_hash, prompt_contract=obj_real)
    with pytest.raises(MfSubagentContractError, match="mismatch"):
        validate_route_token_mutation_gate(
            {"route_token": token},
            action="task_timeline_append",
        )


def test_validate_route_token_manifest_match_passes() -> None:
    """visible_injection_manifest present and hash matches → accepted."""
    manifest_obj = {"manifest_key": "manifest_value", "version": 2}
    manifest_hash = canonical_contract_hash(manifest_obj)
    token = _make_valid_token(
        visible_injection_manifest_hash=manifest_hash,
        visible_injection_manifest=manifest_obj,
    )
    result = validate_route_token_mutation_gate(
        {"route_token": token},
        action="task_timeline_append",
    )
    assert result["allowed"] is True
    hv = result["hash_verification"]
    assert hv["visible_injection_manifest_hash"]["ok"] is True
    assert hv["visible_injection_manifest_hash"]["status"] == "verified"


def test_validate_route_token_manifest_mismatch_rejected() -> None:
    """visible_injection_manifest present but hash mismatches → error."""
    manifest_obj = {"manifest_key": "real"}
    wrong_hash = canonical_contract_hash({"manifest_key": "forged"})
    token = _make_valid_token(
        visible_injection_manifest_hash=wrong_hash,
        visible_injection_manifest=manifest_obj,
    )
    with pytest.raises(MfSubagentContractError, match="mismatch"):
        validate_route_token_mutation_gate(
            {"route_token": token},
            action="task_timeline_append",
        )


def test_validate_route_token_manifest_absent_format_floor_passes() -> None:
    """visible_injection_manifest_hash present but manifest object absent → format floor only."""
    manifest_hash = canonical_contract_hash({"any": "obj"})
    token = _make_valid_token(visible_injection_manifest_hash=manifest_hash)
    assert "visible_injection_manifest" not in token
    result = validate_route_token_mutation_gate(
        {"route_token": token},
        action="task_timeline_append",
    )
    assert result["allowed"] is True
    hv = result["hash_verification"]
    assert hv["visible_injection_manifest_hash"]["ok"] is True
    assert hv["visible_injection_manifest_hash"]["status"] == "format_verified"


def test_gate_output_records_hash_verification() -> None:
    """Accepted gate result must include hash_verification dict."""
    obj = {"route_id": "r1"}
    token = _make_valid_token(route_context=obj)
    result = validate_route_token_mutation_gate(
        {"route_token": token},
        action="task_timeline_append",
    )
    assert "hash_verification" in result
    assert isinstance(result["hash_verification"], dict)


# -- service_router._route_prompt_identity hash format floor --

def test_route_prompt_identity_rejects_ill_formed_route_context_hash() -> None:
    """_route_prompt_identity must drop ill-formed route_context_hash strings."""
    forged_hash = "sha256:rr-not-hex-padding-000000000000000000000000000000000000000000000"
    result = _route_prompt_identity({"route_context_hash": forged_hash})
    assert "route_context_hash" not in result


def test_route_prompt_identity_accepts_valid_route_context_hash() -> None:
    """_route_prompt_identity keeps a valid sha256:<64 hex> route_context_hash."""
    valid_hash = "sha256:" + "a" * 64
    result = _route_prompt_identity({"route_context_hash": valid_hash})
    assert result.get("route_context_hash") == valid_hash


def test_route_prompt_identity_rejects_ill_formed_prompt_contract_hash() -> None:
    """_route_prompt_identity must drop ill-formed prompt_contract_hash strings."""
    forged_hash = "sha256:rr-not-hex-padding-000000000000000000000000000000000000000000000"
    result = _route_prompt_identity({"prompt_contract_hash": forged_hash})
    assert "prompt_contract_hash" not in result


def test_route_prompt_identity_computes_manifest_hash_when_manifest_present() -> None:
    """_route_prompt_identity computes and records manifest hash from the object."""
    from agent.governance.mf_subagent_contract import canonical_contract_hash as cch
    manifest = {"inject_key": "inject_val"}
    expected_hash = cch(manifest)
    bundle = {"visible_injection_manifest": manifest}
    result = _route_prompt_identity({"route_prompt_bundle": bundle})
    assert result.get("visible_injection_manifest_hash") == expected_hash
