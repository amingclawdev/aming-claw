# Parallel Agent Graph Rollback Contract

> Status: P0 design contract
> Backlog: `ARCH-GRAPH-ROLLBACK-DB-CONTRACT-DOC`
> Runtime row: `OPT-GRAPH-REFLOG-MERGE-ROLLBACK-PROJECTION-RULES`
> Parent design: `docs/dev/parallel-agent-multibranch-design.md`

## Purpose

Parallel branch execution cannot treat rollback as only a git operation. A
rollback must also make graph refs, semantic projections, semantic jobs,
pending scope rows, graph events, and Governance Hint bindings agree with the
target ref.

This contract defines the DB and projection rules that must exist before the
parallel branch runtime can safely merge, rollback, and replay retained
branches.

## Scenario Coverage

| Scenario | Required rollback behavior |
| --- | --- |
| PB-004 | Wrong merge order triggers batch rollback and ordered replay. |
| PB-005 | DB rollback keeps graph, semantic, jobs, and pending scope consistent with code rollback. |
| PB-006 | Governance Hint add/change/remove emits invertible deltas. |
| PB-011 | Branch graph artifacts stay isolated until merge acceptance. |
| PB-012 | Project, branch, batch, and epoch keys prevent cross-boundary reuse. |

## Graph Ref Event Model

`graph_snapshot_refs` is a current pointer. Parallel runtime also needs an
append-only ref event log. The implementation can call it `graph_ref_events` or
an equivalent table, but it must preserve these fields:

| Field | Meaning |
| --- | --- |
| `event_id` | Stable event identity. |
| `project_id` | Project identity. |
| `ref_name` | Target ref, such as `active` or `refs/heads/main`. |
| `branch_ref` | Candidate branch ref when operation came from branch work. |
| `batch_id` | Batch identity when operation is part of a batch. |
| `merge_queue_id` | Merge queue row that authorized the operation. |
| `operation_type` | `activate`, `merge`, `rollback`, `revert`, `replay`, or `backfill_escape`. |
| `old_snapshot_id` | Previous snapshot for the ref. |
| `new_snapshot_id` | New snapshot for the ref. |
| `old_commit` | Previous commit for the ref. |
| `new_commit` | New commit for the ref. |
| `old_projection_id` | Previous semantic projection for the ref. |
| `new_projection_id` | New semantic projection for the ref. |
| `merge_epoch` | Epoch assigned to an accepted merge. |
| `rollback_epoch` | Epoch assigned to rollback of one or more merge epochs. |
| `replay_epoch` | Epoch assigned to replay after rollback. |
| `source_event_id` | Prior event this event supersedes or reverses. |
| `actor` | User, agent, worker, or system actor. |
| `evidence_json` | Bounded evidence: tests, queue decision, conflicts, rollback reason. |
| `created_at` | Event timestamp. |

`graph_snapshot_refs` may be rebuilt from the latest valid ref event. The event
log is the audit source.

## Operation Types

| Operation | Allowed source | Required effect |
| --- | --- | --- |
| `activate` | Manual Update Graph, bootstrap, direct scope reconcile. | Move target ref to a snapshot for the current target commit. |
| `merge` | Merge queue after merge gate pass. | Move target ref to post-merge snapshot/projection and assign `merge_epoch`. |
| `rollback` | Batch rollback runtime or observer action. | Move target ref to rollback-compatible snapshot/projection and assign `rollback_epoch`. |
| `revert` | Git revert path. | Keep history-forward commit but mark reverted merge epochs inactive for target currentness. |
| `replay` | Batch replay runtime. | Move target ref through retained branch heads in corrected order and assign `replay_epoch`. |
| `backfill_escape` | Operator escape hatch. | Move target ref to full-scan snapshot and mark prior pending rows with explicit evidence. |

## Currentness Rules

Target-ref currentness is determined by the latest accepted ref event for the
target ref plus its projection rule version.

A graph or semantic row is not current if any of these are true:

- It belongs to a branch-local candidate snapshot that has not been merged.
- It belongs to a merge epoch abandoned by rollback.
- It belongs to a replay epoch that has not reached merge gate acceptance.
- Its projection was built against a superseded ref event.
- Its semantic job was produced for a branch/ref/batch/epoch that is no longer
  active.

Branch-local evidence can still be inspected, but dashboard/MCP must label it
as candidate, stale, inactive, abandoned, or rebuild_required.

## Rollback Epoch

A rollback epoch groups all DB state changes caused by a rollback decision.

Required rollback epoch fields:

