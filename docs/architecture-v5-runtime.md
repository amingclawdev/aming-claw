# Aming Claw Architecture v5 — Session Runtime

> v4 → v5 core change: Remove Scheduled Task polling, Gateway directly drives CLI execution. Introduce Session Runtime state service to manage Coordinator + role lifecycle.

## 1. System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Human User (Telegram)                     │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │    Nginx (:40000)    │
                    └──┬────────┬────┬────┘
                       │        │    │
          ┌────────────▼──┐ ┌──▼────▼───────────┐
          │  Governance   │ │  Telegram Gateway  │
          │  (:40006)     │ │  (:40010)          │
          │  Rules+Events │ │  Messages+Routing  │
          └──────┬────────┘ └──────┬─────────────┘
                 │                 │
          ┌──────▼─────────────────▼───────┐
          │          Redis (:6379)          │
          │  Streams / Pub-Sub / Cache / Lock│
          └──────┬─────────────────────────┘
                 │
          ┌──────▼──────────┐
          │   dbservice     │
          │   (:40002)      │
          │   Memory Layer  │
          └─────────────────┘

─ ─ ─ ─ ─ ─ Docker Internal ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─

          ┌─────────────────────────────────┐
          │         Host Machine             │
          │                                 │
          │  Executor (resident)             │
          │    ├── Watch task files          │
          │    ├── run_claude / run_codex   │
          │    └── Write results + notify   │
          │                                 │
          │  Claude Code CLI               │
          │  Codex CLI                      │
          └─────────────────────────────────┘
```

## 2. v4 → v5 Changes

| Module | v4 | v5 | Reason |
|--------|----|----|--------|
| Message consumption | Scheduled Task polls Redis Stream every minute | **Gateway dispatches on message receipt** | Remove latency and complexity |
| Task execution | None (message handling = reply) | **Gateway writes task file → Executor calls CLI** | Task messages need actual execution |
| Session management | None | **Session Runtime state service** | Manage Coordinator + role lifecycle |
| Role system | Token assigned but no actual management | **Roles have independent context + tasks + lifecycle** | Multi-role collaboration |
| Scheduled Task | 3 scheduled tasks | **All disabled** | No longer needed |

## 3. Message Processing Flow (v5.1 Correction)

> **v5.1 key correction**: Gateway no longer dispatches tasks directly. All non-command messages forwarded to Coordinator.
> Coordinator handles conversation, decisions, task orchestration. Gateway only does message send/receive.

```
User Telegram message: "Help me write tests for L1.3"
    │
    ▼
Gateway (Docker, real-time polling)
    │  1. getUpdates receives message
    │  2. Look up routing table: chat_id → project_id + token
    │  3. Determine message type:
    │
    ├── Command (/menu /status /bind /help)
    │     → Gateway handles directly → reply
    │
    └── Non-command (any text)
          │
          ▼
    Gateway starts Coordinator Session:
      run claude CLI + inject context (project status/memory/active tasks)
          │
          ▼
    Coordinator (Claude CLI session):
      1. Understand user intent
      2. Query governance API (node status/memory)
      3. Decision:
         ├── Direct answer → reply to user via Gateway
         ├── Need code execution → create task {role:"dev"} → notify user
         └── Need confirmation → ask user follow-up
      4. If task was created:
          │
          ▼
    Executor (host machine, resident process):
      Watch pending/ → claim → run_claude/run_codex
      → Write results + Redis notification
          │
          ▼
    Gateway receives notification → start new Coordinator session to evaluate results → reply to user

Role responsibility boundaries:
  Gateway:     Message send/receive + command handling (no decisions, no creating tasks)
  Coordinator: Conversation + decisions + task orchestration (no writing code itself)
  Dev/Executor: Code execution (no user conversation)
