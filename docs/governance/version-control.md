# Version Control & Version Gate

> **Canonical governance topic document** — Version gate, git sync, and chain_version lifecycle.
> Last updated: 2026-04-05 | Phase 2 Documentation Consolidation

## Overview

The version control system ensures governance state stays synchronized with the git repository. The `chain_version` field in the governance DB tracks which git commit the current auto-chain is based on, and the version gate validates this synchronization at each stage transition.

> **See also:** [reconcile-workflow.md](reconcile-workflow.md) §12 for how reconcile operations
> interact with chain_version lifecycle and version gate during spec updates.

## Core Concepts

### chain_version

The `chain_version` is a git commit SHA stored in the governance DB that represents the current baseline:

- Set during project initialization
- Updated after each successful merge via `version-update` API
- Compared against git HEAD at each auto-chain stage transition

### Version Gate

The version gate is a check that runs at every auto-chain stage transition:

```
chain_version (DB) == git HEAD (repo)?
  YES → proceed
  NO  → warning (downgraded from blocker since D3 fix)
```

**Current behavior (post-D3):** Version mismatch logs a warning but does **not** block auto-chain progression. This prevents false blocks when commits happen outside the chain.

### Dirty Files Check

The version gate also checks for uncommitted changes:

```
git status --porcelain → dirty_files list
  EMPTY → clean workspace
  NON-EMPTY → filter .claude/ paths (D5 fix)
    STILL NON-EMPTY → warning (post-D5: warning only, not blocker)
    EMPTY after filter → clean (D5 fix resolved false positive)
```

## Version Sync Lifecycle

### 1. Executor Sync Loop

The executor worker syncs git HEAD to the governance DB periodically:

```
Every 60 seconds:
  current_head = git rev-parse HEAD
  if current_head != last_synced_head:
    POST /api/version-sync/{project_id}
    last_synced_head = current_head
```

This ensures the governance service always knows the current git state.

### 2. chain_version Update (Merge Stage)

When a merge task completes:

```
1. Dev worktree changes cherry-picked/merged to main
2. New commit created on main branch
3. POST /api/version-update/{project_id}
   Body: {"version": "<new_head>", "updated_by": "auto-chain", "task_id": "<merge_task_id>"}
4. chain_version in DB updated to new HEAD
5. Chain marked complete
```

**Important:** `updated_by` must be `"auto-chain"` or `"merge-service"` with a real task_id. Never use `"init"` after bootstrap — this creates a false governance record.

### 3. VERSION File

The `VERSION` file in the repo root tracks the current version:

**Bootstrap paradox:** Committing changes to VERSION updates HEAD, so VERSION always lags 1 commit. Resolution: force DB sync after merge, don't amend the commit.

## API Endpoints

### Version Check

```bash
GET /api/version-check/{project_id}

Response:
{
  "ok": true,
  "chain_version": "abc1234",
  "head": "abc1234",
  "dirty": false,
  "dirty_files": []
}
```

### Version Update

```bash
POST /api/version-update/{project_id}
Content-Type: application/json

{
  "version": "def5678",
  "updated_by": "auto-chain",
  "task_id": "task-123"
}
```

### Version Sync

```bash
POST /api/version-sync/{project_id}
Content-Type: application/json

{
  "head": "def5678"
}
```

## Version-Update Lockdown (commit e57e7ba)

The `version-update` endpoint now enforces a lockdown on the `updated_by` field.
Prior to this change, any caller could pass arbitrary `updated_by` values (including
`"init"` or `"observer"`), which created false governance records and could
desynchronize chain_version with the actual merge state.

**What changed:** Commit `e57e7ba` added a restricted whitelist of allowed
`updated_by` values:

- `auto-chain` / `auto-chain:<task_id>` — standard merge-stage updates
- `merge-service` — legacy merge path
- `manual-recovery` — explicit human-authorized recovery

All other values are rejected with `403 permission_denied`. This lockdown ensures
that only governed code paths can mutate `chain_version`.

## In-Flight Check Relaxation (commit 4a12c29)

The version gate previously blocked stage transitions if any in-flight task existed
for the same project, even if that task belonged to a different chain or was a stale
lease from a crashed executor.

**What changed:** Commit `4a12c29` relaxed the in-flight check behavior:

- The in-flight task check now only considers tasks in the *same chain*
  (matching `root_task_id`) rather than all tasks for the project.
- Stale claimed tasks (lease age > 30 minutes) are excluded from the in-flight
  count, preventing crashed-executor leftovers from blocking new chains.
- The check emits a warning log instead of blocking when in-flight tasks are
  detected in a *different* chain, allowing parallel chains to proceed.

This change resolves false blocks that occurred when multiple chains ran
concurrently or when executor crashes left orphaned task claims.

## Known Issues and Fixes

### D3: Version Gate False Blocks

**Problem:** `SERVER_VERSION` comparison blocked auto-chain after every merge because HEAD advances but `chain_version` update is asynchronous.

**Fix:** Downgraded version gate from blocker to warning-only. Auto-chain proceeds even with version mismatch.

### D5: Dirty Workspace False Positive

**Problem:** Executor syncs `.claude/settings.local.json` every 60s, permanently populating `dirty_files`. This caused the version gate to always report dirty workspace, blocking all auto-chain dispatch.

**Fix:** Filter `.claude/` paths from dirty_files check. Remaining dirty files downgraded to warning-only.

### DB Lock After Version Update

**Problem:** `version-update` API call sometimes causes WAL lock on governance DB (~50% of merges).

**Fix (MF-001/MF-002):** Resolved via `independent_connection()` pattern — version-update and version-sync now use dedicated short-lived connections with `conn.commit()` before EventBus publish, preventing WAL lock leaks. See [governance-api.md](../api/governance-api.md#sqlite-independent-connection-pattern) for details.

**Previous workaround (no longer needed):** Restart governance service to clear WAL locks.

## Manual Version Recovery

When version gate is blocking and auto-recovery fails:

```bash
# 1. Check current state
curl http://localhost:40000/api/version-check/aming-claw

# 2. Force version sync
curl -X POST http://localhost:40000/api/version-sync/aming-claw \
  -H "Content-Type: application/json" \
  -d "{\"head\": \"$(git rev-parse HEAD)\"}"

# 3. Update chain_version
curl -X POST http://localhost:40000/api/version-update/aming-claw \
  -H "Content-Type: application/json" \
  -d "{\"version\": \"$(git rev-parse HEAD)\", \"updated_by\": \"manual-recovery\"}"

# 4. Verify
curl http://localhost:40000/api/version-check/aming-claw
# Expected: ok=true, dirty=false
```

## Implementation

The version gate is implemented in `agent/governance/auto_chain.py`:

- `_gate_version_check()` — Main gate function
- Returns `(passed: bool, reason: str)`
- Currently always returns `passed=True` with warning log on mismatch (D3 fix)

The version sync is implemented in `agent/governance/executor_worker.py`:

- `_sync_version()` — Periodic sync function (every 60s)
- `_version_update()` — Called after merge completion
