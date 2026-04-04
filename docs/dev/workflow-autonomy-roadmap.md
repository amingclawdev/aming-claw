# Workflow Autonomy Roadmap

> Baseline: `4cc1688` (2026-04-04, post-reconciliation)
> Revision: v2 (status updated after baseline reconciliation)
> Previous: `docs/dev/workflow-autonomy-roadmap-2026-03-31-archived.md`

## Goal

Evolve the current workflow from:
- `Observer participates in diagnosis and repair`

to:
- `Workflow self-diagnoses and self-improves by default`
- `Observer mainly monitors, approves high-risk actions, and handles rare exceptions`

## Target End State

- workflow runs full chain automatically: `coordinator -> pm -> dev -> test -> qa -> gatekeeper -> merge -> deploy`
- failures are automatically classified
- workflow defects trigger automatic repair tasks
- repaired workflow changes are validated through replay and regression coverage
- Observer sees summaries, alerts, and approval requests instead of manually tracing logs

## Current Progress (2026-04-04)

| Phase | Status | Key Evidence |
|-------|--------|--------------|
| Phase 1: Stabilize Contracts | DONE | Role contracts in artifacts.py, memory in models.py, evidence in evidence.py |
| Phase 2: Failure Classification | DONE | failure_classifier.py with 5 classes + structured issue summaries |
| Phase 3: Automatic Workflow Repair | PARTIAL | Auto-creation works; replay validation set missing |
| Phase 4: Graph-Driven Acceptance | NOT STARTED | -- |
| Phase 5: Observer-Mostly-Monitoring | NOT STARTED | -- |

---

## Phase 1: Stabilize Contracts -- DONE

All contract areas are implemented:

### Role Contracts
- PM: goal, acceptance_criteria, fail_conditions (artifacts.py ROLE_ARTIFACT_SCHEMAS)
- Dev: implementation_summary, changed_files, commit_hash
- Tester: tests_executed, result_summary, recommendation
- QA: scenarios_checked, verdict, recommendation, criteria_results
- Gatekeeper: PM alignment, requirement coverage, acceptance trace

### Memory Contract
- MemoryEntry dataclass (models.py): module_id, kind, content, structured, supersedes, related_nodes
- MemoryKind enum: 8 types (decision, pitfall, workaround, invariant, ownership, pattern, api, stub)
- Write guards: dedup similarity > 0.85, confidence threshold 0.6, source validation, TTL enforcement

### Graph Contract
- Hard rule #1: target_files/create_files requirement (decision_validator.py)
- Graph state validation in auto_chain gates
- Node status tracking: pending -> testing -> t2_pass -> qa_pass (+ waived, failed)

### Evidence Schema
- EVIDENCE_RULES dict (evidence.py): transition-specific requirements
- PENDING->T2_PASS requires test_report
- T2_PASS->QA_PASS requires e2e_report
- any->FAILED requires error_log
- False-pass anti-pattern detectors

---

## Phase 2: Failure Classification -- DONE

Implementation: `agent/governance/failure_classifier.py`

### Failure Classes
1. **task_defect** -- the task itself produced bad output
2. **environment_defect** -- infra/tooling failure
3. **graph_defect** -- graph/governance mismatch
4. **contract_defect** -- role contract or stage contract violation
5. **provider_tool_defect** -- AI provider or tool failure

### Structured Output
- failure_class, workflow_improvement flag, observer_attention flag
- suggested_action, issue_summary
- Confidence levels: high/medium/low based on anti-pattern count

### Observer Summaries
- Classification result available in task metadata
- Automatic improvement task creation when workflow_improvement=true

---

## Phase 3: Automatic Workflow Repair -- PARTIAL

### What's Done
- Workflow improvement task auto-creation (_maybe_create_workflow_improvement_task in auto_chain.py)
- Repair prompt generation from classification (build_workflow_improvement_prompt in failure_classifier.py)
- Event publication + audit recording for improvement tasks
- Lane deferral logic for doc-gate contradictions
- Governance internal repair detection

### What's Missing

**3.1 Replay Validation Set**
- No formal replay cases maintained for validating repair quality
- Repair success is measured only by next-stage execution outcome
- No regression harness for: coordinator routing, PM contract, dev worktree, test contract, QA contract, gatekeeper alignment, merge isolation, deploy smoke, version gate

