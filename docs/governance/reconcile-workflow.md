# Reconcile Workflow Governance Specification

> **Meta-governed document** — Modifications to this spec require a governance chain (modify via chain).
> Canonical governance spec for the reconcile workflow.
> Last updated: 2026-05-01 | PR2 atomic swap + disappearance review additions (§6 disappearance review, §8 atomic swap, §10 rollback)

---

## §1 Purpose and Scope

The reconcile workflow detects and repairs governance graph drift — situations where the
implemented codebase state diverges from what the governance graph records. This includes:

- Nodes stuck in incorrect states (e.g., pending when code already merged)
- Missing graph nodes for implemented features
- Orphaned nodes referencing deleted code
- Gate exemptions that bypassed normal progression

This spec formalizes the reconcile workflow as a first-class governance operation with
defined phases, contracts, failure modes, and rollback paths.

### 1.1 Relationship to Other Governance Docs

- **[auto-chain.md](auto-chain.md)** — Reconcile leverages the auto-chain pipeline for execution;
  §11 documents gate exemption patterns within auto_chain.py
- **[version-control.md](version-control.md)** — Reconcile operations must respect the version gate;
  §12 versioning aligns with chain_version lifecycle
- **[manual-fix-sop.md](manual-fix-sop.md)** — Reconcile replaces ad-hoc manual fixes with a
  structured, auditable process; rollback paths reference manual-fix-sop procedures

---

## §2 Reconcile Task Type Definition

A reconcile task is a specialized governance task type with restricted creation and execution rules.

### 2.1 Creator Allowlist

Only the following actors may create reconcile-type tasks:

- `observer` — Human operator via observer interface
- `coordinator` — Coordinator agent acting on detected drift
- `auto-chain-reconcile` — Automated reconcile subsystem

Explicitly excluded: `MF-*` prefixed actors (manual-fix sessions) — these require explicit
observer action to escalate to reconcile scope.

### 2.2 Audit Logging

Every reconcile task creation and state transition is logged to the audit table with:

- `action_type: "reconcile"`
- `actor`: creator from allowlist above
- `metadata`: includes `run_id`, `drift_type`, `affected_nodes[]`
- Timestamp and chain_version at creation time

Audit records are immutable and queryable via `/api/audit/{pid}/log`.

### 2.3 Rate Limit

Reconcile operations enforce a 3-tier rate limit to prevent runaway repair loops:

- **Tier 1 (run-level)**: Maximum 1 active `reconcile_run_id` per project at any time. A new run cannot start until the previous run reaches terminal state (`completed` or `rolled_back`). Counter: `audit_index.run_level_active`. Alert threshold: attempt to start second run logs `F2.1a`.

- **Tier 2 (task-level)**: Maximum N=3 concurrent `reconcile_task` instances within a single run (configurable via `reconcile_config.task_concurrency`, default N=3). Counter: `audit_index.task_level_active`. Alert threshold: N-1 active tasks logs `F2.1b` warning.

- **Tier 3 (action-level)**: Maximum M=10 discrete actions per task (configurable via `reconcile_config.max_actions_per_task`, default M=10). Prevents a single task from runaway changes. Counter: `audit_index.action_level_count`. Alert threshold: M-2 actions logs `F2.1c` warning.

Each tier has a separate counter in `audit_index` and a separate alert threshold. Rate limit violations at any tier are logged as `F2.1` failure events and require observer intervention.

---

## §3 Phase 1: Drift Detection

### Trigger

- Scheduled cron scan (every 30 minutes by default)
- Manual observer invocation via `/api/reconcile/scan`
- Post-merge hook detecting graph inconsistency

### Input contract

- Access to governance DB (read-only scan)
- Current `chain_version` from version gate
- Full node graph export (`/api/wf/{pid}/export`)

### Output contract

- `suspected_drift_findings`: list of `{node_id, drift_type, evidence, severity, confidence}` where `confidence` is a float 0.0–1.0 representing detection-phase certainty
- No state mutations during detection phase
- Report persisted to `reconcile_runs` table with unique `run_id`

### Acceptance criteria

- AC3.1: All nodes in graph are scanned
- AC3.2: Drift types classified: `stuck`, `orphan`, `missing`, `state_mismatch`
- AC3.3: Zero false positives on nodes modified within last 10 minutes (grace period)

### Failure modes

- F3.1: DB connection timeout during scan — retry with exponential backoff (max 3 attempts)
- F3.2: Graph export returns partial data — abort scan, log warning, retry next cycle
- F3.3: Rate limit exceeded — defer to next scheduled cycle

### Rollback path

Detection is read-only; no rollback needed. Failed scans are logged and retried.

---

## §4 Phase 2: Drift Classification

### Input contract

- Drift report from Phase 1 (§3)
- Node metadata including last_updated timestamps
- Historical reconcile runs for deduplication

### Output contract

- Classified drift items with proposed remediation action, each including:
  - `confirmed: bool` — set to `true` only when classification raises confidence above 0.7 AND assigns a severity tier (low/medium/high)
  - `severity`: one of `low`, `medium`, `high`
  - `confidence`: float 0.0–1.0 (carried from §3, potentially adjusted by classification)
- Unconfirmed findings (confidence < 0.7) receive `observer_review_required` status and do NOT auto-flow to §5
- Priority ordering (P0 > P1 > P2)
- Deduplication against in-progress reconcile tasks

### Acceptance criteria

- AC4.1: Each drift item has exactly one classification
- AC4.2: P0 items (stuck gates blocking other chains) prioritized first
- AC4.3: Duplicate drift (same node, same type, existing active reconcile) filtered out

### Failure modes

- F4.1: Classification ambiguity (node matches multiple drift types) — default to highest severity
- F4.2: Historical data unavailable — classify without dedup, flag for manual review
- F4.3: Priority conflict (multiple P0 items) — FIFO ordering by detection timestamp

### Rollback path

Classification is stateless computation on the drift report. Re-run from Phase 1 output
if classification logic is updated. No DB mutations to reverse.

---

## §5 Phase 3: Remediation Planning

### Input contract

- Classified drift items from Phase 2 (§4)
- Current project state (active chains, queue depth)
- Reconcile rate limit remaining capacity

### Output contract

- Remediation plan: ordered list of `{action, target_node, params, estimated_impact}`
- Plan persisted to `reconcile_plans` with `plan_id` linked to `run_id`
- Each action tagged with rollback strategy

### Acceptance criteria

- AC5.1: Every remediation action has a defined rollback strategy
- AC5.2: Plan respects rate limits (no more actions than remaining capacity)
- AC5.3: Plan avoids mutating nodes currently in active chains

### Failure modes

