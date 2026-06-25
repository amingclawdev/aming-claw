"""Decision envelope for source-backed ContractRuntime gates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .hash import stable_sha256


CONTRACT_GATE_DECISION_SCHEMA_VERSION = "contract_gate_decision.v1"


@dataclass(frozen=True)
class ContractGateDecision:
    """Common allow/warn/block decision returned by ContractGateKernel."""

    decision: str
    action: str
    gate_id: str
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    next_move: Mapping[str, Any] | None = None
    gate_type: str = ""
    stage_id: str = ""
    line_id: str = ""
    required_role: str = ""
    actor_role: str = ""
    missing_lines: tuple[str, ...] = ()
    missing_proof_fields: tuple[str, ...] = ()
    hash_status: Mapping[str, Any] = field(default_factory=dict)
    graph_status: Mapping[str, Any] = field(default_factory=dict)
    dirty_scope_status: Mapping[str, Any] = field(default_factory=dict)
    imported_legacy_checks: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    projection_actions: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    policy_hash: str = ""
    contract_definition_hash: str = ""
    execution_state_revision: int = 0
    runtime_guide_hash: str = ""

    @property
    def ok(self) -> bool:
        return self.decision in {"allow", "warn"}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": CONTRACT_GATE_DECISION_SCHEMA_VERSION,
            "ok": self.ok,
            "decision": self.decision,
            "action": self.action,
            "gate_id": self.gate_id,
            "gate_type": self.gate_type,
            "stage_id": self.stage_id,
            "line_id": self.line_id,
            "required_role": self.required_role,
            "actor_role": self.actor_role,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "missing_lines": list(self.missing_lines),
            "missing_proof_fields": list(self.missing_proof_fields),
            "hash_status": dict(self.hash_status),
            "graph_status": dict(self.graph_status),
            "dirty_scope_status": dict(self.dirty_scope_status),
            "imported_legacy_checks": [dict(item) for item in self.imported_legacy_checks],
            "projection_actions": [dict(item) for item in self.projection_actions],
            "policy_hash": self.policy_hash,
            "contract_definition_hash": self.contract_definition_hash,
            "execution_state_revision": self.execution_state_revision,
            "runtime_guide_hash": self.runtime_guide_hash,
            "next_move": dict(self.next_move or {}),
        }
        payload["decision_hash"] = stable_sha256(
            {key: value for key, value in payload.items() if key != "decision_hash"}
        )
        return payload


def make_gate_decision(
    *,
    action: str,
    gate_id: str,
    errors: Sequence[str] = (),
    warnings: Sequence[str] = (),
    next_move: Mapping[str, Any] | None = None,
    gate_type: str = "",
    stage_id: str = "",
    line_id: str = "",
    required_role: str = "",
    actor_role: str = "",
    missing_lines: Sequence[str] = (),
    missing_proof_fields: Sequence[str] = (),
    hash_status: Mapping[str, Any] | None = None,
    graph_status: Mapping[str, Any] | None = None,
    dirty_scope_status: Mapping[str, Any] | None = None,
    imported_legacy_checks: Sequence[Mapping[str, Any]] = (),
    projection_actions: Sequence[Mapping[str, Any]] = (),
    policy_hash: str = "",
    contract_definition_hash: str = "",
    execution_state_revision: int = 0,
    runtime_guide_hash: str = "",
) -> ContractGateDecision:
    normalized_errors = tuple(str(item) for item in errors if str(item or "").strip())
    normalized_warnings = tuple(str(item) for item in warnings if str(item or "").strip())
    decision = "block" if normalized_errors else "warn" if normalized_warnings else "allow"
    return ContractGateDecision(
        decision=decision,
        action=str(action or ""),
        gate_id=str(gate_id or ""),
        errors=normalized_errors,
        warnings=normalized_warnings,
        next_move=dict(next_move or {}),
        gate_type=str(gate_type or ""),
        stage_id=str(stage_id or ""),
        line_id=str(line_id or ""),
        required_role=str(required_role or ""),
        actor_role=str(actor_role or ""),
        missing_lines=tuple(str(item) for item in missing_lines if str(item or "").strip()),
        missing_proof_fields=tuple(
            str(item) for item in missing_proof_fields if str(item or "").strip()
        ),
        hash_status=dict(hash_status or {}),
        graph_status=dict(graph_status or {}),
        dirty_scope_status=dict(dirty_scope_status or {}),
        imported_legacy_checks=tuple(dict(item) for item in imported_legacy_checks),
        projection_actions=tuple(dict(item) for item in projection_actions),
        policy_hash=str(policy_hash or ""),
        contract_definition_hash=str(contract_definition_hash or ""),
        execution_state_revision=int(execution_state_revision or 0),
        runtime_guide_hash=str(runtime_guide_hash or ""),
    )
