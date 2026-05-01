#!/usr/bin/env python3
"""Phase Z v2 CLI — dry-run, review, apply, rollback, status subcommands.

This script drives the v2 graph swap workflow:

* ``dry-run``  — produce a candidate graph artifact without writing the
  canonical ``graph.json``.
* ``review``   — diff the existing graph against a candidate and print a
  disappearance review report (see
  :mod:`agent.governance.symbol_disappearance_review`).
* ``apply``    — invoke the atomic swap in
  :mod:`agent.governance.symbol_swap`.
* ``rollback`` — restore ``graph.json`` from its ``.json.bak`` sibling.
* ``status``   — print whether a ``.json.bak`` exists and its age.

The legacy ``abort`` subcommand has been removed (replaced by
``rollback``).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

# Ensure agent package is importable
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from agent.governance.reconcile_phases.phase_z_v2 import (  # noqa: E402
    build_graph_v2_from_symbols,
)
from agent.governance import symbol_disappearance_review  # noqa: E402
from agent.governance import symbol_swap  # noqa: E402
from agent.governance.ai_cluster_processor import (  # noqa: E402
    ClusterReport,
    process_cluster_with_ai,
)
from agent.governance.llm_cache import LLMCache  # noqa: E402
from agent.governance.auto_backlog_bridge import (  # noqa: E402
    file_remediation_plan,
)


# ---------------------------------------------------------------------------
# enrich helpers
# ---------------------------------------------------------------------------

class _ClusterMember:
    """Lightweight stand-in exposing ``qname`` + ``lines`` for cache keying."""

    __slots__ = ("qname", "lines")

    def __init__(self, qname: str, lines):
        self.qname = qname
        self.lines = lines


def _load_cluster_payload(path: pathlib.Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _payload_to_members(payload: dict):
    members = payload.get("members") or payload.get("functions") or []
    out = []
    for m in members:
        if isinstance(m, dict):
            qn = m.get("qname") or m.get("qualified_name") or ""
            lines = m.get("lines")
            if lines is None:
                lo = m.get("lineno")
                hi = m.get("end_lineno")
                if lo is not None:
                    lines = [lo, hi]
            out.append(_ClusterMember(qn, lines))
    return out


def _payload_to_entry(payload: dict, members):
    entry_qname = payload.get("entry") or payload.get("entry_qname")
    if entry_qname:
        return _ClusterMember(entry_qname, None)
    if members:
        return members[0]
    return _ClusterMember("", None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_graph(path: pathlib.Path) -> dict:
    """Load a graph JSON from *path*; return {} on missing/invalid."""
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, default=str))


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_dry_run(args: argparse.Namespace) -> int:
    """Produce a candidate graph without writing the canonical graph.json."""
    project_root = args.project_root or _repo_root
    result = build_graph_v2_from_symbols(project_root, dry_run=True)
    _print_json(result)
    return 0 if result.get("status") in {"ok", None} else 1


def cmd_review(args: argparse.Namespace) -> int:
    """Run the pre-swap disappearance review and print a JSON report."""
    old_graph_path = pathlib.Path(args.graph_path)
    new_graph_path = pathlib.Path(args.candidate_path)
    old_graph = _load_graph(old_graph_path)
    new_graph = _load_graph(new_graph_path)

    decisions: dict = {}
    if args.decisions:
        try:
            with open(args.decisions, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                decisions = loaded
        except (OSError, json.JSONDecodeError) as exc:
            _print_json({"ok": False, "reason": f"decisions file unreadable: {exc}"})
            return 1

    report = symbol_disappearance_review.review_report(old_graph, new_graph, decisions)
    _print_json(report)
    decision_status = report.get("decision_status", {})
    return 0 if decision_status.get("ok", False) else 1


def cmd_apply(args: argparse.Namespace) -> int:
    """Invoke the atomic swap."""
    graph_path = pathlib.Path(args.graph_path)
    candidate_path = pathlib.Path(args.candidate_path)

    alerts: list = []

    def _alert(info: dict) -> None:
        alerts.append(info)

    result = symbol_swap.atomic_swap(
        graph_path,
        candidate_path,
        observer_alert=_alert,
    )
    if alerts:
        result["observer_alerts"] = alerts
    _print_json(result)
    return 0 if result.get("ok") else 1


def cmd_rollback(args: argparse.Namespace) -> int:
    """Restore graph.json from its .json.bak sibling."""
    graph_path = pathlib.Path(args.graph_path)
    result = symbol_swap.rollback(
        graph_path,
        max_age_days=args.max_age_days,
    )
    _print_json(result)
    return 0 if result.get("ok") else 1


def cmd_status(args: argparse.Namespace) -> int:
    """Print current swap state (bak presence + age)."""
    graph_path = pathlib.Path(args.graph_path)
    result = symbol_swap.status(graph_path)
    _print_json(result)
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    """Pass-5 AI enrichment of cluster JSON files in *workspace*.

    Iterates every ``cluster_*.json`` (and ``*.cluster.json``) under
    ``--workspace``, calls
    :func:`agent.governance.ai_cluster_processor.process_cluster_with_ai`
    for each, and writes a sibling ``<name>.enriched.json`` containing
    the :class:`ClusterReport`.  When ``--resume`` is set, clusters
    whose existing report already has ``enrichment_status ==
    'ai_complete'`` are skipped.
    """
    workspace = pathlib.Path(args.workspace).resolve()
    if not workspace.exists():
        _print_json({"ok": False, "reason": f"workspace not found: {workspace}"})
        return 1

    cache = LLMCache(workspace)

    candidates: list[pathlib.Path] = []
    for pattern in ("cluster_*.json", "*.cluster.json", "clusters/*.json"):
        candidates.extend(sorted(workspace.rglob(pattern)))
    # De-dup while preserving order
    seen: set[str] = set()
    cluster_files: list[pathlib.Path] = []
    for p in candidates:
        if p.name.endswith(".enriched.json"):
            continue
        key = str(p.resolve())
        if key in seen:
            continue
        seen.add(key)
        cluster_files.append(p)

    processed = 0
    skipped = 0
    failed = 0
    for cluster_path in cluster_files:
        enriched_path = cluster_path.with_suffix(".enriched.json")
        if args.resume and enriched_path.exists():
            existing = _load_graph(enriched_path)
            if existing.get("enrichment_status") == "ai_complete":
                skipped += 1
                continue

        payload = _load_cluster_payload(cluster_path)
        if payload is None:
            failed += 1
            continue

        members = _payload_to_members(payload)
        entry = _payload_to_entry(payload, members)

        try:
            report = process_cluster_with_ai(
                members,
                entry,
                str(workspace),
                use_ai=args.use_ai,
                cache=cache,
            )
        except Exception as exc:  # pragma: no cover — defensive
            failed += 1
            _print_json({"ok": False, "cluster": str(cluster_path), "error": str(exc)})
            continue

        try:
            with enriched_path.open("w", encoding="utf-8") as f:
                json.dump(report.to_dict(), f, indent=2, sort_keys=True)
            processed += 1
        except OSError as exc:
            failed += 1
            _print_json({"ok": False, "cluster": str(cluster_path), "error": str(exc)})
            continue

    _print_json({
        "ok": failed == 0,
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
        "total": len(cluster_files),
    })
    return 0 if failed == 0 else 1


def cmd_file_backlog(args: argparse.Namespace) -> int:
    """Phase Z v2 PR4 — file an approved remediation plan as reconcile tasks.

    Reads the plan JSON at ``--plan``, calls
    :func:`agent.governance.auto_backlog_bridge.file_remediation_plan`, and
    prints a JSON summary. Exit codes: 0 on success, 1 if filed=0 with
    errors, 2 on partial errors (filed>0 but errors non-empty).
    """
    plan_path = pathlib.Path(args.plan)
    try:
        with plan_path.open("r", encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        _print_json({"ok": False, "reason": f"plan unreadable: {exc}"})
        return 1
    if not isinstance(plan, dict):
        _print_json({"ok": False, "reason": "plan JSON must be an object"})
        return 1

    project_id = args.project_id or os.environ.get("PROJECT_ID", "aming-claw")
    summary = file_remediation_plan(
        plan,
        run_id=args.run_id,
        project_id=project_id,
        creator=args.creator,
        dry_run=bool(args.dry_run),
    )
    _print_json(summary)

    filed = int(summary.get("filed", 0) or 0)
    errors = summary.get("errors") or []
    if filed == 0 and errors:
        return 1
    if filed > 0 and errors:
        return 2
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Phase Z v2 — graph swap workflow. Subcommands: "
            "dry-run, review, apply, rollback, status."
        )
    )
    parser.add_argument("--project-root", default=None, help="Project root directory")
    sub = parser.add_subparsers(dest="command")

    # dry-run
    sub.add_parser("dry-run", help="Run driver in dry-run mode (no writes).")

    # review
    review_p = sub.add_parser(
        "review",
        help="Diff graph.json vs candidate and emit disappearance review JSON.",
    )
    review_p.add_argument(
        "--graph-path",
        default="agent/governance/graph.json",
        help="Path to the existing canonical graph.json",
    )
    review_p.add_argument(
        "--candidate-path",
        default="agent/governance/graph.v2.json",
        help="Path to the candidate graph (e.g. graph.v2.json)",
    )
    review_p.add_argument(
        "--decisions",
        default=None,
        help=(
            "Optional JSON file mapping {node_id: decision} where decision is "
            "one of approve_removal, map_to_new_node, preserve_as_supplement, "
            "block_swap"
        ),
    )

    # apply
    apply_p = sub.add_parser(
        "apply",
        help="Atomically swap candidate into graph.json (auto-rollback on smoke fail).",
    )
    apply_p.add_argument(
        "--graph-path",
        default="agent/governance/graph.json",
        help="Path to the canonical graph.json",
    )
    apply_p.add_argument(
        "--candidate-path",
        default="agent/governance/graph.v2.json",
        help="Path to the candidate graph",
    )

    # rollback
    rollback_p = sub.add_parser(
        "rollback",
        help="Restore graph.json from its .json.bak sibling.",
    )
    rollback_p.add_argument(
        "--graph-path",
        default="agent/governance/graph.json",
        help="Path to the canonical graph.json",
    )
    rollback_p.add_argument(
        "--max-age-days",
        type=int,
        default=symbol_swap.BAK_RETENTION_DAYS,
        help=f"Maximum backup age (default {symbol_swap.BAK_RETENTION_DAYS} days).",
    )

    # status
    status_p = sub.add_parser(
        "status",
        help="Show current swap state (bak presence + age).",
    )
    status_p.add_argument(
        "--graph-path",
        default="agent/governance/graph.json",
        help="Path to the canonical graph.json",
    )

    # enrich (Phase Z v2 PR3 — AI cluster processor)
    enrich_p = sub.add_parser(
        "enrich",
        help=(
            "Pass-5 AI enrichment: iterate cluster JSON files under "
            "--workspace and write sibling <name>.enriched.json reports."
        ),
    )
    enrich_p.add_argument(
        "--workspace",
        required=True,
        help="Workspace root containing cluster_*.json output from prior passes.",
    )
    enrich_p.add_argument(
        "--use-ai",
        dest="use_ai",
        action="store_true",
        default=True,
        help="Enable AI enrichment (default).",
    )
    enrich_p.add_argument(
        "--no-ai",
        dest="use_ai",
        action="store_false",
        help="Disable AI calls; emit ai_unavailable placeholders.",
    )
    enrich_p.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help=(
            "Skip clusters whose existing enriched report already has "
            "enrichment_status == 'ai_complete'."
        ),
    )

    # file-backlog (Phase Z v2 PR4 — auto-backlog filing bridge)
    file_p = sub.add_parser(
        "file-backlog",
        help=(
            "File an approved remediation plan as reconcile-type tasks via "
            "the governance HTTP API."
        ),
    )
    file_p.add_argument(
        "--plan",
        required=True,
        help="Path to the approved remediation plan JSON.",
    )
    file_p.add_argument(
        "--run-id",
        required=True,
        help="Reconcile run id (correlates this filing with a ClusterReport run).",
    )
    file_p.add_argument(
        "--project-id",
        default=None,
        help="Governance project id (defaults to $PROJECT_ID or 'aming-claw').",
    )
    file_p.add_argument(
        "--creator",
        default="reconcile-bridge",
        help=(
            "Creator name; must be in {reconcile-bridge, coordinator, "
            "auto-approval-bot} or start with 'observer-'."
        ),
    )
    file_p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Compose plan + bug_ids; do not POST any task creates.",
    )

    return parser


def main(argv: list | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "dry-run": cmd_dry_run,
        "review": cmd_review,
        "apply": cmd_apply,
        "rollback": cmd_rollback,
        "status": cmd_status,
        "enrich": cmd_enrich,
        "file-backlog": cmd_file_backlog,
    }

    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
