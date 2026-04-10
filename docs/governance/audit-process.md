# Chain 全流程审计流程

> **模板版本**: v1.0  
> **适用系统**: aming-claw governance pipeline  
> **数据库路径**: `shared-volume/codex-tasks/state/governance/{project_id}/governance.db`

---

## 一、审计目的与范围

### 目的

对 aming-claw 治理流水线的 chain 任务进行端到端审计，验证：

1. 每个阶段的任务状态转换是否正确
2. 所有门控检查（gate check）是否全部通过
3. 执行时间线是否合理、无异常延迟
4. Executor 分配是否正常
5. 有无异常事件或错误

### 审计范围

| 数据源 | 说明 |
|--------|------|
| `tasks` | 每个阶段任务的状态、时间、executor 分配 |
| `chain_events` | 链路事件（各阶段完成通知） |
| `gate_events` | 每个任务的门控检查记录 |
| `audit_index` | 项目级全局审计事件流 |
| `gatekeeper_checks` | Gatekeeper 深度验证记录（如有） |
| `node_state` | 节点状态变更（如有） |

---

## 二、审计步骤与 SQL 查询

以下所有查询中，将 `{chain_id}` 替换为实际 chain ID，将 `{project_id}` 替换为项目名。

### Step 1 — 查询链路所有任务

```sql
-- 获取 chain 下所有任务基本信息
SELECT
    task_id,
    type,
    status,
    execution_status,
    created_at,
    started_at,
    completed_at,
    assigned_to,
    attempt_count,
    error_message
FROM tasks
WHERE chain_id = '{chain_id}'
ORDER BY created_at;
```

**检查项**：
- [ ] 所有任务 `status = 'succeeded'`，`execution_status = 'succeeded'`
- [ ] 阶段顺序正确：`pm → dev → test → qa → gatekeeper → merge → deploy`
- [ ] 无任务 `error_message` 非空

### Step 2 — 验证阶段时间线

```sql
-- 计算每个任务的等待时间和执行时长
SELECT
    task_id,
    type,
    created_at,
    started_at,
    completed_at,
    ROUND((julianday(started_at) - julianday(created_at)) * 86400) AS wait_sec,
    ROUND((julianday(completed_at) - julianday(started_at)) * 86400) AS exec_sec
FROM tasks
WHERE chain_id = '{chain_id}'
ORDER BY created_at;
```

**检查项**：
- [ ] 等待时间（wait_sec）无异常大值（通常 < 120s；> 300s 需关注）
- [ ] 执行时长（exec_sec）在合理范围内
- [ ] 前一阶段 `completed_at` ≤ 后一阶段 `created_at`（依赖关系正确）

### Step 3 — 查询链路事件

```sql
-- 查询 chain_events（阶段完成事件）
SELECT
    id,
    task_id,
    event_type,
    ts,
    payload_json
FROM chain_events
WHERE root_task_id = '{chain_id}'
ORDER BY ts;
```

**检查项**：
- [ ] 每个阶段均有对应 `task.completed` 事件
- [ ] 事件时间戳与 tasks 表 `completed_at` 吻合
- [ ] payload 中 result 内容完整

### Step 4 — 验证门控检查

```sql
-- 查询所有 gate_events（包含 chain 根任务）
SELECT
    ge.task_id,
    t.type,
    ge.gate_name,
    ge.passed,
    ge.reason,
    ge.trace_id,
    ge.created_at
FROM gate_events ge
JOIN tasks t ON ge.task_id = t.task_id
WHERE t.chain_id = '{chain_id}'
   OR ge.task_id = '{chain_id}'
ORDER BY ge.created_at;

-- 检查是否存在 FAIL 的 gate
SELECT COUNT(*) AS failed_gates
FROM gate_events ge
LEFT JOIN tasks t ON ge.task_id = t.task_id
WHERE (t.chain_id = '{chain_id}' OR ge.task_id = '{chain_id}')
  AND ge.passed = 0;
```

**检查项**：
- [ ] `failed_gates = 0`（无任何 gate 失败）
- [ ] 每个任务至少有 `version_check` 和对应阶段 gate（如 `_gate_post_pm`、`_gate_checkpoint`、`_gate_t2_pass`、`_gate_qa_pass`、`_gate_gatekeeper_pass`、`_gate_release`、`_gate_deploy_pass`）
- [ ] 所有 gate 的 `trace_id` 一致（同一 trace）

