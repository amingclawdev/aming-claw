from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from agent.governance import parallel_branch_runtime, server
from agent.governance.contracts.runtime import (
    ContractRuntime,
    WriteGateDecision,
    _active_failed_qa_line,
    _contract_completion_satisfying_lines,
    _line_status_allows_contract_completion,
    _mf_parallel_worker_commit_errors,
    _worker_commit_completed_implementation,
)
from agent.governance.contracts.execution_state import build_execution_state


def test_mf_parallel_rev8_requires_both_worker_lanes_before_merge_and_final_qa():
    definition = json.loads(
        (
            Path(__file__).parents[1]
            / "governance"
            / "contract_definitions"
            / "mf_parallel.v2.rev8.json"
        ).read_text()
    )
    workers = [
        {
            "runtime_context_id": "mfrctx-two-worker-a",
            "task_id": "two-worker-a",
            "parent_task_id": "cex-two-worker",
            "worker_id": "slot-a",
            "worker_slot_id": "slot-a",
            "merge_queue_id": "mq-two-worker-a",
            "line_instance_id": "runtime_context:mfrctx-two-worker-a",
        },
        {
            "runtime_context_id": "mfrctx-two-worker-b",
            "task_id": "two-worker-b",
            "parent_task_id": "cex-two-worker",
            "worker_id": "slot-b",
            "worker_slot_id": "slot-b",
            "merge_queue_id": "mq-two-worker-b",
            "line_instance_id": "runtime_context:mfrctx-two-worker-b",
        },
    ]
    reconcile_policy = definition["system_layer"]["graph_binding_policy"][
        "current_full_reconcile_evidence_policy"
    ]
    assert reconcile_policy["qa_authority_required_before_reconcile"] is False
    assert reconcile_policy["qa_authority_required_after_reconcile"] is True
    assert "qa_authority_alternatives" not in reconcile_policy
    completed = [
        {
            "stage_id": "orchestration",
            "line_id": "observer_prefill_child_contracts",
        },
        {
            "stage_id": "dispatch",
            "line_id": "observer_dispatch_bounded_workers",
            "payload": {
                "worker_count": 2,
                "atomic": True,
                "bounded_workers": workers,
            },
        },
    ]

    def current():
        return build_execution_state(
            definition,
            project_id="aming-claw",
            backlog_id="AC-TWO-WORKER",
            contract_execution_id="cex-two-worker",
            actor_role="observer",
            completed_lines=completed,
        )["next_action"]

    def finish_next():
        action = current()
        completed.append(
            {
                "stage_id": action["stage_id"],
                "line_id": action["line_id"],
                "line_instance_id": action.get("line_instance_id", ""),
                "runtime_context_id": action.get("runtime_context_id", ""),
            }
        )
        return action

    assert current()["runtime_context_id"] == "mfrctx-two-worker-a"
    first = finish_next()
    assert first["line_id"] == "worker_read_runtime_guide"
    assert current()["runtime_context_id"] == "mfrctx-two-worker-b"

    while current()["line_id"] != "worker_finish_gate":
        finish_next()
    assert current()["runtime_context_id"] == "mfrctx-two-worker-a"
    finish_next()
    assert current()["line_id"] == "worker_finish_gate"
    assert current()["runtime_context_id"] == "mfrctx-two-worker-b"
    finish_next()

    assert current()["line_id"] == "observer_merge"
    assert current()["runtime_context_id"] == "mfrctx-two-worker-a"
    finish_next()
    assert current()["line_id"] == "observer_merge"
    assert current()["runtime_context_id"] == "mfrctx-two-worker-b"
    finish_next()
    assert current()["line_id"] == "observer_reconcile"
    finish_next()
    assert current()["line_id"] == "qa_graph_context"
    finish_next()
    assert current()["line_id"] == "qa_independent_verification"


def test_mf_parallel_rev9_preserves_rev8_stage_machine_and_requires_allocation_precheck():
    definitions = {}
    for revision in ("rev8", "rev9"):
        definitions[revision] = json.loads(
            (
                Path(__file__).parents[1]
                / "governance"
                / "contract_definitions"
                / f"mf_parallel.v2.{revision}.json"
            ).read_text()
        )

    rev8 = definitions["rev8"]
    rev9 = definitions["rev9"]
    assert [
        (
            stage["stage_id"],
            [line["line_id"] for line in stage.get("lines") or []],
        )
        for stage in rev9["rule_layer"]["stages"]
    ] == [
        (
            stage["stage_id"],
            [line["line_id"] for line in stage.get("lines") or []],
        )
        for stage in rev8["rule_layer"]["stages"]
    ]

    policy = rev9["system_layer"]["allocation_precheck_policy"]
    assert policy["enabled"] is True
    assert policy["tool"] == "parallel_branch_allocate_precheck"
    assert policy["required_before"] == "initial_parallel_branch_allocate"
    assert policy["expected_lane_count"] == 2
    assert policy["atomic"] is True
    assert policy["read_only"] is True
    assert policy["submit_returned_bodies_unchanged"] is True
    assert policy["applies_to_stage_types"] == ["mf_sub"]
    assert policy["excluded_stage_types"] == ["failed_qa_rework"]
    assert policy["zero_write_surfaces"] == [
        "runtime_context",
        "worktree",
        "merge_queue",
        "timeline",
        "contract_runtime",
    ]
    assert "direct_fix" not in {
        successor["contract_id"] for successor in rev9["successors"]
    }


def test_mf_parallel_rev9_requires_two_lanes_before_merge_and_final_qa():
    definition = json.loads(
        (
            Path(__file__).parents[1]
            / "governance"
            / "contract_definitions"
            / "mf_parallel.v2.rev9.json"
        ).read_text()
    )
    workers = [
        {
            "runtime_context_id": f"mfrctx-rev9-worker-{suffix}",
            "task_id": f"rev9-worker-{suffix}",
            "parent_task_id": "cex-rev9-two-worker",
            "worker_id": f"slot-{suffix}",
            "worker_slot_id": f"slot-{suffix}",
            "merge_queue_id": f"mq-rev9-worker-{suffix}",
            "line_instance_id": f"runtime_context:mfrctx-rev9-worker-{suffix}",
        }
        for suffix in ("a", "b")
    ]
    completed = [
        {
            "stage_id": "orchestration",
            "line_id": "observer_prefill_child_contracts",
        },
        {
            "stage_id": "dispatch",
            "line_id": "observer_dispatch_bounded_workers",
            "payload": {
                "worker_count": 2,
                "atomic": True,
                "bounded_workers": workers,
            },
        },
    ]

    def current():
        return build_execution_state(
            definition,
            project_id="aming-claw",
            backlog_id="AC-REV9-TWO-WORKER",
            contract_execution_id="cex-rev9-two-worker",
            actor_role="observer",
            completed_lines=completed,
        )["next_action"]

    def finish_next():
        action = current()
        completed.append(
            {
                "stage_id": action["stage_id"],
                "line_id": action["line_id"],
                "line_instance_id": action.get("line_instance_id", ""),
                "runtime_context_id": action.get("runtime_context_id", ""),
            }
        )
        return action

    while current()["line_id"] != "observer_merge":
        finish_next()
    assert current()["runtime_context_id"] == "mfrctx-rev9-worker-a"
    finish_next()
    assert current()["line_id"] == "observer_merge"
    assert current()["runtime_context_id"] == "mfrctx-rev9-worker-b"
    finish_next()
    assert current()["line_id"] == "observer_reconcile"
    finish_next()
    assert current()["line_id"] == "qa_graph_context"
    finish_next()
    assert current()["line_id"] == "qa_independent_verification"


