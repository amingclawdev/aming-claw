"""Reconcile — unified scan/diff/merge/sync/verify for acceptance graph.

Replaces the manual 3-flow approach (bootstrap / manual-fix / node-recovery)
with a single idempotent operation that uses a two-phase commit model:
graph.json and DB are committed together only after verify passes.

See docs/dev/reconcile-flow-design.md v2 for full design.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from .db import get_connection, _governance_root
from .graph import AcceptanceGraph
from .graph_generator import scan_codebase, save_graph_atomic
from .project_service import load_project_graph
from . import state_service
from . import audit_service
from .errors import GovernanceError


# ---------------------------------------------------------------------------
# Waive Reason Schema
# ---------------------------------------------------------------------------

class WaiveReason:
    """Standard reasons for waiving a node. Stored in evidence_json.waive_reason."""
    ORPHANED_BY_RECONCILE = "orphaned_by_reconcile"
    AUTO_CHAIN_TEMPORARY  = "auto_chain_temporary"
    PREFLIGHT_AUTOFIX     = "preflight_autofix"
    MANUAL_EXCEPTION      = "manual_exception"
    LEGACY_FROZEN         = "legacy_frozen"
    DEPRECATED            = "deprecated"


AUTO_UNWAIVE_REASONS = {
    WaiveReason.ORPHANED_BY_RECONCILE,
    WaiveReason.AUTO_CHAIN_TEMPORARY,
    WaiveReason.PREFLIGHT_AUTOFIX,
}


def should_auto_unwaive(evidence_json_str: str | None) -> bool:
    """Check if a waived node can be automatically un-waived."""
    try:
        evidence = json.loads(evidence_json_str or "{}")
        reason = evidence.get("waive_reason")
        if reason is None:
            # Legacy waived nodes without structured reason: assume auto-unwaivable
            return True
        return reason in AUTO_UNWAIVE_REASONS
    except (json.JSONDecodeError, TypeError):
        return True


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------

class ReconcileError(GovernanceError):
    def __init__(self, message: str, details: dict = None):
        super().__init__("reconcile_failed", message, 500, details)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RefSuggestion:
    node_id: str
    field: str           # "primary" | "secondary" | "test"
    old_path: str
    suggestion: str | None
    confidence: str      # "high" | "medium" | "low"
    evidence: list[str] = field(default_factory=list)


@dataclass
class DiffReport:
    stale_refs: list[RefSuggestion] = field(default_factory=list)
    orphan_nodes: list[str] = field(default_factory=list)
    unmapped_files: list[str] = field(default_factory=list)
    healthy_nodes: list[str] = field(default_factory=list)
    stale_doc_refs: list[RefSuggestion] = field(default_factory=list)  # 5h
    unmapped_docs: list[str] = field(default_factory=list)  # 5h
    stats: dict = field(default_factory=dict)


@dataclass
class MergeOptions:
    auto_fix_stale: bool = True
    require_high_confidence_only: bool = True
    remove_dead_refs: bool = True             # Remove refs with no match (file deleted)
    mark_orphans_waived: bool = False
    max_auto_fix_count: int = 50
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _deep_copy_graph(graph: AcceptanceGraph) -> AcceptanceGraph:
    """Deep copy an AcceptanceGraph (NetworkX graphs are copy-safe)."""
    new = AcceptanceGraph()
    new.G = copy.deepcopy(graph.G)
    new.gates_G = copy.deepcopy(graph.gates_G)
    return new


def _get_git_head(workspace_path: str) -> str | None:
    """Get current git HEAD SHA."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=workspace_path,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Phase 1: SCAN
# ---------------------------------------------------------------------------

def phase_scan(workspace_path: str, scan_depth: int = 3,
               exclude_patterns: list | None = None) -> tuple[set[str], dict[str, dict]]:
    """Scan filesystem. Returns (file_set, file_metadata_by_path)."""
    files = scan_codebase(workspace_path, scan_depth, exclude_patterns)
    file_set = {f["path"] for f in files}
    file_metadata = {f["path"]: f for f in files}
    return file_set, file_metadata


# ---------------------------------------------------------------------------
# Phase 2: DIFF with multi-signal confidence
# ---------------------------------------------------------------------------

