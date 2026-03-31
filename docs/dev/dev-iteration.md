# Dev Stage — Predict / Verify Iteration Log

> Created: 2026-03-30 | Status: **Iteration 1 complete — mismatch found**
> Prerequisites: Coordinator stable, PM stable, Observer cancel/release available
> Goal of this file: record Dev-stage prediction, actual execution, mismatches, and next fixes

---

## 1. Current Objective

Use the existing Dev stage as-is to validate whether the current PM output for the Python compatibility task can flow through:

`PM result -> _build_dev_prompt -> Dev execution -> _gate_checkpoint -> Test task`

Target task scope from PM:
- `agent/executor_worker.py`
- `agent/governance/memory_service.py`
- `agent/telegram_gateway/gateway.py`

Target verification from PM:
- `pytest agent/tests/test_coordinator_decisions.py agent/tests/test_memory_backend.py agent/tests/test_verify_spec.py`

---

## 2. Memory / Pitfall Context Used

Confirmed via nginx-proxied memory API:
- `GET http://localhost:40000/api/mem/aming-claw/query?kind=pitfall`
- `GET http://localhost:40000/api/mem/aming-claw/search?q=dev%20stage%20gate%20pitfall&top_k=8`

Relevant active pitfalls before Dev run:
1. `Gate blocked at dev: No files changed`
2. `MCP subprocess log.info() deadlock`
3. `ai_lifecycle Popen + poll deadlock on Windows`
4. `ServiceManager stdout=PIPE deadlock`
5. `Nginx :40000 is the correct host-side governance ingress`

Additional context discovered during this iteration:
1. Dev system prompt says it works in an isolated git worktree, but current Codex run is actually launched with `-C C:\Users\z5866\Documents\amingclaw\aming_claw` on the main workspace
2. `_gate_checkpoint` compares `changed_files` against repo-wide git diff, so a dirty workspace will contaminate Dev results

---

## 3. Prediction Before Running Dev

### Expected prompt/data path

Current code path:
- `agent/governance/auto_chain.py::_build_dev_prompt`
- `agent/executor_worker.py::_build_prompt(task_type="dev")`
- `agent/governance/auto_chain.py::_gate_checkpoint`

Expected behavior if the stage were healthy:
1. Dev receives the three PM target files plus concrete requirements and ACs
2. Dev edits only those files plus the specified tests
3. Dev returns strict JSON with a narrow `changed_files` set
4. `_gate_checkpoint` sees only target files and passes
5. Auto-chain creates a `test` task

### Predicted mismatches from code inspection

#### P1. PM requirements are likely lost before Dev

Evidence:
- `_build_dev_prompt()` reads `requirements = prd.get("requirements", [])`
- Current PM result stores `requirements` at the top level, not inside `prd`

Predicted effect:
- Dev prompt will contain `requirements: []`
- Dev loses the highest-signal implementation constraints from PM

#### P2. `related_nodes` shape is likely wrong for downstream gates

Evidence:
- `_build_dev_prompt()` sets:
  - `related_nodes = result.get("proposed_nodes", metadata.get("related_nodes", []))`
- `proposed_nodes` is a list of node objects, not node-id strings
- `_check_nodes_min_status()` expects iterable node ids

Predicted effect:
- Later node checks may misbehave or block once node verification is enforced

#### P3. Dirty workspace will likely poison `changed_files`

Evidence:
- Dev runs in the main repo working tree, not a fresh worktree
- `_execute_task()` overwrites AI-reported `changed_files` with repo git diff
- `_gate_checkpoint()` rejects files outside `metadata.target_files`

Predicted effect:
- Even if Dev edits the correct three files, `_gate_checkpoint` will see many unrelated modified files and block

---

## 4. Verify Run

### Observer actions

Created manual Dev task for predict/verify:
- `task-1774845277-9ff351`

Released via nginx:
- `POST http://localhost:40000/api/task/aming-claw/release`

Ran worker manually:
- `python3.13 -m agent.executor_worker --project aming-claw --once`

### Key logs

Task log:
- `shared-volume/codex-tasks/logs/timing-task-1774845277-9ff351.txt`

Session logs:
- `shared-volume/codex-tasks/logs/input-dev-1774845305-db3877.txt`
- `shared-volume/codex-tasks/logs/output-dev-1774845305-db3877.txt`
- `shared-volume/codex-tasks/logs/complete-task-1774845277-9ff351.txt`

### Actual timing

Observed:
- `build_prompt`: 0.6s
- `create_session`: 1.2s
- `wait_for_output`: 197.2s
- total to parsed result: about 198.7s

### Actual Dev output

The Dev agent itself returned a narrow, reasonable JSON payload:
- changed only the 3 target source files + 3 requested test files
- used `Optional[...]` replacements for `dict | None` / `int | None`
- added focused regression tests
- reported targeted verification

This is visible in:
- `output-dev-1774845305-db3877.txt`

### Actual stage result recorded by executor

