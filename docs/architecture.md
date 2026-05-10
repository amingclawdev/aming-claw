# Aming Claw — System Architecture

> **Canonical architecture document** — Single source of truth for the Aming Claw system architecture.
> Last updated: 2026-04-05 | Phase 2 Documentation Consolidation

## 1. Overview

Aming Claw is an AI-driven project governance system that coordinates multiple agent roles (Coordinator, PM, Dev, Tester, QA, Observer) through an automated task chain. The system runs as **host-based services** with optional Docker dependencies for auxiliary services.

### Design Principles

- **SQLite is the source of truth** — semantic layers (FTS5, embeddings) help *find* objects; they never serve as final state.
- **Rules before AI** — conflict detection and task routing use zero-token rule engines before invoking AI models.
- **Degrade before interrupt** — mem0/semantic search down falls back to FTS5; index write failure preserves the primary record with async retry.
- **Single responsibility** — each component owns exactly one concern.

## 2. Component Architecture

```
Host Machine (primary runtime)
├── Governance Service     :40000   ← Rule engine, task registry, workflow, audit, memory, auto-chain
├── Service Manager                 ← Executor supervisor (monitor thread, circuit breaker)
└── Executor Worker                 ← Task execution via Claude CLI / provider backends

Optional Docker Dependencies
├── Telegram Gateway       :40010   ← Message ingress/egress
├── dbservice              :40002   ← Semantic memory service (mem0)
└── Redis                  :40079   ← Pub/sub, cache, hot context store
```

### 2.1 Governance Service (`:40000`)

The governance service is the central control plane, running on the host at `http://localhost:40000`. It provides:

- **Task Registry** — CRUD for tasks with status lifecycle (queued → claimed → succeeded/failed)
- **Workflow Engine** — Acceptance graph import, node state tracking, verify-update API
- **Auto-Chain** — Automated stage progression: PM → Dev → Test → QA → Gatekeeper → Merge
- **Memory Backend** — SQLite + FTS5 full-text search, pluggable backend interface
- **Conflict Rules** — 5-rule engine for duplicate detection, conflict resolution, retry logic
- **Version Gate** — Git HEAD synchronization with chain_version
- **Audit Log** — Append-only event log for all governance actions
- **Context API** — Runtime context save/load for session state

Key files:
- `agent/governance/server.py` — FastAPI application, all REST endpoints
- `agent/governance/auto_chain.py` — Stage transition logic, gate checks
- `agent/governance/memory_backend.py` — Memory abstraction (Local/Docker/Cloud backends)
- `agent/governance/conflict_rules.py` — 5-rule conflict engine

### 2.2 Executor Worker

The executor worker is a single-instance process per project that claims and executes tasks from the governance queue.

**Lifecycle:**
- Started explicitly by an executor-owned session or by MCP ServiceManager only when `--autostart-executor` is enabled
- File lock + PID file ensures single instance
- Heartbeat every 30s; stuck detection at 120s
- Crash recovery on startup: requeues orphaned claimed tasks
- Circuit breaker: 5 restarts within 300s triggers OPEN state

**Execution model:**
- Polls governance `/api/task/{pid}/list` for queued tasks
- Claims task via `/api/task/{pid}/claim`
- Executes via Claude CLI (`claude` command) with role-specific prompts and turn limits
- Reports results via `/api/task/{pid}/complete`
- Auto-chain triggers next stage on success

Key file: `agent/governance/executor_worker.py`

### 2.3 MCP Server

The MCP (Model Context Protocol) server provides tool-based access to governance APIs for Claude Code sessions.

**Tools exposed:**
- `task_list`, `task_create`, `task_claim`, `task_complete`, `task_cancel`, `task_hold`, `task_release`
- `backlog_list`, `backlog_get`, `backlog_upsert`, `backlog_close`
- `graph_status`, `graph_operations_queue`, `graph_query`, `graph_pending_scope_queue`
- `executor_status`, `executor_scale`
- `observer_mode`, `wf_summary`, `wf_impact`, `node_update`
- `version_check`, `health`, `preflight_check`
- `telegram_send`

