---
status: archived
superseded_by: workflow-governance-architecture-v2.md
archived_date: 2026-04-05
historical_value: "Original workflow governance design concepts"
do_not_use_for: "governance design decisions"
---

# Workflow Governance Service — Design Document

## Origin
Requirements summarized from the toolBoxClient project development process. Governance scheme refined after multiple process violations.

## Problem Background

Recurring problems in AI Agent collaborative development:
1. **Acceptance graph status arbitrarily marked** — Dev marks verify:pass without running E2E
2. **Role overstepping** — Coordinator directly modifies code, Dev modifies acceptance graph
3. **Phase false completion** — stub/TODO marked as COMPLETED
4. **Process step skipping** — Skipping Gatekeeper and releasing directly
5. **Dev delivery untrustworthy** — Reports "modified" but files unchanged
6. **Rules rely on prompt constraints** — AI will ignore, forget, bypass

**Core conclusion**: Rules must be written in code, enforced by API, cannot rely on AI self-discipline.

## Architecture

```
workflow-governance (standalone service, port 30006)
  │
  ├── State Service (acceptance graph state management)
  │   ├── Data: acceptance-state.json (single source of truth)
  │   ├── Endpoints:
  │   │   ├── POST /api/wf/verify-update     — Update node status (requires role + evidence)
  │   │   ├── POST /api/wf/task-create       — Create task
  │   │   ├── POST /api/wf/task-update       — Update task status
  │   │   ├── POST /api/wf/gate-check        — Gatekeeper audit
  │   │   ├── GET  /api/wf/acceptance-graph   — Generate readable markdown (read-only view)
  │   │   ├── GET  /api/wf/node/:id          — Query single node
  │   │   ├── GET  /api/wf/summary           — Statistics summary
  │   │   └── POST /api/wf/release-gate      — Release gate (non-all-green = 403)
  │   └── Rules:
  │       ├── Status transition permission matrix (who can change what)
  │       ├── Evidence validation (T2-pass requires test output)
  │       └── Audit log automatic recording
  │
  ├── Memory Service (development memory store)
  │   ├── Data: memories.db (SQLite)
  │   ├── Endpoints:
  │   │   ├── POST /api/mem/write            — Write module memory
  │   │   ├── GET  /api/mem/query            — Query by module
  │   │   ├── GET  /api/mem/related?node=X   — Query by acceptance graph association
  │   │   ├── GET  /api/mem/pitfalls?module=X — Pitfall records
  │   │   └── GET  /api/mem/patterns?module=X — Design patterns
  │   └── Data model:
  │       ├── module_id: "stateService" / "agent.js"
  │       ├── category: "pattern" / "pitfall" / "decision" / "stub" / "api"
  │       ├── content: Memory content
  │       ├── related_nodes: ["L1.5", "L2.5"]
  │       └── created_by: "dev-agent-xxx"
  │
  └── Audit Service (operation audit)
      ├── Data: audit-log.json
      ├── Endpoints:
      │   ├── GET  /api/audit/log            — Query audit log
      │   ├── GET  /api/audit/violations     — Query violation records
      │   └── POST /api/audit/report         — Generate audit report
      └── Auto-recording: every state/memory operation
```

## Status Transition Permission Matrix

```
| Transition | Allowed Roles | Evidence Required |
|-----------|--------------|-------------------|
| pending → T2-pass | tester | test output (exit code + pass count) |
| T2-pass → pass | qa | E2E output (Playwright report) |
| pass → fail | any | Failure evidence (error log) |
| fail → pending | dev | Fix commit hash |
| pending → pass | Forbidden | Cannot skip T2-pass |
| any → manual edit | Forbidden | API is the only entry point |
```

## Role File Permissions

```
| File Type | PM | Dev | Tester | QA | Gatekeeper | Coordinator |
|-----------|----|----|--------|----|-----------:|------------|
| Source code | ❌ | ✅ | ❌ | ❌ | ❌ | ❌(<=2 times) |
| Test files | ❌ | ✅ | ✅read | ✅read | ❌ | ❌ |
| PRD | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Workflow rules | ❌ | ❌ | ❌ | ❌ | ❌ | ✅(after approval) |
| Acceptance graph state | ❌ | ❌ | ❌ | ❌ | ✅(via API) | ❌ |
| task-log | ❌ | ❌ | ❌ | ❌ | ❌ | ✅(via API) |
```

