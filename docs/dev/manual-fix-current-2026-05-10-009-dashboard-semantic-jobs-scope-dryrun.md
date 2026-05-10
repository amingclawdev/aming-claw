# Manual Fix 2026-05-10-009: Dashboard Semantic Jobs Scope/Dry-Run

## Backlog

- `OPT-BACKLOG-DASHBOARD-SEMANTIC-JOBS-SCOPE-DRYRUN`
- Priority: P1
- MF type: observer hotfix

## Problem

Frontend E2E submitted a selected-node semantic retry request with
`target_ids=["L7.104"]` and `options.dry_run=true`. The backend returned a
large `queued_count` and persisted pending semantic work, creating a runaway
queue risk for executor/AI processing.

## Root Cause

- `POST /semantic/jobs` did not treat `options.dry_run` as a true preview.
- Node semantic job responses used the snapshot-wide open queue count for
  `queued_count`, so existing pending jobs could be reported as work created by
  the current request.
- Dashboard scope aliases such as `selected_node` were not normalized into the
  semantic selector payload.

## Fix

- Normalize dashboard semantic job options through the backend enqueue body.
- Add dry-run handling for node and edge semantic job creation that returns a
  preview without DB/event writes.
- Return current-request `queued_count` from the semantic run summary.
- Filter response job rows to explicit node targets when a selected-node request
  is made.
- Add regression coverage in `agent/tests/test_semantic_jobs_scope.py`.

## Verification

- `python -m py_compile agent\governance\server.py`
- `python -m pytest agent\tests\test_semantic_jobs_scope.py -q`
- `python -m pytest agent\tests\test_graph_governance_api.py -q`
