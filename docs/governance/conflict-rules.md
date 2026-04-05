# Conflict Rules Engine

> **Canonical governance topic document** — The 5-rule conflict detection and resolution engine.
> Last updated: 2026-04-05 | Phase 2 Documentation Consolidation

## Overview

The conflict rules engine is a zero-token, rule-based system that runs before AI involvement to detect and resolve task conflicts. It evaluates incoming tasks against the current queue and memory to produce a decision that guides the coordinator and auto-chain.

## Design Principle

**Rules before AI** — Conflict detection uses deterministic rules (0 AI tokens) to classify tasks before any model is invoked. This ensures fast, consistent decisions without model costs or hallucination risk.

## The 5 Rules

### Rule 1: Duplicate Detection

**Trigger:** Same `source_message_hash` exists within a 5-minute window.

**Decision:** `duplicate` — reject the task.

**Purpose:** Prevents double-submission from Telegram message retries or rapid user clicks.

**Implementation:**
```python
# Check for existing task with same source_message_hash
# within DUPLICATE_WINDOW (300 seconds)
if existing_task and (now - existing_task.created_at) < DUPLICATE_WINDOW:
    return Decision(action="duplicate", reason="Same message within 5 min")
```

### Rule 2: Same-File Conflict

**Trigger:** Incoming task's `target_files` overlaps with an active (queued/claimed) task's `target_files`.

**Decision:** `conflict` — queue the task or reject based on operation types.

**Sub-rules:**
- Same file, same operation → `duplicate` (likely re-request)
- Same file, opposite operation (e.g., add vs delete) → `conflict` with hold
- Same file, different operations → `queue` (wait for first to complete)

### Rule 3: Dependency Check

**Trigger:** Task depends on upstream task that hasn't completed.

**Decision:** `queue` — hold until dependency resolves.

**Purpose:** Ensures tasks execute in correct order when there are inter-task dependencies.

### Rule 4: Failure Pattern Matching

**Trigger:** Known `failure_pattern` memory matches the incoming task context.

**Decision:** `retry` — proceed but enrich the task prompt with failure context.

**Purpose:** Prevents repeating known mistakes by injecting historical failure information into the task prompt.

**Implementation:**
```python
# Search memories for failure_pattern matching task keywords
patterns = memory_search(kind="failure_pattern", query=task.prompt)
if patterns:
    return Decision(
        action="retry",
        reason="Known failure pattern detected",
        enrichment=patterns
    )
```

### Rule 5: New Task (Default)

**Trigger:** No conflicts detected by rules 1-4.

**Decision:** `new` — proceed normally.

## Decision Set

| Decision | Meaning | Action |
|----------|---------|--------|
| `new` | No conflicts | Proceed normally |
| `duplicate` | Same task exists | Reject |
| `conflict` | File-level conflict | Queue or reject |
| `queue` | Dependency pending | Hold until resolved |
| `retry` | Known failure pattern | Proceed with enriched context |
| `merge` | Can combine with existing task | Merge prompts |
| `block` | Critical conflict | Block until manual resolution |

## Integration Points

### Coordinator

The conflict rules engine runs during coordinator task evaluation:
1. User message arrives → coordinator task created
2. Conflict rules evaluate the message context
3. Decision injected into coordinator prompt as `rule_decision`
4. Coordinator uses decision to inform its `reply_only` vs `create_pm_task` choice

### Auto-Chain

The auto-chain uses conflict rules for:
- Dedup guards on retry task creation (D4 fix)
- Checking for conflicting active chains
- Preventing parallel chains on the same target files

### Memory

The conflict rules engine reads from memory:
- `failure_pattern` memories for Rule 4
- `task_result` memories for duplicate detection
- `decision` memories for conflict history

## API

```bash
# Conflict rules are evaluated internally, not directly via API.
# The decision is included in task metadata and coordinator prompts.

# Query conflict decision for a task
GET /api/task/{pid}/list
# Response includes rule_decision in task metadata
```

## Implementation

Key file: `agent/governance/conflict_rules.py`

Main class: `ConflictRuleEngine`

Methods:
- `evaluate(task_metadata)` → `Decision`
- `_check_duplicate(source_hash, window)` → bool
- `_check_file_conflict(target_files, active_tasks)` → Decision
- `_check_dependency(task_id, deps)` → bool
- `_check_failure_patterns(prompt, memories)` → list

## Task Metadata Enrichment

When conflict rules detect relevant context, they enrich the task metadata:

```json
{
  "rule_decision": "retry",
  "rule_reason": "Known failure: gate blocks when .claude/ is dirty",
  "enrichment": [
    {
      "kind": "failure_pattern",
      "content": "Auto-chain gate blocked by .claude/settings.local.json"
    }
  ]
}
```

This enrichment is injected into the coordinator/PM prompt so AI can avoid repeating known mistakes.
