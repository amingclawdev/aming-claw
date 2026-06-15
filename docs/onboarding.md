# Aming Claw V1 Onboarding Guide

This guide describes the V1 path for using Aming Claw to govern another local
project. V1 is local-first: start governance, open the dashboard, explicitly
register a clean target project, build the graph, then use dashboard/MCP tools
to inspect, file backlog, and plan PR work.

The single user-invocable onboarding entry is
`/aming-claw:aming-claw-launcher` in Claude Code or `aming-claw launcher` at
the CLI. The launcher skill is the compact state machine; this document remains
the full schema and first-run route-gate source.

The packaged Claude and Codex plugin manifests also install a client-side
`PreToolUse` hard guard that runs `python agent/hooks/onboarding_guard.py`
before protected governance or dispatch actions. This is a client hard rail, not
a replacement for the server-side route/identity gate. It cannot intercept
direct `curl` or other HTTP calls to governance made outside the CLI hook path;
the client hook and server gate are layered controls, and neither one is
sufficient by itself.

## 1. Start Aming Claw

Install from the repository or plugin flow in [README.md](../README.md), then
verify the install before starting governance:

```bash
aming-claw plugin doctor
```

The doctor is read-only. It reports missing CLI, marketplace, manifest, or MCP
config issues; fix anything that reads `fail` before continuing.
It does not check ServiceManager by default because ServiceManager/executor are
advanced chain/ops surfaces, not V1 onboarding requirements.

Then start governance in a separate terminal:

```bash
aming-claw start
```

Open:

```text
http://localhost:40000/dashboard
```

The root path `/` is not the dashboard and may return `404`.

## 2. Verify Host MCP Context

Before a new Codex or Claude Code observer session does observer or governed
implementation work, verify the host session can see Aming Claw MCP current
context:

1. List MCP resources.
2. Confirm `aming-claw://current-context` is present.
3. Read `aming-claw://current-context` and use its project id, governance URL,
   and guardrails as the session anchor.

Governance health, `/api/health`, and a working `/dashboard` prove the service
can answer HTTP. They do not prove the AI host loaded `.mcp.json`, exposed the
Aming Claw MCP server, or has route/current-context visibility.

If `aming-claw://current-context` is missing, stop normal governed work. Reload
or open a new Codex/Claude host session from a root whose MCP config actually
points at the plugin checkout. That can be the plugin/workspace root with the
source-controlled, relocatable `.mcp.json`; it can also be a parent/workspace
root with a host-local bridge `.mcp.json` whose absolute `cwd` points back to
the plugin checkout. The readiness test is not which directory looks correct;
it is whether the new host session lists and can read
`aming-claw://current-context`. Use `aming-claw plugin doctor` to inspect the
repo-local config, installed plugin cache, and any parent bridge. HTTP/CLI
fallback is only for an explicit system-recovery hotfix or diagnosis, not
ordinary governed implementation.

## 3. Register A Target Project

Project registration is explicit. Do not silently register a workspace just
because `/api/projects` is empty.

Use the dashboard Projects page first:

1. Choose or paste the target workspace path.
2. Review the exclude-path field. Confirm which generated, vendored, nested, or
   tool-owned directories should be excluded before graph build.
3. Give it a project name/id.
4. Click Bootstrap or Build graph.
5. Watch progress in Projects/Operations Queue.

Bootstrap mutates Aming Claw governance state: it writes project registry/DB
rows, scans the workspace, and creates a commit-bound graph snapshot. It uses
governance on port `40000`, not ServiceManager on `40101`.

API fallback:

```http
POST http://127.0.0.1:40000/api/project/bootstrap
```

This is the Lane 1 first-run bootstrap path. Do not use old ungated CLI or DB
side doors for first registration; the governance API/dashboard path records
the project-bootstrap route binding and graph-build evidence.

Request body schema:

```json
{
  "workspace_path": "/absolute/path/to/project",
  "project_id": "my-project",
  "project_name": "My Project",
  "language": "python",
  "scan_depth": 3,
  "exclude_patterns": ["node_modules", "dist", "build", "coverage"],
  "config_override": {
    "project_id": "my-project",
    "language": "python",
    "graph": {
      "exclude_paths": ["node_modules", "dist", "build", "coverage"],
      "ignore_globs": ["**/*.generated.*"]
    }
  },
  "route_token": {},
  "route_token_ref": "",
  "route_waiver": {}
}
```

`workspace_path` is required. `project_id` and `language` may be supplied at the
top level or inside `config_override`; top-level values are folded into the
override before graph build. Top-level `exclude_patterns` is a compatibility
alias for `config_override.graph.exclude_paths`: bootstrap stores the union in
`graph.exclude_paths`, preserving existing `config_override.graph.exclude_paths`
first and appending new top-level entries after dedupe. The response includes
`effective_exclude_roots`, the exact roots passed into graph reconcile.

For a first registration of an unregistered project, a dashboard/API/HN demo/CLI
caller may omit `route_token`: governance mints a narrow server-side
`project_bootstrap` route binding, persists only the opaque
`route_token_ref`/digest, validates the normal protected mutation gate, and
records `route_token_gate.project_bootstrap` timeline evidence. A tokenless
bootstrap for an already registered project is still rejected; use a valid
`route_token`, `route_token_ref`, or accepted `route_waiver` for non-first-run
re-bootstrap or repair flows.

Use common excludes for generated folders:

```text
node_modules, dist, build, .expo, .next, coverage
```

Also check for project-specific names that are not safe defaults, such as
`node`, `vendor`, generated SDK/client folders, local model downloads, embedded
example repositories, fixture clones, scratch worktrees, and large build/cache
roots. The dashboard bootstrap form requires this review before it submits. If
they should not become governed L4/L7 nodes, add them before first bootstrap:

