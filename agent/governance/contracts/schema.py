"""Validation and normalized schema for source-controlled contracts."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
import re
from pathlib import PurePosixPath
from typing import Any

from .hash import definition_hash


CONTRACT_DEFINITION_SCHEMA_VERSION = "contract_definition.v1"
VALID_CONTRACT_STATUSES = frozenset({"draft", "active", "deprecated"})
VALID_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SYSTEM_LAYER_SCHEMA_VERSION = "contract_system_layer.v1"
SYSTEM_LAYER_POLICY_NAMES = (
    "entrypoint_policy",
    "successor_policy",
    "write_authority_policy",
    "next_action_policy",
    "projection_policy",
    "route_policy",
    "authority_policy",
    "graph_binding_policy",
)


class ContractDefinitionError(ValueError):
    """Raised when a source-controlled contract definition is invalid."""


class ContractLifecycleError(ContractDefinitionError):
    """Raised when a definition lifecycle operation is not allowed."""


def is_contract_definition_payload(payload: Mapping[str, Any]) -> bool:
    return payload.get("schema_version") == CONTRACT_DEFINITION_SCHEMA_VERSION


def normalize_definition(
    payload: Mapping[str, Any],
    *,
    source_path: str = "",
) -> dict[str, Any]:
    """Validate and normalize a contract definition config payload."""

    if not is_contract_definition_payload(payload):
        raise ContractDefinitionError(
            f"schema_version must be {CONTRACT_DEFINITION_SCHEMA_VERSION!r}"
        )

    normalized: dict[str, Any] = {
        "schema_version": CONTRACT_DEFINITION_SCHEMA_VERSION,
        "contract_id": _required_str(payload, "contract_id"),
        "version": _required_str(payload, "version"),
        "revision": _required_str(payload, "revision"),
        "role": _required_str(payload, "role"),
        "contract_type": _contract_type(payload),
        "status": _status(payload),
        "compat_aliases": _string_list(payload.get("compat_aliases"), "compat_aliases"),
        "successors": _successor_list(payload.get("successors")),
        "system_layer": _normalize_system_layer(payload.get("system_layer")),
        "rule_layer": _normalize_rule_layer(payload.get("rule_layer")),
        "instruction_layer": _normalize_instruction_layer(payload.get("instruction_layer")),
    }
    if isinstance(payload.get("metadata"), Mapping):
        normalized["metadata"] = dict(payload["metadata"])
    normalized["definition_hash"] = definition_hash(normalized)
    normalized["read_model"] = _build_read_model(normalized)
    if source_path:
        normalized["_source_path"] = source_path
    return normalized


def iter_stage_lines(definition: Mapping[str, Any]) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    for stage in definition.get("rule_layer", {}).get("stages", []):
        if not isinstance(stage, Mapping):
            continue
        for line in stage.get("lines", []):
            if isinstance(line, Mapping):
                yield dict(stage), dict(line)


def find_line(
    definition: Mapping[str, Any],
    *,
    stage_id: str,
    line_id: str,
) -> dict[str, Any]:
    for stage, line in iter_stage_lines(definition):
        if stage.get("stage_id") == stage_id and line.get("line_id") == line_id:
            found = dict(line)
            found["stage_id"] = stage_id
            return found
    raise ContractDefinitionError(
        f"unknown contract line stage_id={stage_id!r} line_id={line_id!r}"
    )


def is_new_execution_allowed(definition: Mapping[str, Any]) -> bool:
    """Return whether a definition can start new contract executions."""

    return definition.get("status") == "active"


def _required_str(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ContractDefinitionError(f"{field} must be a non-empty string")
    return value.strip()


def _contract_type(payload: Mapping[str, Any]) -> str:
    value = payload.get("contract_type", payload.get("type"))
    if not isinstance(value, str) or not value.strip():
        raise ContractDefinitionError("contract_type must be a non-empty string")
    return value.strip()


def _status(payload: Mapping[str, Any]) -> str:
    value = payload.get("status", "draft")
    if not isinstance(value, str) or value not in VALID_CONTRACT_STATUSES:
        raise ContractDefinitionError(
            f"status must be one of {sorted(VALID_CONTRACT_STATUSES)!r}"
        )
    return value


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ContractDefinitionError(f"{field} must be a list of strings")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ContractDefinitionError(f"{field} must be a list of strings")
        token = item.strip()
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _successor_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ContractDefinitionError("successors must be a list")
    successors: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ContractDefinitionError(f"successors[{index}] must be an object")
        contract_id = item.get("contract_id") or item.get("contract_template_id")
        if not isinstance(contract_id, str) or not contract_id.strip():
            raise ContractDefinitionError(f"successors[{index}] missing contract_id")
        successors.append(dict(item))
    return successors


def _normalize_system_layer(value: Any) -> dict[str, Any]:
    embedded_status: Mapping[str, Any] | None = None
    if value is None:
        source: Mapping[str, Any] = {}
        layer_status = "legacy_default_deny"
        layer_defaulted = True
        explicit = False
    elif not isinstance(value, Mapping):
        raise ContractDefinitionError("system_layer must be an object")
    else:
        source = value
        embedded_status = _embedded_system_layer_policy_status(source)
        if embedded_status is not None:
            explicit = bool(embedded_status.get("explicit"))
            layer_defaulted = bool(embedded_status.get("defaulted"))
            layer_status = str(embedded_status.get("status") or "partial_default_deny")
        else:
            explicit = True
            missing = [name for name in SYSTEM_LAYER_POLICY_NAMES if name not in source]
            layer_defaulted = bool(missing)
            layer_status = "explicit" if not missing else "partial_default_deny"

    normalized: dict[str, Any] = {
        "schema_version": SYSTEM_LAYER_SCHEMA_VERSION,
    }
    if embedded_status is not None:
        missing_policies = _string_list(
            embedded_status.get("missing_policies"),
            "system_layer.policy_status.missing_policies",
        )
        defaulted_policies = _string_list(
            embedded_status.get("defaulted_policies"),
            "system_layer.policy_status.defaulted_policies",
        )
        explicit_policies = _string_list(
            embedded_status.get("explicit_policies"),
            "system_layer.policy_status.explicit_policies",
        )
    else:
        missing_policies: list[str] = []
        defaulted_policies: list[str] = []
        explicit_policies: list[str] = []
    for policy_name in SYSTEM_LAYER_POLICY_NAMES:
        if policy_name in source:
            normalized[policy_name] = _normalize_system_policy(
                source[policy_name],
                policy_name=policy_name,
                defaulted=False,
            )
            if embedded_status is None:
                explicit_policies.append(policy_name)
        else:
            normalized[policy_name] = _normalize_system_policy(
                None,
                policy_name=policy_name,
                defaulted=True,
            )
            if embedded_status is None:
                missing_policies.append(policy_name)
                defaulted_policies.append(policy_name)

    for key, child in source.items():
        key_text = str(key)
        if key_text in SYSTEM_LAYER_POLICY_NAMES or key_text == "schema_version":
            continue
        normalized[key_text] = child

    normalized["policy_status"] = {
        "schema_version": "contract_system_layer_policy_status.v1",
        "status": layer_status,
        "explicit": explicit,
        "defaulted": layer_defaulted,
        "deny_by_default": True,
        "missing_policies": missing_policies,
        "defaulted_policies": defaulted_policies,
        "explicit_policies": explicit_policies,
    }
    return normalized


def _embedded_system_layer_policy_status(source: Mapping[str, Any]) -> Mapping[str, Any] | None:
    status = source.get("policy_status")
    if not isinstance(status, Mapping):
        return None
    status_token = str(status.get("status") or "")
    if status_token not in {"legacy_default_deny", "partial_default_deny"}:
        return None
    return status


def _normalize_system_policy(
    value: Any,
    *,
    policy_name: str,
    defaulted: bool,
) -> dict[str, Any]:
    if value is None:
        return {
            "schema_version": "contract_system_policy.v1",
            "policy_name": policy_name,
            "policy_status": "legacy_default_deny",
            "defaulted": True,
            "deny_by_default": True,
            "allowed": False,
        }
    if not isinstance(value, Mapping):
        raise ContractDefinitionError(f"system_layer.{policy_name} must be an object")
    normalized = dict(value)
    normalized.setdefault("schema_version", "contract_system_policy.v1")
    normalized.setdefault("policy_name", policy_name)
    normalized.setdefault("policy_status", "explicit")
    normalized.setdefault("defaulted", defaulted)
    normalized.setdefault("deny_by_default", True)
    return normalized


def _normalize_rule_layer(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractDefinitionError("rule_layer must be an object")
    stages = value.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ContractDefinitionError("rule_layer.stages must be a non-empty list")
    normalized_stages: list[dict[str, Any]] = []
    seen_stages: set[str] = set()
    for index, stage in enumerate(stages):
        if not isinstance(stage, Mapping):
            raise ContractDefinitionError(f"rule_layer.stages[{index}] must be an object")
        stage_id = _required_str(stage, "stage_id")
        if stage_id in seen_stages:
            raise ContractDefinitionError(f"duplicate stage_id {stage_id!r}")
        seen_stages.add(stage_id)
        normalized_stages.append(
            {
                "stage_id": stage_id,
                "description": _optional_str(stage.get("description")),
                "lines": _normalize_lines(stage.get("lines"), stage_id=stage_id),
            }
        )
    out = {"stages": normalized_stages}
    transitions = value.get("transitions")
    if transitions is not None:
        if not isinstance(transitions, list):
            raise ContractDefinitionError("rule_layer.transitions must be a list")
        out["transitions"] = [dict(item) for item in transitions if isinstance(item, Mapping)]
    return out


def _normalize_lines(value: Any, *, stage_id: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ContractDefinitionError(f"stage {stage_id!r} lines must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, line in enumerate(value):
        if not isinstance(line, Mapping):
            raise ContractDefinitionError(f"stage {stage_id!r} line[{index}] must be an object")
        line_id = _required_str(line, "line_id")
        if line_id in seen:
            raise ContractDefinitionError(
                f"stage {stage_id!r} duplicate line_id {line_id!r}"
            )
        seen.add(line_id)
        owner_role = _required_str(line, "owner_role")
        allowed_writer_roles = _string_list(
            line.get("allowed_writer_roles", [owner_role]),
            f"stage {stage_id!r} line {line_id!r} allowed_writer_roles",
        )
        normalized_line = {
            "line_id": line_id,
            "owner_role": owner_role,
            "allowed_writer_roles": allowed_writer_roles,
            "evidence_kind": _optional_str(line.get("evidence_kind")),
            "required": bool(line.get("required", True)),
            "description": _optional_str(line.get("description")),
        }
        if isinstance(line.get("requires"), list):
            normalized_line["requires"] = _string_list(
                line.get("requires"),
                f"stage {stage_id!r} line {line_id!r} requires",
            )
        normalized.append(normalized_line)
    return normalized


def _normalize_instruction_layer(value: Any) -> dict[str, Any]:
    if value is None:
        return {"inline": [], "refs": []}
    if not isinstance(value, Mapping):
        raise ContractDefinitionError("instruction_layer must be an object")
    inline = _string_list(value.get("inline"), "instruction_layer.inline")
    refs = value.get("refs") or []
    if not isinstance(refs, list):
        raise ContractDefinitionError("instruction_layer.refs must be a list")
    normalized_refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, ref in enumerate(refs):
        if not isinstance(ref, Mapping):
            raise ContractDefinitionError(f"instruction_layer.refs[{index}] must be an object")
        ref_id = _required_str(ref, "id")
        if ref_id in seen:
            raise ContractDefinitionError(f"duplicate instruction ref id {ref_id!r}")
        seen.add(ref_id)
        path = _safe_instruction_path(_required_str(ref, "path"))
        expected_hash = ref.get("sha256") or ref.get("hash")
        if expected_hash is not None and (
            not isinstance(expected_hash, str) or not VALID_HASH_RE.match(expected_hash)
        ):
            raise ContractDefinitionError(f"instruction ref {ref_id!r} has invalid sha256")
        normalized_refs.append(
            {
                "id": ref_id,
                "path": path,
                "sha256": expected_hash or "",
                "visible_to_roles": _string_list(
                    ref.get("visible_to_roles"),
                    f"instruction ref {ref_id!r} visible_to_roles",
                ),
                "stage_ids": _string_list(
                    ref.get("stage_ids"),
                    f"instruction ref {ref_id!r} stage_ids",
                ),
            }
        )
    return {"inline": inline, "refs": normalized_refs}


def _build_read_model(definition: Mapping[str, Any]) -> dict[str, Any]:
    rule_lines: list[dict[str, Any]] = []
    required_evidence: list[dict[str, Any]] = []
    writer_roles: list[str] = []
    seen_writer_roles: set[str] = set()
    for stage, line in iter_stage_lines(definition):
        entry = {
            "stage_id": str(stage.get("stage_id") or ""),
            "stage_description": str(stage.get("description") or ""),
            "line_id": str(line.get("line_id") or ""),
            "owner_role": str(line.get("owner_role") or ""),
            "allowed_writer_roles": list(line.get("allowed_writer_roles") or []),
            "evidence_kind": str(line.get("evidence_kind") or ""),
            "required": bool(line.get("required", True)),
            "description": str(line.get("description") or ""),
        }
        if line.get("requires"):
            entry["requires"] = list(line.get("requires") or [])
        rule_lines.append(entry)
        for role in entry["allowed_writer_roles"]:
            if role not in seen_writer_roles:
                seen_writer_roles.add(role)
                writer_roles.append(role)
        if entry["required"]:
            required_evidence.append(
                {
                    "stage_id": entry["stage_id"],
                    "line_id": entry["line_id"],
                    "owner_role": entry["owner_role"],
                    "allowed_writer_roles": list(entry["allowed_writer_roles"]),
                    "evidence_kind": entry["evidence_kind"],
                    "required": True,
                }
            )
    return {
        "schema_version": "contract_definition_read_model.v1",
        "contract_id": definition.get("contract_id", ""),
        "version": definition.get("version", ""),
        "revision": definition.get("revision", ""),
        "role": definition.get("role", ""),
        "contract_type": definition.get("contract_type", ""),
        "status": definition.get("status", ""),
        "definition_hash": definition.get("definition_hash", ""),
        "system_layer": dict(definition.get("system_layer") or {}),
        "system_layer_policy_status": dict(
            (definition.get("system_layer") or {}).get("policy_status") or {}
        ),
        "compat_aliases": list(definition.get("compat_aliases") or []),
        "successors": [dict(item) for item in definition.get("successors") or []],
        "instruction_layer": {
            "inline": list(
                (definition.get("instruction_layer") or {}).get("inline") or []
            ),
            "refs": [
                dict(ref)
                for ref in (definition.get("instruction_layer") or {}).get("refs") or []
            ],
        },
        "rule_lines": rule_lines,
        "required_evidence": required_evidence,
        "allowed_writer_roles": writer_roles,
    }


def _safe_instruction_path(path_text: str) -> str:
    path = PurePosixPath(path_text.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ContractDefinitionError("instruction ref paths must be relative and contained")
    return path.as_posix()


def _optional_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
