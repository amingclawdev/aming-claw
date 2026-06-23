"""Instruction bundle resolution for source-controlled contract definitions."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .hash import file_sha256, stable_sha256


class InstructionResolutionError(ValueError):
    """Raised when a contract-owned instruction reference cannot be resolved."""


def resolve_instruction_bundle(
    definition: Mapping[str, Any],
    *,
    root: str | Path,
    include_content: bool = True,
) -> dict[str, Any]:
    """Resolve and hash the contract-owned instruction layer."""

    root_path = Path(root).resolve()
    instruction_layer = definition.get("instruction_layer") or {}
    inline = list(instruction_layer.get("inline") or [])
    resolved_refs: list[dict[str, Any]] = []
    hash_refs: list[dict[str, Any]] = []
    for ref in instruction_layer.get("refs") or []:
        if not isinstance(ref, Mapping):
            continue
        ref_id = str(ref.get("id") or "").strip()
        rel_path = str(ref.get("path") or "").strip()
        if not ref_id or not rel_path:
            raise InstructionResolutionError("instruction refs require id and path")
        path = _contained_path(root_path, rel_path)
        if not path.exists() or not path.is_file():
            raise InstructionResolutionError(f"instruction ref {ref_id!r} file is missing")
        actual_hash = file_sha256(path)
        expected_hash = str(ref.get("sha256") or "").strip()
        if expected_hash and expected_hash != actual_hash:
            raise InstructionResolutionError(
                f"instruction ref {ref_id!r} hash mismatch: expected {expected_hash}, got {actual_hash}"
            )
        entry = {
            "id": ref_id,
            "path": rel_path,
            "sha256": actual_hash,
            "visible_to_roles": list(ref.get("visible_to_roles") or []),
            "stage_ids": list(ref.get("stage_ids") or []),
        }
        if include_content:
            entry["content"] = path.read_text(encoding="utf-8")
        resolved_refs.append(entry)
        hash_refs.append({key: value for key, value in entry.items() if key != "content"})
    bundle_input = {
        "schema_version": "contract_instruction_bundle.v1",
        "contract_id": definition.get("contract_id"),
        "version": definition.get("version"),
        "revision": definition.get("revision"),
        "definition_hash": definition.get("definition_hash", ""),
        "inline": inline,
        "refs": hash_refs,
    }
    return {
        "schema_version": "contract_instruction_bundle.v1",
        "contract_id": definition.get("contract_id"),
        "version": definition.get("version"),
        "revision": definition.get("revision"),
        "definition_hash": definition.get("definition_hash", ""),
        "inline": inline,
        "refs": resolved_refs,
        "instruction_bundle_hash": stable_sha256(bundle_input),
    }


def _contained_path(root: Path, rel_path: str) -> Path:
    path = (root / rel_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise InstructionResolutionError(
            "instruction ref paths must resolve inside the configured root"
        ) from exc
    return path
