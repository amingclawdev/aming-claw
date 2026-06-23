<!-- governance-hint {"attach_to_node": {"path": "skills/aming-claw/references/mf-sop.md", "role": "doc", "target_area_key": "agent.governance", "target_node_id": "L3.13", "target_subsystem_key": "workflow_orchestration", "target_title": "Workflow Orchestration"}} -->

# Manual-Fix Checklist

Canonical source: `docs/governance/manual-fix-sop.md`. This file is only the short session checklist.

## Before Editing

1. Confirm the V1 implementation default: ordinary V1 implementation uses
   observer-led Manual Fix, with local Codex subagents as bounded `mf_sub`
   workers when parallel help is needed. Governance chain/executor
   dev/test/qa/merge execution is not the V1 default route; reserve it for
   explicit user requests to test chain automation or for documented
   experiments.
   - Observer/judge is a no-direct-code coordinator for governed nontrivial
     implementation work; it does not directly write implementation code.
   - If a local route/precheck provider is configured, resolve it through
     Aming-owned route/precheck contracts and record provider id, version, and
     hash evidence; source-controlled Aming skills must not name private
     provider systems or provider-specific tool names.
   - Route context must be consumed by machine gates, not merely shown in a
     prompt. For `observer_led_parallel_lanes` / `mf_parallel.v1` work, record
     timeline evidence for `route_context`, `route_action_precheck`,
     `bounded_implementation_worker_dispatch`, and `mf_subagent_startup` with
     matching required route identity (`route_context_hash`,
     `prompt_contract_id`). Route context consumption also needs public-safe
     `visible_injection_manifest_hash` or `visible_injection_manifest`;
     propagate and compare `prompt_contract_hash` when provided.
   - Before local implementation writes for P0, cross-module, or parallel MF
     work, run `agent.governance.precheck_service.run_precheck` with
     `kind="route.pre_mutation"` or an equivalent `route.action_precheck`
     service gate. `preflight_check` output or advisory route prose is not
     authorization; required machine route identity (`route_context_hash`,
     `prompt_contract_id`), allowed/blocked actions, required lanes/evidence,
     caller role, and public-safe visible injection manifest evidence must be
     present. `prompt_contract_hash` may be supplied for propagation/comparison,
     but is not required route identity. Provider unavailable, transport
     closed, or stale route evidence must block as
     `blocked_route_context_unavailable`.
   - Dispatch nontrivial implementation to bounded `mf_sub`/worker lanes with
     target files, tests or a recorded no-test/E2E decision, worktree/fence
     evidence, and review evidence.
   - The only direct observer mutation exception is tiny deterministic scope:
     record the explicit reason, allowed files, exact dirty-scope match
     evidence, and timeline event before mutation.
   - In a fresh observer session, load `aming-claw://current-context`,
     `aming-claw://skill`, `aming-claw://graph-first`, and this `mf-sop`
     resource before observer-root-route-context, dispatch, or implementation
     planning. Do not rely on dashboard health or remembered route state as a
     substitute for the current Aming Claw context.
2. Ensure a backlog row exists with target files, acceptance criteria, and details.
3. Predeclare/start the MF row with an MF id.
   - In MVP, API/storage may show `mf_type=chain_rescue` for observer-hotfix or
     manual-fix work. Treat it as the internal audited MF bucket, not as a sign
     that chain execution is required.
4. Capture baselines:
   - `git status`;
   - `version_check`;
   - `preflight_check`;
   - `graph_status`;
   - `graph_operations_queue`;
   - `wf_impact` for target files.
   Resolve any `plugin_update_state` blocker reported by `preflight_check`;
   missing state or `update_available` is a warning to record.
5. Run graph-first discovery and list reused nodes/modules in the working notes or final summary.
6. For new features or user-visible behavior changes, record the E2E impact decision:
   - run or add/update the relevant E2E and record evidence;
   - for dashboard/graph/bootstrap/file-hygiene paths, update the repo-owned fixture artifact first, materialize it into an isolated temp project, then run the E2E against that generated project;
   - for orphan file flows, put the orphan doc/test/config file in the fixture artifact, verify weak evidence first appears as an `asset_binding_proposal`, then let the E2E write the source-controlled governance hint, commit the fixture change, run Update graph, and assert the binding;
   - file a follow-up backlog row when live-AI, DB-mutating, slow, or human-approval E2E is deferred;
   - write `e2e_not_applicable` with a reason for docs-only or non-runtime changes.
