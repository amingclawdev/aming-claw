# O1 Runtime Context 迁移方案

> 版本: v1.0
> 作者: Observer
> 日期: 2026-04-11
> 状态: DRAFT — 待 PM/Dev workflow 实现

---

## 1. 问题背景

2026-04-10/11 对 chain `task-1775855010-7fcf8b`（audit-process 固化）和 `task-1775862217-e742de`（B24 修复）的审计揭示了多个 context 传播断裂点，全部指向同一根因：**各组件各自从 metadata 读取上下文，而非从统一的 chain_context 读取，导致阶段间信息丢失和语义不一致。**

### 具体症状

| 症状 | Bug | 根因 |
|------|-----|------|
| Retry dev 的 SCOPE CONSTRAINT 不包含前序 dev 改过的文件，导致 checkpoint gate 反复失败 | 新增 B28a | `auto_chain.py:1146` 只读 PM 静态 metadata，不读前序 dev 的 `changed_files` |
| QA agent 收到空 context 时输出自然语言而非 JSON，`recommendation=None` 导致 gate 永久阻断 | B28（QA context 退化） | `executor_worker.py:1248` 的 QA prompt builder 虽有条件注入，但 `test_report` 传播依赖 metadata 链，断链时 context 为空 |
| `chain_events` 丢失：服务重启后 recovery 期间事件被丢弃，后续链路事件级联丢失 | B25 | `chain_context.py:452` `_recovering=True` 时 `_persist_event` 是 no-op，且 `_task_to_root` 重建不完整 |
| `node_state.updated_by` 为空字符串 | B26 | 节点更新调用路径未传递 task_id |
| Dev 的 `changed_files` 漏报新建文件 | B27 | Dev executor 未将新建文件加入 `result_json.changed_files` |
| `_gate_qa_pass` 第一步 block 后节点状态不推进，报错消息混淆 | B28b | `_try_verify_update`（`:2097`）只在 `recommendation` 通过后才执行 |

---

## 2. 当前架构问题

### 2.1 chain_context.py 的数据已有，但没有被消费

`chain_context.py` 已实现完整的 `StageSnapshot` 结构（`:80-96`），`result_core` 存储每阶段的 `changed_files`、`target_files` 等核心字段。`ROLE_RESULT_FIELDS`（`:44-53`）定义了每个 role 可见哪些字段：

```python
ROLE_RESULT_FIELDS = {
    "dev":  ["target_files", "requirements", "acceptance_criteria", "verification", "prd"],
    "test": ["changed_files", "target_files"],
    "qa":   ["test_report", "changed_files", "acceptance_criteria"],
    ...
}
```

**问题**：`dev` 角色看不到前序 dev 的 `changed_files`（该字段未在 `ROLE_RESULT_FIELDS["dev"]` 中）。`get_chain(task_id, role="dev")` 返回的 context 中缺少累积的变更文件列表。

### 2.2 SCOPE CONSTRAINT 只读 PM metadata 静态字段

`auto_chain.py:1145-1149`（retry dev prompt 构建）：

```python
allowed = set(metadata.get("target_files", []))   # PM 声明
allowed.update(metadata.get("test_files", []))
doc_files = (metadata.get("doc_impact") or {}).get("files", [])
allowed.update(doc_files)
```

不读 chain_context 中前序 dev 的 `StageSnapshot.result_core["changed_files"]`，导致每次 retry 的 `allowed` 都退回到 PM 原始声明，丢失前序 dev 已经扩展的文件集。

### 2.3 QA prompt builder 的 test_report 依赖 metadata 传播

`executor_worker.py:1248-1276`（QA prompt 构建）读取 `context.get("test_report", {})`，而 `context` 来自 `:280-300` 的 metadata 展开。若 metadata 中 `test_report` 未正确传播（metadata 链断裂、或 auto_chain dispatch 时未携带），QA agent 收到的是空的 test_report，prompt 变成空洞的 review 请求，输出退化为自然语言。

### 2.4 recovery 后 _task_to_root 不完整

`chain_context.py:346-368`（`recover_from_db`）通过 replay chain_events 重建 `_task_to_root`。若 recovery 期间有新事件发布（`_recovering=True` 导致 `_persist_event` no-op），这些任务的 `task_id` 永远不进入 `_task_to_root`，后续所有相关事件因查不到 root 而被静默丢弃（`on_task_completed:166`：`if not root_id: return`）。

