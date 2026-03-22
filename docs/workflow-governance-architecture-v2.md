# Workflow Governance Service — 架构设计 v2

## Context

AI Agent 协作开发中反复出现流程违规。核心结论：规则必须写在代码里，由 API 强制执行。
本文档基于初版设计 + 外部 session 评审反馈，整合为可落地的工程骨架。

---

## 核心模型：三层分离

评审反馈的首要建议：**图是规则，状态是快照，审计是事实**。三者必须分层清晰。

```
┌─────────────────────────────────────────────────────┐
│  Layer 1: Graph Definition (规则层，只读为主)          │
│  存储: JSON + NetworkX                               │
│  内容: 节点定义、deps 边、gates 策略、verify policy    │
│  变更频率: 极低（仅新增/删除节点时）                    │
│  权限: 仅 Coordinator 可变更                          │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│  Layer 2: Runtime State (运行态，高频可变)             │
│  存储: SQLite (governance.db)                        │
│  内容: 节点当前状态、会话、任务、版本号、锁            │
│  变更频率: 每次 verify-update / heartbeat / task 操作  │
│  并发保护: SQLite WAL + 事务                          │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│  Layer 3: Event Log (事件流，append-only)             │
│  存储: JSONL 文件 + SQLite audit 索引                 │
│  内容: 谁、什么时候、对什么节点、做了什么变更           │
│  特性: 不可变、可 replay、可回溯                       │
│  用途: 审计、回滚、debug、diff                         │
└─────────────────────────────────────────────────────┘
```

---

## 存储决策

| 数据类型 | 存储 | 原因 |
|---------|------|------|
| DAG 拓扑（节点+边+policy） | JSON + NetworkX | 89 节点，读多写极少 |
| 运行态（状态、会话、任务、锁） | **SQLite** | 高频可变、需事务、需并发保护、跨平台免文件锁 |
| 审计原文 | JSONL append-only | 匹配已有模式，不可变 |
| 审计索引 | SQLite audit 表 | 支持 violations 查询、时间范围过滤 |
| 版本快照 | SQLite snapshots 表 | 支持回滚 |

**为什么从 JSON 升级 SQLite（评审 #2）**：
- 已经需要版本控制、冲突处理、锁
- Windows/Unix 文件锁跨平台细节很烦（fcntl/msvcrt）
- sessions、tasks、state、idempotency 都适合 SQLite
- 仍然"零基础设施"，但比多文件 JSON 更稳

---

## 架构总览

```
agent/governance/                    ← Python 子包
    __init__.py                      ← 包标记 + 版本
    server.py                        ← FastAPI/Starlette HTTP 层 (port 30006)
    errors.py                        ← 统一异常层级 + 错误码
    db.py                            ← SQLite 连接管理 + schema migration (NEW)
    enums.py                         ← 显式枚举：VerifyStatus, BuildStatus, Role (NEW)
    project_service.py               ← 项目注册 + 隔离 + 路由
    role_service.py                  ← Principal + Session 模型 + 心跳 + 鉴权
    graph.py                         ← NetworkX DAG（规则层）
    state_service.py                 ← 运行态管理（SQLite 事务）
    gate_policy.py                   ← Gate 策略引擎（可配置 min_status）(NEW)
    impact_analyzer.py               ← 策略化影响分析（从 graph.py 拆出）(NEW)
    memory_service.py                ← 开发记忆库 (P1)
    audit_service.py                 ← 事件流 + 审计索引
    evidence.py                      ← 结构化证据模型 + 校验
    permissions.py                   ← 权限矩阵 + scope
    idempotency.py                   ← 幂等键管理 (NEW)
    event_bus.py                     ← 内部事件订阅 + webhook (NEW)
    client.py                        ← GovernanceClient SDK（含降级逻辑）(NEW)

agent/tests/
    test_governance_enums.py
    test_governance_db.py
    test_governance_project.py
    test_governance_role.py
    test_governance_graph.py
    test_governance_gate_policy.py
    test_governance_impact.py
    test_governance_state.py
    test_governance_evidence.py
    test_governance_permissions.py
    test_governance_memory.py
    test_governance_audit.py
    test_governance_idempotency.py
    test_governance_server.py

数据存储（按项目隔离）：
shared-volume/codex-tasks/state/governance/
    projects.json                    ← 项目注册表
    {project_id}/
        graph.json                   ← DAG 拓扑（Layer 1, NetworkX 序列化）
        governance.db                ← 运行态 + 审计索引 + 快照（Layer 2, SQLite）
        audit-YYYYMMDD.jsonl         ← 审计原文（Layer 3, append-only）

新依赖：
    networkx                         ← 图操作
    starlette (或 fastapi)           ← HTTP 层（评审 #9：别自造半个框架）
    uvicorn                          ← ASGI server
```

