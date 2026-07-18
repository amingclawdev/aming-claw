# Aming Claw V1 Onboarding Guide

This guide describes the V1 path for using Aming Claw to govern another local
project. V1 is local-first: start governance, open the dashboard, explicitly
register a clean target project, build the graph, then use dashboard/MCP tools
to inspect, file backlog, and plan PR work.

The single user-invocable skill entry is `/aming-claw:aming-claw-onboard`.
From that entry, prefer MCP `onboard_route_guide` as the live onboard route
guide service. If the host has not exposed that MCP tool, fall back to
`POST /api/projects/{project_id}/onboard-route-guide` with the same fields.
The CLI launcher remains `aming-claw launcher` for local setup. Archived skills
under `Archive/skills/` are provenance only, not active instructions.

The packaged Codex plugin manifest also installs a client-side `PreToolUse`
hard guard that runs `python agent/hooks/onboarding_guard.py` before protected
governance or dispatch actions. Claude Code relies on the server-side
route/identity gate instead, so non-interactive demo prompts can progress after
runtime route/context evidence is recorded. Client hooks cannot intercept direct
`curl` or other HTTP calls to governance made outside the CLI hook path; the
server gate remains the authoritative control.

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
3. Read `aming-claw://current-context`.
4. Use `/aming-claw:aming-claw-onboard`, then call MCP `onboard_route_guide`.
   Use the HTTP route-guide endpoint only when MCP is unavailable.
5. Confirm role and work type in the returned onboard guide.
6. Follow the returned role/token guidance, next legal action, capability,
   interface, system-operation, archive/resource, and backlog-chain index paths.
7. Follow `agent_onboard_guidance.onboard_route_guide.graph_first_policy` before
   source search fallback: pick the returned `graph_query` `query_purpose`,
   preserve graph trace ids in runtime/timeline evidence, and inspect the
   source-hints status paths when docs/config/tests are missing from the graph.

Use the current-context project id, governance URL, dashboard URL, graph state,
and guardrails as the session anchor before observer root-route-context,
dispatch, graph queries, or implementation planning. A fresh observer should
not begin by relying on remembered project state, dashboard health, or old
prompt text; it should load the current Aming Claw resources first, then read
the backlog row/route context for the current task.

Governance health, `/api/health`, and a working `/dashboard` prove the service
can answer HTTP. They do not prove the AI host loaded `.mcp.json`, exposed the
Aming Claw MCP server, or has route/current-context visibility.

Every source-controlled mutation must be contract-bound before it happens. This
applies to parallel MF, direct fix, legacy/operator recovery, Docker dogfood,
fixture, docs, dashboard, runtime, config, and test changes alike: update or
create the backlog row, bind the route/contract identity, record timeline
evidence for the intended action, then mutate only the allowed files. A legacy
recovery hotfix changes the allowed route, not the requirement to leave
contract and timeline evidence.

Observer-owned protected writes have a fixed identity order. First call
`observer_session_register`. Keep that session alive with
`observer_session_heartbeat` every 300 seconds. Then call
`observer_route_context_issue` or `observer_route_context_renew` before
`direct_fix_enter`, `mf_parallel_enter`, backlog close, graph reconcile, or any
other protected write. The `observer_session_id` and its session token prove
observer liveness; `observer_route_token_ref` / `route_token_ref` proves scoped
route authority. They are not interchangeable. If `observer_session_id` is
missing, recover by authenticating/signing with the known session token and
heartbeating that session, or register a fresh observer session before issuing
or renewing the route-token ref.

During governed execution, the live runtime is the authority for what is legal
next. Use `observer-root-route-context`, `runtime_context_worker_guide`,
`runtime_context_current`, and the contract-state `next_legal_action` before
acting. Historical docs, `docs/dev/` drafts, previous timeline examples, and
source-code searches are orientation only; they do not authorize a step. If the
runtime guidance is missing, stale, contradictory, or too vague to act on, file
that as blocker evidence and stop instead of reconstructing the flow from old
context prose. Runtime resume should prefer live
`backlog_contract_chain_current` / ContractRuntime current state; compact-ledger
resume is a recovery fallback only when the live projection is missing or cannot
be rebuilt.

When a parent observer materializes QA evidence from a QA-owned packet or role
session, preserve evidence-owner, submitter, materialized-from, and
authorization provenance in the contract line. This records delegated
materialization without treating the observer as the QA evidence owner.

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

## 3. Docker Live-Lane Dogfood Loop

Use the Docker install audit harness when validating that a new Codex or Claude
host can install the plugin, load the Aming Claw MCP server, and run the demo
prompt in a clean container runtime. The loop is contract-first even when the
container itself is only a fixture:

1. In the host observer session, read `aming-claw://current-context`, then call
   MCP `onboard_route_guide`. Read graph-first, MCP-tools, MF SOP, or archive
   resources only through the returned index paths and availability status.
