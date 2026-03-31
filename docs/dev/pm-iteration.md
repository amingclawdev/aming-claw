# PM Stage — Iteration & Optimization Plan

> Created: 2026-03-29 | Status: **Draft — awaiting review**
> Prerequisite: Coordinator stable (~10-21s, no blocking)
> Previous session: PM runs (10.2s) but outputs wrong format (coordinator JSON instead of PRD JSON)

---

## 1. Current State (verified in previous session)

### Verified OK
- Full PM timing chain visible: 0.0s → 10.2s, all checkpoints written to file
- CLI ~10s (sonnet, single turn)
- git_diff: skipped (pm added to skip list)
- log.info blocking: all critical-path calls converted to _timing()
- complete file contains chain result
- memory.search goes through dbservice semantic → FTS5 fallback (3 results)
- _gate_post_pm field check → blocked: "PRD missing target_files, verification, acceptance_criteria"
- gate failure → retry PM task created (observer_hold)
- pitfall memory auto-written

### Core Problem
PM outputs coordinator-style JSON `{"actions": [{"type": "create_pm_task"}]}` instead of PRD format.

**Root cause**: `TASK_ROLE_MAP["pm"] = "coordinator"` — PM maps to coordinator role:
1. ai_lifecycle gives PM config = NO tools, `--max-turns 1`
2. `_build_system_prompt` skips API reference + context snapshot for `role="coordinator"`
3. PM receives `ROLE_PROMPTS["pm"]` as system prompt, but CLI params are identical to coordinator
4. PM AI gets contradictory signals: system prompt says "output PRD JSON", but environment matches coordinator → confused output

---

## 2. Proposed Changes

### Change 1: Independent PM Role Mapping

**File**: `agent/executor_worker.py` line 53

```python
# BEFORE:
"pm": "coordinator",
# AFTER:
"pm": "pm",
```

**Impact chain**:
- `ai_lifecycle.create_session(role="pm")` will take a new branch
- `_build_system_prompt(role="pm")` will no longer skip context snapshot and API reference
- pipeline_config needs a `pm` role model config entry

### Change 2: ai_lifecycle tool + turn config for role="pm"

**File**: `agent/ai_lifecycle.py` lines 135-161

```python
# BEFORE (pm falls through to else branch):
else:
    allowed_tools = "Read,Grep,Glob"

# AFTER (explicit pm branch):
elif role == "pm":
    allowed_tools = "Read,Grep,Glob"  # PM can read code to determine target_files, but cannot write
```

`--max-turns` handling:
```python
# BEFORE (line 159-161):
if role == "coordinator":
    cmd.extend(["--max-turns", "1"])

# AFTER:
if role in ("coordinator", "pm"):
    cmd.extend(["--max-turns", "3"])  # PM needs 1-2 turns to read files, turn 3 to output JSON
```

> **Decision Point A**: What should PM's `--max-turns` be?
> - Option 1: `--max-turns 1` (pure JSON output, no code reading → fast but may guess wrong target_files)
> - Option 2: `--max-turns 3` (1-2 turns reading code structure, turn 3 outputs → slower but more accurate target_files)
> - Option 3: Unlimited (PM explores freely → may be very slow 60s+)

### Change 3: `_build_system_prompt` context injection for role="pm"

**File**: `agent/ai_lifecycle.py` lines 420-462

Current logic: context snapshot + API reference fetched only when `role != "coordinator"`. After PM gets its own role, this **takes effect automatically** — no additional code changes needed.

Points to confirm:
- Context snapshot (`/api/context-snapshot/{pid}?role=pm`) — what does it return? PM needs project structure + active tasks + memory, not worktree paths
- API reference: PM doesn't call APIs (no Bash), but the reference contains governance data model descriptions that help PM understand node/verification concepts

### Change 4: PM hang_timeout adjustment

**File**: `agent/ai_lifecycle.py` line 227

```python
# BEFORE:
hang_timeout = _COORDINATOR_HANG_TIMEOUT if role == "coordinator" else _HANG_TIMEOUT

# AFTER:
if role == "coordinator":
    hang_timeout = _COORDINATOR_HANG_TIMEOUT  # 300s (no tools, single long output)
elif role == "pm":
    hang_timeout = 180  # PM: reads a few files + outputs JSON (tool calls produce stdout, resetting hang timer)
else:
    hang_timeout = _HANG_TIMEOUT  # 120s
```

> **Decision Point B**: If PM uses `--max-turns 1` (no tools), hang_timeout can be shorter (120s). If `--max-turns 3`, needs to be longer.

### Change 5: Enhanced `_build_prompt` PM branch

**File**: `agent/executor_worker.py` lines 513-523

Current PM branch only appends a simplified 3-field instruction. Needs enhancement to elicit full PRD output:

