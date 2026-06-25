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
    completed_lines: Sequence[Mapping[str, Any]] | None = None,
    route_token_ref: str = "",
    instruction_bundle_hash: str = "",
    execution_state_revision: int = 1,
) -> dict[str, Any]:
    """Build a deterministic read model for one contract execution."""

    completed_items = list(completed_lines or [])
    completed = {
        _completed_line_key(item)
        for item in completed_items
        if _completed_line_key(item)[0] and _completed_line_key(item)[1]
    }
    next_action = None
    for stage, line in iter_stage_lines(definition):
        stage_id = str(stage.get("stage_id") or "")
        line_id = str(line.get("line_id") or "")
        line_instances = _line_instances_for(definition, line, completed_items)
        for instance in line_instances:
            key = (stage_id, line_id, str((instance or {}).get("line_instance_id") or ""))
            if _is_completed(key, completed, line_instances):
                continue
            next_action = {
                "stage_id": stage_id,
                "line_id": line_id,
                "owner_role": line.get("owner_role", ""),
                "allowed_writer_roles": list(line.get("allowed_writer_roles") or []),
                "evidence_kind": line.get("evidence_kind", ""),
                "required": bool(line.get("required", True)),
            }
            if instance:
                next_action.update(instance)
            break
        if next_action is not None:
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
        "completed_lines": _completed_line_state(completed),
        "next_action": next_action,
    }
    state["execution_state_hash"] = stable_sha256(
        {key: value for key, value in state.items() if key != "execution_state_hash"}
    )
    return state


_MF_PARALLEL_CONTRACT_IDS = frozenset({"mf_parallel", "mf_parallel.v1"})
_MF_PARALLEL_WORKER_DISPATCH_LINE = (
    "dispatch",
    "observer_dispatch_bounded_workers",
)
_MF_PARALLEL_WORKER_LIST_KEYS = (
    "workers",
    "bounded_workers",
    "worker_contexts",
    "lanes",
)


def _completed_line_key(item: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(item.get("stage_id") or ""),
        str(item.get("line_id") or ""),
        _line_instance_id_from_mapping(item),
    )


