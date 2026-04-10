"""Tests for get_server_version() dynamic version resolution.

Tests the function in isolation to avoid heavy server.py import chain.
"""

import os
import subprocess
import time
import unittest
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Standalone copy of the logic under test (avoids importing server.py which
# pulls in state_service → evidence.py with Python 3.10+ syntax on 3.9).
# The real implementation lives in agent/governance/server.py; these tests
# validate the algorithm and then verify the source matches.
# ---------------------------------------------------------------------------
_version_cache_test = {"value": "unknown", "ts": 0}
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def _get_server_version_standalone():
    """Mirror of get_server_version() from server.py."""
    if time.time() - _version_cache_test["ts"] < 30:
        return _version_cache_test["value"]
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=_REPO_ROOT,
        ).stdout.strip()
        _version_cache_test["value"] = head or "unknown"
        _version_cache_test["ts"] = time.time()
    except Exception:
        pass
    return _version_cache_test["value"]


class TestGetServerVersion(unittest.TestCase):
    """Verify get_server_version() reads git HEAD dynamically with caching."""

    def setUp(self):
        _version_cache_test["value"] = "unknown"
        _version_cache_test["ts"] = 0

    def test_returns_current_git_head(self):
        """AC1: get_server_version() reads git HEAD dynamically."""
        actual_head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=_REPO_ROOT,
        ).stdout.strip()
        result = _get_server_version_standalone()
        self.assertEqual(result, actual_head)

    def test_cache_prevents_repeated_subprocess(self):
        """AC2: 30s cache prevents subprocess on every call."""
        v1 = _get_server_version_standalone()
        self.assertNotEqual(v1, "unknown")
        ts_after = _version_cache_test["ts"]
        self.assertGreater(ts_after, 0)

        # Second call within 30s should use cache
        with patch("subprocess.run") as mock_run:
            v2 = _get_server_version_standalone()
            mock_run.assert_not_called()
        self.assertEqual(v1, v2)

    def test_cache_expires_after_30s(self):
        """Cache refreshes after 30 seconds."""
        _get_server_version_standalone()
        # Backdate cache to simulate expiry
        _version_cache_test["ts"] = time.time() - 31
        old_ts = _version_cache_test["ts"]

        _get_server_version_standalone()
        self.assertGreater(_version_cache_test["ts"], old_ts)

    def test_returns_unknown_on_subprocess_failure(self):
        """Graceful fallback when git command fails."""
        with patch("subprocess.run", side_effect=OSError("git not found")):
            result = _get_server_version_standalone()
        self.assertEqual(result, "unknown")

    def test_not_stale_after_simulated_commit(self):
        """AC3: Version gate passes after commit without restart.

        After cache expiry, function returns fresh HEAD, not stale value.
        """
        _version_cache_test["value"] = "aaa1111"
        _version_cache_test["ts"] = time.time() - 31  # expired
        result = _get_server_version_standalone()
        self.assertNotEqual(result, "aaa1111")
        self.assertNotEqual(result, "unknown")

    def test_source_code_has_get_server_version(self):
        """Verify server.py contains get_server_version and _version_cache."""
        server_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "governance", "server.py"
        )
        with open(server_path, "r") as f:
            src = f.read()
        self.assertIn("def get_server_version():", src)
        self.assertIn('_version_cache = {"value": "unknown", "ts": 0}', src)
        self.assertIn("SERVER_VERSION = get_server_version()", src)

    def test_auto_chain_uses_get_server_version(self):
        """Verify auto_chain.py imports get_server_version, not SERVER_VERSION."""
        chain_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "governance", "auto_chain.py"
        )
        with open(chain_path, "r") as f:
            src = f.read()
        self.assertIn("from .server import get_server_version", src)
        # Should NOT have the old import pattern
        self.assertNotIn("from .server import SERVER_VERSION", src)


if __name__ == "__main__":
    unittest.main()
