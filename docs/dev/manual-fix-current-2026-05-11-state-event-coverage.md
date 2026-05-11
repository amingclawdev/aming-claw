# Manual Fix · 2026-05-11 · State-transition event coverage

## Phase 0 — Assess

- **Backlog**: `task-1778519683-cb18ec`
  (`BACKLOG-STATE-TRANSITION-EVENT-COVERAGE`) — filed + succeeded via
  create+complete-same-turn pattern.
- **wf_impact**: direct_hit `L7.102` (server.py); 5 nodes total. Scope B.
- **SSE tap evidence (2026-05-11T17:12Z)**:
  - Enqueue: `semantic_job.enqueued` + `dashboard.changed` ✅
  - Worker pre-AI window (5-30s): **silent** ❌
  - Worker post-AI: `edge_semantic.proposed` + `dashboard.changed` ✅
- **Activate hook**: internal (no HTTP) → never fires `_emit_dashboard_changed`.
- **Pre-flight**: ok=true (running governance `2678e6f`).

## Phase 1 — Classify

- **Scope**: B (5 affected nodes; semantic_worker.py is unmapped but
  server.py pulls in L7.102 + 4 indirect).
- **Danger**: Medium — touching worker drain (the queue substrate),
  state mapping in server.py, and graph_snapshot_store.activate_hook.
  No auto_chain / executor / version gate / governance startup touch.
- **Combined (B × Medium)**: run module tests + verify explicit nodes.

## Phase 2 — Implementation plan (mirror existing pattern, don't redesign)

1. **Worker emits `ai_reviewing` event before AI** (semantic_worker.py)
   - `_drain_edge`: BEFORE `ai_call("edge", ...)`, write a new
     `edge_semantic_requested` graph_event row with status=ai_reviewing
     (ALLOWED_EVENT_STATUSES already includes this). Publish
     `edge_semantic.running` + `dashboard.changed`.
   - `_drain_node`: similar — write a `semantic_node_enriched` event
     with status=ai_reviewing right before `run_semantic_enrichment`.
     Publish `semantic_node.running` + `dashboard.changed`.

2. **activate_graph_snapshot publishes dashboard.changed**
   (graph_snapshot_store.py)
   - The activate flow rewrites the snapshot ref AND triggers projection
     rebuild. After both writes commit, publish `dashboard.changed`
     with `path=/internal/activate_hook`.
   - One publish, idempotent, advisory.

3. **Dashboard status mapping covers ai_reviewing** (server.py)
   - `_edge_semantic_job_status`: add ai_reviewing → "running".
   - `_semantic_job_progress`: ensure running bucket counts.

4. **Frontend SSE hook** (frontend/dashboard/src/lib/sse.ts)
   - Add `edge_semantic.running` + `semantic_node.running` to the
     `known` list so EventSource fires the dispatch handler.

5. **OperationsQueueView running badge**
   - statusClass already covers "running" — confirm "ai_reviewing"
     also maps to status-running; add fallthrough if missing.

## Phase 3 — Commit

Single MF commit on main with `Chain-Source-Stage: observer-hotfix`
trailer. Frontend changes go on `frontend/dashboard-p0` (separate commit).

## Phase 4 — Post-commit verify

- Restart governance.
- SSE tap: `curl -N /events/stream` during a fresh edge enrich.
  Expected event sequence:
    1. ready
    2. semantic_job.enqueued
    3. dashboard.changed (POST)
    4. **edge_semantic.running** ← NEW
    5. **dashboard.changed (worker pre-AI)** ← NEW
    6. edge_semantic.proposed
    7. dashboard.changed (worker post-AI)
- Browser: trigger reconcile, confirm dashboard auto-refreshes
  WITHOUT manual ↻ (via activate_hook publish).

## Phase 5 — MF closure

`task_create + task_complete` same turn with audit metadata.

## Result

(filled at the end)
