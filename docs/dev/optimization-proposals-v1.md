# Optimization Proposals v1

Status: **awaiting review**
Date: 2026-03-29

Based on issues discovered during coordinator Step 1 predict-verify cycles.

---

## P1: Coordinator Latency Reduction

**Current**: Coordinator takes ~5-6 min per decision (Claude CLI startup + model inference).
**Impact**: Each user message has 5+ min latency before PM task is created.

### Proposal P1.1: Use Anthropic SDK instead of Claude CLI for coordinator

Claude CLI has heavy startup overhead (loads extensions, tools, etc). Coordinator has no tools (`--max-turns 1`). Switch to direct SDK call:

```python
# Instead of spawning claude CLI subprocess:
response = anthropic.messages.create(
    model=model,  # from pipeline_config
    max_tokens=4096,
    system=system_prompt,
    messages=[{"role": "user", "content": enhanced_prompt}],
)
```

**Estimated improvement**: 5-6 min → 30-60 seconds
**Risk**: Low — coordinator has no tool access anyway
**Blocked on**: Batch 4 provider abstraction (done)

### Proposal P1.2: Cache coordinator context assembly

Memory search + queue fetch + context load takes ~10 seconds (3 API calls). Cache results for 60 seconds per project — if another coordinator task comes within 60s, reuse cached context.

**Estimated improvement**: 10s per repeated request
**Risk**: Stale cache (60s is acceptable for decision context)

---

## P2: Memory Quality

### Proposal P2.1: Dedup existing pitfall memories

Current DB has 5+ identical pitfall entries from auto-chain gate retries. Run a one-time cleanup:

```sql
DELETE FROM memories WHERE memory_id NOT IN (
  SELECT MIN(memory_id) FROM memories
  WHERE status='active' GROUP BY project_id, module_id, kind, content
);
```

**Risk**: Low — only removes exact duplicates
**Blocked on**: C1 dedup fix (done — prevents future duplicates)

### Proposal P2.2: Memory TTL for pitfalls

Pitfall memories about specific gate failures become stale after the issue is fixed. Add TTL:
- pitfall kind: 7 days TTL (auto-archive after 7 days)
- pattern kind: 30 days TTL
- decision kind: no TTL (permanent)

**Implementation**: Add `expires_at` column to memories table, periodic cleanup in executor's `_run_ttl_cleanup`.

### Proposal P2.3: Memory relevance scoring

Current FTS5 returns results ranked by text match score. Add a recency boost:
```
final_score = fts_score * 0.7 + recency_score * 0.3
```
Where `recency_score = 1.0 / (1 + days_since_created)`

---

## P3: Chain Flow Optimization

### Proposal P3.1: Skip PM for retry chains

When a PM task fails the gate and auto-retries, the retry PM re-analyzes the same requirement. The PRD is already known — only the missing fields need to be added.

**Optimization**: For retry PM tasks (chain_depth > 0), inject the previous PRD into the prompt and instruct PM to only fill missing fields. Reduces PM inference from ~5 min to ~1 min.

### Proposal P3.2: Parallel gate + next-stage preparation

Current flow: gate check → if pass → create next task → executor picks up. Between gate pass and executor claim there's POLL_INTERVAL delay (10s).

**Optimization**: Pre-create next task during gate check (but in observer_hold). If gate passes, release immediately. Saves one poll cycle.

---

## P4: Observability Enhancements

### Proposal P4.1: Coordinator decision audit trail

Currently coordinator output is dumped to `logs/coordinator-{task_id}.raw.txt`. Add structured audit:

```python
# In _handle_coordinator_result:
audit_service.record(conn, project_id, "coordinator.decision", {
    "task_id": task_id,
    "decision": action_type,  # reply_only or create_pm_task
    "reply_preview": reply[:200],
    "pm_prompt_preview": prompt[:200] if action_type == "create_pm_task" else "",
    "context_update": context_update,
    "gate_retries": attempt_count,
    "execution_time_ms": elapsed_ms,
})
```

### Proposal P4.2: Pipeline execution timeline

Track wall-clock time for each stage:
```
coordinator: 5m 23s → PM: 4m 12s → Dev: 8m 45s → Test: 2m 30s → QA: 1m 50s → Merge: 15s
Total: 22m 55s
```

Store in `chain_events` with `elapsed_ms` field. Surface via MCP tool or API.

### Proposal P4.3: Memory search quality metrics

Log search→decision correlation:
- What was searched, what was found, what was used in the decision
- Track "memory hit rate" = memories found / memories referenced in coordinator output

---

## P5: Architecture Evolution

### Proposal P5.1: Coordinator as SDK call (not task queue)

Current architecture: coordinator runs through executor task queue → Claude CLI.
Better: coordinator runs inline in gateway (direct SDK call), only PM+ stages go through task queue.

```
Current:  gateway → task(type=task) → executor → CLI → JSON → executor creates PM
Better:   gateway → SDK call → JSON → gateway creates PM → task queue (PM→Dev→...)
```

**Benefits**: Eliminates executor overhead, reduces latency, simplifies architecture.
**Risk**: Needs careful error handling — gateway process handles coordinator failures directly.

### Proposal P5.2: Streaming coordinator replies

With SDK call, enable streaming: user sees "thinking..." then partial reply before PM task is created.

### Proposal P5.3: Multi-provider for different stages

Enable using OpenAI Codex for dev tasks (code generation) while keeping Anthropic for coordinator/PM/QA. pipeline_config already supports this — just need the provider execution path:

```python
if provider == "openai":
    # Use codex CLI or OpenAI SDK
    response = openai.chat.completions.create(model=model, ...)
```

---

## Priority Matrix

| Proposal | Impact | Effort | Priority |
|----------|--------|--------|----------|
| P1.1 SDK for coordinator | High (5min → 30s) | Medium | **P0** |
| P2.1 Dedup existing pitfalls | Medium | Low | **P0** |
| P4.1 Decision audit trail | Medium | Low | **P1** |
| P3.1 Skip PM for retries | Medium | Medium | **P1** |
| P4.2 Pipeline timeline | Medium | Medium | **P1** |
| P2.2 Memory TTL | Medium | Medium | **P2** |
| P1.2 Context cache | Low | Low | **P2** |
| P2.3 Relevance scoring | Low | Medium | **P2** |
| P4.3 Memory quality metrics | Low | Medium | **P3** |
| P3.2 Parallel gate | Low | Medium | **P3** |
| P5.1 Coordinator as SDK | High | High | **P1** (next round) |
| P5.2 Streaming replies | Medium | High | **P2** (next round) |
| P5.3 Multi-provider | Medium | High | **P3** (next round) |
