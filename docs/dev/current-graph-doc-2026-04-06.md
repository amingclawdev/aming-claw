# Execution: Graph-Driven Doc Governance

> Plan: [plan-graph-driven-doc.md](../governance/plan-graph-driven-doc.md)
> Started: 2026-04-06
> Status: Step 1 in progress

---

## Step 1: Bootstrap Graph [MANUAL]

- [x] Mapping script created (`scripts/rebuild_graph.py`)
- [x] Mapping generated: 29 nodes, 34 edges, 5 levels, 0 cycles
- [x] Source: 80/84 mapped (95%)
- [x] Tests: 44/71 mapped (62%), 27 need manual assignment
- [x] Active docs: 42/42 mapped (100%)
- [ ] AC-B1: unmapped_py = 0
- [ ] AC-B2: unmapped_test = 0
- [ ] AC-B3: all active docs classified
- [ ] AC-B4: archive_refs = 0
- [ ] AC-B5: reconcile clean
- [ ] AC-B6: verify_status preserved
- [ ] AC-B7: level-by-level tests pass

## Step 2: Verify L0 [NO CODE]

- [ ] All Level 0 tests green

## Step 3: Level 1 [WORKFLOW+MANUAL]

- [ ] AC-L1.1 — AC-L1.3 (see plan)

## Step 4: Verify L2 [NO CODE]

- [ ] All Level 2 tests green

## Step 5: Level 3 [WORKFLOW]

- [ ] AC-L3.1 — AC-L3.11 (see plan)

## Step 6: Level 4 [WORKFLOW]

- [ ] AC-L4.1 — AC-L4.8 (see plan)

## Step 7: Observation [NO CODE]

- [ ] ≥8 tasks, precision≥0.85, recall≥0.80

## Step 8: Hard Gate [MANUAL]

- [ ] AC-HG1 — AC-HG3 (see plan)