Executor then replaced AI-reported `changed_files` with repo git diff ground truth and produced a 38-file changed set including:
- `.mcp.json`
- many unrelated `agent/*`
- multiple docs
- `governance.db`

As a result, auto-chain blocked at checkpoint and created retry task:
- `task-1774845512-f2b90f`
- status: `observer_hold`

Gate reason:
- `Unrelated files modified: [...]`

---

## 5. Prediction vs Actual

### P1. PM requirements lost

Predicted:
- Dev prompt would lose requirements

Actual:
- confirmed

Evidence from `input-dev-1774845305-db3877.txt`:
- `requirements: []`

Conclusion:
- `_build_dev_prompt()` is dropping top-level PM requirements

### P2. Wrong worktree assumption

Predicted:
- system prompt and runtime reality may diverge

Actual:
- confirmed

Evidence:
- system prompt says: `You work in an isolated git worktree`
- CLI command uses main repo:
  - `codex.cmd exec ... -C C:\Users\z5866\Documents\amingclaw\aming_claw`

Conclusion:
- Dev is being told an invariant that is currently false

### P3. Dirty workspace poisons checkpoint

Predicted:
- checkpoint would likely block on unrelated files

Actual:
- confirmed

Evidence:
- AI returned 6-file `changed_files`
- executor replaced that with 38 files from repo git diff
- `_gate_checkpoint` blocked with `Unrelated files modified`

Conclusion:
- current Dev stage is not verifiable in a dirty shared workspace

### P4. `related_nodes` forwarding shape mismatch

Predicted:
- future node checks may break because `proposed_nodes` objects are forwarded as `related_nodes`

Actual:
- not yet the blocking issue in this run

Reason:
- checkpoint already failed earlier on unrelated files

Conclusion:
- still a real structural bug, just not the first blocker

---

## 6. Root Cause Summary

The first Dev-stage failure is **not** that Dev cannot solve the task.

The real blockers are orchestration-layer mismatches:
1. `Dev prompt builder` drops PM requirements
2. `Dev runtime` claims isolated worktree but runs in shared dirty repo
3. `Executor` overwrites AI-reported changed files with shared-workspace git diff
4. `Checkpoint gate` interprets shared-workspace diff as Dev task output

This means the current Dev stage is behaviorally inconsistent with its own contract.

---

## 7. Current State After Iteration 1

Observed task states:
- `task-1774845277-9ff351` — `succeeded` at execution level, but chain blocked at checkpoint
- `task-1774845512-f2b90f` — auto-retry `dev`, currently `observer_hold`

Important note:
- The workspace now also contains real edits produced by the Dev run
- These should be treated as experimental verification output until Observer decides whether to keep, discard, or replay in an isolated worktree

---

## 8. Recommended Fix Order for Iteration 2

### Fix A — restore PM requirements into Dev prompt

Change:
- `_build_dev_prompt()` should read top-level `result["requirements"]` before falling back to `prd["requirements"]`

Expected benefit:
- Dev receives the full PM implementation contract

### Fix B — introduce actual Dev worktree isolation

Change:
- run Dev in a dedicated branch/worktree, not the shared main workspace

Expected benefit:
- `git diff` reflects only Dev-task edits
- checkpoint gate becomes meaningful

### Fix C — stop forwarding `proposed_nodes` objects as `related_nodes`

Change:
- keep `related_nodes` as node-id strings only
- store `proposed_nodes` separately for observer review

Expected benefit:
- downstream node checks use the expected data shape

### Fix D — improve memory injection readability for `prd_scope`

Observed issue:
- Dev prompt currently shows:
  - `[prd_scope]`
  - `[prd_scope]`
  - `[prd_scope]`

Likely reason:
- injected memory display prefers `summary`, but `prd_scope` entries can be mostly structured data

Expected benefit:
- Dev gets useful memory context instead of blank labels

### Fix E — restore real role-specific context injection

Observed issues:
- `session_context` can be recovered from DB via `/api/context/{project_id}/load`
- but `context-snapshot` currently returns weak data for Dev
- and the snapshot was not actually injected into this Dev prompt

Concrete problems found:
- `ai_lifecycle._build_system_prompt()` currently swallows the snapshot fetch path on exception, so Dev can run without injected snapshot
- `server.py` currently parses `role` incorrectly in `context-snapshot`; `role=dev` was observed as `"d"`
- snapshot payload does not currently expose the most useful recovered fields for Dev, such as session focus / last decision / richer task-chain context

Expected benefit:
- Dev receives role-appropriate context from DB instead of relying only on prompt assembly side channels

### Fix F — add hard role restrictions for Codex, not just prompt restrictions

Observed issue:
- Claude path has explicit per-role tool shaping
- Codex path currently relies much more on prompt instructions and ambient environment capabilities

Expected benefit:
- Prevent coordinator / pm / test / qa role drift or overreach after Codex provider activation

### Fix G — align Dev memory write shape with memory service schema

Observed issue:
- Dev completion currently writes a `decision` memory entry successfully
- but `module` and `structured` do not align with `MemoryEntry.from_dict()`
- result: memory is persisted, but important structure is lost

