# Dev Stage Verification Review — 2026-03-30

> Reviewer: Observer (Claude Opus)
> Scope: Verify Codex Round 1-6 dev iteration results, assess coordinator→PM→dev chain integrity
> Input: `docs/dev/dev-iteration.md`, `docs/dev/session-handoff-2026-03-30.md`, full code review

---

## 1. Review Method

- Read dev-iteration.md (Rounds 1-6) and session-handoff for context
- Parallel code review of 6 critical files via subagents:
  - `agent/governance/auto_chain.py` — chain logic, gate, prompt, normalization
  - `agent/executor_worker.py` — worktree, git diff, merge, dev prompt
  - `agent/ai_lifecycle.py` — provider routing, snapshot injection
  - `agent/governance/server.py` — context-snapshot endpoint
  - `agent/governance/models.py` — MemoryEntry.from_dict
  - `agent/governance/memory_service.py` — memory write passthrough
- Ran all dev-related tests (13 targeted + 422 full suite)

---

## 2. Test Results

| Test File | Isolated | Full Suite |
|-----------|----------|------------|
| test_dev_contract_round2.py | 2/2 passed | passed |
| test_dev_worktree_round3.py | 2/2 passed | passed |
| test_dev_contract_round4.py | 6/6 passed | 5/6 (1 ordering-dependent failure) |
| test_ai_lifecycle_provider_routing.py | 3/3 passed | passed |
| **Full suite** | — | **421 passed, 1 failed** |

### The 1 failure

`test_dev_contract_round4.py::test_retry_prompt_rebuilds_dev_contract_from_metadata`

- Passes in isolation, fails in full suite
- Root cause: test uses `object()` as mock DB conn; when run with other tests, patch target for `task_registry.create_task` shifts due to import side-effects, causing real `conn.execute` to be called on `object()`
- Classification: **test isolation bug**, not a logic bug
- Fix: tighten mock path or use shared conftest fixture

---

## 3. Code Verification Matrix

### Round 2 — Contract Repair

| Fix | Code Location | Verified |
|-----|--------------|----------|
| PM requirements forwarded to Dev prompt | auto_chain.py:708-712, 3-tier fallback (result→prd→metadata) | OK |
| `_render_dev_contract_prompt()` includes all fields | auto_chain.py:54-79 (verification, requirements, AC, test_files, doc_impact) | OK |
| context-snapshot role parsing (no "d" truncation) | server.py:1909-1911, handles list/string safely | OK |
| session_context included in snapshot response | server.py:2010-2016 (focus, decision, version, updated_at) | OK |
| Dev prompt injects snapshot | ai_lifecycle.py:431-439, single fetch path, no duplicate | OK |
| MemoryEntry.from_dict accepts module alias | models.py:174 `module_id=d.get("module_id", d.get("module", ""))` | OK |
| structured payload passthrough | memory_service.py:110-112, extracted and merged into backend_entry | OK |

### Round 3 — Worktree Isolation

| Fix | Code Location | Verified |
|-----|--------------|----------|
| `_create_worktree()` | executor_worker.py:1071-1088, path=`.worktrees/dev-{task_id}`, branch=`dev/{task_id}` | OK |
| `_remove_worktree()` with branch cleanup | executor_worker.py:1090-1110, `git worktree remove --force` + `git branch -D` | OK |
| Dev execution uses worktree workspace | executor_worker.py:167-174, `execution_workspace = worktree_path` | OK |
| `create_session(workspace=)` receives worktree | executor_worker.py:210 | OK |
| `_get_git_changed_files(cwd=)` task-local diff | executor_worker.py:1112-1150, `repo_cwd = cwd or self.workspace` | OK |
| Merge handles worktree branch merge + cleanup | executor_worker.py:360-388, stage→commit→merge→remove | OK |

### Round 4 — Verification Contract

| Fix | Code Location | Verified |
|-----|--------------|----------|
| Dev contract prompt includes verification plan | executor_worker.py:782-788, explicit "MUST attempt" instruction | OK |
| test_results.command in output schema | executor_worker.py:803 | OK |
| Retry rebuilds from structured metadata | auto_chain.py:346-349, calls `_render_dev_contract_prompt(metadata)` | OK |

### Round 5 — Doc Gate + Node Normalization

| Fix | Code Location | Verified |
|-----|--------------|----------|
| `_normalize_related_nodes()` | auto_chain.py:36-51, handles string/dict(node_id)/dict(id) | OK |
| Checkpoint respects PM doc_impact.files | auto_chain.py:588-592, explicit files → use; absent → infer | OK |

