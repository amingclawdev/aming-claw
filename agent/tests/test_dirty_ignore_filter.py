"""Tests for shared dirty-worktree filtering.

Verifies that governance gates classify dirty files consistently as governed
or local/generated.
"""

import subprocess

import pytest

from agent.governance.auto_chain import _DIRTY_IGNORE
from agent.governance.dirty_worktree import filter_dirty_files, is_ignored_dirty_path
from agent.governance.state_reconcile import _git_dirty_files


def _is_filtered(path: str) -> bool:
    return is_ignored_dirty_path(path)


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
        (".codex/config.toml", True),
        (".codex\\config.toml", True),
        (".hypothesis/unicode_data/14.0.0/charmap.json.gz", True),
        (".hypothesis\\unicode_data\\14.0.0\\charmap.json.gz", True),
        (".venv/lib/python/site-packages/example.py", True),
        (".venv\\Scripts\\python.exe", True),
        (".worktrees/dev-task-123", True),
        ("build/lib/module.py", True),
        ("build\\lib\\module.py", True),
        ("docs/dev/notes.md", True),
        (".aming-claw/cache/state.json", True),
        # --- Files that MUST NOT be filtered (governed source) ---
        ("AGENTS.md", False),
        ("agent/foo.py", False),
        (".gitignore", False),
        ("claude/no-dot", False),
        ("codex/no-dot", False),
        ("hypothesis/no-dot", False),
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
        "codex-filtered",
        "codex-backslash-filtered",
        "hypothesis-filtered",
        "hypothesis-backslash-filtered",
        "venv-filtered",
        "venv-backslash-filtered",
        "worktrees-filtered",
        "build-filtered",
        "build-backslash-filtered",
        "docs-dev-filtered",
        "aming-cache-filtered",
        "agents-md-NOT-filtered",
        "agent-foo-NOT-filtered",
        "gitignore-NOT-filtered",
        "claude-no-dot-NOT-filtered",
        "codex-no-dot-NOT-filtered",
        "hypothesis-no-dot-NOT-filtered",
        "governance-cache-typo-NOT-filtered",
        "observer-cache-typo-NOT-filtered",
        "src-main-NOT-filtered",
    ],
)
def test_dirty_ignore_filter(dirty_path: str, should_be_filtered: bool) -> None:
    result = _is_filtered(dirty_path)
    assert result is should_be_filtered, (
        f"Expected _is_filtered({dirty_path!r}) == {should_be_filtered}, got {result}"
    )


def test_auto_chain_exports_shared_dirty_ignore_prefixes() -> None:
    assert _DIRTY_IGNORE
    assert filter_dirty_files([".venv/lib/example.py", "agent/foo.py"]) == ["agent/foo.py"]


def test_scope_reconcile_dirty_files_uses_shared_filter(monkeypatch, tmp_path) -> None:
    stdout = "\n".join([
        "?? .codex/",
        "?? .hypothesis/",
        "?? .venv/",
        "?? build/",
        "?? AGENTS.md",
        " M agent/governance/server.py",
    ])

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("agent.governance.state_reconcile.subprocess.run", fake_run)

    assert _git_dirty_files(tmp_path) == [
        "AGENTS.md",
        "agent/governance/server.py",
    ]
