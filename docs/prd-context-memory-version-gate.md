# PRD v5: Context Assembly + Memory + Multi-Project Version Gate

**Author:** Observer (Claude Code session)
**Date:** 2026-03-27
**Status:** Pending Review
**Priority:** P0 — Foundation infrastructure

---

## 1. Problem Statement

Three critical gaps in the current system:

1. **No context injection** — AI roles launch with minimal prompt (role description + JSON context). Coordinator doesn't know audit logs are in SQLite, tells users to check log files. Dev doesn't see past decisions or node status.

2. **No memory persistence** — Task completion results (decisions, test reports, pitfalls) are lost. Next session starts from scratch with no institutional knowledge.

3. **No workflow enforcement** — Observer/session can manually commit code bypassing the auto-chain (PM→Dev→Test→QA→Merge). In the current session, 12+ manual commits were made with zero gate checks, zero node updates, zero doc sync.

### Evidence

- Coordinator replied "check log files at shared-volume/codex-tasks/logs/" when audit logs are in governance.db
- 12 manual commits between `9226e4d` (last auto-merge) and `1a0965d` (current HEAD)
- 0 memory entries written despite 30+ successful task completions
- context_assembler.py exists with full implementation but ai_lifecycle.py never calls it

---

## 2. Architecture Overview

```
Telegram Message
  │
  ▼
Gateway ─── GET /api/version-check/{pid} ─── Governance Server
  │                                              │
  │         ┌────────────────────────────────────┘
  │         │  Governance:
  │         │    1. Read project_version table (chain_version)
  │         │    2. Call MCP Server GET /git-status
  │         │    3. Compare: HEAD == chain_version? dirty?
  │         │    4. Return result
  │         │
  │  ◄──────┘
  │
  ├── NOT OK → Reply to user: "⚠️ N manual commits, M dirty files"
  │            (0 token, no task created)
  │
  └── OK → Create coordinator task → Executor claim
                                        │
                                        ▼
                                  ContextAssembler.assemble()
                                    ├── memories (GET /api/mem)
                                    ├── node_summary (GET /api/wf)
                                    ├── runtime (GET /api/runtime)
                                    └── git_status (dev only)
                                        │
                                        ▼
                                  Claude CLI with full context
                                        │
                                        ▼
                                  Task completes
                                    ├── dev/test → write memory
                                    └── merge → UPDATE project_version
```

---

## 3. Changes

### 3.1 Multi-Project Version Gate

#### 3.1.1 Database Schema

**File:** `agent/governance/db.py`

```sql
CREATE TABLE IF NOT EXISTS project_version (
    project_id    TEXT PRIMARY KEY,
    chain_version TEXT NOT NULL,     -- git short hash from last auto-merge
    updated_at    TEXT NOT NULL,     -- ISO 8601
    updated_by    TEXT NOT NULL      -- "auto-chain" | "init" | "register"
);
```

#### 3.1.2 Project Init / Register

**File:** `agent/governance/project_service.py`

On `POST /api/init` and `POST /api/projects/register`:

```python
# After creating project, initialize version
conn.execute(
    "INSERT OR IGNORE INTO project_version VALUES (?, ?, ?, ?)",
    (project_id, current_git_head, utc_now(), "init")
)
```

For registered external projects, `current_git_head` is obtained from the workspace's git HEAD.

#### 3.1.3 MCP Server Git Status Endpoint

**File:** `agent/mcp/server.py`

MCP server runs on host machine (has git). Exposes HTTP :40020 for Docker services:

```
GET /git-status
Response:
{
    "head": "1a0965d",
    "dirty": true,
    "dirty_files": ["agent/gateway.py", "agent/server.py"],
    "timestamp": "2026-03-27T15:30:00Z"
}
```

Implementation:
- `git rev-parse --short HEAD` → head
- `git diff --name-only` → dirty_files (unstaged)
- `git diff --cached --name-only` → dirty_files (staged)
- dirty = len(dirty_files) > 0

Also exposed as MCP tool `version_check` for Observer to call directly.

#### 3.1.4 Governance Version Check API

**File:** `agent/governance/server.py`

