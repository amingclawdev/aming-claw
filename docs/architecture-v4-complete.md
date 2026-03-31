# Aming Claw Architecture Plan v4

> v3 → v4 core changes: reinforce the foundation. Reliable message delivery, no event loss, no context overwrite, revocable tokens, Agent lifecycle moved earlier.

## I. System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Human User (Telegram)                    │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │    Nginx (:40000)    │  Reverse Proxy / Rate Limiting (reserved)
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
          │  Streams / Pub-Sub / Cache / Locks│
          └──────┬─────────────────────────┘
                 │
          ┌──────▼──────────┐
          │   dbservice     │
          │   (:40002)      │
          │   Memory Layer  │
          └─────────────────┘

─ ─ ─ ─ ─ ─ Docker Internal Network ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─

          ┌─────────────────────────────────┐
          │         Host Machine            │
          │                                 │
          │  Coordinator Session (Claude)   │
          │    ├── ChatProxy (Stream consumer)│
          │    ├── GovernanceClient (HTTP)   │
          │    └── Claude Code CLI          │
          │                                 │
          │  Message Worker (Resident/Scheduled)│
          │    └── Message consumption + context recovery│
          └─────────────────────────────────┘
```

## II. v3 → v4 Change List

| Module | v3 | v4 | Reason |
|------|----|----|------|
| Message Queue | Redis LIST (LPUSH/RPOP) | **Redis Streams + Consumer Group + ACK** | RPOP crashes lose messages |
| Governance Events | Pub/Sub only | **Outbox + Pub/Sub dual-track** | Pub/Sub loses events |
| Session Context | Single refId replace | **Snapshot + Append Log + Version** | Multiple writers overwrite |
| Token Model | 10-year coordinator token | **Refresh + Access dual-token** | Leaked tokens cannot be revoked |
| Agent Lifecycle | Round 5 expansion | **Round 2 infrastructure** | Already a current pain point |
| dbservice Dependency | Synchronous calls | **Async write-back + degradation strategy** | Memory enhancement != memory dependency |
| Scheduled Task | Per-minute RPOP | **Blocking consume + lease + Cron fallback** | Minute-level delay too high |
| Observability | None | **trace_id chaining + structured logging** | No way to troubleshoot |

## III. Five-Layer Architecture (v4 Revision)

### Layer 1: Rules Layer (Governance Service)

**Responsibility**: Enforce workflow rules + event sourcing.

| Module | Function | v4 Change |
|------|------|---------|
| DAG Graph (NetworkX) | Nodes, dependencies, gate policies | Unchanged |
| State Machine (SQLite) | verify status transitions, permission validation | Unchanged |
| Role Service | Token authentication | **Dual-token model** |
| Agent Lifecycle | register/heartbeat/deregister/orphans | **New (moved up from Round 5)** |
| Event Outbox | Event persistence + async delivery | **New** |
| Audit | Who did what when | Unchanged |
| Docs API | /api/docs/* | Unchanged |

**Key Change 1: Dual-Token Model**

```
Human calls /api/init
    → Returns refresh_token (long-term, 90 days, only used to exchange for access_token)
    → Human saves refresh_token

Coordinator starts:
    POST /api/token/refresh {refresh_token}
    → Returns access_token (short-term, 4 hours)
    → All subsequent API calls use access_token

access_token expires:
    → Automatically renews using refresh_token
    → No manual intervention required

Security operations:
    POST /api/token/revoke {refresh_token, password}  ← Human revokes
    POST /api/token/rotate {refresh_token, password}  ← Rotate to new refresh_token
```

| Token | TTL | Holder | Capability |
|-------|-----|--------|------|
| refresh_token | 90 days | Human | Exchange for access_token, revoke |
| access_token | 4 hours | Coordinator | Call all APIs |
| agent_token | 24 hours | Agent (tester/qa/dev) | Call restricted APIs |

**Key Change 2: Event Outbox**

```
State change occurs
    │
    ▼
1. Write SQLite state table (within transaction)
2. Write SQLite outbox table (same transaction)  ← Guarantees atomicity
    │
    ▼
