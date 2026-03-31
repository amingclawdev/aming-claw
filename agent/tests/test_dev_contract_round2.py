import os
import sys
import unittest

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


class TestBuildDevPromptRound2(unittest.TestCase):
    def test_top_level_requirements_are_forwarded(self):
        from governance.auto_chain import _build_dev_prompt

        result = {
            "target_files": ["agent/foo.py"],
            "requirements": ["R1", "R2"],
            "acceptance_criteria": ["AC1"],
            "verification": {"command": "pytest"},
            "proposed_nodes": [{"title": "Node A"}],
            "related_nodes": ["L1.1"],
        }
        metadata = {
            "related_nodes": ["L1.1"],
            "test_files": ["agent/tests/test_foo.py"],
        }

        prompt, out_meta = _build_dev_prompt("task-1", result, metadata)

        self.assertIn('requirements: ["R1", "R2"]', prompt)
        self.assertEqual(out_meta["related_nodes"], ["L1.1"])
        self.assertEqual(out_meta["proposed_nodes"], [{"title": "Node A"}])


class TestMemoryEntryRound2(unittest.TestCase):
    def test_from_dict_accepts_module_alias_and_structured(self):
        from governance.models import MemoryEntry

        entry = MemoryEntry.from_dict({
            "module": "agent/foo.py",
            "kind": "decision",
            "content": "summary",
            "summary": "human summary",
            "structured": {"task_id": "task-1", "chain_stage": "dev"},
        })

        self.assertEqual(entry.module_id, "agent/foo.py")
        self.assertEqual(entry.applies_when, "human summary")
        self.assertEqual(entry.structured["task_id"], "task-1")


if __name__ == "__main__":
    unittest.main()
