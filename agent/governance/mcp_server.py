"""MCP (Model Context Protocol) server for the governance service.

Implements JSON-RPC 2.0 over stdio transport (per MCP spec).

Capabilities:
  - initialize / initialized handshake
  - tools/list  → returns registered governance tools
  - tools/call  → dispatches to governance API
  - Subscribes to Redis Pub/Sub and forwards events as MCP notifications

Usage:
    python -m agent.governance.mcp_server
  or
    python agent/governance/mcp_server.py

Environment variables:
    REDIS_URL          Redis connection URL (default: redis://localhost:6379/0)
    GOVERNANCE_URL     Governance HTTP base URL (default: http://localhost:40000)
    GOV_TOKEN          Bearer token for governance API calls
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Ensure the agent package root is on sys.path so relative imports work when
# the file is executed directly (python mcp_server.py).
# ---------------------------------------------------------------------------
_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

log = logging.getLogger(__name__)

_RECONCILE_MCP_TIMEOUT_DEFAULT_SECONDS = 900
_RECONCILE_MCP_TIMEOUT_MAX_SECONDS = 8 * 60 * 60
_RECONCILE_MCP_TIMEOUT_ENV_KEYS = (
    "AMING_GRAPH_RECONCILE_MCP_TIMEOUT_SECONDS",
    "AMING_RECONCILE_MCP_TIMEOUT_SECONDS",
)
_RECONCILE_PROGRESS_POLL_TIMEOUT_SECONDS = 10


def _int_arg(args: dict, key: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(args.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _positive_timeout_seconds(value: Any, default: int) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return min(parsed, _RECONCILE_MCP_TIMEOUT_MAX_SECONDS)


def _reconcile_mcp_timeout_seconds(args: dict) -> int:
    if args.get("timeout_seconds") is not None:
        return _positive_timeout_seconds(
            args.get("timeout_seconds"),
            _RECONCILE_MCP_TIMEOUT_DEFAULT_SECONDS,
        )
    for key in _RECONCILE_MCP_TIMEOUT_ENV_KEYS:
        if os.environ.get(key):
            return _positive_timeout_seconds(
                os.environ.get(key),
                _RECONCILE_MCP_TIMEOUT_DEFAULT_SECONDS,
            )
    return _RECONCILE_MCP_TIMEOUT_DEFAULT_SECONDS


def _is_timeout_result(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    text = " ".join(
        str(result.get(key) or "")
        for key in ("error", "message", "reason")
    ).lower()
    return (
        str(result.get("error") or "") in {"request_timeout", "timeout", "timed_out"}
        or "timed out" in text
        or "timeout" in text
    )


def _summarize_reconcile_progress(queue: Any, run_id: str) -> dict:
    if not isinstance(queue, dict) or queue.get("error"):
        return {
            "available": False,
            "status": "unknown",
            "progress": {},
            "error": str(queue.get("error") if isinstance(queue, dict) else queue),
        }

    operations = queue.get("operations") if isinstance(queue.get("operations"), list) else []
    matched_operation = None
    if run_id:
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            haystack = " ".join(
                str(operation.get(key) or "")
                for key in ("operation_id", "target_id", "last_result", "run_id")
            )
            if run_id in haystack:
                matched_operation = operation
                break
    if matched_operation is None and len(operations) == 1:
        matched_operation = operations[0] if isinstance(operations[0], dict) else None

    if matched_operation:
        return {
            "available": True,
            "status": str(matched_operation.get("status") or "unknown"),
            "progress": matched_operation.get("progress") or {},
            "operation_id": matched_operation.get("operation_id") or "",
            "operation_type": matched_operation.get("operation_type") or "",
            "last_result": matched_operation.get("last_result") or "",
        }

    summary = queue.get("summary") if isinstance(queue.get("summary"), dict) else {}
    graph_stale = summary.get("graph_stale") if isinstance(summary.get("graph_stale"), dict) else {}
    metrics = summary.get("reconcile_metrics") if isinstance(summary.get("reconcile_metrics"), dict) else {}
    latest = (
        metrics.get("latest_full_rebuild_fallback")
        if isinstance(metrics.get("latest_full_rebuild_fallback"), dict)
        else {}
    )
    status = "unknown"
    progress: dict[str, Any] = {}
    if latest and (not run_id or latest.get("run_id") == run_id):
        status = str(latest.get("status") or "observed")
        progress = {"elapsed_ms": latest.get("elapsed_ms")}
    elif graph_stale:
        status = "graph_current" if graph_stale.get("is_stale") is False else "graph_stale"

    return {
        "available": bool(summary or graph_stale or latest),
        "status": status,
        "progress": progress,
        "operation_count": queue.get("count", 0),
        "active_snapshot_id": queue.get("active_snapshot_id") or queue.get("snapshot_id") or "",
        "graph_stale": graph_stale,
    }


def _current_full_reconcile_run_id(body: dict) -> str:
    explicit = str(body.get("run_id") or "").strip()
    if explicit:
        return explicit
    target = str(body.get("target_commit_sha") or body.get("commit_sha") or "").strip()
    if target:
        return f"current-full-{target[:7]}"
    return ""


def _current_full_reconcile_timeout_response(
    project_id: str,
    body: dict,
    *,
    timeout_seconds: int,
    timeout_result: dict,
    progress: dict,
) -> dict:
    run_id = _current_full_reconcile_run_id(body)
    return {
        "ok": False,
        "error": "reconcile_timeout",
        "project_id": project_id,
        "run_id": run_id,
        "run_id_available": bool(run_id),
        "timeout_seconds": timeout_seconds,
        "status": progress.get("status") or "unknown",
        "progress": progress.get("progress") or {},
        "reconcile_progress": progress,
        "timeout_error": timeout_result.get("error") or timeout_result.get("message") or "",
        "message": (
            "graph_current_full_reconcile exceeded its bounded MCP reconcile "
            "timeout. The reconcile may still be running; poll "
            "graph_operations_queue or graph_status before deciding whether to retry."
        ),
        "next_legal_action": {
            "action": "poll_graph_operations_queue_or_retry_safely",
            "poll_tool": "graph_operations_queue",
            "status_tool": "graph_status",
            "retry_tool": "graph_current_full_reconcile",
            "safe_retry": True,
            "retry_guidance": (
                "Poll graph_operations_queue for the run/progress first. Retry only "
                "after no running reconcile is reported, preferably with the same "
                "run_id when one was supplied."
            ),
        },
    }


def _current_full_reconcile_route_token_alias_error(
    observer_route_token_ref: str,
    route_token_ref: str,
) -> dict:
    return {
        "ok": False,
        "error": "route_token_ref_alias_conflict",
        "code": "route_token_ref_alias_conflict",
        "message": (
            "graph_current_full_reconcile received conflicting route-token "
            "alias values; pass only one of observer_route_token_ref or "
            "route_token_ref, or pass the same value for both."
        ),
        "aliases": {
            "observer_route_token_ref": "route_token_ref",
            "route_token_ref": "observer_route_token_ref",
        },
        "observer_route_token_ref": observer_route_token_ref,
        "route_token_ref": route_token_ref,
        "raw_route_token_required": False,
        "raw_route_token_exposed": False,
    }


def _normalize_current_full_reconcile_route_token_aliases(
    body: dict,
) -> tuple[dict, dict | None]:
    normalized = dict(body)
    observer_route_token_ref = str(
        normalized.get("observer_route_token_ref") or ""
    ).strip()
    route_token_ref = str(normalized.get("route_token_ref") or "").strip()
    if observer_route_token_ref and route_token_ref:
        if observer_route_token_ref != route_token_ref:
            return normalized, _current_full_reconcile_route_token_alias_error(
                observer_route_token_ref,
                route_token_ref,
            )
        normalized["observer_route_token_ref"] = observer_route_token_ref
        normalized.pop("route_token_ref", None)
    elif route_token_ref:
        normalized["observer_route_token_ref"] = route_token_ref
        normalized.pop("route_token_ref", None)
    elif observer_route_token_ref:
        normalized["observer_route_token_ref"] = observer_route_token_ref
        normalized.pop("route_token_ref", None)
    return normalized, None


def _backlog_list_query(args: dict) -> dict:
    query: dict[str, Any] = {
        "view": str(args.get("view") or "compact"),
        "limit": _int_arg(args, "limit", 50, minimum=1, maximum=100),
        "offset": _int_arg(args, "offset", 0, minimum=0, maximum=1_000_000),
    }
    if args.get("priority"):
        query["priority"] = args["priority"]
    if args.get("q"):
        query["q"] = args["q"]
    if args.get("status"):
        query["status"] = args["status"]
    elif "include_closed" in args:
        query["include_closed"] = "true" if args.get("include_closed") else "false"
    else:
        query["status"] = "OPEN"
    return query


def _task_timeline_query(args: dict) -> dict:
    query: dict[str, Any] = {}
    for key in (
        "task_id",
        "backlog_id",
        "trace_id",
        "phase",
        "event_kind",
        "scenario_id",
        "correlation_id",
        "severity",
        "decision",
    ):
        if args.get(key):
            query[key] = str(args[key])
    if args.get("parent_event_id"):
        query["parent_event_id"] = str(_int_arg(args, "parent_event_id", 0, minimum=1, maximum=1_000_000_000))
    if args.get("limit"):
        query["limit"] = str(_int_arg(args, "limit", 200, minimum=1, maximum=1000))
    return query


def _task_timeline_body(args: dict) -> dict:
    allowed = {
        "task_id",
        "backlog_id",
        "mf_id",
        "attempt_num",
        "event_type",
        "phase",
        "event_kind",
        "scenario_id",
        "parent_event_id",
        "correlation_id",
        "severity",
        "decision",
        "schema_version",
        "actor",
        "status",
        "payload",
        "verification",
        "artifact_refs",
        "trace_id",
        "commit_sha",
        "route_token",
        "route_token_ref",
        "route_waiver",
        "route_token_waiver",
    }
    return {key: args[key] for key in allowed if key in args and args[key] is not None}


_RUNTIME_CONTEXT_QUERY_FIELDS = (
    "fence_token",
    "parent_task_id",
    "view",
    "graph_trace_id",
    "session_token",
    "session_token_ref",
    "target_project_root",
)


def _runtime_context_query(args: dict) -> dict:
    return {
        key: str(args[key])
        for key in _RUNTIME_CONTEXT_QUERY_FIELDS
        if args.get(key)
    }


def _runtime_context_schema_properties() -> dict[str, Any]:
    return {
        "project_id": {"type": "string", "description": "Project identifier."},
        "runtime_context_id": {
            "type": "string",
            "description": "Runtime context id, e.g. mfrctx-...",
        },
        "fence_token": {
            "type": "string",
            "description": "Required for mf_sub role-filtered worker lookup.",
        },
        "parent_task_id": {
            "type": "string",
            "description": "Parent observer/MF task id for worker fence validation.",
        },
        "view": {
            "type": "string",
            "enum": ["auto", "current", "gate_inputs", "worker_view", "close_gate_view", "all"],
            "description": "Observer view selector. mf_sub callers always receive worker_view.",
        },
        "graph_trace_id": {
            "type": "string",
            "description": "Optional graph trace id fallback when no trace row is persisted.",
        },
        "session_token": {
            "type": "string",
            "description": "Scoped worker session token issued at allocation.",
        },
        "session_token_ref": {
            "type": "string",
            "description": "Opaque scoped worker session-token reference.",
        },
        "target_project_root": {
            "type": "string",
            "description": "Target project root used to validate worker route identity.",
        },
    }


def _runtime_context_write_schema_properties() -> dict[str, Any]:
    properties = dict(_runtime_context_schema_properties())
    properties.update(
        {
            "task_id": {"type": "string"},
            "worker_session_id": {"type": "string"},
            "worker_id": {"type": "string"},
            "worker_slot_id": {"type": "string"},
            "filer_principal": {"type": "string"},
            "worker_transcript_ref": {"type": "string"},
            "worker_transcript_path": {"type": "string"},
            "harness_type": {"type": "string"},
            "launch_text_hash": {"type": "string"},
            "receipt_hash": {"type": "string"},
            "context_hash": {"type": "string"},
            "contract_hash": {"type": "string"},
            "acknowledged_at": {"type": "string"},
            "actor_role": {"type": "string"},
            "actor_session_principal": {"type": "string"},
            "evidence_owner_actor": {"type": "string"},
            "evidence_owner_role": {"type": "string"},
            "evidence_owner_session": {"type": "string"},
            "evidence_owner_session_ref": {"type": "string"},
            "submitter_session": {"type": "string"},
            "submitter_principal": {"type": "string"},
            "materialized_from": {"type": "string"},
            "materialized_from_report": {"type": "string"},
            "authorization_source": {"type": "string"},
            "observer_impersonation": {"type": "boolean"},
            "qa_session_token_ref": {"type": "string"},
            "parent_materialization_authorized": {"type": "boolean"},
            "qa_evidence_provenance": {"type": "object"},
            "contract_context_read_receipt": {"type": "object"},
            "checkpoint_id": {"type": "string"},
            "head_commit": {"type": "string"},
            "changed_files": {"type": "array", "items": {"type": "string"}},
            "owned_changed_files": {"type": "array", "items": {"type": "string"}},
            "worker_changed_files": {"type": "array", "items": {"type": "string"}},
            "owned_files": {"type": "array", "items": {"type": "string"}},
            "graph_trace_ids": {"type": "array", "items": {"type": "string"}},
            "graph_query_trace_ids": {"type": "array", "items": {"type": "string"}},
            "read_receipt_event_id": {"type": "string"},
            "read_receipt_hash": {"type": "string"},
            "tests": {"type": "array", "items": {"type": "object"}},
            "test_results": {"type": "object"},
            "finish_time_worker_self_attestation": {"type": "object"},
            "payload": {"type": "object"},
            "verification": {"type": "object"},
            "artifact_refs": {"type": "object"},
            "event_type": {"type": "string"},
            "event_kind": {"type": "string"},
            "phase": {"type": "string"},
            "actor": {"type": "string"},
            "status": {"type": "string"},
            "trace_id": {"type": "string"},
            "commit_sha": {"type": "string"},
            "route_id": {"type": "string"},
            "route_context_hash": {"type": "string"},
            "prompt_contract_id": {"type": "string"},
            "prompt_contract_hash": {"type": "string"},
            "visible_injection_manifest_hash": {"type": "string"},
            "route_token_ref": {"type": "string"},
            "route_token": {"type": "object"},
            "route_waiver": {"type": "object"},
            "reason": {"type": "string"},
            "join_reason": {"type": "string"},
            "rejoin_reason": {"type": "string"},
            "ttl_seconds": {"type": "integer"},
            "now_iso": {"type": "string"},
        }
    )
    return properties


def _contract_runtime_submit_line_schema_properties() -> dict[str, Any]:
    properties: dict[str, Any] = {
        "project_id": {"type": "string"},
        "backlog_id": {"type": "string"},
        "contract_execution_id": {"type": "string"},
        "definition_hash": {"type": "string"},
        "instruction_bundle_hash": {"type": "string"},
        "execution_state_revision": {"type": "integer"},
        "runtime_guide_hash": {"type": "string"},
        "stage_id": {"type": "string"},
        "line_id": {"type": "string"},
        "evidence_kind": {"type": "string"},
        "payload": {"type": "object"},
        "artifact_refs": {"type": "object"},
        "trace_id": {"type": "string"},
        "commit_sha": {"type": "string"},
        "observer_route_token_ref": {
            "type": "string",
            "description": "Opaque observer route-token ref; raw route tokens are not accepted.",
        },
        "route_token_ref": {"type": "string"},
        "observer_session_id": {
            "type": "string",
            "description": "Opaque active observer session id used with observer_route_token_ref.",
        },
        "qa_session_token": {
            "type": "string",
            "description": "Raw QA role token used only as X-Gov-Token; never forwarded as evidence body.",
        },
        "worker_role": {
            "type": "string",
            "description": "Use mf_sub for fenced worker-authored lines.",
        },
    }
    for key, value in _runtime_context_write_schema_properties().items():
        properties.setdefault(key, value)
    return properties


def _parallel_branch_allocate_schema_properties() -> dict[str, Any]:
    route_identity_properties = {
        "route_id": {"type": "string"},
        "route_context_hash": {"type": "string"},
        "prompt_contract_id": {"type": "string"},
        "prompt_contract_hash": {"type": "string"},
        "route_token_ref": {"type": "string"},
        "visible_injection_manifest_hash": {"type": "string"},
    }
    return {
        "project_id": {"type": "string"},
        "task_id": {"type": "string"},
        "workspace_root": {"type": "string"},
        "repo_root_path": {"type": "string"},
        "batch_id": {"type": "string"},
        "backlog_id": {"type": "string"},
        "chain_id": {"type": "string"},
        "parent_task_id": {"type": "string"},
        "root_task_id": {"type": "string"},
        "stage_task_id": {"type": "string"},
        "stage_type": {"type": "string"},
        "agent_id": {"type": "string"},
        "worker_id": {"type": "string"},
        "actor": {"type": "string"},
        "attempt": {"type": "integer"},
        "branch_prefix": {"type": "string"},
        "worktree_root": {"type": "string"},
        "worktree_path": {
            "type": "string",
            "description": (
                "Final absolute worker worktree path; use when the path "
                "is already fully allocated."
            ),
        },
        "worker_worktree_path": {
            "type": "string",
            "description": "Alias for worktree_path.",
        },
        "assigned_worktree": {
            "type": "string",
            "description": "Alias for worktree_path.",
        },
        "ref_name": {"type": "string"},
        "target_branch": {"type": "string"},
        "base_commit": {"type": "string"},
        "target_head_commit": {"type": "string"},
        "merge_queue_id": {"type": "string"},
        "fence_token": {"type": "string"},
        "observer_command_id": {
            "type": "string",
            "description": (
                "Claimed backlog-specific execute_backlog_row command id "
                "used for bounded-worker dispatch/startup lineage."
            ),
        },
        "route_id": {"type": "string"},
        "route_context_hash": {"type": "string"},
        "prompt_contract_id": {"type": "string"},
        "prompt_contract_hash": {"type": "string"},
        "visible_injection_manifest_hash": {"type": "string"},
        "owned_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Dispatch-visible worker-owned file fence.",
        },
        "target_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Alias for dispatch-visible owned_files scope.",
        },
        "route_identity": {
            "type": "object",
            "description": (
                "Optional public-safe route identity object; top-level route "
                "identity fields are also accepted."
            ),
            "properties": route_identity_properties,
        },
        "canonical_route_identity": {
            "type": "object",
            "description": "Alias container for the canonical public route identity.",
            "properties": route_identity_properties,
        },
        "parent_route_identity": {
            "type": "object",
            "description": (
                "Optional public-safe parent route identity used to bind child "
                "worker route lineage."
            ),
            "properties": route_identity_properties,
        },
        "parent_route_lineage": {
            "type": "object",
            "description": (
                "Public-safe parent route lineage preserved from the observer "
                "dispatch route. Used with child_route_lineage to bind the "
                "worker allocation to its route parent."
            ),
        },
        "child_route_lineage": {
            "type": "object",
            "description": (
                "Public-safe child route lineage for the allocated worker lane, "
                "including task/route lineage and optional merge_queue_id."
            ),
        },
        "route_lineage": {
            "type": "object",
            "description": (
                "Combined public-safe parent/child route lineage envelope. "
                "Callers may pass this alongside parent_route_lineage and "
                "child_route_lineage."
            ),
        },
        "issue_same_owner_session_token": {
            "type": "boolean",
            "description": (
                "When agent_id == allocation_owner, issue a scoped worker "
                "session_token and persist only its hash."
            ),
        },
        "create_worktree": {"type": "boolean"},
        "now_iso": {"type": "string"},
        "route_token": {
            "type": "object",
            "description": "Route-token evidence required when governance protects this mutation.",
        },
        "route_token_ref": {
            "type": "string",
            "description": "Opaque server-registered route token reference accepted by protected HTTP facades.",
        },
        "route_waiver": {
            "type": "object",
            "description": "Explicit waiver for protected route-token gates.",
        },
        "route_token_waiver": {
            "type": "object",
            "description": "Alias for route_waiver.",
        },
    }


_MERGE_QUEUE_FLOW_VALUES = [
    "direct_fix",
    "hotfix",
    "mf_parallel",
    "mf_batch_parallel",
]


def _merge_queue_query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(item) for item in value if str(item or "").strip())
    return str(value)


def _parallel_branch_merge_queue_status_schema_properties() -> dict[str, Any]:
    return {
        "project_id": {"type": "string"},
        "flow": {
            "type": "string",
            "enum": _MERGE_QUEUE_FLOW_VALUES,
            "description": "Client hint; merge_queue_id and persisted queue rows are authoritative.",
        },
        "merge_queue_id": {"type": "string"},
        "batch_id": {"type": "string"},
        "target_ref": {"type": "string"},
        "current_target_head": {"type": "string"},
        "latest_target_head": {
            "type": "string",
            "description": "Alias for current_target_head.",
        },
        "limit": {"type": "integer"},
        "scenario_id": {"type": "string"},
        "severe_integration_failure": {"type": "boolean"},
        "corrected_replay_order": {
            "type": "array",
            "items": {"type": "string"},
        },
    }


def _parallel_branch_merge_queue_apply_schema_properties() -> dict[str, Any]:
    return {
        "project_id": {"type": "string"},
        "flow": {
            "type": "string",
            "enum": _MERGE_QUEUE_FLOW_VALUES,
            "description": "Client hint for direct-fix, hotfix, mf_parallel, or mf_batch_parallel callers.",
        },
        "merge_queue_id": {"type": "string"},
        "queue_item_id": {"type": "string"},
        "task_id": {"type": "string"},
        "backlog_id": {"type": "string"},
        "branch_ref": {
            "type": "string",
            "description": "Optional explicit branch ref; server derives it from the branch lane when omitted.",
        },
        "repo_root_path": {"type": "string"},
        "workspace_root": {"type": "string"},
        "target_ref": {"type": "string"},
        "evidence": {"type": "object"},
        "runtime_context_id": {"type": "string"},
        "parent_task_id": {"type": "string"},
        "checkpoint_id": {
            "type": "string",
            "description": "Worker finish-gate checkpoint id copied into merge_gate_evidence.",
        },
        "finish_gate_ref": {
            "type": "string",
            "description": "Accepted runtime_context.finish_gate timeline ref for merge gate evidence.",
        },
        "verification_event_refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Independent QA verification event refs required before live merge.",
        },
        "graph_trace_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Worker-owned graph query trace ids used by the finish gate.",
        },
        "route_action_precheck_event_ref": {"type": "string"},
        "close_ready_event_ref": {"type": "string"},
        "qa_evidence": {
            "type": "object",
            "description": "Copied into evidence.qa_evidence for live merge evidence records.",
        },
        "batch_status": {"type": "string"},
        "dry_run": {"type": "boolean", "description": "Defaults to true."},
        "allow_target_ref_mutation": {"type": "boolean"},
        "message": {"type": "string"},
        "bug_id": {"type": "string"},
        "source_contract_execution_id": {"type": "string"},
        "source_contract_id": {"type": "string"},
        "contract_execution_id": {"type": "string"},
        "active_contract_execution_id": {"type": "string"},
        "fence_token": {"type": "string"},
        "route_token": {
            "type": "object",
            "description": "Route-token evidence required for live target-ref mutation.",
        },
        "route_token_ref": {
            "type": "string",
            "description": "Opaque server-registered route token reference.",
        },
        "route_waiver": {"type": "object"},
        "route_token_waiver": {"type": "object"},
        "timeout_seconds": {"type": "integer"},
        "scenario_id": {"type": "string"},
        "now_iso": {"type": "string"},
        "actor": {"type": "string"},
        "contract_actor": {"type": "string"},
    }


def _parallel_branch_merge_queue_materialize_schema_properties() -> dict[str, Any]:
    return {
        "project_id": {"type": "string"},
        "flow": {
            "type": "string",
            "enum": _MERGE_QUEUE_FLOW_VALUES,
            "description": "Client hint for direct-fix, hotfix, mf_parallel, or mf_batch_parallel callers.",
        },
        "merge_queue_id": {"type": "string"},
        "queue_item_id": {"type": "string"},
        "queue_index": {"type": "integer"},
        "task_id": {"type": "string"},
        "backlog_id": {"type": "string"},
        "target_ref": {"type": "string"},
        "current_target_head": {"type": "string"},
        "latest_target_head": {"type": "string"},
        "validated_target_head": {"type": "string"},
        "validation_attempt": {"type": "integer"},
        "merge_preview_id": {"type": "string"},
        "runtime_context_id": {"type": "string"},
        "parent_task_id": {"type": "string"},
        "checkpoint_id": {
            "type": "string",
            "description": "Worker finish-gate checkpoint id; required when require_finish_gate is true.",
        },
        "finish_gate_ref": {
            "type": "string",
            "description": "Accepted runtime_context.finish_gate timeline ref.",
        },
        "verification_event_refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Independent QA verification event refs required before materialized merge.",
        },
        "graph_trace_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Worker-owned graph query trace ids used by finish evidence.",
        },
        "route_action_precheck_event_ref": {"type": "string"},
        "close_ready_event_ref": {"type": "string"},
        "require_finish_gate": {
            "type": "boolean",
            "description": "Defaults to true when checkpoint_id is provided.",
        },
        "worker_role": {"type": "string"},
        "status": {"type": "string"},
        "fence_token": {"type": "string"},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "hard_depends_on": {"type": "array", "items": {"type": "string"}},
        "serializes_after": {"type": "array", "items": {"type": "string"}},
        "conflicts_with": {"type": "array", "items": {"type": "string"}},
        "same_node_or_file_conflicts": {"type": "array", "items": {"type": "string"}},
        "requires_graph_epoch": {"type": "array", "items": {"type": "string"}},
        "route_token": {
            "type": "object",
            "description": "Route-token evidence required when governance protects this mutation.",
        },
        "route_token_ref": {
            "type": "string",
            "description": "Opaque server-registered route token reference.",
        },
        "route_waiver": {"type": "object"},
        "route_token_waiver": {"type": "object"},
        "scenario_id": {"type": "string"},
        "now_iso": {"type": "string"},
        "actor": {"type": "string"},
        "contract_actor": {"type": "string"},
    }


def _parallel_branch_merge_queue_status_query(args: dict) -> dict[str, str]:
    query: dict[str, str] = {}
    for key in (
        "merge_queue_id",
        "batch_id",
        "target_ref",
        "current_target_head",
        "limit",
        "scenario_id",
        "severe_integration_failure",
        "corrected_replay_order",
    ):
        if key in args and args[key] is not None:
            query[key] = _merge_queue_query_value(args[key])
    latest_target_head = str(args.get("latest_target_head") or "").strip()
    if latest_target_head and not query.get("current_target_head"):
        query["current_target_head"] = latest_target_head
    return query


def _parallel_branch_merge_queue_apply_body(args: dict) -> dict:
    allowed_keys = {
        "merge_queue_id",
        "queue_item_id",
        "task_id",
        "backlog_id",
        "branch_ref",
        "repo_root_path",
        "workspace_root",
        "target_ref",
        "evidence",
        "batch_status",
        "dry_run",
        "allow_target_ref_mutation",
        "message",
        "bug_id",
        "source_contract_execution_id",
        "source_contract_id",
        "contract_execution_id",
        "active_contract_execution_id",
        "fence_token",
        "runtime_context_id",
        "parent_task_id",
        "route_token",
        "route_token_ref",
        "route_waiver",
        "route_token_waiver",
        "timeout_seconds",
        "scenario_id",
        "now_iso",
        "actor",
        "contract_actor",
    }
    merge_evidence_keys = {
        "checkpoint_id",
        "finish_gate_ref",
        "verification_event_refs",
        "graph_trace_ids",
        "route_action_precheck_event_ref",
        "close_ready_event_ref",
    }
    body = {
        key: value
        for key, value in args.items()
        if key in allowed_keys and key not in merge_evidence_keys and value is not None
    }
    if body.get("backlog_id") and not body.get("bug_id"):
        body["bug_id"] = body["backlog_id"]
    merge_gate_evidence = {
        key: args[key]
        for key in merge_evidence_keys
        if key in args and args[key] is not None
    }
    if merge_gate_evidence:
        evidence = body.get("evidence") if isinstance(body.get("evidence"), dict) else {}
        evidence = dict(evidence)
        evidence.setdefault("merge_gate_evidence", merge_gate_evidence)
        body["evidence"] = evidence
    if args.get("qa_evidence") is not None:
        evidence = body.get("evidence") if isinstance(body.get("evidence"), dict) else {}
        evidence = dict(evidence)
        evidence.setdefault("qa_evidence", args["qa_evidence"])
        body["evidence"] = evidence
    return body


def _parallel_branch_merge_queue_materialize_body(args: dict) -> dict:
    allowed_keys = {
        "merge_queue_id",
        "queue_item_id",
        "queue_index",
        "task_id",
        "backlog_id",
        "target_ref",
        "current_target_head",
        "validated_target_head",
        "validation_attempt",
        "merge_preview_id",
        "runtime_context_id",
        "parent_task_id",
        "checkpoint_id",
        "finish_gate_ref",
        "verification_event_refs",
        "graph_trace_ids",
        "route_action_precheck_event_ref",
        "close_ready_event_ref",
        "require_finish_gate",
        "worker_role",
        "status",
        "fence_token",
        "depends_on",
        "hard_depends_on",
        "serializes_after",
        "conflicts_with",
        "same_node_or_file_conflicts",
        "requires_graph_epoch",
        "route_token",
        "route_token_ref",
        "route_waiver",
        "route_token_waiver",
        "scenario_id",
        "now_iso",
        "actor",
        "contract_actor",
    }
    body = {
        key: value
        for key, value in args.items()
        if key in allowed_keys and value is not None
    }
    latest_target_head = str(args.get("latest_target_head") or "").strip()
    if latest_target_head and not body.get("current_target_head"):
        body["current_target_head"] = latest_target_head
    if body.get("backlog_id") and not body.get("bug_id"):
        body["bug_id"] = body["backlog_id"]
    if body.get("checkpoint_id") and "require_finish_gate" not in body:
        body["require_finish_gate"] = True
    return body


def _runtime_context_write_body(args: dict) -> dict:
    return {
        key: value
        for key, value in args.items()
        if key != "project_id" and value is not None
    }


def _onboard_route_guide_body(args: dict) -> dict:
    return {
        key: value
        for key, value in args.items()
        if key != "project_id" and value not in (None, "", [], {})
    }


# ---------------------------------------------------------------------------
# MCP protocol constants
# ---------------------------------------------------------------------------
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "aming-claw-governance"
SERVER_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
TOOLS: list[dict] = [
    {
        "name": "gov_node_list",
        "description": "List all workflow nodes in a project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project identifier.",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "gov_node_status_update",
        "description": "Update the verify status of a workflow node.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "node_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "testing", "t2_pass", "qa_pass", "failed", "waived", "skipped"],
                },
            },
            "required": ["project_id", "node_id", "status"],
        },
    },
    {
        "name": "gov_gate_check",
        "description": "Check whether all gates for a node are satisfied.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "node_id": {"type": "string"},
            },
            "required": ["project_id", "node_id"],
        },
    },
    {
        "name": "gov_memory_write",
        "description": "Append a memory entry (decision, pitfall, workaround…) to the project knowledge base.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "node_id": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["decision", "pitfall", "workaround", "invariant", "ownership", "pattern", "api", "stub"],
                },
                "content": {"type": "string"},
                "author": {"type": "string"},
            },
            "required": ["project_id", "node_id", "kind", "content"],
        },
    },
    # --- Backlog tools (OPT-DB-BACKLOG) ---
    {
        "name": "backlog_list",
        "description": "List backlog bugs for a project. Defaults to compact OPEN rows to avoid oversized MCP context; use backlog_get for full detail.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project identifier."},
                "status": {"type": "string", "description": "Filter by status (e.g. OPEN, FIXED)."},
                "priority": {"type": "string", "description": "Filter by priority (e.g. P1, P2, P3)."},
                "limit": {"type": "integer", "description": "Maximum rows to return, default 50, max 100."},
                "offset": {"type": "integer", "description": "Pagination offset."},
                "q": {"type": "string", "description": "Case-insensitive search across id, title, details, and file fields."},
                "view": {"type": "string", "enum": ["compact", "full"], "description": "Row shape; compact is the default."},
                "include_closed": {"type": "boolean", "description": "When true and no status is supplied, include closed statuses."},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "backlog_get",
        "description": "Get details of a single backlog bug by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project identifier."},
                "bug_id": {"type": "string", "description": "Bug identifier (e.g. B47)."},
            },
            "required": ["project_id", "bug_id"],
        },
    },
    {
        "name": "backlog_upsert",
        "description": "Create or update a backlog bug entry.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "bug_id": {"type": "string"},
                "title": {"type": "string"},
                "status": {"type": "string"},
                "priority": {"type": "string"},
                "target_files": {"type": "array", "items": {"type": "string"}},
                "test_files": {"type": "array", "items": {"type": "string"}},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                "chain_task_id": {"type": "string"},
                "commit": {"type": "string"},
                "discovered_at": {"type": "string"},
                "details_md": {"type": "string"},
                "chain_trigger_json": {"type": "object"},
                "fixed_at": {"type": "string"},
                "actor": {"type": "string"},
                "route_token": {"type": "object", "description": "Route-token evidence required for protected backlog state/close evidence writes."},
                "route_token_ref": {"type": "string", "description": "Opaque server-registered route token reference accepted by protected HTTP facades."},
                "route_waiver": {"type": "object", "description": "Explicit route-context-consuming waiver for protected route-token gates."},
                "route_token_waiver": {"type": "object", "description": "Alias for route_waiver."},
            },
            "required": ["project_id", "bug_id"],
        },
    },
    {
        "name": "backlog_close",
        "description": "Close a backlog bug (set status=FIXED with commit hash).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "bug_id": {"type": "string"},
                "commit": {"type": "string", "description": "Git commit hash that fixes the bug."},
                "contract_execution_id": {"type": "string", "description": "Optional ContractRuntime execution id to use for backlog close authority projection."},
                "route_token": {"type": "object", "description": "Route-token evidence required for protected backlog close."},
                "route_token_ref": {"type": "string", "description": "Opaque server-registered route token reference accepted by protected HTTP facades."},
                "route_waiver": {"type": "object", "description": "Explicit manual-fix/same-worktree waiver for protected route-token gates."},
                "route_token_waiver": {"type": "object", "description": "Alias for route_waiver."},
            },
            "required": ["project_id", "bug_id"],
        },
    },
    {
        "name": "backlog_audit_archive",
        "description": "Observer-owned audit archive for implemented backlog rows that cannot legally reconstruct MF close evidence. Sets status=WAIVED; does not claim can_close or emit close_ready.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "bug_id": {"type": "string"},
                "commit": {"type": "string", "description": "Implementation commit hash to preserve on the archived row."},
                "reason": {"type": "string", "description": "Human-readable reason explaining why ordinary MF close cannot be satisfied."},
                "non_reconstructable_evidence_reason": {"type": "string"},
                "references": {"type": "array", "items": {"type": "string"}},
                "failure_audit": {"type": "object"},
                "qa_acceptance": {"type": "object"},
                "audit_close_gate": {"type": "object"},
                "verification": {"type": "object"},
                "graph_snapshot": {"type": "object"},
                "graph_snapshot_id": {"type": "string"},
                "timeline_precheck": {"type": "object"},
                "runtime_context": {"type": "object"},
                "source_backlog_id": {"type": "string"},
                "source_runtime_context_id": {"type": "string"},
                "actor": {"type": "string"},
                "route_token": {"type": "object", "description": "Route-token evidence required for protected audit archive."},
                "route_token_ref": {"type": "string", "description": "Opaque server-registered route token reference accepted by protected HTTP facades."},
                "route_waiver": {"type": "object", "description": "Explicit route-context-consuming waiver for protected route-token gates."},
                "route_token_waiver": {"type": "object", "description": "Alias for route_waiver."},
            },
            "required": ["project_id", "bug_id", "commit", "reason"],
        },
    },
    {
        "name": "task_timeline_append",
        "description": "Append observer/agent execution evidence to the task timeline. Use this during MF work before close.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "task_id": {"type": "string"},
                "backlog_id": {"type": "string"},
                "mf_id": {"type": "string"},
                "attempt_num": {"type": "integer"},
                "event_type": {"type": "string"},
                "phase": {"type": "string"},
                "event_kind": {"type": "string", "description": "For MF close gate use implementation, verification, or close_ready."},
                "scenario_id": {"type": "string"},
                "parent_event_id": {"type": "integer"},
                "correlation_id": {"type": "string"},
                "severity": {"type": "string"},
                "decision": {"type": "string"},
                "schema_version": {"type": "integer"},
                "actor": {"type": "string"},
                "status": {"type": "string", "description": "Use accepted/ok/passed/succeeded for close-gate evidence."},
                "payload": {"type": "object"},
                "verification": {"type": "object"},
                "artifact_refs": {"type": "object"},
                "trace_id": {"type": "string"},
                "commit_sha": {"type": "string"},
                "route_token": {"type": "object", "description": "Route-token evidence required for protected close-gate timeline evidence."},
                "route_token_ref": {"type": "string", "description": "Opaque server-registered route token reference accepted by protected HTTP facades."},
                "route_waiver": {"type": "object", "description": "Explicit route-context-consuming waiver for protected route-token gates."},
                "route_token_waiver": {"type": "object", "description": "Alias for route_waiver."},
            },
            "required": ["project_id", "event_type"],
        },
    },
    {
        "name": "task_timeline_list",
        "description": "List append-only observer/agent timeline events by backlog, task, trace, phase, or event kind.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "task_id": {"type": "string"},
                "backlog_id": {"type": "string"},
                "trace_id": {"type": "string"},
                "phase": {"type": "string"},
                "event_kind": {"type": "string"},
                "scenario_id": {"type": "string"},
                "correlation_id": {"type": "string"},
                "severity": {"type": "string"},
                "decision": {"type": "string"},
                "parent_event_id": {"type": "integer"},
                "limit": {"type": "integer", "description": "Maximum events to return, default 200, max 1000"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "mf_timeline_precheck",
        "description": (
            "Legacy/advisory MF timeline diagnostic before backlog_close. "
            "This is not final close authority; contract runtime, the "
            "server-side contract gate kernel, and backlog_close remain authoritative."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "bug_id": {"type": "string"},
                "view": {
                    "type": "string",
                    "enum": ["full", "compact", "repair"],
                    "description": "Response projection: full legacy gate tree, compact advisory gate summary, or advisory repair payloads. None of these are final close authority.",
                },
                "include_events": {"type": "boolean", "description": "Include matching timeline rows in the response."},
                "limit": {"type": "integer", "description": "Maximum events to inspect/return, default 1000, max 1000"},
                "close_commit": {"type": "string", "description": "Optional close commit to plan against; activates commit-scoped close evidence guidance."},
                "commit": {"type": "string", "description": "Alias for close_commit."},
                "commit_sha": {"type": "string", "description": "Alias for close_commit."},
                "target_head_commit": {"type": "string", "description": "Alias for close_commit."},
                "head_commit": {"type": "string", "description": "Alias for close_commit."},
            },
            "required": ["project_id", "bug_id"],
        },
    },
    {
        "name": "onboard_route_guide",
        "description": (
            "Only onboard service entrypoint for Aming Claw role, work-type, "
            "capability, system-operation, and backlog-chain guidance. Prefer "
            "this MCP tool; use the HTTP endpoint only when MCP is unavailable."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project identifier.",
                },
                "backlog_id": {
                    "type": "string",
                    "description": "Optional backlog row to scope the guide.",
                },
                "bug_id": {
                    "type": "string",
                    "description": "Alias for backlog_id.",
                },
                "role": {
                    "type": "string",
                    "description": "Requested actor role, such as observer, mf_sub, worker, or qa.",
                },
                "actor_role": {
                    "type": "string",
                    "description": "Alias for role when the host names the actor role explicitly.",
                },
                "work_type": {
                    "type": "string",
                    "description": "Requested work type for onboard routing.",
                },
                "requested_work_type": {
                    "type": "string",
                    "description": "Alias for work_type.",
                },
                "route_token_ref": {
                    "type": "string",
                    "description": "Opaque route-token ref accepted by protected HTTP facades.",
                },
                "observer_route_token_ref": {
                    "type": "string",
                    "description": "Opaque observer route-token ref; raw route tokens are not accepted.",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "contract_chain_current",
        "description": (
            "Read the durable backlog_contract_chain_current projection for a "
            "backlog row. This does not timeline-scan as a normal fallback; "
            "set rebuild_if_missing only for an explicit projection rebuild."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project identifier.",
                },
                "backlog_id": {
                    "type": "string",
                    "description": "Backlog row id to read from the current projection.",
                },
                "bug_id": {
                    "type": "string",
                    "description": "Alias for backlog_id.",
                },
                "rebuild_if_missing": {
                    "type": "boolean",
                    "description": "Explicitly rebuild the projection if no current row exists.",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "observer_hotfix_enter",
        "description": "Enter source-backed observer_hotfix successor runtime after onboarding completion.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "backlog_id": {"type": "string"},
                "bug_id": {"type": "string"},
                "task_id": {"type": "string"},
                "reason": {"type": "string"},
                "human_reason": {"type": "string"},
                "hotfix_reason": {"type": "string"},
                "actor": {"type": "string"},
                "actor_role": {
                    "type": "string",
                    "description": "Accepted for audit only; HTTP facade derives the effective role from the session/token.",
                },
                "route_token_ref": {"type": "string"},
                "observer_route_token_ref": {
                    "type": "string",
                    "description": "Opaque observer route-token ref; raw route tokens are not accepted.",
                },
                "observer_session_id": {
                    "type": "string",
                    "description": "Opaque active observer session id used with observer_route_token_ref.",
                },
                "onboard_service_waiver": {
                    "type": "object",
                    "description": "Use the onboard_route_guide service parent instead of legacy onboard_contract.",
                },
            },
            "required": ["project_id", "reason"],
            "anyOf": [
                {"required": ["backlog_id"]},
                {"required": ["bug_id"]},
            ],
        },
    },
    {
        "name": "mf_parallel_enter",
        "description": (
            "Enter source-backed mf_parallel successor runtime only after "
            "onboard_route_guide selects it as the next interface."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "backlog_id": {"type": "string"},
                "bug_id": {"type": "string"},
                "task_id": {"type": "string"},
                "reason": {"type": "string"},
                "human_reason": {"type": "string"},
                "actor": {"type": "string"},
                "actor_role": {
                    "type": "string",
                    "description": "Accepted for audit only; HTTP facade derives the effective role from the session/token.",
                },
                "contract_execution_id": {"type": "string"},
                "route_token_ref": {"type": "string"},
                "observer_route_token_ref": {
                    "type": "string",
                    "description": "Opaque observer route-token ref; raw route tokens are not accepted.",
                },
                "observer_session_id": {
                    "type": "string",
                    "description": "Opaque active observer session id used with observer_route_token_ref.",
                },
                "onboard_service_waiver": {
                    "type": "boolean",
                    "description": "Use the onboard_route_guide service parent instead of legacy onboard_contract.",
                },
                "worker_fence": {"type": "object"},
                "owned_files": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "target_files": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "metadata": {"type": "object"},
            },
            "required": ["project_id", "reason"],
            "anyOf": [
                {"required": ["backlog_id"]},
                {"required": ["bug_id"]},
            ],
        },
    },
    {
        "name": "mf_batch_parallel_enter",
        "description": (
            "Enter source-backed mf_batch_parallel parent runtime only after "
            "onboard_route_guide selects multi_backlog_parallel."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "backlog_id": {
                    "type": "string",
                    "description": "Coordination backlog row for the batch.",
                },
                "bug_id": {
                    "type": "string",
                    "description": "Alias for the coordination backlog row.",
                },
                "backlog_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Child backlog rows to preflight and fan out.",
                },
                "task_id": {"type": "string"},
                "reason": {"type": "string"},
                "human_reason": {"type": "string"},
                "actor": {"type": "string"},
                "actor_role": {
                    "type": "string",
                    "description": "Accepted for audit only; HTTP facade derives the effective role from the session/token.",
                },
                "route_token_ref": {"type": "string"},
                "observer_route_token_ref": {
                    "type": "string",
                    "description": "Opaque observer route-token ref; raw route tokens are not accepted.",
                },
                "observer_session_id": {
                    "type": "string",
                    "description": "Opaque active observer session id used with observer_route_token_ref.",
                },
                "onboard_service_waiver": {
                    "type": "boolean",
                    "description": "Use the onboard_route_guide service parent instead of legacy onboard_contract.",
                },
                "target_head_commit": {"type": "string"},
                "target_head": {
                    "type": "string",
                    "description": "Alias for target_head_commit.",
                },
                "head_commit": {
                    "type": "string",
                    "description": "Alias for target_head_commit.",
                },
                "target_ref": {"type": "string"},
                "snapshot_id": {"type": "string"},
                "graph_snapshot_id": {
                    "type": "string",
                    "description": "Alias for snapshot_id.",
                },
                "preflight_mode": {"type": "string"},
                "merge_mode": {
                    "type": "string",
                    "description": "Alias for preflight_mode.",
                },
                "merge_queue_id": {"type": "string"},
                "metadata": {"type": "object"},
            },
            "required": ["project_id", "backlog_ids", "reason"],
            "anyOf": [
                {"required": ["backlog_id"]},
                {"required": ["bug_id"]},
            ],
        },
    },
    {
        "name": "onboard_contract_start",
        "description": (
            "Legacy/internal onboard_contract facade. Do not use as an entrypoint; "
            "call onboard_route_guide first and use this only when that service "
            "explicitly returns the waived legacy contract path."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "backlog_id": {"type": "string"},
                "bug_id": {"type": "string"},
                "route_token_ref": {"type": "string"},
                "observer_route_token_ref": {
                    "type": "string",
                    "description": "Opaque observer route-token ref; raw route tokens are not accepted.",
                },
                "observer_session_id": {
                    "type": "string",
                    "description": "Opaque active observer session id used with observer_route_token_ref.",
                },
                "metadata": {"type": "object"},
            },
            "required": ["project_id", "backlog_id"],
        },
    },
    {
        "name": "onboard_contract_current",
        "description": (
            "Legacy/internal onboard_contract current-state reader. Do not use as "
            "an entrypoint; call onboard_route_guide first and use this only for "
            "a service-returned contract_execution_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "contract_execution_id": {"type": "string"},
                "observer_route_token_ref": {
                    "type": "string",
                    "description": "Opaque observer route-token ref; raw route tokens are not accepted.",
                },
                "route_token_ref": {"type": "string"},
                "observer_session_id": {
                    "type": "string",
                    "description": "Opaque active observer session id used with observer_route_token_ref.",
                },
            },
            "required": ["project_id", "contract_execution_id"],
        },
    },
    {
        "name": "onboard_contract_submit_line",
        "description": (
            "Legacy/internal onboard_contract evidence writer. Do not use as an "
            "entrypoint; call onboard_route_guide first and submit only when the "
            "service-returned guide requires this waived legacy contract line."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "contract_execution_id": {"type": "string"},
                "execution_state_revision": {"type": "integer"},
                "runtime_guide_hash": {"type": "string"},
                "stage_id": {"type": "string"},
                "line_id": {"type": "string"},
                "evidence_kind": {"type": "string"},
                "payload": {"type": "object"},
                "artifact_refs": {"type": "object"},
                "trace_id": {"type": "string"},
                "commit_sha": {"type": "string"},
                "observer_route_token_ref": {
                    "type": "string",
                    "description": "Opaque observer route-token ref; raw route tokens are not accepted.",
                },
                "route_token_ref": {"type": "string"},
                "observer_session_id": {
                    "type": "string",
                    "description": "Opaque active observer session id used with observer_route_token_ref.",
                },
            },
            "required": ["project_id", "contract_execution_id"],
        },
    },
    {
        "name": "contract_add_start",
        "description": "Start or enter the thin source-backed contract_add guided runtime facade.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "backlog_id": {"type": "string"},
                "bug_id": {"type": "string"},
                "contract_execution_id": {"type": "string"},
                "route_token_ref": {"type": "string"},
                "observer_route_token_ref": {
                    "type": "string",
                    "description": "Opaque observer route-token ref; raw route tokens are not accepted.",
                },
                "observer_session_id": {
                    "type": "string",
                    "description": "Opaque active observer session id used with observer_route_token_ref.",
                },
                "metadata": {"type": "object"},
            },
            "required": ["project_id", "backlog_id"],
        },
    },
    {
        "name": "contract_add_current",
        "description": "Read contract_add runtime guide/current-state without exposing generic CRUD.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "contract_execution_id": {"type": "string"},
                "observer_route_token_ref": {
                    "type": "string",
                    "description": "Opaque observer route-token ref; raw route tokens are not accepted.",
                },
                "route_token_ref": {"type": "string"},
                "observer_session_id": {
                    "type": "string",
                    "description": "Opaque active observer session id used with observer_route_token_ref.",
                },
            },
            "required": ["project_id", "contract_execution_id"],
        },
    },
    {
        "name": "contract_add_submit_line",
        "description": "Submit one role-bound contract_add evidence line via ContractRuntime.submit_line_write.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "contract_execution_id": {"type": "string"},
                "stage_id": {"type": "string"},
                "line_id": {"type": "string"},
                "evidence_kind": {"type": "string"},
                "payload": {"type": "object"},
                "artifact_refs": {"type": "object"},
                "trace_id": {"type": "string"},
                "commit_sha": {"type": "string"},
                "observer_route_token_ref": {
                    "type": "string",
                    "description": "Opaque observer route-token ref; raw route tokens are not accepted.",
                },
                "route_token_ref": {"type": "string"},
                "observer_session_id": {
                    "type": "string",
                    "description": "Opaque active observer session id used with observer_route_token_ref.",
                },
            },
            "required": ["project_id", "contract_execution_id"],
        },
    },
    {
        "name": "contract_update_start",
        "description": "Start or enter the thin source-backed contract_update guided runtime facade.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "backlog_id": {"type": "string"},
                "bug_id": {"type": "string"},
                "contract_execution_id": {"type": "string"},
                "route_token_ref": {"type": "string"},
                "observer_route_token_ref": {
                    "type": "string",
                    "description": "Opaque observer route-token ref; raw route tokens are not accepted.",
                },
                "observer_session_id": {
                    "type": "string",
                    "description": "Opaque active observer session id used with observer_route_token_ref.",
                },
                "metadata": {"type": "object"},
            },
            "required": ["project_id", "backlog_id"],
        },
    },
    {
        "name": "contract_update_current",
        "description": "Read contract_update runtime guide/current-state without exposing generic CRUD.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "contract_execution_id": {"type": "string"},
                "observer_route_token_ref": {
                    "type": "string",
                    "description": "Opaque observer route-token ref; raw route tokens are not accepted.",
                },
                "route_token_ref": {"type": "string"},
                "observer_session_id": {
                    "type": "string",
                    "description": "Opaque active observer session id used with observer_route_token_ref.",
                },
            },
            "required": ["project_id", "contract_execution_id"],
        },
    },
    {
        "name": "contract_update_submit_line",
        "description": "Submit one role-bound contract_update evidence line via ContractRuntime.submit_line_write.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "contract_execution_id": {"type": "string"},
                "stage_id": {"type": "string"},
                "line_id": {"type": "string"},
                "evidence_kind": {"type": "string"},
                "payload": {"type": "object"},
                "artifact_refs": {"type": "object"},
                "trace_id": {"type": "string"},
                "commit_sha": {"type": "string"},
                "observer_route_token_ref": {
                    "type": "string",
                    "description": "Opaque observer route-token ref; raw route tokens are not accepted.",
                },
                "route_token_ref": {"type": "string"},
                "observer_session_id": {
                    "type": "string",
                    "description": "Opaque active observer session id used with observer_route_token_ref.",
                },
            },
            "required": ["project_id", "contract_execution_id"],
        },
    },
    {
        "name": "contract_runtime_current",
        "description": "Read a source-backed ContractRuntime execution current-state through the generic facade.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "contract_execution_id": {"type": "string"},
                "observer_route_token_ref": {
                    "type": "string",
                    "description": "Opaque observer route-token ref; raw route tokens are not accepted.",
                },
                "route_token_ref": {"type": "string"},
                "observer_session_id": {
                    "type": "string",
                    "description": "Opaque active observer session id used with observer_route_token_ref.",
                },
            },
            "required": ["project_id", "contract_execution_id"],
        },
    },
    {
        "name": "contract_runtime_guide",
        "description": "Read a source-backed ContractRuntime execution guide through the generic facade.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "contract_execution_id": {"type": "string"},
                "observer_route_token_ref": {
                    "type": "string",
                    "description": "Opaque observer route-token ref; raw route tokens are not accepted.",
                },
                "route_token_ref": {"type": "string"},
                "observer_session_id": {
                    "type": "string",
                    "description": "Opaque active observer session id used with observer_route_token_ref.",
                },
            },
            "required": ["project_id", "contract_execution_id"],
        },
    },
    {
        "name": "contract_runtime_submit_line",
        "description": "Submit one role-bound generic ContractRuntime evidence line via ContractRuntime.submit_line_write.",
        "inputSchema": {
            "type": "object",
            "properties": _contract_runtime_submit_line_schema_properties(),
            "required": ["project_id", "contract_execution_id"],
        },
    },
    {
        "name": "contract_runtime_precheck_line",
        "description": "Precheck one role-bound generic ContractRuntime evidence line without appending completed evidence.",
        "inputSchema": {
            "type": "object",
            "properties": _contract_runtime_submit_line_schema_properties(),
            "required": ["project_id", "contract_execution_id"],
        },
    },
    {
        "name": "graph_current_full_reconcile",
        "description": (
            "Run the canonical current-commit full graph reconcile path. "
            "Defaults to current clean HEAD and activate=true; route-proof "
            "calls use observer_session_id with exactly one of "
            "observer_route_token_ref or route_token_ref."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "target_commit_sha": {"type": "string"},
                "commit_sha": {"type": "string"},
                "run_id": {"type": "string"},
                "snapshot_id": {"type": "string"},
                "expected_old_snapshot_id": {"type": "string"},
                "actor": {"type": "string"},
                "backlog_id": {"type": "string"},
                "bug_id": {"type": "string", "description": "Alias for backlog_id."},
                "task_id": {"type": "string"},
                "contract_execution_id": {"type": "string", "description": "Alias for task_id."},
                "observer_session_id": {
                    "type": "string",
                    "description": "Opaque active observer session id used with observer_route_token_ref.",
                },
                "observer_route_token_ref": {
                    "type": "string",
                    "description": "Opaque observer route-token ref; raw route tokens are not accepted.",
                },
                "route_token_ref": {
                    "type": "string",
                    "description": "Alias for observer_route_token_ref.",
                },
                "activate": {"type": "boolean", "default": True},
                "require_clean": {"type": "boolean", "default": True},
                "semantic_use_ai": {"type": "boolean"},
                "semantic_enrich": {"type": "boolean"},
                "enqueue_stale": {"type": "boolean", "default": False},
                "notes_extra": {"type": "object"},
                "timeout_seconds": {
                    "type": "integer",
                    "description": (
                        "Client-side MCP reconcile request timeout. Defaults to "
                        "AMING_GRAPH_RECONCILE_MCP_TIMEOUT_SECONDS or 900 seconds."
                    ),
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "runtime_context_current",
        "description": "Read the Runtime Context Service current-state projection. mf_sub callers receive only the role-filtered worker view.",
        "inputSchema": {
            "type": "object",
            "properties": _runtime_context_schema_properties(),
            "required": ["project_id", "runtime_context_id"],
        },
    },
    {
        "name": "runtime_context_worker_guide",
        "description": "Read the Runtime Context Service worker guide, including read/write guide intent for a bounded worker.",
        "inputSchema": {
            "type": "object",
            "properties": _runtime_context_schema_properties(),
            "required": ["project_id", "runtime_context_id"],
        },
    },
    {
        "name": "runtime_context_read_receipt",
        "description": "Worker-authored canonical Runtime Context read-receipt facade. Prefer this over legacy task_timeline_append or generic ContractRuntime line writes for mf_sub happy paths.",
        "inputSchema": {
            "type": "object",
            "properties": _runtime_context_write_schema_properties(),
            "required": ["project_id", "runtime_context_id"],
        },
    },
    {
        "name": "runtime_context_implementation_evidence",
        "description": "Worker-authored canonical Runtime Context implementation-evidence facade. Prefer this over legacy task_timeline_append for mf_sub happy paths.",
        "inputSchema": {
            "type": "object",
            "properties": _runtime_context_write_schema_properties(),
            "required": ["project_id", "runtime_context_id"],
        },
    },
    {
        "name": "runtime_context_finish_time_worker_attestation",
        "description": "Worker-authored canonical Runtime Context finish-time self-attestation facade. Must run before the worker git commit; runtime-context lanes block if the assigned row head already moved and no committed-branch evidence lane exists.",
        "inputSchema": {
            "type": "object",
            "properties": _runtime_context_write_schema_properties(),
            "required": ["project_id", "runtime_context_id"],
        },
    },
    {
        "name": "runtime_context_finish_gate",
        "description": "Canonical Runtime Context finish-gate facade that records mf_subagent_finish_gate evidence before the worker git commit.",
        "inputSchema": {
            "type": "object",
            "properties": _runtime_context_write_schema_properties(),
            "required": ["project_id", "runtime_context_id"],
        },
    },
    {
        "name": "runtime_context_session_token_initial_join",
        "description": "Observer/host-adapter facade that issues the first audited worker host envelope before mf_sub read-receipt/startup lineage exists. Does not persist raw tokens.",
        "inputSchema": {
            "type": "object",
            "properties": _runtime_context_write_schema_properties(),
            "required": ["project_id", "runtime_context_id", "task_id", "reason"],
        },
    },
    {
        "name": "runtime_context_session_token_rejoin",
        "description": "Observer recovery facade that issues an audited worker host envelope when a resumed mf_sub session lost raw worker auth material. Does not authorize ref-only worker writes.",
        "inputSchema": {
            "type": "object",
            "properties": _runtime_context_write_schema_properties(),
            "required": ["project_id", "runtime_context_id", "task_id", "reason"],
        },
    },
    {
        "name": "parallel_branch_allocate",
        "description": "Observer-facing wrapper to allocate/register a parallel branch runtime context before spawning a bounded worker.",
        "inputSchema": {
            "type": "object",
            "properties": _parallel_branch_allocate_schema_properties(),
            "required": ["project_id", "task_id"],
        },
    },
    {
        "name": "parallel_branch_merge_queue_status",
        "description": (
            "Copy-safe merge queue status for direct-fix, hotfix, mf_parallel, "
            "and mf_batch_parallel flows. Returns the durable ordered queue "
            "read model without mutating refs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _parallel_branch_merge_queue_status_schema_properties(),
            "required": ["project_id"],
        },
    },
    {
        "name": "parallel_branch_merge_queue_materialize",
        "description": (
            "Materialize a durable merge queue item after mf_sub finish gate and "
            "independent QA. This is the explicit mf_parallel handoff before "
            "ordered merge apply."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _parallel_branch_merge_queue_materialize_schema_properties(),
            "required": ["project_id", "merge_queue_id", "task_id"],
        },
    },
    {
        "name": "parallel_branch_merge_queue_apply",
        "description": (
            "Copy-safe ordered merge queue apply path. Defaults to dry_run; "
            "live target-ref mutation requires dry_run=false, "
            "allow_target_ref_mutation=true, route authorization, and merge "
            "gate evidence."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _parallel_branch_merge_queue_apply_schema_properties(),
            "required": ["project_id", "merge_queue_id"],
        },
    },
    {
        "name": "observer_repair_run_plan",
        "description": "Build a read-only replayable observer repair-run plan for cross-system recovery. Does not authorize protected writes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "root_backlog_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Root backlog ids to diagnose and order.",
                },
                "backlog_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Alias for root_backlog_ids.",
                },
                "blockers": {
                    "type": "array",
                    "items": {},
                    "description": "Optional blocker messages or structured failures to classify.",
                },
                "include_timeline_precheck": {
                    "type": "boolean",
                    "description": "When true, include read-only MF timeline precheck summaries for root backlog ids.",
                },
                "route_context_seed": {
                    "type": "object",
                    "description": "Public-safe seed material for deterministic route context identity.",
                },
                "version_check": {
                    "type": "object",
                    "description": "Optional clean-workspace/version evidence for route action precheck preview.",
                },
                "actor": {"type": "string"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "observer_repair_run_route_evidence",
        "description": "Dry-run or record replayable route-service evidence for an observer repair-run plan. Defaults to dry-run and does not fabricate worker, QA, implementation, verification, or close_ready evidence.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "root_backlog_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Root backlog ids to diagnose and attach route-service evidence to.",
                },
                "backlog_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Alias for root_backlog_ids.",
                },
                "blockers": {
                    "type": "array",
                    "items": {},
                    "description": "Optional blocker messages or structured failures to classify.",
                },
                "include_timeline_precheck": {
                    "type": "boolean",
                    "description": "When true, include read-only MF timeline precheck summaries while building the plan.",
                },
                "route_context_seed": {
                    "type": "object",
                    "description": "Public-safe seed material for deterministic route context identity.",
                },
                "version_check": {
                    "type": "object",
                    "description": "Optional clean-workspace/version evidence for route action precheck.",
                },
                "action_precheck_id": {
                    "type": "string",
                    "description": "Route action precheck to record; defaults to observer_dispatch_bounded_worker.",
                },
                "route_identity": {
                    "type": "object",
                    "description": "Public route identity for external action-precheck materialization: route_context_hash, prompt_contract_id, optional prompt_contract_hash, and visible_injection_manifest_hash.",
                },
                "external_route_identity": {
                    "type": "object",
                    "description": "Alias for route_identity.",
                },
                "action_precheck": {
                    "type": "object",
                    "description": "Optional public action-precheck packet to validate against route_identity. Private provider bodies and raw prompts are not required and are not materialized.",
                },
                "record": {
                    "type": "boolean",
                    "description": "When true, append route-service source events to the timeline. Defaults to false.",
                },
                "include_plan": {
                    "type": "boolean",
                    "description": "Include the full repair-run plan in dry-run output.",
                },
                "actor": {"type": "string"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "observer_route_context_issue",
        "description": (
            "Mint an Aming-owned, write-authorizing observer route token "
            "(decision route_token) without any external route provider. "
            "Authorizes observer orchestration/close actions and observer-prefilled "
            "child action-scope refs for QA-owned timeline evidence, but blocks "
            "direct file edits. Also returns a consumable route_token_ref + "
            "merge_queue_id and an execute_backlog_row_payload; handoffs should pass "
            "the ref, not the raw token."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "backlog_id": {
                    "type": "string",
                    "description": "Backlog id the route token scope binds to.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task id the route token scope binds to.",
                },
                "target_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Fenced target files for the bounded implementation subagent.",
                },
                "allowed_actions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional override of authorized protected actions (defaults to observer orchestration/close set). Wildcard and blocked actions are rejected at mint.",
                },
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional extra session/command/graph evidence refs to embed.",
                },
                "ttl_hours": {
                    "type": "number",
                    "description": "Token lifetime in hours (default 24, clamped to a max).",
                },
                "parent_route_identity": {
                    "type": "object",
                    "description": (
                        "Optional public-only canonical parent route identity to bind "
                        "the issued worker route as a child. Accepts public fields such "
                        "as route_id (route-*, event.route_prompt_context.preview, "
                        "or event.route_action.pre_mutation), "
                        "route_context_hash, prompt_contract_id, prompt_contract_hash, "
                        "visible_injection_manifest_hash, selected_project, "
                        "selected_backlog_id, and opaque route_token_ref. Raw route or "
                        "session token bodies are rejected by the issuer."
                    ),
                    "properties": {
                        "route_id": {"type": "string"},
                        "route_context_hash": {"type": "string"},
                        "prompt_contract_id": {"type": "string"},
                        "prompt_contract_hash": {"type": "string"},
                        "visible_injection_manifest_hash": {"type": "string"},
                        "selected_project": {"type": "string"},
                        "selected_backlog_id": {"type": "string"},
                        "route_token_ref": {"type": "string"},
                    },
                },
                "parent_route_id": {
                    "type": "string",
                    "description": (
                        "Explicit public parent route id (route-*, "
                        "event.route_prompt_context.preview, or "
                        "event.route_action.pre_mutation)."
                    ),
                },
                "parent_route_context_hash": {
                    "type": "string",
                    "description": "Explicit parent route_context_hash; must be sha256:...",
                },
                "parent_prompt_contract_id": {
                    "type": "string",
                    "description": "Explicit parent prompt contract id; must be rprompt-*.",
                },
                "parent_prompt_contract_hash": {
                    "type": "string",
                    "description": "Explicit parent prompt contract hash; must be sha256:...",
                },
                "parent_visible_injection_manifest_hash": {
                    "type": "string",
                    "description": "Explicit parent visible injection manifest hash; must be sha256:...",
                },
                "parent_route_token_ref": {
                    "type": "string",
                    "description": "Optional opaque parent route token reference (rtok-*), never a raw token body.",
                },
            },
            "required": ["project_id", "backlog_id", "task_id", "target_files"],
        },
    },
    {
        "name": "observer_route_context_renew",
        "description": (
            "Renew an expired or near-expired server-registered route_token_ref "
            "for the same project/backlog/task/action/file scope. Requires an "
            "active observer_session_id and never returns a raw route token."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "caller_role": {
                    "type": "string",
                    "description": "Must be observer; defaults to observer in this MCP wrapper.",
                },
                "observer_session_id": {"type": "string"},
                "route_token_ref": {"type": "string"},
                "observer_route_token_ref": {"type": "string"},
                "backlog_id": {"type": "string"},
                "bug_id": {"type": "string"},
                "task_id": {"type": "string"},
                "contract_execution_id": {"type": "string"},
                "allowed_actions": {"type": "array", "items": {"type": "string"}},
                "target_files": {"type": "array", "items": {"type": "string"}},
                "owned_files": {"type": "array", "items": {"type": "string"}},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "ttl_hours": {"type": "number"},
                "renew_within_seconds": {"type": "integer"},
            },
            "required": [
                "project_id",
                "observer_session_id",
                "route_token_ref",
                "backlog_id",
                "task_id",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# Governance HTTP client helpers
# ---------------------------------------------------------------------------

def _gov_url() -> str:
    return os.environ.get("GOVERNANCE_URL", "http://localhost:40000").rstrip("/")


def _gov_token() -> str:
    return os.environ.get("GOV_TOKEN", "")


def _http(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    gov_token: str | None = None,
    timeout_seconds: int | None = None,
) -> dict:
    """Make an HTTP request to the governance service."""
    url = f"{_gov_url()}{path}"
    data = json.dumps(body, ensure_ascii=False).encode() if body else None
    request_timeout = int(timeout_seconds or 10)
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Gov-Token": gov_token if gov_token is not None else _gov_token(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=request_timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() if exc.fp else ""
        try:
            return json.loads(raw)
        except Exception:
            return {"error": str(exc), "body": raw}
    except (TimeoutError, socket.timeout) as exc:
        return {
            "ok": False,
            "error": "request_timeout",
            "message": str(exc),
            "timeout_seconds": request_timeout,
        }
    except Exception as exc:
        if _is_timeout_result({"error": str(exc)}):
            return {
                "ok": False,
                "error": "request_timeout",
                "message": str(exc),
                "timeout_seconds": request_timeout,
            }
        return {"error": str(exc)}


def _http_with_optional_gov_token(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    gov_token: str = "",
) -> dict:
    if gov_token:
        return _http(method, path, body, gov_token=gov_token)
    return _http(method, path, body)


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

def _dispatch_tool(name: str, args: dict) -> Any:
    """Dispatch a tools/call to the governance HTTP API."""
    if name == "gov_node_list":
        pid = args["project_id"]
        return _http("GET", f"/api/wf/{pid}/nodes")

    if name == "gov_node_status_update":
        pid = args["project_id"]
        nid = args["node_id"]
        return _http("POST", f"/api/wf/{pid}/nodes/{nid}/status", {"status": args["status"]})

    if name == "gov_gate_check":
        pid = args["project_id"]
        nid = args["node_id"]
        return _http("GET", f"/api/wf/{pid}/gates/{nid}")

    if name == "gov_memory_write":
        pid = args["project_id"]
        return _http("POST", f"/api/wf/{pid}/memory", args)

    # --- Backlog tools (OPT-DB-BACKLOG) ---
    if name == "backlog_list":
        pid = args["project_id"]
        query = _backlog_list_query(args)
        qs = f"?{urllib.parse.urlencode(query)}" if query else ""
        return _http("GET", f"/api/backlog/{pid}{qs}")

    if name == "backlog_get":
        pid = args["project_id"]
        bug_id = args["bug_id"]
        return _http("GET", f"/api/backlog/{pid}/{bug_id}")

    if name == "backlog_upsert":
        pid = args["project_id"]
        bug_id = args["bug_id"]
        return _http("POST", f"/api/backlog/{pid}/{bug_id}", args)

    if name == "backlog_close":
        pid = args["project_id"]
        bug_id = args["bug_id"]
        return _http("POST", f"/api/backlog/{pid}/{bug_id}/close", args)

    if name == "backlog_audit_archive":
        pid = args["project_id"]
        bug_id = urllib.parse.quote(str(args["bug_id"]), safe="")
        body = {
            key: value
            for key, value in args.items()
            if key not in {"project_id", "bug_id"} and value is not None
        }
        return _http("POST", f"/api/backlog/{pid}/{bug_id}/audit-archive", body)

    if name == "task_timeline_append":
        pid = args["project_id"]
        return _http("POST", f"/api/task/{pid}/timeline", _task_timeline_body(args))

    if name == "task_timeline_list":
        pid = args["project_id"]
        query = _task_timeline_query(args)
        qs = f"?{urllib.parse.urlencode(query)}" if query else ""
        return _http("GET", f"/api/task/{pid}/timeline{qs}")

    if name == "mf_timeline_precheck":
        pid = args["project_id"]
        bug_id = urllib.parse.quote(str(args["bug_id"]), safe="")
        query = {}
        if "include_events" in args:
            query["include_events"] = "true" if args.get("include_events") else "false"
        view = str(args.get("view") or "").strip().lower()
        if view:
            query["view"] = view
        if args.get("limit"):
            query["limit"] = str(_int_arg(args, "limit", 1000, minimum=1, maximum=1000))
        for key in ("close_commit", "commit", "commit_sha", "target_head_commit", "head_commit"):
            value = str(args.get(key) or "").strip()
            if value:
                query[key] = value
        qs = f"?{urllib.parse.urlencode(query)}" if query else ""
        return _http("GET", f"/api/backlog/{pid}/{bug_id}/timeline-gate{qs}")

    if name == "onboard_route_guide":
        pid = args["project_id"]
        return _http(
            "POST",
            f"/api/projects/{pid}/onboard-route-guide",
            _onboard_route_guide_body(args),
        )

    if name == "contract_chain_current":
        pid = args["project_id"]
        backlog_id = str(args.get("backlog_id") or args.get("bug_id") or "").strip()
        query = {}
        if backlog_id:
            query["backlog_id"] = backlog_id
        if "rebuild_if_missing" in args:
            query["rebuild_if_missing"] = (
                "true" if args.get("rebuild_if_missing") else "false"
            )
        qs = f"?{urllib.parse.urlencode(query)}" if query else ""
        return _http("GET", f"/api/projects/{pid}/contract-chain-current{qs}")

    if name == "observer_hotfix_enter":
        pid = args["project_id"]
        body = {
            key: value
            for key, value in args.items()
            if key != "project_id" and value is not None
        }
        return _http("POST", f"/api/projects/{pid}/hotfix/enter", body)

    if name == "mf_parallel_enter":
        pid = args["project_id"]
        body = {
            key: value
            for key, value in args.items()
            if key != "project_id" and value is not None
        }
        return _http("POST", f"/api/projects/{pid}/mf-parallel/enter", body)

    if name == "mf_batch_parallel_enter":
        pid = args["project_id"]
        body = {
            key: value
            for key, value in args.items()
            if key != "project_id" and value is not None
        }
        return _http(
            "POST", f"/api/projects/{pid}/mf-batch-parallel/enter", body
        )

    if name == "onboard_contract_start":
        pid = args["project_id"]
        body = {
            key: value
            for key, value in args.items()
            if key not in {"project_id", "contract_execution_id"} and value is not None
        }
        return _http("POST", f"/api/projects/{pid}/onboard-contract/start", body)

    if name == "onboard_contract_current":
        pid = args["project_id"]
        execution_id = urllib.parse.quote(str(args["contract_execution_id"]), safe="")
        query = {
            key: value
            for key, value in args.items()
            if key
            in {"observer_session_id", "observer_session_ref", "observer_route_token_ref", "route_token_ref"}
            and value is not None
        }
        qs = f"?{urllib.parse.urlencode(query)}" if query else ""
        return _http(
            "GET",
            f"/api/projects/{pid}/onboard-contract/{execution_id}/current-state{qs}",
        )

    if name == "onboard_contract_submit_line":
        pid = args["project_id"]
        execution_id = urllib.parse.quote(str(args["contract_execution_id"]), safe="")
        qa_session_token = str(args.get("qa_session_token") or "").strip()
        body = {
            key: value
            for key, value in args.items()
            if key not in {"project_id", "contract_execution_id", "qa_session_token"}
            and value is not None
        }
        return _http_with_optional_gov_token(
            "POST",
            f"/api/projects/{pid}/onboard-contract/{execution_id}/line-writes",
            body,
            gov_token=qa_session_token,
        )

    if name == "contract_add_start":
        pid = args["project_id"]
        body = {
            key: value
            for key, value in args.items()
            if key != "project_id" and value is not None
        }
        return _http("POST", f"/api/projects/{pid}/contract-add/start", body)

    if name == "contract_add_current":
        pid = args["project_id"]
        execution_id = urllib.parse.quote(str(args["contract_execution_id"]), safe="")
        query = {
            key: value
            for key, value in args.items()
            if key
            in {"observer_session_id", "observer_session_ref", "observer_route_token_ref", "route_token_ref"}
            and value is not None
        }
        qs = f"?{urllib.parse.urlencode(query)}" if query else ""
        return _http(
            "GET",
            f"/api/projects/{pid}/contract-add/{execution_id}/current-state{qs}",
        )

    if name == "contract_add_submit_line":
        pid = args["project_id"]
        execution_id = urllib.parse.quote(str(args["contract_execution_id"]), safe="")
        body = {
            key: value
            for key, value in args.items()
            if key not in {"project_id", "contract_execution_id"} and value is not None
        }
        return _http(
            "POST",
            f"/api/projects/{pid}/contract-add/{execution_id}/line-writes",
            body,
        )

    if name == "contract_update_start":
        pid = args["project_id"]
        body = {
            key: value
            for key, value in args.items()
            if key != "project_id" and value is not None
        }
        return _http("POST", f"/api/projects/{pid}/contract-update/start", body)

    if name == "contract_update_current":
        pid = args["project_id"]
        execution_id = urllib.parse.quote(str(args["contract_execution_id"]), safe="")
        qa_session_token = str(args.get("qa_session_token") or "").strip()
        query = {
            key: value
            for key, value in args.items()
            if key
            in {"observer_session_id", "observer_session_ref", "observer_route_token_ref", "route_token_ref"}
            and value is not None
        }
        qs = f"?{urllib.parse.urlencode(query)}" if query else ""
        return _http_with_optional_gov_token(
            "GET",
            f"/api/projects/{pid}/contract-update/{execution_id}/current-state{qs}",
            gov_token=qa_session_token,
        )

    if name == "contract_update_submit_line":
        pid = args["project_id"]
        execution_id = urllib.parse.quote(str(args["contract_execution_id"]), safe="")
        qa_session_token = str(args.get("qa_session_token") or "").strip()
        body = {
            key: value
            for key, value in args.items()
            if key not in {"project_id", "contract_execution_id", "qa_session_token"}
            and value is not None
        }
        return _http_with_optional_gov_token(
            "POST",
            f"/api/projects/{pid}/contract-update/{execution_id}/line-writes",
            body,
            gov_token=qa_session_token,
        )

    if name in {"contract_runtime_current", "contract_runtime_guide"}:
        pid = args["project_id"]
        execution_id = urllib.parse.quote(str(args["contract_execution_id"]), safe="")
        suffix = "guide" if name == "contract_runtime_guide" else "current-state"
        qa_session_token = str(args.get("qa_session_token") or "").strip()
        query = {
            key: value
            for key, value in args.items()
            if key
            in {"observer_session_id", "observer_session_ref", "observer_route_token_ref", "route_token_ref"}
            and value is not None
        }
        qs = f"?{urllib.parse.urlencode(query)}" if query else ""
        return _http_with_optional_gov_token(
            "GET",
            f"/api/projects/{pid}/contract-runtime/{execution_id}/{suffix}{qs}",
            gov_token=qa_session_token,
        )

    if name == "contract_runtime_submit_line":
        pid = args["project_id"]
        execution_id = urllib.parse.quote(str(args["contract_execution_id"]), safe="")
        qa_session_token = str(args.get("qa_session_token") or "").strip()
        body = {
            key: value
            for key, value in args.items()
            if key not in {"project_id", "contract_execution_id", "qa_session_token"}
            and value is not None
        }
        return _http_with_optional_gov_token(
            "POST",
            f"/api/projects/{pid}/contract-runtime/{execution_id}/line-writes",
            body,
            gov_token=qa_session_token,
        )

    if name == "contract_runtime_precheck_line":
        pid = args["project_id"]
        execution_id = urllib.parse.quote(str(args["contract_execution_id"]), safe="")
        qa_session_token = str(args.get("qa_session_token") or "").strip()
        body = {
            key: value
            for key, value in args.items()
            if key not in {"project_id", "contract_execution_id", "qa_session_token"}
            and value is not None
        }
        return _http_with_optional_gov_token(
            "POST",
            f"/api/projects/{pid}/contract-runtime/{execution_id}/line-writes/precheck",
            body,
            gov_token=qa_session_token,
        )

    if name == "graph_current_full_reconcile":
        pid = args["project_id"]
        timeout_seconds = _reconcile_mcp_timeout_seconds(args)
        body = {
            key: value
            for key, value in args.items()
            if key not in {"project_id", "timeout_seconds"} and value is not None
        }
        body, alias_error = _normalize_current_full_reconcile_route_token_aliases(body)
        if alias_error:
            return alias_error
        result = _http(
            "POST",
            f"/api/graph-governance/{pid}/reconcile/current-full",
            body,
            timeout_seconds=timeout_seconds,
        )
        if _is_timeout_result(result):
            queue = _http(
                "GET",
                f"/api/graph-governance/{pid}/operations/queue"
                "?include_status_observations=true&include_resolved=false",
                timeout_seconds=_RECONCILE_PROGRESS_POLL_TIMEOUT_SECONDS,
            )
            return _current_full_reconcile_timeout_response(
                pid,
                body,
                timeout_seconds=timeout_seconds,
                timeout_result=result,
                progress=_summarize_reconcile_progress(
                    queue,
                    _current_full_reconcile_run_id(body),
                ),
            )
        return result

    if name in {"runtime_context_current", "runtime_context_worker_guide"}:
        pid = args["project_id"]
        runtime_context_id = urllib.parse.quote(str(args["runtime_context_id"]), safe="")
        query = _runtime_context_query(args)
        qs = f"?{urllib.parse.urlencode(query)}" if query else ""
        suffix = "current-state" if name == "runtime_context_current" else "worker-guide"
        return _http(
            "GET",
            f"/api/graph-governance/{pid}/runtime-contexts/"
            f"{runtime_context_id}/{suffix}{qs}",
        )

    if name in {
        "runtime_context_read_receipt",
        "runtime_context_implementation_evidence",
        "runtime_context_finish_time_worker_attestation",
        "runtime_context_finish_gate",
        "runtime_context_session_token_initial_join",
        "runtime_context_session_token_rejoin",
    }:
        pid = args["project_id"]
        runtime_context_id = urllib.parse.quote(str(args["runtime_context_id"]), safe="")
        suffix_by_name = {
            "runtime_context_read_receipt": "read-receipts",
            "runtime_context_implementation_evidence": "implementation-evidence",
            "runtime_context_finish_time_worker_attestation": (
                "finish-time-worker-attestation"
            ),
            "runtime_context_finish_gate": "finish-gate",
            "runtime_context_session_token_initial_join": (
                "session-token/initial-join"
            ),
            "runtime_context_session_token_rejoin": "session-token/rejoin",
        }
        return _http(
            "POST",
            f"/api/graph-governance/{pid}/runtime-contexts/"
            f"{runtime_context_id}/{suffix_by_name[name]}",
            _runtime_context_write_body(args),
        )

    if name == "parallel_branch_allocate":
        pid = args["project_id"]
        body = {
            key: value
            for key, value in args.items()
            if key != "project_id" and value is not None
        }
        return _http(
            "POST",
            f"/api/graph-governance/{pid}/parallel-branches/allocate",
            body,
        )

    if name == "parallel_branch_merge_queue_status":
        pid = args["project_id"]
        query = _parallel_branch_merge_queue_status_query(args)
        qs = f"?{urllib.parse.urlencode(query)}" if query else ""
        return _http(
            "GET",
            f"/api/graph-governance/{pid}/parallel-branches{qs}",
        )

    if name == "parallel_branch_merge_queue_materialize":
        pid = args["project_id"]
        body = _parallel_branch_merge_queue_materialize_body(args)
        return _http(
            "POST",
            f"/api/graph-governance/{pid}/parallel-branches/merge-queue/materialize",
            body,
        )

    if name == "parallel_branch_merge_queue_apply":
        pid = args["project_id"]
        body = _parallel_branch_merge_queue_apply_body(args)
        return _http(
            "POST",
            f"/api/graph-governance/{pid}/parallel-branches/merge-execute",
            body,
        )

    if name == "observer_repair_run_plan":
        pid = args["project_id"]
        body = {
            key: value
            for key, value in args.items()
            if key != "project_id" and value is not None
        }
        return _http("POST", f"/api/projects/{pid}/observer-repair-run/plan", body)

    if name == "observer_repair_run_route_evidence":
        pid = args["project_id"]
        body = {
            key: value
            for key, value in args.items()
            if key != "project_id" and value is not None
        }
        return _http("POST", f"/api/projects/{pid}/observer-repair-run/route-evidence", body)

    if name == "observer_route_context_issue":
        pid = args["project_id"]
        body = {
            key: value
            for key, value in args.items()
            if key != "project_id" and value is not None
        }
        # This MCP tool IS the observer's native issuance path; assert the
        # observer role so the endpoint's caller_role authorization check passes
        # (unless an explicit caller_role was already supplied by the caller).
        body.setdefault("caller_role", "observer")
        return _http("POST", f"/api/projects/{pid}/observer/route-context/issue", body)

    if name == "observer_route_context_renew":
        pid = args["project_id"]
        body = {
            key: value
            for key, value in args.items()
            if key != "project_id" and value is not None
        }
        body.setdefault("caller_role", "observer")
        return _http("POST", f"/api/projects/{pid}/observer/route-context/renew", body)

    raise ValueError(f"Unknown tool: {name!r}")


# ---------------------------------------------------------------------------
# Stdio transport — thread-safe output
# ---------------------------------------------------------------------------

_stdout_lock = threading.Lock()


def _write(msg: dict) -> None:
    """Serialize *msg* as a single JSON line and write to stdout."""
    line = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
    with _stdout_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _response(req_id: Any, result: Any) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error_response(req_id: Any, code: int, message: str, data: Any = None) -> None:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    _write({"jsonrpc": "2.0", "id": req_id, "error": err})


def _notification(method: str, params: dict) -> None:
    """Send a server-initiated notification (no id field)."""
    _write({"jsonrpc": "2.0", "method": method, "params": params})


# ---------------------------------------------------------------------------
# JSON-RPC error codes
# ---------------------------------------------------------------------------
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

def _handle(raw: str) -> None:
    """Parse and handle one JSON-RPC message."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        _error_response(None, PARSE_ERROR, f"Parse error: {exc}")
        return

    req_id = msg.get("id")  # None for notifications from client
    method = msg.get("method", "")
    params = msg.get("params") or {}

    # -----------------------------------------------------------------------
    # initialize
    # -----------------------------------------------------------------------
    if method == "initialize":
        _response(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
        })
        return

    # -----------------------------------------------------------------------
    # notifications/initialized  (client acknowledges initialize)
    # -----------------------------------------------------------------------
    if method == "notifications/initialized":
        # No response required for notifications
        return

    # -----------------------------------------------------------------------
    # tools/list
    # -----------------------------------------------------------------------
    if method == "tools/list":
        _response(req_id, {"tools": TOOLS})
        return

    # -----------------------------------------------------------------------
    # tools/call
    # -----------------------------------------------------------------------
    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments") or {}
        try:
            result = _dispatch_tool(tool_name, tool_args)
            _response(req_id, {
                "content": [
                    {"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)},
                ],
            })
        except ValueError as exc:
            _error_response(req_id, METHOD_NOT_FOUND, str(exc))
        except Exception as exc:
            log.exception("Tool dispatch error: %s", tool_name)
            _error_response(req_id, INTERNAL_ERROR, str(exc))
        return

    # -----------------------------------------------------------------------
    # ping
    # -----------------------------------------------------------------------
    if method == "ping":
        _response(req_id, {})
        return

    # -----------------------------------------------------------------------
    # Unknown method
    # -----------------------------------------------------------------------
    if req_id is not None:
        _error_response(req_id, METHOD_NOT_FOUND, f"Method not found: {method!r}")


# ---------------------------------------------------------------------------
# Redis event subscriber → MCP notifications
# ---------------------------------------------------------------------------

def _redis_subscriber_thread() -> None:
    """Subscribe to Redis governance events and emit MCP notifications."""
    try:
        from .redis_client import get_redis
        from .event_bus import REDIS_CHANNEL_PREFIX
    except ImportError:
        try:
            # fallback when run as __main__
            from governance.redis_client import get_redis
            from governance.event_bus import REDIS_CHANNEL_PREFIX
        except ImportError:
            log.warning("Cannot import redis_client; Redis notifications disabled.")
            return

    # Retry loop — Redis may not be available at startup
    while True:
        try:
            r = get_redis()
            if not r.available or r._client is None:
                log.debug("Redis not available, retrying in 5s…")
                time.sleep(5)
                continue

            pubsub = r._client.pubsub()
            # Subscribe to the global channel and all project channels (wildcard)
            pubsub.psubscribe(f"{REDIS_CHANNEL_PREFIX}:*")
            log.info("MCP server subscribed to Redis pattern %s:*", REDIS_CHANNEL_PREFIX)

            for raw_msg in pubsub.listen():
                if raw_msg.get("type") not in ("pmessage", "message"):
                    continue
                data = raw_msg.get("data", "")
                if not data:
                    continue
                try:
                    payload = json.loads(data) if isinstance(data, str) else data
                except (json.JSONDecodeError, TypeError):
                    payload = {"raw": str(data)}

                _notification("governance/event", {
                    "channel": raw_msg.get("channel", ""),
                    "event": payload.get("event", "unknown"),
                    "payload": payload.get("payload", payload),
                })

        except Exception as exc:
            log.warning("Redis subscriber error (%s), reconnecting in 5s…", exc)
            time.sleep(5)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    """Start the MCP server: read stdin, dispatch, emit notifications."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    # Start Redis subscriber in background daemon thread
    t = threading.Thread(target=_redis_subscriber_thread, daemon=True, name="redis-sub")
    t.start()

    log.info("MCP governance server started (PID %d)", os.getpid())

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            _handle(raw)
        except Exception:
            log.exception("Unhandled error processing message: %s", raw[:200])


if __name__ == "__main__":
    run()
