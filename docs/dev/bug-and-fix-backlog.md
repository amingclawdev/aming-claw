# Bug & Fix Backlog

> Maintained by: Observer
> Created: 2026-04-05
> Last updated: 2026-04-09 (full audit: B1-B14 status reconciled, G1-G10 added, O1-O3 optimization)

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
| G4 | PM doc_impact not auto-populated from graph | 272dfa6 | 2026-04-07 |
| G5 | Retry prompt missing gate scope rules | 6ffa422 | 2026-04-07 |
| G6 | Graph lookup not bidirectional for doc targets | 272dfa6 | 2026-04-07 |
| G7 | config/roles/*.yaml not in acceptance graph | 9faa28a | 2026-04-09 |

---

## Open Bugs

### B15: Version gate blocks chain after every dev task [OPEN] [P0]

- **Symptom**: Every dev task completion triggers version gate block ("dirty workspace N files"). Auto-chain stops, no test/QA task created. Observer must manually create next stage.
- **Root cause**: Executor stages changed files in worktree via `git add`. Governance's periodic git sync sees worktree changes as dirty files in `project_version.dirty_files`. Version gate reads dirty_files, finds non-zero, blocks.
- **Impact**: No chain ever completes autonomously through dev→test without manual intervention. This is the #1 process friction.
- **Related**: D5 filtered `.claude/` paths, but worktree changes still bleed through.
- **Fix proposal**: Version gate should filter files that exist in active worktrees, OR the executor's git sync should only report files dirty in main workspace (not worktrees).
- **File**: `agent/governance/auto_chain.py` (`_gate_version_check`)

### B16: No retry mechanism for version gate blocks [OPEN] [P0]

- **Symptom**: When version gate blocks (`dirty workspace` or `HEAD != CHAIN_VERSION`), the chain returns `{"gate_blocked": True}` and dies. No retry scheduled, no delayed re-check.
- **Root cause**: Version gate is a pre-gate with no retry logic. Checkpoint gate has retry (up to 2), but version gate is all-or-nothing.
- **Impact**: Every transient dirty state (worktree files, governance restart pending) permanently kills the chain. Observer must intervene.
- **Fix proposal**: Add delayed retry (e.g. wait 30s then re-check) or "wait for clean" loop for version gate blocks that are likely transient.
- **File**: `agent/governance/auto_chain.py` (`_do_chain`)

### B17: task.completed event publishes AFTER version gate [OPEN] [P1]

- **Symptom**: `chain_events` table has zero entries since 2026-04-05. ChainContextStore never receives task completions.
- **Root cause**: `_publish_event("task.completed")` is at line 915 in `_do_chain`, AFTER `_gate_version_check` at line 872. When gate blocks (returns at line 910), the event is never published. Since gate blocks ~95% of the time (B15), almost no completions reach chain_context.
- **Impact**: Runtime context store is empty. Crash recovery has nothing to replay. Chain audit trail missing for 4 days.
- **Fix proposal**: Move `_publish_event("task.completed")` BEFORE the version gate check. Task completion is a fact regardless of whether the chain progresses.
- **File**: `agent/governance/auto_chain.py` (line 915 → move to ~line 870)

### B18: User API task_create does not publish task.created event [OPEN] [P2]

- **Symptom**: Root PM tasks created via `POST /api/task/create` are invisible to chain_context until they complete. Chain context bootstraps them on completion as a workaround.
- **Root cause**: `handle_task_create()` in server.py creates the task in DB but never publishes `task.created` to event bus. Only auto-chain publishes `task.created` (line 1351).
- **Impact**: Chain context has incomplete view of chains. Recovery misses root tasks.
- **Fix proposal**: Add `_publish_event("task.created", {...})` in `handle_task_create()` after DB insert.
- **File**: `agent/governance/server.py`

### B19: Governance server not restarted by deploy [OPEN] [P1]

- **Symptom**: After merge, governance server version stays stale. Version gate blocks all subsequent chains with "server version != git HEAD".
- **Root cause**: Deploy task calls `restart_executor()` (signal file) and `rebuild_governance()` (Docker). But deploy task never runs because chain dies at merge stage (B15/B16). Even when deploy runs, governance restart requires Docker or manual process kill — not covered by ServiceManager signal.
- **Impact**: Governance must be manually restarted after every merge. Combined with B15/B16, this means the full chain never completes autonomously.
- **Fix proposal**: Either (a) add governance to ServiceManager signal mechanism, or (b) governance reads version from git dynamically instead of caching at startup.
- **File**: `agent/deploy_chain.py`, `agent/service_manager.py`

---

## Design Gaps

### G1: No dirty-workspace root cause classification [OPEN] [P3]

- **Current**: Gate sees `dirty_files` → block. No distinction between worktree files, staged changes, or stale refs.
- **Desired**: Classify dirty cause and suggest action.

### G2: No pre-flight advisory at task_create [OPEN] [P3]

- **Current**: Version gate only fires at auto_chain dispatch. Manual task_create has no warning.
- **Desired**: `task_create` returns advisory warnings.

### G3: Chain context doesn't track bypass history [OPEN] [P3]

- **Current**: Chain events don't record bypass flags.
- **Desired**: Audit trail for all gate bypasses.

### G8: PM does not populate related_nodes from graph [OPEN] [P1]

- **Symptom**: `related_nodes` is empty throughout every chain. PM doesn't set it, auto-chain doesn't infer it from `target_files`. The qa_pass graph gate and gatekeeper have nothing to check against.
- **Impact**: Graph node verification is bypassed for every chain. Nodes can reach merge without qa_pass verification.
- **Root cause**: `_build_dev_prompt` does NOT infer `related_nodes` from graph. PM prompt doesn't require it. Auto-chain passes empty list.
- **Fix proposal**: In `_gate_post_pm` or `_build_dev_prompt`, auto-populate `related_nodes` from graph by looking up which nodes own the `target_files`.
- **File**: `agent/governance/auto_chain.py`

### G9: Observer SOP missing for manual task creation [OPEN] [P2]

- **Symptom**: When observer must manually create a task to bypass a gate block, metadata chain breaks (target_files, changed_files, related_nodes all missing).
- **Impact**: Downstream stages (QA, gatekeeper) have no graph data to verify.
- **Root cause**: No documented SOP for manual task creation. Observer doesn't know to fetch parent task metadata from DB.
- **Fix proposal**: Add to `docs/governance/manual-fix-sop.md`: "When manually creating a task, fetch parent task's metadata_json and copy target_files, changed_files, related_nodes, verification, test_files, _worktree, _branch."
- **File**: `docs/governance/manual-fix-sop.md`

### G10: Graph not rebuilt after rebuild_graph.py changes [OPEN] [P2]

- **Symptom**: `scripts/rebuild_graph.py` was updated (G7) to scan YAML configs, but `graph.json` was never rebuilt. YAML configs not in graph.
- **Root cause**: PM AC said "running rebuild_graph.py includes YAML" but dev only modified the script, didn't execute it. No AC verified actual `graph.json` contents.
- **Fix proposal**: Run `python scripts/rebuild_graph.py` + `python scripts/apply_graph.py`. Add AC to future graph-related PRDs: "graph.json contains expected secondary files."
- **File**: `shared-volume/codex-tasks/state/governance/aming-claw/graph.json`

---

## Optimizations (Architecture)

### O1: Consolidate to runtime context as single source of truth [OPEN] [P1]

- **Current**: Two parallel data paths — metadata propagation (`{**metadata}` in `create_task`) and event bus → chain_context. Metadata is primary, chain_context is broken (B17).
- **Problem**: Metadata propagation has no role-based filtering, grows unbounded, breaks on manual task creation.
- **Proposed**: Use `ChainContextStore.get_chain(task_id, role)` as the single source. Already has role-based visibility (`ROLE_VISIBLE_STAGES`, `ROLE_RESULT_FIELDS`), stage history, and DB persistence.
- **Blockers**: B17 (events not published), B18 (API tasks invisible), builder functions read from metadata not context.
- **Migration**: Phase 1: fix B17+B18 (events flow). Phase 2: builders read context with metadata fallback. Phase 3: remove metadata propagation.
- **File**: `agent/governance/auto_chain.py`, `agent/governance/chain_context.py`

### O2: Version gate should filter worktree dirty files [OPEN] [P1]

- **Current**: `_gate_version_check` reads `project_version.dirty_files` which includes files from active worktrees.
- **Proposed**: Filter dirty files against active worktrees before blocking. If all dirty files are in worktrees, don't block.
- **Related**: B15 (every dev task triggers block).
- **File**: `agent/governance/auto_chain.py`

### O3: Governance should read version from git dynamically [OPEN] [P2]

- **Current**: `SERVER_VERSION` cached at startup. Any commit after startup causes mismatch.
- **Proposed**: Read `git rev-parse --short HEAD` on each version check, or re-read on each API call.
- **Related**: B19 (governance not restarted by deploy).
- **File**: `agent/governance/server.py`

---

## Stale Docs (from audit 2026-04-09)

15 manual commits left graph-linked docs stale. Highest priority:

| Doc | Times Skipped | Status |
|-----|---------------|--------|
| `docs/governance/auto-chain.md` | 5x | OPEN — B8/B9/G4/G5/G6/B1 changes not documented |
| `docs/api/executor-api.md` | 3x | OPEN — L4/B10/B12 changes not documented |
| `docs/roles/tester.md` | 4x | FIXED (9faa28a) — rewritten for script-based execution |
| `docs/roles/*.md` (coordinator, dev, qa, pm) | 2x each | OPEN — minor behavioral notes missing |
| `docs/deployment.md` | 1x | OPEN — B7 changes not documented |

---

## Priority Summary

```
P0 (blocking autonomy):
  B15  Version gate blocks after every dev task
  B16  No retry for version gate blocks

P1 (chain quality):
  B17  task.completed not published (chain_context dead)
  B19  Governance not restarted by deploy
  G8   related_nodes not auto-populated from graph
  O1   Consolidate to runtime context single source
  O2   Filter worktree dirty files from version gate

P2 (observability + process):
  B18  API task_create missing task.created event
  G9   Observer SOP for manual task creation
  G10  Graph.json not rebuilt after script change
  O3   Governance dynamic version read

P3 (low priority):
  G1   Dirty workspace classification
  G2   Pre-flight advisory
  G3   Bypass history tracking
  Stale docs (auto-chain.md, executor-api.md, role docs)
```
