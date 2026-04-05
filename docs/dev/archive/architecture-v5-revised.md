---
status: archived
superseded_by: architecture-v7-context-service.md
archived_date: 2026-04-05
historical_value: "Revised architecture with governance concepts"
do_not_use_for: "architecture decisions"
---

# Aming Claw Architecture v5 Revised

> Based on v5 initial draft + 10 review feedback items + lessons learned from Toolbox project.
> Core principle: Stabilize single-role single-task first, then add multi-role.

## Revision History

### Review Feedback Adopted

| # | Suggestion | Adopted | Revision |
|---|-----------|---------|----------|
| 1 | Runtime changed to state projection, Task Registry as single source of truth | ✅ | Runtime is read-only model |
| 2 | Add atomicity + recovery mechanism for task files | ✅ | tmp+rename, claim lease, startup recovery scan |
| 3 | Pub/Sub not the sole notification channel | ✅ | Pub/Sub for speed, persistence as fallback |
| 4 | Define complete task status enum | ✅ | 11 statuses |
| 5 | Message classifier changed to two-stage | ✅ | Rule interception + LLM follow-up |
| 6 | Notifications belong to task, not project view | ✅ | Task completion replies to original chat |
| 7 | Role conflict governance | ✅ Deferred | To be done in P2 |
| 8 | Executor permission boundary | ✅ | workspace allowlist + tool policy |
| 9 | Long task progress heartbeat | ✅ | phase + percent |
| 10 | Implementation order adjustment | ✅ | Reliability loop first, multi-role later |

### Toolbox Lessons Learned Adopted

| Lesson | Source | Impact on v5 |
|--------|--------|-------------|
| Coordinator only dispatches, no business logic | toolbox v1.4.4 | **Hard role responsibility constraint**: coord does not touch code/analysis/verification |
| Gatekeeper memory isolation | toolbox wf-gatekeeper | **Gatekeeper checks only see acceptance graph + task-log**, not coord context |
| Subprocess PID + orphan management | toolbox 14 leftover worktrees | **Runtime records worker_pid**, startup recovery scan kills orphans |
| Non-blocking dispatch | toolbox coord deadlocked | **Agent must launch in background**, coord does not block waiting |
| Release gate hard check | toolbox 6 nodes not green but released | **Code fix ≠ verify:pass**, nodes with code changes must be re-verified |
| coverage-check belongs to governance not runtime | Architecture analysis | **Static analysis stays out of runtime**, auto-triggered on phase transition |

## 1. Core Principles

```
1. Single source of truth: Task Registry (SQLite)
2. Runtime is a projection, not dual-write
3. File queue retained, but with added atomicity
4. Pub/Sub for speed, persistence as fallback
5. Stabilize single-role first, then add multi-role
6. Coordinator only dispatches, no business logic (toolbox lesson)
7. Gatekeeper memory isolation, naturally immune to drift (toolbox lesson)
8. Code fix ≠ verify:pass, must re-verify (toolbox lesson)
9. Message-driven: session is stateless, token bound to project not session
```

## 2. Task State Machine (Finalized, Revision #2)

### Dual-Field Model: Execution Status + Notification Status

```
Execution status and notification status are two independent dimensions, not mixed in a single status chain.

execution_status:
  queued ──→ claimed ──→ running ──→ succeeded
    │           │          │
    │           │          ├──→ failed
    │           │          │
    │           │          └──→ timed_out
    │           │
    └──→ cancelled

  running ──→ waiting_human ──→ running (after confirmation)
  running ──→ blocked ──→ running (after unblocked)

notification_status (independent field):
  none ──→ pending ──→ sent ──→ read
```

### Table Structure

```sql
ALTER TABLE tasks ADD COLUMN execution_status TEXT NOT NULL DEFAULT 'queued';
ALTER TABLE tasks ADD COLUMN notification_status TEXT NOT NULL DEFAULT 'none';
ALTER TABLE tasks ADD COLUMN notified_at TEXT;

-- execution_status tracks "where the task is at"
-- notification_status tracks "whether the user knows"
-- Both change independently, no mutual blocking
```

