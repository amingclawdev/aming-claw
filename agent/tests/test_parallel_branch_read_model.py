"""PB-010 compact read model tests for parallel branch operator state."""

from __future__ import annotations

from dataclasses import replace
import sqlite3

from agent.governance import mf_subagent_contract as mf_contract
from agent.governance import parallel_branch_runtime as pbr
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
            "backlog_ids": ["AC-ROW-B"],
            "reason": "no_overlap",
            "overlap_component": False,
        },
        {
            "group_index": 2,
            "backlog_ids": ["AC-ROW-A", "AC-ROW-C"],
            "reason": "connected_overlap_component",
            "overlap_component": True,
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


def test_mf_batch_parallel_preflight_groups_connected_overlap_component():
    plan = plan_mf_batch_parallel_preflight(
        project_id=PROJECT_ID,
        coordination_backlog_id="AC-BATCH",
        batch_id="batch-overlap",
        merge_queue_id="mq-batch-overlap",
        target_head_commit="HEAD1",
        snapshot_id="scope-HEAD1",
        backlog_rows=[
            {
                "bug_id": "AC-ROW-A",
                "status": "OPEN",
                "priority": "P0",
                "target_files": ["agent/governance/server.py"],
                "test_files": ["agent/tests/test_graph_governance_api.py"],
            },
            {
                "bug_id": "AC-ROW-B",
                "status": "OPEN",
                "priority": "P1",
                "target_files": ["agent/governance/server.py"],
                "test_files": ["agent/tests/test_mcp_server_stdio.py"],
            },
            {
                "bug_id": "AC-ROW-C",
                "status": "OPEN",
                "priority": "P2",
                "target_files": ["agent/governance/mcp_server.py"],
                "test_files": ["agent/tests/test_mcp_server_stdio.py"],
            },
            {
                "bug_id": "AC-ROW-D",
                "status": "OPEN",
                "priority": "P3",
                "target_files": ["agent/governance/mcp_server.py"],
                "test_files": ["agent/tests/test_graph_governance_api.py"],
            },
        ],
    )

    assert plan["status"] == "passed"
    assert plan["dispatch_groups"] == [
        {
            "group_index": 1,
            "backlog_ids": ["AC-ROW-A", "AC-ROW-B", "AC-ROW-C", "AC-ROW-D"],
            "reason": "connected_overlap_component",
            "overlap_component": True,
        }
    ]
    planned = plan["merge_queue_plan"]["planned_items"]
    by_row = {item["backlog_id"]: item for item in planned}
    assert by_row["AC-ROW-B"]["serializes_after"] == [by_row["AC-ROW-A"]["task_id"]]
    assert by_row["AC-ROW-C"]["serializes_after"] == [by_row["AC-ROW-B"]["task_id"]]
    assert by_row["AC-ROW-D"]["serializes_after"] == [
        by_row["AC-ROW-A"]["task_id"],
        by_row["AC-ROW-C"]["task_id"],
    ]


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


def test_serialized_batch_read_model_keeps_row2_visible_after_row1_merge():
    conn = _runtime_conn()
    batch_id = "batch-row2-recovery"
    queue_id = "mq-row2-recovery"
    plan = plan_mf_batch_parallel_preflight(
        project_id=PROJECT_ID,
        coordination_backlog_id="AC-BATCH-ROW2",
        batch_id=batch_id,
        merge_queue_id=queue_id,
        target_head_commit="target-before-row1",
        snapshot_id="scope-target-before-row1",
        target_ref=TARGET_REF,
        mode="strict_ordered",
        backlog_rows=[
            {
                "bug_id": "AC-ROW-ONE",
                "status": "OPEN",
                "priority": "P1",
                "target_files": ["src/one.py"],
            },
            {
                "bug_id": "AC-ROW-TWO",
                "status": "OPEN",
                "priority": "P1",
                "target_files": ["src/two.py"],
            },
        ],
    )
    planned = plan["merge_queue_plan"]["planned_items"]
    row1_task = planned[0]["task_id"]
    row2_task = planned[1]["task_id"]
    queue_items = [
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id=str(item["merge_queue_id"]),
            queue_item_id=str(item["queue_item_id"]),
            backlog_id=str(item["backlog_id"]),
            task_id=str(item["task_id"]),
            branch_ref=str(item.get("branch_ref") or ""),
            queue_index=int(item["queue_index"]),
            status=str(item["status"]),
            depends_on=tuple(item.get("depends_on") or ()),
            hard_depends_on=tuple(item.get("hard_depends_on") or ()),
            serializes_after=tuple(item.get("serializes_after") or ()),
            conflicts_with=tuple(item.get("conflicts_with") or ()),
            same_node_or_file_conflicts=tuple(
                item.get("same_node_or_file_conflicts") or ()
            ),
            requires_graph_epoch=tuple(item.get("requires_graph_epoch") or ()),
            target_ref=str(item["target_ref"]),
            base_commit=str(item["target_head_commit"]),
            validated_target_head=str(item["target_head_commit"]),
            current_target_head=str(item["target_head_commit"]),
            snapshot_id=str(item.get("snapshot_id") or ""),
        )
        for item in planned
    ]
    upsert_merge_queue_items(conn, queue_items, now_iso=NOW)

    before = build_parallel_branch_read_model_from_db(
        conn,
        project_id=PROJECT_ID,
        batch_id=batch_id,
        merge_queue_id=queue_id,
        target_ref=TARGET_REF,
        now_iso=NOW,
        limit=10,
    ).to_dict()
    before_rows = {row["task_id"]: row for row in before["merge_queue"]["rows"]}

    assert [row["backlog_id"] for row in before["merge_queue"]["rows"]] == [
        "AC-ROW-ONE",
        "AC-ROW-TWO",
    ]
    assert before["merge_queue"]["blocked_task_ids"] == [row2_task]
    assert before_rows[row2_task]["queue_item_id"] == planned[1]["queue_item_id"]
    assert before_rows[row2_task]["target_ref"] == TARGET_REF
    assert before_rows[row2_task]["serializes_after"] == [row1_task]
    assert before_rows[row2_task]["dependency_blockers"] == [row1_task]
    assert before_rows[row2_task]["queue_state"] == "waiting_dependency"
    assert before_rows[row2_task]["action"] == "wait_for_dependency"

    upsert_merge_queue_items(
        conn,
        [
            replace(
                queue_items[0],
                status=pbr.STATE_MERGED,
                merge_commit="merge-row-one",
                snapshot_id="scope-row-one",
                projection_id="semproj-row-one",
                target_head_after_merge="target-after-row1",
                current_target_head="target-after-row1",
            )
        ],
        now_iso=NOW,
    )

    after = build_parallel_branch_read_model_from_db(
        conn,
        project_id=PROJECT_ID,
        batch_id=batch_id,
        merge_queue_id=queue_id,
        target_ref=TARGET_REF,
        now_iso=NOW,
        limit=10,
    ).to_dict()
    after_rows = {row["task_id"]: row for row in after["merge_queue"]["rows"]}
    row2 = after_rows[row2_task]

    assert [row["task_id"] for row in after["merge_queue"]["rows"]] == [
        row1_task,
        row2_task,
    ]
    assert after["merge_queue"]["blocked_task_ids"] == []
    assert row2["backlog_id"] == "AC-ROW-TWO"
    assert row2["queue_item_id"] == planned[1]["queue_item_id"]
    assert row2["target_ref"] == TARGET_REF
    assert row2["depends_on"] == [row1_task]
    assert row2["hard_depends_on"] == [row1_task]
    assert row2["serializes_after"] == [row1_task]
    assert row2["dependency_blockers"] == []
    assert row2["observed_status"] == "planned"
    assert row2["queue_state"] == "planned"
    assert row2["action"] == "dispatch_serialized_successor"
    assert row2["next_actions"] == [
        "dispatch_serialized_successor",
        "enter_mf_parallel_successor",
        "do_not_merge",
    ]
    assert row2["merge_allowed"] is False


