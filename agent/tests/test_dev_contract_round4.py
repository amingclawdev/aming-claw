import os
import sys
import unittest
from unittest import mock

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


class TestBuildDevPromptRound4(unittest.TestCase):
    def test_verification_is_forwarded_into_dev_prompt(self):
        from governance.auto_chain import _build_dev_prompt

        result = {
            "target_files": ["agent/foo.py"],
            "requirements": ["R1"],
            "acceptance_criteria": ["AC1"],
            "test_files": ["agent/tests/test_foo.py"],
            "doc_impact": {"files": [], "changes": ["No doc update required."]},
            "skip_reasons": {"doc_impact": "Lane C owns docs"},
            "verification": {
                "method": "automated test",
                "command": "pytest agent/tests/test_foo.py -q",
            },
        }

        prompt, out_meta = _build_dev_prompt("task-pm-1", result, {})

        self.assertIn('verification: {"method": "automated test", "command": "pytest agent/tests/test_foo.py -q"}', prompt)
        self.assertEqual(out_meta["verification"]["command"], "pytest agent/tests/test_foo.py -q")
        self.assertEqual(out_meta["test_files"], ["agent/tests/test_foo.py"])
        self.assertEqual(out_meta["doc_impact"]["files"], [])
        self.assertEqual(out_meta["skip_reasons"]["doc_impact"], "Lane C owns docs")
        self.assertEqual(out_meta["related_nodes"], [])

    def test_retry_prompt_rebuilds_dev_contract_from_metadata(self):
        from governance import auto_chain
        import governance.task_registry as task_registry

        metadata = {
            "parent_task_id": "task-parent",
            "target_files": ["agent/foo.py"],
            "requirements": ["R1"],
            "acceptance_criteria": ["AC1"],
            "verification": {
                "method": "automated test",
                "command": "pytest agent/tests/test_foo.py -q",
            },
            "_original_prompt": "stale original prompt without verification",
            "_gate_retry_count": 0,
            "chain_depth": 0,
        }

        class DummyTaskRegistry:
            last_prompt = ""

            @staticmethod
            def create_task(conn, project_id, prompt, task_type, created_by, metadata, **kwargs):
                DummyTaskRegistry.last_prompt = prompt
                return {"task_id": "task-retry-1", "prompt": prompt, "metadata": metadata}

        with mock.patch.object(task_registry, "create_task", DummyTaskRegistry.create_task), \
             mock.patch.object(auto_chain, "_GATES", {"_gate_checkpoint": lambda *args: (False, "Dev tests failed: 1 failures")}), \
             mock.patch.object(auto_chain, "_BUILDERS", {"test_builder": lambda *args: ("", {})}), \
             mock.patch.object(auto_chain, "CHAIN", {"dev": ("_gate_checkpoint", "test", "test_builder")}), \
             mock.patch.object(auto_chain, "_publish_event"), \
             mock.patch.object(auto_chain, "_write_chain_memory"), \
             mock.patch.object(auto_chain, "_try_verify_update"):
            result = {"changed_files": ["agent/foo.py"], "test_results": {"ran": True, "failed": 1}}
            out = auto_chain._do_chain(object(), "aming-claw", "task-dev-1", "dev", result, metadata)

        self.assertEqual(out["retry_task_id"], "task-retry-1")
        retry_prompt = DummyTaskRegistry.last_prompt
        self.assertIn("Use the same Dev contract below", retry_prompt)
        self.assertIn("verification:", retry_prompt)
        self.assertIn("pytest agent/tests/test_foo.py -q", retry_prompt)

    def test_retry_prompt_rewrites_stale_doc_gate_for_lane_b(self):
        from governance import auto_chain
        import governance.task_registry as task_registry

        metadata = {
            "parent_task_id": "task-parent-dev",
            "target_files": ["agent/service_manager.py"],
            "requirements": ["R1"],
            "acceptance_criteria": ["AC1"],
            "verification": {"command": "pytest agent/tests/test_service_manager.py -q"},
            "_gate_retry_count": 0,
            "chain_depth": 0,
        }

        class DummyTaskRegistry:
            last_prompt = ""
            last_metadata = {}

            @staticmethod
            def create_task(conn, project_id, prompt, task_type, created_by, metadata, **kwargs):
                DummyTaskRegistry.last_prompt = prompt
                DummyTaskRegistry.last_metadata = metadata
                return {"task_id": "task-retry-2", "prompt": prompt, "metadata": metadata}

        class _Row(dict):
            pass

        class _Conn:
            def execute(self, query, params):
                task_id = params[1]

                class _Cursor:
                    def __init__(self, row):
                        self._row = row

                    def fetchone(self):
                        return self._row

                if task_id == "task-parent-dev":
                    return _Cursor(_Row({
                        "metadata_json": '{"parent_task_id":"task-root-lane-b"}',
                    }))
                if task_id == "task-root-lane-b":
                    return _Cursor(_Row({
                        "metadata_json": (
                            '{"lane":"B","parallel_plan":"dirty-reconciliation-2026-03-30",'
                            '"allow_dirty_workspace_reconciliation":true}'
                        ),
                    }))
                return _Cursor(None)

        with mock.patch.object(task_registry, "create_task", DummyTaskRegistry.create_task), \
             mock.patch.object(auto_chain, "_GATES", {"_gate_checkpoint": lambda *args: (False, "Related docs not updated: [\'README.md\']. Add them to changed_files.")}), \
             mock.patch.object(auto_chain, "_BUILDERS", {"test_builder": lambda *args: ("", {})}), \
             mock.patch.object(auto_chain, "CHAIN", {"dev": ("_gate_checkpoint", "test", "test_builder")}), \
             mock.patch.object(auto_chain, "_publish_event"), \
             mock.patch.object(auto_chain, "_write_chain_memory"), \
             mock.patch.object(auto_chain, "_try_verify_update"):
            result = {"changed_files": ["agent/service_manager.py"], "test_results": {"ran": True, "failed": 0}}
            out = auto_chain._do_chain(_Conn(), "aming-claw", "task-dev-2", "dev", result, metadata)

        self.assertEqual(out["retry_task_id"], "task-retry-2")
        self.assertIn("Lane C owns documentation updates", DummyTaskRegistry.last_prompt)
        self.assertIn("Do NOT modify README.md or docs/", DummyTaskRegistry.last_prompt)
        self.assertEqual(
            DummyTaskRegistry.last_metadata["previous_gate_reason"],
            "Lane C owns documentation updates for this governed dirty-workspace "
            "reconciliation. Do NOT modify README.md or docs/. Retry as a code-only "
            "fix within target_files and keep changed_files limited to target_files.",
        )


