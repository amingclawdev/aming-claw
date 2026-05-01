# Dev Agent Guide

**Role:** Write code, fix bugs, write development memory.
**Cannot do:** Mark T2-pass, QA-pass, assign roles.

> **2026-04-07 (B10):** Dev tasks require a successful worktree. If worktree creation fails, the task fails immediately with `"worktree creation failed"` instead of running in the main workspace. Auto-chain will retry.

> **2026-04-11 (B24):** Retry dev tasks now receive a `SCOPE CONSTRAINT` line listing allowed files from `get_retry_scope()` (target_files + test_files + doc_impact.files + prior changed_files). Verification commands with shell operators (`&&`, `||`, `;`, `|`) are executed with `shell=True`.

---

## Setup

Receive your token at startup via environment variable or initialization message.

```
Header: X-Gov-Token: gov-<dev-token>
Header: Content-Type: application/json
```

Send heartbeat every 60s:
```
POST /api/role/heartbeat
Body: {"project_id": "<pid>", "status": "idle"}
```

---

## Task Workflow

### 1. Query memory before starting

```
GET /api/mem/{pid}/query?node=L3.7
GET /api/mem/{pid}/query?kind=pitfall
GET /api/mem/{pid}/search?q=stateService+timeout&top_k=5
```

### 2. Impact analysis (required before every task)

```
GET /api/wf/{pid}/impact?files=server/services/stateService.js,config.js
```

Response fields:
- `direct_hit` — Directly affected nodes
- `verification_order` — Topological sort order
- `test_files` — Test files that need to run
- `max_verify` — Maximum verification level required

### 3. Write code, commit

### 3.5 Self-validate dev output (recommended)

Before posting your dev result, you MAY pipe the JSON through the preflight validator:

```
python scripts/validate_stage_output.py --stage=dev --input=<output.json>
```

This catches phantom-create errors, missing required fields, mixed `parent_layer` types,
and unauthorized self-waivers locally before the server-side gate runs. The script exits
non-zero on FATAL violations and prints a human-readable diff.

Notes:
- The server validates your output regardless of whether you run the script — running it
  locally is purely an early-warning aid, not a bypass for any gate.
- Phantom-create errors against PM-declared `removed_nodes`
  (`PHANTOM_CREATE_FOR_DECLARED_REMOVED`) and PM-declared `unmapped_files`
  (`PHANTOM_CREATE_FOR_UNMAPPED_FILE`) are now FATAL: a `graph_delta.creates` entry that
  targets a node PM marked as removed, or a `primary` file PM marked as unmapped, will
  fail the gate even under `mode='warn'`.
- Background and design rationale: see
  [`docs/dev/proposal-stage-output-preflight-validator.md`](../dev/proposal-stage-output-preflight-validator.md).

### 4. Mark node status after fix

```json
POST /api/wf/{pid}/verify-update
Header: X-Gov-Token: gov-<dev-token>
Header: Idempotency-Key: dev-001-L3.7-fix-20260322

{
  "nodes": ["L3.7"],
  "status": "pending",
  "evidence": {
    "type": "commit_ref",
    "tool": "git",
    "summary": {
      "commit_hash": "a1b2c3d4e5f6a7b8"
    }
  }
}
```

### 5. Write memory after fix

```json
POST /api/mem/{pid}/write

{
  "module_id": "stateService",
  "kind": "pitfall",
  "content": "cp command unreliable in Windows worktree, use cat > instead",
  "applies_when": "Windows environment + git worktree",
  "related_nodes": ["L5.1", "L5.2"]
}
```

---

## Mark Failed

```json
POST /api/wf/{pid}/verify-update

{
  "nodes": ["L3.7"],
  "status": "failed",
  "evidence": {
    "type": "error_log",
    "summary": {"error": "Search timeout after 180s"},
    "artifact_uri": "logs/error-20260322.log"
  }
}
```

---

## Dev API Reference

| Operation | Method | Path |
|-----------|--------|------|
| Heartbeat | POST | `/api/role/heartbeat` |
| Search memory | GET | `/api/mem/{pid}/search?q=X&top_k=5` |
| Query memory | GET | `/api/mem/{pid}/query?module=X` |
| Write memory | POST | `/api/mem/{pid}/write` |
| Impact analysis | GET | `/api/wf/{pid}/impact?files=a.js,b.js` |
| Mark pending (after fix) | POST | `/api/wf/{pid}/verify-update` |
| Mark failed | POST | `/api/wf/{pid}/verify-update` |
| Pre-flight check | GET | `/api/wf/{pid}/preflight-check` |

---

## Memory Kinds

| Kind | Purpose |
|------|---------|
| `pattern` | Design patterns, architectural decisions |
| `pitfall` | Lessons learned, known issues |
| `workaround` | Temporary solutions |
| `decision` | Why A was chosen over B |
| `task_result` | Merge outcome summary (auto-written on merge) |
| `invariant` | Constraints that must not be violated |

---

## Gate Errors

If verify-update returns 403 `gate_unsatisfied`:
```json
{
  "error": "gate_unsatisfied",
  "message": "Gate prerequisites not met for L1.1",
  "details": {"unsatisfied_gates": [{"node_id": "L0.2", "reason": "..."}]}
}
```
**Action:** Complete upstream nodes first, then retry.

---

## Scope Restrictions

If you get 403 `scope_violation`, you are operating outside your assigned scope.
Contact Coordinator to expand scope or reassign the node.

---

## Error Reference

| HTTP Status | Error Code | Action |
|-------------|-----------|--------|
| 400 `invalid_evidence` | Evidence fields missing/wrong | Check evidence type + summary |
| 401 `token_expired` | Token expired | Contact Coordinator for new token |
| 403 `permission_denied` | Role cannot do this | Do not attempt bypass |
| 403 `gate_unsatisfied` | Upstream not passed | Complete upstream nodes first |
| 403 `forbidden_transition` | Illegal state change | Follow pending→testing→t2_pass order |
| 409 `conflict` | Concurrent write | Retry with Idempotency-Key |

---

## When Governance Is Unreachable

| Operation | Behavior |
|-----------|----------|
| verify-update | Block and wait (max 120s) — do NOT mark status manually |
| mem/write | Cache locally, push when service recovers |
| mem/query | Return empty, do not block work |
