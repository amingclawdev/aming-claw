# Reconcile Workflow Governance Specification

> **Meta-governed document** — Modifications to this spec require a governance chain (modify via chain).
> Canonical governance spec for the reconcile workflow.
> Last updated: 2026-04-28 | Initial formalization from scratch draft

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

Reconcile tasks are rate-limited to prevent runaway repair loops:

- **Default rate limit**: Maximum 5 reconcile tasks per hour per project
- **Burst allowance**: Up to 3 concurrent reconcile tasks
- **Cooldown**: 60 seconds minimum between consecutive reconcile task creations
- **Override**: Project-level `reconcile_config.rate_limit` metadata can adjust thresholds

Rate limit violations are logged as `F2.1` failure events and require observer intervention.

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

- Drift report: list of `{node_id, drift_type, evidence, severity}`
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

- Classified drift items with proposed remediation action
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

## §8 Phase 6: Verification

### Input contract

- Completed reconcile execution (§7)
- Expected end-state from remediation plan
- Access to governance DB for state verification

### Output contract

- Verification result: `pass` or `fail` with evidence
- If pass: task transitions to QA stage
- If fail: task marked for retry or escalation

### Acceptance criteria

- AC8.1: Verification checks actual DB state (not cached/stale)
- AC8.2: Verification runs within 60s of execution completion
- AC8.3: Failed verification triggers automatic retry (max 2 retries)

### Failure modes

- F8.1: DB state inconsistent with expected — retry execution from Phase 5
- F8.2: Verification timeout — treat as failure, retry
- F8.3: Max retries exceeded — escalate to observer, mark task `failed`

### Rollback path

Verification is read-only check. On failure, rollback is handled by re-entering Phase 5 (§7)
with rollback flag, which reverses the state change.

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

## §10 Phase 8: Closure

### Input contract

- Approved review from Phase 7 (§9) OR rollback completion
- All affected nodes in expected final state
- Audit trail complete for the reconcile run

### Output contract

- Reconcile run marked `completed` or `rolled_back` in `reconcile_runs`
- Summary report generated with: nodes_fixed, duration, drift_types_resolved
- Metrics emitted for monitoring dashboards

### Acceptance criteria

- AC10.1: Run status is terminal (`completed` or `rolled_back`)
- AC10.2: All intermediate artifacts (plans, evidence) linked to run record
- AC10.3: Summary includes actionable insights for preventing recurrence

### Failure modes

- F10.1: Final state verification fails at closure — re-open run, return to Phase 6 (§8)
- F10.2: Audit trail incomplete — block closure until gaps filled
- F10.3: Metrics emission fails — log warning but allow closure (non-blocking)

### Rollback path

Closure is terminal. If reopened due to F10.1, the run returns to verification phase.
No "undo close" — the run simply transitions back to active state.

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
Detection (§3) → Classification (§4) → Planning (§5) → Task Creation (§6)
    → Execution (§7) → Verification (§8) → Review (§9) → Closure (§10)
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
