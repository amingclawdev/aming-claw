"""Executor completion retry regression tests."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import executor_worker
from executor_worker import ExecutorWorker


def _worker():
    return ExecutorWorker(
        project_id="proj",
        governance_url="http://localhost:40000",
        worker_id="executor-complete-retry-test",
        workspace="C:/repo",
    )


def test_complete_task_retries_error_payload_then_returns_success():
    worker = _worker()
    worker._api = MagicMock(side_effect=[
        {"error": "connection refused"},
        {"error": "timeout"},
        {"ok": True, "auto_chain": {"task_id": "next"}},
    ])

    with patch.object(executor_worker.time, "sleep") as sleep:
        out = worker._complete_task("task-1", "succeeded", {"summary": "done"})

    assert out["ok"] is True
    assert worker._api.call_count == 3
    assert [c.args[0] for c in sleep.call_args_list] == [5, 15]


def test_complete_task_stops_without_retry_on_success():
    worker = _worker()
    worker._api = MagicMock(return_value={"ok": True})

    with patch.object(executor_worker.time, "sleep") as sleep:
        out = worker._complete_task("task-2", "failed", {"error": "boom"})

    assert out == {"ok": True}
    worker._api.assert_called_once()
    sleep.assert_not_called()


def test_complete_task_uses_long_complete_timeout():
    worker = _worker()
    worker._api = MagicMock(return_value={"ok": True})

    worker._complete_task("task-timeout", "succeeded", {"summary": "done"})

    assert executor_worker.COMPLETE_REQUEST_TIMEOUT >= 900
    assert worker._api.call_args.kwargs["timeout"] == executor_worker.COMPLETE_REQUEST_TIMEOUT


def test_complete_task_returns_final_error_after_all_retries():
    worker = _worker()
    worker._api = MagicMock(return_value={"error": "governance down"})

    with patch.object(executor_worker.time, "sleep") as sleep:
        out = worker._complete_task("task-3", "succeeded", {"summary": "done"})

    assert out == {"error": "governance down"}
    assert worker._api.call_count == 4
    assert [c.args[0] for c in sleep.call_args_list] == [5, 15, 30]