Expected benefit:
- Dev memories become queryable by module and retain validation / task metadata

### Fix H — add Dev role specification doc

Observed issue:
- `coordinator`, `pm`, and `observer` already have role-spec markdown
- `dev` still has no equivalent `docs/dev-rules.md`
- current Dev contract is scattered across prompt text, auto-chain code, and gate logic

Expected benefit:
- single source of truth for Dev inputs, outputs, tool limits, worktree rules, verification duties, and memory-write rules

---

## 9. Additional Questions Collected After Iteration 1

### Q1. Did Dev load role-specific context after the coordinator context changes?

Answer:
- partially designed, not reliably effective

Findings:
- `Dev` is intended to fetch `/api/context-snapshot/{project_id}?role=dev&task_id=...`
- current snapshot request path exists in `ai_lifecycle.py`
- but the snapshot did not appear in the actual Dev prompt
- current server-side role parsing returns `"d"` for `role=dev`, showing a bug in query parsing

Conclusion:
- role-specific context loading is currently not trustworthy for Dev

### Q2. Was a dedicated Dev role markdown spec created?

Answer:
- no

Conclusion:
- this is now a documentation and contract gap

### Q3. Did Dev write to memory successfully?

Answer:
- yes, but only partially correctly

Findings:
- a new `decision` memory entry was written for this Dev run
- governance-side memory query can retrieve it
- but memory indexing to dbservice is not fully healthy (`flush-index` showed pending entries)
- and the write shape loses `module_id` / structured fields because executor payload does not align with `MemoryEntry.from_dict()`

Conclusion:
- local governance memory write succeeded
- semantic/dbservice side cannot be assumed complete
- Dev memory schema needs repair

### Q4. Why did git worktree isolation not take effect?

Answer:
- because the current governance workflow does not execute through the code path that implements worktree creation

Findings:
- worktree logic exists in `agent/mcp/executor.py`
- current workflow run used `agent/executor_worker.py` + `agent/ai_lifecycle.py`
- that path passes the main workspace directly into `codex exec -C ...`
- current Dev prompt claims isolated worktree behavior that the runtime does not enforce

Conclusion:
- worktree isolation is a design-intent / alternate-executor feature, not an active behavior in the current governance path

### Q5. Did Dev update node-related docs while modifying code?

Answer:
- not as part of this task's actual Dev output

Findings:
- PM explicitly set `doc_impact.files = []` for this first-pass compatibility task
- actual AI-reported Dev output changed only 3 target source files + 3 tests
- docs appearing in checkpoint failure came from the dirty shared workspace, not this task's intentional Dev output

Conclusion:
- current environment prevents reliable attribution of doc changes without worktree isolation

### Q6. Does `_gate_checkpoint()` need adjustment?

Answer:
- yes

Current high-priority problems:
1. git diff baseline is taken from the shared repo instead of task-isolated state
2. unrelated-file rule is too strict for legitimate test/doc/helper updates
3. doc checking relies too heavily on automatic inference instead of PM `doc_impact`
4. `related_nodes` currently risks receiving `proposed_nodes` objects instead of node-id strings
5. gate cannot distinguish AI-reported file set from executor-observed contamination

### Q7. Does Codex need role-based hard restrictions similar to Claude?

Answer:
- yes

Conclusion:
- prompt-only restrictions are insufficient
- Codex also needs explicit role-based capability control

### Q8. Should Dev gain a dedicated E2E acceptance scenario?

Answer:
- yes, and it should be high priority

Suggested coverage:
1. PM -> Dev prompt contract integrity
2. role-specific context snapshot injection
3. isolated worktree execution
4. checkpoint gate behavior under dirty main workspace
5. doc-impact enforcement and allowed-file rules

### Q9. Can task/session context be restored from DB with useful information?

Answer:
- session-level context can be restored from DB
- task-level Dev snapshot currently restores too little and is not reliably injected

Findings:
- `/api/context/{project_id}/load` returns useful focus/decision data
- `/api/context-snapshot/...` currently returns weak Dev context and incorrect role echo
- actual Dev prompt did not include the snapshot block

Conclusion:
- DB recovery exists
- end-to-end usage by Dev is still degraded

---

## 10. Recommended Dev E2E Scenarios

### D1 — PM to Dev contract integrity

Verify:
- PM `requirements`, `acceptance_criteria`, `verification`, `test_files`, and `doc_impact` all arrive in the created Dev prompt/metadata

### D2 — Dev role context recovery and injection

Verify:
- `session_context` and `task_chain` can be recovered from DB
- `context-snapshot` is requested with `role=dev`
- returned snapshot is actually embedded in the Dev system prompt

### D3 — Dev isolated worktree execution

Verify:
- Dev runs in a dedicated worktree
- main workspace dirt does not contaminate task `changed_files`

### D4 — Checkpoint gate file accounting

Verify:
- gate compares against task-isolated diff
- allowed file classes include target files, declared tests, and declared docs
- unrelated files still block

