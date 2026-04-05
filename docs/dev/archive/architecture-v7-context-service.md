---
status: archived
superseded_by: design-spec-memory-coordinator-executor.md
archived_date: 2026-04-05
historical_value: "Context service architecture with memory integration"
do_not_use_for: "architecture decisions"
---

# Aming Claw Architecture v7 — Context Service + Observer SOP

> v6 → v7 core change: Removed CLI `-p` direct context passing, replaced with Context Service. All AI session input/output goes through structured storage, enabling full-chain auditing, replay, and observability.

## 1. Problem Analysis

### Problems with the Legacy Architecture (Removed)

> Note: The legacy Telegram bot system (bot_commands, coordinator, executor and 20 other modules) has been completely removed.
> Context management is now entirely handled by the governance API (`/api/context/*`).

The following problems existed with the legacy CLI `-p` mode (now resolved):

| Problem | Impact |
|---------|--------|
| Overly long prompts truncated by shell | Information loss on complex tasks |
| All context stuffed into one string | AI easily ignores key info (e.g., target_files) |
| Images cannot be passed | Multimodal tasks impossible |
| Process not auditable | Only final stdout, intermediate reasoning lost |
| Observer cannot see intermediate state | Difficult to debug failures |
| Failures not replayable | Input not saved, cannot reproduce |

### v7 Solution: Context Service

```
Executor → Write Context to Redis → Start Claude CLI → Claude reads Context from API
         → AI output written back to Redis → Executor reads → Validates → Executes
```

## 2. System Architecture

> Note: The legacy `TaskOrchestrator` (in the deleted executor.py) has been replaced.
> Now handled collaboratively by governance server (port 40006) + executor-gateway (port 8090) + executor_api (port 40100).

```
┌────────────────────────────────────────────────────────────────┐
│                     Current Architecture                          │
│                                                                │
│  governance server (port 40006)                                │
│    │  task registry, workflow, audit, context API               │
│    │                                                           │
│  telegram_gateway (port 40010)                                 │
│    │  Telegram message routing                                  │
│    │                                                           │
│  executor-gateway (FastAPI port 8090)                          │
│    │  Actual task execution                                     │
│    │                                                           │
│  executor_api (port 40100)                                     │
│    │  Monitoring API                                            │
│    │                                                           │
│  Context management flow:                                       │
│    ├── 1. Assemble Context → governance API /api/context/*      │
│    ├── 2. Store Context → Redis + SQLite audit table            │
│    ├── 3. Start AI Session → executor-gateway scheduling        │
│    ├── 4. AI output writeback → governance API                  │
│    ├── 5. Validate + execute → governance workflow              │
│    └── 6. Archive → governance audit                           │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

## 3. Context Store Data Model

### Redis Structure (Hot Data, Real-Time Queries)

```
# Context input
ctx:input:{session_id} = HASH {
    "role": "dev",
    "project_id": "amingClaw",
    "prompt": "Modify gatekeeper.py...",
    "target_files": '["agent/governance/gatekeeper.py"]',
    "prd": '{...}',                          # PM's PRD (if any)
    "conversation_history": '[...]',          # Recent conversations
    "governance_summary": '{...}',            # Node status
    "memories": '[...]',                      # Related memories
    "git_status": '{...}',                   # Current git status
    "image_paths": '[]',                      # Image file paths
    "file_contents": '{...}',                # Key file content snippets
    "created_at": "2026-03-23T..."
}

# Context output
ctx:output:{session_id} = HASH {
    "status": "completed|failed|timeout",
    "stdout": "...",
    "stderr": "...",
    "parsed_decision": '{...}',              # Parsed structured decision
    "validation_result": '{...}',            # Validator result
    "executed_actions": '[...]',             # Actually executed actions
    "rejected_actions": '[...]',             # Rejected actions
    "evidence": '{...}',                     # Independently collected evidence
    "completed_at": "2026-03-23T..."
}

# TTL: 24h (active session), deleted after archiving
```

### SQLite Audit Table (Cold Data, Persistent)

```sql
CREATE TABLE context_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    project_id      TEXT NOT NULL,
    role            TEXT NOT NULL,
    task_id         TEXT,

    -- Input
    prompt          TEXT NOT NULL,
    target_files    TEXT,            -- JSON array
    prd_json        TEXT,            -- PM PRD
    context_json    TEXT NOT NULL,   -- Complete context snapshot
    image_paths     TEXT,            -- JSON array

    -- Output
    ai_stdout       TEXT,
    ai_stderr       TEXT,
    parsed_json     TEXT,            -- Parsed decision

    -- Validation
    validation_json TEXT,            -- Validator result
    approved_actions TEXT,           -- JSON array
    rejected_actions TEXT,           -- JSON array

    -- Evidence
    evidence_json   TEXT,            -- Independently collected evidence

    -- Metadata
    status          TEXT NOT NULL,   -- pending|running|completed|failed
    duration_ms     INTEGER,
    created_at      TEXT NOT NULL,
    completed_at    TEXT
);

CREATE INDEX idx_ctx_project ON context_audit(project_id, created_at);
CREATE INDEX idx_ctx_session ON context_audit(session_id);
CREATE INDEX idx_ctx_task ON context_audit(task_id);
```

## 4. Executor API New Endpoints

### Observer-Queryable Endpoints

```
# View AI session input in real time
GET /ctx/{session_id}/input
Returns: Complete structure of Context input

# View AI session output in real time
GET /ctx/{session_id}/output
Returns: AI output + validation results

