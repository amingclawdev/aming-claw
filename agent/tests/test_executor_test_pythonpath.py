"""Tests for B50: _execute_test PYTHONPATH propagation.

Verifies that _execute_test builds an env dict with PYTHONPATH containing
repo_root and repo_root/agent, preserves existing PYTHONPATH entries,
and passes env= to subprocess.run().
"""

import os
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_worker(workspace: str):
    """Create a minimal ExecutorWorker for testing _execute_test."""
    from agent.executor_worker import ExecutorWorker

    worker = object.__new__(ExecutorWorker)
    worker.workspace = workspace
    worker.project_id = "test-project"
    worker.gov_url = "http://localhost:40000"
    worker._task_id = None
    return worker


def _run_execute_test(worker, metadata, monkeypatch, env_override=None):
    """Run _execute_test and capture the env dict passed to subprocess.run."""
    captured = {}

    def fake_run(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        # Return a fake CompletedProcess with passing tests
        return types.SimpleNamespace(
            stdout="1 passed",
            stderr="",
            returncode=0,
        )

    if env_override:
        for k, v in env_override.items():
            monkeypatch.setenv(k, v)

    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        result = worker._execute_test("task-001", metadata)

    return captured.get("env"), result


class TestPythonpathIncludesRepoRoot:
    """AC6: env['PYTHONPATH'] contains both repo_root and repo_root/agent."""

    def test_pythonpath_includes_repo_root(self, tmp_path, monkeypatch):
        workspace = str(tmp_path)
        worker = _make_worker(workspace)
        # Create a dummy test file so pre-flight passes
        test_file = tmp_path / "test_dummy.py"
        test_file.write_text("pass")

        metadata = {
            "verification": {"command": "pytest test_dummy.py -v"},
            "test_files": ["test_dummy.py"],
        }

        env, result = _run_execute_test(worker, metadata, monkeypatch)

        assert env is not None, "env= kwarg must be passed to subprocess.run"
        pythonpath = env.get("PYTHONPATH", "")
        repo_root = str(Path(workspace).resolve())
        agent_path = str(Path(workspace).resolve() / "agent")

        assert repo_root in pythonpath, (
            f"PYTHONPATH must contain repo_root ({repo_root}), got: {pythonpath}"
        )
        assert agent_path in pythonpath, (
            f"PYTHONPATH must contain repo_root/agent ({agent_path}), got: {pythonpath}"
        )

        # Verify ordering: repo_root before agent_path
        parts = pythonpath.split(os.pathsep)
        root_idx = next(i for i, p in enumerate(parts) if p == repo_root)
        agent_idx = next(i for i, p in enumerate(parts) if p == agent_path)
        assert root_idx < agent_idx, "repo_root must come before repo_root/agent"


class TestPythonpathPreservesExisting:
    """AC7: pre-set PYTHONPATH appears after repo_root entries."""

    def test_pythonpath_preserves_existing(self, tmp_path, monkeypatch):
        workspace = str(tmp_path)
        worker = _make_worker(workspace)
        test_file = tmp_path / "test_dummy.py"
        test_file.write_text("pass")

        existing_path = "/some/existing/path"
        metadata = {
            "verification": {"command": "pytest test_dummy.py -v"},
            "test_files": ["test_dummy.py"],
        }

        env, result = _run_execute_test(
            worker, metadata, monkeypatch, env_override={"PYTHONPATH": existing_path}
        )

        assert env is not None, "env= kwarg must be passed to subprocess.run"
        pythonpath = env["PYTHONPATH"]

        assert existing_path in pythonpath, (
            f"Existing PYTHONPATH ({existing_path}) must be preserved, got: {pythonpath}"
        )

        # Existing must come AFTER the new entries (AC4)
        repo_root = str(Path(workspace).resolve())
        parts = pythonpath.split(os.pathsep)
        root_idx = next(i for i, p in enumerate(parts) if p == repo_root)
        existing_idx = next(i for i, p in enumerate(parts) if p == existing_path)
        assert existing_idx > root_idx, (
            f"Existing PYTHONPATH must be appended after repo_root entries. "
            f"root_idx={root_idx}, existing_idx={existing_idx}"
        )


class TestEnvKwargPassedToRun:
    """AC2: _sp.run call includes env=test_env kwarg."""

    def test_env_kwarg_present(self, tmp_path, monkeypatch):
        workspace = str(tmp_path)
        worker = _make_worker(workspace)
        test_file = tmp_path / "test_dummy.py"
        test_file.write_text("pass")

        metadata = {
            "verification": {"command": "pytest test_dummy.py -v"},
            "test_files": ["test_dummy.py"],
        }

        env, result = _run_execute_test(worker, metadata, monkeypatch)
        assert env is not None, "env= kwarg must be passed to subprocess.run (AC2)"
        assert "PYTHONPATH" in env, "env must contain PYTHONPATH key"
