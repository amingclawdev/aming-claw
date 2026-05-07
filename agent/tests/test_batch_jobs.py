from __future__ import annotations

import json
import sqlite3
import subprocess

import pytest

from agent.governance.db import _ensure_schema
from agent.governance import batch_jobs


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)
    return repo


def _metadata(conn, task_id: str) -> dict:
    raw = conn.execute(
        "SELECT metadata_json FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()["metadata_json"]
    return json.loads(raw)


def test_job_type_defaults_to_feature_work_for_normal_chain():
    from agent.governance.task_registry import create_task

    conn = _conn()
    task = create_task(conn, "proj", "plan feature", task_type="pm")

    meta = _metadata(conn, task["task_id"])
    assert meta["job_type"] == "feature_work"
    assert meta["stage_type"] == "pm"
    row = conn.execute("SELECT type FROM tasks WHERE task_id=?", (task["task_id"],)).fetchone()
    assert row["type"] == "pm"


def test_stage_type_routing_ignores_job_type():
    from agent.governance.task_registry import claim_task, create_task

    conn = _conn()
    create_task(
        conn,
        "proj",
        "batch-owned pm stage",
        task_type="pm",
        metadata={"job_type": "batch_migration"},
    )

    claimed, _fence = claim_task(conn, "proj", "worker-1")
    assert claimed["type"] == "pm"
    assert claimed["metadata"]["job_type"] == "batch_migration"
    assert claimed["metadata"]["stage_type"] == "pm"


def test_batch_migration_strategy_uses_codex_batch_branch(tmp_path):
    repo = _git_repo(tmp_path)
    base = batch_jobs.git_commit(repo)

    strategy = batch_jobs.resolve_branch_strategy(
        job_type="batch_migration",
        repo_root_path=repo,
        project_id="proj",
        base_commit=base,
        batch_id="batch-001",
    )

    assert strategy.base_commit == base
    assert strategy.work_branch == "codex/batch-batch-001"
    assert strategy.worktree_relpath == ".worktrees/batch-batch-001"
    assert strategy.direct is False


def test_manual_fix_strategy_is_direct_main(tmp_path):
    repo = _git_repo(tmp_path)
    strategy = batch_jobs.resolve_branch_strategy(
        job_type="manual_fix",
        repo_root_path=repo,
        target_branch="main",
        base_commit=batch_jobs.git_commit(repo),
    )

    assert strategy.direct is True
    assert strategy.work_branch == "main"
    assert strategy.worktree_path == ""


def test_batch_metadata_round_trips_through_task_metadata(tmp_path):
    repo = _git_repo(tmp_path)
    conn = _conn()

    created = batch_jobs.create_batch_task(
        conn,
        "proj",
        "implement migration",
        repo_root_path=repo,
        batch_id="batch-002",
        base_commit=batch_jobs.git_commit(repo),
        metadata={"bug_id": "OPT-BATCH"},
    )

    meta = _metadata(conn, created["task_id"])
    row = conn.execute(
        "SELECT status, execution_status FROM tasks WHERE task_id=?",
        (created["task_id"],),
    ).fetchone()
    assert row["status"] == "queued"
    assert row["execution_status"] == "queued"
    assert meta["job_type"] == "batch_migration"
    assert meta["batch_id"] == "batch-002"
    assert meta["batch_status"] == "created"
    assert meta["work_branch"] == "codex/batch-batch-002"
    assert meta["batch_state_history"][0]["status"] == "created"


def test_batch_status_is_metadata_not_task_status(tmp_path):
    repo = _git_repo(tmp_path)
    conn = _conn()
    created = batch_jobs.create_batch_task(
        conn,
        "proj",
        "implement migration",
        repo_root_path=repo,
        batch_id="batch-003",
        base_commit=batch_jobs.git_commit(repo),
    )

    batch_jobs.record_task_batch_state(
        conn,
        created["task_id"],
        "worktree_ready",
        evidence={"worktree_path": created["branch_strategy"]["worktree_path"]},
    )
    row = conn.execute(
        "SELECT status, execution_status, metadata_json FROM tasks WHERE task_id=?",
        (created["task_id"],),
    ).fetchone()
    meta = json.loads(row["metadata_json"])
    assert row["status"] == "queued"
    assert row["execution_status"] == "queued"
    assert meta["batch_status"] == "worktree_ready"


def test_one_active_batch_migration_per_project(tmp_path):
    repo = _git_repo(tmp_path)
    conn = _conn()
    base = batch_jobs.git_commit(repo)
    batch_jobs.create_batch_task(
        conn,
        "proj",
        "first",
        repo_root_path=repo,
        batch_id="batch-004",
        base_commit=base,
    )

    with pytest.raises(batch_jobs.ActiveBatchExistsError):
        batch_jobs.create_batch_task(
            conn,
            "proj",
            "second",
            repo_root_path=repo,
            batch_id="batch-005",
            base_commit=base,
        )

    created = batch_jobs.create_batch_task(
        conn,
        "proj",
        "second override",
        repo_root_path=repo,
        batch_id="batch-006",
        base_commit=base,
        observer_override=True,
    )
    assert created["metadata"]["observer_override"] is True


def test_worktree_create_abandon_and_stale_report(tmp_path):
    repo = _git_repo(tmp_path)
    conn = _conn()
    created = batch_jobs.create_batch_task(
        conn,
        "proj",
        "worktree smoke",
        repo_root_path=repo,
        batch_id="batch-007",
        base_commit=batch_jobs.git_commit(repo),
    )
    strategy = batch_jobs.BranchStrategy(**created["branch_strategy"])

    out = batch_jobs.create_worktree(strategy, repo_root_path=repo)
    assert out["created"] is True
    assert (repo / ".worktrees" / "batch-batch-007").exists()

    stale = batch_jobs.report_stale_worktrees(conn, "proj", repo_root_path=repo)
    assert stale["stale_count"] == 0

    batch_jobs.record_task_batch_state(conn, created["task_id"], "abandoned")
    stale_after = batch_jobs.report_stale_worktrees(conn, "proj", repo_root_path=repo)
    assert stale_after["stale_count"] == 1

    removed = batch_jobs.abandon_worktree(strategy, repo_root_path=repo, remove_branch=True)
    assert removed["removed"] is True
    assert not (repo / ".worktrees" / "batch-batch-007").exists()


def test_batch_merge_dry_run_records_ready_for_review(tmp_path):
    repo = _git_repo(tmp_path)
    conn = _conn()
    created = batch_jobs.create_batch_task(
        conn,
        "proj",
        "merge dry run",
        repo_root_path=repo,
        batch_id="batch-008",
        base_commit=batch_jobs.git_commit(repo),
    )

    result = batch_jobs.merge_batch_branch(
        conn,
        created["task_id"],
        repo_root_path=repo,
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["merge_plan"]["work_branch"] == "codex/batch-batch-008"
    meta = _metadata(conn, created["task_id"])
    assert meta["batch_status"] == "ready_for_review"
    assert "merge_commit" not in meta


def test_batch_merge_uses_chain_trailer_helper(tmp_path):
    repo = _git_repo(tmp_path)
    conn = _conn()
    created = batch_jobs.create_batch_task(
        conn,
        "proj",
        "merge real",
        repo_root_path=repo,
        batch_id="batch-009",
        base_commit=batch_jobs.git_commit(repo),
        metadata={"bug_id": "OPT-BATCH-009"},
    )
    strategy = batch_jobs.BranchStrategy(**created["branch_strategy"])
    batch_jobs.create_worktree(strategy, repo_root_path=repo)

    wt = repo / ".worktrees" / "batch-batch-009"
    (wt / "feature.txt").write_text("batch change\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-m", "batch change"], cwd=wt, check=True, capture_output=True, text=True)

    merged = batch_jobs.merge_batch_branch(
        conn,
        created["task_id"],
        repo_root_path=repo,
        message="batch merge",
        dry_run=False,
    )

    assert merged["dry_run"] is False
    assert merged["merge_commit"]
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "batch change\n"
    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"Chain-Source-Task: {created['task_id']}" in log
    assert "Chain-Source-Stage: merge" in log
    assert "Chain-Bug-Id: OPT-BATCH-009" in log
    meta = _metadata(conn, created["task_id"])
    assert meta["batch_status"] == "merged"
    assert meta["merge_commit"] == merged["merge_commit"]


def test_unsafe_worktree_path_rejected(tmp_path):
    repo = _git_repo(tmp_path)
    with pytest.raises(batch_jobs.BatchJobError):
        batch_jobs.ensure_worktree_path_safe(repo, tmp_path / "outside")
