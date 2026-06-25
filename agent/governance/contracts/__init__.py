"""Config-backed governance contract primitives.

This package is intentionally standalone: it validates source-controlled
contract definitions and produces runtime-guide/write-gate facts without
depending on the legacy route-context runtime.
"""

from .execution_state import build_execution_state
from .crud import ContractCrudResult, ContractCrudService
from .gate_decision import ContractGateDecision
from .gate_kernel import ContractGateKernel, contract_gate_precheck
from .gate_policy import normalize_gate_policy
from .guide_compiler import compile_runtime_guide
from .instructions import resolve_instruction_bundle
from .registry import ContractDefinitionRegistry
from .runtime_precheck import run_contract_runtime_precheck
from .runtime import ContractRuntime, InMemoryContractExecutionStore
from .schema import (
    CONTRACT_DEFINITION_SCHEMA_VERSION,
    ContractDefinitionError,
    ContractLifecycleError,
    find_line,
    is_contract_definition_payload,
    is_new_execution_allowed,
    normalize_definition,
)
from .write_gate import WriteGateDecision, validate_contract_write

__all__ = [
    "CONTRACT_DEFINITION_SCHEMA_VERSION",
    "ContractCrudResult",
    "ContractCrudService",
    "ContractDefinitionError",
    "ContractDefinitionRegistry",
    "ContractGateDecision",
    "ContractGateKernel",
    "ContractLifecycleError",
    "ContractRuntime",
    "InMemoryContractExecutionStore",
    "WriteGateDecision",
    "build_execution_state",
    "compile_runtime_guide",
    "contract_gate_precheck",
    "find_line",
    "is_contract_definition_payload",
    "is_new_execution_allowed",
    "normalize_gate_policy",
    "normalize_definition",
    "resolve_instruction_bundle",
    "run_contract_runtime_precheck",
    "validate_contract_write",
]