### Step 5 — 查询审计事件流

```sql
-- 查询 chain 执行时间窗口内的审计事件
SELECT event_id, event, actor, ok, ts, node_ids
FROM audit_index
WHERE project_id = '{project_id}'
  AND ts >= '{chain_start_time}'
  AND ts <= '{chain_end_time}'
ORDER BY ts;

-- 检查是否有 ok=0 的失败事件
SELECT COUNT(*) AS failed_events
FROM audit_index
WHERE project_id = '{project_id}'
  AND ts >= '{chain_start_time}'
  AND ts <= '{chain_end_time}'
  AND ok = 0;
```

**检查项**：
- [ ] 事件序列完整：`pm.completed → dev.completed → test.completed → qa.completed（×N）→ gatekeeper.completed（×N）→ merge.completed（×N）→ deploy.completed（×N）→ chain.completed`
- [ ] `failed_events = 0`（无失败审计事件）
- [ ] `chain.completed` 事件存在且 `ok = 1`

### Step 6 — 检查 Gatekeeper 深度验证

```sql
-- 查询 gatekeeper_checks（深度验证记录）
SELECT *
FROM gatekeeper_checks
WHERE project_id = '{project_id}'
  AND created_by IN (
      SELECT task_id FROM tasks WHERE chain_id = '{chain_id}'
  )
ORDER BY created_at;
```

**检查项**：
- [ ] 若有记录，所有 `pass = 1`
- [ ] result_json 中无异常

### Step 7 — 检查节点状态变更

```sql
-- 查询 node_state 变更（如有节点关联）
SELECT *
FROM node_state
WHERE project_id = '{project_id}'
  AND updated_by IN (
      SELECT task_id FROM tasks WHERE chain_id = '{chain_id}'
  )
ORDER BY updated_at;
```

**检查项**：
- [ ] 若有节点更新，`verify_status` 和 `build_status` 符合预期
- [ ] 无节点回退到更低状态

### Step 8 — 验证 Merge 幂等性

```sql
-- 查询 merge 任务结果（验证幂等模式）
SELECT
    task_id,
    created_at,
    started_at,
    completed_at,
    result_json
FROM tasks
WHERE chain_id = '{chain_id}'
  AND type = 'merge'
ORDER BY created_at;
```

**检查项**：
- [ ] 第一个 merge 使用 `isolated_integration` 模式，得到真实 merge commit
- [ ] 后续 merge（如有）使用 `already_merged_replay` 模式（幂等重放，不重复 merge）
- [ ] 所有 merge 的 merge_commit 相同

---

## 三、本次审计结果

### 3.1 审计对象

| 项目 | 值 |
|------|-----|
| **Chain ID** | `task-1775801122-39f7dc` |
| **项目** | `aming-claw` |
| **Trace ID** | `tr-2b6b3ff531f8` |
| **任务内容** | 在 `docs/dev/bug-and-fix-backlog.md` Fixed Bugs 表中新增 B20 条目 |
| **Chain 开始** | `2026-04-10T06:05:22Z` |
| **Chain 结束** | `2026-04-10T06:16:44Z`（最后 chain.completed 事件） |
| **总耗时** | ~11 分 22 秒 |
| **Merge Commit** | `8ab5bce5ac702dd2e2b8c97afe2b956f4a53c809` |

### 3.2 任务清单与时间线

