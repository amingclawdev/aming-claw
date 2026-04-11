# O1 Runtime Context 迁移方案

> 版本: v3.0（评审修订 — temporary bridge + executor 硬校验 + 字段级 accessor）
> 作者: Observer
> 日期: 2026-04-11
> 状态: DRAFT

---

## 1. 问题背景

2026-04-10/11 对 chain `task-1775855010` 和 `task-1775862217-e742de`（B24）的审计揭示多个 context 传播断裂点，根因一致：**各组件各自从 metadata 读上下文，而非从统一的 chain_context 读取，导致阶段间信息丢失和语义不一致。**

### 当前状态

| 阶段 | 状态 | 说明 |
|------|------|------|
| O1-Phase-1a | **DONE** | chain_context 基础设施：`StageSnapshot`、`ChainContextStore`、`ROLE_RESULT_FIELDS`、`recover_from_db` 骨架。`session-status.md` 中「O1 Phase 1 complete」指此。 |
| O1-Phase-1b | **OPEN（本方案 P1）** | scope 继承 / QA context / executor 硬校验，通过 temporary bridge accessor 实现 |
| O1-Phase-2a | **OPEN（本方案 P1.5）** | B25 recovery 可靠性，是 Phase-2b 的前提 |
| O1-Phase-2b | **OPEN（本方案 P2）** | builder 全面迁移，依赖 P1.5 完成 |
| O1-Phase-3 | **OPEN（本方案 P3）** | gate 报错优化 + skip_reason 审计化 |

### 具体症状

| 症状 | Bug | 根因 |
|------|-----|------|
| Retry dev SCOPE CONSTRAINT 不含前序 changed_files，checkpoint 反复失败 | B28a | `auto_chain.py:1146` 只读 PM 静态 metadata |
| QA 输出自然语言，`recommendation=None`，gate 永久阻断 | B28b | metadata 链断裂时 context 为空；executor 层无结构化输出校验 |
| Dev `changed_files` 漏报新建文件，scope 继承不可靠 | B27 | `executor_worker.py:345` 未用 `--diff-filter=A` |
| recovery 后 `_task_to_root` 不完整，后续事件级联丢失 | B25 | `chain_context.py:452` `_recovering=True` 时 `_persist_event` no-op |
| `node_state.updated_by` 为空 | B26 | `_try_verify_update:2645` 调用路径未传 task_id |

**B27 是 B28a 的前提**：先修采集完整性，再修 scope 继承。**B25 是 Phase-2b 的前提**：内存不可信时不可全面迁移。

---

## 2. 目标架构

chain_context 作为链路内 single source of truth，**字段级语义 accessor** 替代 metadata 裸读和通用 `_ctx_get()`。

```
B27: Dev executor 完整采集 changed_files（含 A/M/R）
  ↓
B28a Temporary Bridge: get_retry_scope(chain_id) 封装 DB fallback + 内存路径
  ↓                    B25 修复后 DB fallback 删除
B28b: executor 硬校验 QA 输出（structured_output_invalid）
  ↓    + get_latest_test_report accessor（DB fallback）
B25:  recover_from_db 补扫 + recovery 期间事件排队
  ↓
P2:  字段级 accessor 全面替换（无 _ctx_get()）
  ↓
P3:  gate 消息准确化 + skip_reason 枚举审计
```

**accessor 语义规则**：

| accessor | 语义 | 取值逻辑 |
|----------|------|---------|
| `get_accumulated_changed_files(chain_id, project_id)` | 累积：所有前序 dev 的并集 | 内存 → DB fallback（B25 前） |
| `get_retry_scope(chain_id, project_id, base_metadata)` | 累积：PM target + 前序 dev changed | 组合上述 + metadata |
| `get_latest_test_report(task_id, project_id)` | 最新：created_at DESC 第一条 test | 内存 → DB fallback（B25 前） |
| `get_pm_acceptance_criteria(task_id, project_id)` | 指定来源：只取 PM stage | type=pm StageSnapshot |
| `get_dev_scope_files(task_id, project_id)` | 指定来源：PM 原始声明（不含前序 dev）| PM metadata 直读 |

---

## 3. 迁移步骤

### P1 — B27: Dev changed_files 采集完整性

**目标**：`git diff` 改为同时捕获修改、新建、重命名文件，消除漏报。

