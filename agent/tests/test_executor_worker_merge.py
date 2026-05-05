"""Tests for merge handler pre-merged detection (OPT-BACKLOG-MERGE-D6-EXPLICIT-FLAG).

Covers AC1-AC5 from the PRD:
  AC1: HEAD==chain_version + changed_files in HEAD commit → success, pre_merged
  AC2: HEAD==chain_version + metadata.pre_merged=True → success, pre_merged
  AC3: HEAD!=chain_version (existing D6 path) → success, pre_merged
  AC4: metadata._already_merged or ._merge_commit → success (existing explicit path)
  AC5: HEAD==chain_version + changed_files NOT in HEAD + no pre_merged → failed
"""

import json
import subprocess
import types
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helper: build a minimal ExecutorWorker-like object with _execute_merge
# ---------------------------------------------------------------------------

def _make_worker(workspace="/tmp/ws", base_url="http://localhost:40000", project_id="test"):
    """Return a lightweight stub that has _execute_merge bound."""
    # Import the real module to get _execute_merge's code
    import importlib
    import sys
    import os

    # We need access to the real _execute_merge method.  Rather than
    # instantiating a full ExecutorWorker (which requires MCP connections,
    # PID files, etc.) we build a thin namespace and bind the method.
    mod_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "executor_worker.py",
    )
    spec = importlib.util.spec_from_file_location("executor_worker", mod_path)
    mod = importlib.util.module_from_spec(spec)

    # Provide stubs for heavy dependencies that import-time code may need
    for dep in ("mcp", "mcp.server", "mcp.server.fastmcp", "mcp.types"):
        if dep not in sys.modules:
            sys.modules[dep] = types.ModuleType(dep)
    if not hasattr(sys.modules["mcp.server.fastmcp"], "FastMCP"):
        sys.modules["mcp.server.fastmcp"].FastMCP = type("FastMCP", (), {"tool": lambda *a, **k: (lambda f: f)})

    spec.loader.exec_module(mod)

    worker = object.__new__(mod.ExecutorWorker)
    worker.workspace = workspace
    worker.base_url = base_url
    worker.project_id = project_id
    worker._report_progress = lambda tid, data: None
    return worker


# ---------------------------------------------------------------------------
# Subprocess / urllib mocking helpers
# ---------------------------------------------------------------------------

def _mock_subprocess_run(head_rev="abc1234", head_files=None):
    """Return a side_effect function for subprocess.run.

    - ``git rev-parse --short HEAD`` → head_rev
    - ``git log -1 --name-only ...`` → head_files (list of filenames)
    """
    head_files = head_files or []

    def _side_effect(cmd, **kwargs):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        result = subprocess.CompletedProcess(cmd, 0)
        if "rev-parse" in cmd_str and "--short" in cmd_str:
            result.stdout = head_rev + "\n"
            result.stderr = ""
        elif "log" in cmd_str and "--name-only" in cmd_str:
            result.stdout = "\n".join(head_files) + "\n"
            result.stderr = ""
        else:
            result.stdout = ""
            result.stderr = ""
        return result

    return _side_effect


def _mock_urlopen(chain_version):
    """Return a context-less urlopen replacement returning a version-check response."""
    body = json.dumps({"chain_version": chain_version}).encode()

    def _urlopen(url, **kwargs):
        resp = types.SimpleNamespace()
        resp.read = lambda: body
        return resp

    return _urlopen


# ===========================================================================
# AC1: HEAD == chain_version AND all changed_files in HEAD commit
# ===========================================================================

def test_ac1_head_eq_chain_version_changed_files_in_head():
    """When HEAD matches chain_version and changed_files appear in git log,
    the merge handler returns success with pre_merged=True and merge_commit."""
    worker = _make_worker()
    metadata = {
        "parent_task_id": "task-parent-1",
        "changed_files": ["agent/executor_worker.py", "agent/tests/test_foo.py"],
    }
    with mock.patch("subprocess.run", side_effect=_mock_subprocess_run(
        head_rev="abc1234",
        head_files=["agent/executor_worker.py", "agent/tests/test_foo.py", "docs/readme.md"],
    )), mock.patch("urllib.request.urlopen", side_effect=_mock_urlopen("abc1234")):
        result = worker._execute_merge("task-merge-1", metadata)

    assert result["status"] == "succeeded"
    assert result["result"]["pre_merged"] is True
    assert result["result"]["merge_commit"] == "abc1234"
    assert result["result"]["changed_files"] == metadata["changed_files"]


# ===========================================================================
# AC2: HEAD == chain_version AND metadata.pre_merged is True
# ===========================================================================

