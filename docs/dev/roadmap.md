# Aming-Claw Roadmap

> Baseline: `de8133f` (2026-04-05, Layer 2 in progress)
> Revision: v6 (ALL LAYERS COMPLETE — 809 tests)
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

**Status: COMPLETE (10/10 fixed)**

### 3.1 Worker Claim Stability (D1) -- FIXED

- [x] Worker logs every poll cycle result
- [x] Worker detects N consecutive empty polls (counter at executor_worker.py:84)
- [x] Heartbeat mechanism (ai_lifecycle.py:417-428, _HANG_TIMEOUT=120s)
- [x] run_once wrapped in try/except
- [x] Worker self-restarts after 20 consecutive empty polls with known queued tasks
- [x] Configurable via EXECUTOR_STALL_THRESHOLD env var
- [x] 9 new tests (test_executor_stall.py)

### 3.2 Auto-Chain Timeout (D2) -- FIXED

- [x] task_complete returns immediately after DB commit
- [x] Auto-chain runs asynchronously via background thread (task_registry.py:359-394)
- [x] No behavioral change: chain still produces same next-stage task

### 3.3 PM Role Mapping (D3) -- FIXED

- [x] TASK_ROLE_MAP["pm"] = "pm" (executor_worker.py:54-60)
- [x] PM output matches PRD schema

### 3.4 Gate Block Reason Persistence (D4) -- FIXED

- [x] Gate block reason stored in task metadata (executor_worker.py:1014-1044)
- [x] Gate block reason available in auto_chain event metadata
- [x] Dedicated gate_events table (schema v12, db.py)
- [x] Every gate check records pass/block event with reason and trace_id
- [x] API: GET /api/task/{project_id}/{task_id}/gates returns gate history
- [x] 11 new tests (test_gate_events.py)

### 3.5 Duplicate Reply Prevention (D5) -- FIXED

- [x] Idempotency key mechanism (idempotency.py:15-66)
- [x] _reply_sent flag (executor_worker.py)

### 3.6 Observability Baseline (D6) -- FIXED

**What's done:**
- [x] trace_id generation (observability.py:20-22)
- [x] Structured logging with trace_id support
- [x] trace_id in event_outbox table
- [x] trace_id/chain_id columns in tasks table (schema v10→v11 migration)
- [x] API: GET /api/task/{project_id}/trace/{trace_id}
- [x] Gate transitions logged with trace_id in structured fields (auto_chain.py)

**Severity:** Resolved. End-to-end chain tracing now works via trace_id propagation.

### 3.7 Test Result Contract Integrity (D8) -- FIXED

- [x] _gate_t2_pass validates structured test_report (auto_chain.py:1069-1073)
- [x] Missing test_report on success treated as contract defect
- [x] test -> qa progression requires structured report presence

### 3.8 Internal Doc-Gate Contradiction (D9) -- FIXED

