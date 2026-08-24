from __future__ import annotations

import json
from pathlib import Path

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


REPO_ROOT = Path(__file__).resolve().parents[2]
HAPPY_PATH_GATE_MAP = (
    REPO_ROOT / "docs/dev/contract-rule-gate-map.happy_path.v1.json"
)


def _happy_path_gate_map():
    return json.loads(HAPPY_PATH_GATE_MAP.read_text(encoding="utf-8"))


def _direct_definition():
    return ContractDefinitionRegistry().get(
        "operator_supervised_direct_main",
        version="v1",
        revision="rev3",
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


def test_direct_main_rev2_common_rule_overapplication_remains_immutable() -> None:
    registry = ContractDefinitionRegistry()
    definition = registry.get(
        "operator_supervised_direct_main",
        version="v1",
        revision="rev2",
    )
    package = registry.common_rule_package()
    join = registry.resolve_common_rule_applicability(definition)

    assert definition["status"] == "active"
    assert definition["revision"] == "rev2"
    assert definition["source_sha256"] == (
        "sha256:a98b860023b33cebaabc3d76d30c3a2c779532bd642bc8eb1a4ae7917540bef0"
    )
    assert join["authoritative"] is True
    assert join["join_state"] == "resolved"
    assert join["package_id"] == package["package_id"]
    assert join["package_version"] == package["package_version"]
    assert join["package_digest"] == package["package_digest"]
    assert join["rule_ids"] == package["rule_ids"]
    assert "AC-COMMON-MERGE-ORDERED" in join["rule_ids"]
    assert join["scopes"] == ["operator_supervised_direct_main"]
    assert join["omitted_rules_apply"] is False
    assert join["server_inference_allowed"] is False


def test_direct_main_rev3_rule_gate_and_guide_join_serial_authority() -> None:
    registry = ContractDefinitionRegistry()
    definition = _direct_definition()
    rev2 = registry.get(
        "operator_supervised_direct_main",
        version="v1",
        revision="rev2",
    )
    template = get_contract_template("operator_supervised_direct_main.v1")
    package = registry.common_rule_package()
    join = registry.resolve_common_rule_applicability(definition)
    expected_rule_ids = [
        rule_id
        for rule_id in package["rule_ids"]
        if rule_id != "AC-COMMON-MERGE-ORDERED"
    ]

    assert definition["status"] == "active"
    assert definition["revision"] == "rev3"
    assert registry.get("direct_main", version="v1")["revision"] == "rev3"
    assert join["authoritative"] is True
    assert join["join_state"] == "resolved"
    assert join["package_id"] == package["package_id"]
    assert join["package_version"] == package["package_version"]
    assert join["package_digest"] == package["package_digest"]
    assert join["rule_ids"] == expected_rule_ids
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
        "applicable_rule_count": len(expected_rule_ids),
        "new_revision_required_after_resolution": False,
    }
    assert definition["rule_layer"] == rev2["rule_layer"]
    assert definition["system_layer"]["retry_policy"] == rev2["system_layer"][
        "retry_policy"
    ]
    assert definition["system_layer"]["terminal_outcome_policy"] == rev2[
        "system_layer"
    ]["terminal_outcome_policy"]
    correction = definition["metadata"]["applicability_correction"]
    assert correction["removed_rule_ids"] == ["AC-COMMON-MERGE-ORDERED"]
    assert correction["rev2_mutated_in_place"] is False
    assert correction["execution_semantics_changed"] is False
    assert correction["gate_predicate_added"] is False
    assert correction["server_inference_allowed"] is False
    assert template["source"]["revision"] == "rev3"
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


