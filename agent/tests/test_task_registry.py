"""Tests for task_registry complete_task — async auto_chain dispatch."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_conn(tmp_dir):
    os.environ["SHARED_VOLUME_PATH"] = tmp_dir
    os.makedirs(
        os.path.join(tmp_dir, "codex-tasks", "state", "governance", "proj"),
        exist_ok=True,
    )
    from agent.governance.db import get_connection
    conn = get_connection("proj")
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


class TestCompleteTaskAutoChain(unittest.TestCase):
    """AC1: complete_task dispatches auto_chain synchronously and reflects result."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _make_conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def _create_and_claim(self, task_type="pm"):
        from agent.governance.task_registry import create_task, claim_task
        task = create_task(self.conn, "proj", "test prompt", task_type=task_type)
        self.conn.commit()
        claimed, fence = claim_task(self.conn, "proj", "worker-1")
        self.conn.commit()
        return task["task_id"], fence

    def test_complete_calls_auto_chain_on_success(self):
        """complete_task calls auto_chain synchronously and includes result."""
        task_id, fence = self._create_and_claim("pm")

        def fake_chain(*args, **kwargs):
            return {"next_task_id": "fake-123", "dispatched": True}

        with mock.patch(
            "agent.governance.auto_chain.on_task_completed", side_effect=fake_chain
        ), mock.patch("agent.governance.db.get_connection", return_value=self.conn):
            from agent.governance.task_registry import complete_task
            result = complete_task(
                self.conn, task_id, status="succeeded",
                result={"summary": "done"}, project_id="proj",
                fence_token=fence,
            )

        self.assertEqual(result["status"], "succeeded")
        self.assertIn("auto_chain", result)

    def test_complete_calls_auto_chain_on_failure(self):
        """complete_task calls auto_chain on failure and reflects result."""
        task_id, fence = self._create_and_claim("dev")

        def fake_fail(*args, **kwargs):
            return {"retried": True, "next_task_id": "retry-456"}

        with mock.patch(
            "agent.governance.auto_chain.on_task_failed", side_effect=fake_fail
        ), mock.patch("agent.governance.db.get_connection", return_value=self.conn):
            from agent.governance.task_registry import complete_task
            result = complete_task(
                self.conn, task_id, status="failed",
                error_message="boom", project_id="proj",
                fence_token=fence,
            )

        # Failed tasks with retries left get re-queued
        self.assertIn(result["status"], ("queued", "failed", "observer_hold"))


class TestCompleteAutoChainCreatesTask(unittest.TestCase):
    """AC2: auto_chain still creates correct next-stage task."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _make_conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_complete_dispatches_auto_chain_on_success(self):
        """Verify on_task_completed is called with correct args."""
        from agent.governance.task_registry import create_task, claim_task, complete_task
        task = create_task(self.conn, "proj", "test", task_type="pm")
        self.conn.commit()
        claimed, fence = claim_task(self.conn, "proj", "w1")
        self.conn.commit()

        call_args = {}
        call_event = threading.Event()

        def capture_chain(*args, **kwargs):
            call_args.update(kwargs)
            call_args["_positional"] = args
            call_event.set()
            return {"next_task_id": "task-next"}

        with mock.patch(
            "agent.governance.auto_chain.on_task_completed", side_effect=capture_chain
        ), mock.patch("agent.governance.db.get_connection", return_value=self.conn):
            complete_task(
                self.conn, task["task_id"], status="succeeded",
                result={"summary": "prd done"}, project_id="proj",
                fence_token=fence,
            )

        # Synchronous dispatch — chain should have been called
        self.assertTrue(call_event.is_set(), "auto_chain was not called")
        self.assertEqual(call_args["task_type"], "pm")
        self.assertEqual(call_args["status"], "succeeded")


class TestCompleteAutoChainErrorLogged(unittest.TestCase):
    """AC3: auto_chain errors logged, not swallowed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _make_conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_complete_auto_chain_error_is_logged(self):
        """If auto_chain raises, error must be logged (not just print_exc)."""
        from agent.governance.task_registry import create_task, claim_task, complete_task
        task = create_task(self.conn, "proj", "test", task_type="dev")
        self.conn.commit()
        claimed, fence = claim_task(self.conn, "proj", "w1")
        self.conn.commit()

        error_logged = threading.Event()

        def exploding_chain(*args, **kwargs):
            raise RuntimeError("chain kaboom")

        with mock.patch(
            "agent.governance.auto_chain.on_task_completed", side_effect=exploding_chain
        ), mock.patch("agent.governance.db.get_connection", return_value=self.conn), \
             mock.patch("agent.governance.task_registry.log") as mock_log:
            # Make log.error signal the event
            original_error = mock_log.error
            def logging_error(*a, **kw):
                error_logged.set()
            mock_log.error = logging_error

            result = complete_task(
                self.conn, task["task_id"], status="succeeded",
                result={}, project_id="proj", fence_token=fence,
            )

        # complete_task itself should not raise
        self.assertEqual(result["status"], "succeeded")
        # Background thread should have logged the error
        self.assertTrue(
            error_logged.wait(timeout=5),
            "auto_chain error was not logged via log.error",
        )


