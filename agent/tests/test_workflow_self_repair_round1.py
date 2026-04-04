import os
import sys
import unittest
from unittest import mock

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


class TestFailureClassifierRound1(unittest.TestCase):
    def test_graph_defect_is_marked_for_workflow_improvement(self):
        from governance.failure_classifier import classify_gate_failure

        out = classify_gate_failure("dev", "NodeNotFoundError: L4.12 missing in acceptance graph")

        self.assertEqual(out["failure_class"], "graph_defect")
        self.assertTrue(out["workflow_improvement"])

    def test_dirty_workspace_is_environment_defect_not_workflow_improvement(self):
        from governance.failure_classifier import classify_gate_failure

        out = classify_gate_failure("version_check", "Dirty workspace detected (6 files). Commit or discard out-of-band edits before continuing auto-chain.")

        self.assertEqual(out["failure_class"], "environment_defect")
        self.assertFalse(out["workflow_improvement"])

    def test_prompt_conflict_is_contract_defect(self):
        from governance.failure_classifier import classify_gate_failure

        out = classify_gate_failure(
            "dev",
            "Prompt conflict: gate reason says update README but lane contract says docs belong to Lane C",
        )

        self.assertEqual(out["failure_class"], "contract_defect")
        self.assertTrue(out["workflow_improvement"])


class TestWorkflowImprovementTaskRound1(unittest.TestCase):
    def test_do_chain_creates_workflow_improvement_task_for_graph_defect(self):
        from governance import auto_chain
        import governance.task_registry as task_registry

        metadata = {
            "_original_prompt": "Implement feature X",
            "_gate_retry_count": 0,
            "chain_depth": 0,
            "target_files": ["agent/foo.py"],
        }

        created = []

        def fake_create_task(conn, project_id, prompt, task_type, created_by, metadata, **kwargs):
            created.append({
                "task_type": task_type,
                "created_by": created_by,
                "prompt": prompt,
                "metadata": metadata,
            })
            if created_by == "auto-chain-workflow-improvement":
                return {"task_id": "task-workflow-fix-1"}
            return {"task_id": "task-retry-1"}

        with mock.patch.object(task_registry, "create_task", fake_create_task), \
             mock.patch.object(auto_chain, "_GATES", {"_gate_checkpoint": lambda *args: (False, "NodeNotFoundError: L4.12 missing in acceptance graph")}), \
             mock.patch.object(auto_chain, "_BUILDERS", {"test_builder": lambda *args: ("", {})}), \
             mock.patch.object(auto_chain, "CHAIN", {"dev": ("_gate_checkpoint", "test", "test_builder")}), \
             mock.patch.object(auto_chain, "_publish_event"), \
             mock.patch.object(auto_chain, "_write_chain_memory"), \
             mock.patch.object(auto_chain, "_try_verify_update"), \
             mock.patch.object(auto_chain, "_gate_version_check", return_value=(True, "ok")):
            out = auto_chain._do_chain(
                object(),
                "aming-claw",
                "task-dev-graph-1",
                "dev",
                {"changed_files": ["agent/foo.py"], "test_results": {"ran": True, "failed": 1}},
                metadata,
            )

        self.assertEqual(out["workflow_improvement_task_id"], "task-workflow-fix-1")
        self.assertEqual(out["failure_class"], "graph_defect")
        improvement = next(c for c in created if c["created_by"] == "auto-chain-workflow-improvement")
        self.assertEqual(improvement["task_type"], "task")
        self.assertEqual(improvement["metadata"]["operation_type"], "workflow_improvement")
        self.assertIn("Workflow graph/governance mismatch", improvement["prompt"])

    def test_version_gate_dirty_workspace_does_not_spawn_workflow_improvement(self):
        from governance import auto_chain
        import governance.task_registry as task_registry

        with mock.patch.object(task_registry, "create_task") as create_task, \
             mock.patch.object(auto_chain, "_gate_version_check", return_value=(False, "Dirty workspace detected (6 files). Commit or discard out-of-band edits before continuing auto-chain.")):
            out = auto_chain._do_chain(
                object(),
                "aming-claw",
                "task-pm-1",
                "pm",
                {"target_files": ["agent/foo.py"], "acceptance_criteria": ["AC1"], "verification": {"command": "pytest -q"}},
                {},
            )

        self.assertTrue(out["gate_blocked"])
        self.assertEqual(out["stage"], "version_check")
        self.assertNotIn("workflow_improvement_task_id", out)
        create_task.assert_not_called()

    def test_failed_task_creates_workflow_improvement_for_provider_tool_defect(self):
        from governance import auto_chain

        created = []

        def fake_create_task(conn, project_id, prompt, task_type, created_by, metadata, **kwargs):
            created.append({
                "task_type": task_type,
                "created_by": created_by,
                "prompt": prompt,
                "metadata": metadata,
            })
            return {"task_id": "task-workflow-fix-failed-1"}

        with mock.patch("governance.task_registry.create_task", fake_create_task), \
             mock.patch.object(auto_chain, "_publish_event"), \
             mock.patch("governance.audit_service.record"):
            out = auto_chain.on_task_failed(
                object(),
                "aming-claw",
                "task-dev-failed-1",
                "dev",
                result={"error": "Error: Reached max turns (40)"},
                metadata={"target_files": ["agent/ai_lifecycle.py"]},
                reason="Error: Reached max turns (40)",
            )

        self.assertEqual(out["task_id"], "task-workflow-fix-failed-1")
        self.assertEqual(out["classification"]["failure_class"], "provider_tool_defect")
        self.assertTrue(out["classification"]["workflow_improvement"])
        self.assertEqual(created[0]["created_by"], "auto-chain-workflow-improvement")
        self.assertIn("repair_provider_or_runtime_limits", created[0]["prompt"])


if __name__ == "__main__":
    unittest.main()