- F5.1: No valid remediation exists for a drift item — escalate to observer with evidence
- F5.2: Rate limit would be exceeded by plan — defer low-priority items to next cycle
- F5.3: Active chain conflict detected — mark items as `deferred_conflict` and skip

### Rollback path

Plans are proposals; delete the plan record from `reconcile_plans`. No node state changed yet.

### 5.5 Plan Approval

Before a remediation plan flows to §6 Task Creation, it must pass an approval gate.

**Approver allowlist**: `observer-*`, `coordinator`, or `auto-approval-bot` (under conditions below).

**Auto-approval conditions** (all must hold for `auto-approval-bot` to approve):
- Single-node remediation (exactly one target node)
- Severity = `low`
- Confidence ≥ 0.85
- No gate bypass involved in the remediation plan

**Mandatory observer approval** (any triggers human review):
- Multi-node remediation (>1 target node)
- Severity = `medium` or `high`
- Gate bypass involved in the plan
- Dead code deletion > 20 LOC
- Edge rewrite touching foundation layers L0–L1

**Rejection path**: Plan marked `rejected` with reason; `reconcile_run` aborted; remediation logged for re-planning in next cycle.

**Approval output**: Signed approval token containing:
- `approver_id`: identity of the approving actor
- `approved_at`: ISO-8601 timestamp
- `plan_hash`: SHA-256 of the approved plan
- `expires_at`: token expiry (default 1 hour from `approved_at`)

---

### 6.0 Disappearance Review (Pre-Swap)

Before §8 atomic swap can run, every node that disappears between the
existing `graph.json` and the candidate `graph.v2.json` MUST receive an
explicit observer decision. The review is implemented in
[`agent/governance/symbol_disappearance_review.py`](../../agent/governance/symbol_disappearance_review.py)
and is invoked via:

```
python scripts/phase-z-v2.py review \
    --graph-path agent/governance/graph.json \
    --candidate-path agent/governance/graph.v2.json \
    --decisions <decisions.json>
```

The module exposes two canonical constant tables:

* `REMOVAL_REASONS` — exactly 5 strings, in order:
  `files_relocated`, `files_deleted`, `merged_into_other_node`,
  `low_confidence_inference`, `no_matching_call_topology`. Each
  disappearing node is classified into exactly one reason by
  `classify_removal()`.
* `OBSERVER_DECISIONS` — exactly 4 strings, in order:
  `approve_removal`, `map_to_new_node`, `preserve_as_supplement`,
  `block_swap`. The observer must record one decision per disappearing
  node; `block_swap` halts the swap regardless of other decisions.

`require_observer_decision()` returns `{ok, missing, blocked}` and is the
gate-equivalent for §8: `ok=False` means the swap MUST NOT be invoked.
`detect_governance_markers()` flags B36 dangling-L7 nodes, legacy
waivers, and manual carve-outs so the observer can decide based on full
governance context.

### 6.1 Acceptance Criteria

- AC6.0.1: Every node missing from the candidate graph receives a
  classification from `REMOVAL_REASONS`.
- AC6.0.2: Every removed node has a recorded decision from
  `OBSERVER_DECISIONS`; absent or unknown values are reported in
  `missing`.
- AC6.0.3: Any `block_swap` decision aborts the swap and is logged in
  the audit trail.
- AC6.0.4: The review report is JSON-serialisable and is the input to
  the §8 atomic swap.

---

## §6 Phase 4: Task Creation

### Input contract

- Approved remediation plan from Phase 3 (§5)
- Available task slots (queue not at capacity)
- Valid creator identity from allowlist (§2.1)

### Output contract

- One or more reconcile tasks created in governance DB
- Each task has `bug_id` following naming convention: `OPT-BACKLOG-RECONCILE-{run_id[:8]}-{type}-{slug}`
- Tasks linked to plan_id and run_id for traceability

### Acceptance criteria

- AC6.1: Task `bug_id` matches pattern `OPT-BACKLOG-RECONCILE-{8chars}-{type}-{slug}`
- AC6.2: Task metadata includes `reconcile_run_id`, `drift_type`, `plan_id`
- AC6.3: Creator is validated against allowlist before task insertion

### Failure modes

- F6.1: Queue at capacity — defer task creation, log `queue_full` event
- F6.2: Creator not in allowlist — reject with `unauthorized_creator` error
- F6.3: Duplicate bug_id collision — append numeric suffix `-2`, `-3`, etc.

### Rollback path

Cancel created tasks via `/api/task/cancel`. Tasks in `pending` state can be safely removed.
Tasks already `claimed` require observer intervention to cancel.

---

## §7 Phase 5: Execution

### Input contract

- Reconcile task in `claimed` state
- Executor worker available with reconcile capability
- Target node(s) accessible and not locked by another operation
- Optimistic locking fields (MUST validate before any mutation):
  - `expected_before_state`: SHA-256 of relevant state snapshot taken at §5 plan time
  - `expected_node_version`: per-node version counter (incremented on each mutation)
  - `chain_version`: governance HEAD at §5 plan time
  - `lock_token`: signed token from §5.5 Plan Approval

If any optimistic lock check fails, abort execution with `stale_plan` error. No mutation has occurred so no state change to undo, but the plan must be invalidated and re-planning triggered via §5.

### Output contract

- Node state corrected per remediation plan
- Execution evidence recorded (before/after state snapshots)
- Task marked `succeeded` or `failed` with evidence

### Acceptance criteria

- AC7.1: Node state after execution matches expected remediation outcome
- AC7.2: Evidence includes before-state and after-state snapshots
- AC7.3: No side effects on unrelated nodes

### Failure modes

- F7.1: Target node locked by concurrent operation — wait with timeout (300s), then fail
- F7.2: State transition rejected by state_service — log conflict, fail task, escalate
- F7.3: Executor crash mid-operation — heartbeat timeout triggers recovery via lease expiry

### Rollback path

Reverse the node state change using before-state snapshot. If reversal fails, escalate to
observer with full evidence per [manual-fix-sop.md](manual-fix-sop.md) §2 procedures.

---

### 7.5 Atomic Graph Swap

For graph-replacement reconcile actions (e.g. Phase Z v2 swap of
`graph.json` ⇄ `graph.v2.json`), execution uses the atomic-swap module
[`agent/governance/symbol_swap.py`](../../agent/governance/symbol_swap.py).
The legacy `agent/governance/migration_state_machine.py` (V5-era
14-day staged migration with 4-condition swap gate) has been
**REMOVED**; per spec §4.4 v6 / GPT R4 it is replaced by the
deterministic atomic swap below.

**Sequence** (`atomic_swap(graph_path, candidate_path, *, observer_alert=None)`):

