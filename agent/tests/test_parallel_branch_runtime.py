"""Executable dry-run scenarios for parallel branch runtime recovery."""

from __future__ import annotations

import sqlite3
import subprocess

import pytest

from agent.tests.fixtures.parallel_project import (
    PB001RestartFixtureProject,
    create_parallel_fixture_project,
    create_pb001_restart_fixture_project,
)
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
    STATE_RUNNING,
    STATE_ALLOCATED,
    STATE_WORKTREE_READY,
    BranchRuntimeFenceError,
    BranchRuntimeTask,
    BranchTaskRuntimeContext,
    branch_context_from_chain_stage,
    decide_restart_recovery,
    ensure_branch_runtime_schema,
    get_branch_context,
    list_branch_contexts,
    materialize_branch_worktree,
    plan_branch_runtime_context,
    queue_merge_item_for_branch_context,
    record_mf_subagent_startup,
    recover_expired_branch_contexts,
    record_branch_checkpoint,
    runtime_tasks_from_contexts,
    upsert_branch_context,
    validate_mf_subagent_graph_query_identity,
)

PROJECT_ID = "fixture-parallel-project"
BATCH_ID = "PB-001"
NOW = "2026-05-16T12:00:00Z"
EXPIRED = "2026-05-16T11:50:00Z"
PB001_TASK_IDS = ("T1", "T2", "T3", "T4", "T5")
PB001_BRANCH_NAMES = {
    "T1": "codex/PB001-T1-scope-reconcile",
    "T2": "codex/PB001-T2-branch-graph-refs",
    "T3": "codex/PB001-T3-task-runtime",
    "T4": "codex/PB001-T4-dashboard-read-model",
    "T5": "codex/PB001-T5-chain-adapter",
}


def _pb001_branch_ref(
    task_id: str,
    fixture: PB001RestartFixtureProject | None = None,
) -> str:
    if fixture is not None:
        return fixture.task_branches[task_id].branch_ref
    return f"refs/heads/{PB001_BRANCH_NAMES[task_id]}"


def _pb001_base_commit(
    task_id: str,
    fixture: PB001RestartFixtureProject | None = None,
) -> str:
    if fixture is not None:
        return fixture.task_branches[task_id].base_commit
    return "base-001"


def _pb001_head_commit(
    task_id: str,
    fixture: PB001RestartFixtureProject | None = None,
) -> str:
    if fixture is not None:
        return fixture.task_branches[task_id].head_commit
    return f"head-{task_id}"


def _pb001_target_head(fixture: PB001RestartFixtureProject | None = None) -> str:
    if fixture is not None:
        return fixture.target_head_after_t1
    return "head-T1"


