"""Tests for manager_http_server redeploy endpoint (PR1-R6).

Covers:
  (a) Success path with mocked subprocess.Popen + health probe → verify db_write called
  (b) Mutual-exclusion 400 for service_manager target
  (c) Failure path (health check fails) → verify NO db_write
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import HTTPServer
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

# Ensure the project root is on sys.path
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


def _git(args, cwd, check=True):
    """Run a real git command in a temp fixture repo."""
    res = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30
    )
    if check and res.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {res.stderr or res.stdout}"
        )
    return res


class _TempGitRepo:
    """Context manager creating a throwaway git repo with one commit."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        env_args = [
            "-c", "user.email=t@t.test", "-c", "user.name=Test",
        ]
        _git(["init", "-q"], self.root)
        (self.root / "file.txt").write_text("v1\n", encoding="utf-8")
        _git(["add", "."], self.root)
        _git(env_args + ["commit", "-q", "-m", "c1"], self.root)
        self.head = _git(["rev-parse", "HEAD"], self.root).stdout.strip()
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()
        return False


class TestEnsurePluginCloneCheckout(unittest.TestCase):
    """Drive _ensure_plugin_clone_checkout against a temp git repo via the
    injectable _run_git hook, with REDEPLOY_REAL_CHECKOUT=1 so the real logic
    runs (the no-op pytest guard is verified separately)."""

    def setUp(self):
        import agent.manager_http_server as mod
        self.mod = mod
        # Allow the real checkout logic to run during this test.
        self._prev_real = os.environ.get("REDEPLOY_REAL_CHECKOUT")
        os.environ["REDEPLOY_REAL_CHECKOUT"] = "1"
        # Save originals to restore.
        self._orig_run_git = mod._run_git
        self._orig_project_root = mod._project_root

    def tearDown(self):
        self.mod._run_git = self._orig_run_git
        self.mod._project_root = self._orig_project_root
        if self._prev_real is None:
            os.environ.pop("REDEPLOY_REAL_CHECKOUT", None)
        else:
            os.environ["REDEPLOY_REAL_CHECKOUT"] = self._prev_real

    def _wire_repo(self, repo):
        """Point _run_git / _project_root at the temp repo."""
        def fake_run_git(args, cwd):
            # Ignore cwd passed in; always operate on the fixture repo so the
            # helper's _project_root() resolution is exercised too.
            return subprocess.run(
                ["git", *args], cwd=str(repo.root),
                capture_output=True, text=True, timeout=30,
            )
        self.mod._run_git = fake_run_git
        self.mod._project_root = lambda: repo.root

    def test_dirty_tree_raises(self):
        with _TempGitRepo() as repo:
            (repo.root / "file.txt").write_text("dirty\n", encoding="utf-8")
            self._wire_repo(repo)
            with self.assertRaises(RuntimeError) as ctx:
                self.mod._ensure_plugin_clone_checkout(repo.head)
            self.assertIn("dirty", str(ctx.exception).lower())

    def test_clean_valid_target_checks_out_and_verifies(self):
        with _TempGitRepo() as repo:
            # Create a second commit and an older target to check out to.
            (repo.root / "file.txt").write_text("v2\n", encoding="utf-8")
            _git(["add", "."], repo.root)
            _git(["-c", "user.email=t@t.test", "-c", "user.name=Test",
                  "commit", "-q", "-m", "c2"], repo.root)
            target = repo.head  # the first commit (full hash)
            self._wire_repo(repo)
            resolved = self.mod._ensure_plugin_clone_checkout(target)
            self.assertEqual(resolved, target)
            # HEAD really moved.
            now = _git(["rev-parse", "HEAD"], repo.root).stdout.strip()
            self.assertEqual(now, target)

    def test_short_hash_target_verifies(self):
        with _TempGitRepo() as repo:
            short = repo.head[:8]
            self._wire_repo(repo)
            resolved = self.mod._ensure_plugin_clone_checkout(short)
            self.assertTrue(resolved.startswith(short))

    def test_missing_ref_raises(self):
        with _TempGitRepo() as repo:
            self._wire_repo(repo)
            with self.assertRaises(RuntimeError) as ctx:
                self.mod._ensure_plugin_clone_checkout("deadbeefdeadbeef")
            self.assertIn("not present", str(ctx.exception).lower())


