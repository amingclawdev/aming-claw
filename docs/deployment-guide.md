# Aming Claw Deployment Guide — Development to Production Switch

> **2026-03-26 Update:** The legacy Telegram bot system (coordinator.py, executor.py, backends.py and 20 other modules) has been completely removed. Deployment now uses governance + gateway + executor-gateway exclusively.

## 1. Service Architecture

```
Docker Containers (docker-compose.governance.yml)
├── nginx          :40000  Reverse proxy
├── governance     :40006  Rule engine
├── telegram-gw    :40010  Message gateway
├── dbservice      :40002  Memory service
└── redis          :40079  Cache/queue

Host Machine
└── executor-gateway :8090  Task execution (FastAPI)
```

## 2. Complete Deployment Process

### 2.1 First-Time Deployment

```bash
cd C:\Users\z5866\Documents\amingclaw\aming_claw

# 1. Start all Docker services
docker compose -f docker-compose.governance.yml up -d

# 2. Wait for all services to become healthy
docker compose -f docker-compose.governance.yml ps

# 3. Register dbservice domain pack (not persisted, required after every restart)
curl -s -X POST http://localhost:40002/knowledge/register-pack \
  -H "Content-Type: application/json" \
  -d '{"domain":"development","types":{"architecture":{"durability":"permanent","conflictPolicy":"replace","description":"Architecture decisions"},"pitfall":{"durability":"permanent","conflictPolicy":"append","description":"Known pitfalls"},"pattern":{"durability":"permanent","conflictPolicy":"replace","description":"Code patterns"},"workaround":{"durability":"durable","conflictPolicy":"replace","description":"Workarounds"},"session_summary":{"durability":"durable","conflictPolicy":"replace","description":"Session summaries"},"verify_decision":{"durability":"permanent","conflictPolicy":"append","description":"Verify decisions"}}}'

# 4. Initialize project (first time)
python init_project.py
# Enter project name and password → Obtain coordinator token

# 5. Import acceptance graph
curl -X POST http://localhost:40000/api/wf/{project_id}/import-graph \
  -H "X-Gov-Token: {token}" \
  -d '{"md_path":"/workspace/docs/aming-claw-acceptance-graph.md"}'

# 6. Start executor-gateway (host machine)
cd agent && python -m executor_gateway &
# executor-gateway listens on :8090, executor_api.py listens on :40100

# 7. Telegram binding
# Send to bot in Telegram: /bind {coordinator_token}
```

### 2.2 Code Update Deployment (Routine)

```bash
# Method A: Quick deploy (5-10s downtime, Agent auto-retries)
docker compose -f docker-compose.governance.yml up -d --build
docker compose -f docker-compose.governance.yml restart nginx

# Method B: Zero-downtime deploy (via script)
GOV_COORDINATOR_TOKEN=gov-xxx ./deploy-governance.sh

# Method C: Update a single service only
docker compose -f docker-compose.governance.yml up -d --build governance
docker compose -f docker-compose.governance.yml up -d --build telegram-gateway
```

### 2.3 Development Environment to Production Switch

```
Development Flow:
  1. Modify code
  2. Create acceptance node (if new feature)
  3. Import acceptance graph
  4. Run tests
  5. Run coverage-check
  6. verify-update (testing → t2_pass → qa_pass)
  7. verify_loop self-check
  8. Deploy

Deployment Checklist:
  □ verify_loop all green (7/7 pass)
  □ coverage-check pass
  □ All new nodes qa_pass
  □ Documentation updated (/api/docs)
  □ Memory written (dbservice)
  □ git commit
```

## Workspace Routing Configuration

> **Note (2026-03-26):** The legacy workspace_registry.py has been deleted. The following workspace routing documentation is for historical reference only; project routing is now managed uniformly by the governance API.

### project_id Normalization

All project_id variants are automatically normalized to kebab-case:

| Input | Normalized |
|------|--------|
| `amingClaw` | `aming-claw` |
| `aming_claw` | `aming-claw` |
| `toolBoxClient` | `tool-box-client` |

### Auto-Registration

On startup, the Executor automatically registers the current working directory and backfills `project_id` for existing entries (normalized from label).

### Manually Registering Additional Workspaces

If you need to manage multiple projects (e.g., toolBoxClient + amingClaw), register after Executor startup:

```bash
# View current registry
curl http://localhost:40100/workspaces

# Query the workspace for a specific project
curl "http://localhost:40100/workspaces/resolve?project_id=amingClaw"
```

Or via Python (**deprecated**, workspace_registry.py has been deleted):

