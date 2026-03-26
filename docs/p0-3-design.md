# P0-3 Design: Dev→Gatekeeper→Tester→QA→Merge Chain

## Problem

`handle_dev_complete` 曾只调用 Coordinator eval。Eval pass → 什么都不发生。
165 qa_pass 全部手动设置。没有 auto-chain。

> 注意：旧的 coordinator.py, executor.py, task_orchestrator.py 等 20 个 agent/ 模块已完全移除。
> Auto-chain 现在通过 governance server (port 40006) 的 task_registry 实现。

## Root Cause（历史问题，已通过架构重构解决）

1. 旧 Coordinator eval 是 self-review（同一 AI 评审自己的请求）
2. eval pass 后没有自动创建 test_task
3. test pass 后没有自动创建 qa_task
4. QA pass 后没有触发 gatekeeper

## New Process

```
User message
  │
  ├─ Coordinator (JUDGE + DISPATCHER)
  │   1. Evaluate: task? question? feedback?
  │   2. If task → create PM session
  │   3. If question → answer directly
  │   4. If ambiguous → ask user
  │
  ├─ PM (requirements analysis)
  │   Output: PRD + target_files + acceptance_criteria
  │
  ├─ Coordinator REVIEWS PM output (JUDGE)
  │   - Scope reasonable?
  │   - Needs user permission? (destructive/large/costly)
  │   - NEEDS PERMISSION → ask user, wait
  │   - REJECTED → back to PM
  │   - APPROVED → create dev_task
  │
  ├─ Dev (code in worktree dev/task-xxx)
  │
  ├─ Stage 1: Isolated Checkpoint Gatekeeper (~10s)
  │   Replaces Coordinator Eval (removes self-review bias)
  │   Input: git diff + target_files + acceptance_criteria ONLY
  │   No project context, no conversation history
  │   Checks:
  │     - target_files actually changed
  │     - no unrelated files modified
  │     - diff size reasonable (not empty, not huge)
  │     - syntax valid (py_compile / eslint)
  │   FAIL → write pitfall to memory → retry Dev (max 3)
  │   PASS ↓
  │
  ├─ Stage 2: Tester (auto-created)
  │   Runs unit tests + coverage on changed files
  │   FAIL → write test_failure to memory → retry Dev
  │   PASS ↓
  │
  ├─ Stage 3: QA (auto-created)
  │   Verifies in real environment
  │   FAIL → write pitfall to memory → retry Dev
  │   PASS ↓
  │
  └─ Stage 4: Full Gatekeeper
      Final merge gate
      PASS → merge dev/task-xxx to main
      FAIL → block, notify Observer
```

## 角色变更（旧 Coordinator → Governance Server）

> 旧的 coordinator.py 已删除。以下描述 governance server 如何承接这些职责。

| Stage | 旧架构 (coordinator.py) | 新架构 (governance server) |
|-------|--------|-------|
| Inbound eval | Coordinator Judge | Governance task_registry 路由 |
| PM dispatch | Coordinator Dispatch | Governance task_registry 创建 |
| PM output review | None (auto-pass) | Governance workflow: judge + user permission gate |
| Dev output eval | Coordinator Judge (self-review) | **Removed** → Isolated Gatekeeper (via governance) |
| Chain trigger | Broken (P0-3 bug) | Governance task_registry auto-chain |

## Context / Prompt Consumption

### Context Assembler Budget (tokens per role)

| Layer | coord | dev | pm | test | qa | gatekeeper |
|-------|-------|-----|----|------|----|------------|
| total | 6000 | 4000 | 4000 | 2000 | 2000 | 1000 |
| hard_context | 3000 | 2000 | 2000 | 1000 | 1000 | 0 |
| memory | 1500 | 1500 | 1500 | 500 | 500 | 0 |
| git | 500 | 500 | 0 | 500 | 0 | 500 |
| runtime | 1000 | 0 | 500 | 0 | 500 | 0 |

### System Prompt Structure (all roles)

```
{ROLE_PROMPT}                         ← from role_permissions.py

项目: {project_id}
工作目录: {workspace}          (dev only)
目标文件: {target_files}       (dev only)

当前上下文:
{
  "governance_summary": {...},   ← Layer 1: node statuses
  "conversation": [...],         ← Layer 2: recent messages
  "memories": [...],             ← Layer 3: dbservice top-3 search
  "runtime": {...},              ← Layer 4: active tasks
  "git_status": {...},           ← Layer 5: branch state
  "workspace": "C:/...",         ← Layer 6: resolved path (dev)
  "target_files": [...]          ← Layer 7: from PM (dev)
}

用户消息: {prompt}
请按照指定 JSON 格式输出你的决策。
```

