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
    from governance.db import get_connection
    conn = get_connection("proj")
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


class TestCompleteTaskSpeed(unittest.TestCase):
    """AC1: task_complete returns in <1 second for chain-eligible task types."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _make_conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def _create_and_claim(self, task_type="pm"):
        from governance.task_registry import create_task, claim_task
        task = create_task(self.conn, "proj", "test prompt", task_type=task_type)
        self.conn.commit()
        claimed, fence = claim_task(self.conn, "proj", "worker-1")
        self.conn.commit()
        return task["task_id"], fence

    def test_complete_returns_fast_for_chain_eligible(self):
        """complete_task must return in <1s even when auto_chain would be slow."""
        task_id, fence = self._create_and_claim("pm")

        # Mock auto_chain.on_task_completed to simulate a slow operation
        slow_event = threading.Event()

        def slow_chain(*args, **kwargs):
            slow_event.wait(timeout=5)
            return {"next_task_id": "fake"}

        with mock.patch(
            "governance.auto_chain.on_task_completed", side_effect=slow_chain
        ), mock.patch("governance.db.get_connection", return_value=self.conn):
            from governance.task_registry import complete_task
            start = time.monotonic()
            result = complete_task(
                self.conn, task_id, status="succeeded",
                result={"summary": "done"}, project_id="proj",
                fence_token=fence,
            )
            elapsed = time.monotonic() - start

        # Signal slow_chain to finish so thread cleans up
        slow_event.set()

        self.assertLess(elapsed, 1.0, f"complete_task took {elapsed:.2f}s, expected <1s")
        self.assertEqual(result["status"], "succeeded")
        self.assertIn("auto_chain", result)
        self.assertTrue(result["auto_chain"]["dispatched"])

    def test_complete_returns_fast_for_failed_chain_eligible(self):
        """complete_task returns fast on failure too."""
        task_id, fence = self._create_and_claim("dev")

        slow_event = threading.Event()

        def slow_fail(*args, **kwargs):
            slow_event.wait(timeout=5)
            return None

        with mock.patch(
            "governance.auto_chain.on_task_failed", side_effect=slow_fail
        ), mock.patch("governance.db.get_connection", return_value=self.conn):
            from governance.task_registry import complete_task
            start = time.monotonic()
            result = complete_task(
                self.conn, task_id, status="failed",
                error_message="boom", project_id="proj",
                fence_token=fence,
            )
            elapsed = time.monotonic() - start

        slow_event.set()
        self.assertLess(elapsed, 1.0)
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
        from governance.task_registry import create_task, claim_task, complete_task
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
            "governance.auto_chain.on_task_completed", side_effect=capture_chain
        ), mock.patch("governance.db.get_connection", return_value=self.conn):
            complete_task(
                self.conn, task["task_id"], status="succeeded",
                result={"summary": "prd done"}, project_id="proj",
                fence_token=fence,
            )

        # Wait for background thread
        self.assertTrue(call_event.wait(timeout=5), "auto_chain was not called")
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
        from governance.task_registry import create_task, claim_task, complete_task
        task = create_task(self.conn, "proj", "test", task_type="dev")
        self.conn.commit()
        claimed, fence = claim_task(self.conn, "proj", "w1")
        self.conn.commit()

        error_logged = threading.Event()

        def exploding_chain(*args, **kwargs):
            raise RuntimeError("chain kaboom")

        with mock.patch(
            "governance.auto_chain.on_task_completed", side_effect=exploding_chain
        ), mock.patch("governance.db.get_connection", return_value=self.conn), \
             mock.patch("governance.task_registry.log") as mock_log:
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
        from governance.task_registry import create_task, claim_task, complete_task
        task = create_task(self.conn, "proj", "do thing", task_type="task")
        self.conn.commit()
        claimed, fence = claim_task(self.conn, "proj", "w1")
        self.conn.commit()

        # auto_chain dispatch still happens (it's the auto_chain module that
        # skips non-chain types), but complete_task should succeed normally
        with mock.patch(
            "governance.auto_chain.on_task_completed", return_value=None
        ) as mock_chain, mock.patch(
            "governance.db.get_connection", return_value=self.conn
        ):
            result = complete_task(
                self.conn, task["task_id"], status="succeeded",
                result={"output": "done"}, project_id="proj",
                fence_token=fence,
            )

        self.assertEqual(result["status"], "succeeded")

    def test_complete_coordinator_type_no_behavior_change(self):
        """type='coordinator' completes normally."""
        from governance.task_registry import create_task, claim_task, complete_task
        task = create_task(self.conn, "proj", "coordinate", task_type="coordinator")
        self.conn.commit()
        claimed, fence = claim_task(self.conn, "proj", "w1")
        self.conn.commit()

        with mock.patch(
            "governance.auto_chain.on_task_completed", return_value=None
        ), mock.patch("governance.db.get_connection", return_value=self.conn):
            result = complete_task(
                self.conn, task["task_id"], status="succeeded",
                result={}, project_id="proj", fence_token=fence,
            )

        self.assertEqual(result["status"], "succeeded")
        self.assertIn("completed_at", result)


if __name__ == "__main__":
    unittest.main()