**修改**：`agent/executor_worker.py:345-352`（`_get_git_changed_files`）

```python
# 当前：只检测已追踪文件的变更（漏报 untracked added files）
# 改为：
#   git diff --name-only --diff-filter=AMR HEAD   （已 stage 的修改/新增/重命名）
# + git ls-files --others --exclude-standard       （untracked，如有 staging 步骤）
# 合并去重
```

同时确认 worktree 执行路径（`execution_workspace`）与 `--diff-filter` 配合正确。

**验证**：
```bash
pytest agent/tests/test_dev_contract_round4.py -k "new_file" -v
```
新建文件出现在 result `changed_files`。

---

### P1 — B28a: retry dev SCOPE CONSTRAINT 继承（Temporary Bridge）

> **Temporary Bridge 标注**：本节实现 O1-Phase-1b。DB fallback 封在 `chain_context.py` accessor 内，不在 `auto_chain.py` 手写 SQL。Phase-2b（B25 修复后）移除 DB fallback 分支。

**步骤 1：`chain_context.py` 封装两个语义 accessor**

在 `ChainContextStore`（`:116`）新增（**DB fallback 在 accessor 内，不外漏**）：

```python
def get_accumulated_changed_files(self, chain_id: str, project_id: str) -> list[str]:
    """所有前序 dev 任务 changed_files 并集。
    Temporary bridge：内存 miss 时 fallback DB（B25 修复后删 fallback）。"""
    result = set()
    with self._lock:
        chain = self._chains.get(chain_id)
        if chain:
            for stage in chain.stages.values():
                if stage.task_type == "dev" and stage.result_core:
                    result.update(stage.result_core.get("changed_files", []))
            if result:
                return sorted(result)
    # DB fallback（B25 修复前的保障，标记 TODO:B25-remove）
    try:
        from .db import get_connection
        conn = get_connection(project_id)
        rows = conn.execute(
            "SELECT result_json FROM tasks "
            "WHERE chain_id=? AND type='dev' AND status='succeeded'",
            (chain_id,)
        ).fetchall()
        conn.close()
        for row in rows:
            result.update(json.loads(row["result_json"] or "{}").get("changed_files", []))
    except Exception:
        pass
    return sorted(result)

def get_retry_scope(self, chain_id: str, project_id: str, base_metadata: dict) -> set[str]:
    """retry dev 的完整 allowed 文件集 = PM 声明 + 前序所有 dev changed_files。"""
    allowed = set(base_metadata.get("target_files", []))
    allowed.update(base_metadata.get("test_files", []))
    allowed.update((base_metadata.get("doc_impact") or {}).get("files", []))
    allowed.update(self.get_accumulated_changed_files(chain_id, project_id))
    return allowed
```

同时修改 `ROLE_RESULT_FIELDS`（`:44`）dev 条目加入 `"changed_files"`：
```python
"dev": ["target_files", "requirements", "acceptance_criteria",
        "verification", "prd", "changed_files"],  # +changed_files: retry scope
```

**步骤 2：`auto_chain.py:1145-1149` 调用 accessor（删除裸 SQL）**

```python
# 旧（直接读 metadata，裸 SQL）：
allowed = set(metadata.get("target_files", []))
allowed.update(metadata.get("test_files", []))
doc_files = (metadata.get("doc_impact") or {}).get("files", [])
allowed.update(doc_files)

# 新（通过 accessor，DB fallback 封在 chain_context 内）：
from .chain_context import get_store
allowed = get_store().get_retry_scope(_chain_id, project_id, metadata)
if not allowed:
    allowed = set(metadata.get("target_files", []))  # 最终 fallback
```

**验证**：
```bash
pytest agent/tests/test_dev_contract_round4.py -k "retry_scope" -v
```
触发 qa_fail → retry dev，确认 SCOPE CONSTRAINT 含前序 changed_files。

---

### P1 — B28b: QA 结构化输出 + Executor 硬校验

**目标**：QA 必须输出合法 JSON 含 `recommendation`；executor 层拦截非结构化输出，标记 `structured_output_invalid`，不让其流入 gate。

**步骤 1：executor 硬校验（`executor_worker.py:~374`，`_parse_output` 调用后）**