def test_mf_parallel_rev10_keeps_nominal_chain_and_forbids_commit_backedge() -> None:
    registry = ContractDefinitionRegistry()
    rev9 = registry.get(
        "mf_parallel.v2", version="v2", revision="rev9"
    )
    rev10 = registry.get(
        "mf_parallel.v2", version="v2", revision="rev10"
    )

    def chain(definition):
        return [
            (
                stage["stage_id"],
                line["line_id"],
                tuple(line.get("requires") or []),
                line["owner_role"],
                line["evidence_kind"],
            )
            for stage in definition["rule_layer"]["stages"]
            for line in stage["lines"]
        ]

    assert chain(rev10) == chain(rev9)
    ordered_line_ids = [line_id for _, line_id, *_ in chain(rev10)]
    assert ordered_line_ids.count("worker_commit") == 1
    assert ordered_line_ids.index("worker_implementation") < ordered_line_ids.index(
        "worker_commit"
    ) < ordered_line_ids.index("worker_finish_time_attestation")
    later_requirements = {
        requirement
        for _, line_id, requirements, *_ in chain(rev10)
        if ordered_line_ids.index(line_id) > ordered_line_ids.index("worker_commit")
        for requirement in requirements
    }
    assert "worker_implementation" not in later_requirements

    applicability = rev10["metadata"]["common_rule_applicability"]
    assert "AC-COMMON-COMMIT-IMMUTABLE-WORKER" in applicability["rule_ids"]
    assert applicability["server_inference_allowed"] is False

    successor_modes = {
        successor.get("mode")
        for successor in rev10["successors"]
        if successor.get("contract_id") == "mf_parallel.v2"
    }
    assert successor_modes == {"same_contract_append_only_failed_qa_rework"}


def test_mf_batch_parallel_rev1_maps_existing_gates_without_nominal_backedge() -> None:
    registry = ContractDefinitionRegistry()
    definition = registry.get(
        "mf_batch_parallel",
        version="v1",
        revision="rev1",
    )
    package = registry.common_rule_package()
    join = registry.resolve_common_rule_applicability(definition)
    expected_chain = [
        ("batch_contract_draft", "observer_bind_batch_parent", "observer"),
        ("batch_scope_review", "observer_batch_preflight", "observer"),
        ("row_fanout_planning", "observer_plan_row_successors", "observer"),
        ("row_successor_dispatch", "observer_bind_row_successors", "observer"),
        ("row_successor_tracking", "observer_bind_row_candidates", "observer"),
        ("batch_integration_epoch", "observer_open_integration_epoch", "observer"),
        ("ordered_batch_merge", "observer_ordered_batch_merge", "observer"),
        ("final_batch_reconcile", "observer_final_reconcile", "observer"),
        ("batch_final_qa", "qa_batch_postmerge_verification", "qa"),
        (
            "protected_child_closures",
            "observer_verify_protected_child_closures",
            "observer",
        ),
        (
            "atomic_coordination_epoch_close",
            "observer_atomic_coordination_epoch_close",
            "observer",
        ),
    ]
    flattened = [
        (stage["stage_id"], line["line_id"], line["owner_role"])
        for stage in definition["rule_layer"]["stages"]
        for line in stage["lines"]
    ]

    assert flattened == expected_chain
    assert join["authoritative"] is True
    assert join["rule_ids"] == package["rule_ids"]
    assert join["scopes"] == ["mf_batch_parallel"]
    assert join["server_inference_allowed"] is False

    line_ids = [line_id for _, line_id, _ in flattened]
    previous_line = ""
    for stage in definition["rule_layer"]["stages"]:
        assert len(stage["lines"]) == 1
        line = stage["lines"][0]
        assert line["allowed_writer_roles"] == [line["owner_role"]]
        assert line.get("requires", []) == (
            [] if not previous_line else [previous_line]
        )
        previous_line = line["line_id"]

    stage_order = {stage_id: index for index, (stage_id, *_rest) in enumerate(expected_chain)}
    for transition in definition["rule_layer"]["transitions"]:
        assert stage_order[transition["from"]] < stage_order[transition["to"]]
    assert not any(
        transition["from"] == "batch_final_qa"
        and transition["to"] in {
            "row_successor_dispatch",
            "batch_integration_epoch",
            "ordered_batch_merge",
        }
        for transition in definition["rule_layer"]["transitions"]
    )

    bindings = definition["metadata"]["gate_bindings"]
    assert bindings["new_predicates_added"] is False
    assert bindings["hidden_server_inference_allowed"] is False
    expected_symbols = {
        "plan_mf_batch_parallel_preflight": "agent/governance/parallel_branch_runtime.py",
        "handle_project_mf_batch_parallel_enter": "agent/governance/server.py",
        "open_or_validate_integration_epoch": "agent/governance/parallel_branch_runtime.py",
        "select_merge_queue_item": "agent/governance/parallel_branch_runtime.py",
        "_contract_runtime_shared_batch_reconcile_authority": "agent/governance/server.py",
        "_contract_runtime_shared_batch_postmerge_qa_activation_verified": "agent/governance/server.py",
        "_contract_runtime_mf_batch_parent_close_authority_gate": "agent/governance/server.py",
        "_parallel_branch_allocate_merged_batch_failed_qa_rework_authority": "agent/governance/server.py",
    }
    assert {
        item["validator_symbol"]: item["implementation_file"]
        for item in bindings["bindings"]
    } == expected_symbols
    assert {
        item["line_id"]
        for item in bindings["bindings"]
        if item.get("line_id")
    }.issubset(set(line_ids))
    for symbol, path in expected_symbols.items():
        implementation = (REPO_ROOT / path).read_text(encoding="utf-8")
        assert f"def {symbol}(" in implementation


