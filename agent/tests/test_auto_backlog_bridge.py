"""Tests for Phase Z v2 PR4 — auto_backlog_bridge.

Covers compose_bug_id format, simple plan filing, duplicate-suffix walk,
invalid-creator rejection, dry-run behaviour, and queue-full deferral.
All tests use a mock http_client; no real network is contacted.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

import pytest

# Ensure agent package importable
_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from agent.governance.auto_backlog_bridge import (  # noqa: E402
    compose_bug_id,
    file_remediation_plan,
)


# ---------------------------------------------------------------------------
# Mock HTTP client
# ---------------------------------------------------------------------------


class MockHttpClient:
    """Captures get/post calls and returns scripted responses.

    Defaults:
      * GET /api/task/{pid}/list?limit=200 → 0 active tasks
      * GET /api/backlog/{pid}/exists?bug_id=... → exists=False
      * POST /api/task/{pid}/create → {task_id: t-<n>}
    """

    def __init__(
        self,
        active_tasks: int = 0,
        existing_bug_ids: set[str] | None = None,
        all_bug_ids_exist: bool = False,
        create_response: dict | None = None,
    ):
        self.active_tasks = active_tasks
        self.existing_bug_ids = set(existing_bug_ids or set())
        self.all_bug_ids_exist = all_bug_ids_exist
        self.create_response = create_response
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []
        self._task_counter = 0

    def get(self, url: str) -> dict:
        self.gets.append(url)
        if "/list" in url:
            return {
                "tasks": [
                    {"task_id": f"q{i}", "status": "pending"}
                    for i in range(self.active_tasks)
                ],
                "count": self.active_tasks,
            }
        if "/exists" in url:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(url).query)
            bug_id = (qs.get("bug_id") or [""])[0]
            if self.all_bug_ids_exist:
                return {"exists": True}
            return {"exists": bug_id in self.existing_bug_ids}
        return {}

    def post(self, url: str, payload: dict) -> dict:
        self.posts.append((url, payload))
        if self.create_response is not None:
            return dict(self.create_response)
        self._task_counter += 1
        return {"task_id": f"t-{self._task_counter}"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_compose_bug_id_format():
    """AC2: bug_id format truncates run_id to 8 chars and slugs target_node."""
    assert (
        compose_bug_id("run-12345678", "unmap_file", "L7.21")
        == "OPT-BACKLOG-RECONCILE-run-1234-unmap_file-l7-21"
    )
    # Empty run_id → empty fragment
    bid = compose_bug_id("", "x", "L1.0")
    assert bid.startswith("OPT-BACKLOG-RECONCILE--x-")
    # Special chars in node fall back through slug regex
    assert compose_bug_id("abcdefgh", "rename", "Foo/Bar.baz") == (
        "OPT-BACKLOG-RECONCILE-abcdefgh-rename-foo-bar-baz"
    )


def test_file_simple_plan_creates_tasks():
    """AC3: simple plan files one reconcile task with required metadata."""
    mock = MockHttpClient()
    plan = {
        "plan_id": "p1",
        "actions": [
            {
                "action": "unmap_file",
                "target_node": "L7.21",
                "params": {"files": ["a.py"], "drift_type": "unmapped_file"},
            }
        ],
    }
    out = file_remediation_plan(
        plan,
        run_id="run-abcdef01",
        project_id="aming-claw",
        http_client=mock,
    )
    assert set(out.keys()) >= {"filed", "skipped", "errors", "task_ids", "planned"}
    assert out["filed"] == 1
    assert out["skipped"] == 0
    assert out["errors"] == []
    assert out["task_ids"] == ["t-1"]
    # planned should be None outside dry_run
    assert out["planned"] is None

    assert len(mock.posts) == 1
    url, payload = mock.posts[0]
    assert url == "/api/task/aming-claw/create"
    assert payload["type"] == "reconcile"
    assert payload["target_files"] == ["a.py"]
    md = payload["metadata"]
    assert md["reconcile_run_id"] == "run-abcdef01"
    assert md["drift_type"] == "unmapped_file"
    assert md["plan_id"] == "p1"
    assert md["action_index"] == 0
    assert md["bug_id"] == "OPT-BACKLOG-RECONCILE-run-abcd-unmap_file-l7-21"


def test_duplicate_bug_id_appends_suffix():
    """AC4: when first bug_id exists, suffix walks -2 .. -10 then errors out."""
    base = "OPT-BACKLOG-RECONCILE-run-abcd-unmap_file-l7-21"

    # Case 1: only the bare bug_id exists → second filing uses -2.
    mock = MockHttpClient(existing_bug_ids={base})
    plan = {
        "plan_id": "p1",
        "actions": [
            {
                "action": "unmap_file",
                "target_node": "L7.21",
                "params": {"files": ["a.py"]},
            }
        ],
    }
    out = file_remediation_plan(
        plan, run_id="run-abcdef01", project_id="aming-claw", http_client=mock
    )
    assert out["filed"] == 1
    assert out["errors"] == []
    md = mock.posts[0][1]["metadata"]
    assert md["bug_id"] == f"{base}-2"

    # Case 2: every bug_id exists → walks past -10, records error, no create.
    mock2 = MockHttpClient(all_bug_ids_exist=True)
    out2 = file_remediation_plan(
        plan, run_id="run-abcdef01", project_id="aming-claw", http_client=mock2
    )
    assert out2["filed"] == 0
    assert mock2.posts == []
    assert len(out2["errors"]) == 1
    reason = out2["errors"][0]["reason"].lower()
    assert "duplicate" in reason or "collision" in reason


def test_invalid_creator_rejected():
    """AC5: disallowed creator rejected; allowed creators (incl. observer-*) accepted."""
    mock = MockHttpClient()
    plan = {
        "plan_id": "p",
        "actions": [
            {
                "action": "unmap_file",
                "target_node": "L1.0",
                "params": {"files": ["x.py"]},
            }
        ],
    }
    out = file_remediation_plan(
        plan,
        run_id="run-1",
        project_id="aming-claw",
        creator="hacker",
        http_client=mock,
    )
    assert out["filed"] == 0
    assert mock.posts == []
    assert len(out["errors"]) == 1
    reason = out["errors"][0]["reason"].lower()
    assert "creator" in reason or "unauthorized_creator" in reason

    # observer-* is allowed
    mock_ok = MockHttpClient()
    out_ok = file_remediation_plan(
        plan, run_id="run-1", project_id="aming-claw",
        creator="observer-z3", http_client=mock_ok,
    )
    assert out_ok["filed"] == 1
    assert out_ok["errors"] == []

    # explicit allowlist members
    for c in ("reconcile-bridge", "coordinator", "auto-approval-bot"):
        m = MockHttpClient()
        r = file_remediation_plan(
            plan, run_id="run-1", project_id="aming-claw",
            creator=c, http_client=m,
        )
        assert r["filed"] == 1, f"{c} should be allowed"


def test_dry_run_no_creates():
    """AC6: dry_run returns planned[] and never posts."""
    mock = MockHttpClient()
    plan = {
        "plan_id": "p1",
        "actions": [
            {
                "action": "unmap_file",
                "target_node": "L7.21",
                "params": {"files": ["a.py"], "drift_type": "unmapped_file"},
            },
            {
                "action": "rename_node",
                "target_node": "L8.5",
                "params": {"files": ["b.py", "c.py"]},
            },
        ],
    }
    out = file_remediation_plan(
        plan,
        run_id="run-abcdef01",
        project_id="aming-claw",
        dry_run=True,
        http_client=mock,
    )
    assert out["task_ids"] == []
    assert out["filed"] == 0
    assert isinstance(out["planned"], list) and len(out["planned"]) == 2
    for entry in out["planned"]:
        assert "bug_id" in entry
        assert "action_index" in entry
        assert "target_node" in entry
        assert "target_files" in entry
    # No POSTs were issued
    assert mock.posts == []


def test_queue_full_defers(caplog):
    """AC7: when active >= queue_threshold, every action is skipped and 'queue_full' logged."""
    mock = MockHttpClient(active_tasks=100)
    actions = [
        {"action": "unmap_file", "target_node": f"L7.{i}", "params": {"files": [f"f{i}.py"]}}
        for i in range(3)
    ]
    plan = {"plan_id": "p1", "actions": actions}

    with caplog.at_level(logging.WARNING, logger="agent.governance.auto_backlog_bridge"):
        out = file_remediation_plan(
            plan,
            run_id="run-1",
            project_id="aming-claw",
            queue_threshold=50,
            http_client=mock,
        )

    assert out["filed"] == 0
    assert out["skipped"] == len(actions)
    assert out["errors"] == []
    assert mock.posts == []
    assert any("queue_full" in rec.getMessage() for rec in caplog.records)
