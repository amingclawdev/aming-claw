"""Tests for backlog_bugs provenance_paths field.

Covers: migration v17→v18, round-trip upsert+GET, default empty list, JSON persistence.
"""

import json
import sqlite3
import pytest
import sys
from pathlib import Path

# Ensure agent package is importable
_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)


def _create_test_db() -> sqlite3.Connection:
    """Create an in-memory DB with the backlog_bugs table (v18 schema)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backlog_bugs (
            bug_id              TEXT PRIMARY KEY,
            title               TEXT NOT NULL DEFAULT '',
            status              TEXT NOT NULL DEFAULT 'OPEN',
            priority            TEXT NOT NULL DEFAULT 'P3',
            target_files        TEXT NOT NULL DEFAULT '[]',
            test_files          TEXT NOT NULL DEFAULT '[]',
            acceptance_criteria TEXT NOT NULL DEFAULT '[]',
            chain_task_id       TEXT NOT NULL DEFAULT '',
            "commit"            TEXT NOT NULL DEFAULT '',
            discovered_at       TEXT NOT NULL DEFAULT '',
            fixed_at            TEXT NOT NULL DEFAULT '',
            details_md          TEXT NOT NULL DEFAULT '',
            chain_trigger_json  TEXT NOT NULL DEFAULT '{}',
            required_docs       TEXT NOT NULL DEFAULT '[]',
            provenance_paths    TEXT NOT NULL DEFAULT '[]',
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL
        )
    """)
    return conn


def _create_v17_db() -> sqlite3.Connection:
    """Create an in-memory DB with backlog_bugs table WITHOUT provenance_paths (pre-v18)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backlog_bugs (
            bug_id              TEXT PRIMARY KEY,
            title               TEXT NOT NULL DEFAULT '',
            status              TEXT NOT NULL DEFAULT 'OPEN',
            priority            TEXT NOT NULL DEFAULT 'P3',
            target_files        TEXT NOT NULL DEFAULT '[]',
            test_files          TEXT NOT NULL DEFAULT '[]',
            acceptance_criteria TEXT NOT NULL DEFAULT '[]',
            chain_task_id       TEXT NOT NULL DEFAULT '',
            "commit"            TEXT NOT NULL DEFAULT '',
            discovered_at       TEXT NOT NULL DEFAULT '',
            fixed_at            TEXT NOT NULL DEFAULT '',
            details_md          TEXT NOT NULL DEFAULT '',
            chain_trigger_json  TEXT NOT NULL DEFAULT '{}',
            required_docs       TEXT NOT NULL DEFAULT '[]',
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL
        )
    """)
    return conn


class TestMigrationV17ToV18:
    """Migration v17→v18 adds provenance_paths column."""

    def test_migration_adds_provenance_paths_column(self):
        """Migration adds provenance_paths column to existing table."""
        conn = _create_v17_db()
        conn.execute(
            "INSERT INTO backlog_bugs (bug_id, created_at, updated_at) VALUES (?, ?, ?)",
            ("BUG-1", "2026-01-01", "2026-01-01"),
        )
        conn.commit()

        from governance.db import _run_migrations
        _run_migrations(conn, 17, 18)

        row = conn.execute("SELECT provenance_paths FROM backlog_bugs WHERE bug_id = 'BUG-1'").fetchone()
        assert row is not None
        assert row["provenance_paths"] == "[]"

    def test_migration_is_idempotent(self):
        """Running migration twice does not raise an error."""
        conn = _create_v17_db()
        from governance.db import _run_migrations
        _run_migrations(conn, 17, 18)
        _run_migrations(conn, 17, 18)


class TestSchemaVersion:
    """SCHEMA_VERSION is 18 and MIGRATIONS dict contains key 18."""

    def test_schema_version_is_18(self):
        from governance.db import SCHEMA_VERSION
        assert SCHEMA_VERSION == 18

    def test_ddl_contains_provenance_paths(self):
        from governance.db import SCHEMA_SQL
        assert "provenance_paths" in SCHEMA_SQL
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_SQL)
        info = conn.execute("PRAGMA table_info(backlog_bugs)").fetchall()
        col_names = [row[1] for row in info]
        assert "provenance_paths" in col_names


