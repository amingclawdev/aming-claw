"""Backlog database helpers.

Provides utility functions for querying backlog_bugs data
outside the HTTP server context.
"""

import json
import sqlite3
from typing import Any, Callable, Dict, List, Optional


def get_backlog_required_docs(conn: sqlite3.Connection, project_id: str, bug_id: str) -> List[str]:
    """Return the required_docs list for a given backlog bug.

    Args:
        conn: SQLite connection (with row_factory=sqlite3.Row expected).
        project_id: Project identifier (unused for query but kept for API consistency).
        bug_id: The bug_id to look up.

    Returns:
        List of document path strings. Returns [] if bug not found or column missing.
    """
    try:
        row = conn.execute(
            "SELECT required_docs FROM backlog_bugs WHERE bug_id = ?",
            (bug_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        # Column doesn't exist yet (pre-v17 schema)
        return []

    if not row:
        return []

    raw = row[0] if isinstance(row, (tuple, list)) else row["required_docs"]
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return [str(item) for item in result]
        return []
    except (json.JSONDecodeError, TypeError):
        return []


FIXED_CLOSE_WAIVER_ALERT_SCHEMA_VERSION = "backlog_fixed_close_waiver_alert.v1"


def fixed_close_waiver_alerts(
    conn: sqlite3.Connection,
    project_id: str,
    can_close_resolver: Callable[[str], Optional[bool]],
    has_close_waiver_resolver: Callable[[str], bool],
) -> Dict[str, Any]:
    """Surface FIXED rows lacking close authorization as a governance alert.

    Criterion 2: a row in FIXED status must satisfy can_close=true OR carry a
    visible close-waiver marker. Any FIXED row with can_close=false and no
    recorded close-waiver state is an evidence-integrity alert.

    The two resolvers decouple this helper from the timeline subsystem:
      - ``can_close_resolver(bug_id)`` returns the precheck can_close (or None
        when the row is not MF-applicable / not evaluable — treated as no alert).
      - ``has_close_waiver_resolver(bug_id)`` returns whether an explicit,
        visible close-waiver state exists for the row.
    """

    try:
        rows = conn.execute(
            "SELECT bug_id, status FROM backlog_bugs WHERE status = 'FIXED'"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    alerts: List[Dict[str, Any]] = []
    for row in rows:
        bug_id = row[0] if isinstance(row, (tuple, list)) else row["bug_id"]
        bug_id = str(bug_id)
        can_close = can_close_resolver(bug_id)
        if can_close is None or bool(can_close):
            continue
        if has_close_waiver_resolver(bug_id):
            continue
        alerts.append(
            {
                "bug_id": bug_id,
                "status": "FIXED",
                "can_close": False,
                "has_close_waiver": False,
                "reason": "fixed_row_without_can_close_or_close_waiver",
            }
        )

    return {
        "schema_version": FIXED_CLOSE_WAIVER_ALERT_SCHEMA_VERSION,
        "project_id": project_id,
        "alert": bool(alerts),
        "status": "alert" if alerts else "ok",
        "alert_count": len(alerts),
        "alerts": alerts,
    }