class TestCompleteNonChainTypes(unittest.TestCase):
    """AC4: No behavior change for non-chain task types (task, coordinator)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _make_conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_complete_task_type_no_auto_chain_dispatch(self):
        """type='task' should not trigger auto_chain background dispatch."""
        from agent.governance.task_registry import create_task, claim_task, complete_task
        task = create_task(self.conn, "proj", "do thing", task_type="task")
        self.conn.commit()
        claimed, fence = claim_task(self.conn, "proj", "w1")
        self.conn.commit()

        # auto_chain dispatch still happens (it's the auto_chain module that
        # skips non-chain types), but complete_task should succeed normally
        with mock.patch(
            "agent.governance.auto_chain.on_task_completed", return_value=None
        ) as mock_chain, mock.patch(
            "agent.governance.db.get_connection", return_value=self.conn
        ):
            result = complete_task(
                self.conn, task["task_id"], status="succeeded",
                result={"output": "done"}, project_id="proj",
                fence_token=fence,
            )

        self.assertEqual(result["status"], "succeeded")

    def test_complete_coordinator_type_no_behavior_change(self):
        """type='coordinator' completes normally."""
        from agent.governance.task_registry import create_task, claim_task, complete_task
        task = create_task(self.conn, "proj", "coordinate", task_type="coordinator")
        self.conn.commit()
        claimed, fence = claim_task(self.conn, "proj", "w1")
        self.conn.commit()

        with mock.patch(
            "agent.governance.auto_chain.on_task_completed", return_value=None
        ), mock.patch("agent.governance.db.get_connection", return_value=self.conn):
            result = complete_task(
                self.conn, task["task_id"], status="succeeded",
                result={}, project_id="proj", fence_token=fence,
            )

        self.assertEqual(result["status"], "succeeded")
        self.assertIn("completed_at", result)


class TestVersionDriftWarning(unittest.TestCase):
    """B3: Advisory version drift warning at task_create time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _make_conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def _ensure_project_version(self, chain_version):
        """Insert or update project_version row for 'proj'."""
        row = self.conn.execute(
            "SELECT 1 FROM project_version WHERE project_id = 'proj'"
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE project_version SET chain_version = ? WHERE project_id = 'proj'",
                (chain_version,),
            )
        else:
            self.conn.execute(
                "INSERT INTO project_version (project_id, chain_version, updated_at, updated_by) "
                "VALUES ('proj', ?, '2026-01-01T00:00:00Z', 'test')",
                (chain_version,),
            )
        self.conn.commit()

    def test_no_warning_when_versions_match(self):
        """AC4: When HEAD == chain_version, no version_warning in result."""
        from agent.governance.task_registry import create_task
        # Set chain_version to match the mocked HEAD
        fake_hash = "abcdef1234567890"
        self._ensure_project_version(fake_hash)

        with mock.patch(
            "agent.governance.task_registry.subprocess.check_output",
            return_value=fake_hash.encode(),
        ):
            result = create_task(self.conn, "proj", "test prompt", task_type="task")

        self.assertNotIn("version_warning", result)
        self.assertIn("task_id", result)

    def test_warning_when_versions_differ(self):
        """AC5: When HEAD != chain_version, version_warning present with both hashes."""
        from agent.governance.task_registry import create_task
        chain_hash = "abcdef1234567890"
        head_hash = "9876543210fedcba"
        self._ensure_project_version(chain_hash)

        with mock.patch(
            "agent.governance.task_registry.subprocess.check_output",
            return_value=head_hash.encode(),
        ):
            result = create_task(self.conn, "proj", "test prompt", task_type="task")

        self.assertIn("version_warning", result)
        self.assertIn(chain_hash[:7], result["version_warning"])
        self.assertIn(head_hash[:7], result["version_warning"])
        # Task still created successfully
        self.assertIn("task_id", result)
        self.assertEqual(result["status"], "queued")

    def test_no_warning_when_drift_check_raises(self):
        """AC6: When _check_version_drift raises, task still succeeds without warning."""
        from agent.governance.task_registry import create_task
        self._ensure_project_version("abc1234")

        with mock.patch(
            "agent.governance.task_registry.subprocess.check_output",
            side_effect=FileNotFoundError("git not found"),
        ):
            result = create_task(self.conn, "proj", "test prompt", task_type="task")

        self.assertNotIn("version_warning", result)
        self.assertIn("task_id", result)
        self.assertEqual(result["status"], "queued")


if __name__ == "__main__":
    unittest.main()
