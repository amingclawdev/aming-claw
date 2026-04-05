# Auto-Chain — Automated Stage Progression

> **Canonical governance topic document** — How the auto-chain pipeline orchestrates task stages.
> Last updated: 2026-04-05 | Phase 2 Documentation Consolidation

## Overview

The auto-chain is the core workflow automation in Aming Claw. It automatically progresses tasks through a fixed sequence of governance stages, with gate checks at each transition to ensure quality.

## Stage Sequence

```
PM → Dev → Test → QA → Gatekeeper → Merge
```

Each stage represents one task in the governance task queue. When a task completes successfully and passes its gate check, the auto-chain automatically creates the next stage's task.

### Stage Responsibilities

| Stage | Role | Purpose |
|-------|------|---------|
| PM | Project Manager | Define scope, target_files, acceptance criteria, verification plan |
| Dev | Developer | Implement changes per PM PRD |
| Test | Tester | Run automated tests, verify test evidence |
| QA | QA Agent | Review code, verify acceptance criteria, E2E checks |
| Gatekeeper | Automated | Final validation before merge |
| Merge | Automated | Git merge to main, version update |

## Stage Transitions

### PM → Dev

**Trigger:** PM task completes with `status=succeeded`

**PM Gate checks:**
- Result contains `target_files` (non-empty list)
- Result contains `verification` (string describing how to verify)
- Result contains `acceptance_criteria` (non-empty list)

**On pass:** Creates Dev task with PM's PRD as prompt, target_files carried forward.

**On fail:** Creates retry PM task with gate failure reason in prompt.

### Dev → Test

**Trigger:** Dev task completes with `status=succeeded`

**Checkpoint Gate checks:**
- Result contains `changed_files` (non-empty list)
- Changed files exist in `git diff` output
- Changed files are within scope of PM's `target_files`

**On pass:** Creates Test task with verification command from PM PRD.

**On fail:** Creates retry Dev task with checkpoint failure details.

### Test → QA

**Trigger:** Test task completes with `status=succeeded`

**T2 Pass Gate checks:**
- Result contains `test_report` as a dict (not string)
- `test_report.passed > 0`
- `test_report.failed == 0`

**On pass:** Creates QA task with acceptance criteria from PM PRD.

**On fail:** Creates retry Test task (or Dev task if test failures indicate code issues).

### QA → Gatekeeper

**Trigger:** QA task completes with `status=succeeded`

**QA Pass Gate checks:**
- Result contains `recommendation == "qa_pass"`
- All `criteria_results` entries have `passed: true`

**On pass:** Creates Gatekeeper task.

**On fail:** Creates retry QA task (or Dev task if criteria failures indicate code issues).

### Gatekeeper → Merge

**Trigger:** Gatekeeper validates all gates passed in sequence.

**On pass:** Creates Merge task with branch/worktree metadata.

### Merge → Complete

**Trigger:** Merge task completes.

**Actions:**
1. Git merge/cherry-pick to main branch
2. Call `version-update` API with new HEAD
3. Update `chain_version` in governance DB
4. Chain completes

## Observer Mode Interaction

When `observer_mode=ON`:
- All auto-created tasks enter `observer_hold` status instead of `queued`
- Observer must explicitly `task_release` or `task_claim` each task
- This allows human review at every stage transition

When `observer_mode=OFF`:
- Tasks enter `queued` status and are auto-claimed by executor
- Fully automated pipeline with no human intervention

## Gate Failure Handling

### Retry Logic

- Gate failure triggers automatic retry task creation
- Retry task prompt includes the failure reason and original context
- Maximum retry attempts are configurable per stage
- Failed tasks are marked with `exec_status=gate_blocked`

### Dedup Guards (D4 Fix)

The auto-chain includes dedup guards to prevent duplicate retry task creation:
- Checks for existing retry tasks with same parent task
- Prevents multiple retries from racing conditions
- Implemented in `auto_chain.py` with atomic DB checks

### Dirty Workspace Filter (D5 Fix)

The version gate's dirty_files check filters out `.claude/` paths to prevent false positives from settings synchronization:
- `.claude/settings.local.json` is synced by executor every 60s
- Without filtering, this would permanently block all auto-chain dispatch
- Remaining dirty files are downgraded to warning-only

## Implementation

Key file: `agent/governance/auto_chain.py`

The auto-chain runs as a background thread within the governance service. It:
1. Monitors task completion events
2. Evaluates the appropriate gate for the completed stage
3. Creates the next stage's task if gate passes
4. Creates a retry task if gate fails
5. Logs all decisions to the audit log

## Chain Context (Phase 8)

Each auto-chain run maintains event-sourced runtime context:
- Stage history with timestamps and outcomes
- Gate check results
- Accumulated metadata across stages
- Auto-archive of failed chain context for debugging

See `agent/governance/auto_chain.py` for the `ChainContext` class.