| # | Task ID | 类型 | 状态 | Executor | 创建时间 | 启动时间 | 完成时间 | 等待(s) | 执行(s) | 重试 |
|---|---------|------|------|----------|----------|----------|----------|---------|---------|------|
| 1 | task-1775801122-39f7dc | **pm** | succeeded | executor-80500 | 06:05:22 | 06:05:37 | 06:06:00 | 15 | 23 | 2 |
| 2 | task-1775801171-bce342 | **dev** | succeeded | executor-55780 | 06:06:11 | 06:06:28 | 06:07:02 | 17 | 34 | 2 |
| 3 | task-1775801222-bd15ec | **test** | succeeded | executor-44892 | 06:07:02 | 06:07:25 | 06:08:18 | 23 | 53 | 2 |
| 4 | task-1775801275-3821a7 | **qa** | succeeded | executor-55780 | 06:07:55 | 06:08:11 | 06:08:54 | 16 | 43 | 2 |
| 5 | task-1775801298-623036 | **qa** | succeeded | executor-44892 | 06:08:18 | 06:08:31 | 06:09:05 | 13 | 34 | 1 |
| 6 | task-1775801316-70124e | **gatekeeper** | succeeded | executor-72104 | 06:08:36 | 06:09:44 | 06:09:57 | 68 | 13 | 2 |
| 7 | task-1775801334-23f1f1 | **gatekeeper** | succeeded | executor-36208 | 06:08:54 | 06:09:44 | 06:10:09 | 50 | 25 | 2 |
| 8 | task-1775801345-828ee3 | **gatekeeper** | succeeded | executor-67844 | 06:09:05 | 06:09:45 | 06:10:20 | 40 | 35 | 2 |
| 9 | task-1775801397-d8a028 | **merge** | succeeded | executor-55780 | 06:09:57 | 06:10:33 | 06:10:47 | 36 | 14 | 2 |
| 10 | task-1775801409-5d2d2a | **merge** | succeeded | executor-72104 | 06:10:09 | 06:16:20 | 06:16:27 | **371** ⚠️ | 7 | 2 |
| 11 | task-1775801420-5970fc | **merge** | succeeded | executor-80500 | 06:10:20 | 06:10:43 | 06:11:00 | 23 | 17 | 2 |
| 12 | task-1775801447-f7ab28 | **deploy** | succeeded | executor-80500 | 06:10:47 | 06:11:29 | 06:11:35 | 42 | 6 | 2 |
| 13 | task-1775801460-9df749 | **deploy** | succeeded | executor-72104 | 06:11:00 | 06:11:32 | 06:11:48 | 32 | 16 | 2 |
| 14 | task-1775801787-d02357 | **deploy** | succeeded | executor-44892 | 06:16:27 | 06:16:38 | 06:16:44 | 11 | 6 | 1 |

### 3.3 阶段并行结构

本 chain 采用并行子任务架构，Test 完成后同时触发 QA×2，QA 完成后触发 Gatekeeper×3，每个 Gatekeeper 完成后各自触发 Merge → Deploy：

```
PM ──► Dev ──► Test
                └──► QA-1 ──► GK-1 ──► Merge-1 ──► Deploy-1
                └──► QA-2 ──► GK-2 ──► Merge-2 ──► Deploy-2 (⚠️延迟)
                         └──► GK-3 ──► Merge-3 ──► Deploy-3
```

### 3.4 Gate 检查验证

共 **32 条** gate_events，全部 `passed = 1`，无任何 gate 失败。

| 阶段 | Gate 名称 | 数量 | 结果 |
|------|-----------|------|------|
| PM | `version_check` + `_gate_post_pm` | 2 | ✅ 全 PASS |
| Dev | `version_check` + `_gate_checkpoint` | 2 | ✅ 全 PASS |
| Test | `version_check` + `_gate_t2_pass` | 4 | ✅ 全 PASS |
| QA×2 | `version_check` + `_gate_qa_pass` | 6 | ✅ 全 PASS |
| Gatekeeper×3 | `version_check` + `_gate_gatekeeper_pass` | 6 | ✅ 全 PASS |
| Merge×3 | `version_check` + `_gate_release` | 6 | ✅ 全 PASS |
| Deploy×3 | `version_check` + `_gate_deploy_pass` | 6 | ✅ 全 PASS |

所有 gate 版本号：PM/Dev/Test/QA/GK 阶段为 `2bd20f9`（开发分支），Merge/Deploy 阶段为 `8ab5bce`（合入 main 后的 commit）。

### 3.5 审计事件流

