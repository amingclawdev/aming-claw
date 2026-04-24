# Option A — `_persist_event` Dedicated Connection + 30s Busy Timeout

> **Status**: APPROVED 2026-04-23 by user (z5866) — awaiting SM-TIMEOUT-BUMP recovery before execution
> **Author**: observer-z5866 session 67351297
> **Date**: 2026-04-23
> **User review outcome**: Accepted with 4 modifications (§9 answers)
> **Supersedes**: partial scope of commit `1990edb` (`OPT-BACKLOG-PERSIST-EVENT-DB-LOCK-FIX`)
> **Replaces**: (none — hotfix layered on top of 1990edb; no revert)
> **Does NOT supersede**: the broader write-queue architecture (Option D, filed separately as P1)

---

## 1. Problem recap

After commit `1990edb` landed the 3-retry `_retry_on_db_lock` wrapper around
`_persist_event`'s INSERT, the SM-TIMEOUT-BUMP verification chain
(`task-1776983390-7e24eb`) still dropped all its persist events:

```
22:31:03  _persist_event: entry event_type=task.completed
22:31:14  DB lock retry 1/3 (persist_task.completed): waiting 0.10s
22:31:25  DB lock retry 2/3: waiting 0.30s
22:31:37  DB lock retry 3/3: waiting 0.90s
22:31:49  ERROR persist event failed (task.completed)

22:31:53  _persist_event: entry event_type=pm.prd.published
22:32:04  DB lock retry 1/3 (persist_pm.prd.published): waiting 0.10s
22:32:38  ERROR persist event failed (pm.prd.published)
```

Effective wait under current config:
- `busy_timeout` = **10000 ms** (10 s per attempt)
- retries = **3** (4 attempts total)
- Python sleeps: 0.1 + 0.3 + 0.9 = 1.3 s
- **Max wait ≈ 41.3 s**

**Measured lock-holder duration during PM commit**: 45–50 s
(auto_chain main txn holds write lock while `memory_backend.write` re-indexes
FTS5 + relation graph). 41 s retry window is too short.

### 1.1 Cascading failure scope

Database audit (read-only snapshot 2026-04-23 23:30Z):

| Event type | Persisted count (EVER) |
|---|---|
| `task.completed` | 1053 |
| `task.created` | 113 |
| `gate.blocked` | 3 |
| `graph.delta.test_probe` | 1 (diagnostic only) |
| **`pm.prd.published`** | **0** |
| **`graph.delta.proposed`** | **0** |
| **`graph.delta.inferred`** | **0** |
| **`graph.delta.validated`** | **0** |
| **`graph.delta.committed`** | **0** |

Why: `pm.prd.published` is published **inside** the auto_chain PM handler's
main txn, at the peak of the lock-holding window. Less loaded events
(dev/test/merge `task.completed`) slip through when the window narrows.
`graph.delta.*` is downstream of `pm.prd.published` — when the upstream drops,
`_emit_or_infer_graph_delta` queries chain_events, gets 0 rows for PM context,
early-returns, produces no graph.delta output. **Cascading failure**.

### 1.2 PM workload measurement

```sql
SELECT COUNT(*) FROM tasks
WHERE type='pm' AND status='succeeded'
  AND result_json LIKE '%proposed_nodes%'
  AND created_at > '2026-04-15T00:00:00Z';
```

**32 PM tasks produced 94 proposed_nodes in the last 8 days**, all dropped.
Graph sync gap = ~94 nodes.

---

## 2. Option A fix design

### 2.1 Scope

Single file: `agent/governance/chain_context.py`.
One new helper: `_persist_connection(project_id)`.
One call site: `_persist_event._do_insert`.

### 2.2 Code change