```
GET /api/version-check/{project_id}
Response:
{
    "ok": false,
    "project_id": "aming-claw",
    "head": "1a0965d",
    "chain_version": "9226e4d",
    "dirty": true,
    "dirty_files": ["agent/gateway.py"],
    "commits_since_chain": 12,
    "message": "12 manual commits, 1 uncommitted file"
}
```

Logic:
1. Read `chain_version` from `project_version` table
2. Call MCP Server `GET http://host.docker.internal:40020/git-status`
3. Compare HEAD vs chain_version
4. Return combined result

If MCP server unreachable: return `{"ok": true, "message": "git status unavailable, proceeding"}` (fail-open to avoid blocking when MCP is down)

#### 3.1.5 Gateway Version Gate

**File:** `agent/telegram_gateway/gateway.py`

At the top of `handle_task_dispatch()`:

```python
def handle_task_dispatch(chat_id, text, route):
    project_id = route.get("project_id", "")

    # Version gate — code logic, 0 token
    try:
        check = gov_api("GET", f"/api/version-check/{project_id}")
        if not check.get("ok"):
            lines = ["⚠️ Workflow gate blocked:"]
            if check.get("commits_since_chain"):
                lines.append(f"  {check['commits_since_chain']} manual commits since last chain merge")
                lines.append(f"  HEAD={check['head']}  CHAIN_VERSION={check['chain_version']}")
            if check.get("dirty_files"):
                files = check["dirty_files"][:5]
                lines.append(f"  {len(check['dirty_files'])} uncommitted files: {', '.join(files)}")
            lines.append("\nSubmit changes through auto-chain to sync.")
            send_text(chat_id, "\n".join(lines))
            return
    except Exception as e:
        log.warning("Version check failed: %s (proceeding)", e)

    # ... create coordinator task
```

#### 3.1.6 Merge Updates Version

**File:** `agent/executor_worker.py` — `_execute_merge()`

After successful merge commit:

```python
new_hash = subprocess.check_output(
    ["git", "rev-parse", "--short", "HEAD"], cwd=self.workspace
).decode().strip()

# Update project_version via API
self._api("POST", f"/api/version-update/{self.project_id}", {
    "chain_version": new_hash,
    "updated_by": "auto-chain",
})
```

**File:** `agent/governance/server.py` — new endpoint:

```
POST /api/version-update/{project_id}
Body: {"chain_version": "abc123", "updated_by": "auto-chain"}
```

Only accepts `updated_by` = "auto-chain" or "init". Rejects manual updates.

#### 3.1.7 Anti-Tamper

AI cannot bypass the version gate by:

- **Editing VERSION via code:** No VERSION file exists. Data is in governance.db which AI cannot directly modify.
- **Calling /api/version-update manually:** Endpoint rejects unless `updated_by` = "auto-chain", and this is enforced server-side (not by AI honor system).
- **Committing to match chain_version:** Any commit changes HEAD, creating a new mismatch. The only way HEAD == chain_version is through the merge stage.
- **Modifying dirty check:** MCP server runs git commands directly, AI cannot intercept subprocess output.

---

### 3.2 Context Assembly Integration

#### 3.2.1 Wire ContextAssembler into AI Lifecycle

**File:** `agent/ai_lifecycle.py` — `_build_system_prompt()`

```python
def _build_system_prompt(self, role, prompt, context, project_id):
    from role_permissions import ROLE_PROMPTS
    from context_assembler import ContextAssembler

    # Static: role prompt
    role_prompt = ROLE_PROMPTS.get(role, "")

    # Dynamic: assembled context (memories, nodes, runtime)
    assembler = ContextAssembler(
        governance_url=os.getenv("GOVERNANCE_URL", "http://localhost:40000")
    )
    assembled = assembler.assemble(
        project_id=project_id,
        chat_id=context.get("chat_id", 0),
        role=role,
        prompt=prompt,
        workspace=context.get("workspace", ""),
        target_files=context.get("target_files", []),
    )

    assembled_str = json.dumps(assembled, ensure_ascii=False, indent=2)

    # Dev: workspace info
    workspace_info = ""
    if role == "dev":
        ws = context.get("workspace", "")
        if ws:
            workspace_info = f"Working directory: {ws}\nUse absolute paths.\n"

    return f"{role_prompt}\n\nProject: {project_id}\n{workspace_info}\n{assembled_str}\n\nTask: {prompt}"
```

