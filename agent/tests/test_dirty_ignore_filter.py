"""Tests for shared dirty-worktree filtering.

Verifies that governance gates classify dirty files consistently as governed
or local/generated.
"""

import subprocess

import pytest

from agent.governance import parallel_branch_runtime as pbr
from agent.governance import server
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
        (".aming-claw-demo-environment.json", True),
        (".aming-claw/cache/state.json", True),
        # --- Files that MUST NOT be filtered (governed source) ---
        ("AGENTS.md", False),
        ("agent/foo.py", False),
        (".gitignore", False),
        (".aming-claw-demo-environment.json.bak", False),
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
        "demo-environment-marker-filtered",
        "aming-cache-filtered",
        "agents-md-NOT-filtered",
        "agent-foo-NOT-filtered",
        "gitignore-NOT-filtered",
        "demo-environment-marker-suffix-NOT-filtered",
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
        "?? .aming-claw-demo-environment.json",
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


def test_parallel_merge_dirty_files_uses_shared_filter(monkeypatch, tmp_path) -> None:
    stdout = "\n".join([
        "?? .aming-claw-demo-environment.json",
        "?? .worktrees/row-a/",
        " M src/app.js",
    ])

    def fake_preview_command(repo_root, args, *, timeout_seconds):
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(pbr, "_git_preview_command", fake_preview_command)

    assert pbr._git_worktree_dirty_files(tmp_path, timeout_seconds=30) == [
        "src/app.js",
    ]


def test_runtime_context_dirty_files_uses_shared_filter(tmp_path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    graph_cache = tmp_path / ".aming-claw/cache/branches/worker"
    graph_cache.mkdir(parents=True)
    for name in ("graph.base.json", "graph.branch.overlay.json", "manifest.json"):
        (graph_cache / name).write_text("{}\n", encoding="utf-8")
    source = tmp_path / "src/app.js"
    source.parent.mkdir(parents=True)
    source.write_text("export {};\n", encoding="utf-8")

    assert server._runtime_context_git_dirty_files(str(tmp_path)) == [
        "src/app.js",
    ]