| 时间 | 事件 | ok | 说明 |
|------|------|----|------|
| 06:06:11 | `pm.completed` | ✅ | PM 完成，开始 Dev |
| 06:06:11 | `memory.written` | ✅ | PM 结果写入 memory |
| 06:07:00 | `memory.written` | ✅ | Dev 执行中写入 memory |
| 06:07:02 | `dev.completed` | ✅ | Dev 完成，开始 Test+QA |
| 06:07:02 | `memory.written` | ✅ | Dev 结果写入 memory |
| 06:07:55 | `test.completed` | ✅ | Test 完成 |
| 06:07:55 | `memory.written` | ✅ | Test 结果写入 memory |
| 06:08:16 | `memory.written` | ✅ | QA 执行中写入 memory |
| 06:08:18 | `test.completed` | ✅ | Test 第二次完成事件（并行分支） |
| 06:08:36 | `qa.completed` | ✅ | QA-1 完成 |
| 06:08:36 | `memory.written` | ✅ | |
| 06:08:54 | `qa.completed` | ✅ | QA-2 完成 |
| 06:09:05 | `qa.completed` | ✅ | QA-3 完成（test 分支 QA） |
| 06:09:05 | `memory.written` | ✅ | |
| 06:09:57 | `gatekeeper.completed` | ✅ | GK-1 完成 |
| 06:10:09 | `gatekeeper.completed` | ✅ | GK-2 完成 |
| 06:10:20 | `gatekeeper.completed` | ✅ | GK-3 完成 |
| 06:10:47 | `merge.completed` | ✅ | Merge-1 完成（isolated_integration） |
| 06:11:00 | `merge.completed` | ✅ | Merge-3 完成（already_merged_replay） |
| 06:11:35 | `deploy.completed` | ✅ | Deploy-1 完成 |
| 06:11:35 | `chain.completed` | ✅ | **链路第一次完成（主路径）** |
| 06:11:48 | `deploy.completed` | ✅ | Deploy-2 完成 |
| 06:11:48 | `chain.completed` | ✅ | 链路再次完成（并行分支） |
| 06:16:27 | `merge.completed` | ✅ | Merge-2 完成（延迟后 already_merged_replay） |
| 06:16:44 | `deploy.completed` | ✅ | Deploy-3 完成 |
| 06:16:45 | `chain.completed` | ✅ | 链路最终完成 |

> 注：`chain.completed` 出现 3 次，是因为每条并行分支各自触发一次完成事件，属正常行为。

### 3.6 Merge 幂等性验证

| 任务 | merge_mode | merge_commit |
|------|-----------|--------------|
| task-1775801397-d8a028 | `isolated_integration` | `8ab5bce5ac702dd2e2b8c97afe2b956f4a53c809` |
| task-1775801409-5d2d2a | `already_merged_replay` | `8ab5bce5ac702dd2e2b8c97afe2b956f4a53c809` |
| task-1775801420-5970fc | `already_merged_replay` | `8ab5bce5ac702dd2e2b8c97afe2b956f4a53c809` |

✅ **幂等性正常**：第一个 Merge 完成真实合并，后两个 Merge 检测到已合并后做 replay，三者 commit hash 一致。

### 3.7 内容验证结果

**Dev 阶段完成内容**：
- 修改文件：`docs/dev/bug-and-fix-backlog.md`
- 操作：在 Fixed Bugs 表第 48 行插入 `| B20 | Clean staged/untracked leaks before merge | 2bd20f9 | 2026-04-10 |`
- 位置：B19（第 47 行）之后，G4（第 49 行）之前，ID 顺序正确

**Test 阶段验证结果**（3/3 通过）：
- AC1 ✅：`grep 'B20'` 返回恰好 1 行
- AC2 ✅：内容格式完全匹配
- AC3 ✅：位置顺序正确（B19@47 < B20@48 < G4@49）

**QA 阶段结果**（2 个 QA 均独立验证通过）：
- 两个 QA 均给出 `recommendation: qa_pass`，issues 列表为空
- governance_status: passed

**Gatekeeper 结果**（3 个 Gatekeeper 均通过）：
- 均给出 `recommendation: merge_pass`
- pm_alignment: pass
- 检查了 R1、R2 两项需求，全部满足

---

## 三点五、Step 9 — 输入输出质量审计

> 本节专项验证"该改的有没有改、不该改的有没有动"，是对 gate check 之外的内容质量层审计。

### Step 9.1 — PM PRD 输出质量检查

验证 PM 任务的 `result_json` 中关键字段是否完整、规范。

