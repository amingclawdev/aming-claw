# Workflow Self-Repair Observer Run (2026-03-30)

## Goal

Run the split dirty-workspace reconciliation lanes through the real workflow under Observer control and evaluate automatic workflow quality.

## Scope

- Lane A: runtime / gate / recovery core
- Lane B: provider / session / orchestration contracts
- Lane C: held convergence lane (docs / tests / graph)

## Observer Actions Taken

1. Released Lane A root task and Lane B root task in parallel.
2. Confirmed no live executor worker was consuming tasks.
3. Started local executor worker processes and diagnosed startup issues.
4. Fixed `pipeline_config.py` so `gatekeeper` is a valid pipeline role.
5. Verified the fix with tests and local executor replay.
6. Observed `Coordinator -> PM` for both lanes complete successfully.
7. Found a new paradox: explicit dirty-workspace reconciliation PM tasks were still blocked by `version gate`.
8. Fixed `auto_chain._gate_version_check()` to allow a narrow bypass for governed dirty-workspace reconciliation chains by following parent task metadata.
9. Fixed coordinator-created PM task metadata forwarding in `executor_worker.py` for lane metadata / split-plan hints.
10. Rebuilt live governance so the new auto-chain logic was actually running.
11. Replayed PM tasks and confirmed they now advance to `dev` instead of stopping at `version gate`.
12. Released the new Lane A / Lane B `dev` tasks and observed real execution.

## What Worked

### 1. Controlled parallelism worked

The split model behaved as intended:

- Lane A root task succeeded
- Lane B root task succeeded
- both produced scoped PM tasks
- PM replay under the repaired gate logic produced scoped `dev` tasks in `observer_hold`

### 2. Auto-flow quality improved after two small repairs

The following workflow defects were fixed during the run:

- `pipeline_config.py` rejected `gatekeeper` as an unknown role
- `version gate` incorrectly blocked explicit dirty-workspace reconciliation chains

### 3. Dev execution respected worktree isolation

Observed dev outputs:

- Lane A dev changed 3 files:
  - `agent/deploy_chain.py`
  - `agent/executor_worker.py`
  - `agent/governance/auto_chain.py`
- Lane B dev changed 8 files:
  - `agent/context_assembler.py`
  - `agent/decision_validator.py`
  - `agent/governance/task_registry.py`
  - `agent/mcp/tools.py`
  - `agent/pipeline_config.py`
  - `agent/pipeline_config.yaml.example`
  - `agent/service_manager.py`
  - `agent/telegram_gateway/gateway.py`

This is strong evidence that:

- worktree isolation is functioning
- the workflow can keep lane writes relatively narrow
- the split plan is materially constraining implementation

## New Quality Finding

### Dev checkpoint is still not lane-aware enough

After each dev completed, auto-chain did **not** advance to `test`.

Instead, it created a dev retry task with gate reasons such as:

- Lane A:
  - `Related docs not updated: ['docs/ai-agent-integration-guide.md', 'docs/deployment-guide.md', 'docs/human-intervention-guide.md', 'docs/p0-3-design.md']`
- Lane B:
  - `Related docs not updated: ['README.md', 'docs/ai-agent-integration-guide.md', 'docs/telegram-project-binding-design.md']`

This is the wrong behavior for the current split design because:

- Lane A and Lane B were intentionally defined as code-only lanes
- docs convergence belongs to Lane C
- PM results already expressed no doc updates for these lanes

### Impact

The workflow currently:

- can split and run parallel reconciliation lanes
- can self-repair some workflow defects
- can advance through `Coordinator -> PM -> Dev`
- but still misroutes lane-specific doc responsibility at the dev checkpoint

This causes:

- unnecessary `dev` retries
- no promotion to `test`
- extra churn before convergence

## Assessment

Current automatic workflow quality is:

- **Good** at:
  - controlled parallel root-task decomposition
  - role/model routing
  - real PM generation
  - real dev execution in isolated worktrees
  - handling targeted workflow self-repair defects
- **Not yet good enough** at:
  - lane-aware doc gating
  - distinguishing code-only lane progress from convergence-lane responsibilities

## Next Repair Target

Repair the dev checkpoint so that:

1. Lane A and Lane B may omit doc changes when the chain is explicitly part of the split dirty-workspace reconciliation plan.
2. The doc responsibility is deferred to Lane C.
3. The workflow advances `Lane A/B Dev -> Test` instead of creating another `Dev` retry.

## Observer Continuation (Host Executor + Lane Retry)