1. `shutil.move(graph_path → graph_path.with_suffix('.json.bak'))` —
   the existing graph is backed up.
2. `shutil.move(candidate_path → graph_path)` — the candidate becomes
   the canonical graph.
3. `smoke_validate(graph_path)` runs — purely deterministic, NO AI / NO
   network. It checks: parses as JSON, unique `node_id`s, every layer
   in L0–L6 inclusive, and every primary path resolvable on disk.
4. On smoke failure the function auto-rolls-back: it restores the
   `.bak` to `graph_path`, returns the candidate file to its original
   path, and invokes `observer_alert({"ok": False, "reason": str})`
   exactly once.

`smoke_validate` is required to be deterministic — calling it twice on
the same input MUST return the same result. There are no environment-
or time-dependent branches.

---

## §8 Phase 6: Verification (verify-before-close)

Verification uses a verify-before-close pattern: terminal status is written exactly once, only after all checks complete.

**State machine**: `queued` → `claimed` → `executing` → `executed` → `verifying` → `succeeded | failed`

- §7 Execution completes → status = `executed` (NOT terminal)
- §8 Verification runs ALL checks while status = `verifying`
- If all pass → status = `succeeded` (terminal, written exactly once)
- If any fail → trigger §10 action-specific rollback → status = `failed` (terminal, written exactly once)
- NEVER allow `succeeded` to revert to `failed` after the fact

### Input contract

- Completed reconcile execution (§7) in `executed` state
- Expected end-state from remediation plan
- Access to governance DB for state verification

### Output contract

- Verification result: `pass` or `fail` with evidence
- If pass: status transitions to `succeeded` (terminal, written exactly once)
- If fail: trigger §10 rollback, then status transitions to `failed` (terminal, written exactly once)

### Acceptance criteria

- AC8.1: Verification checks actual DB state (not cached/stale)
- AC8.2: Verification runs within 60s of execution completion
- AC8.3: Failed verification triggers §10 action-specific rollback before writing terminal status
- AC8.4: Terminal status (`succeeded` or `failed`) is written exactly once — no double-write

### Failure modes

- F8.1: DB state inconsistent with expected — trigger §10 rollback, write `failed`
- F8.2: Verification timeout — treat as failure, trigger rollback
- F8.3: Max retries exceeded — escalate to observer, mark task `failed`

### Rollback path

Verification is read-only check. On failure, rollback is handled by §10 action-specific
rollback subsections before terminal status is written.

---

## §9 Phase 7: Review

### Input contract

- Verified reconcile result from Phase 6 (§8)
- Review queue not saturated
- Reviewer available (automated QA or observer)

### Output contract

- Review decision: `approve`, `reject`, `request_changes`
- If approved: proceed to close (§10)
- If rejected: rollback execution and close as `rolled_back`

### Acceptance criteria

- AC9.1: Review completed within stalled threshold (default: 7 days)
- AC9.2: Review decision includes rationale
- AC9.3: Stalled reviews auto-escalate to observer after threshold

### Stalled Review Threshold

Default threshold: **7 days**. If a reconcile task remains in review state beyond this
threshold, it is automatically escalated to observer attention.

Project-level override: Set `reconcile_config.stalled_review_days` in project metadata
to adjust per-project. Minimum: 1 day. Maximum: 30 days.

### Failure modes

- F9.1: Review stalled beyond threshold — auto-escalate to observer notification
- F9.2: Reviewer rejects without rationale — request rationale, block rejection
- F9.3: Conflicting reviews (multi-reviewer) — defer to observer as tiebreaker

### Rollback path

If review rejects: execute rollback from Phase 5 evidence (before-state snapshot).
Per [manual-fix-sop.md](manual-fix-sop.md) rollback procedures if automated rollback fails.

---

## §10 Phase 8: Rollback and Closure

### Input contract

- Failed verification from §8 OR rejected review from §9
- Before-state snapshots from §7 execution evidence
- Action type from remediation plan (determines which rollback subsection applies)

### Output contract

- Rollback completed: affected nodes restored to pre-execution state
- Reconcile run marked `completed` or `rolled_back` in `reconcile_runs`
- Summary report with rollback details and root cause

### Acceptance criteria

- AC10.1: Rollback restores node state to match before-state snapshot
- AC10.2: All rollback actions audited with action-specific event type
- AC10.3: Observer notified if any rollback sub-step fails

Action-specific rollback strategies replace the generic .bak swap. Each subsection defines its own acceptance criteria, audit requirements, and observer escalation path.

### 10.1 State-only Repair Rollback

Restore node state from `.bak` snapshot (existing pattern). AC: before-state matches `.bak` content after restore. Audit: log `rollback_state_repair` with node_id and before/after hashes. Observer alert if `.bak` file missing or corrupted.

### 10.2 Missing-node Creation Rollback

Delete the created node from the graph. Restore `proposed_nodes` to `pending` status. AC: node no longer exists in graph export; proposed_nodes entry reverted. Audit: log `rollback_node_creation` with node_id. Observer alert if deletion fails (foreign key constraints).

### 10.3 Orphan-node Deletion Rollback

Restore deleted node from `.bak` with full metadata. Verify all metadata fields preserved (title, deps, primary, description, layer). AC: restored node matches pre-deletion snapshot. Audit: log `rollback_orphan_deletion` with node_id and metadata hash. Observer alert if metadata fields missing after restore.

### 10.4 Edge Rewrite Rollback

Revert call-graph edges to pre-rewrite state. Recompute affected `in_degree` and `color_count` for all touched nodes. AC: edge set matches pre-rewrite snapshot; derived metrics recalculated. Audit: log `rollback_edge_rewrite` with affected edge list. Observer alert if recomputation produces inconsistent metrics.

### 10.5 Gate-bypass Action Rollback

Revoke the bypass token. Re-enable the original gate check. AC: gate function returns to enforcing state; bypass token invalidated. Audit: log `rollback_gate_bypass` with token_id and revoke reason. Observer alert if gate re-enable fails.

### 10.5b Atomic Graph Swap Rollback (`.bak` Restore)

When a `.json.bak` exists alongside a swapped `graph.json`,
[`symbol_swap.rollback(graph_path, max_age_days=BAK_RETENTION_DAYS)`](../../agent/governance/symbol_swap.py)
restores the previous graph from the backup. Operationally, this is
exposed via the [`scripts/phase-z-v2.py`](../../scripts/phase-z-v2.py)
`rollback` subcommand:

```
python scripts/phase-z-v2.py rollback \
    --graph-path agent/governance/graph.json \
    --max-age-days 30
```

