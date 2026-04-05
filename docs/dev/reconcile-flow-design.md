# Reconcile Flow Design v2

> Status: DRAFT v2 — incorporates Codex review feedback
> Author: Observer
> Date: 2026-04-05
> Scope: Unify bootstrap / manual-fix / node-recovery into one reusable flow
> Review: v1 reviewed by Codex; 8 suggestions evaluated, 7.5 adopted

---

## 1. Problem Statement

The project currently has **3 separate flows** that partially overlap but don't compose:

| Flow | Entry Point | What It Does | What It Doesn't Do |
|------|-------------|--------------|---------------------|
| **Bootstrap** (`bootstrap_project()`) | `POST /api/project/bootstrap` | Scan codebase, generate graph, init node_state, seed version, preflight | Preserve existing node statuses; preserve human-maintained file mappings |
| **Manual Fix** (Observer SOP) | Undocumented series of API calls | Cherry-pick, version-sync, version-update, restart | Touch node_state; update graph; run preflight; verify node enforcement |
| **Node Recovery** (`sync_node_state_from_graph()`) | `POST /api/wf/{pid}/observer-sync-node-state` | Re-materialize node_state from graph.json | Scan for new/deleted files; update file refs; version sync; preflight |

When the system drifts (manual commits, stale node references, HEAD != CHAIN_VERSION), none of these flows alone can restore it. The operator must mentally combine pieces from all three, in the right order, with no documentation to guide it.

**The reconcile flow unifies all three into a single idempotent operation.**

---

## 2. Current Architecture (What Exists)

### 2.1 Reusable Building Blocks

| Function | Location | Can Reuse? |
|----------|----------|:----------:|
| `scan_codebase(workspace_path, scan_depth)` | graph_generator.py L126 | Yes |
| `generate_graph(workspace_path, scan_depth)` | graph_generator.py L360 | Partial (overwrites; need merge mode) |
| `save_graph_atomic(graph, path)` | graph_generator.py L445 | Yes |
| `init_node_states(conn, pid, graph, sync_status)` | state_service.py L52 | Yes (idempotent) |
| `set_baseline(conn, pid, node_statuses, session)` | state_service.py L104 | Yes |
| `run_preflight(conn, pid, auto_fix)` | preflight.py L391 | Yes |
| `check_bootstrap(conn, pid)` | preflight.py L292 | Yes |
| `load_project_graph(pid)` | project_service.py | Yes |
| `AcceptanceGraph.update_node_attrs(nid, attrs)` | graph.py L375 | Yes |
| `AcceptanceGraph.affected_nodes_by_files()` | graph.py L452 | Yes |
| `create_snapshot(conn, pid)` | state_service.py L489 | Yes |
| `rollback(conn, pid, version, session)` | state_service.py L512 | Yes |
| version-sync / version-update | server.py HTTP handlers | Yes |

### 2.2 What's Missing

| Gap | Impact |
|-----|--------|
| **Graph merge** (not overwrite) | `generate_graph()` creates fresh graph, discards human-maintained mappings |
| **Stale ref detection + confidence scoring** | No scan comparing graph refs vs filesystem; no confidence-based auto-fix |
| **Orphan detection** | No way to find nodes whose files no longer exist |
| **Waive reason schema** | All waived nodes look the same; can't distinguish orphan vs manual vs legacy |
| **Node enforcement verification** | No automated test that ImpactAnalyzer to gate block works end-to-end |
| **Graph-DB atomic consistency** | No mechanism to ensure graph.json and DB state are committed together |
| **Reconcile orchestrator** | No single function that chains all steps with rollback |

---

## 3. Reconcile Flow Design

### 3.1 Overview (Two-Phase Commit Model)

```
  reconcile_project(project_id, workspace_path, options)
      |
      |-- Phase 1: SCAN (read-only)
      |     +-- scan_codebase() -> file inventory
      |
      |-- Phase 2: DIFF (read-only)
      |     +-- stale refs with confidence scoring
      |     +-- orphan nodes (all primary files gone)
      |     +-- unmapped files (new files not in any node)
      |     +-- drift report
      |
      |   [dry_run=true stops here, returns diff + planned_changes]
      |
      |   [dry_run=false safety gate: stale_refs > threshold -> force dry_run]
      |
      |-- Phase 3: MERGE (in-memory only, candidate_graph)
      |     +-- fix stale refs (high confidence only by default)
      |     +-- flag orphan nodes
      |     +-- DO NOT write graph.json yet
      |
      |-- Phase 4: SYNC (DB transaction, not yet committed)
      |     +-- create_snapshot() for rollback safety
      |     +-- init_node_states() from candidate_graph
      |     +-- orphan waive (with structured waive_reason)
      |     +-- version-update if requested
      |     +-- DO NOT commit yet
      |
      |-- Phase 5: VERIFY (read-only checks against pending state)
      |     +-- run_preflight(auto_fix=False)
      |     +-- graph-DB consistency check
      |     +-- ImpactAnalyzer smoke test
      |     +-- gate enforcement smoke test
      |     +-- version semantic check (if update_version)
      |
      |-- COMMIT or ROLLBACK
      |     +-- verify.ok=true  -> save_graph_atomic() + conn.commit()
      |     +-- verify.ok=false -> conn.rollback() + discard candidate_graph
      |
      +-- Returns: ReconcileReport (with audit trail)
```

**Key change from v1**: Graph file write and DB commit happen together AFTER verify passes. This is the **candidate graph model** (Codex suggestion A) — no partial state is ever persisted.

### 3.2 Phase 1: SCAN (read-only)

**Input**: `workspace_path`, `scan_depth` (default 3), `exclude_patterns`

**Action**: Call existing `scan_codebase()` to get file inventory.

**Output**: `Set[str]` of all file paths (posix-normalized, relative to workspace)

