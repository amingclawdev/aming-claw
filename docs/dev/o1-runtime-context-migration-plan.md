# O1 Runtime Context 迁移方案

> 版本: v2.0（评审修订）
> 作者: Observer
> 日期: 2026-04-11
> 状态: DRAFT — 待 PM/Dev workflow 实现

---

## 1. 问题背景

2026-04-10/11 对 chain `task-1775855010-7fcf8b` 和 `task-1775862217-e742de`（B24 修复）的审计揭示多个 context 传播断裂点，全部指向同一根因：**各组件各自从 metadata 读取上下文，而非从统一的 chain_context 读取，导致阶段间信息丢失和语义不一致。**

### 具体症状

| 症状 | Bug | 根因 |
|------|-----|------|
| Retry dev 的 SCOPE CONSTRAINT 不含前序 dev 改过的文件，checkpoint gate 反复失败 | B28a | `auto_chain.py:1146` 只读 PM 静态 metadata |
| QA agent 收到空 context 输出自然语言，`recommendation=None` 导致 gate 永久阻断 | B28b | `test_report` 传播依赖 metadata 链，断链时 context 为空；executor 层无结构化输出校验 |
| Dev `changed_files` 漏报新建文件，导致 checkpoint 误判、scope 继承不可靠 | B27 | Dev executor 未用 `git diff --diff-filter=A` 采集新建文件 |
| `chain_events` 丢失：recovery 期间事件被丢弃，后续链路事件级联丢失 | B25 | `chain_context.py:452` `_recovering=True` 时 `_persist_event` no-op，`_task_to_root` 重建不完整 |
| `node_state.updated_by` 为空字符串 | B26 | 节点更新调用路径未传递 task_id |
| `_gate_qa_pass` 第一步 block 后节点不推进，报错消息混淆（显示 `t2_pass` 而非真正原因） | B28b 附 | `_try_verify_update`（`:2097`）只在 `recommendation` 通过后才执行 |

### 与 session-status.md 对齐说明

`session-status.md:74` 中「O1 Phase 1 complete」指的是 **O1-Phase-1a**（chain_context 基础设施：`StageSnapshot`、`ChainContextStore`、`ROLE_RESULT_FIELDS`、`recover_from_db` 骨架）已完成。本方案从 **O1-Phase-1b** 开始，即在基础设施之上实现实际的 context 读取和继承。

---

## 2. 当前架构问题

### 2.1 chain_context 数据已有，但未被消费

`chain_context.py:80-96`（`StageSnapshot`）的 `result_core` 存储每阶段核心字段。`ROLE_RESULT_FIELDS`（`:44-53`）定义角色可见字段：

```python
ROLE_RESULT_FIELDS = {
    "dev":  ["target_files", "requirements", "acceptance_criteria", "verification", "prd"],
    # dev 没有 changed_files → retry dev 无法继承前序变更范围
    "test": ["changed_files", "target_files"],
    "qa":   ["test_report", "changed_files", "acceptance_criteria"],
}
```

`get_chain(task_id, role="dev")` 返回的 context 缺少前序所有 dev 的累积 `changed_files`。

### 2.2 SCOPE CONSTRAINT 只读 PM 静态 metadata

`auto_chain.py:1145-1149`（retry dev prompt 构建）：

```python
allowed = set(metadata.get("target_files", []))
allowed.update(metadata.get("test_files", []))
doc_files = (metadata.get("doc_impact") or {}).get("files", [])
allowed.update(doc_files)
```

每次 retry 的 `allowed` 退回到 PM 原始声明，丢失前序 dev 已扩展的文件集。

### 2.3 QA prompt builder 无 fallback，executor 无输出校验

`executor_worker.py:1248-1276`（QA prompt 构建）读 `context.get("test_report", {})`，metadata 链断裂时 test_report 为空，QA agent 输出退化为自然语言。`_parse_output`（`:1717`）遇到非 JSON 输出走 raw fallback 返回 `{"summary":..., "exit_code":...}`，该非结构化输出被默默传递给 gate，gate 的 `recommendation=None` block 消息掩盖了真正原因。

### 2.4 Dev changed_files 采集不完整

