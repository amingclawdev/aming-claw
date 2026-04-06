# Graph-Driven Documentation Governance

> Author: Observer
> Created: 2026-04-06
> Status: PROPOSAL v5 (level-aware implementation order)

---

## 1. Problem Statement

Code changes are not followed by documentation updates. Six bug fixes (B1–B7) produced 7 commits, yet docs remain stale. Graph is 57% unmapped for code, 88% for docs.

Root cause: PM has no graph context → outputs `doc_impact: []` → gate has nothing to check → docs permanently lag.

---

## 2. Design Principles

- **P1**: Observe before blocking — all new checks start as WARNING + audit
- **P2**: Classify, don't just list — PM classifies each doc: `public_behavior_changed | internal_only | doc_stale_but_out_of_scope | graph_link_suspect`
- **P3**: Clean graph is prerequisite — rebuild before trusting
- **P4**: Two-stage node materialization — proposed_nodes → pending → review → graph
- **P5**: Inferred associations are candidates — `inferred=true`, needs human confirm
- **P6**: **Implementation follows graph levels** — change Level N only after Level 0..N-1 are verified

---

## 3. Graph Topology

### 3.1 Node Dependencies (29 nodes, 34 edges, DAG)

```
Level 0 (12 roots, verify first, parallel):
  agent.agent_misc      agent.ai_lifecycle    agent.cli*
  agent.config          agent.context         agent.service_manager
  governance.chain_ctx  governance.conflict   governance.doc_policy
  governance.models     governance.preflight  governance.task_registry
    │
Level 1 (7 nodes, parallel):
  agent.deploy          governance.db         governance.evidence
  governance.gatekeeper governance.gov_misc   governance.graph
  governance.impact*
    │
Level 2 (4 nodes, parallel):
  governance.memory     governance.observability
  governance.services*  governance.state_svc*
    │
Level 3 (3 nodes, parallel):
  governance.auto_chain  governance.reconcile  governance.server
    │
Level 4 (3 nodes, parallel):
  agent.executor        agent.gateway*        agent.mcp

(* = no test files)
```

### 3.2 Critical Path

`agent.agent_misc → governance.db → governance.services → governance.server → agent.gateway`
Depth: 5 levels. Minimum serial verification steps: 5.

### 3.3 Phase → Node → Level Mapping

| Phase | Nodes Touched | Levels | External Deps |
|-------|--------------|--------|---------------|
| P1 Graph Rebuild | governance.graph, governance.services | L1, L2 | models, db, gov_misc |
| P2 PM + Test | governance.auto_chain, agent.executor | L3, L4 | doc_policy, server |
| P3 Observation | governance.auto_chain | L3 | doc_policy, observability |
| P4 Node Maint | governance.graph, db, auto_chain, reconcile | L1, L3 | models, services |
| P5 Hard Gate | governance.auto_chain, governance.preflight | L0, L3 | observability |

**Key constraint**: `governance.auto_chain` (L3) is touched by P2–P5. All auto_chain changes require L0–L2 stable first.

---

## 4. Implementation: Level-Aware Execution

### Principle

Each step:
1. **Change** code at one level
2. **Test** all affected nodes at that level
3. **Verify** no regression at lower levels
4. **Proceed** to next level only after green

### Step 1: Bootstrap Graph (Level 0–4, read-only scan) [MANUAL FIX]

No code changes — scan existing files, generate mapping, apply to graph.json.

| Sub-step | Action | Verification |
|----------|--------|-------------|
| 1a | Backup graph.json | File exists |
| 1b | Run `scripts/rebuild_graph.py` to generate mapping | Review mapping JSON |
| 1c | Manually assign 27 unmatched tests to nodes | All tests mapped |
| 1d | Apply mapping to graph.json via bootstrap API | `graph.json` updated |
| 1e | Run reconcile(dry_run) | stale_refs=0, orphan=0 |
| 1f | Run ALL tests level by level | All 66 existing tests pass |