def _score_suggestion(old_path: str, candidate_path: str,
                      field_type: str, file_metadata: dict) -> tuple[str, list[str]]:
    """Score a candidate replacement path. Returns (confidence, evidence)."""
    evidence = ["same_basename"]
    score = 1  # baseline: basename match (candidates pre-filtered)

    # Signal 2: parent directory similarity
    old_parts = Path(old_path).parent.parts
    new_parts = Path(candidate_path).parent.parts
    if old_parts and new_parts:
        # Count common leading parts
        common = 0
        for a, b in zip(old_parts, new_parts):
            if a == b:
                common += 1
            else:
                break
        # If at least half the old directory structure is preserved
        if common > 0 and common >= len(old_parts) * 0.5:
            evidence.append("similar_parent_dir")
            score += 1

    # Signal 3: field type constraint
    info = file_metadata.get(candidate_path, {})
    file_type = info.get("type", "source")
    # secondary can hold configs AND docs (md files); primary holds source/entrypoint/docs
    type_ok = (
        (field_type == "primary" and file_type in ("source", "entrypoint")) or
        (field_type == "primary" and candidate_path.endswith(".md")) or
        (field_type == "test" and file_type == "test") or
        (field_type == "secondary" and file_type == "config") or
        (field_type == "secondary" and candidate_path.endswith(".md"))
    )
    if type_ok:
        evidence.append("type_match")
        score += 1

    if score >= 3:
        return "high", evidence
    elif score >= 2:
        return "medium", evidence
    return "low", evidence


def _build_basename_index(file_set: set[str]) -> dict[str, list[str]]:
    """Build basename -> [full_paths] index for fast lookup."""
    index: dict[str, list[str]] = {}
    for path in file_set:
        basename = Path(path).name
        index.setdefault(basename, []).append(path)
    return index


def phase_diff(graph: AcceptanceGraph, file_set: set[str],
               file_metadata: dict[str, dict]) -> DiffReport:
    """Compare graph file references against filesystem reality."""
    report = DiffReport()
    basename_index = _build_basename_index(file_set)
    all_referenced = set()

    for node_id in graph.list_nodes():
        try:
            node_data = graph.get_node(node_id)
        except Exception:
            continue

        node_stale = False
        node_all_primary_gone = True
        primary = node_data.get("primary", [])

        for field_name in ("primary", "secondary", "test"):
            file_list = node_data.get(field_name, [])
            if not isinstance(file_list, list):
                continue
            for path in file_list:
                all_referenced.add(path)
                if path in file_set:
                    # File exists — healthy ref
                    if field_name == "primary":
                        node_all_primary_gone = False
                    continue

                # Stale ref — find suggestion
                node_stale = True
                basename = Path(path).name
                candidates = basename_index.get(basename, [])

                if len(candidates) == 0:
                    report.stale_refs.append(RefSuggestion(
                        node_id=node_id, field=field_name, old_path=path,
                        suggestion=None, confidence="low",
                        evidence=["no_basename_match"],
                    ))
                elif len(candidates) == 1:
                    confidence, evidence = _score_suggestion(
                        path, candidates[0], field_name, file_metadata)
                    report.stale_refs.append(RefSuggestion(
                        node_id=node_id, field=field_name, old_path=path,
                        suggestion=candidates[0], confidence=confidence,
                        evidence=evidence,
                    ))
                    if field_name == "primary" and confidence in ("high", "medium"):
                        node_all_primary_gone = False
                else:
                    # Multiple matches — score all, pick best
                    scored = []
                    for c in candidates:
                        conf, ev = _score_suggestion(path, c, field_name, file_metadata)
                        scored.append((conf, c, ev))
                    # Sort: high > medium > low
                    rank = {"high": 0, "medium": 1, "low": 2}
                    scored.sort(key=lambda x: rank.get(x[0], 3))
                    best_conf, best_path, best_ev = scored[0]
                    # Only suggest if best is strictly better than second
                    if len(scored) > 1 and scored[0][0] == scored[1][0]:
                        # Tie — ambiguous
                        report.stale_refs.append(RefSuggestion(
                            node_id=node_id, field=field_name, old_path=path,
                            suggestion=None, confidence="low",
                            evidence=["ambiguous_multiple_matches"],
                        ))
                    else:
                        report.stale_refs.append(RefSuggestion(
                            node_id=node_id, field=field_name, old_path=path,
                            suggestion=best_path, confidence=best_conf,
                            evidence=best_ev,
                        ))
                        if field_name == "primary" and best_conf in ("high", "medium"):
                            node_all_primary_gone = False

        if primary and node_all_primary_gone:
            report.orphan_nodes.append(node_id)
        elif not node_stale:
            report.healthy_nodes.append(node_id)

    # Unmapped files: in filesystem but not referenced by any node
    report.unmapped_files = sorted(file_set - all_referenced)

    # 5h: Stale doc refs — secondary refs pointing to missing docs
    for ref in report.stale_refs:
        if ref.field == "secondary" and (ref.old_path.startswith("docs/") or ref.old_path.endswith(".md")):
            report.stale_doc_refs.append(ref)

    # 5h: Unmapped docs — docs/ files not referenced by any node's secondary
    all_secondary = set()
    for node_id in graph.list_nodes():
        try:
            node_data = graph.get_node(node_id)
        except Exception:
            continue
        for s in node_data.get("secondary", []):
            all_secondary.add(s)
    doc_files_in_fs = {f for f in file_set if f.startswith("docs/") and f.endswith(".md")
                       and not f.startswith("docs/dev/")}  # exclude informal dev docs
    report.unmapped_docs = sorted(doc_files_in_fs - all_secondary)

    report.stats = {
        "total_nodes": graph.node_count(),
        "stale_count": len(report.stale_refs),
        "orphan_count": len(report.orphan_nodes),
        "unmapped_count": len(report.unmapped_files),
        "healthy_count": len(report.healthy_nodes),
        "stale_doc_count": len(report.stale_doc_refs),
        "unmapped_doc_count": len(report.unmapped_docs),
    }

    return report


