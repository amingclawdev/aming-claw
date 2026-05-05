"""Tests for auto_backlog_bridge.file_cluster_as_backlog (CR3 R4).

Covers >=4 test functions per AC7:
    test_file_cluster_as_backlog (bug_id format)
    test_metadata_operation_type_reconcile_cluster
    test_terminal_success_hook
    test_terminal_withdrawn_hook
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

# Repo root for `agent.governance...` imports
_REPO_ROOT = _AGENT_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from governance import auto_backlog_bridge as bridge  # noqa: E402
from governance import reconcile_deferred_queue as q  # noqa: E402
from governance.db import _configure_connection, _ensure_schema  # noqa: E402


PROJECT_ID = "p-test"


# ---------------------------------------------------------------------------
# Mock HTTP client
# ---------------------------------------------------------------------------


class MockHttpClient:
    def __init__(self, create_response: dict | None = None, active_session: dict | None = None):
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []
        self.create_response = create_response or {"task_id": "t-cluster-1"}
        self.active_session = active_session

    def get(self, url: str) -> dict:
        self.gets.append(url)
        if url.endswith("/sessions/active"):
            return {"session": self.active_session}
        if "/list" in url:
            return {"tasks": [], "count": 0}
        if "/exists" in url:
            return {"exists": False}
        return {}

    def post(self, url: str, payload: dict) -> dict:
        self.posts.append((url, payload))
        if url.endswith("/batch-memory"):
            return {
                "batch": {
                    "batch_id": payload.get("batch_id", ""),
                    "session_id": payload.get("session_id", ""),
                    "memory": payload.get("initial_memory", {}),
                }
            }
        return dict(self.create_response)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "gov.db"
    c = sqlite3.connect(str(db_path))
    _configure_connection(c, busy_timeout=2000)
    _ensure_schema(c)
    q.ensure_schema(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def isolate_reconcile_session_overlay(tmp_path: Path, monkeypatch):
    """Keep auto-started reconcile sessions from touching the live repo overlay."""
    from governance import reconcile_session as rs

    real_start = rs.start_session
    gov_dir = tmp_path / "session-governance"

    def _start_with_tmp(c, pid, **kw):
        kw.setdefault("governance_dir", gov_dir)
        return real_start(c, pid, **kw)

    monkeypatch.setattr(rs, "start_session", _start_with_tmp)


# ---------------------------------------------------------------------------
# AC7 — file_cluster_as_backlog tests
# ---------------------------------------------------------------------------


def test_file_cluster_as_backlog():
    """bug_id format: OPT-BACKLOG-RECONCILE-{run[:8]}-CLUSTER-{fp[:8]}-{slug}."""
    mock = MockHttpClient()
    cluster_group = {
        "cluster_fingerprint": "abc12345fpoffset",
        "slug": "Drift Cleanup",
        "primary_files": ["agent/foo.py", "agent/bar.py"],
        "prompt": "fix cluster",
    }
    cluster_report = {"purpose": "Drift cleanup", "expected_doc_sections": ["docs/api/x.md"]}
    out = bridge.file_cluster_as_backlog(
        cluster_group, cluster_report, run_id="run-deadbeefcafe",
        project_id=PROJECT_ID, http_client=mock,
    )
    assert out["filed"] is True
    assert out["task_id"] == "t-cluster-1"
    bug_id = out["backlog_id"]
    # AC: starts with the canonical reconcile prefix
    assert bug_id.startswith("OPT-BACKLOG-RECONCILE-")
    # AC: contains -CLUSTER- segment
    assert "-CLUSTER-" in bug_id
    # First 8 chars of run_id present
    assert "run-dead"[:8] in bug_id
    # First 8 chars of fingerprint present
    assert "abc12345" in bug_id
    # Slug appears (lowercased, kebab)
    assert "drift-cleanup" in bug_id


def test_metadata_operation_type_reconcile_cluster():
    """Posted task body has type='pm' AND metadata.operation_type='reconcile-cluster'."""
    mock = MockHttpClient()
    cluster_group = {"cluster_fingerprint": "fp-meta01", "slug": "MetaTest",
                     "primary_files": ["a.py"]}
    cluster_report = {"purpose": "p"}
    out = bridge.file_cluster_as_backlog(
        cluster_group, cluster_report, run_id="run-12345678",
        project_id=PROJECT_ID, http_client=mock,
    )
    assert out["filed"] is True
    assert len(mock.posts) == 3
    batch_url, batch_body = mock.posts[0]
    assert batch_url == f"/api/reconcile/{PROJECT_ID}/batch-memory"
    assert batch_body["batch_id"] == "run-12345678"
    backlog_url, backlog_body = mock.posts[1]
    assert backlog_url.startswith(f"/api/backlog/{PROJECT_ID}/")
    assert backlog_body["status"] == "OPEN"
    assert backlog_body["force_admit"] is True
    url, body = mock.posts[2]
    assert url == f"/api/task/{PROJECT_ID}/create"
    assert body["type"] == "pm"
    md = body["metadata"]
    assert md["operation_type"] == "reconcile-cluster"
    assert md["cluster_fingerprint"] == "fp-meta01"
    assert md["cluster_payload"] == cluster_group
    assert md["cluster_report"] == cluster_report
    assert md["target_files"] == ["a.py"]
    assert md["test_files"] == []
    assert md["batch_id"] == "run-12345678"
    assert md["reconcile_batch_id"] == "run-12345678"
    assert md["bug_id"].startswith("OPT-BACKLOG-RECONCILE-")
    assert "-CLUSTER-" in md["bug_id"]


def test_cluster_task_metadata_carries_reconcile_target_branch():
    """Cluster PM tasks inherit active session branch/base provenance."""
    mock = MockHttpClient(active_session={
        "session_id": "sess-branch1",
        "target_branch": "reconcile/p-test-sess-branch1",
        "base_commit_sha": "base123",
        "target_head_sha": "head123",
    })
    cluster_group = {"cluster_fingerprint": "fp-branch1", "slug": "BranchTest",
                     "primary_files": ["agent/a.py"]}
    cluster_report = {"purpose": "p"}
    out = bridge.file_cluster_as_backlog(
        cluster_group, cluster_report, run_id="run-branch1",
        project_id=PROJECT_ID, http_client=mock,
    )
    assert out["filed"] is True
    assert f"/api/reconcile/{PROJECT_ID}/sessions/active" in mock.gets
    batch_url, batch_body = mock.posts[0]
    assert batch_url == f"/api/reconcile/{PROJECT_ID}/batch-memory"
    assert batch_body["session_id"] == "sess-branch1"
    _, backlog_body = mock.posts[1]
    trigger = backlog_body["chain_trigger_json"]
    assert trigger["reconcile_session_id"] == "sess-branch1"
    assert trigger["reconcile_target_branch"] == "reconcile/p-test-sess-branch1"
    _, task_body = mock.posts[2]
    md = task_body["metadata"]
    assert md["session_id"] == "sess-branch1"
    assert md["reconcile_session_id"] == "sess-branch1"
    assert md["reconcile_target_branch"] == "reconcile/p-test-sess-branch1"
    assert md["reconcile_target_base_commit"] == "base123"
    assert md["reconcile_target_head"] == "head123"


def test_cluster_task_metadata_carries_expected_test_files():
    """PM executor receives target/test files from metadata, not task body only."""
    mock = MockHttpClient()
    cluster_group = {"cluster_fingerprint": "fp-tests1", "primary_files": ["agent/a.py"]}
    cluster_report = {"expected_test_files": ["agent/tests/test_a.py"]}
    out = bridge.file_cluster_as_backlog(
        cluster_group, cluster_report, run_id="run-tests1",
        project_id=PROJECT_ID, http_client=mock,
    )
    assert out["filed"] is True
    _, body = mock.posts[2]
    assert body["target_files"] == ["agent/a.py"]
    assert body["metadata"]["target_files"] == ["agent/a.py"]
    assert body["metadata"]["test_files"] == ["agent/tests/test_a.py"]


def test_terminal_success_hook(conn):
    """auto_chain hook: merge succeeded -> mark_terminal('resolved', 'merged@<sha>')."""
    from governance import auto_chain

    q.enqueue_or_lookup(PROJECT_ID, "fp-suc1", payload={"x": 1},
                        run_id="run-1", conn=conn)
    sess = conn.execute(
        "SELECT session_id, target_branch FROM reconcile_sessions WHERE project_id=?",
        (PROJECT_ID,),
    ).fetchone()
    target_branch = sess["target_branch"] or "reconcile/p-test-sess-branch"
    conn.execute(
        "UPDATE reconcile_sessions SET target_branch='' WHERE project_id=? AND session_id=?",
        (PROJECT_ID, sess["session_id"]),
    )
    metadata = {"operation_type": "reconcile-cluster",
                "cluster_fingerprint": "fp-suc1",
                "chain_id": "task-root-1",
                "reconcile_session_id": sess["session_id"],
                "reconcile_target_branch": target_branch}
    auto_chain._reconcile_cluster_terminal_hook(
        conn, PROJECT_ID, "task-merge-1", "merge", "succeeded",
        {"merge_commit": "abc1234"}, metadata,
    )
    row = conn.execute(
        "SELECT status, terminal_reason FROM reconcile_deferred_clusters "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (PROJECT_ID, "fp-suc1"),
    ).fetchone()
    assert row[0] == "resolved"
    assert "abc1234" in (row[1] or "")
    sess_after = conn.execute(
        "SELECT target_branch, target_head_sha FROM reconcile_sessions WHERE project_id=?",
        (PROJECT_ID,),
    ).fetchone()
    assert sess_after["target_branch"] == target_branch
    assert sess_after["target_head_sha"] == "abc1234"


def test_terminal_withdrawn_hook(conn):
    """observer cancel -> mark_terminal('skipped', cancel_reason)."""
    from governance import auto_chain

    q.enqueue_or_lookup(PROJECT_ID, "fp-wd1", payload={}, run_id="r", conn=conn)
    metadata = {"operation_type": "reconcile-cluster",
                "cluster_fingerprint": "fp-wd1",
                "cancel_reason": "observer_withdraw"}
    auto_chain._reconcile_cluster_terminal_hook(
        conn, PROJECT_ID, "task-w-1", "pm", "cancelled", {}, metadata,
    )
    row = conn.execute(
        "SELECT status, skipped_reason FROM reconcile_deferred_clusters "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (PROJECT_ID, "fp-wd1"),
    ).fetchone()
    assert row[0] == "skipped"
    assert row[1] == "observer_withdraw"


def test_stale_duplicate_child_cancel_does_not_skip_cluster(conn):
    """Cancelling a non-current duplicate child must not terminalize the cluster."""
    from governance import auto_chain

    bug_id = "OPT-BACKLOG-RECONCILE-test-stale-child"
    q.enqueue_or_lookup(PROJECT_ID, "fp-stale-child", payload={}, run_id="r", conn=conn)
    q.mark_filing(PROJECT_ID, "fp-stale-child", conn=conn)
    q.mark_in_chain(
        PROJECT_ID,
        "fp-stale-child",
        "task-root",
        bug_id=bug_id,
        conn=conn,
    )
    conn.execute(
        "INSERT INTO backlog_bugs "
        "(bug_id, title, status, current_task_id, root_task_id, created_at, updated_at) "
        "VALUES (?, ?, 'OPEN', ?, ?, '2026-05-04T00:00:00Z', '2026-05-04T00:00:00Z')",
        (bug_id, "cluster stale child guard", "task-real-dev", "task-root"),
    )
    conn.commit()
    metadata = {
        "operation_type": "reconcile-cluster",
        "cluster_fingerprint": "fp-stale-child",
        "bug_id": bug_id,
        "chain_id": "task-root",
    }

    out = auto_chain._reconcile_cluster_terminal_hook(
        conn, PROJECT_ID, "task-duplicate-dev", "dev", "cancelled", {}, metadata,
    )
    row = conn.execute(
        "SELECT status, last_terminal_status, skipped_reason "
        "FROM reconcile_deferred_clusters "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (PROJECT_ID, "fp-stale-child"),
    ).fetchone()

    assert out["hook"] == "reconcile_cluster_stale_terminal_ignored"
    assert row[0] == "in_chain"
    assert row[1] is None
    assert row[2] is None


def test_on_task_failed_terminal_updates_deferred_queue(conn):
    """Terminal root failure must move reconcile deferred row out of in_chain."""
    from governance import auto_chain
    from governance.task_registry import create_task

    q.enqueue_or_lookup(PROJECT_ID, "fp-fail1", payload={}, run_id="r", conn=conn)
    q.mark_filing(PROJECT_ID, "fp-fail1", conn=conn)
    metadata = {
        "operation_type": "reconcile-cluster",
        "cluster_fingerprint": "fp-fail1",
    }
    task = create_task(
        conn,
        PROJECT_ID,
        "pm failed",
        task_type="pm",
        metadata=metadata,
        max_attempts=1,
    )
    conn.execute(
        "UPDATE tasks SET status = 'failed', execution_status = 'failed' "
        "WHERE task_id = ?",
        (task["task_id"],),
    )
    q.mark_in_chain(PROJECT_ID, "fp-fail1", task["task_id"], conn=conn)

    with mock.patch(
        "governance.auto_chain._maybe_create_workflow_improvement_task",
        return_value=None,
    ):
        auto_chain.on_task_failed(
            conn,
            PROJECT_ID,
            task["task_id"],
            "pm",
            result={"error": "executor_crash_recovery"},
            metadata=metadata,
            reason="executor crashed",
        )

    row = conn.execute(
        "SELECT status, retry_count, terminal_reason FROM reconcile_deferred_clusters "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (PROJECT_ID, "fp-fail1"),
    ).fetchone()
    assert row[0] == "failed_retryable"
    assert row[1] == 1
    assert "executor_crash_recovery" in row[2]


def test_compose_cluster_bug_id_format():
    """Direct unit test on compose_cluster_bug_id helper."""
    bid = bridge.compose_cluster_bug_id(
        run_id="abcdefghijkl", cluster_fingerprint="ZYXWVUTS999",
        slug_hint="Some Title",
    )
    assert bid.startswith("OPT-BACKLOG-RECONCILE-abcdefgh-CLUSTER-ZYXWVUTS-")
    assert "some-title" in bid


def test_unauthorized_creator_skipped():
    """Disallowed creator returns skipped=True without POSTing."""
    mock = MockHttpClient()
    out = bridge.file_cluster_as_backlog(
        {"cluster_fingerprint": "fp"}, {}, "r", PROJECT_ID,
        creator="hacker", http_client=mock,
    )
    assert out["filed"] is False
    assert out["skipped"] is True
    assert mock.posts == []


def test_file_cluster_upserts_backlog_before_task_create():
    mock = MockHttpClient()
    cluster_group = {"cluster_fingerprint": "fp-audit1", "slug": "Audit Path",
                     "primary_files": ["agent/a.py"]}
    out = bridge.file_cluster_as_backlog(
        cluster_group, {}, "run-audit1", PROJECT_ID, http_client=mock,
    )
    assert out["filed"] is True
    assert len(mock.posts) == 3
    assert mock.posts[0][0] == f"/api/reconcile/{PROJECT_ID}/batch-memory"
    assert mock.posts[1][0].startswith(f"/api/backlog/{PROJECT_ID}/")
    assert mock.posts[2][0] == f"/api/task/{PROJECT_ID}/create"
    assert mock.posts[1][1]["chain_trigger_json"]["cluster_fingerprint"] == "fp-audit1"


def test_file_cluster_stops_when_backlog_upsert_fails():
    class FailingBacklogClient(MockHttpClient):
        def post(self, url: str, payload: dict) -> dict:
            self.posts.append((url, payload))
            if url.startswith(f"/api/backlog/{PROJECT_ID}/"):
                raise RuntimeError("backlog gate refused")
            return {"task_id": "should-not-create"}

    mock = FailingBacklogClient()
    out = bridge.file_cluster_as_backlog(
        {"cluster_fingerprint": "fp-fail-backlog", "primary_files": ["a.py"]},
        {},
        "run-fail",
        PROJECT_ID,
        http_client=mock,
    )
    assert out["filed"] is False
    assert out["reason"].startswith("backlog_upsert_failed:")
    assert len(mock.posts) == 2
    assert mock.posts[0][0] == f"/api/reconcile/{PROJECT_ID}/batch-memory"
