# Backlog Governance

Single-source-of-truth contract for the project backlog (bugs, optimizations, manual-fix audit log).

Created: 2026-04-21 (after OPT-DB-BACKLOG Phase 2 ETL backfill).
Supersedes: the transitional measures in `docs/roles/observer.md` and `docs/dev/manual-fix-sop.md` that pre-date DB-first backlog.

---

## 1. Authoritative source

| Layer | Location | Role |
|---|---|---|
| **DB table** | `backlog_bugs` (governance DB, schema v15+) | **Authoritative**. All reads and writes go through this. |
| **Event stream** | `chain_events` (append-only) + `audit_log` | Change history; powers the audit view. |
| **Markdown file** | `docs/dev/bug-and-fix-backlog.md` | **Read-only human-readable projection**. Regenerated from DB. Do not hand-edit. |

**Rule:** if the DB and the markdown disagree, the DB wins. The markdown is re-derived, not reconciled.

---

## 2. Who writes

All writes use the API — never direct SQL, never direct md edits.

| Actor | What they write | How |
|---|---|---|
| **Observer (human)** | MF-YYYY-MM-DD-NNN manual-fix entries; new bugs discovered via operational pain; priority re-ranks | `POST /api/backlog/{pid}/{bug_id}` or `mcp__aming-claw__backlog_upsert` |
| **Auto-chain / merge stage** | `status=FIXED` + `commit=<hash>` when a PM→Dev→Test→QA→Merge chain closes an open bug | `POST /api/backlog/{pid}/{bug_id}/close` (called from merge finalize) |
| **ETL script** | Bulk import from legacy markdown (one-off rescue) | `python scripts/etl-backlog-md-to-db.py --apply` (idempotent) |
| **Cron observer (future, B39)** | Status refreshes, staleness flags | API only |

**Prohibited writes:**
- Hand-edit `docs/dev/bug-and-fix-backlog.md`. The file is regenerated; edits are lost. Committed edits bump HEAD and can silently kill in-flight chains (B47).
- Direct `sqlite3.connect()` on governance.db from the host while Docker governance is running — WAL cross-process lock causes cascade timeout.
- `version-update` with `updated_by="init"` to re-bootstrap the version chain as a side-channel.

---

## 3. Who reads

| Consumer | How they read | Notes |
|---|---|---|
| **Coordinator** | `backlog_list(status=OPEN)` for duplicate-bug detection | Memory + rule engine consult this before creating new tasks |
| **Cron observer** (B39) | `backlog_list` filtered by `chain_trigger_json.needs_chain=true` | Drives scheduled chain execution |
| **Human via MCP** | `mcp__aming-claw__backlog_list`, `backlog_get` | Primary way for developers to inspect |
| **Human via md** | `docs/dev/bug-and-fix-backlog.md` | PR-review / git-blame / offline read. Projection of DB at last regen. |
| **Auto-chain gatekeeper** | `backlog_get(bug_id)` when task metadata carries `bug_id` | Verifies acceptance_criteria + target_files match |

---

## 4. Markdown regeneration

Interim (today, manual):
- Observer runs `python scripts/etl-backlog-md-to-db.py --regen-md` (TODO: `--regen-md` flag not yet implemented — Phase 3 item).
- Expected cadence: nightly, and after significant observer upserts.

Target (OPT-DB-BACKLOG Phase 3):
- `OutboxWorker` subscribes to `backlog_bugs.changed` events; regenerates the md file within 30s of every upsert/close.
- The regenerated md is committed via a dedicated low-frequency chain (`doc-backlog-regen` chain) with `skip_doc_check=true`, bypassing normal gate rules because it's mechanical output.
- This makes the "no hand-editing" rule self-enforcing — any edit a human makes gets overwritten on the next event.

Pre-commit lint (recommended, not yet implemented):
- `scripts/check-backlog-md-untouched.py` — fails if `git diff HEAD -- docs/dev/bug-and-fix-backlog.md` shows changes that don't correspond to a `backlog_bugs.updated_at > last_regen_ts` row.

---

## 5. Manual Fix (MF) audit log

MF entries follow the same rule: **DB is authoritative**.

- Entry ID pattern: `MF-YYYY-MM-DD-NNN` (date + sequence).
- Store as a regular row in `backlog_bugs` with `status=FIXED` and `priority=P3` (or higher if the fix itself is architecturally significant).
- Historical MF entries in the markdown under `## Manual Fix Audit Log` were ETL-imported 2026-04-21. That section of the markdown is **frozen historical** — new MF entries go to DB only, regeneration will pull them out into the same section but read-only.
- See `docs/dev/manual-fix-sop.md` §6 for the required JSON schema and example `curl` invocation.

---

## 6. Audit view

Full audit of backlog mutations = two append-only tables, UNION ALL, ordered by timestamp:

```sql
SELECT ts, 'chain_event' AS source, actor, event_type, payload
FROM chain_events
WHERE project_id = 'aming-claw' AND event_type LIKE 'backlog.%'
UNION ALL
SELECT ts, 'audit_log' AS source, actor, action, details
FROM audit_log
WHERE project_id = 'aming-claw' AND (action = 'backlog_upsert' OR action = 'backlog_close')
ORDER BY ts DESC;
```

- `chain_events` records mutations that happen inside a PM→Dev→Test→QA→Merge chain (e.g. merge finalize closing a bug).
- `audit_log` records mutations that happen outside chains (observer manual upsert, cron refresh, ETL bulk import).

No separate backlog-audit table is needed.

---

## 7. Schema (reference)

```sql
CREATE TABLE backlog_bugs (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  bug_id                TEXT UNIQUE NOT NULL,    -- B48, MF-2026-04-21-001, OPT-SAFE-COMMIT, ...
  title                 TEXT NOT NULL,
  status                TEXT NOT NULL,            -- OPEN, FIXED, PROPOSED, LANDED, DEFERRED
  priority              TEXT NOT NULL,            -- P0, P1, P1.5, P2, P3
  target_files          TEXT NOT NULL,            -- JSON array
  test_files            TEXT NOT NULL,            -- JSON array
  acceptance_criteria   TEXT NOT NULL,            -- JSON array
  chain_task_id         TEXT,                     -- task-id that triggered/fixed this bug
  "commit"              TEXT,                     -- short git hash at fix time
  discovered_at         TEXT,                     -- ISO-8601
  fixed_at              TEXT,                     -- ISO-8601
  details_md            TEXT,                     -- free-form markdown
  chain_trigger_json    TEXT,                     -- JSON: {needs_chain, priority, ...}
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_backlog_bug_id ON backlog_bugs(bug_id);
CREATE INDEX idx_backlog_status ON backlog_bugs(status);
CREATE INDEX idx_backlog_priority ON backlog_bugs(priority);
```

`chain_trigger_json` shape (for cron-driven scheduled execution, B39):

```json
{
  "status":       "OPEN",
  "needs_chain":  true,
  "priority":     "P1",
  "bug_id":       "B48",
  "target_files": ["agent/service_manager.py", "agent/executor_worker.py"],
  "test_files":   ["agent/tests/test_executor_recovery.py"]
}
```

---

## 8. Migration status (as of 2026-04-21)

- ✅ Phase 1 (LANDED, 3a7be63): schema + CRUD API + MCP tools + tests (23 `test_backlog_db` tests)
- ✅ Phase 2 (LANDED, 2026-04-21): existing 70 md entries ETL'd into DB; 4 docs updated (this file, observer.md, manual-fix-sop.md, backlog md banner); OPT-CONTEXT-UNIFY closed as duplicate of O1; O1 upgraded P3→P1 with full acceptance criteria absorbing the B48 structural fix
- ⏳ Phase 3 (not started): outbox-worker md regeneration; pre-commit lint; `--regen-md` flag in ETL script

Related entries in backlog DB: `OPT-DB-BACKLOG`, `O1`, `B47`, `B48`.

---

## 9. Backlog-as-chain-source policy (graph contract L4.43)

> **Governance node:** `L4.43` declares this section as authoritative contract. Implementation
> tracked by `OPT-BACKLOG-AS-CHAIN-SOURCE` (P1). Until that OPT lands, the invariants below
> are enforced by observer/coordinator discipline; the hook in §9.4 turns discipline into code.

### 9.1 Four invariants

| # | Invariant | Enforced by |
|---|---|---|
| I1 | Every main-branch commit must cite a backlog ID (`B\d+` / `MF-YYYY-MM-DD-NNN` / `OPT-[A-Z-]+`) in its message, OR mark itself `[trivial]` (touches only `.claude/`, docs-only), OR `[emergency-fix] Reason: <text>`. | Pre-commit hook (§9.4) |
| I2 | Chain path: `bug_id` propagates through `chain_context` from coordinator → PM → Dev → Test → QA → Merge. Merge stage reads it from `chain_context`, not from individual task metadata. | `auto_chain._try_backlog_close_via_db` refactor (OPT Chain 2) |
| I3 | MF path: backlog row is written **before** the first code edit (`status=MF_PLANNED`), then transitioned to `FIXED` after commit. No "fixed but OPEN" drift window. | `manual-fix-sop.md` §13 + hook (§9.4) |
| I4 | `backlog_close` requires a commit hash that exists in `git log`. Row cannot be closed against a ghost commit. | Close endpoint validation (OPT Chain 5) |

### 9.2 Unified state machine (chain + MF converge at FIXED)

