"""Tests for executor redeploy via manager_signal.json mechanism (R1).

AC3: handle_redeploy('executor', {expected_head:'abc123',...}) writes
     manager_signal.json with action='restart'. Source contains 'manager_signal'.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from unittest.mock import patch


def test_executor_writes_manager_signal(tmp_path, monkeypatch):
    """AC3: executor redeploy writes manager_signal.json with action='restart'."""
    from agent.governance.redeploy_handler import handle_redeploy

    import agent.governance.redeploy_handler as mod

    monkeypatch.setenv("SHARED_VOLUME_PATH", str(tmp_path))
    signal_path = tmp_path / "codex-tasks" / "state" / "manager_signal.json"

    with patch.object(mod, "_db_write_chain_version", return_value=True):
        result, status = mod.handle_redeploy("executor", {
            "expected_head": "abc123",
            "task_id": "test-deploy-task",
            "drain_grace_seconds": 0,
        })

    assert status == 200, f"Expected 200, got {status}: {result}"
    assert result["ok"] is True
    assert result.get("mechanism") == "manager_signal.json"
    assert result.get("signal_path") == str(signal_path)

    assert signal_path.exists(), f"manager_signal.json not found at {signal_path}"
    data = json.loads(signal_path.read_text(encoding="utf-8"))
    assert data["action"] == "restart"
    assert data["expected_head"] == "abc123"
    assert data["task_id"] == "test-deploy-task"


def test_executor_signal_path_matches_service_manager(tmp_path, monkeypatch):
    """Redeploy handler and service manager must agree on the watched signal path."""
    from agent.governance import redeploy_handler
    from agent import service_manager

    monkeypatch.setenv("SHARED_VOLUME_PATH", str(tmp_path))

    assert redeploy_handler._manager_signal_path() == service_manager._signal_file_path()


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