```python
# agent/governance/chain_context.py

def _persist_connection(project_id: str):
    """Get a SQLite connection tuned for event persistence.

    Uses a longer busy_timeout (60s) than the default get_connection
    (10s) because _persist_event writes during the main chain's
    commit window, which can hold the write lock 30-50s while
    memory_backend re-indexes FTS5.

    Combined with the _retry_on_db_lock wrapper (3 retries × 60s),
    this gives ~241s total wait — 4.8× the observed worst case (50s).
    """
    import sqlite3
    from .db import _project_db_path, _configure_connection
    db_path = _project_db_path(project_id)
    conn = sqlite3.connect(str(db_path), timeout=60)
    _configure_connection(conn, busy_timeout=60000)  # 60s, vs default 10s
    return conn


def _persist_event(self, root_task_id, task_id, event_type, payload, project_id):
    """Append event to chain_events table. Non-blocking, best-effort."""
    if self._recovering:
        return

    log.info("_persist_event: entry event_type=%s ...", event_type, ...)

    try:
        from .task_registry import _retry_on_db_lock

        def _do_insert():
            conn = _persist_connection(project_id)   # ← changed from get_connection
            try:
                conn.execute(
                    "INSERT INTO chain_events ...",
                    (root_task_id, task_id, event_type,
                     json.dumps(payload, ensure_ascii=False, default=str)[:20000],
                     _utc_iso()),
                )
                conn.commit()
            finally:
                conn.close()

        _retry_on_db_lock(_do_insert, _context=f"persist_{event_type}")
    except Exception:
        log.error("chain_context: persist event failed (%s/%s)", ...)
```

### 2.3 Code diff estimate

| File | Lines added | Lines removed |
|---|---|---|
| `agent/governance/chain_context.py` | ~15 (helper) + 1 (call swap) | 1 (old `get_connection` call) |
| `agent/tests/test_persist_event_db_lock_retry.py` | ~40 (new timeout-bump test) | 0 |
| **Total** | **~55** | **1** |

### 2.4 Why this doesn't affect other callers

- `_persist_connection` is module-private to `chain_context.py`
- Only used by `_persist_event` (the background subscriber)
- Main chain, API handlers, memory backend still use default `get_connection` with `busy_timeout=10000`
- Subscriber is best-effort; a 120s wait on the subscriber thread does not block any user-visible path

### 2.5 Test plan

New test file: `agent/tests/test_persist_event_timeout_bump.py`
- Verify `_persist_connection` sets `busy_timeout=60000` (not 10000)
- **25s lock hold** — confirm first attempt (busy_timeout internal wait) succeeds
- **50s lock hold** — confirm first attempt succeeds (50 < 60 busy_timeout);
  this is the observed worst-case window, must not need retries
- **75s lock hold** — confirm first retry succeeds (after 60s busy_timeout
  + 0.1s sleep + second 60s busy_timeout window catches the release)
- **260s lock hold** — confirm all retries exhaust and `log.error` fires;
  verifies the upper bound still behaves correctly

Existing `test_persist_event_db_lock_retry.py` must still pass.

---

## 3. Manual fix: NOT needed for deployment of Option A

### 3.1 Can Option A go through the normal auto-chain?