def _pb001_tasks(fixture: PB001RestartFixtureProject | None = None) -> list[BranchRuntimeTask]:
    return [
        BranchRuntimeTask(
            task_id="T1",
            branch_ref=_pb001_branch_ref("T1", fixture),
            status="merged",
            merge_epoch="merge-001",
        ),
        BranchRuntimeTask(
            task_id="T2",
            branch_ref=_pb001_branch_ref("T2", fixture),
            status="merge_failed",
            depends_on=("T1",),
        ),
        BranchRuntimeTask(
            task_id="T3",
            branch_ref=_pb001_branch_ref("T3", fixture),
            status="running",
            depends_on=("T1",),
            lease_expired=True,
            checkpoint_id="checkpoint-T3",
        ),
        BranchRuntimeTask(
            task_id="T4",
            branch_ref=_pb001_branch_ref("T4", fixture),
            status="queued_for_merge",
            depends_on=("T2",),
        ),
        BranchRuntimeTask(
            task_id="T5",
            branch_ref=_pb001_branch_ref("T5", fixture),
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


def _startup_payload(worktree: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "task_id": "mf-sub-startup",
        "parent_task_id": "parent-startup",
        "worker_role": "mf_sub",
        "worker_id": "worker-startup",
        "agent_id": "agent-startup",
        "session_token": "secret-worker-session-token",
        "fence_token": "fence-startup",
        "actual_cwd": worktree,
        "actual_git_root": worktree,
        "branch": "refs/heads/codex/mf-sub-startup",
        "head_commit": "head-startup",
        "base_commit": "base-startup",
        "target_head_commit": "target-startup",
        "merge_queue_id": "mq-startup",
        "owned_files": ["agent/governance/parallel_branch_runtime.py"],
        "route_id": "route-startup",
        "route_context_hash": "sha256:route-startup",
        "prompt_contract_id": "rprompt-startup",
        "prompt_contract_hash": "sha256:prompt-startup",
        "visible_injection_manifest_hash": "sha256:visible-startup",
        "observer_command_id": "cmd-startup",
    }
    payload.update(overrides)
    return payload


def test_mf_sub_startup_records_real_worker_identity_and_token_hash(tmp_path) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup"
    worktree.mkdir(parents=True)
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="mf-sub-startup",
            root_task_id="parent-startup",
            stage_task_id="mf-sub-startup",
            backlog_id="BUG-STARTUP",
            worker_id="worker-startup",
            agent_id="agent-startup",
            branch_ref="refs/heads/codex/mf-sub-startup",
            status=STATE_WORKTREE_READY,
            fence_token="fence-startup",
            worktree_path=str(worktree),
            base_commit="base-startup",
            target_head_commit="target-startup",
            merge_queue_id="mq-startup",
        ),
        now_iso=NOW,
    )

    result = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(str(worktree)),
        now_iso=NOW,
    )

    gate = result["startup_gate"]
    saved = get_branch_context(conn, PROJECT_ID, "mf-sub-startup")
    assert result["ok"] is True
    assert saved is not None
    assert saved.status == STATE_RUNNING
    assert saved.head_commit == "head-startup"
    assert gate["actual_startup_recorded"] is True
    assert gate["session_token_hash"].startswith("sha256:")
    assert gate["session_token_persisted"] is False
    assert "secret-worker-session-token" not in str(result)
    assert result["timeline_event"]["event_kind"] == "mf_subagent_startup"
    assert result["timeline_event"]["payload"]["mf_subagent_startup_gate"] == gate

    accepted = validate_mf_subagent_graph_query_identity(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        parent_task_id="parent-startup",
        worker_role="mf_sub",
        fence_token="fence-startup",
    )
    assert accepted.task_id == "mf-sub-startup"


def test_mf_sub_startup_blocks_allocation_only_and_stale_fence(tmp_path) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-blocked"
    worktree.mkdir(parents=True)
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="mf-sub-startup",
            root_task_id="parent-startup",
            stage_task_id="mf-sub-startup",
            backlog_id="BUG-STARTUP",
            worker_id="worker-startup",
            agent_id="agent-startup",
            branch_ref="refs/heads/codex/mf-sub-startup",
            status=STATE_WORKTREE_READY,
            fence_token="fence-startup",
            worktree_path=str(worktree),
            base_commit="base-startup",
            target_head_commit="target-startup",
            merge_queue_id="mq-startup",
        ),
        now_iso=NOW,
    )

    allocation_only = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload={"task_id": "mf-sub-startup"},
        now_iso=NOW,
    )
    stale_fence = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(str(worktree), fence_token="stale-fence"),
        now_iso=NOW,
    )
    wrong_worker = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(str(worktree), worker_id="other-worker"),
        now_iso=NOW,
    )

    assert allocation_only["ok"] is False
    assert allocation_only["blocker_id"] == "no_truthful_bounded_mf_sub_startup_surface_available"
    assert allocation_only["terminal_dispatch_blocker"] is True
    assert "actual_cwd" in allocation_only["missing"]
    assert stale_fence["ok"] is False
    assert stale_fence["blocker_id"] == "fence_invalidated_or_unknown"
    assert wrong_worker["ok"] is False
    assert wrong_worker["blocker_id"] == "worker_id_mismatch"

    with pytest.raises(BranchRuntimeFenceError):
        validate_mf_subagent_graph_query_identity(
            conn,
            project_id=PROJECT_ID,
            task_id="mf-sub-startup",
            parent_task_id="parent-startup",
            worker_role="",
            fence_token="fence-startup",
        )


