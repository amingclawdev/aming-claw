# Observer Milestone Orchestrator Proposal

## Status

- Draft proposal v2 (revised after architecture review)
- Scope: `docs/dev/observer/` runtime and coordination layer
- Intended reviewers: Claude / human maintainer

## Why This Exists

The current aming-claw workflow already has strong governance primitives:

- task queueing and auto-chain progression
- observer hold / release controls
- health and version checks
- role-based execution through PM, dev, test, QA, and gatekeeper stages

What it does not yet have is a durable, milestone-aware observer that can do all of the following at once:

- keep watching live workflow state without depending on a chat turn staying open
- understand the current project milestone and roadmap context
- decide when routine transitions can be auto-released
- detect when a failure is really a workflow defect rather than a product-code defect
- leave an auditable handoff trail that the next session can resume from quickly

The gap became visible during the recent docs architecture and governance work:

- a normal chat-based observer can stop at turn boundaries
- live governance moved to host service on `http://localhost:40000`, while some repo-side defaults still point to stale locations
- some failures, such as `Reached max turns (20)`, are execution-pipeline failures and should not be misclassified as product regressions
- the active milestone required long-lived context across many queue transitions and retries

This proposal introduces a milestone-aware observer orchestrator so the workflow can continue moving even when no human is actively watching the terminal.

## Goals

- Accept a human-defined milestone from a Desktop session.
- Convert that milestone into workflow tasks and release strategy.
- Continuously monitor live governance state from the host service.
- Trigger model-based reasoning only when a meaningful event occurs.
- Keep stable context in Markdown and optionally in semantic memory.
- Write operational logs and handoff material for recovery and audit.

## Non-Goals

- Replacing the existing governance server
- Replacing auto-chain with a new scheduler
- Making a desktop chat session itself act as a daemon
- Storing transient runtime noise in long-term memory
- Bypassing auto-chain progression — observer only releases holds, never completes tasks

## Core Problem Statement

Today there are two extremes, and neither is ideal on its own.

### Extreme A: Human chat session as observer

Pros:

- rich context
- easy to explain and inspect decisions

Cons:

- stops when the interaction round ends
- cannot be trusted as the only always-on monitor
- awkward for long-lived queue progression and retry handling

### Extreme B: Pure script-based observer

Pros:

- stable and always on
- good at polling, filtering, and logging

Cons:

- poor at interpreting milestone intent
- poor at distinguishing workflow defects from normal failures
- tends to become a brittle rules engine if overextended

The recommended design is therefore a hybrid observer:

- deterministic runtime loop for monitoring and safe routine actions
- model-driven decision agent for milestone-aware reasoning

## Proposed Architecture

The observer system should be split into three layers.

### 1. Desktop Milestone Session

This is the human-facing Codex Desktop or Claude Desktop session.

Responsibilities:

- receive a milestone from the user
- define or update milestone scope
- split the milestone into workflow tasks
- seed canonical context files under `docs/dev/observer/`
- review exceptional decisions when a real approval point is reached

This layer is not the daemon. It is the control plane for milestone definition and oversight.

### 2. Observer Daemon

This is a Python long-running service under `docs/dev/observer/`, implemented with asyncio to match the existing codebase style (service_manager.py, executor.py).

Responsibilities:

- poll governance host APIs every 15 to 30 seconds
- read live task queue, health, and version status
- detect state changes relevant to the active milestone
- perform only low-risk, pre-approved actions (see Release Allowlist)
- write state snapshots and operational logs
- trigger the decision agent when a meaningful event occurs

This layer must remain deterministic and conservative.

#### Daemon Lifecycle

The daemon registers with the existing ServiceManager infrastructure:

- **PID lock**: single-instance enforcement via `state/observer.pid`, same pattern as executor
- **Crash recovery**: ServiceManager monitor thread checks every 10s, restarts on crash (circuit breaker: 5 failures in 300s)
- **Startup recovery**: on start, daemon reads `state/current-state.json` to resume from last known position; stale in-flight decisions older than 120s are discarded and logged as `hold_for_human`
- **Telegram alert**: if daemon fails to start after circuit breaker trips, send alert via `telegram_send` to the configured chat

#### Kill Switch

