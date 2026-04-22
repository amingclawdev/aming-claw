"""Tests for R3: auth failure classification in _detect_terminal_cli_error.

Verifies that JSON-shaped auth failure responses (401, Unauthorized,
invalid_token) are classified as terminal errors.
"""

import unittest
from unittest.mock import MagicMock
from types import SimpleNamespace


def _make_session(stdout="", stderr="", exit_code=1):
    """Create a mock session with stdout/stderr."""
    return SimpleNamespace(stdout=stdout, stderr=stderr, exit_code=exit_code)


class TestAuthFailureClassifier(unittest.TestCase):
    """AC-C: _detect_terminal_cli_error must catch auth-failure JSON patterns."""

    def setUp(self):
        from agent.executor_worker import ExecutorWorker
        self.worker = ExecutorWorker.__new__(ExecutorWorker)

    def test_unauthorized_json_detected(self):
        """JSON with 'Unauthorized' should be classified as terminal."""
        session = _make_session(stdout='{"error":"Unauthorized"}')
        result = self.worker._detect_terminal_cli_error(session, "dev")
        self.assertIsNotNone(result)
        self.assertIn("Auth failure", result)

    def test_401_status_detected(self):
        """JSON with status 401 should be classified as terminal."""
        session = _make_session(stdout='{"error":"auth failed","status":401}')
        result = self.worker._detect_terminal_cli_error(session, "dev")
        self.assertIsNotNone(result)
        self.assertIn("Auth failure", result)

    def test_invalid_token_detected(self):
        """Response containing 'invalid_token' should be classified as terminal."""
        session = _make_session(stdout='{"error":"invalid_token","message":"Token expired"}')
        result = self.worker._detect_terminal_cli_error(session, "dev")
        self.assertIsNotNone(result)
        self.assertIn("Auth failure", result)

    def test_authentication_error_detected(self):
        """Response containing 'authentication_error' should be terminal."""
        session = _make_session(stderr='authentication_error: bad credentials')
        result = self.worker._detect_terminal_cli_error(session, "dev")
        self.assertIsNotNone(result)

    def test_normal_output_not_flagged(self):
        """Normal successful output should return None."""
        session = _make_session(stdout='{"result":"success","files_changed":3}', exit_code=0)
        result = self.worker._detect_terminal_cli_error(session, "dev")
        self.assertIsNone(result)

    def test_coordinator_type_skipped(self):
        """Coordinator task type should always return None (skipped)."""
        session = _make_session(stdout='{"error":"Unauthorized"}')
        result = self.worker._detect_terminal_cli_error(session, "coordinator")
        self.assertIsNone(result)

    def test_403_with_error_json_detected(self):
        """JSON with status 403 and 'error' key should be classified as terminal."""
        session = _make_session(stdout='{"error":"Forbidden","status":403}')
        result = self.worker._detect_terminal_cli_error(session, "dev")
        self.assertIsNotNone(result)
        self.assertIn("Auth failure", result)

    def test_max_turns_still_works(self):
        """Existing 'reached max turns' detection should still work."""
        session = _make_session(stdout="Error: Reached max turns (60)")
        result = self.worker._detect_terminal_cli_error(session, "dev")
        self.assertIsNotNone(result)
        self.assertIn("max turns", result.lower())


class TestAuthFailureSourceGrep(unittest.TestCase):
    """Static verification that the source contains auth patterns."""

    def test_source_contains_unauthorized(self):
        """_detect_terminal_cli_error source must reference 'unauthorized'."""
        import inspect
        from agent.executor_worker import ExecutorWorker
        source = inspect.getsource(ExecutorWorker._detect_terminal_cli_error)
        self.assertIn("unauthorized", source.lower(),
                       "Must contain 'unauthorized' pattern for auth failure detection")

    def test_source_contains_401(self):
        """_detect_terminal_cli_error source must reference '401'."""
        import inspect
        from agent.executor_worker import ExecutorWorker
        source = inspect.getsource(ExecutorWorker._detect_terminal_cli_error)
        self.assertIn("401", source,
                       "Must contain '401' pattern for HTTP auth failure detection")


if __name__ == "__main__":
    unittest.main()