def test_ac2_explicit_pre_merged_flag():
    """When metadata contains pre_merged=True, merge handler returns success
    regardless of changed_files content."""
    worker = _make_worker()
    metadata = {
        "parent_task_id": "task-parent-2",
        "changed_files": ["nonexistent/file.py"],
        "pre_merged": True,
    }
    with mock.patch("subprocess.run", side_effect=_mock_subprocess_run(
        head_rev="def5678",
        head_files=[],  # changed_files NOT in HEAD — doesn't matter
    )), mock.patch("urllib.request.urlopen", side_effect=_mock_urlopen("def5678")):
        result = worker._execute_merge("task-merge-2", metadata)

    assert result["status"] == "succeeded"
    assert result["result"]["pre_merged"] is True


# ===========================================================================
# AC3: HEAD != chain_version (existing D6 path) → pre_merged success
# ===========================================================================

def test_ac3_head_ahead_of_chain_version():
    """Existing D6 path: HEAD differs from chain_version → pre-merged."""
    worker = _make_worker()
    metadata = {
        "parent_task_id": "task-parent-3",
        "changed_files": ["some/file.py"],
    }
    with mock.patch("subprocess.run", side_effect=_mock_subprocess_run(
        head_rev="aaa1111",
    )), mock.patch("urllib.request.urlopen", side_effect=_mock_urlopen("bbb2222")):
        result = worker._execute_merge("task-merge-3", metadata)

    assert result["status"] == "succeeded"
    assert result["result"]["pre_merged"] is True
    assert result["result"]["merge_commit"] == "aaa1111"


# ===========================================================================
# AC4: metadata._already_merged or ._merge_commit (existing explicit path)
# ===========================================================================

def test_ac4_already_merged_explicit_flag():
    """Existing explicit path: _already_merged metadata → success."""
    worker = _make_worker()
    metadata = {
        "parent_task_id": "task-parent-4",
        "changed_files": ["x.py"],
        "_already_merged": True,
    }
    with mock.patch("subprocess.run", side_effect=_mock_subprocess_run(head_rev="ccc3333")):
        result = worker._execute_merge("task-merge-4", metadata)

    assert result["status"] == "succeeded"
    assert result["result"]["pre_merged"] is True
    assert result["result"]["merge_commit"] == "ccc3333"


def test_ac4_merge_commit_explicit_flag():
    """Existing explicit path: _merge_commit metadata → success with that commit."""
    worker = _make_worker()
    metadata = {
        "parent_task_id": "task-parent-4b",
        "changed_files": ["y.py"],
        "_merge_commit": "explicit123",
    }
    with mock.patch("subprocess.run", side_effect=_mock_subprocess_run(head_rev="ddd4444")):
        result = worker._execute_merge("task-merge-4b", metadata)

    assert result["status"] == "succeeded"
    assert result["result"]["merge_commit"] == "explicit123"
    assert result["result"]["pre_merged"] is True


# ===========================================================================
# AC5: HEAD == chain_version AND changed_files NOT in HEAD AND no pre_merged
# ===========================================================================

def test_ac5_head_eq_chain_version_files_not_in_head():
    """When HEAD==chain_version, changed_files not in HEAD, and no pre_merged flag,
    the handler must return failure with 'no isolated merge metadata'."""
    worker = _make_worker()
    metadata = {
        "parent_task_id": "task-parent-5",
        "changed_files": ["agent/missing_file.py"],
    }
    with mock.patch("subprocess.run", side_effect=_mock_subprocess_run(
        head_rev="eee5555",
        head_files=["totally/different.py"],
    )), mock.patch("urllib.request.urlopen", side_effect=_mock_urlopen("eee5555")):
        result = worker._execute_merge("task-merge-5", metadata)

    assert result["status"] == "failed"
    assert "no isolated merge metadata" in result["error"].lower() or \
           "no isolated" in result["error"].lower()


def test_ac5_head_eq_chain_version_no_changed_files():
    """Edge case: HEAD==chain_version, empty changed_files, no pre_merged flag
    → should still return the 'no isolated merge metadata' error."""
    worker = _make_worker()
    metadata = {
        "parent_task_id": "task-parent-5b",
        "changed_files": [],
    }
    with mock.patch("subprocess.run", side_effect=_mock_subprocess_run(
        head_rev="fff6666",
    )), mock.patch("urllib.request.urlopen", side_effect=_mock_urlopen("fff6666")):
        result = worker._execute_merge("task-merge-5b", metadata)

    assert result["status"] == "failed"
    assert "no isolated" in result["error"].lower()