# ---------------------------------------------------------------------------
# Phase 3: MERGE (in-memory candidate graph)
# ---------------------------------------------------------------------------

def phase_merge(candidate_graph: AcceptanceGraph, diff_report: DiffReport,
                options: MergeOptions) -> tuple[list[dict], int]:
    """Apply fixes to candidate_graph in memory. Returns (changes, auto_fix_count)."""
    changes = []
    auto_fix_count = 0

    for ref in diff_report.stale_refs:
        if ref.suggestion is None:
            # No match found — optionally remove the dead ref
            if options.remove_dead_refs:
                try:
                    node_data = candidate_graph.get_node(ref.node_id)
                    file_list = list(node_data.get(ref.field, []))
                    if ref.old_path in file_list:
                        file_list.remove(ref.old_path)
                        candidate_graph.update_node_attrs(ref.node_id, {ref.field: file_list})
                        changes.append({
                            "action": "remove_dead_ref", "node": ref.node_id,
                            "field": ref.field, "old": ref.old_path,
                        })
                        auto_fix_count += 1
                except Exception:
                    pass
            else:
                changes.append({
                    "action": "dead_ref", "node": ref.node_id,
                    "old": ref.old_path, "reason": "no replacement found",
                })
            continue

        if not options.auto_fix_stale:
            changes.append({
                "action": "skip_ref", "node": ref.node_id,
                "old": ref.old_path, "suggestion": ref.suggestion,
                "confidence": ref.confidence,
                "reason": "auto_fix_stale=false",
            })
            continue

        if options.require_high_confidence_only and ref.confidence != "high":
            changes.append({
                "action": "skip_ref", "node": ref.node_id,
                "old": ref.old_path, "suggestion": ref.suggestion,
                "confidence": ref.confidence,
                "reason": "below confidence threshold",
            })
            continue

        if auto_fix_count >= options.max_auto_fix_count:
            changes.append({
                "action": "skip_ref", "node": ref.node_id,
                "old": ref.old_path,
                "reason": "max_auto_fix_count reached",
            })
            continue

        try:
            node_data = candidate_graph.get_node(ref.node_id)
            file_list = list(node_data.get(ref.field, []))
            idx = file_list.index(ref.old_path)
            file_list[idx] = ref.suggestion
            candidate_graph.update_node_attrs(ref.node_id, {ref.field: file_list})
            changes.append({
                "action": "fix_ref", "node": ref.node_id,
                "old": ref.old_path, "new": ref.suggestion,
                "confidence": ref.confidence, "evidence": ref.evidence,
            })
            auto_fix_count += 1
        except (ValueError, Exception) as e:
            changes.append({
                "action": "error", "node": ref.node_id,
                "old": ref.old_path, "error": str(e),
            })

    for node_id in diff_report.orphan_nodes:
        changes.append({"action": "flag_orphan", "node": node_id})

    for path in diff_report.unmapped_files[:100]:  # cap log noise
        changes.append({"action": "unmapped", "path": path})

    return changes, auto_fix_count


