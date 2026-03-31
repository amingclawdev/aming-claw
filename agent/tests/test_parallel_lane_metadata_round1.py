import json
import os
import sys
import unittest

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


class TestParallelLaneMetadataRound1(unittest.TestCase):
    def test_create_pm_task_forwards_parallel_lane_metadata(self):
        from executor_worker import ExecutorWorker

        worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000")
        calls = []

        def fake_api(method, path, data=None):
            calls.append((method, path, data))
            if path.endswith("/create"):
                return {"task_id": "task-pm-1"}
            return {}

        worker._api = fake_api
        worker._last_query_memories = [{"kind": "pitfall", "content": "example"}]

        task = {
            "task_id": "task-root-1",
            "prompt": "root prompt",
            "metadata": {
                "parallel_plan": "dirty-reconciliation-2026-03-30",
                "lane": "A",
                "lane_name": "runtime-gate-recovery-core",
                "split_plan_doc": "docs/dev/parallel-closure-plan-2026-03-30.md",
                "allow_dirty_workspace_reconciliation": True,
            },
        }
        result = {
            "actions": [{
                "type": "create_pm_task",
                "prompt": "Create a PM task for lane A",
                "target_files": ["agent/governance/server.py"],
                "related_nodes": [],
            }],
            "reply": "Creating PM task",
            "context_update": {"current_focus": "workflow_lane_a_reconciliation"},
        }

        worker._handle_coordinator_result(task, result)

        create_calls = [c for c in calls if c[1].endswith("/create")]
        self.assertEqual(len(create_calls), 1)
        metadata = create_calls[0][2]["metadata"]
        self.assertEqual(metadata["parallel_plan"], "dirty-reconciliation-2026-03-30")
        self.assertEqual(metadata["lane"], "A")
        self.assertEqual(metadata["lane_name"], "runtime-gate-recovery-core")
        self.assertTrue(metadata["allow_dirty_workspace_reconciliation"])
        self.assertEqual(metadata["target_files"], ["agent/governance/server.py"])
        self.assertEqual(metadata["_coordinator_context"]["current_focus"], "workflow_lane_a_reconciliation")


if __name__ == "__main__":
    unittest.main()
