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
        """Test tasks run as scripts (6a/6b) using inherited worktree."""
        worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace=os.getcwd())

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

        fake_proc = MagicMock()
        fake_proc.stdout = "1 passed in 0.5s"
        fake_proc.stderr = ""
        fake_proc.returncode = 0

        with patch("subprocess.run", return_value=fake_proc) as mock_run, \
             patch("os.path.isfile", return_value=True):
            result = worker._execute_task(task)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["_worktree"], os.getcwd())
        self.assertEqual(result["result"]["_branch"], "dev/task-dev-1")
        self.assertEqual(result["result"]["changed_files"], ["agent/foo.py"])
        mock_run.assert_called_once()
        # Verify subprocess was called with correct cwd (inherited worktree)
        call_kwargs = mock_run.call_args
        self.assertEqual(call_kwargs.kwargs.get("cwd"), os.getcwd())

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
