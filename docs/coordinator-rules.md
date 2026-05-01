# Coordinator Rules

> Migrated from original `docs/coordinator-rules.md`. Primary coordinator role specification now lives in [`docs/roles/coordinator.md`](roles/coordinator.md).
> This document covers supplementary rules enforced by the executor on behalf of the coordinator pipeline.

> **2026-04-28 update (Phase A):** Version gate now uses git-derived `chain_sha` as source of truth (not DB `chain_version`). `_gate_version_check` reads `get_chain_state()` which walks `git log --first-parent` for the latest `Chain-Source-Stage` trailer. The `effective_ver = db_chain_ver if db_chain_ver` pattern is removed — git trailers are authoritative.

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

## OPT-BACKLOG-TASK-MUST-FROM-BACKLOG: Task-Create Backlog Gate (Phase 1)

**Added:** 2026-04-22 | **Source:** `handle_task_create` in `agent/governance/server.py` | **Bug:** OPT-BACKLOG-TASK-MUST-FROM-BACKLOG

### Purpose

Ensures every code-change task (`pm`, `dev`, `test`, `qa`, `gatekeeper`, `merge`, `deploy`) carries a `metadata.bug_id` linking it to a backlog entry. Phase 1 operates in **warn mode** (log warning, never reject). Strict mode (HTTP 422 rejection) exists in code but is activated only by setting `OPT_BACKLOG_ENFORCE=strict`.

### Gate Logic

1. If `task_type` is a code-change type AND `metadata.bug_id` is missing:
   - **`OPT_BACKLOG_ENFORCE=warn`** (default): log `backlog_gate: missing bug_id` warning, allow creation.
   - **`OPT_BACKLOG_ENFORCE=strict`**: reject with HTTP 422 `bug_id required`.
2. If `metadata.force_no_backlog=true` AND `metadata.force_reason` is set: bypass gate, audit event `backlog_gate.observer_bypass` written to `chain_events`.
3. Non-code-change types (e.g., `coordinator`, `task`) are not gated.

### Observer Bypass

Set `metadata.force_no_backlog: true` and `metadata.force_reason: "<reason>"` to skip the gate. The bypass is audited via event bus and `chain_events` table with event type `backlog_gate.observer_bypass`.

### Invariant I2 Enforcement

This gate is the **server-side** half of invariant I2 (bug_id propagation). The **chain-side** half is CH2 (see below) which propagates `bug_id` through stage transitions and retries via `chain_context`.

### Test Coverage

- `agent/tests/test_task_create_backlog_gate.py` — warn mode, strict mode, observer bypass, non-code types

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

---

## OPT-BACKLOG-EXECUTOR-TEST-USE-SYS-EXECUTABLE: Use sys.executable in _execute_test

**Added:** 2026-04-27 | **Source:** `_execute_test` in `agent/executor_worker.py` (default cmd construction, line ~650) | **Bug:** OPT-BACKLOG-EXECUTOR-TEST-USE-SYS-EXECUTABLE

### Background

When `service_manager` spawns executor as a detached process on Windows, `PATH` inheritance is unreliable. ~50% of test-stage chains on 2026-04-26 failed with `WinError 2` because the bare `"python"` string in the default test command could not be resolved by the OS. `sys.executable` is the correct way to reference the running interpreter — it returns an absolute path to the exact Python binary that is executing the current process.

### Change

The default test command construction in `_execute_test` was:

```python
cmd = ["python", "-m", "pytest"] + test_files + ["-v", "--tb=short"]
```

Changed to:

```python
cmd = [sys.executable, "-m", "pytest"] + test_files + ["-v", "--tb=short"]
```

`sys` was already imported at module level. No other code paths are affected — when `verification.command` is provided in task metadata, the custom command is used as-is (unchanged).

### Test Coverage

| Test file | Tests | Covers |
|-----------|-------|--------|
| `agent/tests/test_executor_test_pythonpath.py` | `TestExecuteTestUsesSysExecutable` | Default cmd uses `sys.executable` as cmd[0] |
| `agent/tests/test_executor_test_pythonpath.py` | `TestExecuteTestResolvesToRuntimePythonOnWindows` | `sys.executable` is absolute and exists (Windows-only) |

### Coordinator Impact

The coordinator is unaffected. The fix operates at the executor level when running test-stage tasks with no explicit `verification.command`. Coordinator-dispatched test tasks benefit because the subprocess now always resolves the correct Python binary regardless of PATH inheritance.

---

