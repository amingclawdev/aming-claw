"""Tests for PR1c: prd_declarations threading from _emit_or_infer_graph_delta to _infer_graph_delta.

Background:
    MF b6e874a (2026-04-29) added declared_files / declared_removed_ids filters
    inside _infer_graph_delta to prevent phantom creates for files PM declared as
    unmapped/removed. The filter is implemented but the production caller
    _emit_or_infer_graph_delta historically never passed prd_declarations, so all
    filters degenerated to no-ops.

This module covers:
  - test_prd_declarations_passed_through: kwargs threaded into _infer_graph_delta
  - test_phantom_create_filtered_for_unmapped_file: end-to-end Rule J phantom
    suppression for an unmapped/removed migration_state_machine.py
  - test_backward_compatible_no_declarations: payload without declaration fields
    yields prd_declarations with empty lists for all 4 fields; inference proceeds
    without exception.
"""

import json
import os
import sys
import unittest
import unittest.mock as mock

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.auto_chain import (
    _emit_or_infer_graph_delta,
    _PRD_GRAPH_DECLARATION_FIELDS,
)


class _BasePR1cThreadingTest(unittest.TestCase):
    """Shared setup: capture chain_context._persist_event calls and pre-wire
    chain mapping for dev-001 → pm-root."""

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

        self.store._task_to_root["dev-001"] = "pm-root"
        self.store._task_to_root["pm-root"] = "pm-root"

    def tearDown(self):
        self.store._persist_event = self._orig_persist
        self.store._task_to_root.pop("dev-001", None)
        self.store._task_to_root.pop("pm-root", None)


class TestPrdDeclarationsThreading(_BasePR1cThreadingTest):
    """AC1/AC2: prd_declarations is forwarded as kwarg to _infer_graph_delta."""

    def test_prd_declarations_passed_through(self):
        """When pm.prd.published carries declaration fields, they reach _infer_graph_delta."""
        result = {"changed_files": ["agent/auth.py"]}
        metadata = {"chain_id": "pm-root"}

        pm_prd_payload = json.dumps({
            "proposed_nodes": [
                {"node_id": "N1", "title": "Auth", "primary": ["agent/auth.py"],
                 "parent_layer": "", "deps": [], "description": ""},
            ],
            "removed_nodes": ["L7.21"],
            "unmapped_files": ["agent/governance/migration_state_machine.py"],
            "renamed_nodes": [{"from": "L7.10", "to": "L7.99"}],
            "remapped_files": [{"file": "agent/foo.py", "to_node": "L7.50"}],
            "test_files": [],
            "target_files": ["agent/auth.py"],
            "requirements": [],
            "acceptance_criteria": [],
        })

        mock_conn = mock.MagicMock()
        mock_row = {"payload_json": pm_prd_payload}
        mock_conn.execute.return_value.fetchone.return_value = mock_row

        captured = {}

        def _spy(pm_nodes, changed_files, dev_delta, dev_result, prd_declarations=None):
            captured["pm_nodes"] = pm_nodes
            captured["changed_files"] = changed_files
            captured["prd_declarations"] = prd_declarations
            return ({"creates": [], "updates": [], "links": []}, [], [], "auto-inferred")

        with mock.patch("governance.db.get_connection", return_value=mock_conn), \
                mock.patch("governance.auto_chain._infer_graph_delta", side_effect=_spy):
            _emit_or_infer_graph_delta(self.pid, "dev-001", result, metadata)

        # prd_declarations kwarg must be supplied
        self.assertIn("prd_declarations", captured)
        decl = captured["prd_declarations"]
        self.assertIsNotNone(decl)
        # All 4 declaration fields present
        for f in _PRD_GRAPH_DECLARATION_FIELDS:
            self.assertIn(f, decl, f"missing declaration field {f!r}")
        # Values match the payload
        self.assertEqual(decl["removed_nodes"], ["L7.21"])
        self.assertEqual(decl["unmapped_files"],
                         ["agent/governance/migration_state_machine.py"])
        self.assertEqual(decl["renamed_nodes"], [{"from": "L7.10", "to": "L7.99"}])
        self.assertEqual(decl["remapped_files"],
                         [{"file": "agent/foo.py", "to_node": "L7.50"}])


