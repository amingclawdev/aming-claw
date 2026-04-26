#!/usr/bin/env python3
"""Phase Z v2 calibration helper — iterate and status subcommands."""
from __future__ import annotations

import argparse
import json
import os
import sys

# Ensure agent package is importable
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from agent.governance.reconcile_phases.phase_z_v2 import (
    build_graph_v2_from_symbols,
    diff_against_existing_graph,
)


def cmd_iterate(args: argparse.Namespace) -> None:
    """Run dry-run + diff to calibrate weight adjustments."""
    project_root = args.project_root or _repo_root
    result = build_graph_v2_from_symbols(project_root, dry_run=True)
    print(json.dumps(result, indent=2))


def cmd_status(args: argparse.Namespace) -> None:
    """Show current calibration status."""
    print(json.dumps({"status": "no calibration in progress"}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase Z v2 calibration helper"
    )
    parser.add_argument("--project-root", default=None, help="Project root directory")
    sub = parser.add_subparsers(dest="command")

    # iterate
    sub.add_parser("iterate", help="Run dry-run + diff for calibration")

    # status
    sub.add_parser("status", help="Show calibration status")

    args = parser.parse_args()

    if args.command == "iterate":
        cmd_iterate(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
