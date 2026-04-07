# Bug & Fix Backlog

> Maintained by: Observer
> Created: 2026-04-05
> Last updated: 2026-04-07 (B1-B7 FIXED, B8-B10 + G4-G6 added from Step 7 observation)

---

## Status Legend

| Tag | Meaning |
|-----|---------|
| `OPEN` | Confirmed, not yet fixed |
| `FIXED` | Fix committed |
| `WONTFIX` | By design or deferred indefinitely |
| `WORKAROUND` | Has temporary bypass, root fix pending |

---

## Active Bugs

### B1: auto_chain dispatch silently fails on dirty workspace [OPEN] [P0]

- **Symptom**: `task_complete` returns `auto_chain.dispatched: true` but no next-stage task is created.
- **Root cause**: `_gate_version_check()` in auto_chain.py L1416-1486 reads `dirty_files` from DB. Non-.claude dirty files cause gate to return False. The dispatch thread catches the block but does not surface it to the task_complete response.
- **Reproduced**: 3 times in 2026-04-05 session (PM->Dev, Dev->Test, QA->next all silently blocked by `server.py` being staged).
- **Impact**: Every chain breaks silently after any stage completes. Observer must manually create each next-stage task.
- **Related**: D5 fix (1ea497f) filtered `.claude/` paths but didn't fix the core issue of silent failure.
- **Fix proposal**:
  1. `task_complete` response should include `gate_blocked: true` + `gate_reason` when dispatch is attempted but blocked.
  2. auto_chain dispatch should log WARNING (not just debug) when gate blocks.
  3. Consider: gate should distinguish "dirty from pending feature work" vs "dirty from unknown manual edits".

### B2: skip_version_check has no access control or audit [OPEN] [P1]

- **Symptom**: Any task_create caller can set `metadata.skip_version_check: true` to bypass version gate entirely.
- **Root cause**: auto_chain.py L1432 does a bare `metadata.get("skip_version_check")` with no authorization, no audit log, no rate limit.
- **Impact**: Version gate can be circumvented trivially. Bypass leaves no trace in governance DB. Observer bypass during 2026-04-05 session was not audited.
- **Contrast**: `reconciliation_lane` bypass (L1440-1444) correctly has: allowed_lanes whitelist, observer_authorized check, audit_action logged. `skip_version_check` has none of these.
- **Fix proposal**:
  1. Require `operator_id` + `bypass_reason` when `skip_version_check` is set.
  2. Write audit row: `INSERT INTO governance_events (action='version_gate_bypass', operator, reason, task_id, timestamp)`.
  3. Add bypass counter per project; alert if >3 bypasses in 24h.

### B3: Version gate only enforces at auto_chain dispatch, not at task_create [OPEN] [P1]

- **Symptom**: Dirty workspace blocks auto_chain from creating next-stage tasks, but manual `task_create` + `skip_version_check` bypasses entirely.
- **Root cause**: `_gate_version_check()` is only called inside auto_chain dispatch logic. `task_create` endpoint has no gate check.
- **Impact**: Gate is a recommendation, not an enforcement. Any API caller can create tasks in a dirty state.
- **Fix proposal**:
  1. Add optional gate check in `task_create` handler: if dirty workspace and no bypass flag, return warning (not block, to avoid breaking emergency flows).
  2. Or: tag tasks created during dirty state with `created_during_dirty: true` so downstream stages know.

### B4: Executor CLI hangs on dev/qa tasks (progress stays 0%) [OPEN] [P2]

- **Symptom**: Executor claims dev/qa tasks, spawns Claude CLI subprocess, but progress never updates from 0%. Lease expires, task stays in `claimed` state.
- **Observed**: dev task (task-1775409544-abc75b) hung for >5min. qa task (task-1775409979-bd9fe6) hung for >4min. test task succeeded normally (~50s).
- **Root cause**: Unknown. Possible causes:
  - dev/qa role prompts are too large for CLI subprocess
  - CLI subprocess hits permission/tool restriction
  - Worktree isolation not working (dev task needs file write access)
- **Impact**: Executor cannot autonomously complete dev or qa stages. Observer must intervene.
- **Fix proposal**: Add executor CLI subprocess logging (stdout/stderr capture to file for post-mortem).

### B5: DB lock on task_complete (intermittent) [WORKAROUND] [P2]

- **Symptom**: `task_complete` returns `"error": "database is locked"`.
- **Reproduced**: 1 time during QA complete in 2026-04-05 session. Retry after 5s succeeded.
- **Root cause**: SQLite WAL lock contention between executor worker thread and MCP request handler.
- **Workaround**: Retry after short delay (3-5s).
- **Fix proposal**: Add retry-with-backoff in task_complete handler (up to 3 retries, 1s/2s/4s).

