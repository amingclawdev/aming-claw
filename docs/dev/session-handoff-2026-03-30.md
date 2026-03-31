# Session Handoff — 2026-03-30

## What Was Done (2 sessions, ~12 hours)

### Coordinator (complete ✅)
- Two-round flow: Round 1 outputs `query_memory` or decision → executor searches → Round 2 with memories
- No tools (--max-turns 1), context pre-injected by executor._build_prompt
- Gate: G1-G7 validation, only reply_only/create_pm_task allowed
- Conversation history via session_context (context/log API)
- 21s per decision (sonnet)

### PM (complete ✅)
- Independent role (not mapped to coordinator), Read/Grep/Glob tools, --max-turns 10
- Output: scheme C flat JSON (target_files, test_files, requirements, acceptance_criteria, verification, proposed_nodes, doc_impact, skip_reasons, prd metadata)
- Gate: explain-or-provide (mandatory + soft-mandatory with skip_reasons)
- Context forwarded from coordinator (memories + context_update in metadata)
- 58s sonnet / 71s opus per PRD

### Infrastructure fixes
- `subprocess.run` replaces Popen+watchdog (Windows pipe deadlock)
- `ServiceManager stdout=DEVNULL` (buffer overflow crash)
- All `log.info` in executor_worker + ai_lifecycle replaced with file-based logging (MCP IO deadlock)
- `pipeline_config.yaml` created with per-role model config
- Task cancel API (cancelled status, no auto-chain/retry)
- dbservice search/write fix (endpoint + field mapping)
- Input/output file logging for all CLI sessions

## Post-Handoff Update (2026-03-30, Codex provider activation)

### What changed
- `ai_lifecycle.py` now routes by `pipeline_config` provider:
  - `anthropic` → `claude`
  - `openai` → `codex exec`
- Runtime `pipeline_config.yaml` switched to OpenAI/Codex defaults:
  - coordinator/tester/qa/utility → `gpt-5.4-mini`
  - pm/dev → `gpt-5.4-codex`
- Codex CLI was registered with the current `aming-claw` MCP server so OpenAI/Codex runs can access governance tools through Codex
- Added targeted unit test: `agent/tests/test_ai_lifecycle_provider_routing.py`

### Verification status
- Targeted provider-routing unit test passes (`3 passed`)
- `py_compile` passes for `ai_lifecycle.py` and `pipeline_config.py`
- Broader workflow/unit suite is currently blocked by existing Python runtime compatibility errors (`dict | None` style annotations on the active interpreter), not by the provider-routing patch itself

### Important ingress note
- External governance entrypoint is `http://localhost:40000` via nginx
- Internal container port `40006` is proxied by nginx and should not be treated as the primary observer entrypoint from host-side tooling

## Critical Pitfalls (MUST READ)

1. **log.info() blocks in MCP subprocess** — Python logging IO pipe deadlock. ALL logging in executor_worker.py and ai_lifecycle.py MUST use file-based `_timing()` / `_al_log()` / `_hv_log()`, NEVER `log.info/warning/error`
2. **Popen + proc.poll() deadlocks on Windows** — Claude CLI child processes keep pipes open. Use `subprocess.run(input=, capture_output=True, timeout=)` instead
3. **ServiceManager stdout=PIPE crashes executor** — buffer overflow when CLI outputs large stdout. Use `DEVNULL`
4. **MCP caches Python modules** — code changes to executor_worker.py, ai_lifecycle.py, service_manager.py require session restart to take effect. pycache clear alone is NOT sufficient
5. **Orphan claude.exe processes** — ServiceManager restarts executor without killing child process tree. Manual `taskkill /F /IM claude.exe` needed between test runs. Fix pending (taskkill /T in ServiceManager)
6. **ROLE_PROMPTS format vs _build_prompt format** — must be consistent. Format spec goes in `_build_prompt` ONLY. ROLE_PROMPTS has role identity/rules only
7. **pipeline_config.yaml** — must exist at `shared-volume/codex-tasks/state/pipeline_config.yaml` or model defaults to empty (CLI uses default model). Role name is `tester` not `test`
8. **Codex MCP registration is not automatic from repo `.mcp.json`** — `codex exec` only sees MCP servers registered in Codex CLI config. Verify with `codex mcp list`
9. **Host-side observer/API calls should use nginx on port 40000** — `/api/*` is proxied there; direct `40006` access is container-internal and may appear intermittently unavailable from the host

## Working Method: Predict → Verify