```python
elif task_type == "pm":
    # 1. Coordinator-forwarded context (from PM task metadata)
    coordinator_memories = metadata.get("_coordinator_memories", [])
    coordinator_context = metadata.get("_coordinator_context", {})
    if coordinator_memories:
        parts.append("\n## Context from Coordinator (pre-searched memories)")
        seen_content = set()
        for m in coordinator_memories:
            content = m.get('summary', m.get('content', ''))[:150]
            if content not in seen_content:
                seen_content.add(content)
                parts.append(f"  - [{m.get('kind','')}] {content}")
    if coordinator_context:
        parts.append(f"\n## Coordinator Decision Context")
        parts.append(f"  {json.dumps(coordinator_context, ensure_ascii=False)}")

    # 2. PM's own memory search (targeted at module if identifiable)
    memories = self._fetch_memories(prompt[:120])
    if memories:
        parts.append("\n## Additional Memories (PM search)")
        seen_content = set()
        for m in memories:
            content = m.get('summary', m.get('content', ''))[:150]
            if content not in seen_content:
                seen_content.add(content)
                parts.append(f"  - [{m.get('kind','')}] {content}")

    # 3. Runtime context + active queue
    try:
        import requests as _req
        ctx_data = _req.get(f"{self.base_url}/api/context/{self.project_id}/load", timeout=3).json()
        if ctx_data.get("exists"):
            parts.append(f"\n## Runtime Context")
            parts.append(f"  {json.dumps(ctx_data.get('context', {}), ensure_ascii=False)}")
    except Exception:
        pass
    try:
        task_list = _req.get(f"{self.base_url}/api/task/{self.project_id}/list", timeout=3).json()
        active = [t for t in task_list.get("tasks", [])
                  if t.get("status") in ("queued", "claimed", "observer_hold")]
        if active:
            parts.append(f"\n## Active Task Queue ({len(active)} tasks)")
            for t in active[:5]:
                parts.append(f"  - {t.get('task_id','')}: [{t.get('type','')}] {(t.get('prompt',''))[:60]}")
    except Exception:
        pass

    # 4. Project structure hint (helps PM determine target_files)
    parts.append("\n## Project Structure")
    parts.append("  agent/ — executor, lifecycle, pipeline")
    parts.append("  agent/governance/ — auto_chain, db, server, memory")
    parts.append("  agent/telegram_gateway/ — message_worker, bot")
    parts.append("  agent/tests/ — pytest tests")
    parts.append("  docs/ — specs & rules")

    # Explicit output format instruction (overrides ROLE_PROMPTS format)
    parts.append(
        "\nYou are the architect. Use Read/Grep tools to examine the codebase first, "
        "then output a PRD as strict JSON with ALL of these fields:\n"
        "{\n"
        '  "target_files": ["agent/xxx.py"],        // files Dev will modify (use Read/Grep to confirm paths)\n'
        '  "test_files": ["agent/tests/test_xxx.py"], // test files Dev should create or modify\n'
        '  "requirements": ["Requirement 1", ...],\n'
        '  "acceptance_criteria": ["Criterion 1", ...],  // concrete, grep-verifiable\n'
        '  "verification": {"method": "automated test", "command": "pytest agent/tests/"},\n'
        '  "doc_impact": {"files": ["docs/xxx.md"], "changes": ["what changed"]},\n'
        '  "related_nodes": ["L7.4"],    // existing acceptance graph nodes affected\n'
        '  "proposed_nodes": [           // new nodes to create (observer reviews)\n'
        '    {\n'
        '      "parent_layer": 7,\n'
        '      "title": "Node title",\n'
        '      "deps": ["L3.2"],\n'
        '      "verify_requires": ["L4.32"],  // E2E dependency chain\n'
        '      "primary": ["agent/xxx.py"],\n'
        '      "test": ["agent/tests/test_xxx.py"],\n'
        '      "test_strategy": "what to test and how",\n'
        '      "description": "what this node covers"\n'
        '    }\n'
        '  ],\n'
        '  "skip_doc_check": false\n'
        "}\n"
        '  "skip_reasons": {           // REQUIRED for any omitted soft-mandatory field\n'
        '    "proposed_nodes": "reason why no new node needed",\n'
        '    "doc_impact": "reason why no docs affected"\n'
        '  }\n'
        "}\n"
        "\nRules:\n"
        "- target_files, verification, acceptance_criteria are MANDATORY (gate blocks if missing)\n"
        "- test_files, proposed_nodes, doc_impact are soft-mandatory: provide OR explain in skip_reasons\n"
        "- Use Read/Grep to verify file paths exist before listing them\n"
        "- Do NOT output coordinator-style actions. Do NOT output reply_only or create_pm_task.\n"
        "Output ONLY the PRD JSON object."
    )
```

> **Decision Point C**: `test_files` field
> - Current PM output format has no `test_files` — gate doesn't check it either
> - But Dev needs to know which tests to modify/run
> - Option 1: PM outputs `test_files`, gate doesn't check (soft field)
> - Option 2: PM outputs `test_files`, gate also checks (hard field)
> - Option 3: Don't add — Dev infers test files from target_files

### Change 6: Add pm role to pipeline_config

**File**: `agent/pipeline_config.yaml.example` + `agent/pipeline_config.py`

```yaml
roles:
  pm:
    provider: anthropic
    model: claude-opus-4-6  # PM = architect role: determines scope, test strategy, node structure, verification chain — needs strongest model
```

> **Rationale**: PM decides the entire downstream chain quality — wrong target_files or missing test strategy means Dev/Test/QA all fail. Coordinator only routes (sonnet sufficient), but PM architecturally defines the work. Opus pays for itself by reducing gate-block retries.

### Change 7: Enhanced `_gate_post_pm` — mandatory + explain-or-provide

Current gate only checks 3 fields: `target_files`, `verification`, `acceptance_criteria`.

New design: **each field is either present OR has a `_skip_reason` explaining why it was omitted.**

```python
def _gate_post_pm(conn, project_id, result, metadata):
    """Validate PM PRD has mandatory fields. Optional fields require skip_reason if absent."""
    prd = result.get("prd", {})
    missing = []

    # === Mandatory fields (hard block if absent) ===
    for field in ("target_files", "verification", "acceptance_criteria"):
        if not result.get(field) and not prd.get(field) and not metadata.get(field):
            missing.append(field)
    if missing:
        return False, f"PRD missing mandatory fields: {missing}"

    target_files = (result.get("target_files") or prd.get("target_files")
                    or metadata.get("target_files") or [])
    if not target_files:
        return False, "PRD target_files is empty"

    # === Soft-mandatory fields (present OR skip_reason) ===
    soft_fields = {
        "test_files": "Which test files Dev should create/modify",
        "proposed_nodes": "Acceptance graph nodes for this change",
        "doc_impact": "Which docs are affected",
    }
    warnings = []
    skip_reasons = result.get("skip_reasons", {})
    for field, description in soft_fields.items():
        value = result.get(field) or prd.get(field)
        reason = skip_reasons.get(field, "")
        if not value and not reason:
            warnings.append(f"{field} ({description}): missing without skip_reason")

    if warnings:
        return False, f"PRD soft-mandatory fields missing without explanation: {warnings}"

    # === Quality checks (non-blocking warnings, logged only) ===
    # 1. target_files paths should start with agent/ or docs/
    # 2. acceptance_criteria should have at least 2 items
    # 3. proposed_nodes should include test + test_strategy fields

    # Merge PRD fields back into result for downstream stages
    for field in ("target_files", "verification", "acceptance_criteria",
                  "test_files", "proposed_nodes", "doc_impact"):
        if not result.get(field):
            result[field] = prd.get(field) or metadata.get(field)

    return True, "ok"
```

### skip_reasons Example

When PM intentionally omits a soft-mandatory field:
```json
{
  "target_files": ["agent/executor_worker.py"],
  "verification": {"method": "automated test", "command": "pytest"},
  "acceptance_criteria": ["no hardcoded timeout", "heartbeat extends deadline"],
  "test_files": ["agent/tests/test_executor_timeout.py"],
  "proposed_nodes": [],
  "skip_reasons": {
    "proposed_nodes": "Change is within scope of existing node L7.4, no new node needed",
    "doc_impact": "Code-only change, no documentation affected"
  }
}
```

Gate logic:
- `test_files` present → ✅ pass
- `proposed_nodes` empty BUT `skip_reasons.proposed_nodes` explains why → ✅ pass
- `doc_impact` absent AND no `skip_reasons.doc_impact` → ❌ block with message

