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
import secrets
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
_CONTRACT_RUNTIME_MCP_TIMEOUT_LEGACY_SECONDS = 10
_CONTRACT_RUNTIME_MCP_TIMEOUT_DEFAULT_SECONDS = 120
_CONTRACT_RUNTIME_MCP_TIMEOUT_MIN_SECONDS = 10
_CONTRACT_RUNTIME_MCP_TIMEOUT_MAX_SECONDS = 60 * 60
_CONTRACT_RUNTIME_MCP_TIMEOUT_ENV_KEYS = (
    "AMING_CONTRACT_RUNTIME_MCP_TIMEOUT_SECONDS",
)
_WORKER_AUTH_ENV_FIELDS = {
    "session_token": "AMING_WORKER_SESSION_TOKEN",
    "fence_token": "AMING_WORKER_FENCE_TOKEN",
}
_WORKER_MCP_HOST_ONLY_TOOLS = frozenset(
    {
        "runtime_context_session_token_initial_join",
        "runtime_context_session_token_reissue",
        "runtime_context_session_token_rejoin",
    }
)


def _copy_safe_observer_route_context_issue_result(value: Any) -> Any:
    """Remove raw route authorization from the public MCP result."""

    def scrub(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: False if key == "raw_route_token_exposed" else scrub(nested)
                for key, nested in item.items()
                if key != "route_token"
            }
        if isinstance(item, list):
            return [scrub(nested) for nested in item]
        return item

    result = scrub(value)
    if isinstance(result, dict):
        result["raw_route_token_exposed"] = False
    return result


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


def _contract_runtime_mcp_timeout_seconds(args: dict) -> int:
    value = args.get("timeout_seconds")
    if value is None:
        for key in _CONTRACT_RUNTIME_MCP_TIMEOUT_ENV_KEYS:
            if os.environ.get(key):
                value = os.environ.get(key)
                break
    if value is None:
        return _CONTRACT_RUNTIME_MCP_TIMEOUT_DEFAULT_SECONDS
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return _CONTRACT_RUNTIME_MCP_TIMEOUT_DEFAULT_SECONDS
    if parsed <= 0:
        return _CONTRACT_RUNTIME_MCP_TIMEOUT_DEFAULT_SECONDS
    return max(
        _CONTRACT_RUNTIME_MCP_TIMEOUT_MIN_SECONDS,
        min(parsed, _CONTRACT_RUNTIME_MCP_TIMEOUT_MAX_SECONDS),
    )


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


def _contract_runtime_timeout_response(
    *,
    operation: str,
    timeout_seconds: int,
    result: dict,
) -> dict:
    is_submit = operation == "submit_line"
    write_disposition = "ambiguous" if is_submit else "not_written"
    retry_disposition = (
        "poll_authoritative_current_state_before_retry"
        if is_submit
        else "safe_to_retry_precheck"
    )
    retry_guidance = {
        "automatic_retry": False,
        "exact_once_intent": True,
        "poll_tool": "contract_runtime_current" if is_submit else "",
        "required_before_retry": (
            "authoritative_contract_runtime_current_state_poll"
            if is_submit
            else "none"
        ),
        "retry_when": (
            "execution_state_revision_unchanged_and_line_missing"
            if is_submit
            else "precheck_may_be_reissued"
        ),
        "do_not_retry_when": (
            "line_completed_or_execution_state_revision_changed"
            if is_submit
            else ""
        ),
    }
    return {
        "ok": False,
        "error": "request_timeout",
        "message": (
            f"ContractRuntime {operation} transport timed out after "
            f"{timeout_seconds} seconds."
        ),
        "operation": operation,
        "timeout_seconds": timeout_seconds,
        "effective_timeout_seconds": timeout_seconds,
        "legacy_timeout_seconds": _CONTRACT_RUNTIME_MCP_TIMEOUT_LEGACY_SECONDS,
        "transport_only_timeout": True,
        "transport_disposition": "ambiguous",
        "write_disposition": write_disposition,
        "disposition": write_disposition,
        "retry_disposition": retry_disposition,
        "retry_guidance": retry_guidance,
        "request_id": str(result.get("request_id") or ""),
    }


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
            operation_run_id = str(operation.get("run_id") or "").strip()
            operation_id = str(operation.get("operation_id") or "").strip()
            if operation_run_id == run_id or operation_id in {
                run_id,
                f"current-full:{run_id}",
            }:
                matched_operation = operation
                break
    if matched_operation is None and not run_id and len(operations) == 1:
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


