---
name: aming-claw-launcher
description: Explicit Aming Claw onboarding and launcher entry. Use for `/aming-claw:aming-claw-launcher`, "aming-claw launcher", "onboard me to Aming Claw", "start/open dashboard/status", install-and-start requests, fresh project registration, or any onboarding question. Defer graph, backlog, semantic, manual-fix, or chain implementation work to `/aming-claw:aming-claw` after this state machine reaches the fixed first-value steps.
---

# Aming Claw Launcher

This is the single explicit onboarding entry. Do not create or route to a
second onboarding skill. In Claude Code the memorable command is:

```text
/aming-claw:aming-claw-launcher
```

Keep this skill short and stateful. Full bootstrap schema, route-gate behavior,
and project-config details live in [docs/onboarding.md](../../docs/onboarding.md).
At the CLI, show `aming-claw launcher`. The launcher gives the next safe
action, not a long tutorial.

## Ground Rules

- Never start governance silently unless the user explicitly requested an
  end-to-end one-shot.
- Use governance on `http://127.0.0.1:40000`, not ServiceManager on `40101`.
- Treat ServiceManager/executor as advanced chain/ops readiness, not V1
  onboarding health.
- Ask before bootstrap unless the user requested initialize, register,
  bootstrap, or one-shot setup.
- After plugin install/update, ask for a new host session so skills and MCP load.

## State Machine

Start by reading current context when available:

```text
aming-claw://current-context
```

If MCP is unavailable, use `aming-claw status` or `GET /api/health`.
After each state branch, run [Fixed First-Value Steps](#fixed-first-value-steps).

### State A: Governance Offline

When health/current-context is missing, refused, or stale enough to be unsafe:

1. Show the explicit startup path: `aming-claw launcher`, then
   `aming-claw start`.
2. Explain that `aming-claw start` is long-running when it succeeds and should
   stay open in its own terminal. The root path `/` may return `404`; use
   `/dashboard`.
3. After the user starts it, verify with `aming-claw status`, MCP
   `runtime_status(project_id="<project_id>")`, or `/api/health`.
4. Then re-enter the state machine at State B or State C.

### State B: Governance Running, Workspace Unregistered

When governance is healthy but `GET /api/projects` does not include the active
target workspace:

1. Refuse to bootstrap the Aming Claw plugin checkout itself. Stop bootstrapping
   that root if it contains `.codex-plugin/`, `.claude-plugin/`,
   `shared-volume/codex-tasks/`, or `.mcp.json` that points to
   `--project aming-claw`. Ask for the real project root or cleanup first. If a
   selected/registered project exists, still run Fixed First-Value Steps.
2. Inspect or ask the user to confirm excludes before graph build. Start with
   `node_modules`, `dist`, `build`, `.expo`, `.next`, and `coverage`, then add
   project-local generated, vendored, nested, fixture, scratch, or downloaded
   asset roots.
3. Check the target git worktree. If dirty, ask the user to commit/stash before
   bootstrap because graph snapshots are commit-bound.
4. Use the Lane 1 first-run path from [docs/onboarding.md](../../docs/onboarding.md):
   the dashboard Projects bootstrap or
   `POST http://127.0.0.1:40000/api/project/bootstrap`. First registration may
   use the server-minted first-run route binding; do not use an old ungated CLI
   side door.
5. Tell the user to wait for graph build. It can take a bit; watch the Projects
   page or Operations Queue until an active snapshot appears or an actionable
   error is shown.

### State C: Project Already Registered

When the active project is registered and governance is healthy:

1. Open the dashboard for the selected project.
2. If the graph is stale, make "Update Graph / Reconcile" the first action.
3. If the graph is current, offer one first-value path:
   Check Current Project Status, Find PR Opportunities, or Explain Graph
   Concepts.
4. Keep any concept answer scoped to the selected concept first: graph, node,
   edge, snapshot, semantic enrichment, backlog, or browser collaboration.

## Fixed First-Value Steps

Every branch finishes with the same three steps:

1. Give the dashboard URL:

   ```text
   http://localhost:40000/dashboard?project_id=<project_id>&view=projects
   ```

   Use `view=graph` after an active snapshot exists.

2. Run exactly one graph query and show its audit `trace_id`:

   ```json
   {
     "project_id": "<project_id>",
     "tool": "list_features",
     "args": {"limit": 10, "compact": true},
     "query_source": "observer",
     "query_purpose": "prompt_context_build"
   }
   ```

   If the user asked about a specific file, use `find_node_by_path` instead.
   Do not invent trace ids.

3. File the first backlog row from the finding or user goal. Include target
   files, graph evidence, risk, acceptance criteria, and test/E2E decision.
   Use the dashboard or MCP backlog tool; defer implementation to the main
   `/aming-claw:aming-claw` skill.

## Jargon Translation

| Runtime term | User-facing translation |
| --- | --- |
| `runtime_match: false` | The running governance service does not match this checkout/version closely enough. Restart/reload/update before relying on new behavior. |
| `chain_rescue` | The audited Manual Fix bucket. It does not mean ordinary V1 implementation must use experimental auto-chain execution. |
| `node_missing` / `edge_missing` drift counts | The current graph/projection is missing expected nodes or relations. Run Update Graph/reconcile or review the queue before trusting impact claims. |
| Snapshot ids such as `scope-f17ca39-b610` | Opaque commit-bound graph snapshot pointers. They are audit ids, not branch names or files to edit. |
| `recommended_actions` | Governance suggestions for the next operator step. They are not automatic permission to mutate state. |

## One-Shot Install Mode

Only enter one-shot mode when the prompt explicitly says "install and start",
"install and open dashboard", "one-shot install", "full install", or equivalent.
Run install, runtime setup, health polling, and dashboard open with tool calls.
If the user only asks to install, do not start governance or open the dashboard.
Afterward, say the dashboard can work now, but skills and MCP tools require a
new host session or reload.

## Hand Off

Use `/aming-claw:aming-claw` for graph discovery beyond the one first query,
backlog mutation details, Manual Fix, Chain trailers, semantic/review work,
drift analysis, and implementation. Keep launcher changes out of
`governance.db`, graph state, merge queues, and release gates.