### Status Enum

```python
EXECUTION_STATUSES = {
    "queued",           # Created, waiting for claim
    "claimed",          # Executor has claimed, not yet started
    "running",          # Currently executing
    "waiting_human",    # Waiting for human confirmation (release/rollback)
    "blocked",          # Missing context/permissions, paused
    "succeeded",        # Execution succeeded
    "failed",           # Execution failed (retryable)
    "cancelled",        # Cancelled
    "timed_out",        # Timed out
    "enqueue_failed",   # DB write succeeded but file delivery failed
}

NOTIFICATION_STATUSES = {
    "none",             # No notification needed (query type)
    "pending",          # Notification needed but not yet sent
    "sent",             # Sent via Telegram
    "read",             # User confirmed viewing
}
```

## 3. Token Model (Simplified)

### Old Model vs New Model

```
Old (v4 dual token):
  Human init → refresh_token(90d)
  Session start → POST /api/token/refresh → access_token(4h)
  All APIs use access_token
  Refresh every 4h → session end deregister

New (v5 message-driven):
  Human init → project_token (no expiry)
  Gateway holds project_token → proxies all API calls
  CLI session only needs project_id → Gateway forwards
  No refresh/rotate/expire overhead
```

### Token Classification

| Token | Holder | TTL | Purpose |
|-------|--------|-----|---------|
| **project_token** | Gateway / Human | No expiry | Full project API permissions (coordinator level) |
| **agent_token** | dev/tester/qa process | 24h | Restricted API (only role operations like verify-update) |

### Security Guarantees

```
No expiry does not mean insecure:

1. Password protection: set password at init, resetting token requires password
2. Revocable: POST /api/token/revoke (manual operation)
3. Network isolation: token only used on localhost / Docker internal network
4. Gateway proxy: CLI session does not directly hold token
   → Gateway receives message → calls API with its stored token
   → CLI session only needs project_id
5. agent_token still has TTL: independent process permissions are time-limited
```

### Removed Components

```
Removed:
  - /api/token/refresh  → Not needed, project_token has no expiry
  - /api/token/rotate   → Simplified to /api/token/revoke + re-init
  - access_token (gat-*) → Not needed, use project_token (gov-*) directly
  - token_service.py    → Can keep but mark as deprecated

Retained:
  - /api/token/revoke   → Secure revocation capability
  - /api/init           → Create project + obtain project_token
  - /api/role/assign    → Coordinator assigns agent_token (24h TTL)
```

### Gateway as Token Proxy

```
User Telegram message
    ↓
Gateway looks up routing table → finds project_token
    ↓
Gateway calls governance API with project_token
    ↓
CLI session does not need to manage tokens itself

CLI session at startup:
    Not needed: token refresh / agent register / lease
    Only needed: know project_id + Gateway URL
    Gateway handles all authentication for it
```

## 4. Task Registry as Single Source of Truth

```
All state changes only write to Task Registry (SQLite):

  Gateway creates task    → INSERT tasks SET status='queued'
  Executor claim          → UPDATE tasks SET status='claimed', worker_id, lease_expires_at
  Executor starts running → UPDATE tasks SET status='running'
  Executor completes      → UPDATE tasks SET status='succeeded', result_json
  Executor fails          → UPDATE tasks SET status='failed', error, attempt+1
  Timeout                 → UPDATE tasks SET status='timed_out'
  Waiting for human       → UPDATE tasks SET status='waiting_human'
  Gateway notified        → UPDATE tasks SET status='notified', notified_at

Runtime API only reads Task Registry for projection, does not maintain its own state.
```

### Runtime Projection API