def _ensure_current_full_reconcile_run_id(body: dict) -> dict:
    normalized = dict(body)
    run_id = str(normalized.get("run_id") or "").strip()
    if not run_id:
        run_id = f"current-full-mcp-{secrets.token_hex(8)}"
    normalized["run_id"] = run_id
    return normalized


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
            "action": "poll_graph_operations_queue_then_resume_same_run_id",
            "poll_tool": "graph_operations_queue",
            "status_tool": "graph_status",
            "retry_tool": "graph_current_full_reconcile",
            "safe_retry": False,
            "retry_disposition": "conditional_same_run_id_resume",
            "same_run_id_required": True,
            "retry_guidance": (
                "Poll graph_operations_queue for this run_id first. Do not replay "
                "while it is running. If the durable state is candidate_ready or "
                "failed before activation, call graph_current_full_reconcile with "
                "the exact same run_id so the server resumes instead of rebuilding."
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


def _worker_auth_from_env(args: dict) -> dict:
    """Add host-only worker auth at the HTTP boundary without mutating tool args."""

    enriched = dict(args)
    environment = {
        field: str(os.environ.get(env_key) or "").strip()
        for field, env_key in _WORKER_AUTH_ENV_FIELDS.items()
    }
    if not any(environment.values()):
        return enriched
    if not all(environment.values()):
        raise ValueError("worker host auth environment is incomplete")
    for field, value in environment.items():
        supplied = str(enriched.get(field) or "").strip()
        if supplied and supplied != value:
            raise ValueError("worker tool auth conflicts with the host environment")
        enriched[field] = value
    return enriched


def _worker_host_envelope_present() -> bool:
    return any(env_key in os.environ for env_key in _WORKER_AUTH_ENV_FIELDS.values())


def _tools_for_current_process() -> list[dict]:
    if not _worker_host_envelope_present():
        return TOOLS
    return [
        tool
        for tool in TOOLS
        if str(tool.get("name") or "") not in _WORKER_MCP_HOST_ONLY_TOOLS
    ]


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
            "backlog_id": {"type": "string"},
            "definition_hash": {"type": "string"},
            "instruction_bundle_hash": {"type": "string"},
            "execution_state_revision": {"type": "integer"},
            "runtime_guide_hash": {"type": "string"},
            "stage_id": {"type": "string"},
            "line_id": {"type": "string"},
            "evidence_kind": {"type": "string"},
            "line_instance_id": {"type": "string"},
            "lane_id": {
                "type": "string",
                "description": (
                    "Atomic worker lane identity projected by the Runtime "
                    "Context guide; the facade verifies it against the "
                    "authenticated worker context."
                ),
            },
            "contract_execution_id": {"type": "string"},
            "implementation_event_ref": {"type": "string"},
            "implementation_lineage_ref": {"type": "string"},
            "worker_implementation_lineage": {"type": "object"},
            "worker_commit_sha": {"type": "string"},
            "worker_session_id": {"type": "string"},
            "worker_id": {"type": "string"},
            "worker_slot_id": {"type": "string"},
            "filer_principal": {"type": "string"},
            "worker_transcript_ref": {"type": "string"},
            "worker_transcript_path": {"type": "string"},
            "harness_type": {
                "type": "string",
                "description": (
                    "Worker harness type. Required for "
                    "runtime_context_finish_time_worker_attestation; use 'codex' "
                    "for Codex app/CLI workers and copy it from the worker guide "
                    "copy_safe_body instead of hand-building the body."
                ),
            },
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
            "missing_files": {"type": "array", "items": {"type": "string"}},
            "requested_files": {"type": "array", "items": {"type": "string"}},
            "blocked_acceptance_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "graph_refs": {"type": "array", "items": {"type": "string"}},
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


def _runtime_context_session_token_reissue_auth_branches() -> list[dict[str, Any]]:
    """Return self-contained auth alternatives for host schema projection."""

    identity_fields = ("project_id", "runtime_context_id", "task_id")

    def branch(*proof_fields: str) -> dict[str, Any]:
        fields = (*identity_fields, *proof_fields)
        return {
            "type": "object",
            "required": list(fields),
            "properties": {
                field: {"type": "string", "minLength": 1}
                for field in fields
            },
        }

    return [
        branch("session_token_ref"),
        branch("fence_token", "session_token"),
    ]


def _contract_runtime_submit_line_schema_properties() -> dict[str, Any]:
    properties: dict[str, Any] = {
        "project_id": {"type": "string"},
        "backlog_id": {"type": "string"},
        "contract_execution_id": {"type": "string"},
        "timeout_seconds": {
            "type": "integer",
            "minimum": _CONTRACT_RUNTIME_MCP_TIMEOUT_MIN_SECONDS,
            "maximum": _CONTRACT_RUNTIME_MCP_TIMEOUT_MAX_SECONDS,
            "default": _CONTRACT_RUNTIME_MCP_TIMEOUT_DEFAULT_SECONDS,
            "description": (
                "MCP-to-governance transport timeout only; never forwarded in "
                "the ContractRuntime HTTP request body."
            ),
        },
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


def _contract_runtime_bypass_line_schema_properties() -> dict[str, Any]:
    return {
        "project_id": {"type": "string"},
        "backlog_id": {"type": "string"},
        "contract_execution_id": {"type": "string"},
        "bypass_identity": {"type": "string"},
        "stage_id": {"type": "string"},
        "line_id": {"type": "string"},
        "execution_state_revision": {"type": "integer"},
        "runtime_guide_hash": {"type": "string"},
        "diagnostic_backlog_id": {
            "type": "string",
            "description": (
                "Omit on the first bypass to create the root diagnostic. "
                "Afterward use only the server-advertised generation root; "
                "downstream gates never create another diagnostic row."
            ),
        },
        "diagnostic_priority": {"type": "string"},
        "classification": {"type": "string"},
        "reason": {"type": "string"},
        "decision": {"type": "string"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "graph_trace_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Canonical graph-query trace ids forwarded unchanged to the "
                "ContractRuntime bypass facade; evidence_refs are descriptive "
                "only and are never converted into graph authority."
            ),
        },
        "task_id": {"type": "string"},
        "phase": {"type": "string"},
        "commit_sha": {"type": "string"},
        "observer_route_token_ref": {"type": "string"},
        "route_token_ref": {"type": "string"},
        "observer_session_id": {"type": "string"},
        "qa_session_token": {
            "type": "string",
            "description": "Raw QA role token used only as X-Gov-Token; never forwarded as evidence body.",
        },
    }


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
        "target_project_root": {
            "type": "string",
            "description": (
                "Canonical target project/worktree root for runtime-context "
                "worker identity; copy this value into worker-guide, "
                "graph-query, and runtime-context write facades."
            ),
        },
        "target_graph_root": {
            "type": "string",
            "description": (
                "Alias for target_project_root accepted by "
                "allocation/runtime context."
            ),
        },
        "batch_id": {"type": "string"},
        "backlog_id": {"type": "string"},
        "contract_execution_id": {
            "type": "string",
            "description": (
                "Canonical ContractRuntime execution scope used to resolve a "
                "protected route_token_ref for this allocation."
            ),
        },
        "successor_contract_execution_id": {
            "type": "string",
            "description": (
                "Canonical successor ContractRuntime execution scope alias. "
                "Prefer the value projected by live ContractRuntime guidance."
            ),
        },
        "current_contract_execution_id": {
            "type": "string",
            "description": (
                "Canonical current ContractRuntime execution scope alias. "
                "Accepted without requiring a cex-shaped observer_command_id."
            ),
        },
        "chain_id": {"type": "string"},
        "parent_task_id": {"type": "string"},
        "root_task_id": {"type": "string"},
        "stage_task_id": {"type": "string"},
        "stage_type": {"type": "string"},
        "agent_id": {"type": "string"},
        "worker_id": {"type": "string"},
        "worker_slot_id": {
            "type": "string",
            "description": (
                "Canonical worker slot identity copied unchanged from "
                "parallel_branch_allocate_precheck."
            ),
        },
        "actor": {"type": "string"},
        "attempt": {"type": "integer"},
        "profile_requirements": {
            "type": "object",
            "description": (
                "Public worker profile authority copied unchanged from "
                "parallel_branch_allocate_precheck."
            ),
        },
        "retry_policy": {
            "type": "object",
            "description": (
                "Bounded retry authority copied unchanged from "
                "parallel_branch_allocate_precheck."
            ),
        },
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
        "branch_ref": {
            "type": "string",
            "description": (
                "Canonical worker branch ref copied unchanged from "
                "parallel_branch_allocate_precheck."
            ),
        },
        "base_commit": {"type": "string"},
        "target_head_commit": {"type": "string"},
        "merge_queue_id": {
            "type": "string",
            "description": (
                "Optional durable merge queue id. When omitted, "
                "parallel_branch_allocate generates and persists a canonical "
                "runtime_context.current_values.merge_queue_id before worker startup."
            ),
        },
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
        "acceptance_criteria": {
            "type": "array",
            "items": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "object"},
                ]
            },
            "description": (
                "Server-normalized acceptance scope copied unchanged from "
                "parallel_branch_allocate_precheck."
            ),
        },
        "allocation_precheck": {
            "type": "object",
            "description": (
                "Server-signed allocation precheck receipt. The complete object "
                "must be forwarded unchanged to parallel_branch_allocate."
            ),
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


def _parallel_branch_allocate_precheck_schema_properties() -> dict[str, Any]:
    lane_properties = {
        key: value
        for key, value in _parallel_branch_allocate_schema_properties().items()
        if key != "project_id"
    }
    return {
        "project_id": {"type": "string"},
        "lanes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 2,
            "description": (
                "One verified batch-child lane or two standalone atomic "
                "mf_parallel lanes. The read-only precheck resolves child "
                "route refs and returns canonical parallel_branch_allocate "
                "bodies."
            ),
            "items": {
                "type": "object",
                "properties": lane_properties,
                "required": [
                    "task_id",
                    "backlog_id",
                    "contract_execution_id",
                    "worker_id",
                    "route_token_ref",
                    "owned_files",
                ],
            },
        },
        "expected_lane_count": {"type": "integer", "enum": [1, 2]},
        "expected_worker_count": {"type": "integer", "enum": [1, 2]},
        "base_commit": {"type": "string"},
        "target_head_commit": {"type": "string"},
        "ref_name": {"type": "string"},
        "target_branch": {"type": "string"},
        "profile_requirements": {"type": "object"},
        "retry_policy": {"type": "object"},
    }


