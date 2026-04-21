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
