---
name: acceptance-graph
description: 项目验收图（Verification Topology）v3 — 依赖分层拓扑（L0-L4），双状态（impl/verify）、gate_mode auto/explicit、test_coverage、critical_file 升级、失败策略、传播策略
type: reference
version: v3.0
---

# 项目验收图（Verification Topology）v3

## 维护规则

1. **PM**: PRD 中必须指明新节点的层级、依赖、gates、verify、gate_mode、test_coverage 和文件映射，格式：
   ```
   [TREE:ADD] Lx > node_name | deps:[Lx.y] | gate_mode:auto | verify:Lx | test_coverage:none | primary:[file1] | secondary:[file2] | test:[file3]
   ```
   - `gate_mode:auto` 时无需写 gates（由脚本自动推导）
   - `gate_mode:explicit` 时必须手写 `gates:[Lx.y]`
2. **Tester**: 根据新增叶节点生成单元测试 + E2E 测试用例，更新 `test:[]` 字段和 `test_coverage`
3. **QA**: 验收时必须验证到每个新增叶节点的 `verify` 深度，叶节点全绿 = PASS
4. **Coordinator**: 任务完成后更新节点状态（build_status + verify_status）+ 版本号
5. **回归**: 每次打包前，pre-dist 检查 GUARD 守卫节点对应的代码守卫
6. **层级规则**: 节点层级 = max(所有 deps 节点层级) + 1；无依赖 = L0
7. **gates 规则**: gates 中的节点 verify_status 非 pass 时，本节点验收 SKIP（不执行），整体标记 FAIL
8. **verify 规则**: 标注该节点验收的最低验证深度（L1-L5），QA 不可降级
9. **文件映射三态**:
   - `primary:[]` — 直接定义该能力的核心文件，diff 命中则必须验收本节点
   - `secondary:[]` — 被动消费/转发的文件，仅当关联 primary 节点也受影响时才纳入
   - `test:[]` — 覆盖该节点的测试文件
   - `[TBD]` = 待补映射，`[]` = 明确无关联

### verify 分配规则

| 节点类型 | verify 最低 | 说明 | 测试层级 |
|---------|------------|------|---------|
| 纯配置/代码存在 | L1 | 代码存在 | T1 单元测试可验 |
| 服务层/API | L2 | API 可调用 | T2 API 集成测试可验 |
| UI 展示 | L3 | UI 可见 | T3 E2E 验 |
| 主流程核心（搜索/AI对话/数据隔离） | L4 | 端到端 | T3 E2E 验 |
| 涉及外部系统（Indeed/LinkedIn/JobBank 登录） | L5 | 真实第三方 | T3/T4 E2E 验 |

**每任务验收（T1+T2）可达到 `verify:T2-pass` 的节点**：L1、L2 类型
**发布前验收（T3 E2E）才能达到 `verify:pass` 的节点**：L3、L4、L5 类型

### Gates 自动推导规则

- 默认 `gate_mode: auto`：节点的 gates 自动等于其 deps 中 verify >= L3 的节点
- 当需要精细控制时，改为 `gate_mode: explicit` 并手写 gates
- 脚本校验时，auto 模式的 gates 由脚本根据 deps 重新计算并对比

## 状态说明

### build_status（实现状态）

| 值 | 含义 |
|----|------|
| impl:done | 实现完成 |
| impl:partial | 部分实现 |
| impl:missing | 未实现 |

### verify_status（验收状态）

| 值 | 含义 | 允许操作 |
|----|------|---------|
| verify:pass | E2E 全流程验收通过 | 可发布 |
| verify:T2-pass | 单元+API 测试通过（未经 E2E） | 可 merge，不可发布 |
| verify:fail | 验收失败（已知 bug） | 必须修复 |
| verify:pending | 待验证 | — |
| verify:skipped | 被 GATE 跳过 | 等上游解除 |

**分层验收规则**：
- 每次 `-coord` 任务完成后：Tester 跑 T1+T2（单元+API），通过后节点标记 `verify:T2-pass`
- 版本发布前（`-coord release`）：QA 跑 T3 E2E（真实环境），通过后节点升级为 `verify:pass`
- `verify:T2-pass` 足够 merge 和继续开发，但 **不够发布**
- 发布 GATE（G5 Strict）要求所有节点 `verify:pass`，`T2-pass` 不满足

### test_coverage（测试覆盖）

| 值 | 含义 |
|----|------|
| none | 无测试覆盖 |
| partial | 有单元测试但无 E2E |
| strong | 有单元 + E2E 或有 L4+ 验证证据 |

### 其他标记

| 标记 | 含义 |
|------|------|
| GUARD | 关键守卫（pre-dist 自动检查） |

## 层级定义

| 层级 | 名称 | 含义 | 依赖 |
|------|------|------|------|
| **L0** | 基础设施层 | 无外部依赖，系统启动和打包的最底层 | 无 |
| **L1** | 服务层 | 依赖 L0 提供的运行环境，提供核心服务能力 | L0 |
| **L2** | 能力层 | 依赖 L0+L1，组合服务实现具体业务能力 | L0, L1 |
| **L3** | 场景层 | 依赖 L0+L1+L2，完整用户场景和工作流 | L0, L1, L2 |
| **L4** | 表现层 | 依赖所有下层，UI 展示和用户交互 | L0, L1, L2, L3 |
| **L5** | stateService HTTP + SSE | stateService HTTP CRUD + SSE 广播，跨层集成 | L0, L1, L4 |

## 节点格式说明

```
Lx.y  节点名称  [build_status] [verify_status] 版本 [GUARD]
      deps:[依赖节点]         — 功能依赖（本节点运行需要这些节点正常）
      gate_mode: auto|explicit
      gates:[验收前置]        — 仅 explicit 模式需要（auto 模式由脚本推导）
      verify: Lx              — 最低验证深度
      test_coverage: none|partial|strong
      propagation: smoke_ui   — 仅连接类节点，命中时建议追加 UI smoke
      primary:[核心文件]      — diff 命中 → 必验
      secondary:[辅助文件]    — diff 命中 → 仅 --full 模式纳入
      test:[测试文件]         — 覆盖该节点的测试
```

## 拆树规则

当前为单棵图。拆分时机：
- 服务独立部署时（如 notifyService）→ 拆出独立子图
- 新增 Agent 模板时（不只 job-seek）→ 拆出 Agent 子图
- 子图通过 deps 标记对主图节点的依赖

---

## L0 — 基础设施层（无依赖）

```
L0.1  Electron 主窗口加载  [impl:done] [verify:pass] v1.4.3 GUARD
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[electron.js, preload.js]
      secondary:[client/src/index.js]
      test:[TBD]

L0.2  env 传递给 server fork  [impl:done] [verify:pass] v1.4.1 GUARD
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[electron.js]
      secondary:[]
      test:[TBD]

L0.3  首次启动 npm install（toolService/dbservice）  [impl:done] [verify:pass] v1.4.2
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[electron.js]
      secondary:[toolService/package.json, dbservice/package.json]
      test:[TBD]

L0.4  初始化拦截页显示  [impl:done] [verify:pass] v1.4.2
      deps:[]
      gate_mode: auto
      verify: L3
      test_coverage: none
      primary:[electron.js]
      secondary:[client/src/index.js]
      test:[TBD]

L0.5  Express 端口分配  [impl:done] [verify:pass] v1.4.3
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      propagation: smoke_ui
      primary:[server/server.js, server/services/webSocketService.js]
      secondary:[config.js]
      test:[server/services/webSocketService.test.js]

L0.6  WebSocket 服务启动  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      propagation: smoke_ui
      primary:[server/server.js, server/services/webSocketService.js]
      secondary:[]
      test:[server/services/webSocketService.test.js]

L0.7  API 路由注册  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: none
      propagation: smoke_ui
      primary:[server/router.js, server/server.js]
      secondary:[]
      test:[TBD]

L0.8  client/build 包含在 asar  [impl:done] [verify:pass] v1.4.3 GUARD
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[package.json, scripts/pre-dist.js]
      secondary:[]
      test:[TBD]

L0.9  pre-dist 检查通过  [impl:done] [verify:pass] v1.4.3
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[scripts/pre-dist.js]
      secondary:[]
      test:[TBD]

L0.10 安装目录无用户数据  [impl:done] [verify:pass] v1.4.2 GUARD
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[config.js, electron.js]
      secondary:[]
      test:[TBD]

L0.11 toolService/dbservice node_modules 延迟安装  [impl:done] [verify:pass] v1.4.2
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[electron.js]
      secondary:[toolService/package.json, dbservice/package.json]
      test:[TBD]

L0.12 默认 savePath 自动创建  [impl:done] [verify:pass] v1.4.3
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[config.js]
      secondary:[]
      test:[TBD]

L0.13 NeDB CRUD（wallet/fingerPrint/task）  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[config.js]
      secondary:[server/services/walletService.js, server/services/fingerPrintService.js, server/services/taskService.js]
      test:[server/services/walletService.test.js, server/services/fingerPrintService.test.js, server/services/taskService.test.js]

L0.14 Chromium 安装  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: partial
      primary:[server/services/fingerPrintService.js]
      secondary:[]
      test:[server/services/fingerPrintService.test.js]

L0.15 指纹配置生成  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[server/services/fingerPrintService.js]
      secondary:[]
      test:[server/services/fingerPrintService.test.js]

L0.16 环境列表 CRUD  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[server/services/fingerPrintService.js, server/router.js]
      secondary:[]
      test:[server/services/fingerPrintService.test.js, client/src/pages/ChromeManager/index.test.js]

L0.17 侧边栏导航  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L3
      test_coverage: partial
      primary:[client/src/Layout/index.js, client/src/router.js]
      secondary:[]
      test:[client/src/Layout/index.test.js]

L0.18 响应式布局（900px 断点）  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L3
      test_coverage: partial
      primary:[client/src/index.scss, client/src/Layout/index.js]
      secondary:[]
      test:[client/src/Layout/index.test.js]

L0.19 统一卡片宽度 1400px  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L3
      test_coverage: none
      primary:[client/src/index.scss]
      secondary:[]
      test:[TBD]

L0.20 中文国际化  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[client/src/i18n.js, client/src/utils/languages/]
      secondary:[]
      test:[TBD]

L0.21 英文国际化  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[client/src/i18n.js, client/src/utils/languages/]
      secondary:[]
      test:[TBD]
```

