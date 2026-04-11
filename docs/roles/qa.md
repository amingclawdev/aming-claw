# QA Role Specification

> **Canonical document** for the QA role in the Aming Claw governance pipeline.

> **2026-04-07 (B10):** Dev tasks now fail fast on worktree creation failure instead of falling back to main workspace. QA should verify that failed dev tasks with worktree errors are properly retried by auto-chain.

> **2026-04-11 (B24):** QA output must now include a `recommendation` field with value in `{qa_pass, reject, merge_pass}`. Missing or invalid recommendation causes immediate task failure with `structured_output_invalid` error instead of silent pass-through.

## Role Definition

The QA agent is responsible for end-to-end verification, acceptance criteria review, and marking acceptance graph nodes from `t2_pass` to `qa_pass`. QA operates within the auto-chain pipeline as the stage after Test.

## Responsibilities

1. **Review code changes** — Verify implementation matches PM's acceptance criteria
2. **Run E2E tests** — Execute end-to-end verification (automated or manual)
3. **Validate acceptance criteria** — Check each criterion with evidence
4. **Mark QA-pass** — Update acceptance graph nodes via verify-update API
5. **Report failures** — Mark nodes as failed with detailed evidence when criteria not met

## State Transitions

```
T2_PASS ──→ QA_PASS
   │
   ↓
 FAILED
```

The QA agent **cannot**:
- Mark nodes as `t2_pass` (Tester role only)
- Mark nodes that haven't passed T2 (gate enforcement)
- Waive nodes (Coordinator only)

## Auto-Chain Integration

In the auto-chain pipeline, the QA stage:
1. Receives task from Test stage completion (T2 Pass Gate passed)
2. Executor claims and runs the QA task
3. QA task reviews changed files against acceptance criteria
4. Results reported via `task_complete` with structured recommendation
5. QA Pass Gate checks: `recommendation == "qa_pass"`, all criteria passed
6. On gate pass → Gatekeeper stage task created automatically

## QA Result Format

```json
{
  "recommendation": "qa_pass",
  "criteria_results": [
    {
      "criterion": "API returns correct response format",
      "passed": true,
      "evidence": "Verified via curl — response matches schema"
    },
    {
      "criterion": "Error handling covers edge cases",
      "passed": true,
      "evidence": "Test test_error_handling passes with 5 edge cases"
    }
  ]
}
```

**Gate requirement:** `recommendation` must be exactly `"qa_pass"` and all criteria must have `passed: true`.

## API Operations

### Mark QA-Pass

```json
POST /api/wf/{pid}/verify-update
Header: X-Gov-Token: gov-<qa-token>

{
  "nodes": ["L0.1"],
  "status": "qa_pass",
  "evidence": {
    "type": "e2e_report",
    "tool": "playwright",
    "summary": {
      "passed": 14,
      "failed": 0
    },
    "artifact_uri": "test/main-flow.spec.js"
  }
}
```

### Manual Acceptance (UI/Scheduled Task Nodes)

When a node requires human verification:

```json
{
  "type": "e2e_report",
  "producer": "qa-agent",
  "tool": "manual_e2e",
  "summary": {
    "passed": 1,
    "failed": 0,
    "manual_verified": true,
    "verified_by": "human",
    "verification_method": "telegram_message_test",
    "notes": "Sent test message, reply correct, ACK normal, no duplicate."
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
    "summary": {"error": "Acceptance criterion 3 not met — missing validation"},
    "artifact_uri": "logs/qa-review-20260322.log"
  }
}
```

## Evidence Requirements

| Transition | Evidence Type | Required Fields |
|------------|--------------|-----------------|
| t2_pass → qa_pass | `e2e_report` | `summary.passed > 0` |
| * → failed | `error_log` | `summary.error` or `artifact_uri` |
| failed → pending | `commit_ref` | `summary.commit_hash` (7-40 hex chars) |

## Typical Workflow

```
1. GET  /api/wf/{pid}/summary              ← Confirm T2-pass nodes exist
2. GET  /api/wf/{pid}/node/{nid}           ← Review specific node evidence
3. (Review code changes + run E2E verification)
4. POST /api/wf/{pid}/verify-update        ← Mark QA-pass or failed
```

## QA Criteria Review Checklist

When reviewing a dev task's output:

1. ✅ All acceptance criteria from PM PRD addressed
2. ✅ Changed files match target_files scope
3. ✅ No deprecated references or patterns introduced
4. ✅ Tests pass (T2 already verified, but spot-check)
5. ✅ Code quality acceptable (no obvious bugs or regressions)
6. ✅ Documentation updated if required

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
| 403 `gate_unsatisfied` | T2 not passed | Ensure node has T2-pass before QA |
| 403 `forbidden_transition` | Illegal state change | Cannot skip T2-pass requirement |
| 403 `scope_violation` | Out of scope | Contact Coordinator |

## When Governance Is Unreachable

| Operation | Behavior |
|-----------|----------|
| verify-update | Block and wait (max 120s) — do NOT mark status manually |
| mem/query | Return empty, do not block work |