def test_mf_batch_parallel_rev1_keeps_170425_as_non_authoritative_witness() -> None:
    definition = ContractDefinitionRegistry().get(
        "mf_batch_parallel",
        version="v1",
        revision="rev1",
    )
    witness = definition["metadata"]["runtime_dogfood"]
    historical = next(
        world
        for world in _happy_path_gate_map()["historical_reference_worlds"]
        if world["lane"] == "mf_batch_parallel"
    )

    assert witness["historical_behavior_witness_commit"] == historical["commit_sha"]
    assert witness["historical_behavior_witness_is_certificate"] is False
    assert historical["current_contract_certificate"] is False
    assert historical["simultaneous_environment"] is False


def test_happy_path_gate_map_pins_three_independent_historical_worlds() -> None:
    gate_map = _happy_path_gate_map()

    assert gate_map["schema_version"] == (
        "aming_claw.contract_rule_gate_map.happy_path.v1"
    )
    assert gate_map["audit_position"] == {
        "project_id": "aming-claw",
        "audited_code_commit": "5de2f28e8e4cfead4b3451edf9aa0af21f8c6ac3",
        "graph_snapshot_id": "full-5de2f28e8e4c-direct-retry",
        "graph_snapshot_kind": "full",
        "graph_snapshot_status": "active",
        "graph_stale": False,
        "pending_scope_reconcile_count": 0,
        "graph_query_trace_id": "gqt-20260823-024720bf7b",
    }
    worlds = gate_map["historical_reference_worlds"]
    assert [world["lane"] for world in worlds] == [
        "direct_main",
        "mf_parallel",
        "mf_batch_parallel",
    ]
    assert [world["commit_sha"] for world in worlds] == [
        "8a6ef43d2454a8f02586f30380a41d6e16a37210",
        "c18af8b3df4971a6beb2c8b07b793dbad1ae6e70",
        "170425064c5e8cdb27e7c86c06b9f2ccaf1b72ee",
    ]
    assert [world["snapshot_id"] for world in worlds] == [
        "full-8a6ef43-a390",
        "full-c18af8b-2540",
        "full-1704250-9d45",
    ]
    assert all(world["simultaneous_environment"] is False for world in worlds)
    assert all(world["current_contract_certificate"] is False for world in worlds)
    assert all(world["formal_no_pass"] is False for world in worlds)


