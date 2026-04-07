# Execution: Graph-Driven Doc Governance

> Plan: [plan-graph-driven-doc.md](../governance/plan-graph-driven-doc.md)
> Started: 2026-04-06
> Status: Step 3 pending (need context window for workflow)

---

## Step 1: Bootstrap Graph [MANUAL]

- [x] Mapping script created (`scripts/rebuild_graph.py`)
- [x] Mapping generated: 29 nodes, 34 edges, 5 levels, 0 cycles
- [x] Source: 80/84 mapped (95%)
- [x] Tests: 44/71 mapped (62%), 27 need manual assignment
- [x] Active docs: 42/42 mapped (100%)
- [x] AC-B1: unmapped_py = 4 (95%) — 4 remaining are __init__.py files, acceptable
- [x] AC-B2: unmapped_test = 0 (100%) — all 71 tests assigned via import analysis
- [x] AC-B3: all active docs classified — 42/42 mapped, 0 active unmapped, 41 dev/ intentionally excluded
- [x] AC-B4: archive_refs = 0 — all old secondary refs replaced with current docs
- [x] AC-B5: wf_summary: 29 pending (new), 119 waived (old) — no orphan errors
- [x] AC-B6: verify_status — old nodes waived, new nodes pending (clean slate)
- [x] AC-B7: 66 core tests pass (commit 3de15f1)

## Step 2: Verify L0 [NO CODE]

- [x] All Level 0 tests green — 276 passed in 30.22s

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
