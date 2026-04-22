# Coordinator Rules

> Migrated from original `docs/coordinator-rules.md`. Primary coordinator role specification now lives in [`docs/roles/coordinator.md`](roles/coordinator.md).
> This document covers supplementary rules enforced by the executor on behalf of the coordinator pipeline.

## B41: Cross-Platform Verification Command Guard

**Added:** 2026-04-21 | **Source:** `_assert_portable_verification_command` in `agent/executor_worker.py`

### Purpose

The executor runs verification commands specified in PM PRDs (the `verification.command` field). On Windows hosts, Unix-only commands silently fail or produce misleading results. The B41 guard rejects non-portable commands **before** subprocess execution, ensuring verification is meaningful across platforms.

### Banned Unix Commands (9)

The following first-token commands are rejected:

| Command | Reason |
|---------|--------|
| `grep`  | Use `python -m pytest` or `python -c` for assertion-based checks |
| `sed`   | Not available on Windows without additional tooling |
| `awk`   | Not available on Windows without additional tooling |
| `find`  | Windows `find` has different semantics than Unix `find` |
| `head`  | Not available on Windows without additional tooling |
| `tail`  | Not available on Windows without additional tooling |
| `cat`   | Not available on Windows without additional tooling |
| `cut`   | Not available on Windows without additional tooling |
| `xargs` | Not available on Windows without additional tooling |

### Banned Shell Operators (4)

The following shell chaining operators are rejected anywhere in the command string:

| Operator | Reason |
|----------|--------|
| `&&`     | Shell-specific chaining; use pytest for multi-step verification |
| `\|\|`  | Shell-specific chaining; unreliable across shells |
| `\|`    | Pipe operator; not portable across cmd.exe / PowerShell / bash |
| `;`     | Statement separator; not portable across shells |

### Valid Commands

Commands that pass the guard (examples):

- `pytest agent/tests/test_foo.py -v`
- `python -m pytest agent/tests/ -v --tb=short`
- `python -c "assert True"`

### Rejection Behavior

When a command is rejected, the guard returns a structured failure dict:

```json
{
  "status": "failed",
  "result": {
    "test_report": {
      "tool": "b41-guard",
      "summary": "B41: non-portable verification.command rejected: <reason>",
      "passed": 0,
      "failed": 1,
      "command": "<rejected command>"
    },
    "error": "B41: non-portable verification.command rejected: <reason>"
  }
}
```

This causes the task to fail at the test stage, triggering a dev retry with the rejection reason in the prompt. The PM or dev must then provide a portable verification command.

### Empty / Whitespace Commands

Empty strings and whitespace-only strings are also rejected with reason `"empty or whitespace-only command"`.

### Implementation

- **Guard function:** `_assert_portable_verification_command()` in `agent/executor_worker.py`
- **Helper:** `_b41_reject()` builds the structured failure dict
- **Integration point:** Called in `ExecutorWorker._run_test_script()` before `subprocess.run()`
- **Test coverage:** `agent/tests/test_verification_command_cross_platform.py` (18 parametrized cases: 9 banned-command, 4 banned-operator, 2 empty/whitespace, 3 valid pass-through)

### Coordinator Impact

The coordinator itself is unaffected (it has no tools and does not run commands). The guard operates at the executor level when executing test-stage tasks. However, PM PRDs that specify non-portable verification commands will cause test-stage failures, which propagate back through the auto-chain as dev retries. PMs should specify `pytest` or `python -c` commands for verification.

---

## OPT-BACKLOG-CH1: Backlog ID Auto-Tagging

**Added:** 2026-04-21 | **Source:** `_extract_backlog_id` + `_handle_coordinator_v1` in `agent/executor_worker.py` | **Graph node:** L4.43 (backlog-as-chain-source policy)

### Purpose

When the coordinator emits a `create_pm_task` action, its `forwarded_meta` must carry a `bug_id` so that the downstream merge-stage helper `auto_chain._try_backlog_close_via_db` can fire backlog close on merge. Without this tag, chain-driven fixes leave their backlog rows stuck at `OPEN` (the B41 drift class generalized as `OPT-BACKLOG-AS-CHAIN-SOURCE`).

Auto-tagging extracts the ID from the coordinator task's own prompt when it is not already provided by an upstream source.

### Recognized ID shapes

| Pattern | Regex | Example |
|---------|-------|---------|
| Bug ID  | `B\d+` | `B41` |
| Manual-fix ID | `MF-\d{4}-\d{2}-\d{2}-\d{3}` | `MF-2026-04-21-004` |
| Optimization epic / sub-chain | `OPT-[A-Z0-9][A-Z0-9-]*` | `OPT-BACKLOG-CH1-COORD-AUTOTAG` |

