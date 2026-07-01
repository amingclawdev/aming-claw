"""ContractRuntime close-authority projections shared by governance handlers."""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import task_timeline

CONTRACT_RUNTIME_CLOSE_AUTHORITY_SCHEMA_VERSION = (
    "contract_runtime_close_authority_projection.v1"
)


def _first_deep_text(value: Any, key: str) -> str:
    if isinstance(value, Mapping):
        direct = value.get(key)
        if isinstance(direct, str):
            return direct.strip()
        if direct is not None and not isinstance(direct, (Mapping, list, tuple)):
            return str(direct).strip()
        for child in value.values():
            found = _first_deep_text(child, key)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for child in value:
            found = _first_deep_text(child, key)
            if found:
                return found
    return ""


def _batch_child_bridge_event(
    *,
    project_id: str,
    backlog_id: str,
    contract_execution_id: str,
    child_task_ids: Sequence[str],
) -> dict[str, Any]:
    child_identities = [
        {
            "project_id": project_id,
            "backlog_id": backlog_id,
            "task_id": task_id,
        }
        for task_id in dict.fromkeys(
            str(task_id or "").strip() for task_id in child_task_ids
        )
        if task_id
    ]
    return {
        "id": "contract-runtime:onboard-service:batch-child-bridge",
        "event_id": (
            f"contract-runtime:{contract_execution_id}:"
            "batch-child-cross-ref-bridge"
        ),
        "project_id": project_id,
        "backlog_id": backlog_id,
        "task_id": contract_execution_id or backlog_id,
        "event_type": "contract_runtime.cross_ref_lineage_bridge",
        "event_kind": "cross_ref_lineage_bridge",
        "phase": "lineage",
        "actor": "contract-runtime",
        "status": "accepted",
        "payload": {
            "schema_version": "contract_runtime.batch_child_cross_ref_bridge.v1",
            "contract_runtime_projection": True,
            "contract_execution_id": contract_execution_id,
            "bridge_source": "runtime_context_merge_queue_child_lane",
            "bridged_identities": [
                {
                    "project_id": project_id,
                    "backlog_id": backlog_id,
                    "task_id": backlog_id,
                },
                {
                    "project_id": project_id,
                    "backlog_id": backlog_id,
                    "task_id": "",
                },
                *child_identities,
            ],
        },
        "artifact_refs": {
            "contract_execution_id": contract_execution_id,
            "child_task_ids": [item["task_id"] for item in child_identities],
        },
    }


