from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from agent.governance import (
    graph_snapshot_store,
    parallel_branch_runtime,
    server,
    task_timeline,
)
from agent.governance.contracts import ContractDefinitionRegistry
from agent.governance.contracts.registry import ContractDependencyUnresolvedError
from agent.governance.contracts.registry import (
    ContractCommonRuleApplicabilityError,
)
from agent.governance.contracts.runtime import (
    ContractRetirementError,
    ContractRuntime,
    ContractRuntimeError,
    SQLiteContractExecutionStore,
    WriteGateDecision,
    _active_failed_qa_line,
    _contract_completion_satisfying_lines,
    _enrich_qa_evidence_provenance,
    _line_status_allows_contract_completion,
    _mf_parallel_worker_commit_errors,
    _project_record_state,
    _qa_authored_pass_atomic_completion_errors,
    _worker_implementation_atomic_advance_errors,
    _worker_commit_completed_implementation,
    terminal_supersession_receipt_for_record,
)
from agent.governance.contracts.execution_state import build_execution_state


def _common_rule_bound_definition(registry, *, package_digest=None):
    package = registry.common_rule_package()
    return {
        "schema_version": "contract_definition.v1",
        "contract_id": "common_rule_bound_runtime_test",
        "version": "v1",
        "revision": "rev1",
        "role": "observer",
        "contract_type": "implementation",
        "status": "active",
        "rule_layer": {
            "stages": [
                {
                    "stage_id": "implementation",
                    "lines": [
                        {
                            "line_id": "observer_implementation",
                            "owner_role": "observer",
                            "allowed_writer_roles": ["observer"],
                            "evidence_kind": "implementation",
                        }
                    ],
                }
            ]
        },
        "instruction_layer": {
            "inline": ["Implement only the bounded common Rule test row."],
            "refs": [],
        },
        "metadata": {
            "common_rule_applicability": {
                "schema_version": "contract_common_rule_applicability.v1",
                "join_state": "resolved",
                "package_id": package["package_id"],
                "package_version": package["package_version"],
                "package_digest": package_digest or package["package_digest"],
                "rule_ids": [
                    "AC-COMMON-IDENTITY-PROJECT-BACKLOG-TASK",
                    "AC-COMMON-SCOPE-OWNED-FILES",
                ],
                "scopes": ["runtime_test"],
                "omitted_rules_apply": False,
                "server_inference_allowed": False,
                "activation_allowed": True,
            }
        },
    }


