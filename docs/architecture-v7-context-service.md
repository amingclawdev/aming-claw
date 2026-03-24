# Aming Claw 架构方案 v7 — Context Service + 观察者 SOP

> v6 → v7 核心变更：去掉 CLI `-p` 直传上下文，改为 Context Service 服务化。所有 AI session 的输入/输出通过结构化存储，实现全链路可审计、可 replay、可观察。

## 一、问题分析

### 当前（v6）的 CLI -p 模式

```
Executor → claude -p "你是dev...\n上下文...\n用户消息..." → stdout
```

| 问题 | 影响 |
|------|------|
| 超长 prompt 被 shell 截断 | 复杂任务信息丢失 |
| 上下文全塞一个字符串 | AI 容易忽略关键信息（如 target_files） |
| 图片无法传递 | 多模态任务不可能 |
| 过程不可审计 | 只有最终 stdout，中间推理丢失 |
| 观察者看不到中间状态 | 失败时排查困难 |
| 失败不可 replay | 输入没存，无法重现 |

### v7 方案：Context Service

```
Executor → 写 Context 到 Redis → 启动 Claude CLI → Claude 从 API 读 Context
         → AI 输出写回 Redis → Executor 读取 → 校验 → 执行
```

## 二、系统架构

```
┌────────────────────────────────────────────────────────────────┐
│                     Executor (宿主机)                           │
│                                                                │
│  TaskOrchestrator                                              │
│    │                                                           │
│    ├── 1. 组装 Context                                         │
│    │     ContextAssembler.assemble()                           │
│    │     → 结构化 context dict                                 │
│    │                                                           │
│    ├── 2. 存储 Context                                         │
│    │     ContextStore.save(session_id, context)                │
│    │     → Redis HASH: ctx:{session_id}                        │
│    │     → 同时写 SQLite 审计表（持久化）                       │
│    │                                                           │
│    ├── 3. 启动 AI Session                                      │
│    │     claude --system-prompt-file /tmp/ctx-{session_id}.md  │
│    │     或                                                    │
│    │     claude -p "读取 http://localhost:40100/ctx/{sid}"     │
│    │                                                           │
│    ├── 4. AI 输出回写                                          │
│    │     ContextStore.save_output(session_id, output)          │
│    │     → Redis + SQLite                                      │
│    │                                                           │
│    ├── 5. 校验 + 执行                                          │
│    │     DecisionValidator → 执行 approved actions              │
│    │                                                           │
│    └── 6. 归档                                                 │
│          ContextStore.archive(session_id)                      │
│          → 完整 input+output+validation 写入审计                │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

## 三、Context Store 数据模型

### Redis 结构（热数据，实时查询）

```
# Context 输入
ctx:input:{session_id} = HASH {
    "role": "dev",
    "project_id": "amingClaw",
    "prompt": "修改 gatekeeper.py...",
    "target_files": '["agent/governance/gatekeeper.py"]',
    "prd": '{...}',                          # PM 的 PRD（如有）
    "conversation_history": '[...]',          # 最近对话
    "governance_summary": '{...}',            # 节点状态
    "memories": '[...]',                      # 相关记忆
    "git_status": '{...}',                   # 当前 git 状态
    "image_paths": '[]',                      # 图片文件路径
    "file_contents": '{...}',                # 关键文件内容片段
    "created_at": "2026-03-23T..."
}

# Context 输出
ctx:output:{session_id} = HASH {
    "status": "completed|failed|timeout",
    "stdout": "...",
    "stderr": "...",
    "parsed_decision": '{...}',              # 解析后的结构化决策
    "validation_result": '{...}',            # validator 结果
    "executed_actions": '[...]',             # 实际执行的 actions
    "rejected_actions": '[...]',             # 被拒绝的 actions
    "evidence": '{...}',                     # 独立采集的证据
    "completed_at": "2026-03-23T..."
}

# TTL: 24h（活跃 session），归档后删除
```

### SQLite 审计表（冷数据，持久化）

```sql
CREATE TABLE context_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    project_id      TEXT NOT NULL,
    role            TEXT NOT NULL,
    task_id         TEXT,

    -- Input
    prompt          TEXT NOT NULL,
    target_files    TEXT,            -- JSON array
    prd_json        TEXT,            -- PM PRD
    context_json    TEXT NOT NULL,   -- 完整 context 快照
    image_paths     TEXT,            -- JSON array

    -- Output
    ai_stdout       TEXT,
    ai_stderr       TEXT,
    parsed_json     TEXT,            -- 解析后的决策

    -- Validation
    validation_json TEXT,            -- validator 结果
    approved_actions TEXT,           -- JSON array
    rejected_actions TEXT,           -- JSON array

    -- Evidence
    evidence_json   TEXT,            -- 独立采集的证据

    -- Metadata
    status          TEXT NOT NULL,   -- pending|running|completed|failed
    duration_ms     INTEGER,
    created_at      TEXT NOT NULL,
    completed_at    TEXT
);

CREATE INDEX idx_ctx_project ON context_audit(project_id, created_at);
CREATE INDEX idx_ctx_session ON context_audit(session_id);
CREATE INDEX idx_ctx_task ON context_audit(task_id);
```

## 四、Executor API 新增端点

### 观察者可查询的接口

```
# 实时查看 AI session 的输入
GET /ctx/{session_id}/input
返回: Context 输入的完整结构

# 实时查看 AI session 的输出
GET /ctx/{session_id}/output
返回: AI 输出 + 校验结果

# 查看 session 完整链路
GET /ctx/{session_id}/trace
返回: input → output → validation → execution → evidence 完整链路

