from __future__ import annotations

from agent.governance.contracts import (
    ContractDefinitionRegistry,
    build_execution_state,
    validate_contract_write,
)
from agent.governance.contract_template_registry import get_contract_template


EXPECTED_DIRECT_MAIN_CHAIN = [
    ("route_gate", "observer_bind_direct_scope", "observer", "contract_binding"),
    ("graph_first", "observer_graph_context", "observer", "graph_trace"),
    (
        "pre_mutation",
        "observer_direct_implementation_exception",
        "observer",
        "observer_direct_implementation_exception",
    ),
    ("implementation", "observer_implementation", "observer", "implementation"),
    ("qa_graph_context", "qa_graph_context", "qa", "graph_trace"),
    ("qa", "qa_independent_verification", "qa", "independent_verification"),
    ("reconcile", "observer_reconcile", "observer", "current_full_reconcile"),
    ("close_ready", "observer_close_ready", "observer", "close_ready"),
]


def _direct_definition():
    return ContractDefinitionRegistry().get(
        "operator_supervised_direct_main",
        version="v1",
        revision="rev2",
    )


def test_direct_main_contract_chain_has_one_coherent_role_bound_order() -> None:
    definition = _direct_definition()
    flattened = [
        (
            stage["stage_id"],
            line["line_id"],
            line["owner_role"],
            line["evidence_kind"],
        )
        for stage in definition["rule_layer"]["stages"]
        for line in stage["lines"]
    ]

    assert flattened == EXPECTED_DIRECT_MAIN_CHAIN
    previous_line = ""
    for stage in definition["rule_layer"]["stages"]:
        assert len(stage["lines"]) == 1
        line = stage["lines"][0]
        assert line["allowed_writer_roles"] == [line["owner_role"]]
        assert line.get("requires", []) == (
            [] if not previous_line else [previous_line]
        )
        previous_line = line["line_id"]

    template = get_contract_template("operator_supervised_direct_main.v1")
    assert template["stages"] == [stage for stage, *_ in EXPECTED_DIRECT_MAIN_CHAIN]
    assert [
        (
            item["phase"],
            item["id"],
            item["owner_role"],
            item["kind"],
        )
        for item in template["evidence_requirements"]
    ] == EXPECTED_DIRECT_MAIN_CHAIN


def test_direct_main_runtime_order_and_qa_role_gate_are_definition_derived() -> None:
    definition = _direct_definition()
    completed = []

    for stage_id, line_id, owner_role, evidence_kind in EXPECTED_DIRECT_MAIN_CHAIN:
        state = build_execution_state(
            definition,
            project_id="aming-claw",
            backlog_id="AC-DIRECT-MAIN-CHAIN-COHERENCE",
            contract_execution_id="cex-direct-main-chain-coherence",
            actor_role=owner_role,
            instruction_bundle_hash="sha256:direct-main-chain-coherence",
            completed_lines=completed,
        )
        assert state["next_action"] == {
            "stage_id": stage_id,
            "line_id": line_id,
            "owner_role": owner_role,
            "allowed_writer_roles": [owner_role],
            "evidence_kind": evidence_kind,
            "required": True,
        }
        completed.append({"stage_id": stage_id, "line_id": line_id})

    qa_state = build_execution_state(
        definition,
        project_id="aming-claw",
        backlog_id="AC-DIRECT-MAIN-QA-ROLE",
        contract_execution_id="cex-direct-main-qa-role",
        actor_role="qa",
        instruction_bundle_hash="sha256:direct-main-qa-role",
        completed_lines=[
            {"stage_id": stage_id, "line_id": line_id}
            for stage_id, line_id, *_ in EXPECTED_DIRECT_MAIN_CHAIN[:4]
        ],
    )
    wrong_role_write = {
        "project_id": qa_state["project_id"],
        "backlog_id": qa_state["backlog_id"],
        "contract_execution_id": qa_state["contract_execution_id"],
        "definition_hash": qa_state["definition_hash"],
        "instruction_bundle_hash": qa_state["instruction_bundle_hash"],
        "execution_state_revision": qa_state["execution_state_revision"],
        "stage_id": "qa_graph_context",
        "line_id": "qa_graph_context",
        "actor_role": "observer",
        "evidence_kind": "graph_trace",
    }
    decision = validate_contract_write(definition, qa_state, wrong_role_write)

    assert decision.ok is False
    assert any(
        "actor_role 'observer' cannot write line 'qa_graph_context'" in error
        for error in decision.errors
    )
    assert "qa_graph_context requires actor_role=qa" in decision.errors


