"""PB-010 compact read model tests for parallel branch operator state."""

from __future__ import annotations

from agent.governance.parallel_branch_runtime import (
    BATCH_STATE_OPEN,
    BranchTaskRuntimeContext,
    BatchMergeItem,
    BatchMergeRuntime,
    MergeQueueItem,
    build_parallel_branch_read_model,
    decide_batch_rollback_replay,
    decide_merge_queue,
    decide_restart_recovery,
    runtime_tasks_from_contexts,
)

PROJECT_ID = "fixture-parallel-project"
BATCH_ID = "PB-010"
TARGET_REF = "refs/heads/main"
NOW = "2026-05-16T12:00:00Z"
EXPIRED = "2026-05-16T11:50:00Z"


def _contexts() -> list[BranchTaskRuntimeContext]:
    return [
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id=BATCH_ID,
            task_id="T1",
            backlog_id="OPT-PB010-T1",
            branch_ref="refs/heads/codex/PB010-T1-foundation",
            ref_name="main",
            status="merged",
            attempt=1,
            checkpoint_id="checkpoint-T1",
            replay_source="checkpoint",
            base_commit="B0",
            head_commit="H1",
            target_head_commit="M1",
            snapshot_id="scope-T1",
            projection_id="semproj-T1",
            merge_queue_id="mergeq-PB010",
            merge_preview_id="preview-T1",
        ),
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id=BATCH_ID,
            task_id="T2",
            backlog_id="OPT-PB010-T2",
            branch_ref="refs/heads/codex/PB010-T2-failed",
            ref_name="main",
            status="merge_failed",
            attempt=1,
            depends_on=("T1",),
            checkpoint_id="checkpoint-T2",
            base_commit="B0",
            head_commit="H2",
            snapshot_id="scope-T2",
            projection_id="semproj-T2",
            merge_queue_id="mergeq-PB010",
        ),
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id=BATCH_ID,
            task_id="T3",
            backlog_id="OPT-PB010-T3",
            branch_ref="refs/heads/codex/PB010-T3-reclaimable",
            ref_name="main",
            status="running",
            attempt=1,
            lease_id="lease-T3",
            lease_expires_at=EXPIRED,
            depends_on=("T1",),
            checkpoint_id="checkpoint-T3",
            replay_source="checkpoint",
            base_commit="B0",
            head_commit="H3",
            snapshot_id="scope-T3",
            projection_id="semproj-T3",
        ),
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id=BATCH_ID,
            task_id="T4",
            backlog_id="OPT-PB010-T4",
            branch_ref="refs/heads/codex/PB010-T4-blocked",
            ref_name="main",
            status="queued_for_merge",
            attempt=1,
            depends_on=("T2",),
            checkpoint_id="checkpoint-T4",
            base_commit="B0",
            head_commit="H4",
        ),
    ]


def _merge_queue_plan():
    return decide_merge_queue(
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="mergeq-PB010",
                queue_item_id="item-T1",
                task_id="T1",
                branch_ref="refs/heads/codex/PB010-T1-foundation",
                queue_index=1,
                status="running",
                target_ref=TARGET_REF,
            ),
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="mergeq-PB010",
                queue_item_id="item-T2",
                task_id="T2",
                branch_ref="refs/heads/codex/PB010-T2-feature",
                queue_index=2,
                status="merge_ready",
                target_ref=TARGET_REF,
                hard_depends_on=("T1",),
                requires_graph_epoch=("T1",),
            ),
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="mergeq-PB010",
                queue_item_id="item-T3",
                task_id="T3",
                branch_ref="refs/heads/codex/PB010-T3-independent",
                queue_index=3,
                status="merge_ready",
                target_ref=TARGET_REF,
            ),
        ],
        scenario_id="PB-010",
    )