def test_noop_worktree_branch_already_ancestor_returns_success(tmp_path):
    """A no-op dev worktree can be reused after observer/runtime recovery.

    If its branch is already an ancestor of HEAD and there are no staged
    changes, merge should be idempotent instead of trying an isolated merge.
    """
    worker = _make_worker(workspace=str(tmp_path))
    worker._branch_exists = lambda branch: True
    worker._branch_already_merged = lambda branch: True
    worker._remove_worktree = mock.Mock()
    worktree = tmp_path / "dev-task"
    worktree.mkdir()

    metadata = {
        "parent_task_id": "task-parent-6",
        "_branch": "dev/task-parent-6",
        "_worktree": str(worktree),
        "changed_files": [],
    }

    def _run(cmd, **kwargs):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        result = subprocess.CompletedProcess(cmd, 0)
        result.stdout = ""
        result.stderr = ""
        if "rev-parse HEAD" in cmd_str:
            result.stdout = "abc123456789\n"
        return result

    with mock.patch("subprocess.run", side_effect=_run):
        result = worker._execute_merge("task-merge-6", metadata)

    assert result["status"] == "succeeded"
    assert result["result"]["merge_mode"] == "already_merged_replay"
    assert result["result"]["merge_commit"] == "abc123456789"
    assert result["result"]["files_changed"] == 0


def test_reconcile_merge_advances_target_branch_not_main(tmp_path):
    """Reconcile cluster merge updates the session target branch, not main."""
    worker = _make_worker(workspace=str(tmp_path))
    worker._branch_exists = lambda branch: True
    worker._branch_already_merged = lambda branch, target_ref="HEAD": False
    worker._create_integration_worktree = mock.Mock(
        return_value=(str(tmp_path / "merge-task"), "merge/task-merge-7", "")
    )
    worker._remove_worktree = mock.Mock()
    worker._api = mock.Mock()
    worktree = tmp_path / "dev-task"
    worktree.mkdir()
    metadata = {
        "parent_task_id": "task-parent-7",
        "operation_type": "reconcile-cluster",
        "reconcile_target_branch": "reconcile/p-test-session",
        "reconcile_target_base_commit": "base123",
        "_branch": "dev/task-parent-7",
        "_worktree": str(worktree),
        "changed_files": ["agent/example.py"],
    }
    run_calls = []

    def _run(cmd, **kwargs):
        run_calls.append((cmd, kwargs.get("cwd")))
        result = subprocess.CompletedProcess(cmd, 0)
        result.stdout = ""
        result.stderr = ""
        if cmd[:3] == ["git", "diff", "--cached"]:
            result.stdout = "agent/example.py\n"
        return result

    with mock.patch("subprocess.run", side_effect=_run), \
         mock.patch("agent.governance.chain_trailer.get_chain_state",
                    return_value={"chain_sha": "parent123"}), \
         mock.patch("agent.governance.chain_trailer.write_merge_with_trailer",
                    return_value=(True, "merge123", "")):
        result = worker._execute_merge("task-merge-7", metadata)

    assert result["status"] == "succeeded"
    assert result["result"]["branch"] == "reconcile/p-test-session"
    assert result["result"]["merge_mode"] == "reconcile_target_branch"
    assert result["result"]["main_redeployed"] is False
    worker._create_integration_worktree.assert_called_once_with(
        "task-merge-7", base_ref="reconcile/p-test-session"
    )
    assert (["git", "branch", "-f", "reconcile/p-test-session", "merge123"],
            str(tmp_path)) in run_calls
    assert not any(call[0][:3] == ["git", "merge", "--ff-only"] for call in run_calls)
    worker._api.assert_not_called()


def test_reconcile_dev_worktree_uses_target_branch_base(tmp_path):
    """Dev worktrees for reconcile clusters are created from the session branch."""
    worker = _make_worker(workspace=str(tmp_path))
    worker._ensure_branch_at_ref = mock.Mock(return_value=(True, ""))
    run_calls = []

    def _run(cmd, **kwargs):
        run_calls.append((cmd, kwargs.get("cwd")))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with mock.patch("subprocess.run", side_effect=_run), mock.patch("os.makedirs"):
        worktree_path, branch = worker._create_worktree(
            "task-dev-8",
            base_ref="reconcile/p-test-session",
            base_commit="base123",
        )

    assert branch == "dev/task-dev-8"
    assert worktree_path
    worker._ensure_branch_at_ref.assert_called_once_with(
        "reconcile/p-test-session", "base123"
    )
    assert any(
        call[0] == [
            "git", "worktree", "add", "-b", "dev/task-dev-8",
            str(tmp_path / ".worktrees" / "dev-task-dev-8"),
            "reconcile/p-test-session",
        ]
        for call in run_calls
    )


def test_reconcile_deploy_skips_main_redeploy():
    """Deploy stage is a branch-local record for reconcile clusters."""
    worker = _make_worker()
    metadata = {
        "operation_type": "reconcile-cluster",
        "reconcile_target_branch": "reconcile/p-test-session",
        "merge_commit": "merge123",
        "changed_files": [],
    }
    with mock.patch.object(worker, "_report_progress"):
        result = worker._execute_deploy("task-deploy-9", metadata)

    assert result["status"] == "succeeded"
    assert result["result"]["report"]["main_redeployed"] is False
    assert "branch-local" in result["result"]["report"]["note"]