7. For nontrivial architecture, frontend/UI, or QA-sensitive work, resolve the
   matching source-controlled review pack before implementation or before close:
   - architecture/data continuity: preserve API, persistence, state, migration,
     retry, and acceptance traceability;
   - frontend/UI implementation: require component convention, responsive,
     state, accessibility, and screenshot evidence;
   - QA evidence gate: require focused test, fixture, contract validation, E2E
     run/defer, and close-gate evidence;
   - validate structured review output with the review pack validator before
     converting accepted findings into acceptance criteria or follow-up backlog.
8. For parallel MF or subagent work, instantiate a source-controlled contract template before delegation:
   - start from `agent/governance/contract_templates/mf_parallel.v1.json`;
   - when a deterministic MF workflow worker is used, instantiate
     `agent/governance/contract_templates/mf_workflow_runtime.v1.json` and run
     each privileged stage through
     `agent.governance.precheck_service.run_precheck(kind, contract_id, stage,
     subject, actor)`;
   - run `route.pre_mutation` before local implementation writes for high-risk,
     P0, cross-module, `mf_parallel.v1`, or `observer_led_parallel_lanes`
     implementation actions;
   - use the workflow runtime stage graph
     `dispatch -> startup_gate -> implementation_wait -> handoff_gate -> merge_gate ->
     merge_queue_entry -> merge_preview -> live_merge -> reconcile ->
     close_gate -> done`, with `observer_review` for yellow-lane results and
     `blocked` for red-lane results;
   - require every precheck result to carry `precheck_run_id`, `kind`,
     `contract_id`, `stage`, `decision`, `status`, `subject`, `evidence`,
     `evidence_hash`, and `created_at`; merge/reconcile/close gates must verify
     the referenced token still matches subject commit/fence evidence;
   - for governed nontrivial, Judge-routed, or parent-route-bound `mf_sub`
     dispatch, require branch runtime registration evidence before
     `spawn_agent`. Use MCP `parallel_branch_allocate` when available; outside
     MCP, fall back to
     `/api/graph-governance/{project_id}/parallel-branches/allocate` or
     `agent.governance.parallel_branch_runtime.upsert_branch_context`. Record
     `mf_subagent_branch_runtime.v1` evidence matching `task_id`,
     `parent_task_id` where available, `fence_token`, `worktree_path`,
     `base_commit`, `target_head_commit`, and `merge_queue_id`;
   - after branch runtime registration, require `mf_subagent_graph_trace.v1`
     evidence before worker implementation starts and again at finish/handoff.
     The graph lookup must be audited with explicit `query_source=mf_subagent`,
     `task_id`, `parent_task_id`, `worker_role=mf_sub`, and `fence_token`;
  - for observer-to-subagent dispatch, require
    `observer_subagent_service_dispatch.v1` evidence with replayable
    `dispatch_command_ref` plus `monitor_ref`, or a documented host-adapter
    boundary with a documentation ref;
  - before `observer_runtime_text_prepare`, startup, or read-receipt evidence,
    enqueue or claim the backlog-specific `execute_backlog_row` command for the
    same backlog and route identity, then pass that command id as
    `observer_command_id` through CLI/MCP/API and `parallel_branch_startup`.
    Do not use a blind `observer_command_next` result as lineage unless it is
    the exact claimed `execute_backlog_row` command for this backlog and route
    identity;
  - when recording the worker `mf_subagent_read_receipt` timeline event, the
    append is validated at write time (AC-READ-RECEIPT-APPEND-WRITE-VALIDATION-20260610).
    A receipt append MUST include all of the following or it will be rejected
    with an actionable error naming the missing fields
    (`runtime_context.timeline_evidence_fields.v1`):
      - `event_kind`: must be `"mf_subagent_read_receipt"` (normalized automatically
        from `event_type` when absent, but supply it explicitly to avoid ambiguity);
      - `status`: must be a passing value (`ok`, `accepted`, `passed`, or `succeeded`);
      - `payload.runtime_context_id`: required lineage field;
      - `payload.task_id`: required lineage field;
      - `payload.parent_task_id`: required lineage field;
      - `payload.fence_token`: required lineage field;
      - `payload.worker_slot_id`: required for projection matching (normalized
        from `payload.worker_id` when absent, but supply both to be safe);
      - at least one of `payload.read_receipt_hash` or `payload.launch_text_hash`;
    A receipt accepted at write time that satisfies these fields is matchable by
    `mf_subagent_read_receipt_gate_verification` under the same lineage filter;
  - when route topology or instantiated evidence names
    `architecture_review_lane` or `qa_evidence_gate_review`, record those
     review lanes as first-class evidence. Do not make architecture review
     mandatory for ordinary `mf_parallel.v1` rows unless the route/contract
     asks for it;
   - write the instance to `chain_trigger_json.parallel_contract`;
   - treat subagents as local Codex workers governed by the MF backlog row,
     contract, file/worktree fence, and timeline evidence; do not use
     governance `task_create` dev/test/qa/merge as the default implementation
     entrypoint;
   - use observer-only coordination by default: the observer clarifies scope,
     checks runtime/graph/backlog state, creates the contract, may start agents
     only when the user explicitly asks or an approved contract calls for it,
     and reviews candidates; the observer does
     not implement, wait, merge, push, release gates, activate graph refs, close
     backlog, delete worktrees, or mutate merge queues unless the user
     explicitly asks or a documented governance transition requires it;
   - fill each `mf_sub` worker's runtime identity from task metadata before dispatch:
     `task_id`, `parent_task_id`, `worker_role=mf_sub`, `branch_ref`,
     `worktree_path`, `base_commit`, `target_head_commit`, `merge_queue_id`,
     and `fence_token`;
   - require every fresh `mf_sub` worker with a `runtime_context_id` to read
     `runtime_context_worker_guide` before implementation. Treat the guide as
     the concentrated route/startup/evidence entrypoint: it provides safe route
     context, route token ref, graph-query identity, read receipt and startup
     facades, implementation-evidence facade, finish-time attestation guidance,
     finish gate inputs, and close-gate gaps;
   - follow the worker guide order for the normal path: worker-authored read
     receipt, real pre-implementation startup, worker-scoped graph query,
     owned-scope implementation, runtime-context implementation evidence,
     finish-time worker attestation, finish gate, then `review_ready` or
     `waiting_merge`. Historical startup replay, observer-filled worker
     evidence, and QA/audit archive paths are recovery evidence only, not the
     happy path for close;
   - assign every worker a branch/worktree/file fence before dispatch, then
     require it to stay inside that fence and stop at `review_ready` or
     `waiting_merge`, never merge/push or mutate merge queues;
   - require subagent graph lookups to use audited
     `query_source=mf_subagent`, with `task_id`, `parent_task_id`,
     `worker_role=mf_sub`, and `fence_token` in the query context. Trace ids alone do
     not satisfy the gate when `query_source` is missing or not `mf_subagent`;
   - before `spawn_agent`, allocate/register the branch runtime context, then
     run and record
     `agent.governance.mf_subagent_contract.validate_mf_subagent_dispatch_gate`
     for each local `mf_sub` worker; the gate must pass with an isolated
     branch/worktree/file fence, `base_commit`, `target_head_commit`,
     `merge_queue_id`, `fence_token`, owned files, current target graph
     evidence, dirty-scope evidence, and branch runtime registration evidence
     before non-blocking dispatch. Without that registration,
     `query_source=mf_subagent` graph queries fail fence validation as
     `fence_invalidated_or_unknown`;
   - block dispatch when the target/main HEAD moved after contract creation or
     when the active target graph is stale. Existing branch/worktree adoption is
     allowed only as a first-class recovery path with explicit adoption
     evidence in the contract/timeline;
   - block target/main worktree dispatch by default. A same-worktree exception
     requires `same_worktree_allowed=true`, an explicit operator reason, exact
     dirty-scope evidence, and observer timeline evidence before dispatch;
   - after the local `mf_sub` worker starts and before it edits files, require
     `mf_subagent.startup` through the unified precheck service with
     `actual_git_root` or `actual_cwd`, `actual_fence_token`, branch, HEAD,
     target/main HEAD, and owned files. Block and stop the worker if actual
     runtime root is target/main, differs from the assigned worker worktree,
     carries the wrong branch/HEAD/fence token, or target/main became dirty,
     especially when dirty files overlap owned files;
   - require subagent implementation or verification timeline evidence to
     include returned graph trace ids in `payload.graph_trace_ids`,
     `payload.graph_query_trace_ids`, `verification.graph_trace_ids`, or
     `verification.graph_query_trace_ids`;
   - align self-precheck and gate expectations before dispatch: required
     evidence ids, focused test commands, E2E decision or defer row, finish-gate
     fence expectations, and the compact `self_check` evidence the subagent must
     report;
   - record the observer-configured test scenario policy before delegation:
     MF/subagent work is not universally test-first; the observer must choose
     `none`, `reuse_existing`, or `new_scenario_required`, give the reason,
     list required evidence ids, and record the E2E run/defer/not_applicable
     decision with a follow-up backlog id when deferred;
   - when the decision is `new_scenario_required`, name the fixture path and
     scenario ids in the contract and require fixture-backed tests before or
     with implementation. For `MF-WORKFLOW-PRECHECK-SERVICE-20260525`, the
     decision is `new_scenario_required`; the fixture
     `agent/tests/fixtures/mf_workflow_runtime.py` is required and must create
     isolated temporary git repositories/worktrees without mutating the live
     repo;
   - require structured worker final output with status, branch/worktree, owned
     changed files, tests run, graph query trace ids, precheck evidence,
     generated assets policy, and risks/open questions;
   - after a dispatch gate passes, stop at non-blocking dispatch unless the
     user explicitly asks the observer to wait, review, merge, close, or take
     another privileged action;
   - give every required evidence item a stable `id`;
   - require timeline evidence to reference ids through `payload.requirement_id(s)`,
     `verification.requirement_id(s)`, or `verification.contract_evidence[].requirement_id`;
   - make E2E evidence required for dashboard/API/operator-path changes unless explicitly deferred with a follow-up backlog row;
   - before accepting a merge candidate, check contract fit, diff scope,
     focused test/E2E evidence, docs/test/config impact, generated assets
     policy, graph/reconcile plan, Chain trailers, and backlog close policy;
   - when changed docs/templates are not graph-bound, record Asset Inbox
     binding or Governance Hint follow-up as needed for auditability.
     Close-impact accepted asset reminders stay warning/follow-up based for
     unrelated repairs; do not claim graph/doc/test asset coverage until the
     accepted binding or durable evidence exists.
