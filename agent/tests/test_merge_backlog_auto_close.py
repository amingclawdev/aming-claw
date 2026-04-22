"""Unit tests for OPT-BACKLOG merge-stage auto-close (R3).

Verifies:
    AC6: executor_worker._try_backlog_close_impl calls /api/backlog/{pid}/{bug_id}/close
    AC7: failure does not fail the merge (non-fatal, log warning only)
    grep-verify: 'backlog.*close' or '_try_backlog_close' in executor_worker.py
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from agent.executor_worker import ExecutorWorker


# ---------------------------------------------------------------------------
# AC6: _try_backlog_close_impl calls the backlog close API
# ---------------------------------------------------------------------------

def test_ac6_backlog_close_called_on_success() -> None:
    """When bug_id is present, the close endpoint is called."""
    mock_api = MagicMock()
    ExecutorWorker._try_backlog_close_impl("aming-claw", "B42", "abc1234", mock_api)
    mock_api.assert_called_once_with(
        "POST", "/api/backlog/aming-claw/B42/close",
        {"commit": "abc1234", "actor": "executor-merge"},
    )


def test_ac6_backlog_close_skipped_when_no_bug_id() -> None:
    """When bug_id is empty, the close endpoint is NOT called."""
    mock_api = MagicMock()
    ExecutorWorker._try_backlog_close_impl("aming-claw", "", "abc1234", mock_api)
    mock_api.assert_not_called()


def test_ac6_backlog_close_skipped_when_none_bug_id() -> None:
    """When bug_id is None, the close endpoint is NOT called."""
    mock_api = MagicMock()
    ExecutorWorker._try_backlog_close_impl("aming-claw", None, "abc1234", mock_api)
    mock_api.assert_not_called()


# ---------------------------------------------------------------------------
# AC7: failure does NOT fail the merge — non-fatal
# ---------------------------------------------------------------------------

def test_ac7_backlog_close_failure_does_not_raise() -> None:
    """If the backlog close API returns an error (HTTP 500), it must NOT raise."""
    mock_api = MagicMock(side_effect=Exception("HTTP 500: Internal Server Error"))
    # Should not raise
    ExecutorWorker._try_backlog_close_impl("aming-claw", "B42", "abc1234", mock_api)


def test_ac7_backlog_close_connection_error_does_not_raise() -> None:
    """ConnectionError from backlog close must NOT propagate."""
    mock_api = MagicMock(side_effect=ConnectionError("Connection refused"))
    # Should not raise
    ExecutorWorker._try_backlog_close_impl("aming-claw", "OPT-FOO", "def5678", mock_api)


# ---------------------------------------------------------------------------
# Grep-verify: backlog close in executor_worker.py
# ---------------------------------------------------------------------------

def test_grep_verify_backlog_close_in_executor_worker() -> None:
    ew_path = Path(__file__).resolve().parents[1] / "executor_worker.py"
    content = ew_path.read_text(encoding="utf-8")
    assert "_try_backlog_close" in content, \
        "_try_backlog_close not found in executor_worker.py"
