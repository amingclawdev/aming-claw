import os
import sys
import tempfile
import unittest
from unittest import mock

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


class TestExecutorWorkerPidLockRound1(unittest.TestCase):
    def test_acquire_pid_lock_treats_systemerror_as_stale_pid(self):
        from executor_worker import ExecutorWorker

        worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace="C:/repo")
        pid_path = os.path.join(tempfile.gettempdir(), "aming-claw-executor-aming-claw.pid")
        with open(pid_path, "w", encoding="utf-8") as handle:
            handle.write("99999")

        try:
            with mock.patch("executor_worker.os.kill", side_effect=SystemError("winerror 87")), \
                 mock.patch("executor_worker.os.getpid", return_value=12345):
                acquired = worker._acquire_pid_lock()

            self.assertTrue(acquired)
            self.assertEqual(worker._pid_path, pid_path)
            with open(pid_path, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read().strip(), "12345")
        finally:
            if os.path.exists(pid_path):
                os.unlink(pid_path)


if __name__ == "__main__":
    unittest.main()
