"""Tests for shared dirty-worktree filtering.

Verifies that governance gates classify dirty files consistently as governed
or local/generated.
"""

import subprocess

import pytest

from agent.governance import parallel_branch_runtime as pbr
from agent.governance import server
from agent.governance.auto_chain import _DIRTY_IGNORE
from agent.governance.dirty_worktree import (
    CandidateGitStatusError,
    candidate_dirty_status_from_porcelain_z,
    filter_dirty_files,
    is_ignored_dirty_path,
)
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
        ("daily_planner/__pycache__/models.cpython-314.pyc", True),
        ("tests\\__pycache__\\test_models.cpython-314-pytest-9.1.1.pyc", True),
        ("src/__pycache__/", True),
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
        ("src/main.pyc", False),
        ("src/my__pycache__/main.pyc", False),
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
        "nested-python-cache-filtered",
        "nested-python-cache-backslash-filtered",
        "python-cache-directory-filtered",
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
        "arbitrary-pyc-NOT-filtered",
        "python-cache-lookalike-NOT-filtered",
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
    generated_cache = tmp_path / "src/__pycache__/app.cpython-314.pyc"
    generated_cache.parent.mkdir(parents=True)
    generated_cache.write_bytes(b"generated bytecode fixture\n")
    arbitrary_pyc = tmp_path / "src/app.pyc"
    arbitrary_pyc.write_bytes(b"governed arbitrary bytecode-shaped file\n")

    assert server._runtime_context_git_dirty_files(str(tmp_path)) == [
        "src/app.js",
        "src/app.pyc",
    ]


def test_worker_commit_dirty_diagnostic_is_exact_and_host_safe() -> None:
    dirty_files = ["src/app.pyc", "src/main.py"]

    details = server._runtime_context_worker_commit_dirty_worktree_details(
        dirty_files
    )

    assert details["field"] == "dirty_files"
    assert details["expected"] == []
    assert details["actual"] == dirty_files
    assert details["dirty_files"] == dirty_files
    assert details["source"].startswith(
        "git status --porcelain=v1 --untracked-files=all"
    )
    assert details["host_safe"] is True
    assert "do not recursively clean" in details["guide"]
    assert details["next_legal_action"] == (
        "commit_owned_changes_or_remove_only_listed_non_generated_dirty_files"
    )
    assert details["generated_artifact_policy"] == {
        "ignored_directory_components": ["__pycache__"],
        "arbitrary_pyc_outside_ignored_components_is_governed": True,
    }


def _candidate_git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "source.py").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "source.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    return tmp_path


def test_candidate_status_classifies_all_untracked_descendants_before_diagnostic_cap(tmp_path):
    root = _candidate_git_repo(tmp_path)
    cache = root / ".aming-claw/cache"
    cache.mkdir(parents=True)
    for index in range(60):
        (cache / f"generated-{index:03}.json").write_text("{}", encoding="utf-8")
    (root / "late-governed.py").write_text("change\n", encoding="utf-8")
    status = candidate_dirty_status_from_porcelain_z(
        subprocess.check_output(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=root,
        ),
    )
    assert status.dirty_files == ("late-governed.py",)
    assert status.dirty_file_count == 1
    assert status.ignored_untracked_count == 60
    assert server._git_dirty_paths(root) == ["late-governed.py"]


def test_candidate_status_never_exempts_tracked_cache_or_parent(tmp_path):
    root = _candidate_git_repo(tmp_path)
    tracked = root / ".aming-claw/cache/tracked.json"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", str(tracked.relative_to(root))], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "tracked cache"], cwd=root, check=True)
    tracked.write_text("changed\n", encoding="utf-8")
    (root / ".aming-claw" / "other.json").write_text("{}", encoding="utf-8")
    assert server._git_dirty_paths(root) == [
        ".aming-claw/cache/tracked.json", ".aming-claw/other.json",
    ]


def test_candidate_status_preserves_rename_and_literal_special_names(tmp_path):
    root = _candidate_git_repo(tmp_path)
    target = "renamed\tline\nquote\"slash\\Unicode-雪.py"
    (root / "source.py").rename(root / target)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    assert server._git_dirty_paths(root) == sorted(["source.py", target])


def test_candidate_status_preserves_untracked_arrow_and_literal_backslash(tmp_path):
    root = _candidate_git_repo(tmp_path)
    names = ["a -> b\t雪.txt", ".aming-claw\\cache\\not-a-child.json"]
    for name in names:
        (root / name).write_text("untracked\n", encoding="utf-8")
    assert server._git_dirty_paths(root) == sorted(names)


@pytest.mark.parametrize("raw", [
    b"?? source.py", b"? source.py\0", b"R  target.py\0",
    b"!! ignored.py\0", b"?? ../outside.py\0", b"  clean.py\0",
    b"?? bad-\xff.py\0",
])
def test_candidate_status_malformed_porcelain_fails_closed(raw):
    with pytest.raises(CandidateGitStatusError, match="candidate_git_status_malformed"):
        candidate_dirty_status_from_porcelain_z(raw)


def test_candidate_status_caps_diagnostics_after_complete_classification():
    raw = b"".join(f"?? src/file-{index:03}.py\0".encode() for index in range(80))
    status = candidate_dirty_status_from_porcelain_z(raw)
    assert status.dirty_file_count == 80
    assert status.dirty_files_truncated is True
    assert len(status.dirty_files) == 50


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "malformed"])
def test_candidate_git_command_failure_never_looks_clean(monkeypatch, tmp_path, failure):
    def failed_run(args, **kwargs):
        assert args == ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"]
        assert kwargs["text"] is False
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 10)
        return subprocess.CompletedProcess(
            args, 1 if failure == "nonzero" else 0,
            stdout=b"" if failure == "nonzero" else b"?? incomplete",
            stderr=b"failure" if failure == "nonzero" else b"",
        )
    monkeypatch.setattr(server.subprocess, "run", failed_run)
    with pytest.raises(CandidateGitStatusError, match="candidate_git_status_"):
        server._git_dirty_paths(tmp_path)


def test_candidate_status_is_scoped_to_assigned_linked_worktree(tmp_path):
    parent = tmp_path / "repo"
    parent.mkdir()
    _candidate_git_repo(parent)
    assigned = tmp_path / "assigned-worker"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "candidate-worker", str(assigned)],
        cwd=parent, check=True,
    )
    cache = assigned / ".aming-claw/cache/branches/worker"
    cache.mkdir(parents=True)
    (cache / "graph.branch.overlay.json").write_text("{}", encoding="utf-8")
    sibling = parent / "sibling-source.py"
    sibling.write_text("not in assigned root\n", encoding="utf-8")
    assert server._git_dirty_paths(assigned) == []
    assert server._git_dirty_paths(parent) == ["sibling-source.py"]
    (assigned / "source.py").write_text("changed\n", encoding="utf-8")
    assert server._git_dirty_paths(assigned) == ["source.py"]
