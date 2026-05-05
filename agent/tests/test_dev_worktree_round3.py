import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)

from executor_worker import ExecutorWorker


class TestDevWorktreeRound3(unittest.TestCase):
    def test_dev_session_uses_worktree_workspace(self):
        worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace=os.getcwd())

        fake_session = MagicMock()
        fake_session.pid = 123
        fake_session.status = "completed"
        fake_session.stderr = ""
        fake_session.stdout = '{"schema_version":"v1","summary":"ok","changed_files":[]}'
        fake_session.session_id = "sess-1"

        fake_lifecycle = MagicMock()
        fake_lifecycle.create_session.return_value = fake_session
        fake_lifecycle.wait_for_output.return_value = {"status": "completed", "elapsed_sec": 1.0}
        fake_lifecycle.extend_deadline = MagicMock()

        worker._lifecycle = fake_lifecycle

        task = {
            "task_id": "task-dev-1",
            "type": "dev",
            "prompt": "Implement change",
            "metadata": {"target_files": ["agent/executor_worker.py"]},
        }

        with patch.object(worker, "_create_worktree", return_value=("C:/tmp/dev-task-dev-1", "dev/task-dev-1")), \
             patch.object(worker, "_build_prompt", return_value="prompt"), \
             patch.object(worker, "_get_git_changed_files", return_value=["agent/executor_worker.py"]), \
             patch.object(worker, "_write_memory"), \
             patch("subprocess.run") as mock_run:
            result = worker._execute_task(task)

        self.assertEqual(result["status"], "succeeded")
        fake_lifecycle.create_session.assert_called_once()
        self.assertEqual(fake_lifecycle.create_session.call_args.kwargs["workspace"], "C:/tmp/dev-task-dev-1")
        self.assertEqual(result["result"]["_worktree"], "C:/tmp/dev-task-dev-1")
        self.assertEqual(result["result"]["_branch"], "dev/task-dev-1")
        mock_run.assert_called()

    def test_git_changed_files_uses_supplied_cwd(self):
        worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace="C:/repo/main")

        proc1 = MagicMock(returncode=0, stdout="agent/foo.py\n")
        proc2 = MagicMock(returncode=0, stdout="")
        proc3 = MagicMock(returncode=0, stdout="")

        with patch("subprocess.run", side_effect=[proc1, proc2, proc3]) as mock_run:
            files = worker._get_git_changed_files(cwd="C:/repo/worktree")

        self.assertEqual(files, ["agent/foo.py"])
        self.assertEqual(mock_run.call_args_list[0].kwargs["cwd"], "C:/repo/worktree")

    def test_git_changed_files_includes_untracked_new_files(self):
        """B27: untracked new files (git ls-files --others) must appear in changed_files."""
        worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace="C:/repo/main")

        proc1 = MagicMock(returncode=0, stdout="agent/existing.py\n")  # modified tracked
        proc2 = MagicMock(returncode=0, stdout="agent/staged_new.py\n")  # staged new
        proc3 = MagicMock(returncode=0, stdout="agent/untracked_new.py\n")  # untracked new

        with patch("subprocess.run", side_effect=[proc1, proc2, proc3]):
            files = worker._get_git_changed_files(cwd="C:/repo/worktree")

        self.assertIn("agent/existing.py", files)
        self.assertIn("agent/staged_new.py", files)
        self.assertIn("agent/untracked_new.py", files)
        self.assertEqual(len(files), 3)

    def test_create_worktree_uses_attempt_scoped_path_for_retry(self):
        with tempfile.TemporaryDirectory() as repo:
            worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace=repo)
            ok = MagicMock(returncode=0, stdout="", stderr="")

            with patch("subprocess.run", side_effect=[ok, ok]) as mock_run:
                worktree_path, branch_name = worker._create_worktree("task-abc", attempt_num=2)

            self.assertEqual(branch_name, "dev/task-abc-attempt-2")
            self.assertEqual(
                worktree_path,
                os.path.join(repo, ".worktrees", "dev-task-abc-attempt-2"),
            )
            add_cmd = mock_run.call_args_list[1].args[0]
            self.assertEqual(add_cmd[:5], ["git", "worktree", "add", "-b", "dev/task-abc-attempt-2"])
            self.assertEqual(add_cmd[5], worktree_path)

    def test_create_worktree_keeps_first_attempt_names(self):
        with tempfile.TemporaryDirectory() as repo:
            worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace=repo)
            ok = MagicMock(returncode=0, stdout="", stderr="")

            with patch("subprocess.run", side_effect=[ok, ok]):
                worktree_path, branch_name = worker._create_worktree("task-abc", attempt_num=1)

            self.assertEqual(branch_name, "dev/task-abc")
            self.assertEqual(worktree_path, os.path.join(repo, ".worktrees", "dev-task-abc"))


if __name__ == "__main__":
    unittest.main()
