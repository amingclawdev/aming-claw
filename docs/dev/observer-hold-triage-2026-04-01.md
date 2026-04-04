## Observer Hold Triage - 2026-04-01

### Purpose
- Classify current `observer_hold` backlog into:
  - active main-chain tasks to keep
  - docs-architecture related tasks
  - stale / duplicate tasks that can likely be cancelled
- Cross-check against commits already landed on `main`

### Active Main-Chain Tasks To Keep

#### `task-1775086841-467a2b`
- Type: `dev`
- Why it exists:
  - retry from `task-1775084898-c99dd0`
  - gate reason: `Test stage missing required test_report`
- Status:
  - still relevant to the current merge-repair chain rooted at `task-1775083288-44d032`
- Recommendation:
  - keep

#### `task-1775086727-64af56`
- Type: `task`
- Why it exists:
  - workflow-improvement root for current graph defect
  - `L1.3=pending (not found in DB)`
- Status:
  - still relevant because the latest main chain is blocked on the same graph/runtime mismatch
- Recommendation:
  - keep

### Older Same-Issue Task Superseded By Newer Main-Chain Task

#### `task-1775084514-110f58`
- Type: `task`
- Issue:
  - same defect class and same issue summary as `task-1775086727-64af56`
  - both are `graph_defect` roots for `L1.3=pending (not found in DB)`
- Superseded by:
  - `task-1775086727-64af56`
- Recommendation:
  - cancel as duplicate

### Merge Task Still Useful As Evidence, But Not The Current Repair Target

#### `task-1775083021-253f2f`
- Type: `merge`
- Status:
  - `observer_hold`
- Failure:
  - ff-only merge blocked by untracked file:
  - `docs/dev/roadmap-2026-03-31.md`
- Role in backlog:
  - root evidence for the current repair chain
  - not the task that should be resumed directly right now
- Recommendation:
  - keep for traceability
  - do not release directly

### Superseded By Later Successful Repair Chains

#### `task-1775082995-97fd74`
- Type: `dev`
- Original purpose:
  - fix QA-stage fallout in the PM max-turns / doc tracking chain
- Why stale:
  - later chain already succeeded further:
    - `task-1775082017-8bb5cd` `dev`
    - `task-1775082299-23e3c7` `test`
    - `task-1775082585-82a138` `qa`
    - `task-1775082975-d42ee5` `gatekeeper`
  - current focus moved to merge-untracked-doc repair
- Recommendation:
  - cancel as stale

#### `task-1775081371-87218e`
- Type: `dev`
- Original purpose:
  - fix QA fallout after PM max-turns regression repair
- Why stale:
  - superseded by later successful chain rooted at `task-1775081344-7b0e5e`
- Recommendation:
  - cancel as stale

#### `task-1775073919-1973c1`
- Type: `pm`
- Original purpose:
  - doc gate false-block repair
- Why stale:
  - later repair chains already moved past this stage
  - current active issue is merge-untracked-doc plus graph/runtime follow-up
- Recommendation:
  - cancel as stale unless needed for forensic reference

### Already Implemented On Main; Remaining Holds Are Obsolete

#### `task-1775062649-6c437d`
- Type: `gatekeeper`
- Topic:
  - waived nodes should satisfy min-status gates
- Matching landed commit:
  - `3806939 fix: treat waived nodes as universally passing in _check_nodes_min_status gate`
- Recommendation:
  - cancel as obsolete

#### `task-1775063676-4b5717`
- Type: `qa`
- Topic:
  - same waived-node gate semantics chain as above
- Matching landed commit:
  - `3806939 fix: treat waived nodes as universally passing in _check_nodes_min_status gate`
- Recommendation:
  - cancel as obsolete

#### `task-1775055992-0f322b`
- Type: `task`
- Topic:
  - broad graph defect / docs governance / archive migration chain
  - original failure included waived nodes being treated as unknown
- Matching landed commits:
  - `3806939 fix: treat waived nodes as universally passing in _check_nodes_min_status gate`
  - `b658c12 Docs Phase 1: Archive 14 superseded docs, create role stubs and directory structure`
  - `d0cea15 docs: docs governance proposal v3 + session handoff 2026-04-01`
- Assessment:
  - the original issue is partly governance/docs-architecture related, but the gate symptom that triggered it is already addressed
- Recommendation:
  - do not release as-is
  - either cancel, or replace with a fresh docs-architecture-specific task if still needed

#### `task-1775048706-40b6e8`
- Type: `dev`
- Topic:
  - old QA fallback inside the target-file inferred test gate repair chain
- Matching landed commit:
  - `e0104e5 feat: auto-derive allowed test files from target_files stems in _gate_checkpoint`
- Assessment:
  - this hold belongs to an already-merged chain
- Recommendation:
  - cancel as obsolete

#### `task-1775048752-fa7f19`
- Type: `dev`
- Topic:
  - stale retry from the PM role refinement chain
- Matching landed commits:
  - `d93cb5a feat: PM role refinement — turn cap 60→5 + target_files code preview`
  - later superseded by `5b09ad0 fix: increase PM role max_turns cap from 10 to 60`
- Assessment:
  - this task is from an older chain and should not be resumed
- Recommendation:
  - cancel as obsolete

### Summary Recommendation

#### Keep
- `task-1775086841-467a2b`
- `task-1775086727-64af56`
- `task-1775083021-253f2f` as evidence only

#### Cancel as duplicate
- `task-1775084514-110f58`

#### Cancel as stale / obsolete
- `task-1775082995-97fd74`
- `task-1775081371-87218e`
- `task-1775073919-1973c1`
- `task-1775062649-6c437d`
- `task-1775063676-4b5717`
- `task-1775055992-0f322b`
- `task-1775048706-40b6e8`
- `task-1775048752-fa7f19`