3. Background worker reads outbox
    │
    ├──▶ Redis Pub/Sub (real-time notification, best-effort)
    ├──▶ Redis Stream (persistent, retryable)
    └──▶ dbservice (memory write, async)
    │
    ▼
4. Delivery success → mark outbox row as delivered
   Delivery failure → retry (exponential backoff, max 5 times)
   5 failures → enter dead letter, alert
```

Outbox table schema:
```sql
CREATE TABLE event_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,          -- node.status_changed
    payload TEXT NOT NULL,             -- JSON
    project_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT,                 -- NULL = pending delivery
    retry_count INTEGER DEFAULT 0,
    next_retry_at TEXT,
    dead_letter INTEGER DEFAULT 0,    -- 1 = dead letter
    trace_id TEXT                      -- trace chaining
);
CREATE INDEX idx_outbox_pending ON event_outbox(delivered_at) WHERE delivered_at IS NULL;
```

**Key Change 3: Agent Lifecycle API**

```
POST /api/agent/register
  Body: {role, principal_id, expected_duration_sec}
  Returns: {agent_id, agent_token, lease_id}

POST /api/agent/heartbeat
  Body: {lease_id, status: "idle"|"busy"|"processing"}
  Returns: {ok, lease_renewed_until}

POST /api/agent/deregister
  Body: {lease_id}
  Returns: {ok}

GET  /api/agent/orphans
  Returns: {orphans: [{agent_id, last_heartbeat, lease_expired_at}]}

POST /api/agent/cleanup
  Coordinator calls: clean up orphan agents, release resources, invalidate routes
```

Lease mechanism:
```
Agent registers → gets lease_id (TTL 5 minutes)
Agent heartbeats every 2 minutes → renews lease
No heartbeat for 5+ minutes → lease expires → marked as orphan
Coordinator periodically calls /agent/orphans → discovers orphans → cleans up
Gateway checks routes: coordinator with expired lease → prompt user "offline"
```

### Layer 2: Memory Layer (dbservice)

**Responsibility**: Knowledge storage and retrieval, assists decision-making. **Not a critical path dependency.**

Unchanged parts:
- Knowledge Store (SQLite + FTS4)
- Memory Schema + conflict strategy
- Memory Relations
- Embedder (local vectors)
- Context Assembly

**Key Change: Degradation Strategy**

```python
class MemoryClient:
    """Client for Governance to call dbservice"""

    def write(self, entry):
        try:
            resp = requests.post(f"{DBSERVICE_URL}/knowledge/upsert",
                                 json=entry, timeout=3)
            return resp.json()
        except Exception:
            # Degradation: write to local pending file, write back later
            self._write_to_local_pending(entry)
            log.warning("dbservice unavailable, queued locally")
            return {"ok": True, "degraded": True}

    def query(self, **kwargs):
        try:
            return requests.get(f"{DBSERVICE_URL}/knowledge/find",
                               params=kwargs, timeout=3).json()
        except Exception:
            # Degradation: return empty, do not block main flow
            log.warning("dbservice unavailable, returning empty")
            return {"documents": [], "degraded": True}

    def assemble_context(self, task_type, scope, budget):
        try:
            return requests.post(f"{DBSERVICE_URL}/assemble-context",
                                json={...}, timeout=5).json()
        except Exception:
            # Degradation: minimal context (only project_id + token)
            return {"context": [], "degraded": True}
```

**Principle**: governance runs independently; if dbservice goes down, it only affects "memory enhancement", not rule execution.

**Development workflow domain pack**:
```javascript
registerPack("dev-workflow", {
  types: {
    "node_status":      { conflict: "temporal_replace" },
    "verify_decision":  { conflict: "append" },
    "pitfall":          { conflict: "append_set" },
    "session_snapshot": { conflict: "replace" },       // v4: renamed
    "session_log":      { conflict: "append" },        // v4: new
    "architecture":     { conflict: "replace" },
    "workaround":       { conflict: "append" },
    "release_note":     { conflict: "append" },
  }
})
```

### Layer 3: Message Layer (Telegram Gateway)

**Responsibility**: Message sending/receiving, routing, interactive menus.

Unchanged parts:
- Telegram long polling
- InlineKeyboard interactive menus
- HTTP API (/gateway/bind, /gateway/reply)

**Key Change 1: Redis Streams Replaces LIST**

```
# Gateway writes message
XADD chat:inbox:{token_hash} * chat_id 7848961760 text "hello" ts "2026-..."