**AC-BOOTSTRAP:**

| AC | Description | Verification |
|----|-------------|-------------|
| AC-B1 | All `agent/**/*.py` in graph primary | unmapped_py=0 |
| AC-B2 | All test files in graph test | unmapped_test=0 |
| AC-B3 | All active docs classified (mapped/candidate/intentionally_unmapped) | 100% classified |
| AC-B4 | Zero archive refs in secondary | archive_refs=0 |
| AC-B5 | reconcile clean | stale_refs=0 |
| AC-B6 | Existing verify_status preserved | No regression |
| AC-B7 | Level-by-level test pass | `pytest` per level, all green |

**Target (not hard):**
- Mapped doc coverage ≥90%
- code_doc_map.json generated from graph

---

### Step 2: Level 0 Changes — governance.preflight [MANUAL FIX]

Only P5 touches a Level 0 node (preflight). But P5 is the last phase, so Level 0 changes are deferred.

**For now: Level 0 = stable baseline. No code changes. Run Level 0 tests to confirm.**

```bash
# Level 0 test verification
pytest agent/tests/test_chain_context.py \
      agent/tests/test_conflict_rules.py \
      agent/tests/test_doc_policy*.py \
      agent/tests/test_task_registry.py \
      -v
```

---

### Step 3: Level 1 Changes — governance.graph, governance.db [WORKFLOW or MANUAL]

**What changes:**
- `governance.graph` (graph_generator.py): `_infer_doc_associations()` candidate generation (P4)
- `governance.db` (db.py): `pending_nodes` table schema (P4)

**Implementation**: These are P4 (Node Maintenance) items. Since they touch Level 1, they must be done and verified before any Level 2-4 changes.

| Sub-step | File | Change | Method |
|----------|------|--------|--------|
| 3a | graph_generator.py | `_infer_doc_associations()` returns candidates with `inferred=true` | Workflow |
| 3b | db.py | `pending_nodes` table DDL | Manual Fix (schema change) |
| 3c | Verify Level 1 | Run all Level 1 tests | `pytest test_graph*.py test_deploy*.py test_gatekeeper*.py test_db*.py` |

**AC-LEVEL1:**

| AC | Description |
|----|-------------|
| AC-L1.1 | `_infer_doc_associations` returns list[dict] with inferred=True |
| AC-L1.2 | `pending_nodes` table exists after DDL |
| AC-L1.3 | All Level 0 + Level 1 tests pass |

---

### Step 4: Level 2 Changes — (none planned)

Level 2 nodes (memory, observability, services, state_service) have no planned changes. Run Level 2 tests to confirm stability.

```bash
pytest agent/tests/test_memory*.py agent/tests/test_observability*.py -v
```

---

### Step 5: Level 3 Changes — governance.auto_chain, governance.reconcile, governance.server [WORKFLOW]

**This is the core change level.** Most optimization logic lives here.

| Sub-step | File | Change | Phase |
|----------|------|--------|-------|
| 5a | auto_chain.py `_gate_post_pm` | Graph doc classification validation (observation mode) | P2 |
| 5b | auto_chain.py `_build_dev_prompt` | Merge graph docs into doc_impact | P3 |
| 5c | auto_chain.py `_gate_checkpoint` | Graph docs in allowed + observation doc check | P3 |
| 5d | auto_chain.py `_build_qa_prompt` | Graph consistency check injection | P3 |
| 5e | auto_chain.py `_gate_qa_pass` | Graph doc verification (observation mode) | P3 |
| 5f | auto_chain.py `_audit_doc_gap` | Audit function for doc gaps | P3 |
| 5g | auto_chain.py `_gate_release` | proposed_nodes → pending_nodes (not graph) | P4 |
| 5h | reconcile.py `phase_diff` | stale_doc_refs + unmapped_docs detection | P4 |
| 5i | server.py (no change expected) | — | — |

