# Executor API Integration Guide

> **2026-03-26 update:** executor_api.py no longer depends on old executor.py or workspace_registry.py (deleted). `/cleanup-orphans`, `/workspaces`, `/workspaces/resolve` endpoints are now stubs returning degraded responses. `/coordinator/chat` endpoint is deprecated.

> Executor HTTP API (port 40100) runs on the host machine.
> Used for Claude Code session direct monitoring and debugging the execution chain.

## Quick Start

```bash
# Check if Executor is running
curl http://localhost:40100/health

# View overall status
curl http://localhost:40100/status

# ⚠ /coordinator/chat deprecated (old coordinator.py deleted)
# curl -X POST http://localhost:40100/coordinator/chat \
#   -H "Content-Type: application/json" \
#   -d '{"message": "current project status", "project_id": "amingClaw"}'
```

## Endpoint List

### Monitoring (GET)

| Endpoint | Description | Returns |
|----------|-------------|---------|
| `/health` | API health check | `{status, port, ai_manager, orchestrator}` |
| `/status` | Overall status | `{pending_tasks, processing_tasks, active_ai_sessions}` |
| `/sessions` | Active AI processes | `{sessions: [{session_id, role, pid, elapsed_sec}]}` |
| `/tasks?project_id=X&status=Y` | Task list | `{tasks: [...], count}` |
| `/task/{task_id}` | Single task details | Task JSON + `_stage` + `_file` |
| `/trace/{trace_id}` | Trace tracking | `{trace_id, entries: [...]}` |
| `/workspaces` | ~~Workspace registry~~ **stub, returns degraded** | `{workspaces: [], count: 0, degraded: true}` |
| `/workspaces/resolve?project_id=X` | ~~Resolve workspace by project ID~~ **stub, returns degraded** | `{degraded: true}` |

### Intervention (POST)

| Endpoint | Description | Body |
|----------|-------------|------|
| `/task/{id}/pause` | Pause running task | None |
| `/task/{id}/cancel` | Cancel task | None |
| `/task/{id}/retry` | Retry failed task | None |
| `/cleanup-orphans` | ~~Clean up zombie processes~~ **stub, returns degraded** | None |
| `/tasks/create` | Idempotent task file creation (used by Orchestrator) | JSON task object |

### POST /tasks/create — Idempotency Guarantees

Before creating a new task file, the endpoint checks all three active stages in order:

1. `pending/` — task file already waiting to be picked up
2. `processing/` — task is currently being executed
3. `results/` — task has already completed (pending acceptance)

If a match is found in **any** of these stages, the endpoint returns the existing task with `"status": "exists"` instead of creating a duplicate. Only if no match is found does it write a new task file to `pending/`.

**Request schema:**

```json
{
  "task_id": "task-abc123",          // required, must be unique per logical task
  "project_id": "aming-claw",        // required
  "role": "dev",                     // required: dev | tester | qa | merge
  "description": "Implement X",      // required
  "target_files": ["agent/foo.py"],  // optional
  "context": {}                      // optional, extra context passed to AI
}
```

**Response schema:**

```json
// New task created:
{"status": "created", "task_id": "task-abc123", "stage": "pending"}

// Task already exists:
{"status": "exists",  "task_id": "task-abc123", "stage": "processing"}
```

### Direct Conversation (POST) — **Deprecated**

| Endpoint | Description | Body |
|----------|-------------|------|
| `/coordinator/chat` | ~~Directly start Coordinator session~~ **Deprecated, old coordinator.py deleted** | `{message, project_id, chat_id?}` |

### Debugging (GET)

| Endpoint | Description | Returns |
|----------|-------------|---------|
| `/validator/last-result` | Latest validation result | `{approved, rejected, layers[], needs_retry}` |
| `/context/{project_id}` | Current context assembly result | `{project_id, context: {...}}` |
| `/ai-session/{id}/output` | AI raw output | `{stdout, stderr, exit_code, elapsed_sec}` |