def _pb001_contexts(
    fixture: PB001RestartFixtureProject | None = None,
) -> list[BranchTaskRuntimeContext]:
    return [
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T1",
            batch_id=BATCH_ID,
            backlog_id="OPT-PB001-T1",
            branch_ref=_pb001_branch_ref("T1", fixture),
            status=STATE_MERGED,
            merge_queue_id="merge-001",
            base_commit=_pb001_base_commit("T1", fixture),
            head_commit=_pb001_head_commit("T1", fixture),
            target_head_commit=_pb001_target_head(fixture),
            snapshot_id="scope-base",
            projection_id="semproj-base",
            fence_token="fence-T1",
        ),
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T2",
            batch_id=BATCH_ID,
            backlog_id="OPT-PB001-T2",
            branch_ref=_pb001_branch_ref("T2", fixture),
            status=STATE_MERGE_FAILED,
            depends_on=("T1",),
            base_commit=_pb001_base_commit("T2", fixture),
            head_commit=_pb001_head_commit("T2", fixture),
            target_head_commit=_pb001_target_head(fixture),
            fence_token="fence-T2",
        ),
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T3",
            batch_id=BATCH_ID,
            backlog_id="OPT-PB001-T3",
            branch_ref=_pb001_branch_ref("T3", fixture),
            status="running",
            depends_on=("T1",),
            attempt=1,
            lease_id="lease-T3",
            lease_expires_at=EXPIRED,
            fence_token="fence-old-T3",
            checkpoint_id="checkpoint-T3",
            replay_source="checkpoint",
            base_commit=_pb001_base_commit("T3", fixture),
            head_commit=_pb001_head_commit("T3", fixture),
            target_head_commit=_pb001_target_head(fixture),
            snapshot_id="scope-T3",
            projection_id="semproj-T3",
        ),
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T4",
            batch_id=BATCH_ID,
            backlog_id="OPT-PB001-T4",
            branch_ref=_pb001_branch_ref("T4", fixture),
            status="queued_for_merge",
            depends_on=("T2",),
            merge_queue_id="merge-004",
            base_commit=_pb001_base_commit("T4", fixture),
            head_commit=_pb001_head_commit("T4", fixture),
            target_head_commit=_pb001_target_head(fixture),
            fence_token="fence-T4",
        ),
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T5",
            batch_id=BATCH_ID,
            backlog_id="OPT-PB001-T5",
            branch_ref=_pb001_branch_ref("T5", fixture),
            status="running",
            depends_on=("T3",),
            attempt=2,
            lease_id="lease-T5",
            lease_expires_at=EXPIRED,
            fence_token="fence-old-T5",
            checkpoint_id="checkpoint-T5",
            replay_source="checkpoint",
            base_commit=_pb001_base_commit("T5", fixture),
            head_commit=_pb001_head_commit("T5", fixture),
            target_head_commit=_pb001_target_head(fixture),
            snapshot_id="scope-T5",
            projection_id="semproj-T5",
        ),
    ]


def _persist_pb001_contexts(
    conn: sqlite3.Connection,
    fixture: PB001RestartFixtureProject | None = None,
) -> None:
    for context in _pb001_contexts(fixture):
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

    assert SCHEMA_VERSION >= 38
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?, ?, ?)",
        (
            "parallel_branch_runtime_contexts",
            "parallel_branch_merge_queue_items",
            "parallel_branch_batch_runtimes",
            "parallel_branch_batch_items",
        ),
    ).fetchall()
    assert {row["name"] for row in rows} == {
        "parallel_branch_runtime_contexts",
        "parallel_branch_merge_queue_items",
        "parallel_branch_batch_runtimes",
        "parallel_branch_batch_items",
    }


