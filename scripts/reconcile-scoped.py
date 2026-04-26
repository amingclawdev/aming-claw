#!/usr/bin/env python3
"""CLI wrapper for scoped reconcile runs.

Usage:
    python scripts/reconcile-scoped.py --bug-id OPT-BACKLOG-FOO --dry-run
    python scripts/reconcile-scoped.py --commit abc123 --phases A,E --strict
    python scripts/reconcile-scoped.py --paths agent/governance/server.py --auto-fix-threshold high
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Ensure project root is on path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run scoped reconcile on specific files/bugs/commits",
    )
    parser.add_argument("--bug-id", help="Backlog bug ID to scope to")
    parser.add_argument("--commit", help="Single commit SHA to scope to")
    parser.add_argument("--commit-range", help="Commit range (A..B) to scope to")
    parser.add_argument("--node", action="append", default=[], help="Node ID (repeatable)")
    parser.add_argument("--paths", nargs="*", default=[], help="Explicit file paths")
    parser.add_argument("--strict", action="store_true", help="Fail on empty scope")
    parser.add_argument("--phases", default=None, help="Comma-separated phase list (e.g. A,E,B)")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Report only (default)")
    parser.add_argument("--auto-fix-threshold", default="high",
                        choices=["high", "medium", "low"], help="Min confidence for auto-fix")
    parser.add_argument("--project-id", default="aming-claw", help="Project ID")
    parser.add_argument("--workspace", default=None, help="Workspace path (default: repo root)")

    args = parser.parse_args(argv)

    from agent.governance.reconcile_phases.scope import (
        ReconcileScope, EmptyScopeError,
    )
    from agent.governance.reconcile_phases.orchestrator import run_orchestrated

    scope = ReconcileScope(
        bug_id=args.bug_id,
        commit=args.commit,
        commit_range=args.commit_range,
        nodes=args.node if args.node else None,
        paths=args.paths if args.paths else None,
        strict=args.strict,
    )

    phases = args.phases.split(",") if args.phases else None
    workspace = args.workspace or _project_root

    try:
        result = run_orchestrated(
            project_id=args.project_id,
            workspace_path=workspace,
            phases=phases,
            dry_run=args.dry_run,
            auto_fix_threshold=args.auto_fix_threshold,
            scope=scope,
        )
    except EmptyScopeError as exc:
        print(json.dumps({"error": str(exc), "empty_scope": True}), file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
