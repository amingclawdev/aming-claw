"""Tests for _DIRTY_IGNORE filtering in auto_chain.py.

Verifies that the startswith-prefix filter used by the version gate
correctly classifies dirty files as governed or non-governed.
"""

import pytest

from agent.governance.auto_chain import _DIRTY_IGNORE


def _is_filtered(path: str) -> bool:
    """Reproduce the same startswith filter used at line 1845 of auto_chain.py."""
    return any(path.startswith(p) for p in _DIRTY_IGNORE)


@pytest.mark.parametrize(
    "dirty_path, should_be_filtered",
    [
        # --- Files that SHOULD be filtered (non-governed / runtime-state) ---
        (".recent-tasks.json", True),
        (".claude/settings.local.json", True),
        (".claude\\settings.local.json", True),
        (".governance-cache/foo", True),
        (".governance-cache\\bar", True),
        (".observer-cache/state.json", True),
        (".observer-cache\\state.json", True),
        (".worktrees/dev-task-123", True),
        ("docs/dev/notes.md", True),
        # --- Files that MUST NOT be filtered (governed source) ---
        ("agent/foo.py", False),
        (".gitignore", False),
        ("claude/no-dot", False),
        ("governance-cache-typo/foo", False),
        ("observer-cache-typo/foo", False),
        ("src/main.py", False),
    ],
    ids=[
        "recent-tasks-json-filtered",
        "claude-settings-filtered",
        "claude-backslash-filtered",
        "governance-cache-filtered",
        "governance-cache-backslash-filtered",
        "observer-cache-filtered",
        "observer-cache-backslash-filtered",
        "worktrees-filtered",
        "docs-dev-filtered",
        "agent-foo-NOT-filtered",
        "gitignore-NOT-filtered",
        "claude-no-dot-NOT-filtered",
        "governance-cache-typo-NOT-filtered",
        "observer-cache-typo-NOT-filtered",
        "src-main-NOT-filtered",
    ],
)
def test_dirty_ignore_filter(dirty_path: str, should_be_filtered: bool) -> None:
    """Each dirty_path is checked against _DIRTY_IGNORE using startswith."""
    result = _is_filtered(dirty_path)
    assert result is should_be_filtered, (
        f"Expected _is_filtered({dirty_path!r}) == {should_be_filtered}, got {result}"
    )