def test_worker_commit_contract_accepts_target_relative_delta_with_inherited_projection():
    worker_files = [
        "agent/governance/contract_definitions/mf_parallel.v2.rev8.json",
        "agent/governance/server.py",
        "agent/tests/test_contract_registry.py",
        "agent/tests/test_contract_runtime.py",
        "agent/tests/test_graph_governance_api.py",
    ]
    inherited_file = "agent/governance/contracts/execution_state.py"
    runtime_context_id = "mfrctx-inherited-target-worker-commit"
    task_id = "inherited-target-worker-commit"
    worker_id = "inherited-target-slot"
    commit_sha = "c" * 40
    base_commit = "a" * 40
    target_head = "b" * 40
    identity = {
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "parent_task_id": "cex-inherited-target-worker-commit",
        "worker_id": worker_id,
        "worker_slot_id": worker_id,
        "worker_session_id": worker_id,
        "actor_session_principal": worker_id,
        "filer_principal": worker_id,
        "target_project_root": "/tmp/inherited-target-worker-commit",
        "session_token_ref": "wstok-inherited-target-worker-commit",
        "fence_token_hash": "sha256:" + "d" * 64,
        "worker_role": "mf_sub",
        "evidence_owner_role": "mf_sub",
    }
    implementation = {
        "line_id": "worker_implementation",
        "actor_role": "mf_sub",
        "evidence_kind": "implementation",
        "status": "accepted",
        **identity,
        "changed_files": worker_files,
        "graph_trace_ids": ["gqt-inherited-target-worker-commit"],
        "payload": {
            **identity,
            "changed_files": worker_files,
            "graph_trace_ids": ["gqt-inherited-target-worker-commit"],
            "test_results": {"status": "passed", "passed": True},
        },
    }
    record = {
        "contract_execution_id": identity["parent_task_id"],
        "contract_id": "mf_parallel.v2",
        "completed_lines": [implementation],
    }
    normal_projection = {
        "schema_version": (
            "runtime_context.normal_pre_qa_target_head_revision.v1"
        ),
        "source": "server_revalidated_normal_pre_qa_target_head",
        "server_derived": True,
        "post_qa_merge_conflict_recovery": False,
        "runtime_base_commit": base_commit,
        "runtime_target_head_commit": target_head,
        "current_target_baseline_commit": target_head,
        "target_head_boundary_applied": True,
        "target_head_boundary_reason": (
            "runtime_target_head_is_pre_worker_ancestor"
        ),
        "inherited_target_head_files": [inherited_file],
        "worker_authored_candidate_delta_files": worker_files,
        "target_baseline_changes_worker_authored": False,
        "base_to_target_inheritance_preserved": True,
    }
    worker_commit = {
        **identity,
        "changed_files": worker_files,
        "commit_diff_files": worker_files,
        "owned_files": worker_files,
        "graph_trace_ids": ["gqt-inherited-target-worker-commit"],
        "db_verified": True,
        "clean_worktree": True,
        "dirty_files": [],
        "commit_sha": commit_sha,
        "worker_commit_sha": commit_sha,
        "head_commit": commit_sha,
        "immutable_head_commit": commit_sha,
        "validated_head_commit": commit_sha,
        "diff_base_commit": target_head,
        "normal_pre_qa_target_head_revision": normal_projection,
        "observer_impersonation": False,
    }
    write = {
        "stage_id": "worker_commit",
        "line_id": "worker_commit",
        "actor_role": "mf_sub",
        "evidence_kind": "worker_commit",
        "commit_sha": commit_sha,
        "payload": worker_commit,
    }

    assert _mf_parallel_worker_commit_errors(
        record,
        write,
        actor_role="mf_sub",
    ) == ()

    invalid_source = deepcopy(record)
    invalid_source["completed_lines"][0]["payload"]["test_results"] = {
        "status": "partial_sibling_blocked",
        "passed": True,
        "focused_pytest": "blocked_by_unmerged_sibling_task",
        "planner_only_probe": "passed",
        "git_diff_check": "passed",
    }
    assert any(
        "finish-compatible test_results" in error
        for error in _mf_parallel_worker_commit_errors(
            invalid_source,
            write,
            actor_role="mf_sub",
        )
    )

    legacy_candidate_comparison = deepcopy(record)
    legacy_candidate_comparison["completed_lines"][0]["payload"][
        "test_results"
    ] = {
        "schema_version": "runtime_context.worker_test_results.v1",
        "candidate_new_failures": 0,
        "focused_candidate": {
            "status": "passed",
            "failed": 0,
            "passed": 6,
        },
        "expanded_candidate": {
            "status": "passed",
            "failed": 0,
            "passed": 23,
        },
        "immutable_base": {
            "status": "expected_red",
            "failed": 2,
            "passed": 1,
            "selector": (
                "rev8_postmerge_qa_graph_binding or "
                "pre_rev8_qa_graph_binding"
            ),
        },
        "qa_claim": False,
        "release_claim": False,
        "old_world_reuse": False,
    }
    assert _mf_parallel_worker_commit_errors(
        legacy_candidate_comparison,
        write,
        actor_role="mf_sub",
    ) == ()

    cumulative_regression = deepcopy(write)
    cumulative_regression["payload"]["changed_files"] = sorted(
        [inherited_file, *worker_files]
    )
    cumulative_regression["payload"]["commit_diff_files"] = sorted(
        [inherited_file, *worker_files]
    )
    assert any(
        "normal pre-QA worker delta" in error
        for error in _mf_parallel_worker_commit_errors(
            record,
            cumulative_regression,
            actor_role="mf_sub",
        )
    )