## Key Design Principles

1. **State separated from documentation** — acceptance-state.json is the single source of truth, acceptance-graph.md is an auto-generated read-only view
2. **AI calls API, does not edit files** — All state changes through HTTP API
3. **Evidence-driven** — Status transitions must include evidence (test output, E2E report)
4. **Role enforcement** — API validates caller role, unauthorized requests are rejected
5. **Audit traceable** — Every operation auto-recorded with who/when/what/evidence
6. **Independent deployment** — Governance service not in business project repo, AI Agent cannot tamper

## AI Agent Workflow Example

```
# After Dev completes task
Tester runs tests → 162/162 pass

# Coordinator calls state service
curl POST http://localhost:30006/api/wf/verify-update \
  -d '{"nodes":["L5.1","L5.2"], "status":"T2-pass",
       "role":"tester", "evidence":"162/162 pass, exit code 0"}'

# API validation:
#   ✅ tester has permission for pending → T2-pass
#   ✅ evidence contains pass count
#   → Write to acceptance-state.json
#   → Record audit-log
#   → Regenerate acceptance-graph.md

# QA runs E2E → 14/14 pass
curl POST http://localhost:30006/api/wf/verify-update \
  -d '{"nodes":["L5.1","L5.2"], "status":"pass",
       "role":"qa", "evidence":"14/14 E2E pass, Playwright report"}'

# Pre-release gate check
curl POST http://localhost:30006/api/wf/release-gate
# → 200 all green, release allowed
# → 403 has non-passing nodes, lists them
```

## Dev Memory Store Usage Example

```
# Dev writes memory after completing stateService module
curl POST http://localhost:30006/api/mem/write \
  -d '{"module":"stateService", "category":"pattern",
       "content":"HTTP CRUD + SSE broadcast, state via acceptance-state.json",
       "related_nodes":["L5.1","L5.2","L5.5"]}'

curl POST http://localhost:30006/api/mem/write \
  -d '{"module":"stateService", "category":"pitfall",
       "content":"cp command unreliable in worktree paths, use cat > as alternative",
       "related_nodes":["L5.1"]}'

# Next Dev queries before receiving related task
curl GET http://localhost:30006/api/mem/related?node=L5.3
# → Returns all stateService memories (pattern + pitfall)
```

## Integration with toolBoxClient

toolBoxClient calls governance service via HTTP:
- Coordinator: calls /api/wf/* to manage tasks and state
- Dev: calls /api/mem/query to get related memories, calls /api/mem/write after completion
- Tester: after running tests, Coordinator calls /api/wf/verify-update to submit evidence
- QA: after running E2E, Coordinator calls /api/wf/verify-update to submit evidence
- Release: calls /api/wf/release-gate to check if release is allowed

## Relationship with aming_claw Existing Capabilities

| aming_claw Existing | Governance Service Reuse |
|--------------------|------------------------|
| Multi-stage AI pipeline | Role division framework (PM→Dev→Test→QA) |
| Human-in-the-loop gate | Status transition approval mechanism |
| Git checkpoint/rollback | State rollback capability |
| Telegram driven | Notification and interaction channel |
| Workspace management | Multi-project governance |

## Implementation Priority

| Priority | Module | Description |
|----------|--------|-------------|
| P0 | State Service — verify-update + release-gate | Solve "arbitrary green marking" problem |
| P0 | Audit Service — basic audit log | Operation traceability |
| P1 | State Service — gate-check + task CRUD | Gatekeeper automation |
| P1 | Memory Service — write + query | Dev memory accumulation |
| P2 | Memory Service — related + pitfalls | Associative queries |
| P2 | Audit Service — violations + report | Violation detection |

## Changes Needed on toolBoxClient Side

1. Coordinator prompt: call /api/wf/* instead of directly editing md
2. Dev prompt: call /api/mem/query before task, call /api/mem/write after completion
3. pre-commit hook: reject direct edits to acceptance-state.json
4. acceptance-graph.md: change to auto-generated (git ignore or mark auto-generated)
