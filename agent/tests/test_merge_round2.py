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
             mock.patch.object(worker, "_report_progress"), \
             mock.patch.object(worker, "_api", return_value={"chain_version": "old123"}):
            out = worker._execute_merge("task-1", metadata)

        self.assertEqual(out["status"], "succeeded")
        self.assertEqual(out["result"]["merge_mode"], "isolated_integration")
        self.assertEqual(out["result"]["merge_commit"], "mergehash123")
        self.assertIn((["git", "merge", "dev/task-1", "--no-ff", "-m", "Auto-merge: task-1"], "C:/repo/.worktrees/merge-task-1"), run_calls)
        # Verify ff-only advance of main workspace
        self.assertIn((["git", "merge", "--ff-only", "mergehash123"], "C:/repo"), run_calls)
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


    def test_isolated_merge_ff_only_call_present(self):
        """Regression: verify ff-only subprocess call is present in run_calls."""
        from executor_worker import ExecutorWorker

        worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace="C:/repo")
        metadata = {
            "changed_files": ["agent/bar.py"],
            "_branch": "dev/task-2",
            "_worktree": "C:/repo/.worktrees/dev-task-2",
        }

        run_calls = []

        def fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None):
            run_calls.append((cmd, cwd))
            if cmd[:3] == ["git", "diff", "--cached"]:
                return SimpleNamespace(returncode=0, stdout="agent/bar.py\n", stderr="")
            if cmd[:2] == ["git", "rev-parse"]:
                return SimpleNamespace(returncode=0, stdout="abc456\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("executor_worker.os.path.isdir", return_value=True), \
             mock.patch("executor_worker.subprocess.run", side_effect=fake_run), \
             mock.patch.object(worker, "_create_integration_worktree", return_value=("C:/repo/.worktrees/merge-task-2", "merge/task-2", "")), \
             mock.patch.object(worker, "_remove_worktree"), \
             mock.patch.object(worker, "_report_progress"), \
             mock.patch.object(worker, "_api", return_value={"chain_version": "old456"}):
            out = worker._execute_merge("task-2", metadata)

        self.assertEqual(out["status"], "succeeded")
        # The ff-only call must be present with cwd=self.workspace
        ff_only_calls = [(cmd, cwd) for cmd, cwd in run_calls
                         if cmd[:3] == ["git", "merge", "--ff-only"]]
        self.assertEqual(len(ff_only_calls), 1, "Expected exactly one ff-only call")
        self.assertEqual(ff_only_calls[0][0], ["git", "merge", "--ff-only", "abc456"])
        self.assertEqual(ff_only_calls[0][1], "C:/repo")

    def test_isolated_merge_ff_only_failure_returns_failed(self):
        """Regression: simulate ff-only failure returns failed status."""
        from executor_worker import ExecutorWorker

        worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace="C:/repo")
        metadata = {
            "changed_files": ["agent/baz.py"],
            "_branch": "dev/task-3",
            "_worktree": "C:/repo/.worktrees/dev-task-3",
        }

        def fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None):
            if cmd[:3] == ["git", "diff", "--cached"]:
                return SimpleNamespace(returncode=0, stdout="agent/baz.py\n", stderr="")
            if cmd[:2] == ["git", "rev-parse"]:
                return SimpleNamespace(returncode=0, stdout="def789\n", stderr="")
            # Simulate ff-only failure
            if cmd[:3] == ["git", "merge", "--ff-only"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="fatal: Not possible to fast-forward")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("executor_worker.os.path.isdir", return_value=True), \
             mock.patch("executor_worker.subprocess.run", side_effect=fake_run), \
             mock.patch.object(worker, "_create_integration_worktree", return_value=("C:/repo/.worktrees/merge-task-3", "merge/task-3", "")), \
             mock.patch.object(worker, "_remove_worktree"), \
             mock.patch.object(worker, "_report_progress"), \
             mock.patch.object(worker, "_api", return_value={"chain_version": "old789"}):
            out = worker._execute_merge("task-3", metadata)

        self.assertEqual(out["status"], "failed")
        self.assertIn("ff-only", out["error"])

    def test_isolated_merge_version_sync_called(self):
        """Verify version sync to governance DB happens in isolated path."""
        from executor_worker import ExecutorWorker

        worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace="C:/repo")
        metadata = {
            "changed_files": ["agent/qux.py"],
            "_branch": "dev/task-4",
            "_worktree": "C:/repo/.worktrees/dev-task-4",
        }

        api_calls = []
        def fake_api(method, path, data=None):
            api_calls.append((method, path, data))
            return {"chain_version": "oldver"}

        def fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None):
            if cmd[:3] == ["git", "diff", "--cached"]:
                return SimpleNamespace(returncode=0, stdout="agent/qux.py\n", stderr="")
            if cmd[:2] == ["git", "rev-parse"]:
                return SimpleNamespace(returncode=0, stdout="synctest123\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("executor_worker.os.path.isdir", return_value=True), \
             mock.patch("executor_worker.subprocess.run", side_effect=fake_run), \
             mock.patch.object(worker, "_create_integration_worktree", return_value=("C:/repo/.worktrees/merge-task-4", "merge/task-4", "")), \
             mock.patch.object(worker, "_remove_worktree"), \
             mock.patch.object(worker, "_report_progress"), \
             mock.patch.object(worker, "_api", side_effect=fake_api):
            out = worker._execute_merge("task-4", metadata)

        self.assertEqual(out["status"], "succeeded")
        # Verify version-sync was called
        sync_calls = [(m, p) for m, p, _ in api_calls if "version-sync" in p]
        self.assertTrue(len(sync_calls) >= 1, "version-sync API should be called")
        # Verify version-update was called
        update_calls = [(m, p, d) for m, p, d in api_calls if "version-update" in p]
        self.assertTrue(len(update_calls) >= 1, "version-update API should be called")
        self.assertEqual(update_calls[0][2]["chain_version"], "synctest123")


if __name__ == "__main__":
    unittest.main()
