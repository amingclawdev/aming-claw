# Session Runtime 状态服务设计

> 注意：旧的 coordinator.py session 模型已完全移除。Session 现在是 governance server 的 principal+session 模型。

## 核心概念

```
用户 (Telegram)
    │ 通过 telegram_gateway (port 40010) 路由消息
    ▼
Governance Server (port 40006)
    │ principal+session 模型
    │ task_registry 管理任务生命周期
    │ 每个 agent 通过 /api/role/assign 获得 session token
    │
    ├── Dev Agent (长/短生命周期)
    │     独立上下文 + 独立任务 + governance session
    ├── Tester Agent (短生命周期)
    │     独立上下文 + 独立任务 + governance session
    └── QA Agent (短生命周期)
          独立上下文 + 独立任务 + governance session
```

## Session 生命周期模型

### Governance Session（取代旧 Coordinator Session）

> 旧的 Coordinator Session（在已删除的 coordinator.py 中）已被 governance server 的 principal+session 模型取代。

```
消息到达 (telegram_gateway / API)
    │
    ▼
[ROUTE] governance server 接收消息
    │  1. 通过 /api/context/* 加载项目上下文
    │  2. task_registry 管理任务状态
    │  3. 通过 /api/role/sessions 管理角色
    │
    ▼
[PROCESS] 处理消息
    │  理解用户意图:
    │  ├── 查询 → 直接回复 → 通过 telegram_gateway 返回
    │  ├── 短任务 → executor-gateway 执行 → 回复结果
    │  └── 长任务 → 派发给角色 → 监控进度
    │
    ▼
[MANAGE] 管理角色
    │  检查各角色状态:
    │  ├── GET /api/role/{pid}/sessions
    │  ├── dev: running (task-xxx)
    │  ├── tester: idle
    │  └── qa: idle
    │
    │  派发新任务:
    │  └── POST /api/task/create → assign to dev
    │
    ▼
[COMPLETE] 任务完成
    │  1. 通过 governance API 保存上下文
    │  2. 角色 session 通过 heartbeat 自主管理生命周期
    │  3. 过期 session 自动清理 (180s stale, 600s expired)
```

### 角色 Session (Dev/Tester/QA)

```
Governance server 派发任务（通过 task_registry）
    │
    ▼
[SPAWN] 角色 Session 启动
    │  1. 加载角色上下文 (之前的工作记忆)
    │  2. Claim task: POST /api/task/claim
    │  3. 注册 lease: POST /api/agent/register
    │
    ▼
[EXECUTE] 执行任务
    │  运行 Claude Code CLI / 跑测试 / 代码审查（通过 executor-gateway port 8090）
    │  定期 heartbeat 续租（POST /api/role/heartbeat）
    │  进度写入 task registry（governance API）
    │
    ▼
[COMPLETE] 任务完成
    │  1. POST /api/task/complete {status, result}
    │  2. 保存角色上下文
    │  3. POST /api/agent/deregister
    │  4. 通知 governance server (Redis event)
    │
    ▼
Governance server 收到通知 → 通过 telegram_gateway 回复用户 → 决定下一步
```

## 状态服务 (Session Runtime)

### 数据模型

```json
// 存在 Redis + SQLite
// Key: runtime:{project_id}

{
  "project_id": "amingClaw",
  "coordinator": {
    "session_id": "coord-1774210000",
    "status": "active",           // active / closing / closed
    "started_at": "2026-03-22T...",
    "current_message": "帮我跑一下 L1.3 的测试",
    "lock": "coord-lock-9cb15f91"  // 同时只有一个 coord
  },
  "agents": {
    "dev": {
      "session_id": "dev-1774210050",
      "status": "running",        // idle / running / completed / failed
      "task_id": "task-xxx",
      "task_prompt": "为 L1.3 编写单元测试",
      "started_at": "2026-03-22T...",
      "lease_id": "lease-xxx",
      "progress": "正在分析代码结构...",
      "context": {
        "files_modified": ["agent/tests/test_xxx.py"],
        "decisions": ["用 unittest 而不是 pytest"]
      }
    },
    "tester": {
      "status": "idle",
      "last_task": "task-yyy",
      "context": {}
    },
    "qa": {
      "status": "idle",
      "last_task": null,
      "context": {}
    }
  },
  "pending_tasks": [
    {"task_id": "task-zzz", "prompt": "...", "assigned_to": null}
  ],
  "version": 42
}
```

### API

