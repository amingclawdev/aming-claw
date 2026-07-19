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

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "contract_write_gate_decision.v1",
            "ok": self.ok,
            "errors": list(self.errors),
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
    _expect_equal(errors, write, execution_state, "project_id")
    _expect_equal(errors, write, execution_state, "backlog_id")
    _expect_equal(errors, write, execution_state, "contract_execution_id")
    _expect_equal(errors, write, execution_state, "definition_hash")
    _expect_equal(errors, write, execution_state, "instruction_bundle_hash")
    _expect_equal(errors, write, execution_state, "execution_state_revision")

    if runtime_guide is not None:
        _expect_runtime_guide_hash(errors, write, runtime_guide)

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
        _validate_next_action_instance(errors, write, next_action)
    elif require_next_action and next_action is None:
        errors.append("contract execution has no remaining next legal action")

    bounded_qa_policy = contract_line_evidence_policy(
        definition,
        _BOUNDED_QA_POLICY_NAME,
        line_id=line_id,
    )
    _validate_graph_context(
        errors,
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
    if reconcile_policy:
        _validate_current_full_reconcile_evidence(
            errors,
            write,
            execution_state=execution_state,
            line_id=line_id,
            policy=reconcile_policy,
        )

    return WriteGateDecision(ok=not errors, errors=tuple(errors))


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
            write,
            execution_state=execution_state,
            line_id=line_id,
            policy=bounded_qa_policy,
        )


def _validate_bounded_qa_graph_context(
    errors: list[str],
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
    identity_mismatches = evidence.get("identity_mismatches")
    if identity_mismatches:
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

    commits = [
        str(authority.get(field) or "").strip().lower()
        for field in policy.get("required_commit_fields") or ()
    ]
    if not commits or not all(_is_full_commit(value) for value in commits):
        errors.append(f"{line_id} requires full canonical reconcile commit identities")
    elif len(set(commits)) != 1:
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
        == str(authority.get("active_snapshot_id") or "").strip()
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
    write: Mapping[str, Any],
    next_action: Mapping[str, Any],
) -> None:
    expected_instance = str(next_action.get("line_instance_id") or "")
    expected_runtime_context_id = str(next_action.get("runtime_context_id") or "")
    expected_task_id = str(next_action.get("task_id") or "")
    expected_lane_id = str(next_action.get("lane_id") or "")
    if not any(
        (expected_instance, expected_runtime_context_id, expected_task_id, expected_lane_id)
    ):
        return

    actual_runtime_context_id = _write_field(write, "runtime_context_id")
    if expected_runtime_context_id:
        if not actual_runtime_context_id:
            errors.append("missing runtime_context_id for next legal action")
        elif actual_runtime_context_id != expected_runtime_context_id:
            errors.append("runtime_context_id does not match next legal action")

    actual_task_id = _write_field(write, "task_id")
    if expected_task_id and actual_task_id and actual_task_id != expected_task_id:
        errors.append("task_id does not match next legal action")

    actual_lane_id = _write_field(write, "lane_id", "worker_slot_id", "worker_id")
    if expected_lane_id and actual_lane_id and actual_lane_id != expected_lane_id:
        errors.append("lane_id does not match next legal action")

    actual_instance = _write_field(write, "line_instance_id", "instance_id")
    if not actual_instance:
        actual_instance = _line_instance_id_from_values(
            runtime_context_id=actual_runtime_context_id,
            task_id=actual_task_id,
            lane_id=actual_lane_id,
        )
    if expected_instance and actual_instance and actual_instance != expected_instance:
        errors.append("line_instance_id does not match next legal action")
    elif expected_instance and not actual_instance:
        errors.append("missing line_instance_id for next legal action")


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
    write: Mapping[str, Any],
    runtime_guide: Mapping[str, Any],
) -> None:
    field = "runtime_guide_hash"
    expected = runtime_guide.get(field)
    if field not in write:
        errors.append(f"missing {field}")
        return
    actual = write.get(field)
    if actual == expected:
        return
    errors.append(_runtime_guide_hash_mismatch_message(write, runtime_guide, actual, expected))


def _runtime_guide_hash_mismatch_message(
    write: Mapping[str, Any],
    runtime_guide: Mapping[str, Any],
    actual: Any,
    expected: Any,
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
    required_hash = str(
        alignment.get("required_writer_runtime_guide_hash")
        or submit_payload.get("runtime_guide_hash")
        or expected
        or ""
    )
    actual_hash = str(actual or "")
    reader_role = _matching_reader_role(alignment, actual_hash, required_role)
    if not reader_role:
        reader_role = str(alignment.get("reader_role") or "")
    reader_fragment = (
        f"received reader-role guide hash for role {reader_role!r} ({actual_hash})"
        if reader_role and actual_hash
        else f"received {actual_hash!r}, which is not the required owner/writer-role hash"
    )
    return (
        "runtime_guide_hash mismatch: "
        f"{reader_fragment}; submit_line requires owner/writer-role guide hash "
        f"for role {required_role!r} ({required_hash}). "
        "Recover by copying writer_role_safe_copy_payload.copy_payload.runtime_guide_hash "
        "or the full writer_role_safe_copy_payload.copy_payload from the current guide "
        "before calling contract_runtime_submit_line."
    )


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
