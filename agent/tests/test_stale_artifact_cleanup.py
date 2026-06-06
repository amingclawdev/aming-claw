from __future__ import annotations

import json
import sqlite3
import subprocess

import pytest

from agent.governance import batch_jobs
from agent.governance import graph_query_trace
from agent.governance import stale_artifact_cleanup
from agent.governance import task_timeline
from agent.governance.db import _ensure_schema


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    graph_query_trace.ensure_schema(conn)
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


def _terminal_batch_with_worktree(conn, repo, *, project_id="proj", batch_id="cleanup"):
    created = batch_jobs.create_batch_task(
        conn,
        project_id,
        "cleanup candidate",
        repo_root_path=repo,
        batch_id=batch_id,
        base_commit=batch_jobs.git_commit(repo),
    )
    strategy = batch_jobs.BranchStrategy(**created["branch_strategy"])
    batch_jobs.create_worktree(strategy, repo_root_path=repo)
    batch_jobs.record_task_batch_state(conn, created["task_id"], "abandoned")
    conn.commit()
    return created, strategy


def _insert_backlog_ref(conn, *, bug_id, worktree_path, status="CLOSED", branch="codex/batch-cleanup"):
    now = batch_jobs.utc_now()
    conn.execute(
        """
        INSERT INTO backlog_bugs
          (bug_id, title, status, worktree_path, worktree_branch, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (bug_id, "cleanup backlog", status, str(worktree_path), branch, now, now),
    )
    conn.commit()


def _insert_graph_trace(conn, *, project_id, trace_id, task_id="", parent_task_id=""):
    now = batch_jobs.utc_now()
    conn.execute(
        """
        INSERT INTO graph_query_traces
          (trace_id, project_id, snapshot_id, actor, query_source, query_purpose,
           run_id, parent_task_id, runtime_context_id, task_id, worker_role,
           fence_token, status, budget_json, usage_json, artifact_path,
           created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trace_id,
            project_id,
            "scope-test",
            "mcp",
            "mf_subagent",
            "subagent_context_build",
            "run-test",
            parent_task_id,
            "rctx-test",
            task_id,
            "mf_sub",
            "fence-test",
            "complete",
            "{}",
            "{}",
            "",
            now,
            now,
        ),
    )
    conn.commit()


def test_dry_run_projects_stale_worktree_backlog_and_retained_trace(tmp_path):
    repo = _git_repo(tmp_path)
    conn = _conn()
    created, strategy = _terminal_batch_with_worktree(conn, repo)
    _insert_backlog_ref(conn, bug_id="OPT-CLEAN", worktree_path=strategy.worktree_path)
    _insert_graph_trace(conn, project_id="proj", trace_id="gqt-clean", task_id=created["task_id"])
    task_timeline.record_event(
        conn,
        project_id="proj",
        task_id=created["task_id"],
        backlog_id="OPT-CLEAN",
        event_type="worker.done",
        event_kind="implementation",
        status="succeeded",
    )
    conn.commit()

    projection = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn,
        "proj",
        repo_root_path=repo,
    )

    assert projection["dry_run"] is True
    assert projection["summary"]["stale_worktree_count"] == 1
    assert projection["summary"]["safe_apply_count"] == 2
    assert projection["summary"]["backlog_reference_count"] == 1
    assert projection["append_only_retained"]["graph_trace_ids"] == ["gqt-clean"]
    assert projection["append_only_retained"]["task_timeline_event_count"] == 1
    by_type = {item["artifact_type"]: item for item in projection["candidates"]}
    assert by_type["batch_worktree"]["safe_to_apply"] is True
    assert by_type["backlog_worktree_reference"]["safe_to_apply"] is True


def test_apply_refuses_unowned_stale_worktree(tmp_path):
    repo = _git_repo(tmp_path)
    conn = _conn()
    orphan = repo / ".worktrees" / "orphan"
    orphan.mkdir(parents=True)

    projection = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn,
        "proj",
        repo_root_path=repo,
    )
    candidate = projection["candidates"][0]
    assert candidate["safe_to_apply"] is False

    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as excinfo:
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn,
            "proj",
            repo_root_path=repo,
            candidate_ids=[candidate["candidate_id"]],
            actor="test",
            reason="should refuse",
        )

    assert excinfo.value.payload["error"] == "unsafe_stale_artifact_cleanup_refused"
    assert orphan.exists()


def test_apply_terminal_candidates_removes_worktree_updates_metadata_and_retains_trace(tmp_path):
    repo = _git_repo(tmp_path)
    conn = _conn()
    created, strategy = _terminal_batch_with_worktree(conn, repo, batch_id="apply")
    _insert_backlog_ref(conn, bug_id="OPT-APPLY", worktree_path=strategy.worktree_path)
    _insert_graph_trace(conn, project_id="proj", trace_id="gqt-retained", task_id=created["task_id"])

    projection = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn,
        "proj",
        repo_root_path=repo,
    )
    candidate_ids = [item["candidate_id"] for item in projection["candidates"] if item["safe_to_apply"]]

    result = stale_artifact_cleanup.apply_stale_artifact_cleanup(
        conn,
        "proj",
        repo_root_path=repo,
        candidate_ids=candidate_ids,
        actor="test",
        backlog_id="OPT-APPLY",
        task_id=created["task_id"],
        reason="terminal cleanup",
    )

    assert result["ok"] is True
    assert result["applied_count"] == 2
    assert not (repo / ".worktrees" / "batch-apply").exists()
    meta = json.loads(
        conn.execute(
            "SELECT metadata_json FROM tasks WHERE task_id=?",
            (created["task_id"],),
        ).fetchone()["metadata_json"]
    )
    assert meta["stale_artifact_cleanup"]["cleanup_id"] == result["cleanup_id"]
    backlog = conn.execute(
        "SELECT worktree_path, worktree_branch, takeover_json FROM backlog_bugs WHERE bug_id='OPT-APPLY'"
    ).fetchone()
    assert backlog["worktree_path"] == ""
    assert backlog["worktree_branch"] == ""
    takeover = json.loads(backlog["takeover_json"])
    assert takeover["stale_artifact_cleanup"]["cleanup_id"] == result["cleanup_id"]
    assert conn.execute("SELECT COUNT(*) AS count FROM graph_query_traces").fetchone()["count"] == 1
    event = conn.execute(
        "SELECT event_type, payload_json FROM task_timeline_events WHERE event_kind='stale_artifact_cleanup'"
    ).fetchone()
    assert event["event_type"] == "governance.stale_artifact_cleanup.apply"
    assert json.loads(event["payload_json"])["append_only_retained"]["graph_trace_ids"] == ["gqt-retained"]


def test_preflight_batch_worktree_warning_references_cleanup_workflow(tmp_path):
    from agent.governance.preflight import check_batch_worktrees

    repo = _git_repo(tmp_path)
    conn = _conn()
    _terminal_batch_with_worktree(conn, repo, batch_id="preflight")

    result = check_batch_worktrees(conn, "proj", project_root=repo)

    assert result["status"] == "warn"
    assert result["details"]["cleanup"]["api"]["dry_run"].endswith("/stale-artifact-cleanup")
    assert result["details"]["cleanup"]["mcp"]["apply_tool"] == "stale_artifact_cleanup_apply"