---

## 显式枚举（评审 #3：不再字符串比较）

```python
# enums.py
from enum import IntEnum, Enum

class VerifyLevel(IntEnum):
    """验证深度，用整型比较，不依赖字符串字典序"""
    L1 = 1   # 代码存在
    L2 = 2   # API 可调用
    L3 = 3   # UI 可见
    L4 = 4   # 端到端
    L5 = 5   # 真实第三方

class VerifyStatus(Enum):
    """验收状态，显式状态机"""
    PENDING   = "pending"
    TESTING   = "testing"      # 正在跑测试（新增中间态）
    T2_PASS   = "t2_pass"      # T1+T2 通过
    QA_PASS   = "qa_pass"      # E2E 通过（原 pass）
    FAILED    = "failed"
    WAIVED    = "waived"       # 人工豁免（新增）
    SKIPPED   = "skipped"      # 被 gate 跳过

class BuildStatus(Enum):
    DONE    = "impl:done"
    PARTIAL = "impl:partial"
    MISSING = "impl:missing"

class Role(Enum):
    PM          = "pm"
    DEV         = "dev"
    TESTER      = "tester"
    QA          = "qa"
    GATEKEEPER  = "gatekeeper"
    COORDINATOR = "coordinator"

class SessionStatus(Enum):
    ACTIVE       = "active"
    STALE        = "stale"
    EXPIRED      = "expired"
    DEREGISTERED = "deregistered"
```

**状态转换用显式状态机表**（不再依赖字典序比较）：

```python
# permissions.py
TRANSITION_RULES = {
    (VerifyStatus.PENDING,  VerifyStatus.T2_PASS):  {Role.TESTER},
    (VerifyStatus.T2_PASS,  VerifyStatus.QA_PASS):  {Role.QA},
    (VerifyStatus.QA_PASS,  VerifyStatus.FAILED):   set(Role),  # 任意角色
    (VerifyStatus.T2_PASS,  VerifyStatus.FAILED):   set(Role),
    (VerifyStatus.FAILED,   VerifyStatus.PENDING):  {Role.DEV},
    (VerifyStatus.PENDING,  VerifyStatus.WAIVED):   {Role.COORDINATOR},  # 人工豁免
}

FORBIDDEN = {
    (VerifyStatus.PENDING, VerifyStatus.QA_PASS),  # 禁止跳过 T2
}
```

---

## SQLite Schema (db.py)

```sql
-- 运行态：节点状态
CREATE TABLE node_state (
    project_id  TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    verify_status TEXT NOT NULL DEFAULT 'pending',
    build_status  TEXT NOT NULL DEFAULT 'impl:missing',
    evidence_json TEXT,              -- 结构化证据 JSON
    updated_by  TEXT,                -- session_id
    updated_at  TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (project_id, node_id)
);

-- 运行态：节点状态历史（event sourcing 辅助）
CREATE TABLE node_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    role        TEXT NOT NULL,
    evidence_json TEXT,
    session_id  TEXT,
    ts          TEXT NOT NULL,
    version     INTEGER NOT NULL
);

-- 会话管理
CREATE TABLE sessions (
    session_id    TEXT PRIMARY KEY,
    principal_id  TEXT NOT NULL,      -- 逻辑身份（评审 #5）
    project_id    TEXT NOT NULL,
    role          TEXT NOT NULL,
    scope_json    TEXT,               -- ["L1.*", "L2.*"]
    token_hash    TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    last_heartbeat TEXT,
    metadata_json TEXT
);

-- 任务跟踪
CREATE TABLE tasks (
    task_id      TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'created',
    related_nodes TEXT,              -- JSON array
    created_by   TEXT,               -- session_id
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- 幂等键（评审 #14）
CREATE TABLE idempotency_keys (
    idem_key     TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL       -- 24h TTL
);

-- 审计索引（原文在 JSONL，这里做查询索引）
CREATE TABLE audit_index (
    event_id    TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    event       TEXT NOT NULL,
    actor       TEXT,
    ok          INTEGER NOT NULL DEFAULT 1,
    ts          TEXT NOT NULL,
    node_ids    TEXT                 -- JSON array, for filtering
);

-- 版本快照（支持回滚）
CREATE TABLE snapshots (
    project_id  TEXT NOT NULL,
    version     INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,      -- 完整 node_state 快照
    created_at  TEXT NOT NULL,
    created_by  TEXT,
    PRIMARY KEY (project_id, version)
);

-- 索引
CREATE INDEX idx_session_principal ON sessions(principal_id, project_id);
CREATE INDEX idx_session_status ON sessions(status);
CREATE INDEX idx_audit_project_ts ON audit_index(project_id, ts);
CREATE INDEX idx_audit_ok ON audit_index(ok);
CREATE INDEX idx_idem_expires ON idempotency_keys(expires_at);
```

