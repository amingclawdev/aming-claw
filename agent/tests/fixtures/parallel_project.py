"""Generated git fixtures for parallel branch scenario tests."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ParallelFixtureProject:
    root: Path
    main_head: str


@dataclass(frozen=True)
class MergePreviewFixtureProject(ParallelFixtureProject):
    clean_branch: str
    conflict_branch: str


@dataclass(frozen=True)
class ParallelTaskBranch:
    task_id: str
    branch_name: str
    branch_ref: str
    base_commit: str
    head_commit: str
    changed_path: str


@dataclass(frozen=True)
class PB001RestartFixtureProject(ParallelFixtureProject):
    batch_base_commit: str
    target_head_after_t1: str
    task_branches: dict[str, ParallelTaskBranch]


def git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def create_parallel_fixture_project(tmp_path: Path, *, name: str = "parallel-project") -> ParallelFixtureProject:
    """Create a deterministic git-backed project for branch/worktree tests."""
    repo = tmp_path / name
    repo.mkdir()
    git(["init"], cwd=repo)
    git(["checkout", "-b", "main"], cwd=repo)
    git(["config", "user.email", "test@example.com"], cwd=repo)
    git(["config", "user.name", "Test User"], cwd=repo)

    (repo / "README.md").write_text("# Parallel Fixture Project\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "service.py").write_text(
        "def service_entry():\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_service.py").write_text(
        "from src.service import service_entry\n\n"
        "def test_service_entry():\n"
        "    assert service_entry() == 'ok'\n",
        encoding="utf-8",
    )
    git(["add", "."], cwd=repo)
    git(["commit", "-m", "base fixture"], cwd=repo)
    main_head = git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    return ParallelFixtureProject(root=repo, main_head=main_head)


def _commit_branch_file(
    repo: Path,
    *,
    branch_name: str,
    base_ref: str,
    path: str,
    body: str,
    message: str,
) -> ParallelTaskBranch:
    git(["checkout", "-B", branch_name, base_ref], cwd=repo)
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    git(["add", path], cwd=repo)
    git(["commit", "-m", message], cwd=repo)
    head = git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    task_id = branch_name.split("-", 2)[1]
    return ParallelTaskBranch(
        task_id=task_id,
        branch_name=branch_name,
        branch_ref=f"refs/heads/{branch_name}",
        base_commit=git(["rev-parse", base_ref], cwd=repo).stdout.strip(),
        head_commit=head,
        changed_path=path,
    )


def create_pb001_restart_fixture_project(tmp_path: Path) -> PB001RestartFixtureProject:
    """Create PB-001's five-task restart topology in an isolated git repo."""
    fixture = create_parallel_fixture_project(tmp_path, name="pb001-restart-project")
    repo = fixture.root
    batch_base_commit = fixture.main_head

    branches: dict[str, ParallelTaskBranch] = {}
    branches["T1"] = _commit_branch_file(
        repo,
        branch_name="codex/PB001-T1-scope-reconcile",
        base_ref="main",
        path="docs/scope_reconcile.md",
        body="# Scope Reconcile\n\nT1 foundation change.\n",
        message="PB001 T1 scope reconcile",
    )
    git(["checkout", "main"], cwd=repo)
    git(["merge", "--ff-only", branches["T1"].branch_name], cwd=repo)
    target_head_after_t1 = git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()

    branches["T2"] = _commit_branch_file(
        repo,
        branch_name="codex/PB001-T2-branch-graph-refs",
        base_ref=branches["T1"].branch_name,
        path="docs/branch_graph_refs.md",
        body="# Branch Graph Refs\n\nT2 graph ref candidate change.\n",
        message="PB001 T2 branch graph refs",
    )
    branches["T3"] = _commit_branch_file(
        repo,
        branch_name="codex/PB001-T3-task-runtime",
        base_ref=branches["T1"].branch_name,
        path="src/task_runtime.py",
        body="def task_runtime_marker():\n    return 'T3'\n",
        message="PB001 T3 task runtime",
    )
    branches["T4"] = _commit_branch_file(
        repo,
        branch_name="codex/PB001-T4-dashboard-read-model",
        base_ref=branches["T2"].branch_name,
        path="docs/dashboard_read_model.md",
        body="# Dashboard Read Model\n\nT4 queued read-model change.\n",
        message="PB001 T4 dashboard read model",
    )
    branches["T5"] = _commit_branch_file(
        repo,
        branch_name="codex/PB001-T5-chain-adapter",
        base_ref=branches["T3"].branch_name,
        path="src/chain_adapter.py",
        body="def chain_adapter_marker():\n    return 'T5'\n",
        message="PB001 T5 chain adapter",
    )
    git(["checkout", "main"], cwd=repo)

    return PB001RestartFixtureProject(
        root=repo,
        main_head=target_head_after_t1,
        batch_base_commit=batch_base_commit,
        target_head_after_t1=target_head_after_t1,
        task_branches=branches,
    )


def create_merge_preview_fixture_project(tmp_path: Path) -> MergePreviewFixtureProject:
    """Create a git project with clean and conflicting feature branches."""
    fixture = create_parallel_fixture_project(tmp_path, name="merge-preview-project")
    repo = fixture.root

    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    git(["add", "shared.txt"], cwd=repo)
    git(["commit", "-m", "shared base"], cwd=repo)

    git(["checkout", "-b", "feature-clean"], cwd=repo)
    (repo / "clean.txt").write_text("clean\n", encoding="utf-8")
    git(["add", "clean.txt"], cwd=repo)
    git(["commit", "-m", "clean branch"], cwd=repo)

    git(["checkout", "main"], cwd=repo)
    git(["checkout", "-b", "feature-conflict"], cwd=repo)
    (repo / "shared.txt").write_text("branch\n", encoding="utf-8")
    git(["commit", "-am", "conflict branch"], cwd=repo)

    git(["checkout", "main"], cwd=repo)
    (repo / "shared.txt").write_text("main\n", encoding="utf-8")
    git(["commit", "-am", "main change"], cwd=repo)
    main_head = git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    return MergePreviewFixtureProject(
        root=repo,
        main_head=main_head,
        clean_branch="feature-clean",
        conflict_branch="feature-conflict",
    )