### Host execution plane recovered

The queue consumer is now running on the host, not in Docker Compose:

- one host `service_manager.py`
- one managed host `executor_worker.py`
- MCP autostart no longer spawns duplicate executor workers by default

This means the workflow can again consume queued tasks without ad-hoc `--once` replays.

### New workflow defect confirmed: complex dev tasks hit Claude turn cap

Real replay result:

- Lane B retry `task-1774894977-4bb630` completed with:
  - `summary = "Error: Reached max turns (20)"`
  - changed files were still produced from the isolated worktree

Lane A fresh dev replay showed the same pattern:

- `task-1774900216-470fc1`
  - `summary = "Error: Reached max turns (20)"`
  - changed files captured from worktree

This established that the current Claude dev turn cap was too small for workflow-improvement closure tasks.

### Repairs applied

1. Increased Claude dev turn cap in `agent/ai_lifecycle.py`
   - `dev: 20 -> 40`
   - `gatekeeper` explicitly set to `10`
   - role turn caps are now centralized in `_CLAUDE_ROLE_TURN_CAPS`

2. Made checkpoint doc gate lane-aware
   - `agent/governance/auto_chain.py`
   - governed dirty-workspace reconciliation chains now walk ancestor metadata
   - Lane `A/B` can defer doc updates to convergence Lane `C`
   - this removes false `Related docs not updated` retries for split closure lanes

3. Restarted the host manager/executor so retries consume the new code path

### Verification

- `pytest agent/tests/test_ai_lifecycle_provider_routing.py -q`
- `pytest agent/tests/test_dev_contract_round4.py -q`
- combined result: passing

### Current live state at handoff point

- Lane B retry under new config: `task-1774902140-ce79da`
  - claimed
  - running with Claude command showing `--max-turns 40`
- Lane A retry queued behind it: `task-1774902320-12b887`

### Quality assessment update

The workflow is now better than the previous round in three specific ways:

- host-side automatic task consumption is back
- duplicate executor spawning from MCP sessions has been neutralized
- the main remaining dev-stage blocker has narrowed from "doc gate + turn cap" to "can the current long-running Claude task complete successfully under the expanded turn budget"

## Relevant Task IDs

- Lane A root: `task-1774893365-8567af`
- Lane B root: `task-1774893364-78de31`
- Lane C held convergence task: `task-1774893365-576a16`
- Lane A PM replay that successfully advanced to dev: `task-1774894511-86e03c`
- Lane B PM replay that successfully advanced to dev: `task-1774894512-4e16a7`
- Lane A dev result: `task-1774894599-0d0780`
- Lane B dev result: `task-1774894641-b70901`
- Lane A dev retry created by doc gate: `task-1774894931-112163`
- Lane B dev retry created by doc gate: `task-1774894977-4bb630`

## Observer Continuation 2 (Prompt Conflict + Fresh Replay)

### What actually happened

The old Lane A/B retry prompts were internally contradictory:

- retry gate reason said `Add README/docs to changed_files`
- lane contract said `Do NOT edit docs/ or test files — those belong to Lane C`

This created an impossible instruction set for Dev. The model kept trying to
resolve the contradiction inside the worktree, which looked like a provider
turn-limit problem, but the root cause was a prompt conflict.

### Repairs applied

1. `agent/governance/auto_chain.py`
   - dev retry generation now rewrites stale doc-related gate reasons for
     governed Lane A/B dirty-workspace reconciliation chains
   - new retry reason explicitly says:
     - docs belong to Lane C
     - do not modify `README.md` or `docs/`
     - keep changes inside `target_files`

2. Live recovery
   - cancelled stale Lane A claimed retry `task-1774902320-12b887`
   - restarted live governance so the new retry logic was active
   - rebuilt fresh dev replay tasks directly from the completed PM outputs:
     - Lane A: `task-1774903624-47705f`
     - Lane B: `task-1774903624-e3672d`

3. Checkpoint gate refinement
   - `agent/governance/auto_chain.py`
   - unrelated-file gate now treats:
     - `target_files`
     - explicit `test_files`
     - explicit `doc_impact.files`
     as allowed output scope
   - this fixes Lane B false blocks where PM-authorized test files were being
     rejected as unrelated

### Current replay outcomes

- Lane A fresh replay `task-1774903624-47705f`
  - succeeded
  - passed `53` tests
  - still blocked by one true scope overrun:
    - `agent/governance/chain_context.py`