## L1 — 服务层（依赖 L0）

```
L1.1  dbservice 启动（memoryService）  [impl:done] [verify:pass] v1.3
      deps:[L0.2, L0.12]
      gate_mode: explicit
      gates:[L0.2]
      verify: L2
      test_coverage: partial
      primary:[server/services/memoryService.js, dbservice/index.js, dbservice/lib/knowledgeStore.js]
      secondary:[]
      test:[server/services/memoryService.test.js]

L1.2  toolService 启动  [impl:done] [verify:pass] v1.3
      deps:[L0.2]
      gate_mode: explicit
      gates:[L0.2]
      verify: L2
      test_coverage: partial
      primary:[server/services/toolServiceManager.js, toolService/index.js]
      secondary:[]
      test:[server/services/toolServiceManager.test.js]

L1.3  toolService 健康检查  [impl:done] [verify:pass] v1.3
      deps:[L1.2]
      gate_mode: explicit
      gates:[L1.2]
      verify: L2
      test_coverage: partial
      primary:[server/services/toolServiceManager.js]
      secondary:[]
      test:[server/services/toolServiceManager.test.js]

L1.4  浏览器环境启动  [impl:done] [verify:pass] v1.0
      deps:[L0.14, L0.15, L0.16]
      gate_mode: explicit
      gates:[L0.14, L0.16]
      verify: L2
      test_coverage: partial
      primary:[server/services/fingerPrintService.js, assets/agents/job-seek/lib/core/browserLauncher.js]
      secondary:[]
      test:[server/services/fingerPrintService.test.js, assets/agents/job-seek/lib/core/browserLauncher.test.js]

L1.5  savePath 切换后 NeDB 重连  [impl:done] [verify:pass] v1.4.0
      deps:[L0.12, L0.13]
      gate_mode: explicit
      gates:[L0.12]
      verify: L2
      test_coverage: partial
      primary:[config.js, server/services/stateService.js]
      secondary:[]
      test:[server/services/stateService.test.js, server/routes/stateRoutes.test.js]

L1.6  knowledge.db SQLite 读写  [impl:done] [verify:pass] v1.3
      deps:[L1.1]
      gate_mode: explicit
      gates:[L1.1]
      verify: L2
      test_coverage: partial
      primary:[dbservice/lib/knowledgeStore.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/core/knowledgeClient.test.js]

L1.7  knowledge.db 存储到 savePath/db/  [impl:done] [verify:pass] v1.3
      deps:[L0.12, L1.1]
      gate_mode: explicit
      gates:[L1.1]
      verify: L2
      test_coverage: none
      primary:[dbservice/lib/knowledgeStore.js, config.js]
      secondary:[]
      test:[TBD]

L1.8  dbservice savePath 切换时重启  [impl:done] [verify:pass] v1.4.3 GUARD
      deps:[L1.1, L1.5]
      gate_mode: explicit
      gates:[L1.1, L1.5]
      verify: L4
      test_coverage: partial
      primary:[server/services/memoryService.js, config.js]
      secondary:[]
      test:[server/services/memoryService.test.js]

L1.9  WebSocket 客户端（自动重连 + 心跳）  [impl:done] [verify:pass] v1.0
      deps:[L0.6]
      gate_mode: explicit
      gates:[L0.6]
      verify: L2
      test_coverage: partial
      propagation: smoke_ui
      primary:[client/src/utils/webSocket.js]
      secondary:[]
      test:[client/src/utils/webSocket.test.js]

L1.10 API 客户端（Axios 封装）  [impl:done] [verify:pass] v1.0
      deps:[L0.7]
      gate_mode: explicit
      gates:[L0.7]
      verify: L2
      test_coverage: partial
      propagation: smoke_ui
      primary:[client/src/utils/api.js, client/src/utils/requestBase.js]
      secondary:[]
      test:[client/src/utils/api.test.js, client/src/utils/api.coverage.test.js, client/src/utils/requestBase.test.js]

L1.11 Zustand 状态管理  [impl:done] [verify:pass] v1.0
      deps:[L0.7]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[client/src/store/walletStore.js, client/src/store/fingerPrintStore.js, client/src/store/pathStore.js, client/src/store/agentStore.js]
      secondary:[]
      test:[client/src/store/walletStore.test.js, client/src/store/fingerPrintStore.test.js, client/src/store/pathStore.test.js, client/src/store/agentStore.test.js]

L1.12 事件总线  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[client/src/utils/eventEmitter.js]
      secondary:[]
      test:[client/src/utils/eventEmitter.test.js]
```

## L2 — 能力层（依赖 L0+L1）

