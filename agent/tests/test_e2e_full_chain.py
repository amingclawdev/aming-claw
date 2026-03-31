import json
import os
import sys
import unittest
from unittest import mock

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


class TestE2EFullChain(unittest.TestCase):
    def test_pm_to_deploy_chain_progresses_through_all_stages(self):
        from governance.db import get_connection
        from governance import auto_chain, task_registry

        project_id = "e2e-full-chain"
        conn = get_connection(project_id)
        conn.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM project_version WHERE project_id = ?", (project_id,))
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, updated_at, updated_by, git_head, dirty_files, git_synced_at, observer_mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, "abc1234", "2026-03-30T00:00:00Z", "init", "abc1234", "[]", "2026-03-30T00:00:00Z", 1),
        )
        conn.commit()

        pm_result = {
            "target_files": ["agent/foo.py"],
            "test_files": ["agent/tests/test_foo.py"],
            "requirements": ["R1"],
            "acceptance_criteria": ["AC1"],
            "verification": {"command": "pytest agent/tests/test_foo.py -q"},
            "doc_impact": {"files": [], "changes": ["No doc update required."]},
            "skip_reasons": {"proposed_nodes": "Synthetic full-chain test uses task-level flow only."},
        }

        with mock.patch.object(auto_chain, "_gate_version_check", return_value=(True, "ok")), \
             mock.patch.object(auto_chain, "_publish_event"), \
             mock.patch.object(auto_chain, "_write_chain_memory"), \
             mock.patch.object(auto_chain, "_try_verify_update"):
            out_pm = auto_chain._do_chain(conn, project_id, "task-pm-root", "pm", pm_result, {})
            dev_task = task_registry.get_task(conn, out_pm["task_id"])
            dev_meta = json.loads(dev_task["metadata_json"])

            out_dev = auto_chain._do_chain(
                conn, project_id, dev_task["task_id"], "dev",
                {"changed_files": ["agent/foo.py"], "test_results": {"ran": True, "failed": 0}},
                dev_meta,
            )
            test_task = task_registry.get_task(conn, out_dev["task_id"])
            test_meta = json.loads(test_task["metadata_json"])

            out_test = auto_chain._do_chain(
                conn, project_id, test_task["task_id"], "test",
                {"test_report": {"passed": 3, "failed": 0}, "summary": "ok", "changed_files": ["agent/foo.py"]},
                test_meta,
            )
            qa_task = task_registry.get_task(conn, out_test["task_id"])
            qa_meta = json.loads(qa_task["metadata_json"])

            out_qa = auto_chain._do_chain(
                conn, project_id, qa_task["task_id"], "qa",
                {"recommendation": "qa_pass", "review_summary": "ok", "issues": [], "doc_updates_applied": []},
                qa_meta,
            )
            gate_task = task_registry.get_task(conn, out_qa["task_id"])
            gate_meta = json.loads(gate_task["metadata_json"])

            out_gate = auto_chain._do_chain(
                conn, project_id, gate_task["task_id"], "gatekeeper",
                {"recommendation": "merge_pass", "review_summary": "ok", "pm_alignment": "pass", "checked_requirements": ["R1"]},
                gate_meta,
            )
            merge_task = task_registry.get_task(conn, out_gate["task_id"])
            merge_meta = json.loads(merge_task["metadata_json"])

            out_merge = auto_chain._do_chain(
                conn, project_id, merge_task["task_id"], "merge",
                {"merge_commit": "abc1234", "changed_files": ["agent/foo.py"]},
                merge_meta,
            )
            deploy_task = task_registry.get_task(conn, out_merge["task_id"])
            deploy_meta = json.loads(deploy_task["metadata_json"])

            out_deploy = auto_chain._do_chain(
                conn, project_id, deploy_task["task_id"], "deploy",
                {"deploy": "completed", "report": {"success": True, "smoke_test": {"all_pass": True}}},
                deploy_meta,
            )

        self.assertEqual(dev_task["type"], "dev")
        self.assertEqual(test_task["type"], "test")
        self.assertEqual(qa_task["type"], "qa")
        self.assertEqual(gate_task["type"], "gatekeeper")
        self.assertEqual(merge_task["type"], "merge")
        self.assertEqual(deploy_task["type"], "deploy")
        self.assertEqual(out_deploy["deploy"], "completed")
        self.assertTrue(out_deploy["report"]["success"])


if __name__ == "__main__":
    unittest.main()
