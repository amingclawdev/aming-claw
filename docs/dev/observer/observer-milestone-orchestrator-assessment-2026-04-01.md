# Observer Milestone Orchestrator Assessment

## Scope

Assessment target:

- [observer-milestone-orchestrator-proposal.md](C:/Users/z5866/Documents/amingclaw/aming_claw/docs/dev/observer/observer-milestone-orchestrator-proposal.md)

Assessment perspective:

- based on real Observer operation experience during the recent host-governance, workflow self-repair, and full-chain stabilization work

## Overall Judgment

The proposal is directionally strong and worth pursuing.

Its core architectural idea is correct:

- deterministic daemon for monitoring and safe routine actions
- model-based reasoning only at meaningful decision points
- human-readable milestone context in Markdown
- observer stays outside stage completion and only releases or creates narrowly scoped unblock tasks

However, the proposal is **not yet implementation-ready as written**.

The main issue is not the high-level idea. The main issue is that some parts of the document still reflect an older mental model of the codebase and runtime, while current production experience has already exposed several operational constraints that must be encoded before implementation starts.

So the right conclusion is:

- **approve the direction**
- **do not implement v2 directly**
- **revise into a v3 implementation draft first**

## What Is Strong in the Proposal

### 1. Hybrid observer is the right model

The proposal correctly rejects both extremes:

- a chat session alone is not durable enough
- a pure script observer becomes too brittle

The hybrid split is the correct long-term shape for this project:

- Python daemon for polling, logging, release checks, and safe deterministic actions
- CLI model invocation only for event-level reasoning

This matches what recent maintenance work has shown in practice.

### 2. Observer boundary with auto-chain is well chosen

The proposal correctly preserves this invariant:

- observer does not call `task_complete`
- observer only releases holds or creates narrow unblock tasks

This is important because recent workflow hardening depended on a clear separation between:

- executor / auto-chain progressing stages
- observer handling exceptions and policy-based release

If this boundary is kept, the orchestrator can be added without turning into a second governance engine.

### 3. Markdown as canonical observer context is a good choice

Using Markdown for milestone context, rules, and handoff is appropriate because it is:

- diffable
- reviewable
- easy to repair manually
- already aligned with current `docs/dev` practices

This also fits the actual way observer work has been evolving in the repo.

### 4. Event-triggered reasoning is better than polling-triggered reasoning

The proposal is right to avoid invoking the model on every poll cycle.

The daemon should remain conservative and deterministic, and only call the reasoning layer when a meaningful state transition happens.

That is both cheaper and more stable.

## What Must Change Before Implementation

### P0. Executable code must move out of `docs/dev/`

This is the single most important structural correction.

The proposal currently places executable files in:

- `docs/dev/observer/observer_daemon.py`
- `docs/dev/observer/decision_agent.py`

That conflicts with the current docs architecture, where `docs/dev/` is effectively a non-governed working-doc area.

Executable code that affects system behavior must stay inside governed code paths.

Recommended split:

```text
agent/observer/
  __init__.py
  observer_daemon.py
  decision_agent.py

docs/dev/observer/
  observer-context.md
  observer-rules.md
  observer-milestone-orchestrator-proposal.md
  observer-milestone-orchestrator-assessment-2026-04-01.md
  milestones/

shared-volume/observer/
  state/
  logs/
```

### P0. Governance poll logic must account for current HTTP blocking behavior

The proposal assumes governance can be polled at stable 15-30 second intervals without much trouble.

That is not currently true in the real runtime.

Recent observer operation exposed a recurring issue:

- governance still behaves like a single-threaded `HTTPServer`
- while auto-chain is doing some completion work, the API can temporarily stop responding

This means the daemon must not treat one timeout as a real outage.

Required implementation rule:

- add retry with backoff before escalating host unreachability
- classify transient timeout during chain completion as normal degraded behavior
- strongly consider upgrading governance to `ThreadingHTTPServer` as a root fix

### P1. `observer_mode` must not be reused as daemon kill switch

The current proposal says:

- `observer_mode(enabled=false)` should shut down the daemon

That conflates two different concerns:

1. whether new tasks default into `observer_hold`
2. whether the observer daemon should keep monitoring

Operationally, these are not the same.

There is a valid mode where:

- observer mode is OFF
- tasks run automatically
- daemon still monitors and logs anomalies

Recommended state model:

