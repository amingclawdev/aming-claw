# Test Stage Iteration — 2026-03-30

## Round 1: Test Contract + Worktree Reuse

### Prediction before changes

The first `test` task already had the right metadata:
- `verification.command`
- `test_files`
- inherited `_worktree` / `_branch`

But the actual `tester` runtime path did not use that metadata well:
1. `_build_test_prompt()` only forwarded `changed_files`
2. executor did not explicitly reuse the inherited worktree for `test`
3. tester output schema was underspecified compared with the gate's expected `test_report`

Predicted result:
- without contract/workspace repair, `tester` would either run in the wrong repo context or produce weak output
- even if it completed, the chain signal would be noisy

### What was changed

#### A. Test prompt now carries the real PM/Dev verification contract

In `agent/governance/auto_chain.py`:
- `_build_test_prompt()` now forwards:
  - `verification`
  - `test_files`
  - `changed_files`

#### B. Test execution now reuses the inherited Dev worktree

In `agent/executor_worker.py`:
- `test` and `qa` now reuse metadata `_worktree` / `_branch` when available
- tester context now receives:
  - `verification`
  - `test_files`
- tester prompt now includes:
  - required verification command
  - priority test files
  - strict JSON output schema including `test_report.command`

#### C. Targeted tests

New file:
- `agent/tests/test_test_contract_round1.py`

Coverage:
1. `_build_test_prompt()` forwards `verification` + `test_files`
2. test sessions reuse inherited worktree
3. tester prompt includes required verification command and strict `test_report` schema

### Static verification

Commands run:
- `python3.13 -m pytest agent/tests/test_test_contract_round1.py -q`
- `python3.13 -m py_compile agent/governance/auto_chain.py agent/executor_worker.py agent/tests/test_test_contract_round1.py`

Result:
- `3 passed`
- `py_compile` passed

### First real verify run

Released existing test task:
- `task-1774873430-6505d6`

Observed runtime:
- worktree reused successfully
- prompt length increased, reflecting forwarded verification/test metadata

But the result exposed a real bug:
- tester returned no `test_report`
- summary was `Error: Reached max turns (10)`
- `_gate_t2_pass()` treated missing `test_report` as `failed=0`
- chain incorrectly advanced to QA

This means the first real verify run found a **gate bug**, not a tester success.

## Round 2: Strict `test_report` Gate + Claude Tester Turn Budget

### Prediction before changes

The first live run showed two issues:
1. Claude tester could hit max-turns before emitting JSON
2. `_gate_t2_pass()` incorrectly treated missing `test_report` as pass

Predicted fix:
- increase Claude tester turn budget
- make `test_report` mandatory at gate time
- prevent false advancement to QA when tester only emits a summary/error string

### What was changed

In `agent/ai_lifecycle.py`:
- Claude `tester` turn cap increased to `20`

In `agent/governance/auto_chain.py`:
- `_gate_t2_pass()` now blocks when:
  - `test_report` is missing
  - `passed/failed` counts are missing

In `agent/executor_worker.py`:
- test memory write now skips incomplete/no-report results instead of writing fake `0 passed, 0 failed`

In `agent/tests/test_test_contract_round1.py`:
- added gate regression test for missing `test_report`

### Static verification

Commands run:
- `python3.13 -m pytest agent/tests/test_test_contract_round1.py agent/tests/test_ai_lifecycle_provider_routing.py -q`
- `python3.13 -m py_compile agent/governance/auto_chain.py agent/executor_worker.py agent/ai_lifecycle.py agent/tests/test_test_contract_round1.py agent/tests/test_ai_lifecycle_provider_routing.py`

Result:
- `8 passed`
- `py_compile` passed

### Second real verify run

Created replay test task:
- `task-1774876193-614f70`

Observed result:
- tester reused inherited worktree
- tester ran much longer than the failed 10-turn attempt
- final result contained a valid structured `test_report`

