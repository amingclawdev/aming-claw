"""Small CRUD facade for source-controlled contract definitions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .registry import ContractDefinitionRegistry
from .schema import ContractDefinitionError


@dataclass(frozen=True)
class ContractCrudResult:
    """Serializable result envelope for contract CRUD operations."""

    ok: bool
    operation: str
    status: str
    data: dict[str, Any] = field(default_factory=dict)
    error: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "operation": self.operation,
            "status": self.status,
            "data": self.data,
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload


class ContractCrudService:
    """Pure-Python CRUD service over :class:`ContractDefinitionRegistry`."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        registry: ContractDefinitionRegistry | None = None,
    ) -> None:
        if root is not None and registry is not None:
            raise ValueError("provide either root or registry, not both")
        if registry is not None:
            self.registry = registry
        elif root is None:
            self.registry = ContractDefinitionRegistry()
        else:
            self.registry = ContractDefinitionRegistry(root)

    def list_definitions(self, *, include_deprecated: bool = True) -> dict[str, Any]:
        operation = "list"
        try:
            definitions = self.registry.list_definitions(
                include_deprecated=include_deprecated
            )
        except (ContractDefinitionError, OSError) as exc:
            return _failure(operation, exc)
        return _success(
            operation,
            "listed",
            {"definitions": definitions, "count": len(definitions)},
        )

    def list(self, *, include_deprecated: bool = True) -> dict[str, Any]:
        return self.list_definitions(include_deprecated=include_deprecated)

    def read(
        self,
        contract_id: str,
        *,
        version: str | None = None,
        revision: str | None = None,
        include_deprecated: bool = True,
    ) -> dict[str, Any]:
        operation = "read"
        try:
            definition = self.registry.get(
                contract_id,
                version=version,
                revision=revision,
                include_deprecated=include_deprecated,
            )
        except (ContractDefinitionError, OSError) as exc:
            return _failure(operation, exc)
        return _success(operation, "found", {"definition": definition})

    def validate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        operation = "validate"
        try:
            definition = self.registry.validate_payload(payload)
        except (ContractDefinitionError, OSError) as exc:
            return _failure(operation, exc)
        return _success(operation, "valid", {"definition": definition})

    def create(
        self,
        payload: Mapping[str, Any],
        *,
        file_name: str | None = None,
    ) -> dict[str, Any]:
        operation = "create"
        try:
            definition = self.registry.validate_payload(payload)
            path = self.registry.create_definition(payload, file_name=file_name)
            saved = self.registry.get(
                definition["contract_id"],
                version=definition["version"],
                revision=definition["revision"],
            )
        except (ContractDefinitionError, OSError) as exc:
            return _failure(operation, exc)
        return _success(operation, "created", {"path": str(path), "definition": saved})

    def update(
        self,
        payload: Mapping[str, Any],
        *,
        file_name: str | None = None,
        expected_previous_hash: str | None = None,
    ) -> dict[str, Any]:
        operation = "update"
        try:
            definition = self.registry.validate_payload(payload)
            path = self.registry.update_definition(
                payload,
                file_name=file_name,
                expected_previous_hash=expected_previous_hash,
            )
            saved = self.registry.get(
                definition["contract_id"],
                version=definition["version"],
                revision=definition["revision"],
            )
        except (ContractDefinitionError, OSError) as exc:
            return _failure(operation, exc)
        return _success(operation, "updated", {"path": str(path), "definition": saved})

    def deprecate(
        self,
        contract_id: str,
        *,
        version: str | None = None,
        revision: str | None = None,
        reason: str = "",
        replacement_contract_id: str = "",
    ) -> dict[str, Any]:
        operation = "deprecate"
        try:
            path = self.registry.deprecate_definition(
                contract_id,
                version=version,
                revision=revision,
                reason=reason,
                replacement_contract_id=replacement_contract_id,
            )
            definition = self.registry.get(
                contract_id,
                version=version,
                revision=revision,
                include_deprecated=True,
            )
        except (ContractDefinitionError, OSError) as exc:
            return _failure(operation, exc)
        return _success(operation, "deprecated", {"path": str(path), "definition": definition})

    def hard_delete(
        self,
        contract_id: str,
        *,
        version: str | None = None,
        revision: str | None = None,
        references: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        return self._delete(
            "hard_delete",
            contract_id,
            version=version,
            revision=revision,
            references=references,
        )

    def delete(
        self,
        contract_id: str,
        *,
        version: str | None = None,
        revision: str | None = None,
        references: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        return self._delete(
            "delete",
            contract_id,
            version=version,
            revision=revision,
            references=references,
        )

    def _delete(
        self,
        operation: str,
        contract_id: str,
        *,
        version: str | None = None,
        revision: str | None = None,
        references: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        try:
            definition = self.registry.get(
                contract_id,
                version=version,
                revision=revision,
                include_deprecated=True,
            )
            path = self.registry.hard_delete_definition(
                contract_id,
                version=version,
                revision=revision,
                references=references,
            )
        except (ContractDefinitionError, OSError) as exc:
            return _failure(operation, exc)
        return _success(
            operation,
            "deleted",
            {"path": str(path), "deleted": _identity(definition)},
        )


def _success(operation: str, status: str, data: dict[str, Any]) -> dict[str, Any]:
    return ContractCrudResult(
        ok=True,
        operation=operation,
        status=status,
        data=data,
    ).to_dict()


def _failure(operation: str, exc: BaseException) -> dict[str, Any]:
    return ContractCrudResult(
        ok=False,
        operation=operation,
        status="failed",
        error={"type": exc.__class__.__name__, "message": str(exc)},
    ).to_dict()


def _identity(definition: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "contract_id": str(definition.get("contract_id") or ""),
        "version": str(definition.get("version") or ""),
        "revision": str(definition.get("revision") or ""),
        "status": str(definition.get("status") or ""),
    }