def test_mf_parallel_rev8_reconcile_accepts_two_pre_qa_lane_merges(monkeypatch):
    execution_id = "cex-two-worker-reconcile"
    backlog_id = "AC-TWO-WORKER-RECONCILE"
    dispatch_source_ref = (
        f"contract_runtime:{execution_id}:completed_lines:0"
    )
    workers = [
        {
            "runtime_context_id": "mfrctx-reconcile-a",
            "task_id": "reconcile-worker-a",
            "parent_task_id": execution_id,
            "merge_queue_id": "mq-reconcile-a",
        },
        {
            "runtime_context_id": "mfrctx-reconcile-b",
            "task_id": "reconcile-worker-b",
            "parent_task_id": execution_id,
            "merge_queue_id": "mq-reconcile-b",
        },
    ]
    completed = [
        {
            "stage_id": "dispatch",
            "line_id": "observer_dispatch_bounded_workers",
            "actor_role": "observer",
            "evidence_kind": "dispatch_bounded_worker",
            "payload": {
                "worker_count": 2,
                "atomic_dispatch": True,
                "bounded_workers": workers,
            },
        }
    ]
    for index, worker in enumerate(workers, start=1):
        durable = {
            "schema_version": (
                "contract_runtime.observer_merge_durable_authority.v1"
            ),
            "server_derived": True,
            "db_verified": True,
            "project_id": "aming-claw",
            "backlog_id": backlog_id,
            "contract_execution_id": execution_id,
            **worker,
            "merge_commit": str(index) * 40,
            "merge_event_ref": f"timeline:{100 + index}",
            "merge_event_id": 100 + index,
            "merge_event_created_at": f"2026-08-01T00:00:0{index}Z",
            "contract_runtime_dispatch_source_ref": dispatch_source_ref,
            "pre_qa_merge_authorized": True,
            "final_qa_required_after_reconcile": True,
            "close_satisfying": False,
        }
        completed.append(
            {
                "stage_id": "merge",
                "line_id": "observer_merge",
                "line_instance_id": (
                    f"runtime_context:{worker['runtime_context_id']}"
                ),
                "payload": {"durable_merge_authority": durable},
            }
        )
    record = {
        "project_id": "aming-claw",
        "backlog_id": backlog_id,
        "contract_id": "mf_parallel.v2",
        "revision": "rev8",
        "contract_execution_id": execution_id,
        "completed_lines": completed,
    }
    monkeypatch.setattr(
        server,
        "_contract_runtime_current_full_reconcile_authority_from_merge",
        lambda *_args, **_kwargs: {},
    )

    authority = server._contract_runtime_reconcile_record_authority(
        None,
        project_id="aming-claw",
        record=record,
    )

    assert authority["record_verified"] is True
    assert authority["merge_projection_verified"] is True
    assert authority["dispatch_lineage_verified"] is True
    assert authority["all_lane_merges_verified"] is True
    assert authority["lane_merge_count"] == 2
    assert authority["lane_runtime_context_ids"] == [
        "mfrctx-reconcile-a",
        "mfrctx-reconcile-b",
    ]
    assert authority["merged_commit_sha"] == "2" * 40
    assert authority["merge_event_id"] == 102
    assert authority["reconcile_event_recorded"] is False

    captured = {}

    def enrich_reconcile(
        _conn,
        *,
        project_id,
        record,
        context,
        timeline_events,
        merge,
    ):
        captured["resolver_merge"] = dict(merge)
        return {
            **merge,
            "reconcile_source_ref": "timeline:103",
            "reconcile_event_id": 103,
            "reconcile_event_created_at": "2026-08-01T00:00:03Z",
            "reconcile_task_id": context.task_id,
            "reconcile_runtime_context_id": context.runtime_context_id,
        }

    def current_full(
        _conn,
        *,
        project_id,
        record,
        merge,
        reconcile,
    ):
        captured["current_full_merge"] = dict(merge)
        captured["current_full_reconcile"] = dict(reconcile)
        return {**merge, "db_verified": True}

    final_worker = workers[1]
    context = SimpleNamespace(
        **final_worker,
        backlog_id=backlog_id,
        batch_id="",
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_contexts_for_dispatch_line",
        lambda *_args, **_kwargs: [context],
    )
    monkeypatch.setattr(
        server,
        "_runtime_context_service_timeline_events",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_completed_merge_reconcile_authority",
        enrich_reconcile,
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_current_full_reconcile_authority_from_merge",
        current_full,
    )

    close_authority = server._contract_runtime_current_full_reconcile_authority(
        None,
        project_id="aming-claw",
        record=record,
    )

    assert captured["resolver_merge"]["all_lane_merges_verified"] is True
    assert captured["resolver_merge"]["close_satisfying"] is False
    assert captured["current_full_reconcile"]["reconcile_event_id"] == 103
    assert close_authority["db_verified"] is True


def test_mf_parallel_rev8_failed_final_qa_routes_to_bounded_worker_fix():
    record = {
        "contract_id": "mf_parallel.v2",
        "revision": "rev8",
        "contract_execution_id": "cex-two-worker-failed-qa",
        "runtime_guide": {
            "next_legal_action": {
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "owner_role": "qa",
            }
        },
        "completed_lines": [
            {
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "actor_role": "qa",
                "evidence_kind": "independent_verification",
                "status": "failed",
                "payload": {"status": "failed", "verdict": "FAIL"},
            }
        ],
    }

    state = server._runtime_current_state_from_record(record)

    assert state["readiness_state"] == "failed_qa_worker_fix_required"
    assert state["next_legal_action"] == {
        "id": "enter_direct_fix_successor",
        "action": "enter_direct_fix_successor",
        "owner_role": "observer",
        "recommended_successor_contract_id": "direct_fix",
        "failed_qa_source_ref": (
            "contract_runtime:cex-two-worker-failed-qa:completed_lines:0"
        ),
        "bounded_worker_fix_required": True,
        "fresh_qa_session_required": True,
        "return_sequence": [
            "observer_merge",
            "observer_reconcile",
            "qa_graph_context",
            "qa_independent_verification",
        ],
    }


def test_mf_parallel_rev9_failed_final_qa_routes_to_same_contract_worker_fix():
    record = {
        "contract_id": "mf_parallel.v2",
        "revision": "rev9",
        "contract_execution_id": "cex-rev9-failed-qa",
        "runtime_guide": {
            "next_legal_action": {
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "owner_role": "qa",
            }
        },
        "completed_lines": [
            {
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "actor_role": "qa",
                "evidence_kind": "independent_verification",
                "status": "failed",
                "payload": {"status": "failed", "verdict": "FAIL"},
            }
        ],
    }

    state = server._runtime_current_state_from_record(record)

    assert state["readiness_state"] == "failed_qa_worker_fix_required"
    assert state["next_legal_action"] == {
        "id": "allocate_bounded_worker_fix",
        "action": "parallel_branch_allocate",
        "owner_role": "observer",
        "recommended_successor_contract_id": "mf_parallel.v2",
        "failed_qa_source_ref": (
            "contract_runtime:cex-rev9-failed-qa:completed_lines:0"
        ),
        "bounded_worker_fix_required": True,
        "successor_mode": "same_contract_append_only_failed_qa_rework",
        "worker_fix_cardinality": 1,
        "allocation_precheck_required": False,
        "allocation_tool": "parallel_branch_allocate",
        "allocation_request_requirements": {
            "stage_type": "failed_qa_rework",
            "minimum_attempt": 2,
            "failed_qa_source_ref": (
                "contract_runtime:cex-rev9-failed-qa:completed_lines:0"
            ),
            "fresh_runtime_context_required": True,
        },
        "direct_fix_allowed": False,
        "fresh_qa_session_required": True,
        "return_sequence": [
            "observer_merge",
            "observer_reconcile",
            "qa_graph_context",
            "qa_independent_verification",
        ],
    }