`executor_worker.py:345-352`（git diff 采集）使用 `git diff`，不包含 `--diff-filter=A` 或 `git status --porcelain` 的新增文件扫描。Dev 新建文件不出现在 `changed_files`，导致：
1. `_gate_checkpoint` 误判文件未改
2. Phase-1b 的 scope 继承累加了不完整输入，意义有限

**B27 是 B28a 的前提：先修采集，再修继承。**

### 2.5 recovery 后 _task_to_root 不完整（B25）

`chain_context.py:346-368`（`recover_from_db`）replay chain_events 时 `_recovering=True`，此窗口内发布的事件被 `_persist_event` 静默丢弃。recovery 完成后，仅已持久化的任务知道 root，后续事件因 `_task_to_root` miss 而级联丢失（`:166`：`if not root_id: return`）。

**B25 是 Phase-2b（chain_context 可靠读）的前提**：chain_context 不可信，O1 Phase-2b 以后的内存读取无保障。

---

## 3. 目标架构

**chain_context 作为链路内 single source of truth，字段级语义 accessor 替代通用 get。**

```
PM result → StageSnapshot(pm)
              ↓
B27 修复：Dev executor 完整采集 changed_files（含新建文件）
              ↓
Dev result → StageSnapshot(dev, result_core={changed_files:[all files]})
              ↓
B28a 修复：retry dev 调用 get_retry_scope(chain_id) 继承累积文件集
              ↓
Test result → StageSnapshot(test, result_core={test_report:{...}})
              ↓
B28b 修复：QA 调用 get_latest_test_report(chain_id) + executor 硬校验输出结构
              ↓
QA gate → recommendation 合法 → _try_verify_update 推进节点 → _check_nodes_min_status
```

accessor 按语义分三类：

| accessor | 语义 | 实现 |
|----------|------|------|
| `get_accumulated_changed_files(chain_id)` | 累积：所有前序 dev 的 changed_files 并集 | DB fallback（B25 修复前）→ chain_context 内存（B25 修复后） |
| `get_retry_scope(chain_id)` | 累积：PM target_files + test_files + doc_impact + 前序 dev changed_files | 组合上述 + metadata |
| `get_latest_test_report(chain_id)` | 最新：最近一次 test 任务的 test_report | 按 created_at DESC 取第一条 |
| `get_pm_acceptance_criteria(chain_id)` | 指定来源：只取 PM stage 的 acceptance_criteria | type=pm 的 StageSnapshot |
| `get_dev_scope_files(chain_id)` | 指定来源：PM target_files + doc_impact（不含前序 dev）| PM metadata 直读 |

metadata 保留为兼容层 fallback，不删除，但 builders 优先走 accessor。

---

## 4. 迁移步骤

### P1 — B27: Dev changed_files 采集完整性

**目标**：`git diff` 改为同时捕获修改和新建文件，消除漏报。

**修改文件**：`agent/executor_worker.py:345-352`（`_get_git_changed_files` 或 inline git diff 调用处）

**修改方案**：

```python
# 当前：只检测已追踪文件的变更
changed_files = self._get_git_changed_files(cwd=execution_workspace)

# 目标：修改 + 新建（untracked staged）
# _get_git_changed_files 内部改为：
#   git diff --name-only HEAD          （已提交 vs HEAD，修改）
# + git diff --name-only --cached      （staged 但未提交）
# + git ls-files --others --exclude-standard  （untracked，如有 staging）
# 合并去重
```

若 Dev 在 worktree 里操作，确认 `--diff-filter` 或 `git status --porcelain` 覆盖 `A`（added）、`M`（modified）、`R`（renamed）状态。

**验证**：`pytest agent/tests/test_dev_contract_round4.py -k "new_file"` — 新建文件出现在 result `changed_files`。

---

### P1 — B28a: retry dev SCOPE CONSTRAINT 继承

> **标注**：本节是 **temporary bridge**（O1-Phase-1b）。Phase-2b 完成后，`get_retry_scope` 内部将从 chain_context 内存读取，届时 DB fallback 可移除。

**目标**：retry dev 的 `allowed` 文件集继承所有前序 dev 已改文件。

**步骤 1：在 chain_context.py 封装语义 accessor**

在 `ChainContextStore`（`:116`）新增方法（不暴露裸 SQL 到 auto_chain）：

