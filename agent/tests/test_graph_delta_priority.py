"""Tests for graph delta priority — PM declarations override auto-inferrer (R3/R4).

Covers:
  1. declaration-overrides-inferrer — removed_nodes suppresses creates
  2. inferrer-for-undeclared — undeclared files still go through auto-inference
  3. conflict-audit — override emits structured_log event
  4. qa-gate-validation — _gate_qa_pass calls validate_prd_graph_declarations
  5. combined-delta — declarations + inference produce merged result
  6. remapped-files-skip-inferrer — remapped files excluded from Rule B
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.auto_chain import (
    _infer_graph_delta,
    validate_prd_graph_declarations,
)


class TestDeclarationOverridesInferrer(unittest.TestCase):
    """PM-declared removed_nodes suppress auto-inferrer creates for those nodes."""

    def test_removed_node_not_in_creates(self):
        pm_nodes = [
            {"node_id": "N1", "title": "Old module", "primary": ["agent/old.py"],
             "parent_layer": "L3", "deps": [], "description": ""},
        ]
        decl = {"removed_nodes": ["N1"]}
        delta, hits, sources, source = _infer_graph_delta(
            pm_nodes, ["agent/old.py"], None, {}, prd_declarations=decl
        )
        # N1 should appear as remove_node, not as a standard create
        remove_ops = [c for c in delta["creates"] if c.get("op") == "remove_node"]
        standard_creates = [c for c in delta["creates"] if c.get("op") != "remove_node"]
        self.assertEqual(len(remove_ops), 1)
        self.assertEqual(remove_ops[0]["node_id"], "N1")
        self.assertEqual(remove_ops[0]["source"], "pm_declaration")
        # No standard create for N1
        self.assertFalse(any(c.get("node_id") == "N1" for c in standard_creates))

    def test_removed_node_skipped_in_rule_h_bridge(self):
        pm_nodes = [
            {"node_id": "N1", "title": "Old", "primary": ["agent/old.py"],
             "parent_layer": "", "deps": [], "description": ""},
            {"node_id": "N2", "title": "New", "primary": ["agent/new.py"],
             "parent_layer": "", "deps": [], "description": ""},
        ]
        decl = {"removed_nodes": ["N1"]}
        delta, hits, _, _ = _infer_graph_delta(
            pm_nodes, ["agent/new.py"], None, {}, prd_declarations=decl
        )
        # N2 should be bridged, N1 should not
        standard_creates = [c for c in delta["creates"] if c.get("op") != "remove_node"]
        self.assertTrue(any(c.get("node_id") == "N2" for c in standard_creates))
        self.assertFalse(any(c.get("node_id") == "N1" and c.get("op") != "remove_node"
                             for c in delta["creates"]))


class TestInferrerForUndeclared(unittest.TestCase):
    """Files not covered by declarations still go through auto-inference."""

    def test_undeclared_file_still_inferred(self):
        pm_nodes = [
            {"node_id": "N1", "title": "Auth", "primary": ["agent/auth.py"],
             "parent_layer": "L2", "deps": [], "description": ""},
        ]
        decl = {"removed_nodes": [], "unmapped_files": []}
        delta, hits, _, _ = _infer_graph_delta(
            pm_nodes, ["agent/auth.py"], None, {}, prd_declarations=decl
        )
        standard_creates = [c for c in delta["creates"] if c.get("op") != "remove_node"]
        self.assertTrue(any(c.get("node_id") == "N1" for c in standard_creates))

    def test_no_declarations_same_as_before(self):
        """With empty prd_declarations, behavior matches no-declarations path."""
        pm_nodes = [
            {"node_id": "N1", "title": "Auth", "primary": ["agent/auth.py"],
             "parent_layer": "L2", "deps": [], "description": ""},
        ]
        delta_no_decl, _, _, _ = _infer_graph_delta(
            pm_nodes, ["agent/auth.py"], None, {}
        )
        delta_empty_decl, _, _, _ = _infer_graph_delta(
            pm_nodes, ["agent/auth.py"], None, {}, prd_declarations={}
        )
        # Both should produce same standard creates
        no_decl_ids = {c["node_id"] for c in delta_no_decl["creates"]}
        empty_decl_ids = {c["node_id"] for c in delta_empty_decl["creates"]}
        self.assertEqual(no_decl_ids, empty_decl_ids)


class TestConflictAudit(unittest.TestCase):
    """R4: Override emits structured_log graph_delta.declaration_overrides_inference."""

    @patch("governance.auto_chain.structured_log")
    def test_override_emits_audit(self, mock_slog):
        pm_nodes = [
            {"node_id": "N1", "title": "Old", "primary": ["agent/old.py"],
             "parent_layer": "", "deps": [], "description": ""},
        ]
        decl = {"removed_nodes": ["N1"]}
        _infer_graph_delta(pm_nodes, ["agent/old.py"], None, {}, prd_declarations=decl)
        # Should have called structured_log with override event
        calls = [c for c in mock_slog.call_args_list
                 if len(c[0]) >= 2 and c[0][1] == "graph_delta.declaration_overrides_inference"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["node_id"], "N1")
        self.assertEqual(calls[0][1]["declared_op"], "remove_node")
        self.assertEqual(calls[0][1]["inferred_op"], "creates")


class TestQaGateValidation(unittest.TestCase):
    """AC5: _gate_qa_pass calls validate_prd_graph_declarations."""

    @patch("governance.auto_chain.validate_prd_graph_declarations")
    def test_qa_gate_calls_validation(self, mock_validate):
        mock_validate.return_value = ["file 'x.py' not declared"]
        # project_service is imported locally; mock the import target
        mock_ps = MagicMock()
        mock_ps.load_project_graph.return_value = {}
        with patch.dict("sys.modules", {"governance.project_service": mock_ps}):
            from governance.auto_chain import _gate_qa_pass
            conn = MagicMock()
            result = {"recommendation": "qa_pass"}
            metadata = {
                "changed_files": ["x.py"],
                "removed_nodes": ["N1"],
            }
            passed, reason = _gate_qa_pass(conn, "test-proj", result, metadata)
            self.assertFalse(passed)
            self.assertIn("PRD graph-declaration validation failed", reason)

    @patch("governance.auto_chain.validate_prd_graph_declarations")
    def test_qa_gate_passes_when_valid(self, mock_validate):
        mock_validate.return_value = []
        mock_ps = MagicMock()
        mock_ps.load_project_graph.return_value = {}
        with patch.dict("sys.modules", {"governance.project_service": mock_ps}), \
             patch("governance.auto_chain._query_graph_delta_proposed", return_value=None), \
             patch("governance.auto_chain._try_verify_update", return_value=(True, "")), \
             patch("governance.auto_chain._check_nodes_min_status", return_value=(True, "ok")):
            from governance.auto_chain import _gate_qa_pass
            conn = MagicMock()
            result = {"recommendation": "qa_pass"}
            metadata = {"changed_files": [], "related_nodes": []}
            passed, reason = _gate_qa_pass(conn, "test-proj", result, metadata)
            self.assertTrue(passed)


class TestCombinedDelta(unittest.TestCase):
    """Declarations + inference produce merged result."""

    def test_remove_plus_create(self):
        pm_nodes = [
            {"node_id": "N1", "title": "Old", "primary": ["agent/old.py"],
             "parent_layer": "", "deps": [], "description": ""},
            {"node_id": "N2", "title": "New", "primary": ["agent/new.py"],
             "parent_layer": "L3", "deps": [], "description": "New mod"},
        ]
        decl = {"removed_nodes": ["N1"]}
        delta, hits, sources, source = _infer_graph_delta(
            pm_nodes, ["agent/old.py", "agent/new.py"], None, {},
            prd_declarations=decl
        )
        # Should have remove_node for N1 and standard create for N2
        remove_ops = [c for c in delta["creates"] if c.get("op") == "remove_node"]
        standard = [c for c in delta["creates"] if c.get("op") != "remove_node"]
        self.assertEqual(len(remove_ops), 1)
        self.assertEqual(remove_ops[0]["node_id"], "N1")
        self.assertTrue(any(c["node_id"] == "N2" for c in standard))
        self.assertIn("pm_declarations", sources)
        self.assertIn("pm_proposed_nodes", sources)


class TestRemappedFilesSkipInferrer(unittest.TestCase):
    """remapped_files are excluded from auto-inferrer file matching."""

    def test_remapped_file_excluded_from_rule_a(self):
        pm_nodes = [
            {"node_id": "N1", "title": "Module", "primary": ["agent/mod.py"],
             "parent_layer": "", "deps": [], "description": ""},
        ]
        decl = {"remapped_files": ["agent/mod.py"]}
        delta, hits, _, _ = _infer_graph_delta(
            pm_nodes, ["agent/mod.py"], None, {}, prd_declarations=decl
        )
        # Rule A should not match because agent/mod.py is in declared_files
        rule_a = [h for h in hits if h.get("rule") == "A"]
        self.assertEqual(len(rule_a), 0)


if __name__ == "__main__":
    unittest.main()