9. If an AI session or `mf_sub` worker proposes doc/test/config binding changes, require the local asset-binding precheck first:
   - run `agent.governance.asset_binding_proposals.precheck_asset_binding_proposal` against the draft proposal;
   - include compact `self_precheck` evidence with the submitted proposal;
   - do not request direct graph materialization from weak evidence.
10. Treat documentation as a commit-bound asset before impact scope:
   - weak doc path matches stay as doc asset state `candidate` rows;
   - only accepted bindings from review decisions, source-controlled hints, or durable rules count as node-owned docs;
   - when changing doc binding behavior, verify `doc-asset-state.json` shows path/hash/status/proposal evidence.
   - governance hints should prefer stable target evidence such as
     `target_module`, or `target_area_key` + `target_subsystem_key` +
     `target_title`; title-only hints are repair candidates when the title is
     ambiguous. Reset/repair hints by editing the source hint, committing it,
     and running Update Graph/reconcile.
11. Keep asset binding and drift on separate audit lines:
   - binding relationships are source-controlled append-only commands,
     normally governance-hint bind/unbind events, then reconcile materializes
     graph secondary/test/config fields, file inventory effective state,
     asset projection, and binding events;
   - file/hash/drift/impact state is observed DB evidence written by reconcile,
     gate, or workflow worker from git diff plus accepted bindings;
   - changed bound assets covered by contract/gate may be recorded as
     `not_drifted` with gate evidence;
   - unchanged bound assets impacted by related source/config changes become
     `suspected`/`impact_pending` until observer, user, or AI-assisted review
     resolves them;
   - do not directly hand-write trusted accepted binding rows into DB as a
     substitute for source-controlled binding evidence.
