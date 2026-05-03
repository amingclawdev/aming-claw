#!/usr/bin/env python3
"""Phase Z v2 calibration helper.

The calibration loop is intentionally a dry-run observer tool:

* run Phase Z v2 multiple times to prove deterministic output;
* diff the candidate graph against the real governance graph;
* write disagreement cases for human spot-check before any graph apply.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

# Ensure agent package is importable.
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from agent.governance.reconcile_phases.phase_z_v2 import (  # noqa: E402
    _default_existing_graph_path,
    build_graph_v2_from_symbols,
    diff_against_existing_graph,
)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _json_hash(payload: Any) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _candidate_signature(result: Dict[str, Any]) -> Dict[str, Any]:
    """Return a compact deterministic signature for reproducibility checks."""
    nodes = []
    for node in result.get("nodes") or []:
        nodes.append({
            "node_id": node.get("node_id") or node.get("id") or "",
            "primary_file": node.get("primary_file") or "",
            "module": node.get("module") or "",
            "layer": node.get("layer"),
            "functions": sorted(node.get("functions") or []),
        })

    clusters = []
    for cluster in result.get("feature_clusters") or []:
        clusters.append({
            "cluster_fingerprint": cluster.get("cluster_fingerprint") or "",
            "entries": sorted(cluster.get("entries") or []),
            "primary_files": sorted(cluster.get("primary_files") or []),
            "functions": sorted(cluster.get("functions") or []),
        })

    return {
        "status": result.get("status"),
        "node_count": result.get("node_count", len(nodes)),
        "nodes": sorted(nodes, key=lambda item: item["node_id"]),
        "feature_clusters": sorted(
            clusters, key=lambda item: item["cluster_fingerprint"]
        ),
    }


def _limited(values: List[Any], limit: int) -> Dict[str, Any]:
    limit = max(0, int(limit))
    return {
        "count": len(values),
        "sample": values[:limit],
        "truncated": len(values) > limit,
    }


def _summarize_diff(diff_report: Dict[str, Any], sample_size: int) -> Dict[str, Any]:
    primary = diff_report.get("primary_file_diff") or {}
    return {
        "graph_path": diff_report.get("graph_path", ""),
        "old_node_count": diff_report.get("old_node_count", 0),
        "new_node_count": diff_report.get("new_node_count", 0),
        "id_diff": {
            "only_in_new": _limited(diff_report.get("only_in_new") or [], sample_size),
            "only_in_old": _limited(diff_report.get("only_in_old") or [], sample_size),
            "layer_changes": _limited(diff_report.get("layer_changes") or [], sample_size),
        },
        "primary_file_diff": {
            "matched": primary.get("matched", 0),
            "only_in_new": _limited(primary.get("only_in_new") or [], sample_size),
            "only_in_old": _limited(primary.get("only_in_old") or [], sample_size),
            "layer_changes": _limited(primary.get("layer_changes") or [], sample_size),
            "duplicates_in_old": primary.get("duplicates_in_old") or {},
            "duplicates_in_new": primary.get("duplicates_in_new") or {},
        },
    }


def _build_calibration_report(
    *,
    project_root: str,
    iterations: int,
    graph_path: str | None,
    scratch_dir: str,
    sample_size: int,
) -> Dict[str, Any]:
    if iterations < 1 or iterations > 5:
        raise ValueError("--iterations must be between 1 and 5")

    scratch_root = pathlib.Path(scratch_dir)
    scratch_root.mkdir(parents=True, exist_ok=True)

    runs = []
    last_result: Dict[str, Any] | None = None
    for index in range(iterations):
        iter_scratch = scratch_root / f"iter-{index + 1}"
        iter_scratch.mkdir(parents=True, exist_ok=True)
        result = build_graph_v2_from_symbols(
            project_root,
            dry_run=True,
            scratch_dir=str(iter_scratch),
        )
        signature = _candidate_signature(result)
        runs.append({
            "iteration": index + 1,
            "status": result.get("status"),
            "report_path": result.get("report_path", ""),
            "node_count": result.get("node_count", 0),
            "feature_cluster_count": len(result.get("feature_clusters") or []),
            "signature_sha256": _json_hash(signature),
        })
        last_result = result

    hashes = [run["signature_sha256"] for run in runs]
    reproducible = len(set(hashes)) <= 1
    resolved_graph_path = graph_path or _default_existing_graph_path(project_root)
    diff_report = diff_against_existing_graph(
        project_root,
        (last_result or {}).get("nodes") or [],
        graph_path=resolved_graph_path,
    )

    return {
        "ok": all(run["status"] == "ok" for run in runs),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(pathlib.Path(project_root).resolve()),
        "iterations": iterations,
        "reproducible": reproducible,
        "runs": runs,
        "candidate_node_count": (last_result or {}).get("node_count", 0),
        "feature_cluster_count": len((last_result or {}).get("feature_clusters") or []),
        "disagreement_report": _summarize_diff(diff_report, sample_size),
    }


def cmd_iterate(args: argparse.Namespace) -> int:
    """Run 3-5 dry-run iterations and write a calibration report."""
    project_root = str(pathlib.Path(getattr(args, "project_root", None) or _repo_root).resolve())
    out_dir = pathlib.Path(
        getattr(args, "out_dir", None)
        or pathlib.Path(project_root) / ".observer-cache" / "phase-z-v2-calibration"
    )
    scratch_dir = str(
        pathlib.Path(getattr(args, "scratch_dir", None) or out_dir / "scratch").resolve()
    )

    report = _build_calibration_report(
        project_root=project_root,
        iterations=int(args.iterations),
        graph_path=getattr(args, "graph_path", None),
        scratch_dir=scratch_dir,
        sample_size=int(args.sample_size),
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"calibration-{_utc_stamp()}.json"
    report["report_path"] = str(report_path)
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("ok") and report.get("reproducible") else 1


def cmd_status(args: argparse.Namespace) -> int:
    """Show the latest calibration report location and summary."""
    project_root = str(pathlib.Path(getattr(args, "project_root", None) or _repo_root).resolve())
    out_dir = pathlib.Path(
        getattr(args, "out_dir", None)
        or pathlib.Path(project_root) / ".observer-cache" / "phase-z-v2-calibration"
    )
    reports = sorted(out_dir.glob("calibration-*.json"))
    if not reports:
        print(json.dumps({"status": "no calibration report found"}, indent=2))
        return 0

    latest = reports[-1]
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "unreadable", "path": str(latest), "error": str(exc)}, indent=2))
        return 1

    summary = {
        "status": "ok" if payload.get("ok") else "failed",
        "path": str(latest),
        "generated_at": payload.get("generated_at", ""),
        "iterations": payload.get("iterations", 0),
        "reproducible": payload.get("reproducible", False),
        "candidate_node_count": payload.get("candidate_node_count", 0),
        "feature_cluster_count": payload.get("feature_cluster_count", 0),
        "disagreement_report": payload.get("disagreement_report", {}),
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project-root", default=argparse.SUPPRESS, help="Project root directory")
    common.add_argument("--out-dir", default=argparse.SUPPRESS, help="Calibration report directory")

    parser = argparse.ArgumentParser(
        description="Phase Z v2 calibration helper",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command")

    iterate = sub.add_parser(
        "iterate",
        parents=[common],
        help="Run repeated dry-runs and report graph disagreements",
    )
    iterate.add_argument("--iterations", type=int, default=3, help="Dry-run count, 1-5")
    iterate.add_argument("--graph-path", default=None, help="Existing graph.json path")
    iterate.add_argument("--scratch-dir", default=None, help="Dry-run artifact directory")
    iterate.add_argument("--sample-size", type=int, default=25, help="Disagreement sample size")

    status = sub.add_parser("status", parents=[common], help="Show latest calibration status")

    args = parser.parse_args()
    if args.command == "iterate":
        return cmd_iterate(args)
    if args.command == "status":
        return cmd_status(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
