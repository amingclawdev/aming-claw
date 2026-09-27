from __future__ import annotations

import json
import io
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent.governance import batch_jobs
from agent.governance import graph_query_trace
from agent.governance import graph_snapshot_store
from agent.governance import mcp_server as governance_mcp_server
from agent.governance import server as governance_server
from agent.governance import stale_artifact_cleanup
from agent.governance import task_timeline
from agent.governance.db import _ensure_schema
from agent.mcp import server as managed_mcp_server
from agent.mcp.server import AmingClawMCP


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


@pytest.mark.parametrize("run_id", [
    "x" * 1_000_000,
    ("雪\\\"\n") * 200_000,
], ids=["ascii-million", "unicode-escaped-million"])
def test_cleanup_preview_bounds_complete_frames_without_changing_plan(
    tmp_path, run_id,
):
    repo = _git_repo(tmp_path)
    conn = _conn()
    created, _strategy = _terminal_batch_with_worktree(
        conn, repo, batch_id="oversized-trace",
    )
    _insert_graph_trace(
        conn, project_id="proj", trace_id="gqt-oversized",
        task_id=created["task_id"],
    )
    conn.execute("UPDATE graph_query_traces SET run_id=? WHERE trace_id=?",
                 (run_id, "gqt-oversized"))
    conn.commit()
    full = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
        response_budget=False,
    )
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    assert preview["ok"] is True
    assert preview["plan_hash"] == full["plan_hash"]
    assert preview["candidates"] == full["candidates"]
    assert preview["summary"]["append_only_graph_trace_count"] == 1
    retained = preview["append_only_retained"]
    assert retained["graph_query_traces"] == []
    assert retained["graph_trace_ids"] == []
    assert retained["graph_query_traces_omitted_count"] == 1
    assert retained["graph_query_traces_truncated"] is True
    sizes = stale_artifact_cleanup.cleanup_response_wire_bytes(preview)
    assert all(size <= 224 * 1024 for size in sizes.values())
    assert run_id not in json.dumps(preview, ensure_ascii=False)


def test_cleanup_essential_identity_overflow_has_no_executable_plan(
    tmp_path, monkeypatch,
):
    repo = _git_repo(tmp_path)
    conn = _conn()
    oversized_id = "candidate-" + "x" * 300_000
    monkeypatch.setattr(stale_artifact_cleanup,
                        "_build_graph_snapshot_candidates", lambda *_: [{
        "candidate_id": oversized_id,
        "artifact_type": "graph_snapshot_dir",
        "action": "remove_stale_graph_snapshot",
        "path": str(repo / "snapshot"),
        "safe_to_apply": True,
        "refusal_reasons": [],
        "evidence": {"size_bytes": 1},
    }])
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="graph_snapshots",
    )
    assert preview["error"] == "cleanup_response_identity_overflow"
    assert preview["apply_plan_available"] is False
    assert preview["candidates"] == []
    assert oversized_id not in json.dumps(preview)
    assert all(size <= 224 * 1024 for size in
               stale_artifact_cleanup.cleanup_response_wire_bytes(preview).values())
    request_id = "z" * (4 * 1024 - 2)
    expected_http = json.dumps({**preview, "request_id": "req-" + "x" * 12},
                               ensure_ascii=False).encode("utf-8")
    assert len(expected_http) == stale_artifact_cleanup.cleanup_response_wire_bytes(
        preview,
    )["http"]
    managed = object.__new__(AmingClawMCP)
    monkeypatch.setattr(managed, "_dispatch_tool_call", lambda *_a: preview)
    managed_output = io.StringIO()
    monkeypatch.setattr(managed_mcp_server.sys, "stdout", managed_output)
    managed._handle(json.dumps({
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": "stale_artifact_cleanup",
                   "arguments": {"project_id": "proj", "dimension": "graph_snapshots"}},
    }))
    managed_frame = managed_output.getvalue().encode("utf-8").rstrip(b"\n")
    assert len(managed_frame) <= 224 * 1024
    assert json.loads(json.loads(managed_frame)["result"]["content"][0]["text"]) == preview
    standalone_output = io.StringIO()
    monkeypatch.setattr(governance_mcp_server.sys, "stdout", standalone_output)
    monkeypatch.setattr(governance_mcp_server, "_dispatch_tool", lambda *_a: preview)
    governance_mcp_server._handle(json.dumps({
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": "stale_artifact_cleanup",
                   "arguments": {"project_id": "proj", "dimension": "graph_snapshots"}},
    }))
    standalone_frame = standalone_output.getvalue().encode("utf-8").rstrip(b"\n")
    assert len(standalone_frame) <= 224 * 1024
    assert json.loads(json.loads(standalone_frame)["result"]["content"][0]["text"]) == preview


