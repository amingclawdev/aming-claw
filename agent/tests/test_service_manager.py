"""Tests for agent/service_manager.py."""

import sys
import os
import time
import threading
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

# Ensure agent directory is on the path
_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from service_manager import ServiceManager, get_manager  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_process(pid: int = 12345, return_code: int = None) -> MagicMock:
    """Return a mock that behaves like a Popen object.

    We intentionally do NOT use spec=subprocess.Popen because the test may run
    inside a @patch('subprocess.Popen') context where subprocess.Popen is
    already a Mock, making spec= raise InvalidSpecError on Python 3.13.
    """
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = return_code   # None → still running
    proc.wait.return_value = 0
    return proc


# ---------------------------------------------------------------------------
# start() / stop()
# ---------------------------------------------------------------------------


class TestStartStop(unittest.TestCase):
    """Tests for start() and stop() lifecycle methods."""

    def setUp(self):
        self.mgr = ServiceManager(
            project_id="test-proj",
            governance_url="http://localhost:40006",
            executor_cmd=["echo", "hello"],
        )

    @patch("service_manager.subprocess.Popen")
    def test_start_launches_process(self, mock_popen):
        fake_proc = _make_fake_process(pid=9999)
        mock_popen.return_value = fake_proc

        result = self.mgr.start()

        self.assertTrue(result)
        mock_popen.assert_called_once()
        self.assertEqual(self.mgr._process.pid, 9999)

    @patch("service_manager.subprocess.Popen")
    def test_start_noop_if_already_running(self, mock_popen):
        fake_proc = _make_fake_process(pid=8888)
        mock_popen.return_value = fake_proc

        self.mgr.start()
        result = self.mgr.start()   # second call

        self.assertFalse(result)
        mock_popen.assert_called_once()   # only launched once

    @patch("service_manager.subprocess.Popen")
    def test_stop_terminates_process(self, mock_popen):
        fake_proc = _make_fake_process(pid=7777)
        mock_popen.return_value = fake_proc

        self.mgr.start()
        stopped = self.mgr.stop()

        self.assertTrue(stopped)
        fake_proc.terminate.assert_called_once()
        self.assertIsNone(self.mgr._process)

    def test_stop_noop_if_not_started(self):
        stopped = self.mgr.stop()
        self.assertFalse(stopped)

    @patch("service_manager.subprocess.Popen")
    def test_stop_kills_if_timeout(self, mock_popen):
        fake_proc = _make_fake_process(pid=6666)
        fake_proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="echo", timeout=5), 0]
        mock_popen.return_value = fake_proc

        self.mgr.start()
        self.mgr.stop()

        fake_proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------


class TestStatus(unittest.TestCase):
    """Tests for the status() method."""

    def _make_mgr(self, active=0, queued=0):
        mgr = ServiceManager(
            project_id="test-proj",
            governance_url="http://localhost:40006",
            executor_cmd=["echo"],
        )
        mgr._get_task_counts = MagicMock(return_value=(active, queued))
        return mgr

    @patch("service_manager.subprocess.Popen")
    def test_status_while_running(self, mock_popen):
        fake_proc = _make_fake_process(pid=1234)
        mock_popen.return_value = fake_proc

        mgr = self._make_mgr(active=2, queued=3)
        mgr.start()
        s = mgr.status()

        self.assertTrue(s["running"])
        self.assertEqual(s["pid"], 1234)
        self.assertIsNotNone(s["uptime_s"])
        self.assertGreaterEqual(s["uptime_s"], 0)
        self.assertEqual(s["active_tasks"], 2)
        self.assertEqual(s["queued_tasks"], 3)

    def test_status_when_not_started(self):
        mgr = self._make_mgr(active=0, queued=1)
        s = mgr.status()

        self.assertFalse(s["running"])
        self.assertIsNone(s["pid"])
        self.assertIsNone(s["uptime_s"])
        self.assertEqual(s["active_tasks"], 0)
        self.assertEqual(s["queued_tasks"], 1)

    @patch("service_manager.subprocess.Popen")
    def test_status_after_process_dies(self, mock_popen):
        """If the process exits on its own, status() should reflect that."""
        fake_proc = _make_fake_process(pid=5555, return_code=1)  # already exited
        mock_popen.return_value = fake_proc

        mgr = self._make_mgr()
        # Manually inject the "dead" process
        mgr._process = fake_proc
        mgr._start_time = time.monotonic() - 10

        s = mgr.status()

        self.assertFalse(s["running"])
        self.assertIsNone(s["pid"])
        self.assertIsNone(s["uptime_s"])

    def test_status_dict_has_required_keys(self):
        mgr = self._make_mgr()
        s = mgr.status()
        for key in ("pid", "running", "uptime_s", "active_tasks", "queued_tasks"):
            self.assertIn(key, s, f"Missing key: {key}")


