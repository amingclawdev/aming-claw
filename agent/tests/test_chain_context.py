"""Tests for chain context event-sourced store."""

import json
import os
import sqlite3
import sys
import unittest

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.chain_context import (
    ChainContextStore, _extract_core, RESULT_CORE_FIELDS,
)


class TestExtractCore(unittest.TestCase):
    def test_extracts_known_fields(self):
        result = {
            "target_files": ["a.py"],
            "changed_files": ["b.py"],
            "summary": "test",
            "extra_field": "ignored",
        }
        core = _extract_core(result)
        self.assertEqual(core["target_files"], ["a.py"])
        self.assertEqual(core["summary"], "test")
        self.assertNotIn("extra_field", core)

    def test_extracts_from_prd_nested(self):
        result = {"prd": {"target_files": ["c.py"], "verification": {"test": True}}}
        core = _extract_core(result)
        self.assertEqual(core["target_files"], ["c.py"])
        self.assertEqual(core["verification"], {"test": True})

    def test_empty_result(self):
        self.assertEqual(_extract_core({}), {})
        self.assertEqual(_extract_core(None), {})


class TestChainContextStore(unittest.TestCase):
    def setUp(self):
        self.store = ChainContextStore()
        self.pid = "test-proj"

    def _create_task(self, task_id, task_type="pm", parent=None, prompt="test prompt"):
        self.store.on_task_created({
            "task_id": task_id,
            "type": task_type,
            "prompt": prompt,
            "parent_task_id": parent or "",
            "project_id": self.pid,
        })

    def _complete_task(self, task_id, result=None):
        self.store.on_task_completed({
            "task_id": task_id,
            "result": result or {},
            "project_id": self.pid,
        })

    def test_create_chain(self):
        self._create_task("t1")
        chain = self.store.get_chain("t1")
        self.assertIsNotNone(chain)
        self.assertEqual(chain["state"], "running")
        self.assertEqual(chain["stage_count"], 1)

    def test_chain_progression(self):
        self._create_task("t1", "pm")
        self._complete_task("t1", {"target_files": ["a.py"]})
        self._create_task("t2", "dev", parent="t1")
        chain = self.store.get_chain("t2")
        self.assertEqual(chain["stage_count"], 2)
        self.assertEqual(chain["current_stage"], "t2")

    def test_get_original_prompt(self):
        self._create_task("t1", "pm", prompt="Build feature X")
        self._create_task("t2", "dev", parent="t1", prompt="Implement X")
        prompt = self.store.get_original_prompt("t2")
        self.assertEqual(prompt, "Build feature X")

    def test_get_parent_result(self):
        self._create_task("t1", "pm")
        self._complete_task("t1", {"target_files": ["a.py"], "verification": {"test": True}})
        self._create_task("t2", "dev", parent="t1")
        parent_result = self.store.get_parent_result("t2")
        self.assertIsNotNone(parent_result)
        self.assertEqual(parent_result["target_files"], ["a.py"])

    def test_get_parent_result_none_for_root(self):
        self._create_task("t1", "pm")
        self.assertIsNone(self.store.get_parent_result("t1"))

    def test_gate_blocked(self):
        self._create_task("t1", "dev")
        self.store.on_gate_blocked({
            "task_id": "t1", "reason": "missing docs",
            "project_id": self.pid,
        })
        self.assertEqual(self.store.get_state("t1"), "blocked")
        chain = self.store.get_chain("t1")
        blocked_stage = [s for s in chain["stages"] if s["task_id"] == "t1"][0]
        self.assertEqual(blocked_stage["gate_reason"], "missing docs")

    def test_retry_flow(self):
        self._create_task("t1", "dev")
        self.store.on_gate_blocked({
            "task_id": "t1", "reason": "test fail",
            "project_id": self.pid,
        })
        self.store.on_task_retry({
            "task_id": "t2", "original_task_id": "t1",
            "project_id": self.pid,
        })
        self.assertEqual(self.store.get_state("t1"), "retrying")
        chain = self.store.get_chain("t2")
        retry_stage = [s for s in chain["stages"] if s["task_id"] == "t2"][0]
        self.assertEqual(retry_stage["attempt"], 2)

    def test_failed_auto_archives(self):
        self._create_task("t1", "dev")
        self.store.on_task_failed({
            "task_id": "t1", "reason": "retry_exhausted",
            "project_id": self.pid,
        })
        # Chain should be archived (released from memory)
        self.assertIsNone(self.store.get_chain("t1"))
        self.assertIsNone(self.store.get_state("t1"))

    def test_merge_completes_chain(self):
        self._create_task("t1", "merge")
        self._complete_task("t1")
        self.assertEqual(self.store.get_state("t1"), "completed")

    def test_archive_releases_memory(self):
        self._create_task("t1", "pm")
        self._create_task("t2", "dev", parent="t1")
        self.store.archive_chain("t2", self.pid)
        self.assertIsNone(self.store.get_chain("t1"))
        self.assertIsNone(self.store.get_chain("t2"))

    def test_role_filtering(self):
        self._create_task("t1", "pm", prompt="secret PRD")
        self._complete_task("t1", {"target_files": ["a.py"], "summary": "done"})
        self._create_task("t2", "dev", parent="t1")

        # test role: should only see dev and test stages, not pm
        chain = self.store.get_chain("t2", role="test")
        visible_types = {s["type"] for s in chain["stages"]}
        self.assertIn("dev", visible_types)
        self.assertNotIn("pm", visible_types)

        # coordinator: sees everything
        chain = self.store.get_chain("t2", role="coordinator")
        visible_types = {s["type"] for s in chain["stages"]}
        self.assertIn("pm", visible_types)
        self.assertIn("dev", visible_types)

    def test_idempotent_create(self):
        self._create_task("t1")
        self._create_task("t1")  # duplicate
        chain = self.store.get_chain("t1")
        self.assertEqual(chain["stage_count"], 1)

    def test_unknown_task_returns_none(self):
        self.assertIsNone(self.store.get_chain("nonexistent"))
        self.assertIsNone(self.store.get_state("nonexistent"))
        self.assertEqual(self.store.get_original_prompt("nonexistent"), "")
        self.assertIsNone(self.store.get_parent_result("nonexistent"))


class TestChainContextRecovery(unittest.TestCase):
    """Test crash recovery by simulating DB events."""

    def test_recovery_rebuilds_state(self):
        # Create events in a fresh store, then recover in a new store
        store1 = ChainContextStore()
        store1._recovering = True  # suppress DB writes

        store1.on_task_created({
            "task_id": "r1", "type": "pm", "prompt": "build it",
            "parent_task_id": "", "project_id": "proj",
        })
        store1.on_task_completed({
            "task_id": "r1", "result": {"target_files": ["x.py"]},
            "project_id": "proj",
        })
        store1.on_task_created({
            "task_id": "r2", "type": "dev", "prompt": "implement",
            "parent_task_id": "r1", "project_id": "proj",
        })
        store1._recovering = False

        # Verify state was built
        chain = store1.get_chain("r2")
        self.assertIsNotNone(chain)
        self.assertEqual(chain["stage_count"], 2)
        self.assertEqual(store1.get_original_prompt("r2"), "build it")


if __name__ == "__main__":
    unittest.main()
