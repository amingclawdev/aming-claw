---
status: archived
superseded_by: design-spec-memory-coordinator-executor.md
archived_date: 2026-04-05
historical_value: "Session runtime design for coordinator sessions"
do_not_use_for: "session runtime decisions"
---

# Session Runtime State Service Design

> Note: The old coordinator.py session model has been completely removed. Session is now the governance server's principal+session model.

## Core Concepts

```
User (Telegram)
    │ Routes messages via telegram_gateway (port 40010)
    ▼
Governance Server (port 40006)
    │ principal+session model
    │ task_registry manages task lifecycle
    │ Each agent gets session token via /api/role/assign
    │
    ├── Dev Agent (long/short lifecycle)
    │     Independent context + independent tasks + governance session
    ├── Tester Agent (short lifecycle)
    │     Independent context + independent tasks + governance session
    └── QA Agent (short lifecycle)
          Independent context + independent tasks + governance session
```

## Session Lifecycle Model

### Governance Session (Replaces Old Coordinator Session)

> The old Coordinator Session (in the deleted coordinator.py) has been replaced by the governance server's principal+session model.

```
Message arrives (telegram_gateway / API)
    │
    ▼
[ROUTE] governance server receives message
    │  1. Load project context via /api/context/*
    │  2. task_registry manages task state
    │  3. Manage roles via /api/role/sessions
    │
    ▼
[PROCESS] Process message
    │  Understand user intent:
    │  ├── Query → direct reply → return via telegram_gateway
    │  ├── Short task → executor-gateway executes → reply with result
    │  └── Long task → dispatch to role → monitor progress
    │
    ▼
[MANAGE] Manage roles
    │  Check each role's status:
    │  ├── GET /api/role/{pid}/sessions
    │  ├── dev: running (task-xxx)
    │  ├── tester: idle
    │  └── qa: idle
    │
    │  Dispatch new task:
    │  └── POST /api/task/create → assign to dev
    │
    ▼
[COMPLETE] Task completed
    │  1. Save context via governance API
    │  2. Role session self-manages lifecycle via heartbeat
    │  3. Expired sessions auto-cleaned (180s stale, 600s expired)
```

### Role Session (Dev/Tester/QA)

```
Governance server dispatches task (via task_registry)
    │
    ▼
[SPAWN] Role Session starts
    │  1. Load role context (previous working memory)
    │  2. Claim task: POST /api/task/claim
    │  3. Register lease: POST /api/agent/register
    │
    ▼
[EXECUTE] Execute task
    │  Run Claude Code CLI / run tests / code review (via executor-gateway port 8090)
    │  Periodic heartbeat lease renewal (POST /api/role/heartbeat)
    │  Progress written to task registry (governance API)
    │
    ▼
[COMPLETE] Task completed
    │  1. POST /api/task/complete {status, result}
    │  2. Save role context
    │  3. POST /api/agent/deregister
    │  4. Notify governance server (Redis event)
    │
    ▼
Governance server receives notification → reply to user via telegram_gateway → decide next step
```

## State Service (Session Runtime)

### Data Model

```json
// Stored in Redis + SQLite
// Key: runtime:{project_id}

{
  "project_id": "amingClaw",
  "coordinator": {
    "session_id": "coord-1774210000",
    "status": "active",           // active / closing / closed
    "started_at": "2026-03-22T...",
    "current_message": "Help me run L1.3 tests",
    "lock": "coord-lock-9cb15f91"  // Only one coord at a time
  },
  "agents": {
    "dev": {
      "session_id": "dev-1774210050",
      "status": "running",        // idle / running / completed / failed
      "task_id": "task-xxx",
      "task_prompt": "Write unit tests for L1.3",
      "started_at": "2026-03-22T...",
      "lease_id": "lease-xxx",
      "progress": "Analyzing code structure...",
      "context": {
        "files_modified": ["agent/tests/test_xxx.py"],
        "decisions": ["Use unittest instead of pytest"]
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
GET  /api/runtime/{project_id}           → Full runtime state
POST /api/runtime/{project_id}/acquire   → Coordinator acquires control
POST /api/runtime/{project_id}/release   → Coordinator releases control
POST /api/runtime/{project_id}/spawn     → Dispatch role task
POST /api/runtime/{project_id}/update    → Update role status
GET  /api/runtime/{project_id}/agents    → Status of each role
```