**Service lifecycle:**
- Configured via `.mcp.json` in project root
- Normal editor/plugin sessions pass `--workers 0` so MCP exposes tools without claiming queue work
- In-process workers start only when `--workers N` is greater than zero
- Executor subprocess autostart is opt-in via `--autostart-executor`

Key files: `agent/mcp/server.py`, `agent/mcp/tools.py`

### 2.4 Telegram Gateway

The Telegram gateway handles message ingress from Telegram and egress of replies.

**Flow:**
1. User sends message via Telegram
2. Gateway classifies intent: greeting / status_query / task / chat / dangerous
3. Greetings and status queries → direct reply (0 tokens, no task creation)
4. Task/chat messages → coordinator task created → executor processes → reply sent

**Architecture:**
- Runs as Docker container on port 40010
- Communicates with governance service at `http://host.docker.internal:40000`
- Redis pub/sub for real-time reply delivery

### 2.5 Redis

Redis provides:
- **Pub/sub** — Real-time event delivery (task completion notifications, reply routing)
- **Hot context store** — `ctx:input:{session_id}` and `ctx:output:{session_id}` hashes with 24h TTL
- **Cache** — Frequently accessed governance data

Runs as Docker container on port 40079.

## 3. Observer Pattern

The Observer is a human-in-the-loop governance role that can monitor and intervene in the auto-chain pipeline.

### Observer Mode

When `observer_mode=ON`:
- All auto-chain tasks enter `observer_hold` status instead of `queued`
- Observer reviews task prompts before releasing to executor
- Observer can claim tasks directly for manual execution
- Executor scale set to 0 prevents auto-claiming

### Observer Capabilities

| Capability | Description |
|------------|-------------|
| Task review | Inspect task prompts before release |
| Manual execution | Claim and execute any stage manually |
| Governance repair | Restore graph state, sync node state |
| Memory management | Search, write, promote memories |
| Chain intervention | Hold, release, or cancel tasks |

### Prohibited Actions

- Direct SQLite access to governance.db (WAL lock conflicts)
- Marking nodes as t2_pass/qa_pass without verification
- Skipping governance chain for code changes

## 4. Auto-Chain Pipeline

The auto-chain is the core workflow automation that progresses tasks through governance stages.

### Stage Sequence

```
PM → Dev → Test → QA → Gatekeeper → Merge
```

Each stage transition includes gate checks:

| Gate | Stage | Checks |
|------|-------|--------|
| PM Gate | PM → Dev | PRD has target_files, verification, acceptance_criteria |
| Checkpoint Gate | Dev → Test | changed_files exist in git diff, files within target_files scope |
| T2 Pass Gate | Test → QA | test_report is dict with passed > 0, failed == 0 |
| QA Pass Gate | QA → Gatekeeper | recommendation == "qa_pass", all criteria passed |
| Version Gate | Any stage | Git HEAD matches chain_version (warning-only since D3 fix) |

### Gate Failure Handling

- Gate failure triggers automatic retry task creation
- Dedup guards prevent duplicate retry tasks (D4 fix)
- Maximum retry attempts configurable per stage
- `.claude/` paths filtered from dirty_files check (D5 fix)

## 5. Memory System

### Schema v8

Four SQLite tables:

| Table | Purpose |
|-------|---------|
| `memories` | Primary memory records (kind, scope, version, status, confidence, TTL) |
| `memories_fts` | FTS5 virtual table for full-text search |
| `memory_relations` | Graph edges between ref_ids (PRODUCED, CAUSED_BY, DEPENDS_ON) |
| `memory_events` | Append-only event log |

### Memory Kinds

| Kind | Durability | Conflict Policy |
|------|-----------|-----------------|
| fact | permanent | replace |
| summary | durable | replace |
| decision | permanent | append |
| failure_pattern | permanent | append |
| task_result | durable | replace |
| task_snapshot | ephemeral | replace |
| module_note | durable | append |
| rule | permanent | replace |
| audit_event | permanent | append |
| architecture | permanent | replace |
| pattern | permanent | replace |