**Implementation order within Level 3** (by dependency):
1. First: `reconcile.py` (depends on graph, db — both verified at L1)
2. Then: `auto_chain.py` (depends on doc_policy L0, observability L2 — both stable)
3. Verify: Run all Level 3 tests

**Method**: Single workflow chain (PM→Dev→Test→QA→Merge) covering 5a–5h as one dev task. The changes are all in observation mode so no risk of breaking existing chains.

**AC-LEVEL3:**

| AC | Description |
|----|-------------|
| AC-L3.1 | PM prompt contains "Graph Impact Analysis" section |
| AC-L3.2 | _gate_post_pm warns on unclassified graph docs (observation) |
| AC-L3.3 | Dev prompt doc_impact contains graph-derived docs |
| AC-L3.4 | Dev changing graph doc not rejected as "Unrelated" |
| AC-L3.5 | _gate_checkpoint doc check is observation mode (warn not reject) |
| AC-L3.6 | QA prompt contains "Graph Consistency Check" |
| AC-L3.7 | _gate_qa_pass is observation mode |
| AC-L3.8 | _audit_doc_gap writes audit_log row |
| AC-L3.9 | proposed_nodes stored in pending_nodes, not graph |
| AC-L3.10 | reconcile detects stale secondary refs |
| AC-L3.11 | All Level 0 + 1 + 2 + 3 tests pass |

---

### Step 6: Level 4 Changes — agent.executor [WORKFLOW]

**What changes:**
- `executor_worker.py`: Test scriptification (`_execute_test()`, TASK_ROLE_MAP, `_parse_pytest_output`)
- `executor_worker.py`: PM prompt graph impact injection

| Sub-step | File | Change | Phase |
|----------|------|--------|-------|
| 6a | executor_worker.py | `_execute_test()` script mode + pre-flight file check | P2 |
| 6b | executor_worker.py | `TASK_ROLE_MAP["test"]` → `"script"` | P2 |
| 6c | executor_worker.py | `_parse_pytest_output()` | P2 |
| 6d | executor_worker.py | PM prompt: query graph impact API, inject into prompt | P2 |
| 6e | executor_worker.py | Command schema: command_argv / command_shell / shlex fallback | P2 |

**Method**: Workflow. `agent.executor` depends on `governance.server` (L3), which must be verified first.

**AC-LEVEL4:**

| AC | Description |
|----|-------------|
| AC-L4.1 | Test task does not start Claude CLI |
| AC-L4.2 | Test returns correct test_report from subprocess |
| AC-L4.3 | Test inherits worktree path as cwd |
| AC-L4.4 | Test pre-flight rejects missing files |
| AC-L4.5 | command_argv uses shell=False |
| AC-L4.6 | command_shell uses shell=True |
| AC-L4.7 | PM prompt contains graph impact for target_files |
| AC-L4.8 | All Level 0–4 tests pass (full regression) |

---

### Step 7: Observation Period [NO CODE]

Run 8+ real workflow tasks. Collect data:

| Metric | Threshold for Hard Gate |
|--------|----------------------|
| Real tasks observed | ≥ 8 |
| Graph-linked doc judgments | ≥ 30 |
| Precision (graph says update → should update) | ≥ 0.85 |
| Recall (should update → graph detected) | ≥ 0.80 |
| Consecutive tasks without false positive | ≥ 3 |

---

### Step 8: Hard Gate Switch [MANUAL FIX]

After observation thresholds met:

| Sub-step | File | Change |
|----------|------|--------|
| 8a | auto_chain.py `_gate_post_pm` | log.warning → return False |
| 8b | auto_chain.py `_gate_checkpoint` | log.warning → return False |
| 8c | auto_chain.py `_gate_qa_pass` | log.warning → return False |
| 8d | preflight.py | Add stale_secondary_refs check |

Verify: All tests pass. Then run one real workflow task to confirm hard gate works correctly.

---

## 5. Execution Summary