Over budget? Trim order: conversation → memories → runtime

### Delivery

- System prompt → temp file → `--system-prompt-file`
- User prompt → stdin pipe
- Audit copy → Redis Stream `ai:prompt:{session_id}`

### Per-Role Input

**Coordinator**: governance_summary + conversation + memories + runtime → route decision

**PM**: governance_summary + conversation + memories + runtime → PRD output

**Dev**: governance_summary + memories + git_status + workspace + target_files → code changes

**Checkpoint Gatekeeper (isolated)**: git diff + target_files + acceptance_criteria ONLY (no context assembler)

**Tester**: governance_summary + memories(500) + git_status + parent_task changed_files → test results

**QA**: governance_summary + memories(500) + runtime + test_report → verification

## Memory Flow

### Two Channels (complementary)

| Channel | Speed | Scope | Survives task? |
|---------|-------|-------|---------------|
| Direct (prompt) | Immediate | This retry only | No |
| Memory (dbservice) | Next search | All future tasks | Yes |

### Write Triggers

```
Gatekeeper FAIL → dbservice write:
  type: "pitfall"
  content: "wrong files / empty diff / syntax error"
  scope: project_id

Tester FAIL → dbservice write:
  type: "test_failure"
  content: "test X failed: assertion Y, file Z line N"
  related_nodes: [L1.3]
  scope: project_id

QA FAIL → dbservice write:
  type: "pitfall"
  content: "change breaks real env: symptom X"
  scope: project_id

Dev SUCCESS → dbservice write:
  type: "pattern"
  content: "approach that worked for this class of problem"
  scope: project_id
```

### Read Flow (on retry)

```
context_assembler._fetch_memories(query=task.prompt, scope=project_id)
  → POST /knowledge/search {query, scope, limit:3}
  → returns top-3 semantically matched memories
  → injected into Dev system prompt under "memories" key
  → Dev sees pitfalls + test_failures from previous attempts
```

### Retry Enhancement

On Dev retry, prompt includes BOTH channels:

```
[Direct] rejection_history (last 5 iterations):
  - Iteration 1: "target_files not changed"
  - Iteration 2: "test_utils.py:45 assertion failed"

[Memory] related pitfalls (semantic search):
  - "py_compile check: always validate syntax before commit"
  - "agent/executor.py import order matters: utils before governance"
```

## Redis Stream Audit

Each AI session produces two stream entries in `ai:prompt:{session_id}`:

```
Entry 1 (type: prompt):
  session_id, role, project_id, workspace
  system_prompt_length, user_prompt (truncated 5K)
  created_at

Entry 2 (type: result):
  status (completed/failed/timeout)
  exit_code, elapsed_sec
  stdout (truncated 10K), stderr (truncated 2K)
  changed_files, completed_at
```

Query: `redis-cli -p 40079 XRANGE ai:prompt:ai-dev-xxx - +`

## Rollback

### Code Layer
- `pre_task_checkpoint()` before Dev → saves SHA
- On failure: `rollback_to_checkpoint(SHA)` → `git reset --hard`

### Governance Layer
- `create_snapshot(project_id)` before verify-update → saves version
- On failure: `POST /api/wf/{pid}/rollback {target_version}` → reverts all nodes

### Gap (to fix later)
- No auto-sync between code rollback and node rollback
- Snapshot is project-global (not per-node)
- Worktree orphans not auto-cleaned

## Codex Review Feedback (incorporated)

### 1. Auto-chain idempotency (P0)

Every `_trigger_*()` must check idempotency before creating a task.

**Idempotency key**: `{parent_task_id}:{stage}`

```python
# 现在在 governance server (task_registry) 中实现
def _trigger_tester(self, parent_task_id, changed_files, project_id):
    idem_key = f"{parent_task_id}:test"
    if self._check_idempotency(idem_key):
        log.info("test_task already created for %s, skip", parent_task_id)
        return None
    task = self._create_task(type="test_task", ...)
    self._store_idempotency(idem_key, task["task_id"], ttl=3600)
    return task
```

Uses `redis_client.check_idempotency()` / `store_idempotency()`.

Retry does NOT bypass idempotency — retry creates a NEW parent_task_id (via task_retry.py), so the chain restarts cleanly with a fresh idem_key.

### 2. Checkpoint Gatekeeper boundary (P0)

Explicitly documented: **hard gate only, not semantic judge**.

