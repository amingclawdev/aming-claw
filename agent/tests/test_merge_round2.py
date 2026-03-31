import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


class TestMergeRound2(unittest.TestCase):
    def test_merge_prompt_preserves_worktree_chain_metadata(self):
        from governance.auto_chain import _build_merge_prompt

        prompt, out_meta = _build_merge_prompt(
            "task-gatekeeper-1",
            {"_worktree": "C:/repo/.worktrees/dev-task-1", "_branch": "dev/task-1"},
            {"changed_files": ["agent/foo.py"]},
        )

        self.assertIn("task-gatekeeper-1", prompt)
        self.assertEqual(out_meta["_worktree"], "C:/repo/.worktrees/dev-task-1")
        self.assertEqual(out_meta["_branch"], "dev/task-1")

    def test_branch_merge_uses_isolated_integration_worktree(self):
        from executor_worker import ExecutorWorker

        worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace="C:/repo")
        metadata = {
            "changed_files": ["agent/foo.py"],
            "_branch": "dev/task-1",
            "_worktree": "C:/repo/.worktrees/dev-task-1",
        }

        run_calls = []

        def fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None):
            run_calls.append((cmd, cwd))
            if cmd[:3] == ["git", "diff", "--cached"]:
                return SimpleNamespace(returncode=0, stdout="agent/foo.py\n", stderr="")
            if cmd[:2] == ["git", "rev-parse"]:
                return SimpleNamespace(returncode=0, stdout="mergehash123\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("executor_worker.os.path.isdir", return_value=True), \
             mock.patch("executor_worker.subprocess.run", side_effect=fake_run), \
             mock.patch.object(worker, "_create_integration_worktree", return_value=("C:/repo/.worktrees/merge-task-1", "merge/task-1", "")), \
             mock.patch.object(worker, "_remove_worktree") as remove_worktree, \
             mock.patch.object(worker, "_report_progress"):
            out = worker._execute_merge("task-1", metadata)

        self.assertEqual(out["status"], "succeeded")
        self.assertEqual(out["result"]["merge_mode"], "isolated_integration")
        self.assertEqual(out["result"]["merge_commit"], "mergehash123")
        self.assertIn((["git", "merge", "dev/task-1", "--no-ff", "-m", "Auto-merge: task-1"], "C:/repo/.worktrees/merge-task-1"), run_calls)
        remove_worktree.assert_any_call("C:/repo/.worktrees/dev-task-1", "dev/task-1", delete_branch=False)
        remove_worktree.assert_any_call("C:/repo/.worktrees/merge-task-1", "merge/task-1")

    def test_branch_merge_requires_existing_dev_worktree(self):
        from executor_worker import ExecutorWorker

        worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace="C:/repo")
        metadata = {
            "changed_files": ["agent/foo.py"],
            "_branch": "dev/task-1",
            "_worktree": "C:/repo/.worktrees/dev-task-1",
        }

        with mock.patch("executor_worker.os.path.isdir", return_value=False), \
             mock.patch.object(worker, "_report_progress"):
            out = worker._execute_merge("task-1", metadata)

        self.assertEqual(out["status"], "failed")
        self.assertIn("Merge branch missing", out["error"])

    def test_branch_merge_replay_succeeds_when_branch_already_merged(self):
        from executor_worker import ExecutorWorker

        worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace="C:/repo")
        metadata = {
            "changed_files": ["agent/foo.py"],
            "_branch": "dev/task-1",
            "_worktree": "C:/repo/.worktrees/dev-task-1",
        }

        with mock.patch("executor_worker.os.path.isdir", return_value=False), \
             mock.patch.object(worker, "_branch_exists", return_value=True), \
             mock.patch.object(worker, "_branch_already_merged", return_value=True), \
             mock.patch("executor_worker.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="head123\n", stderr="")), \
             mock.patch.object(worker, "_report_progress"):
            out = worker._execute_merge("task-1", metadata)

        self.assertEqual(out["status"], "succeeded")
        self.assertEqual(out["result"]["merge_mode"], "already_merged_replay")
        self.assertEqual(out["result"]["merge_commit"], "head123")


if __name__ == "__main__":
    unittest.main()
