---
name: aming-claw
description: Use when working in the Aming Claw repo or any governance, dashboard, MCP, ServiceManager, backlog, graph, semantic reconcile, scope/full reconcile, chain, executor, or manual-fix/observer-hotfix task. Enforces graph-first discovery, backlog/MF tracking before mutations, MCP-first operations, Chain trailers on commits, and post-commit runtime/graph checks.
---

# Aming Claw

## Capabilities

Use Aming Claw as a local graph-first governance assistant. In a fresh session,
tell the user you can help with:

- Diagnose project governance state: runtime, ServiceManager, version, active snapshot, graph stale/current, pending scope reconcile, operations queue, semantic queue, and open backlog.
- Explore graph structure: layers, subsystems, features, hierarchy, node files, function indexes, neighbors, edge evidence, fan-in/fan-out, quality flags, orphan/low-relation signals, and doc/test coverage.
- Locate code precisely: resolve file paths to nodes, search module/title/file/function metadata, inspect `function_lines`, and fetch bounded file excerpts only after graph lookup.
- Rank PR opportunities: use graph evidence to identify high fan-out nodes, missing tests/docs, suspicious dependencies, semantic drift, review debt, and candidate refactor/test/doc issues.
- Generate evidence-backed backlog rows: include node ids, primary files, related functions, graph metrics, neighbors, risk, acceptance criteria, target files, and test files.
- Guide dashboard collaboration: use browser-use to inspect Projects, Graph tree, Inspector, Relations, Functions, Operations Queue, Review Queue, and Backlog as the same shared control plane the user sees.
- Run targeted semantic enrichment and review when requested: explain missing/current/hash-unverified/pending-review states, queue/cancel/retry semantics, and the difference between AI-proposed memory and user-approved memory.
- Drive advanced chain/dev/test/qa workflows only when explicitly needed; MVP work can stay local with graph, backlog, tests, and dashboard checks.

## Operating Contract

Treat the active graph as the project map and the backlog as the work ledger. Before editing code, docs, config, dashboard assets, or runtime state, establish current graph/runtime status, identify the owning nodes/modules, and record the work item.
For new features or user-visible behavior changes, treat E2E impact as part of the work ledger: run/update the relevant suite and evidence, or file an explicit follow-up backlog row when the E2E is deferred.
For dashboard/graph E2E work, update repo-owned fixture artifacts first and materialize them into isolated temporary projects; do not hand-edit generated example projects as the source of truth.

## Start Sequence

1. Confirm the workspace root and project id, normally `aming-claw`.
2. Check runtime health with MCP/HTTP: `health`, `version_check`, and `runtime_status` when available.
3. Check graph state: `graph_status` and `graph_operations_queue`.
4. Call `graph_query` with `tool=query_schema` to discover the live query contract.
5. Run graph-first discovery before implementation. Prefer `find_node_by_path`, `search_structure`, `function_index`, `degree_summary`, `high_degree_nodes`, `get_neighbors`, and `search_semantic` before broad filesystem scans. See [graph-first.md](references/graph-first.md).
6. Read or create the backlog row before any mutation. For MF/observer-hotfix work, predeclare/start the MF row first.
7. Inspect files only after graph discovery identifies likely owners and reusable modules.

## Visual AI Collaboration

Aming Claw dashboard is the shared cockpit for the user, AI session, and
governance system. When browser-use is available, open the dashboard to align
with what the user sees.

- Browser-use may navigate the graph tree, node inspector, Relations, Functions, Operations Queue, Review Queue, Projects, and Backlog.
- Cross-check visible dashboard state with MCP/Graph API results before drawing conclusions or recommending actions.
- Use dashboard state to explain graph health, stale/current state, semantic status, pending jobs, review proposals, and backlog/workflow state.
- Dashboard `vscode://file/...` links are for the human editor. Browser-use does not control VS Code directly.
- For AI-side code inspection, use graph `function_lines`, `get_file_excerpt`, and workspace tools.
- For governance actions, use MCP/Graph API. For code edits, use Codex workspace tools after the user has approved the work.

Recommended visual workflow:

1. Open dashboard and select the project.
2. Verify runtime, graph, operations queue, and semantic/review state.
3. Inspect candidate nodes or edges in the graph.
4. Use graph-native queries for precise node, edge, function, and file context.
5. Use bounded file excerpts or workspace reads only for the narrowed target.
6. File/update backlog before mutation, then implement and verify.

## Local Plugin Launcher

When the user asks for a local plugin entrypoint or the governance runtime is
offline, offer the explicit launcher flow instead of auto-starting services:

```text
aming-claw launcher
aming-claw start
```

The generated launcher artifact is an HTML guide with the dashboard link and
copyable commands. It must not execute local commands from a browser button;
service startup remains an explicit MCP/CLI action.

## Mutation Rules

- Prefer MCP tools over raw DB access or ad hoc HTTP when a tool exists. See [mcp-tools.md](references/mcp-tools.md).
- Never write directly to `governance.db` for normal operations.
- Use existing graph-owned modules/adapters before creating a new abstraction.
- Keep manual fixes small and tied to one backlog row.
- Commit with Chain trailers:

```text
Chain-Source-Stage: observer-hotfix
Chain-Project: aming-claw
Chain-Bug-Id: <backlog-id>
```

## Verification

Before closing a row:

1. Run focused tests or validation for the touched surface.
2. Run `git diff --check`.
3. Commit explicit files only.
4. Restart/redeploy governance or ServiceManager when runtime code changed.
5. Re-run `version_check` and confirm runtime matches HEAD.
6. Check graph status and operations queue; if graph is stale, queue/perform scope reconcile before claiming dashboard state is current.
7. Confirm E2E impact is current, deferred with a backlog row, or explicitly not applicable.
8. Close the backlog row with commit evidence.

## References

- [graph-first.md](references/graph-first.md): graph discovery playbook and reuse rule.
- [mf-sop.md](references/mf-sop.md): short MF checklist; canonical SOP remains `docs/governance/manual-fix-sop.md`.
- [mcp-tools.md](references/mcp-tools.md): MCP tool family guide and common payloads.
- [plugin-packaging.md](references/plugin-packaging.md): repo-local plugin layout and publish cautions.