def test_direct_main_rev1_dependency_join_remains_immutable_and_fail_closed() -> None:
    registry = ContractDefinitionRegistry()
    definition = registry.get(
        "operator_supervised_direct_main",
        version="v1",
        revision="rev1",
    )
    definition_join = definition["metadata"]["common_rule_applicability"]
    package = registry.common_rule_package()
    resolved_join = registry.resolve_common_rule_applicability(definition)

    expected_join = {
        "schema_version": "contract_common_rule_applicability.unresolved.v1",
        "join_state": "unresolved",
        "dependency_id": "AC-CONTRACT-COMMON-SAFETY-RULE-PACKAGE-P0-20260815",
        "package_id": "",
        "package_version": "",
        "package_digest": "",
        "rule_ids": [],
        "scopes": [],
        "omitted_rules_apply": False,
        "server_inference_allowed": False,
        "activation_allowed": False,
        "resolution_requires_new_contract_revision": True,
    }
    assert definition_join == expected_join
    assert definition["status"] == "draft"
    assert definition["system_layer"]["entrypoint_policy"]["allow_entry"] is False
    assert definition["system_layer"]["route_policy"]["start_allowed"] is False
    assert definition["system_layer"]["common_rule_policy"] == {
        "join_state": "unresolved",
        "activation_allowed": False,
        "omitted_rules_apply": False,
        "server_inference_allowed": False,
        "new_revision_required_after_resolution": True,
    }
    assert package["package_id"] == "aming-claw.common-safety"
    assert package["package_version"] == "v1"
    assert len(package["rule_ids"]) == 10
    assert resolved_join["join_state"] == "unresolved"
    assert resolved_join["authoritative"] is False
    assert resolved_join["rule_ids"] == []
    assert resolved_join["server_inference_allowed"] is False


def test_direct_main_rev2_rule_gate_and_guide_join_one_explicit_authority() -> None:
    registry = ContractDefinitionRegistry()
    definition = _direct_definition()
    template = get_contract_template("operator_supervised_direct_main.v1")
    package = registry.common_rule_package()
    join = registry.resolve_common_rule_applicability(definition)

    assert definition["status"] == "active"
    assert definition["revision"] == "rev2"
    assert join["authoritative"] is True
    assert join["join_state"] == "resolved"
    assert join["package_id"] == package["package_id"]
    assert join["package_version"] == package["package_version"]
    assert join["package_digest"] == package["package_digest"]
    assert join["rule_ids"] == package["rule_ids"]
    assert join["scopes"] == ["operator_supervised_direct_main"]
    assert join["omitted_rules_apply"] is False
    assert join["server_inference_allowed"] is False
    assert definition["system_layer"]["entrypoint_policy"]["allow_entry"] is True
    assert definition["system_layer"]["route_policy"]["start_allowed"] is True
    assert definition["system_layer"]["common_rule_policy"] == {
        "join_state": "resolved",
        "activation_allowed": True,
        "omitted_rules_apply": False,
        "server_inference_allowed": False,
        "package_id": package["package_id"],
        "package_version": package["package_version"],
        "package_digest": package["package_digest"],
        "applicable_rule_count": len(package["rule_ids"]),
        "new_revision_required_after_resolution": False,
    }
    assert template["source"]["revision"] == "rev2"
    assert template["source"]["definition_hash"] == definition["definition_hash"]
    assert template["source"]["definition_source_sha256"] == definition[
        "source_sha256"
    ]
    assert template["common_rule_applicability"] == definition["metadata"][
        "common_rule_applicability"
    ]
    assert template["activation_policy"] == definition["metadata"]["activation"]


def test_direct_main_terminal_and_retry_policy_forbid_in_place_repair() -> None:
    definition = _direct_definition()
    system = definition["system_layer"]

    assert system["retry_policy"]["same_execution_retry_allowed"] is False
    assert system["retry_policy"]["same_generation_retry_allowed"] is False
    assert system["retry_policy"]["post_hoc_pass_backfill_allowed"] is False
    assert system["retry_policy"]["failed_source_generation_terminal"] is True
    assert system["retry_policy"]["fresh_bounded_row_required"] is True
    assert system["terminal_outcome_policy"] == {
        "success": (
            "observer_close_ready_after_independent_qa_and_current_head_full_reconcile"
        ),
        "qa_failure": "terminal_no_pass_preserve_source_generation_as_audit",
        "authoritative_close_failure": (
            "terminal_no_pass_preserve_source_generation_as_audit"
        ),
        "dependency_unresolved": "typed_rejection_without_execution_or_write",
        "history_rewrite_allowed": False,
    }
    assert system["successor_policy"]["same_generation_retry_allowed"] is False
    assert system["successor_policy"]["fresh_bounded_row_required"] is True