```python
# chain_context.py — 新增 accessor
def get_accumulated_changed_files(self, chain_id: str, project_id: str) -> list[str]:
    """返回 chain 内所有前序 dev 任务的 changed_files 并集。
    
    Temporary bridge（O1-Phase-1b）：优先读内存 StageSnapshot，
    内存 miss（B25 未修复导致恢复不完整）时 fallback 到 DB 查询。
    标注：B25 修复后删除 DB fallback 分支。
    """
    result = set()
    # 内存路径
    with self._lock:
        chain = self._chains.get(chain_id)
        if chain:
            for stage in chain.stages.values():
                if stage.task_type == "dev" and stage.result_core:
                    result.update(stage.result_core.get("changed_files", []))
            if result:
                return sorted(result)
    # DB fallback（B25 未修复时的保障）
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
            try:
                cf = json.loads(row["result_json"] or "{}").get("changed_files", [])
                result.update(cf)
            except Exception:
                pass
    except Exception:
        pass
    return sorted(result)

def get_retry_scope(self, chain_id: str, project_id: str, base_metadata: dict) -> set[str]:
    """返回 retry dev 的完整 allowed 文件集。
    
    = PM target_files + test_files + doc_impact.files + 前序所有 dev changed_files
    """
    allowed = set(base_metadata.get("target_files", []))
    allowed.update(base_metadata.get("test_files", []))
    doc_files = (base_metadata.get("doc_impact") or {}).get("files", [])
    allowed.update(doc_files)
    allowed.update(self.get_accumulated_changed_files(chain_id, project_id))
    return allowed
```

**步骤 2：auto_chain.py:1145-1149 调用 accessor**

```python
# 当前（直接读 metadata）:
allowed = set(metadata.get("target_files", []))
allowed.update(metadata.get("test_files", []))
doc_files = (metadata.get("doc_impact") or {}).get("files", [])
allowed.update(doc_files)

# 替换为（通过 accessor，O1-Phase-1b temporary bridge）:
from .chain_context import get_store
_chain_id = metadata.get("chain_id") or _chain_id  # _chain_id 在调用处已有
allowed = get_store().get_retry_scope(_chain_id, project_id, metadata)
if not allowed:
    allowed = set(metadata.get("target_files", []))  # 最终 fallback
```

**同时修改**：`chain_context.py:44-47`，`ROLE_RESULT_FIELDS["dev"]` 加入 `"changed_files"`（使 `get_chain(role="dev")` 序列化时包含前序 dev 的变更列表）：

```python
"dev": ["target_files", "requirements", "acceptance_criteria",
        "verification", "prd", "changed_files"],  # +changed_files: retry scope
```

**验证**：
```bash
pytest agent/tests/test_dev_contract_round4.py -k "retry_scope" -v
```
手动触发 qa_fail → retry dev，确认 SCOPE CONSTRAINT 包含前序 role docs。

---

### P1 — B28b: QA 结构化输出 + executor 硬校验

**目标**：QA 任务必须输出合法 JSON 含 `recommendation`；executor 层拦截非结构化输出，不让其流入 gate。

**步骤 1：executor_worker.py — QA 完成后硬校验输出**

在 `_parse_output` 调用后（`:372`），针对 `task_type == "qa"` 增加合约校验：

```python
# executor_worker.py:~374，parse_output 之后插入
if task_type == "qa":
    rec = result.get("recommendation", "")
    valid_recs = {"qa_pass", "qa_pass_with_fallback", "reject", "rejected"}
    if rec not in valid_recs:
        # 结构化输出合约违反：不让非法结果流入 gate
        log.error(
            "QA structured_output_invalid: recommendation=%r keys=%s",
            rec, list(result.keys())
        )
        return {
            "status": "failed",
            "error": f"structured_output_invalid: QA must set recommendation to one of {sorted(valid_recs)}, got {rec!r}",
            "result": {
                "recommendation": "",
                "error": "structured_output_invalid",
                "raw_summary": result.get("summary", "")[:500],
            },
        }
```

这样非结构化输出触发任务 `failed`（而非走到 gate 被 block），executor 重试能得到新的 QA 尝试。

**步骤 2：executor_worker.py:1248 QA prompt builder — chain_context fallback + 强指令**

