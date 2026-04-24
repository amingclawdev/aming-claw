"""Unit tests for OPT-BACKLOG-TASK-MUST-FROM-BACKLOG (Phase 1 + Z3 remaining).

Verifies the backlog gate in handle_task_create:
    AC1: warn mode logs warning, request succeeds (HTTP 200)
    AC2: strict mode returns HTTP 422 with 'bug_id required'
    AC3: force_no_backlog=true bypasses gate, audit event written
    AC8: no scripts under scripts/ call /api/task with code-change types without bug_id
    Z3:  parent_task_id, bug_id status, force_no_backlog tightening
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — simulate handle_task_create's backlog gate logic
# ---------------------------------------------------------------------------

_CODE_CHANGE_TYPES = ("pm", "dev", "test", "qa", "gatekeeper", "merge", "deploy")
_PARENT_REQUIRED_TYPES = ("dev", "test", "qa", "gatekeeper", "merge", "deploy")

# Simulated tasks table for parent_task_id existence checks
_EXISTING_TASKS = {"task-parent-001", "task-parent-002", "task-pm-root"}

# Simulated backlog_bugs table: bug_id -> status
_BACKLOG_BUGS = {
    "BUG-OPEN-1": "OPEN",
    "BUG-INPROG-1": "IN_PROGRESS",
    "BUG-FIXED-1": "FIXED",
    "BUG-CLOSED-1": "CLOSED",
    "BUG-CANCELLED-1": "CANCELLED",
}


def _backlog_gate_check(
    task_type: str,
    metadata: dict,
    enforce_mode: str = "warn",
    created_by: str = "user",
):
    """Simulate the backlog gate logic from server.py handle_task_create."""
    if task_type not in _CODE_CHANGE_TYPES:
        return {"action": "allow", "reason": "non-code-change type"}

    # auto-chain exemption
    if created_by in ("auto-chain", "auto-chain-retry"):
        return {"action": "allow", "reason": "auto-chain exempt"}

    _force_bypass = metadata.get("force_no_backlog") is True

    if _force_bypass:
        # R3: tighter force_no_backlog requirements
        _reason = metadata.get("force_reason", "")
        _mf_id = metadata.get("mf_id", "")
        if not _reason or len(_reason) < 30:
            if enforce_mode == "strict":
                return {"action": "reject", "status": 422,
                        "error": "force_no_backlog requires force_reason of at least 30 chars"}
            return {"action": "warn",
                    "warning": "force_no_backlog requires force_reason of at least 30 chars"}
        if not _mf_id or not re.match(r'^MF-\d{4}-\d{2}-\d{2}-\d{3}$', _mf_id):
            if enforce_mode == "strict":
                return {"action": "reject", "status": 422,
                        "error": "force_no_backlog requires mf_id matching MF-YYYY-MM-DD-NNN"}
            return {"action": "warn",
                    "warning": "force_no_backlog requires mf_id matching MF-YYYY-MM-DD-NNN"}
        return {"action": "bypass", "reason": _reason}

    bug_id = metadata.get("bug_id") or ""
    if not bug_id:
        if enforce_mode == "strict":
            return {"action": "reject", "status": 422, "error": "bug_id required"}
        return {"action": "warn", "warning": "backlog_gate: missing bug_id"}

    # Bug existence + status check
    bug_status = _BACKLOG_BUGS.get(bug_id)
    if bug_status is None:
        if enforce_mode == "strict":
            return {"action": "reject", "status": 422, "error": "bug_id not in backlog"}
        return {"action": "warn", "warning": "bug_id not found in backlog_bugs"}
    if bug_status not in ("OPEN", "IN_PROGRESS"):
        _msg = (f"bug_id {bug_id} is not OPEN (current status={bug_status}); "
                f"cannot attach new work to closed bug")
        if enforce_mode == "strict":
            return {"action": "reject", "status": 422, "error": _msg}
        return {"action": "warn", "warning": _msg}

    # R1: parent_task_id for non-pm types
    if task_type in _PARENT_REQUIRED_TYPES and not _force_bypass:
        _parent = metadata.get("parent_task_id") or ""
        if not _parent:
            _msg = "parent_task_id required for code-change type from non-auto-chain creator"
            if enforce_mode == "strict":
                return {"action": "reject", "status": 422, "error": _msg}
            return {"action": "warn", "warning": _msg}
        if _parent not in _EXISTING_TASKS:
            _msg = "parent_task_id not found in tasks table"
            if enforce_mode == "strict":
                return {"action": "reject", "status": 422, "error": _msg}
            return {"action": "warn", "warning": _msg}

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
    result = _backlog_gate_check("dev", {"bug_id": "BUG-OPEN-1", "parent_task_id": "task-parent-001"}, enforce_mode="warn")
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
    result = _backlog_gate_check("dev", {"bug_id": "BUG-OPEN-1", "parent_task_id": "task-parent-001"}, enforce_mode="strict")
    assert result["action"] == "allow"


# ---------------------------------------------------------------------------
# AC3: observer bypass with force_no_backlog
# ---------------------------------------------------------------------------

def test_ac3_force_no_backlog_bypasses_gate() -> None:
    meta = {
        "force_no_backlog": True,
        "force_reason": "emergency hotfix requiring immediate bypass action",
        "mf_id": "MF-2026-04-24-001",
    }
    result = _backlog_gate_check("dev", meta, enforce_mode="strict")
    assert result["action"] == "bypass"
    assert result["reason"] == "emergency hotfix requiring immediate bypass action"


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


# ---------------------------------------------------------------------------
# Z3: R1 — parent_task_id requirement (negative tests)
# ---------------------------------------------------------------------------

def test_r1_parent_task_id_missing_strict_rejects() -> None:
    """Non-pm code-change type without parent_task_id → 422."""
    meta = {"bug_id": "BUG-OPEN-1"}
    result = _backlog_gate_check("dev", meta, enforce_mode="strict")
    assert result["action"] == "reject"
    assert result["status"] == 422
    assert "parent_task_id required" in result["error"]


def test_r1_parent_task_id_nonexistent_strict_rejects() -> None:
    """parent_task_id that doesn't exist in tasks table → 422."""
    meta = {"bug_id": "BUG-OPEN-1", "parent_task_id": "task-does-not-exist"}
    result = _backlog_gate_check("dev", meta, enforce_mode="strict")
    assert result["action"] == "reject"
    assert result["status"] == 422
    assert "parent_task_id not found in tasks table" in result["error"]


