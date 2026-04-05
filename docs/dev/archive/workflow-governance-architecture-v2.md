---
status: archived
superseded_by: governance/design-spec-full.md
archived_date: 2026-04-05
historical_value: "Refined workflow governance with acceptance graph"
do_not_use_for: "governance architecture decisions"
---

# Workflow Governance Service — Architecture Design v2

> **2026-03-26 Update:** Governance is now the sole task management system. The legacy file-based coordinator/executor pipeline (coordinator.py, executor.py, backends.py, task_state.py and 20 other modules) has been completely removed. All task lifecycle management, workflow orchestration, and auditing is done through the Governance API.

## Context

Process violations have repeatedly occurred during AI Agent collaborative development. Core conclusion: rules must be written in code and enforced by APIs.
This document integrates the initial design plus external session review feedback into an implementable engineering skeleton.

---

## Core Model: Three-Layer Separation

The primary recommendation from the review: **the graph is the rules, state is a snapshot, audit is the facts**. The three must be clearly separated into layers.

```
┌─────────────────────────────────────────────────────┐
│  Layer 1: Graph Definition (rule layer, mostly read) │
│  Storage: JSON + NetworkX                            │
│  Contents: node definitions, dep edges, gate         │
│    policies, verify policy                           │
│  Change frequency: Very low (only when adding/       │
│    removing nodes)                                   │
│  Permissions: Only Coordinator can modify            │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│  Layer 2: Runtime State (runtime, high-frequency     │
│    changes)                                          │
│  Storage: SQLite (governance.db)                     │
│  Contents: current node state, sessions, tasks,      │
│    version numbers, locks                            │
│  Change frequency: Every verify-update / heartbeat / │
│    task operation                                    │
│  Concurrency protection: SQLite WAL + transactions   │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│  Layer 3: Event Log (event stream, append-only)      │
│  Storage: JSONL files + SQLite audit index            │
│  Contents: who, when, which node, what change        │
│  Properties: immutable, replayable, traceable        │
│  Purpose: audit, rollback, debug, diff               │
└─────────────────────────────────────────────────────┘
```

---

## Storage Decisions

| Data Type | Storage | Reason |
|-----------|---------|--------|
| DAG topology (nodes+edges+policy) | JSON + NetworkX | 89 nodes, read-heavy, rarely written |
| Runtime state (state, sessions, tasks, locks) | **SQLite** | High-frequency changes, needs transactions, needs concurrency protection, cross-platform without file locks |
| Audit raw data | JSONL append-only | Matches existing pattern, immutable |
| Audit index | SQLite audit table | Supports violations queries, time range filtering |
| Version snapshots | SQLite snapshots table | Supports rollback |

