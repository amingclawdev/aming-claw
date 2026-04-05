---
status: archived
superseded_by: architecture-v7-context-service.md
archived_date: 2026-04-05
historical_value: "Initial architecture design with component definitions"
do_not_use_for: "component design decisions"
---

# Aming Claw Complete Architecture Plan v3

## I. System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Human User (Telegram)                    │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │    Nginx (:30000)    │  Reverse Proxy
                    └──┬─────────┬────────┘
                       │         │
          ┌────────────▼──┐  ┌──▼────────────────┐
          │  Governance   │  │  Telegram Gateway  │
          │  (:30006)     │  │  (:30010)          │
          │  Rules Layer  │  │  Message Layer     │
          └──────┬────────┘  └──────┬─────────────┘
                 │                  │
          ┌──────▼──────────────────▼──────┐
          │           Redis (:6379)         │
          │   Cache / Pub-Sub / Message Queue│
          └──────┬─────────────────────────┘
                 │
          ┌──────▼──────────┐
          │   dbservice     │
          │   (:30002)      │
          │   Memory Layer  │
          └─────────────────┘

─ ─ ─ ─ ─ Docker Internal Network ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─

          ┌─────────────────────────────────┐
          │         Host Machine            │
          │                                 │
          │  Coordinator Session (Claude)   │
          │    ├── ChatProxy (Redis Subscribe)│
          │    ├── GovernanceClient (HTTP)   │
          │    └── Claude Code CLI          │
          │                                 │
          │  Scheduled Task (Timed)         │
          │    └── Message Processing + Context Recovery │
          └─────────────────────────────────┘
```

## II. Five-Layer Architecture

### Layer 1: Rules Layer (Governance Service)

**Responsibility**: Enforce workflow rules, cannot be bypassed.

| Module | Function |
|------|------|
| DAG Graph (NetworkX) | Node definitions, dependency relationships, gate policies |
| State Machine (SQLite) | verify status transitions, permission validation |
| Role Service | principal + session model, token authentication |
| Audit | Who made what change at what time |
| Release Gate | release-gate checks |
| Docs API | /api/docs/* onboarding guide |

**Data Storage**:
- Graph definitions: JSON + NetworkX (primarily read-only)
- Runtime state: SQLite per project (WAL mode)
- Audit: JSONL append-only + SQLite index

**Principle**: governance manages "what is allowed".

### Layer 2: Memory Layer (dbservice)

**Responsibility**: Store and retrieve development knowledge to assist Agent decision-making.

| Module | Function |
|------|------|
| Knowledge Store | Structured knowledge CRUD + FTS4 full-text search |
| Memory Schema | Type classification + conflict strategy (replace/append/temporal_replace) |
| Memory Relations | Inter-document relationship graph |
| Embedder | Local vector embedding (Xenova/all-MiniLM-L6-v2, no API required) |
| Context Assembly | Auto-assembles context by task type + token budget |
| Semantic Search | mem0 vector similarity search + deduplication |

**Data Storage**:
- Structured: SQLite + FTS4
- Vectors: mem0 (SQLite-backed)
- All runs locally, zero external API dependencies

**Development workflow dedicated domain pack**:
```
node_status      — Node status change records     (temporal_replace)
verify_decision  — Verification decisions and reasons (append)
pitfall          — Lessons learned records        (append_set)
session_context  — Short-term session context     (replace, TTL 24h)
architecture     — Architecture decisions         (replace)
workaround       — Temporary solutions            (append)
release_note     — Release records                (append)
```

**Principle**: dbservice manages "how to do it".

### Layer 3: Message Layer (Telegram Gateway)

**Responsibility**: Message sending/receiving, routing, interactive menus.

| Function | Implementation |
|------|------|
| Telegram Long Polling | getUpdates polling |
| Message Routing | Redis routing table: chat_id → coordinator token |
| Interactive Menus | InlineKeyboard: coordinator list, switching, status view |
| HTTP API | /gateway/bind, /gateway/reply, /gateway/unbind |
| Event Notifications | Redis Pub/Sub subscription gov:events:* → Telegram push |
| Multi-coordinator | Routing table supports multiple coordinators bound to different chats |

**Message Queue (Reliable Delivery)**:
```
User message enqueue:  LPUSH chat:inbox:{token_hash} {message_json}
Coordinator consume:   RPOP chat:inbox:{token_hash}
Messages are not lost, consumed in order.
```

### Layer 4: Cache/Communication Layer (Redis)

| Purpose | Key Pattern |
|------|---------|
| Session Cache | session:{id}, token:{hash} |
| Distributed Lock | lock:{name} |
| Idempotency Key | idem:{key} |
| Message Queue | chat:inbox:{token_hash} (LIST) |
| Routing Table | chat:route:{chat_id}, chat:reverse:{token_hash} |
| Event Notifications | gov:events:{project_id} (Pub/Sub) |
| Session Context Cache | context:{project_id}:{token_hash} |

### Layer 5: Execution Layer (Host Machine)

**Responsibility**: Run Claude Code / Codex and other CLI tools.

| Component | Function |
|------|------|
| Coordinator Session | Claude Code interactive session |
| ChatProxy | Redis subscribed messages → process → reply |
| GovernanceClient | HTTP calls to governance API |
| Executor Worker | Monitor task files → start CLI → write results |

## III. Coordinator Session Lifecycle

### 3.1 Session Types

| Type | Trigger | Lifecycle | Purpose |
|------|---------|---------|------|
| **Interactive Session** | Human starts Claude Code in terminal | Human-controlled, manual exit | Daily development, complex tasks |
| **Scheduled Session** | Scheduled task auto-starts | Auto-exits on completion | Message processing, health checks, automation |

### 3.2 Interactive Session Lifecycle

```
Human starts Claude Code
    │
    ▼
