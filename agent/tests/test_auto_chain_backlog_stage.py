"""Unit tests for OPT-BACKLOG-CH4 backlog_bugs chain_stage transitions.

Verifies:
    AC1-AC4:  _update_backlog_stage helper function behaviour
    AC5-AC6:  Integration call-sites in _do_chain (grep-verify)
    AC7-AC10: Functional DB tests
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conn() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with backlog_bugs table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE backlog_bugs ("
        "  bug_id TEXT PRIMARY KEY,"
        "  project_id TEXT,"
        "  chain_task_id TEXT DEFAULT '',"
        "  chain_stage TEXT DEFAULT '',"
        "  stage_updated_at TEXT DEFAULT '',"
        "  last_failure_reason TEXT DEFAULT '',"
        "  runtime_state TEXT DEFAULT '',"
        "  current_task_id TEXT DEFAULT '',"
        "  root_task_id TEXT DEFAULT '',"
        "  worktree_path TEXT DEFAULT '',"
        "  worktree_branch TEXT DEFAULT '',"
        "  bypass_policy_json TEXT DEFAULT '{}',"
        "  mf_type TEXT DEFAULT '',"
        "  takeover_json TEXT DEFAULT '{}',"
        "  runtime_updated_at TEXT DEFAULT '',"
        "  updated_at TEXT DEFAULT ''"
        ")"
    )
    return conn


def _get_helper():
    """Import _update_backlog_stage from auto_chain."""
    import importlib
    mod = importlib.import_module("agent.governance.auto_chain")
    return mod._update_backlog_stage


# ---------------------------------------------------------------------------
# AC1: function signature exists
# ---------------------------------------------------------------------------

def test_update_backlog_stage_exists_with_correct_signature() -> None:
    """AC1: auto_chain.py contains _update_backlog_stage with expected params."""
    auto_chain_path = Path(__file__).resolve().parents[1] / "governance" / "auto_chain.py"
    content = auto_chain_path.read_text(encoding="utf-8")
    assert "def _update_backlog_stage(" in content
    assert "failure_reason=\"\"" in content
    assert "task_id=\"\"" in content


# ---------------------------------------------------------------------------
# AC2: correct SQL
# ---------------------------------------------------------------------------

def test_update_backlog_stage_sql_statement() -> None:
    """AC2: helper executes UPDATE with correct column order."""
    auto_chain_path = Path(__file__).resolve().parents[1] / "governance" / "backlog_runtime.py"
    content = auto_chain_path.read_text(encoding="utf-8")
    assert "UPDATE backlog_bugs SET" in content
    assert "runtime_state = ?" in content
    assert "current_task_id = CASE WHEN ? != '' THEN ? ELSE current_task_id END" in content


# ---------------------------------------------------------------------------
# AC5: _do_chain calls helper after task.completed
# ---------------------------------------------------------------------------

def test_do_chain_calls_update_backlog_stage_on_complete() -> None:
    """AC5: _do_chain calls _update_backlog_stage with task_type_complete."""
    auto_chain_path = Path(__file__).resolve().parents[1] / "governance" / "auto_chain.py"
    content = auto_chain_path.read_text(encoding="utf-8")
    assert 'f"{task_type}_complete"' in content
    assert "root_task_id=_chain_id" in content


# ---------------------------------------------------------------------------
# AC6: _do_chain calls helper in version gate block
# ---------------------------------------------------------------------------

def test_do_chain_calls_update_backlog_stage_on_gate_block() -> None:
    """AC6: _do_chain calls _update_backlog_stage with task_type_complete_blocked."""
    auto_chain_path = Path(__file__).resolve().parents[1] / "governance" / "auto_chain.py"
    content = auto_chain_path.read_text(encoding="utf-8")
    assert 'f"{task_type}_complete_blocked"' in content
    assert "failure_reason=ver_reason" in content


# ---------------------------------------------------------------------------
# AC7: functional — writes columns correctly
# ---------------------------------------------------------------------------

def test_update_backlog_stage_writes_columns() -> None:
    """AC7: inserts bug_id='TEST-1', calls helper, asserts chain_stage and stage_updated_at."""
    fn = _get_helper()
    conn = _make_conn()
    conn.execute(
        "INSERT INTO backlog_bugs (bug_id, project_id) VALUES (?, ?)",
        ("TEST-1", "aming-claw"),
    )
    fn(
        conn, "aming-claw", "TEST-1", "dev_complete",
        task_id="task-1", task_type="dev",
        metadata={"chain_id": "task-root"},
        result={"_worktree": ".worktrees/dev-task-1", "_branch": "dev/task-1"},
    )
    row = conn.execute(
        "SELECT chain_stage, stage_updated_at, runtime_state, current_task_id, root_task_id, worktree_path, worktree_branch FROM backlog_bugs WHERE bug_id=?",
        ("TEST-1",),
    ).fetchone()
    assert row is not None
    assert row["chain_stage"] == "dev_complete"
    assert row["stage_updated_at"] != ""
    assert row["runtime_state"] == "in_chain"
    assert row["current_task_id"] == "task-1"
    assert row["root_task_id"] == "task-root"
    assert row["worktree_path"] == ".worktrees/dev-task-1"
    assert row["worktree_branch"] == "dev/task-1"


# ---------------------------------------------------------------------------
# AC8: functional — failure_reason written
# ---------------------------------------------------------------------------

def test_update_backlog_stage_with_failure() -> None:
    """AC8: calls helper with failure_reason, asserts last_failure_reason stored."""
    fn = _get_helper()
    conn = _make_conn()
    conn.execute(
        "INSERT INTO backlog_bugs (bug_id, project_id) VALUES (?, ?)",
        ("TEST-2", "aming-claw"),
    )
    fn(conn, "aming-claw", "TEST-2", "dev_complete_blocked", failure_reason="gate blocked")
    row = conn.execute(
        "SELECT last_failure_reason FROM backlog_bugs WHERE bug_id=?",
        ("TEST-2",),
    ).fetchone()
    assert row is not None
    assert row["last_failure_reason"] == "gate blocked"


# ---------------------------------------------------------------------------
# AC9: no-op when bug_id is empty
# ---------------------------------------------------------------------------

def test_update_backlog_stage_no_bug_id() -> None:
    """AC9: calls helper with bug_id='', no exception, no row updated."""
    fn = _get_helper()
    conn = _make_conn()
    conn.execute(
        "INSERT INTO backlog_bugs (bug_id, project_id) VALUES (?, ?)",
        ("TEST-3", "aming-claw"),
    )
    # Should not raise
    fn(conn, "aming-claw", "", "dev_complete")
    # Original row unchanged
    row = conn.execute(
        "SELECT chain_stage FROM backlog_bugs WHERE bug_id=?",
        ("TEST-3",),
    ).fetchone()
    assert row["chain_stage"] == ""


# ---------------------------------------------------------------------------
# AC10: unknown bug_id — no exception
# ---------------------------------------------------------------------------

def test_update_backlog_stage_unknown_bug() -> None:
    """AC10: calls helper with bug_id='NOPE', no exception (0 rows matched)."""
    fn = _get_helper()
    conn = _make_conn()
    # No rows inserted for 'NOPE'
    fn(conn, "aming-claw", "NOPE", "dev_complete")
    # Verify no rows
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM backlog_bugs WHERE bug_id=?",
        ("NOPE",),
    ).fetchone()
    assert row["cnt"] == 0


def test_task_taken_over_by_mf_detection() -> None:
    """A task listed in takeover_json is treated as superseded by active MF."""
    import json
    import importlib

    mod = importlib.import_module("agent.governance.auto_chain")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE backlog_bugs (bug_id TEXT PRIMARY KEY, status TEXT, takeover_json TEXT DEFAULT '{}')"
    )
    conn.execute(
        "INSERT INTO backlog_bugs (bug_id, status, takeover_json) VALUES (?, ?, ?)",
        (
            "BUG-1",
            "MF_IN_PROGRESS",
            json.dumps({
                "action": "hold_current_chain",
                "taken_over_task_id": "task-1",
                "reason": "observer takeover",
            }),
        ),
    )

    taken, reason = mod._is_task_taken_over_by_mf(
        conn,
        "aming-claw",
        "task-1",
        {"bug_id": "BUG-1"},
    )

    assert taken is True
    assert reason == "observer takeover"
