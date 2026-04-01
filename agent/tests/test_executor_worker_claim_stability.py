"""Regression tests for executor claim-loop stability (D1 fix).

Verifies:
  - Worker continues polling after completing N consecutive tasks
  - Unhandled exceptions in _execute_task are caught; task marked failed; loop continues
  - Consecutive empty poll counter tracks and resets correctly
"""

import types
import pytest
from unittest.mock import MagicMock, patch, call

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from executor_worker import ExecutorWorker


@pytest.fixture
def worker():
    """Create a worker with mocked API calls."""
    w = ExecutorWorker(
        project_id="test-project",
        governance_url="http://localhost:40006",
        worker_id="test-worker",
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


class TestClaimLoopContinuesAfterBatch:
    """Worker must keep polling after completing N tasks."""

    def test_continues_polling_after_n_tasks(self, worker):
        """After processing N tasks, run_once keeps returning (True then False)
        proving the loop continues."""
        n = 5
        tasks = [_make_task(f"task-{i}") for i in range(n)]
        claim_results = list(tasks) + [None, None, None]  # N tasks then 3 empty polls

        worker._claim_task = MagicMock(side_effect=claim_results)
        worker._execute_task = MagicMock(return_value={
            "status": "succeeded",
            "result": {"summary": "ok"},
        })
        worker._complete_task = MagicMock(return_value={"auto_chain": {}})

        results = []
        for _ in range(n + 3):
            results.append(worker.run_once())

        # First N calls processed tasks (True), next 3 found nothing (False)
        assert results == [True] * n + [False] * 3
        # _claim_task was called N+3 times total — proves loop kept polling
        assert worker._claim_task.call_count == n + 3

    def test_consecutive_empty_polls_counter_resets(self, worker):
        """Counter resets to 0 after a successful claim."""
        worker._claim_task = MagicMock(side_effect=[
            None, None, None,  # 3 empty
            _make_task("task-reset"),  # claim succeeds
            None,  # empty again
        ])
        worker._execute_task = MagicMock(return_value={
            "status": "succeeded", "result": {"summary": "ok"},
        })
        worker._complete_task = MagicMock(return_value={"auto_chain": {}})

        for _ in range(5):
            worker.run_once()

        # After task-reset, counter should have been reset to 0,
        # then incremented once for the final empty poll
        assert worker._consecutive_empty_polls == 1


class TestRunOnceNeverRaises:
    """run_once must catch all exceptions and return True so loop continues."""

    def test_execute_task_exception_marks_failed(self, worker):
        """If _execute_task raises, run_once catches it, marks task failed,
        returns True."""
        worker._claim_task = MagicMock(return_value=_make_task("task-crash"))
        worker._execute_task = MagicMock(side_effect=RuntimeError("boom"))
        worker._complete_task = MagicMock(return_value={"auto_chain": {}})

        result = worker.run_once()

        assert result is True
        worker._complete_task.assert_called_once()
        args = worker._complete_task.call_args
        assert args[0][0] == "task-crash"  # task_id
        assert args[0][1] == "failed"  # status
        assert "boom" in str(args[0][2])  # error in result

    def test_claim_task_exception_returns_false(self, worker):
        """If _claim_task raises, run_once catches it and returns False
        (no task to mark failed)."""
        worker._claim_task = MagicMock(side_effect=ConnectionError("network down"))

        result = worker.run_once()

        assert result is False
        # consecutive_empty_polls should increment
        assert worker._consecutive_empty_polls == 1

    def test_current_task_cleared_after_exception(self, worker):
        """_current_task is always cleared, even after exception."""
        worker._claim_task = MagicMock(return_value=_make_task("task-x"))
        worker._execute_task = MagicMock(side_effect=ValueError("bad"))
        worker._complete_task = MagicMock(return_value={"auto_chain": {}})

        worker.run_once()
        assert worker._current_task is None


class TestRunLoopResilience:
    """run_loop must survive exceptions across multiple iterations."""

    def test_run_loop_calls_run_once_repeatedly(self, worker):
        """run_loop calls run_once in a loop, sleeping between iterations."""
        call_count = 0

        def counting_run_once():
            nonlocal call_count
            call_count += 1
            if call_count >= 7:
                worker._running = False
            return call_count <= 3  # first 3 "process" tasks, rest are empty

        worker.run_once = counting_run_once
        worker._acquire_pid_lock = MagicMock()
        worker._release_pid_lock = MagicMock()
        worker._recover_stuck_tasks = MagicMock()
        worker._sync_git_status = MagicMock()
        worker._recover_stale_leases = MagicMock()
        worker._run_ttl_cleanup = MagicMock()
        worker._api = MagicMock(return_value={"version": "test", "pid": 1})

        with patch("time.sleep"):
            worker.run_loop()

        assert call_count == 7

    def test_run_loop_survives_run_once_exception(self, worker):
        """Even if run_once somehow raises (shouldn't happen), run_loop catches it."""
        call_count = 0

        def sometimes_raises():
            nonlocal call_count
            call_count += 1
            if call_count >= 5:
                worker._running = False
                return False
            if call_count == 2:
                raise RuntimeError("unexpected")
            return False

        worker.run_once = sometimes_raises
        worker._acquire_pid_lock = MagicMock()
        worker._release_pid_lock = MagicMock()
        worker._recover_stuck_tasks = MagicMock()
        worker._sync_git_status = MagicMock()
        worker._recover_stale_leases = MagicMock()
        worker._run_ttl_cleanup = MagicMock()
        worker._api = MagicMock(return_value={"version": "test", "pid": 1})

        with patch("time.sleep"):
            worker.run_loop()

        # Loop continued past the exception
        assert call_count == 5