```python
if task_type == "qa":
    rec = result.get("recommendation", "")
    valid_recs = {"qa_pass", "qa_pass_with_fallback", "reject", "rejected"}
    if rec not in valid_recs:
        log.error("QA structured_output_invalid: recommendation=%r keys=%s",
                  rec, list(result.keys()))
        return {
            "status": "failed",
            "error": (
                f"structured_output_invalid: QA recommendation must be one of "
                f"{sorted(valid_recs)}, got {rec!r}. "
                "Check QA prompt — likely non-JSON output from agent."
            ),
            "result": {
                "recommendation": "",
                "error": "structured_output_invalid",
                "raw_summary": result.get("summary", "")[:500],
            },
        }
```

非结构化输出触发 `failed`（不走 gate），executor 重试得到新的 QA 尝试。

**步骤 2：`chain_context.py` 新增 `get_latest_test_report` accessor**

```python
def get_latest_test_report(self, task_id: str, project_id: str) -> dict | None:
    """最近 test 任务的 test_report。
    Temporary bridge：内存 miss 时 fallback DB（B25 修复后删 fallback）。"""
    with self._lock:
        root_id = self._task_to_root.get(task_id)
        chain = self._chains.get(root_id) if root_id else None
        if chain:
            for stage in sorted(chain.stages.values(), key=lambda s: s.ts, reverse=True):
                if stage.task_type == "test" and stage.result_core:
                    tr = stage.result_core.get("test_report")
                    if tr:
                        return tr
    # DB fallback（TODO:B25-remove）
    try:
        from .db import get_connection
        conn = get_connection(project_id)
        root_row = conn.execute(
            "SELECT chain_id FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if root_row and root_row["chain_id"]:
            row = conn.execute(
                "SELECT result_json FROM tasks "
                "WHERE chain_id=? AND type='test' AND status='succeeded' "
                "ORDER BY completed_at DESC LIMIT 1",
                (root_row["chain_id"],)
            ).fetchone()
            conn.close()
            if row:
                return json.loads(row["result_json"] or "{}").get("test_report")
        conn.close()
    except Exception:
        pass
    return None
```

**步骤 3：`executor_worker.py:1248` QA prompt builder**

```python
elif task_type == "qa":
    # test_report 从 chain_context 获取（DB fallback 封在 accessor 内）
    test_report = context.get("test_report", {})
    if not test_report:
        from agent.governance.chain_context import get_store
        test_report = get_store().get_latest_test_report(task_id, project_id) or {}
    # 强化输出格式指令（降低 structured_output_invalid 频率）
    parts.append(
        "You MUST respond ONLY with a JSON object. "
        "Valid recommendations: \"qa_pass\", \"qa_pass_with_fallback\", \"reject\".\n"
        "Required keys: recommendation, review_summary, criteria_results.\n"
        "If context is insufficient: "
        "{\"recommendation\": \"reject\", \"reason\": \"insufficient context\"}\n"
        "Do NOT output plain text or markdown outside JSON."
    )
```

**验证**：
```bash
pytest agent/tests/test_test_contract_round1.py -v
pytest agent/tests/test_qa_context_fallback.py -v   # 新增
```
构造 test_report 为空的 QA 任务，确认 fallback 生效；构造 QA 非 JSON 输出，确认 `structured_output_invalid` 触发 failed。

---

### P1.5 — B25: chain_context recovery 可靠性

> **前提地位**：B25 修复后，Phase-2b accessor 的 DB fallback 分支才可安全移除，chain_context 内存才可信为 single source of truth。

**根因**：`recover_from_db`（`:346`）期间 `_recovering=True`，`_persist_event`（`:452`）静默丢弃新进事件；recovery 后 `_task_to_root` 仅含 chain_events 已记录任务，服务重启窗口内新建任务永久 miss（`:166`：`if not root_id: return` 级联跳过）。

**步骤 1：recovery 完成后从 `tasks` 表补扫活跃 chain**

```python
# chain_context.py，_rebuild_task_to_root_from_db，在 recover_from_db 末尾调用
def _rebuild_task_to_root_from_db(self, project_id: str):
    """补全 _task_to_root：扫 tasks 表中 queued/claimed/running 任务的 chain_id。
    用 chain_id 字段直接定位 root，不依赖 parent_task_id 链条（B25 修复）。"""
    try:
        rows = get_connection(project_id).execute(
            "SELECT task_id, chain_id, type FROM tasks "
            "WHERE status IN ('queued','claimed','running') AND chain_id IS NOT NULL"
        ).fetchall()
    except Exception:
        return
    with self._lock:
        for row in rows:
            if row["task_id"] in self._task_to_root:
                continue
            chain_id = row["chain_id"]
            if chain_id not in self._chains:
                self._chains[chain_id] = ChainContext(chain_id, project_id)
            self._task_to_root[row["task_id"]] = chain_id
            if row["task_id"] not in self._chains[chain_id].stages:
                self._chains[chain_id].stages[row["task_id"]] = StageSnapshot(
                    row["task_id"], row["type"], "", None
                )
```

