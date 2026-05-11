# Manual Fix · 2026-05-11 · Merge frontend/dashboard-p0 into main

## Phase 0 — Assess

- **Backlog**: `task-1778537315-7bd9f0`
  (`MF-2026-05-11-FRONTEND-DASHBOARD-MERGE`) — filed via task_create; the
  executor auto-claimed it before task_complete (race with queue worker)
  and marked failed, but the row remains as audit evidence. Inert per R6
  (no PRD, no dev dispatch).
- **wf_impact** (`agent/governance/server.py, agent/governance/event_bus.py,
  agent/tests/test_graph_governance_api.py`): direct_hit L7.102 + L7.42;
  6 affected nodes total. Scope B.
- **MF #1 prerequisite**: `5b5ab64` adds `.mjs/.cjs` to
  `LanguagePolicy.source_extensions` so post-merge reconcile picks up
  frontend node scripts. Verified via 9 adapter tests + existing 86
  governance tests green.
- **Branch divergence (`ca47b2e` ancestor)**:
  - dashboard-p0: 33 commits ahead — frontend (entirely new tree),
    skill removals, 2 backend commits (`9382bb8` SSE, `24170de` hydration).
  - main: 35 commits ahead — 5 MF rounds of backend work
    (SSE backend, hydration patch, reconcile-no-stale-jobs, edge
    persistent state, state-event coverage). These supersede
    dashboard's older backend touches.
- **Pre-flight**: HEAD `5b5ab64`, dirty=false, governance running
  `2678e6f` (pre-MF #1 — will restart post-merge).

## Phase 1 — Classify

- **Scope**: B (6 affected nodes; mostly L7 server endpoints).
- **Danger**: Medium — touches the governance HTTP entrypoint
  (`server.py`) and event bus (`event_bus.py`), but conflict resolution
  is a strict `--ours` for backend (main's MF work is newer + tested)
  and `--theirs` for frontend (entirely new tree, no conflict possible).
  No auto_chain / executor / version gate / governance startup behavior
  changes — pure merge.
- **Combined (B × Medium)**: run full pytest suite + frontend tsc; do
  not require executor chain.

## Phase 2 — Implementation plan

1. **Merge with --no-ff** so the commit graph preserves the 33-commit
   dashboard branch as a topology branch off main. `git merge --no-ff
   frontend/dashboard-p0` from main worktree
   (`C:\Users\z5866\Documents\amingclaw\aming_claw`).
2. **Resolve conflicts**:
   - `agent/governance/server.py` is the only known conflict. Take
     main's version: `git checkout --ours agent/governance/server.py
     && git add agent/governance/server.py`.
   - For any other backend conflict that surfaces: take `--ours` (main).
   - For any frontend conflict (unlikely — frontend tree doesn't exist
     on main): take `--theirs` (dashboard).
3. **Verify backend untouched**:
   - `git diff HEAD agent/` post-merge should show only additions
     from dashboard's frontend-side touches that weren't in main, never
     reverts to main's MF work.
4. **Run tests**:
   - `python -m pytest agent/tests/` — expect existing 86 tests + the
     13 new edge persistence tests = 99 green.
5. **Frontend type check**:
   - `cd frontend/dashboard && npx tsc --noEmit` — frontend tree is
     entirely new to main, so any error here is pre-existing on
     dashboard branch (4 known errors in FocusCard.tsx + InspectorDrawer.tsx
     per session history — non-blocking).
6. **Commit with trailer**: merge commit message must include
   `Chain-Source-Stage: observer-hotfix` so the version walker
   recognizes the new HEAD as the chain anchor.

## Phase 3 — Commit

Single MF merge commit on main with `Chain-Source-Stage: observer-hotfix`
trailer. No separate frontend commit needed — frontend tree is folded
in by the merge.

## Phase 4 — Post-commit verify

1. Restart governance via `start-governance.ps1 -Takeover`.
2. Trigger a `scope-catchup` reconcile through the dashboard or
   `task_create` (type=task, kind=reconcile).
3. Confirm next snapshot auto-creates L7 nodes for
   `frontend/dashboard/src/**.ts*x` files via the existing
   JavaScriptTypescriptAdapter (already wired in `_GRAPH_LANGUAGE_ADAPTERS`,
   already supports the suffixes after MF #1).
4. SSE tap: confirm `dashboard.changed` still fires on reconcile +
   semantic worker events (regression check that my prior MF rounds
   survived the merge).

## Phase 5 — MF closure

`task_complete` on the backlog row was attempted (became idempotent
no-op because the executor pre-claimed and terminal'd it; intentional
behavior per R6 — backlog row is an audit trail, not a chain
trigger). The merge itself is the change of record; the trailer makes
auto-chain aware. No separate closure task needed because the executor
already terminal'd `task-1778537315-7bd9f0`.

## Result

_(to fill in after merge + verify)_