12. For observer/MF work, append timeline evidence as work proceeds:
   - `task_timeline_append` with `event_kind=implementation` after scoped code,
     docs, config, or fixture changes are made;
   - `task_timeline_append` with `event_kind=verification` after focused tests,
     review checks, or documented no-test decisions;
   - `task_timeline_append` with `event_kind=close_ready` after commit,
     redeploy/reconcile/version checks are complete;
   - run `mf_timeline_precheck` before `backlog_close`; for route-parallel work,
     the gate also checks route-context consumption and bounded worker
     dispatch/startup evidence tied to the same route identity.
   - when using MCP `backlog_close`, pass the route-token gate evidence:
     either `route_token` with route context / prompt contract / scope /
     expiry / evidence refs, or an explicit `route_waiver` with reason and
     timeline evidence.
   - closed/retired backlog statuses (excluded from active list and current
     task): FIXED, CLOSED, DONE, RESOLVED, CANCELLED, MERGED, SUPERSEDED, VOID,
     WAIVED. WAIVED — intentional, reason-documented set-aside (carry a waive
     reason / superseded_by); distinct from SUPERSEDED (replaced) and
     CANCELLED/VOID (abandoned).

## Observer Work Modes and Root Route Context

Fresh-session bootstrap flow (do this before any mutation):
1. Load route context (read backlog row + `aming-claw://current-context`).
2. `GET/POST /api/projects/{project_id}/observer-root-route-context` — read
   the compact handoff. Pass `graph_query_schema_trace_id` in the POST body
   if you already ran `graph_query(tool=query_schema)` and have a trace id.
