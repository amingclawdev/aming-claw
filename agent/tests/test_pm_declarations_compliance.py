"""Tests for the PR1f PM-stage preflight validator (output_schemas).

PR1f rewrote the validator to be STRUCTURAL ONLY. The prior PR1d
delete-keyword substring scan was deleted because it over-fired on
feature-description ACs (e.g. "Rule J respects dev removes"). These tests
cover the new S1–S7 structural checks. All tests use mode='warn' to match
production wiring (FATAL codes still drive valid=False, non-FATAL demote
to warnings).
"""
from __future__ import annotations

from agent.governance.output_schemas import (
    error_codes,
    validate_pm_output,
)


# --------------------------------------------------------------------------- #
# Structural FATAL checks (S1 – S7)                                           #
# --------------------------------------------------------------------------- #

def test_payload_not_dict_fatal():
    """S1: payload is not a JSON object → MALFORMED_JSON FATAL."""
    res = validate_pm_output("not a dict", None, mode="warn")  # type: ignore[arg-type]
    assert res.valid is False
    assert any(e.code == error_codes.MALFORMED_JSON for e in res.errors)


def test_missing_acceptance_criteria_fatal():
    """S2: acceptance_criteria absent → MISSING_REQUIRED_FIELD FATAL."""
    payload = {
        "target_files": ["x.py"],
        # acceptance_criteria intentionally absent
    }
    res = validate_pm_output(payload, None, mode="warn")
    assert res.valid is False
    err = [e for e in res.errors
           if e.code == error_codes.MISSING_REQUIRED_FIELD
           and "acceptance_criteria" in e.field_path]
    assert err, f"expected MISSING_REQUIRED_FIELD on acceptance_criteria; got {res.errors}"


def test_empty_acceptance_criteria_fatal():
    """S2: acceptance_criteria=[] → MISSING_REQUIRED_FIELD FATAL."""
    payload = {
        "target_files": ["x.py"],
        "acceptance_criteria": [],
    }
    res = validate_pm_output(payload, None, mode="warn")
    assert res.valid is False
    err = [e for e in res.errors
           if e.code == error_codes.MISSING_REQUIRED_FIELD
           and "acceptance_criteria" in e.field_path]
    assert err


def test_acceptance_criteria_non_string_element_fatal():
    """S3: any AC element is not a string → MISSING_REQUIRED_FIELD FATAL."""
    payload = {
        "target_files": ["x.py"],
        "acceptance_criteria": ["valid AC", 42, {"oops": True}],
    }
    res = validate_pm_output(payload, None, mode="warn")
    assert res.valid is False
    bad_paths = [e.field_path for e in res.errors
                 if e.code == error_codes.MISSING_REQUIRED_FIELD
                 and "acceptance_criteria[" in e.field_path]
    assert bad_paths, f"expected per-element MISSING_REQUIRED_FIELD errors; got {res.errors}"


def test_empty_work_scope_fatal():
    """S4: target_files empty AND proposed_nodes empty → MISSING_REQUIRED_FIELD FATAL."""
    payload = {
        "target_files": [],
        "proposed_nodes": [],
        "acceptance_criteria": ["do something"],
    }
    res = validate_pm_output(payload, None, mode="warn")
    assert res.valid is False
    err = [e for e in res.errors
           if e.code == error_codes.MISSING_REQUIRED_FIELD
           and "target_files" in e.field_path]
    assert err


def test_proposed_node_missing_primary_fatal():
    """S5: proposed_nodes element missing non-empty primary → MISSING_REQUIRED_FIELD FATAL."""
    payload = {
        "acceptance_criteria": ["AC1"],
        "proposed_nodes": [
            {"node_id": "L7.1", "primary": "agent/foo.py"},
            {"node_id": "L7.2"},  # missing primary
            {"node_id": "L7.3", "primary": ""},  # empty primary
        ],
    }
    res = validate_pm_output(payload, None, mode="warn")
    assert res.valid is False
    bad = [e for e in res.errors
           if e.code == error_codes.MISSING_REQUIRED_FIELD
           and "proposed_nodes[" in e.field_path
           and "primary" in e.field_path]
    assert len(bad) >= 2, f"expected >=2 missing-primary errors; got {res.errors}"


def test_removed_nodes_wrong_type_fatal():
    """S6: removed_nodes present but not a list → MALFORMED_JSON FATAL."""
    payload = {
        "target_files": ["x.py"],
        "acceptance_criteria": ["AC1"],
        "removed_nodes": "L7.1",  # string instead of list
    }
    res = validate_pm_output(payload, None, mode="warn")
    assert res.valid is False
    err = [e for e in res.errors
           if e.code == error_codes.MALFORMED_JSON
           and "removed_nodes" in e.field_path]
    assert err


def test_unmapped_files_wrong_type_fatal():
    """S6: unmapped_files present but not a list → MALFORMED_JSON FATAL."""
    payload = {
        "target_files": ["x.py"],
        "acceptance_criteria": ["AC1"],
        "unmapped_files": {"oops": True},  # dict instead of list
    }
    res = validate_pm_output(payload, None, mode="warn")
    assert res.valid is False
    err = [e for e in res.errors
           if e.code == error_codes.MALFORMED_JSON
           and "unmapped_files" in e.field_path]
    assert err


