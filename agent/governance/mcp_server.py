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


def _int_arg(args: dict, key: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(args.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


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
            "filer_principal": {"type": "string"},
            "worker_transcript_ref": {"type": "string"},
            "worker_transcript_path": {"type": "string"},
            "harness_type": {"type": "string"},
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


def _runtime_context_write_body(args: dict) -> dict:
    return {
        key: value
        for key, value in args.items()
        if key != "project_id" and value is not None
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
        "description": "Precheck whether an MF/observer backlog row has the required timeline evidence before backlog_close.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "bug_id": {"type": "string"},
                "view": {
                    "type": "string",
                    "enum": ["full", "compact", "repair"],
                    "description": "Response projection: full gate tree, compact gate summary, or advisory repair payloads.",
                },
                "include_events": {"type": "boolean", "description": "Include matching timeline rows in the response."},
                "limit": {"type": "integer", "description": "Maximum events to inspect/return, default 1000, max 1000"},
            },
            "required": ["project_id", "bug_id"],
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
        "description": "Worker-authored canonical Runtime Context finish-time self-attestation facade.",
        "inputSchema": {
            "type": "object",
            "properties": _runtime_context_write_schema_properties(),
            "required": ["project_id", "runtime_context_id"],
        },
    },
    {
        "name": "runtime_context_finish_gate",
        "description": "Canonical Runtime Context finish-gate facade that records mf_subagent_finish_gate evidence.",
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
                        "as route_id (route-* or event.route_prompt_context.preview), "
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
                    "description": "Explicit public parent route id (route-* or event.route_prompt_context.preview).",
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
]

# ---------------------------------------------------------------------------
# Governance HTTP client helpers
# ---------------------------------------------------------------------------

def _gov_url() -> str:
    return os.environ.get("GOVERNANCE_URL", "http://localhost:40000").rstrip("/")


def _gov_token() -> str:
    return os.environ.get("GOV_TOKEN", "")


def _http(method: str, path: str, body: dict | None = None) -> dict:
    """Make an HTTP request to the governance service."""
    url = f"{_gov_url()}{path}"
    data = json.dumps(body, ensure_ascii=False).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Gov-Token": _gov_token(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() if exc.fp else ""
        try:
            return json.loads(raw)
        except Exception:
            return {"error": str(exc), "body": raw}
    except Exception as exc:
        return {"error": str(exc)}


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
        qs = f"?{urllib.parse.urlencode(query)}" if query else ""
        return _http("GET", f"/api/backlog/{pid}/{bug_id}/timeline-gate{qs}")

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
        "runtime_context_implementation_evidence",
        "runtime_context_finish_time_worker_attestation",
        "runtime_context_finish_gate",
        "runtime_context_session_token_initial_join",
        "runtime_context_session_token_rejoin",
    }:
        pid = args["project_id"]
        runtime_context_id = urllib.parse.quote(str(args["runtime_context_id"]), safe="")
        suffix_by_name = {
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
