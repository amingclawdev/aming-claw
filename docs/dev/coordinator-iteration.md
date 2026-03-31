# Coordinator — Development Iteration Log

This file tracks design discussions, decisions, and changes to the coordinator role across sessions. Chronological, append-only. Not bound to acceptance graph nodes.

Related guiding docs (these ARE node-bound):
- `docs/coordinator-rules.md` — coordinator role specification (L4.25)
- `docs/pm-rules.md` — PM role specification (L4.26)
- `docs/observer-rules.md` — observer operation rules (L4.23)

---

## 2026-03-29 Session 1: Coordinator Architecture Discovery & Fixes

### Problems Found

**Architecture issues:**
1. `TASK_ROLE_MAP` mapped `type="task"` to `"dev"` — executor ran coordinator tasks as dev role, directly writing code instead of making decisions
2. Gateway `classify_message()` pre-filtered messages with keyword matching — English keyword coverage was incomplete (`"change"`, `"replace"`, `"migrate"` missing), Chinese-only keywords dominated
3. `_needs_pm_analysis()` did second-layer keyword matching — redundant with coordinator's own decision capability
4. Coordinator had no role specification document

**Coordinator tool access iterations:**
- v1: coordinator had Bash → used `curl POST` to directly create tasks via governance API, bypassing executor action validation and observer_hold
- v2: added "curl GET only" prompt restriction → AI still used `curl POST` (soft constraint not respected)
- v3: removed Bash, kept Read/Grep/Glob → coordinator spent 10+ minutes reading source code files
- v4 (final): removed ALL tools, added `--max-turns 1` → coordinator outputs pure JSON in ~5 minutes, executor handles all API calls

**Memory pre-fetch issues:**
- `_build_prompt` used `re.findall(r'\b(?:fix|add|implement|...)\s+\w+')` for key term extraction — only 8 English verbs, zero Chinese coverage
- `prompt[:100]` as raw FTS query — too long, too noisy, same results as key term search

### Decisions Made

1. **Coordinator = pure decision role, no tools** — all context pre-injected by executor, AI outputs JSON only
2. **All non-query messages go to PM** — `_needs_pm_analysis()` simplified to always-true except pure status queries
3. **Coordinator cannot create dev/test/qa tasks** — must go through PM. Enforced in role_permissions.py (denied set) and gate validation
4. **Memory/queue/context pre-fetched by executor** — `_build_prompt` calls governance APIs, injects results into prompt
5. **Coordinator output gate** — `_handle_coordinator_result` validates JSON format, only allows `reply_only` and `create_pm_task` actions
6. **Raw output dump** — `logs/coordinator-{task_id}.raw.txt` for debugging

### Verified Flow (final)

```
task_create(type="task") → observer_hold
  → release → executor claim (role=coordinator, --max-turns 1, no tools)
  → executor pre-fetches: memories (2x FTS5), queue, context
  → injects into prompt
  → Claude CLI outputs v1 JSON (~5 min)
  → _handle_coordinator_result parses → gate validates
  → creates PM task (observer_hold) or sends reply
  → context_update saved to DB
```

### Coordinator Output Format (agreed)

**reply_only:**
```json
{
  "schema_version": "v1",
  "reply": "Reply text (required, non-empty)",
  "actions": [{"type": "reply_only"}],
  "context_update": {"current_focus": "topic", "last_decision": "reply_only"}
}
```

**create_pm_task:**
```json
{
  "schema_version": "v1",
  "reply": "Summary for user (required, non-empty)",
  "actions": [{
    "type": "create_pm_task",
    "prompt": "Detailed description with memory context (required, >=50 chars)"
  }],
  "context_update": {"current_focus": "topic", "last_decision": "create_pm_task"}
}
```

Note: `target_files` and `related_nodes` are PM's responsibility, not coordinator's. Coordinator has no search tools to determine file paths.

### Gate Validation Rules