```python
def _phase_scan(workspace_path, scan_depth=3, exclude_patterns=None):
    from .graph_generator import scan_codebase
    files = scan_codebase(workspace_path, scan_depth, exclude_patterns)
    return {f["path"] for f in files}, files  # set for lookup, list for metadata
```

No new code needed.

### 3.3 Phase 2: DIFF (read-only, multi-signal confidence)

**Input**: Current `AcceptanceGraph`, file inventory from Phase 1

**Output**: `DiffReport`

```python
@dataclass
class RefSuggestion:
    node_id: str
    field: str                  # "primary" | "secondary" | "test"
    old_path: str
    suggestion: str | None      # None if no good match
    confidence: str             # "high" | "medium" | "low"
    evidence: list[str]         # ["same_basename", "similar_parent_dir", "type_match"]

@dataclass
class DiffReport:
    stale_refs: list[RefSuggestion]
    orphan_nodes: list[str]     # node IDs where ALL primary files are gone
    unmapped_files: list[str]   # files not referenced by any node
    healthy_nodes: list[str]    # nodes with all refs intact
    stats: dict                 # totals
```

**Multi-signal confidence algorithm**:

```python
def _score_suggestion(old_path, candidate_path, field_type, file_metadata):
    """Score a candidate replacement path. Returns (confidence, evidence)."""
    evidence = []
    score = 0

    # Signal 1: basename match (required — candidates are pre-filtered by basename)
    evidence.append("same_basename")
    score += 1

    # Signal 2: parent directory similarity
    old_parts = Path(old_path).parent.parts
    new_parts = Path(candidate_path).parent.parts
    common = len(os.path.commonpath([old_path, candidate_path]).split('/')) - 1
    if common > 0 and common >= len(old_parts) - 1:
        evidence.append("similar_parent_dir")
        score += 1

    # Signal 3: field type constraint
    file_info = file_metadata.get(candidate_path, {})
    file_type = file_info.get("type", "source")
    type_ok = (
        (field_type == "primary" and file_type in ("source", "entrypoint")) or
        (field_type == "test" and file_type == "test") or
        (field_type == "secondary" and file_type == "config")
    )
    if type_ok:
        evidence.append("type_match")
        score += 1

    # Map score to confidence
    if score >= 3:
        confidence = "high"
    elif score >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    return confidence, evidence
```

**Matching flow per stale ref**:
1. Extract basename from old_path
2. Find all files in inventory with same basename
3. If 0 matches: `suggestion=None, confidence="low"`
4. If 1 match: score it; return with scored confidence
5. If N matches: score all; pick highest; if tie, return `confidence="low"` (ambiguous)

### 3.4 Phase 3: MERGE (in-memory candidate graph)

**Input**: `DiffReport`, `AcceptanceGraph` (deep-copied as candidate), merge options

**Options**:
```python
@dataclass
class MergeOptions:
    auto_fix_stale: bool = True             # Apply high-confidence stale ref fixes
    require_high_confidence_only: bool = True  # Only auto-fix "high" confidence
    mark_orphans_waived: bool = False       # Phase A/B: off; Phase C: on
    max_auto_fix_count: int = 50            # Safety cap; exceeding forces dry_run
    dry_run: bool = False
```

**Action** (operates on candidate_graph in memory, never touches disk):

```python
def _phase_merge(candidate_graph, diff_report, options):
    changes = []
    auto_fix_count = 0

    # 1. Fix stale references (confidence-gated)
    for ref in diff_report.stale_refs:
        if not options.auto_fix_stale:
            continue
        if ref.suggestion is None:
            continue
        if options.require_high_confidence_only and ref.confidence != "high":
            changes.append({"action": "skip_ref", "node": ref.node_id,
                           "old": ref.old_path, "suggestion": ref.suggestion,
                           "confidence": ref.confidence,
                           "reason": "below confidence threshold"})
            continue
        if auto_fix_count >= options.max_auto_fix_count:
            changes.append({"action": "skip_ref", "node": ref.node_id,
                           "reason": "max_auto_fix_count reached"})
            continue

        node_data = candidate_graph.get_node(ref.node_id)
        file_list = list(node_data.get(ref.field, []))
        try:
            idx = file_list.index(ref.old_path)
            file_list[idx] = ref.suggestion
            candidate_graph.update_node_attrs(ref.node_id, {ref.field: file_list})
            changes.append({"action": "fix_ref", "node": ref.node_id,
                           "old": ref.old_path, "new": ref.suggestion,
                           "confidence": ref.confidence,
                           "evidence": ref.evidence})
            auto_fix_count += 1
        except ValueError:
            pass  # already fixed or path changed

    # 2. Flag orphan nodes (don't modify graph structure, pass to Phase 4)
    for node_id in diff_report.orphan_nodes:
        changes.append({"action": "flag_orphan", "node": node_id})

    # 3. Log unmapped files (informational)
    for path in diff_report.unmapped_files:
        changes.append({"action": "unmapped", "path": path})

    # candidate_graph stays in memory — NO save_graph_atomic() here
    return changes, auto_fix_count
```

**Key change from v1**: No disk write. candidate_graph is an in-memory deep copy. Original graph.json is untouched until final COMMIT.

### 3.5 Phase 4: SYNC (DB transaction, uncommitted)

**Input**: candidate_graph, orphan list, DB connection (transaction started)