---

## Principal + Session 模型（评审 #5）

"谁"与"这次运行"分开：

```
┌─────────────────────┐
│ Principal            │  ← 逻辑身份（如 "tester-agent"）
│ principal_id: str    │     一个 principal 可以有多个 session
│ display_name: str    │     跨项目可复用同一 principal
└──────────┬──────────┘
           │ 1:N
┌──────────▼──────────┐
│ Session              │  ← 运行实例
│ session_id: str      │     绑定一个 project + 一个 role
│ principal_id: str    │     携带 token
│ project_id: str      │     有生命周期（心跳/过期）
│ role: Role           │
│ scope: ["L1.*"]      │
│ status: SessionStatus│
└─────────────────────┘
```

**注册接口**（评审 #6：请求体不允许传 role 到 verify-update）：

```
POST /api/role/register
  {
    "principal_id": "tester-agent",    ← 逻辑身份
    "project_id": "toolbox-client",
    "role": "tester",
    "scope": ["L1.*", "L2.*"]
  }
  → 201 { "session_id": "ses-xxx", "token": "gov-xxx", ... }

POST /api/wf/verify-update
  Header: X-Gov-Token: gov-xxx
  Header: Idempotency-Key: idem-abc123
  {
    "nodes": ["L1.1"],
    "status": "t2_pass",
    "evidence": {                      ← 结构化证据（评审 #8）
      "type": "test_report",
      "tool": "pytest",
      "summary": {"passed": 162, "failed": 0, "exit_code": 0},
      "artifact_uri": "logs/task-xxx.run.json",
      "checksum": "sha256:a1b2c3..."
    }
  }
  // role 从 token session 自动提取，不再出现在 body 中
```

---

## Gate 策略引擎（评审 #4）

从硬编码 `pass` 升级为可配置策略：

```python
# gate_policy.py
from dataclasses import dataclass
from enums import VerifyStatus

@dataclass
class GateRequirement:
    """单个 gate 的要求"""
    node_id: str
    min_status: VerifyStatus = VerifyStatus.QA_PASS   # 默认要求全绿
    policy: str = "default"          # default | release_only | waivable
    waived_by: str | None = None     # 谁豁免的

# 节点的 gates 从固定 pass 变为策略列表
# 示例：
gates = [
    GateRequirement("L1.4", min_status=VerifyStatus.T2_PASS),  # 开发阶段只要 T2
    GateRequirement("L3.2", min_status=VerifyStatus.QA_PASS),  # 发布前要全绿
    GateRequirement("L4.1", policy="release_only"),             # 仅发布时检查
]

STATUS_ORDER = {
    VerifyStatus.PENDING: 0,
    VerifyStatus.TESTING: 1,
    VerifyStatus.T2_PASS: 2,
    VerifyStatus.QA_PASS: 3,
    VerifyStatus.WAIVED: 3,   # waived 等同于 pass
}

def check_gate(requirement: GateRequirement, current_status: VerifyStatus,
               context: str = "default") -> tuple[bool, str]:
    """检查单个 gate 是否满足"""
    if requirement.policy == "release_only" and context != "release":
        return True, ""  # 非发布场景跳过
    if requirement.policy == "waivable" and requirement.waived_by:
        return True, f"waived by {requirement.waived_by}"
    if current_status == VerifyStatus.FAILED:
        return False, f"{requirement.node_id} is FAILED"
    if STATUS_ORDER.get(current_status, 0) < STATUS_ORDER.get(requirement.min_status, 0):
        return False, f"{requirement.node_id} requires {requirement.min_status.value}, got {current_status.value}"
    return True, ""
```

---

## 结构化证据模型（评审 #8）

```python
# evidence.py
from dataclasses import dataclass, field

@dataclass
class Evidence:
    """结构化证据对象，可追溯、可签名"""
    type: str               # test_report | e2e_report | error_log | commit_ref | manual_review
    producer: str            # session_id of evidence creator
    tool: str | None = None  # pytest | playwright | git | manual
    summary: dict = field(default_factory=dict)  # {"passed": 162, "failed": 0}
    artifact_uri: str | None = None              # 指向完整报告
    checksum: str | None = None                  # 内容校验
    created_at: str = ""

# 校验规则：按证据类型
EVIDENCE_VALIDATORS = {
    ("pending", "t2_pass"): {
        "required_type": "test_report",
        "validate": lambda e: e.summary.get("passed", 0) > 0 and e.summary.get("exit_code") == 0,
        "error": "test_report must have passed > 0 and exit_code == 0",
    },
    ("t2_pass", "qa_pass"): {
        "required_type": "e2e_report",
        "validate": lambda e: e.summary.get("passed", 0) > 0,
        "error": "e2e_report must have passed > 0",
    },
    ("*", "failed"): {
        "required_type": "error_log",
        "validate": lambda e: bool(e.summary.get("error") or e.artifact_uri),
        "error": "error_log must have error detail or artifact reference",
    },
    ("failed", "pending"): {
        "required_type": "commit_ref",
        "validate": lambda e: bool(e.summary.get("commit_hash")),
        "error": "commit_ref must contain commit_hash",
    },
}
```