def runtime_context_child_lane_close_authority_projection(
    *,
    project_id: str,
    backlog_id: str,
    requested_execution_id: str,
    close_commit: str,
    timeline_events: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Project verified multi-row child-lane evidence when no cex exists yet."""

    if not requested_execution_id.startswith("onboard-service-"):
        return {}

    events = [
        dict(event)
        for event in timeline_events or []
        if isinstance(event, Mapping)
        and str(event.get("project_id") or "").strip() == project_id
        and str(event.get("backlog_id") or "").strip() == backlog_id
    ]
    if not events:
        return {}

    child_task_ids: list[str] = []
    protected_child_event_ids: list[Any] = []
    merge_queue_ids: list[str] = []
    merge_queue_item_ids: list[str] = []
    for event in events:
        if not task_timeline.is_protected_close_evidence(event):
            continue
        if not task_timeline._route_event_passed(event):
            continue
        merge_queue_id = _first_deep_text(event, "merge_queue_id")
        if merge_queue_id:
            merge_queue_ids.append(merge_queue_id)
        merge_queue_item_id = _first_deep_text(event, "merge_queue_item_id")
        if merge_queue_item_id:
            merge_queue_item_ids.append(merge_queue_item_id)
        task_id = str(event.get("task_id") or "").strip()
        if task_id and task_id not in {backlog_id, requested_execution_id}:
            child_task_ids.append(task_id)
            protected_child_event_ids.append(event.get("id") or event.get("event_id"))
    child_task_ids = list(dict.fromkeys(child_task_ids))
    if not child_task_ids:
        return {}
    merge_queue_ids = list(dict.fromkeys(merge_queue_ids))
    merge_queue_item_ids = list(dict.fromkeys(merge_queue_item_ids))
    if not merge_queue_ids:
        return {
            "schema_version": CONTRACT_RUNTIME_CLOSE_AUTHORITY_SCHEMA_VERSION,
            "accepted": False,
            "status": "runtime_context_child_lane_missing_merge_queue_ref",
            "contract_execution_id": requested_execution_id,
            "requested_contract_execution_id": requested_execution_id,
            "missing_requirement_ids": ["merge_queue_id"],
            "runtime_context_child_lane_close_authority_gate": {
                "schema_version": "runtime_context_child_lane_close_authority_gate.v1",
                "passed": False,
                "status": "failed",
                "source": "runtime_context_merge_queue_child_lane",
                "contract_execution_id": requested_execution_id,
                "child_task_ids": child_task_ids,
                "protected_child_event_ids": protected_child_event_ids,
                "missing_requirement_ids": ["merge_queue_id"],
            },
        }

    bridge = _batch_child_bridge_event(
        project_id=project_id,
        backlog_id=backlog_id,
        contract_execution_id=requested_execution_id,
        child_task_ids=child_task_ids,
    )
    projected_events = [bridge, *events]
    verification = task_timeline.mf_close_gate_verification(
        projected_events,
        contract={
            "project_id": project_id,
            "template_id": "mf_batch_parallel.v1",
            "contract_instance_id": backlog_id,
            "close_context": {"close_commit": close_commit},
            "governance_policy": {
                "requirements": {
                    "close_timeline": True,
                    "worker_graph_trace": False,
                    "independent_qa": True,
                }
            },
        },
    )
    if not bool(verification.get("passed")):
        return {
            "schema_version": CONTRACT_RUNTIME_CLOSE_AUTHORITY_SCHEMA_VERSION,
            "accepted": False,
            "status": "runtime_context_child_lane_incomplete",
            "contract_execution_id": requested_execution_id,
            "requested_contract_execution_id": requested_execution_id,
            "projected_events": projected_events,
            "missing_requirement_ids": (
                list(verification.get("missing_event_kinds") or [])
                or [
                    str(item.get("gate") or "")
                    for item in verification.get("failed_gates") or []
                    if isinstance(item, Mapping) and item.get("gate")
                ]
            ),
            "runtime_context_child_lane_close_authority_gate": {
                "schema_version": "runtime_context_child_lane_close_authority_gate.v1",
                "passed": False,
                "status": "failed",
                "source": "runtime_context_merge_queue_child_lane",
                "contract_execution_id": requested_execution_id,
                "child_task_ids": child_task_ids,
                "protected_child_event_ids": protected_child_event_ids,
                "merge_queue_ids": merge_queue_ids,
                "merge_queue_item_ids": merge_queue_item_ids,
            },
        }

    source_refs = [
        f"contract_runtime:{requested_execution_id}",
        *[
            f"timeline:{event_id}"
            for event_id in protected_child_event_ids
            if str(event_id or "").strip()
        ],
    ]
    return {
        "schema_version": CONTRACT_RUNTIME_CLOSE_AUTHORITY_SCHEMA_VERSION,
        "accepted": True,
        "status": "projected_runtime_context_child_lane",
        "close_authority": {
            "schema_version": "contract_runtime_close_authority.v1",
            "source": "runtime_context_merge_queue_child_lane_projection",
            "legacy": False,
            "advisory": False,
            "authoritative": True,
            "close_authoritative": True,
            "can_close_authoritative": True,
            "contract_execution_id": requested_execution_id,
            "source_refs": source_refs,
            "message": (
                "backlog_close accepted server-derived runtime-context child lane "
                "evidence for an onboard-service contract chain without a cex child."
            ),
        },
        "legacy_advisory": False,
        "authoritative": True,
        "projection_authoritative": True,
        "contract_execution_id": requested_execution_id,
        "requested_contract_execution_id": requested_execution_id,
        "projected_events": projected_events,
        "source_contract_execution_ids": [requested_execution_id],
        "runtime_context_child_lane_close_authority_gate": {
            "schema_version": "runtime_context_child_lane_close_authority_gate.v1",
            "accepted": True,
            "passed": True,
            "status": "passed",
            "source": "runtime_context_merge_queue_child_lane",
            "primary_decision_source": True,
            "meta_contract_gate_decision_source": False,
            "contract_execution_id": requested_execution_id,
            "child_task_ids": child_task_ids,
            "protected_child_event_ids": protected_child_event_ids,
            "merge_queue_ids": merge_queue_ids,
            "merge_queue_item_ids": merge_queue_item_ids,
            "source_refs": source_refs,
            "checks": {
                "normal_close_gate_passed_with_projected_bridge": True,
                "same_backlog_project_floor_enforced": True,
                "independent_qa_required": True,
                "merge_queue_ref_required": True,
            },
        },
        "mf_parallel_close_authority_gate": {
            "schema_version": "contract_runtime_mf_parallel_close_authority_gate.v1",
            "accepted": True,
            "passed": True,
            "status": "passed",
            "source": "runtime_context_merge_queue_child_lane",
            "primary_decision_source": True,
            "meta_contract_gate_decision_source": False,
            "contract_execution_id": requested_execution_id,
            "merge_queue_ids": merge_queue_ids,
            "merge_queue_item_ids": merge_queue_item_ids,
            "source_refs": source_refs,
            "missing_requirement_ids": [],
            "checks": {
                "has_runtime_context_child_lane": True,
                "normal_close_gate_passed_with_projected_bridge": True,
                "independent_qa_required": True,
                "merge_queue_ref_required": True,
            },
        },
    }
