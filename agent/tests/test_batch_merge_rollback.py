"""Executable dry-run scenarios for batch merge rollback and replay."""

from __future__ import annotations

import sqlite3

from agent.governance.parallel_branch_runtime import (
    ACTION_CLEANUP_RETAINED_BRANCH,
    ACTION_REPLAY_THROUGH_MERGE_QUEUE,
    ACTION_RETAIN_FOR_REPLAY,
    ACTION_ROLLBACK_BATCH,
    BATCH_STATE_ACCEPTED,
    BATCH_STATE_OPEN,
    BATCH_STATE_ROLLBACK_REQUIRED,
    STATE_MERGE_READY,
    STATE_MERGED,
    STATE_QUEUED_FOR_MERGE,
    BatchMergeItem,
    BatchMergeRuntime,
    decide_batch_rollback_replay,
    decide_persisted_batch_rollback_replay,
    get_batch_merge_runtime,
    upsert_batch_merge_runtime,
)

PROJECT_ID = "fixture-parallel-project"
BATCH_ID = "PB-004"
TARGET_REF = "refs/heads/main"


def _runtime_conn(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _pb004_runtime(*, batch_status: str = BATCH_STATE_OPEN) -> BatchMergeRuntime:
    return BatchMergeRuntime(
        project_id=PROJECT_ID,
        batch_id=BATCH_ID,
        target_ref=TARGET_REF,
        batch_base_commit="B0",
        current_target_head="merge-T2",
        batch_status=batch_status,
        rollback_snapshot_id="snapshot-main-B0",
        rollback_projection_id="semproj-main-B0",
        failure_reason="wrong merge order caused severe integration failure",
        items=(
            BatchMergeItem(
                task_id="T2",
                branch_ref="refs/heads/codex/PB004-T2-feature-before-foundation",
                worktree_path="/tmp/worktrees/PB004-T2",
                queue_index=2,
                status=STATE_MERGED,
                base_commit="B0",
                branch_head="head-T2",
                checkpoint_id="checkpoint-T2",
                merge_commit="merge-T2",
                target_head_before_merge="B0",
                target_head_after_merge="merge-T2",
                snapshot_id="scope-T2",
                projection_id="semproj-T2",
                merge_queue_id="mergeq-PB004",
                merge_preview_id="preview-T2",
                depends_on=("T1",),
            ),
            BatchMergeItem(
                task_id="T1",
                branch_ref="refs/heads/codex/PB004-T1-scope-foundation",
                worktree_path="/tmp/worktrees/PB004-T1",
                queue_index=1,
                status=STATE_MERGE_READY,
                base_commit="B0",
                branch_head="head-T1",
                checkpoint_id="checkpoint-T1",
                snapshot_id="scope-T1",
                projection_id="semproj-T1",
                merge_queue_id="mergeq-PB004",
                merge_preview_id="preview-T1",
            ),
            BatchMergeItem(
                task_id="T3",
                branch_ref="refs/heads/codex/PB004-T3-dashboard",
                worktree_path="/tmp/worktrees/PB004-T3",
                queue_index=3,
                status=STATE_MERGE_READY,
                base_commit="B0",
                branch_head="head-T3",
                checkpoint_id="checkpoint-T3",
                snapshot_id="scope-T3",
                projection_id="semproj-T3",
                merge_queue_id="mergeq-PB004",
                merge_preview_id="preview-T3",
                depends_on=("T1", "T2"),
            ),
        ),
    )


def test_pb004_wrong_merge_order_enters_rollback_required_and_retains_branches() -> None:
    plan = decide_batch_rollback_replay(
        _pb004_runtime(),
        severe_integration_failure=True,
        corrected_replay_order=("T1", "T2", "T3"),
    )

    assert plan.scenario_id == "PB-004"
    assert plan.batch_status == BATCH_STATE_ROLLBACK_REQUIRED
    assert plan.rollback_required is True
    assert plan.rollback_target_commit == "B0"
    assert plan.rollback_epoch == "rollback-PB-004-B0"
    assert plan.replay_epoch == "replay-PB-004-merge-T2"
    assert plan.rollback_snapshot_id == "snapshot-main-B0"
    assert plan.rollback_projection_id == "semproj-main-B0"

    assert plan.abandoned_merge_commits == ("merge-T2",)
    assert plan.abandoned_snapshot_ids == ("scope-T2",)
    assert plan.abandoned_projection_ids == ("semproj-T2",)
    assert plan.retained_branch_refs == (
        "refs/heads/codex/PB004-T2-feature-before-foundation",
        "refs/heads/codex/PB004-T1-scope-foundation",
        "refs/heads/codex/PB004-T3-dashboard",
    )
    assert plan.retained_worktree_paths == (
        "/tmp/worktrees/PB004-T2",
        "/tmp/worktrees/PB004-T1",
        "/tmp/worktrees/PB004-T3",
    )
    assert plan.cleanup_allowed is False
    assert plan.cleanup_blockers == ("T2", "T1", "T3")
    assert plan.operator_actions == (
        ACTION_ROLLBACK_BATCH,
        ACTION_REPLAY_THROUGH_MERGE_QUEUE,
        "approve_cleanup_after_replay",
    )


def test_pb004_persisted_batch_replays_rollback_after_restart(tmp_path) -> None:
    db_path = str(tmp_path / "runtime.db")
    conn = _runtime_conn(db_path)
    upsert_batch_merge_runtime(
        conn,
        _pb004_runtime(),
        now_iso="2026-05-17T06:10:00Z",
    )
    conn.commit()
    conn.close()

    restarted = _runtime_conn(db_path)
    runtime = get_batch_merge_runtime(restarted, PROJECT_ID, BATCH_ID)
    assert runtime is not None
    assert runtime.failure_reason == "wrong merge order caused severe integration failure"
    assert [item.task_id for item in runtime.items] == ["T1", "T2", "T3"]
    assert [item.worktree_path for item in runtime.items] == [
        "/tmp/worktrees/PB004-T1",
        "/tmp/worktrees/PB004-T2",
        "/tmp/worktrees/PB004-T3",
    ]
    assert runtime.items[1].merge_commit == "merge-T2"
    assert runtime.items[1].depends_on == ("T1",)

    plan = decide_persisted_batch_rollback_replay(
        restarted,
        PROJECT_ID,
        BATCH_ID,
        severe_integration_failure=True,
        corrected_replay_order=("T1", "T2", "T3"),
    )

    assert plan.rollback_required is True
    assert plan.abandoned_merge_commits == ("merge-T2",)
    assert plan.replay_task_ids == ("T1", "T2", "T3")
    assert plan.retained_worktree_paths == (
        "/tmp/worktrees/PB004-T1",
        "/tmp/worktrees/PB004-T2",
        "/tmp/worktrees/PB004-T3",
    )
    assert plan.cleanup_allowed is False


def test_pb004_replays_retained_branch_heads_through_merge_queue_in_corrected_order() -> None:
    plan = decide_batch_rollback_replay(
        _pb004_runtime(),
        severe_integration_failure=True,
        corrected_replay_order=("T1", "T2", "T3"),
    )

    assert plan.replay_task_ids == ("T1", "T2", "T3")
    assert [item.task_id for item in plan.replay_merge_queue_items] == ["T1", "T2", "T3"]
    assert [item.queue_index for item in plan.replay_merge_queue_items] == [1, 2, 3]
    assert all(item.status == STATE_QUEUED_FOR_MERGE for item in plan.replay_merge_queue_items)
    assert all(item.base_commit == "B0" for item in plan.replay_merge_queue_items)
    assert all(item.current_target_head == "B0" for item in plan.replay_merge_queue_items)
    assert [item.branch_head for item in plan.replay_merge_queue_items] == [
        "head-T1",
        "head-T2",
        "head-T3",
    ]
    assert plan.replay_merge_queue_items[1].depends_on == ("T1",)
    assert plan.replay_merge_queue_items[2].depends_on == ("T1", "T2")


def test_pb004_dashboard_rows_show_retention_replay_and_cleanup_blockers() -> None:
    plan = decide_batch_rollback_replay(
        _pb004_runtime(),
        severe_integration_failure=True,
        corrected_replay_order=("T1", "T2", "T3"),
    )
    rows = {row["task_id"]: row for row in plan.dashboard_rows}

    assert set(rows) == {"T1", "T2", "T3"}
    assert rows["T1"]["action"] == ACTION_RETAIN_FOR_REPLAY
    assert rows["T1"]["cleanup_allowed"] is False
    assert rows["T1"]["rollback_epoch"] == plan.rollback_epoch
    assert rows["T1"]["replay_epoch"] == plan.replay_epoch
    assert rows["T1"]["operator_actions"] == [
        "retain_branch",
        "replay_through_merge_queue",
        "block_cleanup",
    ]
    assert rows["T2"]["merge_commit"] == "merge-T2"
    assert rows["T3"]["snapshot_id"] == "scope-T3"


def test_pb009_cleanup_allowed_only_after_batch_acceptance() -> None:
    rollback_plan = decide_batch_rollback_replay(
        _pb004_runtime(batch_status=BATCH_STATE_ROLLBACK_REQUIRED),
        severe_integration_failure=False,
        corrected_replay_order=("T1", "T2", "T3"),
        scenario_id="PB-009",
    )

    assert rollback_plan.cleanup_allowed is False
    assert rollback_plan.cleanup_blockers == ("T2", "T1", "T3")

    accepted_plan = decide_batch_rollback_replay(
        _pb004_runtime(batch_status=BATCH_STATE_ACCEPTED),
        severe_integration_failure=False,
        scenario_id="PB-009",
    )

    assert accepted_plan.rollback_required is False
    assert accepted_plan.cleanup_allowed is True
    assert accepted_plan.cleanup_blockers == ()
    assert accepted_plan.replay_task_ids == ()
    assert accepted_plan.operator_actions == (ACTION_CLEANUP_RETAINED_BRANCH,)
    assert {row["action"] for row in accepted_plan.dashboard_rows} == {
        ACTION_CLEANUP_RETAINED_BRANCH
    }


def test_pb009_persisted_accepted_batch_allows_cleanup_after_restart(tmp_path) -> None:
    db_path = str(tmp_path / "runtime.db")
    conn = _runtime_conn(db_path)
    upsert_batch_merge_runtime(
        conn,
        _pb004_runtime(batch_status=BATCH_STATE_ACCEPTED),
        now_iso="2026-05-17T06:15:00Z",
    )
    conn.commit()
    conn.close()

    restarted = _runtime_conn(db_path)
    plan = decide_persisted_batch_rollback_replay(
        restarted,
        PROJECT_ID,
        BATCH_ID,
        severe_integration_failure=False,
        scenario_id="PB-009",
    )

    assert plan.rollback_required is False
    assert plan.cleanup_allowed is True
    assert plan.cleanup_blockers == ()
    assert plan.operator_actions == (ACTION_CLEANUP_RETAINED_BRANCH,)