def test_pb001_recovery_rehydrates_replay_ready_contexts_from_generated_project(
    tmp_path,
) -> None:
    fixture = create_pb001_restart_fixture_project(tmp_path)
    for task_id in PB001_TASK_IDS:
        branch = fixture.task_branches[task_id]
        actual = subprocess.run(
            ["git", "rev-parse", "--verify", branch.branch_ref],
            cwd=fixture.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert actual == branch.head_commit

    conn = _runtime_conn()
    _persist_pb001_contexts(conn, fixture)

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
    assert t3_context.head_commit == fixture.task_branches["T3"].head_commit
    assert t5_context.head_commit == fixture.task_branches["T5"].head_commit
    t4_context = get_branch_context(conn, PROJECT_ID, "T4")
    assert t4_context is not None
    assert t4_context.base_commit == fixture.task_branches["T4"].base_commit
    assert t4_context.head_commit == fixture.task_branches["T4"].head_commit

    contexts_after_restart = list_branch_contexts(conn, PROJECT_ID, batch_id=BATCH_ID)
    runtime_tasks = runtime_tasks_from_contexts(contexts_after_restart, now_iso=NOW)
    plan = decide_restart_recovery(runtime_tasks)
    decisions = _by_task(plan)

    assert plan.retained_branch_refs == tuple(
        fixture.task_branches[task_id].branch_ref for task_id in PB001_TASK_IDS
    )
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
        head_commit="head-T3-after-reclaim",
        now_iso=NOW,
    )

    assert updated.checkpoint_id == "checkpoint-T3-after-reclaim"
    assert updated.replay_source == "checkpoint"
    assert updated.head_commit == "head-T3-after-reclaim"


def test_pb007_chain_stage_identity_round_trips_without_running_chain() -> None:
    conn = _runtime_conn()
    context = branch_context_from_chain_stage(
        project_id=PROJECT_ID,
        chain_id="chain-root-1",
        root_task_id="chain-root-1",
        stage_task_id="chain-dev-2",
        stage_type="dev",
        retry_round=2,
        batch_id="PB-007",
        backlog_id="OPT-PB007",
        branch_ref="refs/heads/codex/PB007-chain-dev",
        worktree_id="worktree-PB007",
        worktree_path="/tmp/worktrees/PB007-chain-dev",
        base_commit="base-PB007",
        head_commit="head-PB007",
        target_head_commit="target-PB007",
        snapshot_id="scope-PB007",
        projection_id="semproj-PB007",
        merge_queue_id="mergeq-PB007",
        merge_preview_id="preview-PB007",
        checkpoint_id="checkpoint-PB007",
        replay_source="checkpoint",
        fence_token="fence-PB007",
    )

    saved = upsert_branch_context(conn, context, now_iso=NOW)

    assert saved.task_id == "chain-dev-2"
    assert saved.chain_id == "chain-root-1"
    assert saved.root_task_id == "chain-root-1"
    assert saved.stage_task_id == "chain-dev-2"
    assert saved.stage_type == "dev"
    assert saved.retry_round == 2
    assert saved.attempt == 3
    assert saved.branch_ref == "refs/heads/codex/PB007-chain-dev"
    assert saved.merge_queue_id == "mergeq-PB007"

    reloaded = get_branch_context(conn, PROJECT_ID, "chain-dev-2")
    assert reloaded is not None
    assert reloaded.chain_id == "chain-root-1"
    assert reloaded.retry_round == 2
    assert reloaded.to_runtime_task(now_iso=NOW).checkpoint_id == "checkpoint-PB007"


def test_mf_branch_allocation_planner_sanitizes_worker_attempt_and_persists() -> None:
    conn = _runtime_conn()
    context = plan_branch_runtime_context(
        project_id=PROJECT_ID,
        task_id="../Task 123",
        batch_id="PB-009",
        backlog_id="ARCH-PB009",
        agent_id="observer",
        worker_id="worker 0/../../x",
        workspace_root="/repo",
        attempt=2,
        base_commit="B0",
        target_head_commit="M0",
        merge_queue_id="mergeq-PB009",
        fence_token="fence-planned",
    )

    assert context.status == STATE_ALLOCATED
    assert context.branch_ref == "refs/heads/codex/task-123-attempt-2"
    assert context.worktree_id == "wt-task-123-attempt-2"
    assert context.worktree_path == "/repo/.worktrees/worker-0-x/task-123-attempt-2"
    assert context.fence_token == "fence-planned"
    assert ".." not in context.branch_ref
    assert ".." not in context.worktree_path

    saved = upsert_branch_context(conn, context, now_iso=NOW)
    reloaded = get_branch_context(conn, PROJECT_ID, "../Task 123")

    assert saved == reloaded
    assert reloaded is not None
    assert reloaded.worker_id == "worker 0/../../x"
    assert reloaded.merge_queue_id == "mergeq-PB009"


