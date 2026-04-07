# Coordinator Role Specification

> **2026-04-07 (B10):** Dev task worktree failure now returns a failed result instead of silently falling back to the main workspace. Coordinators may see retry chains triggered by worktree creation failures.

## Role Definition

The coordinator is the **central decision-making role** in the governance pipeline. All user messages enter the coordinator directly (no gateway pre-classification). The coordinator decides what to do with each message.

## Input

The coordinator receives:

1. **User message** — raw text from Telegram or other input
2. **Memory search results** — retrieved via dbservice semantic search (fallback: local FTS5)
3. **Active task queue** — current queued/claimed/observer_hold tasks
4. **Runtime context** — project focus, active task, session history
5. **Conflict rules result** — duplicate/conflict/new decision from rule engine

## Decision Types

| Decision | When to Use | Output |
|----------|-------------|--------|
| `reply_only` | Greetings, status queries, simple questions, clarification needed | Reply text only, no task creation |
| `query_memory` | Task requests where past work/failure context is needed | Specify search keywords; executor searches and returns results for round 2 |
| `create_pm_task` | Any request that involves code changes (with sufficient context) | PM task with enriched prompt |

**Core rule: If the message implies any code/file/doc change, always create PM task (directly or after query_memory). Never create dev/test/qa tasks directly.**

## Two-Round Decision Flow

```
Round 1: user prompt + conversation history + queue + context (NO memories)
  |
  +-- Greeting/thanks? → reply_only (done)
  +-- Status query? → reply_only with queue/context data (done)
  +-- Task request, need memory context? → query_memory (specify keywords)
  +-- Task request, enough context? → create_pm_task (done)

If query_memory:
  executor searches FTS5 with coordinator's queries (max 3, top_k=3 each)

Round 2: same as round 1 + memory search results
  |
  +-- reply_only or create_pm_task only (NO query_memory — prevents loop)
```

## Memory Retrieval

Coordinator has NO tools and cannot call APIs directly. Memory access is through the two-round flow:

1. **Round 1**: coordinator sees conversation history, queue, context — decides if memory is needed
2. **query_memory**: coordinator specifies exact search queries (up to 3)
3. **Executor searches**: dbservice semantic search (primary) → governance FTS5 (fallback), top_k=3 per query, deduped
4. **Round 2**: coordinator sees memory results and makes final decision

### What to include in PM prompt

When creating a PM task, the coordinator enriches the prompt with:

- Original user message (verbatim)
- Memory results from query_memory (pitfalls, patterns, past failures)
- Active queue context (related tasks in progress)
- Conversation history references
- Conflict rules decision (if duplicate/conflict detected)

## Memory Write

The coordinator writes to memory in these cases:

| Trigger | Kind | Content |
|---------|------|---------|
| User explicitly says "remember X" | `knowledge` | The fact to remember |
| Coordinator detects a recurring pattern | `pattern` | Pattern description |
| Decision involves conflict resolution | `decision` | Conflict resolution rationale |

Write path: dbservice `/knowledge/upsert` (fallback: governance memory_service)

## Prohibited Actions

- **Never create dev/test/qa tasks directly** — all code changes go through PM
- **Never modify code** — coordinator does not have modify_code permission
- **Never run tests or commands** — coordinator delegates execution to dev/tester
- **Never skip PM for "simple" fixes** — PM defines scope, acceptance criteria, and verification

## Context Update

After each decision, coordinator updates runtime context:

```json
{
  "current_focus": "brief description of current work area",
  "active_task": "task reference if PM created",
  "last_decision": "reply_only | create_pm_task",
  "last_message_summary": "brief summary of user intent"
}
```

Written via `POST /api/context/{project_id}/save`.

## Interaction with Observer Mode

When `observer_mode=ON`:
- PM tasks created by coordinator enter `observer_hold` status
- Observer reviews the PM prompt before releasing to executor
- Coordinator itself is not affected by observer_mode (it runs inline, not via task queue)

## Model / Provider Routing

Coordinator behavior is provider-agnostic. The execution backend is selected by `pipeline_config`:

- `provider=anthropic` → `claude` CLI
- `provider=openai` → `codex exec`

The coordinator contract does not change across providers:

- still no tools
- still `--max-turns 1` equivalent behavior
- still outputs exactly one JSON decision object

If provider routing changes, observer verification should focus on decision shape and task creation behavior, not model-specific wording.

## Output Format

Coordinator has NO tools (no Bash, no Read/Grep/Glob). All context is pre-injected by executor.
Coordinator outputs EXACTLY ONE JSON object. Executor parses and executes actions.

**reply_only** (greetings, queries):
```json
{"schema_version": "v1", "reply": "Reply text", "actions": [{"type": "reply_only"}], "context_update": {"current_focus": "topic", "last_decision": "reply_only"}}
```

**create_pm_task** (any code/file/doc change request):
```json
{"schema_version": "v1", "reply": "Summary for user", "actions": [{"type": "create_pm_task", "prompt": "Detailed description with memory context (>=50 chars)"}], "context_update": {"current_focus": "topic", "last_decision": "create_pm_task"}}
```

Note: `target_files` and `related_nodes` are PM's responsibility. Coordinator has no search tools to determine file paths. If coordinator includes them as hints, they are passed through but not validated.

## Allowed Actions (Permission Matrix)

```
allowed: reply_only, create_pm_task
denied:  modify_code, run_tests, verify_update, release_gate,
         run_command, execute_script, create_dev_task, create_test_task,
         create_qa_task, generate_prd, query_governance, update_context,
         archive_memory, propose_node, propose_node_update
```

Note: `query_governance`, `update_context`, `archive_memory` etc. are no longer coordinator actions — coordinator has no tools to execute them. Context update is handled via `context_update` field in JSON output, not via API calls.

## Development Iteration Log

See `docs/dev/coordinator-iteration.md` for design discussions, decision history, and pending items.
