from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from agent.governance import (
    graph_snapshot_store,
    parallel_branch_runtime,
    server,
    task_timeline,
)
from agent.governance.contracts import ContractDefinitionRegistry
from agent.governance.contracts.runtime import (
    ContractRuntime,
    SQLiteContractExecutionStore,
    WriteGateDecision,
    _active_failed_qa_line,
    _contract_completion_satisfying_lines,
    _line_status_allows_contract_completion,
    _mf_parallel_worker_commit_errors,
    _worker_commit_completed_implementation,
)
from agent.governance.contracts.execution_state import build_execution_state


def test_current_record_guide_projection_does_not_compete_with_finish_writer_lock(
    tmp_path,
):
    """A sibling guide read must stay read-only while finish owns the writer."""

    definition = {
        "schema_version": "contract_definition.v1",
        "contract_id": "sqlite_read_only_guide",
        "version": "v1",
        "revision": "rev1",
        "role": "mf_sub",
        "contract_type": "mf_parallel",
        "status": "active",
        "rule_layer": {
            "stages": [
                {
                    "stage_id": "worker_finish",
                    "lines": [
                        {
                            "line_id": "worker_finish_gate",
                            "owner_role": "mf_sub",
                            "allowed_writer_roles": ["mf_sub"],
                            "evidence_kind": "mf_subagent_finish_gate",
                        }
                    ],
                }
            ]
        },
        "instruction_layer": {
            "inline": ["Finish through the canonical runtime gate."],
            "refs": [],
        },
    }
    (tmp_path / "sqlite_read_only_guide.v1.rev1.json").write_text(
        json.dumps(definition),
        encoding="utf-8",
    )
    db_path = tmp_path / "contract-runtime.sqlite3"
    writer_conn = sqlite3.connect(db_path, timeout=0.05)
    reader_conn = sqlite3.connect(db_path, timeout=0.05)
    for conn in (writer_conn, reader_conn):
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 50")

    registry = ContractDefinitionRegistry(tmp_path)
    writer_runtime = ContractRuntime(
        registry,
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(writer_conn),
    )
    reader_runtime = ContractRuntime(
        registry,
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(reader_conn),
    )
    execution_id = "cex-sqlite-read-only-guide"
    created = writer_runtime.start_execution(
        "sqlite_read_only_guide",
        project_id="daily-planner",
        backlog_id="DP-SQLITE-FINISH",
        contract_execution_id=execution_id,
        actor_role="mf_sub",
        route_token_ref="rtok-sqlite-finish",
    )
    writer_conn.commit()

    writer_conn.execute("BEGIN IMMEDIATE")
    writer_conn.execute(
        "UPDATE contract_runtime_executions SET updated_at = updated_at "
        "WHERE contract_execution_id = ?",
        (execution_id,),
    )
    changes_before_read = reader_conn.total_changes
    guide = reader_runtime.current_record(
        execution_id,
        actor_role="mf_sub",
    )["runtime_guide"]
    assert guide["next_legal_action"]["line_id"] == "worker_finish_gate"
    assert reader_conn.total_changes == changes_before_read

    write = {
        "project_id": created["project_id"],
        "backlog_id": created["backlog_id"],
        "contract_execution_id": execution_id,
        "definition_hash": created["definition_hash"],
        "instruction_bundle_hash": created["instruction_bundle_hash"],
        "execution_state_revision": created["execution_state_revision"],
        "runtime_guide_hash": guide["runtime_guide_hash"],
        "stage_id": "worker_finish",
        "line_id": "worker_finish_gate",
        "actor_role": "mf_sub",
        "evidence_kind": "mf_subagent_finish_gate",
    }
    precheck = reader_runtime.precheck_line_write(
        execution_id,
        write,
        actor_role="mf_sub",
    )
    assert precheck["ok"] is True
    assert reader_conn.total_changes == changes_before_read

    finished = writer_runtime.submit_line_write(
        execution_id,
        write,
        actor_role="mf_sub",
    )
    assert finished["ok"] is True
    assert finished["record"]["execution_state_revision"] == 2
    writer_conn.commit()

    duplicate = writer_runtime.submit_line_write(
        execution_id,
        write,
        actor_role="mf_sub",
    )
    assert duplicate["ok"] is False
    persisted = writer_runtime.store.get(execution_id)
    assert persisted["execution_state_revision"] == 2
    assert len(persisted["completed_lines"]) == 1
    writer_conn.close()
    reader_conn.close()


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
    assert policy["allowed_lane_counts"] == [1, 2]
    assert policy["atomic"] is True
    assert policy["cardinality_source"] == (
        "runtime_guide.effective_worker_cardinality_policy.required_worker_count"
    )
    assert policy["effective_policy_projection"] == (
        "runtime_guide.effective_allocation_precheck_policy"
    )
    assert policy["standalone_policy"] == {
        "expected_lane_count": 2,
        "atomic": True,
        "scope": "standalone_contract",
    }
    assert policy["verified_batch_child_policy"] == {
        "cardinality_source": "verified_batch_child_lineage",
        "expected_lane_count": 1,
        "atomic": False,
        "scope": "per_child_contract",
        "cross_child_union_allowed": False,
        "precheck_each_child_independently": True,
    }
    assert policy["read_only"] is True
    assert policy["submit_returned_bodies_unchanged"] is True
    assert policy["applies_to_stage_types"] == ["mf_sub"]
    assert policy["excluded_stage_types"] == ["failed_qa_rework"]
    cardinality_sensitive_text = json.dumps(rev9, ensure_ascii=False)
    assert "both lane merges" not in cardinality_sensitive_text
    assert "two-lane fan-out" not in cardinality_sensitive_text
    assert "both finished worker lanes" not in cardinality_sensitive_text
    assert "both line instances" not in cardinality_sensitive_text
    assert "all required lane merges" in cardinality_sensitive_text
    assert "initial required-lane fan-out" in cardinality_sensitive_text
    assert "every finished worker lane required" in cardinality_sensitive_text
    assert "every required line instance" in cardinality_sensitive_text
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


