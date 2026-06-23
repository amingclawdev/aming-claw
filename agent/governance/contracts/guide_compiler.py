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
) -> dict[str, Any]:
    """Return the only agent-facing next-action guide for this execution."""

    instruction_bundle = instruction_bundle or {}
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
    guide["runtime_guide_hash"] = stable_sha256(
        {key: value for key, value in guide.items() if key != "runtime_guide_hash"}
    )
    return guide


def _ref_visible(ref: Any, *, role: str, stage_id: str) -> bool:
    if not isinstance(ref, Mapping):
        return False
    roles = [str(item) for item in ref.get("visible_to_roles") or []]
    stages = [str(item) for item in ref.get("stage_ids") or []]
    return (not roles or role in roles) and (not stages or stage_id in stages)