def _batch_plan():
    return decide_batch_rollback_replay(
        BatchMergeRuntime(
            project_id=PROJECT_ID,
            batch_id=BATCH_ID,
            target_ref=TARGET_REF,
            batch_base_commit="B0",
            current_target_head="merge-T2",
            batch_status=BATCH_STATE_OPEN,
            rollback_snapshot_id="snapshot-main-B0",
            rollback_projection_id="semproj-main-B0",
            items=(
                BatchMergeItem(
                    task_id="T2",
                    branch_ref="refs/heads/codex/PB010-T2-feature",
                    worktree_path="/tmp/worktrees/PB010-T2",
                    queue_index=2,
                    status="merged",
                    branch_head="H2",
                    base_commit="B0",
                    merge_commit="merge-T2",
                    snapshot_id="scope-T2",
                    projection_id="semproj-T2",
                    retained=True,
                ),
                BatchMergeItem(
                    task_id="T1",
                    branch_ref="refs/heads/codex/PB010-T1-foundation",
                    worktree_path="/tmp/worktrees/PB010-T1",
                    queue_index=1,
                    status="merge_ready",
                    branch_head="H1",
                    base_commit="B0",
                    snapshot_id="scope-T1",
                    projection_id="semproj-T1",
                    retained=True,
                ),
            ),
        ),
        severe_integration_failure=True,
        corrected_replay_order=("T1", "T2"),
        scenario_id="PB-010",
    )


def test_pb010_compact_read_model_combines_branch_queue_and_rollback_state() -> None:
    contexts = _contexts()
    recovery_plan = decide_restart_recovery(
        runtime_tasks_from_contexts(contexts, now_iso=NOW),
        scenario_id="PB-010",
    )

    model = build_parallel_branch_read_model(
        project_id=PROJECT_ID,
        batch_id=BATCH_ID,
        contexts=contexts,
        recovery_plan=recovery_plan,
        merge_queue_plan=_merge_queue_plan(),
        batch_plan=_batch_plan(),
        limit=10,
    )
    payload = model.to_dict()

    assert payload["summary"]["lane_count"] == 4
    assert payload["summary"]["status_counts"] == {
        "dependency_blocked": 1,
        "merge_failed": 1,
        "merged": 1,
        "reclaimable": 1,
    }
    assert payload["summary"]["mergeable_count"] == 1
    assert payload["summary"]["blocked_count"] == 1
    assert payload["summary"]["rollback_required"] is True
    assert payload["summary"]["truncated"] is False

    lanes = {row["task_id"]: row for row in payload["branch_lanes"]}
    assert lanes["T1"]["graph_epoch"] == {
        "snapshot_id": "scope-T1",
        "projection_id": "semproj-T1",
        "base_commit": "B0",
        "head_commit": "H1",
        "target_head_commit": "M1",
        "rollback_epoch": "",
        "replay_epoch": "",
    }
    assert lanes["T3"]["status"] == "reclaimable"
    assert lanes["T3"]["recovery_actions"] == ["reclaim", "replay_from_checkpoint"]
    assert lanes["T4"]["status"] == "dependency_blocked"
    assert lanes["T4"]["dependency_blockers"] == ["T2"]

    queue_rows = {row["task_id"]: row for row in payload["merge_queue"]["rows"]}
    assert payload["merge_queue"]["mergeable_task_ids"] == ["T3"]
    assert queue_rows["T2"]["dependency_blocker_types"] == {
        "T1": ["hard_depends_on", "requires_graph_epoch"]
    }
    assert queue_rows["T2"]["next_actions"] == ["resolve_dependency", "do_not_merge"]

    assert payload["rollback"]["rollback_epoch"] == "rollback-PB-010-B0"
    assert payload["rollback"]["replay_epoch"] == "replay-PB-010-merge-T2"
    assert payload["rollback"]["retained_branch_refs"] == [
        "refs/heads/codex/PB010-T2-feature",
        "refs/heads/codex/PB010-T1-foundation",
    ]
    assert payload["rollback"]["cleanup_allowed"] is False
    assert payload["rollback"]["operator_actions"] == [
        "rollback_batch",
        "replay_through_merge_queue",
        "approve_cleanup_after_replay",
    ]


def test_pb010_read_model_is_bounded_and_marks_truncation() -> None:
    model = build_parallel_branch_read_model(
        project_id=PROJECT_ID,
        batch_id=BATCH_ID,
        contexts=_contexts(),
        merge_queue_plan=_merge_queue_plan(),
        batch_plan=_batch_plan(),
        limit=2,
    )
    payload = model.to_dict()

    assert [row["task_id"] for row in payload["branch_lanes"]] == ["T1", "T2"]
    assert payload["total_counts"] == {
        "branch_lanes": 4,
        "merge_queue_rows": 3,
        "rollback_rows": 2,
    }
    assert payload["truncated"] == {
        "branch_lanes": True,
        "merge_queue_rows": True,
        "rollback_rows": False,
    }
    assert payload["summary"]["truncated"] is True