**步骤 2：recovery 期间事件排队而非丢弃**

```python
# _persist_event:449
def _persist_event(self, ...):
    if self._recovering:
        self._recovery_queue.append((root_task_id, task_id, event_type, payload, project_id))
        return   # 原先直接 return，事件丢失
    # ... 原有 INSERT 逻辑 ...

# recover_from_db 末尾回放
for args in self._recovery_queue:
    self._persist_event(*args)
self._recovery_queue.clear()
self._rebuild_task_to_root_from_db(project_id)
```

**验证**：重启 governance（有活跃任务时），确认 `chain_events` 连续无缺失，`_task_to_root` 覆盖所有 queued/claimed 任务。

---

### P2 — 字段级 accessor + builder 全面迁移

> **前提**：B25 修复完成，chain_context 内存可信。此 phase 移除所有 accessor 内的 `# TODO:B25-remove` DB fallback 分支。

**原则：不用通用 `_ctx_get()`，每个字段有专属语义 accessor，不同字段不同取值语义。**

**新增 accessor（`chain_context.py:116+`）**：

| accessor | 语义 | 取值逻辑（B25 后纯内存）|
|----------|------|----------------------|
| `get_pm_acceptance_criteria(task_id, project_id)` | 指定来源：只取 PM stage | type=pm StageSnapshot |
| `get_dev_scope_files(task_id, project_id)` | 指定来源：PM target_files（不含前序 dev）| PM metadata 直读 |
| `get_accumulated_changed_files_for_test(task_id, project_id)` | 最近 dev 的 changed_files | created_at DESC 第一条 dev stage |

**`executor_worker.py:_build_prompt`（`:996`）各 role 分支改写**：

```python
elif task_type == "dev":
    target = get_store().get_dev_scope_files(task_id, project_id) \
             or context.get("target_files", [])

elif task_type == "test":
    changed = get_store().get_accumulated_changed_files_for_test(task_id, project_id) \
              or context.get("changed_files", [])

elif task_type == "qa":
    test_report = get_store().get_latest_test_report(task_id, project_id) \
                  or context.get("test_report", {})
    changed = get_store().get_accumulated_changed_files(chain_id, project_id) \
              or context.get("changed_files", [])
    criteria = get_store().get_pm_acceptance_criteria(task_id, project_id) \
               or context.get("acceptance_criteria", [])
```

metadata 保留为兼容层 fallback（`or context.get(...)`），不在此 phase 删除。

**验证**：
```bash
pytest agent/tests/ -q --tb=short   # 全量回归，行为不应改变
```

---

### P3 — Gate 改进

**3a — `_gate_qa_pass` 消息优化**（`auto_chain.py:2072`）

P1 硬校验完成后 `recommendation=None` 概率极低，仍需准确消息：

```python
if rec not in ("qa_pass", "qa_pass_with_fallback", "reject", "rejected"):
    return False, (
        f"QA gate: recommendation={rec!r} 无效，"
        f"期望值：qa_pass | qa_pass_with_fallback | reject。"
        f"可能原因：QA executor 输出非 JSON（structured_output_invalid）。"
        f"检查 executor 日志 task_id={task_id}。"
    )
```

**3b — checkpoint skip_reason 枚举化 + 审计**（`auto_chain.py:~1983`）

只接受枚举值，必须来自 `result_json.skip_reasons`，不接受 metadata 传入，写入审计：

```python
# 顶层常量
VALID_SKIP_REASONS = frozenset({
    "file_unchanged_by_design",
    "covered_by_separate_task",
    "doc_not_applicable",
    "already_up_to_date",
})

# _gate_checkpoint 中
skip_reasons = result.get("skip_reasons") or {}  # 只读 result_json
for f in missing_docs:
    reason = skip_reasons.get(f, "")
    if reason in VALID_SKIP_REASONS:
        _audit_skip_reason(conn, project_id, task_id, f, reason)
        log.info("checkpoint: %s skipped, reason=%s", f, reason)
    else:
        still_missing.append(f)
```