class TestExecutorDevPromptRound4(unittest.TestCase):
    def test_executor_dev_prompt_includes_required_verification(self):
        from executor_worker import ExecutorWorker

        worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace=os.getcwd())
        prompt = worker._build_prompt(
            "Implement fix",
            "dev",
            {
                "target_files": ["agent/foo.py"],
                "verification": {
                    "method": "automated test",
                    "command": "pytest agent/tests/test_foo.py -q",
                },
            },
        )

        self.assertIn("Required verification command: pytest agent/tests/test_foo.py -q", prompt)
        self.assertIn('"test_results":{"ran":true,"passed":N,"failed":N,"command":"exact command attempted"}', prompt)

    def test_executor_treats_reached_max_turns_as_failure(self):
        from executor_worker import ExecutorWorker

        class FakeSession:
            def __init__(self):
                self.session_id = "ai-dev-test"
                self.pid = 0
                self.status = "running"
                self.stdout = ""
                self.stderr = ""
                self.exit_code = 0

        class FakeLifecycle:
            def __init__(self):
                self.session = FakeSession()

            def create_session(self, **kwargs):
                return self.session

            def wait_for_output(self, session_id):
                self.session.status = "completed"
                self.session.stdout = "Error: Reached max turns (40)"
                return {
                    "status": "completed",
                    "stdout": self.session.stdout,
                    "stderr": "",
                    "exit_code": 0,
                    "elapsed_sec": 12.3,
                }

        worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace=os.getcwd())
        worker._lifecycle = FakeLifecycle()

        with mock.patch.object(worker, "_create_worktree", return_value=(None, None)), \
             mock.patch.object(worker, "_build_prompt", return_value="Implement fix"), \
             mock.patch.object(worker, "_report_progress"), \
             mock.patch.object(worker, "_write_memory"):
            outcome = worker._execute_task({
                "task_id": "task-dev-max-turns",
                "type": "dev",
                "prompt": "Implement fix",
                "metadata": {"target_files": ["agent/foo.py"]},
            })

        self.assertEqual(outcome["status"], "failed")
        self.assertIn("Reached max turns", outcome["error"])


