"""Tests for handle_backlog_close commit verification (OPT-BACKLOG-CH5).

AC6: At least 3 test functions covering real commit, fake commit, empty commit.
AC8: All tests use unittest.mock.patch to mock subprocess.run.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest


def _make_ctx(bug_id="BUG-001", commit="abc123", project_id="test-proj"):
    """Build a minimal RequestContext-like object for handle_backlog_close."""
    ctx = MagicMock()
    ctx.path_params = {"project_id": project_id, "bug_id": bug_id}
    ctx.body = {"commit": commit, "actor": "test"}
    return ctx


@pytest.fixture
def _mock_db():
    """Patch get_connection so SELECT returns a row and UPDATE/commit succeed."""
    with patch("agent.governance.server.get_connection") as mock_gc:
        conn = MagicMock()
        # SELECT returns a row (bug exists) with OPEN status for close eligibility
        conn.execute.return_value.fetchone.return_value = {"bug_id": "BUG-001", "status": "OPEN"}
        mock_gc.return_value = conn
        yield conn


@pytest.fixture
def _mock_audit():
    """Patch audit_service.record to no-op."""
    with patch("agent.governance.server.audit_service") as mock_audit:
        yield mock_audit


@patch("agent.governance.server.subprocess.run")
def test_close_with_real_commit(_mock_subprocess, _mock_db, _mock_audit):
    """AC1/AC5: When commit resolves (returncode=0), close succeeds."""
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    ctx = _make_ctx(commit="abc123")

    result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["status"] == "FIXED"
    _mock_subprocess.assert_called_once()
    call_args = _mock_subprocess.call_args
    assert "git" in call_args[0][0]
    assert "rev-parse" in call_args[0][0]
    assert "--verify" in call_args[0][0]
    assert "abc123" in call_args[0][0]


@patch("agent.governance.server.subprocess.run")
def test_close_with_fake_commit(_mock_subprocess, _mock_db, _mock_audit):
    """AC2: When commit doesn't resolve (returncode!=0), raise 422 commit_not_found."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=1, stderr="fatal: not a valid object")
    ctx = _make_ctx(commit="deadbeef999")

    with pytest.raises(GovernanceError) as exc_info:
        handle_backlog_close(ctx)

    assert exc_info.value.code == "commit_not_found"
    assert exc_info.value.status == 422


@patch("agent.governance.server.subprocess.run")
def test_close_with_empty_commit(_mock_subprocess, _mock_db, _mock_audit):
    """AC5: When commit is empty string, skip verification entirely."""
    from agent.governance.server import handle_backlog_close

    ctx = _make_ctx(commit="")

    result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["status"] == "FIXED"
    _mock_subprocess.assert_not_called()


@patch("agent.governance.server.subprocess.run")
def test_close_with_timeout(_mock_subprocess, _mock_db, _mock_audit):
    """AC3: When git times out, log warning and allow close."""
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)
    ctx = _make_ctx(commit="abc123")

    result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["status"] == "FIXED"


@patch("agent.governance.server.subprocess.run")
def test_close_with_git_not_found(_mock_subprocess, _mock_db, _mock_audit):
    """AC4: When git binary not found, log warning and allow close."""
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.side_effect = FileNotFoundError("git not found")
    ctx = _make_ctx(commit="abc123")

    result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["status"] == "FIXED"
