"""Executable dry-run scenarios for parallel branch merge queue decisions."""

from __future__ import annotations

import pytest

from agent.governance.parallel_branch_runtime import (
    ACTION_ALLOW_MERGE,
    ACTION_REVALIDATE_AFTER_DEPENDENCY_MERGE,
    ACTION_WAIT_FOR_DEPENDENCY,
    STATE_MERGE_READY,
    STATE_MERGED,
    STATE_STALE_AFTER_DEPENDENCY_MERGE,
    STATE_WAITING_DEPENDENCY,
    MergeQueueItem,
    decide_merge_queue,
)

PROJECT_ID = "fixture-parallel-project"
QUEUE_ID = "mergeq-PB002"
TARGET_REF = "refs/heads/main"


def _by_task(plan):
    return {decision.task_id: decision for decision in plan.decisions}


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
    assert decisions["T2"].next_actions == ("wait_for_dependency", "do_not_merge")
    assert decisions["T2"].merge_allowed is False
    assert decisions["T2"].target_branch_mutation_allowed is False
    assert decisions["T2"].target_graph_activation_allowed is False
    assert decisions["T2"].target_semantic_activation_allowed is False


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
    assert plan.dashboard_rows[1] == {
        "queue_item_id": "item-T2",
        "task_id": "T2",
        "branch_ref": "refs/heads/codex/PB002-T2-dashboard-read-model",
        "observed_status": STATE_MERGE_READY,
        "queue_state": STATE_WAITING_DEPENDENCY,
        "action": ACTION_WAIT_FOR_DEPENDENCY,
        "dependency_blockers": ["T1"],
        "stale_target_head": False,
        "next_actions": ["wait_for_dependency", "do_not_merge"],
        "merge_allowed": False,
        "target_branch_mutation_allowed": False,
        "target_graph_activation_allowed": False,
        "target_semantic_activation_allowed": False,
        "validation_attempt": 0,
        "merge_preview_id": "",
    }


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