def test_cleanup_essential_refusal_reason_overflow_has_no_plan(
    tmp_path, monkeypatch,
):
    repo = _git_repo(tmp_path)
    conn = _conn()
    exact_reason = "required-refusal-" + ("雪\\\"\n" * 100_000)
    monkeypatch.setattr(stale_artifact_cleanup,
                        "_build_graph_snapshot_candidates", lambda *_: [{
        "candidate_id": "graph_snapshot_protected:exact-id",
        "artifact_type": "graph_snapshot_dir",
        "action": "remove_stale_graph_snapshot",
        "snapshot_id": "exact-id", "path": str(repo / "snapshot"),
        "safe_to_apply": False,
        "refusal_reasons": [exact_reason],
        "evidence": {"created_at": "2026-09-27T00:00:00Z",
                     "size_bytes": None, "size_bytes_status": "unreadable"},
    }])
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="graph_snapshots",
    )
    assert preview["ok"] is False
    assert preview["error"] == "cleanup_response_identity_overflow"
    assert preview["apply_plan_available"] is False
    assert preview["candidates"] == []
    assert exact_reason not in json.dumps(preview, ensure_ascii=False)
    assert all(size <= 224 * 1024 for size in
               stale_artifact_cleanup.cleanup_response_wire_bytes(preview).values())


def test_protected_snapshot_size_is_nofollow_bounded_and_repeatable(tmp_path):
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    measured = snapshot_root / "measured"
    (measured / "nested").mkdir(parents=True)
    with (measured / "nested" / "graph.json").open("wb") as handle:
        handle.truncate(535_104_567)
    assert stale_artifact_cleanup._snapshot_directory_size(
        str(measured), remaining_entries=[100],
    ) == (535_104_567, "measured")
    assert stale_artifact_cleanup._snapshot_directory_size(
        str(snapshot_root / "absent"), remaining_entries=[100],
    ) == (0, "verified_absent")

    outside = tmp_path / "outside"
    outside.write_bytes(b"private")
    (measured / "nested" / "linked").symlink_to(outside)
    assert stale_artifact_cleanup._snapshot_directory_size(
        str(measured), remaining_entries=[100],
    )[0] is None
    (measured / "nested" / "linked").unlink()
    assert stale_artifact_cleanup._snapshot_directory_size(
        str(measured), remaining_entries=[1],
    ) == (None, "entry_budget_exceeded")
    (snapshot_root / "linked-root").symlink_to(measured, target_is_directory=True)
    assert stale_artifact_cleanup._snapshot_directory_size(
        str(snapshot_root / "linked-root"), remaining_entries=[100],
    )[0] is None


def test_protected_snapshot_size_refuses_child_swap_to_symlink(
    tmp_path, monkeypatch,
):
    root = tmp_path / "snapshots"
    nested = root / "one" / "nested"
    nested.mkdir(parents=True)
    (nested / "graph.json").write_bytes(b"{}")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"private")
    actual_open = os.open
    swapped = False

    def swap_before_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "nested" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            nested.rename(root / "one" / "nested-old")
            nested.symlink_to(outside, target_is_directory=True)
        return actual_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(stale_artifact_cleanup.os, "open", swap_before_open)
        size, status = stale_artifact_cleanup._snapshot_directory_size(
            str(root / "one"), remaining_entries=[100],
        )
    assert swapped is True
    assert size is None
    assert status == "unreadable"


