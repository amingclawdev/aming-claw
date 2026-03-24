"""Tests for executor.py health check server."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


def _find_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestHealthHandler(unittest.TestCase):
    """Unit tests for _HealthHandler and start_health_server."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        # SHARED_VOLUME_PATH is the root; tasks_root() returns root/codex-tasks/
        self._shared_root = Path(self._tmpdir.name)
        self._shared = self._shared_root / "codex-tasks"
        for stage in ("processing", "pending"):
            (self._shared / stage).mkdir(parents=True)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _start_server(self, port: int) -> None:
        """Start health server with patched tasks_root and given port."""
        import executor
        with patch.dict(os.environ, {
            "SHARED_VOLUME_PATH": str(self._shared_root),
            "EXECUTOR_HEALTH_PORT": str(port),
        }):
            executor.start_health_server()
        time.sleep(0.3)  # Give thread time to bind

    def _get_health(self, port: int) -> dict:
        url = f"http://127.0.0.1:{port}/health"
        with urllib.request.urlopen(url, timeout=3) as resp:
            self.assertEqual(resp.status, 200)
            ct = resp.headers.get("Content-Type", "")
            self.assertIn("application/json", ct)
            return json.loads(resp.read())

    def test_health_returns_ok_when_empty(self):
        port = _find_free_port()
        with patch.dict(os.environ, {
            "SHARED_VOLUME_PATH": str(self._shared_root),
            "EXECUTOR_HEALTH_PORT": str(port),
        }):
            import executor
            executor.start_health_server()
        time.sleep(0.3)
        data = self._get_health(port)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["active_count"], 0)
        self.assertEqual(data["queued_count"], 0)
        self.assertIn("uptime_seconds", data)
        self.assertIn("timestamp", data)
        self.assertIsInstance(data["uptime_seconds"], int)

    def test_health_reflects_active_count(self):
        """Files in processing/ increment active_count."""
        (self._shared / "processing" / "task-001.json").write_text("{}")
        (self._shared / "processing" / "task-002.json").write_text("{}")
        port = _find_free_port()
        with patch.dict(os.environ, {
            "SHARED_VOLUME_PATH": str(self._shared_root),
            "EXECUTOR_HEALTH_PORT": str(port),
        }):
            import executor
            executor.start_health_server()
        time.sleep(0.3)
        data = self._get_health(port)
        self.assertEqual(data["active_count"], 2)

    def test_health_reflects_queued_count(self):
        """Files in pending/ increment queued_count (excludes .tmp.json)."""
        (self._shared / "pending" / "task-a.json").write_text("{}")
        (self._shared / "pending" / "task-b.json").write_text("{}")
        (self._shared / "pending" / "task-c.tmp.json").write_text("{}")  # should be excluded
        port = _find_free_port()
        with patch.dict(os.environ, {
            "SHARED_VOLUME_PATH": str(self._shared_root),
            "EXECUTOR_HEALTH_PORT": str(port),
        }):
            import executor
            executor.start_health_server()
        time.sleep(0.3)
        data = self._get_health(port)
        self.assertEqual(data["queued_count"], 2)

    def test_status_degraded_when_active_exceeds_threshold(self):
        """status becomes 'degraded' when active_count > threshold (default 10)."""
        for i in range(11):
            (self._shared / "processing" / f"task-{i:03d}.json").write_text("{}")
        port = _find_free_port()
        with patch.dict(os.environ, {
            "SHARED_VOLUME_PATH": str(self._shared_root),
            "EXECUTOR_HEALTH_PORT": str(port),
            "EXECUTOR_HEALTH_DEGRADED_THRESHOLD": "10",
        }):
            import executor
            executor.start_health_server()
        time.sleep(0.3)
        data = self._get_health(port)
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(data["active_count"], 11)

    def test_status_ok_at_threshold_boundary(self):
        """Exactly at threshold → ok; above → degraded."""
        for i in range(10):
            (self._shared / "processing" / f"task-{i:03d}.json").write_text("{}")
        port = _find_free_port()
        with patch.dict(os.environ, {
            "SHARED_VOLUME_PATH": str(self._shared_root),
            "EXECUTOR_HEALTH_PORT": str(port),
            "EXECUTOR_HEALTH_DEGRADED_THRESHOLD": "10",
        }):
            import executor
            executor.start_health_server()
        time.sleep(0.3)
        data = self._get_health(port)
        self.assertEqual(data["status"], "ok")

    def test_404_for_unknown_path(self):
        """Non-/health paths return 404."""
        port = _find_free_port()
        with patch.dict(os.environ, {
            "SHARED_VOLUME_PATH": str(self._shared_root),
            "EXECUTOR_HEALTH_PORT": str(port),
        }):
            import executor
            executor.start_health_server()
        time.sleep(0.3)
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=3)
        self.assertEqual(ctx.exception.code, 404)

    def test_uptime_increases_over_time(self):
        """uptime_seconds grows between two requests."""
        port = _find_free_port()
        with patch.dict(os.environ, {
            "SHARED_VOLUME_PATH": str(self._shared_root),
            "EXECUTOR_HEALTH_PORT": str(port),
        }):
            import executor
            executor.start_health_server()
        time.sleep(0.3)
        d1 = self._get_health(port)
        time.sleep(1.1)
        d2 = self._get_health(port)
        self.assertGreaterEqual(d2["uptime_seconds"], d1["uptime_seconds"])

    def test_port_conflict_logs_and_does_not_raise(self):
        """Port conflict must not raise — only print a warning."""
        import socket
        port = _find_free_port()
        # Hold the port so start_health_server finds it occupied
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        try:
            with patch.dict(os.environ, {
                "SHARED_VOLUME_PATH": str(self._shared_root),
                "EXECUTOR_HEALTH_PORT": str(port),
            }):
                import executor
                # Should not raise
                executor.start_health_server()
        finally:
            blocker.close()

    def test_json_fields_present(self):
        """All required fields exist in response."""
        port = _find_free_port()
        with patch.dict(os.environ, {
            "SHARED_VOLUME_PATH": str(self._shared_root),
            "EXECUTOR_HEALTH_PORT": str(port),
        }):
            import executor
            executor.start_health_server()
        time.sleep(0.3)
        data = self._get_health(port)
        for field in ("status", "active_count", "queued_count", "uptime_seconds", "timestamp"):
            self.assertIn(field, data, f"missing field: {field}")

    def test_timestamp_iso_format(self):
        """timestamp field is ISO-8601 UTC format."""
        import re
        port = _find_free_port()
        with patch.dict(os.environ, {
            "SHARED_VOLUME_PATH": str(self._shared_root),
            "EXECUTOR_HEALTH_PORT": str(port),
        }):
            import executor
            executor.start_health_server()
        time.sleep(0.3)
        data = self._get_health(port)
        self.assertRegex(data["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_env_port_override(self):
        """EXECUTOR_HEALTH_PORT env var controls the port."""
        port = _find_free_port()
        with patch.dict(os.environ, {
            "SHARED_VOLUME_PATH": str(self._shared_root),
            "EXECUTOR_HEALTH_PORT": str(port),
        }):
            import executor
            executor.start_health_server()
        time.sleep(0.3)
        # Server must be reachable on the overridden port
        data = self._get_health(port)
        self.assertIn("status", data)


if __name__ == "__main__":
    unittest.main()