### D5 — Doc impact enforcement

Verify:
- missing required docs block when PM declares them
- explicit no-doc / skip-doc cases behave as intended

### D6 — Dev memory write integrity

Verify:
- Dev writes decision memory with correct `module_id`
- structured metadata survives persistence
- dbservice indexing is either confirmed or explicitly marked pending/degraded

---

## 11. Consolidated Optimization Themes

### Theme A — Isolation

1. real Dev worktree creation and cleanup
2. task-local diff baseline
3. shared worktree propagation through test/qa/merge

### Theme B — Contract fidelity

1. preserve full PM -> Dev contract
2. inject usable role-specific context
3. keep `related_nodes` and `proposed_nodes` semantically separate
4. create `docs/dev-rules.md`

### Theme C — Gate quality

1. compare task-isolated file evidence, not shared repo dirt
2. allow legitimate test/doc/helper edits
3. prioritize PM `doc_impact` over pure inference
4. detect executor/environment contamination explicitly

### Theme D — Memory and audit

1. align Dev memory payload with memory schema
2. confirm local + semantic indexing health
3. make memory injection into Dev prompts readable and relevant

### Theme E — Role safety

1. hard role restrictions for Codex
2. auditable capability use per role
3. block or flag overreach, not just instruct against it

---

## 12. Iteration 1 Conclusion

Iteration 1 validated the method:
- predict first
- run real Dev stage
- compare logs against prediction

It also established the first concrete Dev-stage truth:

**Current Dev failure is primarily an orchestration / isolation problem, not a code-generation problem.**

The existing Dev agent can produce a plausible fix for this task, but the surrounding workflow cannot currently verify it correctly in a dirty shared workspace.

---

## 13. 2026-03-30 Session 2: Dev Contract Repair (Round 2)

### Scope chosen for Round 2

Round 2 intentionally focused on contract-layer fixes that were:
- high confidence
- fast to verify
- prerequisite to meaningful future Dev E2E work

Chosen fixes:
1. restore Dev context snapshot injection
2. fix server-side `role=dev` parsing in `context-snapshot`
3. forward top-level PM `requirements` into Dev prompt
4. separate `related_nodes` from `proposed_nodes`
5. align new memory writes with `module_id` and `structured`-aware schema

Deferred again:
1. true git worktree execution in the governance path
2. checkpoint gate redesign
3. full Dev E2E scenario implementation

### Round 2 prediction

If the patch is correct:
1. `GET /api/context-snapshot/...?...role=dev...` should return `role: "dev"` instead of `"d"`
2. snapshot payload should include useful DB-recovered context for Dev
3. `_build_system_prompt("dev", ...)` should contain:
   - `--- Base Context Snapshot ---`
   - `session_context`
   - task chain information
4. `_build_dev_prompt()` should preserve top-level PM `requirements`
5. new memory writes using `module_id` + `structured` should persist with correct module id
6. old Dev gate/worktree problem should remain unresolved (not in this scope)

### Files changed in Round 2

- `agent/governance/server.py`
- `agent/ai_lifecycle.py`
- `agent/governance/auto_chain.py`
- `agent/governance/models.py`
- `agent/governance/memory_service.py`
- `agent/executor_worker.py`
- `agent/tests/test_dev_contract_round2.py`

### What was changed

#### A. `context-snapshot` server repair

In `server.py`:
- query parsing for `role` / `task_id` now handles both string and list forms safely
- session context is loaded and included in snapshot result
- recent-memory scoring now uses recovered session focus / last decision instead of a broken `task_summary.get(...)` call

#### B. Dev snapshot injection repair

In `ai_lifecycle.py`:
- removed the broken duplicate snapshot-fetch path
- retained a single role-aware snapshot fetch
- verified the fetched snapshot is rendered into the Dev system prompt

#### C. PM -> Dev contract repair

In `auto_chain.py::_build_dev_prompt()`:
- top-level `requirements` now flow into Dev prompt
- `related_nodes` remains node-id oriented
- `proposed_nodes` is forwarded separately instead of being misused as `related_nodes`

#### D. Memory write shape repair

In `models.py` + `memory_service.py` + `executor_worker.py`:
- memory writes now use `module_id`
- `MemoryEntry.from_dict()` accepts `module` as an alias for compatibility
- `summary` can populate `applies_when`
- `structured` payload is accepted and merged into backend structured metadata for new writes

### Verification run

#### Static / unit verification

Commands run:
- `python3.13 -m pytest agent/tests/test_dev_contract_round2.py -q`
- `python3.13 -m py_compile agent/ai_lifecycle.py agent/executor_worker.py agent/governance/auto_chain.py agent/governance/server.py agent/governance/models.py agent/governance/memory_service.py`

Result:
- targeted tests: `2 passed`
- py_compile: passed

#### Runtime verification

Because `server.py` changed, governance service was rebuilt and restarted:
- `docker compose -f docker-compose.governance.yml up -d --build governance`

