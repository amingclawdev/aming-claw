# Manual Fix SOP (Standard Operating Procedure)

> Status: DRAFT v2 — incorporates Codex review feedback (7 suggestions, all adopted)
> Author: Observer
> Date: 2026-04-05
> Scope: Enforceable operating procedure for AI agents performing manual fixes

---

## 1. When Manual Fix Is Required

Manual fixes are **only** permitted in chicken-and-egg deadlock scenarios where the normal Workflow (PM->Dev->Test->QA->Merge) cannot operate:

| Scenario | Why Workflow Cannot Run | Manual Fix Scope |
|----------|------------------------|------------------|
| Dirty workspace blocks all chains | auto_chain gate rejects every dispatch | Commit accumulated code |
| Fixing auto_chain itself | The chain engine is the thing being repaired | Modify auto_chain.py |
| Fixing executor CLI | Dev stage requires executor to run | Modify executor code |
| Governance service won't start | No service = no API = no tasks | Fix server.py startup |

**Bootstrap is NOT a manual fix.** It has its own dedicated flow (`bootstrap_project()`) with separate preconditions and verification. Do not use this SOP for first-time initialization — use the Bootstrap Flow documented in `reconcile-flow-design.md` section 11.1.

**Principle: Manual fixes must be minimal in scope. The sole goal is to restore Workflow operation. All subsequent fixes must return to the normal Workflow chain.**

---

## 2. Manual Fix Flow (6 Phases)