def test_happy_path_gate_map_keeps_rule_gate_guide_authority_explicit() -> None:
    gate_map = _happy_path_gate_map()
    authority = gate_map["authority_model"]
    common = gate_map["source_inventory"]["common_rule_package"]

    assert "selected source-backed Contract revision" in authority["rule_authority"]
    assert "cannot invent a Rule" in authority["gate_authority"]
    assert "one legal Entrance or a typed refusal" in authority["guide_authority"]
    assert "never produces PASS" in authority["bypass_authority"]
    assert common["omitted_rules_apply"] is False
    assert common["server_inference_allowed"] is False

    for lane in gate_map["lanes"].values():
        for mapping in lane["gate_mappings"]:
            assert mapping["rule_refs"]
            assert mapping["validator_symbol"]
            assert mapping["fact_selectors"]
            assert mapping["expected"]
            assert mapping["actual"]
            assert mapping["verdict"] == mapping["disposition"]
            assert mapping["authority_class"] in {
                "rule_validation",
                "template_validation_only",
            }
            implementation_path = REPO_ROOT / mapping["implementation_file"]
            implementation = implementation_path.read_text(encoding="utf-8")
            assert f"def {mapping['validator_symbol']}(" in implementation

    common_rule_ids = set(common["rule_ids"])
    registry = ContractDefinitionRegistry()
    source_backed_lanes = {
        "direct_main": (
            "operator_supervised_direct_main.v1.rev2",
            registry.get(
                "operator_supervised_direct_main",
                version="v1",
                revision="rev2",
            ),
        ),
        "mf_parallel": (
            "mf_parallel.v2.rev10",
            registry.get("mf_parallel.v2", version="v2", revision="rev10"),
        ),
    }
    for lane_name, (contract_ref_prefix, definition) in source_backed_lanes.items():
        lane = gate_map["lanes"][lane_name]
        rule_refs = {
            rule_ref
            for mapping in lane["gate_mappings"]
            for rule_ref in mapping["rule_refs"]
        }
        expected_contract_lines = {
            f"{contract_ref_prefix}:{line['line_id']}"
            for stage in definition["rule_layer"]["stages"]
            for line in stage["lines"]
        }
        mapped_contract_lines = {
            rule_ref
            for rule_ref in rule_refs
            if rule_ref.startswith(f"{contract_ref_prefix}:")
        }

        assert mapped_contract_lines == expected_contract_lines

    direct = gate_map["lanes"]["direct_main"]
    direct_closure = direct["common_rule_closure"]
    direct_mapped_common_rules = {
        rule_ref
        for mapping in direct["gate_mappings"]
        for rule_ref in mapping["rule_refs"]
        if rule_ref in common_rule_ids
    }
    direct_unvalidated_joined_rules = set(
        direct_closure["unvalidated_joined_rule_ids"]
    )
    assert direct_closure["join_state"] == "resolved"
    assert direct_closure["closure_status"] == "contract_applicability_gap"
    assert direct_mapped_common_rules == set(direct_closure["mapped_rule_ids"])
    assert direct_mapped_common_rules.isdisjoint(direct_unvalidated_joined_rules)
    assert direct_mapped_common_rules | direct_unvalidated_joined_rules == (
        common_rule_ids
    )
    assert direct_unvalidated_joined_rules == {"AC-COMMON-MERGE-ORDERED"}
    assert direct_closure["server_inference_allowed"] is False
    assert direct_closure["disposition"] == "CONTRACT_UPDATE"

    parallel = gate_map["lanes"]["mf_parallel"]
    parallel_closure = parallel["common_rule_closure"]
    parallel_mapped_common_rules = {
        rule_ref
        for mapping in parallel["gate_mappings"]
        for rule_ref in mapping["rule_refs"]
        if rule_ref in common_rule_ids
    }
    assert parallel_closure["join_state"] == "resolved"
    assert parallel_closure["closure_status"] == "complete"
    assert parallel_mapped_common_rules == set(
        parallel_closure["mapped_rule_ids"]
    ) == common_rule_ids
    assert parallel_closure["explicit_non_applicability"] == []

    parallel_predicates = {
        mapping["predicate_id"]: mapping for mapping in parallel["gate_mappings"]
    }
    expected_temporal_validators = {
        "parallel.prefill_child_contracts": (
            "mf_parallel.v2.rev10:observer_prefill_child_contracts",
            "_contract_runtime_mf_parallel_prefill_plan_write_errors",
        ),
        "parallel.atomic_dispatch": (
            "mf_parallel.v2.rev10:observer_dispatch_bounded_workers",
            "_contract_runtime_bind_mf_parallel_dispatch_authority",
        ),
        "parallel.worker_finish_time_attestation": (
            "mf_parallel.v2.rev10:worker_finish_time_attestation",
            "handle_graph_governance_runtime_context_finish_time_worker_attestation",
        ),
        "parallel.worker_finish_gate": (
            "mf_parallel.v2.rev10:worker_finish_gate",
            "handle_graph_governance_runtime_context_finish_gate",
        ),
    }
    for predicate_id, (line_ref, validator_symbol) in (
        expected_temporal_validators.items()
    ):
        mapping = parallel_predicates[predicate_id]
        assert line_ref in mapping["rule_refs"]
        assert mapping["validator_symbol"] == validator_symbol

    batch = gate_map["lanes"]["mf_batch_parallel"]
    batch_closure = batch["common_rule_closure"]
    batch_authoritative_rule_refs = {
        rule_ref
        for mapping in batch["gate_mappings"]
        for rule_ref in mapping["rule_refs"]
    }
    batch_unjoined_candidates = {
        rule_ref
        for mapping in batch["gate_mappings"]
        for rule_ref in mapping.get("unjoined_candidate_rule_refs", [])
    }
    assert batch_authoritative_rule_refs.isdisjoint(common_rule_ids)
    assert batch_closure["join_state"] == "missing_source_backed_parent_contract"
    assert batch_closure["closure_status"] == "authority_gap"
    assert batch_closure["mapped_rule_ids"] == []
    assert batch_closure["server_inference_allowed"] is False
    assert batch_closure["disposition"] == "CONTRACT_UPDATE"
    assert batch_unjoined_candidates == set(
        batch_closure["unjoined_candidate_rule_ids"]
    )
    assert batch_unjoined_candidates.issubset(common_rule_ids)
    assert gate_map["orphan_predicate_audit"]["accepted_orphan_predicates"] == []
    orphan_candidate = gate_map["orphan_predicate_audit"]["candidates"][0]
    assert orphan_candidate["drift_gym_case"] == "DC-042"
    assert orphan_candidate["disposition"] == "TRANSPORT_ONLY"
    assert orphan_candidate["rule_change"] is False
    assert orphan_candidate["bypass_used"] is False


