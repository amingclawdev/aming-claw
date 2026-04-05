# Documentation Migration Map

> Created: 2026-04-05 | Phase 1 Documentation Restructuring

This document maps all old file paths to their new locations after the Phase 1 documentation restructuring.

## Archived Documents (14 files)

Files moved to `docs/dev/archive/` with YAML frontmatter (`status: archived`).

| # | Old Path | New Path |
|---|----------|----------|
| 1 | `docs/architecture-v3-complete.md` | `docs/dev/archive/architecture-v3-complete.md` |
| 2 | `docs/architecture-v4-complete.md` | `docs/dev/archive/architecture-v4-complete.md` |
| 3 | `docs/architecture-v5-revised.md` | `docs/dev/archive/architecture-v5-revised.md` |
| 4 | `docs/architecture-v5-runtime.md` | `docs/dev/archive/architecture-v5-runtime.md` |
| 5 | `docs/architecture-v6-executor-driven.md` | `docs/dev/archive/architecture-v6-executor-driven.md` |
| 6 | `docs/architecture-v7-context-service.md` | `docs/dev/archive/architecture-v7-context-service.md` |
| 7 | `docs/workflow-governance-design.md` | `docs/dev/archive/workflow-governance-design.md` |
| 8 | `docs/workflow-governance-architecture-v2.md` | `docs/dev/archive/workflow-governance-architecture-v2.md` |
| 9 | `docs/session-runtime-design.md` | `docs/dev/archive/session-runtime-design.md` |
| 10 | `docs/scheduled-task-design.md` | `docs/dev/archive/scheduled-task-design.md` |
| 11 | `docs/telegram-project-binding-design.md` | `docs/dev/archive/telegram-project-binding-design.md` |
| 12 | `docs/toolbox-acceptance-graph.md` | `docs/dev/archive/toolbox-acceptance-graph.md` |
| 13 | `docs/p0-3-design.md` | `docs/dev/archive/p0-3-design.md` |
| 14 | `docs/production-guard.md` | `docs/dev/archive/production-guard.md` |

## Role Documents (5 files)

Files moved to `docs/roles/` with renamed filenames.

| # | Old Path | New Path |
|---|----------|----------|
| 15 | `docs/coordinator-rules.md` | `docs/roles/coordinator.md` |
| 16 | `docs/pm-rules.md` | `docs/roles/pm.md` |
| 17 | `docs/observer-rules.md` | `docs/roles/observer.md` |
| 18 | `docs/guide-dev-agent.md` | `docs/roles/dev.md` |
| 19 | `docs/guide-tester-qa.md` | `docs/roles/tester-qa.md` |

## Governance Documents (3 files)

Files moved to `docs/governance/`.

| # | Old Path | New Path |
|---|----------|----------|
| 20 | `docs/aming-claw-acceptance-graph.md` | `docs/governance/acceptance-graph.md` |
| 21 | `docs/design-spec-memory-coordinator-executor.md` | `docs/governance/design-spec-full.md` |
| 22 | `docs/prd-memory-coordinator-executor.md` | `docs/governance/prd-full.md` |

## API Documents (2 files)

Files moved to `docs/api/`.

| # | Old Path | New Path |
|---|----------|----------|
| 23 | `docs/ai-agent-integration-guide.md` | `docs/api/governance-api.md` |
| 24 | `docs/executor-api-guide.md` | `docs/api/executor-api.md` |

## Phase 2 Merge Candidates (3 files)

Files with redirect stubs pending Phase 2 consolidation.

| # | File | Status |
|---|------|--------|
| 25 | `docs/guide-coordinator.md` | Phase 2 merge into `docs/roles/coordinator.md` |
| 26 | `docs/observer-feature-guide.md` | Phase 2 merge into `docs/roles/observer.md` |
| 27 | `docs/human-intervention-guide.md` | Phase 2 merge into appropriate role doc |

## New Index Files (5 files)

| File | Purpose |
|------|---------|
| `docs/roles/README.md` | Role documentation index |
| `docs/governance/README.md` | Governance documentation index |
| `docs/api/README.md` | API documentation index |
| `docs/config/README.md` | Configuration documentation index |
| `docs/dev/migration-map.md` | This file — migration path reference |

## Summary

- **Total mappings**: 27 (14 archived + 5 roles + 3 governance + 2 API + 3 Phase 2)
- **Redirect stubs created**: 27 at original locations
- **New files created**: 5 index/map files
- **Total files touched**: 59
