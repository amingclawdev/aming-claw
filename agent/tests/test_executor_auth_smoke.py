"""Tests for R4: auth smoke test in executor_worker.run_loop startup.

Verifies that run_loop includes an auth smoke test between governance
health check and _recover_stuck_tasks.
"""

import inspect
import json
import logging
import os
import unittest
from unittest.mock import patch, MagicMock

import pytest


class TestAuthSmokeTestPresence(unittest.TestCase):
    """AC-D: run_loop must contain auth smoke test call."""

    def test_run_loop_contains_auth_check(self):
        """run_loop source must reference _check_auth_smoke_test."""
        from agent.executor_worker import ExecutorWorker
        source = inspect.getsource(ExecutorWorker.run_loop)
        self.assertIn("_check_auth_smoke_test", source,
                       "run_loop must call _check_auth_smoke_test between "
                       "governance health check and _recover_stuck_tasks")

    def test_auth_check_before_recover(self):
        """_check_auth_smoke_test must appear before _recover_stuck_tasks in run_loop."""
        from agent.executor_worker import ExecutorWorker
        source = inspect.getsource(ExecutorWorker.run_loop)
        auth_pos = source.find("_check_auth_smoke_test")
        recover_pos = source.find("_recover_stuck_tasks")
        self.assertGreater(auth_pos, -1, "_check_auth_smoke_test not found in run_loop")
        self.assertGreater(recover_pos, -1, "_recover_stuck_tasks not found in run_loop")
        self.assertLess(auth_pos, recover_pos,
                        "_check_auth_smoke_test must come before _recover_stuck_tasks")


class TestAuthSmokeTestBehavior(unittest.TestCase):
    """Test the _check_auth_smoke_test method itself."""

    def setUp(self):
        from agent.executor_worker import ExecutorWorker
        self.worker = ExecutorWorker.__new__(ExecutorWorker)
        self.worker.project_id = "test-project"

    def test_warns_when_stale_token_present(self):
        """Should log warning when CLAUDE_CODE_OAUTH_TOKEN is in env."""
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "stale-token"}):
            with self.assertLogs("executor_worker", level="WARNING") as cm:
                self.worker._check_auth_smoke_test()
            self.assertTrue(
                any("CLAUDE_CODE_OAUTH_TOKEN" in msg for msg in cm.output),
                "Should warn about stale CLAUDE_CODE_OAUTH_TOKEN in env"
            )

    def test_ok_when_no_token(self):
        """Should log info (not warning) when no stale token present."""
        env = dict(os.environ)
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        with patch.dict(os.environ, env, clear=True):
            with self.assertLogs("executor_worker", level="INFO") as cm:
                self.worker._check_auth_smoke_test()
            self.assertTrue(
                any("OK" in msg for msg in cm.output),
                "Should log OK when no stale token"
            )

    def test_non_blocking_on_exception(self):
        """Should not raise even if something goes wrong."""
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.environ.get", side_effect=RuntimeError("test")):
                # Should not raise — non-blocking
                try:
                    self.worker._check_auth_smoke_test()
                except RuntimeError:
                    self.fail("_check_auth_smoke_test should not propagate exceptions")


if __name__ == "__main__":
    unittest.main()
def test_executor_world_binding_routes_ac_only_to_dev(monkeypatch, tmp_path):
    from agent import executor_worker

    monkeypatch.setenv("AMING_CLAW_DEV_STORAGE_ROOT", str(tmp_path / "dev-world"))
    assert (
        executor_worker._world_bound_governance_url("aming-claw", "")
        == "http://127.0.0.1:40008"
    )
    assert executor_worker._world_bound_log_root("aming-claw", str(tmp_path)).is_relative_to(
        tmp_path / "dev-world"
    )
    with pytest.raises(ValueError):
        executor_worker._world_bound_governance_url(
            "aming-claw", "http://127.0.0.1:40000"
        )
    with pytest.raises(ValueError):
        executor_worker._world_bound_governance_url(
            "content-sys", "http://127.0.0.1:40008"
        )


def test_executor_session_token_is_header_only_and_startup_verified(monkeypatch, tmp_path):
    from agent import executor_worker

    token = "gov-test-worker-session-token"
    monkeypatch.setenv("AMING_CLAW_DEV_STORAGE_ROOT", str(tmp_path / "dev-world"))
    worker = executor_worker.ExecutorWorker(
        "aming-claw",
        session_token=token,
    )
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "valid": True,
                "project_id": "aming-claw",
                "session_id": "ses-worker",
                "role": "dev",
            }

    class Requests:
        @staticmethod
        def get(url, **kwargs):
            calls.append(("GET", url, kwargs))
            return Response()

        @staticmethod
        def post(url, **kwargs):
            calls.append(("POST", url, kwargs))
            return Response()

    monkeypatch.setitem(__import__("sys").modules, "requests", Requests)
    assert worker._validate_session_credential()["project_id"] == "aming-claw"
    worker._api(
        "POST",
        "/api/task/aming-claw/progress",
        {"task_id": "task-1", "percent": 1},
    )
    assert calls
    for _method, _url, kwargs in calls:
        assert kwargs["headers"]["X-Gov-Token"] == token
        assert token not in _url
        assert token not in json.dumps(kwargs.get("json") or {})

    with pytest.raises(RuntimeError, match="session credential"):
        executor_worker.ExecutorWorker(
            "aming-claw",
            session_token="",
        )._validate_session_credential()