All three are matched with `\b` word boundaries; the leftmost match wins. Token `_BACKLOG_ID_RE` is the single source of truth.

### Precedence (idempotent)

When multiple sources could supply `bug_id`, the resolved order is:

1. **`parent_meta.bug_id`** — forwarded from the whitelist loop (inherited from coordinator task metadata).
2. **`action.bug_id`** — explicitly set by the coordinator in its JSON output.
3. **Prompt extraction** — `_extract_backlog_id(task.prompt)` only when 1 and 2 are both absent.

Once `forwarded_meta["bug_id"]` is set, no later step may overwrite it. This guarantees observer-tagged tasks and explicit coordinator tags always win over heuristic extraction.

### Trace log line

On successful extraction, the code emits:

```
autotag: task=<coordinator_task_id> bug_id=<extracted_id>
```

via `_hv_log` (file-based, written to `shared-volume/codex-tasks/logs/coordinator-flow-<task_id>.txt`). **Never** via `log.info` — see the MCP subprocess `log.info` IO-deadlock pitfall in `MEMORY.md`.

### Negative cases (must NOT match)

- `"please fix the login page"` — no backlog token
- `"OPTION-X"` — OPT- must be followed by a letter/digit, but the `-` after `OPT` is missing
- `"BMW42"` — `B` is followed by a non-digit, so `\bB\d+\b` cannot match
- `"MF-2026-04-21"` — missing the `-NNN` suffix

### Implementation

- **Regex:** `_BACKLOG_ID_RE` in `agent/executor_worker.py` (module-level)
- **Extractor:** `_extract_backlog_id(text) -> Optional[str]`
- **Injection site:** `_handle_coordinator_v1` around line 1611-1635 (just before the `POST /api/task/{pid}/create` call)
- **Test coverage:** `agent/tests/test_coordinator_autotag.py` (9 parametrized positive, 9 parametrized negative, 6 idempotency/precedence cases, 1 source-guard)

### Downstream consumer

`agent/governance/auto_chain.py::_try_backlog_close_via_db` reads `metadata.bug_id` during merge-stage finalize and calls `POST /api/backlog/{pid}/{bug_id}/close` with the merge commit. Without autotag, that helper short-circuits and the backlog row stays `OPEN`.

### Observer note

Before this autotag landed, observers had to manually set `metadata.bug_id` on every coordinator task. That discipline remains valid — the autotag is a fallback for cases where the observer forgets or for AI-driven flows that cite the ID in prose. Pre-declaring the backlog entry (see `docs/dev/manual-fix-sop.md` §13) plus passing `metadata.bug_id` explicitly is still the canonical form.

---

## OPT-BACKLOG-CH2: Chain-Level bug_id Propagation

**Added:** 2026-04-21 | **Source:** `ChainContext.bug_id` + `get_bug_id` in `agent/governance/chain_context.py`; retry-metadata fallback in `agent/governance/auto_chain.py` | **Graph node:** L4.43 (backlog-as-chain-source policy, same as CH1)

### Purpose

CH1 makes sure the **first** coord→PM hop carries `metadata.bug_id`. But that tag only survives as long as the in-process metadata dict survives. Any of these can drop it:

- A retry task builds new metadata from a parent whose `bug_id` was already stripped.
- A gate-block reflex re-dispatches from a stage that lost the field in transit.
- The governance process restarts, and the replay path has to reconstitute context from `chain_events` rather than a live dict.

CH2 makes `bug_id` durable at the **chain** level. Once any task in the chain announces a `bug_id` via its `task.created` event, the `ChainContextStore` memorizes it on the `ChainContext` (first-write-wins). Retry code paths fall back to this chain-level tag when their own metadata comes up empty.

### Contract

- **`ChainContext.bug_id`** — `Optional[str]`, defaults to `None`. Listed in `__slots__`, so typos raise `AttributeError` instead of silently appearing.
- **First-write-wins** — `on_task_created` only sets `chain.bug_id` if it is currently `None` and the incoming `payload.metadata.bug_id` is a non-empty string. Subsequent stages with a different `bug_id` do **not** overwrite. (Defensive behavior: unexpected override usually means bug swap mid-chain, which is a caller bug.)
- **Read API** — `ChainContextStore.get_bug_id(task_id) -> Optional[str]`. Resolves any `task_id` in the chain to the shared chain-level `bug_id`. Returns `None` for unknown tasks or chains without a tag.
- **Serialization** — `get_chain(...)` includes `"bug_id": <value>` only when set. Backward compatible: existing callers who never asked for the field still see the same shape.

