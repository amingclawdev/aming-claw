#!/usr/bin/env python3
"""Run scoped reconcile commit-sweep in a dedicated branch/worktree.

Examples:
    python scripts/reconcile-scope-catchup.py --dry-run
    python scripts/reconcile-scope-catchup.py --apply --branch codex/scope-catchup-main
"""
from __future__ import annotations

import argparse
import json
import os
import sys


_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _parse_phases(raw: str | None):
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Dogfood scoped reconcile catch-up on a branch/worktree without redeploy.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Scan only; do not write baseline")
    mode.add_argument("--apply", action="store_true", help="Write commit_sweep baseline when commits exist")
    parser.add_argument("--project-id", default="aming-claw", help="Governance project id")
    parser.add_argument("--repo-root", default=_project_root, help="Source repo root")
    parser.add_argument("--base-ref", default="HEAD", help="Ref to fast-forward the catch-up branch to")
    parser.add_argument("--branch", default=None, help="Catch-up branch name")
    parser.add_argument("--worktree", default=None, help="Catch-up worktree path")
    parser.add_argument("--since-baseline", default=None, help="Commit SHA to start from")
    parser.add_argument("--phases", default=None, help="Comma-separated phases; default K,A,E,D,F")
    parser.add_argument("--output", default=None, help="JSON output path, relative to worktree if not absolute")
    parser.add_argument(
        "--file-materialization-backlog",
        action="store_true",
        help="Upsert a backlog row when actionable graph/doc/test materialization drift is found",
    )
    parser.add_argument(
        "--materialization-api-base",
        default="http://localhost:40000",
        help="Governance API base used when filing materialization backlog rows",
    )
    parser.add_argument(
        "--materialization-bug-id",
        default=None,
        help="Optional explicit backlog bug_id for the materialization handoff",
    )
    args = parser.parse_args(argv)

    from agent.governance.reconcile_scope_catchup import run_scope_catchup

    result = run_scope_catchup(
        project_id=args.project_id,
        repo_root=args.repo_root,
        base_ref=args.base_ref,
        branch=args.branch,
        worktree_path=args.worktree,
        since_baseline=args.since_baseline,
        phases=_parse_phases(args.phases),
        dry_run=not args.apply,
        output_path=args.output,
        file_materialization_backlog=args.file_materialization_backlog,
        materialization_api_base=args.materialization_api_base,
        materialization_bug_id=args.materialization_bug_id,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