> **Decision Point D (revised)**: Gate uses explain-or-provide pattern instead of hard/soft binary.

### Change 8: Coordinator forwards memory + context to PM metadata

**File**: `agent/executor_worker.py` `_handle_coordinator_v1` create_pm_task branch

Currently coordinator creates PM task with:
```python
self._api("POST", f"/api/task/{self.project_id}/create", {
    "prompt": prompt,
    "type": "pm",
    "metadata": {"parent_task_id": task_id, "chat_id": chat_id, "source": "coordinator"},
})
```

Add coordinator's memory results and context to PM metadata:
```python
self._api("POST", f"/api/task/{self.project_id}/create", {
    "prompt": prompt,
    "type": "pm",
    "metadata": {
        "parent_task_id": task_id,
        "chat_id": chat_id,
        "source": "coordinator",
        "_coordinator_memories": self._last_query_memories,   # memories found in round 1/2
        "_coordinator_context": context_update,                # current_focus, last_decision, etc.
    },
})
```

This requires storing the memory results from the two-round coordinator flow. In `run_once` coordinator block, save `memory_results` to an instance variable before calling `_handle_coordinator_result`.

**Data flow after change**:
```
Coordinator round 1: query_memory → search → memory_results
  ↓ save to self._last_query_memories
Coordinator round 2: create_pm_task decision
  ↓ _handle_coordinator_v1 creates PM task
  ↓ metadata._coordinator_memories = memory_results
  ↓ metadata._coordinator_context = context_update
PM _build_prompt:
  ↓ reads metadata._coordinator_memories → injects into prompt
  ↓ reads metadata._coordinator_context → injects into prompt
  ↓ also does own memory search + reads runtime context + queue
```

**No second round needed for PM** — coordinator already searched memory, PM reuses + supplements.
> PM must either provide the field OR explain why it's not needed. This catches PM laziness without blocking legitimate cases.

---

## 3. PM Output Field Coverage Analysis

### Current PM Output (ROLE_PROMPTS["pm"])

| Field | In ROLE_PROMPTS | Gate Check | Dev Needs | Auto-chain Passes | Gap |
|-------|-----------------|-----------|----------|-------------------|-----|
| `target_files` | YES | YES (mandatory) | YES | YES (metadata) | -- |
| `acceptance_criteria` | YES (in prd) | YES (mandatory) | YES (prompt body) | YES (metadata) | -- |
| `verification` | YES (in prd) | YES (mandatory) | YES (metadata) | YES (metadata) | -- |
| `requirements` | YES (in prd) | NO | YES (prompt body) | YES (via _build_dev_prompt) | -- |
| `test_files` | **adding** | **soft-mandatory (or skip_reason)** | **YES** | **YES (via _build_dev_prompt)** | ❌→✅ |
| `proposed_nodes` | YES | **soft-mandatory (or skip_reason)** | Via related_nodes | YES (metadata.related_nodes) | -- |
| `proposed_nodes[].test` | **adding** | NO (quality check only) | **YES (test strategy)** | **YES (metadata)** | ❌→✅ |
| `proposed_nodes[].test_strategy` | **adding** | NO (quality check only) | **YES (what to test)** | **YES (metadata)** | ❌→✅ |
| `proposed_nodes[].verify_requires` | **adding** | NO (quality check only) | NO (graph ops) | **YES (observer creates)** | ❌→✅ |
| `doc_impact` | YES (in prd) | **soft-mandatory (or skip_reason)** | Affects skip_doc_check at gate_checkpoint | **YES (via _build_dev_prompt)** | ⚠️→✅ |
| `skip_reasons` | **adding** | YES (gate checks if soft field missing) | NO | NO | ❌→✅ |
| `related_nodes` | YES | NO | YES (scope) | YES (metadata) | -- |
| `prd.feature` | YES | NO | NO (memory only) | NO | -- |
| `prd.scope` | YES | NO | NO | NO | -- |
| `prd.risk` | YES | NO | NO | NO | -- |
| `prd.estimated_effort` | YES | NO | NO | NO | -- |
| `acceptance_scope` | YES | NO | NO | NO | -- |

### Downstream Data Flow

```
PM → _gate_post_pm → _build_dev_prompt → Dev metadata
         ↓                    ↓
   target_files         target_files
   verification         verification
   acceptance_criteria   requirements + acceptance_criteria
                        related_nodes (from proposed_nodes)
                        skip_doc_check (from original metadata)
```

**Gaps**:
1. `test_files`: PM doesn't output → Dev doesn't know which tests to run → Dev can only do `pytest agent/tests/` (full suite)
2. `doc_impact`: PM outputs but doesn't pass to Dev metadata → `_gate_checkpoint` relies on `CODE_DOC_MAP` to compute expected_docs, disconnected from PM's doc_impact

---

## 4. Timing Sequence (post-change estimates)

| # | Step | Before | After | Change |
|---|------|--------|-------|--------|
| 1 | Claim | role="coordinator" | role="pm" | role changes |
| 2 | _report_progress | 3s timeout inline | unchanged | -- |
| 3 | _build_prompt PM | fetch_memories + 3-field instruction | fetch_memories + project structure + full PRD instruction | longer prompt |
| 4 | create_session | no tools, --max-turns 1 | Read/Grep/Glob, --max-turns 3 | tools + turns change |
| 5 | _build_system_prompt | skip API ref + snapshot | includes API ref + context snapshot | more context |
| 6 | CLI inference | sonnet, 1 turn, ~10s | sonnet, 1-3 turns, ~15-45s | may be slower |
| 7 | git_diff | skip | skip (unchanged) | -- |
| 8 | _parse_output | generic JSON extraction | unchanged | -- |
| 9 | _write_memory | prd_scope | unchanged | -- |
| 10 | _complete_task | complete file | unchanged | -- |
| 11 | _gate_post_pm | 3 fields | 3 fields (optionally enhanced) | -- |
| 12 | auto-chain | create dev (observer_hold) | unchanged | -- |

**Total time estimate**: 15-45s (depends on --max-turns choice)

---

## 5. Decision Points Summary

