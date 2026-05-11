# Manual Fix · 2026-05-11 · Edge semantic persistent-state parity

## Phase 0 — Assess

- **Backlog**: `task-1778516527-48b634`
  (`BACKLOG-EDGE-SEMANTIC-PERSISTENT-STATE-PARITY`) — filed + succeeded
  via create+complete-same-turn pattern (executor doesn't auto-claim).
- **wf_impact** for
  `agent/governance/reconcile_semantic_enrichment.py,
   agent/governance/graph_events.py,
   agent/governance/semantic_worker.py`:
  - direct_hit: `[]`
  - affected_nodes: 0
  - All 3 files in preflight's pre-existing 91-unmapped-files warning.
- **Pre-flight** (last run 11:48Z `req-d1228273342b`): ok=true, no
  blockers. Running governance = `ad47a36` (PID 29860).
- **Git**: clean on main, 755 commits ahead of origin.

## Phase 1 — Classify

- **Scope**: A (0 affected nodes; 3 target files are unmapped).
- **Danger**: Medium — schema introduction + carry-forward business
  logic. NOT touching auto_chain / executor / version gate /
  governance startup.
- **Combined (A × Medium)** per S3 matrix: "Run related tests first.
  Commit directly." No mandatory split.
- **Mandatory hard rules**:
  - R4 (audit record · THIS DOC)
  - R7 (execution record · THIS DOC)
  - R8 (multi-commit restart loop if extra commits get generated)
  - R11 (`Chain-Source-Stage: observer-hotfix` trailer)
  - R9 (coverage warnings on touched files — they're pre-existing
    unmapped, will document)

## Phase 2 — Pre-commit verify (plan)

Mirror the node pipeline verbatim. No new architecture.

### Implementation steps

1. **Schema** (`reconcile_semantic_enrichment.py:43`):
   - Add `graph_semantic_edges` CREATE TABLE. Mirror
     `graph_semantic_nodes` minus `file_hashes_json` (edges have no
     primary/test/docs files); rename `feature_hash` →
     `edge_signature_hash`.
   - Add status index.
   - Wire into `_ensure_semantic_state_schema`.

2. **Edge signature hash** (`graph_events.py` adjacent to
   `stable_node_key_for_node`):
   - `stable_edge_key_for_edge(edge, src_node, dst_node)` — hash of
     `(stable_node_key(src), stable_node_key(dst), edge_type)`.
   - `edge_signature_hash_for_edge(edge, src_node, dst_node)` — hash
     of `(stable_edge_key, src.feature_hash, dst.feature_hash,
     edge_type, sorted(evidence keys))`. Endpoint drift invalidates.

3. **`_persist_semantic_state_to_db`** (`reconcile_semantic_enrichment.py:1144`):
   - Add `state.edge_semantics` loop, INSERT/UPSERT into
     `graph_semantic_edges`. Mirror node loop including
     `submit_for_review` → `pending_review` row_status override
     (skip for carried-forward entries).

4. **`_carry_forward_semantic_graph_state`** (`reconcile_semantic_enrichment.py:1517`):
   - Add `state.edge_semantics` loop. Compare `edge_signature_hash`.
   - Skip entries whose edge_id is no longer in current snapshot's
     edge index (endpoint deletion cascade).
   - Skip entries whose signature drifted.

5. **`backfill_existing_semantic_events`** (`graph_events.py:1378`):
   - Add edge branch reading `graph_semantic_edges` and writing
     `edge_semantic_enriched` graph_events rows with appropriate
     status mapping (pending_review → PROPOSED, else OBSERVED).
   - Set `stable_node_key` column to `stable_edge_key` for edge events.

6. **`_latest_edge_semantic_events`** (`graph_events.py:1572`):
   - Drop `snapshot_id = ?` filter.
   - Add `stable_node_key = ?` filter (reused column) AND existing
     `target_id` fallback.
   - Match node lookup pattern (line 1517-1556).

7. **`_build_edge_semantics`** (`graph_events.py:1613`):
   - Change `edge_ids = sorted(set(eligible_edges) | set(edge_events))`
     → `edge_ids = sorted(eligible_edges)`. Orphans dropped to match
     node-side behaviour. Document in comment.

8. **`semantic_worker._drain_edge`** (`semantic_worker.py:_drain_edge`):
   - After `graph_events.create_event(edge_semantic_enriched...)`,
     ALSO INSERT/UPSERT `graph_semantic_edges` row so carry-forward
     has a state record to pick up.

9. **One-time migration**: extend `backfill_existing_semantic_events`
   to pre-populate `graph_semantic_edges` from existing
   `edge_semantic_enriched` events on first run (idempotent UPSERT).

### Tests
- `agent/tests/test_edge_semantic_persistence.py` covering:
  - schema migration (CREATE TABLE idempotent)
  - `_persist` writes edge rows
  - `_carry_forward` with matching hash → carried
  - `_carry_forward` with drifted hash → skipped
  - `_carry_forward` with deleted endpoint → skipped (cascade)
  - `_build_edge_semantics` skips orphan (semantic exists, structure gone)
  - `backfill` mirrors `graph_semantic_edges` rows → events
  - `_latest_edge_semantic_events` finds cross-snapshot via stable_edge_key

Pre-existing 12 worker tests + 5 edge API tests must continue to pass.

## Phase 3 — Commit

Single MF commit on main with `Chain-Source-Stage: observer-hotfix`.

## Phase 4 — Post-commit verify

- Restart governance.
- preflight_check: confirm no new blockers.
- Smoke test: trigger a fresh dashboard "Catch up to HEAD"
  reconcile, confirm previously-enriched edges carry forward to new
  snapshot's `projection.edge_semantics` with
  `status=edge_semantic_current`.

## Phase 5 — File MF closure

`task_create + task_complete` same turn with audit metadata.

## Result

(filled at the end)
