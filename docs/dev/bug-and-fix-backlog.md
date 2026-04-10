# Bug & Fix Backlog

> Maintained by: Observer
> Created: 2026-04-05
> Last updated: 2026-04-10 (B21-B22 added from chain task-1775801122-39f7dc)

---

## Status Legend

| Tag | Meaning |
|-----|---------|
| `OPEN` | Confirmed, not yet fixed |
| `FIXED` | Fix committed to main |
| `WONTFIX` | By design or deferred indefinitely |

---

## Fixed Bugs

| ID | Description | Fix Commit | Date |
|----|-------------|------------|------|
| D1 | Executor stops claiming after initial batch | e9506c0 | 2026-03-31 |
| D2 | PM max_turns=10 instead of 60 | 5b09ad0 | 2026-03-31 |
| D3 | SERVER_VERSION blocks auto_chain after merge | 942b5de | 2026-03-31 |
| D4 | Duplicate retry task creation | 7d96c74 | 2026-03-31 |
| D5 | Dirty workspace gate blocks auto_chain (.claude/ paths) | 1ea497f | 2026-03-31 |
| D6 | Merge task fails without _branch/_worktree metadata | 20baea3 | 2026-03-31 |
| D7 | Coordinator duplicate reply | c931792 | 2026-03-31 |
| B1/B6 | auto_chain dispatch silently fails / reports dispatched:true | 8652f51 | 2026-04-05 |
| B2 | skip_version_check no access control or audit | efd7740 | 2026-04-05 |
| B3 | Version gate only at dispatch, not task_create | abc9795 | 2026-04-05 |
| B4 | Executor CLI hangs on dev/qa tasks | dd5d940 | 2026-04-05 |
| B5 | DB lock on task_complete (intermittent) | a413b9d | 2026-04-05 |
| B7 | Deploy restart silent fail | ac873e9 | 2026-04-05 |
| B8 | _gate_checkpoint blocks docs/dev/ as unrelated | 1f080bf | 2026-04-07 |
| B9 | Gate retry prompt lacks test failure detail | 6ffa422 | 2026-04-07 |
| B10 | Executor worktree fallback contaminates main tree | 3ffe09a | 2026-04-07 |
| B11 | ServiceManager does not consume restart signal | eff196f | 2026-04-08 |
| B12 | KeyError 'reason' in executor run_once after task_complete | ee9d9bb | 2026-04-09 |
| B13 | Dead tester.yaml + ungoverned YAML configs (G7 combined) | 9faa28a | 2026-04-09 |
| B14 | Claude CLI gets empty stdin — communicate() missing input= | d71baa6 | 2026-04-09 |
| B15 | Version gate blocks on worktree dirty files | 44ab315 | 2026-04-09 |
| B16 | No retry for version gate blocks (transient dirty) | 8f84d82 | 2026-04-10 |
| B17 | task.completed event publishes after version gate | 8f84d82 | 2026-04-10 |
| B18 | API task_create missing task.created event | 0235786 | 2026-04-10 |
| B19 | Governance version stale after commits | 6810a37 | 2026-04-10 |
| B20 | Clean staged/untracked leaks before merge | 2bd20f9 | 2026-04-10 |
| G4 | PM doc_impact not auto-populated from graph | 272dfa6 | 2026-04-07 |
| G5 | Retry prompt missing gate scope rules | 6ffa422 | 2026-04-07 |
| G6 | Graph lookup not bidirectional for doc targets | 272dfa6 | 2026-04-07 |
| G7 | config/roles/*.yaml not in acceptance graph | 9faa28a | 2026-04-09 |
| G8 | related_nodes not auto-populated from graph | 8f84d82 | 2026-04-10 |
| G9 | Observer SOP for manual task metadata | 79f9c39 | 2026-04-10 |
| G10 | Graph rebuild mapping updated | 79f9c39 | 2026-04-10 |
| O2 | Version gate filter worktree dirty files | 44ab315 | 2026-04-09 |
| O3 | Governance dynamic version read (no restart) | 6810a37 | 2026-04-10 |

---

## Open Items (P3 — low priority, next session)

### B21: 并发 merge 竞争 [OPEN] [P2]

- **Status**: Open. Idempotent guard catches it, but race window exists.
- **Symptom**: 多个 executor 同时尝试 ff-only merge main，首次失败需重试。幂等守卫兜住但有竞争窗口。
- **Discovered**: chain task-1775801122-39f7dc, task-1775801420
- **File**: `agent/governance/merge.py` (推测) — merge 幂等锁机制

### B22: 任务扇出 bug [OPEN] [P2]

- **Status**: Open. Extra tasks complete safely in replay mode but waste resources.
- **Symptom**: dispatcher 对下游任务（merge/gatekeeper/deploy/qa）重复派发，预期各 1 个但实际产生多个。
- **Discovered**: chain task-1775801122-39f7dc（与 B21 同一 chain）
- **File**: `agent/governance/auto_chain.py` — dispatch 去重逻辑

### O1: Consolidate runtime context as single source of truth [OPEN] [P3]

- **Status**: Phase 1 complete (B17+B18 fixed events flow). Phase 2-3 remaining.
- **Phase 2**: Builder functions read from chain_context with metadata fallback.
- **Phase 3**: Remove metadata propagation (`{**metadata}`) from builders.
- **Effort**: Medium. Not blocking — metadata propagation works as primary path.
- **File**: `agent/governance/auto_chain.py`, `agent/governance/chain_context.py`

### G1: Dirty-workspace root cause classification [OPEN] [P3]

- Gate blocks on dirty but doesn't classify why (worktree vs staged vs stale).
- Low priority — B15 already filters the main false positive source.

### G2: Pre-flight advisory at task_create [OPEN] [P3]

- Manual task_create has no dirty-workspace warning. Low priority.

### G3: Chain context bypass tracking [OPEN] [P3]

- No audit trail for gate bypass flags. Low priority.

### Stale docs (minor) [OPEN] [P3]

- `docs/roles/*.md` (coordinator, dev, qa, pm) — minor behavioral notes from B10/B12.
- Low priority — core docs (auto-chain.md, executor-api.md, tester.md, manual-fix-sop.md) are current.

---

## Test Count

963 tests pass, 2 pre-existing failures (test_e3_write_index_status, test_valid_test_success_accepted).
