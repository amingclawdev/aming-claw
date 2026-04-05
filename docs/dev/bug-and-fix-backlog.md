# Bug & Fix Backlog

> Maintained by: Observer
> Created: 2026-04-05
> Last updated: 2026-04-05

---

## Status Legend

| Tag | Meaning |
|-----|---------|
| `OPEN` | Confirmed, not yet fixed |
| `FIXED` | Fix committed |
| `WONTFIX` | By design or deferred indefinitely |
| `WORKAROUND` | Has temporary bypass, root fix pending |

---

## Active Bugs

### B1: auto_chain dispatch silently fails on dirty workspace [OPEN] [P0]

- **Symptom**: `task_complete` returns `auto_chain.dispatched: true` but no next-stage task is created.
- **Root cause**: `_gate_version_check()` in auto_chain.py L1416-1486 reads `dirty_files` from DB. Non-.claude dirty files cause gate to return False. The dispatch thread catches the block but does not surface it to the task_complete response.
- **Reproduced**: 3 times in 2026-04-05 session (PM->Dev, Dev->Test, QA->next all silently blocked by `server.py` being staged).
- **Impact**: Every chain breaks silently after any stage completes. Observer must manually create each next-stage task.
- **Related**: D5 fix (1ea497f) filtered `.claude/` paths but didn't fix the core issue of silent failure.
- **Fix proposal**:
  1. `task_complete` response should include `gate_blocked: true` + `gate_reason` when dispatch is attempted but blocked.
  2. auto_chain dispatch should log WARNING (not just debug) when gate blocks.
  3. Consider: gate should distinguish "dirty from pending feature work" vs "dirty from unknown manual edits".

### B2: skip_version_check has no access control or audit [OPEN] [P1]

- **Symptom**: Any task_create caller can set `metadata.skip_version_check: true` to bypass version gate entirely.
- **Root cause**: auto_chain.py L1432 does a bare `metadata.get("skip_version_check")` with no authorization, no audit log, no rate limit.
- **Impact**: Version gate can be circumvented trivially. Bypass leaves no trace in governance DB. Observer bypass during 2026-04-05 session was not audited.
- **Contrast**: `reconciliation_lane` bypass (L1440-1444) correctly has: allowed_lanes whitelist, observer_authorized check, audit_action logged. `skip_version_check` has none of these.
- **Fix proposal**:
  1. Require `operator_id` + `bypass_reason` when `skip_version_check` is set.
  2. Write audit row: `INSERT INTO governance_events (action='version_gate_bypass', operator, reason, task_id, timestamp)`.
  3. Add bypass counter per project; alert if >3 bypasses in 24h.

### B3: Version gate only enforces at auto_chain dispatch, not at task_create [OPEN] [P1]

- **Symptom**: Dirty workspace blocks auto_chain from creating next-stage tasks, but manual `task_create` + `skip_version_check` bypasses entirely.
- **Root cause**: `_gate_version_check()` is only called inside auto_chain dispatch logic. `task_create` endpoint has no gate check.
- **Impact**: Gate is a recommendation, not an enforcement. Any API caller can create tasks in a dirty state.
- **Fix proposal**:
  1. Add optional gate check in `task_create` handler: if dirty workspace and no bypass flag, return warning (not block, to avoid breaking emergency flows).
  2. Or: tag tasks created during dirty state with `created_during_dirty: true` so downstream stages know.

### B4: Executor CLI hangs on dev/qa tasks (progress stays 0%) [OPEN] [P2]

- **Symptom**: Executor claims dev/qa tasks, spawns Claude CLI subprocess, but progress never updates from 0%. Lease expires, task stays in `claimed` state.
- **Observed**: dev task (task-1775409544-abc75b) hung for >5min. qa task (task-1775409979-bd9fe6) hung for >4min. test task succeeded normally (~50s).
- **Root cause**: Unknown. Possible causes:
  - dev/qa role prompts are too large for CLI subprocess
  - CLI subprocess hits permission/tool restriction
  - Worktree isolation not working (dev task needs file write access)
