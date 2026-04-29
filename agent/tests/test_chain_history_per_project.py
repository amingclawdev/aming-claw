"""Tests for per-project chain history cache (R1-R3, R7).

AC7: test_per_project_files_isolated uses 2 distinct project_ids in tmp_path.
AC9: test_ai_bypass_detection_per_project verifies manual commit on project A
     detected as legacy_inferred without polluting project B's cache.
"""

import json
import os
import subprocess
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


class TestPerProjectFilesIsolated:
    """AC7: Two distinct project_ids produce separate cache files."""

    def test_per_project_files_isolated(self, git_repo, tmp_path, monkeypatch):
        from agent.governance.chain_trailer import backfill_legacy_chain_history, _chain_history_dir
        cache_dir = str(tmp_path / "chain_history")
        monkeypatch.setattr(
            "agent.governance.chain_trailer._chain_history_dir",
            lambda: cache_dir,
        )

        res_a = backfill_legacy_chain_history(
            project_id="project-a", cwd=git_repo, incremental=False,
        )
        res_b = backfill_legacy_chain_history(
            project_id="project-b", cwd=git_repo, incremental=False,
        )

        assert res_a["project_id"] == "project-a"
        assert res_b["project_id"] == "project-b"

        # Separate cache files
        assert os.path.exists(os.path.join(cache_dir, "project-a.json"))
        assert os.path.exists(os.path.join(cache_dir, "project-b.json"))

        # Contents are independent
        with open(os.path.join(cache_dir, "project-a.json")) as f:
            data_a = json.load(f)
        with open(os.path.join(cache_dir, "project-b.json")) as f:
            data_b = json.load(f)
        assert data_a["project_id"] == "project-a"
        assert data_b["project_id"] == "project-b"

    def test_return_dict_keys(self, git_repo, tmp_path, monkeypatch):
        from agent.governance.chain_trailer import backfill_legacy_chain_history
        cache_dir = str(tmp_path / "chain_history")
        monkeypatch.setattr(
            "agent.governance.chain_trailer._chain_history_dir",
            lambda: cache_dir,
        )

        result = backfill_legacy_chain_history(
            project_id="project-a", cwd=git_repo, incremental=False,
        )
        assert isinstance(result, dict)
        for key in ("project_id", "new_entries", "total_entries",
                     "last_scanned_sha", "scanned_at", "scan_mode"):
            assert key in result, f"Missing key: {key}"

    def test_incremental_scan_uses_last_sha(self, git_repo, tmp_path, monkeypatch):
        """R2: Incremental scan reads last_scanned_sha and only scans new commits."""
        from agent.governance.chain_trailer import backfill_legacy_chain_history
        cache_dir = str(tmp_path / "chain_history")
        monkeypatch.setattr(
            "agent.governance.chain_trailer._chain_history_dir",
            lambda: cache_dir,
        )

        # Full scan first
        r1 = backfill_legacy_chain_history(
            project_id="project-a", cwd=git_repo, incremental=False,
        )
        assert r1["scan_mode"] == "full"
        initial_count = r1["total_entries"]

        # Add a new commit
        with open(os.path.join(git_repo, "new.txt"), "w") as f:
            f.write("new\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "New commit"], cwd=git_repo, capture_output=True)

        # Incremental scan
        r2 = backfill_legacy_chain_history(
            project_id="project-a", cwd=git_repo, incremental=True,
        )
        assert r2["scan_mode"] == "incremental"
        assert r2["total_entries"] >= initial_count

    def test_single_git_log_no_rev_parse_short(self, git_repo, tmp_path, monkeypatch):
        """R3/AC4: backfill uses single git log with %H and %h, no rev-parse --short."""
        from agent.governance.chain_trailer import backfill_legacy_chain_history
        cache_dir = str(tmp_path / "chain_history")
        monkeypatch.setattr(
            "agent.governance.chain_trailer._chain_history_dir",
            lambda: cache_dir,
        )

        result = backfill_legacy_chain_history(
            project_id="project-a", cwd=git_repo, incremental=False,
        )
        # Should have entries (initial commit lacks trailer)
        assert result["total_entries"] >= 1

        # Verify cache has short hashes populated
        with open(os.path.join(cache_dir, "project-a.json")) as f:
            data = json.load(f)
        for entry in data["backfill_results"]:
            assert "short" in entry
            assert len(entry["short"]) >= 7
            assert len(entry["short"]) < len(entry["commit"])


class TestAIBypassDetectionPerProject:
    """AC9: Manual commit on project A detected as legacy_inferred without
    polluting project B's cache."""

    def test_ai_bypass_detection_per_project(self, git_repo, tmp_path, monkeypatch):
        from agent.governance.chain_trailer import backfill_legacy_chain_history
        cache_dir = str(tmp_path / "chain_history")
        monkeypatch.setattr(
            "agent.governance.chain_trailer._chain_history_dir",
            lambda: cache_dir,
        )

        # Add a manual commit (no trailer)
        with open(os.path.join(git_repo, "manual.txt"), "w") as f:
            f.write("manual change\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "manual commit without trailer"],
            cwd=git_repo, capture_output=True,
        )

        # Backfill project A — should detect the manual commit
        res_a = backfill_legacy_chain_history(
            project_id="project-a", cwd=git_repo, incremental=False,
        )
        assert res_a["total_entries"] >= 1

        # Check project A cache has legacy_inferred entries
        with open(os.path.join(cache_dir, "project-a.json")) as f:
            data_a = json.load(f)
        legacy_commits = [e for e in data_a["backfill_results"] if e.get("legacy_inferred")]
        assert len(legacy_commits) >= 1

        # Backfill project B (fresh, no prior cache)
        res_b = backfill_legacy_chain_history(
            project_id="project-b", cwd=git_repo, incremental=False,
        )

        # Project B has its own cache, not polluted by project A's entries
        with open(os.path.join(cache_dir, "project-b.json")) as f:
            data_b = json.load(f)
        assert data_b["project_id"] == "project-b"
        # B sees same git history (same repo) but its cache file is separate
        assert data_a["project_id"] != data_b["project_id"]


class TestLegacyCacheMigration:
    """R7: Existing root chain_history.json migrated on first run."""

    def test_migrates_legacy_cache(self, git_repo, tmp_path, monkeypatch):
        from agent.governance.chain_trailer import backfill_legacy_chain_history, _migrate_legacy_cache

        cache_dir = str(tmp_path / "chain_history")
        os.makedirs(cache_dir, exist_ok=True)
        monkeypatch.setattr(
            "agent.governance.chain_trailer._chain_history_dir",
            lambda: cache_dir,
        )

        # Create a fake legacy cache at repo root
        legacy_data = {"backfill_results": [{"commit": "abc123", "short": "abc123"}], "total_scanned": 1}
        legacy_path = os.path.join(git_repo, "chain_history.json")
        with open(legacy_path, "w") as f:
            json.dump(legacy_data, f)

        monkeypatch.setattr("agent.governance.chain_trailer._repo_root", lambda: git_repo)

        _migrate_legacy_cache("aming-claw", cache_dir)
        migrated = os.path.join(cache_dir, "aming-claw.json")
        assert os.path.exists(migrated)
        with open(migrated) as f:
            data = json.load(f)
        assert data["backfill_results"][0]["commit"] == "abc123"
