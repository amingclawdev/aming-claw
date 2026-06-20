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
    "handoff_event_id",
    "handoff_preconditions",
    "handoff_reason",
    "parent_contract_execution_id",
    "selected_by_actor",
    "state",
    "status",
    "successor_backlog_id",
    "successor_contract_execution_id",
    "successor_contract_instance_id",
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


def _event_numeric_id(event: Mapping[str, Any]) -> int:
    for key in ("id", "event_id"):
        try:
            return int(event.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return 0


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
        field_value = value.get(field)
        if field_value:
            binding[field] = field_value
    return binding


def _latest_contract_binding(
    events: list[dict[str, Any]],
    root: Mapping[str, Any],
) -> dict[str, Any]:
    binding = _contract_binding_from_mapping(root)
    for event in events:
        event_kind = str(event.get("event_kind") or "").strip()
        for payload in _event_payloads(event):
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
    for key, expected in condition.items():
        if key == "target_kind":
            actual = (
                root.get("target_kind")
                or root.get("target_project_kind")
                or backlog_row.get("target_kind")
                or backlog_row.get("target_project_kind")
                or backlog_row.get("project_kind")
            )
            if not actual and (
                root.get("generated_demo") is True
                or backlog_row.get("generated_demo") is True
            ):
                actual = "generated_demo"
        else:
            actual = root.get(key, backlog_row.get(key))
        if str(actual or "").strip() != str(expected or "").strip():
            return False
    return True


def _normalize_requirement(
    item: Any,
    *,
    index: int,
    condition: Mapping[str, Any] | None = None,
    conditional: bool = False,
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
    step.setdefault("order", index)
    step["conditional"] = bool(conditional)
    if condition:
        step["condition"] = dict(condition)
    step["prerequisite_ids"] = _string_list(
        step.get("prerequisite_ids") or step.get("requires")
    )
    step["accepted_event_kinds"] = _string_list(
        step.get("accepted_event_kinds") or step.get("event_kinds")
    ) or [requirement_id]
    step["accepted_statuses"] = [
        item.lower()
        for item in (
            _string_list(step.get("accepted_statuses"))
            or sorted(CONTRACT_PASS_STATUSES)
        )
    ]
    return step


def _requirement_steps(
    root: Mapping[str, Any],
    backlog_row: Mapping[str, Any],
    *,
    default_required_evidence: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw = root.get("evidence_requirements") or root.get("required_evidence") or []
    if not raw:
        raw = default_required_evidence
    required: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        step = _normalize_requirement(item, index=index)
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
        event_execution_id = str(
            event.get("contract_execution_id")
            or event.get("successor_contract_execution_id")
            or ""
        ).strip()
        if not event_execution_id:
            for payload in _event_payloads(event):
                event_execution_id = _payload_field_value(
                    payload,
                    {
                        "active_contract_execution_id",
                        "contract_execution_id",
                        "reviewed_contract_execution_id",
                        "successor_contract_execution_id",
                    },
                )
                if event_execution_id:
                    break
        if event_execution_id != required_execution_id:
            return False, None
    source = {
        "event_id": event.get("id") or event.get("event_id") or "",
        "event_kind": event_kind,
        "status": str(event.get("status") or ""),
        "phase": str(event.get("phase") or ""),
    }
    if required_execution_id:
        source["contract_execution_id"] = required_execution_id
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


def build_contract_state_projection(
    events: list[dict[str, Any]] | None,
    contract: Mapping[str, Any] | None = None,
    backlog_row: Mapping[str, Any] | None = None,
    *,
    contract_projection: Mapping[str, Any] | None = None,
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
    next_legal_action: dict[str, Any] | None = None
    if has_explicit_contract and requirements_explicit and ordered_next_steps:
        first = ordered_next_steps[0]
        next_legal_action = {
            "id": first["id"],
            "action": first.get("action") or f"record_{first['id']}",
            "detail": f"record contract evidence for {first['id']}",
            "source": first.get("source") or "contract_state",
            "precedence": first.get("precedence") or "active_contract_missing_step",
            "blocked_by": first.get("blocked_by") or [],
            "ordered_missing_steps_source": "contract_state",
            "ordered_missing_steps": ordered_next_steps,
        }

    route_binding = _latest_route_binding(rows, root)
    route_identity_hash = _route_identity_hash(route_binding)
    projection_watermark = projection.get("projection_watermark", 0)
    if not projection_watermark:
        projection_watermark = max((_event_numeric_id(event) for event in rows), default=0) or len(rows)
    active_execution: dict[str, Any] = {}
    if has_explicit_contract:
        active_execution = {
            "schema_version": "active_contract_execution.v1",
            "project_id": project_id,
            "backlog_id": backlog_id,
            "contract_execution_id": str(root.get("contract_execution_id") or "")
            or _execution_id(
                project_id=project_id,
                backlog_id=backlog_id,
                contract_id=contract_id,
                template_id=template_id,
                revision_id=current_revision_id,
            ),
            "contract_id": contract_id,
            "contract_template_id": template_id,
            "contract_instance_id": str(root.get("contract_instance_id") or binding.get("contract_instance_id") or ""),
            "contract_revision_id": current_revision_id,
            "state": state,
            "projection_watermark": projection_watermark,
            "observer_session_id": str(root.get("observer_session_id") or ""),
            "route_identity_hash": route_identity_hash,
            "route_token_ref": str(route_binding.get("route_token_ref") or ""),
            "prompt_contract_id": str(route_binding.get("prompt_contract_id") or ""),
            "visible_injection_manifest_hash": str(route_binding.get("visible_injection_manifest_hash") or ""),
        }

    contract_complete = (
        has_explicit_contract and requirements_explicit and not ordered_next_steps
    )
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

    successor_next_legal_action: dict[str, Any] | None = None
    if selected_successor:
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
    elif contract_complete and successor_candidates:
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

    if next_legal_action and active_execution:
        next_legal_action = {
            **next_legal_action,
            "requirement_id": next_legal_action.get("id") or "",
            "contract_chain_id": contract_chain_id,
            "contract_execution_id": active_execution.get("contract_execution_id") or "",
            "backlog_id": backlog_id,
            "route_token_ref": active_execution.get("route_token_ref") or "",
            "projection_watermark": projection_watermark,
        }
    elif successor_next_legal_action:
        next_legal_action = dict(successor_next_legal_action)

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
        "successor_next_legal_action": successor_next_legal_action,
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
