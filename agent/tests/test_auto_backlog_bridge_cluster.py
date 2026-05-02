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
    def __init__(self, create_response: dict | None = None):
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []
        self.create_response = create_response or {"task_id": "t-cluster-1"}

    def get(self, url: str) -> dict:
        self.gets.append(url)
        if "/list" in url:
            return {"tasks": [], "count": 0}
        if "/exists" in url:
            return {"exists": False}
        return {}

    def post(self, url: str, payload: dict) -> dict:
        self.posts.append((url, payload))
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
    assert len(mock.posts) == 1
    url, body = mock.posts[0]
    assert url == f"/api/task/{PROJECT_ID}/create"
    assert body["type"] == "pm"
    md = body["metadata"]
    assert md["operation_type"] == "reconcile-cluster"
    assert md["cluster_fingerprint"] == "fp-meta01"
    assert md["cluster_payload"] == cluster_group
    assert md["cluster_report"] == cluster_report
    assert md["bug_id"].startswith("OPT-BACKLOG-RECONCILE-")
    assert "-CLUSTER-" in md["bug_id"]


def test_terminal_success_hook(conn):
    """auto_chain hook: merge succeeded -> mark_terminal('resolved', 'merged@<sha>')."""
    from governance import auto_chain

    q.enqueue_or_lookup(PROJECT_ID, "fp-suc1", payload={"x": 1},
                        run_id="run-1", conn=conn)
    metadata = {"operation_type": "reconcile-cluster",
                "cluster_fingerprint": "fp-suc1",
                "chain_id": "task-root-1"}
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