```python
def _phase_sync(conn, project_id, candidate_graph, orphan_nodes, options):
    results = {
        "snapshot_version": None,
        "node_states_synced": 0,
        "orphans_waived": 0,
        "version_updated": False,
    }

    # 0. Safety snapshot (for rollback if needed later)
    results["snapshot_version"] = state_service.create_snapshot(conn, project_id)

    # 1. Sync node_state from candidate_graph (idempotent)
    results["node_states_synced"] = state_service.init_node_states(
        conn, project_id, candidate_graph)

    # 2. Waive orphan nodes (only if mark_orphans_waived=True)
    if options.mark_orphans_waived:
        now = _utc_iso()
        for node_id in orphan_nodes:
            evidence = json.dumps({
                "type": "reconcile",
                "waive_reason": WaiveReason.ORPHANED_BY_RECONCILE,
                "detail": "all primary files deleted",
            })
            conn.execute(
                "UPDATE node_state SET verify_status='waived', "
                "updated_by='reconcile', updated_at=?, evidence_json=? "
                "WHERE project_id=? AND node_id=? AND verify_status != 'waived'",
                (now, evidence, project_id, node_id))
            # Record in history
            conn.execute(
                "INSERT INTO node_history "
                "(project_id, node_id, from_status, to_status, role, evidence_json, session_id, ts, version) "
                "VALUES (?, ?, 'pending', 'waived', 'reconcile', ?, 'reconcile', ?, 1)",
                (project_id, node_id, evidence, now))
            results["orphans_waived"] += 1

    # 3. Version update (optional)
    if options.get("update_version"):
        head = _get_git_head(options["workspace_path"])
        now = _utc_iso()
        conn.execute(
            "UPDATE project_version SET chain_version=?, git_head=?, "
            "updated_by='reconcile', updated_at=? WHERE project_id=?",
            (head, head, now, project_id))
        results["version_updated"] = True

    # DO NOT conn.commit() — wait for Phase 5 verify
    return results
```

### 3.6 Phase 5: VERIFY (read-only against pending state)

```python
def _phase_verify(conn, project_id, candidate_graph, options):
    report = {
        "preflight": None,
        "graph_db_consistency": None,
        "impact_test": None,
        "gate_test": None,
        "version_test": None,
        "issues": [],
    }

    # 1. Preflight (all checks, no auto-fix)
    report["preflight"] = run_preflight(conn, project_id, auto_fix=False)
    if not report["preflight"].get("ok"):
        for b in report["preflight"].get("blockers", []):
            report["issues"].append(f"preflight blocker: {b}")

    # 2. Graph-DB consistency check (NEW)
    graph_nodes = set(candidate_graph.list_nodes())
    db_rows = conn.execute(
        "SELECT node_id FROM node_state WHERE project_id = ?",
        (project_id,)).fetchall()
    db_nodes = {r["node_id"] for r in db_rows}

    in_graph_not_db = graph_nodes - db_nodes
    in_db_not_graph = db_nodes - graph_nodes

    report["graph_db_consistency"] = {
        "passed": len(in_graph_not_db) == 0 and len(in_db_not_graph) == 0,
        "in_graph_not_db": sorted(in_graph_not_db),
        "in_db_not_graph": sorted(in_db_not_graph),
    }
    if not report["graph_db_consistency"]["passed"]:
        report["issues"].append(
            f"Graph-DB mismatch: {len(in_graph_not_db)} in graph not DB, "
            f"{len(in_db_not_graph)} in DB not graph")

    # 3. Impact analyzer smoke test
    test_node = _find_testable_node(candidate_graph)
    if test_node:
        from .impact_analyzer import ImpactAnalyzer, ImpactAnalysisRequest, FileHitPolicy
        from .state_service import _get_status_fn
        analyzer = ImpactAnalyzer(candidate_graph, _get_status_fn(conn, project_id))
        test_file = candidate_graph.get_node(test_node)["primary"][0]
        result = analyzer.analyze(ImpactAnalysisRequest(
            changed_files=[test_file],
            file_policy=FileHitPolicy(match_primary=True, match_secondary=True)))
        if test_node in result.get("direct_hit", []):
            report["impact_test"] = {"passed": True, "node": test_node, "file": test_file}
        else:
            report["impact_test"] = {"passed": False, "node": test_node, "file": test_file,
                                     "reason": "ImpactAnalyzer did not match file to expected node"}
            report["issues"].append("Impact enrichment broken")
    else:
        report["impact_test"] = {"passed": None, "reason": "no testable node found"}

    # 4. Gate enforcement smoke test
    pending_node = _find_node_with_status(conn, project_id, "pending")
    if pending_node:
        from .auto_chain import _check_nodes_min_status
        passed, reason = _check_nodes_min_status(conn, project_id, [pending_node], "qa_pass")
        if not passed:
            report["gate_test"] = {"passed": True, "node": pending_node,
                                   "detail": "gate correctly blocked pending node"}
        else:
            report["gate_test"] = {"passed": False, "node": pending_node,
                                   "reason": "gate did NOT block pending node"}
            report["issues"].append("Gate enforcement broken")
    else:
        report["gate_test"] = {"passed": None, "reason": "no pending node to test against"}

    # 5. Version semantic check (only if update_version was requested)
    if options.get("update_version"):
        row = conn.execute(
            "SELECT chain_version, git_head FROM project_version WHERE project_id=?",
            (project_id,)).fetchone()
        if row and row["chain_version"] == row["git_head"]:
            report["version_test"] = {"passed": True,
                                      "chain_version": row["chain_version"]}
        else:
            report["version_test"] = {"passed": False,
                                      "chain_version": row["chain_version"] if row else None,
                                      "git_head": row["git_head"] if row else None}
            report["issues"].append("Version update failed: chain_version != git_head")

    return report
```

### 3.7 Complete Orchestrator (Two-Phase Commit)

