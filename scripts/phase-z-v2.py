#!/usr/bin/env python3
"""Phase Z v2 CLI — dry-run, apply, status, abort subcommands."""
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
)
from agent.governance.migration_state_machine import (
    trigger_migration_window,
    check_swap_gate,
    abort_migration,
    MigrationWindow,
)


def cmd_dry_run(args: argparse.Namespace) -> None:
    """Run the driver in dry-run mode."""
    project_root = args.project_root or _repo_root
    result = build_graph_v2_from_symbols(project_root, dry_run=True)
    print(json.dumps(result, indent=2))


def cmd_apply(args: argparse.Namespace) -> None:
    """Run the driver in apply mode (writes graph.v2.json + baseline)."""
    project_root = args.project_root or _repo_root
    result = build_graph_v2_from_symbols(project_root, dry_run=False, owner=args.owner)
    print(json.dumps(result, indent=2))


def cmd_status(args: argparse.Namespace) -> None:
    """Show current migration status."""
    print(json.dumps({"status": "no active migration"}, indent=2))


def cmd_abort(args: argparse.Namespace) -> None:
    """Abort an active migration."""
    print(json.dumps({"status": "aborted", "reason": args.reason}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase Z v2 — symbol topology driver"
    )
    parser.add_argument("--project-root", default=None, help="Project root directory")
    sub = parser.add_subparsers(dest="command")

    # dry-run
    sub.add_parser("dry-run", help="Run driver in dry-run mode")

    # apply
    apply_p = sub.add_parser("apply", help="Apply graph.v2.json + create baseline")
    apply_p.add_argument("--owner", required=True, help="Owner identifier")

    # status
    sub.add_parser("status", help="Show migration status")

    # abort
    abort_p = sub.add_parser("abort", help="Abort active migration")
    abort_p.add_argument("--reason", required=True, help="Reason for abort")

    args = parser.parse_args()

    if args.command == "dry-run":
        cmd_dry_run(args)
    elif args.command == "apply":
        cmd_apply(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "abort":
        cmd_abort(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