# ---------------------------------------------------------------------------
# Phase 4: SYNC (DB transaction, uncommitted)
# ---------------------------------------------------------------------------

def phase_sync(conn, project_id: str, candidate_graph: AcceptanceGraph,
               orphan_nodes: list[str], options: dict) -> dict:
    """Sync DB state from candidate_graph. Does NOT commit."""
    results = {
        "snapshot_version": None,
        "node_states_synced": 0,
        "orphans_waived": 0,
        "unwaived": 0,
        "version_updated": False,
    }

    # 0. Safety snapshot
    try:
        results["snapshot_version"] = state_service.create_snapshot(
            conn, project_id, created_by="reconcile")
    except Exception as e:
        log.warning("reconcile: snapshot creation failed (non-critical): %s", e)

    # 1. Sync node_state from candidate_graph
    results["node_states_synced"] = state_service.init_node_states(
        conn, project_id, candidate_graph)

    # 2. Waive orphan nodes (only if mark_orphans_waived)
    if options.get("mark_orphans_waived"):
        now = _utc_iso()
        for node_id in orphan_nodes:
            evidence = json.dumps({
                "type": "reconcile",
                "waive_reason": WaiveReason.ORPHANED_BY_RECONCILE,
                "detail": "all primary files deleted",
                "reconcile_ts": now,
            })
            cur = conn.execute(
                "UPDATE node_state SET verify_status='waived', "
                "updated_by='reconcile', updated_at=?, evidence_json=? "
                "WHERE project_id=? AND node_id=? AND verify_status != 'waived'",
                (now, evidence, project_id, node_id))
            if cur.rowcount > 0:
                results["orphans_waived"] += 1
                conn.execute(
                    "INSERT INTO node_history "
                    "(project_id, node_id, from_status, to_status, role, "
                    "evidence_json, session_id, ts, version) "
                    "VALUES (?, ?, 'pending', 'waived', 'reconcile', ?, 'reconcile', ?, 1)",
                    (project_id, node_id, evidence, now))

    # 3. Un-waive nodes with auto-unwaivable reason that have live files
    now = _utc_iso()
    orphan_set = set(orphan_nodes)
    for node_id in candidate_graph.list_nodes():
        if node_id in orphan_set:
            continue
        row = conn.execute(
            "SELECT verify_status, evidence_json FROM node_state "
            "WHERE project_id=? AND node_id=?",
            (project_id, node_id)).fetchone()
        if row and row["verify_status"] == "waived":
            if should_auto_unwaive(row["evidence_json"]):
                node_data = candidate_graph.get_node(node_id)
                primary = node_data.get("primary", [])
                if primary:  # has live file refs
                    conn.execute(
                        "UPDATE node_state SET verify_status='pending', "
                        "updated_by='reconcile-unwaive', updated_at=? "
                        "WHERE project_id=? AND node_id=?",
                        (now, project_id, node_id))
                    conn.execute(
                        "INSERT INTO node_history "
                        "(project_id, node_id, from_status, to_status, role, "
                        "evidence_json, session_id, ts, version) "
                        "VALUES (?, ?, 'waived', 'pending', 'reconcile', ?, 'reconcile', ?, 1)",
                        (project_id, node_id,
                         json.dumps({"type": "reconcile", "reason": "auto-unwaive: files exist"}),
                         now))
                    results["unwaived"] += 1

    # 4. Version update
    if options.get("update_version"):
        head = _get_git_head(options.get("workspace_path", "."))
        if head:
            now = _utc_iso()
            conn.execute(
                "UPDATE project_version SET chain_version=?, git_head=?, "
                "updated_by='reconcile', updated_at=? WHERE project_id=?",
                (head, head, now, project_id))
            results["version_updated"] = True

    # DO NOT commit — wait for verify
    return results


