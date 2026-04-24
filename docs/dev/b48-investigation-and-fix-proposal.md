# B48 — Executor Silent-Death: Investigation + Fix Proposal

> **Status**: DRAFT — for observer review
> **Author**: observer-z5866 session 67351297
> **Date**: 2026-04-23
> **Related**: Phase 1 SM-TIMEOUT-BUMP chain (719f2ae) hit B48 × 6
> **Predecessor row**: `OPT-BACKLOG-SM-SCRIPT-TIMEOUT-BUMP` (addresses *startup wait*, not B48 itself)

---

## 0. TL;DR

B48 is **not** a worker crash — it's a Windows process-tree cleanup bug.
Workers are being `TerminateProcess()`'d silently by something outside Python;
SM's monitor loop probably dies too (sidecar crash or `_running=False` side-effect).
Neither emits any trace because SM's logs go to a hidden-window stderr that is
never captured.

Proposed fix: **two layered changes**, rolled out as separate chains.
- **Fix A (10 min)** — add FileHandler to SM logging so we can actually *see* the crash.
- **Fix B (30 min, after A proves it)** — detach executor Popen with `CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB` so Windows process-tree cleanup can't cascade.

---

## 1. Observed evidence (Phase 1 chain, 6 B48 cycles)

### 1.1 The error log

Full stderr log for executor: `shared-volume/codex-tasks/logs/service-manager-executor-aming-claw.err.log` (2267 lines).

```
grep -c Traceback → 3 lines, all from 2026-03-30 (unrelated os.kill bug)
grep -c "Error:\|Exception\|FATAL\|SystemExit" → 0 for this session
```

**No Python exception was raised by any of the 6 dying workers.**

### 1.2 The "last log" pattern

| Worker PID | Started | Last log line before death |
|---|---|---|
| 44928 | 18:28:10 | `18:34:09 WARNING: 10 consecutive empty polls` |
| 24100 | 19:44:21 | `19:48:11 WARNING: 10 consecutive empty polls` |
| 39512 | 20:15:58 | `20:19:08 WARNING: 10 consecutive empty polls` |
| 47392 | 20:49:24 | `20:54:07 WARNING: 10 consecutive empty polls` |
| 48784 | 21:20:04 | `21:23:22 WARNING: 10 consecutive empty polls` |
| 25124 | 21:47:37 | `21:50:48 WARNING: 10 consecutive empty polls` |

**All six** workers last log is the idle-poll warning. No shutdown message, no
traceback, no goodbye. Process just stops writing.

Important nuance: the warning fires at the **10th** consecutive empty poll
(per `_consecutive_empty_polls == 10` in executor_worker.py:2148). It does NOT
recur. So "last log = empty-polls warning" just means the worker **was idle
when it died** — not necessarily that the warning caused the death.

### 1.3 `run_loop` cannot exit silently

Per `agent/executor_worker.py:2388-2442`:

```python
try:
    while self._running:
        try:
            ...                                # business logic
        except KeyboardInterrupt:
            log.info("Shutting down...")        # would appear in log
            self._running = False
        except Exception as e:
            log.error("Poll loop error: %s", e, exc_info=True)
            # traceback would appear
            time.sleep(POLL_INTERVAL)
finally:
    if self._worker_pool:
        self._worker_pool.shutdown()            # would log
    self._release_pid_lock()                    # would log
```

A Python-level exit path MUST leave a trace. None exists for the 6 deaths.

### 1.4 Conclusion

**The worker process was terminated externally by the OS, not by Python itself.**
On Windows this means `TerminateProcess()` — immediate kill, no Python cleanup,
no traceback, no `finally` block.

### 1.5 Who called `TerminateProcess()`?

Candidates:

1. **SM's `_stop_locked`** → `proc.terminate()` → `TerminateProcess()` (service_manager.py:214)
2. **`taskkill /F /T`** from `start-manager.ps1` takeover mode (only when observer takes over)
3. **Windows Job Object cleanup** when parent PowerShell exits
4. **Windows OOM killer** (unlikely — machine had plenty of RAM; no Event Log entries)
5. **Antivirus / Windows Defender** (speculative)

### 1.6 Why SM doesn't respawn

Per SM's `_monitor_loop` (service_manager.py:433-520):
```
every 10s:
    check proc.poll() — if None, skip
    if process died:
        respawn (unless circuit_breaker_tripped)
```

This **should** work. But we observed 6 consecutive deaths with NO respawn.
That means monitor_loop itself is not running. Possible causes:

- **Sidecar crash-together semantics**: if `manager_http_server` sidecar
  crashes, `_sidecar_crashed = True` → `_running = False` → monitor exits
  (service_manager.py:446-452). Main SM process stays alive (stuck in
  `while True: time.sleep(5)` at line 770), but no longer monitoring.
- **Circuit breaker tripped**: 5 restarts within 300s → `_circuit_breaker_tripped = True`.
  Main process still alive, but line 483-490 returns early without respawning.
- **Monitor thread crashed**: unhandled exception in the thread target. Daemon
  threads die silently on exception.

