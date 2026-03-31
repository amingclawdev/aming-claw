# Host Executor Manager (2026-03-30)

## Why

The live workflow cannot rely on Docker Compose for the executor path because:

- Claude / Codex CLI credentials and local AI tooling live on the host
- the governance container can create tasks but cannot execute host AI sessions
- the previous compose setup had no persistent queue consumer

## Decision

Move the long-running executor management path to the host:

- `scripts/start-manager.ps1`
  - loads `.env`
  - defaults `GOVERNANCE_URL` to `http://localhost:40000`
  - defaults `CODEX_WORKSPACE` to the repo root
  - launches `python -m agent.service_manager`
- `agent.service_manager`
  - now has a real CLI entrypoint
  - supervises `python -m agent.executor_worker`
  - passes host `workspace` and nginx `governance-url` through to the worker
  - stays alive until interrupted

## Code Changes

- `agent/service_manager.py`
  - runtime defaults now prefer host/nginx routing
  - default executor command includes:
    - `--project`
    - `--url`
    - `--workspace`
  - added host-process `main()` loop
  - added lightweight `.env` loading
- `agent/mcp/server.py`
  - default governance URL now points to `http://localhost:40000`
  - executor autostart is now opt-in (`--autostart-executor` / `MCP_AUTOSTART_EXECUTOR=1`)
  - ad-hoc MCP sessions no longer spawn duplicate queue consumers by default
- `scripts/start-manager.ps1`
  - no longer shells into the legacy path
  - now starts `agent.service_manager` as the host-side long-running supervisor
  - `-Takeover` now also clears stray host `executor_worker` and `agent.mcp.server` processes
- `.env.example`
  - documents `GOVERNANCE_URL=http://localhost:40000`

## Verification

- `python3.13 -m pytest agent/tests/test_service_manager.py -q`
- `python3.13 -m py_compile agent/service_manager.py agent/mcp/server.py`
- `python -m agent.service_manager --status-only`

## Operational Use

From the repo root on the host:

```powershell
.\scripts\start-manager.ps1
```

Or directly:

```powershell
python -m agent.service_manager --project aming-claw --governance-url http://localhost:40000 --workspace C:\Users\z5866\Documents\amingclaw\aming_claw
```

## Remaining Gap

This establishes the host-side long-running execution plane, but it still needs a real long-lived host session/process to be started in production. Once that process is up, workflow task consumption no longer depends on ad-hoc `--once` replays.

## Implementation Result

After cleanup and host takeover on 2026-03-30, the live host process tree was reduced to:

- one `service_manager.py` process
- one managed `executor_worker.py` child
- no stray `agent.mcp.server` processes auto-spawning extra workers

This is the intended steady state for host-side queue consumption.