### Backend Abstraction

The `MEMORY_BACKEND` environment variable selects the backend:
- `local` (default) — SQLite + FTS5 on host
- `docker` — dbservice semantic search with FTS5 fallback
- `cloud` — Cloud-hosted memory service (stub)

## 6. Conflict Rules Engine

The 5-rule conflict detection engine runs before AI (0 tokens):

| Rule | Trigger | Decision |
|------|---------|----------|
| Duplicate | Same source_message_hash within 5 min | `duplicate` — reject |
| Same-file conflict | Overlapping target_files with active task | `conflict` — queue or reject |
| Dependency | Required upstream task not complete | `queue` — hold until dependency resolves |
| Failure pattern | Known failure pattern matches task | `retry` with enriched context |
| New | No conflicts detected | `new` — proceed normally |

Key file: `agent/governance/conflict_rules.py`

## 7. Version Control Integration

### Version Gate

The executor syncs git HEAD to governance DB every 60s (only on change). The version gate checks:
- `chain_version` in DB matches current git HEAD
- Dirty files check (excluding `.claude/` paths)
- Downgraded to warning-only (D3 fix) to prevent false blocks

### Merge Flow

1. Dev task completes in worktree branch
2. Merge stage cherry-picks/merges to main
3. `version-update` API called with new HEAD
4. `chain_version` updated in governance DB

## 8. API Surface

### REST API (governance service at `:40000`)

| Category | Endpoints |
|----------|-----------|
| Health | `GET /api/health`, `GET /api/version-check/{pid}` |
| Tasks | `GET/POST /api/task/{pid}/list`, `/claim`, `/complete`, `/cancel`, `/hold`, `/release` |
| Workflow | `GET /api/wf/{pid}/summary`, `/node/{nid}`, `/export`, `/impact` |
| Memory | `GET/POST /api/mem/{pid}/write`, `/query`, `/search`, `/relate`, `/expand`, `/promote` |
| Audit | `GET /api/audit/{pid}/log` |
| Runtime | `GET /api/runtime/{pid}` |
| Context | `GET/POST /api/context/{pid}/save`, `/load` |

### MCP Tools

Core governance operations are available as MCP tools for Claude Code sessions, including task management, backlog filing, graph governance, workflow impact, version checks, and optional executor control. See `.mcp.json` for the active server configuration.

## 9. Data Flow Diagram

```
User (Telegram)
    │
    ▼
Telegram Gateway (:40010)
    │ classify intent
    ▼
Governance Service (:40000)
    │ create coordinator task
    ▼
Executor Worker
    │ claim + execute coordinator
    │ coordinator decides: reply_only | create_pm_task
    ▼
Auto-Chain Pipeline
    PM → Dev → Test → QA → Gatekeeper → Merge
    │                                        │
    │    (each stage: create → claim →       │
    │     execute → complete → gate check)   │
    ▼                                        ▼
Memory System                          Git Merge + Version Update
    │
    ▼
Redis Pub/Sub → Telegram Reply
```

## 10. Symmetric Redeploy Architecture (PR-2: sm↔gov contract)

The governance service and service manager implement a **symmetric redeploy** contract where each service can restart the other, but neither can restart itself. This prevents the indeterminate state that occurs when a process attempts self-restart.

### Contract Design

```
Service Manager (SM)                    Governance Service (GOV)
    │                                        │
    │  POST /api/manager/redeploy/governance │
    │◄───────────────────────────────────────│  GOV requests SM to restart GOV
    │                                        │
    │  POST /api/governance/redeploy/service_manager
    │───────────────────────────────────────►│  SM requests GOV to restart SM
    │                                        │
    │  POST /api/governance/redeploy/executor│
    │───────────────────────────────────────►│  SM requests GOV to restart executor
    │                                        │
    │  POST /api/governance/redeploy/gateway │
    │───────────────────────────────────────►│  SM requests GOV to restart gateway
```

