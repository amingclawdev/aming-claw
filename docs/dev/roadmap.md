# Aming-Claw Roadmap

> Baseline: `4cc1688` (2026-04-04, post-reconciliation)
> Revision: v3 (baseline reconciliation complete, Layer 0/1 status updated)
> Previous: `docs/dev/roadmap-2026-03-31-archived.md`

---

## 0. Guiding Principles

1. **Stop bleeding before abstracting** -- no platform work until current defects are fixed
2. **Consolidate before parallelizing** -- no fan-out/fan-in until contracts are unified
3. **Opt-in before global replacement** -- graph routing, observer escalation, validator blocking all start as opt-in
4. **Prove reuse before platforming** -- bootstrap, packaging, multi-project built on validated patterns, not speculation
5. **Measure stability, not neatness** -- success = task success rate, not "files touched per role change"
6. **Governance semantics must beat file heuristics** -- a task may touch the right file and still be wrong if it changes policy outside contract

---

## 1. Current State (2026-04-04)

### 1.1 Runtime Topology

```
Observer / Claude Code session
        |
        v
  localhost:40000  -->  host governance server (Python, SQLite)
        |
        +-->  host service_manager
        |       +-->  host executor_worker
        |               +-->  Claude CLI (per-task AI session)
        |
        +-->  Docker: telegram-gateway
        +-->  Docker: Redis (pub/sub + distributed lock)
        +-->  Docker: DBService (Node.js, vector search)
```

### 1.2 Workflow Chain (Contract-Driven)

```
PM --> Dev --> Test --> QA --> Gatekeeper --> Merge --> Deploy
```

- Routing: hardcoded CHAIN map in auto_chain.py:93-101
- Gates: validate contract fields (target_files, test_report, recommendation)
- Node graph: exists, used for enrichment, not yet routing

### 1.3 Baseline Reconciliation (2026-04-04)

The system underwent a full baseline reconciliation:
- CHAIN_VERSION promoted from `5ac4f06` to `4cc1688` (20 manual commits absorbed)
- 64 superseded observer_hold tasks cancelled
- Full acceptance sweep passed: 587 tests, all runtime/config/workflow/gate checks green
- Version gate: `ok=true`, `dirty=false`
- Preflight: `ok=true` (warnings only: stale sync timer + 44 unmapped CODE_DOC_MAP files)

### 1.4 What Works

| Capability | Status |
|-----------|--------|
| 7-stage linear chain | Stable |
| Observer hold/release | Stable |
| Version gate (HEAD=CHAIN_VERSION) | Stable |
| Isolated merge + ff-only | Stable |
| Memory backend (SQLite FTS5) | Working |
| Impact analyzer (file to node) | Working |
| Node graph (NetworkX DAG) | Working |
| Multi-project isolation | Working |
| Failure classification | Working |
| Chain context (event-sourced) | Working |
| Role permission matrix | Working |
| Memory write guards + schema | Working |
| Prompt validation (4-layer) | Working |
| Audit trail (JSONL + SQLite) | Working |
| Idempotency keys | Working |
| Auto-chain async dispatch | Working |
| QA structured result contract | Working |
| Bounded closure gate semantics | Working |
| Heartbeat watchdog | Working |

---

## 2. Three-Layer Evolution

```
Layer 0: STOP BLEEDING      --> fix what breaks today
Layer 1: CONSOLIDATE         --> unify rules so future work doesn't fight itself
Layer 2: EXPAND              --> add capabilities on a stable foundation
```

### Decision Gates Between Layers

**Layer 0 --> Layer 1 gate:**
- All D1-D5 defects closed or mitigated with workaround
- Task success rate > 80% on aming-claw project
- Chain can complete PM-->Merge without manual intervention (happy path)

**Current assessment:** Gate PASSED. D1-D5 all fixed or mitigated. Chain can complete PM-->Merge. Layer 1 work can proceed.

**Layer 1 --> Layer 2 gate:**
- Role templates deployed, all roles loading from YAML
- Memory writes validated against schema (warn mode at minimum)
- Prompt validator catching metadata coherence issues
- Observer capability formally defined in ROLE_PERMISSIONS
- Unified audit trail covering task + gate + memory events

**Current assessment:** Gate CONDITIONALLY PASSED. All 5 items implemented in code. However, roles use code-based permission matrix, not YAML templates. Memory and prompt validation are active. Audit trail covers task + gate + memory. Observer is in ROLE_PERMISSIONS.

---

## 3. Layer 0: Stop Bleeding

**Status: MOSTLY COMPLETE (7/10 fixed, 3 remaining)**

### 3.1 Worker Claim Stability (D1) -- PARTIALLY FIXED

**What's done:**
- [x] Worker logs every poll cycle result
- [x] Worker detects N consecutive empty polls (counter at executor_worker.py:84)
- [x] Heartbeat mechanism (ai_lifecycle.py:417-428, _HANG_TIMEOUT=120s)
- [x] run_once wrapped in try/except

