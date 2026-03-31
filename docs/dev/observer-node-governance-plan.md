# Observer Limited Node Governance Plan

## Goal

Allow `observer` to repair acceptance-graph runtime state through governance APIs only, without turning Observer into a bypass path for verification or release.

This plan is specifically for recovery scenarios such as:

- graph definitions exist but `node_state` is empty
- graph was imported before, but runtime state needs to be rebuilt
- governance data drift needs a safe, auditable repair path

It is not for normal implementation, test, QA, or release progression.

## Design Principles

1. Observer may repair governance state, not assert business acceptance.
2. All repairs must go through governance REST APIs.
3. Every repair action must carry a human-readable `reason`.
4. Recovery APIs must be idempotent and safe to re-run.
5. Observer must not directly set nodes to `testing`, `t2_pass`, or `qa_pass`.

## Allowed Observer Capabilities

### A. Graph Import for Recovery

Observer may trigger graph import when runtime node state is missing or corrupted.

Allowed effect:

- import markdown graph definition
- persist `graph.json`
- initialize or re-sync `node_state`

Required inputs:

- `md_path`
- `reason`

Expected audit meaning:

- governance repair
- not a verification event

### B. Runtime Node-State Rebuild

Observer may trigger a dedicated rebuild/sync operation when:

- `graph.json` already exists
- `node_state` is empty or partially missing

Allowed effect:

- create missing `node_state` rows from graph definitions
- sync only import-declared baseline-like statuses already encoded in graph markdown
- never promote nodes based on Observer judgment alone

Required inputs:

- `reason`

Expected audit meaning:

- state recovery from graph definition
- not feature acceptance

## Explicitly Forbidden for Observer

Observer must not be allowed to:

- call `verify-update` to mark nodes `testing`
- call `verify-update` to mark nodes `t2_pass`
- call `verify-update` to mark nodes `qa_pass`
- use `node-update` or `node-batch-update` as a substitute for acceptance
- directly write DB rows outside governance APIs

## API Changes

### 1. Extend `POST /api/wf/{project_id}/import-graph`

Current state:

- coordinator-only

Planned change:

- allow `observer` in addition to `coordinator`
- require non-empty `reason` when caller role is `observer`
- record audit fields that distinguish observer recovery from coordinator authoring

### 2. Add `POST /api/wf/{project_id}/observer-sync-node-state`

Purpose:

- rebuild or re-sync runtime `node_state` from the existing graph definition

Permissions:

- `observer`
- `coordinator`

Required body:

```json
{
  "reason": "Recover node_state after governance DB corruption"
}
```

Response shape:

```json
{
  "project_id": "aming-claw",
  "graph_nodes": 123,
  "node_states_initialized": 123,
  "node_state_total": 123,
  "repair_mode": "sync_from_graph"
}
```

## Audit Requirements

Both recovery APIs must record:

- `actor`
- `role`
- `reason`
- `project_id`
- `operation`
- `graph_nodes`
- `node_states_initialized`

Suggested event names:

- `observer_graph_import`
- `observer_node_state_sync`

## Permission Model Impact

### Gatekeeper Matrix Fix

`gatekeeper` must be added to the role permission matrix so it is no longer treated as an unknown role.

Expected stance:

- read/review-oriented actions only
- no `verify_update`
- no `modify_code`
- no `release_gate`

## Validation Plan

1. Observer import with missing `reason` must fail.
2. Observer import with `reason` must succeed.
3. Observer node-state sync must initialize missing `node_state` rows from graph.
4. Observer must still be unable to promote nodes through `verify-update`.
5. `gatekeeper` must no longer return `unknown role`.

## Rollout Order

1. Land this restricted recovery path.
2. Recover `node_state` through governance API only.
3. Re-check `/api/wf/{project_id}/summary`.
4. Once graph runtime state is healthy again, restore stricter node gating in the workflow.
