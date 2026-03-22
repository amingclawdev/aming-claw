# AI Agent Integration Guide — Governance Service

本文档面向 **AI Agent 开发者和 Agent 本身**，说明如何接入治理服务、遵循工作流规则、正确使用 API。

---

## 你是谁？

治理服务为每个 Agent 分配一个明确的角色。你只能执行你角色允许的操作。

| 角色 | 职责 | 你能做什么 | 你不能做什么 |
|------|------|-----------|-------------|
| **Coordinator** | 编排流程 | 分配/撤销角色、创建任务、导入图、回滚 | 不能直接改代码、不能跑测试 |
| **Dev** | 编写代码 | 标记 failed→pending（修复后）、写入开发记忆 | 不能标记 T2-pass、不能标记 QA-pass |
| **Tester** | 跑 T1+T2 测试 | 标记 pending→T2-pass | 不能标记 QA-pass、不能分配角色 |
| **QA** | 跑 E2E 测试 | 标记 T2-pass→QA-pass | 不能标记 T2-pass、不能分配角色 |
| **Gatekeeper** | 发布审批 | 执行 gate-check | 不能改代码、不能跑测试 |

**规则是代码强制的，不是建议。** 越权操作会被 403 拒绝并记入审计日志。

---

## 接入流程

### Step 1: 获取 Token

你不能自己注册。Token 由人类或 Coordinator 分配给你。

```
人类运行 init_project.py → 获得 Coordinator Token
                              │
Coordinator Agent 启动 ←──── 人类注入 Token
                              │
Coordinator 调 /api/role/assign → 获得 Tester/Dev/QA Token
                              │
各 Agent 启动 ←────────────── Coordinator 分发 Token
```

**作为 Agent，你在启动时会收到一个 Token（通过环境变量或初始化消息）。保持这个 Token，每次 API 调用都带上它。**

### Step 2: 每次 API 调用携带 Token

```
Header: X-Gov-Token: gov-<your-token>
Header: Content-Type: application/json
```

Token 中已包含你的角色信息，请求体中 **不需要也不允许** 传 `role` 字段。

### Step 3: 保持心跳

每 60 秒发送一次心跳，否则你的 session 会变成 stale (180s) 然后 expired (600s)。

```
POST http://localhost:30006/api/role/heartbeat
Header: X-Gov-Token: gov-<your-token>
Body: {"project_id": "<pid>", "status": "idle"}
```

---

## API 速查表

### 所有角色通用

| 操作 | 方法 | 路径 | 说明 |
|------|------|------|------|
| 心跳 | POST | `/api/role/heartbeat` | 每 60s 调一次 |
| 查看摘要 | GET | `/api/wf/{pid}/summary` | 各状态节点数 |
| 查看节点 | GET | `/api/wf/{pid}/node/{nid}` | 单节点详情 |
| 影响分析 | GET | `/api/wf/{pid}/impact?files=a.js,b.js` | 文件变更影响 |
| 查记忆 | GET | `/api/mem/{pid}/query?module=X` | 查关联开发记忆 |
| 写记忆 | POST | `/api/mem/{pid}/write` | 写入 pattern/pitfall |

### Coordinator 专属

| 操作 | 方法 | 路径 | 说明 |
|------|------|------|------|
| 分配角色 | POST | `/api/role/assign` | 给其他 Agent 发 token |
| 撤销角色 | POST | `/api/role/revoke` | 撤销某 Agent 的 session |
| 查看团队 | GET | `/api/role/{pid}/sessions` | 所有活跃 session |
| 导入图 | POST | `/api/wf/{pid}/import-graph` | 从 markdown 导入验收图 |
| 更新状态 | POST | `/api/wf/{pid}/verify-update` | 代其他角色提交状态变更 |
| 发布门禁 | POST | `/api/wf/{pid}/release-gate` | 检查是否可发布 |
| 回滚 | POST | `/api/wf/{pid}/rollback` | 回滚到快照版本 |
| 导出图 | GET | `/api/wf/{pid}/export?format=mermaid` | 导出可视化图 |

### Tester

| 操作 | 方法 | 路径 | Body |
|------|------|------|------|
| 标记 T2-pass | POST | `/api/wf/{pid}/verify-update` | 见下方示例 |
| 标记 failed | POST | `/api/wf/{pid}/verify-update` | 见下方示例 |

### QA

| 操作 | 方法 | 路径 | Body |
|------|------|------|------|
| 标记 QA-pass | POST | `/api/wf/{pid}/verify-update` | 见下方示例 |
| 标记 failed | POST | `/api/wf/{pid}/verify-update` | 见下方示例 |

### Dev

| 操作 | 方法 | 路径 | Body |
|------|------|------|------|
| 修复后恢复 pending | POST | `/api/wf/{pid}/verify-update` | 见下方示例 |
| 标记 failed | POST | `/api/wf/{pid}/verify-update` | 见下方示例 |

---

## verify-update 请求示例

### Tester: pending → T2-pass

