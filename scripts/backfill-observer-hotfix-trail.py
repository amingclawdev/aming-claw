#!/usr/bin/env python3
"""Backfill observer-hotfix trail into governance DB.

Upserts 5 backlog_bugs rows for observer-hotfix commits that were applied
outside auto-chain governance, and writes 3 MF execution record files.

Usage:
    python scripts/backfill-observer-hotfix-trail.py                  # dry-run
    python scripts/backfill-observer-hotfix-trail.py --apply          # upsert
    python scripts/backfill-observer-hotfix-trail.py --apply --pid X  # custom pid

Idempotent: re-running --apply produces 0 net-new rows.
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)

# ---------------------------------------------------------------------------
# Data: 5 backlog_bugs rows to backfill
# ---------------------------------------------------------------------------
BACKFILL_BUGS = [
    {
        "bug_id": "OPT-BACKLOG-B48-SM-LOG",
        "commit": "ba791f0",
        "title": "B48 observer-hotfix: SM log visibility for sidecar crash diagnosis",
        "status": "FIXED",
        "priority": "P0",
        "target_files": ["agent/service_manager.py"],
        "details_md": "Observer-hotfix ba791f0: Added SM log visibility so sidecar crash "
                      "events are surfaced in service_manager logs for B48 diagnosis.",
    },
    {
        "bug_id": "OPT-BACKLOG-B48-SIDECAR-IMPORT",
        "commit": "1bb9f35",
        "title": "B48 observer-hotfix: defensive sidecar import with sys.path fix",
        "status": "FIXED",
        "priority": "P0",
        "target_files": ["agent/service_manager.py"],
        "details_md": "Observer-hotfix 1bb9f35: Defensive sidecar import with sys.path "
                      "adjustment to prevent ImportError → _sidecar_crashed=True cascade.",
    },
    {
        "bug_id": "OPT-BACKLOG-F2-PYTHONPATH",
        "commit": "2763aac",
        "title": "F2 observer-hotfix: PYTHONPATH fix for executor subprocess imports",
        "status": "FIXED",
        "priority": "P1",
        "target_files": ["agent/ai_lifecycle.py"],
        "details_md": "Observer-hotfix 2763aac: PYTHONPATH fix so executor subprocesses "
                      "can locate governance modules without manual sys.path hacks.",
    },
    {
        "bug_id": "OPT-BACKLOG-B48-SEQUEL-VERSION-DEPLOY",
        "commit": "4a12c29",
        "title": "B48 sequel: version-deploy gate fix for observer-hotfix commits",
        "status": "FIXED",
        "priority": "P1",
        "target_files": ["agent/governance/auto_chain.py"],
        "details_md": "Observer-hotfix 4a12c29: Version-deploy gate fix so observer-hotfix "
                      "commits do not block subsequent auto-chain dispatch.",
    },
    {
        "bug_id": "OPT-BACKLOG-VERSION-UPDATE-LOCKDOWN",
        "commit": "e57e7ba",
        "title": "Version-update lockdown: restrict to auto-chain/merge-service actors only",
        "status": "FIXED",
        "priority": "P1",
        "target_files": ["agent/governance/server.py"],
        "details_md": "Observer-hotfix e57e7ba: Version-update endpoint lockdown to prevent "
                      "accidental 'init' actor from creating false governance records.",
    },
]

# ---------------------------------------------------------------------------
# Data: 3 MF execution records to write
# ---------------------------------------------------------------------------
MF_RECORDS = [
    {
        "filename": "observer-hotfix-record-2026-04-24-84e7be8.md",
        "commit": "84e7be8",
        "session": "Z0-sequel",
        "title": "ThreadingHTTPServer fix",
        "content": """\
# MF Execution Record: Z0-sequel (84e7be8)

**Date:** 2026-04-24
**Commit:** 84e7be8
**Session:** Z0-sequel — ThreadingHTTPServer fix

## Summary

Applied ThreadingHTTPServer fix to governance server to resolve request
serialization bottleneck that caused dispatch timeouts under concurrent
auto-chain load. This was an observer-hotfix applied outside auto-chain
governance due to the blocking nature of the issue.

## Changes

- Switched `HTTPServer` to `ThreadingHTTPServer` in governance server
- Verified concurrent request handling under auto-chain load

## Verification

- 24-minute chain completed after fix
- 6 auto-recovered worker deaths, 0 observer SM restarts
""",
    },
    {
        "filename": "observer-hotfix-record-2026-04-24-d4398bb.md",
        "commit": "d4398bb",
        "session": "Z0-sequel-3",
        "title": "File-log dispatch exceptions",
        "content": """\
