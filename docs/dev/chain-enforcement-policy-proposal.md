# Chain-Enforcement Policy Proposal

> **Location**: `docs/dev/` — this is a DRAFT. It may only be promoted to `docs/governance/chain-enforcement-policy.md` **after** a PM→Dev→Test→QA→Gatekeeper→Merge→Deploy chain completes with `qa.status=succeeded` and `gatekeeper` scope-verified against this PRD. Moving any governance file before chain validation is itself the violation this proposal forbids.
> Status: **DRAFT v1 — pending review**
> Author: observer-z5866
> Date: 2026-04-21
> Related MF: `MF-2026-04-21-005`
> Related epic: `OPT-BACKLOG-CHAIN-ENFORCEMENT` (to-be-filed, P1)
> Promotion target: `docs/governance/chain-enforcement-policy.md` (authoritative path after chain validates)

## 1. Motivation

The CH1 + CH2 observer-takeover incident surfaced three independent gaps:

| # | Gap | Evidence |
|---|------|----------|
| G1 | Observer can complete stage tasks via `/api/task/complete` with arbitrary `worker_id` and the audit log mis-attributes the event to `actor=auto-chain` | `audit_log WHERE ts between '2026-04-21T22:50Z' and '2026-04-21T23:02Z'` shows 7 stage.completed rows with `actor=auto-chain` that were in fact POSTed by observer-z5866 |
| G2 | `skip_version_check` bypass path exists but is never triggered from the observer-takeover route, so the `version_gate_bypass` audit counter stays at 0 even when the gate is effectively circumvented | `audit_log WHERE event='version_gate_bypass'` → `count=0` despite 2 full chains that bypassed executor dispatch |
| G3 | Manual-fix SOP permits direct `git commit` at Scope A/B (per S3 matrix), which lets structural drift land without test/QA evidence | `docs/governance/manual-fix-sop.md §3 — Combined Rules Matrix` rows "A/Low", "A/Medium", "B/Low" all say "Commit directly" or "Run module tests" — no chain coupling |

The combination means: a sufficiently motivated observer can commit code, fake a full PM→Dev→Test→QA→Gatekeeper→Merge→Deploy trail, and leave zero structured bypass evidence.

This proposal removes those paths.

## 2. User Intent (verbatim, for the record)

> 后续可以禁止，manual fix直接提交，必须用chain走测试验收后才能commit，限死version gate, 把之前做的bypass删去？

Decomposed:
1. **Forbid `manual fix` direct commit** (even at Scope A/B).
2. **Chain must carry test+QA before any commit** is allowed.
3. **Lock down the version gate** (no loopholes).
4. **Remove the existing `skip_version_check` bypass** path from code.

## 3. Proposed Policy Changes

### P1 — Audit attribution integrity (G1)

**Change**: `handle_task_complete` (`server.py`) MUST set `audit_log.actor = request.body.worker_id` (falling back to token principal_id if no body worker_id), not the stage-type constant `auto-chain`.

**Rejection rule**: any complete call where `worker_id` matches `/^observer/` or the claimed-by row, raise a new audit event `task.observer_direct_complete` AND — unless `metadata.operator_id + metadata.bypass_reason + X-Observer-Override=true` all present — return HTTP 403.

**Legitimate observer-complete path (narrow)**: observer must claim the task first (same as executor), then supply `operator_id + bypass_reason + X-Observer-Override=true`. This produces a `task.observer_direct_complete` audit row with full attribution, identical to the existing `task.observer_override` but at the complete-time (not claim-time) — giving per-call visibility.

### P2 — Chain-mandatory before commit (G3)

**Change**: `docs/governance/manual-fix-sop.md` §3 "Combined Rules Matrix" cells **A/Low, A/Medium, B/Low** to be re-worded:

| Old text | New text |
|----------|----------|
| "Commit directly / No extra checks" | "Dispatch minimal `dev` task via `/api/task/{pid}/create type=dev` with `pre_merged=false`; let chain run test+QA before any `git commit`" |
| "Run module tests" | same as above + `test_files` field mandatory |