[INIT] Load memory
    │  GET /api/docs/quickstart         ← Get onboarding guide
    │  GET /api/context/{pid}/load      ← Restore last working state
    │  ChatProxy.bind(chat_id, pid)     ← Bind Telegram
    │
    ▼
[ACTIVE] Work loop
    │  ┌─────────────────────────────────────────┐
    │  │  Input sources:                          │
    │  │    1. Human terminal input (direct)      │
    │  │    2. Telegram messages (ChatProxy → Redis)│
    │  │    3. Governance events (Redis Pub/Sub)  │
    │  │                                          │
    │  │  Execute actions:                        │
    │  │    - Call governance API (verify/baseline)│
    │  │    - Call dbservice (write/query memory) │
    │  │    - Run Claude Code CLI (code changes)  │
    │  │    - Reply Telegram (ChatProxy.reply)    │
    │  │                                          │
    │  │  Continuously save:                      │
    │  │    - Session context → dbservice          │
    │  │    - Important decisions → dbservice (verify_decision)│
    │  │    - Governance events → auto-record     │
    │  └─────────────────────────────────────────┘
    │
    ▼
[SUSPEND] Human temporarily away
    │  POST /api/context/{pid}/save     ← Save current state
    │  {
    │    current_focus: "Fix A1-A4",
    │    pending_tasks: [...],
    │    recent_messages: [...last 20...],
    │    active_nodes: ["L1.3", "L2.1"]
    │  }
    │  ChatProxy continues listening (background thread)
    │  → New messages enter Redis LIST, not lost
    │
    ▼
[RESUME] Human returns / Scheduled Task recovers
    │  GET /api/context/{pid}/load      ← Load state
    │  RPOP chat:inbox:{hash}           ← Consume backlogged messages
    │  Continue working
    │
    ▼
[EXIT] Session ends
    │  POST /api/context/{pid}/save     ← Final save
    │  POST /api/context/{pid}/archive  ← Archive valuable content to long-term memory
    │  ChatProxy.stop()
    │  Gateway auto-detects coordinator offline
    │  → When Telegram user sends message, prompt "Coordinator offline"
