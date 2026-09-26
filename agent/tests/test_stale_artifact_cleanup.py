from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

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
    # Branch graph initialization leaves untracked cache files; a clean fixture
    # removes them so worktree deletion is a genuinely safe positive control.
    shutil.rmtree(Path(strategy.worktree_path) / ".aming-claw", ignore_errors=True)
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


def test_batch_worktree_refuses_active_backlog_reference_even_with_terminal_task(tmp_path):
    repo = _git_repo(tmp_path)
    conn = _conn()
    _created, strategy = _terminal_batch_with_worktree(conn, repo, batch_id="active-backlog")
    _insert_backlog_ref(
        conn,
        bug_id="OPT-ACTIVE",
        worktree_path=strategy.worktree_path,
        status="OPEN",
    )

    projection = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn,
        "proj",
        repo_root_path=repo,
    )
    batch_candidate = next(
        item for item in projection["candidates"] if item["artifact_type"] == "batch_worktree"
    )

    assert batch_candidate["safe_to_apply"] is False
    assert "referenced_by_active_backlog_row" in batch_candidate["refusal_reasons"]
    assert batch_candidate["details"]["blocked_by_active_backlog_reference"] is True
    assert batch_candidate["evidence"]["terminal_task_ids"]
    assert batch_candidate["evidence"]["active_backlog_ids"] == ["OPT-ACTIVE"]
    assert batch_candidate["evidence"]["active_backlog_references"] == [
        {
            "backlog_id": "OPT-ACTIVE",
            "status": "OPEN",
            "runtime_state": "",
            "current_task_id": "",
            "root_task_id": "",
        }
    ]

    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as excinfo:
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn,
            "proj",
            repo_root_path=repo,
            candidate_ids=[batch_candidate["candidate_id"]],
            plan_hash=projection["plan_hash"],
            plan_revision=projection["plan_revision"],
            actor="test",
            reason="should refuse active backlog reference",
        )

    assert excinfo.value.payload["error"] == "unsafe_stale_artifact_cleanup_refused"
    assert excinfo.value.payload["unsafe_candidate_count"] == 1
    assert batch_candidate["evidence"]["active_backlog_ids"] == ["OPT-ACTIVE"]
    assert Path(strategy.worktree_path).exists()


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
            plan_hash=projection["plan_hash"],
            plan_revision=projection["plan_revision"],
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
        plan_hash=projection["plan_hash"],
        plan_revision=projection["plan_revision"],
        actor="observer",
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


def test_mixed_safe_and_unsafe_plan_is_physical_zero_write(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    conn = _conn()
    _terminal_batch_with_worktree(conn, repo, batch_id="mixed")
    orphan = repo / ".worktrees" / "orphan"
    orphan.mkdir(parents=True)
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    safe = next(item for item in preview["candidates"] if item["safe_to_apply"])
    unsafe = next(item for item in preview["candidates"] if not item["safe_to_apply"])
    monkeypatch.setattr(stale_artifact_cleanup, "_remove_worktree",
                        lambda **_kwargs: pytest.fail("worktree removal reached"))
    monkeypatch.setattr(stale_artifact_cleanup.shutil, "rmtree",
                        lambda *_args, **_kwargs: pytest.fail("snapshot removal reached"))
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as rejected:
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "proj", repo_root_path=repo, dimension="worktrees",
            candidate_ids=[safe["candidate_id"], unsafe["candidate_id"]],
            plan_hash=preview["plan_hash"], plan_revision=preview["plan_revision"],
        )
    assert rejected.value.payload["error"] == "unsafe_stale_artifact_cleanup_refused"
    assert orphan.exists()
    assert not conn.execute(
        "SELECT 1 FROM task_timeline_events WHERE event_kind='stale_artifact_cleanup'"
    ).fetchone()