```python
@route("GET", "/api/runtime/{project_id}")
def handle_runtime(ctx):
    """Projection view, no stored state. Real-time query from Task Registry each time."""
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        active = task_registry.list_tasks(conn, project_id, status="running")
        queued = task_registry.list_tasks(conn, project_id, status="queued")
        pending_notify = task_registry.list_tasks(conn, project_id, status="notify_pending")
        context = session_context.load_snapshot(project_id)

    return {
        "project_id": project_id,
        "active_tasks": active,
        "queued_tasks": queued,
        "pending_notifications": pending_notify,
        "context": context,
    }
```

## 4. Atomic File Delivery

### 4.1 Write Order: DB Before File (Revision #1)

```
Key principle: Task "existence" is defined by DB, not by file.

Order:
  1. DB INSERT tasks (status=queued)    ← Task is born
  2. Write task file (tmp → fsync → rename) ← Deliver to Executor
  3. If file write fails → DB UPDATE status='enqueue_failed'

Benefits:
  - When Executor scans a file, DB always has a record
  - DB has record but no file → re-deliver or mark failed during recovery
  - No inconsistency of "file exists but DB doesn't"
```

```python
def create_task_file(project_id, prompt, backend="claude", chat_id=0):
    task_id = new_task_id()

    # 1. Write DB first (task birth point)
    with DBContext(project_id) as conn:
        task_registry.create_task(conn, project_id, prompt,
            task_type=backend, created_by="gateway",
            metadata={"chat_id": chat_id})

    # 2. Then write file (deliver to Executor)
    task_data = {
        "task_id": task_id,
        "project_id": project_id,
        "chat_id": chat_id,
        "prompt": prompt,
        "backend": backend,
        "attempt": 0,
        "max_attempts": 3,
        "created_at": utc_iso(),
    }

    try:
        # Atomic write: write tmp first, fsync, then rename
        tmp_path = pending_dir / f"{task_id}.json.tmp"
        final_path = pending_dir / f"{task_id}.json"

        with open(tmp_path, "w") as f:
        json.dump(task_data, f)
        f.flush()
        os.fsync(f.fileno())

    os.rename(tmp_path, final_path)  # Atomic operation

    # Also write to Task Registry
    with DBContext(project_id) as conn:
        task_registry.create_task(conn, project_id, prompt,
            task_type=backend, created_by="gateway",
            metadata={"chat_id": chat_id})

    return task_id
```

### 4.2 Claim with Fencing Token (Revision #3)

```python
def claim_task(task_file):
    task = load_json(task_file)
    task_id = task["task_id"]
    project_id = task["project_id"]

    # Generate fencing token (prevent double execution)
    fence_token = f"fence-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    lease_expires = utc_iso_after(seconds=300)  # 5 minute lease

    # 1. Atomic claim: CAS update (only queued status can be claimed)
    with DBContext(project_id) as conn:
        result = conn.execute(
            """UPDATE tasks SET execution_status='claimed',
               assigned_to=?,
               started_at=?,
               metadata_json=json_set(metadata_json,
                 '$.lease_expires_at', ?,
                 '$.lease_owner', ?,
                 '$.fence_token', ?,
                 '$.lease_version', COALESCE(
                   json_extract(metadata_json, '$.lease_version'), 0) + 1
               )
               WHERE task_id=? AND execution_status IN ('queued','created')""",
            (worker_id, utc_iso(), lease_expires, worker_id, fence_token, task_id)
        )
        if result.rowcount == 0:
            return None  # Already claimed by another worker

    # 2. Move file
    os.rename(pending_path, processing_path)
    return task, fence_token

# When executing tasks, validate fence_token on every DB write
def update_with_fence(conn, task_id, fence_token, **updates):
    """Update with fencing token, prevents old worker from overwriting new worker's state"""
    current = conn.execute(
        "SELECT json_extract(metadata_json, '$.fence_token') FROM tasks WHERE task_id=?",
        (task_id,)
    ).fetchone()
    if current and current[0] != fence_token:
        raise RuntimeError(f"Fence token mismatch: task reclaimed by another worker")
    # Safe update...

# Lease renewal (during heartbeat)
def renew_lease(conn, task_id, fence_token):
    new_expires = utc_iso_after(seconds=300)
    conn.execute(
        """UPDATE tasks SET metadata_json=json_set(metadata_json,
             '$.lease_expires_at', ?,
             '$.lease_version', json_extract(metadata_json, '$.lease_version') + 1
           ) WHERE task_id=? AND json_extract(metadata_json, '$.fence_token')=?""",
        (new_expires, task_id, fence_token)
    )
```

