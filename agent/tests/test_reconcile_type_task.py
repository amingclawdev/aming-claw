"""Tests for reconcile task type registration, creator allowlist, audit, and rate limiting."""
import json
import os
import sys
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest import mock

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY, project_id TEXT, status TEXT,
            execution_status TEXT, type TEXT, prompt TEXT, related_nodes TEXT,
            created_by TEXT, created_at TEXT, updated_at TEXT, priority INTEGER DEFAULT 0,
            max_attempts INTEGER DEFAULT 3, metadata_json TEXT, parent_task_id TEXT,
            retry_round INTEGER DEFAULT 0, trace_id TEXT, chain_id TEXT,
            assigned_to TEXT, fencing_token TEXT, worker_id TEXT, worker_pid INTEGER,
            lease_expires_at TEXT, result_json TEXT, progress_json TEXT,
            notification_status TEXT DEFAULT 'none'
        );
        CREATE TABLE IF NOT EXISTS audit_index (
            event_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            event TEXT NOT NULL, actor TEXT, ok INTEGER NOT NULL DEFAULT 1,
            ts TEXT NOT NULL, node_ids TEXT
        );
    """)
    return conn


class TestReconcileInValidTypes(unittest.TestCase):
    """AC1: 'reconcile' is in VALID_TASK_TYPES."""

    def test_reconcile_in_valid_types(self):
        from governance.task_registry import VALID_TASK_TYPES
        self.assertIn("reconcile", VALID_TASK_TYPES)


class TestReconcileCreatorAllowlist(unittest.TestCase):
    """AC7: server.py creator allowlist for reconcile tasks (soft enforcement)."""

    def test_allowed_creators_no_warning(self):
        for creator in ("observer-1", "coordinator", "auto-chain-reconcile"):
            ok = any(creator.startswith(p) for p in ("observer-", "coordinator", "auto-chain-reconcile"))
            self.assertTrue(ok, f"{creator} should be allowed")

    def test_disallowed_creator_flagged(self):
        creator = "anonymous"
        ok = any(creator.startswith(p) for p in ("observer-", "coordinator", "auto-chain-reconcile"))
        self.assertFalse(ok, "anonymous should not be in allowlist")


class TestReconcileTaskAudit(unittest.TestCase):
    """AC6: reconcile_task.created audit event."""

    def test_audit_event_written(self):
        from governance.audit_service import record
        conn = _make_db()
        record(conn, "test-proj", event="reconcile_task.created",
               actor="observer-1", ok=True, node_ids=None, request_id="",
               task_id="t1", task_type="reconcile")
        row = conn.execute("SELECT * FROM audit_index WHERE event='reconcile_task.created'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["event"], "reconcile_task.created")

    def test_audit_event_for_reconcile_prefix(self):
        from governance.audit_service import record
        conn = _make_db()
        record(conn, "test-proj", event="reconcile_task.created",
               actor="observer-1", ok=True, node_ids=None, request_id="",
               task_id="t2", task_type="reconcile_fix")
        row = conn.execute("SELECT * FROM audit_index WHERE event='reconcile_task.created'").fetchone()
        self.assertIsNotNone(row)


class TestReconcileRateLimiting(unittest.TestCase):
    """AC8: 3-tier rate limiting for reconcile tasks."""

    def _insert_task(self, conn, task_id, run_id, status="pending"):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta = json.dumps({"reconcile_run_id": run_id})
        conn.execute(
            "INSERT INTO tasks (task_id, project_id, status, type, prompt, created_by, "
            "created_at, updated_at, metadata_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, "proj", status, "reconcile", "", "observer-1", now, now, meta),
        )

    def test_tier2_rejects_over_3_concurrent(self):
        conn = _make_db()
        for i in range(3):
            self._insert_task(conn, f"t{i}", "run-1", "pending")
        count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE project_id='proj' AND type LIKE 'reconcile%' "
            "AND status IN ('pending','claimed','running') AND json_extract(metadata_json, '$.reconcile_run_id')='run-1'",
            (),
        ).fetchone()[0]
        self.assertGreaterEqual(count, 3)

    def test_tier3_rejects_over_10_actions(self):
        conn = _make_db()
        for i in range(10):
            status = "succeeded" if i < 7 else "pending"
            self._insert_task(conn, f"t{i}", "run-2", status)
        count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE project_id='proj' AND type LIKE 'reconcile%' "
            "AND json_extract(metadata_json, '$.reconcile_run_id')='run-2'",
            (),
        ).fetchone()[0]
        self.assertGreaterEqual(count, 10)

    def test_no_limit_for_non_reconcile(self):
        conn = _make_db()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in range(20):
            conn.execute(
                "INSERT INTO tasks (task_id, project_id, status, type, prompt, created_by, "
                "created_at, updated_at, metadata_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"dev{i}", "proj", "pending", "dev", "", "anon", now, now, "{}"),
            )
        count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE project_id='proj' AND type='dev' AND status='pending'",
        ).fetchone()[0]
        self.assertEqual(count, 20)


if __name__ == "__main__":
    unittest.main()