class TestRedeployRuntimeCheckoutNoOpGuard(unittest.TestCase):
    """Under pytest WITHOUT REDEPLOY_REAL_CHECKOUT=1, no real git checkout runs."""

    def test_no_op_under_pytest(self):
        import agent.manager_http_server as mod
        # Ensure the guard env is not forcing a real checkout.
        prev = os.environ.pop("REDEPLOY_REAL_CHECKOUT", None)
        try:
            with patch.object(mod, "_run_git") as mock_run_git:
                result = mod._ensure_plugin_clone_checkout("abc1234")
                self.assertEqual(result, "abc1234")
                # The real git runner must never be touched under the guard.
                mock_run_git.assert_not_called()
        finally:
            if prev is not None:
                os.environ["REDEPLOY_REAL_CHECKOUT"] = prev


class TestRedeployEndpointRuntimeCheckout(unittest.TestCase):
    """Endpoint-level: a failed runtime checkout returns a loud non-ok response
    and governance is NOT stopped/spawned."""

    @classmethod
    def setUpClass(cls):
        cls.server = create_server("127.0.0.1", 0)
        cls.server_address = cls.server.server_address
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    @patch("agent.manager_http_server._write_chain_version")
    @patch("agent.manager_http_server._wait_for_health")
    @patch("agent.manager_http_server._spawn_governance_process")
    @patch("agent.manager_http_server._stop_governance_process")
    @patch(
        "agent.manager_http_server._ensure_plugin_clone_checkout",
        side_effect=RuntimeError("runtime_checkout: refusing dirty tree foo.py"),
    )
    def test_checkout_failure_blocks_stop_and_spawn(
        self, mock_checkout, mock_stop, mock_spawn, mock_health, mock_write
    ):
        status, body = _make_request(
            self.server_address,
            "POST",
            "/api/manager/redeploy/governance",
            {"chain_version": "abc1234"},
        )
        self.assertEqual(status, 500)
        self.assertFalse(body["ok"])
        self.assertEqual(body["step"], "runtime_checkout")
        self.assertFalse(body["runtime_checkout_advanced"])
        self.assertIn("dirty", body["error"].lower())
        # Governance must NOT be touched when the checkout did not advance.
        mock_stop.assert_not_called()
        mock_spawn.assert_not_called()
        mock_health.assert_not_called()
        mock_write.assert_not_called()

    @patch("agent.manager_http_server._write_chain_version", return_value=True)
    @patch("agent.manager_http_server._wait_for_health", return_value=True)
    @patch("agent.manager_http_server._spawn_governance_process")
    @patch("agent.manager_http_server._stop_governance_process", return_value=True)
    @patch(
        "agent.manager_http_server._ensure_plugin_clone_checkout",
        return_value="fullhead0000000000000000000000000000abcd",
    )
    def test_checkout_success_includes_runtime_fields(
        self, mock_checkout, mock_stop, mock_spawn, mock_health, mock_write
    ):
        mock_proc = MagicMock()
        mock_proc.pid = 4242
        mock_spawn.return_value = mock_proc
        status, body = _make_request(
            self.server_address,
            "POST",
            "/api/manager/redeploy/governance",
            {"chain_version": "abc1234"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["runtime_checkout_advanced"])
        self.assertEqual(body["runtime_head"], "fullhead0000000000000000000000000000abcd")
        mock_checkout.assert_called_once_with("abc1234")
        # Checkout happens before stop/spawn.
        mock_stop.assert_called_once()
        mock_spawn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