---

## 策略化影响分析（评审 #7: impact_analyzer.py）

从 graph.py 拆出，不再硬编码单函数：

```python
# impact_analyzer.py
from dataclasses import dataclass
from enums import VerifyLevel

@dataclass
class FileHitPolicy:
    """文件命中策略"""
    match_primary: bool = True
    match_secondary: bool = False     # 评审指出 secondary 也可能关键
    match_config_glob: list[str] = None  # e.g. ["config.*", "*.env"]

@dataclass
class PropagationPolicy:
    """传播策略"""
    follow_deps: bool = True          # 下游 deps 传播
    follow_reverse_deps: bool = False # 上游反向（少见）
    propagation_tag_filter: list[str] | None = None  # smoke_ui, config_fanout

@dataclass
class VerificationPolicy:
    """验证策略"""
    mode: str = "targeted"            # smoke | targeted | full_regression
    skip_already_passed: bool = True  # 已 pass 且非直接命中 → 跳过
    respect_gates: bool = True        # gate 未满足 → 从 affected 移除（不仅 skip 标记）

@dataclass
class ImpactAnalysisRequest:
    changed_files: list[str]
    file_policy: FileHitPolicy = None
    propagation_policy: PropagationPolicy = None
    verification_policy: VerificationPolicy = None

class ImpactAnalyzer:
    def __init__(self, graph, state_db):
        self.graph = graph
        self.state_db = state_db

    def analyze(self, request: ImpactAnalysisRequest) -> dict:
        policy = request.file_policy or FileHitPolicy()
        prop = request.propagation_policy or PropagationPolicy()
        verify = request.verification_policy or VerificationPolicy()

        # Step 1: 文件 → 命中节点（按 policy 决定匹配范围）
        direct_hit = self._file_match(request.changed_files, policy)

        # Step 2: 传播（按 propagation policy）
        affected = set(direct_hit)
        if prop.follow_deps:
            for nid in direct_hit:
                affected |= self.graph.descendants(nid)

        # Step 3: 裁剪（按 verification policy）
        skipped = []
        if verify.skip_already_passed:
            for nid in list(affected):
                state = self.state_db.get_node_status(nid)
                if state == VerifyStatus.QA_PASS and nid not in direct_hit:
                    affected.discard(nid)

        if verify.respect_gates:
            for nid in list(affected):
                ok, reason = self.graph.check_gates(nid)
                if not ok:
                    affected.discard(nid)  # 移除，不仅标 skip
                    skipped.append({"node": nid, "reason": reason})

        # Step 4: 分层 + 拓扑排序
        by_phase = self._group_by_verify_level(affected)
        ordered = self._topological_filter(affected)
        test_files = self._collect_test_files(affected)

        return {
            "direct_hit": sorted(direct_hit),
            "total_affected": len(affected),
            "verification_order": ordered,
            "by_phase": by_phase,
            "skipped": skipped,
            "test_files": test_files,
            "max_verify": self._max_verify(direct_hit),
        }
```

---

## Release Profile（评审 #11）

发布门禁不再"全项目全绿"，支持 scope：

```
POST /api/wf/{project_id}/release-gate
  {
    "profile": "browser-core",          ← 可选：发布 profile 名
    "scope": ["L3.*", "L4.1", "L4.2"],  ← 可选：节点范围
    "tag": "v1.5.1",                     ← 可选：版本标签
    "path_prefix": "server/services/"    ← 可选：文件范围
  }

  → 200 | 403
  {
    "release": true/false,
    "profile": "browser-core",
    "checked_nodes": 23,
    "total_nodes": 89,
    "results": {
      "qa_pass": 21,
      "t2_pass": 1,
      "waived": 1,
      "pending": 0,
      "failed": 0
    },
    "blockers": [...]
  }
```

不传 profile/scope = 全局检查（向后兼容）。

---

## 事件订阅（评审 #14）

```python
# event_bus.py
class EventBus:
    """内部事件分发，支持同进程订阅 + webhook"""

    EVENTS = [
        "node.status_changed",
        "gate.satisfied",
        "gate.blocked",
        "release.blocked",
        "release.approved",
        "role.registered",
        "role.expired",
        "role.missing",
        "rollback.executed",
    ]

    def subscribe(self, event: str, callback: Callable): ...
    def publish(self, event: str, payload: dict): ...

    # Webhook 支持（P2）
    def register_webhook(self, url: str, events: list[str]): ...
```