def test_stale_plan_and_unknown_dimension_reject_without_removal(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    conn = _conn()
    _created, strategy = _terminal_batch_with_worktree(conn, repo, batch_id="cas")
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(item for item in preview["candidates"] if item["safe_to_apply"])
    _insert_backlog_ref(conn, bug_id="OPT-NEW", worktree_path=strategy.worktree_path,
                        status="OPEN")
    monkeypatch.setattr(stale_artifact_cleanup, "_remove_worktree",
                        lambda **_kwargs: pytest.fail("worktree removal reached"))
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as rejected:
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "proj", repo_root_path=repo, dimension="worktrees",
            candidate_ids=[candidate["candidate_id"]],
            plan_hash=preview["plan_hash"], plan_revision=preview["plan_revision"],
        )
    assert rejected.value.payload["error"] == "stale_cleanup_plan_refused"
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError):
        stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
            conn, "proj", repo_root_path=repo, dimension="../../outside",
        )
    assert Path(strategy.worktree_path).exists()


def test_derived_cache_dimensions_are_bounded_preview_only(tmp_path, monkeypatch):
    from agent.governance import db

    monkeypatch.setattr(db, "_governance_root", lambda: tmp_path)
    repo = _git_repo(tmp_path)
    conn = _conn()
    index = tmp_path / "proj" / "governance-index" / "run-1"
    index.mkdir(parents=True)
    (index / "summary.json").write_text(json.dumps({
        "run_id": "run-1", "commit_sha": "a" * 40,
        "active_snapshot_id": "full-test",
    }), encoding="utf-8")
    for dimension in ("governance_index", "state_reconcile"):
        preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
            conn, "proj", repo_root_path=repo, dimension=dimension,
        )
        assert preview["dimension"] == dimension
        assert preview["summary"]["safe_apply_count"] == 0
        assert preview["plan_hash"].startswith("sha256:")
        for item in preview["candidates"]:
            assert item["refusal_reasons"] == ["stage_b_archive_rebuild_required"]
            assert item["safe_to_apply"] is False
    index_item = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="governance_index",
    )["candidates"][0]
    assert index_item["evidence"]["commit_sha"] == "a" * 40
    assert index_item["evidence"]["snapshot_id"] == "full-test"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "summary.json").write_text(json.dumps({"commit_sha": "secret"}),
                                          encoding="utf-8")
    link = tmp_path / "proj" / "governance-index" / "linked"
    link.symlink_to(outside, target_is_directory=True)
    linked = next(item for item in stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="governance_index",
    )["candidates"] if item["path"] == str(link))
    assert "derived_path_symlink_refused" in linked["refusal_reasons"]
    assert linked["evidence"]["commit_sha"] == ""


def test_same_path_inode_replacement_between_plan_and_item_check_is_zero_write(
    tmp_path, monkeypatch,
):
    repo = _git_repo(tmp_path)
    conn = _conn()
    _created, strategy = _terminal_batch_with_worktree(conn, repo, batch_id="replace")
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(item for item in preview["candidates"] if item["safe_to_apply"])
    original_build = stale_artifact_cleanup.build_stale_artifact_cleanup_projection
    calls = 0

    def changed(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            path = Path(strategy.worktree_path)
            path.rename(path.with_name(path.name + "-held"))
            path.mkdir()
            return preview  # The stale in-memory projection cannot bless a new inode.
        return original_build(*args, **kwargs)

    monkeypatch.setattr(stale_artifact_cleanup, "build_stale_artifact_cleanup_projection", changed)
    monkeypatch.setattr(stale_artifact_cleanup, "_remove_worktree",
                        lambda **_kwargs: pytest.fail("replacement removal reached"))
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as rejected:
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "proj", repo_root_path=repo, dimension="worktrees",
            candidate_ids=[candidate["candidate_id"]],
            plan_hash=preview["plan_hash"], plan_revision=preview["plan_revision"],
        )
    assert rejected.value.payload["error"] == "stale_cleanup_item_drift_refused"
    assert Path(strategy.worktree_path).exists()