3. Call `graph_query(tool=query_schema)` to discover the live query contract.
4. Surface `work_mode` and `next_legal_action` to the user.
5. Stop here. Do not dispatch/merge/close without a `record_work_mode_transition`
   event and `route_action_precheck` bound to the canonical route identity.

Read the observer root route context before acting on a row:
`GET/POST /api/projects/{project_id}/observer-root-route-context`. The runtime
posture is an explicit `work_mode`:

- `observer_look_before_act` (default): read, inspect, file findings, propose
  next legal action only. Blocks edit-implementation, self-clear-judge-blocker,
  dispatch, merge, and close. `next_legal_action` is `record_work_mode_transition`.
- `observer_execution_supervisor`: dispatch/merge/close coordination is legal.
  Requires BOTH a recorded `observer_work_mode_transition` event AND a
  `route_action_precheck` bound to the canonical route identity; the observer
  cannot widen its own authority by fiat.
- `observer_hotfix_exception`: narrowly relaxes host-adapter surrogate startup
  only. Never permits direct implementation or judge self-clear, and a surrogate
  startup under it is still not close-satisfying real-worker evidence.

`edit_implementation` and `self_clear_judge_blocker` are never allowed for the
observer in any mode. The root route context exposes the canonical identity
(`route_id`, `route_context_hash`, `prompt_contract_id`), `work_mode`,
`loaded_skills`/`loaded_resources`, `graph_query_schema_trace_id`,
`allowed_actions`/`blocked_actions`, `required_evidence`, and
`next_legal_action`; the dashboard evidence modal renders these real fields.

The current runtime projection is the authority for this execution. Treat SOP
text, `docs/dev/` notes, old context documents, remembered timelines, and source
searches as explanatory background only. If `next_legal_action`,
`runtime_context_worker_guide`, or `runtime_context_current` is missing,
contradictory, or too stale to act on, record a blocker and stop rather than
inventing or reconstructing the flow.

## observer_repair_run_route_evidence: Two Legal Modes