### B6: auto_chain dispatch reports dispatched:true when actually blocked [OPEN] [P0]

- **Symptom**: `task_complete` response contains `auto_chain: {dispatched: true}` but no task was created.
- **Root cause**: The dispatch function returns True before the gate check, or the gate check failure is caught and swallowed.
- **Impact**: Caller (executor or observer) has no way to know the chain broke. Must poll task_list to discover the gap.
- **Related**: B1 (same root cause, different symptom surface).
- **Fix proposal**: `auto_chain.dispatched` should be `false` when gate blocks, with `gate_reason` field.

---

## Fixed Bugs (Reference)

| ID | Description | Fix Commit | Date |
|----|-------------|------------|------|
| D1 | Executor stops claiming after initial batch | e9506c0 | 2026-03-31 |
| D2 | PM max_turns=10 instead of 60 | 5b09ad0 | 2026-03-31 |
| D3 | SERVER_VERSION blocks auto_chain after merge | 942b5de | 2026-03-31 |
| D4 | Duplicate retry task creation | 7d96c74 | 2026-03-31 |
| D5 | Dirty workspace gate blocks auto_chain (.claude/ paths) | 1ea497f | 2026-03-31 |
| D6 | Merge task fails without _branch/_worktree metadata | 20baea3 | 2026-03-31 |
| D7 | Coordinator duplicate reply | c931792 | 2026-03-31 |
| B10 | Executor worktree fallback contaminates main tree | 3ffe09a | 2026-04-07 |
| B8 | _gate_checkpoint blocks docs/dev/ as unrelated | 1f080bf | 2026-04-07 |
| B9 | Gate retry prompt lacks test failure detail | 6ffa422 | 2026-04-07 |
| G5 | Retry prompt missing gate scope rules | 6ffa422 | 2026-04-07 |
| G4 | PM doc_impact not auto-populated from graph | 272dfa6 | 2026-04-07 |
| G6 | Graph lookup not bidirectional for doc targets | 272dfa6 | 2026-04-07 |

---

## Fix Priority Matrix

```
            Impact
            High          Medium        Low
Effort  ┌─────────────┬─────────────┬───────────┐
Easy    │ B6 (return  │ B5 (retry)  │           │
        │ correct     │             │           │
        │ dispatched) │             │           │
        ├─────────────┼─────────────┼───────────┤
Medium  │ B1 (gate    │ B2 (audit)  │           │
        │ feedback)   │ B3 (create  │           │
        │             │ gate check) │           │
        ├─────────────┼─────────────┼───────────┤
Hard    │ B4 (executor│             │           │
        │ CLI debug)  │             │           │
        └─────────────┴─────────────┴───────────┘

Recommended fix order: B6 → B1 → B5 → B2 → B3 → B4
```

---

## Design Gaps (Not Bugs, But Missing Features)

### G1: No dirty-workspace root cause classification

- **Current**: Gate sees `dirty_files` -> block. No distinction between "pending feature commit" vs "unknown manual edit" vs "stale node refs".
- **Desired**: Gate should classify dirty cause and suggest action:
  - Untracked new files -> "commit pending feature"
  - Staged changes -> "complete pending commit"
  - Graph refs stale -> "run reconcile"
  - Mixed -> "commit first, then reconcile if needed"

### G2: No "pre-flight before task_create" advisory

- **Current**: Version gate only fires at auto_chain dispatch time. Manual task_create has no warning.
- **Desired**: `task_create` returns advisory `warnings: ["dirty workspace: server.py uncommitted"]` so caller can decide.

### G3: Chain context doesn't track bypass history

- **Current**: Chain events don't record which tasks used bypass flags.
- **Desired**: Chain audit trail includes all bypass events, enabling post-mortem "this chain had 3 gate bypasses".

---

## New Bugs (from Step 7 observation, 2026-04-07)

### B8: _gate_checkpoint blocks docs/dev/ files as "unrelated" [FIXED] [P1]

- **Symptom**: Any dev task that moves/creates files in `docs/dev/` (non-governed) gets blocked by checkpoint gate: "Unrelated files modified: [docs/dev/archive/...]"
- **Root cause**: `_gate_checkpoint` unrelated-file loop (auto_chain.py ~line 1840) doesn't skip `docs/dev/` paths. `_is_dev_note()` is imported and used at line 1909 (doc consistency check) but NOT in the unrelated-file loop.
- **Fix**: Add `if _is_dev_note(f): continue` before `unrelated.append(f)`. 2-line change.
- **Discovered**: Step 7 observation task — doc reorganization workflow. Gate correctly blocked but `docs/dev/` should be exempt.
- **File**: `agent/governance/auto_chain.py`
- **Executor produced correct fix** (worktree `dev-task-1775571887-b94c2a`, commit `cc71cc2`) but chain couldn't complete due to B9+B10.