# View complete session chain
GET /ctx/{session_id}/trace
Returns: input → output → validation → execution → evidence full chain

# List recent context sessions
GET /ctx/list?project_id=amingClaw&role=dev&limit=10
Returns: Recent session list

# Replay: Re-run with the same input
POST /ctx/{session_id}/replay
Returns: New session_id (re-run with old input)

# Compare two runs
GET /ctx/diff?a={session_id_1}&b={session_id_2}
Returns: Input/output differences between two runs
```

### AI Session Callable Endpoints

```
# AI reads full context from Context Service (replaces CLI -p)
GET /ctx/{session_id}/prompt
Returns: Assembled prompt text (with role instructions + context + user message)

# AI reports intermediate state (optional)
POST /ctx/{session_id}/progress
Body: {"phase": "coding", "percent": 50, "message": "Modified 2 files"}
```

## 5. CLI Invocation Changes

### Legacy Method (Removed)

> The CLI `-p` stdin mode from the old executor.py was deleted along with the entire bot system.

### Current Approach: system-prompt-file (via executor-gateway)
```python
# 1. Write context to temporary file
ctx_file = f"/tmp/ctx-{session_id}.md"
with open(ctx_file, "w") as f:
    f.write(assembled_prompt)

# 2. Store in both Redis + SQLite (audit)
context_store.save_input(session_id, context)

# 3. Pass via --system-prompt-file
process = subprocess.Popen(
    [claude_bin, "-p",
     "--system-prompt-file", ctx_file,
     "--output-format", "json",
     prompt],  # Only pass user message as prompt arg
    stdout=subprocess.PIPE,
)

# 4. Collect output
stdout, _ = process.communicate(timeout=timeout_sec)

# 5. Store output in Redis + SQLite
context_store.save_output(session_id, stdout)
```

### v7 Approach B: append-system-prompt (alternative)
```python
process = subprocess.Popen(
    [claude_bin, "-p",
     "--append-system-prompt", f"Context stored at {session_id}, key files: {target_files}",
     "--output-format", "json",
     prompt],
    stdout=subprocess.PIPE,
)
```

### v7 Approach C: API Read (most complete but depends on network)
```python
# Tell the AI session in its prompt to read context from the API on startup
prompt = f"""
First call curl http://localhost:40100/ctx/{session_id}/prompt to get the full context,
then execute the task based on that context.
"""
```

**Recommended: Approach A**: system-prompt-file is most stable, does not depend on the network, and file contents are complete. File contents are also copied to Redis/SQLite for auditing.

## 6. Observer System

### 6.1 Role Definition

```
Observer = Full-process monitor of task execution + report generator
Responsibilities: Monitor → Root cause analysis → Record → Generate report → Translate for user
Does not: Directly modify code (unless system cannot self-repair, then downgrade to manual mode)
```

### 6.2 Two Observer Modes

```
┌─────────────────────────────────────────────────────────────┐
│ Mode A: Automatic Observer (built into Executor)            │
│                                                             │
│ Task created → Executor auto-starts observer session        │
│   → Monitors each phase (PM/Coord/Dev/Tester/QA/Gatekeeper)│
│   → Records timestamps, input/output, exceptions           │
│   → Task completed → Auto-generates report to dbservice     │
│   → Suitable for: background tasks, unattended operation    │
│                                                             │
│ Lifecycle = task lifecycle                                   │
│ Task ends → observer session closes                         │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ Mode B: Manual Observer (Claude Code session takeover)      │
│                                                             │
│ User opens Claude Code session                              │
│   → POST /observer/attach {task_id}                         │
│   → Takes over auto-observer (or creates new)               │
│   → Real-time view of context/trace/validation              │
│   → Can intervene: pause/cancel/retry task                  │
│   → Can analyze: 5 Whys, quality scoring                    │
│   → Task completed → Add manual analysis to report          │
│   → Suitable for: critical tasks, debugging, iteration      │
│                                                             │
│ Lifecycle = Duration of Claude Code session                  │
│ Can monitor multiple tasks, compare across tasks            │
└─────────────────────────────────────────────────────────────┘
```

### 6.3 Observer Registration + Permissions

```
Observer registration flow:

Auto mode:
  Executor auto-registers when starting a task
  POST /api/role/assign {
    role: "observer",
    principal_id: "auto-observer-{task_id}",
    scope: ["read_ctx", "write_report"]
  }

Manual mode (Claude Code session):
  POST /observer/attach {task_id, session_type: "human"}
  → Verify: user must have coordinator token
  → Returns: observer_token + current task status
  → Takeover: auto-observer pauses, manual observer takes over