```

### 3.3 Scheduled Session Lifecycle

```
Timed trigger (every 1 minute / on demand)
    │
    ▼
[INIT] Minimal startup
    │  1. Check Redis LIST chat:inbox:{hash} for messages
    │     → No messages → exit immediately (no resource waste)
    │  2. Has messages → continue
    │
    ▼
[LOAD CONTEXT] Restore context
    │  GET /api/context/{pid}/load
    │  POST dbservice /assemble-context {
    │    task_type: "telegram_handler",
    │    scope: project_id,
    │    token_budget: 4000
    │  }
    │  → Get: session_context + relevant decisions + pitfalls
    │
    ▼
[PROCESS] Process messages
    │  while msg = RPOP chat:inbox:{hash}:
    │    1. Understand message (with context)
    │    2. Determine type:
    │       - Query → answer directly (query governance/dbservice)
    │       - Action → execute (call governance API)
    │       - Task → create task file (wait for executor to execute)
    │       - Chat → simple reply
    │    3. POST /gateway/reply → reply Telegram
    │    4. Append to session context
    │
    ▼
[SAVE & EXIT]
    │  POST /api/context/{pid}/save     ← Save updated context
    │  If important decisions → write to long-term memory
    │  Session auto-ends
```

### 3.4 Session Context Store

**Storage location**: dbservice (type: session_context, TTL: 24h)

```json
{
  "type": "session_context",
  "scope": "amingClaw",
  "content": {
    "coordinator_token": "gov-3506be...",
    "chat_id": 7848961760,
    "project_id": "amingClaw",
    "current_focus": "Fix A1-A4 base items",
    "active_nodes": ["L1.3", "L2.1", "L0.1"],
    "pending_tasks": [
      "import-graph sync status",
      "Agent friendly error messages"
    ],
    "recent_messages": [
      {"role": "user", "text": "What is the status of L1.3?", "ts": "2026-03-22T13:00:00Z"},
      {"role": "coordinator", "text": "L1.3 is currently testing...", "ts": "2026-03-22T13:00:05Z"}
    ],
    "decisions_this_session": [
      "Decided to fix import-graph before fixing error handling"
    ]
  },
  "updated_at": "2026-03-22T13:35:00Z",
  "ttl_hours": 24
}
```

**Expiry archiving process**:

```
Context TTL expires
    │
    ▼
Archive check (Scheduled Task)
    │
    ├── Decisions in recent_messages → write to verify_decision
    ├── Discovered pitfalls → write to pitfall
    ├── Architecture changes → write to architecture
    └── Routine conversation → discard
    │
    ▼
Clear expired context
```

## IV. Complete Data Flow

### 4.1 User Sends Message → Coordinator Processes → Reply

```
User Telegram message: "Help me check the status of L1.3"
    │
    ▼
Gateway (Docker)
    │  1. poll_updates receives message
    │  2. Look up routing table: chat:route:7848961760 → coordinator token_hash
    │  3. LPUSH chat:inbox:9cb15f91 {text, chat_id, ts}
    │
    ▼
Redis LIST: chat:inbox:9cb15f91
    │
    ▼
Coordinator Session (Host Machine)
    │  1. ChatProxy.RPOP → receive message
    │  2. Query dbservice: relevant memory (L1.3 pitfall/decision)
    │  3. Query governance: GET /api/wf/amingClaw/node/L1.3
    │  4. Assemble reply
    │  5. POST /gateway/reply → "L1.3 is currently testing, last tester-001..."
    │  6. Write dbservice: append session_context
    │
    ▼
Gateway → Telegram API → User receives reply
```

### 4.2 Governance Event → Auto Notification + Auto Memory

```
An Agent calls verify-update: L2.1 → qa_pass
    │
    ▼
