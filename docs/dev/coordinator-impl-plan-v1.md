# Coordinator Implementation Plan v1

Status: **awaiting review**
Date: 2026-03-29

## Scope

6 implementation items split into 3 batches by dependency order. Each batch is followed by unit tests + predict-verify validation.

---

## Batch 1: Coordinator Output Gate + Retry

**Goal**: Auto-retry when coordinator output is invalid, instead of silently discarding.

### 1.1 Gate validation function

File: `agent/executor_worker.py`

Add `_validate_coordinator_output(parsed: dict) -> (bool, str)`:

```python
def _validate_coordinator_output(self, parsed: dict) -> tuple[bool, str]:
    """Validate coordinator JSON output. Returns (valid, error_message)."""
    if not isinstance(parsed, dict):
        return False, "Output is not a JSON object"
    if parsed.get("schema_version") != "v1":
        return False, "Missing or invalid schema_version (expected 'v1')"
    if not parsed.get("reply"):
        return False, "Missing or empty 'reply' field"
    actions = parsed.get("actions")
    if not actions or not isinstance(actions, list):
        return False, "Missing or empty 'actions' array"
    for a in actions:
        atype = a.get("type", "")
        if atype not in ("reply_only", "create_pm_task"):
            return False, f"Invalid action type '{atype}' (allowed: reply_only, create_pm_task)"
        if atype == "create_pm_task":
            prompt = a.get("prompt", "")
            if not prompt or len(prompt) < 50:
                return False, f"create_pm_task prompt too short ({len(prompt)} chars, min 50)"
    return True, ""
```

### 1.2 Retry loop in coordinator execution path

Modify `_handle_coordinator_result`:
- After JSON parse, call `_validate_coordinator_output`
- Invalid -> re-invoke Claude CLI with error message injected into prompt, max 2 retries
- 3 failures -> task status=failed, error written to result

**Issue**: `_handle_coordinator_result` runs after `_execute_task` completes — CLI session already ended. Retry requires a new CLI session.

**Approach**: Lift retry logic to `run_once()` layer:
```
run_once:
  claim -> _execute_task (coordinator) -> get result
  -> _validate_coordinator_output
  -> invalid? -> _execute_task again (with retry prompt) -> max 2 times
  -> valid -> _handle_coordinator_result -> complete
```

### 1.3 Tests

- Unit tests: `test_coordinator_decisions.py`, new `TestCoordinatorGate` class
  - test_valid_reply_only_passes
  - test_valid_create_pm_passes
  - test_missing_schema_version_fails
  - test_empty_actions_fails
  - test_invalid_action_type_fails
  - test_short_prompt_fails

Estimated changed files: `agent/executor_worker.py`, `agent/tests/test_coordinator_decisions.py`

---

## Batch 2: AI Keyword Extraction + Memory English Normalization

**Goal**: Memory search supports Chinese+English input; memory storage normalized to English.

### 2.1 New LLM utility module

File: `agent/governance/llm_utils.py` (new)

```python
"""Lightweight LLM utility functions using Anthropic SDK directly (not Claude CLI)."""

def extract_keywords(text: str, max_keywords: int = 5) -> list[str]:
    """Extract English search keywords from text (any language).
    Uses haiku for speed. Fallback: naive split on failure."""

def translate_to_english(text: str) -> str:
    """Translate text to English if it contains non-ASCII.
    Uses haiku. Fallback: return original on failure."""
```

- Model: `claude-haiku-4-5-20251001`
- Requires `ANTHROPIC_API_KEY` environment variable
- Timeout: 5 seconds
- Failure fallback: `extract_keywords` -> `text.split()[:5]`; `translate_to_english` -> original text

### 2.2 Replace executor regex extraction

File: `agent/executor_worker.py` `_build_prompt` coordinator branch

```python
# Replace current re.findall logic
from governance.llm_utils import extract_keywords
keywords = extract_keywords(prompt)
for kw in keywords:
    extra = self._fetch_memories(kw)
    # dedup + merge
```

### 2.3 Memory write English normalization

File: `agent/governance/memory_service.py` `write_memory()`

```python
# Before write, check if content contains Chinese
import re
if re.search(r'[\u4e00-\u9fff]', content):
    from .llm_utils import translate_to_english
    original_content = content
    content = translate_to_english(content)
    backend_entry["structured"]["original_content"] = original_content
```

