# Manual Fix Record: MF-2026-05-10-008

## Summary

- Backlog: `OPT-BACKLOG-DASHBOARD-EDGE-RULE-SEMANTIC-CONTRACT`
- Type: `chain_rescue`
- Actor: `codex`
- Reason: dashboard backend contract reported deterministic `edge_semantic_rule` payloads as trusted current edge semantics.
- Graph snapshot before edit: `scope-3cf5648-ceb2`
- Projection before edit: `semproj-3cf5648-edge-semantic-runner-live`

## Graph-First Evidence

- `graph_status` before edit showed `edge_eligible=510`, `edge_current=510`, `edge_missing=0`, and `edge_semantic_status_counts.edge_semantic_current=510`.
- Local code inspection identified the owner modules already in graph/API code:
  - `agent/governance/graph_events.py`: projection edge status and health calculation.
  - `agent/governance/server.py`: dashboard current state, operations queue, and edge semantic job status.
  - `agent/governance/graph_snapshot_store.py`: semantic health field passthrough.
- `wf_impact` reported direct hits including `L7.102 agent.governance.server`; broad indirect impact is treated as dashboard/API contract risk.

## Change

- Classify enriched edge payloads with `semantic_payload.evidence.source=edge_semantic_rule` as `edge_semantic_rule`, not `edge_semantic_current`.
- Expose rule/payload coverage fields alongside trusted current/missing counts.
- Report rule-filled operations as `rule_complete` with `run_edge_semantics` available.
- Document the dashboard API contract.

## Validation

- `python -m py_compile agent\governance\graph_events.py agent\governance\server.py agent\governance\graph_snapshot_store.py`
- `python -m pytest agent\tests\test_graph_governance_api.py -q -k "edge_semantic or operations_queue_unifies_jobs_and_edge_not_queued"`: 3 passed
- `python -m pytest agent\tests\test_graph_governance_api.py -q`: 52 passed
- `python -m pytest agent\tests\test_graph_snapshot_store.py agent\tests\test_reconcile_semantic_config.py -q`: 24 passed
- `git diff --check`
