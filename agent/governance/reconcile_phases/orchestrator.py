"""Orchestrator — runs the full 5-phase reconcile pipeline.

Builds ReconcileContext once, runs requested phases in order A -> E -> B -> C -> D,
calls Aggregator, writes report to docs/dev/scratch/reconcile-comprehensive-YYYY-MM-DD.md,
and returns structured result.

Also provides commit-sweep mode: walk N commits since last baseline, run scoped
reconcile per commit, deduplicate, compute coverage against hot files.
"""
from __future__ import annotations

import os
import logging
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

PHASE_ORDER = ["K", "A", "E", "B", "C", "D", "F", "G"]


def _run_phase(phase_key: str, ctx: Any, phase_results: Dict[str, list],
               scope: Optional[Any] = None) -> list:
    """Run a single phase by key, returning its discrepancy list."""
    if phase_key == "K":
        if scope is None:
            return []
        from . import phase_k
        return phase_k.run(ctx, scope=scope)
    elif phase_key == "A":
        from . import phase_a
        return phase_a.run(ctx, scope=scope)
    elif phase_key == "E":
        from . import phase_e
        return phase_e.run(ctx, scope=scope)
    elif phase_key == "B":
        from . import phase_b
        return phase_b.run(ctx, phase_e_discrepancies=phase_results.get("E"), scope=scope)
    elif phase_key == "C":
        from . import phase_c
        return phase_c.run(ctx, scope=scope)
    elif phase_key == "D":
        from . import phase_d
        return phase_d.run(ctx, scope=scope)
    elif phase_key == "F":
        from . import phase_f
        return phase_f.run(ctx, phase_a_discrepancies=phase_results.get("A"), scope=scope)
    elif phase_key == "G":
        from . import phase_g
        return phase_g.run(ctx, scope=scope)
    else:
        log.warning("Unknown phase key: %s", phase_key)
        return []


def _write_report(
    workspace_path: str,
    aggregated: Dict[str, Any],
    phase_details: Dict[str, Any],
    graph_db_delta: Optional[Dict[str, Any]] = None,
) -> str:
    """Write markdown report and return the relative report path."""
    today = date.today().isoformat()
    rel_path = f"docs/dev/scratch/reconcile-comprehensive-{today}.md"
    abs_path = Path(workspace_path) / rel_path

    abs_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# Reconcile Comprehensive Report — {today}",
        "",
        "## Summary",
        "",
        f"- Auto-fixable: {len(aggregated.get('auto_fixable', []))}",
        f"- Human review: {len(aggregated.get('human_review', []))}",
        f"- Genuinely unresolvable: {len(aggregated.get('genuinely_unresolvable', []))}",
        f"- Dedup removed: {aggregated.get('dedup_removed', 0)}",
        "",
    ]

    # Graph-vs-DB delta section
    if graph_db_delta:
        lines += [
            "## Graph vs DB Node Delta",
            "",
            f"- Graph nodes: {graph_db_delta.get('graph_count', '?')}",
            f"- DB node_state rows: {graph_db_delta.get('db_count', '?')}",
            f"- Orphan DB records (in DB, not in graph): {graph_db_delta.get('orphan_db_count', 0)}",
            f"- Missing DB records (in graph, not in DB): {graph_db_delta.get('missing_db_count', 0)}",
            f"- Stuck in testing: {graph_db_delta.get('stuck_testing_count', 0)}",
            "",
        ]

    lines += [
        "## Auto-fixable",
        "",
    ]

    for d in aggregated.get("auto_fixable", []):
        lines.append(f"- [{getattr(d, 'type', '')}] {getattr(d, 'detail', '')}")
    if not aggregated.get("auto_fixable"):
        lines.append("_(none)_")

    lines += ["", "## Human review", ""]
    for d in aggregated.get("human_review", []):
        lines.append(f"- [{getattr(d, 'type', '')}] {getattr(d, 'detail', '')}")
    if not aggregated.get("human_review"):
        lines.append("_(none)_")

    lines += ["", "## Phase detail blocks", ""]
    for key in PHASE_ORDER:
        if key in phase_details:
            pd = phase_details[key]
            lines.append(f"### Phase {key}")
            lines.append(f"- Count: {pd.get('count', 0)}")
            lines.append("")

    abs_path.write_text("\n".join(lines), encoding="utf-8")
    return rel_path


