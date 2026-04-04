# Host Services Integration Guide

## Purpose

This document describes how the current `aming-claw` runtime is wired after the host-side migration.

It focuses on:

- host-side `governance`
- host-side `executor` supervision
- optional containerized dependencies that still remain
- how external sessions, observers, and MCP clients should connect

## Current Runtime Topology

The active local development/runtime shape is now:

```text
Observer / Codex / Claude session
        |
        v
http://localhost:40000
        |
        +--> host governance server
        |
        +--> host service_manager
                |
                +--> host executor_worker
                        |
                        +--> Claude / Codex CLI on host
        |
        +--> optional external clients (Telegram gateway, tools, scripts)

Supporting dependencies:
- dbservice: still allowed to remain in Docker for now
- redis: still allowed to remain in Docker for now
```

## What Is Already Host-Side

### 1. Governance

Active entrypoint:

- [start_governance.py](C:/Users/z5866/Documents/amingclaw/aming_claw/start_governance.py)
- [start-governance.ps1](C:/Users/z5866/Documents/amingclaw/aming_claw/scripts/start-governance.ps1)

Default runtime address:

- `http://localhost:40000`

Health check:

- `http://localhost:40000/api/health`

### 2. Executor Supervisor

Active entrypoint:

- [service_manager.py](C:/Users/z5866/Documents/amingclaw/aming_claw/agent/service_manager.py)
- [start-manager.ps1](C:/Users/z5866/Documents/amingclaw/aming_claw/scripts/start-manager.ps1)

Main responsibility:

- keep one managed `executor_worker` alive on the host
- restart it if needed
- prevent duplicate unmanaged workers from drifting around

### 3. Executor Worker

Managed file:

- [executor_worker.py](C:/Users/z5866/Documents/amingclaw/aming_claw/agent/executor_worker.py)

Main responsibility:

- claim queued governance tasks
- execute role-specific AI sessions on the host
- write results, logs, memory, and completion state back through governance

## What Is Not Fully Host-Side Yet

These can still remain in Docker for now:

- `dbservice`
- `redis`

The following Docker services are intentionally retired to avoid multi-source governance:

- `governance`
- `governance-dev`
- `nginx` as the governance front door

The workflow no longer supports Docker-hosted `governance` as a parallel development path. Host `http://localhost:40000` is the only supported governance control plane.

## Required Connection Points

### Governance URL

All host-side control-plane tools should use:

- `GOVERNANCE_URL=http://localhost:40000`

This includes:

- service manager
- executor worker
- observer scripts
- local tooling
- MCP integrations that need governance access

### Workspace

Host execution should point at the real repo workspace:

- `CODEX_WORKSPACE=C:\Users\z5866\Documents\amingclaw\aming_claw`

### Shared Volume

Used for logs, state, and workflow artifacts:

- `SHARED_VOLUME_PATH=<repo>\shared-volume`

## Startup Order

Recommended host startup order:

1. Ensure `.env` exists
2. Ensure optional dependencies are reachable
   - `dbservice`
   - `redis`
3. Start governance
4. Verify governance health
5. Start service manager
6. Verify managed worker is alive
7. Then run observer workflows / MCP sessions / external clients

## Recommended Commands

From repo root:

### Start Governance

```powershell
.\scripts\start-governance.ps1
```

Take over an existing instance:

```powershell
.\scripts\start-governance.ps1 -Takeover
```

### Start Host Executor Manager

```powershell
.\scripts\start-manager.ps1
```

Take over an existing manager and worker:

```powershell
.\scripts\start-manager.ps1 -Takeover
```

## Environment Defaults

The current host-side defaults are effectively:

```text
GOVERNANCE_URL=http://localhost:40000
DBSERVICE_URL=http://localhost:40002
REDIS_URL=redis://localhost:40079/0
MEMORY_BACKEND=docker
CODEX_WORKSPACE=C:\Users\z5866\Documents\amingclaw\aming_claw
```

Notes:

- `MEMORY_BACKEND=docker` here does not mean governance itself is still Docker-bound
- it means the memory stack may still depend on containerized helper services like `dbservice`

## Observer / External Session Access Rules

When operating as an observer or external coding session:

- use governance APIs rather than direct DB mutation
- treat governance runtime state as the source of truth
- do not bypass runtime node state through SQLite edits
- use `observer_hold`, `release`, `cancel`, `import-graph`, and constrained observer recovery APIs when needed
- do not use Docker `governance` or `governance-dev` containers as an alternate API source

## MCP Access

MCP can still be used, but it should not automatically own executor lifecycle by default.

Current rule:

- host `service_manager` owns the long-running executor lifecycle
- ad-hoc MCP sessions should not auto-spawn duplicate queue consumers

This is why:

- host manager is the steady-state execution plane
- MCP is a client/control surface, not the default queue supervisor

## Verification Checklist

Use this checklist after startup:

1. Governance health is `ok`
   - `GET http://localhost:40000/api/health`
2. One host governance process is running
3. One host `service_manager` process is running
4. One managed `executor_worker` child is running
5. No stray duplicate `agent.mcp.server` processes are auto-spawning workers
6. Observer can query:
   - task list
   - workflow summary
   - node summary
   - memory query
7. A queued test task can be claimed and processed

## Failure Modes To Watch

### 1. Governance healthy, but no worker consumption

Likely cause:

- service manager not started
- worker crashed
- duplicate stale process state

### 2. MCP session starts another worker unexpectedly

Likely cause:

- MCP autostart-executor was re-enabled

### 3. Governance reachable, but graph/node state inconsistent

Likely cause:

- runtime node state not restored
- graph imported but node state not synced

### 4. Memory queries work, but semantic behavior is incomplete

Likely cause:

- helper dependencies still rely on `dbservice`
- host governance is up, but supporting services are degraded

### 5. Docker governance container is running again

Likely cause:

- stale `docker compose up` habit
- old documentation still referenced `governance-dev`

Resolution:

- stop and remove Docker `governance` / `governance-dev`
- keep only `dbservice` and `redis` as optional containerized dependencies
- continue using `http://localhost:40000` as the single governance endpoint

## Suggested Future Direction

Current target shape:

- keep `governance` on host
- keep `service_manager` + `executor_worker` on host
- continue reducing Docker dependence
- eventually reassess whether `dbservice` and `redis` should also be host-native for development

## Related Docs

- [governance-host-migration-2026-03-30.md](C:/Users/z5866/Documents/amingclaw/aming_claw/docs/dev/governance-host-migration-2026-03-30.md)
- [host-executor-manager-2026-03-30.md](C:/Users/z5866/Documents/amingclaw/aming_claw/docs/dev/host-executor-manager-2026-03-30.md)
- [observer-rules.md](C:/Users/z5866/Documents/amingclaw/aming_claw/docs/observer-rules.md)
- [workflow-gap-decision-2026-03-30.md](C:/Users/z5866/Documents/amingclaw/aming_claw/docs/dev/workflow-gap-decision-2026-03-30.md)