class TestRoundTrip:
    """Round-trip: upsert with provenance_paths then GET and assert equality."""

    def test_upsert_and_get_with_provenance_paths(self):
        """Upsert with provenance_paths, read back, verify parsed list matches."""
        conn = _create_test_db()
        paths = ["docs/dev/x.md", "docs/dev/y.md"]
        conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, target_files, test_files,
                acceptance_criteria, chain_task_id, "commit", discovered_at,
                fixed_at, details_md, chain_trigger_json, required_docs,
                provenance_paths, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(bug_id) DO UPDATE SET
                 provenance_paths = excluded.provenance_paths,
                 updated_at = excluded.updated_at
            """,
            (
                "BUG-PROV",
                "Test bug",
                "OPEN",
                "P1",
                "[]",
                "[]",
                "[]",
                "",
                "",
                "",
                "",
                "",
                "{}",
                "[]",
                json.dumps(paths),
                "2026-01-01",
                "2026-01-01",
            ),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM backlog_bugs WHERE bug_id = ?", ("BUG-PROV",)
        ).fetchone()
        result = dict(row)
        # Simulate server.py handle_backlog_get parsing
        try:
            result["provenance_paths"] = json.loads(result.get("provenance_paths", "[]"))
        except (json.JSONDecodeError, TypeError):
            result["provenance_paths"] = []

        assert isinstance(result["provenance_paths"], list)
        assert result["provenance_paths"] == paths

    def test_default_empty_list_when_omitted(self):
        """When provenance_paths is not specified, defaults to empty JSON array."""
        conn = _create_test_db()
        conn.execute(
            "INSERT INTO backlog_bugs (bug_id, created_at, updated_at) VALUES (?, ?, ?)",
            ("BUG-DEFAULT", "2026-01-01", "2026-01-01"),
        )
        conn.commit()

        row = conn.execute(
            "SELECT provenance_paths FROM backlog_bugs WHERE bug_id = ?", ("BUG-DEFAULT",)
        ).fetchone()
        assert json.loads(row["provenance_paths"]) == []

    def test_empty_list_persists_as_json_array(self):
        """Explicitly storing empty list persists as '[]'."""
        conn = _create_test_db()
        conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, provenance_paths, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            ("BUG-EMPTY", json.dumps([]), "2026-01-01", "2026-01-01"),
        )
        conn.commit()

        row = conn.execute(
            "SELECT provenance_paths FROM backlog_bugs WHERE bug_id = ?", ("BUG-EMPTY",)
        ).fetchone()
        assert row["provenance_paths"] == "[]"
        assert json.loads(row["provenance_paths"]) == []


class TestListHandler:
    """List handler parses provenance_paths in each row."""

    def test_list_includes_provenance_paths(self):
        conn = _create_test_db()
        paths1 = ["docs/dev/x.md"]
        paths2 = []
        conn.execute(
            """INSERT INTO backlog_bugs (bug_id, provenance_paths, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            ("BUG-L1", json.dumps(paths1), "2026-01-01", "2026-01-01"),
        )
        conn.execute(
            """INSERT INTO backlog_bugs (bug_id, provenance_paths, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            ("BUG-L2", json.dumps(paths2), "2026-01-01", "2026-01-01"),
        )
        conn.commit()

        rows = conn.execute("SELECT * FROM backlog_bugs ORDER BY bug_id").fetchall()
        bugs = []
        for r in rows:
            bug = dict(r)
            try:
                bug["provenance_paths"] = json.loads(bug.get("provenance_paths", "[]"))
            except (json.JSONDecodeError, TypeError):
                bug["provenance_paths"] = []
            bugs.append(bug)

        assert len(bugs) == 2
        assert bugs[0]["provenance_paths"] == paths1
        assert bugs[1]["provenance_paths"] == paths2


class TestBackwardCompat:
    """Backward compatibility — pre-v18 rows get default '[]' after migration."""

    def test_pre_v18_rows_get_default_after_migration(self):
        conn = _create_v17_db()
        conn.execute(
            "INSERT INTO backlog_bugs (bug_id, created_at, updated_at) VALUES (?, ?, ?)",
            ("BUG-OLD", "2026-01-01", "2026-01-01"),
        )
        conn.commit()

        from governance.db import _run_migrations
        _run_migrations(conn, 17, 18)

        row = conn.execute(
            "SELECT provenance_paths FROM backlog_bugs WHERE bug_id = ?", ("BUG-OLD",)
        ).fetchone()
        assert row is not None
        assert json.loads(row["provenance_paths"]) == []
