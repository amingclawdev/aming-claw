# Session Handoff 2026-03-31: Main Merge Blocker

## Summary

Current observer work uncovered a still-open workflow defect:

- `merge` tasks can report `succeeded` and emit a `merge_commit`
- downstream `deploy` can be auto-created
- but the real repository `main/HEAD` does **not** actually advance to that merge commit

This means the workflow is still vulnerable to a false-positive chain:

- `dev -> test -> qa -> gatekeeper -> merge` all look successful in governance
- but live `main` remains on the old commit
- therefore `deploy` must **not** be released when this happens

At handoff time, the chain is blocked correctly at `deploy` because releasing it would deploy from a false merge result.

## Current Core Problem

The latest merge verification repair chain was intended to fix exactly this issue, but the workflow still reproduced the same failure mode.

Latest observed chain:

- `gatekeeper` succeeded:
  - `task-1774974321-2214a2`
- `merge` succeeded:
  - `task-1774984674-788e5f`
  - reported `merge_commit = a95a681f9631158bbb3b5db914001569b6718a96`
- downstream `deploy` was auto-created:
  - `task-1774984720-924507`
  - status: `observer_hold`

But real repo state at the same time:

- `git rev-parse HEAD`
  - `568fcd9e371468fa34365431029c0d12be4bc0af`
- `git merge-base --is-ancestor a95a681f9631158bbb3b5db914001569b6718a96 HEAD`
  - `not-ancestor`
- governance health version:
  - `568fcd9`

So the core defect is still:

- workflow-level merge success is not yet tightly bound to real `main/HEAD` advancement

## Why This Matters

If `deploy` is released after a false merge success:

- services may restart or smoke-pass against the old code
- task history will imply the fix is live when it is not
- version and deployment evidence become untrustworthy

This is a release-integrity problem, not just a logging problem.

## Current Blocking Task

Do **not** release this task until the merge integrity issue is fixed or reconciled:

- `task-1774984720-924507`
- type: `deploy`
- status: `observer_hold`
- parent merge task: `task-1774984674-788e5f`

Reason to keep it held:

- reported merge commit is not reachable from current `HEAD/main`

## What Was Already Attempted

We already ran a narrow repair chain for "real main merge verification".

Observer-authored repair task:

- `task-1774973665-30e45a`

Scope:

- `agent/executor_worker.py`
- `agent/tests/test_merge_round2.py`
- `docs/human-intervention-guide.md`

Intent:

1. require merge success to mean real `main/HEAD` advanced
2. fail closed if integration only happened in an isolated context
3. add regression coverage

Chain progression:

- `dev` succeeded
- `test` succeeded with `6 passed`
- `qa` succeeded
- `gatekeeper` succeeded
- `merge` still reproduced the false-success pattern

Conclusion:

- the prior fix improved validation logic, but it did **not** fully constrain the actual merge execution/finalization path

## Most Likely Remaining Defect Area

Next session should focus on the real merge execution path in:

- `agent/executor_worker.py`

Most likely one of these remains true:

1. merge success is being decided from an isolated integration repo/worktree without a final hard check that the live repo ref moved
2. the code creates a merge commit object but does not update the actual `main` ref in the live workspace
3. the success result is emitted before verifying ref update in the real repository used by subsequent deploy/version checks

## Recommended Next Actions

### Priority 1: Fix merge execution truthfulness

Create a new narrow repair chain that:

1. traces the exact merge execution path in `agent/executor_worker.py`
2. verifies which repo/worktree/ref is being updated
3. requires success only if the live workspace ref actually advances
4. fails closed if emitted `merge_commit` is not reachable from live `HEAD`

Suggested scope:

- `agent/executor_worker.py`
- `agent/tests/test_merge_round2.py`
- optionally a second focused test file if merge-path isolation is easier to express there
- `docs/human-intervention-guide.md`

### Priority 2: Add stronger regression coverage

Tests should prove all of these:

1. a merge commit created only in an isolated context is a failure
2. success requires `HEAD` to advance in the real workspace
3. downstream deploy is not emitted from a false merge success

### Priority 3: Only then release deploy

After the merge-integrity fix lands and is verified:

1. rerun the merge stage
2. confirm new merge commit is reachable from real `HEAD`
3. only then release `deploy`
4. verify smoke pass and service version movement

## Validation Checklist For Next Session

Before releasing any future `deploy`, verify:

1. `git rev-parse HEAD`
2. governance health version
3. `git merge-base --is-ancestor <reported_merge_commit> HEAD`
4. task result `changed_files` still match intended fix scope

Deploy should only proceed when:

- ancestor check passes
- `HEAD` actually moved
- governance version can later converge to the new commit after deploy/restart

## Related Open Optimization Items

These are still open and should remain on the optimization backlog:

1. `merge/deploy` consistency hardening
2. runtime artifact filtering from merge payloads
3. restart drain policy when executor has active work
4. role rule documents completion
5. memory write/read contract formalization
6. trace-driven QA/gatekeeper and stronger node/file/doc/test binding

## Notes On Observer Operation

Current observer pattern that worked well:

- inspect each `observer_hold`
- release only after checking the prior stage output quality
- do **not** release `deploy` when real repo state contradicts merge success

Current observer pattern that should be avoided:

- assuming `merge succeeded` in governance means live `main` moved

## Current Workspace State

At handoff time:

- repo `HEAD = CHAIN_VERSION = 568fcd9e371468fa34365431029c0d12be4bc0af`
- working tree not dirty from governed code changes
- local untracked docs in `docs/dev` still exist:
  - `docs/dev/observer-monitoring-prompt-template.md`
  - `docs/dev/restart-drain-policy-todo-2026-03-31.md`

## Immediate Instruction For Next Session

Start from this question:

> Why does `agent/executor_worker.py` still allow `task-1774984674-788e5f` to return merge success with `merge_commit=a95a681f9631158bbb3b5db914001569b6718a96` while real `HEAD` remains `568fcd9e371468fa34365431029c0d12be4bc0af`?

Do not release:

- `task-1774984720-924507`

until that question is answered and repaired.
