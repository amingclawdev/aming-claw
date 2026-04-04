# Restart Drain Policy TODO

## Background

During observer-led workflow repair, we confirmed that task state survives service restarts because governance persists tasks in the host-side state store. However, this does not mean arbitrary restarts are always safe while the executor is actively running a claimed task.

The current risk is not task disappearance. The real risk is mid-flight interruption:

- `dev/test/qa/merge/deploy` work can be cut off while executing
- isolated worktree metadata may be left half-updated
- downstream observer reconciliation becomes more expensive than a delayed restart

## Problem

Service restart behavior should distinguish between:

1. safe delayed reload while active executor work is draining
2. forced restart for crash, deadlock, or explicit operator override

At the moment, this policy is only partially implicit. It is not yet clearly enforced as a workflow rule across restart and deploy paths.

## Desired Policy

- If executor has active claimed/running work, default restart path should delay and use drain/reload semantics
- Normal deploy/restart should wait for active work to complete, subject to timeout
- Forced restart should be reserved for:
  - crashed or wedged process
  - crash-loop recovery
  - explicit operator override
  - urgent safety fix
- Observer and deploy automation should both use the same restart policy language and result contract

## Existing Foundation

There is already partial support in [`agent/service_manager.py`](C:\Users\z5866\Documents\amingclaw\aming_claw\agent\service_manager.py):

- `reload()` polls active task count
- it waits for drain before stop/start
- it still allows timeout-based continuation

This means the optimization is not greenfield. The remaining work is to formalize and consistently apply the policy in workflow/deploy behavior.

## Optimization Task

Future workflow optimization should add a narrow governed change that:

- formalizes restart policy for active executor workloads
- makes deploy/restart prefer drain-aware reload
- clearly separates soft reload from forced restart
- adds focused tests for active-task delay behavior
- documents operator expectations in formal docs, not only `docs/dev`

## Suggested Acceptance Shape

- active executor work blocks ordinary restart until drained or timeout reached
- forced restart remains available but explicit
- deploy result reports whether restart was delayed, drained, forced, or skipped
- no task-loss regression across restart

## Observer Note

This is a new pending optimization item. It should be treated as workflow hardening, not as an emergency runtime defect.