Actual result:
- `passed: 90`
- `failed: 0`
- command:
  - `pytest agent/tests/test_coordinator_decisions.py agent/tests/test_memory_backend.py agent/tests/test_verify_spec.py -v`

Auto-chain outcome:
- `test -> qa` succeeded
- created QA task(s) in `observer_hold`

Concrete QA tasks observed:
- `task-1774876688-ddfe38`
- `task-1774876706-07ca20`

### Round 2 prediction vs actual

| Item | Prediction | Actual | Result |
|------|------------|--------|--------|
| tester reuses inherited worktree | yes | yes | ✅ |
| tester sees required verification command | yes | yes | ✅ |
| missing `test_report` would no longer pass gate | yes | yes (enforced by code) | ✅ |
| tester would avoid early 10-turn failure | likely | yes, produced real test report | ✅ |
| `test -> qa` could advance on valid report | yes | yes | ✅ |

### New blocker exposed by Test iteration

The repaired test path uncovered a new governance issue:

1. duplicate QA task creation
   - same test completion was processed twice
   - dedup logic did not treat existing `observer_hold` QA tasks as already present
   - result: two QA tasks were created for the same parent test task

This is not a `tester` contract failure. It is a downstream chain/dedup problem to address in QA-stage iteration.

### Test stage conclusion

The `test` stage is now meaningfully working:
- it reuses the Dev worktree
- it consumes the real verification contract
- it must emit structured `test_report`
- it can advance valid results into QA

Most important verified outcome:

**`test -> qa(observer_hold)` now works with a real structured `test_report`, and the earlier false-pass bug has been eliminated.**

## Round 3: QA -> Gatekeeper Contract Repair

### Prediction before changes

After `test -> qa` started working, the next expected mismatch was that QA had too little PM-contract context and no isolated acceptance reviewer before merge.

Predicted fixes:
- QA prompt must receive `requirements`, `acceptance_criteria`, `verification`, `doc_impact`
- a separate `gatekeeper` stage should review PM alignment before merge
- dedup must treat `observer_hold` as already existing

### What was changed

In `agent/governance/auto_chain.py`:
- `qa -> gatekeeper` inserted into `CHAIN`
- `_build_qa_prompt()` now forwards PM contract fields and `test_report`
- `_build_gatekeeper_prompt()` added
- `_gate_gatekeeper_pass()` added
- dedup now treats `observer_hold` as existing

In `agent/executor_worker.py` and `agent/role_permissions.py`:
- added `gatekeeper` role handling
- gatekeeper prompt constrained to isolated PM-contract review

New test file:
- `agent/tests/test_qa_gatekeeper_round1.py`

### Verification

Commands run:
- `python3.13 -m pytest agent/tests/test_qa_gatekeeper_round1.py -q`
- `python3.13 -m py_compile agent/governance/auto_chain.py agent/executor_worker.py agent/role_permissions.py agent/ai_lifecycle.py agent/tests/test_qa_gatekeeper_round1.py`

Result:
- `4 passed`
- `py_compile` passed

### Real verify run

Observed chain:
- QA replay succeeded
- gatekeeper task created in `observer_hold`
- gatekeeper executed and returned:
  - `recommendation: merge_pass`
  - `pm_alignment: pass`

This verified that isolated acceptance review now exists before merge.

## Round 4: Merge Isolation Repair

### Prediction before changes

The first live merge attempt failed exactly as expected:
- merge tried to run against dirty `main`
- git rejected it with local-change overwrite protection

Predicted fix:
- keep Dev branch commit in its own worktree
- verify merge in a separate clean integration worktree
- do not depend on the dirty primary workspace for merge proof

### What was changed

In `agent/executor_worker.py`:
- added `_create_integration_worktree(...)`
- `merge` now:
  - commits staged Dev worktree changes
  - creates `merge/<task_id>` integration worktree
  - runs `git merge` there
  - returns `merge_mode: isolated_integration`
  - cleans up both Dev and integration worktrees

New test file:
- `agent/tests/test_merge_round2.py`

