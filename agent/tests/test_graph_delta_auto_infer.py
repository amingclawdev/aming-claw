"""Tests for graph delta auto-inference (OPT-BACKLOG-GRAPH-DELTA-AUTO-INFER).

Covers:
  A1: _emit_or_infer_graph_delta exists and is called from _do_chain
  A2: Dev-emitted graph_delta passthrough with source='dev-emitted'
  A3: Missing graph_delta triggers auto-inference with source='auto-inferred'
  A4: Rule A — PM proposed_nodes with matching primary in changed_files
  A5: Rule B — @route decorator grep
  A6: Rule D — existing graph nodes get updates[] entry
  A7: Rule E — dev override merges with inferred, source='dev-emitted+inferred-gaps'
  A8: Rule F — discard creates[] where all primaries are docs/dev/**
  A10: graph.delta.inferred event emitted for auto-inference
"""

import json
import os
import sys
import tempfile
import unittest

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.auto_chain import (
    _infer_graph_delta,
    _emit_or_infer_graph_delta,
)


class TestInferGraphDeltaRuleA(unittest.TestCase):
    """A4: Rule A — PM proposed_nodes with matching primary in changed_files (excl .md)."""

    def test_matching_primary_creates_entry(self):
        pm_nodes = [
            {"node_id": "N1", "title": "Auth module", "primary": ["agent/auth.py"],
             "parent_layer": "L2", "deps": [], "description": "Auth"},
        ]
        changed = ["agent/auth.py", "agent/utils.py"]
        delta, hits, sources, source = _infer_graph_delta(pm_nodes, changed, None, {})
        self.assertEqual(len(delta["creates"]), 1)
        self.assertEqual(delta["creates"][0]["title"], "Auth module")
        self.assertEqual(delta["creates"][0]["node_id"], "N1")
        rule_a_hits = [h for h in hits if h["rule"] == "A"]
        self.assertEqual(len(rule_a_hits), 1)
        self.assertIn("pm_proposed_nodes", sources)

    def test_md_files_excluded_from_match(self):
        pm_nodes = [
            {"node_id": "N2", "title": "Docs node", "primary": ["docs/readme.md"],
             "parent_layer": "", "deps": [], "description": ""},
        ]
        changed = ["docs/readme.md"]
        delta, hits, _, _ = _infer_graph_delta(pm_nodes, changed, None, {})
        # .md files excluded from non_md_changed, so no Rule A match
        rule_a_hits = [h for h in hits if h["rule"] == "A"]
        self.assertEqual(len(rule_a_hits), 0)

    def test_no_pm_nodes_skips_rule_a(self):
        delta, hits, _, _ = _infer_graph_delta([], ["agent/foo.py"], None, {})
        rule_a_hits = [h for h in hits if h["rule"] == "A"]
        self.assertEqual(len(rule_a_hits), 0)

    def test_primary_as_string(self):
        """Handle proposed_node with primary as string instead of list."""
        pm_nodes = [
            {"node_id": "N3", "title": "Single", "primary": "agent/single.py",
             "parent_layer": "", "deps": [], "description": ""},
        ]
        changed = ["agent/single.py"]
        delta, hits, _, _ = _infer_graph_delta(pm_nodes, changed, None, {})
        self.assertEqual(len(delta["creates"]), 1)


class TestInferGraphDeltaRuleB(unittest.TestCase):
    """A5: Rule B — @route decorator grep on changed agent/**/*.py."""

    def test_route_decorator_detected(self):
        # Create a temp file with a route decorator
        tmpdir = tempfile.mkdtemp()
        agent_dir = os.path.join(tmpdir, "agent")
        os.makedirs(agent_dir)
        route_file = os.path.join(agent_dir, "api.py")
        with open(route_file, "w") as f:
            f.write('@app.get("/health")\ndef health(): pass\n')

        rel_path = "agent/api.py"
        # We need the file to be findable — use abs path as changed_file
        delta, hits, _, _ = _infer_graph_delta([], [route_file], None, {})
        # route_file is absolute and doesn't start with "agent/", so Rule B won't match
        # Use relative path approach instead
        old_cwd = os.getcwd()
        try:
            os.chdir(tmpdir)
            delta, hits, sources, _ = _infer_graph_delta([], [rel_path], None, {})
            rule_b_hits = [h for h in hits if h["rule"] == "B"]
            self.assertEqual(len(rule_b_hits), 1)
            self.assertIn("HTTP endpoint:", delta["creates"][0]["title"])
            self.assertIn("GET", delta["creates"][0]["title"])
            self.assertIn("/health", delta["creates"][0]["title"])
            self.assertIn("route_decorator_grep", sources)
        finally:
            os.chdir(old_cwd)
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_non_agent_py_skipped(self):
        """Files not under agent/ are skipped by Rule B."""
        delta, hits, _, _ = _infer_graph_delta([], ["lib/server.py"], None, {})
        rule_b_hits = [h for h in hits if h["rule"] == "B"]
        self.assertEqual(len(rule_b_hits), 0)