```
L2.1  savePath 切换后 knowledge.db 隔离  [impl:done] [verify:pass] v1.4.3
      deps:[L1.8, L1.6]
      gate_mode: explicit
      gates:[L1.8, L1.6]
      verify: L4
      test_coverage: partial
      primary:[server/services/memoryService.js, dbservice/lib/knowledgeStore.js]
      secondary:[config.js]
      test:[server/services/memoryService.test.js]

L2.2  savePath 切换后 sessions.json 隔离  [impl:done] [verify:pass] v1.4.2
      deps:[L1.5]
      gate_mode: explicit
      gates:[L1.5]
      verify: L4
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/core/sessionStore.js]
      secondary:[config.js]
      test:[assets/agents/job-seek/lib/core/sessionStore.test.js]

L2.3  升级后用户数据保留  [impl:done] [verify:pass] v1.4.2
      deps:[L0.10, L0.12]
      gate_mode: explicit
      gates:[L0.10]
      verify: L4
      test_coverage: none
      primary:[config.js, electron.js]
      secondary:[]
      test:[TBD]

L2.4  Reset All Memory 清除 knowledgeStore  [impl:done] [verify:pass] v1.4.1
      deps:[L1.6]
      gate_mode: explicit
      gates:[L1.6]
      verify: L4
      test_coverage: partial
      primary:[server/services/memoryService.js]
      secondary:[dbservice/lib/knowledgeStore.js]
      test:[server/services/memoryService.test.js]

L2.5  Reset All Memory 清除 sessions.json  [impl:done] [verify:pass] v1.4.1
      deps:[L1.5]
      gate_mode: explicit
      gates:[L1.5]
      verify: L4
      test_coverage: partial
      primary:[server/services/stateService.js, assets/agents/job-seek/lib/core/sessionStore.js]
      secondary:[]
      test:[server/services/stateService.test.js, assets/agents/job-seek/lib/core/sessionStore.test.js, server/routes/stateRoutes.test.js]

L2.6  新 savePath 无旧记忆泄露  [impl:done] [verify:pass] v1.4.3
      deps:[L1.8, L2.1]
      gate_mode: auto
      verify: L4
      test_coverage: partial
      primary:[server/services/memoryService.js]
      secondary:[config.js]
      test:[server/services/memoryService.test.js]

L2.7  ComSpec env 传递（Windows spawn）  [impl:done] [verify:pass] v1.4.1 GUARD
      deps:[L0.2]
      gate_mode: explicit
      gates:[L0.2]
      verify: L1
      test_coverage: partial
      primary:[server/services/taskService.js, electron.js]
      secondary:[]
      test:[server/services/taskService.test.js]

L2.8  workspace 目录自动创建 + git init  [impl:done] [verify:pass] v1.4.1
      deps:[L0.2]
      gate_mode: explicit
      gates:[L0.2]
      verify: L2
      test_coverage: partial
      primary:[server/services/taskService.js]
      secondary:[]
      test:[server/services/taskService.test.js]

L2.9  Claude CLI 可调用  [impl:done] [verify:pass] v1.4.1
      deps:[L2.7]
      gate_mode: explicit
      gates:[L2.7]
      verify: L2
      test_coverage: partial
      primary:[server/services/taskService.js, assets/agents/job-seek/agent.js]
      secondary:[]
      test:[server/services/taskService.test.js]

L2.10 Codex CLI 可调用  [impl:done] [verify:pass] v1.4.1
      deps:[L2.7]
      gate_mode: explicit
      gates:[L2.7]
      verify: L2
      test_coverage: partial
      primary:[server/services/taskService.js]
      secondary:[]
      test:[server/services/taskService.test.js]

L2.11 新建 session  [impl:done] [verify:pass] v1.0
      deps:[L1.5, L0.13]
      gate_mode: explicit
      gates:[L1.5]
      verify: L2
      test_coverage: partial
      primary:[server/services/stateService.js, assets/agents/job-seek/lib/core/sessionStore.js]
      secondary:[]
      test:[server/services/stateService.test.js, assets/agents/job-seek/lib/core/sessionStore.test.js, assets/agents/job-seek/lib/stateApi.test.js]

L2.12 删除 session  [impl:done] [verify:pass] v1.0
      deps:[L2.11]
      gate_mode: explicit
      gates:[L2.11]
      verify: L2
      test_coverage: partial
      primary:[server/services/stateService.js, assets/agents/job-seek/lib/core/sessionStore.js]
      secondary:[]
      test:[server/services/stateService.test.js, assets/agents/job-seek/lib/core/sessionStore.test.js, assets/agents/job-seek/lib/stateApi.test.js]

L2.13 session 列表持久化  [impl:done] [verify:pass] v1.0
      deps:[L2.11]
      gate_mode: explicit
      gates:[L2.11]
      verify: L2
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/core/sessionStore.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/core/sessionStore.test.js]

L2.14 简历上传解析  [impl:done] [verify:pass] v1.2
      deps:[L1.2]
      gate_mode: explicit
      gates:[L1.2]
      verify: L2
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/core/fileParser.js]
      secondary:[assets/agents/job-seek/agent.js]
      test:[assets/agents/job-seek/lib/core/fileParser.test.js]

L2.15 Profile Collection（5 sections）  [impl:done] [verify:pass] v1.2
      deps:[L2.11]
      gate_mode: explicit
      gates:[L2.11]
      verify: L4
      test_coverage: partial
      primary:[assets/agents/job-seek/agent.js, assets/agents/job-seek/lib/prompts.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/prompts.test.js]

L2.16 masterProfile 跨 session 复用  [impl:done] [verify:pass] v1.3
      deps:[L2.15, L1.6]
      gate_mode: explicit
      gates:[L2.15, L1.6]
      verify: L4
      test_coverage: none
      primary:[assets/agents/job-seek/lib/core/masterProfileClient.js]
      secondary:[assets/agents/job-seek/agent.js]
      test:[TBD]

L2.17 Profile seed from knowledgeStore  [impl:done] [verify:pass] v1.3
      deps:[L1.6, L1.1]
      gate_mode: explicit
      gates:[L1.6]
      verify: L2
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/core/knowledgeClient.js]
      secondary:[dbservice/lib/knowledgeStore.js]
      test:[assets/agents/job-seek/lib/core/knowledgeClient.test.js]

L2.18 登录确认流程  [impl:done] [verify:pass] v1.2
      deps:[L1.4]
      gate_mode: explicit
      gates:[L1.4]
      verify: L4
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/workflow/platformService.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/workflow/platformService.test.js]

L2.19 单环境同时只运行一个任务  [impl:done] [verify:pass] v1.3
      deps:[L1.4, L0.16]
      gate_mode: explicit
      gates:[L1.4]
      verify: L4
      test_coverage: partial
      primary:[server/services/taskService.js, server/services/stateService.js]
      secondary:[]
      test:[server/services/taskService.test.js, server/services/stateService.test.js]

L2.20 Onboarding 子任务完成  [impl:done] [verify:pass] v1.2
      deps:[L2.11, L2.15]
      gate_mode: explicit
      gates:[L2.11]
      verify: L4
      test_coverage: partial
      primary:[assets/agents/job-seek/agent.js]
      secondary:[]
      test:[assets/agents/job-seek/agent.memory.test.js]

L2.21 单一 Agent 入口（无重复）  [impl:done] [verify:pass] v1.4.1
      deps:[L0.7, L1.10]
      gate_mode: auto
      verify: L3
      test_coverage: none
      primary:[client/src/pages/aiAgents/index.js, server/router.js]
      secondary:[]
      test:[TBD]
```

## L3 — 场景层（依赖 L0+L1+L2）

```
L3.1  3 平台初始化（Indeed/LinkedIn/JobBank）  [impl:done] [verify:pass] v1.2
      deps:[L2.11, L1.4]
      gate_mode: explicit
      gates:[L2.11, L1.4]
      verify: L4
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/workflow/platformService.js, assets/agents/job-seek/lib/workflow/platformStore.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/workflow/platformService.test.js, assets/agents/job-seek/lib/workflow/platformStore.test.js]

L3.2  搜索工具构建  [impl:done] [verify:pass] v1.2
      deps:[L2.15, L1.4]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/tools/jobSearch.js, assets/agents/job-seek/lib/toolRouter.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/tools/jobSearch.test.js, assets/agents/job-seek/lib/toolRouter.test.js]

L3.3  Indeed 登录  [impl:done] [verify:pass] v1.2
      deps:[L1.4, L2.18]
      gate_mode: explicit
      gates:[L1.4, L2.18]
      verify: L5
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/sources/indeed.js]
      secondary:[assets/agents/job-seek/lib/workflow/platformService.js]
      test:[assets/agents/job-seek/lib/sources/indeed.test.js]

L3.4  LinkedIn 登录  [impl:done] [verify:pass] v1.2
      deps:[L1.4, L2.18]
      gate_mode: explicit
      gates:[L1.4, L2.18]
      verify: L5
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/sources/linkedin.js]
      secondary:[assets/agents/job-seek/lib/workflow/platformService.js]
      test:[assets/agents/job-seek/lib/sources/linkedin.test.js]

L3.5  JobBank 登录  [impl:done] [verify:pass] v1.2
      deps:[L1.4, L2.18]
      gate_mode: explicit
      gates:[L1.4, L2.18]
      verify: L5
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/sources/jobbank.js]
      secondary:[assets/agents/job-seek/lib/workflow/platformService.js]
      test:[assets/agents/job-seek/lib/sources/jobbank.test.js]

L3.6  Re-login 按钮功能  [impl:done] [verify:pass] v1.4.3（2026-03-21 真实验收：Indeed+LinkedIn Re-login 均触发 launchLogin → auto-verified；cookie 有效时状态自动回 Logged in，无需手动 Confirm）
      deps:[L3.4, L2.18]
      gate_mode: explicit
      gates:[L3.4]
      verify: L5
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/workflow/platformService.js, assets/agents/job-seek/lib/dashboardServer.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/dashboardServer.test.js]
      verify_notes:
        - Re-login 点击 → platformLogin() → launchLogin() 调用链正常
        - cookie 存活时 platformService.auto-verify 自动置 ready 状态
        - 关闭浏览器后 login status 保持 Logged in（设计意图，非 bug）
        - wf-cell-action-login testid 在 error 状态下触发 Re-login（非 platform-relogin-{pid}）

L3.7  Search 执行  [impl:done] [verify:pass] v1.4.5（2026-03-21 E2E：self-heal 修复生效，截图+Cloudflare检测+healScript 3个bug已修，LinkedIn 搜索 2→10/11 results，5+ QUALIFIED jobs）
      deps:[L1.4, L3.3, L3.4, L3.5, L3.2]
      gate_mode: explicit
      gates:[L1.4, L3.2]
      verify: L4
      test_coverage: strong
      primary:[assets/agents/job-seek/lib/searchPipeline.js, assets/agents/job-seek/lib/workflow/workflowEngine.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/searchPipeline.test.js, assets/agents/job-seek/lib/searchPipeline.e2e.test.js, assets/agents/job-seek/lib/workflow/workflowEngine.test.js]
      verify_notes:
        - BUG: analyzeFailure healScript 收到 Object 而非 string → "first argument must be of type string or Buffer"
        - Indeed search tool v8 timeout 180s（search script 未能在超时前返回结果）
        - LinkedIn: Cloudflare block → page title empty → "No job card selector found"
        - pipeline 正确识别 0 results 并跳过 generate/apply 步骤

L3.8  JD 解析匹配  [impl:done] [verify:pass] v1.4.5（2026-03-21 E2E Phase 8 验证：job listing 包含 title/company/location，JD 解析正常）
      deps:[L3.7, L2.15]
      gate_mode: explicit
      gates:[L3.7]
      verify: L4
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/tools/parseListing.js, assets/agents/job-seek/lib/tools/matchProfile.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/tools/parseListing.test.js, assets/agents/job-seek/lib/tools/matchProfile.test.js]

L3.9  Resume 生成  [impl:done] [verify:pass] v1.4.5（2026-03-21 E2E：pipeline QUALIFIED jobs 触发 generate 步骤，步骤完成（status=idle）；用户确认基于日志证据通过）
      deps:[L3.8, L2.16]
      gate_mode: auto
      verify: L4
      test_coverage: strong
      primary:[assets/agents/job-seek/lib/tools/resumeGen.js, assets/agents/job-seek/lib/tools/docxBuilder.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/tools/resumeGen.test.js, assets/agents/job-seek/lib/tools/docxBuilder.test.js]
      verify_notes:
        - E2E 验收待下次 search 有结果时：验证 savePath/documents/{jobId}/ 下存在 resume.docx
        - 单元测试已覆盖 docxBuilder 模板渲染、section mapping、文件写入

L3.10 Stuck 超时检测  [impl:done] [verify:pass] v1.4.0
      deps:[L3.7]
      gate_mode: auto
      verify: L4
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/searchPipeline.js, assets/agents/job-seek/lib/workflow/workflowEngine.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/searchPipeline.test.js, assets/agents/job-seek/lib/workflow/workflowEngine.test.js]

L3.11 Pipeline stuck 后中断  [impl:done] [verify:pass] v1.5（alert-service.e2e.test.js 13/13 pass；pipeline abort on consecutive errors + alertService.dispatch 已实现）
      deps:[L3.10]
      gate_mode: auto
      verify: L4
      test_coverage: strong
      primary:[assets/agents/job-seek/lib/searchPipeline.js]
      secondary:[assets/agents/job-seek/lib/workflow/alertService.js]
      test:[assets/agents/job-seek/lib/workflow/alert-service.e2e.test.js]
```

