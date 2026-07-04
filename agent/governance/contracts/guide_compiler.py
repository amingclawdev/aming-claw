"""Compile agent-facing runtime guides from contract definition and state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .hash import stable_sha256


def compile_runtime_guide(
    definition: Mapping[str, Any],
    execution_state: Mapping[str, Any],
    *,
    instruction_bundle: Mapping[str, Any] | None = None,
    judgment_hints: list[Any] | None = None,
) -> dict[str, Any]:
    """Return the only agent-facing next-action guide for this execution."""

    instruction_bundle = instruction_bundle or {}
    sealed_judgment_hints = list(judgment_hints or [])
    next_action = execution_state.get("next_action")
    role = str(execution_state.get("actor_role") or "")
    stage_id = ""
    if isinstance(next_action, Mapping):
        stage_id = str(next_action.get("stage_id") or "")
    visible_refs = [
        ref
        for ref in instruction_bundle.get("refs", [])
        if _ref_visible(ref, role=role, stage_id=stage_id)
    ]
    guide = {
        "schema_version": "contract_runtime_guide.v1",
        "contract": {
            "contract_id": definition.get("contract_id", ""),
            "version": definition.get("version", ""),
            "revision": definition.get("revision", ""),
            "role": definition.get("role", ""),
            "contract_type": definition.get("contract_type", ""),
            "definition_hash": definition.get("definition_hash", ""),
        },
        "execution": {
            "project_id": execution_state.get("project_id", ""),
            "backlog_id": execution_state.get("backlog_id", ""),
            "contract_execution_id": execution_state.get("contract_execution_id", ""),
            "execution_state_revision": execution_state.get("execution_state_revision", 0),
            "execution_state_hash": execution_state.get("execution_state_hash", ""),
            "route_token_ref": execution_state.get("route_token_ref", ""),
            "instruction_bundle_hash": instruction_bundle.get("instruction_bundle_hash", ""),
        },
        "next_legal_action": next_action,
        "instructions": {
            "inline": list(instruction_bundle.get("inline") or []),
            "refs": visible_refs,
        },
    }
    if sealed_judgment_hints:
        guide["schema_version"] = "contract_runtime_guide.v2"
        guide["judgment_hints"] = sealed_judgment_hints
    guide["runtime_guide_hash"] = stable_sha256(
        {key: value for key, value in guide.items() if key != "runtime_guide_hash"}
    )
    return guide


def attach_writer_role_safe_copy_payload(
    guide: dict[str, Any],
    *,
    reader_role: str,
    writer_role: str,
    writer_runtime_guide_hash: str,
    role_runtime_guide_hashes: list[Mapping[str, Any]] | None = None,
) -> None:
    """Attach a submit_line payload whose guide hash is aligned to the writer role."""

    next_action = guide.get("next_legal_action")
    if not isinstance(next_action, Mapping):
        return
    execution = guide.get("execution") if isinstance(guide.get("execution"), Mapping) else {}
    contract = guide.get("contract") if isinstance(guide.get("contract"), Mapping) else {}
    reader_runtime_guide_hash = str(guide.get("runtime_guide_hash") or "")
    writer_role = str(writer_role or "").strip()
    required_hash = str(writer_runtime_guide_hash or reader_runtime_guide_hash)
    copy_payload: dict[str, Any] = {
        "project_id": str(execution.get("project_id") or ""),
        "backlog_id": str(execution.get("backlog_id") or ""),
        "contract_execution_id": str(execution.get("contract_execution_id") or ""),
        "definition_hash": str(contract.get("definition_hash") or ""),
        "instruction_bundle_hash": str(execution.get("instruction_bundle_hash") or ""),
        "execution_state_revision": int(execution.get("execution_state_revision") or 0),
        "runtime_guide_hash": required_hash,
        "stage_id": str(next_action.get("stage_id") or ""),
        "line_id": str(next_action.get("line_id") or ""),
        "actor_role": writer_role,
        "evidence_kind": str(next_action.get("evidence_kind") or ""),
    }
    for field in (
        "line_instance_id",
        "runtime_context_id",
        "task_id",
        "parent_task_id",
        "worker_role",
        "lane_id",
        "worker_slot_id",
        "worker_id",
    ):
        value = next_action.get(field)
        if value not in (None, ""):
            copy_payload[field] = value

    role_hashes = [
        dict(item)
        for item in (role_runtime_guide_hashes or [])
        if isinstance(item, Mapping)
    ]
    guide["writer_role_safe_copy_payload"] = {
        "schema_version": "contract_runtime.writer_role_safe_copy_payload.v1",
        "tool": "contract_runtime_submit_line",
        "copy_payload": copy_payload,
        "hash_alignment": {
            "schema_version": "contract_runtime.writer_role_hash_alignment.v1",
            "status": "writer_role_aligned",
            "reader_role": str(reader_role or ""),
            "reader_runtime_guide_hash": reader_runtime_guide_hash,
            "required_owner_role": str(next_action.get("owner_role") or ""),
            "required_writer_role": writer_role,
            "required_writer_runtime_guide_hash": required_hash,
            "reader_hash_is_writer_hash": reader_runtime_guide_hash == required_hash,
            "known_role_runtime_guide_hashes": role_hashes,
            "recovery": (
                "Use writer_role_safe_copy_payload.copy_payload.runtime_guide_hash "
                "for contract_runtime_submit_line; the top-level runtime_guide_hash "
                "belongs to the reader role that produced this guide."
            ),
        },
    }


def _ref_visible(ref: Any, *, role: str, stage_id: str) -> bool:
    if not isinstance(ref, Mapping):
        return False
    roles = [str(item) for item in ref.get("visible_to_roles") or []]
    stages = [str(item) for item in ref.get("stage_ids") or []]
    return (not roles or role in roles) and (not stages or stage_id in stages)