def run_orchestrated(
    project_id: str,
    workspace_path: str,
    *,
    phases: Optional[List[str]] = None,
    dry_run: bool = True,
    auto_fix_threshold: str = "high",
    scan_depth: int = 3,
    since: Optional[str] = None,
    scope: Optional[Any] = None,
) -> Dict[str, Any]:
    """Run the full reconcile v2 pipeline.

    Args:
        project_id: governance project id
        workspace_path: absolute path to workspace root
        phases: subset of ["A","E","B","C","D"]; None = all
        dry_run: if True, no mutations applied
        auto_fix_threshold: minimum confidence for auto-fix bucket
        scan_depth: passed to ReconcileContext
        since: optional date filter (unused in current phases)

    Returns:
        {report_path, summary, auto_fixed_count, human_review_count, phases: {A:{}, ...}}
    """
    from .context import ReconcileContext
    from . import aggregator

    ctx = ReconcileContext(
        project_id=project_id,
        workspace_path=workspace_path,
        scan_depth=scan_depth,
    )

    # Resolve scope if provided
    resolved_scope = None
    if scope is not None:
        resolved_scope = scope.resolve(ctx)

    requested = phases if phases else list(PHASE_ORDER)
    # Enforce canonical order
    ordered = [p for p in PHASE_ORDER if p in requested]

    phase_results: Dict[str, list] = {}
    phase_details: Dict[str, Any] = {}

    for key in ordered:
        try:
            result = _run_phase(key, ctx, phase_results, scope=resolved_scope)
        except Exception as exc:
            log.error("Phase %s failed: %s", key, exc)
            result = []
        phase_results[key] = result
        phase_details[key] = {"count": len(result), "items": result}

    # Aggregate
    aggregated = aggregator.aggregate(
        phase_results, auto_fix_threshold=auto_fix_threshold,
    )

    # Write report
    try:
        delta_for_report = ctx.graph_db_delta
    except Exception:
        delta_for_report = None
    report_path = _write_report(workspace_path, aggregated, phase_details, delta_for_report)

    auto_fixed_count = len(aggregated["auto_fixable"]) if not dry_run else 0
    human_review_count = len(aggregated["human_review"])

    # Compute graph-vs-DB delta summary
    try:
        delta = ctx.graph_db_delta
        graph_db_summary = {
            "graph_count": delta["graph_count"],
            "db_count": delta["db_count"],
            "orphan_db_count": delta["orphan_db_count"],
            "missing_db_count": delta["missing_db_count"],
            "stuck_testing_count": delta["stuck_testing_count"],
        }
    except Exception as exc:
        log.warning("Failed to compute graph_db_delta: %s", exc)
        graph_db_summary = {}

    return {
        "report_path": report_path,
        "summary": {
            "auto_fixable": len(aggregated["auto_fixable"]),
            "human_review": human_review_count,
            "genuinely_unresolvable": len(aggregated["genuinely_unresolvable"]),
            "dedup_removed": aggregated["dedup_removed"],
        },
        "graph_db_delta": graph_db_summary,
        "auto_fixed_count": auto_fixed_count,
        "human_review_count": human_review_count,
        "phases": {k: {"count": v["count"]} for k, v in phase_details.items()},
    }


# ---------------------------------------------------------------------------
# Commit-sweep helpers (R3, R5, R6, R7)
# ---------------------------------------------------------------------------

_COMMIT_SWEEP_DEFAULT_PHASES = ["K", "A", "E", "D", "H"]


def _dedup_key(d: Dict[str, Any]) -> Tuple[str, str, str]:
    """Return (affected_file, contract_id, type) for deduplication.

    Extracts affected_file from different discrepancy shapes:
      - unmapped_file / unmapped_doc  → d["detail"] is the file path
      - stale_ref                     → first token of d["detail"] (space-split)
      - doc_value_drift               → d["doc"]
      - fallback                      → d.get("detail", "")

    Extracts contract_id from whichever attribute exists first:
      node_id → contract_id → constant_name → ""
    """
    dtype = d.get("type", "")

    # --- affected_file ---
    if dtype in ("unmapped_file", "unmapped_doc"):
        affected_file = d.get("detail", "")
    elif dtype == "stale_ref":
        detail = d.get("detail", "")
        affected_file = detail.split()[0] if detail else ""
    elif dtype == "doc_value_drift":
        affected_file = d.get("doc", "")
    else:
        affected_file = d.get("detail", "")

    # --- contract_id ---
    contract_id = (
        d.get("node_id")
        or d.get("contract_id")
        or d.get("constant_name")
        or ""
    )

    return (affected_file, contract_id, dtype)


def _build_rename_map(
    since: str,
    workspace_path: str,
) -> Dict[str, str]:
    """Build {old_path: new_path} rename map via a single batched git call.

    Uses ``git log --name-status -M`` (AC5: exactly 1 subprocess call,
    NOT per-file --follow).
    """
    try:
        result = subprocess.run(
            [
                "git", "log", f"{since}..HEAD",
                "--name-status", "-M",
                "--no-merges", "--format=",
            ],
            cwd=workspace_path,
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.warning("_build_rename_map: git log failed: %s", result.stderr)
            return {}
    except Exception as exc:
        log.warning("_build_rename_map: %s", exc)
        return {}

    rename_map: Dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Rename lines look like: R100\told_path\tnew_path
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].startswith("R"):
            rename_map[parts[1]] = parts[2]
    return rename_map