def test_contract_runtime_pins_and_exposes_one_common_rule_join_for_precheck_write(
    tmp_path,
):
    package_registry = ContractDefinitionRegistry()
    payload = _common_rule_bound_definition(package_registry)
    (tmp_path / "common_rule_bound_runtime_test.v1.rev1.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    runtime = ContractRuntime(ContractDefinitionRegistry(tmp_path))
    created = runtime.start_execution(
        "common_rule_bound_runtime_test",
        project_id="aming-claw",
        backlog_id="AC-COMMON-RULE-RUNTIME-PARITY",
        actor_role="observer",
        contract_execution_id="cex-common-rule-runtime-parity",
    )
    join = created["authoritative_common_rule_join"]
    assert join["authoritative"] is True
    assert join["rule_ids"] == [
        "AC-COMMON-IDENTITY-PROJECT-BACKLOG-TASK",
        "AC-COMMON-SCOPE-OWNED-FILES",
    ]
    assert (
        created["execution_state"]["authoritative_common_rule_join"]
        ["authority_hash"]
        == join["authority_hash"]
    )
    assert (
        created["runtime_guide"]["authoritative_common_rule_join"]
        ["authority_hash"]
        == join["authority_hash"]
    )

    proposed_write = {
        "project_id": created["project_id"],
        "backlog_id": created["backlog_id"],
        "contract_execution_id": created["contract_execution_id"],
        "definition_hash": created["definition_hash"],
        "instruction_bundle_hash": created["instruction_bundle_hash"],
        "execution_state_revision": created["execution_state_revision"],
        "runtime_guide_hash": created["runtime_guide"]["runtime_guide_hash"],
        "stage_id": "implementation",
        "line_id": "observer_implementation",
        "actor_role": "observer",
        "evidence_kind": "implementation",
    }
    precheck = runtime.precheck_line_write(
        created["contract_execution_id"],
        proposed_write,
        actor_role="observer",
    )
    assert precheck["ok"] is True
    assert precheck["would_mutate_completed_lines"] is False
    assert (
        precheck["record"]["authoritative_common_rule_join"]["authority_hash"]
        == join["authority_hash"]
    )

    written = runtime.submit_line_write(
        created["contract_execution_id"],
        proposed_write,
        actor_role="observer",
    )
    assert written["ok"] is True
    assert (
        written["record"]["authoritative_common_rule_join"]["authority_hash"]
        == join["authority_hash"]
    )
    assert precheck["decision"] == written["decision"]


def test_worker_implementation_atomic_advance_requires_compiler_consumption():
    runtime_context_id = "mfrctx-atomic-advance"
    line = {
        "stage_id": "worker_implementation",
        "line_id": "worker_implementation",
        "line_instance_id": f"runtime_context:{runtime_context_id}",
        "runtime_context_id": runtime_context_id,
    }
    stalled = {
        "execution_state": {"completed_lines": []},
        "runtime_guide": {"next_legal_action": dict(line)},
    }
    assert _worker_implementation_atomic_advance_errors(stalled, line) == [
        "worker_implementation_not_completion_satisfying",
        "worker_implementation_atomic_lane_not_advanced",
    ]

    advanced_to_sibling = {
        "execution_state": {
            "completed_lines": [
                {
                    "stage_id": "worker_implementation",
                    "line_id": "worker_implementation",
                    "line_instance_id": f"runtime_context:{runtime_context_id}",
                }
            ]
        },
        "runtime_guide": {
            "next_legal_action": {
                "stage_id": "worker_implementation",
                "line_id": "worker_implementation",
                "line_instance_id": "runtime_context:mfrctx-sibling",
            }
        },
    }
    assert (
        _worker_implementation_atomic_advance_errors(advanced_to_sibling, line)
        == []
    )


def _authenticated_authored_qa_line(
    *,
    baseline_status: str,
) -> dict:
    return {
        "stage_id": "qa",
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "independent_verification",
        "status": "passed",
        "authorization_source": "qa_session_token_ref",
        "observer_impersonation": False,
        "parent_materialization_authorized": False,
        "qa_evidence_provenance": {
            "schema_version": "qa_evidence_provenance.v1",
            "server_derived": True,
            "authorization_source": "qa_session_token_ref",
            "evidence_owner_role": "qa",
            "observer_impersonation": False,
            "parent_materialization_authorized": False,
            "authenticated_qa_binding": {
                "schema_version": (
                    "contract_runtime.authenticated_qa_binding.v1"
                ),
                "server_derived": True,
                "independent_verification_session_matched": True,
                "qa_principal": "qa:writer-compiler-parity",
                "qa_session_id": "ses-writer-compiler-parity",
            },
            "completion_status_gate": {
                "schema_version": (
                    "contract_runtime.qa_completion_status_gate.v1"
                ),
                "source": "contract_runtime_line_write_normalization",
                "server_derived": True,
                "top_level_status_present": True,
                "top_level_status_passing": True,
                "normalized_status": "passed",
                "nested_payload_decision_satisfies": False,
            },
        },
        "tests": [
            {
                "name": "candidate exact suite",
                "status": "passed",
                "passed": 2,
                "failed": 0,
            },
            {
                "name": "frozen baseline observation",
                "status": baseline_status,
                "passed": 6,
                "failed": 1,
                "candidate_new_failures": 0,
            },
        ],
        "verification": {
            "status": "passed",
            "verdict": "passed",
            "candidate_new_failures": 0,
            "overall_release_pass_claimed": True,
        },
    }


def test_qa_authored_pass_atomic_completion_requires_canonical_evidence():
    invalid = _authenticated_authored_qa_line(
        baseline_status="baseline_known_failure_only",
    )
    stalled = {
        "execution_state": {"completed_lines": []},
    }
    assert _qa_authored_pass_atomic_completion_errors(stalled, invalid) == [
        "qa_authored_pass_not_completion_satisfying"
    ]

    canonical = _authenticated_authored_qa_line(
        baseline_status="baseline_observation",
    )
    consumed = {
        "execution_state": {"completed_lines": [deepcopy(canonical)]},
    }
    assert _qa_authored_pass_atomic_completion_errors(consumed, canonical) == []


def test_qa_pass_precheck_and_submit_share_zero_write_compiler_parity(
    tmp_path,
):
    package_registry = ContractDefinitionRegistry()
    definition = _common_rule_bound_definition(package_registry)
    definition.update(
        {
            "contract_id": "qa_writer_compiler_parity_runtime_test",
            "role": "qa",
        }
    )
    definition["rule_layer"]["stages"] = [
        {
            "stage_id": "qa",
            "lines": [
                {
                    "line_id": "qa_independent_verification",
                    "owner_role": "qa",
                    "allowed_writer_roles": ["qa"],
                    "evidence_kind": "independent_verification",
                }
            ],
        }
    ]
    (tmp_path / "qa_writer_compiler_parity_runtime_test.v1.rev1.json").write_text(
        json.dumps(definition),
        encoding="utf-8",
    )
    runtime = ContractRuntime(ContractDefinitionRegistry(tmp_path))
    created = runtime.start_execution(
        "qa_writer_compiler_parity_runtime_test",
        project_id="aming-claw",
        backlog_id="AC-QA-WRITER-COMPILER-PARITY-TEST",
        actor_role="qa",
        contract_execution_id="cex-qa-writer-compiler-parity-test",
    )
    write = {
        **_authenticated_authored_qa_line(
            baseline_status="baseline_known_failure_only",
        ),
        "project_id": created["project_id"],
        "backlog_id": created["backlog_id"],
        "contract_execution_id": created["contract_execution_id"],
        "definition_hash": created["definition_hash"],
        "instruction_bundle_hash": created["instruction_bundle_hash"],
        "execution_state_revision": created["execution_state_revision"],
        "runtime_guide_hash": created["runtime_guide"]["runtime_guide_hash"],
        "qa_session_token_ref": "qa-session-ref-writer-compiler-parity",
    }
    before = runtime.store.get(created["contract_execution_id"])

    precheck = runtime.precheck_line_write(
        created["contract_execution_id"],
        write,
        actor_role="qa",
    )
    submitted = runtime.submit_line_write(
        created["contract_execution_id"],
        write,
        actor_role="qa",
    )
    after = runtime.store.get(created["contract_execution_id"])

    assert precheck["ok"] is False
    assert submitted["ok"] is False
    assert precheck["decision"] == submitted["decision"]
    assert precheck["qa_pass_write_compiler_parity_prevented"] is True
    assert submitted["qa_pass_write_compiler_parity_prevented"] is True
    assert precheck["zero_contract_runtime_write"] is True
    assert submitted["zero_contract_runtime_write"] is True
    assert "baseline_observation" in precheck["remediation"]
    assert after["execution_state_revision"] == before["execution_state_revision"]
    assert after["completed_lines"] == before["completed_lines"] == []


def test_contract_runtime_rejects_digest_mismatch_before_execution_mutation(
    tmp_path,
):
    package_registry = ContractDefinitionRegistry()
    payload = _common_rule_bound_definition(
        package_registry,
        package_digest="sha256:wrong",
    )
    (tmp_path / "common_rule_bound_runtime_test.v1.rev1.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    runtime = ContractRuntime(ContractDefinitionRegistry(tmp_path))

    with pytest.raises(ContractCommonRuleApplicabilityError) as raised:
        runtime.start_execution(
            "common_rule_bound_runtime_test",
            project_id="aming-claw",
            backlog_id="AC-COMMON-RULE-RUNTIME-DIGEST-MISMATCH",
            actor_role="observer",
        )

    assert raised.value.to_dict()["code"] == (
        "common_rule_applicability_digest_mismatch"
    )
    assert runtime.store._records == {}


@pytest.mark.parametrize(
    ("contract_id", "revision"),
    [
        (contract_id, revision)
        for contract_id in (
            "direct_fix",
            "direct_fix.v1",
            "observer_direct_fix.v1",
        )
        for revision in (None, "rev1", "rev2", "rev3", "rev4")
    ],
)
def test_direct_fix_new_execution_is_typed_terminal_retirement(
    contract_id,
    revision,
):
    runtime = ContractRuntime(ContractDefinitionRegistry())

    with pytest.raises(ContractRetirementError) as raised:
        runtime.start_execution(
            contract_id,
            version="v1",
            revision=revision,
            project_id="aming-claw",
            backlog_id="AC-DIRECT-FIX-RETIRED",
            actor_role="observer",
        )

    error = raised.value.to_dict()
    assert error == {
        "schema_version": "direct_fix_retired.v1",
        "code": "direct_fix_retired",
        "error": "direct_fix_retired",
        "status": "rejected",
        "classification": "contract_retirement",
        "retryable": False,
        "message": (
            "direct_fix is terminally retired; file a fresh independently "
            "bounded current-world backlog instead"
        ),
        "historical_evidence_readable": True,
        "historical_execution_scheduler_eligible": False,
        "authorizes_write": False,
        "next_legal_action": {
            "id": "file_fresh_bounded_row",
            "action": "select_or_create_backlog",
            "next_step": (
                "File or select a fresh independently bounded row in the "
                "current world. Never resume, return to, or retry the "
                "historical source execution."
            ),
        },
        "forbidden_backedges": [
            "direct_fix_enter",
            "parent_to_resume",
            "return_to_parent",
            "resume_original_contract",
            "retry_source_backlog_close_after_repair",
        ],
        "contract_id": "direct_fix",
        "version": "v1",
        "revision": "rev4",
        "terminal_retirement": True,
        "supersedes_revisions": ["rev1", "rev2", "rev3"],
    }


def test_direct_fix_frozen_entry_gate_matches_contract_retirement_result():
    definition = ContractDefinitionRegistry().get(
        "direct_fix",
        version="v1",
        revision="rev4",
    )
    contract_result = definition["metadata"]["lifecycle"]["result"]
    status_code, gate_result = server.handle_project_direct_fix_enter(
        SimpleNamespace(
            get_project_id=lambda: "aming-claw",
            body={"backlog_id": "AC-DIRECT-FIX-RETIRED"},
        )
    )

    assert status_code == 409
    for field in (
        "schema_version",
        "error",
        "status",
        "historical_evidence_readable",
        "historical_execution_scheduler_eligible",
        "authorizes_write",
        "next_legal_action",
        "forbidden_backedges",
    ):
        assert gate_result[field] == contract_result[field]
    assert contract_result["code"] == gate_result["error"]


@pytest.mark.parametrize(
    "contract_id",
    [
        "operator_supervised_direct_main",
        "operator_supervised_direct_main.v1",
        "direct_main",
        "direct_main.v1",
    ],
)
def test_direct_main_rev1_dependency_unresolved_prevents_new_execution(
    contract_id,
):
    runtime = ContractRuntime(ContractDefinitionRegistry())

    with pytest.raises(ContractDependencyUnresolvedError) as raised:
        runtime.start_execution(
            contract_id,
            version="v1",
            revision="rev1",
            project_id="aming-claw",
            backlog_id="AC-DIRECT-MAIN-DEPENDENCY-UNRESOLVED",
            actor_role="observer",
            contract_execution_id="cex-direct-main-dependency-unresolved",
        )

    error = raised.value.to_dict()
    assert error["schema_version"] == "contract_dependency_unresolved.v1"
    assert error["code"] == "contract_dependency_unresolved"
    assert error["error"] == "contract_dependency_unresolved"
    assert error["status"] == "rejected"
    assert error["classification"] == "external_dependency"
    assert error["retryable"] is False
    assert error["authorizes_write"] is False
    assert error["activation_ready"] is False
    assert error["contract_id"] == "operator_supervised_direct_main"
    assert error["revision"] == "rev1"
    assert [
        dependency["dependency_id"]
        for dependency in error["unresolved_dependencies"]
    ] == ["AC-CONTRACT-COMMON-SAFETY-RULE-PACKAGE-P0-20260815"]
    assert runtime.store._records == {}


def test_direct_main_rev2_starts_fresh_with_authoritative_common_rule_join():
    registry = ContractDefinitionRegistry()
    runtime = ContractRuntime(registry)

    created = runtime.start_execution(
        "direct_main",
        version="v1",
        revision="rev2",
        project_id="aming-claw",
        backlog_id="AC-DIRECT-MAIN-REV2-FRESH-EXECUTION",
        actor_role="observer",
        contract_execution_id="cex-direct-main-rev2-fresh",
    )

    join = created["authoritative_common_rule_join"]
    package = registry.common_rule_package()
    assert created["revision"] == "rev2"
    assert join["authoritative"] is True
    assert join["join_state"] == "resolved"
    assert join["package_id"] == package["package_id"]
    assert join["package_version"] == package["package_version"]
    assert join["package_digest"] == package["package_digest"]
    assert join["rule_ids"] == package["rule_ids"]
    assert join["scopes"] == ["operator_supervised_direct_main"]
    assert join["omitted_rules_apply"] is False
    assert join["server_inference_allowed"] is False
    assert created["execution_state"]["authoritative_common_rule_join"] == join
    assert created["runtime_guide"]["authoritative_common_rule_join"] == join
    assert set(runtime.store._records) == {"cex-direct-main-rev2-fresh"}

    proposed_write = {
        "project_id": created["project_id"],
        "backlog_id": created["backlog_id"],
        "contract_execution_id": created["contract_execution_id"],
        "definition_hash": created["definition_hash"],
        "instruction_bundle_hash": created["instruction_bundle_hash"],
        "execution_state_revision": created["execution_state_revision"],
        "runtime_guide_hash": created["runtime_guide"]["runtime_guide_hash"],
        "stage_id": "route_gate",
        "line_id": "observer_bind_direct_scope",
        "actor_role": "observer",
        "evidence_kind": "contract_binding",
    }
    precheck = runtime.precheck_line_write(
        created["contract_execution_id"],
        proposed_write,
        actor_role="observer",
    )
    written = runtime.submit_line_write(
        created["contract_execution_id"],
        proposed_write,
        actor_role="observer",
    )
    assert precheck["ok"] is False
    assert precheck["would_mutate_completed_lines"] is False
    assert written["ok"] is False
    assert precheck["decision"] == written["decision"]
    assert precheck["record"]["authoritative_common_rule_join"] == join
    assert written["record"]["authoritative_common_rule_join"] == join
    assert any(
        "server-admitted immutable runtime binding" in error
        for error in written["decision"]["errors"]
    )
    assert runtime.store.get(created["contract_execution_id"])[
        "completed_lines"
    ] == []

    rev1 = registry.get(
        "operator_supervised_direct_main",
        version="v1",
        revision="rev1",
    )
    assert rev1["status"] == "draft"
    assert registry.resolve_common_rule_applicability(rev1)["authoritative"] is False


def test_direct_main_rev3_fresh_selection_does_not_rebind_pinned_rev2():
    registry = ContractDefinitionRegistry()
    runtime = ContractRuntime(registry)
    pinned_rev2 = runtime.start_execution(
        "direct_main",
        version="v1",
        revision="rev2",
        project_id="aming-claw",
        backlog_id="AC-DIRECT-MAIN-PINNED-REV2",
        actor_role="observer",
        contract_execution_id="cex-direct-main-pinned-rev2",
    )
    fresh_rev3 = runtime.start_execution(
        "direct_main",
        version="v1",
        project_id="aming-claw",
        backlog_id="AC-DIRECT-MAIN-FRESH-REV3",
        actor_role="observer",
        contract_execution_id="cex-direct-main-fresh-rev3",
    )

    assert pinned_rev2["revision"] == "rev2"
    assert fresh_rev3["revision"] == "rev3"
    assert pinned_rev2["definition_hash"] != fresh_rev3["definition_hash"]
    assert runtime.store.get("cex-direct-main-pinned-rev2")["revision"] == "rev2"
    assert runtime.store.get("cex-direct-main-fresh-rev3")["revision"] == "rev3"
    assert "AC-COMMON-MERGE-ORDERED" in pinned_rev2[
        "authoritative_common_rule_join"
    ]["rule_ids"]
    assert "AC-COMMON-MERGE-ORDERED" not in fresh_rev3[
        "authoritative_common_rule_join"
    ]["rule_ids"]


def test_mf_batch_parallel_rev1_generic_root_start_is_zero_write(
    tmp_path,
    monkeypatch,
):
    source_path = (
        Path(__file__).resolve().parents[1]
        / "governance"
        / "contract_definitions"
        / "mf_batch_parallel.v1.rev1.json"
    )
    temp_definition = tmp_path / source_path.name
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    temp_definition.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    registry = ContractDefinitionRegistry(tmp_path)
    runtime = ContractRuntime(registry)
    with pytest.raises(ContractRuntimeError, match="contract_root_start_denied"):
        runtime.start_execution(
            "mf_batch_parallel",
            version="v1",
            project_id="aming-claw",
            backlog_id="AC-MF-BATCH-REV1-ROOT-DENIED",
            actor_role="observer",
            contract_execution_id="cex-mf-batch-rev1-root-denied",
        )

    with pytest.raises(ContractRuntimeError, match="unknown contract execution"):
        runtime.store.get("cex-mf-batch-rev1-root-denied")

    monkeypatch.setattr(
        runtime,
        "_parent_contract_identity",
        lambda _execution_id: {
            "contract_id": "onboard_route_guide",
            "version": "service",
        },
    )
    with pytest.raises(
        ContractRuntimeError,
        match="guide_bound_server_projected_batch_parent",
    ):
        runtime.start_execution(
            "mf_batch_parallel",
            version="v1",
            project_id="aming-claw",
            backlog_id="AC-MF-BATCH-REV1-GENERIC-SUCCESSOR-DENIED",
            actor_role="observer",
            contract_execution_id="cex-mf-batch-rev1-generic-successor-denied",
            parent_contract_execution_id="onboard-service-parent",
            root_contract_execution_id="onboard-service-parent",
            contract_chain_id="cchain-onboard-service-parent",
        )
    with pytest.raises(ContractRuntimeError, match="unknown contract execution"):
        runtime.store.get("cex-mf-batch-rev1-generic-successor-denied")


def test_mf_batch_parallel_rev1_failed_qa_rework_is_append_only_generation() -> None:
    definition = ContractDefinitionRegistry().get(
        "mf_batch_parallel",
        version="v1",
        revision="rev1",
    )
    successor = definition["system_layer"]["successor_policy"]
    epoch = definition["system_layer"]["integration_epoch_policy"][
        "failed_qa_rework_generation"
    ]
    retry = definition["system_layer"]["retry_policy"]

    assert successor["terminal_queue_row_rewrite_allowed"] is False
    assert successor["same_generation_retry_allowed"] is False
    assert successor["failed_qa_rework_mode"] == (
        "same_child_contract_append_only_fresh_runtime_context"
    )
    assert successor["maximum_failed_qa_rework_workers"] == 1
    assert successor["fresh_task_identity_required"] is True
    assert successor["fresh_worker_identity_required"] is True
    assert successor["fresh_runtime_context_required"] is True
    assert successor["new_integration_generation_required"] is True
    assert successor["fresh_reconcile_and_qa_required"] is True
    assert successor["implicit_reopen_allowed"] is False
    assert epoch == {
        "source_queue_item_remains_terminal": True,
        "fresh_runtime_context_required": True,
        "fresh_task_and_worker_identity_required": True,
        "exact_target_ref_and_base_binding_required": True,
        "one_repair_merge": True,
        "one_new_current_full_reconcile": True,
        "fresh_independent_qa": True,
        "same_generation_reopen_allowed": False,
        "otherwise": "terminal_refusal",
    }
    assert retry["same_execution_line_rewrite_allowed"] is False
    assert retry["same_generation_retry_allowed"] is False
    assert retry["terminal_queue_row_rewrite_allowed"] is False
    assert retry["append_only_failed_qa_rework_generation_allowed"] is True
    assert retry["bypass_is_retry_authority"] is False
    assert retry["waive_is_pass_authority"] is False


def test_direct_main_rev3_demo_bypass_to_end_stays_no_pass_audit_only():
    registry = ContractDefinitionRegistry()
    runtime = ContractRuntime(registry)

    created = runtime.start_execution(
        "direct_main",
        version="v1",
        project_id="aming-claw",
        backlog_id="AC-DIRECT-MAIN-REV3-FRESH-WORLD-WARRANTY",
        actor_role="observer",
        contract_execution_id="cex-direct-main-rev3-fresh-world-warranty",
    )

    definition = registry.get("direct_main", version="v1")
    package = registry.common_rule_package()
    join = created["authoritative_common_rule_join"]
    rules = {rule["rule_id"]: rule for rule in package["rules"]}

    expected_rule_ids = [
        rule_id
        for rule_id in package["rule_ids"]
        if rule_id != "AC-COMMON-MERGE-ORDERED"
    ]
    assert created["revision"] == definition["revision"] == "rev3"
    assert definition["status"] == "active"
    assert join["package_digest"] == package["package_digest"]
    assert join["rule_ids"] == expected_rule_ids
    assert set(join["rule_ids"]) == set(rules) - {"AC-COMMON-MERGE-ORDERED"}
    assert join["omitted_rules_apply"] is False
    assert join["server_inference_allowed"] is False
    assert (
        definition["system_layer"]["retry_policy"]["same_generation_retry_allowed"]
        is False
    )
    assert (
        definition["system_layer"]["retry_policy"]["fresh_bounded_row_required"]
        is True
    )
    assert "bypass-only authority" in rules[
        "AC-COMMON-MERGE-ORDERED"
    ]["gate_obligation"]
    assert "close authority inferred from bypass" in rules[
        "AC-COMMON-CLOSE-INTEGRITY"
    ]["gate_obligation"]

    record = created
    expected_lines = [
        ("route_gate", "observer_bind_direct_scope", "observer"),
        ("graph_first", "observer_graph_context", "observer"),
        (
            "pre_mutation",
            "observer_direct_implementation_exception",
            "observer",
        ),
        ("implementation", "observer_implementation", "observer"),
        ("qa_graph_context", "qa_graph_context", "qa"),
        ("qa", "qa_independent_verification", "qa"),
        ("reconcile", "observer_reconcile", "observer"),
        ("close_ready", "observer_close_ready", "observer"),
    ]
    for index, (stage_id, line_id, actor_role) in enumerate(expected_lines, 1):
        guide = runtime.current_guide(
            created["contract_execution_id"], actor_role=actor_role
        )
        assert guide["next_legal_action"]["stage_id"] == stage_id
        assert guide["next_legal_action"]["line_id"] == line_id
        bypassed = runtime.bypass_current_line(
            created["contract_execution_id"],
            {
                "bypass_identity": (
                    f"bypass:{created['contract_execution_id']}:{index}:{line_id}"
                ),
                "stage_id": stage_id,
                "line_id": line_id,
                "execution_state_revision": record[
                    "execution_state_revision"
                ],
                "runtime_guide_hash": guide["runtime_guide_hash"],
                "diagnostic_backlog_id": (
                    "AC-DIRECT-MAIN-REV3-FRESH-WORLD-WARRANTY"
                ),
                "classification": "direct_main_demo_bypass_probe",
                "reason": "exercise the explicit no-PASS bypass path",
                "decision": "continue the bounded demo probe as audit only",
                "evidence_refs": [
                    "backlog:AC-DIRECT-MAIN-REV3-FRESH-WORLD-WARRANTY"
                ],
            },
            actor_role=actor_role,
        )
        assert bypassed["ok"] is True, (index, bypassed["decision"])
        assert bypassed["decision"]["no_pass_claim"] is True
        assert bypassed["written_line"]["status"] == "waived"
        assert bypassed["written_line"]["no_pass_claim"] is True
        assert bypassed["record"]["authoritative_common_rule_join"] == join
        record = bypassed["record"]

    assert len(record["completed_lines"]) == len(expected_lines)
    assert {line["status"] for line in record["completed_lines"]} == {"waived"}
    assert all(line["no_pass_claim"] for line in record["completed_lines"])
    assert record["runtime_guide"]["next_legal_action"] in ({}, None)
    assert record["runtime_guide"].get("close_eligible") is not True
    assert not record["runtime_guide"].get("close_authority")
    assert "terminal_disposition" not in record["runtime_guide"]
    assert '"PASS"' not in json.dumps(record["runtime_guide"], sort_keys=True)


@pytest.mark.parametrize("revision", ["rev2", "rev3"])
def test_direct_main_strict_runtime_binding_is_authoritative_at_write_gate(
    revision,
):
    runtime = ContractRuntime(ContractDefinitionRegistry())
    execution_id = f"cex-direct-main-{revision}-strict-binding"
    backlog_id = f"AC-DIRECT-MAIN-{revision.upper()}-STRICT-BINDING"
    route_identity = {
        "route_id": "route-direct-main-rev2-strict-binding",
        "route_context_hash": "sha256:" + "1" * 64,
        "prompt_contract_id": "rprompt-direct-main-rev2-strict-binding",
        "prompt_contract_hash": "sha256:" + "2" * 64,
        "visible_injection_manifest_hash": "sha256:" + "3" * 64,
        "route_token_ref": "rtok-direct-main-rev2-strict-binding",
    }
    binding = {
        "schema_version": "operator_supervised_direct_main.runtime_binding.v1",
        "strict_runtime_binding_required": True,
        "server_derived": True,
        "caller_claims_trusted": False,
        "project_id": "aming-claw",
        "backlog_id": backlog_id,
        "contract_execution_id": execution_id,
        "route_identity": route_identity,
        "owned_files": ["agent/governance/server.py"],
        "target_files": ["agent/governance/server.py"],
        "target_project_root": f"/tmp/direct-main-{revision}",
        "worktree_path": f"/tmp/direct-main-{revision}",
        "base_commit": "a" * 40,
        "target_head_commit": "a" * 40,
        "same_execution_retry_allowed": False,
        "same_generation_retry_allowed": False,
        "post_hoc_pass_backfill_allowed": False,
    }
    binding["binding_hash"] = server.stable_sha256(binding)
    created = runtime.start_execution(
        "operator_supervised_direct_main",
        version="v1",
        revision=revision,
        project_id="aming-claw",
        backlog_id=backlog_id,
        actor_role="observer",
        contract_execution_id=execution_id,
        route_token_ref=route_identity["route_token_ref"],
        metadata={
            "operator_supervised_direct_main_runtime_binding": binding,
        },
    )
    exact = {
        "project_id": created["project_id"],
        "backlog_id": created["backlog_id"],
        "contract_execution_id": execution_id,
        "definition_hash": created["definition_hash"],
        "instruction_bundle_hash": created["instruction_bundle_hash"],
        "execution_state_revision": created["execution_state_revision"],
        "runtime_guide_hash": created["runtime_guide"]["runtime_guide_hash"],
        "stage_id": "route_gate",
        "line_id": "observer_bind_direct_scope",
        "actor_role": "observer",
        "evidence_kind": "contract_binding",
        "payload": {
            "direct_runtime_binding_hash": binding["binding_hash"],
            "direct_runtime_binding": binding,
        },
    }
    precheck = runtime.precheck_line_write(
        execution_id,
        exact,
        actor_role="observer",
    )
    assert precheck["ok"] is True
    before = runtime.store.get(execution_id)

    tampered = deepcopy(exact)
    tampered["payload"]["direct_runtime_binding_hash"] = (
        "sha256:" + "f" * 64
    )
    rejected = runtime.submit_line_write(
        execution_id,
        tampered,
        actor_role="observer",
    )
    assert rejected["ok"] is False
    assert any(
        "exact runtime binding hash" in error
        for error in rejected["decision"]["errors"]
    )
    after = runtime.store.get(execution_id)
    assert after["execution_state_revision"] == before["execution_state_revision"]
    assert after["execution_state"]["completed_lines"] == []

    accepted = runtime.submit_line_write(
        execution_id,
        exact,
        actor_role="observer",
    )
    assert accepted["ok"] is True
    assert accepted["record"]["execution_state_revision"] == (
        before["execution_state_revision"] + 1
    )


@pytest.mark.parametrize(
    ("terminal_case", "failed_line_id", "policy_field"),
    [
        (
            "failed_qa",
            "qa_independent_verification",
            "qa_failure",
        ),
        (
            "failed_close",
            "observer_close_ready",
            "authoritative_close_failure",
        ),
    ],
)
def test_direct_main_rev2_terminal_failure_is_no_pass_not_rework(
    terminal_case,
    failed_line_id,
    policy_field,
):
    runtime = ContractRuntime(ContractDefinitionRegistry())
    execution_id = f"cex-direct-main-rev2-terminal-{terminal_case}"
    backlog_id = f"AC-DIRECT-MAIN-REV2-TERMINAL-{terminal_case.upper()}"
    route_identity = {
        "route_id": f"route-direct-main-rev2-terminal-{terminal_case}",
        "route_context_hash": "sha256:" + "1" * 64,
        "prompt_contract_id": f"rprompt-direct-main-rev2-{terminal_case}",
        "prompt_contract_hash": "sha256:" + "2" * 64,
        "visible_injection_manifest_hash": "sha256:" + "3" * 64,
        "route_token_ref": f"rtok-direct-main-rev2-terminal-{terminal_case}",
    }
    binding = {
        "schema_version": "operator_supervised_direct_main.runtime_binding.v1",
        "strict_runtime_binding_required": True,
        "server_derived": True,
        "caller_claims_trusted": False,
        "project_id": "aming-claw",
        "backlog_id": backlog_id,
        "contract_execution_id": execution_id,
        "route_identity": route_identity,
        "owned_files": ["agent/governance/server.py"],
        "target_files": ["agent/governance/server.py"],
        "target_project_root": "/tmp/direct-main-terminal",
        "worktree_path": "/tmp/direct-main-terminal",
        "base_commit": "a" * 40,
        "target_head_commit": "a" * 40,
        "same_execution_retry_allowed": False,
        "same_generation_retry_allowed": False,
        "post_hoc_pass_backfill_allowed": False,
    }
    binding["binding_hash"] = server.stable_sha256(binding)
    created = runtime.start_execution(
        "operator_supervised_direct_main",
        version="v1",
        revision="rev2",
        project_id="aming-claw",
        backlog_id=backlog_id,
        actor_role="observer",
        contract_execution_id=execution_id,
        route_token_ref=route_identity["route_token_ref"],
        metadata={
            "generic_crud_exposed": False,
            "operator_supervised_direct_main_runtime_binding": binding,
        },
    )
    completed_line_specs = [
        (
            "route_gate",
            "observer_bind_direct_scope",
            "observer",
            "contract_binding",
            "completed",
        ),
        (
            "graph_first",
            "observer_graph_context",
            "observer",
            "graph_trace",
            "completed",
        ),
        (
            "pre_mutation",
            "observer_direct_implementation_exception",
            "observer",
            "observer_direct_implementation_exception",
            "completed",
        ),
        (
            "implementation",
            "observer_implementation",
            "observer",
            "implementation",
            "completed",
        ),
        (
            "qa_graph_context",
            "qa_graph_context",
            "qa",
            "graph_trace",
            "completed",
        ),
        (
            "qa",
            "qa_independent_verification",
            "qa",
            "independent_verification",
            "failed" if terminal_case == "failed_qa" else "completed",
        ),
    ]
    if terminal_case == "failed_close":
        completed_line_specs.extend(
            [
                (
                    "reconcile",
                    "observer_reconcile",
                    "observer",
                    "current_full_reconcile",
                    "completed",
                ),
            ]
        )
    completed_lines = [
        {
            "stage_id": stage_id,
            "line_id": line_id,
            "actor_role": actor_role,
            "evidence_kind": evidence_kind,
            "status": status,
            "payload": {"fixture": "accepted-runtime-line"},
        }
        for stage_id, line_id, actor_role, evidence_kind, status in (
            completed_line_specs
        )
    ]
    persisted = runtime.store.get(execution_id)
    persisted["completed_lines"] = completed_lines
    persisted["execution_state_revision"] = 7
    runtime.store.update(execution_id, persisted)

    if terminal_case == "failed_close":
        preterminal = runtime.current_record(
            execution_id,
            actor_role="observer",
        )
        assert preterminal["runtime_guide"]["next_legal_action"][
            "line_id"
        ] == "observer_close_ready"
        failed_close = runtime.submit_line_write(
            execution_id,
            server._contract_runtime_line_write_body(
                preterminal,
                {
                    "stage_id": "close_ready",
                    "line_id": "observer_close_ready",
                    "evidence_kind": "close_ready",
                    "status": "failed",
                    "payload": {
                        "direct_runtime_binding_hash": binding[
                            "binding_hash"
                        ],
                        "timeline_payload": {
                            "status": "failed",
                            "reason": "authoritative close failed",
                        },
                    },
                },
                actor_role="observer",
            ),
            actor_role="observer",
        )
        assert failed_close["ok"] is True

    terminal = runtime.current_record(execution_id, actor_role="observer")
    disposition = terminal["runtime_guide"]["terminal_disposition"]
    assert terminal["runtime_guide"]["next_legal_action"] is None
    assert terminal["runtime_guide"]["readiness_state"] == "terminal_no_pass"
    assert disposition["schema_version"] == (
        "contract_runtime.pinned_terminal_no_pass_disposition.v1"
    )
    assert disposition["source_line_id"] == failed_line_id
    assert disposition["policy_field"] == policy_field
    assert disposition["authoritative_pass_synthesized"] is False
    assert "failed_qa_rework" not in terminal["runtime_guide"]
    assert "line_bypass_guidance" not in terminal["runtime_guide"]
    assert terminal["execution_state"]["execution_state_hash"] == (
        server.stable_sha256(
            {
                key: value
                for key, value in terminal["execution_state"].items()
                if key != "execution_state_hash"
            }
        )
    )

    before = runtime.store.get(execution_id)
    rejected = runtime.submit_line_write(
        execution_id,
        {
            "project_id": "aming-claw",
            "backlog_id": backlog_id,
            "contract_execution_id": execution_id,
            "definition_hash": created["definition_hash"],
            "instruction_bundle_hash": created["instruction_bundle_hash"],
            "execution_state_revision": terminal[
                "execution_state_revision"
            ],
            "runtime_guide_hash": terminal["runtime_guide"][
                "runtime_guide_hash"
            ],
            "stage_id": (
                "reconcile" if terminal_case == "failed_qa" else "close_ready"
            ),
            "line_id": (
                "observer_reconcile"
                if terminal_case == "failed_qa"
                else "observer_close_ready"
            ),
            "actor_role": "observer",
            "evidence_kind": (
                "current_full_reconcile"
                if terminal_case == "failed_qa"
                else "close_ready"
            ),
        },
        actor_role="observer",
    )
    assert rejected["ok"] is False
    after = runtime.store.get(execution_id)
    assert after == before

def test_ordinary_direct_contract_has_no_retired_world_alias_or_dependency():
    definition = ContractDefinitionRegistry().get(
        "operator_supervised_direct_main",
        version="v1",
        revision="rev1",
    )

    assert "direct_fix" not in json.dumps(definition, sort_keys=True)
    assert set(definition["compat_aliases"]) == {
        "operator_supervised_direct_main.v1",
        "direct_main",
        "direct_main.v1",
    }
    assert {
        successor["contract_id"] for successor in definition["successors"]
    } == {"operator_supervised_direct_main", "mf_parallel.v2"}
    assert definition["system_layer"]["retry_policy"] == {
        "same_execution_retry_allowed": False,
        "same_generation_retry_allowed": False,
        "post_hoc_pass_backfill_allowed": False,
        "failed_source_generation_terminal": True,
        "fresh_bounded_row_required": True,
        "fresh_contract_revision_required_after_dependency_resolution": True,
        "historical_source_retained_as_audit": True,
    }
    oracle = definition["metadata"]["historical_behavior_oracle"]
    assert oracle == {
        "commit_prefix": "8a6ef43",
        "classification": "behavior_oracle_only",
        "authority": False,
        "certificate": False,
        "relabels_historical_execution": False,
        "proves_current_activation": False,
    }


def test_terminal_retirement_does_not_rewrite_pinned_execution(tmp_path):
    active = {
        "schema_version": "contract_definition.v1",
        "contract_id": "pinned_retirement_test",
        "version": "v1",
        "revision": "rev1",
        "role": "observer",
        "contract_type": "implementation",
        "status": "active",
        "rule_layer": {
            "stages": [
                {
                    "stage_id": "work",
                    "lines": [
                        {
                            "line_id": "implementation",
                            "owner_role": "observer",
                            "allowed_writer_roles": ["observer"],
                            "evidence_kind": "implementation",
                        }
                    ],
                }
            ]
        },
        "instruction_layer": {"inline": ["Implement bounded work."], "refs": []},
    }
    (tmp_path / "pinned_retirement_test.v1.rev1.json").write_text(
        json.dumps(active),
        encoding="utf-8",
    )
    original_runtime = ContractRuntime(ContractDefinitionRegistry(tmp_path))
    created = original_runtime.start_execution(
        "pinned_retirement_test",
        project_id="aming-claw",
        backlog_id="AC-PINNED-HISTORY",
        actor_role="observer",
    )
    original_hash = created["definition_hash"]

    retired = {
        **active,
        "revision": "rev2",
        "status": "deprecated",
        "successors": [],
        "system_layer": {
            "entrypoint_policy": {
                "allow_root_start": False,
                "allow_entry": False,
            },
            "successor_policy": {"allow_successor_start": False},
            "write_authority_policy": {
                "agent_facing_generic_crud_allowed": False,
            },
            "graph_binding_policy": {"new_graph_context_allowed": False},
            "route_policy": {
                "start_allowed": False,
                "new_route_allowed": False,
            },
            "lifecycle_policy": {
                "entry_allowed": False,
                "resume_allowed": False,
                "retry_allowed": False,
                "reentry_allowed": False,
                "successor_allowed": False,
            },
        },
        "rule_layer": {
            "stages": [
                {
                    "stage_id": "terminal_retirement",
                    "lines": [
                        {
                            "line_id": "terminal_retirement",
                            "owner_role": "system",
                            "allowed_writer_roles": ["system"],
                            "evidence_kind": "contract_terminal_retirement",
                            "required": False,
                        }
                    ],
                }
            ],
            "transitions": [],
        },
        "metadata": {
            "lifecycle": {
                "state": "terminal_retired",
                "terminal_retirement": True,
                "supersedes_revisions": ["rev1"],
                "new_execution_allowed": False,
                "entry_allowed": False,
                "resume_allowed": False,
                "retry_allowed": False,
                "reentry_allowed": False,
                "successor_allowed": False,
                "historical_pinned_read_allowed": True,
                "result": {
                    "schema_version": "pinned_test_retired.v1",
                    "code": "pinned_test_retired",
                    "error": "pinned_test_retired",
                    "status": "rejected",
                    "classification": "contract_retirement",
                    "retryable": False,
                    "message": "pinned test contract is terminally retired",
                    "historical_evidence_readable": True,
                    "historical_execution_scheduler_eligible": False,
                    "authorizes_write": False,
                    "next_legal_action": {
                        "id": "file_fresh_bounded_row",
                        "action": "select_or_create_backlog",
                        "next_step": "file a fresh bounded row",
                    },
                    "forbidden_backedges": ["resume_original_contract"],
                },
            }
        },
    }
    (tmp_path / "pinned_retirement_test.v1.rev2.json").write_text(
        json.dumps(retired),
        encoding="utf-8",
    )
    current_runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        store=original_runtime.store,
    )
    before = original_runtime.store.get(created["contract_execution_id"])
    pinned = current_runtime.current_record(
        created["contract_execution_id"],
        actor_role="observer",
    )
    assert pinned["revision"] == "rev1"
    assert pinned["definition_hash"] == original_hash
    assert pinned["runtime_guide"]["contract"]["revision"] == "rev1"
    assert pinned["runtime_guide"]["next_legal_action"] is None
    assert pinned["runtime_guide"]["readiness_state"] == "terminal_retired"
    assert pinned["runtime_guide"]["terminal_retirement"]["code"] == (
        "pinned_test_retired"
    )
    assert pinned["execution_state"]["terminal"] is True
    assert pinned["execution_state"]["scheduler_eligible"] is False
    assert pinned["execution_state"]["resume_eligible"] is False
    assert pinned["execution_state"]["write_eligible"] is False
    assert pinned["precheck_decision"]["decision"] == "block"
    assert pinned["precheck_decision"]["gate_id"] == (
        "contract_terminal_retirement"
    )
    assert pinned["precheck_decision"]["errors"] == [
        "pinned_test_retired"
    ]
    assert pinned["precheck_decision"]["contract_definition_hash"] == (
        current_runtime.registry.get(
            "pinned_retirement_test",
            version="v1",
            revision="rev2",
        )["definition_hash"]
    )
    assert original_runtime.store.get(created["contract_execution_id"]) == before

    current_runtime.current_guide(
        created["contract_execution_id"],
        actor_role="observer",
    )
    assert original_runtime.store.get(created["contract_execution_id"]) == before

    for method_name, request in (
        ("precheck_line_write", {}),
        ("submit_line_write", {}),
        ("revise_failed_qa_worker_implementation", {}),
        ("revise_failed_qa_observer_dispatch", {}),
        ("revise_precommit_worker_implementation", {}),
        ("bypass_current_line", {}),
    ):
        with pytest.raises(ContractRetirementError) as raised:
            getattr(current_runtime, method_name)(
                created["contract_execution_id"],
                request,
                actor_role="observer",
            )
        assert raised.value.to_dict()["authorizes_write"] is False
        assert original_runtime.store.get(created["contract_execution_id"]) == before

    for revision in (None, "rev1"):
        with pytest.raises(ContractRetirementError) as raised:
            current_runtime.start_execution(
                "pinned_retirement_test",
                version="v1",
                revision=revision,
                project_id="aming-claw",
                backlog_id="AC-PINNED-HISTORY-NEW",
                actor_role="observer",
            )
        assert raised.value.code == "pinned_test_retired"
        assert raised.value.status == "rejected"
        assert raised.value.retryable is False


def test_current_record_guide_projection_does_not_compete_with_finish_writer_lock(
    tmp_path,
):
    """A sibling guide read must stay read-only while finish owns the writer."""

    definition = {
        "schema_version": "contract_definition.v1",
        "contract_id": "sqlite_read_only_guide",
        "version": "v1",
        "revision": "rev1",
        "role": "mf_sub",
        "contract_type": "mf_parallel",
        "status": "active",
        "rule_layer": {
            "stages": [
                {
                    "stage_id": "worker_finish",
                    "lines": [
                        {
                            "line_id": "worker_finish_gate",
                            "owner_role": "mf_sub",
                            "allowed_writer_roles": ["mf_sub"],
                            "evidence_kind": "mf_subagent_finish_gate",
                        }
                    ],
                }
            ]
        },
        "instruction_layer": {
            "inline": ["Finish through the canonical runtime gate."],
            "refs": [],
        },
    }
    (tmp_path / "sqlite_read_only_guide.v1.rev1.json").write_text(
        json.dumps(definition),
        encoding="utf-8",
    )
    db_path = tmp_path / "contract-runtime.sqlite3"
    writer_conn = sqlite3.connect(db_path, timeout=0.05)
    reader_conn = sqlite3.connect(db_path, timeout=0.05)
    for conn in (writer_conn, reader_conn):
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 50")

    registry = ContractDefinitionRegistry(tmp_path)
    writer_runtime = ContractRuntime(
        registry,
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(writer_conn),
    )
    reader_runtime = ContractRuntime(
        registry,
        instruction_root=tmp_path,
        store=SQLiteContractExecutionStore(reader_conn),
    )
    execution_id = "cex-sqlite-read-only-guide"
    created = writer_runtime.start_execution(
        "sqlite_read_only_guide",
        project_id="daily-planner",
        backlog_id="DP-SQLITE-FINISH",
        contract_execution_id=execution_id,
        actor_role="mf_sub",
        route_token_ref="rtok-sqlite-finish",
    )
    writer_conn.commit()

    writer_conn.execute("BEGIN IMMEDIATE")
    writer_conn.execute(
        "UPDATE contract_runtime_executions SET updated_at = updated_at "
        "WHERE contract_execution_id = ?",
        (execution_id,),
    )
    changes_before_read = reader_conn.total_changes
    guide = reader_runtime.current_record(
        execution_id,
        actor_role="mf_sub",
    )["runtime_guide"]
    assert guide["next_legal_action"]["line_id"] == "worker_finish_gate"
    assert reader_conn.total_changes == changes_before_read

    write = {
        "project_id": created["project_id"],
        "backlog_id": created["backlog_id"],
        "contract_execution_id": execution_id,
        "definition_hash": created["definition_hash"],
        "instruction_bundle_hash": created["instruction_bundle_hash"],
        "execution_state_revision": created["execution_state_revision"],
        "runtime_guide_hash": guide["runtime_guide_hash"],
        "stage_id": "worker_finish",
        "line_id": "worker_finish_gate",
        "actor_role": "mf_sub",
        "evidence_kind": "mf_subagent_finish_gate",
    }
    precheck = reader_runtime.precheck_line_write(
        execution_id,
        write,
        actor_role="mf_sub",
    )
    assert precheck["ok"] is True
    assert reader_conn.total_changes == changes_before_read

    finished = writer_runtime.submit_line_write(
        execution_id,
        write,
        actor_role="mf_sub",
    )
    assert finished["ok"] is True
    assert finished["record"]["execution_state_revision"] == 2
    writer_conn.commit()

    duplicate = writer_runtime.submit_line_write(
        execution_id,
        write,
        actor_role="mf_sub",
    )
    assert duplicate["ok"] is False
    persisted = writer_runtime.store.get(execution_id)
    assert persisted["execution_state_revision"] == 2
    assert len(persisted["completed_lines"]) == 1
    writer_conn.close()
    reader_conn.close()


def test_mf_parallel_rev8_requires_both_worker_lanes_before_merge_and_final_qa():
    definition = json.loads(
        (
            Path(__file__).parents[1]
            / "governance"
            / "contract_definitions"
            / "mf_parallel.v2.rev8.json"
        ).read_text()
    )
    workers = [
        {
            "runtime_context_id": "mfrctx-two-worker-a",
            "task_id": "two-worker-a",
            "parent_task_id": "cex-two-worker",
            "worker_id": "slot-a",
            "worker_slot_id": "slot-a",
            "merge_queue_id": "mq-two-worker-a",
            "line_instance_id": "runtime_context:mfrctx-two-worker-a",
        },
        {
            "runtime_context_id": "mfrctx-two-worker-b",
            "task_id": "two-worker-b",
            "parent_task_id": "cex-two-worker",
            "worker_id": "slot-b",
            "worker_slot_id": "slot-b",
            "merge_queue_id": "mq-two-worker-b",
            "line_instance_id": "runtime_context:mfrctx-two-worker-b",
        },
    ]
    reconcile_policy = definition["system_layer"]["graph_binding_policy"][
        "current_full_reconcile_evidence_policy"
    ]
    assert reconcile_policy["qa_authority_required_before_reconcile"] is False
    assert reconcile_policy["qa_authority_required_after_reconcile"] is True
    assert "qa_authority_alternatives" not in reconcile_policy
    completed = [
        {
            "stage_id": "orchestration",
            "line_id": "observer_prefill_child_contracts",
        },
        {
            "stage_id": "dispatch",
            "line_id": "observer_dispatch_bounded_workers",
            "payload": {
                "worker_count": 2,
                "atomic": True,
                "bounded_workers": workers,
            },
        },
    ]

    def current():
        return build_execution_state(
            definition,
            project_id="aming-claw",
            backlog_id="AC-TWO-WORKER",
            contract_execution_id="cex-two-worker",
            actor_role="observer",
            completed_lines=completed,
        )["next_action"]

    def finish_next():
        action = current()
        completed.append(
            {
                "stage_id": action["stage_id"],
                "line_id": action["line_id"],
                "line_instance_id": action.get("line_instance_id", ""),
                "runtime_context_id": action.get("runtime_context_id", ""),
            }
        )
        return action

    assert current()["runtime_context_id"] == "mfrctx-two-worker-a"
    first = finish_next()
    assert first["line_id"] == "worker_read_runtime_guide"
    assert current()["runtime_context_id"] == "mfrctx-two-worker-b"

    while current()["line_id"] != "worker_finish_gate":
        finish_next()
    assert current()["runtime_context_id"] == "mfrctx-two-worker-a"
    finish_next()
    assert current()["line_id"] == "worker_finish_gate"
    assert current()["runtime_context_id"] == "mfrctx-two-worker-b"
    finish_next()

    assert current()["line_id"] == "observer_merge"
    assert current()["runtime_context_id"] == "mfrctx-two-worker-a"
    finish_next()
    assert current()["line_id"] == "observer_merge"
    assert current()["runtime_context_id"] == "mfrctx-two-worker-b"
    finish_next()
    assert current()["line_id"] == "observer_reconcile"
    finish_next()
    assert current()["line_id"] == "qa_graph_context"
    finish_next()
    assert current()["line_id"] == "qa_independent_verification"


def test_mf_parallel_rev9_preserves_rev8_stage_machine_and_requires_allocation_precheck():
    definitions = {}
    for revision in ("rev8", "rev9"):
        definitions[revision] = json.loads(
            (
                Path(__file__).parents[1]
                / "governance"
                / "contract_definitions"
                / f"mf_parallel.v2.{revision}.json"
            ).read_text()
        )

    rev8 = definitions["rev8"]
    rev9 = definitions["rev9"]
    assert [
        (
            stage["stage_id"],
            [line["line_id"] for line in stage.get("lines") or []],
        )
        for stage in rev9["rule_layer"]["stages"]
    ] == [
        (
            stage["stage_id"],
            [line["line_id"] for line in stage.get("lines") or []],
        )
        for stage in rev8["rule_layer"]["stages"]
    ]

    policy = rev9["system_layer"]["allocation_precheck_policy"]
    assert policy["enabled"] is True
    assert policy["tool"] == "parallel_branch_allocate_precheck"
    assert policy["required_before"] == "initial_parallel_branch_allocate"
    assert policy["expected_lane_count"] == 2
    assert policy["allowed_lane_counts"] == [1, 2]
    assert policy["atomic"] is True
    assert policy["cardinality_source"] == (
        "runtime_guide.effective_worker_cardinality_policy.required_worker_count"
    )
    assert policy["effective_policy_projection"] == (
        "runtime_guide.effective_allocation_precheck_policy"
    )
    assert policy["standalone_policy"] == {
        "expected_lane_count": 2,
        "atomic": True,
        "scope": "standalone_contract",
    }
    assert policy["verified_batch_child_policy"] == {
        "cardinality_source": "verified_batch_child_lineage",
        "expected_lane_count": 1,
        "atomic": False,
        "scope": "per_child_contract",
        "cross_child_union_allowed": False,
        "precheck_each_child_independently": True,
    }
    assert policy["read_only"] is True
    assert policy["submit_returned_bodies_unchanged"] is True
    assert policy["applies_to_stage_types"] == ["mf_sub"]
    assert policy["excluded_stage_types"] == ["failed_qa_rework"]
    cardinality_sensitive_text = json.dumps(rev9, ensure_ascii=False)
    assert "both lane merges" not in cardinality_sensitive_text
    assert "two-lane fan-out" not in cardinality_sensitive_text
    assert "both finished worker lanes" not in cardinality_sensitive_text
    assert "both line instances" not in cardinality_sensitive_text
    assert "all required lane merges" in cardinality_sensitive_text
    assert "initial required-lane fan-out" in cardinality_sensitive_text
    assert "every finished worker lane required" in cardinality_sensitive_text
    assert "every required line instance" in cardinality_sensitive_text
    assert policy["zero_write_surfaces"] == [
        "runtime_context",
        "worktree",
        "merge_queue",
        "timeline",
        "contract_runtime",
    ]
    assert "direct_fix" not in {
        successor["contract_id"] for successor in rev9["successors"]
    }


def test_mf_parallel_rev9_requires_two_lanes_before_merge_and_final_qa():
    definition = json.loads(
        (
            Path(__file__).parents[1]
            / "governance"
            / "contract_definitions"
            / "mf_parallel.v2.rev9.json"
        ).read_text()
    )
    workers = [
        {
            "runtime_context_id": f"mfrctx-rev9-worker-{suffix}",
            "task_id": f"rev9-worker-{suffix}",
            "parent_task_id": "cex-rev9-two-worker",
            "worker_id": f"slot-{suffix}",
            "worker_slot_id": f"slot-{suffix}",
            "merge_queue_id": f"mq-rev9-worker-{suffix}",
            "line_instance_id": f"runtime_context:mfrctx-rev9-worker-{suffix}",
        }
        for suffix in ("a", "b")
    ]
    completed = [
        {
            "stage_id": "orchestration",
            "line_id": "observer_prefill_child_contracts",
        },
        {
            "stage_id": "dispatch",
            "line_id": "observer_dispatch_bounded_workers",
            "payload": {
                "worker_count": 2,
                "atomic": True,
                "bounded_workers": workers,
            },
        },
    ]

    def current():
        return build_execution_state(
            definition,
            project_id="aming-claw",
            backlog_id="AC-REV9-TWO-WORKER",
            contract_execution_id="cex-rev9-two-worker",
            actor_role="observer",
            completed_lines=completed,
        )["next_action"]

    def finish_next():
        action = current()
        completed.append(
            {
                "stage_id": action["stage_id"],
                "line_id": action["line_id"],
                "line_instance_id": action.get("line_instance_id", ""),
                "runtime_context_id": action.get("runtime_context_id", ""),
            }
        )
        return action

    while current()["line_id"] != "observer_merge":
        finish_next()
    assert current()["runtime_context_id"] == "mfrctx-rev9-worker-a"
    finish_next()
    assert current()["line_id"] == "observer_merge"
    assert current()["runtime_context_id"] == "mfrctx-rev9-worker-b"
    finish_next()
    assert current()["line_id"] == "observer_reconcile"
    finish_next()
    assert current()["line_id"] == "qa_graph_context"
    finish_next()
    assert current()["line_id"] == "qa_independent_verification"


def test_mf_parallel_rev10_preserves_rev9_nominal_machine_and_pins_common_rules():
    registry = ContractDefinitionRegistry()
    rev9 = registry.get(
        "mf_parallel.v2", version="v2", revision="rev9"
    )
    rev10 = registry.get(
        "mf_parallel.v2", version="v2", revision="rev10"
    )
    latest = registry.resolve_for_new_execution(
        "mf_parallel.v2", version="v2"
    )
    package = registry.common_rule_package()
    join = registry.resolve_common_rule_applicability(rev10)

    assert rev10["rule_layer"]["stages"] == rev9["rule_layer"]["stages"]
    assert latest["revision"] == "rev10"
    assert join["authoritative"] is True
    assert join["package_id"] == package["package_id"]
    assert join["package_version"] == package["package_version"]
    assert join["package_digest"] == package["package_digest"]
    assert join["rule_ids"] == package["rule_ids"]
    assert join["scopes"] == ["mf_parallel"]
    assert join["omitted_rules_apply"] is False
    assert join["server_inference_allowed"] is False
    assert "AC-COMMON-COMMIT-IMMUTABLE-WORKER" in join["rule_ids"]

    system = rev10["system_layer"]
    assert "accepted_worker_commit_policy" not in system
    assert system["legacy_reconcile_receipt_correction_policy"] == {
        "schema_version": "mf_parallel.legacy_reconcile_receipt_correction_policy.v1",
        "enabled_for_revision": False,
        "revision": "rev10",
        "same_revision_business_evidence_mutation_allowed": False,
        "exact_duplicate_idempotency_transport_only": True,
    }
    successor_ids = {
        successor["contract_id"] for successor in rev10["successors"]
    }
    assert "observer_hotfix" not in successor_ids
    assert "audit_close_with_qa_acceptance.v1" not in successor_ids


def test_mf_parallel_terminal_supersession_receipt_projects_old_record_no_pass():
    source_execution_id = "cex-mf-parallel-source"
    receipt = {
        "schema_version": "mf_parallel.terminal_supersession_receipt.v1",
        "server_derived": True,
        "source_contract_execution_id": source_execution_id,
        "fresh_contract_execution_id": "cex-mf-parallel-fresh",
        "no_pass_claim": True,
        "authoritative_pass_synthesized": False,
        "old_execution_terminal": True,
        "receipt_ref": "timeline:1",
    }
    receipt["receipt_hash"] = server.stable_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_ref"}
    )
    record = {
        "contract_execution_id": source_execution_id,
        "contract_id": "mf_parallel.v2",
        "execution_state_revision": 14,
        "metadata": {"terminal_supersession_receipt": receipt},
    }

    assert terminal_supersession_receipt_for_record(record) == receipt
    projection = _project_record_state(record)
    assert projection["current_contract_execution_id"] == source_execution_id
    assert projection["active_child_contract_execution_id"] == ""
    assert projection["readiness_state"] == "superseded_no_pass"
    assert projection["terminal"] is True
    assert projection["scheduler_eligible"] is False
    assert projection["resume_eligible"] is False
    assert projection["close_eligible"] is False
    assert projection["next_legal_action"] == {}
    assert projection["next_legal_execution_id"] == "cex-mf-parallel-fresh"
    assert projection["terminal_disposition"]["no_pass_claim"] is True
    assert projection["terminal_disposition"]["pass_synthesized"] is False

    tampered = deepcopy(record)
    tampered["metadata"]["terminal_supersession_receipt"][
        "fresh_contract_execution_id"
    ] = "cex-mf-parallel-conflict"
    assert terminal_supersession_receipt_for_record(tampered) == {}
    assert _project_record_state(tampered)["readiness_state"] != (
        "superseded_no_pass"
    )


