"""Tests for governance gate policy engine."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from governance.enums import VerifyStatus
from governance.models import GateRequirement
from governance.gate_policy import check_gate, check_all_gates, check_gates_or_raise
from governance.errors import GateUnsatisfiedError


class TestCheckGate(unittest.TestCase):
    def test_default_gate_pass(self):
        req = GateRequirement(node_id="L1.4", min_status="qa_pass")
        ok, reason = check_gate(req, VerifyStatus.QA_PASS)
        self.assertTrue(ok)

    def test_default_gate_fail_on_t2(self):
        req = GateRequirement(node_id="L1.4", min_status="qa_pass")
        ok, reason = check_gate(req, VerifyStatus.T2_PASS)
        self.assertFalse(ok)
        self.assertIn("requires qa_pass", reason)

    def test_gate_with_t2_minimum(self):
        req = GateRequirement(node_id="L1.4", min_status="t2_pass")
        ok, reason = check_gate(req, VerifyStatus.T2_PASS)
        self.assertTrue(ok)

    def test_gate_failed_node(self):
        req = GateRequirement(node_id="L1.4", min_status="t2_pass")
        ok, reason = check_gate(req, VerifyStatus.FAILED)
        self.assertFalse(ok)
        self.assertIn("FAILED", reason)

    def test_release_only_skipped_in_default(self):
        req = GateRequirement(node_id="L4.1", policy="release_only")
        ok, reason = check_gate(req, VerifyStatus.PENDING, context="default")
        self.assertTrue(ok)

    def test_release_only_checked_in_release(self):
        req = GateRequirement(node_id="L4.1", min_status="qa_pass", policy="release_only")
        ok, reason = check_gate(req, VerifyStatus.PENDING, context="release")
        self.assertFalse(ok)

    def test_waivable_gate_waived(self):
        req = GateRequirement(node_id="L1.4", policy="waivable", waived_by="coord-001")
        ok, reason = check_gate(req, VerifyStatus.PENDING)
        self.assertTrue(ok)
        self.assertIn("waived", reason)

    def test_waivable_gate_not_waived(self):
        req = GateRequirement(node_id="L1.4", min_status="qa_pass", policy="waivable")
        ok, reason = check_gate(req, VerifyStatus.PENDING)
        self.assertFalse(ok)


class TestCheckAllGates(unittest.TestCase):
    def test_all_satisfied(self):
        gates = [
            GateRequirement(node_id="L0.1", min_status="t2_pass"),
            GateRequirement(node_id="L0.2", min_status="t2_pass"),
        ]
        def get_status(nid):
            return VerifyStatus.QA_PASS
        ok, unsatisfied = check_all_gates(gates, get_status)
        self.assertTrue(ok)
        self.assertEqual(len(unsatisfied), 0)

    def test_one_unsatisfied(self):
        gates = [
            GateRequirement(node_id="L0.1", min_status="t2_pass"),
            GateRequirement(node_id="L0.2", min_status="qa_pass"),
        ]
        def get_status(nid):
            return VerifyStatus.T2_PASS
        ok, unsatisfied = check_all_gates(gates, get_status)
        self.assertFalse(ok)
        self.assertEqual(len(unsatisfied), 1)
        self.assertEqual(unsatisfied[0]["node_id"], "L0.2")

    def test_check_or_raise(self):
        gates = [GateRequirement(node_id="L0.1", min_status="qa_pass")]
        def get_status(nid):
            return VerifyStatus.PENDING
        with self.assertRaises(GateUnsatisfiedError):
            check_gates_or_raise("L1.1", gates, get_status)


class TestReconciliationBypassPolicy(unittest.TestCase):
    """Tests for RECONCILIATION_BYPASS_POLICY and _check_reconciliation_bypass."""

    def test_policy_object_exists_with_required_keys(self):
        from governance.auto_chain import RECONCILIATION_BYPASS_POLICY
        self.assertIn("required_metadata_fields", RECONCILIATION_BYPASS_POLICY)
        self.assertIn("allowed_lanes", RECONCILIATION_BYPASS_POLICY)
        self.assertIn("audit_action", RECONCILIATION_BYPASS_POLICY)
        self.assertIn("reconciliation_lane", RECONCILIATION_BYPASS_POLICY["required_metadata_fields"])
        self.assertIn("observer_authorized", RECONCILIATION_BYPASS_POLICY["required_metadata_fields"])

    def test_check_bypass_rejects_missing_lane(self):
        from unittest.mock import Mock
        from governance.auto_chain import _check_reconciliation_bypass
        conn = Mock()
        bypass, obs_id = _check_reconciliation_bypass(conn, "test", {"observer_authorized": True})
        self.assertFalse(bypass)

    def test_check_bypass_rejects_invalid_lane(self):
        from unittest.mock import Mock
        from governance.auto_chain import _check_reconciliation_bypass
        conn = Mock()
        bypass, obs_id = _check_reconciliation_bypass(conn, "test", {
            "reconciliation_lane": "Z",
            "observer_authorized": True,
        })
        self.assertFalse(bypass)

    def test_check_bypass_rejects_missing_observer_authorized(self):
        from unittest.mock import Mock
        from governance.auto_chain import _check_reconciliation_bypass
        conn = Mock()
        bypass, obs_id = _check_reconciliation_bypass(conn, "test", {
            "reconciliation_lane": "A",
        })
        self.assertFalse(bypass)

    def test_check_bypass_passes_with_full_policy(self):
        from unittest.mock import Mock
        from governance.auto_chain import _check_reconciliation_bypass
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = None
        bypass, obs_id = _check_reconciliation_bypass(conn, "test", {
            "reconciliation_lane": "A",
            "observer_authorized": True,
            "observer_task_id": "task-obs-123",
        })
        self.assertTrue(bypass)
        self.assertEqual(obs_id, "task-obs-123")

    def test_finalize_chain_calls_version_sync(self):
        """Verify _finalize_chain performs version-sync and version-update."""
        from unittest.mock import Mock, patch
        from types import SimpleNamespace
        from governance.auto_chain import _finalize_chain

        conn = Mock()
        with patch("subprocess.run", return_value=SimpleNamespace(stdout="abc1234\n", returncode=0)):
            result = _finalize_chain(conn, "test-proj", "task-merge-1", {"report": {"success": True}}, {})

        self.assertEqual(result["deploy"], "completed")
        # Verify DB writes happened (version-sync + version-update)
        self.assertTrue(conn.execute.called)

    def test_finalize_chain_restart_required_on_stale_server(self):
        """Verify restart_required is set when SERVER_VERSION is stale."""
        import types as _types
        from unittest.mock import Mock, patch
        from types import SimpleNamespace
        from governance.auto_chain import _finalize_chain

        conn = Mock()
        # Create a mock server module to avoid importing the real one (Py3.10+ syntax)
        mock_server = _types.ModuleType("governance.server")
        mock_server.SERVER_VERSION = "oldvers1"
        mock_server.get_server_version = lambda: "oldvers1"
        with patch("subprocess.run", return_value=SimpleNamespace(stdout="newhead1\n", returncode=0)), \
             patch.dict("sys.modules", {"governance.server": mock_server}):
            result = _finalize_chain(conn, "test-proj", "task-merge-1", {"report": {"success": True}}, {})

        self.assertTrue(result.get("restart_required"))


class TestGateCheckpointTestFileInference(unittest.TestCase):
    """Tests for _gate_checkpoint test-file co-modification allowance (R1-R4)."""

    def _call_gate(self, changed_files, target_files, extra_meta=None):
        from unittest.mock import Mock
        from governance.auto_chain import _gate_checkpoint
        conn = Mock()
        # Mock conn.execute for _should_defer_doc_gate_to_lane_c
        conn.execute.return_value.fetchone.return_value = None
        result = {
            "changed_files": changed_files,
            "test_results": {"ran": True, "passed": 1, "failed": 0},
        }
        metadata = {
            "target_files": target_files,
            "doc_impact": {"files": [], "changes": []},
            "skip_doc_check": True,
            "bootstrap_reason": "test",
        }
        if extra_meta:
            metadata.update(extra_meta)
        return _gate_checkpoint(conn, "test-proj", result, metadata)

    def test_ac2_comodified_test_file_allowed(self):
        """AC2: test file matching target stem is allowed."""
        ok, reason = self._call_gate(
            changed_files=["agent/ai_lifecycle.py", "agent/tests/test_ai_lifecycle_provider_routing.py"],
            target_files=["agent/ai_lifecycle.py"],
        )
        self.assertTrue(ok, f"Expected pass but got: {reason}")

    def test_ac3_unrelated_test_file_blocked(self):
        """AC3: test file NOT matching target stem is blocked."""
        ok, reason = self._call_gate(
            changed_files=["agent/ai_lifecycle.py", "agent/tests/test_something_else.py"],
            target_files=["agent/ai_lifecycle.py"],
        )
        self.assertFalse(ok)
        self.assertIn("Unrelated files modified", reason)
        self.assertIn("test_something_else.py", reason)

    def test_ac4_explicit_test_files_still_work(self):
        """AC4: explicit test_files in metadata still allowed."""
        ok, reason = self._call_gate(
            changed_files=["agent/ai_lifecycle.py", "agent/tests/test_something_else.py"],
            target_files=["agent/ai_lifecycle.py"],
            extra_meta={"test_files": ["agent/tests/test_something_else.py"]},
        )
        self.assertTrue(ok, f"Expected pass but got: {reason}")

    def test_exact_stem_match_test_file(self):
        """test_ai_lifecycle.py (exact stem) is also allowed."""
        ok, reason = self._call_gate(
            changed_files=["agent/ai_lifecycle.py", "agent/tests/test_ai_lifecycle.py"],
            target_files=["agent/ai_lifecycle.py"],
        )
        self.assertTrue(ok, f"Expected pass but got: {reason}")

    def test_non_tests_dir_not_allowed(self):
        """R4: test-named file NOT under tests/ directory is blocked."""
        ok, reason = self._call_gate(
            changed_files=["agent/ai_lifecycle.py", "agent/test_ai_lifecycle.py"],
            target_files=["agent/ai_lifecycle.py"],
        )
        self.assertFalse(ok)
        self.assertIn("Unrelated files modified", reason)

    def test_doc_impact_files_still_allowed(self):
        """AC4: doc_impact.files entries still work."""
        ok, reason = self._call_gate(
            changed_files=["agent/ai_lifecycle.py", "docs/lifecycle.md"],
            target_files=["agent/ai_lifecycle.py"],
            extra_meta={"doc_impact": {"files": ["docs/lifecycle.md"], "changes": []}},
        )
        self.assertTrue(ok, f"Expected pass but got: {reason}")


class TestCheckNodesWaivedStatus(unittest.TestCase):
    """Tests that 'waived' in _STATUS_ORDER allows nodes to pass gate checks."""

    def _make_conn(self, node_id, verify_status):
        """Create a mock connection that returns the given verify_status for node_id."""
        from unittest.mock import Mock, MagicMock
        conn = Mock()

        def execute_side_effect(sql, params=None):
            result = Mock()
            if "COUNT" in sql:
                result.fetchone.return_value = (1,)  # non-empty DB
            elif "verify_status" in sql:
                if params and params[1] == node_id:
                    result.fetchone.return_value = {"verify_status": verify_status}
                else:
                    result.fetchone.return_value = None
            else:
                result.fetchone.return_value = None
            return result

        conn.execute = Mock(side_effect=execute_side_effect)
        return conn

    def test_waived_in_status_order(self):
        """AC1/AC2: _STATUS_ORDER contains 'waived' ranked >= qa_pass."""
        from governance.auto_chain import _STATUS_ORDER
        self.assertIn("waived", _STATUS_ORDER)
        self.assertGreaterEqual(
            _STATUS_ORDER.index("waived"),
            _STATUS_ORDER.index("qa_pass"),
        )

    def test_check_nodes_waived_passes_t2_gate(self):
        """AC3: waived node passes t2_pass gate check."""
        from governance.auto_chain import _check_nodes_min_status
        conn = self._make_conn("L1.1", "waived")
        passed, reason = _check_nodes_min_status(conn, "test-proj", ["L1.1"], "t2_pass")
        self.assertTrue(passed, f"Expected pass but got: {reason}")
        self.assertEqual(reason, "ok")

    def test_check_nodes_waived_passes_qa_gate(self):
        """AC4: waived node passes qa_pass gate check."""
        from governance.auto_chain import _check_nodes_min_status
        conn = self._make_conn("L1.1", "waived")
        passed, reason = _check_nodes_min_status(conn, "test-proj", ["L1.1"], "qa_pass")
        self.assertTrue(passed, f"Expected pass but got: {reason}")
        self.assertEqual(reason, "ok")


class TestCheckNodesMissingVsInsufficientStatus(unittest.TestCase):
    """Tests for _check_nodes_min_status: missing nodes skip, insufficient nodes block."""

    def _make_conn(self, nodes_in_db):
        """Create a mock connection with specified nodes.

        Args:
            nodes_in_db: dict of node_id -> verify_status. Only these nodes exist in DB.
                         At least one entry means the DB is non-empty (COUNT > 0).
        """
        from unittest.mock import Mock

        def execute_side_effect(sql, params=None):
            result = Mock()
            if "COUNT" in sql:
                result.fetchone.return_value = (len(nodes_in_db),)
            elif "verify_status" in sql:
                node_id = params[1] if params and len(params) > 1 else None
                if node_id in nodes_in_db:
                    result.fetchone.return_value = {"verify_status": nodes_in_db[node_id]}
                else:
                    result.fetchone.return_value = None
            else:
                result.fetchone.return_value = None
            return result

        conn = Mock()
        conn.execute = Mock(side_effect=execute_side_effect)
        return conn

    def test_ac5_missing_node_skipped_gate_passes(self):
        """AC5: node_state populated but specific related_node missing — gate passes with skip."""
        from governance.auto_chain import _check_nodes_min_status
        # DB has L1.1 at t2_pass, but L1.3 is NOT in DB
        conn = self._make_conn({"L1.1": "t2_pass"})
        passed, reason = _check_nodes_min_status(conn, "test-proj", ["L1.3"], "t2_pass")
        self.assertTrue(passed, f"Expected pass (skip missing node) but got: {reason}")

    def test_ac6_existing_node_below_min_status_blocks(self):
        """AC6: node exists in DB with status 'testing' and min_status is 't2_pass' — gate blocks."""
        from governance.auto_chain import _check_nodes_min_status
        conn = self._make_conn({"L1.1": "testing"})
        passed, reason = _check_nodes_min_status(conn, "test-proj", ["L1.1"], "t2_pass")
        self.assertFalse(passed, f"Expected block but got pass: {reason}")
        self.assertIn("L1.1", reason)

    def test_ac7_warning_logged_for_missing_node(self):
        """AC7: log.warning called with 'not found in DB' and 'skipping' for missing node."""
        from unittest.mock import patch
        from governance.auto_chain import _check_nodes_min_status
        conn = self._make_conn({"L1.1": "t2_pass"})
        with patch("governance.auto_chain.log") as mock_log:
            _check_nodes_min_status(conn, "test-proj", ["L1.3"], "t2_pass")
            mock_log.warning.assert_called_once()
            warning_msg = mock_log.warning.call_args[0][0] % mock_log.warning.call_args[0][1:]
            self.assertIn("not found in DB", warning_msg)
            self.assertIn("skipping", warning_msg.lower())

    def test_ac1_ac3_all_missing_nodes_pass(self):
        """AC1/AC3: all related_nodes missing from DB — gate passes."""
        from governance.auto_chain import _check_nodes_min_status
        conn = self._make_conn({"L1.1": "t2_pass"})  # non-empty DB
        passed, reason = _check_nodes_min_status(conn, "test-proj", ["L1.3", "L1.4"], "t2_pass")
        self.assertTrue(passed, f"Expected pass but got: {reason}")

    def test_ac2_ac4_existing_below_min_blocks_even_with_missing(self):
        """AC2/AC4: one existing node below min_status blocks even if another is missing."""
        from governance.auto_chain import _check_nodes_min_status
        conn = self._make_conn({"L1.1": "testing"})  # below t2_pass
        passed, reason = _check_nodes_min_status(conn, "test-proj", ["L1.1", "L1.3"], "t2_pass")
        self.assertFalse(passed, f"Expected block but got pass: {reason}")
        self.assertIn("L1.1", reason)
        # L1.3 should NOT appear in the blocking reason
        self.assertNotIn("L1.3", reason)


class TestCheckVerifyRequiresSatisfied(unittest.TestCase):
    """Tests for _check_verify_requires_satisfied gate predicate (verify_requires)."""

    def _make_conn(self, nodes_in_db):
        """Create a mock connection returning verify_status for given nodes."""
        from unittest.mock import Mock

        def execute_side_effect(sql, params=None):
            result = Mock()
            if "verify_status" in sql:
                node_id = params[1] if params and len(params) > 1 else None
                if node_id in nodes_in_db:
                    result.fetchone.return_value = {"verify_status": nodes_in_db[node_id]}
                else:
                    result.fetchone.return_value = None
            else:
                result.fetchone.return_value = None
            return result

        conn = Mock()
        conn.execute = Mock(side_effect=execute_side_effect)
        return conn

    def test_ac2_waived_node_not_blocking(self):
        """AC2: node with verify_status='waived' does NOT appear in blocking list."""
        from governance.auto_chain import _check_verify_requires_satisfied
        conn = self._make_conn({"L1.1": "waived"})
        satisfied, blocking = _check_verify_requires_satisfied(conn, "test-proj", ["L1.1"])
        self.assertTrue(satisfied, f"Expected satisfied but got blocking: {blocking}")
        self.assertEqual(blocking, [])

    def test_ac3_pending_node_blocks(self):
        """AC3: node with verify_status='pending' appears in blocking list."""
        from governance.auto_chain import _check_verify_requires_satisfied
        conn = self._make_conn({"L1.1": "pending"})
        satisfied, blocking = _check_verify_requires_satisfied(conn, "test-proj", ["L1.1"])
        self.assertFalse(satisfied)
        self.assertIn("L1.1", blocking)

    def test_ac3_failed_node_blocks(self):
        """AC3: node with verify_status='failed' appears in blocking list."""
        from governance.auto_chain import _check_verify_requires_satisfied
        conn = self._make_conn({"L1.1": "failed"})
        satisfied, blocking = _check_verify_requires_satisfied(conn, "test-proj", ["L1.1"])
        self.assertFalse(satisfied)
        self.assertIn("L1.1", blocking)

    def test_qa_pass_not_blocking(self):
        """qa_pass node satisfies verify_requires."""
        from governance.auto_chain import _check_verify_requires_satisfied
        conn = self._make_conn({"L1.1": "qa_pass"})
        satisfied, blocking = _check_verify_requires_satisfied(conn, "test-proj", ["L1.1"])
        self.assertTrue(satisfied)

    def test_t2_pass_not_blocking(self):
        """t2_pass node satisfies verify_requires."""
        from governance.auto_chain import _check_verify_requires_satisfied
        conn = self._make_conn({"L1.1": "t2_pass"})
        satisfied, blocking = _check_verify_requires_satisfied(conn, "test-proj", ["L1.1"])
        self.assertTrue(satisfied)

    def test_testing_node_blocks(self):
        """testing node does NOT satisfy verify_requires."""
        from governance.auto_chain import _check_verify_requires_satisfied
        conn = self._make_conn({"L1.1": "testing"})
        satisfied, blocking = _check_verify_requires_satisfied(conn, "test-proj", ["L1.1"])
        self.assertFalse(satisfied)
        self.assertIn("L1.1", blocking)

    def test_missing_node_blocks(self):
        """Node not in DB is treated as blocking."""
        from governance.auto_chain import _check_verify_requires_satisfied
        conn = self._make_conn({})
        satisfied, blocking = _check_verify_requires_satisfied(conn, "test-proj", ["L1.1"])
        self.assertFalse(satisfied)
        self.assertIn("L1.1", blocking)

    def test_empty_verify_requires_satisfied(self):
        """Empty verify_requires list is always satisfied."""
        from governance.auto_chain import _check_verify_requires_satisfied
        conn = self._make_conn({})
        satisfied, blocking = _check_verify_requires_satisfied(conn, "test-proj", [])
        self.assertTrue(satisfied)

    def test_mixed_statuses(self):
        """Mix of waived (pass) and pending (block) — only pending blocks."""
        from governance.auto_chain import _check_verify_requires_satisfied
        conn = self._make_conn({"L1.1": "waived", "L1.2": "pending"})
        satisfied, blocking = _check_verify_requires_satisfied(conn, "test-proj", ["L1.1", "L1.2"])
        self.assertFalse(satisfied)
        self.assertNotIn("L1.1", blocking)
        self.assertIn("L1.2", blocking)


if __name__ == "__main__":
    unittest.main()