def test_mf_branch_worktree_materialization_uses_planned_identity(tmp_path) -> None:
    fixture = create_parallel_fixture_project(tmp_path)
    repo = fixture.root
    base = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    conn = _runtime_conn()
    planned = plan_branch_runtime_context(
        project_id=PROJECT_ID,
        task_id="MF Branch API",
        batch_id="PB-001",
        backlog_id="ARCH-PB-WORKTREE",
        worker_id="worker one",
        workspace_root=str(repo),
        base_commit=base,
        target_head_commit=base,
        merge_queue_id="mergeq-worktree",
    )
    upsert_branch_context(conn, planned, now_iso=NOW)

    result = materialize_branch_worktree(
        conn,
        project_id=PROJECT_ID,
        task_id="MF Branch API",
        repo_root_path=repo,
        now_iso=NOW,
    )

    context = get_branch_context(conn, PROJECT_ID, "MF Branch API")
    assert context is not None
    assert context.status == STATE_WORKTREE_READY
    assert context.branch_ref == "refs/heads/codex/mf-branch-api"
    assert context.worktree_path == str(repo / ".worktrees" / "worker-one" / "mf-branch-api")
    assert context.head_commit == base
    assert result["worktree"]["created"] is True
    assert result["branch_strategy"]["work_branch"] == "codex/mf-branch-api"
    assert result["branch_strategy"]["merge_policy"] == "merge_queue"
    assert (repo / ".worktrees" / "worker-one" / "mf-branch-api" / ".git").exists()
    assert result["worktree"]["branch_graph"]["status"] == "ready"


def test_merge_queue_enqueue_uses_current_fence_and_updates_context() -> None:
    conn = _runtime_conn()
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T-merge",
            batch_id="PB-002",
            branch_ref="refs/heads/codex/t-merge",
            status=STATE_WORKTREE_READY,
            fence_token="fence-current",
            base_commit="base-merge",
            head_commit="head-merge",
            target_head_commit="target-merge",
            snapshot_id="scope-merge",
            projection_id="semproj-merge",
        ),
        now_iso=NOW,
    )

    with pytest.raises(BranchRuntimeFenceError):
        queue_merge_item_for_branch_context(
            conn,
            project_id=PROJECT_ID,
            task_id="T-merge",
            merge_queue_id="mergeq-PB002",
            fence_token="fence-stale",
            now_iso=NOW,
        )

    queued = queue_merge_item_for_branch_context(
        conn,
        project_id=PROJECT_ID,
        task_id="T-merge",
        merge_queue_id="mergeq-PB002",
        queue_index=2,
        fence_token="fence-current",
        hard_depends_on=("T-foundation",),
        merge_preview_id="preview-merge",
        now_iso=NOW,
    )

    context = get_branch_context(conn, PROJECT_ID, "T-merge")
    assert context is not None
    assert context.status == "queued_for_merge"
    assert context.merge_queue_id == "mergeq-PB002"
    assert context.merge_preview_id == "preview-merge"
    assert queued["queue_item"]["branch_ref"] == "refs/heads/codex/t-merge"
    assert queued["queue_item"]["hard_depends_on"] == ["T-foundation"]
    assert queued["queue_item"]["snapshot_id"] == "scope-merge"


def test_pb012_branch_contexts_are_isolated_by_project_and_batch() -> None:
    conn = _runtime_conn()
    shared_task_id = "shared-task"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id="project-a",
            task_id=shared_task_id,
            batch_id="batch-a",
            branch_ref="refs/heads/codex/project-a-shared-task",
            status=STATE_RUNNING,
            fence_token="fence-a",
        ),
        now_iso=NOW,
    )
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id="project-b",
            task_id=shared_task_id,
            batch_id="batch-b",
            branch_ref="refs/heads/codex/project-b-shared-task",
            status=STATE_MERGED,
            fence_token="fence-b",
        ),
        now_iso=NOW,
    )

    project_a = get_branch_context(conn, "project-a", shared_task_id)
    project_b = get_branch_context(conn, "project-b", shared_task_id)

    assert project_a is not None
    assert project_b is not None
    assert project_a.branch_ref == "refs/heads/codex/project-a-shared-task"
    assert project_b.branch_ref == "refs/heads/codex/project-b-shared-task"
    assert list_branch_contexts(conn, "project-a", batch_id="batch-a") == [project_a]
    assert list_branch_contexts(conn, "project-a", batch_id="batch-b") == []