```
┌──────────────────────────────────────────────────────────────────┐
│                       MANUAL FIX FLOW                            │
│                                                                  │
│  Phase 0: ASSESS (read-only, no changes)                         │
│  ┌────────────────────────────────────────────────┐              │
│  │ 0.1  git status                                │              │
│  │      -> identify dirty files                   │              │
│  │ 0.2  wf_impact(changed_files)                  │              │
│  │      -> count affected nodes, verify_level,    │              │
│  │         gate_mode                              │              │
│  │ 0.3  preflight_check                           │              │
│  │      -> capture system integrity baseline      │              │
│  │ 0.4  version_check                             │              │
│  │      -> HEAD vs chain_version vs dirty state   │              │
│  └──────────────────────┬─────────────────────────┘              │
│                         v                                        │
│  Phase 1: CLASSIFY (dual-axis risk assessment)                   │
│  ┌────────────────────────────────────────────────┐              │
│  │ Axis 1 — Affected node count:                  │              │
│  │   0 nodes    -> Scope A                        │              │
│  │   1-5 nodes  -> Scope B                        │              │
│  │   6-20 nodes -> Scope C                        │              │
│  │   >20 nodes  -> Scope D                        │              │
│  │                                                │              │
│  │ Axis 2 — Change danger level:                  │              │
│  │   Low:  new files, new routes, docs, tests     │              │
│  │   Med:  modify normal business logic           │              │
│  │   High: delete/rename, modify executor /       │              │
│  │         auto_chain / governance / version gate  │              │
│  │                                                │              │
│  │ Final level = max(Axis 1, Axis 2)              │              │
│  │ (see S3 matrix for combined rules)             │              │
│  └──────────────────────┬─────────────────────────┘              │
│                         v                                        │
│  Phase 2: PRE-COMMIT VERIFY                                      │
│  ┌────────────────────────────────────────────────┐              │
│  │ 2.1  Execute checks per combined level          │              │
│  │      (see S3 matrix)                           │              │
│  │ 2.2  Record: which nodes affected, which are    │              │
│  │      false positives (with evidence per S4)    │              │
│  │ 2.3  If verify_requires dependencies exist:    │              │
│  │      confirm upstream nodes are verified        │              │
│  │ 2.4  MANDATORY RULES (cannot be skipped):      │              │
│  │      - delete/rename -> reconcile(dry_run)      │              │
│  │      - Scope D -> must split or dry_run         │              │
│  │      - explicit+v4 real impact -> auto-generate │              │
│  │        verification task after commit           │              │
│  └──────────────────────┬─────────────────────────┘              │
│                         v                                        │
│  Phase 3: COMMIT                                                 │
│  ┌────────────────────────────────────────────────┐              │
│  │ 3.1  git add <specific files>                  │              │
│  │      (NEVER git add -A; add files explicitly)  │              │
│  │ 3.2  git commit -m "manual fix: <reason>"      │              │
│  │      Commit message MUST include:              │              │
│  │      - "manual fix:" prefix (for audit trail)  │              │
│  │      - affected node list (real vs false pos.)  │              │
│  │      - bypass reason                           │              │
│  │ 3.3  Do NOT push yet (wait for Phase 5 pass)   │              │
│  └──────────────────────┬─────────────────────────┘              │
│                         v                                        │
│  Phase 4: POST-COMMIT VERIFY                                     │
│  ┌────────────────────────────────────────────────┐              │
│  │ 4.1  Restart governance service                │              │
│  │      (SERVER_VERSION must read new HEAD)        │              │
│  │ 4.2  version_check -> confirm ok=true,          │              │
│  │      dirty=false                               │              │
│  │ 4.3  preflight_check -> compare against         │              │
│  │      Phase 0 baseline                          │              │
│  │      New blockers = regressions -> ABORT        │              │
│  │ 4.4  wf_impact recheck -> confirm affected      │              │
│  │      nodes unchanged                           │              │
│  │ 4.5  If gate_mode=explicit nodes truly affected:│              │
│  │      -> auto-create verification task (MANDATORY)│             │
│  └──────────────────────┬─────────────────────────┘              │
│                         v                                        │
│  Phase 5: WORKFLOW RESTORE PROOF                                 │
│  ┌────────────────────────────────────────────────┐              │
│  │ 5.1  Create a minimal test task via task_create │              │
│  │ 5.2  Observe: does it enter the chain?          │              │
│  │      (status transitions: queued -> claimed)   │              │
│  │ 5.3  Observe: does auto_chain dispatch the      │              │
│  │      next stage after completion?              │              │
│  │      (check task_list for follow-up task)      │              │
│  │ 5.4  Record result: RESTORED or STILL_BROKEN    │              │
│  │      If STILL_BROKEN -> diagnose, do NOT push  │              │
│  │ 5.5  Disable observer_mode if temporarily       │              │
│  │      enabled                                   │              │
│  │ 5.6  Write structured audit record (see S6)     │              │
│  └────────────────────────────────────────────────┘              │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. Dual-Axis Risk Matrix

### Axis 1: Scope (affected node count)

| Scope | Node Count | Meaning |
|-------|-----------|---------|
| A | 0 | No governance nodes affected |
| B | 1-5 | Small, targeted impact |
| C | 6-20 | Broad impact, may include false positives |
| D | >20 | Very broad, almost certainly needs splitting |

### Axis 2: Danger (change type)

| Danger | Change Types | Examples |
|--------|-------------|----------|
| Low | New files, new routes, docs, tests | Adding reconcile.py, adding .md |
| Medium | Modify existing business logic | Changing a handler's response format |
| High | Delete/rename, modify infrastructure | Changing auto_chain.py, executor, version gate, server startup |

### Combined Rules Matrix

```
                    Danger
                    Low           Medium          High
Scope  ┌────────────────┬────────────────┬────────────────┐
  A    │ Commit directly│ Commit directly│ Run related    │
  (0)  │ No extra checks│ No extra checks│ tests first    │
       ├────────────────┼────────────────┼────────────────┤
  B    │ Run module     │ Run module     │ Run full suite │
  (1-5)│ tests          │ tests + verify │ + verify each  │
       │                │ explicit nodes │ node manually  │
       ├────────────────┼────────────────┼────────────────┤
  C    │ Run full suite │ Run full suite │ Run full suite │
  (6-20)│ + record false│ + verify each  │ + MANDATORY    │
       │ positives      │ real node      │ split or       │
       │                │                │ dry_run first  │
       ├────────────────┼────────────────┼────────────────┤
  D    │ MUST split     │ MUST split     │ MUST split     │
  (>20)│ or dry_run     │ + full verify  │ + full verify  │
       │ reconcile      │ after each     │ + observer     │
       │                │ sub-commit     │ approval       │
       └────────────────┴────────────────┴────────────────┘
