from __future__ import annotations

import json
import io
import os
import sys
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent.governance import batch_jobs
from agent.governance import db as governance_db
from agent.governance import graph_query_trace
from agent.governance import graph_snapshot_store
from agent.governance import mcp_server as governance_mcp_server
from agent.governance import server as governance_server
from agent.governance import stale_artifact_cleanup
from agent.governance import task_timeline
from agent.governance.db import _ensure_schema
from agent.governance.contracts.runtime import SQLiteContractExecutionStore
from agent.mcp import server as managed_mcp_server
from agent.mcp.server import AmingClawMCP


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    graph_query_trace.ensure_schema(conn)
    conn.executescript(SQLiteContractExecutionStore.SCHEMA_SQL)
    governance_server._ensure_release_operator_head_queue_schema(conn)
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


def _merged_batch_with_worktree(conn, repo, *, batch_id="archive", project_id="proj"):
    created = batch_jobs.create_batch_task(
        conn, project_id, "merged archive candidate", repo_root_path=repo,
        batch_id=batch_id, base_commit=batch_jobs.git_commit(repo),
    )
    strategy = batch_jobs.BranchStrategy(**created["branch_strategy"])
    batch_jobs.create_worktree(strategy, repo_root_path=repo)
    worktree = Path(strategy.worktree_path)
    shutil.rmtree(worktree / ".aming-claw", ignore_errors=True)
    (worktree / "archive.txt").write_text("portable evidence\n", encoding="utf-8")
    subprocess.run(["git", "add", "archive.txt"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", "archive fixture"], cwd=worktree,
                   check=True, capture_output=True)
    subprocess.run(["git", "merge", "--no-ff", "-m", "merge fixture",
                    strategy.work_branch], cwd=repo, check=True, capture_output=True)
    batch_jobs.record_task_batch_state(conn, created["task_id"], "merged")
    conn.commit()
    return created, strategy


def test_merged_worktree_archive_publish_prune_restore_and_replay(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    conn = _conn()
    created, strategy = _merged_batch_with_worktree(conn, repo)
    archive_root = tmp_path / "archive-volume"
    archive_root.mkdir()
    monkeypatch.setenv(stale_artifact_cleanup.ARCHIVE_ROOT_ENV, str(archive_root))
    real_ismount = os.path.ismount
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == archive_root or real_ismount(path))
    preview = stale_artifact_cleanup.build_merged_worktree_archive_projection(
        conn, "proj", repo_root_path=repo,
    )
    candidate = preview["candidates"][0]
    assert candidate["safe_to_apply"] is True, candidate["refusal_reasons"]
    bare = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    assert next(i for i in bare["candidates"] if i["artifact_type"] == "batch_worktree")["safe_to_apply"] is True
    published = stale_artifact_cleanup.archive_merged_worktree(
        conn, "proj", repo_root_path=repo,
        candidate_id=candidate["candidate_id"], plan_hash=preview["plan_hash"],
        plan_revision=preview["plan_revision"],
    )
    assert published["state"] == "archive_only"
    assert Path(strategy.worktree_path).exists()
    pruned = stale_artifact_cleanup.prune_archived_merged_worktree(
        conn, "proj", repo_root_path=repo,
        candidate_id=candidate["candidate_id"], plan_hash=preview["plan_hash"],
        plan_revision=preview["plan_revision"], generation=published["generation"],
    )
    assert pruned["state"] == "pruned"
    assert not Path(strategy.worktree_path).exists()
    replay = stale_artifact_cleanup.prune_archived_merged_worktree(
        conn, "proj", repo_root_path=repo,
        candidate_id=candidate["candidate_id"], plan_hash=preview["plan_hash"],
        plan_revision=preview["plan_revision"], generation=published["generation"],
    )
    assert replay["replay"] is True
    assert replay["writes_performed"] is False


def _archive_fixture(tmp_path, monkeypatch, *, batch_id="negative"):
    repo = _git_repo(tmp_path)
    conn = _conn()
    created, strategy = _merged_batch_with_worktree(conn, repo, batch_id=batch_id)
    archive_root = tmp_path / "archive-volume"
    archive_root.mkdir()
    monkeypatch.setenv(stale_artifact_cleanup.ARCHIVE_ROOT_ENV, str(archive_root))
    real_ismount = os.path.ismount
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == archive_root or real_ismount(path))
    return repo, conn, created, strategy, archive_root


def _ac_cross_world_archive_fixture(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    conn = _conn()
    # The AC DEV source intentionally has no release queue. Its authority is
    # the separate verified stable database, represented by this read-only fixture.
    for table in sorted(stale_artifact_cleanup._STABLE_QUEUE_REFERENCE_TABLES):
        conn.execute(f'DROP TABLE "{table}"')
    created, strategy = _merged_batch_with_worktree(
        conn, repo, batch_id="ac-cross-world", project_id="aming-claw",
    )
    stable_directory = tmp_path / "stable-world"
    stable_directory.mkdir()
    stable_path = stable_directory / "stable-authority.db"
    with sqlite3.connect(stable_path) as stable:
        governance_server._ensure_release_operator_head_queue_schema(stable)
        stable.commit()
    identity = stable_path.stat()
    binding = {
        "database_path": str(stable_path),
        "stable_database_identity": {"device": identity.st_dev, "inode": identity.st_ino},
        "stable_head": batch_jobs.git_commit(repo),
        "health": {"ac_release_queue_writer_fence":
                   stale_artifact_cleanup._STABLE_QUEUE_FENCE_PROTOCOL},
    }
    monkeypatch.setattr(governance_db, "verified_stable_database_binding", lambda: binding)

    def revalidate(current):
        now = Path(current["database_path"]).stat(follow_symlinks=False)
        if (now.st_dev, now.st_ino) != (
            current["stable_database_identity"]["device"],
            current["stable_database_identity"]["inode"],
        ):
            raise RuntimeError("verified stable database identity changed")

    monkeypatch.setattr(governance_db, "_revalidate_stable_database_binding", revalidate)
    archive_root = tmp_path / "archive-volume"
    archive_root.mkdir()
    monkeypatch.setenv(stale_artifact_cleanup.ARCHIVE_ROOT_ENV, str(archive_root))
    real_ismount = os.path.ismount
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == archive_root or real_ismount(path))
    return repo, conn, created, strategy, archive_root, stable_path


def test_ac_dev_archive_reads_verified_stable_queue_and_prunes(tmp_path, monkeypatch):
    repo, conn, _created, strategy, archive_root, stable_path = _ac_cross_world_archive_fixture(
        tmp_path, monkeypatch,
    )
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "aming-claw", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(i for i in preview["candidates"] if i["artifact_type"] == "batch_worktree")
    assert candidate["safe_to_apply"] is True, candidate["refusal_reasons"]
    assert candidate["evidence"]["stable_queue"]["row_count"] == 0
    assert candidate["evidence"]["stable_queue"]["stable_database_identity"]["inode"] == stable_path.stat().st_ino
    with stale_artifact_cleanup._stable_queue_exclusive_fence():
        locked_facts, locked_reasons = stale_artifact_cleanup._stable_release_queue_reference_facts(
            tokens=(str(strategy.worktree_path), candidate["evidence"]["branch"],
                    candidate["evidence"]["head"], candidate["evidence"]["task_id"], ""),
        )
    assert locked_reasons == []
    assert locked_facts == candidate["evidence"]["stable_queue"]
    applied = stale_artifact_cleanup.apply_stale_artifact_cleanup(
        conn, "aming-claw", repo_root_path=repo, dimension="worktrees",
        candidate_ids=[candidate["candidate_id"]], plan_hash=preview["plan_hash"],
        plan_revision=preview["plan_revision"],
    )
    assert applied["state"] == "pruned"
    assert not Path(strategy.worktree_path).exists()
    assert list(archive_root.rglob("index/*.json"))


def test_ac_dev_http_cleanup_archives_and_prunes_with_separate_stable_queue(
    tmp_path, monkeypatch,
):
    repo, conn, _created, strategy, archive_root, _stable_path = (
        _ac_cross_world_archive_fixture(tmp_path, monkeypatch)
    )

    class NoClose:
        def __getattr__(self, name):
            return getattr(conn, name)

        def close(self):
            pass

    monkeypatch.setattr(governance_server, "get_connection", lambda _project: NoClose())
    monkeypatch.setattr(governance_server, "_graph_governance_project_root",
                        lambda *_args, **_kwargs: repo)
    monkeypatch.setattr(governance_server, "_require_graph_governance_operator",
                        lambda *_args, **_kwargs: {"role": "observer"})
    preview_context = governance_server.RequestContext(
        None, "GET", {"project_id": "aming-claw"}, {"dimension": "worktrees"},
        {}, "req-ac-preview", "", "",
    )
    preview = governance_server.handle_graph_governance_stale_artifact_cleanup(preview_context)
    candidate = next(i for i in preview["candidates"] if i["artifact_type"] == "batch_worktree")
    assert candidate["safe_to_apply"] is True
    apply_context = governance_server.RequestContext(
        None, "POST", {"project_id": "aming-claw"}, {},
        {"dimension": "worktrees", "candidate_ids": [candidate["candidate_id"]],
         "plan_hash": preview["plan_hash"], "plan_revision": preview["plan_revision"]},
        "req-ac-apply", "", "",
    )
    applied = governance_server.handle_graph_governance_stale_artifact_cleanup_apply(
        apply_context,
    )
    assert applied["state"] == "pruned", applied
    assert not Path(strategy.worktree_path).exists()
    assert list(archive_root.rglob("index/*.json"))
    assert all(size <= 224 * 1024 for size in
               stale_artifact_cleanup.cleanup_response_wire_bytes(applied).values())


