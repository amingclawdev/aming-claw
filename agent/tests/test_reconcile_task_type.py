"""Tests for reconcile task type — Phase J (AC-J1 through AC-J12).

Covers: _RECONCILE_STAGES registration, task creation, gate bypass,
apply-stage guarded connection, mutation plan I/O, advisory lock,
cancellation, and two-phase commit.
"""
import json
import os
import sys
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


def _make_in_memory_db():
    """Create an in-memory SQLite DB with governance schema (minimal)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS node_state (
            project_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            verify_status TEXT NOT NULL DEFAULT 'pending',
            build_status TEXT NOT NULL DEFAULT 'impl:missing',
            evidence_json TEXT,
            updated_by TEXT,
            updated_at TEXT,
            version INTEGER DEFAULT 1,
            PRIMARY KEY (project_id, node_id)
        );
        CREATE TABLE IF NOT EXISTS project_version (
            project_id TEXT PRIMARY KEY,
            chain_version TEXT,
            git_head TEXT,
            updated_by TEXT,
            updated_at TEXT,
            dirty_files TEXT,
            observer_mode INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            project_id TEXT,
            status TEXT,
            execution_status TEXT,
            notification_status TEXT DEFAULT 'none',
            type TEXT,
            prompt TEXT,
            related_nodes TEXT,
            created_by TEXT,
            created_at TEXT,
            updated_at TEXT,
            priority INTEGER DEFAULT 0,
            max_attempts INTEGER DEFAULT 3,
            metadata_json TEXT,
            parent_task_id TEXT,
            retry_round INTEGER DEFAULT 0,
            trace_id TEXT,
            chain_id TEXT,
            assigned_to TEXT,
            fencing_token TEXT,
            worker_id TEXT,
            worker_pid INTEGER,
            lease_expires_at TEXT,
            result_json TEXT,
            progress_json TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT,
            action TEXT,
            actor TEXT,
            ok INTEGER,
            ts TEXT,
            task_id TEXT,
            details_json TEXT,
            node_ids TEXT,
            chain_depth INTEGER,
            trace_id TEXT
        );
        CREATE TABLE IF NOT EXISTS backlog_bugs (
            bug_id TEXT PRIMARY KEY,
            project_id TEXT,
            title TEXT,
            priority TEXT,
            status TEXT DEFAULT 'open',
            created_at TEXT,
            expires_at TEXT,
            details_json TEXT,
            created_by TEXT
        );
        CREATE TABLE IF NOT EXISTS baselines (
            project_id TEXT,
            baseline_id TEXT,
            created_at TEXT,
            created_by TEXT,
            baseline_type TEXT,
            details_json TEXT,
            PRIMARY KEY (project_id, baseline_id)
        );
    """)
    return conn


class TestReconcileStagesDict(unittest.TestCase):
    """AC-J1: _RECONCILE_STAGES dict exists with correct keys."""

    def test_stages_exist_in_auto_chain(self):
        from governance.auto_chain import _RECONCILE_STAGES
        expected = {"scan", "diff", "propose", "approve", "apply", "verify"}
        self.assertEqual(set(_RECONCILE_STAGES.keys()), expected)

    def test_stages_are_callable(self):
        from governance.auto_chain import _RECONCILE_STAGES
        for name, handler in _RECONCILE_STAGES.items():
            self.assertTrue(callable(handler), f"Stage {name} handler is not callable")

    def test_stages_in_reconcile_task_module(self):
        from governance.reconcile_task import _RECONCILE_STAGES, RECONCILE_STAGES
        self.assertEqual(set(_RECONCILE_STAGES.keys()), set(RECONCILE_STAGES))


