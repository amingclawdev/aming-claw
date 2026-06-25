"""Thin facade for ContractRuntime gate evaluation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .gate_decision import ContractGateDecision
from .gate_evaluator import evaluate_contract_gate
from .registry import ContractDefinitionRegistry


class ContractGateKernel:
    """Assemble source policy and return pure gate decisions."""

    def __init__(self, registry: ContractDefinitionRegistry) -> None:
        self.registry = registry

    def precheck(
        self,
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
        return evaluate_contract_gate(
            definition,
            action=action,
            actor_role=actor_role,
            execution_state=execution_state,
            runtime_guide=runtime_guide,
            write=write,
            subject=subject,
            status_inputs=status_inputs,
        )


def contract_gate_precheck(
    definition: Mapping[str, Any],
    *,
    action: str,
    actor_role: str = "",
    execution_state: Mapping[str, Any] | None = None,
    runtime_guide: Mapping[str, Any] | None = None,
    write: Mapping[str, Any] | None = None,
    subject: Mapping[str, Any] | None = None,
    status_inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return evaluate_contract_gate(
        definition,
        action=action,
        actor_role=actor_role,
        execution_state=execution_state,
        runtime_guide=runtime_guide,
        write=write,
        subject=subject,
        status_inputs=status_inputs,
    ).to_dict()