```json
POST /api/wf/my-app/verify-update
Header: X-Gov-Token: gov-<tester-token>
Header: Idempotency-Key: tester-001-L0.1-t2-20260322

{
  "nodes": ["L0.1", "L0.2"],
  "status": "t2_pass",
  "evidence": {
    "type": "test_report",
    "tool": "pytest",
    "summary": {
      "passed": 162,
      "failed": 0,
      "exit_code": 0
    },
    "artifact_uri": "logs/test-run-20260322.json"
  }
}
```

### QA: T2-pass → QA-pass

```json
POST /api/wf/my-app/verify-update
Header: X-Gov-Token: gov-<qa-token>

{
  "nodes": ["L0.1"],
  "status": "qa_pass",
  "evidence": {
    "type": "e2e_report",
    "tool": "playwright",
    "summary": {
      "passed": 14,
      "failed": 0
    },
    "artifact_uri": "test/main-flow.spec.js"
  }
}
```

### Dev: failed → pending (修复后)

```json
POST /api/wf/my-app/verify-update
Header: X-Gov-Token: gov-<dev-token>

{
  "nodes": ["L3.7"],
  "status": "pending",
  "evidence": {
    "type": "commit_ref",
    "tool": "git",
    "summary": {
      "commit_hash": "a1b2c3d4e5f6a7b8"
    }
  }
}
```

### 任意角色: 标记 failed

```json
POST /api/wf/my-app/verify-update
Header: X-Gov-Token: gov-<any-token>

{
  "nodes": ["L3.7"],
  "status": "failed",
  "evidence": {
    "type": "error_log",
    "summary": {
      "error": "Search timeout after 180s, no results returned"
    },
    "artifact_uri": "logs/error-20260322.log"
  }
}
```

---

## 证据要求

每次状态变更必须附带结构化证据，否则被 400 拒绝。

| 转换 | 证据类型 | 必需字段 |
|------|---------|---------|
| pending → t2_pass | `test_report` | `summary.passed > 0`, `summary.exit_code == 0` |
| t2_pass → qa_pass | `e2e_report` | `summary.passed > 0` |
| * → failed | `error_log` | `summary.error` 或 `artifact_uri` |
| failed → pending | `commit_ref` | `summary.commit_hash`（7-40 位 hex） |
| pending → waived | `manual_review` | 无结构要求（仅 coordinator） |

**Evidence 对象完整字段：**

```json
{
  "type": "test_report",        // 必填：证据类型
  "tool": "pytest",             // 可选：工具名
  "summary": {},                // 必填：关键数据
  "artifact_uri": "path/...",   // 可选：完整报告路径
  "checksum": "sha256:..."      // 可选：校验和
}
```

---

## 状态流转图

```
  PENDING ──→ TESTING ──→ T2_PASS ──→ QA_PASS
    │  ↑         │           │           │
    │  │         ↓           ↓           ↓
    │  └───── FAILED ←───────┘───────────┘
    │
    └──→ WAIVED (仅 coordinator)

  禁止路径：PENDING → QA_PASS（不可跳过 T2）
```

---

## Gate 机制

某些节点有 gate 前置条件。如果 gate 节点未达标，你的 verify-update 会被 403 拒绝。

```json
// 403 响应示例
{
  "error": "gate_unsatisfied",
  "message": "Gate prerequisites not met for L1.1",
  "details": {
    "node_id": "L1.1",
    "unsatisfied_gates": [
      {"node_id": "L0.2", "reason": "L0.2 requires qa_pass, got pending"}
    ]
  }
}
```

**你应该做什么：** 先确保上游 gate 节点通过验证，再验证下游节点。用拓扑顺序工作。

---

## Scope 限制

注册时 Coordinator 可能给你设了 scope（如 `["L0.*", "L1.*"]`）。操作 scope 外的节点会被 403。

```json
// 403 响应示例
{
  "error": "scope_violation",
  "message": "Node 'L3.1' is outside session scope ['L0.*', 'L1.*']"
}
```

**你应该做什么：** 只操作你 scope 内的节点。如果需要操作 scope 外节点，联系 Coordinator 扩展 scope 或让其他 Agent 操作。

---

## 幂等性

所有写入操作支持 `Idempotency-Key` 头。网络超时后可以安全重试。

```
Header: Idempotency-Key: tester-001-L0.1-t2-20260322
```

- 同一个 key 的第二次请求直接返回缓存结果，不重复执行
- Key 有效期 24 小时
- 建议格式：`{principal}-{node}-{action}-{date}`

---

## 开发记忆

完成任务后，写入你的经验供其他 Agent 参考。

### 写入记忆

```json
POST /api/mem/my-app/write
Header: X-Gov-Token: gov-<your-token>

{
  "module_id": "stateService",
  "kind": "pitfall",
  "content": "Windows worktree 下 cp 命令不可靠，用 cat > 替代",
  "applies_when": "Windows 环境 + git worktree",
  "related_nodes": ["L5.1", "L5.2"]
}
```

### 查询记忆（领取任务前）

