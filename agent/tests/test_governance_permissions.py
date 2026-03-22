"""Tests for governance permissions and scope checking."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from governance.enums import VerifyStatus, Role
from governance.errors import (
    PermissionDeniedError, ForbiddenTransitionError,
    InvalidTransitionError, ScopeViolationError,
)
from governance.permissions import check_transition, check_scope, check_nodes_scope


class TestTransitionRules(unittest.TestCase):
    def test_tester_can_pending_to_t2(self):
        check_transition(VerifyStatus.PENDING, VerifyStatus.T2_PASS, Role.TESTER)

    def test_qa_can_t2_to_pass(self):
        check_transition(VerifyStatus.T2_PASS, VerifyStatus.QA_PASS, Role.QA)

    def test_any_role_can_mark_failed(self):
        for role in Role:
            check_transition(VerifyStatus.QA_PASS, VerifyStatus.FAILED, role)

    def test_dev_can_fail_to_pending(self):
        check_transition(VerifyStatus.FAILED, VerifyStatus.PENDING, Role.DEV)

    def test_coordinator_can_waive(self):
        check_transition(VerifyStatus.PENDING, VerifyStatus.WAIVED, Role.COORDINATOR)

    def test_forbidden_skip_t2(self):
        with self.assertRaises(ForbiddenTransitionError):
            check_transition(VerifyStatus.PENDING, VerifyStatus.QA_PASS, Role.QA)

    def test_dev_cannot_t2_to_pass(self):
        with self.assertRaises(PermissionDeniedError):
            check_transition(VerifyStatus.T2_PASS, VerifyStatus.QA_PASS, Role.DEV)

    def test_tester_cannot_fail_to_pending(self):
        with self.assertRaises(PermissionDeniedError):
            check_transition(VerifyStatus.FAILED, VerifyStatus.PENDING, Role.TESTER)

    def test_invalid_transition(self):
        with self.assertRaises(InvalidTransitionError):
            check_transition(VerifyStatus.QA_PASS, VerifyStatus.T2_PASS, Role.QA)


class TestScope(unittest.TestCase):
    def test_empty_scope_allows_all(self):
        check_scope("L3.7", [])

    def test_matching_scope(self):
        check_scope("L1.5", ["L1.*"])
        check_scope("L2.3", ["L1.*", "L2.*"])

    def test_scope_violation(self):
        with self.assertRaises(ScopeViolationError):
            check_scope("L3.1", ["L1.*", "L2.*"])

    def test_exact_match(self):
        check_scope("L0.1", ["L0.1"])

    def test_check_nodes_scope(self):
        check_nodes_scope(["L1.1", "L1.2"], ["L1.*"])
        with self.assertRaises(ScopeViolationError):
            check_nodes_scope(["L1.1", "L3.1"], ["L1.*"])


if __name__ == "__main__":
    unittest.main()
