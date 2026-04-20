# Session Status

> Last updated: 2026-04-10 (B23 fixed: docs/dev/ added to version_check _DIRTY_IGNORE)
> Updated by: Observer session (6a4e694 → 1d66aa5)

---

## How to Use This File

New session? Read this first, then follow links for details.

---

## System State

| Component | Status | Details |
|-----------|--------|---------|
| Governance | Running | Port 40000, dynamic version via `get_server_version()` (no restart needed after commits) |
| Executor | Running via ServiceManager | Restarts on deploy signal, `test=script` path active |
| Git HEAD | 1d66aa5 | `manual fix: B23 add docs/dev/ to _DIRTY_IGNORE` |
| chain_version | 1d66aa5 | Synced |
| Graph | 29 nodes (3 qa_pass, 5 t2_pass) | YAML configs as secondary (G7) |
| Tests | 963+ pass, 2 pre-existing failures | Full regression verified |

## Milestone: Fully Autonomous Chain

**2026-04-10**: First fully autonomous PM→Dev→Test→QA→Gatekeeper→Merge→Deploy chain completed with zero observer intervention. Deploy task ran `restart_executor()`, ServiceManager consumed signal, executor restarted with fresh code.

## All Bugs Fixed (this session: B11-B20, G7-G10, O2-O3)

| ID | Description | Fix | Method |
|----|-------------|-----|--------|
| B11 | ServiceManager no signal consumption | eff196f | Chain |
| B12 | KeyError on chain gate_reason | ee9d9bb | Chain |
| B13 | Dead tester.yaml + YAML not in graph | 9faa28a | Chain |
| B14 | Claude CLI empty stdin (communicate missing input=) | d71baa6 | Manual (chicken-and-egg) |
| B15 | Version gate blocks on worktree dirty files | 44ab315 | Manual SOP (chicken-and-egg) |
| B16 | No retry for version gate blocks | 8f84d82 | Chain |
| B17 | task.completed publishes after version gate | 8f84d82 | Chain |
| B18 | API task_create missing task.created event | 0235786 | Chain |
| B19 | Governance version stale after commits | 6810a37 | Chain |
| B20 | Staged/untracked leaks block ff-only merge | 2bd20f9 | Manual (chicken-and-egg) |
| B23 | version_check dirty filter missing docs/dev/ path | 1d66aa5 | Manual (chicken-and-egg) |
| G7 | config/roles/*.yaml not in graph | 9faa28a | Chain |
| G8 | related_nodes not auto-populated | 8f84d82 | Chain |
| G9 | Observer SOP for manual task metadata | 79f9c39 | Chain |
| G10 | Graph rebuild mapping | 79f9c39 | Chain |
| O2 | Version gate worktree filter | 44ab315 | Manual |
| O3 | Dynamic get_server_version() | 6810a37 | Chain |
| B31 | Version gate dirty filter missing .claude/worktrees/* submodule refs | TBD | Chain |

Manual fixes: 5 (B14, B15, B19, B20, B23) — all chicken-and-egg deadlocks where the bug prevented the chain from running.
Chain fixes: 13 — delivered through autonomous PM→Dev→Test→QA→Merge pipeline.

## What Works Without Observer

| Capability | Status | Notes |
|------------|--------|-------|
| PM→Dev dispatch | ✅ | Auto-chain creates dev task |
| Dev execution in worktree | ✅ | Claude CLI with proper stdin (B14) |
| Dev→Test dispatch | ✅ | Version gate passes (B15+B16+B19) |
| Test via script path | ✅ | `_execute_test()` subprocess pytest, ~15s |
| Test→QA dispatch | ✅ | Auto-chain |
| QA review | ✅ | Claude CLI, checks acceptance criteria |
| QA→Gatekeeper dispatch | ✅ | Graph gate with related_nodes (G8) |
| Gatekeeper→Merge dispatch | ✅ | Auto-chain |
| Merge with worktree cleanup | ✅ | ff-only after staged/untracked cleanup (B20) |
| Merge→Deploy dispatch | ✅ | Version cache invalidation before gate |
| Deploy executor restart | ✅ | ServiceManager signal consumption (B11) |

## Remaining Items (P3, optional, next session)

| Item | Description |
|------|-------------|
| O1 Phase 2-3 | Consolidate runtime context as single source (builders read from chain_context) |
| G1 | Dirty-workspace root cause classification |
| G2 | Pre-flight advisory at task_create |
| G3 | Chain context bypass tracking |
| Stale role docs | docs/roles/coordinator.md, dev.md, qa.md, pm.md — minor notes |

These are architecture improvements, not bugs. System works correctly without them.

## Process Reference

| Document | Path |
|----------|------|
| Bug backlog | [docs/dev/bug-and-fix-backlog.md](bug-and-fix-backlog.md) |
| Manual fix SOP | [docs/governance/manual-fix-sop.md](../governance/manual-fix-sop.md) |
| Implementation process | [docs/governance/implementation-process.md](../governance/implementation-process.md) |
| Graph-driven doc plan | [docs/governance/plan-graph-driven-doc.md](../governance/plan-graph-driven-doc.md) |
| Auto-chain docs | [docs/governance/auto-chain.md](../governance/auto-chain.md) |
| Executor API | [docs/api/executor-api.md](../api/executor-api.md) |

## Starting a New Session

1. Check `curl http://localhost:40000/api/health` — governance running?
2. Check ServiceManager is supervising executor — `tasklist /v /fi "imagename eq python.exe" | findstr service_manager` (Windows) must show a running `agent/service_manager.py` process, and the `executor_worker.py` process's parent PID must match it. ServiceManager does NOT bind a TCP port, so supervision verification is by process tree, not netstat.
3. Tail executor log — `tail shared-volume/codex-tasks/logs/service-manager-executor-*.err.log`
4. If anything down: use the one-click launcher `.\start.ps1`, or start manually in order — (a) `python -m agent.governance.server --port 40000`, then (b) `.\scripts\start-manager.ps1 -Takeover` (which launches `agent/service_manager.py` and supervises `executor_worker`). The MCP server in `.mcp.json` uses `--workers 0` and does NOT start either service.
5. Run `preflight_check` via MCP — should return `ok: true`
6. Create PM task — chain runs autonomously from there
7. Observer mode OFF by default — only enable if you need to inspect/hold tasks