集成点：
- `state_service.verify_update()` → publish `node.status_changed`
- `gate_policy.check_all_gates()` → publish `gate.satisfied` / `gate.blocked`
- `role_service.cleanup_expired()` → publish `role.expired`
- Agent 可 subscribe 替代 polling

---

## 幂等键（评审 #14）

```
所有写入 API 支持 Idempotency-Key header：

POST /api/wf/verify-update
  Header: Idempotency-Key: idem-20260321-abc123

处理逻辑：
1. 查 idempotency_keys 表
2. 已存在 → 直接返回缓存的 response（不重复执行）
3. 不存在 → 执行操作 → 写入 key + response → 返回
4. TTL 24h 自动清理
```

---

## 离线降级（评审 #10：bounded retry）

```python
# client.py
class GovernanceClient:
    def __init__(self, base_url, max_retries=5, base_delay=2, deadline_sec=120):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.deadline_sec = deadline_sec

    def verify_update(self, nodes, status, evidence):
        """严格模式：bounded retry + exponential backoff + deadline"""
        deadline = time.time() + self.deadline_sec
        for attempt in range(self.max_retries):
            if time.time() > deadline:
                return {"status": "governance_unavailable", "action": "escalate_to_coordinator"}
            try:
                return self._post("/api/wf/verify-update", {...})
            except ConnectionError:
                delay = self.base_delay * (2 ** attempt)
                time.sleep(min(delay, deadline - time.time()))
        return {"status": "governance_unavailable", "retries_exhausted": True}

    def mem_write(self, module, category, content):
        """宽松模式：本地缓存 + 恢复补推"""
        try:
            return self._post("/api/mem/write", {...})
        except ConnectionError:
            self._offline_queue.append(("mem/write", {...}))
            return {"status": "cached_locally", "queue_size": len(self._offline_queue)}
```

---

## Bootstrap 安全（评审 #12）

```
POST /api/bootstrap
  限制: 仅允许 localhost / Unix socket 调用
  行为: 一次性，执行后 endpoint 自动失效
  安全:
    - admin_secret 仅本次使用，不持久化
    - token 不写入日志
    - bootstrap 完成后返回 coordinator token，但不再接受第二次调用
    - 如需重新 bootstrap，需手动删除 projects.json 中的项目
```

---

## Memory 增强（评审 #13）

```python
@dataclass
class MemoryEntry:
    id: str
    module_id: str
    kind: str            # decision | pitfall | workaround | invariant | ownership
    content: str
    applies_when: str    # 适用条件（如 "Windows 环境" "并发 > 3 时"）
    supersedes: str | None = None  # 被哪条新记忆替代
    related_nodes: list[str] = field(default_factory=list)
    created_by: str = ""
    created_at: str = ""
    is_active: bool = True  # supersedes 后自动置 False
```

---

## Layer 语义（评审 #7）

```
layer 变为展示属性，不作为正确性硬约束。
真正的正确性保证：
  1. DAG 无环（nx.is_directed_acyclic_graph）
  2. Gate policy 满足
  3. 状态转换合法

node-create 时：
  - layer 建议值 = max(deps.layer) + 1（显示为 warning 如不符）
  - 不阻断创建
  - 同层依赖、跨层软依赖均允许
```

---

## HTTP 层（评审 #9）

```python
# server.py — 用 Starlette（轻量 ASGI）
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.middleware import Middleware

app = Starlette(
    routes=[
        Route("/api/bootstrap", bootstrap_handler, methods=["POST"]),
        Route("/api/project/register", project_register, methods=["POST"]),
        Route("/api/role/register", role_register, methods=["POST"]),
        Route("/api/role/heartbeat", role_heartbeat, methods=["POST"]),
        Route("/api/wf/{project_id}/verify-update", verify_update, methods=["POST"]),
        Route("/api/wf/{project_id}/release-gate", release_gate, methods=["POST"]),
        Route("/api/wf/{project_id}/node/{node_id}", get_node, methods=["GET"]),
        Route("/api/wf/{project_id}/summary", get_summary, methods=["GET"]),
        Route("/api/wf/{project_id}/impact", impact_analysis, methods=["GET"]),
        Route("/api/wf/{project_id}/export", export_graph, methods=["GET"]),
        Route("/api/wf/{project_id}/rollback", rollback, methods=["POST"]),
        Route("/api/mem/{project_id}/write", mem_write, methods=["POST"]),
        Route("/api/mem/{project_id}/query", mem_query, methods=["GET"]),
        Route("/api/audit/{project_id}/log", audit_log, methods=["GET"]),
        Route("/api/audit/{project_id}/violations", audit_violations, methods=["GET"]),
    ],
    middleware=[
        Middleware(AuthMiddleware),         # token 鉴权
        Middleware(IdempotencyMiddleware),  # 幂等键
        Middleware(RequestIdMiddleware),    # request_id 注入
        Middleware(AuditMiddleware),        # 自动审计记录
    ],
)
```