### Event payload shape

`auto_chain._dispatch_next_stage` publishes `task.created` with:

```json
{
  "project_id": "...",
  "parent_task_id": "...",
  "task_id": "...",
  "type": "dev",
  "prompt": "...",
  "source": "auto-chain",
  "metadata": {"bug_id": "<forwarded>"}
}
```

The `metadata` key is new as of CH2 and is the signal `chain_context.on_task_created` reads. Older payloads without `metadata` are still valid — `bug_id` simply stays `None` until some later stage introduces it.

### Retry-path fallback (the whole point)

Two retry sites in `auto_chain.py` now fallback-fill before calling `task_registry.create_task`:

1. **Cross-stage retry** (test/qa failure → dev retry, around line 1221): if the reconstructed retry metadata lacks `bug_id`, consult `get_store().get_bug_id(task_id)` and inject it.
2. **Same-stage retry** (gate-block reflex, around line 1344): same pattern, applied after stripping `_worktree`/`_branch`/`failure_reason`.

Both paths emit an `auto_chain: CH2 fallback-filled bug_id=...` log line on success so the fallback is observable.

### Crash recovery

`ChainContextStore.recover_from_db` replays persisted `chain_events` through the same `on_task_created` handler. Because our `task.created` payloads now include `metadata.bug_id`, replay reconstitutes the chain's `bug_id` automatically — no separate recovery codepath needed. Events written before CH2 (no `metadata` key) leave `bug_id` as `None`; those chains predate the feature and do not benefit from it.

### Relationship with CH1

CH1 is the **source** (autotag the coordinator task). CH2 is the **sink's insurance** (keep the tag alive through retries and restarts). Together they close the loop: every chain starts with a `bug_id`, every chain preserves it, and the merge-stage backlog-close helper (`_try_backlog_close_via_db`) never runs dry.

### Test coverage

- `agent/tests/test_chain_context_bugid.py` — 19 tests covering field presence, `__slots__` enforcement, first-write-wins, empty/non-string rejection, late-arrival population, cross-stage read, serialize round-trip, retry-fallback simulation, crash-recovery replay, idempotency.
- `agent/tests/test_chain_context.py` — 25 regression tests continue to pass (no behavior change for chains without `bug_id`).

---

## OPT-BACKLOG-MERGE-D6-EXPLICIT-FLAG: Pre-Merged Detection for HEAD==chain_version

**Added:** 2026-04-22 | **Source:** `_execute_merge` in `agent/executor_worker.py` (D6 detection block, lines ~706-774) | **Bug:** OPT-BACKLOG-MERGE-D6-EXPLICIT-FLAG

### Background

The D6 fix (20baea3) added pre-merge auto-detection for chained merge tasks that lack `_branch`/`_worktree` metadata. It works when `HEAD != chain_version` (indicating commits landed after the version checkpoint). However, when `HEAD == chain_version` — as happens when an observer-walked chain already committed everything before the merge stage runs — the detection fell through to a "no isolated merge metadata" error. MF-2026-04-21-005 merge was blocked by this on 2026-04-22.

### New Detection Paths

Two new checks are added inside the existing `if metadata.get('parent_task_id') and not branch:` guard, between the `HEAD != chain_version` return and the error return:

1. **Explicit `pre_merged` flag (R2):** If `metadata.get("pre_merged")` is truthy, return success immediately with `pre_merged: True`. This allows upstream dispatchers (observers, auto-chain) to signal that the merge is already done without requiring changed_files verification.

2. **Changed-files-in-HEAD detection (R1):** When `HEAD == chain_version` and `metadata.changed_files` is non-empty, run `git log -1 --name-only HEAD` to list files in the HEAD commit. If all `changed_files` appear in that list, treat as pre-merged and return success with `pre_merged: True` and `merge_commit: HEAD_short_hash`.

If neither check matches, the existing error ("no isolated merge metadata") is still returned.

### Decision Matrix

| HEAD vs chain_version | `_already_merged` / `_merge_commit` | `pre_merged` flag | changed_files in HEAD | Result |
|----------------------|--------------------------------------|-------------------|-----------------------|--------|
| any | set | any | any | success (existing path, line 710) |
| HEAD != chain_version | not set | any | any | success (existing D6, line 736) |
| HEAD == chain_version | not set | True | any | success (new R2, line 745) |
| HEAD == chain_version | not set | not set | all present | success (new R1, line 751) |
| HEAD == chain_version | not set | not set | missing/empty | failed (error, line 766) |