```
GET  /api/runtime/{project_id}           → 完整运行时状态
POST /api/runtime/{project_id}/acquire   → Coordinator 获取控制权
POST /api/runtime/{project_id}/release   → Coordinator 释放控制权
POST /api/runtime/{project_id}/spawn     → 派发角色任务
POST /api/runtime/{project_id}/update    → 更新角色状态
GET  /api/runtime/{project_id}/agents    → 各角色状态
```

## 消息驱动的 Session 切换

```
时间线:

T0: 用户发消息 "帮我改一下 auth 模块"
    → telegram_gateway 路由到 governance server
    → governance server 创建任务 → 派发给 dev
    → Dev Agent 启动（通过 executor-gateway），开始改代码

T1: Dev 还在跑...governance server 监控中

T2: 用户发新消息 "L3.2 状态怎么样"
    → telegram_gateway 路由到 governance server
    → governance server 处理:
        1. 查 /api/wf/{pid}/node/L3.2 状态 → 回复
        2. 检查 dev 状态 → 还在跑 → 不干预

T3: Dev 完成任务
    → 发布 Redis event: task.completed
    → governance server 收到通知:
        1. 看到 dev task-xxx completed
        2. 通过 telegram_gateway 回复用户: "auth 模块修改完成"
        3. 决定: 需要 tester 验证 → 通过 task_registry 派发 tester 任务
```

## 项目控制权锁

> 旧的 Coordinator 控制权锁（在已删除的 coordinator.py 中）已被 governance server 的 session 管理取代。

```
同一项目的 session 管理由 governance server 统一控制:

- 每个 agent 通过 /api/role/assign 获得项目级 session
- session 通过 heartbeat (/api/role/heartbeat) 保持活跃
- 过期 session 自动清理: stale (180s) → expired (600s)
- 同一角色/项目组合由 governance server 保证唯一性
- 查看活跃 session: GET /api/role/{pid}/sessions
```

## 角色上下文隔离

```
每个角色有独立的上下文存储:

context:snapshot:amingClaw:governance   → governance 的项目级上下文
context:snapshot:amingClaw:dev          → dev 的工作上下文
context:snapshot:amingClaw:tester       → tester 的测试上下文
context:snapshot:amingClaw:qa           → qa 的验收上下文

角色上下文内容:
  governance: {focus, pending_tasks, agent_status, recent_messages}
  dev: {current_files, code_changes, decisions, blocked_on}
  tester: {test_results, coverage, failed_tests}
  qa: {review_notes, verified_nodes, blocked_nodes}
```

## Scheduled Task 适配

```
当前:
  Task 启动 → 处理消息 → ACK → 退出

改为:
  Task 启动 → governance API 接管
    → 加载上下文 (/api/context/*)
    → 处理消息
    → 检查角色状态 (GET /api/role/{pid}/sessions)
    → 派发新任务 (POST /api/task/create)
    → 等待 (XREADGROUP BLOCK 30s)
    → 新消息来了? → 处理
    → 超时? → 检查: 有运行中角色?
       → 有 → 继续等 (再 BLOCK 30s)
       → 无 → 保存上下文 → 退出

Session 超时规则:
  无任务运行 + 无新消息 → 5 分钟后退出
  有任务运行 + 无新消息 → 30 分钟后退出 (角色自己会完成)
  有新消息 → 立即处理
```

## 与现有系统的关系

> 旧的 agent/ 模块（coordinator.py, executor.py 等 20 个文件）已全部移除。

```
当前架构:
    │
    ├── governance server (port 40006)
    │     task_registry: create / claim / complete
    │     agent lifecycle: register / heartbeat / deregister
    │     session context: /api/context/* (save / load / log)
    │     workflow: verify-update / summary / release-gate
    │
    ├── telegram_gateway (port 40010)
    │     Telegram 消息路由: reply / bind
    │
    ├── executor-gateway (FastAPI port 8090)
    │     实际任务执行
    │
    └── executor_api (port 40100)
         监控 API
```

## 实现分层

| 层 | 内容 | 优先级 |
|---|------|--------|
| 1 | Runtime 状态模型 + Redis 存储 | P0 |
| 2 | Coordinator 控制权锁 | P0 |
| 3 | 角色上下文隔离 (context key 加角色前缀) | P0 |
| 4 | 消息→任务分流 (查询直接回复 vs 长任务派发) | P1 |
| 5 | 角色 spawn/monitor (实际启动 dev/tester) | P1 |
| 6 | 任务完成通知 → Coordinator 回复 | P1 |
| 7 | Session 切换 (governance server 管理) | P2 |

## 变更记录
- 2026-03-26: 旧 Telegram bot 系统完全移除（bot_commands, coordinator, executor 等 20 个模块），统一使用 governance API
