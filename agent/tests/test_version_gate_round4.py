import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


class TestVersionGateRound4(unittest.TestCase):
    def test_dirty_workspace_blocks_auto_chain(self):
        from governance import auto_chain

        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = {
            "chain_version": "abc1234",
            "git_head": "abc1234",
            "dirty_files": '["agent/executor_worker.py"]',
        }

        with mock.patch.object(auto_chain, "_DISABLE_VERSION_GATE", False), \
             mock.patch("governance.server.SERVER_VERSION", "abc1234"), \
             mock.patch("subprocess.run", return_value=SimpleNamespace(stdout="abc1234\n", returncode=0)):
            passed, reason = auto_chain._gate_version_check(conn, "aming-claw", {}, {})

        self.assertFalse(passed)
        self.assertIn("Dirty workspace detected", reason)

    def test_clean_workspace_and_matching_server_pass(self):
        from governance import auto_chain

        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = {
            "chain_version": "abc1234",
            "git_head": "abc1234",
            "dirty_files": "[]",
        }

        with mock.patch.object(auto_chain, "_DISABLE_VERSION_GATE", False), \
             mock.patch("governance.server.SERVER_VERSION", "abc1234"), \
             mock.patch("subprocess.run", return_value=SimpleNamespace(stdout="abc1234\n", returncode=0)):
            passed, reason = auto_chain._gate_version_check(conn, "aming-claw", {}, {})

        self.assertTrue(passed)
        self.assertIn("version match", reason)

    def test_skip_version_check_metadata_bypasses_gate(self):
        from governance import auto_chain

        with mock.patch.object(auto_chain, "_DISABLE_VERSION_GATE", False):
            passed, reason = auto_chain._gate_version_check(mock.Mock(), "aming-claw", {}, {"skip_version_check": True})

        self.assertTrue(passed)
        self.assertEqual(reason, "skipped")

    def test_governed_dirty_workspace_chain_bypasses_gate_via_parent_metadata(self):
        from governance import auto_chain

        conn = mock.Mock()

        def _execute(query, params):
            if "SELECT metadata_json FROM tasks" in query:
                return mock.Mock(fetchone=mock.Mock(return_value={
                    "metadata_json": '{"parallel_plan":"dirty-reconciliation-2026-03-30","lane":"A"}'
                }))
            if "SELECT chain_version, git_head, dirty_files FROM project_version" in query:
                return mock.Mock(fetchone=mock.Mock(return_value={
                    "chain_version": "abc1234",
                    "git_head": "abc1234",
                    "dirty_files": '["agent/executor_worker.py"]',
                }))
            raise AssertionError("Unexpected query: {}".format(query))

        conn.execute.side_effect = _execute

        with mock.patch.object(auto_chain, "_DISABLE_VERSION_GATE", False):
            passed, reason = auto_chain._gate_version_check(
                conn,
                "aming-claw",
                {},
                {"parent_task_id": "task-parent-pm"},
            )

        self.assertTrue(passed)
        self.assertIn("governed dirty-workspace reconciliation", reason)


if __name__ == "__main__":
    unittest.main()