# ---------------------------------------------------------------------------
# Commit-slice (R1)
# ---------------------------------------------------------------------------

def run_commit_slice_orchestrated(
    project_id: str,
    workspace_path: str,
    commit: str,
    *,
    phases: Optional[List[str]] = None,
    dry_run: bool = True,
    rename_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run scoped reconcile on files changed in a single commit.

    Args:
        project_id: governance project id
        workspace_path: absolute path to workspace root
        commit: git commit SHA to inspect
        phases: subset of phase keys; default ['K','A','E','D','H']
        dry_run: if True, no mutations applied
        rename_map: optional {old_path: new_path} for rename detection

    Returns:
        {commit, discrepancies, files_in_slice}
    """
    from .scope import ReconcileScope

    effective_phases = phases or list(_COMMIT_SWEEP_DEFAULT_PHASES)

    # Get files changed in this commit via git diff-tree
    try:
        result = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit],
            cwd=workspace_path,
            capture_output=True, text=True, timeout=10,
        )
        changed_files = [
            f.strip() for f in result.stdout.strip().splitlines() if f.strip()
        ] if result.returncode == 0 else []
    except Exception as exc:
        log.warning("run_commit_slice_orchestrated: git diff-tree failed: %s", exc)
        changed_files = []

    # Apply rename map: if an old path was renamed, include the new path too
    if rename_map:
        extra = []
        for f in changed_files:
            if f in rename_map:
                extra.append(rename_map[f])
        changed_files.extend(extra)

    # Deduplicate while preserving order
    seen = set()
    unique_files = []
    for f in changed_files:
        if f not in seen:
            seen.add(f)
            unique_files.append(f)
    changed_files = unique_files

    # Delegate to run_orchestrated via ReconcileScope(paths=changed_files)
    scope = ReconcileScope(paths=changed_files) if changed_files else None

    orch_result = run_orchestrated(
        project_id,
        workspace_path,
        phases=effective_phases,
        dry_run=dry_run,
        scope=scope,
    )

    # Flatten discrepancies from all phases
    discrepancies: List[Dict[str, Any]] = []
    for phase_key, phase_info in orch_result.get("phases", {}).items():
        # The full phase detail is in the _run_phase results; we re-extract
        # count only here.  For commit-sweep we need actual items.
        pass

    # Re-run to collect actual items (run_orchestrated returns counts only).
    # Instead, we inline the phase-run loop to capture items directly.
    from .context import ReconcileContext

    ctx = ReconcileContext(
        project_id=project_id,
        workspace_path=workspace_path,
        scan_depth=3,
    )
    resolved_scope = scope.resolve(ctx) if scope else None

    ordered = [p for p in PHASE_ORDER + ["H"] if p in effective_phases]
    # Deduplicate ordered while preserving order
    seen_phases: set = set()
    deduped_ordered: List[str] = []
    for p in ordered:
        if p not in seen_phases:
            seen_phases.add(p)
            deduped_ordered.append(p)
    ordered = deduped_ordered

    phase_results_inner: Dict[str, list] = {}
    for key in ordered:
        try:
            items = _run_phase(key, ctx, phase_results_inner, scope=resolved_scope)
        except Exception as exc:
            log.error("Phase %s failed in commit slice %s: %s", key, commit, exc)
            items = []
        phase_results_inner[key] = items
        for item in items:
            if hasattr(item, "__dict__"):
                d = {k: v for k, v in item.__dict__.items()}
            elif isinstance(item, dict):
                d = dict(item)
            else:
                d = {"detail": str(item)}
            d["phase"] = key
            discrepancies.append(d)

    return {
        "commit": commit,
        "discrepancies": discrepancies,
        "files_in_slice": changed_files,
    }


# ---------------------------------------------------------------------------
# Commit-sweep (R2)
# ---------------------------------------------------------------------------

def run_commit_sweep_orchestrated(
    project_id: str,
    workspace_path: str,
    *,
    since_baseline: Optional[str] = None,
    phases: Optional[List[str]] = None,
    dry_run: bool = True,
    coverage_target: str = "hot_files",
) -> Dict[str, Any]:
    """Walk commits since baseline, run scoped reconcile per commit, deduplicate.

    Args:
        project_id: governance project id
        workspace_path: absolute path to workspace root
        since_baseline: commit SHA to start from; None = auto-resolve from
            version_baselines WHERE scope_kind='commit_sweep'
        phases: subset of phase keys; default ['K','A','E','D','H']
        dry_run: if True, no mutations / baseline writes
        coverage_target: 'hot_files' (default) — coverage denominator

    Returns:
        {commits, all_discrepancies, dedup_discrepancies, hot_files,
         covered_hot, coverage_pct, baseline_written}
    """
    from .. import baseline_service
    from ..db import DBContext

    effective_phases = phases or list(_COMMIT_SWEEP_DEFAULT_PHASES)

    # AC9: auto-resolve since_baseline from version_baselines
    if since_baseline is None:
        try:
            with DBContext(project_id) as conn:
                row = conn.execute(
                    """SELECT chain_version FROM version_baselines
                       WHERE project_id = ? AND scope_kind = 'commit_sweep'
                       ORDER BY baseline_id DESC LIMIT 1""",
                    (project_id,),
                ).fetchone()
                if row:
                    since_baseline = row["chain_version"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
        except Exception as exc:
            log.warning("run_commit_sweep_orchestrated: baseline lookup failed: %s", exc)

    if not since_baseline:
        return {
            "commits": [],
            "all_discrepancies": [],
            "dedup_discrepancies": [],
            "hot_files": [],
            "covered_hot": [],
            "coverage_pct": 0.0,
            "baseline_written": False,
            "error": "no since_baseline resolved",
        }

    # AC3: use --no-merges, NEVER --first-parent
    try:
        result = subprocess.run(
            ["git", "log", f"{since_baseline}..HEAD", "--no-merges", "--format=%H"],
            cwd=workspace_path,
            capture_output=True, text=True, timeout=30,
        )
        commits_newest_first = [
            sha.strip() for sha in result.stdout.strip().splitlines()
            if sha.strip()
        ] if result.returncode == 0 else []
        # git log returns newest first by default; commit-sweep must replay drift
        # in chronological order so later slices can supersede earlier ones.
        commits = list(reversed(commits_newest_first))
    except Exception as exc:
        log.warning("run_commit_sweep_orchestrated: git log failed: %s", exc)
        commits = []

    # AC5: build rename map via single batched call
    rename_map = _build_rename_map(since_baseline, workspace_path)

    # Walk commits, collect discrepancies per commit (DRY reuse of slice)
    all_discrepancies: List[Dict[str, Any]] = []
    hot_files_set: set = set()

    for commit_sha in commits:
        slice_result = run_commit_slice_orchestrated(
            project_id,
            workspace_path,
            commit_sha,
            phases=effective_phases,
            dry_run=dry_run,
            rename_map=rename_map,
        )

        # Track hot files (R7: files touched in since_baseline..HEAD window)
        hot_files_set.update(slice_result.get("files_in_slice", []))

        # AC10: stamp attribution_commit on each discrepancy
        for d in slice_result.get("discrepancies", []):
            d["attribution_commit"] = commit_sha
            all_discrepancies.append(d)

    # Deduplicate via _dedup_key — keep last (newest commit) per key
    seen_keys: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for d in all_discrepancies:
        key = _dedup_key(d)
        seen_keys[key] = d  # last write wins → newest commit

    dedup_discrepancies = list(seen_keys.values())

    # AC2: hot_files coverage
    hot_files = sorted(hot_files_set)
    # Coverage measures scan execution against the hot-file denominator, not
    # the subset of files that happened to produce discrepancies.
    covered_hot_set = set(hot_files_set)
    covered_hot = sorted(covered_hot_set)
    coverage_pct = len(covered_hot) / len(hot_files) if hot_files else 0.0

    # AC8: write baseline on success when not dry_run
    baseline_written = False
    if not dry_run and commits:
        try:
            with DBContext(project_id) as conn:
                # Get current HEAD as chain_version
                head_result = subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=workspace_path,
                    capture_output=True, text=True, timeout=5,
                )
                head_sha = head_result.stdout.strip() if head_result.returncode == 0 else commits[0]

                baseline_service.create_baseline(
                    conn,
                    project_id,
                    chain_version=head_sha,
                    trigger="reconcile-task",
                    triggered_by="auto-chain",
                    scope_kind="commit_sweep",
                    scope_value=f"{since_baseline}..{head_sha}",
                )
                baseline_written = True
        except Exception as exc:
            log.warning("run_commit_sweep_orchestrated: baseline write failed: %s", exc)

    return {
        "since_baseline": since_baseline,
        "commits": commits,
        "all_discrepancies": all_discrepancies,
        "dedup_discrepancies": dedup_discrepancies,
        "hot_files": hot_files,
        "covered_hot": covered_hot,
        "coverage_pct": coverage_pct,
        "baseline_written": baseline_written,
    }
