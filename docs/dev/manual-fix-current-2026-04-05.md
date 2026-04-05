# Manual Fix Execution Record — 2026-04-05

> SOP Reference: `docs/dev/manual-fix-sop.md` v2
> Operator: observer (AI agent, current session)
> Trigger: dirty workspace blocking all auto_chain dispatch (B1/B6)

---

## Phase 0: ASSESS (read-only baseline)

### 0.1 git status

```
M  agent/governance/server.py       (staged, +37 lines)
?? .claude/worktrees/               (ignored, not committing)
?? agent/governance/reconcile.py     (untracked, new file ~520 lines)
?? agent/tests/test_reconcile.py     (untracked, new file ~340 lines)
?? docs/dev/bug-and-fix-backlog.md   (untracked, new file)
?? docs/dev/manual-fix-sop.md        (untracked, new file)
?? docs/dev/reconcile-flow-design.md (untracked, new file ~1112 lines)
```

Files to commit: 6 (excluding .claude/worktrees/)

### 0.2 wf_impact

| File Group | Affected Nodes | Details |
|-----------|:--------------:|---------|
| server.py | 15 | L4.15, L5.3, L5.4, L7.1, L7.4, L8.2, L8.4, L8.5, L9.1, L9.2, L9.3, L9.5, L9.8, L9.10, L10.8 |
| reconcile.py, test_reconcile.py, 3x .md | 0 | New files, not mapped in any node |
| **Total unique** | **15** | |

### 0.3 preflight_check baseline

```
system:    PASS  (25 tables)
version:   FAIL  (chain=dfdc5f1 vs head=dfdc5f1... short/full mismatch, sync stale 3133s)
graph:     WARN  (56 orphan pending nodes)
coverage:  WARN  (49 unmapped files)
queue:     PASS  (0 queued, 0 claimed)

Blockers:  1 (version mismatch — pre-existing, not caused by this fix)
Warnings:  2 (orphan nodes + unmapped files — pre-existing)
```

### 0.4 version_check

```
ok:             false
head:           dfdc5f1780dca487dd6b3df2f7fec4d469d32801
chain_version:  dfdc5f1
dirty:          false (governance DB sees no dirty — executor sync lag)
dirty_files:    [] (git status shows staged server.py, but DB doesn't reflect it)
```

Note: version_check.dirty=false is misleading. git status shows server.py staged. The executor's 60s sync cycle hasn't picked up the staged change. This is a known inconsistency — the DB dirty_files tracker only sees committed vs HEAD diff, not staged changes.

---

## Phase 1: CLASSIFY (dual-axis)

### Axis 1 — Scope: C (15 reported nodes)

### Axis 2 — Danger: Low

All changes are additive:
- server.py: +37 lines, one new route handler `POST /api/wf/{pid}/reconcile`, no modification to existing routes
- reconcile.py: entirely new file
- test_reconcile.py: entirely new file
- 3x .md: entirely new documentation files

No deletions. No renames. No modification of existing logic.

### Combined Level: C-Low

Per SOP S3 matrix: "Run full suite + record false positives"

### Mandatory Rule Check

| Rule | Trigger | Applies? | Action |
|------|---------|:--------:|--------|
| R1 | Scope D (>20 nodes) | No (15 nodes = Scope C) | N/A |
| R2 | Delete/rename in diff | No (all additive) | N/A |
| R3 | explicit+v4 real impact | Yes — L4.15 is real + explicit + v4 | MUST create verification task after commit |
| R4 | Every manual fix | Yes | MUST produce structured audit record |
| R5 | Every manual fix | Yes | MUST demonstrate workflow restore |

---

## Phase 2: PRE-COMMIT VERIFY

### 2.1 False Positive Analysis (per SOP S4)

**Real impact: L4.15 (HTTP routing)**
- This node's scope is HTTP route + middleware. Adding a new route IS within its functional scope.
- Classification: **real**

**False positive: 14 remaining nodes**

Evidence for each (batch — all share identical reasoning):

| Criterion | Satisfied? | Evidence |
|-----------|:----------:|---------|
| E1: Diff is additive only | YES | `git diff --cached` shows only `+` lines in server.py |
| E2: Change outside node's functional scope | YES | New `/api/wf/{pid}/reconcile` handler is unrelated to tokens, lifecycle, context assembly, coverage, artifacts, gatekeeper, memory, or runtime projection |
| E3: Node has other unchanged primary files | YES | 11 of 14 nodes have additional primary files (role_service.py, agent_lifecycle.py, etc.) that are untouched |
| E4: Related tests pass | PENDING | Will verify in step 2.2 |
| E5: Preflight baseline unchanged | YES | Baseline blockers/warnings are pre-existing |

