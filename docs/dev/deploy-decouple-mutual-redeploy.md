# Deploy Decouple: Mutual Redeploy Design

**Bug ID:** OPT-BACKLOG-DEPLOY-SELFKILL (P0)
**Status:** In Progress (PR-1 of 3)

## Problem Statement

`run_deploy` calls `restart_executor()` before `restart_local_governance()`, causing
Windows `taskkill /T` to kill the newly-spawned governance child. The deploy task
orphans, and a respawn-claim loop runs forever until the circuit breaker trips.

## Solution: 3-PR Decouple Plan

Split governance redeploy into a standalone HTTP endpoint on the ServiceManager side,
replacing inline subprocess manipulation with HTTP-driven lifecycle management.

---

## Design Matrix

| Aspect | PR-1 (Observable-Only) | PR-2 (Wire Deploy) | PR-3 (Governance-Side + Cleanup) |
|--------|----------------------|--------------------|---------------------------------|
| **Scope** | SM-side HTTP endpoint | Wire run_deploy to call endpoint | Gov-side endpoints, remove legacy |
| **Files** | `agent/manager_http_server.py` (new), `agent/service_manager.py` (mod) | `agent/deploy_chain.py` (mod) | `agent/governance/server.py` (mod) |
| **Risk** | LOW — endpoint exists but unused | MEDIUM — changes live deploy path | MEDIUM — removes fallback paths |
| **Revertable** | Yes — no callers | Yes — revert to inline restart | Yes — restore legacy paths |

---

## PR-1: SM-Side Governance Redeploy Endpoint (This PR)

### Endpoint Contract

- **URL:** `POST /api/manager/redeploy/{target}`
- **Binding:** `127.0.0.1:40101` (localhost only)
- **Request body:** `{"chain_version": "<short git hash>"}`

### Mutual-Exclusion Guard (R2)

| Target | Response |
|--------|----------|
| `service_manager` | HTTP 400 — Cannot redeploy self (prevents self-destruction loop) |
| `governance` | Perform redeploy contract |
| anything else | HTTP 404 — Unknown target |

**Rationale:** If the ServiceManager redeployed itself, the HTTP server processing the
request would die mid-response, leaving the system in an undefined state with no
supervisor to restart anything.

### PYTHONPATH Fix (R3)

The 2026-04-22 `ModuleNotFoundError` for the `agent` module occurred because the
governance subprocess was spawned without the project root on `PYTHONPATH` and without
`cwd` set to the project root. PR-1 fixes this:

- `env["PYTHONPATH"]` includes project root directory
- `cwd` set to project root directory

### DB Write Semantics (R5)

- On **successful** governance spawn + health check: write `chain_version` to governance
  DB exactly once via `POST /api/version-update/{project_id}`
- On **failed** spawn or health check: do NOT write `chain_version`

### Sidecar Lifecycle (R4)

The HTTP server runs as a sidecar thread inside the ServiceManager process:

- Dedicated asyncio event loop in a daemon thread
- **Crash-together semantics:** if the sidecar crashes, `_sidecar_crashed` is set,
  and the main ServiceManager monitor loop stops on its next tick
- Started before the executor subprocess in `main()`

### New Dependency

- `aiohttp>=3.9` added to `agent/requirements.txt`

---

## PR-2: Wire run_deploy to Call Endpoint (Future)

### Scope

- Modify `agent/deploy_chain.py` `run_deploy()` to call
  `POST http://127.0.0.1:40101/api/manager/redeploy/governance`
  instead of inline `restart_local_governance()`
- Keep `restart_local_governance()` as fallback if HTTP call fails
- Add timeout and retry logic for the HTTP call

### NOT in PR-2

- Do not remove `restart_local_governance()`
- Do not add governance-side endpoints
- Do not change `/api/version-update` allowlist

---

## PR-3: Governance-Side Endpoints + Legacy Cleanup (Future)

### Scope

- Add governance-side graceful shutdown endpoint
- Add governance-side health endpoint enhancements
- Remove `restart_local_governance()` from `deploy_chain.py`
- Remove inline subprocess governance management
- Update `/api/version-update` allowlist to include `manager-redeploy`

### NOT in PR-3

- Do not change the HTTP sidecar binding or port
- Do not change the mutual-exclusion guard logic

---

## Testing Strategy

| PR | Test Coverage |
|----|--------------|
| PR-1 | Unit tests: 400 for self-redeploy, 404 for unknown, success writes version, failure skips version |
| PR-2 | Integration: run_deploy calls HTTP endpoint, fallback to legacy on HTTP failure |
| PR-3 | E2E: full redeploy cycle through HTTP, legacy paths removed |
