"""Tests for reconcile gate exemptions (§11.1-§11.4)."""
import json
import os
import sys
import sqlite3
import unittest
from datetime import datetime, timezone, timedelta
from unittest import mock

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS node_state (
            project_id TEXT NOT NULL, node_id TEXT NOT NULL,
            verify_status TEXT NOT NULL DEFAULT 'pending',
            build_status TEXT NOT NULL DEFAULT 'impl:missing',
            evidence_json TEXT, updated_by TEXT, updated_at TEXT,
            version INTEGER DEFAULT 1,
            PRIMARY KEY (project_id, node_id)
        );
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
        CREATE TABLE IF NOT EXISTS project_version (
            project_id TEXT PRIMARY KEY, chain_version TEXT, git_head TEXT,
            updated_by TEXT, updated_at TEXT, dirty_files TEXT,
            observer_mode INTEGER DEFAULT 0
        );
    """)
    return conn


class TestGateT2PassBypass(unittest.TestCase):
    """AC2: _gate_t2_pass reconcile_run_id bypass BEFORE existing logic."""

    def test_bypass_with_reconcile_run_id(self):
        from governance.auto_chain import _gate_t2_pass
        conn = _make_db()
        result = {}  # would normally fail — no test_report
        metadata = {"reconcile_run_id": "run-123", "task_id": "t1"}
        passed, reason = _gate_t2_pass(conn, "proj", result, metadata)
        self.assertTrue(passed)
        self.assertIn("reconcile bypass", reason)

    def test_no_bypass_without_reconcile_run_id(self):
        from governance.auto_chain import _gate_t2_pass
        conn = _make_db()
        result = {}
        metadata = {}
        passed, reason = _gate_t2_pass(conn, "proj", result, metadata)
        self.assertFalse(passed)  # should fail — no test_report


class TestGateQaPassBypass(unittest.TestCase):
    """AC2: _gate_qa_pass reconcile_run_id bypass."""

    def test_bypass_with_reconcile_run_id(self):
        from governance.auto_chain import _gate_qa_pass
        conn = _make_db()
        result = {}
        metadata = {"reconcile_run_id": "run-456", "task_id": "t2"}
        passed, reason = _gate_qa_pass(conn, "proj", result, metadata)
        self.assertTrue(passed)
        self.assertIn("reconcile bypass", reason)

    def test_no_bypass_without_reconcile_run_id(self):
        from governance.auto_chain import _gate_qa_pass
        conn = _make_db()
        result = {"recommendation": "reject", "reason": "bad"}
        metadata = {}
        passed, reason = _gate_qa_pass(conn, "proj", result, metadata)
        self.assertFalse(passed)


class TestGateReleaseBypass(unittest.TestCase):
    """AC2: _gate_release reconcile_run_id bypass."""

    def test_bypass_with_reconcile_run_id(self):
        from governance.auto_chain import _gate_release
        conn = _make_db()
        result = {}
        metadata = {"reconcile_run_id": "run-789", "task_id": "t3"}
        passed, reason = _gate_release(conn, "proj", result, metadata)
        self.assertTrue(passed)
        self.assertIn("reconcile bypass", reason)


class TestAuditReconcileBypass(unittest.TestCase):
    """AC4/AC5: _audit_reconcile_bypass writes events and checks high frequency."""

    def test_writes_bypass_event(self):
        from governance.auto_chain import _audit_reconcile_bypass
        conn = _make_db()
        _audit_reconcile_bypass(conn, "proj", "t2_pass", "run-1", "t1")
        row = conn.execute("SELECT * FROM audit_index WHERE event='gate.reconcile_bypass'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["event"], "gate.reconcile_bypass")

    def test_high_frequency_event_when_over_3(self):
        from governance.auto_chain import _audit_reconcile_bypass
        conn = _make_db()
        # Insert 3 existing bypass events within the last hour
        now = datetime.now(timezone.utc)
        for i in range(3):
            ts = (now - timedelta(minutes=i + 1)).isoformat()
            conn.execute(
                "INSERT INTO audit_index (event_id, project_id, event, actor, ok, ts, node_ids) "
                "VALUES (?, ?, 'gate.reconcile_bypass', 'auto-chain', 1, ?, '[]')",
                (f"pre-{i}", "proj", ts),
            )
        # 4th bypass should trigger high_frequency event
        _audit_reconcile_bypass(conn, "proj", "qa_pass", "run-2", "t2")
        hf = conn.execute(
            "SELECT * FROM audit_index WHERE event='gate.reconcile_bypass.high_frequency'"
        ).fetchone()
        self.assertIsNotNone(hf, "high_frequency event should be written when count > 3")

    def test_no_high_frequency_when_under_3(self):
        from governance.auto_chain import _audit_reconcile_bypass
        conn = _make_db()
        _audit_reconcile_bypass(conn, "proj", "release", "run-3", "t3")
        hf = conn.execute(
            "SELECT * FROM audit_index WHERE event='gate.reconcile_bypass.high_frequency'"
        ).fetchone()
        self.assertIsNone(hf)


class TestCheckpointDocBypass(unittest.TestCase):
    """AC3: _gate_checkpoint doc-check section reconcile bypass."""

    @mock.patch("governance.auto_chain._is_governance_internal_repair", return_value=False)
    @mock.patch("governance.auto_chain._compute_gate_static_allowed")
    @mock.patch("governance.auto_chain._get_graph_doc_associations", return_value=[])
    def test_bypass_before_doc_check(self, _mock_gda, _mock_cga, _mock_gir):
        from governance.auto_chain import _gate_checkpoint
        _mock_cga.return_value = ({"agent/governance/auto_chain.py"}, {"agent/governance/auto_chain.py"})
        conn = _make_db()
        result = {"changed_files": ["agent/governance/auto_chain.py"]}
        metadata = {"reconcile_run_id": "run-doc", "task_id": "t4",
                    "target_files": ["agent/governance/auto_chain.py"]}
        passed, reason = _gate_checkpoint(conn, "proj", result, metadata)
        self.assertTrue(passed)
        self.assertIn("reconcile bypass", reason)


if __name__ == "__main__":
    unittest.main()