### 2.4 Tests

- Unit tests: `agent/tests/test_llm_utils.py` (new)
  - test_extract_keywords_english
  - test_extract_keywords_chinese
  - test_extract_keywords_fallback_on_failure
  - test_translate_to_english_chinese_input
  - test_translate_to_english_already_english (no-op)
  - test_translate_fallback_on_failure
- Mock Anthropic API calls — no real API key needed for tests

Estimated changed files: `agent/governance/llm_utils.py` (new), `agent/executor_worker.py`, `agent/governance/memory_service.py`, `agent/tests/test_llm_utils.py` (new)

---

## Batch 3: E2E Test Isolation + Test Scenarios

**Goal**: Establish isolated test environment, codify 3 coordinator scenarios as E2E tests.

### 3.1 Test project bootstrap

File: `agent/tests/test_e2e_coordinator.py` (new)

setUp:
```python
def setUp(self):
    self.project_id = "aming-claw-test"
    # Bootstrap test project
    self._api("POST", f"/api/version-update/{self.project_id}",
              {"chain_version": "test", "updated_by": "init"})
    # Register test domain pack
    self._api("POST", f"/api/mem/{self.project_id}/register-pack", {
        "pack_id": "test-pack",
        "kinds": {
            "pitfall": {"conflict_policy": "append_set"},
            "pattern": {"conflict_policy": "append_set"},
        }
    })
    # Seed test memories
    self._api("POST", f"/api/mem/{self.project_id}/write", {
        "module": "agent.executor_worker",
        "kind": "pitfall",
        "content": "Gate blocked at pm: PRD missing mandatory fields: verification, acceptance_criteria"
    })
    # Set observer mode ON
    self._api("POST", f"/api/project/{self.project_id}/observer-mode", {"enabled": True})
```

### 3.2 Scenario S1: create_pm_task

```python
def test_s1_feature_request_creates_pm(self):
    """Feature request -> coordinator outputs create_pm_task -> PM task in observer_hold."""
    task = self._create_and_run_coordinator(
        "Implement heartbeat extension for executor timeout")
    # Verify coordinator output
    self.assertEqual(task["result"]["actions"][0]["type"], "create_pm_task")
    # Verify PM task created
    pm_tasks = [t for t in self._list_tasks()
                if t["type"] == "pm" and t["status"] == "observer_hold"]
    self.assertGreaterEqual(len(pm_tasks), 1)
```

### 3.3 Scenario S5: reply_only

```python
def test_s5_greeting_reply_only(self):
    """Greeting -> coordinator outputs reply_only -> no PM task created."""
    task = self._create_and_run_coordinator("hello, how are you?")
    self.assertEqual(task["result"]["actions"][0]["type"], "reply_only")
    self.assertTrue(len(task["result"]["reply"]) > 0)
```

### 3.4 Scenario S3: duplicate detection

```python
def test_s3_duplicate_request(self):
    """Duplicate request -> coordinator sees conflict rule -> reply or create with context."""
    # Create first PM task
    self._create_and_run_coordinator("fix executor timeout")
    # Send same request again - conflict rules should detect duplicate
    task = self._create_and_run_coordinator("fix executor timeout")
    # Coordinator should reference the duplicate in its reply
    self.assertIn("duplicate", task["result"]["reply"].lower() +
                  task["result"].get("actions", [{}])[0].get("prompt", "").lower())
```

### 3.5 Runtime requirements

- Governance container must be running
- Executor must be running (scale=1)
- ANTHROPIC_API_KEY required (for Claude CLI)
- Each test takes ~5-6 minutes (CLI startup + inference)
- pytest marker: `@pytest.mark.e2e` to separate from unit tests

Estimated changed files: `agent/tests/test_e2e_coordinator.py` (new), `agent/tests/conftest.py` (add e2e marker)

---

## Batch 4: Per-Role Model Selection (pipeline_config integration)

**Goal**: Connect existing `pipeline_config.py` to actual AI invocations. Each role uses its configured provider+model. Users can configure any provider (Anthropic, OpenAI/Codex, etc.) per role.

### Current State

- `pipeline_config.py` already supports per-role provider+model config (YAML + env var override)
- `ai_lifecycle.py` ignores it — always runs `claude` CLI with default model, no `--model` flag
- `llm_utils.py` (Batch 2) will use Anthropic SDK directly — also needs model config