```python
def reconcile_project(
    project_id: str,
    workspace_path: str,
    scan_depth: int = 3,
    exclude_patterns: list = None,
    merge_options: MergeOptions = None,
    update_version: bool = False,
    dry_run: bool = False,
    operator_id: str = "observer",
) -> dict:
    """Unified reconcile: scan -> diff -> merge -> sync -> verify -> commit|rollback.

    Two-phase commit: graph.json and DB are updated together only after
    verify passes. On failure, DB rolls back and candidate_graph is discarded.
    """
    if merge_options is None:
        merge_options = MergeOptions(dry_run=dry_run)

    # --- Phase 1: SCAN (read-only) ---
    file_inventory, file_list = _phase_scan(workspace_path, scan_depth, exclude_patterns)
    file_metadata = {f["path"]: f for f in file_list}

    # --- Phase 2: DIFF (read-only) ---
    graph = load_project_graph(project_id)
    diff_report = diff_graph_vs_filesystem(graph, file_inventory, file_metadata)

    # --- Safety gate: large change sets force dry_run ---
    high_count = sum(1 for r in diff_report.stale_refs if r.confidence == "high")
    has_orphans = len(diff_report.orphan_nodes) > 0
    force_dry_run = (
        len(diff_report.stale_refs) > merge_options.max_auto_fix_count
        or (not dry_run and len(diff_report.stale_refs) > 5 and has_orphans)
    )

    if dry_run or force_dry_run:
        # Phase 3 in preview mode (in-memory, no side effects)
        candidate = _deep_copy_graph(graph)
        changes, _ = _phase_merge(candidate, diff_report, merge_options)
        return {
            "dry_run": True,
            "forced_dry_run": force_dry_run,
            "diff": _asdict(diff_report),
            "planned_changes": changes,
            "confidence_summary": {
                "high": sum(1 for r in diff_report.stale_refs if r.confidence == "high"),
                "medium": sum(1 for r in diff_report.stale_refs if r.confidence == "medium"),
                "low": sum(1 for r in diff_report.stale_refs if r.confidence == "low"),
            },
        }

    # --- Phase 3: MERGE (in-memory candidate) ---
    candidate_graph = _deep_copy_graph(graph)
    merge_changes, auto_fix_count = _phase_merge(candidate_graph, diff_report, merge_options)

    # --- Phase 4 + 5 + COMMIT in DB transaction ---
    conn = get_connection(project_id)
    graph_path = _governance_root() / project_id / "graph.json"
    committed = False

    try:
        # Phase 4: SYNC (DB writes, not yet committed)
        sync_options = {
            "update_version": update_version,
            "workspace_path": workspace_path,
            "mark_orphans_waived": merge_options.mark_orphans_waived,
        }
        sync_result = _phase_sync(conn, project_id, candidate_graph,
                                  diff_report.orphan_nodes, sync_options)

        # Phase 5: VERIFY (read-only checks against pending DB state)
        verify_options = {"update_version": update_version}
        verify_result = _phase_verify(conn, project_id, candidate_graph, verify_options)

        if len(verify_result.get("issues", [])) == 0:
            # COMMIT: graph + DB together
            save_graph_atomic(candidate_graph, str(graph_path))
            conn.commit()
            committed = True
        else:
            # ROLLBACK: discard everything
            conn.rollback()

    except Exception as e:
        conn.rollback()
        raise ReconcileError(f"Reconcile failed, rolled back: {e}")
    finally:
        conn.close()

    # --- Audit log ---
    _write_audit_log(project_id, operator_id, {
        "action": "reconcile",
        "committed": committed,
        "auto_fix_count": auto_fix_count,
        "sync": sync_result,
        "verify_ok": committed,
        "issues": verify_result.get("issues", []),
    })

    return {
        "project_id": project_id,
        "ok": committed,
        "diff": _asdict(diff_report),
        "merge_changes": merge_changes,
        "sync": sync_result,
        "verify": verify_result,
        "committed": committed,
        "rollback_snapshot": sync_result.get("snapshot_version") if not committed else None,
    }
```

---

## 4. Waive Reason Schema

### 4.1 Standard Enum

```python
class WaiveReason:
    """Structured reasons for waiving a node. Stored in evidence_json."""
    ORPHANED_BY_RECONCILE = "orphaned_by_reconcile"  # all primary files deleted
    AUTO_CHAIN_TEMPORARY  = "auto_chain_temporary"    # auto-chain temporary bypass
    PREFLIGHT_AUTOFIX     = "preflight_autofix"       # preflight auto-fix orphan
    MANUAL_EXCEPTION      = "manual_exception"        # human decided to exempt
    LEGACY_FROZEN         = "legacy_frozen"            # explicitly frozen module
    DEPRECATED            = "deprecated"               # module marked for removal
```

### 4.2 evidence_json Format

```json
{
    "type": "reconcile",
    "waive_reason": "orphaned_by_reconcile",
    "detail": "all primary files deleted",
    "reconcile_ts": "2026-04-05T12:00:00Z",
    "operator": "observer"
}
```

### 4.3 Un-waive Rules (by reason)

| waive_reason | Auto un-waive by reconcile? | Auto un-waive by auto-chain? |
|--------------|:---------------------------:|:----------------------------:|
| `orphaned_by_reconcile` | Yes (if files return) | Yes |
| `auto_chain_temporary` | Yes | Yes |
| `preflight_autofix` | Yes | Yes |
| `manual_exception` | **No** | **No** |
| `legacy_frozen` | **No** | **No** |
| `deprecated` | **No** | **No** |

Un-waive check in Phase 4 and auto-chain:

```python
AUTO_UNWAIVE_REASONS = {
    WaiveReason.ORPHANED_BY_RECONCILE,
    WaiveReason.AUTO_CHAIN_TEMPORARY,
    WaiveReason.PREFLIGHT_AUTOFIX,
}

def _should_auto_unwaive(evidence_json_str):
    """Check if a waived node can be automatically un-waived."""
    try:
        evidence = json.loads(evidence_json_str or "{}")
        return evidence.get("waive_reason") in AUTO_UNWAIVE_REASONS
    except (json.JSONDecodeError, TypeError):
        # Legacy waived nodes without structured reason: assume auto-unwaivable
        return True
```

---

## 5. API Design

### 5.1 HTTP Endpoint

```
POST /api/wf/{project_id}/reconcile
```

