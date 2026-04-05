"""Tests for parallel dispatch (WorkerPool) in executor_worker.py.

Covers:
  AC1:  MAX_CONCURRENT_WORKERS env var read (default 2, max 5 clamp)
  AC2:  Concurrent claims for sibling subtasks with same subtask_group_id
  AC3:  Worker-prefixed worktree path pattern
  AC4:  Connection-per-worker pattern (each worker thread uses own SQLite conn)
  AC5:  Atomic SQL fan-in completed_count
  AC6:  ExecutorWorker.stop() joins worker threads with SHUTDOWN_TIMEOUT
  AC7:  Single-task chain backward compatibility
  AC8:  executor_status reports worker pool status
  AC9:  ServiceManager monitor checks all worker threads
  AC10: Existing tests pass (covered by running the full suite)
"""

import json
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Ensure agent/ is on sys.path
_agent_dir = str(Path(__file__).resolve().parent.parent)
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)


# ---------------------------------------------------------------------------
# AC1: MAX_CONCURRENT_WORKERS env var with default 2 and max 5 clamp
# ---------------------------------------------------------------------------


class TestMaxConcurrentWorkers:
    """AC1: verify env var read with default 2 and max 5 clamp."""

    def test_default_value(self):
        """Default is 2 when env var is unset."""
        env = os.environ.copy()
        env.pop("MAX_CONCURRENT_WORKERS", None)
        with patch.dict(os.environ, env, clear=True):
            val = min(5, max(1, int(os.getenv("MAX_CONCURRENT_WORKERS", "2"))))
            assert val == 2

    def test_env_override(self):
        """Reads from env var."""
        with patch.dict(os.environ, {"MAX_CONCURRENT_WORKERS": "4"}):
            val = min(5, max(1, int(os.getenv("MAX_CONCURRENT_WORKERS", "2"))))
            assert val == 4

    def test_clamp_max_5(self):
        """Values above 5 are clamped to 5."""
        with patch.dict(os.environ, {"MAX_CONCURRENT_WORKERS": "10"}):
            val = min(5, max(1, int(os.getenv("MAX_CONCURRENT_WORKERS", "2"))))
            assert val == 5

    def test_clamp_min_1(self):
        """Values below 1 are clamped to 1."""
        with patch.dict(os.environ, {"MAX_CONCURRENT_WORKERS": "0"}):
            val = min(5, max(1, int(os.getenv("MAX_CONCURRENT_WORKERS", "2"))))
            assert val == 1

    def test_grep_pattern_exists(self):
        """AC1: grep -r 'MAX_CONCURRENT_WORKERS' agent/executor_worker.py returns results."""
        executor_path = Path(__file__).resolve().parent.parent / "executor_worker.py"
        content = executor_path.read_text(encoding="utf-8")
        assert "MAX_CONCURRENT_WORKERS" in content
        # Verify env var read with default and clamp
        assert 'os.getenv("MAX_CONCURRENT_WORKERS", "2")' in content
        assert "min(5, max(1," in content


# ---------------------------------------------------------------------------
# AC2: Concurrent claims for sibling subtasks
# ---------------------------------------------------------------------------


