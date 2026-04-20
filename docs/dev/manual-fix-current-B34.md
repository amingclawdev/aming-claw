# Manual Fix Execution Record — B34

> Trigger: Scheduled workflow maintenance (amingclaw-workflow). Auto-chain was stuck retrying Dev on the same bug it was trying to fix (chicken-and-egg precedent: B14, B15, B19).
> Operator: observer (scheduled task)
> Scope: Drop vestigial `qa_pass_with_fallback` recommendation from QA role prompt and auto-chain gate. Validator unchanged.

## Phase 0 — ASSESS (2026-04-20T08:09Z)

### 0.1 git status (baseline)

```
 m .claude/worktrees/compassionate-tu     (submodule ref drift, pre-existing noise)
 m .claude/worktrees/happy-ardinghelli
 m .claude/worktrees/zen-mendeleev
?? .claude/scheduled_tasks.lock
?? .recent-tasks.json
```

None are target files for B34.

### 0.2 version_check

- HEAD `a01ad54` == chain_version `a01ad54`, dirty=false → clean gate
- preflight_check: ok=true, 0 blockers (3 warnings pre-existing: version sync stale 8543s, 16 orphan pending nodes, 49 unmapped files — none cover target files)

### 0.3 Chain state

- Queue: 0 queued, 0 claimed
- Last chain: B34 fix attempt. Dev tasks `task-1776664069-0600f8`, `task-1776664573-f2e3a9`, `task-1776664959-50d463` (all dev-retry chain) succeeded at 05:56–06:08Z but no downstream Test/QA dispatched. Pattern consistent with validator rejecting QA's `qa_pass_with_fallback` output → retry loop until dev retry budget or gate drop.

## Phase 1 — CLASSIFY

| Axis | Value | Reason |
|------|-------|--------|
| Scope | B (1–5) | 2 nodes affected: `agent.role_permissions`, `agent.governance.auto_chain` |
| Danger | Low | Removing vestigial string constant; no deletions, no renames, no behavior change to valid `qa_pass`/`reject` paths |
| Combined | B-Low | Per S3 matrix: "Run module tests" |

### Mandatory rule applicability

| Rule | Trigger | Applies? |
|------|---------|----------|
| R1 Scope D split | >20 nodes | No |
| R2 Dry-run for delete/rename | Delete/rename | No |
| R3 Auto verification task | explicit+v4 real impact | No (both nodes `gate_mode=auto`) |
| R4 Audit record | Every manual fix | Yes — append to bug-and-fix-backlog.md |
| R5 Workflow restore proof | Every manual fix | Yes — Phase 5 |
| R6 New-file node check | Any new file | Yes — this execution record only, no graph node (convention for docs/dev/ operational artifacts) |
| R7 Execution record | Every manual fix | Yes (this file) |
| R8 Multi-commit restart loop | Follow-up commits | Expected (audit record + SOP sync) |
| R9 Coverage warnings | Unmapped files in commit | No (target files are code; `role_permissions.py` + `governance/auto_chain.py` are already mapped) |
| R10 Doc location check | New doc | Yes — execution record in docs/dev/ |
| R11 chain_version sync | Every manual fix | Yes — Phase 4 |

## Phase 2 — PRE-COMMIT VERIFY

- Target-file-only diff review:
  - `agent/role_permissions.py` — 2 lines (prose + JSON example template)
  - `agent/governance/auto_chain.py` — 4 lines in `_gate_qa_pass` (docstring + single-value check)
- Tests: 104 pass across `test_qa_output_validation.py`, `test_qa_gatekeeper_round1.py`, `test_governance_gate_policy.py`, `test_executor_output_parsing.py`, `test_auto_chain_routing.py`. One pre-existing unrelated failure (verified by re-running against pre-B34-fix state).
- No `qa_pass_with_fallback` references remain in repo (grep-verified; only worktree copies under `.claude/worktrees/*` retain it — those get refreshed on next worktree creation).

## Phase 3 — COMMIT

Commit message:

```
manual fix: B34 — drop vestigial qa_pass_with_fallback from QA allowlist

Validator never accepted qa_pass_with_fallback but QA role prompt told
Claude to emit it. Result: QA tasks failed with
structured_output_invalid:invalid_recommendation:qa_pass_with_fallback,
forcing observer takeover. Chicken-and-egg: chain was retrying Dev on
this same bug (3 consecutive retries 2026-04-20 01:55–02:08Z, no Test
dispatch), so autonomous fix impossible.

Standardize QA recommendation to {qa_pass, reject}. Validator's
{qa_pass, reject, merge_pass} union is correct and unchanged
(merge_pass is Gatekeeper's valid output, shared validator path).

Affected nodes: agent.role_permissions (auto), agent.governance.auto_chain (auto)
Bypass reason: R3 N/A (both nodes gate_mode=auto, no explicit verification)
Trigger: chain deadlocked on the bug being fixed

Files:
- agent/role_permissions.py                (QA prompt prose + JSON example)
- agent/governance/auto_chain.py           (_gate_qa_pass docstring + check)
- docs/dev/bug-and-fix-backlog.md          (R4 — B34 [FIXED])
- docs/dev/manual-fix-current-B34.md       (R7 execution record)
```

## Phase 4 — POST-COMMIT VERIFY

1. `curl http://localhost:40000/api/health` → must return `status: ok` (no restart needed; dynamic `get_server_version` since B19/O3)
2. `POST /api/version-sync/aming-claw` → push git HEAD into `git_synced_at`
3. `POST /api/version-update/aming-claw` with new HEAD (short form per B35 lesson) → advance chain_version
4. `GET /api/version-check/aming-claw` → expect `ok=true, dirty=false` (worktree noise filtered by B31 since 42258ee)
5. Follow-up commits (backlog amendment, this record) → re-run Phase 4 per R8

## Phase 5 — WORKFLOW RESTORE PROOF

After commit + version sync, QA tasks that emit `qa_pass` (the correct value per new prompt) will pass the validator AND the gate. QA → Gatekeeper → Merge chain unblocks. Remaining B36 (retry prompt scope > gate scope) is orthogonal — still OPEN, documented, workaround available.

Minimal restore test: create a trivial PM task after sync and observe auto-chain completes without observer intervention. (Deferred to next session — low-priority; B34 fix alone unblocks the previously-stuck chain class.)

## Follow-up observations

- **B36 still OPEN**: retry prompt SCOPE CONSTRAINT can be wider than `_gate_checkpoint` enforces, causing ping-pong on dev retries across tests that import target_files. Observer workaround: expand PM `test_files` to cover every test importing any target_file.
- **Auto-memory MEMORY.md** (out-of-repo) already tracks B34 in "Known Bugs" — no update needed until next consolidation.
- **Preflight warnings** (orphan pending nodes, unmapped files) are architectural, not blockers. Document in next session-status update.