### Mutual-Exclusion Invariant

- **Governance** exposes `POST /api/governance/redeploy/{target}` for targets: `executor`, `gateway`, `coordinator`, `service_manager`. Self-targeting (`governance`) returns `400`.
- **Service Manager** exposes `POST /api/manager/redeploy/governance`. Self-targeting (`service_manager`) returns `400`.
- Deploy stage uses this contract: after merging code, it calls the appropriate redeploy endpoint based on which services' code changed, without requiring a full system restart.

### Implementation (PR-2, commit fc025cd)

- Governance-side: `agent/governance/server.py` — `/api/governance/redeploy/{target}` handler with lock, signal, spawn, health-check pipeline
- Manager-side: `agent/service_manager.py` — `/api/manager/redeploy/governance` handler (PR-1, commit 3cac7d7)
- Both endpoints share the same 5-step pipeline: validate → lock → signal → restart → health-check

## 11. Auto-Infer Pipeline (A4a: dev → QA hook)

The auto-infer pipeline automatically infers acceptance graph changes from dev task outputs. This replaces the manual process of updating `acceptance-graph.md` after code changes.

### Pipeline Flow

```
Dev Task Completes
    │
    ▼
Auto-Infer Hook (post-dev, pre-test)
    │
    ├── Dev provided explicit graph_delta?
    │   ├── YES → validate schema → queue for merge-stage apply
    │   └── NO  → infer from changed_files + CODE_DOC_MAP
    │               ├── New files not in any node → propose "creates"
    │               ├── Cross-node file changes   → propose "links"
    │               └── Deleted files             → propose status updates
    │
    ▼
QA Stage receives inferred delta as metadata
    │
    ▼
Merge Stage applies accepted deltas to acceptance-graph.md
```

### Inference Rules

| Condition | Inferred Action |
|-----------|----------------|
| New `.py` file under `agent/` not in CODE_DOC_MAP | Create node under parent layer matching directory |
| Modified file mapped to node A, also imports module from node B | Link A → B with `DEPENDS_ON` relation |
| File deleted that was sole mapping for a node | Update node status to `pending` review |
| Test file added matching `test_*.py` pattern | Link to node via stem-prefix matching |

### Key Files

- `agent/governance/auto_chain.py` — hook integration point (post-dev gate)
- `agent/governance/graph_delta.py` — delta inference engine
- Base implementation: commit 3e1bc9d (A4a)
- Diagnostic logging: commit 9200b87 (G1)

## 12. Backfill Evidence Channel (A5)

The backfill evidence channel provides a governed API for retroactively attaching verification evidence to nodes that were promoted without proper chain-walked evidence.

### Problem

Several code paths advance node state without full evidence:
- `preflight-autofix` waives orphan pending nodes
- `reconcile.py` bulk-promotes nodes during graph reconciliation
- Manual-fix sessions promote nodes via observer bypass

These nodes have the correct final state but lack audit-trail evidence, making governance forensics incomplete.

### Solution (A5, commit 47423b6)

`POST /api/wf/{pid}/node-promote-backfill` accepts:
- List of node IDs to backfill
- Standard evidence object (type, tool, summary)
- `backfill_reason` for audit trail
- Optional reference to original promotion event

The endpoint validates that:
1. All referenced nodes exist in the graph
2. Evidence object meets standard structural requirements
3. Caller has coordinator or QA role permissions
4. A `backfill_reason` is provided (enforced, not optional)

Every backfill operation is recorded in `audit_log` with `action='node_promote_backfill'`, maintaining full traceability even for retroactive evidence attachment.

## 13. Deployment Topology

See [deployment.md](deployment.md) for complete deployment instructions.

**Minimum viable deployment:**
- Host: Governance service + executor worker (via MCP)
- Docker: Redis (for pub/sub)
- Optional: Telegram gateway, dbservice

**All governance operations run on the host at port 40000.** Docker is only used for auxiliary services (Telegram gateway, semantic memory, Redis).