2. Bind the work to a backlog row and route context before changing docs,
   runner scripts, runtime code, or demo fixtures.
3. Run a minimal CLI/auth smoke in Docker before a required live prompt when
   login state is uncertain.
4. For diagnosis, run the Codex lane with `--keep-container`,
   `--container-name`, and `--prompt-timeout-ms`; preserve the report and
   container logs as blocker evidence instead of rerunning from scratch.
5. When the Docker lane exposes a blocker, file it on the host backlog/timeline,
   repair the host checkout under the row's contract, commit and push, then
   update or rerun the Docker checkout/ref.
6. Final proof must be a fresh container from the pushed ref, without
   `--keep-container` or `--reuse-container`.

The runner documentation lives in
[docker/hn-install-audit/README.md](../docker/hn-install-audit/README.md).
Debug containers reduce repeated auth/runtime setup, but they are not release
proof by themselves.

## 4. Register A Target Project

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

## 5. Project Config In V1

V1 stores most user project metadata in the Aming Claw project registry. It
should not default to creating or mutating `.aming-claw.yaml` in the target
project root unless the user explicitly chooses a source-controlled config.

For source-controlled projects that want a config file, see
[config/aming-claw-yaml.md](config/aming-claw-yaml.md). The key V1 sections are:

- `project_id` and `language`.
- `graph.exclude_paths` / `graph.ignore_globs`.
- `testing.e2e` suite metadata.
- `ai.routing`, especially the `semantic` provider/model.

## 6. First Useful Actions

After graph build:

1. Open Graph and inspect L1/L2/L3/L4/L7 hierarchy.
2. Select candidate nodes and review Files, Relations, Functions, and Problems.
3. Use AI or MCP graph queries for `function_index`, fan-in/fan-out, docs/tests,
   and bounded source excerpts.
4. File backlog rows with node ids, target files, tests, risk, and acceptance
   criteria.
5. For implementation, the V1 implementation default is observer-led Manual
   Fix unless the user explicitly asks to test experimental chain automation.
   The live route guide is the entrypoint for the current MF path. The archived
   historical MF checklist is preserved at
   [Archive/skills/aming-claw/references/mf-sop.md](../Archive/skills/aming-claw/references/mf-sop.md)
   for provenance only.

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

When an `mf_sub` worker has a `runtime_context_id`, it should use the Runtime
Context worker guide as the concentrated entrypoint before acting: use MCP
`runtime_context_worker_guide` or the HTTP worker-guide facade, and refresh
`runtime_context_current` when a gate or evidence write needs the latest state.
The guide is the worker-facing map for safe route context, graph-query
identity, read/write evidence facades, startup, implementation evidence,
finish-time attestation, finish gate, and close-gate gaps.

When allocating bounded parallel workers, pass the canonical
`target_project_root` (or its `target_graph_root` alias) for runtime-context
identity and carry that exact value into worker-guide reads, graph queries, and
runtime-context write facades. Use `worktree_path` and its aliases only for the
final materialized worker worktree path. Pass route token refs, never raw route
tokens.

Route context is not satisfied by reading this onboarding document or by
searching source files. Workers and observers must consume the runtime-projected
route context and record the required evidence through the current facade for
their role.

Default onboard guidance does not expose `observer_hotfix` / `hotfix_enter` as
ordinary observer paths. Those interfaces are legacy/operator recovery only and
should appear through the live guide's explicit `legacy_operator_recovery`
surface or a concrete operator recovery instruction. Ordinary repairs use the
current ContractRuntime `next_legal_action`, `direct_fix_enter`,
`mf_parallel_enter`, or `mf_batch_parallel_enter`.

Classify direct-fix topology before action:

1. Parentless single-branch direct merge: only for explicit operator-approved,
   tiny direct-main repairs with pre-mutation exception evidence.
2. Blocked-parent successor: use `direct_fix_enter`, repair in the child
   contract, run independent QA, and record return-to-parent evidence before
   parent close authority resumes.
3. Multi/parallel merge queue: use `mf_parallel_enter` or
   `mf_batch_parallel_enter`; row-scoped workers finish into the merge queue
   rather than direct parent mutation.

Before stopping or replacing a worker, audit progress from the latest runtime
current state, worker guide, task timeline, graph traces, branch head, changed
files, tests, finish gate, and blockers. Complete direct-fix work with
independent QA, branch-service validation on canonical governance port when
runtime code changed, merge or redeploy, full graph reconcile, and protected
backlog close.