```

## 4. Session Runtime State Service

### 4.1 Why It's Needed

Coordinator is no longer a continuously running session, but rather:
- Each message can trigger a new processing flow
- Multiple roles (dev/tester/qa) may be executing tasks simultaneously
- Need to know "who is doing what" to decide next steps

### 4.2 Data Model

```json
// Redis key: runtime:{project_id}
{
  "project_id": "amingClaw",
  "active_tasks": [
    {
      "task_id": "task-xxx",
      "role": "dev",
      "prompt": "Write tests for L1.3",
      "status": "running",
      "started_at": "2026-03-22T...",
      "backend": "claude"
    }
  ],
  "completed_tasks_pending_notify": [
    {
      "task_id": "task-yyy",
      "status": "succeeded",
      "result_summary": "15 tests passed",
      "completed_at": "2026-03-22T..."
    }
  ],
  "context": {
    "current_focus": "L1.3 test completion",
    "recent_messages": ["...last 20..."],
    "decisions": []
  },
  "updated_at": "2026-03-22T..."
}
```

### 4.3 API

```
GET  /api/runtime/{project_id}
  → Full runtime state (active tasks, pending notification results, context)

POST /api/runtime/{project_id}/dispatch
  → Dispatch task (write task file + update runtime state)
  Body: {prompt, role, backend}

POST /api/runtime/{project_id}/complete
  → Mark task complete (executor callback)
  Body: {task_id, status, result}

POST /api/runtime/{project_id}/notify
  → Mark user notified (Gateway callback)
  Body: {task_id}
```

### 4.4 Relationship with Existing Components

```
Session Runtime (new)
    │
    ├── Read/write Redis (runtime:{pid})     ← Real-time state
    │
    ├── Calls Task Registry (existing)       ← Persistent task records
    │     create / claim / complete
    │
    ├── Calls Session Context (existing)     ← Cross-message context
    │     save / load
    │
    ├── Calls Agent Lifecycle (existing)     ← Role lease management
    │     register / heartbeat
    │
    └── Called by Gateway + Executor         ← Message entry + execution exit
```

## 5. Gateway Refactoring (v5.1 Correction)

> **v5.1 key correction**: Gateway no longer classifies query/task/chat.
> All non-command messages uniformly forwarded to Coordinator for processing.

### 5.1 Message Routing (Simplified)

```python
def handle_message(chat_id, text, route):
    """Gateway message routing — only distinguishes commands from non-commands"""
    if text.startswith("/"):
        handle_command(chat_id, text)  # /menu /status /bind etc.
        return

    # Non-command → all forwarded to Coordinator
    forward_to_coordinator(chat_id, text, route)
```

### 5.2 Coordinator Triggering

```python
def forward_to_coordinator(chat_id, text, route):
    """Start Coordinator CLI session to process user message"""
    project_id = route["project_id"]
    token = route["token"]

    # 1. Assemble context
    context = assemble_context(project_id, token)  # Project status+memory+active tasks

    # 2. Start Claude CLI session
    result = run_coordinator_session(
        message=text,
        context=context,
        project_id=project_id,
        token=token,
    )

    # 3. Reply to user
    send_text(chat_id, result["reply"])

    # Note: Coordinator may have created tasks internally
    # After task completion, Executor notifies → Gateway → new Coordinator session evaluates
```

### 5.3 Responsibility Boundary (Hard Constraints)

```
Gateway CAN do:
  ✅ Handle /command
  ✅ Forward messages to Coordinator
  ✅ Send Coordinator's replies
  ✅ Send task completion notifications

Gateway CANNOT do:
  ❌ Classify messages as query/task/chat
  ❌ Directly create task files
  ❌ Directly call governance API to answer queries
  ❌ Make any decisions
```

### 5.3 Result Notification

```python
# Gateway subscribes to Redis Pub/Sub: task.completed
def on_task_completed(payload):
    task_id = payload["task_id"]
    project_id = payload["project_id"]
    result = payload["result_summary"]

    route = get_route_by_project(project_id)
    if route:
        chat_id = route["chat_id"]
        send_text(chat_id, f"Task completed: {result}")

    # Update runtime
    update_runtime(project_id, complete_task=task_id)
```

## 6. Executor Refactoring

### 6.1 Existing Capabilities (Direct Reuse)

```
agent/executor.py        → Watch pending/ directory
agent/backends.py         → run_claude / run_codex / run_pipeline
agent/task_state.py       → Task state tracking
agent/task_accept.py      → Result processing

Resolved issues:
  - Windows stdin passes prompt (not command line args)
  - Strip CLAUDECODE env var to prevent nested rejection
  - Strip ANTHROPIC_API_KEY to prevent OAuth failure
  - git diff snapshot before execution
  - Noop detection + retry
  - Timeout handling + retry
