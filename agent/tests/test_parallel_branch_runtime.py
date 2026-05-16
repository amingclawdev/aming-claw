"""Executable dry-run scenarios for parallel branch runtime recovery."""

from __future__ import annotations

from agent.governance.parallel_branch_runtime import (
    ACTION_LEAVE_MERGED,
    ACTION_OBSERVER_DECISION_REQUIRED,
    ACTION_RECLAIM_AFTER_DEPENDENCY,
    ACTION_RECLAIM_FROM_CHECKPOINT,
    ACTION_WAIT_FOR_DEPENDENCY,
    STATE_DEPENDENCY_BLOCKED,
    STATE_MERGE_FAILED,
    STATE_MERGED,
    STATE_RECLAIMABLE,
    BranchRuntimeTask,
    decide_restart_recovery,
)


def _pb001_tasks() -> list[BranchRuntimeTask]:
    return [
        BranchRuntimeTask(
            task_id="T1",
            branch_ref="refs/heads/codex/PB001-T1-scope-reconcile",
            status="merged",
            merge_epoch="merge-001",
        ),
        BranchRuntimeTask(
            task_id="T2",
            branch_ref="refs/heads/codex/PB001-T2-branch-graph-refs",
            status="merge_failed",
            depends_on=("T1",),
        ),
        BranchRuntimeTask(
            task_id="T3",
            branch_ref="refs/heads/codex/PB001-T3-task-runtime",
            status="running",
            depends_on=("T1",),
            lease_expired=True,
            checkpoint_id="checkpoint-T3",
        ),
        BranchRuntimeTask(
            task_id="T4",
            branch_ref="refs/heads/codex/PB001-T4-dashboard-read-model",
            status="queued_for_merge",
            depends_on=("T2",),
        ),
        BranchRuntimeTask(
            task_id="T5",
            branch_ref="refs/heads/codex/PB001-T5-chain-adapter",
            status="running",
            depends_on=("T3",),
            lease_expired=True,
            checkpoint_id="checkpoint-T5",
        ),
    ]


def _by_task(plan):
    return {decision.task_id: decision for decision in plan.decisions}


def test_pb001_machine_restart_recovery_decisions() -> None:
    """PB-001: T1 merged, T2 failed, T4 queued, T3/T5 expired after restart."""
    plan = decide_restart_recovery(_pb001_tasks())
    decisions = _by_task(plan)

    assert plan.scenario_id == "PB-001"
    assert decisions["T1"].recovery_state == STATE_MERGED
    assert decisions["T1"].action == ACTION_LEAVE_MERGED

    assert decisions["T2"].recovery_state == STATE_MERGE_FAILED
    assert decisions["T2"].action == ACTION_OBSERVER_DECISION_REQUIRED
    assert decisions["T2"].recovery_actions == ("fix_or_rebase", "abandon", "rollback_batch")

    assert decisions["T3"].recovery_state == STATE_RECLAIMABLE
    assert decisions["T3"].action == ACTION_RECLAIM_FROM_CHECKPOINT

    assert decisions["T4"].recovery_state == STATE_DEPENDENCY_BLOCKED
    assert decisions["T4"].action == ACTION_WAIT_FOR_DEPENDENCY
    assert decisions["T4"].dependency_blockers == ("T2",)

    assert decisions["T5"].recovery_state == STATE_RECLAIMABLE
    assert decisions["T5"].action == ACTION_RECLAIM_AFTER_DEPENDENCY
    assert decisions["T5"].dependency_blockers == ("T3",)


def test_pb001_retains_branches_and_blocks_cleanup_until_unresolved_work_finishes() -> None:
    plan = decide_restart_recovery(_pb001_tasks())

    assert plan.cleanup_allowed is False
    assert plan.retained_branch_refs == tuple(task.branch_ref for task in _pb001_tasks())
    assert {row["task_id"] for row in plan.dashboard_rows} == {"T1", "T2", "T3", "T4", "T5"}

    actionable_rows = {
        row["task_id"]: row["recovery_actions"]
        for row in plan.dashboard_rows
        if row["recovery_actions"]
    }
    assert actionable_rows["T2"] == ["fix_or_rebase", "abandon", "rollback_batch"]
    assert actionable_rows["T3"] == ["reclaim", "replay_from_checkpoint"]
    assert actionable_rows["T4"] == ["wait_for_dependency", "revalidate_after_dependency"]
    assert actionable_rows["T5"] == [
        "wait_for_dependency",
        "reclaim",
        "replay_from_checkpoint",
    ]


def test_pb001_only_merged_task_can_activate_target_graph_or_semantic_projection() -> None:
    plan = decide_restart_recovery(_pb001_tasks())
    decisions = _by_task(plan)

    assert decisions["T1"].target_graph_activation_allowed is True
    assert decisions["T1"].target_semantic_activation_allowed is True

    assert plan.target_graph_activation_blocked_for == ("T2", "T3", "T4", "T5")
    assert plan.target_semantic_activation_blocked_for == ("T2", "T3", "T4", "T5")
