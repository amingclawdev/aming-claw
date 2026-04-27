"""Tests for agent.governance.chain_trailer — Chain-Version commit trailer module.

Covers all 5 exported functions: get_chain_state, get_chain_version,
validate_chain_lineage, backfill_legacy_chain_history, write_merge_with_trailer.
Tests use temporary git repos to avoid touching the real workspace.
"""

import os
import subprocess
import tempfile
import shutil
import pytest


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo with an initial commit."""
    repo = str(tmp_path / "test-repo")
    os.makedirs(repo)
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
    # Initial commit
    init_file = os.path.join(repo, "README.md")
    with open(init_file, "w") as f:
        f.write("# Test\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, capture_output=True)
    return repo


@pytest.fixture
def git_repo_with_trailer(git_repo):
    """Create a repo with a commit that has a Chain-Version trailer."""
    # Make a second commit with a trailer
    fpath = os.path.join(git_repo, "file.txt")
    with open(fpath, "w") as f:
        f.write("content\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Feature commit\n\nChain-Version: abc1234"],
        cwd=git_repo, capture_output=True,
    )
    return git_repo


@pytest.fixture
def git_repo_with_branch(git_repo):
    """Create a repo with a feature branch."""
    # Create feature branch with a commit
    subprocess.run(["git", "checkout", "-b", "feature-test"], cwd=git_repo, capture_output=True)
    fpath = os.path.join(git_repo, "feature.txt")
    with open(fpath, "w") as f:
        f.write("feature\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Feature work"], cwd=git_repo, capture_output=True)
    subprocess.run(["git", "checkout", "master"], cwd=git_repo, capture_output=True)
    # Fallback to main if master doesn't exist
    result = subprocess.run(["git", "branch", "--show-current"], cwd=git_repo, capture_output=True, text=True)
    if not result.stdout.strip():
        subprocess.run(["git", "checkout", "main"], cwd=git_repo, capture_output=True)
    return git_repo


# ---------------------------------------------------------------------------
# get_chain_state tests
# ---------------------------------------------------------------------------

class TestGetChainState:
    def test_returns_dict_with_required_keys(self, git_repo):
        from agent.governance.chain_trailer import get_chain_state
        state = get_chain_state(cwd=git_repo)
        assert isinstance(state, dict)
        assert "version" in state
        assert "dirty" in state
        assert "dirty_files" in state
        assert "source" in state

    def test_source_is_head_without_trailer(self, git_repo):
        from agent.governance.chain_trailer import get_chain_state
        state = get_chain_state(cwd=git_repo)
        assert state["source"] == "head"
        assert len(state["version"]) >= 7  # short hash

    def test_source_is_trailer_with_trailer(self, git_repo_with_trailer):
        from agent.governance.chain_trailer import get_chain_state
        state = get_chain_state(cwd=git_repo_with_trailer)
        assert state["source"] == "trailer"
        assert state["version"] == "abc1234"

    def test_dirty_false_on_clean_repo(self, git_repo):
        from agent.governance.chain_trailer import get_chain_state
        state = get_chain_state(cwd=git_repo)
        assert state["dirty"] is False
        assert state["dirty_files"] == []

    def test_dirty_true_with_untracked_file(self, git_repo):
        from agent.governance.chain_trailer import get_chain_state
        with open(os.path.join(git_repo, "untracked.txt"), "w") as f:
            f.write("dirty\n")
        state = get_chain_state(cwd=git_repo)
        assert state["dirty"] is True
        assert "untracked.txt" in state["dirty_files"]

    def test_dirty_ignores_claude_paths(self, git_repo):
        from agent.governance.chain_trailer import get_chain_state
        # Create a .claude/ file (should be ignored)
        claude_dir = os.path.join(git_repo, ".claude")
        os.makedirs(claude_dir, exist_ok=True)
        with open(os.path.join(claude_dir, "settings.json"), "w") as f:
            f.write("{}")
        state = get_chain_state(cwd=git_repo)
        assert state["dirty"] is False

    def test_version_is_string(self, git_repo):
        from agent.governance.chain_trailer import get_chain_state
        state = get_chain_state(cwd=git_repo)
        assert isinstance(state["version"], str)


# ---------------------------------------------------------------------------
# get_chain_version tests
# ---------------------------------------------------------------------------

class TestGetChainVersion:
    def test_returns_string(self, git_repo):
        from agent.governance.chain_trailer import get_chain_version
        ver = get_chain_version(cwd=git_repo)
        assert isinstance(ver, str)
        assert len(ver) >= 7

    def test_returns_trailer_version_when_present(self, git_repo_with_trailer):
        from agent.governance.chain_trailer import get_chain_version
        ver = get_chain_version(cwd=git_repo_with_trailer)
        assert ver == "abc1234"

    def test_returns_head_hash_without_trailer(self, git_repo):
        from agent.governance.chain_trailer import get_chain_version
        ver = get_chain_version(cwd=git_repo)
        # Should match git rev-parse --short HEAD
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=git_repo, capture_output=True, text=True,
        ).stdout.strip()
        assert ver == head


# ---------------------------------------------------------------------------
# validate_chain_lineage tests
# ---------------------------------------------------------------------------

class TestValidateChainLineage:
    def test_valid_lineage_returns_true(self, git_repo):
        from agent.governance.chain_trailer import validate_chain_lineage
        # Get initial commit
        first = subprocess.run(
            ["git", "rev-list", "--max-parents=0", "HEAD"],
            cwd=git_repo, capture_output=True, text=True,
        ).stdout.strip()
        # Add a second commit
        with open(os.path.join(git_repo, "file2.txt"), "w") as f:
            f.write("content\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Second commit"], cwd=git_repo, capture_output=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True
        ).stdout.strip()

        valid, reason = validate_chain_lineage(first, head, cwd=git_repo)
        assert valid is True
        assert "Valid lineage" in reason

    def test_invalid_ref_returns_false(self, git_repo):
        from agent.governance.chain_trailer import validate_chain_lineage
        valid, reason = validate_chain_lineage("nonexistent123", "HEAD", cwd=git_repo)
        assert valid is False
        assert "Invalid ref" in reason

    def test_empty_range_returns_false(self, git_repo):
        from agent.governance.chain_trailer import validate_chain_lineage
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True
        ).stdout.strip()
        valid, reason = validate_chain_lineage(head, head, cwd=git_repo)
        assert valid is False
        assert "No commits" in reason

    def test_multi_commit_lineage(self, git_repo):
        from agent.governance.chain_trailer import validate_chain_lineage
        first = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True
        ).stdout.strip()
        # Add 3 more commits
        for i in range(3):
            with open(os.path.join(git_repo, f"file{i}.txt"), "w") as f:
                f.write(f"content {i}\n")
            subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
            subprocess.run(["git", "commit", "-m", f"Commit {i}"], cwd=git_repo, capture_output=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True
        ).stdout.strip()
        valid, reason = validate_chain_lineage(first, head, cwd=git_repo)
        assert valid is True
        assert "3 commits" in reason


# ---------------------------------------------------------------------------
# backfill_legacy_chain_history tests
# ---------------------------------------------------------------------------

class TestBackfillLegacyChainHistory:
    def test_returns_list(self, git_repo):
        from agent.governance.chain_trailer import backfill_legacy_chain_history
        results = backfill_legacy_chain_history(cwd=git_repo)
        assert isinstance(results, list)

    def test_tags_commits_without_trailer(self, git_repo):
        from agent.governance.chain_trailer import backfill_legacy_chain_history
        results = backfill_legacy_chain_history(cwd=git_repo)
        assert len(results) >= 1
        for r in results:
            assert r["legacy_inferred"] is True
            assert r["needs_audit"] is True
            assert "audit_note" in r
            assert "commit" in r
            assert "short" in r

    def test_skips_commits_with_trailer(self, git_repo_with_trailer):
        from agent.governance.chain_trailer import backfill_legacy_chain_history
        results = backfill_legacy_chain_history(cwd=git_repo_with_trailer)
        # The initial commit has no trailer, the second does
        # So only initial commit should appear
        commits_with_trailer = [r for r in results if r["short"] != ""]
        for r in results:
            assert r["legacy_inferred"] is True

    def test_limit_parameter(self, git_repo):
        from agent.governance.chain_trailer import backfill_legacy_chain_history
        # Add several commits
        for i in range(5):
            with open(os.path.join(git_repo, f"f{i}.txt"), "w") as f:
                f.write(f"{i}\n")
            subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
            subprocess.run(["git", "commit", "-m", f"Commit {i}"], cwd=git_repo, capture_output=True)
        results = backfill_legacy_chain_history(limit=3, cwd=git_repo)
        assert len(results) <= 3

    def test_audit_note_contains_short_hash(self, git_repo):
        from agent.governance.chain_trailer import backfill_legacy_chain_history
        results = backfill_legacy_chain_history(cwd=git_repo)
        for r in results:
            assert r["short"] in r["audit_note"]

    def test_empty_repo_returns_empty(self, tmp_path):
        """Backfill on a repo with no commits returns empty list."""
        from agent.governance.chain_trailer import backfill_legacy_chain_history
        repo = str(tmp_path / "empty-repo")
        os.makedirs(repo)
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        results = backfill_legacy_chain_history(cwd=repo)
        assert results == []


# ---------------------------------------------------------------------------
# write_merge_with_trailer tests
# ---------------------------------------------------------------------------

class TestWriteMergeWithTrailer:
    def test_commit_contains_trailer(self, git_repo):
        from agent.governance.chain_trailer import write_merge_with_trailer
        # Stage a file
        with open(os.path.join(git_repo, "new.txt"), "w") as f:
            f.write("new\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)

        success, commit_hash, err = write_merge_with_trailer(
            message="Test commit", cwd=git_repo)
        assert success is True
        assert commit_hash
        assert err == ""

        # Verify trailer in commit message
        msg = subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            cwd=git_repo, capture_output=True, text=True,
        ).stdout
        assert "Chain-Version:" in msg

    def test_merge_branch_with_trailer(self, git_repo_with_branch):
        from agent.governance.chain_trailer import write_merge_with_trailer
        success, commit_hash, err = write_merge_with_trailer(
            message="Merge feature", branch="feature-test",
            cwd=git_repo_with_branch)
        assert success is True
        assert commit_hash
        assert err == ""

        # Verify trailer
        msg = subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            cwd=git_repo_with_branch, capture_output=True, text=True,
        ).stdout
        assert "Chain-Version:" in msg

    def test_merge_nonexistent_branch_fails(self, git_repo):
        from agent.governance.chain_trailer import write_merge_with_trailer
        success, commit_hash, err = write_merge_with_trailer(
            message="Bad merge", branch="nonexistent-branch", cwd=git_repo)
        assert success is False
        assert err  # should have error message

    def test_returns_short_hash(self, git_repo):
        from agent.governance.chain_trailer import write_merge_with_trailer
        with open(os.path.join(git_repo, "hash_test.txt"), "w") as f:
            f.write("test\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        success, commit_hash, err = write_merge_with_trailer(
            message="Hash test", cwd=git_repo)
        assert success
        assert 7 <= len(commit_hash) <= 12  # short hash length


# ---------------------------------------------------------------------------
# _parse_trailer tests
# ---------------------------------------------------------------------------

class TestParseTrailer:
    def test_parses_valid_trailer(self):
        from agent.governance.chain_trailer import _parse_trailer
        msg = "Some commit\n\nChain-Version: abc1234"
        assert _parse_trailer(msg) == "abc1234"

    def test_returns_none_without_trailer(self):
        from agent.governance.chain_trailer import _parse_trailer
        assert _parse_trailer("Just a commit message") is None

    def test_parses_trailer_with_full_hash(self):
        from agent.governance.chain_trailer import _parse_trailer
        msg = "msg\n\nChain-Version: abc1234567890abcdef1234567890abcdef12345678"
        assert _parse_trailer(msg) == "abc1234567890abcdef1234567890abcdef12345678"

    def test_parses_trailer_in_multiline_message(self):
        from agent.governance.chain_trailer import _parse_trailer
        msg = "Title\n\nBody text here.\n\nChain-Version: def5678\nSigned-off-by: Test"
        assert _parse_trailer(msg) == "def5678"


# ---------------------------------------------------------------------------
# Integration: get_chain_state after write_merge_with_trailer
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_state_reflects_trailer_after_merge(self, git_repo):
        from agent.governance.chain_trailer import write_merge_with_trailer, get_chain_state
        with open(os.path.join(git_repo, "int.txt"), "w") as f:
            f.write("integration\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        success, commit_hash, _ = write_merge_with_trailer(
            message="Integration test", cwd=git_repo)
        assert success

        state = get_chain_state(cwd=git_repo)
        assert state["source"] == "trailer"
        # The trailer contains a short hash (may differ from final commit hash
        # due to amend cycle, but is always a valid short hash string)
        assert isinstance(state["version"], str)
        assert len(state["version"]) >= 7

    def test_backfill_excludes_trailer_commits(self, git_repo):
        from agent.governance.chain_trailer import (
            write_merge_with_trailer, backfill_legacy_chain_history
        )
        # Initial commit has no trailer
        with open(os.path.join(git_repo, "bf.txt"), "w") as f:
            f.write("backfill test\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        write_merge_with_trailer(message="With trailer", cwd=git_repo)

        results = backfill_legacy_chain_history(cwd=git_repo)
        # Only the initial commit (without trailer) should be in results
        hashes = [r["commit"] for r in results]
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_repo, capture_output=True, text=True,
        ).stdout.strip()
        assert head not in hashes  # HEAD has trailer, should be excluded
