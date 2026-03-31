import os
import sys
import unittest
from unittest import mock

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


class TestIsDevNote(unittest.TestCase):
    def test_docs_dev_is_dev_note(self):
        from governance.auto_chain import _is_dev_note

        self.assertTrue(_is_dev_note("docs/dev/pm-iteration.md"))
        self.assertTrue(_is_dev_note("docs/dev/some-subfolder/notes.md"))

    def test_formal_docs_are_not_dev_notes(self):
        from governance.auto_chain import _is_dev_note

        self.assertFalse(_is_dev_note("docs/ai-agent-integration-guide.md"))
        self.assertFalse(_is_dev_note("docs/human-intervention-guide.md"))
        self.assertFalse(_is_dev_note("docs/p0-3-design.md"))

    def test_backslash_normalized(self):
        from governance.auto_chain import _is_dev_note

        self.assertTrue(_is_dev_note("docs\\dev\\notes.md"))


class TestDocGateDevNoteExclusion(unittest.TestCase):
    def test_dev_note_in_doc_impact_does_not_block_gate(self):
        from governance.auto_chain import _gate_checkpoint

        result = {"changed_files": ["agent/foo.py"]}
        metadata = {
            "target_files": ["agent/foo.py"],
            "doc_impact": {"files": ["docs/dev/my-notes.md"], "changes": ["Updated dev notes"]},
        }

        with mock.patch("governance.auto_chain._try_verify_update"):
            passed, reason = _gate_checkpoint(None, "test-project", result, metadata)

        self.assertTrue(passed, reason)

    def test_formal_doc_in_doc_impact_still_enforced(self):
        from governance.auto_chain import _gate_checkpoint

        result = {"changed_files": ["agent/foo.py"]}
        metadata = {
            "target_files": ["agent/foo.py"],
            "doc_impact": {"files": ["docs/ai-agent-integration-guide.md"], "changes": ["Updated guide"]},
        }

        with mock.patch("governance.auto_chain._try_verify_update"):
            passed, reason = _gate_checkpoint(None, "test-project", result, metadata)

        self.assertFalse(passed)
        self.assertIn("docs/ai-agent-integration-guide.md", reason)

    def test_mixed_dev_and_formal_docs_only_enforces_formal(self):
        from governance.auto_chain import _gate_checkpoint

        result = {"changed_files": ["agent/foo.py", "docs/p0-3-design.md"]}
        metadata = {
            "target_files": ["agent/foo.py", "docs/p0-3-design.md"],
            "doc_impact": {
                "files": ["docs/dev/scratch.md", "docs/p0-3-design.md"],
                "changes": ["Updated design doc and dev notes"],
            },
        }

        with mock.patch("governance.auto_chain._try_verify_update"):
            passed, reason = _gate_checkpoint(None, "test-project", result, metadata)

        # p0-3-design.md is in changed_files so it passes; docs/dev/scratch.md excluded
        self.assertTrue(passed, reason)


class TestChainedMergeFailClosed(unittest.TestCase):
    def test_chained_merge_without_branch_fails_closed(self):
        from executor_worker import ExecutorWorker

        worker = ExecutorWorker("test-proj", governance_url="http://localhost:40000", workspace="C:/repo")
        metadata = {
            "parent_task_id": "task-dev-123",
            "changed_files": ["agent/foo.py"],
            # No _branch or _worktree — missing isolated metadata
        }

        with mock.patch.object(worker, "_report_progress"):
            out = worker._execute_merge("task-merge-1", metadata)

        self.assertEqual(out["status"], "failed")
        self.assertIn("no isolated merge metadata", out["error"])

    def test_non_chained_merge_without_branch_does_not_fail_closed(self):
        """Non-chained (no parent_task_id) merge doesn't hit the fail-closed guard."""
        from executor_worker import ExecutorWorker
        from types import SimpleNamespace

        worker = ExecutorWorker("test-proj", governance_url="http://localhost:40000", workspace="C:/repo")
        metadata = {"changed_files": ["agent/foo.py"]}

        def fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None):
            if "diff" in cmd:
                return SimpleNamespace(returncode=0, stdout="agent/foo.py\n", stderr="")
            if "rev-parse" in cmd:
                return SimpleNamespace(returncode=0, stdout="abc123\n", stderr="")
            if "check_output" in str(cmd):
                return SimpleNamespace(returncode=0, stdout=b"abc123\n", stderr=b"")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("executor_worker.subprocess.run", side_effect=fake_run), \
             mock.patch("executor_worker.subprocess.check_output", return_value=b"abc123\n"), \
             mock.patch.object(worker, "_report_progress"), \
             mock.patch.object(worker, "_api"):
            out = worker._execute_merge("task-merge-2", metadata)

        # Should not be the fail-closed error
        self.assertNotIn("no isolated merge metadata", out.get("error", ""))


class TestGatekeeperPropagatesisolation(unittest.TestCase):
    def test_gatekeeper_prompt_preserves_worktree_branch(self):
        from governance.auto_chain import _build_gatekeeper_prompt

        result = {"review_summary": "QA passed", "issues": []}
        metadata = {
            "_worktree": "C:/repo/.worktrees/dev-task-1",
            "_branch": "dev/task-1",
            "changed_files": ["agent/foo.py"],
        }

        _, out_meta = _build_gatekeeper_prompt("task-qa-1", result, metadata)

        self.assertEqual(out_meta["_worktree"], "C:/repo/.worktrees/dev-task-1")
        self.assertEqual(out_meta["_branch"], "dev/task-1")

    def test_gatekeeper_prompt_falls_back_to_result_worktree(self):
        from governance.auto_chain import _build_gatekeeper_prompt

        result = {
            "review_summary": "QA passed",
            "issues": [],
            "_worktree": "C:/repo/.worktrees/dev-task-2",
            "_branch": "dev/task-2",
        }
        metadata = {"changed_files": ["agent/bar.py"]}

        _, out_meta = _build_gatekeeper_prompt("task-qa-2", result, metadata)

        self.assertEqual(out_meta["_worktree"], "C:/repo/.worktrees/dev-task-2")
        self.assertEqual(out_meta["_branch"], "dev/task-2")


if __name__ == "__main__":
    unittest.main()
