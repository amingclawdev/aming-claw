# Bug & Fix Backlog

> Maintained by: Observer
> Created: 2026-04-05
> Last updated: 2026-04-10 (B22 fix directions B22a/B22b/B22c added)

---

## Status Legend

| Tag | Meaning |
|-----|---------|
| `OPEN` | Confirmed, not yet fixed |
| `FIXED` | Fix committed to main |
| `WONTFIX` | By design or deferred indefinitely |

---

## Fixed Bugs

| ID | Description | Fix Commit | Date |
|----|-------------|------------|------|
| D1 | Executor stops claiming after initial batch | e9506c0 | 2026-03-31 |
| D2 | PM max_turns=10 instead of 60 | 5b09ad0 | 2026-03-31 |
| D3 | SERVER_VERSION blocks auto_chain after merge | 942b5de | 2026-03-31 |
| D4 | Duplicate retry task creation | 7d96c74 | 2026-03-31 |
| D5 | Dirty workspace gate blocks auto_chain (.claude/ paths) | 1ea497f | 2026-03-31 |
| D6 | Merge task fails without _branch/_worktree metadata | 20baea3 | 2026-03-31 |
| D7 | Coordinator duplicate reply | c931792 | 2026-03-31 |
| B1/B6 | auto_chain dispatch silently fails / reports dispatched:true | 8652f51 | 2026-04-05 |
| B2 | skip_version_check no access control or audit | efd7740 | 2026-04-05 |
| B3 | Version gate only at dispatch, not task_create | abc9795 | 2026-04-05 |
| B4 | Executor CLI hangs on dev/qa tasks | dd5d940 | 2026-04-05 |
| B5 | DB lock on task_complete (intermittent) | a413b9d | 2026-04-05 |
| B7 | Deploy restart silent fail | ac873e9 | 2026-04-05 |
| B8 | _gate_checkpoint blocks docs/dev/ as unrelated | 1f080bf | 2026-04-07 |
| B9 | Gate retry prompt lacks test failure detail | 6ffa422 | 2026-04-07 |
| B10 | Executor worktree fallback contaminates main tree | 3ffe09a | 2026-04-07 |
| B11 | ServiceManager does not consume restart signal | eff196f | 2026-04-08 |
| B12 | KeyError 'reason' in executor run_once after task_complete | ee9d9bb | 2026-04-09 |
| B13 | Dead tester.yaml + ungoverned YAML configs (G7 combined) | 9faa28a | 2026-04-09 |
| B14 | Claude CLI gets empty stdin — communicate() missing input= | d71baa6 | 2026-04-09 |
| B15 | Version gate blocks on worktree dirty files | 44ab315 | 2026-04-09 |
| B16 | No retry for version gate blocks (transient dirty) | 8f84d82 | 2026-04-10 |
| B17 | task.completed event publishes after version gate | 8f84d82 | 2026-04-10 |
| B18 | API task_create missing task.created event | 0235786 | 2026-04-10 |
| B19 | Governance version stale after commits | 6810a37 | 2026-04-10 |
| B20 | Clean staged/untracked leaks before merge | 2bd20f9 | 2026-04-10 |
| B23 | version_check dirty filter missing docs/dev/ non-governed path | 1d66aa5 | 2026-04-10 |
| G4 | PM doc_impact not auto-populated from graph | 272dfa6 | 2026-04-07 |
| G5 | Retry prompt missing gate scope rules | 6ffa422 | 2026-04-07 |
| G6 | Graph lookup not bidirectional for doc targets | 272dfa6 | 2026-04-07 |
| G7 | config/roles/*.yaml not in acceptance graph | 9faa28a | 2026-04-09 |
| G8 | related_nodes not auto-populated from graph | 8f84d82 | 2026-04-10 |
| G9 | Observer SOP for manual task metadata | 79f9c39 | 2026-04-10 |
| G10 | Graph rebuild mapping updated | 79f9c39 | 2026-04-10 |
| O2 | Version gate filter worktree dirty files | 44ab315 | 2026-04-09 |
| O3 | Governance dynamic version read (no restart) | 6810a37 | 2026-04-10 |

---

## Open Items (P3 — low priority, next session)

### B28a: Retry dev SCOPE CONSTRAINT 不继承前序 dev changed_files [OPEN] [P1]

- **Status**: Open.
- **Symptom**: retry dev 的 SCOPE CONSTRAINT `allowed` 文件列表仅从 PM 静态元数据（`target_files` + `test_files` + `doc_impact.files`）构建，不包含前序 dev 已修改的文件。若前序 dev 修改了 PM 未列出的文件（如角色文档），retry dev 被禁止再次修改这些文件，导致 `_gate_checkpoint` 反复失败，形成无限循环。
- **Discovered**: chain `task-1775862217-e742de`（B24 修复链路），retry dev 任务 `task-1775869844` 因缺失 `config/roles/dev.yaml` 等角色文档而 checkpoint FAIL。
- **Root cause**: `auto_chain.py:1145-1149` — `allowed` 集合只读 PM metadata，未查询 `chain_events` 中前序 dev 的 `changed_files`。
- **Fix**: `chain_context.py` 新增 `get_accumulated_changed_files(chain_id, project_id)` accessor（DB fallback + 内存路径），`auto_chain.py` retry 路径调用此 accessor 扩充 `allowed`。详见 O1 migration plan Phase 1b。

### B28b: QA executor 无结构化输出校验 [OPEN] [P1]

- **Status**: Open.
- **Symptom**: QA agent 输出自然语言或非 JSON 文本时，`_parse_output()` 返回 raw fallback `{"summary":..., "exit_code":...}`，`recommendation=None`，`_gate_qa_pass` 静默失败而非直接 fail。导致链路无意义循环（QA→fail→retry dev→checkpoint fail→...）。
- **Discovered**: chain `task-1775862217-e742de`，QA 任务 `task-1775868111` result_json 缺少 `recommendation` 字段。
- **Root cause**: `executor_worker.py:377-392` — `_is_raw_fallback` 仅检查 terminal CLI 错误，无 QA 专用结构化输出校验；QA prompt builder (`:1248`) 若 `test_report` 为空则 QA agent 输出自然语言。
- **Fix**: executor_worker.py QA session 后增加硬校验：非 JSON 或缺少 `recommendation` → 立即返回 `{"status":"failed","error":"structured_output_invalid:..."}` 并写入 gate_events。详见 O1 migration plan Phase 1b。

### B24: PM verification.command 语法错误 [OPEN] [P1]

- **Status**: Open.
- **Symptom**: PM 生成的验证命令将多条 shell 命令用 `&&` 串联后作为 `diff` 的参数传入（`diff a.md b.md && grep ...`），导致 diff 收到多余操作数报错，test 阶段 100% 失败（`diff: extra operand '&&'`）。
- **Discovered**: chain `task-1775855010-7fcf8b`，两次 test 任务均因此失败（attempts=3）。
- **File**: PM 提示词 / `agent/governance/auto_chain.py` — PM 生成 `verification.command` 的逻辑，需拆分为独立 shell 步骤而非 `&&` 串联整体作为单条命令。

### B25: chain_events 记录不完整 [OPEN] [P2]

- **Status**: Open.
- **Symptom**: 全链路（PM→Dev→Test→QA→GK→Merge）仅产生 2 条 chain_events（`task.created` + `dev.completed`），缺失 `test.completed`、`qa.completed`、`gatekeeper.completed`、`merge.completed`、`chain.completed` 等事件。
- **Discovered**: chain `task-1775855010-7fcf8b`。
- **File**: `agent/governance/auto_chain.py` 或 chain_events 写入路径 — 事件发布逻辑存在缺失分支。

### B26: node_state updated_by 为空字符串 [OPEN] [P2]

- **Status**: Open.
- **Symptom**: 节点状态变更时 `updated_by` 字段写入空字符串而非合法 task_id，违反审计可追溯性要求。
- **Discovered**: `governance.graph`、`governance.reconcile`、`governance.services`、`governance.doc_policy` 4 个节点，均在 chain `task-1775855010-7fcf8b` 执行窗口内更新。
- **File**: `agent/governance/` — `node_state` 写入逻辑，`updated_by` 未正确传递调用方 task_id。

### B27: Dev changed_files 元数据漏报 [OPEN] [P2]

- **Status**: Open.
- **Symptom**: Dev 创建了 `docs/governance/audit-process.md` 但未将其加入 `result_json.changed_files` 声明，导致 `_gate_checkpoint` 误判该文件"未更新"并阻断链路，需 observer bypass 才能继续。
- **Discovered**: chain `task-1775855010-7fcf8b`，dev 任务 `task-1775855091-2e761b` 和 `task-1775857046-400dca` 均漏报。
- **File**: Dev executor 提示词 / `agent/governance/auto_chain.py` — Dev 完成后收集 `changed_files` 的逻辑，需包含新建文件。

### B21: 并发 merge 竞争 [OPEN] [P2]

- **Status**: Open. Idempotent guard catches it, but race window exists.
- **Symptom**: 多个 executor 同时尝试 ff-only merge main，首次失败需重试。幂等守卫兜住但有竞争窗口。
- **Discovered**: chain task-1775801122-39f7dc, task-1775801420
- **File**: `agent/governance/merge.py` (推测) — merge 幂等锁机制

### B22: 任务扇出 bug [OPEN] [P2]

- **Status**: Open. Extra tasks complete safely in replay mode but waste resources. Root cause confirmed 2026-04-10.
- **Symptom**: dispatcher 对下游任务（merge/gatekeeper/deploy/qa）重复派发，预期各 1 个但实际产生多个。auto-chain 创建的 PM/Dev 任务也出现重复链路（B22 扇出），今日 queue 中观察到同一 chain 内多个同 type 任务并存。
- **Discovered**: chain task-1775801122-39f7dc（原始发现）；chain task-1775855702-7e72b9 等多条链路（2026-04-10 再现）
- **File**: `agent/governance/auto_chain.py` — dispatch 去重逻辑；`agent/governance/conflict_rules.py`；`agent/governance/server.py`
- **Fix directions** (3 sub-items):
  - **B22a** — `auto_chain.py` dispatch 去重：派发下游任务前查询 `WHERE chain_id=? AND type=? AND status IN ('queued','claimed')`，已存在则跳过派发，不重复创建
  - **B22b** — `conflict_rules.py` Rule 2 实现补全：补充 same-file + same-operation → `duplicate` 分支（当前 `_check_file_conflict` 只处理 `OPPOSITE_OPS`，同操作重叠未实现）
  - **B22c** — `server.py:1596` auto-chain 冲突检测豁免范围收窄：当前 `created_by not in ("auto-chain", "auto-chain-retry")` 完全豁免所有 auto-chain 任务，至少同 `chain_id + type` 重复应触发检测

### O1: Consolidate runtime context as single source of truth [OPEN] [P3]

- **Status**: Phase 1 complete (B17+B18 fixed events flow). Phase 2-3 remaining.
- **Phase 2**: Builder functions read from chain_context with metadata fallback.
- **Phase 3**: Remove metadata propagation (`{**metadata}`) from builders.
- **Effort**: Medium. Not blocking — metadata propagation works as primary path.
- **File**: `agent/governance/auto_chain.py`, `agent/governance/chain_context.py`

### G1: Dirty-workspace root cause classification [OPEN] [P3]

- Gate blocks on dirty but doesn't classify why (worktree vs staged vs stale).
- Low priority — B15 already filters the main false positive source.

### G2: Pre-flight advisory at task_create [OPEN] [P3]

- Manual task_create has no dirty-workspace warning. Low priority.

### G3: Chain context bypass tracking [OPEN] [P3]

- No audit trail for gate bypass flags. Low priority.

### Stale docs (minor) [OPEN] [P3]

- `docs/roles/*.md` (coordinator, dev, qa, pm) — minor behavioral notes from B10/B12.
- Low priority — core docs (auto-chain.md, executor-api.md, tester.md, manual-fix-sop.md) are current.

---

## Test Count

963 tests pass, 2 pre-existing failures (test_e3_write_index_status, test_valid_test_success_accepted).