```

### Mandatory Hard Rules (cannot be overridden)

These rules are **not guidelines**. Violation constitutes a governance breach:

| # | Rule | Trigger | Required Action |
|---|------|---------|-----------------|
| R1 | No direct commit at Scope D | >20 affected nodes | MUST split into sub-commits or run reconcile(dry_run=true) first |
| R2 | Dry-run before delete/rename | Any file deletion or rename in diff | MUST run reconcile_project(dry_run=true) to check for broken refs |
| R3 | Auto-generate verification task | Commit affects gate_mode=explicit + verify_level=4 node (real, not false positive) | MUST create task_create(type=test) targeting that node after commit |
| R4 | Structured audit record | Every manual fix | MUST produce a structured record per template in S6 |
| R5 | Workflow restore proof | Every manual fix | MUST demonstrate auto_chain dispatch works before pushing |

---

## 4. False Positive Evidence Standard

When wf_impact reports a node as affected but the change does not actually impact that node's functionality, it may be classified as a **false positive** — but only with sufficient evidence.

### Minimum Evidence Requirement

A false positive classification requires **at least 3 of the following 5 criteria** to be satisfied:

| # | Criterion | How to Check |
|---|-----------|-------------|
| E1 | Diff is additive only | `git diff --cached` shows only `+` lines, no `-` lines in the relevant file |
| E2 | Change location is outside node's functional scope | The modified lines are not in any function/handler that the node documents |
| E3 | Node has other primary files that are unchanged | Node's `primary` field lists multiple files; others are not in the dirty set |
| E4 | Related tests pass | `pytest agent/tests/test_<module>.py` exits 0 |
| E5 | Preflight baseline unchanged | `preflight_check` shows no new blockers compared to Phase 0 |

### Required Documentation

Every false positive must be recorded with:

```
node_id:              L5.3
classification:       false_positive
evidence_satisfied:   [E1, E3, E4]    (minimum 3)
evidence_details:     "diff is +37 lines (additive only), node primary includes
                       role_service.py which is unchanged, test_server passes"
```

This documentation goes into the commit message and the structured audit record (S6).

---

## 5. Node Dependency Rules

### 5.1 verify_requires Dependency Chains

```
If an affected node has a verify_requires field:

  node_A (verify_requires: [node_B, node_C])
    -> node_B and node_C must have node_state = verified
    -> If upstream is pending -> do not proceed, or assess whether
       upstream needs verification first

How to check:
  wf_impact returns verify_requires in each affected_node entry
  If verify_requires is non-empty, check each dependency's state
```

### 5.2 gate_mode Handling

```
gate_mode=auto:
  -> System verifies automatically; auto_chain handles post-commit
  -> No additional manual action needed

gate_mode=explicit:
  -> Requires explicit verification to pass the gate
  -> If truly affected (not false positive):
     MANDATORY: create verification task after commit (Rule R3)
  -> If false positive (with evidence per S4):
     Record in audit, no verification task needed

gate_mode=skip:
  -> Node skips verification entirely, no action needed
```

### 5.3 Handling Inflated Impact Counts

```
Typical scenario: adding one new route to server.py -> wf_impact reports 15 nodes

Diagnosis:
  1. Review the actual diff: does it only add new code (no modification of existing logic)?
  2. Do the affected nodes' primary fields include other files beyond server.py?
  3. Is the change location within the functional scope of the affected node?

Resolution:
  - Truly affected nodes: verify per gate_mode rules
  - False positives: classify with evidence per S4, record in audit
```

---

## 6. Structured Audit Record

Every manual fix **must** produce a structured audit record in the following format. This record is appended to `docs/dev/bug-and-fix-backlog.md` under a new section `## Manual Fix Audit Log`.

### Template