def test_ac_dev_archive_refuses_stable_queue_change_and_binding_loss(tmp_path, monkeypatch):
    repo, conn, created, strategy, archive_root, stable_path = _ac_cross_world_archive_fixture(
        tmp_path, monkeypatch,
    )
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "aming-claw", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(i for i in preview["candidates"] if i["artifact_type"] == "batch_worktree")
    assert candidate["safe_to_apply"] is True
    with sqlite3.connect(stable_path) as stable:
        stable.execute(
            "INSERT INTO release_operator_head_queue "
            "(project_id,backlog_id,position,inserted_at,updated_at) VALUES (?,?,?,?,?)",
            ("aming-claw", created["task_id"], 1, batch_jobs.utc_now(), batch_jobs.utc_now()),
        )
        stable.commit()
    changed = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "aming-claw", repo_root_path=repo, dimension="worktrees",
    )
    changed_candidate = next(i for i in changed["candidates"] if i["artifact_type"] == "batch_worktree")
    assert changed_candidate["safe_to_apply"] is False
    assert "referenced_by_stable_release_operator_head_queue" in changed_candidate["refusal_reasons"]
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as error:
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "aming-claw", repo_root_path=repo, dimension="worktrees",
            candidate_ids=[candidate["candidate_id"]], plan_hash=preview["plan_hash"],
            plan_revision=preview["plan_revision"],
        )
    assert error.value.payload["error"] == "stale_cleanup_plan_refused"
    monkeypatch.setattr(governance_db, "verified_stable_database_binding",
                        lambda: (_ for _ in ()).throw(RuntimeError("binding unavailable")))
    lost = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "aming-claw", repo_root_path=repo, dimension="worktrees",
    )
    lost_candidate = next(i for i in lost["candidates"] if i["artifact_type"] == "batch_worktree")
    assert lost_candidate["safe_to_apply"] is False
    assert "stable_queue_inventory_unavailable" in lost_candidate["refusal_reasons"]
    assert Path(strategy.worktree_path).exists()
    assert not list(archive_root.rglob("index/*.json"))


@pytest.mark.parametrize("fault,reason", [
    ("missing", "stable_queue_inventory_missing_or_incomplete:release_operator_head_queue_events"),
    ("corrupt", "stable_queue_inventory_corrupt:release_operator_head_queue_events.after_json"),
])
def test_ac_dev_archive_refuses_missing_or_corrupt_stable_queue(
    tmp_path, monkeypatch, fault, reason,
):
    repo, conn, _created, strategy, archive_root, stable_path = _ac_cross_world_archive_fixture(
        tmp_path, monkeypatch,
    )
    with sqlite3.connect(stable_path) as stable:
        if fault == "missing":
            stable.execute("DROP TABLE release_operator_head_queue_events")
        else:
            stable.execute(
                "INSERT INTO release_operator_head_queue_events "
                "(project_id,action,backlog_id,actor,reason,before_json,after_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("aming-claw", "insert", "unrelated", "fixture", "", "{}", "{bad",
                 batch_jobs.utc_now()),
            )
        stable.commit()
    refused = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "aming-claw", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(i for i in refused["candidates"] if i["artifact_type"] == "batch_worktree")
    assert candidate["safe_to_apply"] is False
    assert reason in candidate["refusal_reasons"]
    assert Path(strategy.worktree_path).exists()
    assert not list(archive_root.rglob("index/*.json"))


def test_ac_dev_archive_rechecks_stable_queue_immediately_before_prune(tmp_path, monkeypatch):
    repo, conn, _created, strategy, archive_root, stable_path = _ac_cross_world_archive_fixture(
        tmp_path, monkeypatch,
    )
    preview = stale_artifact_cleanup.build_merged_worktree_archive_projection(
        conn, "aming-claw", repo_root_path=repo,
    )
    candidate = preview["candidates"][0]
    assert candidate["safe_to_apply"] is True
    published = stale_artifact_cleanup.archive_merged_worktree(
        conn, "aming-claw", repo_root_path=repo,
        candidate_id=candidate["candidate_id"], plan_hash=preview["plan_hash"],
        plan_revision=preview["plan_revision"],
    )
    original_readback = stale_artifact_cleanup._archive_governance_evidence_readback
    changed = False

    def change_queue_after_plan(connection):
        nonlocal changed
        if not changed:
            with sqlite3.connect(stable_path) as stable:
                stable.execute(
                    "INSERT INTO release_operator_head_queue "
                    "(project_id,backlog_id,position,inserted_at,updated_at) VALUES (?,?,?,?,?)",
                    ("aming-claw", "unrelated-backlog", 1, batch_jobs.utc_now(),
                     batch_jobs.utc_now()),
                )
                stable.commit()
            changed = True
        return original_readback(connection)

    monkeypatch.setattr(stale_artifact_cleanup, "_archive_governance_evidence_readback",
                        change_queue_after_plan)
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as error:
        stale_artifact_cleanup.prune_archived_merged_worktree(
            conn, "aming-claw", repo_root_path=repo,
            candidate_id=candidate["candidate_id"], plan_hash=preview["plan_hash"],
            plan_revision=preview["plan_revision"], generation=published["generation"],
        )
    assert error.value.payload["error"] == "stable_queue_prune_authority_drift"
    assert Path(strategy.worktree_path).exists()
    assert list(archive_root.rglob("index/*.json"))


def test_ac_dev_archive_refuses_old_marker_and_wrong_stable_identity(tmp_path, monkeypatch):
    repo, conn, _created, strategy, archive_root, _stable_path = _ac_cross_world_archive_fixture(
        tmp_path, monkeypatch,
    )
    binding = governance_db.verified_stable_database_binding()
    marker = binding["health"].pop("ac_release_queue_writer_fence")
    old = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "aming-claw", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(i for i in old["candidates"] if i["artifact_type"] == "batch_worktree")
    assert "stable_queue_writer_fence_unavailable" in candidate["refusal_reasons"]
    binding["health"]["ac_release_queue_writer_fence"] = marker
    binding["stable_database_identity"]["inode"] += 1
    wrong = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "aming-claw", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(i for i in wrong["candidates"] if i["artifact_type"] == "batch_worktree")
    assert "stable_queue_inventory_unavailable" in candidate["refusal_reasons"]
    assert Path(strategy.worktree_path).exists()
    assert not list(archive_root.rglob("index/*.json"))


def test_ac_stable_directory_lock_allows_sqlite_and_excludes_other_process(
    tmp_path, monkeypatch,
):
    _repo, _conn, _created, _strategy, _archive_root, stable_path = (
        _ac_cross_world_archive_fixture(tmp_path, monkeypatch)
    )
    child = (
        "import fcntl,os,sys; "
        "fd=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW); "
        "\ntry: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); print('acquired')"
        "\nexcept BlockingIOError: print('busy')"
    )
    with stale_artifact_cleanup._stable_queue_exclusive_fence():
        with sqlite3.connect(stable_path) as stable:
            stable.execute("BEGIN IMMEDIATE")
            stable.execute(
                "INSERT INTO release_operator_head_queue "
                "(project_id,backlog_id,position,inserted_at,updated_at) VALUES (?,?,?,?,?)",
                ("aming-claw", "fixture-write", 1, batch_jobs.utc_now(), batch_jobs.utc_now()),
            )
            stable.commit()
            assert stable.execute(
                "SELECT backlog_id FROM release_operator_head_queue WHERE project_id=?",
                ("aming-claw",),
            ).fetchone()[0] == "fixture-write"
        writer = (
            "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute('BEGIN IMMEDIATE'); "
            "c.execute('INSERT INTO release_operator_head_queue "
            "(project_id,backlog_id,position,inserted_at,updated_at) "
            "VALUES (?,?,?,?,?)',('aming-claw','cross-process-write',2,'now','now')); "
            "c.commit(); "
            "print(c.execute('SELECT backlog_id FROM release_operator_head_queue "
            "WHERE backlog_id=?',('cross-process-write',)).fetchone()[0])"
        )
        written = subprocess.run([sys.executable, "-c", writer, str(stable_path)],
                                 capture_output=True, text=True, check=True)
        assert written.stdout.strip() == "cross-process-write"
        with sqlite3.connect(stable_path.as_uri() + "?mode=ro", uri=True) as readonly:
            assert readonly.execute("SELECT COUNT(*) FROM release_operator_head_queue").fetchone()[0] == 2
        busy = subprocess.run([sys.executable, "-c", child, str(stable_path.parent)],
                              capture_output=True, text=True, check=True)
        assert busy.stdout.strip() == "busy"
    acquired = subprocess.run([sys.executable, "-c", child, str(stable_path.parent)],
                              capture_output=True, text=True, check=True)
    assert acquired.stdout.strip() == "acquired"


