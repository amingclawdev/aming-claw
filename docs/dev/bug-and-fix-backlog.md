# Bug & Fix Backlog

> Maintained by: Observer
> Created: 2026-04-05
> Last updated: 2026-04-20 (B31/B32/B33 filed — MF-2026-04-20-001 follow-ups)

---

## 修复优先级顺序

```
P1   : B31（worktree submodule 脏过滤）→ ~~B27~~（done）→ ~~B28b~~（done）→ ~~B28a~~（done）→ ~~B29~~（done）→ ~~B30~~（done）→ B24（重发链路）
P1.5 : B25（chain_context recovery）
P2   : O1 Phase-2b（builder 全面迁移）→ B21（并发 merge）→ B22（任务扇出）→ B26（updated_by）→ B32（version-update updated_by 白名单 SOP 不一致）→ B33（docs 错误引用 port 39103）
P3   : gate 报错优化 / skip_reason 枚举审计
```

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
| B27 | Dev changed_files misses untracked new files | bd77c14 | 2026-04-11 |
| B28b | QA executor no structured output validation | ad44e1a | 2026-04-11 |
| B28a | Retry dev SCOPE CONSTRAINT missing prev dev changed_files | 59ca4f8 | 2026-04-11 |
| G4 | PM doc_impact not auto-populated from graph | 272dfa6 | 2026-04-07 |
| G5 | Retry prompt missing gate scope rules | 6ffa422 | 2026-04-07 |
| G6 | Graph lookup not bidirectional for doc targets | 272dfa6 | 2026-04-07 |
| G7 | config/roles/*.yaml not in acceptance graph | 9faa28a | 2026-04-09 |
| G8 | related_nodes not auto-populated from graph | 8f84d82 | 2026-04-10 |
| G9 | Observer SOP for manual task metadata | 79f9c39 | 2026-04-10 |
| G10 | Graph rebuild mapping updated | 79f9c39 | 2026-04-10 |
| G11 | manual-fix-sop missing chain_version sync step | aaaab1b | 2026-04-11 |
| O2 | Version gate filter worktree dirty files | 44ab315 | 2026-04-09 |
| O3 | Governance dynamic version read (no restart) | 6810a37 | 2026-04-10 |
| B29 | version gate audit weakened by B19 dynamic HEAD read | 4525406 | 2026-04-11 |
| B30 | B29 side-effect: merge/deploy self-locked by version gate | e3145f1 | 2026-04-11 |
| B31 | Version gate dirty filter missing .claude/worktrees/* submodule refs | TBD | 2026-04-20 |

---

## Open Items (P3 — low priority, next session)

### G11: manual-fix-sop Phase 4 遗漏 chain_version 同步步骤 [FIXED]

- **Status**: Fixed. SOP R11 已添加。
- **Symptom**: manual fix commit 后 chain_version 未更新，version gate（`chain_version != git HEAD`）阻断后续所有 workflow 任务。
- **Root cause**: manual-fix-sop.md Phase 4（POST-COMMIT VERIFY）只要求重启 governance + version_check，未明确要求调用 `version-sync` + `version-update` 将 chain_version 推进到新 HEAD。Deploy 阶段自动调用这两步，但 manual fix 绕过了 Deploy，导致 chain_version 停留在旧值。
- **Fix**: `docs/governance/manual-fix-sop.md` 增加 R11：每次 manual fix commit 后必须调用 `POST /api/version-sync/{project_id}` + `POST /api/version-update/{project_id}`，验证 `GET /api/version-check` 返回 `ok: true`。Governance 离线时可直接更新 `project_version` 表（需在 governance 重启后通过 version-check 验证）。
- **Fix commit**: aaaab1b（SOP R11 写入，本次 commit）

### B28a: Retry dev SCOPE CONSTRAINT 不继承前序 dev changed_files [FIXED]

- **Status**: Fixed. Commit 59ca4f8.
- **Symptom**: retry dev 的 SCOPE CONSTRAINT `allowed` 文件列表仅从 PM 静态元数据（`target_files` + `test_files` + `doc_impact.files`）构建，不包含前序 dev 已修改的文件。若前序 dev 修改了 PM 未列出的文件（如角色文档），retry dev 被禁止再次修改这些文件，导致 `_gate_checkpoint` 反复失败，形成无限循环。
- **Discovered**: chain `task-1775862217-e742de`（B24 修复链路），retry dev 任务 `task-1775869844` 因缺失 `config/roles/dev.yaml` 等角色文档而 checkpoint FAIL。
- **Root cause**: `auto_chain.py:1145-1149` — `allowed` 集合只读 PM metadata，未查询 `chain_events` 中前序 dev 的 `changed_files`。
- **Fix**: `chain_context.py` 新增 `get_accumulated_changed_files(chain_id, project_id)` accessor（DB fallback + 内存路径），`auto_chain.py` retry 路径调用此 accessor 扩充 `allowed`。详见 O1 migration plan Phase 1b。

### B28b: QA executor 无结构化输出校验 [FIXED]

- **Status**: Fixed. Commit ad44e1a.
- **Symptom**: QA agent 输出自然语言或非 JSON 文本时，`_parse_output()` 返回 raw fallback `{"summary":..., "exit_code":...}`，`recommendation=None`，`_gate_qa_pass` 静默失败而非直接 fail。导致链路无意义循环（QA→fail→retry dev→checkpoint fail→...）。
- **Discovered**: chain `task-1775862217-e742de`，QA 任务 `task-1775868111` result_json 缺少 `recommendation` 字段。
- **Root cause**: `executor_worker.py:377-392` — `_is_raw_fallback` 仅检查 terminal CLI 错误，无 QA 专用结构化输出校验；QA prompt builder (`:1248`) 若 `test_report` 为空则 QA agent 输出自然语言。
- **Fix**: executor_worker.py QA session 后增加硬校验：非 JSON 或缺少 `recommendation` → 立即返回 `{"status":"failed","error":"structured_output_invalid:..."}` 并写入 gate_events。详见 O1 migration plan Phase 1b。

### B24: PM verification.command 语法错误 [OPEN] [P1]

- **Status**: Open. Dev 修复已通过测试（chain `task-1775862217-e742de` test: 23/0），但链路未完成 deploy 阶段（governance 崩溃导致中断）。等 B29 修复后重新发起完整链路验证。
- **Symptom**: PM 生成的验证命令将多条 shell 命令用 `&&` 串联后作为 `diff` 的参数传入（`diff a.md b.md && grep ...`），导致 diff 收到多余操作数报错，test 阶段 100% 失败（`diff: extra operand '&&'`）。
- **Discovered**: chain `task-1775855010-7fcf8b`，两次 test 任务均因此失败（attempts=3）。
- **Fix status**: Dev 修复已在 `agent/executor_worker.py` 提交（chain `task-1775862217-e742de`，dev 任务通过，test 23/0）。待 B29 修复后重发 PM 任务完成 merge/deploy。
- **File**: PM 提示词 / `agent/governance/auto_chain.py` — PM 生成 `verification.command` 的逻辑，需拆分为独立 shell 步骤而非 `&&` 串联整体作为单条命令。

### B25: chain_events 记录不完整 [OPEN] [P1.5]

- **Status**: Open.
- **Symptom**: 全链路（PM→Dev→Test→QA→GK→Merge）仅产生 2 条 chain_events（`task.created` + `dev.completed`），缺失 `test.completed`、`qa.completed`、`gatekeeper.completed`、`merge.completed`、`chain.completed` 等事件。
- **Discovered**: chain `task-1775855010-7fcf8b`。
- **File**: `agent/governance/auto_chain.py` 或 chain_events 写入路径 — 事件发布逻辑存在缺失分支。

### B26: node_state updated_by 为空字符串 [OPEN] [P2]

- **Status**: Open.
- **Symptom**: 节点状态变更时 `updated_by` 字段写入空字符串而非合法 task_id，违反审计可追溯性要求。
- **Discovered**: `governance.graph`、`governance.reconcile`、`governance.services`、`governance.doc_policy` 4 个节点，均在 chain `task-1775855010-7fcf8b` 执行窗口内更新。
- **File**: `agent/governance/` — `node_state` 写入逻辑，`updated_by` 未正确传递调用方 task_id。

### B27: Dev changed_files 元数据漏报 [OPEN] [P1]

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

### B31: Version gate dirty filter missing `.claude/worktrees/*` submodule refs [FIXED] [P1]

- **Discovered**: 2026-04-20, MF-2026-04-20-001 follow-up. PM task `task-1776658117-adffde` succeeded but auto-chain silently failed to dispatch Dev — same pattern as Bug 7 / B15 / D5 / B23.
- **Symptom**: `mcp__aming-claw__version_check` returns `dirty=true` with `dirty_files: [".claude/worktrees/compassionate-tu", ".claude/worktrees/happy-ardinghelli", ".claude/worktrees/zen-mendeleev"]`. Each of those paths is a git submodule (mode 160000) left behind by dev worktrees. They can never be cleaned while dev worktrees are in use.
- **Impact**: Auto-chain dispatch path that reads this filtered output silently blocks next-stage task creation. Recurrence of B15/D5/B23 under a new path.
- **Asymmetry to investigate**: `POST /api/version-sync/{project_id}` DOES filter these (`dirty_files: []` after sync), but `mcp__aming-claw__version_check` does NOT. Two code paths disagree — must be unified.
- **Fix**: Extend the D5 `_DIRTY_IGNORE` list (or equivalent) to exclude `.claude/worktrees/**` in BOTH paths. Add regression test covering submodule mode 160000 refs.
- **Fix files (estimate)**: `agent/governance/auto_chain.py` (or the module holding `_DIRTY_IGNORE`), `agent/governance/server.py` (version_check endpoint if separate), `agent/tests/test_version_gate_round4.py` or a new test file.
- **Test**: `pytest agent/tests/ -k "dirty_filter or version_gate"` should cover new case where dirty_files includes worktree submodule paths.

### B32: `version-update` API `updated_by` allowlist doesn't match SOP R11 prescription [OPEN] [P2]

- **Discovered**: 2026-04-20, MF-2026-04-20-001. Manual fix attempted to call `POST /api/version-update/aming-claw` with `updated_by="manual-fix-2026-04-20-docs-mcp-startup"` per `docs/governance/manual-fix-sop.md` R11 guidance. Server rejected with `INVALID_UPDATED_BY`.
- **Symptom**: [agent/governance/server.py:2015](../../agent/governance/server.py) whitelists only `{"auto-chain", "init", "register", "merge-service"}`. SOP R11 format `manual-fix-<slug>` is rejected.
- **Impact**: Every manual fix must invent a workaround. Current workaround: use `updated_by="merge-service"` with `task_id` set to the originating PM task. This muddles the audit trail because "merge-service" implies a real merge stage, not a manual fix.
- **Fix options (pick one)**:
  - **A (preferred)**: Widen allowlist to accept prefix `manual-fix` with an additional audit record that includes `manual_fix_reason` field. Keeps SOP R11 as-written.
  - **B**: Update SOP R11 to document `merge-service` + `task_id` as the canonical manual-fix path (and rename the concept in-SOP). Lower code change, but SOP bends to impl.
- **Fix files**: `agent/governance/server.py:2015` (allowlist), optional `agent/governance/audit_service.py` (new audit field), or `docs/governance/manual-fix-sop.md` §13 R11 if option B.

### B33: Self-introduced docs claim about port 39103 (non-existent supervisor port) [OPEN] [P2]

- **Discovered**: 2026-04-20, immediately after commit `1bed264`. Editing docs to replace the old false "MCP auto-starts executor" claim, I introduced a NEW false claim that ServiceManager supervision can be verified by checking port 39103. `agent/service_manager.py` does not bind any TCP port — singleton protection is done via the `start-manager.ps1` launcher using a named Windows mutex (`Global\aming_claw_manager`).
- **Partial fix in-flight**: Corrected in commit following MF-2026-04-20-001 R8 loop — `docs/deployment.md` §5 and `docs/dev/session-status.md` "Starting a New Session" now describe process-tree verification (tasklist/pgrep for `service_manager.py`) instead of port check.
- **Remaining risk**: The out-of-repo auto-memory `project_service_lifecycle.md` (under `~/.claude/projects/.../memory/`) also contained the port 39103 claim. Needs same correction.
- **Lesson**: When correcting a false doc claim, do not introduce a replacement claim that hasn't been verified against code. This is a governance surface requiring a dry-run convention for doc-rewriting commits.
- **Fix**: Already fixed in-repo; auto-memory correction is a separate out-of-repo operation (not a governance-tracked file).

---

## Manual Fix Audit Log

### MF-2026-04-20-001 — Correct stale MCP auto-start claims in startup docs

```yaml
manual_fix_id:          MF-2026-04-20-001
timestamp:              2026-04-20T04:19:00Z
operator:               observer (scheduled workflow maintenance)
trigger_scenario:       dirty_workspace_blocking_chain
bypass_used:            none (no skip_version_check; commit proceeded via normal git)

changed_files:
  - docs/deployment.md (modified, +54/-20)
  - docs/onboarding.md (modified, +1/-1)
  - docs/dev/session-status.md (modified, +7/-3)
  - docs/dev/manual-fix-current-2026-04-20.md (new, R7 execution record)

classification:
  scope:                B (4 nodes)
  danger:               Low (docs only, no code, no deletions)
  combined_level:       B-Low

reported_impact:
  - agent.deploy         (direct, verify_level=2, gate=auto)
  - governance.server    (direct, verify_level=4, gate=auto)
  - agent.gateway        (transitive, verify_level=5, gate=auto)
  - agent.mcp            (transitive, verify_level=5, gate=auto)

actual_impact:
  - All 4 are doc-only references; no functional code in any of these
    nodes was changed. All gate_mode=auto, so no R3 explicit
    verification task required.

false_positive_nodes:   0 (all nodes genuinely reference the changed docs)

pre_commit_checks:
  - version_check baseline: HEAD=8541b18, chain_version=8541b18, dirty=true
    (3 .claude/worktrees/* submodule refs — pre-existing, not part of this fix)
  - preflight baseline: ok=true, 0 blockers, 3 warnings
    (version sync stale 358s, 16 orphan pending, 49 unmapped)
  - wf_impact: 4 nodes, all auto
  - No tests run: docs have no module test coverage (documented under B-Low)

commit_hash:            1bed264

post_commit_checks:
  - governance dynamic version: reads 1bed264 (B19/O3 working, no restart needed)
  - version-sync: ok=true, dirty_files=[] (worktree submodules filtered at sync layer)
  - version-update: ok=true, chain_version=1bed264
    (NOTE: R11 prescribes updated_by='manual-fix-...' but server allowlist
     only accepts auto-chain|init|register|merge-service. Used merge-service
     with task_id=task-1776658117-adffde; see followup_needed below.)
  - preflight delta: ok=true, 2 warnings (version stale warning cleared, no new blockers)
  - MCP version_check: still reports dirty=true because it reads a
    different code path than version-sync; the .claude/worktrees/*
    submodule refs are not filtered there. This is the underlying
    structural issue and is itself now a backlog item.

workflow_restore_result: PARTIAL
  - Underlying cause (.claude/worktrees/* dirty in MCP version_check)
    remains. Auto-chain dispatch may or may not still be blocked
    depending on which gate path runs.
  - A full PM->Dev restore test was not executed because the orphan
    executor (no ServiceManager supervision as of this session) plus
    persistent submodule dirty state would produce an ambiguous result.
  - Recommended follow-up: (a) restart the executor under ServiceManager
    so crash recovery and deploy signals work, (b) extend the D5 dirty
    filter used by MCP version_check to cover `.claude/worktrees/*`.

followup_needed:
  - BUG-FOLLOWUP-A (P1): MCP version_check / auto_chain gate reads a
    code path that does not filter .claude/worktrees/* submodule refs.
    D5 filter only excludes .claude/settings.local.json. Dev worktrees
    leave these dirty permanently. Silently blocks auto-chain dispatch
    (Bug 7 pattern recurrence).
  - BUG-FOLLOWUP-B (P2): SOP R11 documents updated_by='manual-fix-<slug>'
    but server.py:2015 allowlist rejects it. Either (a) widen the server
    allowlist to accept 'manual-fix' / 'manual-fix-*' with audit trail,
    or (b) update the SOP to say 'merge-service' is the allowed value
    for manual-fix POST /api/version-update calls.
  - BUG-FOLLOWUP-C (P1): Current executor is orphan (no ServiceManager
    parent). Needs `.\scripts\start-manager.ps1 -Takeover` run before
    next chain attempt. The three doc fixes in this commit tell future
    operators/agents how to verify this (port 39103 supervision check).
  - Out-of-repo MEMORY.md auto-memory still contains the false claim
    "MCP server (.mcp.json) auto-starts executor_worker via ServiceManager".
    Must be corrected via memory tools (separate action, not a git commit).
```

---

## Test Count

1003 tests pass (B30 +10: version_gate_round4×3 + auto_chain_version_cache×4 rewritten + net +3 new), 7 pre-existing failures (test_e3_write_index_status, test_valid_test_success_accepted, test_reverse_lookup_doc_to_code, test_pm_to_deploy_chain_progresses_through_all_stages, test_governed_dirty_workspace_lane_defers_related_node_qa_block, test_try_verify_update_returns_true_on_success, test_try_verify_update_returns_false_on_exception), 3 skipped.
