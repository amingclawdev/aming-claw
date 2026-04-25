"""Tests for Phase C — completeness check for merges and observer-hotfixes."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest

from agent.governance.reconcile_phases import phase_c


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _make_ctx(project_id: str = "aming-claw") -> SimpleNamespace:
    return SimpleNamespace(project_id=project_id)


HOTFIX_COMMITS = [
    {
        "sha": "bf564b5",
        "subject": "[observer-hotfix] fix sidecar import crash",
        "parents": ["aaa0001"],
    },
    {
        "sha": "cac32c3",
        "subject": "[observer-hotfix] log visibility patch",
        "parents": ["aaa0002"],
    },
]

MERGE_COMMITS = [
    {
        "sha": "abc1234",
        "subject": "Auto-merge: task-1777097097-3cf388 PR4",
        "parents": ["p1"],
    },
    {
        "sha": "def5678",
        "subject": "Merge branch 'dev' into main",
        "parents": ["p1", "p2"],
    },
]


# ---------------------------------------------------------------------------
# AC4.1: hotfix_no_mf_record detection
# ---------------------------------------------------------------------------

class TestHotfixDetection:
    """AC4.1: bf564b5 + cac32c3 detected as hotfix_no_mf_record."""

    def test_hotfix_commits_detected(self):
        ctx = _make_ctx()
        results = phase_c.run(ctx, git_log=HOTFIX_COMMITS, backlog_bugs=[], mf_files=[])

        shas = {r.detail.split()[0].split("=")[1] for r in results}
        assert "bf564b5" in shas
        assert "cac32c3" in shas
        assert all(r.type == "hotfix_no_mf_record" for r in results)

    def test_hotfix_with_mf_record_not_flagged(self):
        mf = [{"path": "docs/dev/mf-001.md", "content": "Fixed bf564b5 sidecar crash"}]
        ctx = _make_ctx()
        results = phase_c.run(ctx, git_log=HOTFIX_COMMITS, backlog_bugs=[], mf_files=mf)

        shas = {r.detail.split()[0].split("=")[1] for r in results}
        # bf564b5 is covered by mf file, should NOT appear
        assert "bf564b5" not in shas
        # cac32c3 still missing
        assert "cac32c3" in shas


# ---------------------------------------------------------------------------
# AC4.2: merge_not_tracked auto-upsert
# ---------------------------------------------------------------------------

class TestMergeNotTracked:
    """AC4.2: auto-upsert backlog for merge_not_tracked (dry_run=False)."""

    def test_merge_commits_flagged(self):
        ctx = _make_ctx()
        results = phase_c.run(ctx, git_log=MERGE_COMMITS, backlog_bugs=[], mf_files=[])

        assert len(results) == 2
        assert all(r.type == "merge_not_tracked" for r in results)

    def test_merge_tracked_by_backlog_not_flagged(self):
        bugs = [{"details_md": "task-1777097097-3cf388 merged", "commit": ""}]
        ctx = _make_ctx()
        results = phase_c.run(ctx, git_log=MERGE_COMMITS, backlog_bugs=bugs, mf_files=[])

        # First merge has task id in backlog, second doesn't
        assert len(results) == 1
        assert "def5678" in results[0].detail

    def test_apply_mutations_calls_post(self):
        ctx = _make_ctx()
        discs = phase_c.run(ctx, git_log=MERGE_COMMITS, backlog_bugs=[], mf_files=[])

        mock_post = MagicMock()
        mock_post.return_value = MagicMock(status_code=200)

        mutations = phase_c.apply_phase_c_mutations(
            ctx, discs, threshold="medium", dry_run=False, _post_fn=mock_post,
        )

        assert mock_post.call_count == 2
        assert all(m["status"] == "applied" for m in mutations)

        # Verify URLs contain backlog endpoint
        for call in mock_post.call_args_list:
            url = call[0][0]
            assert "/api/backlog/aming-claw/" in url

    def test_dry_run_no_post(self):
        ctx = _make_ctx()
        discs = phase_c.run(ctx, git_log=MERGE_COMMITS, backlog_bugs=[], mf_files=[])

        mock_post = MagicMock()
        mutations = phase_c.apply_phase_c_mutations(
            ctx, discs, threshold="medium", dry_run=True, _post_fn=mock_post,
        )

        assert mock_post.call_count == 0
        assert all(m["status"] == "dry_run" for m in mutations)


# ---------------------------------------------------------------------------
# AC4.3: hotfix entries NEVER auto-fixed
# ---------------------------------------------------------------------------

class TestHotfixNeverAutoFixed:
    """AC4.3: hotfix_no_mf_record NEVER triggers POST, even with dry_run=False."""

    def test_hotfix_not_mutated(self):
        ctx = _make_ctx()
        discs = phase_c.run(ctx, git_log=HOTFIX_COMMITS, backlog_bugs=[], mf_files=[])

        assert len(discs) == 2  # both flagged

        mock_post = MagicMock()
        mutations = phase_c.apply_phase_c_mutations(
            ctx, discs, threshold="low", dry_run=False, _post_fn=mock_post,
        )

        # NEVER called for hotfix
        assert mock_post.call_count == 0
        assert len(mutations) == 0

    def test_mixed_commits_only_merge_mutated(self):
        ctx = _make_ctx()
        all_commits = HOTFIX_COMMITS + MERGE_COMMITS
        discs = phase_c.run(ctx, git_log=all_commits, backlog_bugs=[], mf_files=[])

        hotfix_count = sum(1 for d in discs if d.type == "hotfix_no_mf_record")
        merge_count = sum(1 for d in discs if d.type == "merge_not_tracked")
        assert hotfix_count == 2
        assert merge_count == 2

        mock_post = MagicMock()
        mock_post.return_value = MagicMock(status_code=200)
        mutations = phase_c.apply_phase_c_mutations(
            ctx, discs, threshold="low", dry_run=False, _post_fn=mock_post,
        )

        # Only merge entries get POST calls
        assert mock_post.call_count == 2
        assert all(m["status"] == "applied" for m in mutations)


# ---------------------------------------------------------------------------
# helper unit tests
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_extract_task_id(self):
        assert phase_c.extract_task_id("Auto-merge: task-1777097097-3cf388 PR4") == "task-1777097097-3cf388"
        assert phase_c.extract_task_id("fix something") is None

    def test_is_merge_by_parents(self):
        assert phase_c._is_merge({"parents": ["a", "b"], "subject": "x"})
        assert not phase_c._is_merge({"parents": ["a"], "subject": "x"})

    def test_is_merge_by_auto_merge_prefix(self):
        assert phase_c._is_merge({"parents": ["a"], "subject": "Auto-merge: foo"})

    def test_backlog_lookup(self):
        bugs = [{"details_md": "task-123-abc done", "commit": ""}]
        assert phase_c.backlog_lookup_by_task_id(bugs, "task-123-abc")
        assert not phase_c.backlog_lookup_by_task_id(bugs, "task-999-xyz")