### 4.1 Wire pipeline_config into ai_lifecycle

File: `agent/ai_lifecycle.py` `create_session()`

```python
from pipeline_config import PipelineConfig

config = PipelineConfig()
provider, model = config.get_role_config(role)  # e.g. ("anthropic", "claude-sonnet-4-6")

if provider == "anthropic":
    # Claude CLI path
    cmd = [claude_bin, "-p", "--model", model, ...]
elif provider == "openai":
    # OpenAI Codex path (future) — use different CLI or SDK
    cmd = [codex_bin, "--model", model, ...]
```

### 4.2 Wire pipeline_config into llm_utils

File: `agent/governance/llm_utils.py`

```python
from pipeline_config import PipelineConfig

def _get_utility_model() -> tuple[str, str]:
    """Get provider+model for lightweight utility calls (keyword extraction, translation).
    Uses pipeline_config 'utility' role if defined, otherwise defaults to haiku."""
    config = PipelineConfig()
    try:
        return config.get_role_config("utility")
    except KeyError:
        return ("anthropic", "claude-haiku-4-5-20251001")
```

### 4.3 Update pipeline_config.yaml.example

```yaml
pipeline:
  default:
    provider: anthropic
    model: claude-sonnet-4-6

  roles:
    coordinator:
      provider: anthropic
      model: claude-sonnet-4-6     # fast decision, saves opus quota
    pm:
      provider: anthropic
      model: claude-sonnet-4-6
    dev:
      provider: anthropic
      model: claude-opus-4-6       # strongest model for code writing
    test:
      provider: anthropic
      model: claude-sonnet-4-6
    qa:
      provider: anthropic
      model: claude-sonnet-4-6
    utility:
      provider: anthropic
      model: claude-haiku-4-5-20251001  # keyword extraction, translation
```

Environment variable override examples:
```bash
PIPELINE_ROLE_COORDINATOR_PROVIDER=anthropic
PIPELINE_ROLE_COORDINATOR_MODEL=claude-sonnet-4-6
PIPELINE_ROLE_DEV_PROVIDER=openai
PIPELINE_ROLE_DEV_MODEL=codex     # future: OpenAI Codex for dev
PIPELINE_ROLE_UTILITY_MODEL=claude-haiku-4-5-20251001
```

### 4.4 Provider abstraction for future evolution

File: `agent/ai_lifecycle.py`

Current: only Claude CLI (`claude -p`).
Future providers need different invocation paths:

| Provider | Invocation | Status |
|----------|------------|--------|
| anthropic | `claude -p --model X` (Claude CLI) | Implement now |
| anthropic-sdk | `anthropic.messages.create(model=X)` (direct SDK) | Used by llm_utils |
| openai | `codex --model X` or OpenAI SDK | Future — stub only |

For now, implement anthropic path. Add provider dispatch stub:

```python
if provider == "anthropic":
    return self._run_claude_cli(role, model, prompt_file, allowed_tools, cwd, env)
else:
    raise NotImplementedError(f"Provider '{provider}' not yet supported for role '{role}'. "
                              f"Supported: anthropic. Configure in pipeline_config.yaml")
```

### 4.5 Tests

- Unit tests in `test_coordinator_decisions.py`:
  - test_pipeline_config_default_model
  - test_pipeline_config_role_override
  - test_pipeline_config_env_override
- Existing `pipeline_config.py` tests may already cover this — verify

Estimated changed files: `agent/ai_lifecycle.py`, `agent/governance/llm_utils.py`, `agent/pipeline_config.yaml.example`

---

## Deferred Items

| Item | Reason | When |
|------|--------|------|
| Conversation context (session_context) | Depends on Batch 1 gate + Batch 3 test infra | Next round |
| S7 stop/prioritize tasks | Requires new action types | Next round |
| S9 ambiguous request clarification | Requires multi-turn dialogue | After session_context |
| observer-rules.md update | Sync after gate implementation | After Batch 1 |
| OpenAI/Codex provider support | Stub in Batch 4, full implementation when needed | On demand |

---

## Execution Order

