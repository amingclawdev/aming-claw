"""Structured evidence validation for state transitions.

Evidence is a structured object (not a regex-matched string). Validators
check that the evidence type, content, and summary fields match the
requirements for each transition.
"""

from .models import Evidence
from .enums import VerifyStatus
from .errors import InvalidEvidenceError


def _validate_test_report(e: Evidence) -> None:
    passed = e.summary.get("passed", 0)
    exit_code = e.summary.get("exit_code")
    if passed <= 0:
        raise InvalidEvidenceError(
            "test_report must have passed > 0",
            {"got_passed": passed},
        )
    if exit_code is not None and exit_code != 0:
        raise InvalidEvidenceError(
            "test_report exit_code must be 0",
            {"got_exit_code": exit_code},
        )


def _validate_e2e_report(e: Evidence) -> None:
    passed = e.summary.get("passed", 0)
    if passed <= 0:
        raise InvalidEvidenceError(
            "e2e_report must have passed > 0",
            {"got_passed": passed},
        )


def _validate_error_log(e: Evidence) -> None:
    has_error = bool(e.summary.get("error"))
    has_artifact = bool(e.artifact_uri)
    if not (has_error or has_artifact):
        raise InvalidEvidenceError(
            "error_log must have error detail in summary or artifact_uri reference",
        )


def _validate_commit_ref(e: Evidence) -> None:
    commit_hash = e.summary.get("commit_hash", "")
    if not commit_hash:
        raise InvalidEvidenceError(
            "commit_ref must contain commit_hash in summary",
        )
    # Basic hex validation
    clean = commit_hash.strip()
    if len(clean) < 7 or not all(c in "0123456789abcdef" for c in clean.lower()):
        raise InvalidEvidenceError(
            "commit_hash must be 7-40 hex characters",
            {"got_hash": commit_hash},
        )


# Transition -> (required evidence type, validator function)
EVIDENCE_RULES: dict[tuple, dict] = {
    (VerifyStatus.PENDING, VerifyStatus.T2_PASS): {
        "required_type": "test_report",
        "validator": _validate_test_report,
    },
    (VerifyStatus.TESTING, VerifyStatus.T2_PASS): {
        "required_type": "test_report",
        "validator": _validate_test_report,
    },
    (VerifyStatus.T2_PASS, VerifyStatus.QA_PASS): {
        "required_type": "e2e_report",
        "validator": _validate_e2e_report,
    },
    (VerifyStatus.FAILED, VerifyStatus.PENDING): {
        "required_type": "commit_ref",
        "validator": _validate_commit_ref,
    },
}

# Transitions to FAILED accept any error_log from any prior status
_FAIL_SOURCES = [
    VerifyStatus.PENDING, VerifyStatus.TESTING,
    VerifyStatus.T2_PASS, VerifyStatus.QA_PASS,
]
for _src in _FAIL_SOURCES:
    EVIDENCE_RULES[(_src, VerifyStatus.FAILED)] = {
        "required_type": "error_log",
        "validator": _validate_error_log,
    }

# Transitions to WAIVED require manual_review (lenient)
for _src in [VerifyStatus.PENDING, VerifyStatus.FAILED]:
    EVIDENCE_RULES[(_src, VerifyStatus.WAIVED)] = {
        "required_type": "manual_review",
        "validator": lambda e: None,  # no structural validation for manual review
    }


def validate_evidence(
    from_status: VerifyStatus,
    to_status: VerifyStatus,
    evidence: Evidence,
) -> None:
    """Validate evidence for a state transition.

    Args:
        from_status: Current node status.
        to_status: Target node status.
        evidence: Structured evidence object.

    Raises:
        InvalidEvidenceError: If evidence type or content doesn't match rules.
    """
    rule = EVIDENCE_RULES.get((from_status, to_status))
    if rule is None:
        # No evidence rule for this transition — allow without evidence
        return

    required_type = rule["required_type"]
    if evidence.type != required_type:
        raise InvalidEvidenceError(
            f"Transition {from_status.value} -> {to_status.value} requires "
            f"evidence type {required_type!r}, got {evidence.type!r}",
            {"required_type": required_type, "got_type": evidence.type},
        )

    rule["validator"](evidence)
