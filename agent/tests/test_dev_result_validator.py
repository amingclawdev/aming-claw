"""Tests for the PR1 dev-stage preflight validator (output_schemas)."""
from __future__ import annotations

from agent.governance.output_schemas import (
    validate_dev_output,
    ValidationResult,
    ValidationError,
    error_codes,
)


def _pm_context(proposed_nodes=None, removed_nodes=None, unmapped_files=None):
    """Build a minimal dict-shaped chain_context with one PM stage."""
    return {
        "stages": [
            {
                "type": "pm",
                "result_core": {
                    "proposed_nodes": proposed_nodes or [],
                    "removed_nodes": removed_nodes or [],
                    "unmapped_files": unmapped_files or [],
                },
            }
        ]
    }


def _base_payload(**overrides):
    payload = {
        "summary": "did the thing",
        "changed_files": ["a.py"],
    }
    payload.update(overrides)
    return payload


def test_phantom_create_for_unmapped_file():
    ctx = _pm_context(unmapped_files=["legacy/old.py"])
    payload = _base_payload(graph_delta={
        "creates": [
            {"node_id": "L7.1", "primary": "legacy/old.py", "parent_layer": "L7"}
        ]
    })
    # strict mode keeps the code as an error so we can assert detection
    res = validate_dev_output(payload, ctx, mode="strict")
    assert isinstance(res, ValidationResult)
    codes = [e.code for e in res.errors]
    assert error_codes.PHANTOM_CREATE_FOR_UNMAPPED_FILE in codes
    assert res.valid is False

    # PR1b: PHANTOM_CREATE_FOR_UNMAPPED_FILE is FATAL — warn mode no longer
    # demotes it. It must surface as an error and force valid=False.
    res_warn = validate_dev_output(payload, ctx, mode="warn")
    assert res_warn.valid is False
    err_codes = [e.code for e in res_warn.errors]
    assert error_codes.PHANTOM_CREATE_FOR_UNMAPPED_FILE in err_codes


def test_phantom_for_declared_removed_is_fatal_in_warn_mode():
    """PR1b: PHANTOM_CREATE_FOR_DECLARED_REMOVED is FATAL even under warn."""
    ctx = _pm_context(removed_nodes=["L7.21"])
    payload = _base_payload(graph_delta={
        "creates": [
            {"node_id": "L7.21", "primary": "agent/governance/foo.py",
             "parent_layer": "L7"}
        ]
    })
    res = validate_dev_output(payload, ctx, mode="warn")
    assert res.valid is False
    err_codes = [e.code for e in res.errors]
    assert error_codes.PHANTOM_CREATE_FOR_DECLARED_REMOVED in err_codes


def test_phantom_for_unmapped_file_is_fatal_in_warn_mode():
    """PR1b: PHANTOM_CREATE_FOR_UNMAPPED_FILE is FATAL even under warn."""
    ctx = _pm_context(unmapped_files=["legacy/old.py"])
    payload = _base_payload(graph_delta={
        "creates": [
            {"node_id": "L7.5", "primary": "legacy/old.py",
             "parent_layer": "L7"}
        ]
    })
    res = validate_dev_output(payload, ctx, mode="warn")
    assert res.valid is False
    err_codes = [e.code for e in res.errors]
    assert error_codes.PHANTOM_CREATE_FOR_UNMAPPED_FILE in err_codes


def test_dev_role_prompt_mentions_validator():
    """PR1b R2/AC3: dev role prompt must point dev at the preflight validator."""
    from agent.role_permissions import ROLE_PROMPTS
    assert "validate_stage_output" in ROLE_PROMPTS["dev"]


def test_empty_node_id_fatal():
    payload = _base_payload(graph_delta={
        "creates": [{"node_id": "", "primary": "a.py", "parent_layer": "L7"}]
    })
    res = validate_dev_output(payload, None, mode="warn")
    assert res.valid is False
    assert any(e.code == error_codes.EMPTY_NODE_ID for e in res.errors)


def test_parent_layer_type_mix_fatal():
    payload = _base_payload(graph_delta={
        "creates": [
            {"node_id": "L7.1", "primary": "a.py", "parent_layer": "L7"},
            {"node_id": "L7.2", "primary": "b.py", "parent_layer": {"id": "L7"}},
        ]
    })
    res = validate_dev_output(payload, None, mode="warn")
    assert res.valid is False
    codes = [e.code for e in res.errors]
    assert error_codes.INVALID_PARENT_LAYER_TYPE in codes


def test_creates_not_in_proposed_warning():
    ctx = _pm_context(proposed_nodes=[{"node_id": "L7.1"}])
    payload = _base_payload(graph_delta={
        "creates": [
            {"node_id": "L7.99", "primary": "a.py", "parent_layer": "L7"}
        ]
    })
    res = validate_dev_output(payload, ctx, mode="warn")
    # Non-fatal — should be in warnings, not errors, under warn mode.
    assert res.valid is True
    warn_codes = [w.code for w in res.warnings]
    assert error_codes.CREATE_NOT_IN_PROPOSED_NODES in warn_codes

    # In strict mode it stays as an error.
    res_strict = validate_dev_output(payload, ctx, mode="strict")
    assert res_strict.valid is False
    err_codes = [e.code for e in res_strict.errors]
    assert error_codes.CREATE_NOT_IN_PROPOSED_NODES in err_codes