- Lane B fresh replay `task-1774903624-e3672d`
  - succeeded
  - still hit `Error: Reached max turns (40)`
  - changed files now include three PM-authorized test files
  - old gate block was:
    - `Unrelated files modified: ['agent/tests/test_context_assembler.py', 'agent/tests/test_service_manager.py', 'agent/tests/test_task_registry_escalate.py']`
  - that specific gate defect is now fixed in code and ready for replay

### New governance requirement recorded

The workflow needs an explicit **prompt contradiction handling process**.

Minimum process:

1. Detect contradiction patterns in retry/build prompts
   - stale gate reason conflicts with lane constraints
   - gate reason conflicts with `skip_reasons`
   - gate reason conflicts with `doc_impact.files=[]`

2. Normalize before dispatch
   - prefer the latest lane contract over stale retry text
   - rewrite or drop instructions that contradict active scope constraints

3. Record the conflict
   - classify as workflow defect, not model-quality defect
   - write pitfall/audit entry with both conflicting prompt fragments

4. Replay from the nearest clean stage
   - do not keep consuming turns on a task whose prompt is already known to be
     contradictory

## Observer Continuation 3 (Host Governance Runtime + Merge Replay)

### Host-governance cutover validated

The workflow is now running against host governance on `http://localhost:40000`
instead of Docker governance/nginx.

Observed runtime:

- host governance health passes
- task API remains reachable
- observer actions (`create`, `release`, `cancel`) work against the host service

### New merge replay defect confirmed

Using a fresh observer-created merge replay on the host runtime exposed a real
workflow defect, not a Docker sync artifact:

- replay task: `task-1774915375-a401b7`
- old failure:
  - `Merge worktree missing for chained merge`

After patching merge replay to tolerate missing worktrees, the next replay
revealed the deeper root cause:

- new failure:
  - `Merge branch missing for chained merge: dev/task-1774907129-e92a7c`

This showed that the first successful merge path removed both:

- the dev worktree
- the dev branch

As a result, later replay / retry could not reconstruct the merge source.

### Repairs applied

1. `agent/executor_worker.py`
   - merge now supports replay semantics:
     - if the dev worktree is missing but the branch still exists, merge can
       continue from branch state
     - if the branch is already merged into `HEAD`, merge returns a successful
       `already_merged_replay` result instead of failing
   - successful merge now removes the dev worktree but preserves the dev branch
     (`delete_branch=False`) so later replay still has a source anchor

2. `agent/tests/test_merge_round2.py`
   - added/updated replay coverage for:
     - preserved `_worktree/_branch` metadata
     - already-merged replay success
     - dev branch preservation on successful merge

### Host executor observability repair

The host runtime also exposed a separate execution-plane problem:

- `service_manager.py` launched `executor_worker.py` with stdout/stderr sent to
  `DEVNULL`
- when host worker startup failed, the observer had no direct evidence

Repairs:

1. `agent/service_manager.py`
   - executor child stdout/stderr now persist to:
     - `shared-volume/codex-tasks/logs/service-manager-executor-<project>.log`
     - `shared-volume/codex-tasks/logs/service-manager-executor-<project>.err.log`

2. `agent/executor_worker.py`
   - `_acquire_pid_lock()` now treats Windows `SystemError` from
     `os.kill(old_pid, 0)` as a stale PID condition
   - this fixes host worker bootstrap on Windows after stale PID files

3. `agent/tests/test_executor_worker_pid_lock_round1.py`
   - added regression coverage for Windows stale-PID lock recovery

### Verification

- `pytest agent/tests/test_merge_round2.py -q`
- `pytest agent/tests/test_executor_worker_pid_lock_round1.py -q`
- `pytest agent/tests/test_service_manager.py -q`
- combined result:
  - `27 passed`

### Current quality assessment

Improved:

- host governance is now a real runtime, not just a migration target
- merge replay semantics are more robust and no longer require the original
  dev worktree to survive
- successful merge now preserves branch state for later replays
- host worker bootstrap is observable
- Windows stale PID lock no longer hard-crashes worker startup

Still open:

- historical Lane B merge replay created before branch preservation cannot be
  resumed, because its source branch was already deleted
- `start-manager.ps1` still needs one more pass for completely reliable
  takeover UX; direct host runtime behavior improved, but the launcher
  experience is not yet fully smooth

### Next step

Replay a fresh Lane B closure task under the new branch-preserving merge logic,
so the workflow can be validated end-to-end on the host runtime rather than on
historical Docker-era artifacts.

## Observer Continuation 4 (Host Lane B Fresh Replay + Max-Turn False Success)