Post-rebuild API checks:

1. `GET /api/context-snapshot/aming-claw?role=dev&task_id=task-1774845277-9ff351`
   - verified `role: "dev"`
   - verified `session_context.current_focus = python_compatibility_import_failures`
   - verified `task_chain` is present
   - verified `recent_memories` is now non-empty

2. `_build_system_prompt("dev", ...)`
   - confirmed prompt contains:
     - `--- Base Context Snapshot ---`
     - `session_context`
     - `python_compatibility_import_failures`
     - `task_chain`

3. `_build_dev_prompt(...)`
   - confirmed prompt now contains top-level PM requirements
   - confirmed metadata keeps `related_nodes` separate from `proposed_nodes`

4. `POST /api/mem/aming-claw/write`
   - wrote new verification entries with `module_id`
   - response confirmed `module_id` persisted correctly
   - `index_status: indexed`
   - later `flush-index` returned `remaining: 0`

### Round 2 prediction vs actual

| Item | Prediction | Actual | Result |
|------|------------|--------|--------|
| role parsing | `dev` not `d` | `dev` | ✅ |
| session context in snapshot | present | present | ✅ |
| task chain in snapshot | present | present | ✅ |
| snapshot injected into Dev prompt | yes | yes | ✅ |
| PM requirements forwarded to Dev | yes | yes | ✅ |
| related_nodes/proposed_nodes separated | yes | yes | ✅ |
| new memory writes keep module_id | yes | yes | ✅ |
| semantic index backlog | improved / cleared | `remaining: 0` after check | ✅ |
| worktree isolation | still missing | still missing | expected |

### Remaining problems after Round 2

Round 2 improved contract fidelity, but did not solve the main execution-isolation issue.

Still unresolved:
1. governance-path Dev still does not create a real git worktree
2. checkpoint gate still evaluates shared-workspace diff, so dirty repo contamination risk remains
3. historical memory rows written before this repair still have missing module/structured detail
4. task-chain recovery for manually created observer tasks is present but still thin (`prompt` empty in recovered chain snapshot)

### Round 2 conclusion

Round 2 successfully repaired the "contract and context" side of Dev:
- Dev can now recover meaningful DB context
- Dev prompt construction now preserves more of the PM contract
- new memory writes have a usable schema shape

But the "execution isolation" side is still the biggest blocker.

The next meaningful round should therefore focus on:
1. true worktree creation in the current governance executor path
2. checkpoint gate diff/accounting changes
3. one real Dev E2E scenario that proves these two fixes together

---

## 14. 2026-03-30 Session 3: Dev Worktree Isolation (Round 3)

### Scope chosen for Round 3

Round 3 focused on the biggest remaining execution-layer blocker:
- actual Dev execution isolation in the current governance executor path

Chosen fixes:
1. create a real git worktree for `task_type == "dev"` in `executor_worker.py`
2. run Codex in that worktree instead of the main workspace
3. compute `changed_files` from the task worktree, not the shared repo root
4. preserve `_worktree` / `_branch` in Dev result
5. teach merge stage to understand branch/worktree metadata

### Round 3 prediction

If the patch is correct:
1. Dev timing log should show worktree creation
2. actual Dev run should no longer inherit main-workspace dirty files
3. `changed_files` should collapse from dozens of unrelated files to the task-local file set
4. the earlier `Unrelated files modified` gate failure should disappear
5. a new downstream blocker, if any, should reflect real task evidence instead of workspace contamination

### Files changed in Round 3

- `agent/executor_worker.py`
- `agent/tests/test_dev_worktree_round3.py`

### What was changed

#### A. Worktree creation in current governance executor

In `executor_worker.py`:
- Dev tasks now call `_create_worktree(task_id)`
- successful creation switches `execution_workspace` from main repo to `.worktrees/dev-{task_id}`
- Dev context now carries the actual execution workspace
- `create_session(... workspace=...)` now uses the worktree for Dev

#### B. Task-local git diff

Also in `executor_worker.py`:
- `_get_git_changed_files()` now accepts `cwd`
- Dev changed-file detection and `git add` run inside the worktree path
- result keeps `_worktree` and `_branch` for downstream propagation

#### C. Merge metadata support

`_execute_merge()` now supports:
- committing staged changes in the Dev worktree
- merging the Dev branch into main
- cleaning up worktree + branch after merge

#### D. Targeted tests

New test file:
- `agent/tests/test_dev_worktree_round3.py`

Coverage:
1. Dev session uses worktree workspace when one is created
2. git-changed-file helper respects caller-supplied cwd

### Verification run

#### Static / unit verification

Commands run:
- `python3.13 -m pytest agent/tests/test_dev_worktree_round3.py -q`
- `python3.13 -m py_compile agent/executor_worker.py`

Result:
- `2 passed`
- `py_compile` passed

#### Worktree smoke verification

Command run:
- direct Python smoke check calling `_create_worktree("round3-smoke")` then `_remove_worktree(...)`

Result:
- worktree created successfully
- directory existed
- cleanup succeeded

