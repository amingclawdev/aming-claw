"""Replayable observer repair-run planning contract.

This module is intentionally read-only. It creates deterministic recovery plans
for cross-system observer work, but it never mints route tokens or satisfies
protected close-gate evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from typing import Any


SCHEMA_VERSION = "observer_repair_run_plan.v1"
ROUTE_CONTEXT_SCHEMA_VERSION = "observer_repair_route_context.v1"

CHECKPOINTS = [
    "diagnosed",
    "route_context_ready",
    "dispatch_ready",
    "worker_started",
    "implementation_done",
    "verification_done",
    "graph_reconcile_done",
    "close_precheck_passed",
    "closed",
]

LANE_PRIORITY = {
    "runtime_schema": 10,
    "route_context": 20,
    "graph_reconcile": 30,
    "subsystem_evidence": 40,
    "independent_verification": 50,
    "close_gate": 60,
    "observer_triage": 90,
}

LANE_ACTIONS = {
    "runtime_schema": [
        "compare_current_mcp_schema_to_source",
        "fix_mcp_schema_or_runtime_passthrough",
        "redeploy_or_reload_governance_surfaces",
        "rerun_schema_parity_preflight",
    ],
    "route_context": [
        "request_route_prompt_alert_bundle",
        "run_route_action_precheck",
        "supersede_or_reset_stale_route_identity",
        "retry_protected_action_with_matching_route_token_or_valid_waiver",
    ],
    "graph_reconcile": [
        "inspect_graph_status",
        "prefer_direct_scope_reconcile_with_activation",
        "fall_back_to_full_reconcile_on_rule_fingerprint_change",
        "rerun_graph_status_until_current",
    ],
    "subsystem_evidence": [
        "dispatch_bounded_implementation_lane",
        "record_worker_startup_evidence",
        "append_implementation_evidence_after_worker_result",
    ],
    "independent_verification": [
        "dispatch_independent_verification_lane",
        "run_focused_tests_or_e2e",
        "append_verification_evidence_after_results_pass",
    ],
    "close_gate": [
        "run_mf_timeline_precheck",
        "append_close_ready_only_after_required_evidence_passes",
        "retry_backlog_close_with_matching_route_token_or_valid_waiver",
    ],
    "observer_triage": [
        "group_blockers_by_recovery_class",
        "produce_next_legal_actions",
    ],
}

LANE_BLOCKED_ACTIONS = [
    "close_without_mf_timeline_precheck",
    "protected_write_without_route_token_or_valid_waiver",
    "use_judgment_brain_as_execution_dependency",
    "count_diagnostic_alert_as_route_or_close_evidence",
    "dispatch_worker_without_file_fence",
]

BLOCKER_RULES = [
    (
        "route_token_required",
        ("route_token_required", "route-token", "route token"),
        "route_context",
        "return_to_route_context_and_request_valid_route_token",
    ),
    (
        "schema_mismatch",
        ("schema mismatch", "schema gap", "schema not", "does not expose", "does not consume"),
        "runtime_schema",
        "fix_mcp_schema_runtime_parity",
    ),
    (
        "graph_stale",
        ("graph stale", "active_graph_stale", "pending scope", "pending-scope", "scope reconcile"),
        "graph_reconcile",
        "run_graph_reconcile_or_actionable_fallback",
    ),
    (
        "pending_scope_timeout",
        ("timeout", "timed out"),
        "graph_reconcile",
        "replace_queue_wait_with_bounded_reconcile_fallback",
    ),
    (
        "missing_verification",
        ("missing verification", "independent_verification", "independent verification"),
        "independent_verification",
        "dispatch_independent_verification_lane",
    ),
    (
        "missing_timeline_evidence",
        ("implementation", "verification", "close_ready", "missing_event_kinds"),
        "subsystem_evidence",
        "append_required_timeline_evidence_after_real_work",
    ),
    (
        "route_identity_mismatch",
        ("route_identity_mismatch", "identity mismatch", "stale route"),
        "route_context",
        "supersede_or_reset_stale_route_identity_before_retry",
    ),
]


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


def _stable_hash(payload: Any, *, length: int = 16) -> str:
    raw = json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _string(value: Any) -> str:
    return str(value or "").strip()


def _object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _row_text(row: Mapping[str, Any]) -> str:
    parts = [
        row.get("bug_id"),
        row.get("title"),
        row.get("details_md"),
        row.get("target_files"),
        row.get("acceptance_criteria"),
        row.get("required_docs"),
    ]
    return " ".join(str(part or "") for part in parts).lower()


def classify_text(text: str) -> dict[str, Any]:
    lowered = str(text or "").lower()
    matches: list[dict[str, str]] = []
    for blocker_id, needles, lane, action in BLOCKER_RULES:
        if any(needle in lowered for needle in needles):
            matches.append(
                {
                    "blocker_id": blocker_id,
                    "lane_id": lane,
                    "recovery_action": action,
                }
            )
    if not matches:
        matches.append(
            {
                "blocker_id": "unknown",
                "lane_id": "observer_triage",
                "recovery_action": "inspect_evidence_and_file_bounded_followup",
            }
        )
    return {
        "input": str(text or ""),
        "matches": matches,
    }


def _classify_backlog_row(row: Mapping[str, Any]) -> dict[str, Any]:
    classification = classify_text(_row_text(row))
    lane_ids = sorted({match["lane_id"] for match in classification["matches"]}, key=lambda lane: LANE_PRIORITY[lane])
    return {
        "bug_id": _string(row.get("bug_id")),
        "status": _string(row.get("status")),
        "priority": _string(row.get("priority")),
        "lane_ids": lane_ids or ["observer_triage"],
        "blocker_ids": sorted({match["blocker_id"] for match in classification["matches"]}),
        "recovery_actions": sorted({match["recovery_action"] for match in classification["matches"]}),
    }


def _extract_declared_dependencies(row: Mapping[str, Any]) -> list[str]:
    contract = _parse_json_object(row.get("chain_trigger_json"))
    candidates: list[Any] = []
    for key in ("depends_on", "related_backlog_ids", "dependencies"):
        candidates.extend(_list(contract.get(key)))
    details = _string(row.get("details_md"))
    for token in details.replace(",", " ").replace("\n", " ").split(" "):
        token = token.strip().strip(".,;:()[]")
        if token.startswith(("AC-", "JB-", "CONTENT-", "MS-")):
            candidates.append(token)
    bug_id = _string(row.get("bug_id"))
    return sorted({str(item).strip() for item in candidates if str(item).strip() and str(item).strip() != bug_id})


def _build_route_context(project_id: str, root_backlog_ids: Sequence[str], seed: Mapping[str, Any]) -> dict[str, Any]:
    base = {
        "project_id": project_id,
        "root_backlog_ids": sorted(root_backlog_ids),
        "seed": _jsonable(seed),
    }
    digest = _stable_hash(base, length=16)
    return {
        "schema_version": ROUTE_CONTEXT_SCHEMA_VERSION,
        "route_id": f"route-repair-{digest}",
        "route_context_hash": f"sha256:{_stable_hash(base, length=64)}",
        "prompt_contract_id": f"rprompt-repair-{digest}",
        "topology": "observer_led_repair_run",
        "owner": "aming-claw",
        "judgment_brain_required": False,
        "read_only": True,
        "authorizes_protected_write": False,
        "allowed_actions": [
            "diagnose_backlog_dependency_dag",
            "create_read_only_repair_run_plan",
            "dispatch_bounded_lanes_after_route_token",
            "run_close_gate_precheck",
        ],
        "blocked_actions": list(LANE_BLOCKED_ACTIONS),
    }


def _build_lanes(classified_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_lane: dict[str, set[str]] = {}
    for row in classified_rows:
        bug_id = _string(row.get("bug_id"))
        for lane in _list(row.get("lane_ids")):
            by_lane.setdefault(str(lane), set()).add(bug_id)
    if not by_lane:
        by_lane["observer_triage"] = set()
    lanes: list[dict[str, Any]] = []
    for lane_id in sorted(by_lane, key=lambda lane: LANE_PRIORITY.get(lane, 999)):
        lanes.append(
            {
                "lane_id": lane_id,
                "role": "observer" if lane_id in {"observer_triage", "close_gate"} else "mf_sub",
                "status": "pending",
                "target_backlog_ids": sorted(item for item in by_lane[lane_id] if item),
                "requires_file_fence": lane_id not in {"observer_triage", "close_gate", "route_context"},
                "requires_route_token_for_write": lane_id != "observer_triage",
                "allowed_actions": LANE_ACTIONS.get(lane_id, LANE_ACTIONS["observer_triage"]),
                "blocked_actions": list(LANE_BLOCKED_ACTIONS),
            }
        )
    if "close_gate" not in by_lane:
        lanes.append(
            {
                "lane_id": "close_gate",
                "role": "observer",
                "status": "pending",
                "target_backlog_ids": sorted(
                    row["bug_id"] for row in classified_rows if _string(row.get("bug_id"))
                ),
                "requires_file_fence": False,
                "requires_route_token_for_write": True,
                "allowed_actions": LANE_ACTIONS["close_gate"],
                "blocked_actions": list(LANE_BLOCKED_ACTIONS),
            }
        )
    return lanes


def _build_dependency_dag(classified_rows: Sequence[Mapping[str, Any]], rows_by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    edges: set[tuple[str, str, str]] = set()
    for classified in classified_rows:
        bug_id = _string(classified.get("bug_id"))
        if not bug_id:
            continue
        lane_ids = _list(classified.get("lane_ids")) or ["observer_triage"]
        primary_lane = str(lane_ids[0])
        nodes.append(
            {
                "id": bug_id,
                "kind": "backlog",
                "lane_id": primary_lane,
                "status": _string(classified.get("status")),
                "priority": _string(classified.get("priority")),
                "blocker_ids": _list(classified.get("blocker_ids")),
                "recovery_actions": _list(classified.get("recovery_actions")),
            }
        )
        row = rows_by_id.get(bug_id, {})
        for dep in _extract_declared_dependencies(row):
            edges.add((dep, bug_id, "declared_dependency"))
        if "runtime_schema" not in lane_ids and any(l in lane_ids for l in ("route_context", "subsystem_evidence", "close_gate")):
            schema_nodes = [
                other["bug_id"]
                for other in classified_rows
                if "runtime_schema" in _list(other.get("lane_ids"))
            ]
            for schema_node in schema_nodes:
                edges.add((schema_node, bug_id, "schema_before_protected_write"))
        if "close_gate" in lane_ids:
            for other in classified_rows:
                other_id = _string(other.get("bug_id"))
                if other_id and other_id != bug_id:
                    edges.add((other_id, bug_id, "evidence_before_close"))
    return {
        "nodes": sorted(nodes, key=lambda node: (LANE_PRIORITY.get(node["lane_id"], 999), node["id"])),
        "edges": [
            {"from": src, "to": dst, "reason": reason}
            for src, dst, reason in sorted(edges)
        ],
    }


def _build_checkpoints(route_context: Mapping[str, Any], lanes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "checkpoint_id": checkpoint,
            "status": "passed" if checkpoint == "diagnosed" else "pending",
            "route_context_hash": route_context.get("route_context_hash", ""),
            "requires_evidence": checkpoint not in {"diagnosed", "route_context_ready"},
            "lane_ids": [str(lane.get("lane_id")) for lane in lanes],
        }
        for checkpoint in CHECKPOINTS
    ]


def build_repair_run_plan(
    *,
    project_id: str,
    root_backlog_ids: Sequence[str],
    backlog_rows: Sequence[Mapping[str, Any]] = (),
    blockers: Sequence[Any] = (),
    graph_status: Mapping[str, Any] | None = None,
    operations_queue: Mapping[str, Any] | None = None,
    timeline_prechecks: Sequence[Mapping[str, Any]] = (),
    route_context_seed: Mapping[str, Any] | None = None,
    actor: str = "observer",
) -> dict[str, Any]:
    """Build a deterministic, replayable observer repair-run plan."""

    project = _string(project_id)
    roots = sorted({_string(item) for item in root_backlog_ids if _string(item)})
    normalized_rows = [dict(row) for row in backlog_rows if isinstance(row, Mapping)]
    rows_by_id = {_string(row.get("bug_id")): row for row in normalized_rows if _string(row.get("bug_id"))}
    classified_rows = [_classify_backlog_row(row) for row in normalized_rows]
    blocker_inputs = [str(item.get("error") or item.get("message") or item) if isinstance(item, Mapping) else str(item) for item in blockers]
    blocker_classes = [classify_text(item) for item in blocker_inputs if item.strip()]
    synthetic_rows: list[dict[str, Any]] = []
    if not classified_rows and blocker_classes:
        for idx, classification in enumerate(blocker_classes, start=1):
            synthetic_rows.append(
                {
                    "bug_id": f"blocker:{idx}",
                    "status": "blocked",
                    "priority": "P0",
                    "lane_ids": sorted({match["lane_id"] for match in classification["matches"]}, key=lambda lane: LANE_PRIORITY[lane]),
                    "blocker_ids": sorted({match["blocker_id"] for match in classification["matches"]}),
                    "recovery_actions": sorted({match["recovery_action"] for match in classification["matches"]}),
                }
            )
        classified_rows = synthetic_rows
    seed = _object(route_context_seed)
    route_context = _build_route_context(
        project,
        roots,
        {
            **seed,
            "actor": actor,
            "backlog_count": len(normalized_rows),
            "blocker_count": len(blocker_classes),
        },
    )
    lanes = _build_lanes(classified_rows)
    dag = _build_dependency_dag(classified_rows, rows_by_id)
    repair_run_id = f"repair-{_stable_hash({'project_id': project, 'roots': roots, 'route': route_context})}"
    recovery_actions = sorted(
        {
            action
            for row in classified_rows
            for action in _list(row.get("recovery_actions"))
        }
        | {
            match["recovery_action"]
            for classification in blocker_classes
            for match in classification["matches"]
        }
    )
    graph_stale = bool(
        _object(_object(graph_status).get("current_state"))
        .get("graph_stale", {})
        .get("is_stale", False)
    )
    operation_count = int(_object(operations_queue).get("count") or 0)
    timeline_blocked = [
        {
            "bug_id": _string(item.get("bug_id")),
            "can_close": bool(item.get("can_close")),
            "missing": _object(item.get("timeline_gate")).get("missing_event_kinds", []),
        }
        for item in timeline_prechecks
        if isinstance(item, Mapping) and item.get("can_close") is False
    ]
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "repair_run_id": repair_run_id,
        "project_id": project,
        "actor": actor,
        "root_backlog_ids": roots,
        "route_context": route_context,
        "backlog_dependency_dag": dag,
        "lane_dispatches": lanes,
        "checkpoints": _build_checkpoints(route_context, lanes),
        "blocker_classification": blocker_classes,
        "recovery_actions": recovery_actions,
        "runtime_independent_of_judgment_brain": True,
        "protected_write_policy": {
            "plan_is_read_only": True,
            "requires_route_token_for_protected_writes": True,
            "diagnostic_events_count_as_close_evidence": False,
        },
        "graph_summary": {
            "graph_stale": graph_stale,
            "operation_count": operation_count,
        },
        "timeline_precheck_summary": {
            "blocked_count": len(timeline_blocked),
            "blocked": timeline_blocked,
        },
        "next_legal_actions": recovery_actions or ["inspect_evidence_and_file_bounded_followup"],
    }
