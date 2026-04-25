"""Tests for Phase I async baseline creation in auto_chain._finalize_chain.

Covers AC-I2, AC-I7, AC-I10.
"""
import gc
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _safe_cleanup(tmp_dir):
    try:
        gc.collect()
        tmp_dir.cleanup()
    except (PermissionError, OSError):
        try:
            shutil.rmtree(tmp_dir.name, ignore_errors=True)
        except Exception:
            pass


class AsyncBaselineTestBase(unittest.TestCase):
    """Shared setup for async baseline tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self.pid = "test-project"
        os.makedirs(os.path.join(
            self.tmp.name, "codex-tasks", "state", "governance", self.pid
        ), exist_ok=True)
        self._conns = []

    def tearDown(self):
        for c in self._conns:
            try:
                c.close()
            except Exception:
                pass
        os.environ.pop("SHARED_VOLUME_PATH", None)
        _safe_cleanup(self.tmp)

    def _get_conn(self, pid=None):
        from governance.db import get_connection
        conn = get_connection(pid or self.pid)
        self._conns.append(conn)
        return conn


class TestFinalizeChainBaselineHook(AsyncBaselineTestBase):
    """AC-I2: baseline created after _finalize_chain deploy success."""

    @patch("governance.auto_chain._finalize_version_sync")
    @patch("governance.auto_chain._post_manager_redeploy_governance_from_chain")
    def test_ac_i2_baseline_created_after_deploy(self, mock_redeploy, mock_vsync):
        """After _finalize_chain, baseline row exists within 5s."""
        mock_redeploy.return_value = {"ok": False}
        mock_vsync.return_value = None

        conn = self._get_conn()
        # Seed project version
        conn.execute(
            """INSERT OR REPLACE INTO project_version
               (project_id, chain_version, updated_at, updated_by)
               VALUES (?, 'abc1234', '2026-01-01T00:00:00Z', 'test')""",
            (self.pid,),
        )
        conn.commit()

        from governance.auto_chain import _finalize_chain
        result = _finalize_chain(
            conn, self.pid, "task-deploy-001",
            {"report": {}, "merge_commit": "abc1234"},
            {"chain_version": "abc1234"},
        )

        # Wait for async thread
        self.assertTrue(result.get("baseline_thread_started"))
        time.sleep(3)

        # Check DB for baseline row
        check_conn = self._get_conn()
        row = check_conn.execute(
            "SELECT * FROM version_baselines WHERE project_id = ?",
            (self.pid,),
        ).fetchone()
        self.assertIsNotNone(row, "Baseline row should exist after _finalize_chain")
        self.assertEqual(row["triggered_by"], "auto-chain")


class TestFinalizeChainBaselineFailure(AsyncBaselineTestBase):
    """AC-I7: baseline failure does NOT propagate to _finalize_chain.
    AC-I10: failure files backlog row.
    """

    @patch("governance.auto_chain._finalize_version_sync")
    @patch("governance.auto_chain._post_manager_redeploy_governance_from_chain")
    def test_ac_i7_baseline_failure_does_not_propagate(self, mock_redeploy, mock_vsync):
        """_finalize_chain returns successfully even if create_baseline raises."""
        mock_redeploy.return_value = {"ok": False}
        mock_vsync.return_value = None

        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO project_version
               (project_id, chain_version, updated_at, updated_by)
               VALUES (?, 'abc1234', '2026-01-01T00:00:00Z', 'test')""",
            (self.pid,),
        )
        conn.commit()

        with patch("governance.baseline_service.create_baseline", side_effect=RuntimeError("BOOM")):
            from governance.auto_chain import _finalize_chain
            result = _finalize_chain(
                conn, self.pid, "task-deploy-002",
                {"report": {}, "merge_commit": "abc1234"},
                {"chain_version": "abc1234"},
            )

        # _finalize_chain returned successfully despite baseline failure
        self.assertIn("baseline_thread_started", result)
        self.assertTrue(result["baseline_thread_started"])

    @patch("governance.auto_chain._finalize_version_sync")
    @patch("governance.auto_chain._post_manager_redeploy_governance_from_chain")
    @patch("governance.baseline_service.create_baseline", side_effect=RuntimeError("BOOM"))
    def test_ac_i10_failure_files_backlog_row(self, mock_create, mock_redeploy, mock_vsync):
        """When async baseline fails, OPT-BACKLOG-BASELINE-MISSING-B{n} row is filed."""
        mock_redeploy.return_value = {"ok": False}
        mock_vsync.return_value = None

        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO project_version
               (project_id, chain_version, updated_at, updated_by)
               VALUES (?, 'abc1234', '2026-01-01T00:00:00Z', 'test')""",
            (self.pid,),
        )
        conn.commit()

        from governance.auto_chain import _finalize_chain
        _finalize_chain(
            conn, self.pid, "task-deploy-003",
            {"report": {}, "merge_commit": "abc1234"},
            {"chain_version": "abc1234"},
        )

        # Wait for async thread to file backlog (mock stays active via decorator)
        time.sleep(3)

        check_conn = self._get_conn()
        rows = check_conn.execute(
            "SELECT * FROM backlog_bugs WHERE bug_id LIKE 'OPT-BACKLOG-BASELINE-MISSING-B%'"
        ).fetchall()
        self.assertGreater(len(rows), 0, "Backlog row should be filed on baseline failure")
        row = rows[0]
        self.assertEqual(row["priority"], "P1")
        self.assertEqual(row["status"], "OPEN")


if __name__ == "__main__":
    unittest.main()
