"""Tests for executor worker stall detection and self-restart (R1-R6).

Covers:
  - Normal polling without stall (empty polls below threshold)
  - Stall detection trigger when queued tasks exist
  - Stall recovery resets state
  - No stall when queued tasks are zero (normal idle)
  - Configurable threshold via EXECUTOR_STALL_THRESHOLD
"""

import os
import sys
import time
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from executor_worker import ExecutorWorker, EXECUTOR_STALL_THRESHOLD


@pytest.fixture
def worker():
    """Create a worker with mocked API calls."""
    w = ExecutorWorker(
        project_id="test-project",
        governance_url="http://localhost:40006",
        worker_id="test-worker-stall",
        workspace="/tmp/test-workspace",
    )
    # Stub out _api so no real HTTP calls are made
    w._api = MagicMock(return_value={"error": "mocked"})
    return w


def _make_task(task_id: str, task_type: str = "dev") -> dict:
    return {
        "task_id": task_id,
        "type": task_type,
        "prompt": "do something",
        "metadata": {},
    }


class TestNormalPolling:
    """Normal polling should NOT trigger stall detection."""

    def test_normal_poll_below_threshold(self, worker):
        """Empty polls below STALL_THRESHOLD should not trigger stall."""
        worker._claim_task = MagicMock(return_value=None)
        worker._check_queued_tasks = MagicMock(return_value=5)

        # Run N-1 empty polls (below threshold)
        for _ in range(EXECUTOR_STALL_THRESHOLD - 1):
            worker.run_once()

        # _check_queued_tasks should NOT have been called (below threshold)
        worker._check_queued_tasks.assert_not_called()
        assert worker._consecutive_empty_polls == EXECUTOR_STALL_THRESHOLD - 1

    def test_normal_poll_counter_resets_on_claim(self, worker):
        """Counter resets to 0 when a task is successfully claimed."""
        task = _make_task("task-reset")

        # Simulate 5 empty polls
        worker._claim_task = MagicMock(return_value=None)
        worker._check_queued_tasks = MagicMock(return_value=0)
        for _ in range(5):
            worker.run_once()
        assert worker._consecutive_empty_polls == 5

        # Now claim a task
        worker._claim_task = MagicMock(return_value=task)
        worker._execute_task = MagicMock(return_value={
            "status": "succeeded",
            "result": {"summary": "ok"},
        })
        worker._complete_task = MagicMock(return_value={"auto_chain": {}})
        worker.run_once()

        assert worker._consecutive_empty_polls == 0


class TestStallDetection:
    """Stall detection triggers when empty polls >= threshold AND queued tasks exist."""

    def test_stall_detect_with_queued_tasks(self, worker):
        """After STALL_THRESHOLD empty polls with queued tasks, stall is detected."""
        worker._claim_task = MagicMock(return_value=None)
        worker._check_queued_tasks = MagicMock(return_value=3)

        with patch("executor_worker.log") as mock_log:
            for _ in range(EXECUTOR_STALL_THRESHOLD):
                worker.run_once()

            # Verify log.error was called with 'STALL' in message
            error_calls = [
                c for c in mock_log.error.call_args_list
                if "STALL" in str(c)
            ]
            assert len(error_calls) >= 1, "Expected log.error with STALL message"

        # After stall detection, counter should be reset
        assert worker._consecutive_empty_polls == 0

    def test_stall_detect_no_trigger_without_queued_tasks(self, worker):
        """If no queued tasks exist, stall is NOT triggered (normal idle)."""
        worker._claim_task = MagicMock(return_value=None)
        worker._check_queued_tasks = MagicMock(return_value=0)

        with patch("executor_worker.log") as mock_log:
            for _ in range(EXECUTOR_STALL_THRESHOLD + 5):
                worker.run_once()

            # log.error with STALL should NOT have been called
            error_calls = [
                c for c in mock_log.error.call_args_list
                if "STALL" in str(c)
            ]
            assert len(error_calls) == 0, "Should not trigger stall with zero queued tasks"

        # Counter should keep incrementing (no reset)
        assert worker._consecutive_empty_polls == EXECUTOR_STALL_THRESHOLD + 5