def test_rev9_reconcile_record_resolves_final_two_lane_merge_timeline(
    monkeypatch,
):
    execution_id = "cex-rev9-reconcile-record"
    backlog_id = "AC-REV9-RECONCILE-RECORD"
    workers = [
        {
            "runtime_context_id": "mfrctx-rev9-reconcile-record-a",
            "task_id": "rev9-reconcile-record-a",
            "parent_task_id": execution_id,
            "merge_queue_id": "mq-rev9-reconcile-record-a",
        },
        {
            "runtime_context_id": "mfrctx-rev9-reconcile-record-b",
            "task_id": "rev9-reconcile-record-b",
            "parent_task_id": execution_id,
            "merge_queue_id": "mq-rev9-reconcile-record-b",
        },
    ]
    dispatch_ref = f"contract_runtime:{execution_id}:completed_lines:0"
    completed_lines = [
        {
            "stage_id": "dispatch",
            "line_id": "observer_dispatch_bounded_workers",
            "actor_role": "observer",
            "evidence_kind": "dispatch_bounded_worker",
            "payload": {
                "worker_count": 2,
                "required_worker_count": 2,
                "atomic_dispatch": True,
                "bounded_workers": workers,
            },
        }
    ]
    for index, worker in enumerate(workers, start=1):
        completed_lines.append(
            {
                "stage_id": "observer_lane_merge",
                "line_id": "observer_merge",
                "actor_role": "observer",
                "evidence_kind": "merge",
                "line_instance_id": (
                    f"runtime_context:{worker['runtime_context_id']}"
                ),
                "payload": {
                    "durable_merge_authority": {
                        "schema_version": (
                            "contract_runtime.observer_merge_durable_authority.v1"
                        ),
                        "server_derived": True,
                        "db_verified": True,
                        "pre_qa_merge_authorized": True,
                        "final_qa_required_after_reconcile": True,
                        "project_id": "aming-claw",
                        "backlog_id": backlog_id,
                        "contract_execution_id": execution_id,
                        **worker,
                        "queue_item_id": f"mqi-rev9-reconcile-record-{index}",
                        "merge_commit": str(index) * 40,
                        "merge_event_ref": f"timeline:{200 + index}",
                        "merge_event_id": 200 + index,
                        "merge_event_created_at": (
                            f"2026-08-05T10:00:0{index}Z"
                        ),
                        "contract_runtime_dispatch_source_ref": dispatch_ref,
                    }
                },
            }
        )
    record = {
        "project_id": "aming-claw",
        "backlog_id": backlog_id,
        "contract_id": "mf_parallel.v2",
        "version": "v2",
        "revision": "rev9",
        "contract_execution_id": execution_id,
        "completed_lines": completed_lines,
    }
    final_context = SimpleNamespace(
        **workers[-1],
        backlog_id=backlog_id,
        batch_id="",
    )
    resolver_calls = []

    monkeypatch.setattr(
        server,
        "_contract_runtime_contexts_for_dispatch_line",
        lambda *_args, **_kwargs: [final_context],
    )
    monkeypatch.setattr(
        server,
        "_runtime_context_service_timeline_events",
        lambda *_args, **_kwargs: [
            {
                "id": 203,
                "project_id": "aming-claw",
                "backlog_id": backlog_id,
                "task_id": workers[-1]["task_id"],
                "event_type": "graph.reconcile",
                "event_kind": "reconcile",
                "phase": "reconcile",
                "status": "passed",
                "payload": {
                    "actor_role": "observer",
                    "runtime_context_id": workers[-1][
                        "runtime_context_id"
                    ],
                    "task_id": workers[-1]["task_id"],
                },
                "created_at": "2026-08-05T10:00:03Z",
            }
        ],
    )

    def resolve_reconcile(
        _conn,
        *,
        project_id,
        record,
        context,
        timeline_events,
        merge,
    ):
        resolver_calls.append(
            {
                "project_id": project_id,
                "context": context.runtime_context_id,
                "timeline_event_ids": [event["id"] for event in timeline_events],
                "merge": dict(merge),
            }
        )
        return {
            **merge,
            "reconcile_source_ref": "timeline:203",
            "reconcile_event_id": 203,
            "reconcile_event_created_at": "2026-08-05T10:00:03Z",
            "reconcile_task_id": context.task_id,
            "reconcile_runtime_context_id": context.runtime_context_id,
        }

    monkeypatch.setattr(
        server,
        "_contract_runtime_completed_merge_reconcile_authority",
        resolve_reconcile,
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_current_full_reconcile_authority_from_merge",
        lambda *_args, **_kwargs: {},
    )

    receipt = server._contract_runtime_reconcile_record_authority(
        object(),
        project_id="aming-claw",
        record=record,
    )

    assert len(resolver_calls) == 1
    assert resolver_calls[0]["project_id"] == "aming-claw"
    assert resolver_calls[0]["context"] == workers[-1]["runtime_context_id"]
    assert resolver_calls[0]["timeline_event_ids"] == [203]
    assert resolver_calls[0]["merge"]["all_lane_merges_verified"] is True
    assert receipt["record_verified"] is True
    assert receipt["reconcile_event_recorded"] is True
    assert receipt["reconcile_event_id"] == 203
    assert receipt["reconcile_source_ref"] == "timeline:203"


