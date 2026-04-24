# Handoff — 2026-04-24 Sequence Z (partial) + B48/F2/Lockdown infrastructure fixes

**Audience**: next session (fresh Claude, no prior context)
**Author**: observer-z5866 session 67351297
**State as of**: 2026-04-24T09:55Z (session context ~95% full)
**Repo HEAD**: `4a12c29` (B48-sequel hotfix)
**chain_version (DB)**: `e57e7ba` (stuck — deploy hasn't completed since lockdown)
**Process state**: governance PID 41152 + SM PID 49924 (worker respawned on first task claim)

---

## TL;DR

Long session that landed SIX observer-hotfixes fixing the complete supervisor/deploy/governance infrastructure. Sequence Z (user's 4 concerns — graph-delta, backlog enforcement, reconcile, queue service) is designed and backlog-filed but blocked on a **persistent intermittent Claude CLI subprocess hang** that bites every chain at least once. Read sections 1–3 before resuming.

---

## 1. What landed this session (commits, in merge order)

| Commit | Author | Summary |
|---|---|---|
| `ba791f0` | observer-hotfix | **B48 Fix A**: SM RotatingFileHandler for log visibility (`shared-volume/codex-tasks/logs/service-manager-aming-claw.log`). Before this, every SM log was discarded because `-WindowStyle Hidden` + no FileHandler. |
| `1bb9f35` | observer-hotfix | **B48 Fix B**: Three changes in `agent/service_manager.py`: (a) `sys.path.insert(0, _PROJECT_ROOT)` at module top; (b) `_sidecar_runner` does NOT set `_running=False` on crash; (c) `_monitor_loop` treats `_sidecar_crashed` as warning-only. Fixes the "SM alive, worker dead, no respawn" symptom. |
| `b28d982` | auto-chain | B48 verification smoke chain (trivial docs-only). Proved B48 fix works: 24min end-to-end, 6 auto-recovered worker deaths, 0 observer SM restarts. |
| `2763aac` | observer-hotfix | **F2 fix**: `agent/deploy_chain.py` `restart_local_governance` adds `env={**os.environ, "PYTHONPATH": str(repo_root)}` to Popen. Also fixed `agent/governance/redeploy_handler.py` which was setting PYTHONPATH to `repo_root/agent` instead of `repo_root` (broke `python -m agent.governance.server`). |
| `e57e7ba` | observer-hotfix | **Version-update lockdown**: tightened allowed `updated_by` whitelist to `(auto-chain, init, manager-redeploy, redeploy-orchestrator)`. Removed `merge-service` (observer escape hatch) + `register` (dead). Non-auto-chain requires `VERSION_UPDATE_TOKEN`. `init` only at bootstrap. |
| `59c676f` | auto-chain | **Z1 Option A**: `_persist_connection(project_id)` helper with `busy_timeout=60000` in `chain_context.py`. Unblocks `pm.prd.published` persistence for graph-delta pipeline. |
| `4a12c29` | observer-hotfix | **B48-sequel**: Two fixes: (a) `server.py` step 3b `TASK_NOT_SUCCEEDED` → `TASK_TERMINALLY_FAILED` so auto-chain's in-flight version-update (task still `claimed` at call time) is accepted; (b) `deploy_chain.run_deploy` skips legacy `restart_executor()` signal write (caused SELFKILL loop — worker kills itself mid-deploy). |

### Verification evidence
- B48 smoke chain `b28d982` merge succeeded in 24min with 6 worker auto-recoveries
- Version-update lockdown 5/5 rejection tests passed (see §2.2 for attempt patterns)
- Chain events persist correctly for `task.completed` / `task.created`, but `pm.prd.published` still 0 until Z1 code is actually loaded by worker + first post-Z1 chain completes

---

## 2. Remaining blockers

### 2.1 Z0 verification chain is STUCK

Task `task-1777024212-52fff4` (PM) kicked 09:50:12Z. Currently `claimed`, never executed.
This is the same Claude CLI hang pattern that bit Z1's PM and Z1's Dev.

**Recovery SOP** (known to work, does not require session context):
```powershell
# 1. Find the stuck worker PID from the SM log
Get-Content shared-volume\codex-tasks\logs\service-manager-aming-claw.log -Tail 10

# 2. Find the hung claude.exe and/or executor_worker Python child
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Format-List ProcessId, CreationDate, CommandLine

# 3. Kill the worker (SM will respawn with fresh Python process = new import cache)
Stop-Process -Id <worker_pid> -Force

# 4. Wait 30s — SM monitor respawns, _recover_stuck_tasks marks the stuck task
#    as failed, auto-chain creates a retry. In ~70% of cases, retry succeeds cleanly.
```

Observed pattern: 2-3 kill-retries needed per chain. Total babysitting per chain ≈ 30-120min.

### 2.2 `chain_version` stuck at `e57e7ba`

HEAD = `4a12c29`. Governance's `chain_version` DB field = `e57e7ba`. This means:

- `/api/version-check` returns `ok=False`
- New chains get a **WARNING** (not error) from `_gate_version_check` (D3 downgrade from MEMORY)
- But `_finalize_version_sync` cannot advance chain_version until a chain's merge stage completes successfully AND that merge's version-update POST is accepted

With the Z0 commit 4a12c29 now live in governance, the next chain that completes merge → deploy should ADVANCE chain_version naturally. The ONLY reason it hasn't yet is Z0 verification chain is stuck on Claude hang.

### 2.3 Claude CLI intermittent hang (now the #1 open bug)

- Symptom: `ai-lifecycle-ai-*.txt` log ends at `Popen started: pid=XXX` with no `Popen done`
- Frequency: 1-2 hits per chain of 6 stages
- Platform: Windows + embedded Python + Claude CLI
- Hypothesis 1: Windows pipe deadlock on large stdin (`communicate(input=large_prompt)`)
- Hypothesis 2: `CLAUDE_CODE_OAUTH_TOKEN` stale → Claude CLI blocks on 401 retry forever
- Hypothesis 3: Subprocess-inheritance of some stale state from MCP-or-IDE-parent context

No hotfix has been attempted. Recommended next session action: Z-1 investigation (pre-Z0).

---

## 3. Sequence Z state (user's 4 concerns)

User wants ALL concerns executed in priority order:

| Zn | Concern | Backlog row | State |
|---|---|---|---|
| Z1 | DB lock fix (Option A) — enables pm.prd.published persistence | `OPT-BACKLOG-PERSIST-EVENT-CONN-TIMEOUT` | **CODE MERGED** at `59c676f`; not yet verified via chain_events because Z0 verification is stuck |
| Z2 | Verify `pm.prd.published` + `graph.delta.*` events fire | `OPT-BACKLOG-GRAPH-DELTA-PIPELINE-VERIFICATION` | Blocked on Z0 verification completing |
| Z3 | Task-source enforcement (#4) | `OPT-BACKLOG-TASK-SOURCE-ENFORCEMENT` | Not started |
| Z4 | Graph auto-commit verification (#2 close-out) | — (observation) | Not started |
| Z5 | Reconcile 94 dropped proposed_nodes (#1) | `OPT-BACKLOG-GRAPH-RECONCILE-APR15-ONWARDS` | Not started |
| Z6 | Write queue service (#3) | `OPT-BACKLOG-WRITE-QUEUE-SERVICE` | Not started (multi-chain sprint) |

Design doc + priority order: `docs/dev/sequence-z-master-plan.md`.

---

## 4. Memory writes for this session

### Observer project memory (`.claude/projects/.../memory/`)
- `project_b48_sm_sidecar_import.md` — full B48 postmortem
- `MEMORY.md` — index updated with B48 row

### Chain memory backend (`/api/mem/aming-claw/write`)
- `mem-...-92708a89` (kind=pitfall, module=service_manager) — B48 detection markers for future AI agents
- `mem-...-8def4938` (kind=decision, module=governance) — observer-hotfix-with-verification-chain pattern

### Design docs written
- `docs/dev/b48-investigation-and-fix-proposal.md` — B48 root cause + Fix A + Fix B design (approved + landed)
- `docs/dev/option-a-persist-event-timeout-proposal.md` — Z1 Option A design (approved + landed as `59c676f`)
- `docs/dev/sequence-z-master-plan.md` — Z1-Z6 master plan (in progress)
- `docs/dev/handoff-2026-04-24-sequence-z.md` — this file

---

## 5. Resume protocol for next session

### Step 1 — Check Z0 verification chain state
```bash
curl -s http://localhost:40000/api/task/aming-claw/list?limit=3
curl -s http://localhost:40000/api/version-check/aming-claw
```

Three possible states:
1. **Z0 completed** (deploy succeeded, chain_version advanced to some new hash): GREAT. Skip to Step 4.
2. **Z0 still stuck** at PM `claimed`: apply §2.1 recovery SOP (kill stuck worker, let auto-chain retry). Repeat 1-3× until chain completes.
3. **Z0 failed** (deploy SELFKILL'd despite Z0 fix): investigate — Z0 fix may be insufficient, check `deploy_chain.py:744` actually shipped via `cat` to confirm code loaded.

### Step 2 — Verify Z1 + Z0 live
```bash
# chain_events should have pm.prd.published rows now
python -c "
import sqlite3
c = sqlite3.connect('file:shared-volume/codex-tasks/state/governance/aming-claw/governance.db?mode=ro', uri=True)
cur = c.cursor()
cur.execute(\"SELECT event_type, COUNT(*) FROM chain_events WHERE ts > '2026-04-24T05:00:00Z' GROUP BY event_type\")
for r in cur.fetchall(): print(r)
"
```

Expected: at least 1 `pm.prd.published` row if Z1 worked.

### Step 3 — Address Claude CLI hang (Z-1)
If hang persists, file `OPT-BACKLOG-CLAUDE-CLI-HANG-DIAGNOSIS` (P0) and investigate:
- Capture hung process stack via py-spy or procdump
- Try Popen with `stdin=file_handle` instead of string (avoid pipe deadlock)
- Try stripping `CLAUDE_CODE_OAUTH_TOKEN` from SM env (not just worker env)
- Consider subprocess watchdog timer (force kill if no stdout in 60s)

### Step 4 — Resume Sequence Z at Z2
```bash
# Post Z2 verification task — trivial observation chain
curl -s -X POST http://localhost:40000/api/task/aming-claw/create \
  -H "Content-Type: application/json" \
  -d '{"type":"pm","priority":1,"prompt":"Z2 verification smoke — docs-only","metadata":{"bug_id":"OPT-BACKLOG-GRAPH-DELTA-PIPELINE-VERIFICATION"}}'
```

Then Z3 → Z4 → Z5 → Z6 per sequence-z-master-plan.md.

---

## 6. Key SOPs (collected from this session)

### Observer-hotfix pattern (when chain is broken by the bug being fixed)
1. Edit code on main branch directly (`[observer-hotfix <BUG-ID>]` commit prefix)
2. Manual 5W self-review in commit message
3. **Cannot** manually `/api/version-update` anymore (lockdown — see e57e7ba)
4. Restart affected service (governance or SM) to pick up new code
5. Post verification chain (trivial scope, observer hands-off)
6. Verify code paths activated in a live chain
7. File postmortem backlog row `OPT-BACKLOG-<topic>` after verification

### Version-update allowed updated_by (after lockdown)
- `auto-chain` + `chain_stage=merge` + task_id (either `claimed` or `succeeded`, NOT failed/cancelled/timed_out/design_mismatch)
- `init` — only allowed at first-time bootstrap (rejected if chain_version already set)
- `manager-redeploy` / `redeploy-orchestrator` — require `VERSION_UPDATE_TOKEN` env var server-side

Observer DIRECT version-update is BLOCKED. If you truly need to override:
1. Set `VERSION_UPDATE_TOKEN` env var on governance startup
2. Pass `X-Internal-Token` header matching the token
3. Use `manager-redeploy` or `redeploy-orchestrator` updated_by

Or alternatively — just run a chain that completes deploy. That's the design intent.

### Dead manager_signal.json restart path
`run_deploy` no longer writes `shared-volume/codex-tasks/state/manager_signal.json`. The `[redeploy]` path (`_post_manager_redeploy_executor`) handles executor reload without SELFKILL. If you see `manager_signal.json` being written, that's a regression.

---

## 7. Open backlog rows

Filed this session:
- `OPT-BACKLOG-PERSIST-EVENT-CONN-TIMEOUT` (P0) — Z1, CODE MERGED
- `OPT-BACKLOG-GRAPH-DELTA-PIPELINE-VERIFICATION` (P0) — Z2
- `OPT-BACKLOG-TASK-SOURCE-ENFORCEMENT` (P1) — Z3
- `OPT-BACKLOG-GRAPH-RECONCILE-APR15-ONWARDS` (P1) — Z5
- `OPT-BACKLOG-WRITE-QUEUE-SERVICE` (P1) — Z6

To be filed after Z0 verification confirms fix:
- `OPT-BACKLOG-SM-LOG-VISIBILITY` (postmortem for ba791f0)
- `OPT-BACKLOG-SM-SIDECAR-IMPORT-FIX` (postmortem for 1bb9f35)
- `OPT-BACKLOG-F2-GOVERNANCE-RESTART-PYTHONPATH` (postmortem for 2763aac)
- `OPT-BACKLOG-VERSION-UPDATE-LOCKDOWN` (postmortem for e57e7ba)
- `OPT-BACKLOG-VERSION-UPDATE-INFLIGHT-CHECK-FIX` (postmortem for 4a12c29 part a)
- `OPT-BACKLOG-DEPLOY-SELFKILL-LEGACY-SKIP` (postmortem for 4a12c29 part b)
- `OPT-BACKLOG-CLAUDE-CLI-HANG-DIAGNOSIS` (NEW, P0 — see §2.3)
- `OPT-BACKLOG-WORKER-MID-TASK-CRASH` (P1 — now observable via SM log; separate from B48)

---

## 8. File changes by file

For reference when navigating the session's codebase changes:

| File | Commits that touched it | Nature |
|---|---|---|
| `agent/service_manager.py` | `ba791f0`, `1bb9f35` | Logging visibility + sys.path fix + defensive sidecar |
| `agent/deploy_chain.py` | `2763aac`, `4a12c29` | PYTHONPATH for governance restart + SELFKILL skip |
| `agent/governance/redeploy_handler.py` | `2763aac` | PYTHONPATH correction |
| `agent/governance/server.py` | `e57e7ba`, `4a12c29` | Version-update lockdown + in-flight check relax |
| `agent/governance/chain_context.py` | `59c676f` | Z1 `_persist_connection` 60s timeout (via normal chain, not observer-hotfix) |
| `agent/tests/test_persist_event_timeout_bump.py` | `59c676f` | New test file for Z1 |
| `scripts/observer-watch-chain.py` | `b28d982` | New observer helper for monitoring chain progression |

---

## 9. Session lessons learned

- **Observer-hotfix IS the right tool** for infrastructure meta-circular bugs. Used 6× this session. Saved ~15h vs walking broken chains.
- **SM log visibility (Fix A) is the single highest-ROI change** — revealed B48 root cause in 30 seconds after 2267 lines of silent log were suddenly visible.
- **Naming bugs by symptom is a trap**. B48 was named "executor silent-death" — actual cause was SM monitor_loop dying via sidecar ImportError crash-together. The worker deaths were separate, auto-recoverable.
- **Lockdown without fixing the underlying workaround is harmful**. My initial lockdown (`e57e7ba`) removed `merge-service` without fixing the `TASK_NOT_SUCCEEDED` check that merge-service was working around. Required B48-sequel (`4a12c29`) to fix both together.
- **Fix what you break same commit when it's meta-circular**. B48-sequel committed BOTH fixes (server.py + deploy_chain.py) because both were needed to unblock Z1 chain_version sync.

---

*End of handoff. Next session: resume at §5 Step 1.*