def test_serialized_batch_read_model_recovers_missing_row2_from_context_lineage():
    conn = _runtime_conn()
    batch_id = "mf-batch-parallel-84d4cdf62e77b2c1cc54"
    queue_id = "mq-9ef3fe376e3d7a196350"
    row2_backlog = "AC-MF-PARALLEL-MERGE-CLOSE-ROUTE-GUIDE-20260705"
    plan = plan_mf_batch_parallel_preflight(
        project_id=PROJECT_ID,
        coordination_backlog_id="AC-BATCH-ROW2",
        batch_id=batch_id,
        merge_queue_id=queue_id,
        target_head_commit="target-before-row1",
        snapshot_id="scope-target-before-row1",
        target_ref=TARGET_REF,
        mode="strict_ordered",
        backlog_rows=[
            {
                "bug_id": "AC-ROW-ONE",
                "status": "OPEN",
                "priority": "P1",
                "target_files": ["src/one.py"],
            },
            {
                "bug_id": row2_backlog,
                "status": "OPEN",
                "priority": "P1",
                "target_files": ["src/two.py"],
            },
        ],
    )
    planned = plan["merge_queue_plan"]["planned_items"]
    row1_task = planned[0]["task_id"]
    row2_task = planned[1]["task_id"]
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=queue_id,
                queue_item_id=str(planned[0]["queue_item_id"]),
                backlog_id="AC-ROW-ONE",
                task_id=row1_task,
                branch_ref="refs/heads/codex/live-row1",
                queue_index=1,
                status=pbr.STATE_MERGED,
                depends_on=(),
                target_ref=TARGET_REF,
                base_commit="target-before-row1",
                branch_head="row1-head",
                current_target_head="target-after-row1",
                merge_commit="merge-row1",
                target_head_after_merge="target-after-row1",
            )
        ],
        now_iso=NOW,
    )
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id=batch_id,
            task_id=row2_task,
            backlog_id=row2_backlog,
            branch_ref="",
            status="planned",
            depends_on=(row1_task,),
            target_head_commit="target-after-row1",
            merge_queue_id=queue_id,
        ),
        now_iso=NOW,
    )

    payload = build_parallel_branch_read_model_from_db(
        conn,
        project_id=PROJECT_ID,
        batch_id=batch_id,
        merge_queue_id=queue_id,
        target_ref=TARGET_REF,
        now_iso=NOW,
        limit=10,
    ).to_dict()
    rows = {row["task_id"]: row for row in payload["merge_queue"]["rows"]}
    row2 = rows[row2_task]

    assert [row["task_id"] for row in payload["merge_queue"]["rows"]] == [
        row1_task,
        row2_task,
    ]
    assert row2["backlog_id"] == row2_backlog
    assert row2["merge_queue_id"] == queue_id
    assert row2["queue_item_id"] == planned[1]["queue_item_id"]
    assert row2["depends_on"] == [row1_task]
    assert row2["lineage_status"] == "recovered_task_queue_dependency_lineage"
    assert row2["lineage_source"] == "parallel_branch_runtime_contexts"
    assert row2["governed_recovery_actions"] == [
        "materialize_active_merge_queue_item"
    ]
    assert row2["action"] == "dispatch_serialized_successor"