---

## 3. 目标架构

**chain_context 作为链路内 single source of truth。**

```
PM result → StageSnapshot(pm)
              ↓
Dev 读 chain_context(role="dev") → 获得 PM target_files + 前序所有 dev changed_files
              ↓
Dev result → StageSnapshot(dev, result_core={changed_files:[...]})
              ↓
Test 读 chain_context(role="test") → 获得 dev changed_files（ROLE_RESULT_FIELDS已有）
              ↓
Test result → StageSnapshot(test, result_core={test_report:{...}})
              ↓
QA 读 chain_context(role="qa") → 获得 test_report + changed_files（ROLE_RESULT_FIELDS已有）
              ↓
QA gate → 直接从 StageSnapshot 取数据，不依赖 metadata 传播
```

metadata 仍作为兼容层保留，但 **builders 优先读 chain_context，metadata 作为 fallback**。

---

## 4. 迁移步骤（4 Phase）

### Phase 1: SCOPE CONSTRAINT 修复（优先级 P1）

**目标**：retry dev 的 `allowed` 文件集包含所有前序 dev 已改文件，消除 checkpoint gate 反复失败。

**修改文件**：`agent/governance/auto_chain.py:1145-1149`

**当前代码**：
```python
allowed = set(metadata.get("target_files", []))
allowed.update(metadata.get("test_files", []))
doc_files = (metadata.get("doc_impact") or {}).get("files", [])
allowed.update(doc_files)
```

**目标代码**：
```python
allowed = set(metadata.get("target_files", []))
allowed.update(metadata.get("test_files", []))
doc_files = (metadata.get("doc_impact") or {}).get("files", [])
allowed.update(doc_files)
# O1-P1: 继承前序所有 dev 任务的 changed_files（从 DB 读，避免依赖 chain_context 内存状态）
chain_id = metadata.get("chain_id") or metadata.get("parent_task_id")
if chain_id:
    prev_devs = conn.execute(
        "SELECT result_json FROM tasks "
        "WHERE chain_id=? AND type='dev' AND status='succeeded' AND task_id!=?",
        (chain_id, task_id)
    ).fetchall()
    for row in prev_devs:
        try:
            prev_cf = json.loads(row["result_json"] or "{}").get("changed_files", [])
            allowed.update(prev_cf)
        except Exception:
            pass
```

**同时修改**：`chain_context.py:44-47`，在 `ROLE_RESULT_FIELDS["dev"]` 中加入 `"changed_files"`：
```python
"dev": ["target_files", "requirements", "acceptance_criteria",
        "verification", "prd", "changed_files"],  # +changed_files for retry scope
```

**验证**：新建包含 doc 变更的链路，触发 qa_fail → retry dev，确认 SCOPE CONSTRAINT 包含前序 dev 的 role docs。

---

### Phase 2: QA context 完整性（优先级 P1）

**目标**：QA 任务始终能获得完整的 `test_report` 和 `changed_files`，即使 metadata 传播断链。

**2a — executor_worker.py:1248 QA prompt builder 增加 chain_context fallback**

当 `context.get("test_report", {})` 为空时，从 chain_context 查询最近 test 阶段的 `result_core`：

```python
# executor_worker.py:_build_prompt, elif task_type == "qa" 开始处
test_report = context.get("test_report", {})
if not test_report:
    # O1-P2: fallback to chain_context for test_report
    try:
        from agent.governance.chain_context import get_store
        ctx = get_store().get_chain(task_id, role="qa")
        if ctx:
            for stage in reversed(ctx.get("stages", [])):
                if stage.get("task_type") == "test" and stage.get("result_core", {}).get("test_report"):
                    test_report = stage["result_core"]["test_report"]
                    break
    except Exception:
        pass
```

**2b — QA agent 输出格式强化**

`executor_worker.py:1272-1276`（QA prompt 指令）增加明确的 JSON schema 要求和失败时的降级格式，确保即使 context 为空也能输出结构化结果：

```python
parts.append("You MUST respond ONLY with valid JSON. If you cannot determine qa_pass/reject, "
             "respond: {\"recommendation\": \"reject\", \"reason\": \"insufficient context\"}")
parts.append("Do NOT respond with plain text or mixed text/JSON.")
```

**2c — auto_chain.py QA dispatch 时显式传入 test_report**