def test_current_full_reconcile_event_declares_authenticated_observer_role(
    monkeypatch,
):
    from agent.governance import task_timeline

    captured = {}

    def capture_event(_conn, **kwargs):
        captured.update(kwargs)
        return {"id": 301, "created_at": "2026-08-05T10:30:00Z", **kwargs}

    monkeypatch.setattr(task_timeline, "record_event", capture_event)
    event = server._record_pending_scope_reconcile_contract_event(
        None,
        project_id="aming-claw",
        body={
            "backlog_id": "AC-DECLARED-RECONCILE-ROLE",
            "task_id": "worker-declared-reconcile-role",
            "actor": "codex-observer",
        },
        result={
            "ok": True,
            "status": "complete",
            "snapshot_id": "full-declared-reconcile-role",
            "active_snapshot_id": "full-declared-reconcile-role",
            "current_full_reconcile": True,
            "strategy": "current_full_reconcile",
            "head_commit": "a" * 40,
            "active_graph_commit": "a" * 40,
            "activated": True,
            "activation_verification": {
                "verified": True,
                "active_graph_commit": "a" * 40,
                "active_snapshot_id": "full-declared-reconcile-role",
            },
        },
        target_commit_sha="a" * 40,
        runtime_context_scope={
            "project_id": "aming-claw",
            "backlog_id": "AC-DECLARED-RECONCILE-ROLE",
            "task_id": "worker-declared-reconcile-role",
            "parent_task_id": "cex-declared-reconcile-role",
            "runtime_context_id": "mfrctx-declared-reconcile-role",
            "merge_queue_id": "mq-declared-reconcile-role",
            "contract_execution_id": "cex-declared-reconcile-role",
        },
        declared_actor_role="observer",
        post_commit_hooks=False,
    )

    assert captured["actor"] == "codex-observer"
    assert captured["payload"]["actor_role"] == "observer"
    assert server._contract_runtime_declared_timeline_actor_role(event) == (
        "observer"
    )
    roleless = deepcopy(event)
    roleless["payload"].pop("actor_role")
    assert roleless["actor"] == "codex-observer"
    assert server._contract_runtime_declared_timeline_actor_role(roleless) == ""


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

        def current_record(self, *_args, **_kwargs):
            return record

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


_CLOSE_GRADE_PROJECT_ID = "aming-claw"
_CLOSE_GRADE_BACKLOG_ID = "AC-REV9-CLOSE-GRADE-RECONCILE-AUTHORITY"
_CLOSE_GRADE_EXECUTION_ID = "cex-mf-parallel-close-grade-authority"
_CLOSE_GRADE_BASE_COMMIT = "0" * 40
_CLOSE_GRADE_LANE_A_COMMIT = "1" * 40
_CLOSE_GRADE_LANE_B_COMMIT = "2" * 40