def _completed_line_state(
    completed: set[tuple[str, str, str]],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for stage_id, line_id, line_instance_id in sorted(completed):
        row = {"stage_id": stage_id, "line_id": line_id}
        if line_instance_id:
            row["line_instance_id"] = line_instance_id
        rows.append(row)
    return rows


def _line_instances_for(
    definition: Mapping[str, Any],
    line: Mapping[str, Any],
    completed_lines: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    if not _is_mf_parallel_worker_line(definition, line):
        return [{}]
    instances = _mf_parallel_worker_instances(completed_lines)
    return instances or [{}]


def _is_mf_parallel_worker_line(
    definition: Mapping[str, Any],
    line: Mapping[str, Any],
) -> bool:
    contract_id = str(definition.get("contract_id") or "")
    if contract_id not in _MF_PARALLEL_CONTRACT_IDS:
        return False
    owner_role = str(line.get("owner_role") or "").strip().lower().replace("-", "_")
    allowed_roles = {
        str(item or "").strip().lower().replace("-", "_")
        for item in (line.get("allowed_writer_roles") or [])
    }
    return owner_role == "mf_sub" or "mf_sub" in allowed_roles


def _mf_parallel_worker_instances(
    completed_lines: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    instances: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in completed_lines:
        if (
            str(item.get("stage_id") or ""),
            str(item.get("line_id") or ""),
        ) != _MF_PARALLEL_WORKER_DISPATCH_LINE:
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
        for index, worker in enumerate(_iter_worker_payloads(payload), start=1):
            instance = _worker_instance(worker, index=index)
            line_instance_id = instance.get("line_instance_id", "")
            if not line_instance_id or line_instance_id in seen:
                continue
            seen.add(line_instance_id)
            instances.append(instance)
    return instances


def _iter_worker_payloads(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    workers: list[Mapping[str, Any]] = []
    for key in _MF_PARALLEL_WORKER_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            workers.extend(item for item in value if isinstance(item, Mapping))
    if workers:
        return workers
    worker = payload.get("worker") or payload.get("bounded_worker")
    if isinstance(worker, Mapping):
        return [worker]
    return []


def _worker_instance(worker: Mapping[str, Any], *, index: int) -> dict[str, str]:
    runtime_context_id = _first_mapping_text(
        worker,
        "runtime_context_id",
        "runtimeContextId",
        nested_keys=("runtime_context", "context", "branch_context"),
    )
    task_id = _first_mapping_text(
        worker,
        "task_id",
        "taskId",
        nested_keys=("runtime_context", "context", "branch_context"),
    )
    parent_task_id = _first_mapping_text(
        worker,
        "parent_task_id",
        "parentTaskId",
        nested_keys=("runtime_context", "context", "branch_context"),
    )
    lane_id = _first_mapping_text(
        worker,
        "lane_id",
        "lane",
        "worker_slot_id",
        "worker_id",
        "id",
        "name",
        nested_keys=("runtime_context", "context", "branch_context"),
    )
    worker_slot_id = _first_mapping_text(
        worker,
        "worker_slot_id",
        "worker_id",
        nested_keys=("runtime_context", "context", "branch_context"),
    )
    line_instance_id = _first_mapping_text(worker, "line_instance_id", "instance_id")
    if not line_instance_id:
        line_instance_id = _line_instance_id_from_values(
            runtime_context_id=runtime_context_id,
            task_id=task_id,
            lane_id=lane_id,
            fallback=f"worker:{index}",
        )
    instance = {
        "line_instance_id": line_instance_id,
        "worker_index": str(index),
    }
    for key, value in (
        ("runtime_context_id", runtime_context_id),
        ("task_id", task_id),
        ("parent_task_id", parent_task_id),
        ("lane_id", lane_id),
        ("worker_slot_id", worker_slot_id),
        ("worker_role", "mf_sub"),
    ):
        if value:
            instance[key] = value
    return instance


def _is_completed(
    key: tuple[str, str, str],
    completed: set[tuple[str, str, str]],
    line_instances: Sequence[Mapping[str, Any]],
) -> bool:
    if key in completed:
        return True
    has_instances = any(
        str(instance.get("line_instance_id") or "") for instance in line_instances
    )
    if not key[2] and not has_instances:
        return any(
            stage_id == key[0] and line_id == key[1]
            for stage_id, line_id, _instance_id in completed
        )
    if key[2] and len(line_instances) == 1:
        return (key[0], key[1], "") in completed
    return False


def _line_instance_id_from_mapping(item: Mapping[str, Any]) -> str:
    explicit = _first_mapping_text(item, "line_instance_id", "instance_id")
    if explicit:
        return explicit
    payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
    return _line_instance_id_from_values(
        runtime_context_id=_first_mapping_text(item, "runtime_context_id")
        or _first_mapping_text(payload, "runtime_context_id"),
        task_id=_first_mapping_text(item, "task_id") or _first_mapping_text(payload, "task_id"),
        lane_id=_first_mapping_text(item, "lane_id", "worker_slot_id", "worker_id")
        or _first_mapping_text(payload, "lane_id", "worker_slot_id", "worker_id"),
    )


def _line_instance_id_from_values(
    *,
    runtime_context_id: str = "",
    task_id: str = "",
    lane_id: str = "",
    fallback: str = "",
) -> str:
    if runtime_context_id:
        return f"runtime_context:{runtime_context_id}"
    if task_id:
        return f"task:{task_id}"
    if lane_id:
        return f"lane:{lane_id}"
    return fallback


def _first_mapping_text(
    item: Mapping[str, Any],
    *keys: str,
    nested_keys: Sequence[str] = (),
) -> str:
    for key in keys:
        value = item.get(key)
        text = str(value or "").strip()
        if text:
            return text
    for nested_key in nested_keys:
        nested = item.get(nested_key)
        if not isinstance(nested, Mapping):
            continue
        text = _first_mapping_text(nested, *keys)
        if text:
            return text
    return ""
