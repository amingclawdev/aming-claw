# Graph Delta Auto-Infer Proposal

> **Status:** IMPLEMENTED (base at 3e1bc9d; A4b validation deferred; runtime-failure diagnostic logging at 9200b87)

## Summary

The graph-delta auto-infer pipeline automatically infers acceptance graph node changes from dev task outputs. When a dev task completes, the auto-infer hook analyzes `changed_files`, `new_files`, and `related_nodes` to propose graph mutations (creates, updates, links) without requiring manual graph editing.

## Implementation History

| Milestone | Commit | Description |
|-----------|--------|-------------|
| A4a base  | 3e1bc9d | Auto-infer pipeline: dev→QA hook wired into auto_chain post-dev gate |
| A4b validation | — | Deferred: structural validation of proposed graph deltas before apply |
| Diagnostic logging | 9200b87 | Runtime-failure diagnostic logging for auto-infer pipeline errors |

## Architecture

### A4a: Auto-Infer Pipeline (dev → QA hook)

The auto-infer pipeline is triggered as a post-dev gate step in the auto-chain:

1. **Dev task completes** with `changed_files` and optional `graph_delta` in result JSON
2. **Auto-infer hook** runs before test stage dispatch:
   - If dev provided explicit `graph_delta`, validate and queue for apply
   - If no explicit delta, infer from `changed_files` + CODE_DOC_MAP:
     - New files not in any node → propose `creates` with inferred parent layer
     - Modified files with cross-node impact → propose `links`
     - Deleted files → propose node status updates
3. **QA stage** receives inferred delta as metadata for review
4. **Merge stage** applies accepted deltas to `acceptance-graph.md`

### A4b: Structural Validation (deferred)

Planned structural validation layer that would verify:
- No orphan node references in proposed deltas
- Layer hierarchy consistency (child nodes under correct parent)
- No circular dependency introduction
- verify_requires consistency

This layer is deferred pending real-world data from A4a pipeline runs.

## Revision History

| Rev | Date | Status |
|-----|------|--------|
| 1 | 2026-04-20 | DRAFT |
| 2 | 2026-04-21 | APPROVED WITH REQUIRED REVISIONS |
| 3 | 2026-04-23 | IMPLEMENTED (base at 3e1bc9d; A4b validation deferred; runtime-failure diagnostic logging at 9200b87) |