```python
# ⚠ The following code is no longer available; workspace_registry.py was deleted on 2026-03-26
# Workspace management is now done via the governance API
# cd agent
# python -c "
# from pathlib import Path
# from workspace_registry import add_workspace
# add_workspace(Path('C:/Users/z5866/Documents/Toolbox/toolBoxClient'),
#               label='toolBoxClient', project_id='toolbox-client')
# "
```

### Routing Priority

Priority for routing tasks to workspaces:

1. `target_workspace_id` — Exact ID
2. `target_workspace` — Label match
3. **`project_id`** — Normalized project ID (recommended)
4. `@workspace:<label>` prefix
5. Default workspace fallback

### Redis Stream Audit

Each AI session's prompt + result is recorded in a Redis Stream:

```bash
# View the full audit for a specific session
redis-cli -p 40079 XRANGE ai:prompt:ai-dev-xxx - +
```

## 3. Restart Recovery Checklist

Steps to restore after a computer restart:

```bash
# 1. Start Docker services
cd C:\Users\z5866\Documents\amingclaw\aming_claw
docker compose -f docker-compose.governance.yml up -d

# 2. Wait for healthy status
docker compose -f docker-compose.governance.yml ps
# Confirm all services are healthy

# 3. Restart nginx (resolve upstream resolution issues)
docker compose -f docker-compose.governance.yml restart nginx

# 4. Register dbservice domain pack
curl -s -X POST http://localhost:40002/knowledge/register-pack \
  -H "Content-Type: application/json" \
  -d '{"domain":"development","types":{"architecture":{"durability":"permanent","conflictPolicy":"replace","description":"Architecture decisions"},"pitfall":{"durability":"permanent","conflictPolicy":"append","description":"Known pitfalls"},"pattern":{"durability":"permanent","conflictPolicy":"replace","description":"Code patterns"},"workaround":{"durability":"durable","conflictPolicy":"replace","description":"Workarounds"},"session_summary":{"durability":"durable","conflictPolicy":"replace","description":"Session summaries"},"verify_decision":{"durability":"permanent","conflictPolicy":"append","description":"Verify decisions"}}}'

# 5. Start executor-gateway (host machine)
cd agent && python -m executor_gateway &

# 6. Verify services
curl -s http://localhost:40000/api/health     # governance
curl -s http://localhost:40002/health          # dbservice
curl -s http://localhost:40000/nginx-health    # nginx
curl -s http://localhost:40100/health          # executor
```

## 4. Data Persistence

| Data | Location | After Restart |
|------|----------|---------------|
| Project/node state | Docker volume: governance-data (SQLite) | ✅ Retained |
| DAG graph | Docker volume: governance-data (graph.json) | ✅ Retained |
| Audit logs | Docker volume: governance-data (JSONL) | ✅ Retained |
| Memory data | Docker volume: memory-data (SQLite) | ✅ Retained |
| Redis cache | Docker volume: redis-data (AOF) | ✅ Retained |
| Coordinator token | Does not expire | ✅ Valid |
| **dbservice domain pack** | **In-memory** | **❌ Must re-register** |
| **executor-gateway process** | **Host machine** | **❌ Must start manually** |
| **Telegram chat route** | **Redis** | **✅ Retained (AOF)** |

## 5. Rollback

```bash
# Roll back to the previous version
docker tag aming_claw-governance:rollback aming_claw-governance:latest
docker compose -f docker-compose.governance.yml up -d governance
docker compose -f docker-compose.governance.yml restart nginx

# View rollback audit
curl -s http://localhost:40000/api/audit/amingClaw/log?limit=10
```

## 6. Monitoring

```bash
# Service health
curl http://localhost:40000/api/health
curl http://localhost:40000/nginx-health
curl http://localhost:40002/health

# Node status
curl http://localhost:40000/api/wf/amingClaw/summary -H "X-Gov-Token: {token}"

# Runtime
curl http://localhost:40000/api/runtime/amingClaw -H "X-Gov-Token: {token}"

# Audit logs
curl http://localhost:40000/api/audit/amingClaw/log?limit=20 -H "X-Gov-Token: {token}"
```

## 7. Executor API (:40100)

executor_api.py exposes an HTTP API when running on the host machine, supporting monitoring and intervention (note: no longer depends on the legacy executor.py or workspace_registry.py):

```
GET  /health              — Health check
GET  /status              — Runtime status (pending/processing/sessions)
GET  /sessions            — Active AI sessions
GET  /tasks               — Task list (supports ?project_id=&status= filtering)
GET  /trace/{trace_id}    — Full trace chain
GET  /traces              — Recent trace list (supports ?project_id=&limit=)
POST /coordinator/chat    — Direct conversation with Coordinator (bypasses Telegram)
POST /cleanup-orphans     — Clean up orphaned AI processes
POST /tasks/create        — Idempotent task file creation (called by Orchestrator)
```