| Field | Meaning |
| --- | --- |
| `rollback_epoch` | Stable ID for rollback decision. |
| `batch_id` | Batch being rolled back. |
| `target_ref` | Target ref being restored. |
| `rollback_to_commit` | Target commit after rollback. |
| `rollback_to_snapshot_id` | Snapshot compatible with target rollback commit. |
| `rollback_to_projection_id` | Projection compatible with rollback snapshot. |
| `abandoned_merge_epochs` | Merge epochs no longer current after rollback. |
| `retained_branch_refs` | Branches kept for replay or evidence. |
| `reason` | Operator or runtime reason. |
| `created_by` | Actor. |
| `created_at` | Timestamp. |

Rollback must be idempotent. Replaying the same rollback epoch cannot create a
second active target-ref state.

## Replay Epoch

Replay starts after rollback and uses retained branch heads. It does not replay
shell commands or unstored agent memory.

Replay requirements:

- Each replayed branch receives a `replay_epoch`.
- Merge queue ordering is recalculated before replay.
- Every replay step gets a fresh merge preview.
- Graph/semantic projections are rebuilt or carried forward only when the
  projection rule says the source epoch is compatible.
- Cleanup stays blocked until replay reaches `accepted` or `abandoned`.

## Table Ownership

| Area | Required behavior |
| --- | --- |
| `graph_snapshot_refs` | Stores the current pointer only; updated from ref events. |
| `graph_ref_events` | Append-only source of ref activation, merge, rollback, revert, replay, and backfill evidence. |
| `graph_snapshots` | Structural evidence for a commit/ref candidate; branch candidates are not target truth. |
| `graph_events` | Stores graph mutations with project, branch/ref, snapshot, merge epoch, rollback epoch, and source event identity. |
| `graph_semantic_projections` | Projection view for snapshot/ref/epoch; abandoned epochs are inactive or stale. |
| `graph_semantic_nodes` | Node semantic cache keyed by projection/snapshot/epoch; abandoned rows cannot be current. |
| `graph_semantic_edges` | Edge semantic cache keyed by projection/snapshot/epoch; abandoned rows cannot be current. |
| `graph_semantic_jobs` | Jobs are scoped to project, target type, branch/ref, snapshot, projection, and epoch. |
| `pending_scope_reconcile` | Pending rows must include branch/ref, batch, merge queue, rollback epoch, and replay epoch before parallel rollout. |
| Governance Hint source files | Hint add/change/remove is source-controlled evidence; incremental reconcile must emit inverse graph deltas. |

## Governance Hint Rollback

Full rebuild naturally follows source state, but true incremental reconcile must
handle hint removal and hint changes explicitly.

Hint delta operations:

| Delta | Meaning |
| --- | --- |
| `hint_added` | New hint binds an orphan doc/test/config file to an existing node. |
| `hint_changed` | Existing hint points to a different node or role. |
| `hint_removed` | Hint no longer exists in source and prior binding must be removed or marked inactive. |
| `hint_rollback_restored` | Rollback restored an older hint state. |

Rollback must not leave a stale binding when a hint was removed, changed, or
rolled back.

## Migration Strategy

Existing data has active-only refs and many empty `branch_ref` values. The
migration must be backward compatible:

1. Treat empty `branch_ref` as target ref history for the default active ref.
2. Backfill one synthetic `activate` ref event for the currently active
   snapshot/projection.
3. Keep old projection rows readable, but compute currentness through the new
   ref event model.
4. Add branch/ref/batch/epoch columns as nullable first.
5. Populate new fields on new writes.
6. Tighten invariants after dashboard/MCP and tests read the new model.

## API Requirements

The API must expose compact, ID-addressable state:

| API surface | Required fields |
| --- | --- |
| Runtime status | Active ref event, active snapshot, active projection, graph stale state. |
| Operations queue | Pending merge/rollback/replay events and blockers. |
| Graph status | Last operation type, merge epoch, rollback epoch, replay epoch. |
| Dashboard graph panel | Candidate vs current graph/projection labels. |
| Backlog/merge queue panel | Rollback_required, retained branches, replay order, cleanup blocked reason. |

Detailed ref event history should be fetched by event ID or ref name with
pagination.

## Implementation Order

1. Add doc guard for this contract.
2. Add append-only graph ref event schema and migration.
3. Backfill synthetic active ref event for existing active snapshots.
4. Move snapshot activation through ref event writer.
5. Add projection currentness rules based on ref event and projection rule
   version.
6. Add rollback/revert/replay operations without dashboard write actions.
7. Add branch/ref/batch/epoch keys to pending scope and semantic jobs.
8. Add dashboard/MCP compact read model.
9. Add merge queue and batch rollback integration.

## Acceptance Bar

Runtime/schema PRs are not acceptable unless they answer:

- Which operation type moved the ref?
- Which event supersedes the previous active ref?
- Which merge epochs became abandoned?
- Which rollback epoch owns the DB transition?
- Which semantic jobs became stale, cancelled, or rebuild_required?
- Which pending scope rows are isolated by branch/ref/batch/epoch?
- Which Governance Hint deltas were inverted?
- Which dashboard/MCP compact field exposes the state?
