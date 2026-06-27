---
name: aming-claw-hn-demo-before-work
description: HN demo case for the fear that AI does not understand project structure and duplicates work. Guides evidence collection for graph-first discovery, backlog contract, target file fence, and acceptance criteria before implementation.
---

## REQUIRED FIRST READ

Before any response that uses this skill, in this exact order:

  ListMcpResourcesTool()
  ReadMcpResourceTool(uri="aming-claw://current-context")
  ReadMcpResourceTool(uri="aming-claw://skill")
  ReadMcpResourceTool(uri="aming-claw://graph-first")

current-context anchors project_id, governance URLs, and 3 guardrails.
skill is the operating contract (Start Sequence, Observer Operating Modes).
graph-first has copy-pasteable graph_query payload examples.

Common failures when these are skipped:
- Bootstrapping the wrong project (workspace_match auto-detected aming-claw)
- Calling task_create dev/pm (V1 default is observer-led mf_parallel.v1)
- Using Grep on the aming-claw codebase instead of graph_query
- Fabricating trace_id strings (audit ledger is server-resolvable, will fail)
- Running Execution Supervisor mode by default (Design Alignment is default)

# HN Demo: Before Work

Show how Aming Claw turns "the AI will grep blindly and duplicate work" into a
bounded, auditable start condition.

## Fear

AI does not understand the project structure, misses existing modules, and
creates duplicate work.

## Evidence To Collect

- Graph discovery: node, file, function, or neighbor evidence for the target
  area before reading broad source files.
- Backlog duplicate/overlap probe: real `backlog_list` or governance API
  response for the proposed work title/files before `backlog_upsert`, recorded
  exactly with `count`, `bugs`, and `request_id`.
- Backlog contract: row with title, details, target files, tests, required
  docs, and acceptance criteria.
- Target file fence: exact files or worktree boundary assigned to the worker.
- Acceptance criteria: concrete conditions the implementation must satisfy.

## Architecture Reason

- Commit-bound graph: graph evidence is tied to a known commit, not dirty local
  guesses.
- Graph-first discovery: the operator starts from structure, ownership, and
  neighbors before patching.
- Backlog contract: scope, acceptance criteria, and evidence obligations live
  in the work ledger.

## Synthetic Data Setup (only if data does not exist)

If task_timeline_list / backlog_list returns empty for the demo project, you are
CREATING demo data, not reading existing data. Mandatory rules:

1. DO NOT call task_create with type=pm/dev/test/qa/merge. That is the chain
   path. V1 default is observer-led mf_parallel.v1.

2. Write parallel_contract into backlog.chain_trigger_json via backlog_upsert.
   workers[] is an array; for parallel work include multiple workers with
   DISJOINT owned_files.

3. Tie every task_timeline_append to the same mf_id (MF-<BACKLOG-ID>).
   Per-worker events use the worker's task_id; observer events can use
   parent_task_id.

4. For each mf_sub graph_query: query_source="mf_subagent" + the worker's
   task_id, parent_task_id, worker_role="mf_sub", fence_token as top-level
   params.

5. Capture the returned trace_id and write into payload.graph_query_trace_ids
   in the timeline event. NEVER fabricate trace_id strings -- anyone can GET
   /api/graph-governance/<pid>/query-traces/<trace_id> to verify.

6. mf_type=chain_rescue in mf_timeline_precheck output is the MVP MF storage
   bucket label, not an error. See aming-claw://mf-sop.

## Role and Mode

This subskill inherits the observer-mode operator role, mode boundaries, and
acceptance criteria from the umbrella `aming-claw-hn-demo` skill loaded by the
Skill tool.
Do not invent an `aming-claw://skill-hn-demo` MCP resource.

This subskill covers only Before Work operator steps for graph-first discovery,
backlog duplicate/overlap probing, backlog contract evidence, target fence
evidence, and acceptance criteria. Use Design Alignment by default; enter
Execution Supervisor only when the umbrella mode gate is explicitly satisfied.

## Operator Steps

1. Check governance and dashboard status. If governance is offline, instruct
   the user to run `aming-claw start`; do not start it silently.
2. Confirm the project and graph commit with `graph_status` or the dashboard.
3. Inspect the Graph view for the target area. Prefer node inspector, related
   files, functions, and neighbors over broad source search.
4. Run a real backlog duplicate/overlap probe for the proposed work title and
   target files before creating or updating the backlog row. Record the exact
   governance response body, including `count`, `bugs`, and `request_id`.
5. Inspect or create the Backlog row. Verify target files and acceptance
   criteria are present before implementation.
6. Inspect or state the fence: branch/worktree, owned files, base commit, and
   any merge queue/fence token if this is a subagent demo.
7. Capture screenshots or links for Graph, Backlog, and fence evidence.

## Evidence Summary

```text
Before-work evidence
- Fear: project structure misunderstanding and duplicate work
- Graph: <snapshot/link/screenshot/node evidence>
- Backlog duplicate/overlap probe: <exact governance response/request_id>
- Backlog contract: <bug id/link/screenshot>
- Fence: <branch/worktree/files/base commit>
- Acceptance criteria: <summary or link>
- Architecture reason: commit-bound graph + graph-first discovery + backlog contract
- Limitations: <none/offline dashboard/manual screenshot/etc>
```
