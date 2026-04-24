# Sequence Z — Complete Governance Loop Closure

> **Status**: EXECUTING (user-authorized 2026-04-24)
> **Author**: observer-z5866 session 67351297
> **Predecessor**: Phase 1 (SM-TIMEOUT-BUMP 719f2ae) + B48 fixes (ba791f0, 1bb9f35) + F2 fix (2763aac) + version-update lockdown (e57e7ba)
> **Goal**: Close all four governance-loop gaps the user identified (#1–#4) in a single dependency-ordered sequence.

---

## User's four concerns

1. **Node/doc consistency** — is current graph in sync with reality? Need reconcile?
2. **Auto graph-update after merge+deploy** — PM.proposed_nodes should land in node_state automatically at chain end
3. **DB write deadlock → queue service** — architectural write path
4. **Task source enforcement** — tasks must come from backlog; observer+coordinator maintain state

## Dependency graph

```
Z1 Option A (DB-lock fix) ──┐
                             ├─> Z2 Verify pm.prd.published + graph.delta.* persist
                             │      │
                             │      └─> Z3 Task-source enforcement (#4)
                             │              │
                             │              └─> Z4 Verify graph auto-commit (#2)
                             │                      │
                             │                      └─> Z5 Reconcile (#1)
                             │                              │
                             │                              └─> Z6 Queue service (#3)
                             │
                             └─ (all downstream depend on Z1/Z2 because graph-delta
                                 pipeline is silent-dropping events until Z1 lands)
```

## Sequence rows (estimated chain cost each after B48+F2 fixes)

| ID | Scope | Concern | Chain cost | File |
|---|---|---|---|---|
| Z1 | Option A: `_persist_connection` with busy_timeout=60s + `_retry_on_db_lock` wrapper | #2 enabler | ~25 min | agent/governance/chain_context.py |
| Z2 | Verify Z1 via smoke chain; observe pm.prd.published events | #2 verify | ~15 min | (observation only, trivial PM) |
| Z3 | Task-source enforcement: flip `OPT_BACKLOG_ENFORCE=strict`, existence check, force_no_backlog soft-lock, backlog lifecycle | #4 | ~30 min | agent/governance/server.py (backlog gate + handle_task_create) |
| Z4 | Verify graph auto-commit on a real dev chain | #2 close | observation in Z3 or dedicated trivial chain | (observation) |
| Z5 | Reconcile 94 dropped proposed_nodes historical | #1 | ~30 min (script + dry-run + review + live) | scripts/reconcile-dropped-nodes.py |
| Z6 | Write queue service (Option D): outbox pattern, spool JSONL fallback | #3 | multi-chain sprint | new: agent/governance/write_queue.py + migrations |

## Row ID mapping for backlog

- Z1 → `OPT-BACKLOG-PERSIST-EVENT-CONN-TIMEOUT` (already design-approved in `option-a-persist-event-timeout-proposal.md`)
- Z2 → `OPT-BACKLOG-GRAPH-DELTA-PIPELINE-VERIFICATION`
- Z3 → `OPT-BACKLOG-TASK-SOURCE-ENFORCEMENT`
- Z4 → `OPT-BACKLOG-GRAPH-AUTO-COMMIT-VERIFY` (observation scope)
- Z5 → `OPT-BACKLOG-GRAPH-RECONCILE-APR15-ONWARDS` (supersedes prior `OPT-BACKLOG-RECONCILE-A1-A8-NODES`)
- Z6 → `OPT-BACKLOG-WRITE-QUEUE-SERVICE` (parent row; child PRs Z6-PR1..Z6-PR4)

## Execution protocol

For each Zn:
1. File/confirm backlog row
2. POST PM task referencing bug_id
3. Let chain run **hands-off** (B48+F2 fixed)
4. On chain completion, verify evidence
5. Update this doc with outcome + commit hash
6. Proceed to next

## Progress log

| Zn | Started | Ended | Commit | Result |
|---|---|---|---|---|
| Z1 | pending | | | |
| Z2 | | | | |
| Z3 | | | | |
| Z4 | | | | |
| Z5 | | | | |
| Z6 | | | | |

## Risk register

- **Z1 might expose a different DB lock site** if 60s is still insufficient → escalate to Z6 queue service earlier
- **Z3 enforcement might reject legit tasks** if some existing callers pass no bug_id → monitor executor log for `OPT_BACKLOG_ENFORCE: rejected` during rollout
- **Z5 dry-run might show unexpected scale** if >94 nodes missing → adjust plan
- **Z6 scope is multi-day**; if Z1-Z5 complete and user wants to defer Z6 to separate sprint, acceptable

## Observer escape clauses

- Observer-hotfix remains legitimate for infrastructure meta-circular bugs only (see B48 memory entry)
- If any Zn chain fails in a way that blocks the sequence, observer may do minimal hotfix + file postmortem
- NEVER skip a Zn; each must have evidence

---

*Updated live during execution.*