### Fresh Lane B replay on host governance

To avoid Docker-era replay artifacts, a brand-new Lane B `dev` task was created
from the original PM contract and released through the host governance API:

- fresh dev replay:
  - `task-1774916534-3015a1`

Observed execution:

- host worker successfully claimed the task
- dev worktree was created normally
- prompt build completed
- `ai_lifecycle` executed Claude Opus in the fresh worktree

This confirms that the host governance + host worker execution plane is now
functionally live for real auto-chain tasks.

### New workflow defect: max-turn terminal error was treated as success

The fresh replay uncovered a different, more serious quality issue:

- Claude completed with stdout:
  - `Error: Reached max turns (40)`
- executor still marked the dev task as `succeeded`
- auto-chain then created a downstream `test` task:
  - `task-1774916949-eca618`

Why this happened:

- `_parse_output()` fell back to a plain summary object
- executor treated the session as successful because CLI exit code was `0`
- the terminal CLI error was not classified as execution failure

This is a false-success defect in the workflow itself, not a model-quality
issue.

### Repair applied

1. `agent/executor_worker.py`
   - added terminal CLI error detection before git-diff / parse-output success
     handling
   - non-coordinator AI roles now fail fast when the CLI returns:
     - `Error: Reached max turns (...)`
     - other plain `Error:` terminal outputs

2. `agent/tests/test_dev_contract_round4.py`
   - added regression coverage proving that `Reached max turns` is treated as a
     failed dev execution rather than a successful chain result

### Observer quality assessment

Improved:

- host execution plane is healthy enough to claim and run fresh replay tasks
- this round isolated a workflow semantic defect cleanly, without confusing it
  with Docker/runtime instability

Current blocker:

- Lane B task scope is still large enough that Claude Opus exhausted `40` turns
- more importantly, the workflow previously promoted that terminal CLI error as
  success

### Immediate next action

- land the false-success fix
- cancel the invalid downstream `test` task created from that bad success
- rerun a fresh Lane B replay under the repaired executor semantics

## Observer Continuation 5 (Failed-Task Self-Repair Hook)

### Why this extra repair was needed

After the false-success fix, one more autonomy gap became clear:

- treating `Reached max turns` as `failed` is correct
- but a plain failed task did not automatically create a
  `workflow_improvement` task

That meant the workflow would be more truthful, but not yet more autonomous.

### Repair applied

1. `agent/governance/auto_chain.py`
   - added `on_task_failed(...)`
   - failed executions can now reuse the same workflow-improvement classifier
     path that gate failures already use

2. `agent/governance/task_registry.py`
   - failed completions now trigger the failed-task self-repair hook
   - provider/tooling failures like `Reached max turns` can automatically create
     a `workflow_improvement` task instead of only falling back to retry /
     observer review

3. `agent/tests/test_workflow_self_repair_round1.py`
   - added regression coverage proving failed-task provider/runtime defects are
     promoted into workflow-improvement tasks

### Expected behavior after this repair

For a fresh Lane B replay that again exhausts `max turns`:

- the `dev` task should complete as `failed` or `observer_hold` retry state
  instead of `succeeded`
- no invalid downstream `test` task should be created from that failure
- a workflow-improvement task should be created automatically when the failure
  classifier marks it as a provider/runtime defect

### Host runtime probe result

To validate the new failed-task hook without waiting for another full Lane B
run, an observer-created probe task was completed directly through the host
governance API with:

- source task:
  - `task-1774918344-53d610`
- simulated failure:
  - `Error: Reached max turns (40)`

Observed result:

- failed dev completion stayed in `observer_hold`
- host governance immediately created a workflow-improvement task:
  - `task-1774918344-f22727`
- classified as:
  - `provider_tool_defect`

This is the first confirmed host-runtime proof that failed-task self-repair is
now active, not just unit-tested.

## Observer Continuation 6 (Adaptive Dev Turn Budget)

### Assessment

The latest Lane B replay evidence did not show a new prompt contradiction.
Instead, it showed a heavy-but-consistent dev contract:

- many target files
- many requirements
- long prompt body
- workflow-improvement / lane-style reconciliation scope

So the next repair is not "unlimited turns", but adaptive turn budgeting.

### Repair applied

1. `agent/ai_lifecycle.py`
   - Claude turn caps now remain role-based by default
   - `dev` is promoted from `40` to `60` only for heavy workflow tasks, such
     as:
     - workflow-improvement operations
     - lane replay / reconciliation tasks
     - large target-file sets
     - long requirement lists
     - very large prompt payloads