**Retention policy**: `BAK_RETENTION_DAYS = 30` (module-level constant
in `symbol_swap.py`). `rollback()` REFUSES to restore a `.bak` whose
age exceeds `max_age_days` (default 30); this prevents a stale backup
from overwriting weeks of subsequent intentional changes. AC: when
`age_days > max_age_days`, returns `{ok: False, reason: "backup too old: ..."}`
and leaves both `graph.json` and `graph.json.bak` untouched.

The `status` subcommand is the operator-facing inspection point:

```
python scripts/phase-z-v2.py status --graph-path agent/governance/graph.json
```

It returns `{bak_exists, age_days, expired, graph_path, bak_path}` and
is the canonical way to check whether a recent swap is still rollable.

Audit: log `rollback_atomic_swap` with `graph_path`, `age_days`, and
`max_age_days`. Observer alert if rollback returns `ok=False`.

### 10.6 Multi-node Action Rollback

Execute compensating reverse-order undo per node (last-modified-first). If any sub-undo fails, escalate immediately to observer for manual recovery per [manual-fix-sop.md](manual-fix-sop.md). AC: all nodes restored OR observer notified with partial-undo report. Audit: log `rollback_multi_node` with per-node undo status. Observer alert on any sub-undo failure.

### Closure

After rollback (or after successful verification in §8):

- Reconcile run marked `completed` or `rolled_back` in `reconcile_runs`
- Summary report generated with: nodes_fixed, duration, drift_types_resolved
- Metrics emitted for monitoring dashboards

### Failure modes

- F10.1: Rollback itself fails — escalate to observer manual recovery
- F10.2: Audit trail incomplete — block closure until gaps filled
- F10.3: Metrics emission fails — log warning but allow closure (non-blocking)

### Rollback path

If rollback fails at any subsection, escalate to observer. No automated retry of failed rollbacks — manual intervention required per [manual-fix-sop.md](manual-fix-sop.md).

---

## §11 Gate Exemption Patterns

Reconcile operations may require bypassing normal gate checks when repairing stuck states.
The following code locations implement gate exemptions relevant to reconcile:

### 11.1 Gate Functions

| Function | Location | Purpose |
|----------|----------|---------|
| `_gate_release` | `auto_chain.py:3923` | Release gate for merge→deploy transition |
| `_gate_qa_pass` | `auto_chain.py:3745` | QA pass gate — validates test evidence |
| `_gate_t2_pass` | `auto_chain.py:3610` | Tier-2 pass gate — structural validation |
| `_should_defer_doc_gate_to_lane_c` | `auto_chain.py:1898` | Doc-only changes bypass code gates |

### 11.2 Reconcile Gate Bypass Rules

1. **Doc-only reconcile** (`changed_files=['docs/**']`): Exempt from code gates via
   `_should_defer_doc_gate_to_lane_c` (auto_chain.py:1898). This is the pattern used for
   post-swap dummy tasks (see §11.3).

2. **State-repair reconcile**: May bypass `_gate_qa_pass` (auto_chain.py:3745) when
   repairing nodes already verified by prior chain runs. Requires audit evidence of
   prior QA pass.

3. **Emergency reconcile** (P0 stuck gate): May bypass `_gate_t2_pass` (auto_chain.py:3610)
   with observer approval logged to audit. Referenced in [auto-chain.md](auto-chain.md)
   gate documentation.

### 11.3 Post-Swap Dummy Task Pattern

After a reconcile swaps node state, a verification chain is triggered using a docs-only
no-op chain (`changed_files=['docs/**']`). This exercises graph delta inference with zero
code impact — the pattern was proven in MF-2026-04-21-005.

The dummy task:
- Uses Lane C (doc-only) gate path via `_should_defer_doc_gate_to_lane_c`
- Validates that the graph correctly reflects the reconciled state
- Produces chain evidence without modifying runtime code

---

## §12 Versioning and Change Control

This specification is versioned alongside the governance codebase.

### 12.1 Version History

| Version | Date | Change |
|---------|------|--------|
| v2 | 2026-04-28 | GPT round 5 review hardening — Plan Approval gate, optimistic locking, action-specific rollback, 3-tier rate-limit, verify-before-close. See OPT-BACKLOG-RECONCILE-WORKFLOW-SPEC-V2-HARDENING. |
| 1.0.0 | 2026-04-28 | Initial formalization from scratch draft |

### 12.2 Modification Process

Changes to this spec MUST go through a governance chain:

1. PM task defining the spec change scope
2. Dev task implementing the markdown changes
3. QA verification that structural integrity is maintained (lint test passes)
4. Merge to main via normal auto-chain progression

As referenced in [version-control.md](version-control.md), the chain_version is updated
after merge to track this spec's evolution.

### 12.3 Backward Compatibility

When spec changes alter phase contracts:
- Existing in-progress reconcile runs complete under the old spec version
- New runs use the updated spec
- Transition period: both versions coexist until all old runs complete (max 7 days)

---

## §13 Resolved Design Decisions

This section documents design decisions that were resolved during spec formalization.
All questions have been answered and their resolutions integrated into the relevant sections.

### 13.1 Creator Allowlist (formerly Q1)

**Resolution**: Creator allowlist is `observer`, `coordinator`, `auto-chain-reconcile`.
Manual-fix sessions (`MF-*`) are excluded — they must escalate through observer to become
reconcile operations. See §2.1 for full specification.

**Rationale**: Prevents uncontrolled reconcile spawning from ad-hoc fix sessions. Observer
acts as human-in-the-loop gate for MF-to-reconcile escalation.

### 13.2 Stalled Review Threshold (formerly Q2)

**Resolution**: Default 7 days, project-level override via `reconcile_config` metadata.
See §9 for integration into review phase.

**Rationale**: 7 days balances urgency (reconcile fixes real drift) against review bandwidth.
Projects with faster iteration can reduce; stable projects can extend.

### 13.3 Bug ID Naming Convention (formerly Q3)

**Resolution**: Use 8-char short `run_id` prefix for readability:
`OPT-BACKLOG-RECONCILE-{run_id[:8]}-{type}-{slug}`

Example: `OPT-BACKLOG-RECONCILE-a1b2c3d4-stuck-gate-L7-64`

**Rationale**: Full UUID is unwieldy in logs and task lists. 8 chars provides sufficient
uniqueness for human disambiguation (collision probability negligible at project scale).

### 13.4 Post-Swap Dummy Task (formerly Q4)

**Resolution**: Use docs-only no-op chain (`changed_files=['docs/**']`) which exercises
graph delta inference with zero code impact. Pattern proven in MF-2026-04-21-005.
See §11.3 for implementation details.