### 4.3 Startup Recovery

```python
def recover_on_startup():
    """Recover stuck tasks on Executor startup"""

    # 1. Scan processing/ directory
    for f in processing_dir.glob("*.json"):
        task = load_json(f)
        task_id = task["task_id"]

        # Check Task Registry status
        with DBContext(task["project_id"]) as conn:
            db_task = task_registry.get_task(conn, task_id)

        if not db_task:
            # Orphan file, move back to pending
            os.rename(f, pending_dir / f.name)
            continue

        if db_task["status"] in ("claimed", "running"):
            # Lease expired → re-queue
            if db_task.get("lease_expires_at", "") < utc_iso():
                os.rename(f, pending_dir / f.name)
                with DBContext(task["project_id"]) as conn:
                    conn.execute("UPDATE tasks SET status='queued' WHERE task_id=?", (task_id,))
                    conn.commit()

    # 2. Scan Task Registry for claimed/running with expired lease
    for project in list_projects():
        with DBContext(project["project_id"]) as conn:
            stale = conn.execute(
                """SELECT task_id FROM tasks
                   WHERE status IN ('claimed','running')
                   AND json_extract(metadata_json, '$.lease_expires_at') < ?""",
                (utc_iso(),)
            ).fetchall()
            for row in stale:
                conn.execute("UPDATE tasks SET status='queued' WHERE task_id=?", (row["task_id"],))
            conn.commit()
```

## 5. Notification Reliability

```
Executor completes task:
    │
    ├── 1. UPDATE Task Registry: running → succeeded (persisted)
    ├── 2. UPDATE Task Registry: status = 'notify_pending' (persisted)
    ├── 3. Redis PUBLISH task:completed (acceleration, not required)
    │
    ▼
Gateway notifies user (two paths, mutual backup):
    │
    ├── Path A: Pub/Sub subscription → received → reply Telegram → UPDATE notified
    │
    └── Path B: Periodically scan Task Registry for notify_pending tasks
         → found → reply Telegram → UPDATE notified
         (Gateway checks once on each Telegram poll, no extra timer needed)

Determining notified: notified_at IS NOT NULL, not based on whether Pub/Sub was received
```

## 6. Message Classifier (Two-Stage)

### First Layer: Rule-Based Fast Interception

```python
def classify_fast(text: str) -> str | None:
    """Rule interception, high-certainty cases return directly"""
    if text.startswith("/"):
        return "command"

    # Dangerous operations (require human confirmation)
    danger = ["rollback", "delete", "revoke", "release", "deploy"]
    if any(kw in text.lower() for kw in danger):
        return "dangerous"

    # Explicit query patterns
    query_patterns = [
        r"(status)\s*(how|what|check|view)",
        r"(how many)\s*(node|task)",
        r"(list|show)",
    ]
    for p in query_patterns:
        if re.search(p, text, re.I):
            return "query"

    return None  # Uncertain, pass to second layer
```

### Second Layer: LLM Intent Parsing (Future Integration)

```python
def classify_llm(text: str, context: dict) -> dict:
    """LLM parses intent, currently using simple rules as placeholder"""
    # Phase 1: Keyword fallback
    task_kw = ["help me", "write", "change", "fix", "create", "implement", "optimize",
               "test", "add"]
    if any(kw in text for kw in task_kw):
        return {"intent": "execute", "risk": "low", "needs_workspace": True}

    # Phase 2: Replace with LLM call later
    # return llm_classify(text, context)

    return {"intent": "chat", "risk": "none", "needs_workspace": False}
```

## 7. Notifications Belong to Task (Not Project View)

