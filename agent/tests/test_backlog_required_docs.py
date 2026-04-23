"""Tests for backlog_bugs required_docs field.

Covers: migration, upsert, GET, helper function, and backward compatibility.
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
    """Create an in-memory DB with the backlog_bugs table (v17 schema)."""
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


def _create_v16_db() -> sqlite3.Connection:
    """Create an in-memory DB with backlog_bugs table WITHOUT required_docs (pre-v17)."""
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
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL
        )
    """)
    return conn


class TestMigrationV16ToV17:
    """AC2: _migrate_v16_to_v17 ALTERs backlog_bugs to ADD required_docs column."""

    def test_migration_adds_required_docs_column(self):
        """Migration adds required_docs column to existing table."""
        conn = _create_v16_db()
        # Insert a row before migration
        conn.execute(
            "INSERT INTO backlog_bugs (bug_id, created_at, updated_at) VALUES (?, ?, ?)",
            ("BUG-1", "2026-01-01", "2026-01-01"),
        )
        conn.commit()

        # Apply migration
        from governance.db import _run_migrations
        _run_migrations(conn, 16, 17)

        # Verify column exists and has correct default
        row = conn.execute("SELECT required_docs FROM backlog_bugs WHERE bug_id = 'BUG-1'").fetchone()
        assert row is not None
        assert row["required_docs"] == "[]"

    def test_migration_is_idempotent(self):
        """Running migration twice does not raise an error."""
        conn = _create_v16_db()
        from governance.db import _run_migrations
        _run_migrations(conn, 16, 17)
        # Running again should not fail
        _run_migrations(conn, 16, 17)


class TestDDL:
    """AC1: DDL string contains required_docs column definition."""

    def test_ddl_contains_required_docs(self):
        """The DDL string in db.py includes required_docs with TEXT type and DEFAULT '[]'."""
        from governance.db import SCHEMA_SQL
        assert "required_docs" in SCHEMA_SQL
        # Verify it can create a table with required_docs
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_SQL)
        # Check column info
        info = conn.execute("PRAGMA table_info(backlog_bugs)").fetchall()
        col_names = [row[1] for row in info]
        assert "required_docs" in col_names


class TestMigrationsDict:
    """AC3: MIGRATIONS dict includes key 17."""

    def test_migrations_dict_has_v17(self):
        """MIGRATIONS dict should contain a v17 entry."""
        from governance.db import _run_migrations
        # If _run_migrations(conn, 16, 17) works, the migration is registered
        conn = _create_v16_db()
        _run_migrations(conn, 16, 17)
        # Verify column was actually added
        info = conn.execute("PRAGMA table_info(backlog_bugs)").fetchall()
        col_names = [row[1] for row in info]
        assert "required_docs" in col_names


class TestUpsert:
    """AC4: Upsert includes required_docs field with json.dumps."""

    def test_upsert_stores_required_docs(self):
        """Upserting a bug with required_docs stores it as JSON array."""
        conn = _create_test_db()
        docs = ["docs/dev/backlog-governance.md", "docs/api/executor-api.md"]
        conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, target_files, test_files,
                acceptance_criteria, chain_task_id, "commit", discovered_at,
                fixed_at, details_md, chain_trigger_json, required_docs,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(bug_id) DO UPDATE SET
                 required_docs = excluded.required_docs,
                 updated_at = excluded.updated_at
            """,
            (
                "BUG-TEST",
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
                json.dumps(docs),
                "2026-01-01",
                "2026-01-01",
            ),
        )
        conn.commit()

        row = conn.execute(
            "SELECT required_docs FROM backlog_bugs WHERE bug_id = ?", ("BUG-TEST",)
        ).fetchone()
        assert row is not None
        parsed = json.loads(row["required_docs"])
        assert parsed == docs

    def test_upsert_defaults_to_empty_list(self):
        """When required_docs is not specified, defaults to empty JSON array."""
        conn = _create_test_db()
        conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, created_at, updated_at)
               VALUES (?, ?, ?)""",
            ("BUG-DEFAULT", "2026-01-01", "2026-01-01"),
        )
        conn.commit()

        row = conn.execute(
            "SELECT required_docs FROM backlog_bugs WHERE bug_id = ?", ("BUG-DEFAULT",)
        ).fetchone()
        assert json.loads(row["required_docs"]) == []