**Why upgrade from JSON to SQLite (Review #2)**:
- Already need version control, conflict handling, locks
- Windows/Unix file lock cross-platform details are cumbersome (fcntl/msvcrt)
- sessions, tasks, state, idempotency all fit SQLite well
- Still "zero infrastructure", but more stable than multi-file JSON

---

## Architecture Overview

```
agent/governance/                    ← Python subpackage
    __init__.py                      ← Package marker + version
    server.py                        ← FastAPI/Starlette HTTP layer (port 30006)
    errors.py                        ← Unified exception hierarchy + error codes
    db.py                            ← SQLite connection management + schema migration (NEW)
    enums.py                         ← Explicit enums: VerifyStatus, BuildStatus, Role (NEW)
    project_service.py               ← Project registration + isolation + routing
    role_service.py                  ← Principal + Session model + heartbeat + auth
    graph.py                         ← NetworkX DAG (rule layer)
    state_service.py                 ← Runtime state management (SQLite transactions)
    gate_policy.py                   ← Gate policy engine (configurable min_status) (NEW)
    impact_analyzer.py               ← Policy-based impact analysis (extracted from graph.py) (NEW)
    memory_service.py                ← Development memory store (P1)
    audit_service.py                 ← Event stream + audit index
    evidence.py                      ← Structured evidence model + validation
    permissions.py                   ← Permission matrix + scope
    idempotency.py                   ← Idempotency key management (NEW)
    event_bus.py                     ← Internal event subscription + webhook (NEW)
    client.py                        ← GovernanceClient SDK (with degradation logic) (NEW)

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

Data storage (isolated per project):
shared-volume/codex-tasks/state/governance/
    projects.json                    ← Project registry
    {project_id}/
        graph.json                   ← DAG topology (Layer 1, NetworkX serialized)
        governance.db                ← Runtime state + audit index + snapshots (Layer 2, SQLite)
        audit-YYYYMMDD.jsonl         ← Audit raw data (Layer 3, append-only)

New dependencies:
    networkx                         ← Graph operations
    starlette (or fastapi)           ← HTTP layer (Review #9: don't reinvent half a framework)
    uvicorn                          ← ASGI server
```

---

## Explicit Enums (Review #3: No More String Comparison)

```python
# enums.py
from enum import IntEnum, Enum

class VerifyLevel(IntEnum):
    """Verification depth, using integer comparison, not string lexicographic order"""
    L1 = 1   # Code exists
    L2 = 2   # API callable
    L3 = 3   # UI visible
    L4 = 4   # End-to-end
    L5 = 5   # Real third-party

class VerifyStatus(Enum):
    """Acceptance status, explicit state machine"""
    PENDING   = "pending"
    TESTING   = "testing"      # Tests running (new intermediate state)
    T2_PASS   = "t2_pass"      # T1+T2 passed
    QA_PASS   = "qa_pass"      # E2E passed (formerly pass)
    FAILED    = "failed"
    WAIVED    = "waived"       # Manually waived (new)
    SKIPPED   = "skipped"      # Skipped by gate

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

**State transitions use an explicit state machine table** (no longer relying on lexicographic comparison):

```python
# permissions.py
TRANSITION_RULES = {
    (VerifyStatus.PENDING,  VerifyStatus.T2_PASS):  {Role.TESTER},
    (VerifyStatus.T2_PASS,  VerifyStatus.QA_PASS):  {Role.QA},
    (VerifyStatus.QA_PASS,  VerifyStatus.FAILED):   set(Role),  # Any role
    (VerifyStatus.T2_PASS,  VerifyStatus.FAILED):   set(Role),
    (VerifyStatus.FAILED,   VerifyStatus.PENDING):  {Role.DEV},
    (VerifyStatus.PENDING,  VerifyStatus.WAIVED):   {Role.COORDINATOR},  # Manual waiver
}

FORBIDDEN = {
    (VerifyStatus.PENDING, VerifyStatus.QA_PASS),  # Cannot skip T2
}
```

---

## SQLite Schema (db.py)

```sql
-- Runtime state: node status
CREATE TABLE node_state (
    project_id  TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    verify_status TEXT NOT NULL DEFAULT 'pending',
    build_status  TEXT NOT NULL DEFAULT 'impl:missing',
    evidence_json TEXT,              -- Structured evidence JSON
    updated_by  TEXT,                -- session_id
    updated_at  TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (project_id, node_id)
);

-- Runtime state: node status history (event sourcing support)
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

-- Session management
CREATE TABLE sessions (
    session_id    TEXT PRIMARY KEY,
    principal_id  TEXT NOT NULL,      -- Logical identity (Review #5)
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

-- Task tracking
CREATE TABLE tasks (
    task_id      TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'created',
    related_nodes TEXT,              -- JSON array
    created_by   TEXT,               -- session_id
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- Idempotency keys (Review #14)
CREATE TABLE idempotency_keys (
    idem_key     TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL       -- 24h TTL
);

-- Audit index (raw data in JSONL, this is the query index)
CREATE TABLE audit_index (
    event_id    TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    event       TEXT NOT NULL,
    actor       TEXT,
    ok          INTEGER NOT NULL DEFAULT 1,
    ts          TEXT NOT NULL,
    node_ids    TEXT                 -- JSON array, for filtering
);

-- Version snapshots (supports rollback)
CREATE TABLE snapshots (
    project_id  TEXT NOT NULL,
    version     INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,      -- Complete node_state snapshot
    created_at  TEXT NOT NULL,
    created_by  TEXT,
    PRIMARY KEY (project_id, version)
);

-- Indexes
CREATE INDEX idx_session_principal ON sessions(principal_id, project_id);
CREATE INDEX idx_session_status ON sessions(status);
CREATE INDEX idx_audit_project_ts ON audit_index(project_id, ts);
CREATE INDEX idx_audit_ok ON audit_index(ok);
CREATE INDEX idx_idem_expires ON idempotency_keys(expires_at);
```

---

## Principal + Session Model (Review #5)

Separating "who" from "this run":

```
┌─────────────────────┐
│ Principal            │  ← Logical identity (e.g. "tester-agent")
│ principal_id: str    │     A principal can have multiple sessions
│ display_name: str    │     Same principal reusable across projects
└──────────┬──────────┘
           │ 1:N
┌──────────▼──────────┐
│ Session              │  ← Running instance
│ session_id: str      │     Bound to one project + one role
│ principal_id: str    │     Carries a token
│ project_id: str      │     Has a lifecycle (heartbeat/expiry)
│ role: Role           │
│ scope: ["L1.*"]      │
│ status: SessionStatus│
└─────────────────────┘
```

**Registration API** (Review #6: request body must not pass role to verify-update):

```
POST /api/role/register
  {
    "principal_id": "tester-agent",    ← Logical identity
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
    "evidence": {                      ← Structured evidence (Review #8)
      "type": "test_report",
      "tool": "pytest",
      "summary": {"passed": 162, "failed": 0, "exit_code": 0},
      "artifact_uri": "logs/task-xxx.run.json",
      "checksum": "sha256:a1b2c3..."
    }
  }
  // role is automatically extracted from the token session, no longer appears in the body
```

---

## Gate Policy Engine (Review #4)

Upgraded from hardcoded `pass` to configurable policies:

```python
# gate_policy.py
from dataclasses import dataclass
from enums import VerifyStatus

@dataclass
class GateRequirement:
    """Requirements for a single gate"""
    node_id: str
    min_status: VerifyStatus = VerifyStatus.QA_PASS   # Default requires all green
    policy: str = "default"          # default | release_only | waivable
    waived_by: str | None = None     # Who waived it

# Node gates changed from fixed pass to policy list
# Example:
gates = [
    GateRequirement("L1.4", min_status=VerifyStatus.T2_PASS),  # Development phase only needs T2
    GateRequirement("L3.2", min_status=VerifyStatus.QA_PASS),  # Must be all green before release
    GateRequirement("L4.1", policy="release_only"),             # Only checked during release
]

STATUS_ORDER = {
    VerifyStatus.PENDING: 0,
    VerifyStatus.TESTING: 1,
    VerifyStatus.T2_PASS: 2,
    VerifyStatus.QA_PASS: 3,
    VerifyStatus.WAIVED: 3,   # waived is equivalent to pass
}

def check_gate(requirement: GateRequirement, current_status: VerifyStatus,
               context: str = "default") -> tuple[bool, str]:
    """Check whether a single gate is satisfied"""
    if requirement.policy == "release_only" and context != "release":
        return True, ""  # Skip in non-release scenarios
    if requirement.policy == "waivable" and requirement.waived_by:
        return True, f"waived by {requirement.waived_by}"
    if current_status == VerifyStatus.FAILED:
        return False, f"{requirement.node_id} is FAILED"
    if STATUS_ORDER.get(current_status, 0) < STATUS_ORDER.get(requirement.min_status, 0):
        return False, f"{requirement.node_id} requires {requirement.min_status.value}, got {current_status.value}"
    return True, ""
```

---

## Structured Evidence Model (Review #8)

```python
# evidence.py
from dataclasses import dataclass, field

@dataclass
class Evidence:
    """Structured evidence object, traceable, signable"""
    type: str               # test_report | e2e_report | error_log | commit_ref | manual_review
    producer: str            # session_id of evidence creator
    tool: str | None = None  # pytest | playwright | git | manual
    summary: dict = field(default_factory=dict)  # {"passed": 162, "failed": 0}
    artifact_uri: str | None = None              # Points to full report
    checksum: str | None = None                  # Content verification
    created_at: str = ""

# Validation rules: by evidence type
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

## Policy-Based Impact Analysis (Review #7: impact_analyzer.py)

Extracted from graph.py, no longer a single hardcoded function:

```python
# impact_analyzer.py
from dataclasses import dataclass
from enums import VerifyLevel

@dataclass
class FileHitPolicy:
    """File hit policy"""
    match_primary: bool = True
    match_secondary: bool = False     # Review noted secondary may also be critical
    match_config_glob: list[str] = None  # e.g. ["config.*", "*.env"]

@dataclass
class PropagationPolicy:
    """Propagation policy"""
    follow_deps: bool = True          # Downstream deps propagation
    follow_reverse_deps: bool = False # Upstream reverse (rare)
    propagation_tag_filter: list[str] | None = None  # smoke_ui, config_fanout

@dataclass
class VerificationPolicy:
    """Verification policy"""
    mode: str = "targeted"            # smoke | targeted | full_regression
    skip_already_passed: bool = True  # Already passed and not directly hit → skip
    respect_gates: bool = True        # Gate not satisfied → remove from affected (not just mark as skip)

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

        # Step 1: Files → hit nodes (match scope determined by policy)
        direct_hit = self._file_match(request.changed_files, policy)

        # Step 2: Propagation (per propagation policy)
        affected = set(direct_hit)
        if prop.follow_deps:
            for nid in direct_hit:
                affected |= self.graph.descendants(nid)

        # Step 3: Pruning (per verification policy)
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
                    affected.discard(nid)  # Remove, not just mark as skip
                    skipped.append({"node": nid, "reason": reason})

        # Step 4: Group by layer + topological sort
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

## Release Profile (Review #11)

Release gate no longer requires "all nodes green across the project"; now supports scoping:

```
POST /api/wf/{project_id}/release-gate
  {
    "profile": "browser-core",          ← Optional: release profile name
    "scope": ["L3.*", "L4.1", "L4.2"],  ← Optional: node scope
    "tag": "v1.5.1",                     ← Optional: version tag
    "path_prefix": "server/services/"    ← Optional: file scope
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

Omitting profile/scope = global check (backward compatible).

---

## Event Subscription (Review #14)

```python
# event_bus.py
class EventBus:
    """Internal event dispatch, supports in-process subscriptions + webhooks"""

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

    # Webhook support (P2)
    def register_webhook(self, url: str, events: list[str]): ...
```

Integration points:
- `state_service.verify_update()` → publish `node.status_changed`
- `gate_policy.check_all_gates()` → publish `gate.satisfied` / `gate.blocked`
- `role_service.cleanup_expired()` → publish `role.expired`
- Agents can subscribe instead of polling

---

## Idempotency Keys (Review #14)

```
All write APIs support the Idempotency-Key header:

POST /api/wf/verify-update
  Header: Idempotency-Key: idem-20260321-abc123

Processing logic:
1. Check idempotency_keys table
2. Exists → Return cached response directly (no re-execution)
3. Does not exist → Execute operation → Write key + response → Return
4. TTL 24h auto-cleanup
```

---

## Offline Degradation (Review #10: Bounded Retry)

```python
# client.py
class GovernanceClient:
    def __init__(self, base_url, max_retries=5, base_delay=2, deadline_sec=120):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.deadline_sec = deadline_sec

    def verify_update(self, nodes, status, evidence):
        """Strict mode: bounded retry + exponential backoff + deadline"""
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
        """Relaxed mode: local cache + push on recovery"""
        try:
            return self._post("/api/mem/write", {...})
        except ConnectionError:
            self._offline_queue.append(("mem/write", {...}))
            return {"status": "cached_locally", "queue_size": len(self._offline_queue)}
```

---

## Bootstrap Security (Review #12)

```
POST /api/bootstrap
  Restriction: Only allows localhost / Unix socket calls
  Behavior: One-time, endpoint auto-disables after execution
  Security:
    - admin_secret is used only once, not persisted
    - token is not written to logs
    - After bootstrap completes, returns coordinator token but no longer accepts a second call
    - To re-bootstrap, manually delete the project from projects.json
```

---

## Memory Enhancement (Review #13)

```python
@dataclass
class MemoryEntry:
    id: str
    module_id: str
    kind: str            # decision | pitfall | workaround | invariant | ownership
    content: str
    applies_when: str    # Applicable conditions (e.g. "Windows environment", "when concurrency > 3")
    supersedes: str | None = None  # Which newer memory entry supersedes this one
    related_nodes: list[str] = field(default_factory=list)
    created_by: str = ""
    created_at: str = ""
    is_active: bool = True  # Automatically set to False after being superseded
```

---

## Layer Semantics (Review #7)

```
Layer becomes a display attribute, not a hard correctness constraint.
Actual correctness guarantees:
  1. DAG is acyclic (nx.is_directed_acyclic_graph)
  2. Gate policy satisfied
  3. State transitions are legal

During node-create:
  - Suggested layer value = max(deps.layer) + 1 (shown as warning if mismatched)
  - Does not block creation
  - Same-layer dependencies and cross-layer soft dependencies are both allowed
```

---

## HTTP Layer (Review #9)

```python
# server.py — Using Starlette (lightweight ASGI)
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
        Middleware(AuthMiddleware),         # Token auth
        Middleware(IdempotencyMiddleware),  # Idempotency keys
        Middleware(RequestIdMiddleware),    # request_id injection
        Middleware(AuditMiddleware),        # Automatic audit recording
    ],
)
```

Domain logic is pure Python; the HTTP layer only handles routing + middleware + serialization.

---

## Workflow Diagrams

### Flow 1: Development Task Flow

```
Coordinator creates task
    │
    ▼
POST /api/wf/{pid}/task-create          ← Coordinator session
    │                                      (role extracted from token)
    ▼
Dev queries memory before picking up task
GET /api/mem/{pid}/query?module=X       ← Dev session
    │
    ▼
Dev develops → completes → commits
    │
    ▼
POST /api/mem/{pid}/write               ← Dev writes pattern/pitfall
    │
    ▼
Tester runs T1+T2
    │
    ▼
POST /api/wf/{pid}/verify-update        ← Tester session
  Header: X-Gov-Token + Idempotency-Key
  Body: {nodes, status:"t2_pass", evidence:{type:"test_report",...}}
    │
    ▼
  Auth → scope → permission matrix → evidence validation → gate policy
    │
    ▼
  Node → t2_pass ✅ (can merge)
  EventBus → publish("node.status_changed")
    │
    ▼
(Before release) QA runs E2E
    │
    ▼
POST /api/wf/{pid}/verify-update        ← QA session
  Body: {nodes, status:"qa_pass", evidence:{type:"e2e_report",...}}
    │
    ▼
  Node → qa_pass ✅ (can release)
```

### Flow 2: Bug Fix Flow

```
Bug discovered
    │
    ▼
POST /api/wf/{pid}/verify-update        ← Any role session
  Body: {nodes:["L3.7"], status:"failed",
         evidence:{type:"error_log", summary:{error:"timeout..."}}}
    │
    ▼
  L3.7 → FAILED
  EventBus → publish("node.status_changed")
  Auto-compute: descendants("L3.7") → downstream gates may become invalid
    │
    ▼
Dev fixes + commits
    │
    ▼
POST /api/wf/{pid}/verify-update        ← Dev session
  Body: {nodes:["L3.7"], status:"pending",
         evidence:{type:"commit_ref", summary:{commit_hash:"a1b2c3d"}}}
    │
    ▼
  L3.7 → PENDING → Re-runs Tester → T2_PASS → QA → QA_PASS
```

### Flow 3: Create New Node

```
PM defines PRD → [TREE:ADD] spec
    │
    ▼
POST /api/wf/{pid}/node-create          ← Coordinator session
  Body: {
    id: "L2.22", title: "New Feature",
    deps: ["L1.4", "L0.16"],
    gate_mode: "auto",
    verify_level: 4,                     ← Integer! Not a string
    gates: [                             ← Policy-based gate (Review #4)
      {"node_id": "L1.4", "min_status": "t2_pass"},
    ]
  }
    │
    ▼
  1. Verify deps exist
  2. DAG acyclicity check (does not enforce layer rules)
  3. gate_mode:auto → Auto-derive gate policy
  4. Initialize: PENDING + impl:missing
  5. audit.record(node_create)
  6. EventBus → publish("node.created")
```

### Flow 4: Minimum Verification Path

```
git diff → changed_files
    │
    ▼
GET /api/wf/{pid}/impact
  ?files=stateService.js,config.js
  &file_policy=primary+secondary        ← Configurable (Review #7)
  &propagation=deps                      ← Configurable
  &verification=targeted                 ← Configurable
    │
    ▼
ImpactAnalyzer.analyze()
  Step 1: File hit (match primary + secondary per file_policy)
  Step 2: Propagation (expand descendants per propagation_policy)
  Step 3: Pruning (already passed and not directly hit → remove; gate not satisfied → remove)
  Step 4: Group by VerifyLevel + topological sort
    │
    ▼
  Returns: {
    direct_hit, total_affected,
    verification_order (topological order),
    by_phase: {T1:[...], T2:[...], T3:[...]},
    skipped: [{node, reason}],
    test_files,
    max_verify: 4
  }
```

### Flow 5: Release Gate

```
POST /api/wf/{pid}/release-gate
  Body: {profile:"browser-core", scope:["L3.*","L4.1","L4.2"]}
    │
    ▼
  Iterate nodes in scope → check each gate policy (context="release")
    │
  ┌─┴──┐
  ▼    ▼
 200  403
 All green  Has blocker (including release_only gates that did not pass)
```

---

## Implementation Phases

### Phase 1 — Model Convergence + Core Skeleton
1. `enums.py` — Explicit enums (VerifyStatus, VerifyLevel, Role, SessionStatus)
2. `errors.py` — Unified exception hierarchy
3. `db.py` — SQLite schema + migration + connection management
4. `models.py` — Data structures (Evidence, GateRequirement, MemoryEntry, ImpactRequest)
5. `role_service.py` — Principal + Session + heartbeat + auth
6. `graph.py` — NetworkX DAG (rule layer, with markdown import fault tolerance)
7. `gate_policy.py` — Configurable gate policy engine
8. `permissions.py` — Enum state machine + scope checks
9. `evidence.py` — Structured evidence + validation
10. `idempotency.py` — Idempotency key management
11. `audit_service.py` — JSONL + SQLite index
12. `state_service.py` — verify_update + release_gate + rollback
13. `impact_analyzer.py` — Policy-based impact analysis
14. `event_bus.py` — Internal event dispatch
15. `server.py` — Starlette + middleware
16. `client.py` — GovernanceClient SDK (bounded retry + degradation)
17. 14 test files

### Phase 2 — Memory + Task + Export
18. `memory_service.py` — Enhanced memory (kind, applies_when, supersedes)
19. Task CRUD
20. Export (JSON / Mermaid / Markdown)
21. Webhook support

### Phase 3 — Operations Enhancement
22. Release profile
23. Audit report generation
24. Snapshot compression strategy
25. Monitoring dashboard API

---

## New Dependencies

```
networkx       ← Graph operations (pure Python)
starlette      ← HTTP layer (lightweight ASGI)
uvicorn        ← ASGI server
redis          ← Redis client (session cache / locks / idempotency / pub/sub)
```

SQLite is built into Python, no additional dependencies.

---

## Docker Deployment

### Four-Layer Architecture

```
┌─────────────────────────────────────────────────────┐
│ L1: governance container                             │
│   uvicorn agent.governance.server:app --port 30006   │
│   /api/bootstrap, /api/role/*, /api/wf/*,            │
│   /api/mem/*, /api/audit/*                           │
└──────────┬──────────────┬───────────────────────────┘
           │              │
┌──────────▼──────┐  ┌───▼──────────────────────────┐
│ L2: Redis        │  │ L3: Persistent volumes         │
│ session cache    │  │ shared-volume/.../governance/ │
│ distributed lock │  │   projects.json              │
│ idempotency keys │  │   {project_id}/              │
│  (NX+TTL)       │  │     governance.db (SQLite)   │
│ Pub/Sub notify   │  │     graph.json               │
│                  │  │     audit-*.jsonl            │
│ Not source of    │  │                              │
│ truth            │  │                              │
└─────────────────┘  └───┬──────────────────────────┘
                         │
                    ┌────▼────────────────────────────┐
                    │ L4: Host machine workspace mount  │
                    │ bootstrap reads acceptance-graph  │
                    │ impact analysis reads source files│
                    └─────────────────────────────────┘
```

### Data Flow: Dual-Write SQLite + Redis

```
Write path:
  API request → SQLite transaction write (persistent truth) → Redis write (hot cache)
  Any write failure → SQLite rollback / Redis skip (degradation)

Read path:
  API request → Redis query
  Hit → Return directly
  Miss → SQLite query → Backfill Redis → Return

Redis down:
  Degrades to pure SQLite mode (slower but not offline)
  Auto-backfills hot data after recovery
```

### Redis Responsibility Boundaries

| Data | SQLite (Source of Truth) | Redis (Hot Cache) |
|------|--------------------------|-------------------|
| sessions | ✅ Persisted | ✅ Hash + TTL auto-expiry |
| heartbeat | ✅ last_heartbeat field | ✅ Key TTL for timeout detection |
| Idempotency keys | ❌ No longer in SQLite | ✅ SET NX + 24h TTL |
| Distributed lock | ❌ | ✅ SETNX + TTL |
| pub/sub notifications | ❌ | ✅ Redis Pub/Sub |
| node_state | ✅ Core state | ❌ Low write frequency, not cached |
| node_history | ✅ Event sourcing | ❌ |
| audit_index | ✅ Query index | ❌ |
| snapshots | ✅ Rollback | ❌ |

### redis_client.py — Connection Management + Degradation

```python
class RedisClient:
    """Redis client with built-in degradation to SQLite logic"""

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
        """Return if Redis hits, call fallback_fn if miss or unavailable"""
        if self._available:
            try:
                val = self._client.get(key)
                if val is not None:
                    return json.loads(val)
            except redis.RedisError:
                self._available = False
        return fallback_fn()

    def set_cache(self, key, value, ttl_sec=3600):
        """Write to Redis cache, fail silently"""
        if self._available:
            try:
                self._client.setex(key, ttl_sec, json.dumps(value))
            except redis.RedisError:
                self._available = False

    def check_idempotency(self, key) -> dict | None:
        """Idempotency key check: Redis SET NX"""
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
            return True  # Degradation: no lock (single-instance SQLite WAL is sufficient)
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
      - ${WORKSPACE_PATH:-./}:/workspace:ro    # L4: Host machine workspace
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

### Startup Flow

```bash
# 1. Build and start
docker compose up -d

# 2. Bootstrap (first time only)
curl -X POST http://localhost:30006/api/bootstrap \
  -d '{"project_id":"toolbox-client",
       "graph_source":"/workspace/acceptance-graph.md",
       "coordinator":{"principal_id":"coord","admin_secret":"xxx"}}'

# 3. Check status
docker compose ps
curl http://localhost:30006/api/wf/toolbox-client/summary
```

---

## Verification

```bash
# 1. Run governance service tests
python -m unittest discover -s agent/tests -p "test_governance_*.py" -v

# 2. Full regression
python -m unittest discover -s agent/tests -p "test_*.py" -v

# 3. Start service
uvicorn agent.governance.server:app --port 30006

# 4. Bootstrap + end-to-end verification
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

## Changelog
- 2026-03-26: Legacy Telegram bot system completely removed (bot_commands, coordinator, executor and 20 other modules); now using governance API exclusively
