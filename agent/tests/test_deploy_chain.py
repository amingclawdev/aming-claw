"""Tests for deploy_chain.restart_local_governance (B7 fix).

AC8: Tests cover:
  (a) process crashes immediately after start
  (b) health check retries succeed on 3rd attempt
  (c) port not released in time
  (d) stderr content included in failure summary
"""
import json
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_netstat_no_listener():
    """netstat output with no LISTENING on port 40000."""
    r = MagicMock()
    r.stdout = "  TCP    0.0.0.0:445    0.0.0.0:0    LISTENING    4\n"
    r.returncode = 0
    return r


def _mock_netstat_with_listener(pid=12345):
    """netstat output with governance LISTENING on port 40000."""
    r = MagicMock()
    r.stdout = f"  TCP    0.0.0.0:40000    0.0.0.0:0    LISTENING    {pid}\n"
    r.returncode = 0
    return r


class _FakeProc:
    """Fake subprocess.Popen result with controllable poll() behavior."""

    def __init__(self, pid=99999, crash_after=None, exit_code=1):
        self.pid = pid
        self.returncode = None
        self._crash_after = crash_after  # number of poll() calls before crash
        self._poll_count = 0
        self._exit_code = exit_code

    def poll(self):
        self._poll_count += 1
        if self._crash_after is not None and self._poll_count >= self._crash_after:
            self.returncode = self._exit_code
            return self._exit_code
        return None


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Tests: _is_port_free
# ---------------------------------------------------------------------------

class TestIsPortFree:
    def test_free_port(self):
        from agent.deploy_chain import _is_port_free
        # Use a high ephemeral port that should be free
        assert _is_port_free(59123) is True

    def test_occupied_port(self):
        from agent.deploy_chain import _is_port_free
        # Bind a port, then check it's not free
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 59124))
            assert _is_port_free(59124) is False


# ---------------------------------------------------------------------------
# Tests: _read_stderr_log
# ---------------------------------------------------------------------------

class TestReadStderrLog:
    def test_reads_existing_file(self, tmp_path):
        from agent.deploy_chain import _read_stderr_log
        f = tmp_path / "test.log"
        f.write_text("ImportError: no module named 'missing'\n")
        result = _read_stderr_log(str(f))
        assert "ImportError" in result

    def test_truncates_large_file(self, tmp_path):
        from agent.deploy_chain import _read_stderr_log
        f = tmp_path / "big.log"
        f.write_text("X" * 5000)
        result = _read_stderr_log(str(f), max_bytes=100)
        assert "truncated" in result
        assert len(result) < 200

    def test_missing_file_returns_empty(self):
        from agent.deploy_chain import _read_stderr_log
        assert _read_stderr_log("/nonexistent/path.log") == ""


# ---------------------------------------------------------------------------
# Tests: restart_local_governance (AC8)
# ---------------------------------------------------------------------------