_MERGE_QUEUE_FLOW_VALUES = [
    "direct_fix",
    "hotfix",
    "mf_parallel",
    "mf_batch_parallel",
]

_ONBOARD_ROUTE_GUIDE_WORK_TYPE_VALUES = [
    "",
    "capability_query",
    "system_operation",
    "continue_contract_chain",
    "legacy_operator_recovery",
    "operator_supervised_direct_main",
    "direct_main",
    "direct_fix",
    "multi_backlog_parallel",
    "mf_batch_parallel",
    "parallel_worker",
    "mf_parallel",
    "qa_verification",
    "rollback_or_recover_contract",
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
            "description": (
                "Client hint; runtime_context.current_values.merge_queue_id "
                "and persisted queue rows are authoritative for mf_batch lanes. "
                "Ignore route-local merge_queue_id values returned by route issue."
            ),
        },
        "merge_queue_id": {
            "type": "string",
            "description": (
                "Authoritative batch merge_queue_id from runtime context/current "
                "queue read model, not a freshly issued route-token response. "
                "Rows blocked by a merged dependency missing graph epoch include "
                "copy-safe graph_epoch_recovery tool_args with this queue id."
            ),
        },
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
        "current_target_head": {"type": "string"},
        "latest_target_head": {
            "type": "string",
            "description": "Alias for current_target_head.",
        },
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
        "audited_postmerge_recovery": {
            "type": "object",
            "description": (
                "Immutable evidence refs for server-derived, system-only recovery "
                "of a durable queue row after an audited fast-forward already "
                "landed. This never accepts a raw fence or synthesizes PASS."
            ),
            "properties": {
                "source_contract_execution_id": {"type": "string"},
                "runtime_context_id": {"type": "string"},
                "independent_qa_receipt_ref": {"type": "string"},
                "manual_merge_event_ref": {"type": "string"},
                "diagnostic_backlog_id": {"type": "string"},
            },
            "required": [
                "source_contract_execution_id",
                "runtime_context_id",
                "independent_qa_receipt_ref",
                "manual_merge_event_ref",
                "diagnostic_backlog_id",
            ],
            "additionalProperties": False,
        },
        "worker_role": {"type": "string"},
        "status": {
            "type": "string",
            "description": (
                "Durable merge queue item status. Use queued_for_merge after worker "
                "finish gate and independent QA; this does not change the branch "
                "runtime context status."
            ),
        },
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
        "current_target_head",
        "evidence",
        "batch_status",
        "flow",
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
    latest_target_head = str(args.get("latest_target_head") or "").strip()
    if latest_target_head and not body.get("current_target_head"):
        body["current_target_head"] = latest_target_head
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
        "audited_postmerge_recovery",
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
    body = {
        key: value
        for key, value in args.items()
        if key != "project_id" and value not in (None, "", [], {})
    }
    body.setdefault("response_view", "compact")
    return body