- **Impact**: Executor cannot autonomously complete dev or qa stages. Observer must intervene.
- **Fix proposal**: Add executor CLI subprocess logging (stdout/stderr capture to file for post-mortem).

### B5: DB lock on task_complete (intermittent) [WORKAROUND] [P2]

- **Symptom**: `task_complete` returns `"error": "database is locked"`.
- **Reproduced**: 1 time during QA complete in 2026-04-05 session. Retry after 5s succeeded.
- **Root cause**: SQLite WAL lock contention between executor worker thread and MCP request handler.
- **Workaround**: Retry after short delay (3-5s).
- **Fix proposal**: Add retry-with-backoff in task_complete handler (up to 3 retries, 1s/2s/4s).

### B6: auto_chain dispatch reports dispatched:true when actually blocked [OPEN] [P0]

- **Symptom**: `task_complete` response contains `auto_chain: {dispatched: true}` but no task was created.
- **Root cause**: The dispatch function returns True before the gate check, or the gate check failure is caught and swallowed.
- **Impact**: Caller (executor or observer) has no way to know the chain broke. Must poll task_list to discover the gap.
- **Related**: B1 (same root cause, different symptom surface).
- **Fix proposal**: `auto_chain.dispatched` should be `false` when gate blocks, with `gate_reason` field.

---

## Fixed Bugs (Reference)

| ID | Description | Fix Commit | Date |
|----|-------------|------------|------|
| D1 | Executor stops claiming after initial batch | e9506c0 | 2026-03-31 |
| D2 | PM max_turns=10 instead of 60 | 5b09ad0 | 2026-03-31 |
| D3 | SERVER_VERSION blocks auto_chain after merge | 942b5de | 2026-03-31 |
| D4 | Duplicate retry task creation | 7d96c74 | 2026-03-31 |
| D5 | Dirty workspace gate blocks auto_chain (.claude/ paths) | 1ea497f | 2026-03-31 |
| D6 | Merge task fails without _branch/_worktree metadata | 20baea3 | 2026-03-31 |
| D7 | Coordinator duplicate reply | c931792 | 2026-03-31 |

---

## Fix Priority Matrix

```
            Impact
            High          Medium        Low
Effort  ┌─────────────┬─────────────┬───────────┐
Easy    │ B6 (return  │ B5 (retry)  │           │
        │ correct     │             │           │
        │ dispatched) │             │           │
        ├─────────────┼─────────────┼───────────┤
Medium  │ B1 (gate    │ B2 (audit)  │           │
        │ feedback)   │ B3 (create  │           │
        │             │ gate check) │           │
        ├─────────────┼─────────────┼───────────┤
Hard    │ B4 (executor│             │           │
        │ CLI debug)  │             │           │
        └─────────────┴─────────────┴───────────┘

Recommended fix order: B6 → B1 → B5 → B2 → B3 → B4
```

---

## Design Gaps (Not Bugs, But Missing Features)

### G1: No dirty-workspace root cause classification

- **Current**: Gate sees `dirty_files` -> block. No distinction between "pending feature commit" vs "unknown manual edit" vs "stale node refs".
- **Desired**: Gate should classify dirty cause and suggest action:
  - Untracked new files -> "commit pending feature"
  - Staged changes -> "complete pending commit"
  - Graph refs stale -> "run reconcile"
  - Mixed -> "commit first, then reconcile if needed"

### G2: No "pre-flight before task_create" advisory

- **Current**: Version gate only fires at auto_chain dispatch time. Manual task_create has no warning.
- **Desired**: `task_create` returns advisory `warnings: ["dirty workspace: server.py uncommitted"]` so caller can decide.

### G3: Chain context doesn't track bypass history

- **Current**: Chain events don't record which tasks used bypass flags.
- **Desired**: Chain audit trail includes all bypass events, enabling post-mortem "this chain had 3 gate bypasses".
