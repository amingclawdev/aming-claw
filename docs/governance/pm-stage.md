# PM Stage: Graph-Delta Declarations

## Overview

The PM stage can declare explicit graph-delta intent via 4 optional fields in the PRD result.
These declarations take **priority over the auto-inferrer** — files and nodes covered by
declarations skip heuristic inference. The auto-inferrer only fills gaps for undeclared files.

## Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `removed_nodes` | `list[str]` | `[]` | Node IDs that the PM intends to remove from the graph |
| `unmapped_files` | `list[str]` | `[]` | Files that should be excluded from graph-binding validation |
| `renamed_nodes` | `list[dict]` | `[]` | Nodes being renamed (each entry: `{old_id, new_id}`) |
| `remapped_files` | `list[str]` | `[]` | Files whose graph binding is changing (skip auto-inferrer) |

All fields default to empty lists. PRDs without these fields behave identically to
the previous behavior (backward compatible).

## Priority Order

1. **PM declaration** — `removed_nodes`, `unmapped_files`, `renamed_nodes`, `remapped_files`
2. **Auto-inferrer** — Rules A through J fill gaps for undeclared files only

When both a declaration and the auto-inferrer apply to the same file or node, the
declaration wins. An audit event `graph_delta.declaration_overrides_inference` is emitted
recording both the declared and inferred operations.

## When to Declare

- **Deleting a graph-bound file**: Add the node to `removed_nodes`
- **Moving a file to a different node**: Add the file to `remapped_files`
- **Renaming a node**: Add entry to `renamed_nodes`
- **Temporary/scratch files**: Add to `unmapped_files` to skip validation

## Validation

The QA gate (`_gate_qa_pass`) validates declarations:

- `removed_nodes` entries must correspond to actually-deleted files in `dev_changed_files`
- Graph-bound files in `dev_changed_files` must be declared in `removed_nodes` or `unmapped_files`
- Validation is skipped entirely when no declaration fields are present (backward compat)

## Audit Events

| Event | When |
|---|---|
| `graph_delta.declaration_overrides_inference` | Auto-inferrer would have produced a different op than the PM declaration |

## Example

```json
{
  "proposed_nodes": [{"node_id": "L3.5", "title": "New API", "primary": ["agent/api.py"]}],
  "removed_nodes": ["L3.2"],
  "unmapped_files": ["agent/scratch/temp.py"],
  "renamed_nodes": [],
  "remapped_files": ["agent/old_api.py"]
}
```

In this example:
- `L3.2` will be removed (declaration takes priority over any inferrer create)
- `agent/scratch/temp.py` is excluded from graph-binding validation
- `agent/old_api.py` is excluded from auto-inferrer heuristics
- `L3.5` is proposed as a new node via standard `proposed_nodes`