**3.2 Contract-Drift Detection (D10)**
- No mechanism to detect when repair tasks change policy/config outside PM scope
- This is the most important remaining gap from Layer 0

**3.3 Predict-Verify-Diff-Iterate Pattern**
- Not yet standardized: repairs do not explicitly produce predicted output vs. actual output comparison

### Remaining Work

| Item | Effort | Priority |
|------|--------|----------|
| Replay validation set (9 stage cases) | ~5 days | P1 |
| Contract-drift detection (D10) | ~3 days | P1 |
| Predict-verify-diff-iterate pattern | ~3 days | P2 |

---

## Cross-Cutting: Documentation Governance -- DONE

All items from the original roadmap are addressed:

1. `docs/dev/**` is treated as tracked-but-non-governed -- DONE
2. Formal docs split from dev artifacts in gate semantics -- DONE (lane deferral logic)
3. Internal governance repair policy explicit -- DONE (_is_governance_internal_repair)
4. CODE_DOC_MAP exists but has 22 stale mappings to deleted docs -- LOW PRIORITY cleanup

---

## Phase 4: Graph-Driven Acceptance -- NOT STARTED

### Objective
Move from contract-only acceptance toward evidence-backed graph acceptance.

### Work Items
1. Requirement coverage trace (changed files -> related tests -> node coverage -> evidence)
2. Acceptance trace (criterion -> satisfied? -> by which evidence -> confidence)
3. QA upgrade: validate PM alignment + test evidence + doc impact + scenario coverage
4. Gatekeeper upgrade: require PM alignment + requirement coverage + acceptance trace + node readiness
5. Release gate tightening: node + doc + coverage + version + deploy success

### Prerequisites
- Phase 3 replay set (for regression safety)
- D6 trace_id in tasks (for end-to-end evidence chain)

---

## Phase 5: Observer-Mostly-Monitoring -- NOT STARTED

### Objective
Reduce Observer from active repair participant to governance supervisor.

### Default Observer Role
- Watch dashboards and summaries
- Approve high-risk actions
- Resolve rare ambiguous failures
- Change policy when needed

### Allowed Observer Intervention Categories
1. Policy changes (graph, memory, gate, approval)
2. High-risk overrides (release, cancel/rollback, force gate bypass)
3. Unresolved rare failures (not covered by classifier, infra failures, governance ambiguity)

### Prerequisites
- Phase 3 fully complete (replay + drift detection)
- Phase 4 at least partially complete (graph-driven QA)
- Stable for 2+ weeks without observer-driven repairs

---

## Implementation Priority

### Immediate (P0) -- remaining Layer 0 + Phase 3 gaps

| Item | Effort | Dependency |
|------|--------|------------|
| D10 Contract-drift detection | ~3 days | None |
| D6 trace_id in tasks table + trace API | ~2 days | None |
| Phase 3 replay validation set | ~5 days | None |

### Near-term (P1) -- close Layer 0 + start Phase 4

| Item | Effort | Dependency |
|------|--------|------------|
| D1 worker stall self-restart | ~1 day | None |
| D4 gate_events table + API | ~2 days | None |
| D9 unified doc policy document | ~1 day | None |
| Phase 3 predict-verify-diff pattern | ~3 days | Replay set |
| Phase 4 requirement coverage trace | ~3 days | D6 trace_id |

### Later (P2) -- Layer 2 expansion

| Item | Effort | Dependency |
|------|--------|------------|
| 5.1 PM task decomposition | ~2 weeks | Phase 3 complete |
| 5.3 External project bootstrap | ~1.5 weeks | Layer 1 |
| 5.5 Graph-driven routing | ~4-6 weeks | Phase 4 |
| Role templates YAML migration | ~1 week | Any time |
| CODE_DOC_MAP cleanup | ~1 day | Any time |

---

## Revision History

| Date | Version | Changes |
|------|---------|---------|
| 2026-03-31 | v1 | Initial workflow autonomy roadmap (5 phases) |
| 2026-04-04 | v2 | Post-baseline reconciliation update. Phase 1-2 marked DONE. Phase 3 gaps measured. Priority table added. Archived v1. |