领域逻辑完全纯 Python，HTTP 层只做路由+中间件+序列化。

---

## 工作流程图

### 流程 1：开发任务流程

```
Coordinator 创建任务
    │
    ▼
POST /api/wf/{pid}/task-create          ← Coordinator session
    │                                      (role 从 token 提取)
    ▼
Dev 领取前查记忆
GET /api/mem/{pid}/query?module=X       ← Dev session
    │
    ▼
Dev 开发 → 完成 → 提交
    │
    ▼
POST /api/mem/{pid}/write               ← Dev 写入 pattern/pitfall
    │
    ▼
Tester 跑 T1+T2
    │
    ▼
POST /api/wf/{pid}/verify-update        ← Tester session
  Header: X-Gov-Token + Idempotency-Key
  Body: {nodes, status:"t2_pass", evidence:{type:"test_report",...}}
    │
    ▼
  鉴权 → scope → 权限矩阵 → 证据校验 → gate 策略
    │
    ▼
  节点 → t2_pass ✅ (可 merge)
  EventBus → publish("node.status_changed")
    │
    ▼
(发布前) QA 跑 E2E
    │
    ▼
POST /api/wf/{pid}/verify-update        ← QA session
  Body: {nodes, status:"qa_pass", evidence:{type:"e2e_report",...}}
    │
    ▼
  节点 → qa_pass ✅ (可发布)
```

### 流程 2：Bug 修复流程

```
发现 Bug
    │
    ▼
POST /api/wf/{pid}/verify-update        ← 任意角色 session
  Body: {nodes:["L3.7"], status:"failed",
         evidence:{type:"error_log", summary:{error:"timeout..."}}}
    │
    ▼
  L3.7 → FAILED
  EventBus → publish("node.status_changed")
  自动计算: descendants("L3.7") → 下游 gate 可能失效
    │
    ▼
Dev 修复 + 提交
    │
    ▼
POST /api/wf/{pid}/verify-update        ← Dev session
  Body: {nodes:["L3.7"], status:"pending",
         evidence:{type:"commit_ref", summary:{commit_hash:"a1b2c3d"}}}
    │
    ▼
  L3.7 → PENDING → 重走 Tester → T2_PASS → QA → QA_PASS
```

### 流程 3：创建新节点

```
PM 定义 PRD → [TREE:ADD] 规格
    │
    ▼
POST /api/wf/{pid}/node-create          ← Coordinator session
  Body: {
    id: "L2.22", title: "新功能",
    deps: ["L1.4", "L0.16"],
    gate_mode: "auto",
    verify_level: 4,                     ← 整型！不是字符串
    gates: [                             ← 策略化 gate（评审 #4）
      {"node_id": "L1.4", "min_status": "t2_pass"},
    ]
  }
    │
    ▼
  1. 验证 deps 存在
  2. DAG 无环检查（不强制 layer 规则）
  3. gate_mode:auto → 自动推导 gate 策略
  4. 初始化: PENDING + impl:missing
  5. audit.record(node_create)
  6. EventBus → publish("node.created")
```

### 流程 4：最小验证路径

```
git diff → changed_files
    │
    ▼
GET /api/wf/{pid}/impact
  ?files=stateService.js,config.js
  &file_policy=primary+secondary        ← 可配（评审 #7）
  &propagation=deps                      ← 可配
  &verification=targeted                 ← 可配
    │
    ▼
ImpactAnalyzer.analyze()
  Step 1: 文件命中（按 file_policy 匹配 primary + secondary）
  Step 2: 传播（按 propagation_policy 展开 descendants）
  Step 3: 裁剪（已 pass 非直接命中 → 移除；gate 未满足 → 移除）
  Step 4: 按 VerifyLevel 分层 + 拓扑排序
    │
    ▼
  返回: {
    direct_hit, total_affected,
    verification_order (拓扑序),
    by_phase: {T1:[...], T2:[...], T3:[...]},
    skipped: [{node, reason}],
    test_files,
    max_verify: 4
  }
```

### 流程 5：发布门禁

```
POST /api/wf/{pid}/release-gate
  Body: {profile:"browser-core", scope:["L3.*","L4.1","L4.2"]}
    │
    ▼
  遍历 scope 内节点 → 检查每个的 gate policy（context="release"）
    │
  ┌─┴──┐
  ▼    ▼
 200  403
 全绿  有 blocker（含 release_only gate 未通过的）
```