#### 3.2.2 ContextAssembler Adaptation

**File:** `agent/context_assembler.py`

Current implementation fetches from governance API. Only change needed:
- Constructor accepts `governance_url` parameter
- Default to `http://localhost:40000` (nginx proxy)
- All internal `_fetch_*` methods use this URL

Token budgets (already defined in file):

| Role | Total Budget | Memories | Nodes | Runtime | Git |
|------|-------------|----------|-------|---------|-----|
| coordinator | 8000 | 2000 | 1000 | 500 | 0 |
| pm | 6000 | 2000 | 1000 | 500 | 0 |
| dev | 4000 | 1000 | 500 | 500 | 500 |
| tester | 3000 | 500 | 500 | 500 | 0 |
| qa | 3000 | 500 | 500 | 500 | 0 |

---

### 3.3 Coordinator Static Knowledge

**File:** `agent/role_permissions.py` — Append to coordinator's ROLE_PROMPTS entry

```
Available Governance APIs (use curl in Bash to query):

  Audit:    GET /api/audit/{pid}/log?limit=N
            Task audit log. Stored in governance.db (SQLite).
            Do NOT tell users to check log files.

  Memory:   GET /api/mem/{pid}/query?module=X&kind=Y
            Development memories. Stored in dbservice (:40002).

  Nodes:    GET /api/wf/{pid}/summary
            Node verification status summary.

  Tasks:    GET /api/task/{pid}/list
            Task list with status, type, and assigned worker.

  Health:   GET /api/health
            Service health, version, and PID.

  Graph:    GET /api/wf/{pid}/export?format=json
            Full acceptance graph with all nodes.

  Impact:   GET /api/wf/{pid}/impact?files=a.py,b.py
            File change impact analysis on nodes.

All data is in governance.db and dbservice. Never suggest checking
filesystem log files or shared-volume directories for task data.
```

---

### 3.4 Memory Write on Completion

**File:** `agent/executor_worker.py` — After successful task completion

```python
def _write_memory(self, task_type, result):
    """Write task outcome to development memory."""
    if task_type == "dev" and result.get("summary"):
        changed = result.get("changed_files", [])
        self._api("POST", f"/api/mem/{self.project_id}/write", {
            "module": changed[0] if changed else "general",
            "kind": "decision",
            "content": result["summary"],
        })

    elif task_type == "test" and result.get("test_report"):
        self._api("POST", f"/api/mem/{self.project_id}/write", {
            "module": "testing",
            "kind": "test_result",
            "content": json.dumps(result["test_report"]),
        })
```

Called after `_complete_task()` succeeds.

| Task Type | Write Memory | Content |
|-----------|-------------|---------|
| dev | ✅ | `{kind: "decision", content: summary}` |
| test | ✅ | `{kind: "test_result", content: report}` |
| coordinator | ❌ | — |
| pm | ❌ | — |
| qa | ❌ | — |
| merge | ❌ | — |

---

## 4. File Change Summary

| File | Action | Changes |
|------|--------|---------|
| `agent/governance/db.py` | Modify | Add `project_version` table schema |
| `agent/governance/server.py` | Modify | Add `GET /api/version-check/{pid}`, `POST /api/version-update/{pid}` |
| `agent/governance/project_service.py` | Modify | Init chain_version on project create/register |
| `agent/mcp/server.py` | Modify | Add HTTP :40020 with `/git-status` endpoint |
| `agent/mcp/tools.py` | Modify | Add `version_check` MCP tool |
| `agent/telegram_gateway/gateway.py` | Modify | Call `/api/version-check/{pid}` before creating task |
| `agent/ai_lifecycle.py` | Modify | Call ContextAssembler.assemble() in _build_system_prompt |
| `agent/context_assembler.py` | Modify | Accept governance_url parameter |
| `agent/role_permissions.py` | Modify | Add API knowledge to coordinator ROLE_PROMPT |
| `agent/executor_worker.py` | Modify | Memory write after completion + merge updates version |
| `Dockerfile.telegram-gateway` | Verify | Ensure requests library available |