# Consumer Group creation (first time)
XGROUP CREATE chat:inbox:{token_hash} coordinator-group 0 MKSTREAM

# Coordinator consumes (blocking wait)
XREADGROUP GROUP coordinator-group worker-1 COUNT 10 BLOCK 30000
  STREAMS chat:inbox:{token_hash} >

# ACK after successful processing
XACK chat:inbox:{token_hash} coordinator-group {message_id}

# Crash recovery: read un-ACKed messages
XREADGROUP GROUP coordinator-group worker-1 COUNT 10
  STREAMS chat:inbox:{token_hash} 0
```

Comparison:

| | v3 (LIST) | v4 (Streams) |
|---|---|---|
| Consumer crash | Message lost | Un-ACKed messages auto-redelivered |
| Multiple consumers | Race for messages | Consumer Group distributes |
| Historical replay | Not possible | XRANGE query by time |
| Monitoring | LLEN | XINFO GROUPS / XPENDING |

**Key Change 2: Routing Table Adds Lease Awareness**

```python
def get_route(chat_id):
    route = redis.get(f"chat:route:{chat_id}")
    if not route:
        return None
    # Check if coordinator is still alive
    lease = redis.get(f"lease:{route['token_hash']}")
    if not lease:
        route["status"] = "offline"
    else:
        route["status"] = "online"
    return route
```

When user sends a message:
- coordinator online → forward normally
- coordinator offline → prompt "Coordinator offline, message queued, please start a session on your computer"

### Layer 4: Cache/Communication Layer (Redis)

| Purpose | Key Pattern | Type | v4 Change |
|------|---------|------|---------|
| Session Cache | session:{id} | STRING | Unchanged |
| Token Mapping | token:{hash} | STRING | **Added refresh/access distinction** |
| Distributed Lock | lock:{name} | STRING (NX) | Unchanged |
| Idempotency Key | idem:{key} | STRING | Unchanged |
| **Message Queue** | chat:inbox:{hash} | **STREAM** | **LIST → STREAM** |
| Routing Table | chat:route:{cid} | STRING | Unchanged |
| Reverse Route | chat:reverse:{hash} | STRING | Unchanged |
| Event Notifications | gov:events:{pid} | Pub/Sub | **Downgraded to best-effort** |
| **Event Stream** | gov:stream:{pid} | **STREAM** | **New: persistent events** |
| **Agent Lease** | lease:{token_hash} | **STRING (EX)** | **New** |
| **Worker Lock** | worker:{hash}:owner | **STRING (NX EX)** | **New: single consumer** |

### Layer 5: Execution Layer (Host Machine)

**Key Change: Message Worker Replaces Pure Scheduled Task**

```
┌─ Message Worker (Resident Process) ─────────────────────┐
│                                                          │
│  Main loop:                                              │
│    XREADGROUP BLOCK 30000 → has messages → process → ACK│
│    30 seconds no message → renew lease → continue BLOCK  │
│    lease expired → check if interactive session takeover │
│                                                          │
│  Degradation:                                            │
│    Worker crashes → Cron fallback (checks XPENDING every 5 min)│
│                                                          │
└──────────────────────────────────────────────────────────┘
```

Two consumption modes coexist:

| Mode | Trigger | Latency | Purpose |
|------|------|------|------|
| **Interactive Session** | Human starts Claude Code | Real-time | ChatProxy directly uses XREADGROUP |
| **Message Worker** | Resident/Scheduled Task | <1 second (BLOCK) | Auto-process when unattended |
| **Cron Fallback** | Every 5 minutes | 5 minutes | Last resort when Worker also fails |

Mutual exclusion guarantee (single consumer):
```python
# Worker acquires lock on startup
acquired = redis.set(f"worker:{token_hash}:owner", worker_id, nx=True, ex=60)
if not acquired:
    # Another worker or interactive session is consuming
    log.info("Another consumer active, standing by")
    return