def test_protected_snapshot_size_closes_child_fd_if_fstat_fails(
    tmp_path, monkeypatch,
):
    root = tmp_path / "snapshots" / "one"
    (root / "nested").mkdir(parents=True)
    actual_open = os.open
    actual_fstat = os.fstat
    opened_child = []

    def track_open(path, flags, *args, **kwargs):
        descriptor = actual_open(path, flags, *args, **kwargs)
        if path == "nested":
            opened_child.append(descriptor)
        return descriptor

    def fail_child_fstat(descriptor):
        if descriptor in opened_child:
            raise OSError("simulated child fstat failure")
        return actual_fstat(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(stale_artifact_cleanup.os, "open", track_open)
        patch.setattr(stale_artifact_cleanup.os, "fstat", fail_child_fstat)
        assert stale_artifact_cleanup._snapshot_directory_size(
            str(root), remaining_entries=[100],
        ) == (None, "unreadable")
    assert len(opened_child) == 1
    with pytest.raises(OSError):
        actual_fstat(opened_child[0])


def test_protected_snapshot_size_refuses_parent_swap_to_symlink(
    tmp_path, monkeypatch,
):
    root = tmp_path / "snapshots"
    root.mkdir()
    (root / "one").mkdir()
    (root / "one" / "graph.json").write_bytes(b"{}")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "one").mkdir()
    (outside / "one" / "private").write_bytes(b"private")
    actual_open = os.open
    swapped = False

    def swap_parent(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "snapshots" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            root.rename(tmp_path / "snapshots-old")
            root.symlink_to(outside, target_is_directory=True)
        return actual_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(stale_artifact_cleanup.os, "open", swap_parent)
        size, status = stale_artifact_cleanup._snapshot_directory_size(
            str(root / "one"), remaining_entries=[100],
        )
    assert swapped is True
    assert size is None
    assert status == "parent_unverified"


def test_protected_unknown_bytes_never_appear_as_zero_total(
    tmp_path, monkeypatch,
):
    conn = _conn()
    repo = _git_repo(tmp_path)
    root = tmp_path / "snapshots"
    root.mkdir()
    (root / "measured").mkdir()
    (root / "measured" / "graph.json").write_bytes(b"12345")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    protected = [{
        "snapshot_id": sid, "reasons": ["active_snapshot"],
        "snapshot_kind": "full", "status": "active",
        "created_at": "2026-09-27T00:00:00Z", "dir_exists": True,
    } for sid in ("measured", "linked")]
    monkeypatch.setattr(graph_snapshot_store, "select_snapshot_retention_candidates",
                        lambda *_a, **_kw: {"candidates": [], "protected": protected,
                                       "reference_authority_complete": True})
    monkeypatch.setattr(graph_snapshot_store, "_snapshot_root",
                        lambda _project, sid: root / sid)
    monkeypatch.setattr(governance_server, "_graph_release_build_fence_state",
                        lambda *_a: {"clear": True})
    first = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="graph_snapshots",
    )
    again = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="graph_snapshots",
    )
    assert first["plan_hash"] == again["plan_hash"]
    assert first["summary"]["size_bytes"] is None
    assert first["summary"]["known_size_bytes"] == 5
    assert first["summary"]["unknown_size_count"] == 1
    assert first["summary"]["size_bytes_complete"] is False
    by_id = {item["snapshot_id"]: item for item in first["candidates"]}
    assert by_id["measured"]["evidence"]["size_bytes"] == 5
    assert by_id["linked"]["evidence"]["size_bytes"] is None
    monkeypatch.setattr(stale_artifact_cleanup, "_derived_preview",
                        lambda *_a: ([], False, 0, 0))
    all_view = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="all",
    )
    assert all_view["summary"]["size_bytes"] is None
    assert all_view["summary"]["known_size_bytes"] == 5
    assert all_view["summary"]["unknown_size_count"] == 1
    assert all_view["summary"]["dimensions"]["graph_snapshots"]["size_bytes"] is None


def test_graph_snapshot_ids_must_be_single_components_before_path_access(
    tmp_path, monkeypatch,
):
    conn = _conn()
    repo = _git_repo(tmp_path)
    root = tmp_path / "snapshots"
    root.mkdir()
    invalid = ("../outside", str(tmp_path / "absolute"), "nested/child")
    monkeypatch.setattr(graph_snapshot_store, "select_snapshot_retention_candidates",
                        lambda *_a, **_kw: {
                            "candidates": [{"snapshot_id": invalid[0],
                                            "status": "superseded", "in_db": True,
                                            "dir_exists": True}],
                            "protected": [{"snapshot_id": sid,
                                           "reasons": ["active_snapshot"],
                                           "dir_exists": True}
                                          for sid in invalid[1:]],
                            "reference_authority_complete": True,
                        })
    accessed = []

    def guarded_root(_project, sid):
        accessed.append(sid)
        return root / sid

    monkeypatch.setattr(graph_snapshot_store, "_snapshot_root", guarded_root)
    monkeypatch.setattr(governance_server, "_graph_release_build_fence_state",
                        lambda *_a: {"clear": True})
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="graph_snapshots",
    )
    assert accessed == []
    assert {item["snapshot_id"] for item in preview["candidates"]} == set(invalid)
    for item in preview["candidates"]:
        assert item["safe_to_apply"] is False
        assert item["path"] == ""
        assert item["evidence"]["size_bytes"] is None
        assert item["evidence"]["size_bytes_status"] == "snapshot_id_path_invalid"
        assert "snapshot_id_path_invalid" in item["refusal_reasons"]
    assert "protected:active_snapshot" in next(
        item["refusal_reasons"] for item in preview["candidates"]
        if item["snapshot_id"] == invalid[1]
    )
    assert preview["summary"]["size_bytes"] is None
    assert preview["summary"]["unknown_size_count"] == 3