```
Record chat_id when task is created:
  task.chat_id = 7848961760

When task completes:
  Regardless of which project user is currently bound to
  Send directly back to task.chat_id

/menu shows unread per project:
  ┌──────────────────────────────┐
  │ [>> amingClaw]     2 unread  │  ← Has completed but unviewed tasks
  │ [   toolboxClient] 0 unread  │
  └──────────────────────────────┘
```

## 8. Executor Permission Boundary

### workspace allowlist

```python
# Each project can only access its own repo path
PROJECT_WORKSPACES = {
    "amingClaw": "C:/Users/z5866/Documents/amingclaw/aming_claw",
    "toolboxClient": "C:/Users/z5866/Documents/Toolbox/toolBoxClient",
}

def validate_workspace(project_id, task):
    allowed = PROJECT_WORKSPACES.get(project_id)
    if not allowed:
        raise RuntimeError(f"No workspace configured for {project_id}")
    # backends.py already has is_sensitive_path check
```

### tool policy (Revision #4: Structured Command Policy)

```python
# Phase 1: String rules (current)
# Phase 2: Structured command capability model (future upgrade)

TOOL_POLICY = {
    "auto_allow": [
        {"program": "git", "args": ["diff", "status", "log", "show", "blame"],
         "write": False, "network": False},
        {"program": "python", "args": ["-m", "unittest"],
         "write": False, "network": False},
        {"program": "pytest", "write": False, "network": False},
        {"program": "npm", "args": ["test"], "write": False, "network": False},
    ],
    "needs_approval": [
        {"program": "git", "args": ["push", "reset", "rebase"],
         "write": True, "network": True,
         "reason": "Modifies remote or history"},
        {"program": "docker", "args": ["compose"],
         "write": True, "network": True,
         "reason": "Controls infrastructure"},
        {"program": "bash", "args": ["deploy-governance.sh"],
         "write": True, "network": True,
         "reason": "Production deployment"},
    ],
    "always_deny": [
        {"pattern": "rm -rf /", "reason": "Destructive"},
        {"pattern": "DROP TABLE", "reason": "Database destruction"},
        {"pattern": "format C:", "reason": "Disk format"},
    ],
}

# Validation logic
def check_command_policy(cmd: list[str], project_id: str) -> str:
    """Returns: 'allow' | 'approve' | 'deny'"""
    program = cmd[0] if cmd else ""
    for rule in TOOL_POLICY["always_deny"]:
        if rule["pattern"] in " ".join(cmd):
            return "deny"
    for rule in TOOL_POLICY["needs_approval"]:
        if program == rule["program"]:
            if any(a in cmd for a in rule.get("args", [])):
                return "approve"
    for rule in TOOL_POLICY["auto_allow"]:
        if program == rule["program"]:
            return "allow"
    return "approve"  # Default requires approval
```

## 9. Long Task Progress Heartbeat

```python
# Executor periodically reports progress during execution
def report_progress(task_id, project_id, phase, percent, message):
    with DBContext(project_id) as conn:
        conn.execute(
            """UPDATE tasks SET metadata_json = json_set(
                 metadata_json,
                 '$.progress_phase', ?,
                 '$.progress_percent', ?,
                 '$.progress_message', ?,
                 '$.progress_at', ?
               ) WHERE task_id = ?""",
            (phase, percent, message, utc_iso(), task_id)
        )
        conn.commit()

# Phase enum
PHASES = [
    "planning",        # Analyzing task
    "coding",          # Writing code
    "testing",         # Running tests
    "reviewing",       # Self-review
    "waiting_human",   # Waiting for human confirmation
    "finalizing",      # Wrapping up
]

# When user queries progress
# GET /api/runtime/{pid} → active_tasks[0].progress
# → "coding (60%) — modified 3 files, running unit tests"
```

## 10. Hard Role Responsibility Constraints (Toolbox Lesson)

### 10.1 Role Definitions