```yaml
manual_fix_id:          MF-2026-04-05-001
timestamp:              2026-04-05T17:30:00Z
operator:               observer
trigger_scenario:       dirty_workspace_blocking_chain
                        # One of: dirty_workspace_blocking_chain,
                        #         fixing_auto_chain, fixing_executor,
                        #         governance_startup_failure

bypass_used:            skip_version_check
                        # Or: none, observer_merge, reconciliation_lane,
                        #     _DISABLE_VERSION_GATE

changed_files:
  - agent/governance/server.py (+37, modified)
  - agent/governance/reconcile.py (new)
  - agent/tests/test_reconcile.py (new)
  - docs/dev/reconcile-flow-design.md (new)
  - docs/dev/bug-and-fix-backlog.md (new)

classification:
  scope:                C (15 nodes reported)
  danger:               Low (additive only, new route + new files)
  combined_level:       C-Low

reported_impact:
  - L4.15  HTTP routing (real)
  - L5.3   Dual token model (false_positive, E1+E3+E4)
  - L5.4   Agent Lifecycle API (false_positive, E1+E3+E4)
  - L7.1   Context Assembly (false_positive, E1+E3+E4)
  # ... (list all 15)

actual_impact:
  - L4.15  (real, gate_mode=explicit, verify_level=4)

false_positive_nodes:   14
false_positive_reason:  "server.py granularity — only added 1 new route handler,
                         14 other nodes share server.py as primary but their
                         functional scope is unrelated to reconcile endpoint"

pre_commit_checks:
  - pytest test_reconcile.py: PASS (27 tests)
  - pytest agent/tests/ -x: PASS (275 tests)
  - preflight baseline: 0 blockers, 2 warnings

post_commit_checks:
  - governance restart: OK
  - version_check: ok=true, dirty=false
  - preflight delta: 0 new blockers
  - verification task created: task-XXXX for L4.15

workflow_restore_result: RESTORED
  - test task created: task-XXXX
  - status transitions observed: queued -> claimed -> succeeded
  - auto_chain dispatched next stage: YES
  - follow-up task found in task_list: YES

commit_hash:            (filled after commit)
followup_needed:
  - "wf_impact granularity issue: server.py triggers 15 nodes for any change.
     Consider splitting server.py or adding per-function node mapping."
```

### Purpose

This structured record enables:

| Use Case | How |
|----------|-----|
| Frequency analysis | Count manual fixes per trigger_scenario |
| Deadlock hotspot detection | Which modules most often enter deadlock |
| Bypass risk tracking | Which bypass_used types are most common |
| False positive pattern mining | Which nodes are most often false positives -> improve wf_impact |
| Workflow health monitoring | Track workflow_restore_result over time |

---

## 7. Reconcile vs Manual Fix: Responsibility Split

Manual fix and reconcile are **complementary but distinct**. They must not be confused or mixed:

```
┌─────────────────────────┐      ┌─────────────────────────┐
│      MANUAL FIX          │      │       RECONCILE          │
│                          │      │                          │
│ Responsibility:          │      │ Responsibility:          │
│  Freeze code state       │      │  Fix graph / node refs   │
│  (git commit)            │      │  Fix waive lifecycle     │
│                          │      │  Sync DB state           │
│ Operates on:             │      │ Operates on:             │
│  Working tree + git      │      │  Graph.json + governance │
│                          │      │  DB + node_state         │
│ Precondition:            │      │ Precondition:            │
│  Workflow deadlocked     │      │  Code state is frozen    │
│                          │      │  (committed)             │
│ Output:                  │      │ Output:                  │
│  Clean HEAD, dirty=false │      │  Consistent graph + DB   │
│  Workflow unblocked      │      │  ImpactAnalyzer works    │
└────────────┬────────────┘      └────────────┬────────────┘
             │                                 │
             │          CORRECT ORDER          │
             │                                 │
             v                                 v
        1. Manual Fix               2. Reconcile (if needed)
        (commit first)              (fix refs against committed code)

WRONG ORDER:
  reconcile -> commit -> graph drifts again (reconcile was based on stale code)

WRONG USAGE:
  Using reconcile as "fix everything" button (it only fixes graph/DB, not dirty workspace)
  Using manual fix to update graph refs (that is reconcile's job)
```

---

## 8. Common Pitfalls

### Pitfall 1: Forgetting to restart governance after commit

```
Symptom:  version_check shows ok=false; HEAD changed but SERVER_VERSION still old
Cause:    SERVER_VERSION is captured once at process startup, never auto-refreshed
Fix:      Restart governance service
Prevent:  Phase 4 step 1 is always "restart governance"
```

### Pitfall 2: git add -A accidentally staging sensitive files

```
Symptom:  .env, credentials, .claude/worktrees committed to repo
Cause:    git add -A stages everything without discrimination
Prevent:  Always add files explicitly by name; run git diff --cached before commit
```

### Pitfall 3: Manual fix introduces new dirty files

