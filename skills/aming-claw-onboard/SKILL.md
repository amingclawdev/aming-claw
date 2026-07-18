---
name: aming-claw-onboard
description: Single Aming Claw skill entrypoint. Use this first for Aming Claw governance, backlog, graph, runtime, worker, QA, dashboard, MCP, install/start/bootstrap, or dogfood work; prefer the MCP onboard_route_guide tool before choosing a role or successor contract.
---

# Aming Claw Onboard

This is the only active Aming Claw skill entrypoint.

Start here, then use the live MCP `onboard_route_guide` tool as the source of
truth. The HTTP endpoint is a fallback only when the host does not expose the
MCP tool. Archived skills are historical reference only and are not active
instructions.

## Entry

1. Check the live project state with MCP: `runtime_status`, `graph_status`, and
   `graph_operations_queue`.
2. Call MCP `onboard_route_guide` with `project_id` and any available
   `backlog_id` or `bug_id`, role/work-type hints, and route-token refs.
3. If MCP does not expose `onboard_route_guide`, fall back to
   `POST /api/projects/{project_id}/onboard-route-guide` with the same fields.
4. Confirm role: `observer`, `worker`, `mf_sub`, or `qa`.
5. Confirm work type, such as `capability_query`, `system_operation`,
   `continue_contract_chain`, `operator_supervised_direct_main`,
   `multi_backlog_parallel`, `parallel_worker`, `qa_verification`, or explicit
   `legacy_operator_recovery`.
6. Follow only the returned role/token guidance, `next_legal_action`, and index
   paths under `agent_onboard_guidance.onboard_route_guide`, including
   `interface_index`, `capability_index`, `system_operation_index`,
   `backlog_chain_binding`, `graph_first_policy`, source-hint status paths,
   and archive/resource paths.
7. Before source-only fallback, use source search, IDE symbols, language tooling,
   or project-native navigation to discover exact source symbol names. Then use
   the returned `graph_first_policy` to call `graph_query` with the correct
   `query_purpose`, starting with `function_index` and exact symbols, followed
   by callers/callees for matched symbols. Preserve graph trace ids in the
   contract/timeline payload. Use source-only evidence only when the graph misses,
   is unavailable, or source-hint status says docs/config/tests are not
   materialized.
8. For observer-owned protected writes, establish the observer identity in this
   order before `direct_fix_enter`, `mf_parallel_enter`, backlog close, route
   renewals, or other protected writes: call `observer_session_register`, keep
   the session alive with `observer_session_heartbeat` every 300 seconds, then
   call `observer_route_context_issue` or `observer_route_context_renew`.
   `observer_session_id` plus its session token proves observer liveness;
   `observer_route_token_ref`/`route_token_ref` proves scoped route authority.
   They are not interchangeable. If `observer_session_id` is missing, recover
   by authenticating/signing with the session token and heartbeating the known
   session, or registering a fresh observer session before issuing/renewing the
   route ref.
9. For long-running observer/worker chains, if a protected facade reports an
   expired or near-expired `route_token_ref`, renew the same-scope ref through
   `observer_route_context_renew` (or
   `POST /api/projects/{project_id}/observer/route-context/renew`) with an
   active `observer_session_id`. Pass only refs; raw route tokens are not
   required or exposed.

## Guardrails

- Do not treat archived skill files as active instructions.
- Do not mutate governed files until a backlog row and route/contract evidence
  exist.
- For operator-supervised direct work, record the onboard/direct exception
  evidence before mutation and stay inside the approved target files. The
  canonical close order is pre-mutation exception -> commit-bound
  implementation -> independent role-bound QA -> redeploy -> live regression
  -> graph reconcile/preflight -> close_ready -> backlog_close. Implementation
  records `changed_files` plus `diff_check` or `dirty_scope_check`; QA is
  QA-authored `verification`/`independent_verification` authorized by a managed
  QA session ref, never observer receipt/transcription or a persisted raw QA
  token. `close_ready` accepts `redeployed`/`governance_redeploy`/
  `runtime_sync`/`runtime_version_sync`, `live_regression_evidence`/
  `live_regression`, and requires both `graph_reconciled` and `preflight_ok`.