```
                     OPEN
                   ╱      ╲
           [chain path]   [MF path]
                ↓               ↓
           PM_ACTIVE       MF_PLANNED
                ↓               ↓
           DEV_ACTIVE      MF_IN_PROGRESS  (optional; code being edited)
                ↓               ↓
           TEST_ACTIVE          │
                ↓               │
           QA_ACTIVE            │
                ↓               │
           MERGING              │
                ↓               ↓
                ╲              ╱
                   FIXED  (commit hash attached, verified exists in git log)

   Terminal failure (retries exhausted / abandoned):
      → OPEN (+ last_failure_reason)  OR  CANCELLED (+ reason)
```

Schema v16 (landed in OPT Chain 3) adds columns: `chain_stage`, `last_failure_reason`, `stage_updated_at`.

### 9.3 Why unified: the drift that motivated this

B41 was the triggering case:
- AC1 shipped via Manual Fix (`6afc22a`) — no chain finalize → no `backlog_close`
- AC2/AC3 shipped via chain (`c762d54`) — chain had no `metadata.bug_id="B41"` (because B41 was flagged `needs_chain=false` at the time) → `auto_chain.py:2690` did not trigger close
- Result: code fixed on HEAD, backlog row orphaned as OPEN for 2 days. Discovered by memory recall during a re-attempt.

This is a systematic class: any time code lands without the `metadata.bug_id` linkage (manual fix, ad-hoc chain, observer takeover), the backlog drifts. Invariants I1–I4 close the loop.

### 9.4 Physical enforcement: pre-commit hook (OPT Chain 7)

```python
# scripts/precommit-require-backlog-id.py (target)
# - Parses commit message for B\d+ / MF-YYYY-MM-DD-NNN / OPT-[A-Z-]+
# - If none: accepts [trivial] (staged files all under .claude/ or docs-only)
#            accepts [emergency-fix] (requires `Reason: <text>` in body — logged for post-hoc MF)
#            otherwise: REJECT with message pointing at this section
# - If found: GETs /api/backlog/{pid}/{bug_id}
#            REJECT if 404 (row must be pre-declared)
#            REJECT if status in (FIXED, CANCELLED) (must re-open or use a new ID)
# - On API unreachable (governance down): writes to .pending-mf-entries.jsonl
#            and accepts the commit; entries are replayed when governance returns.
```

Until Chain 7 lands, discipline is: follow §13 of `manual-fix-sop.md` for MF path; ensure coordinator sets `metadata.bug_id` when user prompt names a backlog ID.

### 9.5 Edge cases

| Case | Handling |
|---|---|
| Governance container down, urgent hotfix | `[emergency-fix] Reason: <text>` commit; local `.pending-mf-entries.jsonl` journal; replay on recovery |
| Rebase / cherry-pick | Commit message preserves bug ID; `close` endpoint is idempotent |
| One commit fixes multiple bugs | `Fixes: B47, B48` in body; close endpoint accepts batch |
| `.claude/worktrees/*` dirty (B31 pattern) | Hook treats as trivial; also in `_DIRTY_IGNORE` for gate |
| Observer commits docs-only changes | `[trivial]` tag OR MF row (use MF when audit is wanted) |

### 9.6 Implementation scope (tracked by OPT-BACKLOG-AS-CHAIN-SOURCE P1)

8-chain breakdown (ordered to front-load MF-path fixes, since those leak most frequently):

| # | Chain | What |
|---|---|---|
| 1 | coord-autotag | Coordinator parses user prompt for B/MF/OPT IDs and auto-sets `metadata.bug_id`. When no ID found but task will create code changes, auto-upserts a new backlog row with allocated ID. |
| 2 | chain-context-bug-id | `bug_id` persisted in `chain_context`; each stage inherits. Merge reads from chain_context, not individual task metadata. |
| 3 | backlog-stage-schema | Schema v16: `chain_stage`, `last_failure_reason`, `stage_updated_at`. Migration + MCP/REST projection. |
| 4 | gate-stage-transitions | Each gate (PM/checkpoint/T2/QA/merge) emits `backlog.stage_update` event; backlog row follows the state machine in §9.2. |
| 5 | close-commit-verify | `backlog_close` endpoint verifies commit hash exists in `git log`; rejects ghost-commit closes. Failure-rollback logic for terminal-failed chains. |
| 6 | mf-predeclare-endpoint | `status=MF_PLANNED` and `MF_IN_PROGRESS` accepted by upsert endpoint; `manual-fix-sop.md` §13 becomes enforced. |
| 7 | precommit-hook | `scripts/precommit-require-backlog-id.py` + install instructions + offline journal replay. |
| 8 | sop-rewrite-dogfood | Rewrite `manual-fix-sop.md` front matter to describe pre-declare as the default; retire the "after-the-fact audit" language in §6. |

### 9.7 Related

- Graph node: `L4.43` (this section is its `primary`).
- Backlog entry: `OPT-BACKLOG-AS-CHAIN-SOURCE` (P1).
- Triggering incident: `B41` discovery via MF-2026-04-21-004.
- MF SOP for pre-declare: `manual-fix-sop.md` §13.