def test_mf_parallel_rev9_postmerge_uses_verified_single_worker_fix_generation(
    monkeypatch,
):
    contract_execution_id = "cex-rev9-single-worker-fix"
    rework_runtime_context_id = "mfrctx-rev9-single-worker-fix"
    rework_task_id = "rev9-single-worker-fix"
    failed_qa_index = 4
    initial_workers = [
        {
            "runtime_context_id": "mfrctx-initial-a",
            "task_id": "initial-worker-a",
            "parent_task_id": contract_execution_id,
            "merge_queue_id": "mq-initial-a",
        },
        {
            "runtime_context_id": "mfrctx-initial-b",
            "task_id": "initial-worker-b",
            "parent_task_id": contract_execution_id,
            "merge_queue_id": "mq-initial-b",
        },
    ]
    rework_revision = {
        "append_only_history_preserved": True,
        "timeline_projection_authoritative": False,
        "failed_qa_completed_line_index": failed_qa_index,
        "runtime_context_id": rework_runtime_context_id,
        "task_id": rework_task_id,
    }
    rework_authority = {
        "source": "parallel_branch_allocate_failed_qa_rework",
        "server_derived": True,
        "contract_execution_id": contract_execution_id,
        "failed_qa_completed_line_index": failed_qa_index,
        "runtime_context_id": rework_runtime_context_id,
        "task_id": rework_task_id,
    }
    record = {
        "contract_id": "mf_parallel.v2",
        "version": "v2",
        "revision": "rev9",
        "project_id": "aming-claw",
        "backlog_id": "AC-REV9-SINGLE-WORKER-FIX",
        "contract_execution_id": contract_execution_id,
        "completed_lines": [
            {
                "stage_id": "dispatch",
                "line_id": "observer_dispatch_bounded_workers",
                "evidence_kind": "dispatch_bounded_worker",
                "actor_role": "observer",
                "payload": {
                    "bounded_workers": initial_workers,
                },
            },
            *[
                {
                    "stage_id": "observer_lane_merge",
                    "line_id": "observer_merge",
                    "evidence_kind": "merge",
                    "actor_role": "observer",
                    "runtime_context_id": worker["runtime_context_id"],
                    "line_instance_id": (
                        f"runtime_context:{worker['runtime_context_id']}"
                    ),
                    "payload": {
                        "line_instance_id": (
                            f"runtime_context:{worker['runtime_context_id']}"
                        ),
                        "durable_merge_authority": {
                            "schema_version": (
                                server._CONTRACT_RUNTIME_DURABLE_MERGE_SCHEMA_VERSION
                            ),
                            "server_derived": True,
                            "db_verified": True,
                            "pre_qa_merge_authorized": True,
                            "final_qa_required_after_reconcile": True,
                            "project_id": "aming-claw",
                            "backlog_id": "AC-REV9-SINGLE-WORKER-FIX",
                            "contract_execution_id": contract_execution_id,
                            **worker,
                            "queue_item_id": f"mqi-initial-{index}",
                            "contract_runtime_dispatch_source_ref": (
                                f"contract_runtime:{contract_execution_id}:"
                                "completed_lines:0"
                            ),
                            "merge_commit": str(index) * 40,
                            "merge_event_ref": f"timeline:{100 + index}",
                            "merge_event_id": 100 + index,
                            "merge_event_created_at": (
                                f"2026-08-02T11:00:0{index}Z"
                            ),
                        },
                    },
                }
                for index, worker in enumerate(initial_workers, start=1)
            ],
            {
                "stage_id": "reconcile",
                "line_id": "observer_reconcile",
                "evidence_kind": "reconcile",
                "actor_role": "observer",
                "status": "accepted",
                "payload": {
                    "reconcile_authority": {
                        "record_verified": True,
                        "merged_commit_sha": "2" * 40,
                    }
                },
            },
            {
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "evidence_kind": "independent_verification",
                "actor_role": "qa",
                "status": "failed",
                "payload": {"status": "failed", "verdict": "FAIL"},
            },
            {
                "stage_id": "dispatch",
                "line_id": "observer_dispatch_bounded_workers",
                "evidence_kind": "dispatch_bounded_worker",
                "actor_role": "observer",
                "runtime_context_id": rework_runtime_context_id,
                "task_id": rework_task_id,
                "payload": {
                    "runtime_context_id": rework_runtime_context_id,
                    "task_id": rework_task_id,
                    "parent_task_id": contract_execution_id,
                    "merge_queue_id": "mq-rev9-single-worker-fix",
                    "failed_qa_rework_dispatch_revision": rework_revision,
                    "failed_qa_rework_dispatch_revision_authority": (
                        rework_authority
                    ),
                },
            },
        ],
    }

    selected = server._contract_runtime_current_dispatch_authority_line(record)
    assert selected["status"] == "selected"
    assert selected["completed_line_index"] == 5
    assert (
        server._contract_runtime_mf_parallel_current_generation_worker_count(
            record
        )
        == 1
    )
    assert (
        server._contract_runtime_mf_parallel_current_generation_worker_count(
            {**record, "revision": "rev8"}
        )
        == 2
    )

    record["completed_lines"].append(
        {
            "stage_id": "observer_lane_merge",
            "line_id": "observer_merge",
            "evidence_kind": "merge",
            "actor_role": "observer",
            "runtime_context_id": rework_runtime_context_id,
            "line_instance_id": (
                f"runtime_context:{rework_runtime_context_id}"
            ),
            "payload": {
                "line_instance_id": (
                    f"runtime_context:{rework_runtime_context_id}"
                ),
                "durable_merge_authority": {
                    "schema_version": (
                        server._CONTRACT_RUNTIME_DURABLE_MERGE_SCHEMA_VERSION
                    ),
                    "server_derived": True,
                    "db_verified": True,
                    "pre_qa_merge_authorized": True,
                    "final_qa_required_after_reconcile": True,
                    "project_id": "aming-claw",
                    "backlog_id": "AC-REV9-SINGLE-WORKER-FIX",
                    "contract_execution_id": contract_execution_id,
                    "runtime_context_id": rework_runtime_context_id,
                    "task_id": rework_task_id,
                    "parent_task_id": contract_execution_id,
                    "merge_queue_id": "mq-rev9-single-worker-fix",
                    "queue_item_id": "mqi-rev9-single-worker-fix",
                    "contract_runtime_dispatch_source_ref": (
                        f"contract_runtime:{contract_execution_id}:"
                        "completed_lines:5"
                    ),
                    "merge_commit": "a" * 40,
                    "merge_event_ref": "timeline:123",
                    "merge_event_id": 123,
                    "merge_event_created_at": "2026-08-02T12:00:00Z",
                },
            },
        }
    )
    merge = server._contract_runtime_rev8_two_worker_merge_projection(
        record,
        required_worker_count=1,
    )
    assert merge["all_lane_merges_verified"] is True
    assert merge["required_worker_count"] == 1
    assert merge["runtime_context_id"] == rework_runtime_context_id
    assert merge["dispatch_completed_line_index"] == 5
    assert merge["merge_completed_line_index"] == 6

    current_reconcile_receipt = {
        "record_verified": True,
        "merged_commit_sha": "a" * 40,
        "runtime_context_id": rework_runtime_context_id,
        "task_id": rework_task_id,
        "parent_task_id": contract_execution_id,
        "merge_queue_id": "mq-rev9-single-worker-fix",
    }
    current_reconcile_receipt["authority_hash"] = server.stable_sha256(
        current_reconcile_receipt
    )
    record["completed_lines"].append(
        {
            "stage_id": "reconcile",
            "line_id": "observer_reconcile",
            "evidence_kind": "reconcile",
            "actor_role": "observer",
            "status": "accepted",
            "payload": {
                "reconcile_authority": current_reconcile_receipt,
            },
        }
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_completed_line_acceptance",
        lambda *_args, **_kwargs: {"db_verified": True},
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_reconcile_record_authority",
        lambda *_args, **_kwargs: current_reconcile_receipt,
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_contexts_for_dispatch_line",
        lambda *_args, **_kwargs: [],
    )

    qa_authority = server._contract_runtime_rev8_postmerge_qa_authority(
        None,
        project_id="aming-claw",
        record=record,
    )
    assert qa_authority["verified"] is False
    assert qa_authority["blocker_codes"] == [
        "final_merge_runtime_context_unresolved"
    ]