def test_batch_read_model_surfaces_governed_recovery_for_unbound_row2_lineage():
    conn = _runtime_conn()
    batch_id = "batch-row2-batch-lineage-only"
    queue_id = "mq-row2-batch-lineage-only"
    row1_task = f"{batch_id}:row:1"
    row2_task = f"{batch_id}:row:2"
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=queue_id,
                queue_item_id="item-row1",
                backlog_id="AC-ROW-ONE",
                task_id=row1_task,
                branch_ref="refs/heads/codex/row1",
                queue_index=1,
                status=pbr.STATE_MERGED,
                target_ref=TARGET_REF,
                merge_commit="merge-row1",
                current_target_head="target-after-row1",
            )
        ],
        now_iso=NOW,
    )
    upsert_batch_merge_runtime(
        conn,
        BatchMergeRuntime(
            project_id=PROJECT_ID,
            batch_id=batch_id,
            target_ref=TARGET_REF,
            batch_base_commit="target-before-row1",
            current_target_head="target-after-row1",
            batch_status=BATCH_STATE_OPEN,
            items=(
                BatchMergeItem(
                    task_id=row1_task,
                    branch_ref="refs/heads/codex/row1",
                    worktree_path="/tmp/row1",
                    queue_index=1,
                    status=pbr.STATE_MERGED,
                    branch_head="row1-head",
                    merge_queue_id=queue_id,
                    merge_commit="merge-row1",
                    retained=True,
                ),
                BatchMergeItem(
                    task_id=row2_task,
                    branch_ref="",
                    worktree_path="",
                    queue_index=2,
                    status="planned",
                    branch_head="",
                    base_commit="target-after-row1",
                    merge_queue_id=queue_id,
                    depends_on=(row1_task,),
                    retained=True,
                ),
            ),
        ),
        now_iso=NOW,
    )

    payload = build_parallel_branch_read_model_from_db(
        conn,
        project_id=PROJECT_ID,
        batch_id=batch_id,
        merge_queue_id=queue_id,
        target_ref=TARGET_REF,
        now_iso=NOW,
        limit=10,
    ).to_dict()
    rows = {row["task_id"]: row for row in payload["merge_queue"]["rows"]}
    row2 = rows[row2_task]

    assert row2["merge_queue_id"] == queue_id
    assert row2["backlog_id"] == ""
    assert row2["depends_on"] == [row1_task]
    assert row2["lineage_status"] == "governed_recovery_required"
    assert row2["lineage_source"] == "parallel_branch_batch_items"
    assert row2["governed_recovery_actions"] == [
        "recover_child_backlog_lineage",
        "materialize_active_merge_queue_item",
    ]


def test_active_batch_read_model_filters_stale_queue_lane_from_current_queue():
    conn = _runtime_conn()
    batch_id = "mf-batch-parallel-84d4cdf62e77b2c1cc54"
    current_queue_id = "mq-9ef3fe376e3d7a196350"
    stale_queue_id = "mq-65d78059566f39d29dda3307"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id=batch_id,
            task_id=f"{batch_id}:row:1",
            backlog_id="AC-ROW-ONE",
            branch_ref="refs/heads/codex/live-row1",
            status=pbr.STATE_MERGED,
            merge_queue_id=current_queue_id,
        ),
        now_iso=NOW,
    )
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id=batch_id,
            task_id="legacy-worker-row1",
            backlog_id="AC-ROW-ONE",
            branch_ref="refs/heads/codex/legacy-row1",
            status=pbr.STATE_WORKTREE_READY,
            merge_queue_id=stale_queue_id,
        ),
        now_iso=NOW,
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=current_queue_id,
                queue_item_id="item-row1",
                backlog_id="AC-ROW-ONE",
                task_id=f"{batch_id}:row:1",
                branch_ref="refs/heads/codex/live-row1",
                queue_index=1,
                status=pbr.STATE_MERGED,
                target_ref=TARGET_REF,
            )
        ],
        now_iso=NOW,
    )

    payload = build_parallel_branch_read_model_from_db(
        conn,
        project_id=PROJECT_ID,
        batch_id=batch_id,
        merge_queue_id=current_queue_id,
        target_ref=TARGET_REF,
        now_iso=NOW,
        limit=10,
    ).to_dict()

    assert [row["task_id"] for row in payload["branch_lanes"]] == [
        f"{batch_id}:row:1",
    ]
    assert payload["branch_lanes"][0]["merge_queue_id"] == current_queue_id


def test_active_batch_read_model_filters_stale_queue_lane_with_missing_context_queue():
    conn = _runtime_conn()
    batch_id = "mf-batch-parallel-84d4cdf62e77b2c1cc54"
    current_queue_id = "mq-9ef3fe376e3d7a196350"
    stale_queue_id = "mq-65d78059566f39d29dda3307"
    current_task_id = f"{batch_id}:row:1"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id=batch_id,
            task_id=current_task_id,
            backlog_id="AC-ROW-ONE",
            branch_ref="refs/heads/codex/live-row1",
            status=pbr.STATE_MERGED,
            merge_queue_id=current_queue_id,
        ),
        now_iso=NOW,
    )
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id=batch_id,
            task_id="legacy-worker-row1",
            backlog_id="AC-ROW-ONE",
            branch_ref="refs/heads/codex/legacy-row1",
            status=pbr.STATE_WORKTREE_READY,
            target_head_commit="target-before-row2-recovery",
            merge_queue_id="",
            merge_preview_id=f"{stale_queue_id}:preview",
        ),
        now_iso=NOW,
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=current_queue_id,
                queue_item_id="item-row1",
                backlog_id="AC-ROW-ONE",
                task_id=current_task_id,
                branch_ref="refs/heads/codex/live-row1",
                queue_index=1,
                status=pbr.STATE_MERGED,
                target_ref=TARGET_REF,
            )
        ],
        now_iso=NOW,
    )

    payload = build_parallel_branch_read_model_from_db(
        conn,
        project_id=PROJECT_ID,
        batch_id=batch_id,
        merge_queue_id=current_queue_id,
        target_ref=TARGET_REF,
        now_iso=NOW,
        limit=10,
    ).to_dict()

    assert [row["task_id"] for row in payload["branch_lanes"]] == [current_task_id]
    assert payload["summary"]["status_counts"] == {pbr.STATE_MERGED: 1}