```

### 6.2 New: Execution Completion Notification

```python
# After executor.py completes task, send Redis notification
def on_task_done(task_id, result):
    # Existing: write results to results/
    # New: send Redis notification
    redis.publish("task:completed", {
        "task_id": task_id,
        "project_id": result.get("project_id"),
        "status": "succeeded" if result["exit_code"] == 0 else "failed",
        "result_summary": result.get("stdout", "")[:200],
    })
```

## 7. Project Binding and Switching

```
User /menu:
┌─────────────────────────────────┐
│ Current: amingClaw               │
│ Running: 1 task                  │  ← Read from runtime
│                                  │
│ [>> amingClaw (1 task)]          │
│ [   toolboxClient (idle)]        │
│                                  │
│ [Project Status] [Switch]        │
└─────────────────────────────────┘

Switch flow:
  1. Save amingClaw context
  2. Update route → toolboxClient
  3. Load toolboxClient context + runtime
  4. amingClaw's running tasks are not interrupted
     → After completion, notify amingClaw's runtime
     → But user is on toolboxClient, don't push yet
     → When switching back to amingClaw, see "1 task completed"
```

## 8. Role Context Isolation

```
Each role has independent context:

context:snapshot:amingClaw              → Coordinator context
context:snapshot:amingClaw:dev          → Dev work context
context:snapshot:amingClaw:tester       → Tester context
context:snapshot:amingClaw:qa           → QA context

Coordinator context:
  {focus, active_tasks, recent_messages, decisions}

Dev context:
  {current_task, files_modified, code_decisions, blocked_on}

Tester context:
  {test_results, coverage_data, failed_tests}

QA context:
  {verified_nodes, review_notes, blocked_nodes}
```

## 9. Docker Compose (v5)

```yaml
services:
  nginx:             # Reverse proxy (:40000)
  governance:        # Rules+events+runtime (:40006)
  telegram-gateway:  # Messages+routing+classification+dispatch (:40010)
  dbservice:         # Memory layer (:40002)
  redis:             # Cache/communication (:6379→40079)

# Not in Docker:
#   executor         → Host machine resident, watches task files
#   claude/codex CLI → Host machine, called by executor
```

**Removed components:**
- ~~Scheduled Task (telegram-handler-*)~~ → Gateway handles directly
- ~~Message Worker~~ → Not needed
- ~~ChatProxy~~ → Not needed (Gateway polls directly)

## 10. End-to-End Flow Examples

### Example 1: Query

```
User: "How many nodes does amingClaw have?"
  → Gateway receives → classify: query
  → GET /api/wf/amingClaw/summary
  → Reply: "68 nodes, 68 qa_pass"
  → Duration: <1 second
```

### Example 2: Short Task

```
User: "Run the tests"
  → Gateway receives → classify: task
  → Write task file → reply "Executing..."
  → Executor: run_claude("python -m unittest discover...")
  → Completes after 30 seconds → Redis notify
  → Gateway: "Tests complete: 1038 ran, 1031 passed"
```

### Example 3: Long Task

```
User: "Help me implement L9.7 deploy pre-check"
  → Gateway: task file → "Executing..."
  → Executor: run_claude(prompt) → may run 5-10 minutes
  → Meanwhile user sends new message: "Current task progress?"
    → Gateway: query runtime → "task-xxx running (5 minutes)"
  → Task complete → "Implementation done: deploy-governance.sh updated"
```

### Example 4: Human Intervention

```
User: "Help me release amingClaw"
  → Gateway: classify: task + dangerous keyword "release"
  → Reply: "[Human confirmation needed] Release operation requires confirmation, reply 'confirm release'"
  → User: "confirm release"
  → Gateway: POST /api/wf/amingClaw/release-gate
  → Passed → "Release gate passed ✅"
```

## 11. Implementation Roadmap

| Step | Content | Dependencies |
|------|---------|-------------|
| 1 | Gateway message classifier | None |
| 2 | Gateway task dispatch (write task files) | 1 |
| 3 | Runtime state API | governance |
| 4 | Executor completion notification (Redis pub) | executor.py |
| 5 | Gateway result notification listener | 4 |
| 6 | /menu display runtime status | 3 |
| 7 | Project switch context auto save/load | session_context |
| 8 | Role context isolation | 7 |
