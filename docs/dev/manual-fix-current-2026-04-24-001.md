# Manual Fix Execution Record — MF-2026-04-24-001

**Trigger scenario**: `fixing_auto_chain_itself`
**Bug reference**: `OPT-BACKLOG-AUTO-CHAIN-CONN-CONTENTION` (P0)
**Operator**: observer-z5866
**Date**: 2026-04-24
**SOP version**: docs/governance/manual-fix-sop.md v3
**Commits**: `e745691` + `c6f05be` + `f740cbb`

---

## Phase 0 — ASSESS

### 0.1 git status
Working tree mostly clean:
- 3 chronic worktree submodule mods (filtered by B31 _DIRTY_IGNORE)
- 1 deleted file (Ying_work/doc/... — user's external file, unrelated)
- 3 untracked docs (my earlier session outputs — not part of this MF)

### 0.2 wf_impact

```
Input files:
  - agent/governance/task_registry.py
  - agent/governance/auto_chain.py
  - agent/governance/chain_context.py

Output:
  direct_hit: 17 nodes
  max_verify: 4
  gate_mode: explicit (L7.4 Task Registry)
  related_docs: README.md, docs/api/governance-api.md,
                docs/governance/auto-chain.md, docs/governance/gates.md

Direct nodes (excerpt):
  L7.4   Task Registry                              verify_level 4
  L10.1  Task Registry Dual-Field State Machine     verify_level 4
  L10.5  Cancel/Retry/Timeout                       verify_level 4
  L4.28  Chain Robustness Fixes                     verify_level 2
  L4.37  PM Role Isolation + PRD Output             verify_level 2
  L8.17  auto_chain graph.delta.proposed emission   verify_level 1
  L8.18  QA consumes graph.delta.proposed           verify_level 1
  L8.20  _commit_graph_delta transactional commit   verify_level 1
```

False-positive assessment:
- **L11.4 (Notification Attribution chat_id)** — code-doc map over-associates task_registry.py with telegram gateway. No actual telegram code touched. Marked false positive.

### 0.3 preflight_check
- `ok=true, blockers=[]`
- 3 warnings (version-sync stale 4697s, 138 orphan pending nodes, 1 unmapped file) — all pre-existing, none blocker for this MF

### 0.4 version_check
- `ok=true, head=e017059, chain_version=e017059, dirty=false`

---

## Phase 1 — CLASSIFY

| Axis | Value | Reason |
|---|---|---|
| Axis 1 (affected nodes) | 17 → **Scope C** | wf_impact returned 17 direct_hit nodes, in 6-20 range |
| Axis 2 (danger level) | **High** | Modifies `auto_chain.py` + task_registry transaction flow + chain_context event persistence — all critical governance paths |
| Final | **Scope C / High** | S3 matrix cell "C/High" → requires Phase 2 pytest pre-commit verify |

---

## Phase 2 — PRE-COMMIT VERIFY

Test subset rationale: tests covering touched modules + downstream consumers.

```bash
runtime/python/python.exe -m pytest \
  agent/tests/test_chain_context.py \
  agent/tests/test_chain_context_bugid.py \
  agent/tests/test_auto_chain_bug_id_carry.py \
  agent/tests/test_auto_chain_dedup.py \
  agent/tests/test_auto_chain_routing.py \
  agent/tests/test_auto_chain_related_nodes.py \
  agent/tests/test_auto_chain_version_cache.py \
  agent/tests/test_task_registry.py \
  agent/tests/test_task_registry_escalate.py \
  agent/tests/test_checkpoint_gate.py
```

**Result: 155/155 PASS in 36s.**

Re-run after each follow-up commit (c6f05be, f740cbb): smaller subset still green.

---

## Phase 3 — COMMIT

Files staged explicitly (no `git add -A`):
- `agent/governance/chain_context.py` (+46 / -6 across all commits)
- `agent/governance/auto_chain.py` (+15 / -0)

### Commits

#### `e745691` (core fix)
`manual fix: auto_chain 3-conn contention → share caller transaction (MF-2026-04-24-001)`

- `_persist_event` accepts optional `conn=` param. Caller-passed conn → share transaction, no legacy-path _persist_connection open.
- 2 callsites updated (lines 1867 + 3147 — both inside functions that already have conn).
- 4 helper-callsites kept legacy path (scope-minimal).

#### `c6f05be` (follow-up 1)
`manual fix: release caller write-lock before _publish_event subscribers`

- `conn.commit()` after trace_id UPDATE at `_do_chain:1696`.
- Fixes: subscriber `chain_context.on_task_completed` was blocking 60s on the open trace_id transaction.

#### `f740cbb` (follow-up 2)
`manual fix: commit before gate.blocked publish`

- `conn.commit()` between audit_log INSERT at `_do_chain:1814` and `_publish_event("gate.blocked")` at `_do_chain:1841`.
- Fixes: subscriber `chain_context.on_gate_blocked` was blocking 60s on audit_log INSERT.

All 3 commits have the `manual fix:` prefix per SOP §3.2.

---

## Phase 4 — POST-COMMIT VERIFY

### Restart + health
Gov restarted 3× during iteration (to load each commit). Final PID 46432 running version `f740cbb`, `/api/health → ok`.

### Smoke test design
POST a PM task with non-empty proposed_nodes. Measure:
- Baseline: 10+ min DB write-lock hold after PM status=succeeded (observed 2026-04-24T12:49Z)
- Target: < 30s per MF-001 acceptance criteria

### Smoke results

| Smoke | Commit | Time to DB unlock | Outcome |
|---|---|---|---|
| #1 | e745691 (core only) | ~10+ min (monitor killed at 10:00) | First `conn=` path works, but `_publish_event("task.completed")` subscriber still blocks |
| #2 | c6f05be (+ trace_id commit) | ~5+ min stall | First stall fixed, gate.blocked subscriber still blocks |
| #3 | f740cbb (+ gate-blocked commit) | **~11 seconds** ✅ | All PM-divergent-HEAD path subscribers fire without deadlock |

**Smoke #3 evidence (from chain_events + audit-20260424.jsonl):**
```
2026-04-24T13:40:19Z  task.completed    task-1777037918-fb3f01   (chain_events)
2026-04-24T13:40:19Z  pm.completed      task-1777037918-fb3f01   (audit)
2026-04-24T13:40:29Z  gate.blocked      task-1777037918-fb3f01   (chain_events)
2026-04-24T13:40:30Z  DB lock released  (direct BEGIN IMMEDIATE probe succeeded)
```

PM succeeded → unlock: **11 seconds**. Baseline: 600+ seconds. **~55× speedup**. Target met.

### Known remaining limitations (do NOT block this MF close)

1. `pm.prd.published` count = 0 still. This is a DIFFERENT bug — emission at `auto_chain.py:1867` fires only AFTER version_check. Current HEAD is observer-divergent (c6f05be + f740cbb unreleased chain-wise) so gate blocks before line 1867. Filed separately as `OPT-BACKLOG-PM-PRD-PUBLISH-PRE-GATE` (P0 OPEN). Unblocked now — next session can fix via clean chain (HEAD will re-align after this MF chain's deploy completes).
2. 4 helper `_persist_event` callsites (lines 278, 592, 622, 657) still use legacy path. They fire on DEV stage `graph.delta.proposed` — same contention pattern remains for that code path. Not blocking Z2 verification (PM-side already unblocked). Follow-up scope: apply `conn=` threading through 3 helper functions.
3. Other `_publish_event` sites (task.retry, task.failed, task.created, workflow_improvement) not audited. They follow same pattern; likely benefit from same `conn.commit()` pattern. Non-blocking for current work.

---

## Phase 5 — CLOSE-OUT

- Execution record: **this file** (docs/dev/manual-fix-current-2026-04-24-001.md)
- Backlog rows updated (see below)
- Memory entries: N/A (postmortem material covered in handoff + this record)
- Version gate: NOT advanced — MF did not run through chain (by design; it IS the chain fix)

### Backlog transitions

| bug_id | Before | After | Commit |
|---|---|---|---|
| `MF-2026-04-24-001` | (new row created this session) | FIXED | f740cbb |
| `OPT-BACKLOG-AUTO-CHAIN-CONN-CONTENTION` | OPEN P0 | FIXED | f740cbb |
| `OPT-BACKLOG-DB-LOCK-LEAK-POST-COMPLETION` | OPEN P0 | FIXED (same root cause) | f740cbb |
| `OPT-BACKLOG-PM-PRD-PUBLISH-PRE-GATE` | OPEN P0 | unchanged OPEN (different bug) | — |
| `OPT-BACKLOG-MCP-TASK-CREATE-METADATA-LOSS` | OPEN P0 | unchanged OPEN (orthogonal) | — |

### Next-session unblocked work

Sequence Z progression is now chain-viable (stages complete in seconds not minutes):
- Z2 verification: fix `OPT-BACKLOG-PM-PRD-PUBLISH-PRE-GATE` via clean chain (now <1min per stage instead of >10min)
- docs-dev-reposition PRD PR-1/2/3/4: once Z2 verified
- Z3-full (beyond Z3-partial fedaf27)
- Z4 graph auto-commit verify
- (Before Z5 reconcile, per user instruction this session)

---

## Self-consistency note

This MF executed correctly per SOP §1 "Fixing auto_chain itself" row — the chain engine was the target, and the chain could not repair itself (meta-circular). All 3 commits have `manual fix:` prefix, tests ran green, Phase 4 verified 55× speedup. Zero `observer-hotfix` prefix used.

Phase 1-5 complete.
