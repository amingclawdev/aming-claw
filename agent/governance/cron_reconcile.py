"""Cron entry-point for reconcile-v2 — scheduled dry-run reconciliation.

Designed to be invoked by an external cron scheduler (e.g. crontab, Task Scheduler).
The operator MUST manually wire this; no auto-registration per pitfall policy (R5).

Typical crontab entry:
    0 2 * * *  cd /path/to/project && python -m agent.governance.cron_reconcile

Usage:
    python -m agent.governance.cron_reconcile [--apply]
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_DEFAULT_GOV_URL = "http://localhost:40000"
_DEFAULT_PROJECT_ID = "aming-claw"
_LOG_FILE = "logs/cron-reconcile.log"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_log(line: str) -> None:
    """Append a single JSON line to the cron-reconcile log file."""
    log_path = Path(_LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _emit_audit(
    gov_url: str, project_id: str, event: str, ok: bool, details: dict
) -> None:
    """POST an audit_log entry for traceability (R3)."""
    try:
        requests.post(
            f"{gov_url}/api/audit/{project_id}/log",
            json={
                "event": event,
                "actor": "cron-reconcile-v2",
                "ok": ok,
                **details,
            },
            timeout=10,
        )
    except Exception as exc:
        log.warning("audit_log POST failed: %s", exc)


def cron_reconcile_v2(
    dry_run: bool = True,
    project_id: str = "",
    gov_url: str = "",
) -> dict:
    """Run reconcile-v2 via the governance HTTP API.

    Args:
        dry_run: When True (default), sends auto_fix_threshold='none'
                 so no mutations are applied.  When False, sends 'high'.
        project_id: Override project id (default from env or 'aming-claw').
        gov_url: Override governance base URL (default from env or localhost:40000).

    Returns:
        The JSON response body from the reconcile-v2 endpoint, or an error dict.
    """
    project_id = project_id or os.environ.get("PROJECT_ID", _DEFAULT_PROJECT_ID)
    gov_url = (gov_url or os.environ.get("GOVERNANCE_URL", _DEFAULT_GOV_URL)).rstrip("/")

    auto_fix_threshold = "none" if dry_run else "high"

    payload = {
        "dry_run": dry_run,
        "auto_fix_threshold": auto_fix_threshold,
    }

    timestamp = _utc_iso()
    url = f"{gov_url}/api/wf/{project_id}/reconcile-v2"

    try:
        resp = requests.post(url, json=payload, timeout=120)
        resp.raise_for_status()
        result = resp.json()
    except Exception as exc:
        error_result = {
            "timestamp": timestamp,
            "status": "error",
            "error": str(exc),
            "dry_run": dry_run,
        }
        _append_log(json.dumps(error_result))
        _emit_audit(gov_url, project_id, "cron_reconcile_v2", ok=False, details=error_result)
        return error_result

    # Build structured log entry (R2)
    summary = result.get("summary", result)
    log_entry = {
        "timestamp": timestamp,
        "status": "ok",
        "dry_run": dry_run,
        "auto_fix_threshold": auto_fix_threshold,
        "summary": summary,
    }
    _append_log(json.dumps(log_entry))

    # Audit entry (R3)
    _emit_audit(gov_url, project_id, "cron_reconcile_v2", ok=True, details=log_entry)

    return log_entry


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    apply_mode = "--apply" in sys.argv
    result = cron_reconcile_v2(dry_run=not apply_mode)
    print(json.dumps(result, indent=2))
