# Manual Fix Execution Record: B28b

**Date**: 2026-04-11  
**Bug**: B28b — QA executor 无结构化输出校验（缺 recommendation 导致 gate 永久阻断）  
**Scope**: B (2 files, targeted additions)  
**Danger**: Low  
**Fixer**: Observer

---

## Phase 0: ASSESS

**Governance health**: ok=True, version=6a91a38, queue=0, dirty=False

**Problem**:
- `_parse_output()` in `executor_worker.py` returns raw fallback `{"summary":..., "exit_code":...}` when QA agent outputs natural language
- `recommendation=None` → `_gate_qa_pass` silently fails, returns blocked state instead of hard fail
- Chain enters infinite loop: QA→fail→retry dev→checkpoint fail→...

**Root cause**:
- `executor_worker.py:377-392` — `_is_raw_fallback` only checks for terminal CLI errors; no QA-specific output validation
- QA prompt builder (`:1248`) doesn't enforce JSON-only output; if `test_report` empty, agent may output prose

**Fix plan**:
1. `executor_worker.py`: After `_parse_output()` and raw-fallback check, add QA hard validation block:
   - `_is_raw_fallback` → `structured_output_invalid:no_json`
   - `recommendation` missing → `structured_output_invalid:missing_recommendation`
   - `recommendation` not in `{"qa_pass","reject","merge_pass"}` → `structured_output_invalid:invalid_recommendation:{value}`
2. `chain_context.py`: Add `get_latest_test_report(task_id, project_id)` accessor:
   - Memory path: walk stages for latest `test` type with `result_core.test_report`
   - DB fallback: query `chain_events` (TODO:B25-remove)

**Target files**:
- `agent/executor_worker.py` (lines ~390-392 — insert after _is_raw_fallback block)
- `agent/governance/chain_context.py` (after `get_state` at line 314)
- `agent/tests/test_qa_output_validation.py` (new)
- `agent/tests/test_chain_context.py` (append new class)

---

## Phase 1: CLASSIFY

- Scope B (2 source files)
- Danger: Low (fail-fast validation only; no interface change)
- Rules triggered: none (Scope B, Low)
- Governance nodes: `agent.executor` (executor_worker.py primary), `governance.chain_context` (chain_context.py)
- No reconcile needed

---

## Phase 2: PRE-COMMIT VERIFY

Pre-fix baseline:
```
pytest agent/tests/test_qa_output_validation.py  → file does not exist yet
pytest agent/tests/test_chain_context.py -v      → confirm all pass
```

---

## Phase 3: COMMIT

**Changes**:
- `executor_worker.py`: QA hard validation block after _is_raw_fallback
- `chain_context.py`: `get_latest_test_report()` accessor
- `agent/tests/test_qa_output_validation.py`: new test file (4 tests)
- `agent/tests/test_chain_context.py`: append `TestGetLatestTestReport` class (3 tests)

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