**Request Body**:
```json
{
    "workspace_path": "/path/to/repo",
    "scan_depth": 3,
    "dry_run": false,
    "auto_fix_stale": true,
    "require_high_confidence_only": true,
    "max_auto_fix_count": 50,
    "mark_orphans_waived": false,
    "update_version": false,
    "operator_id": "observer"
}
```

**Safety rules**:
- `stale_refs <= 5` and no orphans: direct apply allowed
- `stale_refs > 5` OR orphans exist: API forces dry_run on first call; second call with `force_apply: true` needed
- `auto_fix_count > max_auto_fix_count`: remaining refs skipped (reported in response)

**Response (dry_run)**:
```json
{
    "dry_run": true,
    "forced_dry_run": false,
    "diff": {
        "stale_refs": [
            {
                "node_id": "L1.3",
                "field": "primary",
                "old_path": "governance/server.py",
                "suggestion": "agent/governance/server.py",
                "confidence": "high",
                "evidence": ["same_basename", "similar_parent_dir", "type_match"]
            }
        ],
        "orphan_nodes": ["L2.7"],
        "unmapped_files": ["agent/new_module.py"],
        "stats": {
            "total_nodes": 55,
            "stale_count": 92,
            "orphan_count": 1,
            "unmapped_count": 3,
            "healthy_count": 40
        }
    },
    "planned_changes": [
        {"action": "fix_ref", "node": "L1.3", "old": "governance/server.py",
         "new": "agent/governance/server.py", "confidence": "high",
         "evidence": ["same_basename", "similar_parent_dir", "type_match"]},
        {"action": "skip_ref", "node": "L4.2", "old": "tests/old_test.py",
         "suggestion": "agent/tests/new_test.py", "confidence": "medium",
         "reason": "below confidence threshold"},
        {"action": "flag_orphan", "node": "L2.7"},
        {"action": "unmapped", "path": "agent/new_module.py"}
    ],
    "confidence_summary": {"high": 78, "medium": 10, "low": 4}
}
```

**Response (apply, committed)**:
```json
{
    "project_id": "aming-claw",
    "ok": true,
    "committed": true,
    "diff": { "..." },
    "merge_changes": [ "..." ],
    "sync": {
        "snapshot_version": 3,
        "node_states_synced": 3,
        "orphans_waived": 0,
        "version_updated": false
    },
    "verify": {
        "preflight": {"ok": true},
        "graph_db_consistency": {"passed": true, "in_graph_not_db": [], "in_db_not_graph": []},
        "impact_test": {"passed": true, "node": "L1.3", "file": "agent/governance/server.py"},
        "gate_test": {"passed": true, "node": "L4.2",
                      "detail": "gate correctly blocked pending node"},
        "version_test": null,
        "issues": []
    }
}
```

**Response (apply, rolled back)**:
```json
{
    "project_id": "aming-claw",
    "ok": false,
    "committed": false,
    "verify": {
        "issues": ["Impact enrichment broken", "Gate enforcement broken"]
    },
    "rollback_snapshot": 3
}
```

### 5.2 MCP Tool

```
reconcile_project(project_id, dry_run=true)
```

Maps to the same HTTP endpoint.

---

## 6. Relationship to Existing Flows

### 6.1 Bootstrap vs Reconcile

```
bootstrap_project()          reconcile_project()
--------------------        --------------------
scan_codebase()      <----- scan_codebase()         [SHARED]
generate_graph()             diff_graph_vs_filesystem [NEW - diff, not overwrite]
                             _phase_merge()           [NEW - selective update]
save_graph_atomic()  <----- save_graph_atomic()      [SHARED - but after verify]
init_node_states()   <----- init_node_states()       [SHARED]
version seed         <----- version update           [SHARED mechanism]
check_bootstrap()    <----- run_preflight()           [SHARED]
                             graph_db_consistency     [NEW]
                             impact_test              [NEW]
                             gate_test                [NEW]
```

Bootstrap is a special case of reconcile where the graph doesn't exist yet.

### 6.2 Manual Fix SOP reduces to

| Before (manual) | After (reconcile) |
|------------------|--------------------|
| Cherry-pick + version-sync + version-update | `reconcile(update_version=true)` |
| Manually verify version-check ok=true | Included in Phase 5 verify |
| (missing) Verify node refs correct | Included in Phase 5 verify |
| (missing) Verify gate enforcement works | Included in Phase 5 verify |

---

## 7. Implementation Plan (Phased)

### Phase A1: Scan + Diff + Dry Run (read-only, zero risk)

| Item | Detail |
|------|--------|
| New file | `agent/governance/reconcile.py` (~120 lines) |
| Functions | `_phase_scan()`, `diff_graph_vs_filesystem()`, `_score_suggestion()` |
| Test file | `agent/tests/test_reconcile.py` (~80 lines) |
| Tests | diff accuracy, confidence scoring, edge cases (duplicate basenames, etc.) |
| Deliverable | `POST /api/wf/{pid}/reconcile` with `dry_run=true` only |
| Risk | Zero. Read-only. |

### Phase A2: Merge + Sync + Verify + Commit (ref fixes only)

| Item | Detail |
|------|--------|
| Add to `reconcile.py` | `_phase_merge()`, `_phase_sync()`, `_phase_verify()`, `reconcile_project()` (~180 lines) |
| Key feature | Two-phase commit (candidate graph + DB transaction) |
| Constraint | `mark_orphans_waived=false` (no waive changes yet) |
| Test additions | merge idempotency, rollback on verify failure, graph-DB consistency |
| Deliverable | Can fix stale refs. Cannot change waive states. |
| Risk | Low. Only file ref lists change. Snapshot + rollback available. |

### Phase B: Verify Hardening

