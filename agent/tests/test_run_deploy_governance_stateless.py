"""Tests for auto_chain governance redeploy wiring (PR1-R7).

Covers:
  AC6: auto_chain.py deploy path posts to localhost:40101/api/manager/redeploy/governance
       when changed_files matches agent/governance/**
  AC7: When localhost:40101 is connection-refused, auto_chain.py falls back to legacy
       restart_local_governance and logs a warning containing 'fallback'
"""

import json
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch, call

# Ensure project root on sys.path
_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from agent.governance.auto_chain import (
    _post_manager_redeploy_governance_from_chain,
    _legacy_restart_local_governance_fallback,
)


class TestPostManagerRedeployGovernanceFromChain(unittest.TestCase):
    """AC6: Verify _post_manager_redeploy_governance_from_chain POSTs to manager endpoint."""

    @patch("urllib.request.urlopen")
    def test_posts_to_manager_endpoint(self, mock_urlopen):
        """AC6: When called, POSTs to localhost:40101/api/manager/redeploy/governance."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True, "pid": 1234}).encode()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _post_manager_redeploy_governance_from_chain("abc1234")

        self.assertTrue(result["ok"])
        self.assertEqual(result["pid"], 1234)

        # Verify the request was made to the correct URL
        call_args = mock_urlopen.call_args
        req_obj = call_args[0][0]
        self.assertEqual(req_obj.full_url, "http://localhost:40101/api/manager/redeploy/governance")
        self.assertEqual(req_obj.method, "POST")
        body = json.loads(req_obj.data.decode("utf-8"))
        self.assertEqual(body["chain_version"], "abc1234")

    @patch("agent.governance.auto_chain._legacy_restart_local_governance_fallback")
    @patch("urllib.request.urlopen")
    def test_connection_refused_triggers_fallback(self, mock_urlopen, mock_fallback):
        """AC7: ConnectionRefusedError → fallback to legacy restart_local_governance."""
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError(ConnectionRefusedError("refused"))
        mock_fallback.return_value = {"ok": True, "detail": "legacy ok", "fallback": True}

        result = _post_manager_redeploy_governance_from_chain("def5678")

        mock_fallback.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertTrue(result["fallback"])

    @patch("urllib.request.urlopen")
    def test_connection_refused_logs_fallback_warning(self, mock_urlopen):
        """AC7: When fallback is entered, logs a warning containing 'fallback'."""
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError(ConnectionRefusedError("refused"))

        with patch("agent.governance.auto_chain._legacy_restart_local_governance_fallback",
                    return_value={"ok": False, "detail": "fail", "fallback": True}):
            with self.assertLogs("agent.governance.auto_chain", level="WARNING") as cm:
                _post_manager_redeploy_governance_from_chain("xyz999")

        # Check that at least one log message contains 'fallback'
        fallback_msgs = [m for m in cm.output if "fallback" in m.lower()]
        self.assertTrue(len(fallback_msgs) > 0,
                        f"Expected 'fallback' in warning logs, got: {cm.output}")


class TestFinalizeChainGovernanceRedeploy(unittest.TestCase):
    """Test that _finalize_chain triggers governance redeploy when changed_files match."""

    @patch("agent.governance.auto_chain._post_manager_redeploy_governance_from_chain")
    @patch("agent.governance.auto_chain._try_backlog_close_via_db", return_value=False)
    @patch("agent.governance.auto_chain._finalize_version_sync")
    def test_finalize_chain_calls_redeploy_for_governance_files(
        self, mock_vsync, mock_backlog, mock_redeploy
    ):
        """AC6: _finalize_chain POSTs to manager when changed_files includes governance code."""
        mock_redeploy.return_value = {"ok": True, "pid": 5555}

        from agent.governance.auto_chain import _finalize_chain

        mock_conn = MagicMock()
        result = {"report": {}, "chain_version": "abc123"}
        metadata = {
            "changed_files": ["agent/governance/server.py", "agent/governance/auto_chain.py"],
            "chain_version": "abc123",
        }

        _finalize_chain(mock_conn, "test-project", "task-1", result, metadata)

        mock_redeploy.assert_called_once_with("abc123")

    @patch("agent.governance.auto_chain._post_manager_redeploy_governance_from_chain")
    @patch("agent.governance.auto_chain._try_backlog_close_via_db", return_value=False)
    @patch("agent.governance.auto_chain._finalize_version_sync")
    def test_finalize_chain_skips_redeploy_for_non_governance_files(
        self, mock_vsync, mock_backlog, mock_redeploy
    ):
        """No redeploy when changed_files does NOT include governance code."""
        from agent.governance.auto_chain import _finalize_chain

        mock_conn = MagicMock()
        result = {"report": {}}
        metadata = {
            "changed_files": ["agent/executor_worker.py", "docs/README.md"],
        }

        _finalize_chain(mock_conn, "test-project", "task-2", result, metadata)

        mock_redeploy.assert_not_called()


class TestLegacyFallback(unittest.TestCase):
    """Test the legacy restart_local_governance fallback function."""

    @patch("agent.deploy_chain.restart_local_governance", return_value=(True, "restarted ok"))
    def test_fallback_calls_restart_local_governance(self, mock_restart):
        """Fallback invokes restart_local_governance from deploy_chain."""
        result = _legacy_restart_local_governance_fallback()
        self.assertTrue(result["ok"])
        self.assertTrue(result["fallback"])
        mock_restart.assert_called_once_with(port=40000)

    @patch("agent.deploy_chain.restart_local_governance", side_effect=RuntimeError("boom"))
    def test_fallback_handles_exception(self, mock_restart):
        """Fallback returns ok=False on exception."""
        result = _legacy_restart_local_governance_fallback()
        self.assertFalse(result["ok"])
        self.assertTrue(result["fallback"])


if __name__ == "__main__":
    unittest.main()
