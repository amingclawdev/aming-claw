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


    # --- AC7: New tests for task.completed before gate, dirty retry, non-dirty skip, retry success ---

    def test_task_completed_publishes_before_gate_check(self):
        """AC7a/AC5: task.completed event is published even when version gate blocks."""
        from governance import auto_chain

        conn = mock.Mock()
        # _load_task_trace
        conn.execute.return_value.fetchone.return_value = None

        publish_calls = []
        orig_publish = auto_chain._publish_event

        def _track_publish(event_name, payload):
            publish_calls.append(event_name)
            return orig_publish(event_name, payload)

        with mock.patch.object(auto_chain, "_publish_event", side_effect=_track_publish), \
             mock.patch.object(auto_chain, "_gate_version_check", return_value=(False, "HEAD != chain_version")), \
             mock.patch.object(auto_chain, "_record_gate_event"), \
             mock.patch.object(auto_chain, "_normalize_related_nodes", return_value=[]), \
             mock.patch.object(auto_chain, "_load_task_trace", return_value=("trace1", "chain1")), \
             mock.patch.object(auto_chain, "structured_log"):
            result = auto_chain._do_chain(conn, "aming-claw", "task-1", "pm", {}, {"chain_depth": 0})

        # task.completed must appear before gate.blocked
        self.assertIn("task.completed", publish_calls)
        self.assertIn("gate.blocked", publish_calls)
        tc_idx = publish_calls.index("task.completed")
        gb_idx = publish_calls.index("gate.blocked")
        self.assertLess(tc_idx, gb_idx, "task.completed must be published before gate.blocked")
        self.assertTrue(result.get("gate_blocked"))

    def test_dirty_workspace_triggers_one_retry(self):
        """AC7b/AC2: dirty workspace reason triggers exactly one retry with 10s sleep."""
        from governance import auto_chain

        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = None

        call_count = [0]

        def _fake_gate(c, pid, res, meta):
            call_count[0] += 1
            if call_count[0] == 1:
                return (False, "dirty workspace: some_file.py")
            # Second call also fails (persistent dirty)
            return (False, "dirty workspace: some_file.py")

        with mock.patch.object(auto_chain, "_publish_event"), \
             mock.patch.object(auto_chain, "_gate_version_check", side_effect=_fake_gate), \
             mock.patch.object(auto_chain, "_record_gate_event"), \
             mock.patch.object(auto_chain, "_normalize_related_nodes", return_value=[]), \
             mock.patch.object(auto_chain, "_load_task_trace", return_value=("trace1", "chain1")), \
             mock.patch.object(auto_chain, "structured_log"), \
             mock.patch("time.sleep") as mock_sleep:
            result = auto_chain._do_chain(conn, "aming-claw", "task-1", "pm", {}, {"chain_depth": 0})

        # Gate called exactly twice (initial + 1 retry)
        self.assertEqual(call_count[0], 2)
        # Slept ~10s
        mock_sleep.assert_called_once_with(10)
        # Still blocked after retry
        self.assertTrue(result.get("gate_blocked"))

    def test_non_dirty_block_skips_retry(self):
        """AC7c/AC4: non-dirty block reason (e.g. HEAD mismatch) gets no retry."""
        from governance import auto_chain

        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = None

        call_count = [0]

        def _fake_gate(c, pid, res, meta):
            call_count[0] += 1
            return (False, "HEAD != chain_version")

        with mock.patch.object(auto_chain, "_publish_event"), \
             mock.patch.object(auto_chain, "_gate_version_check", side_effect=_fake_gate), \
             mock.patch.object(auto_chain, "_record_gate_event"), \
             mock.patch.object(auto_chain, "_normalize_related_nodes", return_value=[]), \
             mock.patch.object(auto_chain, "_load_task_trace", return_value=("trace1", "chain1")), \
             mock.patch.object(auto_chain, "structured_log"), \
             mock.patch("time.sleep") as mock_sleep:
            result = auto_chain._do_chain(conn, "aming-claw", "task-1", "pm", {}, {"chain_depth": 0})

        # Gate called only once — no retry
        self.assertEqual(call_count[0], 1)
        mock_sleep.assert_not_called()
        self.assertTrue(result.get("gate_blocked"))

    def test_dirty_workspace_retry_success_proceeds(self):
        """AC7d: if retry succeeds after dirty workspace, chain proceeds normally."""
        from governance import auto_chain

        call_count = [0]

        def _fake_gate(c, pid, res, meta):
            call_count[0] += 1
            if call_count[0] == 1:
                return (False, "dirty workspace: some_file.py")
            return (True, "version match")

        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = None

        # We need to mock the stage-specific gate and builder too
        # _do_chain after version gate calls the stage gate (e.g. _gate_post_pm)
        # Let's just verify it doesn't return gate_blocked
        with mock.patch.object(auto_chain, "_publish_event"), \
             mock.patch.object(auto_chain, "_gate_version_check", side_effect=_fake_gate), \
             mock.patch.object(auto_chain, "_record_gate_event"), \
             mock.patch.object(auto_chain, "_normalize_related_nodes", return_value=[]), \
             mock.patch.object(auto_chain, "_load_task_trace", return_value=("trace1", "chain1")), \
             mock.patch.object(auto_chain, "structured_log"), \
             mock.patch("time.sleep") as mock_sleep, \
             mock.patch.object(auto_chain, "_gate_post_pm", return_value=(False, "test gate fail")):
            result = auto_chain._do_chain(conn, "aming-claw", "task-1", "pm", {}, {"chain_depth": 0})

        # Gate called twice, sleep called once
        self.assertEqual(call_count[0], 2)
        mock_sleep.assert_called_once_with(10)
        # Version gate passed on retry — if blocked, it's the stage gate, not version_check
        if result.get("gate_blocked"):
            # The block must NOT be from version_check (that passed on retry)
            self.assertNotEqual(result.get("stage"), "version_check",
                                "version gate should have passed on retry")


if __name__ == "__main__":
    unittest.main()
