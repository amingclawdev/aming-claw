"""Tests for _backlog_compact_bug privacy_level / public_safe fields.

AC-PLAYBACK-ROW-PRIVACY-FLAG-NOT-REGEX-20260608:
  - _backlog_compact_bug must emit privacy_level and public_safe.
  - Existing rows with no privacy marking must default to public / True.
  - An explicit privacy_level=private marking must be respected.
"""

import json
import sys
import os

# Ensure repo root is on the path when running from the worktree.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent.governance.server import _backlog_compact_bug  # noqa: E402


def _make_row(
    bug_id: str = "AC-TEST-20260101",
    title: str = "Test row",
    bypass_policy_json: str = "{}",
    privacy_level: str | None = None,
) -> dict:
    """Construct a minimal backlog row dict for testing."""
    row: dict = {
        "bug_id": bug_id,
        "title": title,
        "status": "OPEN",
        "priority": "P2",
        "bypass_policy_json": bypass_policy_json,
        "target_files": "[]",
        "test_files": "[]",
        "acceptance_criteria": "[]",
        "details_md": "",
        "commit": "",
        "created_at": "",
        "updated_at": "",
        "fixed_at": "",
        "chain_task_id": "",
        "chain_stage": "",
        "runtime_state": "",
        "current_task_id": "",
        "root_task_id": "",
        "worktree_path": "",
        "worktree_branch": "",
        "mf_type": "",
        "required_docs": "[]",
        "provenance_paths": "[]",
        "chain_trigger_json": "{}",
    }
    if privacy_level is not None:
        row["privacy_level"] = privacy_level
    return row


class TestBacklogCompactBugDefaults:
    """Existing rows with no privacy marking must default to public."""

    def test_emits_privacy_level_field(self):
        bug = _backlog_compact_bug(_make_row())
        assert "privacy_level" in bug, "compact bug must include privacy_level"

    def test_emits_public_safe_field(self):
        bug = _backlog_compact_bug(_make_row())
        assert "public_safe" in bug, "compact bug must include public_safe"

    def test_default_privacy_level_is_public(self):
        bug = _backlog_compact_bug(_make_row())
        assert bug["privacy_level"] == "public", (
            f"expected privacy_level='public' for existing row, got {bug['privacy_level']!r}"
        )

    def test_default_public_safe_is_true(self):
        bug = _backlog_compact_bug(_make_row())
        assert bug["public_safe"] is True, (
            f"expected public_safe=True for existing row, got {bug['public_safe']!r}"
        )

    def test_row_with_external_provider_title_is_public(self):
        """A row whose title mentions an external provider is public by default."""
        row = _make_row(title="Remove openai dependency from inference layer")
        bug = _backlog_compact_bug(row)
        assert bug["privacy_level"] == "public"
        assert bug["public_safe"] is True


class TestBacklogCompactBugExplicitPrivateFlag:
    """Explicit privacy marking must be respected."""

    def test_explicit_top_level_privacy_level_private(self):
        row = _make_row(privacy_level="private")
        bug = _backlog_compact_bug(row)
        assert bug["privacy_level"] == "private"
        assert bug["public_safe"] is False

    def test_bypass_policy_privacy_level_private(self):
        policy = json.dumps({"privacy_level": "private"})
        row = _make_row(bypass_policy_json=policy)
        bug = _backlog_compact_bug(row)
        assert bug["privacy_level"] == "private"
        assert bug["public_safe"] is False

    def test_bypass_policy_public_safe_false(self):
        policy = json.dumps({"public_safe": False})
        row = _make_row(bypass_policy_json=policy)
        bug = _backlog_compact_bug(row)
        assert bug["public_safe"] is False
        assert bug["privacy_level"] == "private"

    def test_bypass_policy_public_safe_true_is_public(self):
        policy = json.dumps({"public_safe": True})
        row = _make_row(bypass_policy_json=policy)
        bug = _backlog_compact_bug(row)
        assert bug["public_safe"] is True
        assert bug["privacy_level"] == "public"

    def test_top_level_field_wins_over_bypass_policy(self):
        """Explicit top-level privacy_level takes precedence over bypass_policy."""
        policy = json.dumps({"privacy_level": "public", "public_safe": True})
        row = _make_row(bypass_policy_json=policy, privacy_level="private")
        bug = _backlog_compact_bug(row)
        assert bug["privacy_level"] == "private"
        assert bug["public_safe"] is False

    def test_explicit_public_is_not_private(self):
        row = _make_row(privacy_level="public")
        bug = _backlog_compact_bug(row)
        assert bug["privacy_level"] == "public"
        assert bug["public_safe"] is True


class TestBacklogCompactBugPrivateBodyText:
    """Private body text handling is separate from row visibility.

    Row visibility is driven solely by privacy_level / public_safe.
    A row whose title contains keywords that the old regex would match
    must still default to public when no explicit flag is set.
    """

    def test_private_keyword_in_title_does_not_make_row_private(self):
        row = _make_row(title="Fix raw_prompt serialization in event log")
        bug = _backlog_compact_bug(row)
        assert bug["privacy_level"] == "public"
        assert bug["public_safe"] is True

    def test_jb_prefixed_id_does_not_make_row_private(self):
        row = _make_row(bug_id="JB-JUDGE-ROUTING-20260101", title="Judge routing fix")
        bug = _backlog_compact_bug(row)
        assert bug["privacy_level"] == "public"
        assert bug["public_safe"] is True