- For worker or QA work, use the runtime context or QA session entry returned
  by the onboard guide.
- For QA evidence that is materialized by a parent observer, keep the QA owner,
  submitter, materialized-from, and authorization provenance fields returned by
  the runtime guide; do not collapse it into observer-authored QA.
- For `mf_parallel.v2` workers, record implementation evidence, create one
  clean git commit, record that exact immutable HEAD through canonical
  `runtime_context_worker_commit`, then submit finish-time worker attestation
  and finish gate. Finish gate is the worker's last governance write; after it
  passes, resume the parent contract with independent QA and call
  `parallel_branch_merge_queue_materialize` (or HTTP
  `parallel-branches/merge-queue/materialize`) with the finish checkpoint before
  `parallel_branch_merge_queue_apply`. Timeline implementation events are
  append-only compatibility projections and never override canonical
  ContractRuntime worker implementation or worker commit state.
- At the `mf_parallel.v2` `worker_finish_gate` step, never submit a graph-trace
  placeholder such as `<worker-owned-graph-query-trace-id>`. If the copy-safe
  finish-gate payload lacks a concrete worker trace already verified in the
  governance DB, especially after resume, redeploy, or reconcile, the same
  worker must run exactly one current worker-scoped `graph_query` using its
  existing `runtime_context_id`, `task_id`, `parent_task_id`, `fence_token`,
  `session_token_ref`, and `target_project_root`, then substitute the returned
  trace id into `graph_trace_ids`. Preserve accepted read, startup,
  implementation, git commit, `worker_commit`, and finish-attestation lines;
  never repeat them. A `missing_worker_graph_trace_evidence` rejection is
  non-mutating: retry the finish gate once, and only after that graph query
  succeeds.
- Route-token-ref renewal must preserve or narrow the existing project,
  backlog/task, allowed-actions, target-files, and owned-files scope. If the
  observer session is stale, heartbeat or register an observer session before
  renewal; never request or paste a raw route token.
- `observer_hotfix` / `hotfix_enter` are not ordinary observer paths. Use them
  only when the live guide returns `legacy_operator_recovery` or an operator has
  explicitly requested legacy recovery. Normal repairs should use
  `direct_fix_enter`, `mf_parallel_enter`, or the current ContractRuntime
  `next_legal_action`.
- Direct-fix topology must be classified before action: parentless
  single-branch direct merge, blocked-parent successor that returns to parent,
  or multi/parallel merge queue. Before stopping or replacing a worker, audit
  progress from runtime current state and timeline evidence; complete direct
  fix with independent QA, branch-service validation when runtime code changed,
  merge or redeploy, full reconcile, and protected backlog close.
- For authoritative activation after an ordinary merge or redeploy, call
  `graph_current_full_reconcile` with `semantic_use_ai=false`; this still runs
  full structural and semantic materialization. Use `semantic_use_ai=true` only
  for semantic-specific acceptance after provider auth and readiness preflight
  succeeds. If a reconcile is already running, poll that same run and snapshot;
  never start a concurrent retry. If an unready AI provider leaves the candidate
  running, first confirm it is the same run, then have the observer stop it
  through a manager-owned redeploy and issue exactly one non-AI retry.
- For post-merge current-full route proof, the submitted `task_id` or
  `contract_execution_id` must exactly match the selected observer
  `route_token_ref` server-registered task scope. A parent observer ref uses its
  parent task scope (for example, `qa-role-admission-r1`), never a worker task
  guessed from RuntimeContext. A route-proof rejection before run creation is
  non-mutating; correct it once with the same `snapshot_id` and `run_id`.
- Runtime resume guidance prefers live `backlog_contract_chain_current` /
  ContractRuntime current state. Treat compact-ledger resume as recovery
  fallback only when the live projection is missing or unrebuildable.

## Archive

Legacy skill documents moved to `Archive/skills/`. Use
`Archive/skills/index.json` only as a provenance map from old skill ids to
archived paths and replacement onboard route guide paths.