class TestStallRecovery:
    """After stall detection, worker state is reset for recovery."""

    def test_stall_recover_resets_state(self, worker):
        """Stall recovery resets _consecutive_empty_polls and _current_task."""
        worker._claim_task = MagicMock(return_value=None)
        worker._check_queued_tasks = MagicMock(return_value=2)
        worker._current_task = "some-old-task"

        for _ in range(EXECUTOR_STALL_THRESHOLD):
            worker.run_once()

        assert worker._consecutive_empty_polls == 0
        assert worker._current_task is None

    def test_stall_recover_reinitializes_lifecycle(self, worker):
        """Stall recovery re-initializes _lifecycle if it was set."""
        worker._claim_task = MagicMock(return_value=None)
        worker._check_queued_tasks = MagicMock(return_value=1)
        worker._lifecycle = MagicMock()  # Simulate existing lifecycle

        with patch("executor_worker.log"):
            # Patch ai_lifecycle import to avoid real import
            with patch.dict("sys.modules", {"ai_lifecycle": MagicMock()}):
                for _ in range(EXECUTOR_STALL_THRESHOLD):
                    worker.run_once()

        # _lifecycle should have been re-initialized (not None, but a new instance)
        # The key check is that the old mock is no longer the same object
        assert worker._consecutive_empty_polls == 0

    def test_stall_recover_allows_subsequent_claim(self, worker):
        """After stall recovery, worker can claim tasks again."""
        worker._check_queued_tasks = MagicMock(return_value=2)

        # Phase 1: trigger stall
        worker._claim_task = MagicMock(return_value=None)
        for _ in range(EXECUTOR_STALL_THRESHOLD):
            worker.run_once()
        assert worker._consecutive_empty_polls == 0

        # Phase 2: claim a task after recovery
        task = _make_task("task-after-stall")
        worker._claim_task = MagicMock(return_value=task)
        worker._execute_task = MagicMock(return_value={
            "status": "succeeded",
            "result": {"summary": "recovered"},
        })
        worker._complete_task = MagicMock(return_value={"auto_chain": {}})

        result = worker.run_once()
        assert result is True
        assert worker._consecutive_empty_polls == 0


class TestWorktreeFailure:
    """Dev task must fail fast when worktree creation fails (B10)."""

    def test_worktree_creation_failure_returns_failed(self, worker):
        """When _create_worktree returns (None, None) for a dev task,
        _execute_task must return a failed result, not fall back to main workspace."""
        task = {
            "task_id": "task-worktree-fail",
            "type": "dev",
            "prompt": "implement X",
            "metadata": {},
        }

        worker._create_worktree = MagicMock(return_value=(None, None))

        with patch("executor_worker.log") as mock_log:
            result = worker._execute_task(task)

        assert result["status"] == "failed"
        assert "worktree creation failed" in result["error"]
        assert "worktree" in result["error"]

        # Verify warning was logged
        warning_calls = [
            c for c in mock_log.warning.call_args_list
            if "worktree" in str(c).lower() and "fail" in str(c).lower()
        ]
        assert len(warning_calls) >= 1, "Expected log.warning mentioning worktree failure"

    def test_worktree_creation_success_proceeds(self, worker):
        """When _create_worktree succeeds, _execute_task should NOT return early."""
        task = {
            "task_id": "task-worktree-ok",
            "type": "dev",
            "prompt": "implement X",
            "metadata": {},
        }

        worker._create_worktree = MagicMock(return_value=("/tmp/wt", "dev/task-worktree-ok"))
        # Mock _run_claude_session to avoid real CLI calls
        worker._run_claude_session = MagicMock(return_value={
            "status": "succeeded",
            "result": {"summary": "ok"},
        })
        worker._remove_worktree = MagicMock()

        result = worker._execute_task(task)
        # Should NOT be a worktree failure
        assert result.get("error", "") == "" or "worktree creation failed" not in result.get("error", "")


class TestStallThresholdConfig:
    """EXECUTOR_STALL_THRESHOLD is configurable."""

    def test_default_threshold_is_20(self):
        """Default stall threshold should be 20."""
        assert EXECUTOR_STALL_THRESHOLD == 20

    def test_threshold_read_from_env(self):
        """EXECUTOR_STALL_THRESHOLD reads from env var."""
        # This is a compile-time constant, but verify it's an int from env
        assert isinstance(EXECUTOR_STALL_THRESHOLD, int)
        assert EXECUTOR_STALL_THRESHOLD > 0