def test_ac_stable_directory_lock_detects_replaced_parent(tmp_path, monkeypatch):
    _repo, _conn, _created, _strategy, _archive_root, stable_path = (
        _ac_cross_world_archive_fixture(tmp_path, monkeypatch)
    )
    original_parent = stable_path.parent
    moved_parent = tmp_path / "stable-world-moved"
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as error:
        with stale_artifact_cleanup._stable_queue_exclusive_fence():
            original_parent.rename(moved_parent)
            original_parent.mkdir()
            os.link(moved_parent / stable_path.name, stable_path)
    assert str(error.value) == "stable_queue_writer_fence_identity_drift"


def test_ac_dev_prune_refuses_other_process_holding_stable_queue_lock(tmp_path, monkeypatch):
    repo, conn, _created, strategy, archive_root, stable_path = _ac_cross_world_archive_fixture(
        tmp_path, monkeypatch,
    )
    preview = stale_artifact_cleanup.build_merged_worktree_archive_projection(
        conn, "aming-claw", repo_root_path=repo,
    )
    candidate = preview["candidates"][0]
    published = stale_artifact_cleanup.archive_merged_worktree(
        conn, "aming-claw", repo_root_path=repo,
        candidate_id=candidate["candidate_id"], plan_hash=preview["plan_hash"],
        plan_revision=preview["plan_revision"],
    )
    child = (
        "import fcntl,os,sys; "
        "fd=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW); "
        "fcntl.flock(fd,fcntl.LOCK_EX); print('locked',flush=True); "
        "sys.stdin.readline(); os.close(fd)"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", child, str(stable_path.parent)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as error:
            stale_artifact_cleanup.prune_archived_merged_worktree(
                conn, "aming-claw", repo_root_path=repo,
                candidate_id=candidate["candidate_id"], plan_hash=preview["plan_hash"],
                plan_revision=preview["plan_revision"], generation=published["generation"],
            )
        assert str(error.value) == "stable_queue_writer_fence_busy"
        assert Path(strategy.worktree_path).exists()
        assert list(archive_root.rglob("index/*.json"))
    finally:
        holder.communicate(input="\n", timeout=5)
    assert holder.returncode == 0
    pruned = stale_artifact_cleanup.prune_archived_merged_worktree(
        conn, "aming-claw", repo_root_path=repo,
        candidate_id=candidate["candidate_id"], plan_hash=preview["plan_hash"],
        plan_revision=preview["plan_revision"], generation=published["generation"],
    )
    assert pruned["state"] == "pruned"


def test_stable_queue_post_enters_lock_before_connection_and_releases_on_return(
    tmp_path, monkeypatch,
):
    _repo, _conn, _created, _strategy, _archive_root, stable_path = (
        _ac_cross_world_archive_fixture(tmp_path, monkeypatch)
    )
    monkeypatch.setattr(governance_server, "_runtime_plane", lambda: "stable")
    observed = []

    class GuardedConnection:
        def __enter__(self):
            with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as error:
                with stale_artifact_cleanup._stable_queue_exclusive_fence():
                    pass
            assert str(error.value) == "stable_queue_writer_fence_busy"
            observed.append("entered_under_lock")
            self.conn = sqlite3.connect(stable_path)
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "INSERT INTO release_operator_head_queue "
                "(project_id,backlog_id,position,inserted_at,updated_at) VALUES (?,?,?,?,?)",
                ("aming-claw", "route-lock-fixture", 1, batch_jobs.utc_now(),
                 batch_jobs.utc_now()),
            )
            return self.conn

        def __exit__(self, _kind, _value, _traceback):
            self.conn.commit()
            self.conn.close()
            observed.append("closed_under_lock")

    monkeypatch.setattr(governance_server, "DBContext", lambda _project: GuardedConnection())
    ctx = governance_server.RequestContext(
        None, "POST", {"project_id": "aming-claw"}, {}, {"action": "invalid"},
        "req-stable-route", "", "",
    )
    status, body = governance_server.handle_project_release_operator_head_queue(ctx)
    assert status == 400
    assert body["error"] == "invalid_release_operator_head_queue_action"
    assert observed == ["entered_under_lock", "closed_under_lock"]
    with stale_artifact_cleanup._stable_queue_exclusive_fence():
        pass

    binding = governance_db.verified_stable_database_binding()
    monkeypatch.setattr(governance_server, "_ac_stable_promotion_signoff_body", lambda _body: {
        "stable_database_identity": binding["stable_database_identity"],
        "stable_anchor_commit": binding["stable_head"],
    })

    def open_signoff_connection(_identity):
        with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as error:
            with stale_artifact_cleanup._stable_queue_exclusive_fence():
                pass
        assert str(error.value) == "stable_queue_writer_fence_busy"
        observed.append("signoff_opened_after_lock")
        return sqlite3.connect(stable_path)

    monkeypatch.setattr(governance_server, "_ac_stable_release_control_connection",
                        open_signoff_connection)
    signoff_ctx = governance_server.RequestContext(
        None, "POST", {"project_id": "aming-claw"}, {}, {"action": "invalid"},
        "req-signoff-route", "fixture-token", "",
    )
    status, _body = governance_server.handle_project_release_operator_head_queue(signoff_ctx)
    assert status == 400
    assert observed[-1] == "signoff_opened_after_lock"
    with stale_artifact_cleanup._stable_queue_exclusive_fence():
        pass

    def fail_signoff_open(_identity):
        raise RuntimeError("fixture connection failure")

    monkeypatch.setattr(governance_server, "_ac_stable_release_control_connection",
                        fail_signoff_open)
    with pytest.raises(RuntimeError, match="fixture connection failure"):
        governance_server.handle_project_release_operator_head_queue(signoff_ctx)
    with stale_artifact_cleanup._stable_queue_exclusive_fence():
        pass


def test_archive_preview_shows_active_registered_worktree_as_protected(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    conn = _conn()
    created = batch_jobs.create_batch_task(
        conn, "proj", "active", repo_root_path=repo,
        batch_id="active-archive", base_commit=batch_jobs.git_commit(repo),
    )
    strategy = batch_jobs.BranchStrategy(**created["branch_strategy"])
    batch_jobs.create_worktree(strategy, repo_root_path=repo)
    archive_root = tmp_path / "archive-volume"
    archive_root.mkdir()
    monkeypatch.setenv(stale_artifact_cleanup.ARCHIVE_ROOT_ENV, str(archive_root))
    real_ismount = os.path.ismount
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == archive_root or real_ismount(path))
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(i for i in preview["candidates"] if i["artifact_type"] == "batch_worktree")
    assert candidate["safe_to_apply"] is False
    assert "referenced_by_active_batch_task" in candidate["refusal_reasons"]
    assert Path(strategy.worktree_path).exists()


@pytest.mark.parametrize("missing_table", sorted(stale_artifact_cleanup._ARCHIVE_REFERENCE_COLUMNS))
def test_archive_missing_required_reference_store_refuses_public_apply_without_deletion(
    tmp_path, monkeypatch, missing_table,
):
    repo, conn, _created, strategy, archive_root = _archive_fixture(tmp_path, monkeypatch)
    approved = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(i for i in approved["candidates"] if i["artifact_type"] == "batch_worktree")
    assert candidate["safe_to_apply"] is True
    conn.execute(f'DROP TABLE "{missing_table}"')
    conn.commit()
    refused = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    if missing_table == "tasks":
        assert refused["error"] == "archive_reference_inventory_refused"
        assert refused["apply_plan_available"] is False
        reasons = refused["refusal_reasons"]
    else:
        refused_candidate = next(i for i in refused["candidates"]
                                 if i["artifact_type"] == "batch_worktree")
        assert refused_candidate["safe_to_apply"] is False
        reasons = refused_candidate["refusal_reasons"]
    assert "reference_inventory_missing:" + missing_table in reasons
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as error:
        stale_art_cleanup_apply(conn, repo, candidate, approved)
    assert error.value.payload["error"] == (
        "archive_reference_inventory_refused" if missing_table == "tasks"
        else "stale_cleanup_plan_refused"
    )
    if missing_table != "tasks":
        with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as fresh_error:
            stale_art_cleanup_apply(conn, repo, refused_candidate, refused)
        assert fresh_error.value.payload["error"] == "unsafe_stale_artifact_cleanup_refused"
    assert Path(strategy.worktree_path).exists()
    assert not list(archive_root.rglob("index/*.json"))


