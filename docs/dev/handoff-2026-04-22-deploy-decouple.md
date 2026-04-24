# Handoff — 2026-04-22 Deploy Decouple + remaining MF-005 chains

**Audience**: next session (fresh Claude, no prior context)
**Author**: observer-z5866 session 713ef614
**State as of**: 2026-04-22T02:36Z
**Repo HEAD**: `05f45af` (chain #2 merge)
**chain_version (DB)**: `05f45af` (synced manually after deploy short-circuit)
**Process state**: governance PID 7048 + service_manager PID 33484 + executor PID 41856 (child of sm)

---

## TL;DR

Two of four MF-2026-04-21-005 follow-up chains landed clean. Two
remaining chains plus one new high-priority architectural chain are
queued. **Read this whole doc before kicking off anything** — the
deploy path has known footguns that bite hard.

---

## What landed today

| Chain | Backlog row | Merge commit | Notes |
|---|---|---|---|
| #1 | `OPT-BACKLOG-MERGE-D6-EXPLICIT-FLAG` | `94edd28` | First clean run end-to-end. Hit deploy SELFKILL once; recovered with docs-only re-dispatch. |
| #2 | `OPT-BACKLOG-DIRTY-FILTER-CACHE` | `05f45af` | Hit silent-drop in PM (cache file `.recent-tasks.json`); fixed by extending `_DIRTY_IGNORE`. Deploy SELFKILL hit twice + revealed second bug `restart_local_governance ModuleNotFoundError`. Recovered same docs-only workaround. |

Code in `agent/governance/auto_chain.py:35-42` now filters
`.recent-tasks.json`, `.governance-cache/`, `.observer-cache/`. Gitignore
covers them. Test in `agent/tests/test_dirty_ignore_filter.py`.

---

## Pending chains (recommended order)

### Chain #3 — `OPT-BACKLOG-QA-CLI-AUTH-TOKEN-STALE` (P0)

**Why first**: lowest deploy risk. Touches `agent/ai_lifecycle.py:270-277`
(env-strip tuple). Dev change is small; deploy will affect `executor`
service only (not governance). Expected SELFKILL on deploy unless
`changed_files=['docs/...']` workaround used (see footguns below).

**Bug**: executor's child Claude CLI subprocess inherits stale
`CLAUDE_CODE_OAUTH_TOKEN` from the original service_manager launch env.
When Claude rotates the OAuth token mid-session, child gets 401.

**Fix surface**: add `CLAUDE_CODE_OAUTH_TOKEN` to the env-strip tuple
in `_build_child_env`. Re-read from current env each time.

**Backlog AC update needed before kicking off**: the original AC was
written before chain #1 produced executor reclaim evidence. Worth
re-reading and adding "MUST verify token refresh works across an
executor reclaim cycle" to AC.

### Chain #4 — `OPT-BACKLOG-TASK-MUST-FROM-BACKLOG` (P1)

**Why second**: medium deploy risk. Touches `agent/governance/server.py`
(task creation validation) + scripts. Deploy will affect `governance`
which means the broken `restart_local_governance` will fire — same
SELFKILL family as chain #2.

**Bug** (it's a feature gap, not a bug): currently observer/operator
can `POST /api/task/.../create` directly with arbitrary types. This
bypasses backlog → enables the kind of ad-hoc task creation that
caused the chain #1 PM silent-drop incident.

**Fix surface**: server-side validation that rejects code-change task
types (pm, dev, test, qa, gatekeeper, merge, deploy) without a
`metadata.bug_id` referencing an OPEN backlog row. Two-phase rollout
via `OPT_BACKLOG_ENFORCE` env var: `warn` (log only) → `strict`
(reject).

### Chain #5 — `OPT-BACKLOG-DEPLOY-DECOUPLE-MUTUAL-REDEPLOY` (P0, NEW)

**Why last**: it touches the deploy path itself. Needs all the other
chains' validation passes as confidence-building first. **Read the
full backlog row** before starting — the design is non-trivial.

**Summary**: deploy task currently lives inside executor and tries to
restart executor + governance. Both fail badly. Fix is mutual-redeploy
contract: governance owns restart of executor/gateway/sm/coordinator;
service_manager owns restart of governance/dbservice. chain_version
update becomes internal side-effect of redeploy API; eliminates need
for `/api/version-update` HTTP endpoint (lockdown to bootstrap-only).

**Three-PR rollout** documented in `chain_trigger_json.rollout_phases`
on the backlog row. Each PR independently shippable.

**Supersedes**: `OPT-BACKLOG-DEPLOY-SELFKILL` (now status=MERGED).

---

## Footguns — read before touching deploy

### F1. Deploy SELFKILL is GUARANTEED if `changed_files` includes anything in `agent/governance/`

Until chain #5 lands, ANY chain whose dev modifies `agent/governance/*.py`
will hit deploy SELFKILL because:
1. `affected_services` will include `governance`
2. `restart_local_governance()` will spawn `python -m agent.governance.server`
3. That spawn dies immediately with `ModuleNotFoundError: No module named 'agent'`
4. Then `restart_executor()` signal kills executor mid-task
5. service_manager respawns executor 5x, each dies on "Cannot reach governance"
6. service_manager hits circuit breaker → giving up

**Workaround** (proven by chain #1 + chain #2):
1. After merge stage succeeds, watch deploy
2. If deploy gets `executor_crash_recovery`, immediately:
   - Cancel old deploy: `mcp__aming-claw__task_cancel`
   - Re-dispatch with `metadata.changed_files=["docs/dev/<some-real-doc>.md"]`
3. Re-dispatched deploy short-circuits: `affected_services=[]`, `note: "No services needed restarting"`
4. Manually sync `chain_version`:
   ```
   POST /api/version-update/aming-claw
   {"chain_version": "<short-hash>", "updated_by": "merge-service", "task_id": "<merge_task_id>"}
   ```
5. Verify `GET /api/version-check/aming-claw` returns `ok=true`

**Critical**: pass SHORT hash (7 chars from `git rev-parse --short HEAD`),
NOT full 40-char hash. See B35 in MEMORY.md.

### F2. SELFKILL recovery is fragile when service_manager is in respawn loop

If you hit SELFKILL and find `service-manager-executor-aming-claw.err.log`
showing 5x respawn cascade with "Cannot reach governance", the
service_manager has likely circuit-broken on executor respawns.
Recovery sequence:

1. **Stop service_manager FIRST** (otherwise it fights you):
   `Stop-Process -Id <sm_pid> -Force`
2. **Restart governance**: `.\scripts\start-governance.ps1`
3. **Wait for `/api/health` 200 OK**
4. **Cancel orphan deploy task** (the one stuck in `claimed`)
5. **Re-dispatch docs-only deploy** (template above)
6. **Restart service_manager**: `.\scripts\start-manager.ps1`
   (Note: script has 20s strict wait for executor, may print error but
   executor often comes up at 21s. Verify via `Get-CimInstance Win32_Process`.)
7. Confirm executor child of sm via process tree.

### F3. `/api/version-update` allowed `updated_by` values

Per `agent/governance/server.py:1981-2060`, accepts only:
- `auto-chain` (used by deploy `_finalize_version_sync`)
- `init` (bootstrap; **DO NOT use after bootstrap** — see MEMORY.md
  pitfalls)
- `register`
- `merge-service` (use this for manual post-deploy sync; pair with
  `task_id=<merge task id>`)

Will return HTTP 403 for any other value.

### F4. `_DIRTY_IGNORE` is now broader (post chain #2)

Current contents (`agent/governance/auto_chain.py:35`):
```python
_DIRTY_IGNORE = (
    ".claude/", ".claude\\",
    ".worktrees/", ".worktrees\\",
    "docs/dev/", "docs/dev\\",
    ".recent-tasks.json",
    ".governance-cache/", ".governance-cache\\",
    ".observer-cache/", ".observer-cache\\",
)
```

If you discover ANOTHER stray cache file blocking version-check (look
for `dirty_files=[...]` in `/api/version-check`), file a backlog row
extending the tuple. Don't just `git rm` and move on — the next
operator will trip on the same shape.

---

## Kickoff template (PM task creation)

```python
"""Kickoff template — adapt per chain."""
import json, urllib.request, urllib.error

BASE = "http://localhost:40000"
PID = "aming-claw"
BUG_ID = "OPT-BACKLOG-XXXXX"  # <-- fill in

prompt = (
    f"Implement {BUG_ID} per its backlog acceptance_criteria.\n\n"
    "## Constraints\n"
    "- Read backlog row in full before designing PRD\n"
    "- Include test_files matching every target_file\n"
    "- Follow rollout phases in chain_trigger_json.rollout_phases if present\n"
)

body = {
    "type": "pm",
    "prompt": prompt,
    "metadata": {
        "bug_id": BUG_ID,
        "operator_id": "observer-z5866",
        "source": f"chainN_{BUG_ID}_kickoff",
        "chain_note": "chain #N of MF-2026-04-21-005 follow-ups",
    },
    "priority": 80,
    "max_attempts": 3,
}

req = urllib.request.Request(
    f"{BASE}/api/task/{PID}/create",
    data=json.dumps(body).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
try:
    resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
    print(json.dumps(resp, indent=2))
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode('utf-8','replace')}")
```

**Before kickoff, check**:
1. `GET /api/version-check/aming-claw` → `ok=true, dirty=false`
2. Process tree: governance + service_manager + executor all up
3. Backlog row status=OPEN; AC reads sensibly to a fresh PM
4. No previous task with same `bug_id` still in `queued` or `claimed`

**During cascade, monitor**:
```bash
# Each stage transition
curl -s "http://localhost:40000/api/task/aming-claw/list" | python -c "
import json, sys
tasks = json.load(sys.stdin).get('tasks', [])
rows = [t for t in tasks if (t.get('metadata') or {}).get('bug_id') == 'OPT-BACKLOG-XXXXX']
rows.sort(key=lambda t: t.get('created_at',''))
for t in rows:
    print(t.get('created_at','')[11:19], t.get('task_id'), t.get('type'), t.get('status'))
"
```

---

## Where things live

| What | Where |
|---|---|
| Backlog API | `POST/GET /api/backlog/{pid}/{bug_id}` (governance port 40000) |
| Backlog table schema | `agent/governance/backlog_db.py` |
| Task creation API | `POST /api/task/{pid}/create` |
| Auto-chain logic | `agent/governance/auto_chain.py` (large file; key fns: `_gate_version_check` 1820-1878, `run_deploy` 2700-2900, `_finalize_version_sync` 2745-2785, `_DIRTY_IGNORE` 35) |
| Deploy bug evidence | `shared-volume/codex-tasks/logs/service-manager-executor-aming-claw.err.log` |
| MCP server tools | imported in registered MCP `aming-claw` (task_create, task_cancel, task_list, version_check, etc.) |
| Manual SOP | MEMORY.md → "Auto-Chain Manual Bootstrap SOP" |

---

## Recent backlog activity (2026-04-22)

```
NEW    OPT-BACKLOG-DEPLOY-DECOUPLE-MUTUAL-REDEPLOY  (P0, OPEN)
MERGED OPT-BACKLOG-DEPLOY-SELFKILL                  (superseded)
FIXED  OPT-BACKLOG-DIRTY-FILTER-CACHE               (commit 05f45af, chain #2)
FIXED  OPT-BACKLOG-MERGE-D6-EXPLICIT-FLAG           (commit 94edd28, chain #1)
OPEN   OPT-BACKLOG-QA-CLI-AUTH-TOKEN-STALE          (P0, chain #3)
OPEN   OPT-BACKLOG-TASK-MUST-FROM-BACKLOG           (P1, chain #4)
```

---

## Recommended next actions

1. **Read** the full `OPT-BACKLOG-DEPLOY-DECOUPLE-MUTUAL-REDEPLOY` row.
   Verify the design matches your understanding before touching code.
2. **Update** `OPT-BACKLOG-QA-CLI-AUTH-TOKEN-STALE` AC to add the
   "verify across executor reclaim" requirement (chain #1 reclaim
   evidence).
3. **Kick off chain #3** (QA-CLI-AUTH) — lowest deploy risk, good
   warm-up.
4. After chain #3 lands clean: **kick off chain #4** (TASK-MUST-FROM-
   BACKLOG). Note this one will hit SELFKILL (touches governance);
   apply F1 workaround.
5. After chains #3 + #4 land: **kick off chain #5** (DEPLOY-DECOUPLE).
   This is the structural fix that makes future chains safe. Ship in
   the 3-PR sequence captured in `chain_trigger_json.rollout_phases`.

After chain #5 PR-3 lands, F1/F2/F3 footguns become obsolete and this
handoff doc can be archived.

---

## Open architectural questions (for chain #5 PRD stage to address)

1. Should service_manager's HTTP server (port 40101) bind 127.0.0.1
   only, or also accept loopback6 (::1)? Current governance binds both.
2. Should redeploy handler use synchronous HTTP (caller waits for
   restart completion) or async (returns immediately, caller polls
   for status)? Sync is simpler but blocks the deploy task for the
   restart window (~5-10s); async needs a status endpoint.
3. For `affected = [executor]`, where exactly does the "mark task
   succeeded" call go? It needs to be persisted to DB before executor
   is killed but AFTER redeploy is confirmed inflight. One safe order:
   POST redeploy → wait for sm to ACK precheck OK → mark task
   succeeded → sm performs kill → new executor starts.
4. What is the bootstrap-token rotation policy for the locked-down
   `/api/version-update`? Stored in `.env`? Generated on each
   `start-governance.ps1` run?
5. Should the rare `affected = [governance, service_manager]` case
   actually be auto-handled by spawning a one-shot ops process from
   either side? The "manual runbook only" answer is conservative; an
   alternative is a third tiny daemon (`bootstrap-daemon`) that lives
   purely for this case + initial cold start.

These don't block kickoff; PM should explicitly call them out as
"to-be-decided in design phase".

---

*End of handoff. Good luck.*
