"""Tests for governance DB migrations (v22 -> v23)."""

import sqlite3
import sys
from pathlib import Path

import pytest

# Ensure agent package is importable
_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.db import SCHEMA_SQL, SCHEMA_VERSION, _run_migrations


def _create_v22_db() -> sqlite3.Connection:
    """Create an in-memory DB migrated up to v22."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    # Run all migrations up to v22
    _run_migrations(conn, 0, 22)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
        ("schema_version", "22"),
    )
    conn.commit()
    return conn


def _get_column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return column names for a table via PRAGMA table_info."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [row[1] for row in rows]


def test_schema_version_is_23():
    """AC1: SCHEMA_VERSION should be 23."""
    assert SCHEMA_VERSION == 23


def test_migrate_v22_to_v23():
    """AC2/AC3/AC6: Migration adds 3 new columns to backlog_bugs."""
    conn = _create_v22_db()

    # Run v22->v23 migration
    _run_migrations(conn, 22, 23)

    columns = _get_column_names(conn, "backlog_bugs")
    assert "chain_stage" in columns
    assert "last_failure_reason" in columns
    assert "stage_updated_at" in columns

    conn.close()


def test_migrate_v22_to_v23_idempotent():
    """AC7: Calling the migration twice should not raise."""
    conn = _create_v22_db()

    # Run twice — second call must not error
    _run_migrations(conn, 22, 23)
    _run_migrations(conn, 22, 23)

    columns = _get_column_names(conn, "backlog_bugs")
    assert "chain_stage" in columns
    assert "last_failure_reason" in columns
    assert "stage_updated_at" in columns

    conn.close()


def test_fresh_db_has_new_columns():
    """AC4: Fresh DB via SCHEMA_SQL includes the 3 new columns."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    _run_migrations(conn, 0, SCHEMA_VERSION)

    columns = _get_column_names(conn, "backlog_bugs")
    assert "chain_stage" in columns
    assert "last_failure_reason" in columns
    assert "stage_updated_at" in columns

    conn.close()