| observer_mode | daemon_enabled | Behavior |
|---------------|----------------|----------|
| true | true | monitor + release + decide |
| false | true | monitor-only |
| true | false | manual observer |
| false | false | fully autonomous |

### P1. Release allowlist is close, but not fully aligned with current merge reality

The current allowlist says observer should never auto-release:

- `merge`
- `gatekeeper`

That is too strict relative to current runtime behavior.

Recent merge hardening introduced pre-merged and observer-chain-friendly merge handling. In practice, some merge tasks in observer-driven chains can be released safely when the metadata and parent chain shape clearly show low-risk automatic continuation.

So the release policy should stay conservative, but not remain absolute in outdated ways.

Recommended change:

- keep `gatekeeper` human-only by default for now
- allow a narrow merge exception when:
  - it is clearly an observer chain or pre-merged chain
  - there is no destructive ambiguity
  - version and health checks are clean

### P1. Version check must be defined as HTTP-only for Observer decisions

This needs to be a hard rule.

Recent operation exposed that MCP-side version checking can reflect worktree HEAD instead of the canonical project HEAD, creating false alarms.

Observer rules should explicitly say:

- use `GET /api/version-check/{project_id}` as source of truth
- do not use MCP `version_check` tool for release decisions

Without this, the daemon will generate false interventions.

## What Needs Better Alignment with the Current Codebase

### 1. Some file references are outdated

The proposal still references parts of the system as if older file ownership still applied.

Examples:

- legacy references around MCP/governance path ownership
- older assumptions around backend execution paths
- references to `executor.py` patterns that no longer reflect the current executor runtime

Before implementation starts, the proposal should be updated to point to:

- current host governance entrypoints
- current executor worker path
- current service manager ownership
- current AI lifecycle path

### 2. Milestone task decomposition must recognize auto-chain retry behavior

The proposal discusses milestone-aware task management, but it is still missing a strong enough distinction between:

- milestone-created tasks
- auto-chain-created retries
- auto-chain-created next-stage tasks

This matters because recent repairs already had to solve duplicate retry and overlapping follow-up behavior.

If the daemon does not classify task origin correctly, it may:

- release the wrong task
- create duplicate unblock tasks
- interfere with auto-chain recovery

The daemon should explicitly use metadata such as:

- `created_by`
- retry markers
- parent/root task relationships

before deciding whether a task belongs to milestone planning or normal chain progression.

### 3. Multi-session observer concurrency needs a defined policy

The proposal correctly identifies the risk, but this should become a first-class rule before implementation.

Real operating mode already allows:

- multiple desktop sessions
- multiple worktrees
- manual observer actions

So daemon design must include:

- single daemon ownership per project
- visible daemon-active state
- clear restrictions on what manual observer sessions may do while daemon is active

Without that, the orchestrator will create a new coordination problem instead of reducing one.

## Recommended v3 Shape

The proposal should be revised into a v3 implementation draft with this structure:

### Phase 1. Proposal correction

- move executable code target paths into `agent/observer/`
- move runtime state target paths into `shared-volume/observer/`
- update stale file references
- separate `observer_mode` from daemon enable/disable
- add HTTP timeout retry semantics
- add HTTP-only version-check rule

### Phase 2. Policy-first implementation

- create machine-readable `observer-rules.md`
- define release allowlist as explicit structured policy
- define event schema
- define decision schema
- define task-origin classification rules

### Phase 3. Minimal daemon

Implement only:

- polling
- snapshot diff
- event log
- handoff write
- monitor-only mode

Do **not** start with auto-release.

This gives safe observability first.

### Phase 4. Safe auto-release

After monitor-only mode proves stable:

- enable low-risk auto-release for selected `pm/dev/test/qa` holds
- keep merge/gatekeeper conservative until enough evidence exists

### Phase 5. Decision-agent integration

Only after the event/policy layer is stable:

- add CLI-backed reasoning
- require strict JSON output
- validate all actions
- fallback to `hold_for_human`

## Final Recommendation

This proposal should move forward, but only after revision.

My recommendation is:

1. Accept the architecture direction
2. Merge the operational feedback into the proposal itself
3. Promote it to a `v3` implementation draft
4. Implement in a conservative order:
   - monitor-only daemon first
   - then low-risk release logic
   - then decision-agent reasoning

In short:

- **the idea is good**
- **the current draft is not yet executable as-is**
- **a corrected v3 is worth implementing**

