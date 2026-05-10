# Manual Fix 2026-05-10-010: Dashboard Cancel Operations Contract

## Backlog

- `OPT-BACKLOG-DASHBOARD-CANCEL-OPS-CONTRACT`
- Priority: P1
- MF type: chain rescue / observer hotfix
- Note: MCP backlog write was unavailable in this session with `Transport closed`;
  backlog was upserted through governance HTTP `POST /api/backlog/...`.

## Graph Evidence

- Active graph status checked through governance HTTP before implementation.
- Active snapshot: `scope-372aca4-41d0`
- Graph commit matched HEAD `372aca4ad9b6b7d227614d2cae5afb424169a2a8`.
- Pending scope reconcile count was `0`.
- Semantic drift still existed: 53 stale node semantics and 510 missing edge
  semantics, so dashboard cancel controls must be reliable before broad E2E.

## Problem

Dashboard E2E found that action presets can enqueue semantic, scope, and
feedback operations but several cancel paths were missing or inconsistent:

- No batch cancel endpoint for semantic jobs.
- Edge semantic cancel did not accept the dashboard pipe edge id form.
- Scope reconcile operations advertised `cancel` but had no route.
- Feedback review had only an implicit `keep_status_observation` workaround.
- `POST /semantic/jobs` returned a session job id that dashboard could not map
  directly to per-row cancel operations.

## Fix

- Add `POST /semantic/jobs/cancel-all` with optional AND-combined filters.
- Accept edge ids as event id, `src->dst:type`, or `src|dst|type`.
- Return `queued_ops` from semantic job creation and tag graph events with the
  session `job_id`.
- Allow cancelling by returned semantic session `job_id`.
- Add `POST /reconcile/scope/cancel` to waive pending scope reconcile rows.
- Add `POST /feedback/cancel` as the documented soft-cancel contract using
  `keep_status_observation`.
- Document the dashboard cancel contract in `docs/api/governance-api.md`.

## Verification

- `python -m pytest -q agent/tests/test_dashboard_cancel_contract.py`
- `python -m pytest -q agent/tests/test_semantic_jobs_scope.py`
- `python -m pytest -q agent/tests/test_graph_governance_api.py -k "semantic_jobs or edge_semantic or operations_queue or feedback_decision or pending_scope"`
