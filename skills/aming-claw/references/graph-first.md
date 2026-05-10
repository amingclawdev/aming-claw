# Graph-First Playbook

Use the active graph before inventing modules, moving files, or deciding ownership.

## Minimum Discovery

1. Call `graph_status` for the active snapshot and graph stale state.
2. Call `graph_operations_queue` for pending scope reconcile, semantic jobs, review queue, and drift.
3. Use `graph_query` before editing:
   - `search_semantic` for concepts such as `dashboard semantic drift`, `MCP host ops`, or `language adapter`.
   - `search_docs` for canonical docs and SOPs.
   - `get_node` for known L4/L7 nodes.
   - `get_neighbors` around the candidate owner node.
   - `get_file_excerpt` for targeted snippets when available.
4. Run `wf_impact` on candidate files before mutation.
5. Then inspect files directly.

## Example Query Payloads

```json
{
  "project_id": "aming-claw",
  "tool": "search_semantic",
  "args": {"query": "dashboard semantic drift operations queue", "limit": 10},
  "query_purpose": "implementation_owner_discovery"
}
```

```json
{
  "project_id": "aming-claw",
  "tool": "get_neighbors",
  "args": {"node_id": "L7.106", "depth": 1},
  "query_purpose": "reuse_existing_reconcile_modules"
}
```

## Reuse Rule

If the graph shows an existing owner, adapter, queue, or API contract, extend that owner first. Create a new module only after recording why the existing graph-owned surface does not fit.

## Dashboard Notes

For dashboard work, always check:

- active graph commit vs HEAD;
- `current_state.graph_stale`;
- `current_state.semantic_snapshot`;
- `current_state.semantic_drift`;
- operations queue rows for `scope_reconcile`, `node_semantic`, `edge_semantic`, and feedback/review work.
