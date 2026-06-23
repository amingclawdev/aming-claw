"""Pure execution-state projection for contract definitions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .hash import stable_sha256
from .schema import iter_stage_lines


def build_execution_state(
    definition: Mapping[str, Any],
    *,
    project_id: str,
    backlog_id: str,
    contract_execution_id: str,
    actor_role: str,
    completed_lines: Sequence[Mapping[str, str]] | None = None,
    route_token_ref: str = "",
    instruction_bundle_hash: str = "",
    execution_state_revision: int = 1,
) -> dict[str, Any]:
    """Build a deterministic read model for one contract execution."""

    completed = {
        (str(item.get("stage_id") or ""), str(item.get("line_id") or ""))
        for item in (completed_lines or [])
    }
    next_action = None
    for stage, line in iter_stage_lines(definition):
        key = (str(stage.get("stage_id") or ""), str(line.get("line_id") or ""))
        if key in completed:
            continue
        next_action = {
            "stage_id": key[0],
            "line_id": key[1],
            "owner_role": line.get("owner_role", ""),
            "allowed_writer_roles": list(line.get("allowed_writer_roles") or []),
            "evidence_kind": line.get("evidence_kind", ""),
            "required": bool(line.get("required", True)),
        }
        break

    state = {
        "schema_version": "contract_execution_state.v1",
        "project_id": project_id,
        "backlog_id": backlog_id,
        "contract_execution_id": contract_execution_id,
        "contract_id": definition.get("contract_id", ""),
        "version": definition.get("version", ""),
        "revision": definition.get("revision", ""),
        "role": definition.get("role", ""),
        "status": definition.get("status", ""),
        "definition_hash": definition.get("definition_hash", ""),
        "instruction_bundle_hash": instruction_bundle_hash,
        "execution_state_revision": execution_state_revision,
        "actor_role": actor_role,
        "route_token_ref": route_token_ref,
        "completed_lines": [
            {"stage_id": stage_id, "line_id": line_id}
            for stage_id, line_id in sorted(completed)
        ],
        "next_action": next_action,
    }
    state["execution_state_hash"] = stable_sha256(
        {key: value for key, value in state.items() if key != "execution_state_hash"}
    )
    return state