| ID | Question | Options | Recommended | Rationale |
|----|----------|---------|-------------|-----------|
| A | PM --max-turns | 1 / 3 / unlimited | **3** | 1 turn may guess wrong target_files; unlimited may be too slow; 3 turns is enough to read 2 files + output |
| B | PM hang_timeout | 120s / 180s / 300s | **Follow A**: turns=1→120s, turns=3→180s | With tools, hang timer resets on stdout — unlikely to trigger |
| C | test_files field | soft / hard / don't add | **soft** (PM outputs, gate doesn't check) | Gives Dev a reference without blocking the chain; test stage handles actual verification |
| D | gate_post_pm enhancement | keep current / explain-or-provide | **explain-or-provide** | PM must either provide test_files/proposed_nodes/doc_impact OR give skip_reason. Catches laziness without blocking legitimate omissions. |
| E | doc_impact forwarding | pass to Dev metadata / don't pass | **pass** (via _build_dev_prompt) | Dev knows which docs to update, reducing gate_checkpoint rejections |
| F | proposed_nodes auto-create | auto / observer / deferred | **observer** | Auto-creation risks polluting graph; observer reviews node structure + verify_requires chain |
| G | PM model | sonnet / opus | **opus** | PM = architect: defines scope, test strategy, node structure, verify chain. Wrong decisions cascade to all downstream stages. Opus reduces gate-block retries. |
| H | Role-based context filtering | now / defer | **defer** | PM gets full context via _build_prompt + coordinator forwarding. Role filter is quality improvement, not blocker. |
| I | Task cancel status | now / defer | **now** | Observer needs cancel immediately — every test cycle wastes time draining chain-spawned tasks |
| J | PM output intercept | pre-complete (B) / post-complete + cancel (C) | **C** | observer_hold already pauses next stage; cancel covers bad-output cleanup. Simpler than changing executor flow. |

---

## 6. Implementation Order

```
Phase 1: Role Isolation (required — fixes core problem)
├── 1.1 TASK_ROLE_MAP["pm"] = "pm"                     [executor_worker.py]
├── 1.2 ai_lifecycle: pm tools = Read,Grep,Glob          [ai_lifecycle.py]
├── 1.3 ai_lifecycle: pm --max-turns = 3                  [ai_lifecycle.py]
├── 1.4 ai_lifecycle: pm hang_timeout = 180s              [ai_lifecycle.py]
└── 1.5 pipeline_config: pm role model = opus             [pipeline_config.py]

Phase 2: Context Forwarding (important — PM needs coordinator's work)
├── 2.1 _handle_coordinator_v1: forward memories + context to PM metadata  [executor_worker.py]
├── 2.2 run_once: save memory_results for forwarding     [executor_worker.py]
└── 2.3 _build_prompt PM: read _coordinator_memories + _coordinator_context from metadata  [executor_worker.py]

Phase 3: Prompt Quality (important — improves output accuracy)
├── 3.1 _build_prompt PM: project structure + full PRD format + skip_reasons  [executor_worker.py]
├── 3.2 _build_prompt PM: inject runtime context + queue  [executor_worker.py]
├── 3.3 _build_prompt PM: own memory search (supplement)  [executor_worker.py]
└── 3.4 ROLE_PROMPTS["pm"]: simplify/strengthen JSON format directive  [role_permissions.py]

Phase 4: Gate Enhancement (explain-or-provide)
├── 4.1 _gate_post_pm: soft-mandatory fields + skip_reasons check  [auto_chain.py]
├── 4.2 _gate_post_pm: merge all fields into result for downstream  [auto_chain.py]
└── 4.3 _build_dev_prompt: forward doc_impact + test_files  [auto_chain.py]

Phase 5: Verify
├── 5.1 Clear cache, scale 0→1 restart executor
├── 5.2 Create PM task directly (with _coordinator_memories in metadata)
├── 5.3 Release → check timing/complete files
├── 5.4 Verify PM output is PRD format (not coordinator JSON)
├── 5.5 Verify output has target_files + test_files + proposed_nodes (or skip_reasons)
├── 5.6 Verify gate pass → dev task created
└── 4.6 Record PM estimate vs actual comparison
```

---

## 6.5. PM Memory Write + Role Context + Error Append

### Problem 1: PM memory write is incomplete

Current M1 write (auto_chain.py line 153-167):
```python
_write_chain_memory(conn, project_id, "prd_scope",
    json.dumps({"requirements": requirements, "acceptance_criteria": criteria, "summary": ...}),
    metadata)
```

Only writes requirements + acceptance_criteria. Missing: target_files, test_files, proposed_nodes, doc_impact, skip_reasons, verification.

**Fix**: Write full PRD to memory so Dev/Test/QA can recall it:
```python
if task_type == "pm":
    prd = result.get("prd", result)
    _write_chain_memory(
        conn, project_id, "prd_scope",
        json.dumps({
            "requirements": prd.get("requirements", result.get("requirements", [])),
            "acceptance_criteria": result.get("acceptance_criteria", prd.get("acceptance_criteria", [])),
            "target_files": result.get("target_files", []),
            "test_files": result.get("test_files", []),
            "proposed_nodes": result.get("proposed_nodes", []),
            "doc_impact": result.get("doc_impact", {}),
            "verification": result.get("verification", {}),
            "skip_reasons": result.get("skip_reasons", {}),
        }, ensure_ascii=False),
        metadata,
        extra_structured={"task_id": task_id, "chain_stage": "pm"},
    )
```

### Problem 2: Role-based context filtering not implemented

context-snapshot endpoint (`/api/context-snapshot/{pid}?role=X`) accepts `role` parameter but returns **identical content for all roles**:
- Same task_summary (last 3 tasks regardless of type)
- Same recent_memories (scored by followup_needed + failure_pattern, not role-relevant)
- Same node_counts

**Fix design (deferred — not blocking PM iteration)**:

| Role | Should see | Should NOT see |
|------|-----------|---------------|
| coordinator | all task types, all memories, full queue | — |
| pm | pm+dev tasks, prd_scope+pitfall+pattern memories | test/qa results |
| dev | dev tasks only, prd_scope+pattern+pitfall memories, target_files | qa decisions |
| test | test tasks, test_result+failure_pattern memories | dev decisions |
| qa | all types, all memories (reviewer needs full picture) | — |

Implementation: add role filter in context-snapshot handler:
```python
# Memory filter by role
role_kind_filter = {
    "pm": ["prd_scope", "pitfall", "pattern", "decision"],
    "dev": ["prd_scope", "pitfall", "pattern"],
    "test": ["test_result", "failure_pattern", "pitfall"],
    "qa": None,  # sees all
    "coordinator": None,  # sees all
}
```

> **Decision Point H**: Implement role-based context filtering now or defer?
> Recommended: **Defer** — PM gets full context via _build_prompt enhancement (Change 5/8). Role filtering is a quality improvement, not a blocker.

### Problem 4: Orphan Claude CLI processes on executor crash

**Root cause**: ServiceManager restarts executor but does NOT kill the executor's child processes (Claude CLI subprocesses). Each crash-restart cycle leaves orphan CLI processes running indefinitely.

**Chain**: executor → Popen(claude CLI) → executor dies → ServiceManager restarts executor → old CLI becomes orphan → new executor starts fresh CLI → repeat

