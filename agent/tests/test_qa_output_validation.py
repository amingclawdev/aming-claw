"""Tests for B28b: QA executor hard validation of structured output."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)

from executor_worker import ExecutorWorker


def _make_worker():
    return ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace=os.getcwd())


def _make_qa_task(**meta):
    return {
        "task_id": "task-qa-b28b",
        "type": "qa",
        "prompt": "Review changes",
        "metadata": meta,
    }


def _fake_lifecycle(stdout: str):
    session = MagicMock()
    session.pid = 999
    session.status = "completed"
    session.stderr = ""
    session.stdout = stdout
    session.session_id = "sess-b28b"

    lc = MagicMock()
    lc.create_session.return_value = session
    lc.wait_for_output.return_value = {"status": "completed", "elapsed_sec": 1.0}
    lc.extend_deadline = MagicMock()
    return lc


class TestQAOutputValidation(unittest.TestCase):

    def test_qa_natural_language_fails_no_json(self):
        """B28b: QA outputs prose → structured_output_invalid:no_json."""
        worker = _make_worker()
        worker._lifecycle = _fake_lifecycle("The changes look good. All tests passed.")

        with patch.object(worker, "_build_prompt", return_value="prompt"), \
             patch.object(worker, "_write_memory"):
            result = worker._execute_task(_make_qa_task())

        self.assertEqual(result["status"], "failed")
        self.assertIn("structured_output_invalid:no_json", result["error"])

    def test_qa_json_missing_recommendation_fails(self):
        """B28b: QA JSON without recommendation → structured_output_invalid:missing_recommendation."""
        worker = _make_worker()
        worker._lifecycle = _fake_lifecycle('{"summary": "looks ok", "review_summary": "all good"}')

        with patch.object(worker, "_build_prompt", return_value="prompt"), \
             patch.object(worker, "_write_memory"):
            result = worker._execute_task(_make_qa_task())

        self.assertEqual(result["status"], "failed")
        self.assertIn("structured_output_invalid:missing_recommendation", result["error"])

    def test_qa_invalid_recommendation_value_fails(self):
        """B28b: QA with unknown recommendation → structured_output_invalid:invalid_recommendation."""
        worker = _make_worker()
        worker._lifecycle = _fake_lifecycle(
            '{"recommendation": "approved", "review_summary": "all good"}'
        )

        with patch.object(worker, "_build_prompt", return_value="prompt"), \
             patch.object(worker, "_write_memory"):
            result = worker._execute_task(_make_qa_task())

        self.assertEqual(result["status"], "failed")
        self.assertIn("structured_output_invalid:invalid_recommendation:approved", result["error"])

    def test_qa_valid_qa_pass_succeeds(self):
        """B28b: QA with recommendation=qa_pass → succeeds."""
        worker = _make_worker()
        worker._lifecycle = _fake_lifecycle(
            '{"recommendation": "qa_pass", "review_summary": "Tests pass, changes approved"}'
        )

        with patch.object(worker, "_build_prompt", return_value="prompt"), \
             patch.object(worker, "_write_memory"):
            result = worker._execute_task(_make_qa_task())

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["recommendation"], "qa_pass")

    def test_qa_valid_reject_succeeds(self):
        """B28b: QA with recommendation=reject → succeeds (gate handles reject logic)."""
        worker = _make_worker()
        worker._lifecycle = _fake_lifecycle(
            '{"recommendation": "reject", "reason": "tests failed"}'
        )

        with patch.object(worker, "_build_prompt", return_value="prompt"), \
             patch.object(worker, "_write_memory"):
            result = worker._execute_task(_make_qa_task())

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["recommendation"], "reject")

    def test_qa_valid_merge_pass_succeeds(self):
        """B28b: QA with recommendation=merge_pass → succeeds."""
        worker = _make_worker()
        worker._lifecycle = _fake_lifecycle(
            '{"recommendation": "merge_pass", "review_summary": "Ready to merge"}'
        )

        with patch.object(worker, "_build_prompt", return_value="prompt"), \
             patch.object(worker, "_write_memory"):
            result = worker._execute_task(_make_qa_task())

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["recommendation"], "merge_pass")


if __name__ == "__main__":
    unittest.main()
