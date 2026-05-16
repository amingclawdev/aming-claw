"""Executable dry-run scenarios for parallel branch runtime recovery."""

from __future__ import annotations

import sqlite3

import pytest

from agent.governance.db import SCHEMA_VERSION, _ensure_schema
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
    BranchRuntimeFenceError,
    BranchRuntimeTask,
    BranchTaskRuntimeContext,
    decide_restart_recovery,
    ensure_branch_runtime_schema,
    get_branch_context,
    list_branch_contexts,
    recover_expired_branch_contexts,
    record_branch_checkpoint,
    runtime_tasks_from_contexts,
    upsert_branch_context,
)

PROJECT_ID = "fixture-parallel-project"
BATCH_ID = "PB-001"
NOW = "2026-05-16T12:00:00Z"
EXPIRED = "2026-05-16T11:50:00Z"


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


def _runtime_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_branch_runtime_schema(conn)
    return conn


def _pb001_contexts() -> list[BranchTaskRuntimeContext]:
    return [
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T1",
            batch_id=BATCH_ID,
            backlog_id="OPT-PB001-T1",
            branch_ref="refs/heads/codex/PB001-T1-scope-reconcile",
            status=STATE_MERGED,
            merge_queue_id="merge-001",
            base_commit="base-001",
            head_commit="head-T1",
            snapshot_id="scope-base",
            projection_id="semproj-base",
            fence_token="fence-T1",
        ),
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T2",
            batch_id=BATCH_ID,
            backlog_id="OPT-PB001-T2",
            branch_ref="refs/heads/codex/PB001-T2-branch-graph-refs",
            status=STATE_MERGE_FAILED,
            depends_on=("T1",),
            base_commit="base-001",
            head_commit="head-T2",
            fence_token="fence-T2",
        ),
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T3",
            batch_id=BATCH_ID,
            backlog_id="OPT-PB001-T3",
            branch_ref="refs/heads/codex/PB001-T3-task-runtime",
            status="running",
            depends_on=("T1",),
            attempt=1,
            lease_id="lease-T3",
            lease_expires_at=EXPIRED,
            fence_token="fence-old-T3",
            checkpoint_id="checkpoint-T3",
            replay_source="checkpoint",
            base_commit="base-001",
            head_commit="head-T3",
            snapshot_id="scope-T3",
            projection_id="semproj-T3",
        ),
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T4",
            batch_id=BATCH_ID,
            backlog_id="OPT-PB001-T4",
            branch_ref="refs/heads/codex/PB001-T4-dashboard-read-model",
            status="queued_for_merge",
            depends_on=("T2",),
            merge_queue_id="merge-004",
            base_commit="base-001",
            head_commit="head-T4",
            fence_token="fence-T4",
        ),
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T5",
            batch_id=BATCH_ID,
            backlog_id="OPT-PB001-T5",
            branch_ref="refs/heads/codex/PB001-T5-chain-adapter",
            status="running",
            depends_on=("T3",),
            attempt=2,
            lease_id="lease-T5",
            lease_expires_at=EXPIRED,
            fence_token="fence-old-T5",
            checkpoint_id="checkpoint-T5",
            replay_source="checkpoint",
            base_commit="base-001",
            head_commit="head-T5",
            snapshot_id="scope-T5",
            projection_id="semproj-T5",
        ),
    ]


def _persist_pb001_contexts(conn: sqlite3.Connection) -> None:
    for context in _pb001_contexts():
        upsert_branch_context(conn, context, now_iso=NOW)


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
    assert decisions["T3"].checkpoint_id == "checkpoint-T3"

    assert decisions["T4"].recovery_state == STATE_DEPENDENCY_BLOCKED
    assert decisions["T4"].action == ACTION_WAIT_FOR_DEPENDENCY
    assert decisions["T4"].dependency_blockers == ("T2",)

    assert decisions["T5"].recovery_state == STATE_RECLAIMABLE
    assert decisions["T5"].action == ACTION_RECLAIM_AFTER_DEPENDENCY
    assert decisions["T5"].dependency_blockers == ("T3",)
    assert decisions["T5"].checkpoint_id == "checkpoint-T5"


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


def test_branch_runtime_schema_is_in_governance_migration() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    _ensure_schema(conn)

    assert SCHEMA_VERSION >= 36
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("parallel_branch_runtime_contexts",),
    ).fetchone()
    assert row is not None


def test_pb001_recovery_rehydrates_replay_ready_contexts_from_db() -> None:
    conn = _runtime_conn()
    _persist_pb001_contexts(conn)

    recovered = recover_expired_branch_contexts(conn, PROJECT_ID, now_iso=NOW)
    assert [context.task_id for context in recovered] == ["T3", "T5"]

    t3_context = get_branch_context(conn, PROJECT_ID, "T3")
    t5_context = get_branch_context(conn, PROJECT_ID, "T5")
    assert t3_context is not None
    assert t5_context is not None
    assert t3_context.status == STATE_RECLAIMABLE
    assert t5_context.status == STATE_RECLAIMABLE
    assert t3_context.attempt == 2
    assert t5_context.attempt == 3
    assert t3_context.checkpoint_id == "checkpoint-T3"
    assert t5_context.checkpoint_id == "checkpoint-T5"
    assert t3_context.fence_token != "fence-old-T3"
    assert t5_context.fence_token != "fence-old-T5"

    contexts_after_restart = list_branch_contexts(conn, PROJECT_ID, batch_id=BATCH_ID)
    runtime_tasks = runtime_tasks_from_contexts(contexts_after_restart, now_iso=NOW)
    plan = decide_restart_recovery(runtime_tasks)
    decisions = _by_task(plan)

    assert decisions["T3"].action == ACTION_RECLAIM_FROM_CHECKPOINT
    assert decisions["T3"].checkpoint_id == "checkpoint-T3"
    assert decisions["T3"].replay_source == "checkpoint"

    assert decisions["T5"].action == ACTION_RECLAIM_AFTER_DEPENDENCY
    assert decisions["T5"].dependency_blockers == ("T3",)
    assert decisions["T5"].checkpoint_id == "checkpoint-T5"
    assert decisions["T5"].replay_source == "checkpoint"


def test_branch_runtime_rejects_stale_fence_after_reclaim() -> None:
    conn = _runtime_conn()
    _persist_pb001_contexts(conn)

    recover_expired_branch_contexts(conn, PROJECT_ID, now_iso=NOW)

    with pytest.raises(BranchRuntimeFenceError):
        record_branch_checkpoint(
            conn,
            project_id=PROJECT_ID,
            task_id="T3",
            checkpoint_id="checkpoint-stale",
            fence_token="fence-old-T3",
            now_iso=NOW,
        )

    current = get_branch_context(conn, PROJECT_ID, "T3")
    assert current is not None
    updated = record_branch_checkpoint(
        conn,
        project_id=PROJECT_ID,
        task_id="T3",
        checkpoint_id="checkpoint-T3-after-reclaim",
        fence_token=current.fence_token,
        now_iso=NOW,
    )

    assert updated.checkpoint_id == "checkpoint-T3-after-reclaim"
    assert updated.replay_source == "checkpoint"
