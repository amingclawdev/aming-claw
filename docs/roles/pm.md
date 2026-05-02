# PM Role Specification

> **2026-04-07 (B10):** Dev tasks now fail fast on worktree creation failure. PM PRDs should account for potential worktree retry scenarios in acceptance criteria.

> **2026-04-11 (B24):** Chain integrity verification — PM `doc_impact.files` are now included in the retry scope constraint. Dev retries receive the full allowed file list via `get_retry_scope()` so gate rejections for missing doc updates are avoided.

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

## Graph-delta declarations (required when AC implies file changes)

When the acceptance criteria contain delete-keywords (`DELETE`, `remove`, `replaces`, `replaced_by` — case-insensitive substring match) AND `target_files` is non-empty, the PM PRD MUST populate the graph-delta declaration fields. These declarations let the dev-stage graph-delta auto-inferrer avoid emitting phantom `creates` for nodes/files the PM marked as removed.

### Fields

| Field | Description | Example |
|-------|-------------|---------|
| `removed_nodes` | List of acceptance-graph node_ids the PR will delete | `["L7.21"]` |
| `unmapped_files` | List of file paths whose owning nodes should be unmapped/removed | `["agent/legacy/old.py"]` |
| `renamed_nodes` | Optional list of `{"from": "L7.X", "to": "L7.Y"}` for renamed nodes | `[{"from": "L7.5", "to": "L7.5b"}]` |
| `remapped_files` | Optional list of `{"from": "old/path.py", "to": "new/path.py"}` for moved files | `[{"from": "agent/old.py", "to": "agent/new.py"}]` |

### Worked example (single-file deletion PR)

```json
{
  "target_files": ["agent/legacy/migration_state_machine.py"],
  "acceptance_criteria": [
    "DELETE agent/legacy/migration_state_machine.py",
    "all imports of MigrationStateMachine removed"
  ],
  "verification": "python -m pytest agent/tests/test_migration_removal.py -v",
  "removed_nodes": ["L7.21"],
  "unmapped_files": ["agent/legacy/migration_state_machine.py"]
}
```

### Server-side enforcement

The post-PM transition runs `validate_pm_output` (see `agent/governance/output_schemas/pm_result_schema.py`). PM tasks whose `acceptance_criteria` contain `DELETE`/`remove`/`replaces`/`replaced_by` keywords AND non-empty `target_files` but empty `removed_nodes` AND empty `unmapped_files` are blocked at the gate with `MISSING_DECLARATION_FOR_DELETED_FILE`. The validator runs in `mode='warn'` by default (controlled by `OPT_PREFLIGHT_VALIDATOR_MODE`); observer emergency bypass via `metadata.observer_emergency_bypass` + `bypass_reason` short-circuits the check identically to the dev-side validator.

The keyword scan is case-insensitive simple substring matching only (no LLM, no regex backreferences) — see the keyword tuple literal in `pm_result_schema.py`.

## Reconcile cluster audit pattern

When the PM task metadata carries `operation_type == "reconcile-cluster"`, the request originates from a reconcile-driven standard-chain audit (proposal §4.4). The PM prompt switches to the cluster-audit contract instead of the normal PRD flow.

### Inputs

- `metadata.cluster_payload` — raw cluster definition (cluster_id, candidate_nodes, file scope)
- `metadata.cluster_report` — `ClusterReport` carrying `purpose`, `candidate_nodes`, `expected_test_files`, `expected_doc_sections`

PM MUST read both before drafting the PRD. Do not widen scope beyond what these two payloads describe.

### Output rules

1. `proposed_nodes` mirrors `cluster_payload.candidate_nodes` one-for-one with **every `node_id` set to `null`**. The downstream auto-inferrer Rule J + the ID allocator assign concrete IDs during dev-stage processing — PM never invents node IDs in reconcile-cluster mode.
2. **Always-bootstrap:** the PRD MUST NOT declare `removed_nodes` and MUST NOT declare `unmapped_files`. The cluster-audit contract is purely additive. The post-PM `MISSING_DECLARATION_FOR_DELETED_FILE` rule does not apply because `acceptance_criteria` should NOT contain delete-keywords (`DELETE`, `remove`, `replaces`, `replaced_by`).
3. `acceptance_criteria` reflect the `ClusterReport` contract: each criterion references `purpose`, lists every entry from `expected_test_files`, and lists every entry from `expected_doc_sections`. Criteria must be concretely testable (substring scan, file-exists check, pytest-runnable assertion).

### Worked example

```json
{
  "metadata": {"operation_type": "reconcile-cluster", "cluster_id": "cluster-foo-7"},
  "feature": "Reconcile cluster audit — cluster-foo-7",
  "target_files": ["agent/foo/bar.py", "docs/modules/foo.md"],
  "proposed_nodes": [
    {"node_id": null, "title": "foo.bar audit anchor", "parent_layer": "L7", "primary": "agent/foo/bar.py"}
  ],
  "acceptance_criteria": [
    "ClusterReport.purpose is documented in docs/modules/foo.md",
    "Test file agent/tests/test_foo_bar_audit.py exists and covers expected_test_files entries",
    "Doc section '## Foo audit' present in docs/modules/foo.md per expected_doc_sections"
  ],
  "verification": "python -m pytest agent/tests/test_foo_bar_audit.py -v"
}
```

In the example above, `proposed_nodes` deliberately uses `node_id=null`. The dev stage's auto-inferrer Rule J + the allocator handle ID assignment after dev runs; PM never speculates IDs.

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
