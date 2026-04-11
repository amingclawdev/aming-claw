# Manual Fix Execution Record — B30

> manual_fix_id: MF-2026-04-11-002
> operator: observer
> started: 2026-04-11
> trigger: fixing_auto_chain (B29 side-effect: merge/deploy tasks blocked by version gate)
> bug_id: B30

---

## Phase 0 — ASSESS

**git HEAD**: `bb2f9f6` (Auto-merge: task-1775937851-9af3a1)
**chain_version (DB)**: `e7bf687` (from B28a manual fix sync)
**governance version**: `bb2f9f6` (dynamic HEAD, online)
**active tasks**: 0

**Symptom confirmed**: merge task `task-1775937883-ac1626` completed with `version_check` FAIL:
`chain_version (e7bf687) != git HEAD (bb2f9f6)`. deploy task was never dispatched.

**Root cause**: B29 anchored version gate to DB `chain_version`. But merge itself produces a
new commit (advancing HEAD), and chain_version is only updated by deploy. So merge's own
auto_chain callback runs `_gate_version_check` after the merge commit exists → chain_version
< HEAD → gate blocks → dispatch returns `{gate_blocked: True}` → no deploy task created.

Same issue applies to deploy: if deploy is re-dispatched after another commit, same block.

**Key code**: `auto_chain.py:904-961`
- L905-911: merge invalidates version cache (cosmetic, doesn't fix the check)
- L914: `_gate_version_check` called unconditionally for all task types
- No merge/deploy exemption exists

---

## Phase 1 — CLASSIFY

**Changed files (planned)**:
- `agent/governance/auto_chain.py` — skip version_check for merge and deploy task types

**Affected nodes**: governance.graph (auto_chain.py) — Scope B (1 node)
**Danger**: High (version gate logic in auto_chain.py)
**Combined level**: B-High → run full test suite + verify node manually

---

## Phase 2 — PRE-COMMIT VERIFY

### 2.1 Pre-change test baseline

**Full suite** (pre-change): 993 passed, 5 pre-existing failures, 3 skipped

### 2.2 Fix design

In `on_task_completed()` before calling `_gate_version_check`, add:

```python
# B30: merge produces a new commit advancing HEAD; deploy updates chain_version.
# Both are version-advancing operations — exempting them from version gate prevents
# self-lock. Version gate remains active for pm/dev/test/qa/gatekeeper.
if task_type in ("merge", "deploy"):
    log.debug("auto_chain: version_check skipped for %s task %s (version-advancing op)",
              task_type, task_id)
else:
    # existing version_check block (L914-961)
```

### 2.3 verify_requires: None
### 2.4 Mandatory rules
- R6: No new files → N/A
- R7: This execution record ✓
- R9: auto_chain.py mapped in governance.graph ✓
- R10: docs/dev/ convention ✓

---

## Phase 3 — COMMIT

**Commit hash**: [TO BE FILLED]

---

## Phase 4 — POST-COMMIT VERIFY

[TO BE FILLED]

---

## Phase 5 — WORKFLOW RESTORE PROOF

[TO BE FILLED]

---

## Phase 6 — SESSION STATUS + BACKLOG UPDATE

[TO BE FILLED]
