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


if __name__ == "__main__":
    unittest.main()