### 1.7 Why we can't tell which

**SM's logs are invisible**. Per service_manager.py:738-742:

```python
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
```

No FileHandler. Stderr goes to the hidden window (or void, since we launch
with `-WindowStyle Hidden` from PowerShell). **Every SM log message since day 1
has been silently discarded.**

---

## 2. Fix A — Add FileHandler to SM logging (P0, 10 min)

### 2.1 Scope

Single file: `agent/service_manager.py`. Add ~10 lines to `main()`.

### 2.2 Code change

```python
# service_manager.py :: main()

log_dir = _shared_log_dir()
log_dir.mkdir(parents=True, exist_ok=True)

log_file = log_dir / f"service-manager-{args.project}.log"
# Rotate when >50 MB; keep 3 old files
from logging.handlers import RotatingFileHandler
file_handler = RotatingFileHandler(
    str(log_file), maxBytes=50_000_000, backupCount=3, encoding="utf-8"
)
file_handler.setFormatter(logging.Formatter(
    "[%(asctime)s] %(name)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[file_handler, logging.StreamHandler()],
)
```

### 2.3 What we'll see immediately next B48

All the following existing log calls become visible:
- `ServiceManager._monitor_loop: executor process (PID X) died; preparing restart …`
- `ServiceManager._monitor_loop: circuit breaker tripped — N restarts within Ms`
- `ServiceManager: sidecar crashed: <exception>`
- `ServiceManager._check_restart_signal: restart signal received`
- `ServiceManager._stop_locked: terminating executor (PID X)`
- `ServiceManager.stop: executor did not exit after SIGTERM; sending SIGKILL`

If **any** of these appears in the new log file when B48 next strikes, we have
the answer to "who killed the worker" in one shot.

### 2.4 Verification

After Fix A lands + next chain runs:
1. Check `shared-volume/codex-tasks/logs/service-manager-aming-claw.log` exists
2. Tail should show `monitor_loop` ticks, sidecar startup, executor spawn
3. Next B48 → file tells us the cause

### 2.5 Test plan

Add `agent/tests/test_service_manager_logging.py`:
- Import service_manager, call `main()` with --status-only flag
- Verify `service-manager-*.log` exists in shared-volume
- Verify it contains at least one line with expected format

### 2.6 Risk

Near-zero. Adding a FileHandler cannot kill anything. Only side-effect: a new
file is created.

---

## 3. Fix B — Detach Executor Popen from Parent Process Tree (P0, 30 min after A)

### 3.1 Scope

Modify `ServiceManager._spawn_executor_process` (service_manager.py:622-638)
to launch the executor detached from SM's process tree.

### 3.2 Code change

```python
def _spawn_executor_process(self) -> subprocess.Popen:
    """Spawn the executor and redirect output to a persistent host log file."""
    log_dir = _shared_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f"service-manager-executor-{self.project_id}.log"
    stderr_path = log_dir / f"service-manager-executor-{self.project_id}.err.log"
    stdout_handle = open(stdout_path, "ab")
    stderr_handle = open(stderr_path, "ab")

    # Windows: detach from parent process tree to prevent Job Object cascade kill
    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | 0x01000000  # CREATE_BREAKAWAY_FROM_JOB (may fail if Job has NO_BREAKAWAY)
        )

    try:
        return subprocess.Popen(
            self._executor_cmd,
            stdout=stdout_handle,
            stderr=stderr_handle,
            creationflags=creationflags,
            # Unix equivalent (no-op on Windows):
            start_new_session=True if sys.platform != "win32" else False,
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()
```

### 3.3 Why this might fix B48

Windows Job Objects: when a parent process is in a Job with
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, **all children die when the Job closes**.
Claude Code (the IDE that is running this session) likely uses Job Objects to
manage shell/subprocess lifecycles.

With `CREATE_BREAKAWAY_FROM_JOB`, the executor escapes the Job at spawn time.
Even if Claude Code tears down a PowerShell that called our script, the
executor stays alive.

`CREATE_NEW_PROCESS_GROUP` additionally makes the executor immune to Ctrl+C
(SIGBREAK) propagating from parent shell — this is important because PowerShell
tool invocations often send Ctrl+C on timeout.

### 3.4 Caveats

- **CREATE_BREAKAWAY_FROM_JOB fails silently** if the Job has
  `JOB_OBJECT_LIMIT_BREAKAWAY_OK` disabled. We'd need to catch and fall back.
- Detached process has no parent stdin/stdout — already handled (we pipe to files).
- Detached process becomes observer's responsibility to terminate cleanly on
  shutdown. SM's `_stop_locked` still works (`proc.terminate()` kills by PID
  regardless of tree).

### 3.5 Test plan

Add `agent/tests/test_service_manager_detach.py`:
- Mock subprocess.Popen, verify creationflags on Windows
- Verify `CREATE_NEW_PROCESS_GROUP` bit is set
- Verify `CREATE_BREAKAWAY_FROM_JOB` bit is set
- Non-Windows: verify `start_new_session=True`

### 3.6 Verification (post-deploy)

