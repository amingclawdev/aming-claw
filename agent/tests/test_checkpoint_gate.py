"""Tests for checkpoint gate: B8 dev-note exemption, G4 auto-populate, G6 bidirectional lookup."""

import unittest
from unittest.mock import Mock, patch


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


class TestG6BidirectionalGraphLookup(unittest.TestCase):
    """G6: _get_graph_doc_associations finds code files from doc targets."""

    def _make_mock_graph(self, nodes):
        """Create a mock graph with given nodes dict {id: {primary, secondary}}."""
        mock_graph = Mock()
        mock_graph.list_nodes.return_value = list(nodes.keys())
        mock_graph.get_node.side_effect = lambda nid: nodes[nid]
        return mock_graph

    @patch("governance.graph.AcceptanceGraph")
    @patch("os.path.exists", return_value=True)
    def test_forward_lookup_code_to_docs(self, mock_exists, MockGraph):
        """Forward: code target → find related docs."""
        from governance.auto_chain import _get_graph_doc_associations

        nodes = {
            "node1": {
                "primary": ["agent/governance/auto_chain.py"],
                "secondary": ["docs/governance/gates.md", "docs/api/auto-chain.md"],
            },
        }
        MockGraph.return_value = self._make_mock_graph(nodes)
        result = _get_graph_doc_associations("test-proj", ["agent/governance/auto_chain.py"])
        self.assertIn("docs/governance/gates.md", result)
        self.assertIn("docs/api/auto-chain.md", result)

    @patch("governance.graph.AcceptanceGraph")
    @patch("os.path.exists", return_value=True)
    def test_reverse_lookup_doc_to_code(self, mock_exists, MockGraph):
        """G6: doc target → find related code files."""
        from governance.auto_chain import _get_graph_doc_associations

        nodes = {
            "node1": {
                "primary": ["agent/governance/auto_chain.py"],
                "secondary": ["docs/governance/gates.md"],
            },
        }
        MockGraph.return_value = self._make_mock_graph(nodes)
        result = _get_graph_doc_associations("test-proj", ["docs/governance/gates.md"])
        self.assertIn("agent/governance/auto_chain.py", result)

    @patch("governance.graph.AcceptanceGraph")
    @patch("os.path.exists", return_value=True)
    def test_no_match_returns_empty(self, mock_exists, MockGraph):
        """No matches returns empty list."""
        from governance.auto_chain import _get_graph_doc_associations

        nodes = {
            "node1": {
                "primary": ["agent/other.py"],
                "secondary": ["docs/other.md"],
            },
        }
        MockGraph.return_value = self._make_mock_graph(nodes)
        result = _get_graph_doc_associations("test-proj", ["agent/unrelated.py"])
        self.assertEqual(result, [])


class TestG4AutoPopulateDocImpact(unittest.TestCase):
    """G4: _gate_post_pm auto-populates doc_impact from graph when PM leaves it empty."""

    @patch("governance.auto_chain._get_graph_doc_associations",
           return_value=["docs/governance/gates.md"])
    def test_empty_doc_impact_gets_auto_filled(self, mock_graph):
        from governance.auto_chain import _gate_post_pm

        result = {
            "target_files": ["agent/governance/auto_chain.py"],
            "verification": {"command": "pytest -q"},
            "acceptance_criteria": ["AC1"],
            "test_files": ["agent/tests/test_foo.py"],
            "proposed_nodes": [],
            "doc_impact": {},
        }
        metadata = {}
        passed, reason = _gate_post_pm(None, "test-proj", result, metadata)
        # doc_impact should now be auto-populated
        self.assertEqual(result["doc_impact"]["files"], ["docs/governance/gates.md"])
        self.assertIn("Auto-populated", result["doc_impact"]["changes"][0])

    @patch("governance.auto_chain._get_graph_doc_associations",
           return_value=[])
    def test_empty_doc_impact_no_graph_still_needs_skip_reason(self, mock_graph):
        from governance.auto_chain import _gate_post_pm

        result = {
            "target_files": ["agent/governance/auto_chain.py"],
            "verification": {"command": "pytest -q"},
            "acceptance_criteria": ["AC1"],
            "test_files": ["agent/tests/test_foo.py"],
            "proposed_nodes": [],
        }
        metadata = {}
        passed, reason = _gate_post_pm(None, "test-proj", result, metadata)
        self.assertFalse(passed)
        self.assertIn("doc_impact", reason)


if __name__ == "__main__":
    unittest.main()