| Role | Only Does | Does Not Do |
|------|-----------|-------------|
| **Coordinator** | Receive instructions → dispatch → monitor → report | ❌ No reading code, writing code, analyzing requirements, running tests |
| **PM** (future) | Requirements analysis + design + acceptance criteria | ❌ No writing code |
| **Dev** | Code implementation + unit testing | ❌ No requirements analysis, no QA |
| **Tester** | Run tests + generate test reports | ❌ No modifying code |
| **QA** | Real environment E2E acceptance | ❌ No modifying code |
| **Gatekeeper** | Audit + alignment + correction + adjudication | ❌ No modifying files, dispatching agents, running tests |

### 10.2 Coordinator Code Modification Limit

```
"Minor edits" Coordinator is allowed (max 2 per task):
  - Modify config files (docker-compose, nginx.conf, .env)
  - Modify documentation (docs/, README)
  - Modify acceptance graph

Over 2 code modifications → auto-trigger Gatekeeper role collapse check
```

### 10.3 Code Fix ≠ verify:pass

```
After a node is marked qa_pass, if code is modified:
  → Node auto-downgrades to testing (not pending)
  → Must go through tester → qa verification again
  → Cannot be skipped

Implementation:
  On verify-update check: the node's primary/secondary files
  Whether there are git changes since last qa_pass
  If yes → block: "Node L1.3 files changed since qa_pass, re-verify required"
```

## 11. Gatekeeper Design (Toolbox Memory Isolation Model)

### 11.1 Gatekeeper Trigger Points (Revision #5: Shift Errors Left, Don't Pile on Release)

```
Principle: Shift checks as far left as possible to the step where errors occur. release-gate only does final non-bypassable checks.
```

| Trigger Point | Timing | Check Content | Block Level |
|---------------|--------|--------------|-------------|
| G-coverage | **verify-update (t2_pass/qa_pass)** | Are node primary files all covered in graph | Reject advancement |
| G-artifacts | **verify-update (qa_pass)** | Are docs/test files complete (including auto-inference) | Reject advancement |
| G-role | **When coord modifies code >= 2 times** | Role collapse check | Warning |
| G-file-change | **On verify-update** | Were node primary files changed after qa_pass? | Auto-downgrade to testing |
| G-release | **On release-gate** | Final check: coverage-check pass within last 1h + all green | Block release |

```
Shift-left error chain:

  Modify code → verify-update
            ├── G-coverage: Files have node coverage? (shifted left)
            ├── G-file-change: Files changed after qa_pass? (shifted left)
            └── G-artifacts: Docs/tests complete? (shifted left)
                      ↓
            All pass → allow advancement
                      ↓
  release-gate
            └── G-release: Only checks "has someone run coverage-check and passed"
                          Does not re-check content already shifted left
```

### 11.2 Gatekeeper Memory Isolation

```
Gatekeeper checks only receive:
  ✅ acceptance-graph current state (nodes + statuses)
  ✅ task-log (role instances + statuses)
  ✅ Original user instruction (one sentence)
  ✅ Current phase transition direction

Gatekeeper does not receive:
  ❌ Coordinator's context (recent_messages, decisions)
  ❌ Debug context, error logs, code diffs
  ❌ Multi-iteration history
  ❌ Inter-role conversation content

Reason: After long execution, Coordinator accumulates sunk costs,
     leading to "good enough" compromise mentality.
     Gatekeeper doesn't know how many debug rounds occurred,
     only checks if nodes are all green, naturally immune to drift.
```

### 11.3 Governance vs Runtime Responsibility Boundary

```
Governance (static rules):
  ├── Node state machine (who can make which transitions)
  ├── Gate policy (inter-node dependencies)
  ├── Coverage-check (file → node mapping)
  ├── Artifacts check (doc/test completeness)
  ├── Gatekeeper checks (pre-release global validation)
  └── Release profile (release scope)

Runtime (dynamic state):
  ├── Who is running (worker_pid, lease)
  ├── What task is running (task_id, prompt, phase, percent)
  ├── Progress (heartbeat + progress)
  ├── Which results need notification (notify_pending)
  └── Orphan process detection (pid alive check)

coverage-check belongs to Governance, not Runtime.
Governance auto-triggers coverage-check on phase transitions.
```