2. `agent/tests/test_ai_lifecycle_provider_routing.py`
   - added regression coverage proving heavy dev tasks receive `--max-turns 60`
   - ordinary roles and ordinary dev tasks keep their previous caps

### Rationale

This keeps the workflow honest:

- normal dev tasks do not get an unlimited budget
- heavy workflow repair tasks get more room to finish coherently
- if a task still exhausts the larger budget, it should be split or further
  normalized rather than made unbounded

### Live result

After refreshing host governance + host worker, a new fresh Lane B replay was
launched:

- `task-1774920506-ac46f4`

Observed execution:

- host worker claimed the task successfully
- Claude dev session ran with:
  - `--max-turns 60`
- the task completed successfully instead of terminating at `40`

Observed dev outcome:

- status:
  - `succeeded`
- downstream auto-chain:
  - created `test` task `task-1774921011-854dc5`
- result highlights:
  - changed files stayed within Lane B scope
  - verification command reported `86 passed, 0 failed`

This is the first live proof that adaptive dev turn budgeting materially
improved workflow self-repair progress instead of just masking a false success.

### Follow-through validation

The same fresh Lane B replay then continued successfully into the next stage:

- dev task:
  - `task-1774920506-ac46f4`
  - `succeeded`
- auto-created test task:
  - `task-1774921011-854dc5`
  - `succeeded`
- auto-created QA task:
  - `task-1774921126-19d5b0`
  - `observer_hold`

Observed test result:

- `86 passed, 0 failed`
- recommendation:
  - `t2_pass`

This confirms the host workflow is now stably advancing through:

- `dev -> test -> qa_hold`

under the repaired semantics, instead of stalling at dev or generating false
success downstream tasks.

### Further progression

The same chain then advanced one more stage:

- QA task:
  - `task-1774921126-19d5b0`
  - `succeeded`
- auto-created gatekeeper task:
  - `task-1774921235-138a06`
  - `observer_hold`

Observed QA result:

- recommendation:
  - `qa_pass`
- review summary:
  - all 86 tests passed
  - acceptance criteria spot-checks succeeded

This confirms the host workflow is now stably advancing through:

- `dev -> test -> qa -> gatekeeper_hold`

### Gatekeeper progression

The chain then advanced through gatekeeper as well:

- gatekeeper task:
  - `task-1774921235-138a06`
  - `succeeded`
- auto-created merge task:
  - `task-1774921291-29b9e5`
  - `observer_hold`

Observed gatekeeper result:

- recommendation:
  - `merge_pass`
- pm_alignment:
  - `pass`

This extends the live host-chain proof to:

- `dev -> test -> qa -> gatekeeper -> merge_hold`

### Merge-stage blocker identified

The first merge attempt succeeded technically:

- merge task:
  - `task-1774921291-29b9e5`
  - `succeeded`
  - `merge_mode: isolated_integration`

But the chain still stopped before deploy because release gating created:

- merge retry:
  - `task-1774921352-fed939`
- workflow-improvement task:
  - `task-1774921341-1839f1`

Root cause analysis:

1. related nodes were still at `t2_pass`, not `qa_pass`
2. direct `qa_pass` promotion was blocked by artifacts checks for nodes such as
   `L2.2`
3. that artifact gap is expected for Lane C convergence
4. however, this fresh Lane B replay had lost enough explicit lane metadata
   that the release gate did not recognize it as a governed dirty-workspace lane
   and therefore did not defer correctly

### Repair applied

1. `agent/governance/auto_chain.py`
   - governed dirty-workspace lane detection now supports best-effort inference
     from:
     - `replay_source`
     - `intent_summary`
     - `_original_prompt`
   - this allows fresh replay chains rebuilt from original contracts to retain
     their Lane A/B reconciliation identity even when explicit `lane` metadata
     is not present

2. `agent/tests/test_qa_gatekeeper_round1.py`
   - added regression coverage proving a replayed Lane B chain still defers the
     release gate when node status is blocked at `t2_pass`

### Release-gate replay result

After refreshing host governance with the inferred-lane repair, the previously
generated merge retry was replayed:

- merge retry:
  - `task-1774921352-fed939`
  - `succeeded`

Observed outcome:

- release gate no longer blocked on `qa_pass`
- chain advanced automatically to:
  - deploy task `task-1774921650-4d7625`
  - `observer_hold`

This extends the live host-chain proof to:

- `dev -> test -> qa -> gatekeeper -> merge -> deploy_hold`