skip_reason 写入 `audit_index`（`event="checkpoint.skip_reason"`），可追溯。

**验证**：
```bash
pytest agent/tests/test_checkpoint_gate.py -v
```

---

## 4. 影响文件清单

| 文件 | Phase | 变更内容 | 优先级 |
|------|-------|---------|--------|
| `agent/executor_worker.py:345-352` | P1 B27 | git diff 覆盖 A/M/R | P1 |
| `agent/governance/chain_context.py:44-47` | P1 B28a | ROLE_RESULT_FIELDS dev 加 changed_files | P1 |
| `agent/governance/chain_context.py:116+` | P1 B28a | `get_accumulated_changed_files`、`get_retry_scope` | P1 |
| `agent/governance/auto_chain.py:1145-1149` | P1 B28a | SCOPE CONSTRAINT 改用 accessor（删除裸 SQL）| P1 |
| `agent/executor_worker.py:~374` | P1 B28b | QA 完成后 executor 硬校验（structured_output_invalid）| P1 |
| `agent/governance/chain_context.py:116+` | P1 B28b | `get_latest_test_report` accessor | P1 |
| `agent/executor_worker.py:1248-1276` | P1 B28b | QA prompt builder：accessor fallback + 强格式指令 | P1 |
| `agent/governance/chain_context.py:346-368` | P1.5 B25 | `recover_from_db` + `_rebuild_task_to_root_from_db` | P1.5 |
| `agent/governance/chain_context.py:449-453` | P1.5 B25 | `_persist_event` recovery 期间排队不丢弃 | P1.5 |
| `agent/governance/chain_context.py:116+` | P2 | 全量字段级 accessor；删除 TODO:B25-remove fallback | P2 |
| `agent/executor_worker.py:996-1310` | P2 | `_build_prompt` 各 role 改用字段级 accessor | P2 |
| `agent/governance/auto_chain.py:2072-2082` | P3 | `_gate_qa_pass` 报错消息 | P3 |
| `agent/governance/auto_chain.py:~1983` | P3 | skip_reason 枚举 + 审计写入 | P3 |
| `agent/tests/test_dev_contract_round4.py` | P1 B27 | new_file 测试 | P1 |
| `agent/tests/test_qa_context_fallback.py` | P1 B28b | QA fallback + 硬校验测试（新增）| P1 |
| `agent/tests/test_chain_context_recovery.py` | P1.5 B25 | recovery 可靠性测试（新增）| P1.5 |

---

## 5. Bug Backlog 对应

| Bug | Phase | 状态 | 备注 |
|-----|-------|------|------|
| B22a/b/c — auto_chain dispatch 去重 | 独立 | OPEN | 与 B28b 并行，防 QA failed 扇出 |
| B24 — PM verification.command 语法 | 已修复 | FIXED | B24 链路 deploy 进行中 |
| **B25** — chain_context recovery 不完整 | **P1.5** | OPEN | Phase-2b accessor 内存化的前提 |
| B26 — node_state updated_by 为空 | 独立 | OPEN | `_try_verify_update:2645` |
| **B27** — Dev changed_files 漏报新建文件 | **P1** | OPEN | B28a scope 继承的前提 |
| **B28a** — retry SCOPE CONSTRAINT 丢失 | **P1 Temporary Bridge** | OPEN | Phase-2b 后移除 DB fallback |
| **B28b** — QA context 退化 + 无硬校验 | **P1** | OPEN | executor 硬校验 + accessor fallback |

---

## 6. 风险

| 风险 | 严重度 | 缓解 |
|------|--------|------|
| P1 accessor DB fallback 被高频调用 | 低 | 走 chain_id 索引；可加进程级缓存（TTL 30s）|
| B28b 硬校验致 QA 频繁 failed → B22 扇出 | 中 | 先加 prompt 强化（步骤 3）降低概率；B22 去重同步修 |
| P2 迁移后 prompt token 超限 | 中 | `ROLE_RESULT_FIELDS` 已做字段裁剪；对比 P1/P2 实际长度 |
| P1.5 recovery 队列在服务长时间重启后积压 | 低 | recovery 通常 <1s，队列极短 |