**Tightening**: manual fix **may still** perform the commit itself (so the chain has commits to advance), but **only AFTER** a test+QA stage pair has completed `succeeded` for the exact changed_files set. Enforcement:
- New `/api/wf/{pid}/manual-fix-readiness` endpoint: POST `{changed_files}` → returns `{ready: bool, last_passed_test_task_id, last_passed_qa_task_id, staleness_seconds}`.
- Observer SOP appended rule **R12**: "Before `git commit` in any MF, call `manual-fix-readiness` with the staged-files list. If `ready=false`, the commit is forbidden; the observer must create a dev task and let executor run it first."
- Optional hard enforcement: pre-commit hook that calls R12 and refuses to commit if `ready=false`.

**Exception** (bootstrap deadlock): if `preflight_check` returns `bootstrap_required=true`, R12 is waived, but the observer must pre-declare a `MF-YYYY-MM-DD-NNN` backlog row with `trigger_scenario=governance_startup_failure` — and the first real commit after bootstrap must include the R12-compliant dev-task evidence for all subsequent changes.

### P3 — Version-gate lockdown (G2)

**Remove entirely**: `metadata.skip_version_check` handling in `_gate_version_check` (`auto_chain.py` lines 1804–1820). The structured bypass path was added for emergency use but:
- `grep -r skip_version_check agent/` shows it's referenced only in `auto_chain.py` + 3 test files; no production code relies on it.
- Its 0-count in audit_log confirms it's never used in the legitimate emergency scenario — only as a theoretical escape hatch.
- `_DISABLE_VERSION_GATE` dev override (line 1802) stays as `DEBUG-only` but gains a startup assertion: `DEBUG=True` required, else server refuses to start.

**Keep**: `observer_merge`, `RECONCILIATION_BYPASS_POLICY` (lane-based), `_is_governed_dirty_workspace_chain` — these are chain-internal; observer can't invoke them from outside.

**Add**: audit event `version_gate_bypass_attempt` when `metadata.skip_version_check=true` is observed in any complete/dispatch, regardless of whether bypass is granted. (After P3 is landed, bypass is never granted, so every such row is a regression alarm.)

### P4 — Deprecate R11's bypass phrasing

`manual-fix-sop.md` R11 says "omitting this step causes version gate to block all subsequent workflow tasks" — implying it's a self-service operation. Re-word: "chain_version is advanced by the `deploy` stage or by `_finalize_chain`. Observers must not call `/api/version-update` directly unless the chain's `_finalize_chain` demonstrably failed; in that case, the call must be preceded by `MF-YYYY-MM-DD-NNN` pre-declaration with `trigger_scenario=chain_finalize_failure`."

## 4. Code Diff Sketch (authoritative version will go through the chain)

### 4.1 `agent/governance/auto_chain.py` (P3)

Remove lines 1804–1820, keep `_audit_version_gate_bypass` as a legacy helper used only for audit tests.

### 4.2 `agent/governance/server.py` (P1)

In `handle_task_complete` (approx line 1820, not shown here), change audit record call:

```python
# BEFORE
audit_service.record(conn, pid, f"{task_type}.completed", actor="auto-chain", ...)
# AFTER
actor = body.get("worker_id") or (session.get("principal_id") if session else "anonymous")
audit_service.record(conn, pid, f"{task_type}.completed", actor=actor, ...)
# plus observer-guard:
if actor.startswith("observer"):
    if not (body.get("operator_id") and body.get("bypass_reason") and ctx.headers.get("X-Observer-Override")):
        raise PermissionDeniedError(actor, f"{task_type}.complete",
            {"detail": "observer complete requires operator_id + bypass_reason + X-Observer-Override header"})
    audit_service.record(conn, pid, "task.observer_direct_complete", actor=actor, ...)
```

