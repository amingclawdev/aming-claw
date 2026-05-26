---
name: aming-claw-hn-demo-after-work
description: HN demo case for the fear that code changes leave docs, tests, and config stale after implementation. Guides evidence collection for Asset Inbox, binding state, Baseline and Possible drift, Review Queue, impact scope, and review boundaries.
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

# HN Demo: After Work

Show how Aming Claw keeps docs, tests, and config from becoming invisible
collateral damage after code changes.

## Fear

Code changes land, but related docs, tests, or config are stale, orphaned, or
accepted as true without review.

## Evidence To Collect

- Asset Inbox: changed, orphaned, candidate, or bound doc/test/config assets.
- Binding state: accepted, candidate, orphan, unbound, or source-controlled
  hint state.
- Baseline/Possible drift: whether related assets are known clean, suspected,
  or pending impact review.
- Review Queue: proposal review boundary for AI or weak-evidence changes.

## Architecture Reason

- Asset inventory records docs/tests/config as commit-bound project assets.
- Binding projection separates candidate relationships from trusted graph
  ownership.
- Impact scope flags related assets that may need review after source changes.
- Drift status distinguishes baseline, suspected, possible, and resolved
  states.
- Review boundary keeps weak AI or path evidence out of graph truth until
  accepted.

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

## Observer Mode Reminder

This skill is Design Alignment Mode by default: scope, design contract,
dispatch, STOP. Do not append implementation/verification/close_ready events
yourself unless the user explicitly said one of: "推进实施", "进入执行模式",
"监视任务完成", "我睡了你接管", or equivalent Execution Supervisor trigger
phrase.

For demos that need to populate timeline events showing the gate flow, declare
"entering Execution Supervisor for demo populate" explicitly before doing it.

## Operator Steps

1. Check governance and dashboard status. If governance is offline, instruct
   the user to run `aming-claw start`; do not start it silently.
2. Open Asset Inbox for the project and active graph snapshot.
3. Filter or inspect docs, tests, and config related to the demo change.
4. Identify binding state and drift status. Do not treat candidate or weak path
   matches as trusted graph ownership.
5. Open Review Queue for pending binding, unbind, semantic, or impact review
   proposals.
6. Capture screenshots or links for Asset Inbox, binding details, drift status,
   and Review Queue.

## Evidence Summary

```text
After-work evidence
- Fear: docs/tests/config become stale after code changes
- Asset Inbox: <link/screenshot/asset ids>
- Binding state: <accepted/candidate/orphan/unbound/hint evidence>
- Drift: <baseline/possible/suspected/impact_pending/resolved evidence>
- Review Queue: <link/screenshot/proposal ids>
- Architecture reason: asset inventory + binding projection + impact scope + drift status + review boundary
- Limitations: <none/offline dashboard/manual screenshot/etc>
```