Every change follows this cycle:
1. **Predict**: write expected behavior for each step (timing, output format, gate result, etc.)
2. **Execute**: run the task via observer (task_create → observer_hold → release)
3. **Verify**: compare timing files + flow logs + governance logs + output files against prediction
4. **If mismatch**: diagnose root cause, fix, re-run. Record bug + pitfall to memory

## File Logging System

Every executor task produces these files in `shared-volume/codex-tasks/logs/`:

| File | Content |
|------|---------|
| `timing-{task_id}.txt` | Step-by-step elapsed time |
| `build-prompt-{task_id}.txt` | Prompt assembly timing (coordinator/pm) |
| `ai-lifecycle-{session_id}.txt` | CLI startup to completion |
| `input-{session_id}.txt` | Full system prompt + stdin prompt + CLI cmd |
| `output-{session_id}.txt` | Full CLI stdout + stderr + status |
| `coordinator-flow-{task_id}.txt` | Two-round coordinator logic |
| `complete-{task_id}.txt` | Task completion + auto-chain result |
| `error-{task_id}.txt` | Exception traceback (on error only) |

## Document Structure

```
docs/
├── coordinator-rules.md          — Coordinator role spec (L4.25)
├── pm-rules.md                   — PM role spec (L4.26)
├── observer-rules.md             — Observer operation rules (L4.23)
├── observer-feature-guide.md     — Observer feature design (L4.21-L4.24)
├── aming-claw-acceptance-graph.md — All nodes (L4.20-L4.40)
└── dev/                          — Development iteration logs (NOT node-bound)
    ├── coordinator-iteration.md  — Coordinator design decisions + session log
    ├── coordinator-impl-plan-v1.md — Batch 1-7 implementation plan
    ├── pm-iteration.md           — PM design decisions + session 2 results
    ├── executor-evolution.md     — Process pool + chain_path proposals
    ├── optimization-proposals-v1.md — P1-P5 optimization proposals
    └── session-handoff-2026-03-30.md — This file
```

## Claude Memory Files

```
.claude/projects/.../memory/
├── MEMORY.md                          — Index
├── project_coordinator_gaps.md        — Coordinator flow gaps + fixes
├── project_memory_i18n_keyword.md     — AI keyword extraction + memory English normalization
├── project_coordinator_context_gap.md — Conversation context design
├── project_test_isolation.md          — E2E test isolation (aming-claw-test project)
├── project_test_dependency_chain.md   — verify_requires E2E chain
├── feedback_governance_db_is_event_log.md — DB records events not states
└── (others)
```

## Test Counts

- Unit tests: 391 passed
- E2E tests: 13 passed (coordinator S1-S5 + C1-C5 + D1-D2 + E1-E3 + PM PA1-PA5)
- Total: 404

## What's Left

### Immediate (Dev stage)

1. **Dev stage predict → verify** — create dev task from PM's PRD, verify dev output, gate checkpoint
2. **Fix remaining log.info on dev path** (executor_worker.py lines 239/241/327/947)
3. **Dev needs worktree isolation** — dev writes code, needs separate git branch/worktree
4. **Python runtime compatibility fix** — unblock imports/tests on current interpreter (`dict | None` style annotations and similar syntax issues)

### Short-term

5. **Orphan process cleanup** — taskkill /T in ServiceManager before restart
6. **PM chain_path** — PM outputs which stages to run (skip test for doc-only, etc.)
7. **Test/QA/Merge stages** — predict → verify each one

### Medium-term

8. **Process pool** — multi-slot parallel execution per project
9. **Multi-project isolation** — separate pools per project
10. **Session context** — populate session_context table for chat history

## How to Start New Session

```
Prompt for new session:

"Continue developing the aming-claw governance system. Read these files first:
1. docs/dev/session-handoff-2026-03-30.md — what was done + pitfalls
2. docs/dev/pm-iteration.md — PM design decisions + session 2 results
3. docs/dev/executor-evolution.md — process pool + chain_path proposals

Current state:
- Coordinator: stable, 21s, two-round query_memory flow ✅
- PM: stable, 71s opus, scheme C PRD output, gate pass ✅
- Dev: NOT YET TESTED — next step
- 404 tests passing (391 unit + 13 E2E)

Next task: Dev stage predict → verify. Create dev task from the PM PRD output
(task-1774841916-9d378e in observer_hold), verify dev executes correctly.

Key pitfalls: log.info blocks in MCP subprocess (use file logging),
Popen deadlocks on Windows (use subprocess.run), MCP caches modules
(restart session after code changes to executor_worker/ai_lifecycle/service_manager).

Work method: predict expected behavior → execute → compare logs → fix mismatches.
All logging goes to shared-volume/codex-tasks/logs/ files."
```