# ---------------------------------------------------------------------------
# Phase 5: VERIFY
# ---------------------------------------------------------------------------

def _find_testable_node(graph: AcceptanceGraph) -> str | None:
    """Find a node with at least one primary file for smoke testing."""
    for node_id in graph.list_nodes():
        try:
            data = graph.get_node(node_id)
            primary = data.get("primary", [])
            if primary and isinstance(primary, list) and len(primary) > 0:
                return node_id
        except Exception:
            continue
    return None


def _find_node_with_status(conn, project_id: str, status: str) -> str | None:
    """Find any node with given verify_status."""
    row = conn.execute(
        "SELECT node_id FROM node_state WHERE project_id=? AND verify_status=? LIMIT 1",
        (project_id, status)).fetchone()
    return row["node_id"] if row else None


def phase_verify(conn, project_id: str, candidate_graph: AcceptanceGraph,
                 options: dict) -> dict:
    """Verify reconciliation is consistent. Read-only checks."""
    report = {
        "preflight": None,
        "graph_db_consistency": None,
        "impact_test": None,
        "gate_test": None,
        "version_test": None,
        "issues": [],
    }

    # 1. Preflight (informational — version/queue issues don't block ref fixes)
    try:
        from .preflight import run_preflight
        report["preflight"] = run_preflight(conn, project_id, auto_fix=False)
        # Preflight is informational for reconcile; only graph/bootstrap failures
        # are actual reconcile blockers (version mismatch is a separate concern)
        if not report["preflight"].get("ok"):
            for b in report["preflight"].get("blockers", []):
                if "bootstrap" in str(b).lower() or "system" in str(b).lower():
                    report["issues"].append(f"preflight blocker: {b}")
                else:
                    report.setdefault("warnings", []).append(f"preflight: {b}")
    except Exception as e:
        report["preflight"] = {"ok": False, "error": str(e)}

    # 2. Graph-DB consistency check
    graph_nodes = set(candidate_graph.list_nodes())
    db_rows = conn.execute(
        "SELECT node_id FROM node_state WHERE project_id = ?",
        (project_id,)).fetchall()
    db_nodes = {r["node_id"] for r in db_rows}

    in_graph_not_db = sorted(graph_nodes - db_nodes)
    in_db_not_graph = sorted(db_nodes - graph_nodes)

    report["graph_db_consistency"] = {
        "passed": len(in_graph_not_db) == 0,  # graph nodes missing from DB is a problem
        "in_graph_not_db": in_graph_not_db,
        "in_db_not_graph": in_db_not_graph,  # informational, not a blocker
    }
    if in_graph_not_db:
        report["issues"].append(
            f"Graph-DB mismatch: {len(in_graph_not_db)} graph nodes missing from DB")

    # 3. Impact analyzer smoke test
    test_node = _find_testable_node(candidate_graph)
    if test_node:
        try:
            from .impact_analyzer import ImpactAnalyzer, ImpactAnalysisRequest, FileHitPolicy
            from .state_service import _get_status_fn

            get_status = _get_status_fn(conn, project_id)
            analyzer = ImpactAnalyzer(candidate_graph, get_status)
            test_file = candidate_graph.get_node(test_node)["primary"][0]
            result = analyzer.analyze(ImpactAnalysisRequest(
                changed_files=[test_file],
                file_policy=FileHitPolicy(match_primary=True, match_secondary=True)))
            if test_node in result.get("direct_hit", []):
                report["impact_test"] = {
                    "passed": True, "node": test_node, "file": test_file,
                }
            else:
                report["impact_test"] = {
                    "passed": False, "node": test_node, "file": test_file,
                    "reason": "ImpactAnalyzer did not match file to expected node",
                }
                report["issues"].append("Impact enrichment broken: file-to-node mapping failed")
        except Exception as e:
            report["impact_test"] = {"passed": None, "error": str(e)}
    else:
        report["impact_test"] = {"passed": None, "reason": "no testable node found"}

    # 4. Gate enforcement smoke test
    pending_node = _find_node_with_status(conn, project_id, "pending")
    if pending_node:
        try:
            from .auto_chain import _check_nodes_min_status
            passed, reason = _check_nodes_min_status(
                conn, project_id, [pending_node], "qa_pass")
            if not passed:
                report["gate_test"] = {
                    "passed": True, "node": pending_node,
                    "detail": "gate correctly blocked pending node",
                }
            else:
                report["gate_test"] = {
                    "passed": False, "node": pending_node,
                    "reason": "gate did NOT block pending node — enforcement may be disabled",
                }
                report["issues"].append("Gate enforcement broken: pending node passed qa_pass check")
        except Exception as e:
            report["gate_test"] = {"passed": None, "error": str(e)}
    else:
        report["gate_test"] = {"passed": None, "reason": "no pending node to test against"}

    # 5. Version semantic check
    if options.get("update_version"):
        row = conn.execute(
            "SELECT chain_version, git_head FROM project_version WHERE project_id=?",
            (project_id,)).fetchone()
        if row and row["chain_version"] and row["git_head"]:
            if row["chain_version"] == row["git_head"]:
                report["version_test"] = {
                    "passed": True,
                    "chain_version": row["chain_version"],
                }
            else:
                report["version_test"] = {
                    "passed": False,
                    "chain_version": row["chain_version"],
                    "git_head": row["git_head"],
                }
                report["issues"].append("Version mismatch after update")
        else:
            report["version_test"] = {"passed": None, "reason": "no version record"}

    return report