When a bounded QA graph query rejects a non-descendant or otherwise unsafe
candidate overlay and requires an exact candidate snapshot, do not reuse an old
trace and do not make QA build or activate the graph. The observer calls
`graph_current_full_reconcile` with `activate=false`, the exact clean worker
`project_root`, and that worktree's full HEAD. The returned
`candidate_snapshot_id` is supplied to the same bounded QA session's next
`graph_query`. This pre-QA candidate build never advances the active graph and
does not count as merge, redeploy, or post-merge reconcile evidence; run the
ordinary activated current-full reconcile only after QA and merge.

The normal worker order is:

1. Read the worker guide and confirm the next legal action. A fresh worker
   should normally see `submit_mf_subagent_read_receipt`.
2. Record a worker-authored read receipt through the runtime-context
   `runtime_context_read_receipt` MCP tool or HTTP `read-receipts` facade,
   using the guide hash material and public route identity. Do not persist raw
   session, route, or launch-text tokens.
3. Record real startup before implementation through `parallel_branch_startup`
   or the runtime-context `startup` facade, including actual cwd/git root,
   branch/head, base/target head, merge queue id, owned files, read-receipt
   refs, route identity, and route token ref.
4. Run worker-scoped `graph_query` as `query_source=mf_subagent` with
   `query_purpose=subagent_context_build` or `subagent_gate_validation`,
   carrying `task_id`, `parent_task_id`, `worker_role=mf_sub`, fence, and
   session identity.
5. Before any implementation edit, verify `pwd` and
   `git rev-parse --show-toplevel` both equal the assigned runtime worktree.
   If either points at target/main, stop before editing and relaunch in the
   assigned worktree. Then implement only inside the file/worktree fence and
   run focused tests, but do not create the worker git commit yet. Write
   implementation evidence through the runtime-context `implementation-evidence`
   facade with changed files, tests, and graph trace ids.
6. Record finish-time worker attestation and the finish gate while the worker
   diff is still uncommitted. If the finish gate blocks, stop and report the
   blocker; do not backfill or fabricate evidence. Only after the finish gate
   passes may the worker commit its branch. The worker does not merge, mutate
   the merge queue, or write QA evidence. After the finish gate, the observer
   resumes the parent contract with independent QA, then materializes the
   durable merge queue item with
   `parallel_branch_merge_queue_materialize` (or HTTP
   `parallel-branches/merge-queue/materialize`) using the worker finish
   checkpoint before any ordered merge apply.

Startup and implementation belong to the same live worker session for the
happy path. If a startup-only probe exits, do not reuse that completed
transcript with `send_input` as the implementation worker. Spawn a fresh
implementation worker and record fresh read receipt plus startup evidence for
that worker before file writes. Before any protected worker write, the worker
must have the raw host envelope env values `AMING_WORKER_SESSION_TOKEN` and
`AMING_WORKER_FENCE_TOKEN`; `session_token_ref` alone is copy-safe identity,
not write authorization. If those env values are missing after read receipt
and startup, use the runtime-context `session-token/rejoin` facade and inject
the returned host envelope into the real `mf_sub` worker.

For the `daily-planner-lite` one-prompt demo, the intended fixture path is two
parallel backlog rows moving from open through normal close gate with no manual
route/startup/identity repair. If a worker guide says route context, read
receipt, startup, graph trace, implementation evidence, finish gate, or
close-gate evidence is missing, repair that surface through its documented
facade before implementation or handoff rather than treating post-hoc
reconstruction as the happy path.

Worker final evidence should name the branch/worktree, owned changed files,
tests run, graph query trace ids, precheck evidence, generated assets policy,
and risks/open questions. Merge review checks contract fit, diff scope, test
and E2E evidence, docs/test/config impact, generated assets policy,
graph/reconcile plan, Chain trailers, and backlog close policy. If changed docs
or templates are not graph-bound, add an Asset Inbox binding or Governance Hint
follow-up so auditability can be materialized.
Chain trailers are MF audit anchors on commits; they do not mean auto-chain
execution is active.

## 7. AI Enrich

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

## 8. Governance Hint

Governance Hint is the V1-safe graph correction path for orphan doc/test/config
files that already appear in snapshot file inventory.

It writes a source-controlled hint into the file, then requires:

1. Commit the hint.
2. Update Graph/reconcile.
3. Confirm the file is attached to the expected node.

It does not create nodes, rewrite ownership, move hierarchy edges, or edit
dependency/function-call relations.

## 9. What Is Not The V1 Default

- Auto-chain PM -> Dev -> Test -> QA -> Merge is experimental in V1 and is
  not the V1 default implementation route.
- ServiceManager/executor are advanced chain/ops surfaces. They are not
  required for governance, dashboard, graph query, backlog, AI Enrich Review
  Queue, or Manual Fix.
- Workflow acceptance graph tools (`wf_*`) are separate from the snapshot graph
  and require import before use.
- Telegram, Redis, dbservice, and full production deployment are advanced
  surfaces, not required for the local plugin MVP.