## L4 — 表现层（依赖所有下层）

```
L4.1  AI 对话面板  [impl:done] [verify:pass] v1.2
      deps:[L2.9, L1.9, L1.10]
      gate_mode: explicit
      gates:[L2.9, L1.9]
      verify: L4
      test_coverage: partial
      primary:[client/src/pages/agentWorkspace/index.js]
      secondary:[]
      test:[client/src/pages/agentWorkspace/index.test.js]

L4.2  Runtime Settings（provider/model）  [impl:done] [verify:pass] v1.4.0
      deps:[L4.1, L1.10]
      gate_mode: auto
      verify: L3
      test_coverage: partial
      primary:[client/src/pages/agentWorkspace/index.js, server/services/providerModelService.js]
      secondary:[client/src/config/providerModels.js]
      test:[client/src/pages/agentWorkspace/index.test.js, server/services/providerModelService.test.js, client/src/config/providerModels.test.js]

L4.3  Subtask 面板  [impl:done] [verify:pass] v1.2
      deps:[L4.1, L2.20]
      gate_mode: explicit
      gates:[L4.1]
      verify: L3
      test_coverage: partial
      primary:[client/src/pages/agentWorkspace/index.js]
      secondary:[]
      test:[client/src/pages/agentWorkspace/index.test.js]

L4.4  Preset Questions  [impl:done] [verify:pass] v1.2
      deps:[L4.1]
      gate_mode: auto
      verify: L3
      test_coverage: partial
      primary:[client/src/pages/agentWorkspace/index.js]
      secondary:[]
      test:[client/src/pages/agentWorkspace/index.test.js]

L4.5  文件上传  [impl:done] [verify:pass] v1.2
      deps:[L4.1, L2.14]
      gate_mode: explicit
      gates:[L4.1, L2.14]
      verify: L3
      test_coverage: partial
      primary:[client/src/pages/agentWorkspace/index.js]
      secondary:[]
      test:[client/src/pages/agentWorkspace/index.test.js]

L4.6  Job listing 显示（title/company/location/salary）  [impl:done] [verify:pass] v1.4.5（2026-03-21 E2E Phase 8 验证：dashboard job listing 正常显示）
      deps:[L3.7, L1.9]
      gate_mode: explicit
      gates:[L3.7, L1.9]
      verify: L4
      test_coverage: strong
      primary:[assets/agents/job-seek/lib/dashboardServer.js]
      secondary:[client/src/pages/agentWorkspace/index.js]
      test:[assets/agents/job-seek/lib/dashboardServer.test.js, assets/agents/job-seek/lib/workflow/dashboard-features.e2e.test.js]

L4.7  dashboardServer HTTP 服务（port 30003）  [impl:done] [verify:pass] v1.4.3（2026-03-21 真实验收：服务启动、dashboard 页面渲染、平台卡片、workflow 控制栏均正常）
      deps:[L3.1, L2.11]
      gate_mode: explicit
      gates:[L3.1, L2.11]
      verify: L4
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/dashboardServer.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/dashboardServer.test.js]
      verify_notes:
        - dashboardServer 绑定 localhost:30003（已从 127.0.0.1 改为 localhost）
        - Build Dashboard subtask 触发后自动 seed 3 个平台（Indeed/LinkedIn/JobBank）
        - BUG-001: 重启 agent 后 seed 无幂等判断，导致平台卡片重复（待修复）
        - /api/debug/browsers 接口已添加用于 E2E 获取 browserId
        - data-testid 已覆盖所有控制栏按钮、平台卡片、workflow cell 操作按钮

L4.8  工作流编辑器 + 启动  [impl:done] [verify:pass] v1.5（2026-03-21 真实验收：编辑器弹出、参数配置、确认后进入 RUNNING 状态）
      deps:[L4.7, L3.7]
      gate_mode: explicit
      gates:[L4.7]
      verify: L4
      test_coverage: none
      primary:[assets/agents/job-seek/lib/dashboardServer.js]
      secondary:[]
      test:[TBD]
      verify_notes:
        - wfStart() 检查 AI provider → 打开 workflowEditorModal
        - 编辑器加载搜索配置、平台列表、job 列表（3个并行 fetch）
        - 搜索配置：minScore/targetCount/maxResults/searchPreference/平台勾选
        - 生成配置：定制简历/求职信/面试准备 toggle
        - 投递：下个版本上线（locked）
        - 确认后 POST /api/workflow/:sid/start，dashboard 状态变 RUNNING

L4.9  工作流进度面板  [impl:done] [verify:pass] v1.5（2026-03-21 真实验收：面板显示 customizeProfile/search/generate/apply 4个步骤，实时日志滚动）
      deps:[L4.8]
      gate_mode: auto
      verify: L3
      test_coverage: none
      primary:[assets/agents/job-seek/lib/dashboardServer.js]
      secondary:[]
      test:[TBD]
      verify_notes:
        - workflow-progress-btn 点击弹出进度 offcanvas
        - 显示步骤状态：done(Xs)/running(Xs)/idle/skipped
        - 日志区实时显示 pipeline 输出（带时间戳）
        - 步骤/状态两个 tab 可切换

L4.10  E2E 主流程测试  [impl:done] [verify:pass] v1.4.5（2026-03-21 E2E 14/14 pass，Phase 0-8 全通过，self-heal + QUALIFIED 检测 + generate 完成）
      deps:[L4.1, L4.3, L4.5, L4.7, L4.8]
      gate_mode: explicit
      gates:[L4.7]
      verify: L4
      test_coverage: strong
      primary:[test/main-flow.spec.js, test/helpers/e2e-helpers.js]
      secondary:[]
      test:[test/main-flow.spec.js]
      last_full_pass: 2026-03-21
      verify_notes:
        - Phase 0-8 serial GATE 机制
        - 不使用 page.goto() 跳步，走完整 UI 点击流程
        - Dashboard 用第二个 browser context (port 30003)
        - 涵盖 session 创建、preset 填写、简历上传、dashboard 验证、登录、search build、workflow、结果验证

L4.11  E2E Rebuild/Self-heal 测试  [impl:done] [verify:pass] v1.4.5（2026-03-21 E2E 4/4 pass，GATE + Scenario 1-3 通过）
      deps:[L4.7, L3.7]
      gate_mode: explicit
      gates:[L4.7]
      verify: L4
      test_coverage: partial
      primary:[test/rebuild-flow.spec.js]
      secondary:[]
      test:[test/rebuild-flow.spec.js]
      verify_notes:
        - Scenario 1: search error → Rebuild → building → ready
        - Scenario 2: zero results → self-heal log 验证
        - Scenario 3: re-login → launching → verifying → verified

L4.12  E2E 跳步模式验证  [impl:done] [verify:pass] v1.4.6
      deps:[L4.10]
      gate_mode: conditional
      gate_condition: L4.10 verify:pass + 改动范围不含 Phase 1-3 文件 + 距上次 full pass < 7 天
      verify: verify:pass
      test_coverage: partial
      primary:[test/main-flow.spec.js, test/helpers/e2e-helpers.js]
      secondary:[]
      test:[test/main-flow.spec.js]
```

## L5 — stateService HTTP + SSE（依赖 L0+L1）