### Verification

Commands run:
- `python3.13 -m pytest agent/tests/test_merge_round2.py -q`
- `python3.13 -m py_compile agent/executor_worker.py agent/tests/test_merge_round2.py`

Result:
- `2 passed`
- `py_compile` passed

### Real verify run

Replay of the previously failed merge task:
- `task-1774878577-9d993b`

Actual result:
- `status: succeeded`
- `merge_mode: isolated_integration`
- `merge_commit: 6ee691aedde152284cf85f38400a8d1b6a5b26b2`

This removed the dirty-main-workspace blocker without forcing a real merge into the user's current workspace.

## Round 5: Deploy Moved to Host Executor

### Prediction before changes

After merge succeeded, deploy still failed because:
- deploy was being executed inside governance container
- governance container had no `docker`
- smoke used executor HTTP status only, but `40100` is not reliably exposed in this environment

Predicted fix:
- make `deploy` a first-class host-side task stage
- let executor run `deploy_chain.run_deploy(...)`
- make executor smoke fall back to `manager_status.json`

### What was changed

In `agent/governance/auto_chain.py`:
- chain changed from:
  - `merge -> terminal deploy trigger`
- to:
  - `merge -> deploy -> finalize`
- added:
  - `_build_deploy_prompt()`
  - `_gate_deploy_pass()`
  - `_finalize_chain()`

In `agent/executor_worker.py`:
- added `deploy` script task type
- added `_execute_deploy(...)`

In `agent/deploy_chain.py`:
- executor smoke now falls back to `shared-volume/codex-tasks/state/manager_status.json`

New test files:
- `agent/tests/test_deploy_round3.py`
- `agent/tests/test_e2e_full_chain.py`
- `agent/tests/test_version_gate_round4.py`

### Verification

Commands run:
- `python3.13 -m pytest agent/tests/test_merge_round2.py agent/tests/test_deploy_round3.py agent/tests/test_version_gate_round4.py agent/tests/test_e2e_full_chain.py -q`
- `python3.13 -m py_compile agent/executor_worker.py agent/governance/auto_chain.py agent/governance/db.py agent/deploy_chain.py agent/tests/test_merge_round2.py agent/tests/test_deploy_round3.py agent/tests/test_version_gate_round4.py agent/tests/test_e2e_full_chain.py`

Result:
- `9 passed`
- `py_compile` passed

### Real verify run

Created host-side deploy replay from the successful merge outcome:
- `task-1774879879-f33466`

Actual result:
- `status: succeeded`
- deploy report written to:
  - `shared-volume/codex-tasks/state/deploy_report_2026-03-30T14-11-39Z.json`

Observed report:
- affected services:
  - `executor`
  - `gateway`
  - `governance`
- smoke:
  - `executor: true`
  - `governance: true`
  - `gateway: true`
  - `all_pass: true`
- overall:
  - `success: true`

This is the first fully successful host-side deploy verification for the repaired chain.

## Round 6: Version Gate Re-enabled

### Goal

After the full chain was working, the final guard was re-enabled so observer-side out-of-band edits cannot silently continue through auto-chain.

### What was changed

In `agent/governance/auto_chain.py`:
- `_DISABLE_VERSION_GATE = False`
- version gate now blocks when governance DB reports non-empty `dirty_files`
- stale governance server code vs current git HEAD still blocks

### Verification

Added coverage in:
- `agent/tests/test_version_gate_round4.py`

Confirmed behaviors:
- dirty workspace blocks auto-chain
- clean workspace + matching server version passes
- explicit `skip_version_check` still bypasses when intentionally used

## Current status

The repaired chain is now verified through:
- `coordinator`
- `pm`
- `dev`
- `test`
- `qa`
- `gatekeeper`
- `merge`
- `deploy`

And the chain now has:
- isolated Dev execution
- isolated pre-merge acceptance review
- isolated integration merge proof
- host-side deploy execution
- successful smoke verification
- version-gate protection against out-of-band dirty edits