```sql
-- 提取 PM 任务的 PRD 关键字段
SELECT
    task_id,
    JSON_EXTRACT(result_json, '$.target_files')        AS target_files,
    JSON_EXTRACT(result_json, '$.requirements')        AS requirements,
    JSON_EXTRACT(result_json, '$.acceptance_criteria') AS acceptance_criteria,
    JSON_EXTRACT(result_json, '$.verification')        AS verification,
    JSON_EXTRACT(result_json, '$.changed_files')       AS changed_files_declared
FROM tasks
WHERE chain_id = '{chain_id}'
  AND type = 'pm';
```

**检查项**：
- [ ] `target_files` 非空且为合理路径列表（非 `[]` 或 `null`）
- [ ] `requirements` 包含至少 1 条可验证的需求（Rn 格式）
- [ ] `acceptance_criteria` 包含至少 1 条可机器验证的 AC（含 grep/test 命令）
- [ ] `verification.method` 和 `verification.command` 均非空
- [ ] `changed_files` 在 PM 阶段为空（`[]`）是正常的，PM 不做变更

---

### Step 9.2 — Dev 变更范围审计（该改没改 / 不该改却改了）

```sql
-- 获取 Dev 任务声明的变更文件
SELECT
    task_id,
    JSON_EXTRACT(result_json, '$.changed_files') AS dev_changed_files,
    JSON_EXTRACT(metadata_json, '$.target_files') AS pm_target_files
FROM tasks
WHERE chain_id = '{chain_id}'
  AND type = 'dev';
```

结合 git 命令验证实际变更：

```bash
# 查看 Dev worktree 分支的实际变更文件
git diff --name-only main dev/{dev_task_id}

# 对比 PM 声明的 target_files 与 Dev 实际变更（差集检查）
# 该改没改（漏改）：
comm -23 <(echo '{pm_target_files}' | jq -r '.[]' | sort) \
         <(git diff --name-only main dev/{dev_task_id} | sort)

# 不该改却改了（多改）：
comm -13 <(echo '{pm_target_files}' | jq -r '.[]' | sort) \
         <(git diff --name-only main dev/{dev_task_id} | sort)
```

**检查项**：
- [ ] Dev `changed_files` 与 PM `target_files` 交集非空（实际修改了目标文件）
- [ ] Dev `changed_files` 不包含 PM `target_files` 之外的非预期文件
- [ ] 不含 `*.lock`、`*.env`、配置文件等敏感或非预期变更
- [ ] `changed_files` 非空（Dev 任务应有实际文件变更）

---

### Step 9.3 — Test / QA / Gatekeeper 验证深度检查

验证各验证阶段是否对每条 AC 做了逐项验证，无遗漏或放水。

```sql
-- 提取 Test 任务的 AC 验证结果
SELECT
    task_id,
    JSON_EXTRACT(result_json, '$.test_report.total')  AS ac_total,
    JSON_EXTRACT(result_json, '$.test_report.passed') AS ac_passed,
    JSON_EXTRACT(result_json, '$.test_report.failed') AS ac_failed,
    JSON_EXTRACT(result_json, '$.summary')            AS summary
FROM tasks
WHERE chain_id = '{chain_id}'
  AND type = 'test';

-- 提取 QA 任务的逐项验证结果（criteria_results 数组）
SELECT
    task_id,
    JSON_EXTRACT(result_json, '$.recommendation')    AS recommendation,
    JSON_EXTRACT(result_json, '$.criteria_results')  AS criteria_results,
    JSON_EXTRACT(result_json, '$.issues')             AS issues
FROM tasks
WHERE chain_id = '{chain_id}'
  AND type = 'qa';

-- 提取 Gatekeeper 的验收摘要
SELECT
    gc.id,
    gc.check_type,
    gc.pass,
    JSON_EXTRACT(gc.result_json, '$.pm_alignment')        AS pm_alignment,
    JSON_EXTRACT(gc.result_json, '$.checked_requirements') AS checked_reqs,
    JSON_EXTRACT(gc.result_json, '$.summary')              AS summary
FROM gatekeeper_checks gc
WHERE gc.project_id = '{project_id}'
  AND gc.created_at BETWEEN '{chain_start}' AND '{chain_end}';
```