| Checkpoint Gatekeeper checks | Does NOT check |
|------------------------------|---------------|
| target_files changed? | Correctness of logic |
| Unrelated files modified? | Dependency impact |
| Diff empty or huge? | Requirement alignment |
| Syntax valid? (py_compile) | Runtime behavior |

Anything beyond mechanical checks → Tester/QA responsibility.

### 3. Memory dedup strategy (P0)

Use existing `MemoryWriteGuard` with these rules:

```python
def write_failure_memory(self, stage, failure_info, project_id, parent_task_id):
    entry = {
        "type": "test_failure" if stage == "tester" else "pitfall",
        "scope": project_id,
        "content": failure_info["summary"],
        "confidence": 0.9,
        "refId": f"{parent_task_id}:{stage}",  # Dedup anchor
        "sourceType": "auto_chain",
        "supersedes": failure_info.get("previous_memory_id"),  # Update, not append
    }
    # MemoryWriteGuard checks similarity > 0.85 → skip duplicate
    self._memory_guard.guarded_write(entry, project_id)
```

Rules:
- `refId = parent_task_id:stage` — same stage for same task always updates, never duplicates
- `supersedes` — retry N+1 replaces retry N's memory, not append
- `MemoryWriteGuard` similarity check (>0.85) catches cross-task duplicates
- 3 retries of same failure → 1 memory entry (updated), not 3

### 4. Rollback symmetry (P0)

**Auto-snapshot before verify-update:**

```python
# In _trigger_tester() or any stage that calls verify-update
snapshot_version = state_service.create_snapshot(conn, project_id)
task["_pre_verify_snapshot"] = snapshot_version

# On failure:
state_service.rollback(conn, project_id, task["_pre_verify_snapshot"])
rollback_to_checkpoint(task["_git_checkpoint"])
# Both layers now consistent
```

**State visibility contract:**
- After rollback, Observer sees: `code=checkpoint_SHA, nodes=snapshot_version`
- Both stored in task metadata → queryable via `/task/{id}`
- `/observer/report/{task_id}` includes both code and governance state

### 5. Global retry budget (P0)

```python
# Task metadata
{
    "total_attempts_budget": 6,     # Global max across ALL stages
    "total_attempts_used": 0,       # Incremented on each retry (any stage)
    "stage_attempts": {             # Per-stage tracking
        "checkpoint_gate": 0,
        "tester": 0,
        "qa": 0
    },
    "max_per_stage": 3              # Per-stage cap
}
```

Check before any retry:

```python
def _can_retry(self, task):
    if task["total_attempts_used"] >= task["total_attempts_budget"]:
        self._escalate_to_observer(task, "global budget exhausted")
        return False
    stage = task["current_stage"]
    if task["stage_attempts"].get(stage, 0) >= task["max_per_stage"]:
        self._escalate_to_observer(task, f"stage {stage} budget exhausted")
        return False
    return True
```

Worst case: 6 total attempts (e.g., gate:1 + test:2 + qa:3 = 6 → budget hit).
Not: gate:3 + test:3 + qa:3 = 9.

### 6. PM permission gate rules (P1)

```python
PM_PERMISSION_RULES = {
    "destructive": {
        "triggers": ["delete", "remove", "drop", "truncate", "overwrite", "migrate"],
        "description": "Destructive operation detected",
    },
    "large_scope": {
        "triggers": lambda prd: len(prd.get("target_files", [])) > 5,
        "description": "More than 5 target files",
    },
    "large_diff": {
        "triggers": lambda prd: prd.get("estimated_lines", 0) > 500,
        "description": "Estimated >500 lines changed",
    },
    "external_call": {
        "triggers": ["deploy", "publish", "push", "send", "notify", "api call"],
        "description": "External system interaction",
    },
    "long_running": {
        "triggers": lambda prd: prd.get("estimated_minutes", 0) > 30,
        "description": "Estimated >30 minutes execution",
    },
}
```

Coordinator checks PRD against rules → any match → ask user permission before dev_task.

## Implementation Priority (Codex-aligned)

### P0 must-do (this implementation)

| # | Item | 位置 |
|---|------|------|
| 1 | Auto-chain with idempotency keys | governance server (task_registry) |
| 2 | Stage state machine + parent_task binding | governance server (task_registry) |
| 3 | Global retry budget (total_attempts_budget=6) | governance server + executor-gateway |
| 4 | Memory write with dedup (refId + supersedes) | governance server |
| 5 | Rollback symmetry: auto-snapshot before verify + sync rollback | governance server (workflow) |
| 6 | Checkpoint Gatekeeper (hard gate, not semantic) | executor-gateway + governance server |
| 7 | Route new task types | executor-gateway |
| 8 | Tests | governance server tests |

