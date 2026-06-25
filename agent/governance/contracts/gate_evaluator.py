"""Pure ContractGateKernel evaluator."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .gate_adapters import (
    adapter_errors,
    adapter_warnings,
    submit_line_write_gate_adapter,
    warning_adapter_result,
)
from .gate_decision import ContractGateDecision, make_gate_decision
from .gate_policy import (
    normalize_gate_policy,
    parent_contract_allowed,
    requires_parent_execution,
    root_start_allowed,
)


def evaluate_contract_gate(
    definition: Mapping[str, Any],
    *,
    action: str,
    actor_role: str = "",
    execution_state: Mapping[str, Any] | None = None,
    runtime_guide: Mapping[str, Any] | None = None,
    write: Mapping[str, Any] | None = None,
    subject: Mapping[str, Any] | None = None,
    status_inputs: Mapping[str, Any] | None = None,
) -> ContractGateDecision:
    policy = normalize_gate_policy(definition)
    subject = subject if isinstance(subject, Mapping) else {}
    status_inputs = status_inputs if isinstance(status_inputs, Mapping) else {}
    execution_state = execution_state if isinstance(execution_state, Mapping) else {}
    runtime_guide = runtime_guide if isinstance(runtime_guide, Mapping) else {}
    errors: list[str] = []
    warnings: list[str] = []
    imported_legacy_checks: list[dict[str, Any]] = []
    gate_id = f"{action}:{policy['contract_id']}"
    gate_type = "runtime_precheck"
    next_move: dict[str, Any] = {}

    if action == "start_execution":
        _evaluate_start_execution(
            policy,
            subject=subject,
            errors=errors,
            warnings=warnings,
            next_move=next_move,
        )
    elif action == "submit_line":
        adapter = submit_line_write_gate_adapter(
            definition,
            execution_state,
            write or {},
            runtime_guide=runtime_guide,
        )
        imported_legacy_checks.append(adapter)
        errors.extend(adapter_errors([adapter]))
        warnings.extend(adapter_warnings([adapter]))
    elif action in {"current_state", "current_guide"}:
        if not policy.get("explicit_system_layer"):
            imported_legacy_checks.append(
                warning_adapter_result(
                    "legacy_system_layer_default.v1",
                    "contract system_layer policy is defaulted; root/successor gates are warning-only",
                )
            )
    else:
        warnings.append(f"contract gate action {action!r} has no specialized policy")

    _append_status_warnings(status_inputs, warnings=warnings, imported=imported_legacy_checks)
    next_line = runtime_guide.get("next_legal_action")
    if isinstance(next_line, Mapping) and not next_move:
        next_move = dict(next_line)
    return make_gate_decision(
        action=action,
        gate_id=gate_id,
        gate_type=gate_type,
        errors=errors,
        warnings=warnings,
        next_move=next_move,
        stage_id=str((write or {}).get("stage_id") or (next_move or {}).get("stage_id") or ""),
        line_id=str((write or {}).get("line_id") or (next_move or {}).get("line_id") or ""),
        required_role=str((next_move or {}).get("owner_role") or ""),
        actor_role=actor_role,
        missing_proof_fields=_missing_proof_fields(errors),
        hash_status=_hash_status(definition, execution_state, runtime_guide),
        graph_status=_mapping(status_inputs.get("graph_status")),
        dirty_scope_status=_mapping(status_inputs.get("dirty_scope_status")),
        imported_legacy_checks=imported_legacy_checks,
        policy_hash=str(policy.get("policy_hash") or ""),
        contract_definition_hash=str(definition.get("definition_hash") or ""),
        execution_state_revision=int(execution_state.get("execution_state_revision") or 0),
        runtime_guide_hash=str(runtime_guide.get("runtime_guide_hash") or ""),
    )


def _evaluate_start_execution(
    policy: Mapping[str, Any],
    *,
    subject: Mapping[str, Any],
    errors: list[str],
    warnings: list[str],
    next_move: dict[str, Any],
) -> None:
    parent_execution_id = str(subject.get("parent_contract_execution_id") or "")
    contract_id = str(policy.get("contract_id") or "")
    if not parent_execution_id:
        if root_start_allowed(policy):
            return
        if policy.get("explicit_system_layer"):
            errors.append("contract_root_start_denied")
            next_move.update(
                {
                    "action": "start_onboard_contract",
                    "contract_id": "onboard_contract",
                    "reason": (
                        f"{contract_id} cannot be started as a root execution; "
                        "enter it through onboard successor policy"
                    ),
                }
            )
            return
        warnings.append("contract_root_start_policy_defaulted")
        return

    if requires_parent_execution(policy):
        for field in (
            "parent_contract_execution_id",
            "root_contract_execution_id",
            "contract_chain_id",
        ):
            if not str(subject.get(field) or ""):
                errors.append(f"missing {field}")
        parent_contract = _mapping(subject.get("parent_contract"))
        if not parent_contract:
            errors.append("missing parent_contract")
        elif not parent_contract_allowed(policy, parent_contract):
            errors.append("parent_contract_not_allowed")
        return
    warnings.append("successor_started_without_explicit_requires_parent_policy")


def _append_status_warnings(
    status_inputs: Mapping[str, Any],
    *,
    warnings: list[str],
    imported: list[dict[str, Any]],
) -> None:
    if status_inputs.get("runtime_match") is False:
        warning = "advanced_chain_runtime_match_false"
        warnings.append(warning)
        imported.append(
            warning_adapter_result(
                "advanced_chain_runtime_match.v1",
                warning,
                evidence={"runtime_match": False},
            )
        )
    graph_status = _mapping(status_inputs.get("graph_status"))
    if graph_status.get("graph_stale") or graph_status.get("is_stale"):
        warnings.append("graph_stale_read_only_hint")
    dirty_scope = _mapping(status_inputs.get("dirty_scope_status"))
    if dirty_scope.get("dirty") or dirty_scope.get("dirty_files"):
        warnings.append("dirty_scope_read_only_hint")


def _hash_status(
    definition: Mapping[str, Any],
    execution_state: Mapping[str, Any],
    runtime_guide: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "contract_gate_hash_status.v1",
        "contract_definition_hash": str(definition.get("definition_hash") or ""),
        "definition_source_sha256": str(definition.get("source_sha256") or ""),
        "execution_state_hash": str(execution_state.get("execution_state_hash") or ""),
        "runtime_guide_hash": str(runtime_guide.get("runtime_guide_hash") or ""),
    }


def _missing_proof_fields(errors: list[str]) -> list[str]:
    fields: list[str] = []
    for error in errors:
        if error.startswith("missing "):
            fields.append(error.removeprefix("missing "))
    return fields


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
