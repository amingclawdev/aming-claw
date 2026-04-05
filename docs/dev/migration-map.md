# Documentation Migration Map

> Created: 2026-04-05 | Phase 1 Documentation Restructuring

This document maps all old file paths to their new locations after the Phase 1 documentation restructuring.

## Archived Documents (14 files)

Files moved to `docs/dev/archive/` with YAML frontmatter (`status: archived`).
Redirect stubs at old paths have been **deleted** (no longer needed).

| # | Old Path | New Path | Stub Status |
|---|----------|----------|-------------|
| 1 | `docs/architecture-v3-complete.md` | `docs/dev/archive/architecture-v3-complete.md` | deleted |
| 2 | `docs/architecture-v4-complete.md` | `docs/dev/archive/architecture-v4-complete.md` | deleted |
| 3 | `docs/architecture-v5-revised.md` | `docs/dev/archive/architecture-v5-revised.md` | deleted |
| 4 | `docs/architecture-v5-runtime.md` | `docs/dev/archive/architecture-v5-runtime.md` | deleted |
| 5 | `docs/architecture-v6-executor-driven.md` | `docs/dev/archive/architecture-v6-executor-driven.md` | deleted |
| 6 | `docs/architecture-v7-context-service.md` | `docs/dev/archive/architecture-v7-context-service.md` | deleted |
| 7 | `docs/workflow-governance-design.md` | `docs/dev/archive/workflow-governance-design.md` | deleted |
| 8 | `docs/workflow-governance-architecture-v2.md` | `docs/dev/archive/workflow-governance-architecture-v2.md` | deleted |
| 9 | `docs/session-runtime-design.md` | `docs/dev/archive/session-runtime-design.md` | deleted |
| 10 | `docs/scheduled-task-design.md` | `docs/dev/archive/scheduled-task-design.md` | deleted |
| 11 | `docs/telegram-project-binding-design.md` | `docs/dev/archive/telegram-project-binding-design.md` | deleted |
| 12 | `docs/toolbox-acceptance-graph.md` | `docs/dev/archive/toolbox-acceptance-graph.md` | deleted |
| 13 | `docs/p0-3-design.md` | `docs/dev/archive/p0-3-design.md` | deleted |
| 14 | `docs/production-guard.md` | `docs/dev/archive/production-guard.md` | deleted |

## Role Documents (5 files)

Files moved to `docs/roles/` with renamed filenames.
Redirect stubs at old paths have been **deleted** (no longer needed).

| # | Old Path | New Path | Stub Status |
|---|----------|----------|-------------|
| 15 | `docs/coordinator-rules.md` | `docs/roles/coordinator.md` | deleted |
| 16 | `docs/pm-rules.md` | `docs/roles/pm.md` | deleted |
| 17 | `docs/observer-rules.md` | `docs/roles/observer.md` | deleted |
| 18 | `docs/guide-dev-agent.md` | `docs/roles/dev.md` | deleted |
| 19 | `docs/guide-tester-qa.md` | `docs/roles/tester-qa.md` | deleted |

## Governance Documents (3 files)

Files moved to `docs/governance/`.
Redirect stubs at old paths have been **deleted** (no longer needed).

| # | Old Path | New Path | Stub Status |
|---|----------|----------|-------------|
| 20 | `docs/aming-claw-acceptance-graph.md` | `docs/governance/acceptance-graph.md` | deleted |
| 21 | `docs/design-spec-memory-coordinator-executor.md` | `docs/governance/design-spec-full.md` | deleted |
| 22 | `docs/prd-memory-coordinator-executor.md` | `docs/governance/prd-full.md` | deleted |

## API Documents (2 files)

Files moved to `docs/api/`.
Redirect stubs at old paths have been **deleted** (no longer needed).

| # | Old Path | New Path | Stub Status |
|---|----------|----------|-------------|
| 23 | `docs/ai-agent-integration-guide.md` | `docs/api/governance-api.md` | deleted |
| 24 | `docs/executor-api-guide.md` | `docs/api/executor-api.md` | deleted |

## Phase 2 Merge Candidates (3 files)

Redirect stubs have been **deleted**. Content already merged into target docs.

| # | File | Target | Stub Status |
|---|------|--------|-------------|
| 25 | `docs/guide-coordinator.md` | `docs/roles/coordinator.md` | deleted |
| 26 | `docs/observer-feature-guide.md` | `docs/roles/observer.md` | deleted |
| 27 | `docs/human-intervention-guide.md` | appropriate governance doc | deleted |

## Superseded Documents (2 files)

Files fully superseded by canonical docs and **deleted**.

| # | Old Path | Superseded By | Status |
|---|----------|---------------|--------|
| 28 | `docs/deployment-guide.md` | `docs/deployment.md` | deleted |
| 29 | `docs/roles/tester-qa.md` | `docs/roles/tester.md` + `docs/roles/qa.md` | deleted |

## New Index Files (5 files)

| File | Purpose |
|------|---------|
| `docs/roles/README.md` | Role documentation index |
| `docs/governance/README.md` | Governance documentation index |
| `docs/api/README.md` | API documentation index |
| `docs/config/README.md` | Configuration documentation index |
| `docs/dev/migration-map.md` | This file — migration path reference |

## Summary

- **Total mappings**: 29 (14 archived + 5 roles + 3 governance + 2 API + 3 Phase 2 + 2 superseded)
- **Redirect stubs deleted**: All 29 stubs removed (no longer needed after transition period)
- **New files created**: 5 index/map files
- **Total files touched**: 59