class TestTaskCreateReconcile(unittest.TestCase):
    """AC-J2, AC-J10: task_create accepts type='reconcile'."""

    def test_create_reconcile_task(self):
        from governance import task_registry
        conn = _make_in_memory_db()
        # Insert project_version for version drift check
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version) VALUES (?, ?)",
            ("test-proj", "abc1234"),
        )
        result = task_registry.create_task(
            conn, "test-proj", "reconcile test", task_type="reconcile"
        )
        conn.commit()
        self.assertIn("task_id", result)
        self.assertEqual(result["type"], "reconcile")

    def test_reconcile_type_in_valid_types(self):
        from governance.task_registry import VALID_TASK_TYPES
        self.assertIn("reconcile", VALID_TASK_TYPES)


class TestReconcileBypassVersionCheck(unittest.TestCase):
    """AC-J3, AC-J4: reconcile tasks bypass version_check and other gates."""

    def test_reconcile_dispatches_without_version_check(self):
        """Reconcile type routes to _dispatch_reconcile, not through CHAIN gates."""
        from governance.auto_chain import on_task_completed, CHAIN
        # 'reconcile' should NOT be in CHAIN
        self.assertNotIn("reconcile", CHAIN)

    def test_reconcile_prefix_dispatches(self):
        """task_type starting with 'reconcile_' also bypasses gates."""
        from governance.auto_chain import on_task_completed
        # Smoke test: the function handles reconcile_ prefix
        conn = _make_in_memory_db()
        # Should return None (no succeeded task) gracefully
        result = on_task_completed(
            conn, "test-proj", "task-999", "reconcile_custom",
            "failed", {}, {}
        )
        self.assertIsNone(result)  # failed status returns None


class TestReconcileStageHandlers(unittest.TestCase):
    """AC-J2: Test individual stage handlers."""

    def setUp(self):
        self.conn = _make_in_memory_db()
        self.project_id = "test-proj"
        self.task_id = "task-reconcile-001"
        # Insert project version
        self.conn.execute(
            "INSERT INTO project_version (project_id, chain_version, git_head) "
            "VALUES (?, ?, ?)",
            (self.project_id, "abc1234", "abc1234"),
        )
        # Insert a task
        self.conn.execute(
            "INSERT INTO tasks (task_id, project_id, status, execution_status, type) "
            "VALUES (?, ?, 'running', 'running', 'reconcile')",
            (self.task_id, self.project_id),
        )
        # Insert some node states
        for nid in ["L1.1", "L1.2", "L1.3"]:
            self.conn.execute(
                "INSERT INTO node_state (project_id, node_id, verify_status, build_status) "
                "VALUES (?, ?, 'pending', 'impl:missing')",
                (self.project_id, nid),
            )
        self.conn.commit()

    def test_scan_stage(self):
        from governance.reconcile_task import handle_scan
        result = handle_scan(self.conn, self.project_id, self.task_id, {}, None)
        self.assertEqual(result["stage"], "scan")
        self.assertEqual(result["node_count"], 3)
        self.assertIn("L1.1", result["node_states"])

    def test_diff_stage(self):
        from governance.reconcile_task import handle_scan, handle_diff
        scan = handle_scan(self.conn, self.project_id, self.task_id, {}, None)
        result = handle_diff(self.conn, self.project_id, self.task_id, {}, scan)
        self.assertEqual(result["stage"], "diff")
        self.assertIsInstance(result["diffs"], list)

    def test_propose_writes_mutation_plan(self):
        from governance.reconcile_task import handle_propose, _mutation_plan_path
        prev = {"diffs": [{"node_id": "L1.99", "type": "missing_in_db", "severity": "high"}],
                "baseline": {"chain_version": "abc1234"}}
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("governance.reconcile_task._MUTATION_PLAN_DIR",
                            os.path.join(tmpdir, "{pid}", "mutation_plans")):
                result = handle_propose(self.conn, self.project_id, self.task_id, {}, prev)
                self.assertEqual(result["stage"], "propose")
                self.assertEqual(result["mutation_count"], 1)

    def test_stages_run_in_order(self):
        """AC-J2: All 6 stages run in sequence."""
        from governance.reconcile_task import RECONCILE_STAGES
        self.assertEqual(RECONCILE_STAGES,
                         ["scan", "diff", "propose", "approve", "apply", "verify"])


