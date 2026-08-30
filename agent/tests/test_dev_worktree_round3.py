import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)

from executor_worker import ExecutorWorker


def _ac_worker(workspace):
    workspace = os.path.realpath(workspace)
    storage = os.path.realpath(os.path.join(workspace, "dev-storage"))
    os.makedirs(storage, exist_ok=True)
    with patch.dict(os.environ, {"AMING_CLAW_DEV_STORAGE_ROOT": storage}):
        return ExecutorWorker("aming-claw", governance_url="http://127.0.0.1:40008", workspace=workspace)


class TestDevWorktreeRound3(unittest.TestCase):
    def test_dev_session_uses_worktree_workspace(self):
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory(dir=repo) as worktree:
            worker = _ac_worker(repo)
            worktree = os.path.realpath(worktree)
            worker._register_task_worktree("task-dev-1", worktree)
            fake_session = MagicMock(pid=123, status="completed", stderr="", session_id="sess-1")
            fake_session.stdout = '{"schema_version":"v1","summary":"ok","changed_files":[]}'
            fake_lifecycle = MagicMock()
            fake_lifecycle.create_session.return_value = fake_session
            fake_lifecycle.wait_for_output.return_value = {"status": "completed", "elapsed_sec": 1.0}
            fake_lifecycle.extend_deadline = MagicMock()
            worker._lifecycle = fake_lifecycle
            task = {
                "task_id": "task-dev-1", "type": "dev", "prompt": "Implement change",
                "metadata": {"target_files": ["agent/executor_worker.py"]},
            }
            with patch.object(worker, "_create_worktree", return_value=(worktree, "dev/task-dev-1")), \
                 patch.object(worker, "_build_prompt", return_value="prompt"), \
                 patch.object(worker, "_get_git_changed_files", return_value=["agent/executor_worker.py"]), \
                 patch.object(worker, "_write_memory"), patch("subprocess.run") as mock_run:
                result = worker._execute_task(task)

            self.assertEqual(result["status"], "succeeded")
            fake_lifecycle.create_session.assert_called_once()
            self.assertEqual(fake_lifecycle.create_session.call_args.kwargs["workspace"], worktree)
            self.assertEqual(result["result"]["_worktree"], worktree)
            self.assertEqual(result["result"]["_branch"], "dev/task-dev-1")
            mock_run.assert_called()

    def test_git_changed_files_uses_supplied_cwd(self):
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory(dir=repo) as worktree:
            worker = _ac_worker(repo)
            proc1 = MagicMock(returncode=0, stdout="agent/foo.py\n")
            proc2 = MagicMock(returncode=0, stdout="")
            proc3 = MagicMock(returncode=0, stdout="")
            with patch("subprocess.run", side_effect=[proc1, proc2, proc3]) as mock_run:
                files = worker._get_git_changed_files(cwd=worktree)

            self.assertEqual(files, ["agent/foo.py"])
            self.assertEqual(mock_run.call_args_list[0].kwargs["cwd"], worktree)

    def test_git_changed_files_includes_untracked_new_files(self):
        """B27: untracked new files (git ls-files --others) must appear in changed_files."""
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory(dir=repo) as worktree:
            worker = _ac_worker(repo)

            proc1 = MagicMock(returncode=0, stdout="agent/existing.py\n")  # modified tracked
            proc2 = MagicMock(returncode=0, stdout="agent/staged_new.py\n")  # staged new
            proc3 = MagicMock(returncode=0, stdout="agent/untracked_new.py\n")  # untracked new

            with patch("subprocess.run", side_effect=[proc1, proc2, proc3]):
                files = worker._get_git_changed_files(cwd=worktree)

            self.assertIn("agent/existing.py", files)
            self.assertIn("agent/staged_new.py", files)
            self.assertIn("agent/untracked_new.py", files)
            self.assertEqual(len(files), 3)

    def test_create_worktree_uses_attempt_scoped_path_for_retry(self):
        with tempfile.TemporaryDirectory() as repo:
            worker = _ac_worker(repo)
            ok = MagicMock(returncode=0, stdout="", stderr="")

            def _run(cmd, **kwargs):
                if cmd[:3] == ["git", "worktree", "add"]:
                    os.makedirs(cmd[-2], exist_ok=True)
                return ok
            with patch("subprocess.run", side_effect=_run) as mock_run:
                worktree_path, branch_name = worker._create_worktree("task-abc", attempt_num=2)

            self.assertEqual(branch_name, "dev/task-abc-attempt-2")
            self.assertEqual(
                worktree_path,
                os.path.realpath(os.path.join(repo, ".worktrees", "dev-task-abc-attempt-2")),
            )
            add_cmd = mock_run.call_args_list[1].args[0]
            self.assertEqual(add_cmd[:5], ["git", "worktree", "add", "-b", "dev/task-abc-attempt-2"])
            self.assertEqual(os.path.realpath(add_cmd[5]), worktree_path)

    def test_create_worktree_keeps_first_attempt_names(self):
        with tempfile.TemporaryDirectory() as repo:
            worker = _ac_worker(repo)
            ok = MagicMock(returncode=0, stdout="", stderr="")

            def _run(cmd, **kwargs):
                if cmd[:3] == ["git", "worktree", "add"]:
                    os.makedirs(cmd[-2], exist_ok=True)
                return ok
            with patch("subprocess.run", side_effect=_run):
                worktree_path, branch_name = worker._create_worktree("task-abc", attempt_num=1)

            self.assertEqual(branch_name, "dev/task-abc")
            self.assertEqual(worktree_path, os.path.realpath(os.path.join(repo, ".worktrees", "dev-task-abc")))


if __name__ == "__main__":
    unittest.main()
