"""Tests for agent.governance.chain_trailer — 4-field Chain trailer module.

Phase A rewrite: covers 4-field trailer schema (Chain-Source-Task, Chain-Source-Stage,
Chain-Parent, Chain-Bug-Id), first-parent walk, lineage validation, backfill, and rollback.
Tests use temporary git repos to avoid touching the real workspace.
"""

import json
import os
import subprocess
import tempfile
import pytest


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo with an initial commit."""
    repo = str(tmp_path / "test-repo")
    os.makedirs(repo)
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
    init_file = os.path.join(repo, "README.md")
    with open(init_file, "w") as f:
        f.write("# Test\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, capture_output=True)
    return repo


@pytest.fixture
def git_repo_with_4field_trailer(git_repo):
    """Create a repo with a commit that has a 4-field trailer."""
    fpath = os.path.join(git_repo, "file.txt")
    with open(fpath, "w") as f:
        f.write("content\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
    msg = (
        "Feature commit\n\n"
        "Chain-Source-Task: task-abc123\n"
        "Chain-Source-Stage: merge\n"
        "Chain-Parent: def456\n"
        "Chain-Bug-Id: BUG-001"
    )
    subprocess.run(["git", "commit", "-m", msg], cwd=git_repo, capture_output=True)
    return git_repo


@pytest.fixture
def git_repo_with_legacy_trailer(git_repo):
    """Create a repo with a legacy Chain-Version trailer."""
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
    subprocess.run(["git", "checkout", "-b", "feature-test"], cwd=git_repo, capture_output=True)
    fpath = os.path.join(git_repo, "feature.txt")
    with open(fpath, "w") as f:
        f.write("feature\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Feature work"], cwd=git_repo, capture_output=True)
    subprocess.run(["git", "checkout", "master"], cwd=git_repo, capture_output=True)
    result = subprocess.run(["git", "branch", "--show-current"], cwd=git_repo, capture_output=True, text=True)
    if not result.stdout.strip():
        subprocess.run(["git", "checkout", "main"], cwd=git_repo, capture_output=True)
    return git_repo


# ---------------------------------------------------------------------------
# _parse_trailer tests
# ---------------------------------------------------------------------------

class TestParseTrailer:
    def test_parses_4field_stage(self):
        from agent.governance.chain_trailer import _parse_trailer
        msg = "Some commit\n\nChain-Source-Stage: merge"
        assert _parse_trailer(msg) == "merge"

    def test_returns_none_without_trailer(self):
        from agent.governance.chain_trailer import _parse_trailer
        assert _parse_trailer("Just a commit message") is None

    def test_falls_back_to_legacy_chain_version(self):
        from agent.governance.chain_trailer import _parse_trailer
        msg = "msg\n\nChain-Version: abc1234"
        assert _parse_trailer(msg) == "abc1234"

    def test_prefers_4field_over_legacy(self):
        from agent.governance.chain_trailer import _parse_trailer
        msg = "msg\n\nChain-Source-Stage: deploy\nChain-Version: abc1234"
        assert _parse_trailer(msg) == "deploy"


# ---------------------------------------------------------------------------
# _parse_4field_trailer tests
# ---------------------------------------------------------------------------

class TestParse4FieldTrailer:
    def test_parses_all_four_fields(self):
        from agent.governance.chain_trailer import _parse_4field_trailer
        msg = (
            "msg\n\n"
            "Chain-Source-Task: task-123\n"
            "Chain-Source-Stage: merge\n"
            "Chain-Parent: abc123\n"
            "Chain-Bug-Id: BUG-001"
        )
        fields = _parse_4field_trailer(msg)
        assert fields["task_id"] == "task-123"
        assert fields["stage"] == "merge"
        assert fields["parent_sha"] == "abc123"
        assert fields["bug_id"] == "BUG-001"

    def test_returns_none_for_missing_fields(self):
        from agent.governance.chain_trailer import _parse_4field_trailer
        msg = "plain commit message"
        fields = _parse_4field_trailer(msg)
        assert fields["task_id"] is None
        assert fields["stage"] is None
        assert fields["parent_sha"] is None
        assert fields["bug_id"] is None

    def test_partial_fields(self):
        from agent.governance.chain_trailer import _parse_4field_trailer
        msg = "msg\n\nChain-Source-Stage: qa\nChain-Bug-Id: X-1"
        fields = _parse_4field_trailer(msg)
        assert fields["stage"] == "qa"
        assert fields["bug_id"] == "X-1"
        assert fields["task_id"] is None
        assert fields["parent_sha"] is None


# ---------------------------------------------------------------------------
# get_chain_state tests (4-field schema)
# ---------------------------------------------------------------------------

class TestGetChainState:
    def test_returns_dict_with_required_keys(self, git_repo):
        from agent.governance.chain_trailer import get_chain_state
        state = get_chain_state(cwd=git_repo)
        assert isinstance(state, dict)
        assert "chain_sha" in state
        assert "task_id" in state
        assert "stage" in state
        assert "parent_sha" in state
        assert "dirty" in state
        assert "dirty_files" in state
        assert "source" in state

    def test_source_is_head_without_trailer(self, git_repo):
        from agent.governance.chain_trailer import get_chain_state
        state = get_chain_state(cwd=git_repo)
        assert state["source"] == "head"
        assert len(state["chain_sha"]) >= 7

    def test_source_is_trailer_with_4field_trailer(self, git_repo_with_4field_trailer):
        from agent.governance.chain_trailer import get_chain_state
        state = get_chain_state(cwd=git_repo_with_4field_trailer)
        assert state["source"] == "trailer"
        assert state["task_id"] == "task-abc123"
        assert state["stage"] == "merge"
        assert state["parent_sha"] == "def456"

    def test_source_is_trailer_with_legacy_trailer(self, git_repo_with_legacy_trailer):
        from agent.governance.chain_trailer import get_chain_state
        state = get_chain_state(cwd=git_repo_with_legacy_trailer)
        assert state["source"] == "trailer"

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
        claude_dir = os.path.join(git_repo, ".claude")
        os.makedirs(claude_dir, exist_ok=True)
        with open(os.path.join(claude_dir, "settings.json"), "w") as f:
            f.write("{}")
        state = get_chain_state(cwd=git_repo)
        assert state["dirty"] is False

    def test_chain_sha_is_string(self, git_repo):
        from agent.governance.chain_trailer import get_chain_state
        state = get_chain_state(cwd=git_repo)
        assert isinstance(state["chain_sha"], str)

    def test_version_compat_alias(self, git_repo):
        from agent.governance.chain_trailer import get_chain_state
        state = get_chain_state(cwd=git_repo)
        assert state["version"] == state["chain_sha"]

    def test_task_id_none_without_trailer(self, git_repo):
        from agent.governance.chain_trailer import get_chain_state
        state = get_chain_state(cwd=git_repo)
        assert state["task_id"] is None
        assert state["stage"] is None
        assert state["parent_sha"] is None


# ---------------------------------------------------------------------------
# get_chain_version tests
# ---------------------------------------------------------------------------

class TestGetChainVersion:
    def test_returns_string(self, git_repo):
        from agent.governance.chain_trailer import get_chain_version
        ver = get_chain_version(cwd=git_repo)
        assert isinstance(ver, str)
        assert len(ver) >= 7

    def test_returns_head_hash_without_trailer(self, git_repo):
        from agent.governance.chain_trailer import get_chain_version
        ver = get_chain_version(cwd=git_repo)
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=git_repo, capture_output=True, text=True,
        ).stdout.strip()
        assert ver == head


# ---------------------------------------------------------------------------
# validate_chain_lineage tests (returns dict with breaks[])
# ---------------------------------------------------------------------------

class TestValidateChainLineage:
    def test_returns_dict_with_breaks(self, git_repo):
        from agent.governance.chain_trailer import validate_chain_lineage
        first = subprocess.run(
            ["git", "rev-list", "--max-parents=0", "HEAD"],
            cwd=git_repo, capture_output=True, text=True,
        ).stdout.strip()
        with open(os.path.join(git_repo, "file2.txt"), "w") as f:
            f.write("content\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Second commit"], cwd=git_repo, capture_output=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True
        ).stdout.strip()

        result = validate_chain_lineage(first, head, cwd=git_repo)
        assert isinstance(result, dict)
        assert "valid" in result
        assert "breaks" in result
        assert isinstance(result["breaks"], list)

    def test_non_trailer_commits_appear_in_breaks(self, git_repo):
        from agent.governance.chain_trailer import validate_chain_lineage
        first = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True
        ).stdout.strip()
        # Add commit without trailer
        with open(os.path.join(git_repo, "f1.txt"), "w") as f:
            f.write("1\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "No trailer commit"], cwd=git_repo, capture_output=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True
        ).stdout.strip()

        result = validate_chain_lineage(first, head, cwd=git_repo)
        assert result["valid"] is False
        assert len(result["breaks"]) == 1
        assert result["commits"] == 1

    def test_trailer_commits_have_no_breaks(self, git_repo):
        from agent.governance.chain_trailer import validate_chain_lineage
        first = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True
        ).stdout.strip()
        # Add commit WITH trailer
        with open(os.path.join(git_repo, "f1.txt"), "w") as f:
            f.write("1\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        msg = "With trailer\n\nChain-Source-Task: t1\nChain-Source-Stage: merge\nChain-Parent: none\nChain-Bug-Id: none"
        subprocess.run(["git", "commit", "-m", msg], cwd=git_repo, capture_output=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True
        ).stdout.strip()

        result = validate_chain_lineage(first, head, cwd=git_repo)
        assert result["valid"] is True
        assert result["breaks"] == []

    def test_invalid_ref_returns_false(self, git_repo):
        from agent.governance.chain_trailer import validate_chain_lineage
        result = validate_chain_lineage("nonexistent123", "HEAD", cwd=git_repo)
        assert result["valid"] is False
        assert "Invalid ref" in result["reason"]

    def test_empty_range(self, git_repo):
        from agent.governance.chain_trailer import validate_chain_lineage
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True
        ).stdout.strip()
        result = validate_chain_lineage(head, head, cwd=git_repo)
        assert result["valid"] is False
        assert result["commits"] == 0

    def test_multi_commit_mixed_breaks(self, git_repo):
        from agent.governance.chain_trailer import validate_chain_lineage
        first = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True
        ).stdout.strip()
        # Commit 1: no trailer
        with open(os.path.join(git_repo, "a.txt"), "w") as f:
            f.write("a\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "no trailer 1"], cwd=git_repo, capture_output=True)
        # Commit 2: with trailer
        with open(os.path.join(git_repo, "b.txt"), "w") as f:
            f.write("b\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        msg2 = "with trailer\n\nChain-Source-Task: t1\nChain-Source-Stage: merge\nChain-Parent: x\nChain-Bug-Id: y"
        subprocess.run(["git", "commit", "-m", msg2], cwd=git_repo, capture_output=True)
        # Commit 3: no trailer
        with open(os.path.join(git_repo, "c.txt"), "w") as f:
            f.write("c\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "no trailer 2"], cwd=git_repo, capture_output=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True
        ).stdout.strip()

        result = validate_chain_lineage(first, head, cwd=git_repo)
        assert result["valid"] is False
        assert len(result["breaks"]) == 2  # commits 1 and 3
        assert result["commits"] == 3


# ---------------------------------------------------------------------------
# backfill_legacy_chain_history tests
# ---------------------------------------------------------------------------

class TestBackfillLegacyChainHistory:
    def _get_results(self, res):
        """Helper: extract backfill_results list from new dict return value."""
        if isinstance(res, dict):
            # Read from cache file or return empty
            return []  # results are in cache, use total_entries
        return res

    def test_returns_dict_with_keys(self, git_repo, tmp_path, monkeypatch):
        from agent.governance.chain_trailer import backfill_legacy_chain_history
        monkeypatch.setattr(
            "agent.governance.chain_trailer._chain_history_dir",
            lambda: str(tmp_path / "ch"),
        )
        result = backfill_legacy_chain_history(cwd=git_repo, incremental=False)
        assert isinstance(result, dict)
        for key in ("project_id", "new_entries", "total_entries",
                     "last_scanned_sha", "scanned_at", "scan_mode"):
            assert key in result

    def test_tags_commits_without_trailer(self, git_repo, tmp_path, monkeypatch):
        from agent.governance.chain_trailer import backfill_legacy_chain_history
        cache_dir = str(tmp_path / "ch")
        monkeypatch.setattr(
            "agent.governance.chain_trailer._chain_history_dir",
            lambda: cache_dir,
        )
        result = backfill_legacy_chain_history(cwd=git_repo, incremental=False)
        assert result["total_entries"] >= 1
        # Verify cache file contents
        with open(os.path.join(cache_dir, "aming-claw.json")) as f:
            data = json.load(f)
        for r in data["backfill_results"]:
            assert r["legacy_inferred"] is True
            assert r["needs_audit"] is True
            assert "audit_note" in r
            assert "commit" in r
            assert "short" in r

    def test_skips_commits_with_4field_trailer(self, git_repo_with_4field_trailer, tmp_path, monkeypatch):
        from agent.governance.chain_trailer import backfill_legacy_chain_history
        cache_dir = str(tmp_path / "ch")
        monkeypatch.setattr(
            "agent.governance.chain_trailer._chain_history_dir",
            lambda: cache_dir,
        )
        result = backfill_legacy_chain_history(cwd=git_repo_with_4field_trailer, incremental=False)
        # Only the initial commit (without trailer) should appear
        assert result["total_entries"] == 1

    def test_detects_legacy_trailer(self, git_repo_with_legacy_trailer, tmp_path, monkeypatch):
        from agent.governance.chain_trailer import backfill_legacy_chain_history
        cache_dir = str(tmp_path / "ch")
        monkeypatch.setattr(
            "agent.governance.chain_trailer._chain_history_dir",
            lambda: cache_dir,
        )
        result = backfill_legacy_chain_history(cwd=git_repo_with_legacy_trailer, incremental=False)
        with open(os.path.join(cache_dir, "aming-claw.json")) as f:
            data = json.load(f)
        # Legacy Chain-Version commit is NOT skipped - it lacks Chain-Source-Stage
        assert any(r["has_legacy_trailer"] for r in data["backfill_results"])

    def test_limit_parameter(self, git_repo, tmp_path, monkeypatch):
        from agent.governance.chain_trailer import backfill_legacy_chain_history
        monkeypatch.setattr(
            "agent.governance.chain_trailer._chain_history_dir",
            lambda: str(tmp_path / "ch"),
        )
        for i in range(5):
            with open(os.path.join(git_repo, f"f{i}.txt"), "w") as f:
                f.write(f"{i}\n")
            subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
            subprocess.run(["git", "commit", "-m", f"Commit {i}"], cwd=git_repo, capture_output=True)
        result = backfill_legacy_chain_history(limit=3, cwd=git_repo, incremental=False)
        assert result["total_entries"] <= 3

    def test_audit_note_contains_short_hash(self, git_repo, tmp_path, monkeypatch):
        from agent.governance.chain_trailer import backfill_legacy_chain_history
        cache_dir = str(tmp_path / "ch")
        monkeypatch.setattr(
            "agent.governance.chain_trailer._chain_history_dir",
            lambda: cache_dir,
        )
        backfill_legacy_chain_history(cwd=git_repo, incremental=False)
        with open(os.path.join(cache_dir, "aming-claw.json")) as f:
            data = json.load(f)
        for r in data["backfill_results"]:
            assert r["short"] in r["audit_note"]

    def test_writes_chain_history_json(self, git_repo, tmp_path, monkeypatch):
        from agent.governance.chain_trailer import backfill_legacy_chain_history
        cache_dir = str(tmp_path / "ch")
        monkeypatch.setattr(
            "agent.governance.chain_trailer._chain_history_dir",
            lambda: cache_dir,
        )
        out = str(tmp_path / "chain_history.json")
        backfill_legacy_chain_history(cwd=git_repo, output_path=out, incremental=False)
        assert os.path.exists(out)
        with open(out) as f:
            data = json.load(f)
        assert "backfill_results" in data

    def test_empty_repo_returns_zero_entries(self, tmp_path, monkeypatch):
        from agent.governance.chain_trailer import backfill_legacy_chain_history
        monkeypatch.setattr(
            "agent.governance.chain_trailer._chain_history_dir",
            lambda: str(tmp_path / "ch"),
        )
        repo = str(tmp_path / "empty-repo")
        os.makedirs(repo)
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        result = backfill_legacy_chain_history(cwd=repo, incremental=False)
        assert isinstance(result, dict)
        assert result["total_entries"] == 0


# ---------------------------------------------------------------------------
# write_merge_with_trailer tests (4-field)
# ---------------------------------------------------------------------------

class TestWriteMergeWithTrailer:
    def test_commit_contains_4field_trailers(self, git_repo):
        from agent.governance.chain_trailer import write_merge_with_trailer
        with open(os.path.join(git_repo, "new.txt"), "w") as f:
            f.write("new\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)

        success, commit_hash, err = write_merge_with_trailer(
            message="Test commit", cwd=git_repo,
            task_id="task-test-1", parent_chain_sha="parent123", bug_id="BUG-X")
        assert success is True
        assert commit_hash
        assert err == ""

        msg = subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            cwd=git_repo, capture_output=True, text=True,
        ).stdout
        assert "Chain-Source-Task: task-test-1" in msg
        assert "Chain-Source-Stage: merge" in msg
        assert "Chain-Parent: parent123" in msg
        assert "Chain-Bug-Id: BUG-X" in msg

    def test_merge_branch_with_trailer(self, git_repo_with_branch):
        from agent.governance.chain_trailer import write_merge_with_trailer
        success, commit_hash, err = write_merge_with_trailer(
            message="Merge feature", branch="feature-test",
            cwd=git_repo_with_branch,
            task_id="task-merge-1", parent_chain_sha="p1", bug_id="B1")
        assert success is True
        assert commit_hash
        assert err == ""

        msg = subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            cwd=git_repo_with_branch, capture_output=True, text=True,
        ).stdout
        assert "Chain-Source-Stage:" in msg

    def test_merge_nonexistent_branch_fails(self, git_repo):
        from agent.governance.chain_trailer import write_merge_with_trailer
        success, commit_hash, err = write_merge_with_trailer(
            message="Bad merge", branch="nonexistent-branch", cwd=git_repo)
        assert success is False
        assert err

    def test_returns_short_hash(self, git_repo):
        from agent.governance.chain_trailer import write_merge_with_trailer
        with open(os.path.join(git_repo, "hash_test.txt"), "w") as f:
            f.write("test\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        success, commit_hash, err = write_merge_with_trailer(
            message="Hash test", cwd=git_repo, task_id="t1")
        assert success
        assert 7 <= len(commit_hash) <= 12

    def test_defaults_unknown_task_id(self, git_repo):
        from agent.governance.chain_trailer import write_merge_with_trailer
        with open(os.path.join(git_repo, "default.txt"), "w") as f:
            f.write("x\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        success, _, _ = write_merge_with_trailer(message="No task", cwd=git_repo)
        assert success
        msg = subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            cwd=git_repo, capture_output=True, text=True,
        ).stdout
        assert "Chain-Source-Task: unknown" in msg

    def test_defaults_none_parent_and_bug(self, git_repo):
        from agent.governance.chain_trailer import write_merge_with_trailer
        with open(os.path.join(git_repo, "none.txt"), "w") as f:
            f.write("y\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        success, _, _ = write_merge_with_trailer(message="No parent/bug", cwd=git_repo)
        assert success
        msg = subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            cwd=git_repo, capture_output=True, text=True,
        ).stdout
        assert "Chain-Parent: none" in msg
        assert "Chain-Bug-Id: none" in msg


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_state_reflects_4field_trailer_after_merge(self, git_repo):
        from agent.governance.chain_trailer import write_merge_with_trailer, get_chain_state
        with open(os.path.join(git_repo, "int.txt"), "w") as f:
            f.write("integration\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        success, commit_hash, _ = write_merge_with_trailer(
            message="Integration test", cwd=git_repo,
            task_id="task-int-1", parent_chain_sha="pint", bug_id="BINT")
        assert success

        state = get_chain_state(cwd=git_repo)
        assert state["source"] == "trailer"
        assert state["task_id"] == "task-int-1"
        assert state["stage"] == "merge"
        assert state["parent_sha"] == "pint"

    def test_backfill_excludes_4field_trailer_commits(self, git_repo, tmp_path, monkeypatch):
        from agent.governance.chain_trailer import (
            write_merge_with_trailer, backfill_legacy_chain_history
        )
        cache_dir = str(tmp_path / "ch")
        monkeypatch.setattr(
            "agent.governance.chain_trailer._chain_history_dir",
            lambda: cache_dir,
        )
        with open(os.path.join(git_repo, "bf.txt"), "w") as f:
            f.write("backfill test\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        write_merge_with_trailer(message="With trailer", cwd=git_repo, task_id="t1")

        backfill_legacy_chain_history(cwd=git_repo, incremental=False)
        with open(os.path.join(cache_dir, "aming-claw.json")) as f:
            data = json.load(f)
        hashes = [r["commit"] for r in data["backfill_results"]]
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_repo, capture_output=True, text=True,
        ).stdout.strip()
        assert head not in hashes  # HEAD has trailer, should be excluded

    def test_rollback_via_git_reset(self, git_repo):
        """After git reset, get_chain_state should track the reset HEAD."""
        from agent.governance.chain_trailer import write_merge_with_trailer, get_chain_state
        # Create chain commit 1
        with open(os.path.join(git_repo, "c1.txt"), "w") as f:
            f.write("c1\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        write_merge_with_trailer(message="Chain 1", cwd=git_repo, task_id="t-c1")
        c1_state = get_chain_state(cwd=git_repo)

        # Create chain commit 2
        with open(os.path.join(git_repo, "c2.txt"), "w") as f:
            f.write("c2\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        write_merge_with_trailer(message="Chain 2", cwd=git_repo, task_id="t-c2")

        # Reset back to c1
        subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=git_repo, capture_output=True)
        state_after_reset = get_chain_state(cwd=git_repo)
        assert state_after_reset["task_id"] == "t-c1"

    def test_first_parent_walk_finds_trailer_past_non_trailer_commits(self, git_repo):
        """get_chain_state walks first-parent to find trailer even past non-trailer commits."""
        from agent.governance.chain_trailer import write_merge_with_trailer, get_chain_state
        # Create trailer commit
        with open(os.path.join(git_repo, "t1.txt"), "w") as f:
            f.write("t1\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        write_merge_with_trailer(message="Trailer 1", cwd=git_repo, task_id="t-walk")

        # Create non-trailer commit on top
        with open(os.path.join(git_repo, "nt.txt"), "w") as f:
            f.write("no trailer\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "No trailer here"], cwd=git_repo, capture_output=True)

        state = get_chain_state(cwd=git_repo)
        assert state["source"] == "trailer"
        assert state["task_id"] == "t-walk"
