"""Normalize source-backed ContractRuntime gate policy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .hash import stable_sha256


GATE_POLICY_SCHEMA_VERSION = "contract_gate_policy.v1"


def normalize_gate_policy(definition: Mapping[str, Any]) -> dict[str, Any]:
    """Return the first-class gate policy read model for a contract definition."""

    system_layer = (
        definition.get("system_layer")
        if isinstance(definition.get("system_layer"), Mapping)
        else {}
    )
    entrypoint_policy = _policy(system_layer, "entrypoint_policy")
    successor_policy = _policy(system_layer, "successor_policy")
    explicit = _system_layer_explicit(system_layer)
    gate_policy = (
        definition.get("gate_policy")
        if isinstance(definition.get("gate_policy"), Mapping)
        else {}
    )
    precheck_policy = (
        definition.get("precheck_policy")
        if isinstance(definition.get("precheck_policy"), Mapping)
        else {}
    )
    normalized = {
        "schema_version": GATE_POLICY_SCHEMA_VERSION,
        "contract_id": str(definition.get("contract_id") or ""),
        "version": str(definition.get("version") or ""),
        "revision": str(definition.get("revision") or ""),
        "contract_definition_hash": str(definition.get("definition_hash") or ""),
        "explicit_system_layer": explicit,
        "entrypoint_policy": entrypoint_policy,
        "successor_policy": successor_policy,
        "gate_policy": dict(gate_policy),
        "precheck_policy": dict(precheck_policy),
        "legacy_adapters": _legacy_adapters(gate_policy, precheck_policy),
    }
    normalized["policy_hash"] = stable_sha256(
        {key: value for key, value in normalized.items() if key != "policy_hash"}
    )
    return normalized


def root_start_allowed(policy: Mapping[str, Any]) -> bool:
    entrypoint = _mapping(policy.get("entrypoint_policy"))
    if entrypoint.get("allow_root_start") is True:
        allowed = _contract_ids(entrypoint.get("allowed_entrypoints"))
        contract_id = str(policy.get("contract_id") or "")
        return not allowed or contract_id in allowed or f"{contract_id}.v1" in allowed
    return False


def requires_parent_execution(policy: Mapping[str, Any]) -> bool:
    entrypoint = _mapping(policy.get("entrypoint_policy"))
    return bool(entrypoint.get("requires_parent_execution")) or (
        entrypoint.get("allow_root_start") is False
        and str(entrypoint.get("root_start_default") or "").startswith("deny")
    )


def parent_contract_allowed(policy: Mapping[str, Any], parent: Mapping[str, Any]) -> bool:
    successor_policy = _mapping(policy.get("successor_policy"))
    allowed = successor_policy.get("allowed_parent_contracts")
    if not isinstance(allowed, Sequence) or isinstance(allowed, (str, bytes, bytearray)):
        return True
    parent_contract_id = str(parent.get("contract_id") or "").strip()
    parent_version = str(parent.get("version") or "").strip()
    if not parent_contract_id:
        return False
    for item in allowed:
        if not isinstance(item, Mapping):
            continue
        allowed_contract_id = str(
            item.get("contract_id") or item.get("contract_template_id") or ""
        ).strip()
        allowed_version = str(item.get("version") or "").strip()
        if allowed_contract_id != parent_contract_id:
            continue
        if allowed_version and allowed_version != parent_version:
            continue
        return True
    return False


def successor_allowed(
    parent_definition: Mapping[str, Any] | None,
    successor_contract_id: str,
) -> bool:
    if not isinstance(parent_definition, Mapping):
        return False
    policy = normalize_gate_policy(parent_definition)
    successor_policy = _mapping(policy.get("successor_policy"))
    candidates = list(successor_policy.get("allowed_successors") or [])
    candidates.extend(parent_definition.get("successors") or [])
    return any(_contract_id_matches(item, successor_contract_id) for item in candidates)


def _policy(system_layer: Mapping[str, Any], policy_name: str) -> dict[str, Any]:
    value = system_layer.get(policy_name)
    return dict(value) if isinstance(value, Mapping) else {}


def _system_layer_explicit(system_layer: Mapping[str, Any]) -> bool:
    status = system_layer.get("policy_status")
    if isinstance(status, Mapping):
        return bool(status.get("explicit")) and not bool(status.get("defaulted"))
    return bool(system_layer)


def _legacy_adapters(
    gate_policy: Mapping[str, Any],
    precheck_policy: Mapping[str, Any],
) -> list[dict[str, Any]]:
    adapters: list[dict[str, Any]] = []
    for source in (gate_policy, precheck_policy):
        raw = source.get("legacy_adapters")
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, Mapping):
                adapters.append(dict(item))
    return adapters


def _contract_ids(value: Any) -> set[str]:
    ids: set[str] = set()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ids
    for item in value:
        if isinstance(item, Mapping):
            text = str(item.get("contract_id") or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            ids.add(text)
    return ids


def _contract_id_matches(item: Any, contract_id: str) -> bool:
    text = str(contract_id or "").strip()
    if not text:
        return False
    if isinstance(item, Mapping):
        candidate = str(item.get("contract_id") or item.get("contract_template_id") or "")
    else:
        candidate = str(item or "")
    candidate = candidate.strip()
    return candidate == text or candidate == f"{text}.v1"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