**Yes**, but with one caveat: the A-landing chain's own `pm.prd.published`
event will drop (because the fix isn't live yet). That's acceptable —
this chain is a BOOTSTRAP; it creates the fix, lands it, and future
chains benefit.

Chain state transitions work independently of `_persist_event` failures
(verified: `_persist_event` failures are swallowed via `log.error`, not
raised; `auto_chain._do_chain` uses its own in-memory state, not
`chain_events` replay).

### 3.2 Expected chain timeline

```
t=0s    Observer POST PM task (bug_id=OPT-BACKLOG-PERSIST-EVENT-CONN-TIMEOUT)
t=60s   PM completes, produces PRD with target_files=[chain_context.py, test_persist_event_timeout_bump.py]
t=120s  Dev completes: ~55 lines added
t=180s  Test completes (pytest runs new + existing)
t=240s  QA passes (1-line change, narrow scope)
t=260s  Gatekeeper passes
t=280s  Merge lands (Auto-merge commit)
t=340s  Deploy completes — governance restarts with new code
t=360s  Version-update → chain_version = new HEAD
        Fix is LIVE. Next chain's pm.prd.published will persist.
```

### 3.3 Potential snags (known footguns)

| Risk | Probability | Mitigation |
|---|---|---|
| Executor silent-death (B48) | Medium | Observer watches `task_list`, restarts `service_manager` if dev stalls >5min |
| Deploy SELFKILL (F2) | High | Use docs-only deploy re-dispatch pattern; keep `start-governance.ps1` handy |
| Gatekeeper graph-check fails (no proposed_node for the fix itself) | Low | A's PRD will include a `proposed_nodes` entry; gatekeeper won't block (doesn't enforce node existence post-A4a) |
| SM-TIMEOUT-BUMP dev (currently stalled) collides | **Already collided** | Must be cancelled/retried before starting A chain — see §5.1 |

---

## 4. Reconcile: YES needed, but separate task AFTER A lands

### 4.1 Scope

All PM tasks since **2026-04-15** (post-Phase-8 event-sourcing deployment)
that produced `proposed_nodes` in their result but have **no corresponding
node_state row** for the inferred node_id.

- **32 PM tasks** produced **94 proposed_nodes** in the window
- Current `node_state` has **246 nodes** total (mostly pre-April, pre-event-sourcing)
- Need to diff: which of the 94 are already in node_state (created by
  legacy `handle_node_create` API path) vs. genuinely missing

### 4.2 Reconcile tool design

New observer script: `scripts/reconcile-dropped-nodes.py`

```
Inputs:
  --since YYYY-MM-DD (default 2026-04-15)
  --dry-run (default True)
  --project-id aming-claw

Logic:
  1. Query tasks WHERE type='pm' AND status='succeeded'
     AND result_json LIKE '%proposed_nodes%' AND created_at > $since
  2. For each PM task:
     a. Parse result_json.proposed_nodes[]
     b. For each proposed node:
        - Compute deterministic node_id:
          L{parent_layer}.{next_seq}  (same algo as gatekeeper A6)
        - Check if node_id already in node_state
        - If missing:
          - POST /api/wf/{pid}/node-create
          - Body: { parent_layer, node_id, title, deps, primary, description }
          - Observer token
        - Emit evidence log
  3. Emit summary report: N created, M skipped (existed), K failed

Output:
  logs/reconcile-dropped-nodes-2026-04-23.json
  logs/reconcile-dropped-nodes-2026-04-23.md
```

### 4.3 Why reconcile AFTER Option A, not before

- **If before**: the reconcile chain itself emits 94 `task.completed` events
  that will drop under current lock contention. We can't audit the
  reconcile itself.
- **If after**: the reconcile writes are straight `node-create` API calls,
  which are separate txns with their own commit windows. Fix makes future
  auditing reliable.

Also: Option A landing itself produces ~1 proposed_node (for the helper),
which we'd want the *new* pipeline to handle — testing the fix on its own
dogfood.

### 4.4 Existing backlog row

`OPT-BACKLOG-RECONCILE-A1-A8-NODES` (filed during Sequence A)
originally scoped for ~19 A1-A8 proposed_nodes. Needs **scope expansion**
to 94+ nodes (Apr 15 → now).

Proposed new row: `OPT-BACKLOG-GRAPH-RECONCILE-APR15-ONWARDS` (P1,
~10 ACs, depends on Option A merged).

---

## 5. Pre-flight: what to do with the stalled SM-TIMEOUT-BUMP chain

### 5.1 Current state

```
task-1776983558-9e1627  dev  claimed  2026-04-23T22:33:23
  lease_expires_at: 22:38:23
  assigned_to: executor-44928
  now: 23:30+ (>55min since lease expired)
```

Classic B48 silent-death. Observer must:

1. `Get-CimInstance Win32_Process -Filter "ProcessId=44928"` → confirm dead
2. `Stop-Process` any stale executor_worker processes
3. Re-run `scripts/start-manager.ps1 -Takeover` (ignore 20s false-alarm per
   `OPT-BACKLOG-SM-SCRIPT-TIMEOUT-BUMP` which is the very fix this chain
   was supposed to land — bootstrap loop acknowledged)
4. Task should auto-recover on next executor startup (lease-expired →
   re-claim eligibility)

### 5.2 Collision avoidance with Option A

Two non-options:
- ❌ Cancel SM-TIMEOUT-BUMP and start A in isolation
- ❌ Let SM-TIMEOUT-BUMP finish first, then start A

Recommended:
- ✅ **Let SM-TIMEOUT-BUMP finish first**. It's 1-line scope, low-risk.
  Once merged + deployed, it also solves the 20s false-alarm issue that's
  been biting us all session. A chain goes smoother on top.

Timeline:
- SM-TIMEOUT-BUMP completes: t=0 to t+30min (after stall recovery)
- Option A chain: t+30min to t+90min
- Reconcile chain: t+90min to t+120min
- Total: ~2 hours observer attention

---

## 6. Verification plan (post-Option-A merge)

### 6.1 Immediate DB check (t+360s post-deploy)

```sql
SELECT event_type, COUNT(*)
FROM chain_events
WHERE ts > '<option-a-deploy-ts>'
GROUP BY event_type;
```

Expected: at least 1 `task.completed`, 1 `task.created` row for the
deploy task itself.

### 6.2 First fresh chain post-A (recommended: a tiny docs-only change)

Kick a trivial PM task, verify:
```sql
SELECT event_type, COUNT(*)
FROM chain_events
WHERE root_task_id = '<new-pm-id>'
  AND event_type IN ('pm.prd.published', 'graph.delta.proposed',
                     'graph.delta.inferred', 'graph.delta.committed');
```

Expected ≥ 1 for each `pm.prd.published`, `graph.delta.proposed`.
`graph.delta.committed` requires gatekeeper to reach `_commit_graph_delta`
which requires the full chain to pass.

### 6.3 Node creation verification

```sql
SELECT node_id, updated_at FROM node_state
WHERE updated_at > '<option-a-deploy-ts>'
ORDER BY updated_at DESC;
```

Expected: 1 new row corresponding to the test chain's proposed_node.

### 6.4 Rollback plan

If post-merge verification fails (still 0 `pm.prd.published`):
- `git revert <option-a-merge-commit>`
- Re-deploy via observer docs-only pattern
- File new diagnostic RFC; consider jumping to Option C or D

---

## 7. Non-goals (explicitly deferred)

- **Write-queue architecture** (Option D): filed as
  `OPT-BACKLOG-WRITE-QUEUE-SERVICE` (P1, separate sprint)
- **Caller-conn reuse refactor** (Option C): filed as
  `OPT-BACKLOG-PERSIST-EVENT-CONN-REUSE` (P2, when we have time to
  refactor EventBus payloads)
- **Graph-delta validation rules V1-V4** (Sequence A4b): blocked on A's
  fix landing; nodes won't be inferred reliably until then
- **OPT_BACKLOG_ENFORCE=strict flip**: remains deferred pending full
  graph-delta pipeline stability (this fix is a prerequisite)

---

## 8. Checklist for execution

Observer must confirm each before moving on:

- [ ] Option A design reviewed & approved by user
- [ ] SM-TIMEOUT-BUMP stall recovered and chain completed (or cancelled
      with evidence + requeued after A lands)
- [ ] Backlog row filed: `OPT-BACKLOG-PERSIST-EVENT-CONN-TIMEOUT` (P0)
- [ ] Old backlog row `OPT-BACKLOG-PERSIST-EVENT-DB-LOCK-FIX` marked
      "superseded_by: CONN-TIMEOUT" (not deleted — keep for audit)
- [ ] Option A chain kicked (PM → Dev → Test → QA → Gatekeeper → Merge → Deploy)
- [ ] Post-deploy DB verification per §6.1
- [ ] Fresh smoke chain per §6.2 to prove `pm.prd.published` persists
- [ ] Backlog row filed: `OPT-BACKLOG-GRAPH-RECONCILE-APR15-ONWARDS` (P1)
- [ ] Reconcile script drafted (dry-run first), reviewed, then executed
- [ ] MEMORY.md updated with:
      - A fix landing commit
      - Reconcile results (N created, M skipped, K failed)
      - B48 recurrence count during this cycle

---

## 9. Open questions — RESOLVED by user 2026-04-23

1. **Busy timeout value** → **60s** (upgraded from 30s proposal for 4.8× safety
   margin over observed 50s worst case; total wait ≈ 241s)

2. **Test case for 50s lock hold** → **ADDED** (§2.5 now includes 25s/50s/75s/260s
   cases; 50s is the observed worst case and must pass on first attempt without
   retries)

3. **Execution order** → **SM-TIMEOUT-BUMP first, then Option A** (original proposal
   retained; let SM-TIMEOUT-BUMP complete, then kick Option A chain)

4. **Reconcile safety** → **MUST dry-run first with human review** before live
   execution. Script `scripts/reconcile-dropped-nodes.py` defaults to `--dry-run
   True` and emits `logs/reconcile-dropped-nodes-YYYY-MM-DD.md` for inspection.
   Only after user confirms the diff does observer flip `--dry-run False` for
   the live pass.

---

## 10. Final execution checklist (post-approval)

**Phase 1: Pre-flight (est. 30min)**
- [ ] Recover SM-TIMEOUT-BUMP stalled dev task (§5.1 recovery SOP)
- [ ] Let SM-TIMEOUT-BUMP complete through deploy
- [ ] Version-update to confirm HEAD matches chain_version

**Phase 2: Option A landing (est. 60min)**
- [ ] File backlog row `OPT-BACKLOG-PERSIST-EVENT-CONN-TIMEOUT` (P0)
- [ ] Mark old row `OPT-BACKLOG-PERSIST-EVENT-DB-LOCK-FIX` as
      `superseded_by: CONN-TIMEOUT` (via `PATCH /api/backlog`)
- [ ] POST PM task with 60s busy_timeout scope + 4 test cases
- [ ] Chain through Dev → Test → QA → Gatekeeper → Merge → Deploy
- [ ] Observer watches executor_status every 2min; recovers B48 if hit

**Phase 3: Verification (est. 15min)**
- [ ] DB query: `task.completed` + `task.created` for deploy task persisted
- [ ] Kick docs-only smoke chain; verify `pm.prd.published` persists ≥1 row
- [ ] Verify `graph.delta.proposed` persists ≥1 row on smoke chain merge
- [ ] Verify new `node_state` row for smoke-chain proposed_node

**Phase 4: Reconcile dry-run (est. 30min)**
- [ ] File backlog row `OPT-BACKLOG-GRAPH-RECONCILE-APR15-ONWARDS` (P1)
- [ ] Author `scripts/reconcile-dropped-nodes.py` (default `--dry-run True`)
- [ ] Run dry-run against aming-claw; emit `logs/reconcile-dropped-nodes-YYYY-MM-DD.md`
- [ ] Human review of the markdown report:
      - count: N creates, M updates, K skipped (already exist)
      - spot-check 5 random entries for correctness
      - confirm no duplicates / no overly-aggressive inference
- [ ] **User approves the dry-run output** before live pass

**Phase 5: Reconcile live run (est. 10min)**
- [ ] Re-run with `--dry-run False`
- [ ] Emit `logs/reconcile-dropped-nodes-YYYY-MM-DD.live.md`
- [ ] Verify node_state count delta matches dry-run prediction
- [ ] MEMORY.md update with final tallies

---

*End of design doc. APPROVED for execution starting with Phase 1.*
