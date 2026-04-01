"""E2E2: Configuration validation tests.

Ensures .mcp.json, start_governance.py, and executor_worker.py defaults
are consistent and correct after governance migration from Docker to host.
"""

import json
import os
import sys
import re
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO_ROOT, "agent"))


class TestMcpJsonConfig(unittest.TestCase):
    """Verify .mcp.json has required environment variables."""

    @classmethod
    def setUpClass(cls):
        mcp_path = os.path.join(_REPO_ROOT, ".mcp.json")
        with open(mcp_path, "r", encoding="utf-8") as f:
            cls.mcp = json.load(f)
        cls.server_cfg = cls.mcp.get("mcpServers", {}).get("aming-claw", {})
        cls.env = cls.server_cfg.get("env", {})

    def test_governance_url_is_host_port_40000(self):
        """Governance URL must point to host port 40000 (not Docker 40006)."""
        url = self.env.get("GOVERNANCE_URL", "")
        self.assertIn("40000", url, f"GOVERNANCE_URL should use port 40000, got: {url}")
        self.assertNotIn("40006", url, "GOVERNANCE_URL still references old Docker port 40006")

    def test_memory_backend_is_docker(self):
        """MEMORY_BACKEND must be 'docker' for semantic search via dbservice."""
        self.assertEqual(self.env.get("MEMORY_BACKEND"), "docker",
                         "MEMORY_BACKEND should be 'docker' in .mcp.json")

    def test_telegram_bot_token_present(self):
        """TELEGRAM_BOT_TOKEN must be set (non-empty)."""
        token = self.env.get("TELEGRAM_BOT_TOKEN", "")
        self.assertTrue(len(token) > 10, "TELEGRAM_BOT_TOKEN missing or too short in .mcp.json")

    def test_server_command_and_cwd(self):
        """MCP server command and cwd should be set."""
        self.assertIn("agent.mcp.server", " ".join(self.server_cfg.get("args", [])))
        self.assertTrue(self.server_cfg.get("cwd"), "cwd must be set in .mcp.json")


class TestGovernanceServerDefaults(unittest.TestCase):
    """Verify governance server.py default port matches host setup."""

    def test_default_port_is_40000(self):
        from governance.server import PORT
        self.assertEqual(PORT, 40000,
                         f"server.py PORT default should be 40000 (host), got {PORT}")


class TestExecutorWorkerDefaults(unittest.TestCase):
    """Verify executor_worker.py default GOVERNANCE_URL matches host setup."""

    def test_default_governance_url_is_40000(self):
        # Read the source to check default (avoid import side effects)
        ew_path = os.path.join(_REPO_ROOT, "agent", "executor_worker.py")
        with open(ew_path, "r", encoding="utf-8") as f:
            content = f.read()
        match = re.search(r'GOVERNANCE_URL\s*=\s*os\.getenv\([^,]+,\s*"([^"]+)"\)', content)
        self.assertIsNotNone(match, "Could not find GOVERNANCE_URL default in executor_worker.py")
        default_url = match.group(1)
        self.assertIn("40000", default_url, f"Default GOVERNANCE_URL should use 40000, got: {default_url}")
        self.assertNotIn("40006", default_url, "Default GOVERNANCE_URL still references old Docker port 40006")


class TestStartGovernanceDefaults(unittest.TestCase):
    """Verify start_governance.py sets correct port."""

    def test_default_port_env_is_40000(self):
        sg_path = os.path.join(_REPO_ROOT, "start_governance.py")
        with open(sg_path, "r", encoding="utf-8") as f:
            content = f.read()
        match = re.search(r'setdefault\("GOVERNANCE_PORT",\s*"(\d+)"\)', content)
        self.assertIsNotNone(match, "Could not find GOVERNANCE_PORT default in start_governance.py")
        self.assertEqual(match.group(1), "40000",
                         f"start_governance.py default port should be 40000, got {match.group(1)}")


if __name__ == "__main__":
    unittest.main()