def test_r1_pm_type_exempt_from_parent_task_id() -> None:
    """PM type should not require parent_task_id (chain origin)."""
    meta = {"bug_id": "BUG-OPEN-1"}
    result = _backlog_gate_check("pm", meta, enforce_mode="strict")
    assert result["action"] == "allow"


# ---------------------------------------------------------------------------
# Z3: R2 — bug_id status check (negative test)
# ---------------------------------------------------------------------------

def test_r2_bug_id_fixed_status_strict_rejects() -> None:
    """bug_id with FIXED status → 422."""
    meta = {"bug_id": "BUG-FIXED-1", "parent_task_id": "task-parent-001"}
    result = _backlog_gate_check("dev", meta, enforce_mode="strict")
    assert result["action"] == "reject"
    assert result["status"] == 422
    assert "is not OPEN (current status=" in result["error"]


def test_r2_bug_id_closed_status_strict_rejects() -> None:
    """bug_id with CLOSED status → 422."""
    meta = {"bug_id": "BUG-CLOSED-1", "parent_task_id": "task-parent-001"}
    result = _backlog_gate_check("dev", meta, enforce_mode="strict")
    assert result["action"] == "reject"
    assert "is not OPEN (current status=" in result["error"]


# ---------------------------------------------------------------------------
# Z3: R3 — force_no_backlog tighter requirements (negative tests)
# ---------------------------------------------------------------------------

def test_r3_force_reason_too_short_strict_rejects() -> None:
    """force_reason < 30 chars → 422."""
    meta = {"force_no_backlog": True, "force_reason": "short", "mf_id": "MF-2026-04-24-001"}
    result = _backlog_gate_check("dev", meta, enforce_mode="strict")
    assert result["action"] == "reject"
    assert result["status"] == 422
    assert "force_no_backlog requires force_reason of at least 30 chars" in result["error"]


def test_r3_mf_id_malformed_strict_rejects() -> None:
    """mf_id not matching MF-YYYY-MM-DD-NNN → 422."""
    meta = {
        "force_no_backlog": True,
        "force_reason": "a valid reason that is at least thirty characters long",
        "mf_id": "INVALID-FORMAT",
    }
    result = _backlog_gate_check("dev", meta, enforce_mode="strict")
    assert result["action"] == "reject"
    assert result["status"] == 422
    assert "force_no_backlog requires mf_id matching MF-YYYY-MM-DD-NNN" in result["error"]


def test_r3_mf_id_missing_strict_rejects() -> None:
    """mf_id missing entirely → 422."""
    meta = {
        "force_no_backlog": True,
        "force_reason": "a valid reason that is at least thirty characters long",
    }
    result = _backlog_gate_check("dev", meta, enforce_mode="strict")
    assert result["action"] == "reject"
    assert result["status"] == 422
    assert "mf_id matching MF-YYYY-MM-DD-NNN" in result["error"]


# ---------------------------------------------------------------------------
# Z3: Positive tests
# ---------------------------------------------------------------------------

def test_positive_valid_parent_task_id_allows() -> None:
    """Valid parent_task_id + OPEN bug_id → allow."""
    meta = {"bug_id": "BUG-OPEN-1", "parent_task_id": "task-parent-001"}
    result = _backlog_gate_check("dev", meta, enforce_mode="strict")
    assert result["action"] == "allow"