def test_dirty_terminal_worktree_is_previewed_but_never_removed(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    conn = _conn()
    _created, strategy = _terminal_batch_with_worktree(conn, repo, batch_id="dirty")
    (Path(strategy.worktree_path) / "new-private-work.txt").write_text("retain")
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    item = next(row for row in preview["candidates"] if row["artifact_type"] == "batch_worktree")
    assert not item["safe_to_apply"]
    assert "worktree_dirty_or_unregistered" in item["refusal_reasons"]
    monkeypatch.setattr(stale_artifact_cleanup, "_remove_worktree",
                        lambda **_kwargs: pytest.fail("dirty worktree removal reached"))
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError):
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "proj", repo_root_path=repo, dimension="worktrees",
            candidate_ids=[item["candidate_id"]],
            plan_hash=preview["plan_hash"], plan_revision=preview["plan_revision"],
        )
    assert (Path(strategy.worktree_path) / "new-private-work.txt").exists()


def test_all_dimension_counts_hidden_derived_items_without_returning_them(tmp_path, monkeypatch):
    from agent.governance import db

    monkeypatch.setattr(db, "_governance_root", lambda: tmp_path)
    repo = _git_repo(tmp_path)
    conn = _conn()
    root = tmp_path / "proj" / "governance-index"
    root.mkdir(parents=True)
    for idx in range(165):
        child = root / f"run-{idx:03d}"
        child.mkdir()
        (child / "summary.json").write_text('{"commit_sha":"abc"}')
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="all",
    )
    dimension = preview["summary"]["dimensions"]["governance_index"]
    assert dimension["count"] == 165
    assert 0 < dimension["visible_count"] <= 160
    assert dimension["truncated"] is True
    assert dimension["size_bytes"] == 165 * len('{"commit_sha":"abc"}')
    assert preview["summary"]["total_candidate_count"] >= 165
    assert len(preview["candidates"]) <= 160
    assert preview["summary"]["truncated"] is True


def test_retained_trace_preview_is_bounded_but_total_count_is_exact(tmp_path):
    repo = _git_repo(tmp_path)
    conn = _conn()
    created, _strategy = _terminal_batch_with_worktree(conn, repo, batch_id="many-traces")
    for idx in range(stale_artifact_cleanup.PREVIEW_LIMIT + 5):
        _insert_graph_trace(conn, project_id="proj", trace_id=f"gqt-many-{idx:03d}",
                            task_id=created["task_id"])
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    retained = preview["append_only_retained"]
    assert len(retained["graph_query_traces"]) == stale_artifact_cleanup.PREVIEW_LIMIT
    assert len(retained["graph_trace_ids"]) == stale_artifact_cleanup.PREVIEW_LIMIT
    assert retained["graph_query_traces_truncated"] is True
    assert preview["summary"]["append_only_graph_trace_count"] == (
        stale_artifact_cleanup.PREVIEW_LIMIT + 5
    )


def test_later_item_active_backlog_drift_stops_further_deletion(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    conn = _conn()
    _terminal_batch_with_worktree(conn, repo, batch_id="drift-a")
    _terminal_batch_with_worktree(conn, repo, batch_id="drift-b")
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    candidates = sorted((item for item in preview["candidates"] if item["safe_to_apply"]),
                        key=lambda item: item["candidate_id"])
    assert len(candidates) == 2
    original = stale_artifact_cleanup.build_stale_artifact_cleanup_projection
    calls = 0

    def changed(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            _insert_backlog_ref(conn, bug_id="OPT-LATE-LEASE",
                                worktree_path=candidates[1]["path"], status="OPEN")
        return original(*args, **kwargs)

    monkeypatch.setattr(stale_artifact_cleanup, "build_stale_artifact_cleanup_projection", changed)
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError,
                       match="stale_cleanup_item_drift_refused"):
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "proj", repo_root_path=repo, dimension="worktrees",
            candidate_ids=[item["candidate_id"] for item in candidates],
            plan_hash=preview["plan_hash"], plan_revision=preview["plan_revision"],
        )
    # A later drift cannot roll back an earlier physical removal; the caller
    # receives a refusal and must inspect the surviving item before retrying.
    assert not Path(candidates[0]["path"]).exists()
    assert Path(candidates[1]["path"]).exists()
    event = conn.execute(
        "SELECT status,payload_json FROM task_timeline_events "
        "WHERE event_type='governance.stale_artifact_cleanup.apply'"
    ).fetchone()
    assert event["status"] == "partial"
    assert json.loads(event["payload_json"])["applied_candidate_ids"] == [
        candidates[0]["candidate_id"]
    ]
