# Workflow Gap Decision Record (2026-03-30)

## Purpose

Convert the current workflow gap assessment into an explicit execution decision set.

This document answers four concrete questions:

1. whether Observer should import baseline nodes and let auto-chain update status later
2. how dirty workspaces should be handled relative to version gate
3. whether QA and Gatekeeper should evolve from contract-driven to graph-trace-driven through workflow iterations
4. whether graph/doc/test/code relationships must be fully established before governance import

It also defines the execution order that follows from those decisions.

## Decision 1: Observer May Restore Node Baseline, But Auto-Chain Should Only Advance Touched Nodes

### Decision

Observer is allowed to:

- import the current acceptance graph baseline
- restore or rebuild runtime `node_state`
- re-establish governance visibility over all nodes

Observer is not expected to:

- automatically re-verify all historical nodes through normal auto-chain
- reconstruct historical verification truth that was never encoded in graph markdown or audit evidence

### Meaning

After baseline recovery:

- all nodes become governable again
- future workflow runs update only the nodes touched by real tasks
- historical state must be rebuilt either from:
  - import-declared statuses already present in graph markdown
  - dedicated replay / baseline-reconstruction work

### Operational Rule

Use Observer recovery APIs to restore graph runtime state first, then let normal workflow advance node statuses incrementally on future tasks.

## Decision 2: Dirty Workspace Must Not Be Silently Absorbed; Version Gate Stays Enabled

### Decision

Do not disable `version gate` just to allow dirty workspace changes to flow through.

### Meaning

Dirty workspace content must be handled explicitly as one of:

- a normal governed task
- a dedicated cleanup / reconciliation task
- a deliberate discard outside the chain

It must not be silently mixed into unrelated workflow tasks.

### Operational Rule

`version gate` remains enabled.

If the live workspace is dirty:

1. identify the outstanding files
2. decide whether they belong to:
   - a new governed task
   - an already open task
   - explicit discard
3. only then continue the normal chain

## Decision 3: QA and Gatekeeper Should Evolve Through Workflow Iteration to Graph-Trace-Driven Acceptance

### Decision

Yes. QA and Gatekeeper should evolve through workflow iterations from contract-driven acceptance toward graph-trace-driven acceptance.

### Meaning

Current state is acceptable for chain continuity:

- QA checks PM contract, tests, docs, and changed files
- Gatekeeper checks PM alignment before merge

Target state should add structured traceability:

- `requirement -> files`
- `requirement -> tests`
- `requirement -> docs`
- `requirement -> nodes`
- `acceptance criteria -> evidence`
- `node -> scenarios`

### Operational Rule

Do not try to “upgrade QA/Gatekeeper in one prompt change”.

Instead:

1. stabilize graph mappings
2. stabilize evidence schema
3. add `requirement_coverage`
4. add `acceptance_trace`
5. then tighten QA and Gatekeeper using those artifacts

## Decision 4: Establish Minimal Governance Mapping Before Import, Then Iterate in Workflow

### Decision

Do not wait until every graph/doc/test/code relationship is perfect before importing governance.

Also do not import with nearly empty mappings and expect strict governance to work immediately.

### Meaning

The correct approach is staged:

#### First establish a minimum viable governance mapping

Required before strict operation:

- graph definitions
- runtime `node_state`
- core `file -> node`
- basic `node -> docs`
- role contracts for PM / Dev / Test / QA / Gatekeeper

#### Then iterate richer mappings through workflow

Add later:

- `node -> tests`
- `node -> acceptance scenarios`
- `requirement_coverage`
- `acceptance_trace`
- graph-driven QA and Gatekeeper

### Operational Rule

Import governance after the minimum viable mapping exists, then iterate stronger graph-driven behavior through the workflow itself.

## Execution Decisions

These decisions imply the following sequence.

### Phase A. Restore Governance Runtime

1. verify the live governance service is running the repaired code
2. recover live `node_state` through governance API only
3. confirm `GET /api/wf/{project_id}/summary` shows real nodes again

### Phase B. Keep Safety Gates On

1. keep `version gate` enabled
2. do not absorb dirty workspace implicitly
3. convert dirty workspace into governed work before continuing

### Phase C. Re-tighten Graph Governance

1. restore strict Dev-stage node gate after runtime graph recovery
2. re-run live smoke on the normal chain

### Phase D. Evolve Acceptance Strength

1. build stronger `file -> node -> docs -> tests` mapping
2. add `requirement_coverage`
3. add `acceptance_trace`
4. upgrade QA and Gatekeeper to graph-trace-driven operation

## Immediate Implementation Order

The next concrete actions should be:

1. check whether live governance already exposes the new Observer recovery APIs
2. if yes, use governance API to restore `aming-claw` graph runtime state
3. if no, treat live deployment as the active blocker
4. after runtime graph recovery, assess live dirty workspace before attempting full-chain smoke

## Execution Update

### Executed In This Round

1. built and restarted the live `governance` container
2. confirmed `observer-sync-node-state` is live
3. restored live `aming-claw` runtime node state through governance API
4. re-checked live summary
5. re-checked version gate status

### Observed Live Results

- `GET /api/wf/aming-claw/summary`
  - before recovery: `total_nodes = 0`
  - after recovery: `total_nodes = 117`
- observer recovery path was exercised with an Observer token
- resulting live summary currently reports:
  - `pending = 117`
- `version gate` remains enabled and still blocks because the workspace is dirty

### Updated Active Blocker

The next blocker is no longer missing runtime node state.

The active blocker is now:

- dirty workspace reconciliation under an enabled `version gate`

### Immediate Next Action

Before a full-chain live smoke:

1. classify current dirty files
2. decide which dirty changes belong to governed tasks
3. reconcile them explicitly instead of disabling `version gate`

## Success Criteria

This decision set is considered successfully applied when:

1. live `node_state` is restored through governance API
2. `version gate` remains enabled
3. dirty workspace is handled explicitly, not implicitly absorbed
4. Dev-stage node gate can be safely tightened again
5. the workflow is ready for the next graph-trace strengthening round
