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


class TestB36ScanDependentTests(unittest.TestCase):
    """B36-fix(4): _scan_dependent_tests finds tests that import any target file.

    Protects against PM under-specification — when target code is edited but PM
    omits dependent test files from metadata.test_files, gate should still allow
    those tests so Dev isn't ping-ponged.
    """

    def test_direct_importer_discovered(self):
        """Tests that `from agent.role_permissions import ...` should be included."""
        from governance.auto_chain import _scan_dependent_tests, _DEPENDENT_TESTS_CACHE
        _DEPENDENT_TESTS_CACHE.clear()
        deps = _scan_dependent_tests(["agent/role_permissions.py"])
        # At least one real importer exists in the codebase
        self.assertTrue(len(deps) > 0, "Expected at least one dependent test")
        self.assertIn("agent/tests/test_role_config.py", deps)

    def test_worktree_mirrors_excluded(self):
        """Scan must not pick up .worktrees/** or .claude/** mirrors."""
        from governance.auto_chain import _scan_dependent_tests, _DEPENDENT_TESTS_CACHE
        _DEPENDENT_TESTS_CACHE.clear()
        deps = _scan_dependent_tests(["agent/role_permissions.py"])
        for d in deps:
            self.assertFalse(d.startswith(".worktrees/"), f"worktree path leaked: {d}")
            self.assertFalse(d.startswith(".claude/"), f".claude path leaked: {d}")

    def test_empty_target_returns_empty(self):
        from governance.auto_chain import _scan_dependent_tests, _DEPENDENT_TESTS_CACHE
        _DEPENDENT_TESTS_CACHE.clear()
        self.assertEqual(_scan_dependent_tests([]), set())

    def test_non_py_target_ignored(self):
        from governance.auto_chain import _scan_dependent_tests, _DEPENDENT_TESTS_CACHE
        _DEPENDENT_TESTS_CACHE.clear()
        self.assertEqual(_scan_dependent_tests(["docs/foo.md"]), set())

    def test_init_stem_ignored(self):
        """__init__.py as target must not match every test that imports its package."""
        from governance.auto_chain import _scan_dependent_tests, _DEPENDENT_TESTS_CACHE
        _DEPENDENT_TESTS_CACHE.clear()
        self.assertEqual(_scan_dependent_tests(["agent/__init__.py"]), set())


class TestB36ComputeGateStaticAllowed(unittest.TestCase):
    """B36-fix(2): single source of truth shared by gate and retry-prompt scope_line."""

    def test_allowed_includes_target_test_doc_impact(self):
        from governance.auto_chain import _compute_gate_static_allowed, _DEPENDENT_TESTS_CACHE
        _DEPENDENT_TESTS_CACHE.clear()
        metadata = {
            "target_files": ["agent/foo.py"],
            "test_files": ["agent/tests/test_foo.py"],
            "doc_impact": {"files": ["docs/api/foo.md"]},
            "verification": {"command": "pytest agent/tests/test_foo_extra.py -q"},
        }
        target, allowed = _compute_gate_static_allowed("test-proj", metadata)
        self.assertEqual(target, {"agent/foo.py"})
        self.assertIn("agent/foo.py", allowed)
        self.assertIn("agent/tests/test_foo.py", allowed)
        self.assertIn("docs/api/foo.md", allowed)
        self.assertIn("agent/tests/test_foo_extra.py", allowed)

    def test_dependent_tests_folded_in(self):
        from governance.auto_chain import _compute_gate_static_allowed, _DEPENDENT_TESTS_CACHE
        _DEPENDENT_TESTS_CACHE.clear()
        metadata = {"target_files": ["agent/role_permissions.py"]}
        _, allowed = _compute_gate_static_allowed("test-proj", metadata)
        # Real codebase has importers of role_permissions
        self.assertIn("agent/tests/test_role_config.py", allowed)


if __name__ == "__main__":
    unittest.main()
