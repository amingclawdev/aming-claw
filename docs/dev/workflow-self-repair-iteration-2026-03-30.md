# Workflow Self-Repair Iteration (2026-03-30)

## Goal

Add the first minimal self-repair loop so the workflow can distinguish:

- business-task failures
- workflow/governance failures

and automatically create a governed repair task for the second category.

## Round 1 Scope

This round intentionally keeps the feature narrow.

Implemented:

- heuristic failure classifier
- workflow-improvement task prompt builder
- auto-chain hook that creates a governed repair task when a gate failure is classified as a workflow defect

Not implemented yet:

- full failure taxonomy across all layers
- replay-aware auto verification of the repair task
- automatic closure of dirty-workspace blockers
- automatic graph-trace acceptance upgrades

## Design Choice

Do not introduce a new executor role for self-repair.

Instead:

- create a normal coordinator-entry `task`
- mark it with `operation_type = workflow_improvement`
- let the existing chain repair the workflow through the same governance path

This keeps self-repair:

- auditable
- observer-visible
- consistent with the current workflow architecture

## Current Classification Policy

### Treated as workflow defects

- graph defects
- contract defects
- provider/tooling defects

### Not treated as workflow defects

- dirty workspace / version gate environment blockers
- ordinary test failures
- QA rejection due to business-task quality
- merge conflicts caused by task content

## Expected Behavior

When auto-chain sees a gate failure:

1. classify it
2. if it is a workflow defect:
   - create one `workflow_improvement` task
   - continue normal retry behavior if configured
3. if it is only an environment or business-task defect:
   - do not create a workflow-improvement task

## Verification

Round 1 tests cover:

- graph defect classification produces a workflow-improvement task
- dirty workspace version-gate failure does not produce one
- created repair tasks re-enter the normal chain through `type=task`

## Known Limits

1. classification is heuristic, not evidence-trace-backed
2. Observer is still needed to validate whether the produced workflow-improvement task is sensible
3. dirty workspace is still an explicit governance blocker rather than an auto-reconciled path
4. repair tasks are created, but not yet auto-replayed against a fixed regression set

## Next Step

After this round, the next useful upgrades are:

1. persist structured failure classification into audit and memory
2. attach replay case ids to workflow-improvement tasks
3. auto-run replay validation after a workflow-improvement task succeeds
4. tighten QA/Gatekeeper toward graph-trace-driven acceptance
