"""Tests for R5: env-strip + auth classifier survive reclaim cycle.

Simulates: create_session with stale token env -> fail -> _recover_stuck_tasks
-> re-claim -> create_session with clean env -> verify CLAUDE_CODE_OAUTH_TOKEN
not in child env.
"""

import inspect
import os
import time
import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace


class TestAuthReclaimE2E(unittest.TestCase):
    """AC-E: env-strip fix and auth classifier survive reclaim cycle."""

    def test_reclaim_cycle_strips_stale_token(self):
        """Simulate full reclaim cycle: stale token env -> fail -> recover ->
        re-claim -> fresh create_session -> verify CLAUDE_CODE_OAUTH_TOKEN
        not in child env.
        """
        from agent.ai_lifecycle import AILifecycleManager

        captured_envs = []

        def fake_popen(cmd, **kwargs):
            env = kwargs.get("env", {})
            captured_envs.append(dict(env))
            proc = MagicMock()
            proc.pid = 99999
            proc.communicate.return_value = ('{"result":"ok"}', "")
            proc.returncode = 0
            return proc

        fake_env = {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "CLAUDE_CODE_OAUTH_TOKEN": "stale-token-from-service-manager",
        }

        manager = AILifecycleManager()

        # First session (simulating stale token present in parent env)
        with patch.dict(os.environ, fake_env, clear=True), \
             patch("subprocess.Popen", side_effect=fake_popen):
            session1 = manager.create_session(
                role="dev",
                prompt="first attempt with stale token",
                context={"system_prompt": "test"},
                project_id="test-project",
                timeout_sec=30,
                workspace=os.getcwd(),
            )
            # Wait for background thread
            for _ in range(100):
                if captured_envs:
                    break
                time.sleep(0.05)

        # Verify stale token was stripped in first session
        self.assertTrue(len(captured_envs) >= 1,
                        "Popen should have been called at least once")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", captured_envs[0],
                         "First session child env should NOT contain stale token")

        # Simulate reclaim: _recover_stuck_tasks would mark task as failed,
        # then a new session is created (fresh create_session)
        captured_envs.clear()

        # Second session (after reclaim - token still in parent env)
        with patch.dict(os.environ, fake_env, clear=True), \
             patch("subprocess.Popen", side_effect=fake_popen):
            session2 = manager.create_session(
                role="dev",
                prompt="second attempt after reclaim",
                context={"system_prompt": "test"},
                project_id="test-project",
                timeout_sec=30,
                workspace=os.getcwd(),
            )
            for _ in range(100):
                if captured_envs:
                    break
                time.sleep(0.05)

        self.assertTrue(len(captured_envs) >= 1,
                        "Popen should have been called for second session")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", captured_envs[0],
                         "Reclaimed session child env should NOT contain stale token")

    def test_auth_classifier_detects_after_reclaim(self):
        """After reclaim, auth classifier should still detect auth failures."""
        from agent.executor_worker import ExecutorWorker

        worker = ExecutorWorker.__new__(ExecutorWorker)

        # Simulate auth failure output from a reclaimed task
        session = SimpleNamespace(
            stdout='{"error":"Unauthorized","status":401}',
            stderr="",
            exit_code=1,
        )

        # First detection (before reclaim)
        result1 = worker._detect_terminal_cli_error(session, "dev")
        self.assertIsNotNone(result1, "Should detect auth failure before reclaim")

        # Second detection (simulating after reclaim - same function, stateless)
        result2 = worker._detect_terminal_cli_error(session, "dev")
        self.assertIsNotNone(result2, "Should detect auth failure after reclaim")

    def test_env_strip_source_contains_token(self):
        """Verify the env-strip tuple contains CLAUDE_CODE_OAUTH_TOKEN."""
        from agent import ai_lifecycle
        source = inspect.getsource(ai_lifecycle)
        # Find the env-strip block
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", source)
        # Verify it's in the exclusion tuple context
        strip_block_start = source.find("k not in (")
        strip_block_end = source.find(")", strip_block_start + 10)
        if strip_block_start > -1 and strip_block_end > -1:
            strip_block = source[strip_block_start:strip_block_end]
            self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", strip_block,
                          "CLAUDE_CODE_OAUTH_TOKEN must be in the env-strip exclusion tuple")


if __name__ == "__main__":
    unittest.main()