# 列出最近的 context sessions
GET /ctx/list?project_id=amingClaw&role=dev&limit=10
返回: 最近 session 列表

# Replay: 用同样的 input 重新运行
POST /ctx/{session_id}/replay
返回: 新 session_id（用旧 input 重跑）

# 对比两次运行
GET /ctx/diff?a={session_id_1}&b={session_id_2}
返回: 两次运行的 input/output 差异
```

### AI Session 可调用的接口

```
# AI 从 Context Service 读取完整上下文（替代 CLI -p）
GET /ctx/{session_id}/prompt
返回: 组装好的 prompt 文本（含角色指令+上下文+用户消息）

# AI 上报中间状态（可选）
POST /ctx/{session_id}/progress
Body: {"phase": "coding", "percent": 50, "message": "修改了2个文件"}
```

## 五、CLI 调用方式变更

### 当前（v6）
```python
# 所有内容塞进 stdin
process = subprocess.Popen(
    [claude_bin, "-p", "--output-format", "json"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
)
stdout, _ = process.communicate(input=huge_prompt_string)
```

### v7 方案 A: system-prompt-file（推荐）
```python
# 1. 写 context 到临时文件
ctx_file = f"/tmp/ctx-{session_id}.md"
with open(ctx_file, "w") as f:
    f.write(assembled_prompt)

# 2. 同时存 Redis + SQLite（审计）
context_store.save_input(session_id, context)

# 3. 用 --system-prompt-file 传入
process = subprocess.Popen(
    [claude_bin, "-p",
     "--system-prompt-file", ctx_file,
     "--output-format", "json",
     prompt],  # 只传用户消息作为 prompt arg
    stdout=subprocess.PIPE,
)

# 4. 收集输出
stdout, _ = process.communicate(timeout=timeout_sec)

# 5. 存输出到 Redis + SQLite
context_store.save_output(session_id, stdout)
```

### v7 方案 B: append-system-prompt（备选）
```python
process = subprocess.Popen(
    [claude_bin, "-p",
     "--append-system-prompt", f"上下文已存储在 {session_id}，关键文件: {target_files}",
     "--output-format", "json",
     prompt],
    stdout=subprocess.PIPE,
)
```

### v7 方案 C: API 读取（最完整但依赖网络）
```python
# AI session 启动时在 prompt 里告诉它从 API 读上下文
prompt = f"""
请先调用 curl http://localhost:40100/ctx/{session_id}/prompt 获取完整上下文，
然后根据上下文执行任务。
"""
```

**推荐方案 A**：system-prompt-file 最稳定，不依赖网络，文件内容完整。同时将文件内容复制到 Redis/SQLite 做审计。

## 六、观察者系统

### 6.1 角色定义

```
观察者 = 任务执行的全程监控者 + 报告生成者
职责: 监控 → 根因分析 → 记录 → 生成报告 → 转译给用户
不做: 直接改代码（除非系统无法自修时降级到手动模式）
```

### 6.2 两种观察者模式

```
┌─────────────────────────────────────────────────────────────┐
│ 模式 A: 自动观察者（Executor 内置）                           │
│                                                             │
│ 任务创建 → Executor 自动启动 observer session                │
│   → 监控每个 phase (PM/Coord/Dev/Tester/QA/Gatekeeper)     │
│   → 记录时间戳、输入输出、异常                               │
│   → 任务完成 → 自动生成报告写入 dbservice                    │
│   → 适合: 后台任务、无人值守                                 │
│                                                             │
│ 生命周期 = 任务生命周期                                      │
│ 任务结束 → observer session 关闭                            │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ 模式 B: 人工观察者（Claude Code session 接管）               │
│                                                             │
│ 用户开 Claude Code session                                  │
│   → POST /observer/attach {task_id}                         │
│   → 接管自动观察者（或新建）                                 │
│   → 实时查看 context/trace/validation                       │
│   → 可干预: 暂停/取消/重试 task                              │
│   → 可分析: 5 Whys、质量评分                                │
│   → 任务完成 → 补充人工分析到报告                            │
│   → 适合: 重要任务、调试、迭代                               │
│                                                             │
│ 生命周期 = Claude Code session 存续期间                      │
│ 可监控多个任务，可跨任务对比                                  │
└─────────────────────────────────────────────────────────────┘
```

### 6.3 观察者注册 + 权限

```
观察者注册流程:

自动模式:
  Executor 启动任务时自动注册
  POST /api/role/assign {
    role: "observer",
    principal_id: "auto-observer-{task_id}",
    scope: ["read_ctx", "write_report"]
  }

人工模式 (Claude Code session):
  POST /observer/attach {task_id, session_type: "human"}
  → 验证: 用户必须有 coordinator token
  → 返回: observer_token + task 当前状态
  → 接管: 自动观察者暂停，人工观察者接管

权限矩阵:
  ┌──────────────────┬────────┬────────┬────────────────────────────────┐
  │ 操作             │ 自动   │ 人工   │ 说明                           │
  ├──────────────────┼────────┼────────┼────────────────────────────────┤
  │ 读 /ctx/*        │ ✅     │ ✅     │ read + report + control        │
  │ 读 /status       │ ✅     │ ✅     │                                │
  │ 读 /traces       │ ✅     │ ✅     │                                │
  │ 写 /report       │ ✅     │ ✅     │                                │
  │ 暂停/取消 task   │ ❌     │ ✅     │ control（干预但不执行）         │
  │ 下发新任务       │ ❌     │ ✅*    │ 通过 coordinator，不直接创建   │
  │ 降级到手动模式   │ ❌     │ ✅**   │ 需要显式声明，审计记录         │
  │ 修改代码         │ ❌     │ ❌***  │ 只在降级模式下按 workflow 执行  │
  └──────────────────┴────────┴────────┴────────────────────────────────┘

  核心原则（codex 评审 R2 #3）:
  观察者默认只做 read + report + control，不做 domain execution。
  Coordinator 不该自己写代码，Observer 也不该默认自己修代码。

  * 下发任务: 观察者通过 /coordinator/chat 间接下发，
    不直接创建 task 文件。保持角色分层。
  ** 降级: 必须显式调用 POST /observer/downgrade
    → 审计记录 "observer downgraded to manual"
    → 从此刻起按 workflow 流程操作
    → 任务完成后自动恢复为观察者模式
  *** 修改代码: 降级后才允许，且必须:
    1. 停 executor（防并发）
    2. 建节点（如果没有）
    3. 改代码
    4. commit
    5. coverage-check + verify + verify_loop
    6. 恢复观察者模式
```

### 6.3.1 系统 KPI（codex 评审 R2 #4）

观察者报告中必须包含以下核心指标，用于衡量"系统是否在变强"：

```
系统健康 KPI:

  首次通过率 (first_pass_rate):
    = 不需要重试就通过的 task 数 / 总 task 数
    目标: > 70%

  平均重试轮数 (avg_retry_rounds):
    = sum(retry_round) / 总 task 数
    目标: < 1.5

  Validator 拦截率 (validator_reject_rate):
    = rejected_actions / total_actions
    目标: 稳定在 5-15%（太低=没拦住，太高=AI太差）

  AI自报 vs Evidence 不一致率 (evidence_mismatch_rate):
    = evidence 与 AI 自报不一致的 task 数 / 总 task 数
    目标: < 10%

  错改目标文件率 (wrong_file_rate):
    = 改了非 target_files 文件的 task 数 / 总 dev_task 数
    目标: < 5%（PM 修复后应下降）

  手动降级率 (manual_downgrade_rate):
    = 降级到手动修的次数 / 总迭代轮数
    目标: 持续下降

  Replay 成功复现率 (replay_success_rate):
    = replay 结果一致的次数 / replay 总次数
    目标: > 80%

  PM 触发准确率 (pm_trigger_accuracy):
    = PM 正确触发次数 / (应触发总次数)
    目标: > 90%
```

### 6.4 统一任务入口

Telegram 和 Claude Code 使用**同一个 API 创建任务**，只是通知方式不同：

```
POST /executor/task  ← 统一入口
Body: {
    "source": "telegram|claude_code",   // 来源
    "session_type": "auto|human_observer",  // 观察者模式
    "message": "修复bug",               // 用户需求
    "project_id": "amingClaw",          // 项目
    "chat_id": 7848961760,              // Telegram 时必填
}
Returns: {
    "task_id": "task-xxx",
    "observer_token": "obs-xxx",        // 用于后续查询
    "observer_url": "/observer/watch/task-xxx",
    "status": "created"
}
```

#### 两种入口的完整流程对比

```
┌────────────────────────────────────────────────────────────────┐
│ 入口 A: Telegram                                               │
│                                                                │
│ 用户发 Telegram 消息: "修复上下文bug"                           │
│   ↓                                                            │
│ Gateway 收到 → 调 POST /executor/task {                        │
│   source: "telegram", chat_id: 7848961760,                     │
│   session_type: "auto",                                        │
│   message: "修复上下文bug", project_id: "amingClaw"             │
│ }                                                              │
│   ↓                                                            │
│ Executor:                                                      │
│   1. 创建 task (DB + file)                                     │
│   2. 自动注册 observer session (auto 模式)                     │
│   3. 返回 {task_id, observer_token} 给 Gateway                 │
│   4. TaskOrchestrator.handle_user_message()                    │
│   5. PM → Coordinator → Dev → Tester → QA → Gatekeeper        │
│   ↓                                                            │
│ Gateway:                                                       │
│   持有 observer_token → 每 30s 轮询 /observer/status           │
│   状态变化 → push 到 Telegram                                  │
│   完成 → 推送报告摘要                                          │
└────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────┐
│ 入口 B: Claude Code session                                    │
│                                                                │
│ 用户在终端: curl POST /executor/task {                         │
│   source: "claude_code",                                       │
│   session_type: "human_observer",                              │
│   message: "修复上下文bug", project_id: "amingClaw"             │
│ }                                                              │
│   ↓                                                            │
│ Executor:                                                      │
│   1. 创建 task (DB + file)                                     │
│   2. 注册 observer session (human 模式, 可干预)                │
│   3. 返回 {task_id, observer_token} 给 Claude Code             │
│   4. TaskOrchestrator.handle_user_message()                    │
│   5. PM → Coordinator → Dev → Tester → QA → Gatekeeper        │
│   ↓                                                            │
│ Claude Code session:                                           │
│   持有 observer_token → 主动查询:                              │
│     curl /observer/status?token=obs-xxx                        │
│     curl /ctx/{session_id}/trace                               │
│     curl /observer/report/{task_id}                            │
│   可干预:                                                      │
│     curl POST /observer/pause {task_id}                        │
│     curl POST /observer/cancel {task_id}                       │
│   完成 → 查看完整报告 + 补充分析                               │
└────────────────────────────────────────────────────────────────┘
```

#### 通知方式差异

| | Telegram 入口 | Claude Code 入口 |
|---|---|---|
| 任务创建 | 同一个 /executor/task | 同一个 /executor/task |
| 观察者注册 | 自动(Gateway 持有 token) | 自动(返回给 session) |
| 进度通知 | Gateway 轮询 → push Telegram | session 主动 curl |
| 完成通知 | Gateway push Telegram | session 读 /observer/report |
| 干预能力 | 有限(Telegram 按钮) | 完整(pause/cancel/downgrade) |
| 报告查看 | /report Telegram 命令 | curl /observer/report |
| 降级修代码 | 不支持 | 支持(显式声明) |

### 6.5 Executor API — 观察者端点

```
# 统一任务创建（替代旧的 /coordinator/chat）
POST /executor/task
Body: {source, session_type, message, project_id, chat_id?}
Returns: {task_id, observer_token, observer_url, status}

# 接管已有任务的观察者（从 auto 切换到 human）
POST /observer/attach
Body: {"task_id": "xxx", "observer_token": "obs-xxx"}
Returns: {"observer_id": "...", "mode": "human", "task_status": "..."}

# 释放观察者（恢复自动模式）
POST /observer/detach
Body: {"observer_id": "xxx"}

# 查询当前任务状态
GET /observer/status?token=obs-xxx
Returns: {"task_id": "...", "phase": "dev", "progress": 50, "duration": 120}

# 查看观察者正在监控的任务
GET /observer/watching
Returns: {"tasks": [{"task_id": "...", "phase": "dev", "duration": 120}]}

# 查看/下载执行报告
GET /observer/report/{task_id}
Returns: {完整报告 JSON}

# 观察者列表
GET /observer/list
Returns: {"observers": [{"id": "...", "type": "auto|human", "task_id": "..."}]}

# 干预操作（仅 human 观察者）
POST /observer/pause   Body: {"task_id": "xxx"}
POST /observer/cancel  Body: {"task_id": "xxx"}
POST /observer/retry   Body: {"task_id": "xxx"}

# 降级到手动模式
POST /observer/downgrade
Body: {"observer_id": "xxx", "reason": "系统无法自修"}
→ 审计记录 "observer downgraded"
→ 必须按 workflow 操作
```

### 6.6 Telegram 观察者命令

```
用户在 Telegram 可用:
  /observe task-xxx   → 查看任务实时状态（简化版 trace）
  /report task-xxx    → 查看执行报告
  /trace task-xxx     → 查看完整链路
  /reports            → 最近 10 个报告摘要
```

### 6.6 执行报告结构

每个任务完成后，观察者（自动或人工）必须生成报告写入 dbservice：

```json
{
    "refId": "report:{task_id}",
    "type": "observation_report",
    "scope": "{project_id}",
    "content": {
        "task_id": "task-xxx",
        "task_prompt": "修复上下文系统",
        "project_id": "amingClaw",
        "observer_type": "auto|human",
        "observer_id": "...",
        "duration_sec": 180,
        "started_at": "...",
        "completed_at": "...",

        "phases": {
            "pm": {
                "triggered": true,
                "duration_sec": 15,
                "prd_quality": "good|partial|poor",
                "target_files_provided": true,
                "nodes_proposed": 1
            },
            "coordinator": {
                "actions_created": 2,
                "actions_rejected": 0,
                "context_continuous": true
            },
            "dev": {
                "branch": "dev/task-xxx",
                "worktree_used": true,
                "files_changed": ["agent/governance/gatekeeper.py"],
                "files_match_target": true,
                "test_passed": true,
                "evidence_collected": true,
                "ai_vs_evidence_match": true
            },
            "eval": {
                "triggered": true,
                "verdict": "pass",
                "retry_round": 0
            },
            "tester": {
                "triggered": true,
                "tests_run": 15,
                "tests_passed": 15,
                "verify_status": "t2_pass"
            },
            "qa": {
                "triggered": true,
                "verdict": "pass",
                "verify_status": "qa_pass"
            },
            "gatekeeper": {
                "isolated_session": true,
                "checks_passed": ["coverage", "artifacts", "staleness"],
                "overall": "pass"
            }
        },

        "issues_found": [
            {
                "phase": "dev",
                "type": "wrong_file_modified",
                "severity": "high",
                "description": "Dev AI 改了 executor.py 而不是 gatekeeper.py",
                "root_cause": "PM target_files 未传递到 dev prompt",
                "resolved": true,
                "resolution": "手动修复 PM 触发条件"
            }
        ],

        "five_whys": [
            {
                "symptom": "Dev改错文件",
                "why1": "prompt里没有target_files",
                "why2": "Coordinator create_dev_task没带target_files",
                "why3": "PM没触发所以没PRD",
                "fix": "加宽PM关键词",
                "fix_applied": true
            }
        ],

        "quality_scores": {
            "flow_correctness": 8,
            "memory_quality": 7,
            "context_continuity": 9,
            "executor_constraints": 8,
            "evidence_reliability": 8,
            "overall": 8
        },

        "recommendations": [
            "PM 关键词需要继续扩大",
            "Dev AI 对 governance/ 子目录路径认知不够"
        ],

        "system_improvements": [
            {
                "type": "validator_rule",
                "description": "dev_task 没有 target_files 应该被 validator 拒绝",
                "priority": "P0",
                "status": "implemented"
            }
        ]
    }
}
```

### 6.7 Claude Code 接入观察者的 Prompt

当用户使用 Claude Code session 作为人工观察者时，使用以下 prompt：

```
你是 {project_id} 项目的人工观察者。

## 角色
- 观察者：监控任务执行 → 分析问题 → 生成报告 → 转译给用户
- 可以干预：暂停/取消/重试 task
- 可以降级：系统无法自修时按 workflow 手动修代码

## 接入方式（统一入口）

1. 创建任务 + 自动注册观察者:
   RESULT=$(curl -s -X POST http://localhost:40100/executor/task \
     -H "Content-Type: application/json" \
     -d '{
       "source": "claude_code",
       "session_type": "human_observer",
       "message": "你的需求描述",
       "project_id": "{project_id}"
     }')
   # 返回: {task_id, observer_token, observer_url}
   TASK_ID=$(echo $RESULT | python -c "import sys,json;print(json.load(sys.stdin)['task_id'])")
   OBS_TOKEN=$(echo $RESULT | python -c "import sys,json;print(json.load(sys.stdin)['observer_token'])")

2. 监控任务状态:
   # 实时状态
   curl http://localhost:40100/observer/status?token=$OBS_TOKEN
   # 完整 trace
   curl http://localhost:40100/ctx/$TASK_ID/trace
   # 当前 phase
   curl http://localhost:40100/observer/watching

3. 接管已有任务（从 Telegram 创建的任务）:
   curl -X POST http://localhost:40100/observer/attach \
     -H "Content-Type: application/json" \
     -d '{"task_id":"task-xxx", "observer_token":"obs-xxx"}'

4. 干预（仅 human 模式）:
   curl -X POST http://localhost:40100/observer/pause -d '{"task_id":"xxx"}'
   curl -X POST http://localhost:40100/observer/cancel -d '{"task_id":"xxx"}'
   curl -X POST http://localhost:40100/observer/retry -d '{"task_id":"xxx"}'

5. 查看报告:
   curl http://localhost:40100/observer/report/$TASK_ID

6. Telegram 通知:
   curl -X POST http://localhost:40000/gateway/reply \
     -H "X-Gov-Token: {coordinator_token}" \
     -d '{"chat_id": {chat_id}, "text": "消息"}'

7. 降级到手动模式（仅在系统无法自修时）:
   curl -X POST http://localhost:40100/observer/downgrade \
     -d '{"observer_id":"xxx", "reason":"说明原因"}'
   # 从此必须按 workflow 流程操作代码

## 观察者 SOP

### 任务下发流程

```
1. 下发任务
   curl -X POST http://localhost:40100/coordinator/chat \
     -d '{"message":"...", "project_id":"..."}'

2. 确认 PM 是否触发
   检查 reply 中是否有 "PRD" / "PM" / "target_files"
   如果没有 → 检查 _needs_pm_analysis 是否匹配关键词

3. 监控 Dev 执行
   curl http://localhost:40100/status
   curl http://localhost:40100/ctx/list?role=dev  (v7)
   ls shared-volume/codex-tasks/processing/

4. Dev 完成后检查
   检查分支改动: git diff main..dev/task-xxx
   检查是否改了正确文件（对照 PM target_files）
   检查 evidence: curl http://localhost:40100/ctx/{sid}/trace (v7)
```

### 失败根因分析 SOP（5 Whys）

```
自动修失败后，观察者必须执行以下分析链:

Step 1: 记录失败现象
  "Dev task 完成但改了 executor.py 而不是 evidence.py"

Step 2: Why x1 — 直接原因
  "Dev AI prompt 里没有明确 target_files"

  验证方法:
  - v7: curl /ctx/{session_id}/input → 检查 target_files 字段
  - v6: 检查 task 文件的 _coordinator_context

Step 3: Why x2 — 上游原因
  "Coordinator 的 create_dev_task 没带 target_files"

  验证方法:
  - 检查 Coordinator 输出的 actions 里有没有 target_files
  - 检查 DecisionValidator 是否应该拦截没有 target_files 的 dev_task

Step 4: Why x3 — 系统原因
  "PM 没触发，所以没有 PRD 提供 target_files"

  验证方法:
  - 检查 executor 日志: grep "PM check"
  - 检查 _needs_pm_analysis 的关键词匹配

Step 5: 定位修复点
  分类:
  a) 系统 bug → 加入自动修复队列
  b) 配置问题 → 手动调整后重试
  c) AI 能力边界 → 记录为已知限制，手动修
  d) 架构缺陷 → 记录为 Gap，设计方案后修

  决不能做:
  ❌ 看到失败就"移入手动组"
  ❌ 简单重试不分析原因
  ❌ 只记录表面症状不追根因
```

### 质量评估 SOP

```
每轮迭代完成后，观察者必须评估:

1. 流程正确性
   - PM 触发了吗？PRD 质量如何？
   - Coordinator 编排合理吗？
   - Dev 改了正确文件吗？
   - Tester/QA/Gatekeeper 触发了吗？
   - verify_loop 全绿吗？

2. 记忆质量
   - 新增记忆是否有价值？
   - 是否有重复/噪声？
   - refId 是否语义化（不是 UUID）？

3. 上下文连续性
   - 多条消息之间 Coordinator 是否记得之前说了什么？
   - session_context 是否正确更新？

4. Executor 约束
   - DecisionValidator 拦截了什么？应该拦没拦？
   - EvidenceCollector 采集到了什么？和 AI 自报一致吗？
   - tool_policy 有没有被绕过？

5. 性能
   - Dev task 执行时间
   - 僵尸进程数量
   - 失败重试次数
```

### 迭代 Loop 退出条件

```
Loop 退出当且仅当:
  1. 手动组为空
  2. P0 全部完成（自动或手动）
  3. P1 全部完成（自动或手动）
  4. P2 全部完成或评估后关闭
  5. 没有新增手动项

Loop 中每轮:
  1. 手动组先修
  2. 自动组尝试修
     - 成功 → 观察质量 → 有新问题加入列表
     - 失败 → 5 Whys 分析 → 能修系统的修系统后重试 → 不能的移手动
  3. 按严重度重排
  4. 回到 1
```

## 七、评审反馈整合（codex 评估 6 点）

### 7.1 双写一致性 → Session 状态机 + 幂等

**问题：** Redis + SQLite 双写无事务语义，可能 Redis 有 SQLite 没有。

**修复：** 定义明确的 session 生命周期状态机：

```
created → input_saved → prompt_rendered → running → output_saved → validated → executed → archived
                                                                                    ↘ failed
```

#### 状态迁移约束（codex 评审 R2 #1 补充）

| 当前状态 | 允许迁移到 | 禁止 | 说明 |
|---------|-----------|------|------|
| created | input_saved | running, archived | 必须先存 input |
| input_saved | prompt_rendered | running | 必须先渲染 |
| prompt_rendered | running | validated | 必须先跑 |
| running | output_saved, failed | archived | 只能正常完成或失败 |
| output_saved | validated | executed | 必须先校验 |
| validated | executed, failed | archived | 校验通过才执行，失败可拒绝 |
| executed | archived | running | 执行完归档，不可回退 |
| archived | — | 任何迁移 | 终态，只读 |
| failed | created (new attempt) | running | 失败后必须创建新 attempt，不 resume |

**关键规则：**
- 非法迁移一律拒绝并记录 violation
- 重复写入同一状态 → 幂等（检查 idempotency_key，已存在则跳过）
- **replay = 新 session + 新 attempt_no**，不是原 session 改状态
- **failed 后不允许直接回到 validated**，必须新建 session 从 created 开始
- **archived 后彻底只读**，任何写入请求返回 403

```python
VALID_TRANSITIONS = {
    "created":          {"input_saved"},
    "input_saved":      {"prompt_rendered"},
    "prompt_rendered":  {"running"},
    "running":          {"output_saved", "failed"},
    "output_saved":     {"validated"},
    "validated":        {"executed", "failed"},
    "executed":         {"archived"},
    "archived":         set(),  # terminal, read-only
    "failed":           set(),  # terminal; retry = new session
}

def transition(self, session_id: str, from_state: str, to_state: str) -> bool:
    allowed = VALID_TRANSITIONS.get(from_state, set())
    if to_state not in allowed:
        self._record_violation(session_id, from_state, to_state)
        return False
    # CAS: compare-and-swap with version
    return self._cas_update(session_id, from_state, to_state)
```

所有写操作必须带：
- `session_id` — 唯一标识
- `attempt_no` — 第几次尝试（retry 时递增，从 0 开始）
- `idempotency_key` — `{session_id}:{attempt_no}:{phase}`
- `version` — 乐观锁（CAS）

写入策略：**SQLite 先写（真源），Redis 后写（缓存）。** Redis 写失败不阻塞，SQLite 写失败则整个操作失败。

```python
def save_input(self, session_id, context):
    idem_key = f"{session_id}:0:input"
    # 1. Check idempotency
    if self._idem_exists(idem_key):
        return  # Already saved, skip
    # 2. SQLite first (truth)
    self._sqlite_write(session_id, "input_saved", context, idem_key)
    # 3. Redis cache (best-effort)
    try:
        self._redis_write(f"ctx:input:{session_id}", context, ttl=86400)
    except Exception:
        pass  # Redis failure is non-fatal
```

### 7.2 唯一真源 → 结构化 snapshot 是 canonical

**问题：** Redis JSON 和 /tmp prompt file 可能不一致。

**原则：**
- **Canonical source = SQLite context_audit 表的 context_json**
- prompt file 是 render artifact，由 `PromptRenderer` 从 canonical source 生成
- 审计时同时保存 `renderer_version` 和 `rendered_prompt_hash`
- trace 展示 `snapshot_hash` vs `rendered_hash`，不一致则报警

```python
class PromptRenderer:
    VERSION = "v1.0"

    def render(self, context: dict) -> str:
        """从结构化 context 生成 prompt 文本。"""
        text = self._format(context)
        return text

    def render_to_file(self, session_id: str, context: dict) -> tuple[str, str]:
        """生成 prompt file 并返回 (file_path, content_hash)."""
        text = self.render(context)
        path = f"/tmp/ctx-{session_id}.md"
        with open(path, "w") as f:
            f.write(text)
        import hashlib
        content_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        return path, content_hash
```

### 7.3 Replay Bundle → 完整重现环境

**问题：** 只保存 input 不够，还需要环境信息。

**Replay Bundle 必须包含：**

```json
{
    "session_id": "...",
    "attempt_no": 0,
    "input": { "...canonical context..." },
    "environment": {
        "model_name": "claude-sonnet-4-5",
        "git_commit": "abc123",
        "git_branch": "dev/task-xxx",
        "workspace_dirty": false,
        "tool_versions": {"claude": "1.0.30", "python": "3.12"},
        "env_fingerprint": "sha256:...",
        "renderer_version": "v1.0",
        "rendered_prompt_hash": "abc123de",
        "timestamp": "2026-03-23T..."
    },
    "external_deps": {
        "governance_api_snapshot": {"total_nodes": 158, "qa_pass": 158},
        "dbservice_query_results": [{"refId": "...", "relevance": 0.9}],
        "redis_state": {"session_cache_hit": true},
        "file_system_state": {"target_files_exist": true, "file_hashes": {"agent/governance/gatekeeper.py": "sha256:..."}}
    },
    "time_boundary": {
        "context_assembled_at": "2026-03-23T18:00:00Z",
        "session_started_at": "2026-03-23T18:00:01Z",
        "external_deps_queried_at": "2026-03-23T18:00:00Z"
    },
    "evidence_snapshot": { "...before snapshot..." },
    "output": { "..." },
    "validation": { "..." }
}
```

**补充说明（codex 评审 R2 #2）：**
- `external_deps` 记录外部工具调用结果的快照/引用，防止"不是模型变了而是环境变了"
- `time_boundary` 记录关键依赖输入的时间边界，replay 时可对比"当时的环境 vs 现在的环境"
- 没有这些，replay 是"再跑一次"；有了这些，replay 是"还原现场"

### 7.4 权限模型 → 四级访问控制

**问题：** /ctx/* 端点能看完整 context，是最敏感的一层。

| 权限级别 | 能访问 | 角色 |
|---------|--------|------|
| `observer_read` | /ctx/list, /ctx/{sid}/trace (摘要) | 观察者 |
| `executor_internal` | /ctx/{sid}/input, /ctx/{sid}/output (完整) | Executor 内部 |
| `ai_session_prompt_only` | /ctx/{sid}/prompt (渲染后文本) | AI session |
| `admin_full` | /ctx/{sid}/replay, /ctx/diff, 完整 JSON | 管理员 |

**AI session 默认只读 /prompt，不能读完整 /input 结构。** 这防止 AI 看到系统内部元数据。

### 7.5 Context Budget → 前移到 P0.5

**问题：** budget 放 P2 太晚，直接影响 AI 行为正确性。

**立即实施的 budget 规则：**

```python
ROLE_BUDGETS = {
    "coordinator": {"max_tokens": 8000, "required": ["prompt", "conversation_history", "governance_summary"]},
    "pm":          {"max_tokens": 6000, "required": ["prompt", "governance_summary"]},
    "dev":         {"max_tokens": 4000, "required": ["prompt", "target_files", "file_contents"]},
    "tester":      {"max_tokens": 3000, "required": ["prompt", "changed_files", "test_commands"]},
    "qa":          {"max_tokens": 3000, "required": ["prompt", "evidence", "node_status"]},
}
```

字段优先级（裁剪时从低到高删）：
1. **必选**：prompt, target_files, role_instructions
2. **重要**：conversation_history (最近 5 条), governance_summary
3. **辅助**：memories, git_status, runtime_info
4. **可删**：full file contents (改为摘要), 旧对话历史

### 7.6 SOP → 硬规则升级

**问题：** SOP 是人治，需要变成 Validator 硬规则。

**新增 DecisionValidator 规则：**

```python
# 观察者 SOP 发现的问题 → 升级为代码强制规则

HARD_RULES = {
    "dev_task_must_have_target_files": {
        "check": lambda action: bool(action.get("target_files")),
        "reject_msg": "create_dev_task 必须包含 target_files（由 PM PRD 提供）",
    },
    "pm_required_for_complex_task": {
        "check": lambda action: True,  # 由 _needs_pm_analysis 在 orchestrator 层控制
        "reject_msg": "复杂任务必须先经过 PM 分析",
    },
    "evidence_must_be_complete": {
        "check": lambda action: True,  # 由 EvidenceCollector 在 dev_complete 时检查
        "reject_msg": "证据不完整，不允许 merge/pass",
    },
    "session_must_have_snapshot": {
        "check": lambda action: True,  # 由 ContextStore 在 session 创建时检查
        "reject_msg": "Session 没有输入快照，不能执行",
    },
}
```

## 八、迭代复盘 — 缺陷报告（codex 评审 R3）

### 8.1 核心发现

| 类别 | 缺陷 | 严重度 | 根因 |
|------|------|--------|------|
| **架构** | Node ID 由 AI 生成 | 🔴 | 把确定性元数据交给概率模型 |
| **架构** | "创建新文件"不是一等执行操作 | 🔴 | Executor 主要消费 stdout/diff |
| **架构** | PM 触发靠关键词碰运气 | 🟡 | 没有硬规则，只有 SOP |
| **流程** | 手动修改没走 workflow (5次) | 🔴 | "紧急情况"心态 |
| **流程** | 手动修改污染自动实验 | 🟡 | 未 commit 就开新任务 |
| **分析** | 5 Whys 过早归因（上下文污染） | 🟡 | 先解释后验证 |
| **分析** | "AI 不能创建新文件"结论过大 | 🟡 | 系统锅≠AI 能力边界 |
| **运行** | worktree 基线不干净 | 🟡 | 手动改动和自动任务混在一起 |

### 8.2 Node ID 系统分配方案

**问题：** AI 反复生成空 ID / 占位符 ID，被 validator 拒绝。

**根因：** 治理图的 node ID 是系统内部主键，本质是确定性元数据，不应由概率模型生成。

**方案：分离 node_uid 和 display_id**

```
数据模型:
  node_uid:     n_8f3a2c...   (系统生成，永不变，内部引用用)
  display_id:   L22.1         (系统分配，给人看，可调整)
  parent_uid:   n_ab12...     (父节点引用)
  order_index:  3             (同级排序)
  title:        "ContextStore"

AI 输出（propose_node）:
  {
    "parent_display_id": "L22",     ← AI 只说"挂哪里"
    "title": "ContextStore",         ← AI 说"做什么"
    "description": "...",
    "acceptance_criteria": [...],
    "target_files": [...]
  }
  不输出 node_id / display_id / node_uid

系统处理:
  1. 解析 parent_display_id → 找到 parent_uid
  2. 查该父节点现有子节点最大序号
  3. 分配 display_id = L22.3 (auto-increment)
  4. 生成 node_uid = n_{uuid}
  5. 落库 + 审计
```

### 8.3 Dev Task 显式文件契约

**问题：** Dev AI 不知道该创建新文件还是修改已有文件。

**方案：** create_dev_task 必须包含显式文件契约：

```json
{
  "type": "create_dev_task",
  "target_files": ["agent/executor.py"],       // 允许修改的已有文件
  "create_files": ["agent/context_store.py"],   // 必须创建的新文件
  "forbidden_files": ["agent/governance/*"],    // 禁止修改的文件
  "expected_artifacts": ["test_file"]           // 预期产出
}
```

DecisionValidator 检查：
- `target_files` 全部存在 → 否则拒绝
- `create_files` 全部不存在 → 否则拒绝（防覆盖）
- Dev 完成后 EvidenceCollector 检查：
  - `create_files` 里的文件确实被创建了
  - 没有改动 `forbidden_files`
  - `expected_artifacts` 已生成

### 8.4 观察者纪律约束

**问题：** 观察者（我）5 次手动修改没走 workflow。

**硬规则（补充到观察者 SOP）：**

```
手动修改前 checklist（必须全部完成才能动代码）:
  □ executor 已停止（防并发污染）
  □ git status clean（无未 commit 改动）
  □ 节点存在（没有就先建）
  □ 改完后: coverage-check + verify-update + verify_loop + commit
  □ 启动 executor 前确认 worktree 干净

自动实验前 checklist:
  □ main 分支 clean（git status 无改动）
  □ 基线 commit 固定（记录 commit hash）
  □ 无残留 dev 分支
  □ executor 重启（加载最新代码）
```

### 8.5 分析纪律

**5 Whys 正确顺序：**

```
1. 看原始输出 JSON（不是推理）
2. 看 PM 是否触发（检查日志，不是猜）
3. 看 validator 拒绝/通过了什么（检查 trace）
4. 看 Dev AI 实际做了什么（检查 git diff）
5. 最后才考虑上下文污染/AI 能力边界

不要:
  ❌ 看到相关现象就先解释
  ❌ 一次失败就下"AI 不能 xxx"的结论
  ❌ 不区分系统锅/实验污染/AI 边界
```

## 九、修订后的实施路线（v7.1）

基于迭代复盘，重新排优先级。**核心原则：先修执行契约，再加审计能力。**

### P-1：执行契约修正（最高优先级，手动）

必须先手动完成，否则自动修复链路不可靠。

**共同模式：全部是"自举悖论"——修的是 AI 运行所依赖的基础设施，AI 不能通过自己来修自己的运行环境。** 类似操作系统不能在运行时重写自己的内核。

| 步骤 | 内容 | 文件 | 不能自动修的原因 |
|------|------|------|----------------|
| 0 | **Node ID 系统分配** | graph_validator.py, governance/server.py | 自动修需要 propose_node → propose_node 依赖 ID 生成 → **循环依赖** |
| 1 | **Dev task 文件契约** | decision_validator.py, task_orchestrator.py | 需要改 validator 的校验逻辑 → Dev AI 被 validator 校验 → **自己改不了审判自己的规则** |
| 2 | **PM 硬规则** | task_orchestrator.py | 自动修 PM 时 Dev AI 改错文件 → PM 不触发时无法通过 PM 产出正确路径 → **鸡生蛋问题** |
| 3 | **worktree 强制 clean** | executor.py | 需要改 executor 的任务处理逻辑 → Dev AI 通过 executor 被调用 → **自己改不了运行自己的代码** |

### P0：Context Store 基础（已完成 ✅）

| 步骤 | 内容 | 状态 |
|------|------|------|
| 4 | ContextStore + Session 状态机 | ✅ L22.1 自动完成 |
| 5 | AILifecycleManager system-prompt-file | ✅ L22.2 手动完成 |
| 6 | 统一入口 /executor/task | ✅ L22.3 自动完成 |

### P0.5：Budget + 硬规则（已完成 ✅）

| 步骤 | 内容 | 状态 |
|------|------|------|
| 7 | Context budget 角色裁剪 | ✅ L22.4 自动完成 |
| 8 | DecisionValidator 硬规则 | ✅ L22.5 自动完成 |

### P1：审计 + 权限 + Observer

| 步骤 | 内容 | 文件 | 方式 |
|------|------|------|------|
| 9 | context_audit 表 + replay bundle | agent/context_store.py | 自动 |
| 10 | 四级权限模型 | agent/executor_api.py | 自动 |
| 11 | /ctx/{sid}/trace 完整链路 | agent/executor_api.py | 自动 |
| 12 | Observer 系统 (attach/detach/report) | agent/executor_api.py | 自动 |
| 13 | KPI 自动采集 | agent/executor_api.py | 自动 |

### P2：增强

| 步骤 | 内容 | 文件 |
|------|------|------|
| 14 | /ctx/{sid}/replay 完整重现 | agent/executor_api.py |
| 15 | /ctx/diff 对比 | agent/executor_api.py |
| 16 | 图片路径 + 多模态 | agent/context_store.py |
| 17 | AI 进度上报 | agent/executor_api.py |

### 实施顺序

```
1. P-1（手动）→ 修执行契约，让自动修复可靠
2. P1（自动尝试）→ 审计+Observer
3. P2（自动尝试）→ 增强
4. 每轮自动修后评估 KPI
```

## 十、与 v6 的兼容

v7 是 v6 的增量升级，不破坏现有功能：
- v6 的 CLI `-p` stdin 方式保留作为 fallback
- ContextStore 不可用时退化为 v6 模式
- 所有新增端点是只读查询，不影响执行链路
- 审计表独立于 governance SQLite，不影响节点状态
- **SQLite 是唯一真源，Redis 是缓存层**
- **AI session 只读 /prompt，不读完整 /input**
- **Node ID 由系统分配，AI 只提供 parent + title**（R3 修正）
- **Dev task 必须有显式文件契约**（R3 修正）
