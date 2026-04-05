# Tester Role Specification

> **Canonical document** for the Tester role in the Aming Claw governance pipeline.

## Role Definition

The Tester is responsible for running automated tests (T1 unit + T2 integration) and marking acceptance graph nodes from `pending` to `t2_pass`. The Tester operates within the auto-chain pipeline as the stage after Dev.

## Responsibilities

1. **Run automated tests** — Execute pytest test suites against changed code
2. **Verify test evidence** — Ensure test reports meet evidence requirements
3. **Mark T2-pass** — Update acceptance graph nodes via verify-update API
4. **Report failures** — Mark nodes as failed with error evidence when tests fail

## State Transitions

```
PENDING ──→ TESTING ──→ T2_PASS
   │            │
   │            ↓
   └──────→ FAILED
```

The Tester **cannot**:
- Mark nodes as `qa_pass` (QA role only)
- Skip T2 testing to go directly to QA
- Waive nodes (Coordinator only)

## Auto-Chain Integration

In the auto-chain pipeline, the Test stage:
1. Receives task from Dev stage completion
2. Executor claims and runs the test task
3. Test task executes pytest with the verification command from PM's PRD
4. Results reported via `task_complete` with structured test_report
5. T2 Pass Gate checks: `test_report` is dict, `passed > 0`, `failed == 0`
6. On gate pass → QA stage task created automatically

## Test Execution

### Standard Test Run

```bash
# Run full test suite
pytest agent/tests/ -v

# Run specific verification tests (from PM PRD)
pytest agent/tests/test_specific.py -v
```

### Test Report Format

The test report must be a structured dict (not a string):

```json
{
  "test_report": {
    "tool": "pytest",
    "passed": 162,
    "failed": 0,
    "summary": "162 tests passed, 0 failed"
  }
}
```

**Common error:** Submitting `test_report` as a string instead of a dict causes the T2 Pass Gate to reject.

## API Operations

### Mark T2-Pass

```json
POST /api/wf/{pid}/verify-update
Header: X-Gov-Token: gov-<tester-token>
Header: Idempotency-Key: tester-001-L0.1-t2-20260322

{
  "nodes": ["L0.1", "L0.2"],
  "status": "t2_pass",
  "evidence": {
    "type": "test_report",
    "tool": "pytest",
    "summary": {
      "passed": 162,
      "failed": 0,
      "exit_code": 0
    },
    "artifact_uri": "logs/test-run-20260322.json"
  }
}
```

### Mark Failed

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

## Evidence Requirements

| Transition | Evidence Type | Required Fields |
|------------|--------------|-----------------|
| pending → t2_pass | `test_report` | `summary.passed > 0`, `summary.exit_code == 0` |
| * → failed | `error_log` | `summary.error` or `artifact_uri` |
| failed → pending | `commit_ref` | `summary.commit_hash` (7-40 hex chars) |

## Typical Workflow

```
1. GET  /api/wf/{pid}/summary              ← See which nodes are pending
2. GET  /api/mem/{pid}/query?kind=failure_pattern   ← Check known failures
3. (Run tests — pytest agent/tests/ -v)
4. POST /api/wf/{pid}/verify-update        ← Mark T2-pass or failed
```

## Setup

```
Header: X-Gov-Token: gov-<your-token>
Header: Content-Type: application/json
```

Send heartbeat every 60s:
```
POST /api/role/heartbeat
Body: {"project_id": "<pid>", "status": "idle"}
```

## Error Reference

| HTTP Status | Error Code | Action |
|-------------|-----------|--------|
| 400 `invalid_evidence` | Evidence fields wrong | Check evidence type + summary |
| 403 `gate_unsatisfied` | Upstream not passed | Ensure upstream nodes are T2-pass first |
| 403 `forbidden_transition` | Illegal state change | Cannot skip T2; cannot mark QA-pass |

## When Governance Is Unreachable

| Operation | Behavior |
|-----------|----------|
| verify-update | Block and wait (max 120s) — do NOT mark status manually |
| mem/query | Return empty, do not block work |