def test_mf_parallel_rev10_rejects_legacy_reconcile_business_correction(
    monkeypatch,
):
    monkeypatch.setattr(
        server,
        "_contract_runtime_reconcile_receipt_resolution",
        lambda *_args, **_kwargs: {
            "status": "legacy_pending",
            "source_line_index": 7,
            "source_line": {"line_id": "observer_reconcile"},
        },
    )
    record = {
        "contract_id": "mf_parallel.v2",
        "version": "v2",
        "revision": "rev10",
        "contract_execution_id": "cex-rev10-reconcile-correction",
        "completed_lines": [{"line_id": "observer_reconcile"}],
    }
    write = {
        "stage_id": "reconcile",
        "line_id": "observer_reconcile",
        "evidence_kind": "reconcile",
        "payload": {"reconcile_authority": {"record_verified": True}},
    }

    with pytest.raises(server.GovernanceError) as rejected:
        server._contract_runtime_reconcile_receipt_correction(
            None,
            runtime=None,
            project_id="aming-claw",
            record=record,
            write=write,
            actor_role="observer",
            mutate=True,
        )

    assert rejected.value.code == (
        "mf_parallel_rev10_legacy_reconcile_correction_forbidden"
    )
    assert rejected.value.details["writes_performed"] is False
    assert rejected.value.details["mutation_performed"] is False
    assert rejected.value.details[
        "exact_duplicate_idempotency_transport_only"
    ] is True


def test_worker_commit_contract_accepts_target_relative_delta_with_inherited_projection():
    worker_files = [
        "agent/governance/contract_definitions/mf_parallel.v2.rev8.json",
        "agent/governance/server.py",
        "agent/tests/test_contract_registry.py",
        "agent/tests/test_contract_runtime.py",
        "agent/tests/test_graph_governance_api.py",
    ]
    inherited_file = "agent/governance/contracts/execution_state.py"
    runtime_context_id = "mfrctx-inherited-target-worker-commit"
    task_id = "inherited-target-worker-commit"
    worker_id = "inherited-target-slot"
    commit_sha = "c" * 40
    base_commit = "a" * 40
    target_head = "b" * 40
    identity = {
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "parent_task_id": "cex-inherited-target-worker-commit",
        "worker_id": worker_id,
        "worker_slot_id": worker_id,
        "worker_session_id": worker_id,
        "actor_session_principal": worker_id,
        "filer_principal": worker_id,
        "target_project_root": "/tmp/inherited-target-worker-commit",
        "session_token_ref": "wstok-inherited-target-worker-commit",
        "fence_token_hash": "sha256:" + "d" * 64,
        "worker_role": "mf_sub",
        "evidence_owner_role": "mf_sub",
    }
    implementation = {
        "line_id": "worker_implementation",
        "actor_role": "mf_sub",
        "evidence_kind": "implementation",
        "status": "accepted",
        **identity,
        "changed_files": worker_files,
        "graph_trace_ids": ["gqt-inherited-target-worker-commit"],
        "payload": {
            **identity,
            "changed_files": worker_files,
            "graph_trace_ids": ["gqt-inherited-target-worker-commit"],
            "test_results": {"status": "passed", "passed": True},
        },
    }
    record = {
        "contract_execution_id": identity["parent_task_id"],
        "contract_id": "mf_parallel.v2",
        "completed_lines": [implementation],
    }
    normal_projection = {
        "schema_version": (
            "runtime_context.normal_pre_qa_target_head_revision.v1"
        ),
        "source": "server_revalidated_normal_pre_qa_target_head",
        "server_derived": True,
        "post_qa_merge_conflict_recovery": False,
        "runtime_base_commit": base_commit,
        "runtime_target_head_commit": target_head,
        "current_target_baseline_commit": target_head,
        "target_head_boundary_applied": True,
        "target_head_boundary_reason": (
            "runtime_target_head_is_pre_worker_ancestor"
        ),
        "inherited_target_head_files": [inherited_file],
        "worker_authored_candidate_delta_files": worker_files,
        "target_baseline_changes_worker_authored": False,
        "base_to_target_inheritance_preserved": True,
    }
    worker_commit = {
        **identity,
        "changed_files": worker_files,
        "commit_diff_files": worker_files,
        "owned_files": worker_files,
        "graph_trace_ids": ["gqt-inherited-target-worker-commit"],
        "db_verified": True,
        "clean_worktree": True,
        "dirty_files": [],
        "commit_sha": commit_sha,
        "worker_commit_sha": commit_sha,
        "head_commit": commit_sha,
        "immutable_head_commit": commit_sha,
        "validated_head_commit": commit_sha,
        "diff_base_commit": target_head,
        "normal_pre_qa_target_head_revision": normal_projection,
        "observer_impersonation": False,
    }
    write = {
        "stage_id": "worker_commit",
        "line_id": "worker_commit",
        "actor_role": "mf_sub",
        "evidence_kind": "worker_commit",
        "commit_sha": commit_sha,
        "payload": worker_commit,
    }

    assert _mf_parallel_worker_commit_errors(
        record,
        write,
        actor_role="mf_sub",
    ) == ()

    invalid_source = deepcopy(record)
    invalid_source["completed_lines"][0]["payload"]["test_results"] = {
        "status": "partial_sibling_blocked",
        "passed": True,
        "focused_pytest": "blocked_by_unmerged_sibling_task",
        "planner_only_probe": "passed",
        "git_diff_check": "passed",
    }
    assert any(
        "finish-compatible test_results" in error
        for error in _mf_parallel_worker_commit_errors(
            invalid_source,
            write,
            actor_role="mf_sub",
        )
    )

    legacy_candidate_comparison = deepcopy(record)
    legacy_candidate_comparison["completed_lines"][0]["payload"][
        "test_results"
    ] = {
        "schema_version": "runtime_context.worker_test_results.v1",
        "candidate_new_failures": 0,
        "focused_candidate": {
            "status": "passed",
            "failed": 0,
            "passed": 6,
        },
        "expanded_candidate": {
            "status": "passed",
            "failed": 0,
            "passed": 23,
        },
        "immutable_base": {
            "status": "expected_red",
            "failed": 2,
            "passed": 1,
            "selector": (
                "rev8_postmerge_qa_graph_binding or "
                "pre_rev8_qa_graph_binding"
            ),
        },
        "qa_claim": False,
        "release_claim": False,
        "old_world_reuse": False,
    }
    assert _mf_parallel_worker_commit_errors(
        legacy_candidate_comparison,
        write,
        actor_role="mf_sub",
    ) == ()

    cumulative_regression = deepcopy(write)
    cumulative_regression["payload"]["changed_files"] = sorted(
        [inherited_file, *worker_files]
    )
    cumulative_regression["payload"]["commit_diff_files"] = sorted(
        [inherited_file, *worker_files]
    )
    assert any(
        "normal pre-QA worker delta" in error
        for error in _mf_parallel_worker_commit_errors(
            record,
            cumulative_regression,
            actor_role="mf_sub",
        )
    )


def test_mf_parallel_rev8_reconcile_accepts_two_pre_qa_lane_merges(monkeypatch):
    execution_id = "cex-two-worker-reconcile"
    backlog_id = "AC-TWO-WORKER-RECONCILE"
    dispatch_source_ref = (
        f"contract_runtime:{execution_id}:completed_lines:0"
    )
    workers = [
        {
            "runtime_context_id": "mfrctx-reconcile-a",
            "task_id": "reconcile-worker-a",
            "parent_task_id": execution_id,
            "merge_queue_id": "mq-reconcile-a",
        },
        {
            "runtime_context_id": "mfrctx-reconcile-b",
            "task_id": "reconcile-worker-b",
            "parent_task_id": execution_id,
            "merge_queue_id": "mq-reconcile-b",
        },
    ]
    completed = [
        {
            "stage_id": "dispatch",
            "line_id": "observer_dispatch_bounded_workers",
            "actor_role": "observer",
            "evidence_kind": "dispatch_bounded_worker",
            "payload": {
                "worker_count": 2,
                "atomic_dispatch": True,
                "bounded_workers": workers,
            },
        }
    ]
    for index, worker in enumerate(workers, start=1):
        durable = {
            "schema_version": (
                "contract_runtime.observer_merge_durable_authority.v1"
            ),
            "server_derived": True,
            "db_verified": True,
            "project_id": "aming-claw",
            "backlog_id": backlog_id,
            "contract_execution_id": execution_id,
            **worker,
            "merge_commit": str(index) * 40,
            "merge_event_ref": f"timeline:{100 + index}",
            "merge_event_id": 100 + index,
            "merge_event_created_at": f"2026-08-01T00:00:0{index}Z",
            "contract_runtime_dispatch_source_ref": dispatch_source_ref,
            "pre_qa_merge_authorized": True,
            "final_qa_required_after_reconcile": True,
            "close_satisfying": False,
        }
        completed.append(
            {
                "stage_id": "merge",
                "line_id": "observer_merge",
                "line_instance_id": (
                    f"runtime_context:{worker['runtime_context_id']}"
                ),
                "payload": {"durable_merge_authority": durable},
            }
        )
    record = {
        "project_id": "aming-claw",
        "backlog_id": backlog_id,
        "contract_id": "mf_parallel.v2",
        "revision": "rev8",
        "contract_execution_id": execution_id,
        "completed_lines": completed,
    }
    monkeypatch.setattr(
        server,
        "_contract_runtime_current_full_reconcile_authority_from_merge",
        lambda *_args, **_kwargs: {},
    )

    authority = server._contract_runtime_reconcile_record_authority(
        None,
        project_id="aming-claw",
        record=record,
    )

    assert authority["record_verified"] is True
    assert authority["merge_projection_verified"] is True
    assert authority["dispatch_lineage_verified"] is True
    assert authority["all_lane_merges_verified"] is True
    assert authority["lane_merge_count"] == 2
    assert authority["lane_runtime_context_ids"] == [
        "mfrctx-reconcile-a",
        "mfrctx-reconcile-b",
    ]
    assert authority["merged_commit_sha"] == "2" * 40
    assert authority["merge_event_id"] == 102
    assert authority["reconcile_event_recorded"] is False

    captured = {}

    def enrich_reconcile(
        _conn,
        *,
        project_id,
        record,
        context,
        timeline_events,
        merge,
    ):
        captured["resolver_merge"] = dict(merge)
        return {
            **merge,
            "reconcile_source_ref": "timeline:103",
            "reconcile_event_id": 103,
            "reconcile_event_created_at": "2026-08-01T00:00:03Z",
            "reconcile_task_id": context.task_id,
            "reconcile_runtime_context_id": context.runtime_context_id,
        }

    def current_full(
        _conn,
        *,
        project_id,
        record,
        merge,
        reconcile,
    ):
        captured["current_full_merge"] = dict(merge)
        captured["current_full_reconcile"] = dict(reconcile)
        return {**merge, "db_verified": True}

    final_worker = workers[1]
    context = SimpleNamespace(
        **final_worker,
        backlog_id=backlog_id,
        batch_id="",
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_contexts_for_dispatch_line",
        lambda *_args, **_kwargs: [context],
    )
    monkeypatch.setattr(
        server,
        "_runtime_context_service_timeline_events",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_completed_merge_reconcile_authority",
        enrich_reconcile,
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_current_full_reconcile_authority_from_merge",
        current_full,
    )

    close_authority = server._contract_runtime_current_full_reconcile_authority(
        None,
        project_id="aming-claw",
        record=record,
    )

    assert captured["resolver_merge"]["all_lane_merges_verified"] is True
    assert captured["resolver_merge"]["close_satisfying"] is False
    assert captured["current_full_reconcile"]["reconcile_event_id"] == 103
    assert close_authority["db_verified"] is True


def test_rev9_reconcile_record_resolves_final_two_lane_merge_timeline(
    monkeypatch,
):
    execution_id = "cex-rev9-reconcile-record"
    backlog_id = "AC-REV9-RECONCILE-RECORD"
    workers = [
        {
            "runtime_context_id": "mfrctx-rev9-reconcile-record-a",
            "task_id": "rev9-reconcile-record-a",
            "parent_task_id": execution_id,
            "merge_queue_id": "mq-rev9-reconcile-record-a",
        },
        {
            "runtime_context_id": "mfrctx-rev9-reconcile-record-b",
            "task_id": "rev9-reconcile-record-b",
            "parent_task_id": execution_id,
            "merge_queue_id": "mq-rev9-reconcile-record-b",
        },
    ]
    dispatch_ref = f"contract_runtime:{execution_id}:completed_lines:0"
    completed_lines = [
        {
            "stage_id": "dispatch",
            "line_id": "observer_dispatch_bounded_workers",
            "actor_role": "observer",
            "evidence_kind": "dispatch_bounded_worker",
            "payload": {
                "worker_count": 2,
                "required_worker_count": 2,
                "atomic_dispatch": True,
                "bounded_workers": workers,
            },
        }
    ]
    for index, worker in enumerate(workers, start=1):
        completed_lines.append(
            {
                "stage_id": "observer_lane_merge",
                "line_id": "observer_merge",
                "actor_role": "observer",
                "evidence_kind": "merge",
                "line_instance_id": (
                    f"runtime_context:{worker['runtime_context_id']}"
                ),
                "payload": {
                    "durable_merge_authority": {
                        "schema_version": (
                            "contract_runtime.observer_merge_durable_authority.v1"
                        ),
                        "server_derived": True,
                        "db_verified": True,
                        "pre_qa_merge_authorized": True,
                        "final_qa_required_after_reconcile": True,
                        "project_id": "aming-claw",
                        "backlog_id": backlog_id,
                        "contract_execution_id": execution_id,
                        **worker,
                        "queue_item_id": f"mqi-rev9-reconcile-record-{index}",
                        "merge_commit": str(index) * 40,
                        "merge_event_ref": f"timeline:{200 + index}",
                        "merge_event_id": 200 + index,
                        "merge_event_created_at": (
                            f"2026-08-05T10:00:0{index}Z"
                        ),
                        "contract_runtime_dispatch_source_ref": dispatch_ref,
                    }
                },
            }
        )
    record = {
        "project_id": "aming-claw",
        "backlog_id": backlog_id,
        "contract_id": "mf_parallel.v2",
        "version": "v2",
        "revision": "rev9",
        "contract_execution_id": execution_id,
        "completed_lines": completed_lines,
    }
    final_context = SimpleNamespace(
        **workers[-1],
        backlog_id=backlog_id,
        batch_id="",
    )
    resolver_calls = []

    monkeypatch.setattr(
        server,
        "_contract_runtime_contexts_for_dispatch_line",
        lambda *_args, **_kwargs: [final_context],
    )
    monkeypatch.setattr(
        server,
        "_runtime_context_service_timeline_events",
        lambda *_args, **_kwargs: [
            {
                "id": 203,
                "project_id": "aming-claw",
                "backlog_id": backlog_id,
                "task_id": workers[-1]["task_id"],
                "event_type": "graph.reconcile",
                "event_kind": "reconcile",
                "phase": "reconcile",
                "status": "passed",
                "payload": {
                    "actor_role": "observer",
                    "runtime_context_id": workers[-1][
                        "runtime_context_id"
                    ],
                    "task_id": workers[-1]["task_id"],
                },
                "created_at": "2026-08-05T10:00:03Z",
            }
        ],
    )

    def resolve_reconcile(
        _conn,
        *,
        project_id,
        record,
        context,
        timeline_events,
        merge,
    ):
        resolver_calls.append(
            {
                "project_id": project_id,
                "context": context.runtime_context_id,
                "timeline_event_ids": [event["id"] for event in timeline_events],
                "merge": dict(merge),
            }
        )
        return {
            **merge,
            "reconcile_source_ref": "timeline:203",
            "reconcile_event_id": 203,
            "reconcile_event_created_at": "2026-08-05T10:00:03Z",
            "reconcile_task_id": context.task_id,
            "reconcile_runtime_context_id": context.runtime_context_id,
        }

    monkeypatch.setattr(
        server,
        "_contract_runtime_completed_merge_reconcile_authority",
        resolve_reconcile,
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_current_full_reconcile_authority_from_merge",
        lambda *_args, **_kwargs: {},
    )

    receipt = server._contract_runtime_reconcile_record_authority(
        object(),
        project_id="aming-claw",
        record=record,
    )

    assert len(resolver_calls) == 1
    assert resolver_calls[0]["project_id"] == "aming-claw"
    assert resolver_calls[0]["context"] == workers[-1]["runtime_context_id"]
    assert resolver_calls[0]["timeline_event_ids"] == [203]
    assert resolver_calls[0]["merge"]["all_lane_merges_verified"] is True
    assert receipt["record_verified"] is True
    assert receipt["reconcile_event_recorded"] is True
    assert receipt["reconcile_event_id"] == 203
    assert receipt["reconcile_source_ref"] == "timeline:203"


def test_current_full_reconcile_event_declares_authenticated_observer_role(
    monkeypatch,
):
    from agent.governance import task_timeline

    captured = {}

    def capture_event(_conn, **kwargs):
        captured.update(kwargs)
        return {"id": 301, "created_at": "2026-08-05T10:30:00Z", **kwargs}

    monkeypatch.setattr(task_timeline, "record_event", capture_event)
    event = server._record_pending_scope_reconcile_contract_event(
        None,
        project_id="aming-claw",
        body={
            "backlog_id": "AC-DECLARED-RECONCILE-ROLE",
            "task_id": "worker-declared-reconcile-role",
            "actor": "codex-observer",
        },
        result={
            "ok": True,
            "status": "complete",
            "snapshot_id": "full-declared-reconcile-role",
            "active_snapshot_id": "full-declared-reconcile-role",
            "current_full_reconcile": True,
            "strategy": "current_full_reconcile",
            "head_commit": "a" * 40,
            "active_graph_commit": "a" * 40,
            "activated": True,
            "activation_verification": {
                "verified": True,
                "active_graph_commit": "a" * 40,
                "active_snapshot_id": "full-declared-reconcile-role",
            },
        },
        target_commit_sha="a" * 40,
        runtime_context_scope={
            "project_id": "aming-claw",
            "backlog_id": "AC-DECLARED-RECONCILE-ROLE",
            "task_id": "worker-declared-reconcile-role",
            "parent_task_id": "cex-declared-reconcile-role",
            "runtime_context_id": "mfrctx-declared-reconcile-role",
            "merge_queue_id": "mq-declared-reconcile-role",
            "contract_execution_id": "cex-declared-reconcile-role",
        },
        declared_actor_role="observer",
        post_commit_hooks=False,
    )

    assert captured["actor"] == "codex-observer"
    assert captured["payload"]["actor_role"] == "observer"
    assert server._contract_runtime_declared_timeline_actor_role(event) == (
        "observer"
    )
    roleless = deepcopy(event)
    roleless["payload"].pop("actor_role")
    assert roleless["actor"] == "codex-observer"
    assert server._contract_runtime_declared_timeline_actor_role(roleless) == ""


def test_mf_parallel_rev8_failed_final_qa_routes_to_bounded_worker_fix():
    record = {
        "contract_id": "mf_parallel.v2",
        "revision": "rev8",
        "contract_execution_id": "cex-two-worker-failed-qa",
        "runtime_guide": {
            "next_legal_action": {
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "owner_role": "qa",
            }
        },
        "completed_lines": [
            {
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "actor_role": "qa",
                "evidence_kind": "independent_verification",
                "status": "failed",
                "payload": {"status": "failed", "verdict": "FAIL"},
            }
        ],
    }

    state = server._runtime_current_state_from_record(record)

    assert state["readiness_state"] == "failed_qa_worker_fix_required"
    assert state["next_legal_action"] == {
        "id": "enter_direct_fix_successor",
        "action": "enter_direct_fix_successor",
        "owner_role": "observer",
        "recommended_successor_contract_id": "direct_fix",
        "failed_qa_source_ref": (
            "contract_runtime:cex-two-worker-failed-qa:completed_lines:0"
        ),
        "bounded_worker_fix_required": True,
        "fresh_qa_session_required": True,
        "return_sequence": [
            "observer_merge",
            "observer_reconcile",
            "qa_graph_context",
            "qa_independent_verification",
        ],
    }