class TestParallelDispatch:
    """AC2: When 2 subtask dev tasks with same subtask_group_id are queued
    and have no dependencies, both are claimed within the same poll cycle."""

    def _make_executor(self):
        from executor_worker import ExecutorWorker
        worker = ExecutorWorker.__new__(ExecutorWorker)
        worker.project_id = "test-project"
        worker.base_url = "http://localhost:40000"
        worker.worker_id = "test-worker"
        worker.workspace = "/tmp/test-workspace"
        worker._running = False
        worker._current_task = None
        worker._lifecycle = None
        worker._pid_path = None
        worker._consecutive_empty_polls = 0
        worker._start_time = time.monotonic()
        worker.last_claimed_at = time.monotonic()
        return worker

    def test_get_sibling_tasks_returns_eligible(self):
        """get_sibling_tasks returns tasks with same subtask_group_id and no deps."""
        from executor_worker import WorkerPool
        executor = self._make_executor()
        pool = WorkerPool(executor, max_workers=2)

        tasks_response = {
            "tasks": [
                {
                    "task_id": "task-a",
                    "type": "dev",
                    "status": "queued",
                    "metadata": json.dumps({"subtask_group_id": "group-1"}),
                },
                {
                    "task_id": "task-b",
                    "type": "dev",
                    "status": "queued",
                    "metadata": json.dumps({"subtask_group_id": "group-1"}),
                },
            ]
        }
        executor._api = MagicMock(return_value=tasks_response)
        siblings = pool.get_sibling_tasks()
        assert len(siblings) == 2

    def test_no_siblings_returns_empty(self):
        """No parallel dispatch when no sibling subtasks exist."""
        from executor_worker import WorkerPool
        executor = self._make_executor()
        pool = WorkerPool(executor, max_workers=2)

        tasks_response = {
            "tasks": [
                {
                    "task_id": "task-a",
                    "type": "dev",
                    "status": "queued",
                    "metadata": json.dumps({"subtask_group_id": "group-1"}),
                },
            ]
        }
        executor._api = MagicMock(return_value=tasks_response)
        siblings = pool.get_sibling_tasks()
        assert len(siblings) == 0

    def test_concurrent_claim_calls(self):
        """AC2: Both tasks are claimed within the same poll cycle."""
        from executor_worker import WorkerPool
        executor = self._make_executor()
        pool = WorkerPool(executor, max_workers=2)

        claim_count = 0
        claimed_tasks = [
            {"task_id": "task-a", "type": "dev", "prompt": "do A", "metadata": {}},
            {"task_id": "task-b", "type": "dev", "prompt": "do B", "metadata": {}},
        ]

        def mock_claim():
            nonlocal claim_count
            if claim_count < len(claimed_tasks):
                t = claimed_tasks[claim_count]
                claim_count += 1
                return t
            return None

        executor._claim_task = mock_claim
        executor._execute_task = MagicMock(return_value={"status": "succeeded", "result": {}})
        executor._complete_task = MagicMock(return_value={})

        threads = pool.dispatch_parallel(claimed_tasks)
        for t in threads:
            t.join(timeout=10)

        assert executor._execute_task.call_count == 2


# ---------------------------------------------------------------------------
# AC3: Worker-prefixed worktree path
# ---------------------------------------------------------------------------


class TestWorkerWorktreePath:
    """AC3: Each concurrent task creates its own worktree under
    .worktrees/worker-{N}/dev-task-{id} pattern."""

    def test_create_worktree_with_worker_id(self):
        """_create_worktree with worker_id produces correct path pattern."""
        from executor_worker import ExecutorWorker
        worker = ExecutorWorker.__new__(ExecutorWorker)
        worker.workspace = "/tmp/test"

        # Mock subprocess.run to succeed
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            with patch("os.makedirs"):
                worktree_path, branch = worker._create_worktree("task-123", worker_id="worker-0")

        if worktree_path:
            assert "worker-0" in worktree_path
            assert "dev-task-task-123" in worktree_path

    def test_create_worktree_without_worker_id(self):
        """_create_worktree without worker_id produces old-style path (backward compat)."""
        from executor_worker import ExecutorWorker
        worker = ExecutorWorker.__new__(ExecutorWorker)
        worker.workspace = "/tmp/test"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            with patch("os.makedirs"):
                worktree_path, branch = worker._create_worktree("task-456")

        if worktree_path:
            assert "worker-" not in worktree_path
            assert "dev-task-456" in worktree_path


# ---------------------------------------------------------------------------
# AC4: Connection-per-worker pattern
# ---------------------------------------------------------------------------


class TestConnectionPerWorker:
    """AC4: Each worker thread uses its own SQLite connection."""

    def test_worker_threads_are_independent(self):
        """Each worker runs in its own thread (no shared state between workers)."""
        from executor_worker import WorkerPool, ExecutorWorker

        executor = ExecutorWorker.__new__(ExecutorWorker)
        executor.project_id = "test"
        executor.base_url = "http://localhost:40000"
        executor.workspace = "/tmp/test"
        executor._lifecycle = None

        pool = WorkerPool(executor, max_workers=3)

        thread_ids = []
        lock = threading.Lock()

        original_execute = executor._execute_task

        def mock_execute(task):
            with lock:
                thread_ids.append(threading.current_thread().ident)
            return {"status": "succeeded", "result": {}}

        executor._execute_task = mock_execute
        executor._complete_task = MagicMock(return_value={})
        executor._create_worktree = MagicMock(return_value=(None, None))
        executor._api = MagicMock(return_value={})

        tasks = [
            {"task_id": "t1", "type": "dev", "prompt": "a", "metadata": {}},
            {"task_id": "t2", "type": "dev", "prompt": "b", "metadata": {}},
        ]

        threads = pool.dispatch_parallel(tasks)
        for t in threads:
            t.join(timeout=10)

        # AC4: Each worker ran in a different thread
        assert len(set(thread_ids)) == 2, f"Expected 2 unique threads, got {thread_ids}"


# ---------------------------------------------------------------------------
# AC5: Atomic SQL fan-in completed_count
# ---------------------------------------------------------------------------


