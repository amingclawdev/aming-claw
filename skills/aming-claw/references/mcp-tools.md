# MCP Tool Guide

Prefer MCP tools over raw SQLite or hand-rolled HTTP calls when the tool exists. Raw HTTP is acceptable as a fallback when a tool is absent from the current client.

## Runtime And Health

- `health`: governance service health.
- `version_check`: HEAD, chain version, dirty files, and runtime match.
- `runtime_status`: combined governance, ServiceManager, and version state.
- `preflight_check`: system, version, graph, coverage, and queue baseline.

Use these at session start, after commits, and before closing a backlog row.

## AI Config And Local Runtime

Use the project AI config endpoint before queueing AI Enrich or claiming that
chain/executor work is ready:

- HTTP fallback: `GET /api/projects/{project_id}/ai-config`.
- Check `tool_health.openai` and `tool_health.anthropic` for local CLI probe
  status, path, runtime, command, and version.
- Check `project_config.ai.routing` for project-specific role/provider/model
  routes. The `semantic` route must have both provider and model before live AI
  semantic enrichment should run.
- Check `semantic.use_ai_default` for the semantic worker default and
  `model_catalog` for valid provider/model choices.

Provider mapping:

- `openai` -> Codex CLI, command `codex`, optional override `CODEX_BIN`.
- `anthropic` -> Claude Code CLI, command `claude`, optional override
  `CLAUDE_BIN`.

Status wording must separate command detection from real AI availability:

- `detected`: local command exists and version probe worked.
- `auth unknown`: version probe does not prove login or model-call success.
- `missing`: command or configured path is absent.
- `routing missing`: project semantic provider/model is unset; block AI Enrich
  and ask the user to configure AI config.
- `executor degraded`: ServiceManager/executor is unavailable, so automatic
  chain/executor work is not ready even if CLIs are detected.

Do not run a real Codex or Claude model call as a readiness check unless the
user explicitly asks; it may spend quota or trigger interactive login.

## Backlog

- `backlog_list`: find open rows by status/priority.
- `backlog_get`: inspect the selected row.
- `backlog_upsert`: create/update a row before code or doc mutations.
- `backlog_close`: close with commit evidence.

For MF work, use the backlog row as the single source of scope, target files, acceptance, and commit evidence.

**HTTP fallback when the MCP backlog tools are not registered on this client**
(observed 2026-05-10 — `mcp__aming-claw__backlog_*` not exposed by current
MCP server). Use governance HTTP routes directly:

- `GET  /api/backlog/{project_id}` — list (returns `{bugs: [...], count}`).
- `GET  /api/backlog/{project_id}/{bug_id}` — fetch one row.
- `POST /api/backlog/{project_id}/{bug_id}` — upsert. Body fields: `title`,
  `status` (`OPEN`/`FIXED`/`CLOSED`/...), `priority` (`P0..P3`),
  `mf_type`, `target_files` (semicolon-joined), `test_files`,
  `acceptance_criteria` (semicolon-joined sentences), `commit`,
  `fixed_at`, `details_md`. Pass `"force_admit": true` to skip the AI
  triage duplicate-check gate when filing a known/intentional row.
- `POST /api/backlog/{project_id}/{bug_id}/predeclare-mf` — pre-declare MF
  intent before the commit.
- `POST /api/backlog/{project_id}/{bug_id}/start-mf` — mark MF in progress.
- `POST /api/backlog/{project_id}/{bug_id}/close` — close with commit
  evidence after the MF lands.

**Do not "file" backlog by writing a markdown doc into `docs/dev/`** — the
canonical store is `backlog_bugs` table behind these routes. The
`docs/dev/manual-fix-current-*.md` files are session scratch notes, not the
backlog of record (and `docs/dev/` is gitignored, so they're not committed).

## Graph Governance

- `graph_status`: active snapshot, graph stale state, pending scope reconcile.
- `graph_operations_queue`: dashboard-ready operation rows and semantic queue status.
- `graph_query`: audited graph discovery. Start with `query_schema`, then use graph-native tools before filesystem scans:
  - `find_node_by_path`: resolve a path to owning nodes.
  - `search_structure`: search node id/title/kind/files/metadata/functions.
  - `function_index`: search `metadata.functions` and `metadata.function_lines`.
  - `degree_summary`: exact fan-in/fan-out and edge-type breakdown for a node.
  - `high_degree_nodes`: rank high fan-in/fan-out candidates.
  - `get_neighbors`: structural neighbors; pass `include_edge_semantic=true` for semantic edge projection payloads.
  - `search_semantic`: node semantics, node metadata, and current edge semantic projection.
  - `search_docs`, `get_node`, and `get_file_excerpt`: docs, exact node fetches, and bounded code excerpts.
- `graph_pending_scope_queue`: queue/update pending scope reconcile when HEAD and active graph diverge.

Example:

```json
{
  "project_id": "aming-claw",
  "tool": "query_schema"
}
```

```json
{
  "project_id": "aming-claw",
  "tool": "search_structure",
  "args": {"query": "language adapter", "limit": 10},
  "query_source": "observer",
  "query_purpose": "prompt_context_build"
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
