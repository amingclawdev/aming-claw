# Governance Host Migration (2026-03-30)

## Goal

Move `governance` from Docker-first iteration to host-first development so local code changes take effect immediately and the Observer can continue workflow repair without container rebuild drift.

## What Changed

- Added [pyproject.toml](/Users/z5866/Documents/amingclaw/aming_claw/pyproject.toml)
  - packages `agent*`
  - console scripts:
    - `aming-governance`
    - `aming-governance-host`
- Reworked [start_governance.py](/Users/z5866/Documents/amingclaw/aming_claw/start_governance.py)
  - loads `.env`
  - applies host defaults
  - defaults governance port to `40000`
  - defaults `DBSERVICE_URL` to `http://localhost:40002`
  - defaults `REDIS_URL` to `redis://localhost:40079/0`
- Added [start-governance.ps1](/Users/z5866/Documents/amingclaw/aming_claw/scripts/start-governance.ps1)
  - host governance launcher
  - takeover support
  - dependency bootstrap
  - duplicate process/port protection
- Added host migration tests
  - [test_governance_host_migration_round1.py](/Users/z5866/Documents/amingclaw/aming_claw/agent/tests/test_governance_host_migration_round1.py)

## Intended Operating Mode

- Host:
  - governance on `http://localhost:40000`
  - executor worker via host `service_manager`
- Optional Docker only for:
  - `redis`
  - `dbservice`
- `nginx` and Docker `governance` become compatibility layers, not the default dev path.

## Next Step

Cut over the runtime to host governance by default, then continue Observer/runtime checks directly against `http://localhost:40000`.

## Current Status

- Host governance is now the active runtime on `http://localhost:40000`
- Docker `governance` and `nginx` are no longer required for local workflow iteration
- Docker `dbservice` and `redis` remain temporary dependencies
- Host `service_manager` and `executor_worker` now point at host governance
- `scripts/start-governance.ps1 -Takeover` has been validated as the supported restart path