```python
elif task_type == "qa":
    # 2a: test_report 从 chain_context 获取（fallback 路径）
    test_report = context.get("test_report", {})
    if not test_report:
        try:
            from agent.governance.chain_context import get_store
            tr = get_store().get_latest_test_report(task_id, project_id)
            if tr:
                test_report = tr
        except Exception:
            pass
    changed = context.get("changed_files", [])
    # ... 原有 context 注入 ...
    # 2b: 强化输出格式指令
    parts.append(
        "You MUST respond ONLY with a JSON object. "
        "Valid recommendations: \"qa_pass\", \"qa_pass_with_fallback\", \"reject\".\n"
        "Required format: {\"recommendation\": \"qa_pass\", \"review_summary\": \"...\", "
        "\"criteria_results\": [{\"criterion\": \"AC1\", \"passed\": true, \"evidence\": \"...\"}]}\n"
        "If context is insufficient to review, respond: "
        "{\"recommendation\": \"reject\", \"reason\": \"insufficient context to review\"}\n"
        "Do NOT respond with plain text or markdown outside of JSON."
    )
```

**步骤 3：chain_context.py — 新增 `get_latest_test_report` accessor**

```python
def get_latest_test_report(self, task_id: str, project_id: str) -> dict | None:
    """返回 chain 内最近 test 任务的 test_report。
    
    Temporary bridge：优先读内存，内存 miss 时 fallback DB。
    """
    with self._lock:
        root_id = self._task_to_root.get(task_id)
        chain = self._chains.get(root_id) if root_id else None
        if chain:
            for stage in sorted(chain.stages.values(), key=lambda s: s.ts, reverse=True):
                if stage.task_type == "test" and stage.result_core:
                    tr = stage.result_core.get("test_report")
                    if tr:
                        return tr
    # DB fallback
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

**验证**：
```bash
pytest agent/tests/test_test_contract_round1.py -v
pytest agent/tests/test_qa_context_fallback.py -v  # 新增
```
构造 `test_report` 为空的 QA 任务，确认 fallback 路径生效，输出包含合法 `recommendation`。

---

### P1.5 — B25: chain_context recovery 可靠性

> **前提**：此 phase 完成后，Phase-2b 的 chain_context 内存读才可信，accessor 内的 DB fallback 分支才可以移除。

**根因**：`recover_from_db`（`:346`）期间 `_recovering=True`，新进事件被 `_persist_event` 静默丢弃；recovery 完成后 `_task_to_root` 仅包含 chain_events 里有记录的任务，服务重启窗口内新建的任务永久 miss。

**修复方案（两步）**：

**步骤 1：recovery 完成后从 `tasks` 表补扫活跃 chain**

```python
# chain_context.py:recover_from_db 末尾追加
def _rebuild_task_to_root_from_db(self, project_id: str):
    """recovery 完成后，扫描 tasks 表补全 _task_to_root（B25 修复）。
    
    用 chain_id 字段直接定位 root，不依赖 parent_task_id 链条。
    只处理 status IN (queued, claimed, running) 的活跃任务。
    """
    try:
        from .db import get_connection
        conn = get_connection(project_id)
        rows = conn.execute(
            "SELECT task_id, chain_id, type FROM tasks "
            "WHERE status IN ('queued','claimed','running') AND chain_id IS NOT NULL"
        ).fetchall()
        conn.close()
    except Exception:
        return
    with self._lock:
        for row in rows:
            task_id = row["task_id"]
            chain_id = row["chain_id"]
            if task_id in self._task_to_root:
                continue  # 已由 chain_events replay 建立
            # 确保 root chain 存在
            if chain_id not in self._chains:
                self._chains[chain_id] = ChainContext(chain_id, project_id)
            self._task_to_root[task_id] = chain_id
            if task_id not in self._chains[chain_id].stages:
                self._chains[chain_id].stages[task_id] = StageSnapshot(
                    task_id, row["type"], "", None
                )
```

在 `recover_from_db` 末尾调用：`self._rebuild_task_to_root_from_db(project_id)`

**步骤 2：recovery 期间的事件排队而非丢弃**

`_persist_event`（`:449`）当 `_recovering=True` 时将事件加入临时队列，recovery 完成后回放：

```python
def _persist_event(self, ...):
    if self._recovering:
        self._recovery_queue.append((root_task_id, task_id, event_type, payload, project_id))
        return
    # ... 原有 INSERT 逻辑 ...