```
Step 1: Bootstrap graph [MANUAL]
  Scan → map → apply → reconcile → verify L0–L4 tests
    ↓ AC-BOOTSTRAP pass
Step 2: Verify Level 0 stable [NO CODE]
  Run L0 tests → confirm baseline
    ↓ all green
Step 3: Level 1 changes [WORKFLOW + MANUAL]
  graph_generator + db schema → verify L0+L1 tests
    ↓ AC-LEVEL1 pass
Step 4: Verify Level 2 stable [NO CODE]
  Run L2 tests → confirm
    ↓ all green
Step 5: Level 3 changes [WORKFLOW]
  auto_chain (observation) + reconcile + audit → verify L0–L3 tests
    ↓ AC-LEVEL3 pass
Step 6: Level 4 changes [WORKFLOW]
  executor (test script + PM prompt) → verify L0–L4 tests (full)
    ↓ AC-LEVEL4 pass
Step 7: Observation period [NO CODE]
  8+ real tasks → collect precision/recall
    ↓ thresholds met
Step 8: Hard gate switch [MANUAL]
  warn → reject → verify
```

## 6. Test Strategy

### Level-by-Level Test Commands

```bash
# Level 0 (12 nodes, 20 tests)
pytest agent/tests/test_chain_context.py agent/tests/test_conflict_rules.py \
      agent/tests/test_doc_policy*.py agent/tests/test_task_registry.py \
      agent/tests/test_config*.py -v

# Level 1 (7 nodes, 18 tests)
pytest agent/tests/test_deploy_chain.py agent/tests/test_db*.py \
      agent/tests/test_evidence*.py agent/tests/test_gatekeeper*.py \
      agent/tests/test_graph*.py -v

# Level 2 (4 nodes, 3 tests)
pytest agent/tests/test_memory*.py agent/tests/test_observability*.py -v

# Level 3 (3 nodes, 4 tests)
pytest agent/tests/test_auto_chain*.py agent/tests/test_reconcile*.py \
      agent/tests/test_server*.py -v

# Level 4 (3 nodes, 5 tests)
pytest agent/tests/test_executor*.py agent/tests/test_mcp*.py -v

# Full regression (all levels)
pytest agent/tests/ -v
```

### New Tests (per step)

```
Step 3 — test_doc_governance.py (Level 1):
  TestInferDocAssociations: 3 tests

Step 5 — test_doc_governance.py (Level 3):
  TestPMGraphImpact: 4 tests
  TestDevGraphAwareness: 3 tests
  TestCheckpointObservation: 3 tests
  TestQAGraphConsistency: 4 tests
  TestNodeMaintenance: 2 tests

Step 6 — test_doc_governance.py (Level 4):
  TestScriptTest: 7 tests
```

---

## 7. Risk & Mitigation

| Risk | Mitigation |
|------|-----------|
| Graph rebuild breaks existing tests | Step 1 runs ALL existing tests before proceeding |
| Level N change breaks Level N-1 | Each step re-runs all lower level tests |
| auto_chain changes (L3) too large for one PR | Split into sub-steps: reconcile first, then auto_chain observation, then QA |
| Test scriptification misparses output | generic parser as fallback (exit code 0 = pass) |
| Observation period too short | Hard quantitative thresholds, not time-based |
| Graph still dirty after bootstrap | AC-B1..B5 are hard gates; no proceeding until clean |

---

## 8. Estimated Effort

| Step | LOC | Method | Prerequisite |
|------|-----|--------|-------------|
| 1. Bootstrap | ~0 (script exists) + manual review | Manual | None |
| 2. L0 verify | 0 | Test only | Step 1 |
| 3. L1 changes | ~40 | Workflow + Manual | Step 2 |
| 4. L2 verify | 0 | Test only | Step 3 |
| 5. L3 changes | ~160 | Workflow | Step 4 |
| 6. L4 changes | ~130 | Workflow | Step 5 |
| 7. Observation | 0 | Monitor | Step 6 |
| 8. Hard gate | ~35 | Manual | Step 7 |
| **Total** | **~365 LOC** | | |
