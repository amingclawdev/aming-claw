"""Source-controlled contract definition registry and lifecycle helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import json
from pathlib import Path
from typing import Any

from .hash import canonical_json
from .schema import (
    CONTRACT_DEFINITION_SCHEMA_VERSION,
    ContractDefinitionError,
    ContractLifecycleError,
    is_contract_definition_payload,
    normalize_definition,
)


DEFAULT_DEFINITION_DIR = Path(__file__).resolve().parent.parent / "contract_definitions"


class UnknownContractDefinitionError(ContractDefinitionError):
    """Raised when a contract definition cannot be found."""


class ContractDefinitionRegistry:
    """Registry for source-controlled contract definition config files."""

    def __init__(self, root: str | Path = DEFAULT_DEFINITION_DIR):
        self.root = Path(root)

    def definition_paths(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(
            path
            for path in self.root.glob("*.json")
            if not path.name.endswith(".schema.json")
        )

    def list_definitions(self, *, include_deprecated: bool = True) -> list[dict[str, Any]]:
        definitions: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str, str]] = set()
        for path in self.definition_paths():
            payload = _load_json(path)
            if not is_contract_definition_payload(payload):
                continue
            definition = normalize_definition(payload, source_path=str(path))
            if not include_deprecated and definition["status"] == "deprecated":
                continue
            key = _definition_key(definition)
            if key in seen_keys:
                raise ContractDefinitionError(f"duplicate contract definition {key!r}")
            seen_keys.add(key)
            definitions.append(definition)
        return sorted(definitions, key=_definition_key)

    def get(
        self,
        contract_id: str,
        *,
        version: str | None = None,
        revision: str | None = None,
        include_deprecated: bool = True,
    ) -> dict[str, Any]:
        matches = [
            definition
            for definition in self.list_definitions(include_deprecated=include_deprecated)
            if _matches(definition, contract_id=contract_id, version=version, revision=revision)
        ]
        if not matches:
            raise UnknownContractDefinitionError(f"unknown contract definition: {contract_id}")
        if len(matches) > 1 and revision is None:
            active = [definition for definition in matches if definition["status"] == "active"]
            if len(active) == 1:
                return active[0]
            raise ContractDefinitionError(
                "ambiguous contract definition: "
                + ", ".join(
                    f"{item['contract_id']}@{item['version']}#{item['revision']}"
                    for item in matches
                )
            )
        return matches[0]

    def validate_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return normalize_definition(payload)

    def create_definition(
        self,
        payload: Mapping[str, Any],
        *,
        file_name: str | None = None,
    ) -> Path:
        definition = normalize_definition(payload)
        key = _definition_key(definition)
        existing = [
            item for item in self.list_definitions()
            if _definition_key(item) == key
        ]
        if existing:
            raise ContractLifecycleError(f"contract definition already exists: {key!r}")
        path = self._target_path(definition, file_name=file_name)
        if path.exists():
            raise ContractLifecycleError(f"contract definition file already exists: {path.name}")
        _write_json(path, _without_private_fields(definition))
        return path

    def update_definition(
        self,
        payload: Mapping[str, Any],
        *,
        file_name: str | None = None,
        expected_previous_hash: str | None = None,
    ) -> Path:
        definition = normalize_definition(payload)
        same_revision = [
            item for item in self.list_definitions()
            if _definition_key(item) == _definition_key(definition)
        ]
        if same_revision:
            existing = same_revision[0]
            if expected_previous_hash and existing["definition_hash"] != expected_previous_hash:
                raise ContractLifecycleError("expected_previous_hash mismatch")
            if (
                existing["status"] == "active"
                and existing["definition_hash"] != definition["definition_hash"]
            ):
                raise ContractLifecycleError(
                    "active contract semantic changes require a new revision or version"
                )

        path = self._target_path(definition, file_name=file_name)
        _write_json(path, _without_private_fields(definition))
        return path

    def deprecate_definition(
        self,
        contract_id: str,
        *,
        version: str | None = None,
        revision: str | None = None,
        reason: str = "",
        replacement_contract_id: str = "",
    ) -> Path:
        definition = self.get(contract_id, version=version, revision=revision)
        payload = _without_private_fields(definition)
        payload["status"] = "deprecated"
        if reason:
            payload["deprecation_reason"] = reason
        if replacement_contract_id:
            payload["deprecated_by"] = replacement_contract_id
        path = Path(str(definition.get("_source_path") or ""))
        if not path:
            path = self._target_path(definition)
        _write_json(path, payload)
        return path

    def hard_delete_definition(
        self,
        contract_id: str,
        *,
        version: str | None = None,
        revision: str | None = None,
        references: Sequence[str] | None = None,
    ) -> Path:
        definition = self.get(contract_id, version=version, revision=revision)
        refs = [item for item in (references or []) if item]
        if definition["status"] != "draft" and refs:
            raise ContractLifecycleError(
                "hard delete is limited to draft or unreferenced contracts"
            )
        path = Path(str(definition.get("_source_path") or ""))
        if not path.exists():
            raise ContractLifecycleError("contract definition source path is missing")
        path.unlink()
        return path

    def _target_path(self, definition: Mapping[str, Any], *, file_name: str | None = None) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        name = file_name or _file_name(definition)
        if "/" in name or "\\" in name or name.startswith("."):
            raise ContractLifecycleError("contract definition file_name must be local")
        return self.root / name


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractDefinitionError(f"{path.name}: invalid json: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ContractDefinitionError(f"{path.name}: root must be an object")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def _definition_key(definition: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(definition.get("contract_id") or ""),
        str(definition.get("version") or ""),
        str(definition.get("revision") or ""),
    )


def _matches(
    definition: Mapping[str, Any],
    *,
    contract_id: str,
    version: str | None,
    revision: str | None,
) -> bool:
    ids = {str(definition.get("contract_id") or "")}
    ids.update(str(item) for item in definition.get("compat_aliases") or [])
    if contract_id not in ids:
        return False
    if version is not None and definition.get("version") != version:
        return False
    if revision is not None and definition.get("revision") != revision:
        return False
    return True


def _without_private_fields(definition: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in definition.items()
        if not str(key).startswith("_")
        and key not in {"definition_hash", "read_model"}
    }


def _file_name(definition: Mapping[str, Any]) -> str:
    parts = [
        _safe_name(str(definition.get("contract_id") or "contract")),
        _safe_name(str(definition.get("version") or "v1")),
        _safe_name(str(definition.get("revision") or "rev1")),
    ]
    return ".".join(parts) + ".json"


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value)