def test_mf_parallel_rev9_failed_final_qa_routes_to_same_contract_worker_fix():
    record = {
        "contract_id": "mf_parallel.v2",
        "revision": "rev9",
        "contract_execution_id": "cex-rev9-failed-qa",
        "runtime_guide": {
            "next_legal_action": {
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "owner_role": "qa",
            }
        },
        "completed_lines": [
            {
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "actor_role": "qa",
                "evidence_kind": "independent_verification",
                "status": "failed",
                "payload": {"status": "failed", "verdict": "FAIL"},
            }
        ],
    }

    state = server._runtime_current_state_from_record(record)

    assert state["readiness_state"] == "failed_qa_worker_fix_required"
    assert state["next_legal_action"] == {
        "id": "allocate_bounded_worker_fix",
        "action": "parallel_branch_allocate",
        "owner_role": "observer",
        "recommended_successor_contract_id": "mf_parallel.v2",
        "failed_qa_source_ref": (
            "contract_runtime:cex-rev9-failed-qa:completed_lines:0"
        ),
        "bounded_worker_fix_required": True,
        "successor_mode": "same_contract_append_only_failed_qa_rework",
        "worker_fix_cardinality": 1,
        "allocation_precheck_required": False,
        "allocation_tool": "parallel_branch_allocate",
        "allocation_request_requirements": {
            "stage_type": "failed_qa_rework",
            "minimum_attempt": 2,
            "failed_qa_source_ref": (
                "contract_runtime:cex-rev9-failed-qa:completed_lines:0"
            ),
            "fresh_runtime_context_required": True,
        },
        "direct_fix_allowed": False,
        "fresh_qa_session_required": True,
        "return_sequence": [
            "observer_merge",
            "observer_reconcile",
            "qa_graph_context",
            "qa_independent_verification",
        ],
    }


def test_mf_parallel_rev10_failed_qa_files_copy_safe_fresh_repair_row():
    acceptance_criteria = [
        {
            "id": "AC-REV10-FAILED-QA-FRESH-REPAIR",
            "text": (
                "Failed QA must advertise a fresh bounded row without reusing "
                "the completed worker generation."
            ),
            "required_scope": [
                "agent/governance/server.py",
                "agent/tests/test_contract_runtime.py",
            ],
        }
    ]
    old_identity = {
        "runtime_context_id": "mfrctx-rev10-stale-source",
        "task_id": "rev10-stale-source-worker",
        "parent_task_id": "cex-rev10-stale-source",
        "route_token_ref": "rtok-rev10-stale-source",
        "worktree_path": "/tmp/rev10-stale-source",
        "branch_ref": "refs/heads/codex/rev10-stale-source",
        "merge_queue_id": "mq-rev10-stale-source",
    }
    record = {
        "project_id": "aming-claw",
        "backlog_id": "AC-REV10-FAILED-QA-SOURCE",
        "contract_id": "mf_parallel.v2",
        "revision": "rev10",
        "contract_execution_id": "cex-rev10-stale-source",
        "route_token_ref": old_identity["route_token_ref"],
        "metadata": {
            "acceptance_criteria": acceptance_criteria,
            "acceptance_scope_closure": {
                "row_declared_files": [
                    "agent/governance/server.py",
                    "agent/tests/test_contract_runtime.py",
                ],
                "required_file_union": [
                    "agent/governance/server.py",
                    "agent/tests/test_contract_runtime.py",
                ],
            },
            "observer_prefill_child_plan": {
                "row_owned_files": [
                    "agent/governance/server.py",
                    "agent/tests/test_contract_runtime.py",
                ],
                "row_test_files": ["agent/tests/test_contract_runtime.py"],
                "acceptance_criteria": acceptance_criteria,
                "lanes": [
                    {
                        "owned_files": ["agent/governance/server.py"],
                        "test_files": [],
                    },
                    {
                        "owned_files": [
                            "agent/tests/test_contract_runtime.py"
                        ],
                        "test_files": [
                            "agent/tests/test_contract_runtime.py"
                        ],
                    },
                ],
            },
        },
        "runtime_guide": {
            "next_legal_action": {
                "id": "worker_read_runtime_guide",
                "action": "record_read_receipt",
                "stage_id": "worker_read",
                "line_id": "worker_read_runtime_guide",
                "owner_role": "mf_sub",
                **old_identity,
            }
        },
        "completed_lines": [
            {
                "stage_id": "dispatch",
                "line_id": "observer_dispatch_bounded_workers",
                "actor_role": "observer",
                "evidence_kind": "dispatch_bounded_worker",
                "payload": dict(old_identity),
            },
            {
                "stage_id": "merge",
                "line_id": "observer_merge",
                "actor_role": "observer",
                "evidence_kind": "merge_result",
                "status": "completed",
            },
            {
                "stage_id": "merge",
                "line_id": "observer_merge",
                "actor_role": "observer",
                "evidence_kind": "merge_result",
                "status": "completed",
            },
            {
                "stage_id": "reconcile",
                "line_id": "observer_reconcile",
                "actor_role": "observer",
                "evidence_kind": "reconcile_result",
                "status": "completed",
            },
            {
                "stage_id": "qa",
                "line_id": "qa_graph_context",
                "actor_role": "qa",
                "evidence_kind": "graph_trace",
                "status": "completed",
            },
            {
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "actor_role": "qa",
                "evidence_kind": "independent_verification",
                "status": "failed",
                "payload": {
                    "status": "failed",
                    "verdict": "FAIL",
                    "acceptance_failed": [
                        "AC-REV10-FAILED-QA-FRESH-REPAIR"
                    ],
                },
            },
        ],
    }

    state = server._runtime_current_state_from_record(record)
    repeated = server._runtime_current_state_from_record(deepcopy(record))
    response = server._contract_runtime_response(
        record,
        actor_role="observer",
        response_view="cli_current",
    )

    assert state["readiness_state"] == (
        "failed_qa_fresh_repair_successor_required"
    )
    action = state["next_legal_action"]
    assert action == repeated["next_legal_action"]
    assert response["next_legal_action"] == action
    assert action["id"] == "file_fresh_bounded_row"
    assert action["action"] == action["mcp_tool"] == "backlog_upsert"
    assert action["owner_role"] == "observer"
    assert action["actionable"] is True
    assert action["action_input_ready"] is True
    assert action["fresh_authority_required"] is True
    assert action["source_generation_terminal"] is True
    assert action["same_row_resume_allowed"] is False
    assert "accepted_dispatch_authority" not in state
    assert "mf_sub_host_bridge_guidance" not in state
    body = action["copy_safe_body"]
    assert body == action["action_input"]
    assert body["project_id"] == "aming-claw"
    assert body["bug_id"].startswith(
        "AC-REV10-FAILED-QA-SOURCE-QA-REPAIR-"
    )
    assert body["status"] == "OPEN"
    assert body["mf_type"] == "chain_rescue"
    assert body["target_files"] == ["agent/governance/server.py"]
    assert body["test_files"] == ["agent/tests/test_contract_runtime.py"]
    assert body["acceptance_criteria"] == acceptance_criteria
    assert body["provenance_paths"] == [action["failed_qa_source_ref"]]
    assert action["failed_qa_source_ref"].startswith(
        "contract_runtime:cex-rev10-stale-source:completed_lines:"
    )
    assert action["canonical_executable_action"]["copy_safe_body"] == body
    assert action["next_after_success"]["body"]["backlog_id"] == body[
        "bug_id"
    ]
    assert action["historical_contract_successor"] == {
        "contract_id": "direct_fix",
        "status": "terminal_retired",
        "historical_evidence_readable": True,
        "scheduler_eligible": False,
        "authorizes_write": False,
    }
    rendered_body = json.dumps(body, sort_keys=True)
    for identity_field in (
        "runtime_context_id",
        "task_id",
        "route_token_ref",
        "worktree_path",
        "branch_ref",
        "merge_queue_id",
    ):
        assert identity_field not in body
        assert old_identity[identity_field] not in rendered_body
    assert old_identity["parent_task_id"] in body["provenance_paths"][0]


def _rev10_failed_qa_fresh_repair_record(
    acceptance_criteria,
    *,
    backlog_id="AC-REV10-FAILED-QA-UNSAFE-AC",
    contract_execution_id="cex-rev10-unsafe-ac-source",
):
    return {
        "project_id": "aming-claw",
        "backlog_id": backlog_id,
        "contract_id": "mf_parallel.v2",
        "revision": "rev10",
        "contract_execution_id": contract_execution_id,
        "metadata": {
            "acceptance_criteria": deepcopy(acceptance_criteria),
            "acceptance_scope_closure": {
                "row_declared_files": [
                    "agent/governance/server.py",
                    "agent/tests/test_contract_runtime.py",
                ]
            },
            "observer_prefill_child_plan": {
                "row_test_files": ["agent/tests/test_contract_runtime.py"],
            },
        },
        "runtime_guide": {
            "next_legal_action": {
                "line_id": "worker_read_runtime_guide",
            }
        },
        "completed_lines": [
            {
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "actor_role": "qa",
                "evidence_kind": "independent_verification",
                "status": "failed",
                "payload": {"status": "failed", "verdict": "FAIL"},
            }
        ],
    }


_SERVER_CANONICAL_AUTHORITY_PROVEN_GAPS = (
    "contract_runtime_completed_lines_hash",
    "timeline_worker_write_hash",
    "timeline_worker_write_count",
    "authenticated_qa_binding",
    "evidence_owner_session",
    "evidence_owner_session_ref",
    "submitter_session",
    "current_full_reconcile",
    "next_legal_action",
    "generation",
    "onboard_service_waiver",
)


_TYPED_AUTHORITY_PROVEN_GAPS = (
    "candidate_diff_sha256",
    "manual_merge_event_ref",
    "overwritten_candidate_commit",
    "merge_preview_id",
    "actual_worktree_head",
    "initial_join_event_ref",
    "actual_worker_head",
    "previous_target_head",
    "prior_rejoin_event_ref",
    "current_target_commit",
    "checkpoint_commit",
    "target_head_before_merge",
    "provenance_id",
)


_FRESH_REPAIR_BENIGN_SEMANTIC_KEYS = (
    "payload",
    "verification",
    "artifact_refs",
    "action",
    "error",
    "message",
    "reason",
    "current_state",
    "producer",
    "query_purpose",
    "query_source",
)


_FRESH_REPAIR_EXTRA_SEMANTIC_KEYS = (
    "expected",
    "actual",
    "result",
    "tool",
    "method",
    "path",
    "phase",
    "event_type",
    "dependencies",
    "constraints",
    "operation_id",
    "malformed_input_outcome",
    "given",
    "when",
    "then",
)


_FRESH_REPAIR_MIXED_QA_LEDGER_BENIGN_FIELDS = (
    "candidate_specific_issues",
    "candidate_new_failures",
    "base_reproduction",
    "candidate_suite_counts",
    "refs",
)


_FRESH_REPAIR_MIXED_QA_LEDGER_AUTHORITY_FIELDS = (
    "base_commit_sha",
    "candidate_commit_sha",
)


_FRESH_REPAIR_SOURCE_QUALIFIED_SAFE_FIELDS = (
    (
        "_CONTRACT_RUNTIME_AUTHORITY_TOP_LEVEL_FIELDS",
        "evidence_kind",
        "plain semantic evidence kind",
    ),
    (
        "_ONBOARD_RUNTIME_CONTEXT_STARTUP_COPY_SAFE_FIELDS",
        "harness_type",
        "plain semantic harness type",
    ),
    (
        "_PARALLEL_BRANCH_ALLOCATION_PRECHECK_RECEIPT_BOUND_FIELDS",
        "acceptance_criteria",
        ["plain nested acceptance label"],
    ),
    (
        "_RUNTIME_CONTEXT_LEGACY_REJOIN_LEASE_KEYS",
        "invalid_reason",
        "plain lease diagnostic",
    ),
    (
        "_RUNTIME_CONTEXT_LEGACY_REJOIN_LEASE_KEYS",
        "renewal_endpoint",
        "/plain/semantic/renewal",
    ),
    (
        "_RUNTIME_CONTEXT_LEGACY_REJOIN_LEASE_KEYS",
        "renewal_supported",
        True,
    ),
    (
        "_RUNTIME_CONTEXT_LEGACY_REJOIN_LEASE_KEYS",
        "renewal_default_ttl_seconds",
        900,
    ),
    (
        "_RUNTIME_CONTEXT_LEGACY_REJOIN_LEASE_KEYS",
        "renewal_max_ttl_seconds",
        3600,
    ),
    (
        "_RUNTIME_CONTEXT_LEGACY_REJOIN_NOT_APPLICABLE_AUTHORITY_KEYS",
        "errors",
        ["plain rejoin diagnostic"],
    ),
    (
        "_RUNTIME_CONTEXT_LEGACY_REJOIN_NOT_APPLICABLE_AUTHORITY_KEYS",
        "identity_mismatches",
        ["plain identity diagnostic"],
    ),
    (
        "_RUNTIME_CONTEXT_LEGACY_REJOIN_REPLACEMENT_AUTHORITY_KEYS",
        "errors",
        ["plain replacement diagnostic"],
    ),
    (
        "_RUNTIME_CONTEXT_LEGACY_REJOIN_REPLACEMENT_AUTHORITY_KEYS",
        "identity_mismatches",
        ["plain replacement identity diagnostic"],
    ),
)


_FRESH_REPAIR_MIXED_SOURCE_AUTHORITY_NEIGHBORS = (
    ("_CONTRACT_RUNTIME_AUTHORITY_TOP_LEVEL_FIELDS", "contract_execution_id"),
    ("_ONBOARD_RUNTIME_CONTEXT_STARTUP_COPY_SAFE_FIELDS", "runtime_context_id"),
    (
        "_PARALLEL_BRANCH_ALLOCATION_PRECHECK_RECEIPT_BOUND_FIELDS",
        "task_id",
    ),
    ("_RUNTIME_CONTEXT_LEGACY_REJOIN_LEASE_KEYS", "lease_id"),
    ("_RUNTIME_CONTEXT_LEGACY_REJOIN_LEASE_KEYS", "session_token_ref"),
    ("_CONTRACT_RUNTIME_QA_AUTHORITY_FIELDS", "candidate_commit_sha"),
    ("_CONTRACT_RUNTIME_QA_AUTHORITY_FIELDS", "qa_scope_binding_ref"),
    ("_CONTRACT_RUNTIME_RECONCILE_AUTHORITY_FIELDS", "merge_event_id"),
    (
        "_RUNTIME_CONTEXT_LEGACY_REJOIN_NOT_APPLICABLE_AUTHORITY_KEYS",
        "replacement_generation",
    ),
    (
        "_RUNTIME_CONTEXT_LEGACY_REJOIN_REPLACEMENT_AUTHORITY_KEYS",
        "current_session_token_ref",
    ),
)


_FRESH_REPAIR_CONTEXTUAL_AUTHORITY_ENVELOPES = (
    (
        "_RUNTIME_CONTEXT_IMPLEMENTATION_WRITER_BINDING_FIELDS",
        "evidence_kind",
    ),
    ("_CONTRACT_RUNTIME_QA_AUTHORITY_FIELDS", "identity_mismatches"),
    (
        "_CONTRACT_RUNTIME_RECONCILE_AUTHORITY_FIELDS",
        "identity_mismatches",
    ),
)


_RETIRED_EXECUTION_AUTHORITY_KEY_CASES = (
    (
        "credential_session_fence_lease",
        (
            "route_token_hash",
            "session_token_hash",
            "fence_token_hash",
            "fence_token_verifier",
            "source_session_token_ref",
            "source_fence_token_hash",
            "host_session_id",
            "observer_session_id",
            "observer_session_ref",
            "worker_session_id",
            "worker_session_ref",
            "qa_session_id",
            "lease_id",
            "session_lease_id",
            "host_startup_id",
            "actual_host_worker_id",
            "worker_transcript_ref",
            "worker_transcript_path",
            "transcript_ref",
            "transcript_path",
            "session_authority_event_ref",
            "session_identity_hash",
            "route_token_ref",
            "session_token_ref",
            "fence_token_ref",
        ),
    ),
    (
        "execution_submit_identity",
        (
            "contract_chain_id",
            "worker_task_id",
            "reconcile_runtime_context_id",
            "reconcile_task_id",
            "root_task_id",
            "stage_task_id",
            "active_task_id",
            "line_instance_id",
            "instance_id",
            "source_line_instance_id",
            "execution_state_revision",
            "source_execution_state_revision",
            "execution_state_hash",
            "definition_hash",
            "instruction_bundle_hash",
            "runtime_guide_hash",
            "contract_hash",
            "contract_revision_id",
            "observer_command_id",
            "correction_id",
            "source_authority_sha256",
            "source_line_sha256",
            "source_implementation_lineage_ref",
            "source_test_results_sha256",
            "contract_execution_id",
            "runtime_context_id",
            "task_id",
            "parent_task_id",
            "root_task_id",
            "stage_task_id",
            "active_task_id",
            "route_task_id",
            "qa_scope_task_id",
            "qa_graph_trace_task_id",
            "coordination_reconcile_task_id",
            "worker_id",
            "worker_slot_id",
            "lane_id",
        ),
    ),
    (
        "route_binding",
        (
            "route_id",
            "route_context_hash",
            "prompt_contract_id",
            "prompt_contract_hash",
            "route_token_ref",
            "visible_injection_manifest_hash",
            "route_identity_hash",
            "identity_binding_hash",
            "canonical_identity_binding",
            "qa_scope_binding_ref",
            "accepted_route_identity",
            "active_route_identity",
            "child_route_identity",
            "current_route_identity",
            "expected_binding",
            "route_identity",
            "route_gate",
            "route_token_gate",
            "canonical_route_identity",
        ),
    ),
    (
        "location",
        (
            "project_root",
            "repo_root",
            "repo_root_path",
            "target_graph_root",
            "target_project_root",
            "workspace_root",
            "worktree_root",
            "worktree_path",
            "worker_worktree_path",
            "assigned_worktree",
            "worktree_id",
            "branch_ref",
            "branch",
            "worktree_branch",
            "git_branch",
            "target_branch",
            "target_ref",
            "ref_name",
            "worker_transcript_ref",
            "worker_transcript_path",
            "transcript_ref",
            "transcript_path",
        ),
    ),
    (
        "commit_graph",
        (
            "base_commit",
            "base_commit_sha",
            "head_commit",
            "target_head_commit",
            "current_target_head",
            "canonical_head_commit",
            "merged_commit_sha",
            "merge_commit_sha",
            "reconciled_commit_sha",
            "reconcile_commit_sha",
            "snapshot_commit_sha",
            "query_root_head_commit",
            "query_root_commit",
            "query_root_commit_sha",
            "query_root_head_sha",
            "active_snapshot_id",
            "active_snapshot_commit_sha",
            "base_snapshot_id",
            "canonical_snapshot_id",
            "snapshot_id",
            "graph_snapshot_id",
            "graph_snapshot_commit",
            "graph_snapshot_commit_sha",
            "qa_snapshot_id",
            "qa_snapshot_commit",
            "reconcile_snapshot_id",
            "reconcile_snapshot_commit",
            "trace_snapshot_id",
            "projection_hash",
            "checkpoint_id",
            "epoch_id",
            "integration_epoch_id",
            "active_epoch_id",
            "trace_id",
            "trace_ids",
            "graph_trace_id",
            "graph_trace_ids",
            "graph_query_trace_id",
            "graph_query_trace_ids",
            "authority_hash",
            "identity_hash",
            "provenance_hash",
            "candidate_diff_hash",
            "state_hash",
            "reconcile_provenance_hash",
            "candidate_state_hash",
            "target_commit",
            "target_commit_sha",
            "target_head_sha",
            "merge_head_commit",
            "merge_head_sha",
            "reconcile_head_commit",
            "reconcile_head_sha",
            "worker_commit",
            "worker_commit_sha",
        ),
    ),
    (
        "dispatch",
        (
            "accepted_dispatch_authority",
            "dispatch_ticket_authority",
            "canonical_dispatch_identity",
            "accepted_dispatch_identity",
            "contract_runtime_dispatch_identity",
            "contract_runtime_dispatch_revision",
            "dispatch_source_ref",
            "contract_runtime_dispatch_source_ref",
            "worker_commit_source_ref",
            "dispatch_acceptance_ref",
            "dispatch_authority",
            "dispatch_authority_hash",
            "dispatch_completed_line_ref",
            "dispatch_context",
            "dispatch_event_ref",
            "dispatch_evidence",
            "dispatch_identity",
            "dispatch_identity_hash",
            "dispatch_line_hash",
            "dispatch_ticket",
            "dispatch_worker_hash",
            "runtime_dispatch_evidence",
            "ticket_authority_source_ref",
        ),
    ),
    (
        "merge_reconcile_qa",
        (
            "merge_queue_id",
            "merge_queue_item_id",
            "queue_item_id",
            "queue_index",
            "batch_id",
            "parent_batch_id",
            "merge_event_id",
            "merge_event_ref",
            "merge_source_ref",
            "reconcile_event_id",
            "reconcile_event_ref",
            "reconcile_source_ref",
            "qa_event_id",
            "qa_event_ref",
            "qa_source_ref",
            "materialize_event_id",
            "materialize_event_ref",
            "materialize_source_ref",
            "review_event_id",
            "review_event_ref",
            "review_source_ref",
            "read_receipt_event_id",
            "read_receipt_event_ref",
            "read_receipt_ref",
            "read_receipt_source_ref",
            "receipt_event_id",
            "receipt_event_ref",
            "receipt_source_ref",
            "rejoin_event_id",
            "rejoin_event_ref",
            "rejoin_source_ref",
            "durable_merge_authority",
            "current_full_reconcile_marker",
            "current_full_reconcile_provenance",
            "reconcile_authority",
            "post_merge_provenance",
            "qa_graph_trace_db_evidence",
            "graph_trace_evidence",
            "graph_review_context",
            "candidate_review_context",
            "selected_atomic_lane_authority",
            "implementation_event_ref",
            "implementation_lineage_ref",
            "implementation_source_ref",
            "source_merge_event_ref",
            "source_qa_event_ref",
            "source_reconcile_event_ref",
            "worker_commit_event_ref",
            "qa_receipt_ref",
            "qa_report_ref",
            "failed_qa_source_ref",
            "provenance_paths",
        ),
    ),
    ("typed_authority_schema", _TYPED_AUTHORITY_PROVEN_GAPS),
    (
        "server_canonical_authority_schema",
        _SERVER_CANONICAL_AUTHORITY_PROVEN_GAPS,
    ),
)


def test_failed_qa_repair_server_authority_schemas_have_no_classifier_drift():
    registry_audit = (
        server._contract_runtime_server_canonical_source_registry_audit()
    )
    assert registry_audit["complete"] is True
    assert registry_audit["unclassified_source_names"] == []
    assert registry_audit["missing_registered_source_names"] == []
    assert registry_audit["overlapping_source_names"] == []
    assert registry_audit["unclassified_field_paths"] == []
    assert registry_audit["overlapping_field_paths"] == []
    assert registry_audit["stale_partition_field_paths"] == []
    assert registry_audit["missing_partition_source_names"] == []
    assert registry_audit["unknown_partition_disposition_paths"] == []
    assert len(registry_audit["inventory_source_names"]) == 63
    assert len(registry_audit["field_inventory_paths"]) >= 849
    assert len(registry_audit["field_inventory_paths"]) == len(
        set(registry_audit["field_inventory_paths"])
    )
    assert set(registry_audit["inventory_source_names"]) == set(
        registry_audit["registered_source_names"]
    )
    assert set(registry_audit["registered_source_names"]) == (
        set(registry_audit["authority_leaf_source_names"])
        | set(registry_audit["recursive_container_source_names"])
        | set(registry_audit["audited_non_authority_source_names"])
    )
    assert set(registry_audit["field_inventory_paths"]) == (
        set(registry_audit["authority_leaf_field_paths"])
        | set(registry_audit["recursive_container_field_paths"])
        | set(registry_audit["audited_non_authority_field_paths"])
    )
    assert not (
        set(registry_audit["authority_leaf_field_paths"])
        & set(registry_audit["recursive_container_field_paths"])
    )
    assert not (
        set(registry_audit["authority_leaf_field_paths"])
        & set(registry_audit["audited_non_authority_field_paths"])
    )
    assert not (
        set(registry_audit["recursive_container_field_paths"])
        & set(registry_audit["audited_non_authority_field_paths"])
    )
    sources = server._contract_runtime_server_canonical_authority_field_sources()
    assert {
        "_CONTRACT_RUNTIME_LINE_WRITE_PROTOCOL_FIELDS",
        "_CONTRACT_RUNTIME_QA_AUTHORITY_FIELDS",
        "_CONTRACT_RUNTIME_QA_PROVENANCE_SECURITY_FIELDS",
        "_CONTRACT_RUNTIME_RECONCILE_AUTHORITY_FIELDS",
        "_MF_BATCH_PARALLEL_CALLER_AUTHORITY_FIELDS",
        "_RUNTIME_CONTEXT_REJOIN_CHECKPOINT_BASELINE_FIELDS",
        "_RUNTIME_CONTEXT_IMPLEMENTATION_WRITER_BINDING_FIELDS",
        "_RUNTIME_CONTEXT_SERVER_IDENTITY_FIELDS",
        "_TIMELINE_BOUNDED_DISPATCH_RUNTIME_REQUIRED_FIELDS",
    }.issubset(sources)
    container_sources = (
        server._contract_runtime_server_canonical_authority_container_sources()
    )
    assert {
        "_CONTRACT_RUNTIME_CONTAINER_KEYS",
        "_CONTRACT_RUNTIME_QA_AUTHORITY_CONTAINERS",
        "_QA_REVIEW_AUTHORITY_CONTAINERS",
    }.issubset(container_sources)
    assert set(sources).intersection(container_sources) == {
        "_QA_EXTERNAL_NO_PASS_COMPARISON_LEDGER_REQUIRED_KEYS"
    }
    mixed_partitions = (
        server._CONTRACT_RUNTIME_SERVER_CANONICAL_MIXED_SOURCE_PARTITIONS[
            "_QA_EXTERNAL_NO_PASS_COMPARISON_LEDGER_REQUIRED_KEYS"
        ]
    )
    assert mixed_partitions["authority_leaf"] == set(
        _FRESH_REPAIR_MIXED_QA_LEDGER_AUTHORITY_FIELDS
    )
    assert set(_FRESH_REPAIR_MIXED_QA_LEDGER_BENIGN_FIELDS).issubset(
        mixed_partitions["recursive_container"]
        | server._CONTRACT_RUNTIME_SERVER_CANONICAL_AUDITED_SAFE_FIELD_SEMANTICS[
            "_QA_EXTERNAL_NO_PASS_COMPARISON_LEDGER_REQUIRED_KEYS"
        ]
    )
    expected_source_qualified_safe_paths = {
        f"{source_name}.{field_name}"
        for source_name, field_name, _value in (
            _FRESH_REPAIR_SOURCE_QUALIFIED_SAFE_FIELDS
        )
    }
    assert expected_source_qualified_safe_paths.issubset(
        registry_audit["audited_non_authority_field_paths"]
    )
    assert expected_source_qualified_safe_paths.isdisjoint(
        registry_audit["authority_leaf_field_paths"]
    )
    assert server._CONTRACT_RUNTIME_CANONICAL_AUTHORITY_CONTAINER_FIELDS == {
        field_name
        for source_fields in container_sources.values()
        for field_name in source_fields
    }
    is_nontransferable = (
        parallel_branch_runtime
        .parallel_branch_authority_field_is_nontransferable
    )
    derived_server_fields = {
        field_name
        for source_fields in sources.values()
        for field_name in source_fields
        if is_nontransferable(field_name)
    }
    canonical_fields = (
        server._CONTRACT_RUNTIME_CANONICAL_NONTRANSFERABLE_AUTHORITY_FIELDS
    )
    typed_nontransferable_fields = (
        parallel_branch_runtime
        .PARALLEL_BRANCH_TYPED_NONTRANSFERABLE_AUTHORITY_FIELDS
    )
    assert canonical_fields == (
        derived_server_fields | typed_nontransferable_fields
    )
    assert not derived_server_fields - canonical_fields
    assert not (
        set(_SERVER_CANONICAL_AUTHORITY_PROVEN_GAPS) - canonical_fields
    )
    assert canonical_fields == set(
        server._CONTRACT_RUNTIME_EXECUTION_AUTHORITY_KEY_ALIAS_MATRIX[
            "canonical_schema"
        ]
    )