class TestInferGraphDeltaRuleD(unittest.TestCase):
    """A6: Rule D — existing graph nodes with primary in changed_files get updates[]."""

    def test_existing_node_gets_update(self):
        """Rule D: mock project_service to return a graph with nodes."""
        import unittest.mock as mock
        import governance.project_service as real_ps

        mock_graph = mock.MagicMock()
        mock_graph.list_nodes.return_value = ["existing-node-1"]
        mock_graph.get_node.return_value = {"primary": ["agent/foo.py"], "secondary": []}

        with mock.patch.object(real_ps, "load_project_graph", return_value=mock_graph):
            delta, hits, sources, _ = _infer_graph_delta(
                [], ["agent/foo.py"], None,
                {"project_id": "test-proj", "task_id": "dev-001"},
            )

        self.assertEqual(len(delta["updates"]), 1)
        self.assertEqual(delta["updates"][0]["node_id"], "existing-node-1")
        self.assertEqual(delta["updates"][0]["fields"]["touched_by"], "dev-001")
        rule_d_hits = [h for h in hits if h["rule"] == "D"]
        self.assertEqual(len(rule_d_hits), 1)
        self.assertIn("existing_graph_nodes", sources)

    def test_pm_declared_update_skipped(self):
        """Rule D skips nodes already declared in dev_delta updates."""
        import unittest.mock as mock
        import governance.project_service as real_ps

        mock_graph = mock.MagicMock()
        mock_graph.list_nodes.return_value = ["existing-node-1"]
        mock_graph.get_node.return_value = {"primary": ["agent/foo.py"]}

        dev_delta = {"updates": [{"node_id": "existing-node-1", "fields": {"x": 1}}],
                     "creates": [], "links": []}

        with mock.patch.object(real_ps, "load_project_graph", return_value=mock_graph):
            delta, hits, _, _ = _infer_graph_delta(
                [], ["agent/foo.py"], dev_delta,
                {"project_id": "test-proj", "task_id": "dev-001"},
            )

        # Rule D should skip because dev_delta already declares this node
        rule_d_hits = [h for h in hits if h["rule"] == "D"]
        self.assertEqual(len(rule_d_hits), 0)


class TestInferGraphDeltaRuleE(unittest.TestCase):
    """A7: Rule E — dev override merges with inferred."""

    def test_dev_entries_replace_inferred_by_title(self):
        pm_nodes = [
            {"node_id": "N1", "title": "Auth module", "primary": ["agent/auth.py"],
             "parent_layer": "", "deps": [], "description": "PM version"},
        ]
        dev_delta = {
            "creates": [
                {"node_id": "N1-dev", "title": "Auth module", "primary": ["agent/auth.py"],
                 "parent_layer": "", "deps": [], "description": "Dev version"},
            ],
            "updates": [],
            "links": [],
        }
        changed = ["agent/auth.py"]
        delta, hits, _, source = _infer_graph_delta(pm_nodes, changed, dev_delta, {})
        # Dev entry replaces inferred (same title), so only dev entry remains
        titles = [c["title"] for c in delta["creates"]]
        self.assertEqual(titles.count("Auth module"), 1)
        self.assertEqual(delta["creates"][0]["description"], "Dev version")
        self.assertEqual(source, "dev-emitted+inferred-gaps")

    def test_inferred_fills_gaps(self):
        pm_nodes = [
            {"node_id": "N1", "title": "Auth module", "primary": ["agent/auth.py"],
             "parent_layer": "", "deps": [], "description": "PM"},
            {"node_id": "N2", "title": "Config module", "primary": ["agent/config.py"],
             "parent_layer": "", "deps": [], "description": "PM"},
        ]
        dev_delta = {
            "creates": [
                {"node_id": "N1-dev", "title": "Auth module", "primary": ["agent/auth.py"],
                 "parent_layer": "", "deps": [], "description": "Dev"},
            ],
            "updates": [],
            "links": [],
        }
        changed = ["agent/auth.py", "agent/config.py"]
        delta, _, _, source = _infer_graph_delta(pm_nodes, changed, dev_delta, {})
        # Auth replaced by dev, Config filled as gap
        titles = [c["title"] for c in delta["creates"]]
        self.assertIn("Auth module", titles)
        self.assertIn("Config module", titles)
        self.assertEqual(source, "dev-emitted+inferred-gaps")

    def test_dev_replaces_by_primary(self):
        pm_nodes = [
            {"node_id": "N1", "title": "PM Auth", "primary": ["agent/auth.py"],
             "parent_layer": "", "deps": [], "description": ""},
        ]
        dev_delta = {
            "creates": [
                {"node_id": "N1-dev", "title": "Dev Auth Different Title",
                 "primary": ["agent/auth.py"],
                 "parent_layer": "", "deps": [], "description": "Dev"},
            ],
            "updates": [],
            "links": [],
        }
        changed = ["agent/auth.py"]
        delta, _, _, source = _infer_graph_delta(pm_nodes, changed, dev_delta, {})
        # Dev replaces by primary match
        titles = [c["title"] for c in delta["creates"]]
        self.assertNotIn("PM Auth", titles)
        self.assertIn("Dev Auth Different Title", titles)


