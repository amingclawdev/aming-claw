"""Backlog-backed contract gate for parallel agent work."""

from __future__ import annotations

import fnmatch
import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any


CONTRACT_SCHEMA_VERSION = "parallel_agent_contract.v1"
CONTRACT_ROW_TYPE = "contract"
CONTRACT_TYPE = "parallel_agent_contract"
ACCEPTED_CONTRACT_STATUS = "ACCEPTED"

_ACTIVE_BACKLOG_STATUSES = {"OPEN", "IN_PROGRESS", "MF_IN_PROGRESS"}
_PARALLEL_MARKER_KEYS = {
    "parallel_contract_id",
    "parallel_agent_contract_id",
    "parallel_agent_id",
    "parallel_flow",
}


class ParallelAgentContractError(ValueError):
    """Raised when a parallel task is not covered by an accepted contract."""


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _string(value: Any) -> str:
    return str(value or "").strip()


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return []
    result: list[str] = []
    for item in value:
        token = _string(item)
        if token:
            result.append(token)
    return result


def _normalize_path(path: str) -> str:
    token = _string(path).replace("\\", "/")
    while token.startswith("./"):
        token = token[2:]
    return token.strip("/")


def _literal_prefix(pattern: str) -> str:
    pattern = _normalize_path(pattern)
    wildcard_positions = [
        pos for pos in (pattern.find("*"), pattern.find("?"), pattern.find("["))
        if pos >= 0
    ]
    if wildcard_positions:
        pattern = pattern[: min(wildcard_positions)]
    return pattern.rstrip("/")


def _scope_matches(pattern: str, path: str) -> bool:
    pattern = _normalize_path(pattern)
    path = _normalize_path(path)
    if not pattern:
        return False
    if pattern in {"*", "**"}:
        return True
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    if pattern.endswith("/*"):
        prefix = pattern[:-2].rstrip("/")
        suffix = path[len(prefix):].lstrip("/") if path.startswith(prefix) else ""
        return path.startswith(prefix + "/") and "/" not in suffix
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatchcase(path, pattern)
    return path == pattern or path.startswith(pattern.rstrip("/") + "/")


def _scopes_overlap(left: str, right: str) -> bool:
    left = _normalize_path(left)
    right = _normalize_path(right)
    if not left or not right:
        return False
    if _scope_matches(left, right) or _scope_matches(right, left):
        return True
    left_prefix = _literal_prefix(left)
    right_prefix = _literal_prefix(right)
    if not left_prefix or not right_prefix:
        return False
    return (
        left_prefix == right_prefix
        or left_prefix.startswith(right_prefix.rstrip("/") + "/")
        or right_prefix.startswith(left_prefix.rstrip("/") + "/")
    )


def _parallel_gate_requested(metadata: Mapping[str, Any]) -> bool:
    if any(key in metadata for key in _PARALLEL_MARKER_KEYS):
        return True
    if _string(metadata.get("worker_role")) == "mf_sub":
        return True
    return bool(metadata.get("parallel_agent_contract_required"))


def _participant_scopes(participant: Mapping[str, Any]) -> list[str]:
    scopes: list[str] = []
    for key in ("write_scope", "owns", "target_files"):
        scopes.extend(_string_list(participant.get(key)))
    return [_normalize_path(item) for item in scopes if _normalize_path(item)]


def _load_contract_row(
    conn: sqlite3.Connection,
    contract_id: str,
) -> tuple[str, dict[str, Any]]:
    row = conn.execute(
        "SELECT status, chain_trigger_json FROM backlog_bugs WHERE bug_id = ?",
        (contract_id,),
    ).fetchone()
    if row is None:
        raise ParallelAgentContractError(
            f"parallel contract backlog row not found: {contract_id}"
        )
    status = _string(row["status"] if isinstance(row, sqlite3.Row) else row[0]).upper()
    if status not in _ACTIVE_BACKLOG_STATUSES:
        raise ParallelAgentContractError(
            f"parallel contract backlog row must be active, got {status or 'unknown'}"
        )
    raw = row["chain_trigger_json"] if isinstance(row, sqlite3.Row) else row[1]
    return status, _json_object(raw)


def _contract_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    payload = raw.get("parallel_contract")
    if isinstance(payload, Mapping):
        return dict(payload)
    return dict(raw)


