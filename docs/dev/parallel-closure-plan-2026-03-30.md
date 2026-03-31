# Parallel Closure Plan (2026-03-30)

## Goal

Split the current large dirty-workspace reconciliation package into a small number of workflow-safe lanes that can be monitored by Observer and released in a controlled order.

The immediate goal is not "maximum parallelism". The goal is:

1. preserve write isolation
2. avoid lane overlap on the same core files
3. keep `version gate` enabled
4. let the workflow consume the remaining system-work items in governed form

## Current Parallelism Assessment

The current workflow supports limited parallel execution, not fully automatic fan-out/fan-in orchestration.

What already works:

- multiple tasks can exist in the queue at the same time
- multiple workers can claim different tasks
- `dev` worktree isolation reduces direct write collisions
- conflict rules can reject some duplicate or opposite-intent task creation
- `observer_mode=true` allows manual release control per task

What is still missing:

- no first-class parent task that automatically splits into child implementation lanes
- no automatic join barrier that waits for multiple child lanes before unified QA / Gatekeeper / merge
- no strong graph-trace coverage yet for large multi-lane convergence

Because of that, the safest operating model is:

- manually define disjoint write scopes
- release only the lanes that are safe to run side-by-side
- hold the convergence lane until the parallel lanes complete

## Decision

Use a `2 parallel lanes + 1 convergence lane` structure.

### Lane A: Runtime / Gate / Recovery Core

This lane owns the files that affect live governance runtime behavior, node recovery, gate behavior, and deployment closure.

Primary files:

- `agent/deploy_chain.py`
- `agent/executor_worker.py`
- `agent/governance/auto_chain.py`
- `agent/governance/db.py`
- `agent/governance/enums.py`
- `agent/governance/project_service.py`
- `agent/governance/server.py`
- `agent/role_permissions.py`
- `agent/governance/failure_classifier.py`

Why this lane is isolated:

- these files are tightly coupled
- most of the remaining unstaged changes are concentrated here
- splitting them further would create more merge risk than it removes

### Lane B: Provider / Session / Orchestration Contracts

This lane owns provider routing, role prompt/session assembly, orchestration contracts, and supporting execution adapters.

Primary files:

- `.mcp.json`
- `agent/ai_lifecycle.py`
- `agent/backends.py`
- `agent/context_assembler.py`
- `agent/decision_validator.py`
- `agent/execution_sandbox.py`
- `agent/executor_api.py`
- `agent/governance/task_registry.py`
- `agent/mcp/tools.py`
- `agent/pipeline_config.py`
- `agent/pipeline_config.yaml.example`
- `agent/service_manager.py`
- `agent/task_orchestrator.py`
- `agent/telegram_gateway/gateway.py`
- `agent/telegram_gateway/message_worker.py`

Why this lane is parallel-safe:

- it does not need to mutate the same runtime gate/recovery files as Lane A
- it mostly affects provider selection, prompt/session contracts, and orchestration glue

### Lane C: Convergence (Docs / Tests / Graph Alignment)

This lane should not run until Lanes A and B are complete enough to stabilize behavior.

Primary files:

- `agent/tests/*`
- `docs/coordinator-rules.md`
- `docs/pm-rules.md`
- `docs/observer-rules.md`
- `docs/dev/*`
- `docs/aming-claw-acceptance-graph.md`
- other workflow design / PRD / architecture docs touched by the current package

Why this lane is not parallel with A/B:

- docs and tests are the place where both code lanes converge
- if A and B both continue moving while docs/tests are being finalized, convergence will churn
- this lane is the right place to finish graph alignment and prepare stricter gate re-enable

## Execution Policy

### What may run in parallel

- Lane A
- Lane B

### What must wait

- Lane C waits for Lane A and Lane B

### What remains blocked until later

- strict Dev-stage node gate re-enable
- graph-trace-driven QA / Gatekeeper
- full-chain smoke under a clean workspace and converged docs/tests

## Governance Rules For This Split

1. Use governance API only for task state changes.
2. Keep `version gate` enabled.
3. Do not absorb `governance.db`, `agent/governance/governance.db`, or `.worktrees/` into governed implementation tasks.
4. Treat the previous monolithic dirty-workspace reconciliation task as superseded once the split tasks are created.
5. Release Lane C only after Observer confirms Lane A and Lane B are both in a good state for convergence.

## Implementation Steps

1. Create two parallel-safe workflow-improvement root tasks:
   - Lane A
   - Lane B
2. Create one held convergence root task:
   - Lane C
3. Cancel the previous monolithic closure task so only the split plan remains active.
4. Keep all three in `observer_hold` until release decisions are made.

## Expected Outcome

After this split:

- the remaining large package is no longer one opaque dirty block
- Observer can release controlled lanes instead of one all-or-nothing task
- parallel work is possible without pretending the workflow already supports automatic multi-lane joins
- the later convergence task has a stable place to finish docs/tests/graph alignment before final smoke

## Execution Update

This split was applied in the current round.

Created tasks:

- Lane A: `task-1774893365-8567af`
- Lane B: `task-1774893364-78de31`
- Lane C: `task-1774893365-576a16`

All three tasks are currently in `observer_hold`.

Superseded task:

- previous monolithic closure task `task-1774892852-e459ea` was cancelled

Current release policy:

- Lane A and Lane B are the only lanes eligible for parallel release
- Lane C remains held until A and B have produced stable outputs