**Rationale**: A real chain (even doc-only) produces authentic governance evidence —
audit trail, gate passes, version update — without risking code regressions. The Lane C
gate path (`_should_defer_doc_gate_to_lane_c`) already handles this pattern.

---

## Appendix A: Reconcile Run Lifecycle Diagram

```
Detection (§3) → Classification (§4) → Planning (§5) → Plan Approval (§5.5)
    → Task Creation (§6) → Execution (§7) → Verification (§8) → Review (§9) → Closure (§10)
```

Each phase has defined input/output contracts and failure modes documented above.
Failed phases retry up to their specified limits before escalating.

## Appendix B: Configuration Reference

| Key | Default | Description |
|-----|---------|-------------|
| `reconcile_config.rate_limit` | 5/hour | Max reconcile tasks per hour |
| `reconcile_config.burst` | 3 | Max concurrent reconcile tasks |
| `reconcile_config.cooldown_seconds` | 60 | Min seconds between creations |
| `reconcile_config.stalled_review_days` | 7 | Days before review auto-escalate |
| `reconcile_config.scan_interval_minutes` | 30 | Drift detection scan frequency |
| `reconcile_config.grace_period_minutes` | 10 | Ignore recently-modified nodes |

---

## §14 Symbol Reconcile — Cluster-Driven Graph Rebase Session

_(Stub section — see proposal for full design.)_

**Pointer**: [docs/dev/proposal-reconcile-cluster-driven-standard-chain.md](../dev/proposal-reconcile-cluster-driven-standard-chain.md) — full design, contracts, and rollout plan live in the proposal.

**Pipeline summary** (one line): phase_z → cluster_grouper → cluster_report → task type=pm → reconcile session → gatekeeper → overlay → finalize → graph.json.

**Coexistence**: the cluster-driven pipeline coexists with §3-§11 reconcile-type tasks; nothing in §3 is demoted, and both routes remain first-class — the cluster session is an additional surface, not a replacement.

**Scope at landing**: this section is intentionally minimal at landing time; CR6b will expand via dogfood (or observer manual fallback if dogfood fails) once the implemented pipeline can describe itself.

**Proposal anchors**: §4.2 (cluster definition), §4.6 (deferred queue), §4.8 (reconcile session + overlay), §4.9 (snapshot+rollback).

### §14.1 Language adapters

The cluster-driven reconcile pipeline delegates per-language analysis to the
`agent/governance/language_adapters/` package, which publishes a small,
stable contract — the **`LanguageAdapter`** Protocol — used by
`reconcile_phases.cluster_grouper` for similarity scoring and module-root
attribution.

**Required methods** (Protocol surface from `base.py`):

| Method | Contract |
|--------|----------|
| `supports(file_path)` | True if this adapter can analyse the file |
| `collect_decorators(ast_node)` | Decorator names extracted from an AST node |
| `find_module_root(file_path)` | Closest non-`__init__` package boundary |
| `detect_test_pairing(source_file)` | Inferred test file path, or `None` |

**In-tree implementations**:

- **`PythonAdapter`** (`python_adapter.py`) — AST-backed analysis: parses
  `.py`/`.pyi` source via the standard-library `ast` module, walks
  `decorator_list` for `@route` / `@app.route` / `@route(...)` shapes, climbs
  `__init__.py` chains to find the package root, and maps `foo.py` →
  `tests/test_foo.py` for test pairing.
- **`FileTreeAdapter`** (`filetree_adapter.py`) — conservative
  language-agnostic fallback: `supports(...)` returns True for any non-empty
  path, `collect_decorators(...)` returns `[]`, `find_module_root(...)`
  degenerates to `os.path.dirname(...)`, and `detect_test_pairing(...)`
  returns `None` unconditionally. Used when no language-specific adapter
  claims the file (`.go`, `.rs`, `.unknown`, …).

**Invariant** (from `base.py` docstring): adapters MUST be **import-safe**
(no I/O at import time) and **stateless** — two independent instances must
yield identical outputs for identical inputs. The contract is locked down by
`agent/tests/test_language_adapters.py`; `test_cluster_grouper.py` exercises
the same surface indirectly via the grouper.

---

## §15 Scope-Catchup Materialization Contract

The `scope-catchup` wrapper closes reconcile scope-materialization backlog items
such as `RECONCILE-SCOPE-MATERIALIZE-45d31bd-9050487-335273de` without redeploying
the governance service. It creates a temporary branch and worktree for the
requested commit range, runs the scope catchup phases, and leaves runtime
governance code untouched unless the operator explicitly applies generated
backlog materialization.

The wrapper entrypoint is `scripts/reconcile-scope-catchup.py`, backed by
`agent/governance/reconcile_scope_catchup.py`. Its default phase list is
`DEFAULT_PHASES = K,A,E,D,F`, matching the catchup path that discovers scoped
materialization gaps, resolves actionable files, and emits backlog evidence.
Only entries classified in `ACTIONABLE_MATERIALIZATION_TYPES` are eligible for
materialization; non-actionable findings remain audit evidence.

Operator modes are explicit:

- `--dry-run` inspects the range and reports candidate materialization without
  writing backlog state.
- `--apply` writes the selected materialization results.
- `--file-materialization-backlog` points at the backlog artifact used to carry
  file-scoped materialization evidence between dry-run review and apply.

The no-redeploy behavior is part of the contract: the wrapper performs all work
from the isolated branch/worktree checkout and does not require governance
service restart, image rebuild, or executor redeploy.

---

## §16 AC Stable/Dev Runtime Isolation and Promotion

AC repair work uses two independent runtime planes:

- The stable service remains on port `40000` and branch
  `codex/direct-no-pass-post-reconcile-r2`. The first isolation promotion is
  anchored at `a25838f15f949ac434cf78e03f20760e82ff81f0`; every successor promotion
  is anchored at the exact current stable HEAD plus the previous durable
  promotion receipt. Dev work must not checkout, rebase, restart, redeploy, or
  replace that process. `aming-claw start` keeps its generic multi-project
  behavior; AC identity gates apply only to explicit `--runtime-plane stable`
  or `--runtime-plane dev` self-hosting starts.
- The only dev service is branch `codex/ac-dev` on reserved port `40008`.
  Its health response binds port, PID, physical worktree, branch, loaded commit,
  stable anchor, project allowlist, schema policy, and mutation boundary. Each
  dev start resolves the exact non-stale commit currently loaded on stable
  `40000`; only the legacy a258 bootstrap health may omit plane identity.
  Caller-supplied stale anchors and starting dev while stable identity is
  unavailable fail closed, so successor rows do not remain pinned to a258.
  Dev also discovers the one exact stable-branch worktree and requires its
  physical `shared-volume` path and database identity to equal stable health;
  an alternate AC-shaped database is rejected before a process or DB write.
  At the frozen a258 bootstrap only, where old health has no DB identity field,
  the exact stable-worktree database is the sole fallback authority.
  Reusing an already-listening `40008` is also an exact identity check: the
  invoking checkout must be clean and its physical root, branch, full commit,
  loopback bind, loaded runtime, non-stale status, and current stable anchor
  must all equal the service health identity. A process from another worktree
  or an older `codex/ac-dev` commit is never treated as the requested service.