```yaml
graph:
  exclude_paths:
    - "node"
    - "vendor"
    - "generated"
  ignore_globs:
    - "**/*.generated.*"
  nested_projects:
    mode: "exclude"
    roots:
      - "examples/fixture-app"
```

If the target workspace is a dirty git repo, commit/stash first. Dirty
worktree rejection is intentional because graph snapshots are commit-bound.
Bootstrap refusals are evidence-bearing: tokenless non-first-run refusals and
first-run precondition failures after a server-minted binding are deduped into
`route_token_gate.project_bootstrap_refusal` timeline events on the requested
project id. For a dirty first-run target that is not yet registered, this event
lives in the requested project's governance timeline DB even though the project
registry row is not created.

## 4. Project Config In V1

V1 stores most user project metadata in the Aming Claw project registry. It
should not default to creating or mutating `.aming-claw.yaml` in the target
project root unless the user explicitly chooses a source-controlled config.

For source-controlled projects that want a config file, see
[config/aming-claw-yaml.md](config/aming-claw-yaml.md). The key V1 sections are:

- `project_id` and `language`.
- `graph.exclude_paths` / `graph.ignore_globs`.
- `testing.e2e` suite metadata.
- `ai.routing`, especially the `semantic` provider/model.

## 5. First Useful Actions

After graph build:

1. Open Graph and inspect L1/L2/L3/L4/L7 hierarchy.
2. Select candidate nodes and review Files, Relations, Functions, and Problems.
3. Use AI or MCP graph queries for `function_index`, fan-in/fan-out, docs/tests,
   and bounded source excerpts.
4. File backlog rows with node ids, target files, tests, risk, and acceptance
   criteria.
5. For implementation, the V1 implementation default is observer-led Manual
   Fix unless the user explicitly asks to test experimental chain automation.
   The MF checklist
   (predeclare backlog → graph-first discovery → focused tests → Chain trailer
   commit → Update Graph → backlog close) lives in
   [skills/aming-claw/references/mf-sop.md](../skills/aming-claw/references/mf-sop.md).

Parallel MF uses observer-only coordination by default. The observer writes the
backlog row and `mf_parallel.v1` contract, starts bounded local Codex
subagents as `mf_sub` workers only when the user explicitly asks or an approved
contract calls for it, and reviews their `review_ready` or `waiting_merge`
evidence. Subagents are governed by the MF backlog row, contract,
file/worktree fence, and timeline evidence; governance `task_create`
dev/test/qa/merge is not the V1 default implementation entrypoint. Agents do
not merge, push, release gates, activate graph refs, close backlog, delete
worktrees, or mutate merge queues. The observer also does not wait, merge, or
push by default unless the user explicitly asks or a documented governance
transition requires it.

When an `mf_sub` worker has a `runtime_context_id`, it should read the Runtime
Context Service before acting: use MCP `runtime_context_current`, CLI
`aming-claw runtime-context current`, or HTTP
`/api/graph-governance/{project_id}/runtime-contexts/{runtime_context_id}/current-state`.
Use `/worker-guide` or the returned `worker_guide` to find the graph route
context and write-guide surfaces for read receipts, startup, checkpoints,
implementation evidence, and finish gates.

Worker final evidence should name the branch/worktree, owned changed files,
tests run, graph query trace ids, precheck evidence, generated assets policy,
and risks/open questions. Merge review checks contract fit, diff scope, test
and E2E evidence, docs/test/config impact, generated assets policy,
graph/reconcile plan, Chain trailers, and backlog close policy. If changed docs
or templates are not graph-bound, add an Asset Inbox binding or Governance Hint
follow-up so auditability can be materialized.
Chain trailers are MF audit anchors on commits; they do not mean auto-chain
execution is active.

## 6. AI Enrich

Configure the project's `semantic` provider/model in AI config before live
semantic jobs. OpenAI routes use Codex CLI (`codex`); Anthropic routes use
Claude Code CLI (`claude`). Version detection does not prove authentication.

> Cost expectation: AI Enrich subprocess-spawns the local CLI per node/edge,
> so token use scales linearly with the selection size. On a fresh project of a
> few hundred nodes, start with 5–10 nodes you actually care about, watch the
> Review Queue, and only run repository-wide enrichment after the routing and
> prompt template are tuned.

Recommended flow:

1. Select a node or edge.
2. Run AI Enrich.
3. Watch Operations Queue.
4. Review the proposed semantic memory.
5. Accept, reject, or retry in Review Queue.

`ai_complete` means a proposal exists. It is not trusted project memory until
reviewed and accepted.

## 7. Governance Hint

Governance Hint is the V1-safe graph correction path for orphan doc/test/config
files that already appear in snapshot file inventory.

It writes a source-controlled hint into the file, then requires:

1. Commit the hint.
2. Update Graph/reconcile.
3. Confirm the file is attached to the expected node.

It does not create nodes, rewrite ownership, move hierarchy edges, or edit
dependency/function-call relations.

## 8. What Is Not The V1 Default

- Auto-chain PM -> Dev -> Test -> QA -> Merge is experimental in V1 and is
  not the V1 default implementation route.
- ServiceManager/executor are advanced chain/ops surfaces. They are not
  required for governance, dashboard, graph query, backlog, AI Enrich Review
  Queue, or Manual Fix.
- Workflow acceptance graph tools (`wf_*`) are separate from the snapshot graph
  and require import before use.
- Telegram, Redis, dbservice, and full production deployment are advanced
  surfaces, not required for the local plugin MVP.