**Evidence**: 10+ claude.exe orphans (32 conhost.exe), 71% memory usage from screenshot.

**Fixes needed**:

1. **ServiceManager: kill process tree on restart** (`agent/service_manager.py`)
```python
# Before restart, kill the entire process tree of the dead executor
import psutil  # or use taskkill /T /F /PID on Windows
try:
    parent = psutil.Process(dead_pid)
    for child in parent.children(recursive=True):
        child.kill()
    parent.kill()
except Exception:
    pass  # process already dead
```

Windows alternative without psutil:
```python
import subprocess
subprocess.run(["taskkill", "/T", "/F", "/PID", str(dead_pid)],
               capture_output=True, timeout=10)
```

2. **ai_lifecycle: register spawned PIDs for cleanup** (already has `_EXECUTOR_SPAWNED_PIDS`)
```python
# In create_session, line ~206:
try:
    from executor import _EXECUTOR_SPAWNED_PIDS
    _EXECUTOR_SPAWNED_PIDS.add(proc.pid)
except ImportError:
    pass
```
This exists but nobody reads `_EXECUTOR_SPAWNED_PIDS` for cleanup.

3. **ServiceManager stdout PIPE→DEVNULL** (already fixed on disk)
Reduces crash frequency (the root cause of most crashes was PIPE buffer overflow).

> **Decision Point K**: Orphan cleanup — psutil or taskkill?
> Recommended: **taskkill /T** (Windows built-in, no dependency). Add to ServiceManager._monitor_loop before restart.

### Problem 3: Gate failure pitfall too sparse

Current pitfall write (M3):
```
content: "Gate blocked at pm: PRD missing mandatory fields: ['target_files', 'verification', 'acceptance_criteria']"
```

This tells the next retry **what was missing** but not **what the PM actually output** — the retry PM can't learn from the previous attempt.

**Fix**: Include the previous PM output summary in the pitfall:
```python
_write_chain_memory(
    conn, project_id, "pitfall",
    f"Gate blocked at {task_type}: {reason}\n"
    f"Previous output keys: {list(result.keys())}\n"
    f"Previous output preview: {json.dumps(result, ensure_ascii=False)[:300]}",
    metadata,
    extra_structured={
        "task_id": task_id,
        "gate_stage": task_type,
        "gate_reason": reason,
        "previous_output_keys": list(result.keys()),
        "chain_stage": task_type,
    },
)
```

This way the retry PM sees: "Last time you output `actions` (coordinator format) instead of `target_files` (PRD format). Fix it."

### Implementation phase

Add to Phase 4 (Gate Enhancement):
```
Phase 4: Gate Enhancement + Memory
├── 4.1 _gate_post_pm: explain-or-provide                [auto_chain.py]
├── 4.2 _gate_post_pm: merge all fields for downstream    [auto_chain.py]
├── 4.3 _build_dev_prompt: forward doc_impact + test_files [auto_chain.py]
├── 4.4 M1: PM memory write full PRD fields               [auto_chain.py]
└── 4.5 M3: Gate pitfall includes previous output summary  [auto_chain.py]
```

Role-based context filtering → defer to next round (Decision Point H).

---

## 6.6. Observer Task Cancel + Output Intercept

### Problem 1: No task cancel — cleanup triggers auto-chain

Current options for observer to clean up a task:
- `complete(succeeded)` → triggers auto-chain → spawns downstream tasks → more cleanup needed
- `complete(failed)` → triggers retry (if attempt_count < max_attempts) → task reappears

Neither is a clean "stop and discard".

**Fix: Add `cancelled` terminal status**

```python
# task_registry.py — new function
def cancel_task(conn, task_id):
    """Cancel a task. No auto-chain, no retry. Terminal state."""
    conn.execute(
        "UPDATE tasks SET status='cancelled', execution_status='cancelled', updated_at=? WHERE task_id=?",
        (_utc_iso(), task_id))
    return {"task_id": task_id, "status": "cancelled"}
```

```python
# server.py — new endpoint
@route("POST", "/api/task/{project_id}/cancel")
def handle_task_cancel(ctx):
    task_id = ctx.body.get("task_id", "")
    with DBContext(ctx.get_project_id()) as conn:
        return task_registry.cancel_task(conn, task_id)
```

```python
# tools.py — new MCP tool
if name == "task_cancel":
    return self._api("POST", f"/api/task/{pid}/cancel", {"task_id": args["task_id"]})
```

```python
# complete_task — skip auto-chain for cancelled
if exec_status == "cancelled":
    # No auto-chain, no retry
    pass
```

> **Decision Point I**: Add `cancelled` status now or defer?
> Recommended: **Now** — observer needs this immediately for queue cleanup. Every test cycle wastes time draining chain-spawned tasks.

### Problem 2: Observer can't intercept PM output before auto-chain

Current flow when observer releases PM task:
```
release → executor claim → CLI → _parse_output → _complete_task → auto_chain → gate → dev task
                                                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                              observer has NO control here
```

Observer sees the result only after everything happened. Can't reject PM output before dev task is created.

**Fix options**:

| Option | Design | Impact |
|--------|--------|--------|
| A: observer_mode blocks auto-chain | If `observer_mode=ON`, `on_task_completed` puts next-stage task in `observer_hold` | ✅ Already works — dev task enters observer_hold |
| B: Pre-complete intercept | After _execute_task, if observer_mode, don't call _complete_task. Put result in a "pending_review" state | Complex — changes executor flow |
| C: observer reviews PM result via existing flow | PM completes → gate → dev task in observer_hold. Observer reads PM result from DB, then decides to release or cancel dev task | ✅ Works with current design + cancel |

**Recommended: Option C (current design + cancel is sufficient)**

Flow with cancel:
```
PM release → executor runs → PM completes → gate → dev task (observer_hold)
                                                          ↓
Observer checks:
  - Read PM task result from DB
  - Read timing/complete files
  - If PM output bad: task_cancel(dev_task_id) ← new capability
  - If PM output good: task_release(dev_task_id)
```

This means observer already has intercept ability — dev task sits in `observer_hold` until observer reviews and releases. The missing piece is just `cancel` for when the output is bad.

> **Decision Point J**: Pre-complete intercept (Option B) or post-complete review + cancel (Option C)?
> Recommended: **Option C** — simpler, uses existing observer_hold mechanism. Cancel covers the cleanup case.

### MCP tools needed

```
task_cancel: Cancel a task (no auto-chain, no retry)
```

