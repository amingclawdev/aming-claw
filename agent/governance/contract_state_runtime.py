"""Pure contract-state projection helpers.

This module folds contract bindings and timeline event facts into a read-only
projection. It must not append timeline events, validate route tokens, or write
database state; those authorities stay with task_timeline/server/MCP adapters.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


CONTRACT_STATE_PROJECTION_SCHEMA_VERSION = "contract_state_projection.v1"

CONTRACT_PASS_STATUSES = {
    "accepted",
    "allowed",
    "complete",
    "completed",
    "not_applicable",
    "ok",
    "passed",
    "succeeded",
}

_DEFAULT_REQUIREMENT_ORDER = {
    "route_context": 100,
    "route_action_precheck": 200,
    "observer_work_mode_transition": 250,
    "bounded_implementation_worker_dispatch": 300,
    "dispatch_bounded_worker": 300,
    "mf_subagent_dispatch": 300,
    "read_receipt": 350,
    "mf_subagent_startup": 400,
    "worker_graph_trace": 450,
    "graph_trace": 450,
    "implementation": 500,
    "worker_implementation": 500,
    "record_finish_time_worker_attestation": 550,
    "finish_gate": 600,
    "independent_verification_lane": 700,
    "independent_verification": 700,
    "independent_qa": 700,
    "independent_qa_lane": 700,
    "qa_review": 720,
    "qa_verification": 720,
    "verification": 800,
    "close_ready": 900,
}

_CONTRACT_REVISION_FIELD_NAMES = {
    "contract_revision_id",
    "current_revision_id",
    "revision_id",
    "runtime_contract_revision_id",
}

_CONTRACT_BINDING_EVENT_KINDS = {
    "contract_binding",
    "contract_bound",
    "contract_binding_changed",
    "contract_revision_created",
    "contract_revision_changed",
    "contract_state_changed",
}

_CONTRACT_BINDING_CONTAINER_KEYS = (
    "contract_binding",
    "active_contract",
    "contract_revision",
    "contract_instance",
    "contract",
)

_CONTRACT_BINDING_FIELD_NAMES = {
    "contract_id",
    "contract_chain_id",
    "contract_execution_id",
    "contract_instance_id",
    "contract_template",
    "contract_template_id",
    "template_id",
    "contract_kind",
    "contract_type",
    "contract_status",
    "contract_state",
    "current_revision_id",
    "contract_revision_id",
    "revision_id",
    "runtime_contract_revision_id",
    "parent_contract_execution_id",
    "successor_backlog_id",
    "successor_contract_execution_id",
    "successor_contract_template_id",
    "handoff_event_id",
    "handoff_reason",
    "state",
    "status",
}

_SUCCESSOR_CONTRACT_CONTAINER_KEYS = (
    "successor_contract",
    "selected_successor_contract",
    "successor_contract_binding",
)

_SUCCESSOR_CONTRACT_FIELD_NAMES = {
    "backlog_id",
    "contract_id",
    "contract_chain_id",
    "contract_execution_id",
    "contract_instance_id",
    "contract_revision_id",
    "contract_template",
    "contract_template_id",
    "conditional_required_evidence",
    "evidence_requirements",
    "handoff_event_id",
    "handoff_preconditions",
    "handoff_reason",
    "parent_contract_execution_id",
    "required_evidence",
    "selected_by_actor",
    "state",
    "status",
    "successor_contract_candidates",
    "successor_backlog_id",
    "successor_contract_execution_id",
    "successor_contract_instance_id",
    "successor_contract_policy",
    "successor_contract_template_id",
    "template_id",
}

_ROUTE_BINDING_FIELD_NAMES = (
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "prompt_contract_hash",
    "visible_injection_manifest_hash",
    "route_token_ref",
)

_STRICT_REQUIREMENT_IDENTITY_FIELD_ALIASES = {
    "writer_role": (
        "expected_writer_role",
        "required_writer_role",
        "expected_actor_role",
        "required_actor_role",
        "allowed_writer_role",
        "writer_role",
    ),
    "route_token_ref": (
        "expected_route_token_ref",
        "required_route_token_ref",
        "route_token_ref",
    ),
    "runtime_context_id": (
        "expected_runtime_context_id",
        "required_runtime_context_id",
        "runtime_context_id",
    ),
    "task_id": (
        "expected_task_id",
        "required_task_id",
        "task_id",
    ),
    "parent_task_id": (
        "expected_parent_task_id",
        "required_parent_task_id",
        "parent_task_id",
    ),
    "worker_slot_id": (
        "expected_worker_slot_id",
        "required_worker_slot_id",
        "worker_slot_id",
    ),
    "fence_token_hash": (
        "expected_fence_token_hash",
        "required_fence_token_hash",
        "fence_token_hash",
    ),
}

_STRICT_EVENT_IDENTITY_FIELD_ALIASES = {
    "writer_role": (
        "actor_role",
        "lane_actor_role",
        "role",
    ),
    "route_token_ref": ("route_token_ref",),
    "runtime_context_id": ("runtime_context_id",),
    "task_id": ("task_id",),
    "parent_task_id": ("parent_task_id",),
    "worker_slot_id": ("worker_slot_id",),
    "fence_token_hash": ("fence_token_hash",),
}

_STRICT_REQUIREMENT_PAYLOAD_FIELDS = {
    "writer_role": "writer_role",
    "route_token_ref": "route_token_ref",
    "runtime_context_id": "runtime_context_id",
    "task_id": "task_id",
    "parent_task_id": "parent_task_id",
    "worker_slot_id": "worker_slot_id",
    "fence_token_hash": "fence_token_hash",
}

_DIRECT_TIMELINE_APPEND_EVENT_KINDS = {
    "architecture_review",
    "close_after_clauses",
    "close_ready",
    "contract_binding",
    "contract_binding_changed",
    "contract_revision_created",
    "contract_state_changed",
    "cross_ref_lineage_bridge",
    "design_review",
    "dispatch_bounded_worker",
    "forbidden_attempt_recorded",
    "hotfix_entered",
    "hotfix_under_action",
    "implementation",
    "independent_verification",
    "lineage_bridge",
    "merge",
    "merge_preview",
    "merge_queue_entry",
    "mf_subagent_startup",
    "no_progress_timeout",
    "observer_command",
    "observer_visual_smoke",
    "observer_work_mode_transition",
    "plan_precheck",
    "qa_review",
    "qa_verification",
    "reconcile",
    "record_blocker",
    "review_lane",
    "route_action_precheck",
    "route_context",
    "route_identity_cleanup",
    "route_token_gate",
    "verification",
}

_QA_EVIDENCE_CONTRACT_TEMPLATE_ID = "qa_evidence_gate_review.v1"
_AUDIT_CLOSE_CONTRACT_TEMPLATE_ID = "audit_close_with_qa_acceptance.v1"

_CONTRACT_FIRST_CANDIDATE_TEMPLATES = [
    "onboard_contract.v1",
    "observer_hotfix_direct_mutation.v1",
    "mf_parallel.v1",
    _QA_EVIDENCE_CONTRACT_TEMPLATE_ID,
    _AUDIT_CLOSE_CONTRACT_TEMPLATE_ID,
]

_CONTRACT_FIRST_SUPPORTED_ROLES = [
    "onboard",
    "observer_hotfix",
    "parallel_worker",
    "qa",
    "merge",
    "audit_close",
]

_META_CONTRACT_ALLOWED_ACTIONS_BY_ROLE = {
    "observer": {
        "route_context",
        "route_identity_cleanup",
        "route_action_precheck",
        "design_review",
        "architecture_review",
        "review_lane",
        "plan_precheck",
        "adversarial_review",
        "contract_binding",
        "contract_revision_created",
        "contract_binding_changed",
        "contract_state_changed",
        "observer_work_mode_transition",
        "dispatch_bounded_worker",
        "merge_preview",
        "merge_queue_entry",
        "live_merge",
        "merge",
        "reconcile",
        "record_blocker",
        "close_ready",
        "close_after_clauses",
        "hotfix_entered",
        "hotfix_under_action",
        "observer_command",
        "cross_ref_lineage_bridge",
        "lineage_bridge",
        "route_token_gate",
        "no_progress_timeout",
        "forbidden_attempt_recorded",
    },
    "mf_sub": {
        "read_receipt",
        "mf_subagent_startup",
        "record_finish_time_worker_attestation",
        "implementation",
        "review_ready",
        "worker_progress",
        "patch",
        "close_ready",
        "record_blocker",
        "graph_trace",
    },
    "qa": {
        "independent_verification",
        "qa_verification",
        "qa_review",
        "record_blocker",
        "review_ready",
    },
    "judge": {
        "design_review",
        "architecture_review",
        "review_lane",
        "plan_precheck",
        "adversarial_review",
        "contract_binding",
        "contract_revision_created",
        "contract_binding_changed",
        "contract_state_changed",
        "independent_verification",
        "qa_review",
        "record_blocker",
        "forbidden_attempt_recorded",
    },
    "system": {
        "service_route",
        "route_token_gate",
        "observer_command",
        "no_progress_timeout",
        "forbidden_attempt_recorded",
        "record_blocker",
        "hotfix_entered",
        "hotfix_under_action",
        "stale_artifact_cleanup",
    },
    "operator": {
        "hotfix_entered",
        "hotfix_under_action",
        "record_blocker",
        "close_after_clauses",
    },
}

_QA_TIMELINE_EVENT_KINDS = {
    "independent_verification",
    "qa_review",
    "qa_verification",
}

_QA_REQUIREMENT_EVENT_KIND_ALIASES = {
    "independent_verification_lane": (
        "independent_verification",
        "qa_verification",
        "qa_review",
    ),
    "independent_verification_evidence": (
        "independent_verification",
        "qa_verification",
        "qa_review",
    ),
    "independent_qa": (
        "independent_verification",
        "qa_verification",
        "qa_review",
    ),
    "independent_qa_lane": (
        "independent_verification",
        "qa_verification",
        "qa_review",
    ),
    "qa_evidence_gate_review": (
        "qa_review",
        "qa_verification",
        "independent_verification",
    ),
}

_WORKER_REQUIREMENT_EVENT_KIND_ALIASES = {
    "bounded_implementation_worker_dispatch": (
        "mf_subagent_dispatch",
        "dispatch_bounded_worker",
        "bounded_implementation_worker_dispatch",
    ),
    "bounded_implementation_subagent.dispatch": (
        "mf_subagent_dispatch",
        "dispatch_bounded_worker",
        "bounded_implementation_worker_dispatch",
    ),
}

_WORKER_TIMELINE_EVENT_KINDS = {
    "graph_trace",
    "implementation",
    "mf_subagent_startup",
    "patch",
    "read_receipt",
    "record_finish_time_worker_attestation",
    "review_ready",
    "worker_progress",
}

_CONDITION_JSON_CONTEXT_FIELDS = (
    "chain_trigger_json",
    "target_json",
    "metadata_json",
    "bypass_policy_json",
    "contract_json",
)

_CONDITION_NESTED_CONTEXT_FIELDS = (
    "chain_trigger",
    "target",
    "target_project",
    "project",
    "metadata",
    "manual_fix",
    "contract",
)

_SUCCESSOR_ONLY_CONTRACT_BINDING_FIELD_NAMES = {
    "handoff_event_id",
    "handoff_reason",
    "successor_backlog_id",
    "successor_contract_execution_id",
    "successor_contract_template_id",
}

_SUCCESSOR_BINDING_MARKER_FIELD_NAMES = {
    "successor_backlog_id",
    "successor_contract_execution_id",
    "successor_contract_template_id",
}

_TEST_ROUTE_FIELD_NAMES = (
    "test_route",
    "verification_route",
    "verification_route_policy",
    "test_scenario_policy",
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _contract_root(contract: Mapping[str, Any] | None) -> dict[str, Any]:
    data = _mapping(contract)
    for key in ("parallel_contract", "mf_contract", "contract_instance", "contract"):
        nested = data.get(key)
        if isinstance(nested, Mapping):
            return dict(nested)
    return data


def _event_payloads(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for key in ("payload", "verification", "artifact_refs"):
        value = event.get(key)
        if isinstance(value, Mapping):
            payloads.append(dict(value))
    return payloads


def _dedupe_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        token = str(value or "").strip()
        if token and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _first_mapping(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Mapping):
        value = value.values()
    if isinstance(value, list | tuple | set):
        return _dedupe_nonempty([str(item or "").strip() for item in value])
    return []


def _event_kind(event: Mapping[str, Any]) -> str:
    return str(
        event.get("event_kind")
        or event.get("kind")
        or event.get("event_type")
        or ""
    ).strip()


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _condition_contexts(
    root: Mapping[str, Any],
    backlog_row: Mapping[str, Any],
) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    for source in (root, backlog_row):
        if isinstance(source, Mapping):
            contexts.append(dict(source))
            for field in _CONDITION_JSON_CONTEXT_FIELDS:
                nested = _json_mapping(source.get(field))
                if nested:
                    contexts.append(nested)
            for field in _CONDITION_NESTED_CONTEXT_FIELDS:
                nested = _json_mapping(source.get(field))
                if nested:
                    contexts.append(nested)
    for context in list(contexts):
        for field in _CONDITION_NESTED_CONTEXT_FIELDS:
            nested = _json_mapping(context.get(field))
            if nested:
                contexts.append(nested)
    return contexts


def _first_condition_value(
    contexts: list[dict[str, Any]],
    *field_names: str,
) -> Any:
    for context in contexts:
        for field in field_names:
            if field not in context:
                continue
            value = context.get(field)
            if value not in (None, "", [], {}):
                return value
    return None


def _event_numeric_id(event: Mapping[str, Any]) -> int:
    for key in ("id", "event_id"):
        try:
            return int(event.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return 0


def _event_ref_id(event: Mapping[str, Any]) -> str:
    return str(event.get("id") or event.get("event_id") or "").strip()


def _payload_field_value(value: Any, field_names: set[str], *, depth: int = 0) -> str:
    if depth > 6:
        return ""
    if isinstance(value, Mapping):
        for key in field_names:
            token = str(value.get(key) or "").strip()
            if token:
                return token
        for child in value.values():
            token = _payload_field_value(child, field_names, depth=depth + 1)
            if token:
                return token
    elif isinstance(value, list | tuple | set):
        for child in value:
            token = _payload_field_value(child, field_names, depth=depth + 1)
            if token:
                return token
    return ""


def _normalized_writer_role(value: Any) -> str:
    role = str(value or "").strip().lower()
    if role in {"worker", "mf_subagent", "mf-sub"}:
        return "mf_sub"
    return role


def _trusted_meta_contract_gate_containers(
    event: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    containers: list[Mapping[str, Any]] = []
    gate = event.get("meta_contract_gate")
    if isinstance(gate, Mapping):
        containers.append(gate)
    for payload in _event_payloads(event):
        gate = payload.get("meta_contract_gate")
        if isinstance(gate, Mapping):
            containers.append(gate)
    return containers


def _trusted_writer_role_value(event: Mapping[str, Any]) -> str:
    for gate in _trusted_meta_contract_gate_containers(event):
        for field in ("role", "actor_role"):
            token = str(gate.get(field) or "").strip()
            if token:
                return token
    for field in ("actor_role", "lane_actor_role", "role"):
        token = str(event.get(field) or "").strip()
        if token:
            return token
    return ""


def _strict_requirement_identity_expectations(
    requirement: Mapping[str, Any],
) -> dict[str, str]:
    containers: list[Mapping[str, Any]] = [requirement]
    strict_identity = requirement.get("strict_identity")
    if isinstance(strict_identity, Mapping):
        containers.append(strict_identity)
        expected = strict_identity.get("expected")
        if isinstance(expected, Mapping):
            containers.append(expected)
    for key in ("expected_identity", "required_identity", "identity"):
        value = requirement.get(key)
        if isinstance(value, Mapping):
            containers.append(value)

    expectations: dict[str, str] = {}
    for canonical, aliases in _STRICT_REQUIREMENT_IDENTITY_FIELD_ALIASES.items():
        for container in containers:
            for alias in aliases:
                token = str(container.get(alias) or "").strip()
                if token:
                    expectations[canonical] = token
                    break
            if canonical in expectations:
                break
    return expectations


def _strict_event_identity_value(
    event: Mapping[str, Any],
    canonical: str,
) -> str:
    if canonical == "writer_role":
        return _trusted_writer_role_value(event)
    aliases = set(_STRICT_EVENT_IDENTITY_FIELD_ALIASES.get(canonical) or ())
    if not aliases:
        return ""
    for alias in aliases:
        token = str(event.get(alias) or "").strip()
        if token:
            return token
    for payload in _event_payloads(event):
        token = _payload_field_value(payload, aliases)
        if token:
            return token
    return ""


def _event_matches_strict_requirement_identity(
    event: Mapping[str, Any],
    requirement: Mapping[str, Any],
) -> tuple[bool, dict[str, str]]:
    expectations = _strict_requirement_identity_expectations(requirement)
    if not expectations:
        return True, {}
    matched: dict[str, str] = {}
    for canonical, expected in expectations.items():
        actual = _strict_event_identity_value(event, canonical)
        if canonical == "writer_role":
            if _normalized_writer_role(actual) != _normalized_writer_role(expected):
                return False, {}
        elif actual != expected:
            return False, {}
        matched[canonical] = expected
    return True, matched


def _strict_requirement_identity_hint(
    requirement: Mapping[str, Any],
) -> dict[str, Any]:
    expectations = _strict_requirement_identity_expectations(requirement)
    if not expectations:
        return {}
    required_payload_fields = [
        _STRICT_REQUIREMENT_PAYLOAD_FIELDS[canonical]
        for canonical in expectations
        if canonical in _STRICT_REQUIREMENT_PAYLOAD_FIELDS
    ]
    return {
        "schema_version": "contract_state_strict_requirement_identity.v1",
        "expected_identity": dict(expectations),
        "required_payload_fields": _dedupe_nonempty(required_payload_fields),
        "match_policy": "all_declared_identity_fields",
        "trusted_writer_role_sources": [
            "meta_contract_gate.role",
            "meta_contract_gate.actor_role",
            "event.actor_role",
            "event.lane_actor_role",
            "event.role",
        ],
    }


def _contract_binding_from_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    binding: dict[str, Any] = {}
    for key in _CONTRACT_BINDING_CONTAINER_KEYS:
        nested = value.get(key)
        if isinstance(nested, Mapping):
            binding.update(
                {
                    field: nested_value
                    for field, nested_value in nested.items()
                    if field in _CONTRACT_BINDING_FIELD_NAMES
                    or field in {"source_event_id"}
                }
            )
    for field in _CONTRACT_BINDING_FIELD_NAMES:
        if field in _SUCCESSOR_ONLY_CONTRACT_BINDING_FIELD_NAMES:
            continue
        field_value = value.get(field)
        if field_value:
            binding[field] = field_value
    return binding


def _payload_is_successor_binding_only(value: Mapping[str, Any]) -> bool:
    has_root_binding_container = any(
        isinstance(value.get(key), Mapping) for key in _CONTRACT_BINDING_CONTAINER_KEYS
    )
    if has_root_binding_container:
        return False
    if any(
        isinstance(value.get(key), Mapping)
        for key in _SUCCESSOR_CONTRACT_CONTAINER_KEYS
    ):
        return True
    return any(
        str(value.get(field) or "").strip()
        for field in _SUCCESSOR_BINDING_MARKER_FIELD_NAMES
    )


def _latest_contract_binding(
    events: list[dict[str, Any]],
    root: Mapping[str, Any],
) -> dict[str, Any]:
    binding = _contract_binding_from_mapping(root)
    for event in events:
        event_kind = str(event.get("event_kind") or "").strip()
        for payload in _event_payloads(event):
            if _payload_is_successor_binding_only(payload):
                continue
            candidate = _contract_binding_from_mapping(payload)
            if not candidate:
                continue
            has_nested_binding = any(
                isinstance(payload.get(key), Mapping)
                for key in _CONTRACT_BINDING_CONTAINER_KEYS
            )
            if event_kind not in _CONTRACT_BINDING_EVENT_KINDS and not has_nested_binding:
                continue
            binding.update(candidate)
            event_id = event.get("id")
            if event_id:
                binding["source_event_id"] = event_id
            binding["status"] = str(
                event.get("status")
                or binding.get("contract_status")
                or binding.get("status")
                or ""
            )
    return binding


def _latest_contract_revision_id(
    events: list[dict[str, Any]],
    root: Mapping[str, Any],
) -> str:
    for key in _CONTRACT_REVISION_FIELD_NAMES:
        token = str(root.get(key) or "").strip()
        if token:
            return token
    for event in reversed(events):
        for payload in _event_payloads(event):
            for key in _CONTRACT_REVISION_FIELD_NAMES:
                token = str(payload.get(key) or "").strip()
                if token:
                    return token
    return ""


def _latest_route_binding(
    events: list[dict[str, Any]],
    root: Mapping[str, Any],
) -> dict[str, Any]:
    binding = _first_mapping(
        root.get("route_binding"),
        root.get("route_identity"),
        root.get("route_context"),
    )
    for event in events:
        for payload in _event_payloads(event):
            candidate = _first_mapping(
                payload.get("route_binding"),
                payload.get("route_identity"),
                payload.get("route_context"),
                payload.get("route_token_gate"),
            )
            for field in _ROUTE_BINDING_FIELD_NAMES:
                value = payload.get(field)
                if value:
                    candidate[field] = value
            if not candidate:
                continue
            binding.update(
                {
                    key: value
                    for key, value in candidate.items()
                    if key in _ROUTE_BINDING_FIELD_NAMES
                    or key in {"source_event_id", "status"}
                }
            )
            event_id = event.get("id")
            if event_id:
                binding["source_event_id"] = event_id
            binding["status"] = str(event.get("status") or binding.get("status") or "")
    return binding


def _latest_test_route(
    events: list[dict[str, Any]],
    root: Mapping[str, Any],
) -> dict[str, Any]:
    route = _first_mapping(*(root.get(field) for field in _TEST_ROUTE_FIELD_NAMES))
    for event in events:
        for payload in _event_payloads(event):
            candidate = _first_mapping(
                *(payload.get(field) for field in _TEST_ROUTE_FIELD_NAMES)
            )
            if not candidate:
                continue
            route.update(candidate)
            event_id = event.get("id")
            if event_id:
                route["source_event_id"] = event_id
            route["status"] = str(event.get("status") or route.get("status") or "")
    return route


def _condition_matches(
    condition: Mapping[str, Any] | None,
    *,
    root: Mapping[str, Any],
    backlog_row: Mapping[str, Any],
) -> bool:
    if not condition:
        return True
    contexts = _condition_contexts(root, backlog_row)
    for key, expected in condition.items():
        if key == "target_kind":
            actual = _first_condition_value(
                contexts,
                "target_kind",
                "target_project_kind",
                "project_kind",
            )
            if not actual and (
                _first_condition_value(contexts, "generated_demo") is True
                or str(
                    _first_condition_value(contexts, "generated_demo") or ""
                ).strip().lower()
                in {"1", "true", "yes", "y", "on"}
            ):
                actual = "generated_demo"
        else:
            actual = _first_condition_value(contexts, key)
        if str(actual or "").strip() != str(expected or "").strip():
            return False
    return True


def _normalize_requirement(
    item: Any,
    *,
    index: int,
    condition: Mapping[str, Any] | None = None,
    conditional: bool = False,
    semantic_default_order: bool = False,
) -> dict[str, Any] | None:
    if isinstance(item, Mapping):
        requirement_id = str(
            item.get("id")
            or item.get("requirement_id")
            or item.get("evidence_id")
            or ""
        ).strip()
        if not requirement_id:
            return None
        step = dict(item)
    else:
        requirement_id = str(item or "").strip()
        if not requirement_id:
            return None
        step = {"id": requirement_id}
    step["id"] = requirement_id
    step.setdefault("action", f"record_{requirement_id}")
    step.setdefault("source", "contract_state")
    step.setdefault("precedence", "active_contract_missing_step")
    if "order" not in step or step.get("order") in (None, ""):
        step["order"] = (
            _DEFAULT_REQUIREMENT_ORDER.get(requirement_id, 10000) + index
            if semantic_default_order
            else index
        )
    step["conditional"] = bool(conditional)
    if condition:
        step["condition"] = dict(condition)
    step["prerequisite_ids"] = _string_list(
        step.get("prerequisite_ids") or step.get("requires")
    )
    step["accepted_event_kinds"] = _string_list(
        step.get("accepted_event_kinds")
        or step.get("event_kinds")
        or step.get("event_kind")
    )
    step["accepted_event_kinds"] = _requirement_event_kinds(
        requirement_id,
        step["accepted_event_kinds"],
    )
    step["accepted_statuses"] = [
        item.lower()
        for item in (
            _string_list(step.get("accepted_statuses"))
            or sorted(CONTRACT_PASS_STATUSES)
        )
    ]
    step["timeline_append_hint"] = _timeline_append_hint(step)
    return step


def _qa_requirement_event_kinds(
    requirement_id: str,
    accepted_event_kinds: list[str],
) -> list[str]:
    aliases = list(_QA_REQUIREMENT_EVENT_KIND_ALIASES.get(requirement_id) or ())
    if not aliases:
        return accepted_event_kinds or [requirement_id]
    if not accepted_event_kinds:
        return aliases
    if accepted_event_kinds == [requirement_id] or not any(
        event_kind in _DIRECT_TIMELINE_APPEND_EVENT_KINDS
        for event_kind in accepted_event_kinds
    ):
        return _dedupe_nonempty([*aliases, *accepted_event_kinds])
    return accepted_event_kinds


def _requirement_event_kinds(
    requirement_id: str,
    accepted_event_kinds: list[str],
) -> list[str]:
    aliases = list(_WORKER_REQUIREMENT_EVENT_KIND_ALIASES.get(requirement_id) or ())
    if aliases:
        if not accepted_event_kinds:
            return aliases
        if accepted_event_kinds == [requirement_id] or not any(
            event_kind in _DIRECT_TIMELINE_APPEND_EVENT_KINDS
            for event_kind in accepted_event_kinds
        ):
            return _dedupe_nonempty([*aliases, *accepted_event_kinds])
    return _qa_requirement_event_kinds(requirement_id, accepted_event_kinds)


def _timeline_append_hint(step: Mapping[str, Any]) -> dict[str, Any]:
    requirement_id = str(step.get("id") or "").strip()
    accepted_event_kinds = _string_list(
        step.get("accepted_event_kinds")
        or step.get("event_kinds")
        or step.get("event_kind")
    )
    accepted_event_kinds = _requirement_event_kinds(
        requirement_id,
        accepted_event_kinds,
    )
    direct_event_kind, actor_role = _appendable_event_kind_for_requirement(
        accepted_event_kinds,
        requirement_id,
    )
    if not direct_event_kind:
        direct_event_kind = next(
            (
                event_kind
                for event_kind in accepted_event_kinds
                if event_kind in _DIRECT_TIMELINE_APPEND_EVENT_KINDS
            ),
            "",
        )
        actor_role = _actor_role_for_append_event(direct_event_kind, requirement_id)
    uses_requirement_payload_match = False
    if not direct_event_kind:
        direct_event_kind = "contract_state_changed"
        actor_role = _actor_role_for_append_event(direct_event_kind, requirement_id)
        uses_requirement_payload_match = True
    accepted_statuses = _string_list(step.get("accepted_statuses"))
    if "passed" in accepted_statuses:
        status = "passed"
    elif "accepted" in accepted_statuses:
        status = "accepted"
    else:
        status = accepted_statuses[0] if accepted_statuses else "passed"
    payload = {
        "schema_version": "contract_state_evidence.v1",
        "requirement_id": requirement_id,
        "requirement_ids": [requirement_id] if requirement_id else [],
    }
    strict_identity = _strict_requirement_identity_hint(step)
    if strict_identity:
        for canonical, value in strict_identity["expected_identity"].items():
            payload_field = _STRICT_REQUIREMENT_PAYLOAD_FIELDS.get(canonical)
            if payload_field:
                payload.setdefault(payload_field, value)
    hint = {
        "schema_version": "contract_state_timeline_append_hint.v1",
        "event_kind": direct_event_kind,
        "event_type": str(step.get("event_type") or "").strip()
        or (
            "contract.state.changed"
            if uses_requirement_payload_match
            else direct_event_kind.replace("_", ".")
        ),
        "status": status,
        "payload": payload,
        "actor_role": actor_role,
        "lane_actor_role": actor_role,
        "meta_contract_gate": _meta_contract_append_gate(
            direct_event_kind,
            actor_role,
        ),
        "satisfies_by": (
            "payload.requirement_id"
            if uses_requirement_payload_match
            else "event_kind"
        ),
        "accepted_event_kinds": accepted_event_kinds,
        "accepted_statuses": accepted_statuses,
    }
    if strict_identity:
        hint["strict_identity"] = strict_identity
        hint["expected_identity"] = dict(strict_identity["expected_identity"])
        hint["required_payload_fields"] = strict_identity["required_payload_fields"]
    return hint


def _appendable_event_kind_for_requirement(
    accepted_event_kinds: list[str],
    requirement_id: str,
) -> tuple[str, str]:
    for event_kind in accepted_event_kinds:
        if event_kind not in _DIRECT_TIMELINE_APPEND_EVENT_KINDS:
            continue
        actor_role = _actor_role_for_append_event(event_kind, requirement_id)
        if _meta_contract_append_gate(event_kind, actor_role).get("allowed") is True:
            return event_kind, actor_role
    return "", ""


def _actor_role_for_append_event(event_kind: str, requirement_id: str = "") -> str:
    marker = str(event_kind or requirement_id or "").strip()
    if marker in _QA_TIMELINE_EVENT_KINDS:
        return "qa"
    if marker in _WORKER_TIMELINE_EVENT_KINDS:
        return "mf_sub"
    return "observer"


def _role_bound_prefill_policy(
    *,
    actor_role: str,
    event_kind: str,
    requirement_id: str,
    lane_role: str = "",
) -> dict[str, Any]:
    owner = _normalized_writer_role(actor_role) or "observer"
    observer_owned = owner == "observer"
    base_fields = [
        "schema_version",
        "requirement_id",
        "requirement_ids",
        "contract_execution_id",
        "active_contract_execution_id",
        "parent_contract_execution_id_expected",
        "task_id",
        "route_identity",
    ]
    route_fields = list(_ROUTE_BINDING_FIELD_NAMES)
    actor_execution_fields = [
        "status",
        "evidence_refs",
        "verification",
        "result",
        "changed_files",
        "tests",
        "graph_trace_ids",
    ]
    return {
        "schema_version": "contract_state.role_bound_prefill_policy.v1",
        "requirement_id": requirement_id,
        "event_kind": event_kind,
        "lane_role": lane_role,
        "execution_owner_role": owner,
        "observer_owned": observer_owned,
        "observer_prefill_allowed": True,
        "observer_prefill_fields": (
            [*base_fields, *route_fields] if not observer_owned else ["*"]
        ),
        "actor_must_supply_evidence": not observer_owned,
        "actor_owned_execution_fields": [] if observer_owned else actor_execution_fields,
        "boundary": (
            "observer_may_prefill_and_execute"
            if observer_owned
            else "observer_prefills_identity_only_actor_records_execution_evidence"
        ),
    }


def _meta_contract_append_gate(event_kind: str, actor_role: str) -> dict[str, Any]:
    role = str(actor_role or "").strip() or "observer"
    allowed_actions = set(_META_CONTRACT_ALLOWED_ACTIONS_BY_ROLE.get(role) or set())
    if role == "worker":
        allowed_actions.update(_META_CONTRACT_ALLOWED_ACTIONS_BY_ROLE["mf_sub"])
    allowed = str(event_kind or "").strip() in allowed_actions
    return {
        "schema_version": "meta_contract_append_hint_gate.v1",
        "checked": True,
        "meta_contract_id": "meta_contract.v1",
        "actor_role": role,
        "action": str(event_kind or "").strip(),
        "allowed": allowed,
        "reason": "role_action_whitelist_allows_action"
        if allowed
        else "role_action_whitelist_does_not_allow_action",
    }


def _public_route_identity(route_binding: Mapping[str, Any]) -> dict[str, str]:
    return {
        field: str(route_binding.get(field) or "").strip()
        for field in _ROUTE_BINDING_FIELD_NAMES
        if str(route_binding.get(field) or "").strip()
    }


def _completed_requirement_event_ref(
    completed_sources: Mapping[str, Mapping[str, Any]],
    requirement_id: str,
) -> dict[str, Any]:
    source = completed_sources.get(requirement_id)
    if not isinstance(source, Mapping):
        return {}
    return {
        key: source.get(key)
        for key in ("event_id", "event_kind", "status", "phase")
        if source.get(key) not in (None, "", [], {})
    }


def _event_contract_execution_ids(event: Mapping[str, Any]) -> set[str]:
    execution_ids = {
        str(event.get(field) or "").strip()
        for field in (
            "active_contract_execution_id",
            "contract_execution_id",
            "parent_contract_execution_id",
            "reviewed_contract_execution_id",
            "successor_contract_execution_id",
        )
        if str(event.get(field) or "").strip()
    }
    for payload in _event_payloads(event):
        for field in (
            "active_contract_execution_id",
            "contract_execution_id",
            "parent_contract_execution_id",
            "reviewed_contract_execution_id",
            "successor_contract_execution_id",
        ):
            execution_id = _payload_field_value(payload, {field})
            if execution_id:
                execution_ids.add(execution_id)
    return execution_ids


def _close_ready_evidence_refs(
    events: list[dict[str, Any]],
    *,
    contract_execution_id: str = "",
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    expected_execution_id = str(contract_execution_id or "").strip()
    for event in events:
        event_kind = str(event.get("event_kind") or "").strip()
        is_close_ready = event_kind == "close_ready" or any(
            _payload_declares_requirement(payload, "close_ready")
            for payload in _event_payloads(event)
        )
        if not is_close_ready:
            continue
        status = str(event.get("status") or "").strip().lower()
        if status and status not in CONTRACT_PASS_STATUSES:
            continue
        execution_ids = _event_contract_execution_ids(event)
        if (
            expected_execution_id
            and execution_ids
            and expected_execution_id not in execution_ids
        ):
            continue
        event_id = _event_ref_id(event)
        if not event_id:
            continue
        ref = f"timeline:{event_id}"
        if ref in seen:
            continue
        seen.add(ref)
        item = {
            "ref": ref,
            "event_id": event_id,
            "event_kind": event_kind,
            "status": str(event.get("status") or ""),
        }
        if expected_execution_id:
            item["contract_execution_id"] = expected_execution_id
        refs.append({key: value for key, value in item.items() if value})
    return refs


def _projection_task_id(row: Mapping[str, Any], backlog_id: str) -> str:
    return str(
        row.get("task_id")
        or row.get("parent_task_id")
        or row.get("bug_id")
        or row.get("backlog_id")
        or backlog_id
        or ""
    ).strip()


def _backlog_target_files(row: Mapping[str, Any]) -> list[str]:
    value = row.get("target_files") or row.get("target_file") or row.get("owned_files")
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = value
        return _string_list(parsed)
    return _string_list(value)


def _backlog_close_route_token_request_hint(
    *,
    row: Mapping[str, Any],
    backlog_id: str,
    task_id: str,
    route_binding: Mapping[str, Any],
    route_token_ref: str,
    close_ready_evidence_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    evidence_refs = [
        str(item.get("ref") or "").strip()
        for item in close_ready_evidence_refs
        if str(item.get("ref") or "").strip()
    ]
    target_files = _backlog_target_files(row)
    issue_request = {
        "caller_role": "observer",
        "backlog_id": backlog_id,
        "task_id": task_id or backlog_id,
        "target_files": target_files,
        "allowed_actions": ["backlog_close"],
        "evidence_refs": evidence_refs,
    }
    if route_token_ref:
        issue_request["parent_route_token_ref"] = route_token_ref
    route_identity = _public_route_identity(route_binding)
    for field, value in route_identity.items():
        if field == "route_token_ref":
            continue
        issue_request[f"parent_{field}"] = value
    return {
        "schema_version": "contract_state_backlog_close_route_token_request_hint.v1",
        "action": "issue_route_token_ref",
        "allowed_action": "backlog_close",
        "allowed_actions": ["backlog_close"],
        "protected_action": True,
        "backlog_id": backlog_id,
        "task_id": task_id or backlog_id,
        "target_files": target_files,
        "evidence_refs": evidence_refs,
        "close_ready_evidence_refs": close_ready_evidence_refs,
        "current_route_token_ref": route_token_ref,
        "route_identity": route_identity,
        "issue_route_token_request": issue_request,
        "protected_entrypoint": {
            "mcp_tool": "backlog_close",
            "allowed_action": "backlog_close",
        },
        "route_token_issue_entrypoint": {
            "path": "/api/projects/{project_id}/observer/route-context/issue",
            "allowed_actions": ["backlog_close"],
        },
        "raw_route_token_exposed": False,
        "raw_secret_exposed": False,
    }


def _backlog_close_next_action_after_close_ready(
    *,
    row: Mapping[str, Any],
    backlog_id: str,
    contract_chain_id: str,
    contract_execution_id: str,
    route_binding: Mapping[str, Any],
    route_token_ref: str,
    projection_watermark: Any,
    close_ready_evidence_refs: list[dict[str, Any]],
    precedence: str,
    selected_successor_contract: Mapping[str, Any] | None = None,
    selected_successor_contract_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    task_id = _projection_task_id(row, backlog_id)
    route_token_request_hint = _backlog_close_route_token_request_hint(
        row=row,
        backlog_id=backlog_id,
        task_id=task_id,
        route_binding=route_binding,
        route_token_ref=route_token_ref,
        close_ready_evidence_refs=close_ready_evidence_refs,
    )
    action = {
        "id": "issue_backlog_close_route_token_then_backlog_close",
        "action": "backlog_close",
        "requirement_id": "backlog_close",
        "detail": (
            "close_ready evidence already exists; issue a backlog_close-scoped "
            "route token and then call backlog_close by ref"
        ),
        "source": "contract_state",
        "precedence": precedence,
        "allowed_action": "backlog_close",
        "protected_action": True,
        "contract_chain_id": contract_chain_id,
        "contract_execution_id": contract_execution_id,
        "backlog_id": backlog_id,
        "task_id": task_id,
        "route_token_ref": route_token_ref,
        "projection_watermark": projection_watermark,
        "close_ready_evidence_refs": close_ready_evidence_refs,
        "route_token_request_hint": route_token_request_hint,
    }
    if selected_successor_contract:
        action["selected_successor_contract"] = dict(selected_successor_contract)
    if selected_successor_contract_state:
        action["selected_successor_contract_state"] = dict(
            selected_successor_contract_state
        )
    return action


def _action_candidate_template_ids(action: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for item in action.get("successor_contract_candidates") or []:
        if not isinstance(item, Mapping):
            continue
        template_id = str(item.get("contract_template_id") or item.get("id") or "")
        if template_id:
            ids.append(template_id)
    return _dedupe_nonempty(ids)


def _default_timeline_append_hint_for_action(
    action: Mapping[str, Any],
) -> dict[str, Any]:
    action_id = str(action.get("id") or action.get("action") or "").strip()
    if action_id == "select_or_enter_contract":
        event_kind = "contract_binding"
        actor_role = _actor_role_for_append_event(event_kind, action_id)
        candidate_templates = _string_list(action.get("candidate_contract_templates"))
        if not candidate_templates:
            candidate_templates = list(_CONTRACT_FIRST_CANDIDATE_TEMPLATES)
        supported_roles = _string_list(action.get("supported_contract_roles"))
        if not supported_roles:
            supported_roles = list(_CONTRACT_FIRST_SUPPORTED_ROLES)
        return {
            "schema_version": "contract_state_timeline_append_hint.v1",
            "event_kind": event_kind,
            "event_type": "contract.selected",
            "status": "passed",
            "payload": {
                "schema_version": "contract_first_selection_hint.v1",
                "requirement_id": "select_or_enter_contract",
                "requirement_ids": ["select_or_enter_contract"],
                "selection_required_before_mutation": True,
                "candidate_contract_templates": candidate_templates,
                "supported_contract_roles": supported_roles,
                "entrypoints": {
                    "onboard_or_parallel_or_qa": (
                        "append contract_binding / contract_revision_created evidence"
                    ),
                    "observer_hotfix": (
                        "POST /api/projects/{project_id}/hotfix/enter before mutation"
                    ),
                    "audit_close": (
                        "select audit_close_with_qa_acceptance.v1 only for "
                        "recovery closure"
                    ),
                },
            },
            "actor_role": actor_role,
            "lane_actor_role": actor_role,
            "meta_contract_gate": _meta_contract_append_gate(event_kind, actor_role),
            "satisfies_by": "event_kind",
            "accepted_event_kinds": [
                "contract_binding",
                "contract_revision_created",
                "contract_bound",
                "hotfix_entered",
            ],
            "accepted_statuses": sorted(CONTRACT_PASS_STATUSES),
        }
    if action_id == "hotfix_post_action_summary":
        event_kind = "hotfix_under_action"
        actor_role = _actor_role_for_append_event(event_kind, action_id)
        return {
            "schema_version": "contract_state_timeline_append_hint.v1",
            "event_kind": event_kind,
            "event_type": "hotfix.under_action",
            "status": "accepted",
            "payload": {
                "schema_version": "observer_hotfix_action_summary.v1",
                "requirement_id": "hotfix_post_action_summary",
                "requirement_ids": ["hotfix_post_action_summary"],
                "pre_reason_event_id": str(action.get("pre_reason_event_id") or ""),
                "what_changed": "<summary of the tiny deterministic mutation>",
                "changed_files": [],
                "verification_evidence_refs": [],
                "implementation_close_evidence": {
                    "counts_as_implementation": True,
                    "changed_files": [],
                    "verification_evidence_refs": [],
                    "qa_lineage": {
                        "required": True,
                        "required_gate": "independent_qa_gate",
                        "successor_contract_execution_id": (
                            "<qa successor contract_execution_id>"
                        ),
                    },
                },
                "deviations_from_plan": [],
                "remaining_close_gate_evidence": [
                    "independent_verification",
                    "verification",
                    "close_ready",
                ],
            },
            "actor_role": actor_role,
            "lane_actor_role": actor_role,
            "meta_contract_gate": _meta_contract_append_gate(event_kind, actor_role),
            "satisfies_by": "event_kind",
            "accepted_event_kinds": [event_kind],
            "accepted_statuses": sorted(CONTRACT_PASS_STATUSES),
        }
    if action_id == "close_ready":
        event_kind = "close_ready"
        actor_role = _actor_role_for_append_event(event_kind, action_id)
        return {
            "schema_version": "contract_state_timeline_append_hint.v1",
            "event_kind": event_kind,
            "event_type": "close.ready",
            "status": "accepted",
            "payload": {
                "schema_version": "contract_state_evidence.v1",
                "requirement_id": "close_ready",
                "requirement_ids": ["close_ready"],
            },
            "actor_role": actor_role,
            "lane_actor_role": actor_role,
            "meta_contract_gate": _meta_contract_append_gate(event_kind, actor_role),
            "satisfies_by": "event_kind",
            "accepted_event_kinds": [event_kind],
            "accepted_statuses": sorted(CONTRACT_PASS_STATUSES),
        }
    if action_id == "select_successor_contract":
        event_kind = "contract_binding"
        actor_role = _actor_role_for_append_event(event_kind, action_id)
        return {
            "schema_version": "contract_state_timeline_append_hint.v1",
            "event_kind": event_kind,
            "event_type": "contract.successor.selected",
            "status": "passed",
            "payload": {
                "schema_version": "contract_successor_selection_hint.v1",
                "requirement_id": "successor_contract_selection",
                "requirement_ids": ["successor_contract_selection"],
                "successor_contract": {
                    "contract_chain_id": str(
                        action.get("contract_chain_id") or ""
                    ).strip(),
                    "parent_contract_execution_id": str(
                        action.get("contract_execution_id") or ""
                    ).strip(),
                    "successor_contract_execution_id": (
                        "<new successor execution id>"
                    ),
                    "contract_template_id": "<selected successor contract_template_id>",
                    "handoff_event_id": "<timeline event id or external evidence ref>",
                    "handoff_reason": "<why this successor is the next contract>",
                },
            },
            "actor_role": actor_role,
            "lane_actor_role": actor_role,
            "meta_contract_gate": _meta_contract_append_gate(event_kind, actor_role),
            "satisfies_by": "event_kind",
            "accepted_event_kinds": [event_kind],
            "accepted_statuses": sorted(CONTRACT_PASS_STATUSES),
        }
    return {}


def _lane_context_for_action(
    action: Mapping[str, Any],
    lane_contract_executions: list[dict[str, Any]],
    active_execution: Mapping[str, Any],
) -> dict[str, Any]:
    action_execution_id = str(action.get("contract_execution_id") or "").strip()
    for lane in lane_contract_executions:
        if str(lane.get("contract_execution_id") or "").strip() == action_execution_id:
            return lane
    return {
        "role": "root",
        "contract_execution_id": active_execution.get("contract_execution_id") or "",
        "parent_contract_execution_id": active_execution.get(
            "parent_contract_execution_id"
        )
        or "",
        "contract_template_id": active_execution.get("contract_template_id") or "",
    }


def _enrich_next_action_append_hint(
    action: Mapping[str, Any] | None,
    *,
    lane_contract_executions: list[dict[str, Any]],
    active_execution: Mapping[str, Any],
    route_binding: Mapping[str, Any],
    task_id: str,
    route_action_precheck_ref: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(action, Mapping):
        return None
    enriched = dict(action)
    hint = _mapping(enriched.get("timeline_append_hint"))
    if not hint:
        hint = _default_timeline_append_hint_for_action(enriched)
    if not hint:
        return enriched

    lane = _lane_context_for_action(enriched, lane_contract_executions, active_execution)
    lane_role = str(lane.get("role") or "root").strip()
    event_kind = str(hint.get("event_kind") or "").strip()
    actor_role = str(hint.get("actor_role") or "").strip() or (
        _actor_role_for_append_event(
            event_kind,
            str(enriched.get("requirement_id") or enriched.get("id") or ""),
        )
    )
    route_identity = _public_route_identity(route_binding)
    payload = _mapping(hint.get("payload"))
    contract_execution_id = str(enriched.get("contract_execution_id") or "").strip()
    parent_expected = str(lane.get("parent_contract_execution_id") or "").strip()
    if contract_execution_id:
        payload.setdefault("contract_execution_id", contract_execution_id)
        payload.setdefault("active_contract_execution_id", contract_execution_id)
    if parent_expected:
        payload.setdefault("parent_contract_execution_id_expected", parent_expected)
    if task_id:
        payload.setdefault("task_id", task_id)
    if route_identity:
        payload.setdefault("route_identity", route_identity)
        for field, value in route_identity.items():
            payload.setdefault(field, value)
    if (
        str(enriched.get("id") or enriched.get("requirement_id") or "")
        == "observer_work_mode_transition"
        and route_action_precheck_ref
    ):
        payload.setdefault(
            "route_action_precheck_ref",
            dict(route_action_precheck_ref),
        )

    hint.update(
        {
            "payload": payload,
            "actor_role": actor_role,
            "lane_actor_role": actor_role,
            "lane_role": lane_role,
            "task_id": task_id,
            "contract_execution_id": contract_execution_id,
            "parent_contract_execution_id_expected": parent_expected,
            "route_identity_required": bool(route_identity),
            "route_identity": route_identity,
            "meta_contract_gate": _meta_contract_append_gate(event_kind, actor_role),
            "role_bound_prefill_policy": _role_bound_prefill_policy(
                actor_role=actor_role,
                event_kind=event_kind,
                requirement_id=str(
                    enriched.get("requirement_id") or enriched.get("id") or ""
                ),
                lane_role=lane_role,
            ),
        }
    )
    candidate_template_ids = _action_candidate_template_ids(enriched)
    if candidate_template_ids:
        hint["successor_semantics"] = {
            "schema_version": "successor_next_action_semantics.v1",
            "selection_required": enriched.get("id") == "select_successor_contract",
            "candidate_template_ids": candidate_template_ids,
            "audit_close_candidate": _AUDIT_CLOSE_CONTRACT_TEMPLATE_ID
            in candidate_template_ids,
            "hotfix_candidate": "observer_hotfix_direct_mutation.v1"
            in candidate_template_ids,
        }
    if str(enriched.get("id") or "") == "close_ready":
        selected_state = _mapping(enriched.get("selected_successor_contract_state"))
        state_candidates = _action_candidate_template_ids(
            {
                "successor_contract_candidates": selected_state.get(
                    "successor_contract_candidates"
                )
                or [],
            }
        )
        if not state_candidates:
            state_candidates = _action_candidate_template_ids(enriched)
        hint["terminal_semantics"] = {
            "schema_version": "terminal_successor_semantics.v1",
            "terminal_contract_complete": True,
            "successor_selection_required": False,
            "candidate_template_ids": state_candidates,
            "audit_close_candidate": _AUDIT_CLOSE_CONTRACT_TEMPLATE_ID
            in state_candidates,
            "hotfix_candidate": "observer_hotfix_direct_mutation.v1"
            in state_candidates,
            "close_gate_next": "audit_close_with_qa_acceptance_or_terminal_close",
        }
    enriched["timeline_append_hint"] = hint
    return enriched


def _next_action_requirement_metadata(step: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    requirement_id = str(step.get("id") or "").strip()
    hint = _mapping(step.get("timeline_append_hint"))
    append_event_kind = str(hint.get("event_kind") or "").strip()
    if (
        requirement_id in _WORKER_REQUIREMENT_EVENT_KIND_ALIASES
        and append_event_kind == "dispatch_bounded_worker"
    ):
        metadata["id"] = append_event_kind
        metadata["action"] = append_event_kind
        metadata["requirement_id"] = requirement_id
    for key in (
        "accepted_event_kinds",
        "accepted_statuses",
        "timeline_append_hint",
        "prerequisite_ids",
        "candidate_contract_templates",
        "supported_contract_roles",
        "entrypoints",
        "pre_reason_event_id",
    ):
        value = step.get(key)
        if value not in (None, "", [], {}):
            metadata[key] = value
    return metadata


def _next_action_summary(action: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(action, Mapping):
        return {}
    summary: dict[str, Any] = {}
    for key in (
        "id",
        "action",
        "requirement_id",
        "detail",
        "source",
        "precedence",
        "contract_chain_id",
        "contract_execution_id",
        "backlog_id",
        "route_token_ref",
        "projection_watermark",
        "ordered_missing_steps_source",
    ):
        value = action.get(key)
        if value not in (None, "", [], {}):
            summary[key] = value
    hint = action.get("timeline_append_hint")
    if isinstance(hint, Mapping) and hint:
        summary["timeline_append_hint"] = dict(hint)
    for key in (
        "protected_action",
        "allowed_action",
        "allowed_actions",
        "route_token_request_hint",
        "close_ready_evidence_refs",
        "terminal_semantics",
        "candidate_contract_templates",
        "supported_contract_roles",
        "entrypoints",
        "recovery_state",
        "dirty_files",
        "dirty_target_files",
    ):
        value = action.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, Mapping):
            summary[key] = dict(value)
        elif isinstance(value, list):
            summary[key] = [
                dict(item) if isinstance(item, Mapping) else item for item in value
            ]
        else:
            summary[key] = value
    for key in ("blocked_by", "prerequisite_ids"):
        value = action.get(key)
        if value:
            summary[key] = list(value) if isinstance(value, list | tuple | set) else value
    ordered = action.get("ordered_missing_steps")
    if isinstance(ordered, list):
        summary["ordered_missing_step_ids"] = [
            str(step.get("id") or "").strip()
            for step in ordered
            if isinstance(step, Mapping) and str(step.get("id") or "").strip()
        ]
    return summary


def _runtime_next_legal_operation(action: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(action, Mapping) or not action:
        return {
            "schema_version": "contract_state.next_legal_operation.v1",
            "id": "",
            "operation": "",
            "source": "contract_state",
            "status": "none",
            "close_gate_is_navigation_source": False,
        }
    action_id = str(action.get("id") or "").strip()
    operation = str(action.get("action") or action_id or "").strip()
    hint = _mapping(action.get("timeline_append_hint"))
    role_policy = _mapping(hint.get("role_bound_prefill_policy"))
    result = {
        "schema_version": "contract_state.next_legal_operation.v1",
        "id": action_id,
        "operation": operation,
        "requirement_id": str(action.get("requirement_id") or action_id).strip(),
        "source": str(action.get("source") or "contract_state").strip(),
        "precedence": str(action.get("precedence") or "").strip(),
        "actor_role": str(hint.get("actor_role") or "").strip(),
        "execution_owner_role": str(
            role_policy.get("execution_owner_role") or hint.get("actor_role") or ""
        ).strip(),
        "contract_execution_id": str(action.get("contract_execution_id") or "").strip(),
        "contract_chain_id": str(action.get("contract_chain_id") or "").strip(),
        "backlog_id": str(action.get("backlog_id") or "").strip(),
        "route_token_ref_present": bool(str(action.get("route_token_ref") or "").strip()),
        "close_gate_is_navigation_source": False,
        "close_gate_role": "final_verifier",
        "prefill_state": role_policy,
    }
    for key in (
        "candidate_contract_templates",
        "supported_contract_roles",
        "entrypoints",
        "recovery_state",
        "dirty_files",
        "dirty_target_files",
    ):
        value = action.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, Mapping):
            result[key] = dict(value)
        elif isinstance(value, list):
            result[key] = list(value)
        else:
            result[key] = value
    return result


def _runtime_contract_hints(
    *,
    root: Mapping[str, Any],
    active_execution: Mapping[str, Any],
    active_lane_contract: Mapping[str, Any],
    next_legal_action: Mapping[str, Any] | None,
    lane_contract_executions: list[dict[str, Any]],
    route_binding: Mapping[str, Any],
) -> dict[str, Any]:
    next_operation = _runtime_next_legal_operation(next_legal_action)
    template_hints = _mapping(root.get("runtime_contract_hints"))
    active = dict(active_execution) if isinstance(active_execution, Mapping) else {}
    lane = dict(active_lane_contract) if isinstance(active_lane_contract, Mapping) else {}
    return {
        "schema_version": "contract_state.runtime_contract_hints.v1",
        "template_hints": template_hints,
        "active_contract_id": str(active.get("contract_id") or "").strip(),
        "active_contract_template_id": str(
            active.get("contract_template_id") or ""
        ).strip(),
        "active_contract_execution_id": str(
            active.get("contract_execution_id") or ""
        ).strip(),
        "active_contract_chain_id": str(active.get("contract_chain_id") or "").strip(),
        "active_lane_role": str(lane.get("role") or "root").strip(),
        "active_lane_execution_id": str(lane.get("contract_execution_id") or "").strip(),
        "route_identity": _public_route_identity(route_binding),
        "next_legal_operation": next_operation,
        "prefill_state": next_operation.get("prefill_state") or {},
        "role_boundaries": [
            {
                "role": str(item.get("role") or "").strip(),
                "contract_execution_id": str(
                    item.get("contract_execution_id") or ""
                ).strip(),
                "contract_template_id": str(
                    item.get("contract_template_id") or ""
                ).strip(),
                "next_legal_action": _next_action_summary(
                    _mapping(item.get("next_legal_action"))
                ),
            }
            for item in lane_contract_executions
        ],
        "close_gate_policy": {
            "schema_version": "contract_state.close_gate_runtime_policy.v1",
            "role": "final_verifier",
            "drives_next_legal_operation": False,
            "navigation_source": "contract_state.next_legal_operation",
            "finalizes_after": [
                "implementation_evidence",
                "verification_evidence",
                "close_ready",
            ],
        },
    }


def _executable_contract_envelope(
    *,
    active_execution: Mapping[str, Any],
    active_lane_contract: Mapping[str, Any],
    next_legal_action: Mapping[str, Any] | None,
    runtime_hints: Mapping[str, Any],
) -> dict[str, Any]:
    active = dict(active_execution) if isinstance(active_execution, Mapping) else {}
    lane = dict(active_lane_contract) if isinstance(active_lane_contract, Mapping) else {}
    next_operation = _mapping(runtime_hints.get("next_legal_operation"))
    return {
        "schema_version": "runtime_context.executable_contract_envelope.v1",
        "advisory_only": True,
        "proof_policy": (
            "runtime next_legal_operation/current-state/access_audit are advisory; "
            "actor-authored timeline or runtime-context facade evidence remains "
            "required for contract proof"
        ),
        "active_contract_id": str(active.get("contract_id") or "").strip(),
        "active_contract_template_id": str(
            active.get("contract_template_id") or ""
        ).strip(),
        "contract_execution": {
            "schema_version": "runtime_context.contract_execution_identity.v1",
            "contract_execution_id": str(
                active.get("contract_execution_id") or ""
            ).strip(),
            "contract_chain_id": str(active.get("contract_chain_id") or "").strip(),
            "parent_contract_execution_id": str(
                active.get("parent_contract_execution_id") or ""
            ).strip(),
            "successor_contract_execution_id": str(
                lane.get("contract_execution_id") or ""
            ).strip()
            if str(lane.get("role") or "") != "root"
            else "",
            "contract_revision_id": str(
                active.get("contract_revision_id") or ""
            ).strip(),
            "contract_hash": str(active.get("contract_hash") or "").strip(),
            "contract_hash_source": (
                "branch_contract_revision.payload."
                "revision_receipt.canonical_visible_contract_text_hash"
            ),
        },
        "role": str(lane.get("role") or "root").strip(),
        "prefill_state": _mapping(runtime_hints.get("prefill_state")),
        "next_legal_operation": next_operation,
        "next_legal_action": next_operation.get("operation") or "",
        "close_gate_policy": _mapping(runtime_hints.get("close_gate_policy")),
    }


def _requirement_steps(
    root: Mapping[str, Any],
    backlog_row: Mapping[str, Any],
    *,
    default_required_evidence: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw = root.get("evidence_requirements") or root.get("required_evidence") or []
    raw_from_default = False
    if not raw:
        raw = default_required_evidence
        raw_from_default = True
    required: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        step = _normalize_requirement(
            item,
            index=index,
            semantic_default_order=raw_from_default,
        )
        if step:
            required.append(step)

    conditional_groups: list[dict[str, Any]] = []
    conditional_raw = root.get("conditional_required_evidence") or []
    if isinstance(conditional_raw, Mapping):
        conditional_raw = [conditional_raw]
    if isinstance(conditional_raw, list):
        for group_index, group in enumerate(conditional_raw):
            if not isinstance(group, Mapping):
                continue
            condition = _mapping(group.get("condition"))
            evidence = group.get("evidence") or group.get("required_evidence") or []
            steps: list[dict[str, Any]] = []
            for offset, item in enumerate(evidence):
                step = _normalize_requirement(
                    item,
                    index=len(required) + group_index + offset,
                    condition=condition,
                    conditional=True,
                )
                if step:
                    steps.append(step)
            active = _condition_matches(condition, root=root, backlog_row=backlog_row)
            conditional_groups.append(
                {
                    "condition": dict(condition),
                    "active": active,
                    "evidence": [step["id"] for step in steps],
                    "steps": steps,
                }
            )
            if active:
                required.extend(steps)
    return required, conditional_groups


def _payload_declares_requirement(value: Any, requirement_id: str, *, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if isinstance(value, Mapping):
        for key in (
            "requirement_id",
            "requirement_ids",
            "evidence_id",
            "evidence_ids",
            "contract_evidence_id",
            "contract_evidence_ids",
            "satisfied_evidence",
            "completed_evidence",
            "present_required_evidence",
        ):
            if requirement_id in _string_list(value.get(key)):
                return True
        for child in value.values():
            if _payload_declares_requirement(child, requirement_id, depth=depth + 1):
                return True
    elif isinstance(value, list | tuple | set):
        for child in value:
            if _payload_declares_requirement(child, requirement_id, depth=depth + 1):
                return True
    return False


def _qa_verification_event_satisfies_requirement(
    event: Mapping[str, Any],
    requirement_id: str,
) -> bool:
    if requirement_id not in _QA_REQUIREMENT_EVENT_KIND_ALIASES:
        return False
    event_kind = str(event.get("event_kind") or "").strip()
    if event_kind != "verification":
        return False
    phase = str(event.get("phase") or "").strip().lower().replace("-", "_").replace(".", "_")
    event_type = (
        str(event.get("event_type") or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(".", "_")
    )
    marker = f"{phase} {event_type}"
    if not any(
        token in marker
        for token in (
            "independent_verification",
            "independent_qa",
            "qa_verification",
            "qa_review",
        )
    ):
        return False
    return any(
        _payload_declares_requirement(payload, requirement_id)
        for payload in _event_payloads(event)
    )


def _event_deep_text(event: Mapping[str, Any], field_names: set[str]) -> str:
    for field_name in field_names:
        token = str(event.get(field_name) or "").strip()
        if token:
            return token
    for payload in _event_payloads(event):
        token = _payload_field_value(payload, field_names)
        if token:
            return token
    return ""


def _multiple_attempt_lineage_bridge_step(
    events: list[dict[str, Any]],
    *,
    backlog_id: str,
) -> dict[str, Any] | None:
    """Project explicit bridge repair before generic implementation work.

    Root rows can have several bounded worker attempts that share the same
    backlog/merge lane. When no accepted lineage bridge exists yet, the next
    action should document that sibling relationship before asking for another
    implementation event.
    """

    accepted_bridge_kinds = {
        "cross_ref_lineage_bridge",
        "lineage_bridge",
        "mf_cross_ref_lineage_bridge",
    }
    for event in events:
        event_kind = str(event.get("event_kind") or "").strip()
        status = str(event.get("status") or "").strip().lower()
        if event_kind in accepted_bridge_kinds and status in CONTRACT_PASS_STATUSES:
            return None

    attempt_task_ids: set[str] = set()
    for event in events:
        event_kind = str(event.get("event_kind") or "").strip()
        status = str(event.get("status") or "").strip().lower()
        if status and status not in CONTRACT_PASS_STATUSES:
            continue
        task_id = _event_deep_text(event, {"task_id"})
        parent_task_id = _event_deep_text(event, {"parent_task_id", "root_task_id"})
        runtime_context_id = _event_deep_text(event, {"runtime_context_id"})
        if not runtime_context_id and event_kind not in {
            "mf_subagent_dispatch",
            "mf_subagent_startup",
            "route_context",
            "route_action_precheck",
        }:
            continue
        if not task_id or task_id == backlog_id:
            continue
        if backlog_id and parent_task_id != backlog_id:
            continue
        attempt_task_ids.add(task_id)

    if len(attempt_task_ids) <= 1:
        return None
    return {
        "id": "cross_ref_lineage_bridge",
        "action": "record_cross_ref_lineage_bridge",
        "source": "contract_state",
        "precedence": "attempt_lineage_bridge_required",
        "order": 250,
        "reason": (
            "multiple worker attempt lineages exist for this root row without "
            "an accepted bridge"
        ),
        "attempt_task_ids": sorted(attempt_task_ids),
    }


def _requirement_requires_event_kind_match(requirement: Mapping[str, Any]) -> bool:
    policy = str(
        requirement.get("match_policy")
        or requirement.get("evidence_match_policy")
        or requirement.get("satisfies_by")
        or ""
    ).strip().lower()
    if policy in {
        "event_kind",
        "event_kind_only",
        "canonical_event_kind",
        "direct_timeline_event",
    }:
        return True
    if policy in {
        "payload.requirement_id",
        "payload_requirement_id",
        "requirement_id",
        "event_kind_or_requirement_id",
    }:
        return False
    accepted_kinds = set(_string_list(requirement.get("accepted_event_kinds")))
    return bool(accepted_kinds.intersection(_DIRECT_TIMELINE_APPEND_EVENT_KINDS))


def _event_satisfies_requirement(
    event: Mapping[str, Any],
    requirement: Mapping[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    requirement_id = str(requirement.get("id") or "").strip()
    if not requirement_id:
        return False, None
    status = str(event.get("status") or "").strip().lower()
    accepted_statuses = set(_string_list(requirement.get("accepted_statuses")))
    if status and accepted_statuses and status not in accepted_statuses:
        return False, None
    event_kind = str(event.get("event_kind") or "").strip()
    accepted_kinds = set(_string_list(requirement.get("accepted_event_kinds")))
    matched = event_kind in accepted_kinds
    if not matched:
        if _qa_verification_event_satisfies_requirement(event, requirement_id):
            matched = True
        elif not _requirement_requires_event_kind_match(requirement):
            matched = any(
                _payload_declares_requirement(payload, requirement_id)
                for payload in _event_payloads(event)
            )
    if not matched:
        return False, None
    required_execution_id = str(
        requirement.get("contract_execution_id")
        or requirement.get("required_contract_execution_id")
        or ""
    ).strip()
    if required_execution_id:
        event_id = _event_ref_id(event)
        accepted_event_ids = set(_string_list(requirement.get("accepted_event_ids")))
        execution_ids = {
            str(event.get(field) or "").strip()
            for field in (
                "active_contract_execution_id",
                "contract_execution_id",
                "parent_contract_execution_id",
                "reviewed_contract_execution_id",
                "successor_contract_execution_id",
            )
            if str(event.get(field) or "").strip()
        }
        for payload in _event_payloads(event):
            for field in (
                "active_contract_execution_id",
                "contract_execution_id",
                "parent_contract_execution_id",
                "reviewed_contract_execution_id",
                "successor_contract_execution_id",
            ):
                execution_id = _payload_field_value(payload, {field})
                if execution_id:
                    execution_ids.add(execution_id)
        if (
            required_execution_id not in execution_ids
            and event_id not in accepted_event_ids
        ):
            return False, None
    strict_identity_matched, strict_identity = _event_matches_strict_requirement_identity(
        event,
        requirement,
    )
    if not strict_identity_matched:
        return False, None
    source = {
        "event_id": event.get("id") or event.get("event_id") or "",
        "event_kind": event_kind,
        "status": str(event.get("status") or ""),
        "phase": str(event.get("phase") or ""),
    }
    if required_execution_id:
        source["contract_execution_id"] = required_execution_id
    if strict_identity:
        source["strict_identity"] = strict_identity
    return True, source


def _route_identity_hash(route_binding: Mapping[str, Any]) -> str:
    payload = {
        key: str(route_binding.get(key) or "")
        for key in _ROUTE_BINDING_FIELD_NAMES
        if route_binding.get(key)
    }
    if not payload:
        return ""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _execution_id(
    *,
    project_id: str,
    backlog_id: str,
    contract_id: str,
    template_id: str,
    revision_id: str,
    discriminator: str = "",
) -> str:
    seed = "|".join(
        [project_id, backlog_id, contract_id, template_id, revision_id, discriminator]
    )
    return "cex-" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def _contract_chain_id(
    *,
    project_id: str,
    backlog_id: str,
    root: Mapping[str, Any],
    binding: Mapping[str, Any],
    active_contract_execution_id: str,
) -> str:
    explicit = str(
        root.get("contract_chain_id") or binding.get("contract_chain_id") or ""
    ).strip()
    if explicit:
        return explicit
    if not active_contract_execution_id:
        return ""
    seed = "|".join([project_id, backlog_id, active_contract_execution_id])
    return "cchain-" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def _successor_candidates(
    root: Mapping[str, Any],
    *,
    contract_chain_id: str,
    active_contract_execution: Mapping[str, Any],
    contract_complete: bool,
) -> list[dict[str, Any]]:
    policy = root.get("successor_contract_policy")
    raw_candidates: Any = []
    if isinstance(policy, Mapping):
        raw_candidates = policy.get("candidates") or policy.get(
            "successor_contract_candidates"
        )
    if not raw_candidates:
        raw_candidates = root.get("successor_contract_candidates") or []
    if isinstance(raw_candidates, Mapping):
        raw_candidates = list(raw_candidates.values())
    if not isinstance(raw_candidates, list | tuple):
        return []

    parent_execution_id = str(
        active_contract_execution.get("contract_execution_id") or ""
    ).strip()
    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(raw_candidates):
        if isinstance(item, Mapping):
            candidate = dict(item)
            template_id = str(
                candidate.get("contract_template_id")
                or candidate.get("template_id")
                or candidate.get("contract_template")
                or candidate.get("id")
                or ""
            ).strip()
        else:
            template_id = str(item or "").strip()
            candidate = {"contract_template_id": template_id}
        if not template_id:
            continue
        candidate.setdefault("id", template_id)
        candidate["contract_template_id"] = template_id
        candidate.setdefault("contract_id", template_id)
        candidate.setdefault("action", "select_successor_contract")
        candidate["contract_chain_id"] = contract_chain_id
        candidate["parent_contract_execution_id"] = parent_execution_id
        candidate["available"] = bool(contract_complete)
        if not contract_complete:
            candidate["blocked_by"] = ["active_contract_incomplete"]
        candidate.setdefault("order", index)
        candidates.append(candidate)
    return candidates


def _successor_policy_summary(root: Mapping[str, Any]) -> dict[str, Any]:
    policy = root.get("successor_contract_policy")
    if not isinstance(policy, Mapping):
        return {}
    return {
        str(key): value
        for key, value in policy.items()
        if key not in {"candidates", "successor_contract_candidates"}
    }


def _policy_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "required"}
    return bool(value)


_QA_FOLLOWUP_DECISIONS = {"block", "pass_with_followups"}
_QA_FOLLOWUP_FINDING_SEVERITIES = {"blocking", "major", "critical"}


def _normal_signal_token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _meaningful_followup_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return _normal_signal_token(value) not in {
            "",
            "0",
            "false",
            "none",
            "null",
            "no",
            "n",
            "off",
        }
    if isinstance(value, Mapping):
        return any(_meaningful_followup_value(child) for child in value.values())
    if isinstance(value, list | tuple | set):
        return any(_meaningful_followup_value(child) for child in value)
    return bool(value)


def _findings_include_blocking_followup(value: Any, *, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(value, Mapping):
        severity = _normal_signal_token(value.get("severity"))
        if severity in _QA_FOLLOWUP_FINDING_SEVERITIES:
            return True
        return any(
            _findings_include_blocking_followup(child, depth=depth + 1)
            for child in value.values()
        )
    if isinstance(value, list | tuple | set):
        return any(
            _findings_include_blocking_followup(child, depth=depth + 1)
            for child in value
        )
    return False


def _qa_followup_signal_present(value: Any, *, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_token = _normal_signal_token(key)
            child_token = _normal_signal_token(child)
            if (
                key_token in {"gate_decision", "decision", "result", "output"}
                and child_token in _QA_FOLLOWUP_DECISIONS
            ):
                return True
            if key_token in {"followup_needed", "requires_followup"} and _policy_truthy(
                child
            ):
                return True
            if key_token == "follow_up_backlog" and _meaningful_followup_value(child):
                return True
            if key_token == "findings" and _findings_include_blocking_followup(child):
                return True
            if _qa_followup_signal_present(child, depth=depth + 1):
                return True
    elif isinstance(value, list | tuple | set):
        return any(
            _qa_followup_signal_present(child, depth=depth + 1) for child in value
        )
    return False


def _event_mentions_contract_execution(
    event: Mapping[str, Any],
    contract_execution_id: str,
) -> bool:
    execution_id = str(contract_execution_id or "").strip()
    if not execution_id:
        return True
    for field in (
        "active_contract_execution_id",
        "contract_execution_id",
        "parent_contract_execution_id",
        "reviewed_contract_execution_id",
        "successor_contract_execution_id",
    ):
        if str(event.get(field) or "").strip() == execution_id:
            return True
    for payload in _event_payloads(event):
        for field in (
            "active_contract_execution_id",
            "contract_execution_id",
            "parent_contract_execution_id",
            "reviewed_contract_execution_id",
            "successor_contract_execution_id",
        ):
            if _payload_field_value(payload, {field}) == execution_id:
                return True
    return False


def _qa_followup_needed_from_completed_evidence(
    events: list[dict[str, Any]],
    *,
    requirement_state: Mapping[str, Any] | None,
    contract_execution_id: str,
) -> bool:
    state = requirement_state or {}
    if state and not state.get("contract_complete"):
        return False
    completed_event_ids = {
        str(item.get("event_id") or "").strip()
        for item in state.get("completed_evidence") or []
        if isinstance(item, Mapping) and str(item.get("event_id") or "").strip()
    }
    matched_events: list[dict[str, Any]] = []
    if completed_event_ids:
        matched_events = [
            event
            for event in events
            if str(event.get("id") or event.get("event_id") or "").strip()
            in completed_event_ids
        ]
    if not matched_events:
        matched_events = [
            event
            for event in events
            if str(event.get("event_kind") or "").strip() in _QA_TIMELINE_EVENT_KINDS
            and _event_mentions_contract_execution(event, contract_execution_id)
        ]
    for event in matched_events:
        for key in ("payload", "verification", "result", "output", "decision"):
            if key not in event:
                continue
            value = event.get(key)
            if (
                key in {"result", "output", "decision"}
                and _normal_signal_token(value) in _QA_FOLLOWUP_DECISIONS
            ):
                return True
            if _qa_followup_signal_present(value):
                return True
    return False


def _successor_selection_required(
    root: Mapping[str, Any],
    *,
    events: list[dict[str, Any]] | None = None,
    requirement_state: Mapping[str, Any] | None = None,
    contract_execution_id: str = "",
) -> bool:
    template_id = str(
        root.get("contract_template_id") or root.get("template_id") or ""
    ).strip()
    policy = root.get("successor_contract_policy")
    if template_id == _QA_EVIDENCE_CONTRACT_TEMPLATE_ID:
        return bool(
            isinstance(policy, Mapping)
            and _policy_truthy(policy.get("selection_required_when_followup_needed"))
            and _qa_followup_needed_from_completed_evidence(
                events or [],
                requirement_state=requirement_state,
                contract_execution_id=contract_execution_id,
            )
        )
    if not isinstance(policy, Mapping):
        return bool(root.get("successor_contract_candidates"))
    if _policy_truthy(policy.get("selection_required_when_followup_needed")):
        return False
    return bool(
        _policy_truthy(policy.get("selection_required_after_hotfix_complete"))
        or _policy_truthy(policy.get("selection_required"))
        or policy.get("selection_action")
        or policy.get("candidates")
        or policy.get("successor_contract_candidates")
        or root.get("successor_contract_candidates")
    )


def _successor_lane_role(depth: int) -> str:
    if depth == 1:
        return "successor"
    if depth == 2:
        return "nested_successor"
    if depth == 3:
        return "deep_successor"
    return f"successor_depth_{depth}"


def _successor_missing_steps_source(depth: int) -> str:
    if depth == 1:
        return "selected_successor_contract_state"
    if depth == 2:
        return "nested_successor_contract_state"
    if depth == 3:
        return "deep_successor_contract_state"
    return f"successor_depth_{depth}_contract_state.v1"


def _successor_contract_from_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    successor: dict[str, Any] = {}
    for key in _SUCCESSOR_CONTRACT_CONTAINER_KEYS:
        nested = value.get(key)
        if isinstance(nested, Mapping):
            successor.update(
                {
                    field: nested_value
                    for field, nested_value in nested.items()
                    if field in _SUCCESSOR_CONTRACT_FIELD_NAMES
                }
            )
    for field in _SUCCESSOR_CONTRACT_FIELD_NAMES:
        field_value = value.get(field)
        if field_value:
            successor[field] = field_value
    if not successor:
        return {}
    template_id = str(
        successor.get("successor_contract_template_id")
        or successor.get("contract_template_id")
        or successor.get("template_id")
        or successor.get("contract_template")
        or ""
    ).strip()
    if template_id:
        successor["contract_template_id"] = template_id
        successor.setdefault("contract_id", template_id)
    contract_execution_id = str(
        successor.get("successor_contract_execution_id")
        or successor.get("contract_execution_id")
        or ""
    ).strip()
    if contract_execution_id:
        successor["successor_contract_execution_id"] = contract_execution_id
        successor["contract_execution_id"] = contract_execution_id
    successor_backlog_id = str(
        successor.get("successor_backlog_id") or successor.get("backlog_id") or ""
    ).strip()
    if successor_backlog_id:
        successor["successor_backlog_id"] = successor_backlog_id
    return successor


def _successor_contract_bindings(
    events: list[dict[str, Any]],
    *,
    project_id: str,
    backlog_id: str,
    contract_chain_id: str,
    active_contract_execution: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if not contract_chain_id:
        return []
    parent_execution_id = str(
        active_contract_execution.get("contract_execution_id") or ""
    ).strip()
    if not parent_execution_id:
        return []
    successors: list[dict[str, Any]] = []
    for event in events:
        event_kind = str(event.get("event_kind") or "").strip()
        if event_kind not in _CONTRACT_BINDING_EVENT_KINDS:
            continue
        for payload in _event_payloads(event):
            successor = _successor_contract_from_mapping(payload)
            if not successor:
                continue
            successor_chain_id = str(successor.get("contract_chain_id") or "").strip()
            if successor_chain_id != contract_chain_id:
                continue
            successor_parent_id = str(
                successor.get("parent_contract_execution_id") or ""
            ).strip()
            if successor_parent_id != parent_execution_id:
                continue
            template_id = str(successor.get("contract_template_id") or "").strip()
            contract_id = str(successor.get("contract_id") or template_id).strip()
            if not (template_id or contract_id):
                continue
            event_id = event.get("id") or event.get("event_id") or ""
            handoff_event_id = str(successor.get("handoff_event_id") or event_id or "")
            successor_backlog_id = str(
                successor.get("successor_backlog_id") or backlog_id
            ).strip()
            execution_id = str(
                successor.get("successor_contract_execution_id")
                or successor.get("contract_execution_id")
                or ""
            ).strip()
            if not execution_id:
                execution_id = _execution_id(
                    project_id=project_id,
                    backlog_id=successor_backlog_id,
                    contract_id=contract_id,
                    template_id=template_id,
                    revision_id=str(successor.get("contract_revision_id") or ""),
                    discriminator=f"successor|{parent_execution_id}|{handoff_event_id}|{event_id}",
                )
            normalized = {
                **successor,
                "schema_version": "successor_contract_binding.v1",
                "contract_chain_id": contract_chain_id,
                "parent_contract_execution_id": str(
                    successor.get("parent_contract_execution_id")
                    or parent_execution_id
                ),
                "successor_contract_execution_id": execution_id,
                "contract_execution_id": execution_id,
                "contract_id": contract_id,
                "contract_template_id": template_id,
                "successor_backlog_id": successor_backlog_id,
                "handoff_event_id": handoff_event_id,
                "status": str(event.get("status") or successor.get("status") or ""),
                "source_event_id": event_id,
            }
            successors.append(normalized)
    return successors


def _contract_template_map(
    contract_templates: Mapping[str, Mapping[str, Any]]
    | list[Mapping[str, Any]]
    | tuple[Mapping[str, Any], ...]
    | None,
) -> dict[str, dict[str, Any]]:
    if isinstance(contract_templates, Mapping):
        return {
            str(key): dict(value)
            for key, value in contract_templates.items()
            if isinstance(value, Mapping)
        }
    if isinstance(contract_templates, list | tuple):
        templates: dict[str, dict[str, Any]] = {}
        for item in contract_templates:
            if not isinstance(item, Mapping):
                continue
            template_id = str(item.get("template_id") or "").strip()
            if template_id:
                templates[template_id] = dict(item)
        return templates
    return {}


def _successor_contract_root(
    successor: Mapping[str, Any],
    *,
    contract_templates: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    template_id = str(successor.get("contract_template_id") or "").strip()
    template = contract_templates.get(template_id) or {}
    root = dict(template) if isinstance(template, Mapping) else {}
    for key in (
        "required_evidence",
        "evidence_requirements",
        "conditional_required_evidence",
        "successor_contract_policy",
        "successor_contract_candidates",
    ):
        if key in successor:
            root[key] = successor[key]
    root["contract_id"] = str(successor.get("contract_id") or template_id or "").strip()
    root["contract_template_id"] = template_id
    root["template_id"] = template_id
    root["contract_execution_id"] = str(
        successor.get("successor_contract_execution_id")
        or successor.get("contract_execution_id")
        or ""
    ).strip()
    root["contract_chain_id"] = str(successor.get("contract_chain_id") or "").strip()
    root["contract_revision_id"] = str(successor.get("contract_revision_id") or "").strip()
    root["handoff_event_id"] = str(successor.get("handoff_event_id") or "").strip()
    root["parent_contract_execution_id"] = str(
        successor.get("parent_contract_execution_id") or ""
    ).strip()
    root["state"] = str(successor.get("state") or successor.get("status") or "selected")
    return root


def _successor_active_execution(
    successor: Mapping[str, Any],
    *,
    project_id: str,
    backlog_id: str,
    contract_chain_id: str,
    parent_execution_id: str,
    projection_watermark: Any,
    route_identity_hash: str,
    route_token_ref: str,
) -> dict[str, Any]:
    execution_id = str(
        successor.get("successor_contract_execution_id")
        or successor.get("contract_execution_id")
        or ""
    ).strip()
    return {
        "schema_version": "active_contract_execution.v1",
        "project_id": project_id,
        "backlog_id": successor.get("successor_backlog_id") or backlog_id,
        "contract_execution_id": execution_id,
        "contract_id": successor.get("contract_id") or "",
        "contract_template_id": successor.get("contract_template_id") or "",
        "contract_instance_id": successor.get("contract_instance_id") or "",
        "contract_revision_id": successor.get("contract_revision_id") or "",
        "state": successor.get("state") or successor.get("status") or "selected",
        "projection_watermark": projection_watermark,
        "route_identity_hash": route_identity_hash,
        "route_token_ref": route_token_ref,
        "contract_chain_id": contract_chain_id,
        "parent_contract_execution_id": (
            successor.get("parent_contract_execution_id") or parent_execution_id
        ),
    }


def _contract_requirements_state(
    events: list[dict[str, Any]],
    *,
    root: Mapping[str, Any],
    backlog_row: Mapping[str, Any],
    default_required_evidence: list[str],
    contract_execution_id: str = "",
    missing_precedence: str = "active_contract_missing_step",
) -> dict[str, Any]:
    requirement_steps, conditional_groups = _requirement_steps(
        root,
        backlog_row,
        default_required_evidence=default_required_evidence,
    )
    handoff_event_id = str(root.get("handoff_event_id") or "").strip()
    handoff_event = next(
        (
            event
            for event in events
            if handoff_event_id and _event_ref_id(event) == handoff_event_id
        ),
        None,
    )
    scoped_steps: list[dict[str, Any]] = []
    for requirement in requirement_steps:
        step = dict(requirement)
        if contract_execution_id:
            step.setdefault("contract_execution_id", contract_execution_id)
        if handoff_event_id and handoff_event is not None:
            handoff_probe = dict(step)
            handoff_probe.pop("contract_execution_id", None)
            handoff_probe.pop("required_contract_execution_id", None)
            handoff_probe.pop("accepted_event_ids", None)
            handoff_matches_requirement, _ = _event_satisfies_requirement(
                handoff_event,
                handoff_probe,
            )
        else:
            handoff_matches_requirement = False
        if handoff_matches_requirement:
            accepted_event_ids = _string_list(step.get("accepted_event_ids"))
            if handoff_event_id not in accepted_event_ids:
                accepted_event_ids.append(handoff_event_id)
            step["accepted_event_ids"] = accepted_event_ids
        if missing_precedence:
            step["precedence"] = missing_precedence
        scoped_steps.append(step)

    completed_sources: dict[str, dict[str, Any]] = {}
    for requirement in scoped_steps:
        requirement_id = str(requirement.get("id") or "")
        for event in events:
            matched, source = _event_satisfies_requirement(event, requirement)
            if matched and source:
                completed_sources[requirement_id] = source
                break
    completed_ids = set(completed_sources)
    missing_steps: list[dict[str, Any]] = []
    blocked_steps: list[dict[str, Any]] = []
    for requirement in scoped_steps:
        requirement_id = str(requirement.get("id") or "")
        if requirement_id in completed_ids:
            continue
        step = dict(requirement)
        missing_prerequisites = [
            prereq
            for prereq in _string_list(requirement.get("prerequisite_ids"))
            if prereq not in completed_ids
        ]
        if missing_prerequisites:
            step["blocked_by"] = missing_prerequisites
            blocked_steps.append(step)
        missing_steps.append(step)

    ordered_next_steps = sorted(
        missing_steps,
        key=lambda step: int(step.get("order") or 0),
    )
    requirements_explicit = bool(
        root.get("required_evidence")
        or root.get("evidence_requirements")
        or root.get("conditional_required_evidence")
    )
    return {
        "requirements_explicit": requirements_explicit,
        "required_evidence": [step["id"] for step in scoped_steps],
        "conditional_required_evidence": conditional_groups,
        "completed_evidence": [
            {"id": requirement_id, **source}
            for requirement_id, source in completed_sources.items()
        ],
        "missing_evidence": [step["id"] for step in ordered_next_steps],
        "blocked_evidence": [step["id"] for step in blocked_steps],
        "ordered_next_steps": ordered_next_steps,
        "contract_complete": bool(requirements_explicit and not ordered_next_steps),
    }


def _latest_passing_event_with_kind(
    events: list[dict[str, Any]],
    event_kind: str,
) -> dict[str, Any]:
    matches = [
        event
        for event in events
        if _event_kind(event) == event_kind
        and str(event.get("status") or "").strip() in CONTRACT_PASS_STATUSES
    ]
    if not matches:
        return {}
    return max(matches, key=_event_numeric_id)


def _contract_first_selection_step(
    *,
    backlog_id: str,
    project_id: str,
) -> dict[str, Any]:
    return {
        "id": "select_or_enter_contract",
        "action": "select_or_enter_contract",
        "detail": (
            "select or enter an onboard, hotfix, parallel-worker, QA, merge, "
            "or audit-close contract before mutating governed files"
        ),
        "source": "contract_state",
        "precedence": "contract_first_pre_mutation",
        "order": 1,
        "backlog_id": backlog_id,
        "project_id": project_id,
        "accepted_event_kinds": [
            "contract_binding",
            "contract_revision_created",
            "contract_bound",
            "hotfix_entered",
        ],
        "accepted_statuses": sorted(CONTRACT_PASS_STATUSES),
        "candidate_contract_templates": list(_CONTRACT_FIRST_CANDIDATE_TEMPLATES),
        "supported_contract_roles": list(_CONTRACT_FIRST_SUPPORTED_ROLES),
        "entrypoints": {
            "onboard_contract.v1": (
                "append contract_binding / contract_revision_created evidence"
            ),
            "observer_hotfix_direct_mutation.v1": (
                "POST /api/projects/{project_id}/hotfix/enter"
            ),
            "mf_parallel.v1": "instantiate and bind a role-scoped parallel contract",
            _QA_EVIDENCE_CONTRACT_TEMPLATE_ID: (
                "create a QA-owned successor contract/evidence lane"
            ),
            _AUDIT_CLOSE_CONTRACT_TEMPLATE_ID: (
                "select only for audited recovery closure"
            ),
            "merge": "select a merge/close successor contract before privileged merge",
        },
    }


def _legacy_hotfix_post_action_step(
    *,
    backlog_id: str,
    project_id: str,
    hotfix_event: Mapping[str, Any],
) -> dict[str, Any]:
    pre_reason_event_id = _event_ref_id(hotfix_event)
    return {
        "id": "hotfix_post_action_summary",
        "action": "record_hotfix_under_action",
        "detail": (
            "record the direct observer hotfix action summary before verification "
            "or close"
        ),
        "source": "contract_state",
        "precedence": "legacy_hotfix_direct_mutation_step",
        "order": 2,
        "backlog_id": backlog_id,
        "project_id": project_id,
        "pre_reason_event_id": pre_reason_event_id,
        "accepted_event_kinds": ["hotfix_under_action"],
        "accepted_statuses": sorted(CONTRACT_PASS_STATUSES),
    }


def build_contract_state_projection(
    events: list[dict[str, Any]] | None,
    contract: Mapping[str, Any] | None = None,
    backlog_row: Mapping[str, Any] | None = None,
    *,
    contract_projection: Mapping[str, Any] | None = None,
    contract_templates: Mapping[str, Mapping[str, Any]]
    | list[Mapping[str, Any]]
    | tuple[Mapping[str, Any], ...]
    | None = None,
    default_required_evidence: list[str] | None = None,
    schema_version: str = CONTRACT_STATE_PROJECTION_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Fold backlog contract JSON and timeline rows into a read-only state view."""

    rows = [event for event in (events or []) if isinstance(event, dict)]
    root = _contract_root(contract)
    row = dict(backlog_row or {})
    project_id = str(
        row.get("project_id")
        or root.get("project_id")
        or root.get("target_project_id")
        or ""
    ).strip()
    if not project_id:
        for event in rows:
            project_id = str(event.get("project_id") or "").strip()
            if project_id:
                break
    backlog_id = str(row.get("bug_id") or row.get("backlog_id") or "").strip()
    if not backlog_id:
        for event in rows:
            backlog_id = str(event.get("backlog_id") or "").strip()
            if backlog_id:
                break
    binding = _latest_contract_binding(rows, root)
    template_id = str(
        binding.get("contract_template_id")
        or binding.get("contract_template")
        or binding.get("template_id")
        or root.get("contract_template_id")
        or root.get("contract_template")
        or root.get("template_id")
        or ""
    ).strip()
    contract_id = str(
        binding.get("contract_id")
        or root.get("contract_id")
        or template_id
        or binding.get("contract_instance_id")
        or root.get("contract_instance_id")
        or ""
    ).strip()
    current_revision_id = str(
        binding.get("contract_revision_id")
        or binding.get("current_revision_id")
        or binding.get("revision_id")
        or binding.get("runtime_contract_revision_id")
        or ""
    ).strip() or _latest_contract_revision_id(rows, root)
    default_required = default_required_evidence or []
    requirements_explicit = bool(
        root.get("required_evidence")
        or root.get("evidence_requirements")
        or root.get("conditional_required_evidence")
    )
    requirement_steps, conditional_groups = _requirement_steps(
        root,
        row,
        default_required_evidence=default_required,
    )
    has_explicit_contract = bool(
        root
        and (
            contract_id
            or template_id
            or current_revision_id
            or root.get("required_evidence")
            or root.get("evidence_requirements")
            or root.get("conditional_required_evidence")
        )
    )
    legacy_no_contract = not bool(has_explicit_contract or binding)
    binding_state = str(
        binding.get("contract_state")
        or binding.get("contract_status")
        or binding.get("state")
        or binding.get("status")
        or root.get("contract_state")
        or root.get("contract_status")
        or root.get("state")
        or root.get("status")
        or ""
    ).strip()
    projection = dict(contract_projection or {})
    if not binding_state and (contract_id or current_revision_id) and not legacy_no_contract:
        binding_state = "bound"
    state = binding_state or str(projection.get("status") or "no_contract")

    completed_sources: dict[str, dict[str, Any]] = {}
    for requirement in requirement_steps:
        requirement_id = str(requirement.get("id") or "")
        for event in rows:
            matched, source = _event_satisfies_requirement(event, requirement)
            if matched and source:
                completed_sources[requirement_id] = source
                break
    completed_ids = set(completed_sources)
    missing_steps: list[dict[str, Any]] = []
    blocked_steps: list[dict[str, Any]] = []
    for requirement in requirement_steps:
        requirement_id = str(requirement.get("id") or "")
        if requirement_id in completed_ids:
            continue
        step = dict(requirement)
        missing_prerequisites = [
            prereq
            for prereq in _string_list(requirement.get("prerequisite_ids"))
            if prereq not in completed_ids
        ]
        if missing_prerequisites:
            step["blocked_by"] = missing_prerequisites
            blocked_steps.append(step)
        missing_steps.append(step)

    ordered_next_steps = sorted(
        missing_steps,
        key=lambda step: int(step.get("order") or 0),
    )
    attempt_bridge_step = _multiple_attempt_lineage_bridge_step(
        rows,
        backlog_id=backlog_id,
    )
    if attempt_bridge_step and not any(
        step.get("id") == attempt_bridge_step["id"] for step in ordered_next_steps
    ):
        ordered_next_steps = [attempt_bridge_step, *ordered_next_steps]
    contract_first_runtime_step: dict[str, Any] = {}
    if legacy_no_contract:
        latest_hotfix_entered = _latest_passing_event_with_kind(
            rows,
            "hotfix_entered",
        )
        latest_hotfix_under_action = _latest_passing_event_with_kind(
            rows,
            "hotfix_under_action",
        )
        if latest_hotfix_entered and not latest_hotfix_under_action:
            contract_first_runtime_step = _legacy_hotfix_post_action_step(
                backlog_id=backlog_id,
                project_id=project_id,
                hotfix_event=latest_hotfix_entered,
            )
        elif not latest_hotfix_entered:
            contract_first_runtime_step = _contract_first_selection_step(
                backlog_id=backlog_id,
                project_id=project_id,
            )
        if contract_first_runtime_step and not any(
            step.get("id") == contract_first_runtime_step["id"]
            for step in ordered_next_steps
        ):
            ordered_next_steps = [
                contract_first_runtime_step,
                *ordered_next_steps,
            ]
    requirement_contract_active = bool(has_explicit_contract and requirement_steps)
    next_legal_action: dict[str, Any] | None = None
    if (
        requirement_contract_active or contract_first_runtime_step
    ) and ordered_next_steps:
        first = ordered_next_steps[0]
        next_legal_action = {
            "id": first["id"],
            "action": first.get("action") or f"record_{first['id']}",
            "detail": first.get("detail")
            or f"record contract evidence for {first['id']}",
            "source": first.get("source") or "contract_state",
            "precedence": first.get("precedence") or "active_contract_missing_step",
            "blocked_by": first.get("blocked_by") or [],
            "ordered_missing_steps_source": "contract_state",
            "ordered_missing_steps": ordered_next_steps,
            **_next_action_requirement_metadata(first),
        }

    route_binding = _latest_route_binding(rows, root)
    route_identity_hash = _route_identity_hash(route_binding)
    projection_watermark = projection.get("projection_watermark", 0)
    if not projection_watermark:
        projection_watermark = max((_event_numeric_id(event) for event in rows), default=0) or len(rows)
    active_execution: dict[str, Any] = {}
    if has_explicit_contract:
        active_contract_execution_id = str(
            binding.get("contract_execution_id")
            or root.get("contract_execution_id")
            or ""
        ).strip()
        active_execution = {
            "schema_version": "active_contract_execution.v1",
            "project_id": project_id,
            "backlog_id": backlog_id,
            "contract_execution_id": active_contract_execution_id
            or _execution_id(
                project_id=project_id,
                backlog_id=backlog_id,
                contract_id=contract_id,
                template_id=template_id,
                revision_id=current_revision_id,
            ),
            "contract_id": contract_id,
            "contract_template_id": template_id,
            "contract_instance_id": str(binding.get("contract_instance_id") or root.get("contract_instance_id") or ""),
            "contract_revision_id": current_revision_id,
            "state": state,
            "projection_watermark": projection_watermark,
            "observer_session_id": str(binding.get("observer_session_id") or root.get("observer_session_id") or ""),
            "route_identity_hash": route_identity_hash,
            "route_token_ref": str(route_binding.get("route_token_ref") or ""),
            "prompt_contract_id": str(route_binding.get("prompt_contract_id") or ""),
            "visible_injection_manifest_hash": str(route_binding.get("visible_injection_manifest_hash") or ""),
        }

    contract_complete = (
        has_explicit_contract and requirements_explicit and not ordered_next_steps
    )
    root_requirement_state = {
        "contract_complete": contract_complete,
        "completed_evidence": [
            {"id": requirement_id, **source}
            for requirement_id, source in completed_sources.items()
        ],
    }
    contract_chain_id = _contract_chain_id(
        project_id=project_id,
        backlog_id=backlog_id,
        root=root,
        binding=binding,
        active_contract_execution_id=str(
            active_execution.get("contract_execution_id") or ""
        ),
    )
    if active_execution and contract_chain_id:
        active_execution["contract_chain_id"] = contract_chain_id
    successor_candidates = _successor_candidates(
        root,
        contract_chain_id=contract_chain_id,
        active_contract_execution=active_execution,
        contract_complete=contract_complete,
    )
    successor_policy = _successor_policy_summary(root)
    successor_bindings = _successor_contract_bindings(
        rows,
        project_id=project_id,
        backlog_id=backlog_id,
        contract_chain_id=contract_chain_id,
        active_contract_execution=active_execution,
    )
    selected_successor = successor_bindings[-1] if successor_bindings else {}
    completed_contract_executions = [
        successor
        for successor in successor_bindings
        if str(successor.get("state") or successor.get("status") or "").lower()
        in {"complete", "completed", "passed", "succeeded"}
    ]
    contract_chain: list[dict[str, Any]] = []
    if active_execution and contract_chain_id:
        contract_chain.append(
            {
                "role": "root",
                "contract_chain_id": contract_chain_id,
                **active_execution,
            }
        )
        for successor in successor_bindings:
            contract_chain.append({"role": "successor", **successor})

    template_catalog = _contract_template_map(contract_templates)
    selected_successor_contract_state: dict[str, Any] = {}
    selected_successor_contract_candidates: list[dict[str, Any]] = []
    selected_successor_bindings: list[dict[str, Any]] = []
    nested_selected_successor: dict[str, Any] = {}
    nested_successor_contract_state: dict[str, Any] = {}
    deep_selected_successor: dict[str, Any] = {}
    deep_successor_bindings: list[dict[str, Any]] = []
    deep_successor_contract_state: dict[str, Any] = {}
    if selected_successor:
        successor_execution_id = str(
            selected_successor.get("successor_contract_execution_id")
            or selected_successor.get("contract_execution_id")
            or ""
        ).strip()
        successor_root = _successor_contract_root(
            selected_successor,
            contract_templates=template_catalog,
        )
        successor_execution = {
            "schema_version": "active_contract_execution.v1",
            "project_id": project_id,
            "backlog_id": selected_successor.get("successor_backlog_id") or backlog_id,
            "contract_execution_id": successor_execution_id,
            "contract_id": selected_successor.get("contract_id") or "",
            "contract_template_id": selected_successor.get("contract_template_id") or "",
            "contract_instance_id": selected_successor.get("contract_instance_id") or "",
            "contract_revision_id": selected_successor.get("contract_revision_id") or "",
            "state": selected_successor.get("state")
            or selected_successor.get("status")
            or "selected",
            "projection_watermark": projection_watermark,
            "route_identity_hash": route_identity_hash,
            "route_token_ref": active_execution.get("route_token_ref") or "",
            "contract_chain_id": contract_chain_id,
            "parent_contract_execution_id": selected_successor.get(
                "parent_contract_execution_id"
            )
            or active_execution.get("contract_execution_id")
            or "",
        }
        successor_requirement_state = _contract_requirements_state(
            rows,
            root=successor_root,
            backlog_row={
                **row,
                "bug_id": selected_successor.get("successor_backlog_id")
                or backlog_id,
                "backlog_id": selected_successor.get("successor_backlog_id")
                or backlog_id,
            },
            default_required_evidence=[],
            contract_execution_id=successor_execution_id,
            missing_precedence="successor_contract_missing_step",
        )
        selected_successor_contract_candidates = _successor_candidates(
            successor_root,
            contract_chain_id=contract_chain_id,
            active_contract_execution=successor_execution,
            contract_complete=bool(successor_requirement_state["contract_complete"]),
        )
        selected_successor_bindings = _successor_contract_bindings(
            rows,
            project_id=project_id,
            backlog_id=str(
                selected_successor.get("successor_backlog_id") or backlog_id
            ),
            contract_chain_id=contract_chain_id,
            active_contract_execution=successor_execution,
        )
        nested_selected_successor = (
            selected_successor_bindings[-1] if selected_successor_bindings else {}
        )
        if nested_selected_successor:
            nested_execution_id = str(
                nested_selected_successor.get("successor_contract_execution_id")
                or nested_selected_successor.get("contract_execution_id")
                or ""
            ).strip()
            nested_root = _successor_contract_root(
                nested_selected_successor,
                contract_templates=template_catalog,
            )
            nested_execution = {
                "schema_version": "active_contract_execution.v1",
                "project_id": project_id,
                "backlog_id": nested_selected_successor.get("successor_backlog_id")
                or selected_successor.get("successor_backlog_id")
                or backlog_id,
                "contract_execution_id": nested_execution_id,
                "contract_id": nested_selected_successor.get("contract_id") or "",
                "contract_template_id": nested_selected_successor.get(
                    "contract_template_id"
                )
                or "",
                "contract_instance_id": nested_selected_successor.get(
                    "contract_instance_id"
                )
                or "",
                "contract_revision_id": nested_selected_successor.get(
                    "contract_revision_id"
                )
                or "",
                "state": nested_selected_successor.get("state")
                or nested_selected_successor.get("status")
                or "selected",
                "projection_watermark": projection_watermark,
                "route_identity_hash": route_identity_hash,
                "route_token_ref": active_execution.get("route_token_ref") or "",
                "contract_chain_id": contract_chain_id,
                "parent_contract_execution_id": nested_selected_successor.get(
                    "parent_contract_execution_id"
                )
                or successor_execution_id,
            }
            nested_requirement_state = _contract_requirements_state(
                rows,
                root=nested_root,
                backlog_row={
                    **row,
                    "bug_id": nested_selected_successor.get("successor_backlog_id")
                    or selected_successor.get("successor_backlog_id")
                    or backlog_id,
                    "backlog_id": nested_selected_successor.get("successor_backlog_id")
                    or selected_successor.get("successor_backlog_id")
                    or backlog_id,
                },
                default_required_evidence=[],
                contract_execution_id=nested_execution_id,
                missing_precedence="nested_successor_contract_missing_step",
            )
            deep_successor_bindings = _successor_contract_bindings(
                rows,
                project_id=project_id,
                backlog_id=str(
                    nested_selected_successor.get("successor_backlog_id")
                    or selected_successor.get("successor_backlog_id")
                    or backlog_id
                ),
                contract_chain_id=contract_chain_id,
                active_contract_execution=nested_execution,
            )
            deep_selected_successor = (
                deep_successor_bindings[-1] if deep_successor_bindings else {}
            )
            if deep_selected_successor:
                deep_execution_id = str(
                    deep_selected_successor.get("successor_contract_execution_id")
                    or deep_selected_successor.get("contract_execution_id")
                    or ""
                ).strip()
                deep_root = _successor_contract_root(
                    deep_selected_successor,
                    contract_templates=template_catalog,
                )
                deep_execution = {
                    "schema_version": "active_contract_execution.v1",
                    "project_id": project_id,
                    "backlog_id": deep_selected_successor.get("successor_backlog_id")
                    or nested_selected_successor.get("successor_backlog_id")
                    or selected_successor.get("successor_backlog_id")
                    or backlog_id,
                    "contract_execution_id": deep_execution_id,
                    "contract_id": deep_selected_successor.get("contract_id") or "",
                    "contract_template_id": deep_selected_successor.get(
                        "contract_template_id"
                    )
                    or "",
                    "contract_instance_id": deep_selected_successor.get(
                        "contract_instance_id"
                    )
                    or "",
                    "contract_revision_id": deep_selected_successor.get(
                        "contract_revision_id"
                    )
                    or "",
                    "state": deep_selected_successor.get("state")
                    or deep_selected_successor.get("status")
                    or "selected",
                    "projection_watermark": projection_watermark,
                    "route_identity_hash": route_identity_hash,
                    "route_token_ref": active_execution.get("route_token_ref") or "",
                    "contract_chain_id": contract_chain_id,
                    "parent_contract_execution_id": deep_selected_successor.get(
                        "parent_contract_execution_id"
                    )
                    or nested_execution_id,
                }
                deep_requirement_state = _contract_requirements_state(
                    rows,
                    root=deep_root,
                    backlog_row={
                        **row,
                        "bug_id": deep_selected_successor.get("successor_backlog_id")
                        or nested_selected_successor.get("successor_backlog_id")
                        or selected_successor.get("successor_backlog_id")
                        or backlog_id,
                        "backlog_id": deep_selected_successor.get(
                            "successor_backlog_id"
                        )
                        or nested_selected_successor.get("successor_backlog_id")
                        or selected_successor.get("successor_backlog_id")
                        or backlog_id,
                    },
                    default_required_evidence=[],
                    contract_execution_id=deep_execution_id,
                    missing_precedence="deep_successor_contract_missing_step",
                )
                deep_successor_contract_state = {
                    "schema_version": "deep_successor_contract_state.v1",
                    "source": "contract_state",
                    "selected_successor_contract": deep_selected_successor,
                    "parent_successor_contract": nested_selected_successor,
                    "root_successor_contract": selected_successor,
                    "active_contract_execution": deep_execution,
                    "contract_id": deep_execution["contract_id"],
                    "contract_template_id": deep_execution["contract_template_id"],
                    "contract_execution_id": deep_execution_id,
                    "contract_chain_id": contract_chain_id,
                    "parent_contract_execution_id": deep_execution[
                        "parent_contract_execution_id"
                    ],
                    "successor_contract_policy": _successor_policy_summary(deep_root),
                    "successor_contract_candidates": _successor_candidates(
                        deep_root,
                        contract_chain_id=contract_chain_id,
                        active_contract_execution=deep_execution,
                        contract_complete=bool(
                            deep_requirement_state["contract_complete"]
                        ),
                    ),
                    **deep_requirement_state,
                }
            nested_successor_contract_state = {
                "schema_version": "nested_successor_contract_state.v1",
                "source": "contract_state",
                "selected_successor_contract": nested_selected_successor,
                "parent_successor_contract": selected_successor,
                "active_contract_execution": nested_execution,
                "contract_id": nested_execution["contract_id"],
                "contract_template_id": nested_execution["contract_template_id"],
                "contract_execution_id": nested_execution_id,
                "contract_chain_id": contract_chain_id,
                "parent_contract_execution_id": nested_execution[
                    "parent_contract_execution_id"
                ],
                "successor_contract_policy": _successor_policy_summary(nested_root),
                "successor_contract_candidates": _successor_candidates(
                    nested_root,
                    contract_chain_id=contract_chain_id,
                    active_contract_execution=nested_execution,
                    contract_complete=bool(
                        nested_requirement_state["contract_complete"]
                    ),
                ),
                "selected_successor_contract_binding": deep_selected_successor,
                "successor_contract_bindings": deep_successor_bindings,
                "deep_selected_successor_contract_state": deep_successor_contract_state,
                **nested_requirement_state,
            }
        selected_successor_contract_state = {
            "schema_version": "selected_successor_contract_state.v1",
            "source": "contract_state",
            "selected_successor_contract": selected_successor,
            "active_contract_execution": successor_execution,
            "contract_id": successor_execution["contract_id"],
            "contract_template_id": successor_execution["contract_template_id"],
            "contract_execution_id": successor_execution_id,
            "contract_chain_id": contract_chain_id,
            "parent_contract_execution_id": successor_execution[
                "parent_contract_execution_id"
            ],
            "successor_contract_policy": _successor_policy_summary(successor_root),
            "successor_contract_candidates": selected_successor_contract_candidates,
            "selected_successor_contract_binding": nested_selected_successor,
            "successor_contract_bindings": selected_successor_bindings,
            "nested_selected_successor_contract_state": nested_successor_contract_state,
            **successor_requirement_state,
        }

    successor_next_legal_action: dict[str, Any] | None = None
    if selected_successor and selected_successor_contract_state:
        successor_missing_steps = selected_successor_contract_state.get(
            "ordered_next_steps"
        ) or []
        if successor_missing_steps:
            first = dict(successor_missing_steps[0])
            successor_next_legal_action = {
                "id": first["id"],
                "action": first.get("action") or f"record_{first['id']}",
                "requirement_id": first["id"],
                "detail": f"record successor contract evidence for {first['id']}",
                "source": first.get("source") or "contract_state",
                "precedence": first.get("precedence")
                or "successor_contract_missing_step",
                "blocked_by": first.get("blocked_by") or [],
                "ordered_missing_steps_source": "selected_successor_contract_state",
                "ordered_missing_steps": successor_missing_steps,
                "contract_chain_id": contract_chain_id,
                "contract_execution_id": selected_successor_contract_state.get(
                    "contract_execution_id"
                )
                or "",
                "backlog_id": selected_successor.get("successor_backlog_id")
                or backlog_id,
                "route_token_ref": active_execution.get("route_token_ref") or "",
                "projection_watermark": projection_watermark,
                "selected_successor_contract": selected_successor,
                "selected_successor_contract_state": selected_successor_contract_state,
                **_next_action_requirement_metadata(first),
            }
        elif nested_successor_contract_state:
            nested_missing_steps = nested_successor_contract_state.get(
                "ordered_next_steps"
            ) or []
            if nested_missing_steps:
                first = dict(nested_missing_steps[0])
                successor_next_legal_action = {
                    "id": first["id"],
                    "action": first.get("action") or f"record_{first['id']}",
                    "requirement_id": first["id"],
                    "detail": (
                        f"record nested successor contract evidence for {first['id']}"
                    ),
                    "source": first.get("source") or "contract_state",
                    "precedence": first.get("precedence")
                    or "nested_successor_contract_missing_step",
                    "blocked_by": first.get("blocked_by") or [],
                    "ordered_missing_steps_source": "nested_successor_contract_state",
                    "ordered_missing_steps": nested_missing_steps,
                    "contract_chain_id": contract_chain_id,
                    "contract_execution_id": nested_successor_contract_state.get(
                        "contract_execution_id"
                    )
                    or "",
                    "backlog_id": nested_selected_successor.get(
                        "successor_backlog_id"
                    )
                    or selected_successor.get("successor_backlog_id")
                    or backlog_id,
                    "route_token_ref": active_execution.get("route_token_ref") or "",
                    "projection_watermark": projection_watermark,
                    "selected_successor_contract": nested_selected_successor,
                    "parent_successor_contract": selected_successor,
                    "selected_successor_contract_state": (
                        nested_successor_contract_state
                    ),
                        **_next_action_requirement_metadata(first),
                    }
            elif deep_successor_contract_state:
                deep_missing_steps = deep_successor_contract_state.get(
                    "ordered_next_steps"
                ) or []
                if deep_missing_steps:
                    first = dict(deep_missing_steps[0])
                    successor_next_legal_action = {
                        "id": first["id"],
                        "action": first.get("action") or f"record_{first['id']}",
                        "requirement_id": first["id"],
                        "detail": (
                            f"record deep successor contract evidence for {first['id']}"
                        ),
                        "source": first.get("source") or "contract_state",
                        "precedence": first.get("precedence")
                        or "deep_successor_contract_missing_step",
                        "blocked_by": first.get("blocked_by") or [],
                        "ordered_missing_steps_source": "deep_successor_contract_state",
                        "ordered_missing_steps": deep_missing_steps,
                        "contract_chain_id": contract_chain_id,
                        "contract_execution_id": deep_successor_contract_state.get(
                            "contract_execution_id"
                        )
                        or "",
                        "backlog_id": deep_selected_successor.get(
                            "successor_backlog_id"
                        )
                        or nested_selected_successor.get("successor_backlog_id")
                        or selected_successor.get("successor_backlog_id")
                        or backlog_id,
                        "route_token_ref": active_execution.get("route_token_ref")
                        or "",
                        "projection_watermark": projection_watermark,
                        "selected_successor_contract": deep_selected_successor,
                        "parent_successor_contract": nested_selected_successor,
                        "selected_successor_contract_state": (
                            deep_successor_contract_state
                        ),
                        **_next_action_requirement_metadata(first),
                    }
                else:
                    deep_successor_contract_candidates = (
                        deep_successor_contract_state.get(
                            "successor_contract_candidates"
                        )
                        or []
                    )
                    if deep_successor_contract_candidates:
                        successor_next_legal_action = {
                            "id": "select_successor_contract",
                            "action": "select_successor_contract",
                            "requirement_id": "successor_contract_selection",
                            "detail": (
                                "select the next contract execution after the "
                                "active deep successor contract"
                            ),
                            "source": "contract_state",
                            "precedence": "deep_successor_contract_selection",
                            "contract_chain_id": contract_chain_id,
                            "contract_execution_id": (
                                deep_successor_contract_state.get(
                                    "contract_execution_id"
                                )
                                or ""
                            ),
                            "backlog_id": deep_selected_successor.get(
                                "successor_backlog_id"
                            )
                            or nested_selected_successor.get("successor_backlog_id")
                            or selected_successor.get("successor_backlog_id")
                            or backlog_id,
                            "route_token_ref": active_execution.get(
                                "route_token_ref"
                            )
                            or "",
                            "projection_watermark": projection_watermark,
                            "successor_contract_policy": (
                                deep_successor_contract_state.get(
                                    "successor_contract_policy"
                                )
                                or {}
                            ),
                            "successor_contract_candidates": (
                                deep_successor_contract_candidates
                            ),
                            "selected_successor_contract": deep_selected_successor,
                            "parent_successor_contract": nested_selected_successor,
                            "selected_successor_contract_state": (
                                deep_successor_contract_state
                            ),
                        }
                    else:
                        successor_next_legal_action = {
                            "id": "successor_contract_selected",
                            "action": "read_successor_contract_state",
                            "requirement_id": "successor_contract_selected",
                            "detail": (
                                "continue with selected deep successor contract execution"
                            ),
                            "source": "contract_state",
                            "precedence": "deep_successor_contract_selected",
                            "contract_chain_id": contract_chain_id,
                            "contract_execution_id": deep_selected_successor.get(
                                "successor_contract_execution_id"
                            )
                            or deep_selected_successor.get("contract_execution_id")
                            or "",
                            "backlog_id": deep_selected_successor.get(
                                "successor_backlog_id"
                            )
                            or nested_selected_successor.get("successor_backlog_id")
                            or selected_successor.get("successor_backlog_id")
                            or backlog_id,
                            "route_token_ref": active_execution.get(
                                "route_token_ref"
                            )
                            or "",
                            "projection_watermark": projection_watermark,
                            "selected_successor_contract": deep_selected_successor,
                            "parent_successor_contract": nested_selected_successor,
                            "selected_successor_contract_state": (
                                deep_successor_contract_state
                            ),
                        }
            else:
                nested_successor_contract_candidates = (
                    nested_successor_contract_state.get(
                        "successor_contract_candidates"
                    )
                    or []
                )
                if nested_successor_contract_candidates:
                    successor_next_legal_action = {
                        "id": "select_successor_contract",
                        "action": "select_successor_contract",
                        "requirement_id": "successor_contract_selection",
                        "detail": (
                            "select the next contract execution after the active "
                            "nested successor contract"
                        ),
                        "source": "contract_state",
                        "precedence": "nested_successor_contract_selection",
                        "contract_chain_id": contract_chain_id,
                        "contract_execution_id": (
                            nested_successor_contract_state.get(
                                "contract_execution_id"
                            )
                            or ""
                        ),
                        "backlog_id": nested_selected_successor.get(
                            "successor_backlog_id"
                        )
                        or selected_successor.get("successor_backlog_id")
                        or backlog_id,
                        "route_token_ref": active_execution.get("route_token_ref")
                        or "",
                        "projection_watermark": projection_watermark,
                        "successor_contract_policy": (
                            nested_successor_contract_state.get(
                                "successor_contract_policy"
                            )
                            or {}
                        ),
                        "successor_contract_candidates": (
                            nested_successor_contract_candidates
                        ),
                        "selected_successor_contract": nested_selected_successor,
                        "parent_successor_contract": selected_successor,
                        "selected_successor_contract_state": (
                            nested_successor_contract_state
                        ),
                    }
                else:
                    successor_next_legal_action = {
                        "id": "successor_contract_selected",
                        "action": "read_successor_contract_state",
                        "requirement_id": "successor_contract_selected",
                        "detail": (
                            "continue with selected nested successor contract execution"
                        ),
                        "source": "contract_state",
                        "precedence": "nested_successor_contract_selected",
                        "contract_chain_id": contract_chain_id,
                        "contract_execution_id": nested_selected_successor.get(
                            "successor_contract_execution_id"
                        )
                        or nested_selected_successor.get("contract_execution_id")
                        or "",
                        "backlog_id": nested_selected_successor.get(
                            "successor_backlog_id"
                        )
                        or selected_successor.get("successor_backlog_id")
                        or backlog_id,
                        "route_token_ref": active_execution.get("route_token_ref")
                        or "",
                        "projection_watermark": projection_watermark,
                        "selected_successor_contract": nested_selected_successor,
                        "parent_successor_contract": selected_successor,
                        "selected_successor_contract_state": (
                            nested_successor_contract_state
                        ),
                    }
        elif nested_selected_successor:
            successor_next_legal_action = {
                "id": "successor_contract_selected",
                "action": "read_successor_contract_state",
                "requirement_id": "successor_contract_selected",
                "detail": "continue with selected nested successor contract execution",
                "source": "contract_state",
                "precedence": "nested_successor_contract_selected",
                "contract_chain_id": contract_chain_id,
                "contract_execution_id": nested_selected_successor.get(
                    "successor_contract_execution_id"
                )
                or nested_selected_successor.get("contract_execution_id")
                or "",
                "backlog_id": nested_selected_successor.get("successor_backlog_id")
                or selected_successor.get("successor_backlog_id")
                or backlog_id,
                "route_token_ref": active_execution.get("route_token_ref") or "",
                "projection_watermark": projection_watermark,
                "selected_successor_contract": nested_selected_successor,
                "parent_successor_contract": selected_successor,
                "selected_successor_contract_state": selected_successor_contract_state,
            }
        elif selected_successor_contract_candidates:
            successor_next_legal_action = {
                "id": "select_successor_contract",
                "action": "select_successor_contract",
                "requirement_id": "successor_contract_selection",
                "detail": "select the next contract execution after the active successor contract",
                "source": "contract_state",
                "precedence": "successor_contract_selection",
                "contract_chain_id": contract_chain_id,
                "contract_execution_id": selected_successor_contract_state.get(
                    "contract_execution_id"
                )
                or "",
                "backlog_id": selected_successor.get("successor_backlog_id")
                or backlog_id,
                "route_token_ref": active_execution.get("route_token_ref") or "",
                "projection_watermark": projection_watermark,
                "successor_contract_policy": selected_successor_contract_state.get(
                    "successor_contract_policy"
                )
                or {},
                "successor_contract_candidates": selected_successor_contract_candidates,
                "selected_successor_contract": selected_successor,
                "selected_successor_contract_state": selected_successor_contract_state,
            }
        else:
            successor_next_legal_action = {
                "id": "successor_contract_complete",
                "action": "read_successor_contract_state",
                "requirement_id": "successor_contract_complete",
                "detail": "selected successor contract has no further successor policy",
                "source": "contract_state",
                "precedence": "successor_contract_complete",
                "contract_chain_id": contract_chain_id,
                "contract_execution_id": selected_successor_contract_state.get(
                    "contract_execution_id"
                )
                or "",
                "backlog_id": selected_successor.get("successor_backlog_id")
                or backlog_id,
                "route_token_ref": active_execution.get("route_token_ref") or "",
                "projection_watermark": projection_watermark,
                "selected_successor_contract": selected_successor,
                "selected_successor_contract_state": selected_successor_contract_state,
            }
    elif selected_successor:
        successor_next_legal_action = {
            "id": "successor_contract_selected",
            "action": "read_successor_contract_state",
            "requirement_id": "successor_contract_selected",
            "detail": "continue with selected successor contract execution",
            "source": "contract_state",
            "precedence": "successor_contract_selected",
            "contract_chain_id": contract_chain_id,
            "contract_execution_id": selected_successor.get(
                "successor_contract_execution_id"
            )
            or selected_successor.get("contract_execution_id")
            or "",
            "backlog_id": selected_successor.get("successor_backlog_id") or backlog_id,
            "route_token_ref": active_execution.get("route_token_ref") or "",
            "projection_watermark": projection_watermark,
            "successor_contract_policy": successor_policy,
            "selected_successor_contract": selected_successor,
        }
    elif (
        contract_complete
        and successor_candidates
        and _successor_selection_required(
            root,
            events=rows,
            requirement_state=root_requirement_state,
            contract_execution_id=str(
                active_execution.get("contract_execution_id") or ""
            ),
        )
    ):
        successor_next_legal_action = {
            "id": "select_successor_contract",
            "action": "select_successor_contract",
            "requirement_id": "successor_contract_selection",
            "detail": "select the next contract execution before implementation or close",
            "source": "contract_state",
            "precedence": "successor_contract_selection",
            "contract_chain_id": contract_chain_id,
            "contract_execution_id": active_execution.get("contract_execution_id") or "",
            "backlog_id": backlog_id,
            "route_token_ref": active_execution.get("route_token_ref") or "",
            "projection_watermark": projection_watermark,
            "successor_contract_policy": successor_policy,
            "successor_contract_candidates": successor_candidates,
        }
    elif contract_complete:
        active_contract_execution_id = str(
            active_execution.get("contract_execution_id") or ""
        ).strip()
        close_ready_refs = _close_ready_evidence_refs(
            rows,
            contract_execution_id=active_contract_execution_id,
        )
        if close_ready_refs:
            successor_next_legal_action = _backlog_close_next_action_after_close_ready(
                row=row,
                backlog_id=backlog_id,
                contract_chain_id=contract_chain_id,
                contract_execution_id=active_contract_execution_id,
                route_binding=route_binding,
                route_token_ref=str(active_execution.get("route_token_ref") or ""),
                projection_watermark=projection_watermark,
                close_ready_evidence_refs=close_ready_refs,
                precedence="active_contract_terminal_backlog_close",
            )
        else:
            successor_next_legal_action = {
                "id": "close_ready",
                "action": "record_close_ready",
                "requirement_id": "close_ready",
                "detail": "record close_ready and run the close gate after the active contract completes",
                "source": "contract_state",
                "precedence": "active_contract_terminal_close_ready",
                "contract_chain_id": contract_chain_id,
                "contract_execution_id": active_contract_execution_id,
                "backlog_id": backlog_id,
                "route_token_ref": active_execution.get("route_token_ref") or "",
                "projection_watermark": projection_watermark,
                "successor_contract_policy": successor_policy,
                "successor_contract_candidates": successor_candidates,
            }

    root_next_legal_action: dict[str, Any] | None = None
    if next_legal_action and active_execution:
        root_next_legal_action = {
            **next_legal_action,
            "requirement_id": (
                next_legal_action.get("requirement_id")
                or next_legal_action.get("id")
                or ""
            ),
            "contract_chain_id": contract_chain_id,
            "contract_execution_id": active_execution.get("contract_execution_id") or "",
            "backlog_id": backlog_id,
            "route_token_ref": active_execution.get("route_token_ref") or "",
            "projection_watermark": projection_watermark,
        }
    if successor_next_legal_action:
        next_legal_action = dict(successor_next_legal_action)
    elif root_next_legal_action:
        next_legal_action = root_next_legal_action

    lane_contract_executions: list[dict[str, Any]] = []
    if active_execution:
        lane_contract_executions.append(
            {
                "schema_version": "contract_lane_execution.v1",
                "role": "root",
                "source": "contract_state",
                "contract_chain_id": contract_chain_id,
                "backlog_id": backlog_id,
                "active_contract_execution": active_execution,
                "contract_id": active_execution.get("contract_id") or "",
                "contract_template_id": active_execution.get("contract_template_id") or "",
                "contract_execution_id": active_execution.get("contract_execution_id") or "",
                "state": active_execution.get("state") or "",
                "next_legal_action": _next_action_summary(root_next_legal_action),
                "required_evidence": [step["id"] for step in requirement_steps],
                "completed_evidence": sorted(completed_ids),
                "missing_evidence": [step["id"] for step in ordered_next_steps],
            }
        )
    if selected_successor_contract_state:
        successor_execution = selected_successor_contract_state.get(
            "active_contract_execution"
        )
        if isinstance(successor_execution, Mapping):
            lane_contract_executions.append(
                {
                    "schema_version": "contract_lane_execution.v1",
                    "role": "successor",
                    "source": "contract_state",
                    "contract_chain_id": contract_chain_id,
                    "backlog_id": selected_successor.get("successor_backlog_id")
                    or backlog_id,
                    "active_contract_execution": dict(successor_execution),
                    "contract_id": selected_successor_contract_state.get("contract_id")
                    or "",
                    "contract_template_id": selected_successor_contract_state.get(
                        "contract_template_id"
                    )
                    or "",
                    "contract_execution_id": selected_successor_contract_state.get(
                        "contract_execution_id"
                    )
                    or "",
                    "parent_contract_execution_id": selected_successor_contract_state.get(
                        "parent_contract_execution_id"
                    )
                    or "",
                    "state": successor_execution.get("state") or "",
                    "next_legal_action": _next_action_summary(
                        successor_next_legal_action
                    ),
                    "selected_successor_contract": selected_successor,
                    "successor_contract_candidates": (
                        selected_successor_contract_state.get(
                            "successor_contract_candidates"
                        )
                        or []
                    ),
                    "selected_successor_contract_binding": (
                        selected_successor_contract_state.get(
                            "selected_successor_contract_binding"
                        )
                        or {}
                    ),
                    "required_evidence": selected_successor_contract_state.get(
                        "required_evidence"
                    )
                    or [],
                    "completed_evidence": [
                        item.get("id")
                        for item in (
                            selected_successor_contract_state.get(
                                "completed_evidence"
                            )
                            or []
                        )
                        if isinstance(item, Mapping) and item.get("id")
                    ],
                    "missing_evidence": selected_successor_contract_state.get(
                        "missing_evidence"
                    )
                    or [],
                }
            )
    if nested_successor_contract_state:
        nested_execution = nested_successor_contract_state.get(
            "active_contract_execution"
        )
        if isinstance(nested_execution, Mapping):
            nested_action = (
                successor_next_legal_action
                if str(
                    (successor_next_legal_action or {}).get("contract_execution_id")
                    or ""
                )
                == str(
                    nested_successor_contract_state.get("contract_execution_id")
                    or ""
                )
                else None
            )
            lane_contract_executions.append(
                {
                    "schema_version": "contract_lane_execution.v1",
                    "role": "nested_successor",
                    "source": "contract_state",
                    "contract_chain_id": contract_chain_id,
                    "backlog_id": nested_selected_successor.get("successor_backlog_id")
                    or selected_successor.get("successor_backlog_id")
                    or backlog_id,
                    "active_contract_execution": dict(nested_execution),
                    "contract_id": nested_successor_contract_state.get("contract_id")
                    or "",
                    "contract_template_id": nested_successor_contract_state.get(
                        "contract_template_id"
                    )
                    or "",
                    "contract_execution_id": nested_successor_contract_state.get(
                        "contract_execution_id"
                    )
                    or "",
                    "parent_contract_execution_id": nested_successor_contract_state.get(
                        "parent_contract_execution_id"
                    )
                    or "",
                    "state": nested_execution.get("state") or "",
                    "next_legal_action": _next_action_summary(nested_action),
                    "selected_successor_contract": nested_selected_successor,
                    "required_evidence": nested_successor_contract_state.get(
                        "required_evidence"
                    )
                    or [],
                    "completed_evidence": [
                        item.get("id")
                        for item in (
                            nested_successor_contract_state.get("completed_evidence")
                            or []
                        )
                        if isinstance(item, Mapping) and item.get("id")
                    ],
                    "missing_evidence": nested_successor_contract_state.get(
                        "missing_evidence"
                    )
                    or [],
                }
            )
    if deep_successor_contract_state:
        deep_execution = deep_successor_contract_state.get("active_contract_execution")
        if isinstance(deep_execution, Mapping):
            deep_action = (
                successor_next_legal_action
                if str(
                    (successor_next_legal_action or {}).get("contract_execution_id")
                    or ""
                )
                == str(
                    deep_successor_contract_state.get("contract_execution_id") or ""
                )
                else None
            )
            lane_contract_executions.append(
                {
                    "schema_version": "contract_lane_execution.v1",
                    "role": "deep_successor",
                    "source": "contract_state",
                    "contract_chain_id": contract_chain_id,
                    "backlog_id": deep_selected_successor.get("successor_backlog_id")
                    or nested_selected_successor.get("successor_backlog_id")
                    or selected_successor.get("successor_backlog_id")
                    or backlog_id,
                    "active_contract_execution": dict(deep_execution),
                    "contract_id": deep_successor_contract_state.get("contract_id")
                    or "",
                    "contract_template_id": deep_successor_contract_state.get(
                        "contract_template_id"
                    )
                    or "",
                    "contract_execution_id": deep_successor_contract_state.get(
                        "contract_execution_id"
                    )
                    or "",
                    "parent_contract_execution_id": deep_successor_contract_state.get(
                        "parent_contract_execution_id"
                    )
                    or "",
                    "state": deep_execution.get("state") or "",
                    "next_legal_action": _next_action_summary(deep_action),
                    "selected_successor_contract": deep_selected_successor,
                    "required_evidence": deep_successor_contract_state.get(
                        "required_evidence"
                    )
                    or [],
                    "completed_evidence": [
                        item.get("id")
                        for item in (
                            deep_successor_contract_state.get("completed_evidence")
                            or []
                        )
                        if isinstance(item, Mapping) and item.get("id")
                    ],
                    "missing_evidence": deep_successor_contract_state.get(
                        "missing_evidence"
                    )
                    or [],
                }
            )
    recursive_successor_states: list[dict[str, Any]] = []
    if active_execution and contract_chain_id:
        parent_execution: Mapping[str, Any] = active_execution
        parent_successor: Mapping[str, Any] = {}
        seen_executions = {str(active_execution.get("contract_execution_id") or "")}
        for depth in range(1, 16):
            parent_execution_id = str(
                parent_execution.get("contract_execution_id") or ""
            ).strip()
            if not parent_execution_id:
                break
            bindings = _successor_contract_bindings(
                rows,
                project_id=project_id,
                backlog_id=str(parent_execution.get("backlog_id") or backlog_id),
                contract_chain_id=contract_chain_id,
                active_contract_execution=parent_execution,
            )
            selected = bindings[-1] if bindings else {}
            if not selected:
                break
            execution_id = str(
                selected.get("successor_contract_execution_id")
                or selected.get("contract_execution_id")
                or ""
            ).strip()
            if not execution_id or execution_id in seen_executions:
                break
            seen_executions.add(execution_id)
            successor_root = _successor_contract_root(
                selected,
                contract_templates=template_catalog,
            )
            successor_execution = _successor_active_execution(
                selected,
                project_id=project_id,
                backlog_id=backlog_id,
                contract_chain_id=contract_chain_id,
                parent_execution_id=parent_execution_id,
                projection_watermark=projection_watermark,
                route_identity_hash=route_identity_hash,
                route_token_ref=str(active_execution.get("route_token_ref") or ""),
            )
            requirement_state = _contract_requirements_state(
                rows,
                root=successor_root,
                backlog_row={
                    **row,
                    "bug_id": selected.get("successor_backlog_id") or backlog_id,
                    "backlog_id": selected.get("successor_backlog_id") or backlog_id,
                },
                default_required_evidence=[],
                contract_execution_id=execution_id,
                missing_precedence=(
                    "successor_contract_missing_step"
                    if depth == 1
                    else (
                        "nested_successor_contract_missing_step"
                        if depth == 2
                        else (
                            "deep_successor_contract_missing_step"
                            if depth == 3
                            else f"successor_depth_{depth}_contract_missing_step"
                        )
                    )
                ),
            )
            successor_state = {
                "schema_version": f"successor_depth_{depth}_contract_state.v1",
                "source": "contract_state",
                "depth": depth,
                "role": _successor_lane_role(depth),
                "selected_successor_contract": selected,
                "parent_successor_contract": parent_successor,
                "active_contract_execution": successor_execution,
                "contract_id": successor_execution.get("contract_id") or "",
                "contract_template_id": successor_execution.get(
                    "contract_template_id"
                )
                or "",
                "contract_execution_id": execution_id,
                "contract_chain_id": contract_chain_id,
                "parent_contract_execution_id": parent_execution_id,
                "successor_contract_policy": _successor_policy_summary(
                    successor_root
                ),
                "successor_contract_candidates": _successor_candidates(
                    successor_root,
                    contract_chain_id=contract_chain_id,
                    active_contract_execution=successor_execution,
                    contract_complete=bool(requirement_state["contract_complete"]),
                ),
                "successor_selection_required": _successor_selection_required(
                    successor_root,
                    events=rows,
                    requirement_state=requirement_state,
                    contract_execution_id=execution_id,
                ),
                **requirement_state,
            }
            recursive_successor_states.append(
                {
                    "depth": depth,
                    "role": _successor_lane_role(depth),
                    "selected": selected,
                    "state": successor_state,
                    "root": successor_root,
                }
            )
            parent_execution = successor_execution
            parent_successor = selected

    recursive_next_legal_action: dict[str, Any] | None = None
    if recursive_successor_states:
        latest = recursive_successor_states[-1]
        latest_state = latest["state"]
        latest_selected = latest["selected"]
        latest_missing_steps = latest_state.get("ordered_next_steps") or []
        latest_depth = int(latest.get("depth") or 0)
        latest_role = str(latest.get("role") or "successor")
        if latest_missing_steps:
            first = dict(latest_missing_steps[0])
            recursive_next_legal_action = {
                "id": first["id"],
                "action": first.get("action") or f"record_{first['id']}",
                "requirement_id": first["id"],
                "detail": f"record {latest_role} contract evidence for {first['id']}",
                "source": first.get("source") or "contract_state",
                "precedence": first.get("precedence")
                or f"successor_depth_{latest_depth}_contract_missing_step",
                "blocked_by": first.get("blocked_by") or [],
                "ordered_missing_steps_source": _successor_missing_steps_source(
                    latest_depth
                ),
                "ordered_missing_steps": latest_missing_steps,
                "contract_chain_id": contract_chain_id,
                "contract_execution_id": latest_state.get("contract_execution_id")
                or "",
                "backlog_id": latest_selected.get("successor_backlog_id")
                or backlog_id,
                "route_token_ref": active_execution.get("route_token_ref") or "",
                "projection_watermark": projection_watermark,
                "selected_successor_contract": latest_selected,
                "selected_successor_contract_state": latest_state,
                **_next_action_requirement_metadata(first),
            }
        elif (
            latest_state.get("successor_contract_candidates")
            and latest_state.get("successor_selection_required")
        ):
            recursive_next_legal_action = {
                "id": "select_successor_contract",
                "action": "select_successor_contract",
                "requirement_id": "successor_contract_selection",
                "detail": (
                    "select the next contract execution after the active "
                    f"{latest_role} contract"
                ),
                "source": "contract_state",
                "precedence": f"successor_depth_{latest_depth}_contract_selection",
                "contract_chain_id": contract_chain_id,
                "contract_execution_id": latest_state.get("contract_execution_id")
                or "",
                "backlog_id": latest_selected.get("successor_backlog_id")
                or backlog_id,
                "route_token_ref": active_execution.get("route_token_ref") or "",
                "projection_watermark": projection_watermark,
                "successor_contract_policy": latest_state.get(
                    "successor_contract_policy"
                )
                or {},
                "successor_contract_candidates": latest_state.get(
                    "successor_contract_candidates"
                )
                or [],
                "selected_successor_contract": latest_selected,
                "selected_successor_contract_state": latest_state,
            }
        elif latest_state.get("contract_complete"):
            latest_contract_execution_id = str(
                latest_state.get("contract_execution_id") or ""
            ).strip()
            close_ready_refs = _close_ready_evidence_refs(
                rows,
                contract_execution_id=latest_contract_execution_id,
            )
            latest_backlog_id = (
                latest_selected.get("successor_backlog_id") or backlog_id
            )
            if close_ready_refs:
                recursive_next_legal_action = (
                    _backlog_close_next_action_after_close_ready(
                        row=row,
                        backlog_id=latest_backlog_id,
                        contract_chain_id=contract_chain_id,
                        contract_execution_id=latest_contract_execution_id,
                        route_binding=route_binding,
                        route_token_ref=str(
                            active_execution.get("route_token_ref") or ""
                        ),
                        projection_watermark=projection_watermark,
                        close_ready_evidence_refs=close_ready_refs,
                        precedence=(
                            f"successor_depth_{latest_depth}_terminal_backlog_close"
                        ),
                        selected_successor_contract=latest_selected,
                        selected_successor_contract_state=latest_state,
                    )
                )
            else:
                recursive_next_legal_action = {
                    "id": "close_ready",
                    "action": "record_close_ready",
                    "requirement_id": "close_ready",
                    "detail": (
                        "record close_ready and run the close gate after the terminal "
                        f"{latest_role} contract completes"
                    ),
                    "source": "contract_state",
                    "precedence": f"successor_depth_{latest_depth}_terminal_close_ready",
                    "contract_chain_id": contract_chain_id,
                    "contract_execution_id": latest_contract_execution_id,
                    "backlog_id": latest_backlog_id,
                    "route_token_ref": active_execution.get("route_token_ref") or "",
                    "projection_watermark": projection_watermark,
                    "selected_successor_contract": latest_selected,
                    "selected_successor_contract_state": latest_state,
                }
        if recursive_next_legal_action:
            next_legal_action = dict(recursive_next_legal_action)
            successor_next_legal_action = dict(recursive_next_legal_action)
        for item in recursive_successor_states[3:]:
            state_item = item["state"]
            execution_item = state_item.get("active_contract_execution")
            if not isinstance(execution_item, Mapping):
                continue
            state_action = (
                recursive_next_legal_action
                if str((recursive_next_legal_action or {}).get("contract_execution_id") or "")
                == str(state_item.get("contract_execution_id") or "")
                else None
            )
            lane_contract_executions.append(
                {
                    "schema_version": "contract_lane_execution.v1",
                    "role": item.get("role") or _successor_lane_role(
                        int(item.get("depth") or 0)
                    ),
                    "source": "contract_state",
                    "contract_chain_id": contract_chain_id,
                    "backlog_id": (item.get("selected") or {}).get(
                        "successor_backlog_id"
                    )
                    or backlog_id,
                    "active_contract_execution": dict(execution_item),
                    "contract_id": state_item.get("contract_id") or "",
                    "contract_template_id": state_item.get("contract_template_id")
                    or "",
                    "contract_execution_id": state_item.get("contract_execution_id")
                    or "",
                    "parent_contract_execution_id": state_item.get(
                        "parent_contract_execution_id"
                    )
                    or "",
                    "state": execution_item.get("state") or "",
                    "next_legal_action": _next_action_summary(state_action),
                    "selected_successor_contract": item.get("selected") or {},
                    "required_evidence": state_item.get("required_evidence") or [],
                    "completed_evidence": [
                        completed.get("id")
                        for completed in (state_item.get("completed_evidence") or [])
                        if isinstance(completed, Mapping) and completed.get("id")
                    ],
                    "missing_evidence": state_item.get("missing_evidence") or [],
                }
            )
    if recursive_next_legal_action:
        recursive_action_execution_id = str(
            recursive_next_legal_action.get("contract_execution_id") or ""
        ).strip()
        if recursive_action_execution_id:
            for lane in lane_contract_executions:
                if (
                    str(lane.get("contract_execution_id") or "").strip()
                    == recursive_action_execution_id
                ):
                    lane["next_legal_action"] = _next_action_summary(
                        recursive_next_legal_action
                    )
    task_id = _projection_task_id(row, backlog_id)
    route_action_precheck_ref = _completed_requirement_event_ref(
        completed_sources,
        "route_action_precheck",
    )
    next_legal_action = _enrich_next_action_append_hint(
        next_legal_action,
        lane_contract_executions=lane_contract_executions,
        active_execution=active_execution,
        route_binding=route_binding,
        task_id=task_id,
        route_action_precheck_ref=route_action_precheck_ref,
    )
    successor_next_legal_action = _enrich_next_action_append_hint(
        successor_next_legal_action,
        lane_contract_executions=lane_contract_executions,
        active_execution=active_execution,
        route_binding=route_binding,
        task_id=task_id,
        route_action_precheck_ref=route_action_precheck_ref,
    )
    if next_legal_action:
        action_execution_id = str(
            next_legal_action.get("contract_execution_id") or ""
        ).strip()
        for lane in lane_contract_executions:
            if (
                action_execution_id
                and str(lane.get("contract_execution_id") or "").strip()
                != action_execution_id
            ):
                continue
            lane_action = _mapping(lane.get("next_legal_action"))
            if not lane_action:
                continue
            if str(lane_action.get("id") or "") != str(
                next_legal_action.get("id") or ""
            ):
                continue
            lane["next_legal_action"] = _next_action_summary(next_legal_action)
    contract_execution_index = {
        str(item.get("contract_execution_id") or ""): item
        for item in lane_contract_executions
        if str(item.get("contract_execution_id") or "").strip()
    }
    active_lane_contract = (
        lane_contract_executions[-1] if lane_contract_executions else {}
    )
    runtime_contract_hints = _runtime_contract_hints(
        root=root,
        active_execution=active_execution,
        active_lane_contract=active_lane_contract,
        next_legal_action=next_legal_action,
        lane_contract_executions=lane_contract_executions,
        route_binding=route_binding,
    )
    executable_contract = _executable_contract_envelope(
        active_execution=active_execution,
        active_lane_contract=active_lane_contract,
        next_legal_action=next_legal_action,
        runtime_hints=runtime_contract_hints,
    )

    return {
        "schema_version": schema_version,
        "source_of_truth": "Contract/Revision/Event",
        "contract_id": contract_id,
        "contract_template_id": template_id,
        "backlog_id": backlog_id,
        "current_revision_id": current_revision_id,
        "state": state,
        "status": state,
        "legacy_no_contract": legacy_no_contract,
        "contract_binding": binding,
        "route_binding": route_binding,
        "test_route": _latest_test_route(rows, root),
        "contract_chain_id": contract_chain_id,
        "root_contract_execution": active_execution if contract_chain_id else {},
        "active_contract_execution": active_execution,
        "contract_chain": contract_chain,
        "successor_contract_candidates": successor_candidates,
        "successor_contract_policy": successor_policy,
        "selected_successor_contract": selected_successor,
        "selected_successor_contract_state": selected_successor_contract_state,
        "successor_next_legal_action": successor_next_legal_action,
        "contract_lane_executions": lane_contract_executions,
        "contract_execution_index": contract_execution_index,
        "active_lane_contract": active_lane_contract,
        "runtime_contract_hints": runtime_contract_hints,
        "executable_contract": executable_contract,
        "completed_contract_executions": completed_contract_executions,
        "required_evidence": [step["id"] for step in requirement_steps],
        "conditional_required_evidence": conditional_groups,
        "completed_evidence": [
            {"id": requirement_id, **source}
            for requirement_id, source in completed_sources.items()
        ],
        "missing_evidence": [step["id"] for step in ordered_next_steps],
        "blocked_evidence": [step["id"] for step in blocked_steps],
        "ordered_next_steps": ordered_next_steps,
        "next_legal_action": next_legal_action,
        "requirements_explicit": requirements_explicit,
        "contract_complete": contract_complete,
        "projection_watermark": projection_watermark,
        "contract_projection": projection,
        "close_ready_policy": {
            "source": "rule_gate_projection",
            "timeline_event_is_authoritative": False,
        },
    }