# Renew lock every 30 seconds
redis.expire(f"worker:{token_hash}:owner", 60)

# Release on exit
redis.delete(f"worker:{token_hash}:owner")
```

## IV. Session Context (v4 Revision)

### 4.1 Changed from replace to Snapshot + Log

```
session_snapshot (replace, latest snapshot)
    │
    │  Overwritten on each save
    │
    ├── coordinator_token
    ├── chat_id
    ├── project_id
    ├── current_focus
    ├── active_nodes
    ├── pending_tasks
    ├── version: 42          ← optimistic lock
    ├── updated_at
    └── recent_messages (last 20, compressed from log)

session_log (append, event log)
    │
    │  Append on each message/action
    │
    ├── {type:"msg_in", text:"...", ts:"..."}
    ├── {type:"msg_out", text:"...", ts:"..."}
    ├── {type:"action", action:"verify_update", node:"L1.3", ts:"..."}
    ├── {type:"decision", content:"Fix A2 before A4", ts:"..."}
    └── ...
```

### 4.2 Optimistic Lock on Save

```python
def save_context(project_id, context, expected_version):
    current = load_snapshot(project_id)
    if current and current.get("version", 0) != expected_version:
        raise ConflictError(
            f"Context version conflict: expected {expected_version}, "
            f"got {current['version']}. Another session modified it."
        )
    context["version"] = expected_version + 1
    upsert_snapshot(project_id, context)
```

### 4.3 Expiry Archiving

```
Context inactive for 24h
    │
    ▼
Archive Scheduled Task:
    1. Read session_log
    2. Extract valuable entries:
       - type:"decision" → write to long-term verify_decision
       - type:"action" + failed → write to pitfall
       - type:"msg_in" involving architecture → write to architecture
    3. Compress log to session_summary → write to long-term memory
    4. Clear expired snapshot + log
```

## V. Coordinator Session Lifecycle (v4 Revision)

### 5.1 Interactive Session

```
Human starts Claude Code
    │
    ▼
[INIT]
    │  POST /api/token/refresh {refresh_token}  ← Exchange for access_token
    │  GET /api/docs/quickstart                 ← Onboarding guide
    │  GET context snapshot                     ← Restore last state
    │  POST /api/agent/register                 ← Register + get lease
    │  ChatProxy.bind(chat_id)                  ← Bind Telegram
    │  Acquire worker lock (NX)                 ← Take over message consumption
    │  XREADGROUP 0 → consume un-ACKed messages ← Recover crash residuals
    │
    ▼
[ACTIVE]
    │  ┌─────────────────────────────────────────┐
    │  │  Input:                                 │
    │  │    Terminal / ChatProxy(Stream) / Gov events│
    │  │                                         │
    │  │  Processing:                            │
    │  │    governance API / dbservice / CLI     │
    │  │                                         │
    │  │  Output:                                │
    │  │    /gateway/reply / code changes / status updates│
    │  │                                         │
    │  │  Continuously:                          │
    │  │    heartbeat renewal (every 2 minutes)  │
    │  │    worker lock renewal (every 30 seconds)│
    │  │    session_log append (every action)    │
    │  │    snapshot save (every 5 min or after important actions)│
    │  └─────────────────────────────────────────┘
    │
    ▼
[SUSPEND] (Human temporarily away)
    │  save snapshot (with version)
    │  release worker lock → Message Worker can take over
    │  heartbeat continues → lease does not expire
    │  ChatProxy continues listening → messages queue in Stream
    │
    ▼
[RESUME]
    │  acquire worker lock
    │  load snapshot
    │  XREADGROUP 0 → consume backlog
    │  continue working
    │
    ▼
[EXIT]
    │  save snapshot (final)
    │  POST /api/agent/deregister → release lease
    │  release worker lock
    │  ChatProxy.stop()
    │  → Gateway next lease check → offline
    │  → User message → "Coordinator offline, message queued"
```

### 5.2 Message Worker (Resident)

```
Start (systemd / Scheduled Task / manual)
    │
    ▼