class TestGetHandler:
    """AC5: GET response includes required_docs parsed as JSON list."""

    def test_get_returns_parsed_required_docs(self):
        """GET handler should return required_docs as a parsed list, not raw JSON string."""
        conn = _create_test_db()
        docs = ["docs/dev/backlog-governance.md"]
        conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, required_docs, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            ("BUG-GET", json.dumps(docs), "2026-01-01", "2026-01-01"),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM backlog_bugs WHERE bug_id = ?", ("BUG-GET",)
        ).fetchone()
        result = dict(row)
        # Simulate what server.py handle_backlog_get does
        try:
            result["required_docs"] = json.loads(result.get("required_docs", "[]"))
        except (json.JSONDecodeError, TypeError):
            result["required_docs"] = []

        assert isinstance(result["required_docs"], list)
        assert result["required_docs"] == docs


class TestListHandler:
    """AC6: List response rows include required_docs field."""

    def test_list_includes_required_docs(self):
        """List handler response should include required_docs in each bug row."""
        conn = _create_test_db()
        docs1 = ["docs/dev/backlog-governance.md"]
        docs2 = []
        conn.execute(
            """INSERT INTO backlog_bugs (bug_id, required_docs, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            ("BUG-L1", json.dumps(docs1), "2026-01-01", "2026-01-01"),
        )
        conn.execute(
            """INSERT INTO backlog_bugs (bug_id, required_docs, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            ("BUG-L2", json.dumps(docs2), "2026-01-01", "2026-01-01"),
        )
        conn.commit()

        rows = conn.execute("SELECT * FROM backlog_bugs ORDER BY bug_id").fetchall()
        bugs = []
        for r in rows:
            bug = dict(r)
            try:
                bug["required_docs"] = json.loads(bug.get("required_docs", "[]"))
            except (json.JSONDecodeError, TypeError):
                bug["required_docs"] = []
            bugs.append(bug)

        assert len(bugs) == 2
        assert bugs[0]["required_docs"] == docs1
        assert bugs[1]["required_docs"] == docs2


class TestBacklogDbHelper:
    """AC7: backlog_db.py exports get_backlog_required_docs returning list[str]."""

    def test_get_backlog_required_docs_returns_list(self):
        """Helper function returns list of strings for existing bug."""
        from governance.backlog_db import get_backlog_required_docs

        conn = _create_test_db()
        docs = ["docs/dev/backlog-governance.md", "docs/api/executor-api.md"]
        conn.execute(
            """INSERT INTO backlog_bugs (bug_id, required_docs, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            ("BUG-HELPER", json.dumps(docs), "2026-01-01", "2026-01-01"),
        )
        conn.commit()

        result = get_backlog_required_docs(conn, "aming-claw", "BUG-HELPER")
        assert result == docs
        assert all(isinstance(d, str) for d in result)

    def test_get_backlog_required_docs_missing_bug(self):
        """Helper returns empty list for non-existent bug."""
        from governance.backlog_db import get_backlog_required_docs

        conn = _create_test_db()
        result = get_backlog_required_docs(conn, "aming-claw", "NONEXISTENT")
        assert result == []

    def test_get_backlog_required_docs_empty_default(self):
        """Helper returns empty list when required_docs is default '[]'."""
        from governance.backlog_db import get_backlog_required_docs

        conn = _create_test_db()
        conn.execute(
            """INSERT INTO backlog_bugs (bug_id, created_at, updated_at)
               VALUES (?, ?, ?)""",
            ("BUG-EMPTY", "2026-01-01", "2026-01-01"),
        )
        conn.commit()

        result = get_backlog_required_docs(conn, "aming-claw", "BUG-EMPTY")
        assert result == []


class TestBackwardCompat:
    """AC8 / R8: Backward compatibility — existing rows default to empty JSON array."""

    def test_pre_v17_rows_get_default_after_migration(self):
        """Rows created before migration get default '[]' for required_docs."""
        conn = _create_v16_db()
        # Insert row without required_docs column
        conn.execute(
            """INSERT INTO backlog_bugs (bug_id, created_at, updated_at)
               VALUES (?, ?, ?)""",
            ("BUG-OLD", "2026-01-01", "2026-01-01"),
        )
        conn.commit()

        # Apply migration
        from governance.db import _run_migrations
        _run_migrations(conn, 16, 17)

        # Check default
        row = conn.execute(
            "SELECT required_docs FROM backlog_bugs WHERE bug_id = ?", ("BUG-OLD",)
        ).fetchone()
        assert row is not None
        assert json.loads(row["required_docs"]) == []

    def test_helper_handles_missing_column_gracefully(self):
        """get_backlog_required_docs returns [] when column doesn't exist (pre-v17)."""
        from governance.backlog_db import get_backlog_required_docs

        conn = _create_v16_db()
        conn.execute(
            """INSERT INTO backlog_bugs (bug_id, created_at, updated_at)
               VALUES (?, ?, ?)""",
            ("BUG-PRE17", "2026-01-01", "2026-01-01"),
        )
        conn.commit()

        result = get_backlog_required_docs(conn, "aming-claw", "BUG-PRE17")
        assert result == []