### Round 6 — Executor-Owned Diff + Node Gate Bypass

| Fix | Code Location | Verified |
|-----|--------------|----------|
| No governance-side git diff in checkpoint | auto_chain.py:562-565 (design comment), trusts executor evidence | OK |
| Dev-stage node gate log-only | auto_chain.py:606-614, `log.warning` + `return True` | OK |

### Provider Routing (Codex session addition)

| Fix | Code Location | Verified |
|-----|--------------|----------|
| Provider resolution anthropic→claude / openai→codex | ai_lifecycle.py:186, 203-211 | OK |
| Codex command builder | ai_lifecycle.py:108-128 | OK |
| File-based logging (no log.info in subprocess) | ai_lifecycle.py:166-176, `_al_log()` throughout | OK |

---

## 4. Findings Summary

### No bugs in Codex dev scope

All 6 rounds of fixes are correctly implemented and match their documented predictions. The orchestration layer (auto_chain, gate, worktree, prompt, memory) is sound.

### 1 test isolation issue (minor)

- File: `test_dev_contract_round4.py`
- Impact: ordering-dependent failure in full suite only
- Priority: low — fix mock path

### Pre-existing design gaps (outside Codex dev scope)

These are NOT bugs introduced by the dev iterations. They existed before and remain unchanged:

#### G1. Claude CLI path: dev has no `--max-turns`

```
ai_lifecycle.py:102-105
coordinator → --max-turns 1
pm → --max-turns 10
dev/tester/qa → (none, unlimited)
```

Risk: Claude dev could run indefinitely. Codex has its own timeout. Claude CLI relies on `subprocess.run(timeout=)` but no turn cap.

Recommendation: add `--max-turns 20` for dev, `--max-turns 10` for tester/qa.

#### G2. Claude CLI has no `-C` workspace control

```
ai_lifecycle.py:93-96 (Claude path)
cmd = ["claude", "-p", "--system-prompt-file", prompt_file]
# no workspace argument

ai_lifecycle.py:114-119 (Codex path)
cmd = ["codex", "exec", "-C", cwd, ...]
# explicit workspace
```

Claude CLI's Read/Grep/Glob tools may not be constrained by `subprocess.run(cwd=...)`. Worktree isolation depends on the AI respecting the system prompt instruction about workspace path.

Recommendation: investigate whether Claude CLI inherits cwd from subprocess, or needs explicit `--cwd` (if available). If not, document the constraint.

#### G3. `ANTHROPIC_API_KEY` stripped from env

```
ai_lifecycle.py:218
env.pop("ANTHROPIC_API_KEY", None)
```

Intended to avoid nested Claude auth conflicts. Claude CLI must authenticate via another mechanism (e.g., `~/.claude/` config). Confirm this works before switching dev provider to anthropic.

#### G4. `_build_dev_prompt()` chain context fallback has bare except

```
auto_chain.py:716-730
try:
    # chain context lookup
except Exception:
    pass  # silent swallow
```

Low priority. Should be `log.debug()` at minimum.

#### G5. `doc_impact` field has no structure validation

```
auto_chain.py:589-590
```

Typo `file` vs `files` would silently fallback to inferred docs. Low risk since PM output is structured.

#### G6. Gate-blocked worktrees are not cleaned up

When dev retries after gate block, old worktree at `.worktrees/dev-{task_id}` persists. No lifecycle policy defined for failed/retried dev worktrees.

---

## 5. Chain Flow Status

| Stage | Status | Evidence |
|-------|--------|---------|
| Coordinator → decision | Stable, 21s | Verified in prior sessions |
| PM → PRD output | Stable, 58-71s | Scheme C flat JSON, gate pass |
| Dev → worktree execution | Working | Round 3 real task: 3-file diff |
| Dev → checkpoint gate | Working | Round 5-6: gate pass, test task created |
| Dev → Test auto-chain | Working | task created in observer_hold |
| Test/QA/Merge | Not yet tested | Next iteration scope |

**Coordinator → PM → Dev chain is operational.**

---

## 6. Recommendations for Next Session

1. **Fix test isolation bug** in test_dev_contract_round4.py (5 min)
2. **Address G1-G3** before switching pipeline_config to Claude provider
3. **Test/QA/Merge stage** predict→verify (next chain stages)
4. **Dev worktree cleanup policy** (G6) before running multiple dev tasks
5. **Create `docs/dev-rules.md`** — dev role spec is still scattered across code (noted in dev-iteration.md Fix H, still open)