def test_active_batch_read_model_filters_unscoped_stale_running_lane_from_current_queue():
    conn = _runtime_conn()
    batch_id = "mf-batch-parallel-84d4cdf62e77b2c1cc54"
    current_queue_id = "mq-9ef3fe376e3d7a196350"
    stale_queue_id = "mq-65d78059566f39d29dda3307"
    current_task_id = f"{batch_id}:row:1"
    stale_task_id = "legacy-row2-recovery-worker"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id=batch_id,
            task_id=current_task_id,
            backlog_id="AC-ROW-ONE",
            branch_ref="refs/heads/codex/live-row1",
            status=pbr.STATE_MERGED,
            merge_queue_id=current_queue_id,
        ),
        now_iso=NOW,
    )
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id=batch_id,
            task_id=stale_task_id,
            backlog_id="AC-STALE-ROW-TWO",
            branch_ref="refs/heads/codex/stale-row2-recovery",
            status=pbr.STATE_RUNNING,
            target_head_commit="target-before-row2-recovery",
            merge_queue_id="",
            merge_preview_id=f"{stale_queue_id}:preview",
        ),
        now_iso=NOW,
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=current_queue_id,
                queue_item_id="item-row1",
                backlog_id="AC-ROW-ONE",
                task_id=current_task_id,
                branch_ref="refs/heads/codex/live-row1",
                queue_index=1,
                status=pbr.STATE_MERGED,
                target_ref=TARGET_REF,
            )
        ],
        now_iso=NOW,
    )

    payload = build_parallel_branch_read_model_from_db(
        conn,
        project_id=PROJECT_ID,
        batch_id=batch_id,
        merge_queue_id=current_queue_id,
        target_ref=TARGET_REF,
        now_iso=NOW,
        limit=10,
    ).to_dict()

    assert [row["task_id"] for row in payload["branch_lanes"]] == [current_task_id]
    assert stale_task_id not in {
        row["task_id"] for row in payload["branch_lanes"]
    }


def test_live_read_model_filters_unbound_stale_queue_lane_from_active_queue():
    conn = _runtime_conn()
    current_queue_id = "mq-9ef3fe376e3d7a196350"
    stale_queue_id = "mq-65d78059566f39d29dda3307"
    current_task_id = "mf-parallel-branch-lanes-live-filter-worker-20260706"
    stale_task_id = "mf-batch-qa-graph-evidence-shape-worker-20260705"
    stale_recovery_task_id = "mf-batch-qa-graph-evidence-shape-worker-row2-20260705"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id=current_task_id,
            backlog_id="AC-CURRENT",
            branch_ref="refs/heads/codex/current-lane",
            status=pbr.STATE_QUEUED_FOR_MERGE,
            merge_queue_id=current_queue_id,
        ),
        now_iso=NOW,
    )
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id=stale_task_id,
            backlog_id="AC-STALE",
            branch_ref="refs/heads/codex/stale-lane",
            status=pbr.STATE_RUNNING,
            merge_queue_id="",
            merge_preview_id=f"{stale_queue_id}:preview",
        ),
        now_iso=NOW,
    )
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id=stale_recovery_task_id,
            backlog_id="AC-STALE-RECOVERY",
            branch_ref="refs/heads/codex/stale-recovery-lane",
            status=pbr.STATE_QUEUED_FOR_MERGE,
            merge_queue_id=stale_queue_id,
        ),
        now_iso=NOW,
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=current_queue_id,
                queue_item_id="item-current",
                backlog_id="AC-CURRENT",
                task_id=current_task_id,
                branch_ref="refs/heads/codex/current-lane",
                queue_index=1,
                status=pbr.STATE_QUEUED_FOR_MERGE,
                target_ref=TARGET_REF,
            ),
        ],
        now_iso=NOW,
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=stale_queue_id,
                queue_item_id="item-stale",
                backlog_id="AC-STALE",
                task_id=stale_task_id,
                branch_ref="refs/heads/codex/stale-lane",
                queue_index=1,
                status=pbr.STATE_RUNNING,
                target_ref=TARGET_REF,
            ),
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=stale_queue_id,
                queue_item_id="item-stale-recovery",
                backlog_id="AC-STALE-RECOVERY",
                task_id=stale_recovery_task_id,
                branch_ref="refs/heads/codex/stale-recovery-lane",
                queue_index=2,
                status=pbr.STATE_QUEUED_FOR_MERGE,
                target_ref=TARGET_REF,
            ),
        ],
        now_iso=NOW,
    )

    active_payload = build_parallel_branch_read_model_from_db(
        conn,
        project_id=PROJECT_ID,
        merge_queue_id=current_queue_id,
        target_ref=TARGET_REF,
        now_iso=NOW,
        limit=10,
    ).to_dict()
    stale_payload = build_parallel_branch_read_model_from_db(
        conn,
        project_id=PROJECT_ID,
        merge_queue_id=stale_queue_id,
        target_ref=TARGET_REF,
        now_iso=NOW,
        limit=10,
    ).to_dict()

    assert [row["task_id"] for row in active_payload["merge_queue"]["rows"]] == [
        current_task_id
    ]
    assert [row["task_id"] for row in active_payload["branch_lanes"]] == [
        current_task_id
    ]
    assert [row["task_id"] for row in stale_payload["merge_queue"]["rows"]] == [
        stale_task_id,
        stale_recovery_task_id,
    ]
    assert [row["task_id"] for row in stale_payload["branch_lanes"]] == [
        stale_task_id,
        stale_recovery_task_id,
    ]


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


