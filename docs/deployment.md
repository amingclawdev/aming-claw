# Aming Claw — Deployment Guide

> **Canonical deployment document** — Host-based deployment for development and production.
> Last updated: 2026-04-05 | Phase 2 Documentation Consolidation

## 1. Prerequisites

### System Requirements

- **Python 3.11+** with pip
- **Node.js 18+** (for MCP server)
- **Git** (with worktree support)
- **Docker** and **Docker Compose** (for optional services)
- **Claude CLI** (`claude` command available in PATH)
- **Redis** (via Docker or host install)

### Environment Variables

```bash
# Required
export GOV_PROJECT_ID="aming-claw"
export GOV_TOKEN="gov-<your-token>"

# Optional
export MEMORY_BACKEND="local"          # local | docker | cloud
export TELEGRAM_BOT_TOKEN="<token>"    # Required for Telegram gateway
export REDIS_URL="redis://localhost:40079"
```

## 2. Service Architecture

```
Host Machine (primary runtime)
├── Governance Service     :40000   ← Rule engine, API, auto-chain
├── Service Manager                 ← Executor supervisor
└── Executor Worker                 ← Task execution

Optional Docker Dependencies
├── Telegram Gateway       :40010   ← Message gateway
├── dbservice              :40002   ← Semantic memory (mem0)
└── Redis                  :40079   ← Pub/sub, cache
```

**All governance operations run on the host at `http://localhost:40000`.**

## 3. MCP Configuration (`.mcp.json`)

The MCP server auto-starts the governance service and executor worker when a Claude Code session opens.

```json
{
  "mcpServers": {
    "aming-claw": {
      "command": "python",
      "args": ["agent/mcp_server.py"],
      "env": {
        "GOV_PROJECT_ID": "aming-claw",
        "MEMORY_BACKEND": "local"
      }
    }
  }
}
```

Place this file in the project root. Claude Code reads it automatically on session start.

## 4. Governance Service Startup

### Option A: Via MCP (Recommended)

The MCP server starts the governance service automatically. No manual action needed — just open a Claude Code session.

### Option B: Manual Start

```bash
# Start governance service directly
python agent/governance/server.py --port 40000

# Or via PowerShell script (Windows)
.\scripts\start-governance.ps1
```

### Health Check

```bash
curl http://localhost:40000/api/health
# Expected: {"status": "ok", "version": "...", "pid": ...}
```

## 5. Executor Lifecycle

The executor worker is managed by the ServiceManager:

1. **Auto-start** — MCP server starts ServiceManager, which starts executor worker
2. **Monitor** — ServiceManager checks executor health every 10s
3. **Auto-restart** — If executor crashes, ServiceManager restarts it
4. **Circuit breaker** — 5 restarts within 300s triggers OPEN state (stops restart attempts)
5. **Crash recovery** — On startup, executor requeues orphaned claimed tasks

### Manual Executor Control

```bash
# Check executor status (via MCP tool)
# executor_status tool shows: running, tasks_claimed, uptime

# Scale executor (via MCP tool)
# executor_scale(0) — pause claiming (for observer mode)
# executor_scale(1) — resume claiming
```

### Session Exit

When the Claude Code session ends:
- MCP server shuts down
- ServiceManager stops executor worker
- Governance service continues running (if started separately)

## 6. Telegram Gateway

### Start via Docker Compose

```bash
docker compose -f docker-compose.governance.yml up -d telegram-gateway
```

### Configuration

The gateway connects to the host governance service:
- Gateway listens on `:40010`
- Governance URL: `http://host.docker.internal:40000` (Docker-to-host)
- Requires `TELEGRAM_BOT_TOKEN` environment variable

### Message Flow

```
Telegram → Gateway (:40010) → Governance (:40000) → Executor → Reply via Redis pub/sub
```

## 7. Redis Setup

### Via Docker Compose (Recommended)

```bash
docker compose -f docker-compose.governance.yml up -d redis
```

Redis runs on port 40079 and provides:
- Pub/sub for real-time event delivery
- Hot context store (24h TTL)
- Cache for governance data

### Connection Test

```bash
redis-cli -p 40079 ping
# Expected: PONG
```

## 8. Docker Compose for Optional Services

