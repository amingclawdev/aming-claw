"""Tests for governance evidence validation."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from governance.enums import VerifyStatus
from governance.models import Evidence
from governance.evidence import validate_evidence
from governance.errors import InvalidEvidenceError


class TestEvidenceValidation(unittest.TestCase):
    def test_valid_test_report(self):
        e = Evidence(type="test_report", summary={"passed": 162, "failed": 0, "exit_code": 0})
        validate_evidence(VerifyStatus.PENDING, VerifyStatus.T2_PASS, e)

    def test_test_report_no_pass(self):
        e = Evidence(type="test_report", summary={"passed": 0, "failed": 5, "exit_code": 1})
        with self.assertRaises(InvalidEvidenceError):
            validate_evidence(VerifyStatus.PENDING, VerifyStatus.T2_PASS, e)

    def test_wrong_evidence_type(self):
        e = Evidence(type="error_log", summary={"error": "something"})
        with self.assertRaises(InvalidEvidenceError):
            validate_evidence(VerifyStatus.PENDING, VerifyStatus.T2_PASS, e)

    def test_valid_e2e_report(self):
        e = Evidence(type="e2e_report", summary={"passed": 14})
        validate_evidence(VerifyStatus.T2_PASS, VerifyStatus.QA_PASS, e)

    def test_e2e_report_no_pass(self):
        e = Evidence(type="e2e_report", summary={"passed": 0})
        with self.assertRaises(InvalidEvidenceError):
            validate_evidence(VerifyStatus.T2_PASS, VerifyStatus.QA_PASS, e)

    def test_valid_error_log(self):
        e = Evidence(type="error_log", summary={"error": "timeout after 30s"})
        validate_evidence(VerifyStatus.QA_PASS, VerifyStatus.FAILED, e)

    def test_error_log_with_artifact(self):
        e = Evidence(type="error_log", artifact_uri="logs/error-123.log")
        validate_evidence(VerifyStatus.T2_PASS, VerifyStatus.FAILED, e)

    def test_error_log_empty(self):
        e = Evidence(type="error_log", summary={})
        with self.assertRaises(InvalidEvidenceError):
            validate_evidence(VerifyStatus.QA_PASS, VerifyStatus.FAILED, e)

    def test_valid_commit_ref(self):
        e = Evidence(type="commit_ref", summary={"commit_hash": "a1b2c3d"})
        validate_evidence(VerifyStatus.FAILED, VerifyStatus.PENDING, e)

    def test_commit_ref_invalid_hash(self):
        e = Evidence(type="commit_ref", summary={"commit_hash": "xyz"})
        with self.assertRaises(InvalidEvidenceError):
            validate_evidence(VerifyStatus.FAILED, VerifyStatus.PENDING, e)

    def test_commit_ref_no_hash(self):
        e = Evidence(type="commit_ref", summary={})
        with self.assertRaises(InvalidEvidenceError):
            validate_evidence(VerifyStatus.FAILED, VerifyStatus.PENDING, e)

    def test_manual_review_for_waive(self):
        e = Evidence(type="manual_review", summary={"reason": "approved by PM"})
        validate_evidence(VerifyStatus.PENDING, VerifyStatus.WAIVED, e)


if __name__ == "__main__":
    unittest.main()
