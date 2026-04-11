# Manual Fix Execution Record — B29

> manual_fix_id: MF-2026-04-11-001
> operator: observer
> started: 2026-04-11
> trigger: fixing_auto_chain (version gate reads dynamic git HEAD instead of DB chain_version)
> bug_id: B29

---

## Phase 0 — ASSESS

**git HEAD**: `8c5598b` (docs: update backlog — 2026-04-11)
**chain_version (DB)**: `993aa29` (updated 2026-04-10T23:04:41Z, updated_by: observer-sync)
**governance version** (`/api/health`): `8c5598b` (dynamic HEAD, B19 side-effect)
**git status**: only `.claude/worktrees/` dirty files (ignored by version gate filter)

**Symptom confirmed**: `get_server_version()` returns `8c5598b` (current HEAD), version gate compares
`server_ver != head` → passes. DB `chain_version=993aa29` ≠ HEAD `8c5598b` — any Observer commit
auto-advances the gate baseline, bypassing the requirement that only workflow-merged versions count.

---

## Phase 1 — CLASSIFY

**Changed files (planned)**:
- `agent/governance/auto_chain.py` — `_gate_version_check()` lines 1668-1686

**Affected nodes**: governance.graph (contains auto_chain.py mapping) — Scope B (1-5)
**Danger**: High (modifying version gate logic in auto_chain.py)
**Combined level**: B-High → run full test suite + verify each node manually

---

## Phase 2 — PRE-COMMIT VERIFY

### 2.1 Pre-change test baseline

**Full suite** (pre-change): [TO BE FILLED after test run completes]

### 2.2 Version gate test coverage

No dedicated `test_version_gate` / `test_gate_version_check` test file found.
The change is logic-level: replacing `server_ver != head` (where server_ver = dynamic HEAD)
with `chain_version != head` (where chain_version = DB-stored version from last Deploy).

Affected code path: `_gate_version_check()` → currently passes when server_ver == HEAD
(always true after B19 since server_ver is dynamically read HEAD).
After fix: passes only when `chain_version == HEAD` (DB value = last deployed version).

Since B29 is a design correction (closing a security/audit gap, not a functional regression),
the correct behavior change is:
- **Before**: version gate always passes (server_ver == HEAD == HEAD)  
- **After**: version gate blocks until chain_version is updated by Deploy

For the workflow restore proof (Phase 5), a reconciliation-bypass PM task will be used to
confirm the chain can still proceed through the bypass lane when needed.

### 2.3 verify_requires: None for auto_chain nodes
### 2.4 Mandatory rules
- R6: No new files created → N/A
- R7: This execution record ✓
- R9: Coverage — auto_chain.py is mapped in governance.graph ✓
- R10: This file placed in docs/dev/ per convention ✓

---

## Phase 3 — COMMIT

**Planned change** (`auto_chain.py:1668-1686`):

Replace:
```python
from .server import get_server_version
import subprocess
head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], ...).stdout.strip()
if not head or head == "unknown":
    return True, "git HEAD unavailable, skipping"
server_ver = get_server_version()
if server_ver == "unknown":
    return True, "server version unavailable, skipping"
if server_ver != head:
    return False, f"server version ({server_ver}) != git HEAD ({head}). Restart governance..."
return True, f"version match: {server_ver}"
```

With:
```python
import subprocess
head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], ...).stdout.strip()
if not head or head == "unknown":
    return True, "git HEAD unavailable, skipping"
chain_ver = (row["chain_version"] or "").strip() if row else ""
if not chain_ver or chain_ver == "unknown":
    return True, "chain_version unavailable in DB, skipping"
if chain_ver != head:
    return False, f"chain_version ({chain_ver}) != git HEAD ({head}). Complete workflow Deploy to update."
return True, f"version match: {chain_ver}"
```

Note: `row` is already fetched at line 1654. `get_server_version()` import removed from this path
(it remains in server.py for `/api/health`).

**Commit hash**: [TO BE FILLED]

---

## Phase 4 — POST-COMMIT VERIFY

[TO BE FILLED after commit]

- Governance restart: [ ]
- version_check response: [ ]
- preflight delta: [ ]

---

## Phase 5 — WORKFLOW RESTORE PROOF

[TO BE FILLED]

- Test task created: [ ]
- Status transitions: [ ]
- auto_chain dispatched next: [ ]

---

## Phase 6 — SESSION STATUS + BACKLOG UPDATE

[TO BE FILLED]

- Backlog B29 status: OPEN → FIXED
- chain_version in DB: still `993aa29` (correct — Deploy has not run since)
- Workflow gate now correctly blocks until Deploy updates chain_version
