"""Tests for retry heartbeat reset fix - prevent coordinator false timeout after retry.

TC-01: Retry resets heartbeat_at (via retry_task clearing + mark_task_started refresh)
TC-02: Coordinator doesn't falsely timeout retried task
TC-03: Genuine timeout still detected
TC-04: First execution without heartbeat_at not affected
TC-05: Heartbeat thread works normally after retry
"""
import datetime
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from utils import save_json, load_json, task_file, tasks_root, utc_iso  # noqa: E402
from task_state import (  # noqa: E402
    load_task_status,
    mark_task_started,
    register_task_created,
    save_task_status,
    update_task_heartbeat,
    update_task_runtime,
)
from task_retry import retry_task  # noqa: E402


def _utc_iso_offset(seconds: int) -> str:
    """Return UTC ISO timestamp offset from now by `seconds` (negative = past)."""
    dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_rejected_task(task_id="task-hb-001", task_code="T0099",
                        heartbeat_age_sec=1860):
    """Create a rejected task dict with an old heartbeat_at from previous execution."""
    return {
        "task_id": task_id,
        "task_code": task_code,
        "chat_id": 123,
        "requested_by": 456,
        "action": "codex",
        "text": "implement something",
        "status": "rejected",
        "_stage": "results",
        "created_at": _utc_iso_offset(-7200),
        "started_at": _utc_iso_offset(-heartbeat_age_sec - 60),
        "updated_at": utc_iso(),
        "_git_checkpoint": "abc123",
        "executor": {
            "action": "codex",
            "elapsed_ms": 5000,
            "returncode": 0,
            "last_message": "done",
        },
        "acceptance": {
            "state": "rejected",
            "acceptance_required": True,
            "archive_allowed": False,
            "gate_rule": "only_after_user_accept",
            "rejected_at": utc_iso(),
            "rejected_by": 456,
            "reason": "not good enough",
            "iteration_count": 1,
            "rejection_history": [],
            "updated_at": utc_iso(),
            "doc_file": "",
            "cases_file": "",
        },
    }


class RetryHeartbeatTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self._git_patch = patch(
            "task_retry.pre_task_checkpoint",
            return_value={"checkpoint_commit": "def456", "error": ""},
        )
        self._git_patch.start()

    def tearDown(self):
        self._git_patch.stop()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def _setup_rejected_task_with_old_heartbeat(self, task_id="task-hb-001",
                                                 heartbeat_age_sec=1860):
        """Register a rejected task and inject an old heartbeat_at into status.json."""
        task = _make_rejected_task(task_id=task_id, heartbeat_age_sec=heartbeat_age_sec)
        result_path = task_file("results", task_id)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(result_path, task)
        register_task_created(task)
        update_task_runtime(task, status="rejected", stage="results")

        # Inject stale heartbeat_at into status.json
        old_hb = _utc_iso_offset(-heartbeat_age_sec)
        st = load_task_status(task_id)
        st["heartbeat_at"] = old_hb
        st["started_at"] = _utc_iso_offset(-heartbeat_age_sec - 60)
        save_task_status(task_id, st)

        return task, old_hb