---

## 5. Affected Nodes

| Node ID | Title | Current | Action |
|---------|-------|---------|--------|
| L15.1 | AI Session Lifecycle | qa_pass | → testing (context assembly added) |
| L15.8 | Context Assembly | qa_pass | → testing (wired into lifecycle) |
| L22.2 | Memory Write | qa_pass | → testing (executor writes memory) |
| L22.4 | Memory Query | qa_pass | → testing (context assembler queries) |
| L4.11 | Project Service | qa_pass | → testing (init writes version) |
| L4.15 | Governance Server | qa_pass | → testing (new API endpoints) |

After verification passes: testing → t2_pass → qa_pass

---

## 6. Documentation Updates

| Document | Section | Change |
|----------|---------|--------|
| `docs/architecture-v6-executor-driven.md` | Context Assembly | Add flow diagram: ContextAssembler → role prompt |
| `docs/architecture-v6-executor-driven.md` | Version Gate | New section: multi-project version, DB schema, anti-tamper |
| `docs/ai-agent-integration-guide.md` | Role Context | Table: what each role sees at startup |
| `docs/ai-agent-integration-guide.md` | Version Gate | Usage, fail-open behavior, bypass audit |
| `README.md` | Architecture diagram | Add MCP Server :40020 |
| `README.md` | Roles table | Update coordinator: "with API knowledge" |
| `README.md` | API Reference | Add version-check and version-update endpoints |

---

## 7. Verification

| # | Scenario | Expected Result |
|---|----------|-----------------|
| 1 | Manual commit → send Telegram message | Gateway replies "N manual commits", no task created |
| 2 | Edit file without commit → send Telegram | Gateway replies "N uncommitted files", no task created |
| 3 | Auto-chain merge → send Telegram | Normal: coordinator task created, AI responds |
| 4 | MCP server down → send Telegram | Fail-open: proceeds normally (logged warning) |
| 5 | AI modifies version via API | Rejected: server enforces updated_by = "auto-chain" |
| 6 | AI commits to match chain_version | Impossible: commit changes HEAD, new mismatch |
| 7 | Register new project | project_version initialized with workspace HEAD |
| 8 | Coordinator asked "check audit log" | Uses curl /api/audit, returns real DB data |
| 9 | Dev session starts | System prompt contains memories + node summary |
| 10 | Dev task completes | /api/mem/query returns new memory entry |
| 11 | version_check MCP tool | Observer gets ok/dirty/commits status |

---

## 8. Acceptance Criteria

- [ ] `project_version` table in governance.db, per-project chain_version
- [ ] `POST /api/init` initializes chain_version
- [ ] `POST /api/projects/register` initializes chain_version
- [ ] MCP server :40020 `/git-status` returns HEAD + dirty files
- [ ] `version_check` MCP tool available to Observer
- [ ] `GET /api/version-check/{pid}` combines DB + git data
- [ ] `POST /api/version-update/{pid}` rejects non-auto-chain callers
- [ ] Gateway blocks messages when version mismatch (0 token)
- [ ] Gateway blocks messages when dirty files exist (0 token)
- [ ] Gateway fail-open when MCP unreachable
- [ ] ContextAssembler.assemble() called before every AI session
- [ ] Token budget respected per role
- [ ] Coordinator ROLE_PROMPT contains API endpoint knowledge
- [ ] Dev/test write memory on successful completion
- [ ] Merge stage updates project_version
- [ ] 6 affected nodes updated testing → qa_pass
- [ ] 7 documentation sections updated

---

## 9. Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| MCP server crash blocks all Telegram | High | Fail-open: gateway proceeds if check unavailable |
| ContextAssembler API calls slow down session start | Medium | Budget limits cap total context; timeout 5s per fetch |
| Memory writes fail (dbservice down) | Low | Best-effort: log warning, don't fail the task |
| Multi-project version divergence | Medium | Each project independent; version check is per-project |
| Token budget too small for useful context | Medium | Monitor and tune ROLE_BUDGETS based on actual usage |
