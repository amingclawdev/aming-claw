"""Tests for agent.governance.cron_reconcile module."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent.governance.cron_reconcile import cron_reconcile_v2, _LOG_FILE


class _FakeResponse:
    """Minimal requests.Response stand-in."""

    def __init__(self, json_body, status_code=200):
        self._json = json_body
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


# ---------------------------------------------------------------------------
# AC-CR1: signature defaults and payload correctness
# ---------------------------------------------------------------------------

class TestCronReconcileV2Signature:
    """AC-CR1: dry_run=True default, auto_fix_threshold logic."""

    @patch("agent.governance.cron_reconcile.requests.post")
    @patch("agent.governance.cron_reconcile._LOG_FILE", "nonexistent/cron.log")
    def test_dry_run_default_sends_threshold_none(self, mock_post, tmp_path):
        """dry_run=True (default) → auto_fix_threshold='none'."""
        mock_post.return_value = _FakeResponse({"summary": {"nodes_checked": 10}})

        with patch("agent.governance.cron_reconcile._LOG_FILE", str(tmp_path / "cron.log")):
            result = cron_reconcile_v2(gov_url="http://fake:1234", project_id="test-proj")

        # Verify the POST payload
        call_args = mock_post.call_args
        posted_json = call_args[1].get("json") or call_args[0][1] if len(call_args[0]) > 1 else call_args[1]["json"]
        assert posted_json["dry_run"] is True
        assert posted_json["auto_fix_threshold"] == "none"

    @patch("agent.governance.cron_reconcile.requests.post")
    def test_apply_mode_sends_threshold_high(self, mock_post, tmp_path):
        """dry_run=False → auto_fix_threshold='high'."""
        mock_post.return_value = _FakeResponse({"summary": {"fixed": 2}})

        with patch("agent.governance.cron_reconcile._LOG_FILE", str(tmp_path / "cron.log")):
            result = cron_reconcile_v2(dry_run=False, gov_url="http://fake:1234", project_id="test-proj")

        call_args = mock_post.call_args
        posted_json = call_args[1].get("json") or call_args[1]["json"]
        assert posted_json["dry_run"] is False
        assert posted_json["auto_fix_threshold"] == "high"

    def test_default_dry_run_is_true(self):
        """Function signature has dry_run=True as default."""
        import inspect
        sig = inspect.signature(cron_reconcile_v2)
        assert sig.parameters["dry_run"].default is True


# ---------------------------------------------------------------------------
# AC-CR3: log file assertions
# ---------------------------------------------------------------------------

class TestCronReconcileLogging:
    """AC-CR3: response logged to logs/cron-reconcile.log with timestamp + summary."""

    @patch("agent.governance.cron_reconcile.requests.post")
    def test_log_contains_timestamp_and_summary(self, mock_post, tmp_path):
        """Log entry must have timestamp and summary fields."""
        mock_post.return_value = _FakeResponse({
            "summary": {"nodes_checked": 42, "issues_found": 3}
        })

        log_file = tmp_path / "cron-reconcile.log"
        with patch("agent.governance.cron_reconcile._LOG_FILE", str(log_file)):
            cron_reconcile_v2(gov_url="http://fake:1234", project_id="test-proj")

        assert log_file.exists(), "Log file should be created"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1

        entry = json.loads(lines[0])
        assert "timestamp" in entry, "Log entry must contain 'timestamp'"
        assert "summary" in entry, "Log entry must contain 'summary'"
        assert entry["summary"]["nodes_checked"] == 42

    @patch("agent.governance.cron_reconcile.requests.post")
    def test_log_appends_multiple_runs(self, mock_post, tmp_path):
        """Multiple invocations append to the same log file."""
        mock_post.return_value = _FakeResponse({"summary": {"ok": True}})

        log_file = tmp_path / "cron-reconcile.log"
        with patch("agent.governance.cron_reconcile._LOG_FILE", str(log_file)):
            cron_reconcile_v2(gov_url="http://fake:1234", project_id="test-proj")
            cron_reconcile_v2(gov_url="http://fake:1234", project_id="test-proj")

        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2

    @patch("agent.governance.cron_reconcile.requests.post")
    def test_error_is_logged(self, mock_post, tmp_path):
        """Network errors are logged with status='error'."""
        mock_post.side_effect = Exception("connection refused")

        log_file = tmp_path / "cron-reconcile.log"
        with patch("agent.governance.cron_reconcile._LOG_FILE", str(log_file)):
            result = cron_reconcile_v2(gov_url="http://fake:1234", project_id="test-proj")

        assert result["status"] == "error"
        entry = json.loads(log_file.read_text().strip())
        assert entry["status"] == "error"
        assert "timestamp" in entry


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------

class TestCronReconcileURL:
    """Verify correct URL construction for the reconcile-v2 endpoint."""

    @patch("agent.governance.cron_reconcile.requests.post")
    def test_url_contains_project_id(self, mock_post, tmp_path):
        mock_post.return_value = _FakeResponse({"summary": {}})

        with patch("agent.governance.cron_reconcile._LOG_FILE", str(tmp_path / "cron.log")):
            cron_reconcile_v2(gov_url="http://localhost:40000", project_id="my-proj")

        # First call is to reconcile-v2, second is audit
        url_called = mock_post.call_args_list[0][0][0]
        assert url_called == "http://localhost:40000/api/wf/my-proj/reconcile-v2"


# ---------------------------------------------------------------------------
# Audit emission
# ---------------------------------------------------------------------------

class TestAuditEmission:
    """R3: audit_log entry emitted for traceability."""

    @patch("agent.governance.cron_reconcile.requests.post")
    def test_audit_post_called(self, mock_post, tmp_path):
        """Two POSTs: one to reconcile-v2, one to audit."""
        mock_post.return_value = _FakeResponse({"summary": {}})

        with patch("agent.governance.cron_reconcile._LOG_FILE", str(tmp_path / "cron.log")):
            cron_reconcile_v2(gov_url="http://fake:1234", project_id="test-proj")

        # Should have at least 2 calls: reconcile-v2 + audit
        assert mock_post.call_count >= 2
        urls = [c[0][0] for c in mock_post.call_args_list]
        assert any("reconcile-v2" in u for u in urls)
        assert any("audit" in u for u in urls)