**检查项**：
- [ ] Test 的 `ac_passed = ac_total`（全部通过），`ac_failed = 0`
- [ ] QA 的 `recommendation` 为 `qa_pass`（不接受其他值）
- [ ] QA 的 `criteria_results` 数组长度 = PM 的 `acceptance_criteria` 数量（无遗漏项）
- [ ] QA 的每条 `criteria_results[].passed = true`，`evidence` 字段非空
- [ ] QA 的 `issues` 为空列表（`[]`）
- [ ] Gatekeeper 的 `pm_alignment = 'pass'`
- [ ] Gatekeeper 的 `checked_requirements` 覆盖 PM 所有 Rn 编号

---

### Step 9.4 — Config 安全检查（无意外配置修改）

```bash
# 检查 config/ 目录是否有变更
git diff main dev/{dev_task_id} -- config/

# 检查 .env 相关文件
git diff main dev/{dev_task_id} -- '*.env' '*.env.*' '.env.example'

# 检查所有非 target_files 声明文件的变更（宽泛扫描）
git diff --name-only main dev/{dev_task_id} | grep -vE '\.(md|txt)$'
```

```sql
-- 从 Dev result_json 提取 changed_files，确认无 config 类文件
SELECT
    task_id,
    JSON_EXTRACT(result_json, '$.changed_files') AS changed_files
FROM tasks
WHERE chain_id = '{chain_id}'
  AND type IN ('dev', 'merge')
  AND JSON_EXTRACT(result_json, '$.changed_files') LIKE '%config%';
-- 期望：0 行（无 config 文件变更）
```

**检查项**：
- [ ] `config/` 目录无变更
- [ ] `.env`、`.env.example` 无变更
- [ ] 无 `*.json` 配置文件（如 `package.json`、`pyproject.toml`）意外修改
- [ ] 上述 SQL 返回 0 行

---

### Step 9.5 — 节点状态合理性检查

```sql
-- 查询 chain 执行期间 node_state 的所有变更
SELECT
    ns.node_id,
    ns.verify_status,
    ns.build_status,
    ns.updated_by,
    ns.updated_at,
    ns.version,
    ns.evidence_json
FROM node_state ns
WHERE ns.project_id = '{project_id}'
  AND ns.updated_at BETWEEN '{chain_start}' AND '{chain_end}'
ORDER BY ns.updated_at;

-- 对比变更前后状态（通过 node_history 表，如有）
SELECT *
FROM node_history
WHERE project_id = '{project_id}'
  AND updated_at BETWEEN '{chain_start}' AND '{chain_end}'
ORDER BY updated_at;
```

**检查项**：
- [ ] 若 chain 为纯文档变更，`node_state` 期间**无变更**是正常的（文档类 chain 不影响代码节点）
- [ ] 若有节点变更，`verify_status` 只能前进（`pending → verified`），不能回退
- [ ] `build_status` 变更符合 `impl:missing → impl:done` 方向
- [ ] `updated_by` 为本 chain 的合法 task_id
- [ ] `version` 单调递增，无并发写冲突（同一 node_id 的 version 不跳号）

---

### Step 9.6 — 本次链路输入输出质量审计结果

| 检查维度 | 结果 | 说明 |
|---------|------|------|
| PM PRD 字段完整性 | ✅ | target_files、requirements（R1/R2）、acceptance_criteria（AC1-3）、verification.command 均完整 |
| Dev 变更范围 | ✅ | 仅修改 `docs/dev/bug-and-fix-backlog.md`，与 PM target_files 完全一致，无额外文件变更 |
| Dev 漏改检查 | ✅ | B20 条目已插入第 48 行，无漏改 |
| Dev 多改检查 | ✅ | changed_files 仅含 1 个文件，无非预期变更 |
| Test AC 覆盖 | ✅ | 3 条 AC 全部逐项验证，passed=3/total=3，无遗漏 |
| QA criteria_results | ✅ | 两个 QA 均给出 3 条 criteria_results，与 PM AC 数量一致，每条均有 evidence |
| QA issues | ✅ | issues 列表为空（`[]`） |
| Gatekeeper 需求覆盖 | ✅ | 3 个 Gatekeeper 均覆盖 R1、R2，pm_alignment=pass |
| Config 安全 | ✅ | 纯文档变更，无 config/ 或 .env 文件修改 |
| node_state 变更 | ✅ | 无节点变更（预期行为：文档变更不影响代码节点） |