```
L5.1  State HTTP Read (Phase A1)  [impl:done] [verify:pass] v1.5
      deps:[L0.7]
      gate_mode: auto
      verify: L2
      test_coverage: strong
      primary:[server/routes/stateRoutes.js, server/services/stateService.js]
      secondary:[]
      test:[server/routes/stateRoutes.test.js]

L5.2  State HTTP Write (Phase A2)  [impl:done] [verify:pass] v1.5
      deps:[L0.7, L5.1]
      gate_mode: auto
      verify: L2
      test_coverage: strong
      primary:[server/routes/stateRoutes.js, server/services/stateService.js]
      secondary:[]
      test:[server/routes/stateRoutes.test.js]

L5.3  Session CRUD HTTP (Phase A3)  [impl:done] [verify:pass] v1.5
      deps:[L5.1, L5.2, L1.5]
      gate_mode: explicit
      verify: L2
      test_coverage: strong
      primary:[server/routes/stateRoutes.js, server/services/stateService.js]
      secondary:[]
      test:[server/routes/stateRoutes.test.js, server/services/stateService.test.js]

L5.4  Language Preference (Phase A4)  [impl:done] [verify:pass] v1.5
      deps:[L5.1, L5.2]
      gate_mode: auto
      verify: L2
      test_coverage: strong
      primary:[server/routes/stateRoutes.js, server/services/stateService.js]
      secondary:[]
      test:[server/routes/stateRoutes.test.js, server/services/stateService.test.js]

L5.5  SSE Subscribe Endpoint (Phase B1)  [impl:done] [verify:pass] v1.5
      deps:[L5.1]
      gate_mode: auto
      verify: L2
      test_coverage: strong
      primary:[server/routes/stateRoutes.js, server/services/stateService.js]
      secondary:[]
      test:[server/routes/stateRoutes.test.js]

L5.6  SSE Broadcast Wiring (Phase B2)  [impl:done] [verify:pass] v1.5
      deps:[L5.5]
      gate_mode: auto
      verify: L2
      test_coverage: strong
      primary:[server/services/stateService.js]
      secondary:[]
      test:[server/routes/stateRoutes.test.js]

L5.7  Agent HTTP Read on Startup (Phase C1)  [impl:done] [verify:pass] v1.5
      deps:[L5.3]
      gate_mode: explicit
      verify: L2
      test_coverage: partial
      primary:[assets/agents/job-seek/agent.js, assets/agents/job-seek/lib/stateApi.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/stateApi.test.js]

L5.8  Agent HTTP Write-through (Phase C2)  [impl:done] [verify:pass] v1.5
      deps:[L5.7, L5.3]
      gate_mode: explicit
      verify: L2
      test_coverage: partial
      primary:[assets/agents/job-seek/agent.js, assets/agents/job-seek/lib/stateApi.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/stateApi.test.js]

L5.9  Agent SSE Subscription (Phase C3)  [impl:done] [verify:pass] v1.5
      deps:[L5.6, L5.7]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[assets/agents/job-seek/agent.js, assets/agents/job-seek/lib/stateApi.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/stateApi.test.js]

L5.10 Frontend Session HTTP (Phase D1)  [impl:done] [verify:pass] v1.5
      deps:[L5.3, L4.1]
      gate_mode: explicit
      verify: L3
      test_coverage: partial
      primary:[client/src/pages/agentWorkspace/index.js, client/src/utils/api.js]
      secondary:[]
      test:[client/src/pages/agentWorkspace/index.test.js]

L5.11 savePath via stateService (Phase D2)  [impl:done] [verify:pass] v1.5
      deps:[L5.3, L5.9]
      gate_mode: explicit
      verify: L3
      test_coverage: partial
      primary:[client/src/pages/agentWorkspace/index.js, client/src/utils/api.js]
      secondary:[]
      test:[client/src/pages/agentWorkspace/index.test.js]

L5.12 Dashboard Language Sync (Phase D3)  [impl:done] [verify:pass] v1.5
      deps:[L5.4]
      gate_mode: auto
      verify: L3
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/dashboardServer.js, client/src/Layout/index.js, client/src/utils/api.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/dashboardServer.test.js]
```

---

## 反向依赖索引（含 max_verify 推荐）

改了某节点 → 查表 → 得出所有受影响节点 → 按 max_verify 决定验证深度上限。

| 节点 | 被以下节点直接依赖 | 传递影响（间接） | max_verify |
|------|-------------------|-----------------|------------|
| L0.2 | L1.1, L1.2, L2.7, L2.8 | L1.3, L1.6-L1.8, L2.1-L2.6, L2.9-L2.10, L2.17, L3.*, L4.* | L5 |
| L0.6 | L1.9 | L4.1-L4.6 | L4 |
| L0.7 | L1.10, L1.11, L2.21 | L4.1-L4.6 | L4 |
| L0.10 | L2.3 | — | L4 |
| L0.12 | L1.1, L1.5, L1.7, L2.3 | L1.6, L1.8, L2.1-L2.6, L2.11-L2.13, L3.*, L4.* | L5 |
| L0.13 | L1.5, L2.11 | L2.2, L2.5, L2.12-L2.13, L2.15, L2.20, L3.1-L3.11, L4.* | L5 |
| L0.14 | L1.4 | L2.18-L2.19, L3.1-L3.11, L4.6 | L5 |
| L0.15 | L1.4 | 同 L0.14 | L5 |
| L0.16 | L1.4, L2.19 | 同 L0.14 | L5 |
| L1.1 | L1.6, L1.7, L1.8, L2.17 | L2.1, L2.4, L2.6, L2.16, L3.8-L3.9 | L4 |
| L1.2 | L1.3, L2.14 | L4.5 | L3 |
| L1.4 | L2.18, L2.19, L3.1-L3.5, L3.7 | L3.6-L3.11, L4.6 | L5 |
| L1.5 | L1.8, L2.2, L2.5, L2.11 | L2.1, L2.6, L2.12-L2.20, L3.*, L4.* | L5 |
| L1.6 | L2.1, L2.4, L2.16, L2.17 | L2.6, L3.8-L3.9 | L4 |
| L1.8 | L2.1, L2.6 | — | L4 |
| L1.9 | L4.1, L4.6 | L4.2-L4.5 | L4 |
| L1.10 | L4.1, L4.2, L2.21 | L4.3-L4.5 | L4 |
| L1.12 | — | — | L2 |
| L2.7 | L2.9, L2.10 | L4.1, L4.2 | L4 |
| L2.9 | L4.1 | L4.2-L4.5 | L4 |
| L2.11 | L2.12, L2.13, L2.15, L2.20, L3.1 | L2.16, L3.2, L3.7-L3.11, L4.* | L5 |
| L2.14 | L4.5 | — | L3 |
| L2.15 | L2.16, L2.20, L3.2, L3.8 | L2.16, L3.7, L3.9, L4.* | L4 |
| L2.16 | L3.9 | — | L4 |
| L2.18 | L3.3, L3.4, L3.5, L3.6 | L3.7, L4.6 | L5 |
| L2.20 | L4.3 | — | L3 |
| L3.2 | L3.7 | L3.8-L3.11, L4.6 | L4 |
| L3.3 | L3.7 | L3.8-L3.11, L4.6 | L4 |
| L3.4 | L3.6, L3.7 | L3.8-L3.11, L4.6 | L5 |
| L3.5 | L3.7 | L3.8-L3.11, L4.6 | L4 |
| L3.7 | L3.8, L3.10, L4.6 | L3.9, L3.11 | L4 |
| L3.8 | L3.9 | — | L4 |
| L3.10 | L3.11 | — | L4 |
| L4.1 | L4.2, L4.3, L4.4, L4.5 | — | L4 |

**max_verify 说明**: 该节点变更时，建议验证到的最高层级。基于其传递影响中最深的 verify 级别。例如 L0.2 影响到 L3.3-L3.5（verify:L5），故 max_verify=L5。

---

## 文件 → 节点索引表

