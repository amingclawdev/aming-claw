# Auto-Chain — Automated Stage Progression

> **Canonical governance topic document** — How the auto-chain pipeline orchestrates task stages.
> Last updated: 2026-04-24 | Phase 2 Documentation Consolidation + B/G-series updates + B24 chain integrity + MF-2026-04-24 connection-contention fixes

> **2026-04-11 (B24):** Chain integrity verification — retry dev prompts now include `SCOPE CONSTRAINT` populated by `get_retry_scope()` which accumulates changed_files from prior dev attempts. Version gate remains warning-only for merge/deploy stages (D3/B29/B30 fixes preserved).

## Overview

The auto-chain is the core workflow automation in Aming Claw. It automatically progresses tasks through a fixed sequence of governance stages, with gate checks at each transition to ensure quality.

> **See also:** [reconcile-workflow.md](reconcile-workflow.md) §11 for gate exemption patterns
> used during reconcile operations (references `_gate_release`, `_gate_qa_pass`, `_gate_t2_pass`).

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

## Retry Budget Per-Stage (B8)

Each stage in the auto-chain has an independent retry budget rather than a single global retry counter. This ensures that a flaky test stage does not exhaust retries intended for other stages.

- **Default budget:** 2 retries per stage (configurable via `CHAIN_RETRY_BUDGET` env var)
- **Budget tracking:** Stored in `pipeline_retry_budget.json`, keyed by `{chain_id}:{stage}`
- **Exhaustion behavior:** When a stage's budget reaches 0, the entire chain is marked `failed` and the chain context is archived (see B9)
- **Reset:** Budget resets when a new chain starts; retries from a previous chain do not carry over

## Chain Context Archive on Failure (B9)

When a chain fails (budget exhausted, unrecoverable gate error, or manual cancellation), the full `ChainContext` is archived for post-mortem analysis:

- **Archive location:** `chain_context` field in the failed task's metadata, plus a snapshot written to the governance audit log
- **Contents:** Complete event-sourced history — every stage transition, gate check result, retry attempt, and timing data
- **Retention:** Archives are never automatically deleted; they serve as the primary debugging artifact for failed chains
- **Access:** Query via `GET /api/audit/{pid}/log?limit=N` filtering for `event_type=chain_archived`

## Observer Hold Auto-Release (B16)

When `observer_mode=ON`, auto-created tasks enter `observer_hold` status. The B16 enhancement adds an automatic release timer to prevent tasks from being indefinitely held:

- **Default timeout:** 30 minutes (configurable via `OBSERVER_HOLD_TIMEOUT_SEC`)
- **Behavior on timeout:** Task status transitions from `observer_hold` → `queued`, allowing the executor to claim it
- **Notification:** A Telegram notification is sent to the observer when auto-release triggers, including the task ID and hold duration
- **Override:** Observer can set `hold_indefinite=true` on a specific task to exempt it from auto-release

## Gate Failure Event Bus (B17)

Gate failures now publish structured events to an in-process event bus, enabling decoupled consumers to react to failures:

- **Event schema:** `{event_type: "gate_failure", task_id, stage, gate_name, failure_reason, timestamp}`
- **Consumers:** Audit logger (always active), Telegram notifier (configurable), memory writer (records failure patterns)
- **Purpose:** Eliminates the need for polling-based gate failure detection; consumers subscribe once and receive events in real-time
- **Implementation:** Uses Python `queue.Queue` in `auto_chain.py`; consumers run on the governance service's thread pool

## Preflight Self-Check Integration (G4)

Before dispatching any new stage task, the auto-chain now runs a preflight self-check to verify system health:

- **Checks performed:** DB connectivity, governance service health, git repo integrity, executor reachability, coverage mapping completeness
- **Blocking vs warning:** Only DB connectivity and governance health are blocking; others are warnings logged to audit
- **Failure behavior:** If a blocking check fails, stage dispatch is deferred (not failed) and retried on the next poll cycle
- **Implementation:** Calls `preflight_check()` from `agent/governance/preflight.py`