- [x] Lane deferral logic (doc_policy.py)
- [x] Governance internal repair detection (doc_policy.py:is_governance_internal_repair)
- [x] docs/dev/** treated as tracked-but-non-governed (doc_policy.py:is_dev_artifact)
- [x] agent/tests/** classified as always-related (doc_policy.py:is_test_fixture)
- [x] Unified doc_policy.py consolidating all doc governance rules
- [x] auto_chain.py refactored to use doc_policy instead of inline logic
- [x] 26 new tests (test_doc_policy.py)

### 3.9 Contract-Drift Detection (D10) -- FIXED

- [x] drift_detector.py: capture_baseline() snapshots policy constants
- [x] detect_drift() compares current vs baseline, flags unauthorized changes
- [x] Integrated as warn-only check in _gate_checkpoint (auto_chain.py)
- [x] Drift report stored in task metadata as _drift_report for QA/Gatekeeper
- [x] Minimum tracked constants: CHAIN, _CLAUDE_ROLE_TURN_CAPS, _HANG_TIMEOUT, etc.
- [x] 8 new tests (test_drift_detector.py)

### Layer 0 Completion Summary

All defects resolved on 2026-04-04. 658 tests pass (up from 587).

| Commit | Feature | Tests Added |
|--------|---------|-------------|
| `816bb67` | D6: trace_id/chain_id in tasks + trace API (schema v11) | +8 |
| `659d0a7` | D10: drift_detector.py + gate integration | +8 |
| `89dc07c` | Phase 3 replay validation (9 contract boundary cases) | +9 |
| `42d320e` | D1: worker stall self-restart | +9 |
| `11b2b9b` | D4: gate_events audit table + API (schema v12) | +11 |
| `879f1a9` | D9: unified doc_policy.py + auto_chain refactor | +26 |

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

**Status: COMPLETE (5/5 done)**

### 5.1 PM Task Decomposition -- DONE (de8133f)

PM can output subtasks with explicit dependencies. Max subtask limit: 5 (configurable per project).

Implementation:
- Schema v13: subtask_groups table + tasks columns (subtask_group_id, subtask_local_id, subtask_depends_on)
- _gate_post_pm validates subtask count, mandatory fields, DAG acyclicity
- Fan-out: creates subtask_group + dev tasks for roots, blocked tasks for dependents
- Fan-in: merge completion unblocks downstream; all-complete triggers deploy
- Failure cascade: terminal failure cancels blocked siblings
- API: GET /api/task/{pid}/subtask-group/{gid}
- 15 tests (test_subtask_decomposition.py)

### 5.2 Parallel Dispatch -- DONE (fdba749)

Independent subtasks execute concurrently. Fan-in waits for all subtasks.

Implementation:
- WorkerPool class: manages up to MAX_CONCURRENT_WORKERS threads (env var, default 2, max 5)
- Sibling subtask detection: same subtask_group_id, no unmet deps → parallel dispatch
- Each worker thread gets own worktree (.worktrees/worker-{N}/dev-task-{id})
- Atomic fan-in via governance API (completed_count = completed_count + 1)
- Graceful shutdown: join threads with SHUTDOWN_TIMEOUT, force-release on timeout
- Backward compatible: single-task chains use sequential run_once
- ServiceManager worker pool lifecycle monitoring
- MCP executor_status reports pool status (active_workers, per-worker info)
- 25 tests (test_parallel_dispatch.py)

### 5.3 External Project Bootstrap -- DONE (c90fbb8)

Single API call to onboard external project. Auto-generate minimal graph from codebase structure.

Implementation:
- New module graph_generator.py: codebase scanning, language detection, layered graph generation (L0-L4)
- Python import-based dependency edges via ast.parse; directory-structure fallback for other languages
- POST /api/project/bootstrap endpoint with config discovery, graph generation, version seed
- generate_default_config() auto-detects test runner and deploy strategy
- Per-project code_doc_map.json in impact_analyzer.py
- check_bootstrap() preflight verification
- 42 tests (test_graph_generator.py + test_bootstrap.py)

### 5.4 Pip Packaging + Interface Abstraction -- DONE (cc289f4)

NotificationGateway abstract class, Redis optional dependency, AmingConfig dataclass, pyproject.toml.

Implementation:
- pyproject.toml: renamed to aming-claw, click dep, redis/docker/full optional groups
- NotificationGateway ABC + ConsoleGateway + factory (agent/notification_gateway.py)
- TelegramGateway adapter with virtual ABC registration (duck-typed for Docker isolation)
- AmingConfig dataclass: env > yaml > defaults priority (agent/config.py)
- Click-based CLI: init/bootstrap/status/run-executor (agent/cli.py)
- Public API exports: AmingConfig, bootstrap_project, create_task (agent/__init__.py)
- aming_claw.py shim for `from aming_claw import ...` after pip install
- redis_client.py: Python 3.9 compat (Optional[] types, __future__ annotations)
- 24 tests (test_notification_gateway.py, test_aming_config.py, test_package_install.py, test_cli.py)

### 5.5 Graph-Path-Driven Routing -- DONE (3b110a9)

Replace hardcoded CHAIN map with dynamic graph-driven routing.

Implementation:
- dispatch_next_stage(): graph-driven routing with CHAIN dict fallback
- Node gate_mode=skip bypasses QA/gatekeeper; verify_level=0 skips test
- verify_requires ordering: blocks dispatch until prerequisite nodes verified
- Backward compatible: falls back to linear CHAIN when no graph or all default policies
- Every routing decision audited with trace_id in audit_log
- get_node_routing_policy() + get_routing_policies_for_nodes() on AcceptanceGraph
- get_node_specific_gates() in gate_policy.py for per-node gate retrieval
- GateMode.SKIP + MANUAL enum variants, NodeDef.verify_requires field
- Impact analyzer returns gate_mode/verify_level in affected_nodes
- Integration in _do_chain: graph routing override with CHAIN fallback
- 45 tests (test_graph_routing.py, test_auto_chain_routing.py)

---

## 6. Dependency Graph (Updated)

```
LAYER 0 (Stop Bleeding) -- COMPLETE
  D1 Worker claim fix .................. FIXED (42d320e)
  D2 Auto-chain timeout fix ............ FIXED
  D3 PM role mapping fix ............... FIXED
  D4 Gate reason persistence ........... FIXED (11b2b9b)
  D5 Duplicate reply fix ............... FIXED
  D6 Observability baseline ............ FIXED (816bb67)
  D8 Test result contract .............. FIXED
  D9 Doc-gate contradiction ............ FIXED (879f1a9)
  D10 Contract-drift detection ......... FIXED (659d0a7)
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
LAYER 2 (Expand) -- COMPLETE (5/5)
  5.1 PM decomposition ............ DONE (de8133f) -----> 5.2 Parallel dispatch .... DONE (fdba749)
  5.3 Bootstrap ................... DONE (c90fbb8) -----> 5.4 Pip packaging ....... DONE (cc289f4)
  5.5 Graph-path routing .......... DONE (3b110a9)
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
| 2026-04-04 | v4 | Layer 0 COMPLETE (879f1a9). All 10 defects fixed. +71 new tests (658 total). Phase 3 replay validation done. |
| 2026-04-05 | v5 | Layer 2: 5.1 PM decomposition (de8133f) + 5.3 project bootstrap (c90fbb8). +57 new tests (715 total). |
| 2026-04-05 | v6 | ALL LAYERS COMPLETE. Layer 2: 5.2 parallel dispatch (fdba749) + 5.4 pip packaging (cc289f4) + 5.5 graph routing (3b110a9). +94 new tests (809 total). |