```

recovery 完成后：
```python
for args in self._recovery_queue:
    self._persist_event(*args)
self._recovery_queue.clear()
```

**验证**：重启 governance 服务，确认已 queued/claimed 任务的后续事件（task.completed、gate.blocked）被正确记录到 `chain_events`。

---

### P2 — builder 全面迁移到字段级 accessor

> **前提**：B25 修复完成（chain_context 内存可信）。

**目标**：`executor_worker.py:_build_prompt`（`:996`）各 role 分支改用字段级 accessor，不直接读 metadata 裸字段，不用通用 `_ctx_get()`。

**accessor 完整清单**（在 `chain_context.py` 中实现）：

| accessor | 语义 | 调用方 |
|----------|------|-------|
| `get_accumulated_changed_files(chain_id, project_id)` | 累积，所有前序 dev | retry dev SCOPE CONSTRAINT |
| `get_retry_scope(chain_id, project_id, base_metadata)` | 累积，完整 allowed 集 | auto_chain.py retry dev |
| `get_latest_test_report(task_id, project_id)` | 最新 test stage | QA prompt builder |
| `get_pm_acceptance_criteria(task_id, project_id)` | 指定来源（PM stage only） | QA / gatekeeper prompt |
| `get_dev_scope_files(task_id, project_id)` | 指定来源（PM 原始声明，不含前序 dev） | dev prompt（PM target） |
| `get_accumulated_changed_files_for_test(task_id, project_id)` | 最近一个 dev 的 changed_files | test prompt（关注最新变更） |

**各 role 分支修改**：

```python
# executor_worker.py:_build_prompt

elif task_type == "dev":
    # 旧: context.get("target_files")
    # 新:
    target = get_store().get_dev_scope_files(task_id, project_id) or context.get("target_files", [])

elif task_type == "test":
    # 旧: context.get("changed_files", [])
    # 新:
    changed = get_store().get_accumulated_changed_files_for_test(task_id, project_id) or context.get("changed_files", [])

elif task_type == "qa":
    # 旧: context.get("test_report", {}) / context.get("changed_files", [])
    # 新:
    test_report = get_store().get_latest_test_report(task_id, project_id) or context.get("test_report", {})
    changed = (get_store().get_accumulated_changed_files(chain_id, project_id)
               or context.get("changed_files", []))
    criteria = get_store().get_pm_acceptance_criteria(task_id, project_id) or context.get("acceptance_criteria", [])
```

**P1.5 accessor 的 DB fallback 分支可在此 phase 移除**（内存已可信）。

**验证**：
```bash
pytest agent/tests/ -q --tb=short  # 全量回归
```
检查各 role 的 prompt 长度未超限（chain_context `ROLE_RESULT_FIELDS` 已做字段裁剪）。

---

### P3 — gate 改进

**目标**：gate 报错消息准确，减少误导性 retry；checkpoint skip 审计化。

**3a — `_gate_qa_pass` 消息优化**（`auto_chain.py:2072`）

P1 的 executor 硬校验完成后，gate 收到 `recommendation=None` 的概率极低，但仍需明确消息：

```python
if rec not in ("qa_pass", "qa_pass_with_fallback", "reject", "rejected"):
    return False, (
        f"QA gate: recommendation={rec!r} is invalid. "
        f"Expected: qa_pass | qa_pass_with_fallback | reject. "
        f"Likely cause: QA executor produced non-JSON output (structured_output_invalid). "
        f"Check executor logs for task {task_id}."
    )
```

**3b — checkpoint skip_reason（枚举 + 审计化）**（`auto_chain.py:~1983`）

允许 Dev 在 `result_json.skip_reasons` 中声明跳过某文件的原因，**只允许枚举值，必须进 result_json，不接受 metadata 传入**：

```python
# 合法枚举（在 auto_chain.py 顶部定义）
VALID_SKIP_REASONS = frozenset({
    "file_unchanged_by_design",   # 本次任务故意不修改该文件
    "covered_by_separate_task",   # 已有单独任务处理该文件
    "doc_not_applicable",         # 文件为纯文档但与本次变更无实质关联
    "already_up_to_date",         # 文件内容已反映当前变更，无需再改
})

