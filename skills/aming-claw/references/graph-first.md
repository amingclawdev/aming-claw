# Graph-First Playbook

Use the active graph before inventing modules, moving files, or deciding ownership.

For target/user projects, graph-backed claims require a registered active graph.
For Aming Claw internals during MVP, `aming-claw://seed-graph-summary` is an
acceptable packaged navigation fallback when no active `aming-claw` graph
exists; use bounded file reads for exact code and do not overclaim node-level
evidence in that fallback mode.

## Minimum Discovery

1. Call `graph_status` for the active snapshot and graph stale state.
2. Call `graph_operations_queue` for pending scope reconcile, semantic jobs, review queue, and drift.
   If graph is stale, prefer direct Update graph (`/reconcile/pending-scope`
   with `activate=true`) over explicit pending-scope queueing.
3. Use `graph_query` before editing:
   - `query_schema` first, so the session learns the live tool list, valid `query_source`, and valid `query_purpose` values.
   - `find_node_by_path` to resolve a file path to graph node ids.
   - `search_structure` for module/title/file/function lookup when semantic payload is missing.
   - `function_index` to locate functions and line ranges from `metadata.function_lines`.
   - `degree_summary` for exact fan-in/fan-out and edge-type breakdown for a node.
   - `high_degree_nodes` to rank high fan-out/fan-in candidates for PR opportunity analysis.
   - `search_semantic` for node semantics, node metadata, and current edge semantic projection payloads.
   - `search_docs` for canonical docs and SOPs.
   - `get_node` for known L4/L7 nodes.
   - `get_neighbors` around the candidate owner node; pass `include_edge_semantic=true` when edge semantic payloads matter.
   - `get_file_excerpt` for targeted snippets when available.
4. Run `wf_impact` on candidate files before mutation.
5. Then inspect files directly.

Graph snapshots are commit-bound. Reconcile/build/update flows should use a
clean worktree. If dirty files exist, commit/stash unrelated work or switch to
an isolated clean worktree before claiming graph state reflects current code.

## Example Query Payloads

```json
{
  "project_id": "aming-claw",
  "tool": "query_schema",
  "query_source": "observer",
  "query_purpose": "prompt_context_build"
}
```

```json
{
  "project_id": "aming-claw",
  "tool": "find_node_by_path",
  "args": {"path": "agent/governance/graph_query_trace.py"},
  "query_source": "observer",
  "query_purpose": "prompt_context_build"
}
```

```json
{
  "project_id": "aming-claw",
  "tool": "function_index",
  "args": {"query": "traced_query", "limit": 5},
  "query_source": "observer",
  "query_purpose": "prompt_context_build"
}
```

```json
{
  "project_id": "aming-claw",
  "tool": "get_neighbors",
  "args": {"node_id": "L7.106", "direction": "both", "include_edge_semantic": true},
  "query_source": "observer",
  "query_purpose": "prompt_context_build"
}
```

```json
{
  "project_id": "aming-claw",
  "tool": "high_degree_nodes",
  "args": {"metric": "fan_out", "edge_types": ["depends_on"], "limit": 20},
  "query_source": "observer",
  "query_purpose": "prompt_context_build"
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