## 12. Process Lifecycle Management (Toolbox Lesson)

### 12.1 PID Tracking

```python
# Record PID when Executor starts CLI process
def run_with_pid_tracking(task_id, project_id, cmd):
    proc = subprocess.Popen(cmd, ...)

    # Record to Task Registry
    with DBContext(project_id) as conn:
        conn.execute(
            """UPDATE tasks SET metadata_json = json_set(
                 metadata_json, '$.worker_pid', ?, '$.worker_started', ?
               ) WHERE task_id = ?""",
            (proc.pid, utc_iso(), task_id)
        )
        conn.commit()

    return proc
```

### 12.2 Startup Orphan Scan

```python
def cleanup_orphan_processes():
    """Clean up orphan processes on Executor startup"""
    for project in list_projects():
        with DBContext(project["project_id"]) as conn:
            stale = conn.execute(
                """SELECT task_id, json_extract(metadata_json, '$.worker_pid') as pid
                   FROM tasks WHERE status IN ('claimed','running')"""
            ).fetchall()

            for row in stale:
                pid = row["pid"]
                if pid and not is_process_alive(pid):
                    # Process dead but status still running → re-queue
                    conn.execute(
                        "UPDATE tasks SET status='queued' WHERE task_id=?",
                        (row["task_id"],)
                    )
            conn.commit()
```

### 12.3 Process Cleanup After Task Completion

```python
def cleanup_after_task(task_id, project_id):
    """Clean up all related processes after task completion"""
    with DBContext(project_id) as conn:
        task = task_registry.get_task(conn, task_id)
        pid = task.get("metadata", {}).get("worker_pid")
        if pid and is_process_alive(pid):
            kill_process_tree(pid)
```

## 13. Implementation Roadmap (Final)

### P0: Stabilize Single-Role Single-Task

| Step | Content | Deliverable |
|------|---------|-------------|
| 1 | Token model simplification (remove refresh/access, project_token no expiry) | token_service.py deprecated |
| 2 | Task Registry state machine (dual field: execution + notification) | task_registry.py |
| 3 | Atomic file delivery (DB first → file second + fencing token) | Gateway + Executor |
| 4 | Executor writes persistent state on completion (notification_status=pending) | executor.py |
| 5 | Persistent notifications + re-delivery (Gateway poll checks notification_status=pending) | gateway.py |
| 6 | Cancel / retry / timeout | task_registry + executor |
| 7 | Progress heartbeat (phase+percent) | executor |
| 8 | PID tracking + orphan scan (toolbox lesson) | executor |

### P1: User Experience

| Step | Content |
|------|---------|
| 9 | Message classifier (two-stage: rules + LLM) |
| 10 | Runtime projection API (read-only Task Registry) |
| 11 | /menu runtime status + unread notifications |
| 12 | Project switch context auto save/load |
| 13 | Notification belongs to chat_id (cross-project notification) |
| 14 | Gateway as token proxy (CLI session does not need to manage tokens) |

### P2: Multi-Role Collaboration

| Step | Content |
|------|---------|
| 15 | Hard role responsibility constraints (coord code modification limit) |
| 16 | Role context isolation (per-role context key) |
| 17 | Gatekeeper memory isolation (only sees acceptance graph + task-log) |
| 18 | Code fix → auto node downgrade (file changes after qa_pass) |
| 19 | Role conflict governance (workspace lock + resource scope) |
| 20 | Executor workspace allowlist + structured tool policy |
| 21 | Role handoff protocol (dev → tester → qa auto-flow) |

### P3: Intelligence

| Step | Content |
|------|---------|
| 22 | LLM intent classifier (replace keywords) |
| 23 | Auto task decomposition (large task → sub-task DAG) |
| 24 | Context Assembly driven smart replies |
| 25 | PM role (requirements analysis + design) |
