"""Tests for the PR1d PM-stage preflight validator (output_schemas).

Covers the MISSING_DECLARATION_FOR_DELETED_FILE check + the
_validate_pm_at_transition wiring inside auto_chain.
"""
from __future__ import annotations

import os

from agent.governance.output_schemas import (
    error_codes,
    validate_pm_output,
)


# --------------------------------------------------------------------------- #
# Validator unit tests                                                        #
# --------------------------------------------------------------------------- #

def test_pm_with_delete_ac_missing_declarations_fatal():
    """target_files non-empty + delete-keyword AC + empty declarations → FATAL.

    Under mode='warn' the new code MUST stay as an error (FATAL_CODES) and
    drive valid=False, mirroring the dev-side phantom-create FATAL behavior.
    """
    payload = {
        "target_files": ["x.py"],
        "acceptance_criteria": ["DELETE the legacy module"],
        "removed_nodes": [],
        "unmapped_files": [],
    }
    res = validate_pm_output(payload, None, mode="warn")
    assert res.valid is False
    err_codes = [e.code for e in res.errors]
    assert error_codes.MISSING_DECLARATION_FOR_DELETED_FILE in err_codes
    # Severity must be 'error' (FATAL stays an error in warn mode).
    matching = [e for e in res.errors
                if e.code == error_codes.MISSING_DECLARATION_FOR_DELETED_FILE]
    assert matching, "expected at least one MISSING_DECLARATION_FOR_DELETED_FILE error"
    assert all(e.severity == "error" for e in matching)
    # suggested_fix should be set so PM gets a useful retry hint.
    assert all(e.suggested_fix for e in matching)


def test_pm_with_delete_ac_proper_declarations_pass():
    """delete-keyword AC + populated declarations → valid=True."""
    # Case A: removed_nodes populated.
    payload_a = {
        "target_files": ["agent/legacy/old.py"],
        "acceptance_criteria": ["DELETE agent/legacy/old.py"],
        "removed_nodes": ["L7.21"],
        "unmapped_files": [],
    }
    res_a = validate_pm_output(payload_a, None, mode="warn")
    assert res_a.valid is True
    assert all(
        e.code != error_codes.MISSING_DECLARATION_FOR_DELETED_FILE
        for e in res_a.errors
    )

    # Case B: only unmapped_files populated.
    payload_b = {
        "target_files": ["agent/legacy/old.py"],
        "acceptance_criteria": ["remove agent/legacy/old.py"],
        "removed_nodes": [],
        "unmapped_files": ["agent/legacy/old.py"],
    }
    res_b = validate_pm_output(payload_b, None, mode="warn")
    assert res_b.valid is True
    assert all(
        e.code != error_codes.MISSING_DECLARATION_FOR_DELETED_FILE
        for e in res_b.errors
    )

    # Case C: replaced_by keyword.
    payload_c = {
        "target_files": ["agent/legacy/old.py"],
        "acceptance_criteria": [
            "module replaced_by agent/new/replacement.py"
        ],
        "removed_nodes": ["L7.99"],
        "unmapped_files": [],
    }
    res_c = validate_pm_output(payload_c, None, mode="warn")
    assert res_c.valid is True


def test_pm_with_no_delete_ac_declarations_optional():
    """No delete keywords in AC → declarations not required, valid=True."""
    payload = {
        "target_files": ["agent/governance/new_feature.py"],
        "acceptance_criteria": [
            "add new feature",
            "all existing tests still pass",
            "verification command exits zero",
        ],
        "removed_nodes": [],
        "unmapped_files": [],
    }
    res = validate_pm_output(payload, None, mode="warn")
    assert res.valid is True
    assert all(
        e.code != error_codes.MISSING_DECLARATION_FOR_DELETED_FILE
        for e in res.errors
    )

    # Also verify case-insensitivity: substrings 'DELETE'/'delete' both trip
    # the keyword scan, but a plain word like 'add' / 'enhance' / 'wire' must
    # NOT trip it (negative case for the case-insensitive scan logic).
    payload_neg = {
        "target_files": ["x.py"],
        "acceptance_criteria": ["enhance the parser", "wire new flag"],
        "removed_nodes": [],
        "unmapped_files": [],
    }
    res_neg = validate_pm_output(payload_neg, None, mode="warn")
    assert res_neg.valid is True


# --------------------------------------------------------------------------- #
# Auto-chain wiring                                                           #
# --------------------------------------------------------------------------- #

def test_pm_validator_wired_into_auto_chain():
    """_validate_pm_at_transition is importable and blocks bad PM output.

    Calls the function with a stub conn=None and a payload that should fail
    the validator (delete-keyword AC + empty declarations). Asserts the
    function returns False so that on_task_completed will emit the
    preflight_blocked dict instead of dispatching the dev stage.
    """
    # Force warn mode so the FATAL code surfaces (matches default).
    os.environ["OPT_PREFLIGHT_VALIDATOR_MODE"] = "warn"
    try:
        from agent.governance.auto_chain import _validate_pm_at_transition
    finally:
        # Don't pollute later tests' env if import side-effected anything.
        pass

    bad_payload = {
        "target_files": ["agent/legacy/old.py"],
        "acceptance_criteria": ["DELETE agent/legacy/old.py"],
        "removed_nodes": [],
        "unmapped_files": [],
    }
    # observer_emergency_bypass off — must NOT short-circuit to True.
    metadata = {}
    result = _validate_pm_at_transition(
        conn=None,
        project_id="aming-claw",
        task_id="task-test-pm-validator",
        result=bad_payload,
        metadata=metadata,
    )
    assert result is False, (
        "expected _validate_pm_at_transition to return False for a payload "
        "missing declarations; got True instead"
    )