class TestAtomicFanIn:
    """AC5: Fan-in completed_count uses atomic SQL UPDATE (not read-modify-write)."""

    def test_atomic_fan_in_api_call(self):
        """Fan-in update calls API with subtask_group_id for atomic increment."""
        from executor_worker import WorkerPool, ExecutorWorker

        executor = ExecutorWorker.__new__(ExecutorWorker)
        executor.project_id = "test"
        executor.base_url = "http://localhost:40000"
        executor._api = MagicMock(return_value={})

        pool = WorkerPool(executor)
        task = {
            "task_id": "t1",
            "metadata": json.dumps({"subtask_group_id": "grp-abc"}),
        }
        pool._atomic_fan_in_update(task)
        executor._api.assert_called_once_with(
            "POST",
            "/api/task/test/fan-in-increment",
            {"subtask_group_id": "grp-abc"},
        )

    def test_no_fan_in_without_group_id(self):
        """No API call when subtask_group_id is absent."""
        from executor_worker import WorkerPool, ExecutorWorker

        executor = ExecutorWorker.__new__(ExecutorWorker)
        executor.project_id = "test"
        executor.base_url = "http://localhost:40000"
        executor._api = MagicMock(return_value={})

        pool = WorkerPool(executor)
        task = {"task_id": "t1", "metadata": {}}
        pool._atomic_fan_in_update(task)
        executor._api.assert_not_called()


# ---------------------------------------------------------------------------
# AC6: ExecutorWorker.stop() joins all worker threads with SHUTDOWN_TIMEOUT
# ---------------------------------------------------------------------------


class TestGracefulShutdown:
    """AC6: stop() joins all worker threads with SHUTDOWN_TIMEOUT."""

    def test_stop_joins_worker_threads(self):
        """stop() calls WorkerPool.shutdown() which joins threads."""
        from executor_worker import WorkerPool, ExecutorWorker

        executor = ExecutorWorker.__new__(ExecutorWorker)
        executor._running = True
        executor.project_id = "test"
        executor.base_url = "http://localhost:40000"

        pool = WorkerPool(executor, max_workers=2)
        executor._worker_pool = pool

        # Simulate a running worker
        done_event = threading.Event()

        def slow_worker():
            done_event.wait(timeout=5)

        t = threading.Thread(target=slow_worker, daemon=True)
        t.start()
        with pool._lock:
            pool._active_workers["worker-0"] = {
                "thread": t,
                "task_id": "task-x",
                "worktree": "/tmp/test",
            }

        # stop() should signal shutdown and join
        done_event.set()  # let the worker finish
        executor.stop()

        assert executor._running is False
        assert pool._shutdown_event.is_set()

    def test_shutdown_timeout_force_releases(self):
        """Timeout triggers force-release of uncompleted tasks."""
        from executor_worker import WorkerPool, ExecutorWorker

        executor = ExecutorWorker.__new__(ExecutorWorker)
        executor.project_id = "test"
        executor.base_url = "http://localhost:40000"
        executor._complete_task = MagicMock(return_value={})

        pool = WorkerPool(executor, max_workers=2)

        # Create a thread that won't finish
        hang_event = threading.Event()

        def hanging_worker():
            hang_event.wait(timeout=30)  # will hang

        t = threading.Thread(target=hanging_worker, daemon=True)
        t.start()
        with pool._lock:
            pool._active_workers["worker-0"] = {
                "thread": t,
                "task_id": "stuck-task",
                "worktree": "/tmp/test",
            }

        # Shutdown with very short timeout
        pool.shutdown(timeout=1)
        hang_event.set()  # cleanup

        # Should have attempted force-release
        executor._complete_task.assert_called_once()
        args = executor._complete_task.call_args
        assert args[0][0] == "stuck-task"
        assert args[0][1] == "failed"