1. Kick a chain; let it run to completion
2. Observer does NOT manually restart SM throughout
3. Chain reaches merge+deploy without observer intervention
4. Count B48 = 0 (vs. current 6 per chain)

---

## 4. Fix C — Worker Self-Respawn (P1, defer)

Not needed if Fix A+B solve the problem. File as `OPT-BACKLOG-WORKER-SELF-RESPAWN`
(P1) for later, only if B48 recurs after A+B.

---

## 5. Execution plan

### Phase 2 (NEW, replacing the previous Phase 2 Option A ordering)

**Step 1** — Fix A chain (log visibility)
- File backlog row `OPT-BACKLOG-SM-LOG-VISIBILITY` (P0)
- POST PM task: scope = agent/service_manager.py:738-742 + new test file
- Normal chain through merge + deploy
- Expected duration with current B48: ~5h (will hit B48 × 6)
- Expected duration after Fix B: ~45min

**Step 2** — Observe next B48 (no code change, just wait)
- After Fix A lands, next chain's B48 leaves evidence in
  `service-manager-aming-claw.log`
- Observer reads log, refines Fix B based on actual cause

**Step 3** — Fix B chain (detach executor)
- Based on Fix A's diagnostic evidence, implement detach fix
- If cause confirmed as Windows Job Object (likely), use proposed detach code
- If cause is different (e.g., SM monitor thread exception), fix accordingly
- Normal chain

**Step 4** — Verification chain (smoke test)
- Kick a trivial docs-only chain
- Observer does NOT touch sm/executor
- Verify B48 count = 0 through all 6 stages
- If passes → B48 resolved

**Step 5** — Resume original plan
- Phase 2: Option A (`OPT-BACKLOG-PERSIST-EVENT-CONN-TIMEOUT`)
- Phase 3-5 as before (verification, reconcile dry-run, reconcile live)

### Total delay vs. original plan

Fix A adds ~5h (one chain hitting B48 × 6 — because the FIX for B48 isn't live yet, bootstrap paradox).
Fix B adds ~5h (same bootstrap issue if B48 still present).
**BUT** — once Fix B lands, all subsequent chains (Option A, reconcile, Option D) run in 45min instead of 5h each. Net savings = ~4h per future chain, paying off within 2 chains.

---

## 6. Open questions for review

1. **Fix A first, diagnose, then Fix B** — vs. ship Fix A+B in one chain based
   on current hypothesis?
   - Recommendation: **Fix A first**. Current hypothesis is ~70% confident;
     cheap to verify before shipping a 30-line code change.

2. **Separate chains or combined PR?**
   - Recommendation: **separate**. Combining increases surface area + risk.
     Fix A is genuinely independent diagnostic value.

3. **Circuit breaker reset policy** (orthogonal but relevant)
   - Currently 5 restarts in 300s → trip + never reset until manual SM
     restart. Should we add auto-reset after 600s quiet?
   - Recommendation: **defer to Fix C** (worker self-respawn). Not critical now.

4. **Apply Fix B only on Windows (`sys.platform == "win32"`)?**
   - Yes — Unix uses `start_new_session=True` which is already the correct
     equivalent. Windows is the only platform with Job Object cascade kill.
   - Recommendation: **conditional on platform**.

---

## 7. What happens if I'm wrong about root cause

If Fix A's diagnostic log shows the actual cause is DIFFERENT from Job Object
cascade (e.g., monitor_thread raised KeyError somewhere), we:

1. Don't ship Fix B as-is
2. Use the actual traceback to fix the real cause
3. Re-verify via another smoke chain

Fix A is **zero-regression**; it only adds visibility. No harm done.

---

## 8. Alignment with prior Option A work

- Option A (`OPT-BACKLOG-PERSIST-EVENT-CONN-TIMEOUT`) design doc at
  `docs/dev/option-a-persist-event-timeout-proposal.md` remains APPROVED but
  execution deferred until B48 is fixed.
- No conflicts between B48 fix and Option A (different code files, different
  concerns).
- After B48 is fixed, Option A chain will run in ~45min (vs. current 5h).

---

## 9. Checklist for execution

- [ ] Observer reviews & approves this doc
- [ ] File backlog row `OPT-BACKLOG-SM-LOG-VISIBILITY` (P0, Fix A)
- [ ] POST PM task → Dev → Test → QA → Gatekeeper → Merge → Deploy
- [ ] Observer survives 6 B48 cycles during this chain (bootstrap)
- [ ] Verify `service-manager-aming-claw.log` file exists + contains output
- [ ] Next chain: observe & collect B48 evidence from new log
- [ ] Refine Fix B design based on evidence
- [ ] File `OPT-BACKLOG-SM-DETACH-PROCESS-TREE` (P0, Fix B)
- [ ] Ship Fix B chain
- [ ] Smoke verification chain
- [ ] If B48 = 0: update MEMORY.md, resume Option A
- [ ] If B48 persists: file Fix C (`OPT-BACKLOG-WORKER-SELF-RESPAWN`) + escalate

---

*End of doc. Awaiting observer review before execution.*