def test_missing_required_field_fatal():
    payload = {"changed_files": ["a.py"]}  # missing 'summary'
    res = validate_dev_output(payload, None, mode="warn")
    assert res.valid is False
    err_codes = [e.code for e in res.errors]
    assert error_codes.MISSING_REQUIRED_FIELD in err_codes
    # The missing-field error must reference 'summary'.
    assert any("summary" in e.field_path for e in res.errors
               if e.code == error_codes.MISSING_REQUIRED_FIELD)


def test_unauthorized_self_waiver_rejected():
    payload = _base_payload(bypass_validations=True)
    res = validate_dev_output(payload, None, mode="warn")
    assert res.valid is False
    assert any(e.code == error_codes.UNAUTHORIZED_SELF_WAIVER for e in res.errors)


def test_happy_path_valid():
    ctx = _pm_context(proposed_nodes=[{"node_id": "L7.1"}])
    payload = _base_payload(graph_delta={
        "creates": [
            {"node_id": "L7.1", "primary": "a.py", "parent_layer": "L7"}
        ]
    })
    res = validate_dev_output(payload, ctx, mode="warn")
    assert res.valid is True
    assert res.errors == []
    # Sanity-check the machine + human serializers
    machine = res.to_machine_json()
    assert machine["valid"] is True
    assert machine["schema_version"] == "v1"
    assert isinstance(res.to_human_readable(), str)


def test_empty_node_id_demoted_when_pm_also_empty():
    """PR1f: dev node_id='' demoted to CREATE_NOT_IN_PROPOSED_NODES warning
    when PM proposed_nodes also has no concrete id for the same primary.

    PM is a proposer, not an ID allocator — when PM emits proposed_nodes
    with id=None for a primary, dev passing through with node_id='' is
    acceptable; auto-inferrer Rule H assigns the id downstream.
    """
    ctx = _pm_context(proposed_nodes=[
        {"node_id": None, "primary": "agent/governance/new_feature.py"},
    ])
    payload = _base_payload(graph_delta={
        "creates": [
            {"node_id": "", "primary": "agent/governance/new_feature.py",
             "parent_layer": "L7"}
        ]
    })
    res = validate_dev_output(payload, ctx, mode="warn")
    assert res.valid is True, (
        f"expected valid=True with EMPTY_NODE_ID demoted; got errors={res.errors}"
    )
    err_codes = [e.code for e in res.errors]
    assert error_codes.EMPTY_NODE_ID not in err_codes
    warn_codes = [w.code for w in res.warnings]
    assert error_codes.CREATE_NOT_IN_PROPOSED_NODES in warn_codes, (
        f"expected CREATE_NOT_IN_PROPOSED_NODES warning; got warnings={res.warnings}"
    )


def test_empty_node_id_fatal_when_pm_has_concrete_id():
    """PR1f anti-regression: dev node_id='' STAYS FATAL when PM had a
    concrete id for the same primary — protects against dev silently
    dropping a PM-allocated id."""
    ctx = _pm_context(proposed_nodes=[
        {"node_id": "L7.99", "primary": "agent/governance/new_feature.py"},
    ])
    payload = _base_payload(graph_delta={
        "creates": [
            {"node_id": "", "primary": "agent/governance/new_feature.py",
             "parent_layer": "L7"}
        ]
    })
    res = validate_dev_output(payload, ctx, mode="warn")
    assert res.valid is False
    err_codes = [e.code for e in res.errors]
    assert error_codes.EMPTY_NODE_ID in err_codes


def test_empty_node_id_fatal_when_no_pm_context():
    """PR1f anti-regression: chain_context=None → EMPTY_NODE_ID stays FATAL.

    Without a PM stage in chain_context, the validator cannot tell whether
    PM acted as a proposer; safest behaviour is to keep EMPTY_NODE_ID FATAL.
    """
    payload = _base_payload(graph_delta={
        "creates": [
            {"node_id": "", "primary": "agent/governance/new_feature.py",
             "parent_layer": "L7"}
        ]
    })
    res = validate_dev_output(payload, None, mode="warn")
    assert res.valid is False
    err_codes = [e.code for e in res.errors]
    assert error_codes.EMPTY_NODE_ID in err_codes


def test_observer_emergency_bypass_demotes():
    """mode='disabled' should empty errors and set valid=True."""
    payload = _base_payload(
        bypass_validations=True,  # would be fatal under strict/warn
        graph_delta={"creates": [{"node_id": "", "parent_layer": "L7"}]},
    )
    res_strict = validate_dev_output(payload, None, mode="strict")
    assert res_strict.valid is False
    assert len(res_strict.errors) >= 2  # waiver + empty node_id

    res_disabled = validate_dev_output(payload, None, mode="disabled")
    assert res_disabled.valid is True
    assert res_disabled.errors == []
    # All violations were demoted to warnings.
    assert len(res_disabled.warnings) >= 2
    assert all(isinstance(w, ValidationError) for w in res_disabled.warnings)
    assert all(w.severity == "warning" for w in res_disabled.warnings)
