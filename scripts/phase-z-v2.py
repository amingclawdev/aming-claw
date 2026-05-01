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
    }

    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