class TestGuardedConnection(unittest.TestCase):
    """AC-J5: Apply stage blocks direct DB writes to node_state."""

    def test_direct_update_raises(self):
        from governance.reconcile_task import _GuardedConnection
        conn = _make_in_memory_db()
        guarded = _GuardedConnection(conn)
        with self.assertRaises(RuntimeError) as ctx:
            guarded.execute("UPDATE node_state SET verify_status = 'qa_pass' WHERE 1=1")
        self.assertIn("forbidden", str(ctx.exception).lower())

    def test_direct_insert_raises(self):
        from governance.reconcile_task import _GuardedConnection
        conn = _make_in_memory_db()
        guarded = _GuardedConnection(conn)
        with self.assertRaises(RuntimeError) as ctx:
            guarded.execute(
                "INSERT INTO node_state (project_id, node_id, verify_status) VALUES ('x','y','z')"
            )
        self.assertIn("forbidden", str(ctx.exception).lower())

    def test_select_allowed(self):
        from governance.reconcile_task import _GuardedConnection
        conn = _make_in_memory_db()
        guarded = _GuardedConnection(conn)
        # SELECT should work fine
        result = guarded.execute("SELECT * FROM node_state")
        self.assertIsNotNone(result)

    def test_other_table_allowed(self):
        from governance.reconcile_task import _GuardedConnection
        conn = _make_in_memory_db()
        guarded = _GuardedConnection(conn)
        guarded.execute(
            "INSERT INTO audit_log (project_id, action, actor, ok, ts) "
            "VALUES ('test', 'test', 'test', 1, '2026-01-01')"
        )