```
GET /api/mem/my-app/query?module=stateService
GET /api/mem/my-app/query?kind=pitfall
GET /api/mem/my-app/query?node=L5.1
```

**Memory kind 类型：**

| Kind | 用途 |
|------|------|
| `pattern` | 设计模式、架构决策 |
| `pitfall` | 踩坑记录、已知问题 |
| `workaround` | 临时解决方案 |
| `decision` | 为什么选了 A 而不是 B |
| `invariant` | 不可违反的约束 |
| `ownership` | 谁负责什么模块 |

---

## 影响分析（任务前必查）

领取任务前，查询你将修改的文件会影响哪些节点：

```
GET /api/wf/my-app/impact?files=server/services/stateService.js,config.js
```

响应告诉你：
- `direct_hit`: 直接受影响的节点
- `verification_order`: 拓扑排序的验证顺序
- `test_files`: 需要跑的测试文件
- `max_verify`: 最高需要验证到什么级别
- `skipped`: 因 gate 未满足而跳过的节点

---

## 错误处理

| HTTP 状态 | 错误码 | 你应该做什么 |
|-----------|--------|-------------|
| 400 `invalid_request` | 请求格式错误 | 检查必填字段 |
| 400 `invalid_evidence` | 证据不合格 | 检查证据类型和 summary 字段 |
| 400 `node_not_found` | 节点不存在 | 检查节点 ID |
| 401 `auth_required` | 未提供 token | 添加 X-Gov-Token header |
| 401 `token_expired` | Token 过期 | 联系 Coordinator 获取新 token |
| 403 `permission_denied` | 角色无权 | 你不能执行这个操作，这是正确的拒绝 |
| 403 `scope_violation` | 超出 scope | 操作你 scope 内的节点 |
| 403 `gate_unsatisfied` | 上游未通过 | 先完成上游节点验证 |
| 403 `forbidden_transition` | 禁止的转换 | 按正确路径走（不能跳过 T2） |
| 409 `conflict` | 并发冲突 | 用 Idempotency-Key 重试 |
| 503 `role_unavailable` | 缺少必要角色 | 等待对应角色 Agent 上线 |

**关键原则：403 不是 bug，是系统在保护流程正确性。不要试图绕过。**

---

## Coordinator 专属操作

### 分配角色

```json
POST /api/role/assign
Header: X-Gov-Token: gov-<coordinator-token>

{
  "project_id": "my-app",
  "principal_id": "tester-001",
  "role": "tester",
  "scope": ["L0.*", "L1.*", "L2.*"]
}
```

响应包含该 Agent 的 token，你需要将 token 传递给对应的 Agent。

### 撤销角色

```json
POST /api/role/revoke
Header: X-Gov-Token: gov-<coordinator-token>

{
  "project_id": "my-app",
  "session_id": "ses-xxx"
}
```

### 查看团队状态

```
GET /api/role/my-app/sessions
```

### 发布前检查

```json
POST /api/wf/my-app/release-gate

{
  "scope": ["L3.*", "L4.*"],
  "profile": "browser-core"
}
```

200 = 可发布，403 = 有 blocker（返回清单）。

---

## 典型工作流

### Dev 修复 Bug

```
1. GET  /api/mem/{pid}/query?node=L3.7        ← 查关联记忆
2. GET  /api/wf/{pid}/impact?files=xxx.js     ← 影响分析
3. (编写代码、提交 commit)
4. POST /api/wf/{pid}/verify-update            ← 标记 failed→pending
   Body: {nodes:["L3.7"], status:"pending",
          evidence:{type:"commit_ref", summary:{commit_hash:"abc123"}}}
5. POST /api/mem/{pid}/write                   ← 写入修复经验
   Body: {module_id:"searchPipeline", kind:"pitfall", ...}
```

### Tester 验证任务

```
1. GET  /api/wf/{pid}/summary                  ← 看哪些节点 pending
2. (运行测试)
3. POST /api/wf/{pid}/verify-update            ← 标记 T2-pass
   Body: {nodes:["L0.1","L0.2"], status:"t2_pass",
          evidence:{type:"test_report", summary:{passed:162, failed:0, exit_code:0}}}
```

### Coordinator 编排发布

```
1. GET  /api/role/{pid}/sessions               ← 确认团队就位
2. GET  /api/wf/{pid}/summary                  ← 确认状态
3. POST /api/wf/{pid}/release-gate             ← 发布门禁检查
   Body: {scope:["L3.*","L4.*"]}
4. 如果 403 → 查看 blockers → 安排对应角色处理
5. 如果 200 → 可以发布
```

---

## 治理服务不可达时

| API 类型 | 行为 |
|---------|------|
| verify-update | **阻塞等待**（bounded retry, 最多 120s）— 状态变更不可绕过 |
| release-gate | **阻塞等待** — 发布门禁不可跳过 |
| mem/write | 本地缓存，服务恢复后补推 |
| mem/query | 返回空，不阻塞工作 |

**永远不要在治理服务不可达时自行标记节点状态。**