```
Batch 1 (gate + retry)
  -> run unit tests -> deploy -> predict-verify
  -> update observer-rules.md + coordinator-rules.md

Batch 2 (AI keywords + memory i18n)
  -> run unit tests -> deploy -> predict-verify (validate Chinese+English memory search)

Batch 3 (E2E isolation + scenarios)
  -> bootstrap test project -> run E2E -> verify S1/S3/S5

Batch 4 (per-role model selection)
  -> wire pipeline_config -> deploy -> verify coordinator uses sonnet, dev uses opus
```

Estimated total: 5 new files, 5 modified files, ~18 new test cases

---

## Batch 5: Revised E2E + llm_utils CLI Migration (2026-03-29 late session)

### Context

- User is Claude Max subscriber — no ANTHROPIC_API_KEY / API token quota
- All AI calls must go through Claude CLI (uses subscription)
- llm_utils.py (Batch 2) used direct Anthropic SDK — won't work without API key
- E2E tests timed out at 480s (coordinator CLI takes ~5 min per decision)

### 5.1 llm_utils: SDK → CLI migration

File: `agent/governance/llm_utils.py`

Replace `_call_anthropic()` (direct HTTP to api.anthropic.com) with Claude CLI call:

```python
def _call_cli(prompt: str, model: str = "", max_turns: int = 1) -> str:
    """Call Claude CLI with a single prompt. Uses subscription quota."""
    import subprocess
    claude_bin = os.getenv("CLAUDE_BIN", "claude")
    cmd = [claude_bin, "-p", "--max-turns", str(max_turns)]
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)  # prompt as positional arg

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return result.stdout.strip()
```

Model: use `sonnet` via pipeline_config utility role (fast, included in Max subscription).

### 5.2 Batched E2E test design

**Constraint**: Each CLI call ~5 min. Minimize calls by batching independent scenarios.

**Group A** (1 CLI call, ~5 min): Independent scenarios
```
Input: [
  {id: "S1", prompt: "Implement heartbeat extension for executor timeout...",
   memories: [...], queue: [], context: {}},
  {id: "S5", prompt: "Hello! How are you doing today?",
   memories: [], queue: [], context: {}},
]
Output: [
  {id: "S1", schema_version: "v1", actions: [{type: "create_pm_task", ...}], ...},
  {id: "S5", schema_version: "v1", actions: [{type: "reply_only"}], ...},
]
```

**Group B** (1 CLI call, ~5 min): Context-dependent scenarios
```
Input: [
  {id: "S3", prompt: "Fix the executor subprocess timeout",
   memories: [...], queue: [{existing PM task for same topic}], context: {}},
  {id: "S2", prompt: "Add dark mode to settings",
   memories: [], queue: [{5 active tasks...}], context: {}},
]
Output: [
  {id: "S3", ...},  // should reference duplicate/existing task
  {id: "S2", ...},  // should acknowledge queue congestion
]
```

**Total: 2 CLI calls = ~10 min** (down from 15-20 min)

### 5.3 E2E batch prompt construction

E2E test constructs its own system prompt (not using production `_build_prompt`):

```python
BATCH_SYSTEM_PROMPT = """You are the project Coordinator. You will receive multiple user messages,
each with its own context (memories, queue, runtime context).

For EACH message, output an independent coordinator decision.

Output a JSON array where each element follows this schema:
{
  "id": "scenario_id",
  "schema_version": "v1",
  "reply": "...",
  "actions": [{"type": "reply_only"} or {"type": "create_pm_task", "prompt": "...>=50 chars"}],
  "context_update": {"current_focus": "...", "last_decision": "..."}
}

Decision rules per message:
- Greetings/thanks → reply_only
- Status queries → reply_only
- Code change requests → create_pm_task (never create_dev_task directly)

Output ONLY the JSON array. No other text."""
```

### 5.4 E2E test fixture changes

File: `agent/tests/test_e2e_coordinator.py`

- Remove executor subprocess management (not needed — test calls CLI directly)
- Add `_run_batch(scenarios: list[dict]) -> list[dict]` helper
- Pre-fetch memories per scenario from governance API
- Validate each output element independently against gate rules
- Timeout: 600s per batch (10 min)

### 5.5 Fix setUp stale data issue

