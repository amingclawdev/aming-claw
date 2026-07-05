"""PB-010 compact read model tests for parallel branch operator state."""

from __future__ import annotations

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
    assert row["queue_state"] == "planned"


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
    assert reminders["worker_rules"]["finish_gate_before_git_commit"][
        "sequence"
    ] == [
        "implementation_evidence",
        "finish_time_worker_attestation",
        "finish_gate",
        "git_commit",
    ]
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
    assert route_scope["successor_contract_execution_id_role"] == (
        "current child mf_parallel contract runtime writes and primary merge "
        "close_or_merge_after_evidence route scope"
    )
    assert route_scope["root_contract_execution_id_role"] == (
        "fallback root/onboard route authorization only"
    )
    assert route_scope["primary_close_or_merge_route_scope"] == (
        "successor_contract_execution_id"
    )
    assert route_scope["close_or_merge_after_evidence_route_issue_shape"] == {
        "task_id": "<successor_contract_execution_id>",
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
