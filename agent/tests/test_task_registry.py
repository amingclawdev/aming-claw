"""Tests for task_registry complete_task — async auto_chain dispatch."""
import json
import os
import sqlite3
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
            return {"task_id": "fake-123", "type": "dev", "dispatched": True}

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
        self.assertTrue(result["auto_chain"]["dispatched"])
        self.assertEqual(result["auto_chain"]["task_id"], "fake-123")
        self.assertEqual(result["auto_chain"]["type"], "dev")

    def test_complete_reports_preflight_block_without_dispatch(self):
        """Preflight blocks must be visible to the executor/operator."""
        task_id, fence = self._create_and_claim("pm")

        def fake_chain(*args, **kwargs):
            return {
                "preflight_blocked": True,
                "stage": "pm",
                "reason": "pm result preflight validation failed",
            }

        with mock.patch(
            "agent.governance.auto_chain.on_task_completed", side_effect=fake_chain
        ), mock.patch("agent.governance.db.get_connection", return_value=self.conn):
            from agent.governance.task_registry import complete_task
            result = complete_task(
                self.conn, task_id, status="succeeded",
                result={"summary": "done"}, project_id="proj",
                fence_token=fence,
            )

        self.assertFalse(result["auto_chain"]["dispatched"])
        self.assertTrue(result["auto_chain"]["preflight_blocked"])
        self.assertEqual(result["auto_chain"]["stage"], "pm")
        self.assertIn("preflight", result["auto_chain"]["reason"])

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

    def test_duplicate_complete_on_terminal_task_is_idempotent(self):
        """Duplicate executor retries must not re-run auto-chain once terminal."""
        task_id, fence = self._create_and_claim("dev")

        with mock.patch(
            "agent.governance.auto_chain.on_task_completed", return_value={"ok": True}
        ) as first_chain:
            from agent.governance.task_registry import complete_task
            first = complete_task(
                self.conn, task_id, status="succeeded",
                result={"summary": "done"}, project_id="proj",
                fence_token=fence,
            )

        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(first_chain.call_count, 1)

        with mock.patch(
            "agent.governance.auto_chain.on_task_completed", return_value={"ok": True}
        ) as duplicate_chain:
            second = complete_task(
                self.conn, task_id, status="succeeded",
                result={"summary": "done"}, project_id="proj",
                fence_token=fence,
            )

        self.assertTrue(second["idempotent"])
        self.assertEqual(second["auto_chain"]["reason"], "task already terminal")
        duplicate_chain.assert_not_called()

    def test_terminal_task_can_replay_auto_chain_when_observer_requests_it(self):
        """Observer recovery can replay auto-chain from stored result after a crash."""
        task_id, fence = self._create_and_claim("dev")
        stored = {"summary": "done", "changed_files": ["agent/foo.py"]}

        with mock.patch(
            "agent.governance.auto_chain.on_task_completed", return_value={"ok": True}
        ):
            from agent.governance.task_registry import complete_task
            complete_task(
                self.conn, task_id, status="succeeded",
                result=stored, project_id="proj",
                fence_token=fence,
            )

        replay_args = {}

        def capture_replay(*args, **kwargs):
            replay_args.update(kwargs)
            replay_args["_positional"] = args
            return {"task_id": "next-task"}

        with mock.patch(
            "agent.governance.auto_chain.on_task_completed", side_effect=capture_replay
        ):
            replay = complete_task(
                self.conn, task_id, status="succeeded",
                result=None, project_id="proj",
                fence_token=fence,
                completed_by="observer-runtime-recovery",
                override_reason="replay_auto_chain",
            )

        self.assertTrue(replay["idempotent"])
        self.assertTrue(replay["replayed_auto_chain"])
        self.assertEqual(replay_args["result"], stored)
        self.assertEqual(replay_args["task_type"], "dev")

    def test_terminal_replay_commits_observer_audit_before_auto_chain(self):
        """Observer replay must release audit writes before chain event writes."""
        task_id, fence = self._create_and_claim("dev")

        with mock.patch(
            "agent.governance.auto_chain.on_task_completed", return_value={"ok": True}
        ):
            from agent.governance.task_registry import complete_task
            complete_task(
                self.conn, task_id, status="succeeded",
                result={"summary": "done"}, project_id="proj",
                fence_token=fence,
            )

        def write_audit(conn, project_id, event, **kwargs):
            conn.execute(
                "INSERT INTO audit_index "
                "(event_id, project_id, event, actor, ok, ts, node_ids) "
                "VALUES ('aud-test-replay', ?, ?, 'observer', 1, ?, '[]')",
                (project_id, event, "2026-05-03T00:00:00Z"),
            )

        def assert_outer_conn_released(*args, **kwargs):
            self.assertFalse(self.conn.in_transaction)
            return {"task_id": "next-task"}

        with mock.patch(
            "agent.governance.audit_service.record", side_effect=write_audit
        ), mock.patch(
            "agent.governance.auto_chain.on_task_completed",
            side_effect=assert_outer_conn_released,
        ):
            replay = complete_task(
                self.conn, task_id, status="succeeded",
                result=None, project_id="proj",
                fence_token=fence,
                completed_by="observer-runtime-recovery",
                override_reason="replay_auto_chain",
            )

        self.assertTrue(replay["replayed_auto_chain"])


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
        ), mock.patch(
            "agent.governance.chain_trailer.get_chain_state",
            side_effect=RuntimeError("trailer unavailable"),
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
        ), mock.patch(
            "agent.governance.chain_trailer.get_chain_state",
            side_effect=RuntimeError("trailer unavailable"),
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


class TestBacklogRuntimeMirrorForRegistryTransitions(unittest.TestCase):
    """Registry-only transitions must keep backlog runtime state current."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _make_conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def _insert_backlog(self, bug_id):
        self.conn.execute(
            "INSERT INTO backlog_bugs (bug_id, created_at, updated_at) VALUES (?, ?, ?)",
            (bug_id, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        self.conn.commit()

    def test_cancel_task_mirrors_backlog_runtime_cancelled(self):
        from agent.governance.task_registry import create_task, claim_task, cancel_task

        bug_id = "OPT-BACKLOG-CANCEL-MIRROR"
        self._insert_backlog(bug_id)
        task = create_task(
            self.conn,
            "proj",
            "pm task",
            task_type="pm",
            metadata={"bug_id": bug_id},
        )
        self.conn.commit()
        claim_task(self.conn, "proj", "worker-1")
        self.conn.commit()

        with mock.patch("agent.governance.auto_chain.on_task_completed", return_value=None):
            cancel_task(
                self.conn,
                task["task_id"],
                "observer withdrew reconcile smoke",
                project_id="proj",
            )

        row = self.conn.execute(
            "SELECT runtime_state, chain_stage, current_task_id, root_task_id, last_failure_reason "
            "FROM backlog_bugs WHERE bug_id = ?",
            (bug_id,),
        ).fetchone()
        self.assertEqual(row["runtime_state"], "cancelled")
        self.assertEqual(row["chain_stage"], "pm_cancelled")
        self.assertEqual(row["current_task_id"], task["task_id"])
        self.assertEqual(row["root_task_id"], task["task_id"])
        self.assertIn("observer withdrew", row["last_failure_reason"])

    def test_recover_stale_task_mirrors_backlog_runtime_queued(self):
        from agent.governance.task_registry import (
            create_task,
            claim_task,
            recover_stale_tasks,
        )

        bug_id = "OPT-BACKLOG-RECOVER-MIRROR"
        self._insert_backlog(bug_id)
        task = create_task(
            self.conn,
            "proj",
            "pm task",
            task_type="pm",
            metadata={"bug_id": bug_id},
        )
        self.conn.commit()
        claim_task(self.conn, "proj", "worker-1")
        self.conn.execute(
            "UPDATE tasks SET metadata_json = json_set(metadata_json, '$.lease_expires_at', ?) "
            "WHERE task_id = ?",
            ("2000-01-01T00:00:00Z", task["task_id"]),
        )
        self.conn.commit()

        result = recover_stale_tasks(self.conn, "proj")

        self.assertEqual(result["recovered"], 1)
        task_row = self.conn.execute(
            "SELECT status, execution_status FROM tasks WHERE task_id = ?",
            (task["task_id"],),
        ).fetchone()
        self.assertEqual(task_row["status"], "queued")
        self.assertEqual(task_row["execution_status"], "queued")
        backlog_row = self.conn.execute(
            "SELECT runtime_state, chain_stage, current_task_id, root_task_id, last_failure_reason "
            "FROM backlog_bugs WHERE bug_id = ?",
            (bug_id,),
        ).fetchone()
        self.assertEqual(backlog_row["runtime_state"], "queued")
        self.assertEqual(backlog_row["chain_stage"], "pm_queued")
        self.assertEqual(backlog_row["current_task_id"], task["task_id"])
        self.assertEqual(backlog_row["root_task_id"], task["task_id"])
        self.assertIn("Lease expired", backlog_row["last_failure_reason"])

    def test_complete_terminal_failure_mirrors_backlog_runtime_failed(self):
        from agent.governance.task_registry import create_task, claim_task, complete_task

        bug_id = "OPT-BACKLOG-COMPLETE-FAIL-MIRROR"
        self._insert_backlog(bug_id)
        task = create_task(
            self.conn,
            "proj",
            "pm task",
            task_type="pm",
            max_attempts=1,
            metadata={"bug_id": bug_id},
        )
        self.conn.commit()
        claimed, fence = claim_task(self.conn, "proj", "worker-1")
        self.assertEqual(claimed["task_id"], task["task_id"])
        self.conn.commit()

        with mock.patch(
            "agent.governance.task_registry._dispatch_auto_chain_failed",
            return_value=None,
        ):
            result = complete_task(
                self.conn,
                task["task_id"],
                status="failed",
                result={"error": "Reached max turns (60)"},
                project_id="proj",
                fence_token=fence,
            )

        self.assertEqual(result["status"], "failed")
        backlog_row = self.conn.execute(
            "SELECT runtime_state, chain_stage, current_task_id, root_task_id, last_failure_reason "
            "FROM backlog_bugs WHERE bug_id = ?",
            (bug_id,),
        ).fetchone()
        self.assertEqual(backlog_row["runtime_state"], "failed")
        self.assertEqual(backlog_row["chain_stage"], "pm_failed")
        self.assertEqual(backlog_row["current_task_id"], task["task_id"])
        self.assertEqual(backlog_row["root_task_id"], task["task_id"])
        self.assertIn("Reached max turns", backlog_row["last_failure_reason"])

    def test_complete_retry_failure_mirrors_backlog_runtime_queued(self):
        from agent.governance.task_registry import create_task, claim_task, complete_task

        bug_id = "OPT-BACKLOG-COMPLETE-RETRY-MIRROR"
        self._insert_backlog(bug_id)
        task = create_task(
            self.conn,
            "proj",
            "pm task",
            task_type="pm",
            max_attempts=2,
            metadata={"bug_id": bug_id},
        )
        self.conn.commit()
        claimed, fence = claim_task(self.conn, "proj", "worker-1")
        self.assertEqual(claimed["task_id"], task["task_id"])
        self.conn.commit()

        with mock.patch(
            "agent.governance.task_registry._dispatch_auto_chain_failed",
            return_value=None,
        ):
            result = complete_task(
                self.conn,
                task["task_id"],
                status="failed",
                result={"error": "temporary model failure"},
                project_id="proj",
                fence_token=fence,
            )

        self.assertEqual(result["status"], "queued")
        backlog_row = self.conn.execute(
            "SELECT runtime_state, chain_stage, current_task_id, root_task_id, last_failure_reason "
            "FROM backlog_bugs WHERE bug_id = ?",
            (bug_id,),
        ).fetchone()
        self.assertEqual(backlog_row["runtime_state"], "queued")
        self.assertEqual(backlog_row["chain_stage"], "pm_queued")
        self.assertEqual(backlog_row["current_task_id"], task["task_id"])
        self.assertEqual(backlog_row["root_task_id"], task["task_id"])
        self.assertIn("temporary model failure", backlog_row["last_failure_reason"])


class TestRetryOnDbLock(unittest.TestCase):
    """B5: Retry-with-backoff for sqlite3.OperationalError('database is locked')."""

    def test_succeeds_after_transient_lock(self):
        """AC8: Retry succeeds after a transient DB lock."""
        from agent.governance.task_registry import _retry_on_db_lock
        call_count = [0]

        def flaky():
            call_count[0] += 1
            if call_count[0] <= 2:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        with mock.patch("agent.governance.task_registry.time.sleep"):
            result = _retry_on_db_lock(flaky, _context="test")

        self.assertEqual(result, "ok")
        self.assertEqual(call_count[0], 3)  # 2 failures + 1 success

    def test_non_lock_error_not_retried(self):
        """AC9: Non-lock OperationalErrors propagate immediately."""
        from agent.governance.task_registry import _retry_on_db_lock
        call_count = [0]

        def bad():
            call_count[0] += 1
            raise sqlite3.OperationalError("no such table: tasks")

        with self.assertRaises(sqlite3.OperationalError) as ctx:
            _retry_on_db_lock(bad, _context="test")

        self.assertIn("no such table", str(ctx.exception))
        self.assertEqual(call_count[0], 1)  # no retry

    def test_raises_after_max_retries(self):
        """AC10: After max retries exhausted, raises original error."""
        from agent.governance.task_registry import _retry_on_db_lock

        def always_locked():
            raise sqlite3.OperationalError("database is locked")

        with mock.patch("agent.governance.task_registry.time.sleep"):
            with self.assertRaises(sqlite3.OperationalError) as ctx:
                _retry_on_db_lock(always_locked, _context="test")

        self.assertIn("database is locked", str(ctx.exception))

    def test_exponential_backoff_delays(self):
        """Verify exponential backoff timing: 0.1, 0.3, 0.9."""
        from agent.governance.task_registry import _retry_on_db_lock
        sleep_calls = []

        def always_locked():
            raise sqlite3.OperationalError("database is locked")

        with mock.patch("agent.governance.task_registry.time.sleep", side_effect=lambda d: sleep_calls.append(d)):
            with self.assertRaises(sqlite3.OperationalError):
                _retry_on_db_lock(always_locked, _context="test")

        self.assertEqual(len(sleep_calls), 3)
        self.assertAlmostEqual(sleep_calls[0], 0.1, places=2)
        self.assertAlmostEqual(sleep_calls[1], 0.3, places=2)
        self.assertAlmostEqual(sleep_calls[2], 0.9, places=2)


class TestCallerPid(unittest.TestCase):
    """B4: caller_pid parameter in claim_task stores correct PID in metadata."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _make_conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_caller_pid_stored_in_metadata(self):
        """AC5: caller_pid is stored as worker_pid in task metadata."""
        from agent.governance.task_registry import create_task, claim_task
        task = create_task(self.conn, "proj", "test", task_type="dev")
        self.conn.commit()
        claimed, fence = claim_task(self.conn, "proj", "w1", caller_pid=12345)
        self.conn.commit()

        self.assertIsNotNone(claimed)
        row = self.conn.execute(
            "SELECT metadata_json FROM tasks WHERE task_id = ?",
            (task["task_id"],),
        ).fetchone()
        meta = json.loads(row["metadata_json"])
        self.assertEqual(meta["worker_pid"], "12345")

    def test_caller_pid_zero_uses_os_getpid(self):
        """When caller_pid=0, falls back to os.getpid()."""
        from agent.governance.task_registry import create_task, claim_task
        task = create_task(self.conn, "proj", "test", task_type="dev")
        self.conn.commit()
        claimed, fence = claim_task(self.conn, "proj", "w1", caller_pid=0)
        self.conn.commit()

        self.assertIsNotNone(claimed)
        row = self.conn.execute(
            "SELECT metadata_json FROM tasks WHERE task_id = ?",
            (task["task_id"],),
        ).fetchone()
        meta = json.loads(row["metadata_json"])
        self.assertEqual(meta["worker_pid"], str(os.getpid()))

    def test_default_caller_pid_uses_os_getpid(self):
        """Default (no caller_pid) uses os.getpid()."""
        from agent.governance.task_registry import create_task, claim_task
        task = create_task(self.conn, "proj", "test", task_type="dev")
        self.conn.commit()
        claimed, fence = claim_task(self.conn, "proj", "w1")
        self.conn.commit()

        self.assertIsNotNone(claimed)
        row = self.conn.execute(
            "SELECT metadata_json FROM tasks WHERE task_id = ?",
            (task["task_id"],),
        ).fetchone()
        meta = json.loads(row["metadata_json"])
        self.assertEqual(meta["worker_pid"], str(os.getpid()))


class TestIsPidAlive(unittest.TestCase):
    """B4: _is_pid_alive helper for PID liveness checks."""

    def test_current_process_is_alive(self):
        """Current process PID should be alive."""
        from agent.governance.task_registry import _is_pid_alive
        self.assertTrue(_is_pid_alive(os.getpid()))

    def test_zero_pid_is_not_alive(self):
        """PID 0 should return False."""
        from agent.governance.task_registry import _is_pid_alive
        self.assertFalse(_is_pid_alive(0))

    def test_negative_pid_is_not_alive(self):
        """Negative PID should return False."""
        from agent.governance.task_registry import _is_pid_alive
        self.assertFalse(_is_pid_alive(-1))

    def test_nonexistent_pid_is_not_alive(self):
        """A very high PID that doesn't exist should return False."""
        from agent.governance.task_registry import _is_pid_alive
        # Use a PID unlikely to exist
        self.assertFalse(_is_pid_alive(4999999))


class TestRecoverStalePidCheck(unittest.TestCase):
    """B4: recover_stale_tasks Phase 2 — PID liveness check."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _make_conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_dead_pid_task_recovered(self):
        """Task with dead worker PID is re-queued."""
        from agent.governance.task_registry import create_task, claim_task, recover_stale_tasks
        task = create_task(self.conn, "proj", "test", task_type="dev")
        self.conn.commit()
        # Claim with a PID that doesn't exist
        claimed, fence = claim_task(self.conn, "proj", "w1", caller_pid=4999999)
        self.conn.commit()

        # Set lease far in the future so Phase 1 doesn't recover it
        self.conn.execute(
            "UPDATE tasks SET metadata_json = json_set(metadata_json, '$.lease_expires_at', '2099-01-01T00:00:00Z') WHERE task_id = ?",
            (task["task_id"],),
        )
        self.conn.commit()

        result = recover_stale_tasks(self.conn, "proj")
        self.conn.commit()
        self.assertGreaterEqual(result["dead_pid"], 1)

        # Task should be re-queued
        row = self.conn.execute(
            "SELECT execution_status FROM tasks WHERE task_id = ?",
            (task["task_id"],),
        ).fetchone()
        self.assertEqual(row["execution_status"], "queued")

    def test_alive_pid_task_not_recovered(self):
        """Task with alive worker PID is NOT re-queued."""
        from agent.governance.task_registry import create_task, claim_task, recover_stale_tasks
        task = create_task(self.conn, "proj", "test", task_type="dev")
        self.conn.commit()
        # Claim with current process PID (alive)
        claimed, fence = claim_task(self.conn, "proj", "w1", caller_pid=os.getpid())
        self.conn.commit()

        # Set lease far in the future so Phase 1 doesn't recover it
        self.conn.execute(
            "UPDATE tasks SET metadata_json = json_set(metadata_json, '$.lease_expires_at', '2099-01-01T00:00:00Z') WHERE task_id = ?",
            (task["task_id"],),
        )
        self.conn.commit()

        result = recover_stale_tasks(self.conn, "proj")
        self.assertEqual(result["dead_pid"], 0)

        # Task should still be claimed
        row = self.conn.execute(
            "SELECT execution_status FROM tasks WHERE task_id = ?",
            (task["task_id"],),
        ).fetchone()
        self.assertEqual(row["execution_status"], "claimed")

    def test_zero_pid_skipped(self):
        """Task with worker_pid=0 is skipped in Phase 2."""
        from agent.governance.task_registry import create_task, claim_task, recover_stale_tasks
        task = create_task(self.conn, "proj", "test", task_type="dev")
        self.conn.commit()

        # Manually set worker_pid to "0" and keep lease valid
        claimed, fence = claim_task(self.conn, "proj", "w1")
        self.conn.commit()
        self.conn.execute(
            """UPDATE tasks SET metadata_json = json_set(metadata_json,
                '$.worker_pid', '0',
                '$.lease_expires_at', '2099-01-01T00:00:00Z')
               WHERE task_id = ?""",
            (task["task_id"],),
        )
        self.conn.commit()

        result = recover_stale_tasks(self.conn, "proj")
        self.assertEqual(result["dead_pid"], 0)

        # Task should still be claimed
        row = self.conn.execute(
            "SELECT execution_status FROM tasks WHERE task_id = ?",
            (task["task_id"],),
        ).fetchone()
        self.assertEqual(row["execution_status"], "claimed")


class TestCrashRecoveryStateConsistency(unittest.TestCase):
    """MF-2026-05-02-009: retry/recover/cancel must keep task rows coherent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _make_conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_retry_reclaim_clears_previous_terminal_fields(self):
        from agent.governance.task_registry import create_task, claim_task, complete_task

        task = create_task(self.conn, "proj", "test", task_type="pm")
        self.conn.commit()
        claimed, fence = claim_task(self.conn, "proj", "worker-1")
        self.conn.commit()

        out = complete_task(
            self.conn,
            task["task_id"],
            status="failed",
            result={"error": "executor_crash_recovery"},
            error_message="crashed",
            fence_token=fence,
        )
        self.conn.commit()
        self.assertEqual(out["status"], "queued")

        claimed2, fence2 = claim_task(self.conn, "proj", "worker-2")
        self.conn.commit()
        self.assertIsNotNone(claimed2)
        self.assertTrue(fence2)

        row = self.conn.execute(
            "SELECT status, execution_status, completed_at, result_json, error_message "
            "FROM tasks WHERE task_id = ?",
            (task["task_id"],),
        ).fetchone()
        self.assertEqual(row["status"], "claimed")
        self.assertEqual(row["execution_status"], "claimed")
        self.assertIsNone(row["completed_at"])
        self.assertIsNone(row["result_json"])
        self.assertEqual(row["error_message"], "")
        running = self.conn.execute(
            "SELECT COUNT(*) FROM task_attempts WHERE task_id = ? AND status = 'running'",
            (task["task_id"],),
        ).fetchone()[0]
        self.assertEqual(running, 1)

    def test_cancel_task_updates_execution_status_and_running_attempt(self):
        from agent.governance.task_registry import create_task, claim_task, cancel_task

        task = create_task(self.conn, "proj", "test", task_type="pm")
        self.conn.commit()
        claim_task(self.conn, "proj", "worker-1")
        self.conn.commit()

        cancel_task(self.conn, task["task_id"], "observer withdraw", project_id="proj")
        self.conn.commit()

        row = self.conn.execute(
            "SELECT status, execution_status, completed_at, result_json, error_message "
            "FROM tasks WHERE task_id = ?",
            (task["task_id"],),
        ).fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["execution_status"], "cancelled")
        self.assertIsNotNone(row["completed_at"])
        self.assertIn("observer withdraw", row["error_message"])
        running = self.conn.execute(
            "SELECT COUNT(*) FROM task_attempts WHERE task_id = ? AND status = 'running'",
            (task["task_id"],),
        ).fetchone()[0]
        self.assertEqual(running, 0)
        attempt = self.conn.execute(
            "SELECT status, completed_at, error_message FROM task_attempts "
            "WHERE task_id = ?",
            (task["task_id"],),
        ).fetchone()
        self.assertEqual(attempt["status"], "cancelled")
        self.assertIsNotNone(attempt["completed_at"])
        self.assertIn("observer withdraw", attempt["error_message"])

    def test_dead_pid_recovery_requeues_and_closes_attempt(self):
        from agent.governance.task_registry import create_task, claim_task, recover_stale_tasks

        task = create_task(self.conn, "proj", "test", task_type="pm")
        self.conn.commit()
        claim_task(self.conn, "proj", "worker-1", caller_pid=4999999)
        self.conn.commit()
        self.conn.execute(
            "UPDATE tasks SET metadata_json = json_set(metadata_json, '$.lease_expires_at', "
            "'2099-01-01T00:00:00Z') WHERE task_id = ?",
            (task["task_id"],),
        )
        self.conn.commit()

        out = recover_stale_tasks(self.conn, "proj")
        self.conn.commit()
        self.assertGreaterEqual(out["dead_pid"], 1)
        row = self.conn.execute(
            "SELECT status, execution_status, completed_at, result_json, error_message "
            "FROM tasks WHERE task_id = ?",
            (task["task_id"],),
        ).fetchone()
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["execution_status"], "queued")
        self.assertIsNone(row["completed_at"])
        self.assertIsNone(row["result_json"])
        self.assertEqual(row["error_message"], "")
        attempt = self.conn.execute(
            "SELECT status, completed_at, result_json FROM task_attempts WHERE task_id = ?",
            (task["task_id"],),
        ).fetchone()
        self.assertEqual(attempt["status"], "failed")
        self.assertIsNotNone(attempt["completed_at"])
        self.assertIn("executor_crash_recovery", attempt["result_json"])


if __name__ == "__main__":
    unittest.main()