def test_bypass_validations_self_waiver_fatal():
    """S7: bypass_validations key present → UNAUTHORIZED_SELF_WAIVER FATAL."""
    payload = {
        "target_files": ["x.py"],
        "acceptance_criteria": ["AC1"],
        "bypass_validations": True,
    }
    res = validate_pm_output(payload, None, mode="warn")
    assert res.valid is False
    assert any(e.code == error_codes.UNAUTHORIZED_SELF_WAIVER for e in res.errors)


# --------------------------------------------------------------------------- #
# Happy paths                                                                 #
# --------------------------------------------------------------------------- #

def test_valid_minimal_pm_output_passes():
    """Smallest legal payload — single AC + single target_file — passes."""
    payload = {
        "target_files": ["agent/foo.py"],
        "acceptance_criteria": ["AC1: implement foo"],
    }
    res = validate_pm_output(payload, None, mode="warn")
    assert res.valid is True, f"expected valid; got errors={res.errors}"


def test_valid_full_pm_output_passes():
    """Realistic payload with all optional fields populated structurally."""
    payload = {
        "target_files": ["agent/governance/foo.py"],
        "acceptance_criteria": [
            "AC1: feature works",
            "AC2: tests pass",
            "AC3: docs updated",
        ],
        "proposed_nodes": [
            {"node_id": "L7.1", "primary": "agent/governance/foo.py"},
            {"primary": "agent/governance/bar.py"},  # PM-as-proposer (no id)
        ],
        "removed_nodes": ["L7.21"],
        "unmapped_files": ["agent/legacy/old.py"],
        "verification": {"method": "pytest", "command": "pytest -v"},
    }
    res = validate_pm_output(payload, None, mode="warn")
    assert res.valid is True, f"expected valid; got errors={res.errors}"


def test_valid_prd_fallback_passes():
    """PRD sub-dict supplies acceptance_criteria + target_files — must pass."""
    payload = {
        "prd": {
            "target_files": ["agent/foo.py"],
            "acceptance_criteria": ["AC1: implement foo"],
        },
    }
    res = validate_pm_output(payload, None, mode="warn")
    assert res.valid is True, f"expected valid via prd fallback; got {res.errors}"


# --------------------------------------------------------------------------- #
# S5 list[str] primary acceptance (PR1g)                                      #
# --------------------------------------------------------------------------- #

def test_proposed_node_primary_as_list_passes():
    """S5: proposed_nodes element with primary=['agent/foo.py'] is accepted."""
    payload = {
        "acceptance_criteria": ["AC1"],
        "target_files": ["agent/foo.py"],
        "proposed_nodes": [
            {"primary": ["agent/foo.py"], "title": "X"},
        ],
    }
    res = validate_pm_output(payload, None, mode="warn")
    assert res.valid is True, f"expected valid for list[str] primary; got {res.errors}"
    bad = [e for e in res.errors
           if e.code == error_codes.MISSING_REQUIRED_FIELD
           and "proposed_nodes[0].primary" in e.field_path]
    assert not bad, f"unexpected primary errors: {bad}"


def test_proposed_node_primary_as_list_with_multiple_paths_passes():
    """S5: proposed_nodes element with multi-element list[str] primary is accepted."""
    payload = {
        "acceptance_criteria": ["AC1"],
        "target_files": ["agent/foo.py"],
        "proposed_nodes": [
            {
                "primary": [
                    "agent/foo.py",
                    "agent/bar.py",
                    "agent/governance/baz.py",
                ],
                "title": "Multi-path node",
            },
        ],
    }
    res = validate_pm_output(payload, None, mode="warn")
    assert res.valid is True, f"expected valid for multi-path list primary; got {res.errors}"
    bad = [e for e in res.errors
           if e.code == error_codes.MISSING_REQUIRED_FIELD
           and "proposed_nodes[0].primary" in e.field_path]
    assert not bad, f"unexpected primary errors: {bad}"


def test_proposed_node_primary_as_empty_list_fails():
    """S5: proposed_nodes element with primary=[] still fails."""
    payload = {
        "acceptance_criteria": ["AC1"],
        "proposed_nodes": [
            {"primary": [], "title": "X"},
        ],
    }
    res = validate_pm_output(payload, None, mode="warn")
    assert res.valid is False, "empty list primary must fail"
    bad = [e for e in res.errors
           if e.code == error_codes.MISSING_REQUIRED_FIELD
           and "proposed_nodes[0].primary" in e.field_path]
    assert bad, f"expected MISSING_REQUIRED_FIELD on primary; got {res.errors}"


def test_proposed_node_primary_as_list_with_empty_string_fails():
    """S5: proposed_nodes element with primary=[''] (or whitespace-only) fails."""
    payload = {
        "acceptance_criteria": ["AC1"],
        "proposed_nodes": [
            {"primary": ["agent/foo.py", ""], "title": "X"},
        ],
    }
    res = validate_pm_output(payload, None, mode="warn")
    assert res.valid is False, "list with empty string element must fail"
    bad = [e for e in res.errors
           if e.code == error_codes.MISSING_REQUIRED_FIELD
           and "proposed_nodes[0].primary" in e.field_path]
    assert bad, f"expected MISSING_REQUIRED_FIELD on primary; got {res.errors}"
