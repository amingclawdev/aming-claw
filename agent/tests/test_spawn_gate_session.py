"""
test_spawn_gate_session.py — Unit tests for executor.spawn_gate_session.

Coverage:
  1. Session isolation: two spawns return distinct UUIDs.
  2. Payload sanitisation: forbidden keys (debug context, history) stripped.
  3. Payload content: only allowed keys reach the subprocess.
  4. Timeout path: subprocess.TimeoutExpired → status="timeout".
  5. FAIL + re-spawn: first spawn fails, second spawn returns new session_id.
  6. Windows env isolation: child env contains explicit keys, not parent secrets.
"""
import os
import sys
import json
import unittest
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Path setup (mirrors pattern used across this test suite)
# ---------------------------------------------------------------------------
_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _AGENT_DIR)

from executor import (
    spawn_gate_session,
    _GATE_PAYLOAD_ALLOWED_KEYS,
    _GATE_ENV_PASSTHROUGH,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_payload(**extra):
    """Return a valid payload optionally augmented with forbidden fields."""
    base = {
        "acceptance_screenshot": "screenshot_data",
        "logs": "log line 1\nlog line 2",
        "original_instruction": "Fix the failing tests",
    }
    base.update(extra)
    return base


def _mock_proc(returncode=0, stdout_data=None, stderr=""):
    """Build a fake CompletedProcess for subprocess.run mock."""
    if stdout_data is None:
        stdout_data = {
            "session_id": "MOCKED_SESSION",
            "task_id": "task-001",
            "status": "completed",
            "payload_keys": list(_GATE_PAYLOAD_ALLOWED_KEYS),
        }
    m = MagicMock()
    m.returncode = returncode
    m.stdout = json.dumps(stdout_data) + "\n"
    m.stderr = stderr
    return m


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSpawnGateSessionIsolation(unittest.TestCase):
    """Each spawn must return a unique session_id."""

    @patch("executor.subprocess.run")
    def test_unique_session_ids(self, mock_run):
        mock_run.side_effect = [_mock_proc(), _mock_proc()]

        r1 = spawn_gate_session("task-001", _make_payload())
        r2 = spawn_gate_session("task-001", _make_payload())

        self.assertNotEqual(r1["session_id"], r2["session_id"],
                            "Two consecutive spawns must have distinct session_ids")

    @patch("executor.subprocess.run")
    def test_session_id_is_uuid4_format(self, mock_run):
        mock_run.return_value = _mock_proc()
        import uuid
        r = spawn_gate_session("task-002", _make_payload())
        # Should parse as valid UUID without raising
        parsed = uuid.UUID(r["session_id"], version=4)
        self.assertEqual(str(parsed), r["session_id"])


class TestSpawnGateSessionPayloadSanitisation(unittest.TestCase):
    """Forbidden fields must never reach the subprocess."""

    @patch("executor.subprocess.run")
    def test_forbidden_keys_stripped(self, mock_run):
        """debug_context and conversation_history must be stripped."""
        captured_env = {}

        def capture_side_effect(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            # Read the payload file written by spawn_gate_session
            payload_file = captured_env.get("GATE_PAYLOAD_FILE", "")
            if payload_file and os.path.exists(payload_file):
                with open(payload_file, encoding="utf-8") as f:
                    captured_env["_payload"] = json.load(f)
            return _mock_proc()

        mock_run.side_effect = capture_side_effect

        dirty_payload = _make_payload(
            debug_context={"stack": "frame1"},
            conversation_history=[{"role": "user", "content": "hi"}],
            _internal_secret="s3cr3t",
        )
        spawn_gate_session("task-003", dirty_payload)

        payload_seen = captured_env.get("_payload", {})
        for forbidden_key in ("debug_context", "conversation_history", "_internal_secret"):
            self.assertNotIn(forbidden_key, payload_seen,
                             f"Forbidden key '{forbidden_key}' must not reach subprocess")

    @patch("executor.subprocess.run")
    def test_allowed_keys_present(self, mock_run):
        """All three allowed keys must reach the subprocess."""
        captured_env = {}

        def capture_side_effect(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            payload_file = captured_env.get("GATE_PAYLOAD_FILE", "")
            if payload_file and os.path.exists(payload_file):
                with open(payload_file, encoding="utf-8") as f:
                    captured_env["_payload"] = json.load(f)
            return _mock_proc()

        mock_run.side_effect = capture_side_effect

        spawn_gate_session("task-004", _make_payload())

        payload_seen = captured_env.get("_payload", {})
        for key in _GATE_PAYLOAD_ALLOWED_KEYS:
            self.assertIn(key, payload_seen,
                          f"Allowed key '{key}' must reach subprocess payload")


class TestSpawnGateSessionWindowsEnv(unittest.TestCase):
    """Child process receives only explicitly passed env keys."""

    @patch("executor.subprocess.run")
    def test_env_contains_gate_vars(self, mock_run):
        """GATE_SESSION_ID and GATE_TASK_ID must appear in child env."""
        captured_env = {}

        def capture_side_effect(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return _mock_proc()

        mock_run.side_effect = capture_side_effect
        spawn_gate_session("task-005", _make_payload())

        self.assertIn("GATE_SESSION_ID", captured_env)
        self.assertIn("GATE_TASK_ID", captured_env)
        self.assertEqual(captured_env["GATE_TASK_ID"], "task-005")

    @patch("executor.subprocess.run")
    def test_env_does_not_contain_arbitrary_parent_vars(self, mock_run):
        """Arbitrary parent env vars must not leak into child env."""
        captured_env = {}
        sentinel_key = "_TEST_SENTINEL_SHOULD_NOT_LEAK"
        os.environ[sentinel_key] = "secret"

        def capture_side_effect(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return _mock_proc()

        mock_run.side_effect = capture_side_effect
        try:
            spawn_gate_session("task-006", _make_payload())
            self.assertNotIn(sentinel_key, captured_env,
                             "Arbitrary parent env var must not leak to child")
        finally:
            del os.environ[sentinel_key]

    @patch("executor.subprocess.run")
    def test_no_shell_true(self, mock_run):
        """subprocess.run must NOT use shell=True (Windows security)."""
        mock_run.return_value = _mock_proc()
        spawn_gate_session("task-007", _make_payload())

        _, kwargs = mock_run.call_args
        self.assertFalse(kwargs.get("shell", False),
                         "shell=True is forbidden for Windows security")


class TestSpawnGateSessionTimeout(unittest.TestCase):
    """Timeout path must return status='timeout'."""

    @patch("executor.subprocess.run")
    def test_timeout_returns_timeout_status(self, mock_run):
        import subprocess as _subprocess
        mock_run.side_effect = _subprocess.TimeoutExpired(cmd="python", timeout=120)

        r = spawn_gate_session("task-008", _make_payload())
        self.assertEqual(r["status"], "timeout")
        self.assertIn("session_id", r)

    @patch("executor.subprocess.run")
    def test_custom_timeout_env(self, mock_run):
        """GATE_SESSION_TIMEOUT_SEC env var must be respected."""
        import subprocess as _subprocess
        mock_run.side_effect = _subprocess.TimeoutExpired(cmd="python", timeout=30)

        with patch.dict(os.environ, {"GATE_SESSION_TIMEOUT_SEC": "30"}):
            r = spawn_gate_session("task-009", _make_payload())

        self.assertEqual(r["status"], "timeout")
        self.assertEqual(r["elapsed_ms"], 30 * 1000)


class TestSpawnGateSessionFailAndRespawn(unittest.TestCase):
    """FAIL then re-spawn: second call gets new session_id, status=completed."""

    @patch("executor.subprocess.run")
    def test_fail_then_respawn(self, mock_run):
        """Simulate first spawn fails (returncode=1), second succeeds."""
        fail_proc = MagicMock()
        fail_proc.returncode = 1
        fail_proc.stdout = ""
        fail_proc.stderr = "AI runner error"

        success_proc = _mock_proc(returncode=0)
        mock_run.side_effect = [fail_proc, success_proc]

        r1 = spawn_gate_session("task-010", _make_payload())
        self.assertEqual(r1["status"], "failed")

        r2 = spawn_gate_session("task-010", _make_payload())
        self.assertEqual(r2["status"], "completed")

        # Each spawn must be independently identified
        self.assertNotEqual(r1["session_id"], r2["session_id"])

    @patch("executor.subprocess.run")
    def test_respawn_on_exception(self, mock_run):
        """If subprocess raises generic Exception, status=error, re-spawn succeeds."""
        mock_run.side_effect = [RuntimeError("process died"), _mock_proc()]

        r1 = spawn_gate_session("task-011", _make_payload())
        self.assertEqual(r1["status"], "error")

        r2 = spawn_gate_session("task-011", _make_payload())
        self.assertEqual(r2["status"], "completed")

        self.assertNotEqual(r1["session_id"], r2["session_id"])


class TestSpawnGateSessionReturnShape(unittest.TestCase):
    """Return value always has required fields."""

    @patch("executor.subprocess.run")
    def test_return_has_required_fields(self, mock_run):
        mock_run.return_value = _mock_proc()
        r = spawn_gate_session("task-012", _make_payload())

        for field in ("session_id", "task_id", "status", "elapsed_ms"):
            self.assertIn(field, r, f"Missing required field: {field}")

    @patch("executor.subprocess.run")
    def test_task_id_echoed(self, mock_run):
        mock_run.return_value = _mock_proc()
        r = spawn_gate_session("task-XYZ", _make_payload())
        self.assertEqual(r["task_id"], "task-XYZ")


if __name__ == "__main__":
    unittest.main()
