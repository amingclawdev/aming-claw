---
name: aming-claw-hn-demo
description: Guided operator for the full Aming Claw HN three-fear demo. Use when a user asks to run, preview, present, or collect evidence for the HN demo covering before-work project understanding, during-work subagent observability, and after-work docs/tests/config drift.
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

# Aming Claw HN Demo

Run this as a guided operator flow, not a mandatory replay engine. Prefer
deterministic dashboard, MCP, git, and fixture evidence. Do not require a live
AI provider for the demo.

## Guardrails

- Do not silently start services. If governance is offline, tell the user to
  run `aming-claw start` in a separate terminal.
- Use governance on `http://127.0.0.1:40000`; the dashboard is
  `http://127.0.0.1:40000/dashboard`.
- Check or ask for the target `project_id` before using project-scoped
  dashboard links. If no target project exists and the user asked to run or
  preview the HN demo, use the isolated demo fixture path below instead of
  asking the user to invent a project id.
- Do not mutate a user's real project by default. Use read-only evidence unless
  the user explicitly asks for a governed action.
- Creating the isolated HN demo fixture is allowed for this skill: it writes a
  generated project under the OS temp directory, bootstraps that fixture through
  governance, and leaves the user's active app untouched.
- If browser automation is available, open the dashboard and capture
  screenshots of each case. Otherwise provide exact links and ask the user to
  capture screenshots.
- Treat screenshots as evidence references: record filename, view, project id,
  and what claim the screenshot supports.

## Operator Flow

1. Baseline runtime:
   - Check `runtime_status`, `graph_status`, and `graph_operations_queue` when
     MCP is available.
   - If MCP is unavailable, check `GET /api/health` and provide the dashboard
     link; say MCP is not loaded in this session.
   - If dashboard assets are missing, say the demo cannot show dashboard
     evidence until assets exist or the dashboard build runs.
   - If `/api/projects` is empty, or the user has not selected a real project,
     run `node frontend/dashboard/scripts/e2e-hn-demo.mjs --ensure-fixture --no-browser`
     from the Aming Claw plugin checkout or installed plugin payload. This
     runner is packaged with the plugin and does not require a dashboard npm
     install for the `--no-browser` path. Use the returned
     `project_id="aming-claw-hn-demo"` for dashboard links.
2. Run the three cases in order:
   - Before work: use `aming-claw-hn-demo-before-work`.
   - During work: use `aming-claw-hn-demo-during-work`.
   - After work: use `aming-claw-hn-demo-after-work`.
3. For each case, collect:
   - fear being addressed;
   - dashboard or MCP views inspected;
   - screenshots or links;
   - architecture reason;
   - any limitation, such as offline services or missing fixture data.
4. End with a compact evidence index.

## Suggested Dashboard Links

Use these with `project_id=<id>` when known:

- Projects: `/dashboard?project_id=<id>&view=projects`
- Graph: `/dashboard?project_id=<id>&view=graph`
- Backlog: `/dashboard?project_id=<id>&view=backlog`
- Operations Queue: `/dashboard?project_id=<id>&view=operations`
- Review Queue: `/dashboard?project_id=<id>&view=review`
- Asset Inbox: `/dashboard?project_id=<id>&view=assets`

If a view slug differs in the current dashboard, navigate from the dashboard
sidebar and record the actual URL.

## Evidence Summary

```text
HN demo evidence
- Runtime: governance=<ok/offline>, dashboard=<ok/missing>, MCP=<ok/missing>, project_id=<id>
- Before work: graph=<link/screenshot>, backlog=<link/screenshot>, fence=<evidence>, result=<claim>
- During work: timeline=<link/screenshot>, lanes=<link/screenshot>, gate=<evidence>, result=<claim>
- After work: asset_inbox=<link/screenshot>, drift=<link/screenshot>, review_queue=<link/screenshot>, result=<claim>
- Limitations: <offline services, missing fixture, manual screenshots, no live AI provider needed>
```
