# Executor Evolution — Design Proposals

Status: **draft — awaiting review**
Date: 2026-03-30

---

## 1. Executor Crash Recovery + Orphan Cleanup

### Current State
- ServiceManager detects executor death → restarts ✅
- Does NOT kill child process tree → orphan Claude CLI processes ❌
- subprocess.run is synchronous in thread → executor death = orphan CLI

### Proposed Fix
```python
# service_manager.py — before restart
import subprocess as sp
sp.run(["taskkill", "/T", "/F", "/PID", str(dead_pid)],
       capture_output=True, timeout=10)
```

### Decision Point K (from pm-iteration.md)
Recommended: taskkill /T (Windows built-in, no dependency)

---

## 2. Process Pool for Parallel Execution

### Current State
Single executor process, single worker thread, one task at a time.

### Proposal: ExecutorPool with N slots

```
ExecutorPool
  ├── Slot 1: fast tasks (coordinator, ~20s) — sonnet
  ├── Slot 2: PM tasks (~60s) — sonnet/opus
  ├── Slot 3: dev tasks (~5min) — opus
  └── Slot 4: test/E2E tasks (~90s) — sonnet
```

### Architecture

```python
class ExecutorPool:
    def __init__(self, max_workers=4):
        self.slots = [WorkerSlot(i) for i in range(max_workers)]

    def claim_and_execute(self):
        """Claim one task per idle slot, matching slot type to task type."""
        for slot in self.slots:
            if slot.is_idle():
                task = self._claim_for_slot(slot)
                if task:
                    slot.execute_async(task)

    def _claim_for_slot(self, slot):
        """Claim task matching slot's preferred type."""
        # Slot 1 prefers coordinator/task
        # Slot 2 prefers pm
        # Slot 3 prefers dev
        # Slot 4 prefers test/qa
```

### Benefits
- Parallel: coordinator + PM + test run simultaneously
- E2E tests don't block normal tasks
- Independent crash isolation per slot
- Resource control: limit opus slots

### Changes Required
- ServiceManager manages N subprocess workers (or thread pool)
- task_registry.claim_task: accept type filter
- Pipeline config: per-slot model assignment
- Memory management: cap total claude.exe processes

### Priority
Deferred — current single-worker is sufficient for observer-mode debugging.
Implement when auto-flow needs throughput (multiple users or batch testing).

---

## 3. PM Chain Path — Skip Steps Based on Task Type

### Current State
auto-chain is fixed: PM → Dev → Test → QA → Merge. No way to skip stages.

### Problem Scenarios

| Scenario | Ideal Chain | Current Chain | Gap |
|----------|-------------|---------------|-----|
| Feature development | PM → Dev → Test → QA → Merge | Same | — |
| Test-only task | PM → Test → QA | PM → Dev → Test → QA → Merge | Dev unnecessary |
| Doc-only update | PM → Dev → Merge | PM → Dev → Test → QA → Merge | Test+QA unnecessary |
| Verification task | PM → Test → QA | Full chain | Dev unnecessary |
| Bug fix (low risk) | PM → Dev → Test → Merge | Full chain | QA unnecessary for trivial fix |

### Proposal: PM outputs `chain_path`

```json
{
  "target_files": [...],
  "chain_path": ["dev", "test", "qa", "merge"],  // normal
  // OR
  "chain_path": ["test", "qa"],                   // test-only
  // OR
  "chain_path": ["dev", "merge"],                 // doc/config change
}
```

### auto_chain Changes

```python
CHAIN = {
    "pm":    ("_gate_post_pm",    None,    "_build_next_prompt"),  # next_type from chain_path
    "dev":   ("_gate_checkpoint", None,    "_build_next_prompt"),
    "test":  ("_gate_t2_pass",    None,    "_build_next_prompt"),
    "qa":    ("_gate_qa_pass",    None,    "_build_next_prompt"),
    "merge": ("_gate_release",    None,    "_trigger_deploy"),
}

def _get_next_type(task_type, metadata):
    """Determine next stage from chain_path in metadata."""
    chain_path = metadata.get("chain_path", ["dev", "test", "qa", "merge"])
    try:
        idx = chain_path.index(task_type)
        if idx + 1 < len(chain_path):
            return chain_path[idx + 1]
    except ValueError:
        pass
    return None  # terminal
```

### Gate Implications
- If chain_path skips "test", _gate_checkpoint still runs on dev completion
- If chain_path skips "qa", _gate_t2_pass still runs on test completion
- Gates validate what's present, chain_path determines what's next

### PM E2E Test Scenarios