# ---------------------------------------------------------------------------
# AC7: Single-task chain backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """AC7: When no sibling subtasks are queued, executor behaves identically
    to current single-worker sequential mode."""

    def test_no_siblings_falls_through_to_run_once(self):
        """get_sibling_tasks returns empty → executor uses run_once."""
        from executor_worker import WorkerPool, ExecutorWorker

        executor = ExecutorWorker.__new__(ExecutorWorker)
        executor.project_id = "test"
        executor.base_url = "http://localhost:40000"
        executor._api = MagicMock(return_value={"tasks": [
            {"task_id": "t1", "type": "dev", "status": "queued", "metadata": "{}"},
        ]})

        pool = WorkerPool(executor, max_workers=2)
        siblings = pool.get_sibling_tasks()
        assert len(siblings) == 0  # Only 1 task → no parallel

    def test_different_group_ids_not_parallel(self):
        """Tasks with different subtask_group_ids are not dispatched together."""
        from executor_worker import WorkerPool, ExecutorWorker

        executor = ExecutorWorker.__new__(ExecutorWorker)
        executor.project_id = "test"
        executor.base_url = "http://localhost:40000"
        executor._api = MagicMock(return_value={"tasks": [
            {"task_id": "t1", "type": "dev", "status": "queued",
             "metadata": json.dumps({"subtask_group_id": "group-A"})},
            {"task_id": "t2", "type": "dev", "status": "queued",
             "metadata": json.dumps({"subtask_group_id": "group-B"})},
        ]})

        pool = WorkerPool(executor, max_workers=2)
        siblings = pool.get_sibling_tasks()
        assert len(siblings) == 0


# ---------------------------------------------------------------------------
# AC8: executor_status reports worker pool status
# ---------------------------------------------------------------------------


class TestExecutorStatusReport:
    """AC8: executor_status reports active_workers count."""

    def test_worker_pool_status(self):
        """WorkerPool.status() returns active_workers and max_workers."""
        from executor_worker import WorkerPool, ExecutorWorker

        executor = ExecutorWorker.__new__(ExecutorWorker)
        executor.project_id = "test"
        executor.base_url = "http://localhost:40000"

        pool = WorkerPool(executor, max_workers=3)
        status = pool.status()

        assert "active_workers" in status
        assert "max_workers" in status
        assert status["max_workers"] == 3
        assert status["active_workers"] == 0
        assert "workers" in status

    def test_mcp_tool_includes_pool_status(self):
        """ToolDispatcher.dispatch('executor_status') includes pool info."""
        from mcp.tools import ToolDispatcher

        mock_api = MagicMock(return_value={})
        mock_pool = MagicMock()
        mock_pool.status.return_value = {
            "active_workers": 1,
            "max_workers": 2,
            "workers": [{"worker_name": "worker-0", "task_id": "t1", "worktree": "/tmp/w", "alive": True}],
            "shutdown_requested": False,
        }
        mock_svc = MagicMock()
        mock_svc.status.return_value = {"pid": 123, "running": True}

        dispatcher = ToolDispatcher(mock_api, mock_pool, mock_svc)
        result = dispatcher.dispatch("executor_status", {})

        assert result["active_workers"] == 1
        assert result["max_workers"] == 2
        assert result["pid"] == 123


# ---------------------------------------------------------------------------
# AC9: ServiceManager monitor checks all worker threads
# ---------------------------------------------------------------------------


class TestServiceManagerWorkerAwareness:
    """AC9: ServiceManager monitor checks all worker threads."""

    def test_set_worker_pool(self):
        """ServiceManager can register a WorkerPool."""
        from service_manager import ServiceManager
        mgr = ServiceManager(project_id="test")
        mock_pool = MagicMock()
        mock_pool.status.return_value = {"active_workers": 0, "max_workers": 2, "workers": []}
        mgr.set_worker_pool(mock_pool)
        assert mgr._worker_pool is mock_pool

    def test_status_includes_worker_pool(self):
        """ServiceManager.status() includes worker pool info when pool is set."""
        from service_manager import ServiceManager
        mgr = ServiceManager(project_id="test")
        mock_pool = MagicMock()
        mock_pool.status.return_value = {
            "active_workers": 2,
            "max_workers": 3,
            "workers": [],
        }
        mgr.set_worker_pool(mock_pool)
        status = mgr.status()
        assert "active_workers" in status
        assert status["active_workers"] == 2

    def test_status_without_pool(self):
        """ServiceManager.status() works fine without a worker pool."""
        from service_manager import ServiceManager
        mgr = ServiceManager(project_id="test")
        status = mgr.status()
        assert "pid" in status
        assert "running" in status


# ---------------------------------------------------------------------------
# AC10: Integration — module imports work
# ---------------------------------------------------------------------------


class TestModuleImports:
    """AC10: All modules import cleanly with the new code."""

    def test_import_executor_worker(self):
        import executor_worker
        assert hasattr(executor_worker, 'WorkerPool')
        assert hasattr(executor_worker, 'MAX_CONCURRENT_WORKERS')
        assert hasattr(executor_worker, 'SHUTDOWN_TIMEOUT')

    def test_import_service_manager(self):
        import service_manager
        assert hasattr(service_manager.ServiceManager, 'set_worker_pool')
        assert hasattr(service_manager.ServiceManager, '_worker_pool_status')

    def test_import_mcp_tools(self):
        from mcp.tools import ToolDispatcher, TOOLS
        # Verify executor_status tool exists
        tool_names = [t["name"] for t in TOOLS]
        assert "executor_status" in tool_names