def test_failed_qa_repair_registry_audit_flags_unclassified_schema(
    monkeypatch,
):
    future_source = "_RUNTIME_CONTEXT_FUTURE_WRITER_BINDING_FIELDS"
    monkeypatch.setattr(
        server,
        future_source,
        ("future_writer_generation_binding",),
        raising=False,
    )

    audit = server._contract_runtime_server_canonical_source_registry_audit()

    assert audit["complete"] is False
    assert audit["unclassified_source_names"] == [future_source]
    assert future_source not in audit["registered_source_names"]


def test_failed_qa_repair_mixed_source_audit_flags_unclassified_future_field(
    monkeypatch,
):
    source_name = "_QA_EXTERNAL_NO_PASS_COMPARISON_LEDGER_REQUIRED_KEYS"
    future_field = "future_qa_diagnostic_generation_binding"
    original_fields = getattr(server, source_name)

    try:
        monkeypatch.setattr(
            server,
            source_name,
            {*original_fields, future_field},
        )
        audit = (
            server._contract_runtime_server_canonical_source_registry_audit()
        )
        assert audit["complete"] is False
        assert audit["unclassified_field_paths"] == [
            f"{source_name}.{future_field}"
        ]

        with pytest.raises(server.GovernanceError) as refresh_error:
            server._contract_runtime_refresh_canonical_authority_field_inventory()
        assert refresh_error.value.code == (
            "contract_runtime_authority_registry_incomplete"
        )
        assert refresh_error.value.details == {
            "schema_version": "contract_runtime.authority_registry_failure.v1",
            "status": "blocked",
            "actionable": False,
            "writes_performed": False,
            "diagnostic_paths": [f"{source_name}.{future_field}"],
            "diagnostic_path_count": 1,
            "safe_next_step": (
                "classify every canonical schema field and restart the "
                "governance runtime"
            ),
            "authority_values_exposed": False,
        }

        record = _rev10_failed_qa_fresh_repair_record(
            [
                {
                    "id": "AC-REV10-REGISTRY-RUNTIME-GATE",
                    "text": "Registry drift blocks successor projection.",
                    "required_scope": ["agent/governance/server.py"],
                }
            ]
        )
        with pytest.raises(server.GovernanceError) as runtime_error:
            server._runtime_current_state_from_record(record)
        assert runtime_error.value.code == refresh_error.value.code
        assert runtime_error.value.details == refresh_error.value.details
    finally:
        monkeypatch.setattr(server, source_name, original_fields)
        server._contract_runtime_refresh_canonical_authority_field_inventory()


@pytest.mark.parametrize(
    ("source_name", "field_name", "field_value"),
    _FRESH_REPAIR_SOURCE_QUALIFIED_SAFE_FIELDS,
)
def test_mf_parallel_rev10_failed_qa_preserves_source_qualified_semantics(
    source_name,
    field_name,
    field_value,
):
    criterion = {
        "id": f"AC-REV10-SOURCE-SAFE-{field_name.upper()}",
        "text": "Source-qualified semantic metadata remains copy-safe.",
        "required_scope": ["agent/governance/server.py"],
        "historical_context": {field_name: deepcopy(field_value)},
    }
    record = _rev10_failed_qa_fresh_repair_record(
        [criterion],
        backlog_id=f"AC-REV10-SOURCE-SAFE-{field_name.upper()}",
        contract_execution_id=f"cex-rev10-source-safe-{field_name}",
    )

    assert server._contract_runtime_server_canonical_field_dispositions(
        source_name,
        field_name,
    ) == ("audited_non_authority",)
    assert (
        server._contract_runtime_key_is_execution_authority_or_credential(
            field_name
        )
        is False
    )
    action = server._runtime_current_state_from_record(record)[
        "next_legal_action"
    ]
    assert action["actionable"] is True
    assert action["action_input_ready"] is True
    assert action["unsafe_action_input_paths"] == []
    assert action["copy_safe_body"]["acceptance_criteria"] == [criterion]

    compact = server._onboard_route_guide_compact_service_response(
        project_id="aming-claw",
        backlog_id=record["backlog_id"],
        role="observer",
        work_type="parallel_worker",
        record=record,
        next_action=action,
        current_projection={
            "current_contract_execution_id": record[
                "contract_execution_id"
            ],
            "execution_state_revision": 1,
            "projection_hash": "sha256:" + "6" * 64,
            "next_legal_action": action,
        },
        runtime_resume={"next_legal_action": action},
        target_files=["agent/governance/server.py"],
        projection_degraded=False,
    )
    assert compact["ok"] is True
    assert compact["actionable"] is True
    assert compact["next_legal_action"]["action_input_ready"] is True
    assert compact["canonical_executable_action"]["copy_safe_body"][
        "acceptance_criteria"
    ] == [criterion]


@pytest.mark.parametrize(
    ("source_name", "field_name"),
    _FRESH_REPAIR_MIXED_SOURCE_AUTHORITY_NEIGHBORS,
)
def test_mf_parallel_rev10_failed_qa_rejects_mixed_source_authority_neighbors(
    source_name,
    field_name,
):
    sentinel = f"retired-{field_name}-sentinel"
    record = _rev10_failed_qa_fresh_repair_record(
        [
            {
                "id": "AC-REV10-MIXED-SOURCE-AUTHORITY",
                "text": "Authority beside safe semantics remains blocked.",
                "required_scope": ["agent/governance/server.py"],
                "historical_context": {field_name: sentinel},
            }
        ]
    )

    assert server._contract_runtime_server_canonical_field_dispositions(
        source_name,
        field_name,
    ) == ("authority_leaf",)
    action = server._runtime_current_state_from_record(record)[
        "next_legal_action"
    ]
    assert action["actionable"] is False
    assert action["action_input"] == {}
    assert action["copy_safe_body"] == {}
    assert action["unsafe_action_input_paths"] == [
        f"acceptance_criteria[0].historical_context.{field_name}"
    ]
    assert sentinel not in json.dumps(action, sort_keys=True)


@pytest.mark.parametrize(
    ("source_name", "target_field"),
    _FRESH_REPAIR_CONTEXTUAL_AUTHORITY_ENVELOPES,
)
def test_mf_parallel_rev10_failed_qa_rejects_contextual_authority_envelopes(
    source_name,
    target_field,
):
    source_fields = getattr(server, source_name)
    envelope = {
        field_name: f"retired-{source_name}-{field_name}"
        for field_name in source_fields
    }
    target_sentinel = f"retired-contextual-{target_field}-sentinel"
    envelope[target_field] = target_sentinel
    criterion = {
        "id": f"AC-REV10-CONTEXTUAL-{target_field.upper()}",
        "text": "A canonical authority envelope is not semantic prose.",
        "required_scope": ["agent/governance/server.py"],
        "historical_context": {"canonical_envelope": envelope},
    }
    record = _rev10_failed_qa_fresh_repair_record(
        [criterion],
        backlog_id=f"AC-REV10-CONTEXTUAL-{target_field.upper()}",
        contract_execution_id=f"cex-rev10-contextual-{target_field}",
    )

    assert source_name in (
        server._contract_runtime_fresh_repair_canonical_envelope_sources(
            envelope
        )
    )
    assert server._contract_runtime_key_is_execution_authority_or_credential(
        target_field,
        canonical_source_names=(source_name,),
    ) is True
    action = server._runtime_current_state_from_record(record)[
        "next_legal_action"
    ]
    target_path = (
        "acceptance_criteria[0].historical_context.canonical_envelope."
        f"{target_field}"
    )
    assert action["actionable"] is False
    assert action["action_input_ready"] is False
    assert action["action_input"] == {}
    assert action["copy_safe_body"] == {}
    assert target_path in action["unsafe_action_input_paths"]
    assert target_sentinel not in json.dumps(action, sort_keys=True)

    compact = server._onboard_route_guide_compact_service_response(
        project_id="aming-claw",
        backlog_id=record["backlog_id"],
        role="observer",
        work_type="parallel_worker",
        record=record,
        next_action=action,
        current_projection={
            "current_contract_execution_id": record[
                "contract_execution_id"
            ],
            "execution_state_revision": 1,
            "projection_hash": "sha256:" + "7" * 64,
            "next_legal_action": action,
        },
        runtime_resume={"next_legal_action": action},
        target_files=["agent/governance/server.py"],
        projection_degraded=False,
    )
    assert compact["ok"] is True
    assert compact["actionable"] is False
    assert compact["action_input"] == {}
    assert compact["copy_safe_body"] == {}
    assert target_path in compact["next_legal_action"][
        "unsafe_action_input_paths"
    ]
    assert target_sentinel not in json.dumps(compact, sort_keys=True)


def test_mf_parallel_rev10_failed_qa_unknown_contextual_field_hard_fails():
    source_name = "_RUNTIME_CONTEXT_IMPLEMENTATION_WRITER_BINDING_FIELDS"
    unknown_field = "future_writer_diagnostic_generation_binding"
    sentinel = "retired-unknown-writer-value-must-not-echo"
    envelope = {
        field_name: f"retired-writer-{field_name}"
        for field_name in getattr(server, source_name)
    }
    envelope[unknown_field] = sentinel
    record = _rev10_failed_qa_fresh_repair_record(
        [
            {
                "id": "AC-REV10-UNKNOWN-CONTEXTUAL-FIELD",
                "text": "Unknown canonical fields fail closed.",
                "required_scope": ["agent/governance/server.py"],
                "historical_context": {"canonical_envelope": envelope},
            }
        ]
    )

    with pytest.raises(server.GovernanceError) as error:
        server._runtime_current_state_from_record(record)

    assert error.value.code == "contract_runtime_authority_registry_incomplete"
    assert error.value.status == 503
    assert error.value.details["actionable"] is False
    assert error.value.details["writes_performed"] is False
    assert error.value.details["diagnostic_paths"] == [
        f"{source_name}.{unknown_field}"
    ]
    assert error.value.details["authority_values_exposed"] is False
    assert sentinel not in json.dumps(error.value.to_dict(), sort_keys=True)


def test_mf_parallel_rev10_failed_qa_lone_contextual_labels_remain_semantic():
    criterion = {
        "id": "AC-REV10-LONE-CONTEXTUAL-LABELS",
        "text": "Lone labels do not assert canonical source identity.",
        "required_scope": ["agent/governance/server.py"],
        "extra_semantics": {
            "evidence_kind": "diagnostic",
            "identity_mismatches": ["plain semantic mismatch"],
        },
    }
    record = _rev10_failed_qa_fresh_repair_record([criterion])

    action = server._runtime_current_state_from_record(record)[
        "next_legal_action"
    ]

    assert action["actionable"] is True
    assert action["action_input_ready"] is True
    assert action["unsafe_action_input_paths"] == []
    assert action["copy_safe_body"]["acceptance_criteria"] == [criterion]


def test_failed_qa_repair_dispatch_runtime_schema_refreshes_future_fields(
    monkeypatch,
):
    source_name = "_TIMELINE_BOUNDED_DISPATCH_RUNTIME_REQUIRED_FIELDS"
    future_field = "future_dispatch_generation_binding"
    original_fields = getattr(server, source_name)
    assert source_name in (
        server._contract_runtime_server_canonical_authority_field_sources()
    )
    assert (
        server._contract_runtime_key_is_execution_authority_or_credential(
            future_field
        )
        is False
    )

    try:
        monkeypatch.setattr(
            server,
            source_name,
            (*original_fields, future_field),
        )
        refreshed = (
            server._contract_runtime_refresh_canonical_authority_field_inventory()
        )
        assert future_field in refreshed
        assert future_field in (
            server._contract_runtime_server_canonical_authority_field_sources()[
                source_name
            ]
        )
        assert (
            server._contract_runtime_key_is_execution_authority_or_credential(
                future_field
            )
            is True
        )
    finally:
        monkeypatch.setattr(server, source_name, original_fields)
        server._contract_runtime_refresh_canonical_authority_field_inventory()


@pytest.mark.parametrize(
    ("source_name", "future_field"),
    (
        (
            "_RUNTIME_CONTEXT_IMPLEMENTATION_WRITER_BINDING_FIELDS",
            "future_writer_generation_binding",
        ),
        (
            "_CONTRACT_RUNTIME_LINE_WRITE_PROTOCOL_FIELDS",
            "future_line_write_generation_binding",
        ),
    ),
)
def test_failed_qa_repair_registered_submit_schema_refreshes_future_fields(
    monkeypatch,
    source_name,
    future_field,
):
    original_fields = getattr(server, source_name)
    assert source_name in (
        server._contract_runtime_server_canonical_authority_field_sources()
    )
    assert (
        server._contract_runtime_key_is_execution_authority_or_credential(
            future_field
        )
        is False
    )

    try:
        monkeypatch.setattr(
            server,
            source_name,
            (*original_fields, future_field),
        )
        refreshed = (
            server._contract_runtime_refresh_canonical_authority_field_inventory()
        )
        assert future_field in refreshed
        assert future_field in (
            server._contract_runtime_server_canonical_authority_field_sources()[
                source_name
            ]
        )
        assert (
            server._contract_runtime_key_is_execution_authority_or_credential(
                future_field
            )
            is True
        )
    finally:
        monkeypatch.setattr(server, source_name, original_fields)
        server._contract_runtime_refresh_canonical_authority_field_inventory()


def test_failed_qa_repair_typed_authority_schema_has_no_classifier_drift():
    assert {
        authority_type.__name__
        for authority_type in (
            parallel_branch_runtime.PARALLEL_BRANCH_TYPED_AUTHORITY_TYPES
        )
    } == {
        "AuditedPostmergeRecoveryAuthority",
        "DependencyRevalidationQaCandidateAuthority",
        "FailedQaRunningRevisionRejoinAuthority",
        "PostQaMergeConflictRejoinAuthority",
        "PostQaRejoinRetargetAuthority",
        "SafeRefPrestartupReissueAuthority",
        "StandaloneHistoricalCheckpointAuthority",
    }
    typed_fields = (
        parallel_branch_runtime.PARALLEL_BRANCH_TYPED_AUTHORITY_FIELDS
    )
    safe_fields = (
        parallel_branch_runtime.PARALLEL_BRANCH_TYPED_AUTHORITY_SAFE_FIELDS
    )
    nontransferable_fields = (
        parallel_branch_runtime
        .PARALLEL_BRANCH_TYPED_NONTRANSFERABLE_AUTHORITY_FIELDS
    )
    assert typed_fields == safe_fields | nontransferable_fields
    assert safe_fields.isdisjoint(nontransferable_fields)
    assert not (
        nontransferable_fields
        - server._CONTRACT_RUNTIME_CANONICAL_NONTRANSFERABLE_AUTHORITY_FIELDS
    )
    assert all(
        server._contract_runtime_key_is_execution_authority_or_credential(
            field_name
        )
        for field_name in nontransferable_fields
    )
    assert not any(
        server._contract_runtime_key_is_execution_authority_or_credential(
            field_name
        )
        for field_name in safe_fields
    )


@pytest.mark.parametrize("authority_key", _TYPED_AUTHORITY_PROVEN_GAPS)
def test_failed_qa_repair_classifier_rejects_typed_authority_gaps(
    authority_key,
):
    assert authority_key in (
        parallel_branch_runtime
        .PARALLEL_BRANCH_TYPED_NONTRANSFERABLE_AUTHORITY_FIELDS
    )
    assert (
        server._contract_runtime_key_is_execution_authority_or_credential(
            authority_key
        )
        is True
    )


@pytest.mark.parametrize(
    "authority_key", _SERVER_CANONICAL_AUTHORITY_PROVEN_GAPS
)
def test_failed_qa_repair_classifier_rejects_server_authority_gaps(
    authority_key,
):
    assert authority_key in (
        server._CONTRACT_RUNTIME_CANONICAL_NONTRANSFERABLE_AUTHORITY_FIELDS
    )
    assert (
        server._contract_runtime_key_is_execution_authority_or_credential(
            authority_key
        )
        is True
    )


@pytest.mark.parametrize(
    "authority_key",
    (
        "line_instance_id",
        "source_line_instance_id",
        "dispatch_source_ref",
        "contract_runtime_dispatch_source_ref",
        "source_implementation_lineage_ref",
    ),
)
def test_failed_qa_repair_classifier_rejects_proven_authority_gaps(
    authority_key,
):
    assert (
        server._contract_runtime_key_is_execution_authority_or_credential(
            authority_key
        )
        is True
    )


@pytest.mark.parametrize(
    ("authority_group", "authority_keys"),
    _RETIRED_EXECUTION_AUTHORITY_KEY_CASES,
)
def test_mf_parallel_rev10_failed_qa_rejects_nested_acceptance_authority(
    authority_group,
    authority_keys,
):
    leaked_authority = {
        key: f"must-not-leak-{authority_group}-{index}"
        for index, key in enumerate(authority_keys, start=1)
    }
    record = _rev10_failed_qa_fresh_repair_record(
        [
            {
                "id": "AC-REV10-UNSAFE-NESTED-AUTHORITY",
                "text": "Historical criterion contains unsafe metadata.",
                "required_scope": [
                    "agent/governance/server.py",
                    "agent/tests/test_contract_runtime.py",
                ],
                "historical_context": {
                    "nested": dict(leaked_authority),
                },
            },
        ]
    )

    state = server._runtime_current_state_from_record(record)
    action = state["next_legal_action"]

    assert action["id"] == "file_fresh_bounded_row"
    assert action["actionable"] is False
    assert action["action_input_ready"] is False
    assert action["action_input"] == {}
    assert action["copy_safe_body"] == {}
    assert action["canonical_executable_action"] == {}
    assert "acceptance_criteria" in action["action_input_missing_fields"]
    assert action["unsafe_action_input_paths"] == sorted(
        f"acceptance_criteria[0].historical_context.nested.{key}"
        for key in leaked_authority
    )
    assert action["forbidden_authority_reuse"] == sorted(
        server._CONTRACT_RUNTIME_EXECUTION_AUTHORITY_KEY_ALIAS_MATRIX
    )
    rendered_action = json.dumps(action, sort_keys=True)
    for value in leaked_authority.values():
        assert value not in rendered_action
    typed_failed_qa_ref = action["failed_qa_source_ref"]
    assert typed_failed_qa_ref.startswith(
        "contract_runtime:cex-rev10-unsafe-ac-source:completed_lines:"
    )
    assert typed_failed_qa_ref not in leaked_authority.values()

    compact = server._onboard_route_guide_compact_service_response(
        project_id="aming-claw",
        backlog_id=record["backlog_id"],
        role="observer",
        work_type="parallel_worker",
        record=record,
        next_action=action,
        current_projection={
            "current_contract_execution_id": record[
                "contract_execution_id"
            ],
            "execution_state_revision": 1,
            "projection_hash": "sha256:" + "4" * 64,
            "next_legal_action": action,
        },
        runtime_resume={"next_legal_action": action},
        target_files=["agent/governance/server.py"],
        projection_degraded=False,
    )
    assert compact["ok"] is True
    assert compact["actionable"] is False
    compact_action = compact["next_legal_action"]
    assert compact_action["action_input_ready"] is False
    assert compact_action["action_input_missing_fields"] == [
        "acceptance_criteria"
    ]
    assert compact["action_input"] == {}
    assert compact["copy_safe_body"] == {}
    assert compact["canonical_executable_action"] == {}
    assert compact_action["unsafe_action_input_paths"] == (
        server._onboard_guide_bounded_unsafe_action_input_paths(
            action["unsafe_action_input_paths"]
        )
    )
    rendered_compact = json.dumps(compact, sort_keys=True)
    for value in leaked_authority.values():
        assert value not in rendered_compact


def test_mf_parallel_rev10_failed_qa_allows_safe_semantic_key_mutations():
    safe_semantic_criteria = [
        {
            "id": "AC-REV10-SAFE-SEMANTIC-MUTATIONS",
            "text": (
                "Semantic prose may name cex-historical-example, "
                "runtime_context_id, line_instance_id, and "
                "dispatch_source_ref without carrying structured authority."
            ),
            "schema_version": "acceptance_criterion.v1",
            "status": "required",
            "strategy": "fresh bounded successor",
            "actor_role": "observer",
            "worker_role": "mf_sub",
            "source": "historical audit label",
            "source_details": {
                "source_ref": "audit-label-only-not-execution-authority",
                "example": (
                    "line_instance_id and dispatch_source_ref may appear in "
                    "prose values without becoming executable authority"
                ),
            },
            "required_scope": [
                "agent/governance/server.py",
                "agent/tests/test_contract_runtime.py",
            ],
            "kind": "behavioral",
            "files": ["agent/governance/server.py"],
            "node_ids": ["methodology-node"],
            "target_files": ["agent/governance/server.py"],
            "test_files": ["agent/tests/test_contract_runtime.py"],
            "owned_files": ["agent/governance/server.py"],
            "inputs": ["failed QA audit source"],
            "outputs": ["fresh repair row"],
            "behavior_contract": {
                "given": "a terminal failed generation",
                "when": "the observer files a new row",
                "then": "old authority is not copied",
            },
            "methodology": {
                "project_root_behavior": "derive a fresh registered root",
                "worker_worktree_path_requirement": "allocate a fresh path",
                "assigned_worktree_expectation": "must be isolated",
                "worktree_branching_strategy": "use a fresh branch",
                "git_branch_policy": "never reuse retired authority",
                "merge_queue_item_idempotency": "preserve row semantics",
                "queue_item_identifier_format": "server generated",
                "observer_command_description": "file a fresh repair row",
                "session_behavior": "fresh authorization is required",
                "tokenization_strategy": "index the source text",
            },
        }
    ]
    record = _rev10_failed_qa_fresh_repair_record(
        safe_semantic_criteria,
        backlog_id="AC-REV10-FAILED-QA-SAFE-SEMANTIC-AC",
        contract_execution_id="cex-rev10-safe-semantic-ac-source",
    )

    action = server._runtime_current_state_from_record(record)[
        "next_legal_action"
    ]

    assert action["actionable"] is True
    assert action["action_input_ready"] is True
    assert action["action_input_missing_fields"] == []
    assert action["unsafe_action_input_paths"] == []
    assert action["copy_safe_body"]["acceptance_criteria"] == (
        safe_semantic_criteria
    )
    assert action["failed_qa_source_ref"].startswith(
        "contract_runtime:cex-rev10-safe-semantic-ac-source:completed_lines:"
    )
    assert "contract_execution_id" not in action["copy_safe_body"]


@pytest.mark.parametrize(
    "semantic_key",
    (
        *_FRESH_REPAIR_BENIGN_SEMANTIC_KEYS,
        *_FRESH_REPAIR_EXTRA_SEMANTIC_KEYS,
    ),
)
def test_failed_qa_repair_classifier_allows_benign_semantic_keys(
    semantic_key,
):
    assert (
        server._contract_runtime_key_is_execution_authority_or_credential(
            semantic_key
        )
        is False
    )
    criterion = {
        "id": f"AC-REV10-BENIGN-{semantic_key.upper()}",
        "text": "One plain semantic key remains copy-safe.",
        "required_scope": ["agent/governance/server.py"],
        "extra_semantics": {
            semantic_key: f"plain semantic {semantic_key}",
        },
    }
    record = _rev10_failed_qa_fresh_repair_record(
        [criterion],
        backlog_id=f"AC-REV10-BENIGN-{semantic_key.upper()}",
        contract_execution_id=f"cex-rev10-benign-{semantic_key}",
    )

    action = server._runtime_current_state_from_record(record)[
        "next_legal_action"
    ]

    assert action["actionable"] is True
    assert action["action_input_ready"] is True
    assert action["unsafe_action_input_paths"] == []
    assert action["copy_safe_body"]["acceptance_criteria"] == [criterion]


def test_mf_parallel_rev10_failed_qa_preserves_benign_semantic_keys():
    benign_context = {
        key: f"benign semantic value for {key}"
        for key in (
            *_FRESH_REPAIR_BENIGN_SEMANTIC_KEYS,
            *_FRESH_REPAIR_EXTRA_SEMANTIC_KEYS,
        )
    }
    acceptance_criteria = [
        {
            "id": "AC-REV10-BENIGN-SEMANTIC-KEYS",
            "text": "Ordinary semantic metadata remains copy-safe.",
            "required_scope": ["agent/governance/server.py"],
            "historical_context": benign_context,
        }
    ]
    record = _rev10_failed_qa_fresh_repair_record(
        acceptance_criteria,
        backlog_id="AC-REV10-FAILED-QA-BENIGN-SEMANTIC-KEYS",
        contract_execution_id="cex-rev10-benign-semantic-keys",
    )

    action = server._runtime_current_state_from_record(record)[
        "next_legal_action"
    ]

    assert action["actionable"] is True
    assert action["action_input_ready"] is True
    assert action["action_input_missing_fields"] == []
    assert action["unsafe_action_input_paths"] == []
    assert action["copy_safe_body"]["acceptance_criteria"] == (
        acceptance_criteria
    )


def test_mf_parallel_rev10_failed_qa_preserves_benign_container_dicts():
    benign_containers = {
        container_key: {
            "given": f"plain semantic given in {container_key}",
            "when": f"plain semantic when in {container_key}",
            "then": f"plain semantic then in {container_key}",
        }
        for container_key in (
            "payload",
            "verification",
            "artifact_refs",
            "current_state",
        )
    }
    acceptance_criteria = [
        {
            "id": "AC-REV10-BENIGN-CONTAINER-DICTS",
            "text": "Benign container mappings remain copy-safe.",
            "required_scope": ["agent/governance/server.py"],
            "historical_context": benign_containers,
        }
    ]
    record = _rev10_failed_qa_fresh_repair_record(
        acceptance_criteria,
        backlog_id="AC-REV10-FAILED-QA-BENIGN-CONTAINER-DICTS",
        contract_execution_id="cex-rev10-benign-container-dicts",
    )

    action = server._runtime_current_state_from_record(record)[
        "next_legal_action"
    ]

    assert action["actionable"] is True
    assert action["action_input_ready"] is True
    assert action["unsafe_action_input_paths"] == []
    assert action["copy_safe_body"]["acceptance_criteria"] == (
        acceptance_criteria
    )


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    (
        ("candidate_specific_issues", ["plain scoped diagnostic"]),
        ("candidate_new_failures", ["plain candidate diagnostic"]),
        (
            "base_reproduction",
            {
                "reproduced": True,
                "total": 1,
                "failure_identities": ["plain baseline diagnostic"],
            },
        ),
        (
            "candidate_suite_counts",
            {"passed": 3, "failed": 1, "baseline_known_non_green": 1},
        ),
        ("refs", ["plain audit label"]),
        ("failure_summary", "plain failure summary"),
    ),
)
def test_mf_parallel_rev10_failed_qa_preserves_mixed_qa_diagnostics(
    field_name,
    field_value,
):
    criterion = {
        "id": f"AC-REV10-MIXED-QA-{field_name.upper()}",
        "text": "Diagnostic QA metadata remains copy-safe.",
        "required_scope": ["agent/governance/server.py"],
        "extra_semantics": {field_name: field_value},
    }
    record = _rev10_failed_qa_fresh_repair_record(
        [criterion],
        backlog_id=f"AC-REV10-MIXED-QA-{field_name.upper()}",
        contract_execution_id=f"cex-rev10-mixed-qa-{field_name}",
    )

    action = server._runtime_current_state_from_record(record)[
        "next_legal_action"
    ]

    assert (
        server._contract_runtime_key_is_execution_authority_or_credential(
            field_name
        )
        is False
    )
    assert action["actionable"] is True
    assert action["action_input_ready"] is True
    assert action["unsafe_action_input_paths"] == []
    assert action["copy_safe_body"]["acceptance_criteria"] == [criterion]