class TestInferGraphDeltaRuleF(unittest.TestCase):
    """A8: Rule F — discard creates[] where ALL primary files are docs/dev/**."""

    def test_all_dev_docs_discarded(self):
        pm_nodes = [
            {"node_id": "N1", "title": "Dev note", "primary": ["docs/dev/notes.md"],
             "parent_layer": "", "deps": [], "description": ""},
        ]
        # Force match by including non-.md file that matches docs/dev/
        # Actually docs/dev/notes.md is .md so Rule A won't match.
        # Let's test directly with a creates entry
        delta, hits, _, _ = _infer_graph_delta([], [], None, {})
        # No creates at all
        self.assertEqual(len(delta["creates"]), 0)

    def test_dev_doc_primary_discarded_directly(self):
        """Test Rule F by providing creates via dev_delta."""
        dev_delta = {
            "creates": [
                {"node_id": "N1", "title": "Dev note", "primary": ["docs/dev/impl-plan.md"],
                 "parent_layer": "", "deps": [], "description": ""},
            ],
            "updates": [],
            "links": [],
        }
        delta, hits, _, _ = _infer_graph_delta([], [], dev_delta, {})
        # Rule F should discard it
        rule_f_hits = [h for h in hits if h["rule"] == "F"]
        self.assertEqual(len(rule_f_hits), 1)
        self.assertEqual(len(delta["creates"]), 0)

    def test_mixed_primaries_not_discarded(self):
        """Creates with mix of dev doc and code files are kept."""
        dev_delta = {
            "creates": [
                {"node_id": "N1", "title": "Mixed", "primary": ["docs/dev/plan.md", "agent/foo.py"],
                 "parent_layer": "", "deps": [], "description": ""},
            ],
            "updates": [],
            "links": [],
        }
        delta, hits, _, _ = _infer_graph_delta([], [], dev_delta, {})
        # Not all primaries are dev docs, so it's kept
        self.assertEqual(len(delta["creates"]), 1)
        rule_f_hits = [h for h in hits if h["rule"] == "F"]
        self.assertEqual(len(rule_f_hits), 0)