# _gate_checkpoint 中:
skip_reasons = result.get("skip_reasons") or {}  # 只读 result_json，拒绝 metadata
for f in missing_docs:
    reason = skip_reasons.get(f, "")
    if reason in VALID_SKIP_REASONS:
        # 审计：写入 audit_index
        _audit_skip_reason(conn, project_id, task_id, f, reason)
        log.info("checkpoint: %s skipped, reason=%s", f, reason)
    else:
        still_missing.append(f)
if still_missing:
    return False, f"Related docs not updated: {still_missing}..."
```

skip_reason 写入 `audit_index`（event=`"checkpoint.skip_reason"`），可追溯。

**验证**：
```bash
pytest agent/tests/test_checkpoint_gate.py -v
```
测试 skip_reason 枚举校验（非法值被拒绝）和审计记录生成。

---

## 5. 影响文件清单

| 文件 | Phase | 变更内容 | 优先级 |
|------|-------|---------|--------|
| `agent/executor_worker.py:345-352` | P1 B27 | git diff 改为覆盖新建文件 | P1 |
| `agent/governance/chain_context.py:44-47` | P1 B28a | ROLE_RESULT_FIELDS dev 加 changed_files | P1 |
| `agent/governance/chain_context.py:116+` | P1 B28a | 新增 `get_accumulated_changed_files`、`get_retry_scope` accessor | P1 |
| `agent/governance/auto_chain.py:1145-1149` | P1 B28a | SCOPE CONSTRAINT 改用 `get_retry_scope` | P1 |
| `agent/executor_worker.py:372-392` | P1 B28b | QA 完成后 executor 硬校验输出结构 | P1 |
| `agent/governance/chain_context.py:116+` | P1 B28b | 新增 `get_latest_test_report` accessor | P1 |
| `agent/executor_worker.py:1248-1276` | P1 B28b | QA prompt builder chain_context fallback + 强指令 | P1 |
| `agent/governance/chain_context.py:346-368` | P1.5 B25 | `recover_from_db` + `_rebuild_task_to_root_from_db` | P1.5 |
| `agent/governance/chain_context.py:449-453` | P1.5 B25 | `_persist_event` 排队而非丢弃 recovery 期间事件 | P1.5 |
| `agent/governance/chain_context.py:116+` | P2 | 新增全部字段级 accessor（`get_pm_acceptance_criteria` 等） | P2 |
| `agent/executor_worker.py:996-1310` | P2 | `_build_prompt` 各 role 分支改用字段级 accessor | P2 |
| `agent/governance/auto_chain.py:2072-2082` | P3 | `_gate_qa_pass` 报错消息优化 | P3 |
| `agent/governance/auto_chain.py:~1983` | P3 | checkpoint skip_reason（枚举 + 审计） | P3 |
| `agent/tests/test_qa_context_fallback.py` | P1 B28b | 新增 QA context fallback + 硬校验测试 | P1 |
| `agent/tests/test_dev_contract_round4.py` | P1 B27 | 新增 new_file changed_files 测试 | P1 |
| `agent/tests/test_chain_context_recovery.py` | P1.5 B25 | 新增 recovery 可靠性测试 | P1.5 |

---

## 6. 验证计划

### 单元测试

```bash
# P1: B27 + B28a + B28b
pytest agent/tests/test_dev_contract_round4.py -k "new_file or retry_scope" -v
pytest agent/tests/test_qa_context_fallback.py -v  # 新增
pytest agent/tests/test_test_contract_round1.py -v  # 回归

# P1.5: B25
pytest agent/tests/test_chain_context_recovery.py -v  # 新增

# P2: 全量回归（accessor 迁移不应改变行为）
pytest agent/tests/ -q --tb=short