| Item | Detail |
|------|--------|
| Add to verify | Graph-DB consistency check, version semantic check |
| New test | `test_reconcile_idempotent` — run reconcile twice, assert second dry_run shows 0 changes |
| Modified | `server.py` (+40 lines for HTTP handler), `mcp_server.py` (+20 lines for MCP tool) |
| Deliverable | Full verify suite. Idempotency proven by test. |

### Phase C: Orphan Waive + Waive Reason Schema

| Item | Detail |
|------|--------|
| New | `WaiveReason` enum in `reconcile.py` or `enums.py` |
| Modified | `_phase_sync()` orphan waive with structured evidence |
| Modified | `preflight.py` check_graph() — waived-node audit warning |
| Constraint | Only `orphaned_by_reconcile` reason initially |
| Enable | `mark_orphans_waived=true` in API |
| Deliverable | Orphan nodes properly categorized. |

### Phase D: Runtime Un-waive (Auto-Chain Integration)

| Item | Detail |
|------|--------|
| Modified | `auto_chain.py` L709-735 — waive-on-change in impact enrichment |
| Modified | `_phase_sync()` — reason-aware un-waive in reconcile |
| New | `_should_auto_unwaive()` helper |
| Test | waive node -> change its file -> confirm un-waived |
| Deliverable | Waive is no longer permanent. |

### Phase E: Bootstrap Delegation (optional, low priority)

| Item | Detail |
|------|--------|
| Modified | `project_service.bootstrap_project()` delegates to reconcile when graph exists |
| Deliverable | Single code path for all graph operations. |

---

## 8. Safety Properties

| Property | How Reconcile Ensures It |
|----------|--------------------------|
| **Atomic** | Graph.json and DB committed together after verify passes. On failure, DB rolls back and candidate_graph is discarded. (Two-phase commit model) |
| **Idempotent** | Running twice produces same result. Second dry_run shows 0 planned changes. Proven by automated test. |
| **Non-destructive** | Never deletes nodes. Never overwrites human-maintained attrs (title, layer, gates, deps). Only updates file ref lists. |
| **Auditable** | All changes: audit_log with operator_id, timestamp, action details. API response includes full change list. |
| **Rollback** | `create_snapshot()` before Phase 4. On verify failure: auto-rollback. Manual rollback via `state_service.rollback()`. |
| **Confidence-gated** | Only high-confidence ref fixes applied automatically. Medium/low require human review via dry_run. |
| **Threshold-protected** | `max_auto_fix_count` caps batch size. Large change sets force dry_run. |
| **Reason-aware waive** | Waive reason is schema-enforced. Un-waive only applies to auto-generated waives, not human decisions. |

---

## 9. Decision Points (Updated)

| # | Decision | Resolution |
|---|----------|------------|
| D1 | Auto-create nodes for unmapped files? | **No** — node hierarchy is human-designed |
| D2 | Orphan nodes: delete or waive? | **Waive** with structured `waive_reason` |
| D3 | Run through governance workflow? | **Direct API** but with mandatory audit log + operator_id |
| D4 | Mandatory dry_run? | **Threshold-based**: stale_refs <= 5 and no orphans = direct apply; otherwise force dry_run first |
| D5 | Version update included? | **Optional** via `update_version` flag |
| D6 | Un-waive automatic? | **Reason-aware**: auto for `orphaned_by_reconcile`/`auto_chain_temporary`/`preflight_autofix`; manual for `manual_exception`/`legacy_frozen`/`deprecated` |
| D7 | New file or extend project_service? | **New `reconcile.py`** — single responsibility |

---

## 10. Codex Review Adoption Log

| # | Codex Suggestion | Adopted? | Detail |
|---|------------------|:--------:|--------|
| 1 | Graph-DB atomic consistency (two-phase commit) | **Full** | Candidate graph model; commit after verify |
| 2 | Multi-signal confidence for stale refs | **Adopted 3/5 signals** | basename + parent dir similarity + field type constraint. Git rename and extension check deferred. |
| 3 | Waive reason schema | **Full** | 6 standard reasons in `WaiveReason` enum |
| 4 | Reason-aware un-waive | **Full** | `AUTO_UNWAIVE_REASONS` set; manual/legacy/deprecated exempt |
| 5 | Verify: graph-DB consistency + version check | **2/3 adopted** | Graph-DB check and version check in verify. Idempotent re-run moved to test suite. |
| 6 | API safety thresholds | **2/4 adopted** | `max_auto_fix_count` and `require_high_confidence_only`. Deferred: `max_unwaive_count`, `apply_scope`. |
| 7 | Phased implementation (separate ref fix from waive) | **Full** | A1 -> A2 -> B -> C -> D -> E |
| 8 | Decision point adjustments (D3 audit, D4 threshold) | **Full** | D3: audit mandatory. D4: threshold-based enforcement. |

---

## 11. Flow Diagrams

### 11.1 Bootstrap Flow

```
┌─────────────────────────────────────────────────────────────┐
│                      BOOTSTRAP FLOW                         │
│                                                             │
│  ┌──────────┐    ┌──────────────┐    ┌───────────────┐     │
│  │ Config    │───>│ init_project │───>│ scan_codebase │     │
│  │ Discovery │    │ (DB tables)  │    │ (filesystem)  │     │
│  └──────────┘    └──────────────┘    └───────┬───────┘     │
│                                              │              │
│                                              v              │
│  ┌──────────────┐    ┌──────────────┐    ┌───────────────┐ │
│  │ version seed │<───│init_node_    │<───│generate_graph │ │
│  │ "bootstrap"  │    │states(sync)  │    │+ save_graph   │ │
│  └──────┬───────┘    └──────────────┘    └───────────────┘ │
│         │                                                   │
│         v                                                   │
│  ┌──────────────────────────────────────┐                  │
│  │ check_bootstrap                      │                  │
│  │  * graph file exists?                │                  │
│  │  * node_state rows match graph?      │                  │
│  │  * chain_version == git HEAD?        │                  │
│  └──────────────┬───────────────────────┘                  │
│                 │                                           │
│         ┌───────┴───────┐                                  │
│         │               │                                  │
│      PASS            FAIL                                  │
│    (ready)      (manual fix                                │
│                  or reconcile)                              │
└─────────────────────────────────────────────────────────────┘

Version Gate state during bootstrap:
  * bootstrap start: gate not yet active (chain_version not written)
  * bootstrap end: version seed writes chain_version -> gate becomes active
  * SERVER_VERSION set at server.py startup = git HEAD (immutable)
```