@pytest.mark.parametrize("table", [
    "sessions", "parallel_branch_runtime_contexts",
    "contract_runtime_executions", "release_operator_head_queue",
])
def test_archive_partial_required_reference_schema_refuses_public_apply(
    tmp_path, monkeypatch, table,
):
    repo, conn, _created, strategy, archive_root = _archive_fixture(tmp_path, monkeypatch)
    approved = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(i for i in approved["candidates"] if i["artifact_type"] == "batch_worktree")
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'CREATE TABLE "{table}" (fixture_only TEXT)')
    conn.commit()
    refused = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    refused_candidate = next(i for i in refused["candidates"]
                             if i["artifact_type"] == "batch_worktree")
    assert refused_candidate["safe_to_apply"] is False
    assert "reference_inventory_schema_incomplete:" + table in refused_candidate["refusal_reasons"]
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as error:
        stale_art_cleanup_apply(conn, repo, candidate, approved)
    assert error.value.payload["error"] == "stale_cleanup_plan_refused"
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as fresh_error:
        stale_art_cleanup_apply(conn, repo, refused_candidate, refused)
    assert fresh_error.value.payload["error"] == "unsafe_stale_artifact_cleanup_refused"
    assert Path(strategy.worktree_path).exists()
    assert not list(archive_root.rglob("index/*.json"))


@pytest.mark.parametrize("table", [
    "sessions", "parallel_branch_runtime_contexts",
    "contract_runtime_executions", "release_operator_head_queue",
])
def test_archive_unreadable_required_reference_store_refuses_public_apply(
    tmp_path, monkeypatch, table,
):
    repo, conn, _created, strategy, archive_root = _archive_fixture(tmp_path, monkeypatch)
    approved = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(i for i in approved["candidates"] if i["artifact_type"] == "batch_worktree")

    def deny_reference_read(action, arg1, _arg2, _database, _trigger):
        if action == sqlite3.SQLITE_READ and arg1 == table:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(deny_reference_read)
    refused = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    refused_candidate = next(i for i in refused["candidates"]
                             if i["artifact_type"] == "batch_worktree")
    assert refused_candidate["safe_to_apply"] is False
    assert "reference_inventory_unreadable:" + table in refused_candidate["refusal_reasons"]
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as error:
        stale_art_cleanup_apply(conn, repo, candidate, approved)
    assert error.value.payload["error"] == "stale_cleanup_plan_refused"
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as fresh_error:
        stale_art_cleanup_apply(conn, repo, refused_candidate, refused)
    assert fresh_error.value.payload["error"] == "unsafe_stale_artifact_cleanup_refused"
    conn.set_authorizer(None)
    assert Path(strategy.worktree_path).exists()
    assert not list(archive_root.rglob("index/*.json"))


def test_archive_corrupt_session_json_refuses_public_apply(tmp_path, monkeypatch):
    repo, conn, _created, strategy, archive_root = _archive_fixture(tmp_path, monkeypatch)
    conn.execute(
        "INSERT INTO sessions (session_id,principal_id,project_id,role,scope_json,token_hash,"
        "status,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("ses-corrupt", "qa", "proj", "qa", "{bad", "hash-corrupt", "active",
         "2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z"),
    )
    conn.commit()
    refused = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(i for i in refused["candidates"] if i["artifact_type"] == "batch_worktree")
    assert candidate["safe_to_apply"] is False
    assert "reference_inventory_corrupt:sessions.scope_json" in candidate["refusal_reasons"]
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as error:
        stale_art_cleanup_apply(conn, repo, candidate, refused)
    assert error.value.payload["error"] == "unsafe_stale_artifact_cleanup_refused"
    assert Path(strategy.worktree_path).exists()
    assert not list(archive_root.rglob("index/*.json"))


def test_archive_release_queue_backlog_only_reference_refuses(tmp_path, monkeypatch):
    repo, conn, created, strategy, archive_root = _archive_fixture(tmp_path, monkeypatch)
    row = conn.execute("SELECT metadata_json FROM tasks WHERE task_id=?",
                       (created["task_id"],)).fetchone()
    metadata = json.loads(row[0])
    metadata["bug_id"] = "OPT-QUEUED-ARCHIVE"
    conn.execute("UPDATE tasks SET metadata_json=? WHERE task_id=?",
                 (json.dumps(metadata), created["task_id"]))
    conn.execute(
        "INSERT INTO release_operator_head_queue "
        "(project_id,backlog_id,position,inserted_at,updated_at) VALUES (?,?,?,?,?)",
        ("proj", "OPT-QUEUED-ARCHIVE", 1, batch_jobs.utc_now(), batch_jobs.utc_now()),
    )
    conn.commit()
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(i for i in preview["candidates"] if i["artifact_type"] == "batch_worktree")
    assert candidate["safe_to_apply"] is False
    assert "referenced_by_release_operator_head_queue" in candidate["refusal_reasons"]
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError):
        stale_art_cleanup_apply(conn, repo, candidate, preview)
    assert Path(strategy.worktree_path).exists()
    assert not list(archive_root.rglob("index/*.json"))


@pytest.mark.parametrize("kind", ["session", "lease", "cex", "queue"])
def test_archive_preview_refuses_live_governance_reference(tmp_path, monkeypatch, kind):
    repo, conn, created, strategy, archive_root = _archive_fixture(tmp_path, monkeypatch)
    if kind == "session":
        conn.execute(
            "INSERT INTO sessions (session_id,principal_id,project_id,role,scope_json,token_hash,"
            "status,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("ses-protected", "qa", "proj", "qa", json.dumps({"worktree_path": str(strategy.worktree_path)}),
             "hash-protected", "active", "2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z"),
        )
        table = "sessions"
    else:
        table = "fixture_" + kind + "_refs"
        conn.execute(f"CREATE TABLE {table} (reference TEXT)")
        conn.execute(f"INSERT INTO {table} VALUES (?)", (str(strategy.worktree_path),))
    conn.commit()
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(i for i in preview["candidates"] if i["artifact_type"] == "batch_worktree")
    assert candidate["safe_to_apply"] is False
    assert "referenced_by_" + table in candidate["refusal_reasons"]
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError):
        stale_art_cleanup_apply(conn, repo, candidate, preview)
    assert Path(strategy.worktree_path).exists()
    assert list(archive_root.iterdir()) == []


def stale_art_cleanup_apply(conn, repo, candidate, preview):
    return stale_artifact_cleanup.apply_stale_artifact_cleanup(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
        candidate_ids=[candidate["candidate_id"]],
        plan_hash=preview["plan_hash"], plan_revision=preview["plan_revision"],
    )


@pytest.mark.parametrize("fault,reason", [
    ("acl", "archive_volume_or_acl_unverified"),
    ("capacity", "archive_capacity_insufficient"),
])
def test_archive_preflight_refuses_acl_or_capacity(tmp_path, monkeypatch, fault, reason):
    repo, conn, _created, strategy, archive_root = _archive_fixture(tmp_path, monkeypatch)
    if fault == "acl":
        actual_access = os.access
        monkeypatch.setattr(os, "access", lambda path, mode: False if Path(path) == archive_root
                            else actual_access(path, mode))
    else:
        actual_usage = shutil.disk_usage
        monkeypatch.setattr(shutil, "disk_usage", lambda path: actual_usage(path)._replace(free=1)
                            if Path(path) == archive_root else actual_usage(path))
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(i for i in preview["candidates"] if i["artifact_type"] == "batch_worktree")
    assert candidate["safe_to_apply"] is False
    assert reason in candidate["refusal_reasons"]
    assert Path(strategy.worktree_path).exists()


def test_archive_copy_hash_failure_leaves_source_and_no_index(tmp_path, monkeypatch):
    repo, conn, _created, strategy, archive_root = _archive_fixture(tmp_path, monkeypatch)
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(i for i in preview["candidates"] if i["artifact_type"] == "batch_worktree")
    original_digest = stale_artifact_cleanup._archive_file_digest

    def corrupt_copy(path):
        size, digest = original_digest(path)
        return (size, "sha256:" + "0" * 64) if "staging" in path.parts else (size, digest)

    monkeypatch.setattr(stale_artifact_cleanup, "_archive_file_digest", corrupt_copy)
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError,
                       match="archive_before_prune_refused") as refused:
        stale_art_cleanup_apply(conn, repo, candidate, preview)
    assert refused.value.payload["cause"] == "archive_copy_hash_mismatch"
    assert refused.value.payload["source_removed"] is False
    assert Path(strategy.worktree_path).exists()
    assert not list(archive_root.rglob("index/*.json"))


def test_archive_index_failure_leaves_source_and_published_bundle_unpruned(tmp_path, monkeypatch):
    repo, conn, _created, strategy, archive_root = _archive_fixture(tmp_path, monkeypatch)
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(i for i in preview["candidates"] if i["artifact_type"] == "batch_worktree")
    original_write = stale_artifact_cleanup._archive_write_atomic

    def fail_index(path, data):
        if path.parent.name == "index":
            raise OSError("fixture index write failure")
        return original_write(path, data)

    monkeypatch.setattr(stale_artifact_cleanup, "_archive_write_atomic", fail_index)
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError,
                       match="archive_before_prune_refused") as refused:
        stale_art_cleanup_apply(conn, repo, candidate, preview)
    assert refused.value.payload["cause"] == "archive_publish_failed"
    assert Path(strategy.worktree_path).exists()
    assert list(archive_root.rglob("bundles/*/manifest.json"))
    assert not list(archive_root.rglob("index/*.json"))