#### Real Dev task verification

Released pending retry Dev task:
- `task-1774845512-f2b90f`

Ran worker once:
- `python3.13 -m agent.executor_worker --project aming-claw --once`

Observed in timing log:
- `worktree: created C:\Users\z5866\Documents\amingclaw\aming_claw\.worktrees\dev-task-1774845512-f2b90f`
- `git_diff: done, 3 files`
- final `changed_files`:
  - `agent/executor_worker.py`
  - `agent/governance/memory_service.py`
  - `agent/telegram_gateway/gateway.py`

This directly contrasts with Round 1, where the same task family inherited 38 unrelated files from the dirty shared workspace.

### Round 3 prediction vs actual

| Item | Prediction | Actual | Result |
|------|------------|--------|--------|
| worktree gets created | yes | yes | ✅ |
| Dev runs outside main repo | yes | yes | ✅ |
| changed_files shrink to task-local set | yes | yes, 3 files | ✅ |
| unrelated-file contamination disappears | yes | yes | ✅ |
| next blocker reflects real task evidence | yes | yes | ✅ |

### New blocker exposed by Round 3

After isolation worked, checkpoint no longer failed on unrelated files.

The new gate failure became:
- `Dev tests failed: 1 failures`

This is a much healthier failure mode because it is about task evidence, not environment contamination.

Specific observed result:
- Dev returned `test_results.failed = 1`
- summary indicated `pytest` was unavailable in the environment and only partial verification could run
- auto-chain therefore created a new retry task:
  - `task-1774849310-8f6800`
  - status: `observer_hold`

### Remaining problems after Round 3

Round 3 solved the main workspace contamination problem, but several follow-ups remain:

1. `checkpoint gate` still trusts Dev-reported `test_results`; it now needs clearer rules for partial verification / missing pytest
2. blocked Dev tasks currently leave their worktrees behind for inspection; lifecycle cleanup policy is still undefined for gate-blocked retries
3. PM/Dev contract for test execution is still weak in this task family (`requirements` in retry prompt remains thin because retry prompt reuses old prompt text)
4. `related_nodes` in existing queued retry metadata can still contain old malformed data from pre-fix tasks

### Round 3 conclusion

Round 3 is the first round where the current governance-path Dev executor behaved like a task-isolated system rather than a shared dirty workspace.

Most important verified outcome:

**Dev worktree isolation now works in the actual governance executor path, and the old `Unrelated files modified` failure mode has been eliminated for the verified retry task.**

That means the next round should shift from isolation work to:
1. test/verification contract tightening
2. checkpoint gate semantics for partial verification
3. Dev E2E scenario that locks this isolation behavior in permanently

## 2026-03-30 Session 4: Dev Verification Contract Repair (Round 4)

### Prediction before changes

Round 3 showed a healthier failure, but the exact evidence was still wrong:
- the host environment already had `pytest`
- the pending retry task still carried a stale prompt with no `verification` block
- `Dev` therefore improvised its own narrower command and reported a misleading failure

Prediction:
1. if `verification.command` is forwarded into both the Dev prompt and same-stage retry prompt, Dev should stop inventing verification commands
2. if the executor prompt requires explicit `test_results.command`, observer can distinguish real test failures from partial verification or environment issues

### What was changed

#### A. Structured verification is now part of the Dev contract

In `agent/governance/auto_chain.py`:
- added `_render_dev_contract_prompt(...)`
- `_build_dev_prompt(...)` now includes:
  - `verification`
  - `requirements`
  - `acceptance_criteria`
  - `test_files`
  - `doc_impact`

#### B. Dev retry prompts no longer fall back to stale free-text only

Also in `agent/governance/auto_chain.py`:
- same-stage Dev retries now rebuild the contract from structured metadata
- retry text explicitly tells Dev to use the required verification command

#### C. Executor-side Dev instructions now require verification evidence

In `agent/executor_worker.py`:
- Dev prompt now appends `Verification plan: {...}`
- if a verification command exists, the prompt explicitly says Dev must attempt it
- Dev output format instruction now includes `test_results.command`

#### D. Targeted tests

New file:
- `agent/tests/test_dev_contract_round4.py`

Coverage:
1. `verification` is forwarded into `_build_dev_prompt`
2. Dev retry prompt rebuilds from structured metadata instead of stale original prompt text
3. executor-side Dev prompt includes the required verification command and explicit `test_results` evidence schema

### Verification run

Commands run:
- `python3.13 -m pytest agent/tests/test_dev_contract_round4.py -q`
- `python3.13 -m py_compile agent/governance/auto_chain.py agent/executor_worker.py agent/tests/test_dev_contract_round4.py`

Result:
- `3 passed`
- `py_compile` passed

### Real retry verification

Rebuilt governance service and released:
- `task-1774849310-8f6800`

Observed result:
- task succeeded
- `test_results.failed = 0`
- summary now clearly states:
  - changed scope verification passed
  - broader repository-level Python compatibility still fails outside the requested scope