```bash
# Start all optional services
docker compose -f docker-compose.governance.yml up -d

# Start specific services
docker compose -f docker-compose.governance.yml up -d redis telegram-gateway dbservice

# Check service health
docker compose -f docker-compose.governance.yml ps

# View logs
docker compose -f docker-compose.governance.yml logs -f telegram-gateway
```

### Service Dependencies

| Service | Port | Depends On | Required? |
|---------|------|------------|-----------|
| Governance | 40000 | — | **Yes** (host) |
| Executor | — | Governance | **Yes** (host) |
| Redis | 40079 | — | Recommended |
| Telegram GW | 40010 | Governance, Redis | Optional |
| dbservice | 40002 | — | Optional (for semantic search) |

## 9. First-Time Setup

```bash
# 1. Clone and install dependencies
git clone <repo-url> && cd aming_claw
pip install -r requirements.txt

# 2. Start optional Docker dependencies
docker compose -f docker-compose.governance.yml up -d redis

# 3. Start governance service
python agent/governance/server.py --port 40000

# 4. Initialize project (first time only)
python init_project.py
# Enter project name and password → obtain governance token

# 5. Import acceptance graph
curl -X POST http://localhost:40000/api/wf/aming-claw/import-graph \
  -H "X-Gov-Token: gov-<token>" \
  -d '{"md_path": "docs/governance/acceptance-graph.md"}'

# 6. Register dbservice domain pack (if using dbservice)
curl -s -X POST http://localhost:40002/knowledge/register-pack \
  -H "Content-Type: application/json" \
  -d '{"domain": "development", "types": {...}}'

# 7. Configure .mcp.json and open Claude Code session
```

## 10. Workspace and Worktree Routing

The executor uses git worktrees for task isolation:

- **Main workspace** — coordinator and PM tasks execute here
- **Dev worktrees** — dev tasks get isolated `dev/task-{id}` worktrees
- **Merge** — merge tasks cherry-pick dev worktree commits to main

### Worktree Lifecycle

```
dev task created → worktree created at .worktrees/dev-task-{id}
dev task completes → worktree preserved for merge
merge task → cherry-pick to main → worktree cleaned up
```

## 11. Restart and Recovery

### After Host Reboot

```bash
# 1. Start Docker services
docker compose -f docker-compose.governance.yml up -d

# 2. Start governance
python agent/governance/server.py --port 40000

# 3. Open Claude Code session (auto-starts executor via MCP)
```

### After Crash

The executor automatically recovers on restart:
- Orphaned claimed tasks are requeued
- Circuit breaker resets after cooldown period
- Version gate re-syncs git HEAD to DB

### DB Lock Recovery

If governance DB becomes locked (known issue after version-update):
```bash
# Restart governance service
# This clears WAL locks and restores normal operation
```

## 12. Monitoring

### Health Endpoints

```bash
# Governance health
curl http://localhost:40000/api/health

# Version gate status
curl http://localhost:40000/api/version-check/aming-claw

# Task queue
curl http://localhost:40000/api/task/aming-claw/list

# Workflow summary
curl http://localhost:40000/api/wf/aming-claw/summary
```

### Executor Monitoring

Use MCP tools in Claude Code:
- `executor_status` — current state, tasks claimed, uptime
- `task_list` — all tasks with status
- `wf_summary` — node status counts

## 13. Known Issues

| Issue | Workaround |
|-------|------------|
| DB lock after version-update | Restart governance service |
| VERSION file lags 1 commit | Force DB sync after merge, don't amend |
| Gateway code changes need rebuild | `docker compose build telegram-gateway && up -d` |
| Dirty workspace gate false positive | `.claude/` paths auto-filtered (D5 fix) |

## 14. Data Persistence

| Data | Location | Backup Strategy |
|------|----------|----------------|
| Governance DB | `governance.db` (SQLite) | Git-tracked or periodic backup |
| Memory records | `governance.db` memories table | Included in DB backup |
| Task history | `governance.db` tasks table | Included in DB backup |
| Audit log | `governance.db` audit table | Included in DB backup |
| Redis data | Docker volume | Ephemeral (24h TTL), no backup needed |
| Git repo | `.git/` | Standard git remote push |