### B9: Gate retry prompt lacks test failure detail [FIXED] [P1]

- **Symptom**: When `_gate_checkpoint` blocks with "Dev tests failed: N failures", the retry dev task prompt only says "Dev tests failed: 1 failures" — no test name, no error message, no stack trace.
- **Root cause**: `_gate_checkpoint` returns `(False, f"Dev tests failed: {failed} failures")` without including `test_results` detail. The retry prompt builder (`_build_dev_prompt` retry path) only receives `gate_reason` string, not the structured result.
- **Impact**: Retry dev agent is "blind" — must re-run all tests to discover which one failed. This wastes ~5 minutes per retry and causes scope creep (dev agent modifies unrelated files trying to fix unknown failure).
- **Fix proposal**:
  1. `_gate_checkpoint` should include failed test name/error in gate_reason string
  2. Retry prompt should carry forward `test_results` from previous attempt
- **File**: `agent/governance/auto_chain.py` (gate + retry prompt builder)

### B10: Executor worktree fallback silently contaminates main tree [FIXED] [P0]

- **Symptom**: After dev task workflow, main tree has staged changes (`git status` shows `M agent/governance/auto_chain.py` etc.) that no one committed. These changes block subsequent version gate checks.
- **Root cause**: `executor_worker._execute_task` line 207-217: when `_create_worktree()` fails, `execution_workspace` falls back to `self.workspace` (main tree) **silently**. Claude CLI then writes directly to main tree. Executor's `git add` (line 352) stages changes in main's index.
- **Evidence**: Three different executor workers (PID 44892, 59308, 31808) claimed consecutive dev tasks. Main tree index was modified at 11:08 (11 min after last worktree commit). 4 staged files include `test_e2e_coordinator.py` and `test_test_report_gate.py` — not in any task's target_files.
- **Impact**: Main tree pollution → dirty workspace → version gate blocks all future chains → cascading failure.
- **Fix proposal**:
  1. `_create_worktree` failure should be FATAL for dev tasks (return error, don't fall back)
  2. If fallback is needed, require explicit `allow_main_tree=true` in metadata
  3. Log WARNING when falling back to main tree
- **File**: `agent/executor_worker.py`

---

## New Gaps (from Step 7 observation)

### G4: PM does not use graph impact to populate doc_impact [FIXED]

- **Symptom**: PM receives graph impact data (via 6d injection into prompt) but outputs `doc_impact: {}` even when graph shows `related_docs: [auto-chain.md, gates.md]`.
- **Impact**: 5a `_gate_post_pm` correctly detects the gap (writes `doc_gap_observation` audit), but the doc information doesn't flow to dev prompt. Dev agent doesn't know which docs to update.
- **Root cause**: 6d injects graph impact as text in PM prompt, but PM doesn't parse it into structured `doc_impact` output. PM treats it as context, not as a field to populate.
- **Fix proposal**: Either (a) auto-populate `doc_impact` from graph in `_gate_post_pm` if PM left it empty, or (b) strengthen PM prompt to explicitly require copying graph-linked docs into `doc_impact`.
- **File**: `agent/governance/auto_chain.py` (`_gate_post_pm` or `_build_dev_prompt`)

### G5: Retry dev prompt doesn't explain gate scope rules [FIXED]

- **Symptom**: Dev agent on retry modifies files outside `target_files` (evidence.py, project_service.py, state_service.py) — scope creep. Gate correctly blocks, but the next retry has the same problem.
- **Root cause**: Retry prompt says "Fix the issue described above and retry" but doesn't explain that `_gate_checkpoint` enforces `changed_files ⊆ target_files + test_files + doc_impact.files`. Dev agent doesn't know the constraint.
- **Fix proposal**: Inject gate rules into retry prompt: "IMPORTANT: checkpoint gate only allows changes to these files: {allowed_list}. Any other files will be blocked."
- **File**: `agent/governance/auto_chain.py` (retry prompt builder)

### G6: _get_graph_doc_associations doesn't work for doc-only tasks [FIXED]

- **Symptom**: 5a `_gate_post_pm` observation doesn't fire when `target_files` are all docs (no code). Graph lookup does `primary ∩ target_files` which is empty for doc paths.
- **Root cause**: `_get_graph_doc_associations` only checks `primary` field (code files). For doc-only tasks, should also check `secondary` field (docs) — this is the reverse direction that `match_secondary=True` in ImpactAnalyzer already supports.
- **Fix proposal**: Replace `_get_graph_doc_associations` with `ImpactAnalyzer.analyze(match_secondary=True)` call.
- **File**: `agent/governance/auto_chain.py`
