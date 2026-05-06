"""Tests for branch/worktree scoped reconcile catch-up."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from agent.governance.reconcile_scope_catchup import (
    DEFAULT_PHASES,
    ensure_catchup_worktree,
    run_scope_catchup,
)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return proc.stdout.strip()


def _commit(repo: Path, name: str, content: str) -> str:
    target = repo / name
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", "update " + name)
    return _git(repo, "rev-parse", "HEAD")


def test_ensure_catchup_worktree_creates_and_fast_forwards(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    first = _commit(repo, "a.txt", "one")

    worktree = repo / ".worktrees" / "catchup"
    info = ensure_catchup_worktree(
        repo,
        worktree_path=worktree,
        branch="codex/test-catchup",
        base_ref=first,
    )

    assert info["action"] == "created"
    assert info["worktree_head"] == first
    assert _git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "codex/test-catchup"

    second = _commit(repo, "a.txt", "two")
    updated = ensure_catchup_worktree(
        repo,
        worktree_path=worktree,
        branch="codex/test-catchup",
        base_ref=second,
    )

    assert updated["action"] == "fast_forwarded"
    assert updated["worktree_head"] == second


def test_run_scope_catchup_writes_output_and_marks_scan_only(tmp_path):
    repo = tmp_path / "repo"
    worktree = repo / ".worktrees" / "catchup"
    repo.mkdir(parents=True)
    worktree.mkdir(parents=True)

    fake_worktree = {
        "action": "created",
        "repo_root": str(repo),
        "worktree_path": str(worktree),
        "branch": "codex/test-catchup",
        "base_ref": "HEAD",
        "base_commit": "abcdef123",
        "base_short": "abcdef1",
        "worktree_head": "abcdef123",
    }
    fake_sweep = {
        "commits": ["c1"],
        "all_discrepancies": [{"type": "x"}],
        "dedup_discrepancies": [{"type": "x"}],
        "hot_files": ["a.py"],
        "covered_hot": ["a.py"],
        "coverage_pct": 1.0,
        "baseline_written": False,
    }

    with patch(
        "agent.governance.reconcile_scope_catchup._rev_parse",
        return_value="abcdef1",
    ), patch(
        "agent.governance.reconcile_scope_catchup.ensure_catchup_worktree",
        return_value=fake_worktree,
    ) as mock_ensure, patch(
        "agent.governance.reconcile_scope_catchup.run_commit_sweep_orchestrated",
        return_value=fake_sweep,
    ) as mock_sweep:
        result = run_scope_catchup(
            project_id="aming-claw",
            repo_root=repo,
            branch="codex/test-catchup",
            worktree_path=worktree,
            phases=["K", "A"],
            dry_run=True,
        )

    assert result["no_redeploy"] is True
    assert result["doc_update_mode"] == "scan_only"
    assert result["materialization_backlog"]["actionable_count"] == 0
    assert result["materialization_backlog"]["filed"] is False
    assert result["summary"]["coverage_pct"] == 1.0
    assert Path(result["result_path"]).exists()
    written = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))
    assert written["summary"]["commits"] == 1
    mock_ensure.assert_called_once()
    mock_sweep.assert_called_once_with(
        "aming-claw",
        str(worktree),
        since_baseline=None,
        phases=["K", "A"],
        dry_run=True,
    )


def test_default_scope_catchup_phases_exclude_global_chain_closure():
    assert DEFAULT_PHASES == ["K", "A", "E", "D", "F"]
    assert "G" not in DEFAULT_PHASES


def test_run_scope_catchup_can_file_materialization_backlog(tmp_path):
    repo = tmp_path / "repo"
    worktree = repo / ".worktrees" / "catchup"
    repo.mkdir(parents=True)
    worktree.mkdir(parents=True)

    fake_worktree = {
        "action": "created",
        "repo_root": str(repo),
        "worktree_path": str(worktree),
        "branch": "codex/test-catchup",
        "base_ref": "HEAD",
        "base_commit": "abcdef123",
        "base_short": "abcdef1",
        "worktree_head": "abcdef123",
    }
    fake_sweep = {
        "commits": ["c1"],
        "all_discrepancies": [],
        "dedup_discrepancies": [
            {
                "type": "unmapped_file",
                "detail": "agent/new_runtime.py",
                "confidence": "low",
            },
            {
                "type": "unmapped_medium_conf_suggest",
                "detail": "file=agent/tests/test_new_runtime.py candidates=[('L7.1', 0.7)]",
                "confidence": "medium",
            },
        ],
        "hot_files": ["agent/new_runtime.py", "agent/tests/test_new_runtime.py"],
        "covered_hot": ["agent/new_runtime.py", "agent/tests/test_new_runtime.py"],
        "coverage_pct": 1.0,
        "baseline_written": False,
    }
    filed = {
        "enabled": True,
        "filed": True,
        "bug_id": "RECONCILE-SCOPE-MATERIALIZE-TEST",
        "actionable_count": 2,
    }

    with patch(
        "agent.governance.reconcile_scope_catchup._rev_parse",
        return_value="abcdef1",
    ), patch(
        "agent.governance.reconcile_scope_catchup.ensure_catchup_worktree",
        return_value=fake_worktree,
    ), patch(
        "agent.governance.reconcile_scope_catchup.run_commit_sweep_orchestrated",
        return_value=fake_sweep,
    ), patch(
        "agent.governance.reconcile_scope_catchup._file_materialization_backlog",
        return_value=filed,
    ) as mock_file:
        result = run_scope_catchup(
            project_id="aming-claw",
            repo_root=repo,
            branch="codex/test-catchup",
            worktree_path=worktree,
            dry_run=True,
            file_materialization_backlog=True,
            materialization_bug_id="RECONCILE-SCOPE-MATERIALIZE-TEST",
        )

    assert result["materialization_backlog"]["filed"] is True
    assert result["materialization_backlog"]["actionable_count"] == 2
    mock_file.assert_called_once()
    summary = mock_file.call_args.kwargs["summary"]
    assert summary["bug_id"] == "RECONCILE-SCOPE-MATERIALIZE-TEST"
    assert summary["target_files"] == [
        "agent/new_runtime.py",
        "agent/tests/test_new_runtime.py",
    ]
    assert summary["test_files"] == ["agent/tests/test_new_runtime.py"]


def test_apply_blocks_baseline_when_actionable_drift_is_unfiled(tmp_path):
    repo = tmp_path / "repo"
    worktree = repo / ".worktrees" / "catchup"
    repo.mkdir(parents=True)
    worktree.mkdir(parents=True)

    fake_worktree = {
        "action": "created",
        "repo_root": str(repo),
        "worktree_path": str(worktree),
        "branch": "codex/test-catchup",
        "base_ref": "HEAD",
        "base_commit": "abcdef123",
        "base_short": "abcdef1",
        "worktree_head": "abcdef123",
    }
    fake_sweep = {
        "commits": ["c1"],
        "all_discrepancies": [],
        "dedup_discrepancies": [
            {"type": "unmapped_file", "detail": "agent/new_runtime.py"},
        ],
        "hot_files": ["agent/new_runtime.py"],
        "covered_hot": ["agent/new_runtime.py"],
        "coverage_pct": 1.0,
        "baseline_written": False,
    }

    with patch(
        "agent.governance.reconcile_scope_catchup._rev_parse",
        return_value="abcdef1",
    ), patch(
        "agent.governance.reconcile_scope_catchup.ensure_catchup_worktree",
        return_value=fake_worktree,
    ), patch(
        "agent.governance.reconcile_scope_catchup.run_commit_sweep_orchestrated",
        return_value=fake_sweep,
    ) as mock_sweep:
        result = run_scope_catchup(
            project_id="aming-claw",
            repo_root=repo,
            branch="codex/test-catchup",
            worktree_path=worktree,
            dry_run=False,
        )

    assert result["summary"]["baseline_written"] is False
    assert "baseline_blocked_reason" in result
    assert mock_sweep.call_count == 1
    assert mock_sweep.call_args.kwargs["dry_run"] is True