def test_unprotected_snapshot_scan_error_is_unknown_not_zero(
    tmp_path, monkeypatch,
):
    conn = _conn()
    repo = _git_repo(tmp_path)
    snapshot = tmp_path / "snapshots" / "superseded"
    snapshot.mkdir(parents=True)
    monkeypatch.setattr(graph_snapshot_store, "select_snapshot_retention_candidates",
                        lambda *_a, **_kw: {"candidates": [{
                            "snapshot_id": "superseded", "status": "superseded",
                            "in_db": True, "dir_exists": True, "size_bytes": 0,
                        }], "protected": [], "reference_authority_complete": True})
    monkeypatch.setattr(graph_snapshot_store, "_snapshot_root",
                        lambda *_a: snapshot)
    monkeypatch.setattr(governance_server, "_graph_release_build_fence_state",
                        lambda *_a: {"clear": True})
    monkeypatch.setattr(stale_artifact_cleanup, "_snapshot_directory_size",
                        lambda *_a, **_kw: (None, "unreadable"))
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="graph_snapshots",
    )
    item = preview["candidates"][0]
    assert item["evidence"]["size_bytes"] is None
    assert item["evidence"]["size_bytes_status"] == "unreadable"
    assert preview["summary"]["size_bytes"] is None
    assert preview["summary"]["unknown_size_count"] == 1


def test_cleanup_preview_skips_selector_rglob_and_bounds_large_eligible_tree(
    tmp_path, monkeypatch,
):
    conn = _conn()
    repo = _git_repo(tmp_path)
    sid = "superseded-large"
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    monkeypatch.setattr(graph_snapshot_store, "_snapshot_root",
                        lambda _project, snapshot_id: snapshot_root / snapshot_id)
    created = graph_snapshot_store.create_graph_snapshot(
        conn, "proj", snapshot_id=sid, commit_sha="a", snapshot_kind="scope",
    )
    conn.execute("UPDATE graph_snapshots SET status='superseded' "
                 "WHERE project_id=? AND snapshot_id=?", ("proj", created["snapshot_id"]))
    monkeypatch.setattr(graph_snapshot_store, "get_snapshot_retention_config",
                        lambda *_a, **_kw: {"keep_last_n": 0})
    monkeypatch.setattr(graph_snapshot_store, "snapshot_retention_reference_state",
                        lambda *_a: {"protected": {}, "complete": True,
                                     "refusal_reasons": []})
    monkeypatch.setattr(graph_snapshot_store,
                        "_bundle_referenced_snapshot_ids", lambda: set())
    monkeypatch.setattr(governance_server, "_graph_release_build_fence_state",
                        lambda *_a: {"clear": True})
    directory = snapshot_root / sid
    for number in range(4_097):
        (directory / f"entry-{number:04d}").write_bytes(b"x")
    outside = tmp_path / "outside"
    outside.write_bytes(b"private")
    (directory / "linked").symlink_to(outside)
    actual_rglob = Path.rglob

    def forbid_selector_size_walk(path, pattern):
        if path == directory:
            raise AssertionError("unbounded selector size scan was invoked")
        return actual_rglob(path, pattern)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rglob", forbid_selector_size_walk)
        preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
            conn, "proj", repo_root_path=repo, dimension="graph_snapshots",
        )
    item = next(item for item in preview["candidates"]
                if item["snapshot_id"] == sid)
    assert item["evidence"]["size_bytes"] is None
    assert item["evidence"]["size_bytes_status"] in {
        "entry_budget_exceeded", "nonregular_or_symlink_entry",
    }
    assert preview["summary"]["size_bytes"] is None
    assert preview["summary"]["unknown_size_count"] == 1