@pytest.mark.parametrize(
    "field_name",
    _FRESH_REPAIR_MIXED_QA_LEDGER_AUTHORITY_FIELDS,
)
def test_mf_parallel_rev10_failed_qa_rejects_mixed_qa_commit_authority(
    field_name,
):
    sentinel = f"retired-{field_name}-sentinel"
    record = _rev10_failed_qa_fresh_repair_record(
        [
            {
                "id": "AC-REV10-MIXED-QA-COMMIT-AUTHORITY",
                "text": "Historical comparison commits are not reusable.",
                "required_scope": ["agent/governance/server.py"],
                "extra_semantics": {field_name: sentinel},
            }
        ],
        backlog_id="AC-REV10-MIXED-QA-COMMIT-AUTHORITY",
        contract_execution_id="cex-rev10-mixed-qa-commit-authority",
    )

    action = server._runtime_current_state_from_record(record)[
        "next_legal_action"
    ]

    assert (
        server._contract_runtime_server_canonical_field_dispositions(
            "_QA_EXTERNAL_NO_PASS_COMPARISON_LEDGER_REQUIRED_KEYS",
            field_name,
        )
        == ("authority_leaf",)
    )
    assert action["actionable"] is False
    assert action["action_input_ready"] is False
    assert action["action_input"] == {}
    assert action["copy_safe_body"] == {}
    assert action["unsafe_action_input_paths"] == [
        f"acceptance_criteria[0].extra_semantics.{field_name}"
    ]
    assert sentinel not in json.dumps(action, sort_keys=True)


@pytest.mark.parametrize(
    "container_key",
    ("payload", "verification", "artifact_refs", "current_state"),
)
def test_mf_parallel_rev10_failed_qa_rejects_authority_inside_safe_container(
    container_key,
):
    sentinel = f"retired-task-inside-{container_key}"
    record = _rev10_failed_qa_fresh_repair_record(
        [
            {
                "id": "AC-REV10-NESTED-CONTAINER-AUTHORITY",
                "text": "Container labels do not hide retired authority.",
                "required_scope": ["agent/governance/server.py"],
                "historical_context": {
                    container_key: {"task_id": sentinel},
                },
            }
        ],
        backlog_id="AC-REV10-FAILED-QA-NESTED-CONTAINER-AUTHORITY",
        contract_execution_id="cex-rev10-nested-container-authority",
    )

    action = server._runtime_current_state_from_record(record)[
        "next_legal_action"
    ]

    assert action["actionable"] is False
    assert action["action_input_ready"] is False
    assert action["action_input"] == {}
    assert action["copy_safe_body"] == {}
    assert action["canonical_executable_action"] == {}
    assert action["action_input_missing_fields"] == ["acceptance_criteria"]
    assert action["unsafe_action_input_paths"] == [
        (
            "acceptance_criteria[0].historical_context."
            f"{container_key}.task_id"
        )
    ]
    assert sentinel not in json.dumps(action, sort_keys=True)


def test_mf_parallel_rev10_failed_qa_compact_caps_unsafe_path_diagnostics():
    record = _rev10_failed_qa_fresh_repair_record(
        [
            {
                "id": "AC-REV10-UNSAFE-DIAGNOSTIC-CAP",
                "text": "Bound unsafe key-path diagnostics.",
                "required_scope": ["agent/governance/server.py"],
                "historical_context": {"line_instance_id": "retired-lane"},
            }
        ],
        backlog_id="AC-REV10-FAILED-QA-DIAGNOSTIC-CAP",
        contract_execution_id="cex-rev10-diagnostic-cap",
    )
    action = server._runtime_current_state_from_record(record)[
        "next_legal_action"
    ]
    unsafe_paths = [
        (
            "acceptance_criteria[0].historical_context."
            + (f"safe_segment_{index}_" * 40)
            + ".trace_id"
        )
        for index in range(40)
    ]
    assert all(
        len(path)
        > server._ONBOARD_GUIDE_UNSAFE_ACTION_INPUT_PATH_MAX_CHARS
        for path in unsafe_paths
    )
    action["unsafe_action_input_paths"] = unsafe_paths

    compact = server._onboard_route_guide_compact_service_response(
        project_id="aming-claw",
        backlog_id=record["backlog_id"],
        role="observer",
        work_type="parallel_worker",
        record=record,
        next_action=action,
        current_projection={
            "current_contract_execution_id": record[
                "contract_execution_id"
            ],
            "execution_state_revision": 1,
            "projection_hash": "sha256:" + "5" * 64,
            "next_legal_action": action,
        },
        runtime_resume={"next_legal_action": action},
        target_files=["agent/governance/server.py"],
        projection_degraded=False,
    )

    assert compact["ok"] is True
    projected_paths = compact["next_legal_action"][
        "unsafe_action_input_paths"
    ]
    assert projected_paths == (
        server._onboard_guide_bounded_unsafe_action_input_paths(
            unsafe_paths
        )
    )
    assert len(projected_paths) <= 32
    assert all(len(path) == 512 for path in projected_paths)
    assert sum(map(len, projected_paths)) <= 4 * 1024


def test_mf_parallel_rev9_postmerge_uses_verified_single_worker_fix_generation(
    monkeypatch,
):
    contract_execution_id = "cex-rev9-single-worker-fix"
    rework_runtime_context_id = "mfrctx-rev9-single-worker-fix"
    rework_task_id = "rev9-single-worker-fix"
    failed_qa_index = 4
    initial_workers = [
        {
            "runtime_context_id": "mfrctx-initial-a",
            "task_id": "initial-worker-a",
            "parent_task_id": contract_execution_id,
            "merge_queue_id": "mq-initial-a",
        },
        {
            "runtime_context_id": "mfrctx-initial-b",
            "task_id": "initial-worker-b",
            "parent_task_id": contract_execution_id,
            "merge_queue_id": "mq-initial-b",
        },
    ]
    rework_revision = {
        "append_only_history_preserved": True,
        "timeline_projection_authoritative": False,
        "failed_qa_completed_line_index": failed_qa_index,
        "runtime_context_id": rework_runtime_context_id,
        "task_id": rework_task_id,
    }
    rework_authority = {
        "source": "parallel_branch_allocate_failed_qa_rework",
        "server_derived": True,
        "contract_execution_id": contract_execution_id,
        "failed_qa_completed_line_index": failed_qa_index,
        "runtime_context_id": rework_runtime_context_id,
        "task_id": rework_task_id,
    }
    record = {
        "contract_id": "mf_parallel.v2",
        "version": "v2",
        "revision": "rev9",
        "project_id": "aming-claw",
        "backlog_id": "AC-REV9-SINGLE-WORKER-FIX",
        "contract_execution_id": contract_execution_id,
        "completed_lines": [
            {
                "stage_id": "dispatch",
                "line_id": "observer_dispatch_bounded_workers",
                "evidence_kind": "dispatch_bounded_worker",
                "actor_role": "observer",
                "payload": {
                    "bounded_workers": initial_workers,
                },
            },
            *[
                {
                    "stage_id": "observer_lane_merge",
                    "line_id": "observer_merge",
                    "evidence_kind": "merge",
                    "actor_role": "observer",
                    "runtime_context_id": worker["runtime_context_id"],
                    "line_instance_id": (
                        f"runtime_context:{worker['runtime_context_id']}"
                    ),
                    "payload": {
                        "line_instance_id": (
                            f"runtime_context:{worker['runtime_context_id']}"
                        ),
                        "durable_merge_authority": {
                            "schema_version": (
                                server._CONTRACT_RUNTIME_DURABLE_MERGE_SCHEMA_VERSION
                            ),
                            "server_derived": True,
                            "db_verified": True,
                            "pre_qa_merge_authorized": True,
                            "final_qa_required_after_reconcile": True,
                            "project_id": "aming-claw",
                            "backlog_id": "AC-REV9-SINGLE-WORKER-FIX",
                            "contract_execution_id": contract_execution_id,
                            **worker,
                            "queue_item_id": f"mqi-initial-{index}",
                            "contract_runtime_dispatch_source_ref": (
                                f"contract_runtime:{contract_execution_id}:"
                                "completed_lines:0"
                            ),
                            "merge_commit": str(index) * 40,
                            "merge_event_ref": f"timeline:{100 + index}",
                            "merge_event_id": 100 + index,
                            "merge_event_created_at": (
                                f"2026-08-02T11:00:0{index}Z"
                            ),
                        },
                    },
                }
                for index, worker in enumerate(initial_workers, start=1)
            ],
            {
                "stage_id": "reconcile",
                "line_id": "observer_reconcile",
                "evidence_kind": "reconcile",
                "actor_role": "observer",
                "status": "accepted",
                "payload": {
                    "reconcile_authority": {
                        "record_verified": True,
                        "merged_commit_sha": "2" * 40,
                    }
                },
            },
            {
                "stage_id": "qa",
                "line_id": "qa_independent_verification",
                "evidence_kind": "independent_verification",
                "actor_role": "qa",
                "status": "failed",
                "payload": {"status": "failed", "verdict": "FAIL"},
            },
            {
                "stage_id": "dispatch",
                "line_id": "observer_dispatch_bounded_workers",
                "evidence_kind": "dispatch_bounded_worker",
                "actor_role": "observer",
                "runtime_context_id": rework_runtime_context_id,
                "task_id": rework_task_id,
                "payload": {
                    "runtime_context_id": rework_runtime_context_id,
                    "task_id": rework_task_id,
                    "parent_task_id": contract_execution_id,
                    "merge_queue_id": "mq-rev9-single-worker-fix",
                    "failed_qa_rework_dispatch_revision": rework_revision,
                    "failed_qa_rework_dispatch_revision_authority": (
                        rework_authority
                    ),
                },
            },
        ],
    }

    selected = server._contract_runtime_current_dispatch_authority_line(record)
    assert selected["status"] == "selected"
    assert selected["completed_line_index"] == 5
    assert (
        server._contract_runtime_mf_parallel_current_generation_worker_count(
            record
        )
        == 1
    )
    assert (
        server._contract_runtime_mf_parallel_current_generation_worker_count(
            {**record, "revision": "rev8"}
        )
        == 2
    )

    record["completed_lines"].append(
        {
            "stage_id": "observer_lane_merge",
            "line_id": "observer_merge",
            "evidence_kind": "merge",
            "actor_role": "observer",
            "runtime_context_id": rework_runtime_context_id,
            "line_instance_id": (
                f"runtime_context:{rework_runtime_context_id}"
            ),
            "payload": {
                "line_instance_id": (
                    f"runtime_context:{rework_runtime_context_id}"
                ),
                "durable_merge_authority": {
                    "schema_version": (
                        server._CONTRACT_RUNTIME_DURABLE_MERGE_SCHEMA_VERSION
                    ),
                    "server_derived": True,
                    "db_verified": True,
                    "pre_qa_merge_authorized": True,
                    "final_qa_required_after_reconcile": True,
                    "project_id": "aming-claw",
                    "backlog_id": "AC-REV9-SINGLE-WORKER-FIX",
                    "contract_execution_id": contract_execution_id,
                    "runtime_context_id": rework_runtime_context_id,
                    "task_id": rework_task_id,
                    "parent_task_id": contract_execution_id,
                    "merge_queue_id": "mq-rev9-single-worker-fix",
                    "queue_item_id": "mqi-rev9-single-worker-fix",
                    "contract_runtime_dispatch_source_ref": (
                        f"contract_runtime:{contract_execution_id}:"
                        "completed_lines:5"
                    ),
                    "merge_commit": "a" * 40,
                    "merge_event_ref": "timeline:123",
                    "merge_event_id": 123,
                    "merge_event_created_at": "2026-08-02T12:00:00Z",
                },
            },
        }
    )
    merge = server._contract_runtime_rev8_two_worker_merge_projection(
        record,
        required_worker_count=1,
    )
    assert merge["all_lane_merges_verified"] is True
    assert merge["required_worker_count"] == 1
    assert merge["runtime_context_id"] == rework_runtime_context_id
    assert merge["dispatch_completed_line_index"] == 5
    assert merge["merge_completed_line_index"] == 6

    current_reconcile_receipt = {
        "record_verified": True,
        "merged_commit_sha": "a" * 40,
        "runtime_context_id": rework_runtime_context_id,
        "task_id": rework_task_id,
        "parent_task_id": contract_execution_id,
        "merge_queue_id": "mq-rev9-single-worker-fix",
    }
    current_reconcile_receipt["authority_hash"] = server.stable_sha256(
        current_reconcile_receipt
    )
    record["completed_lines"].append(
        {
            "stage_id": "reconcile",
            "line_id": "observer_reconcile",
            "evidence_kind": "reconcile",
            "actor_role": "observer",
            "status": "accepted",
            "payload": {
                "reconcile_authority": current_reconcile_receipt,
            },
        }
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_completed_line_acceptance",
        lambda *_args, **_kwargs: {"db_verified": True},
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_reconcile_record_authority",
        lambda *_args, **_kwargs: current_reconcile_receipt,
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_contexts_for_dispatch_line",
        lambda *_args, **_kwargs: [],
    )

    qa_authority = server._contract_runtime_rev8_postmerge_qa_authority(
        None,
        project_id="aming-claw",
        record=record,
    )
    assert qa_authority["verified"] is False
    assert qa_authority["blocker_codes"] == [
        "final_merge_runtime_context_unresolved"
    ]


def _accepted_no_pass_line(*, reported_baseline_failed: int) -> dict:
    failures = [f"test_known_baseline_{index:02d}" for index in range(19)]
    base_commit = "a" * 40
    candidate_commit = "b" * 40
    common_results = {
        "status": "accepted",
        "candidate_new_failures": 0,
        "candidate_specific_issues": [],
        "no_pass_claim": True,
        "passed": False,
        "overall_release_pass": False,
        "overall_release_pass_claimed": False,
        "baseline": {"failed": reported_baseline_failed},
        "candidate": {"failed": len(failures), "passed": 741},
    }
    return {
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "independent_verification",
        "runtime_context_id": "mfrctx-no-pass-binding",
        "task_id": "worker-no-pass-binding",
        "parent_task_id": "parent-no-pass-binding",
        "authorization_source": "qa_session_token_ref",
        "observer_impersonation": False,
        "parent_materialization_authorized": False,
        "commit_sha": candidate_commit,
        "status": "accepted",
        "verdict": "accepted",
        "qa_evidence_provenance": {
            "schema_version": "qa_evidence_provenance.v1",
            "server_derived": True,
            "authorization_source": "qa_session_token_ref",
            "evidence_owner_role": "qa",
            "observer_impersonation": False,
            "parent_materialization_authorized": False,
            "completion_status_gate": {
                "schema_version": "contract_runtime.qa_completion_status_gate.v1",
                "source": "contract_runtime_line_write_normalization",
                "server_derived": True,
                "top_level_status_present": True,
                "top_level_status_passing": True,
                "normalized_status": "accepted",
                "nested_payload_decision_satisfies": False,
            },
            "authenticated_qa_binding": {
                "schema_version": "contract_runtime.authenticated_qa_binding.v1",
                "server_derived": True,
                "qa_principal": "qa:no-pass-binding",
                "qa_session_id": "ses-no-pass-binding",
                "independent_verification_session_matched": True,
            },
        },
        "payload": {
            "schema_version": "mf_parallel.qa_independent_verification.v1",
            "runtime_context_id": "mfrctx-no-pass-binding",
            "task_id": "worker-no-pass-binding",
            "parent_task_id": "parent-no-pass-binding",
            "acceptance_scope": "candidate_regression_and_acceptance_criteria",
            "verdict": "accepted",
            "full_suite_claim": "not_claimed",
            "candidate_new_failures": 0,
            "candidate_specific_issues": [],
            "no_pass_claim": True,
            "overall_release_pass_claimed": False,
            "test_results": dict(common_results),
        },
        "test_results": dict(common_results),
        "verification": {
            "status": "accepted",
            "verdict": "accepted",
            "candidate_new_failures": 0,
            "candidate_specific_issues": [],
            "no_pass_claim": True,
            "overall_release_pass_claimed": False,
        },
        "artifact_refs": {
            "external_no_pass_baseline_ledger": {
                "schema_version": (
                    "contract_runtime.external_no_pass_baseline_ledger.v2"
                ),
                "server_normalized": True,
                "base_commit_sha": base_commit,
                "candidate_commit_sha": candidate_commit,
                "base_failure_identities": failures,
                "candidate_failure_identities": failures,
                "base_reproduction": {
                    "reproduced": len(failures),
                    "total": len(failures),
                    "failure_identities": failures,
                },
                "candidate_suite_counts": {
                    "baseline_known_non_green": len(failures),
                    "failed": len(failures),
                    "passed": 741,
                },
                "candidate_new_failures": 0,
                "candidate_specific_issues": [],
                "no_pass_claim": True,
                "overall_release_pass_claimed": False,
                "refs": ["base:known", "candidate:known"],
            }
        },
    }


def test_exact_accepted_no_pass_line_allows_contract_completion():
    line = _accepted_no_pass_line(reported_baseline_failed=19)
    record = {
        "contract_execution_id": "cex-no-pass-binding",
        "completed_lines": [line],
    }

    assert _line_status_allows_contract_completion(
        line,
        source_record=record,
        source_line_index=0,
    )
    assert _active_failed_qa_line(
        [line],
        source_record=record,
    ) == (-1, {})


def test_accepted_no_pass_count_mismatch_is_active_failed_qa():
    line = _accepted_no_pass_line(reported_baseline_failed=20)
    record = {
        "contract_execution_id": "cex-no-pass-binding",
        "completed_lines": [line],
    }

    assert not _line_status_allows_contract_completion(
        line,
        source_record=record,
        source_line_index=0,
    )
    failed_index, failed_line = _active_failed_qa_line(
        [line],
        source_record=record,
    )
    assert failed_index == 0
    assert failed_line is line


def _partial_rework_baseline_record() -> dict:
    execution_id = "cex-partial-rework-baseline"
    runtime_context_id = "mfrctx-partial-rework-baseline"
    task_id = "worker-partial-rework-baseline"
    commit_sha = "b" * 40
    revision_event_ref = "timeline:19032"
    failed_qa_ref = (
        f"contract_runtime:{execution_id}:completed_lines:10"
    )
    trace_ids = [
        "gqt-partial-rework-baseline-a",
        "gqt-partial-rework-baseline-b",
    ]
    affected_suite = {
        "baseline_failed": 43,
        "failed": 43,
        "new_failures": 0,
        "passed": 981,
    }
    canonical = {
        "stage_id": "worker_implementation",
        "line_id": "worker_implementation",
        "actor_role": "mf_sub",
        "evidence_kind": "implementation",
        "status": "completed",
        "commit_sha": commit_sha,
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "parent_task_id": execution_id,
        "payload": {
            "runtime_context_id": runtime_context_id,
            "task_id": task_id,
            "parent_task_id": execution_id,
            "graph_trace_ids": trace_ids,
            "canonical_rework_lineage_revision": {
                "schema_version": (
                    "contract_runtime.worker_implementation_rework_revision.v1"
                ),
                "source": "server_verified_failed_qa_rework",
                "failed_qa_completed_line_index": 10,
                "revision_event_ref": revision_event_ref,
                "commit_sha": commit_sha,
                "append_only_history_preserved": True,
            },
            "canonical_rework_lineage_revision_authority": {
                "schema_version": (
                    "runtime_context.clean_cumulative_git_revision_authority.v1"
                ),
                "source": "runtime_context_clean_cumulative_git_revision",
                "server_derived": True,
                "clean_worktree": True,
                "actual_head_commit": commit_sha,
                "revision_event_ref": revision_event_ref,
                "failed_qa_source_ref": failed_qa_ref,
            },
            "failed_qa_revision_rejoin_marker": {
                "schema_version": (
                    "contract_runtime.failed_qa_revision_rejoin_marker.v1"
                ),
                "source": "accepted_runtime_context_rejoin_event",
                "evidence_backfill": False,
                "contract_execution_id": execution_id,
                "runtime_context_id": runtime_context_id,
                "task_id": task_id,
                "parent_task_id": execution_id,
                "revision_event_ref": revision_event_ref,
                "failed_qa_source": "contract_runtime_completed_lines",
                "failed_qa_source_ref": failed_qa_ref,
            },
            "graph_trace_db_evidence": {
                "schema_version": "mf_subagent_graph_trace_db_evidence.v1",
                "db_verified": True,
                "missing_trace_ids": [],
                "identity_mismatches": [],
                "query_source": "mf_subagent",
                "worker_role": "mf_sub",
                "runtime_context_id": runtime_context_id,
                "task_id": task_id,
                "parent_task_id": execution_id,
                "requested_trace_ids": trace_ids,
                "verified_trace_ids": trace_ids,
            },
            "worker_evidence_provenance": {
                "schema_version": (
                    "contract_runtime.worker_evidence_provenance.v1"
                ),
                "source": "runtime_context_copy_safe_worker_proof",
                "verified": True,
                "worker_owned": True,
                "worker_role": "mf_sub",
                "runtime_context_id": runtime_context_id,
                "task_id": task_id,
            },
            "tests": [
                {
                    "name": "affected_suite_baseline_compare",
                    "status": "baseline_matched",
                    **affected_suite,
                }
            ],
        },
        "verification": {"affected_suite": dict(affected_suite)},
    }
    duplicate = deepcopy(canonical)
    duplicate["payload"].pop("canonical_rework_lineage_revision")
    duplicate["payload"].pop(
        "canonical_rework_lineage_revision_authority"
    )
    duplicate["payload"].pop("failed_qa_revision_rejoin_marker")
    duplicate["test_results"] = {
        "baseline_failed": 43,
        "affected_suite_failed": 43,
        "new_failures": 0,
    }
    padding = [
        {
            "stage_id": "historical",
            "line_id": f"historical_{index}",
            "actor_role": "observer",
            "evidence_kind": "historical_context",
            "status": "accepted",
        }
        for index in range(10)
    ]
    failed_qa = {
        "stage_id": "qa",
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "independent_verification",
        "status": "failed",
        "payload": {"status": "failed", "verdict": "FAIL"},
    }
    return {
        "schema_version": "contract_runtime_execution_record.v1",
        "project_id": "aming-claw",
        "backlog_id": "AC-PARTIAL-REWORK-BASELINE",
        "contract_execution_id": execution_id,
        "contract_id": "mf_parallel.v2",
        "completed_lines": [*padding, failed_qa, canonical, duplicate],
    }


def test_partial_rework_baseline_selects_canonical_line_before_duplicate():
    record = _partial_rework_baseline_record()
    lines = record["completed_lines"]
    canonical = lines[11]
    duplicate = lines[12]

    assert _line_status_allows_contract_completion(
        canonical,
        source_record=record,
        source_line_index=11,
    )
    assert not _line_status_allows_contract_completion(
        duplicate,
        source_record=record,
        source_line_index=12,
    )
    assert _worker_commit_completed_implementation(
        record,
        runtime_context_id="mfrctx-partial-rework-baseline",
        task_id="worker-partial-rework-baseline",
    ) is canonical
    satisfying = _contract_completion_satisfying_lines(
        lines,
        source_record=record,
    )
    assert canonical in satisfying
    assert duplicate not in satisfying
    assert len(record["completed_lines"]) == 13


def test_clean_worker_implementation_still_selects_latest_completion():
    runtime_context_id = "mfrctx-clean-worker-implementation"
    task_id = "clean-worker-implementation"
    earlier = {
        "line_id": "worker_implementation",
        "actor_role": "mf_sub",
        "evidence_kind": "implementation",
        "status": "completed",
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "commit_sha": "a" * 40,
        "verification": {"passed": True, "failed": 0},
    }
    latest = {
        **earlier,
        "commit_sha": "b" * 40,
        "verification": {"passed": True, "failed": 0, "new_failures": 0},
    }
    record = {
        "contract_execution_id": "cex-clean-worker-implementation",
        "contract_id": "mf_parallel.v2",
        "completed_lines": [earlier, latest],
    }

    assert _worker_commit_completed_implementation(
        record,
        runtime_context_id=runtime_context_id,
        task_id=task_id,
    ) is latest


def test_partial_rework_baseline_fails_closed_on_counts_or_identity_drift():
    record = _partial_rework_baseline_record()
    canonical = record["completed_lines"][11]
    affected_sources = (
        canonical["payload"]["tests"][0],
        canonical["verification"]["affected_suite"],
    )
    for source in affected_sources:
        source["baseline_failure_identities"] = [
            "test_inherited_a",
            "test_inherited_b",
        ]
        source["candidate_failure_identities"] = [
            "test_inherited_a",
            "test_inherited_b",
        ]
    assert _line_status_allows_contract_completion(
        canonical,
        source_record=record,
        source_line_index=11,
    )

    identity_drift = deepcopy(record)
    identity_drift["completed_lines"][11]["verification"]["affected_suite"][
        "candidate_failure_identities"
    ] = ["test_inherited_a", "test_candidate_new"]
    assert not _line_status_allows_contract_completion(
        identity_drift["completed_lines"][11],
        source_record=identity_drift,
        source_line_index=11,
    )

    count_drift = deepcopy(record)
    count_drift["completed_lines"][11]["verification"]["affected_suite"][
        "failed"
    ] = 44
    assert not _line_status_allows_contract_completion(
        count_drift["completed_lines"][11],
        source_record=count_drift,
        source_line_index=11,
    )

    candidate_new = deepcopy(record)
    candidate_new["completed_lines"][11]["payload"]["tests"][0][
        "new_failures"
    ] = 1
    assert not _line_status_allows_contract_completion(
        candidate_new["completed_lines"][11],
        source_record=candidate_new,
        source_line_index=11,
    )

    forged_authority = deepcopy(record)
    forged_authority["completed_lines"][11]["payload"][
        "canonical_rework_lineage_revision_authority"
    ]["server_derived"] = False
    assert not _line_status_allows_contract_completion(
        forged_authority["completed_lines"][11],
        source_record=forged_authority,
        source_line_index=11,
    )


def _synthetic_later_qa_pass(*, summary: str) -> dict:
    return {
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "independent_verification",
        "status": "passed",
        "observer_impersonation": False,
        "payload": {
            "status": "passed",
            "summary": summary,
        },
        "qa_evidence_provenance": {
            "schema_version": "qa_evidence_provenance.v1",
            "server_derived": True,
            "authorization_source": "qa_role_session",
            "evidence_owner_role": "qa",
            "observer_impersonation": False,
            "completion_status_gate": {
                "schema_version": (
                    "contract_runtime.qa_completion_status_gate.v1"
                ),
                "source": "contract_runtime_line_write_normalization",
                "server_derived": True,
                "top_level_status_present": False,
                "top_level_status_passing": False,
                "normalized_status": "",
                "nested_payload_decision_satisfies": False,
            },
        },
    }


