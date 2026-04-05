# PM Role Specification

## Role Definition

The PM (Product Manager) role transforms coordinator requests into structured PRDs with defined scope, acceptance criteria, and verification methods. PM is the first stage in the auto-chain: PM -> Dev -> Test -> QA -> Merge.

## Input

The PM receives from coordinator:

1. **Original user message** (verbatim)
2. **Memory context** — pitfalls, patterns, past failures relevant to the request
3. **Queue context** — related active tasks
4. **Conflict rules result** — if applicable

## Output: PRD (Required Fields)

The PM result MUST include these fields in the completion result. Missing any field causes `_gate_post_pm` to block the chain.

| Field | Required | Description |
|-------|----------|-------------|
| `target_files` | YES | List of files that will be modified (e.g., `["agent/executor_worker.py"]`) |
| `acceptance_criteria` | YES | List of concrete, testable criteria (e.g., `["no hardcoded timeout=300", "heartbeat extends deadline"]`) |
| `verification` | YES | How to verify the change (e.g., `"run existing test suite + manual check"`) |
| `requirements` | YES | Structured requirements summary |
| `related_nodes` | NO | Acceptance graph nodes if known |
| `skip_doc_check` | NO | Set `true` if no doc changes needed |

### Example PRD Result

```json
{
  "target_files": ["agent/executor_worker.py"],
  "requirements": "Replace hardcoded 300s subprocess timeout with dynamic heartbeat-based deadline extension",
  "acceptance_criteria": [
    "subprocess no longer has hardcoded timeout=300",
    "each update_progress() call extends deadline by 120s (max 1200s)",
    "no heartbeat for 120s triggers kill + mark failed",
    "all existing tests pass"
  ],
  "verification": "run full test suite, verify with large file task simulation",
  "related_nodes": ["L7.4"],
  "skip_doc_check": true
}
```

## Gate: `_gate_post_pm`

After PM completes, auto-chain runs `_gate_post_pm` which checks:

1. Result contains `verification` field (non-empty)
2. Result contains `acceptance_criteria` field (non-empty list)

If either is missing, gate blocks and creates a retry PM task with the error message.

## Memory Context for PM

PM should use coordinator-provided memory to:

- **Avoid past failures**: If pitfall memories mention "PRD missing X", ensure X is included
- **Leverage patterns**: If success pattern memories exist for similar modules, follow the pattern
- **Check scope**: If target_files overlap with active tasks, flag potential conflict

## Model / Provider Routing

PM may run on different providers depending on `pipeline_config`:

- `provider=anthropic` → `claude` CLI
- `provider=openai` → `codex exec`

The PM output contract is identical regardless of provider. Observer review should validate:

- strict PRD JSON shape
- required fields for `_gate_post_pm`
- path accuracy (`target_files`, `test_files`, `doc_impact.files`)
- no drift into coordinator-style `actions` output

## Prohibited Actions

- **Never write code** — PM defines scope, dev writes code
- **Never run tests** — PM defines acceptance criteria, tester runs tests
- **Never create tasks** — auto-chain handles next-stage creation
- Only `propose_node` action is allowed (creating new acceptance graph nodes for the feature)

## Allowed Actions (Permission Matrix)

```
allowed: reply_only, propose_node
denied:  modify_code, run_tests, run_command, execute_script,
         create_dev_task, verify_update, release_gate, archive_memory
```
