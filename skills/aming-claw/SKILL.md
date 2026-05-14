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
- Onboard new users with a Codex-rendered launcher MVP: dashboard link, project initialization path, browser collaboration entry, graph concepts, backlog workflow, and safe startup commands.
- Run targeted semantic enrichment and review when requested: explain missing/current/hash-unverified/pending-review states, queue/cancel/retry semantics, and the difference between AI-proposed memory and user-approved memory.
- Drive advanced chain/dev/test/qa workflows only when explicitly needed; MVP work can stay local with graph, backlog, tests, and dashboard checks.

## Operating Contract

Treat the active graph as the project map and the backlog as the work ledger. Before editing code, docs, config, dashboard assets, or runtime state, establish current graph/runtime status, identify the owning nodes/modules, and record the work item.
For new features or user-visible behavior changes, treat E2E impact as part of the work ledger: run/update the relevant suite and evidence, or file an explicit follow-up backlog row when the E2E is deferred.
For dashboard/graph E2E work, update repo-owned fixture artifacts first and materialize them into isolated temporary projects; do not hand-edit generated example projects as the source of truth.

## Manual Fix SOP

Use the manual-fix SOP for observer-hotfix, chain rescue, and other bypass
work where normal chain execution is not the right path. The canonical SOP is
`docs/governance/manual-fix-sop.md`; the compact session checklist is
`aming-claw://mf-sop`.

Before editing:

1. Confirm the MF route is justified.
2. Ensure the backlog row exists with target files, acceptance criteria, and
   details.
3. Predeclare/start the MF row when the MCP/HTTP surface is available.
4. Capture baselines: `git status`, `version_check`, `preflight_check`,
   `graph_status`, `graph_operations_queue`, and `wf_impact` for target files.
5. Run graph-first discovery and record the owning nodes or why the file is not
   graph-mapped.
6. Record the E2E decision: run it, defer it with a follow-up backlog row, or
   mark it `e2e_not_applicable` with a reason.

Commit explicit files only, and use Chain trailers for true MF commits:

```text
Chain-Source-Stage: observer-hotfix
Chain-Project: aming-claw
Chain-Bug-Id: <backlog-id>
```

After commit, re-check version/runtime and graph state, reconcile if the graph
is stale, then close the backlog row with commit and verification evidence.

## Start Sequence

1. Confirm the workspace root and project id, normally `aming-claw`.
2. Check runtime health with MCP/HTTP: `health`, `version_check`, and `runtime_status` when available.
3. Check graph state: `graph_status` and `graph_operations_queue`.
4. If governance is offline or this is a fresh install, read `aming-claw://seed-graph-summary` for packaged MVP structure before asking the user to start services.
5. Call `graph_query` with `tool=query_schema` to discover the live query contract.
6. Run graph-first discovery before implementation. Prefer `find_node_by_path`, `search_structure`, `function_index`, `degree_summary`, `high_degree_nodes`, `get_neighbors`, and `search_semantic` before broad filesystem scans. See [graph-first.md](references/graph-first.md).
7. Read or create the backlog row before any mutation. For MF/observer-hotfix work, predeclare/start the MF row first.
8. Inspect files only after graph discovery identifies likely owners and reusable modules.

## Fresh Session Launcher

On the first Aming Claw skill load in a fresh session, show a short
Codex-rendered launcher block before deep work. This is the MVP for onboarding
buttons until the dashboard/plugin frontend owns native controls.

First read `aming-claw://current-context` when available. If that resource is
missing or governance is offline, continue safely with the offline launcher
state instead of auto-starting services.

The launcher should be status-aware:

- If governance is online, include the dashboard link, project id, runtime
  version, graph stale state, operations queue count, and open backlog count
  when known.
- If the active project differs from the plugin default project, call that out
  before recommending graph or backlog actions.
- If governance is offline or current context is missing, show the explicit
  startup flow:

  ```text
  aming-claw launcher
  aming-claw start
  ```

- If no active graph exists for the selected project, make "Initialize Project"
  the primary next action.
- If graph is stale, make "Review Impact / Reconcile Graph" the primary next
  action.
- If graph is current, keep the primary actions to three: "Check Current
  Project Status", "Find PR Opportunities", and "Explain Graph Concepts".

Codex-owned MVP behavior:

- Render the launcher as a compact Markdown action panel with button-like
  labels and links/copyable commands. If the host app supports interactive
  choice buttons, use them; otherwise ask the user to click the link or reply
  with the action label.
- Prefer the `## Primary Next Actions` emitted by
  `aming-claw://current-context` or `aming-claw://project/<project_id>/context`;
  do not expand it into a long menu unless the user asks.
- Keep the panel short enough to fit above the fold. Do not replace normal
  task work with a long tutorial.
- Show it once per fresh session unless the user asks for help, onboarding, or
  the launcher again.
- For concept actions, explain only the selected concept first: graph, node,
  edge, snapshot, semantic enrichment, backlog, or browser collaboration.

Suggested action labels:

- Check Current Project Status
- Find PR Opportunities
- Explain Graph Concepts
- Initialize Project
- Update Graph

Do not silently start services. Browser or dashboard buttons may open URLs or
copy commands, but must not execute local shell commands.

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

When the user asks for a local plugin entrypoint, onboarding help, or the
governance runtime is offline, offer the explicit launcher flow instead of
auto-starting services:

```text
aming-claw launcher
aming-claw start
```

The generated launcher artifact may be a Codex-rendered Markdown panel for MVP
or an HTML/dashboard guide in later iterations. It may include:

- Dashboard link.
- Project selector or project id.
- Runtime and graph status summary.
- Copyable startup commands.
- Onboarding actions for initialization, graph concepts, backlog, and browser
  collaboration.

It must not execute local commands from a browser button; service startup
remains an explicit MCP/CLI action.

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