class TestTC01_RetryResetsHeartbeat(RetryHeartbeatTestBase):
    """TC-01: After retry, heartbeat_at is reset (cleared), not the old value."""

    def test_heartbeat_at_cleared_after_retry(self):
        """retry_task clears heartbeat_at so coordinator won't see stale value."""
        task, old_hb = self._setup_rejected_task_with_old_heartbeat()
        task_id = task["task_id"]

        # Verify old heartbeat is there before retry
        before = load_task_status(task_id)
        self.assertEqual(before["heartbeat_at"], old_hb)

        # Perform retry
        success, msg, updated = retry_task(task, user_id=456)
        self.assertTrue(success)

        # Check status.json after retry
        after = load_task_status(task_id)
        self.assertEqual(after["heartbeat_at"], "",
                         "heartbeat_at should be cleared after retry")
        self.assertEqual(after["started_at"], "",
                         "started_at should be cleared after retry")

    def test_old_heartbeat_not_preserved(self):
        """Old heartbeat value from previous execution is gone after retry."""
        task, old_hb = self._setup_rejected_task_with_old_heartbeat(
            task_id="task-hb-001b", heartbeat_age_sec=3600,
        )

        success, msg, _ = retry_task(task, user_id=456)
        self.assertTrue(success)

        after = load_task_status(task["task_id"])
        self.assertNotEqual(after["heartbeat_at"], old_hb,
                            "Old heartbeat_at must not be preserved after retry")

    def test_mark_task_started_sets_fresh_heartbeat(self):
        """After retry + mark_task_started, heartbeat_at is a fresh timestamp."""
        task, old_hb = self._setup_rejected_task_with_old_heartbeat(task_id="task-hb-001c")

        success, msg, _ = retry_task(task, user_id=456)
        self.assertTrue(success)

        # Simulate executor picking up and calling mark_task_started
        now_before = datetime.datetime.now(datetime.timezone.utc)
        obj = mark_task_started(
            {"task_id": task["task_id"], "task_code": task["task_code"], "chat_id": 123},
            stage="processing",
        )

        # heartbeat_at should be within 5s of now
        hb_dt = datetime.datetime.strptime(obj["heartbeat_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
        age_sec = abs((now_before - hb_dt).total_seconds())
        self.assertLess(age_sec, 5.0,
                        "heartbeat_at should be within 5s of now after mark_task_started")


class TestTC02_CoordinatorNoFalseTimeout(RetryHeartbeatTestBase):
    """TC-02: Coordinator doesn't falsely timeout a just-retried task."""

    @patch("coordinator.send_text")
    def test_retry_then_processing_not_timed_out(self, mock_send):
        """After retry → mark_task_started, coordinator should NOT mark timeout."""
        from coordinator import maybe_timeout_stale_tasks

        task, old_hb = self._setup_rejected_task_with_old_heartbeat(task_id="task-hb-002")

        # Retry the task
        success, msg, _ = retry_task(task, user_id=456)
        self.assertTrue(success)

        # Simulate executor: mark_task_started resets heartbeat_at
        mark_task_started(
            {"task_id": task["task_id"], "task_code": task["task_code"], "chat_id": 123},
            stage="processing",
        )

        # Coordinator timeout check
        maybe_timeout_stale_tasks()

        after = load_task_status(task["task_id"])
        self.assertEqual(after["status"], "processing",
                         "Retried task should NOT be marked as timeout")

    @patch("coordinator.send_text")
    def test_retry_pending_state_not_timed_out(self, mock_send):
        """While task is still 'pending' after retry, coordinator skips it."""
        from coordinator import maybe_timeout_stale_tasks

        task, _ = self._setup_rejected_task_with_old_heartbeat(task_id="task-hb-002b")

        success, msg, _ = retry_task(task, user_id=456)
        self.assertTrue(success)

        # Coordinator checks - status is "pending" so it should be skipped
        maybe_timeout_stale_tasks()

        after = load_task_status(task["task_id"])
        self.assertEqual(after["status"], "pending",
                         "Pending task should not be touched by timeout check")

    @patch("coordinator.send_text")
    def test_retry_cleared_heartbeat_prevents_false_timeout_in_processing(self, mock_send):
        """Even if executor only calls update_task_runtime (not yet mark_task_started),
        the cleared heartbeat_at prevents false timeout because ts_str is empty."""
        from coordinator import maybe_timeout_stale_tasks

        task, _ = self._setup_rejected_task_with_old_heartbeat(task_id="task-hb-002c")

        success, msg, _ = retry_task(task, user_id=456)
        self.assertTrue(success)

        # Simulate executor calling update_task_runtime but NOT yet mark_task_started
        # (the tiny window between the two calls)
        update_task_runtime(task, status="processing", stage="processing")

        # heartbeat_at and started_at were cleared by retry_task, so coordinator
        # should find empty ts_str and skip this task
        st = load_task_status(task["task_id"])
        self.assertEqual(st["status"], "processing")

        maybe_timeout_stale_tasks()

        after = load_task_status(task["task_id"])
        self.assertEqual(after["status"], "processing",
                         "Task with cleared timestamps should NOT be timed out")


class TestTC03_GenuineTimeoutStillDetected(unittest.TestCase):
    """TC-03: Real stale tasks (no heartbeat >1800s) are correctly timed out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("coordinator.send_text")
    def test_stale_processing_task_times_out(self, mock_send):
        """Task with heartbeat_at 1801s ago and status=processing is marked timeout."""
        from coordinator import maybe_timeout_stale_tasks

        task = {
            "task_id": "task-stale-003",
            "task_code": "T0050",
            "chat_id": 123,
            "requested_by": 456,
            "action": "codex",
            "text": "stale task",
            "status": "processing",
        }
        register_task_created(task)
        update_task_runtime(task, status="processing", stage="processing")

        # Set heartbeat_at to 1801 seconds ago
        st = load_task_status(task["task_id"])
        st["heartbeat_at"] = _utc_iso_offset(-1801)
        st["status"] = "processing"
        save_task_status(task["task_id"], st)

        os.environ["TASK_TIMEOUT_SEC"] = "1800"
        try:
            maybe_timeout_stale_tasks()
        finally:
            os.environ.pop("TASK_TIMEOUT_SEC", None)

        after = load_task_status(task["task_id"])
        self.assertEqual(after["status"], "timeout",
                         "Genuinely stale task must be marked as timeout")
        self.assertIn("no heartbeat", after.get("error", "").lower())

    @patch("coordinator.send_text")
    def test_stale_started_at_fallback_times_out(self, mock_send):
        """Task with only started_at (no heartbeat_at) beyond threshold is timed out."""
        from coordinator import maybe_timeout_stale_tasks

        task = {
            "task_id": "task-stale-003b",
            "task_code": "T0051",
            "chat_id": 123,
            "requested_by": 456,
            "action": "codex",
            "text": "stale fallback task",
            "status": "processing",
        }
        register_task_created(task)

        st = load_task_status(task["task_id"]) or {}
        st.update({
            "task_id": task["task_id"],
            "task_code": "T0051",
            "chat_id": 123,
            "status": "processing",
            "heartbeat_at": "",
            "started_at": _utc_iso_offset(-1801),
        })
        save_task_status(task["task_id"], st)

        os.environ["TASK_TIMEOUT_SEC"] = "1800"
        try:
            maybe_timeout_stale_tasks()
        finally:
            os.environ.pop("TASK_TIMEOUT_SEC", None)

        after = load_task_status(task["task_id"])
        self.assertEqual(after["status"], "timeout")


class TestTC04_FirstExecutionNotAffected(unittest.TestCase):
    """TC-04: New task without heartbeat_at (first execution) is not falsely timed out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("coordinator.send_text")
    def test_new_task_with_recent_started_at(self, mock_send):
        """New processing task with started_at 60s ago, no heartbeat_at, is NOT timed out."""
        from coordinator import maybe_timeout_stale_tasks

        task = {
            "task_id": "task-new-004",
            "task_code": "T0060",
            "chat_id": 123,
            "requested_by": 456,
            "action": "codex",
            "text": "new task",
            "status": "processing",
        }
        register_task_created(task)

        st = load_task_status(task["task_id"]) or {}
        st.update({
            "task_id": task["task_id"],
            "task_code": "T0060",
            "chat_id": 123,
            "status": "processing",
            "started_at": _utc_iso_offset(-60),
            "heartbeat_at": "",
        })
        save_task_status(task["task_id"], st)

        os.environ["TASK_TIMEOUT_SEC"] = "1800"
        try:
            maybe_timeout_stale_tasks()
        finally:
            os.environ.pop("TASK_TIMEOUT_SEC", None)

        after = load_task_status(task["task_id"])
        self.assertEqual(after["status"], "processing",
                         "New task with recent started_at should NOT be timed out")

    @patch("coordinator.send_text")
    def test_no_timestamps_skipped(self, mock_send):
        """Task with neither heartbeat_at nor started_at is skipped by coordinator."""
        from coordinator import maybe_timeout_stale_tasks

        task = {
            "task_id": "task-empty-004b",
            "task_code": "T0061",
            "chat_id": 123,
            "requested_by": 456,
            "action": "codex",
            "text": "empty ts task",
            "status": "processing",
        }
        register_task_created(task)

        st = load_task_status(task["task_id"]) or {}
        st.update({
            "task_id": task["task_id"],
            "task_code": "T0061",
            "chat_id": 123,
            "status": "processing",
            "started_at": "",
            "heartbeat_at": "",
        })
        save_task_status(task["task_id"], st)

        os.environ["TASK_TIMEOUT_SEC"] = "1800"
        try:
            maybe_timeout_stale_tasks()
        finally:
            os.environ.pop("TASK_TIMEOUT_SEC", None)

        after = load_task_status(task["task_id"])
        self.assertEqual(after["status"], "processing",
                         "Task with no timestamps should be skipped")


class TestTC05_HeartbeatThreadAfterRetry(RetryHeartbeatTestBase):
    """TC-05: Heartbeat thread works normally after retry scenario."""

    def test_heartbeat_loop_updates_after_retry(self):
        """Heartbeat thread updates heartbeat_at; timestamps are monotonically increasing."""
        from executor import _heartbeat_loop

        task, _ = self._setup_rejected_task_with_old_heartbeat(task_id="task-hb-005")

        # Retry
        success, msg, _ = retry_task(task, user_id=456)
        self.assertTrue(success)

        # Simulate executor starting
        mark_task_started(
            {"task_id": task["task_id"], "task_code": task["task_code"], "chat_id": 123},
            stage="processing",
        )

        initial_hb = load_task_status(task["task_id"])["heartbeat_at"]
        self.assertTrue(initial_hb, "heartbeat_at should be set after mark_task_started")

        # Run heartbeat loop with short interval
        stop_event = threading.Event()
        hb_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(task["task_id"], stop_event, 0.1),  # 100ms interval
            daemon=True,
        )
        hb_thread.start()

        import time
        time.sleep(0.35)  # Wait for ~3 heartbeat cycles
        stop_event.set()
        hb_thread.join(timeout=2)

        after_hb = load_task_status(task["task_id"])["heartbeat_at"]
        self.assertTrue(after_hb)
        self.assertGreaterEqual(after_hb, initial_hb,
                                "heartbeat_at should be monotonically increasing")

    def test_update_task_heartbeat_works_for_retried_task(self):
        """update_task_heartbeat directly updates heartbeat_at for a retried task."""
        task, _ = self._setup_rejected_task_with_old_heartbeat(task_id="task-hb-005b")

        success, msg, _ = retry_task(task, user_id=456)
        self.assertTrue(success)

        mark_task_started(
            {"task_id": task["task_id"], "task_code": task["task_code"], "chat_id": 123},
            stage="processing",
        )

        before = load_task_status(task["task_id"])["heartbeat_at"]

        import time
        time.sleep(1.1)  # Ensure at least 1s difference

        update_task_heartbeat(task["task_id"])
        after = load_task_status(task["task_id"])["heartbeat_at"]

        self.assertGreater(after, before,
                           "update_task_heartbeat should refresh the timestamp")


if __name__ == "__main__":
    unittest.main()
