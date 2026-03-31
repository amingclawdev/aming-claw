import json
import os
import sys
import tempfile
import unittest
from unittest import mock

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


class TestDeployStageRound3(unittest.TestCase):
    def test_merge_builds_followup_deploy_task(self):
        from governance.auto_chain import _build_deploy_prompt

        result = {
            "merge_commit": "abc123",
            "changed_files": ["agent/executor_worker.py"],
        }
        metadata = {
            "changed_files": ["agent/executor_worker.py"],
            "related_nodes": ["L4.41"],
        }

        prompt, out_meta = _build_deploy_prompt("task-merge-1", result, metadata)

        self.assertIn("Deploy changes after merge task task-merge-1", prompt)
        self.assertEqual(out_meta["merge_commit"], "abc123")
        self.assertEqual(out_meta["changed_files"], ["agent/executor_worker.py"])
        self.assertEqual(out_meta["related_nodes"], ["L4.41"])

    def test_host_deploy_returns_succeeded_when_report_successful(self):
        from executor_worker import ExecutorWorker

        worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace=os.getcwd())
        metadata = {"changed_files": ["agent/executor_worker.py"]}

        with mock.patch.object(worker, "_report_progress"), \
             mock.patch("deploy_chain.run_deploy", return_value={"success": True, "affected_services": ["executor"]}):
            out = worker._execute_deploy("task-deploy-1", metadata)

        self.assertEqual(out["status"], "succeeded")
        self.assertEqual(out["result"]["deploy"], "completed")


class TestDeploySmokeFallbackRound3(unittest.TestCase):
    def test_executor_health_falls_back_to_manager_status_file(self):
        from deploy_chain import _executor_health_from_state

        with tempfile.TemporaryDirectory() as td:
            state_dir = os.path.join(td, "codex-tasks", "state")
            os.makedirs(state_dir, exist_ok=True)
            status_path = os.path.join(state_dir, "manager_status.json")
            with open(status_path, "w", encoding="utf-8") as f:
                json.dump({"services": {"executor": "running", "manager": "running"}}, f)

            with mock.patch.dict(os.environ, {"SHARED_VOLUME_PATH": td}):
                self.assertTrue(_executor_health_from_state())


if __name__ == "__main__":
    unittest.main()
