"""Tests for manager_http_server redeploy endpoint (PR1-R6).

Covers:
  (a) Success path with mocked subprocess.Popen + health probe → verify db_write called
  (b) Mutual-exclusion 400 for service_manager target
  (c) Failure path (health check fails) → verify NO db_write
"""

import io
import json
import sys
import threading
import time
import unittest
from http.server import HTTPServer
from unittest import mock
from unittest.mock import MagicMock, patch

# Ensure the project root is on sys.path
from pathlib import Path
_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from agent.manager_http_server import (
    ManagerHTTPHandler,
    create_server,
    _FORBIDDEN_TARGETS,
    _VALID_TARGETS,
    MANAGER_HTTP_HOST,
    MANAGER_HTTP_PORT,
)


def _make_request(server_address, method, path, body=None):
    """Send an HTTP request to the test server and return (status, body_dict)."""
    import urllib.request
    import urllib.error

    url = f"http://{server_address[0]}:{server_address[1]}{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class TestManagerRedeployEndpoint(unittest.TestCase):
    """Integration-style tests using a real ThreadingHTTPServer on a random port."""

    @classmethod
    def setUpClass(cls):
        """Start a test HTTP server on a random port."""
        cls.server = create_server("127.0.0.1", 0)  # port 0 = random available
        cls.server_address = cls.server.server_address
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        # Give server a moment to bind
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_redeploy_self_returns_400(self):
        """AC5: POST /api/manager/redeploy/service_manager returns 400 with 'cannot redeploy self'."""
        status, body = _make_request(
            self.server_address,
            "POST",
            "/api/manager/redeploy/service_manager",
            {"chain_version": "abc1234"},
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertIn("cannot redeploy self", body["detail"])

    @patch("agent.manager_http_server._write_chain_version", return_value=True)
    @patch("agent.manager_http_server._wait_for_health", return_value=True)
    @patch("agent.manager_http_server._spawn_governance_process")
    @patch("agent.manager_http_server._stop_governance_process", return_value=True)
    def test_redeploy_governance_success(self, mock_stop, mock_spawn, mock_health, mock_write):
        """AC2/AC4: Successful redeploy returns {ok:true, pid:<int>} and calls version-update."""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_spawn.return_value = mock_proc

        status, body = _make_request(
            self.server_address,
            "POST",
            "/api/manager/redeploy/governance",
            {"chain_version": "abc1234"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["pid"], 12345)
        self.assertEqual(body["chain_version"], "abc1234")

        # AC4: version-update called exactly once
        mock_write.assert_called_once_with("abc1234")

    @patch("agent.manager_http_server._write_chain_version")
    @patch("agent.manager_http_server._wait_for_health", return_value=False)
    @patch("agent.manager_http_server._spawn_governance_process")
    @patch("agent.manager_http_server._stop_governance_process", return_value=True)
    def test_redeploy_governance_failure_no_db_write(self, mock_stop, mock_spawn, mock_health, mock_write):
        """AC3: When health check fails, return {ok:false} and do NOT call version-update."""
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_spawn.return_value = mock_proc

        status, body = _make_request(
            self.server_address,
            "POST",
            "/api/manager/redeploy/governance",
            {"chain_version": "def5678"},
        )
        self.assertEqual(status, 500)
        self.assertFalse(body["ok"])
        self.assertIn("health check failed", body["detail"])

        # AC3: version-update NOT called
        mock_write.assert_not_called()

    @patch("agent.manager_http_server._write_chain_version")
    @patch("agent.manager_http_server._spawn_governance_process", side_effect=RuntimeError("spawn failed"))
    @patch("agent.manager_http_server._stop_governance_process", return_value=True)
    def test_redeploy_governance_spawn_failure_no_db_write(self, mock_stop, mock_spawn, mock_write):
        """AC3: When spawn fails, return {ok:false} and do NOT call version-update."""
        status, body = _make_request(
            self.server_address,
            "POST",
            "/api/manager/redeploy/governance",
            {"chain_version": "ghi9012"},
        )
        self.assertEqual(status, 500)
        self.assertFalse(body["ok"])

        # version-update NOT called
        mock_write.assert_not_called()

    def test_redeploy_unknown_target_404(self):
        """Unknown target returns 404."""
        status, body = _make_request(
            self.server_address,
            "POST",
            "/api/manager/redeploy/nonexistent",
            {"chain_version": "abc1234"},
        )
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])

    def test_redeploy_governance_missing_chain_version(self):
        """Missing chain_version returns 400."""
        status, body = _make_request(
            self.server_address,
            "POST",
            "/api/manager/redeploy/governance",
            {},
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertIn("chain_version", body["detail"])


class TestManagerHTTPServerImports(unittest.TestCase):
    """AC1: Verify stdlib imports, no aiohttp."""

    def test_uses_stdlib_http_server(self):
        """AC1: from http.server import is present."""
        import agent.manager_http_server as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        self.assertIn("from http.server import", source)

    def test_no_aiohttp_import(self):
        """AC1: 'from aiohttp' is absent."""
        import agent.manager_http_server as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from aiohttp", source)
        self.assertNotIn("import aiohttp", source)


if __name__ == "__main__":
    unittest.main()