# ---------------------------------------------------------------------------
# reload()
# ---------------------------------------------------------------------------


class TestReload(unittest.TestCase):
    """Tests for the reload() method."""

    def _make_mgr(self, task_count_sequence=None):
        """Create a ServiceManager whose _get_active_task_count is mocked.

        *task_count_sequence* is a list of ints returned in order on each call.
        If exhausted, returns 0.
        """
        mgr = ServiceManager(
            project_id="test-proj",
            governance_url="http://localhost:40006",
            executor_cmd=["echo"],
            reload_timeout=10,
            poll_interval=0.05,
        )
        counts = list(task_count_sequence or [0])

        def _side_effect():
            return counts.pop(0) if counts else 0

        mgr._get_active_task_count = MagicMock(side_effect=_side_effect)
        return mgr

    @patch("service_manager.subprocess.Popen")
    def test_reload_immediate_when_no_active_tasks(self, mock_popen):
        fake1 = _make_fake_process(pid=100)
        fake2 = _make_fake_process(pid=200)
        mock_popen.side_effect = [fake1, fake2]

        mgr = self._make_mgr(task_count_sequence=[0])
        mgr.start()   # starts fake1
        result = mgr.reload()

        self.assertTrue(result["success"])
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["pid"], 200)

    @patch("service_manager.subprocess.Popen")
    def test_reload_waits_for_active_task_completion(self, mock_popen):
        """active=2 → active=1 → active=0 ⇒ then reload."""
        fake1 = _make_fake_process(pid=101)
        fake2 = _make_fake_process(pid=202)
        mock_popen.side_effect = [fake1, fake2]

        mgr = self._make_mgr(task_count_sequence=[2, 1, 0])
        mgr.start()
        result = mgr.reload()

        self.assertTrue(result["success"])
        self.assertFalse(result["timed_out"])
        self.assertGreater(mgr._get_active_task_count.call_count, 1)

    @patch("service_manager.subprocess.Popen")
    def test_reload_proceeds_after_timeout(self, mock_popen):
        """Even if tasks never drain, reload proceeds once timeout expires."""
        fake1 = _make_fake_process(pid=111)
        fake2 = _make_fake_process(pid=222)
        mock_popen.side_effect = [fake1, fake2]

        mgr = ServiceManager(
            project_id="test-proj",
            governance_url="http://localhost:40006",
            executor_cmd=["echo"],
            reload_timeout=1,       # very short timeout
            poll_interval=0.05,
        )
        # Always return active=5 so we hit timeout
        mgr._get_active_task_count = MagicMock(return_value=5)

        mgr.start()
        result = mgr.reload()

        self.assertTrue(result["success"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["pid"], 222)

    @patch("service_manager.subprocess.Popen")
    def test_reload_callback_called_with_status(self, mock_popen):
        fake1 = _make_fake_process(pid=300)
        fake2 = _make_fake_process(pid=400)
        mock_popen.side_effect = [fake1, fake2]

        received = []
        callback = lambda s: received.append(s)  # noqa: E731

        mgr = self._make_mgr(task_count_sequence=[0])
        mgr.start()
        mgr.reload(callback=callback)

        self.assertEqual(len(received), 1)
        status = received[0]
        self.assertIn("pid", status)
        self.assertIn("running", status)
        self.assertIn("active_tasks", status)
        self.assertIn("queued_tasks", status)

    @patch("service_manager.subprocess.Popen")
    def test_reload_callback_exception_does_not_propagate(self, mock_popen):
        fake1 = _make_fake_process(pid=500)
        fake2 = _make_fake_process(pid=600)
        mock_popen.side_effect = [fake1, fake2]

        def bad_callback(s):
            raise RuntimeError("bot send failed")

        mgr = self._make_mgr(task_count_sequence=[0])
        mgr.start()
        # Should not raise
        result = mgr.reload(callback=bad_callback)
        self.assertTrue(result["success"])

    @patch("service_manager.subprocess.Popen")
    def test_reload_without_prior_start(self, mock_popen):
        """reload() should work even when executor was never started."""
        fake = _make_fake_process(pid=700)
        mock_popen.return_value = fake

        mgr = self._make_mgr(task_count_sequence=[0])
        result = mgr.reload()

        self.assertTrue(result["success"])
        self.assertEqual(result["pid"], 700)


# ---------------------------------------------------------------------------
# _get_task_counts() — network layer
# ---------------------------------------------------------------------------


class TestGetTaskCounts(unittest.TestCase):
    """Tests for the internal _get_task_counts() helper."""

    def _mgr(self):
        return ServiceManager(
            project_id="proj",
            governance_url="http://localhost:40006",
            executor_cmd=["echo"],
        )

    @patch("service_manager.requests.get")
    def test_counts_parsed_correctly(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "tasks": [
                {"status": "claimed"},
                {"status": "processing"},
                {"status": "queued"},
                {"status": "pending"},
                {"status": "completed"},
            ]
        }
        mock_get.return_value = mock_resp

        active, queued = self._mgr()._get_task_counts()

        self.assertEqual(active, 2)
        self.assertEqual(queued, 2)

    @patch("service_manager.requests.get")
    def test_network_error_returns_zeros(self, mock_get):
        mock_get.side_effect = ConnectionError("refused")

        active, queued = self._mgr()._get_task_counts()

        self.assertEqual(active, 0)
        self.assertEqual(queued, 0)


# ---------------------------------------------------------------------------
# get_manager() singleton
# ---------------------------------------------------------------------------


class TestGetManager(unittest.TestCase):
    def test_singleton_returns_same_instance(self):
        import service_manager as sm
        # Reset module singleton for isolation
        sm._default_manager = None

        m1 = get_manager()
        m2 = get_manager()
        self.assertIs(m1, m2)

        sm._default_manager = None   # cleanup

    def test_singleton_is_service_manager(self):
        import service_manager as sm
        sm._default_manager = None

        m = get_manager()
        self.assertIsInstance(m, ServiceManager)

        sm._default_manager = None   # cleanup


class TestHostDefaults(unittest.TestCase):
    def test_default_governance_url_prefers_nginx_entrypoint(self):
        import service_manager as sm
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sm._default_governance_url(), "http://localhost:40000")

    def test_default_executor_cmd_includes_host_routing(self):
        import service_manager as sm
        cmd = sm._default_executor_cmd(
            "aming-claw",
            "http://localhost:40000",
            "C:/workspace/aming_claw",
        )
        self.assertEqual(
            cmd,
            [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "executor_worker.py"),
                "--project",
                "aming-claw",
                "--url",
                "http://localhost:40000",
                "--workspace",
                "C:/workspace/aming_claw",
            ],
        )

    def test_constructor_uses_runtime_defaults(self):
        with patch.dict(
            os.environ,
            {
                "GOVERNANCE_URL": "http://localhost:40000",
                "PROJECT_ID": "runtime-project",
                "CODEX_WORKSPACE": "C:/runtime/workspace",
            },
            clear=True,
        ):
            mgr = ServiceManager()
        self.assertEqual(mgr.project_id, "runtime-project")
        self.assertEqual(mgr.governance_url, "http://localhost:40000")
        self.assertEqual(mgr.workspace, "C:/runtime/workspace")
        self.assertEqual(
            mgr._executor_cmd,
            [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "executor_worker.py"),
                "--project",
                "runtime-project",
                "--url",
                "http://localhost:40000",
                "--workspace",
                "C:/runtime/workspace",
            ],
        )


