# Workflow Governance Service — 设计文档

## 来源
从 toolBoxClient 项目开发过程中总结的需求。经历了多次流程违规后提炼的治理方案。

## 问题背景

AI Agent 协作开发中反复出现的问题：
1. **验收图状态被随意标记** — Dev 标 verify:pass 但没跑 E2E
2. **角色越权** — Coordinator 直接改代码，Dev 改验收图
3. **Phase 假完成** — stub/TODO 被标记 COMPLETED
4. **流程跳步** — 跳过 Gatekeeper 直接发布
5. **Dev 交付不可信** — 报告"已修改"但文件未变
6. **规则靠 prompt 约束** — AI 会忽略、遗忘、绕过

**核心结论**：规则必须写在代码里，由 API 强制执行，不能靠 AI 自律。

## 架构

```
workflow-governance (独立服务, port 30006)
  │
  ├── State Service (验收图状态管理)
  │   ├── 数据: acceptance-state.json (唯一状态源)
  │   ├── 接口:
  │   │   ├── POST /api/wf/verify-update     — 更新节点状态（需角色+证据）
  │   │   ├── POST /api/wf/task-create       — 创建任务
  │   │   ├── POST /api/wf/task-update       — 更新任务状态
  │   │   ├── POST /api/wf/gate-check        — Gatekeeper 审计
  │   │   ├── GET  /api/wf/acceptance-graph   — 生成可读 markdown（只读视图）
  │   │   ├── GET  /api/wf/node/:id          — 查询单节点
  │   │   ├── GET  /api/wf/summary           — 统计摘要
  │   │   └── POST /api/wf/release-gate      — 发布门禁（非全绿=403）
  │   └── 规则:
  │       ├── 状态转换权限矩阵（谁能改什么）
  │       ├── 证据校验（T2-pass 需要 test output）
  │       └── audit log 自动记录
  │
  ├── Memory Service (开发记忆库)
  │   ├── 数据: memories.db (SQLite)
  │   ├── 接口:
  │   │   ├── POST /api/mem/write            — 写入模块记忆
  │   │   ├── GET  /api/mem/query            — 按模块查询
  │   │   ├── GET  /api/mem/related?node=X   — 按验收图关联查询
  │   │   ├── GET  /api/mem/pitfalls?module=X — 踩坑记录
  │   │   └── GET  /api/mem/patterns?module=X — 设计模式
  │   └── 数据模型:
  │       ├── module_id: "stateService" / "agent.js"
  │       ├── category: "pattern" / "pitfall" / "decision" / "stub" / "api"
  │       ├── content: 记忆内容
  │       ├── related_nodes: ["L1.5", "L2.5"]
  │       └── created_by: "dev-agent-xxx"
  │
  └── Audit Service (操作审计)
      ├── 数据: audit-log.json
      ├── 接口:
      │   ├── GET  /api/audit/log            — 查询审计日志
      │   ├── GET  /api/audit/violations     — 查询违规记录
      │   └── POST /api/audit/report         — 生成审计报告
      └── 自动记录: 每次 state/memory 操作
```

## 状态转换权限矩阵

```
| 转换 | 允许角色 | 需要证据 |
|------|---------|---------|
| pending → T2-pass | tester | test output (exit code + pass count) |
| T2-pass → pass | qa | E2E output (Playwright report) |
| pass → fail | any | 失败证据 (error log) |
| fail → pending | dev | 修复 commit hash |
| pending → pass | 禁止 | 不允许跳过 T2-pass |
| 任意 → 手动编辑 | 禁止 | API 是唯一入口 |
```

## 角色文件权限

```
| 文件类型 | PM | Dev | Tester | QA | Gatekeeper | Coordinator |
|---------|----|----|--------|----|-----------:|------------|
| 源代码 | ❌ | ✅ | ❌ | ❌ | ❌ | ❌(≤2次) |
| 测试文件 | ❌ | ✅ | ✅读 | ✅读 | ❌ | ❌ |
| PRD | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| workflow 规则 | ❌ | ❌ | ❌ | ❌ | ❌ | ✅(审批后) |
| 验收图状态 | ❌ | ❌ | ❌ | ❌ | ✅(via API) | ❌ |
| task-log | ❌ | ❌ | ❌ | ❌ | ❌ | ✅(via API) |
```

