"""Tests for B26: Populate session_id in _try_verify_update.

Covers AC7 (task_id provided), AC8 (fallback to parent_task_id), AC9 (fallback to auto-chain).
"""

import sqlite3
import pytest
from unittest.mock import patch, MagicMock, create_autospec

import sys
import os

_agent_dir = os.path.join(os.path.dirname(__file__), "..")
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)


def _make_conn():
    """Create a minimal in-memory governance DB."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _run_try_verify_update(metadata, task_id=""):
    """Helper: call _try_verify_update with mocked dependencies, return captured session dict."""
    from agent.governance.auto_chain import _try_verify_update

    conn = _make_conn()
    captured_session = {}

    mock_state_service = MagicMock()
    def capture_verify_update(*args, **kwargs):
        captured_session.update(kwargs.get("session", {}))
    mock_state_service.verify_update.side_effect = capture_verify_update

    mock_graph_cls = MagicMock()

    with patch("agent.governance.auto_chain._normalize_related_nodes", return_value=["L1.1"]), \
         patch.dict("sys.modules", {"agent.governance.state_service": mock_state_service}), \
         patch("agent.governance.auto_chain.os.path.exists", return_value=False):
        # The function does `from . import state_service` which resolves through sys.modules
        # We need to patch it as a module attribute with create=True since it's a lazy import
        with patch("agent.governance.auto_chain.state_service", mock_state_service, create=True), \
             patch("agent.governance.auto_chain.AcceptanceGraph", mock_graph_cls, create=True):
            ok, err = _try_verify_update(
                conn, "test-proj", metadata, "testing", "dev",
                {"type": "dev_complete"}, task_id=task_id
            )

    return ok, err, captured_session


def test_session_id_from_task_id():
    """AC7: When task_id='task-123' is provided, session['session_id'] == 'task-123'."""
    metadata = {"related_nodes": ["L1.1"], "parent_task_id": "task-456", "task_id": "task-789"}
    ok, err, session = _run_try_verify_update(metadata, task_id="task-123")
    assert ok is True
    assert session["session_id"] == "task-123"


def test_session_id_falls_back_to_parent_task_id():
    """AC8: When task_id='', metadata has parent_task_id='task-456', session['session_id'] == 'task-456'."""
    metadata = {"related_nodes": ["L1.1"], "parent_task_id": "task-456"}
    ok, err, session = _run_try_verify_update(metadata, task_id="")
    assert ok is True
    assert session["session_id"] == "task-456"


def test_session_id_falls_back_to_auto_chain():
    """AC9: When task_id='', metadata={}, session['session_id'] == 'auto-chain'."""
    metadata = {"related_nodes": ["L1.1"]}
    ok, err, session = _run_try_verify_update(metadata, task_id="")
    assert ok is True
    assert session["session_id"] == "auto-chain"


def test_session_id_falls_back_to_metadata_task_id():
    """When task_id='', no parent_task_id, but metadata has task_id='task-999'."""
    metadata = {"related_nodes": ["L1.1"], "task_id": "task-999"}
    ok, err, session = _run_try_verify_update(metadata, task_id="")
    assert ok is True
    assert session["session_id"] == "task-999"
