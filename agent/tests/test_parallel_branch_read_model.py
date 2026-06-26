"""PB-010 compact read model tests for parallel branch operator state."""

from __future__ import annotations

import sqlite3

from agent.tests.fixtures.parallel_project import (
    PB001RestartFixtureProject,
    create_pb001_restart_fixture_project,
)
from agent.governance.parallel_branch_runtime import (
    BATCH_STATE_OPEN,
    BranchTaskRuntimeContext,
    BatchMergeItem,
    BatchMergeRuntime,
    MergeQueueItem,
    build_parallel_branch_read_model,
    build_parallel_branch_read_model_from_db,
    decide_batch_rollback_replay,
    decide_merge_queue,
    decide_restart_recovery,
    plan_mf_batch_parallel_preflight,
    recover_expired_branch_contexts,
    runtime_tasks_from_contexts,
    upsert_batch_merge_runtime,
    upsert_branch_context,
    upsert_merge_queue_items,
)

PROJECT_ID = "fixture-parallel-project"
BATCH_ID = "PB-010"
PB001_BATCH_ID = "PB-001"
TARGET_REF = "refs/heads/main"
NOW = "2026-05-16T12:00:00Z"
EXPIRED = "2026-05-16T11:50:00Z"
PB001_TASK_IDS = ("T1", "T2", "T3", "T4", "T5")


def _runtime_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


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


def _pb001_contexts(fixture: PB001RestartFixtureProject) -> list[BranchTaskRuntimeContext]:
    branches = fixture.task_branches
    target_head = fixture.target_head_after_t1
    return [
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id=PB001_BATCH_ID,
            task_id="T1",
            backlog_id="OPT-PB001-T1",
            branch_ref=branches["T1"].branch_ref,
            ref_name="main",
            status="merged",
            base_commit=branches["T1"].base_commit,
            head_commit=branches["T1"].head_commit,
            target_head_commit=target_head,
            snapshot_id="scope-T1",
            projection_id="semproj-T1",
            merge_queue_id="mergeq-PB001",
            fence_token="fence-T1",
        ),
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id=PB001_BATCH_ID,
            task_id="T2",
            backlog_id="OPT-PB001-T2",
            branch_ref=branches["T2"].branch_ref,
            ref_name="main",
            status="merge_failed",
            depends_on=("T1",),
            base_commit=branches["T2"].base_commit,
            head_commit=branches["T2"].head_commit,
            target_head_commit=target_head,
            snapshot_id="scope-T2",
            projection_id="semproj-T2",
            merge_queue_id="mergeq-PB001",
            fence_token="fence-T2",
        ),
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id=PB001_BATCH_ID,
            task_id="T3",
            backlog_id="OPT-PB001-T3",
            branch_ref=branches["T3"].branch_ref,
            ref_name="main",
            status="running",
            attempt=1,
            lease_id="lease-T3",
            lease_expires_at=EXPIRED,
            depends_on=("T1",),
            checkpoint_id="checkpoint-T3",
            replay_source="checkpoint",
            base_commit=branches["T3"].base_commit,
            head_commit=branches["T3"].head_commit,
            target_head_commit=target_head,
            snapshot_id="scope-T3",
            projection_id="semproj-T3",
            fence_token="fence-old-T3",
        ),
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id=PB001_BATCH_ID,
            task_id="T4",
            backlog_id="OPT-PB001-T4",
            branch_ref=branches["T4"].branch_ref,
            ref_name="main",
            status="queued_for_merge",
            depends_on=("T2",),
            checkpoint_id="checkpoint-T4",
            base_commit=branches["T4"].base_commit,
            head_commit=branches["T4"].head_commit,
            target_head_commit=target_head,
            merge_queue_id="mergeq-PB001",
            fence_token="fence-T4",
        ),
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id=PB001_BATCH_ID,
            task_id="T5",
            backlog_id="OPT-PB001-T5",
            branch_ref=branches["T5"].branch_ref,
            ref_name="main",
            status="running",
            attempt=2,
            lease_id="lease-T5",
            lease_expires_at=EXPIRED,
            depends_on=("T3",),
            checkpoint_id="checkpoint-T5",
            replay_source="checkpoint",
            base_commit=branches["T5"].base_commit,
            head_commit=branches["T5"].head_commit,
            target_head_commit=target_head,
            snapshot_id="scope-T5",
            projection_id="semproj-T5",
            fence_token="fence-old-T5",
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


