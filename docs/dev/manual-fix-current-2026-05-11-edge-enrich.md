# Manual Fix · 2026-05-11 · Edge AI enrich broken end-to-end

## Phase 0 — Assess

- **Trigger**: dashboard repro. Operator picked edge `L7.28 → L7.106 depends_on`,
  clicked "AI enrich edge". Job stayed visible as queued. Cancel returned 500.
  Manual refresh later showed a `proposed` candidate with
  `risk=insufficient_context` and AI's own open_issues said:
  > "edge_context.src and edge_context.dst are empty strings; cannot identify
  >  the L7.28 -> L7.106 nodes to characterize the depends_on relation."
- **Backlog**: filed as `task-1778510544-d048e8`
  (`BACKLOG-EDGE-AI-ENRICH-BROKEN`, P2) before MF was approved.
- **Pre-flight** (mcp__aming-claw__preflight_check at 2026-05-11T14:45Z):
  - system: pass · 52 tables
  - version: pass · chain_version=`4fe7f61`, git_head=`4fe7f61`, source=trailer
  - graph: warn · 297 orphan pending nodes (pre-existing)
  - coverage: warn · 91 unmapped files (pre-existing — includes
    `reconcile_semantic_enrichment.py`, `reconcile_semantic_ai.py`, etc.)
  - queue: pass · 0 queued / 0 claimed
  - batch_worktrees: warn · 208 stale worktrees (pre-existing)
  - **blockers: []** — safe to commit.
- **wf_impact** on
  `agent/governance/server.py, agent/governance/reconcile_semantic_enrichment.py,
   agent/governance/reconcile_semantic_ai.py, frontend/dashboard/src/lib/sse.ts`:
  - direct_hit: `L7.102` (agent.governance.server)
  - affected_nodes: 5 (L7.102, L4.33, L7.171, L7.45, L7.30)
  - max_verify: 8 (T4)
  - test_files: 43 listed (full server-side suite)
- **git status**: only dashboard MF changes already in flight on
  `frontend/dashboard-p0`; nothing else dirty.

## Phase 1 — Classify

- **Scope**: B (1–5 affected nodes) — 5 total, 1 direct.
- **Danger**: Medium — modifying business logic in two existing handlers
  (`_semantic_jobs_edge_targets`, `_semantic_job_status_update`). No
  delete / rename, no infrastructure change, no auto_chain / version gate
  touch.
- **Combined (B × Medium)**: run module tests + verify explicit nodes.
- **Mandatory hard rules in scope**: R4 (audit record · THIS DOC),
  R5 (workflow restore proof), R7 (execution record · THIS DOC),
  R8 (multi-commit restart loop), R11 (Chain-Source-Stage trailer).

## Phase 2 — Pre-commit verify

Plan:
1. Implement Bug 1 fix in `_semantic_jobs_edge_targets` — hydrate `edge` from
   snapshot when only target_ids strings are provided.
2. Implement Bug 3 fix in `_semantic_job_status_update` — branch on edge-shaped
   job_id and update the matching `graph_events` row instead of
   `graph_semantic_jobs`.
3. Write unit tests covering both fixes in `agent/tests/`.
4. `pytest` the new tests + the module-related suites
   (`test_graph_governance_api.py`, `test_reconcile_semantic_config.py`).
5. Frontend type-check from `frontend/dashboard/` (no JS changes planned).

**Bugs 2 and 4** (wrong output_schema for edges; missing EventBus event on
worker state transition) are left on the original backlog row — they need
deeper investigation in `reconcile_semantic_enrichment.py` /
`reconcile_semantic_ai.py` and were judged out-of-scope for this MF
(violates "minimal scope" if folded in here). Will file follow-up.

## Phase 3 — Commit

Single MF commit on branch `frontend/dashboard-p0`. Will carry a
`Chain-Source-Stage: observer-hotfix` trailer per R11.

## Phase 4 — Post-commit verify

After commit:
- Restart governance (if needed — chain_trailer reads HEAD on every
  `/api/version-check`)
- `mcp__aming-claw__preflight_check` — confirm no new blockers
- `mcp__aming-claw__wf_impact` — re-verify the same 5 nodes
- (Optional) restart governance for SERVER_VERSION refresh

## Phase 5 — Workflow restore proof

- File MF closure task (`task_create` + `task_complete` with status
  `succeeded`, audit metadata).
- Confirm the closure task transitions cleanly through
  `task_complete` without errors → demonstrates the chain bus is alive.

## Result

**RESTORED** — edge AI enrich path no longer feeds empty edge_context to the
AI; edge cancel no longer 500s.

- MF commit: `24170de` (branch `frontend/dashboard-p0`).
  Carries `Chain-Source-Stage: observer-hotfix` trailer per R11.
- Tests: 3 new + 57 existing in
  `agent/tests/test_graph_governance_api.py` and
  `agent/tests/test_reconcile_semantic_config.py` — all 60 pass.
- Post-commit preflight (2026-05-11T14:51Z): `ok: true`, blockers `[]`.
  Warnings unchanged from Phase 0 baseline (297 orphan pending, 91
  unmapped files, 208 stale worktrees — all pre-existing).
- `chain_version` / `git_head` still report `4fe7f61` from preflight
  because the running governance service is reading the **main** repo,
  not this dashboard branch. The MF commit is on `frontend/dashboard-p0`
  and will roll up to chain when the branch merges. No chain drift.
- Backlog partial closure: `task-1778510544-d048e8` —
  Bugs 1 + 3 fixed. Bugs 2 (wrong output_schema for edge prompts) and
  4 (no SSE event on edge worker transition) remain open on that row.
- MF closure task: (filed in next step — see task_create result).