Governance
    │  1. State machine validates → allowed
    │  2. SQLite write
    │  3. EventBus.publish("node.status_changed", payload)
    │
    ▼
Redis Pub/Sub: gov:events:amingClaw
    │
    ├──▶ Gateway: format → Telegram notification: "✅ L2.1 → qa_pass"
    │
    └──▶ Governance: auto-write dbservice
         POST dbservice /knowledge/upsert {
           type: "node_status",
           refId: "L2.1:qa_pass:2026-03-22",
           content: "L2.1 passed QA verification",
           tags: ["L2.1", "qa_pass"],
           scope: "amingClaw"
         }
```

### 4.3 Scheduled Task Message Processing

```
Cron trigger (every 1 minute)
    │
    ▼
New Session starts
    │
    ▼
Check Redis: LLEN chat:inbox:9cb15f91
    │
    ├── 0 messages → exit (<1 second)
    │
    └── 3 messages → continue processing
         │
         ▼
    Load context:
         GET dbservice /knowledge/find?type=session_context&scope=amingClaw
         POST dbservice /assemble-context {task_type: "telegram_handler"}
         │
         ▼
    Process each message:
         msg1: "What is L1.3's status" → query governance → reply
         msg2: "Help me run the tests" → create task file → reply "Task created"
         msg3: "Was that bug fixed" → query dbservice memory → reply
         │
         ▼
    Save context:
         POST dbservice /knowledge/upsert {type: "session_context", ...}
         │
         ▼
    Session ends
```

## V. Complete Docker Compose Topology

```yaml
services:
  nginx:          # Reverse proxy (:30000)
  governance:     # Rules layer (:30006)
  governance-dev: # Dev environment (:30007, profile: dev)
  telegram-gateway: # Message layer (:30010)
  dbservice:      # Memory layer (:30002)
  redis:          # Cache/communication (:6379, host:6380)

volumes:
  governance-data:     # governance SQLite + graph
  governance-dev-data: # dev environment data
  redis-data:          # Redis AOF
  memory-data:         # dbservice SQLite + vectors
  task-data:           # Task files (shared-volume)
```

**Port Mapping**:

| Service | Container Port | Host Port | Purpose |
|------|---------|-----------|------|
| Nginx | 80 | 30000 | Unified entry point |
| Governance | 30006 | (nginx) | Rules API |
| Gateway | 30010 | (nginx) | Message API |
| dbservice | 30002 | 30002 | Memory API |
| Redis | 6379 | 6380 | Host access |

**Nginx Routing**:

| Path | Upstream |
|------|------|
| /api/* | governance:30006 |
| /gateway/* | telegram-gateway:30010 |
| /dev/api/* | governance-dev:30007 (on demand) |
| /memory/* | dbservice:30002 (new) |

## VI. Governance ↔ dbservice Interaction Contract

### 6.1 Governance Writes Memory (Event-driven)

Governance EventBus subscribers automatically write events to dbservice:

```python
# New subscriber added in governance/event_bus.py
def _write_to_memory(payload):
    requests.post("http://dbservice:30002/knowledge/upsert", json={
        "refId": f"{payload['node_id']}:{payload['event']}:{payload['timestamp']}",
        "type": event_to_memory_type(payload["event"]),
        "title": format_title(payload),
        "body": json.dumps(payload),
        "tags": extract_tags(payload),
        "scope": payload.get("project_id", "global"),
        "status": "active",
    })
```

### 6.2 Coordinator Queries Memory

```python
# Query all related memory for a node
GET dbservice:30002/knowledge/find?scope=amingClaw&tags=L1.3

# Semantic search
POST dbservice:30002/search
{"query": "file lock concurrency issue", "namespace": "amingClaw"}