## OPT-BACKLOG-PREFLIGHT-VALIDATOR-PR1B-PHANTOM-FATAL-DEV-ADVISORY: Phantom-Create FATAL Promotion + Dev Validator Advisory

**Added:** 2026-04-30 | **Source:** `agent/governance/output_schemas/error_codes.py` (FATAL_CODES expansion); `agent/role_permissions.py` (`_DEFAULT_ROLE_PROMPTS['dev']` advisory section); `docs/roles/dev.md` (workflow step 3.5) | **Bug:** OPT-BACKLOG-PREFLIGHT-VALIDATOR-PR1B-PHANTOM-FATAL-DEV-ADVISORY

### Background

PR1 (commit `f7bc3c4`) shipped the dev-stage preflight validator framework but
left two gaps that PR1b closes:

1. The two phantom-create error codes that motivated the validator —
   `PHANTOM_CREATE_FOR_DECLARED_REMOVED` and `PHANTOM_CREATE_FOR_UNMAPPED_FILE` —
   were classified as warnings, so default `mode='warn'` demoted them to
   non-fatal. The exact failure mode the validator was built to catch sailed
   through with `valid=True`.
2. The dev role prompt and `docs/roles/dev.md` were never updated to point dev
   at `scripts/validate_stage_output.py`. Dev had no in-prompt awareness of
   the validator at all.

### Changes

| Area | File | Change |
|------|------|--------|
| FATAL split | `agent/governance/output_schemas/error_codes.py` | `PHANTOM_CREATE_FOR_DECLARED_REMOVED` and `PHANTOM_CREATE_FOR_UNMAPPED_FILE` added to `FATAL_CODES`. `CREATE_NOT_IN_PROPOSED_NODES` remains the only "demoted under warn" code. |
| Dev advisory | `agent/role_permissions.py` | New "Output preflight (recommended)" section in `_DEFAULT_ROLE_PROMPTS['dev']` that mentions `scripts/validate_stage_output.py`, the `--stage=dev --input=<output.json>` flag form, the server-validates-regardless note, and the phantom-FATAL note. `_initialize()` falls back to the Python default when a stale YAML config lacks the validator marker. |
| Dev workflow doc | `docs/roles/dev.md` | New `### 3.5 Self-validate dev output (recommended)` subsection between steps 3 and 4 of the Task Workflow, mirroring the role-prompt advisory and back-linking to `docs/dev/proposal-stage-output-preflight-validator.md`. |
| Test coverage | `agent/tests/test_dev_result_validator.py` | Three new tests (`test_phantom_for_declared_removed_is_fatal_in_warn_mode`, `test_phantom_for_unmapped_file_is_fatal_in_warn_mode`, `test_dev_role_prompt_mentions_validator`) plus a rewrite of `test_phantom_create_for_unmapped_file`'s warn-mode assertions (now expects `valid=False` and the code in `errors`, not `warnings`). |

### Coordinator Impact

The coordinator itself is unaffected — it does not produce `graph_delta` and does
not run the preflight validator. The change reaches dev only. However, dev tasks
dispatched by the coordinator now receive a role prompt that explicitly tells them
how (and why) to self-validate before submitting, and any phantom-create errors
against PM-declared `removed_nodes` / `unmapped_files` will fail the checkpoint
gate — preventing the late-chain reject loops that previously stalled OPT-BACKLOG
chains for several stages before QA caught the drift.

---

## OPT-BACKLOG-PM-PRD-GRAPH-DECLARATIONS-MANDATORY: PM-Side Stage-Output Preflight Compliance (PR1d)

**Added:** 2026-05-01 | **Source:** `agent/governance/output_schemas/pm_result_schema.py` (new validator); `agent/governance/auto_chain.py` (`_validate_pm_at_transition` + `on_task_completed` wiring); `agent/role_permissions.py` (PM role-prompt advisory); `docs/roles/pm.md` (`## Graph-delta declarations` section) | **Bug:** OPT-BACKLOG-PM-PRD-GRAPH-DECLARATIONS-MANDATORY

### Background

PR1c (commit `6003ee8`) wired PRD declarations through to the graph-delta
auto-inferrer, but PR2 atomic-swap chains continued to phantom-reject because
PM result payloads consistently came back with `removed_nodes=None` and
`unmapped_files=None` across 6 verified attempts. The root cause: the PM role
prompt in `agent/role_permissions.py` never mentioned these fields, so per-task
prompt directives were unreliable. PR1d is the PM analogue of the PR1b dev fix:
role-prompt advisory + role-spec doc update + a server-side FATAL validator
wired into the post-PM transition.