在 `_do_next_stage`（`:~1320`）dispatch qa 任务时，从前序 test 任务的 `result_json` 中提取 `test_report` 并注入 metadata：

找到 `auto_chain.py` 中构建 qa 任务 metadata 的位置，确保 `test_report` 被显式传递，而非依赖 metadata `{**task_meta}` 自动传播。

**验证**：创建 `test_report` 为空的 QA 任务，确认 fallback 路径能从 chain_context 获取，QA 输出包含 `recommendation` 字段。

---

### Phase 3: prompt builder 统一从 chain_context 读（优先级 P2）

**目标**：`executor_worker.py` 的 `_build_prompt` 不直接读 metadata，而是优先读 chain_context 序列化后的 context。

**3a — get_chain API 标准化**

`chain_context.py:269`（`get_chain`）当前返回 stages 列表，需增加按 role 过滤的便捷方法，返回该 role 可见的最新 stage 数据。

**3b — executor_worker.py:_build_prompt 调用 chain_context**

当前各 `elif task_type == X:` 分支直接从 `context`（即 metadata 展开）读字段，改为：

```python
# 优先从 chain_context 获取上游 stage 结果
chain_ctx = None
try:
    from agent.governance.chain_context import get_store
    chain_ctx = get_store().get_chain(task_id, role=task_type)
except Exception:
    pass

def _ctx_get(field, default=None):
    """从 chain_context 读，fallback 到 metadata context"""
    if chain_ctx:
        for stage in reversed(chain_ctx.get("stages", [])):
            val = (stage.get("result_core") or {}).get(field)
            if val is not None:
                return val
    return context.get(field, default)
```

**3c — 各 role prompt 分支替换**

| role | 当前读法 | 目标读法 |
|------|---------|---------|
| dev | `context.get("target_files")` | `_ctx_get("target_files")` |
| qa | `context.get("test_report", {})` | `_ctx_get("test_report", {})` |
| qa | `context.get("changed_files", [])` | `_ctx_get("changed_files", [])` |
| test | `context.get("changed_files", [])` | `_ctx_get("changed_files", [])` |

**验证**：pytest `agent/tests/test_test_contract_round1.py` 和 `test_dev_contract_round4.py`，加新增 `test_qa_context_fallback.py`。

---

### Phase 4: gate 改进（优先级 P3）

**目标**：gate 失败消息准确，减少误导性 retry。

**4a — _gate_qa_pass 消息优化**（`auto_chain.py:2072`）

当前第1步（`recommendation` 检查）失败时直接 block，但报错消息不提示真正原因（用户看到的是 `agent.executor=t2_pass`，实际是 `recommendation=None`）。改为：

```python
# 第1步 block 后，仍尝试推进节点状态（不等 recommendation 通过）
# 避免节点状态卡住导致后续误导性报错
if rec not in ("qa_pass", "qa_pass_with_fallback", "reject", "rejected"):
    _try_verify_update_best_effort(...)  # 不阻断，best-effort
    return False, f"QA gate requires explicit recommendation. Got: {rec!r}. QA output may be non-JSON — check executor logs."
```

**4b — doc_impact skip_reason 支持**

`_gate_checkpoint`（`auto_chain.py:~1983`）当文件不在 `changed_files` 时报 block，但没有 skip_reason 机制。加入：

```python
skip_reasons = metadata.get("skip_reasons") or {}
if f in skip_reasons:
    log.info("checkpoint: %s skipped (reason: %s)", f, skip_reasons[f])
    continue  # 不算 missing
```

允许 dev 在 result 中声明某些 governed docs 不更新的理由，避免无意义的 checkpoint block。

---

## 5. 影响文件清单

| 文件 | Phase | 变更类型 | 优先级 |
|------|-------|---------|--------|
| `agent/governance/auto_chain.py:1145-1149` | P1 | 修复 SCOPE CONSTRAINT | P1 |
| `agent/governance/chain_context.py:44-47` | P1 | ROLE_RESULT_FIELDS dev 加 changed_files | P1 |
| `agent/executor_worker.py:1248-1276` | P2 | QA prompt builder chain_context fallback | P1 |
| `agent/executor_worker.py:1272-1276` | P2 | QA agent 输出格式强化 | P1 |
| `agent/governance/auto_chain.py:~1320` | P2 | qa dispatch 显式传 test_report | P1 |
| `agent/governance/chain_context.py:269` | P3 | get_chain API 标准化 | P2 |
| `agent/executor_worker.py:996-1310` | P3 | _build_prompt 统一读 chain_context | P2 |
| `agent/governance/auto_chain.py:2072-2114` | P4 | _gate_qa_pass 消息优化 + best-effort update | P3 |
| `agent/governance/auto_chain.py:~1983` | P4 | checkpoint skip_reason 支持 | P3 |
| `agent/tests/test_qa_context_fallback.py` | P2/P3 | 新增 QA context fallback 测试 | P1 |

