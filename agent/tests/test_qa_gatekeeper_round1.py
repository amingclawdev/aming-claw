import os
import sqlite3
import sys
import unittest
from unittest import mock

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


class TestQaPromptRound1(unittest.TestCase):
    def test_qa_prompt_includes_pm_contract_fields(self):
        from governance.auto_chain import _build_qa_prompt

        result = {
            "test_report": {"passed": 3, "failed": 0, "tool": "pytest"},
            "changed_files": ["agent/foo.py"],
            "_worktree": "C:/tmp/worktree",
            "_branch": "dev/task-1",
        }
        metadata = {
            "requirements": ["R1"],
            "acceptance_criteria": ["AC1"],
            "verification": {"command": "pytest agent/tests/test_foo.py -q"},
            "doc_impact": {"files": [], "changes": ["No doc update required."]},
        }

        prompt, out_meta = _build_qa_prompt("task-test-1", result, metadata)

        self.assertIn('requirements: ["R1"]', prompt)
        self.assertIn('acceptance_criteria: ["AC1"]', prompt)
        self.assertIn('verification: {"command": "pytest agent/tests/test_foo.py -q"}', prompt)
        self.assertIn('doc_impact: {"files": [], "changes": ["No doc update required."]}', prompt)
        self.assertEqual(out_meta["_worktree"], "C:/tmp/worktree")
        self.assertEqual(out_meta["_branch"], "dev/task-1")


class TestGatekeeperStageRound1(unittest.TestCase):
    def test_gatekeeper_prompt_is_isolated_and_contract_based(self):
        from governance.auto_chain import _build_gatekeeper_prompt

        result = {
            "review_summary": "QA passed",
            "issues": [],
            "doc_updates_applied": [],
        }
        metadata = {
            "requirements": ["R1"],
            "acceptance_criteria": ["AC1"],
            "verification": {"command": "pytest agent/tests/test_foo.py -q"},
            "doc_impact": {"files": [], "changes": ["No doc update required."]},
            "test_report": {"passed": 3, "failed": 0},
            "changed_files": ["agent/foo.py"],
        }

        prompt, _ = _build_gatekeeper_prompt("task-qa-1", result, metadata)

        self.assertIn("final isolated acceptance check before merge", prompt)
        self.assertIn('requirements: ["R1"]', prompt)
        self.assertIn('acceptance_criteria: ["AC1"]', prompt)
        self.assertIn('"recommendation":"merge_pass|reject"', prompt)

    def test_gatekeeper_gate_requires_explicit_merge_pass(self):
        from governance.auto_chain import _gate_gatekeeper_pass

        passed, reason = _gate_gatekeeper_pass(object(), "aming-claw", {"review_summary": "no decision"}, {})
        self.assertFalse(passed)
        self.assertIn("merge_pass", reason)

    def test_dedup_treats_observer_hold_as_existing(self):
        from governance import auto_chain

        class FakeRow(dict):
            pass

        class FakeConn:
            def execute(self, query, params=()):
                if "status IN ('queued','claimed','observer_hold')" in query:
                    return mock.Mock(fetchone=lambda: FakeRow({"task_id": "task-existing-qa"}))
                return mock.Mock(fetchone=lambda: None)

            def commit(self):
                return None

        with mock.patch.object(auto_chain, "_gate_version_check", return_value=(True, "ok")), \
             mock.patch.object(auto_chain, "_publish_event"), \
             mock.patch.object(auto_chain, "_write_chain_memory"), \
             mock.patch.object(auto_chain, "_try_verify_update"), \
             mock.patch.object(auto_chain, "_GATES", {"_gate_qa_pass": lambda *args: (True, "ok")}), \
             mock.patch.object(auto_chain, "_BUILDERS", {"_build_gatekeeper_prompt": lambda *args: ("prompt", {})}), \
             mock.patch.object(auto_chain, "CHAIN", {"qa": ("_gate_qa_pass", "gatekeeper", "_build_gatekeeper_prompt")}):
            out = auto_chain._do_chain(
                FakeConn(),
                "aming-claw",
                "task-qa-1",
                "qa",
                {"recommendation": "qa_pass", "review_summary": "ok"},
                {},
            )

        self.assertEqual(out["task_id"], "task-existing-qa")
        self.assertTrue(out["dedup"])


class TestQaGateRound2(unittest.TestCase):
    def test_governed_dirty_workspace_lane_defers_related_node_qa_block(self):
        from governance.auto_chain import _gate_qa_pass

        class _Row(dict):
            pass

        class _Conn:
            def execute(self, query, params=()):
                task_id = params[1]

                class _Cursor:
                    def __init__(self, row):
                        self._row = row

                    def fetchone(self):
                        return self._row

                if task_id == "task-parent":
                    return _Cursor(_Row({
                        "metadata_json": (
                            '{"lane":"B","parallel_plan":"dirty-reconciliation-2026-03-30",'
                            '"allow_dirty_workspace_reconciliation":true}'
                        )
                    }))
                return _Cursor(None)

        with mock.patch("governance.auto_chain._try_verify_update"), \
             mock.patch("governance.auto_chain._check_nodes_min_status", return_value=(False, "L2.2=t2_pass")):
            passed, reason = _gate_qa_pass(
                _Conn(),
                "aming-claw",
                {"recommendation": "qa_pass", "review_summary": "ok"},
                {"parent_task_id": "task-parent", "related_nodes": ["L2.2"]},
            )

        self.assertTrue(passed, reason)

    def test_governed_dirty_workspace_lane_defers_release_gate_node_block(self):
        from governance.auto_chain import _gate_release

        class _Row(dict):
            pass

        class _Conn:
            def execute(self, query, params=()):
                task_id = params[1]

                class _Cursor:
                    def __init__(self, row):
                        self._row = row

                    def fetchone(self):
                        return self._row

                if task_id == "task-parent":
                    return _Cursor(_Row({
                        "metadata_json": (
                            '{"lane":"B","parallel_plan":"dirty-reconciliation-2026-03-30",'
                            '"allow_dirty_workspace_reconciliation":true}'
                        )
                    }))
                return _Cursor(None)

        with mock.patch("governance.auto_chain._try_verify_update"), \
             mock.patch("governance.auto_chain._check_nodes_min_status", return_value=(False, "L2.2=t2_pass")):
            passed, reason = _gate_release(
                _Conn(),
                "aming-claw",
                {"merge_commit": "abc123"},
                {"parent_task_id": "task-parent", "related_nodes": ["L2.2"]},
            )

        self.assertTrue(passed, reason)

    def test_replayed_lane_b_chain_is_inferred_for_release_gate_defer(self):
        from governance.auto_chain import _gate_release

        class _Conn:
            def execute(self, query, params=()):
                return mock.Mock(fetchone=lambda: None)

        metadata = {
            "related_nodes": ["L2.2"],
            "replay_source": "observer-host-governance-fresh-lane-b-rebuild-v3",
            "intent_summary": "Workflow improvement Lane B: Reconcile provider routing",
            "_original_prompt": "Workflow improvement Lane B: dirty-workspace reconciliation",
        }

        with mock.patch("governance.auto_chain._try_verify_update"), \
             mock.patch("governance.auto_chain._check_nodes_min_status", return_value=(False, "L2.2=t2_pass")):
            passed, reason = _gate_release(
                _Conn(),
                "aming-claw",
                {"merge_commit": "abc123"},
                metadata,
            )

        self.assertTrue(passed, reason)


if __name__ == "__main__":
    unittest.main()
