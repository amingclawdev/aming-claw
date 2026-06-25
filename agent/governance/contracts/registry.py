"""Source-controlled contract definition registry and lifecycle helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from .hash import canonical_json, file_sha256, stable_sha256
from .schema import (
    CONTRACT_DEFINITION_SCHEMA_VERSION,
    ContractDefinitionError,
    ContractLifecycleError,
    is_contract_definition_payload,
    normalize_definition,
)


DEFAULT_DEFINITION_DIR = Path(__file__).resolve().parent.parent / "contract_definitions"
CONTRACT_REGISTRY_RUNTIME_VERSION = "contract_registry.v1"


class UnknownContractDefinitionError(ContractDefinitionError):
    """Raised when a contract definition cannot be found."""


class ContractDefinitionRegistry:
    """Registry for source-controlled contract definition config files."""

    def __init__(
        self,
        root: str | Path = DEFAULT_DEFINITION_DIR,
        *,
        loaded_at: str | None = None,
    ):
        self.root = Path(root)
        self.loaded_at = loaded_at or _utc_now()

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
            _attach_source_load_record(definition, path, loaded_at=self.loaded_at)
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
            active = [
                definition
                for definition in matches
                if definition["status"] == "active"
            ]
            candidates = active or matches
            if _same_contract_version(candidates):
                return max(candidates, key=_revision_sort_key)
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


def _same_contract_version(definitions: Sequence[Mapping[str, Any]]) -> bool:
    keys = {
        (
            str(definition.get("contract_id") or ""),
            str(definition.get("version") or ""),
        )
        for definition in definitions
    }
    return len(keys) == 1


def _revision_sort_key(definition: Mapping[str, Any]) -> tuple[int, int, str]:
    revision = str(definition.get("revision") or "")
    match = re.fullmatch(r"rev(\d+)", revision)
    if match:
        return (1, int(match.group(1)), revision)
    return (0, 0, revision)


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
        and key not in {
            "definition_hash",
            "definition_load_record",
            "read_model",
            "source_control_integrity",
            "source_sha256",
        }
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


def _attach_source_load_record(
    definition: dict[str, Any],
    path: Path,
    *,
    loaded_at: str,
) -> None:
    source_sha256 = file_sha256(path)
    source_path = str(path)
    source_control_integrity = _source_control_integrity(
        path,
        source_sha256=source_sha256,
    )
    load_record_identity = {
        "schema_version": "contract_definition_load_record_identity.v1",
        "source_path": source_path,
        "contract_id": definition.get("contract_id", ""),
        "version": definition.get("version", ""),
        "revision": definition.get("revision", ""),
        "source_sha256": source_sha256,
        "definition_hash": definition.get("definition_hash", ""),
    }
    load_record_id = "cdlr-" + stable_sha256(load_record_identity).split(":", 1)[1][:24]
    load_record = {
        "schema_version": "contract_definition_load_record.v1",
        "load_record_id": load_record_id,
        "source_path": source_path,
        "contract_id": definition.get("contract_id", ""),
        "version": definition.get("version", ""),
        "revision": definition.get("revision", ""),
        "status": "loaded",
        "contract_status": definition.get("status", ""),
        "source_sha256": source_sha256,
        "definition_hash": definition.get("definition_hash", ""),
        "loaded_at": loaded_at,
        "runtime_version": CONTRACT_REGISTRY_RUNTIME_VERSION,
        "drift_status": source_control_integrity["drift_status"],
        "source_control_integrity": dict(source_control_integrity),
        "next_operator_action": source_control_integrity["next_operator_action"],
    }
    definition["source_sha256"] = source_sha256
    definition["source_control_integrity"] = dict(source_control_integrity)
    definition["definition_load_record"] = load_record
    read_model = definition.get("read_model")
    if isinstance(read_model, dict):
        read_model["source_sha256"] = source_sha256
        read_model["source_control_integrity"] = dict(source_control_integrity)
        read_model["definition_load_record"] = dict(load_record)


def _source_control_integrity(path: Path, *, source_sha256: str) -> dict[str, Any]:
    integrity: dict[str, Any] = {
        "schema_version": "contract_source_control_integrity.v1",
        "source_path": str(path),
        "source_sha256": source_sha256,
        "baseline_source": "git_head",
        "status": "current",
        "drift_status": "current",
        "severity": "none",
        "changed_since_head": False,
        "requires_contract_update": False,
        "legal_status": "legal",
        "gate_enforcement": "warn_only_until_contract_update_runtime_exists",
        "blocks_runtime": False,
        "next_operator_action": "none",
    }
    git_root = _git_root_for(path)
    if git_root is None:
        integrity.update(
            {
                "baseline_source": "unavailable",
                "status": "source_control_unavailable",
            }
        )
        return integrity

    try:
        relative_path = path.resolve().relative_to(git_root).as_posix()
    except ValueError:
        integrity.update(
            {
                "baseline_source": "unavailable",
                "status": "outside_git_worktree",
            }
        )
        return integrity

    integrity["git_root"] = str(git_root)
    integrity["git_relative_path"] = relative_path
    head_blob = _git_head_blob(git_root, relative_path)
    if head_blob is None:
        integrity.update(_warning_integrity_fields("missing_from_head"))
        return integrity

    head_sha256 = "sha256:" + hashlib.sha256(head_blob).hexdigest()
    integrity["git_head_source_sha256"] = head_sha256
    if head_sha256 != source_sha256:
        integrity.update(_warning_integrity_fields("changed_since_head"))
    return integrity


def _warning_integrity_fields(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "drift_status": "source_control_drift",
        "severity": "warning",
        "changed_since_head": True,
        "requires_contract_update": True,
        "legal_status": "illegal_without_contract_update",
        "blocks_runtime": False,
        "next_operator_action": "run_contract_update_or_revert_direct_source_edit",
        "warning": (
            "source-controlled contract definition differs from its git HEAD "
            "baseline; this is warning-only until contract_update can author "
            "the update path"
        ),
    }


def _git_root_for(path: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return Path(root).resolve() if root else None


def _git_head_blob(git_root: Path, relative_path: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(git_root), "show", f"HEAD:{relative_path}"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return bytes(result.stdout)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
