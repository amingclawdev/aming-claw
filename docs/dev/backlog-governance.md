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