### POST /tasks/create — Idempotent Task Creation

Used by the Orchestrator to create task files without duplicating existing work. Before writing a new task file, the endpoint checks whether a task with the same `task_id` already exists in `pending/`, `processing/`, or `results/`. If found, it returns the existing task rather than creating a duplicate.

```bash
curl -X POST http://localhost:40100/tasks/create \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "task-abc123",
    "project_id": "aming-claw",
    "role": "dev",
    "description": "Implement feature X",
    "target_files": ["agent/foo.py"]
  }'

# Response (new task):
# {"status": "created", "task_id": "task-abc123", "stage": "pending"}

# Response (already exists):
# {"status": "exists", "task_id": "task-abc123", "stage": "processing"}
```

## 8. Git Worktree Workflow

Dev AI works in an isolated worktree without affecting the main working directory:

```bash
# Executor auto-executes:
git worktree add -b dev/task-xxx .worktrees/dev-task-xxx
# Dev AI operates inside .worktrees/dev-task-xxx/
# After completion:
git worktree remove .worktrees/dev-task-xxx --force
# Branch retained: dev/task-xxx (for review)

# Merge to main:
git merge dev/task-xxx --no-ff
git branch -d dev/task-xxx
```

## 9. merge-and-deploy Flow

```bash
bash scripts/merge-and-deploy.sh dev/task-xxx

# Auto-executes:
# 1. git merge dev/task-xxx → main
# 2. pre-deploy-check.sh (nodes/coverage/docs/gatekeeper)
# 3. docker compose up -d --build
# 4. restart nginx
# 5. health check
# 6. sync governance data: prod → dev
# 7. restart executor
# 8. gateway notification
```

## 10. Known Issues

1. **dbservice domain pack is not persisted** — Lost after container restart; auto-registered in startup.sh.
2. **nginx healthcheck occasionally unhealthy** — Restart nginx to resolve.
3. **executor-gateway is a host machine process** — Not inside Docker; lifecycle must be managed manually.
4. **Observer and executor-gateway operate in parallel** — Isolated via git worktree.

## 11. Executor Safety & Reliability Mechanisms

### Orphan Cleanup Safety: `_EXECUTOR_SPAWNED_PIDS`

The Executor tracks every AI subprocess it spawns in an in-memory set `_EXECUTOR_SPAWNED_PIDS`. When `/cleanup-orphans` is called (or the built-in timeout sweep runs), **only PIDs present in this set are eligible for termination**. This prevents the cleanup logic from accidentally killing unrelated processes — including any user-owned Claude Code sessions running in the same terminal.

```
_EXECUTOR_SPAWNED_PIDS = {pid_1, pid_2, ...}   # populated on subprocess.Popen()
                                                 # removed on process exit
# cleanup-orphans only kills: process.pid IN _EXECUTOR_SPAWNED_PIDS
```

### Startup Reconcile: `_reconcile_stale_claimed()`

On every Executor startup, `_reconcile_stale_claimed()` is called before the main task loop begins. It scans the governance DB for tasks that are still in `claimed` state from a previous run (e.g., after a crash or forced restart) and resets them to `pending`, preventing those tasks from being permanently stuck.

```python
# Called once at startup, before processing loop:
_reconcile_stale_claimed()
# Effect: governance DB tasks in state=claimed → state=pending
```

### Branch Pollution Fix: Checkout `main` After Task Completion

After every task completes (success or failure), the Executor explicitly runs `git checkout main` inside the task's working directory. This prevents the repository from being left on a feature branch (`dev/task-xxx`) between tasks, which could pollute subsequent git operations or cause worktree conflicts.

```
Task finishes → git worktree remove .worktrees/dev-task-xxx --force
             → git checkout main   # always, regardless of outcome
```

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `AI_SESSION_TIMEOUT` | `600` (seconds) | Maximum wall-clock time allowed for a single AI session. After this, the session is killed and the task is retried or failed. |
| `EXECUTOR_API_URL` | `http://localhost:40100` | Base URL for the Executor HTTP API. Used by Orchestrator and other internal callers to reach the Executor. Override when running Executor on a non-default port. |

```bash
# Example: Extend timeout for large refactor tasks
export AI_SESSION_TIMEOUT=1200

# Example: Executor running on an alternate port
export EXECUTOR_API_URL=http://localhost:40200
```

## Changelog
- 2026-03-26: Legacy Telegram bot system completely removed (bot_commands, coordinator, executor and 20 other modules); now using governance API exclusively