Score: E1+E2+E3+E5 = 4/5 (minimum 3 required) -> **false positive confirmed** (pending E4)

Nodes L9.2 and L8.5 have server.py as sole primary — but E1+E2+E5 = 3/5, still sufficient.

### 2.2 Test Execution

```
[ ] pytest agent/tests/test_reconcile.py     -> (execute)
[ ] pytest agent/tests/ -x                   -> (execute)
```

### 2.3 Dependency Check

L4.15 verify_requires: [] (no upstream dependencies) -> OK

---

## Phase 3: COMMIT

### 3.1 Files to add (explicit, no git add -A)

```
git add agent/governance/server.py
git add agent/governance/reconcile.py
git add agent/tests/test_reconcile.py
git add docs/dev/reconcile-flow-design.md
git add docs/dev/bug-and-fix-backlog.md
git add docs/dev/manual-fix-sop.md
```

Note: NOT adding .claude/worktrees/ (build artifact, not source)

### 3.2 Commit message

```
manual fix: add reconcile feature + governance docs

Reconcile: unified 5-phase flow (SCAN/DIFF/MERGE/SYNC/VERIFY) with two-phase
commit for graph-DB consistency. Replaces manual bootstrap/fix/recovery.

Files:
  server.py (+37): new POST /api/wf/{pid}/reconcile endpoint
  reconcile.py (new): core reconcile implementation (~520 lines)
  test_reconcile.py (new): 27 tests for reconcile phases
  reconcile-flow-design.md (new): design spec v2 + flow diagrams
  bug-and-fix-backlog.md (new): 6 active bugs + 3 design gaps
  manual-fix-sop.md (new): manual fix SOP v2

Affected nodes: L4.15 (real), L5.3/L5.4/L7.1/L7.4/L8.2/L8.4/L8.5/
  L9.1/L9.2/L9.3/L9.5/L9.8/L9.10/L10.8 (false positive, E1+E2+E3+E5)
Bypass reason: dirty workspace blocking all auto_chain dispatch (B1)
```

### 3.3 Do NOT push until Phase 5 passes

---

## Phase 4: POST-COMMIT VERIFY

```
[ ] 4.1 Restart governance service
[ ] 4.2 version_check -> ok=true, dirty=false
[ ] 4.3 preflight_check -> compare against Phase 0 baseline (no new blockers)
[ ] 4.4 wf_impact recheck -> 15 nodes (unchanged)
[ ] 4.5 Create verification task for L4.15 (Rule R3)
```

---

## Phase 5: WORKFLOW RESTORE PROOF

```
[ ] 5.1 Create minimal test task
[ ] 5.2 Observe: queued -> claimed -> succeeded
[ ] 5.3 Observe: auto_chain dispatches next stage (follow-up task exists)
[ ] 5.4 Record: RESTORED or STILL_BROKEN
[ ] 5.5 Disable observer_mode
[ ] 5.6 Write structured audit record
```

---

## Structured Audit Record (to be filled after completion)

```yaml
manual_fix_id:          MF-2026-04-05-001
timestamp:              (after commit)
operator:               observer
trigger_scenario:       dirty_workspace_blocking_chain

bypass_used:            none (this commit resolves the dirty state)

changed_files:
  - agent/governance/server.py (+37, modified, staged)
  - agent/governance/reconcile.py (new, ~520 lines)
  - agent/tests/test_reconcile.py (new, ~340 lines)
  - docs/dev/reconcile-flow-design.md (new, ~1112 lines)
  - docs/dev/bug-and-fix-backlog.md (new)
  - docs/dev/manual-fix-sop.md (new)

classification:
  scope:                C (15 nodes reported)
  danger:               Low (additive only)
  combined_level:       C-Low

reported_impact:        15 nodes
actual_impact:          1 node (L4.15)
false_positive_nodes:   14
false_positive_evidence: E1+E2+E3+E5 (4/5 criteria, min 3 required)

pre_commit_checks:
  - test_reconcile.py: (pending)
  - full suite: (pending)
  - preflight baseline: 1 blocker (pre-existing version), 2 warnings

post_commit_checks:
  - governance restart: (pending)
  - version_check: (pending)
  - preflight delta: (pending)
  - verification task for L4.15: (pending)

workflow_restore_result: (pending)
commit_hash:            (pending)

followup_needed:
  - wf_impact granularity: server.py triggers 15 nodes for any change
  - 56 orphan pending nodes need reconcile or waive
  - 49 unmapped files in coverage check
```
