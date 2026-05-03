---
name: acceptance-graph
description: Governance contract for the aming-claw acceptance graph. DB + graph.json are authoritative; this file is no longer a node definition source.
type: governance
version: v2.0
---

# Acceptance Graph Governance

> **Plan Y adopted 2026-04-21 (MF-2026-04-21-003).** This file previously
> contained the full definition of 189 L-nodes (v1.0, 2105 lines). That role
> has been retired. Node definitions now live in `graph.json` + governance DB
> `node_state` table; this file only documents the contract.
>
> Historical v1.0 content is preserved in git history (`git show <pre-MF-003>:docs/governance/acceptance-graph.md`) and in the `description` field of each node in `graph.json`. Do not restore it here.

---

## 1. Authoritative source

| Layer | Location | Role |
|---|---|---|
| **Graph definition** | `shared-volume/codex-tasks/state/governance/{pid}/graph.json` | **Authoritative** for node definitions (id, title, primary/secondary/test, verify_level, gate_mode, description). Read directly by gate, observer, coordinator, reconcile. |
| **Node runtime state** | `node_state` table (governance DB) | **Authoritative** for per-node status (pending/testing/t2_pass/qa_pass/failed/waived/skipped) and evidence. |
| **Change history** | `node_history` table + `chain_events` + `audit_log` | Append-only audit; powers drift and waive-reason tracking. |
| **This markdown file** | `docs/governance/acceptance-graph.md` | **Governance contract only** (what you are reading). Not a node source. |

**Rule:** if this markdown and `graph.json` ever disagree on node definitions, `graph.json` wins. This file is not read by any runtime code.

---

## 2. Who writes

| Actor | What | How |
|---|---|---|
| **Coordinator** | New nodes during a chain's PM stage (when target_files introduce new modules/docs) | `POST /api/wf/{pid}/node-create` |
| **Coordinator** | Node attribute updates (secondary/test/description/propagation) | `POST /api/wf/{pid}/node-update` |
| **Observer** (governance repair) | Structural corrections, bulk alignment, legacy waives | `POST /api/wf/{pid}/reconcile` (+ direct graph.json patches with MF audit) |
| **Chain gates** | `verify_status` transitions (pending → testing → t2_pass → qa_pass) | via `state_service` inside auto_chain |
| **Reconcile** | Ref fix-up, orphan waive, un-waive when files return | `reconcile_project()` — two-phase commit (graph.json + DB together) |

**Prohibited writes:**
- Hand-editing `graph.json` outside an MF audit trail. It's a machine-generated artifact.
- Direct `sqlite3.connect()` on governance.db from host while Docker governance runs — WAL cross-process lock.
- Treating this markdown file as a node source — it's a contract, not data. Regenerating it from `graph.json` is not supported.

---

## 3. Who reads

| Consumer | How | Notes |
|---|---|---|
| **Gate (T2/QA/merge)** | `AcceptanceGraph.load_project_graph(pid)` → in-memory | Checks `primary`/`test` exist, verifies dependencies via `deps_graph` edges |
| **Observer** | `mcp__aming-claw__wf_summary`, `wf_impact`, or REST `/api/wf/{pid}/node/{nid}` | Inspection, PR review, troubleshooting |
| **Coordinator** | `load_project_graph` + `wf_impact` during PM stage | Decides related_nodes, checks for dangling refs before creating task |
| **Reconcile** | `graph.json` + filesystem + DB | Detects drift, produces DiffReport |

---

## 4. How to change the graph

### 4.1 Normal flow: during a chain

PM stage automatically declares `target_files`. On **merge**, a local reconcile should be triggered for the delta (OPT-DB-GRAPH roadmap; not yet wired). Until then:

- **New file introduced** → PM/observer calls `POST /api/wf/{pid}/node-create` with `{parent_layer, title, deps, primary}`. System allocates `display_id = L{layer}.{next_index}`.
- **File renamed / moved** → `POST /api/wf/{pid}/reconcile` with `{workspace_path, auto_fix_stale: true, dry_run: true}` first, review, then `dry_run: false, force_apply: true`.
- **Module deprecated** → same reconcile call with `mark_orphans_waived: true`. Evidence records `waive_reason` (one of `orphaned_by_reconcile` / `auto_chain_temporary` / `preflight_autofix` / `manual_exception` / `legacy_frozen` / `deprecated`).

### 4.2 Recovery flow: governance repair (observer only)

For drift accumulated across a refactor window (see MF-2026-04-21-003):

```bash
# 1. Detect (read-only)
curl -X POST http://localhost:40000/api/wf/aming-claw/reconcile \
  -H "Content-Type: application/json" \
  -d '{"workspace_path":".","dry_run":true,"mark_orphans_waived":true}'

# 2. Apply if report looks sane
curl -X POST http://localhost:40000/api/wf/aming-claw/reconcile \
  -d '{"workspace_path":".","dry_run":false,"force_apply":true,"mark_orphans_waived":true}'
```

Observer must record the reconcile in an MF entry (`MF-YYYY-MM-DD-NNN`) and `POST /api/backlog/{pid}/{mf_id}`.

### 4.3 Bootstrap flow: initial import

For a fresh project (no graph.json yet):

```bash
curl -X POST http://localhost:40000/api/wf/{pid}/import-graph \
  -d '{"md_path":"docs/governance/bootstrap-graph.md","reason":"initial bootstrap"}'
```

**Note**: this project (`aming-claw`) does not use import-graph anymore — `graph.json` is the maintained artifact. The import-graph endpoint exists for new projects.

---

## 5. Audit view

Full audit of graph mutations = four tables, joined by timestamp:

```sql
SELECT ts, source, actor, action, details FROM (
  SELECT ts, 'node_history' AS source, role AS actor,
         printf('%s: %s->%s', node_id, from_status, to_status) AS action,
         evidence_json AS details
  FROM node_history
  WHERE project_id = 'aming-claw'
  UNION ALL
  SELECT ts, 'chain_event' AS source, actor, event_type AS action, payload AS details
  FROM chain_events
  WHERE project_id = 'aming-claw' AND event_type LIKE 'graph.%'
  UNION ALL
  SELECT ts, 'audit_log' AS source, actor, action, details
  FROM audit_log
  WHERE project_id = 'aming-claw' AND (action LIKE '%graph%' OR action LIKE '%reconcile%' OR action LIKE '%node_%')
) ORDER BY ts DESC;
```

---

## 6. Roadmap — OPT-DB-GRAPH (P2)

The current design (Plan Y) treats `graph.json` as the authoritative file and uses reconcile for drift repair. The longer-term target (tracked as `OPT-DB-GRAPH` in the backlog) mirrors the `OPT-DB-BACKLOG` pattern:

| Concern | Plan Y (current) | OPT-DB-GRAPH (target) |
|---|---|---|
| Node definitions | `graph.json` (file) | `graph_nodes` table (DB) |
| Drift detection | Manual reconcile | Auto-trigger on chain merge |
| Projection for PR review | — | Outbox worker regenerates read-only `acceptance-graph-snapshot.md` |
| Consistency guarantee | File atomicity | DB transaction |
| Observability | Log lines | Same + `graph_events` audit table |

Until OPT-DB-GRAPH lands, the discipline is: **every code change that introduces or renames a file must be followed by a reconcile call** (ideally automated in the merge stage). This is the gap B49-class bugs exploit.

---

## 7. Troubleshooting

**"Gate complains node L4.xx has missing primary file"** — a file was deleted or moved. Run `reconcile` in dry-run mode to see the suggested fix. If confidence is HIGH, apply. If LOW, manually patch with `node-update` or direct graph.json edit (with MF audit).

**"Chain passed gate but I expected it to fail"** — check if the node is `waived` in DB. A waived node bypasses gate checks. Verify `SELECT verify_status, evidence_json FROM node_state WHERE node_id='L4.xx'`.