# ---------------------------------------------------------------------------
# MCP protocol constants
# ---------------------------------------------------------------------------
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "aming-claw-governance"
SERVER_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
_BACKLOG_ACCEPTANCE_CRITERION_ITEM_SCHEMA = {
    "anyOf": [
        {"type": "string"},
        {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Stable acceptance criterion identifier.",
                },
                "description": {"type": "string"},
                "text": {"type": "string"},
                "required_scope": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "files",
                                "nodes",
                                "files_and_nodes",
                                "verification_only_external_dependency",
                                "unresolved",
                            ],
                        },
                        "files": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "node_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "nodes": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "dependency_id": {"type": "string"},
                        "dependency_ref": {"type": "string"},
                    },
                    "required": ["kind"],
                    "additionalProperties": True,
                },
            },
            "required": ["id", "required_scope"],
            "additionalProperties": True,
        },
    ],
    "description": (
        "Legacy free-text criterion or structured criterion with a stable id "
        "and required_scope for acceptance/file-fence closure."
    ),
}

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
        "name": "release_operator_head_queue",
        "description": (
            "Read or mutate the bounded release-operator queue. Mutations are "
            "authorized by an operator session or an opaque server-registered "
            "route_token_ref with separate authorization backlog/task scope, "
            "and are audited. Removing an active historical "
            "membership requires historical_non_schedulable=true, "
            "historical_execution_resume_allowed=false, and durable "
            "evidence_refs; active integration-epoch members remain protected."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project identifier.",
                },
                "action": {
                    "type": "string",
                    "enum": ["read", "insert", "reorder", "skip", "remove"],
                    "default": "read",
                },
                "backlog_id": {
                    "type": "string",
                    "description": "Exact queue member for insert, skip, or remove.",
                },
                "backlog_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exact full membership order for reorder.",
                },
                "position": {"type": "integer", "minimum": 1},
                "pinned": {"type": "boolean"},
                "reason": {
                    "type": "string",
                    "description": "Non-empty audit reason for queue mutation.",
                },
                "route_token_ref": {
                    "type": "string",
                    "description": (
                        "Opaque server-registered route token reference for "
                        "mutations; raw route/session tokens are not accepted."
                    ),
                },
                "authorization_backlog_id": {
                    "type": "string",
                    "description": (
                        "Backlog scope bound to route_token_ref; separate from "
                        "the queue member backlog_id."
                    ),
                },
                "authorization_task_id": {
                    "type": "string",
                    "description": "Task scope bound to route_token_ref.",
                },
                "historical_non_schedulable": {
                    "type": "boolean",
                    "description": (
                        "Explicit operator assertion required to remove an "
                        "active historical queue membership."
                    ),
                },
                "historical_execution_resume_allowed": {
                    "type": "boolean",
                    "description": (
                        "Must be false when removing an active historical "
                        "queue membership."
                    ),
                },
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Durable backlog, contract-runtime, timeline, request, "
                        "or operator evidence refs for historical removal."
                    ),
                },
            },
            "required": ["project_id"],
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
                "acceptance_criteria": {
                    "type": "array",
                    "items": _BACKLOG_ACCEPTANCE_CRITERION_ITEM_SCHEMA,
                },
                "chain_task_id": {"type": "string"},
                "commit": {"type": "string"},
                "discovered_at": {"type": "string"},
                "details_md": {"type": "string"},
                "chain_trigger_json": {"type": "object"},
                "fixed_at": {"type": "string"},
                "actor": {"type": "string"},
                "triage_action": {
                    "type": "string",
                    "enum": ["admit", "merge_into", "supersede", "reject_dup"],
                },
                "triage_target_bug_id": {"type": "string"},
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
                "qa_session_token": {
                    "type": "string",
                    "description": "Raw QA role token used only as X-Gov-Token; never forwarded into timeline evidence.",
                },
                "qa_session_token_ref": {
                    "type": "string",
                    "description": "Process-local opaque QA session ref. The standalone governance dispatcher rejects this ref fail-closed; use the managed MCP dispatcher that issued it.",
                },
                "route_token": {"type": "object", "description": "Route-token evidence required for protected close-gate timeline evidence."},
                "route_token_ref": {"type": "string", "description": "Opaque server-registered route token reference accepted by protected HTTP facades."},
                "route_waiver": {"type": "object", "description": "Explicit route-context-consuming waiver for protected route-token gates."},
                "route_token_waiver": {"type": "object", "description": "Alias for route_waiver."},
            },
            "required": ["project_id", "event_type"],
        },
    },
    {
        "name": "observer_direct_mutation_exception",
        "description": (
            "Append the canonical pre-mutation Direct Main exception from "
            "onboard_route_guide. This facade validates its exact event shape "
            "and route before delegating to the authoritative timeline writer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "backlog_id": {"type": "string"},
                "task_id": {"type": "string"},
                "event_type": {
                    "type": "string",
                    "enum": ["mf.observer_direct_implementation_exception"],
                },
                "event_kind": {
                    "type": "string",
                    "enum": ["observer_direct_implementation_exception"],
                },
                "phase": {"type": "string", "enum": ["pre_mutation"]},
                "status": {"type": "string", "enum": ["accepted"]},
                "decision": {
                    "type": "string",
                    "enum": ["operator_supervised_direct_main_approved"],
                },
                "actor": {"type": "string", "enum": ["observer"]},
                "payload": {"type": "object"},
                "verification": {"type": "object"},
                "artifact_refs": {"type": "object"},
                "route_token_ref": {
                    "type": "string",
                    "description": (
                        "Copy-safe opaque route reference from the active Direct "
                        "Main Guide; raw route credentials are not accepted."
                    ),
                },
            },
            "required": [
                "project_id",
                "backlog_id",
                "task_id",
                "event_type",
                "event_kind",
                "phase",
                "status",
                "decision",
                "actor",
                "payload",
                "verification",
                "artifact_refs",
                "route_token_ref",
            ],
            "additionalProperties": False,
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
                    "enum": list(_ONBOARD_ROUTE_GUIDE_WORK_TYPE_VALUES),
                    "description": "Requested work type for onboard routing.",
                },
                "requested_work_type": {
                    "type": "string",
                    "enum": list(_ONBOARD_ROUTE_GUIDE_WORK_TYPE_VALUES),
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
                "backlog_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "For mf_batch_parallel selection, the exact child rows "
                        "to bind into the copy-safe entry action_input."
                    ),
                },
                "task_id": {"type": "string"},
                "reason": {"type": "string"},
                "human_reason": {"type": "string"},
                "observer_session_id": {"type": "string"},
                "target_head_commit": {"type": "string"},
                "target_ref": {"type": "string"},
                "graph_snapshot_id": {"type": "string"},
                "preflight_mode": {"type": "string"},
                "metadata": {
                    "type": "object",
                    "properties": {
                        "required_worker_count": {
                            "type": "integer",
                            "enum": [1, 2],
                        }
                    },
                },
                "response_view": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "default": "compact",
                    "description": (
                        "Agent-facing MCP defaults to compact. Use full only "
                        "for explicit audit/debug compatibility."
                    ),
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "onboard_route_guide_section_fetch",
        "description": (
            "Fetch one to three named bounded advisory sections from an opaque "
            "onboard guide capsule. Missing, stale, or wrong-scope refs return "
            "deterministic compact-guide refresh instructions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "guide_capsule_ref": {"type": "string"},
                "sections": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "next_action",
                            "authority",
                            "action_input",
                            "role_guidance",
                            "runtime_identity",
                            "blockers",
                        ],
                    },
                    "maxItems": 3,
                },
                "backlog_id": {"type": "string"},
                "bug_id": {"type": "string"},
                "role": {"type": "string"},
                "actor_role": {"type": "string"},
                "work_type": {
                    "type": "string",
                    "enum": list(_ONBOARD_ROUTE_GUIDE_WORK_TYPE_VALUES),
                },
                "requested_work_type": {
                    "type": "string",
                    "enum": list(_ONBOARD_ROUTE_GUIDE_WORK_TYPE_VALUES),
                },
            },
            "required": ["project_id", "guide_capsule_ref", "sections"],
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
                "parent_contract_execution_id": {"type": "string"},
                "predecessor_contract_execution_id": {"type": "string"},
                "predecessor_execution_state_revision": {"type": "integer"},
                "predecessor_execution_state_hash": {"type": "string"},
                "successor_attempt_id": {"type": "string"},
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
            "allOf": [
                {
                    "if": {"required": ["successor_attempt_id"]},
                    "then": {
                        "required": [
                            "project_id",
                            "backlog_id",
                            "task_id",
                            "parent_contract_execution_id",
                            "predecessor_contract_execution_id",
                            "predecessor_execution_state_revision",
                            "predecessor_execution_state_hash",
                            "successor_attempt_id",
                            "actor",
                            "reason",
                            "route_token_ref",
                        ],
                        "propertyNames": {
                            "enum": [
                                "project_id",
                                "backlog_id",
                                "task_id",
                                "parent_contract_execution_id",
                                "predecessor_contract_execution_id",
                                "predecessor_execution_state_revision",
                                "predecessor_execution_state_hash",
                                "successor_attempt_id",
                                "actor",
                                "reason",
                                "route_token_ref",
                            ]
                        },
                    },
                }
            ],
        },
    },
    {
        "name": "mf_parallel_enter",
        "description": (
            "Enter source-backed mf_parallel successor runtime only after "
            "onboard_route_guide selects it as the next interface. The "
            "observer must explicitly select metadata.required_worker_count."
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
                "parent_batch_id": {
                    "type": "string",
                    "description": "Server-issued batch parent id copied from mf_batch_parallel per-row successor.",
                },
                "merge_queue_id": {
                    "type": "string",
                    "description": "Server-issued canonical merge queue id for a verified batch child.",
                },
                "merge_queue_item": {
                    "type": "object",
                    "description": "Server-issued durable queue item projection for a verified batch child.",
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
                "worker_fence": {"type": "object"},
                "owned_files": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "target_files": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "metadata": {
                    "type": "object",
                    "properties": {
                        "required_worker_count": {
                            "type": "integer",
                            "enum": [1, 2],
                        },
                    },
                    "required": ["required_worker_count"],
                },
            },
            "required": [
                "project_id",
                "reason",
                "metadata",
                "observer_session_id",
            ],
            "anyOf": [
                {"required": ["backlog_id"]},
                {"required": ["bug_id"]},
            ],
            "allOf": [
                {
                    "anyOf": [
                        {"required": ["route_token_ref"]},
                        {"required": ["observer_route_token_ref"]},
                    ]
                }
            ],
        },
    },
    {
        "name": "mf_parallel_revise",
        "description": (
            "Revise an observer-selected mf_parallel worker count before the "
            "first RuntimeContext allocation or dispatch. Ordinary re-enter "
            "cannot change the frozen selection."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "backlog_id": {"type": "string"},
                "contract_execution_id": {"type": "string"},
                "required_worker_count": {
                    "type": "integer",
                    "enum": [1, 2],
                },
                "reason": {"type": "string"},
                "human_reason": {"type": "string"},
                "observer_session_id": {"type": "string"},
                "route_token_ref": {"type": "string"},
                "observer_route_token_ref": {
                    "type": "string",
                    "description": (
                        "Opaque child-scoped observer route-token ref; raw "
                        "route tokens are not accepted."
                    ),
                },
            },
            "required": [
                "project_id",
                "backlog_id",
                "contract_execution_id",
                "required_worker_count",
                "reason",
                "observer_session_id",
            ],
            "anyOf": [
                {"required": ["route_token_ref"]},
                {"required": ["observer_route_token_ref"]},
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
                "schema_version": {
                    "type": "string",
                    "const": "onboard_route_guide.mf_batch_parallel_entry_input.v1",
                },
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
                "metadata": {
                    "type": "object",
                    "properties": {
                        "required_worker_count": {
                            "type": "integer",
                            "enum": [1, 2],
                        },
                        "batch_scope": {
                            "type": "string",
                            "const": "row_scoped_mf_parallel_successors",
                        },
                        "successor_contract_template_id": {
                            "type": "string",
                            "const": "mf_parallel.v2",
                        },
                        "nested_worker_fanout_supported": {
                            "type": "boolean",
                            "const": False,
                        },
                        "initial_two_worker_selection_supported": {
                            "type": "boolean",
                            "const": True,
                        },
                        "revision_to_two_workers_supported": {
                            "type": "boolean",
                            "const": False,
                        },
                    },
                    "required": ["required_worker_count"],
                },
            },
            "required": [
                "project_id",
                "backlog_ids",
                "reason",
                "observer_session_id",
                "target_head_commit",
                "graph_snapshot_id",
                "metadata",
            ],
            "anyOf": [
                {"required": ["backlog_id"]},
                {"required": ["bug_id"]},
            ],
            "allOf": [
                {
                    "anyOf": [
                        {"required": ["route_token_ref"]},
                        {"required": ["observer_route_token_ref"]},
                    ]
                }
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
        "name": "contract_runtime_recover",
        "description": (
            "Start the server-derived replacement execution for one stale "
            "pinned ContractRuntime using copy-safe observer session and "
            "route references. Raw authorization tokens are not accepted."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string"},
                "backlog_id": {"type": "string"},
                "recovery_policy": {
                    "type": "string",
                    "enum": [
                        "start_new_execution",
                        "invalid_runtime_context_authority",
                    ],
                },
                "stale_contract_execution_id": {"type": "string"},
                "recovery_authority_hash": {"type": "string"},
                "observer_session_id": {
                    "type": "string",
                    "description": "Opaque active observer session id.",
                },
                "observer_route_token_ref": {
                    "type": "string",
                    "description": (
                        "Opaque server-registered observer route ref; raw "
                        "route tokens are not accepted."
                    ),
                },
            },
            "required": [
                "project_id",
                "backlog_id",
                "recovery_policy",
                "stale_contract_execution_id",
                "observer_session_id",
                "observer_route_token_ref",
            ],
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
        "name": "contract_runtime_bypass_line",
        "description": (
            "Waive the current ContractRuntime line without claiming PASS. The "
            "first bypass roots one no-PASS diagnostic generation; downstream "
            "gates reuse that root and must record their own reason."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _contract_runtime_bypass_line_schema_properties(),
            "required": [
                "project_id",
                "contract_execution_id",
                "bypass_identity",
                "stage_id",
                "line_id",
                "execution_state_revision",
                "classification",
                "reason",
                "decision",
            ],
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
            "observer_route_token_ref or route_token_ref plus backlog_id and "
            "task_id/contract_execution_id; failures return public-safe "
            "route_proof_diagnostics and never require raw route tokens. If "
            "parallel_branch_merge_queue_status reports graph_epoch_recovery, "
            "pass its authoritative merge_queue_id and queue_item_id here; a "
            "successful current-full reconcile auto-records snapshot_id and "
            "projection_id on the matching merged durable queue item."
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
                "merge_queue_id": {
                    "type": "string",
                    "description": (
                        "Authoritative batch merge_queue_id for graph-epoch recovery; "
                        "used to narrow auto-recording after reconcile."
                    ),
                },
                "queue_item_id": {
                    "type": "string",
                    "description": (
                        "Authoritative durable queue_item_id for graph-epoch recovery; "
                        "used to narrow auto-recording after reconcile."
                    ),
                },
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
            "required": [
                "project_id",
                "runtime_context_id",
                "backlog_id",
                "definition_hash",
                "instruction_bundle_hash",
                "execution_state_revision",
                "runtime_guide_hash",
                "stage_id",
                "line_id",
                "evidence_kind",
                "line_instance_id",
            ],
        },
    },
    {
        "name": "runtime_context_scope_insufficiency_request",
        "description": (
            "Worker-authored append-only scope-insufficiency blocker. "
            "Records missing/requested files and blocked acceptance criteria "
            "without widening owned_files or granting authority."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _runtime_context_write_schema_properties(),
            "required": [
                "project_id",
                "runtime_context_id",
                "backlog_id",
                "task_id",
                "parent_task_id",
                "target_project_root",
                "missing_files",
                "requested_files",
                "blocked_acceptance_ids",
                "reason",
                "graph_refs",
            ],
        },
    },
    {
        "name": "runtime_context_worker_commit",
        "description": "Worker-authored canonical Runtime Context commit facade. Records the exact clean immutable HEAD in source-backed ContractRuntime after implementation evidence and before finish attestation.",
        "inputSchema": {
            "type": "object",
            "properties": _runtime_context_write_schema_properties(),
            "required": [
                "project_id",
                "runtime_context_id",
                "contract_execution_id",
                "task_id",
                "parent_task_id",
                "worker_session_id",
                "filer_principal",
                "worker_commit_sha",
                "owned_files",
                "changed_files",
                "graph_trace_ids",
            ],
        },
    },
    {
        "name": "runtime_context_finish_time_worker_attestation",
        "description": "Worker-authored canonical Runtime Context finish-time self-attestation facade. Consumes the exact source-backed ContractRuntime worker_commit and rejects later HEAD or worktree drift. Requires harness_type; Codex workers must send harness_type='codex' from the worker-guide copy_safe_body.",
        "inputSchema": {
            "type": "object",
            "properties": _runtime_context_write_schema_properties(),
            "required": ["project_id", "runtime_context_id", "harness_type"],
        },
    },
    {
        "name": "runtime_context_finish_gate",
        "description": "Canonical Runtime Context finish-gate facade that consumes the exact ContractRuntime worker_commit after finish-time attestation.",
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
        "name": "runtime_context_session_token_reissue",
        "description": "Runtime Context session-token rotation facade for expired or lost pre-startup mf_sub auth. Accepts either the server-projected copy-safe session_token_ref recovery proof (including a server-validated special-authority pre-lineage source normalized to the closed safe-ref capability) or matching raw session/fence proof, and never persists raw tokens.",
        "inputSchema": {
            "type": "object",
            "properties": _runtime_context_write_schema_properties(),
            "required": ["project_id", "runtime_context_id", "task_id"],
            "anyOf": _runtime_context_session_token_reissue_auth_branches(),
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
        "name": "parallel_branch_allocate_precheck",
        "description": (
            "Read-only cardinality-aware precheck for one verified batch-child "
            "lane or two standalone atomic lanes. Resolves child route identity, "
            "file fences, acceptance scope, commits, and repository-local "
            ".worktrees paths before any allocation write."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _parallel_branch_allocate_precheck_schema_properties(),
            "required": ["project_id", "lanes"],
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
            "read model without mutating refs. Durable rows must be "
            "queued_for_merge or merge_ready before live apply; materialized/noop "
            "status is not close-satisfying. If a merged dependency lacks graph "
            "epoch refs, rows include graph_epoch_recovery with copy-safe "
            "graph_current_full_reconcile arguments."
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
            "Mint an Aming-owned, write-authorizing observer route context "
            "without any external route provider. "
            "Authorizes observer orchestration/close actions and observer-prefilled "
            "child action-scope refs for QA-owned timeline evidence, but blocks "
            "direct file edits. The MCP result returns only a consumable "
            "route_token_ref plus copy-safe identity/diagnostics and never a raw "
            "route_token. For mf_batch lanes, any returned "
            "merge_queue_id is route-issue local diagnostic context; batch merge "
            "semantics use runtime_context.current_values.merge_queue_id."
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
    timeout_seconds: int | None = None,
) -> dict:
    timeout_kwargs = (
        {"timeout_seconds": timeout_seconds}
        if timeout_seconds is not None
        else {}
    )
    if gov_token:
        return _http(
            method,
            path,
            body,
            gov_token=gov_token,
            **timeout_kwargs,
        )
    return _http(method, path, body, **timeout_kwargs)


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------


def _mf_parallel_enter_project_id_rejection() -> dict[str, Any]:
    return {
        "ok": False,
        "error": "mf_parallel_enter_project_id_missing_or_empty",
        "code": "mf_parallel_enter_project_id_missing_or_empty",
        "field": "project_id",
        "expected": "non_empty_exact_project_id",
        "actual": "missing_or_empty",
        "guide": (
            "A server-issued per_row_successors[*].body must already include "
            "project_id; external callers must provide the exact project_id "
            "before calling mf_parallel_enter."
        ),
        "source": (
            "governance_mcp._dispatch_tool."
            "mf_parallel_enter_prewrite_gate"
        ),
        "host_correctable": True,
        "zero_write_rejection": True,
        "writes_performed": False,
    }


_RUNTIME_CONTEXT_IDENTITY_REQUIRED_TOOLS = frozenset(
    {
        "runtime_context_current",
        "runtime_context_worker_guide",
        "runtime_context_read_receipt",
        "runtime_context_implementation_evidence",
        "runtime_context_scope_insufficiency_request",
        "runtime_context_worker_commit",
        "runtime_context_finish_time_worker_attestation",
        "runtime_context_finish_gate",
        "runtime_context_session_token_initial_join",
        "runtime_context_session_token_reissue",
        "runtime_context_session_token_rejoin",
    }
)


def _runtime_context_required_argument_rejection(
    name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    if name not in _RUNTIME_CONTEXT_IDENTITY_REQUIRED_TOOLS:
        return {}
    required = ["project_id", "runtime_context_id"]
    if name == "runtime_context_finish_time_worker_attestation":
        required.append("harness_type")
    missing = [
        field
        for field in required
        if not str(args.get(field) or "").strip()
    ]
    if not missing:
        return {}
    return {
        "ok": False,
        "error": "invalid_request",
        "code": "mcp_tool_required_arguments_missing",
        "tool": name,
        "field": missing[0],
        "missing_fields": missing,
        "expected": "non_empty_required_arguments",
        "actual": "missing_or_empty",
        "source": "governance_mcp._dispatch_tool.pre_http_argument_gate",
        "zero_write_rejection": True,
        "writes_performed": False,
        "http_request_performed": False,
    }


def _dispatch_tool(name: str, args: dict) -> Any:
    if _worker_host_envelope_present() and name in _WORKER_MCP_HOST_ONLY_TOOLS:
        raise ValueError("host-only authentication tool is unavailable in worker MCP")
    """Dispatch a tools/call to the governance HTTP API."""
    args = dict(args or {})
    required_argument_rejection = _runtime_context_required_argument_rejection(
        name,
        args,
    )
    if required_argument_rejection:
        return required_argument_rejection
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

    if name == "release_operator_head_queue":
        pid = args["project_id"]
        action = str(args.get("action") or "read").strip().lower()
        path = f"/api/projects/{pid}/release-operator-head-queue"
        if action == "read":
            return _http("GET", path)
        body = {
            key: value
            for key, value in args.items()
            if key != "project_id" and value is not None
        }
        body["action"] = action
        return _http("POST", path, body)

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

    if name == "observer_direct_mutation_exception":
        pid = args["project_id"]
        return _http(
            "POST",
            f"/api/projects/{pid}/observer/direct-mutation-exception",
            _task_timeline_body(args),
        )

    if name == "task_timeline_append":
        pid = args["project_id"]
        qa_session_token_ref = str(
            args.get("qa_session_token_ref") or ""
        ).strip()
        if qa_session_token_ref:
            return {
                "ok": False,
                "error": "qa_session_token_ref_unavailable",
                "message": (
                    "Opaque QA session refs are process-local to the managed MCP "
                    "dispatcher; this standalone dispatcher cannot resolve them."
                ),
            }
        qa_session_token = str(args.get("qa_session_token") or "").strip()
        return _http_with_optional_gov_token(
            "POST",
            f"/api/task/{pid}/timeline",
            _task_timeline_body(args),
            gov_token=qa_session_token,
        )

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

    if name == "onboard_route_guide_section_fetch":
        pid = args["project_id"]
        return _http(
            "POST",
            f"/api/projects/{pid}/onboard-route-guide/capsule",
            {
                key: value
                for key, value in args.items()
                if key != "project_id" and value not in (None, "", [], {})
            },
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
        observer_session_id = str(
            args.get("observer_session_id") or ""
        ).strip()
        body = {
            key: value
            for key, value in args.items()
            if key not in {"project_id", "observer_session_id"}
            and value is not None
        }
        query = (
            "?"
            + urllib.parse.urlencode(
                {"observer_session_id": observer_session_id}
            )
            if observer_session_id
            else ""
        )
        return _http(
            "POST", f"/api/projects/{pid}/hotfix/enter{query}", body
        )

    if name == "mf_parallel_enter":
        if not str(args.get("project_id") or "").strip():
            return _mf_parallel_enter_project_id_rejection()
        pid = args["project_id"]
        body = {
            key: value
            for key, value in args.items()
            if key != "project_id" and value is not None
        }
        return _http("POST", f"/api/projects/{pid}/mf-parallel/enter", body)

    if name == "mf_parallel_revise":
        pid = args["project_id"]
        execution_id = urllib.parse.quote(
            str(args["contract_execution_id"]), safe=""
        )
        body = {
            key: value
            for key, value in args.items()
            if key not in {"project_id", "contract_execution_id"}
            and value is not None
        }
        return _http(
            "POST",
            f"/api/projects/{pid}/mf-parallel/{execution_id}/revise",
            body,
        )

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

    if name == "contract_runtime_recover":
        pid = args["project_id"]
        body = {
            key: args[key]
            for key in (
                "backlog_id",
                "recovery_policy",
                "stale_contract_execution_id",
                "recovery_authority_hash",
                "observer_session_id",
                "observer_route_token_ref",
            )
            if key in args and args[key] is not None
        }
        return _http(
            "POST",
            f"/api/projects/{pid}/contract-runtime/recover",
            body,
        )

    if name in {"contract_runtime_current", "contract_runtime_guide"}:
        pid = args["project_id"]
        execution_id = urllib.parse.quote(str(args["contract_execution_id"]), safe="")
        suffix = "guide" if name == "contract_runtime_guide" else "current-state"
        qa_session_token = str(args.get("qa_session_token") or "").strip()
        query = {
            "response_view": (
                "cli_guide" if name == "contract_runtime_guide" else "cli_current"
            ),
            **{
                key: value
                for key, value in args.items()
                if key
                in {"observer_session_id", "observer_session_ref", "observer_route_token_ref", "route_token_ref"}
                and value is not None
            },
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
        timeout_seconds = _contract_runtime_mcp_timeout_seconds(args)
        body = {
            key: value
            for key, value in args.items()
            if key
            not in {
                "project_id",
                "contract_execution_id",
                "qa_session_token",
                "timeout_seconds",
            }
            and value is not None
        }
        result = _http_with_optional_gov_token(
            "POST",
            f"/api/projects/{pid}/contract-runtime/{execution_id}/line-writes",
            body,
            gov_token=qa_session_token,
            timeout_seconds=timeout_seconds,
        )
        if _is_timeout_result(result):
            return _contract_runtime_timeout_response(
                operation="submit_line",
                timeout_seconds=timeout_seconds,
                result=result,
            )
        return result

    if name == "contract_runtime_bypass_line":
        pid = args["project_id"]
        execution_id = urllib.parse.quote(
            str(args["contract_execution_id"]), safe=""
        )
        qa_session_token = str(args.get("qa_session_token") or "").strip()
        body = {
            key: value
            for key, value in args.items()
            if key not in {"project_id", "contract_execution_id", "qa_session_token"}
            and value is not None
        }
        return _http_with_optional_gov_token(
            "POST",
            f"/api/projects/{pid}/contract-runtime/{execution_id}/line-bypasses",
            body,
            gov_token=qa_session_token,
        )

    if name == "contract_runtime_precheck_line":
        pid = args["project_id"]
        execution_id = urllib.parse.quote(str(args["contract_execution_id"]), safe="")
        qa_session_token = str(args.get("qa_session_token") or "").strip()
        timeout_seconds = _contract_runtime_mcp_timeout_seconds(args)
        body = {
            key: value
            for key, value in args.items()
            if key
            not in {
                "project_id",
                "contract_execution_id",
                "qa_session_token",
                "timeout_seconds",
            }
            and value is not None
        }
        result = _http_with_optional_gov_token(
            "POST",
            f"/api/projects/{pid}/contract-runtime/{execution_id}/line-writes/precheck",
            body,
            gov_token=qa_session_token,
            timeout_seconds=timeout_seconds,
        )
        if _is_timeout_result(result):
            return _contract_runtime_timeout_response(
                operation="precheck_line",
                timeout_seconds=timeout_seconds,
                result=result,
            )
        return result

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
        body = _ensure_current_full_reconcile_run_id(body)
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
        query = _runtime_context_query(_worker_auth_from_env(args))
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
        "runtime_context_scope_insufficiency_request",
        "runtime_context_worker_commit",
        "runtime_context_finish_time_worker_attestation",
        "runtime_context_finish_gate",
        "runtime_context_session_token_initial_join",
        "runtime_context_session_token_reissue",
        "runtime_context_session_token_rejoin",
    }:
        pid = args["project_id"]
        runtime_context_id = urllib.parse.quote(str(args["runtime_context_id"]), safe="")
        suffix_by_name = {
            "runtime_context_read_receipt": "read-receipts",
            "runtime_context_implementation_evidence": "implementation-evidence",
            "runtime_context_scope_insufficiency_request": (
                "scope-insufficiency-requests"
            ),
            "runtime_context_worker_commit": "worker-commit",
            "runtime_context_finish_time_worker_attestation": (
                "finish-time-worker-attestation"
            ),
            "runtime_context_finish_gate": "finish-gate",
            "runtime_context_session_token_initial_join": (
                "session-token/initial-join"
            ),
            "runtime_context_session_token_reissue": "session-token/reissue",
            "runtime_context_session_token_rejoin": "session-token/rejoin",
        }
        request_args = (
            _worker_auth_from_env(args)
            if name
            not in {
                "runtime_context_session_token_initial_join",
                "runtime_context_session_token_reissue",
                "runtime_context_session_token_rejoin",
            }
            else args
        )
        return _http(
            "POST",
            f"/api/graph-governance/{pid}/runtime-contexts/"
            f"{runtime_context_id}/{suffix_by_name[name]}",
            _runtime_context_write_body(request_args),
        )

    if name == "parallel_branch_allocate_precheck":
        pid = args["project_id"]
        body = {
            key: value
            for key, value in args.items()
            if key != "project_id" and value is not None
        }
        return _http(
            "POST",
            f"/api/graph-governance/{pid}/parallel-branches/allocation-precheck",
            body,
        )

    if name == "parallel_branch_allocate":
        pid = args["project_id"]
        preserve_signed_project_id = isinstance(
            args.get("allocation_precheck"), dict
        )
        body = {
            key: value
            for key, value in args.items()
            if value is not None
            and (key != "project_id" or preserve_signed_project_id)
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
        return _copy_safe_observer_route_context_issue_result(
            _http(
                "POST",
                f"/api/projects/{pid}/observer/route-context/issue",
                body,
            )
        )

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
        _response(req_id, {"tools": _tools_for_current_process()})
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
