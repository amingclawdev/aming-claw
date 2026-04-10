# Governance Documentation

This directory contains governance-related documentation for the Aming Claw project.

## Specifications

| File | Description |
|------|-------------|
| [acceptance-graph.md](acceptance-graph.md) | Acceptance graph: verification nodes and criteria |
| [design-spec-full.md](design-spec-full.md) | Design specification: memory, coordinator, executor |
| [prd-full.md](prd-full.md) | Product requirements: memory, coordinator, executor |

## Processes

| File | Description |
|------|-------------|
| [implementation-process.md](implementation-process.md) | Document lifecycle: proposal → plan → execution record |
| [manual-fix-sop.md](manual-fix-sop.md) | Manual fix standard operating procedure (v3) |
| [version-control.md](version-control.md) | Version gate and chain_version lifecycle |
| [audit-process.md](audit-process.md) | Chain full-process audit procedure |

## Rules & Policies

| File | Description |
|------|-------------|
| [auto-chain.md](auto-chain.md) | Auto-chain dispatch: PM→Dev→Test→QA→Merge pipeline |
| [gates.md](gates.md) | Gate definitions: checkpoint, t2_pass, qa_pass, release |
| [conflict-rules.md](conflict-rules.md) | Task conflict detection: 5-rule engine |
| [memory.md](memory.md) | Memory backend: FTS5 + semantic search |

## Current Status

**Quick links for new sessions:**

| What | Where |
|------|-------|
| **Session handoff** | [docs/dev/session-status.md](../dev/session-status.md) |
| **Bug backlog** | [docs/dev/bug-and-fix-backlog.md](../dev/bug-and-fix-backlog.md) |
| **Active execution** | [docs/dev/current-graph-doc-2026-04-06.md](../dev/current-graph-doc-2026-04-06.md) |
| **Graph health** | 29 nodes, 905 tests pass — run `mcp__aming-claw__preflight_check` |

## Active Plans

| Plan | Status | Execution Record | Next Step |
|------|--------|-----------------|-----------|
| [plan-graph-driven-doc.md](plan-graph-driven-doc.md) | Step 2 ✅ | [current-graph-doc-2026-04-06](../dev/current-graph-doc-2026-04-06.md) | Step 3: Level 1 changes |
