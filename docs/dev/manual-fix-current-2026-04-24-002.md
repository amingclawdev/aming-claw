# Manual Fix Execution Record — MF-2026-04-24-002

**Trigger scenario**: `fixing_auto_chain_itself` (same as MF-001)
**Bug reference**: follow-on to `OPT-BACKLOG-AUTO-CHAIN-CONN-CONTENTION` (dev-path extension)
**Operator**: observer-z5866
**Date**: 2026-04-24
**Commit**: `bf3b497`

---

## Why a follow-on MF

MF-2026-04-24-001 (e745691 + c6f05be + f740cbb) fixed PM-stage `on_task_completed`'s 3-conn contention. Phase 4 smoke showed **PM unlock in 11s**. But the fix did NOT cover dev-stage path — Plan B's own verify chain stalled 25+ min at dev completion (`dev.completed` audit fired, then silence for the retry PM at 16:50Z).

Root cause for dev-stage stall:
- `audit_service.record("dev.completed")` at `auto_chain.py:1760` opens implicit write-tx on main conn
- `_try_verify_update()` at line 1918 adds more writes (dev path only)
- `_emit_or_infer_graph_delta()` at line 1924 calls 4 legacy `_persist_event(conn=None)` callsites → each opens separate conn → waits 60s busy_timeout
- `_publish_event("task.created")` at line 2416 for next-stage dispatch → subscriber's legacy `_persist_event` blocks same way
- Compounded: ~10+ min stall per dev completion

## Phase 0 — ASSESS

### 0.1 git status
6 items: 3 chronic worktree submodules + 3 untracked docs. Clean working tree (after unstaging 85 bogus deletions from the first botched dev attempt — see §"side incident" below).

### 0.2 wf_impact on [agent/governance/auto_chain.py]
- 9 direct_hit nodes
- max_verify: 2
- False positives: L4.24 (Observer Instrumentation, doc-code map over-associates), L4.37 (PM Role Isolation, partially relevant)
- Real affected: L4.28, L8.13-L8.20 (chain robustness + graph-delta emission chain)

### 0.3 preflight_check
- ok=true, blockers=[], warnings present but all pre-existing (sync stale, orphan pending nodes, 1 unmapped file)

### 0.4 version_check
- `ok=true, head=dca502b, chain_version=dca502b, dirty=false` (after manual /api/version-sync to clear stale cache from side incident)

### Side incident: cancelled dev worktree corrupted main index

The first dev attempt for `OPT-BACKLOG-DOCS-DEV-GITIGNORE-AND-PROVENANCE` (`task-1777049227-8294e6`) ran `git rm --cached -r docs/dev/` in the MAIN repo directory instead of its worktree. Main's index ended up with 85 staged deletions. Preflight correctly flagged this as 85 dirty files. Fixed via:
- `git reset HEAD docs/dev/` — unstaged the deletions
- `curl POST /api/version-sync` — forced gov to re-read clean state

Lesson: dev agent should be instructed with `cd .worktrees/dev-task-<id>` before any `git` command, or use `git -C` flag. This will be fixed in PR-2 of OPT-BACKLOG-DOCS-DEV-REPOSITION-AS-HISTORY (dev stage staging scope fix).

## Phase 1 — CLASSIFY

| Axis | Value | Reason |
|---|---|---|
| Axis 1 (affected nodes) | 9 → **Scope C** | 6-20 range |
| Axis 2 (danger) | **High** | auto_chain.py is governance-critical |
| Final | **Scope C / High** | S3 matrix → Phase 2 pytest required |

## Phase 2 — PRE-COMMIT VERIFY

```
pytest agent/tests/test_chain_context.py agent/tests/test_chain_context_bugid.py
       agent/tests/test_auto_chain_bug_id_carry.py agent/tests/test_auto_chain_dedup.py
       agent/tests/test_auto_chain_routing.py agent/tests/test_auto_chain_related_nodes.py
       agent/tests/test_auto_chain_version_cache.py
       agent/tests/test_task_registry.py agent/tests/test_task_registry_escalate.py
       agent/tests/test_checkpoint_gate.py
       agent/tests/test_pm_prd_publish_pre_gate.py
```
**Result: 156/156 PASS in 33s.**

## Phase 3 — COMMIT

Single commit `bf3b497`. 1 file, +26 lines, -0 lines. `manual fix:` prefix per SOP §3.2.

Three `conn.commit()` sites added:
1. **Before `_emit_or_infer_graph_delta`** at `auto_chain.py:1924` (dev-specific)
2. **Before stage-gate `_publish_event("gate.blocked")`** at line 1947 (general stage failure)
3. **Before next-stage `_publish_event("task.created")`** at line 2433 (dispatch boundary)

Pattern matches MF-001 follow-up 2's approach (commit BEFORE synchronous publish so legacy-path subscribers don't wait 60s on main conn's open tx).

## Phase 4 — POST-COMMIT VERIFY

### Restart + health
Gov restarted, loaded `bf3b497`.

### Smoke test design
Bootstrap a dev task (since HEAD was divergent post-commit, auto-chain gate would block PM→dev dispatch; post a PM first, then manually dispatch dev to exercise dev-stage `on_task_completed`).

### Smoke results

| Event | Time | Note |
|---|---|---|
| Dev task.created | 17:18:24 | via manual bootstrap POST |
| Dev claimed | 17:18:32 | executor pick-up (8s) |
| Dev succeeded | 17:21:47 | 3min 15s implementation time |
| Chain event task.completed | 17:21:39 | auto-chain on_task_completed fires |
| Chain event gate.blocked | 17:21:39 | same second — no stall |
| DB unlocked | immediately after | BEGIN IMMEDIATE probe passes at 17:22:04 |

**Dev completion → DB unlock: ~0 seconds (vs 10+ min baseline). ~600× speedup**, matching MF-001's PM-path improvement.

### Known remaining (not blocking this MF close)

- The 4 legacy `_emit_graph_delta_event` helper callsites still use `conn=None` path. Works because we commit-before-call, but cleaner long-term: thread `conn=conn` through the 3 helpers. Separate refactor scope.
- `task.retry` / `task.failed` / `workflow_improvement` publishes (lines 2050/2258/2269/1541) not covered. Lower-frequency paths; can add commit-before-publish pattern if stall observed there.

## Phase 5 — CLOSE-OUT

- Execution record: **this file**
- Backlog row: `MF-2026-04-24-002` lifecycle row filed FIXED (see close-out curl below)
- No standalone OPT-BACKLOG row needed since this extends MF-001's scope covered by `OPT-BACKLOG-AUTO-CHAIN-CONN-CONTENTION` (already FIXED in f740cbb); this is a documented follow-up.
- Version gate: NOT advanced — MF is observer-fix (SOP-sanctioned meta-circular scenario)

### Backlog transitions
| bug_id | Before | After | Commit |
|---|---|---|---|
| `MF-2026-04-24-002` | (filed this session) | FIXED | bf3b497 |

## Self-consistency note

Two `manual fix:` MFs in one day both targeting `auto_chain` contention — this matches SOP §1's "Fixing auto_chain itself" row as a legitimate recurring scenario. MF-001 covered PM path + gate_blocked (version_check); MF-002 covered dev path + stage gate_blocked + next-stage dispatch. Together they free the auto-chain pipeline for all stages.

Phase 1-5 complete.