## Workspace Routing (Deprecated)

> **Note (2026-03-26):** workspace_registry.py has been deleted. The following routing logic is for historical reference only. `/workspaces` and `/workspaces/resolve` now return degraded responses.

1. `target_workspace_id` — Exact ID match
2. `target_workspace` — Label match
3. **`project_id`** — Normalized project ID match (recommended)
4. `@workspace:<label>` prefix
5. Default workspace (fallback)

### project_id Normalization Rules

All variants are automatically unified to kebab-case:

| Input | Normalized Result |
|-------|-------------------|
| `amingClaw` | `aming-claw` |
| `aming_claw` | `aming-claw` |
| `toolBoxClient` | `tool-box-client` |

### Query Workspaces

```bash
# List all registered workspaces
curl http://localhost:40100/workspaces

# Query workspace for a specific project
curl "http://localhost:40100/workspaces/resolve?project_id=amingClaw"
# → {"workspace": {"id":"ws-xxx", "path":"C:/...", "project_id":"aming-claw"}, "matched_by":"project_id"}
```

### Register Workspace (Deprecated)

> workspace_registry.py was deleted on 2026-03-26, the following code is no longer available.

```python
# ⚠ Deprecated
# from workspace_registry import add_workspace
# add_workspace(Path("/path/to/repo"), label="my-project", project_id="my-project")
```

### Redis Stream Audit

Each AI session's prompt (input) and result (output) are written to Redis Stream `ai:prompt:{session_id}`, used for auditing and debugging:

```bash
# View a session's complete prompt+result
redis-cli -p 40079 XRANGE ai:prompt:ai-dev-xxx - +
```

## Usage Scenarios

### 1. Check Why a Task Hasn't Executed

```bash
# View queue
curl http://localhost:40100/status
# → pending_tasks: 3, processing_tasks: 1

# View specific tasks
curl http://localhost:40100/tasks?status=queued

# View task being processed
curl http://localhost:40100/task/task-xxx
```

### 2. Task is Stuck

```bash
# View active AI sessions
curl http://localhost:40100/sessions

# Cancel stuck task
curl -X POST http://localhost:40100/task/task-xxx/cancel

# Clean up all zombies
curl -X POST http://localhost:40100/cleanup-orphans
```

### 3. Debug Incorrect Reply (Old Coordinator removed, the following is for reference only)

```bash
# View latest validator decisions
curl http://localhost:40100/validator/last-result

# View injected context
curl http://localhost:40100/context/amingClaw

# View AI raw output
curl http://localhost:40100/ai-session/ai-coordinator-xxx/output
```

### 4. Direct Conversation with Coordinator (Deprecated, old coordinator.py deleted)

```bash
curl -X POST http://localhost:40100/coordinator/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Help me analyze L15 status",
    "project_id": "amingClaw"
  }'

# Returns:
# {
#   "reply": "L15 has 9 nodes, all qa_pass...",
#   "actions_executed": 0,
#   "actions_rejected": 0
# }
```

### 5. View Full Trace

```bash
# List recent traces
ls shared-volume/codex-tasks/traces/

# View a specific trace in detail
curl http://localhost:40100/trace/trace-1774230000-abcdef12
```

## Relationship with Other Services

```
Claude Code Session (Developer)
    │ curl localhost:40100/...
    ▼
Executor API (:40100)  ← Interface described in this document (monitoring layer)
    │
    └── executor-gateway (:8090) → Actual task execution

Telegram User
    │ Messages
    ▼
Gateway (:40010) → task files → Executor task loop
    │
    ▼
Governance (:40006)  → Rule engine
dbservice (:40002)   → Memory layer
Redis (:40079)       → Cache
```

## QA Status Types

Tasks processed by the QA role can finish with one of three status values:

| Status | Meaning |
|---|---|
| `completed` | QA passed cleanly; all checks green. |
| `failed` | QA found blocking issues; task is marked failed and may trigger a retry budget check. |
| `passed_with_fallback` | QA passed, but one or more non-critical checks were skipped or substituted with a fallback strategy. This typically happens when an optional validation tool is unavailable (e.g., coverage reporter not installed) or a secondary lint rule is suppressed. The task proceeds to Merge, but the audit log records the fallback reason. |

Orchestrator downstream logic should treat `passed_with_fallback` the same as `completed` for routing purposes, but flag it for human review if the audit log shows repeated fallbacks on the same check.

## Auto-Chain Pipeline

After a Dev task completes successfully, the Executor automatically chains into the full validation pipeline without manual intervention. Each stage is logged to the audit trail.

### Pipeline Stages

```
Dev  ──→  Checkpoint Gate  ──→  Tester  ──→  QA  ──→  Merge
 │              │                  │          │          │
 └─ audit       └─ audit           └─ audit   └─ audit   └─ audit
```

| Stage | Role | Purpose |
|---|---|---|
| **Dev** | `dev` | Implements the task (code changes, file edits). |
| **Checkpoint Gate** | internal | Validates that Dev output meets minimum quality bar before proceeding (e.g., syntax check, required files present). Aborts chain if gate fails. |
| **Tester** | `tester` | Runs automated tests; writes results to the task result file. |
| **QA** | `qa` | Reviews test results and code diff; emits `completed`, `failed`, or `passed_with_fallback`. |
| **Merge** | `merge` | Merges the dev worktree branch into `main` and cleans up the worktree. |

Each stage transition is recorded as an entry in `pipeline_audit.jsonl` with a timestamp, stage name, outcome, and any notes.

### Pipeline State Files

The pipeline stores its state in the task's working directory under `shared-volume/codex-tasks/state/`:

| File | Purpose |
|---|---|
| `pipeline_idempotency.json` | Records which pipeline stages have already been submitted for a given `task_id`. Prevents the Orchestrator from re-submitting a stage that is already pending/processing/completed. |
| `pipeline_retry_budget.json` | Tracks how many retry attempts remain for each stage. Each stage starts with a configured budget (default 2). When a stage fails and is retried, the budget decrements. At 0, the pipeline halts and marks the task `failed`. |
| `pipeline_audit.jsonl` | Append-only log of every stage transition. Each line is a JSON object: `{ts, task_id, stage, outcome, notes}`. Used for post-mortem analysis and human review of `passed_with_fallback` cases. |

```bash
# Example: inspect pipeline audit for a task
cat shared-volume/codex-tasks/state/pipeline_audit.jsonl | grep "task-abc123"

# Example: check remaining retry budget
cat shared-volume/codex-tasks/state/pipeline_retry_budget.json
```

## Spin Loop Prevention: `_skipped_tasks`

The Executor's main processing loop maintains an in-memory set called `_skipped_tasks`. When a task is evaluated but cannot be processed in the current loop iteration (e.g., its workspace is busy, its dependencies are unmet, or it has been retried too many times within a short window), its `task_id` is added to `_skipped_tasks`.

On each subsequent loop pass, tasks present in `_skipped_tasks` are **not re-evaluated** until the set is cleared (which happens at the start of each full scan cycle). This prevents a single problematic task from consuming 100% of loop iterations and starving other tasks.

```
loop iteration N:
  for task in pending_tasks:
    if task.id in _skipped_tasks → skip
    else → try to process
      if cannot process now → _skipped_tasks.add(task.id)

end of full scan cycle → _skipped_tasks.clear()
```

## Notes

- Executor API is only accessible on the host machine (localhost:40100)
- Does not go through nginx, no governance token needed
- `/coordinator/chat` is deprecated (old coordinator.py deleted)
- `/task/{id}/cancel` will terminate AI process, use with caution
- `/cleanup-orphans` is now a stub (old executor.py's `_EXECUTOR_SPAWNED_PIDS` mechanism removed)

## Changelog
- 2026-03-26: Old Telegram bot system completely removed (bot_commands, coordinator, executor and 20 other modules), unified to use governance API
