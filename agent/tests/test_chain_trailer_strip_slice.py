"""Regression tests for chain_trailer git status parsing.

Covers OPT-BACKLOG-CHAIN-TRAILER-STRIP-SLICE-DROPS-DOT — the strip+slice bug
that dropped the leading dot from the first dirty file path.
"""
from unittest import mock
from agent.governance import chain_trailer


def _mock_status(stdout: str):
    """Return a mock CompletedProcess for git status --porcelain."""
    p = mock.MagicMock()
    p.returncode = 0
    p.stdout = stdout
    return p


def test_dirty_files_preserves_leading_dot_first_line(monkeypatch):
    """First line dot must be preserved (the strip+slice regression)."""
    output = " M .claude/scheduled_tasks.lock\n M .claude/worktrees/foo\n"
    monkeypatch.setattr(chain_trailer, "_git",
                        lambda args, cwd=None, timeout=10:
                        _mock_status(output) if args[0] == "status"
                        else mock.MagicMock(returncode=1, stdout="", stderr=""))
    state = chain_trailer.get_chain_state()
    # Both files should now be filtered by _DIRTY_IGNORE (.claude/), so dirty=False
    assert state["dirty"] is False, \
        f"Expected .claude/* filtered, got dirty_files={state['dirty_files']}"


def test_dirty_files_preserves_dot_when_unfilterable(monkeypatch):
    """First line dot preserved even when path is not in _DIRTY_IGNORE."""
    output = " M .hidden/realfile.py\n M docs/readme.md\n"
    monkeypatch.setattr(chain_trailer, "_git",
                        lambda args, cwd=None, timeout=10:
                        _mock_status(output) if args[0] == "status"
                        else mock.MagicMock(returncode=1, stdout="", stderr=""))
    state = chain_trailer.get_chain_state()
    # Both files survive filter; assert leading dot is preserved on first
    assert ".hidden/realfile.py" in state["dirty_files"], \
        f"Leading dot lost from first line: {state['dirty_files']}"
    assert "docs/readme.md" in state["dirty_files"]


def test_dirty_files_empty_output(monkeypatch):
    """Empty git status returns empty dirty list."""
    monkeypatch.setattr(chain_trailer, "_git",
                        lambda args, cwd=None, timeout=10:
                        _mock_status("") if args[0] == "status"
                        else mock.MagicMock(returncode=1, stdout="", stderr=""))
    state = chain_trailer.get_chain_state()
    assert state["dirty"] is False
    assert state["dirty_files"] == []