The daemon respects `observer_mode(enabled=false)` as an immediate shutdown signal. On the next poll cycle, if observer mode is disabled, the daemon:

- logs the shutdown reason
- writes a final handoff summary
- exits cleanly

Manual restart requires re-enabling observer mode and restarting the service.

### 3. Decision Agent

This is an on-demand `codex-cli` or `claude` CLI invocation, not a permanent loop.

Responsibilities:

- load milestone context and observer context
- inspect the latest runtime event and live project state
- decide whether to:
  - do nothing
  - release a routine hold
  - create an unblock task
  - classify a failure
  - request human review
  - update handoff or milestone notes

This layer provides reasoning, but only when necessary.

#### Concurrency Control

Only one decision agent invocation is allowed at a time:

- the daemon holds an in-memory mutex before invoking the agent
- if an event arrives while the agent is running, it is queued (max queue depth: 10; overflow events are logged and dropped)
- agent invocation has a hard timeout of 120 seconds
- on timeout, the daemon logs the event and applies `hold_for_human` as the default action
- the daemon must wait for the agent response (or timeout) before processing the next queued event

## Why Not Make Raw Codex CLI the Entire Observer

The repository already supports Codex CLI execution through the OpenAI provider path in [ai_lifecycle.py](/Users/z5866/Documents/amingclaw/aming_claw/agent/ai_lifecycle.py#L146) and [ai_lifecycle.py](/Users/z5866/Documents/amingclaw/aming_claw/agent/ai_lifecycle.py#L241).

However, the current backend design explicitly warns that analysis-oriented stages should not rely on `codex exec` alone. [backends.py](/Users/z5866/Documents/amingclaw/aming_claw/agent/backends.py#L747) notes that `verify/test/qa/plan` style stages should prefer Claude CLI because `Codex CLI 'exec' mode` is optimized for execution and can return ACK-only responses for analysis prompts. The repo also contains dedicated noop and acknowledgement-only detection in [backends.py](/Users/z5866/Documents/amingclaw/aming_claw/agent/backends.py#L544).

That matters here because observer work is primarily:

- interpreting state
- classifying blockers
- applying roadmap context
- deciding whether intervention is warranted

Those are analysis-heavy tasks. A pure raw `codex exec` loop would be possible, but it would be less stable and would likely reproduce the same noop or turn-limit problems that the governance system is already trying to control.

The safer design is:

- use a deterministic daemon for monitoring
- use a model only as a decision layer
- allow either Codex CLI or Claude CLI as that decision layer, depending on model routing and task type

## Boundary with Auto-Chain

The observer daemon and auto-chain have distinct, non-overlapping responsibilities:

| Concern | Auto-chain | Observer |
|---------|-----------|----------|
| Stage progression (PM→Dev→Test→QA→Merge) | Yes — `_dispatch_next_stage` | No — never calls `task_complete` |
| Release `observer_hold` tasks | No | Yes — only action type `release_task` |
| Task completion | Yes — executor marks done | No — observer only releases holds |
| Task creation | Yes — next-stage tasks | Yes — only `create_unblock_task` for workflow defects |

Key invariant: **observer never calls `task_complete`**. It can only `task_release` (which puts the task back into `queued` for the executor to claim) or `create_unblock_task` (which creates a new governance task through the standard chain).

This prevents the race condition where both observer and auto-chain try to advance the pipeline simultaneously.

## Release Allowlist

The observer may auto-release a task from `observer_hold` only when ALL of the following conditions are true:

1. Task type is one of: `pm`, `dev`, `test`, `qa` (never `merge` or `gatekeeper`)
2. Task belongs to the active milestone (matched by milestone ID in task metadata)
3. Health check returns `ok` from host governance (`GET /api/health`)
4. Version gate returns `ok` (`GET /api/version-check/aming-claw`)
5. `target_files` (if present) do not touch governance core paths:
   - `agent/governance/*.py`
   - `agent/ai_lifecycle.py`
   - `agent/backends.py`
   - `docker-compose*.yml`
   - `.claude/settings*.json`
6. No other task of the same type is currently `claimed` (prevents parallel execution conflicts)

If any condition fails, the observer must `hold_for_human` and log the reason.

These rules must be codified in `observer-rules.md` as a machine-readable allowlist, not left to decision agent interpretation.

## Context and Memory Model

The observer needs memory, but not all information deserves long-term storage.

### Canonical Markdown Context

Canonical observer context should live in `docs/dev/observer/`.

Recommended files:

- `observer-context.md`: stable cross-milestone facts
- `observer-rules.md`: approved operating rules (including release allowlist)
- `milestones/<milestone-id>.md`: active milestone scope, task decomposition, success criteria
- `handoff-latest.md`: current observer handoff summary

Markdown should be the canonical human-readable source because it is reviewable, diffable, and easy to repair.

### Runtime State

Runtime state should remain local and disposable.

Recommended files:

- `logs/observer.log`
- `state/current-state.json`
- `state/last-decision.json`
- `state/active-milestone.json`
- `state/observer.pid`

## Proposed Directory Layout

Within `docs/dev/observer/`:

```text
observer/
  README.md
  observer-context.md
  observer-rules.md
  observer-milestone-orchestrator-proposal.md
  observer_daemon.py
  decision_agent.py
  milestones/
    docs-architecture-phase1.md
  state/
    active-milestone.json
    current-state.json
    last-decision.json
    observer.pid
  logs/
    observer.log
    event-log.jsonl
```

## Event Model

The daemon should not invoke a model on every poll. It should invoke a model only when an event boundary is crossed.

Recommended event types:

- `root_task_changed`
- `stage_changed`
- `task_entered_observer_hold`
- `task_failed_repeatedly`
- `workflow_defect_suspected`
- `version_gate_changed`
- `merge_started`
- `deploy_started`
- `smoke_finished`
- `milestone_completed`

Each event should contain:

- timestamp
- active milestone ID
- relevant task IDs
- current stage
- previous status
- new status
- health summary
- version summary
- recent related logs

## Decision Contract

The decision agent should return a strictly limited action set so the daemon can remain safe.

Recommended actions:

- `no_action`
- `release_task`
- `create_unblock_task`
- `hold_for_human`
- `classify_failure`
- `update_handoff`
- `mark_milestone_complete`

Recommended response shape:

```json
{
  "action": "release_task",
  "reason": "Routine QA hold for active milestone; no elevated risk detected.",
  "task_id": "task-123",
  "risk": "low",
  "notes": [
    "Health is OK on host governance.",
    "No merge or destructive action involved."
  ]
}
```

The daemon must validate every response:

- `action` must be one of the 7 allowed values; unknown actions are treated as `hold_for_human`
- `release_task` must pass all Release Allowlist checks; if any check fails, the daemon overrides to `hold_for_human`
- `task_id` must exist and be in `observer_hold` status; invalid task IDs are rejected
- malformed JSON is treated as `hold_for_human`

## Runtime Flow

### Step 1. Milestone intake

The Desktop session receives a milestone such as:

- docs architecture update and node binding
- governance host alignment
- workflow self-repair hardening

The session then:

- creates or updates a milestone Markdown file
- defines success criteria
- splits the milestone into workflow tasks
- enables observer mode if appropriate
- records the active milestone in runtime state

### Step 2. Daemon monitoring

The observer daemon polls:

- `GET /api/health`
- `GET /api/task/{project_id}/list`
- `GET /api/version-check/{project_id}`
- optionally `GET /api/project/{project_id}/observer-mode`

The daemon compares the new snapshot with the prior snapshot.

If nothing meaningful changed:

- write nothing or a very small heartbeat
- continue polling

If something meaningful changed:

- write an event record
- check whether the event can be handled by the Release Allowlist directly (no model needed)
- if the event requires judgment, invoke the decision agent (respecting concurrency control)

### Step 3. Decision agent invocation

When invoked, the agent receives:

- `observer-context.md`
- `observer-rules.md` (including release allowlist)
- current milestone Markdown
- latest event JSON
- most recent relevant log lines (last 50 lines max)
- current health and version-check result
- optional repo facts such as changed files or specific docs/code references

The agent returns one structured decision.

### Step 4. Action execution

The daemon validates the returned action against the Release Allowlist and Decision Contract.

Examples:

- `release_task` maps to `POST /api/task/{project_id}/release` (only if all allowlist checks pass)
- `create_unblock_task` maps to governance task creation (type must be `pm` or `dev`, never `merge`)
- `update_handoff` writes Markdown or JSON state
- `hold_for_human` stops auto-advancing that branch and records the reason

### Step 5. Logging and handoff

Every material action should be recorded in:

- append-only event log
- human-readable observer log
- current handoff summary

This makes the system resumable even if the Desktop session closes.

## Intervention Policy

The observer should act autonomously for normal state transitions and ask for human review only when needed.

### Safe automatic actions

- routine release of milestone-related `pm`, `dev`, `test`, or `qa` holds (per Release Allowlist)
- creating focused unblock tasks for repeated workflow defects
- updating logs, handoff, and milestone state

### Human-required actions

- a `gatekeeper` or merge decision with meaningful risk
- destructive or irreversible operations
- ambiguous cross-milestone conflicts
- evidence that governance state itself is inconsistent or corrupted
- external actions such as service restart, secret rotation, or manual deployment approval
- any `release_task` where allowlist checks fail
- target_files touching governance core paths

## Host Governance Alignment

The live observer should trust the host governance service, not stale bridge defaults.

Current known rule:

- host governance is live on `http://localhost:40000`

Known drift to track:

- `agent/governance/mcp_server.py` still defaults to `http://localhost:40006`

The daemon should read live status from the host service first and treat repo-side defaults as configuration that may need remediation, not as the source of truth.

## Milestone Decomposition Strategy

When a user provides a milestone, the Desktop session should split it into tasks with minimal overlap and clear ownership.

For example, a docs architecture milestone may decompose into:

1. mechanical doc migration and archive reshape
2. canonical topic doc creation
3. CODE_DOC_MAP and node binding alignment
4. governance host and MCP configuration cleanup
5. release validation and handoff update

The observer should follow the chain, but the milestone file should preserve:

- task rationale
- dependency order
- stop conditions
- known blockers

## Degradation and Recovery

### Degradation modes

| Scenario | Behavior |
|----------|----------|
| Decision agent timeout (>120s) | Apply `hold_for_human`, log, continue polling |
| Decision agent returns invalid JSON | Apply `hold_for_human`, log the raw output |
| Daemon crashes | ServiceManager restarts within 10s; daemon resumes from `state/current-state.json` |
| Circuit breaker trips (5 crashes in 300s) | Send Telegram alert, stop restart attempts |
| Governance host unreachable | Log warning, skip release actions, retry on next poll |
| observer_mode disabled externally | Clean shutdown with handoff write |

### Manual fallback

At any time, the operator can:

1. Disable observer mode: `observer_mode(project_id, enabled=false)`
2. The daemon exits cleanly on the next poll cycle
3. Resume manual observation via Desktop session
4. All state is in `state/` and `logs/` — no context is lost

### Recovery from bad state

If the observer has made incorrect releases or created wrong tasks:

1. Enable observer hold on all in-flight tasks: `observer_mode(project_id, enabled=true)` (all new tasks start as `observer_hold`)
2. Review `logs/event-log.jsonl` for the decision history
3. Cancel or revert incorrect tasks via governance API
4. Fix `observer-rules.md` if the allowlist was too permissive
5. Re-enable the daemon

## Logging Strategy

Use two log formats:

### Human-readable log

Purpose:

- quick scan by a maintainer
- handoff and audit trail

Example entry:

```text
[2026-04-01 14:20:00] event=task_entered_observer_hold task=task-123 stage=qa action=invoke_decision_agent
[2026-04-01 14:20:04] decision=release_task task=task-123 risk=low reason="Routine QA hold for active docs milestone"
```

### Structured event log

Purpose:

- replay and analytics
- machine-readable debugging

Format:

- JSON Lines

## Recommended Implementation Plan

### Phase 1. Formalize context and event contracts

- add `observer-rules.md` with machine-readable release allowlist
- add milestone template
- add JSON schemas for event and decision payloads
- validate rules against recent observer session logs to confirm they are reasonable

### Phase 2. Build Python daemon

- implement `observer_daemon.py` with asyncio (match executor.py patterns)
- PID lock and crash recovery via ServiceManager registration
- polling loop with snapshot diffing
- event detection and logging
- Telegram alert on circuit breaker trip
- kill switch via observer_mode check

### Phase 3. Add decision-agent launcher

- implement `decision_agent.py`
- concurrency mutex + 120s timeout
- event queue (max depth 10)
- support either Codex CLI or Claude CLI backend
- feed the backend a compact context bundle (observer-rules.md + milestone + event + health)
- require structured JSON output, validate against decision contract
- fallback to `hold_for_human` on any validation failure

### Phase 4. Add milestone intake flow

- create milestone Markdown from Desktop session input
- generate initial workflow tasks
- record active milestone metadata for the daemon

### Phase 5. (Deferred) Evaluate semantic memory integration

- evaluate only after Phases 1-4 are stable and have run for at least one full milestone cycle
- if evaluated, write only durable project facts — no transient runtime state
- current FTS5 + mem0 infrastructure is sufficient; do not build observer-specific memory unless proven necessary

## Risks and Mitigations

### Risk: the daemon becomes a second governance engine

Mitigation:

- keep daemon actions narrow and explicit (Release Allowlist)
- daemon can only `release` or `create_unblock_task`, never `complete`
- push reasoning to the decision agent
- preserve governance as the system of record

### Risk: model output is too open-ended

Mitigation:

- use a constrained decision contract (7 actions only)
- validate actions before execution
- fall back to `hold_for_human` on invalid output
- release_task must additionally pass Release Allowlist checks

### Risk: context grows noisy and degrades decisions

Mitigation:

- separate stable Markdown context from runtime state
- only pass compact event windows to the decision agent (last 50 log lines max)
- keep runtime state local and disposable

### Risk: stale configuration causes wrong endpoint usage

Mitigation:

- always prefer live host health and version APIs
- log drift between live host and repo-side defaults
- create remediation tasks when drift is detected

### Risk: race condition between observer and auto-chain

Mitigation:

- observer never calls `task_complete` — only `task_release`
- observer only acts on `observer_hold` tasks
- auto-chain handles all stage progression
- clear separation documented in Boundary with Auto-Chain section

### Risk: daemon itself becomes unavailable

Mitigation:

- ServiceManager monitors and restarts (10s check interval)
- PID lock prevents duplicate instances
- crash recovery reads from `state/current-state.json`
- circuit breaker (5/300s) prevents restart loops
- Telegram alert on circuit breaker trip
- manual fallback always available (disable observer mode, resume Desktop session)

## Success Criteria

The proposal is successful when the system can:

- accept a milestone from a Desktop session
- decompose it into workflow tasks
- keep monitoring without chat-turn dependence
- auto-handle routine transitions per Release Allowlist
- invoke a model only at meaningful decision points
- preserve enough context for a new session to resume quickly
- distinguish workflow defects from product regressions more reliably
- degrade gracefully when any component fails (daemon, agent, governance host)
- be shut down and resumed without data loss

## Recommendation

Implement the observer as a milestone-aware hybrid orchestrator.

Do not rely on a Desktop session as the daemon.
Do not rely on raw `codex exec` as the full observer loop.

Instead:

- keep the live monitor deterministic, in Python, integrated with ServiceManager
- keep milestone context in Markdown
- use `codex-cli` or `claude` CLI as an on-demand decision layer with strict concurrency control
- enforce release decisions through a machine-readable allowlist, not model judgment alone
- write every important event and decision to logs and handoff artifacts
- design for graceful degradation at every layer

This design matches the current governance architecture, reduces fragility, and provides a clean path for future Claude review and iterative hardening.

---

## Observer Review: Operational Experience Feedback (2026-04-01)

> Source: Observer session that completed D4-D7 fixes, Batch 3 optimization, and docs architecture proposal.
> These findings are based on real production experience maintaining the governance chain as Observer.

### Issue 1 (P0): HTTPServer Single-Threaded Blocking

**Assumption in proposal**: Daemon polls governance API every 15-30s reliably.

**Reality**: Governance server uses `http.server.HTTPServer` (single-threaded). When `auto_chain._do_chain()` runs in a background thread, it calls `subprocess.run(["git", "rev-parse", "HEAD"])` which takes ~11 seconds. During this window, the HTTPServer cannot respond to ANY request. Observer session experienced repeated HTTP timeouts during auto_chain dispatch.

**Impact**: Daemon poll will timeout whenever auto_chain is processing a task completion. This happens after every successful task, so it is frequent, not rare.

**Required changes**:

1. Daemon must implement poll retry with backoff — do not classify a single timeout as "governance host unreachable"
2. Add to Degradation table: `Governance API timeout during auto_chain dispatch → retry in 5s, max 3 retries, do not escalate`
3. Consider upgrading governance server to `ThreadingHTTPServer` (one-line change, root fix)

### Issue 2 (P0): Executable Code Must Not Live in docs/dev/

**Proposal**: Places `observer_daemon.py` and `decision_agent.py` in `docs/dev/observer/`.

**Conflict**: The docs-architecture-proposal v3 (finalized in this session) defines `docs/dev/` as "non-governed working documents — explicitly excluded from gate association and node validation." Executable code that controls system behavior must be governed (tested, reviewed, merged via chain).

**Required change**:

```
agent/observer/                    # Governed code (walks PM→Dev→Test chain)
  observer_daemon.py
  decision_agent.py
  __init__.py

docs/dev/observer/                 # Non-governed context & config
  observer-context.md
  observer-rules.md
  observer-milestone-orchestrator-proposal.md
  milestones/

shared-volume/observer/            # Runtime state (not in git)
  state/
    active-milestone.json
    current-state.json
    last-decision.json
    observer.pid
  logs/
    observer.log
    event-log.jsonl
```

### Issue 3 (P1): observer_mode as Kill Switch Conflates Two Concerns

**Proposal**: `observer_mode(enabled=false)` triggers daemon shutdown.

**Reality**: `observer_mode` is a project-level flag controlling "do new tasks start as observer_hold." Disabling it is a legitimate operational choice meaning "let chains run fully automatic" — the user may still want daemon monitoring.

**Current behavior**: `task_registry._is_observer_mode(conn, project_id)` checks `project_version.observer_mode`. When OFF, `create_task` sets initial status to `queued` instead of `observer_hold`. This is independent of whether monitoring should continue.

**Required change**:

- Daemon kill switch: separate mechanism — `state/observer.enabled` file or dedicated API endpoint
- When `observer_mode=false` and daemon is running: daemon switches to **monitor-only mode** (log events, write handoff, but skip all release actions since there are no observer_hold tasks to release)
- When `observer_mode=true` and daemon is running: normal operation (monitor + release per allowlist)
- Kill switch states:

| observer_mode | daemon.enabled | Daemon behavior |
|---------------|---------------|-----------------|
| true | true | Full operation: monitor + release + decide |
| false | true | Monitor-only: log events, detect anomalies, no release actions |
| true | false | Daemon stopped; manual Observer via Desktop session |
| false | false | Fully autonomous; no observer intervention |

### Issue 4 (P2): Outdated Code References

The proposal references files that no longer exist or have been restructured:

| Proposal reference | Actual current state |
|-------------------|---------------------|
| `agent/governance/mcp_server.py` defaults to `http://localhost:40006` | MCP tools are in `agent/mcp/tools.py`, using `self._api` HTTP calls. Default port fixed to 40000 in commit b902295 |
| `agent/backends.py` noop detection (lines 544, 747) | `backends.py` is legacy. Current AI execution: `agent/ai_lifecycle.py` → Claude CLI subprocess |
| `executor.py` patterns | Current executor is `agent/executor_worker.py` (completely different codebase) |

**Required change**: Update all file references in the proposal to current paths.

### Issue 5 (P1): Release Allowlist Should Allow Pre-Merged Merges

**Proposal**: "Task type `merge` or `gatekeeper` → never auto-release."

**Reality**: D6 fix (commit 20baea3) added pre-merge detection to the merge handler. When Observer completes dev tasks manually (without worktree isolation), the merge handler checks if HEAD is ahead of chain_version and auto-succeeds. In this session, ALL merge tasks for Observer chains completed automatically after release.

**Required change**:

Update Release Allowlist rule 1:

```
OLD: Task type is one of: pm, dev, test, qa (never merge or gatekeeper)
NEW: Task type is one of: pm, dev, test, qa, OR
     Task type is merge AND metadata contains _already_merged=true or
     parent chain has no _branch/_worktree (Observer chain)
```

Gatekeeper can also be considered for auto-release when the preceding QA passed with full criteria_results (E2E1 improvement from this session makes this verifiable).

### Issue 6 (P1): version-check Must Use HTTP API, Never MCP Tool

**Real incident**: During this session, MCP `version_check` tool returned `ok=false` with `head=486fed3` (worktree HEAD) vs `chain_version=e9506c0` (main). This was a **false alarm** — the MCP tool runs in the zen-mendeleev worktree and overrides the `head` field with `git rev-parse HEAD` from the worktree directory (`agent/mcp/tools.py` lines 334-341).

The HTTP API (`GET /api/version-check/{project_id}`) reads `git_head` from DB (synced by executor every 60s) and returns the correct main HEAD.

**Required change**:

Add hard rule to observer-rules.md:

```
RULE: Always use HTTP API (GET /api/version-check/{project_id}) for version status.
NEVER use MCP version_check tool — it returns worktree HEAD in non-main worktrees,
causing false ok=false alarms that trigger unnecessary intervention.
```

### Issue 7 (P2): Milestone Tasks vs Auto-Chain Retry Dedup

**Real incident**: D4 (commit 7d96c74) fixed duplicate retry task creation by adding dedup guards in auto_chain.py. When a gate blocks a task, auto_chain creates a retry. If dispatch_chain is called twice (async), two identical retries were created.

**Implication for milestones**: When a milestone is decomposed into tasks, auto_chain may independently create retry tasks that overlap with milestone-planned tasks. The daemon must distinguish:

- **Milestone tasks**: created by Desktop session or daemon via `create_unblock_task`
- **Auto-chain retries**: created by `auto-chain-retry` or `auto-chain-stage-retry`
- **Auto-chain next-stage**: created by `auto-chain`

**Required change**:

Add to daemon event processing:

```
When task_entered_observer_hold:
  1. Check created_by field:
     - "auto-chain" / "auto-chain-retry" / "auto-chain-stage-retry" → auto-chain managed
     - "observer-daemon" / "milestone-intake" → milestone managed
     - other → unknown, hold_for_human
  2. Auto-chain tasks: apply Release Allowlist normally
  3. Milestone tasks: match against active milestone plan before releasing
  4. Never create a milestone task if auto_chain will create the same type for the same parent
```

### Issue 8 (P2): Multi-Observer Session Concurrency

**Scenario**: User opens two Claude Code sessions (e.g., worktree + main). Both may act as Observer, creating race conditions: duplicate releases, conflicting task creates, version-update races.

**Current state**: Executor has PID lock preventing duplicate instances. Daemon would also have PID lock. But manual Observer operations from Desktop sessions have no concurrency control.

**Required change**:

Add coordination protocol:

```
1. Daemon PID lock: only one daemon instance per project (already designed) ✓
2. When daemon is active, publish a "daemon_active" flag via governance API
3. Desktop Observer sessions check daemon_active before taking actions:
   - daemon_active=true → Desktop session is read-only (query/inspect)
     except for: hold_for_human approvals and milestone updates
   - daemon_active=false → Desktop session has full Observer capability
4. Daemon writes its PID and start_time to state/observer.pid
   Desktop sessions read this to detect stale daemon (PID not running)
```

### Summary: Required Changes Before Implementation

| Priority | Issue | Change Type |
|----------|-------|-------------|
| **P0** | HTTPServer blocking | Add retry logic to daemon + consider ThreadingHTTPServer |
| **P0** | Code in docs/dev/ | Move to `agent/observer/` |
| **P1** | observer_mode ≠ kill switch | Decouple, add monitor-only mode |
| **P1** | Pre-merged merge auto-release | Expand Release Allowlist |
| **P1** | version-check worktree trap | Hard rule: HTTP API only |
| **P2** | Outdated file references | Update paths |
| **P2** | Milestone vs retry dedup | Add created_by classification |
| **P2** | Multi-session concurrency | Add daemon_active coordination |