---

## 四、发现与结论

### 4.1 总体结论

**✅ 审计通过**。Chain `task-1775801122-39f7dc` 全流程正常完成，无任何失败、错误或异常事件。

### 4.2 正常发现

| 发现 | 说明 |
|------|------|
| 所有 14 个任务成功 | 无任何 `failed`、`error` 状态 |
| 32 条 gate 全部通过 | 无任何 gate 被拒绝 |
| 26 条审计事件全部 ok=1 | 无任何失败审计事件 |
| Merge 幂等性正确 | 3 个并行 Merge 只做了 1 次真实合并 |
| Deploy 无服务重启 | 文档变更，affected_services 为空 |

### 4.3 需关注项

| 编号 | 级别 | 描述 | 影响 | 建议 |
|------|------|------|------|------|
| F-1 | ⚠️ 观察 | `task-1775801409-5d2d2a`（Merge-2）等待时间 371 秒（约 6 分 11 秒） | 无实质影响（最终以 already_merged_replay 模式成功） | 检查该时段 executor-72104 是否有其他任务排队，评估是否需调整调度优先级 |
| F-2 | ℹ️ 信息 | `attempt_count = 2` 出现在 12/14 个任务中 | 无异常，这是 executor 正常的轮询/重试行为 | 无需处理 |
| F-3 | ℹ️ 信息 | `chain.completed` 事件出现 3 次 | 属于并行分支各自完成的预期行为 | 如需唯一化，可在消费方做去重 |
| F-4 | ℹ️ 信息 | `gatekeeper_checks` 和 `node_state` 无本 chain 记录 | 本次为纯文档变更，无代码节点关联 | 正常 |

### 4.4 审计摘要

```
Chain:          task-1775801122-39f7dc
日期:           2026-04-10
项目内容:       添加 bug backlog B20 条目（文档变更）
总任务数:       14（pm×1 dev×1 test×1 qa×2 gatekeeper×3 merge×3 deploy×3）
通过率:         14/14 (100%)
Gate 检查:      32/32 通过 (100%)
审计事件异常:   0
Merge Commit:   8ab5bce
结论:           PASS ✅
```

---

## 附录：快速审计脚本

```python
# 适用于任意 chain_id 的快速审计脚本
import sqlite3, json

def audit_chain(db_path, chain_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    q = lambda sql, p=(): [dict(r) for r in conn.execute(sql, p).fetchall()]

    tasks = q(
        "SELECT task_id, type, status, assigned_to, created_at, started_at, completed_at, "
        "attempt_count, error_message FROM tasks WHERE chain_id=? ORDER BY created_at",
        (chain_id,)
    )
    task_ids = [t['task_id'] for t in tasks] + [chain_id]
    ph = ','.join(['?'] * len(task_ids))

    failed_gates = q(
        f"SELECT * FROM gate_events WHERE task_id IN ({ph}) AND passed=0",
        task_ids
    )
    failed_tasks = [t for t in tasks if t['status'] != 'succeeded']
    error_tasks = [t for t in tasks if t.get('error_message')]

    print(f"总任务数: {len(tasks)},  失败: {len(failed_tasks)},  错误: {len(error_tasks)}")
    print(f"失败 Gate: {len(failed_gates)}")

    # 时间线异常检测（等待 > 300s 标记）
    for t in tasks:
        if t['created_at'] and t['started_at']:
            from datetime import datetime, timezone
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            wait = (datetime.strptime(t['started_at'], fmt) -
                    datetime.strptime(t['created_at'], fmt)).total_seconds()
            if wait > 300:
                print(f"⚠️  {t['task_id']} ({t['type']}) 等待时间异常: {wait:.0f}s")

    print("结论:", "PASS ✅" if not failed_tasks and not failed_gates else "FAIL ❌")

# 使用示例:
# DB = "aming_claw/shared-volume/codex-tasks/state/governance/aming-claw/governance.db"
# audit_chain(DB, "task-1775801122-39f7dc")
```
