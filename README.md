# Aming Claw

> AI Workflow Governance Platform — Monitor and take over AI task execution through the Observer pattern

## Your AI Supervisor

Any Claude Code session can act as an **Observer** to supervise your project. The Observer doesn't write code — it makes decisions: submitting work, watching the auto-chain execute, and stepping in only when things go wrong.

For a full architectural overview, see [Architecture](docs/architecture.md).

---

## Session Entry Point

**New session? Start here →** [docs/dev/session-status.md](docs/dev/session-status.md) — Current system state, active work, and next steps.

**Project map →** [docs/governance/feature-index.md](docs/governance/feature-index.md) — Generated reconcile index of feature nodes, owned code, linked docs, linked tests, and remaining coverage debt.

## Get Started

1. **[Architecture](docs/architecture.md)** — System design, service topology, and data flow
2. **[Deployment](docs/deployment.md)** — Installation, Docker setup, and service startup
3. **[Observer Onboarding](docs/roles/observer.md)** — How to operate as an Observer

---

## Deep Dive

### Roles

| Role | Guide |
|------|-------|
| Observer | [docs/roles/observer.md](docs/roles/observer.md) |
| Coordinator | [docs/roles/coordinator.md](docs/roles/coordinator.md) |
| PM | [docs/roles/pm.md](docs/roles/pm.md) |
| Dev | [docs/roles/dev.md](docs/roles/dev.md) |
| Tester | [docs/roles/tester.md](docs/roles/tester.md) |
| QA | [docs/roles/qa.md](docs/roles/qa.md) |
| Gatekeeper | [docs/roles/gatekeeper.md](docs/roles/gatekeeper.md) |

See [Roles Overview](docs/roles/README.md) for the full permission matrix.

### Governance

- [Auto-Chain](docs/governance/auto-chain.md) — Multi-stage task pipeline (PM → Dev → Test → QA → Merge)
- [Gates](docs/governance/gates.md) — Gate validation between pipeline stages
- [Acceptance Graph](docs/governance/acceptance-graph.md) — DAG-based verification tracking
- [Memory](docs/governance/memory.md) — Development memory backend and search
- [Conflict Rules](docs/governance/conflict-rules.md) — Task conflict detection and resolution

See [Governance Overview](docs/governance/README.md) for the full governance reference.

### Config & API

- [Configuration Reference](docs/config/README.md) — All configuration schemas
- [Governance API](docs/api/governance-api.md) — Task, workflow, and audit endpoints
- [Executor API](docs/api/executor-api.md) — Execution monitoring and control

See [API Overview](docs/api/README.md) for the full API reference.

### Development

- [Design Spec](docs/governance/design-spec-full.md) — Full design specification
- [PRD](docs/governance/prd-full.md) — Product requirements document
- [Roadmap](docs/dev/roadmap.md) — Development roadmap and iteration plans

---

## Changelog

- **2026-04-05**: Phase 3 documentation restructuring — README as navigation hub, config schema docs, CODE_DOC_MAP update
- **2026-03-28**: M3-M6 Gate enhancements (skip_doc_check guard, version-update validation, QA dedup)
- **2026-03-28**: M1+M2 Task ownership validation + observer override audit
- **2026-03-28**: Phase 8 Chain Context — event-sourced chain runtime context, crash recovery, retry prompt fallback
- **2026-03-26**: Auto-chain fully wired. PM → Dev → Test → QA → Merge → Deploy runs end-to-end with gate validation
- **2026-03-26**: Old Telegram bot system fully removed. Unified on Governance API. Observer pattern is now the primary interaction model
