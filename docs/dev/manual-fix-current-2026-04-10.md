# Manual Fix Execution Record: MF-2026-04-10-001

> SOP: [docs/governance/manual-fix-sop.md](../governance/manual-fix-sop.md)
> Bug: B23 — version_check dirty workspace filter missing docs/dev/ non-governed paths
> Classification: Scope B (1 node: _gate_version_check), Danger High (auto_chain.py modification)

---

## Phase 0: ASSESS

- git status: workspace has only `.claude/worktrees/` dirty (filtered by existing _DIRTY_IGNORE — no blockage)
- Changed file: `aming_claw/agent/governance/auto_chain.py` line 1658
- Bug: `_DIRTY_IGNORE` at auto_chain.py:1658 only contains `.claude/` and `.worktrees/` paths.
  Writing execution records to `docs/dev/` creates dirty files that erroneously block the version gate.
- wf_impact: 1 node affected (_gate_version_check in auto_chain)
- preflight baseline: 15/15 version_gate tests pass, 4/4 version_cache tests pass

## Phase 1: CLASSIFY

- Axis 1: 1 node affected → Scope B
- Axis 2: Modifying auto_chain.py (infrastructure) → High danger
- Final: B-High — requires full test suite + verify node manually
- Scenario: "Fixing auto_chain itself" (chicken-and-egg: docs/dev/ writes dirty workspace that blocks the chain)

## Phase 2: PRE-COMMIT VERIFY

- [x] R7: This execution record created at Phase 0 start
- [x] R9: `docs/dev/` files are intentionally unmapped (non-governed); no nodes needed
- [x] R10: Doc location verified — execution records belong in `docs/dev/` per convention
- [x] Pre-commit test run: 15/15 test_version_gate_round4.py PASSED, 4/4 test_auto_chain_version_cache.py PASSED
- [x] False positive check: No false positives — change is targeted to _DIRTY_IGNORE tuple only

## Phase 3: COMMIT

- File: `agent/governance/auto_chain.py`
- Change: Added `"docs/dev/", "docs/dev\\"` to `_DIRTY_IGNORE` at line 1658
- git commit: `manual fix: B23 add docs/dev/ to _DIRTY_IGNORE in version_check gate`
- Status: PENDING

## Phase 4: POST-COMMIT VERIFY

- Governance service restart: PENDING
- version_check result: PENDING
- preflight delta: PENDING
- Post-commit test run: PENDING

## Phase 5: WORKFLOW RESTORE PROOF

- PENDING

## Phase 6: RECONCILE

- session-status.md update: PENDING
- bug backlog update: PENDING