class TestCheckpointGateRound5(unittest.TestCase):
    def test_doc_impact_empty_files_skips_inferred_doc_block(self):
        from governance.auto_chain import _gate_checkpoint

        passed, reason = _gate_checkpoint(
            None,
            "aming-claw",
            {"changed_files": ["agent/executor_worker.py"], "test_results": {"ran": True, "failed": 0}},
            {
                "target_files": ["agent/executor_worker.py"],
                "doc_impact": {"files": [], "changes": ["No doc update required for first pass."]},
            },
        )

        self.assertTrue(passed, reason)

    def test_related_nodes_dicts_are_dropped_from_gate_metadata(self):
        from governance.auto_chain import _build_dev_prompt

        result = {
            "target_files": ["agent/foo.py"],
            "requirements": ["R1"],
            "acceptance_criteria": ["AC1"],
            "verification": {"command": "pytest agent/tests/test_foo.py -q"},
        }
        metadata = {
            "related_nodes": [{"title": "planned node but not existing id"}, "L4.41"],
        }

        _, out_meta = _build_dev_prompt("task-pm-2", result, metadata)
        self.assertEqual(out_meta["related_nodes"], ["L4.41"])

    def test_checkpoint_gate_does_not_require_governance_git_or_node_alignment(self):
        from governance.auto_chain import _gate_checkpoint

        passed, reason = _gate_checkpoint(
            object(),
            "aming-claw",
            {
                "changed_files": ["agent/executor_worker.py"],
                "test_results": {"ran": True, "failed": 0},
            },
            {
                "target_files": ["agent/executor_worker.py"],
                "doc_impact": {"files": [], "changes": ["No doc update required for first pass."]},
                "related_nodes": ["L4.12", "L4.41"],
            },
        )

        self.assertTrue(passed, reason)

    def test_checkpoint_gate_allows_explicit_test_files(self):
        from governance.auto_chain import _gate_checkpoint

        passed, reason = _gate_checkpoint(
            object(),
            "aming-claw",
            {
                "changed_files": [
                    "agent/service_manager.py",
                    "agent/tests/test_service_manager.py",
                ],
                "test_results": {"ran": True, "failed": 0},
            },
            {
                "target_files": ["agent/service_manager.py"],
                "test_files": ["agent/tests/test_service_manager.py"],
                "doc_impact": {"files": [], "changes": []},
            },
        )

        self.assertTrue(passed, reason)

    def test_checkpoint_gate_allows_test_file_from_verification_command(self):
        from governance.auto_chain import _gate_checkpoint

        passed, reason = _gate_checkpoint(
            object(),
            "aming-claw",
            {
                "changed_files": [
                    "agent/governance/enums.py",
                    "agent/tests/test_governance_enums.py",
                ],
                "test_results": {"ran": True, "failed": 0},
            },
            {
                "target_files": ["agent/governance/enums.py"],
                "verification": {
                    "command": "pytest agent/tests/test_governance_enums.py -q",
                },
                "doc_impact": {"files": [], "changes": []},
            },
        )

        self.assertTrue(passed, reason)

    def test_checkpoint_gate_defers_docs_for_governed_lane_b_chain(self):
        from governance.auto_chain import _gate_checkpoint

        class _Row(dict):
            pass

        class _Conn:
            def execute(self, query, params):
                task_id = params[1]

                class _Cursor:
                    def __init__(self, row):
                        self._row = row

                    def fetchone(self):
                        return self._row

                if task_id == "task-parent-dev":
                    return _Cursor(_Row({
                        "metadata_json": '{"parent_task_id":"task-root-lane-b"}',
                    }))
                if task_id == "task-root-lane-b":
                    return _Cursor(_Row({
                        "metadata_json": (
                            '{"lane":"B","parallel_plan":"dirty-reconciliation-2026-03-30",'
                            '"allow_dirty_workspace_reconciliation":true}'
                        ),
                    }))
                return _Cursor(None)

        with mock.patch("governance.impact_analyzer.get_related_docs", return_value={"README.md"}):
            passed, reason = _gate_checkpoint(
                _Conn(),
                "aming-claw",
                {
                    "changed_files": ["agent/service_manager.py"],
                    "test_results": {"ran": True, "failed": 0},
                },
                {
                    "parent_task_id": "task-parent-dev",
                    "target_files": ["agent/service_manager.py"],
                },
            )

        self.assertTrue(passed, reason)
        self.assertIn("Lane C", reason)


if __name__ == "__main__":
    unittest.main()