def _accepted_no_pass_line(*, reported_baseline_failed: int) -> dict:
    failures = [f"test_known_baseline_{index:02d}" for index in range(19)]
    base_commit = "a" * 40
    candidate_commit = "b" * 40
    common_results = {
        "status": "accepted",
        "candidate_new_failures": 0,
        "candidate_specific_issues": [],
        "no_pass_claim": True,
        "passed": False,
        "overall_release_pass": False,
        "overall_release_pass_claimed": False,
        "baseline": {"failed": reported_baseline_failed},
        "candidate": {"failed": len(failures), "passed": 741},
    }
    return {
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "independent_verification",
        "runtime_context_id": "mfrctx-no-pass-binding",
        "task_id": "worker-no-pass-binding",
        "parent_task_id": "parent-no-pass-binding",
        "authorization_source": "qa_session_token_ref",
        "observer_impersonation": False,
        "parent_materialization_authorized": False,
        "commit_sha": candidate_commit,
        "status": "accepted",
        "verdict": "accepted",
        "qa_evidence_provenance": {
            "schema_version": "qa_evidence_provenance.v1",
            "server_derived": True,
            "authorization_source": "qa_session_token_ref",
            "evidence_owner_role": "qa",
            "observer_impersonation": False,
            "parent_materialization_authorized": False,
            "completion_status_gate": {
                "schema_version": "contract_runtime.qa_completion_status_gate.v1",
                "source": "contract_runtime_line_write_normalization",
                "server_derived": True,
                "top_level_status_present": True,
                "top_level_status_passing": True,
                "normalized_status": "accepted",
                "nested_payload_decision_satisfies": False,
            },
            "authenticated_qa_binding": {
                "schema_version": "contract_runtime.authenticated_qa_binding.v1",
                "server_derived": True,
                "qa_principal": "qa:no-pass-binding",
                "qa_session_id": "ses-no-pass-binding",
                "independent_verification_session_matched": True,
            },
        },
        "payload": {
            "schema_version": "mf_parallel.qa_independent_verification.v1",
            "runtime_context_id": "mfrctx-no-pass-binding",
            "task_id": "worker-no-pass-binding",
            "parent_task_id": "parent-no-pass-binding",
            "acceptance_scope": "candidate_regression_and_acceptance_criteria",
            "verdict": "accepted",
            "full_suite_claim": "not_claimed",
            "candidate_new_failures": 0,
            "candidate_specific_issues": [],
            "no_pass_claim": True,
            "overall_release_pass_claimed": False,
            "test_results": dict(common_results),
        },
        "test_results": dict(common_results),
        "verification": {
            "status": "accepted",
            "verdict": "accepted",
            "candidate_new_failures": 0,
            "candidate_specific_issues": [],
            "no_pass_claim": True,
            "overall_release_pass_claimed": False,
        },
        "artifact_refs": {
            "external_no_pass_baseline_ledger": {
                "schema_version": (
                    "contract_runtime.external_no_pass_baseline_ledger.v2"
                ),
                "server_normalized": True,
                "base_commit_sha": base_commit,
                "candidate_commit_sha": candidate_commit,
                "base_failure_identities": failures,
                "candidate_failure_identities": failures,
                "base_reproduction": {
                    "reproduced": len(failures),
                    "total": len(failures),
                    "failure_identities": failures,
                },
                "candidate_suite_counts": {
                    "baseline_known_non_green": len(failures),
                    "failed": len(failures),
                    "passed": 741,
                },
                "candidate_new_failures": 0,
                "candidate_specific_issues": [],
                "no_pass_claim": True,
                "overall_release_pass_claimed": False,
                "refs": ["base:known", "candidate:known"],
            }
        },
    }


def test_exact_accepted_no_pass_line_allows_contract_completion():
    line = _accepted_no_pass_line(reported_baseline_failed=19)
    record = {
        "contract_execution_id": "cex-no-pass-binding",
        "completed_lines": [line],
    }

    assert _line_status_allows_contract_completion(
        line,
        source_record=record,
        source_line_index=0,
    )
    assert _active_failed_qa_line(
        [line],
        source_record=record,
    ) == (-1, {})


def test_accepted_no_pass_count_mismatch_is_active_failed_qa():
    line = _accepted_no_pass_line(reported_baseline_failed=20)
    record = {
        "contract_execution_id": "cex-no-pass-binding",
        "completed_lines": [line],
    }

    assert not _line_status_allows_contract_completion(
        line,
        source_record=record,
        source_line_index=0,
    )
    failed_index, failed_line = _active_failed_qa_line(
        [line],
        source_record=record,
    )
    assert failed_index == 0
    assert failed_line is line


def _partial_rework_baseline_record() -> dict:
    execution_id = "cex-partial-rework-baseline"
    runtime_context_id = "mfrctx-partial-rework-baseline"
    task_id = "worker-partial-rework-baseline"
    commit_sha = "b" * 40
    revision_event_ref = "timeline:19032"
    failed_qa_ref = (
        f"contract_runtime:{execution_id}:completed_lines:10"
    )
    trace_ids = [
        "gqt-partial-rework-baseline-a",
        "gqt-partial-rework-baseline-b",
    ]
    affected_suite = {
        "baseline_failed": 43,
        "failed": 43,
        "new_failures": 0,
        "passed": 981,
    }
    canonical = {
        "stage_id": "worker_implementation",
        "line_id": "worker_implementation",
        "actor_role": "mf_sub",
        "evidence_kind": "implementation",
        "status": "completed",
        "commit_sha": commit_sha,
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "parent_task_id": execution_id,
        "payload": {
            "runtime_context_id": runtime_context_id,
            "task_id": task_id,
            "parent_task_id": execution_id,
            "graph_trace_ids": trace_ids,
            "canonical_rework_lineage_revision": {
                "schema_version": (
                    "contract_runtime.worker_implementation_rework_revision.v1"
                ),
                "source": "server_verified_failed_qa_rework",
                "failed_qa_completed_line_index": 10,
                "revision_event_ref": revision_event_ref,
                "commit_sha": commit_sha,
                "append_only_history_preserved": True,
            },
            "canonical_rework_lineage_revision_authority": {
                "schema_version": (
                    "runtime_context.clean_cumulative_git_revision_authority.v1"
                ),
                "source": "runtime_context_clean_cumulative_git_revision",
                "server_derived": True,
                "clean_worktree": True,
                "actual_head_commit": commit_sha,
                "revision_event_ref": revision_event_ref,
                "failed_qa_source_ref": failed_qa_ref,
            },
            "failed_qa_revision_rejoin_marker": {
                "schema_version": (
                    "contract_runtime.failed_qa_revision_rejoin_marker.v1"
                ),
                "source": "accepted_runtime_context_rejoin_event",
                "evidence_backfill": False,
                "contract_execution_id": execution_id,
                "runtime_context_id": runtime_context_id,
                "task_id": task_id,
                "parent_task_id": execution_id,
                "revision_event_ref": revision_event_ref,
                "failed_qa_source": "contract_runtime_completed_lines",
                "failed_qa_source_ref": failed_qa_ref,
            },
            "graph_trace_db_evidence": {
                "schema_version": "mf_subagent_graph_trace_db_evidence.v1",
                "db_verified": True,
                "missing_trace_ids": [],
                "identity_mismatches": [],
                "query_source": "mf_subagent",
                "worker_role": "mf_sub",
                "runtime_context_id": runtime_context_id,
                "task_id": task_id,
                "parent_task_id": execution_id,
                "requested_trace_ids": trace_ids,
                "verified_trace_ids": trace_ids,
            },
            "worker_evidence_provenance": {
                "schema_version": (
                    "contract_runtime.worker_evidence_provenance.v1"
                ),
                "source": "runtime_context_copy_safe_worker_proof",
                "verified": True,
                "worker_owned": True,
                "worker_role": "mf_sub",
                "runtime_context_id": runtime_context_id,
                "task_id": task_id,
            },
            "tests": [
                {
                    "name": "affected_suite_baseline_compare",
                    "status": "baseline_matched",
                    **affected_suite,
                }
            ],
        },
        "verification": {"affected_suite": dict(affected_suite)},
    }
    duplicate = deepcopy(canonical)
    duplicate["payload"].pop("canonical_rework_lineage_revision")
    duplicate["payload"].pop(
        "canonical_rework_lineage_revision_authority"
    )
    duplicate["payload"].pop("failed_qa_revision_rejoin_marker")
    duplicate["test_results"] = {
        "baseline_failed": 43,
        "affected_suite_failed": 43,
        "new_failures": 0,
    }
    padding = [
        {
            "stage_id": "historical",
            "line_id": f"historical_{index}",
            "actor_role": "observer",
            "evidence_kind": "historical_context",
            "status": "accepted",
        }
        for index in range(10)
    ]
    failed_qa = {
        "stage_id": "qa",
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "independent_verification",
        "status": "failed",
        "payload": {"status": "failed", "verdict": "FAIL"},
    }
    return {
        "schema_version": "contract_runtime_execution_record.v1",
        "project_id": "aming-claw",
        "backlog_id": "AC-PARTIAL-REWORK-BASELINE",
        "contract_execution_id": execution_id,
        "contract_id": "mf_parallel.v2",
        "completed_lines": [*padding, failed_qa, canonical, duplicate],
    }