---

## 实现阶段

### Phase 1 — 模型收敛 + 核心骨架
1. `enums.py` — 显式枚举（VerifyStatus, VerifyLevel, Role, SessionStatus）
2. `errors.py` — 统一异常层级
3. `db.py` — SQLite schema + migration + 连接管理
4. `models.py` — 数据结构（Evidence, GateRequirement, MemoryEntry, ImpactRequest）
5. `role_service.py` — Principal + Session + 心跳 + 鉴权
6. `graph.py` — NetworkX DAG（规则层，含 markdown 导入容错）
7. `gate_policy.py` — 可配置 gate 策略引擎
8. `permissions.py` — 枚举状态机 + scope 检查
9. `evidence.py` — 结构化证据 + 校验
10. `idempotency.py` — 幂等键管理
11. `audit_service.py` — JSONL + SQLite 索引
12. `state_service.py` — verify_update + release_gate + rollback
13. `impact_analyzer.py` — 策略化影响分析
14. `event_bus.py` — 内部事件分发
15. `server.py` — Starlette + 中间件
16. `client.py` — GovernanceClient SDK（bounded retry + 降级）
17. 14 个测试文件

### Phase 2 — Memory + Task + Export
18. `memory_service.py` — 增强 memory（kind, applies_when, supersedes）
19. Task CRUD
20. Export（JSON / Mermaid / Markdown）
21. Webhook 支持

### Phase 3 — 运维增强
22. Release profile
23. 审计报告生成
24. 快照压缩策略
25. 监控 dashboard API

---

## 新依赖

```
networkx       ← 图操作（纯 Python）
starlette      ← HTTP 层（轻量 ASGI）
uvicorn        ← ASGI server
redis          ← Redis 客户端（session 缓存 / 锁 / 幂等 / pub/sub）
```

SQLite 为 Python 内置，无额外依赖。

---

## Docker 部署

### 四层架构

```
┌─────────────────────────────────────────────────────┐
│ L1: governance 容器                                  │
│   uvicorn agent.governance.server:app --port 30006   │
│   /api/bootstrap, /api/role/*, /api/wf/*,            │
│   /api/mem/*, /api/audit/*                           │
└──────────┬──────────────┬───────────────────────────┘
           │              │
┌──────────▼──────┐  ┌───▼──────────────────────────┐
│ L2: Redis 容器   │  │ L3: 持久化卷                  │
│ session 缓存     │  │ shared-volume/.../governance/ │
│ 分布式锁         │  │   projects.json              │
│ 幂等键 (NX+TTL) │  │   {project_id}/              │
│ Pub/Sub 通知     │  │     governance.db (SQLite)   │
│                  │  │     graph.json               │
│ 非唯一真相源     │  │     audit-*.jsonl            │
└─────────────────┘  └───┬──────────────────────────┘
                         │
                    ┌────▼────────────────────────────┐
                    │ L4: 宿主机 workspace 挂载        │
                    │ bootstrap 读 acceptance-graph.md  │
                    │ impact analysis 读源码文件        │
                    └─────────────────────────────────┘
```

### 数据流：双写 SQLite + Redis

```
写路径:
  API 请求 → SQLite 事务写入（持久化真相）→ Redis 写入（热缓存）
  任一写入失败 → SQLite 回滚 / Redis 跳过（降级）

读路径:
  API 请求 → Redis 查询
  命中 → 直接返回
  未命中 → SQLite 查询 → 回填 Redis → 返回

Redis 挂了:
  退化为纯 SQLite 模式（性能降但不停服）
  恢复后自动回填热数据
```

### Redis 职责边界

| 数据 | SQLite（真相源） | Redis（热缓存） |
|------|-----------------|-----------------|
| sessions | ✅ 持久化 | ✅ Hash + TTL 自动过期 |
| heartbeat | ✅ last_heartbeat 字段 | ✅ Key TTL 做超时检测 |
| 幂等键 | ❌ 不再存 SQLite | ✅ SET NX + 24h TTL |
| 分布式锁 | ❌ | ✅ SETNX + TTL |
| pub/sub 通知 | ❌ | ✅ Redis Pub/Sub |
| node_state | ✅ 核心状态 | ❌ 写频率不高不缓存 |
| node_history | ✅ 事件溯源 | ❌ |
| audit_index | ✅ 查询索引 | ❌ |
| snapshots | ✅ 回滚 | ❌ |

### redis_client.py — 连接管理 + 降级