`observer_repair_run_route_evidence` (record=true) accepts exactly two calling patterns:

**Mode 1 — External-validated precheck packet (canonical repair path)**

Supply both `route_identity` (all 5 canonical fields: `route_id`,
`route_context_hash`, `prompt_contract_id`, `prompt_contract_hash`,
`visible_injection_manifest_hash`) AND a non-empty `action_precheck` packet with
at least one source marker (`action`, `caller_role`, `source_event_id`,
`allowed`, `status`, etc.).  The packet is validated against the supplied identity
by `_external_route_action_precheck`; if valid, the external identity is consumed
and recorded as-is without any mint or supersession.

**Mode 2 — Fresh replay-mint (no claimed identity)**

Omit `route_identity` entirely (or pass an empty mapping).  The service generates
a new route identity from scratch via `_build_route_service_preview`.  No
supersession is recorded; the generated identity is canonical from creation.

**Identity-only pattern is illegal (record=true)**

Supplying `route_identity` WITHOUT an `action_precheck` packet (i.e. an empty or
missing packet) with `record=true` is rejected immediately with
`record_blocked_reason: identity_only_without_action_precheck`.  Before this guard
(incident #3384–#3393), the missing precheck caused a silent degrade-to-mint that
generated a new identity and recorded `route.identity.superseded` declaring the
supplied (live canonical) identity as superseded — invalidating all previously
accepted startup/QA evidence on the same backlog row.

**Live-identity supersession guard**

Before recording any `route.identity.superseded` event, the server must call
`observer_repair_run.guard_live_identity_supersession(...)` against the timeline
events for the affected backlog row.  If accepted `mf_subagent_startup` or
`independent_verification` events exist whose route identity matches the proposed
superseded identity, the supersession is refused unless BOTH `force_supersede=true`
AND a non-empty `force_reason` are supplied (both are recorded in the supersession
payload for audit).

## Route Token Passing Forms

Protected governance calls (`task_timeline_append`, `backlog_close`, etc.) accept
the route token evidence in two forms.  Both produce a valid, audited gate result;
only the decision value differs:

**Form A — full token (authoritative, always works):**
Pass the full `route_token` object in the request body.  The gate validates
inline and returns `decision=route_token`.

**Form B — ref-only (preferred low-token form, ~3.5 KB saved per call):**
Pass only `route_token_ref` (the `rtok-…` string returned by
`POST /api/projects/{project_id}/observer/route-context/issue`).  The gate
resolves the ref server-side from the persisted registry and returns
`decision=route_token_ref_resolved` plus `resolved_from_ref=true`.

Ref-only calls fail closed: an unknown ref, a superseded/expired ref, or a
ref whose stored identity does not match the request scope (`task_id`,
`backlog_id`, `route_context_hash`) is rejected with the same 422 as an
invalid full token.  The full-token path is never weakened.

To use Form B, the token must have been issued via the HTTP endpoint (which
persists the ref automatically) or via `observer_route_context.persist_route_token_ref`
before the protected call is made.

## Close-Evidence Integrity Gates

The MF close gate enforces:

- `#3090` cross-ref: close evidence from a different backlog/scope row is
  rejected unless an accepted bridge/lineage event links the rows.
- `#3092` blocker-resolution: an observer may propose `pending_judge_review` but
  must never self-clear a judge blocker; independent judge acceptance is required.
- `#3093/#3094` stale-route evidence: evidence recorded under a superseded route
  identity is invalidated and must be re-recorded under the canonical identity.
- `#3104` surrogate-not-close-satisfying: `session_token_evidence_type =
  "surrogate"` is never close-satisfying real bounded-worker evidence, even when
  the startup gate stamps `close_satisfying=true` and even under
  `observer_hotfix_exception`. Only a real session-token startup is close-satisfying.
- `#3516-F1` server-verified token evidence: `session_token_evidence_type` in
  startup events may now be `'server_verified'` (server confirmed hash matches
  allocation-time record), `'hash'` (first-sight commitment, server stores the
  hash), `'claimed_unverified'` (presented token hash did NOT match stored hash —
  treated as surrogate), `'surrogate'`, or `''`.  Only `'server_verified'` and
  `'hash'` and the legacy `'hash'` (pre-fix events) exempt startups from surrogate
  classification in the finish gate.  `'claimed_unverified'` is classified as
  surrogate.  Existing recorded startup events with `'hash'` are NOT retroactively
  invalidated; the stricter gate behavior applies to NEW startups only.

## Surrogate Policy: Finish Gate vs Close Gate (F4 asymmetry — by design)

The finish gate (`validate_mf_subagent_finish_gate` in
`agent/governance/mf_subagent_contract.py`) evaluates per-startup surrogate
classification via `_startup_is_host_adapter_surrogate` and
`surrogate_startup_evidence_gate`.  A surrogate startup without a real-worker
join blocks the finish gate.

The close gate (`mf_close_gate_verification` in `agent/governance/task_timeline.py`)
does NOT evaluate per-startup surrogate status.  This is intentional: the close
gate checks timeline event kinds (contract, route-context, lane-ownership,
worker-graph-trace, independent-QA, etc.) that are already separately gated
upstream.  A lane that passes the finish gate has already proven its startup is
non-surrogate or has been joined by a real-worker startup; by the time close is
reached the startup evidence is settled and does not need re-evaluation.

If the close gate were to re-evaluate startup surrogate policy, it would need
access to the same runtime-context DB queries the finish gate uses — introducing
a coupling that is not warranted for the close gate's purpose.  If future work
requires close-gate startup re-validation (e.g. for multi-lane rollup), add an
explicit `mf_subagent_startup_verification` timeline event kind to carry the
joined verdict rather than re-running the surrogate logic at close time.

## Commit

Stage explicit files only. Use Chain trailers as MF audit anchors. Chain
trailers do not mean auto-chain execution is active:

```text
Chain-Source-Stage: observer-hotfix
Chain-Project: aming-claw
Chain-Bug-Id: <backlog-id>
```

Use `[observer-hotfix]` or `manual fix:` in the subject when this is a true MF bypass.

## Live Current-Task Runtime Binding Rule

A backlog row appears as a live entry in the Current activity pool only when
**all three** of the following hold:

1. `runtime_state` is set to an active value (e.g. `manual_fix_in_progress`).
2. `current_task_id` is set to the running worker's task id.
3. `updated_at` is refreshed to reflect recent activity.

A **timeline event alone** (e.g. an `observer_hold` event recorded against the
backlog id) does **not** make a row live in the runtime pool; it only makes the
row a candidate for the secondary timeline-based path.

A row with `runtime_state=manual_fix_in_progress` and an **empty
`current_task_id`** is treated as **stale** and is excluded from the active
pool and the single-active count.  Workers must call
`backlog_runtime.update_backlog_runtime` (or the equivalent MCP/API path)
immediately on startup to bind both fields atomically.  Failing to set
`current_task_id` while setting `runtime_state` leaves the row stale and
invisible to the Current activity widget.

**Data note**: 63 rows matching `manual_fix_in_progress` with empty
`current_task_id` were CANCELLED on 2026-06-09 as part of the cleanup that
preceded this guard.

## After Commit

1. Restart/redeploy changed runtime services when needed.
2. Run `version_check`; require `ok=true`, `dirty=false`, and runtime matching HEAD for runtime changes.
3. Run MCP `preflight_check`; require no `plugin_update_state` blockers. As
   supplemental local evidence from the repo checkout, run
   `python -m agent.cli mf precommit-check --json-output`. Do not assume a
   stale installed `aming-claw` shell command has the same subcommands until
   plugin/CLI update aftercare has run.
4. Check graph status. If HEAD is ahead of the active graph, run direct Update graph/scope reconcile before telling a dashboard user the graph is current. Explicit pending-scope queueing is legacy/debug only.
5. Rebuild or refresh semantic projection when dashboard semantic state changed.
6. Confirm the E2E impact decision is current, deferred with a backlog row, or explicitly not applicable.
7. Append `close_ready` timeline evidence and run `mf_timeline_precheck`.
8. Close the backlog row with the commit hash and verification evidence.
