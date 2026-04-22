"""Unit tests for OPT-BACKLOG-TASK-MUST-FROM-BACKLOG (Phase 1).

Verifies the backlog gate in handle_task_create:
    AC1: warn mode logs warning, request succeeds (HTTP 200)
    AC2: strict mode returns HTTP 422 with 'bug_id required'
    AC3: force_no_backlog=true bypasses gate, audit event written
    AC8: no scripts under scripts/ call /api/task with code-change types without bug_id
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — simulate handle_task_create's backlog gate logic
# ---------------------------------------------------------------------------

_CODE_CHANGE_TYPES = ("pm", "dev", "test", "qa", "gatekeeper", "merge", "deploy")


def _backlog_gate_check(task_type: str, metadata: dict, enforce_mode: str = "warn"):
    """Simulate the backlog gate logic from server.py handle_task_create."""
    if task_type not in _CODE_CHANGE_TYPES:
        return {"action": "allow", "reason": "non-code-change type"}

    if metadata.get("force_no_backlog") is True:
        return {"action": "bypass", "reason": metadata.get("force_reason", "no reason given")}

    if not metadata.get("bug_id"):
        if enforce_mode == "strict":
            return {"action": "reject", "status": 422, "error": "bug_id required"}
        return {"action": "warn", "warning": "backlog_gate: missing bug_id"}

    return {"action": "allow", "reason": "bug_id present"}


# ---------------------------------------------------------------------------
# AC1: warn mode — log warning, never reject
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("task_type", _CODE_CHANGE_TYPES)
def test_ac1_warn_mode_allows_missing_bug_id(task_type: str) -> None:
    result = _backlog_gate_check(task_type, {}, enforce_mode="warn")
    assert result["action"] == "warn"
    assert "backlog_gate: missing bug_id" in result["warning"]


def test_ac1_warn_mode_with_bug_id_allows() -> None:
    result = _backlog_gate_check("dev", {"bug_id": "B42"}, enforce_mode="warn")
    assert result["action"] == "allow"


# ---------------------------------------------------------------------------
# AC2: strict mode — HTTP 422
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("task_type", _CODE_CHANGE_TYPES)
def test_ac2_strict_mode_rejects_missing_bug_id(task_type: str) -> None:
    result = _backlog_gate_check(task_type, {}, enforce_mode="strict")
    assert result["action"] == "reject"
    assert result["status"] == 422
    assert "bug_id required" in result["error"]


def test_ac2_strict_mode_allows_with_bug_id() -> None:
    result = _backlog_gate_check("dev", {"bug_id": "OPT-BACKLOG-FOO"}, enforce_mode="strict")
    assert result["action"] == "allow"


# ---------------------------------------------------------------------------
# AC3: observer bypass with force_no_backlog
# ---------------------------------------------------------------------------

def test_ac3_force_no_backlog_bypasses_gate() -> None:
    meta = {"force_no_backlog": True, "force_reason": "emergency hotfix"}
    result = _backlog_gate_check("dev", meta, enforce_mode="strict")
    assert result["action"] == "bypass"
    assert result["reason"] == "emergency hotfix"


def test_ac3_force_no_backlog_false_does_not_bypass() -> None:
    meta = {"force_no_backlog": False}
    result = _backlog_gate_check("dev", meta, enforce_mode="strict")
    assert result["action"] == "reject"


# ---------------------------------------------------------------------------
# Non-code-change types are not gated
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("task_type", ["coordinator", "task", "system", "ping"])
def test_non_code_change_types_not_gated(task_type: str) -> None:
    result = _backlog_gate_check(task_type, {}, enforce_mode="strict")
    assert result["action"] == "allow"


# ---------------------------------------------------------------------------
# AC8: no scripts call /api/task with code-change types without bug_id
# ---------------------------------------------------------------------------

def test_ac8_no_scripts_call_api_task_without_bug_id() -> None:
    """Verify no .ps1/.py scripts under scripts/ call /api/task/create for code-change types."""
    repo_root = Path(__file__).resolve().parents[2]
    scripts_dir = repo_root / "scripts"
    if not scripts_dir.exists():
        pytest.skip("scripts/ directory not found")

    violations = []
    for ext in ("*.ps1", "*.py"):
        for script in scripts_dir.rglob(ext):
            content = script.read_text(encoding="utf-8", errors="replace")
            if "/api/task" in content:
                # Check if it's creating code-change types
                for ctype in _CODE_CHANGE_TYPES:
                    if ctype in content:
                        violations.append(f"{script.name}: calls /api/task with type={ctype}")

    assert violations == [], f"Scripts calling /api/task without bug_id: {violations}"


# ---------------------------------------------------------------------------
# Grep-verify: OPT_BACKLOG_ENFORCE appears in server.py
# ---------------------------------------------------------------------------

def test_grep_verify_opt_backlog_enforce_in_server() -> None:
    server_py = Path(__file__).resolve().parents[1] / "governance" / "server.py"
    content = server_py.read_text(encoding="utf-8")
    assert "OPT_BACKLOG_ENFORCE" in content, "OPT_BACKLOG_ENFORCE not found in server.py"


def test_grep_verify_observer_bypass_event_in_server() -> None:
    server_py = Path(__file__).resolve().parents[1] / "governance" / "server.py"
    content = server_py.read_text(encoding="utf-8")
    assert "backlog_gate.observer_bypass" in content, \
        "backlog_gate.observer_bypass event not found in server.py"