```
Symptom:  version_check still shows dirty=true after commit
Cause:    During the fix process, other files were modified (tests, docs)
Prevent:  Phase 0 records baseline dirty_files; Phase 4 compares
          If new dirty files appeared -> either commit them too, or git checkout to revert
```

### Pitfall 4: auto_chain reports dispatched:true but creates no task

```
Symptom:  task_complete returns {auto_chain: {dispatched: true}} but task_list is empty
Cause:    auto_chain dispatch silently blocked by version gate (B1/B6)
Diagnose: Check version_check.dirty — if still dirty files, gate is still blocking
Prevent:  Ensure ALL files are committed before relying on auto_chain
```

### Pitfall 5: Forgetting to audit version gate bypass

```
Symptom:  Tasks with skip_version_check mixed into normal chain, no audit trail
Cause:    skip_version_check has no access control or logging (B2)
Prevent:  Every bypass MUST be recorded in the structured audit record (S6)
```

### Pitfall 6: Wrong order — reconcile before commit

```
Wrong:  reconcile (fix graph refs) -> commit (introduce new code)
        -> reconcile results overwritten, graph drifts again
Right:  commit (freeze code state) -> reconcile (fix refs against latest code)
See:    S7 for the full responsibility split
```

---

## 9. Worked Example

### Example: Current State (2026-04-05)

```
Dirty files:
  M  agent/governance/server.py       (staged, +37 lines reconcile endpoint)
  ?? agent/governance/reconcile.py     (untracked, new file)
  ?? agent/tests/test_reconcile.py     (untracked, new file)
  ?? docs/dev/reconcile-flow-design.md (untracked, new file)
  ?? docs/dev/bug-and-fix-backlog.md   (untracked, new file)

Phase 0: ASSESS
  $ version_check -> ok=false, dirty=true, dirty_files=["server.py"]
  $ wf_impact(server.py) -> 15 nodes, 13 explicit, 2 auto
  $ wf_impact(reconcile.py) -> 0 nodes (new file, not in any node)
  $ wf_impact(test_reconcile.py) -> 0 nodes
  $ wf_impact(*.md) -> 0 nodes
  $ preflight_check -> baseline: 0 blockers, 2 warnings

Phase 1: CLASSIFY (dual-axis)
  Axis 1 (Scope): 15 reported nodes -> Scope C
  Axis 2 (Danger): all changes are additive (new route, new files) -> Low
  Combined: C-Low -> "Run full suite + record false positives"

  Impact analysis:
  - server.py: only adds 1 new route handler, does not modify existing routes
  - All 15 nodes triggered because their primary field includes server.py
  - Real impact: L4.15 (HTTP routing) — new route is within its scope
  - False positive: remaining 14 — evidence: E1 (additive only) + E3 (other
    primary files unchanged) + E4 (tests pass) = 3/5 criteria met

Phase 2: PRE-COMMIT VERIFY
  [x] Mandatory rule check:
      - R1 (Scope D): N/A, we are Scope C
      - R2 (delete/rename): N/A, no deletions or renames
      - R3 (explicit+v4 real): L4.15 is real + explicit + v4 -> MUST create
        verification task after commit
      - R4 (audit record): will produce after commit
      - R5 (workflow restore proof): will execute in Phase 5
  [x] pytest agent/tests/test_reconcile.py -> 27 tests PASS
  [x] pytest agent/tests/ -x -> 275 tests PASS
  [x] L4.15 verify_requires: [] (no upstream dependencies)
  [x] False positive evidence documented for 14 nodes (E1+E3+E4)

Phase 3: COMMIT
  $ git add agent/governance/server.py
  $ git add agent/governance/reconcile.py
  $ git add agent/tests/test_reconcile.py
  $ git add docs/dev/reconcile-flow-design.md
  $ git add docs/dev/bug-and-fix-backlog.md
  $ git diff --cached   (verify staged files are correct)
  $ git commit -m "manual fix: add reconcile feature (endpoint + core + tests + docs)

  Affected nodes: L4.15 (real), L5.3-L10.8 (false positive, E1+E3+E4)
  Bypass reason: dirty workspace blocking all auto_chain dispatch (B1)
  Files: server.py (+37), reconcile.py (new), test_reconcile.py (new), 2x .md (new)"

Phase 4: POST-COMMIT VERIFY
  $ restart governance
  $ version_check -> ok=true, dirty=false
  $ preflight_check -> compare: 0 blockers (same as baseline), no regression
  $ wf_impact(server.py) -> confirm 15 nodes (unchanged)
  $ task_create type=test for L4.15 verification (Rule R3)

Phase 5: WORKFLOW RESTORE PROOF
  $ task_create type=test "verify auto_chain dispatch works"
  $ Observe: queued -> claimed -> succeeded (state transitions confirmed)
  $ Observe: auto_chain dispatched next stage (follow-up task exists in task_list)
  $ Record: workflow_restore_result = RESTORED
  $ observer_mode(false)
  $ Write structured audit record to bug-and-fix-backlog.md
```

