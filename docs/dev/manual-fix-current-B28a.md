# Manual Fix Execution Record: B28a

**Date**: 2026-04-11  
**Bug**: B28a — retry dev SCOPE CONSTRAINT 不继承前序 dev changed_files  
**Scope**: B (2 source files)  
**Danger**: Low  
**Fixer**: Observer

---

## Phase 0: ASSESS

**Governance health**: ok=True, version=c206112, queue=0, dirty=False

**Problem**:
- `auto_chain.py:1152-1155` — retry dev `allowed` 集合只读 PM metadata (`target_files`, `test_files`, `doc_impact.files`)
- 前序 dev 修改的文件（如角色文档 `config/roles/dev.yaml`）不在 PM metadata 中
- retry dev 被禁止再次修改这些文件 → `_gate_checkpoint` 反复失败 → 无限循环
- 发现于 chain `task-1775862217-e742de`，retry dev `task-1775869844` 因缺失 `config/roles/dev.yaml` 等

**Fix** (per O1 migration plan Phase 1b):
1. `chain_context.py`: 新增 `get_accumulated_changed_files(chain_id, project_id)` accessor
2. `chain_context.py`: 新增 `get_retry_scope(chain_id, project_id, base_metadata)` accessor
3. `chain_context.py`: `ROLE_RESULT_FIELDS["dev"]` 加入 `"changed_files"`
4. `auto_chain.py:1152-1155`: SCOPE CONSTRAINT 改用 `get_retry_scope()` accessor

**Target files**:
- `agent/governance/chain_context.py`
- `agent/governance/auto_chain.py`
- `agent/tests/test_chain_context.py` (append new class)
- `agent/tests/test_dev_contract_round4.py` (new file — retry_scope tests)

---

## Phase 1: CLASSIFY

- Scope B (2 source files)
- Danger: Low (additive: only extends allowed set, never restricts)
- Rules triggered: none
- No reconcile needed

---

## Phase 2: PRE-COMMIT VERIFY

Pre-fix baseline:
```
pytest agent/tests/test_chain_context.py -v
```

---

## Phase 3: COMMIT

**Changes**:
- `chain_context.py`: ROLE_RESULT_FIELDS dev +changed_files; 2 new accessors
- `auto_chain.py`: SCOPE CONSTRAINT calls get_retry_scope()
- `test_chain_context.py`: +TestGetRetryScope class
- `agent/tests/test_dev_contract_round4.py`: new file with retry_scope tests

---

## Phase 4: POST-COMMIT VERIFY

[ ] Governance version matches HEAD  
[ ] version-check ok  
[ ] full test suite

---

## Phase 5: WORKFLOW RESTORE PROOF

[ ] Queue empty, governance healthy

---

## Phase 6: RECONCILE + R11

[ ] version-sync + version-update  
[ ] version-check ok: true
