"""Source-controlled contract definition registry and lifecycle helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from threading import RLock
from typing import Any

from ..governance_hints import governance_hints_envelope_sha256
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
_UNSET = object()


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
        self._loaded_at_override = loaded_at is not None
        self._snapshot_lock = RLock()
        self._snapshot_key: tuple[Any, ...] | None = None
        self._snapshot_definitions: list[dict[str, Any]] = []

    def definition_paths(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(
            path
            for path in self.root.glob("*.json")
            if not path.name.endswith(".schema.json")
        )

    def list_definitions(self, *, include_deprecated: bool = True) -> list[dict[str, Any]]:
        with self._snapshot_lock:
            sources = _read_definition_sources(self.definition_paths())
            git_root, head_revision = _git_registry_snapshot(self.root)
            snapshot_key = (
                tuple((str(path), source_sha256) for path, _, source_sha256 in sources),
                str(git_root or ""),
                head_revision,
            )
            if snapshot_key != self._snapshot_key:
                if not self._loaded_at_override:
                    self.loaded_at = _utc_now()
                head_blobs = _git_head_blobs(
                    git_root,
                    [path for path, _, _ in sources],
                )
                definitions: list[dict[str, Any]] = []
                seen_keys: set[tuple[str, str, str]] = set()
                for path, source_bytes, source_sha256 in sources:
                    payload = _load_json_bytes(path, source_bytes)
                    if not is_contract_definition_payload(payload):
                        continue
                    definition = normalize_definition(payload, source_path=str(path))
                    head_blob = _head_blob_for_path(
                        path,
                        git_root=git_root,
                        head_blobs=head_blobs,
                    )
                    _attach_source_load_record(
                        definition,
                        path,
                        loaded_at=self.loaded_at,
                        source_sha256=source_sha256,
                        git_root=git_root,
                        head_blob=head_blob,
                    )
                    key = _definition_key(definition)
                    if key in seen_keys:
                        raise ContractDefinitionError(
                            f"duplicate contract definition {key!r}"
                        )
                    seen_keys.add(key)
                    definitions.append(definition)
                self._snapshot_definitions = sorted(
                    definitions,
                    key=_definition_key,
                )
                if head_blobs is not None:
                    self._snapshot_key = snapshot_key
                else:
                    self._snapshot_key = None

            definitions = deepcopy(self._snapshot_definitions)
        if not include_deprecated:
            definitions = [
                definition
                for definition in definitions
                if definition["status"] != "deprecated"
            ]
        return definitions

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
        if _has_protocol_control_characters(name):
            raise ContractLifecycleError(
                "contract definition file_name must not contain control characters"
            )
        return self.root / name


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractDefinitionError(f"{path.name}: invalid json: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ContractDefinitionError(f"{path.name}: root must be an object")
    return payload


def _load_json_bytes(path: Path, source_bytes: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(source_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ContractDefinitionError(f"{path.name}: invalid utf-8") from exc
    except json.JSONDecodeError as exc:
        raise ContractDefinitionError(f"{path.name}: invalid json: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ContractDefinitionError(f"{path.name}: root must be an object")
    return payload


def _read_definition_sources(
    paths: Sequence[Path],
) -> list[tuple[Path, bytes, str]]:
    sources: list[tuple[Path, bytes, str]] = []
    for path in paths:
        source_bytes = path.read_bytes()
        sources.append(
            (
                path,
                source_bytes,
                "sha256:" + hashlib.sha256(source_bytes).hexdigest(),
            )
        )
    return sources


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
            "governance_hints_sha256",
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
    source_sha256: str | None = None,
    git_root: Path | None = None,
    head_blob: bytes | None | object = _UNSET,
) -> None:
    source_sha256 = source_sha256 or file_sha256(path)
    governance_hints_sha256 = governance_hints_envelope_sha256(
        definition.get("governance_hints")
    )
    source_path = str(path)
    source_control_integrity = _source_control_integrity(
        path,
        source_sha256=source_sha256,
        git_root=git_root,
        head_blob=head_blob,
    )
    load_record_identity = {
        "schema_version": "contract_definition_load_record_identity.v1",
        "source_path": source_path,
        "contract_id": definition.get("contract_id", ""),
        "version": definition.get("version", ""),
        "revision": definition.get("revision", ""),
        "source_sha256": source_sha256,
        "governance_hints_sha256": governance_hints_sha256,
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
        "governance_hints_sha256": governance_hints_sha256,
        "definition_hash": definition.get("definition_hash", ""),
        "loaded_at": loaded_at,
        "runtime_version": CONTRACT_REGISTRY_RUNTIME_VERSION,
        "drift_status": source_control_integrity["drift_status"],
        "source_control_integrity": dict(source_control_integrity),
        "next_operator_action": source_control_integrity["next_operator_action"],
    }
    definition["source_sha256"] = source_sha256
    definition["governance_hints_sha256"] = governance_hints_sha256
    definition["source_control_integrity"] = dict(source_control_integrity)
    definition["definition_load_record"] = load_record
    read_model = definition.get("read_model")
    if isinstance(read_model, dict):
        read_model["source_sha256"] = source_sha256
        read_model["source_control_integrity"] = dict(source_control_integrity)
        read_model["definition_load_record"] = dict(load_record)


def _source_control_integrity(
    path: Path,
    *,
    source_sha256: str,
    git_root: Path | None = None,
    head_blob: bytes | None | object = _UNSET,
) -> dict[str, Any]:
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
    if head_blob is _UNSET and git_root is None:
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
    if head_blob is _UNSET:
        head_blob = _git_head_blob(git_root, relative_path)
    if head_blob is None:
        integrity.update(_warning_integrity_fields("missing_from_head"))
        return integrity

    head_sha256 = "sha256:" + hashlib.sha256(head_blob).hexdigest()
    integrity["git_head_source_sha256"] = head_sha256
    if head_sha256 != source_sha256:
        try:
            current_blob = path.read_bytes()
        except OSError:
            current_blob = b""
        if _governance_hints_only_source_change(head_blob, current_blob):
            integrity.update(_governance_hints_integrity_fields())
        else:
            integrity.update(_warning_integrity_fields("changed_since_head"))
    return integrity


def _governance_hints_only_source_change(previous: bytes, current: bytes) -> bool:
    try:
        previous_payload = json.loads(previous.decode("utf-8"))
        current_payload = json.loads(current.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return False
    if not isinstance(previous_payload, dict) or not isinstance(current_payload, dict):
        return False
    previous_envelope = previous_payload.pop("governance_hints", None)
    current_envelope = current_payload.pop("governance_hints", None)
    return previous_payload == current_payload and previous_envelope != current_envelope


def _governance_hints_integrity_fields() -> dict[str, Any]:
    return {
        "status": "governance_hints_changed",
        "drift_status": "graph_source_metadata_changed",
        "severity": "none",
        "changed_since_head": True,
        "requires_contract_update": False,
        "legal_status": "legal",
        "gate_enforcement": "graph_projection_only",
        "blocks_runtime": False,
        "next_operator_action": "run_graph_reconcile",
    }


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


def _git_registry_snapshot(root: Path) -> tuple[Path | None, str]:
    """Return one source-control identity for the whole registry snapshot."""

    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                "--show-toplevel",
                "HEAD",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    if result.returncode != 0:
        return None, ""
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return None, ""
    return Path(lines[0]).resolve(), lines[1]


def _git_head_blobs(
    git_root: Path | None,
    paths: Sequence[Path],
) -> dict[str, bytes | None] | None:
    """Load all registry files from HEAD with one git subprocess."""

    if git_root is None:
        return {}
    relative_paths: list[str] = []
    for path in paths:
        try:
            relative_paths.append(path.resolve().relative_to(git_root).as_posix())
        except ValueError:
            continue
    if not relative_paths:
        return {}
    if any(_has_protocol_control_characters(path) for path in relative_paths):
        return None
    specs = [f"HEAD:{relative_path}" for relative_path in relative_paths]
    try:
        result = subprocess.run(
            ["git", "-C", str(git_root), "cat-file", "--batch"],
            input=("\n".join(specs) + "\n").encode("utf-8"),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None

    blobs: dict[str, bytes | None] = {}
    output = bytes(result.stdout)
    offset = 0
    for relative_path in relative_paths:
        newline = output.find(b"\n", offset)
        if newline < 0:
            return None
        header = output[offset:newline]
        offset = newline + 1
        if header.endswith(b" missing"):
            blobs[relative_path] = None
            continue
        parts = header.rsplit(b" ", 2)
        try:
            size = int(parts[-1])
        except (ValueError, IndexError):
            return None
        blob_end = offset + size
        if (
            size < 0
            or blob_end >= len(output)
            or output[blob_end:blob_end + 1] != b"\n"
        ):
            return None
        blobs[relative_path] = output[offset:blob_end]
        offset = blob_end + 1
    if offset != len(output):
        return None
    return blobs


def _head_blob_for_path(
    path: Path,
    *,
    git_root: Path | None,
    head_blobs: Mapping[str, bytes | None] | None,
) -> bytes | None | object:
    if git_root is None:
        return None
    if head_blobs is None:
        return _UNSET
    try:
        relative_path = path.resolve().relative_to(git_root).as_posix()
    except ValueError:
        return None
    return head_blobs.get(relative_path)


def _has_protocol_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


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