class TestPhantomCreateFilteredForUnmappedFile(_BasePR1cThreadingTest):
    """AC4: Rule J does NOT emit a phantom create for a PM-declared unmapped/removed file."""

    def test_phantom_create_filtered_for_unmapped_file(self):
        """End-to-end: removed_nodes=['L7.21'] +
        unmapped_files=['agent/governance/migration_state_machine.py'] →
        no creates entry references either of them."""
        result = {
            "changed_files": ["agent/governance/migration_state_machine.py"],
            # No graph_delta — force inference
        }
        metadata = {"chain_id": "pm-root"}

        pm_prd_payload = json.dumps({
            "proposed_nodes": [],
            "removed_nodes": ["L7.21"],
            "unmapped_files": ["agent/governance/migration_state_machine.py"],
            "renamed_nodes": [],
            "remapped_files": [],
            "test_files": [],
            "target_files": ["agent/governance/migration_state_machine.py"],
            "requirements": [],
            "acceptance_criteria": [],
        })

        mock_conn = mock.MagicMock()
        mock_row = {"payload_json": pm_prd_payload}
        mock_conn.execute.return_value.fetchone.return_value = mock_row

        with mock.patch("governance.db.get_connection", return_value=mock_conn):
            _emit_or_infer_graph_delta(self.pid, "dev-001", result, metadata)

        proposed = [e for e in self._persisted_events
                    if e["event_type"] == "graph.delta.proposed"]
        # Inspect every creates entry in every emitted event.
        # NOTE: PM-declared `remove_node` ops (source='pm_declaration') are
        # intentionally appended to creates[] (line 574 in auto_chain.py) — they
        # are explicit declarations, not phantom positive creates. Filter those
        # out before asserting AC4.
        for evt in proposed:
            creates = evt["payload"].get("graph_delta", {}).get("creates", [])
            positive_creates = [
                c for c in creates
                if c.get("op") != "remove_node" and c.get("source") != "pm_declaration"
            ]
            for c in positive_creates:
                # No phantom L7.21 reuse via newly-created node
                self.assertNotEqual(
                    c.get("node_id"), "L7.21",
                    f"phantom create reused freed node_id L7.21: {c!r}",
                )
                # No primary referencing the unmapped file
                primary = c.get("primary", [])
                if isinstance(primary, str):
                    primary = [primary]
                primary_norm = {p.replace("\\", "/") for p in primary}
                self.assertNotIn(
                    "agent/governance/migration_state_machine.py",
                    primary_norm,
                    f"phantom create references unmapped file: {c!r}",
                )


class TestBackwardCompatibleNoDeclarations(_BasePR1cThreadingTest):
    """AC5: Payload with only proposed_nodes (no declaration fields) yields
    prd_declarations whose 4 declaration fields are all empty lists. Inference
    must still proceed without exception."""

    def test_backward_compatible_no_declarations(self):
        result = {"changed_files": ["agent/auth.py"]}
        metadata = {"chain_id": "pm-root"}

        pm_prd_payload = json.dumps({
            "proposed_nodes": [
                {"node_id": "N1", "title": "Auth", "primary": ["agent/auth.py"],
                 "parent_layer": "", "deps": [], "description": ""},
            ],
            # NO removed_nodes / unmapped_files / renamed_nodes / remapped_files
            "test_files": [],
            "target_files": ["agent/auth.py"],
            "requirements": [],
            "acceptance_criteria": [],
        })

        mock_conn = mock.MagicMock()
        mock_row = {"payload_json": pm_prd_payload}
        mock_conn.execute.return_value.fetchone.return_value = mock_row

        captured = {}

        def _spy(pm_nodes, changed_files, dev_delta, dev_result, prd_declarations=None):
            captured["prd_declarations"] = prd_declarations
            return ({"creates": [], "updates": [], "links": []}, [], [], "auto-inferred")

        with mock.patch("governance.db.get_connection", return_value=mock_conn), \
                mock.patch("governance.auto_chain._infer_graph_delta", side_effect=_spy):
            # Must not raise
            _emit_or_infer_graph_delta(self.pid, "dev-001", result, metadata)

        decl = captured.get("prd_declarations")
        self.assertIsNotNone(decl, "prd_declarations kwarg must always be supplied")
        # All 4 fields present and empty
        for f in _PRD_GRAPH_DECLARATION_FIELDS:
            self.assertIn(f, decl, f"missing declaration field {f!r}")
            self.assertEqual(
                decl[f], [],
                f"backward-compat: {f!r} should default to [] when payload omits it",
            )


if __name__ == "__main__":
    unittest.main()