| 文件路径 | Primary 节点 | Secondary 节点 | Test 覆盖节点 |
|---------|-------------|---------------|--------------|
| `electron.js` | L0.1, L0.2, L0.3, L0.4, L0.10, L0.11, L2.3, L2.7 | — | — |
| `preload.js` | L0.1 | — | — |
| `config.js` | L0.5, L0.10, L0.12, L0.13, L1.5, L1.7, L1.8 | L2.1, L2.2, L2.3, L2.6 | — |
| `package.json` | L0.8 | — | — |
| `server/server.js` | L0.5, L0.6, L0.7 | — | — |
| `server/router.js` | L0.7, L0.16, L2.21 | — | — |
| `server/services/webSocketService.js` | L0.6 | — | — |
| `server/services/taskService.js` | L2.7, L2.8, L2.9, L2.10, L2.19 | L0.13 | — |
| `server/services/stateService.js` | L1.5, L2.5, L2.11, L2.12, L2.19, L5.1-L5.6 | — | — |
| `server/routes/stateRoutes.js` | L5.1, L5.2, L5.3, L5.4, L5.5 | — | — |
| `server/services/memoryService.js` | L1.1, L1.8, L2.1, L2.4, L2.6 | — | — |
| `server/services/fingerPrintService.js` | L0.14, L0.15, L0.16, L1.4 | L0.13 | — |
| `server/services/walletService.js` | — | L0.13 | — |
| `server/services/providerModelService.js` | L4.2 | — | — |
| `server/services/toolServiceManager.js` | L1.2, L1.3 | — | — |
| `server/services/proxyService.js` | — | — | — |
| `dbservice/index.js` | L1.1 | — | — |
| `dbservice/lib/knowledgeStore.js` | L1.1, L1.6, L1.7 | L2.1, L2.4, L2.17 | — |
| `toolService/index.js` | L1.2 | — | — |
| `toolService/package.json` | — | L0.3, L0.11 | — |
| `dbservice/package.json` | — | L0.3, L0.11 | — |
| `scripts/pre-dist.js` | L0.8, L0.9 | — | — |
| `client/src/index.js` | — | L0.1, L0.4 | — |
| `client/src/index.scss` | L0.18, L0.19 | — | — |
| `client/src/router.js` | L0.17 | — | — |
| `client/src/i18n.js` | L0.20, L0.21 | — | — |
| `client/src/utils/languages/` | L0.20, L0.21 | — | — |
| `client/src/utils/webSocket.js` | L1.9 | — | — |
| `client/src/utils/api.js` | L1.10, L5.10, L5.11, L5.12 | — | — |
| `client/src/utils/requestBase.js` | L1.10 | — | — |
| `client/src/utils/eventEmitter.js` | L1.12 | — | — |
| `client/src/store/walletStore.js` | L1.11 | — | — |
| `client/src/store/fingerPrintStore.js` | L1.11 | — | — |
| `client/src/store/pathStore.js` | L1.11 | — | — |
| `client/src/store/agentStore.js` | L1.11 | — | — |
| `client/src/Layout/index.js` | L0.17, L0.18, L5.12 | — | — |
| `client/src/pages/agentWorkspace/index.js` | L4.1, L4.2, L4.3, L4.4, L4.5, L5.10, L5.11 | L4.6 | — |
| `client/src/pages/aiAgents/index.js` | L2.21 | — | — |
| `client/src/pages/ChromeManager/index.js` | — | — | L0.16 |
| `client/src/config/providerModels.js` | — | L4.2 | — |
| `assets/agents/job-seek/agent.js` | L2.9, L2.15, L2.20, L5.7, L5.8, L5.9 | L2.14, L2.16 | — |
| `assets/agents/job-seek/lib/stateApi.js` | L5.7, L5.8, L5.9 | — | — |
| `assets/agents/job-seek/lib/core/sessionStore.js` | L2.2, L2.5, L2.11, L2.12, L2.13 | — | — |
| `assets/agents/job-seek/lib/core/browserLauncher.js` | L1.4 | — | — |
| `assets/agents/job-seek/lib/core/fileParser.js` | L2.14 | — | — |
| `assets/agents/job-seek/lib/core/knowledgeClient.js` | L2.17 | — | — |
| `assets/agents/job-seek/lib/core/masterProfileClient.js` | L2.16 | — | — |
| `assets/agents/job-seek/lib/prompts.js` | L2.15 | — | — |
| `assets/agents/job-seek/lib/toolRouter.js` | L3.2 | — | — |
| `assets/agents/job-seek/lib/searchPipeline.js` | L3.7, L3.10, L3.11 | — | — |
| `assets/agents/job-seek/lib/dashboardServer.js` | L3.6, L4.6, L5.12 | — | — |
| `assets/agents/job-seek/lib/tools/jobSearch.js` | L3.2 | — | — |
| `assets/agents/job-seek/lib/tools/parseListing.js` | L3.8 | — | — |
| `assets/agents/job-seek/lib/tools/matchProfile.js` | L3.8 | — | — |
| `assets/agents/job-seek/lib/tools/resumeGen.js` | L3.9 | — | — |
| `assets/agents/job-seek/lib/tools/docxBuilder.js` | L3.9 | — | — |
| `assets/agents/job-seek/lib/sources/indeed.js` | L3.3 | — | — |
| `assets/agents/job-seek/lib/sources/linkedin.js` | L3.4 | — | — |
| `assets/agents/job-seek/lib/sources/jobbank.js` | L3.5 | — | — |
| `assets/agents/job-seek/lib/workflow/platformService.js` | L2.18, L3.1 | L3.3, L3.4, L3.5, L3.6 | — |
| `assets/agents/job-seek/lib/workflow/platformStore.js` | L3.1 | — | — |
| `assets/agents/job-seek/lib/workflow/workflowEngine.js` | L3.7, L3.10 | — | — |
| `assets/agents/job-seek/lib/workflow/alertService.js` | — | L3.11 | — |
| **测试文件** | | | |
| `server/services/memoryService.test.js` | — | — | L1.1, L1.8, L2.1, L2.4, L2.6 |
| `server/services/stateService.test.js` | — | — | L1.5, L2.5, L2.11, L2.12, L2.19, L5.3, L5.4 |
| `server/routes/stateRoutes.test.js` | — | — | L1.5, L2.5, L5.1, L5.2, L5.3, L5.4, L5.5, L5.6 |
| `server/services/taskService.test.js` | — | — | L0.13, L2.7, L2.8, L2.9, L2.10, L2.19 |
| `server/services/fingerPrintService.test.js` | — | — | L0.14, L0.15, L0.16, L1.4 |
| `server/services/walletService.test.js` | — | — | L0.13 |
| `server/services/webSocketService.test.js` | — | — | L0.6 |
| `server/services/toolServiceManager.test.js` | — | — | L1.2, L1.3 |
| `server/services/providerModelService.test.js` | — | — | L4.2 |
| `server/services/proxyService.test.js` | — | — | UNMAPPED |
| `client/src/utils/webSocket.test.js` | — | — | L1.9 |
| `client/src/utils/api.test.js` | — | — | L1.10 |
| `client/src/utils/api.coverage.test.js` | — | — | L1.10 |
| `client/src/utils/requestBase.test.js` | — | — | L1.10 |
| `client/src/utils/eventEmitter.test.js` | — | — | L1.12 |
| `client/src/store/walletStore.test.js` | — | — | L1.11 |
| `client/src/store/fingerPrintStore.test.js` | — | — | L1.11 |
| `client/src/store/pathStore.test.js` | — | — | L1.11 |
| `client/src/store/agentStore.test.js` | — | — | L1.11 |
| `client/src/Layout/index.test.js` | — | — | L0.17, L0.18 |
| `client/src/pages/agentWorkspace/index.test.js` | — | — | L4.1, L4.2, L4.3, L4.4, L4.5, L5.10, L5.11 |
| `client/src/pages/ChromeManager/index.test.js` | — | — | L0.16 |
| `client/src/config/providerModels.test.js` | — | — | L4.2 |
| `client/src/components/taskOffcanvas/AITaskPanel.test.js` | — | — | UNMAPPED |
| `assets/agents/job-seek/lib/core/sessionStore.test.js` | — | — | L2.2, L2.5, L2.11, L2.12, L2.13 |
| `assets/agents/job-seek/lib/core/fileParser.test.js` | — | — | L2.14 |
| `assets/agents/job-seek/lib/core/knowledgeClient.test.js` | — | — | L1.6, L2.17 |
| `assets/agents/job-seek/lib/core/browserLauncher.test.js` | — | — | L1.4 |
| `assets/agents/job-seek/lib/prompts.test.js` | — | — | L2.15 |
| `assets/agents/job-seek/lib/toolRouter.test.js` | — | — | L3.2 |
| `assets/agents/job-seek/lib/searchPipeline.test.js` | — | — | L3.7, L3.10 |
| `assets/agents/job-seek/lib/searchPipeline.e2e.test.js` | — | — | L3.7 |
| `assets/agents/job-seek/lib/dashboardServer.test.js` | — | — | L3.6, L4.6, L5.12 |
| `assets/agents/job-seek/lib/tools/jobSearch.test.js` | — | — | L3.2 |
| `assets/agents/job-seek/lib/tools/parseListing.test.js` | — | — | L3.8 |
| `assets/agents/job-seek/lib/tools/matchProfile.test.js` | — | — | L3.8 |
| `assets/agents/job-seek/lib/tools/resumeGen.test.js` | — | — | L3.9 |
| `assets/agents/job-seek/lib/tools/docxBuilder.test.js` | — | — | L3.9 |
| `assets/agents/job-seek/lib/sources/indeed.test.js` | — | — | L3.3 |
| `assets/agents/job-seek/lib/sources/linkedin.test.js` | — | — | L3.4 |
| `assets/agents/job-seek/lib/sources/jobbank.test.js` | — | — | L3.5 |
| `assets/agents/job-seek/lib/workflow/platformService.test.js` | — | — | L2.18, L3.1 |
| `assets/agents/job-seek/lib/workflow/platformStore.test.js` | — | — | L3.1 |
| `assets/agents/job-seek/lib/workflow/workflowEngine.test.js` | — | — | L3.7, L3.10 |
| `assets/agents/job-seek/lib/workflow/alert-service.e2e.test.js` | — | — | L3.11 |
| `assets/agents/job-seek/lib/workflow/dashboard-features.e2e.test.js` | — | — | L4.6 |
| `test/main-flow.spec.js` | — | — | L4.1, L4.3, L4.5, L4.7, L4.8, L4.10 |
| `test/rebuild-flow.spec.js` | — | — | L3.6, L3.7, L4.7, L4.11 |
| `test/helpers/e2e-helpers.js` | — | — | L4.10, L4.11 |
| `assets/agents/job-seek/lib/stateApi.test.js` | — | — | L2.11, L2.12, L5.7, L5.8, L5.9 |
| `assets/agents/job-seek/agent.memory.test.js` | — | — | L2.20 |
| `assets/agents/shared/stateClient.test.js` | — | — | UNMAPPED |

---

## 最小验收路径推导规则 v3

### 核心规则