# P3: gate
pytest agent/tests/test_checkpoint_gate.py -v
```

### 集成验证（每 phase 后）

1. **P1**：创建修改 `executor_worker.py`（目标文件含 role docs）的 PM 任务 → 等待完整链路 → 检查：
   - Dev changed_files 包含新建文件
   - 触发 qa_fail → retry dev 的 SCOPE CONSTRAINT 包含前序 role docs
   - QA 非 JSON 输出 → executor 标记 failed，不流入 gate

2. **P1.5**：重启 governance 服务（有活跃任务时）→ 验证 chain_events 连续无缺失，`_task_to_root` 覆盖所有 queued/claimed 任务

3. **P2**：运行 5 条完整链路，对比 P1 前后 prompt 长度和 context 完整性无退化

---

## 7. 风险注意

| 风险 | 严重度 | 缓解方案 |
|------|--------|---------|
| P1 accessor DB fallback 在 B25 未修复时被高频调用，增加 DB 压力 | 低 | 查询走 `chain_id` 索引（B22 修复后 chain depth O(n) 可控），可加进程级缓存（TTL 30s） |
| P1 executor 硬校验导致 QA 任务频繁 failed → 触发 B22 扇出新 QA | 中 | 硬校验前先加 prompt 强化（P1 步骤2），降低 QA 输出非 JSON 概率；B22 修复（dispatch 去重）同步进行 |
| P2 accessor 迁移后 prompt 长度增加超出 token 限制 | 中 | `ROLE_RESULT_FIELDS` + `_extract_core` 已做字段裁剪；迁移时对比 P1/P2 的实际 prompt 长度 |
| P3 skip_reason 枚举被硬编码，未来扩展需改代码 | 低 | 枚举定义在 `VALID_SKIP_REASONS`（顶层常量），可配置化，但先保持枚举防止自由文本滥用 |
| P1.5 recovery 排队事件在服务长时间重启后积压过多 | 低 | 仅排队 recovery 期间（通常 < 1s）的事件，不存在积压问题 |

---

## 8. 与 Bug Backlog 对应关系

| Bug | Phase | 当前状态 | 备注 |
|-----|-------|---------|------|
| B22a — auto_chain dispatch 去重 | 独立于 O1 | OPEN | 与 B28b 硬校验并行，防止 QA failed 扇出 |
| B22b — conflict_rules Rule 2 same-op | 独立于 O1 | OPEN | |
| B22c — auto-chain 豁免范围收窄 | 独立于 O1 | OPEN | |
| B24 — PM verification.command 语法错误 | 已修复 | FIXED（待 merge） | B24 链路进行中 |
| **B25** — chain_events 记录不完整 | **P1.5** | OPEN | O1 Phase-2b 的前提，不先修 O1 停在半迁移 |
| B26 — node_state updated_by 为空 | 独立于 O1 | OPEN | `_try_verify_update:2645` 调用路径未传 task_id |
| **B27** — Dev changed_files 漏报新建文件 | **P1** | OPEN | B28a scope 继承的前提，先修采集 |
| **B28a** — retry dev SCOPE CONSTRAINT 丢失前序 changed_files | **P1**（temporary bridge） | OPEN | Phase-2b 完成后移除 DB fallback |
| **B28b** — QA context 退化 + 无结构化输出校验 | **P1** | OPEN | executor 硬校验 + chain_context fallback |
| O1-Phase-1a — chain_context 基础设施 | 已完成 | DONE | `session-status.md` 中「O1 Phase 1 complete」指此 |
| O1-Phase-1b — scope 继承 / QA context（本方案 P1） | **P1** | OPEN | |
| O1-Phase-2a — B25 recovery 可靠性（本方案 P1.5） | **P1.5** | OPEN | |
| O1-Phase-2b — builder 全面迁移（本方案 P2） | **P2** | OPEN | 依赖 P1.5 完成 |
| O1-Phase-3 — gate 改进（本方案 P3） | **P3** | OPEN | |

---

## 附录：诊断依据

- **B24 chain QA 失败**（task-1775868111）：`result_json.keys = ['summary', 'exit_code', 'changed_files', '_worktree', '_branch']`，`recommendation=None`，`_parse_output` raw fallback。`metadata.test_report = {'passed': 23, 'failed': 0}`（已在 metadata 中），但 QA prompt builder 输出 context 空洞，agent 输出非 JSON。
- **首次零干预链路**（task-1775801122）：QA `recommendation="qa_pass"`，`criteria_results` 3条，32/32 gate 全通过，无 observer bypass。
- **SCOPE CONSTRAINT 反复失败**：`auto_chain.py:1146` 三次 retry dev 的 `allowed = {'agent/executor_worker.py', 'agent/tests/...'}` 均不含 role docs，checkpoint 均失败，需 observer bypass 推进。
- **B25 直接原因**（`chain_context.py:452`）：`_recovering=True` 期间 `_persist_event` `return` 无写入；`on_task_completed:166`：`if not root_id: return` 级联跳过。