**Remaining gap:**
- [ ] Worker self-restarts on stall detection (logs warning but doesn't force restart)
- [ ] Service manager stall detection (monitor checks process alive, not poll health)

**Severity:** Low. Worker stalls are rare post-D5 fix (.claude/ dirty filter). Current logging is sufficient for observer detection.

### 3.2 Auto-Chain Timeout (D2) -- FIXED

- [x] task_complete returns immediately after DB commit
- [x] Auto-chain runs asynchronously via background thread (task_registry.py:359-394)
- [x] No behavioral change: chain still produces same next-stage task

### 3.3 PM Role Mapping (D3) -- FIXED

- [x] TASK_ROLE_MAP["pm"] = "pm" (executor_worker.py:54-60)
- [x] PM output matches PRD schema

### 3.4 Gate Block Reason Persistence (D4) -- PARTIALLY FIXED

**What's done:**
- [x] Gate block reason stored in task metadata (executor_worker.py:1014-1044)
- [x] Gate block reason available in auto_chain event metadata

**Remaining gap:**
- [ ] Dedicated gate_events table for audit trail
- [ ] API: GET /api/task/{project_id}/{task_id}/history
- [ ] Observer can inspect block reasons without reading logs

**Severity:** Low. Metadata storage is queryable; dedicated table is nice-to-have.

### 3.5 Duplicate Reply Prevention (D5) -- FIXED

- [x] Idempotency key mechanism (idempotency.py:15-66)
- [x] _reply_sent flag (executor_worker.py)

### 3.6 Observability Baseline (D6) -- PARTIALLY FIXED

**What's done:**
- [x] trace_id generation (observability.py:20-22)
- [x] Structured logging with trace_id support
- [x] trace_id in event_outbox table

**Remaining gap:**
- [ ] trace_id/chain_id columns in tasks table
- [ ] API: GET /api/task/{project_id}/trace/{trace_id}
- [ ] Gate transitions logged with trace_id in structured fields

**Severity:** Medium. End-to-end chain tracing requires manual log correlation today.

### 3.7 Test Result Contract Integrity (D8) -- FIXED

- [x] _gate_t2_pass validates structured test_report (auto_chain.py:1069-1073)
- [x] Missing test_report on success treated as contract defect
- [x] test -> qa progression requires structured report presence

### 3.8 Internal Doc-Gate Contradiction (D9) -- PARTIALLY FIXED

**What's done:**
- [x] Lane deferral logic (_should_defer_doc_gate_to_lane_c, auto_chain.py:189-214)
- [x] Governance internal repair detection (_is_governance_internal_repair, auto_chain.py:940-943)
- [x] docs/dev/** treated as tracked-but-non-governed

**Remaining gap:**
- [ ] Unified doc policy document (currently scattered across code)
- [ ] Replay coverage for doc-governance edge cases

**Severity:** Low. Current code handles the common cases; edge cases are rare.

### 3.9 Contract-Drift Detection (D10) -- NOT FIXED

**What's needed:**
- [ ] Detect policy/config/contract changes outside PM scope
- [ ] Treat excluded directly-related tests as a defect signal
- [ ] Record semantic drift as a workflow defect
- [ ] Add replay coverage for "changed the right file but changed the wrong thing"

**Severity:** Medium. This is the only unaddressed Layer 0 defect. It causes silent regressions when workflow repair tasks change config values without PM authorization.

### Layer 0 Remaining Work Summary

| Item | Effort | Priority |
|------|--------|----------|
| D1 stall self-restart | ~1 day | P2 |
| D4 gate_events table + API | ~2 days | P2 |
| D6 trace_id in tasks + trace API | ~2 days | P1 |
| D9 unified doc policy doc | ~1 day | P2 |
| D10 contract-drift detection | ~3 days | P1 |

**Total remaining Layer 0 effort: ~9 days**

---

## 4. Layer 1: Consolidate

**Status: COMPLETE (all 5 items implemented)**

### 4.1 Role Template Standardization -- DONE (code-based)

Implementation: role_permissions.py permission matrix + role_service.py session management.
Note: Uses code-based matrix, not YAML files. Functionally equivalent but role changes still require Python edits.

**Future improvement (P3):** Migrate to YAML-based role templates for easier editing.

### 4.2 Memory Write Standardization -- DONE

Implementation: models.py MemoryEntry + memory_service.py single write entry + memory_write_guard.py schema validation.

### 4.3 Prompt Quality Control -- DONE

Implementation: decision_validator.py 4-layer validation (Schema, Policy, Graph, Precondition).

### 4.4 Observer Capability Formalization -- DONE

Implementation: Observer in Role enum + formal session auth + observer_hold state machine.

### 4.5 Unified Audit Trail -- DONE

Implementation: audit_service.py dual-write JSONL + SQLite index with cross-module coverage.

---

## 5. Layer 2: Expand

**Status: NOT STARTED**

All Layer 2 items remain as designed in v2. The Layer 1 --> Layer 2 gate is conditionally passed. Work can begin when prioritized.

### 5.1 PM Task Decomposition (~2 weeks)

PM can output subtasks with explicit dependencies. Max subtask limit: 5 (configurable per project).

Prerequisite: Role templates (4.1) + prompt validator (4.3) -- both done.

### 5.2 Parallel Dispatch (~2 weeks)

Independent subtasks execute concurrently. Fan-in waits for all subtasks.

Prerequisite: PM decomposition (5.1) + unified audit (4.5) -- audit done, decomposition not started.

### 5.3 External Project Bootstrap (~1.5 weeks)

Single API call to onboard external project. Auto-generate minimal graph from codebase structure.

Prerequisite: Role templates (4.1) + audit trail (4.5) -- both done.

### 5.4 Pip Packaging + Interface Abstraction (~1.5 weeks)

NotificationGateway abstract class, Redis optional dependency, AmingConfig dataclass, pyproject.toml.

Prerequisite: Bootstrap (5.3) + stable API surface.

### 5.5 Graph-Path-Driven Routing (~4-6 weeks)

Replace hardcoded CHAIN map with dynamic graph-driven routing.

Prerequisite: All of Layer 1 + PM decomposition (5.1).

---

## 6. Dependency Graph (Updated)

```
LAYER 0 (Stop Bleeding) -- MOSTLY DONE
  D1 Worker claim fix .................. PARTIALLY FIXED
  D2 Auto-chain timeout fix ............ FIXED
  D3 PM role mapping fix ............... FIXED
  D4 Gate reason persistence ........... PARTIALLY FIXED
  D5 Duplicate reply fix ............... FIXED
  D6 Observability baseline ............ PARTIALLY FIXED
  D8 Test result contract .............. FIXED
  D9 Doc-gate contradiction ............ PARTIALLY FIXED
  D10 Contract-drift detection ......... NOT FIXED  <-- main remaining gap
      |
      | [Gate: PASSED -- task success > 85%, chain completes PM-->Merge]
      v
LAYER 1 (Consolidate) -- DONE
  4.1 Role templates ................... DONE (code-based)
  4.2 Memory schemas ................... DONE
  4.3 Prompt validator ................. DONE
  4.4 Observer capability .............. DONE
  4.5 Unified audit trail .............. DONE
      |
      | [Gate: CONDITIONALLY PASSED]
      v
LAYER 2 (Expand) -- NOT STARTED
  5.1 PM decomposition -----> 5.2 Parallel dispatch
  5.3 Bootstrap -----> 5.4 Pip packaging
  5.5a Node gate enforce --> 5.5b Dynamic routing --> 5.5c Skip --> 5.5d Per-node
```

---

## 7. Known Warnings (Non-Blocking)

| Warning | Source | Action |
|---------|--------|--------|
| 44 unmapped files in CODE_DOC_MAP | preflight | Expand CODE_DOC_MAP in impact_analyzer.py |
| Version sync stale (git_synced_at old) | preflight | Will auto-fix on next executor run |
| Role templates are code-based, not YAML | Layer 1 | P3 improvement, not blocking |

---

## 8. Governance Defect Classes

Documented patterns of implementation drift discovered during D-series fixes.

### 8.1 Implementation Drift via Workflow Repair

A workflow repair task introduces strategy or config changes not explicitly required by the PM PRD. The repair succeeds at its stated goal but silently regresses a previously validated value.

Prevention: D10 contract-drift detection (NOT FIXED -- main remaining gap).

### 8.2 Excluded Test Regression

A dev task modifies a config value and updates the corresponding test to assert the new (regressed) value, making the regression invisible to CI.

Prevention: Directly relevant tests must not be excluded or regressed without explicit contract permission from the PM PRD.

---

## 9. Anti-Patterns to Avoid

1. **Premature platforming:** Do not build multi-project, multi-notification, multi-backend abstractions until single-project chain is stable for 2+ weeks
2. **Phase 0 as universal blocker:** Layer 1 items that are truly independent of defect fixes can start prep work during Layer 0
3. **Silent degradation:** Every fallback must log a warning, not silently succeed
4. **Abstraction without evidence:** Before creating NotificationGateway, confirm at least 2 concrete backends are needed
5. **Optimistic timelines as commitments:** Layer gates are the real schedule control, not calendar weeks
6. **Baseline drift:** Never let manual commits accumulate without promoting CHAIN_VERSION. The 2026-04-04 reconciliation resolved 20 manual commits -- do not repeat this pattern.

---

## 10. Revision History

| Date | Version | Changes |
|------|---------|---------|
| 2026-03-31 | v1 | Initial roadmap (architecture-first, 4 phases) |
| 2026-03-31 | v2 | Restructured to stability-first (3 layers). Added decision gates, metrics, resolved questions, anti-patterns. |
| 2026-04-01 | v2.1 | Added Governance Defect Classes section. |
| 2026-04-04 | v3 | Baseline reconciliation complete (4cc1688). Updated all defect statuses. Layer 0: 7/10 fixed. Layer 1: 5/5 done. Archived v2 to docs/dev/roadmap-2026-03-31-archived.md. |