def test_mf_batch_parallel_preflight_plans_parallel_groups_and_queue_dependencies():
    plan = plan_mf_batch_parallel_preflight(
        project_id=PROJECT_ID,
        coordination_backlog_id="AC-BATCH",
        batch_id="batch-1",
        merge_queue_id="mq-batch-1",
        target_head_commit="HEAD1",
        snapshot_id="scope-HEAD1",
        backlog_rows=[
            {
                "bug_id": "AC-ROW-A",
                "status": "OPEN",
                "priority": "P1",
                "target_files": ["src/a.py"],
                "test_files": ["tests/test_a.py"],
            },
            {
                "bug_id": "AC-ROW-B",
                "status": "OPEN",
                "priority": "P0",
                "target_files": ["src/b.py"],
                "test_files": ["tests/test_b.py"],
            },
            {
                "bug_id": "AC-ROW-C",
                "status": "OPEN",
                "priority": "P2",
                "target_files": ["src/a.py"],
                "test_files": ["tests/test_c.py"],
            },
        ],
    )

    assert plan["status"] == "passed"
    assert plan["fanout_ready"] is True
    assert [row["backlog_id"] for row in plan["ordered_rows"]] == [
        "AC-ROW-B",
        "AC-ROW-A",
        "AC-ROW-C",
    ]
    assert plan["dispatch_groups"] == [
        {
            "group_index": 1,
            "backlog_ids": ["AC-ROW-B", "AC-ROW-A"],
            "reason": "no_overlap_with_group",
        },
        {
            "group_index": 2,
            "backlog_ids": ["AC-ROW-C"],
            "reason": "no_overlap_with_group",
        },
    ]
    planned = plan["merge_queue_plan"]["planned_items"]
    by_row = {item["backlog_id"]: item for item in planned}
    assert by_row["AC-ROW-C"]["serializes_after"] == [by_row["AC-ROW-A"]["task_id"]]
    assert by_row["AC-ROW-C"]["same_node_or_file_conflicts"] == [
        by_row["AC-ROW-A"]["task_id"]
    ]
    assert plan["merge_queue_plan"]["planner_only"] is True
    assert plan["merge_queue_plan"]["durable_queue_write"] is False


def test_mf_batch_parallel_preflight_strict_mode_serializes_every_row():
    plan = plan_mf_batch_parallel_preflight(
        project_id=PROJECT_ID,
        coordination_backlog_id="AC-BATCH",
        batch_id="batch-strict",
        merge_queue_id="mq-batch-strict",
        target_head_commit="HEAD1",
        mode="strict_ordered",
        backlog_rows=[
            {
                "bug_id": "AC-ROW-A",
                "status": "OPEN",
                "priority": "P1",
                "target_files": ["src/a.py"],
            },
            {
                "bug_id": "AC-ROW-B",
                "status": "OPEN",
                "priority": "P1",
                "target_files": ["src/b.py"],
            },
        ],
    )

    planned = plan["merge_queue_plan"]["planned_items"]
    assert plan["status"] == "passed"
    assert [group["backlog_ids"] for group in plan["dispatch_groups"]] == [
        ["AC-ROW-A"],
        ["AC-ROW-B"],
    ]
    assert planned[1]["serializes_after"] == [planned[0]["task_id"]]


def test_mf_batch_parallel_preflight_blocks_missing_target_head_and_files():
    plan = plan_mf_batch_parallel_preflight(
        project_id=PROJECT_ID,
        coordination_backlog_id="AC-BATCH",
        batch_id="batch-blocked",
        merge_queue_id="mq-batch-blocked",
        target_head_commit="",
        backlog_rows=[
            {
                "bug_id": "AC-ROW-A",
                "status": "OPEN",
                "priority": "P1",
                "target_files": [],
            },
            {
                "bug_id": "AC-ROW-B",
                "status": "FIXED",
                "priority": "P1",
                "target_files": ["src/b.py"],
            },
        ],
    )

    assert plan["fanout_ready"] is False
    assert {blocker["code"] for blocker in plan["blockers"]} >= {
        "missing_target_head_commit",
        "missing_target_files",
        "child_not_actionable",
    }


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