- The dev plane's **mutation** authority accepts only project `aming-claw` and
  only an already-existing
  `shared-volume/codex-tasks/state/governance/aming-claw/governance.db`.
  Empty, foreign, alias, or traversal-shaped project IDs fail before directory
  creation, schema execution, or writes. AC dev connections verify the
  existing schema version and never auto-migrate it; schema repairs run
  against an isolated clone first.
- A separate discovery capability may read a non-AC project only when its
  exact normalized key is already initialized and active in `projects.json`,
  its governance policy says `public_safe=true`, and its project directory and
  database are existing non-symlink objects under the governance root. The
  only routes are bounded public-safe backlog list/item, active graph status,
  and `GET|POST .../onboard-route-guide`. The Onboard result has schema
  `ac_dev_external_read_only_discovery.v1`, is discovery-only, and directs the
  caller to stable `40000`; it never mints a route, CEX, ContractRuntime, QA, or
  session authority and never implies managed PASS. Runtime/source/commit,
  route, task, CEX, QA-session, mismatched nested-project, and unknown
  selectors fail before a handler.
- External discovery never opens a non-AC `governance.db`, WAL, SHM, or rollback
  journal. Registration validates those existing paths and rejects symlink or
  non-regular storage, but the dev process has no external SQLite capability.
  Backlog list/item and active graph status use hard-coded loopback
  `127.0.0.1:40000` GETs owned by stable. The proxy sends no caller headers,
  credentials, body, or arbitrary query selectors; the item projector may
  reuse its already-validated path backlog ID in a fixed bounded search. It
  refuses redirects, bounds timeout and response bytes, and validates exact
  stable plane/port/loaded-anchor/non-stale
  identity both before and after each GET. The frozen `a25838f...` service is
  the sole legacy exception: its real health schema omits plane and database
  identity, so its exact loaded commit and positive PID are frozen on the
  first probe and must be byte-equivalent on the second. A successor must
  provide valid stable-plane and database identities. Backlog reads also bind
  exact project, generation, and authority generation; item reads use one
  health window around targeted-list-before, item, and targeted-list-after and
  require one exact, explicitly public compact row in the returned bounded
  page. A target outside that page fails closed rather than falling back to a
  default-public classification. The proxy republishes only allowlisted public
  fields. Transport, HTTP, JSON, schema, size, privacy, generation, or identity
  drift fails closed. External Onboard is a local typed redirect to fresh
  stable Onboard, but reports discovery available only after its own stable
  health pre/post check; it never calls stable Onboard or any authority mint path.
  Every other non-AC surface remains a pre-handler zero-write rejection.
- Dev may append repair backlog, timeline, ContractRuntime, candidate graph,
  and independent-QA evidence. It cannot activate/finalize current-full,
  rewrite stable version identity, run stable deployment endpoints, or join
  stable Redis/event/background workers.
- The stable authority plane owns observer-session registration, observer
  route-context issue/renew, and managed QA role/session issuance. The dev
  plane never mints these capabilities. An observer first obtains the
  session/route/QA refs through port `40000`, then port `40008` may consume the
  already-persisted refs from the shared AC database and append bounded repair
  evidence. Heartbeat/close/revoke may consume an existing observer ref on the
  dev plane; they cannot create one. Fresh Onboard routing projections are
  usable on `40008` with the public MCP body (which has no `project_root`), but
  any projected authority-mint action must be executed against `40000` before
  returning to dev. Retired `direct_fix` enter/start is denied on both the dev
  allowlist and the active Onboard contract.
- Port `40008` binds only `127.0.0.1`. Branch-service validation is a local CLI
  supervisor using the current Python executable; the former HTTP process
  spawner returns `410` and cannot accept a caller-controlled executable.

Downstream work blocked by an AC defect may use the existing evidence-bound
ContractRuntime bypass and WAIVE path. The bypass must name the AC repair
backlog and preserve the original failed line; `pass_implied=false` remains
mandatory. It may restore downstream WIP but cannot synthesize PASS,
independent QA, close-ready, or stable promotion evidence.

Promotion is a separate operator action. `scripts/merge-and-deploy.sh` rejects
legacy branch arguments and requires an `ac_stable_promotion_manifest.v1`
file. The manifest binds the exact current stable HEAD, prior chained receipt
(or the one-time explicit bootstrap anchor), `codex/ac-dev` candidate, Direct
Main CEX, exact file fence and binary diff hash, and a canonical promotion
intent digest and a second digest over the signable manifest. The QA gate is
one canonical, role-bound `qa.independent_verification` verdict—not three
aliases for one fact. Its `promotion_gate_results` contains the loopback
branch-service regression and all three release-candidate lanes; every
subresult has a test id, PASS status, and report hash. The verdict also binds
the exact ContractRuntime stage/line, candidate/parent comparison authority,
stable anchor, file fence, binary diff, and intent, and must be
non-synthesized. The verdict actor must equal the authenticated QA proof
principal. An identical QA authority under any other project event or line,
including an observer-authored copy under another backlog/task, is an
ambiguous replay and fails closed.

The promotion database is not caller-selectable. The script derives
`shared-volume/codex-tasks/state/governance/aming-claw/governance.db` from the
discovered exact stable worktree and rejects a different
`SHARED_VOLUME_PATH`, symlink, or path escape. A public-safe physical identity
(`ac_stable_database_identity.v1`: device, inode, and the stable-relative path
hash, with no absolute host path) is bound through QA evidence, intent,
manifest, operator signoff, precheck, stable health, prior receipt, and the
completion receipt. Normal SQLite writes preserve this identity; file
replacement does not. The projector re-stats it immediately before Git
mutation, the restarted service reports it from its actual canonical path,
and completion verifies the database opened by SQLite. Bootstrap uses the
current canonical stable DB; every successor must equal both stable health and
the prior receipt's identity.