```python
class RedisClient:
    """Redis 客户端，内置降级到 SQLite 的逻辑"""

    def __init__(self, url="redis://redis:6379/0"):
        self._url = url
        self._client = None
        self._available = False

    def connect(self):
        try:
            self._client = redis.Redis.from_url(self._url, decode_responses=True)
            self._client.ping()
            self._available = True
        except (redis.ConnectionError, redis.TimeoutError):
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def get_or_fallback(self, key, fallback_fn):
        """Redis 命中返回，未命中或不可用时调 fallback_fn"""
        if self._available:
            try:
                val = self._client.get(key)
                if val is not None:
                    return json.loads(val)
            except redis.RedisError:
                self._available = False
        return fallback_fn()

    def set_cache(self, key, value, ttl_sec=3600):
        """写入 Redis 缓存，失败静默"""
        if self._available:
            try:
                self._client.setex(key, ttl_sec, json.dumps(value))
            except redis.RedisError:
                self._available = False

    def check_idempotency(self, key) -> dict | None:
        """幂等键检查：Redis SET NX"""
        if not self._available:
            return None
        try:
            val = self._client.get(f"idem:{key}")
            return json.loads(val) if val else None
        except redis.RedisError:
            return None

    def store_idempotency(self, key, response, ttl_sec=86400):
        if self._available:
            try:
                self._client.setex(f"idem:{key}", ttl_sec, json.dumps(response))
            except redis.RedisError:
                pass

    def acquire_lock(self, name, ttl_sec=30) -> bool:
        if not self._available:
            return True  # 降级：无锁（单实例 SQLite WAL 够用）
        try:
            return bool(self._client.set(f"lock:{name}", "1", nx=True, ex=ttl_sec))
        except redis.RedisError:
            return True

    def release_lock(self, name):
        if self._available:
            try:
                self._client.delete(f"lock:{name}")
            except redis.RedisError:
                pass

    def publish(self, channel, message):
        if self._available:
            try:
                self._client.publish(channel, json.dumps(message))
            except redis.RedisError:
                pass
```

### Dockerfile.governance

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY agent/ agent/
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 30006

CMD ["uvicorn", "agent.governance.server:app", \
     "--host", "0.0.0.0", "--port", "30006"]
```

### docker-compose.yml

```yaml
version: "3.8"

services:
  governance:
    build:
      context: .
      dockerfile: Dockerfile.governance
    ports:
      - "30006:30006"
    volumes:
      - governance-data:/app/shared-volume/codex-tasks/state/governance
      - ${WORKSPACE_PATH:-./}:/workspace:ro    # L4: 宿主机 workspace
    environment:
      - GOVERNANCE_PORT=30006
      - REDIS_URL=redis://redis:6379/0
      - SHARED_VOLUME_PATH=/app/shared-volume
    depends_on:
      redis:
        condition: service_healthy
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis-data:/data
    command: redis-server --appendonly yes --maxmemory 128mb --maxmemory-policy allkeys-lru
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 3
    restart: unless-stopped

volumes:
  governance-data:
    driver: local
  redis-data:
    driver: local
```

### 启动流程

```bash
# 1. 构建并启动
docker compose up -d

# 2. Bootstrap（首次使用）
curl -X POST http://localhost:30006/api/bootstrap \
  -d '{"project_id":"toolbox-client",
       "graph_source":"/workspace/acceptance-graph.md",
       "coordinator":{"principal_id":"coord","admin_secret":"xxx"}}'

# 3. 检查状态
docker compose ps
curl http://localhost:30006/api/wf/toolbox-client/summary
```

---

## 验证方式

```bash
# 1. 运行治理服务测试
python -m unittest discover -s agent/tests -p "test_governance_*.py" -v

# 2. 全量回归
python -m unittest discover -s agent/tests -p "test_*.py" -v

# 3. 启动服务
uvicorn agent.governance.server:app --port 30006

# 4. Bootstrap + 端到端验证
curl -X POST http://localhost:30006/api/bootstrap \
  -d '{"project_id":"toolbox-client","graph_source":"path/to/graph.md","coordinator":{"principal_id":"coord","admin_secret":"xxx"}}'

curl -X POST http://localhost:30006/api/role/register \
  -d '{"principal_id":"tester-001","project_id":"toolbox-client","role":"tester"}'

curl -X POST http://localhost:30006/api/wf/toolbox-client/verify-update \
  -H "X-Gov-Token: gov-xxx" -H "Idempotency-Key: test-001" \
  -d '{"nodes":["L1.1"],"status":"t2_pass","evidence":{"type":"test_report","summary":{"passed":162,"failed":0,"exit_code":0}}}'

curl http://localhost:30006/api/wf/toolbox-client/summary
curl "http://localhost:30006/api/wf/toolbox-client/impact?files=stateService.js"
curl "http://localhost:30006/api/wf/toolbox-client/export?format=mermaid"
```
