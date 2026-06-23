"""Minimal executable runtime path for config-backed contracts.

This module intentionally stays independent from HTTP, MCP, timeline, and DB
facades. It proves the new contract system can drive the next legal action and
line-level write authorization from source-controlled definitions before legacy
route-context migration begins.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from .execution_state import build_execution_state
from .guide_compiler import compile_runtime_guide
from .instructions import resolve_instruction_bundle
from .registry import ContractDefinitionRegistry
from .schema import ContractDefinitionError, is_new_execution_allowed
from .write_gate import WriteGateDecision, validate_contract_write


class ContractRuntimeError(ValueError):
    """Raised when a contract execution cannot be started or advanced."""


class InMemoryContractExecutionStore:
    """Small test/runtime store for contract executions.

    The production integration can replace this with a DB-backed adapter while
    preserving the same record shape and runtime semantics.
    """

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    def create(self, record: Mapping[str, Any]) -> dict[str, Any]:
        contract_execution_id = str(record.get("contract_execution_id") or "")
        if not contract_execution_id:
            raise ContractRuntimeError("contract_execution_id is required")
        if contract_execution_id in self._records:
            raise ContractRuntimeError(
                f"contract execution already exists: {contract_execution_id}"
            )
        stored = deepcopy(dict(record))
        self._records[contract_execution_id] = stored
        return deepcopy(stored)

    def get(self, contract_execution_id: str) -> dict[str, Any]:
        try:
            return deepcopy(self._records[contract_execution_id])
        except KeyError as exc:
            raise ContractRuntimeError(
                f"unknown contract execution: {contract_execution_id}"
            ) from exc

    def update(self, contract_execution_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
        if contract_execution_id not in self._records:
            raise ContractRuntimeError(
                f"unknown contract execution: {contract_execution_id}"
            )
        stored = deepcopy(dict(record))
        self._records[contract_execution_id] = stored
        return deepcopy(stored)


class ContractRuntime:
    """Runtime facade that compiles guides and validates writes from state."""

    def __init__(
        self,
        registry: ContractDefinitionRegistry,
        *,
        instruction_root: str | Path | None = None,
        store: InMemoryContractExecutionStore | None = None,
    ) -> None:
        self.registry = registry
        self.instruction_root = Path(instruction_root) if instruction_root is not None else registry.root
        self.store = store or InMemoryContractExecutionStore()

    def start_execution(
        self,
        contract_id: str,
        *,
        project_id: str,
        backlog_id: str,
        actor_role: str,
        contract_execution_id: str | None = None,
        version: str | None = None,
        revision: str | None = None,
        route_token_ref: str = "",
    ) -> dict[str, Any]:
        definition = self.registry.get(
            contract_id,
            version=version,
            revision=revision,
            include_deprecated=True,
        )
        if not is_new_execution_allowed(definition):
            raise ContractRuntimeError(
                f"contract {definition['contract_id']}@{definition['version']}#{definition['revision']} "
                f"is {definition['status']} and cannot start new executions"
            )
        instruction_bundle = resolve_instruction_bundle(
            definition,
            root=self.instruction_root,
            include_content=True,
        )
        execution_id = contract_execution_id or f"cex-{uuid4().hex}"
        state = build_execution_state(
            definition,
            project_id=project_id,
            backlog_id=backlog_id,
            contract_execution_id=execution_id,
            actor_role=actor_role,
            route_token_ref=route_token_ref,
            instruction_bundle_hash=instruction_bundle["instruction_bundle_hash"],
        )
        guide = compile_runtime_guide(
            definition,
            state,
            instruction_bundle=instruction_bundle,
        )
        record = {
            "schema_version": "contract_runtime_execution_record.v1",
            "project_id": project_id,
            "backlog_id": backlog_id,
            "contract_execution_id": execution_id,
            "contract_id": definition["contract_id"],
            "version": definition["version"],
            "revision": definition["revision"],
            "definition_hash": definition["definition_hash"],
            "instruction_bundle_hash": instruction_bundle["instruction_bundle_hash"],
            "route_token_ref": route_token_ref,
            "completed_lines": [],
            "execution_state_revision": state["execution_state_revision"],
            "execution_state": state,
            "runtime_guide": guide,
        }
        return self.store.create(record)

    def current_guide(
        self,
        contract_execution_id: str,
        *,
        actor_role: str | None = None,
    ) -> dict[str, Any]:
        record = self.store.get(contract_execution_id)
        definition = self._load_pinned_definition(record)
        instruction_bundle = resolve_instruction_bundle(
            definition,
            root=self.instruction_root,
            include_content=True,
        )
        _assert_hash(
            "instruction_bundle_hash",
            record.get("instruction_bundle_hash"),
            instruction_bundle.get("instruction_bundle_hash"),
        )
        state = build_execution_state(
            definition,
            project_id=str(record["project_id"]),
            backlog_id=str(record["backlog_id"]),
            contract_execution_id=contract_execution_id,
            actor_role=actor_role or str(record["execution_state"].get("actor_role") or ""),
            completed_lines=record.get("completed_lines") or [],
            route_token_ref=str(record.get("route_token_ref") or ""),
            instruction_bundle_hash=str(record.get("instruction_bundle_hash") or ""),
            execution_state_revision=int(record.get("execution_state_revision") or 1),
        )
        guide = compile_runtime_guide(
            definition,
            state,
            instruction_bundle=instruction_bundle,
        )
        record["execution_state"] = state
        record["runtime_guide"] = guide
        self.store.update(contract_execution_id, record)
        return guide

    def submit_line_write(
        self,
        contract_execution_id: str,
        write: Mapping[str, Any],
    ) -> dict[str, Any]:
        record = self.store.get(contract_execution_id)
        definition = self._load_pinned_definition(record)
        guide = self.current_guide(
            contract_execution_id,
            actor_role=str(write.get("actor_role") or ""),
        )
        refreshed = self.store.get(contract_execution_id)
        decision = validate_contract_write(
            definition,
            refreshed["execution_state"],
            write,
            runtime_guide=guide,
        )
        if not decision.ok:
            return {
                "schema_version": "contract_runtime_write_result.v1",
                "ok": False,
                "decision": decision.to_dict(),
                "record": refreshed,
            }

        completed_lines = list(refreshed.get("completed_lines") or [])
        completed_lines.append(
            {
                "stage_id": str(write.get("stage_id") or ""),
                "line_id": str(write.get("line_id") or ""),
                "actor_role": str(write.get("actor_role") or ""),
                "evidence_kind": str(write.get("evidence_kind") or ""),
            }
        )
        refreshed["completed_lines"] = completed_lines
        refreshed["execution_state_revision"] = int(refreshed.get("execution_state_revision") or 1) + 1
        self.store.update(contract_execution_id, refreshed)
        next_guide = self.current_guide(
            contract_execution_id,
            actor_role=str(write.get("actor_role") or ""),
        )
        updated = self.store.get(contract_execution_id)
        updated["runtime_guide"] = next_guide
        self.store.update(contract_execution_id, updated)
        return {
            "schema_version": "contract_runtime_write_result.v1",
            "ok": True,
            "decision": WriteGateDecision(ok=True).to_dict(),
            "record": self.store.get(contract_execution_id),
        }

    def _load_pinned_definition(self, record: Mapping[str, Any]) -> dict[str, Any]:
        definition = self.registry.get(
            str(record.get("contract_id") or ""),
            version=str(record.get("version") or ""),
            revision=str(record.get("revision") or ""),
            include_deprecated=True,
        )
        _assert_hash("definition_hash", record.get("definition_hash"), definition.get("definition_hash"))
        return definition


def _assert_hash(field: str, expected: Any, actual: Any) -> None:
    if expected != actual:
        raise ContractDefinitionError(f"{field} mismatch for pinned contract execution")