# ---------------------------------------------------------------------------
# Main Orchestrator
# ---------------------------------------------------------------------------

def reconcile_project(
    project_id: str,
    workspace_path: str,
    scan_depth: int = 3,
    exclude_patterns: list | None = None,
    merge_options: MergeOptions | None = None,
    update_version: bool = False,
    dry_run: bool = False,
    force_apply: bool = False,
    operator_id: str = "observer",
) -> dict:
    """Unified reconcile: scan -> diff -> merge -> sync -> verify -> commit|rollback.

    Two-phase commit: graph.json and DB are updated together only after
    verify passes. On failure, DB rolls back and candidate_graph is discarded.

    Args:
        project_id: Governance project ID.
        workspace_path: Repository root path.
        scan_depth: Directory scan depth (default 3).
        exclude_patterns: Additional dir names to exclude from scan.
        merge_options: Control auto-fix behavior. Defaults to safe settings.
        update_version: If True, promote chain_version to current git HEAD.
        dry_run: If True, only scan+diff, return planned changes.
        force_apply: If True, skip safety gate that forces dry_run for large changes.
        operator_id: For audit trail.

    Returns:
        ReconcileReport dict with diff, changes, sync, verify, and commit status.
    """
    if merge_options is None:
        merge_options = MergeOptions(dry_run=dry_run)

    log.info("reconcile: starting for project=%s workspace=%s dry_run=%s",
             project_id, workspace_path, dry_run)

    # --- Phase 1: SCAN ---
    file_set, file_metadata = phase_scan(workspace_path, scan_depth, exclude_patterns)
    log.info("reconcile: phase_scan found %d files", len(file_set))

    # --- Phase 2: DIFF ---
    graph = load_project_graph(project_id)
    if graph is None:
        raise ReconcileError("No graph found for project. Run bootstrap first.",
                             {"project_id": project_id})
    diff_report = phase_diff(graph, file_set, file_metadata)
    log.info("reconcile: phase_diff — stale=%d orphan=%d unmapped=%d healthy=%d",
             diff_report.stats.get("stale_count", 0),
             diff_report.stats.get("orphan_count", 0),
             diff_report.stats.get("unmapped_count", 0),
             diff_report.stats.get("healthy_count", 0))

    # --- Safety gate: large change sets force dry_run ---
    force_dry_run = False
    if not dry_run and not force_apply:
        has_orphans = len(diff_report.orphan_nodes) > 0
        stale_count = len(diff_report.stale_refs)
        if stale_count > merge_options.max_auto_fix_count:
            force_dry_run = True
            log.warning("reconcile: stale_count=%d > max_auto_fix_count=%d, forcing dry_run",
                        stale_count, merge_options.max_auto_fix_count)
        elif stale_count > 5 and has_orphans:
            force_dry_run = True
            log.warning("reconcile: stale=%d with orphans, forcing dry_run", stale_count)

    # --- Confidence summary ---
    confidence_summary = {"high": 0, "medium": 0, "low": 0}
    for ref in diff_report.stale_refs:
        confidence_summary[ref.confidence] = confidence_summary.get(ref.confidence, 0) + 1

    if dry_run or force_dry_run:
        candidate = _deep_copy_graph(graph)
        changes, _ = phase_merge(candidate, diff_report, merge_options)
        return {
            "dry_run": True,
            "forced_dry_run": force_dry_run,
            "diff": asdict(diff_report),
            "planned_changes": changes,
            "confidence_summary": confidence_summary,
        }

    # --- Phase 3: MERGE (in-memory candidate) ---
    candidate_graph = _deep_copy_graph(graph)
    merge_changes, auto_fix_count = phase_merge(candidate_graph, diff_report, merge_options)
    log.info("reconcile: phase_merge — %d changes, %d auto-fixed", len(merge_changes), auto_fix_count)

    # --- Phase 4 + 5 + COMMIT in DB transaction ---
    conn = get_connection(project_id)
    graph_path = str(_governance_root() / project_id / "graph.json")
    committed = False
    sync_result = {}
    verify_result = {"issues": []}

    try:
        # Phase 4: SYNC
        sync_options = {
            "update_version": update_version,
            "workspace_path": workspace_path,
            "mark_orphans_waived": merge_options.mark_orphans_waived,
        }
        sync_result = phase_sync(
            conn, project_id, candidate_graph,
            diff_report.orphan_nodes, sync_options)
        log.info("reconcile: phase_sync — synced=%d waived=%d unwaived=%d version=%s",
                 sync_result.get("node_states_synced", 0),
                 sync_result.get("orphans_waived", 0),
                 sync_result.get("unwaived", 0),
                 sync_result.get("version_updated", False))

        # Phase 5: VERIFY
        verify_options = {"update_version": update_version}
        verify_result = phase_verify(conn, project_id, candidate_graph, verify_options)
        log.info("reconcile: phase_verify — issues=%d", len(verify_result.get("issues", [])))

        if len(verify_result.get("issues", [])) == 0:
            # COMMIT: graph + DB together
            save_graph_atomic(candidate_graph, graph_path)
            conn.commit()
            committed = True
            log.info("reconcile: COMMITTED successfully")
        else:
            # ROLLBACK
            conn.rollback()
            log.warning("reconcile: ROLLED BACK due to verify issues: %s",
                        verify_result["issues"])

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.error("reconcile: FAILED with exception, rolled back: %s", e, exc_info=True)
        raise ReconcileError(f"Reconcile failed, rolled back: {e}")
    finally:
        # Audit log (always, even on failure)
        try:
            audit_conn = get_connection(project_id)
            audit_service.record(
                audit_conn, project_id, "reconcile",
                actor=operator_id,
                ok=committed,
                committed=committed,
                auto_fix_count=auto_fix_count,
                stale_count=diff_report.stats.get("stale_count", 0),
                orphan_count=diff_report.stats.get("orphan_count", 0),
                issues=verify_result.get("issues", []),
            )
            audit_conn.commit()
            audit_conn.close()
        except Exception:
            log.debug("reconcile: audit log write failed (non-critical)", exc_info=True)
        conn.close()

    return {
        "project_id": project_id,
        "ok": committed,
        "committed": committed,
        "diff": asdict(diff_report),
        "merge_changes": merge_changes,
        "confidence_summary": confidence_summary,
        "sync": sync_result,
        "verify": verify_result,
        "rollback_snapshot": sync_result.get("snapshot_version") if not committed else None,
    }
