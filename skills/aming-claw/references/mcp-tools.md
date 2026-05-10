# MCP Tool Guide

Prefer MCP tools over raw SQLite or hand-rolled HTTP calls when the tool exists. Raw HTTP is acceptable as a fallback when a tool is absent from the current client.

## Runtime And Health

- `health`: governance service health.
- `version_check`: HEAD, chain version, dirty files, and runtime match.
- `runtime_status`: combined governance, ServiceManager, and version state.
- `preflight_check`: system, version, graph, coverage, and queue baseline.

Use these at session start, after commits, and before closing a backlog row.

## Backlog

- `backlog_list`: find open rows by status/priority.
- `backlog_get`: inspect the selected row.
- `backlog_upsert`: create/update a row before code or doc mutations.
- `backlog_close`: close with commit evidence.

For MF work, use the backlog row as the single source of scope, target files, acceptance, and commit evidence.

## Graph Governance

- `graph_status`: active snapshot, graph stale state, pending scope reconcile.
- `graph_operations_queue`: dashboard-ready operation rows and semantic queue status.
- `graph_query`: audited graph discovery. Use `search_semantic`, `search_docs`, `get_node`, `get_neighbors`, and `get_file_excerpt`.
- `graph_pending_scope_queue`: queue/update pending scope reconcile when HEAD and active graph diverge.

Example:

```json
{
  "project_id": "aming-claw",
  "tool": "search_docs",
  "args": {"query": "manual fix graph-first", "limit": 5}
}
```

## Workflow And Nodes

- `wf_summary`: node verification summary.
- `wf_impact`: impacted nodes for target files.
- `node_update`: update node verification status with evidence only after real verification.

## Tasks And Observer

- `task_create`, `task_list`, `task_claim`, `task_complete`, `task_cancel`.
- `task_hold`, `task_release`, `observer_mode`.

Use observer controls for review/takeover flows. Preserve task metadata when manually completing or re-creating chain stages.

## ServiceManager And Executor

- `manager_health`: ServiceManager sidecar status.
- `manager_start`: fixed bootstrap facade. Do not request takeover from MCP; run takeover from an external ops shell when needed.
- `governance_redeploy`: redeploy governance through ServiceManager.
- `executor_respawn`: ask ServiceManager to respawn the external executor.
- `executor_status` and `executor_scale`: only manage MCP-local workers when the MCP server was intentionally started with workers.

Normal editor/plugin MCP sessions should use `--workers 0`. The executor is owned by ServiceManager, not by ad hoc MCP sessions.