1. **输入**: git diff 文件列表 或 指定变更节点
2. **查文件索引（primary-first）**: 文件 → primary 关联节点集合 `S_primary`
3. **默认模式**: 仅从 `S_primary` 扩散。`--full` 模式追加 secondary 关联节点到 `S`
4. **GATE 检查**: 对 `S` 中每个节点，检查其 `gates:[]` 中所有节点是否 verify:pass。GATE FAIL → 节点 SKIP
5. **反向扩散**: 对 `S` 中每个节点，查反向索引得所有直接 + 传递依赖它的节点 → 影响集合 `A`
6. **合并**: `R = S ∪ A`（去掉 GATE FAIL 的 SKIP 节点）
7. **按层级升序排序**: L0 → L1 → L2 → L3 → L4
8. **verify 深度裁剪**: 每个节点验收到其标注的 `verify` 深度，但不超过 `max_verify`（反向索引表中查）
9. **生成验收步骤**: 按排序结果，从底层到顶层逐个验证

### 关键基础文件自动升级

以下文件被标记为 `critical_file`，命中时自动从 primary-first 升级为 full-lite 模式：

| 文件 | 原因 | 升级行为 |
|------|------|---------|
| electron.js | 系统启动入口，挂 8 节点 | primary 全纳入 + 高风险 secondary 纳入 |
| config.js | 全局配置，挂 10 节点 | primary 全纳入 + 高风险 secondary 纳入 |
| server/router.js | API 路由注册 | primary 全纳入 |
| server/services/taskService.js | 核心任务引擎 | primary 全纳入 + secondary 纳入 |
| client/src/pages/agentWorkspace/index.js | Agent UI 壳文件 | primary 全纳入 |
| assets/agents/job-seek/agent.js | Agent 编排文件 | primary 全纳入 |

规则：
- `--query file.js` 命中 critical_file → 自动切换为 full-lite
- full-lite = primary 全部 + secondary 中 verify >= L3 的节点
- 用户可用 `--primary-only` 强制仅 primary

### GATE 阻塞逻辑

```
节点 X 的 gates:[L1.8, L2.1]
  ├─ L1.8 verify:pass 且 L2.1 verify:pass → 正常验收 X
  ├─ L1.8 verify:fail → X 直接 SKIP，不执行验收
  └─ L2.1 verify:pending → X 直接 SKIP，等待 L2.1 完成后再验
```

GATE 与 deps 的区别：
- **deps** = 功能依赖（X 运行需要这些节点正常工作）
- **gates** = 验收阻塞（这些节点未通过时，X 的验收不执行）
- deps 不通过时 X 可能也会失败但会尝试执行验收
- gates 不通过时 X 直接 SKIP 不浪费验收时间

### primary-first 策略

```
默认模式（无 --full）:
  git diff → 只查 primary 映射 → 精准验收

--full 模式:
  git diff → 查 primary + secondary 映射 → 完整验收
```

目的：减少日常开发中的验收噪音。改了 `config.js` 不需要验证所有 secondary 引用它的节点，只验证 primary 核心节点。

### 基础连接节点传播

当 diff 命中带 `propagation: smoke_ui` 的节点时：
- 标准路径照常推导
- 额外建议：在最高层（L4）中选取 1-2 个核心 UI 节点做 smoke check
- smoke 节点选择：优先选 L4.1（AI 对话面板）和 L4.6（Job listing）

带 `propagation: smoke_ui` 的节点：
- L0.5（Express 端口分配）
- L0.6（WebSocket 服务启动）
- L0.7（API 路由注册）
- L1.9（WebSocket 客户端）
- L1.10（API 客户端）

### 执行时失败策略

| 情况 | 策略 |
|------|------|
| GATE 节点失败 | 阻断所有以它为 gate 的上层节点，标记 verify:skipped |
| 非 GATE 但 verify < L3 的节点失败 | 记录失败，继续同层其他节点 |
| 连续 2 个关键节点（verify >= L4）失败 | 自动建议切换到 --full 模式 |
| L0 层节点失败 | 该节点所有传递依赖者全部 SKIP |

失败后输出：
- 已通过节点列表
- 失败节点 + 原因
- 被 SKIP 的节点列表
- 建议下一步操作

### 推导示例 1：修改 `server/services/memoryService.js`

```
Step 1 — 查文件索引（primary-first）:
  memoryService.js primary → {L1.1, L1.8, L2.1, L2.4, L2.6}

Step 2 — GATE 检查:
  L1.1 gates:[L0.2] → L0.2 verify:pass → 通过
  L1.8 gates:[L1.1, L1.5] → 均 verify:pass → 通过
  L2.1 gates:[L1.8, L1.6] → 均 verify:pass → 通过
  L2.4 gates:[L1.6] → verify:pass → 通过
  L2.6 gates:[L1.8, L2.1] → 均 verify:pass → 通过
  全部通过，无 SKIP

Step 3 — 反向扩散:
  L1.1 被依赖: L1.6, L1.7, L1.8, L2.17
  L1.8 被依赖: L2.1, L2.6
  L2.1 被依赖: (无)
  L2.4 被依赖: (无)
  L2.6 被依赖: (无)

Step 4 — 合并去重:
  {L1.1, L1.6, L1.7, L1.8, L2.1, L2.4, L2.6, L2.16, L2.17, L3.8, L3.9}

Step 5 — 按层级排序 + verify 深度:
  L1: L1.1(L2) → L1.6(L2) → L1.7(L2) → L1.8(L4)
  L2: L2.1(L4) → L2.4(L4) → L2.6(L4) → L2.16(L4) → L2.17(L2)
  L3: L3.8(L4) → L3.9(L4,verify:pending 跳过)
```

### 推导示例 2：修改 `assets/agents/job-seek/lib/sources/linkedin.js`

```
Step 1 — 查文件索引（primary-first）:
  linkedin.js primary → {L3.4}

Step 2 — GATE 检查:
  L3.4 gates:[L1.4, L2.18]
  ├─ L1.4 verify:pass → 通过
  └─ L2.18 verify:pass → 通过
  通过

Step 3 — 反向扩散:
  L3.4 被依赖: L3.6, L3.7 → 传递: L3.8-L3.11, L4.6

Step 4 — 合并:
  {L3.4, L3.6, L3.7, L3.8, L3.9, L3.10, L3.11, L4.6}

Step 5 — GATE 二次检查:
  L3.6 gates:[L3.4] → L3.4 刚修改需先验 → 待 L3.4 验收后决定
  L3.7 gates:[L1.4, L3.2] → 均 verify:pass

Step 6 — 验收路径 + verify 深度:
  L3: L3.4(L5) → L3.6(L5,verify:fail 已知失败) → L3.7(L4) → L3.8(L4) → L3.10(L4) → L3.11(L4,verify:pending)
  L4: L4.6(L4)
```

---

## 编号迁移映射表

| 旧编号 | 旧名称 | 新编号 |
|--------|--------|--------|
| 1.1.1 | 主窗口加载 index.html | L0.1 |
| 1.1.2 | env 传递给 server fork | L0.2 |
| 1.1.3 | 首次启动 npm install | L0.3 |
| 1.1.4 | 初始化拦截页显示 | L0.4 |
| 1.2.1 | 端口动态分配 | L0.5 |
| 1.2.2 | WebSocket 服务启动 | L0.6 |
| 1.2.3 | API 路由注册 | L0.7 |
| 1.3.1 | dbservice 启动 | L1.1 |
| 1.3.2 | dbservice savePath 切换时重启 | L1.8 |
| 1.3.3 | toolService 启动 | L1.2 |
| 1.3.4 | toolService 健康检查 | L1.3 |
| 1.4.1 | client/build 包含在 asar | L0.8 |
| 1.4.2 | pre-dist 检查通过 | L0.9 |
| 1.4.3 | 安装目录无用户数据 | L0.10 |
| 1.4.4 | toolService/dbservice node_modules 延迟安装 | L0.11 |
| 2.1.1 | 默认 savePath 自动创建 | L0.12 |
| 2.1.2 | savePath 切换后 NeDB 重连 | L1.5 |
| 2.1.3 | savePath 切换后 knowledge.db 隔离 | L2.1 |
| 2.1.4 | savePath 切换后 sessions.json 隔离 | L2.2 |
| 2.1.5 | 升级后用户数据保留 | L2.3 |
| 2.2.1 | NeDB CRUD | L0.13 |
| 2.2.2 | knowledge.db SQLite 读写 | L1.6 |
| 2.2.3 | knowledge.db 存储到 savePath/db/ | L1.7 |
| 2.3.1 | Reset All Memory 清除 knowledgeStore | L2.4 |
| 2.3.2 | Reset All Memory 清除 sessions.json | L2.5 |
| 2.3.3 | 新 savePath 无旧记忆泄露 | L2.6 |
| 3.1.1 | ComSpec env 传递 | L2.7 |
| 3.1.2 | workspace 目录自动创建 + git init | L2.8 |
| 3.1.3 | Claude CLI 可调用 | L2.9 |
| 3.1.4 | Codex CLI 可调用 | L2.10 |
| 3.2.1 | 新建 session | L2.11 |
| 3.2.2 | 删除 session | L2.12 |
| 3.2.3 | session 列表持久化 | L2.13 |
| 3.2.4 | Onboarding 子任务完成 | L2.20 |
| 3.3.1 | 简历上传解析 | L2.14 |
| 3.3.2 | Profile Collection | L2.15 |
| 3.3.3 | masterProfile 跨 session 复用 | L2.16 |
| 3.3.4 | Profile seed from knowledgeStore | L2.17 |
| 3.4.1 | 3 平台初始化 | L3.1 |
| 3.4.2 | 搜索工具构建 | L3.2 |
| 3.4.3 | Job listing 显示 | L4.6 |
| 3.5.1 | Search 执行 | L3.7 |
| 3.5.2 | JD 解析匹配 | L3.8 |
| 3.5.3 | Resume 生成 | L3.9 |
| 3.5.4 | Stuck 超时检测 | L3.10 |
| 3.5.5 | Pipeline stuck 后中断 | L3.11 |
| 4.1.1 | Chromium 安装 | L0.14 |
| 4.1.2 | 指纹配置生成 | L0.15 |
| 4.1.3 | 浏览器环境启动 | L1.4 |
| 4.1.4 | 登录确认流程 | L2.18 |
| 4.2.1 | Indeed 登录 | L3.3 |
| 4.2.2 | LinkedIn 登录 | L3.4 |
| 4.2.3 | JobBank 登录 | L3.5 |
| 4.2.4 | Re-login 按钮功能 | L3.6 |
| 4.3.1 | 环境列表 CRUD | L0.16 |
| 4.3.2 | 单环境同时只运行一个任务 | L2.19 |
| 5.1.1 | 侧边栏导航 | L0.17 |
| 5.1.2 | 响应式布局 | L0.18 |
| 5.1.3 | 统一卡片宽度 | L0.19 |
| 5.2.1 | 中文 | L0.20 |
| 5.2.2 | 英文 | L0.21 |
| 5.3.1 | AI 对话面板 | L4.1 |
| 5.3.2 | Runtime Settings | L4.2 |
| 5.3.3 | Subtask 面板 | L4.3 |
| 5.3.4 | Preset Questions | L4.4 |
| 5.3.5 | 文件上传 | L4.5 |
| 5.4.1 | 单一 Agent 入口 | L2.21 |

