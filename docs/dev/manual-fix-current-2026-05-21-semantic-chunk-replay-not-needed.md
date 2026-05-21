# Manual Fix Record: semantic chunk replay not-needed persistence

Date: 2026-05-21
Actor: codex_handoff_resume
Backlog: MF-SEMANTIC-CHUNK-REPLAY-NOT-NEEDED-20260521

## Context

Resumed from HANDOFF-SEMANTIC-DOGFOOD-SESSION-20260521. The active graph was
`scope-52e0596-8c14` at commit `52e0596`; governance was usable, while
ServiceManager remained on older runtime `689d00d`.

Dry-run dogfood over persisted semantic chunk traces found 15 stale nodes with
matching historical chunk outputs. Seven still require the live chunk-fix model,
one historical run had failed slices, and eight were already node-scoped
aggregates returning `not_needed`. Before this fix, `not_needed` replay returned
without persisting semantic state, so those eight no-AI salvage candidates could
not enter Review Queue.

## Graph Evidence

- `agent/governance/reconcile_semantic_enrichment.py`: L7.128
  `agent.governance.reconcile_semantic_enrichment`
- `agent/governance/server.py`: L7.141 `agent.governance.server`
- Test files are broad shared coverage files with many mapped nodes; targeted
  regression coverage was added for the replay helper and API handler.

`wf_impact` was unavailable because the workflow acceptance graph is not
imported for `aming-claw`.

## Change

- Added a dry-run-aware persistence path to `replay_semantic_chunk_aggregate_fix`
  so `not_needed` aggregates are written as pending review in non-dry-run mode.
- Passed API `dry_run` through to the helper so dry-run replay remains DB-safe.
- Added regression coverage for helper-level no-AI persistence and API dry-run
  no-write behavior.

## Verification

- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q agent/tests/test_reconcile_semantic_enrichment.py::test_semantic_chunk_fix_replay_persists_not_needed_without_ai`
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q agent/tests/test_graph_governance_api.py::test_graph_governance_semantic_chunk_fix_replay_api_dry_run_not_needed_no_db_write`
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q agent/tests/test_reconcile_semantic_enrichment.py`
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q agent/tests/test_graph_governance_api.py`
- `python -m py_compile agent/governance/reconcile_semantic_enrichment.py agent/governance/server.py`
- `git diff --check`

## E2E Decision

No dashboard UI was changed. The behavior is a backend semantic replay recovery
path covered by targeted API/helper tests. Live semantic replay against the
running governance service was deferred because the running service is still on
the pre-fix commit until redeploy, and the handoff explicitly warned not to run
live AI without quota/route confirmation.
