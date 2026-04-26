"""Tests for reconcile scope guard (PR3): scope enforcement on handle_apply.

Covers AC-M1 through AC-M4.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# Patch mutation plan dir before importing reconcile_task
_tmp_dir = tempfile.mkdtemp()
_PLAN_DIR = os.path.join(_tmp_dir, "{pid}", "mutation_plans")

import agent.governance.reconcile_task as rt

# Override the plan dir to use temp
rt._MUTATION_PLAN_DIR = _PLAN_DIR

from agent.governance.errors import ReconcileScopeViolationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conn():
    """Create an in-memory SQLite connection with required tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            execution_status TEXT DEFAULT 'running'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS node_state (
            project_id TEXT,
            node_id TEXT,
            verify_status TEXT DEFAULT 'pending',
            build_status TEXT DEFAULT 'impl:missing',
            updated_by TEXT,
            updated_at TEXT,
            PRIMARY KEY (project_id, node_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_version (
            project_id TEXT PRIMARY KEY,
            chain_version TEXT,
            git_head TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS baselines (
            project_id TEXT,
            baseline_id TEXT PRIMARY KEY,
            created_at TEXT,
            created_by TEXT,
            baseline_type TEXT,
            details_json TEXT
        )
    """)
    return conn


def _seed_task(conn, task_id, status="running"):
    conn.execute(
        "INSERT OR REPLACE INTO tasks (task_id, execution_status) VALUES (?, ?)",
        (task_id, status),
    )


def _seed_version(conn, project_id="test-proj"):
    conn.execute(
        "INSERT OR REPLACE INTO project_version (project_id, chain_version, git_head) "
        "VALUES (?, 'abc123', 'abc123')",
        (project_id,),
    )


PID = "test-proj"
TID = "task-test-scope-001"


# ---------------------------------------------------------------------------
# AC-M1: handle_propose persists scope_declared + scope_overflow_policy
# ---------------------------------------------------------------------------

class TestHandleProposeScope:
    """AC-M1: scope metadata flows into mutation_plan.json."""

    def test_scope_written_to_plan(self):
        conn = _make_conn()
        _seed_task(conn, TID)
        _seed_version(conn, PID)

        metadata = {
            "scope": {
                "bug_id": "BUG-42",
                "file_set": ["z.py", "a.py", "m.py"],
                "node_set": ["L3.2", "L1.1", "L2.5"],
                "commit_set": ["deadbeef"],
            }
        }

        # Run scan → diff → propose chain (mock diff to avoid graph load)
        scan = rt.handle_scan(conn, PID, TID, metadata, None)
        diff = rt.handle_diff(conn, PID, TID, metadata, scan)
        rt.handle_propose(conn, PID, TID, metadata, diff)

        plan = rt._read_mutation_plan(PID, TID)
        assert "scope_declared" in plan
        sd = plan["scope_declared"]
        assert sd["bug_id"] == "BUG-42"
        assert sd["file_set"] == ["a.py", "m.py", "z.py"]  # sorted
        assert sd["node_set"] == ["L1.1", "L2.5", "L3.2"]  # sorted
        assert sd["commit_set"] == ["deadbeef"]
        assert plan["scope_overflow_policy"] == "reject"  # default

    def test_scope_overflow_policy_custom(self):
        conn = _make_conn()
        _seed_task(conn, TID)
        _seed_version(conn, PID)

        metadata = {
            "scope": {
                "bug_id": "BUG-99",
                "file_set": ["x.py"],
                "node_set": ["L1.1"],
            },
            "scope_overflow_policy": "log_and_skip",
        }

        scan = rt.handle_scan(conn, PID, TID, metadata, None)
        diff = rt.handle_diff(conn, PID, TID, metadata, scan)
        rt.handle_propose(conn, PID, TID, metadata, diff)

        plan = rt._read_mutation_plan(PID, TID)
        assert plan["scope_overflow_policy"] == "log_and_skip"

    def test_no_scope_no_scope_declared(self):
        """When metadata has no scope, plan should not contain scope_declared."""
        conn = _make_conn()
        _seed_task(conn, TID)
        _seed_version(conn, PID)

        metadata = {}
        scan = rt.handle_scan(conn, PID, TID, metadata, None)
        diff = rt.handle_diff(conn, PID, TID, metadata, scan)
        rt.handle_propose(conn, PID, TID, metadata, diff)

        plan = rt._read_mutation_plan(PID, TID)
        assert "scope_declared" not in plan


# ---------------------------------------------------------------------------
# AC-M2: handle_apply reject policy raises ReconcileScopeViolationError
# ---------------------------------------------------------------------------

class TestHandleApplyReject:
    """AC-M2: out-of-scope mutation + policy=reject → error + plan status."""

    def _write_plan_with_scope(self, mutations, scope_declared,
                               policy="reject"):
        plan = {
            "task_id": TID,
            "baseline_id_before": "abc123",
            "phases_run": ["scan", "diff", "propose", "approve"],
            "mutations": mutations,
            "summary": "test",
            "approve_threshold": "high",
            "applied_count": 0,
            "scope_declared": scope_declared,
            "scope_overflow_policy": policy,
        }
        rt._write_mutation_plan(PID, TID, plan)

    def test_reject_out_of_scope_node(self):
        conn = _make_conn()
        _seed_task(conn, TID)

        self._write_plan_with_scope(
            mutations=[{
                "action": "node-update",
                "node_id": "L9.99",
                "approved": True,
                "fields": {"verify_status": "pending"},
            }],
            scope_declared={
                "bug_id": "BUG-1",
                "file_set": ["a.py"],
                "node_set": ["L1.1"],
            },
        )

        with pytest.raises(ReconcileScopeViolationError) as exc_info:
            rt.handle_apply(conn, PID, TID, {}, None)

        assert exc_info.value.status == 400
        assert exc_info.value.code == "reconcile_scope_violation"

        # Verify plan status
        plan = rt._read_mutation_plan(PID, TID)
        assert plan["status"] == "rejected_scope_violation"
        assert "rejection_detail" in plan
        assert "L9.99" in plan["rejection_detail"]

    def test_reject_preserves_in_scope_node(self):
        """In-scope mutation followed by out-of-scope → reject on second."""
        conn = _make_conn()
        _seed_task(conn, TID)

        self._write_plan_with_scope(
            mutations=[
                {
                    "action": "node-update",
                    "node_id": "L1.1",
                    "approved": True,
                    "fields": {"verify_status": "pending"},
                },
                {
                    "action": "node-update",
                    "node_id": "L9.99",
                    "approved": True,
                    "fields": {"verify_status": "pending"},
                },
            ],
            scope_declared={
                "bug_id": "BUG-2",
                "file_set": [],
                "node_set": ["L1.1"],
            },
        )

        with pytest.raises(ReconcileScopeViolationError):
            rt.handle_apply(conn, PID, TID, {}, None)


# ---------------------------------------------------------------------------
# AC-M3: handle_apply log_and_skip policy
# ---------------------------------------------------------------------------

class TestHandleApplyLogAndSkip:
    """AC-M3: out-of-scope + log_and_skip → skip + audit + continue."""

    def _write_plan_with_scope(self, mutations, scope_declared):
        plan = {
            "task_id": TID,
            "baseline_id_before": "abc123",
            "phases_run": ["scan", "diff", "propose", "approve"],
            "mutations": mutations,
            "summary": "test",
            "approve_threshold": "high",
            "applied_count": 0,
            "scope_declared": scope_declared,
            "scope_overflow_policy": "log_and_skip",
        }
        rt._write_mutation_plan(PID, TID, plan)

    @mock.patch("agent.governance.audit_service.record")
    def test_skip_out_of_scope_and_continue(self, mock_audit):
        conn = _make_conn()
        _seed_task(conn, TID)

        # node L1.1 is in scope, L9.99 is out
        self._write_plan_with_scope(
            mutations=[
                {
                    "action": "node-create",
                    "node_id": "L9.99",
                    "approved": True,
                    "confidence": "high",
                    "description": "out-of-scope node",
                },
                {
                    "action": "node-create",
                    "node_id": "L1.1",
                    "approved": True,
                    "confidence": "high",
                    "description": "in-scope node",
                },
            ],
            scope_declared={
                "bug_id": "BUG-3",
                "file_set": [],
                "node_set": ["L1.1"],
            },
        )

        result = rt.handle_apply(conn, PID, TID, {}, None)

        # Should have applied only 1 (in-scope)
        assert result["applied_count"] == 1

        # Verify the skipped mutation has skip_reason
        plan = rt._read_mutation_plan(PID, TID)
        skipped = [m for m in plan["mutations"] if m.get("skip_reason") == "scope_violation"]
        assert len(skipped) == 1
        assert skipped[0]["node_id"] == "L9.99"

        # Verify audit was called
        mock_audit.assert_called_once()
        call_args = mock_audit.call_args
        assert call_args[0][2] == "reconcile.scope.violation.skipped"

    @mock.patch("agent.governance.audit_service.record")
    def test_skip_all_out_of_scope(self, mock_audit):
        """All mutations out of scope → 0 applied, no error."""
        conn = _make_conn()
        _seed_task(conn, TID)

        self._write_plan_with_scope(
            mutations=[{
                "action": "node-update",
                "node_id": "L9.99",
                "approved": True,
                "fields": {},
            }],
            scope_declared={
                "bug_id": "BUG-4",
                "file_set": [],
                "node_set": ["L1.1"],
            },
        )

        result = rt.handle_apply(conn, PID, TID, {}, None)
        assert result["applied_count"] == 0
        assert result["txn_status"] == "committed"


# ---------------------------------------------------------------------------
# AC-M4: full reconcile (no scope) — backward compatible
# ---------------------------------------------------------------------------

class TestHandleApplyNoScope:
    """AC-M4: scope_declared=None or {} → no enforcement."""

    def _write_plan_no_scope(self, mutations, scope_declared=None):
        plan = {
            "task_id": TID,
            "baseline_id_before": "abc123",
            "phases_run": ["scan", "diff", "propose", "approve"],
            "mutations": mutations,
            "summary": "test",
            "approve_threshold": "high",
            "applied_count": 0,
        }
        if scope_declared is not None:
            plan["scope_declared"] = scope_declared
        rt._write_mutation_plan(PID, TID, plan)

    def test_no_scope_declared_applies_all(self):
        conn = _make_conn()
        _seed_task(conn, TID)

        self._write_plan_no_scope(
            mutations=[{
                "action": "node-create",
                "node_id": "L99.1",
                "approved": True,
                "confidence": "high",
                "description": "any node",
            }],
        )

        result = rt.handle_apply(conn, PID, TID, {}, None)
        assert result["applied_count"] == 1

    def test_empty_scope_declared_applies_all(self):
        conn = _make_conn()
        _seed_task(conn, TID)

        self._write_plan_no_scope(
            mutations=[{
                "action": "node-create",
                "node_id": "L99.2",
                "approved": True,
                "confidence": "high",
                "description": "any node",
            }],
            scope_declared={},
        )

        result = rt.handle_apply(conn, PID, TID, {}, None)
        assert result["applied_count"] == 1

    def test_scope_with_empty_sets_applies_all(self):
        """scope_declared present but node_set and file_set both empty."""
        conn = _make_conn()
        _seed_task(conn, TID)

        self._write_plan_no_scope(
            mutations=[{
                "action": "node-create",
                "node_id": "L99.3",
                "approved": True,
                "confidence": "high",
                "description": "any node",
            }],
            scope_declared={"bug_id": "X", "node_set": [], "file_set": []},
        )

        result = rt.handle_apply(conn, PID, TID, {}, None)
        assert result["applied_count"] == 1


# ---------------------------------------------------------------------------
# _mutation_in_scope unit tests (R7)
# ---------------------------------------------------------------------------

class TestMutationInScope:
    """Unit tests for _mutation_in_scope helper."""

    def test_node_id_in_node_set(self):
        assert rt._mutation_in_scope(
            {"node_id": "L1.1", "action": "node-update"},
            {"node_set": ["L1.1", "L2.2"], "file_set": []},
        ) is True

    def test_node_id_not_in_scope(self):
        assert rt._mutation_in_scope(
            {"node_id": "L9.9", "action": "node-update"},
            {"node_set": ["L1.1"], "file_set": []},
        ) is False

    def test_affected_file_in_file_set(self):
        assert rt._mutation_in_scope(
            {"node_id": "L9.9", "affected_file": "a.py", "action": "node-update"},
            {"node_set": [], "file_set": ["a.py", "b.py"]},
        ) is True

    def test_node_create_after_primary_string(self):
        assert rt._mutation_in_scope(
            {"action": "node-create", "after": {"primary": "src/foo.py"}},
            {"node_set": [], "file_set": ["src/foo.py"]},
        ) is True

    def test_node_create_after_primary_list(self):
        assert rt._mutation_in_scope(
            {"action": "node-create", "after": {"primary": ["src/foo.py", "other.py"]}},
            {"node_set": [], "file_set": ["src/foo.py"]},
        ) is True

    def test_node_create_after_primary_no_match(self):
        assert rt._mutation_in_scope(
            {"action": "node-create", "after": {"primary": ["other.py"]}},
            {"node_set": [], "file_set": ["src/foo.py"]},
        ) is False

    def test_empty_scope_sets(self):
        """Empty node_set and file_set → always out of scope."""
        assert rt._mutation_in_scope(
            {"node_id": "L1.1", "action": "node-update"},
            {"node_set": [], "file_set": []},
        ) is False


# ---------------------------------------------------------------------------
# ReconcileScopeViolationError unit tests (R6)
# ---------------------------------------------------------------------------

class TestReconcileScopeViolationError:

    def test_error_attributes(self):
        err = ReconcileScopeViolationError("L9.9", "not in scope")
        assert err.status == 400
        assert err.code == "reconcile_scope_violation"
        assert "L9.9" in str(err)

    def test_error_to_dict(self):
        err = ReconcileScopeViolationError("L9.9", "test detail", {"node_set": ["L1.1"]})
        d = err.to_dict()
        assert d["error"] == "reconcile_scope_violation"
        assert "details" in d
        assert d["details"]["mutation_id"] == "L9.9"
