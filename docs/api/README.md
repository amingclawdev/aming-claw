# API Documentation

This directory contains API documentation for Aming Claw. For V1 users and AI
agents, prefer MCP tools when available. Use raw HTTP as a fallback or when a
dashboard action documents the HTTP contract.

## V1 API Surfaces

| Surface | Purpose |
| --- | --- |
| `GET /api/health` | Governance health on port `40000`. |
| `GET /api/projects` | List registered local projects. |
| `POST /api/project/bootstrap` | Explicitly register/bootstrap a target project and build its graph. |
| `GET /api/version-check/{project_id}` | Trailer-derived chain anchor, dirty-files state, and HEAD-vs-chain match (replaces the pre-2026-05-01 DB `chain_version` read). |
| `POST /api/version-sync/{project_id}` | Re-walk the trailer chain on the current git HEAD; used in Manual Fix after-commit. |
| `GET /api/projects/{project_id}/ai-config` | Read project AI routing, model catalog, and local CLI health. |
| `POST /api/projects/{project_id}/ai-config` | Update project AI routing without overwriting unrelated config. |
| `GET /api/graph-governance/{project_id}/status` | Active snapshot, stale state, semantic drift. |
| `POST /api/graph-governance/{project_id}/reconcile/pending-scope` | V1 Update Graph path for a clean HEAD. |
| `POST /api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic/jobs` | Queue targeted node/edge AI Enrich jobs. |
| `GET /api/graph-governance/{project_id}/operations/queue` | Operations Queue rows for dashboard/MCP. |
| `GET/POST /api/backlog/{project_id}` | Backlog list/upsert surfaces. |

Graph-native lookup is normally accessed through MCP `graph_query`, whose
subtools include `find_node_by_path`, `search_structure`, `function_index`,
`function_callers`, `function_callees`, `high_degree_nodes`, `search_semantic`,
`get_neighbors`, and `get_file_excerpt`.

## Documents

| File | V1 Status | Description |
| --- | --- | --- |
| [governance-api.md](governance-api.md) | Legacy/deep reference | Broad governance API guide. V1 users should prefer MCP tools (see [skill MCP guide](../../skills/aming-claw/references/mcp-tools.md)) and the table above; consult this file only when you need an endpoint that isn't surfaced through MCP, and double-check version-gate examples against §7 of [architecture.md](../architecture.md). |
| [executor-api.md](executor-api.md) | Experimental in V1 | Executor API guide for task execution and chain automation. Not required for the V1 dashboard/graph path. |

## Notes

- Dashboard and plugin sessions should not confuse ServiceManager port `40101`
  with governance API port `40000`.
- AI Enrich proposals are not trusted memory until Review Queue accept/reject
  completes and projection materializes the accepted result.
- Workflow acceptance graph APIs (`/api/wf/...`) are separate from the snapshot
  graph APIs used by the dashboard.