def _validate_contract_shape(
    raw: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    row_type = _string(raw.get("row_type") or payload.get("row_type"))
    contract_type = _string(raw.get("contract_type") or payload.get("contract_type"))
    contract_status = _string(raw.get("contract_status") or payload.get("contract_status")).upper()
    if row_type and row_type != CONTRACT_ROW_TYPE:
        raise ParallelAgentContractError(
            f"parallel contract row_type must be {CONTRACT_ROW_TYPE}"
        )
    if contract_type and contract_type != CONTRACT_TYPE:
        raise ParallelAgentContractError(
            f"parallel contract contract_type must be {CONTRACT_TYPE}"
        )
    if contract_status != ACCEPTED_CONTRACT_STATUS:
        raise ParallelAgentContractError(
            "parallel contract must be ACCEPTED before parallel task creation"
        )

    participants_raw = payload.get("participants")
    if not isinstance(participants_raw, Sequence) or isinstance(
        participants_raw, (str, bytes, bytearray)
    ):
        raise ParallelAgentContractError("parallel contract participants must be a list")
    participants: list[dict[str, Any]] = []
    scopes_by_agent: dict[str, list[str]] = {}
    seen: set[str] = set()
    for item in participants_raw:
        if not isinstance(item, Mapping):
            raise ParallelAgentContractError("parallel contract participant must be an object")
        participant = dict(item)
        agent_id = _string(
            participant.get("agent_id")
            or participant.get("id")
            or participant.get("name")
            or participant.get("role")
        )
        if not agent_id:
            raise ParallelAgentContractError("parallel contract participant missing agent_id")
        if agent_id in seen:
            raise ParallelAgentContractError(f"duplicate parallel contract participant: {agent_id}")
        scopes = _participant_scopes(participant)
        if not scopes:
            raise ParallelAgentContractError(
                f"parallel contract participant {agent_id} missing write_scope"
            )
        participant["agent_id"] = agent_id
        participants.append(participant)
        scopes_by_agent[agent_id] = scopes
        seen.add(agent_id)

    shared_interfaces = payload.get("shared_interfaces")
    if not isinstance(shared_interfaces, Sequence) or not shared_interfaces:
        raise ParallelAgentContractError(
            "parallel contract shared_interfaces must declare the cross-agent API/schema"
        )
    integration_gate = payload.get("integration_gate")
    if not isinstance(integration_gate, Mapping):
        raise ParallelAgentContractError("parallel contract integration_gate is required")
    required_checks = _string_list(integration_gate.get("required_checks"))
    if not required_checks:
        raise ParallelAgentContractError(
            "parallel contract integration_gate.required_checks is required"
        )

    agent_ids = list(scopes_by_agent)
    for index, left_id in enumerate(agent_ids):
        for right_id in agent_ids[index + 1:]:
            for left_scope in scopes_by_agent[left_id]:
                for right_scope in scopes_by_agent[right_id]:
                    if _scopes_overlap(left_scope, right_scope):
                        raise ParallelAgentContractError(
                            "parallel contract has overlapping write scopes: "
                            f"{left_id}:{left_scope} overlaps {right_id}:{right_scope}"
                        )

    return participants, scopes_by_agent


def validate_parallel_agent_task_gate(
    conn: sqlite3.Connection,
    project_id: str,
    task_type: str,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate task metadata against an accepted parallel-agent contract.

    Returns an evidence payload when the task is parallel-gated. Non-parallel
    tasks return an empty dict so callers can keep the normal backlog path.
    """

    metadata = dict(metadata or {})
    if not _parallel_gate_requested(metadata):
        return {}

    contract_id = _string(
        metadata.get("parallel_contract_id")
        or metadata.get("parallel_agent_contract_id")
    )
    if not contract_id:
        raise ParallelAgentContractError(
            "parallel_contract_id is required for parallel agent task creation"
        )
    agent_id = _string(
        metadata.get("parallel_agent_id")
        or metadata.get("agent_id")
        or metadata.get("worker_id")
        or metadata.get("worker_role")
    )
    if not agent_id:
        raise ParallelAgentContractError(
            "parallel_agent_id is required for parallel agent task creation"
        )

    _row_status, raw = _load_contract_row(conn, contract_id)
    payload = _contract_payload(raw)
    parent_bug_id = _string(raw.get("parent_bug_id") or payload.get("parent_bug_id"))
    task_bug_id = _string(metadata.get("bug_id"))
    if parent_bug_id and task_bug_id and parent_bug_id != task_bug_id:
        raise ParallelAgentContractError(
            f"parallel contract parent_bug_id {parent_bug_id} does not match task bug_id {task_bug_id}"
        )

    participants, scopes_by_agent = _validate_contract_shape(raw, payload)
    participant_ids = {participant["agent_id"] for participant in participants}
    if agent_id not in participant_ids:
        raise ParallelAgentContractError(
            f"parallel_agent_id {agent_id} is not declared in contract {contract_id}"
        )

    target_files = [_normalize_path(path) for path in _string_list(metadata.get("target_files"))]
    owned_scope = scopes_by_agent[agent_id]
    outside = [
        path for path in target_files
        if not any(_scope_matches(scope, path) for scope in owned_scope)
    ]
    if outside:
        raise ParallelAgentContractError(
            "parallel task target_files outside participant write_scope: "
            + ", ".join(outside)
        )

    other_cross_writes: list[str] = []
    for path in target_files:
        for other_agent, other_scopes in scopes_by_agent.items():
            if other_agent == agent_id:
                continue
            if any(_scope_matches(scope, path) for scope in other_scopes):
                other_cross_writes.append(f"{path} owned by {other_agent}")
    if other_cross_writes:
        raise ParallelAgentContractError(
            "parallel task violates forbidden cross-writes: "
            + ", ".join(sorted(set(other_cross_writes)))
        )

    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "project_id": project_id,
        "task_type": task_type,
        "contract_id": contract_id,
        "contract_status": ACCEPTED_CONTRACT_STATUS,
        "agent_id": agent_id,
        "parent_bug_id": parent_bug_id,
        "target_files": target_files,
        "owned_write_scope": owned_scope,
        "participant_count": len(participants),
    }