[INIT]
    │  POST /api/token/refresh → access_token
    │  POST /api/agent/register → lease
    │
    ▼
[STANDBY] Waiting for worker lock
    │  Attempt SET worker:{hash}:owner NX EX 60
    │  ├── Acquired → enter CONSUME
    │  └── Not acquired → interactive session is consuming
    │       sleep 30s → retry
    │
    ▼
[CONSUME] Blocking consumption loop
    │  while True:
    │    XREADGROUP BLOCK 30000 COUNT 5
    │    ├── Has messages:
    │    │   load context snapshot
    │    │   POST dbservice /assemble-context (can degrade)
    │    │   process each → ACK
    │    │   save context snapshot
    │    │   POST /gateway/reply
    │    │
    │    ├── No messages (30s timeout):
    │    │   renew lease heartbeat
    │    │   renew worker lock
    │    │   continue
    │    │
    │    └── Worker lock taken (interactive session starts):
    │        release → return to STANDBY
    │
    ▼
[EXIT]
    │  release worker lock + lease
    │  → Cron fallback takes over after 5 minutes
```

### 5.3 Cron Fallback

```python
# Execute every 5 minutes
# Check if there are unconsumed messages and no active worker

def cron_fallback():
    for token_hash in get_all_coordinator_hashes():
        # Check if there is an active worker
        owner = redis.get(f"worker:{token_hash}:owner")
        if owner:
            continue  # Someone is consuming

        # Check XPENDING
        pending = redis.xpending(f"chat:inbox:{token_hash}", "coordinator-group")
        if pending["count"] > 0:
            log.warning("Orphaned messages found for %s, processing", token_hash)
            # Claim and process
            claim_and_process(token_hash)

        # Check new messages (not read by any group)
        info = redis.xinfo_stream(f"chat:inbox:{token_hash}")
        if info["length"] > 0:
            process_new_messages(token_hash)
```

## VI. Observability

### 6.1 Trace ID Chaining

```
User Telegram message
    │ trace_id = "tr-{uuid}"  ← Generated by Gateway
    ▼
Gateway log: [tr-xxx] msg from 7848961760: "query L1.3"
    │
    ▼
Redis Stream: message_id + trace_id
    │
    ▼
Worker log: [tr-xxx] processing message
    │
    ├─▶ Governance: [tr-xxx] GET /api/wf/amingClaw/node/L1.3
    ├─▶ dbservice:  [tr-xxx] /knowledge/find?tags=L1.3
    └─▶ Gateway:    [tr-xxx] POST /gateway/reply
         │
         ▼