### Changes

| Area | File | Change |
|------|------|--------|
| FATAL code | `agent/governance/output_schemas/error_codes.py` | New `MISSING_DECLARATION_FOR_DELETED_FILE` constant added to `FATAL_CODES`. |
| PM validator | `agent/governance/output_schemas/pm_result_schema.py` | New module exposing `validate_pm_output(payload, chain_context, mode='warn')`. Detects `MISSING_DECLARATION_FOR_DELETED_FILE` via case-insensitive substring scan over `acceptance_criteria` for `delete`/`remove`/`replaces`/`replaced_by`. Reuses the dev-side `ValidationError` / `ValidationResult` dataclasses + `_apply_mode` so behavior matches the dev validator across `strict` / `warn` / `disabled` modes. |
| Public API | `agent/governance/output_schemas/__init__.py` | `validate_pm_output` added alongside `validate_dev_output` in imports and `__all__`. |
| Auto-chain wiring | `agent/governance/auto_chain.py` | New `_validate_pm_at_transition(conn, project_id, task_id, result, metadata)` mirrors `_validate_dev_at_transition` (env-var mode, `observer_emergency_bypass` short-circuit, best-effort chain_context fetch, validator-crash safety returning `True`). `on_task_completed` invokes it for `task_type == 'pm'` BEFORE the dev branch and emits `{"preflight_blocked": True, "stage": "pm", "reason": "pm result preflight validation failed"}` on failure. |
| PM role prompt | `agent/role_permissions.py` | New "Output graph-delta declarations" advisory section in `_DEFAULT_ROLE_PROMPTS['pm']` placed between the existing "Important rules" bullets and the closing instruction line. Mentions `removed_nodes`, `unmapped_files`, `renamed_nodes`/`remapped_files`, and the delete-keyword trigger semantics. |
| PM role doc | `docs/roles/pm.md` | New `## Graph-delta declarations (required when AC implies file changes)` section with a worked single-file-deletion example showing `{removed_nodes: ["L7.X"], unmapped_files: ["path/to/file.py"]}`. |
| Test coverage | `agent/tests/test_pm_declarations_compliance.py` | Four tests: `test_pm_with_delete_ac_missing_declarations_fatal`, `test_pm_with_delete_ac_proper_declarations_pass`, `test_pm_with_no_delete_ac_declarations_optional`, `test_pm_validator_wired_into_auto_chain`. |

### Trigger Semantics

The keyword scan is **case-insensitive simple substring matching ONLY**:

```python
_DELETE_KEYWORDS = ("delete", "remove", "replaces", "replaced_by")
# applied via str.lower() on each acceptance_criteria entry — no LLM, no
# regex backreferences, no external service.
```

A PM payload trips `MISSING_DECLARATION_FOR_DELETED_FILE` when **all** of the
following hold:

1. `target_files` is a non-empty list, AND
2. At least one `acceptance_criteria` entry contains a delete-keyword
   (case-insensitive substring), AND
3. Both `removed_nodes` and `unmapped_files` are empty/None.

When the trigger fires under `mode='warn'` the validator emits a FATAL error
(severity `error`), driving `valid=False` and `_validate_pm_at_transition`
returning `False`, which causes `on_task_completed` to emit
`preflight_blocked` instead of dispatching the dev stage.

### Mode + Bypass Semantics

Identical to the dev-side path:

- `OPT_PREFLIGHT_VALIDATOR_MODE=warn` (default) — FATAL stays as error.
- `OPT_PREFLIGHT_VALIDATOR_MODE=strict` — all errors stay as errors.
- `OPT_PREFLIGHT_VALIDATOR_MODE=disabled` — short-circuit to `True` with a log line.
- `metadata.observer_emergency_bypass=true` + `metadata.bypass_reason="<reason>"` —
  short-circuit to `True` with a warning log line.
- Validator internal exception → return `True` (never block chain on validator bug).

### Coordinator Impact

The coordinator itself is unaffected — it does not produce PRDs and does not
run the preflight validator. The change reaches PM only. However, coordinator-
dispatched PM tasks now receive a role prompt that explicitly instructs PM to
declare `removed_nodes` / `unmapped_files` whenever AC implies file changes,
and any PM payload that fails the new check will be blocked at the post-PM
transition, preventing the downstream phantom-create reject loops that
previously stalled PR2-style atomic-swap chains.