def test_partial_rework_baseline_selects_canonical_line_before_duplicate():
    record = _partial_rework_baseline_record()
    lines = record["completed_lines"]
    canonical = lines[11]
    duplicate = lines[12]

    assert _line_status_allows_contract_completion(
        canonical,
        source_record=record,
        source_line_index=11,
    )
    assert not _line_status_allows_contract_completion(
        duplicate,
        source_record=record,
        source_line_index=12,
    )
    assert _worker_commit_completed_implementation(
        record,
        runtime_context_id="mfrctx-partial-rework-baseline",
        task_id="worker-partial-rework-baseline",
    ) is canonical
    satisfying = _contract_completion_satisfying_lines(
        lines,
        source_record=record,
    )
    assert canonical in satisfying
    assert duplicate not in satisfying
    assert len(record["completed_lines"]) == 13


def test_clean_worker_implementation_still_selects_latest_completion():
    runtime_context_id = "mfrctx-clean-worker-implementation"
    task_id = "clean-worker-implementation"
    earlier = {
        "line_id": "worker_implementation",
        "actor_role": "mf_sub",
        "evidence_kind": "implementation",
        "status": "completed",
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "commit_sha": "a" * 40,
        "verification": {"passed": True, "failed": 0},
    }
    latest = {
        **earlier,
        "commit_sha": "b" * 40,
        "verification": {"passed": True, "failed": 0, "new_failures": 0},
    }
    record = {
        "contract_execution_id": "cex-clean-worker-implementation",
        "contract_id": "mf_parallel.v2",
        "completed_lines": [earlier, latest],
    }

    assert _worker_commit_completed_implementation(
        record,
        runtime_context_id=runtime_context_id,
        task_id=task_id,
    ) is latest


def test_partial_rework_baseline_fails_closed_on_counts_or_identity_drift():
    record = _partial_rework_baseline_record()
    canonical = record["completed_lines"][11]
    affected_sources = (
        canonical["payload"]["tests"][0],
        canonical["verification"]["affected_suite"],
    )
    for source in affected_sources:
        source["baseline_failure_identities"] = [
            "test_inherited_a",
            "test_inherited_b",
        ]
        source["candidate_failure_identities"] = [
            "test_inherited_a",
            "test_inherited_b",
        ]
    assert _line_status_allows_contract_completion(
        canonical,
        source_record=record,
        source_line_index=11,
    )

    identity_drift = deepcopy(record)
    identity_drift["completed_lines"][11]["verification"]["affected_suite"][
        "candidate_failure_identities"
    ] = ["test_inherited_a", "test_candidate_new"]
    assert not _line_status_allows_contract_completion(
        identity_drift["completed_lines"][11],
        source_record=identity_drift,
        source_line_index=11,
    )

    count_drift = deepcopy(record)
    count_drift["completed_lines"][11]["verification"]["affected_suite"][
        "failed"
    ] = 44
    assert not _line_status_allows_contract_completion(
        count_drift["completed_lines"][11],
        source_record=count_drift,
        source_line_index=11,
    )

    candidate_new = deepcopy(record)
    candidate_new["completed_lines"][11]["payload"]["tests"][0][
        "new_failures"
    ] = 1
    assert not _line_status_allows_contract_completion(
        candidate_new["completed_lines"][11],
        source_record=candidate_new,
        source_line_index=11,
    )

    forged_authority = deepcopy(record)
    forged_authority["completed_lines"][11]["payload"][
        "canonical_rework_lineage_revision_authority"
    ]["server_derived"] = False
    assert not _line_status_allows_contract_completion(
        forged_authority["completed_lines"][11],
        source_record=forged_authority,
        source_line_index=11,
    )


def _synthetic_later_qa_pass(*, summary: str) -> dict:
    return {
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "independent_verification",
        "status": "passed",
        "observer_impersonation": False,
        "payload": {
            "status": "passed",
            "summary": summary,
        },
        "qa_evidence_provenance": {
            "schema_version": "qa_evidence_provenance.v1",
            "server_derived": True,
            "authorization_source": "qa_role_session",
            "evidence_owner_role": "qa",
            "observer_impersonation": False,
            "completion_status_gate": {
                "schema_version": (
                    "contract_runtime.qa_completion_status_gate.v1"
                ),
                "source": "contract_runtime_line_write_normalization",
                "server_derived": True,
                "top_level_status_present": False,
                "top_level_status_passing": False,
                "normalized_status": "",
                "nested_payload_decision_satisfies": False,
            },
        },
    }


def test_synthetic_later_pass_clears_repair_without_satisfying_close():
    failed = {
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "status": "failed",
        "payload": {"summary": "Independent QA failed the worker commit."},
    }
    passed = _synthetic_later_qa_pass(
        summary="Independent QA passed the retry."
    )
    record = {
        "contract_execution_id": "cex-synthetic-qa-pass",
        "completed_lines": [failed, passed],
    }

    assert not _line_status_allows_contract_completion(
        passed,
        source_record=record,
        source_line_index=1,
    )
    assert _active_failed_qa_line(
        record["completed_lines"],
        source_record=record,
    ) == (-1, {})


def test_synthetic_later_pass_with_failure_summary_keeps_repair_active():
    failed = {
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "status": "failed",
        "payload": {"summary": "Independent QA failed the worker commit."},
    }
    contradictory = _synthetic_later_qa_pass(
        summary="Independent QA failed the worker commit again."
    )
    record = {
        "contract_execution_id": "cex-synthetic-qa-contradiction",
        "completed_lines": [failed, contradictory],
    }

    failed_index, failed_line = _active_failed_qa_line(
        record["completed_lines"],
        source_record=record,
    )
    assert failed_index == 1
    assert failed_line is contradictory