# ---------------------------------------------------------------------------
# _check_restart_signal() — signal file consumption
# ---------------------------------------------------------------------------


class TestCheckRestartSignal(unittest.TestCase):
    """Tests for signal file consumption (AC1-AC6)."""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.state_dir = Path(self.tmpdir) / "codex-tasks" / "state"
        self.state_dir.mkdir(parents=True)
        self.signal_file = self.state_dir / "manager_signal.json"

        self.mgr = ServiceManager(
            project_id="test-proj",
            governance_url="http://localhost:40006",
            executor_cmd=["echo", "hello"],
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _patch_signal_path(self):
        """Return a patch that makes _signal_file_path() return our temp path."""
        return patch("service_manager._signal_file_path", return_value=self.signal_file)

    @patch("service_manager.subprocess.Popen")
    def test_restart_signal_stops_and_starts_executor(self, mock_popen):
        """AC4: valid restart signal triggers stop+start and deletes file."""
        fake1 = _make_fake_process(pid=1000)
        fake2 = _make_fake_process(pid=2000)
        mock_popen.side_effect = [fake1, fake2]

        self.mgr.start()
        # Write a valid restart signal
        self.signal_file.write_text('{"action": "restart"}', encoding="utf-8")

        with self._patch_signal_path():
            self.mgr._check_restart_signal()

        # New process should be spawned
        self.assertEqual(self.mgr._process.pid, 2000)
        # Signal file should be deleted (R3)
        self.assertFalse(self.signal_file.exists())

    def test_missing_signal_file_noop(self):
        """AC5: missing file causes no error."""
        self.assertFalse(self.signal_file.exists())
        with self._patch_signal_path():
            # Should not raise
            self.mgr._check_restart_signal()

    @patch("service_manager.subprocess.Popen")
    def test_malformed_json_deletes_file(self, mock_popen):
        """AC6: invalid JSON logs warning and deletes the corrupt file."""
        fake = _make_fake_process(pid=3000)
        mock_popen.return_value = fake

        self.signal_file.write_text("NOT VALID JSON {{{", encoding="utf-8")

        with self._patch_signal_path():
            self.mgr._check_restart_signal()

        # File should be deleted
        self.assertFalse(self.signal_file.exists())
        # Process should NOT have been restarted (no popen call)
        mock_popen.assert_not_called()

    @patch("service_manager.subprocess.Popen")
    def test_restart_does_not_increment_circuit_breaker(self, mock_popen):
        """R5: signal-triggered restart must NOT count toward circuit breaker."""
        fake1 = _make_fake_process(pid=4000)
        fake2 = _make_fake_process(pid=5000)
        mock_popen.side_effect = [fake1, fake2]

        self.mgr.start()
        restart_count_before = self.mgr.restart_count
        restart_times_before = len(self.mgr._restart_times)

        self.signal_file.write_text('{"action": "restart"}', encoding="utf-8")
        with self._patch_signal_path():
            self.mgr._check_restart_signal()

        # Circuit breaker counters should be unchanged
        self.assertEqual(self.mgr.restart_count, restart_count_before)
        self.assertEqual(len(self.mgr._restart_times), restart_times_before)
        self.assertFalse(self.mgr._circuit_breaker_tripped)

    @patch("service_manager.subprocess.Popen")
    def test_unknown_action_ignored(self, mock_popen):
        """Signal with action != 'restart' is silently ignored."""
        fake = _make_fake_process(pid=6000)
        mock_popen.return_value = fake
        self.mgr.start()

        self.signal_file.write_text('{"action": "shutdown"}', encoding="utf-8")
        with self._patch_signal_path():
            self.mgr._check_restart_signal()

        # File should still exist (not consumed)
        self.assertTrue(self.signal_file.exists())
        # Process unchanged
        self.assertEqual(self.mgr._process.pid, 6000)

    @patch("service_manager.subprocess.Popen")
    def test_empty_json_object_ignored(self, mock_popen):
        """Signal file with empty JSON object is ignored (no action key)."""
        fake = _make_fake_process(pid=7000)
        mock_popen.return_value = fake
        self.mgr.start()

        self.signal_file.write_text('{}', encoding="utf-8")
        with self._patch_signal_path():
            self.mgr._check_restart_signal()

        self.assertTrue(self.signal_file.exists())


if __name__ == "__main__":
    unittest.main()