def test_archive_plan_and_volume_drift_refuse_without_source_deletion(tmp_path, monkeypatch):
    repo, conn, _created, strategy, archive_root = _archive_fixture(tmp_path, monkeypatch)
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(i for i in preview["candidates"] if i["artifact_type"] == "batch_worktree")
    (Path(strategy.worktree_path) / "drift.txt").write_text("untracked")
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError,
                       match="stale_cleanup_plan_refused"):
        stale_art_cleanup_apply(conn, repo, candidate, preview)
    (Path(strategy.worktree_path) / "drift.txt").unlink()
    archive_root.rename(tmp_path / "archive-disconnected")
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError,
                       match="stale_cleanup_plan_refused"):
        stale_art_cleanup_apply(conn, repo, candidate, preview)
    assert Path(strategy.worktree_path).exists()


def test_archive_crash_after_remove_replay_never_repeats_removal(tmp_path, monkeypatch):
    repo, conn, _created, strategy, archive_root = _archive_fixture(tmp_path, monkeypatch)
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(i for i in preview["candidates"] if i["artifact_type"] == "batch_worktree")
    published = stale_artifact_cleanup.archive_merged_worktree(
        conn, "proj", repo_root_path=repo,
        candidate_id=candidate["candidate_id"], plan_hash=preview["plan_hash"],
        plan_revision=preview["plan_revision"],
    )
    original_write = stale_artifact_cleanup._archive_write_atomic

    def crash_before_receipt(path, data):
        if path.parent.name == "index" and b'"state":"pruned"' in data:
            raise OSError("fixture crash before durable prune receipt")
        return original_write(path, data)

    monkeypatch.setattr(stale_artifact_cleanup, "_archive_write_atomic", crash_before_receipt)
    with pytest.raises(OSError, match="fixture crash"):
        stale_artifact_cleanup.prune_archived_merged_worktree(
            conn, "proj", repo_root_path=repo,
            candidate_id=candidate["candidate_id"], plan_hash=preview["plan_hash"],
            plan_revision=preview["plan_revision"], generation=published["generation"],
        )
    assert not Path(strategy.worktree_path).exists()
    monkeypatch.setattr(stale_artifact_cleanup, "_archive_write_atomic", original_write)
    monkeypatch.setattr(stale_artifact_cleanup, "_remove_worktree",
                        lambda **_kw: pytest.fail("replay must not remove again"))
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError,
                       match="archive_prune_partial_uncertain"):
        stale_artifact_cleanup.prune_archived_merged_worktree(
            conn, "proj", repo_root_path=repo,
            candidate_id=candidate["candidate_id"], plan_hash=preview["plan_hash"],
            plan_revision=preview["plan_revision"], generation=published["generation"],
        )
    index = stale_artifact_cleanup._archive_load_index(
        stale_artifact_cleanup._archive_index_path(archive_root, "proj", candidate["candidate_id"])
    )
    assert index["state"] == "partial_uncertain"


@pytest.mark.parametrize("drift", ["head", "inode"])
def test_archive_apply_rejects_head_or_inode_drift_before_copy(tmp_path, monkeypatch, drift):
    repo, conn, _created, strategy, archive_root = _archive_fixture(tmp_path, monkeypatch)
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(i for i in preview["candidates"] if i["artifact_type"] == "batch_worktree")
    source = Path(strategy.worktree_path)
    if drift == "head":
        (source / "new-head.txt").write_text("drift\n", encoding="utf-8")
        subprocess.run(["git", "add", "new-head.txt"], cwd=source, check=True)
        subprocess.run(["git", "commit", "-m", "new head"], cwd=source,
                       check=True, capture_output=True)
    else:
        source.rename(source.with_name(source.name + "-held"))
        source.mkdir()
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError,
                       match="stale_cleanup_plan_refused"):
        stale_art_cleanup_apply(conn, repo, candidate, preview)
    assert source.exists()
    assert not list(archive_root.rglob("index/*.json"))


def test_archive_public_frame_overflow_refuses_plan_and_removal(tmp_path, monkeypatch):
    repo, conn, _created, strategy, archive_root = _archive_fixture(tmp_path, monkeypatch)
    original = stale_artifact_cleanup._archive_candidate

    def oversized(*args, **kwargs):
        candidate = original(*args, **kwargs)
        candidate["evidence"]["unrepresentable_identity"] = "雪\\\"\n" * 150_000
        return candidate

    monkeypatch.setattr(stale_artifact_cleanup, "_archive_candidate", oversized)
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    assert preview["error"] == "cleanup_response_identity_overflow"
    assert preview["apply_plan_available"] is False
    assert preview["candidates"] == []
    sizes = stale_artifact_cleanup.cleanup_response_wire_bytes(preview)
    assert all(size <= 224 * 1024 for size in sizes.values())
    candidate_id = stale_artifact_cleanup._candidate_id("batch_worktree", str(strategy.worktree_path))
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError,
                       match="cleanup_response_identity_overflow"):
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "proj", repo_root_path=repo, dimension="worktrees",
            candidate_ids=[candidate_id], plan_hash=preview["plan_hash"],
            plan_revision=preview["plan_revision"],
        )
    assert Path(strategy.worktree_path).exists()
    assert not list(archive_root.rglob("index/*.json"))


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
    assert projection["summary"]["safe_apply_count"] == 1
    assert projection["summary"]["backlog_reference_count"] == 1
    assert projection["append_only_retained"]["graph_trace_ids"] == ["gqt-clean"]
    assert projection["append_only_retained"]["task_timeline_event_count"] == 1
    by_type = {item["artifact_type"]: item for item in projection["candidates"]}
    assert by_type["batch_worktree"]["safe_to_apply"] is False
    assert "terminal_merged_task_required" in by_type["batch_worktree"]["refusal_reasons"]
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


def test_legacy_apply_only_clears_terminal_reference_and_retains_worktree(tmp_path):
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
    assert result["applied_count"] == 1
    assert (repo / ".worktrees" / "batch-apply").exists()
    meta = json.loads(
        conn.execute(
            "SELECT metadata_json FROM tasks WHERE task_id=?",
            (created["task_id"],),
        ).fetchone()["metadata_json"]
    )
    assert "stale_artifact_cleanup" not in meta
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
    _created, strategy = _terminal_batch_with_worktree(conn, repo, batch_id="mixed")
    _insert_backlog_ref(conn, bug_id="OPT-MIXED", worktree_path=strategy.worktree_path)
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
    candidate = next(item for item in preview["candidates"] if item["artifact_type"] == "batch_worktree")
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
            assert item["artifact_type"] == "derived_run_pair"
            assert "missing_or_unpaired:state_reconcile" in item["refusal_reasons"]
            assert item["safe_to_apply"] is False
    index_item = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="governance_index",
    )["candidates"][0]
    assert index_item["evidence"]["run_id"] == "run-1"
    assert index_item["evidence"]["paired"] is False
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "summary.json").write_text(json.dumps({"commit_sha": "secret"}),
                                          encoding="utf-8")
    link = tmp_path / "proj" / "governance-index" / "linked"
    link.symlink_to(outside, target_is_directory=True)
    linked = next(item for item in stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="governance_index",
    )["candidates"] if item["path"] == str(link))
    assert "derived_path_symlink_refused:governance_index" in linked["refusal_reasons"]
    assert linked["evidence"]["commit_sha"] == ""


def _new_terminal_derived_pair(tmp_path, monkeypatch):
    from agent.governance import db, state_reconcile

    monkeypatch.setattr(db, "_governance_root", lambda: tmp_path / "governance")
    repo = _git_repo(tmp_path)
    source = repo / "agent" / "service.py"
    source.parent.mkdir()
    source.write_text("def value():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "source"], cwd=repo,
                   check=True, capture_output=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                            check=True, capture_output=True, text=True).stdout.strip()
    conn = _conn()
    generated = state_reconcile.run_state_only_full_reconcile(
        conn, "proj", repo, run_id="paired-run", commit_sha=commit,
        snapshot_id="full-paired-run", semantic_enrich=False, activate=False,
    )
    assert generated["ok"]
    conn.execute("UPDATE graph_snapshots SET status='superseded' "
                 "WHERE project_id='proj' AND snapshot_id='full-paired-run'")
    original = dict(conn.execute(
        "SELECT * FROM graph_snapshots WHERE project_id='proj' AND snapshot_id='full-paired-run'",
    ).fetchone())
    original.update(snapshot_id="full-newer", status="active", created_at="9999-01-01T00:00:00Z")
    columns = list(original)
    conn.execute("INSERT INTO graph_snapshots (" + ",".join(columns) + ") VALUES (" +
                 ",".join("?" for _ in columns) + ")", list(original.values()))
    conn.commit()
    archive = tmp_path / "archive-volume"
    archive.mkdir()
    monkeypatch.setenv(stale_artifact_cleanup.ARCHIVE_ROOT_ENV, str(archive))
    real_mount = os.path.ismount
    monkeypatch.setattr(os.path, "ismount",
                        lambda path: Path(path) == archive or real_mount(path))
    return repo, conn, archive


