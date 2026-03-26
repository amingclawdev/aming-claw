# Scheduled Task Message Processing Design

## Core Principles

- Each Scheduled Task is bound to one project
- Multiple projects = multiple Task instances
- Project switching is detected via the Gateway routing table
- Context and memory are isolated per project

## Architecture

```
Gateway Routing Table (Redis)
  chat:route:7848961760 → {token_hash, project_id: "amingClaw"}
      │
      │ Auto-updated when user switches project via /bind
      │
      ▼
Scheduled Task: telegram-handler-amingClaw
  │
  ├── 1. Check routing table to confirm chat is still bound to this project
  │     → Yes → Continue processing
  │     → No → Exit silently (user switched to a different project)
  │
  ├── 2. Load project context
  │     GET /api/context/amingClaw/load
  │
  ├── 3. Load project memory
  │     POST /api/context/amingClaw/assemble
  │
  ├── 4. Consume messages + process + reply
  │
  └── 5. Save context
        POST /api/context/amingClaw/save

Scheduled Task: telegram-handler-toolboxClient
  └── Same as above, bound to toolboxClient
```

## Single-Project Task Template

```
Task ID: telegram-handler-{project_id}
Schedule: * * * * * (every minute)

Startup Flow:
  1. CHECK: Is this project the currently active binding?
     → GET /gateway/status → Find the project_id for the chat_id
     → project_id != this task's project → Exit
     → project_id == this task's project → Continue

  2. CHECK: Does the message queue have content?
     → XLEN chat:inbox:{token_hash}
     → 0 → Exit

  3. LOAD: Context + memory
     → GET /api/context/{pid}/load → Previous working state
     → POST /api/context/{pid}/assemble → Project-related memory

  4. PROCESS: Consume messages one by one
     → XREADGROUP + process + XACK
     → Reply: POST /gateway/reply

  5. SAVE: Update context
     → POST /api/context/{pid}/save
     → POST /api/context/{pid}/log (append processing records)
```

## Multi-Project Switching Scenario

```
User switches to toolboxClient via Telegram /menu:
    │
    ▼
Gateway updates routing table:
  chat:route:7848961760 → {project_id: "toolboxClient", token_hash: "xxx"}
    │
    ▼
Next Scheduled Task trigger:
  telegram-handler-amingClaw:
    → Check route → project_id = toolboxClient ≠ amingClaw
    → Exit silently (skip processing)

  telegram-handler-toolboxClient:
    → Check route → project_id = toolboxClient ✓
    → Consume messages → process → reply
```

## Creation Method

A human or Coordinator creates one Task per project:

```bash
# amingClaw
mcp__scheduled-tasks__create_scheduled_task(
    taskId="telegram-handler-amingClaw",
    cronExpression="* * * * *",
    prompt="... bound to amingClaw ..."
)

# toolboxClient
mcp__scheduled-tasks__create_scheduled_task(
    taskId="telegram-handler-toolboxClient",
    cronExpression="* * * * *",
    prompt="... bound to toolboxClient ..."
)
```

## Retained Multi-Project Interaction Capabilities

1. **Cross-project queries**: When a message mentions another project, the task can call that project's API
   ```
   User: "How many nodes do amingClaw and toolboxClient each have?"
   → GET /api/wf/amingClaw/summary
   → GET /api/wf/toolboxClient/summary
   → Merge replies
   ```

2. **Project switch notification**: After user switches project via /bind, the old project's task detects the route change
   → Save context → Notify "Switched to xxx"

3. **Global memory**: dbservice with scope=global can store cross-project common knowledge

4. **Unified Gateway**: Regardless of which project is bound, the Gateway routing table manages everything uniformly; tasks only need to check the route

## Task Prompt Template

```
You are the Coordinator assistant for the {project_id} project.

TOKEN: {coordinator_token}
PROJECT: {project_id}
CHAT_ID: {chat_id}
STREAM: chat:inbox:{token_hash}
BASE_URL: http://localhost:40000

Steps:
1. Check routing: curl -s http://localhost:40000/gateway/status
   → Find the binding for chat_id={chat_id}
   → If project_id is not {project_id}, exit immediately

2. Check queue: docker exec aming_claw-redis-1 redis-cli XLEN {stream}
   → If 0, exit

3. Load context:
   curl -s http://localhost:40000/api/context/{project_id}/load \
     -H "X-Gov-Token: {token}"

4. Read messages:
   docker exec aming_claw-redis-1 redis-cli XRANGE {stream} - + COUNT 5

5. Process each message and reply:
   curl -s -X POST http://localhost:40000/gateway/reply \
     -H "Content-Type: application/json" \
     -H "X-Gov-Token: {token}" \
     -d '{{"token":"{token}","chat_id":{chat_id},"text":"reply content"}}'

6. ACK messages:
   docker exec aming_claw-redis-1 redis-cli XACK {stream} coordinator-group {msg_id}

7. Save context:
   curl -s -X POST http://localhost:40000/api/context/{project_id}/save \
     -H "Content-Type: application/json" \
     -H "X-Gov-Token: {token}" \
     -d '{{"context":{{"current_focus":"...","recent_messages":[...]}}}}'
```