---

## 6. 验证计划

### 单元测试

```bash
# Phase 1: SCOPE CONSTRAINT
pytest agent/tests/test_dev_contract_round4.py -k "retry_scope" -v

# Phase 2: QA context
pytest agent/tests/test_test_contract_round1.py -v
pytest agent/tests/test_qa_context_fallback.py -v  # 新增

# Phase 3: 全量回归
pytest agent/tests/ -q --tb=short
```

### 集成验证（每 phase 后）

1. 创建修改 `executor_worker.py` 的 PM 任务（目标文件含 role docs）
2. 等待 dev 完成 → 验证 checkpoint 通过
3. 触发 qa_fail（手动构造）→ 重新派发 dev → 验证 retry dev 的 SCOPE CONSTRAINT 包含前序 changed_files
4. 构造空 test_report 的 QA 任务 → 验证 QA 从 chain_context fallback 获取 → 输出正确 JSON

---

## 7. 风险注意

| 风险 | 严重度 | 缓解方案 |
|------|--------|---------|
| chain_context 内存状态在服务重启后不完整（B25 未修复前）| 高 | Phase 1 用 DB 查询替代内存读（`SELECT FROM tasks WHERE chain_id=?`），不依赖内存 chain_context |
| Phase 3 prompt builder 统一后 prompt 长度增加，超出 token 限制 | 中 | `ROLE_RESULT_FIELDS` 已做裁剪，`_extract_core` 只保留核心字段；Phase 3 前先测量 prompt size |
| Phase 1 DB 查询在高并发下增加延迟 | 低 | 查询走已有 `chain_id` 索引（`idx_tasks_chain_id`），O(n) n=chain depth，可接受 |
| skip_reason（Phase 4b）被滥用绕过 checkpoint | 中 | skip_reason 必须写入 result_json 并进入审计流，不能在 metadata 中直接设置 |

---

## 8. 与 Bug Backlog 对应关系

| Bug | 对应 Phase | 状态 |
|-----|-----------|------|
| B22a — auto_chain dispatch 去重 | 独立于 O1，dispatch 层加查重 | OPEN |
| B22b — conflict_rules Rule 2 same-op | 独立于 O1 | OPEN |
| B22c — auto-chain 冲突检测豁免收窄 | 独立于 O1 | OPEN |
| B24 — PM verification.command 语法错误 | **已修复**（`executor_worker.py` B24 链路完成） | FIXED（待 merge） |
| B25 — chain_events 记录不完整 | O1 不直接修复；需独立修复 `recover_from_db` | OPEN |
| B26 — node_state updated_by 为空 | `_try_verify_update`（`:2645`）调用路径未传 task_id | OPEN |
| B27 — Dev changed_files 漏报新建文件 | O1 Phase 1 可缓解（扩大 allowed 集）；根治需修 Dev executor | OPEN |
| **B28a** — retry dev SCOPE CONSTRAINT 丢失前序 changed_files | **O1 Phase 1** | OPEN（本方案） |
| **B28b** — QA context 退化导致 recommendation=None | **O1 Phase 2** | OPEN（本方案） |

---

## 附录：诊断依据

- **B24 chain（task-1775862217-e742de）**：QA task-1775868111 的 `result_json.keys = ['summary', 'exit_code', 'changed_files', '_worktree', '_branch']`，`recommendation=None`，`_parse_output` 走 raw fallback。
- **首次零干预链路（task-1775801122-39f7dc）**：QA result `recommendation="qa_pass"`，`criteria_results` 3条，`governance_status="passed"`，gate 32/32 全通过。
- **SCOPE CONSTRAINT 观测**：`auto_chain.py:1146` 三次 retry dev 的 allowed 均为 `{'agent/executor_worker.py', 'agent/tests/...'}` 不含 role docs，每次 checkpoint 均失败，需手动 observer bypass 才能推进。