Observed verification command in task result:
- `python -m py_compile ...`
- targeted `unittest` checks
- direct module import checks

### New blocker exposed by Round 4

Although Dev evidence became correct, governance logs showed the checkpoint gate still blocked the task because:
- it inferred related docs automatically
- it ignored PM's explicit `doc_impact.files = []`

Gate reason observed in governance logs:
- `Related docs not updated: ['README.md', 'docs/ai-agent-integration-guide.md', 'docs/p0-3-design.md', 'docs/telegram-project-binding-design.md']`

This confirmed an earlier hypothesis:
- doc gating must prioritize PM `doc_impact`
- inferred docs should only be a fallback when PM did not specify doc impact

### Round 4 conclusion

Round 4 repaired the Dev verification contract.

Most important verified outcome:

**The Dev retry no longer failed on a fabricated `pytest unavailable` story; it now produced scoped verification evidence with `failed = 0`.**

The next blocker was no longer verification semantics. It was the doc gate policy.

## 2026-03-30 Session 5: Doc Gate + related_nodes Normalization (Round 5)

### Prediction before changes

Two governance-layer issues were now directly confirmed by real logs:
1. `doc_impact.files = []` was being ignored by checkpoint gate
2. `related_nodes` could still contain dicts, which later caused SQLite binding errors or invalid node verification attempts

Prediction:
1. if checkpoint gate treats explicit `doc_impact.files` as source of truth, empty files should mean "no required doc updates"
2. if `related_nodes` is normalized to concrete node-id strings before gate/audit/state operations, dict-shaped metadata should stop poisoning downstream logic

### What was changed

In `agent/governance/auto_chain.py`:
- added `_normalize_related_nodes(...)`
- `_do_chain(...)` now normalizes `metadata["related_nodes"]` at entry
- all gate/prompt/state paths now use normalized node ids
- `_gate_checkpoint(...)` now prefers `metadata.doc_impact.files` when present
- automatic related-doc inference only runs when PM did not provide explicit doc-impact files

### Verification run

Commands run:
- `python3.13 -m pytest agent/tests/test_dev_contract_round4.py -q`
- `python3.13 -m py_compile agent/governance/auto_chain.py agent/executor_worker.py agent/tests/test_dev_contract_round4.py`

Result:
- `5 passed`
- `py_compile` passed

New coverage added:
1. explicit `doc_impact.files = []` no longer triggers inferred doc blockage
2. dict-shaped `related_nodes` are filtered out unless they provide a concrete node id

### Real chain verification

Rebuilt governance service, created a fresh replay Dev task, and released it:
- replay Dev task: `task-1774850335-e73fca`

Observed result:
- Dev task succeeded
- governance created next-stage test task:
  - `task-1774850566-603884`
  - type: `test`
  - status: `observer_hold`

This is the first verified run in this iteration where the repaired Dev path advanced all the way to:
- `Dev succeeded`
- `checkpoint gate passed`
- `test task created`

### New blockers exposed by Round 5

With Dev -> Test now working, the next governance-layer issues became visible:

1. governance container lacks `git`
   - checkpoint gate logs `git diff verification failed (non-blocking): [Errno 2] No such file or directory: 'git'`
   - this no longer blocks because it is non-blocking, but it weakens evidence quality

2. impact-based related-node enrichment returns node ids not found in the current graph
   - example: `L4.12`
   - verify update logs `NodeNotFoundError`
   - current behavior is non-blocking, but the enrichment source and graph are out of sync

### Round 5 conclusion

Round 5 is the first time the current repaired Dev path has been verified end-to-end through its own gate into the next workflow stage.

Most important verified outcome:

**`Dev -> checkpoint -> test(observer_hold)` now works in the real governance flow.**

The next round should shift from Dev contract/gate repair to:
1. governance container tooling parity (`git` in container)
2. impact-enrichment / acceptance-graph alignment
3. first Dev-to-Test E2E acceptance scenario

## 2026-03-30 Session 6: Executor-Owned Diff + Temporary Node Gate Bypass

### Why this round happened

After Round 5, two questions remained open:
1. should checkpoint keep trying to run `git diff` inside governance, or trust executor evidence?
2. should Dev checkpoint keep enforcing node alignment while graph updates are still lagging behind local incremental development?

The decision for this round was:
- trust executor-owned worktree diff at Dev checkpoint
- temporarily bypass Dev-stage node gate
- keep both signals visible in logs so they can be restored after graph/service sync

### What was changed

In `agent/governance/auto_chain.py`:
- removed governance-side `git diff` re-verification from `_gate_checkpoint(...)`
- checkpoint now trusts executor-produced `changed_files`
- Dev-stage node gate is now log-only and non-blocking
- kept:
  - target-file scope enforcement
  - explicit test-result failure blocking
  - PM-driven doc-impact enforcement

### Verification run

Commands run:
- `python3.13 -m pytest agent/tests/test_dev_contract_round4.py -q`
- `python3.13 -m py_compile agent/governance/auto_chain.py agent/tests/test_dev_contract_round4.py`

