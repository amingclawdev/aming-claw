# Telegram Chat-to-Project Binding Runtime Design

> **2026-03-26 Confirmed:** telegram_gateway (port 40010) is the sole Telegram entry point. The old coordinator.py Telegram polling mechanism has been fully removed (along with bot_commands.py, interactive_menu.py, and 20 other modules). All Telegram message routing goes through Gateway → Governance API.

## Problem

A Telegram chat has only one chat_id, but may be bound to different projects. When switching projects:
- Context must be isolated (amingClaw conversations should not appear in toolboxClient)
- Memory must be isolated (dbservice queries use different scopes)
- Scheduled Tasks must be aware of switches (only consume messages for the currently bound project)
- Historical messages must not be lost (what happens to unprocessed messages from the old project after switching)

## Architecture

```
Telegram chat_id: 7848961760
    │
    ▼
Gateway routing table (Redis):
  chat:route:7848961760 → {
    project_id: "amingClaw",         ← currently active project
    token_hash: "9cb15f91",
    token: "gov-3506be...",
    bound_at: "2026-03-22T..."
  }
    │
    ├── Messages enter: chat:inbox:9cb15f91  (amingClaw's stream)
    │
    │   User /bind toolboxClient token → routing table updated
    │
    └── Messages enter: chat:inbox:6643e5d7  (toolboxClient's stream)
```

## Runtime State Model

```
Independent runtime state per project:

Redis:
  chat:route:{chat_id}              → currently active binding
  chat:inbox:{token_hash}           → message stream for this project
  context:snapshot:{project_id}     → session context for this project
  context:log:{project_id}          → session log for this project
  lease:{lease_id}                  → agent lease for this project

SQLite (per project):
  governance.db                     → node status, sessions, outbox
  gatekeeper_checks                 → coverage-check results

dbservice:
  scope={project_id}                → memory for this project
```

## Project Switching Flow

```
User sends: /bind gov-48ed6f69... (toolboxClient token)
    │
    ▼
Gateway:
  1. Validate token → confirm it is toolboxClient coordinator
  2. Save old binding's context:
     POST /api/context/amingClaw/save (automatic)
  3. Update routing table:
     chat:route:7848961760 → {project_id: "toolboxClient", ...}
  4. Load new project context:
     GET /api/context/toolboxClient/load
  5. Reply to user:
     "Switched to toolboxClient (89 nodes, 89 qa_pass)"
    │
    ▼
Scheduled Task awareness:
  telegram-handler-amingclaw:
    → Check route → project_id = toolboxClient ≠ amingClaw
    → Exit silently

  telegram-handler-toolboxclient:
    → Check route → project_id = toolboxClient ✓
    → Consume messages → process
```

## /menu Interactive Switching

```
User sends: /menu
    │
    ▼
Gateway builds menu:
  ┌─────────────────────────────────┐
  │ Aming Claw Gateway              │
  │                                 │
  │ Current: amingClaw (9cb15f91...)│
  │ Registered Coordinators: 2      │
  │                                 │
  │ [>> amingClaw (9cb1)]           │  ← currently active
  │ [   toolboxClient (6643)]       │  ← switchable
  │                                 │
  │ [Project Status] [Project List] │
  │ [Service Health] [Unbind]       │
  └─────────────────────────────────┘
    │
    ▼
User clicks "toolboxClient (6643)":
    │
    ▼
Gateway callback_query:
  1. Auto-save amingClaw context
  2. Switch route → toolboxClient
  3. Load toolboxClient context
  4. Refresh menu:
     "Current: toolboxClient (6643e5d7...)"
```

## Message Routing Details

### Only One Active Project at a Time

```
chat:route:{chat_id} stores only one record → messages enter only one stream

Advantage: Simple, no confusion
Disadvantage: Old project messages stop being consumed after switching
```

### Unprocessed Messages from Old Project

```
Before switching:
  amingClaw stream has 3 unconsumed messages

After switching:
  These 3 messages remain in the stream
  telegram-handler-amingclaw detects route mismatch → does not consume
  Messages are not lost (stream preserved), but not processed

When switching back to amingClaw:
  telegram-handler-amingclaw detects route match → resumes consumption → processes backlog
```

## Context Isolation

```
Auto-save/load on switch:

Gateway.handle_bind():
  1. old_route = get_route(chat_id)
  2. if old_route:
       # Save old project context
       POST /api/context/{old_project}/save
  3. bind_route(chat_id, new_token, new_project)
  4. # Load new project context
     context = GET /api/context/{new_project}/load
  5. Reply: includes new project status + context summary
```

## Scheduled Task Project Awareness

### One Task Per Project

```
telegram-handler-amingclaw:     bound to amingClaw
telegram-handler-toolboxclient: bound to toolboxClient

On each trigger:
  1. Check routing table → which project is the current chat bound to
  2. Not my project → exit immediately (< 1 second)
  3. Is my project → consume messages → process
```

### Context During Task Processing

```
Task starts:
  1. GET /api/context/{my_project}/load → get last working state
  2. POST /api/context/{my_project}/assemble → get project memory
  3. Combine context when processing messages to understand user intent
  4. Save updated context after replying
```

## Cross-Project Queries

Users can query other projects from the current project's conversation:

```
User (currently bound to amingClaw): "How many nodes does toolboxClient have?"
    │
    ▼
Task identifies cross-project query:
  → GET /api/wf/toolboxClient/summary (no token needed, summary is public)
  → Reply: "toolboxClient: 89 nodes, 89 qa_pass"
  → Does not switch project binding
```

## Gateway Modifications

### Auto-save/load context on bind

```python
# gateway.py handle_bind modification
def handle_bind_with_context(chat_id, token, project_id):
    # 1. Save old context
    old_route = get_route(chat_id)
    if old_route and old_route.get("project_id"):
        old_pid = old_route["project_id"]
        try:
            requests.post(f"{GOVERNANCE_URL}/api/context/{old_pid}/save",
                headers={"X-Gov-Token": old_route.get("token", "")},
                json={"context": {"saved_reason": "project_switch"}},
                timeout=3)
        except: pass

    # 2. Bind new project
    bind_route(chat_id, token, project_id)

    # 3. Load new context
    context = None
    try:
        resp = requests.get(f"{GOVERNANCE_URL}/api/context/{project_id}/load",
            headers={"X-Gov-Token": token}, timeout=3)
        context = resp.json().get("context")
    except: pass

    # 4. Get project status
    summary = gov_api("GET", f"/api/wf/{project_id}/summary")

    return context, summary
```

### Display project status on menu switch

```python
# Each project button displays:
#   project_name (first 4 chars of token_hash) — N nodes M% passed
def build_project_button(route):
    pid = route.get("project_id", "?")
    summary = gov_api("GET", f"/api/wf/{pid}/summary")
    total = summary.get("total_nodes", 0)
    passed = summary.get("by_status", {}).get("qa_pass", 0)
    pct = int(passed / total * 100) if total else 0
    return f"{pid} — {total} nodes {pct}% passed"
```

## Implementation Priority

| Step | Content | Complexity |
|------|---------|------------|
| 1 | Auto-save/load context on Gateway bind | Low |
| 2 | /menu displays project status + auto-save on switch | Low |
| 3 | Check route + load context on Task start | Already exists |
| 4 | Cross-project queries | Medium |

## Changelog
- 2026-03-26: Old Telegram bot system fully removed (bot_commands, coordinator, executor, and 20 other modules), unified on governance API
