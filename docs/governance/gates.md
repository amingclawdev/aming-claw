# Governance Gates

> **Canonical governance topic document** — Gate checks that control stage transitions in the auto-chain.
> Last updated: 2026-04-05 | Phase 2 Documentation Consolidation

## Overview

Gates are validation checkpoints in the auto-chain pipeline. Each gate runs between stage transitions and must pass before the next stage can begin. Gates enforce quality, completeness, and consistency throughout the governance pipeline.

## Gate Types

### 1. Version Gate

**Purpose:** Ensures git state is synchronized with governance DB.

**Checks:**
- `chain_version` in governance DB matches current git HEAD
- Dirty files check (files modified but not committed)

**Behavior:**
- Runs at every stage transition
- `.claude/` paths are filtered from dirty files (D5 fix)
- **Downgraded to warning-only** (D3 fix) — version mismatch logs a warning but does not block auto-chain
- Executor syncs git HEAD to DB every 60s (only on change)

**API:** `GET /api/version-check/{project_id}`

**Response:**
```json
{
  "ok": true,
  "chain_version": "abc1234",
  "head": "abc1234",
  "dirty": false,
  "dirty_files": []
}
```

### 2. PM Gate (Doc Impact Gate)

**Purpose:** Validates PM output contains required PRD fields.

**Checks:**
- `target_files` — non-empty list of files to modify
- `verification` — string describing verification method
- `acceptance_criteria` — non-empty list of criteria

**Triggers:** PM → Dev transition

**On failure:** Retry PM task created with missing field details in prompt.

**Example valid output:**
```json
{
  "target_files": ["agent/governance/server.py"],
  "verification": "pytest agent/tests/test_server.py -v",
  "acceptance_criteria": [
    "AC1: Health endpoint returns 200",
    "AC2: Task list includes new fields"
  ]
}
```

### 3. Checkpoint Gate (Dev Gate)

**Purpose:** Validates Dev output matches PM scope.

**Checks:**
- `changed_files` — non-empty list in result
- Files actually exist in `git diff` output
- Files are within `target_files` scope from PM PRD

**Triggers:** Dev → Test transition

**On failure:** Retry Dev task created with checkpoint failure reason.

### 4. T2 Pass Gate (Test Gate)

**Purpose:** Validates test results indicate all tests pass.

**Checks:**
- `test_report` is a dict (not a string)
- `test_report.passed > 0` — at least one test ran
- `test_report.failed == 0` — no test failures

**Triggers:** Test → QA transition

**On failure:** Retry Test task (or Dev task if failures indicate code issues).

**Example valid output:**
```json
{
  "test_report": {
    "tool": "pytest",
    "passed": 162,
    "failed": 0,
    "summary": "162 tests passed"
  }
}
```

### 5. QA Pass Gate

**Purpose:** Validates QA review approved all acceptance criteria.

**Checks:**
- `recommendation == "qa_pass"` — explicit approval
- All entries in `criteria_results` have `passed: true`

**Triggers:** QA → Gatekeeper transition

**On failure:** Retry QA task (or Dev task if criteria indicate code defects).

**Example valid output:**
```json
{
  "recommendation": "qa_pass",
  "criteria_results": [
    {"criterion": "AC1", "passed": true, "evidence": "..."},
    {"criterion": "AC2", "passed": true, "evidence": "..."}
  ]
}
```

## Coordinator Gate (G1-G7)

The coordinator has its own gate that validates all coordinator actions before execution:

| Rule | Check |
|------|-------|
| G1 | Only `reply_only` and `create_pm_task` are allowed actions |
| G2 | PM task prompt must be ≥ 50 characters |
| G3 | Unknown action types are rejected |
| G4 | Legacy `create_task` format forced to PM type |
| G5 | Context updates validated for expected keys |
| G6 | Actions referencing non-existent tasks rejected |
| G7 | Rate limits apply to PM task creation |

## Gate Failure Flow

```
Task completes
    │
    ▼
Gate check runs
    │
    ├── PASS → Create next stage task
    │
    └── FAIL → Log failure reason
                │
                ├── Create retry task (same stage)
                │
                └── Or escalate to previous stage
                    (e.g., test failure → retry dev)
```

## Implementation

Gates are implemented in `agent/governance/auto_chain.py`:

- `_gate_pm_check()` — PM gate
- `_gate_checkpoint_check()` — Dev checkpoint gate
- `_gate_t2_check()` — Test T2 pass gate
- `_gate_qa_check()` — QA pass gate
- `_gate_version_check()` — Version gate (warning-only)

Each gate function returns `(passed: bool, reason: str)`. The reason is logged to audit and included in retry task prompts.

## Known Issues

| Issue | Status | Workaround |
|-------|--------|------------|
| Version gate false blocks | Fixed (D3) | Downgraded to warning-only |
| Dirty workspace blocks | Fixed (D5) | `.claude/` paths filtered |
| Duplicate retry creation | Fixed (D4) | Dedup guards in auto_chain |
| DB lock after version-update | Known | Restart governance service |
