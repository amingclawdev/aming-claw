# Manual Fix Execution Record: B27

**Date**: 2026-04-11  
**Bug**: B27 — Dev `changed_files` 漏报新建文件  
**Scope**: A (1 file, 1 function)  
**Danger**: Low  
**Fixer**: Observer

---

## Phase 0: ASSESS

**Problem**: `_get_git_changed_files()` in `agent/executor_worker.py:1677` uses two git commands:
1. `git diff --name-only HEAD` — detects modified/deleted tracked files + staged new files are NOT shown here
2. `git diff --name-only --diff-filter=A --cached` — detects staged (added) new files

**Gap**: Untracked new files that have been created but NOT yet staged (`git add`) are missed entirely.
Also: staged new files ARE captured by step 2, so the main gap is unstaged new files.

**Fix**: Add `git ls-files --others --exclude-standard` to capture untracked new files.

**Governance health**: ok=True, version=070f258, queue=0, dirty=False  
**Baseline dirty_files**: []

---

## Phase 1: CLASSIFY

- Scope: A (0 governance nodes affected — `executor_worker.py` maps to `agent.executor` node, but this is a 1-line logic fix with no interface change)
- Danger: Low (adding one subprocess call, no interface change)
- Rules triggered: none (Scope A, Low)
- No reconcile needed

**Target files**: `agent/executor_worker.py`, `agent/tests/test_dev_worktree_round3.py`

---

## Phase 2: PRE-COMMIT VERIFY

Pre-fix test run:
```
pytest agent/tests/test_dev_worktree_round3.py -v
```

---

## Phase 3: COMMIT

**Change**: `_get_git_changed_files` — add third subprocess call: `git ls-files --others --exclude-standard`
**Test update**: `test_git_changed_files_uses_supplied_cwd` — add `proc3` mock for ls-files

---

## Phase 4: POST-COMMIT VERIFY

[ ] Restart governance  
[ ] version-check ok  
[ ] preflight-check delta  
[ ] full test suite

---

## Phase 5: WORKFLOW RESTORE PROOF

[ ] Submit test task, verify chain dispatch

---

## Phase 6: RECONCILE + R11

[ ] version-sync + version-update  
[ ] version-check ok: true