## 关键设计原则

1. **状态与文档分离** — acceptance-state.json 是唯一真相，acceptance-graph.md 是自动生成的只读视图
2. **AI 调 API，不编辑文件** — 所有状态变更通过 HTTP API
3. **证据驱动** — 状态转换必须附带证据（测试输出、E2E 报告）
4. **角色强制** — API 校验调用者角色，越权请求被拒绝
5. **审计可追溯** — 每次操作自动记录 who/when/what/evidence
6. **独立部署** — 治理服务不在业务项目仓库里，AI Agent 无法篡改

## AI Agent 工作流示例

```
# Dev 完成任务后
Tester 跑测试 → 162/162 pass

# Coordinator 调用状态服务
curl POST http://localhost:30006/api/wf/verify-update \
  -d '{"nodes":["L5.1","L5.2"], "status":"T2-pass",
       "role":"tester", "evidence":"162/162 pass, exit code 0"}'

# API 校验:
#   ✅ tester 有权 pending → T2-pass
#   ✅ evidence 包含 pass count
#   → 写入 acceptance-state.json
#   → 记录 audit-log
#   → 重新生成 acceptance-graph.md

# QA 跑 E2E → 14/14 pass
curl POST http://localhost:30006/api/wf/verify-update \
  -d '{"nodes":["L5.1","L5.2"], "status":"pass",
       "role":"qa", "evidence":"14/14 E2E pass, Playwright report"}'

# 发布前门禁检查
curl POST http://localhost:30006/api/wf/release-gate
# → 200 全绿允许发布
# → 403 有未通过节点，列出清单
```

## Dev 记忆库使用示例

```
# Dev 完成 stateService 模块后写入记忆
curl POST http://localhost:30006/api/mem/write \
  -d '{"module":"stateService", "category":"pattern",
       "content":"HTTP CRUD + SSE 广播，状态走 acceptance-state.json",
       "related_nodes":["L5.1","L5.2","L5.5"]}'

curl POST http://localhost:30006/api/mem/write \
  -d '{"module":"stateService", "category":"pitfall",
       "content":"cp 命令在 worktree 路径下不可靠，用 cat > 替代",
       "related_nodes":["L5.1"]}'

# 下一个 Dev 接到关联任务前查询
curl GET http://localhost:30006/api/mem/related?node=L5.3
# → 返回 stateService 的所有记忆（pattern + pitfall）
```

## 与 toolBoxClient 的集成

toolBoxClient 通过 HTTP 调用治理服务：
- Coordinator: 调 /api/wf/* 管理任务和状态
- Dev: 调 /api/mem/query 获取关联记忆，完成后调 /api/mem/write
- Tester: 跑测试后 Coordinator 调 /api/wf/verify-update 提交证据
- QA: 跑 E2E 后 Coordinator 调 /api/wf/verify-update 提交证据
- Release: 调 /api/wf/release-gate 检查是否可发布

## 与 aming_claw 已有能力的关系

| aming_claw 已有 | 治理服务复用 |
|----------------|------------|
| Multi-stage AI pipeline | 角色分工框架（PM→Dev→Test→QA） |
| Human-in-the-loop gate | 状态转换审批机制 |
| Git checkpoint/rollback | 状态回滚能力 |
| Telegram 驱动 | 通知和交互通道 |
| Workspace management | 多项目治理 |

## 实现优先级

| 优先级 | 模块 | 说明 |
|--------|------|------|
| P0 | State Service — verify-update + release-gate | 解决"随意标绿"问题 |
| P0 | Audit Service — 基础 audit log | 操作可追溯 |
| P1 | State Service — gate-check + task CRUD | Gatekeeper 自动化 |
| P1 | Memory Service — write + query | Dev 记忆积累 |
| P2 | Memory Service — related + pitfalls | 关联查询 |
| P2 | Audit Service — violations + report | 违规检测 |

## toolBoxClient 侧需要的改动

1. Coordinator prompt: 调 /api/wf/* 而非直接编辑 md
2. Dev prompt: 任务前调 /api/mem/query，完成后调 /api/mem/write
3. pre-commit hook: 拒绝直接编辑 acceptance-state.json
4. acceptance-graph.md: 改为自动生成（git ignore 或标记 auto-generated）