# Assemble context (when Scheduled Task starts)
POST dbservice:30002/assemble-context
{"task_type": "telegram_handler", "scope": "amingClaw", "token_budget": 4000}
```

### 6.3 Session Context (Short-term)

```python
# Save
POST dbservice:30002/knowledge/upsert
{
    "refId": "session-context:amingClaw",
    "type": "session_context",
    "title": "Coordinator Session Context",
    "body": json.dumps(context_data),
    "scope": "amingClaw",
    "status": "active",
    "meta": {"ttl_hours": 24}
}

# Load
GET dbservice:30002/knowledge/find?type=session_context&scope=amingClaw&refId=session-context:amingClaw
```

## VII. Scheduled Task Configuration

```python
# Create message processing scheduled task
create_scheduled_task(
    taskId="telegram-message-handler",
    cronExpression="* * * * *",  # Every minute
    description="Check Telegram message queue and process",
    prompt="""
    You are the Coordinator for the amingClaw project.

    1. Connect to Redis (redis://localhost:6380/0)
    2. Check message queue: LLEN chat:inbox:9cb15f91dcad09a5
       - If 0, end immediately
    3. Load context: GET http://localhost:30002/knowledge/find?type=session_context&scope=amingClaw
    4. Process messages one by one: RPOP chat:inbox:9cb15f91dcad09a5
       - Query type → call governance API for status, reply
       - Action type → call governance API to execute, reply
       - Chat type → simple reply
    5. Reply: POST http://localhost:30000/gateway/reply
       {token: "gov-3506be...", chat_id: 7848961760, text: "..."}
    6. Save context: POST http://localhost:30002/knowledge/upsert
    """,
)
```

## VIII. Security Boundaries

| Level | Security Measure |
|------|---------|
| Init | Password protected, one-time use |
| Coordinator Token | 10-year TTL, held and distributed by human |
| Agent Token | 24h TTL, allocated by coordinator |
| Gateway | Only allows bind/reply after token verification |
| Governance | All state changes require token + role permission |
| dbservice | Scope isolation, partitioned by project_id |
| Redis | Container internal network, host accesses via 6380 |
| Nginx | Unified entry point, rate limiting can be added in the future |

## IX. Implementation Roadmap

### Round 1: Currently Completed ✅

- [x] Governance Service (Docker, port 30006)
- [x] Redis (Docker, port 6380)
- [x] Nginx reverse proxy (Docker, port 30000)
- [x] Telegram Gateway (Docker, port 30010)
- [x] EventBus → Redis Pub/Sub bridge
- [x] Gateway interactive menus (/menu, InlineKeyboard)
- [x] Gateway HTTP API (/gateway/bind, /gateway/reply)
- [x] Governance /api/docs/* documentation endpoints
- [x] Governance /api/role/verify endpoint
- [x] ChatProxy host client

### Round 2: Memory Layer Integration

- [ ] dbservice Dockerize and add to compose
- [ ] Register dev-workflow domain pack
- [ ] Governance memory_service → dbservice proxy
- [ ] Session Context API (save/load/archive)
- [ ] EventBus events → dbservice auto-write
- [ ] Nginx add /memory/* route

### Round 3: Scheduled Task + Automation

- [ ] Gateway inbox changed to Redis LIST (reliable delivery)
- [ ] Create telegram-message-handler Scheduled Task
- [ ] Context Assembly integration
- [ ] Expired context auto-archive to long-term memory

### Round 4: Base Item Fixes (via workflow)

- [ ] A2: import-graph sync status (parse [verify:pass])
- [ ] A4: Agent friendly error messages (evidence validation)
- [ ] A5: Status skip API (force_baseline)
- [ ] A3: Project ID normalization (normalize)
- [ ] A1: API documentation auto-generation

### Round 5: Capability Expansion

- [ ] Agent Lifecycle API (register/deregister/orphans)
- [ ] Release Profile (check by scope, does not require full project green)
- [ ] Gate policy (min_status, policy)
- [ ] Impact analysis policy (file hit + propagation policy)
- [ ] Vector retrieval on demand (sqlite-vec or Chroma)
