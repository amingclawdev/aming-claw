"""Tests for the PR1f PM-stage preflight validator (output_schemas).

Anchors `validate_pm_output` and `_is_empty_list` against the S1–S7 structural
checks enumerated in `agent/governance/output_schemas/pm_result_schema.py`.

Cluster: 2839d2aa5c401d9b (agent/governance/output_schemas/{dev,pm}_result_schema.py).
"""
from __future__ import annotations

from agent.governance.output_schemas import (
    ValidationResult,
    error_codes,
    validate_pm_output,
)
from agent.governance.output_schemas.pm_result_schema import _is_empty_list


# ---------------------------------------------------------------------------
# _is_empty_list helper
# ---------------------------------------------------------------------------


def test_is_empty_list_treats_none_as_empty():
    assert _is_empty_list(None) is True


def test_is_empty_list_treats_empty_list_as_empty():
    assert _is_empty_list([]) is True


def test_is_empty_list_treats_non_empty_list_as_non_empty():
    assert _is_empty_list(["x"]) is False


def test_is_empty_list_treats_non_list_non_none_as_not_empty():
    # Non-list, non-None values pass through (return False) so the downstream
    # `isinstance(value, list)` type check fires the structural error rather
    # than being silently swapped via the prd-fallback path.
    assert _is_empty_list({"primary": "a.py"}) is False
    assert _is_empty_list("a string") is False
    assert _is_empty_list(0) is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_payload(**overrides):
    """Minimal happy-path PM result payload."""
    payload = {
        "acceptance_criteria": ["AC1: feature works"],
        "target_files": ["agent/foo.py"],
        "proposed_nodes": [
            {"node_id": None, "primary": "agent/foo.py", "title": "Foo"}
        ],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# S1: payload-not-dict
# ---------------------------------------------------------------------------


def test_s1_payload_not_dict_returns_malformed_json():
    res = validate_pm_output("not a dict", mode="warn")
    assert isinstance(res, ValidationResult)
    assert res.valid is False
    codes = [e.code for e in res.errors]
    assert error_codes.MALFORMED_JSON in codes


def test_s1_payload_none_returns_malformed_json_strict():
    res = validate_pm_output(None, mode="strict")
    assert res.valid is False
    codes = [e.code for e in res.errors]
    assert error_codes.MALFORMED_JSON in codes


# ---------------------------------------------------------------------------
# S2: acceptance_criteria absent / not list / empty
# ---------------------------------------------------------------------------


def test_s2_acceptance_criteria_missing_is_fatal():
    payload = {
        "target_files": ["agent/foo.py"],
        "proposed_nodes": [
            {"node_id": None, "primary": "agent/foo.py", "title": "Foo"}
        ],
    }
    res = validate_pm_output(payload, mode="warn")
    assert res.valid is False
    codes = [e.code for e in res.errors]
    assert error_codes.MISSING_REQUIRED_FIELD in codes
    assert any(
        e.field_path == "$.acceptance_criteria" for e in res.errors
    )


def test_s2_acceptance_criteria_not_a_list_is_fatal():
    payload = _valid_payload(acceptance_criteria="AC1 as a string")
    res = validate_pm_output(payload, mode="warn")
    assert res.valid is False
    codes = [e.code for e in res.errors]
    assert error_codes.MISSING_REQUIRED_FIELD in codes
    assert any(
        "list of strings" in (e.message or "") for e in res.errors
    )


def test_s2_acceptance_criteria_empty_list_is_fatal():
    payload = _valid_payload(acceptance_criteria=[])
    res = validate_pm_output(payload, mode="warn")
    assert res.valid is False
    codes = [e.code for e in res.errors]
    assert error_codes.MISSING_REQUIRED_FIELD in codes
    assert any(
        "non-empty" in (e.message or "") for e in res.errors
    )


# ---------------------------------------------------------------------------
# S3: non-string AC element
# ---------------------------------------------------------------------------


def test_s3_non_string_ac_element_is_fatal():
    payload = _valid_payload(acceptance_criteria=["AC1 ok", 42])
    res = validate_pm_output(payload, mode="warn")
    assert res.valid is False
    bad = [
        e
        for e in res.errors
        if e.code == error_codes.MISSING_REQUIRED_FIELD
        and "acceptance_criteria[1]" in e.field_path
    ]
    assert bad, f"expected an error pointing at acceptance_criteria[1]; got {res.errors}"


def test_s3_dict_ac_element_is_fatal():
    payload = _valid_payload(
        acceptance_criteria=[{"id": "AC1", "text": "hi"}]
    )
    res = validate_pm_output(payload, mode="warn")
    assert res.valid is False
    assert any(
        e.code == error_codes.MISSING_REQUIRED_FIELD
        and "acceptance_criteria[0]" in e.field_path
        for e in res.errors
    )


# ---------------------------------------------------------------------------
# S4: empty work scope (target_files AND proposed_nodes both empty)
# ---------------------------------------------------------------------------


def test_s4_empty_work_scope_is_fatal():
    payload = {
        "acceptance_criteria": ["AC1"],
        "target_files": [],
        "proposed_nodes": [],
    }
    res = validate_pm_output(payload, mode="warn")
    assert res.valid is False
    bad = [
        e
        for e in res.errors
        if e.code == error_codes.MISSING_REQUIRED_FIELD
        and e.field_path == "$.target_files"
    ]
    assert bad, f"expected MISSING_REQUIRED_FIELD on $.target_files; got {res.errors}"
    assert "work scope is empty" in bad[0].message


def test_s4_target_files_present_satisfies_scope():
    payload = {
        "acceptance_criteria": ["AC1"],
        "target_files": ["agent/foo.py"],
        # proposed_nodes intentionally absent
    }
    res = validate_pm_output(payload, mode="warn")
    # Structural ok — no MISSING_REQUIRED_FIELD on target_files.
    assert not any(
        e.field_path == "$.target_files" for e in res.errors
    ), f"unexpected scope error: {res.errors}"


def test_s4_proposed_nodes_only_satisfies_scope():
    payload = {
        "acceptance_criteria": ["AC1"],
        "proposed_nodes": [
            {"node_id": None, "primary": "agent/foo.py", "title": "Foo"}
        ],
    }
    res = validate_pm_output(payload, mode="warn")
    assert not any(
        e.field_path == "$.target_files" for e in res.errors
    )


# ---------------------------------------------------------------------------
# S5: proposed_nodes element missing 'primary'
# ---------------------------------------------------------------------------


def test_s5_proposed_node_missing_primary_is_fatal():
    payload = {
        "acceptance_criteria": ["AC1"],
        "target_files": ["agent/foo.py"],
        "proposed_nodes": [
            {"node_id": None, "title": "Foo"}  # no 'primary' at all
        ],
    }
    res = validate_pm_output(payload, mode="warn")
    assert res.valid is False
    assert any(
        e.code == error_codes.MISSING_REQUIRED_FIELD
        and "$.proposed_nodes[0].primary" in e.field_path
        for e in res.errors
    )


def test_s5_proposed_node_empty_string_primary_is_fatal():
    payload = {
        "acceptance_criteria": ["AC1"],
        "target_files": ["agent/foo.py"],
        "proposed_nodes": [
            {"node_id": None, "primary": "   ", "title": "Foo"}
        ],
    }
    res = validate_pm_output(payload, mode="warn")
    assert res.valid is False
    assert any(
        "$.proposed_nodes[0].primary" in e.field_path for e in res.errors
    )


def test_s5_proposed_node_list_primary_with_strings_is_ok():
    payload = {
        "acceptance_criteria": ["AC1"],
        "proposed_nodes": [
            {
                "node_id": None,
                "primary": ["agent/foo.py", "agent/bar.py"],
                "title": "Foo",
            }
        ],
    }
    res = validate_pm_output(payload, mode="warn")
    # No primary-shaped error
    assert not any(
        "$.proposed_nodes[0].primary" in e.field_path for e in res.errors
    ), f"unexpected primary error: {res.errors}"


def test_s5_proposed_node_not_a_dict_is_fatal():
    payload = {
        "acceptance_criteria": ["AC1"],
        "target_files": ["agent/foo.py"],
        "proposed_nodes": ["not-a-dict"],
    }
    res = validate_pm_output(payload, mode="warn")
    assert res.valid is False
    assert any(
        e.code == error_codes.MISSING_REQUIRED_FIELD
        and "$.proposed_nodes[0]" in e.field_path
        for e in res.errors
    )


# ---------------------------------------------------------------------------
# S6: removed_nodes / unmapped_files non-list when present
# ---------------------------------------------------------------------------


def test_s6_removed_nodes_non_list_is_fatal():
    payload = _valid_payload(removed_nodes="L7.1")
    res = validate_pm_output(payload, mode="warn")
    assert res.valid is False
    assert any(
        e.code == error_codes.MALFORMED_JSON
        and e.field_path == "$.removed_nodes"
        for e in res.errors
    )


def test_s6_unmapped_files_non_list_is_fatal():
    payload = _valid_payload(unmapped_files={"file": "legacy/old.py"})
    res = validate_pm_output(payload, mode="warn")
    assert res.valid is False
    assert any(
        e.code == error_codes.MALFORMED_JSON
        and e.field_path == "$.unmapped_files"
        for e in res.errors
    )


def test_s6_removed_nodes_absent_is_ok():
    payload = _valid_payload()  # no removed_nodes / unmapped_files at all
    res = validate_pm_output(payload, mode="warn")
    # No MALFORMED_JSON for these fields when absent.
    assert not any(
        e.field_path in ("$.removed_nodes", "$.unmapped_files")
        for e in res.errors
    )


# ---------------------------------------------------------------------------
# S7: bypass_validations key present
# ---------------------------------------------------------------------------


def test_s7_bypass_validations_is_fatal():
    payload = _valid_payload(bypass_validations=True)
    res = validate_pm_output(payload, mode="warn")
    assert res.valid is False
    assert any(
        e.code == error_codes.UNAUTHORIZED_SELF_WAIVER for e in res.errors
    )


def test_s7_bypass_validations_false_still_fatal():
    """Presence of the key — not its truthiness — triggers S7."""
    payload = _valid_payload(bypass_validations=False)
    res = validate_pm_output(payload, mode="warn")
    assert res.valid is False
    assert any(
        e.code == error_codes.UNAUTHORIZED_SELF_WAIVER for e in res.errors
    )


# ---------------------------------------------------------------------------
# Mode behaviour + serializers
# ---------------------------------------------------------------------------


def test_disabled_mode_demotes_everything():
    payload = _valid_payload(
        acceptance_criteria=[],  # would be fatal under warn/strict
        bypass_validations=True,  # would be fatal under warn/strict
    )
    res = validate_pm_output(payload, mode="disabled")
    assert res.valid is True
    assert res.errors == []
    assert all(w.severity == "warning" for w in res.warnings)


def test_strict_mode_keeps_errors_as_errors():
    payload = _valid_payload(acceptance_criteria=[])
    res = validate_pm_output(payload, mode="strict")
    assert res.valid is False
    assert any(
        e.code == error_codes.MISSING_REQUIRED_FIELD for e in res.errors
    )
    assert all(e.severity == "error" for e in res.errors)


def test_happy_path_valid():
    payload = _valid_payload()
    res = validate_pm_output(payload, mode="warn")
    assert res.valid is True
    assert res.errors == []
    machine = res.to_machine_json()
    assert machine["valid"] is True
    assert machine["schema_version"] == "v1"
    human = res.to_human_readable()
    assert isinstance(human, str)
    assert "ValidationResult" in human


def test_prd_fallback_for_acceptance_criteria():
    """Legacy PM payloads nest the declarations under a 'prd' sub-dict —
    validator must accept either top-level or prd-nested forms."""
    payload = {
        "prd": {
            "acceptance_criteria": ["AC1: prd-nested"],
            "target_files": ["agent/foo.py"],
        }
    }
    res = validate_pm_output(payload, mode="warn")
    assert res.valid is True, f"expected prd fallback to satisfy validator; got {res.errors}"