**"I added a new module and nothing references it"** — the PM stage of the chain that introduced it should have called `node-create`. If the chain skipped this (pre-OPT-DB-GRAPH), call `POST /api/wf/{pid}/node-create` manually and record as an MF entry.

**"Where's the old node definition text?"** — in `graph.json`'s `description` field for each node, and in git history for this file pre-2026-04-21.

---

## 8. Audit cluster — agent.governance core (FeatureCluster 03825084)

> **Reconcile-driven cluster audit**, fingerprint `03825084` (full
> `038250847347245a`), package_key `agent/governance`, strategy
> `scc_indegree_root_dfs_filetree_coalesce`. Emitted by `cluster_grouper`
> with root_count=25, function_count=73, module_count=6.

The **agent.governance core cluster** is the bootstrap / preflight / permission /
profile / outbox / reconcile surface that today rolls up to a single coarse L3.3
node with no L7 module-level anchors. The reconcile-cluster audit proposes one
L7 candidate per primary file so each module has a discoverable verification
contract.

### 8.1 Primary files (L7 module anchors under L3.3)

| File | Role |
|---|---|
| `agent/governance/outbox.py` | Outbound delivery queue / projection worker |
| `agent/governance/permissions.py` | Per-role permission scope and authorization checks |
| `agent/governance/preflight.py` | Pre-flight self-check (5 checks + auto-fix) before chain dispatch |
| `agent/governance/project_profile.py` | Project profile discovery (root, settings, metadata) |
| `agent/governance/project_service.py` | Project bootstrap / lifecycle service (registration, update) |
| `agent/governance/reconcile.py` | Drift detection and graph/DB two-phase reconcile orchestrator |

These six modules constitute the **governance core** — the agent.governance
audit anchor referenced by reconcile-cluster runs. ID allocation follows the
PM→Dev contract: `node_id=null` at PM stage, concrete IDs assigned by Rule J +
ID allocator at the dev/gatekeeper boundary.

### 8.2 Coverage envelope — 21 secondary test files

Direct-mapped (3 — verify on each PR touching the cluster):

- `agent/tests/test_preflight.py`
- `agent/tests/test_project_profile.py`
- `agent/tests/test_reconcile.py`

Reconcile sub-surface (18 — coverage anchor, run on focused reconcile work):

- `test_reconcile_batch_memory.py`, `test_reconcile_batch_memory_api.py`
- `test_reconcile_commit_sweep.py`, `test_reconcile_context.py`
- `test_reconcile_deferred_queue.py`, `test_reconcile_dropped_nodes.py`
- `test_reconcile_meta_circular.py`
- `test_reconcile_scope_cli.py`, `test_reconcile_scope_guard.py`
- `test_reconcile_scope_phase_filter.py`, `test_reconcile_scope_resolver.py`
- `test_reconcile_session.py`, `test_reconcile_session_integration.py`
- `test_reconcile_task_type.py`, `test_reconcile_type_task.py`
- `test_reconcile_v2_aggregator.py`, `test_reconcile_v2_endpoint.py`
- `test_reconcile_workflow_spec_lint.py`

The audit contract is **always-bootstrap and additive**: no node removals, no
file unmappings. The cluster purpose is to make the agent.governance surface
individually addressable by the gate (one node per module) so future drift can
be localized rather than rolled up to L3.3.

---

## 9. Related

- `docs/dev/backlog-governance.md` — parallel DB-first governance pattern for bugs/MF entries
- `docs/dev/manual-fix-sop.md` — Manual Fix SOP (includes graph repair flow)
- `docs/roles/observer.md` — observer's recovery powers (includes reconcile use)
- `agent/governance/reconcile.py` — implementation reference
- `agent/governance/graph_generator.py` — codebase scan for auto-discovery
- Backlog IDs: `OPT-DB-GRAPH` (future), `B49` (dangling-ref class), `MF-2026-04-21-003` (Plan Y landing), `OPT-BACKLOG-RECONCILE-phase-z--CLUSTER-03825084-cluster` (this audit)
