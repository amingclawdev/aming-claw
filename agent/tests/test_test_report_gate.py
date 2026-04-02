"""Tests for the test_report validation gate in task_registry.complete_task().

Covers AC1-AC5: test-type tasks with status='succeeded' must have a valid
test_report dict containing 'passed' and 'failed' keys.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_conn(tmp_dir):
    os.environ["SHARED_VOLUME_PATH"] = tmp_dir
    os.makedirs(
        os.path.join(tmp_dir, "codex-tasks", "state", "governance", "proj"),
        exist_ok=True,
    )
    from governance.db import get_connection
    conn = get_connection("proj")
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


def _create_and_claim(conn, task_type="test"):
    from governance.task_registry import create_task, claim_task
    task = create_task(conn, "proj", "test prompt", task_type=task_type)
    conn.commit()
    claimed, fence = claim_task(conn, "proj", "worker-1")
    conn.commit()
    return task["task_id"], fence


class TestTestReportGate(unittest.TestCase):
    """Validation gate: test-type succeeded tasks must include test_report."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _make_conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_malformed_test_success_rejected(self):
        """AC3: test task succeeded with missing test_report raises ValidationError."""
        from governance.errors import ValidationError
        from governance.task_registry import complete_task

        task_id, fence = _create_and_claim(self.conn, task_type="test")

        with self.assertRaises(ValidationError) as ctx:
            complete_task(
                self.conn, task_id, status="succeeded",
                result={"summary": "all passed"},
                project_id="proj", fence_token=fence,
            )

        self.assertIn("test_report", str(ctx.exception))

        # Verify DB was NOT updated (task should still be claimed, not succeeded)
        row = self.conn.execute(
            "SELECT execution_status FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        self.assertNotEqual(row["execution_status"], "succeeded",
                            "Task should not be persisted as succeeded without test_report")

    def test_valid_test_success_accepted(self):
        """AC2: test task succeeded with valid test_report persists to DB."""
        from governance.task_registry import complete_task

        task_id, fence = _create_and_claim(self.conn, task_type="test")

        with mock.patch(
            "governance.auto_chain.on_task_completed", return_value=None
        ), mock.patch("governance.db.get_connection", return_value=self.conn):
            result = complete_task(
                self.conn, task_id, status="succeeded",
                result={"test_report": {"passed": 5, "failed": 0, "tool": "pytest"}},
                project_id="proj", fence_token=fence,
            )

        self.assertEqual(result["status"], "succeeded")

        # Verify DB was updated
        row = self.conn.execute(
            "SELECT execution_status, result_json FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        self.assertEqual(row["execution_status"], "succeeded")
        stored = json.loads(row["result_json"])
        self.assertEqual(stored["test_report"]["passed"], 5)

    def test_failed_test_no_report_accepted(self):
        """AC4: test task with status='failed' and no test_report succeeds."""
        from governance.task_registry import complete_task

        task_id, fence = _create_and_claim(self.conn, task_type="test")

        with mock.patch(
            "governance.auto_chain.on_task_failed", return_value=None
        ), mock.patch("governance.db.get_connection", return_value=self.conn):
            result = complete_task(
                self.conn, task_id, status="failed",
                result={}, error_message="timeout",
                project_id="proj", fence_token=fence,
            )

        # Should not raise — validation only applies to succeeded
        self.assertIn(result["status"], ("queued", "failed", "observer_hold"))

    def test_non_test_task_no_report_accepted(self):
        """AC5: non-test task (type='dev') succeeded with empty result succeeds."""
        from governance.task_registry import complete_task

        task_id, fence = _create_and_claim(self.conn, task_type="dev")

        with mock.patch(
            "governance.auto_chain.on_task_completed", return_value=None
        ), mock.patch("governance.db.get_connection", return_value=self.conn):
            result = complete_task(
                self.conn, task_id, status="succeeded",
                result={}, project_id="proj", fence_token=fence,
            )

        self.assertEqual(result["status"], "succeeded")

    def test_test_report_missing_passed_key_rejected(self):
        """test_report dict without 'passed' key is rejected."""
        from governance.errors import ValidationError
        from governance.task_registry import complete_task

        task_id, fence = _create_and_claim(self.conn, task_type="test")

        with self.assertRaises(ValidationError):
            complete_task(
                self.conn, task_id, status="succeeded",
                result={"test_report": {"failed": 0}},
                project_id="proj", fence_token=fence,
            )

    def test_test_report_not_dict_rejected(self):
        """test_report that is not a dict is rejected."""
        from governance.errors import ValidationError
        from governance.task_registry import complete_task

        task_id, fence = _create_and_claim(self.conn, task_type="test")

        with self.assertRaises(ValidationError):
            complete_task(
                self.conn, task_id, status="succeeded",
                result={"test_report": "all passed"},
                project_id="proj", fence_token=fence,
            )


if __name__ == "__main__":
    unittest.main()