Operator signoff is a separate user-triggered action on stable `40000`; the
promotion script never creates it. The operator submits a no-op reorder of the
existing release-operator queue (`before == after`) whose canonical JSON
reason uses schema `ac_stable_promotion_operator_signoff.v1` and binds a unique
32-hex nonce, the operator principal, one-hour expiry, backlog/CEX, expected
stable HEAD, candidate, intent and signable-manifest digests, verifier hash,
and diff hash. The returned append-only queue event id is placed in the
manifest. The read-only projector opens the existing database with `mode=ro`
and `query_only`, performs no schema setup, verifies the endpoint-authenticated
actor and exact no-op event, and rejects missing, duplicate, replayed, expired,
extra, or partially bound evidence before any Git/process mutation. A queue
actor ending in `:route_ref` is observer-route-derived and can never satisfy
operator signoff, even if its claimed principal and manifest fields agree.
Dev `40008` cannot issue either gate.

After all evidence, import, syntax, stable-health, worktree, ancestry, and diff
checks pass, promotion uses only `git merge --ff-only` so stable HEAD becomes
the exact reviewed candidate. The host supervisor starts explicit stable mode
on `40000`; post-health must report the exact loaded candidate, stable branch,
anchor, ready identity, and `runtime_stale=false`. The stable runtime then
persists `ac.stable_promotion_completed` with the verifier hash and chained
receipt. That receipt is mandatory as the next row's anchor authority. The
projector reads the complete promotion event chain without public timeline
pagination and runs a second time immediately before the merge CAS, catching
late expiry, DB replacement, QA/signoff replay, or a concurrent successor.
Stable completion holds `BEGIN IMMEDIATE` across durable-gate validation,
project-wide fork/replay detection, and receipt append. A retry after a lost
successful response may return the one exact authenticated durable receipt
after signoff expiry; a different candidate, receipt body, backlog, or CEX is
not idempotent.
The completion request's predecessor hash must also equal both the precheck
projection and manifest `prior_promotion` receipt (or be empty in all three
places for bootstrap) before idempotency or insertion.

The path never targets stale `main`, performs no automatic
rebase, merge commit,
fallback merge, force-kill, branch deletion, stable/dev database copy, or
caller-declared PASS.

### §16.1 Exact Promotion-Successor Onboard State Machine

Backlog `AC-ONBOARD-EXACT-PROMOTION-SUCCESSOR-ROUTE-P0-20260827` is a
read-only Onboard system operation. It is projected after active-epoch and
entered-batch custody, before Direct Main or generic Onboard routing. The
projector owns no activation capability and exposes these ordered states:

1. `baseline_only`: the loaded dev runtime is still exact baseline
   `b8b727828f9bffd690e44e2f50be99a50d137c69`; the typed blocker is
   `promotion_successor_descendant_not_materialized`.
2. `candidate_projected`: the loaded, clean `40008` HEAD is one direct child
   of the baseline. Git separately proves `implementation_delta` from b8 to C
   (the exact three-file row fence) and `promotion_delta` from stable a258 to C
   (the complete stable-verifier fence, binary diff bytes/hash, and per-file
   source hashes). Promotion authority always consumes the latter; the former
   is provenance only. The projection also binds the candidate tree, canonical
   DB identity, stable health, strict Direct Main CEX, and current
   implementation route. Caller claims must equal server-derived authority.
3. `promotion_route_missing`: Onboard projects one explicit
   `observer_route_context_issue` precursor for stable `40000`. A generic
   current ref or a dev-world ref cannot be transferred; only a separately
   resolved canonical stable registry ref with the exact backlog/CEX/row fence
   and an exact singleton `ac_stable_promotion_prepare` action advances the
   state. Route ID, context hash, prompt ID/hash, injection-manifest hash, and
   token ref must all be non-empty. Its evidence
   refs additionally bind C, the candidate tree, a258, the complete promotion
   diff and fence hashes, candidate-authority hash, and DB-identity hash; an
   old-C, missing-evidence, or stale route is not transferable.
4. `route_ready_qa_missing`: the next custody belongs to independent QA. One
   exact-C, role-bound, non-synthesized `qa.independent_verification` verdict
   must bind the a258 comparison, file fence, diff and DB identity and include
   PASS subresults for branch service plus `direct_main`, `mf_parallel`, and
   `mf_batch_parallel`.
5. `qa_ready_rollback_missing`: QA transfers custody directly to backlog
   `AC-PROMOTION-ACTIVATION-ROLLBACK-RESTART-P0-20260827`. While that durable
   row is absent, OPEN, or even projected FIXED, candidate C does not request
   operator signoff and does not emit `ac_stable_promotion_manifest.v1` or any
   old-script-consumable artifact. Row status, `fixed_at`, and `fix_commit` are
   diagnostic only and can never unlock C. A bounded preparation record uses a
   distinct non-executable schema and `deploy.authorized=false`.
6. The rollback repair must land as a descendant D after C. Only a future,
   fresh D-bound successor may prove the full a258-to-D promotion delta, both C
   and rollback-fix ancestry, fresh exact-D independent QA, and a new
   manifest-bound operator signoff. C has no `qa_ready_signoff_missing` or
   `manifest_prepared` transition.

Every state is zero-write and public-safe. Compact and full views preserve the
same state and next action. Every response has
`preparation_only=true` and
`activation_authorized=deploy_ready=rollback_enforced=satisfies_gate=authorizes_write=false`.
The operation never merges, restarts, deploys, activates a graph, records QA,
closes a backlog, or calls the post-deploy completion service. Stale,
ambiguous, replayed, or degraded evidence produces a typed recovery blocker;
it never falls through to an empty confirmation or implicit activation.
Any explicit activation, deploy, redeploy, restart, merge, promote, promotion,
execution, completion, or manifest request alias (including `requested_*`
carrier aliases) is intercepted before state discovery and returns
`activation_rollback_not_enforced`; this fail-closed path performs no health
call and cannot emit a consumable manifest. The same stateless preguard runs at
both HTTP and service ingress before authority-registry, active-integration-
epoch, entered-batch, or database state discovery; the later overlay repeats
the check only as defense in depth.
Public blocker diagnostics expose only the fixed claim source and canonical
activation alias; recursive caller-controlled ancestor keys and values are
never copied into responses or logs.

### §16.2 D-Bound Preparation, Activation, and Recovery

Backlog `AC-PROMOTION-ACTIVATION-ROLLBACK-RESTART-P0-20260827` is the first
successor allowed to describe an executable stable promotion. Candidate D is
one direct child of C (`1012ec422160738356678f721ea470d583fe2be6`). Onboard
keeps two separate immutable authorities: `implementation_delta` covers the
exact five-file C-to-D row fence, while `promotion_delta` covers the full
a258-to-D binary diff, complete fence, byte count, per-file source hashes, and
tree. Only the full promotion delta is consumed by stable promotion.

