"""Backlog database helpers.

Provides utility functions for querying backlog_bugs data
outside the HTTP server context.
"""

import json
import sqlite3
from typing import List


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