# MF Execution Record: Z0-sequel-3 (d4398bb)

**Date:** 2026-04-24
**Commit:** d4398bb
**Session:** Z0-sequel-3 — File-log dispatch exceptions

## Summary

Fixed file-log dispatch exceptions that occurred when governance server
tried to write structured log events during high-frequency auto-chain
dispatch cycles. Exceptions were silently swallowed, masking dispatch
failures.

## Changes

- Added exception handling around file-log dispatch calls
- Ensured dispatch failures are logged rather than silently dropped

## Verification

- Confirmed log entries appear for all dispatch events
- No silent exception swallowing observed in subsequent chains
""",
    },
    {
        "filename": "observer-hotfix-record-2026-04-24-fedaf27.md",
        "commit": "fedaf27",
        "session": "Z3-partial",
        "title": "Backlog gate strict default",
        "content": """\
# MF Execution Record: Z3-partial (fedaf27)

**Date:** 2026-04-24
**Commit:** fedaf27
**Session:** Z3-partial — Backlog gate strict default

## Summary

Set backlog gate to strict default mode so that missing backlog_bugs
entries block chain progression rather than silently passing. This
ensures all observer-hotfix commits are tracked before chains can
proceed past them.

## Changes

- Changed backlog gate default from permissive to strict
- Added validation for required backlog_bugs fields

## Verification

- Chains now block when backlog_bugs entry is missing for a commit
- Existing entries pass gate without modification
""",
    },
]


def _upsert_bug(base_url: str, pid: str, bug: dict, dry_run: bool) -> str:
    """Upsert a single bug via POST /api/backlog/{pid}/{bug_id}.

    Returns: 'upserted' | 'dry-run' | error message.
    """
    if dry_run:
        return "dry-run"

    url = f"{base_url}/api/backlog/{pid}/{bug['bug_id']}"
    payload = {
        "title": bug["title"],
        "status": bug["status"],
        "priority": bug.get("priority", "P3"),
        "commit": bug["commit"],
        "target_files": bug.get("target_files", []),
        "test_files": bug.get("test_files", []),
        "acceptance_criteria": bug.get("acceptance_criteria", []),
        "details_md": bug.get("details_md", ""),
        "actor": "backfill-observer-hotfix-trail",
        "fixed_at": "2026-04-24",
        "discovered_at": "2026-04-24",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
            return body.get("action", "ok")
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}: {exc.read().decode()}"
    except Exception as exc:
        return f"error: {exc}"


def _write_mf_records(project_root: str, dry_run: bool) -> list:
    """Write MF execution record markdown files to docs/dev/.

    Returns list of (filename, status) tuples.
    """
    docs_dev = os.path.join(project_root, "docs", "dev")
    os.makedirs(docs_dev, exist_ok=True)
    results = []
    for rec in MF_RECORDS:
        path = os.path.join(docs_dev, rec["filename"])
        if dry_run:
            results.append((rec["filename"], "dry-run"))
            continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(rec["content"])
        results.append((rec["filename"], "written"))
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Backfill observer-hotfix trail into governance DB."
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually upsert rows (default: dry-run)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Print what would be done without changes (default)",
    )
    parser.add_argument(
        "--pid", default="aming-claw",
        help="Project ID (default: aming-claw)",
    )
    parser.add_argument(
        "--base-url", default="http://localhost:40000",
        help="Governance server base URL (default: http://localhost:40000)",
    )
    args = parser.parse_args(argv)

    dry_run = not args.apply

    print(f"{'DRY-RUN' if dry_run else 'APPLY'} mode — pid={args.pid}")
    print(f"Base URL: {args.base_url}")
    print()

    # --- 1. Upsert backlog_bugs ---
    print("=== Backlog Bugs Upsert ===")
    for bug in BACKFILL_BUGS:
        result = _upsert_bug(args.base_url, args.pid, bug, dry_run)
        print(f"  {bug['bug_id']} ({bug['commit']}): {result}")

    # --- 2. Write MF execution records ---
    print()
    print("=== MF Execution Records ===")
    mf_results = _write_mf_records(_PROJECT_ROOT, dry_run)
    for filename, status in mf_results:
        print(f"  {filename}: {status}")

    # Note: docs/dev/ is gitignored — do NOT git-add MF record files.
    print()
    print("Done. (docs/dev/ is gitignored; MF records are local-only)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