Telegram reply [tr-xxx] complete
```

### 6.2 Structured Log Format

```json
{
  "ts": "2026-03-22T13:35:00Z",
  "level": "info",
  "service": "gateway",
  "trace_id": "tr-a1b2c3",
  "message_id": "msg-123",
  "session_id": "ses-xxx",
  "event": "message_forwarded",
  "chat_id": 7848961760,
  "token_hash": "9cb15f91",
  "duration_ms": 12
}
```

### 6.3 Key Metrics

| Metric | Source | Alert Threshold |
|------|------|---------|
| inbox backlog message count | XLEN chat:inbox:* | > 50 |
| Un-ACKed message count | XPENDING | > 10 for 5 minutes |
| outbox undelivered count | SELECT COUNT WHERE delivered_at IS NULL | > 20 |
| Dead letter count | SELECT COUNT WHERE dead_letter = 1 | > 0 |
| Agent orphan count | /api/agent/orphans | > 0 for 10 minutes |
| dbservice degradation count | Log count | > 5/minute |
| Message end-to-end latency | trace_id start/end time diff | > 60 seconds |

## VII. Docker Compose (v4 Complete)

```yaml
services:
  nginx:
    image: nginx:alpine
    ports: ["30000:80"]
    volumes: [./nginx/nginx.conf:/etc/nginx/nginx.conf:ro]
    depends_on:
      governance: { condition: service_healthy }
    restart: unless-stopped

  governance:
    build: { context: ., dockerfile: Dockerfile.governance }
    expose: ["30006"]
    volumes:
      - governance-data:/app/shared-volume/codex-tasks/state/governance
      - .:/workspace:ro
    environment:
      - GOVERNANCE_PORT=40006
      - REDIS_URL=redis://redis:6379/0
      - DBSERVICE_URL=http://dbservice:40002  # new
      - SHARED_VOLUME_PATH=/app/shared-volume
    depends_on:
      redis: { condition: service_healthy }
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:40006/api/health')"]
      interval: 10s
      timeout: 5s
      retries: 3

  telegram-gateway:
    build: { context: ., dockerfile: Dockerfile.telegram-gateway }
    expose: ["30010"]
    env_file: [.env]
    environment:
      - GOVERNANCE_URL=http://governance:40006
      - REDIS_URL=redis://redis:6379/0
      - GATEWAY_PORT=40010
    depends_on:
      governance: { condition: service_healthy }
      redis: { condition: service_healthy }
    restart: unless-stopped

  dbservice:                          # new
    build: { context: ./dbservice }
    expose: ["40002"]
    ports: ["40002:40002"]            # host also needs access
    volumes:
      - memory-data:/app/db
    environment:
      - DBSERVICE_PORT=40002
      - DBSERVICE_SAVE_PATH=/app/db
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "node", "-e", "require('http').get('http://localhost:40002/health',r=>{process.exit(r.statusCode===200?0:1)})"]
      interval: 10s
      timeout: 5s
      retries: 3

  redis:
    image: redis:7-alpine
    expose: ["6379"]
    ports: ["40079:6379"]
    volumes: [redis-data:/data]
    command: redis-server --appendonly yes --maxmemory 128mb --maxmemory-policy allkeys-lru
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 3
    restart: unless-stopped

volumes:
  governance-data: { driver: local }
  redis-data: { driver: local }
  memory-data: { driver: local }       # new
  task-data: { driver: local }
```

## VIII. Nginx Routing (v4 Complete)

```nginx
upstream governance       { server governance:40006; }
upstream telegram-gateway { server telegram-gateway:40010; }
upstream dbservice        { server dbservice:40002; }

server {
    listen 80;

    location /nginx-health { return 200 '{"ok":true}'; }

    location /api/     { proxy_pass http://governance/api/; ... }
    location /gateway/ { proxy_pass http://telegram-gateway/gateway/; ... }
    location /memory/  { proxy_pass http://dbservice/; ... }       # new

    # dev (on demand)
    location /dev/api/ {
        set $dev governance-dev:40007;
        proxy_pass http://$dev/api/; ...
    }
}
```

## IX. Implementation Roadmap (v4 Reordered)

### P0: Foundation (Immediate)

1. **Redis Streams Message Queue**
   - Gateway: XADD replaces LPUSH
   - ChatProxy: XREADGROUP replaces RPOP
   - Consumer Group + ACK

2. **Event Outbox**
   - outbox table + background worker
   - Pub/Sub downgraded to best-effort notification

3. **Dual-Token Model**
   - /api/token/refresh, /api/token/revoke
   - access_token 4h + refresh_token 90d

4. **Agent Lifecycle**
   - register/heartbeat/deregister/orphans
   - Lease mechanism + expiry detection

### P1: Consistency (Next)

5. **Session Context snapshot + log + version**
   - Optimistic lock prevents overwrite
   - Append log prevents data loss

6. **dbservice Dockerize**
   - Add to compose
   - Register dev-workflow domain pack
   - Degradation strategy

7. **Message Worker**
   - Blocking consume + lease + Cron fallback
   - Single consumer mutual exclusion

8. **Observability**
   - trace_id chaining
   - Structured logging
   - Key metrics monitoring

### P2: Capability Enhancement

9. **Context Assembly integration**
10. **Expired context auto-archiving**
11. **Governance memory → dbservice proxy**
12. **Task registry (file → table)**

### P3: Workflow Features

13. **import-graph sync status**
14. **Agent friendly error messages**
15. **Status skip API**
16. **Release Profile**
17. **Gate policy**
18. **Impact analysis policy**
19. **Vector retrieval on demand**
