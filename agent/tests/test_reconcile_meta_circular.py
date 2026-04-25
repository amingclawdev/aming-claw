"""Tests for meta-circular reconcile validation (AC-J7, AC-J6, AC-J9).

Covers: _meta_circular=true validation, approve skip, backlog filing,
commit prefix, regression detection in verify stage.
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


class TestMetaCircularValidation(unittest.TestCase):
    """AC-J7: _meta_circular=true validation rules (R6)."""

    def test_valid_scenarios(self):
        from governance.reconcile_task import validate_meta_circular
        valid_scenarios = [
            "chain_broken", "gov_wedge", "deploy_selfkill",
            "graph_corrupted", "b48_precedent",
        ]
        for scenario in valid_scenarios:
            meta = {
                "scenario": scenario,
                "reason": "x" * 50,  # exactly 50 chars
                "observer_acknowledged_by": "observer-1",
            }
            ok, err = validate_meta_circular(meta, "task-1")
            self.assertTrue(ok, f"Scenario {scenario} should be valid: {err}")

    def test_invalid_scenario_rejected(self):
        from governance.reconcile_task import validate_meta_circular
        meta = {
            "scenario": "not_a_real_scenario",
            "reason": "x" * 50,
            "observer_acknowledged_by": "observer-1",
        }
        ok, err = validate_meta_circular(meta, "task-1")
        self.assertFalse(ok)
        self.assertIn("Invalid meta-circular scenario", err)

    def test_short_reason_rejected(self):
        from governance.reconcile_task import validate_meta_circular
        meta = {
            "scenario": "chain_broken",
            "reason": "too short",  # < 50 chars
            "observer_acknowledged_by": "observer-1",
        }
        ok, err = validate_meta_circular(meta, "task-1")
        self.assertFalse(ok)
        self.assertIn("50 chars", err)

    def test_missing_observer_rejected(self):
        from governance.reconcile_task import validate_meta_circular
        meta = {
            "scenario": "chain_broken",
            "reason": "x" * 60,
            "observer_acknowledged_by": "",
        }
        ok, err = validate_meta_circular(meta, "task-1")
        self.assertFalse(ok)
        self.assertIn("observer_acknowledged_by", err)


class TestMetaCircularApproveSkip(unittest.TestCase):
    """AC-J7: _meta_circular=true skips approve stage."""

    def test_approve_skipped_with_meta_circular(self):
        from governance.reconcile_task import (
            handle_approve, _write_mutation_plan, _read_mutation_plan,
        )
        conn = _make_in_memory_db()
        conn.execute(
            "INSERT INTO tasks (task_id, project_id, status, execution_status, type) "
            "VALUES ('task-mc-1', 'proj1', 'running', 'running', 'reconcile')"
        )
        conn.commit()

        plan = {
            "task_id": "task-mc-1",
            "baseline_id_before": "abc",
            "phases_run": ["scan", "diff", "propose"],
            "mutations": [
                {"action": "node-create", "node_id": "L1.1", "confidence": "medium"},
            ],
            "summary": "test",
            "approve_threshold": "high",
            "applied_count": 0,
        }

        metadata = {
            "_meta_circular": True,
            "scenario": "chain_broken",
            "reason": "Chain is broken and needs immediate repair by reconcile system",
            "observer_acknowledged_by": "observer-main",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("governance.reconcile_task._MUTATION_PLAN_DIR",
                            os.path.join(tmpdir, "{pid}", "mutation_plans")):
                _write_mutation_plan("proj1", "task-mc-1", plan)
                result = handle_approve(conn, "proj1", "task-mc-1", metadata, None)

                self.assertTrue(result["meta_circular"])
                self.assertEqual(result["commit_prefix"], "[reconcile-meta-circular]")
                self.assertTrue(result["all_approved"])
                # Medium confidence should be auto-approved in meta-circular mode
                self.assertEqual(result["approved_count"], 1)
                self.assertEqual(result["queued_count"], 0)


class TestMetaCircularBacklog(unittest.TestCase):
    """AC-J7: _meta_circular=true auto-files backlog row."""

    def test_backlog_filed(self):
        from governance.reconcile_task import _file_meta_circular_backlog
        conn = _make_in_memory_db()
        metadata = {
            "scenario": "gov_wedge",
            "reason": "Governance wedged, needs repair" + "x" * 40,
            "observer_acknowledged_by": "observer-1",
        }
        _file_meta_circular_backlog(conn, "proj1", "task-mc-2", metadata)
        conn.commit()

        row = conn.execute(
            "SELECT * FROM backlog_bugs WHERE bug_id = ?",
            ("OPT-BACKLOG-META-CIRCULAR-REVIEW-task-mc-2",),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["priority"], "P1")
        self.assertEqual(row["project_id"], "proj1")
        # Check 7-day expiry
        self.assertIsNotNone(row["expires_at"])

    def test_backlog_id_format(self):
        """Backlog ID follows OPT-BACKLOG-META-CIRCULAR-REVIEW-{task_id} format."""
        task_id = "task-abc-123"
        expected = f"OPT-BACKLOG-META-CIRCULAR-REVIEW-{task_id}"
        self.assertEqual(expected, "OPT-BACKLOG-META-CIRCULAR-REVIEW-task-abc-123")


class TestVerifyStageRegression(unittest.TestCase):
    """AC-J9: Verify stage detects regression."""

    def test_verify_flags_regressions(self):
        from governance.reconcile_task import handle_verify

        conn = _make_in_memory_db()
        project_id = "proj-verify"
        task_id = "task-verify-1"

        conn.execute(
            "INSERT INTO project_version (project_id, chain_version) VALUES (?, ?)",
            (project_id, "abc"),
        )
        conn.execute(
            "INSERT INTO tasks (task_id, project_id, status, execution_status, type) "
            "VALUES (?, ?, 'running', 'running', 'reconcile')",
            (task_id, project_id),
        )
        # Insert node states
        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status, build_status) "
            "VALUES (?, 'L1.1', 'pending', 'impl:missing')",
            (project_id,),
        )
        conn.commit()

        prev = {"applied_count": 1}
        # Patch load_project_graph to return a graph with an extra node
        mock_graph = mock.MagicMock()
        mock_graph.G.nodes.return_value = {"L1.1", "L1.99"}
        with mock.patch("governance.reconcile_task.handle_diff") as mock_diff:
            # Simulate diff finding a high-severity issue
            mock_diff.return_value = {
                "stage": "diff",
                "diffs": [{"node_id": "L1.99", "type": "missing_in_db", "severity": "high"}],
                "diff_count": 1,
                "baseline": {},
            }
            result = handle_verify(conn, project_id, task_id, {}, prev)

        self.assertEqual(result["stage"], "verify")
        # L1.99 is in graph but not in DB -> regression
        self.assertGreater(result["regression_count"], 0)
        self.assertFalse(result["verified"])

    def test_verify_no_regression(self):
        from governance.reconcile_task import handle_verify

        conn = _make_in_memory_db()
        project_id = "proj-verify2"
        task_id = "task-verify-2"

        conn.execute(
            "INSERT INTO project_version (project_id, chain_version) VALUES (?, ?)",
            (project_id, "abc"),
        )
        conn.execute(
            "INSERT INTO tasks (task_id, project_id, status, execution_status, type) "
            "VALUES (?, ?, 'running', 'running', 'reconcile')",
            (task_id, project_id),
        )
        conn.commit()

        # No nodes, no graph -> no diffs -> no regressions
        mock_ps = mock.MagicMock()
        mock_ps.load_project_graph.return_value = None
        with mock.patch.dict(sys.modules, {"governance.project_service": mock_ps}):
            result = handle_verify(conn, project_id, task_id, {}, {"applied_count": 0})

        self.assertEqual(result["stage"], "verify")
        self.assertEqual(result["regression_count"], 0)
        self.assertTrue(result["verified"])


class TestMetaCircularInvalidReject(unittest.TestCase):
    """R6: Invalid meta-circular metadata causes approve to raise."""

    def test_approve_raises_on_invalid_meta_circular(self):
        from governance.reconcile_task import handle_approve, _write_mutation_plan

        conn = _make_in_memory_db()
        conn.execute(
            "INSERT INTO tasks (task_id, project_id, status, execution_status, type) "
            "VALUES ('task-mc-bad', 'proj1', 'running', 'running', 'reconcile')"
        )
        conn.commit()

        plan = {
            "task_id": "task-mc-bad",
            "baseline_id_before": "abc",
            "phases_run": ["scan", "diff", "propose"],
            "mutations": [],
            "summary": "test",
            "approve_threshold": "high",
            "applied_count": 0,
        }

        metadata = {
            "_meta_circular": True,
            "scenario": "invalid_scenario",
            "reason": "short",
            "observer_acknowledged_by": "",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("governance.reconcile_task._MUTATION_PLAN_DIR",
                            os.path.join(tmpdir, "{pid}", "mutation_plans")):
                _write_mutation_plan("proj1", "task-mc-bad", plan)
                with self.assertRaises(ValueError) as ctx:
                    handle_approve(conn, "proj1", "task-mc-bad", metadata, None)
                self.assertIn("Meta-circular validation failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