def test_happy_path_gate_map_has_four_fail_closed_conformance_classes() -> None:
    gate_map = _happy_path_gate_map()
    classes = gate_map["conformance_classes"]

    assert set(classes) == {
        "missing_validator",
        "under_validation",
        "mis_validation",
        "orphan_predicate",
    }
    assert all(item["pass_allowed"] is False for item in classes.values())
    assert set(classes["orphan_predicate"]["allowed_dispositions"]) == {
        "ROLLBACK",
        "CONTRACT_UPDATE",
        "GUIDE_UPDATE",
        "TRANSPORT_ONLY",
    }


def test_happy_path_global_traces_are_seam_complete_and_terminal() -> None:
    gate_map = _happy_path_gate_map()

    for lane_name, lane in gate_map["lanes"].items():
        trace = lane["global_trace"]
        assert trace
        assert trace[0]["consumes"] == []
        assert trace[-1]["produces"] == [lane["terminal_base_case"]]
        assert lane["terminal_base_case"] == "terminal_fixed"
        assert lane["bypass_in_nominal_trace"] is False
        for previous, current in zip(trace, trace[1:]):
            assert set(current["consumes"]).issubset(previous["produces"]), (
                lane_name,
                previous["stage"],
                current["stage"],
            )

    direct = gate_map["lanes"]["direct_main"]
    parallel = gate_map["lanes"]["mf_parallel"]
    batch = gate_map["lanes"]["mf_batch_parallel"]
    assert direct["concurrency_topology"]["kind"] == "serial"
    assert parallel["concurrency_topology"] == {
        "kind": "fan_out_fan_in",
        "fan_out": 2,
        "fan_in": 2,
        "ordered_merge": True,
        "reconcile_after_fan_in": True,
        "qa_after_reconcile": True,
    }
    assert batch["concurrency_topology"]["ordered_merge"] is True
    assert batch["concurrency_topology"]["durable_epoch"] is True
    assert batch["concurrency_topology"]["single_final_reconcile"] is True
    assert batch["concurrency_topology"]["atomic_parent_close"] is True


def test_happy_path_map_separates_batch_gap_and_release_warranty() -> None:
    gate_map = _happy_path_gate_map()
    batch_contract = gate_map["lanes"]["mf_batch_parallel"]["contract"]
    warranty = gate_map["release_warranty_policy"]
    outcome = gate_map["audit_outcome"]

    assert batch_contract["source_backed_parent_definition_found"] is False
    assert batch_contract["authority_status"] == "gap"
    assert batch_contract["disposition"] == "CONTRACT_UPDATE"
    assert warranty["required_lane_order"] == [
        "direct_main",
        "mf_parallel",
        "mf_batch_parallel",
    ]
    assert warranty["parent_wip_limit"] == 1
    assert warranty["fresh_receipts_required"] is True
    assert warranty["historical_reference_worlds_are_warranty"] is False
    assert warranty["combined_environment_claimed"] is False
    assert warranty["current_status"] == "INSUFFICIENT_EVIDENCE"
    assert outcome["direct_main"] == "CONTRACT_UPDATE"
    assert outcome["mf_parallel"] == "NO_RULE_CHANGE"
    assert outcome["mf_parallel_failed_host_probe"] == "TRANSPORT_ONLY"
    assert outcome["mf_batch_parallel_parent"] == "CONTRACT_UPDATE"
    assert outcome["new_gate_predicates_authorized"] is False
    assert outcome["bypass_used"] is False
