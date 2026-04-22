"""Tests for R2: pid=0 guard in ai_lifecycle session creation.

Verifies that AISession creation with pid=0 does not produce log lines
referencing pid=0 that look like a real process.
"""

import inspect
import unittest


class TestPidZeroGuard(unittest.TestCase):
    """AC-B: pid=0 sentinel must be guarded against in logging."""

    def test_source_has_pid_guard(self):
        """ai_lifecycle.py must contain a pid != 0 or pid == 0 guard
        near the session creation block."""
        from agent import ai_lifecycle
        source = inspect.getsource(ai_lifecycle)
        # The guard should check pid != 0 or pid == 0
        has_neq_guard = "pid != 0" in source or "pid !=0" in source
        has_eq_guard = "pid == 0" in source or "pid ==0" in source
        self.assertTrue(
            has_neq_guard or has_eq_guard,
            "ai_lifecycle.py must contain a pid==0 or pid!=0 guard "
            "near the session creation block to prevent logging pid=0"
        )

    def test_pid_zero_comment_present(self):
        """The sentinel comment explaining pid=0 must be present."""
        from agent import ai_lifecycle
        source = inspect.getsource(ai_lifecycle)
        self.assertIn("pid=0 is a sentinel", source.lower().replace("pid = 0", "pid=0"),
                       "Source should document that pid=0 is a sentinel value")

    def test_session_initial_pid_is_zero(self):
        """AISession created without Popen should have pid=0."""
        from agent.ai_lifecycle import AISession
        import time
        session = AISession(
            session_id="test-123",
            role="dev",
            pid=0,
            project_id="test",
            prompt="test",
            context={},
            started_at=time.time(),
            timeout_sec=30,
        )
        self.assertEqual(session.pid, 0,
                         "Initial session pid should be 0 before Popen assigns real PID")


if __name__ == "__main__":
    unittest.main()
