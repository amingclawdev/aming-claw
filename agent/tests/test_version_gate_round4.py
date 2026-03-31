import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


class TestVersionGateRound4(unittest.TestCase):
    def _patch_server_version(self, version):
        """Helper to patch SERVER_VERSION without importing governance.server."""
        # Create a mock module to avoid importing the real server (which has Py3.10+ syntax deps)
        import types
        mock_server = types.ModuleType("governance.server")
        mock_server.SERVER_VERSION = version
        return mock.patch.dict("sys.modules", {"governance.server": mock_server})

    def test_dirty_workspace_blocks_auto_chain(self):
        from governance import auto_chain

        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = {
            "chain_version": "abc1234",
            "git_head": "abc1234",
            "dirty_files": '["agent/executor_worker.py"]',
        }

        with mock.patch.object(auto_chain, "_DISABLE_VERSION_GATE", False), \
             self._patch_server_version("abc1234"), \
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
             self._patch_server_version("abc1234"), \
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

    # --- AC4: reconciliation bypass requires observer_authorized ---
    def test_reconciliation_bypass_requires_observer_authorized(self):
        """AC4: _gate_version_check blocks when metadata has reconciliation_lane='A'
        but observer_authorized is missing/false."""
        from governance import auto_chain

        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = {
            "chain_version": "abc1234",
            "git_head": "abc1234",
            "dirty_files": '["some_file.py"]',
        }

        metadata_no_auth = {
            "reconciliation_lane": "A",
            # observer_authorized is missing
        }
        with mock.patch.object(auto_chain, "_DISABLE_VERSION_GATE", False), \
             self._patch_server_version("abc1234"), \
             mock.patch("subprocess.run", return_value=SimpleNamespace(stdout="abc1234\n", returncode=0)):
            passed, reason = auto_chain._gate_version_check(conn, "aming-claw", {}, metadata_no_auth)

        self.assertFalse(passed)
        self.assertIn("Dirty workspace detected", reason)

        # Also test with observer_authorized=False
        metadata_false_auth = {
            "reconciliation_lane": "A",
            "observer_authorized": False,
        }
        with mock.patch.object(auto_chain, "_DISABLE_VERSION_GATE", False), \
             self._patch_server_version("abc1234"), \
             mock.patch("subprocess.run", return_value=SimpleNamespace(stdout="abc1234\n", returncode=0)):
            passed, reason = auto_chain._gate_version_check(conn, "aming-claw", {}, metadata_false_auth)

        self.assertFalse(passed)
        self.assertIn("Dirty workspace detected", reason)

    # --- AC5: reconciliation bypass passes with full policy ---
    def test_reconciliation_bypass_passes_with_full_policy(self):
        """AC5: _gate_version_check passes when metadata has reconciliation_lane='A',
        observer_authorized=True, and returns reason containing 'reconciliation-bypass'."""
        from governance import auto_chain

        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = {
            "chain_version": "abc1234",
            "git_head": "abc1234",
            "dirty_files": '["some_file.py"]',
        }

        metadata = {
            "reconciliation_lane": "A",
            "observer_authorized": True,
            "observer_task_id": "task-observer-001",
            "task_id": "task-recon-dev",
        }
        with mock.patch.object(auto_chain, "_DISABLE_VERSION_GATE", False):
            passed, reason = auto_chain._gate_version_check(conn, "aming-claw", {}, metadata)

        self.assertTrue(passed)
        self.assertIn("reconciliation-bypass", reason)
        self.assertIn("task-observer-001", reason)

    # --- AC6: normal task blocked despite reconciliation code existing ---
    def test_normal_task_blocked_despite_reconciliation_code(self):
        """AC6: _gate_version_check blocks when SERVER_VERSION != HEAD and
        metadata has no reconciliation fields."""
        from governance import auto_chain

        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = {
            "chain_version": "abc1234",
            "git_head": "def5678",
            "dirty_files": "[]",
        }

        metadata = {}  # no reconciliation fields at all
        with mock.patch.object(auto_chain, "_DISABLE_VERSION_GATE", False), \
             self._patch_server_version("abc1234"), \
             mock.patch("subprocess.run", return_value=SimpleNamespace(stdout="def5678\n", returncode=0)):
            passed, reason = auto_chain._gate_version_check(conn, "aming-claw", {}, metadata)

        self.assertFalse(passed)
        self.assertIn("behind git HEAD", reason)

    # --- Test RECONCILIATION_BYPASS_POLICY structure (AC1) ---
    def test_reconciliation_bypass_policy_structure(self):
        """AC1: RECONCILIATION_BYPASS_POLICY has required keys."""
        from governance.auto_chain import RECONCILIATION_BYPASS_POLICY

        self.assertIn("required_metadata_fields", RECONCILIATION_BYPASS_POLICY)
        self.assertIn("allowed_lanes", RECONCILIATION_BYPASS_POLICY)
        self.assertIn("audit_action", RECONCILIATION_BYPASS_POLICY)
        self.assertEqual(RECONCILIATION_BYPASS_POLICY["audit_action"], "reconciliation_bypass")


if __name__ == "__main__":
    unittest.main()