def test_pb010_read_model_loads_from_durable_runtime_stores() -> None:
    conn = _runtime_conn()
    for context in _contexts():
        upsert_branch_context(conn, context, now_iso=NOW)
    upsert_merge_queue_items(
        conn,
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
        ],
        now_iso=NOW,
    )
    upsert_batch_merge_runtime(
        conn,
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
        now_iso=NOW,
    )

    model = build_parallel_branch_read_model_from_db(
        conn,
        project_id=PROJECT_ID,
        batch_id=BATCH_ID,
        merge_queue_id="mergeq-PB010",
        target_ref=TARGET_REF,
        now_iso=NOW,
        severe_integration_failure=True,
        corrected_replay_order=("T1", "T2"),
        limit=10,
    )
    payload = model.to_dict()

    assert payload["summary"]["lane_count"] == 4
    assert payload["summary"]["blocked_count"] == 1
    assert payload["summary"]["rollback_required"] is True
    assert payload["merge_queue"]["blocked_task_ids"] == ["T2"]
    assert payload["rollback"]["replay_task_ids"] == ["T1", "T2"]
    assert payload["truncated"] == {
        "branch_lanes": False,
        "merge_queue_rows": False,
        "rollback_rows": False,
    }


def test_pb001_read_model_uses_generated_project_restart_topology(tmp_path) -> None:
    fixture = create_pb001_restart_fixture_project(tmp_path)
    conn = _runtime_conn()
    for context in _pb001_contexts(fixture):
        upsert_branch_context(conn, context, now_iso=NOW)

    recovered = recover_expired_branch_contexts(conn, PROJECT_ID, now_iso=NOW)
    assert [context.task_id for context in recovered] == ["T3", "T5"]

    model = build_parallel_branch_read_model_from_db(
        conn,
        project_id=PROJECT_ID,
        batch_id=PB001_BATCH_ID,
        now_iso=NOW,
        scenario_id="PB-001",
        limit=10,
    )
    payload = model.to_dict()
    lanes = {row["task_id"]: row for row in payload["branch_lanes"]}

    assert payload["summary"]["lane_count"] == 5
    assert payload["summary"]["status_counts"] == {
        "dependency_blocked": 1,
        "merge_failed": 1,
        "merged": 1,
        "reclaimable": 2,
    }
    assert [lanes[task_id]["branch_ref"] for task_id in PB001_TASK_IDS] == [
        fixture.task_branches[task_id].branch_ref for task_id in PB001_TASK_IDS
    ]
    assert lanes["T1"]["graph_epoch"]["head_commit"] == fixture.task_branches["T1"].head_commit
    assert lanes["T1"]["graph_epoch"]["target_head_commit"] == fixture.target_head_after_t1
    assert lanes["T3"]["observed_status"] == "reclaimable"
    assert lanes["T3"]["graph_epoch"]["head_commit"] == fixture.task_branches["T3"].head_commit
    assert lanes["T3"]["recovery_actions"] == ["reclaim", "replay_from_checkpoint"]
    assert lanes["T4"]["status"] == "dependency_blocked"
    assert lanes["T4"]["dependency_blockers"] == ["T2"]
    assert lanes["T5"]["recovery_actions"] == [
        "wait_for_dependency",
        "reclaim",
        "replay_from_checkpoint",
    ]


def test_pb010_read_model_marks_queue_stale_against_latest_target_head() -> None:
    conn = _runtime_conn()
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="mergeq-PB010-stale-target",
                queue_item_id="item-T1",
                task_id="T1",
                branch_ref="refs/heads/codex/PB010-T1",
                queue_index=1,
                status="merge_ready",
                target_ref=TARGET_REF,
                validated_target_head="target-before",
                current_target_head="target-before",
                merge_preview_id="preview-before",
            ),
        ],
        now_iso=NOW,
    )

    model = build_parallel_branch_read_model_from_db(
        conn,
        project_id=PROJECT_ID,
        merge_queue_id="mergeq-PB010-stale-target",
        target_ref=TARGET_REF,
        current_target_head="target-after",
        now_iso=NOW,
        limit=10,
    )
    payload = model.to_dict()
    row = payload["merge_queue"]["rows"][0]

    assert payload["summary"]["mergeable_count"] == 0
    assert payload["summary"]["stale_count"] == 1
    assert payload["merge_queue"]["stale_task_ids"] == ["T1"]
    assert row["stale_target_head"] is True
    assert row["queue_state"] == "stale_after_dependency_merge"
    assert row["next_actions"] == [
        "rebase_or_sync",
        "run_scope_reconcile",
        "verify_semantic_projection",
        "refresh_merge_preview",
    ]