---

## 脚本需求 v3：build-acceptance-graph.js

### 6 项校验（必须全部通过）

#### 校验 1：deps/gates 引用存在且无环

- 解析所有节点的 `deps:[]` 和 `gates:[]`
- 每个引用的 `Lx.y` 必须在已定义节点中存在
- 构建 DAG，检测是否有环（DFS 拓扑排序）
- 违反 → 报错并列出无效引用或环路

#### 校验 2：层级满足 max(dep 层级)+1

- 对每个节点 `Lx.y`，检查 `x == max(deps 中所有节点的层级) + 1`
- 无 deps 的节点必须是 L0
- 违反 → 报错：`L2.7 层级应为 L1（deps 最高层级为 L0）` 等
- **例外白名单**: 允许同层依赖（如 L2.12 deps:[L2.11]），此时层级 = max(deps 层级)

#### 校验 3：file mapping 引用已存在节点

- 所有 `primary:[]`、`secondary:[]`、`test:[]` 中引用的节点 ID 必须存在
- 文件索引表中的节点 ID 必须存在
- 违反 → 报错

#### 校验 4：gate_mode auto 一致性

- 对所有 `gate_mode: auto` 的节点，重新计算 auto gates（deps 中 verify >= L3 的节点）
- 如果节点显式写了 gates 且与 auto 计算不一致 → 报警
- 对所有 `gate_mode: explicit` 的节点，检查 gates 字段是否存在

#### 校验 5：生成 reverse index / file index / minimal path

- **reverse index**: 每个节点 → 直接依赖它的节点 + 传递影响
- **file index**: 每个文件 → primary/secondary/test 节点映射
- **minimal path**: 给定文件列表 → 输出受影响节点（按层级排序）+ 每个节点的 verify 深度
- **critical_file 检测**: 命中 critical_file → 自动 full-lite

#### 校验 6：TBD / UNMAPPED / orphan 报告

- `[TBD]` 统计：哪些节点的 test 字段还是 TBD
- `UNMAPPED` 统计：哪些测试文件未关联到任何节点
- `orphan` 检测：哪些源文件出现在项目中但未出现在任何节点的 primary/secondary 中
- `test_coverage: none` 统计

### CLI 用法

```bash
# 解析 + 校验 + 生成 JSON
node scripts/build-acceptance-graph.js

# 查询影响（primary-first，critical_file 自动升级为 full-lite）
node scripts/build-acceptance-graph.js --query server/services/memoryService.js

# 查询影响（包含 secondary）
node scripts/build-acceptance-graph.js --query server/services/memoryService.js --full

# 查询影响（强制仅 primary，即使命中 critical_file）
node scripts/build-acceptance-graph.js --query config.js --primary-only

# pre-dist 验证守卫节点
node scripts/build-acceptance-graph.js --verify-guards

# 输出 TBD/UNMAPPED/orphan 报告
node scripts/build-acceptance-graph.js --audit
```

### 输出格式

```json
{
  "nodes": {
    "L0.1": {
      "name": "Electron 主窗口加载",
      "layer": 0,
      "build_status": "impl:done",
      "verify_status": "verify:pass",
      "version": "v1.4.3",
      "guard": true,
      "deps": [],
      "gate_mode": "auto",
      "gates": [],
      "verify": "L1",
      "test_coverage": "none",
      "primary": ["electron.js", "preload.js"],
      "secondary": ["client/src/index.js"],
      "test": [],
      "dependedBy": []
    }
  },
  "fileIndex": {
    "electron.js": {
      "primary": ["L0.1", "L0.2", "L0.3", "L0.4", "L0.10", "L0.11", "L2.3", "L2.7"],
      "secondary": [],
      "test": [],
      "critical_file": true
    }
  },
  "reverseIndex": {
    "L0.2": {
      "directDeps": ["L1.1", "L1.2", "L2.7", "L2.8"],
      "transitive": ["L1.3", "L1.6", "..."],
      "maxVerify": "L5"
    }
  },
  "stats": {
    "impl_done": 89,
    "impl_partial": 0,
    "impl_missing": 0,
    "verify_pass": 89,
    "verify_fail": 0,
    "verify_pending": 0,
    "verify_skipped": 0,
    "guard": 5,
    "gate_mode_auto": 38,
    "gate_mode_explicit": 45,
    "test_coverage_none": 14,
    "test_coverage_partial": 61,
    "test_coverage_strong": 8,
    "tbd_test": 14,
    "unmapped_test": 3,
    "orphan_files": 0
  }
}
```

---

## 统计

### 节点统计

| 层级 | 节点数 | verify:pass | verify:fail | verify:pending | verify:skipped |
|------|--------|-------------|-------------|----------------|----------------|
| L0   | 21     | 21          | 0           | 0              | 0              |
| L1   | 12     | 12          | 0           | 0              | 0              |
| L2   | 21     | 21          | 0           | 0              | 0              |
| L3   | 11     | 11          | 0           | 0              | 0              |
| L4   | 12     | 12          | 0           | 0              | 0              |
| L5   | 12     | 12          | 0           | 0              | 0              |
| 合计 | 89     | 89          | 0           | 0              | 0              |

### 状态统计

| build_status | 数量 |
|-------------|------|
| impl:done | 89 |
| impl:partial | 0 |
| impl:missing | 0 |

| verify_status | 数量 |
|--------------|------|
| verify:pass | **89** |
| verify:T2-pass | 0 |
| verify:fail | 0 |
| verify:pending | 0 |
| verify:skipped | 0 |

| GUARD 守卫 | 5（L0.1, L0.2, L0.8, L0.10, L2.7） |
|-----------|------|

### gate_mode 统计

| gate_mode | 数量 |
|-----------|------|
| auto | 39 |
| explicit | 49 |
| conditional | 1（L4.12） |

### verify 分布

| verify 级别 | 节点数 |
|------------|--------|
| L1 | 10 |
| L2 | 37 |
| L3 | 13 |
| L4 | 24 |
| L5 | 4 |
| verify:pass（L4.12） | 1 |

### test_coverage 分布

| test_coverage | 数量 |
|--------------|------|
| none | 14 |
| partial | 61 |
| strong | 8（L3.7, L4.6, L5.1-L5.6） |

### 文件映射质量

| 类别 | 数量 |
|------|------|
| test:[TBD] 待补 | 14 |
| UNMAPPED 测试文件 | 3（proxyService.test.js, AITaskPanel.test.js, stateClient.test.js） |
| 总 primary 映射文件 | 55 |
| 总 secondary 映射文件 | 18 |
| 总 test 映射文件 | 49 |
| critical_file | 6 |
| propagation: smoke_ui | 5 |

### gates 统计

| 类别 | 数量 |
|------|------|
| gate_mode: explicit 的节点 | 49 |
| gate_mode: auto 的节点 | 39（其中 21 个 L0 无 deps） |
| gate_mode: conditional 的节点 | 1（L4.12） |
| gates 总引用数（explicit） | 59 |

## 统计表维护规则

- Dev/Coordinator 新增/删除验收图节点后，必须同步更新 summary 表
- Gatekeeper G5 检查项：统计表数字与实际节点数一致
- 后续实现 scripts/acceptance-graph-check.js 自动验证