def test_new_terminal_derived_pair_public_preview_and_prune(tmp_path, monkeypatch):
    repo, conn, archive = _new_terminal_derived_pair(tmp_path, monkeypatch)
    views = [stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension=dimension,
    ) for dimension in ("governance_index", "state_reconcile")]
    assert len(views[0]["candidates"]) == len(views[1]["candidates"]) == 1
    candidate = views[0]["candidates"][0]
    assert candidate["candidate_id"] == views[1]["candidates"][0]["candidate_id"]
    assert candidate["safe_to_apply"] is True, candidate["refusal_reasons"]
    all_view = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="all",
    )
    assert sum(row["candidate_id"] == candidate["candidate_id"]
               for row in all_view["candidates"]) == 1
    applied = stale_artifact_cleanup.apply_stale_artifact_cleanup(
        conn, "proj", repo_root_path=repo, dimension="governance_index",
        candidate_ids=[candidate["candidate_id"]],
        plan_hash=views[0]["plan_hash"], plan_revision=views[0]["plan_revision"],
    )
    assert applied["state"] == "pruned"
    assert applied["restore_rebuild"]["independent_rebuild"] is True
    assert not Path(candidate["source_paths"]["governance_index"]).exists()
    assert not Path(candidate["source_paths"]["state_reconcile"]).exists()
    assert Path(applied["index_path"]).is_file()
    replay = stale_artifact_cleanup.apply_stale_artifact_cleanup(
        conn, "proj", repo_root_path=repo, dimension="governance_index",
        candidate_ids=[candidate["candidate_id"]],
        plan_hash=views[0]["plan_hash"], plan_revision=views[0]["plan_revision"],
    )
    assert replay["state"] == "replay"
    assert replay["applied_count"] == 0
    assert replay["writes_performed"] is False


def test_derived_pair_exact_live_reference_and_legacy_refuse(tmp_path, monkeypatch):
    repo, conn, _archive = _new_terminal_derived_pair(tmp_path, monkeypatch)
    preview = lambda: stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="governance_index",
    )["candidates"][0]
    assert preview()["safe_to_apply"] is True
    conn.execute("INSERT INTO graph_snapshot_refs "
                 "(project_id,ref_name,snapshot_id,commit_sha,updated_at) "
                 "VALUES ('proj','candidate','full-paired-run','x','now')")
    assert "protected:current_graph_ref" in preview()["refusal_reasons"]
    conn.execute("DELETE FROM graph_snapshot_refs WHERE ref_name='candidate'")
    conn.execute(
        "INSERT INTO sessions (session_id,principal_id,project_id,role,scope_json,"
        "token_hash,status,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("qa-pair", "qa", "proj", "qa",
         json.dumps({"snapshot_id": "full-paired-run"}), "qa-pair-hash",
         "active", "now", "later"),
    )
    assert "referenced_by_sessions" in preview()["refusal_reasons"]
    conn.execute("DELETE FROM sessions WHERE session_id='qa-pair'")
    conn.execute("UPDATE graph_snapshots SET status='active' "
                 "WHERE project_id='proj' AND snapshot_id='full-paired-run'")
    assert "protected:snapshot_not_terminal" in preview()["refusal_reasons"]
    conn.execute("UPDATE graph_snapshots SET status='superseded' "
                 "WHERE project_id='proj' AND snapshot_id='full-paired-run'")
    provenance = (tmp_path / "governance" / "proj" / "state-reconcile" /
                  "paired-run" / "trace" / "derived-rebuild.json")
    provenance.unlink()
    candidate = preview()
    assert candidate["safe_to_apply"] is False
    assert "legacy_missing_rebuild_provenance" in candidate["refusal_reasons"]


def test_derived_pair_newest_and_active_build_claim_refuse(tmp_path, monkeypatch):
    repo, conn, _archive = _new_terminal_derived_pair(tmp_path, monkeypatch)
    preview = lambda: stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="state_reconcile",
    )["candidates"][0]
    conn.execute("DELETE FROM graph_snapshots WHERE project_id='proj' AND snapshot_id='full-newer'")
    assert "protected:newest_snapshot" in preview()["refusal_reasons"]
    conn.execute(
        "INSERT INTO graph_current_full_build_claim_history "
        "(claim_id,project_id,snapshot_id,run_id,commit_sha,status,manager_epoch,"
        "manager_pid,manager_started_at,manager_start_identity,acquired_at) "
        "VALUES (?,?,?,?,?,'active',?,?,?,?,?)",
        ("pair-active-claim", "proj", "full-paired-run", "paired-run", "commit",
         "epoch", 999, "now", "fixture", "now"),
    )
    reasons = preview()["refusal_reasons"]
    assert "protected:newest_snapshot" in reasons
    assert "protected:active_build_claim" in reasons


@pytest.mark.parametrize("store", [
    "observer_command_queue", "parallel_branch_runtime_contexts",
    "contract_runtime_executions",
])
def test_derived_pair_live_unique_reference_refuses_then_terminal_clears(
    tmp_path, monkeypatch, store,
):
    repo, conn, _archive = _new_terminal_derived_pair(tmp_path, monkeypatch)
    preview = lambda: stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="governance_index",
    )
    approved = preview()
    candidate = approved["candidates"][0]
    assert candidate["safe_to_apply"] is True
    paths = {kind: Path(path) for kind, path in candidate["source_paths"].items()}
    before = {kind: stale_artifact_cleanup._derived_tree_facts(path)
              for kind, path in paths.items()}
    if store == "observer_command_queue":
        conn.execute(
            "INSERT INTO observer_command_queue "
            "(command_id,project_id,command_type,payload_json,status,created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("pair-command", "proj", "review", json.dumps({
                "run_id": "paired-run", "snapshot_id": "full-paired-run",
                "source_path": str(paths["governance_index"]),
            }), "queued", "now"),
        )
    elif store == "parallel_branch_runtime_contexts":
        conn.execute(
            "INSERT INTO parallel_branch_runtime_contexts "
            "(project_id,task_id,runtime_context_id,worktree_path,lease_id,"
            "lease_expires_at,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("proj", "pair-runtime-task", "pair-runtime", str(paths["state_reconcile"]),
             "pair-lease", "9999-01-01T00:00:00Z", "running", "now", "now"),
        )
    else:
        conn.execute(
            "INSERT INTO contract_runtime_executions "
            "(contract_execution_id,project_id,backlog_id,contract_id,version,"
            "revision,execution_state_revision,record_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("pair-cex", "proj", "pair-backlog", "pair-contract", "1", "1", 1,
             json.dumps({"runtime_guide": {"next_legal_action": {
                 "run_id": "paired-run", "snapshot_id": "full-paired-run",
                 "source_path": str(paths["governance_index"]),
             }}}), "now", "now"),
        )
    conn.commit()
    blocked = preview()["candidates"][0]
    assert blocked["safe_to_apply"] is False
    assert "referenced_by_" + store in blocked["refusal_reasons"]
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError):
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "proj", repo_root_path=repo, dimension="governance_index",
            candidate_ids=[candidate["candidate_id"]],
            plan_hash=approved["plan_hash"], plan_revision=approved["plan_revision"],
        )
    for kind, path in paths.items():
        assert stale_artifact_cleanup._derived_tree_facts(path) == before[kind]
    if store == "observer_command_queue":
        conn.execute("UPDATE observer_command_queue SET status='completed' "
                     "WHERE command_id='pair-command'")
    elif store == "parallel_branch_runtime_contexts":
        conn.execute("UPDATE parallel_branch_runtime_contexts SET status='complete' "
                     "WHERE task_id='pair-runtime-task'")
    else:
        conn.execute("UPDATE contract_runtime_executions SET record_json=? "
                     "WHERE contract_execution_id='pair-cex'", (json.dumps({
                         "runtime_guide": {"next_legal_action": None},
                         "historical_run_id": "paired-run",
                     }),))
    conn.commit()
    cleared = preview()["candidates"][0]
    assert cleared["safe_to_apply"] is True, cleared["refusal_reasons"]
    for kind, path in paths.items():
        assert stale_artifact_cleanup._derived_tree_facts(path) == before[kind]


def test_derived_pair_archive_acl_refuses_before_source_removal(tmp_path, monkeypatch):
    repo, conn, archive = _new_terminal_derived_pair(tmp_path, monkeypatch)
    preview = lambda: stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="governance_index",
    )
    approved = preview()
    candidate = approved["candidates"][0]
    assert candidate["safe_to_apply"] is True
    before = {kind: stale_artifact_cleanup._derived_tree_facts(Path(path))
              for kind, path in candidate["source_paths"].items()}
    original_access = os.access
    monkeypatch.setattr(os, "access", lambda path, mode: (
        False if Path(path) == archive else original_access(path, mode)))
    blocked = preview()["candidates"][0]
    assert blocked["safe_to_apply"] is False
    assert "archive_volume_or_acl_unverified" in blocked["refusal_reasons"]
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError):
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "proj", repo_root_path=repo, dimension="governance_index",
            candidate_ids=[candidate["candidate_id"]],
            plan_hash=approved["plan_hash"], plan_revision=approved["plan_revision"],
        )
    for kind, path in candidate["source_paths"].items():
        assert stale_artifact_cleanup._derived_tree_facts(Path(path)) == before[kind]
    assert not list(archive.rglob("index/*.json"))