@pytest.mark.parametrize("candidate_count", [129, 136])
def test_129_protected_graph_rows_compact_only_optional_diagnostics(
    tmp_path, monkeypatch, candidate_count,
):
    conn = _conn()
    repo = _git_repo(tmp_path)
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    safe_id = "superseded-safe"
    safe_directory = snapshot_root / safe_id
    safe_directory.mkdir()
    (safe_directory / "graph.json").write_bytes(b"{}")
    protected = []
    for number in range(candidate_count):
        snapshot_id = f"full-{number:03d}-" + "s" * 34
        directory = snapshot_root / snapshot_id
        directory.mkdir()
        (directory / "graph.json").write_bytes(b"{}")
        protected.append({
            "snapshot_id": snapshot_id,
            "reasons": [f"reference:{number:03d}:{part:02d}:" + "r" * 51
                        for part in range(8)],
            "snapshot_kind": "full", "status": "active",
            "created_at": "2026-09-27T00:00:00Z", "dir_exists": True,
        })
    monkeypatch.setattr(graph_snapshot_store, "select_snapshot_retention_candidates",
                        lambda *_a, **_kw: {"candidates": [{
                            "snapshot_id": safe_id, "snapshot_kind": "full",
                            "status": "superseded", "created_at": "2026-09-26T00:00:00Z",
                            "age_days": 1, "size_bytes": 2, "in_db": True,
                            "dir_exists": True,
                        }], "protected": protected,
                                       "reference_authority_complete": True})
    monkeypatch.setattr(graph_snapshot_store, "_snapshot_root",
                        lambda _project, sid: snapshot_root / sid)
    monkeypatch.setattr(governance_server, "_graph_release_build_fence_state",
                        lambda *_a: {"clear": True})
    raw = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="graph_snapshots",
        response_budget=False,
    )
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="graph_snapshots",
    )
    repeat = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="graph_snapshots",
    )
    assert stale_artifact_cleanup.cleanup_response_wire_bytes(raw)["managed_mcp"] > 224 * 1024
    assert preview["ok"] is True
    assert len(preview["candidates"]) == candidate_count + 1
    assert preview["candidate_diagnostics"]["graph_unsafe_rows_compacted"] == candidate_count
    assert preview["plan_hash"] == raw["plan_hash"] == repeat["plan_hash"]
    assert preview["summary"] == raw["summary"] == repeat["summary"]
    assert preview["summary"]["size_bytes"] == candidate_count * 2 + 2
    assert preview["summary"]["unknown_size_count"] == 0
    request_id = "z" * (4 * 1024 - 2)
    expected_http = json.dumps({**preview, "request_id": "req-" + "x" * 12},
                               ensure_ascii=False).encode("utf-8")
    assert len(expected_http) == stale_artifact_cleanup.cleanup_response_wire_bytes(
        preview,
    )["http"]
    managed = object.__new__(AmingClawMCP)
    monkeypatch.setattr(managed, "_dispatch_tool_call", lambda *_a: preview)
    managed_output = io.StringIO()
    monkeypatch.setattr(managed_mcp_server.sys, "stdout", managed_output)
    managed._handle(json.dumps({
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": "stale_artifact_cleanup",
                   "arguments": {"project_id": "proj", "dimension": "graph_snapshots"}},
    }))
    managed_frame = managed_output.getvalue().encode("utf-8").rstrip(b"\n")
    assert len(managed_frame) <= 224 * 1024
    assert json.loads(json.loads(managed_frame)["result"]["content"][0]["text"]) == preview
    standalone_output = io.StringIO()
    monkeypatch.setattr(governance_mcp_server.sys, "stdout", standalone_output)
    monkeypatch.setattr(governance_mcp_server, "_dispatch_tool", lambda *_a: preview)
    governance_mcp_server._handle(json.dumps({
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": "stale_artifact_cleanup",
                   "arguments": {"project_id": "proj", "dimension": "graph_snapshots"}},
    }))
    standalone_frame = standalone_output.getvalue().encode("utf-8").rstrip(b"\n")
    assert len(standalone_frame) <= 224 * 1024
    assert json.loads(json.loads(standalone_frame)["result"]["content"][0]["text"]) == preview
    for source, shown in zip(raw["candidates"], preview["candidates"]):
        for key in ("candidate_id", "snapshot_id", "artifact_type", "action",
                    "safe_to_apply", "refusal_reasons"):
            assert shown[key] == source[key]
        assert shown["evidence"]["created_at"] == source["evidence"]["created_at"]
        assert shown["evidence"]["size_bytes"] == source["evidence"]["size_bytes"]
        if source["safe_to_apply"]:
            assert shown["path"] == str(safe_directory)
            assert shown["evidence"]["path_identity"] == source["evidence"]["path_identity"]
        else:
            assert "path" not in shown
            assert "path_identity" not in shown["evidence"]
    assert all(size <= 224 * 1024 for size in
               stale_artifact_cleanup.cleanup_response_wire_bytes(preview).values())
    if candidate_count == 129:
        safe_candidate = next(item for item in preview["candidates"]
                              if item["safe_to_apply"])
        applied = stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "proj", repo_root_path=repo, dimension="graph_snapshots",
            candidate_ids=[safe_candidate["candidate_id"]],
            plan_hash=preview["plan_hash"],
            plan_revision=preview["plan_revision"],
        )
        assert applied["ok"] is True
        assert applied["applied_candidate_ids"] == [safe_candidate["candidate_id"]]
        assert not safe_directory.exists()