def test_failed_qa_rework_close_gate_prevalidates_without_generic_mutation(
    monkeypatch,
):
    """The facade's first gate must not append the canonical retry line.

    The runtime-context canonical helper owns the one permitted mutation.  If
    the generic timeline close gate writes first, the same request is observed
    as a duplicate revision by the canonical helper and can fail after a
    partial ContractRuntime advance.
    """

    record = {
        "contract_execution_id": "cex-failed-qa-atomic",
        "project_id": "project-failed-qa-atomic",
        "completed_lines": [],
        "runtime_guide": {
            "next_legal_action": {
                "stage_id": "worker_implementation",
                "line_id": "worker_implementation",
                "evidence_kind": "implementation",
            }
        },
    }
    submit_calls = []

    class _Store:
        def get(self, _contract_execution_id):
            return record

    class _Runtime:
        store = _Store()

        def current_guide(self, *_args, **_kwargs):
            return record["runtime_guide"]

        def submit_line_write(self, *_args, **_kwargs):
            submit_calls.append((_args, _kwargs))
            raise AssertionError(
                "generic close gate must not mutate a prevalidated failed-QA retry"
            )

    runtime_context = SimpleNamespace(
        runtime_context_id="mfrctx-failed-qa-atomic",
        task_id="worker-failed-qa-atomic",
    )
    monkeypatch.setattr(server, "_contract_runtime", lambda _conn: _Runtime())
    monkeypatch.setattr(
        server,
        "_contract_runtime_apply_mf_parallel_context_projection",
        lambda *_args, **_kwargs: (record, {}),
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_close_line_for_event",
        lambda *_args, **_kwargs: {
            "stage_id": "worker_implementation",
            "line_id": "worker_implementation",
            "evidence_kind": "implementation",
        },
    )
    monkeypatch.setattr(
        server,
        "_runtime_current_state_from_record",
        lambda _record: {
            "next_legal_action": record["runtime_guide"]["next_legal_action"]
        },
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_write_from_record",
        lambda *_args, **_kwargs: {
            "stage_id": "worker_implementation",
            "line_id": "worker_implementation",
            "evidence_kind": "implementation",
        },
    )
    monkeypatch.setattr(
        server,
        "_runtime_context_revise_failed_qa_implementation_lineage",
        lambda *_args, **_kwargs: {
            "status": "validated_submission",
            "canonical_payload": dict(_kwargs["payload"]),
        },
    )
    monkeypatch.setattr(
        parallel_branch_runtime,
        "get_branch_context_by_runtime_context_id",
        lambda *_args, **_kwargs: runtime_context,
    )

    gate = server._contract_runtime_close_gate(
        object(),
        project_id="project-failed-qa-atomic",
        body={
            "contract_execution_id": "cex-failed-qa-atomic",
            "runtime_context_id": runtime_context.runtime_context_id,
            "actor": "mf_sub",
        },
        event_kind="implementation",
        norm_payload={
            "runtime_context_id": runtime_context.runtime_context_id,
            "task_id": runtime_context.task_id,
        },
        trusted_actor_role="mf_sub",
    )

    assert gate["status"] == "validated_submission"
    assert gate["canonical_submit_required"] is True
    assert submit_calls == []


def test_failed_qa_revision_prepares_guide_before_single_cas_update():
    prior_commit = "a" * 40
    revised_commit = "b" * 40
    revision_event_ref = "timeline:failed-qa-rejoin"
    identity = {
        "runtime_context_id": "mfrctx-failed-qa-single-cas",
        "task_id": "worker-failed-qa-single-cas",
        "parent_task_id": "parent-failed-qa-single-cas",
        "worker_id": "worker-failed-qa-single-cas",
        "worker_slot_id": "worker-failed-qa-single-cas",
        "target_project_root": "/tmp/failed-qa-single-cas",
        "fence_token_hash": "sha256:" + ("c" * 64),
        "session_token_ref": "wstok-failed-qa-single-cas",
    }
    failed_qa = {
        "stage_id": "qa",
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "independent_verification",
        "status": "failed",
        "payload": {"status": "failed", "verdict": "FAIL"},
    }
    prior_implementation = {
        "stage_id": "worker_implementation",
        "line_id": "worker_implementation",
        "actor_role": "mf_sub",
        "evidence_kind": "implementation",
        "commit_sha": prior_commit,
        "changed_files": ["agent/governance/server.py"],
        "graph_trace_ids": ["gqt-failed-qa-single-cas"],
        **identity,
        "payload": {
            **identity,
            "changed_files": ["agent/governance/server.py"],
            "graph_trace_ids": ["gqt-failed-qa-single-cas"],
        },
    }
    record = {
        "contract_execution_id": "cex-failed-qa-single-cas",
        "contract_id": "mf_parallel.v2",
        "project_id": "project-failed-qa-single-cas",
        "backlog_id": "backlog-failed-qa-single-cas",
        "execution_state_revision": 7,
        "completed_lines": [failed_qa, prior_implementation],
    }

    class _SingleCasStore:
        def __init__(self):
            self.record = deepcopy(record)
            self.update_calls = 0

        def get(self, _contract_execution_id):
            return deepcopy(self.record)

        def update(
            self,
            _contract_execution_id,
            updated,
            *,
            expected_revision=None,
        ):
            self.update_calls += 1
            if self.update_calls > 1:
                raise AssertionError(
                    "failed-QA revision must use one prepared CAS update"
                )
            assert expected_revision == 7
            self.record = deepcopy(updated)
            return deepcopy(self.record)

    store = _SingleCasStore()
    runtime = object.__new__(ContractRuntime)
    runtime.store = store

    def _record_view(source, *, actor_role=None, completed_lines=None, **_kwargs):
        view = deepcopy(dict(source))
        view["completed_lines"] = deepcopy(list(completed_lines or []))
        view["execution_state"] = {
            "execution_state_revision": source["execution_state_revision"],
        }
        retry_commit_exists = any(
            line.get("line_id") == "worker_commit"
            for line in view["completed_lines"][1:]
            if isinstance(line, dict)
        )
        view["runtime_guide"] = {
            "next_legal_action": {
                "stage_id": (
                    "worker_attestation"
                    if retry_commit_exists
                    else "worker_commit"
                ),
                "line_id": (
                    "worker_finish_time_attestation"
                    if retry_commit_exists
                    else "worker_commit"
                ),
                "owner_role": "mf_sub",
                "evidence_kind": (
                    "record_finish_time_worker_attestation"
                    if retry_commit_exists
                    else "commit"
                ),
            }
        }
        view["precheck_decision"] = WriteGateDecision(ok=True).to_dict()
        return view

    runtime._record_view = _record_view
    marker = {
        "revision_event_ref": revision_event_ref,
        "session_token_ref_rotation": {},
    }
    authority = {
        "source": "runtime_context_clean_cumulative_git_revision",
        "server_derived": True,
        "actual_head_commit": revised_commit,
        "clean_worktree": True,
        "cumulative_changed_files": ["agent/governance/server.py"],
        "owned_files": ["agent/governance/server.py"],
        "revision_event_ref": revision_event_ref,
    }
    revision_write = {
        "stage_id": "worker_implementation",
        "line_id": "worker_implementation",
        "actor_role": "mf_sub",
        "evidence_kind": "implementation",
        "commit_sha": revised_commit,
        "changed_files": ["agent/governance/server.py"],
        "graph_trace_ids": ["gqt-failed-qa-single-cas"],
        **identity,
        "payload": {
            **identity,
            "changed_files": ["agent/governance/server.py"],
            "graph_trace_ids": ["gqt-failed-qa-single-cas"],
            "graph_trace_db_evidence": {
                "db_verified": True,
                "verified_trace_ids": ["gqt-failed-qa-single-cas"],
            },
            "failed_qa_revision_rejoin_marker": marker,
            "canonical_rework_lineage_revision_authority": authority,
        },
    }
    result = runtime.revise_failed_qa_worker_implementation(
        record["contract_execution_id"],
        revision_write,
        actor_role="mf_sub",
    )

    assert result["ok"] is True
    assert result["status"] == "revised"
    assert store.update_calls == 1
    assert store.record["execution_state_revision"] == 8
    assert store.record["runtime_guide"]["next_legal_action"]["line_id"] == (
        "worker_commit"
    )

    exact_replay = runtime.revise_failed_qa_worker_implementation(
        record["contract_execution_id"],
        revision_write,
        actor_role="mf_sub",
    )
    assert exact_replay["ok"] is True
    assert exact_replay["status"] == "already_completed"
    assert store.update_calls == 1

    store.record["completed_lines"].append(
        {
            "stage_id": "worker_commit",
            "line_id": "worker_commit",
            "actor_role": "mf_sub",
            "evidence_kind": "commit",
            "commit_sha": revised_commit,
            **identity,
            "payload": dict(identity),
        }
    )
    exact_after_commit = runtime.revise_failed_qa_worker_implementation(
        record["contract_execution_id"],
        revision_write,
        actor_role="mf_sub",
    )
    assert exact_after_commit["ok"] is True
    assert exact_after_commit["status"] == "already_completed"
    assert store.update_calls == 1

    later_commit = "d" * 40
    later_revision = deepcopy(revision_write)
    later_revision["commit_sha"] = later_commit
    later_revision["payload"][
        "canonical_rework_lineage_revision_authority"
    ]["actual_head_commit"] = later_commit
    rejected_later_revision = runtime.revise_failed_qa_worker_implementation(
        record["contract_execution_id"],
        later_revision,
        actor_role="mf_sub",
    )
    assert rejected_later_revision["ok"] is False
    assert any(
        "closed after retry worker_commit" in error
        for error in rejected_later_revision["decision"]["errors"]
    )
    assert store.update_calls == 1


