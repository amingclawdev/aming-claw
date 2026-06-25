"""Runtime-facing precheck API for source-backed ContractRuntime flows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .gate_kernel import ContractGateKernel
from .registry import ContractDefinitionRegistry


def run_contract_runtime_precheck(
    *,
    registry: ContractDefinitionRegistry,
    definition: Mapping[str, Any],
    action: str,
    actor_role: str = "",
    execution_state: Mapping[str, Any] | None = None,
    runtime_guide: Mapping[str, Any] | None = None,
    write: Mapping[str, Any] | None = None,
    subject: Mapping[str, Any] | None = None,
    status_inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the same decision shape used by guide and mutation paths."""

    return ContractGateKernel(registry).precheck(
        definition,
        action=action,
        actor_role=actor_role,
        execution_state=execution_state,
        runtime_guide=runtime_guide,
        write=write,
        subject=subject,
        status_inputs=status_inputs,
    ).to_dict()
