"""Executable dry-run scenarios for parallel branch merge queue decisions."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from agent.governance import parallel_branch_runtime as pbr
from agent.tests.fixtures.parallel_project import create_merge_preview_fixture_project
from agent.governance.parallel_branch_runtime import (
    ACTION_ALLOW_MERGE,
    ACTION_BLOCKED_BY_DEPENDENCY,
    ACTION_LEAVE_MERGED,
    ACTION_NOOP,
    ACTION_OPERATOR_APPROVE_LIVE_MERGE,
    ACTION_REVALIDATE_AFTER_DEPENDENCY_MERGE,
    ACTION_WAIT_FOR_DEPENDENCY,
    BATCH_STATE_ROLLBACK_REQUIRED,
    BranchRuntimeFenceError,
    BranchTaskRuntimeContext,
    MERGE_GATE_REQUIRED_EVIDENCE,
    STATE_DEPENDENCY_BLOCKED,
    STATE_MERGE_READY,
    STATE_MERGED,
    STATE_QUEUED_FOR_MERGE,
    STATE_RUNNING,
    STATE_STALE_AFTER_DEPENDENCY_MERGE,
    STATE_WAITING_DEPENDENCY,
    MergeQueueItem,
    IntegrationEpoch,
    IntegrationEpochFrozenError,
    INTEGRATION_EPOCH_MERGE_IN_DOUBT,
    INTEGRATION_EPOCH_RECONCILED,
    advance_integration_epoch_after_merge,
    arm_integration_epoch_merge_in_doubt,
    close_integration_epoch,
    _merge_queue_item_matches_reconciled_head,
    decide_merge_gate,
    decide_merge_queue,
    decide_persisted_merge_gate,
    decide_persisted_merge_queue,
    execute_merge_queue_item,
    get_active_integration_epoch,
    get_branch_context,
    git_merge_preview_evidence,
    integration_epoch_resume_payload,
    list_merge_queue_items,
    merge_gate_plan_to_dict,
    open_or_validate_integration_epoch,
    record_merge_queue_graph_epoch_after_reconcile,
    record_merge_queue_result,
    upsert_branch_context,
    upsert_integration_epoch,
    validate_integration_epoch_backlog_close,
    upsert_merge_queue_items,
)

PROJECT_ID = "fixture-parallel-project"
QUEUE_ID = "mergeq-PB002"
TARGET_REF = "refs/heads/main"


def _by_task(plan):
    return {decision.task_id: decision for decision in plan.decisions}


def _runtime_conn(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _passing_merge_evidence() -> dict[str, dict[str, str]]:
    return {
        key: {"status": "pass", "evidence_id": f"evidence-{key}"}
        for key in MERGE_GATE_REQUIRED_EVIDENCE
    }


def test_pb002_downstream_merge_waits_for_unmerged_foundation_dependency() -> None:
    """PB-002: downstream requests merge before upstream foundation is merged."""
    items = [
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id=QUEUE_ID,
            queue_item_id="item-T2",
            task_id="T2",
            branch_ref="refs/heads/codex/PB002-T2-dashboard-read-model",
            queue_index=2,
            status=STATE_MERGE_READY,
            depends_on=("T1",),
            target_ref=TARGET_REF,
            base_commit="target-base",
            branch_head="head-T2",
            validated_target_head="target-base",
            current_target_head="target-base",
            validation_attempt=1,
            merge_preview_id="preview-T2",
        ),
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id=QUEUE_ID,
            queue_item_id="item-T1",
            task_id="T1",
            branch_ref="refs/heads/codex/PB002-T1-scope-reconcile-foundation",
            queue_index=1,
            status=STATE_MERGE_READY,
            target_ref=TARGET_REF,
            base_commit="target-base",
            branch_head="head-T1",
            validated_target_head="target-base",
            current_target_head="target-base",
            validation_attempt=1,
            merge_preview_id="preview-T1",
        ),
    ]

    plan = decide_merge_queue(items, scenario_id="PB-002")
    decisions = _by_task(plan)

    assert [decision.task_id for decision in plan.decisions] == ["T1", "T2"]
    assert plan.mergeable_task_ids == ("T1",)
    assert plan.blocked_task_ids == ("T2",)
    assert plan.target_mutation_blocked_for == ("T2",)

    assert decisions["T1"].queue_state == STATE_MERGE_READY
    assert decisions["T1"].action == ACTION_ALLOW_MERGE
    assert decisions["T1"].merge_allowed is True
    assert decisions["T1"].target_branch_mutation_allowed is True

    assert decisions["T2"].queue_state == STATE_WAITING_DEPENDENCY
    assert decisions["T2"].action == ACTION_WAIT_FOR_DEPENDENCY
    assert decisions["T2"].dependency_blockers == ("T1",)
    assert decisions["T2"].dependency_blocker_types == {"T1": ("hard_depends_on",)}
    assert decisions["T2"].next_actions == ("wait_for_dependency", "do_not_merge")
    assert decisions["T2"].merge_allowed is False
    assert decisions["T2"].target_branch_mutation_allowed is False
    assert decisions["T2"].target_graph_activation_allowed is False
    assert decisions["T2"].target_semantic_activation_allowed is False


def test_materialized_noop_queue_status_is_not_live_apply_ready() -> None:
    item = MergeQueueItem(
        project_id=PROJECT_ID,
        merge_queue_id="mergeq-materialized-noop",
        queue_item_id="mqitem-materialized-noop",
        task_id="T-materialized-noop",
        branch_ref="refs/heads/codex/materialized-noop",
        queue_index=1,
        status="materialized",
        target_ref=TARGET_REF,
        base_commit="target-base",
        branch_head="head-materialized-noop",
        validated_target_head="target-base",
        current_target_head="target-base",
        merge_preview_id="preview-materialized-noop",
    )

    plan = decide_merge_queue([item], scenario_id="PB-materialized-noop")
    decision = plan.decisions[0]
    row = decision.to_read_model_row()

    assert decision.action == ACTION_NOOP
    assert decision.merge_allowed is False
    assert row["live_apply_ready"] is False
    assert row["durable_status_policy"]["close_satisfying"] is False
    assert row["required_statuses_before_live_apply"] == [
        "queued_for_merge",
        "merge_ready",
    ]
    recovery = row["copy_safe_recovery"]
    assert recovery["tool"] == "parallel_branch_merge_queue_materialize"
    assert recovery["tool_args"]["merge_queue_id"] == "mergeq-materialized-noop"
    assert recovery["tool_args"]["queue_item_id"] == "mqitem-materialized-noop"
    assert recovery["tool_args"]["status"] == "merge_ready"
    assert recovery["tool_args"]["validated_target_head"] == "target-base"
    assert recovery["tool_args"]["merge_preview_id"] == "preview-materialized-noop"
    assert recovery["route_local_merge_queue_id_diagnostics"][
        "route_local_merge_queue_id_allowed_for_materialize"
    ] is False


def test_terminal_merged_queue_status_is_close_satisfying_without_recovery() -> None:
    item = MergeQueueItem(
        project_id=PROJECT_ID,
        merge_queue_id="mergeq-terminal-merged",
        queue_item_id="mqitem-terminal-merged",
        task_id="T-terminal-merged",
        branch_ref="refs/heads/codex/terminal-merged",
        queue_index=1,
        status=STATE_MERGED,
        target_ref=TARGET_REF,
        base_commit="target-before",
        branch_head="branch-merged",
        validated_target_head="target-before",
        current_target_head="target-after",
        merge_commit="target-after",
    )

    plan = decide_merge_queue([item], scenario_id="mf_parallel-terminal")
    decision = plan.decisions[0]
    row = decision.to_read_model_row()

    assert decision.action == ACTION_LEAVE_MERGED
    assert decision.merge_allowed is False
    assert decision.target_branch_mutation_allowed is False
    assert row["live_apply_ready"] is False
    assert row["durable_status_policy"]["live_apply_ready"] is False
    assert row["durable_status_policy"]["terminal_close_satisfying"] is True
    assert row["durable_status_policy"]["close_satisfying"] is True
    assert "copy_safe_recovery" not in row


def test_mf_batch_parallel_uses_shared_terminal_and_pending_status_policy() -> None:
    items = [
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id="mergeq-batch-status-policy",
            queue_item_id="mqitem-batch-merged",
            task_id="T-batch-merged",
            branch_ref="refs/heads/codex/batch-merged",
            queue_index=1,
            status=STATE_MERGED,
            target_ref=TARGET_REF,
            merge_commit="target-after-merged",
        ),
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id="mergeq-batch-status-policy",
            queue_item_id="mqitem-batch-queued",
            task_id="T-batch-queued",
            branch_ref="refs/heads/codex/batch-queued",
            queue_index=2,
            status=STATE_QUEUED_FOR_MERGE,
            target_ref=TARGET_REF,
        ),
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id="mergeq-batch-status-policy",
            queue_item_id="mqitem-batch-ready",
            task_id="T-batch-ready",
            branch_ref="refs/heads/codex/batch-ready",
            queue_index=3,
            status=STATE_MERGE_READY,
            target_ref=TARGET_REF,
        ),
    ]

    rows = decide_merge_queue(
        items,
        scenario_id="mf_batch_parallel-status-policy",
    ).dashboard_rows
    merged, queued, merge_ready = rows

    assert merged["durable_status_policy"]["terminal_close_satisfying"] is True
    assert merged["durable_status_policy"]["live_apply_ready"] is False
    assert "copy_safe_recovery" not in merged
    for pending in (queued, merge_ready):
        assert pending["durable_status_policy"]["terminal_close_satisfying"] is False
        assert pending["durable_status_policy"]["live_apply_ready"] is True
        assert pending["durable_status_policy"]["close_satisfying"] is True
        assert "copy_safe_recovery" not in pending


def test_pb002_persisted_merge_queue_replays_dependency_blockers_after_restart(tmp_path) -> None:
    db_path = str(tmp_path / "runtime.db")
    conn = _runtime_conn(db_path)
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=QUEUE_ID,
                queue_item_id="item-T2",
                task_id="T2",
                branch_ref="refs/heads/codex/PB002-T2-dashboard-read-model",
                queue_index=2,
                status=STATE_MERGE_READY,
                hard_depends_on=("T1",),
                serializes_after=("T1",),
                requires_graph_epoch=("T1",),
                target_ref=TARGET_REF,
                base_commit="target-base",
                branch_head="head-T2",
                validated_target_head="target-base",
                current_target_head="target-base",
                validation_attempt=1,
                merge_preview_id="preview-T2",
                snapshot_id="scope-T2",
                projection_id="semproj-T2",
            ),
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=QUEUE_ID,
                queue_item_id="item-T1",
                task_id="T1",
                branch_ref="refs/heads/codex/PB002-T1-scope-reconcile-foundation",
                queue_index=1,
                status=STATE_RUNNING,
                target_ref=TARGET_REF,
                base_commit="target-base",
                branch_head="head-T1",
            ),
        ],
        now_iso="2026-05-17T06:00:00Z",
    )
    conn.commit()
    conn.close()

    restarted = _runtime_conn(db_path)
    persisted = list_merge_queue_items(restarted, PROJECT_ID, QUEUE_ID, target_ref=TARGET_REF)
    assert [item.task_id for item in persisted] == ["T1", "T2"]
    assert persisted[1].hard_depends_on == ("T1",)
    assert persisted[1].serializes_after == ("T1",)
    assert persisted[1].requires_graph_epoch == ("T1",)
    assert persisted[1].merge_preview_id == "preview-T2"
    assert persisted[1].snapshot_id == "scope-T2"
    assert persisted[1].projection_id == "semproj-T2"

    plan = decide_persisted_merge_queue(
        restarted,
        PROJECT_ID,
        QUEUE_ID,
        target_ref=TARGET_REF,
        scenario_id="PB-002",
    )
    decisions = _by_task(plan)

    assert plan.mergeable_task_ids == ()
    assert plan.blocked_task_ids == ("T2",)
    assert decisions["T2"].queue_state == STATE_DEPENDENCY_BLOCKED
    assert decisions["T2"].dependency_blocker_types == {
        "T1": ("hard_depends_on", "requires_graph_epoch", "serializes_after")
    }
    assert decisions["T2"].target_branch_mutation_allowed is False


def test_pb003_downstream_validated_branch_goes_stale_after_dependency_merge() -> None:
    """PB-003: dependency merge moves target head after downstream validation."""
    items = [
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id="mergeq-PB003",
            queue_item_id="item-T1",
            task_id="T1",
            branch_ref="refs/heads/codex/PB003-T1-scope-reconcile-foundation",
            queue_index=1,
            status=STATE_MERGED,
            target_ref=TARGET_REF,
            base_commit="target-base",
            branch_head="head-T1",
            current_target_head="target-after-T1",
        ),
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id="mergeq-PB003",
            queue_item_id="item-T2",
            task_id="T2",
            branch_ref="refs/heads/codex/PB003-T2-dashboard-read-model",
            queue_index=2,
            status=STATE_MERGE_READY,
            depends_on=("T1",),
            target_ref=TARGET_REF,
            base_commit="target-base",
            branch_head="head-T2",
            validated_target_head="target-base",
            current_target_head="target-after-T1",
            validation_attempt=1,
            merge_preview_id="preview-T2-before-T1",
            snapshot_id="scope-T2-before-T1",
            projection_id="semproj-T2-before-T1",
        ),
    ]

    plan = decide_merge_queue(items, scenario_id="PB-003")
    decisions = _by_task(plan)

    assert plan.mergeable_task_ids == ()
    assert plan.blocked_task_ids == ()
    assert plan.stale_task_ids == ("T2",)
    assert plan.target_mutation_blocked_for == ("T1", "T2")

    assert decisions["T1"].queue_state == STATE_MERGED
    assert decisions["T1"].target_graph_activation_allowed is True
    assert decisions["T1"].target_semantic_activation_allowed is True

    assert decisions["T2"].queue_state == STATE_STALE_AFTER_DEPENDENCY_MERGE
    assert decisions["T2"].action == ACTION_REVALIDATE_AFTER_DEPENDENCY_MERGE
    assert decisions["T2"].stale_target_head is True
    assert decisions["T2"].dependency_blockers == ()
    assert decisions["T2"].next_actions == (
        "rebase_or_sync",
        "run_scope_reconcile",
        "verify_semantic_projection",
        "refresh_merge_preview",
    )
    assert decisions["T2"].merge_allowed is False
    assert decisions["T2"].target_branch_mutation_allowed is False
    assert decisions["T2"].target_graph_activation_allowed is False
    assert decisions["T2"].target_semantic_activation_allowed is False


def test_pb003_persisted_merge_queue_rehydrates_stale_validation_after_restart(tmp_path) -> None:
    db_path = str(tmp_path / "runtime.db")
    conn = _runtime_conn(db_path)
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="mergeq-PB003",
                queue_item_id="item-T1",
                task_id="T1",
                branch_ref="refs/heads/codex/PB003-T1-scope-reconcile-foundation",
                queue_index=1,
                status=STATE_MERGED,
                target_ref=TARGET_REF,
                base_commit="target-base",
                branch_head="head-T1",
                current_target_head="target-after-T1",
                snapshot_id="scope-T1",
                projection_id="semproj-T1",
            ),
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="mergeq-PB003",
                queue_item_id="item-T2",
                task_id="T2",
                branch_ref="refs/heads/codex/PB003-T2-dashboard-read-model",
                queue_index=2,
                status=STATE_MERGE_READY,
                depends_on=("T1",),
                target_ref=TARGET_REF,
                base_commit="target-base",
                branch_head="head-T2",
                validated_target_head="target-base",
                current_target_head="target-after-T1",
                validation_attempt=1,
                merge_preview_id="preview-T2-before-T1",
                snapshot_id="scope-T2-before-T1",
                projection_id="semproj-T2-before-T1",
            ),
        ],
        now_iso="2026-05-17T06:05:00Z",
    )
    conn.commit()
    conn.close()

    restarted = _runtime_conn(db_path)
    plan = decide_persisted_merge_queue(
        restarted,
        PROJECT_ID,
        "mergeq-PB003",
        target_ref=TARGET_REF,
        scenario_id="PB-003",
    )
    decisions = _by_task(plan)

    assert plan.stale_task_ids == ("T2",)
    assert decisions["T1"].target_graph_activation_allowed is True
    assert decisions["T1"].target_semantic_activation_allowed is True
    assert decisions["T2"].queue_state == STATE_STALE_AFTER_DEPENDENCY_MERGE
    assert decisions["T2"].stale_target_head is True
    assert decisions["T2"].merge_preview_id == "preview-T2-before-T1"
    assert decisions["T2"].next_actions == (
        "rebase_or_sync",
        "run_scope_reconcile",
        "verify_semantic_projection",
        "refresh_merge_preview",
    )


def test_persisted_merge_queue_marks_supplied_target_head_drift(tmp_path) -> None:
    db_path = str(tmp_path / "runtime.db")
    conn = _runtime_conn(db_path)
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="mergeq-target-drift",
                queue_item_id="item-T1",
                task_id="T1",
                branch_ref="refs/heads/codex/T1",
                queue_index=1,
                status=STATE_MERGE_READY,
                target_ref=TARGET_REF,
                branch_head="head-T1",
                validated_target_head="target-before",
                current_target_head="target-before",
                merge_preview_id="preview-before",
            )
        ],
        now_iso="2026-05-17T09:10:00Z",
    )
    conn.commit()

    plan = decide_persisted_merge_queue(
        conn,
        PROJECT_ID,
        "mergeq-target-drift",
        target_ref=TARGET_REF,
        current_target_head="target-after",
        scenario_id="PB-target-drift",
    )
    decision = plan.decisions[0]

    assert plan.mergeable_task_ids == ()
    assert plan.stale_task_ids == ("T1",)
    assert decision.queue_state == STATE_STALE_AFTER_DEPENDENCY_MERGE
    assert decision.stale_target_head is True
    assert decision.merge_allowed is False
    assert decision.next_actions == (
        "rebase_or_sync",
        "run_scope_reconcile",
        "verify_semantic_projection",
        "refresh_merge_preview",
    )


def test_merge_queue_dashboard_rows_are_deterministic_and_reviewable() -> None:
    items = [
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id=QUEUE_ID,
            queue_item_id="item-T2",
            task_id="T2",
            branch_ref="refs/heads/codex/PB002-T2-dashboard-read-model",
            queue_index=2,
            status=STATE_MERGE_READY,
            depends_on=("T1",),
            target_ref=TARGET_REF,
        ),
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id=QUEUE_ID,
            queue_item_id="item-T1",
            task_id="T1",
            branch_ref="refs/heads/codex/PB002-T1-scope-reconcile-foundation",
            queue_index=1,
            status=STATE_MERGE_READY,
            target_ref=TARGET_REF,
        ),
    ]

    plan = decide_merge_queue(items, scenario_id="PB-002")

    assert [row["task_id"] for row in plan.dashboard_rows] == ["T1", "T2"]
    row = plan.dashboard_rows[1]
    expected = {
        "queue_item_id": "item-T2",
        "task_id": "T2",
        "branch_ref": "refs/heads/codex/PB002-T2-dashboard-read-model",
        "status": STATE_MERGE_READY,
        "observed_status": STATE_MERGE_READY,
        "queue_state": STATE_WAITING_DEPENDENCY,
        "action": ACTION_WAIT_FOR_DEPENDENCY,
        "dependency_blockers": ["T1"],
        "dependency_blocker_types": {"T1": ["hard_depends_on"]},
        "stale_target_head": False,
        "next_actions": ["wait_for_dependency", "do_not_merge"],
        "merge_allowed": False,
        "target_branch_mutation_allowed": False,
        "target_graph_activation_allowed": False,
        "target_semantic_activation_allowed": False,
        "validation_attempt": 0,
        "merge_preview_id": "",
    }
    for key, value in expected.items():
        assert row[key] == value
    assert row["durable_status_policy"]["live_apply_ready"] is True
    assert row["durable_status_policy"]["close_satisfying"] is True
    assert row["live_apply_ready"] is False
    assert row["required_statuses_before_live_apply"] == [
        "queued_for_merge",
        "merge_ready",
    ]
    assert "copy_safe_recovery" not in row


def test_typed_dependency_blockers_are_compact_and_merge_blocking() -> None:
    items = [
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id=QUEUE_ID,
            queue_item_id="item-T1",
            task_id="T1",
            branch_ref="refs/heads/codex/PB002-T1-foundation",
            queue_index=1,
            status=STATE_RUNNING,
            target_ref=TARGET_REF,
        ),
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id=QUEUE_ID,
            queue_item_id="item-T2",
            task_id="T2",
            branch_ref="refs/heads/codex/PB002-T2-feature",
            queue_index=2,
            status=STATE_MERGE_READY,
            target_ref=TARGET_REF,
            hard_depends_on=("T1",),
            serializes_after=("T1",),
            requires_graph_epoch=("T1",),
        ),
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id=QUEUE_ID,
            queue_item_id="item-T3",
            task_id="T3",
            branch_ref="refs/heads/codex/PB002-T3-independent",
            queue_index=3,
            status=STATE_MERGE_READY,
            target_ref=TARGET_REF,
        ),
    ]

    plan = decide_merge_queue(items, scenario_id="PB-002")
    decisions = _by_task(plan)

    assert decisions["T2"].queue_state == STATE_DEPENDENCY_BLOCKED
    assert decisions["T2"].action == ACTION_BLOCKED_BY_DEPENDENCY
    assert decisions["T2"].dependency_blockers == ("T1",)
    assert decisions["T2"].dependency_blocker_types == {
        "T1": ("hard_depends_on", "requires_graph_epoch", "serializes_after")
    }
    assert decisions["T2"].merge_allowed is False
    assert decisions["T3"].queue_state == STATE_MERGE_READY
    assert decisions["T3"].merge_allowed is True
    assert plan.mergeable_task_ids == ("T3",)


def test_graph_epoch_auto_record_unblocks_merged_dependency_after_reconcile() -> None:
    conn = _runtime_conn()
    merge_head = "merge-head-T1"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id="PB-graph-epoch",
            task_id="T1",
            branch_ref="refs/heads/codex/PB-graph-epoch-T1",
            status=STATE_MERGED,
            target_head_commit=merge_head,
            merge_queue_id=QUEUE_ID,
        ),
        now_iso="2026-05-17T06:10:00Z",
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=QUEUE_ID,
                queue_item_id="item-T1",
                task_id="T1",
                branch_ref="refs/heads/codex/PB-graph-epoch-T1",
                queue_index=1,
                status=STATE_MERGED,
                target_ref=TARGET_REF,
                current_target_head=merge_head,
                merge_commit=merge_head,
                target_head_after_merge=merge_head,
            ),
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=QUEUE_ID,
                queue_item_id="item-T2",
                task_id="T2",
                branch_ref="refs/heads/codex/PB-graph-epoch-T2",
                queue_index=2,
                status=STATE_MERGE_READY,
                target_ref=TARGET_REF,
                validated_target_head=merge_head,
                current_target_head=merge_head,
                requires_graph_epoch=("T1",),
                merge_preview_id="preview-T2",
            ),
        ],
        now_iso="2026-05-17T06:10:00Z",
    )

    before = _by_task(
        decide_persisted_merge_queue(
            conn,
            PROJECT_ID,
            QUEUE_ID,
            target_ref=TARGET_REF,
            scenario_id="PB-graph-epoch",
        )
    )
    row = before["T2"].to_read_model_row()
    assert before["T2"].dependency_blocker_types == {
        "T1": ("requires_graph_epoch",)
    }
    recovery = row["graph_epoch_recovery"]
    assert recovery["tool"] == "graph_current_full_reconcile"
    assert recovery["authoritative_runtime_merge_queue_id"] == QUEUE_ID
    assert recovery["authoritative_runtime_queue_item_id"] == "item-T1"
    assert recovery["tool_args"]["merge_queue_id"] == QUEUE_ID
    assert recovery["tool_args"]["queue_item_id"] == "item-T1"
    assert recovery["raw_route_token_included"] is False

    recorded = record_merge_queue_graph_epoch_after_reconcile(
        conn,
        project_id=PROJECT_ID,
        target_head_commit=merge_head,
        snapshot_id="full-merge-head-T1",
        projection_id="semproj-merge-head-T1",
        merge_queue_id=QUEUE_ID,
        queue_item_id="item-T1",
        now_iso="2026-05-17T06:11:00Z",
    )

    assert recorded["status"] == "recorded"
    assert recorded["updated_count"] == 1
    assert recorded["context_updated_count"] == 1
    persisted = {
        item.task_id: item
        for item in list_merge_queue_items(conn, PROJECT_ID, QUEUE_ID, target_ref=TARGET_REF)
    }
    assert persisted["T1"].snapshot_id == "full-merge-head-T1"
    assert persisted["T1"].projection_id == "semproj-merge-head-T1"
    context = get_branch_context(conn, PROJECT_ID, "T1")
    assert context is not None
    assert context.snapshot_id == "full-merge-head-T1"
    assert context.projection_id == "semproj-merge-head-T1"

    after = _by_task(
        decide_persisted_merge_queue(
            conn,
            PROJECT_ID,
            QUEUE_ID,
            target_ref=TARGET_REF,
            scenario_id="PB-graph-epoch",
        )
    )
    assert after["T2"].dependency_blockers == ()
    assert after["T2"].queue_state == STATE_MERGE_READY
    assert after["T2"].merge_allowed is True


def test_conflict_dependencies_require_operator_resolution() -> None:
    items = [
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id=QUEUE_ID,
            queue_item_id="item-T1",
            task_id="T1",
            branch_ref="refs/heads/codex/PB002-T1-node-change",
            queue_index=1,
            status=STATE_MERGE_READY,
            target_ref=TARGET_REF,
        ),
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id=QUEUE_ID,
            queue_item_id="item-T2",
            task_id="T2",
            branch_ref="refs/heads/codex/PB002-T2-conflicting-node-change",
            queue_index=2,
            status=STATE_MERGE_READY,
            target_ref=TARGET_REF,
            conflicts_with=("T1",),
            same_node_or_file_conflicts=("T1",),
        ),
    ]

    plan = decide_merge_queue(items, scenario_id="PB-002")
    decisions = _by_task(plan)

    assert decisions["T1"].merge_allowed is True
    assert decisions["T2"].queue_state == STATE_DEPENDENCY_BLOCKED
    assert decisions["T2"].action == ACTION_BLOCKED_BY_DEPENDENCY
    assert decisions["T2"].dependency_blocker_types == {
        "T1": ("conflicts_with", "same_node_or_file_conflict")
    }
    assert decisions["T2"].next_actions == ("resolve_dependency", "do_not_merge")


def test_merge_gate_blocks_target_mutation_until_evidence_is_complete() -> None:
    item = MergeQueueItem(
        project_id=PROJECT_ID,
        merge_queue_id="mergeq-gate",
        queue_item_id="item-T1",
        task_id="T1",
        branch_ref="refs/heads/codex/PB013-T1-ready",
        queue_index=1,
        status=STATE_MERGE_READY,
        target_ref=TARGET_REF,
        branch_head="head-T1",
        current_target_head="target-base",
        validated_target_head="target-base",
        merge_preview_id="preview-T1",
        snapshot_id="scope-T1",
        projection_id="semproj-T1",
    )

    missing = decide_merge_gate([item], task_id="T1", scenario_id="PB-013")

    assert missing.merge_gate_passed is False
    assert missing.merge_allowed is False
    assert missing.target_branch_mutation_allowed is False
    assert "missing_evidence:git_conflict_check" in missing.blocker_codes
    assert "provide_required_merge_evidence" in missing.next_actions

    dry_run = decide_merge_gate(
        [item],
        task_id="T1",
        evidence=_passing_merge_evidence(),
        scenario_id="PB-013",
    )

    assert dry_run.merge_gate_passed is True
    assert dry_run.merge_allowed is True
    assert dry_run.dry_run is True
    assert dry_run.target_branch_mutation_allowed is False
    assert dry_run.target_graph_activation_allowed is False
    assert dry_run.next_actions == (ACTION_OPERATOR_APPROVE_LIVE_MERGE,)
    assert dry_run.merge_steps == (
        "lock_target_ref",
        "verify_target_head",
        "merge_branch",
        "record_merge_result",
        "run_scope_catchup",
        "activate_target_graph_refs",
        "activate_target_semantic_projection",
    )

    payload = merge_gate_plan_to_dict(dry_run)
    assert payload["evidence"][0]["evidence_id"].startswith("evidence-")
    assert payload["blockers"] == []


def test_merge_gate_blocks_dependency_stale_target_and_batch_rollback() -> None:
    items = [
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id="mergeq-gate-blocked",
            queue_item_id="item-T1",
            task_id="T1",
            branch_ref="refs/heads/codex/PB013-T1-foundation",
            queue_index=1,
            status=STATE_RUNNING,
            target_ref=TARGET_REF,
        ),
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id="mergeq-gate-blocked",
            queue_item_id="item-T2",
            task_id="T2",
            branch_ref="refs/heads/codex/PB013-T2-downstream",
            queue_index=2,
            status=STATE_MERGE_READY,
            hard_depends_on=("T1",),
            target_ref=TARGET_REF,
            branch_head="head-T2",
            validated_target_head="target-before",
            current_target_head="target-after",
        ),
    ]

    plan = decide_merge_gate(
        items,
        task_id="T2",
        evidence=_passing_merge_evidence(),
        batch_status=BATCH_STATE_ROLLBACK_REQUIRED,
        dry_run=False,
        scenario_id="PB-013",
    )

    assert plan.merge_gate_passed is False
    assert plan.target_branch_mutation_allowed is False
    assert "queue_dependency_blocked" in plan.blocker_codes
    assert "batch_rollback_required" in plan.blocker_codes
    assert "resolve_queue_dependencies" in plan.next_actions
    assert "resolve_batch_rollback" in plan.next_actions


def test_persisted_merge_gate_replays_after_restart(tmp_path) -> None:
    db_path = str(tmp_path / "runtime.db")
    conn = _runtime_conn(db_path)
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="mergeq-gate-restart",
                queue_item_id="item-T1",
                task_id="T1",
                branch_ref="refs/heads/codex/PB013-T1-ready",
                queue_index=1,
                status=STATE_MERGE_READY,
                target_ref=TARGET_REF,
                branch_head="head-T1",
                validated_target_head="target-base",
                current_target_head="target-base",
                merge_preview_id="preview-T1",
                snapshot_id="scope-T1",
                projection_id="semproj-T1",
            ),
        ],
        now_iso="2026-05-17T08:00:00Z",
    )
    conn.commit()
    conn.close()

    restarted = _runtime_conn(db_path)
    plan = decide_persisted_merge_gate(
        restarted,
        PROJECT_ID,
        "mergeq-gate-restart",
        target_ref=TARGET_REF,
        task_id="T1",
        evidence={
            **_passing_merge_evidence(),
            "semantic_projection": {
                "status": "intentionally_deferred",
                "evidence_id": "semantic-deferred",
            },
        },
        scenario_id="PB-013",
    )

    assert plan.merge_gate_passed is True
    assert plan.warnings == (
        {
            "code": "deferred_evidence:semantic_projection",
            "source": "evidence",
            "message": "semantic_projection is intentionally deferred",
        },
    )
    assert plan.merge_preview_id == "preview-T1"
    assert plan.snapshot_id == "scope-T1"


def test_git_merge_preview_evidence_reports_clean_conflict_and_stale(tmp_path) -> None:
    fixture = create_merge_preview_fixture_project(tmp_path)

    clean = git_merge_preview_evidence(
        repo_root_path=fixture.root,
        target_ref="main",
        branch_ref=fixture.clean_branch,
        expected_target_head=fixture.main_head,
    )
    assert clean["status"] == "pass"
    assert clean["passed"] is True
    assert clean["preview_tree"]
    assert clean["target_commit"] == fixture.main_head

    conflict = git_merge_preview_evidence(
        repo_root_path=fixture.root,
        target_ref="main",
        branch_ref=fixture.conflict_branch,
        expected_target_head=fixture.main_head,
    )
    assert conflict["status"] == "fail"
    assert conflict["passed"] is False
    assert "CONFLICT" in conflict["stdout"]

    stale = git_merge_preview_evidence(
        repo_root_path=fixture.root,
        target_ref="main",
        branch_ref=fixture.clean_branch,
        expected_target_head="not-the-current-head",
    )
    assert stale["status"] == "stale"
    assert stale["passed"] is False
    assert stale["reason"] == "target head differs from expected_target_head"


def test_merge_result_recording_updates_queue_and_context_with_fence() -> None:
    conn = _runtime_conn()
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id="PB-014",
            task_id="T1",
            branch_ref="refs/heads/codex/PB014-T1-ready",
            status=STATE_MERGE_READY,
            fence_token="fence-merge-current",
            target_head_commit="target-before",
            merge_queue_id="mergeq-result",
            merge_preview_id="preview-result",
        ),
        now_iso="2026-05-17T08:20:00Z",
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="mergeq-result",
                queue_item_id="item-T1",
                task_id="T1",
                branch_ref="refs/heads/codex/PB014-T1-ready",
                queue_index=1,
                status=STATE_MERGE_READY,
                target_ref=TARGET_REF,
                branch_head="head-T1",
                validated_target_head="target-before",
                current_target_head="target-before",
                merge_preview_id="preview-result",
                snapshot_id="scope-T1",
                projection_id="semproj-T1",
            ),
        ],
        now_iso="2026-05-17T08:20:00Z",
    )

    with pytest.raises(BranchRuntimeFenceError):
        record_merge_queue_result(
            conn,
            project_id=PROJECT_ID,
            merge_queue_id="mergeq-result",
            task_id="T1",
            status=STATE_MERGED,
            merge_commit="merge-T1",
            target_head_after_merge="target-after",
            fence_token="fence-stale",
            now_iso="2026-05-17T08:21:00Z",
        )

    recorded = record_merge_queue_result(
        conn,
        project_id=PROJECT_ID,
        merge_queue_id="mergeq-result",
        task_id="T1",
        status=STATE_MERGED,
        merge_commit="merge-T1",
        target_head_before_merge="target-before",
        target_head_after_merge="target-after",
        fence_token="fence-merge-current",
        now_iso="2026-05-17T08:22:00Z",
    )

    assert recorded["queue_item"]["status"] == STATE_MERGED
    assert recorded["queue_item"]["merge_commit"] == "merge-T1"
    assert recorded["queue_item"]["target_head_before_merge"] == "target-before"
    assert recorded["queue_item"]["target_head_after_merge"] == "target-after"
    assert recorded["queue_item"]["completed_at"] == "2026-05-17T08:22:00Z"
    assert recorded["context"]["status"] == STATE_MERGED
    assert recorded["context"]["target_head_commit"] == "target-after"

    context = get_branch_context(conn, PROJECT_ID, "T1")
    assert context is not None
    assert context.status == STATE_MERGED
    assert context.target_head_commit == "target-after"

    plan = decide_persisted_merge_queue(
        conn,
        PROJECT_ID,
        "mergeq-result",
        target_ref=TARGET_REF,
        scenario_id="PB-014",
    )
    assert plan.decisions[0].target_graph_activation_allowed is True
    assert plan.decisions[0].target_semantic_activation_allowed is True


def test_execute_merge_queue_item_preflights_fence_before_live_writer(
    tmp_path, monkeypatch
) -> None:
    fixture = create_merge_preview_fixture_project(tmp_path)
    conn = _runtime_conn()
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id="PB-016",
            task_id="T-live-preflight",
            branch_ref=fixture.clean_branch,
            status=STATE_MERGE_READY,
            fence_token="fence-live-current",
            target_head_commit=fixture.main_head,
            merge_queue_id="mergeq-live-preflight",
        ),
        now_iso="2026-05-17T08:30:00Z",
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="mergeq-live-preflight",
                queue_item_id="item-live-preflight",
                task_id="T-live-preflight",
                branch_ref=fixture.clean_branch,
                queue_index=1,
                status=STATE_MERGE_READY,
                target_ref="main",
                branch_head=fixture.clean_branch,
                validated_target_head=fixture.main_head,
                current_target_head=fixture.main_head,
            )
        ],
        now_iso="2026-05-17T08:30:00Z",
    )
    writer_calls: list[dict[str, str]] = []

    def fake_write_merge_with_trailer(*args, **kwargs):
        writer_calls.append({"message": str(args[0]) if args else ""})
        return True, "merge-should-not-happen", ""

    monkeypatch.setattr(
        "agent.governance.chain_trailer.write_merge_with_trailer",
        fake_write_merge_with_trailer,
    )

    with pytest.raises(BranchRuntimeFenceError):
        execute_merge_queue_item(
            conn,
            project_id=PROJECT_ID,
            merge_queue_id="mergeq-live-preflight",
            repo_root_path=fixture.root,
            queue_item_id="item-live-preflight",
            target_ref="main",
            evidence=_passing_merge_evidence(),
            dry_run=False,
            allow_target_ref_mutation=True,
            fence_token="fence-live-stale",
            message="merge feature-clean",
            now_iso="2026-05-17T08:31:00Z",
        )

    assert writer_calls == []
    context = get_branch_context(conn, PROJECT_ID, "T-live-preflight")
    assert context is not None
    assert context.status == STATE_MERGE_READY


def test_pb012_merge_queue_rejects_mixed_project_queue_or_target_scope() -> None:
    base = MergeQueueItem(
        project_id=PROJECT_ID,
        merge_queue_id=QUEUE_ID,
        queue_item_id="item-T1",
        task_id="T1",
        branch_ref="refs/heads/codex/PB012-T1",
        queue_index=1,
        status=STATE_MERGE_READY,
        target_ref=TARGET_REF,
    )

    with pytest.raises(ValueError, match="project_id"):
        decide_merge_queue([
            base,
            MergeQueueItem(
                project_id="other-project",
                merge_queue_id=QUEUE_ID,
                queue_item_id="item-T2",
                task_id="T2",
                branch_ref="refs/heads/codex/PB012-T2",
                queue_index=2,
                status=STATE_MERGE_READY,
                target_ref=TARGET_REF,
            ),
        ], scenario_id="PB-012")

    with pytest.raises(ValueError, match="merge_queue_id"):
        decide_merge_queue([
            base,
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="other-queue",
                queue_item_id="item-T2",
                task_id="T2",
                branch_ref="refs/heads/codex/PB012-T2",
                queue_index=2,
                status=STATE_MERGE_READY,
                target_ref=TARGET_REF,
            ),
        ], scenario_id="PB-012")

    with pytest.raises(ValueError, match="target_ref"):
        decide_merge_queue([
            base,
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=QUEUE_ID,
                queue_item_id="item-T2",
                task_id="T2",
                branch_ref="refs/heads/codex/PB012-T2",
                queue_index=2,
                status=STATE_MERGE_READY,
                target_ref="refs/heads/release",
            ),
        ], scenario_id="PB-012")


# Dogfood shape: live merge apply stored a SHORT merge commit ref
# (e.g. "37c4ac33") while graph current-full reconcile supplies the FULL SHA
# ("37c4ac3319c74e...").
_FULL_TARGET_HEAD = "37c4ac3319c74e7360829e93df95b5d895cb2d10"
_SHORT_TARGET_HEAD = "37c4ac33"


def test_graph_epoch_auto_record_matches_short_merge_commit_against_full_reconcile_head() -> None:
    """Short stored merge-commit ref must match the full reconcile target head."""
    conn = _runtime_conn()
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id="PB-short-sha",
            task_id="T1",
            branch_ref="refs/heads/codex/PB-short-sha-T1",
            status=STATE_MERGED,
            target_head_commit=_SHORT_TARGET_HEAD,
            merge_queue_id=QUEUE_ID,
        ),
        now_iso="2026-07-09T02:00:00Z",
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=QUEUE_ID,
                queue_item_id="item-T1",
                task_id="T1",
                branch_ref="refs/heads/codex/PB-short-sha-T1",
                queue_index=1,
                status=STATE_MERGED,
                target_ref=TARGET_REF,
                # live apply persisted SHORT refs only
                current_target_head=_SHORT_TARGET_HEAD,
                merge_commit=_SHORT_TARGET_HEAD,
            ),
        ],
        now_iso="2026-07-09T02:00:00Z",
    )

    recorded = record_merge_queue_graph_epoch_after_reconcile(
        conn,
        project_id=PROJECT_ID,
        # reconcile supplies the FULL target head
        target_head_commit=_FULL_TARGET_HEAD,
        snapshot_id="full-short-sha",
        projection_id="semproj-short-sha",
        merge_queue_id=QUEUE_ID,
        queue_item_id="item-T1",
        now_iso="2026-07-09T02:01:00Z",
    )

    assert recorded["status"] == "recorded"
    assert recorded["updated_count"] == 1
    persisted = {
        item.task_id: item
        for item in list_merge_queue_items(conn, PROJECT_ID, QUEUE_ID, target_ref=TARGET_REF)
    }
    assert persisted["T1"].snapshot_id == "full-short-sha"
    assert persisted["T1"].projection_id == "semproj-short-sha"
    context = get_branch_context(conn, PROJECT_ID, "T1")
    assert context is not None
    assert context.snapshot_id == "full-short-sha"
    assert context.projection_id == "semproj-short-sha"


def test_graph_epoch_auto_record_skips_unrelated_short_ref() -> None:
    """An unrelated short ref must not spuriously match the reconcile target."""
    conn = _runtime_conn()
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=QUEUE_ID,
                queue_item_id="item-unrelated",
                task_id="T9",
                branch_ref="refs/heads/codex/PB-short-sha-T9",
                queue_index=1,
                status=STATE_MERGED,
                target_ref=TARGET_REF,
                current_target_head="deadbee",
                merge_commit="deadbee",
            ),
        ],
        now_iso="2026-07-09T02:00:00Z",
    )

    recorded = record_merge_queue_graph_epoch_after_reconcile(
        conn,
        project_id=PROJECT_ID,
        target_head_commit=_FULL_TARGET_HEAD,
        snapshot_id="full-short-sha",
        projection_id="semproj-short-sha",
        merge_queue_id=QUEUE_ID,
        queue_item_id="item-unrelated",
        now_iso="2026-07-09T02:01:00Z",
    )

    assert recorded["status"] == "skipped"
    assert recorded["updated_count"] == 0
    assert recorded["skipped_reason"] == "no_matching_merged_queue_item_missing_graph_epoch"
    persisted = {
        item.task_id: item
        for item in list_merge_queue_items(conn, PROJECT_ID, QUEUE_ID, target_ref=TARGET_REF)
    }
    assert persisted["T9"].snapshot_id == ""
    assert persisted["T9"].projection_id == ""


def test_merge_queue_item_matches_reconciled_head_prefix_rules() -> None:
    """Ambiguity-safe matching: short hex prefix matches; unrelated/too-short do not."""
    short_item = MergeQueueItem(
        project_id=PROJECT_ID,
        merge_queue_id=QUEUE_ID,
        queue_item_id="item-short",
        task_id="T1",
        branch_ref="refs/heads/codex/PB-short-sha-T1",
        queue_index=1,
        status=STATE_MERGED,
        target_ref=TARGET_REF,
        current_target_head=_SHORT_TARGET_HEAD,
        merge_commit=_SHORT_TARGET_HEAD,
    )
    # short (>=7 char) hex prefix of the full reconcile head matches
    assert _merge_queue_item_matches_reconciled_head(short_item, _FULL_TARGET_HEAD) is True
    # exact full-vs-full still matches
    full_item = replace(short_item, current_target_head=_FULL_TARGET_HEAD, merge_commit=_FULL_TARGET_HEAD)
    assert _merge_queue_item_matches_reconciled_head(full_item, _FULL_TARGET_HEAD) is True
    # unrelated ref does not match
    unrelated_item = replace(short_item, current_target_head="deadbee", merge_commit="deadbee")
    assert _merge_queue_item_matches_reconciled_head(unrelated_item, _FULL_TARGET_HEAD) is False
    # a different full sha (full-vs-full) requires equality
    other_full = "a" * 40
    other_item = replace(short_item, current_target_head=other_full, merge_commit=other_full)
    assert _merge_queue_item_matches_reconciled_head(other_item, _FULL_TARGET_HEAD) is False
    # too-short (<7) shared prefix must not match
    tiny_item = replace(short_item, current_target_head="37c4a", merge_commit="37c4a")
    assert _merge_queue_item_matches_reconciled_head(tiny_item, _FULL_TARGET_HEAD) is False
    # empty refs keep explicit-queue-item behavior
    empty_item = replace(short_item, current_target_head="", merge_commit="", target_head_after_merge="")
    assert _merge_queue_item_matches_reconciled_head(empty_item, _FULL_TARGET_HEAD) is False
    assert (
        _merge_queue_item_matches_reconciled_head(
            empty_item, _FULL_TARGET_HEAD, explicit_queue_item=True
        )
        is True
    )


def test_integration_epoch_restart_recovers_already_integrated_item_and_freezes_unrelated_merge(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_db = tmp_path / "integration-epoch.sqlite"
    conn = _runtime_conn(str(runtime_db))
    batch_id = "batch-restart"
    queue_id = "mq-batch-restart"
    row1 = MergeQueueItem(
        project_id=PROJECT_ID,
        merge_queue_id=queue_id,
        queue_item_id="item-row1",
        task_id="task-row1",
        backlog_id="AC-BATCH-ROW1",
        branch_ref="refs/heads/codex/row1",
        queue_index=1,
        status=STATE_MERGE_READY,
        target_ref=TARGET_REF,
        current_target_head="base-head",
        validated_target_head="base-head",
        merge_preview_id="preview-row1",
    )
    row2 = replace(
        row1,
        queue_item_id="item-row2",
        task_id="task-row2",
        backlog_id="AC-BATCH-ROW2",
        branch_ref="refs/heads/codex/row2",
        queue_index=2,
        merge_preview_id="preview-row2",
    )
    for task_id, backlog_id, checkpoint_id in (
        ("task-row1", "AC-BATCH-ROW1", "checkpoint-row1"),
        ("task-row2", "AC-BATCH-ROW2", "checkpoint-row2"),
    ):
        upsert_branch_context(
            conn,
            BranchTaskRuntimeContext(
                project_id=PROJECT_ID,
                batch_id=batch_id,
                task_id=task_id,
                backlog_id=backlog_id,
                branch_ref=f"refs/heads/codex/{task_id}",
                status=STATE_MERGE_READY,
                checkpoint_id=checkpoint_id,
                merge_queue_id=queue_id,
            ),
        )
    upsert_merge_queue_items(conn, [row1, row2])
    epoch = open_or_validate_integration_epoch(
        conn,
        item=row1,
        batch_id=batch_id,
        target_head="base-head",
        checkpoint_id="checkpoint-row1",
    )
    armed = arm_integration_epoch_merge_in_doubt(
        conn,
        epoch,
        item=row1,
        target_head_before="base-head",
        branch_head="row1-head",
    )
    assert armed.status == INTEGRATION_EPOCH_MERGE_IN_DOUBT
    conn.close()

    restarted = _runtime_conn(str(runtime_db))
    monkeypatch.setattr(
        pbr,
        "git_merge_preview_evidence",
        lambda **_kwargs: {
            "status": "pass",
            "passed": True,
            "target_commit": "merged-row1-head",
            "branch_commit": "row1-head",
        },
    )
    monkeypatch.setattr(
        pbr,
        "_git_preview_branch_is_ancestor",
        lambda *_args, **_kwargs: True,
    )
    recovered = execute_merge_queue_item(
        restarted,
        project_id=PROJECT_ID,
        merge_queue_id=queue_id,
        repo_root_path=tmp_path,
        queue_item_id="item-row1",
        target_ref=TARGET_REF,
        dry_run=False,
        allow_target_ref_mutation=False,
    )
    assert recovered["ok"] is True
    assert recovered["already_integrated"] is True
    assert recovered["target_ref_mutated"] is False
    resumed_epoch = get_active_integration_epoch(
        restarted, PROJECT_ID, target_ref=TARGET_REF
    )
    assert resumed_epoch is not None
    assert resumed_epoch.merged_prefix == ("item-row1",)
    assert resumed_epoch.active_queue_item_id == "item-row2"
    assert resumed_epoch.active_checkpoint_id == "checkpoint-row2"
    assert resumed_epoch.merge_cursor == 1

    upsert_branch_context(
        restarted,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id="unrelated-batch",
            task_id="unrelated-task",
            backlog_id="AC-UNRELATED",
            branch_ref="refs/heads/codex/unrelated",
            status=STATE_MERGE_READY,
            merge_queue_id="mq-unrelated",
        ),
    )
    upsert_merge_queue_items(
        restarted,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="mq-unrelated",
                queue_item_id="item-unrelated",
                task_id="unrelated-task",
                backlog_id="AC-UNRELATED",
                branch_ref="refs/heads/codex/unrelated",
                queue_index=1,
                status=STATE_MERGE_READY,
                target_ref=TARGET_REF,
            )
        ],
    )
    refused = execute_merge_queue_item(
        restarted,
        project_id=PROJECT_ID,
        merge_queue_id="mq-unrelated",
        repo_root_path=tmp_path,
        queue_item_id="item-unrelated",
        target_ref=TARGET_REF,
        dry_run=False,
        allow_target_ref_mutation=True,
    )
    assert refused["ok"] is False
    assert refused["error"] == "integration_epoch_target_ref_frozen"
    assert refused["next_legal_action"]["id"] == "resume_batch_merge"


def test_final_reconcile_accepts_active_snapshot_when_semantic_projection_is_skipped() -> None:
    conn = _runtime_conn()
    batch_id = "batch-projection-skipped"
    queue_id = "mq-projection-skipped"
    item = MergeQueueItem(
        project_id=PROJECT_ID,
        merge_queue_id=queue_id,
        queue_item_id="item-only",
        task_id="task-only",
        backlog_id="AC-BATCH-ONLY",
        branch_ref="refs/heads/codex/only",
        queue_index=1,
        status=STATE_MERGE_READY,
        target_ref=TARGET_REF,
        current_target_head="base-head",
        validated_target_head="base-head",
        merge_preview_id="preview-only",
    )
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id=batch_id,
            task_id=item.task_id,
            backlog_id=item.backlog_id,
            branch_ref=item.branch_ref,
            status=STATE_MERGE_READY,
            checkpoint_id="checkpoint-only",
            merge_queue_id=queue_id,
        ),
    )
    upsert_merge_queue_items(conn, [item])
    open_or_validate_integration_epoch(
        conn,
        item=item,
        batch_id=batch_id,
        target_head="base-head",
        checkpoint_id="checkpoint-only",
    )
    record_merge_queue_result(
        conn,
        project_id=PROJECT_ID,
        merge_queue_id=queue_id,
        queue_item_id=item.queue_item_id,
        status=STATE_MERGED,
        merge_commit="final-head",
        target_head_before_merge="base-head",
        target_head_after_merge="final-head",
    )
    epoch = advance_integration_epoch_after_merge(
        conn,
        project_id=PROJECT_ID,
        batch_id=batch_id,
        queue_item_id=item.queue_item_id,
        merge_commit="final-head",
    )
    assert epoch.status == pbr.INTEGRATION_EPOCH_RECONCILE_PENDING

    candidate_only = record_merge_queue_graph_epoch_after_reconcile(
        conn,
        project_id=PROJECT_ID,
        target_head_commit="final-head",
        snapshot_id="full-final-head",
        projection_id="",
        merge_queue_id=queue_id,
        activation_completed=False,
    )
    assert candidate_only["integration_epoch_reconcile_recording"] == (
        "deferred_until_snapshot_activation"
    )
    assert candidate_only["integration_epoch"]["status"] == (
        pbr.INTEGRATION_EPOCH_RECONCILE_PENDING
    )
    pending = get_active_integration_epoch(conn, PROJECT_ID, merge_queue_id=queue_id)
    assert pending is not None
    assert integration_epoch_resume_payload(conn, pending)["id"] == (
        "final_batch_reconcile"
    )

    recorded = record_merge_queue_graph_epoch_after_reconcile(
        conn,
        project_id=PROJECT_ID,
        target_head_commit="final-head",
        snapshot_id="full-final-head",
        projection_id="",
        merge_queue_id=queue_id,
    )
    assert recorded["semantic_projection_optional"] is True
    assert recorded["projection_status"] == "skipped"
    assert recorded["integration_epoch"]["status"] == INTEGRATION_EPOCH_RECONCILED
    reconciled = get_active_integration_epoch(
        conn, PROJECT_ID, merge_queue_id=queue_id
    )
    assert reconciled is not None
    assert reconciled.snapshot_id == "full-final-head"
    assert reconciled.projection_id == ""
    assert integration_epoch_resume_payload(conn, reconciled)["id"] == (
        "close_reconciled_child_rows"
    )
    conn.execute(
        "CREATE TABLE backlog_bugs (bug_id TEXT PRIMARY KEY, status TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO backlog_bugs (bug_id, status) VALUES (?, ?)",
        ("AC-BATCH-ONLY", "FIXED"),
    )
    assert integration_epoch_resume_payload(conn, reconciled)["id"] == (
        "close_batch_atomically"
    )


def test_child_close_waits_for_final_barrier_and_never_releases_epoch() -> None:
    conn = _runtime_conn()
    open_epoch = upsert_integration_epoch(
        conn,
        IntegrationEpoch(
            project_id=PROJECT_ID,
            batch_id="batch-close-barrier",
            epoch_id="integration-epoch-close-barrier",
            coordination_backlog_id="AC-BATCH-PARENT",
            target_ref=TARGET_REF,
            base_head="base-head",
            current_head="partial-head",
            merge_queue_id="mq-close-barrier",
            remaining_queue_item_ids=("item-row2",),
            status=pbr.INTEGRATION_EPOCH_OPEN,
            active_queue_item_id="item-row2",
            active_task_id="task-row2",
            active_backlog_id="AC-BATCH-ROW2",
        ),
    )
    with pytest.raises(IntegrationEpochFrozenError):
        validate_integration_epoch_backlog_close(
            open_epoch,
            backlog_scope="child",
            target_head_commit="partial-head",
        )

    reconciled = upsert_integration_epoch(
        conn,
        replace(
            open_epoch,
            status=INTEGRATION_EPOCH_RECONCILED,
            current_head="final-head",
            remaining_queue_item_ids=(),
            active_queue_item_id="",
            active_task_id="",
            active_backlog_id="",
            reconcile_state="reconciled",
            snapshot_id="full-final-head",
        ),
    )
    child_gate = validate_integration_epoch_backlog_close(
        reconciled,
        backlog_scope="child",
        target_head_commit="final-head",
    )
    assert child_gate["passed"] is True
    assert child_gate["preserve_epoch_freeze"] is True
    assert child_gate["release_epoch"] is False
    assert get_active_integration_epoch(
        conn, PROJECT_ID, target_ref=TARGET_REF
    ) is not None

    parent_gate = validate_integration_epoch_backlog_close(
        reconciled,
        backlog_scope="coordination",
        target_head_commit="final-head",
    )
    assert parent_gate["release_epoch"] is True
    # A failed parent close after validation is still read-only and cannot
    # release the epoch; only the atomic close write below does so.
    assert get_active_integration_epoch(
        conn, PROJECT_ID, target_ref=TARGET_REF
    ) is not None
    closed = close_integration_epoch(
        conn,
        project_id=PROJECT_ID,
        batch_id=reconciled.batch_id,
        target_head_commit="final-head",
    )
    assert closed.status == pbr.INTEGRATION_EPOCH_CLOSED
    assert get_active_integration_epoch(
        conn, PROJECT_ID, target_ref=TARGET_REF
    ) is None
