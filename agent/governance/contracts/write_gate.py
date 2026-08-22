"""Authoritative line-level write gate for contract executions."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .hash import stable_sha256
from .schema import ContractDefinitionError, find_line


_GRAPH_CONTEXT_POLICIES = {
    "direct_fix_observer_graph_scope": {
        "actor_role": "observer",
        "query_sources": {"observer"},
        "query_purposes": {
            "observer_scope_build",
            "observer_scope_validation",
            "graph_scope_before_dispatch",
        },
        "required_identity_fields": ("target_project_root",),
    },
    "direct_fix_worker_graph_context": {
        "actor_role": "mf_sub",
        "query_sources": {"mf_subagent"},
        "query_purposes": {
            "subagent_context_build",
            "subagent_gate_validation",
            "subagent_scope_validation",
        },
        "required_identity_fields": (
            "runtime_context_id",
            "task_id",
            "parent_task_id",
            "target_project_root",
        ),
        "worker_role": "mf_sub",
    },
    "worker_graph_context": {
        "actor_role": "mf_sub",
        "query_sources": {"mf_subagent"},
        "query_purposes": {
            "subagent_context_build",
            "subagent_gate_validation",
            "subagent_scope_validation",
        },
        "required_identity_fields": (
            "runtime_context_id",
            "task_id",
            "parent_task_id",
            "target_project_root",
        ),
        "worker_role": "mf_sub",
    },
    "direct_fix_qa_graph_context": {
        "actor_role": "qa",
        "query_sources": {"qa"},
        "query_purposes": {
            "qa_context_build",
            "qa_gate_validation",
            "independent_verification",
        },
        "required_identity_fields": ("target_project_root",),
    },
    "qa_graph_context": {
        "actor_role": "qa",
        "query_sources": {"qa"},
        "query_purposes": {
            "qa_context_build",
            "qa_gate_validation",
            "independent_verification",
        },
        "required_identity_fields": ("target_project_root",),
    },
}

_GRAPH_TRACE_ID_KEYS = {
    "graph_trace_id",
    "graph_trace_ids",
    "trace_id",
    "trace_ids",
    "verified_trace_ids",
}

_BOUNDED_QA_POLICY_NAME = "bounded_qa_review_policy"
_CANDIDATE_COMMIT_POLICY_NAME = "candidate_commit_evidence_policy"
_CURRENT_FULL_RECONCILE_POLICY_NAME = "current_full_reconcile_evidence_policy"
_QA_GRAPH_BASIS_DECISION_SCHEMA_VERSION = "qa_review_graph.basis_decision.v1"


@dataclass(frozen=True)
class WriteGateDecision:
    ok: bool
    errors: tuple[str, ...] = ()
    identity_mismatches: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "contract_write_gate_decision.v1",
            "ok": self.ok,
            "errors": list(self.errors),
            "identity_mismatches": [
                dict(item) for item in self.identity_mismatches
            ],
        }


def bounded_qa_graph_decision_errors(
    policy: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    graph_basis: str,
    line_id: str = "qa_graph_context",
) -> list[str]:
    """Validate one persisted bounded-QA basis decision against pinned policy."""

    errors: list[str] = []
    decision = evidence.get("graph_basis_decision")
    decision_hash = str(
        evidence.get("graph_basis_decision_hash") or ""
    ).strip().lower()
    decision_required = policy.get("graph_basis_decision_required") is True
    if not isinstance(decision, Mapping):
        if decision_required:
            errors.append(f"{line_id} requires server-derived graph_basis_decision")
        return errors

    expected_decision_source = str(
        policy.get("graph_basis_decision_source")
        or "server_bounded_qa_graph_basis"
    ).strip()
    expected_default = str(
        policy.get("default_graph_basis")
        or "canonical_base_plus_candidate_diff"
    ).strip()
    if str(decision.get("schema_version") or "").strip() != (
        _QA_GRAPH_BASIS_DECISION_SCHEMA_VERSION
    ):
        errors.append(f"{line_id} requires a supported graph_basis_decision schema")
    if str(decision.get("decision_source") or "").strip() != expected_decision_source:
        errors.append(
            f"{line_id} requires graph_basis_decision.decision_source="
            f"{expected_decision_source}"
        )
    if str(decision.get("default_graph_basis") or "").strip() != expected_default:
        errors.append(f"{line_id} requires default graph basis {expected_default}")
    if str(decision.get("selected_graph_basis") or "").strip() != graph_basis:
        errors.append(f"{line_id} graph_basis_decision must select graph_basis")
    if str(decision.get("overlay_failure_policy") or "").strip() != "fail_closed":
        errors.append(f"{line_id} requires fail-closed overlay failures")
    if (
        str(decision.get("one_hop_dependency_failure_policy") or "").strip()
        != "fail_closed"
    ):
        errors.append(f"{line_id} requires fail-closed one-hop dependency failures")
    if stable_sha256(decision) != decision_hash:
        errors.append(
            f"{line_id} graph_basis_decision_hash must hash the exact decision"
        )

    if graph_basis == "exact_candidate_snapshot":
        allowed_triggers = {
            str(item)
            for item in policy.get("exact_candidate_upgrade_triggers") or ()
            if str(item)
        }
        trigger = str(
            decision.get("exact_candidate_upgrade_trigger") or ""
        ).strip()
        if allowed_triggers and trigger not in allowed_triggers:
            errors.append(
                f"{line_id} exact candidate basis requires an allowed upgrade trigger"
            )
        if (
            trigger != "qa_explicit_exact_snapshot_request"
            and policy.get("server_trigger_escalation_ref_required") is True
            and not str(
                decision.get("exact_candidate_upgrade_ref") or ""
            ).strip()
        ):
            errors.append(
                f"{line_id} server-classified exact basis requires escalation ref"
            )
    elif graph_basis == "canonical_base_plus_candidate_diff":
        if str(decision.get("selection_reason") or "").strip() != (
            "bounded_source_backed_overlay_safe"
        ):
            errors.append(
                f"{line_id} base-diff basis requires the safe default decision"
            )
        if str(decision.get("canonical_head_relation") or "").strip() not in {
            "base",
            "candidate",
        }:
            errors.append(
                f"{line_id} base-diff basis requires canonical HEAD at base or candidate"
            )
    return errors


def validate_contract_write(
    definition: Mapping[str, Any],
    execution_state: Mapping[str, Any],
    write: Mapping[str, Any],
    *,
    runtime_guide: Mapping[str, Any] | None = None,
    require_next_action: bool = True,
) -> WriteGateDecision:
    """Validate a proposed evidence/line write against pinned contract state."""

    errors: list[str] = []
    identity_mismatches: list[dict[str, Any]] = []
    _expect_equal(errors, write, execution_state, "project_id")
    _expect_equal(errors, write, execution_state, "backlog_id")
    _expect_equal(errors, write, execution_state, "contract_execution_id")
    _expect_equal(errors, write, execution_state, "definition_hash")
    _expect_equal(errors, write, execution_state, "instruction_bundle_hash")
    _expect_equal(errors, write, execution_state, "execution_state_revision")

    if runtime_guide is not None:
        _expect_runtime_guide_hash(
            errors,
            identity_mismatches,
            write,
            runtime_guide,
        )

    stage_id = str(write.get("stage_id") or "")
    line_id = str(write.get("line_id") or "")
    actor_role = str(write.get("actor_role") or "")
    if not stage_id:
        errors.append("missing stage_id")
    if not line_id:
        errors.append("missing line_id")
    if not actor_role:
        errors.append("missing actor_role")

    line: dict[str, Any] | None = None
    if stage_id and line_id:
        try:
            line = find_line(definition, stage_id=stage_id, line_id=line_id)
        except ContractDefinitionError as exc:
            errors.append(str(exc))
    if line is not None:
        if actor_role:
            allowed = set(str(item) for item in line.get("allowed_writer_roles") or [])
            if actor_role not in allowed:
                errors.append(
                    f"actor_role {actor_role!r} cannot write line {line_id!r}; "
                    f"allowed_writer_roles={sorted(allowed)!r}"
                )
        expected_evidence_kind = str(line.get("evidence_kind") or "")
        write_evidence_kind = str(write.get("evidence_kind") or "")
        if expected_evidence_kind:
            if "evidence_kind" not in write or not write_evidence_kind:
                errors.append("missing evidence_kind")
            elif write_evidence_kind != expected_evidence_kind:
                errors.append("evidence_kind mismatch")

    _validate_worker_receipt_hash_evidence(errors, write)
    errors.extend(
        mf_parallel_prefill_child_plan_errors(
            definition,
            execution_state,
            write,
        )
    )
    errors.extend(
        operator_supervised_direct_main_runtime_binding_errors(
            definition,
            execution_state,
            write,
        )
    )

    candidate_commit_policy = contract_line_evidence_policy(
        definition,
        _CANDIDATE_COMMIT_POLICY_NAME,
        line_id=line_id,
    )
    if candidate_commit_policy:
        _validate_candidate_commit_evidence(
            errors,
            write,
            line_id=line_id,
            policy=candidate_commit_policy,
        )

    next_action = execution_state.get("next_action")
    if require_next_action and isinstance(next_action, Mapping):
        expected_stage = str(next_action.get("stage_id") or "")
        expected_line = str(next_action.get("line_id") or "")
        if stage_id != expected_stage or line_id != expected_line:
            errors.append(
                "write does not match next legal action "
                f"{expected_stage!r}/{expected_line!r}"
            )
            if stage_id != expected_stage:
                identity_mismatches.append(
                    {
                        "field": "stage_id",
                        "expected": expected_stage,
                        "actual": stage_id or "<missing>",
                    }
                )
            if line_id != expected_line:
                identity_mismatches.append(
                    {
                        "field": "line_id",
                        "expected": expected_line,
                        "actual": line_id or "<missing>",
                    }
                )
        _validate_next_action_instance(
            errors,
            identity_mismatches,
            write,
            next_action,
        )
    elif require_next_action and next_action is None:
        errors.append("contract execution has no remaining next legal action")
        atomic_lane_binding = execution_state.get("atomic_lane_gate_binding")
        if (
            isinstance(atomic_lane_binding, Mapping)
            and atomic_lane_binding.get("bound") is True
        ):
            identity_mismatches.extend(
                [
                    {
                        "field": "stage_id",
                        "expected": "<no remaining lane stage>",
                        "actual": stage_id or "<missing>",
                    },
                    {
                        "field": "line_id",
                        "expected": "<no remaining lane line>",
                        "actual": line_id or "<missing>",
                    },
                ]
            )

    bounded_qa_policy = contract_line_evidence_policy(
        definition,
        _BOUNDED_QA_POLICY_NAME,
        line_id=line_id,
    )
    _validate_graph_context(
        errors,
        identity_mismatches,
        write,
        execution_state=execution_state,
        line_id=line_id,
        actor_role=actor_role,
        bounded_qa_policy=bounded_qa_policy,
    )
    reconcile_policy = contract_line_evidence_policy(
        definition,
        _CURRENT_FULL_RECONCILE_POLICY_NAME,
        line_id=line_id,
    )
    strict_direct_runtime = bool(
        str(definition.get("contract_id") or "").strip()
        == "operator_supervised_direct_main"
        and str(definition.get("revision") or "").strip() == "rev2"
        and isinstance(execution_state.get("metadata"), Mapping)
        and isinstance(
            execution_state["metadata"].get(
                "operator_supervised_direct_main_runtime_binding"
            ),
            Mapping,
        )
        and execution_state["metadata"][
            "operator_supervised_direct_main_runtime_binding"
        ].get("strict_runtime_binding_required")
        is True
    )
    if reconcile_policy and not strict_direct_runtime:
        _validate_reconcile_record_evidence(
            errors,
            write,
            execution_state=execution_state,
            line_id=line_id,
            policy=reconcile_policy,
        )

    return WriteGateDecision(
        ok=not errors,
        errors=tuple(errors),
        identity_mismatches=tuple(identity_mismatches),
    )


def operator_supervised_direct_main_runtime_binding_errors(
    definition: Mapping[str, Any],
    execution_state: Mapping[str, Any],
    write: Mapping[str, Any],
) -> list[str]:
    """Keep every fresh Direct rev2 line bound to its admitted route Fact.

    The HTTP/timeline adapters may enrich a request with DB-derived evidence,
    but the authoritative ContractRuntime Gate still rechecks the immutable
    execution binding.  Historical Direct records have no strict marker and
    are intentionally not retrofitted.
    """

    if not (
        str(definition.get("contract_id") or "").strip()
        == "operator_supervised_direct_main"
        and str(definition.get("revision") or "").strip() == "rev2"
    ):
        return []
    metadata = (
        execution_state.get("metadata")
        if isinstance(execution_state.get("metadata"), Mapping)
        else {}
    )
    binding = (
        metadata.get("operator_supervised_direct_main_runtime_binding")
        if isinstance(
            metadata.get("operator_supervised_direct_main_runtime_binding"),
            Mapping,
        )
        else {}
    )
    if binding.get("strict_runtime_binding_required") is not True:
        return [
            "operator_supervised_direct_main rev2 requires the server-admitted immutable runtime binding"
        ]

    binding_hash = str(binding.get("binding_hash") or "").strip()
    unsigned_binding = {
        key: value for key, value in binding.items() if key != "binding_hash"
    }
    required_route_fields = (
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "visible_injection_manifest_hash",
        "route_token_ref",
    )
    route_identity = (
        binding.get("route_identity")
        if isinstance(binding.get("route_identity"), Mapping)
        else {}
    )
    expected_files = sorted(
        {
            str(path).strip()
            for path in binding.get("owned_files") or []
            if str(path).strip()
        }
    )
    errors: list[str] = []
    if not (
        binding.get("schema_version")
        == "operator_supervised_direct_main.runtime_binding.v1"
        and binding.get("server_derived") is True
        and binding.get("caller_claims_trusted") is False
        and str(binding.get("project_id") or "").strip()
        == str(execution_state.get("project_id") or "").strip()
        and str(binding.get("backlog_id") or "").strip()
        == str(execution_state.get("backlog_id") or "").strip()
        and str(binding.get("contract_execution_id") or "").strip()
        == str(execution_state.get("contract_execution_id") or "").strip()
        and expected_files
        and expected_files
        == sorted(
            {
                str(path).strip()
                for path in binding.get("target_files") or []
                if str(path).strip()
            }
        )
        and all(str(route_identity.get(field) or "").strip() for field in required_route_fields)
        and binding_hash
        and binding_hash == stable_sha256(unsigned_binding)
    ):
        errors.append(
            "operator_supervised_direct_main rev2 requires one complete immutable runtime binding"
        )

    payload = write.get("payload") if isinstance(write.get("payload"), Mapping) else {}
    if str(payload.get("direct_runtime_binding_hash") or "").strip() != binding_hash:
        errors.append(
            "operator_supervised_direct_main line must reference the exact runtime binding hash"
        )

    line_id = str(write.get("line_id") or "").strip()
    if line_id == "observer_bind_direct_scope":
        supplied = (
            payload.get("direct_runtime_binding")
            if isinstance(payload.get("direct_runtime_binding"), Mapping)
            else {}
        )
        if dict(supplied) != dict(binding):
            errors.append(
                "operator_supervised_direct_main route line must equal the immutable runtime binding"
            )
    elif line_id == "observer_graph_context":
        graph_gate = (
            payload.get("pre_implementation_graph_trace_gate")
            if isinstance(
                payload.get("pre_implementation_graph_trace_gate"), Mapping
            )
            else {}
        )
        graph_db = (
            graph_gate.get("db_evidence")
            if isinstance(graph_gate.get("db_evidence"), Mapping)
            else {}
        )
        trace_ids = [
            str(item).strip()
            for item in payload.get("graph_trace_ids") or []
            if str(item).strip()
        ]
        if not (
            graph_gate.get("passed") is True
            and graph_db.get("db_verified") is True
            and trace_ids
            and trace_ids
            == [
                str(item).strip()
                for item in graph_db.get("verified_trace_ids") or []
                if str(item).strip()
            ]
        ):
            errors.append(
                "operator_supervised_direct_main graph line requires exact DB-verified pre-mutation traces"
            )
    elif line_id == "observer_direct_implementation_exception":
        request_fingerprint = str(
            payload.get("pre_mutation_request_fingerprint") or ""
        ).strip()
        if not (
            request_fingerprint.startswith("sha256:")
            and payload.get("server_admitted_single_pre_mutation") is True
            and payload.get("same_generation_retry_allowed") is False
        ):
            errors.append(
                "operator_supervised_direct_main pre-mutation line requires the single-admission request fingerprint"
            )
    elif line_id == "observer_reconcile":
        authority = (
            payload.get("direct_current_full_reconcile_authority")
            if isinstance(
                payload.get("direct_current_full_reconcile_authority"),
                Mapping,
            )
            else {}
        )
        authority_hash = str(authority.get("authority_hash") or "").strip()
        unsigned_authority = {
            key: value
            for key, value in authority.items()
            if key != "authority_hash"
        }
        implementation_commits = {
            str(line.get("commit_sha") or "").strip().lower()
            for line in execution_state.get("completed_lines") or []
            if isinstance(line, Mapping)
            and str(line.get("line_id") or "").strip()
            == "observer_implementation"
            and str(line.get("commit_sha") or "").strip()
        }
        commit_fields = {
            str(authority.get(field) or "").strip().lower()
            for field in (
                "target_commit_sha",
                "canonical_head_commit",
                "active_snapshot_commit",
            )
        }
        route_evidence = (
            authority.get("route_evidence")
            if isinstance(authority.get("route_evidence"), Mapping)
            else {}
        )
        marker = (
            authority.get("current_full_reconcile_marker")
            if isinstance(
                authority.get("current_full_reconcile_marker"), Mapping
            )
            else {}
        )
        if not (
            authority.get("schema_version")
            == (
                "operator_supervised_direct_main."
                "current_full_reconcile_authority.v1"
            )
            and authority.get("server_derived") is True
            and authority.get("db_verified") is True
            and authority.get("canonical_head_verified") is True
            and authority.get("active_snapshot_verified") is True
            and authority.get("current_full_reconcile") is True
            and authority.get("qa_before_reconcile_verified") is True
            and str(authority.get("project_id") or "").strip()
            == str(execution_state.get("project_id") or "").strip()
            and str(authority.get("backlog_id") or "").strip()
            == str(execution_state.get("backlog_id") or "").strip()
            and str(authority.get("contract_execution_id") or "").strip()
            == str(execution_state.get("contract_execution_id") or "").strip()
            and len(implementation_commits) == 1
            and commit_fields == implementation_commits
            and str(authority.get("active_snapshot_id") or "").strip()
            and int(authority.get("qa_event_id") or 0) > 0
            and int(authority.get("qa_event_id") or 0)
            < int(authority.get("reconcile_event_id") or 0)
            and str(authority.get("qa_event_ref") or "").strip()
            == f"timeline:{int(authority.get('qa_event_id') or 0)}"
            and str(authority.get("qa_snapshot_commit") or "")
            .strip()
            .lower()
            in implementation_commits
            and str(authority.get("qa_snapshot_id") or "").strip()
            and str(authority.get("qa_authority_hash") or "").strip()
            and route_evidence.get("schema_version")
            == "graph_current_full_reconcile.route_evidence.v1"
            and route_evidence.get("protected_action")
            == "graph_current_full_reconcile"
            and route_evidence.get("raw_route_token_persisted") is False
            and str(route_evidence.get("route_token_ref") or "").strip()
            == str(route_identity.get("route_token_ref") or "").strip()
            and marker.get("schema_version")
            == "current_full_reconcile.provenance.v2"
            and marker.get("protected_action")
            == "graph_current_full_reconcile"
            and marker.get("activate") is True
            and marker.get("normal_update_path") is True
            and str(marker.get("snapshot_id") or "").strip()
            == str(authority.get("active_snapshot_id") or "").strip()
            and authority_hash
            and authority_hash == stable_sha256(unsigned_authority)
        ):
            errors.append(
                "operator_supervised_direct_main reconcile line requires exact current-full provenance"
            )
    elif line_id == "observer_close_ready":
        close_status = str(write.get("status") or "").strip().lower()
        terminal_policy = (
            definition.get("system_layer", {}).get(
                "terminal_outcome_policy", {}
            )
            if isinstance(definition.get("system_layer"), Mapping)
            else {}
        )
        authoritative_close_failure = str(
            terminal_policy.get("authoritative_close_failure") or ""
        ).strip()
        if close_status in {
            "blocked",
            "fail",
            "failed",
            "failure",
            "rejected",
        }:
            if not authoritative_close_failure.startswith(
                "terminal_no_pass"
            ):
                errors.append(
                    "operator_supervised_direct_main failed close requires the pinned terminal no-PASS policy"
                )
            return errors
        deployment = (
            payload.get("runtime_deployment_authority")
            if isinstance(
                payload.get("runtime_deployment_authority"), Mapping
            )
            else {}
        )
        deployment_hash = str(
            deployment.get("authority_hash") or ""
        ).strip()
        unsigned_deployment = {
            key: value
            for key, value in deployment.items()
            if key != "authority_hash"
        }
        applicable = deployment.get("applicable")
        if not (
            deployment.get("schema_version")
            == (
                "operator_supervised_direct_main."
                "runtime_deployment_authority.v1"
            )
            and applicable in {True, False}
            and (
                deployment.get("passed") is True
                if applicable is True
                else str(deployment.get("status") or "").strip()
                == "not_applicable"
                and "passed" not in deployment
            )
            and str(deployment.get("project_id") or "").strip()
            == str(execution_state.get("project_id") or "").strip()
            and deployment_hash
            and deployment_hash == stable_sha256(unsigned_deployment)
        ):
            errors.append(
                "operator_supervised_direct_main close line requires exact runtime deployment authority"
            )
    return errors


def mf_parallel_prefill_child_plan_errors(
    definition: Mapping[str, Any],
    execution_state: Mapping[str, Any],
    write: Mapping[str, Any],
) -> list[str]:
    """Enforce the fresh rev10 prefill Fact at the authoritative runtime Gate."""

    if not (
        str(definition.get("contract_id") or "").strip() == "mf_parallel.v2"
        and str(definition.get("revision") or "").strip() == "rev10"
        and str(write.get("line_id") or "").strip()
        in {
            "observer_prefill_child_contracts",
            "observer_dispatch_bounded_workers",
        }
    ):
        return []
    metadata = (
        execution_state.get("metadata")
        if isinstance(execution_state.get("metadata"), Mapping)
        else {}
    )
    # The marker distinguishes fresh post-fix executions from immutable
    # historical rev10 records; old executions are not retroactively migrated.
    if metadata.get("observer_prefill_child_plan_required") is not True:
        return []
    plan = (
        metadata.get("observer_prefill_child_plan")
        if isinstance(metadata.get("observer_prefill_child_plan"), Mapping)
        else {}
    )
    selection = (
        metadata.get("observer_worker_cardinality_selection")
        if isinstance(
            metadata.get("observer_worker_cardinality_selection"), Mapping
        )
        else {}
    )
    lanes = plan.get("lanes") if isinstance(plan.get("lanes"), list) else []
    try:
        required_count = int(selection.get("required_worker_count") or 0)
    except (TypeError, ValueError):
        required_count = 0

    def concrete(value: Any) -> bool:
        text = str(value or "").strip()
        return bool(text and "<" not in text and ">" not in text)

    lane_mappings = [lane for lane in lanes if isinstance(lane, Mapping)]
    lane_files = [
        str(path).strip()
        for lane in lane_mappings
        for path in lane.get("owned_files") or []
        if str(path).strip()
    ]
    expected_files = sorted(
        {
            str(path).strip()
            for field in ("owned_files", "target_files")
            for path in metadata.get(field) or []
            if str(path).strip()
        }
    )
    identity_sets = [
        [str(lane.get(field) or "").strip() for lane in lane_mappings]
        for field in ("task_id", "worker_id", "worker_slot_id")
    ]
    command_authority = (
        plan.get("test_command_authority")
        if isinstance(plan.get("test_command_authority"), Mapping)
        else {}
    )
    command_authority_hash = str(
        command_authority.get("authority_hash") or ""
    ).strip()
    command_authority_unsigned = {
        key: value
        for key, value in command_authority.items()
        if key != "authority_hash"
    }
    test_commands = [
        str(item).strip()
        for item in command_authority.get("test_commands") or []
        if str(item).strip()
    ]
    route_binding = (
        plan.get("parent_route_binding")
        if isinstance(plan.get("parent_route_binding"), Mapping)
        else {}
    )
    route_binding_hash = str(route_binding.get("binding_hash") or "").strip()
    route_binding_unsigned = {
        key: value
        for key, value in route_binding.items()
        if key != "binding_hash"
    }
    route_identity = (
        route_binding.get("route_identity")
        if isinstance(route_binding.get("route_identity"), Mapping)
        else {}
    )
    route_id = str(route_identity.get("route_id") or "").strip()
    canonical_route_identity = bool(
        (
            route_id.startswith("route-")
            or route_id
            in {
                "event.route_prompt_context.preview",
                "event.route_action.pre_mutation",
            }
        )
        and str(route_identity.get("prompt_contract_id") or "")
        .strip()
        .startswith("rprompt-")
        and str(route_identity.get("route_token_ref") or "")
        .strip()
        .startswith("rtok-")
        and all(
            str(route_identity.get(field) or "").strip().startswith("sha256:")
            for field in (
                "route_context_hash",
                "prompt_contract_hash",
                "visible_injection_manifest_hash",
            )
        )
    )
    selection_hash = str(selection.get("selection_hash") or "").strip()
    selection_unsigned = {
        key: value
        for key, value in selection.items()
        if key != "selection_hash"
    }
    batch_child_authority = (
        selection.get("batch_child_authority")
        if isinstance(selection.get("batch_child_authority"), Mapping)
        else {}
    )
    batch_authority_hash = str(
        batch_child_authority.get("authority_hash") or ""
    ).strip()
    batch_authority_unsigned = {
        key: value
        for key, value in batch_child_authority.items()
        if key != "authority_hash"
    }
    authoritative_batch_lane_intents = (
        batch_child_authority.get("lane_intents")
        if isinstance(batch_child_authority.get("lane_intents"), list)
        else []
    )
    authoritative_batch_lane_intent = (
        authoritative_batch_lane_intents[0]
        if len(authoritative_batch_lane_intents) == 1
        and isinstance(authoritative_batch_lane_intents[0], Mapping)
        else {}
    )
    batch_lane_intent_matches_plan = bool(
        batch_child_authority.get("lane_intents_authority_present") is True
        and len(lane_mappings) == 1
        and authoritative_batch_lane_intent
        and str(batch_child_authority.get("lane_intents_hash") or "").strip()
        == stable_sha256(authoritative_batch_lane_intents)
        and all(
            str(lane_mappings[0].get(field) or "").strip()
            == str(authoritative_batch_lane_intent.get(field) or "").strip()
            for field in ("task_id", "worker_id", "worker_slot_id")
        )
        and sorted(
            str(path).strip()
            for path in lane_mappings[0].get("owned_files") or []
            if str(path).strip()
        )
        == sorted(
            str(path).strip()
            for path in authoritative_batch_lane_intent.get("owned_files") or []
            if str(path).strip()
        )
    )
    standalone_cardinality = bool(
        required_count == 2
        and selection.get("batch_row_scoped_successor") is False
        and not batch_child_authority
        and selection.get("atomic_dispatch_required") is True
    )
    verified_batch_child_cardinality = bool(
        required_count == 1
        and selection.get("batch_row_scoped_successor") is True
        and batch_child_authority.get("schema_version")
        == "contract_runtime.mf_batch_child_worker_cardinality_authority.v1"
        and batch_child_authority.get("server_derived") is True
        and batch_child_authority.get("db_verified") is True
        and str(batch_child_authority.get("project_id") or "").strip()
        == str(execution_state.get("project_id") or "").strip()
        and str(batch_child_authority.get("child_backlog_id") or "").strip()
        == str(execution_state.get("backlog_id") or "").strip()
        and int(batch_child_authority.get("required_worker_count") or 0) == 1
        and batch_child_authority.get("worker_count_policy") == "exactly"
        and batch_child_authority.get("atomic_dispatch_required") is False
        and batch_child_authority.get(
            "standalone_mf_parallel_policy_unchanged"
        )
        is True
        and batch_lane_intent_matches_plan
        and batch_authority_hash
        == stable_sha256(batch_authority_unsigned)
    )
    required_route_fields = (
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "route_token_ref",
        "visible_injection_manifest_hash",
    )
    persisted_route_binding = (
        metadata.get("route_token_ref_binding")
        if isinstance(metadata.get("route_token_ref_binding"), Mapping)
        else {}
    )
    parent_route_refs = [
        value
        for value in (
            str(metadata.get("enter_only_route_token_ref") or "").strip(),
            str(metadata.get("parent_route_token_ref") or "").strip(),
            str(
                persisted_route_binding.get("parent_route_token_ref") or ""
            ).strip(),
            str(
                metadata.get("terminal_supersession_enter_route_token_ref")
                or ""
            ).strip(),
        )
        if value
    ]
    expected_route_token_ref = (
        parent_route_refs[0] if parent_route_refs else ""
    )
    persisted_parent_route_identity = (
        persisted_route_binding.get("parent_route_identity")
        if isinstance(
            persisted_route_binding.get("parent_route_identity"), Mapping
        )
        else (
            persisted_route_binding.get(
                "observer_route_context_issue_payload"
            )
            or {}
        ).get("parent_route_identity")
        if isinstance(
            (
                persisted_route_binding.get(
                    "observer_route_context_issue_payload"
                )
                or {}
            ).get("parent_route_identity"),
            Mapping,
        )
        else metadata.get("terminal_supersession_enter_route_identity")
        if isinstance(
            metadata.get("terminal_supersession_enter_route_identity"),
            Mapping,
        )
        else {}
    )
    unsigned_plan = {
        key: value for key, value in plan.items() if key != "plan_hash"
    }
    plan_complete = bool(
        plan.get("schema_version") == "mf_parallel.observer_prefill_child_plan.v1"
        and str(plan.get("source") or "").strip()
        in {
            "authenticated_observer_mf_parallel_enter",
            "contract_terminal_supersession_policy",
        }
        and plan.get("server_canonicalized") is True
        and str(plan.get("project_id") or "").strip()
        == str(execution_state.get("project_id") or "").strip()
        and str(plan.get("backlog_id") or "").strip()
        == str(execution_state.get("backlog_id") or "").strip()
        and str(plan.get("contract_execution_id") or "").strip()
        == str(execution_state.get("contract_execution_id") or "").strip()
        and selection.get("schema_version")
        == "mf_parallel.observer_worker_cardinality_selection.v1"
        and selection.get("observer_selected") is True
        and selection.get("selection_frozen") is True
        and selection.get("selection_frozen_at_enter") is True
        and selection.get("worker_count_policy") == "exactly"
        and selection_hash == stable_sha256(selection_unsigned)
        and (standalone_cardinality or verified_batch_child_cardinality)
        and int(plan.get("required_worker_count") or 0) == required_count
        and plan.get("worker_count_policy") == "exactly"
        and plan.get("atomic_dispatch_required") == (required_count == 2)
        and len(lanes) == required_count
        and len(lane_mappings) == required_count
        and all(
            values
            and all(concrete(value) for value in values)
            and len(values) == len(set(values))
            for values in identity_sets
        )
        and all(lane.get("owned_files") for lane in lane_mappings)
        and len(lane_files) == len(set(lane_files))
        and sorted(set(lane_files)) == expected_files
        and command_authority.get("schema_version")
        == "mf_parallel.project_test_command_authority.v1"
        and str(command_authority.get("project_id") or "").strip()
        == str(execution_state.get("project_id") or "").strip()
        and command_authority.get("source") == "project_config.testing"
        and test_commands
        and command_authority_hash == stable_sha256(command_authority_unsigned)
        and list(plan.get("test_commands") or []) == test_commands
        and all(
            list(lane.get("test_commands") or []) == test_commands
            and str(lane.get("test_command_authority_hash") or "").strip()
            == command_authority_hash
            for lane in lane_mappings
        )
        and route_binding.get("schema_version")
        == "mf_parallel.parent_route_binding.v1"
        and concrete(route_binding.get("route_token_ref"))
        and all(concrete(route_identity.get(field)) for field in required_route_fields)
        and canonical_route_identity
        and bool(parent_route_refs)
        and len(set(parent_route_refs)) == 1
        and str(route_binding.get("route_token_ref") or "").strip()
        == str(route_identity.get("route_token_ref") or "").strip()
        == expected_route_token_ref
        and all(
            str(route_identity.get(field) or "").strip()
            == str(persisted_parent_route_identity.get(field) or "").strip()
            for field in required_route_fields
        )
        and route_binding_hash == stable_sha256(route_binding_unsigned)
        and str(plan.get("plan_hash") or "").strip()
        == stable_sha256(unsigned_plan)
    )
    if not plan_complete:
        return [
            "mf_parallel rev10 prefill requires one complete server-admitted typed child plan"
        ]
    line_id = str(write.get("line_id") or "").strip()
    expected_payload = {
        "schema_version": "mf_parallel.observer_prefill_child_contracts.v1",
        "child_plan": dict(plan),
        "child_plan_hash": str(plan.get("plan_hash") or "").strip(),
    }
    if line_id == "observer_dispatch_bounded_workers":
        prefill_admitted = any(
            isinstance(line, Mapping)
            and str(line.get("stage_id") or "").strip() == "orchestration"
            and str(line.get("line_id") or "").strip()
            == "observer_prefill_child_contracts"
            and str(line.get("evidence_kind") or "").strip()
            == "contract_binding"
            and isinstance(line.get("payload"), Mapping)
            and stable_sha256(dict(line["payload"]))
            == stable_sha256(expected_payload)
            for line in execution_state.get("completed_lines") or []
        )
        if not prefill_admitted:
            return [
                "mf_parallel rev10 dispatch requires the accepted prefill child plan"
            ]
        dispatch_payload = (
            write.get("payload")
            if isinstance(write.get("payload"), Mapping)
            else write
        )
        workers = (
            dispatch_payload.get("bounded_workers")
            if isinstance(dispatch_payload.get("bounded_workers"), list)
            else []
        )
        worker_mappings = [
            worker for worker in workers if isinstance(worker, Mapping)
        ]

        def normalized_files(value: Any) -> list[str]:
            return sorted(
                {
                    str(item).strip()
                    for item in value or []
                    if str(item).strip()
                }
            )

        def normalized_values(value: Any) -> list[str]:
            return [
                str(item).strip()
                for item in value or []
                if str(item).strip()
            ]

        workers_by_task_id = {
            str(worker.get("task_id") or "").strip(): worker
            for worker in worker_mappings
            if str(worker.get("task_id") or "").strip()
        }
        lane_task_ids = {
            str(lane.get("task_id") or "").strip()
            for lane in lane_mappings
            if str(lane.get("task_id") or "").strip()
        }
        lane_custody_matches = bool(
            len(worker_mappings) == len(lane_mappings)
            and len(workers_by_task_id) == len(worker_mappings)
            and set(workers_by_task_id) == lane_task_ids
            and all(
                all(
                    str(worker.get(field) or "").strip()
                    == str(lane.get(field) or "").strip()
                    for field in ("task_id", "worker_id", "worker_slot_id")
                )
                and normalized_files(worker.get("owned_files"))
                == normalized_files(lane.get("owned_files"))
                and normalized_files(worker.get("test_files"))
                == normalized_files(lane.get("test_files"))
                and normalized_values(worker.get("test_commands"))
                == normalized_values(lane.get("test_commands"))
                for lane in lane_mappings
                for worker in [
                    workers_by_task_id[str(lane.get("task_id") or "").strip()]
                ]
            )
        )
        if not lane_custody_matches:
            return [
                "mf_parallel rev10 dispatch lane custody must equal the accepted prefill child plan"
            ]
        return []
    actual_payload = (
        dict(write.get("payload"))
        if isinstance(write.get("payload"), Mapping)
        else {}
    )
    if stable_sha256(actual_payload) != stable_sha256(expected_payload):
        return [
            "mf_parallel rev10 prefill child plan must equal the exact mf_parallel_enter projection"
        ]
    return []


def _validate_worker_receipt_hash_evidence(
    errors: list[str],
    write: Mapping[str, Any],
) -> None:
    """Reject missing or copied receipt hashes before runtime persistence."""

    def _valid(value: Any) -> bool:
        text = str(value or "").strip()
        return bool(
            text
            and "<" not in text
            and ">" not in text
            and text.startswith("sha256:")
            and len(text) > len("sha256:")
        )

    def _is_read_receipt_mapping(value: Mapping[str, Any]) -> bool:
        return any(
            "read_receipt" in str(value.get(key) or "").strip().lower()
            for key in (
                "evidence_kind",
                "event_kind",
                "canonical_event_kind",
                "event_type",
                "schema_version",
            )
        )

    read_receipt_write = _is_read_receipt_mapping(write)
    supplied_hash_field = False
    valid_hash_field = False

    def _walk(
        value: Any,
        path: str,
        *,
        read_receipt_context: bool = False,
    ) -> None:
        nonlocal read_receipt_write, supplied_hash_field, valid_hash_field
        if isinstance(value, Mapping):
            schema_version = str(value.get("schema_version") or "").strip()
            event_kind = str(
                value.get("event_kind")
                or value.get("canonical_event_kind")
                or ""
            ).strip()
            mapping_is_read_receipt = (
                read_receipt_context or _is_read_receipt_mapping(value)
            )
            read_receipt_write = read_receipt_write or mapping_is_read_receipt
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if key in {
                    "read_receipt_hash",
                    "worker_read_receipt_hash",
                    "launch_text_hash",
                }:
                    supplied_hash_field = True
                    if _valid(child):
                        valid_hash_field = True
                    else:
                        errors.append(
                            f"{child_path} requires a worker-computed "
                            "non-placeholder sha256: value"
                        )
                elif key == "receipt_hash" and (
                    mapping_is_read_receipt
                    or "read_receipt" in event_kind
                    or schema_version == "contract_context_read_receipt.v1"
                ):
                    supplied_hash_field = True
                    if _valid(child):
                        valid_hash_field = True
                    else:
                        errors.append(
                            f"{child_path} requires a worker-computed "
                            "non-placeholder sha256: value"
                        )
                _walk(
                    child,
                    child_path,
                    read_receipt_context=(
                        mapping_is_read_receipt
                        or key == "contract_context_read_receipt"
                    ),
                )
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                _walk(
                    child,
                    f"{path}[{index}]",
                    read_receipt_context=read_receipt_context,
                )

    _walk(write, "")
    if read_receipt_write and not valid_hash_field:
        field_state = "supplied but invalid" if supplied_hash_field else "missing"
        errors.append(
            "read_receipt evidence requires at least one worker-computed "
            f"non-placeholder sha256: value ({field_state})"
        )


def _validate_candidate_commit_evidence(
    errors: list[str],
    write: Mapping[str, Any],
    *,
    line_id: str,
    policy: Mapping[str, Any],
) -> None:
    path = str(policy.get("candidate_commit_path") or "commit_sha").strip()
    if path != "commit_sha":
        errors.append(f"{line_id} has unsupported candidate commit policy path")
        return
    candidate_commit = str(write.get(path) or "").strip().lower()
    if not _is_full_commit(candidate_commit):
        errors.append(f"{line_id} requires top-level full {path}")


def _validate_graph_context(
    errors: list[str],
    identity_mismatches: list[dict[str, Any]],
    write: Mapping[str, Any],
    *,
    execution_state: Mapping[str, Any],
    line_id: str,
    actor_role: str,
    bounded_qa_policy: Mapping[str, Any],
) -> None:
    policy = _GRAPH_CONTEXT_POLICIES.get(line_id)
    if not policy:
        return
    expected_actor = str(policy.get("actor_role") or "")
    if actor_role != expected_actor:
        errors.append(f"{line_id} requires actor_role={expected_actor}")

    trace_ids = _graph_trace_ids(write)
    if not trace_ids:
        errors.append(f"{line_id} requires non-empty graph_trace_ids")
    elif not all(_is_plausible_graph_trace_id(trace_id) for trace_id in trace_ids):
        errors.append(f"{line_id} contains invalid graph_trace_ids")

    db_verified = _graph_bool(write, "db_verified")
    if not db_verified:
        errors.append(f"{line_id} requires db_verified graph_trace_evidence")

    query_source = _graph_text(write, "query_source")
    allowed_sources = set(policy.get("query_sources") or [])
    if not query_source:
        errors.append(f"{line_id} requires graph query_source")
    elif query_source not in allowed_sources:
        errors.append(f"{line_id} query_source must be one of {sorted(allowed_sources)!r}")

    query_purpose = _graph_text(write, "query_purpose")
    allowed_purposes = set(policy.get("query_purposes") or [])
    if not query_purpose:
        errors.append(f"{line_id} requires graph query_purpose")
    elif query_purpose not in allowed_purposes:
        errors.append(f"{line_id} query_purpose must be one of {sorted(allowed_purposes)!r}")

    expected_worker_role = str(policy.get("worker_role") or "")
    if expected_worker_role:
        worker_role = _graph_text(write, "worker_role")
        if worker_role != expected_worker_role:
            errors.append(f"{line_id} requires worker_role={expected_worker_role}")

    for field in policy.get("required_identity_fields") or ():
        write_value = _write_field(write, field)
        graph_value = _graph_text(write, field)
        value = write_value or graph_value
        if not value:
            errors.append(f"{line_id} requires {field}")
            continue
        if write_value and graph_value and write_value != graph_value:
            errors.append(f"{line_id} {field} does not match graph_trace_evidence")
    if bounded_qa_policy:
        _validate_bounded_qa_graph_context(
            errors,
            identity_mismatches,
            write,
            execution_state=execution_state,
            line_id=line_id,
            policy=bounded_qa_policy,
        )


def _validate_bounded_qa_graph_context(
    errors: list[str],
    identity_mismatches: list[dict[str, Any]],
    write: Mapping[str, Any],
    *,
    execution_state: Mapping[str, Any],
    line_id: str,
    policy: Mapping[str, Any],
) -> None:
    evidence = _canonical_authority_object(write, policy)
    authority_source = str(policy.get("authority_source") or "graph_query_traces")
    if not evidence:
        errors.append(f"{line_id} requires one canonical server-derived evidence object")
        return
    if str(evidence.get("source") or "").strip() != authority_source:
        errors.append(f"{line_id} requires source={authority_source} evidence")
    if evidence.get("db_verified") is not True:
        errors.append(f"{line_id} requires server DB-verified graph trace evidence")

    trace_ids = _canonical_policy_trace_ids(write, policy)
    if not trace_ids:
        errors.append(f"{line_id} requires canonical graph trace lookup keys")

    verified_trace_ids = _flatten_graph_text_values(
        evidence.get("verified_trace_ids")
    )
    if not verified_trace_ids:
        errors.append(f"{line_id} requires verified_trace_ids")
    elif set(verified_trace_ids) != set(trace_ids):
        errors.append(
            f"{line_id} verified_trace_ids must match graph_trace_ids"
        )
    missing_trace_ids = _flatten_graph_text_values(
        evidence.get("missing_trace_ids")
    )
    if missing_trace_ids:
        errors.append(f"{line_id} cannot contain missing_trace_ids")
    persisted_identity_mismatches = evidence.get("identity_mismatches")
    if persisted_identity_mismatches:
        errors.append(f"{line_id} cannot contain graph trace identity_mismatches")

    query_source = str(evidence.get("query_source") or "").strip()
    query_sources = set(str(item) for item in policy.get("query_sources") or [])
    if query_sources and query_source not in query_sources:
        errors.append(f"{line_id} evidence query_source must be one of {sorted(query_sources)!r}")
    query_purpose = str(evidence.get("query_purpose") or "").strip()
    query_purposes = set(str(item) for item in policy.get("query_purposes") or [])
    if query_purposes and query_purpose not in query_purposes:
        errors.append(f"{line_id} evidence query_purpose must be one of {sorted(query_purposes)!r}")

    for field in policy.get("required_identity_fields") or ():
        value = str(evidence.get(field) or "").strip()
        if not value:
            errors.append(f"{line_id} requires authority.{field}")
    for field in ("project_id", "backlog_id", "contract_execution_id"):
        expected = str(execution_state.get(field) or "").strip()
        actual = str(evidence.get(field) or "").strip()
        if expected and actual and actual != expected:
            errors.append(f"{line_id} authority.{field} mismatch")
    write_task_id = _write_field(write, "task_id")
    authority_task_id = str(evidence.get("task_id") or "").strip()
    if write_task_id and authority_task_id and write_task_id != authority_task_id:
        errors.append(f"{line_id} authority.task_id mismatch")

    graph_basis = str(evidence.get("graph_basis") or "").strip()
    canonical_base_snapshot_id = str(
        evidence.get("canonical_base_snapshot_id") or ""
    ).strip()
    base_commit_sha = str(evidence.get("base_commit_sha") or "").strip().lower()
    candidate_commit_sha = str(
        evidence.get("candidate_commit_sha") or ""
    ).strip().lower()
    changed_files = evidence.get("changed_files")
    candidate_diff_hash = str(
        evidence.get("candidate_diff_hash") or ""
    ).strip().lower()
    changed_files_source = str(evidence.get("changed_files_source") or "").strip()
    comparison_base_commit_sha = str(
        evidence.get("comparison_base_commit_sha") or ""
    ).strip().lower()
    comparison_base_commit_source = str(
        evidence.get("comparison_base_commit_source") or ""
    ).strip()
    comparison_authority_required = bool(
        evidence.get("comparison_authority_required") is True
        or (
            isinstance(evidence.get("root_identity"), Mapping)
            and evidence["root_identity"].get(
                "comparison_authority_required"
            )
            is True
        )
    )

    errors.extend(
        bounded_qa_graph_decision_errors(
            policy,
            evidence,
            graph_basis=graph_basis,
            line_id=line_id,
        )
    )

    accepted_graph_basis = set(
        str(item) for item in policy.get("accepted_graph_basis") or []
    )
    if graph_basis not in accepted_graph_basis:
        errors.append(f"{line_id} requires a supported graph_basis")
    if not canonical_base_snapshot_id:
        errors.append(f"{line_id} requires canonical_base_snapshot_id")
    if not _is_full_commit(base_commit_sha):
        errors.append(f"{line_id} requires full base_commit_sha")
    if not _is_full_commit(candidate_commit_sha):
        errors.append(f"{line_id} requires full candidate_commit_sha")
    if not isinstance(changed_files, list):
        errors.append(f"{line_id} requires changed_files as a JSON list")
    if not _is_sha256(candidate_diff_hash):
        errors.append(f"{line_id} requires candidate_diff_hash")
    if not changed_files_source.startswith("server_"):
        errors.append(f"{line_id} requires server-derived changed_files_source")

    if graph_basis == "exact_candidate_snapshot":
        empty_diff_hash = "sha256:" + hashlib.sha256(b"").hexdigest()
        if base_commit_sha != candidate_commit_sha:
            errors.append(
                f"{line_id} exact candidate basis requires matching commits"
            )
        comparison_diff_source = (
            "server_runtime_context_base_to_exact_candidate_diff"
        )
        comparison_base_sources = {
            (
                "ContractRuntime.completed_lines.worker_commit+"
                "parallel_branch_runtime_context.base_commit"
            ),
            (
                "ContractRuntime.completed_lines.observer_merge+"
                "parallel_branch_merge_queue_items.target_head_before_merge"
            ),
        }
        comparison_authority_present = bool(
            comparison_authority_required
            or comparison_base_commit_sha
            or comparison_base_commit_source
            or changed_files_source == comparison_diff_source
        )
        if comparison_authority_present:
            if not comparison_base_commit_sha and comparison_authority_required:
                _append_identity_mismatch(
                    errors,
                    identity_mismatches,
                    message=(
                        f"{line_id} exact candidate comparison basis requires "
                        "comparison_base_commit_sha when "
                        "comparison_authority_required=true"
                    ),
                    field="comparison_base_commit_sha",
                    expected="server-derived full ancestor commit",
                    actual=comparison_base_commit_sha,
                )
            elif not _is_full_commit(comparison_base_commit_sha):
                _append_identity_mismatch(
                    errors,
                    identity_mismatches,
                    message=(
                        f"{line_id} exact candidate comparison basis requires full "
                        "comparison_base_commit_sha"
                    ),
                    field="comparison_base_commit_sha",
                    expected="full git object id",
                    actual=comparison_base_commit_sha,
                )
            elif comparison_base_commit_sha == candidate_commit_sha:
                _append_identity_mismatch(
                    errors,
                    identity_mismatches,
                    message=(
                        f"{line_id} exact candidate comparison basis requires "
                        "distinct comparison base and candidate commits"
                    ),
                    field="comparison_base_commit_sha",
                    expected="full git object id distinct from candidate_commit_sha",
                    actual=comparison_base_commit_sha,
                )
            if comparison_base_commit_source not in comparison_base_sources:
                _append_identity_mismatch(
                    errors,
                    identity_mismatches,
                    message=(
                        f"{line_id} exact candidate comparison basis requires "
                        "comparison_base_commit_source="
                        + "|".join(sorted(comparison_base_sources))
                    ),
                    field="comparison_base_commit_source",
                    expected=sorted(comparison_base_sources),
                    actual=comparison_base_commit_source,
                )
            if changed_files_source != comparison_diff_source:
                _append_identity_mismatch(
                    errors,
                    identity_mismatches,
                    message=(
                        f"{line_id} exact candidate comparison basis requires "
                        f"changed_files_source={comparison_diff_source}"
                    ),
                    field="changed_files_source",
                    expected=comparison_diff_source,
                    actual=changed_files_source,
                )
        else:
            if changed_files_source != "server_exact_candidate_snapshot":
                errors.append(
                    f"{line_id} exact candidate basis requires "
                    "changed_files_source=server_exact_candidate_snapshot"
                )
            if isinstance(changed_files, list) and changed_files:
                errors.append(
                    f"{line_id} exact candidate basis requires empty changed_files"
                )
            if candidate_diff_hash != empty_diff_hash:
                errors.append(
                    f"{line_id} exact candidate basis requires the empty diff hash"
                )
        for field in policy.get("exact_candidate_required_hash_fields") or ():
            value = str(evidence.get(field) or "").strip().lower()
            if not _is_sha256(value):
                errors.append(f"{line_id} requires {field}")
        return

    if graph_basis == "canonical_base_plus_candidate_diff":
        if base_commit_sha == candidate_commit_sha:
            errors.append(
                f"{line_id} base-diff basis requires distinct base and candidate commits"
            )
        for field in policy.get("base_diff_required_hash_fields") or ():
            value = str(evidence.get(field) or "").strip().lower()
            if not _is_sha256(value):
                errors.append(f"{line_id} requires {field}")


def _append_identity_mismatch(
    errors: list[str],
    identity_mismatches: list[dict[str, Any]],
    *,
    message: str,
    field: str,
    expected: Any,
    actual: Any,
) -> None:
    """Retain legacy text while exposing copy-safe comparison diagnostics."""

    errors.append(message)
    identity_mismatches.append(
        {
            "field": str(field or ""),
            "expected": expected,
            "actual": actual,
        }
    )


def _validate_current_full_reconcile_evidence(
    errors: list[str],
    write: Mapping[str, Any],
    *,
    execution_state: Mapping[str, Any],
    line_id: str,
    policy: Mapping[str, Any],
) -> None:
    authority = _canonical_authority_object(write, policy)
    if not authority:
        errors.append(f"{line_id} requires one canonical reconcile authority object")
        return
    expected_source = str(policy.get("authority_source") or "").strip()
    if expected_source and str(authority.get("source") or "").strip() != expected_source:
        errors.append(f"{line_id} requires source={expected_source} authority")
    for field in policy.get("required_verification_fields") or ():
        if authority.get(field) is not True:
            errors.append(f"{line_id} requires authority.{field}=true")
    if policy.get("current_full_reconcile_required") is True and authority.get(
        "current_full_reconcile"
    ) is not True:
        errors.append(f"{line_id} requires current_full_reconcile=true")
    expected_strategy = str(policy.get("strategy") or "").strip()
    if expected_strategy and str(authority.get("strategy") or "").strip() != expected_strategy:
        errors.append(f"{line_id} requires strategy={expected_strategy}")

    for field in policy.get("required_identity_fields") or ():
        if not str(authority.get(field) or "").strip():
            errors.append(f"{line_id} requires authority.{field}")
    for field in ("project_id", "backlog_id", "contract_execution_id"):
        expected = str(execution_state.get(field) or "").strip()
        actual = str(authority.get(field) or "").strip()
        if expected and actual and actual != expected:
            errors.append(f"{line_id} authority.{field} mismatch")
    for field in ("runtime_context_id", "task_id"):
        write_value = _write_field(write, field)
        authority_value = str(authority.get(field) or "").strip()
        if write_value and authority_value and write_value != authority_value:
            errors.append(f"{line_id} authority.{field} mismatch")

    commit_fields = {
        str(field): str(authority.get(field) or "").strip().lower()
        for field in policy.get("required_commit_fields") or ()
    }
    commits = list(commit_fields.values())
    if not commits or not all(_is_full_commit(value) for value in commits):
        errors.append(f"{line_id} requires full canonical reconcile commit identities")
    elif len(set(commits)) != 1 and not _verified_historical_reconcile_descendant(
        authority,
        commit_fields=commit_fields,
    ):
        errors.append(f"{line_id} reconcile commit identities must match")
    if not str(authority.get("active_snapshot_id") or "").strip():
        errors.append(f"{line_id} requires authority.active_snapshot_id")
    for field in policy.get("required_temporal_fields") or ():
        if not str(authority.get(field) or "").strip():
            errors.append(f"{line_id} requires authority.{field}")
    qa_alternatives = [
        dict(item)
        for item in policy.get("qa_authority_alternatives") or ()
        if isinstance(item, Mapping)
    ]
    if qa_alternatives:
        valid_qa_alternatives: list[str] = []
        for alternative in qa_alternatives:
            alternative_id = str(alternative.get("id") or "").strip()
            authority_mode = str(
                alternative.get("authority_mode") or alternative_id
            ).strip()
            valid = bool(
                alternative_id
                and str(authority.get("qa_authority_mode") or "").strip()
                == authority_mode
            )
            valid = valid and all(
                authority.get(field) is True
                for field in alternative.get("required_verification_fields")
                or ()
            )
            valid = valid and all(
                bool(str(authority.get(field) or "").strip())
                for field in (
                    list(alternative.get("required_identity_fields") or ())
                    + list(alternative.get("required_temporal_fields") or ())
                )
            )
            valid = valid and all(
                _int_value(authority.get(field)) > 0
                for field in alternative.get("required_positive_integer_fields")
                or ()
            )
            valid = valid and all(
                authority.get(field) in (None, "", 0, False, [])
                for field in alternative.get("forbidden_fields") or ()
            )
            if valid:
                valid_qa_alternatives.append(alternative_id)
        required_mode = str(
            policy.get("qa_authority_alternative_mode") or "exactly_one"
        ).strip()
        if required_mode == "exactly_one" and len(valid_qa_alternatives) != 1:
            errors.append(
                f"{line_id} requires exactly one verified QA authority alternative"
            )
    try:
        merge_event_id = int(authority.get("merge_event_id") or 0)
        reconcile_event_id = int(authority.get("reconcile_event_id") or 0)
    except (TypeError, ValueError):
        merge_event_id = 0
        reconcile_event_id = 0
    if merge_event_id <= 0 or reconcile_event_id <= merge_event_id:
        errors.append(f"{line_id} requires durable merge before reconcile order")
    marker = authority.get("current_full_reconcile_marker")
    route_evidence = (
        marker.get("route_evidence") if isinstance(marker, Mapping) else None
    )
    if not isinstance(marker, Mapping):
        errors.append(f"{line_id} requires current_full_reconcile_marker")
    elif not (
        str(marker.get("schema_version") or "").strip()
        == "current_full_reconcile.provenance.v2"
        and str(marker.get("protected_action") or "").strip()
        == "graph_current_full_reconcile"
        and str(marker.get("protected_entrypoint") or "").strip()
        == "POST /api/graph-governance/{project_id}/reconcile/current-full"
        and str(marker.get("provenance_id") or "").strip()
        and _is_sha256(str(marker.get("provenance_hash") or "").strip())
        and str(marker.get("request_id") or "").strip()
        and str(marker.get("request_started_at") or "").strip()
        and str(marker.get("marker_created_at") or "").strip()
        and marker.get("activate") is True
        and marker.get("normal_update_path") is True
        and str(marker.get("target_commit_sha") or "").strip().lower()
        == str(authority.get("merged_commit_sha") or "").strip().lower()
        and str(marker.get("snapshot_id") or "").strip()
        == str(
            authority.get("reconcile_snapshot_id")
            or authority.get("active_snapshot_id")
            or ""
        ).strip()
        and _int_value(marker.get("reconcile_event_id")) == reconcile_event_id
        and str(marker.get("reconcile_event_created_at") or "").strip()
        == str(authority.get("reconcile_event_created_at") or "").strip()
        and isinstance(route_evidence, Mapping)
        and route_evidence.get("schema_version")
        == "graph_current_full_reconcile.route_evidence.v1"
        and route_evidence.get("raw_route_token_persisted") is False
        and route_evidence.get("protected_action")
        == "graph_current_full_reconcile"
    ):
        errors.append(f"{line_id} current_full_reconcile_marker is not canonical")
    provenance = authority.get("current_full_reconcile_provenance")
    if not isinstance(provenance, Mapping):
        errors.append(f"{line_id} requires current_full_reconcile_provenance")
    elif isinstance(marker, Mapping) and not (
        str(provenance.get("provenance_id") or "").strip()
        == str(marker.get("provenance_id") or "").strip()
        and str(provenance.get("provenance_hash") or "").strip()
        == str(marker.get("provenance_hash") or "").strip()
        and str(provenance.get("protected_action") or "").strip()
        == "graph_current_full_reconcile"
        and str(provenance.get("protected_entrypoint") or "").strip()
        == "POST /api/graph-governance/{project_id}/reconcile/current-full"
        and str(provenance.get("request_id") or "").strip()
        == str(marker.get("request_id") or "").strip()
        and str(provenance.get("request_started_at") or "").strip()
        == str(marker.get("request_started_at") or "").strip()
        and str(provenance.get("marker_created_at") or "").strip()
        == str(marker.get("marker_created_at") or "").strip()
        and _int_value(provenance.get("reconcile_event_id"))
        == reconcile_event_id
        and str(provenance.get("reconcile_event_created_at") or "").strip()
        == str(authority.get("reconcile_event_created_at") or "").strip()
    ):
        errors.append(f"{line_id} current-full provenance does not match marker")


def _validate_reconcile_record_evidence(
    errors: list[str],
    write: Mapping[str, Any],
    *,
    execution_state: Mapping[str, Any],
    line_id: str,
    policy: Mapping[str, Any],
) -> None:
    """Validate the server-owned progress receipt for observer_reconcile.

    Full current-full provenance, active-snapshot, commit, and scope checks are
    close authority.  The line write only records that the observer reached
    the reconcile step after one durable merge; it must not duplicate the
    close gate.  Caller-shaped authority is removed before this validator is
    reached, so the compact receipt remains server owned.
    """

    authority = _canonical_authority_object(write, policy)
    if not authority:
        errors.append(f"{line_id} requires one canonical reconcile record receipt")
        return
    if str(authority.get("schema_version") or "").strip() != (
        "contract_runtime.observer_reconcile_record_authority.v1"
    ):
        errors.append(f"{line_id} requires canonical reconcile record receipt")
    if str(authority.get("source") or "").strip() != (
        "contract_runtime.server_reconcile_record_projection"
    ):
        errors.append(f"{line_id} requires server-derived reconcile record receipt")
    for field in (
        "server_derived",
        "record_verified",
        "merge_projection_verified",
        "dispatch_lineage_verified",
    ):
        if authority.get(field) is not True:
            errors.append(f"{line_id} requires authority.{field}=true")

    for field in (
        "project_id",
        "backlog_id",
        "contract_execution_id",
        "runtime_context_id",
        "task_id",
        "parent_task_id",
        "merge_queue_id",
        "merge_source_ref",
        "merge_event_created_at",
    ):
        if not str(authority.get(field) or "").strip():
            errors.append(f"{line_id} requires authority.{field}")
    for field in ("project_id", "backlog_id", "contract_execution_id"):
        expected = str(execution_state.get(field) or "").strip()
        actual = str(authority.get(field) or "").strip()
        if expected and actual and actual != expected:
            errors.append(f"{line_id} authority.{field} mismatch")
    for field in (
        "runtime_context_id",
        "task_id",
        "parent_task_id",
        "merge_queue_id",
    ):
        write_value = _write_field(write, field)
        authority_value = str(authority.get(field) or "").strip()
        if write_value and authority_value and write_value != authority_value:
            errors.append(f"{line_id} authority.{field} mismatch")

    merged_commit = str(authority.get("merged_commit_sha") or "").strip().lower()
    if not _is_full_commit(merged_commit):
        errors.append(f"{line_id} requires authority.merged_commit_sha")
    write_commit = _write_field(write, "commit_sha").lower()
    if write_commit and merged_commit and write_commit != merged_commit:
        errors.append(f"{line_id} merge commit mismatch")
    try:
        merge_event_id = int(authority.get("merge_event_id") or 0)
    except (TypeError, ValueError):
        merge_event_id = 0
    if merge_event_id <= 0:
        errors.append(f"{line_id} requires durable merge event identity")

    authority_hash = str(authority.get("authority_hash") or "").strip().lower()
    unsigned_authority = dict(authority)
    unsigned_authority.pop("authority_hash", None)
    if not (
        _is_sha256(authority_hash)
        and stable_sha256(unsigned_authority) == authority_hash
    ):
        errors.append(f"{line_id} reconcile record receipt hash mismatch")


def _verified_historical_reconcile_descendant(
    authority: Mapping[str, Any],
    *,
    commit_fields: Mapping[str, str],
) -> bool:
    """Accept a superseded reconcile only with complete live descendant proof."""

    historical_commit = str(commit_fields.get("merged_commit_sha") or "")
    reconciled_commit = str(commit_fields.get("reconciled_commit_sha") or "")
    reconcile_snapshot_commit = str(
        authority.get("reconcile_snapshot_commit") or ""
    ).strip().lower()
    canonical_head_commit = str(
        commit_fields.get("canonical_head_commit") or ""
    )
    active_snapshot_commit = str(
        commit_fields.get("active_snapshot_commit") or ""
    )
    reconcile_snapshot_id = str(
        authority.get("reconcile_snapshot_id") or ""
    ).strip()
    active_snapshot_id = str(authority.get("active_snapshot_id") or "").strip()
    authority_hash = str(authority.get("authority_hash") or "").strip().lower()
    unsigned_authority = dict(authority)
    unsigned_authority.pop("authority_hash", None)

    return bool(
        _is_full_commit(historical_commit)
        and historical_commit == reconciled_commit == reconcile_snapshot_commit
        and _is_full_commit(canonical_head_commit)
        and canonical_head_commit == active_snapshot_commit
        and historical_commit != canonical_head_commit
        and reconcile_snapshot_id
        and active_snapshot_id
        and reconcile_snapshot_id != active_snapshot_id
        and authority.get("reconcile_snapshot_verified") is True
        and authority.get("canonical_head_equals_merged_commit") is False
        and authority.get("reconciled_commit_is_ancestor_of_canonical_head") is True
        and authority.get("canonical_head_verified") is True
        and authority.get("active_snapshot_matches_canonical_head") is True
        and authority.get("active_snapshot_verified") is True
        and _is_sha256(authority_hash)
        and stable_sha256(unsigned_authority) == authority_hash
    )


def contract_line_evidence_policy(
    definition: Mapping[str, Any],
    policy_name: str,
    *,
    line_id: str,
) -> Mapping[str, Any]:
    """Return an enabled normalized source policy that applies to one line."""

    system_layer = _mapping(definition.get("system_layer"))
    graph_binding_policy = _mapping(system_layer.get("graph_binding_policy"))
    policy = _mapping(graph_binding_policy.get(policy_name))
    if policy.get("enabled") is not True:
        return {}
    line_ids = {str(item).strip() for item in policy.get("line_ids") or []}
    return policy if line_id in line_ids else {}


def _canonical_authority_object(
    write: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> Mapping[str, Any]:
    path = str(policy.get("authority_object_path") or "").strip()
    if not path.startswith("payload.") or path.count(".") != 1:
        return {}
    payload = _mapping(write.get("payload"))
    return _mapping(payload.get(path.split(".", 1)[1]))


def _canonical_policy_trace_ids(
    write: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> list[str]:
    values: list[str] = []
    payload = _mapping(write.get("payload"))
    for source in (write, payload):
        for key in policy.get("lookup_key_fields") or ():
            values.extend(_flatten_graph_text_values(source.get(key)))
    return list(dict.fromkeys(value for value in values if value))


def _is_full_commit(value: str) -> bool:
    return len(value) in {40, 64} and all(
        character in "0123456789abcdef" for character in value
    )


def _is_sha256(value: str) -> bool:
    return (
        value.startswith("sha256:")
        and len(value) == len("sha256:") + 64
        and all(
            character in "0123456789abcdef"
            for character in value.removeprefix("sha256:")
        )
    )


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _graph_trace_ids(write: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in _GRAPH_TRACE_ID_KEYS:
        values.extend(_flatten_graph_text_values(write.get(key)))
    for candidate in _graph_evidence_candidates(write):
        for key in _GRAPH_TRACE_ID_KEYS:
            values.extend(_flatten_graph_text_values(candidate.get(key)))
    return list(dict.fromkeys(value for value in values if value))


def _graph_text(write: Mapping[str, Any], key: str) -> str:
    value = _write_field(write, key)
    if value:
        return value
    for candidate in _graph_evidence_candidates(write):
        value = _first_deep_text(candidate, key)
        if value:
            return value
    return ""


def _graph_bool(write: Mapping[str, Any], key: str) -> bool:
    for candidate in _graph_evidence_candidates(write):
        value = _first_deep_value(candidate, key)
        if isinstance(value, bool):
            return value
        if str(value or "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


def _graph_evidence_candidates(write: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    candidates: list[Mapping[str, Any]] = []
    payload = write.get("payload") if isinstance(write.get("payload"), Mapping) else {}
    for source in (write, payload):
        for key in (
            "graph_trace_evidence",
            "graph_trace_db_evidence",
            "graph_trace",
            "graph_context",
        ):
            value = source.get(key)
            if isinstance(value, Mapping):
                candidates.append(value)
        if any(key in source for key in ("query_source", "query_purpose", "db_verified")):
            candidates.append(source)
    return candidates


def _flatten_graph_text_values(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        values: list[str] = []
        for child in value.values():
            values.extend(_flatten_graph_text_values(child))
        return values
    if isinstance(value, list):
        values = []
        for child in value:
            values.extend(_flatten_graph_text_values(child))
        return values
    text = str(value or "").strip()
    return [text] if text else []


def _first_deep_text(value: Any, key: str) -> str:
    item = _first_deep_value(value, key)
    return str(item or "").strip()


def _first_deep_value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            if str(raw_key or "") == key:
                return child
            found = _first_deep_value(child, key)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_deep_value(child, key)
            if found not in (None, ""):
                return found
    return None


def _is_plausible_graph_trace_id(value: str) -> bool:
    return value.startswith("gqt-") and len(value) >= 8


def _validate_next_action_instance(
    errors: list[str],
    identity_mismatches: list[dict[str, Any]],
    write: Mapping[str, Any],
    next_action: Mapping[str, Any],
) -> None:
    require_full_identity = next_action.get("atomic_lane_gate_bound") is True
    expected_instance = str(next_action.get("line_instance_id") or "")
    expected_runtime_context_id = str(next_action.get("runtime_context_id") or "")
    expected_task_id = str(next_action.get("task_id") or "")
    expected_parent_task_id = str(next_action.get("parent_task_id") or "")
    expected_lane_id = str(next_action.get("lane_id") or "")
    expected_worker_slot_id = str(next_action.get("worker_slot_id") or "")
    expected_worker_id = str(next_action.get("worker_id") or "")
    if not any(
        (
            expected_instance,
            expected_runtime_context_id,
            expected_task_id,
            expected_parent_task_id,
            expected_lane_id,
            expected_worker_slot_id,
            expected_worker_id,
        )
    ):
        return

    def expect_identity_values(
        *,
        field: str,
        expected: str,
        keys: tuple[str, ...],
        missing_error: str,
        mismatch_error: str,
        require_presence: bool = True,
    ) -> str:
        if not expected:
            return ""
        values = _write_field_values(write, *keys)
        if not values and require_presence:
            errors.append(missing_error)
            identity_mismatches.append(
                {"field": field, "expected": expected, "actual": "<missing>"}
            )
            return ""
        mismatches = [item for item in values if item[1] != expected]
        if mismatches:
            errors.append(mismatch_error)
            identity_mismatches.extend(
                {
                    "field": path,
                    "expected": expected,
                    "actual": actual,
                }
                for path, actual in mismatches
            )
        return values[0][1] if values else ""

    actual_runtime_context_id = expect_identity_values(
        field="runtime_context_id",
        expected=expected_runtime_context_id,
        keys=("runtime_context_id",),
        missing_error="missing runtime_context_id for next legal action",
        mismatch_error="runtime_context_id does not match next legal action",
    )

    actual_task_id = expect_identity_values(
        field="task_id",
        expected=expected_task_id,
        keys=("task_id",),
        missing_error="missing task_id for next legal action",
        mismatch_error="task_id does not match next legal action",
        require_presence=require_full_identity,
    )

    expect_identity_values(
        field="parent_task_id",
        expected=expected_parent_task_id,
        keys=("parent_task_id",),
        missing_error="missing parent_task_id for next legal action",
        mismatch_error="parent_task_id does not match next legal action",
        require_presence=require_full_identity,
    )

    if require_full_identity:
        actual_lane_id = expect_identity_values(
            field="lane_id",
            expected=expected_lane_id,
            keys=("lane_id",),
            missing_error="missing lane_id for next legal action",
            mismatch_error="lane_id does not match next legal action",
        )
        expect_identity_values(
            field="worker_slot_id",
            expected=expected_worker_slot_id,
            keys=("worker_slot_id",),
            missing_error="missing worker_slot_id for next legal action",
            mismatch_error="worker_slot_id does not match next legal action",
        )
        expect_identity_values(
            field="worker_id",
            expected=expected_worker_id,
            keys=("worker_id",),
            missing_error="missing worker_id for next legal action",
            mismatch_error="worker_id does not match next legal action",
        )
    else:
        actual_lane_id = _write_field(
            write,
            "lane_id",
            "worker_slot_id",
            "worker_id",
        )
        if expected_lane_id and actual_lane_id and actual_lane_id != expected_lane_id:
            errors.append("lane_id does not match next legal action")
            identity_mismatches.append(
                {
                    "field": "lane_id",
                    "expected": expected_lane_id,
                    "actual": actual_lane_id,
                }
            )
        actual_worker_slot_id = _write_field(write, "worker_slot_id", "worker_id")
        if (
            expected_worker_slot_id
            and actual_worker_slot_id
            and actual_worker_slot_id != expected_worker_slot_id
        ):
            errors.append("worker_slot_id does not match next legal action")
            identity_mismatches.append(
                {
                    "field": "worker_slot_id",
                    "expected": expected_worker_slot_id,
                    "actual": actual_worker_slot_id,
                }
            )

    actual_instance = expect_identity_values(
        field="line_instance_id",
        expected=expected_instance,
        keys=("line_instance_id", "instance_id"),
        missing_error="missing line_instance_id for next legal action",
        mismatch_error="line_instance_id does not match next legal action",
        require_presence=False,
    )
    if not actual_instance:
        actual_instance = _line_instance_id_from_values(
            runtime_context_id=actual_runtime_context_id,
            task_id=actual_task_id,
            lane_id=actual_lane_id,
        )
        if expected_instance and actual_instance != expected_instance:
            errors.append("line_instance_id does not match next legal action")
            identity_mismatches.append(
                {
                    "field": "line_instance_id",
                    "expected": expected_instance,
                    "actual": actual_instance or "<missing>",
                }
            )


def _write_field_values(
    write: Mapping[str, Any],
    *keys: str,
) -> list[tuple[str, str]]:
    payload = write.get("payload") if isinstance(write.get("payload"), Mapping) else {}
    values: list[tuple[str, str]] = []
    for prefix, source in (("", write), ("payload.", payload)):
        for key in keys:
            text = str(source.get(key) or "").strip()
            if text:
                values.append((f"{prefix}{key}", text))
    return values


def _write_field(write: Mapping[str, Any], *keys: str) -> str:
    payload = write.get("payload") if isinstance(write.get("payload"), Mapping) else {}
    for source in (write, payload):
        for key in keys:
            value = source.get(key)
            text = str(value or "").strip()
            if text:
                return text
    return ""


def _line_instance_id_from_values(
    *,
    runtime_context_id: str = "",
    task_id: str = "",
    lane_id: str = "",
) -> str:
    if runtime_context_id:
        return f"runtime_context:{runtime_context_id}"
    if task_id:
        return f"task:{task_id}"
    if lane_id:
        return f"lane:{lane_id}"
    return ""


def _expect_equal(
    errors: list[str],
    write: Mapping[str, Any],
    execution_state: Mapping[str, Any],
    field: str,
) -> None:
    _expect_value(errors, write, field, execution_state.get(field))


def _expect_value(
    errors: list[str],
    write: Mapping[str, Any],
    field: str,
    expected: Any,
) -> None:
    if field not in write:
        errors.append(f"missing {field}")
        return
    if write.get(field) != expected:
        errors.append(f"{field} mismatch")


def _expect_runtime_guide_hash(
    errors: list[str],
    identity_mismatches: list[dict[str, Any]],
    write: Mapping[str, Any],
    runtime_guide: Mapping[str, Any],
) -> None:
    field = "runtime_guide_hash"
    top_level_expected = runtime_guide.get(field)
    writer_expected = _writer_role_safe_copy_runtime_guide_hash(
        write,
        runtime_guide,
    )
    expected = writer_expected or top_level_expected
    if field not in write:
        errors.append(f"missing {field}")
        identity_mismatches.append(
            {
                "field": field,
                "expected": _public_safe_runtime_guide_hash(expected),
                "actual": _public_safe_runtime_guide_hash(None, missing=True),
            }
        )
        return
    actual = write.get(field)
    if actual == expected:
        return
    errors.append(
        _runtime_guide_hash_mismatch_message(
            write,
            runtime_guide,
            actual,
            expected,
            top_level_expected=top_level_expected,
            writer_expected=writer_expected,
        )
    )
    identity_mismatches.append(
        {
            "field": field,
            "expected": _public_safe_runtime_guide_hash(expected),
            "actual": _public_safe_runtime_guide_hash(actual),
        }
    )


def _writer_role_safe_copy_runtime_guide_hash(
    write: Mapping[str, Any],
    runtime_guide: Mapping[str, Any],
) -> str:
    """Resolve the server-issued hash for the exact writer/line tuple.

    A current guide may be rendered for an observer while its next line belongs
    to QA (or another role). In that case the top-level hash is the reader hash
    and the copy-safe payload is the write authority. Only an exact binding may
    select that writer hash; stale or cross-line copy payloads fail closed.
    """

    safe_copy = _mapping(runtime_guide.get("writer_role_safe_copy_payload"))
    submit_payload = _mapping(safe_copy.get("copy_payload"))
    safe_hash = str(submit_payload.get("runtime_guide_hash") or "").strip()
    if not safe_hash:
        return ""

    binding_fields = (
        "project_id",
        "backlog_id",
        "contract_execution_id",
        "definition_hash",
        "instruction_bundle_hash",
        "execution_state_revision",
        "stage_id",
        "line_id",
        "actor_role",
        "evidence_kind",
    )
    if any(
        field in submit_payload
        and submit_payload.get(field) != write.get(field)
        for field in binding_fields
    ):
        return ""

    alignment = _mapping(safe_copy.get("hash_alignment"))
    if alignment:
        required_role = str(
            alignment.get("required_writer_role")
            or alignment.get("required_owner_role")
            or ""
        ).strip()
        aligned_hash = str(
            alignment.get("required_writer_runtime_guide_hash") or ""
        ).strip()
        if required_role and required_role != str(
            write.get("actor_role") or ""
        ).strip():
            return ""
        if aligned_hash and aligned_hash != safe_hash:
            return ""
    return safe_hash


def _runtime_guide_hash_mismatch_message(
    write: Mapping[str, Any],
    runtime_guide: Mapping[str, Any],
    actual: Any,
    expected: Any,
    *,
    top_level_expected: Any = None,
    writer_expected: Any = None,
) -> str:
    copy_payload = _mapping(runtime_guide.get("writer_role_safe_copy_payload"))
    alignment = _mapping(copy_payload.get("hash_alignment"))
    submit_payload = _mapping(copy_payload.get("copy_payload"))
    actor_role = str(write.get("actor_role") or submit_payload.get("actor_role") or "")
    required_role = str(
        alignment.get("required_writer_role")
        or alignment.get("required_owner_role")
        or actor_role
    )
    required_hash = _public_safe_runtime_guide_hash(expected)
    actual_hash = _public_safe_runtime_guide_hash(actual)
    reader_role = _matching_reader_role(
        alignment,
        actual if isinstance(actual, str) else "",
        required_role,
    )
    if not reader_role:
        reader_role = str(alignment.get("reader_role") or "")
    reader_fragment = (
        f"received reader-role guide hash for role {reader_role!r} ({actual_hash})"
        if reader_role and actual_hash
        else f"received {actual_hash!r}, which is not the required owner/writer-role hash"
    )
    top_level_hash = str(top_level_expected or "")
    copy_safe_hash = str(writer_expected or "")
    authority_fragment = (
        f" The top-level reader hash is {top_level_hash}; the exact "
        f"copy-safe writer hash is {copy_safe_hash}."
        if copy_safe_hash and top_level_hash and copy_safe_hash != top_level_hash
        else ""
    )
    return (
        "runtime_guide_hash mismatch: "
        f"{reader_fragment}; submit_line requires owner/writer-role guide hash "
        f"for role {required_role!r} ({required_hash}). "
        "Recover by copying writer_role_safe_copy_payload.copy_payload.runtime_guide_hash "
        "or the full writer_role_safe_copy_payload.copy_payload from the current guide "
        "before calling contract_runtime_submit_line."
        f"{authority_fragment}"
    )


def _public_safe_runtime_guide_hash(
    value: Any,
    *,
    missing: bool = False,
) -> str:
    """Render one hash claim without echoing attacker-controlled containers."""

    if missing:
        return "<missing>"
    if isinstance(value, str) and _is_sha256(value):
        return value
    return "<invalid-runtime-guide-hash>"


def _matching_reader_role(
    alignment: Mapping[str, Any],
    actual_hash: str,
    required_role: str,
) -> str:
    for item in alignment.get("known_role_runtime_guide_hashes") or []:
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "")
        if role == required_role:
            continue
        if str(item.get("runtime_guide_hash") or "") == actual_hash:
            return role
    return ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
