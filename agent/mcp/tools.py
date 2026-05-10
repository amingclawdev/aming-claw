"""MCP Tool definitions and dispatch for Aming Claw.

All tools proxy to the governance HTTP API or the in-process worker pool.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool schema definitions (per MCP spec)
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    # --- Task Management ---
    {
        "name": "task_create",
        "description": "Create a new task in the governance queue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project identifier"},
                "prompt": {"type": "string", "description": "Task description/instructions"},
                "type": {"type": "string", "enum": ["pm", "dev", "test", "qa", "merge", "task"],
                         "description": "Task type (determines role and chain stage)"},
                "priority": {"type": "integer", "description": "Priority (1=highest)", "default": 5},
                "metadata": {"type": "object", "description": "Additional metadata (target_files, etc.)"},
            },
            "required": ["project_id", "prompt", "type"],
        },
    },
    {
        "name": "task_list",
        "description": "List tasks in a project, optionally filtered by status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "status": {"type": "string", "description": "Filter: queued, claimed, succeeded, failed"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "task_claim",
        "description": "Manually claim the next queued task (Observer takeover).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "worker_id": {"type": "string", "default": "observer"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "task_complete",
        "description": "Mark a task as complete (triggers auto-chain to next stage).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "task_id": {"type": "string"},
                "status": {"type": "string", "enum": ["succeeded", "failed"]},
                "result": {"type": "object", "description": "Task result (changed_files, test_report, etc.)"},
            },
            "required": ["project_id", "task_id", "status"],
        },
    },
    # --- Observer Control ---
    {
        "name": "observer_mode",
        "description": "Enable or disable observer mode. When enabled, all new tasks start as observer_hold and cannot be auto-claimed by executor or auto-chain.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "enabled": {"type": "boolean", "description": "True to enable, False to disable"},
            },
            "required": ["project_id", "enabled"],
        },
    },
    {
        "name": "task_hold",
        "description": "Put a queued task into observer_hold state — pauses executor pickup and auto-chain progression. Use before claiming a task for manual review.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "required": ["project_id", "task_id"],
        },
    },
    {
        "name": "task_release",
        "description": "Release a task from observer_hold back to queued — resumes normal executor and auto-chain flow.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "required": ["project_id", "task_id"],
        },
    },
    {
        "name": "task_cancel",
        "description": "Cancel a task (no auto-chain, no retry). Terminal state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "task_id": {"type": "string"},
                "reason": {"type": "string", "description": "Optional cancellation reason"},
            },
            "required": ["project_id", "task_id"],
        },
    },
    # --- Workflow / Nodes ---
    {
        "name": "wf_summary",
        "description": "Get workflow node status summary (pending/testing/t2_pass/qa_pass/waived counts).",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
        },
    },
    {
        "name": "wf_impact",
        "description": "Analyze impact of file changes on workflow nodes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "files": {"type": "string", "description": "Comma-separated file paths"},
            },
            "required": ["project_id", "files"],
        },
    },
    {
        "name": "node_update",
        "description": "Update verification status of workflow nodes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "nodes": {"type": "array", "items": {"type": "string"}, "description": "Node IDs"},
                "status": {"type": "string", "enum": ["pending", "testing", "t2_pass", "qa_pass", "failed", "waived"]},
                "evidence": {"type": "object", "description": "Evidence for the status change"},
            },
            "required": ["project_id", "nodes", "status"],
        },
    },
    # --- Backlog ---
    {
        "name": "backlog_list",
        "description": "List backlog bugs for a project, optionally filtered by status and priority.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "status": {"type": "string", "description": "Optional status filter, e.g. OPEN or FIXED"},
                "priority": {"type": "string", "description": "Optional priority filter, e.g. P1"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "backlog_get",
        "description": "Get one backlog bug by id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "bug_id": {"type": "string"},
            },
            "required": ["project_id", "bug_id"],
        },
    },
    {
        "name": "backlog_upsert",
        "description": "Create or update a backlog bug. Use this before MF/observer hotfix code changes.",
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
                "details_md": {"type": "string"},
                "commit": {"type": "string"},
                "fixed_at": {"type": "string"},
                "required_docs": {"type": "array", "items": {"type": "string"}},
                "provenance_paths": {"type": "array", "items": {"type": "string"}},
                "chain_trigger_json": {"type": "object"},
                "bypass_policy": {"type": "object"},
                "mf_type": {"type": "string"},
                "actor": {"type": "string"},
                "force_admit": {"type": "boolean"},
            },
            "required": ["project_id", "bug_id"],
        },
    },
    {
        "name": "backlog_close",
        "description": "Close a backlog bug as FIXED with commit evidence.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "bug_id": {"type": "string"},
                "commit": {"type": "string"},
                "actor": {"type": "string"},
            },
            "required": ["project_id", "bug_id"],
        },
    },
    # --- Graph Governance ---
    {
        "name": "graph_status",
        "description": "Get active graph snapshot status and pending scope reconcile summary.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "target_commit": {"type": "string"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "graph_operations_queue",
        "description": "Get the dashboard operations queue: semantic jobs, graph stale/scope reconcile, feedback, and patches.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "snapshot_id": {"type": "string"},
                "require_current_semantic": {"type": "boolean"},
                "include_status_observations": {"type": "boolean"},
                "include_resolved": {"type": "boolean"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "graph_query",
        "description": "Run an audited graph query. Preferred first step before implementing or inventing modules.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "tool": {
                    "type": "string",
                    "description": "Graph query tool, e.g. search_semantic, get_node, get_neighbors, search_docs, get_file_excerpt.",
                },
                "args": {"type": "object"},
                "snapshot_id": {"type": "string"},
                "actor": {"type": "string"},
                "query_source": {"type": "string"},
                "query_purpose": {"type": "string"},
                "repo_root": {"type": "string"},
                "project_root": {"type": "string"},
            },
            "required": ["project_id", "tool"],
        },
    },
    {
        "name": "graph_pending_scope_queue",
        "description": "Queue or update a pending scope-reconcile row for a target commit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "commit_sha": {"type": "string"},
                "target_commit_sha": {"type": "string"},
                "parent_commit_sha": {"type": "string"},
                "status": {"type": "string"},
                "snapshot_id": {"type": "string"},
                "evidence": {"type": "object"},
                "actor": {"type": "string"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "preflight_check",
        "description": "Run pre-flight self-check: system, version, graph, coverage, queue health.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "auto_fix": {"type": "boolean", "description": "Auto-fix recoverable issues (orphan nodes, stuck tasks)", "default": False},
            },
            "required": ["project_id"],
        },
    },
    # --- Executor ---
    {
        "name": "executor_status",
        "description": "Get worker pool status (workers, active tasks, etc.).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "executor_scale",
        "description": "Set the number of worker threads.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workers": {"type": "integer", "description": "Target worker count", "minimum": 0, "maximum": 10},
            },
            "required": ["workers"],
        },
    },
    # --- System ---
    {
        "name": "health",
        "description": "Check governance service health and version.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "version_check",
        "description": "Check if working tree is clean and HEAD matches CHAIN_VERSION. Returns ok, head, chain_version, dirty_files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project identifier"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "telegram_send",
        "description": "Send a message to Telegram via the bot.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Telegram chat ID"},
                "text": {"type": "string", "description": "Message text"},
            },
            "required": ["chat_id", "text"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

class ToolDispatcher:
    """Routes MCP tool calls to governance API or in-process worker pool."""

    def __init__(self, api_fn, worker_pool, service_mgr=None):
        """
        Args:
            api_fn: Callable(method, path, data) → dict (HTTP to governance)
            worker_pool: WorkerPool instance for executor tools (may be None)
            service_mgr: ServiceManager for executor subprocess lifecycle
        """
        self._api = api_fn
        self._pool = worker_pool
        self._svc = service_mgr

    def dispatch(self, name: str, args: dict) -> Any:
        args = dict(args or {})
        # --- Task tools ---
        if name == "task_create":
            pid = args["project_id"]
            body = {"prompt": args["prompt"], "type": args["type"]}
            if args.get("priority"):
                body["priority"] = args["priority"]
            if args.get("metadata"):
                body["metadata"] = args["metadata"]
            return self._api("POST", f"/api/task/{pid}/create", body)

        if name == "task_list":
            pid = args["project_id"]
            qs = f"?status={args['status']}" if args.get("status") else ""
            return self._api("GET", f"/api/task/{pid}/list{qs}")

        if name == "task_claim":
            pid = args["project_id"]
            wid = args.get("worker_id", "observer")
            return self._api("POST", f"/api/task/{pid}/claim", {"worker_id": wid})

        if name == "task_complete":
            pid = args["project_id"]
            body = {"task_id": args["task_id"], "status": args["status"]}
            if args.get("result"):
                body["result"] = args["result"]
            return self._api("POST", f"/api/task/{pid}/complete", body)

        # --- Observer tools ---
        if name == "observer_mode":
            pid = args["project_id"]
            return self._api("POST", f"/api/project/{pid}/observer-mode", {"enabled": args["enabled"]})

        if name == "task_hold":
            pid = args["project_id"]
            return self._api("POST", f"/api/task/{pid}/hold", {"task_id": args["task_id"]})

        if name == "task_release":
            pid = args["project_id"]
            return self._api("POST", f"/api/task/{pid}/release", {"task_id": args["task_id"]})

        if name == "task_cancel":
            pid = args["project_id"]
            return self._api("POST", f"/api/task/{pid}/cancel", {"task_id": args["task_id"], "reason": args.get("reason", "")})

        # --- Workflow tools ---
        if name == "wf_summary":
            return self._api("GET", f"/api/wf/{args['project_id']}/summary")

        if name == "wf_impact":
            return self._api("GET", f"/api/wf/{args['project_id']}/impact?files={args['files']}")

        if name == "node_update":
            pid = args["project_id"]
            body = {"nodes": args["nodes"], "status": args["status"]}
            if args.get("evidence"):
                body["evidence"] = args["evidence"]
            return self._api("POST", f"/api/wf/{pid}/verify-update", body)

        # --- Backlog tools ---
        if name == "backlog_list":
            pid = args["project_id"]
            query = {
                key: args[key]
                for key in ("status", "priority")
                if args.get(key)
            }
            qs = f"?{urllib.parse.urlencode(query)}" if query else ""
            return self._api("GET", f"/api/backlog/{pid}{qs}")

        if name == "backlog_get":
            pid = args["project_id"]
            bug_id = urllib.parse.quote(str(args["bug_id"]), safe="")
            return self._api("GET", f"/api/backlog/{pid}/{bug_id}")

        if name == "backlog_upsert":
            pid = args["project_id"]
            bug_id = urllib.parse.quote(str(args["bug_id"]), safe="")
            body = {
                key: value
                for key, value in args.items()
                if key not in {"project_id", "bug_id"} and value is not None
            }
            return self._api("POST", f"/api/backlog/{pid}/{bug_id}", body)

        if name == "backlog_close":
            pid = args["project_id"]
            bug_id = urllib.parse.quote(str(args["bug_id"]), safe="")
            body = {
                key: args[key]
                for key in ("commit", "actor")
                if args.get(key)
            }
            return self._api("POST", f"/api/backlog/{pid}/{bug_id}/close", body)

        # --- Graph governance tools ---
        if name == "graph_status":
            pid = args["project_id"]
            query = {key: args[key] for key in ("target_commit",) if args.get(key)}
            qs = f"?{urllib.parse.urlencode(query)}" if query else ""
            return self._api("GET", f"/api/graph-governance/{pid}/status{qs}")

        if name == "graph_operations_queue":
            pid = args["project_id"]
            query = {}
            for key in ("snapshot_id",):
                if args.get(key):
                    query[key] = args[key]
            for key in ("require_current_semantic", "include_status_observations", "include_resolved"):
                if key in args:
                    query[key] = "true" if args.get(key) else "false"
            qs = f"?{urllib.parse.urlencode(query)}" if query else ""
            return self._api("GET", f"/api/graph-governance/{pid}/operations/queue{qs}")

        if name == "graph_query":
            pid = args["project_id"]
            body = {
                key: value
                for key, value in args.items()
                if key != "project_id" and value is not None
            }
            body.setdefault("actor", "mcp")
            body.setdefault("query_source", "observer")
            body.setdefault("query_purpose", "prompt_context_build")
            return self._api("POST", f"/api/graph-governance/{pid}/query", body)

        if name == "graph_pending_scope_queue":
            pid = args["project_id"]
            body = {
                key: value
                for key, value in args.items()
                if key != "project_id" and value is not None
            }
            return self._api("POST", f"/api/graph-governance/{pid}/pending-scope", body)

        if name == "preflight_check":
            pid = args["project_id"]
            af = "true" if args.get("auto_fix") else "false"
            return self._api("GET", f"/api/wf/{pid}/preflight-check?auto_fix={af}")

        # --- Executor tools ---
        if name == "executor_status":
            result = {}
            if self._svc:
                result = self._svc.status()
            # R9: Include worker pool status if available
            if self._pool:
                pool_status = self._pool.status()
                result.update(pool_status)
            elif hasattr(self._svc, '_worker_pool_status'):
                result.update(self._svc._worker_pool_status())
            if not result:
                return {"mode": "external", "message": "No executor manager configured"}
            return result

        if name == "executor_scale":
            if self._svc:
                workers = args.get("workers", 1)
                if workers == 0:
                    self._svc.stop()
                    return {"action": "stopped"}
                else:
                    self._svc.start()
                    return self._svc.status()
            if self._pool:
                return self._pool.scale(args["workers"])
            return {"error": "No executor manager configured"}

        # --- System ---
        if name == "health":
            return self._api("GET", "/api/health")

        if name == "version_check":
            pid = args["project_id"]
            # Get chain_version from governance DB
            result = self._api("GET", f"/api/version-check/{pid}")
            # Enrich with git dirty check (MCP runs on host, has git)
            import subprocess, os
            workspace = os.environ.get("CODEX_WORKSPACE",
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            try:
                head = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    cwd=workspace, timeout=5
                ).decode().strip()
                result["head"] = head
                dirty = subprocess.check_output(
                    ["git", "diff", "--name-only"],
                    cwd=workspace, timeout=5
                ).decode().strip()
                if dirty:
                    result["dirty"] = True
                    result["dirty_files"] = [f for f in dirty.splitlines() if f.strip()]
                    result["ok"] = False
                    result["message"] = (result.get("message", "") + "; " if result.get("message") else "") + f"{len(result['dirty_files'])} uncommitted files"
                # Also check HEAD vs chain_version
                # B35: normalize short/full hash mismatch — short is a prefix of full.
                chain_ver = result.get("chain_version", "")
                if (chain_ver and chain_ver != "(not set)"
                    and not (head.startswith(chain_ver) or chain_ver.startswith(head))):
                    result["ok"] = False
                    commits = subprocess.check_output(
                        ["git", "log", "--oneline", f"{chain_ver}..HEAD"],
                        cwd=workspace, timeout=5
                    ).decode().strip().splitlines()
                    result["commits_since_chain"] = len(commits)
                    result["message"] = (result.get("message", "") + "; " if result.get("message") else "") + f"{len(commits)} manual commits"
            except Exception:
                pass  # fail-open if git unavailable
            return result

        # --- Telegram ---
        if name == "telegram_send":
            return self._send_telegram(args["chat_id"], args["text"])

        raise ValueError(f"Unknown tool: {name!r}")

    def _send_telegram(self, chat_id: str, text: str) -> dict:
        """Send message directly via Telegram Bot API."""
        import os
        import urllib.request
        import urllib.error
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            return {"error": "TELEGRAM_BOT_TOKEN not set"}
        import json as _json
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = _json.dumps({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return _json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return {"error": str(exc), "body": exc.read().decode()[:200]}
        except Exception as exc:
            return {"error": str(exc)}