def test_pb010_read_model_derives_queue_branch_ref_from_lane_when_target_filter_empty() -> None:
    conn = _runtime_conn()
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id=BATCH_ID,
            task_id="T-context-ref",
            backlog_id="OPT-PB010-CONTEXT-REF",
            branch_ref="refs/heads/codex/PB010-context-ref",
            ref_name="main",
            status="running",
            base_commit="B0",
            head_commit="H-context",
            target_head_commit="M0",
            merge_queue_id="mergeq-PB010-context-ref",
        ),
        now_iso=NOW,
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="mergeq-PB010-context-ref",
                queue_item_id="item-context-ref",
                task_id="T-context-ref",
                branch_ref="",
                queue_index=1,
                status="planned",
                target_ref="",
            ),
        ],
        now_iso=NOW,
    )

    model = build_parallel_branch_read_model_from_db(
        conn,
        project_id=PROJECT_ID,
        merge_queue_id="mergeq-PB010-context-ref",
        target_ref=TARGET_REF,
        now_iso=NOW,
        limit=10,
    )
    payload = model.to_dict()
    row = payload["merge_queue"]["rows"][0]

    assert row["task_id"] == "T-context-ref"
    assert row["branch_ref"] == "refs/heads/codex/PB010-context-ref"
    assert row["target_ref"] == TARGET_REF
    assert row["queue_state"] == "planned"
    assert row["action"] == "dispatch_serialized_successor"


def test_pb010_read_model_filters_old_target_ref_lanes_from_current_queue() -> None:
    conn = _runtime_conn()
    queue_id = "mergeq-PB010-current-target-filter"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T-current-target",
            branch_ref="refs/heads/codex/PB010-current-target",
            ref_name=TARGET_REF,
            status="planned",
            merge_queue_id=queue_id,
        ),
        now_iso=NOW,
    )
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T-old-target",
            branch_ref="refs/heads/codex/PB010-old-target",
            ref_name="refs/heads/old-main",
            status="planned",
            merge_queue_id=queue_id,
        ),
        now_iso=NOW,
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=queue_id,
                queue_item_id="item-current-target",
                backlog_id="AC-CURRENT-TARGET",
                task_id="T-current-target",
                branch_ref="refs/heads/codex/PB010-current-target",
                queue_index=1,
                status="planned",
                target_ref=TARGET_REF,
            ),
        ],
        now_iso=NOW,
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=queue_id,
                queue_item_id="item-old-target",
                backlog_id="AC-OLD-TARGET",
                task_id="T-old-target",
                branch_ref="refs/heads/codex/PB010-old-target",
                queue_index=2,
                status="planned",
                target_ref="refs/heads/old-main",
            ),
        ],
        now_iso=NOW,
    )

    model = build_parallel_branch_read_model_from_db(
        conn,
        project_id=PROJECT_ID,
        merge_queue_id=queue_id,
        target_ref=TARGET_REF,
        now_iso=NOW,
        limit=10,
    )
    payload = model.to_dict()

    assert [row["task_id"] for row in payload["merge_queue"]["rows"]] == [
        "T-current-target",
    ]
    row = payload["merge_queue"]["rows"][0]
    assert row["backlog_id"] == "AC-CURRENT-TARGET"
    assert row["queue_item_id"] == "item-current-target"
    assert row["target_ref"] == TARGET_REF
    assert [row["task_id"] for row in payload["branch_lanes"]] == [
        "T-current-target",
    ]


def test_merge_queue_apply_consumes_already_integrated_lane_without_target_mutation(
    monkeypatch,
    tmp_path,
) -> None:
    conn = _runtime_conn()
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id=BATCH_ID,
            task_id="T-integrated",
            backlog_id="OPT-PB010-INTEGRATED",
            branch_ref="refs/heads/codex/PB010-integrated",
            ref_name="main",
            status="running",
            base_commit="B0",
            head_commit="branch-commit",
            target_head_commit="target-before",
            merge_queue_id="mergeq-PB010-integrated",
        ),
        now_iso=NOW,
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="mergeq-PB010-integrated",
                queue_item_id="item-integrated",
                task_id="T-integrated",
                branch_ref="",
                queue_index=1,
                status="planned",
                target_ref="",
            ),
        ],
        now_iso=NOW,
    )
    preview_call: dict[str, object] = {}

    def fake_preview(**kwargs):
        preview_call.update(kwargs)
        return {
            "status": "pass",
            "passed": True,
            "target_commit": "target-after",
            "branch_commit": "branch-commit",
        }

    monkeypatch.setattr(pbr, "git_merge_preview_evidence", fake_preview)
    monkeypatch.setattr(pbr, "_git_preview_branch_is_ancestor", lambda *args, **kwargs: True)

    result = pbr.execute_merge_queue_item(
        conn,
        project_id=PROJECT_ID,
        merge_queue_id="mergeq-PB010-integrated",
        repo_root_path=tmp_path,
        task_id="T-integrated",
        target_ref=TARGET_REF,
        dry_run=False,
        allow_target_ref_mutation=False,
        now_iso=NOW,
    )

    assert result["ok"] is True
    assert result["already_integrated"] is True
    assert result["target_ref_mutated"] is False
    assert preview_call["branch_ref"] == "refs/heads/codex/PB010-integrated"
    saved = pbr.list_merge_queue_items(conn, PROJECT_ID, "mergeq-PB010-integrated")[0]
    assert saved.status == "merged"
    assert saved.branch_ref == "refs/heads/codex/PB010-integrated"
    assert saved.merge_commit == "target-after"
    terminal_row = decide_merge_queue(
        [saved],
        scenario_id="mf_parallel-already-integrated",
    ).dashboard_rows[0]
    assert terminal_row["durable_status_policy"]["close_satisfying"] is True
    assert terminal_row["durable_status_policy"]["live_apply_ready"] is False
    assert "copy_safe_recovery" not in terminal_row

    replay = pbr.execute_merge_queue_item(
        conn,
        project_id=PROJECT_ID,
        merge_queue_id="mergeq-PB010-integrated",
        repo_root_path=tmp_path,
        task_id="T-integrated",
        target_ref=TARGET_REF,
        dry_run=False,
        allow_target_ref_mutation=False,
        now_iso=NOW,
    )

    assert replay["ok"] is True
    assert replay["already_integrated"] is True
    assert replay["target_ref_mutated"] is False
    assert replay["merge_commit"] == "target-after"
    replayed_rows = pbr.list_merge_queue_items(
        conn,
        PROJECT_ID,
        "mergeq-PB010-integrated",
    )
    assert len(replayed_rows) == 1
    assert replayed_rows[0].status == "merged"
    assert replayed_rows[0].merge_commit == "target-after"
    replay_row = decide_merge_queue(
        replayed_rows,
        scenario_id="mf_parallel-already-integrated-replay",
    ).dashboard_rows[0]
    assert replay_row["durable_status_policy"]["close_satisfying"] is True
    assert "copy_safe_recovery" not in replay_row