Add cleanup at start of setUp:
```python
# Clean all active tasks in test project before seeding
tasks = _api("GET", f"/api/task/{PROJECT_ID}/list")
for t in tasks.get("tasks", []):
    if t["status"] in ("queued", "claimed", "observer_hold"):
        _api("POST", f"/api/task/{PROJECT_ID}/complete",
             {"task_id": t["task_id"], "status": "succeeded",
              "result": {"note": "e2e cleanup"}})
```

### 5.6 Estimated changes

| File | Change |
|------|--------|
| `agent/governance/llm_utils.py` | `_call_anthropic` → `_call_cli` (Claude CLI) |
| `agent/tests/test_e2e_coordinator.py` | Rewrite: batch mode, no executor subprocess, direct CLI |
| `agent/tests/test_llm_utils.py` | Update mocks for CLI call instead of API call |

---

## Batch 6: Deferred to next round

| Item | Reason |
|------|--------|
| Conversation context (session_context) | Needs design review for chat history format |
| S7 stop/prioritize tasks | Needs new action types |
| S9 ambiguous request clarification | Needs multi-turn |
| P2.2 Memory TTL | Not blocking |
| P4.2 Pipeline timeline | Not blocking |

---

## Full Execution History

```
Batch 1 (gate + retry) .................. DONE ✅ (363 unit tests pass)
Batch 2 (AI keywords + memory i18n) ..... DONE ✅ (SDK version — needs CLI migration)
Batch 3 (E2E infra) ..................... DONE ✅ (but timed out, needs batch redesign)
Batch 4 (per-role model selection) ...... DONE ✅
Batch 5 (llm_utils CLI + batched E2E) ... DONE ✅ (Group A 12s, Group B 17s, 4/4 passed)
Batch 6 (pending tests + infra) ......... PENDING — next batch
Batch 7 (deferred items) ................ NEXT ROUND
```

---

## Batch 6: Pending Unit Tests + Node Schema Extension (awaiting review)

### 6.1 Missing unit tests to make coordinator E2E reliable

| # | Test | What it covers | Gap it fills |
|---|------|---------------|-------------|
| T1 | `_build_prompt` coordinator branch | Keywords extracted → memories searched → queue/context injected → final prompt structure | Prompt assembly correctness |
| T2 | Memory write English normalization | Chinese content → translated → original preserved in structured | Memory data correctness |
| T3 | `_handle_coordinator_result` v1 parse | v1 JSON → gate → create PM / reply_only action execution | Output processing correctness |
| T4 | Prompt consistency check | `_build_prompt` output contains all key instructions from E2E batch prompt | Bridges gap between unit test prompt and E2E prompt |

### 6.2 Node schema: `verify_requires` field

Current `deps` field = implementation dependency (code).
Need `verify_requires` field = test/verification dependency (E2E).

```
L4.32 Coordinator E2E ✅
  → L4.33 PM E2E (verify_requires: [L4.32])
    → L4.34 Dev E2E (verify_requires: [L4.33])
      → ...
```

Re-verification rule: when node X code changes, re-run X's E2E + all downstream verify_requires. Upstream nodes unaffected if unchanged.

Changes needed:
- `agent/graph_validator.py`: accept `verify_requires` field in node schema
- `agent/governance/state_service.py`: check verify_requires before allowing E2E pass
- `docs/aming-claw-acceptance-graph.md`: add verify_requires to E2E nodes

### 6.3 E2E test model + prompt mismatch finding

E2E batch tests use simplified prompt (~500 chars) + explicit `--model sonnet` → 12s.
Production executor uses full system prompt (~4000 chars) + default model → 5 min.

The E2E tests validate **decision quality** not **production execution path**.
T4 (prompt consistency) bridges this gap by verifying key instructions are present in both.

### 6.4 Estimated changes

| File | Change |
|------|--------|
| `agent/tests/test_coordinator_decisions.py` | Add T1-T4 test classes |
| `agent/graph_validator.py` | Accept `verify_requires` field |
| `docs/aming-claw-acceptance-graph.md` | Add verify_requires to E2E nodes |

---

## Batch 7: Two-Round Coordinator + Conversation History + E2E Update (awaiting review)

### Context

Coordinator currently does single-round CLI call. Memory search keywords come from llm_utils
(separate CLI call, ~5s). This batch replaces llm_utils keyword extraction with a two-round
coordinator design where the coordinator itself specifies what to search.

### 7.1 Design: Two-Round Coordinator

