"""Tests for B8: _is_dev_note() check in _gate_checkpoint unrelated-file loop."""

import unittest
from unittest.mock import Mock


class TestGateCheckpointDevNoteExemption(unittest.TestCase):
    """docs/dev/ paths should be exempt from unrelated-file blocking."""

    def _call_gate(self, changed_files, target_files):
        from governance.auto_chain import _gate_checkpoint

        conn = Mock()
        conn.execute.return_value.fetchone.return_value = None
        result = {
            "changed_files": changed_files,
            "test_results": {"ran": True, "passed": 1, "failed": 0},
        }
        metadata = {
            "target_files": target_files,
            "doc_impact": {"files": [], "changes": []},
            "skip_doc_check": True,
        }
        return _gate_checkpoint(conn, "test-proj", result, metadata)

    def test_dev_note_not_flagged_as_unrelated(self):
        """AC1: docs/dev/ paths pass through unrelated-file check."""
        ok, reason = self._call_gate(
            changed_files=["agent/governance/auto_chain.py", "docs/dev/archive/foo.md"],
            target_files=["agent/governance/auto_chain.py"],
        )
        self.assertTrue(ok, f"Expected pass but got: {reason}")

    def test_docs_api_still_blocked_as_unrelated(self):
        """AC2: docs/api/ paths still blocked as unrelated."""
        ok, reason = self._call_gate(
            changed_files=["agent/governance/auto_chain.py", "docs/api/unrelated.md"],
            target_files=["agent/governance/auto_chain.py"],
        )
        self.assertFalse(ok)
        self.assertIn("Unrelated files", reason)

    def test_dev_note_nested_path(self):
        """docs/dev/roadmap-2026-03-31.md should also be exempt."""
        ok, reason = self._call_gate(
            changed_files=["agent/governance/auto_chain.py", "docs/dev/roadmap-2026-03-31.md"],
            target_files=["agent/governance/auto_chain.py"],
        )
        self.assertTrue(ok, f"Expected pass but got: {reason}")


if __name__ == "__main__":
    unittest.main()