---

## 10. Decision Tree (Quick Reference)

```
Code needs manual commit?
  |
  +-- Can use Workflow? --YES--> Do NOT manually fix. Use PM->Dev->Test->QA->Merge
  |
  +-- Is this a bootstrap? --YES--> Use Bootstrap Flow, not this SOP
  |
  +-- Cannot use Workflow (deadlock / infrastructure failure)
       |
       +-- Phase 0: git status + wf_impact + preflight + version_check
       |
       +-- Classify: Scope (A/B/C/D) x Danger (Low/Med/High)
       |
       +-- Any delete/rename in diff?
       |    +-- YES -> MANDATORY: reconcile(dry_run=true) first (Rule R2)
       |
       +-- Scope D (>20 nodes)?
       |    +-- YES -> MANDATORY: split commit (Rule R1)
       |
       +-- Run checks per matrix level
       |
       +-- Commit with "manual fix:" prefix + node impact + evidence
       |
       +-- Post-commit: restart + version_check + preflight + delta check
       |
       +-- Real impact on explicit+v4 node?
       |    +-- YES -> MANDATORY: create verification task (Rule R3)
       |
       +-- Workflow restore proof: create test task, observe full chain
       |    +-- RESTORED -> push allowed
       |    +-- STILL_BROKEN -> diagnose, do NOT push
       |
       +-- Write structured audit record (Rule R4)
```

---

## 11. Relationship to Other Flows

```
Manual Fix SOP (this document)
  |
  +-- Precondition: Workflow deadlocked (not bootstrap, not normal dev)
  |
  +-- Tools used:
  |   +-- version_check (MCP)
  |   +-- wf_impact (MCP)
  |   +-- preflight_check (MCP)
  |   +-- git (CLI)
  |   +-- reconcile (optional, for post-commit graph repair)
  |
  +-- After completion: Workflow resumes normal operation
  |   +-- All subsequent fixes return to Workflow
  |
  +-- Audit: structured record in bug-and-fix-backlog.md
```

| Flow | When to Use | Relationship |
|------|-------------|--------------|
| Workflow (PM->Dev->...->Merge) | Normal development | Manual fix goal is to restore this |
| Reconcile | Graph node references drifted | Run AFTER manual commit if graph needs repair (see S7) |
| Bootstrap | First-time initialization | Separate flow, do not use this SOP |
| This SOP | Workflow deadlock only | Minimal-scope fix, return to Workflow ASAP |

---

## 12. Codex Review Adoption Log

| # | Suggestion | Adopted | Detail |
|---|-----------|:-------:|--------|
| 1 | Add hard rules instead of soft guidance | **Full** | 5 mandatory rules (R1-R5) in S3, cannot be overridden |
| 2 | Dual-axis classification (scope + danger) | **Full** | Combined matrix in S3, replaces single-axis node count |
| 3 | False positive evidence standard | **Full** | 5 criteria (E1-E5), minimum 3 required, per S4 |
| 4 | Workflow restore proof in post-commit | **Full** | Phase 5 requires observed state transitions + follow-up task existence |
| 5 | Structured audit template | **Full** | YAML template in S6 with 15 fields for analytics |
| 6 | Clearer reconcile relationship | **Full** | S7 responsibility split diagram + correct order + wrong usage examples |
| 7 | Bootstrap boundary clarification | **Full** | Option A adopted: bootstrap removed from manual fix scenarios, explicit exclusion in S1 |