```
Round 1 (coordinator CLI, --max-turns 1, ~10s):
  Input: user prompt + last 10 conversation history entries + queue status + runtime context
  Output (option A): {"actions": [{"type": "query_memory", "queries": ["executor timeout", "heartbeat"]}]}
  Output (option B): {"actions": [{"type": "reply_only"}]} (no memory needed, greeting etc.)
  Output (option C): {"actions": [{"type": "create_pm_task", "prompt": "..."}]} (enough context already)

If option A (query_memory):
  Executor searches governance FTS5 with each query (top_k=3, max 3 queries = max 9 results)
  Dedup by memory_id

Round 2 (coordinator CLI, --max-turns 1, ~10s):
  Input: user prompt + conversation history + memory results + queue + context
  Output: reply_only or create_pm_task ONLY (query_memory NOT allowed in round 2)
```

### 7.2 New action type: query_memory

```json
{
  "type": "query_memory",
  "queries": ["keyword1", "keyword2 phrase", "keyword3"]
}
```

Gate rules:
- Round 1: allowed actions = `reply_only`, `create_pm_task`, `query_memory`
- Round 2: allowed actions = `reply_only`, `create_pm_task` (no query_memory — prevents loop)
- `queries` must be non-empty array of strings, max 3 items

### 7.3 Conversation history (session_context)

**Storage**: `session_context` table in governance DB (already created, migration v10)

**Write**: after each coordinator decision, executor writes:
```sql
INSERT INTO session_context (project_id, task_id, entry_type, content, metadata_json, created_at, created_by)
VALUES (?, ?, 'coordinator_turn', ?, ?, ?, 'executor')
```

Content JSON:
```json
{
  "user_message": "original user prompt",
  "decision": "reply_only | create_pm_task | query_memory",
  "reply_preview": "first 200 chars of reply",
  "pm_task_id": "task-xxx (if create_pm_task)",
  "queries": ["..."] (if query_memory was used)
}
```

**Read**: executor._build_prompt fetches last 10 entries:
```sql
SELECT content, created_at FROM session_context
WHERE project_id = ? AND entry_type = 'coordinator_turn'
ORDER BY created_at DESC LIMIT 10
```

Injected into prompt as `## Recent Conversation (last 10 turns)`.

**Retention**: all entries kept (no TTL). Table can be cleaned manually if needed.

### 7.4 Changes to executor._build_prompt coordinator branch

```python
# BEFORE: llm_utils.extract_keywords + multiple memory searches
# AFTER: no pre-fetch in round 1, memory results injected only in round 2

# Round 1 prompt assembly:
parts = [prompt]
parts.append(f"\nproject_id: {self.project_id}")

# Inject conversation history (last 10)
history = self._fetch_conversation_history()
if history:
    parts.append("\n## Recent Conversation (last 10 turns)")
    for h in history:
        parts.append(f"  - [{h['created_at']}] user: {h['content']['user_message'][:80]}")
        parts.append(f"    decision: {h['content']['decision']}")

# Inject queue + context (same as before)
# NO memory injection in round 1

# Round 2 prompt assembly (only if round 1 returned query_memory):
# Same as round 1 PLUS:
parts.append("\n## Memory Search Results")
for m in memory_results:
    parts.append(f"  - [{m['kind']}] {m['content'][:150]}")
```

### 7.5 Changes to _handle_coordinator_result

Add `query_memory` handling:

```python
# In _handle_coordinator_v1:
if action_type == "query_memory":
    queries = action.get("queries", [])
    if not queries or not isinstance(queries, list):
        log.warning("coordinator.gate: query_memory missing queries")
        continue
    queries = queries[:3]  # max 3 queries
    log.info("coordinator.action: query_memory queries=%s", queries)
    # Return queries to caller for round 2
    self._pending_queries = queries
```

### 7.6 Changes to run_once coordinator flow

