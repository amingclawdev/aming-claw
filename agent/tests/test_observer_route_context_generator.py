"""Tests for the Aming-owned write-authorizing observer route-token generator.

The generator (``agent.governance.observer_route_context``) mints a route token
that must be accepted by
``mf_subagent_contract.validate_route_token_mutation_gate`` with decision
``route_token`` for observer protected write actions when scope matches, while
keeping direct file-edit actions blocked, staying independent of any external
route provider at runtime, and never persisting the raw token in clear.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys

import pytest


_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from agent.governance import observer_route_context
from agent.governance.mf_subagent_contract import (
    MfSubagentContractError,
    validate_route_token_mutation_gate,
)


_PROJECT = "aming-claw"
_BACKLOG = "AC-OBSERVER-WRITE-AUTH-ROUTE-CONTEXT-GEN-20260608"
_TASK = "task-ac-observer-write-20260609-a"
_TARGET_FILES = [
    "agent/governance/observer_route_context.py",
    "agent/governance/server.py",
]
_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
_PARENT_ROUTE_IDENTITY = {
    "route_id": "event.route_prompt_context.preview",
    "route_context_hash": (
        "sha256:7125c02c654a700e97fb9dc1b8f98ff0d17bd8ee5c87bf5d719e9e99e17fcf0a"
    ),
    "prompt_contract_id": "rprompt-aming-91fa67fa58d0ae34",
    "prompt_contract_hash": (
        "sha256:91fa67fa58d0ae3457f2dd4d0f35059c07a9392c8a02882a80441f7e748fda92"
    ),
    "visible_injection_manifest_hash": (
        "sha256:da5eec8c1af2bf002628037de2fe9bc4cbfb9290ea60a6057004edb050379396"
    ),
    "route_token_ref": "rtok-e66f150c6cf5672dbaad43886bb7368f",
    "selected_project": _PROJECT,
    "selected_backlog_id": _BACKLOG,
}


def _token(**overrides):
    kwargs = dict(
        project_id=_PROJECT,
        backlog_id=_BACKLOG,
        task_id=_TASK,
        target_files=_TARGET_FILES,
        now=_NOW,
    )
    kwargs.update(overrides)
    return observer_route_context.build_observer_write_route_token(**kwargs)


def _issue(**overrides):
    kwargs = dict(
        project_id=_PROJECT,
        backlog_id=_BACKLOG,
        task_id=_TASK,
        target_files=_TARGET_FILES,
        now=_NOW,
    )
    kwargs.update(overrides)
    return observer_route_context.issue_observer_write_route_context(**kwargs)


def _assert_no_raw_token_keys(value):
    raw_keys = {
        "route_token",
        "raw_route_token",
        "token",
        "token_body",
        "session_token",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            assert key not in raw_keys
            _assert_no_raw_token_keys(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_raw_token_keys(item)


# --- AC2: gate acceptance ---------------------------------------------------


def test_gate_accepts_token_for_task_timeline_append():
    token = _token()
    result = validate_route_token_mutation_gate(
        {"route_token": token},
        action="task_timeline_append",
        project_id=_PROJECT,
        backlog_id=_BACKLOG,
        task_id=_TASK,
        now=_NOW,
    )
    assert result["decision"] == "route_token"
    assert result["allowed"] is True
    assert result["caller_role"] == "observer"


def test_gate_accepts_token_for_backlog_close():
    token = _token()
    result = validate_route_token_mutation_gate(
        {"route_token": token},
        action="backlog_close",
        project_id=_PROJECT,
        backlog_id=_BACKLOG,
        task_id=_TASK,
        now=_NOW,
    )
    assert result["decision"] == "route_token"


def test_gate_accepts_token_for_execute_backlog_row():
    token = _token()
    result = validate_route_token_mutation_gate(
        {"route_token": token},
        action="execute_backlog_row",
        project_id=_PROJECT,
        backlog_id=_BACKLOG,
        task_id=_TASK,
        now=_NOW,
    )
    assert result["decision"] == "route_token"


# --- AC1: write-authorizing, aming-owned, no external provider --------------


def test_token_authorizes_protected_write_and_aming_owned():
    token = _token()
    assert token["authorizes_protected_write"] is True
    assert token["read_only"] is False
    assert token["owner"] == "aming-claw"
    assert token["external_provider_required"] is False
    assert token["judgment_brain_required"] is False
    assert token["schema_version"] == "aming_observer_write_route_token.v1"
    assert token["route_id"].startswith("route-20260609-")
    assert token["route_context_hash"].startswith("sha256:")
    assert token["prompt_contract_id"].startswith("rprompt-aming-")
    assert token["visible_injection_manifest_hash"].startswith("sha256:")
    assert token["evidence_refs"]
    assert token["evidence_refs"][0].startswith("route:")


# --- AC3 / AC7: no auth over-reach — direct edits stay blocked --------------


def test_token_blocks_direct_edit_actions():
    token = _token()
    allowed = set(token["allowed_actions"])
    blocked = set(token["blocked_actions"])
    # Observer must not be able to directly edit files via this token.
    for forbidden in ("edit_files", "edit_file", "apply_patch", "write_file"):
        assert forbidden not in allowed
        assert forbidden in blocked
    assert "run_implementation_command" in blocked
    assert "close_without_worker_or_subagent_evidence" in blocked
    # And the gate must reject a direct edit action even with the token present.
    with pytest.raises(MfSubagentContractError):
        validate_route_token_mutation_gate(
            {"route_token": token},
            action="edit_files",
            project_id=_PROJECT,
            backlog_id=_BACKLOG,
            task_id=_TASK,
            now=_NOW,
        )


def test_token_does_not_authorize_unlisted_action():
    # A protected action that is neither in allowed nor blocked must still be
    # rejected — the gate only allows explicit membership / wildcard.
    token = _token()
    with pytest.raises(MfSubagentContractError):
        validate_route_token_mutation_gate(
            {"route_token": token},
            action="some_unlisted_protected_action",
            project_id=_PROJECT,
            backlog_id=_BACKLOG,
            task_id=_TASK,
            now=_NOW,
        )


# --- AC2/AC7: scope security ------------------------------------------------


def test_scope_mismatch_backlog_rejected():
    token = _token()
    with pytest.raises(MfSubagentContractError):
        validate_route_token_mutation_gate(
            {"route_token": token},
            action="task_timeline_append",
            project_id=_PROJECT,
            backlog_id="AC-SOME-OTHER-BACKLOG-20260609",
            task_id=_TASK,
            now=_NOW,
        )


def test_scope_mismatch_project_rejected():
    token = _token()
    with pytest.raises(MfSubagentContractError):
        validate_route_token_mutation_gate(
            {"route_token": token},
            action="task_timeline_append",
            project_id="some-other-project",
            backlog_id=_BACKLOG,
            task_id=_TASK,
            now=_NOW,
        )


def test_scope_mismatch_task_rejected():
    token = _token()
    with pytest.raises(MfSubagentContractError):
        validate_route_token_mutation_gate(
            {"route_token": token},
            action="task_timeline_append",
            project_id=_PROJECT,
            backlog_id=_BACKLOG,
            task_id="task-some-other-001",
            now=_NOW,
        )


# --- AC2: future expiry / stale identity ------------------------------------


def test_expired_token_rejected():
    token = _token(ttl_hours=1)
    far_future = _NOW + timedelta(hours=48)
    with pytest.raises(MfSubagentContractError):
        validate_route_token_mutation_gate(
            {"route_token": token},
            action="task_timeline_append",
            project_id=_PROJECT,
            backlog_id=_BACKLOG,
            task_id=_TASK,
            now=far_future,
        )


def test_stale_identity_token_with_tampered_scope_rejected():
    # If a stale/forked token's embedded scope is mutated to a different task,
    # the gate must reject it against the real request scope.
    token = _token()
    token = dict(token)
    token["scope"] = dict(token["scope"])
    token["scope"]["task_id"] = "task-forked-stale-001"
    with pytest.raises(MfSubagentContractError):
        validate_route_token_mutation_gate(
            {"route_token": token},
            action="task_timeline_append",
            project_id=_PROJECT,
            backlog_id=_BACKLOG,
            task_id=_TASK,
            now=_NOW,
        )


def test_deterministic_identity_for_same_inputs():
    a = _token()
    b = _token()
    assert a["route_id"] == b["route_id"]
    assert a["route_context_hash"] == b["route_context_hash"]
    assert a["prompt_contract_id"] == b["prompt_contract_id"]
    assert a["visible_injection_manifest_hash"] == b["visible_injection_manifest_hash"]


# --- AC5: provider selection (config-driven, runtime-independent) ------------


def test_provider_defaults_to_aming_local():
    provider = observer_route_context.resolve_route_provider(_PROJECT)
    assert provider["source"] == "aming_local_default"
    assert provider["owner"] == "aming-claw"
    assert provider["external_provider_required"] is False
    token = _token()
    assert token["provider"]["source"] == "aming_local_default"


def test_provider_external_recorded_when_configured():
    fake_config = {
        "route": {
            "provider": "external-route-service",
            "id": "external-route-service",
            "version": "9.9.9",
            "model": "external-model",
        }
    }
    provider = observer_route_context.resolve_route_provider(
        _PROJECT, config=fake_config
    )
    assert provider["source"] == "external_provider"
    assert provider["id"] == "external-route-service"
    assert provider["version"] == "9.9.9"
    assert provider["external_provider_required"] is False
    assert provider["hash"].startswith("sha256:")
    # The token still embeds the external provider evidence when passed through.
    token = _token(provider=provider)
    assert token["provider"]["source"] == "external_provider"
    assert token["provider"]["id"] == "external-route-service"
    # External provider config does not weaken the write authorization, and the
    # token is still accepted by the gate (runtime independence: gate never
    # calls the external provider).
    assert token["authorizes_protected_write"] is True
    result = validate_route_token_mutation_gate(
        {"route_token": token},
        action="task_timeline_append",
        project_id=_PROJECT,
        backlog_id=_BACKLOG,
        task_id=_TASK,
        now=_NOW,
    )
    assert result["decision"] == "route_token"


# --- AC1: runtime independence — no external provider import at all ----------


def test_import_does_not_pull_external_route_provider():
    # Importing the generator must not import any external route provider module
    # (judgment-brain is the canonical external provider).
    import importlib

    importlib.import_module("agent.governance.observer_route_context")
    leaked = [
        name
        for name in sys.modules
        if "judgment_brain" in name or "judgment-brain" in name
    ]
    assert leaked == [], f"external provider modules leaked into sys.modules: {leaked}"


# --- AC3: contract lanes + evidence baked in --------------------------------


def test_required_lanes_and_evidence_present():
    token = _token()
    lane_ids = {lane["id"] for lane in token["required_lanes"]}
    assert "observer_intent_capture" in lane_ids
    assert "bounded_implementation_subagent" in lane_ids
    assert "independent_verification_subagent" in lane_ids
    assert "observer_merge_close_gate" in lane_ids
    for lane in token["required_lanes"]:
        assert lane["id"] and lane["role"] and lane["purpose"]
    assert "verification_report" in token["required_evidence"]
    assert "dirty_scope_check" in token["required_evidence"]
    assert "bounded_implementation_subagent_id" in token["required_evidence"]
    assert "independent_verification_subagent_id" in token["required_evidence"]


# --- AC4: sanitize caller-supplied allowed_actions at mint ------------------


def test_wildcard_allowed_action_rejected_at_mint():
    # An unsanitized ["*"] would pass the gate for ANY action — privilege
    # over-reach. It must be rejected at mint with ValueError.
    with pytest.raises(ValueError):
        _token(allowed_actions=["*"])


def test_blocked_action_in_allowed_rejected_at_mint():
    # The gate ignores blocked_actions, so a blocked action sneaked into
    # allowed_actions would authorize a direct file edit. Reject at mint.
    for blocked in ("edit_files", "apply_patch", "write_file"):
        with pytest.raises(ValueError):
            _token(allowed_actions=[blocked])
    # Even mixed with a legitimate action, the intersection must be rejected.
    with pytest.raises(ValueError):
        _token(allowed_actions=["task_timeline_append", "edit_file"])


def test_clean_caller_supplied_allowed_actions_accepted():
    # A clean subset of allowed actions still mints successfully.
    token = _token(allowed_actions=["task_timeline_append", "backlog_close"])
    assert set(token["allowed_actions"]) == {"task_timeline_append", "backlog_close"}


# --- AC4 (HOTFIX): normalize caller-supplied actions to match the gate -------
#
# The gate (``_route_action_allowed`` / ``_normalized_action``) normalizes both
# the token's ``allowed_actions`` AND the requested action (lowercase, ``-``/``.``
# -> ``_``, strip) before membership-testing and ignores ``blocked_actions``. The
# mint-time sanitizer MUST apply the same normalization BEFORE the wildcard /
# blocked-action checks, otherwise a crafted case/separator variant of a blocked
# action (or the wildcard) bypasses the sanitizer and is then accepted by the
# gate for the canonical blocked action. AC-OBSERVER-WRITE-AUTH-ROUTE-CONTEXT-GEN.


def test_blocked_action_separator_variant_rejected_at_mint():
    # "Edit-Files" normalizes (gate-side) to "edit_files" which is blocked.
    with pytest.raises(ValueError):
        _token(allowed_actions=["Edit-Files"])


def test_blocked_action_dot_variant_rejected_at_mint():
    # "APPLY.PATCH" normalizes (gate-side) to "apply_patch" which is blocked.
    with pytest.raises(ValueError):
        _token(allowed_actions=["APPLY.PATCH"])


def test_blocked_action_case_variant_rejected_at_mint():
    # "edit_FILES" normalizes (gate-side) to "edit_files" which is blocked.
    with pytest.raises(ValueError):
        _token(allowed_actions=["edit_FILES"])


def test_wildcard_with_surrounding_whitespace_rejected_at_mint():
    # "  *  " normalizes (gate-side) to "*" — the wildcard authorizes ANY action
    # at the gate, so it must be rejected at mint.
    with pytest.raises(ValueError):
        _token(allowed_actions=["  *  "])


def test_mixed_valid_and_blocked_variant_rejected_at_mint():
    # A legitimate action mixed with a case/separator variant of a blocked action
    # must still raise — the whole mint is rejected, not silently filtered.
    with pytest.raises(ValueError):
        _token(allowed_actions=["task_timeline_append", "Apply-Patch"])


def test_blocked_action_variants_parity_with_gate_normalizer():
    """Parity: for EVERY blocked action, a case/separator variant that the gate
    would normalize to it must be rejected at mint.

    This guards against the sanitizer and the gate's normalizer drifting: it
    derives each variant via the gate's OWN ``_normalized_action`` round-trip and
    asserts the mint rejects it for every blocked action.
    """
    from agent.governance.mf_subagent_contract import _normalized_action

    for blocked in observer_route_context.BLOCKED_ACTIONS:
        canonical = _normalized_action(blocked)
        # Build a crafted variant: uppercase + swap one "_" for "-" / "." so the
        # raw string differs from the canonical but normalizes back to it.
        upper = blocked.upper()
        variants = {upper, upper.replace("_", "-", 1), upper.replace("_", ".", 1)}
        for variant in variants:
            # Sanity: the gate would resolve this variant to the blocked action.
            assert _normalized_action(variant) == canonical, (
                f"variant {variant!r} does not normalize to {canonical!r}"
            )
            with pytest.raises(ValueError):
                _token(allowed_actions=[variant])


def test_clean_caller_action_with_separators_is_normalized_and_stored():
    # A non-blocked action supplied in a non-canonical form is normalized and the
    # NORMALIZED form is stored in the token, so the token's allowed_actions equal
    # exactly what the gate evaluates (no drift between mint and gate).
    token = _token(allowed_actions=["Task-Timeline.Append", "BACKLOG_CLOSE"])
    assert set(token["allowed_actions"]) == {"task_timeline_append", "backlog_close"}
    # Round-trip: the gate accepts the canonical action against the stored token.
    result = validate_route_token_mutation_gate(
        {"route_token": token},
        action="task_timeline_append",
        project_id=_PROJECT,
        backlog_id=_BACKLOG,
        task_id=_TASK,
        now=_NOW,
    )
    assert result["decision"] == "route_token"


# --- AC4: clamp/reject oversized or non-positive TTL ------------------------


def test_ttl_over_max_rejected():
    assert observer_route_context.MAX_TTL_HOURS == 72.0
    with pytest.raises(ValueError):
        _token(ttl_hours=observer_route_context.MAX_TTL_HOURS + 1)


def test_ttl_non_positive_rejected():
    with pytest.raises(ValueError):
        _token(ttl_hours=0)
    with pytest.raises(ValueError):
        _token(ttl_hours=-5)


def test_ttl_at_max_boundary_accepted():
    # Exactly MAX_TTL_HOURS is allowed (only strictly greater is rejected).
    token = _token(ttl_hours=observer_route_context.MAX_TTL_HOURS)
    assert token["expires_at"]


# --- AC6: consumable route_token_ref + merge_queue_id + execute payload -----


def test_issue_returns_consumable_ref_and_merge_queue_id():
    issued = _issue()
    assert issued["ok"] is True
    assert issued["route_token_ref"]
    assert issued["route_token_ref"].startswith("rtok-")
    assert issued["merge_queue_id"]
    assert issued["merge_queue_id"].startswith("mq-")
    # The full token is present and still gate-valid.
    result = validate_route_token_mutation_gate(
        {"route_token": issued["route_token"]},
        action="execute_backlog_row",
        project_id=_PROJECT,
        backlog_id=_BACKLOG,
        task_id=_TASK,
        now=_NOW,
    )
    assert result["decision"] == "route_token"


def test_execute_backlog_row_payload_has_required_fields():
    # The issued execute_backlog_row_payload must carry exactly the fields the
    # observer execute_backlog_row command requires, so it is genuinely
    # consumable. Mirror observer_session.EXECUTE_BACKLOG_ROW_REQUIRED_PAYLOAD_FIELDS.
    issued = _issue()
    payload = issued["execute_backlog_row_payload"]
    required = (
        "backlog_id",
        "merge_queue_id",
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "route_token_ref",
        "visible_injection_manifest_hash",
    )
    for field in required:
        assert payload.get(field), f"missing/empty required field: {field}"
    assert payload["backlog_id"] == _BACKLOG
    assert payload["route_token_ref"] == issued["route_token_ref"]
    assert payload["merge_queue_id"] == issued["merge_queue_id"]

    # Cross-check against the live required-fields constant if importable.
    try:
        from agent.governance.observer_session import (
            EXECUTE_BACKLOG_ROW_REQUIRED_PAYLOAD_FIELDS,
        )
    except Exception:
        EXECUTE_BACKLOG_ROW_REQUIRED_PAYLOAD_FIELDS = required
    for field in EXECUTE_BACKLOG_ROW_REQUIRED_PAYLOAD_FIELDS:
        assert payload.get(field), f"missing live-required field: {field}"


def test_issue_is_deterministic():
    a = _issue()
    b = _issue()
    assert a["route_token_ref"] == b["route_token_ref"]
    assert a["merge_queue_id"] == b["merge_queue_id"]


# --- AC8: parent/child route lineage for worker issuance --------------------


def test_issue_without_parent_route_lineage_is_backward_compatible():
    issued = _issue()
    token = issued["route_token"]
    payload = issued["execute_backlog_row_payload"]

    assert "parent_route_lineage" not in token
    assert "child_route_lineage" not in token
    assert "route_lineage" not in token
    assert "parent_route_lineage" not in payload
    assert "child_route_lineage" not in payload
    assert "route_lineage" not in payload

    result = validate_route_token_mutation_gate(
        {"route_token": token},
        action="execute_backlog_row",
        project_id=_PROJECT,
        backlog_id=_BACKLOG,
        task_id=_TASK,
        now=_NOW,
    )
    assert result["decision"] == "route_token"


def test_issue_with_complete_parent_records_parent_and_child_lineage():
    issued = _issue(parent_route_identity=_PARENT_ROUTE_IDENTITY)
    token = issued["route_token"]
    payload = issued["execute_backlog_row_payload"]

    parent = token["parent_route_lineage"]
    child = token["child_route_lineage"]
    route_lineage = token["route_lineage"]

    assert parent["schema_version"] == "parent_route_lineage.v1"
    assert parent["binding_status"] == "parent_bound"
    assert parent["route_id"] == _PARENT_ROUTE_IDENTITY["route_id"]
    assert parent["route_context_hash"] == _PARENT_ROUTE_IDENTITY["route_context_hash"]
    assert parent["prompt_contract_id"] == _PARENT_ROUTE_IDENTITY["prompt_contract_id"]
    assert parent["route_token_ref"] == _PARENT_ROUTE_IDENTITY["route_token_ref"]

    assert child["schema_version"] == "child_route_lineage.v1"
    assert child["route_id"] == token["route_id"]
    assert child["route_context_hash"] == token["route_context_hash"]
    assert child["prompt_contract_id"] == token["prompt_contract_id"]
    assert child["project_id"] == _PROJECT
    assert child["backlog_id"] == _BACKLOG
    assert child["task_id"] == _TASK
    assert child["allowed_actions"] == token["allowed_actions"]
    assert child["route_token_ref"] == issued["route_token_ref"]
    assert child["merge_queue_id"] == issued["merge_queue_id"]

    assert route_lineage["status"] == "parent_bound"
    assert route_lineage["parent_route_id"] == parent["route_id"]
    assert route_lineage["child_route_id"] == child["route_id"]
    assert route_lineage["raw_route_token_persisted"] is False
    assert route_lineage["raw_session_token_persisted"] is False

    assert payload["parent_route_lineage"] == parent
    assert payload["child_route_lineage"] == child
    assert payload["route_lineage"] == route_lineage
    assert "route_token" not in payload
    _assert_no_raw_token_keys(payload["parent_route_lineage"])
    _assert_no_raw_token_keys(payload["child_route_lineage"])
    _assert_no_raw_token_keys(payload["route_lineage"])


def test_parent_lineage_also_accepts_generated_route_id():
    generated_parent = dict(_PARENT_ROUTE_IDENTITY)
    generated_parent["route_id"] = "route-20260616-7125c02c654a700e"

    issued = _issue(parent_route_identity=generated_parent)

    assert issued["parent_route_lineage"]["route_id"] == generated_parent["route_id"]


def test_parent_lineage_accepts_route_action_pre_mutation_event_id():
    route_action_parent = dict(_PARENT_ROUTE_IDENTITY)
    route_action_parent["route_id"] = "event.route_action.pre_mutation"

    issued = _issue(parent_route_identity=route_action_parent)

    assert issued["parent_route_lineage"]["route_id"] == "event.route_action.pre_mutation"
    assert issued["route_lineage"]["parent_route_id"] == "event.route_action.pre_mutation"


def test_incomplete_or_mismatched_parent_route_identity_fails_closed():
    incomplete = {
        "route_id": _PARENT_ROUTE_IDENTITY["route_id"],
        "route_context_hash": _PARENT_ROUTE_IDENTITY["route_context_hash"],
    }
    with pytest.raises(ValueError, match="incomplete"):
        _issue(parent_route_identity=incomplete)

    mismatched_backlog = dict(_PARENT_ROUTE_IDENTITY)
    mismatched_backlog["selected_backlog_id"] = "AC-SOME-OTHER-BACKLOG-20260616"
    with pytest.raises(ValueError, match="backlog mismatch"):
        _issue(parent_route_identity=mismatched_backlog)

    with pytest.raises(ValueError, match="mismatch for route_id"):
        _issue(
            parent_route_identity=_PARENT_ROUTE_IDENTITY,
            parent_route_id="route-20260616-conflicting",
        )

    raw_token_parent = dict(_PARENT_ROUTE_IDENTITY)
    raw_token_parent["route_token"] = {"raw": "must-not-persist"}
    with pytest.raises(ValueError, match="raw route/session token"):
        _issue(parent_route_identity=raw_token_parent)

    invalid_route = dict(_PARENT_ROUTE_IDENTITY)
    invalid_route["route_id"] = "task.route_context.preview"
    with pytest.raises(ValueError, match="public canonical route id"):
        _issue(parent_route_identity=invalid_route)

    invalid_prompt_id = dict(_PARENT_ROUTE_IDENTITY)
    invalid_prompt_id["prompt_contract_id"] = "prompt-contract-1"
    with pytest.raises(ValueError, match="rprompt"):
        _issue(parent_route_identity=invalid_prompt_id)

    invalid_hash = dict(_PARENT_ROUTE_IDENTITY)
    invalid_hash["route_context_hash"] = "not-a-sha256"
    with pytest.raises(ValueError, match="sha256"):
        _issue(parent_route_identity=invalid_hash)


def test_parent_lineage_route_token_ref_is_opaque_and_stable():
    a = _issue(parent_route_identity=_PARENT_ROUTE_IDENTITY)
    b = _issue(parent_route_identity=_PARENT_ROUTE_IDENTITY)
    assert a["route_token_ref"] == b["route_token_ref"]
    assert a["merge_queue_id"] == b["merge_queue_id"]

    ref = a["route_token_ref"]
    token = a["route_token"]
    assert ref.startswith("rtok-")
    assert len(ref) < 64
    assert token["route_context_hash"] not in ref
    assert token["prompt_contract_hash"] not in ref
    assert _PARENT_ROUTE_IDENTITY["route_context_hash"] not in ref
    assert _PARENT_ROUTE_IDENTITY["prompt_contract_hash"] not in ref
    assert "route-" not in ref

    different_parent = dict(_PARENT_ROUTE_IDENTITY)
    different_parent.update(
        {
            "route_id": "route-20260616-differentparent",
            "route_context_hash": "sha256:" + ("a" * 64),
            "prompt_contract_id": "rprompt-aming-differentparent",
            "prompt_contract_hash": "sha256:" + ("b" * 64),
            "visible_injection_manifest_hash": "sha256:" + ("c" * 64),
        }
    )
    c = _issue(parent_route_identity=different_parent)
    assert c["route_token_ref"] != ref


def test_superseded_parent_bound_ref_next_action_carries_issue_payload():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    issued = _issue(
        parent_route_identity=_PARENT_ROUTE_IDENTITY,
        allowed_actions=["task_timeline_append"],
        evidence_refs=["contract_runtime:cex-parent"],
    )
    ref = issued["route_token_ref"]
    observer_route_context.persist_route_token_ref(
        conn,
        project_id=_PROJECT,
        route_token_ref=ref,
        token=issued["route_token"],
    )
    assert observer_route_context.supersede_route_token_ref(
        conn,
        project_id=_PROJECT,
        route_token_ref=ref,
    )

    with pytest.raises(observer_route_context.RouteTokenRefError) as exc:
        observer_route_context.resolve_route_token_ref(
            conn,
            project_id=_PROJECT,
            route_token_ref=ref,
            backlog_id=_BACKLOG,
            task_id=_TASK,
        )

    assert exc.value.code == "route_token_ref_not_active"
    next_action = exc.value.details["next_action"]
    assert next_action["action"] == "issue_fresh_same_scope_route_token_ref"
    assert next_action["semantic_next_action"] == "observer_route_context_issue"
    assert next_action["parent_route_identity_required"] is True
    assert "parent_route_identity" in next_action["required_fields"]

    issue_payload = next_action["observer_route_context_issue_payload"]
    assert issue_payload["project_id"] == _PROJECT
    assert issue_payload["caller_role"] == "observer"
    assert issue_payload["backlog_id"] == _BACKLOG
    assert issue_payload["task_id"] == _TASK
    assert issue_payload["allowed_actions"] == ["task_timeline_append"]
    assert issue_payload["target_files"] == _TARGET_FILES
    assert issue_payload["parent_route_token_ref"] == (
        _PARENT_ROUTE_IDENTITY["route_token_ref"]
    )
    assert issue_payload["parent_route_identity"]["route_id"] == (
        _PARENT_ROUTE_IDENTITY["route_id"]
    )
    assert issue_payload["parent_route_identity"]["route_context_hash"] == (
        _PARENT_ROUTE_IDENTITY["route_context_hash"]
    )
    assert f"reissued_from:{ref}" in issue_payload["evidence_refs"]
    _assert_no_raw_token_keys(next_action)


# --- AC7 / dogfood: redaction — raw token never persisted in the ref --------


def test_route_token_ref_does_not_leak_raw_token():
    issued = _issue()
    ref = issued["route_token_ref"]
    token = issued["route_token"]
    # The opaque ref is a short hashed handle, not the serialized token.
    assert "authorizes_protected_write" not in ref
    assert token["route_context_hash"] not in ref
    assert token["prompt_contract_hash"] not in ref
    assert len(ref) < 64
    # The execute payload also carries only the opaque ref, never the raw token.
    payload = issued["execute_backlog_row_payload"]
    assert "route_token" not in payload
    assert payload["route_token_ref"] == ref


def test_gate_redacts_raw_token_via_hash():
    # The gate must not echo the raw token back; it returns a stable hash only.
    token = _token()
    result = validate_route_token_mutation_gate(
        {"route_token": token},
        action="task_timeline_append",
        project_id=_PROJECT,
        backlog_id=_BACKLOG,
        task_id=_TASK,
        now=_NOW,
    )
    assert result["route_token_hash"].startswith("sha256:")
    assert "route_token" not in result
    assert "allowed_actions" not in result  # raw token internals not echoed


# --- AC7: no self-authorizing token — only the real gate may accept ---------


def test_token_carries_no_self_accept_decision():
    # The generator must NOT bake a pre-decided "accepted"/"decision" verdict
    # into the token; acceptance is the independent gate's job. A self-authorized
    # verdict would let a caller bypass scope/expiry checks.
    token = _token()
    assert "decision" not in token
    assert "accepted" not in token
    assert token.get("status") in (None, "")


def test_empty_scope_inputs_rejected_at_mint():
    with pytest.raises(ValueError):
        _token(backlog_id="")
    with pytest.raises(ValueError):
        _token(task_id="")
    with pytest.raises(ValueError):
        _token(target_files=[])


# --- DOGFOOD: the previous failure mode (empty route_token_ref) is gone ------


def test_dogfood_fresh_observer_gets_nonempty_write_route_token_ref():
    """Dogfood: a fresh observer calling the native generator now gets a
    NON-EMPTY route_token_ref + merge_queue_id, and that token passes the
    existing validate_route_token_mutation_gate with decision == "route_token".

    Previously the only native route-context builder was read-only, so a fresh
    observer got route_token_ref="" and could not legally enqueue
    execute_backlog_row. This asserts the gap is closed without weakening any
    close defense (direct edits remain blocked).
    """
    issued = _issue()

    # 1) Non-empty, consumable handles (the previously-missing values).
    assert issued["route_token_ref"], "route_token_ref must be non-empty"
    assert issued["merge_queue_id"], "merge_queue_id must be non-empty"

    # 2) The minted token passes the EXISTING gate as decision == "route_token".
    gate = validate_route_token_mutation_gate(
        {"route_token": issued["route_token"]},
        action="task_timeline_append",
        project_id=_PROJECT,
        backlog_id=_BACKLOG,
        task_id=_TASK,
        now=_NOW,
    )
    assert gate["decision"] == "route_token"
    assert gate["allowed"] is True
    assert gate["caller_role"] == "observer"

    # 3) The execute_backlog_row payload is fully populated (no empty route id).
    payload = issued["execute_backlog_row_payload"]
    assert payload["route_token_ref"] == issued["route_token_ref"]
    assert payload["merge_queue_id"] == issued["merge_queue_id"]
    assert payload["route_id"]

    # 4) Layered close defense intact: a direct file edit is still blocked.
    with pytest.raises(MfSubagentContractError):
        validate_route_token_mutation_gate(
            {"route_token": issued["route_token"]},
            action="apply_patch",
            project_id=_PROJECT,
            backlog_id=_BACKLOG,
            task_id=_TASK,
            now=_NOW,
        )

    # 5) Raw token text is not leaked into the consumable ref.
    assert issued["route_token_ref"].startswith("rtok-")
    assert "authorizes_protected_write" not in issued["route_token_ref"]


# --- AC6: HTTP endpoint observer role check ---------------------------------


class _FakeHandler:
    headers: dict = {}


class _Ctx:
    def __init__(self, body):
        self.handler = _FakeHandler()
        self.body = body
        self.path_params = {"project_id": _PROJECT}

    def get_project_id(self):
        return _PROJECT


def _endpoint_body(**overrides):
    body = {
        "caller_role": "observer",
        "backlog_id": _BACKLOG,
        "task_id": _TASK,
        "target_files": _TARGET_FILES,
    }
    body.update(overrides)
    return body


def test_endpoint_rejects_non_observer_caller_role():
    """The issuance handler must 403 a non-observer caller.

    The handler reads caller_role from the request body (or X-Caller-Role
    header) and rejects anything other than "observer" before minting a
    write-authorizing token.
    """
    from agent.governance import server

    # Non-observer caller_role -> 403.
    code, payload = server.handle_observer_route_context_issue(
        _Ctx(
            {
                "caller_role": "mf_sub",
                "backlog_id": _BACKLOG,
                "task_id": _TASK,
                "target_files": _TARGET_FILES,
            }
        )
    )
    assert code == 403
    assert "observer" in payload["error"]

    # Missing caller_role -> 403 as well.
    code2, _ = server.handle_observer_route_context_issue(
        _Ctx(
            {
                "backlog_id": _BACKLOG,
                "task_id": _TASK,
                "target_files": _TARGET_FILES,
            }
        )
    )
    assert code2 == 403


def test_endpoint_accepts_observer_caller_role():
    from agent.governance import server

    result = server.handle_observer_route_context_issue(
        _Ctx(_endpoint_body())
    )
    # Success path returns a plain dict (200), not a (code, body) tuple.
    assert isinstance(result, dict)
    assert result["ok"] is True
    assert result["route_token"]["authorizes_protected_write"] is True
    assert result["route_token_ref"]
    assert result["merge_queue_id"]
    assert result["execute_backlog_row_payload"]["route_token_ref"] == result[
        "route_token_ref"
    ]
    for field in (
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "visible_injection_manifest_hash",
    ):
        assert result[field] == result["route_token"][field]
        assert result["route_identity"][field] == result["route_token"][field]
        assert result["canonical_route_identity"][field] == result["route_token"][field]
    assert result["route_identity"]["route_token_ref"] == result["route_token_ref"]
    assert result["canonical_route_identity"] == result["route_identity"]


def test_endpoint_accepts_parent_route_identity_and_returns_lineage():
    from agent.governance import server

    result = server.handle_observer_route_context_issue(
        _Ctx(_endpoint_body(parent_route_identity=dict(_PARENT_ROUTE_IDENTITY)))
    )

    assert isinstance(result, dict)
    assert result["ok"] is True
    assert result["parent_route_lineage"]["route_id"] == "event.route_prompt_context.preview"
    assert result["parent_route_lineage"] == result["route_token"]["parent_route_lineage"]
    assert result["child_route_lineage"] == result["route_token"]["child_route_lineage"]
    assert result["route_lineage"] == result["route_token"]["route_lineage"]
    assert (
        result["execute_backlog_row_payload"]["parent_route_lineage"]
        == result["parent_route_lineage"]
    )
    assert result["route_lineage"]["parent_route_id"] == "event.route_prompt_context.preview"
    assert result["route_lineage"]["child_route_id"] == result["route_token"]["route_id"]
    assert result["route_lineage"]["raw_route_token_persisted"] is False
    _assert_no_raw_token_keys(result["parent_route_lineage"])
    _assert_no_raw_token_keys(result["child_route_lineage"])
    _assert_no_raw_token_keys(result["route_lineage"])


def test_endpoint_rejects_bad_parent_route_identity_inputs():
    from agent.governance import server

    incomplete = {
        "route_id": _PARENT_ROUTE_IDENTITY["route_id"],
        "route_context_hash": _PARENT_ROUTE_IDENTITY["route_context_hash"],
    }
    code, payload = server.handle_observer_route_context_issue(
        _Ctx(_endpoint_body(parent_route_identity=incomplete))
    )
    assert code == 400
    assert "incomplete" in payload["error"]

    raw_parent = dict(_PARENT_ROUTE_IDENTITY)
    raw_parent["session_token"] = "raw-session-token"
    code, payload = server.handle_observer_route_context_issue(
        _Ctx(_endpoint_body(parent_route_identity=raw_parent))
    )
    assert code == 400
    assert "raw route/session token" in payload["error"]

    code, payload = server.handle_observer_route_context_issue(
        _Ctx(_endpoint_body(parent_route_token={"raw": "must-not-accept"}))
    )
    assert code == 400
    assert "raw route/session token" in payload["error"]

    code, payload = server.handle_observer_route_context_issue(
        _Ctx(
            _endpoint_body(
                parent_route_identity=dict(_PARENT_ROUTE_IDENTITY),
                parent_route_id="route-20260616-conflicting",
            )
        )
    )
    assert code == 400
    assert "mismatch for route_id" in payload["error"]


def test_endpoint_accepts_explicit_parent_route_identity_fields():
    from agent.governance import server

    result = server.handle_observer_route_context_issue(
        _Ctx(
            _endpoint_body(
                parent_route_id=_PARENT_ROUTE_IDENTITY["route_id"],
                parent_route_context_hash=_PARENT_ROUTE_IDENTITY["route_context_hash"],
                parent_prompt_contract_id=_PARENT_ROUTE_IDENTITY["prompt_contract_id"],
                parent_prompt_contract_hash=_PARENT_ROUTE_IDENTITY[
                    "prompt_contract_hash"
                ],
                parent_visible_injection_manifest_hash=_PARENT_ROUTE_IDENTITY[
                    "visible_injection_manifest_hash"
                ],
                parent_route_token_ref=_PARENT_ROUTE_IDENTITY["route_token_ref"],
            )
        )
    )

    assert isinstance(result, dict)
    assert result["parent_route_lineage"]["route_id"] == "event.route_prompt_context.preview"
    assert result["route_lineage"]["status"] == "parent_bound"


def test_mcp_route_context_issue_schema_exposes_parent_identity_fields():
    from agent.governance import mcp_server

    tool = next(
        item for item in mcp_server.TOOLS if item.get("name") == "observer_route_context_issue"
    )
    props = tool["inputSchema"]["properties"]
    parent_props = props["parent_route_identity"]["properties"]

    assert props["parent_route_identity"]["type"] == "object"
    assert "event.route_prompt_context.preview" in props["parent_route_id"]["description"]
    assert (
        "event.route_action.pre_mutation"
        in props["parent_route_id"]["description"]
    )
    for key in (
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "visible_injection_manifest_hash",
        "route_token_ref",
    ):
        assert key in parent_props
        assert f"parent_{key}" in props
    for raw_key in ("route_token", "session_token", "token", "token_body"):
        assert raw_key not in parent_props


def test_mcp_route_context_issue_forwards_public_parent_identity(monkeypatch):
    from agent.governance import mcp_server

    calls = []

    def fake_http(method, path, body):
        calls.append((method, path, body))
        return {"ok": True, "data": body}

    monkeypatch.setattr(mcp_server, "_http", fake_http)

    result = mcp_server._dispatch_tool(
        "observer_route_context_issue",
        {
            "project_id": _PROJECT,
            "backlog_id": _BACKLOG,
            "task_id": _TASK,
            "target_files": _TARGET_FILES,
            "parent_route_identity": dict(_PARENT_ROUTE_IDENTITY),
            "parent_route_id": _PARENT_ROUTE_IDENTITY["route_id"],
        },
    )

    assert result["ok"] is True
    assert calls == [
        (
            "POST",
            f"/api/projects/{_PROJECT}/observer/route-context/issue",
            {
                "backlog_id": _BACKLOG,
                "task_id": _TASK,
                "target_files": _TARGET_FILES,
                "parent_route_identity": _PARENT_ROUTE_IDENTITY,
                "parent_route_id": "event.route_prompt_context.preview",
                "caller_role": "observer",
            },
        )
    ]


def test_endpoint_sanitizes_wildcard_allowed_actions():
    from agent.governance import server

    code, payload = server.handle_observer_route_context_issue(
        _Ctx(
            {
                "caller_role": "observer",
                "backlog_id": _BACKLOG,
                "task_id": _TASK,
                "target_files": _TARGET_FILES,
                "allowed_actions": ["*"],
            }
        )
    )
    assert code == 400
    assert "wildcard" in payload["error"] or "*" in payload["error"]