def test_cleanup_apply_refuses_unrepresentable_ids_before_any_removal(
    tmp_path, monkeypatch,
):
    repo = _git_repo(tmp_path)
    conn = _conn()
    candidate_id = "snapshot:" + "x" * 300_000
    snapshot = repo / "snapshot"
    snapshot.mkdir()
    monkeypatch.setattr(stale_artifact_cleanup,
                        "_build_graph_snapshot_candidates", lambda *_: [{
        "candidate_id": candidate_id,
        "artifact_type": "graph_snapshot_dir",
        "action": stale_artifact_cleanup.ACTION_REMOVE_STALE_GRAPH_SNAPSHOT,
        "path": str(snapshot), "snapshot_id": "snapshot-safe",
        "safe_to_apply": True, "refusal_reasons": [],
        "evidence": {"size_bytes": 0},
    }])
    raw = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="graph_snapshots",
        response_budget=False,
    )
    removals = []
    monkeypatch.setattr(stale_artifact_cleanup.shutil, "rmtree",
                        lambda path: removals.append(path))
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as err:
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "proj", repo_root_path=repo, dimension="graph_snapshots",
            candidate_ids=[candidate_id], plan_hash=raw["plan_hash"],
            plan_revision=raw["plan_revision"],
        )
    assert err.value.payload["error"] == "cleanup_response_identity_overflow"
    assert err.value.payload["writes_performed"] is False
    assert removals == []
    assert snapshot.is_dir()


def test_cleanup_partial_failure_hashes_oversized_diagnostic_and_reports_ids(
    tmp_path, monkeypatch,
):
    repo = _git_repo(tmp_path)
    conn = _conn()
    _terminal_batch_with_worktree(conn, repo, batch_id="partial-oversize-a")
    _terminal_batch_with_worktree(conn, repo, batch_id="partial-oversize-b")
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    candidates = sorted(
        (item for item in preview["candidates"]
         if item["action"] == stale_artifact_cleanup.ACTION_REMOVE_BATCH_WORKTREE
         and item["safe_to_apply"]),
        key=lambda item: item["candidate_id"],
    )
    assert len(candidates) == 2
    original_remove = stale_artifact_cleanup._remove_worktree
    attempts = []

    def remove_once_then_fail(**kwargs):
        attempts.append(kwargs["path"])
        if len(attempts) == 2:
            raise stale_artifact_cleanup.StaleArtifactCleanupError("Z" * 1_000_000)
        return original_remove(**kwargs)

    monkeypatch.setattr(stale_artifact_cleanup, "_remove_worktree",
                        remove_once_then_fail)
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as err:
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "proj", repo_root_path=repo, dimension="worktrees",
            candidate_ids=[item["candidate_id"] for item in candidates],
            plan_hash=preview["plan_hash"], plan_revision=preview["plan_revision"],
        )
    payload = err.value.payload
    assert payload["error"] == "stale_cleanup_item_error"
    assert payload["applied_count"] == 1
    assert payload["applied_candidate_ids"] == [candidates[0]["candidate_id"]]
    assert payload["writes_performed"] is True
    assert payload["diagnostic_sha256"].startswith("sha256:")
    assert all(size <= 224 * 1024 for size in
               stale_artifact_cleanup.cleanup_response_wire_bytes(payload).values())
    assert not Path(candidates[0]["path"]).exists()
    assert Path(candidates[1]["path"]).exists()


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