def test_unauthenticated_synthetic_pass_cannot_clear_repair_boundary():
    failed = {
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "status": "failed",
        "payload": {"summary": "Independent QA failed the worker commit."},
    }
    passed = _synthetic_later_qa_pass(
        summary="Independent QA passed the retry."
    )
    record = {
        "contract_execution_id": "cex-synthetic-qa-pass",
        "completed_lines": [failed, passed],
    }

    assert not _line_status_allows_contract_completion(
        passed,
        source_record=record,
        source_line_index=1,
    )
    failed_index, failed_line = _active_failed_qa_line(
        record["completed_lines"],
        source_record=record,
    )
    assert failed_index == 1
    assert failed_line is passed


def test_authenticated_invalid_pass_is_ignored_without_clearing_prior_failure():
    prior_progress = {
        "stage_id": "observer_integration",
        "line_id": "observer_reconcile",
        "actor_role": "observer",
        "evidence_kind": "reconcile",
        "status": "passed",
    }
    invalid_pass = _authenticated_authored_qa_line(
        baseline_status="baseline_known_failure_only",
    )
    record = {
        "contract_execution_id": "cex-historical-invalid-authored-pass",
        "completed_lines": [prior_progress, invalid_pass],
    }

    satisfying = _contract_completion_satisfying_lines(
        record["completed_lines"],
        source_record=record,
    )
    assert satisfying == [prior_progress]
    assert _active_failed_qa_line(
        record["completed_lines"],
        source_record=record,
    ) == (-1, {})

    genuine_failure = {
        "stage_id": "qa",
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "independent_verification",
        "status": "failed",
    }
    with_prior_failure = {
        "contract_execution_id": "cex-invalid-pass-after-genuine-failure",
        "completed_lines": [
            prior_progress,
            genuine_failure,
            invalid_pass,
        ],
    }
    failed_index, failed_line = _active_failed_qa_line(
        with_prior_failure["completed_lines"],
        source_record=with_prior_failure,
    )
    assert failed_index == 1
    assert failed_line is genuine_failure

    canonical_pass = _authenticated_authored_qa_line(
        baseline_status="baseline_observation",
    )
    canonical_record = {
        "contract_execution_id": "cex-canonical-baseline-observation-pass",
        "completed_lines": [prior_progress, canonical_pass],
    }
    assert _contract_completion_satisfying_lines(
        canonical_record["completed_lines"],
        source_record=canonical_record,
    ) == canonical_record["completed_lines"]


def test_synthetic_later_pass_with_failure_summary_keeps_repair_active():
    failed = {
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "status": "failed",
        "payload": {"summary": "Independent QA failed the worker commit."},
    }
    contradictory = _synthetic_later_qa_pass(
        summary="Independent QA failed the worker commit again."
    )
    record = {
        "contract_execution_id": "cex-synthetic-qa-contradiction",
        "completed_lines": [failed, contradictory],
    }

    failed_index, failed_line = _active_failed_qa_line(
        record["completed_lines"],
        source_record=record,
    )
    assert failed_index == 1
    assert failed_line is contradictory


def test_failed_qa_rework_close_gate_prevalidates_without_generic_mutation(
    monkeypatch,
):
    """The facade's first gate must not append the canonical retry line.

    The runtime-context canonical helper owns the one permitted mutation.  If
    the generic timeline close gate writes first, the same request is observed
    as a duplicate revision by the canonical helper and can fail after a
    partial ContractRuntime advance.
    """

    record = {
        "contract_execution_id": "cex-failed-qa-atomic",
        "project_id": "project-failed-qa-atomic",
        "completed_lines": [],
        "runtime_guide": {
            "next_legal_action": {
                "stage_id": "worker_implementation",
                "line_id": "worker_implementation",
                "evidence_kind": "implementation",
            }
        },
    }
    submit_calls = []

    class _Store:
        def get(self, _contract_execution_id):
            return record

    class _Runtime:
        store = _Store()

        def current_guide(self, *_args, **_kwargs):
            return record["runtime_guide"]

        def current_record(self, *_args, **_kwargs):
            return record

        def submit_line_write(self, *_args, **_kwargs):
            submit_calls.append((_args, _kwargs))
            raise AssertionError(
                "generic close gate must not mutate a prevalidated failed-QA retry"
            )

    runtime_context = SimpleNamespace(
        runtime_context_id="mfrctx-failed-qa-atomic",
        task_id="worker-failed-qa-atomic",
    )
    monkeypatch.setattr(server, "_contract_runtime", lambda _conn: _Runtime())
    monkeypatch.setattr(
        server,
        "_contract_runtime_apply_mf_parallel_context_projection",
        lambda *_args, **_kwargs: (record, {}),
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_close_line_for_event",
        lambda *_args, **_kwargs: {
            "stage_id": "worker_implementation",
            "line_id": "worker_implementation",
            "evidence_kind": "implementation",
        },
    )
    monkeypatch.setattr(
        server,
        "_runtime_current_state_from_record",
        lambda _record: {
            "next_legal_action": record["runtime_guide"]["next_legal_action"]
        },
    )
    monkeypatch.setattr(
        server,
        "_contract_runtime_write_from_record",
        lambda *_args, **_kwargs: {
            "stage_id": "worker_implementation",
            "line_id": "worker_implementation",
            "evidence_kind": "implementation",
        },
    )
    monkeypatch.setattr(
        server,
        "_runtime_context_revise_failed_qa_implementation_lineage",
        lambda *_args, **_kwargs: {
            "status": "validated_submission",
            "canonical_payload": dict(_kwargs["payload"]),
        },
    )
    monkeypatch.setattr(
        parallel_branch_runtime,
        "get_branch_context_by_runtime_context_id",
        lambda *_args, **_kwargs: runtime_context,
    )

    gate = server._contract_runtime_close_gate(
        object(),
        project_id="project-failed-qa-atomic",
        body={
            "contract_execution_id": "cex-failed-qa-atomic",
            "runtime_context_id": runtime_context.runtime_context_id,
            "actor": "mf_sub",
        },
        event_kind="implementation",
        norm_payload={
            "runtime_context_id": runtime_context.runtime_context_id,
            "task_id": runtime_context.task_id,
        },
        trusted_actor_role="mf_sub",
    )

    assert gate["status"] == "validated_submission"
    assert gate["canonical_submit_required"] is True
    assert submit_calls == []


def test_failed_qa_revision_prepares_guide_before_single_cas_update():
    prior_commit = "a" * 40
    revised_commit = "b" * 40
    revision_event_ref = "timeline:failed-qa-rejoin"
    identity = {
        "runtime_context_id": "mfrctx-failed-qa-single-cas",
        "task_id": "worker-failed-qa-single-cas",
        "parent_task_id": "parent-failed-qa-single-cas",
        "worker_id": "worker-failed-qa-single-cas",
        "worker_slot_id": "worker-failed-qa-single-cas",
        "target_project_root": "/tmp/failed-qa-single-cas",
        "fence_token_hash": "sha256:" + ("c" * 64),
        "session_token_ref": "wstok-failed-qa-single-cas",
    }
    failed_qa = {
        "stage_id": "qa",
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "independent_verification",
        "status": "failed",
        "payload": {"status": "failed", "verdict": "FAIL"},
    }
    prior_implementation = {
        "stage_id": "worker_implementation",
        "line_id": "worker_implementation",
        "actor_role": "mf_sub",
        "evidence_kind": "implementation",
        "commit_sha": prior_commit,
        "changed_files": ["agent/governance/server.py"],
        "graph_trace_ids": ["gqt-failed-qa-single-cas"],
        **identity,
        "payload": {
            **identity,
            "changed_files": ["agent/governance/server.py"],
            "graph_trace_ids": ["gqt-failed-qa-single-cas"],
        },
    }
    record = {
        "contract_execution_id": "cex-failed-qa-single-cas",
        "contract_id": "mf_parallel.v2",
        "project_id": "project-failed-qa-single-cas",
        "backlog_id": "backlog-failed-qa-single-cas",
        "execution_state_revision": 7,
        "completed_lines": [failed_qa, prior_implementation],
    }

    class _SingleCasStore:
        def __init__(self):
            self.record = deepcopy(record)
            self.update_calls = 0

        def get(self, _contract_execution_id):
            return deepcopy(self.record)

        def update(
            self,
            _contract_execution_id,
            updated,
            *,
            expected_revision=None,
        ):
            self.update_calls += 1
            if self.update_calls > 1:
                raise AssertionError(
                    "failed-QA revision must use one prepared CAS update"
                )
            assert expected_revision == 7
            self.record = deepcopy(updated)
            return deepcopy(self.record)

    store = _SingleCasStore()
    runtime = object.__new__(ContractRuntime)
    runtime.store = store

    def _record_view(source, *, actor_role=None, completed_lines=None, **_kwargs):
        view = deepcopy(dict(source))
        view["completed_lines"] = deepcopy(list(completed_lines or []))
        view["execution_state"] = {
            "execution_state_revision": source["execution_state_revision"],
        }
        retry_commit_exists = any(
            line.get("line_id") == "worker_commit"
            for line in view["completed_lines"][1:]
            if isinstance(line, dict)
        )
        view["runtime_guide"] = {
            "next_legal_action": {
                "stage_id": (
                    "worker_attestation"
                    if retry_commit_exists
                    else "worker_commit"
                ),
                "line_id": (
                    "worker_finish_time_attestation"
                    if retry_commit_exists
                    else "worker_commit"
                ),
                "owner_role": "mf_sub",
                "evidence_kind": (
                    "record_finish_time_worker_attestation"
                    if retry_commit_exists
                    else "commit"
                ),
            }
        }
        view["precheck_decision"] = WriteGateDecision(ok=True).to_dict()
        return view

    runtime._record_view = _record_view
    marker = {
        "revision_event_ref": revision_event_ref,
        "session_token_ref_rotation": {},
    }
    authority = {
        "source": "runtime_context_clean_cumulative_git_revision",
        "server_derived": True,
        "actual_head_commit": revised_commit,
        "clean_worktree": True,
        "cumulative_changed_files": ["agent/governance/server.py"],
        "owned_files": ["agent/governance/server.py"],
        "revision_event_ref": revision_event_ref,
    }
    revision_write = {
        "stage_id": "worker_implementation",
        "line_id": "worker_implementation",
        "actor_role": "mf_sub",
        "evidence_kind": "implementation",
        "commit_sha": revised_commit,
        "changed_files": ["agent/governance/server.py"],
        "graph_trace_ids": ["gqt-failed-qa-single-cas"],
        **identity,
        "payload": {
            **identity,
            "changed_files": ["agent/governance/server.py"],
            "graph_trace_ids": ["gqt-failed-qa-single-cas"],
            "graph_trace_db_evidence": {
                "db_verified": True,
                "verified_trace_ids": ["gqt-failed-qa-single-cas"],
            },
            "failed_qa_revision_rejoin_marker": marker,
            "canonical_rework_lineage_revision_authority": authority,
        },
    }
    result = runtime.revise_failed_qa_worker_implementation(
        record["contract_execution_id"],
        revision_write,
        actor_role="mf_sub",
    )

    assert result["ok"] is True
    assert result["status"] == "revised"
    assert store.update_calls == 1
    assert store.record["execution_state_revision"] == 8
    assert store.record["runtime_guide"]["next_legal_action"]["line_id"] == (
        "worker_commit"
    )

    exact_replay = runtime.revise_failed_qa_worker_implementation(
        record["contract_execution_id"],
        revision_write,
        actor_role="mf_sub",
    )
    assert exact_replay["ok"] is True
    assert exact_replay["status"] == "already_completed"
    assert store.update_calls == 1

    store.record["completed_lines"].append(
        {
            "stage_id": "worker_commit",
            "line_id": "worker_commit",
            "actor_role": "mf_sub",
            "evidence_kind": "commit",
            "commit_sha": revised_commit,
            **identity,
            "payload": dict(identity),
        }
    )
    exact_after_commit = runtime.revise_failed_qa_worker_implementation(
        record["contract_execution_id"],
        revision_write,
        actor_role="mf_sub",
    )
    assert exact_after_commit["ok"] is True
    assert exact_after_commit["status"] == "already_completed"
    assert store.update_calls == 1

    later_commit = "d" * 40
    later_revision = deepcopy(revision_write)
    later_revision["commit_sha"] = later_commit
    later_revision["payload"][
        "canonical_rework_lineage_revision_authority"
    ]["actual_head_commit"] = later_commit
    rejected_later_revision = runtime.revise_failed_qa_worker_implementation(
        record["contract_execution_id"],
        later_revision,
        actor_role="mf_sub",
    )
    assert rejected_later_revision["ok"] is False
    assert any(
        "closed after retry worker_commit" in error
        for error in rejected_later_revision["decision"]["errors"]
    )
    assert store.update_calls == 1


def test_failed_qa_fresh_dispatch_revision_is_append_only_single_cas_and_replay_safe():
    historical_dispatch = {
        "stage_id": "dispatch",
        "line_id": "observer_dispatch_bounded_workers",
        "actor_role": "observer",
        "evidence_kind": "dispatch_bounded_worker",
        "runtime_context_id": "mfrctx-original",
        "task_id": "worker-original",
        "parent_task_id": "cex-failed-qa-fresh-dispatch",
        "worker_id": "worker-original",
        "worker_slot_id": "worker-original",
        "owned_files": ["agent/governance/server.py"],
        "payload": {
            "runtime_context_id": "mfrctx-original",
            "task_id": "worker-original",
            "parent_task_id": "cex-failed-qa-fresh-dispatch",
            "worker_role": "mf_sub",
        },
    }
    failed_qa = {
        "stage_id": "qa",
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "independent_verification",
        "status": "failed",
        "payload": {"status": "failed", "verdict": "FAIL"},
    }
    record = {
        "contract_execution_id": "cex-failed-qa-fresh-dispatch",
        "contract_id": "mf_parallel.v2",
        "project_id": "project-failed-qa-fresh-dispatch",
        "backlog_id": "backlog-failed-qa-fresh-dispatch",
        "execution_state_revision": 11,
        "completed_lines": [historical_dispatch, failed_qa],
    }

    class _SingleCasStore:
        def __init__(self):
            self.record = deepcopy(record)
            self.update_calls = 0

        def get(self, _contract_execution_id):
            return deepcopy(self.record)

        def update(
            self,
            _contract_execution_id,
            updated,
            *,
            expected_revision=None,
        ):
            self.update_calls += 1
            assert expected_revision == self.record["execution_state_revision"]
            self.record = deepcopy(updated)
            return deepcopy(self.record)

    store = _SingleCasStore()
    runtime = object.__new__(ContractRuntime)
    runtime.store = store

    def _record_view(source, *, actor_role=None, completed_lines=None, **_kwargs):
        view = deepcopy(dict(source))
        view["completed_lines"] = deepcopy(list(completed_lines or []))
        view["runtime_guide"] = {
            "next_legal_action": {
                "stage_id": "worker_read",
                "line_id": "worker_read_runtime_guide",
                "owner_role": "mf_sub",
                "evidence_kind": "read_receipt",
            }
        }
        view["precheck_decision"] = WriteGateDecision(ok=True).to_dict()
        return view

    runtime._record_view = _record_view
    identity = {
        "runtime_context_id": "mfrctx-replacement",
        "task_id": "worker-replacement",
        "parent_task_id": "cex-failed-qa-fresh-dispatch",
        "worker_id": "worker-replacement",
        "worker_slot_id": "worker-replacement",
        "owned_files": [
            "agent/governance/server.py",
            "agent/tests/test_graph_governance_api.py",
        ],
        "route_token_ref": "rtok-replacement",
    }
    write = {
        "stage_id": "dispatch",
        "line_id": "observer_dispatch_bounded_workers",
        "actor_role": "observer",
        "evidence_kind": "dispatch_bounded_worker",
        **identity,
        "payload": {
            **identity,
            "worker_role": "mf_sub",
            "failed_qa_rework_dispatch_revision_authority": {
                "source": "parallel_branch_allocate_failed_qa_rework",
                "server_derived": True,
                "contract_execution_id": record["contract_execution_id"],
                "failed_qa_completed_line_index": 1,
                "runtime_context_id": identity["runtime_context_id"],
                "task_id": identity["task_id"],
            },
        },
    }

    result = runtime.revise_failed_qa_observer_dispatch(
        record["contract_execution_id"],
        write,
        actor_role="observer",
    )

    assert result["ok"] is True
    assert result["status"] == "revised"
    assert result["append_only_history_preserved"] is True
    assert store.update_calls == 1
    assert store.record["execution_state_revision"] == 12
    assert len(store.record["completed_lines"]) == 3
    assert store.record["completed_lines"][0] == historical_dispatch
    replacement = store.record["completed_lines"][-1]
    assert replacement["runtime_context_id"] == "mfrctx-replacement"
    assert replacement["payload"]["failed_qa_rework_dispatch_revision"][
        "timeline_projection_authoritative"
    ] is False

    exact_replay = runtime.revise_failed_qa_observer_dispatch(
        record["contract_execution_id"],
        write,
        actor_role="observer",
    )
    assert exact_replay["ok"] is True
    assert exact_replay["status"] == "already_completed"
    assert exact_replay["contract_runtime_line_mutated"] is False
    assert store.update_calls == 1
    assert len(store.record["completed_lines"]) == 3
    assert store.record["execution_state_revision"] == 12

    different_identity = deepcopy(write)
    different_identity.update(
        {
            "runtime_context_id": "mfrctx-replacement-b",
            "task_id": "worker-replacement-b",
            "worker_id": "worker-replacement-b",
            "worker_slot_id": "worker-replacement-b",
        }
    )
    different_identity["payload"].update(
        {
            "runtime_context_id": "mfrctx-replacement-b",
            "task_id": "worker-replacement-b",
            "worker_id": "worker-replacement-b",
            "worker_slot_id": "worker-replacement-b",
        }
    )
    different_authority = different_identity["payload"][
        "failed_qa_rework_dispatch_revision_authority"
    ]
    different_authority.update(
        {
            "runtime_context_id": "mfrctx-replacement-b",
            "task_id": "worker-replacement-b",
        }
    )
    second_identity = runtime.revise_failed_qa_observer_dispatch(
        record["contract_execution_id"],
        different_identity,
        actor_role="observer",
    )
    assert second_identity["ok"] is False
    assert any(
        "already has a different canonical replacement" in error
        for error in second_identity["decision"]["errors"]
    )
    assert store.update_calls == 1
    assert len(store.record["completed_lines"]) == 3
    assert store.record["execution_state_revision"] == 12

    preserved_first_cycle = deepcopy(store.record["completed_lines"])
    later_failed_qa = deepcopy(failed_qa)
    later_failed_qa["payload"]["acceptance_failed"] = [
        "second_rework_generation_required"
    ]
    store.record["completed_lines"].extend(
        [
            {
                "stage_id": "worker_commit",
                "line_id": "worker_commit",
                "actor_role": "mf_sub",
                "evidence_kind": "worker_commit",
                "commit_sha": "a" * 40,
            },
            later_failed_qa,
        ]
    )
    store.record["execution_state_revision"] = 13

    stale_cycle_replay = runtime.revise_failed_qa_observer_dispatch(
        record["contract_execution_id"],
        write,
        actor_role="observer",
    )
    assert stale_cycle_replay["ok"] is False
    assert any(
        "active failed QA line" in error
        for error in stale_cycle_replay["decision"]["errors"]
    )
    assert store.update_calls == 1
    assert store.record["completed_lines"][:3] == preserved_first_cycle

    second_write = deepcopy(write)
    second_authority = second_write["payload"][
        "failed_qa_rework_dispatch_revision_authority"
    ]
    second_authority["failed_qa_completed_line_index"] = 4
    second_result = runtime.revise_failed_qa_observer_dispatch(
        record["contract_execution_id"],
        second_write,
        actor_role="observer",
    )
    assert second_result["ok"] is True
    assert second_result["status"] == "revised"
    assert store.update_calls == 2
    assert store.record["completed_lines"][:3] == preserved_first_cycle
    second_exact_replay = runtime.revise_failed_qa_observer_dispatch(
        record["contract_execution_id"],
        second_write,
        actor_role="observer",
    )
    assert second_exact_replay["ok"] is True
    assert second_exact_replay["status"] == "already_completed"
    assert store.update_calls == 2

    duplicate_current_authority = deepcopy(
        store.record["completed_lines"][-1]
    )
    store.record["completed_lines"].append(duplicate_current_authority)
    multiple_current_authorities = runtime.revise_failed_qa_observer_dispatch(
        record["contract_execution_id"],
        second_write,
        actor_role="observer",
    )
    assert multiple_current_authorities["ok"] is False
    assert any(
        "multiple current-cycle" in error
        for error in multiple_current_authorities["decision"]["errors"]
    )
    assert store.update_calls == 2


_CLOSE_GRADE_PROJECT_ID = "aming-claw"
_CLOSE_GRADE_BACKLOG_ID = "AC-REV9-CLOSE-GRADE-RECONCILE-AUTHORITY"
_CLOSE_GRADE_EXECUTION_ID = "cex-mf-parallel-close-grade-authority"
_CLOSE_GRADE_BASE_COMMIT = "0" * 40
_CLOSE_GRADE_LANE_A_COMMIT = "1" * 40
_CLOSE_GRADE_LANE_B_COMMIT = "2" * 40