class _CloseGradeNoCloseConn:
    """Hand the shared in-memory connection to server-side helpers."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self) -> None:
        return None


def _close_grade_graph_payload() -> dict:
    return {
        "deps_graph": {
            "nodes": [{"id": "n1", "kind": "module", "path": "planner.py"}],
            "edges": [],
        }
    }


def _close_grade_connection(tmp_path, monkeypatch) -> sqlite3.Connection:
    from agent.governance.db import _ensure_schema

    monkeypatch.setattr(
        "agent.governance.db._governance_root",
        lambda: tmp_path / "state",
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    graph_snapshot_store.ensure_schema(conn)
    monkeypatch.setattr(
        server,
        "get_connection",
        lambda _project_id: _CloseGradeNoCloseConn(conn),
    )
    monkeypatch.setattr(
        "agent.governance.db.get_connection",
        lambda _project_id: _CloseGradeNoCloseConn(conn),
    )
    return conn


def _close_grade_lane_context(conn, *, task_id, merge_queue_id, project_root):
    return parallel_branch_runtime.upsert_branch_context(
        conn,
        parallel_branch_runtime.BranchTaskRuntimeContext(
            project_id=_CLOSE_GRADE_PROJECT_ID,
            task_id=task_id,
            parent_task_id=_CLOSE_GRADE_EXECUTION_ID,
            root_task_id=_CLOSE_GRADE_EXECUTION_ID,
            backlog_id=_CLOSE_GRADE_BACKLOG_ID,
            worker_id=f"worker-{task_id}",
            worker_slot_id=f"slot-{task_id}",
            governance_project_id=_CLOSE_GRADE_PROJECT_ID,
            target_project_id=_CLOSE_GRADE_PROJECT_ID,
            target_project_root=str(project_root),
            branch_ref=f"refs/heads/codex/{task_id}",
            worktree_path=str(project_root),
            base_commit=_CLOSE_GRADE_BASE_COMMIT,
            target_head_commit=_CLOSE_GRADE_BASE_COMMIT,
            merge_queue_id=merge_queue_id,
            status="worktree_ready",
            lease_expires_at="2999-01-01T00:00:00Z",
        ),
        now_iso="2026-08-06T10:00:00Z",
    )


def _close_grade_lane_merge_event(conn, *, context, commit_sha):
    return task_timeline.record_event(
        conn,
        project_id=_CLOSE_GRADE_PROJECT_ID,
        backlog_id=_CLOSE_GRADE_BACKLOG_ID,
        task_id=context.task_id,
        event_type="merge.live",
        event_kind="merge",
        phase="merge",
        actor="observer:rev9-close-grade",
        status="passed",
        commit_sha=commit_sha,
        payload={
            "actor_role": "observer",
            "backlog_id": _CLOSE_GRADE_BACKLOG_ID,
            "contract_execution_id": _CLOSE_GRADE_EXECUTION_ID,
            "runtime_context_id": context.runtime_context_id,
            "task_id": context.task_id,
            "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
            "merge_queue_id": context.merge_queue_id,
            "merge_commit": commit_sha,
            "target_head_after_merge": commit_sha,
        },
    )


def _close_grade_durable_merge_line(
    *,
    context,
    merge_event,
    commit_sha,
    target_head_before_merge,
    dispatch_source_ref,
):
    return {
        "stage_id": "observer_lane_merge",
        "line_id": "observer_merge",
        "actor_role": "observer",
        "evidence_kind": "merge",
        "line_instance_id": f"runtime_context:{context.runtime_context_id}",
        "runtime_context_id": context.runtime_context_id,
        "task_id": context.task_id,
        "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
        "commit_sha": commit_sha,
        "payload": {
            "durable_merge_authority": {
                "schema_version": (
                    "contract_runtime.observer_merge_durable_authority.v1"
                ),
                "source": (
                    "parallel_branch_merge_queue+task_timeline_merge"
                ),
                "server_derived": True,
                "db_verified": True,
                "project_id": _CLOSE_GRADE_PROJECT_ID,
                "backlog_id": _CLOSE_GRADE_BACKLOG_ID,
                "contract_execution_id": _CLOSE_GRADE_EXECUTION_ID,
                "runtime_context_id": context.runtime_context_id,
                "task_id": context.task_id,
                "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
                "merge_queue_id": context.merge_queue_id,
                "queue_item_id": (
                    f"{context.merge_queue_id}:{context.task_id}"
                ),
                "queue_item_status": "merged",
                "branch_head": commit_sha,
                "merge_commit": commit_sha,
                "target_head_before_merge": target_head_before_merge,
                "target_head_after_merge": commit_sha,
                "merge_gate_passed": True,
                "merge_event_ref": f"timeline:{merge_event['id']}",
                "merge_event_id": int(merge_event["id"]),
                "merge_event_created_at": str(merge_event["created_at"]),
                "timeline_event_refs": [f"timeline:{merge_event['id']}"],
                "contract_runtime_dispatch_source_ref": dispatch_source_ref,
                # rev8/rev9 merge every lane before canonical reconcile and
                # run the integration QA afterwards.
                "pre_qa_merge_authorized": True,
                "final_qa_required_after_reconcile": True,
                "qa_contract_runtime_verified": False,
                "qa_completed_line_index": -1,
                "qa_graph_completed_line_index": -1,
                "qa_acceptance_ref": "",
                "qa_audit_only_no_pass_authority": {},
                "close_satisfying": False,
                "no_pass_claim": False,
                "overall_release_pass_claimed": False,
                "authoritative_pass_synthesized": False,
                "worker_commit_completed_line_index": -1,
            },
            "merge_commit": commit_sha,
            "merge_gate_passed": True,
            "merge_queue_id": context.merge_queue_id,
            "runtime_context_id": context.runtime_context_id,
            "task_id": context.task_id,
            "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
        },
    }


def _close_grade_worker_lines(*, context, commit_sha):
    lines = []
    for stage_id, line_id, evidence_kind, status in (
        ("worker_read", "worker_read_runtime_guide", "read_receipt", "accepted"),
        ("worker_startup", "worker_startup", "mf_subagent_startup", "passed"),
        ("worker_context", "worker_graph_context", "graph_trace", ""),
        (
            "worker_implementation",
            "worker_implementation",
            "implementation",
            "passed",
        ),
        ("worker_commit", "worker_commit", "worker_commit", ""),
        (
            "worker_attestation",
            "worker_finish_time_attestation",
            "record_finish_time_worker_attestation",
            "",
        ),
        ("worker_finish", "worker_finish_gate", "mf_subagent_finish_gate", ""),
    ):
        line = {
            "stage_id": stage_id,
            "line_id": line_id,
            "actor_role": "mf_sub",
            "worker_role": "mf_sub",
            "evidence_kind": evidence_kind,
            "line_instance_id": (
                f"runtime_context:{context.runtime_context_id}"
            ),
            "runtime_context_id": context.runtime_context_id,
            "task_id": context.task_id,
            "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
            "payload": {
                "runtime_context_id": context.runtime_context_id,
                "task_id": context.task_id,
                "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
            },
        }
        if status:
            line["status"] = status
        if line_id in {"worker_commit", "worker_finish_gate"}:
            line["commit_sha"] = commit_sha
            line["payload"]["commit_sha"] = commit_sha
        lines.append(line)
    return lines


def _rev9_close_grade_world(
    tmp_path,
    monkeypatch,
    *,
    record_reconcile_event: bool = True,
    reconcile_before_final_lane_merge: bool = False,
    reconcile_scope_execution_id: str = "",
):
    """Build one real rev9 post-merge world through the server projections.

    Every authority here is derived by the shipped server code from durable
    SQLite state: runtime contexts, task timeline events, the active graph
    snapshot, and its current-full reconcile provenance.  Nothing patches an
    authority producer and no reconcile identity is hand-fed onto a line.
    """

    conn = _close_grade_connection(tmp_path, monkeypatch)
    project_root = tmp_path / "rev9-close-grade-target"
    project_root.mkdir()
    monkeypatch.setattr(
        server.project_service,
        "resolve_project_root",
        lambda *_args, **_kwargs: project_root,
    )
    monkeypatch.setattr(
        server,
        "_git_head_commit",
        lambda _root: _CLOSE_GRADE_LANE_B_COMMIT,
    )
    ancestry = {
        (_CLOSE_GRADE_BASE_COMMIT, _CLOSE_GRADE_LANE_A_COMMIT),
        (_CLOSE_GRADE_LANE_A_COMMIT, _CLOSE_GRADE_LANE_B_COMMIT),
        (_CLOSE_GRADE_BASE_COMMIT, _CLOSE_GRADE_LANE_B_COMMIT),
    }
    monkeypatch.setattr(
        server,
        "_git_commit_is_ancestor",
        lambda _root, ancestor, descendant: (
            ancestor == descendant or (ancestor, descendant) in ancestry
        ),
    )

    lane_a = _close_grade_lane_context(
        conn,
        task_id="rev9-close-grade-models",
        merge_queue_id="mq-rev9-close-grade-models",
        project_root=project_root,
    )
    lane_b = _close_grade_lane_context(
        conn,
        task_id="rev9-close-grade-planner",
        merge_queue_id="mq-rev9-close-grade-planner",
        project_root=project_root,
    )

    merge_a = _close_grade_lane_merge_event(
        conn,
        context=lane_a,
        commit_sha=_CLOSE_GRADE_LANE_A_COMMIT,
    )
    reconcile_event = None
    runtime_scope = {
        "project_id": _CLOSE_GRADE_PROJECT_ID,
        "backlog_id": _CLOSE_GRADE_BACKLOG_ID,
        "contract_execution_id": (
            reconcile_scope_execution_id or _CLOSE_GRADE_EXECUTION_ID
        ),
        "runtime_context_id": lane_b.runtime_context_id,
        "task_id": lane_b.task_id,
        "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
        "merge_queue_id": lane_b.merge_queue_id,
        "source": "parallel_branch_runtime_context",
        "server_derived": True,
    }

    def _record_reconcile_event():
        return task_timeline.record_event(
            conn,
            project_id=_CLOSE_GRADE_PROJECT_ID,
            backlog_id=_CLOSE_GRADE_BACKLOG_ID,
            task_id=lane_b.task_id,
            event_type="graph.reconcile",
            event_kind="reconcile",
            phase="reconcile",
            actor="observer:rev9-close-grade",
            status="passed",
            commit_sha=_CLOSE_GRADE_LANE_B_COMMIT,
            payload={
                "actor_role": "observer",
                "backlog_id": _CLOSE_GRADE_BACKLOG_ID,
                "contract_execution_id": _CLOSE_GRADE_EXECUTION_ID,
                "runtime_context_id": lane_b.runtime_context_id,
                "task_id": lane_b.task_id,
                "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
                "merge_queue_id": lane_b.merge_queue_id,
                "runtime_context_scope": runtime_scope,
                "current_full_reconcile": True,
                "reconcile_mode": "current_full",
            },
        )

    # A reconcile recorded before the final lane merge is durably out of
    # order; it is written first so its timeline id proves that.
    if record_reconcile_event and reconcile_before_final_lane_merge:
        reconcile_event = _record_reconcile_event()
    merge_b = _close_grade_lane_merge_event(
        conn,
        context=lane_b,
        commit_sha=_CLOSE_GRADE_LANE_B_COMMIT,
    )
    if record_reconcile_event and not reconcile_before_final_lane_merge:
        reconcile_event = _record_reconcile_event()

    event_times = [
        (merge_a, "2026-08-06T16:00:27Z"),
        (merge_b, "2026-08-06T16:00:29Z"),
    ]
    if reconcile_event is not None:
        event_times.append(
            (
                reconcile_event,
                "2026-08-06T16:00:28Z"
                if reconcile_before_final_lane_merge
                else "2026-08-06T16:01:26Z",
            )
        )
    for event, created_at in event_times:
        conn.execute(
            "UPDATE task_timeline_events SET created_at = ? WHERE id = ?",
            (created_at, int(event["id"])),
        )
        event["created_at"] = created_at
    conn.commit()

    snapshot = graph_snapshot_store.create_graph_snapshot(
        conn,
        _CLOSE_GRADE_PROJECT_ID,
        snapshot_id="full-rev9-close-grade-head",
        commit_sha=_CLOSE_GRADE_LANE_B_COMMIT,
        snapshot_kind="full",
        graph_json=_close_grade_graph_payload(),
    )
    graph_snapshot_store.index_graph_snapshot(
        conn,
        _CLOSE_GRADE_PROJECT_ID,
        snapshot["snapshot_id"],
        nodes=_close_grade_graph_payload()["deps_graph"]["nodes"],
        edges=_close_grade_graph_payload()["deps_graph"]["edges"],
    )
    graph_snapshot_store.activate_graph_snapshot(
        conn,
        _CLOSE_GRADE_PROJECT_ID,
        snapshot["snapshot_id"],
    )
    conn.commit()
    if reconcile_event is not None:
        graph_snapshot_store.record_current_full_reconcile_provenance(
            conn,
            project_id=_CLOSE_GRADE_PROJECT_ID,
            snapshot_id=snapshot["snapshot_id"],
            target_commit_sha=_CLOSE_GRADE_LANE_B_COMMIT,
            request_id=f"req-{_CLOSE_GRADE_EXECUTION_ID}",
            request_started_at=str(merge_b["created_at"]),
            route_evidence={
                "schema_version": (
                    "graph_current_full_reconcile.route_evidence.v1"
                ),
                "authenticated_role": "observer",
                "authentication_source": "test_protected_entrypoint",
                "raw_route_token_persisted": False,
                "protected_action": "graph_current_full_reconcile",
                "contract_execution_id": (
                    reconcile_scope_execution_id or _CLOSE_GRADE_EXECUTION_ID
                ),
                "runtime_context_id": lane_b.runtime_context_id,
                "task_id": lane_b.task_id,
                "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
                "merge_queue_id": lane_b.merge_queue_id,
                "route_token_scope": {
                    "project_id": _CLOSE_GRADE_PROJECT_ID,
                    "backlog_id": _CLOSE_GRADE_BACKLOG_ID,
                    "task_id": lane_b.task_id,
                    "runtime_context_id": lane_b.runtime_context_id,
                },
                "runtime_context_scope": runtime_scope,
            },
            runtime_context_scope=runtime_scope,
            reconcile_event_id=int(reconcile_event["id"]),
            reconcile_event_created_at=str(reconcile_event["created_at"]),
        )
        conn.commit()

    workers = [
        {
            "runtime_context_id": lane.runtime_context_id,
            "task_id": lane.task_id,
            "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
            "worker_id": lane.worker_id,
            "worker_slot_id": lane.worker_slot_id,
            "merge_queue_id": lane.merge_queue_id,
        }
        for lane in (lane_a, lane_b)
    ]
    dispatch_source_ref = (
        f"contract_runtime:{_CLOSE_GRADE_EXECUTION_ID}:completed_lines:0"
    )
    completed_lines = [
        {
            "stage_id": "dispatch",
            "line_id": "observer_dispatch_bounded_workers",
            "actor_role": "observer",
            "evidence_kind": "dispatch_bounded_worker",
            "payload": {
                "worker_count": 2,
                "required_worker_count": 2,
                "atomic_dispatch": True,
                "bounded_workers": workers,
            },
        }
    ]
    for lane, commit_sha in (
        (lane_a, _CLOSE_GRADE_LANE_A_COMMIT),
        (lane_b, _CLOSE_GRADE_LANE_B_COMMIT),
    ):
        completed_lines.extend(
            _close_grade_worker_lines(context=lane, commit_sha=commit_sha)
        )
    for lane, merge_event, commit_sha, before in (
        (lane_a, merge_a, _CLOSE_GRADE_LANE_A_COMMIT, _CLOSE_GRADE_BASE_COMMIT),
        (
            lane_b,
            merge_b,
            _CLOSE_GRADE_LANE_B_COMMIT,
            _CLOSE_GRADE_LANE_A_COMMIT,
        ),
    ):
        completed_lines.append(
            _close_grade_durable_merge_line(
                context=lane,
                merge_event=merge_event,
                commit_sha=commit_sha,
                target_head_before_merge=before,
                dispatch_source_ref=dispatch_source_ref,
            )
        )
    record = {
        "project_id": _CLOSE_GRADE_PROJECT_ID,
        "backlog_id": _CLOSE_GRADE_BACKLOG_ID,
        "contract_id": "mf_parallel.v2",
        "version": "v2",
        "revision": "rev9",
        "contract_execution_id": _CLOSE_GRADE_EXECUTION_ID,
        "completed_lines": completed_lines,
    }

    # The observer_reconcile line carries exactly what the server records at
    # reconcile time: the record-grade receipt it derives from durable state.
    reconcile_receipt = server._contract_runtime_reconcile_record_authority(
        conn,
        project_id=_CLOSE_GRADE_PROJECT_ID,
        record=record,
    )
    completed_lines.append(
        {
            "stage_id": "observer_reconcile",
            "line_id": "observer_reconcile",
            "actor_role": "observer",
            "evidence_kind": "reconcile",
            "line_instance_id": f"runtime_context:{lane_b.runtime_context_id}",
            "runtime_context_id": lane_b.runtime_context_id,
            "task_id": lane_b.task_id,
            "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
            "commit_sha": _CLOSE_GRADE_LANE_B_COMMIT,
            "payload": {"reconcile_authority": reconcile_receipt},
        }
    )
    completed_lines.append(
        {
            "stage_id": "qa_graph_context",
            "line_id": "qa_graph_context",
            "actor_role": "qa",
            "evidence_kind": "graph_trace",
            "line_instance_id": f"runtime_context:{lane_b.runtime_context_id}",
            "runtime_context_id": lane_b.runtime_context_id,
            "task_id": lane_b.task_id,
            "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
            "payload": {
                "graph_trace_evidence": {
                    "db_verified": True,
                    "identity_mismatches": [],
                    "verified_trace_ids": ["gqt-rev9-close-grade"],
                    "trace_ids": ["gqt-rev9-close-grade"],
                }
            },
        }
    )
    completed_lines.append(
        {
            "stage_id": "qa",
            "line_id": "qa_independent_verification",
            "actor_role": "qa",
            "evidence_kind": "independent_verification",
            "status": "pass",
            "line_instance_id": f"runtime_context:{lane_b.runtime_context_id}",
            "runtime_context_id": lane_b.runtime_context_id,
            "task_id": lane_b.task_id,
            "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
            "commit_sha": _CLOSE_GRADE_LANE_B_COMMIT,
            "payload": {
                "verdict": "pass",
                "candidate_new_failures": 0,
                "verified_commit": _CLOSE_GRADE_LANE_B_COMMIT,
            },
        }
    )
    close_ready_write = {
        "stage_id": "observer_close",
        "line_id": "observer_close_ready",
        "actor_role": "observer",
        "evidence_kind": "close_ready",
        "status": "pass",
        "commit_sha": _CLOSE_GRADE_LANE_B_COMMIT,
        "runtime_context_id": lane_b.runtime_context_id,
        "task_id": _CLOSE_GRADE_EXECUTION_ID,
        "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
        "payload": {
            "close_commit": _CLOSE_GRADE_LANE_B_COMMIT,
            "verdict": "pass",
            "runtime_context_id": lane_b.runtime_context_id,
            "worker_task_id": lane_b.task_id,
            "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
        },
    }
    return SimpleNamespace(
        conn=conn,
        record=record,
        reconcile_receipt=reconcile_receipt,
        close_ready_write=close_ready_write,
        lane_a=lane_a,
        lane_b=lane_b,
        merge_a=merge_a,
        merge_b=merge_b,
        reconcile_event=reconcile_event,
    )


def _close_grade_reconcile_diagnostic(world):
    bound = server._contract_runtime_bind_close_reconcile_authority(
        world.conn,
        project_id=_CLOSE_GRADE_PROJECT_ID,
        record=world.record,
    )
    reconcile_line = next(
        line
        for line in bound["completed_lines"]
        if str(line.get("line_id") or "") == "observer_reconcile"
    )
    return bound, server._contract_runtime_mf_parallel_reconcile_close_diagnostic(
        bound,
        reconcile_line,
    )


def _close_grade_close_ready_gate(world):
    return server._contract_runtime_mf_parallel_close_ready_precheck(
        world.record,
        world.close_ready_write,
        conn=world.conn,
        project_id=_CLOSE_GRADE_PROJECT_ID,
    )


def _reconcile_requirement_ids(gate):
    return [
        requirement_id
        for requirement_id in gate.get("missing_requirement_ids") or []
        if str(requirement_id).startswith("contract_runtime.reconcile_")
    ]


def test_rev9_close_grade_reconcile_authority_binds_after_postmerge_qa_pass(
    tmp_path,
    monkeypatch,
):
    """Regression: observer_close_ready was unsatisfiable on every rev9 world.

    rev8/rev9 merge both lanes before canonical reconcile and run the
    integration QA afterwards, so their merge lines carry the server-owned
    pre-QA marker.  The close binder used to read only
    ``_contract_runtime_trusted_merge_projection``, which cannot rebuild that
    shape, so close-grade authority derived nothing while the record-grade
    receipt on the same execution was fully bound.  Before the fix this test
    fails with the full ``contract_runtime.reconcile_*`` family that
    ``contract_runtime_close_authority_incomplete`` reports.
    """

    world = _rev9_close_grade_world(tmp_path, monkeypatch)

    # Record-grade authority is derived, not fed: it is what the server
    # persists on observer_reconcile.
    receipt = world.reconcile_receipt
    assert receipt["record_verified"] is True
    assert receipt["merge_projection_verified"] is True
    assert receipt["all_lane_merges_verified"] is True
    assert receipt["reconcile_event_recorded"] is True
    assert receipt["reconcile_event_id"] == int(world.reconcile_event["id"])
    assert receipt["reconcile_source_ref"] == (
        f"timeline:{world.reconcile_event['id']}"
    )
    assert receipt["merge_event_id"] == int(world.merge_b["id"])
    assert receipt["current_full_reconcile_activation_verified"] is True
    assert receipt["close_grade_authority_deferred"] is True

    _bound, diagnostic = _close_grade_reconcile_diagnostic(world)
    assert diagnostic["missing_requirement_ids"] == []
    assert diagnostic["passed"] is True
    assert diagnostic["reconcile_event_id"] == int(
        world.reconcile_event["id"]
    )
    assert diagnostic["next_action"] == "continue_close"

    gate = _close_grade_close_ready_gate(world)
    assert _reconcile_requirement_ids(gate) == []
    assert gate["missing_requirement_ids"] == []
    assert gate["passed"] is True


def test_rev9_close_grade_reconcile_authority_fails_closed_without_reconcile_event(
    tmp_path,
    monkeypatch,
):
    """No durable reconcile event means no close-grade authority."""

    world = _rev9_close_grade_world(
        tmp_path,
        monkeypatch,
        record_reconcile_event=False,
    )
    assert world.reconcile_receipt["record_verified"] is True
    assert world.reconcile_receipt["reconcile_event_recorded"] is False

    _bound, diagnostic = _close_grade_reconcile_diagnostic(world)
    assert diagnostic["passed"] is False
    assert "contract_runtime.reconcile_event_missing" in (
        diagnostic["missing_requirement_ids"]
    )

    gate = _close_grade_close_ready_gate(world)
    assert gate["passed"] is False
    assert gate["zero_write_rejection"] is True
    assert "contract_runtime.reconcile_event_missing" in (
        gate["missing_requirement_ids"]
    )


def test_rev9_close_grade_reconcile_authority_fails_closed_before_final_lane_merge(
    tmp_path,
    monkeypatch,
):
    """A reconcile that predates the final lane merge is never close-grade."""

    world = _rev9_close_grade_world(
        tmp_path,
        monkeypatch,
        reconcile_before_final_lane_merge=True,
    )
    assert int(world.reconcile_event["id"]) < int(world.merge_b["id"])
    assert world.reconcile_receipt["record_verified"] is True
    assert world.reconcile_receipt["reconcile_event_recorded"] is False

    _bound, diagnostic = _close_grade_reconcile_diagnostic(world)
    assert diagnostic["passed"] is False
    assert "contract_runtime.reconcile_event_missing" in (
        diagnostic["missing_requirement_ids"]
    )
    assert "contract_runtime.reconcile_durable_order" in (
        diagnostic["missing_requirement_ids"]
    )

    gate = _close_grade_close_ready_gate(world)
    assert gate["passed"] is False
    assert gate["zero_write_rejection"] is True
    assert "contract_runtime.reconcile_durable_order" in (
        gate["missing_requirement_ids"]
    )


def test_rev9_close_grade_reconcile_authority_fails_closed_on_foreign_execution(
    tmp_path,
    monkeypatch,
):
    """Reconcile provenance scoped to another execution stays unusable."""

    world = _rev9_close_grade_world(
        tmp_path,
        monkeypatch,
        reconcile_scope_execution_id="cex-mf-parallel-some-other-execution",
    )

    _bound, diagnostic = _close_grade_reconcile_diagnostic(world)
    assert diagnostic["passed"] is False
    assert "contract_runtime.reconcile_contract_execution_scope" in (
        diagnostic["missing_requirement_ids"]
    )
    assert "contract_runtime.reconcile_provenance_scope" in (
        diagnostic["missing_requirement_ids"]
    )

    gate = _close_grade_close_ready_gate(world)
    assert gate["passed"] is False
    assert gate["zero_write_rejection"] is True
    assert "contract_runtime.reconcile_contract_execution_scope" in (
        gate["missing_requirement_ids"]
    )