### 11.2 Reconcile Flow (Two-Phase Commit)

```
┌─────────────────────────────────────────────────────────────────────┐
│                RECONCILE FLOW (Two-Phase Commit)                    │
│                                                                     │
│  Phase 1: SCAN                                                      │
│  ┌────────────────────────────────────────┐                         │
│  │ scan_codebase(exclude=[.claude,        │                         │
│  │   shared-volume, runtime])             │                         │
│  │ -> file_map: {path: {type, size, ...}} │                         │
│  └────────────────────┬───────────────────┘                         │
│                       v                                             │
│  Phase 2: DIFF                                                      │
│  ┌────────────────────────────────────────┐                         │
│  │ For each node in graph:                │                         │
│  │   ref exists on disk? -> healthy       │                         │
│  │   ref missing? -> stale_ref            │                         │
│  │     +-- _score_suggestion():           │                         │
│  │        signal 1: basename match        │  ┌─────────────────┐   │
│  │        signal 2: parent dir similarity │  │ Confidence:      │   │
│  │        signal 3: type constraint       │  │  3/3 = HIGH      │   │
│  │          (.md -> primary/secondary OK) │  │  2/3 = MEDIUM    │   │
│  │     -> RefSuggestion(confidence, ...)  │  │  0-1 = LOW       │   │
│  │                                        │  └─────────────────┘   │
│  │ Files in scan but not in any node?     │                         │
│  │   -> unmapped_files                    │                         │
│  │ Nodes with 0 remaining refs?           │                         │
│  │   -> orphan_nodes                      │                         │
│  │                                        │                         │
│  │ Output: DiffReport                     │                         │
│  └────────────────────┬───────────────────┘                         │
│                       v                                             │
│  Phase 3: MERGE (in-memory only -- candidate_graph)                 │
│  ┌────────────────────────────────────────┐                         │
│  │ candidate = deep_copy(graph)           │                         │
│  │                                        │                         │
│  │ For each stale_ref:                    │                         │
│  │   HIGH confidence -> fix_ref           │   ┌──────────────────┐ │
│  │   MEDIUM -> skip (log)                 │   │ SAFETY GATE:     │ │
│  │   LOW + no match -> remove_dead_ref    │   │ stale>5 + orphan │ │
│  │                                        │   │ -> force dry_run │ │
│  │ orphan nodes -> flag for waive         │   │ force_apply=True │ │
│  │                                        │   │ to override      │ │
│  │ Respect max_auto_fix_count             │   └──────────────────┘ │
│  │                                        │                         │
│  │ Output: candidate_graph (NOT saved)    │                         │
│  └────────────────────┬───────────────────┘                         │
│                       v                                             │
│  Phase 4: SYNC (DB transaction -- uncommitted)                      │
│  ┌────────────────────────────────────────┐                         │
│  │ create_snapshot() <- safety backup     │                         │
│  │ init_node_states(sync=True)            │                         │
│  │ waive orphans (waive_reason schema):   │                         │
│  │   "orphaned_by_reconcile"              │                         │
│  │ un-waive recovered nodes:              │                         │
│  │   only if reason in AUTO_UNWAIVE set   │                         │
│  │   {orphaned_by_reconcile,              │                         │
│  │    auto_chain_temporary,               │                         │
│  │    preflight_autofix}                  │                         │
│  │ optional: update_version(git HEAD)     │                         │
│  │                                        │                         │
│  │ DB writes NOT committed yet            │                         │
│  └────────────────────┬───────────────────┘                         │
│                       v                                             │
│  Phase 5: VERIFY (decides COMMIT or ROLLBACK)                       │
│  ┌────────────────────────────────────────┐                         │
│  │ 1. preflight(non-blocking for version) │                         │
│  │ 2. graph<->DB consistency check        │                         │
│  │    (every graph node has DB row)       │                         │
│  │ 3. ImpactAnalyzer smoke test           │                         │
│  │    (wf_impact returns >0 matches)      │                         │
│  │ 4. Gate enforcement smoke test         │                         │
│  │    (pending nodes blocked by gate)     │                         │
│  │ 5. Version semantic check              │                         │
│  └────────────────────┬───────────────────┘                         │
│                       │                                             │
│              ┌────────┴────────┐                                    │
│              v                 v                                    │
│     ALL PASS               ANY FAIL                                │
│  ┌──────────────┐     ┌──────────────┐                             │
│  │save_graph_   │     │conn.rollback │                             │
│  │atomic()      │     │discard       │                             │
│  │conn.commit() │     │candidate     │                             │
│  │              │     │graph         │                             │
│  │= COMMITTED   │     │= ROLLED BACK │                             │
│  └──────────────┘     └──────────────┘                             │
└─────────────────────────────────────────────────────────────────────┘
```

### 11.3 Version Gate Lifecycle

