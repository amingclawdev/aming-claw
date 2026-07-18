"""Public-safe ContractRuntime read model for dashboard visualization.

This module deliberately accepts already-loaded governance records and returns a
small allow-listed projection.  It is a read boundary: callers may pass raw
ContractRuntime records and timeline events, but route/session/worktree secrets
never cross the returned payload.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA_VERSION = "contract_runtime.visualization.v1"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _scalar_text(value: Any) -> str:
    if isinstance(value, Mapping) or (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    ):
        return ""
    return _text(value)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _nested_text(value: Mapping[str, Any], *keys: str) -> str:
    containers = [
        value,
        _mapping(value.get("payload")),
        _mapping(value.get("decision")),
        _mapping(value.get("bypass")),
        _mapping(value.get("verification")),
        _mapping(value.get("artifact_refs")),
    ]
    for container in containers:
        for key in keys:
            text = _scalar_text(container.get(key))
            if text:
                return text
    return ""


def _safe_refs(values: Any, *, limit: int = 24) -> list[str]:
    if isinstance(values, str):
        items: Sequence[Any] = [values]
    elif isinstance(values, Sequence):
        items = values
    else:
        items = []
    forbidden = ("token", "session", "worktree", "fence", "credential", "secret")
    refs: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        ref = _text(item)
        if not ref or any(term in ref.lower() for term in forbidden):
            continue
        if ref not in refs:
            refs.append(ref)
        if len(refs) >= limit:
            break
    return refs


def _safe_ref(value: Any) -> str:
    refs = _safe_refs([value], limit=1)
    return refs[0] if refs else ""


def _next_action_summary(value: Any) -> dict[str, Any]:
    action = _mapping(value)
    if not action:
        return {}
    result: dict[str, Any] = {}
    for key in (
        "id",
        "action",
        "stage_id",
        "line_id",
        "line_instance_id",
        "evidence_kind",
        "owner_role",
        "worker_role",
        "lane_id",
        "status",
        "required",
        "source",
        "precedence",
        "contract_execution_id",
        "execution_state_revision",
        "mode",
        "block_reason",
        "diagnostic_backlog_id",
        "audited_bypass_continuation",
        "no_pass_claim",
    ):
        if key in action and action.get(key) not in (None, "", [], {}):
            value = action.get(key)
            if isinstance(value, Mapping) or (
                isinstance(value, Sequence) and not isinstance(value, (str, bytes))
            ):
                continue
            result[key] = _text(value) if isinstance(value, str) else value
    roles = action.get("allowed_writer_roles")
    if isinstance(roles, Sequence) and not isinstance(roles, (str, bytes)):
        result["allowed_writer_roles"] = [_text(role) for role in roles if _text(role)]
    return result


def _line_summary(
    line: Mapping[str, Any],
    *,
    contract_execution_id: str,
    index: int,
) -> dict[str, Any]:
    status = _nested_text(line, "status", "decision", "result") or "accepted"
    line_id = _nested_text(line, "line_id", "id")
    stage_id = _nested_text(line, "stage_id", "phase")
    evidence_kind = _nested_text(line, "evidence_kind", "event_kind", "kind")
    classification = _nested_text(line, "classification", "bypass_classification")
    decision = _nested_text(line, "decision", "action")
    bypass_text = " ".join(
        (status, line_id, stage_id, evidence_kind, classification, decision)
    ).lower()
    bypassed = "bypass" in bypass_text or "waiv" in bypass_text
    summary = {
        "id": f"contract-line:{contract_execution_id}:{index}:{line_id or 'line'}",
        "contract_execution_id": contract_execution_id,
        "index": index,
        "stage_id": stage_id,
        "line_id": line_id,
        "evidence_kind": evidence_kind,
        "owner_role": _nested_text(
            line,
            "evidence_owner_role",
            "actor_role",
            "owner_role",
            "worker_role",
            "actor",
        ),
        "status": "bypassed" if bypassed else status,
        "recorded_at": _nested_text(
            line,
            "accepted_at",
            "recorded_at",
            "created_at",
            "completed_at",
        ),
        "source_ref": _safe_ref(
            _nested_text(
                line,
                "source_ref",
                "event_ref",
                "timeline_event_ref",
                "implementation_event_ref",
            )
        ),
        "bypassed": bypassed,
    }
    if bypassed:
        summary["bypass"] = {
            "classification": classification or "audited_bypass",
            "decision": decision or "bypass",
            "reason": _nested_text(line, "reason", "block_reason"),
            "diagnostic_backlog_id": _nested_text(
                line,
                "diagnostic_backlog_id",
                "diagnostic_bug_id",
            ),
            "no_pass_claim": True,
        }
    return summary


def _runtime_record_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    guide = _mapping(record.get("runtime_guide"))
    state = _mapping(record.get("execution_state"))
    next_action = _next_action_summary(guide.get("next_legal_action"))
    readiness = "contract_active" if next_action else "contract_complete"
    return {
        "contract_execution_id": _text(record.get("contract_execution_id")),
        "parent_contract_execution_id": _text(
            record.get("parent_contract_execution_id")
        ),
        "root_contract_execution_id": _text(record.get("root_contract_execution_id")),
        "contract_chain_id": _text(record.get("contract_chain_id")),
        "contract_id": _text(record.get("contract_id")),
        "contract_revision_id": _text(record.get("revision")),
        "contract_hash": _text(record.get("definition_hash")),
        "execution_state_revision": _int(
            record.get("execution_state_revision")
            or state.get("execution_state_revision")
        ),
        "execution_state_hash": _text(state.get("execution_state_hash")),
        "runtime_guide_hash": _text(guide.get("runtime_guide_hash")),
        "readiness_state": readiness,
        "next_legal_action": next_action,
        "updated_at": _text(record.get("updated_at")),
    }


def _event_summary(event: Mapping[str, Any]) -> dict[str, Any]:
    event_id = event.get("id") or event.get("event_id") or ""
    summary = {
        "id": event_id,
        "event_id": event_id,
        "backlog_id": _text(event.get("backlog_id")),
        "task_id": _text(event.get("task_id")),
        "event_type": _text(event.get("event_type")),
        "event_kind": _text(event.get("event_kind")),
        "phase": _text(event.get("phase")),
        "actor": _text(event.get("actor")),
        "status": _text(event.get("status")),
        "commit_sha": _text(event.get("commit_sha")),
        "parent_event_id": event.get("parent_event_id") or None,
        "created_at": _text(event.get("created_at")),
    }
    payload_ref = _mapping(event.get("payload_ref"))
    if payload_ref:
        summary["payload_ref"] = {
            key: payload_ref.get(key)
            for key in ("event_id", "payload_sha256", "payload_bytes")
            if payload_ref.get(key) not in (None, "")
        }
    return summary


def _legacy_advisory(value: Any) -> dict[str, Any]:
    advisory = _mapping(value)
    if not advisory:
        return {}
    return {
        key: advisory.get(key)
        for key in (
            "id",
            "legacy",
            "historical",
            "advisory_only",
            "required",
            "authorization_blocker",
            "ignored_as_next_legal_action",
            "source",
            "replacement_authority",
            "semantic_blocker_reason",
            "message",
        )
        if advisory.get(key) not in (None, "", [], {})
    }


def _edge(
    source: str,
    target: str,
    relationship: str,
    *,
    authority_source: str,
    evidence_ref: str = "",
    inferred: bool = False,
) -> dict[str, Any]:
    edge_id = f"{relationship}:{source}:{target}"
    result = {
        "id": edge_id,
        "source": source,
        "target": target,
        "relationship": relationship,
        "authority_source": authority_source,
        "inferred": bool(inferred),
    }
    if evidence_ref:
        result["evidence_ref"] = evidence_ref
    return result


def build_contract_runtime_visualization(
    *,
    project_id: str,
    backlog: Mapping[str, Any],
    runtime_records: Sequence[Mapping[str, Any]],
    chain_current: Mapping[str, Any],
    chain_edges: Sequence[Mapping[str, Any]],
    timeline_events: Sequence[Mapping[str, Any]],
    compact_ledger_row: Mapping[str, Any] | None = None,
    timeline_total: int | None = None,
    timeline_limit: int = 100,
    timeline_has_more: bool = False,
    next_cursor: str = "",
    generated_at: str = "",
) -> dict[str, Any]:
    """Build the canonical public-safe visualization projection."""

    backlog_id = _text(backlog.get("bug_id") or backlog.get("backlog_id"))
    current = _mapping(chain_current)
    all_records = [
        dict(record) for record in runtime_records if isinstance(record, Mapping)
    ]
    runtime_record_total = len(all_records)
    records = all_records[:50]
    current_execution_id = _text(
        current.get("current_contract_execution_id")
        or current.get("root_contract_execution_id")
    )
    current_record = next(
        (
            record
            for record in records
            if _text(record.get("contract_execution_id")) == current_execution_id
        ),
        records[0] if records else {},
    )
    runtime_current = _runtime_record_summary(current_record) if current_record else {}
    embedded_current = _mapping(current.get("contract_runtime_current_state"))
    if not runtime_current and current:
        runtime_current = {
            "contract_execution_id": current_execution_id,
            "root_contract_execution_id": _text(
                current.get("root_contract_execution_id")
            ),
            "contract_chain_id": _text(current.get("contract_chain_id")),
            "contract_id": _text(current.get("current_contract_id")),
            "contract_revision_id": _text(
                embedded_current.get("contract_revision_id")
            ),
            "contract_hash": _text(embedded_current.get("contract_hash")),
            "execution_state_revision": _int(
                embedded_current.get("execution_state_revision")
                or current.get("generation")
            ),
            "execution_state_hash": _text(
                embedded_current.get("execution_state_hash")
            ),
            "runtime_guide_hash": _text(embedded_current.get("runtime_guide_hash")),
            "readiness_state": _text(current.get("readiness_state")),
            "next_legal_action": {},
            "updated_at": _text(current.get("updated_at")),
        }
    chain_next_action = _next_action_summary(current.get("next_legal_action"))
    if chain_next_action:
        runtime_current["next_legal_action"] = chain_next_action
        runtime_current["readiness_state"] = _text(
            current.get("readiness_state") or "contract_active"
        )
    elif current:
        runtime_current["readiness_state"] = _text(
            current.get("readiness_state") or runtime_current.get("readiness_state")
        )

    line_states: list[dict[str, Any]] = []
    line_states_by_execution: dict[str, list[dict[str, Any]]] = {}
    line_state_total = 0
    for record in records:
        execution_id = _text(record.get("contract_execution_id"))
        completed = record.get("completed_lines")
        if not isinstance(completed, Sequence) or isinstance(completed, (str, bytes)):
            completed = _mapping(record.get("runtime_guide")).get("completed_lines") or []
        execution_lines: list[dict[str, Any]] = []
        for index, line in enumerate(completed, start=1):
            if not isinstance(line, Mapping):
                continue
            line_state_total += 1
            if len(line_states) >= 500:
                continue
            summary = _line_summary(
                line,
                contract_execution_id=execution_id,
                index=index,
            )
            execution_lines.append(summary)
            line_states.append(summary)
        line_states_by_execution[execution_id] = execution_lines

    event_summaries = [_event_summary(event) for event in timeline_events]
    ledger = _mapping(compact_ledger_row)

    advisories: list[dict[str, Any]] = []
    for candidate in (
        current.get("legacy_route_action_precheck_advisory"),
        _mapping(current.get("authority_projection")).get(
            "legacy_route_action_precheck"
        ),
    ):
        advisory = _legacy_advisory(candidate)
        if advisory and advisory not in advisories:
            advisories.append(advisory)

    conflicts: list[dict[str, Any]] = []
    if current_execution_id and runtime_current and (
        current_execution_id != runtime_current.get("contract_execution_id")
    ):
        conflicts.append(
            {
                "kind": "current_execution_mismatch",
                "authority_source": "backlog_contract_chain_current",
                "chain_value": current_execution_id,
                "runtime_value": runtime_current.get("contract_execution_id"),
            }
        )
    embedded_revision = _int(embedded_current.get("execution_state_revision"))
    runtime_revision = _int(runtime_current.get("execution_state_revision"))
    if embedded_revision and runtime_revision and embedded_revision != runtime_revision:
        conflicts.append(
            {
                "kind": "execution_state_revision_mismatch",
                "authority_source": "contract_runtime",
                "chain_value": embedded_revision,
                "runtime_value": runtime_revision,
            }
        )

    nodes: list[dict[str, Any]] = [
        {
            "id": f"backlog:{backlog_id}",
            "kind": "backlog",
            "label": _text(backlog.get("title")) or backlog_id,
            "status": _text(backlog.get("status")),
            "authority_source": "backlog_bugs",
        }
    ]
    edges: list[dict[str, Any]] = []
    for record in records:
        summary = _runtime_record_summary(record)
        execution_id = _text(summary.get("contract_execution_id"))
        if not execution_id:
            continue
        node_id = f"contract-execution:{execution_id}"
        nodes.append(
            {
                "id": node_id,
                "kind": "contract_execution",
                "label": _text(summary.get("contract_id")) or execution_id,
                "contract_execution_id": execution_id,
                "contract_id": summary.get("contract_id"),
                "execution_state_revision": summary.get("execution_state_revision"),
                "status": summary.get("readiness_state"),
                "authority_source": "contract_runtime",
            }
        )
        if not _text(record.get("parent_contract_execution_id")):
            edges.append(
                _edge(
                    f"backlog:{backlog_id}",
                    node_id,
                    "backlog_contract_root",
                    authority_source="contract_runtime",
                )
            )
        previous_line_id = ""
        for line in line_states_by_execution.get(execution_id, []):
            line_node_id = _text(line.get("id"))
            nodes.append(
                {
                    "id": line_node_id,
                    "kind": "contract_line",
                    "label": _text(line.get("line_id")) or _text(line.get("stage_id")),
                    "contract_execution_id": execution_id,
                    "stage_id": line.get("stage_id"),
                    "line_id": line.get("line_id"),
                    "owner_role": line.get("owner_role"),
                    "status": line.get("status"),
                    "bypassed": bool(line.get("bypassed")),
                    "authority_source": "contract_runtime.completed_lines",
                }
            )
            edges.append(
                _edge(
                    previous_line_id or node_id,
                    line_node_id,
                    "precedes" if previous_line_id else "contains_line",
                    authority_source="contract_runtime.completed_lines",
                    evidence_ref=_text(line.get("source_ref")),
                )
            )
            previous_line_id = line_node_id

    for raw_edge in chain_edges:
        parent = _text(raw_edge.get("parent_contract_execution_id"))
        child = _text(raw_edge.get("child_contract_execution_id"))
        if not parent or not child:
            continue
        edges.append(
            _edge(
                f"contract-execution:{parent}",
                f"contract-execution:{child}",
                _text(raw_edge.get("edge_kind")) or "contract_successor",
                authority_source="contract_chain_edges",
                evidence_ref=_text(raw_edge.get("source_ref")),
            )
        )

    event_node_ids: set[str] = set()
    for event in event_summaries:
        event_id = _text(event.get("event_id"))
        if not event_id:
            continue
        event_node_id = f"timeline-event:{event_id}"
        event_node_ids.add(event_id)
        nodes.append(
            {
                "id": event_node_id,
                "kind": "timeline_event",
                "label": _text(event.get("event_kind")) or _text(event.get("event_type")),
                "event_id": event.get("event_id"),
                "status": event.get("status"),
                "created_at": event.get("created_at"),
                "authority_source": "task_timeline_events",
            }
        )
    for event in event_summaries:
        event_id = _text(event.get("event_id"))
        parent_event_id = _text(event.get("parent_event_id"))
        if event_id and parent_event_id and parent_event_id in event_node_ids:
            edges.append(
                _edge(
                    f"timeline-event:{parent_event_id}",
                    f"timeline-event:{event_id}",
                    "parent_event",
                    authority_source="task_timeline_events",
                    evidence_ref=f"timeline:{event_id}",
                )
            )

    bypass_records = [line["bypass"] | {
        "contract_execution_id": line.get("contract_execution_id"),
        "stage_id": line.get("stage_id"),
        "line_id": line.get("line_id"),
        "status": line.get("status"),
        "source_ref": line.get("source_ref"),
    } for line in line_states if line.get("bypassed") and isinstance(line.get("bypass"), Mapping)]

    row_status = _text(backlog.get("status"))
    close_state = "closed" if row_status.upper() in {"FIXED", "CLOSED"} else "open"
    projection_source_refs = _safe_refs(current.get("source_refs") or [])
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "public_safe": True,
        "read_only": True,
        "project_id": _text(project_id),
        "backlog_id": backlog_id,
        "generated_at": _text(generated_at),
        "authority": {
            "schema_version": "contract_runtime.visualization.authority.v1",
            "source_order": [
                "contract_runtime_current",
                "backlog_contract_chain_current",
                "task_timeline_compact_ledger",
                "legacy_contract_state_and_timeline_gate",
            ],
            "source_of_authority": "contract_runtime",
            "authority_decision_source": "backlog_contract_chain_current",
            "axes": [
                "contract_execution_progress",
                "backlog_close_readiness",
                "historical_diagnostics",
            ],
            "legacy_sources_advisory_only": True,
        },
        "backlog": {
            "backlog_id": backlog_id,
            "title": _text(backlog.get("title")),
            "status": row_status,
            "priority": _text(backlog.get("priority")),
            "commit": _text(backlog.get("commit")),
            "updated_at": _text(backlog.get("updated_at")),
        },
        "contract_execution_progress": {
            **runtime_current,
            "line_states": line_states,
            "line_state_count": len(line_states),
            "line_state_total": line_state_total,
            "line_states_truncated": line_state_total > len(line_states),
            "runtime_record_count": len(records),
            "runtime_record_total": runtime_record_total,
            "runtime_records_truncated": runtime_record_total > len(records),
        },
        "backlog_close_readiness": {
            "state": close_state,
            "backlog_status": row_status,
            "contract_execution_state": _text(runtime_current.get("readiness_state")),
            "contract_complete_implies_backlog_close": False,
            "legacy_advisory_count": len(advisories),
        },
        "contract_chain": {
            "contract_chain_id": _text(current.get("contract_chain_id")),
            "root_contract_execution_id": _text(
                current.get("root_contract_execution_id")
            ),
            "current_contract_execution_id": current_execution_id,
            "current_contract_id": _text(current.get("current_contract_id")),
            "parent_to_resume_contract_execution_id": _text(
                current.get("parent_to_resume_contract_execution_id")
            ),
            "active_child_contract_execution_id": _text(
                current.get("active_child_contract_execution_id")
            ),
            "readiness_state": _text(current.get("readiness_state")),
            "next_legal_action": chain_next_action,
            "degraded": bool(current.get("degraded")),
            "source_refs": projection_source_refs,
        },
        "timeline": {
            "events": event_summaries,
            "returned_count": len(event_summaries),
            "total_count": (
                int(timeline_total)
                if timeline_total is not None
                else len(event_summaries)
            ),
            "limit": max(1, int(timeline_limit or 100)),
            "truncated": bool(timeline_has_more),
            "next_cursor": _text(next_cursor) if timeline_has_more else "",
            "next_cursor_parameter": "before_event_id" if timeline_has_more else "",
            "append_only": True,
            "current_snapshot_in_playback": False,
        },
        "dag": {
            "schema_version": "contract_runtime.visualization.dag.v1",
            "nodes": nodes,
            "edges": edges,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "typed_edges": True,
        },
        "compact_ledger": {
            "backlog_id": _text(ledger.get("backlog_id")),
            "contract_execution_id": _text(ledger.get("contract_execution_id")),
            "readiness_state": _text(ledger.get("readiness_state")),
            "projection_generation": _int(ledger.get("projection_generation")),
            "projection_watermark": _int(ledger.get("projection_watermark")),
            "projection_hash": _text(ledger.get("projection_hash")),
            "projection_updated_at": _text(ledger.get("projection_updated_at")),
            "advisory_only_when_stale": True,
        },
        "bypass_records": bypass_records,
        "legacy_advisories": advisories,
        "projection_freshness": {
            "status": (
                "missing"
                if not current
                else "degraded"
                if current.get("degraded")
                else "current"
            ),
            "projection_generation": _int(current.get("generation")),
            "projection_watermark": _int(current.get("projection_watermark")),
            "projection_hash": _text(current.get("projection_hash")),
            "updated_at": _text(current.get("updated_at")),
            "source_refs": projection_source_refs,
        },
        "projection_conflicts": conflicts,
        "projection_conflict_count": len(conflicts),
    }
