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

    def test_dirty_workspace_blocks_chain(self):
        """Dirty workspace (non-.claude files) blocks the chain."""
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
        self.assertIn("dirty workspace", reason)

    def test_claude_config_dirty_files_are_ignored(self):
        """D5: .claude/ files are filtered out of dirty_files entirely."""
        from governance import auto_chain

        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = {
            "chain_version": "abc1234",
            "git_head": "abc1234",
            "dirty_files": '[".claude/settings.local.json"]',
        }

        with mock.patch.object(auto_chain, "_DISABLE_VERSION_GATE", False), \
             self._patch_server_version("abc1234"), \
             mock.patch("subprocess.run", return_value=SimpleNamespace(stdout="abc1234\n", returncode=0)):
            passed, reason = auto_chain._gate_version_check(conn, "aming-claw", {}, {})

        self.assertTrue(passed)
        self.assertIn("version match", reason)  # No dirty files after filtering

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
        self.assertIn("skipped", reason)

    def test_observer_merge_bypasses_gate(self):
        """observer_merge=True in metadata bypasses the version gate."""
        from governance import auto_chain

        with mock.patch.object(auto_chain, "_DISABLE_VERSION_GATE", False):
            passed, reason = auto_chain._gate_version_check(mock.Mock(), "aming-claw", {}, {"observer_merge": True})

        self.assertTrue(passed)
        self.assertIn("observer merge bypass", reason)

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
        """AC4: without reconciliation bypass, dirty workspace blocks."""
        from governance import auto_chain

        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = {
            "chain_version": "abc1234",
            "git_head": "abc1234",
            "dirty_files": '["some_file.py"]',
        }

        metadata_no_auth = {
            "reconciliation_lane": "A",
            # observer_authorized is missing — reconciliation bypass won't trigger
        }
        with mock.patch.object(auto_chain, "_DISABLE_VERSION_GATE", False), \
             self._patch_server_version("abc1234"), \
             mock.patch("subprocess.run", return_value=SimpleNamespace(stdout="abc1234\n", returncode=0)):
            passed, reason = auto_chain._gate_version_check(conn, "aming-claw", {}, metadata_no_auth)

        # Dirty workspace blocks (no bypass triggered)
        self.assertFalse(passed)
        self.assertIn("dirty workspace", reason)

        # Also test with observer_authorized=False — still blocks
        metadata_false_auth = {
            "reconciliation_lane": "A",
            "observer_authorized": False,
        }
        with mock.patch.object(auto_chain, "_DISABLE_VERSION_GATE", False), \
             self._patch_server_version("abc1234"), \
             mock.patch("subprocess.run", return_value=SimpleNamespace(stdout="abc1234\n", returncode=0)):
            passed, reason = auto_chain._gate_version_check(conn, "aming-claw", {}, metadata_false_auth)

        self.assertFalse(passed)
        self.assertIn("dirty workspace", reason)

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

    # --- Server version mismatch now blocks (restored from D3 warning-only) ---
    def test_server_version_mismatch_blocks_chain(self):
        """SERVER_VERSION != HEAD blocks the chain. Restart service to resolve."""
        from governance import auto_chain

        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = {
            "chain_version": "abc1234",
            "git_head": "def5678",
            "dirty_files": "[]",
        }

        metadata = {}
        with mock.patch.object(auto_chain, "_DISABLE_VERSION_GATE", False), \
             self._patch_server_version("abc1234"), \
             mock.patch("subprocess.run", return_value=SimpleNamespace(stdout="def5678\n", returncode=0)):
            passed, reason = auto_chain._gate_version_check(conn, "aming-claw", {}, metadata)

        self.assertFalse(passed)
        self.assertIn("server version", reason)
        self.assertIn("Restart", reason)

    def test_server_version_mismatch_bypassed_by_observer_merge(self):
        """observer_merge metadata lets chain proceed despite version mismatch."""
        from governance import auto_chain

        metadata = {"observer_merge": True}
        with mock.patch.object(auto_chain, "_DISABLE_VERSION_GATE", False):
            passed, reason = auto_chain._gate_version_check(mock.Mock(), "aming-claw", {}, metadata)

        self.assertTrue(passed)
        self.assertIn("observer merge bypass", reason)

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