class TestEmitOrInferGraphDelta(unittest.TestCase):
    """A1-A3, A10: Integration test for _emit_or_infer_graph_delta."""

    def setUp(self):
        self.pid = "test-proj"
        self._persisted_events = []

        import governance.chain_context as cc_mod
        self.store = cc_mod._store
        self._orig_persist = self.store._persist_event

        def _capture_persist(root_task_id, task_id, event_type, payload, project_id):
            self._persisted_events.append({
                "root_task_id": root_task_id,
                "task_id": task_id,
                "event_type": event_type,
                "payload": payload,
                "project_id": project_id,
            })
        self.store._persist_event = _capture_persist

        # Setup chain mapping
        self.store._task_to_root["dev-001"] = "pm-root"
        self.store._task_to_root["pm-root"] = "pm-root"

    def tearDown(self):
        self.store._persist_event = self._orig_persist
        self.store._task_to_root.pop("dev-001", None)
        self.store._task_to_root.pop("pm-root", None)

    def test_a1_function_exists(self):
        """A1: _emit_or_infer_graph_delta exists in auto_chain."""
        from governance import auto_chain
        self.assertTrue(hasattr(auto_chain, '_emit_or_infer_graph_delta'))
        self.assertTrue(callable(auto_chain._emit_or_infer_graph_delta))

    def test_a2_dev_emitted_passthrough(self):
        """A2: Dev result with non-empty graph_delta → source='dev-emitted'."""
        result = {
            "graph_delta": {
                "creates": [{"node_id": "N1", "title": "Test", "primary": ["a.py"]}],
                "updates": [],
                "links": [],
            },
        }
        metadata = {"chain_id": "pm-root"}

        # Mock db.get_connection to return no pm.prd.published event
        import unittest.mock as mock
        mock_conn = mock.MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None

        with mock.patch("governance.db.get_connection", return_value=mock_conn):
            _emit_or_infer_graph_delta(self.pid, "dev-001", result, metadata)

        proposed = [e for e in self._persisted_events if e["event_type"] == "graph.delta.proposed"]
        self.assertGreaterEqual(len(proposed), 1)
        self.assertEqual(proposed[0]["payload"]["source"], "dev-emitted")

    def test_a3_auto_inferred_when_no_graph_delta(self):
        """A3: Dev result without graph_delta + PM nodes → source='auto-inferred'."""
        result = {"changed_files": ["agent/auth.py"]}
        metadata = {"chain_id": "pm-root"}

        pm_prd_payload = json.dumps({
            "proposed_nodes": [
                {"node_id": "N1", "title": "Auth", "primary": ["agent/auth.py"],
                 "parent_layer": "", "deps": [], "description": ""},
            ],
            "test_files": [],
            "target_files": ["agent/auth.py"],
            "requirements": [],
            "acceptance_criteria": [],
        })

        import unittest.mock as mock
        mock_conn = mock.MagicMock()
        mock_row = {"payload_json": pm_prd_payload}
        mock_conn.execute.return_value.fetchone.return_value = mock_row

        with mock.patch("governance.db.get_connection", return_value=mock_conn):
            _emit_or_infer_graph_delta(self.pid, "dev-001", result, metadata)

        proposed = [e for e in self._persisted_events if e["event_type"] == "graph.delta.proposed"]
        self.assertGreaterEqual(len(proposed), 1)
        self.assertEqual(proposed[0]["payload"]["source"], "auto-inferred")
        self.assertGreaterEqual(len(proposed[0]["payload"]["graph_delta"]["creates"]), 1)

    def test_a10_inferred_event_emitted(self):
        """A10: graph.delta.inferred event emitted during auto-inference."""
        result = {"changed_files": ["agent/auth.py"]}
        metadata = {"chain_id": "pm-root"}

        pm_prd_payload = json.dumps({
            "proposed_nodes": [
                {"node_id": "N1", "title": "Auth", "primary": ["agent/auth.py"],
                 "parent_layer": "", "deps": [], "description": ""},
            ],
            "test_files": [],
            "target_files": [],
            "requirements": [],
            "acceptance_criteria": [],
        })

        import unittest.mock as mock
        mock_conn = mock.MagicMock()
        mock_row = {"payload_json": pm_prd_payload}
        mock_conn.execute.return_value.fetchone.return_value = mock_row

        with mock.patch("governance.db.get_connection", return_value=mock_conn):
            _emit_or_infer_graph_delta(self.pid, "dev-001", result, metadata)

        inferred = [e for e in self._persisted_events if e["event_type"] == "graph.delta.inferred"]
        self.assertEqual(len(inferred), 1)
        self.assertEqual(inferred[0]["payload"]["source"], "auto-inferred")
        self.assertIsInstance(inferred[0]["payload"]["inferred_from"], list)
        self.assertIsInstance(inferred[0]["payload"]["rule_hits"], list)

    def test_source_dev_emitted_plus_inferred_gaps(self):
        """A7+A6: Dev provides partial, inference fills gaps → source='dev-emitted+inferred-gaps'."""
        result = {
            "changed_files": ["agent/auth.py", "agent/config.py"],
            "graph_delta": {
                "creates": [
                    {"node_id": "D1", "title": "Auth override", "primary": ["agent/auth.py"],
                     "parent_layer": "", "deps": [], "description": "Dev"},
                ],
                "updates": [],
                "links": [],
            },
        }
        metadata = {"chain_id": "pm-root"}

        pm_prd_payload = json.dumps({
            "proposed_nodes": [
                {"node_id": "N2", "title": "Config module", "primary": ["agent/config.py"],
                 "parent_layer": "", "deps": [], "description": "PM"},
            ],
            "test_files": [],
            "target_files": [],
            "requirements": [],
            "acceptance_criteria": [],
        })

        import unittest.mock as mock
        mock_conn = mock.MagicMock()
        mock_row = {"payload_json": pm_prd_payload}
        mock_conn.execute.return_value.fetchone.return_value = mock_row

        with mock.patch("governance.db.get_connection", return_value=mock_conn):
            _emit_or_infer_graph_delta(self.pid, "dev-001", result, metadata)

        proposed = [e for e in self._persisted_events if e["event_type"] == "graph.delta.proposed"]
        self.assertGreaterEqual(len(proposed), 1)
        self.assertEqual(proposed[0]["payload"]["source"], "dev-emitted+inferred-gaps")