### Implementation

- **Guard function:** Inside `_execute_merge()`, within the `if metadata.get('parent_task_id') and not branch:` block
- **Subprocess calls:** `git log -1 --name-only --format= HEAD` (read-only, already used elsewhere)
- **Test coverage:** `agent/tests/test_executor_worker_merge.py` (8 test functions covering AC1-AC5 plus edge cases)

---

## OPT-BACKLOG-QA-CLI-AUTH-TOKEN-STALE: Stale OAuth Token Env-Strip + Auth Hardening

**Added:** 2026-04-22 | **Source:** `agent/ai_lifecycle.py` (env-strip tuple, lines ~269-277), `agent/executor_worker.py` (`_detect_terminal_cli_error`, `_check_auth_smoke_test`, `run_loop`) | **Bug:** OPT-BACKLOG-QA-CLI-AUTH-TOKEN-STALE

### Background

During MF-2026-04-21-005 reconcile, executor QA tasks failed 3x with 401 because `CLAUDE_CODE_OAUTH_TOKEN` inherited from service_manager launch env outlived Claude Code session rotation. Additionally, `pid=0` race in session creation confused logs, and `_detect_terminal_cli_error` did not classify JSON auth failures. This is chain #3 of MF-005 follow-ups; chains #1 (MERGE-D6-EXPLICIT-FLAG at 94edd28) and #2 (DIRTY-FILTER-CACHE at 05f45af) already landed.

### Changes (5 requirements)

#### R1: Env-Strip — CLAUDE_CODE_OAUTH_TOKEN

`ai_lifecycle.create_session` strips env vars before passing them to `subprocess.Popen`. The exclusion tuple now includes `CLAUDE_CODE_OAUTH_TOKEN`:

```python
env = {k: v for k, v in env.items()
       if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                    ...,
                    "CLAUDE_CODE_OAUTH_TOKEN")}
```

This prevents stale tokens inherited from the service_manager launch environment from being forwarded to Claude CLI subprocesses.

#### R2: pid=0 Logging Guard

`AISession` is created with `pid=0` as a sentinel before `subprocess.Popen` assigns the real PID. A guard now prevents logging `session.pid` when `pid == 0`, so crash-recovery log grep does not confuse sentinel values with real process IDs.

#### R3: Auth Failure Classifier

`executor_worker._detect_terminal_cli_error` now detects JSON-shaped auth failure responses:

| Pattern | Example |
|---------|---------|
| `unauthorized` | `{"error":"Unauthorized"}` |
| `invalid_token` | `{"error":"invalid_token"}` |
| `authentication_error` | `authentication_error: bad credentials` |
| `token_expired` | `{"error":"token_expired"}` |
| HTTP 401/403 with `"error"` | `{"error":"auth failed","status":401}` |

When detected, a descriptive string is returned instead of `None`, causing the task to be marked as a terminal failure rather than retried endlessly.

#### R4: Auth Smoke Test at Startup

`executor_worker.run_loop` now calls `_check_auth_smoke_test()` between the governance health check and `_recover_stuck_tasks`. This verifies `CLAUDE_CODE_OAUTH_TOKEN` is not present in the executor's own environment. Logs a warning if found but does **not** block startup.

#### R5: Reclaim Cycle Durability

The env-strip fix (R1) and auth classifier (R3) are stateless — they operate on each `create_session` / `_detect_terminal_cli_error` call independently. A reclaimed task (`_recover_stuck_tasks` → re-poll → re-claim → fresh `create_session`) gets a clean env without the stale token, because the strip tuple is evaluated at call time against `os.environ`.

### Test Coverage

| Test file | Tests | Covers |
|-----------|-------|--------|
| `agent/tests/test_auth_token_env_strip.py` | 4 | R1: env-strip tuple, child env verification |
| `agent/tests/test_lifecycle_pid_race.py` | 3 | R2: pid=0 guard, sentinel documentation |
| `agent/tests/test_auth_failure_classifier.py` | 10 | R3: auth patterns, 401/403, existing patterns |
| `agent/tests/test_executor_auth_smoke.py` | 5 | R4: smoke test presence, warning/OK behavior |
| `agent/tests/test_auth_reclaim_e2e.py` | 3 | R5: full reclaim cycle, token not in child env |

### Coordinator Impact

The coordinator itself is unaffected — it does not manage env vars or authenticate. The fix operates at the executor/lifecycle level. However, coordinator-dispatched tasks (dev, test, QA) benefit because their child CLI processes no longer inherit stale OAuth tokens.