def test_derived_pair_invalid_and_symlink_escape_sources_never_prune(tmp_path, monkeypatch):
    repo, conn, archive = _new_terminal_derived_pair(tmp_path, monkeypatch)
    root = governance_db._governance_root() / "proj"
    outside = tmp_path / "outside-derived-pair"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("protected", encoding="utf-8")
    escape = root / "governance-index" / "escape-run"
    escape.symlink_to(outside, target_is_directory=True)
    invalid = root / "governance-index" / "bad run"
    invalid.mkdir()
    (invalid / "summary.json").write_text("{}", encoding="utf-8")
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="governance_index",
    )
    candidates = {item["evidence"]["run_id"]: item for item in preview["candidates"]}
    assert "derived_path_symlink_refused:governance_index" in candidates["escape-run"]["refusal_reasons"]
    assert "run_id_path_invalid" in candidates["bad run"]["refusal_reasons"]
    for run_id in ("escape-run", "bad run"):
        assert candidates[run_id]["safe_to_apply"] is False
        with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as error:
            stale_artifact_cleanup.apply_stale_artifact_cleanup(
                conn, "proj", repo_root_path=repo, dimension="governance_index",
                candidate_ids=[candidates[run_id]["candidate_id"]],
                plan_hash=preview["plan_hash"], plan_revision=preview["plan_revision"],
            )
        assert error.value.payload["error"] == "unsafe_stale_artifact_cleanup_refused"
    assert sentinel.read_text(encoding="utf-8") == "protected"
    assert escape.is_symlink() and invalid.is_dir()
    assert all(Path(path).is_dir() for path in candidates["paired-run"]["source_paths"].values())
    assert not list(archive.rglob("index/*.json"))


def test_derived_pair_stale_plan_and_reference_race_preserve_sources(tmp_path, monkeypatch):
    repo, conn, _archive = _new_terminal_derived_pair(tmp_path, monkeypatch)
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="governance_index",
    )
    candidate = preview["candidates"][0]
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError,
                       match="stale_cleanup_plan_refused"):
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "proj", repo_root_path=repo, dimension="governance_index",
            candidate_ids=[candidate["candidate_id"]],
            plan_hash="sha256:old", plan_revision=preview["plan_revision"],
        )
    original_write = stale_artifact_cleanup._archive_write_atomic

    def race(path, data):
        original_write(path, data)
        if path.name == candidate["candidate_id"] + ".json":
            conn.execute("INSERT OR REPLACE INTO graph_snapshot_refs "
                         "(project_id,ref_name,snapshot_id,commit_sha,updated_at) "
                         "VALUES ('proj','candidate','full-paired-run','x','now')")

    monkeypatch.setattr(stale_artifact_cleanup, "_archive_write_atomic", race)
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as error:
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "proj", repo_root_path=repo, dimension="governance_index",
            candidate_ids=[candidate["candidate_id"]],
            plan_hash=preview["plan_hash"], plan_revision=preview["plan_revision"],
        )
    assert error.value.payload["state"] == "archive_only"
    assert not error.value.payload["removed_members"]
    assert all(Path(path).exists() for path in candidate["source_paths"].values())


def test_derived_pair_second_prune_failure_reports_physical_partial(tmp_path, monkeypatch):
    repo, conn, _archive = _new_terminal_derived_pair(tmp_path, monkeypatch)
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="governance_index",
    )
    candidate = preview["candidates"][0]
    original_remove = shutil.rmtree
    second = Path(candidate["source_paths"]["state_reconcile"])

    def fail_second(path, *args, **kwargs):
        if Path(path) == second:
            raise OSError("injected second source failure")
        return original_remove(path, *args, **kwargs)

    monkeypatch.setattr(stale_artifact_cleanup.shutil, "rmtree", fail_second)
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as error:
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "proj", repo_root_path=repo, dimension="governance_index",
            candidate_ids=[candidate["candidate_id"]],
            plan_hash=preview["plan_hash"], plan_revision=preview["plan_revision"],
        )
    payload = error.value.payload
    assert payload["state"] == "partial"
    assert payload["removed_members"] == ["governance_index"]
    assert payload["remaining_members"] == ["state_reconcile"]
    assert not Path(candidate["source_paths"]["governance_index"]).exists()
    assert second.exists()
    assert Path(payload["bundle"]).is_dir()
    assert payload["removed_allocated_bytes_before"] >= 0
    assert isinstance(payload["free_space_delta_bytes"], int)
    assert 0 <= payload["reclaimed_allocated_bytes"] <= payload["removed_allocated_bytes_before"]
    assert not payload.get("timeline_receipt_uncertain")
    assert conn.execute("SELECT COUNT(*) FROM task_timeline_events "
                        "WHERE event_type='governance.stale_artifact_cleanup.apply'").fetchone()[0] == 1
    restored = stale_artifact_cleanup.recover_derived_run_pair_from_archive(
        "proj", candidate["candidate_id"],
    )
    assert restored["state"] == "restored_after_partial"
    assert Path(candidate["source_paths"]["governance_index"]).is_dir()


def test_derived_pair_first_prune_mid_tree_failure_records_and_restores_files(tmp_path, monkeypatch):
    repo, conn, _archive = _new_terminal_derived_pair(tmp_path, monkeypatch)
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="governance_index",
    )
    candidate = preview["candidates"][0]
    first = Path(candidate["source_paths"]["governance_index"])
    summary = first / "summary.json"
    original_remove = shutil.rmtree

    def fail_mid_tree(path, *args, **kwargs):
        if Path(path) == first:
            summary.unlink()
            raise OSError("injected mid-tree failure")
        return original_remove(path, *args, **kwargs)

    monkeypatch.setattr(stale_artifact_cleanup.shutil, "rmtree", fail_mid_tree)
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as error:
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "proj", repo_root_path=repo, dimension="governance_index",
            candidate_ids=[candidate["candidate_id"]],
            plan_hash=preview["plan_hash"], plan_revision=preview["plan_revision"],
        )
    payload = error.value.payload
    assert payload["state"] == payload["write_disposition"] == "partial"
    assert payload["removed_members"] == []
    assert payload["remaining_members"] == ["governance_index", "state_reconcile"]
    assert payload["partially_removed_members"] == ["governance_index"]
    assert "summary.json" in payload["missing_files"]["governance_index"]
    assert payload["removed_allocated_bytes_before"] > 0
    assert payload["archive_verified"] is True
    assert Path(payload["bundle"]).is_dir()
    receipt_path = Path(payload["source_readback_path"])
    assert receipt_path.is_file()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert "summary.json" in receipt["source_readback"]["members"]["governance_index"]["missing_files"]
    assert receipt["archive_verified"] is True
    assert not summary.exists()
    unexpected = first / "unexpected.txt"
    unexpected.write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError,
                       match="derived_recovery_source_drift"):
        stale_artifact_cleanup.recover_derived_run_pair_from_archive(
            "proj", candidate["candidate_id"],
        )
    assert unexpected.read_text(encoding="utf-8") == "do not overwrite"
    assert not summary.exists()
    unexpected.unlink()
    recovered = stale_artifact_cleanup.recover_derived_run_pair_from_archive(
        "proj", candidate["candidate_id"],
    )
    assert recovered["state"] == "restored_after_partial"
    assert recovered["restore_rebuild"]["independent_rebuild"] is True
    assert summary.is_file()
    assert recovered["source_readback"]["state"] == "archive_only"
    assert json.loads(Path(payload["index_path"]).read_text())["state"] == "restored_after_partial"


def test_derived_pair_missing_half_stays_canonical_unsafe_in_named_and_all(tmp_path, monkeypatch):
    repo, conn, _archive = _new_terminal_derived_pair(tmp_path, monkeypatch)
    governance = governance_db._governance_root() / "proj"
    shutil.rmtree(governance / "state-reconcile" / "paired-run")
    ids = []
    for dimension in ("governance_index", "state_reconcile", "all"):
        view = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
            conn, "proj", repo_root_path=repo, dimension=dimension,
        )
        candidates = [item for item in view["candidates"]
                      if item.get("artifact_type") == "derived_run_pair"]
        assert len(candidates) == 1
        candidate = candidates[0]
        ids.append(candidate["candidate_id"])
        assert candidate["safe_to_apply"] is False
        assert "missing_or_unpaired:state_reconcile" in candidate["refusal_reasons"]
    assert len(set(ids)) == 1