```python
# In run_once, after _execute_task:
if task_type in ("coordinator", "task") and status == "succeeded":
    # Round 1: parse output
    parsed = self._extract_json(raw)
    valid, error = self._validate_coordinator_output(parsed, round=1)

    if valid and self._has_query_memory(parsed):
        # Execute memory search with coordinator's queries
        queries = self._extract_queries(parsed)
        memory_results = []
        for q in queries[:3]:
            results = self._fetch_memories(q)
            memory_results.extend(results)
        memory_results = self._dedup_memories(memory_results)

        # Round 2: re-run with memory results injected
        task_copy = dict(task)
        task_copy["_round2_memories"] = memory_results
        task_copy["_round"] = 2
        outcome = self._execute_task(task_copy)
        # Parse and validate round 2 output (query_memory NOT allowed)
        ...
    else:
        # Single round — reply_only or create_pm_task directly
        self._handle_coordinator_result(task, result)

    # Write conversation history
    self._write_conversation_history(task, parsed)
```

### 7.7 llm_utils changes

- `extract_keywords()`: no longer called in coordinator path (coordinator specifies its own queries)
- `translate_to_english()`: still used by memory_service.write_memory for Chinese→English normalization at write time
- `_fallback_keywords()`: kept as fallback if coordinator doesn't output query_memory

### 7.8 ROLE_PROMPTS coordinator update

Add query_memory to output format:

```
For query_memory (need to search before deciding):
{"schema_version": "v1", "actions": [{"type": "query_memory", "queries": ["keyword1", "keyword2"]}]}
```

Update decision rules:
```
1. Greetings/thanks → reply_only (no memory needed)
2. Status queries → reply_only (context pre-injected)
3. Task requests where you need memory context → query_memory first
4. Task requests where pre-injected context is sufficient → create_pm_task directly
```

### 7.9 Gate validation update

```python
def _validate_coordinator_output(self, parsed, round=1):
    # ... existing G1-G7 rules ...
    for a in actions:
        atype = a.get("type", "")
        if round == 1:
            allowed = ("reply_only", "create_pm_task", "query_memory")
        else:
            allowed = ("reply_only", "create_pm_task")  # no query_memory in round 2

        if atype == "query_memory":
            queries = a.get("queries", [])
            if not queries or not isinstance(queries, list) or len(queries) > 3:
                return False, "query_memory: queries must be non-empty list, max 3 items"
            if not all(isinstance(q, str) and len(q) >= 2 for q in queries):
                return False, "query_memory: each query must be a string >= 2 chars"
```

### 7.10 E2E test updates

**Group C (llm_utils) changes:**
- C1-C2 (keyword extraction): keep as llm_utils regression tests (still used for memory write path)
- C3-C4 (translation): keep as-is
- New C5: test query_memory round-trip (coordinator outputs queries → executor searches → round 2 decision)

**Group A/B changes:**
- Update batch system prompt to include query_memory as valid action
- S1 may now output query_memory first, then create_pm_task in round 2
- E2E batch format needs to support two-round simulation OR test final output only

**New Group D: conversation history tests**
- D1: write history → read back → verify last 10
- D2: coordinator with history context references previous conversation

### 7.11 Acceptance graph nodes

```
L4.34  Two-Round Coordinator + query_memory  [impl:pending]
    deps:[L4.25, L4.29]
    verify_requires:[L4.33]
    primary:[agent/executor_worker.py, agent/role_permissions.py]
    test:[agent/tests/test_coordinator_decisions.py]

L4.35  Conversation History (session_context)  [impl:pending]
    deps:[L4.28, L4.34]
    primary:[agent/executor_worker.py, agent/governance/server.py]
    test:[agent/tests/test_coordinator_decisions.py]
```

### 7.12 Estimated changes

| File | Change |
|------|--------|
| `agent/executor_worker.py` | Two-round flow in run_once, query_memory handling, conversation history write/read |
| `agent/role_permissions.py` | ROLE_PROMPTS coordinator: add query_memory output format + decision rules |
| `agent/executor_worker.py` | _build_prompt: inject conversation history, round 2 memory injection |
| `agent/executor_worker.py` | _validate_coordinator_output: round parameter, query_memory validation |
| `agent/governance/server.py` | session_context read/write API endpoints (or direct DB in executor) |
| `agent/tests/test_coordinator_decisions.py` | Gate tests for query_memory, conversation history tests |
| `agent/tests/test_e2e_coordinator.py` | Group C5 (query_memory round-trip), Group D (conversation history) |
| `docs/coordinator-rules.md` | Add query_memory action, two-round flow description |
| `docs/observer-rules.md` | Add query_memory to observability table |
| `docs/aming-claw-acceptance-graph.md` | L4.34, L4.35 nodes |
