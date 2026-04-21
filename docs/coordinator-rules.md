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