### P1 important (follow-up)

| # | Item |
|---|------|
| 1 | PM permission gate rules (quantified) |
| 2 | Checkpoint Gatekeeper rejection reason standardization |
| 3 | Observer escalation payload (stage, diff, reason, memory, audit key) |

### P2 optimize (later)

| # | Item |
|---|------|
| 1 | Merge Gatekeeper change summary / impact info |
| 2 | Redis Stream query wrapper for Observer |
| 3 | Per-node snapshot + worktree orphan cleanup |

## Implementation: 实现位置

> 注意：以下旧文件已全部删除：`agent/task_orchestrator.py`, `agent/executor.py`, `agent/backends.py` 等。
> 对应功能现在由 governance server + executor-gateway 实现。

### auto_chain.py — 已实现

`agent/governance/auto_chain.py` 是自动链路调度器的核心实现。`task_registry.complete_task()` 在任务成功时调用 `auto_chain.on_task_completed()`，自动推进链路。

**链路定义 (`CHAIN` dict):**

| task_type | Gate 函数 | 下一阶段 | Prompt 构建 |
|-----------|----------|---------|------------|
| `pm` | `_gate_post_pm` — PRD 必须包含 target_files, verification, acceptance_criteria | `dev` | `_build_dev_prompt` |
| `dev` | `_gate_checkpoint` — 文件已修改且无 scope 外变更 | `test` | `_build_test_prompt` |
| `test` | `_gate_t2_pass` — 测试全部通过 | `qa` | `_build_qa_prompt` |
| `qa` | `_gate_qa_pass` — QA 推荐 qa_pass 或 qa_pass_with_fallback | `merge` | `_build_merge_prompt` |
| `merge` | `_gate_release` — 信任 merge 结果 | (终端) | `_trigger_deploy` → 调用 `deploy_chain.run_deploy()` |

**关键机制:**
- `MAX_CHAIN_DEPTH = 10` 防止无限循环
- Gate 失败时发布 `gate.blocked` 事件，返回 `{"gate_blocked": True, "stage": ..., "reason": ...}`
- 终端阶段（merge 后）自动调用 `deploy_chain.run_deploy()`；`deploy_chain.py` 对非 Docker 环境提供 `restart_local_governance()` 作为回退
- Task create/claim/complete 不再需要 `X-Gov-Token`

### 其他模块实现位置

| 功能 | 实现位置 |
|------|--------|
| Auto-chain 调度 + gate 验证 | `agent/governance/auto_chain.py` |
| `complete_task()` → `on_task_completed()` 调用 | `agent/governance/task_registry.py` |
| Checkpoint gatekeeper + idempotency | governance server (task_registry) |
| Memory-write-on-failure with refId/supersedes dedup | governance server (memory API) |
| Global retry budget check (`_can_retry()`) | governance server (task_registry) |
| Auto-snapshot before verify-update + sync rollback | governance server (workflow) |
| Route `checkpoint_gate_task` and `merge_gate_task` types | executor-gateway (port 8090) |
| Checkpoint gatekeeper role prompt (minimal, hard-gate only) | governance server (role_permissions) |
| Gatekeeper budget (git only: 1000 tokens, no memory) | governance server (context assembler) |
| Isolated gatekeeper session (diff-only prompt) | executor-gateway |
| Deploy 自动触发（含非 Docker 回退） | `agent/deploy_chain.py` |
| Tests | governance server + executor-gateway tests |

## Acceptance Criteria

1. Dev completes → checkpoint gatekeeper auto-triggers (not Coordinator eval)
2. Gatekeeper PASS → test_task auto-created (idempotent)
3. Tester PASS → qa_task auto-created (idempotent)
4. QA PASS → merge gatekeeper auto-triggered (idempotent)
5. Any FAIL → memory written (deduped via refId) + Dev retried with failure context
6. Max 3 retries per stage AND max 6 total → escalate to Observer
7. Each stage writes audit to Redis Stream
8. Rollback syncs code layer + governance layer (auto-snapshot)
9. Duplicate trigger of same stage for same parent → no-op (idempotency)
10. Checkpoint Gatekeeper has NO access to project context (isolation enforced)

## 变更记录
- 2026-03-26: auto_chain.py 实现完成，全链路 PM→Dev→Test→QA→Merge→Deploy 自动调度，含 gate 验证
- 2026-03-26: 旧 Telegram bot 系统完全移除（bot_commands, coordinator, executor 等 20 个模块），统一使用 governance API