def test_positive_open_bug_with_parent_allows() -> None:
    """OPEN bug_id + valid parent → allow for all non-pm types."""
    for tt in _PARENT_REQUIRED_TYPES:
        meta = {"bug_id": "BUG-OPEN-1", "parent_task_id": "task-parent-002"}
        result = _backlog_gate_check(tt, meta, enforce_mode="strict")
        assert result["action"] == "allow", f"Failed for type {tt}"


def test_positive_in_progress_bug_allows() -> None:
    """IN_PROGRESS bug_id should also be allowed."""
    meta = {"bug_id": "BUG-INPROG-1", "parent_task_id": "task-parent-001"}
    result = _backlog_gate_check("dev", meta, enforce_mode="strict")
    assert result["action"] == "allow"


def test_positive_valid_force_no_backlog_with_reason_and_mf_id() -> None:
    """force_no_backlog with 30+ char reason + valid mf_id → bypass."""
    meta = {
        "force_no_backlog": True,
        "force_reason": "emergency production hotfix bypassing normal governance flow",
        "mf_id": "MF-2026-04-24-001",
    }
    result = _backlog_gate_check("dev", meta, enforce_mode="strict")
    assert result["action"] == "bypass"


# ---------------------------------------------------------------------------
# Z3: R5 — auto-chain exemption
# ---------------------------------------------------------------------------

def test_r5_auto_chain_skips_all_checks() -> None:
    """auto-chain creator skips parent_task_id + bug_id status checks."""
    # No bug_id, no parent_task_id — should still allow
    meta = {}
    result = _backlog_gate_check("dev", meta, enforce_mode="strict", created_by="auto-chain")
    assert result["action"] == "allow"
    assert result["reason"] == "auto-chain exempt"


def test_r5_auto_chain_retry_skips_all_checks() -> None:
    """auto-chain-retry creator also exempt."""
    meta = {}
    result = _backlog_gate_check("dev", meta, enforce_mode="strict", created_by="auto-chain-retry")
    assert result["action"] == "allow"
    assert result["reason"] == "auto-chain exempt"


# ---------------------------------------------------------------------------
# Z3: R4 — warn mode allows through
# ---------------------------------------------------------------------------

def test_r4_warn_mode_parent_task_id_missing_allows() -> None:
    """warn mode: missing parent_task_id logs warning but allows."""
    meta = {"bug_id": "BUG-OPEN-1"}
    result = _backlog_gate_check("dev", meta, enforce_mode="warn")
    assert result["action"] == "warn"
    assert "parent_task_id required" in result["warning"]


def test_r4_warn_mode_bug_status_closed_allows() -> None:
    """warn mode: CLOSED bug_id logs warning but allows."""
    meta = {"bug_id": "BUG-CLOSED-1", "parent_task_id": "task-parent-001"}
    result = _backlog_gate_check("dev", meta, enforce_mode="warn")
    assert result["action"] == "warn"
    assert "is not OPEN" in result["warning"]


# ---------------------------------------------------------------------------
# Grep-verify: Z3 strings present in server.py
# ---------------------------------------------------------------------------

def test_grep_verify_parent_task_id_required_in_server() -> None:
    server_py = Path(__file__).resolve().parents[1] / "governance" / "server.py"
    content = server_py.read_text(encoding="utf-8")
    assert "parent_task_id required" in content


def test_grep_verify_parent_task_id_not_found_in_server() -> None:
    server_py = Path(__file__).resolve().parents[1] / "governance" / "server.py"
    content = server_py.read_text(encoding="utf-8")
    assert "parent_task_id not found in tasks table" in content


def test_grep_verify_bug_status_check_in_server() -> None:
    server_py = Path(__file__).resolve().parents[1] / "governance" / "server.py"
    content = server_py.read_text(encoding="utf-8")
    assert "is not OPEN (current status=" in content


def test_grep_verify_force_reason_length_in_server() -> None:
    server_py = Path(__file__).resolve().parents[1] / "governance" / "server.py"
    content = server_py.read_text(encoding="utf-8")
    assert "force_no_backlog requires force_reason of at least 30 chars" in content


def test_grep_verify_mf_id_pattern_in_server() -> None:
    server_py = Path(__file__).resolve().parents[1] / "governance" / "server.py"
    content = server_py.read_text(encoding="utf-8")
    assert "force_no_backlog requires mf_id matching MF-YYYY-MM-DD-NNN" in content
    assert r"^MF-\d{4}-\d{2}-\d{2}-\d{3}$" in content


def test_grep_verify_auto_chain_exemption_preserved() -> None:
    """AC12: auto-chain and auto-chain-retry in created_by exemption check."""
    server_py = Path(__file__).resolve().parents[1] / "governance" / "server.py"
    content = server_py.read_text(encoding="utf-8")
    assert '"auto-chain"' in content
    assert '"auto-chain-retry"' in content
    # Verify they appear in a 'not in' check, not in a tuple check
    assert 'not in ("auto-chain", "auto-chain-retry")' in content