| # | Field | Rule | On failure |
|---|-------|------|-----------|
| G1 | whole output | must be valid JSON object | retry |
| G2 | schema_version | must exist, value "v1" | retry |
| G3 | reply | must exist, non-empty string | retry |
| G4 | actions | must be non-empty array | retry |
| G5 | actions[*].type | only "reply_only" or "create_pm_task" | reject that action |
| G6 | create_pm_task.prompt | required, non-empty, >=50 chars | retry ("prompt too short") |
| G7 | context_update | optional, must be dict if present | ignore (don't save) |

Retry: up to 2 retries with error message injected. 3 failures → task failed.

### Pending Items (awaiting review)

1. **AI keyword extraction** — replace regex with haiku API call for memory search terms (supports Chinese)
2. **Memory English normalization** — write-time translation to English for consistent FTS matching
3. **Conversation context** — session_context table populated with chat history for multi-turn awareness
4. **E2E test isolation** — `aming-claw-test` project + test domain pack
5. **Gate retry mechanism** — coordinator output gate with retry on invalid format
6. **Coordinator output gate implementation** — enforce G1-G7 rules in _handle_coordinator_result

### Untested Scenarios

| Scenario | Description | Blocked on |
|----------|-------------|-----------|
| S2 | Queue congestion — many tasks queued, new request | E2E test infra |
| S3 | Duplicate request — conflict rules integration | E2E test infra |
| S5 | Greeting → reply_only | E2E test infra |
| S6 | Status query → reply_only with context | E2E test infra |
| S7 | Stop/prioritize task | New action types needed |
| S8 | Follow-up context reference | session_context needed |
| S9 | Ambiguous request needing clarification | Multi-turn needed |
| S10 | Multi-task request | Actions array handling |

---

## 2026-03-29 Session 1 (continued): Batch Implementation

### Batch 1: Output Gate + Retry
- `_validate_coordinator_output()` — G1-G7 validation rules
- Retry loop in `run_once()` — up to 2 retries with error feedback, 3 failures = task failed
- 7 unit tests: TestCoordinatorGate

### Batch 2: AI Keyword Extraction + Memory i18n
- New module: `agent/governance/llm_utils.py` — `extract_keywords()` + `translate_to_english()`
- Uses Anthropic API directly (haiku), 5s timeout, fallback to naive extraction/original text
- executor._build_prompt: replaced regex with `extract_keywords()`, search per keyword with dedup
- memory_service.write_memory: auto-translate Chinese content, preserve original in structured.original_content
- 9 unit tests: test_llm_utils.py

### Batch 3: E2E Test Infrastructure
- New: `agent/tests/test_e2e_coordinator.py` — isolated test project `aming-claw-test`
- Test domain pack with pitfall + pattern seed memories
- Scenarios: S1 (create_pm_task), S5 (reply_only), S3 (duplicate detection)
- `@pytest.mark.e2e` marker, ~5-6 min per test
- New: `agent/tests/conftest.py` — e2e marker registration

### Batch 4: Per-Role Model Selection
- ai_lifecycle.py: reads `PipelineConfig().get_role_config(role)`, passes `--model` to Claude CLI
- pipeline_config.py: added coordinator + utility roles to validation
- pipeline_config.yaml.example: full role config with rationale comments
- Provider abstraction stub (non-anthropic warns, future openai/codex support)

### New Nodes: L4.29-L4.32

---

## 2026-03-29 Session 1 (continued): dbservice Fix

### Root Cause
DockerBackend.search() used wrong endpoint and field names:
- Endpoint: `/search` (404) → fixed to `/knowledge/search`
- Request fields: `project_id`+`top_k` → fixed to `scope`+`limit`
- Response mapping: `r.content` → fixed to `r.doc.content` (doc wrapper)
- Write field: `body` (rejected) → fixed to `content`

### Result
- Semantic search via dbservice now works (was always falling back to FTS5)
- Two-layer write confirmed: governance DB (FTS5) + dbservice (vector index)
- Two-layer search confirmed: semantic first → FTS5 fallback
- E2E Group E: E1 (write+search), E2 (semantic), E3 (index_status) — 3/3 passed
- Test isolation: all E2E writes to aming-claw-test project
- Production DB cleaned: 1 test entry archived

### New Node
L4.36: Dbservice E2E (verify_requires chain: L4.36 → L4.33 → L4.32)
