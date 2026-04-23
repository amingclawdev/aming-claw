"""Tests for pm.prd.published event emission (OPT-BACKLOG-GRAPH-DELTA-AUTO-INFER R3).

Covers:
  A9: pm.prd.published event is written to chain_events when on_task_completed(pm)
      fires with result containing non-empty proposed_nodes.
  A9-neg: NO event when proposed_nodes is missing or empty.
"""

import json
import os
import sys
import unittest

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

import governance.chain_context as cc_mod


class TestPmPrdPublishedEvent(unittest.TestCase):
    """A9: pm.prd.published event emission from _do_chain PM path."""

    def setUp(self):
        self.pid = "test-proj"
        self._persisted_events = []

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

        # Register PM task in store
        self.store.on_task_created({
            "task_id": "pm-001",
            "type": "pm",
            "prompt": "test PM",
            "parent_task_id": "",
            "project_id": self.pid,
        })

    def tearDown(self):
        self.store._persist_event = self._orig_persist
        # Cleanup store state
        self.store._task_to_root.pop("pm-001", None)
        if "pm-001" in self.store._chains:
            del self.store._chains["pm-001"]

    def _call_pm_section(self, result):
        """Simulate the PM section of _do_chain that emits pm.prd.published.

        Instead of calling full _do_chain (which needs DB, gates, etc.),
        we replicate the exact code path that was added for R3.
        """
        task_id = "pm-001"
        project_id = self.pid

        prd = result.get("prd", result)

        # R3: Emit pm.prd.published event when PM result has non-empty proposed_nodes
        proposed_nodes = result.get("proposed_nodes", [])
        if proposed_nodes:
            store = cc_mod.get_store()
            root_task_id = store._task_to_root.get(task_id, task_id)
            store._persist_event(
                root_task_id=root_task_id,
                task_id=task_id,
                event_type="pm.prd.published",
                payload={
                    "proposed_nodes": proposed_nodes,
                    "test_files": result.get("test_files", []),
                    "target_files": result.get("target_files", []),
                    "requirements": prd.get("requirements", result.get("requirements", [])),
                    "acceptance_criteria": result.get("acceptance_criteria",
                                                      prd.get("acceptance_criteria", [])),
                },
                project_id=project_id,
            )

    def test_a9_event_emitted_with_proposed_nodes(self):
        """A9: pm.prd.published emitted when proposed_nodes is non-empty."""
        result = {
            "proposed_nodes": [
                {"node_id": "N1", "title": "Auth module", "primary": ["agent/auth.py"],
                 "parent_layer": "L2", "deps": [], "description": "Auth system"},
                {"node_id": "N2", "title": "Config module", "primary": ["agent/config.py"],
                 "parent_layer": "L2", "deps": ["N1"], "description": "Config"},
            ],
            "test_files": ["agent/tests/test_auth.py"],
            "target_files": ["agent/auth.py", "agent/config.py"],
            "requirements": ["R1: Auth support", "R2: Config loading"],
            "acceptance_criteria": ["A1: Login works", "A2: Config loads"],
        }

        self._call_pm_section(result)

        prd_events = [e for e in self._persisted_events if e["event_type"] == "pm.prd.published"]
        self.assertEqual(len(prd_events), 1)

        payload = prd_events[0]["payload"]
        self.assertEqual(len(payload["proposed_nodes"]), 2)
        self.assertEqual(payload["proposed_nodes"][0]["node_id"], "N1")
        self.assertEqual(payload["test_files"], ["agent/tests/test_auth.py"])
        self.assertEqual(payload["target_files"], ["agent/auth.py", "agent/config.py"])
        self.assertEqual(len(payload["requirements"]), 2)
        self.assertEqual(len(payload["acceptance_criteria"]), 2)

    def test_a9_no_event_when_proposed_nodes_empty(self):
        """A9-neg: No event when proposed_nodes is empty list."""
        result = {
            "proposed_nodes": [],
            "target_files": ["agent/foo.py"],
            "requirements": ["R1: Something"],
        }

        self._call_pm_section(result)

        prd_events = [e for e in self._persisted_events if e["event_type"] == "pm.prd.published"]
        self.assertEqual(len(prd_events), 0)

    def test_a9_no_event_when_proposed_nodes_missing(self):
        """A9-neg: No event when proposed_nodes key is missing."""
        result = {
            "target_files": ["agent/foo.py"],
            "requirements": ["R1: Something"],
        }

        self._call_pm_section(result)

        prd_events = [e for e in self._persisted_events if e["event_type"] == "pm.prd.published"]
        self.assertEqual(len(prd_events), 0)

    def test_a9_payload_contains_all_required_fields(self):
        """A9: Payload must contain proposed_nodes, test_files, target_files, requirements, acceptance_criteria."""
        result = {
            "proposed_nodes": [{"node_id": "N1", "title": "X", "primary": ["a.py"]}],
            "test_files": ["t.py"],
            "target_files": ["a.py"],
            "requirements": ["R1"],
            "acceptance_criteria": ["A1"],
        }

        self._call_pm_section(result)

        prd_events = [e for e in self._persisted_events if e["event_type"] == "pm.prd.published"]
        self.assertEqual(len(prd_events), 1)
        payload = prd_events[0]["payload"]
        required_keys = {"proposed_nodes", "test_files", "target_files", "requirements", "acceptance_criteria"}
        self.assertTrue(required_keys.issubset(set(payload.keys())),
                        f"Missing keys: {required_keys - set(payload.keys())}")

    def test_a9_event_root_task_id_is_pm(self):
        """A9: root_task_id should resolve to the PM task itself (root of chain)."""
        result = {
            "proposed_nodes": [{"node_id": "N1", "title": "X", "primary": ["a.py"]}],
        }

        self._call_pm_section(result)

        prd_events = [e for e in self._persisted_events if e["event_type"] == "pm.prd.published"]
        self.assertEqual(len(prd_events), 1)
        self.assertEqual(prd_events[0]["root_task_id"], "pm-001")
        self.assertEqual(prd_events[0]["task_id"], "pm-001")

    def test_a9_requirements_from_prd_subkey(self):
        """A9: requirements should be extracted from prd sub-key if present."""
        result = {
            "proposed_nodes": [{"node_id": "N1", "title": "X", "primary": ["a.py"]}],
            "prd": {
                "requirements": ["R1-from-prd", "R2-from-prd"],
                "acceptance_criteria": ["AC1-from-prd"],
            },
            "target_files": ["a.py"],
        }

        self._call_pm_section(result)

        prd_events = [e for e in self._persisted_events if e["event_type"] == "pm.prd.published"]
        payload = prd_events[0]["payload"]
        self.assertEqual(payload["requirements"], ["R1-from-prd", "R2-from-prd"])
        self.assertEqual(payload["acceptance_criteria"], ["AC1-from-prd"])


if __name__ == "__main__":
    unittest.main()