## Message-Driven Session Switching

```
Timeline:

T0: User sends message "Help me modify the auth module"
    → telegram_gateway routes to governance server
    → governance server creates task → dispatches to dev
    → Dev Agent starts (via executor-gateway), begins code modification

T1: Dev still running...governance server monitoring

T2: User sends new message "What's L3.2 status"
    → telegram_gateway routes to governance server
    → governance server processes:
        1. Query /api/wf/{pid}/node/L3.2 status → reply
        2. Check dev status → still running → no intervention

T3: Dev completes task
    → Publishes Redis event: task.completed
    → governance server receives notification:
        1. Sees dev task-xxx completed
        2. Reply to user via telegram_gateway: "auth module modification complete"
        3. Decision: need tester verification → dispatch tester task via task_registry
```

## Project Control Lock

> The old Coordinator control lock (in the deleted coordinator.py) has been replaced by governance server's session management.

```
Session management for the same project is centrally controlled by governance server:

- Each agent gets a project-level session via /api/role/assign
- Session stays active via heartbeat (/api/role/heartbeat)
- Expired sessions auto-cleaned: stale (180s) → expired (600s)
- Same role/project combination uniqueness guaranteed by governance server
- View active sessions: GET /api/role/{pid}/sessions
```

## Role Context Isolation

```
Each role has independent context storage:

context:snapshot:amingClaw:governance   → governance project-level context
context:snapshot:amingClaw:dev          → dev work context
context:snapshot:amingClaw:tester       → tester test context
context:snapshot:amingClaw:qa           → qa acceptance context

Role context contents:
  governance: {focus, pending_tasks, agent_status, recent_messages}
  dev: {current_files, code_changes, decisions, blocked_on}
  tester: {test_results, coverage, failed_tests}
  qa: {review_notes, verified_nodes, blocked_nodes}
```

## Scheduled Task Adaptation

```
Current:
  Task starts → process message → ACK → exit

Changed to:
  Task starts → governance API takes over
    → Load context (/api/context/*)
    → Process message
    → Check role status (GET /api/role/{pid}/sessions)
    → Dispatch new task (POST /api/task/create)
    → Wait (XREADGROUP BLOCK 30s)
    → New message arrived? → Process
    → Timeout? → Check: any running roles?
       → Yes → Continue waiting (BLOCK 30s again)
       → No → Save context → Exit

Session timeout rules:
  No tasks running + no new messages → exit after 5 minutes
  Tasks running + no new messages → exit after 30 minutes (roles will complete on their own)
  New message → process immediately
```

## Relationship with Existing System

> The old agent/ modules (coordinator.py, executor.py and 20 other files) have all been removed.

```
Current architecture:
    │
    ├── governance server (port 40006)
    │     task_registry: create / claim / complete
    │     agent lifecycle: register / heartbeat / deregister
    │     session context: /api/context/* (save / load / log)
    │     workflow: verify-update / summary / release-gate
    │
    ├── telegram_gateway (port 40010)
    │     Telegram message routing: reply / bind
    │
    ├── executor-gateway (FastAPI port 8090)
    │     Actual task execution
    │
    └── executor_api (port 40100)
         Monitoring API
```

## Implementation Layers

| Layer | Content | Priority |
|-------|---------|----------|
| 1 | Runtime state model + Redis storage | P0 |
| 2 | Coordinator control lock | P0 |
| 3 | Role context isolation (context key with role prefix) | P0 |
| 4 | Message → task routing (direct query reply vs long task dispatch) | P1 |
| 5 | Role spawn/monitor (actually starting dev/tester) | P1 |
| 6 | Task completion notification → Coordinator reply | P1 |
| 7 | Session switching (governance server managed) | P2 |

## Changelog
- 2026-03-26: Old Telegram bot system completely removed (bot_commands, coordinator, executor and 20 other modules), unified to use governance API
