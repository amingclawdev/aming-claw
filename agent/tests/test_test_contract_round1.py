import os
import sys
import unittest
from unittest.mock import MagicMock, patch

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)

from executor_worker import ExecutorWorker


class TestBuildTestPromptRound1(unittest.TestCase):
    def test_test_prompt_forwards_verification_and_test_files(self):
        from governance.auto_chain import _build_test_prompt

        result = {"changed_files": ["agent/foo.py"]}
        metadata = {
            "verification": {
                "method": "automated test",
                "command": "pytest agent/tests/test_foo.py -q",
            },
            "test_files": ["agent/tests/test_foo.py"],
        }

        prompt, out_meta = _build_test_prompt("task-dev-1", result, metadata)

        self.assertIn('verification: {"method": "automated test", "command": "pytest agent/tests/test_foo.py -q"}', prompt)
        self.assertIn('test_files: ["agent/tests/test_foo.py"]', prompt)
        self.assertEqual(out_meta["verification"]["command"], "pytest agent/tests/test_foo.py -q")
        self.assertEqual(out_meta["test_files"], ["agent/tests/test_foo.py"])


class TestTesterExecutionRound1(unittest.TestCase):
    def test_test_session_reuses_inherited_worktree(self):
        worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace=os.getcwd())

        fake_session = MagicMock()
        fake_session.pid = 123
        fake_session.status = "completed"
        fake_session.stderr = ""
        fake_session.stdout = '{"schema_version":"v1","summary":"ok","test_report":{"passed":1,"failed":0,"tool":"pytest","command":"pytest agent/tests/test_foo.py -q"}}'
        fake_session.session_id = "sess-test-1"

        fake_lifecycle = MagicMock()
        fake_lifecycle.create_session.return_value = fake_session
        fake_lifecycle.wait_for_output.return_value = {"status": "completed", "elapsed_sec": 1.0}
        fake_lifecycle.extend_deadline = MagicMock()
        worker._lifecycle = fake_lifecycle

        task = {
            "task_id": "task-test-1",
            "type": "test",
            "prompt": "Run tests",
            "metadata": {
                "changed_files": ["agent/foo.py"],
                "verification": {"command": "pytest agent/tests/test_foo.py -q"},
                "test_files": ["agent/tests/test_foo.py"],
                "_worktree": os.getcwd(),
                "_branch": "dev/task-dev-1",
            },
        }

        with patch.object(worker, "_build_prompt", wraps=worker._build_prompt), \
             patch.object(worker, "_get_git_changed_files", return_value=["agent/foo.py"]), \
             patch.object(worker, "_write_memory"), \
             patch("subprocess.run") as mock_run:
            result = worker._execute_task(task)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(fake_lifecycle.create_session.call_args.kwargs["workspace"], os.getcwd())
        self.assertEqual(result["result"]["_worktree"], os.getcwd())
        self.assertEqual(result["result"]["_branch"], "dev/task-dev-1")
        self.assertEqual(result["result"]["changed_files"], ["agent/foo.py"])
        mock_run.assert_called()

    def test_tester_prompt_includes_required_verification_command(self):
        worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace=os.getcwd())

        prompt = worker._build_prompt(
            "Run tests",
            "test",
            {
                "changed_files": ["agent/foo.py"],
                "verification": {
                    "method": "automated test",
                    "command": "pytest agent/tests/test_foo.py -q",
                },
                "test_files": ["agent/tests/test_foo.py"],
            },
        )

        self.assertIn("Required verification command: pytest agent/tests/test_foo.py -q", prompt)
        self.assertIn('Priority test files: ["agent/tests/test_foo.py"]', prompt)
        self.assertIn('"test_report":{"passed":N,"failed":N,"tool":"pytest","command":"exact command attempted"}', prompt)


class TestTestGateRound1(unittest.TestCase):
    def test_t2_gate_blocks_missing_test_report(self):
        from governance.auto_chain import _gate_t2_pass

        passed, reason = _gate_t2_pass(object(), "aming-claw", {"summary": "Error: Reached max turns (10)"}, {})

        self.assertFalse(passed)
        self.assertIn("missing required test_report", reason)


if __name__ == "__main__":
    unittest.main()