### 4.3 New endpoint `/api/wf/{pid}/manual-fix-readiness` (P2)

```python
@route("POST", "/api/wf/{project_id}/manual-fix-readiness")
def handle_mf_readiness(ctx):
    changed_files = set(ctx.body.get("changed_files", []))
    STALENESS_CAP = 30 * 60  # 30 min
    with DBContext(project_id) as conn:
        # find newest qa.succeeded whose test_files⊇changed_files
        rows = conn.execute(
            "SELECT task_id, result_json, updated_at FROM tasks "
            "WHERE type='qa' AND status='succeeded' "
            "ORDER BY updated_at DESC LIMIT 20"
        ).fetchall()
    for r in rows:
        result = json.loads(r["result_json"] or "{}")
        test_files = set(result.get("test_files", []))
        if changed_files.issubset(test_files):
            age = (now_utc() - parse_ts(r["updated_at"])).total_seconds()
            if age <= STALENESS_CAP:
                return {"ready": True, "qa_task_id": r["task_id"], "staleness_seconds": age}
    return {"ready": False, "reason": "no_recent_qa_pass_for_changed_files"}
```

### 4.4 Pre-commit hook (P2, optional hard enforcement)

Shipped in `scripts/pre-commit-chain-check.sh`; installed via `git config core.hooksPath .hooks` — refuses commit when `manual-fix-readiness.ready==false`.

## 5. Migration Plan

Because this proposal itself mandates chain-before-commit, the landing sequence is:

1. **This document** (`docs/governance/chain-enforcement-policy-proposal.md`) lands first, through a PM→Dev→Test→QA→Merge chain of its own. Trigger: create PM task with PRD = this document. Dev writes the implementation code; test task runs new tests; QA verifies AC; merge commits. The chain that lands this policy is the proof it is landable.
2. Once landed, P1+P3 are enforced immediately. P2's R12 is added to the SOP but **hook installation is opt-in for 2 weeks**, then mandatory.
3. Audit dashboard adds `observer_direct_complete` + `version_gate_bypass_attempt` counters to weekly review.
4. `skip_version_check` references in tests migrate to `observer_merge` + `reconciliation_lane` paths (tests should already cover these).

## 6. Open Questions (for user review)

1. Should P1's observer-complete guard apply to all stage types, or only to `merge/deploy`? (Arg for narrower: PM/Dev can't commit code, so the risk is lower; arg for broader: any stage's completion advances the chain, which can gate version-update.)
2. Should P3's removal be a soft-deprecation (log.error + still bypass for 1 release) or a hard cut? Recommendation: **hard cut** — 0 production callers.
3. `_DISABLE_VERSION_GATE` startup assertion: should it read from `APP_ENV`, from a CLI flag, or from the config file? Recommendation: CLI flag (`--allow-gate-disable`) combined with `APP_ENV=development`; anything else blocks start.
4. P2's R12 pre-commit hook: is 30min the right staleness cap? Commits that happen fast after QA are fine; anything past 30min means another HEAD moved in between, re-validating is cheap.

## 7. Chain that would land this proposal

```
PM task  → PRD = this document, acceptance_criteria = list of code diff sketches above
Dev task → implement 4.1 / 4.2 / 4.3, add tests
Test task → new test file agent/tests/test_chain_enforcement_policy.py (covers:
              - observer complete without override header -> 403
              - skip_version_check in metadata -> no bypass granted + version_gate_bypass_attempt row
              - manual-fix-readiness endpoint returns ready/not-ready correctly
              - pre-commit hook refuses when not ready)
QA task  → verify all acceptance_criteria one-by-one
Gatekeeper → scope check against PRD
Merge    → commit on main
Deploy   → trivial; stage changes memory-only (no service restart needed for endpoint change)
```

If the user approves the proposal, the chain that lands it is the exact chain described above — starting with `POST /api/task/aming-claw/create type=pm` with this document inlined as `metadata.prd`.
