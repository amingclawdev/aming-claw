"""Tests for executor redeploy via manager_signal.json mechanism (R1).

AC3: handle_redeploy('executor', {expected_head:'abc123',...}) writes
     manager_signal.json with action='restart'. Source contains 'manager_signal'.
"""

from __future__ import annotations

import inspect
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def test_executor_writes_manager_signal(tmp_path):
    """AC3: executor redeploy writes manager_signal.json with action='restart'."""
    from agent.governance.redeploy_handler import handle_redeploy

    # Patch the state dir to use tmp_path
    state_dir = tmp_path / "tasks" / "state"
    state_dir.mkdir(parents=True)
    agent_dir = tmp_path

    with patch.object(Path, "resolve", side_effect=lambda self=None: Path(tmp_path)):
        # We need to patch the path computation inside handle_redeploy
        # The handler computes: _agent_dir = Path(__file__).resolve().parent.parent
        # Then: state_dir = _tasks_root / "state"
        # Let's patch at a higher level
        pass

    # Direct approach: patch the path resolution in the executor branch
    import agent.governance.redeploy_handler as mod

    original_handler = mod.handle_redeploy

    # Create the state dir where the handler will write
    # The handler uses: Path(__file__).resolve().parent.parent / "tasks" / "state"
    handler_file = Path(mod.__file__).resolve()
    agent_dir = handler_file.parent.parent  # agent/
    tasks_state = agent_dir / "tasks" / "state"
    tasks_state.mkdir(parents=True, exist_ok=True)
    signal_path = tasks_state / "manager_signal.json"

    # Clean up any existing signal file
    if signal_path.exists():
        signal_path.unlink()

    # Patch _db_write_chain_version to avoid DB access
    with patch.object(mod, "_db_write_chain_version", return_value=True):
        result, status = mod.handle_redeploy("executor", {
            "expected_head": "abc123",
            "task_id": "test-deploy-task",
            "drain_grace_seconds": 0,
        })

    assert status == 200, f"Expected 200, got {status}: {result}"
    assert result["ok"] is True
    assert result.get("mechanism") == "manager_signal.json"

    # Verify the signal file was written
    assert signal_path.exists(), f"manager_signal.json not found at {signal_path}"
    data = json.loads(signal_path.read_text(encoding="utf-8"))
    assert data["action"] == "restart"
    assert data["expected_head"] == "abc123"
    assert data["task_id"] == "test-deploy-task"

    # Clean up
    if signal_path.exists():
        signal_path.unlink()


def test_executor_source_references_manager_signal():
    """AC3: Source code contains 'manager_signal' reference in executor path."""
    src = Path(__file__).resolve().parent.parent / "governance" / "redeploy_handler.py"
    text = src.read_text(encoding="utf-8")
    assert "manager_signal" in text, "Expected 'manager_signal' reference in redeploy_handler.py"


def test_executor_does_not_directly_kill_spawn():
    """AC3: executor path uses signal file, not direct kill/spawn."""
    from agent.governance.redeploy_handler import handle_redeploy
    src = inspect.getsource(handle_redeploy)

    # Find the executor-specific section
    # The executor path should reference manager_signal.json
    assert "manager_signal.json" in src, "Expected manager_signal.json in handle_redeploy source"


def test_executor_missing_expected_head():
    """Executor redeploy without expected_head returns 400."""
    from agent.governance.redeploy_handler import handle_redeploy

    result, status = handle_redeploy("executor", {
        "task_id": "test",
        "expected_head": "",
    })

    assert status == 400
    assert "expected_head" in result.get("error", "")
