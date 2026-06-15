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
Non-preview governance work belongs to [skills/aming-claw/SKILL.md](../aming-claw/SKILL.md).
At the CLI, show `aming-claw launcher`. The launcher gives the next safe
action, not a long tutorial. Use `aming-claw status` to check health and
`aming-claw open` to open the dashboard after governance is running.

## Ground Rules

- Never start governance silently unless the user explicitly requested an
  end-to-end one-shot.
- Use governance on `http://127.0.0.1:40000`, not ServiceManager on `40101`.
- Treat ServiceManager/executor as advanced chain/ops readiness, not V1
  onboarding health.
- Ask before bootstrap unless the user requested initialize, register,
  bootstrap, or one-shot setup.
- Use `aming-claw plugin install` for local plugin setup or repair. After
  plugin install/update, ask for a new host session so skills and MCP load.
- Before observer or governed implementation work, verify the current host can
  list and read `aming-claw://current-context`. Governance health, dashboard
  health, and `aming-claw start` are not MCP readiness.
- If current-context is missing, stop normal governed work. Reload or open a
  new host session from a root whose MCP config points at the plugin checkout.
  This may be the plugin/workspace root with the repo-local relocatable
  `.mcp.json`, the installed plugin cache/host config, or a parent/workspace
  root with a host-local bridge `.mcp.json` whose absolute `cwd` points back to
  the plugin checkout. Verify current-context again; directory choice is only
  valid if the host can list and read `aming-claw://current-context`.
- Use HTTP/CLI fallback without current-context only for explicit
  system-recovery diagnosis or hotfix work.

## 治理边界：gate 是约束，不是对手

- Gate denial is a governance signal: repair the identity, route evidence, or
  missing precondition, or report the blocker with evidence.
- Do not treat the gate as an opponent to outmaneuver by reading gate internals
  or shaping inputs to satisfy implementation details.
- Do not self-authorize a role, route identity, session, token, or worker
  boundary that governance did not issue.
- Keep client hook enforcement and the server-side route/identity gate layered;
  document boundaries without adding reproducible bypass recipes.

## State Machine

Start by proving current host MCP context visibility:

```text
aming-claw://current-context
```

If `aming-claw://current-context` is unavailable, use `aming-claw status` or
`GET /api/health` only for launcher diagnosis. Do not continue into observer or
governed implementation work until the host session reloads and current-context
is visible.
After each state branch that reaches current-context visibility, run
[Fixed First-Value Steps](#fixed-first-value-steps).

### State A: Governance Offline

When governance health is missing, refused, or stale enough to be unsafe:

1. Show the explicit startup path: `aming-claw launcher`, then
   `aming-claw start`.
2. Explain that `aming-claw start` is long-running when it succeeds and should
   stay open in its own terminal. The root path `/` may return `404`; use
   `/dashboard`.
3. After the user starts it, verify service health with `aming-claw status` or
   `/api/health`.
4. Then verify MCP current-context visibility in the host session before
   observer/governed work, and re-enter the state machine at State B or State C.

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
| Server-minted first-run route binding | A narrow one-hour bootstrap permission governance creates only when the project is not registered yet. It lets first setup proceed without pasting a route token. |
| `route_token_ref` | An opaque handle to a server-stored route binding. It is safe to show; the raw route token is not persisted or displayed. |
| Route binding | The audit record that says which protected action, project, route context, and prompt contract a mutation is allowed to use. |
| `observer_route_token_refs` | The governance table that stores server-issued route-token references and digests for later validation. |

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