def test_runtime_context_worker_views_surface_mf_parallel_happy_path_reminders() -> None:
    context = BranchTaskRuntimeContext(
        project_id=PROJECT_ID,
        task_id="T-reminders",
        parent_task_id="cex-parent-reminders",
        backlog_id="AC-REMINDERS",
        branch_ref="refs/heads/codex/reminders",
        status=pbr.STATE_WORKTREE_READY,
        runtime_context_id="mfrctx-reminders",
        worker_id="worker-reminders",
        worker_slot_id="worker-reminders",
        fence_token="fence-reminders",
        worktree_path="/tmp/reminders",
        base_commit="base-reminders",
        head_commit="head-reminders",
        target_head_commit="target-reminders",
        merge_queue_id="mq-runtime-authoritative",
        target_files=("agent/governance/parallel_branch_runtime.py",),
        owned_files=("agent/governance/parallel_branch_runtime.py",),
    )
    current_view = pbr.build_runtime_context_current_view(
        context,
        route_identity={
            "route_id": "route-reminders",
            "route_context_hash": "sha256:route-reminders",
            "prompt_contract_id": "rprompt-reminders",
            "prompt_contract_hash": "sha256:prompt-reminders",
            "route_token_ref": "rtok-reminders",
            "visible_injection_manifest_hash": "sha256:visible-reminders",
        },
    )

    action_plan = pbr.build_runtime_context_action_plan_view(current_view)
    reminders = action_plan["mf_parallel_happy_path_reminders"]

    assert reminders["schema_version"] == (
        "runtime_context.mf_parallel_happy_path_reminders.v1"
    )
    assert reminders["merge_queue_id"] == "mq-runtime-authoritative"
    batch_diagnostics = reminders["batch_close_blocker_diagnostics"]
    assert batch_diagnostics["status"] == "keep_open"
    assert batch_diagnostics["close_state_policy"]["rows_must_remain_open"] == [
        "AC-MF-BATCH-QA-MERGE-GUIDE-DOGFOOD-20260705",
        "AC-MF-BATCH-DOGFOOD-CLOSE-BLOCKERS-FRICTION-20260706",
    ]
    assert (
        batch_diagnostics["close_state_policy"]["post_hoc_close_evidence_allowed"]
        is False
    )
    assert (
        batch_diagnostics["evidence_provenance_policy"][
            "observer_lane_backfill_allowed"
        ]
        is False
    )
    assert "close_ready" in batch_diagnostics["evidence_provenance_policy"][
        "forbidden_evidence_kinds"
    ]
    assert batch_diagnostics["graph_reconcile_route_proof"][
        "raw_route_token_required"
    ] is False
    assert batch_diagnostics["route_issue_merge_queue_id"][
        "authoritative_merge_queue_id"
    ] == "mq-runtime-authoritative"
    assert batch_diagnostics["route_issue_merge_queue_id"][
        "newly_minted_route_token_merge_queue_id_allowed_for_batch_merge"
    ] is False
    close_precheck_diagnostics = action_plan["close_precheck_gap_projection"][
        "public_safe_diagnostics"
    ]
    assert close_precheck_diagnostics["close_state_policy"][
        "rows_must_remain_open"
    ] == [
        "AC-MF-BATCH-QA-MERGE-GUIDE-DOGFOOD-20260705",
        "AC-REMINDERS",
        "AC-MF-BATCH-DOGFOOD-CLOSE-BLOCKERS-FRICTION-20260706",
    ]
    assert (
        close_precheck_diagnostics["close_state_policy"]["current_backlog_id"]
        == "AC-REMINDERS"
    )
    assert close_precheck_diagnostics["coordinator_close_precheck"][
        "source"
    ] == "runtime_context.close_precheck_gap_projection"
    assert reminders["merge_queue_authority"] == {
        "runtime_context_merge_queue_id_authoritative": True,
        "authoritative_merge_queue_id": "mq-runtime-authoritative",
        "route_issue_queue_id_policy": (
            "If route issue returns another queue id, keep the runtime "
            "context merge_queue_id for this lane."
        ),
        "merge_materialization_prompt_merge_queue_id_source": (
            "runtime_context.current_values.merge_queue_id"
        ),
        "merge_apply_prompt_merge_queue_id_source": (
            "runtime_context.current_values.merge_queue_id"
        ),
        "newly_minted_route_token_merge_queue_id_allowed": False,
        "message": (
            "Durable merge materialization/apply prompts must use the "
            "runtime-context merge_queue_id, not a merge_queue_id returned "
            "by freshly issued route-token payloads."
        ),
    }
    assert reminders["worker_rules"]["no_historical_evidence_backfill"][
        "allowed"
    ] is False
    commit_rule = reminders["worker_rules"]["contract_canonical_worker_commit"]
    assert commit_rule["sequence"] == [
        "implementation_evidence",
        "git_commit",
        "worker_commit_evidence",
        "finish_time_worker_attestation",
        "finish_gate",
    ]
    assert commit_rule["source_of_authority"] == "ContractRuntime.worker_commit"
    assert commit_rule["finish_consumes_contract_recorded_commit"] is True
    assert commit_rule["later_head_drift_rejected"] is True
    graph_first = reminders["worker_rules"]["graph_trace_before_implementation"]
    assert graph_first["required"] is True
    assert graph_first["blocker"] == "pre_implementation_graph_trace_missing"
    assert graph_first["sequence"] == [
        "runtime_context_read_receipt",
        "mf_subagent_startup",
        "worker_graph_query",
        "implementation_and_tests",
    ]
    worktree_guard = reminders["worker_rules"]["pre_edit_worktree_guard"]
    assert worktree_guard["required"] is True
    assert worktree_guard["required_startup_fields"] == [
        "actual_cwd",
        "actual_git_root",
        "worktree_path",
        "worker_session_id",
        "worker_transcript_ref or worker_transcript_path",
        "harness_type",
        "filer_principal",
    ]
    assert worktree_guard["must_match"] == {
        "actual_cwd": "runtime_context.current_values.worktree_path",
        "actual_git_root": "runtime_context.current_values.worktree_path",
    }
    assert "actual_cwd_not_assigned_worktree" in worktree_guard["blockers"]
    assert "actual_git_root_not_assigned_worktree" in worktree_guard["blockers"]
    assert "target/main worktree" in worktree_guard["message"]
    recovery = reminders["worker_rules"]["finish_time_attestation_recovery"]
    assert recovery["required_before"] == "finish_gate"
    assert recovery["sequence"] == [
        "implementation_evidence",
        "worker_commit_evidence",
        "finish_time_worker_attestation",
        "refresh_runtime_context_current",
        "finish_gate",
    ]
    graph_gap = reminders["graph_trace_recovery_gap"]
    assert graph_gap[
        "historical_implementation_without_verified_graph_trace_closeable"
    ] is False
    assert graph_gap["worker_next_action"] == (
        "keep_open_and_redispatch_graph_first_worker"
    )
    assert "post_hoc_graph_trace_evidence" in graph_gap["forbidden_actions"]
    durable_gate = reminders["worker_rules"][
        "independent_qa_before_durable_merge_queue"
    ]
    assert durable_gate["requires_before"] == [
        "parallel_branch_merge_queue_materialize",
        "parallel_branch_merge_queue_apply",
    ]
    assert durable_gate["evidence_order"] == ["finish_gate", "independent_qa"]
    assert reminders["merge_commit"]["required_trailers"] == ["Chain-Source-Stage"]
    assert reminders["post_merge"]["sequence"] == [
        "governance_redeploy",
        "graph_current_full_reconcile",
    ]
    assert reminders["missed_close_evidence_ordering"]["action"] == (
        "leave_row_open_for_later_audit_contract"
    )
    assert reminders["protected_successor_entry"]["required_fields"] == [
        "observer_session_id",
        "route_token_ref",
    ]
    assert reminders["dispatch_recovery"]["required_fields"] == [
        "route_context_hash",
        "prompt_contract_id",
        "observer_command_id",
    ]
    route_scope = reminders["merge_route_scope_guidance"]
    assert route_scope["worker_task_id_role"] == (
        "primary merge close_or_merge_after_evidence route scope"
    )
    assert route_scope["successor_contract_execution_id_role"] == (
        "current child mf_parallel contract runtime writes only"
    )
    assert route_scope["root_contract_execution_id_role"] == (
        "fallback root/onboard route authorization when runtime context "
        "root_task_id resolves to this id"
    )
    assert route_scope["primary_close_or_merge_route_scope"] == (
        "worker_task_id"
    )
    assert route_scope["accepted_route_scope_order"] == [
        "worker_task_id",
        "runtime_context_root_task_id_fallback",
    ]
    assert route_scope["close_or_merge_after_evidence_route_issue_shape"] == {
        "task_id": "<worker_task_id>",
        "allowed_actions": ["close_or_merge_after_evidence"],
        "raw_route_token_required": False,
        "raw_route_token_exposed": False,
    }
    assert route_scope[
        "fallback_close_or_merge_after_evidence_route_issue_shape"
    ] == {
        "task_id": "<root_contract_execution_id>",
        "allowed_actions": ["close_or_merge_after_evidence"],
        "raw_route_token_required": False,
        "raw_route_token_exposed": False,
    }
    fence_guidance = reminders["live_merge_fence_guidance"]
    assert fence_guidance[
        "route_authorization_and_fence_freshness_are_separate"
    ] is True
    assert fence_guidance[
        "branch_context_reclaimed_or_fence_mismatch_next_action"
    ] == "re_read_runtime_context_current_and_pass_current_fence_token"

    read_action = action_plan["read_receipt_hash_action"]
    assert read_action["mf_parallel_happy_path_reminders"] == reminders
    assert read_action["ordered_worker_startup_bridge"][
        "mf_parallel_happy_path_reminders"
    ] == reminders

    worker_view = pbr.build_runtime_context_worker_view(
        current_view,
        task_id="T-reminders",
        fence_token="fence-reminders",
        action_plan_view=action_plan,
    )
    assert worker_view["mf_parallel_happy_path_reminders"] == reminders
    assert worker_view["control_plane"]["mf_parallel_happy_path_reminders"] == (
        reminders
    )
    assert "mf_parallel_happy_path_reminders" in worker_view[
        "role_filter_policy"
    ]["allowed_sections"]

    runtime_contract = mf_contract.build_mf_subagent_runtime_contract_view(
        context,
        route_identity={
            "route_id": "route-reminders",
            "route_context_hash": "sha256:route-reminders",
            "prompt_contract_id": "rprompt-reminders",
            "prompt_contract_hash": "sha256:prompt-reminders",
            "route_token_ref": "rtok-reminders",
            "visible_injection_manifest_hash": "sha256:visible-reminders",
        },
    )
    assert runtime_contract["mf_parallel_happy_path_reminders"] == reminders
    assert runtime_contract["contract"]["mf_parallel_happy_path_reminders"] == (
        reminders
    )
    assert runtime_contract["agent_task_contract"][
        "mf_parallel_happy_path_reminders"
    ] == reminders
    assert runtime_contract["worker_prompt_reminders"] == list(
        mf_contract.MF_PARALLEL_HAPPY_PATH_PROMPT_REMINDERS
    )

    worker_input = mf_contract.build_mf_subagent_input(
        context,
        prompt="Implement the reminder contract.",
        target_files=("agent/governance/parallel_branch_runtime.py",),
        route_context_hash="sha256:route-reminders",
        prompt_contract_id="rprompt-reminders",
        prompt_contract_hash="sha256:prompt-reminders",
        route_id="route-reminders",
        route_token_ref="rtok-reminders",
        visible_injection_manifest_hash="sha256:visible-reminders",
    )
    prompt_reminders = worker_input["work"]["prompt_reminders"]
    assert worker_input["mf_parallel_happy_path_reminders"] == reminders
    assert worker_input["agent_task_contract"][
        "mf_parallel_happy_path_reminders"
    ] == reminders
    assert prompt_reminders == list(mf_contract.MF_PARALLEL_HAPPY_PATH_PROMPT_REMINDERS)
    assert any("Do not backfill historical" in item for item in prompt_reminders)
    assert any("Graph-first trace evidence" in item for item in prompt_reminders)
    assert any("Finish gate evidence" in item for item in prompt_reminders)
    assert any(
        "finish-time worker self-attestation" in item for item in prompt_reminders
    )
    assert any("Independent QA" in item for item in prompt_reminders)
    assert any("Chain-Source-Stage" in item for item in prompt_reminders)
    assert any("graph_current_full_reconcile" in item for item in prompt_reminders)
    assert any("leave the row open" in item for item in prompt_reminders)
    assert any(
        "Historical implementation without verified graph trace" in item
        for item in prompt_reminders
    )
    assert any("Out-of-fence file requirements" in item for item in prompt_reminders)
    assert any("observer_session_id" in item for item in prompt_reminders)
    assert any("observer_command_id" in item for item in prompt_reminders)
    assert any("merge_queue_id is authoritative" in item for item in prompt_reminders)
    assert any("root_contract_execution_id" in item for item in prompt_reminders)
    assert any("runtime_context_current" in item for item in prompt_reminders)
    assert any(
        "writer_role_safe_copy_payload.copy_payload.runtime_guide_hash" in item
        for item in prompt_reminders
    )
    assert any("hash may change" in item for item in prompt_reminders)


