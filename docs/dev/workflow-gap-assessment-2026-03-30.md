# Workflow Gap Assessment (2026-03-30)

## Purpose

Summarize the remaining gaps between the current repaired codebase and a workflow that can run reliably in live governance with strict policy enabled.

This document focuses on:

- what already works
- what still blocks normal live operation
- what is degraded but temporarily tolerable
- what should be restored next

## Current Working Baseline

The repaired codebase already supports a full chain:

- `coordinator -> pm -> dev -> test -> qa -> gatekeeper -> merge -> deploy`

The following capabilities are already implemented and verified in prior rounds:

- Dev isolated worktree
- Test/QA reuse Dev worktree context
- Gatekeeper stage before merge
- Isolated integration merge
- Host-side deploy
- Smoke test after deploy
- Version gate re-enabled
- Observer limited node-governance recovery APIs

## Gap Classification

### A. Live-Run Blockers

These are the gaps that can still prevent the workflow from running normally in the live environment.

#### A1. Live governance service may not yet be running the latest repaired code

Impact:

- New APIs and permission fixes do not exist until the live governance service is restarted or redeployed.

Examples:

- observer-limited `import-graph`
- `observer-sync-node-state`
- `gatekeeper` role-permission fix
- stricter `node-update` coordinator-only enforcement

Required action:

- deploy or restart governance with the current code

#### A2. Live `node_state` runtime layer is still missing or incomplete

Observed state from governance API:

- graph definition exists
- runtime `node_state` was previously empty

Impact:

- strict node-based governance is not trustworthy yet
- node gate cannot be safely restored to blocking mode until runtime state is recovered

Required action:

- use governance API only:
  - `POST /api/wf/{project_id}/import-graph` with Observer recovery reason
  - or `POST /api/wf/{project_id}/observer-sync-node-state`

#### A3. Version gate can block dirty live workspaces

Current state:

- version gate is intentionally enabled again
- dirty workspaces are treated as a blocker

Impact:

- if the live project still contains out-of-band edits, auto-chain can stop before advancing

Required action:

- clear or commit live dirty changes through the normal workflow path
- do not bypass this gate casually, because it is the last defense against Observer overreach and manual code drift

### B. Deliberately Relaxed Governance

These are not fatal to the chain running, but they mean the workflow is still operating in a softened mode.

#### B1. Dev-stage node gate is still relaxed

Current state:

- node gate was reduced to log-only during recovery rounds so the chain could move forward while graph runtime state was broken

Impact:

- workflow can run
- but node governance is not yet enforcing full correctness

Required action:

- after runtime `node_state` is restored in live governance, switch Dev-stage node gate back to blocking

#### B2. QA and Gatekeeper are still mostly contract-driven, not graph-trace-driven

Current state:

- QA and Gatekeeper validate PM contract, test evidence, changed files, and doc impact
- they do not yet fully prove requirement-to-node-to-scenario traceability

Impact:

- merge quality is much better than before
- but still not at full graph-driven acceptance strength

Required action:

- later phase:
  - add `requirement_coverage`
  - add `acceptance_trace`
  - add stronger node evidence checks

### C. Stabilization Gaps

These are the gaps that do not necessarily stop the workflow today, but should be closed before calling the system stable.

#### C1. Role spec docs are still incomplete

Still missing:

- `docs/dev-rules.md`
- `docs/test-rules.md`
- `docs/qa-rules.md`
- `docs/gatekeeper-rules.md`

Impact:

- role behavior is enforced mostly in code and prompts, but lacks clean single-source documentation

#### C2. Memory policy is not yet fully role-governed

Current state:

- schema is stronger than before
- but role-specific read/write policy is not yet fully fixed in both docs and code

Impact:

- memory remains useful
- but long-term autonomy and audit quality are weaker than desired

#### C3. Graph is not yet a strict minimal-verification engine

Current state:

- graph exists
- node definitions exist
- some coverage checks exist

Missing:

- stable `file -> node`
- stable `node -> tests`
- stable `node -> docs`
- stable `node -> acceptance scenarios`

Impact:

- graph can guide
- but cannot yet always minimize the exact tests/docs/scenarios automatically

#### C4. Failure classification is still weak

Current state:

- the workflow can fail, retry, and be inspected
- but it still relies heavily on human diagnosis for why a run failed

Impact:

- Observer still participates in troubleshooting rather than mostly monitoring

## Newly Closed Gaps In This Round

These were gaps before this round and are now addressed in code:

- Observer can now repair graph runtime state through governance API instead of DB access
- Observer recovery requires explicit `reason`
- Observer cannot use `node-update` as a broad shortcut anymore
- `gatekeeper` is no longer an unknown role in the role-permission matrix
- governance recovery path is now auditable as repair, not acceptance

## Recommended Recovery Sequence For Live Workflow

### Step 1. Deploy the repaired governance service

Why first:

- live environment must expose the new recovery APIs and permission fixes

### Step 2. Recover live graph runtime state through governance API only

Preferred order:

1. check `GET /api/wf/{project_id}/summary`
2. if runtime state is empty:
   - run `import-graph` with Observer reason if graph import is needed
   - otherwise run `observer-sync-node-state`
3. re-check summary

Success target:

- `total_nodes > 0`
- counts roughly align with graph definition

### Step 3. Re-enable strict Dev-stage node gate

Condition:

- only after live runtime node state is healthy again

Success target:

- unrelated or uncovered node changes are blocked again

### Step 4. Make sure live worktree passes version gate

Condition:

- no uncontrolled dirty workspace state remains

Success target:

- chain can proceed without dirty-workspace rejection

### Step 5. Run one full-chain smoke on live governance

Target chain:

- `coordinator -> pm -> dev -> test -> qa -> gatekeeper -> merge -> deploy`

Success target:

- all stages advance normally
- no ad-hoc DB repair is needed
- gates behave as intended

## Exit Criteria For “Workflow Can Run Normally”

The workflow should be considered normally operational only when all of the following are true:

1. live governance is running the repaired code
2. live `node_state` has been restored through governance API
3. Dev-stage node gate is back to blocking mode
4. version gate passes under normal workflow usage
5. one full-chain smoke succeeds in live governance

## Follow-Up Work After Recovery

After the live workflow is back to normal operation, the next non-blocking priorities should be:

1. complete role spec docs
2. formalize role-based memory rules
3. make graph the stronger source of:
   - minimal tests
   - required docs
   - acceptance scenarios
4. add failure classification
5. move toward Observer-mostly-monitoring mode