```
┌──────────────────────────────────────────────────────────────────────┐
│                    VERSION GATE LIFECYCLE                             │
│                                                                      │
│  Two version values:                                                 │
│  ╔═══════════════════════════════════════════════╗                   │
│  ║ SERVER_VERSION (immutable per process)         ║                   │
│  ║ Set ONCE at server.py startup:                ║                   │
│  ║   git rev-parse --short HEAD -> "abc1234"     ║                   │
│  ║ Never changes until service restart           ║                   │
│  ╚═══════════════════════════════════════════════╝                   │
│                                                                      │
│  ╔═══════════════════════════════════════════════╗                   │
│  ║ chain_version (mutable in DB)                 ║                   │
│  ║ Updated by:                                   ║                   │
│  ║  * bootstrap: version seed "bootstrap"        ║                   │
│  ║  * executor: sync every 60s (git HEAD)        ║                   │
│  ║  * merge stage: _finalize_version_sync()      ║                   │
│  ║  * reconcile: update_version option           ║                   │
│  ║  * API: POST /api/version-update              ║                   │
│  ╚═══════════════════════════════════════════════╝                   │
│                                                                      │
│  _gate_version_check() decision flow:                                │
│  ┌─────────────────────────────────────────┐                        │
│  │  1. _DISABLE_VERSION_GATE == True?      │--YES--> PASS           │
│  │     |NO                                 │        (gate disabled) │
│  │     v                                   │                        │
│  │  2. metadata.skip_version_check?        │--YES--> PASS           │
│  │     |NO                                 │        (task bypass)   │
│  │     v                                   │                        │
│  │  3. metadata.observer_merge?            │--YES--> PASS           │
│  │     |NO                                 │   (observer merge      │
│  │     v                                   │    bypass)             │
│  │  4. metadata has reconciliation_lane    │                        │
│  │     + observer_authorized == True?      │--YES--> PASS           │
│  │     |NO                                 │   (reconciliation      │
│  │     v                                   │    bypass)             │
│  │  5. parent_task has parallel_plan       │                        │
│  │     metadata? (governed dirty)          │--YES--> PASS           │
│  │     |NO                                 │   (governed dirty-     │
│  │     v                                   │    workspace)          │
│  │  6. Read DB: chain_version, git_head,   │                        │
│  │     dirty_files                         │                        │
│  │     v                                   │                        │
│  │  7. dirty_files (filter .claude/)?      │                        │
│  │     |                                   │                        │
│  │     +-- non-.claude files exist --------│-------> FAIL           │
│  │     |                                   │  "dirty workspace"     │
│  │     v                                   │                        │
│  │  8. SERVER_VERSION == git HEAD?         │                        │
│  │     |                                   │                        │
│  │     +-- mismatch ----------------------│-------> FAIL           │
│  │     |                                   │  "server version       │
│  │     v                                   │   mismatch. Restart"   │
│  │  9. chain_version == git HEAD?          │                        │
│  │     |                                   │                        │
│  │     +-- mismatch ----------------------│-------> FAIL           │
│  │     |                                   │  "chain version        │
│  │     v                                   │   mismatch"            │
│  │  ALL MATCH ----------------------------│-------> PASS           │
│  │                                         │  "version match"       │
│  └─────────────────────────────────────────┘                        │
└──────────────────────────────────────────────────────────────────────┘
```

### 11.4 Version Gate: Disable/Enable Lifecycle

```
Timeline ──────────────────────────────────────────────────────>

 ┌─────────┐
 │ Service  │  SERVER_VERSION = git rev-parse HEAD -> "v1"
 │ Start    │
 └────┬────┘
      v
 ┌──────────────────┐
 │ Normal Operation  │  chain_version = "v1" (DB)
 │                   │  gate: SERVER_VERSION("v1") == HEAD("v1") == chain("v1")
 │                   │  Result: PASS
 └────────┬─────────┘
          │
          │  Developer commits code -> HEAD becomes "v2"
          v
 ┌──────────────────┐
 │ Gate BLOCKS       │  SERVER_VERSION("v1") != HEAD("v2")
 │ auto_chain stops  │  "server version mismatch. Restart service"
 └────────┬─────────┘
          │
          │  === Resolution Options ===
          │
          ├── Option A: Restart Service ──────────────────────┐
          │                                                    v
          │   ┌──────────────────┐    ┌──────────────────┐
          │   │ Service Restart   │    │ chain_version =  │
          │   │ SERVER_VERSION =  │───>│ "v2" (executor   │
          │   │ "v2" (re-read    │    │  sync or merge   │
          │   │  HEAD)           │    │  finalize)       │
          │   └──────────────────┘    └───────┬──────────┘
          │                                    v
          │                            Gate: PASS (all "v2")
          │
          ├── Option B: Reconcile (partial fix) ──────────────┐
          │                                                    v
          │   ┌──────────────────┐    ┌──────────────────┐
          │   │ POST /api/wf/    │    │ Phase 4 SYNC:    │
          │   │ {pid}/reconcile  │───>│ chain_version -> │
          │   │ {update_version: │    │ "v2" (DB)        │
          │   │  true}           │    │                  │
          │   └──────────────────┘    └───────┬──────────┘
          │                                    v
          │                chain("v2")==HEAD("v2") OK
          │                BUT SERVER_VERSION still "v1"
          │                -> Still need service restart
          │
          └── Option C: Metadata Bypass (per-task) ───────────┐
                                                               v
              ┌──────────────────────────────────────────────┐
              │ skip_version_check: True  -> single task     │
              │ observer_merge: True      -> observer merge  │
              │ reconciliation_lane +                        │
              │   observer_authorized     -> reconcile       │
              │ parent parallel_plan      -> governed dirty  │
              │ _DISABLE_VERSION_GATE=True-> global (code)   │
              └──────────────────────────────────────────────┘

Summary Table:

  Scenario               │ Gate State │ Resolution
  ───────────────────────┼────────────┼────────────────────────
  After bootstrap        │ Active     │ version seed written
  Normal (3-way match)   │ PASS       │ No action needed
  After code commit      │ BLOCK      │ Restart service
  After merge finalize   │ PASS       │ _finalize_version_sync
  After reconcile        │ Partial    │ + restart for full fix
  Dirty workspace        │ BLOCK      │ Clean files or bypass
  Emergency task         │ Bypass     │ metadata flags (5 types)
```