Permission matrix:
  ┌──────────────────┬────────┬────────┬────────────────────────────────┐
  │ Operation        │ Auto   │ Manual │ Description                    │
  ├──────────────────┼────────┼────────┼────────────────────────────────┤
  │ Read /ctx/*       │ ✅     │ ✅     │ read + report + control        │
  │ Read /status      │ ✅     │ ✅     │                                │
  │ Read /traces      │ ✅     │ ✅     │                                │
  │ Write /report     │ ✅     │ ✅     │                                │
  │ Pause/cancel task │ ❌     │ ✅     │ control (intervene, not exec)  │
  │ Create new task   │ ❌     │ ✅*    │ Via coordinator, not directly   │
  │ Downgrade to      │ ❌     │ ✅**   │ Requires explicit declaration, │
  │  manual mode      │        │        │  audit recorded                │
  │ Modify code       │ ❌     │ ❌***  │ Only in downgrade mode per     │
  │                   │        │        │  workflow                      │
  └──────────────────┴────────┴────────┴────────────────────────────────┘

  Core principle (codex review R2 #3):
  Observer defaults to read + report + control only, no domain execution.
  Coordinator should not write code itself, Observer should not default to fixing code itself.

  * Create new task: Observer creates indirectly via /coordinator/chat,
    does not create task files directly. Maintains role separation.
  ** Downgrade: Must explicitly call POST /observer/downgrade
    → Audit records "observer downgraded to manual"
    → From this point, operates per workflow process
    → Auto-restores to observer mode after task completion
  *** Modify code: Only allowed after downgrade, and must:
    1. Stop executor (prevent concurrency)
    2. Create node (if none exists)
    3. Modify code
    4. Commit
    5. coverage-check + verify + verify_loop
    6. Restore to observer mode
```

### 6.3.1 System KPIs (codex review R2 #4)

Observer reports must include the following core metrics to measure "whether the system is getting stronger":

```
System Health KPIs:

  First pass rate (first_pass_rate):
    = Tasks that pass without retry / total tasks
    Target: > 70%

  Average retry rounds (avg_retry_rounds):
    = sum(retry_round) / total tasks
    Target: < 1.5

  Validator rejection rate (validator_reject_rate):
    = rejected_actions / total_actions
    Target: Stable at 5-15% (too low = not catching issues, too high = AI too poor)

  AI self-report vs Evidence mismatch rate (evidence_mismatch_rate):
    = Tasks where evidence differs from AI self-report / total tasks
    Target: < 10%

  Wrong file modification rate (wrong_file_rate):
    = Tasks that modified non-target_files / total dev_tasks
    Target: < 5% (should decrease after PM fixes)

  Manual downgrade rate (manual_downgrade_rate):
    = Number of downgrades to manual / total iteration rounds
    Target: Continuously decreasing

  Replay reproduction rate (replay_success_rate):
    = Consistent replay results / total replays
    Target: > 80%

  PM trigger accuracy (pm_trigger_accuracy):
    = Correct PM triggers / (total that should have triggered)
    Target: > 90%
```

### 6.4 Unified Task Entry Point

Telegram and Claude Code use **the same API to create tasks**, only the notification method differs:

```
POST /executor/task  ← Unified entry point
Body: {
    "source": "telegram|claude_code",   // Source
    "session_type": "auto|human_observer",  // Observer mode
    "message": "Fix bug",               // User requirement
    "project_id": "amingClaw",          // Project
    "chat_id": 7848961760,              // Required for Telegram
}
Returns: {
    "task_id": "task-xxx",
    "observer_token": "obs-xxx",        // For subsequent queries
    "observer_url": "/observer/watch/task-xxx",
    "status": "created"
}
```

#### Complete Flow Comparison Between Two Entry Points

```
┌────────────────────────────────────────────────────────────────┐
│ Entry A: Telegram                                               │
│                                                                │
│ User sends Telegram message: "Fix context bug"                  │
│   ↓                                                            │
│ Gateway receives → calls POST /executor/task {                  │
│   source: "telegram", chat_id: 7848961760,                     │
│   session_type: "auto",                                        │
│   message: "Fix context bug", project_id: "amingClaw"           │
│ }                                                              │
│   ↓                                                            │
│ Executor:                                                      │
│   1. Create task (DB + file)                                    │
│   2. Auto-register observer session (auto mode)                 │
│   3. Return {task_id, observer_token} to Gateway                │
│   4. TaskOrchestrator.handle_user_message()                    │
│   5. PM → Coordinator → Dev → Tester → QA → Gatekeeper        │
│   ↓                                                            │
│ Gateway:                                                       │
│   Holds observer_token → polls /observer/status every 30s      │
│   Status change → push to Telegram                              │
│   Complete → push report summary                                │
└────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────┐
│ Entry B: Claude Code session                                    │
│                                                                │
│ User in terminal: curl POST /executor/task {                    │
│   source: "claude_code",                                       │
│   session_type: "human_observer",                              │
│   message: "Fix context bug", project_id: "amingClaw"           │
│ }                                                              │
│   ↓                                                            │
│ Executor:                                                      │
│   1. Create task (DB + file)                                    │
│   2. Register observer session (human mode, can intervene)      │
│   3. Return {task_id, observer_token} to Claude Code            │
│   4. TaskOrchestrator.handle_user_message()                    │
│   5. PM → Coordinator → Dev → Tester → QA → Gatekeeper        │
│   ↓                                                            │
│ Claude Code session:                                           │
│   Holds observer_token → active queries:                        │
│     curl /observer/status?token=obs-xxx                        │
│     curl /ctx/{session_id}/trace                               │
│     curl /observer/report/{task_id}                            │
│   Can intervene:                                                │
│     curl POST /observer/pause {task_id}                        │
│     curl POST /observer/cancel {task_id}                       │
│   Complete → view full report + add analysis                    │
└────────────────────────────────────────────────────────────────┘
```

#### Notification Method Differences

| | Telegram Entry | Claude Code Entry |
|---|---|---|
| Task creation | Same /executor/task | Same /executor/task |
| Observer registration | Auto (Gateway holds token) | Auto (returned to session) |
| Progress notification | Gateway polls → push Telegram | Session actively curls |
| Completion notification | Gateway push Telegram | Session reads /observer/report |
| Intervention capability | Limited (Telegram buttons) | Full (pause/cancel/downgrade) |
| Report viewing | /report Telegram command | curl /observer/report |
| Downgrade to fix code | Not supported | Supported (explicit declaration) |

### 6.5 Executor API — Observer Endpoints

```
# Unified task creation (replaces the legacy /coordinator/chat)
POST /executor/task
Body: {source, session_type, message, project_id, chat_id?}
Returns: {task_id, observer_token, observer_url, status}

# Take over an existing task's observer (switch from auto to human)
POST /observer/attach
Body: {"task_id": "xxx", "observer_token": "obs-xxx"}
Returns: {"observer_id": "...", "mode": "human", "task_status": "..."}

# Release observer (restore auto mode)
POST /observer/detach
Body: {"observer_id": "xxx"}

# Query current task status
GET /observer/status?token=obs-xxx
Returns: {"task_id": "...", "phase": "dev", "progress": 50, "duration": 120}

# View tasks being monitored by the observer
GET /observer/watching
Returns: {"tasks": [{"task_id": "...", "phase": "dev", "duration": 120}]}

# View/download execution report
GET /observer/report/{task_id}
Returns: {Full report JSON}

# Observer list
GET /observer/list
Returns: {"observers": [{"id": "...", "type": "auto|human", "task_id": "..."}]}

# Intervention actions (human observers only)
POST /observer/pause   Body: {"task_id": "xxx"}
POST /observer/cancel  Body: {"task_id": "xxx"}
POST /observer/retry   Body: {"task_id": "xxx"}

# Downgrade to manual mode
POST /observer/downgrade
Body: {"observer_id": "xxx", "reason": "System cannot self-repair"}
→ Audit records "observer downgraded"
→ Must operate per workflow
```

### 6.6 Telegram Observer Commands

```
Available to users in Telegram:
  /observe task-xxx   → View task real-time status (simplified trace)
  /report task-xxx    → View execution report
  /trace task-xxx     → View full chain
  /reports            → Last 10 report summaries
```

### 6.6 Execution Report Structure

After each task completes, the observer (auto or manual) must generate a report and write it to dbservice:

```json
{
    "refId": "report:{task_id}",
    "type": "observation_report",
    "scope": "{project_id}",
    "content": {
        "task_id": "task-xxx",
        "task_prompt": "Fix context system",
        "project_id": "amingClaw",
        "observer_type": "auto|human",
        "observer_id": "...",
        "duration_sec": 180,
        "started_at": "...",
        "completed_at": "...",

        "phases": {
            "pm": {
                "triggered": true,
                "duration_sec": 15,
                "prd_quality": "good|partial|poor",
                "target_files_provided": true,
                "nodes_proposed": 1
            },
            "coordinator": {
                "actions_created": 2,
                "actions_rejected": 0,
                "context_continuous": true
            },
            "dev": {
                "branch": "dev/task-xxx",
                "worktree_used": true,
                "files_changed": ["agent/governance/gatekeeper.py"],
                "files_match_target": true,
                "test_passed": true,
                "evidence_collected": true,
                "ai_vs_evidence_match": true
            },
            "eval": {
                "triggered": true,
                "verdict": "pass",
                "retry_round": 0
            },
            "tester": {
                "triggered": true,
                "tests_run": 15,
                "tests_passed": 15,
                "verify_status": "t2_pass"
            },
            "qa": {
                "triggered": true,
                "verdict": "pass",
                "verify_status": "qa_pass"
            },
            "gatekeeper": {
                "isolated_session": true,
                "checks_passed": ["coverage", "artifacts", "staleness"],
                "overall": "pass"
            }
        },

        "issues_found": [
            {
                "phase": "dev",
                "type": "wrong_file_modified",
                "severity": "high",
                "description": "Dev AI modified executor.py instead of gatekeeper.py",
                "root_cause": "PM target_files not passed to dev prompt",
                "resolved": true,
                "resolution": "Manually fixed PM trigger conditions"
            }
        ],

        "five_whys": [
            {
                "symptom": "Dev modified wrong file",
                "why1": "No target_files in prompt",
                "why2": "Coordinator create_dev_task didn't include target_files",
                "why3": "PM didn't trigger so no PRD",
                "fix": "Broaden PM keywords",
                "fix_applied": true
            }
        ],

        "quality_scores": {
            "flow_correctness": 8,
            "memory_quality": 7,
            "context_continuity": 9,
            "executor_constraints": 8,
            "evidence_reliability": 8,
            "overall": 8
        },

        "recommendations": [
            "PM keywords need to be further expanded",
            "Dev AI has insufficient awareness of governance/ subdirectory paths"
        ],

        "system_improvements": [
            {
                "type": "validator_rule",
                "description": "dev_task without target_files should be rejected by validator",
                "priority": "P0",
                "status": "implemented"
            }
        ]
    }
}
```

### 6.7 Claude Code Observer Prompt

When users use a Claude Code session as a manual observer, use the following prompt:

```
You are the manual observer for the {project_id} project.

## Role
- Observer: Monitor task execution → Analyze issues → Generate reports → Translate for user
- Can intervene: Pause/cancel/retry task
- Can downgrade: Manually fix code per workflow when system cannot self-repair

## Connection Method (Unified Entry Point)

1. Create task + auto-register observer:
   RESULT=$(curl -s -X POST http://localhost:40100/executor/task \
     -H "Content-Type: application/json" \
     -d '{
       "source": "claude_code",
       "session_type": "human_observer",
       "message": "Your requirement description",
       "project_id": "{project_id}"
     }')
   # Returns: {task_id, observer_token, observer_url}
   TASK_ID=$(echo $RESULT | python -c "import sys,json;print(json.load(sys.stdin)['task_id'])")
   OBS_TOKEN=$(echo $RESULT | python -c "import sys,json;print(json.load(sys.stdin)['observer_token'])")

2. Monitor task status:
   # Real-time status
   curl http://localhost:40100/observer/status?token=$OBS_TOKEN
   # Full trace
   curl http://localhost:40100/ctx/$TASK_ID/trace
   # Current phase
   curl http://localhost:40100/observer/watching

3. Take over existing task (created from Telegram):
   curl -X POST http://localhost:40100/observer/attach \
     -H "Content-Type: application/json" \
     -d '{"task_id":"task-xxx", "observer_token":"obs-xxx"}'

4. Intervene (human mode only):
   curl -X POST http://localhost:40100/observer/pause -d '{"task_id":"xxx"}'
   curl -X POST http://localhost:40100/observer/cancel -d '{"task_id":"xxx"}'
   curl -X POST http://localhost:40100/observer/retry -d '{"task_id":"xxx"}'

5. View report:
   curl http://localhost:40100/observer/report/$TASK_ID

6. Telegram notification:
   curl -X POST http://localhost:40000/gateway/reply \
     -H "X-Gov-Token: {coordinator_token}" \
     -d '{"chat_id": {chat_id}, "text": "message"}'

7. Downgrade to manual mode (only when system cannot self-repair):
   curl -X POST http://localhost:40100/observer/downgrade \
     -d '{"observer_id":"xxx", "reason":"explain reason"}'
   # From this point, must operate code per workflow process

## Observer SOP

### Task Dispatch Flow

```
1. Dispatch task
   curl -X POST http://localhost:40100/coordinator/chat \
     -d '{"message":"...", "project_id":"..."}'

2. Confirm whether PM triggered
   Check if reply contains "PRD" / "PM" / "target_files"
   If not → Check whether _needs_pm_analysis matches keywords

3. Monitor Dev execution
   curl http://localhost:40100/status
   curl http://localhost:40100/ctx/list?role=dev  (v7)
   ls shared-volume/codex-tasks/processing/

4. Post-Dev completion checks
   Check branch changes: git diff main..dev/task-xxx
   Check if correct files were modified (compare against PM target_files)
   Check evidence: curl http://localhost:40100/ctx/{sid}/trace (v7)
```

### Failure Root Cause Analysis SOP (5 Whys)

```
After auto-fix fails, the observer must execute the following analysis chain:

Step 1: Record the failure symptom
  "Dev task completed but modified executor.py instead of evidence.py"

Step 2: Why x1 — Direct cause
  "Dev AI prompt did not specify target_files"

  Verification method:
  - v7: curl /ctx/{session_id}/input → Check target_files field
  - v6: Check task file's _coordinator_context

Step 3: Why x2 — Upstream cause
  "Coordinator's create_dev_task did not include target_files"

  Verification method:
  - Check if Coordinator output actions contain target_files
  - Check if DecisionValidator should reject dev_task without target_files

Step 4: Why x3 — System cause
  "PM didn't trigger, so no PRD provided target_files"

  Verification method:
  - Check executor logs: grep "PM check"
  - Check _needs_pm_analysis keyword matching

Step 5: Locate fix point
  Classification:
  a) System bug → Add to auto-fix queue
  b) Configuration issue → Manually adjust then retry
  c) AI capability boundary → Record as known limitation, fix manually
  d) Architecture gap → Record as Gap, design solution then fix

  Must never do:
  ❌ See failure and just "move to manual group"
  ❌ Simply retry without analyzing cause
  ❌ Only record surface symptoms without tracing root cause
```

### Quality Assessment SOP

```
After each iteration round, the observer must evaluate:

1. Process correctness
   - Did PM trigger? What was the PRD quality?
   - Was Coordinator orchestration reasonable?
   - Did Dev modify the correct files?
   - Did Tester/QA/Gatekeeper trigger?
   - Is verify_loop all green?

2. Memory quality
   - Are new memories valuable?
   - Are there duplicates/noise?
   - Are refIds semantic (not UUIDs)?

3. Context continuity
   - Does Coordinator remember what was said across multiple messages?
   - Is session_context correctly updated?

4. Executor constraints
   - What did DecisionValidator reject? Should it have rejected what it didn't?
   - What did EvidenceCollector collect? Does it match AI self-report?
   - Was tool_policy bypassed?

5. Performance
   - Dev task execution time
   - Number of orphaned processes
   - Failure retry count
```

### Iteration Loop Exit Conditions

```
Loop exits if and only if:
  1. Manual group is empty
  2. All P0 completed (auto or manual)
  3. All P1 completed (auto or manual)
  4. All P2 completed or closed after assessment
  5. No new manual items added

Each round in the loop:
  1. Fix manual group first
  2. Auto group attempts fixes
     - Success → Observe quality → Add new issues to list if found
     - Failure → 5 Whys analysis → Fix system if possible then retry → Otherwise move to manual
  3. Re-sort by severity
  4. Return to 1
```

## 7. Review Feedback Integration (codex evaluation 6 points)

### 7.1 Dual-Write Consistency → Session State Machine + Idempotency

**Problem:** Redis + SQLite dual-write has no transaction semantics; Redis may have data that SQLite does not.

**Fix:** Define an explicit session lifecycle state machine:

```
created → input_saved → prompt_rendered → running → output_saved → validated → executed → archived
                                                                                    ↘ failed
```

#### State Transition Constraints (codex review R2 #1 supplement)

| Current State | Allowed Transitions | Forbidden | Description |
|---------------|--------------------|-----------|----|
| created | input_saved | running, archived | Must save input first |
| input_saved | prompt_rendered | running | Must render first |
| prompt_rendered | running | validated | Must run first |
| running | output_saved, failed | archived | Can only complete normally or fail |
| output_saved | validated | executed | Must validate first |
| validated | executed, failed | archived | Execute only if validated, failure can reject |
| executed | archived | running | Archive after execution, cannot revert |
| archived | — | Any transition | Terminal state, read-only |
| failed | created (new attempt) | running | After failure must create new attempt, no resume |

**Key Rules:**
- Illegal transitions are always rejected and recorded as violations
- Duplicate writes to the same state → Idempotent (check idempotency_key, skip if exists)
- **replay = new session + new attempt_no**, not changing the original session's state
- **After failed, cannot directly return to validated**; must create a new session from created
- **After archived, completely read-only**; any write request returns 403

```python
VALID_TRANSITIONS = {
    "created":          {"input_saved"},
    "input_saved":      {"prompt_rendered"},
    "prompt_rendered":  {"running"},
    "running":          {"output_saved", "failed"},
    "output_saved":     {"validated"},
    "validated":        {"executed", "failed"},
    "executed":         {"archived"},
    "archived":         set(),  # terminal, read-only
    "failed":           set(),  # terminal; retry = new session
}

def transition(self, session_id: str, from_state: str, to_state: str) -> bool:
    allowed = VALID_TRANSITIONS.get(from_state, set())
    if to_state not in allowed:
        self._record_violation(session_id, from_state, to_state)
        return False
    # CAS: compare-and-swap with version
    return self._cas_update(session_id, from_state, to_state)
```

All write operations must include:
- `session_id` — Unique identifier
- `attempt_no` — Attempt number (incremented on retry, starting from 0)
- `idempotency_key` — `{session_id}:{attempt_no}:{phase}`
- `version` — Optimistic lock (CAS)

Write strategy: **SQLite writes first (source of truth), Redis writes second (cache).** Redis write failure does not block; SQLite write failure causes the entire operation to fail.

```python
def save_input(self, session_id, context):
    idem_key = f"{session_id}:0:input"
    # 1. Check idempotency
    if self._idem_exists(idem_key):
        return  # Already saved, skip
    # 2. SQLite first (truth)
    self._sqlite_write(session_id, "input_saved", context, idem_key)
    # 3. Redis cache (best-effort)
    try:
        self._redis_write(f"ctx:input:{session_id}", context, ttl=86400)
    except Exception:
        pass  # Redis failure is non-fatal
```

### 7.2 Single Source of Truth → Structured Snapshot is Canonical

**Problem:** Redis JSON and /tmp prompt file may be inconsistent.

**Principles:**
- **Canonical source = context_json in the SQLite context_audit table**
- Prompt file is a render artifact, generated by `PromptRenderer` from the canonical source
- During audit, save both `renderer_version` and `rendered_prompt_hash`
- Trace displays `snapshot_hash` vs `rendered_hash`; alerts on mismatch

```python
class PromptRenderer:
    VERSION = "v1.0"

    def render(self, context: dict) -> str:
        """Generate prompt text from structured context."""
        text = self._format(context)
        return text

    def render_to_file(self, session_id: str, context: dict) -> tuple[str, str]:
        """Generate prompt file and return (file_path, content_hash)."""
        text = self.render(context)
        path = f"/tmp/ctx-{session_id}.md"
        with open(path, "w") as f:
            f.write(text)
        import hashlib
        content_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        return path, content_hash
```

### 7.3 Replay Bundle → Full Environment Reproduction

**Problem:** Saving only input is not enough; environment information is also needed.

**Replay Bundle must contain:**

```json
{
    "session_id": "...",
    "attempt_no": 0,
    "input": { "...canonical context..." },
    "environment": {
        "model_name": "claude-sonnet-4-5",
        "git_commit": "abc123",
        "git_branch": "dev/task-xxx",
        "workspace_dirty": false,
        "tool_versions": {"claude": "1.0.30", "python": "3.12"},
        "env_fingerprint": "sha256:...",
        "renderer_version": "v1.0",
        "rendered_prompt_hash": "abc123de",
        "timestamp": "2026-03-23T..."
    },
    "external_deps": {
        "governance_api_snapshot": {"total_nodes": 158, "qa_pass": 158},
        "dbservice_query_results": [{"refId": "...", "relevance": 0.9}],
        "redis_state": {"session_cache_hit": true},
        "file_system_state": {"target_files_exist": true, "file_hashes": {"agent/governance/gatekeeper.py": "sha256:..."}}
    },
    "time_boundary": {
        "context_assembled_at": "2026-03-23T18:00:00Z",
        "session_started_at": "2026-03-23T18:00:01Z",
        "external_deps_queried_at": "2026-03-23T18:00:00Z"
    },
    "evidence_snapshot": { "...before snapshot..." },
    "output": { "..." },
    "validation": { "..." }
}
```

**Additional notes (codex review R2 #2):**
- `external_deps` records snapshots/references of external tool call results, preventing "it's not the model that changed but the environment"
- `time_boundary` records time boundaries of key dependency inputs; during replay, can compare "environment then vs environment now"
- Without these, replay is "run again"; with these, replay is "restore the scene"

### 7.4 Permission Model → Four-Level Access Control

**Problem:** /ctx/* endpoints can see full context, the most sensitive layer.

| Permission Level | Can Access | Role |
|-----------------|------------|------|
| `observer_read` | /ctx/list, /ctx/{sid}/trace (summary) | Observer |
| `executor_internal` | /ctx/{sid}/input, /ctx/{sid}/output (full) | Executor internal |
| `ai_session_prompt_only` | /ctx/{sid}/prompt (rendered text) | AI session |
| `admin_full` | /ctx/{sid}/replay, /ctx/diff, full JSON | Admin |

**AI session defaults to read-only /prompt, cannot read full /input structure.** This prevents AI from seeing internal system metadata.

### 7.5 Context Budget → Moved Up to P0.5

**Problem:** Budget at P2 is too late; it directly affects AI behavior correctness.

**Budget rules for immediate implementation:**

```python
ROLE_BUDGETS = {
    "coordinator": {"max_tokens": 8000, "required": ["prompt", "conversation_history", "governance_summary"]},
    "pm":          {"max_tokens": 6000, "required": ["prompt", "governance_summary"]},
    "dev":         {"max_tokens": 4000, "required": ["prompt", "target_files", "file_contents"]},
    "tester":      {"max_tokens": 3000, "required": ["prompt", "changed_files", "test_commands"]},
    "qa":          {"max_tokens": 3000, "required": ["prompt", "evidence", "node_status"]},
}
```

Field priority (pruned from lowest to highest):
1. **Required**: prompt, target_files, role_instructions
2. **Important**: conversation_history (last 5 entries), governance_summary
3. **Supplementary**: memories, git_status, runtime_info
4. **Removable**: full file contents (convert to summary), old conversation history

### 7.6 SOP → Hard Rule Upgrade

**Problem:** SOP is governance by convention; needs to become Validator hard rules.

**New DecisionValidator rules:**

```python
# Issues discovered by observer SOP → upgraded to code-enforced rules

HARD_RULES = {
    "dev_task_must_have_target_files": {
        "check": lambda action: bool(action.get("target_files")),
        "reject_msg": "create_dev_task must include target_files (provided by PM PRD)",
    },
    "pm_required_for_complex_task": {
        "check": lambda action: True,  # Controlled by _needs_pm_analysis at orchestrator layer
        "reject_msg": "Complex tasks must go through PM analysis first",
    },
    "evidence_must_be_complete": {
        "check": lambda action: True,  # Checked by EvidenceCollector at dev_complete
        "reject_msg": "Incomplete evidence, merge/pass not allowed",
    },
    "session_must_have_snapshot": {
        "check": lambda action: True,  # Checked by ContextStore at session creation
        "reject_msg": "Session has no input snapshot, cannot execute",
    },
}
```

## 8. Iteration Retrospective — Defect Report (codex review R3)

### 8.1 Core Findings

| Category | Defect | Severity | Root Cause |
|----------|--------|----------|------------|
| **Architecture** | Node ID generated by AI | 🔴 | Giving deterministic metadata to a probabilistic model |
| **Architecture** | "Create new file" is not a first-class execution operation | 🔴 | Executor primarily consumes stdout/diff |
| **Architecture** | PM trigger relies on keyword luck | 🟡 | No hard rules, only SOP |
| **Process** | Manual modifications bypassed workflow (5 times) | 🔴 | "Emergency" mindset |
| **Process** | Manual modifications polluted auto experiments | 🟡 | Started new tasks without committing |
| **Analysis** | 5 Whys premature attribution (context pollution) | 🟡 | Explained before verifying |
| **Analysis** | "AI cannot create new files" conclusion too broad | 🟡 | System fault != AI capability boundary |
| **Runtime** | Worktree baseline not clean | 🟡 | Manual changes mixed with auto tasks |

### 8.2 System-Assigned Node ID Scheme

**Problem:** AI repeatedly generates empty IDs / placeholder IDs, rejected by validator.

**Root Cause:** The governance graph's node ID is a system internal primary key, essentially deterministic metadata that should not be generated by a probabilistic model.

**Solution: Separate node_uid and display_id**

```
Data model:
  node_uid:     n_8f3a2c...   (system-generated, never changes, for internal references)
  display_id:   L22.1         (system-assigned, human-readable, adjustable)
  parent_uid:   n_ab12...     (parent node reference)
  order_index:  3             (sibling ordering)
  title:        "ContextStore"

AI output (propose_node):
  {
    "parent_display_id": "L22",     ← AI only says "where to attach"
    "title": "ContextStore",         ← AI says "what to do"
    "description": "...",
    "acceptance_criteria": [...],
    "target_files": [...]
  }
  Does not output node_id / display_id / node_uid

System processing:
  1. Parse parent_display_id → find parent_uid
  2. Query the parent node's existing children for max sequence number
  3. Assign display_id = L22.3 (auto-increment)
  4. Generate node_uid = n_{uuid}
  5. Persist to DB + audit
```

### 8.3 Dev Task Explicit File Contract

**Problem:** Dev AI does not know whether to create new files or modify existing ones.

**Solution:** create_dev_task must include an explicit file contract:

```json
{
  "type": "create_dev_task",
  "target_files": ["agent/executor.py"],       // Existing files allowed to modify
  "create_files": ["agent/context_store.py"],   // New files that must be created
  "forbidden_files": ["agent/governance/*"],    // Files forbidden to modify
  "expected_artifacts": ["test_file"]           // Expected output artifacts
}
```

DecisionValidator checks:
- All `target_files` exist → reject otherwise
- All `create_files` do not exist → reject otherwise (prevent overwriting)
- After Dev completes, EvidenceCollector checks:
  - Files in `create_files` were actually created
  - `forbidden_files` were not modified
  - `expected_artifacts` were generated

### 8.4 Observer Discipline Constraints

**Problem:** The observer (me) made 5 manual modifications without following workflow.

**Hard rules (added to observer SOP):**

```
Pre-manual-modification checklist (all must be completed before touching code):
  □ Executor stopped (prevent concurrent pollution)
  □ git status clean (no uncommitted changes)
  □ Node exists (create one first if not)
  □ After modification: coverage-check + verify-update + verify_loop + commit
  □ Confirm worktree is clean before starting executor

Pre-auto-experiment checklist:
  □ main branch clean (git status shows no changes)
  □ Baseline commit fixed (record commit hash)
  □ No leftover dev branches
  □ Executor restarted (loads latest code)
```

### 8.5 Analysis Discipline

**5 Whys correct order:**

```
1. Look at raw output JSON (not reasoning)
2. Check if PM triggered (check logs, don't guess)
3. Check what validator rejected/passed (check trace)
4. Check what Dev AI actually did (check git diff)
5. Only then consider context pollution/AI capability boundaries

Do not:
  ❌ See a related symptom and explain first
  ❌ Draw "AI cannot do xxx" conclusions from a single failure
  ❌ Fail to distinguish system fault / experiment pollution / AI boundary
```

## 9. Revised Implementation Roadmap (v7.1)

Based on iteration retrospective, re-prioritized. **Core principle: Fix execution contracts first, then add audit capabilities.**

### P-1: Execution Contract Corrections (Highest Priority, Manual)

Must be completed manually first; otherwise, the auto-fix pipeline is unreliable.

**Common pattern: All are "bootstrapping paradoxes" -- what's being fixed is the infrastructure AI depends on to run; AI cannot fix its own runtime environment through itself.** Similar to how an OS cannot rewrite its own kernel at runtime.

| Step | Content | Location | Why Cannot Auto-Fix |
|------|---------|----------|---------------------|
| 0 | **System-assigned Node ID** | governance server | Auto-fix needs propose_node → propose_node depends on ID generation → **circular dependency** |
| 1 | **Dev task file contract** | governance server (decision validation) | Need to change validator's verification logic → Dev AI is verified by validator → **cannot change the rules that judge itself** |
| 2 | **PM hard rules** | governance server (task_registry) | When auto-fixing PM, Dev AI modifies wrong file → PM not triggering means cannot produce correct path through PM → **chicken-and-egg problem** |
| 3 | **Forced clean worktree** | executor-gateway | Need to change execution logic → Dev AI is invoked by executor-gateway → **cannot change the code that runs itself** |

### P0: Context Store Foundation (Completed ✅)

| Step | Content | Status |
|------|---------|--------|
| 4 | ContextStore + Session state machine | ✅ L22.1 auto-completed |
| 5 | AILifecycleManager system-prompt-file | ✅ L22.2 manually completed |
| 6 | Unified entry /executor/task | ✅ L22.3 auto-completed |

### P0.5: Budget + Hard Rules (Completed ✅)

| Step | Content | Status |
|------|---------|--------|
| 7 | Context budget role-based pruning | ✅ L22.4 auto-completed |
| 8 | DecisionValidator hard rules | ✅ L22.5 auto-completed |

### P1: Audit + Permissions + Observer

| Step | Content | File | Method |
|------|---------|------|--------|
| 9 | context_audit table + replay bundle | governance server | Auto |
| 10 | Four-level permission model | executor_api (port 40100) | Auto |
| 11 | /ctx/{sid}/trace full chain | executor_api (port 40100) | Auto |
| 12 | Observer system (attach/detach/report) | executor_api (port 40100) | Auto |
| 13 | KPI auto-collection | executor_api (port 40100) | Auto |

### P2: Enhancements

| Step | Content | File |
|------|---------|------|
| 14 | /ctx/{sid}/replay full reproduction | executor_api (port 40100) |
| 15 | /ctx/diff comparison | executor_api (port 40100) |
| 16 | Image paths + multimodal | governance server |
| 17 | AI progress reporting | executor_api (port 40100) |

### Implementation Order

```
1. P-1 (manual) → Fix execution contracts, make auto-fix reliable
2. P1 (auto attempt) → Audit + Observer
3. P2 (auto attempt) → Enhancements
4. Evaluate KPIs after each auto-fix round
```

## 10. Current Architecture Notes

> The legacy Telegram bot system (v6 and earlier 20 agent/ modules) has been completely removed. There is no longer a v6 fallback.

Current architecture key points:
- Context management entirely via governance API (`/api/context/*`)
- Task execution via executor-gateway (port 8090)
- Monitoring via executor_api (port 40100)
- Telegram message routing via telegram_gateway (port 40010)
- **SQLite is the sole source of truth, Redis is the cache layer**
- **AI session reads only /prompt, not full /input**
- **Node ID is system-assigned, AI only provides parent + title** (R3 fix)
- **Dev task must have explicit file contract** (R3 fix)

## Changelog
- 2026-03-26: Legacy Telegram bot system completely removed (bot_commands, coordinator, executor and 20 other modules); now using governance API exclusively