## Version Gate Downgrade (G5)

The version gate check (`chain_version == git HEAD`) has been downgraded from a hard blocker to a warning:

- **Previous behavior (pre-D3/G5):** Version mismatch caused `_gate_version_check()` to return `False`, silently blocking all auto-chain dispatch
- **Current behavior:** Version mismatch logs a warning and allows dispatch to proceed. The warning is recorded in the chain context and audit log
- **Rationale:** Version mismatches are common during active development (e.g., executor syncs HEAD every 60s) and rarely indicate a real problem. Hard blocking caused more harm (silent chain stalls) than the mismatch itself

## Dedup Guards (G6)

Enhanced dedup guards prevent duplicate task creation beyond the original D4 fix:

- **Stage-level dedup:** Before creating a next-stage task, checks for any existing task in the same chain with the same stage type and `pending`/`claimed`/`observer_hold` status
- **Cross-chain dedup:** Prevents creating a new chain for the same `ref_id` if an active chain already exists
- **Race condition protection:** Uses atomic DB transactions with `SELECT ... FOR UPDATE` semantics (SQLite serialized mode) to prevent TOCTOU races
- **Logging:** Every dedup hit is logged with the duplicate task ID and the existing task ID that prevented creation

## Dirty Workspace Filter Enhancement (G8)

Extends the D5 dirty workspace filter with additional exclusion patterns and smarter handling:

- **Extended exclusions:** Beyond `.claude/` paths, now also filters `*.pyc`, `__pycache__/`, `.pytest_cache/`, and other build artifacts
- **Configurable patterns:** Exclusion patterns are read from `DIRTY_FILTER_PATTERNS` env var (comma-separated globs), defaulting to the built-in list
- **Remaining dirty handling:** After filtering, if dirty files remain, they are logged as warnings with file paths but do not block dispatch (consistent with G5 downgrade philosophy)
- **Audit trail:** Filtered files are recorded in the chain context so post-mortem analysis can distinguish "genuinely dirty" from "filtered noise"

## Connection-Contention Fix: conn.commit()-before-publish (MF-2026-04-24)

Manual fixes MF-001 and MF-002 (applied 2026-04-24) resolved a critical connection-contention bug in `on_task_completed` that caused 60-second stalls due to SQLite's `busy_timeout` setting.

### Problem

When `on_task_completed` fired, the handler would:
1. Open a SQLite connection and perform writes (task status update, chain context update)
2. Publish an event (e.g., `pm.prd.published`) while the connection was still held open
3. Event subscribers would attempt their own DB writes, hitting the held write-lock
4. SQLite's `busy_timeout` (default 60s) would cause subscribers to wait up to 60 seconds before failing

This affected both the PM-stage path (line ~1700 in auto_chain.py) and the dev-stage path (lines ~1760/1924/2433).

### Fix Pattern

The `conn.commit()` call is now issued **before** any event publication, releasing the write-lock so that downstream subscribers can acquire it immediately:

```python
# BEFORE (stalls):
conn.execute("UPDATE tasks SET status=? ...", ...)
event_bus.publish("task.completed", ...)  # subscribers blocked on write-lock
conn.commit()

# AFTER (MF-001/MF-002 fix):
conn.execute("UPDATE tasks SET status=? ...", ...)
conn.commit()  # release write-lock FIRST
event_bus.publish("task.completed", ...)  # subscribers can now write freely
```

This pattern must be followed in all paths where DB writes precede event publication.

## Event Emission Timing: pm.prd.published

The `pm.prd.published` event fires **after** the PM gate check passes, not before. This means downstream consumers (e.g., backlog chain-trigger) only receive the event once the PRD has been validated and the Dev task dispatch is already underway.

**Design consideration (OPT-BACKLOG-PM-PRD-PUBLISH-PRE-GATE):** Moving `pm.prd.published` to fire *before* the gate would allow subscribers to react to the raw PRD output even if the gate rejects it. This is noted as a potential optimization but has not been implemented, as the current post-gate ordering ensures subscribers only see valid PRDs.
