# Manual Fix Execution Record — 2026-04-20

> Trigger: Auto-chain dispatch silently blocked after PM completion (dirty workspace pattern).
> Operator: observer (scheduled workflow maintenance task)
> Scope: Fix stale startup documentation that claims "MCP auto-starts executor/governance" (false since `--workers 0` was set in .mcp.json).

## Phase 0 — ASSESS (2026-04-20T04:15Z)

### 0.1 git status (baseline)

```
 m .claude/worktrees/compassionate-tu     (submodule ref dirty)
 m .claude/worktrees/happy-ardinghelli    (submodule ref dirty)
 m .claude/worktrees/zen-mendeleev        (submodule ref dirty)
?? .claude/scheduled_tasks.lock
?? .recent-tasks.json
```

Pre-existing noise. None of these are target files for this fix.

### 0.2 wf_impact(docs/deployment.md, docs/onboarding.md, docs/dev/session-status.md)

- Direct hit: `agent.deploy` (v2, gate=auto), `governance.server` (v4, gate=auto)
- Transitive: `agent.gateway` (v5, auto), `agent.mcp` (v5, auto)
- Total affected: 4 nodes, all `gate_mode=auto` → no explicit verification task needed (R3 N/A)

### 0.3 preflight_check

- ok=true, 0 blockers
- Warnings: version sync stale (358s), 16 orphan pending nodes, 49 unmapped files (pre-existing, none are target files → R9 N/A)

### 0.4 version_check

- HEAD `8541b18` == chain_version `8541b18`
- dirty=true (the three `.claude/worktrees/*` submodule refs)
- This dirty condition is what silently blocked auto-chain Dev dispatch after PM `task-1776658117-adffde` succeeded at 04:10:02Z — Bug 7 / D5 pattern recurring because the D5 filter only excludes `.claude/settings.local.json`, not `.claude/worktrees/*`.

## Phase 1 — CLASSIFY

| Axis | Value | Reason |
|------|-------|--------|
| Scope | B (1–5) | 4 nodes affected |
| Danger | Low | Docs-only; no deletions, no renames, no code changes |
| Combined | B-Low | Per S3 matrix: "Run module tests" |

### Mandatory rule applicability

| Rule | Trigger | Applies? |
|------|---------|----------|
| R1 Scope D split | >20 nodes | No (we are B) |
| R2 Dry-run for delete/rename | Delete or rename | No |
| R3 Auto verification task | explicit+v4 real impact | No (all 4 nodes are `auto`) |
| R4 Audit record | Every manual fix | Yes — append to bug-and-fix-backlog.md |
| R5 Workflow restore proof | Every manual fix | Yes — Phase 5 |
| R6 New-file node check | Any new file | Yes — this execution record is new (docs/dev/), no graph node needed per convention (execution records are operational artifacts, not governance surfaces) |
| R7 Execution record | Every manual fix | Yes (this file) |
| R8 Multi-commit restart loop | Follow-up commits | Expected (audit + record commits) |
| R9 Coverage warnings for committed files | Unmapped file in commit | No (target files are docs; not part of CODE_DOC_MAP) |
| R10 Doc location check | New doc | Yes — execution record placed in docs/dev/ (per convention) |
| R11 chain_version sync | Every manual fix | Yes — Phase 4/5 |

## Phase 2 — PRE-COMMIT VERIFY

- Target-file-only diff review: all edits textual corrections to already-stale startup claims.
- No tests run — docs do not have test coverage. B-Low requires "module tests" but there is no module test for markdown content. Documented.
- verify_requires: none of the 4 nodes declare verify_requires.

## Phase 3 — COMMIT (pending)

Planned commit message:

```
manual fix: correct stale MCP auto-start claims in startup docs

Affected nodes: agent.deploy, governance.server (direct, auto); agent.gateway, agent.mcp (transitive, auto)
Bypass reason: auto-chain Dev dispatch blocked by .claude/worktrees/* submodule dirty state
Trigger: D5 filter does not cover worktree submodule refs (mode 160000)

Files:
- docs/deployment.md  (lines 46-65, 69-71, 90-98, 231-243)
- docs/onboarding.md  (line 142)
- docs/dev/session-status.md (line 97)
- docs/dev/manual-fix-current-2026-04-20.md (new — R7 execution record)
```

## Phase 4 — POST-COMMIT VERIFY (pending)

1. Restart governance (if needed for SERVER_VERSION refresh)
2. `version_check` → expect `ok=true` after R11 sync (worktree noise filtered separately)
3. `preflight_check` delta — expect no new blockers
4. Follow-up commits (audit record append, etc.) → re-run this phase per R8

## Phase 5 — WORKFLOW RESTORE PROOF (pending)

Since the underlying auto-chain blocker is structural (`.claude/worktrees/*` dirty), a minimal test task may still be blocked. If so, document as STILL_BROKEN and recommend D5-follow-up to extend the dirty-file filter.

## Follow-up observations

- **Root cause for next fix (not in this scope):** D5 dirty-file filter in version gate should also filter `.claude/worktrees/*` (submodule refs left behind by dev worktrees). This should be a separate fix via the normal chain after workspace is clean.
- MEMORY.md (out-of-repo, auto-memory) still contains the false claim "MCP server (.mcp.json) auto-starts executor_worker via ServiceManager" — will update via memory tools separately (not in-repo commit).