The D state machine remains read-only. It requires, in order, the durable
current Direct Main implementation line, a new canonical-stable route whose
only action is `ac_stable_promotion_prepare`, exact-D independent QA bound to
both deltas and branch service plus all three lanes, the rollback row FIXED at
exact D, and a fresh D-only one-hour operator signoff. Missing, stale, foreign,
expired, duplicated, or replayed authority produces a typed zero-write
blocker. Generic activation aliases are intercepted before registry, database,
active-epoch, or health discovery. Onboard never runs a command itself.

QA is bound to an immutable `qa_candidate_intent_sha256`: exact D/tree and
runtime sources, both deltas, database identity, and the canonical promotion
route plus the current Direct Main implementation CEX/line/revision/binding.
It deliberately excludes the mutable rollback-row status. After that same QA
is durable, only a rollback row fixed at exact D may create the separate final
activation intent. The final intent adds the rollback authority hash and is
what the later signoff, manifest, read-only precheck, activation plan, and
completion receipt bind. Changing or revoking the prepare route, superseding
its ref, or advancing the implementation CEX/revision invalidates the complete
custody receipt immediately before mutation without rewriting QA history.

Once all gates are durable, Onboard may project
`ac_stable_promotion_manifest.v2`. Preparation and activation are separate
operator commands:

```text
scripts/merge-and-deploy.sh --prepare \
  --promotion-manifest <manifest-v2.json> \
  --activation-plan </absolute/path/outside/all-worktrees/plan.json>

scripts/merge-and-deploy.sh --activate \
  --activation-plan </absolute/path/outside/all-worktrees/plan.json>

scripts/merge-and-deploy.sh --recover \
  --activation-plan </absolute/path/outside/all-worktrees/plan.json>
```

`--dry-run` performs validation without creating the lock, journal, plan, or
any other file and without changing source, database, or process state. The old
one-shot invocation is retired. Preparation writes one
canonical JSON plan with `O_EXCL`, mode `0600`, file and parent-directory
`fsync`, and refuses an existing file, symlink, relative path, or any location
inside stable or dev worktrees. The plan binds the manifest/precheck digests,
D/tree, both deltas, verifier source, exact stable branch and a258 CAS, old PID
birth/command/listener, both exact Git-derived runtime source hashes, canonical
DB device/inode/stable-relative path hash with every parent non-symlink,
current graph identity, the route/CEX custody receipt, hard-coded launch argv
and four lane commands, full-index binary
patch and reverse-patch hash, completion template, journal, and lock. Operator
signoff must still be live when activation validates the plan.

Activation requires exactly one worktree for the canonical stable branch and
holds one exclusive mode-0600 lock. Operator/coordinator tokens and every
environment key shaped like a secret, credential, password, or private key are
removed from the candidate and rollback service environments. All fallible imports, source
and database checks, graph verification, old process identity, and isolated
lane tests run while exact a258 remains alive. Frozen a258 is authenticated by
its exact loaded commit/source, canonical DB, PID birth/argv/listener, and
clean anchor even though its legacy health has no plane identity; its restart
argv remains the legacy `start --workspace ... --port 40000` form. D uses the
strict stable-plane argv and health identity. Immediately before mutation the
script reprojects the durable QA/rollback/signoff receipt byte-for-byte,
appends `MUTATION_INTENT`, and repeats the complete source/process/DB/graph
CAS. The first mutation is stopping that exact PID without force kill. After
the stop, every non-process identity is checked again. The script then
uses only `git merge --ff-only` to D, starts the hard-coded stable command, and
requires exact candidate HEAD/tree/clean state, loaded/current source identity,
PID/listener, stable port 40000, canonical DB, unchanged graph, and the four
deterministic lanes a second time against the started stable candidate. Only
then may the stable completion endpoint append the
v2 receipt. The receipt binds the plan hash and previous append-only journal
entry and remains exactly idempotent after a lost response; absent, duplicate,
ambiguous, or conflicting receipts are fatal. A deterministic HTTP 4xx durable
gate rejection before any accepted response rolls back. The client requires
two byte-equivalent exact receipt projections whose promoted commit, positive
timeline event, and receipt hash agree. Transport loss, timeout, malformed
bytes, conflicting readback, or a journal failure after an exact receipt
instead marks completion uncertain and
never rolls back a deployment that might already be durable. If both exact
completion retries lose their response, the script records
`COMPLETION_AMBIGUOUS` and deliberately
leaves the verified candidate running: it cannot roll back a deployment whose
completion receipt may already be durable. A process restart with exact
`CANDIDATE_HEALTHY` as its final journal state (for example, a crash before the
completion call) may replay the identical completion body and finish
idempotently; a deterministic rejection from this pre-submit state rolls back.
`COMPLETION_SUBMITTING` always replays the exact body but never auto-rolls back
on failure because the first response may have been lost. `COMPLETION_COMMITTED`
is finalize-only and appends no second completion request. Once
`COMPLETION_AMBIGUOUS` itself is durable, automatic replay is
disabled pending operator receipt audit. Any other unjournaled candidate HEAD
fails closed.

Every journal row is canonical, mode 0600, append-only, fsynced, and chained
to the previous row and plan hash. Duplicate terminal rows, a malformed chain,
or `ROLLBACK_FATAL` prevents replay. SIGINT and SIGTERM use the same rollback
path after the first mutation. Before the first mutation they cannot trigger a
source or process rollback. `CANDIDATE_SPAWNED`, rollback-ref intent/restored,
patch-reversed, and old-runtime-spawned/started substates durably retain exact
PID birth/argv and source custody across a crash.

Rollback is bounded and deterministic: stop only the exact verified candidate
PID, compare-and-swap the stable ref from D back to a258, apply the prepared
full-index patch in reverse with `git apply -R --index`, verify clean exact
a258, restart the hard-coded old launch specification, and recheck PID,
listener, port, DB, health, and graph. It never uses reset, checkout, rebase,
force update, force kill, database copy, or graph activation. Failure of
candidate stop, ref CAS, reverse patch, clean-anchor verification, old start,
old health, or graph restoration appends `ROLLBACK_FATAL` and requires
operator recovery; it cannot silently claim a successful rollback. An exact
existing terminal receipt is a no-op replay, while an unjournaled already-
promoted or unrelated HEAD is ambiguous and fails closed.

Isolated acceptance uses a real temporary Git repository for the a258-to-D
full-index reverse patch and the crash window where the ref is already at the
anchor but index/worktree still contain the exact candidate preimage. It also
uses a real subprocess on an ephemeral loopback port to exercise delayed bind,
PID birth/argv continuity, source-bound health, spawned-before-bind cleanup,
and a second process for exclusive-lock contention. These tests never address
the canonical repository service or port 40000.