def test_worker_execution_safety_blocks_pre_edit_without_verified_graph_trace() -> None:
    context = BranchTaskRuntimeContext(
        project_id=PROJECT_ID,
        task_id="T-safety",
        parent_task_id="cex-parent-safety",
        backlog_id="AC-SAFETY",
        branch_ref="refs/heads/codex/safety",
        status=pbr.STATE_WORKTREE_READY,
        runtime_context_id="mfrctx-safety",
        worker_id="worker-safety",
        worker_slot_id="worker-safety",
        fence_token="fence-safety",
        worktree_path="/tmp/safety",
        base_commit="base-safety",
        head_commit="head-safety",
        target_head_commit="target-safety",
        merge_queue_id="mq-safety",
        target_files=("agent/governance/parallel_branch_runtime.py",),
        owned_files=("agent/governance/parallel_branch_runtime.py",),
    )
    startup_gate = {
        "runtime_context_id": "mfrctx-safety",
        "worker_session_id": "worker-session-safety",
        "worker_transcript_ref": "codex:safety",
        "harness_type": "codex",
        "actual_cwd": "/tmp/safety",
        "actual_git_root": "/tmp/safety",
        "filer_principal": "worker-session-safety",
        "read_receipt_hash": "sha256:read-safety",
        "read_receipt_event_id": "8896",
    }
    route_identity = {
        "route_id": "route-safety",
        "route_context_hash": "sha256:route-safety",
        "prompt_contract_id": "rprompt-safety",
        "prompt_contract_hash": "sha256:prompt-safety",
        "route_token_ref": "rtok-safety",
        "visible_injection_manifest_hash": "sha256:visible-safety",
    }

    current_without_graph = pbr.build_runtime_context_current_view(
        context,
        route_identity=route_identity,
        timeline_refs={
            "read_receipt_event_ref": "timeline:8896",
            "startup_event_ref": "timeline:8898",
        },
        startup_gate=startup_gate,
    )
    blocked = pbr.build_runtime_context_capability_boundary_view(
        current_without_graph
    )["worker_execution_safety"]

    assert blocked["status"] == "pre_edit_blocked"
    assert blocked["relative_patch_safe"] is False
    assert blocked["graph_trace_verified"] is False
    assert "graph_query_trace.trace_ids" in blocked["pre_edit_required_evidence"]
    assert {
        blocker["code"] for blocker in blocked["pre_edit_blockers"]
    } == {"pre_implementation_graph_trace_missing"}

    current_with_graph = pbr.build_runtime_context_current_view(
        context,
        route_identity=route_identity,
        timeline_refs={
            "read_receipt_event_ref": "timeline:8896",
            "startup_event_ref": "timeline:8898",
        },
        startup_gate=startup_gate,
        graph_trace_refs=["gqt-safety"],
    )
    verified = pbr.build_runtime_context_capability_boundary_view(
        current_with_graph
    )["worker_execution_safety"]

    assert verified["status"] == "verified"
    assert verified["relative_patch_safe"] is True
    assert verified["graph_trace_verified"] is True
    assert verified["graph_trace_ids"] == ["gqt-safety"]