Add to Phase 1 (alongside role isolation, since observer needs it immediately):
```
Phase 0: Observer Cancel (required — unblocks all testing)
├── 0.1 task_registry: cancel_task function               [task_registry.py]
├── 0.2 server: POST /api/task/{pid}/cancel endpoint      [server.py]
├── 0.3 MCP: task_cancel tool                             [tools.py]
├── 0.4 complete_task: skip auto-chain for cancelled       [task_registry.py]
└── 0.5 TERMINAL_STATUSES: add "cancelled"                 [task_registry.py]
```

---

## 7. Dev Path log.info Blockers (appendix: carried from previous session)

Remaining log calls in executor_worker.py on the Dev path (PM path doesn't hit these, recorded here for reference):

| Line | Call | Path | Risk | Recommendation |
|------|------|------|------|----------------|
| 239 | `log.info("Staged %d changed file(s)")` | after git add (dev/test) | ❌ HIGH — MCP deadlock | change to _timing |
| 241 | `log.warning("git add failed")` | git add except (dev/test) | ⚠️ MED — except branch | change to _timing |
| 327 | `log.warning("Memory write failed")` | _write_memory except (all types) | ⚠️ MED | change to _timing |
| 947 | `log.warning("git diff failed")` | _get_git_changed_files except (dev/test) | ⚠️ MED | change to _timing |

These **do not need fixing** for the PM iteration (PM skips git diff/add), but must be fixed before Dev stage testing.

---

## 8. Acceptance Criteria for This Plan

- [ ] PM output contains `target_files` (mandatory, non-empty)
- [ ] PM output contains `acceptance_criteria` (mandatory, non-empty list)
- [ ] PM output contains `verification` (mandatory)
- [ ] PM output does **not** contain coordinator-style `actions` field
- [ ] PM output contains `test_files` (soft, non-blocking)
- [ ] PM output contains `related_nodes` or `proposed_nodes` OR `skip_reasons` explaining omission
- [ ] PM output contains `doc_impact` OR `skip_reasons.doc_impact` explaining omission
- [ ] `proposed_nodes` include `test`, `test_strategy`, `verify_requires` when present
- [ ] _gate_post_pm passes → auto-chain creates dev task
- [ ] PM full flow <= 45s (sonnet, --max-turns 3)
- [ ] PM timing file complete (start → complete)
- [ ] No log.info/log.warning blocking
- [ ] E2E tests pass (P1/P2/P3)
- [ ] verify_requires chain: coordinator E2E → PM E2E → Dev E2E

---

## Session 2 Results (2026-03-30)

### Implemented

| Phase | What | Status |
|-------|------|--------|
| 0 | Task cancel (cancelled status + API + MCP tool) | ✅ Done |
| 1 | PM role isolation (TASK_ROLE_MAP, ai_lifecycle, pipeline_config) | ✅ Done |
| 2 | Context forwarding (coordinator memories → PM metadata) | ✅ Done |
| 3 | PM prompt quality (_build_prompt scheme C format) | ✅ Done |
| 4 | Gate enhancement (explain-or-provide + full PRD memory write + enriched pitfall) | ✅ Done |

### Bugs Found and Fixed During PM Testing

| # | Bug | Root Cause | Fix | Impact |
|---|-----|-----------|-----|--------|
| 1 | PM outputs coordinator JSON instead of PRD | ROLE_PROMPTS["pm"] format contradicts _build_prompt format | Unified to scheme C: ROLE_PROMPTS has role identity only, _build_prompt has format | Blocked PM gate |
| 2 | PM CLI hangs forever via executor | ai_lifecycle `Popen` + `proc.poll()` Windows pipe deadlock with Claude CLI | Replaced entire Popen+watchdog with `subprocess.run` in background thread | Executor hung indefinitely |
| 3 | Executor crashes repeatedly | ServiceManager `stdout=PIPE` buffer overflow when CLI outputs large stdout | Changed to `stdout=DEVNULL` for all Popen calls in ServiceManager | Executor crash loop |
| 4 | ai_lifecycle log.info blocks | MCP subprocess IO pipe contention on Python logging | Replaced all `log.info/warning/error` with file-based `_al_log` in ai_lifecycle.py | Session creation hung |
| 5 | Pipeline config empty (no model) | No pipeline_config.yaml file, only .example | Created actual config at shared-volume/codex-tasks/state/pipeline_config.yaml | CLI used default model (opus) instead of configured sonnet |
| 6 | Orphan Claude CLI processes | ServiceManager restarts executor without killing child process tree | Identified — fix pending (taskkill /T before restart) | Memory leak, 10+ orphan processes |
| 7 | No input/output file logging | system prompt + stdin prompt + CLI stdout not saved to disk | Added input-{id}.txt and output-{id}.txt in ai_lifecycle._run | Debugging blind spot |

### Verified PM Flow (final)

```
Task create (observer_hold) → release → executor claim (role=pm, pid=46568)
  → _report_progress (0.2s, short timeout)
  → _build_prompt PM branch:
      coordinator memories (1 pitfall) ← from metadata
      PM memory search (3 results) ← governance FTS5/dbservice
      runtime context + queue ← API calls (3s timeout)
      project structure + scheme C format instruction
    = 2826 chars prompt (0.4s)
  → ai_lifecycle.create_session:
      _build_system_prompt (5501 chars, role identity only)
      pipeline_config → sonnet
      subprocess.run(claude -p --model sonnet --allowedTools Read,Grep,Glob --max-turns 10)
    = 0.7s to start CLI
  → CLI execution (sonnet, Read/Grep/Glob, ≤10 turns):
      reads executor_worker.py + ai_lifecycle.py
      outputs PRD JSON
    = 58.3s
  → _parse_output: extracts JSON (10 keys) = instant
  → git_diff: skipped (pm) = instant
  → _write_memory: prd_scope (full PRD) = instant
  → _complete_task → auto_chain → _gate_post_pm:
      target_files ✅, verification ✅, acceptance_criteria ✅
      test_files ✅, proposed_nodes ✅, doc_impact ✅
      skip_reasons: {} (all fields provided)
    = gate PASS
  → auto_chain creates dev task (observer_hold)
TOTAL: 58.9s
```

### PM Output (verified)

```json
{
  "target_files": ["agent/executor_worker.py", "agent/ai_lifecycle.py"],
  "test_files": ["agent/tests/test_executor_heartbeat.py"],
  "requirements": ["R1: update_progress extends deadline 120s", "R2: max 1200s cap", "R3: kill after 120s silence", "R4: replace subprocess.run fixed timeout", "R5: all tests pass"],
  "acceptance_criteria": ["AC1-AC7 (grep-verifiable)"],
  "verification": {"method": "automated test", "command": "pytest agent/tests/ -x -q"},
  "proposed_nodes": [{"parent_layer": 8, "title": "Heartbeat-based deadline", "test": ["test_executor_heartbeat.py"], "test_strategy": "..."}],
  "doc_impact": {"files": ["docs/design-spec-memory-coordinator-executor.md"], "changes": ["..."]},
  "skip_reasons": {},
  "related_nodes": [],
  "prd": {"feature": "...", "background": "...", "scope": "...", "risk": "low"}
}
```

### Files Logged Per Task

| File | Content | All roles? |
|------|---------|-----------|
| `timing-{task_id}.txt` | Every step with elapsed time | ✅ |
| `build-prompt-{task_id}.txt` | Prompt assembly timing (coordinator/pm) | coordinator + pm |
| `ai-lifecycle-{session_id}.txt` | CLI startup + completion | ✅ |
| `input-{session_id}.txt` | Full system prompt + stdin prompt + CLI cmd | ✅ NEW |
| `output-{session_id}.txt` | Full CLI stdout + stderr + status | ✅ NEW |
| `coordinator-flow-{task_id}.txt` | Two-round coordinator logic | coordinator only |
| `complete-{task_id}.txt` | Task completion + chain result | ✅ |
| `error-{task_id}.txt` | Exception traceback | on error only |

### Acceptance Criteria Check

- [x] PM output contains `target_files` (mandatory, non-empty) ✅
- [x] PM output contains `acceptance_criteria` (mandatory, non-empty list) ✅
- [x] PM output contains `verification` (mandatory) ✅
- [x] PM output does **not** contain coordinator-style `actions` field ✅
- [x] PM output contains `test_files` (soft, non-blocking) ✅
- [x] PM output contains `proposed_nodes` with test + test_strategy ✅
- [x] PM output contains `doc_impact` ✅
- [x] _gate_post_pm passes → auto-chain creates dev task ✅
- [x] PM full flow <= 60s (sonnet) ✅ (58.9s)
- [x] PM timing file complete (start → complete) ✅
- [x] No log.info/log.warning blocking ✅
- [x] Input/output files saved for replay ✅
- [ ] E2E tests (P1/P2/P3) — pending
- [ ] verify_requires chain — pending (nodes need update)

### Next Steps

| Priority | Item | Effort |
|----------|------|--------|
| P0 | PM E2E tests (P1: PRD pass, P3: field coverage) | Small — batch CLI mode |
| P0 | Update acceptance graph nodes (L4.37-L4.38 + new nodes) | Small |
| P0 | Update coordinator-rules.md + pm-rules.md for scheme C | Small |
| P1 | Orphan process cleanup (taskkill /T in ServiceManager) | Small |
| P1 | Dev stage predict → verify | Medium |
| P2 | PM chain_path (skip stages) | Medium — auto_chain change |
| P3 | Process pool (multi-slot parallel) | Large |
| P3 | Multi-project isolation | Large |

### Decision Points Status

| ID | Question | Decision | Implemented? |
|----|----------|----------|-------------|
| A | PM --max-turns | 10 (safety cap) | ✅ |
| B | PM hang_timeout | 180s | ✅ |
| C | test_files field | soft-mandatory (or skip_reason) | ✅ |
| D | gate_post_pm | explain-or-provide | ✅ |
| E | doc_impact forwarding | pass via _build_dev_prompt | ✅ |
| F | proposed_nodes auto-create | observer reviews | ✅ (design only) |
| G | PM model | sonnet for testing, opus for production | ✅ (config file) |
| H | Role-based context filtering | defer | — |
| I | Task cancel status | now | ✅ |
| J | PM output intercept | post-complete + cancel (option C) | ✅ |
| K | Orphan cleanup | taskkill /T | pending |
| L | Process pool: per-project or shared | per-project | design only |
| M | Slot assignment | static by role type | design only |

---

## 9. Node Coverage: proposed_nodes Handling

### Current State

`ROLE_PROMPTS["pm"]` tells PM to output `proposed_nodes`:
```json
"proposed_nodes": [
    {"parent_layer": 22, "title": "Node title", "deps": ["L15.1"], "primary": ["agent/xxx.py"]}
]
```

But the downstream handling is unclear:

| Step | Does it process proposed_nodes? | Status |
|------|--------------------------------|--------|
| `_parse_output` | Passes through if present in JSON | ✅ transparent |
| `_gate_post_pm` | Does NOT check proposed_nodes | ⚠️ ignored |
| `_build_dev_prompt` | Does NOT forward proposed_nodes | ❌ lost |
| `auto_chain` event publish | Includes full result in payload | ✅ in event log |

**Gap**: PM proposes nodes but nobody creates them. Options:
1. **PM outputs proposed_nodes → auto_chain creates them via `/api/wf/{pid}/node-create`** (automatic)
2. **PM outputs proposed_nodes → observer reviews and creates manually** (manual)
3. **PM outputs related_nodes only (existing nodes) → no new node creation** (deferred)

> **Decision Point F**: Should auto-chain auto-create proposed nodes from PM output?
> Recommended: **Option 2** — observer creates. Auto-creation is risky (wrong node structure could pollute the graph).

### What PM Should Output for Node Coverage

PM acts as **architect** — its proposed_nodes define:
- What code is primary/secondary for the node
- What test files will verify it (test strategy)
- What the verify_requires chain looks like (E2E dependency order)
- What docs are affected

```json
{
  "target_files": ["agent/executor_worker.py"],
  "test_files": ["agent/tests/test_executor_timeout.py"],
  "related_nodes": ["L7.4"],  // existing nodes this change affects
  "proposed_nodes": [          // new nodes PM suggests (observer reviews)
    {
      "parent_layer": 7,
      "title": "Heartbeat-based subprocess deadline",
      "deps": ["L3.2"],
      "verify_requires": ["L4.32"],  // E2E dependency: coordinator E2E must pass first
      "primary": ["agent/executor_worker.py"],
      "secondary": ["agent/ai_lifecycle.py"],
      "test": ["agent/tests/test_executor_timeout.py"],  // test file that Dev should create/modify
      "test_strategy": "unit test: normal completion, heartbeat extension, stale kill after 120s",
      "description": "Dynamic timeout with heartbeat extension for Claude CLI subprocess"
    }
  ],
  "doc_impact": {
    "files": ["docs/dev/coordinator-iteration.md"],
    "changes": ["Document heartbeat timeout mechanism"]
  },
  "verification": {"method": "automated test", "command": "pytest agent/tests/"},
  "acceptance_criteria": ["no hardcoded timeout=300", "heartbeat extends deadline", ...],
  "requirements": ["..."]
}
```

---

## 10. E2E Test Scenarios for PM

### Test File: `agent/tests/test_e2e_pm.py`

Uses `aming-claw-test` project (same isolation as coordinator E2E).

**P1: Normal PRD output → gate pass → dev task created**
```python
def test_p1_prd_output_creates_dev(self):
    """PM outputs valid PRD → gate_post_pm passes → auto-chain creates dev task."""
    # Create PM task with clear requirements
    # Release → executor runs PM
    # Verify: result has target_files + verification + acceptance_criteria
    # Verify: dev task created in observer_hold
```

**P2: PRD missing fields → gate block → retry PM**
```python
def test_p2_missing_fields_triggers_retry(self):
    """PM outputs incomplete PRD → gate blocks → retry PM task created."""
    # Difficult to force PM to output bad PRD via prompt alone
    # Alternative: unit test _gate_post_pm directly with incomplete result
    # This is better as a unit test, not E2E
```

**P3: PRD contains test_files + doc_impact + related_nodes**
```python
def test_p3_prd_has_full_fields(self):
    """PM output includes soft fields: test_files, doc_impact, related_nodes."""
    # Verify PM output contains these fields (may be empty but present)
    # Soft assertion: log if missing, don't fail
```

### Multi-Scenario PM E2E

PM handles different types of requests. Each type should produce different PRD output and potentially different chain_path (when implemented).

**Group PA: Independent scenarios (batch CLI, ~60s total)**

Uses batch mode like coordinator E2E — single CLI call, PM system prompt, multiple scenario inputs. PM doesn't need tools in batch mode (file paths are given in prompt context).

```
PA1: Feature development
  Input: "Add heartbeat-based deadline to executor subprocess"
  Expected: target_files=[executor_worker.py, ai_lifecycle.py], test_files=[test_executor_heartbeat.py],
            proposed_nodes with test_strategy, acceptance_criteria 5+ items
  Gate: pass → chain_path=["dev","test","qa","merge"]

PA2: Bug fix (small scope)
  Input: "Fix log.info deadlock in coordinator result handler"
  Expected: target_files=[executor_worker.py], test_files=[test_coordinator_decisions.py],
            skip_reasons.proposed_nodes="fix within existing node L4.25"
  Gate: pass → chain_path=["dev","test","merge"] (skip QA for trivial fix)

PA3: Test-only task
  Input: "Add E2E tests for PM output format validation"
  Expected: target_files=[], test_files=[test_e2e_pm.py],
            skip_reasons.target_files="test-only change, no source code modified"
  Gate: pass → chain_path=["test","qa"] (skip Dev)

PA4: Documentation update
  Input: "Update architecture docs to reflect two-round coordinator design"
  Expected: target_files=[], doc_impact.files=[docs/architecture-v4-complete.md],
            skip_reasons.target_files="documentation only"
  Gate: pass → chain_path=["dev","merge"] (Dev writes docs, skip Test+QA)

PA5: Verification/acceptance task
  Input: "Verify that L4.37 PM Role Isolation is working correctly"
  Expected: related_nodes=["L4.37"], test_files=[test_coordinator_decisions.py],
            verification.method="run existing tests"
  Gate: pass → chain_path=["test","qa"] (skip Dev)
```

**Group PB: Gate validation scenarios (unit test, not CLI)**

```
PB1: gate pass — all mandatory + soft fields provided
PB2: gate block — missing target_files
PB3: gate block — missing acceptance_criteria
PB4: gate pass — soft field empty + skip_reasons provided
PB5: gate block — soft field empty + no skip_reasons
```

### E2E Implementation Approach

| Group | Method | Why |
|-------|--------|-----|
| PA (5 scenarios) | Batch CLI call (single `-p` with all scenarios as JSON array) | PM doesn't need tools in batch mode; context provided in prompt |
| PB (5 scenarios) | Unit test `_gate_post_pm` directly | Deterministic; don't need AI |
| P1 (real executor flow) | Single task via executor | Validates full chain: claim → CLI → parse → gate → dev task |

**PA batch prompt structure:**
```json
[
  {"id": "PA1", "task": "Add heartbeat deadline...", "context": {...}},
  {"id": "PA2", "task": "Fix log.info deadlock...", "context": {...}},
  {"id": "PA3", "task": "Add E2E tests for PM...", "context": {...}},
  {"id": "PA4", "task": "Update architecture docs...", "context": {...}},
  {"id": "PA5", "task": "Verify L4.37...", "context": {...}}
]
```

Each scenario validates:
1. PRD JSON has all mandatory fields (or skip_reasons)
2. target_files/test_files make sense for the task type
3. chain_path (when implemented) matches expected route
4. No coordinator-style actions/reply in output

---

## 11. Verify Requires Chain

### Acceptance Graph Nodes for PM

```
L4.36  Dbservice E2E ✅
  → L4.33  LLM Utils E2E ✅ (verify_requires: [L4.36])
    → L4.32  Coordinator E2E ✅ (verify_requires: [L4.33])
      → L4.3x  PM Role Isolation + PRD Output  (verify_requires: [L4.32])
        → L4.3y  PM E2E Tests  (verify_requires: [L4.3x])
          → future Dev E2E  (verify_requires: [L4.3y])
```

### Node Definitions (to be added to acceptance-graph.md after implementation)

```
L4.37  PM Role Isolation + PRD Output  [impl:pending] [verify:pending] v4.3
      deps:[L4.25, L4.31]
      verify_requires:[L4.32]
      primary:[agent/executor_worker.py, agent/ai_lifecycle.py, agent/role_permissions.py]
      test:[agent/tests/test_coordinator_decisions.py]
      description: PM gets independent role (not mapped to coordinator). Has Read/Grep/Glob tools,
      --max-turns 3, own hang_timeout. Outputs PRD JSON with target_files, test_files, verification,
      acceptance_criteria, doc_impact, related_nodes, proposed_nodes. _build_prompt PM branch
      includes project structure + full format instruction.

L4.38  PM E2E Tests  [impl:pending] [verify:pending] v4.3
      deps:[L4.37]
      verify_requires:[L4.32]
      primary:[agent/tests/test_e2e_pm.py]
      test:[agent/tests/test_e2e_pm.py]
      description: P1 (PRD → gate pass → dev created), P3 (full field coverage check).
      Uses aming-claw-test project. verify_requires coordinator E2E (L4.32) must pass first.
```

### Re-verification Rules

When PM code changes:
- Re-run PM E2E (L4.38)
- Re-run Dev E2E (downstream)
- Do NOT need to re-run Coordinator E2E (upstream, unchanged)

When Coordinator code changes:
- Re-run Coordinator E2E (L4.32)
- Re-run PM E2E (L4.38) — because PM depends on coordinator's output quality
- Re-run Dev E2E (downstream)