def test_failed_qa_fresh_dispatch_revision_is_append_only_single_cas_and_replay_safe():
    historical_dispatch = {
        "stage_id": "dispatch",
        "line_id": "observer_dispatch_bounded_workers",
        "actor_role": "observer",
        "evidence_kind": "dispatch_bounded_worker",
        "runtime_context_id": "mfrctx-original",
        "task_id": "worker-original",
        "parent_task_id": "cex-failed-qa-fresh-dispatch",
        "worker_id": "worker-original",
        "worker_slot_id": "worker-original",
        "owned_files": ["agent/governance/server.py"],
        "payload": {
            "runtime_context_id": "mfrctx-original",
            "task_id": "worker-original",
            "parent_task_id": "cex-failed-qa-fresh-dispatch",
            "worker_role": "mf_sub",
        },
    }
    failed_qa = {
        "stage_id": "qa",
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "independent_verification",
        "status": "failed",
        "payload": {"status": "failed", "verdict": "FAIL"},
    }
    record = {
        "contract_execution_id": "cex-failed-qa-fresh-dispatch",
        "contract_id": "mf_parallel.v2",
        "project_id": "project-failed-qa-fresh-dispatch",
        "backlog_id": "backlog-failed-qa-fresh-dispatch",
        "execution_state_revision": 11,
        "completed_lines": [historical_dispatch, failed_qa],
    }

    class _SingleCasStore:
        def __init__(self):
            self.record = deepcopy(record)
            self.update_calls = 0

        def get(self, _contract_execution_id):
            return deepcopy(self.record)

        def update(
            self,
            _contract_execution_id,
            updated,
            *,
            expected_revision=None,
        ):
            self.update_calls += 1
            assert expected_revision == self.record["execution_state_revision"]
            self.record = deepcopy(updated)
            return deepcopy(self.record)

    store = _SingleCasStore()
    runtime = object.__new__(ContractRuntime)
    runtime.store = store

    def _record_view(source, *, actor_role=None, completed_lines=None, **_kwargs):
        view = deepcopy(dict(source))
        view["completed_lines"] = deepcopy(list(completed_lines or []))
        view["runtime_guide"] = {
            "next_legal_action": {
                "stage_id": "worker_read",
                "line_id": "worker_read_runtime_guide",
                "owner_role": "mf_sub",
                "evidence_kind": "read_receipt",
            }
        }
        view["precheck_decision"] = WriteGateDecision(ok=True).to_dict()
        return view

    runtime._record_view = _record_view
    identity = {
        "runtime_context_id": "mfrctx-replacement",
        "task_id": "worker-replacement",
        "parent_task_id": "cex-failed-qa-fresh-dispatch",
        "worker_id": "worker-replacement",
        "worker_slot_id": "worker-replacement",
        "owned_files": [
            "agent/governance/server.py",
            "agent/tests/test_graph_governance_api.py",
        ],
        "route_token_ref": "rtok-replacement",
    }
    write = {
        "stage_id": "dispatch",
        "line_id": "observer_dispatch_bounded_workers",
        "actor_role": "observer",
        "evidence_kind": "dispatch_bounded_worker",
        **identity,
        "payload": {
            **identity,
            "worker_role": "mf_sub",
            "failed_qa_rework_dispatch_revision_authority": {
                "source": "parallel_branch_allocate_failed_qa_rework",
                "server_derived": True,
                "contract_execution_id": record["contract_execution_id"],
                "failed_qa_completed_line_index": 1,
                "runtime_context_id": identity["runtime_context_id"],
                "task_id": identity["task_id"],
            },
        },
    }

    result = runtime.revise_failed_qa_observer_dispatch(
        record["contract_execution_id"],
        write,
        actor_role="observer",
    )

    assert result["ok"] is True
    assert result["status"] == "revised"
    assert result["append_only_history_preserved"] is True
    assert store.update_calls == 1
    assert store.record["execution_state_revision"] == 12
    assert len(store.record["completed_lines"]) == 3
    assert store.record["completed_lines"][0] == historical_dispatch
    replacement = store.record["completed_lines"][-1]
    assert replacement["runtime_context_id"] == "mfrctx-replacement"
    assert replacement["payload"]["failed_qa_rework_dispatch_revision"][
        "timeline_projection_authoritative"
    ] is False

    exact_replay = runtime.revise_failed_qa_observer_dispatch(
        record["contract_execution_id"],
        write,
        actor_role="observer",
    )
    assert exact_replay["ok"] is True
    assert exact_replay["status"] == "already_completed"
    assert exact_replay["contract_runtime_line_mutated"] is False
    assert store.update_calls == 1
    assert len(store.record["completed_lines"]) == 3
    assert store.record["execution_state_revision"] == 12

    preserved_first_cycle = deepcopy(store.record["completed_lines"])
    later_failed_qa = deepcopy(failed_qa)
    later_failed_qa["payload"]["acceptance_failed"] = [
        "second_rework_generation_required"
    ]
    store.record["completed_lines"].extend(
        [
            {
                "stage_id": "worker_commit",
                "line_id": "worker_commit",
                "actor_role": "mf_sub",
                "evidence_kind": "worker_commit",
                "commit_sha": "a" * 40,
            },
            later_failed_qa,
        ]
    )
    store.record["execution_state_revision"] = 13

    stale_cycle_replay = runtime.revise_failed_qa_observer_dispatch(
        record["contract_execution_id"],
        write,
        actor_role="observer",
    )
    assert stale_cycle_replay["ok"] is False
    assert any(
        "active failed QA line" in error
        for error in stale_cycle_replay["decision"]["errors"]
    )
    assert store.update_calls == 1
    assert store.record["completed_lines"][:3] == preserved_first_cycle

    second_write = deepcopy(write)
    second_authority = second_write["payload"][
        "failed_qa_rework_dispatch_revision_authority"
    ]
    second_authority["failed_qa_completed_line_index"] = 4
    second_result = runtime.revise_failed_qa_observer_dispatch(
        record["contract_execution_id"],
        second_write,
        actor_role="observer",
    )
    assert second_result["ok"] is True
    assert second_result["status"] == "revised"
    assert store.update_calls == 2
    assert store.record["completed_lines"][:3] == preserved_first_cycle
    second_exact_replay = runtime.revise_failed_qa_observer_dispatch(
        record["contract_execution_id"],
        second_write,
        actor_role="observer",
    )
    assert second_exact_replay["ok"] is True
    assert second_exact_replay["status"] == "already_completed"
    assert store.update_calls == 2

    duplicate_current_authority = deepcopy(
        store.record["completed_lines"][-1]
    )
    store.record["completed_lines"].append(duplicate_current_authority)
    multiple_current_authorities = runtime.revise_failed_qa_observer_dispatch(
        record["contract_execution_id"],
        second_write,
        actor_role="observer",
    )
    assert multiple_current_authorities["ok"] is False
    assert any(
        "multiple current-cycle" in error
        for error in multiple_current_authorities["decision"]["errors"]
    )
    assert store.update_calls == 2