class TestRestartLocalGovernance:
    """B7 fix: restart_local_governance with stderr capture, retry, port check."""

    @patch("time.sleep")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("agent.deploy_chain._is_port_free", return_value=True)
    @patch("agent.deploy_chain._read_stderr_log", return_value="RuntimeError: port in use")
    def test_ac8a_immediate_crash_reports_stderr(
        self, mock_read, mock_port, mock_run, mock_popen, mock_sleep
    ):
        """(a) Process crashes immediately — stderr content in summary."""
        from agent.deploy_chain import restart_local_governance

        mock_run.return_value = _mock_netstat_no_listener()
        crash_proc = _FakeProc(pid=55555, crash_after=1, exit_code=1)
        mock_popen.return_value = crash_proc

        ok, summary = restart_local_governance(port=40000)

        assert ok is False
        assert "crashed immediately" in summary
        assert "RuntimeError: port in use" in summary

    @patch("time.sleep")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("agent.deploy_chain._is_port_free", return_value=True)
    def test_ac8b_health_retry_succeeds_on_third(
        self, mock_port, mock_run, mock_popen, mock_sleep
    ):
        """(b) Health check fails twice, succeeds on 3rd attempt."""
        from agent.deploy_chain import restart_local_governance

        mock_run.return_value = _mock_netstat_no_listener()
        mock_popen.return_value = _FakeProc(pid=55556)  # never crashes

        # Mock requests.get: first 2 calls raise, 3rd succeeds
        call_count = [0]
        import requests as _req

        def fake_get(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise _req.ConnectionError("refused")
            return _FakeResponse(200)

        with patch("requests.get", side_effect=fake_get):
            ok, summary = restart_local_governance(port=40000)

        assert ok is True
        assert "governance OK" in summary
        assert "attempt 3" in summary

    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("agent.deploy_chain._is_port_free", return_value=False)
    def test_ac8c_port_not_released(self, mock_port, mock_run, mock_sleep):
        """(c) Port not released — warning in summary but proceeds."""
        from agent.deploy_chain import restart_local_governance

        mock_run.return_value = _mock_netstat_with_listener(pid=11111)

        with patch("subprocess.Popen") as mock_popen, \
             patch("requests.get") as mock_get:
            mock_popen.return_value = _FakeProc(pid=22222)
            mock_get.return_value = _FakeResponse(200)

            ok, summary = restart_local_governance(port=40000)

        assert "still held after 5s" in summary

    @patch("time.sleep")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("agent.deploy_chain._is_port_free", return_value=True)
    @patch("agent.deploy_chain._read_stderr_log", return_value="Address already in use")
    def test_ac8d_stderr_in_failure_summary(
        self, mock_read, mock_port, mock_run, mock_popen, mock_sleep
    ):
        """(d) All health checks fail — stderr content included in summary."""
        from agent.deploy_chain import restart_local_governance
        import requests as _req

        mock_run.return_value = _mock_netstat_no_listener()
        mock_popen.return_value = _FakeProc(pid=33333)  # stays alive

        with patch("requests.get", side_effect=_req.ConnectionError("refused")):
            ok, summary = restart_local_governance(port=40000)

        assert ok is False
        assert "Address already in use" in summary
        assert "unreachable after 4 attempts" in summary


class TestRestartLocalGovernanceLogging:
    """AC7: Verify log.warning is called on failure."""

    @patch("time.sleep")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("agent.deploy_chain._is_port_free", return_value=True)
    @patch("agent.deploy_chain._read_stderr_log", return_value="crash!")
    @patch("agent.deploy_chain.log")
    def test_ac7_log_warning_on_crash(
        self, mock_log, mock_read, mock_port, mock_run, mock_popen, mock_sleep
    ):
        from agent.deploy_chain import restart_local_governance

        mock_run.return_value = _mock_netstat_no_listener()
        mock_popen.return_value = _FakeProc(pid=44444, crash_after=1)

        restart_local_governance(port=40000)

        warning_calls = [c for c in mock_log.warning.call_args_list
                         if "crashed immediately" in str(c)]
        assert len(warning_calls) >= 1

    @patch("time.sleep")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("agent.deploy_chain._is_port_free", return_value=True)
    @patch("agent.deploy_chain._read_stderr_log", return_value="")
    @patch("agent.deploy_chain.log")
    def test_ac7_log_warning_on_health_failure(
        self, mock_log, mock_read, mock_port, mock_run, mock_popen, mock_sleep
    ):
        from agent.deploy_chain import restart_local_governance
        import requests as _req

        mock_run.return_value = _mock_netstat_no_listener()
        mock_popen.return_value = _FakeProc(pid=55555)

        with patch("requests.get", side_effect=_req.ConnectionError("refused")):
            restart_local_governance(port=40000)

        warning_calls = [c for c in mock_log.warning.call_args_list
                         if "health check failed" in str(c)]
        assert len(warning_calls) >= 1