| ID | Scenario | PM Input | Expected chain_path | E2E Validation |
|----|----------|----------|---------------------|----------------|
| P1 | Feature dev | "add heartbeat timeout" | ["dev","test","qa","merge"] | dev task created |
| P4 | Test-only | "add tests for executor" | ["test","qa"] | test task created (skip dev) |
| P5 | Doc-only | "update architecture docs" | ["dev","merge"] | dev task created (skip test+qa) |
| P6 | Bug fix | "fix log.info deadlock" | ["dev","test","merge"] | dev task created (skip qa) |
| P7 | Verify node | "verify L4.37" | ["test","qa"] | test task created |

### Priority
Medium — implement after PM basic flow is stable. Current fixed chain works for most cases.
PM can use skip_reasons to indicate "test not needed" which observer can use to manually skip.

---

## 4. Multi-Project Process Pool with Isolation

### Problem
Current executor is single-project, single-thread. No isolation between projects.

### Architecture

```
ProjectPoolManager
  ├── aming-claw (Pool A)
  │     ├── Slot 1: coordinator/pm   — sonnet, max 1 concurrent
  │     ├── Slot 2: dev              — opus, max 1 concurrent
  │     └── Slot 3: test/qa/merge    — sonnet, max 1 concurrent
  │
  ├── yings-work (Pool B)
  │     ├── Slot 1: coordinator/pm
  │     └── Slot 2: dev/test/qa
  │
  └── Global Limits
        ├── max_total_claude_processes: 6
        ├── max_per_project: 3
        └── max_opus_concurrent: 2
```

### Isolation Requirements

| Dimension | Isolation Level | How |
|-----------|----------------|-----|
| Task queue | Per-project | task_registry already uses project_id |
| Memory/DB | Per-project | governance.db per project (already isolated) |
| Working directory | Per-project | workspace_registry maps project → path |
| CLI process | Per-slot | each slot runs its own subprocess.run |
| Model quota | Global | shared across projects (user's subscription) |
| Crash blast radius | Per-slot | one slot crash ≠ other slots/projects affected |

### ProjectPool Config

```yaml
# pool_config.yaml (new file)
global:
  max_total_processes: 6
  max_opus_concurrent: 2

projects:
  aming-claw:
    max_slots: 3
    slot_types:
      - {roles: [coordinator, pm], model: sonnet, max: 1}
      - {roles: [dev], model: opus, max: 1}
      - {roles: [test, qa, merge], model: sonnet, max: 1}
  yings-work:
    max_slots: 2
    slot_types:
      - {roles: [coordinator, pm], model: sonnet, max: 1}
      - {roles: [dev, test, qa, merge], model: sonnet, max: 1}
```

### Key Design Decisions

| ID | Question | Options | Notes |
|----|----------|---------|-------|
| L | Pool per project or shared pool? | **per-project** | isolation > utilization; projects may have different workspace/config |
| M | Slot assignment: static or dynamic? | **static by role type** | predictable resource usage; avoid opus contention |
| N | How to register new project? | API call + config entry | `POST /api/pool/register {project_id, slots}` |
| O | What happens when all slots busy? | Task stays queued | observer_mode can hold; auto-mode waits for slot |
| P | E2E tests: separate slot or shared? | **shared test slot** | E2E runs as type=test; doesn't need special handling |

### Implementation Scope

```
Phase 1: Single project, multi-slot (current project only)
  - Replace single executor with 3 worker threads
  - Each thread claims by type filter
  - Shared ServiceManager monitors all threads

Phase 2: Multi-project pool manager
  - ProjectPoolManager registers projects on startup
  - Each project gets its own pool with configured slots
  - Global resource limiter (max opus, max total)

Phase 3: Dynamic scaling
  - Pool manager adjusts slots based on queue depth
  - Idle projects release slots
  - Priority queuing across projects
```

### Changes Required

| File | Change |
|------|--------|
| `agent/service_manager.py` | Manage N worker threads/processes per project |
| `agent/executor_worker.py` | Accept type filter in claim_task; run in thread pool |
| `agent/governance/task_registry.py` | claim_task: add type_filter param |
| `agent/mcp/server.py` | Pool config API, per-project scale control |
| `agent/mcp/tools.py` | executor_scale per project per slot_type |
| New: `agent/pool_config.yaml` | Pool configuration |
| New: `agent/project_pool_manager.py` | Multi-project pool orchestration |

---

## 5. Implementation Priority

```
Phase A (done):  PM basic flow verified ✅ (58.9s, gate pass, dev created)
Phase B (next):  Orphan cleanup (taskkill /T) — small fix, high impact
Phase C (next):  Chain path — PM outputs chain_path to skip stages
Phase D (later): Single-project multi-slot — 3 parallel workers per project
Phase E (future): Multi-project pool manager — full isolation + global limits
```
