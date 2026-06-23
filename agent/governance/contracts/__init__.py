"""Config-backed governance contract primitives.

This package is intentionally standalone: it validates source-controlled
contract definitions and produces runtime-guide/write-gate facts without
depending on the legacy route-context runtime.
"""

from .execution_state import build_execution_state
from .guide_compiler import compile_runtime_guide
from .instructions import resolve_instruction_bundle
from .registry import ContractDefinitionRegistry
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
    "ContractDefinitionError",
    "ContractDefinitionRegistry",
    "ContractLifecycleError",
    "ContractRuntime",
    "InMemoryContractExecutionStore",
    "WriteGateDecision",
    "build_execution_state",
    "compile_runtime_guide",
    "find_line",
    "is_contract_definition_payload",
    "is_new_execution_allowed",
    "normalize_definition",
    "resolve_instruction_bundle",
    "validate_contract_write",
]