class _CloseGradeNoCloseConn:
    """Hand the shared in-memory connection to server-side helpers."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self) -> None:
        return None


def _close_grade_graph_payload() -> dict:
    return {
        "deps_graph": {
            "nodes": [{"id": "n1", "kind": "module", "path": "planner.py"}],
            "edges": [],
        }
    }


def _close_grade_connection(tmp_path, monkeypatch) -> sqlite3.Connection:
    from agent.governance.db import _ensure_schema

    monkeypatch.setattr(
        "agent.governance.db._governance_root",
        lambda: tmp_path / "state",
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    graph_snapshot_store.ensure_schema(conn)
    monkeypatch.setattr(
        server,
        "get_connection",
        lambda _project_id: _CloseGradeNoCloseConn(conn),
    )
    monkeypatch.setattr(
        "agent.governance.db.get_connection",
        lambda _project_id: _CloseGradeNoCloseConn(conn),
    )
    return conn


def _close_grade_lane_context(conn, *, task_id, merge_queue_id, project_root):
    return parallel_branch_runtime.upsert_branch_context(
        conn,
        parallel_branch_runtime.BranchTaskRuntimeContext(
            project_id=_CLOSE_GRADE_PROJECT_ID,
            task_id=task_id,
            parent_task_id=_CLOSE_GRADE_EXECUTION_ID,
            root_task_id=_CLOSE_GRADE_EXECUTION_ID,
            backlog_id=_CLOSE_GRADE_BACKLOG_ID,
            worker_id=f"worker-{task_id}",
            worker_slot_id=f"slot-{task_id}",
            governance_project_id=_CLOSE_GRADE_PROJECT_ID,
            target_project_id=_CLOSE_GRADE_PROJECT_ID,
            target_project_root=str(project_root),
            branch_ref=f"refs/heads/codex/{task_id}",
            worktree_path=str(project_root),
            base_commit=_CLOSE_GRADE_BASE_COMMIT,
            target_head_commit=_CLOSE_GRADE_BASE_COMMIT,
            merge_queue_id=merge_queue_id,
            status="worktree_ready",
            lease_expires_at="2999-01-01T00:00:00Z",
        ),
        now_iso="2026-08-06T10:00:00Z",
    )


def _close_grade_lane_merge_event(conn, *, context, commit_sha):
    return task_timeline.record_event(
        conn,
        project_id=_CLOSE_GRADE_PROJECT_ID,
        backlog_id=_CLOSE_GRADE_BACKLOG_ID,
        task_id=context.task_id,
        event_type="merge.live",
        event_kind="merge",
        phase="merge",
        actor="observer:rev9-close-grade",
        status="passed",
        commit_sha=commit_sha,
        payload={
            "actor_role": "observer",
            "backlog_id": _CLOSE_GRADE_BACKLOG_ID,
            "contract_execution_id": _CLOSE_GRADE_EXECUTION_ID,
            "runtime_context_id": context.runtime_context_id,
            "task_id": context.task_id,
            "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
            "merge_queue_id": context.merge_queue_id,
            "merge_commit": commit_sha,
            "target_head_after_merge": commit_sha,
        },
    )


def _close_grade_durable_merge_line(
    *,
    context,
    merge_event,
    commit_sha,
    target_head_before_merge,
    dispatch_source_ref,
):
    return {
        "stage_id": "observer_lane_merge",
        "line_id": "observer_merge",
        "actor_role": "observer",
        "evidence_kind": "merge",
        "line_instance_id": f"runtime_context:{context.runtime_context_id}",
        "runtime_context_id": context.runtime_context_id,
        "task_id": context.task_id,
        "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
        "commit_sha": commit_sha,
        "payload": {
            "durable_merge_authority": {
                "schema_version": (
                    "contract_runtime.observer_merge_durable_authority.v1"
                ),
                "source": (
                    "parallel_branch_merge_queue+task_timeline_merge"
                ),
                "server_derived": True,
                "db_verified": True,
                "project_id": _CLOSE_GRADE_PROJECT_ID,
                "backlog_id": _CLOSE_GRADE_BACKLOG_ID,
                "contract_execution_id": _CLOSE_GRADE_EXECUTION_ID,
                "runtime_context_id": context.runtime_context_id,
                "task_id": context.task_id,
                "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
                "merge_queue_id": context.merge_queue_id,
                "queue_item_id": (
                    f"{context.merge_queue_id}:{context.task_id}"
                ),
                "queue_item_status": "merged",
                "branch_head": commit_sha,
                "merge_commit": commit_sha,
                "target_head_before_merge": target_head_before_merge,
                "target_head_after_merge": commit_sha,
                "merge_gate_passed": True,
                "merge_event_ref": f"timeline:{merge_event['id']}",
                "merge_event_id": int(merge_event["id"]),
                "merge_event_created_at": str(merge_event["created_at"]),
                "timeline_event_refs": [f"timeline:{merge_event['id']}"],
                "contract_runtime_dispatch_source_ref": dispatch_source_ref,
                # rev8/rev9 merge every lane before canonical reconcile and
                # run the integration QA afterwards.
                "pre_qa_merge_authorized": True,
                "final_qa_required_after_reconcile": True,
                "qa_contract_runtime_verified": False,
                "qa_completed_line_index": -1,
                "qa_graph_completed_line_index": -1,
                "qa_acceptance_ref": "",
                "qa_audit_only_no_pass_authority": {},
                "close_satisfying": False,
                "no_pass_claim": False,
                "overall_release_pass_claimed": False,
                "authoritative_pass_synthesized": False,
                "worker_commit_completed_line_index": -1,
            },
            "merge_commit": commit_sha,
            "merge_gate_passed": True,
            "merge_queue_id": context.merge_queue_id,
            "runtime_context_id": context.runtime_context_id,
            "task_id": context.task_id,
            "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
        },
    }


def _close_grade_worker_lines(*, context, commit_sha):
    lines = []
    for stage_id, line_id, evidence_kind, status in (
        ("worker_read", "worker_read_runtime_guide", "read_receipt", "accepted"),
        ("worker_startup", "worker_startup", "mf_subagent_startup", "passed"),
        ("worker_context", "worker_graph_context", "graph_trace", ""),
        (
            "worker_implementation",
            "worker_implementation",
            "implementation",
            "passed",
        ),
        ("worker_commit", "worker_commit", "worker_commit", ""),
        (
            "worker_attestation",
            "worker_finish_time_attestation",
            "record_finish_time_worker_attestation",
            "",
        ),
        ("worker_finish", "worker_finish_gate", "mf_subagent_finish_gate", ""),
    ):
        line = {
            "stage_id": stage_id,
            "line_id": line_id,
            "actor_role": "mf_sub",
            "worker_role": "mf_sub",
            "evidence_kind": evidence_kind,
            "line_instance_id": (
                f"runtime_context:{context.runtime_context_id}"
            ),
            "runtime_context_id": context.runtime_context_id,
            "task_id": context.task_id,
            "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
            "payload": {
                "runtime_context_id": context.runtime_context_id,
                "task_id": context.task_id,
                "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
            },
        }
        if status:
            line["status"] = status
        if line_id in {"worker_commit", "worker_finish_gate"}:
            line["commit_sha"] = commit_sha
            line["payload"]["commit_sha"] = commit_sha
        lines.append(line)
    return lines


def _rev9_close_grade_world(
    tmp_path,
    monkeypatch,
    *,
    record_reconcile_event: bool = True,
    reconcile_before_final_lane_merge: bool = False,
    reconcile_scope_execution_id: str = "",
):
    """Build one real rev9 post-merge world through the server projections.

    Every authority here is derived by the shipped server code from durable
    SQLite state: runtime contexts, task timeline events, the active graph
    snapshot, and its current-full reconcile provenance.  Nothing patches an
    authority producer and no reconcile identity is hand-fed onto a line.
    """

    conn = _close_grade_connection(tmp_path, monkeypatch)
    project_root = tmp_path / "rev9-close-grade-target"
    project_root.mkdir()
    monkeypatch.setattr(
        server.project_service,
        "resolve_project_root",
        lambda *_args, **_kwargs: project_root,
    )
    monkeypatch.setattr(
        server,
        "_git_head_commit",
        lambda _root: _CLOSE_GRADE_LANE_B_COMMIT,
    )
    ancestry = {
        (_CLOSE_GRADE_BASE_COMMIT, _CLOSE_GRADE_LANE_A_COMMIT),
        (_CLOSE_GRADE_LANE_A_COMMIT, _CLOSE_GRADE_LANE_B_COMMIT),
        (_CLOSE_GRADE_BASE_COMMIT, _CLOSE_GRADE_LANE_B_COMMIT),
    }
    monkeypatch.setattr(
        server,
        "_git_commit_is_ancestor",
        lambda _root, ancestor, descendant: (
            ancestor == descendant or (ancestor, descendant) in ancestry
        ),
    )

    lane_a = _close_grade_lane_context(
        conn,
        task_id="rev9-close-grade-models",
        merge_queue_id="mq-rev9-close-grade-models",
        project_root=project_root,
    )
    lane_b = _close_grade_lane_context(
        conn,
        task_id="rev9-close-grade-planner",
        merge_queue_id="mq-rev9-close-grade-planner",
        project_root=project_root,
    )

    merge_a = _close_grade_lane_merge_event(
        conn,
        context=lane_a,
        commit_sha=_CLOSE_GRADE_LANE_A_COMMIT,
    )
    reconcile_event = None
    runtime_scope = {
        "project_id": _CLOSE_GRADE_PROJECT_ID,
        "backlog_id": _CLOSE_GRADE_BACKLOG_ID,
        "contract_execution_id": (
            reconcile_scope_execution_id or _CLOSE_GRADE_EXECUTION_ID
        ),
        "runtime_context_id": lane_b.runtime_context_id,
        "task_id": lane_b.task_id,
        "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
        "merge_queue_id": lane_b.merge_queue_id,
        "source": "parallel_branch_runtime_context",
        "server_derived": True,
    }

    def _record_reconcile_event():
        return task_timeline.record_event(
            conn,
            project_id=_CLOSE_GRADE_PROJECT_ID,
            backlog_id=_CLOSE_GRADE_BACKLOG_ID,
            task_id=lane_b.task_id,
            event_type="graph.reconcile",
            event_kind="reconcile",
            phase="reconcile",
            actor="observer:rev9-close-grade",
            status="passed",
            commit_sha=_CLOSE_GRADE_LANE_B_COMMIT,
            payload={
                "actor_role": "observer",
                "backlog_id": _CLOSE_GRADE_BACKLOG_ID,
                "contract_execution_id": _CLOSE_GRADE_EXECUTION_ID,
                "runtime_context_id": lane_b.runtime_context_id,
                "task_id": lane_b.task_id,
                "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
                "merge_queue_id": lane_b.merge_queue_id,
                "runtime_context_scope": runtime_scope,
                "current_full_reconcile": True,
                "reconcile_mode": "current_full",
            },
        )

    # A reconcile recorded before the final lane merge is durably out of
    # order; it is written first so its timeline id proves that.
    if record_reconcile_event and reconcile_before_final_lane_merge:
        reconcile_event = _record_reconcile_event()
    merge_b = _close_grade_lane_merge_event(
        conn,
        context=lane_b,
        commit_sha=_CLOSE_GRADE_LANE_B_COMMIT,
    )
    if record_reconcile_event and not reconcile_before_final_lane_merge:
        reconcile_event = _record_reconcile_event()

    event_times = [
        (merge_a, "2026-08-06T16:00:27Z"),
        (merge_b, "2026-08-06T16:00:29Z"),
    ]
    if reconcile_event is not None:
        event_times.append(
            (
                reconcile_event,
                "2026-08-06T16:00:28Z"
                if reconcile_before_final_lane_merge
                else "2026-08-06T16:01:26Z",
            )
        )
    for event, created_at in event_times:
        conn.execute(
            "UPDATE task_timeline_events SET created_at = ? WHERE id = ?",
            (created_at, int(event["id"])),
        )
        event["created_at"] = created_at
    conn.commit()

    snapshot = graph_snapshot_store.create_graph_snapshot(
        conn,
        _CLOSE_GRADE_PROJECT_ID,
        snapshot_id="full-rev9-close-grade-head",
        commit_sha=_CLOSE_GRADE_LANE_B_COMMIT,
        snapshot_kind="full",
        graph_json=_close_grade_graph_payload(),
    )
    graph_snapshot_store.index_graph_snapshot(
        conn,
        _CLOSE_GRADE_PROJECT_ID,
        snapshot["snapshot_id"],
        nodes=_close_grade_graph_payload()["deps_graph"]["nodes"],
        edges=_close_grade_graph_payload()["deps_graph"]["edges"],
    )
    graph_snapshot_store.activate_graph_snapshot(
        conn,
        _CLOSE_GRADE_PROJECT_ID,
        snapshot["snapshot_id"],
    )
    conn.commit()
    if reconcile_event is not None:
        graph_snapshot_store.record_current_full_reconcile_provenance(
            conn,
            project_id=_CLOSE_GRADE_PROJECT_ID,
            snapshot_id=snapshot["snapshot_id"],
            target_commit_sha=_CLOSE_GRADE_LANE_B_COMMIT,
            request_id=f"req-{_CLOSE_GRADE_EXECUTION_ID}",
            request_started_at=str(merge_b["created_at"]),
            route_evidence={
                "schema_version": (
                    "graph_current_full_reconcile.route_evidence.v1"
                ),
                "authenticated_role": "observer",
                "authentication_source": "test_protected_entrypoint",
                "raw_route_token_persisted": False,
                "protected_action": "graph_current_full_reconcile",
                "contract_execution_id": (
                    reconcile_scope_execution_id or _CLOSE_GRADE_EXECUTION_ID
                ),
                "runtime_context_id": lane_b.runtime_context_id,
                "task_id": lane_b.task_id,
                "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
                "merge_queue_id": lane_b.merge_queue_id,
                "route_token_scope": {
                    "project_id": _CLOSE_GRADE_PROJECT_ID,
                    "backlog_id": _CLOSE_GRADE_BACKLOG_ID,
                    "task_id": lane_b.task_id,
                    "runtime_context_id": lane_b.runtime_context_id,
                },
                "runtime_context_scope": runtime_scope,
            },
            runtime_context_scope=runtime_scope,
            reconcile_event_id=int(reconcile_event["id"]),
            reconcile_event_created_at=str(reconcile_event["created_at"]),
        )
        conn.commit()

    workers = [
        {
            "runtime_context_id": lane.runtime_context_id,
            "task_id": lane.task_id,
            "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
            "worker_id": lane.worker_id,
            "worker_slot_id": lane.worker_slot_id,
            "merge_queue_id": lane.merge_queue_id,
        }
        for lane in (lane_a, lane_b)
    ]
    dispatch_source_ref = (
        f"contract_runtime:{_CLOSE_GRADE_EXECUTION_ID}:completed_lines:0"
    )
    completed_lines = [
        {
            "stage_id": "dispatch",
            "line_id": "observer_dispatch_bounded_workers",
            "actor_role": "observer",
            "evidence_kind": "dispatch_bounded_worker",
            "payload": {
                "worker_count": 2,
                "required_worker_count": 2,
                "atomic_dispatch": True,
                "bounded_workers": workers,
            },
        }
    ]
    for lane, commit_sha in (
        (lane_a, _CLOSE_GRADE_LANE_A_COMMIT),
        (lane_b, _CLOSE_GRADE_LANE_B_COMMIT),
    ):
        completed_lines.extend(
            _close_grade_worker_lines(context=lane, commit_sha=commit_sha)
        )
    for lane, merge_event, commit_sha, before in (
        (lane_a, merge_a, _CLOSE_GRADE_LANE_A_COMMIT, _CLOSE_GRADE_BASE_COMMIT),
        (
            lane_b,
            merge_b,
            _CLOSE_GRADE_LANE_B_COMMIT,
            _CLOSE_GRADE_LANE_A_COMMIT,
        ),
    ):
        completed_lines.append(
            _close_grade_durable_merge_line(
                context=lane,
                merge_event=merge_event,
                commit_sha=commit_sha,
                target_head_before_merge=before,
                dispatch_source_ref=dispatch_source_ref,
            )
        )
    record = {
        "project_id": _CLOSE_GRADE_PROJECT_ID,
        "backlog_id": _CLOSE_GRADE_BACKLOG_ID,
        "contract_id": "mf_parallel.v2",
        "version": "v2",
        "revision": "rev9",
        "contract_execution_id": _CLOSE_GRADE_EXECUTION_ID,
        "completed_lines": completed_lines,
    }

    # The observer_reconcile line carries exactly what the server records at
    # reconcile time: the record-grade receipt it derives from durable state.
    reconcile_receipt = server._contract_runtime_reconcile_record_authority(
        conn,
        project_id=_CLOSE_GRADE_PROJECT_ID,
        record=record,
    )
    completed_lines.append(
        {
            "stage_id": "observer_reconcile",
            "line_id": "observer_reconcile",
            "actor_role": "observer",
            "evidence_kind": "reconcile",
            "line_instance_id": f"runtime_context:{lane_b.runtime_context_id}",
            "runtime_context_id": lane_b.runtime_context_id,
            "task_id": lane_b.task_id,
            "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
            "commit_sha": _CLOSE_GRADE_LANE_B_COMMIT,
            "payload": {"reconcile_authority": reconcile_receipt},
        }
    )
    completed_lines.append(
        {
            "stage_id": "qa_graph_context",
            "line_id": "qa_graph_context",
            "actor_role": "qa",
            "evidence_kind": "graph_trace",
            "line_instance_id": f"runtime_context:{lane_b.runtime_context_id}",
            "runtime_context_id": lane_b.runtime_context_id,
            "task_id": lane_b.task_id,
            "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
            "payload": {
                "graph_trace_evidence": {
                    "db_verified": True,
                    "identity_mismatches": [],
                    "verified_trace_ids": ["gqt-rev9-close-grade"],
                    "trace_ids": ["gqt-rev9-close-grade"],
                }
            },
        }
    )
    completed_lines.append(
        {
            "stage_id": "qa",
            "line_id": "qa_independent_verification",
            "actor_role": "qa",
            "evidence_kind": "independent_verification",
            "status": "pass",
            "line_instance_id": f"runtime_context:{lane_b.runtime_context_id}",
            "runtime_context_id": lane_b.runtime_context_id,
            "task_id": lane_b.task_id,
            "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
            "commit_sha": _CLOSE_GRADE_LANE_B_COMMIT,
            "payload": {
                "verdict": "pass",
                "candidate_new_failures": 0,
                "verified_commit": _CLOSE_GRADE_LANE_B_COMMIT,
            },
        }
    )
    close_ready_write = {
        "stage_id": "observer_close",
        "line_id": "observer_close_ready",
        "actor_role": "observer",
        "evidence_kind": "close_ready",
        "status": "pass",
        "commit_sha": _CLOSE_GRADE_LANE_B_COMMIT,
        "runtime_context_id": lane_b.runtime_context_id,
        "task_id": _CLOSE_GRADE_EXECUTION_ID,
        "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
        "payload": {
            "close_commit": _CLOSE_GRADE_LANE_B_COMMIT,
            "verdict": "pass",
            "runtime_context_id": lane_b.runtime_context_id,
            "worker_task_id": lane_b.task_id,
            "parent_task_id": _CLOSE_GRADE_EXECUTION_ID,
        },
    }
    return SimpleNamespace(
        conn=conn,
        record=record,
        reconcile_receipt=reconcile_receipt,
        close_ready_write=close_ready_write,
        lane_a=lane_a,
        lane_b=lane_b,
        merge_a=merge_a,
        merge_b=merge_b,
        reconcile_event=reconcile_event,
    )


def _close_grade_reconcile_diagnostic(world):
    bound = server._contract_runtime_bind_close_reconcile_authority(
        world.conn,
        project_id=_CLOSE_GRADE_PROJECT_ID,
        record=world.record,
    )
    reconcile_line = next(
        line
        for line in bound["completed_lines"]
        if str(line.get("line_id") or "") == "observer_reconcile"
    )
    return bound, server._contract_runtime_mf_parallel_reconcile_close_diagnostic(
        bound,
        reconcile_line,
    )


def _close_grade_close_ready_gate(world):
    return server._contract_runtime_mf_parallel_close_ready_precheck(
        world.record,
        world.close_ready_write,
        conn=world.conn,
        project_id=_CLOSE_GRADE_PROJECT_ID,
    )


def _reconcile_requirement_ids(gate):
    return [
        requirement_id
        for requirement_id in gate.get("missing_requirement_ids") or []
        if str(requirement_id).startswith("contract_runtime.reconcile_")
    ]


def test_rev9_close_grade_reconcile_authority_binds_after_postmerge_qa_pass(
    tmp_path,
    monkeypatch,
):
    """Regression: observer_close_ready was unsatisfiable on every rev9 world.

    rev8/rev9 merge both lanes before canonical reconcile and run the
    integration QA afterwards, so their merge lines carry the server-owned
    pre-QA marker.  The close binder used to read only
    ``_contract_runtime_trusted_merge_projection``, which cannot rebuild that
    shape, so close-grade authority derived nothing while the record-grade
    receipt on the same execution was fully bound.  Before the fix this test
    fails with the full ``contract_runtime.reconcile_*`` family that
    ``contract_runtime_close_authority_incomplete`` reports.
    """

    world = _rev9_close_grade_world(tmp_path, monkeypatch)

    # Record-grade authority is derived, not fed: it is what the server
    # persists on observer_reconcile.
    receipt = world.reconcile_receipt
    assert receipt["record_verified"] is True
    assert receipt["merge_projection_verified"] is True
    assert receipt["all_lane_merges_verified"] is True
    assert receipt["reconcile_event_recorded"] is True
    assert receipt["reconcile_event_id"] == int(world.reconcile_event["id"])
    assert receipt["reconcile_source_ref"] == (
        f"timeline:{world.reconcile_event['id']}"
    )
    assert receipt["merge_event_id"] == int(world.merge_b["id"])
    assert receipt["current_full_reconcile_activation_verified"] is True
    assert receipt["close_grade_authority_deferred"] is True

    _bound, diagnostic = _close_grade_reconcile_diagnostic(world)
    assert diagnostic["missing_requirement_ids"] == []
    assert diagnostic["passed"] is True
    assert diagnostic["reconcile_event_id"] == int(
        world.reconcile_event["id"]
    )
    assert diagnostic["next_action"] == "continue_close"

    gate = _close_grade_close_ready_gate(world)
    assert _reconcile_requirement_ids(gate) == []
    assert gate["missing_requirement_ids"] == []
    assert gate["passed"] is True


def test_rev9_close_grade_reconcile_authority_fails_closed_without_reconcile_event(
    tmp_path,
    monkeypatch,
):
    """No durable reconcile event means no close-grade authority."""

    world = _rev9_close_grade_world(
        tmp_path,
        monkeypatch,
        record_reconcile_event=False,
    )
    assert world.reconcile_receipt["record_verified"] is True
    assert world.reconcile_receipt["reconcile_event_recorded"] is False

    _bound, diagnostic = _close_grade_reconcile_diagnostic(world)
    assert diagnostic["passed"] is False
    assert "contract_runtime.reconcile_event_missing" in (
        diagnostic["missing_requirement_ids"]
    )

    gate = _close_grade_close_ready_gate(world)
    assert gate["passed"] is False
    assert gate["zero_write_rejection"] is True
    assert "contract_runtime.reconcile_event_missing" in (
        gate["missing_requirement_ids"]
    )


def test_rev9_close_grade_reconcile_authority_fails_closed_before_final_lane_merge(
    tmp_path,
    monkeypatch,
):
    """A reconcile that predates the final lane merge is never close-grade."""

    world = _rev9_close_grade_world(
        tmp_path,
        monkeypatch,
        reconcile_before_final_lane_merge=True,
    )
    assert int(world.reconcile_event["id"]) < int(world.merge_b["id"])
    assert world.reconcile_receipt["record_verified"] is True
    assert world.reconcile_receipt["reconcile_event_recorded"] is False

    _bound, diagnostic = _close_grade_reconcile_diagnostic(world)
    assert diagnostic["passed"] is False
    assert "contract_runtime.reconcile_event_missing" in (
        diagnostic["missing_requirement_ids"]
    )
    assert "contract_runtime.reconcile_durable_order" in (
        diagnostic["missing_requirement_ids"]
    )

    gate = _close_grade_close_ready_gate(world)
    assert gate["passed"] is False
    assert gate["zero_write_rejection"] is True
    assert "contract_runtime.reconcile_durable_order" in (
        gate["missing_requirement_ids"]
    )


def test_rev9_close_grade_reconcile_authority_fails_closed_on_foreign_execution(
    tmp_path,
    monkeypatch,
):
    """Reconcile provenance scoped to another execution stays unusable."""

    world = _rev9_close_grade_world(
        tmp_path,
        monkeypatch,
        reconcile_scope_execution_id="cex-mf-parallel-some-other-execution",
    )

    _bound, diagnostic = _close_grade_reconcile_diagnostic(world)
    assert diagnostic["passed"] is False
    assert "contract_runtime.reconcile_contract_execution_scope" in (
        diagnostic["missing_requirement_ids"]
    )
    assert "contract_runtime.reconcile_provenance_scope" in (
        diagnostic["missing_requirement_ids"]
    )

    gate = _close_grade_close_ready_gate(world)
    assert gate["passed"] is False
    assert gate["zero_write_rejection"] is True
    assert "contract_runtime.reconcile_contract_execution_scope" in (
        gate["missing_requirement_ids"]
    )


def test_rev9_standalone_world_never_gains_shared_batch_reconcile_authority(
    tmp_path,
    monkeypatch,
):
    """Standalone rev9 stays outside the shared batch projection.

    The pre-QA child lane-merge resolver exists so the batch projection can
    see a rev8/rev9 lane at all; it never grants authority on its own.  A real
    standalone rev9 world has no batch id, integration epoch or shared merge
    queue, so the batch projection must keep declining it with and without
    post-merge QA admission.
    """

    world = _rev9_close_grade_world(tmp_path, monkeypatch)
    merge = server._contract_runtime_rev8_two_worker_merge_projection(
        world.record,
        required_worker_count=2,
        conn=world.conn,
        project_id=_CLOSE_GRADE_PROJECT_ID,
    )
    assert merge["pre_qa_merge_authorized"] is True
    assert merge["qa_contract_runtime_verified"] is False
    assert str(getattr(world.lane_b, "batch_id", "") or "") == ""

    for allow_postmerge_qa_admission in (False, True):
        assert (
            server._contract_runtime_shared_batch_reconcile_authority(
                world.conn,
                project_id=_CLOSE_GRADE_PROJECT_ID,
                record=world.record,
                context=world.lane_b,
                merge=merge,
                allow_postmerge_qa_admission=allow_postmerge_qa_admission,
            )
            == {}
        )


_QA_STATUS_FLIP_EXECUTION_ID = "cex-qa-status-silent-flip"


def _qa_status_flip_record() -> dict:
    return {
        "contract_execution_id": _QA_STATUS_FLIP_EXECUTION_ID,
        "completed_lines": [],
        "execution_state": {
            "execution_state_revision": 4,
            "execution_state_hash": "sha256:qa-status-silent-flip-state",
        },
        "runtime_guide": {
            "runtime_guide_hash": "sha256:qa-status-silent-flip-guide"
        },
    }


def _qa_pass_verification_without_top_level_status() -> dict:
    """Reproduce the line shape that was accepted and then inverted.

    This is the 2026-08-07 batch-world submission: a genuine independent QA
    PASS carrying ``verdict`` plus a full nested verification, but no
    top-level ``status`` field.
    """

    return {
        "contract_execution_id": _QA_STATUS_FLIP_EXECUTION_ID,
        "stage_id": "qa_verification",
        "line_id": "qa_independent_verification",
        "actor_role": "qa",
        "evidence_kind": "independent_verification",
        "verdict": "PASS",
        "verification": {
            "verdict": "PASS",
            "independent": True,
            "summary": "Independent QA re-ran the affected suite; all green.",
        },
        "test_results": {"status": "passed", "passed": 12, "failed": 0},
        "payload": {"summary": "Independent QA verification passed."},
    }


def _qa_completion_status_gate(write: dict) -> dict:
    """Return the completion gate ContractRuntime derives for one QA line."""

    enriched = deepcopy(write)
    _enrich_qa_evidence_provenance(enriched, "qa")
    return dict(enriched["qa_evidence_provenance"]["completion_status_gate"])


def test_qa_pass_without_top_level_status_is_rejected_not_silently_failed():
    """A passing QA verification must never be inverted behind the author.

    ContractRuntime derives ``completion_status_gate`` from the top-level
    ``status`` field alone.  A verdict-PASS line that omits ``status`` was
    therefore accepted -- decision allow, no missing_proof_fields, state
    revision advanced -- and then normalized into a *failing* verdict, which
    drove the world into the failed-QA rework loop with no error, no warning
    and no remediation hint.  The server must now fail loudly instead, with a
    zero-write rejection naming the exact missing field.
    """

    record = _qa_status_flip_record()
    write = _qa_pass_verification_without_top_level_status()

    observed_gate = _qa_completion_status_gate(write)
    assert observed_gate["top_level_status_present"] is False
    assert observed_gate["top_level_status_passing"] is False
    assert observed_gate["normalized_status"] == ""
    assert observed_gate["nested_payload_decision_satisfies"] is False

    # Deliberate getattr: on the pre-fix server this guard does not exist, and
    # the assertion below must report the observed silent inversion rather
    # than dying on an AttributeError.
    guard = getattr(
        server,
        "_contract_runtime_qa_missing_status_rejection",
        None,
    )
    rejection = (
        guard(record, write, actor_role="qa") if guard is not None else {}
    )
    assert rejection, (
        "qa_independent_verification carrying verdict PASS with no top-level "
        "status was accepted; ContractRuntime then silently normalized the "
        f"passing verification into a failing one: {observed_gate}"
    )

    assert rejection["ok"] is False
    assert rejection["decision"]["ok"] is False
    assert rejection["missing_proof_fields"] == ["status"]
    assert rejection["nested_passing_verdict_field"] == "verdict"
    assert rejection["nested_passing_verdict_value"] == "pass"
    assert rejection["silent_failing_normalization_prevented"] is True
    assert rejection["completed_line_mutated"] is False
    assert rejection["zero_contract_runtime_write"] is True
    assert 'status: "passed"' in rejection["remediation"]
    errors = rejection["decision"]["errors"]
    assert errors and "top-level status" in errors[0]

    # Zero-write: the rejection reports the record unchanged.
    assert rejection["completed_lines_count"] == 0
    assert rejection["execution_state_revision"] == 4

    # The nested verdict is honoured wherever it is recorded, not only at the
    # top level, so the same trap cannot be re-entered through verification.
    nested_only = _qa_pass_verification_without_top_level_status()
    nested_only.pop("verdict")
    nested_rejection = guard(record, nested_only, actor_role="qa")
    assert nested_rejection
    assert nested_rejection["nested_passing_verdict_field"] == (
        "verification.verdict"
    )


def test_qa_verification_that_genuinely_fails_still_fails():
    """The loud-failure guard must not become a way to pass failing QA."""

    guard = server._contract_runtime_qa_missing_status_rejection
    record = _qa_status_flip_record()

    failing = _qa_pass_verification_without_top_level_status()
    failing["verdict"] = "FAIL"
    failing["verification"] = {
        "verdict": "FAIL",
        "independent": True,
        "summary": "Independent QA found 3 regressions.",
    }
    failing["test_results"] = {"status": "failed", "passed": 9, "failed": 3}
    failing["payload"] = {
        "summary": "Independent QA failed the worker commit."
    }

    # No nested passing verdict, so the guard stays out of the way and the
    # line keeps failing exactly as it did before.
    assert guard(record, failing, actor_role="qa") == {}
    failing_gate = _qa_completion_status_gate(failing)
    assert failing_gate["top_level_status_passing"] is False
    assert not _line_status_allows_contract_completion(
        {**failing, "status": "failed"},
        source_record={
            "contract_execution_id": _QA_STATUS_FLIP_EXECUTION_ID,
            "completed_lines": [{**failing, "status": "failed"}],
        },
        source_line_index=0,
    )

    # An explicitly failing status is likewise untouched.
    explicit_failed = {**failing, "status": "failed"}
    assert guard(record, explicit_failed, actor_role="qa") == {}

    # A correctly formed passing line is never blocked.
    passing = _qa_pass_verification_without_top_level_status()
    passing["status"] = "passed"
    assert guard(record, passing, actor_role="qa") == {}
    passing_gate = _qa_completion_status_gate(passing)
    assert passing_gate["top_level_status_present"] is True
    assert passing_gate["top_level_status_passing"] is True

    # Lines that are not independent QA verification stay out of scope.
    other_line = _qa_pass_verification_without_top_level_status()
    other_line["line_id"] = "worker_implementation"
    other_line["evidence_kind"] = "implementation"
    other_line["actor_role"] = "mf_sub"
    assert guard(record, other_line, actor_role="mf_sub") == {}

    other_role = _qa_pass_verification_without_top_level_status()
    assert guard(record, other_role, actor_role="observer") == {}
def test_dev_contract_runtime_uses_canonical_keys_in_physically_separate_database(monkeypatch):
    from agent.governance.contracts import runtime

    monkeypatch.setattr(runtime, "dev_runtime_verify_only", lambda: True)
    namespace = "sha256:" + "1" * 64
    assert runtime.direct_main_dev_storage_project_id("aming-claw", namespace) == "aming-claw"
    assert (
        runtime.direct_main_dev_storage_contract_id(namespace)
        == "operator_supervised_direct_main"
    )