class TestInferGraphDeltaRuleH_L7(unittest.TestCase):
    """AC1: _infer_graph_delta returns creates[] containing every PM proposed_node
    with parent_layer=7 when dev result has no graph_delta (Rule H)."""

    def test_all_l7_pm_nodes_bridged_when_no_dev_delta(self):
        """AC1: dev_delta=None → all L7 PM nodes appear in creates[]."""
        pm_nodes = [
            {"node_id": "L7.1", "title": "L7 Node A", "primary": ["agent/a.py"],
             "parent_layer": "L7", "deps": [], "description": "First L7 node"},
            {"node_id": "L7.2", "title": "L7 Node B", "primary": ["agent/b.py"],
             "parent_layer": "L7", "deps": ["L7.1"], "description": "Second L7 node"},
            {"node_id": "L3.5", "title": "L3 Node", "primary": ["agent/c.py"],
             "parent_layer": "L3", "deps": [], "description": "Non-L7 node"},
        ]
        changed = []
        delta, hits, sources, source = _infer_graph_delta(pm_nodes, changed, None, {})

        # All 3 PM nodes should appear via Rule H (dev_delta is None)
        create_ids = [c["node_id"] for c in delta["creates"]]
        self.assertIn("L7.1", create_ids)
        self.assertIn("L7.2", create_ids)
        self.assertIn("L3.5", create_ids)

        rule_h_hits = [h for h in hits if h["rule"] == "H"]
        self.assertEqual(len(rule_h_hits), 3)  # All 3 bridged via H
        self.assertIn("pm_proposed_bridge", sources)

    def test_l7_node_with_matching_primary_uses_rule_a_then_h(self):
        """AC1: L7 nodes with matching primary → Rule A; remainder → Rule H."""
        pm_nodes = [
            {"node_id": "L7.1", "title": "Matched L7", "primary": ["agent/match.py"],
             "parent_layer": "L7", "deps": [], "description": ""},
            {"node_id": "L7.2", "title": "Unmatched L7", "primary": ["agent/other.py"],
             "parent_layer": "L7", "deps": [], "description": ""},
        ]
        changed = ["agent/match.py"]
        delta, hits, _, _ = _infer_graph_delta(pm_nodes, changed, None, {})

        create_ids = [c["node_id"] for c in delta["creates"]]
        self.assertIn("L7.1", create_ids)
        self.assertIn("L7.2", create_ids)

        # L7.1 matched by Rule A, L7.2 bridged by Rule H
        rule_a_hits = [h for h in hits if h["rule"] == "A"]
        rule_h_hits = [h for h in hits if h["rule"] == "H"]
        self.assertEqual(len(rule_a_hits), 1)
        self.assertEqual(len(rule_h_hits), 1)

    def test_l7_nodes_preserved_parent_layer(self):
        """AC1: creates[] entries preserve parent_layer field from PM nodes."""
        pm_nodes = [
            {"node_id": "L7.10", "title": "Deep L7", "primary": [],
             "parent_layer": "L7", "deps": [], "description": "Deep node"},
        ]
        delta, _, _, _ = _infer_graph_delta(pm_nodes, [], None, {})
        self.assertEqual(len(delta["creates"]), 1)
        self.assertEqual(delta["creates"][0]["parent_layer"], "L7")
        self.assertEqual(delta["creates"][0]["node_id"], "L7.10")


if __name__ == "__main__":
    unittest.main()
