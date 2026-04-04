# Aming-Claw Roadmap

> Baseline: `e5f4409` (2026-03-31)
> Revision: v2 (restructured from architecture-first to stability-first)

---

## 0. Guiding Principles

1. **Stop bleeding before abstracting** — no platform work until current defects are fixed
2. **Consolidate before parallelizing** — no fan-out/fan-in until contracts are unified
3. **Opt-in before global replacement** — graph routing, observer escalation, validator blocking all start as opt-in
4. **Prove reuse before platforming** — bootstrap, packaging, multi-project built on validated patterns, not speculation
5. **Measure stability, not neatness** — success = task success rate, not "files touched per role change"
6. **Governance semantics must beat file heuristics** — a task may touch the right file and still be wrong if it changes policy outside contract

---

## 1. Current State

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
        +-->  optional: Telegram gateway (Docker)
        +-->  optional: Redis (Docker, pub/sub + distributed lock)
        +-->  optional: DBService (Docker, Node.js, vector search)
```

### 1.2 Workflow Chain (Contract-Driven)

```
PM --> Dev --> Test --> QA --> Gatekeeper --> Merge --> Deploy
```

- Routing: hardcoded CHAIN map in auto_chain.py:93-101
- Gates: validate contract fields (target_files, test_report, recommendation)
- Node graph: exists but used only for enrichment, not routing

### 1.3 What Works

| Capability | Status |
|-----------|--------|
| 7-stage linear chain | Stable |
| Observer hold/release | Stable |
| Version gate (HEAD=CHAIN_VERSION) | Stable |
| Isolated merge + ff-only | Fixed (e5f4409) |
| Memory backend (SQLite FTS5) | Working |
| Impact analyzer (file to node) | Working |
| Node graph (NetworkX DAG) | Working |
| Multi-project isolation | Working |
| Failure classification | Working |
| Chain context (event-sourced) | Working |

### 1.4 Active Defects

| # | Defect | Severity | Impact |
|---|--------|----------|--------|
| D1 | Executor worker stops claiming tasks after initial burst | High | Tasks stuck in queued, chain halts |
| D2 | PM complete triggers 13s auto_chain, exceeds MCP 10s timeout | High | Observer must use curl workaround |
| D3 | TASK_ROLE_MAP["pm"]="coordinator" -- PM uses wrong role | High | PM output is coordinator JSON, not PRD |
| D4 | Gate block reason not persisted to DB | Medium | Cannot audit why chain stopped |
| D5 | Coordinator duplicate reply (gateway + executor both send) | Medium | User sees double messages |
| D6 | No trace_id / chain_id across task lifecycle | Medium | Cannot trace a chain end-to-end |
| D7 | memory_events / memory_relations tables created but never written | Low | Zero audit on memory mutations |
| D8 | Successful test task can persist malformed result without structured `test_report` | High | Chain wrongly falls back to Dev retry instead of progressing to QA |
| D9 | Internal governance fix can hit contradictory doc gates | High | Workflow loops between "docs required" and "docs unrelated" |
| D10 | Workflow repair can drift semantically inside allowed files | High | Role policy changes can land without being requested by PM |

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

**Layer 1 --> Layer 2 gate:**
- Role templates deployed, all roles loading from YAML
- Memory writes validated against schema (warn mode at minimum)
- Prompt validator catching metadata coherence issues
- Observer capability formally defined in ROLE_PERMISSIONS
- Unified audit trail covering task + gate + memory events

---

## 3. Layer 0: Stop Bleeding

**Goal:** System goes from "can run but unstable" to "can run a full chain reliably."
**Duration:** ~1-2 weeks
**Scope:** Only defect fixes and observability. No abstraction, no new features.

### 3.1 Worker Claim Stability (D1)

**Problem:** Executor worker claims a batch of tasks, processes them, then stops claiming new ones despite queued tasks existing. Worker process stays alive (low CPU) but never calls claim_task again.

**Investigation targets:**
- executor_worker.py run_loop / run_once poll cycle
- Fence token expiry or CAS failure silently swallowed
- Governance API returning empty claim when tasks exist (status mismatch)
- Redis connection loss causing silent degradation

**Fix requirements:**
- [ ] Worker logs every poll cycle result (claimed / empty / error)
- [ ] Worker detects N consecutive empty polls with known queued tasks and self-restarts
- [ ] Service manager detects worker stall and restarts subprocess
- [ ] Heartbeat mechanism: worker reports last_claimed_at to governance

### 3.2 Auto-Chain Timeout (D2)

**Problem:** task_complete for PM type triggers on_task_completed --> _do_chain which takes ~13s (preflight + gate + builder + task_create + memory writes). MCP tool timeout is ~10s.

**Fix options (pick one):**
- **Option A (recommended):** Make task_complete return immediately after DB commit; run auto_chain asynchronously via background thread or event queue
- **Option B:** Increase MCP tool timeout to 30s for write operations
- **Option C:** Split auto_chain into "schedule next" (fast) + "build prompt" (deferred)

**Fix requirements:**
- [ ] PM task_complete returns in < 3s
- [ ] Auto-chain result available via polling (task_list shows child task)
- [ ] No behavioral change: chain still produces same next-stage task

### 3.3 PM Role Mapping (D3)

**Problem:** TASK_ROLE_MAP["pm"] = "coordinator" causes PM to receive coordinator prompt and produce coordinator JSON output instead of PRD.

**Fix:** Change mapping to "pm" and verify _build_prompt has a pm-specific branch.

**Fix requirements:**
- [ ] TASK_ROLE_MAP["pm"] = "pm"
- [ ] _build_prompt handles task_type == "pm" explicitly
- [ ] PM output matches PRD schema (target_files, acceptance_criteria, verification)
- [ ] Test: create PM task, verify output has PRD fields

### 3.4 Gate Block Reason Persistence (D4)

**Problem:** When a gate blocks chain progression, the reason is logged and optionally sent to Telegram, but not stored in the task record or a dedicated audit table.

**Fix requirements:**
- [ ] Gate block reason stored in task_attempts.error_message or dedicated gate_events table
- [ ] Queryable via API: GET /api/task/{project_id}/{task_id}/history
- [ ] Observer can inspect block reasons without reading logs

### 3.5 Duplicate Reply Prevention (D5)

**Problem:** Coordinator tasks sometimes produce two replies: one from gateway event handler, one from executor result handler.

**Fix requirements:**
- [ ] Idempotency key on reply dispatch (task_id + reply_type)
- [ ] Gateway checks if executor already replied before sending
- [ ] Or: only one path sends replies (not both)

### 3.6 Observability Baseline (D6)

**Fix requirements:**
- [ ] Every task carries trace_id (root chain identifier) and chain_id (current chain)
- [ ] Logs include trace_id in structured fields
- [ ] API: GET /api/task/{project_id}/trace/{trace_id} returns full chain history
- [ ] Gate transitions logged with: task_id, gate_name, passed/blocked, reason, timestamp

### 3.7 Test Result Contract Integrity (D8)

**Problem:** A `test` task may complete successfully while persisting a malformed or incomplete result payload. When `test_report` is missing, auto-chain misclassifies the run and generates unnecessary `dev` retries.

**Fix requirements:**
- [ ] Successful `test` completion must always persist structured `test_report`
- [ ] Missing `test_report` on success is treated as contract defect, not silent success
- [ ] Replay coverage exists for `test -> qa` progression on structured report presence
- [ ] Observer summaries call out malformed success payloads explicitly

### 3.8 Internal Doc-Gate Contradiction (D9)

**Problem:** Internal governance fixes currently can be required to update formal docs by one gate and simultaneously rejected as unrelated by another gate.

**Fix requirements:**
- [ ] One explicit policy for internal workflow/governance fixes:
  - whether formal docs are required
  - which doc domains are in-scope
  - how `docs/dev/**` is treated
- [ ] `doc gate` and `unrelated-files gate` consume the same policy inputs
- [ ] Internal governance repair replays cannot oscillate between adding and removing the same docs

### 3.9 Contract-Drift Detection (D10)

**Problem:** Workflow repair tasks can modify the correct file but still make an unrequested policy change, for example tightening a role parameter that PM never requested.

**Fix requirements:**
- [ ] Detect policy/config/contract changes that are outside PM scope
- [ ] Treat excluded directly-related tests as a defect signal
- [ ] Record semantic drift as a workflow defect, not only a reviewer note
- [ ] Add replay coverage for "changed the right file but changed the wrong thing"

### Layer 0 Success Metrics

| Metric | Current (estimated) | Target |
|--------|-------------------|--------|
| Task success rate (happy path) | ~60% | > 85% |
| Worker stall incidents per day | ~2-3 | 0 |
| PM complete API response time | ~13s | < 3s |
| Gate block reason queryable | 0% | 100% |
| Duplicate reply incidents | ~30% of coordinator tasks | < 5% |
| Chain traceable end-to-end | No | Yes |

---

## 4. Layer 1: Consolidate

**Goal:** Unify rules so future capability work doesn't fight itself.
**Duration:** ~3-4 weeks (after Layer 0 gate passes)
**Scope:** Structural convergence. Not "more general" but "more consistent."

### 4.1 Role Template Standardization

**Why now (not in Layer 0):** D3 is a quick fix (change one mapping). But the root cause -- roles scattered across 4 files -- will keep producing similar bugs. Templates prevent recurrence.

**Approach:** Incremental migration, not big-bang.

```
Phase A: Define schema + write YAML for each role
Phase B: New tasks load from YAML; old tasks still work via fallback
Phase C: Remove inline prompt code after 1 week of dual-running
```

**Deliverables:**
```
agent/roles/
  _schema.yaml         # Template schema definition
  pm.yaml              # PM role
  dev.yaml             # Dev role
  tester.yaml          # Tester role
  qa.yaml              # QA role
  gatekeeper.yaml      # Gatekeeper role
  coordinator.yaml     # Coordinator role
  observer.yaml        # Observer role (NEW)
agent/governance/role_loader.py  # Loader with fallback to inline
```

**Each YAML contains:**
```yaml
role_id: dev
display_name: Developer
prompt_template: |
  You are the developer...
  {acceptance_criteria}
  {target_files}
tools_allowed: [Read, Grep, Glob, Write, Edit, Bash]
turn_cap: 40
token_budget: 4000
output_schema:
  required: [changed_files, summary]
memory_write_kinds: [decision]
```

**Migration safety:**
- role_loader.py tries YAML first, falls back to inline code
- Feature flag: ROLE_TEMPLATE_SOURCE=yaml|inline|auto (default: auto)
- Existing tasks keep working during migration

**Acceptance criteria:**
- [ ] All 7 roles defined in YAML
- [ ] _build_prompt reads from YAML (with inline fallback)
- [ ] PM role bug cannot recur (role_id is explicit in YAML, not a mapping)
- [ ] Role change = edit one YAML file, no Python change

### 4.2 Memory Write Standardization

**Why now:** Memory is the AI's long-term context. If writes are inconsistent, AI decisions degrade over time.

**Approach:** Schema validation in warn mode first, block mode after 2 weeks.

**Kind registry (6 canonical kinds):**

| Kind | Stage | Writer | Content | Conflict policy |
|------|-------|--------|---------|-----------------|
| prd_scope | PM | auto_chain | JSON | replace |
| decision | Dev | executor | text | append |
| validation_result | Test | auto_chain | text + report | replace |
| failure_pattern | Gate | auto_chain | markdown | append |
| qa_decision | QA | auto_chain | text | replace |
| task_result | Merge | executor | text | append |

**Required structured fields (all kinds):**
```json
{
  "task_id": "REQUIRED string",
  "chain_stage": "REQUIRED pm|dev|test|qa|merge|deploy",
  "parent_task_id": "optional string"
}
```

**Deliverables:**
- agent/governance/memory_schemas.py -- JSON Schema per kind
- Single write entry point: memory_service.write_memory() for all callers
- MemoryKind enum aligned with actual registry
- memory_events table populated on every write

**Migration safety:**
- Week 1-2: warn mode (log invalid writes, don't reject)
- Week 3+: block mode (reject writes that fail schema validation)
- Backfill script: normalize existing memory records to new schema

**Acceptance criteria:**
- [ ] All writes go through single entry point
- [ ] Schema validation on write (warn first, block later)
- [ ] memory_events populated
- [ ] Old ad-hoc kind values mapped to canonical kinds

### 4.3 Prompt Quality Control

**Why now:** Contradictory prompts have caused AI agent deadlock. Prevention is cheaper than recovery.

**Approach:** Start with metadata coherence checks. Add contradiction detection later.

**Validation rules (ordered by implementation priority):**

1. **Metadata coherence:** target_files must overlap with verification.command paths
2. **Required fields per role:** PM result must have target_files + acceptance_criteria
3. **Length check:** prompt + injected context must fit role token budget (warn at 80%, block at 100%)
4. **Output schema declared:** task must specify expected output schema_version
5. **Contradiction detection (Phase 2):** flag "add X" + "remove X" patterns

**Integration point:**
```python
# In task_registry.create_task():
warnings = validate_prompt(prompt, metadata, role_template)
if warnings.has_blocking:
    return {"status": "rejected", "validation_errors": warnings.errors}
# Non-blocking warnings stored in task.metadata.prompt_warnings
```

**Migration safety:**
- Rules 1-3: warn mode (log, store in metadata, don't reject)
- Rule 4: enforce after role templates deployed (Phase 4.1)
- Rule 5: deferred to Layer 2

### 4.4 Observer Capability Formalization

**Why now:** Observer is the human's hand in the system. Unclear boundaries = either too much power (silent override) or too little (cannot intervene when needed).

**Capability matrix:**

| Action | Observer | Coordinator | Executor |
|--------|----------|-------------|----------|
| hold / release / cancel task | Yes | Yes | No |
| complete task | Yes (audit required) | Yes | Yes |
| bypass gate | No (must escalate) | Yes | No |
| edit graph | No | Yes | No |
| sync node state | Yes | Yes | No |
| toggle observer_mode | Yes | Yes | No |
| import graph | No | Yes | No |
| rollback chain | No | Yes | No |

**Deliverables:**
- observer.yaml in roles directory (from 4.1)
- ROLE_PERMISSIONS updated with observer entry
- Formal auth check: role_permissions.check(role, action) replaces startswith("observer")
- Escalation path: observer.request_escalation(action, reason) --> coordinator approval

**Acceptance criteria:**
- [ ] Observer in ROLE_PERMISSIONS with explicit allow/deny
- [ ] All observer API calls go through permission check
- [ ] Escalation audit trail for denied actions
- [ ] No more pattern-matching on role name string

### 4.5 Unified Audit Trail

**Why now:** Every module (task, gate, memory, role) currently logs independently. Cross-cutting audit is impossible.

**Deliverables:**
- Unified audit_events table: timestamp, trace_id, actor, action, target, result, detail
- All task transitions, gate decisions, memory writes, role checks emit audit events
- API: GET /api/audit/{project_id}?trace_id=X returns full timeline

**This is the foundation for Layer 2 observability and debugging.**

### Layer 1 Success Metrics

| Metric | Current | Target |
|--------|---------|--------|
| Files to edit for role change | 4 | 1 (YAML) |
| Memory writes with schema validation | 0% | 100% (warn mode) |
| Prompt metadata coherence checked | 0% | 100% |
| Observer actions with permission check | ~30% | 100% |
| Cross-module audit coverage | 0% | > 80% |
| Contradictory prompt pre-detection | 0% | > 50% (basic patterns) |

---

## 5. Layer 2: Expand

**Goal:** Add capabilities that increase system value, built on stable foundation.
**Duration:** ~6-10 weeks (after Layer 1 gate passes)
**Scope:** New features. Each opt-in, per-project controllable, rollback-safe.

### 5.1 PM Task Decomposition (~2 weeks)

**Prerequisite:** Role templates (4.1) + prompt validator (4.3) deployed.

**Scope:**
- PM can output subtasks with explicit dependencies
- Each subtask: own target_files, acceptance_criteria, depends_on list
- Max subtask limit: 5 (configurable per project)
- Dependencies stored in task_dependencies table

**Subtask output format:**
```yaml
subtasks:
  - id: component-a
    target_files: [agent/foo.py]
    depends_on: []
  - id: component-b
    target_files: [agent/bar.py]
    depends_on: [component-a]
```

**New task status:** awaiting_dependency (waits until all depends_on satisfied)

**Rollback:** If decomposition produces worse results, disable via project config; PM reverts to single-task output.

### 5.2 Parallel Dispatch (~2 weeks)

**Prerequisite:** PM decomposition (5.1) + unified audit (4.5).

**Scope:**
- Independent subtasks (no shared files) execute concurrently
- Fan-in: all subtasks complete triggers next stage
- Conflict detection: prevent parallel work on same files
- Service manager can spawn N workers (default: 1, configurable)

**Safety:**
- Parallel only for subtasks with disjoint target_files
- File-level lock prevents concurrent edits
- Fan-in waits for ALL subtasks (no partial advancement)

### 5.3 External Project Bootstrap (~1.5 weeks)

**Prerequisite:** Role templates (4.1) + audit trail (4.5).

**Scope:**
- Single API call to onboard external project
- Auto-generate minimal graph from codebase structure
- Observer mode enabled by default for new projects

**API:**
```
POST /api/project/bootstrap
  Input: { project_id, workspace_path, auto_graph: true, observer_mode: true }
  Does:  init -> scan -> graph -> baseline -> observer_mode
  Returns: { coordinator_token, node_count, health }
```

**Graph scaffold:**
- L1 nodes from top-level directories
- L2 nodes from discovered test files
- Dependencies inferred from imports
- Result: acceptance-graph.md (can be refined manually)

**CLI:** aming bootstrap --project my-lib --workspace /path/to/repo

### 5.4 Pip Packaging + Interface Abstraction (~1.5 weeks)

**Prerequisite:** Bootstrap (5.3) + stable API surface.

**Scope:**
- NotificationGateway abstract class (Telegram, Console, Slack implementations)
- Redis as optional dependency with graceful degradation
- AmingConfig dataclass replacing scattered env vars
- pyproject.toml with complete entry points and optional deps

```toml
[project.optional-dependencies]
telegram = ["requests>=2.32"]
redis = ["redis>=5.0"]
slack = ["slack-sdk>=3.0"]

[project.scripts]
aming-governance = "agent.governance.server:main"
aming-executor = "agent.executor_worker:main"
aming-bootstrap = "agent.governance.bootstrap_service:cli_main"
```

**Constraint:** Only package what is stable. Unstable interfaces stay internal.

### 5.5 Graph-Path-Driven Routing (~4-6 weeks)

**Prerequisite:** All of Layer 1 + PM decomposition (5.1).

**Sub-phases:**

**5.5a. Node gate enforcement (~3 days):**
- Remove "temporarily non-blocking" from Dev stage
- Node gates become hard blockers
- No routing change

**5.5b. Dynamic routing (~2 weeks):**
```python
# Replace:
next_type = CHAIN[task_type][1]

# With:
affected = metadata["related_nodes"]
needed = graph.compute_needed_stages(affected, current_status)
next_type = needed[0] if needed else None
```

**Feature flag:** _ENABLE_GRAPH_ROUTING (default: false, per-project)

**5.5c. Stage skipping (~1 week):**
- Skip Test if all related_nodes already t2_pass
- Skip QA if all related_nodes already qa_pass
- Skip logged in audit trail with reason

**5.5d. Per-node prompt generation (~2 weeks):**
- Prompt built from node's primary/test/secondary files
- Per-node test coverage tracked in node_state.evidence
- Node-level progress API

**Rollback:** Per-project feature flag. If graph routing fails, revert to CHAIN map lookup. Rollback granularity: per-project, not global.

### Layer 2 Success Metrics

| Metric | Target |
|--------|--------|
| Subtask decomposition success rate | > 80% |
| Parallel task conflict rate | < 5% |
| New project bootstrap time | < 5 minutes |
| pip install + first chain completion | < 30 minutes |
| Graph routing rollback success | 100% |
| Stages skipped (when nodes already verified) | Auto-detected |

---

## 6. Dependency Graph

```
LAYER 0 (Stop Bleeding)
  D1 Worker claim fix
  D2 Auto-chain timeout fix
  D3 PM role mapping fix
  D4 Gate reason persistence
  D5 Duplicate reply fix
  D6 Observability baseline
      |
      | [Gate: task success > 85%, chain completes without manual intervention]
      v
LAYER 1 (Consolidate)
  4.1 Role templates --------+
  4.2 Memory schemas          |---> all share bottom-layer models
  4.3 Prompt validator        |     (task schema, role metadata, audit)
  4.4 Observer capability ----+
  4.5 Unified audit trail ----+
      |
      | [Gate: templates deployed, schemas validated, audit covering > 80%]
      v
LAYER 2 (Expand)
  5.1 PM decomposition -----> 5.2 Parallel dispatch
  5.3 Bootstrap -----> 5.4 Pip packaging
  5.5a Node gate enforce --> 5.5b Dynamic routing --> 5.5c Skip --> 5.5d Per-node
```

---

## 7. Success Metrics (Three Categories)

### A. Stability Metrics (Layer 0)

| Metric | Definition |
|--------|-----------|
| Task success rate | Tasks reaching succeeded / total tasks dispatched |
| Worker stall rate | Worker stops per day |
| Mean chain duration | Average time from PM create to merge complete |
| Human intervention rate | Tasks requiring manual observer action / total |
| Chain break rate | Chains that halt without terminal state / total chains |
| Duplicate action rate | Same reply/task created twice / total |

### B. Governance Metrics (Layer 1)

| Metric | Definition |
|--------|-----------|
| Illegal role call intercept rate | Unauthorized actions caught / total unauthorized attempts |
| Memory invalid write rate | Writes failing schema validation / total writes |
| Audit coverage | Events with audit record / total significant events |
| Structured output compliance | Task results matching declared schema / total results |
| Role change cost | Files modified to change a role definition |

### C. Expansion Metrics (Layer 2)

| Metric | Definition |
|--------|-----------|
| Subtask success rate | Decomposed subtasks succeeding / total subtasks |
| Parallel conflict rate | Subtasks conflicting on shared files / total parallel runs |
| Graph routing rollback rate | Routing rollbacks needed / total graph-routed chains |
| Bootstrap time | Time from command to first chain completion |
| New project first-chain success | External projects completing first chain / total onboarded |

---

## 8. Resolved Decisions

These were Open Questions in v1. Resolved here to prevent downstream rework.

| Question | Decision | Rationale |
|----------|----------|-----------|
| Graph routing: opt-in or global? | **Opt-in per project** | Global switch has catastrophic rollback cost |
| Observer can bypass gates? | **No, must escalate to coordinator** | Observer is oversight, not override |
| Prompt validation: block or warn? | **Warn first, block after 2 weeks** | Avoid breaking existing working chains |
| Role template migration: big-bang or incremental? | **Incremental with fallback** | Reduces blast radius |
| Memory schema enforcement timing? | **Warn 2 weeks, then block** | Gives time to fix existing writers |
| PM decomposition max subtasks? | **5 (configurable per project)** | Prevents runaway fan-out |

---

## 9. Anti-Patterns to Avoid

1. **Premature platforming:** Do not build multi-project, multi-notification, multi-backend abstractions until single-project chain is stable for 2+ weeks
2. **Phase 0 as universal blocker:** Layer 1 items that are truly independent of defect fixes can start prep work (schema design, YAML drafting) during Layer 0
3. **Silent degradation:** Every fallback (Redis unavailable, schema validation off, inline role fallback) must log a warning, not silently succeed
4. **Abstraction without evidence:** Before creating NotificationGateway, confirm at least 2 concrete backends are needed. Before graph routing, confirm at least 3 chains where it would have helped
5. **Optimistic timelines as commitments:** All durations in this document are estimates. Layer gates are the real schedule control, not calendar weeks

---

## 10. Revision History

| Date | Version | Changes |
|------|---------|---------|
| 2026-03-31 | v1 | Initial roadmap (architecture-first, 4 phases) |
| 2026-03-31 | v2 | Restructured to stability-first (3 layers: stop bleeding / consolidate / expand). Added decision gates between layers. Added stability + governance + expansion metrics. Resolved open questions. Added anti-patterns. |
| 2026-04-01 | v2.1 | Added Governance Defect Classes section (implementation drift, excluded test patterns). |

---

## 11. Governance Defect Classes

Documented patterns of implementation drift discovered during D-series fixes.

### 11.1 Implementation Drift via Workflow Repair

**Defect pattern:** A workflow repair task (e.g., fixing a chain break or role mapping) introduces strategy or config changes not explicitly required by the PM PRD. The repair succeeds at its stated goal but silently regresses a previously validated value.

**Example:** D2 fix set `_CLAUDE_ROLE_TURN_CAPS["pm"] = "60"`. A subsequent workflow repair task regressed this to `"5"` without PM authorization.

**Prevention rule:** Workflow repair tasks must not introduce strategy or config changes not explicitly required by the PM PRD. Dev tasks must scope changes strictly to the requirements listed in the originating PRD.

### 11.2 Excluded Test Regression

**Defect pattern:** A dev task modifies a config value and updates the corresponding test to assert the new (regressed) value, making the regression invisible to CI. The excluded test no longer guards the original contract.

**Prevention rule:** Directly relevant tests must not be excluded or regressed without explicit contract permission from the PM PRD. If a test value changes, the PRD must explicitly authorize the new value.