Result:
- `6 passed`
- `py_compile` passed

New coverage added:
- checkpoint no longer depends on governance-local `git`
- checkpoint no longer blocks on stale `related_nodes` alignment during Dev stage

### Real replay verification

Rebuilt governance and replayed the same Dev contract:
- replay task: `task-1774873226-af418a`

Observed result:
- Dev task succeeded
- next-stage test task created again:
  - `task-1774873430-6505d6`
  - type: `test`
  - status: `observer_hold`

This repeated the same acceptance outcome under the new checkpoint policy, so the bypass is not just unit-tested; it is also verified in the real chain.

### Current state after Round 6

At this point the repaired flow is:
- `Dev execute in isolated worktree`
- `Dev emits explicit verification evidence`
- `checkpoint consumes executor evidence`
- `checkpoint respects PM doc_impact`
- `checkpoint no longer blocks on temporary graph drift`
- `test task is created and held for observer review`

### Next likely focus

The most natural next step is no longer Dev checkpoint repair. It is:
1. `test` stage verification contract
2. governance container tooling parity (`git` if still desired for later stages)
3. acceptance-graph / impact-enrichment reconciliation before re-enabling strict node gating

## 2026-03-30 Session 7: Claude Path Validation Repair

### Trigger

Observer review in `docs/dev/dev-verification-2026-03-30.md` flagged three Claude-path follow-ups:
1. no turn caps for `dev/tester/qa`
2. no explicit workspace binding in the Claude command
3. one ordering-dependent full-suite failure in `test_dev_contract_round4.py`

### What was changed

In `agent/ai_lifecycle.py`:
- `_build_claude_command(...)` now accepts `cwd`
- Claude command now adds `--add-dir <workspace>`
- Claude turn caps are now:
  - coordinator: `1`
  - pm: `10`
  - dev: `20`
  - tester/qa: `10`
- `create_session(...)` now passes the resolved workspace into the Claude command builder

In `agent/tests/test_ai_lifecycle_provider_routing.py`:
- added coverage for:
  - `--add-dir`
  - dev/tester turn caps

In `agent/tests/test_dev_contract_round4.py`:
- fixed the ordering-dependent retry test by patching `governance.task_registry.create_task` directly instead of swapping `sys.modules`

In runtime/example pipeline config:
- `dev/tester/qa` switched to `anthropic / claude-sonnet-4-6` for validation

### Verification run

Commands run:
- `python3.13 -m pytest agent/tests/test_ai_lifecycle_provider_routing.py agent/tests/test_dev_contract_round4.py -q`
- `python3.13 -m pytest agent/tests/test_dev_contract_round2.py agent/tests/test_dev_worktree_round3.py agent/tests/test_dev_contract_round4.py agent/tests/test_ai_lifecycle_provider_routing.py -q`
- `python3.13 -m py_compile agent/ai_lifecycle.py agent/tests/test_ai_lifecycle_provider_routing.py agent/tests/test_dev_contract_round4.py agent/pipeline_config.py`

Result:
- focused suite: passed
- combined dev-related suite: `14 passed`
- `py_compile` passed

### Claude smoke verification

Ran a direct `AILifecycleManager.create_session(...)` smoke for role `dev` with provider resolved to `anthropic`.

Observed result:
- session status: `completed`
- exit code: `0`
- Claude returned the requested JSON payload

Observed command in logged input:
- `claude -p`
- `--model claude-sonnet-4-6`
- `--add-dir C:\\Users\\z5866\\Documents\\amingclaw\\aming_claw`
- `--allowedTools Read,Grep,Glob,Write,Edit,Bash`
- `--max-turns 20`

### Conclusion

Claude-path validation is now in a better state than when the observer review was written:
- turn caps are explicit
- workspace binding is explicit at CLI level
- the only reported full-suite failure has been fixed

This clears the immediate Claude-path blockers for moving on to `test` stage iteration.

## 2026-03-30 Session 8: Full Chain Closure

After Dev/Test contract repair, the remaining work moved from implementation-stage correctness to chain closure:
- inserted isolated `gatekeeper` review before merge
- repaired merge to use clean integration worktrees instead of dirty `main`
- promoted deploy to a first-class host executor stage
- verified a successful deploy report with `smoke_test.all_pass = true`

Primary detailed record for this closure now lives in:
- `docs/dev/test-iteration-2026-03-30.md`

## 2026-03-30 Session 9: Final Gates and Full-Chain E2E

Once the full chain succeeded, the final guardrails were restored:
- version gate re-enabled
- dirty workspace now blocks auto-chain continuation
- full-chain orchestration E2E added for:
  - `pm -> dev -> test -> qa -> gatekeeper -> merge -> deploy`

New verification files added in this phase:
- `agent/tests/test_merge_round2.py`
- `agent/tests/test_deploy_round3.py`
- `agent/tests/test_version_gate_round4.py`
- `agent/tests/test_e2e_full_chain.py`
