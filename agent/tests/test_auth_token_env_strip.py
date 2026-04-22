"""Tests for R1: CLAUDE_CODE_OAUTH_TOKEN env-strip in ai_lifecycle.

Verifies that CLAUDE_CODE_OAUTH_TOKEN is excluded from the env dict
passed to Claude CLI subprocesses so stale tokens inherited from
service_manager launch env are never forwarded.
"""

import os
import unittest
from unittest.mock import patch, MagicMock
import inspect


class TestAuthTokenEnvStrip(unittest.TestCase):
    """AC-A: CLAUDE_CODE_OAUTH_TOKEN must be stripped from child env."""

    def _get_env_from_create_session(self, extra_env=None):
        """Helper: call create_session and capture the env dict passed to Popen.

        Mocks _build_system_prompt to avoid role_permissions import,
        and _build_claude_command to return a simple command.
        """
        from agent.ai_lifecycle import AILifecycleManager

        manager = AILifecycleManager()
        fake_env = {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "CLAUDE_CODE_OAUTH_TOKEN": "stale-token-123",
            "CLAUDECODE": "1",
            "CLAUDE_CODE_ENTRYPOINT": "/bin/claude",
            "NORMAL_VAR": "keep-me",
        }
        if extra_env:
            fake_env.update(extra_env)

        captured_env = {}

        def fake_popen(cmd, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            proc = MagicMock()
            proc.pid = 12345
            proc.communicate.return_value = ('{"result":"ok"}', "")
            proc.returncode = 0
            return proc

        with patch.dict(os.environ, fake_env, clear=True), \
             patch("subprocess.Popen", side_effect=fake_popen), \
             patch.object(manager, "_build_system_prompt", return_value="test system prompt"), \
             patch.object(manager, "_build_claude_command", return_value=["echo", "test"]):
            session = manager.create_session(
                role="dev",
                prompt="test prompt",
                context={"system_prompt": "test"},
                project_id="test-project",
                timeout_sec=30,
                workspace=os.getcwd(),
            )
            # Wait briefly for background thread to invoke Popen
            import time
            for _ in range(100):
                if captured_env:
                    break
                time.sleep(0.05)

        return captured_env

    def test_oauth_token_stripped_from_child_env(self):
        """CLAUDE_CODE_OAUTH_TOKEN must NOT appear in child process env."""
        env = self._get_env_from_create_session()
        self.assertTrue(len(env) > 0, "Popen should have been called")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env,
                         "Stale CLAUDE_CODE_OAUTH_TOKEN should be stripped from child env")

    def test_other_claude_vars_also_stripped(self):
        """Other CLAUDE_CODE_* vars in the strip tuple should also be absent."""
        env = self._get_env_from_create_session()
        self.assertTrue(len(env) > 0, "Popen should have been called")
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", env)

    def test_normal_vars_preserved(self):
        """Non-excluded env vars should still be passed through."""
        env = self._get_env_from_create_session()
        self.assertTrue(len(env) > 0, "Popen should have been called")
        self.assertIn("NORMAL_VAR", env)
        self.assertEqual(env["NORMAL_VAR"], "keep-me")


class TestEnvStripTupleContainsToken(unittest.TestCase):
    """Static verification that the source contains the token in the strip tuple."""

    def test_source_contains_oauth_token_in_strip(self):
        """grep-equivalent: CLAUDE_CODE_OAUTH_TOKEN appears in ai_lifecycle.py."""
        from agent import ai_lifecycle
        source = inspect.getsource(ai_lifecycle)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", source,
                       "CLAUDE_CODE_OAUTH_TOKEN must appear in ai_lifecycle.py source")


if __name__ == "__main__":
    unittest.main()