class TestApproveStage(unittest.TestCase):
    """AC-J6: Approve auto-approves high confidence, queues medium/low."""

    def test_approve_by_confidence(self):
        from governance.reconcile_task import handle_approve, _write_mutation_plan, _read_mutation_plan

        conn = _make_in_memory_db()
        conn.execute(
            "INSERT INTO tasks (task_id, project_id, status, execution_status, type) "
            "VALUES ('task-appr-1', 'proj1', 'running', 'running', 'reconcile')"
        )
        conn.commit()

        plan = {
            "task_id": "task-appr-1",
            "baseline_id_before": "abc",
            "phases_run": ["scan", "diff", "propose"],
            "mutations": [
                {"action": "node-create", "node_id": "L1.1", "confidence": "high"},
                {"action": "node-update", "node_id": "L1.2", "confidence": "medium"},
                {"action": "node-soft-delete", "node_id": "L1.3", "confidence": "low"},
            ],
            "summary": "test",
            "approve_threshold": "high",
            "applied_count": 0,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("governance.reconcile_task._MUTATION_PLAN_DIR",
                            os.path.join(tmpdir, "{pid}", "mutation_plans")):
                _write_mutation_plan("proj1", "task-appr-1", plan)
                result = handle_approve(conn, "proj1", "task-appr-1", {}, None)

                self.assertEqual(result["approved_count"], 1)
                self.assertEqual(result["queued_count"], 2)
                self.assertFalse(result["all_approved"])

                # Verify file was updated
                updated_plan = _read_mutation_plan("proj1", "task-appr-1")
                high_m = [m for m in updated_plan["mutations"] if m["confidence"] == "high"]
                self.assertTrue(high_m[0]["approved"])


class TestAdvisoryLock(unittest.TestCase):
    """AC-J12: Concurrent reconcile tasks serialize via advisory lock."""

    def test_acquire_and_release(self):
        from governance.reconcile_task import acquire_reconcile_lock, release_reconcile_lock
        conn = _make_in_memory_db()
        self.assertTrue(acquire_reconcile_lock(conn, "proj1", "task-1"))
        conn.commit()
        release_reconcile_lock(conn, "proj1", "task-1")
        conn.commit()

    def test_second_task_blocked(self):
        from governance.reconcile_task import acquire_reconcile_lock
        conn = _make_in_memory_db()
        self.assertTrue(acquire_reconcile_lock(conn, "proj1", "task-1"))
        conn.commit()
        self.assertFalse(acquire_reconcile_lock(conn, "proj1", "task-2"))

    def test_expired_lock_reclaimed(self):
        from governance.reconcile_task import acquire_reconcile_lock, _ensure_reconcile_lock_table
        conn = _make_in_memory_db()
        _ensure_reconcile_lock_table(conn)
        # Insert expired lock
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        conn.execute(
            "INSERT INTO reconcile_lock (project_id, holder_task_id, acquired_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            ("proj1", "task-old", past, past),
        )
        conn.commit()
        # New task should reclaim
        self.assertTrue(acquire_reconcile_lock(conn, "proj1", "task-new"))


class TestTwoPhaseCommit(unittest.TestCase):
    """AC-J11: Cancellable mid-run with rollback."""

    def test_begin_commit(self):
        from governance.reconcile_task import (
            _begin_two_phase, _commit_two_phase, _ensure_mutation_wal_table
        )
        conn = _make_in_memory_db()
        _ensure_mutation_wal_table(conn)
        txn_id = _begin_two_phase(conn, "task-1", [{"action": "test"}])
        conn.commit()
        _commit_two_phase(conn, txn_id)
        conn.commit()
        row = conn.execute(
            "SELECT status FROM reconcile_mutation_wal WHERE txn_id = ?", (txn_id,)
        ).fetchone()
        self.assertEqual(row["status"], "committed")

    def test_begin_rollback(self):
        from governance.reconcile_task import (
            _begin_two_phase, _rollback_two_phase, _ensure_mutation_wal_table
        )
        conn = _make_in_memory_db()
        _ensure_mutation_wal_table(conn)
        txn_id = _begin_two_phase(conn, "task-1", [{"action": "test"}])
        conn.commit()
        _rollback_two_phase(conn, txn_id)
        conn.commit()
        row = conn.execute(
            "SELECT status FROM reconcile_mutation_wal WHERE txn_id = ?", (txn_id,)
        ).fetchone()
        self.assertEqual(row["status"], "rolled_back")

    def test_cancellation_detected(self):
        from governance.reconcile_task import _check_cancellation, ReconcileCancelled
        conn = _make_in_memory_db()
        conn.execute(
            "INSERT INTO tasks (task_id, project_id, status, execution_status, type) "
            "VALUES ('task-cancel-1', 'proj1', 'cancelled', 'cancelled', 'reconcile')"
        )
        conn.commit()
        with self.assertRaises(ReconcileCancelled):
            _check_cancellation(conn, "task-cancel-1")


class TestBaselineWrite(unittest.TestCase):
    """AC-J8: Apply success triggers Phase I baseline write."""

    def test_baseline_written(self):
        from governance.reconcile_task import _trigger_baseline_write
        conn = _make_in_memory_db()
        _trigger_baseline_write(conn, "proj1", "task-bl-1")
        conn.commit()
        row = conn.execute(
            "SELECT * FROM baselines WHERE project_id = ? AND baseline_id = ?",
            ("proj1", "baseline-reconcile-task-bl-1"),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["baseline_type"], "phase_i")


class TestReconcileV2Endpoint(unittest.TestCase):
    """AC-J10: POST /api/wf/{pid}/reconcile-v2 returns task_id + status_url."""

    def test_v2_creates_task(self):
        """Verify the endpoint handler creates a reconcile task."""
        # We test the logic indirectly via task_registry
        from governance import task_registry
        conn = _make_in_memory_db()
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version) VALUES (?, ?)",
            ("test-proj", "abc1234"),
        )
        result = task_registry.create_task(
            conn, "test-proj", "Reconcile project",
            task_type="reconcile",
            metadata={"workspace_path": "/tmp/test"},
        )
        conn.commit()
        self.assertEqual(result["type"], "reconcile")
        self.assertIn("task_id", result)


if __name__ == "__main__":
    unittest.main()