@pytest.mark.parametrize("failure", ["partial_copy", "hash_corrupt", "index_crash"])
def test_derived_pair_preprune_archive_failure_keeps_both_sources(
    tmp_path, monkeypatch, failure,
):
    repo, conn, _archive = _new_terminal_derived_pair(tmp_path, monkeypatch)
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="governance_index",
    )
    candidate = preview["candidates"][0]
    before = {kind: stale_artifact_cleanup._derived_tree_facts(Path(path))
              for kind, path in candidate["source_paths"].items()}
    original_copy = shutil.copytree
    original_write = stale_artifact_cleanup._archive_write_atomic

    def corrupt_copy(source, target, *args, **kwargs):
        if Path(source) == Path(candidate["source_paths"]["governance_index"]):
            if failure == "partial_copy":
                Path(target).mkdir()
                (Path(target) / "summary.json").write_text("partial", encoding="utf-8")
                raise OSError("injected interrupted archive copy")
            copied = original_copy(source, target, *args, **kwargs)
            (Path(target) / "summary.json").write_text("corrupt", encoding="utf-8")
            return copied
        return original_copy(source, target, *args, **kwargs)

    def crash_index(path, data):
        if Path(path).name == candidate["candidate_id"] + ".json":
            raise OSError("injected index publication crash")
        return original_write(path, data)

    if failure == "index_crash":
        monkeypatch.setattr(stale_artifact_cleanup, "_archive_write_atomic", crash_index)
    else:
        monkeypatch.setattr(stale_artifact_cleanup.shutil, "copytree", corrupt_copy)
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as error:
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "proj", repo_root_path=repo, dimension="governance_index",
            candidate_ids=[candidate["candidate_id"]],
            plan_hash=preview["plan_hash"], plan_revision=preview["plan_revision"],
        )
    assert error.value.payload["error"] == "derived_archive_preprune_refused"
    assert error.value.payload["source_removed"] is False
    for kind, path in candidate["source_paths"].items():
        assert stale_artifact_cleanup._derived_tree_facts(Path(path)) == before[kind]


def test_derived_pair_symlink_capacity_and_remount_refuse(tmp_path, monkeypatch):
    repo, conn, archive = _new_terminal_derived_pair(tmp_path, monkeypatch)
    preview = lambda: stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="governance_index",
    )
    first = preview()
    candidate = first["candidates"][0]
    source = Path(candidate["source_paths"]["governance_index"])
    outside = tmp_path / "outside"
    outside.write_text("protected", encoding="utf-8")
    (source / "escape").symlink_to(outside)
    unsafe = preview()["candidates"][0]
    assert unsafe["safe_to_apply"] is False
    assert "source_unreadable_or_unbounded:governance_index" in unsafe["refusal_reasons"]
    (source / "escape").unlink()
    original_usage = shutil.disk_usage
    monkeypatch.setattr(stale_artifact_cleanup.shutil, "disk_usage",
                        lambda path: type("Usage", (), {"free": 0})()
                        if Path(path) == archive else original_usage(path))
    capacity = preview()["candidates"][0]
    assert "archive_capacity_insufficient" in capacity["refusal_reasons"]
    monkeypatch.setattr(stale_artifact_cleanup.shutil, "disk_usage", original_usage)
    monkeypatch.setattr(os.path, "ismount", lambda _path: False)
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError):
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "proj", repo_root_path=repo, dimension="governance_index",
            candidate_ids=[candidate["candidate_id"]],
            plan_hash=first["plan_hash"], plan_revision=first["plan_revision"],
        )
    assert outside.read_text(encoding="utf-8") == "protected"
    assert all(Path(path).exists() for path in candidate["source_paths"].values())


def test_same_path_inode_replacement_between_plan_and_item_check_is_zero_write(
    tmp_path, monkeypatch,
):
    repo = _git_repo(tmp_path)
    conn = _conn()
    _created, strategy = _terminal_batch_with_worktree(conn, repo, batch_id="replace")
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    candidate = next(item for item in preview["candidates"] if item["artifact_type"] == "batch_worktree")
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
    assert rejected.value.payload["error"] == "unsafe_stale_artifact_cleanup_refused"
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
    http_handler = object.__new__(governance_server.GovernanceHandler)
    http_handler.wfile = io.BytesIO()
    http_handler.send_response = lambda _code: None
    http_handler.send_header = lambda _name, _value: None
    http_handler.end_headers = lambda: None
    http_handler._respond(200, {**preview, "request_id": "req-" + "x" * 12})
    assert http_handler.wfile.getvalue() == expected_http
    assert len(expected_http) <= 224 * 1024
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
    assert len(managed_frame) == stale_artifact_cleanup.cleanup_response_wire_bytes(
        preview,
    )["managed_mcp"]
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
    assert len(standalone_frame) == stale_artifact_cleanup.cleanup_response_wire_bytes(
        preview,
    )["standalone_mcp"]
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


@pytest.mark.parametrize("candidate_count", [129, 131, 136])
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
            "reasons": [f"reference:{number:03d}:{part:02d}:" + "r" * 100 + "雪\\\"\n"
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
    pretty_frame = json.dumps({
        "jsonrpc": "2.0", "id": request_id,
        "result": {"content": [{"type": "text", "text": json.dumps(
            preview, ensure_ascii=False, indent=2,
        )}]},
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(pretty_frame) > 224 * 1024
    expected_http = json.dumps({**preview, "request_id": "req-" + "x" * 12},
                               ensure_ascii=False).encode("utf-8")
    assert len(expected_http) == stale_artifact_cleanup.cleanup_response_wire_bytes(
        preview,
    )["http"]
    http_handler = object.__new__(governance_server.GovernanceHandler)
    http_handler.wfile = io.BytesIO()
    http_handler.send_response = lambda _code: None
    http_handler.send_header = lambda _name, _value: None
    http_handler.end_headers = lambda: None
    http_handler._respond(200, {**preview, "request_id": "req-" + "x" * 12})
    assert http_handler.wfile.getvalue() == expected_http
    assert len(expected_http) <= 224 * 1024
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
    assert len(managed_frame) == stale_artifact_cleanup.cleanup_response_wire_bytes(
        preview,
    )["managed_mcp"]
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
    assert len(standalone_frame) == stale_artifact_cleanup.cleanup_response_wire_bytes(
        preview,
    )["standalone_mcp"]
    assert json.loads(json.loads(standalone_frame)["result"]["content"][0]["text"]) == preview
    for source, shown in zip(raw["candidates"], preview["candidates"]):
        for key in ("candidate_id", "snapshot_id", "artifact_type", "action",
                    "safe_to_apply", "refusal_reasons"):
            assert shown[key] == source[key]
        assert shown["evidence"]["created_at"] == source["evidence"]["created_at"]
        assert shown["evidence"]["size_bytes"] == source["evidence"]["size_bytes"]
        assert shown["evidence"]["size_bytes_status"] == source["evidence"]["size_bytes_status"]
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
    candidates = sorted((item for item in preview["candidates"]
                         if item["artifact_type"] == "batch_worktree"),
                        key=lambda item: item["candidate_id"])
    assert len(candidates) == 2
    monkeypatch.setattr(stale_artifact_cleanup, "_remove_worktree",
                        lambda **_kwargs: pytest.fail("bare worktree removal reached"))
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError) as err:
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "proj", repo_root_path=repo, dimension="worktrees",
            candidate_ids=[item["candidate_id"] for item in candidates],
            plan_hash=preview["plan_hash"], plan_revision=preview["plan_revision"],
        )
    payload = err.value.payload
    assert payload["error"] == "unsafe_stale_artifact_cleanup_refused"
    assert payload["unsafe_candidate_count"] == 2
    assert Path(candidates[0]["path"]).exists()
    assert Path(candidates[1]["path"]).exists()


def test_later_item_active_backlog_drift_stops_further_deletion(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    conn = _conn()
    _terminal_batch_with_worktree(conn, repo, batch_id="drift-a")
    _terminal_batch_with_worktree(conn, repo, batch_id="drift-b")
    preview = stale_artifact_cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=repo, dimension="worktrees",
    )
    candidates = sorted((item for item in preview["candidates"] if item["artifact_type"] == "batch_worktree"),
                        key=lambda item: item["candidate_id"])
    assert len(candidates) == 2
    _insert_backlog_ref(conn, bug_id="OPT-LATE-LEASE",
                        worktree_path=candidates[1]["path"], status="OPEN")
    monkeypatch.setattr(stale_artifact_cleanup, "_remove_worktree",
                        lambda **_kwargs: pytest.fail("bare worktree removal reached"))
    with pytest.raises(stale_artifact_cleanup.StaleArtifactCleanupError,
                       match="stale_cleanup_plan_refused"):
        stale_artifact_cleanup.apply_stale_artifact_cleanup(
            conn, "proj", repo_root_path=repo, dimension="worktrees",
            candidate_ids=[item["candidate_id"] for item in candidates],
            plan_hash=preview["plan_hash"], plan_revision=preview["plan_revision"],
        )
    assert Path(candidates[0]["path"]).exists()
    assert Path(candidates[1]["path"]).exists()
    assert not conn.execute(
        "SELECT 1 FROM task_timeline_events WHERE event_type='governance.stale_artifact_cleanup.apply'"
    ).fetchone()
