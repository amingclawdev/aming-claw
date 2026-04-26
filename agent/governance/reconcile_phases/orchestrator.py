"""Orchestrator — runs the full 5-phase reconcile pipeline.

Builds ReconcileContext once, runs requested phases in order A -> E -> B -> C -> D,
calls Aggregator, writes report to docs/dev/scratch/reconcile-comprehensive-YYYY-MM-DD.md,
and returns structured result.
"""
from __future__ import annotations

import os
import logging
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

PHASE_ORDER = ["A", "E", "B", "C", "D", "F", "G"]


def _run_phase(phase_key: str, ctx: Any, phase_results: Dict[str, list],
               scope: Optional[Any] = None) -> list:
    """Run a single phase by key, returning its discrepancy list."""
    if phase_key == "A":
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
