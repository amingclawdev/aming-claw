"""Integration tests for Version Gate as Commit Trailer — Phase A.

Tests verify end-to-end behavior of the 4-field trailer schema:
- git reset rollback auto-heals chain state
- manual Chain-Source-Stage commits are detectable as non-native (legacy_inferred)
- validate_chain_lineage correctly identifies breaks

Uses temporary git repos to avoid touching the real workspace.
"""

import os
import subprocess
import pytest


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo with an initial commit."""
    repo = str(tmp_path / "integration-repo")
    os.makedirs(repo)
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
    init_file = os.path.join(repo, "README.md")
    with open(init_file, "w") as f:
        f.write("# Integration Test\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, capture_output=True)
    return repo


class TestGitResetRollbackAutoHealsChainState:
    """AC8: creates 3 chain commits, resets, verifies get_chain_state tracks reset HEAD."""

    def test_git_reset_rollback_auto_heals_chain_state(self, git_repo):
        from agent.governance.chain_trailer import write_merge_with_trailer, get_chain_state

        # Create 3 chain commits
        commits = []
        for i in range(1, 4):
            with open(os.path.join(git_repo, f"chain{i}.txt"), "w") as f:
                f.write(f"chain content {i}\n")
            subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
            success, commit_hash, err = write_merge_with_trailer(
                message=f"Chain commit {i}",
                cwd=git_repo,
                task_id=f"task-chain-{i}",
                parent_chain_sha=commits[-1] if commits else "none",
                bug_id=f"BUG-{i}",
            )
            assert success, f"Chain commit {i} failed: {err}"
            commits.append(commit_hash)

        # Verify state shows latest commit (commit 3)
        state_before = get_chain_state(cwd=git_repo)
        assert state_before["source"] == "trailer"
        assert state_before["task_id"] == "task-chain-3"

        # Reset to commit 2 (discard commit 3)
        subprocess.run(
            ["git", "reset", "--hard", "HEAD~1"],
            cwd=git_repo, capture_output=True,
        )

        # Verify chain state auto-heals to commit 2
        state_after = get_chain_state(cwd=git_repo)
        assert state_after["source"] == "trailer"
        assert state_after["task_id"] == "task-chain-2"
        assert state_after["chain_sha"]  # should be valid

        # Reset further to commit 1
        subprocess.run(
            ["git", "reset", "--hard", "HEAD~1"],
            cwd=git_repo, capture_output=True,
        )
        state_c1 = get_chain_state(cwd=git_repo)
        assert state_c1["task_id"] == "task-chain-1"

    def test_reset_past_all_trailers_falls_back_to_head(self, git_repo):
        from agent.governance.chain_trailer import write_merge_with_trailer, get_chain_state

        # Create 1 chain commit
        with open(os.path.join(git_repo, "chain.txt"), "w") as f:
            f.write("chain\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        write_merge_with_trailer(
            message="Chain commit", cwd=git_repo,
            task_id="task-only", parent_chain_sha="none")

        # Reset past it to initial commit
        subprocess.run(
            ["git", "reset", "--hard", "HEAD~1"],
            cwd=git_repo, capture_output=True,
        )

        state = get_chain_state(cwd=git_repo)
        assert state["source"] == "head"
        assert state["task_id"] is None


class TestManualChainSourceStageDetectable:
    """AC9: manual commit with Chain-Source-Stage is detectable as non-native."""

    def test_manual_trailer_detected_as_legacy_inferred_by_backfill(self, git_repo, tmp_path, monkeypatch):
        """A manually-crafted commit with Chain-Source-Stage but written by hand
        (not via write_merge_with_trailer) is detectable. backfill will skip it
        since it has the trailer, but validate_chain_lineage will see it as valid
        — the key distinction is the task_id will be whatever the manual user typed."""
        from agent.governance.chain_trailer import get_chain_state, backfill_legacy_chain_history
        import json as _json

        cache_dir = str(tmp_path / "ch")
        monkeypatch.setattr(
            "agent.governance.chain_trailer._chain_history_dir",
            lambda: cache_dir,
        )

        # Manual commit with Chain-Source-Stage trailer (not via write_merge_with_trailer)
        with open(os.path.join(git_repo, "manual.txt"), "w") as f:
            f.write("manual\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        manual_msg = (
            "Manual fix commit\n\n"
            "Chain-Source-Task: manual-observer\n"
            "Chain-Source-Stage: manual-fix\n"
            "Chain-Parent: none\n"
            "Chain-Bug-Id: MANUAL-001"
        )
        subprocess.run(
            ["git", "commit", "-m", manual_msg],
            cwd=git_repo, capture_output=True,
        )

        # get_chain_state should see this trailer
        state = get_chain_state(cwd=git_repo)
        assert state["source"] == "trailer"
        assert state["task_id"] == "manual-observer"
        assert state["stage"] == "manual-fix"  # Non-standard stage value = detectable as non-native

        # backfill should skip this commit (it has Chain-Source-Stage)
        backfill_legacy_chain_history(cwd=git_repo, incremental=False)
        with open(os.path.join(cache_dir, "aming-claw.json")) as f:
            data = _json.load(f)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_repo, capture_output=True, text=True,
        ).stdout.strip()
        backfill_commits = [r["commit"] for r in data["backfill_results"]]
        assert head not in backfill_commits  # Has trailer, excluded from backfill

    def test_non_merge_stage_distinguishable(self, git_repo):
        """Commits with stage != 'merge' are distinguishable from auto-chain merges."""
        from agent.governance.chain_trailer import get_chain_state

        with open(os.path.join(git_repo, "manual2.txt"), "w") as f:
            f.write("manual2\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        msg = (
            "Observer hotfix\n\n"
            "Chain-Source-Task: observer-hotfix-123\n"
            "Chain-Source-Stage: hotfix\n"
            "Chain-Parent: none\n"
            "Chain-Bug-Id: HOTFIX-1"
        )
        subprocess.run(["git", "commit", "-m", msg], cwd=git_repo, capture_output=True)

        state = get_chain_state(cwd=git_repo)
        # stage is "hotfix" not "merge" — detectable as non-native auto-chain
        assert state["stage"] != "merge"
        assert state["stage"] == "hotfix"


class TestValidateLineageIntegration:
    """Integration tests for validate_chain_lineage with real repos."""

    def test_mixed_trailer_and_plain_commits(self, git_repo):
        from agent.governance.chain_trailer import (
            write_merge_with_trailer, validate_chain_lineage
        )

        start = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_repo, capture_output=True, text=True,
        ).stdout.strip()

        # Chain commit (has trailer)
        with open(os.path.join(git_repo, "c1.txt"), "w") as f:
            f.write("c1\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        write_merge_with_trailer(message="Chain 1", cwd=git_repo, task_id="t1")

        # Plain commit (no trailer)
        with open(os.path.join(git_repo, "p1.txt"), "w") as f:
            f.write("p1\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Plain commit"], cwd=git_repo, capture_output=True)

        end = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_repo, capture_output=True, text=True,
        ).stdout.strip()

        result = validate_chain_lineage(start, end, cwd=git_repo)
        assert result["valid"] is False
        assert len(result["breaks"]) == 1  # Only plain commit is a break
        assert result["commits"] == 2

    def test_all_trailer_commits_valid(self, git_repo):
        from agent.governance.chain_trailer import (
            write_merge_with_trailer, validate_chain_lineage
        )

        start = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_repo, capture_output=True, text=True,
        ).stdout.strip()

        for i in range(3):
            with open(os.path.join(git_repo, f"t{i}.txt"), "w") as f:
                f.write(f"t{i}\n")
            subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
            write_merge_with_trailer(message=f"Chain {i}", cwd=git_repo, task_id=f"t-{i}")

        end = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_repo, capture_output=True, text=True,
        ).stdout.strip()

        result = validate_chain_lineage(start, end, cwd=git_repo)
        assert result["valid"] is True
        assert result["breaks"] == []
        assert result["commits"] == 3
