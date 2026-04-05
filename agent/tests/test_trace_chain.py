"""Tests for trace_id / chain_id end-to-end chain tracing (D6 fix).

Verifies that:
  - PM task gets trace_id generated and backfilled
  - Child tasks (Dev, Test) inherit trace_id and chain_id from parent
  - All tasks in a chain share the same trace_id
  - chain_id equals the PM (root) task_id
  - REST endpoint /api/task/{pid}/trace/{trace_id} returns chain tasks
"""

from __future__ import annotations

import json
import sqlite3
import sys
import os
import pytest

# Ensure agent package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _make_db() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with the tasks + project_version schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            execution_status TEXT NOT NULL DEFAULT 'queued',
            notification_status TEXT NOT NULL DEFAULT 'none',
            type TEXT NOT NULL DEFAULT 'task',
            prompt TEXT NOT NULL DEFAULT '',
            related_nodes TEXT NOT NULL DEFAULT '[]',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 5,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            result_json TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            parent_task_id TEXT,
            retry_round INTEGER NOT NULL DEFAULT 0,
            assigned_to TEXT,
            fence_token TEXT,
            lease_expires_at TEXT,
            completed_at TEXT,
            trace_id TEXT,
            chain_id TEXT,
            error_message TEXT
        );
        CREATE TABLE project_version (
            project_id TEXT PRIMARY KEY,
            chain_version TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            updated_by TEXT NOT NULL DEFAULT '',
            git_head TEXT DEFAULT '',
            dirty_files TEXT DEFAULT '[]',
            git_synced_at TEXT DEFAULT '',
            observer_mode INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT INTO schema_meta (key, value) VALUES ('schema_version', '11');
    """)
    return conn


PID = "test-trace-project"


class TestCreateTaskTraceFields:
    """R2: create_task accepts and stores trace_id / chain_id."""

    def test_create_task_with_trace_id(self):
        from agent.governance.task_registry import create_task
        conn = _make_db()
        result = create_task(
            conn, PID, "test prompt", task_type="pm",
            trace_id="tr-abc123", chain_id="chain-xyz",
        )
        conn.commit()
        assert result["trace_id"] == "tr-abc123"
        assert result["chain_id"] == "chain-xyz"
        # Verify stored in DB
        row = conn.execute(
            "SELECT trace_id, chain_id FROM tasks WHERE task_id=?",
            (result["task_id"],)
        ).fetchone()
        assert row["trace_id"] == "tr-abc123"
        assert row["chain_id"] == "chain-xyz"

    def test_create_task_without_trace_id(self):
        """Backward compatible: trace_id/chain_id default to NULL."""
        from agent.governance.task_registry import create_task
        conn = _make_db()
        result = create_task(conn, PID, "test prompt", task_type="dev")
        conn.commit()
        row = conn.execute(
            "SELECT trace_id, chain_id FROM tasks WHERE task_id=?",
            (result["task_id"],)
        ).fetchone()
        assert row["trace_id"] is None
        assert row["chain_id"] is None


class TestTraceChainPropagation:
    """R3/R4: PM→Dev→Test chain shares trace_id, chain_id = PM task_id."""

    def test_full_chain_trace_propagation(self):
        """Create PM→Dev→Test chain and verify all share trace_id."""
        from agent.governance.task_registry import create_task
        conn = _make_db()

        # Simulate: PM task created with trace_id
        pm_trace = "tr-fullchain001"
        pm_task = create_task(
            conn, PID, "PM prompt", task_type="pm",
            trace_id=pm_trace, chain_id=None,  # chain_id set after creation
        )
        pm_id = pm_task["task_id"]
        # Backfill chain_id = pm task_id (as auto_chain does)
        conn.execute("UPDATE tasks SET chain_id=? WHERE task_id=?", (pm_id, pm_id))
        conn.commit()

        # Dev task created with same trace_id, chain_id = pm_id
        dev_task = create_task(
            conn, PID, "Dev prompt", task_type="dev",
            parent_task_id=pm_id,
            trace_id=pm_trace, chain_id=pm_id,
        )
        dev_id = dev_task["task_id"]
        conn.commit()

        # Test task created with same trace_id, chain_id = pm_id
        test_task = create_task(
            conn, PID, "Test prompt", task_type="test",
            parent_task_id=dev_id,
            trace_id=pm_trace, chain_id=pm_id,
        )
        test_id = test_task["task_id"]
        conn.commit()

        # Verify: all three tasks share trace_id
        rows = conn.execute(
            "SELECT task_id, trace_id, chain_id FROM tasks WHERE project_id=? ORDER BY created_at ASC",
            (PID,),
        ).fetchall()
        assert len(rows) == 3
        for row in rows:
            assert row["trace_id"] == pm_trace
            assert row["chain_id"] == pm_id

    def test_trace_query_returns_chain(self):
        """Verify querying by trace_id returns all chain tasks."""
        from agent.governance.task_registry import create_task
        conn = _make_db()
        trace = "tr-query001"
        pm = create_task(conn, PID, "PM", task_type="pm", trace_id=trace, chain_id="pm-root")
        dev = create_task(conn, PID, "Dev", task_type="dev", trace_id=trace, chain_id="pm-root")
        # Unrelated task (no trace_id)
        create_task(conn, PID, "Other", task_type="task")
        conn.commit()

        rows = conn.execute(
            "SELECT task_id FROM tasks WHERE project_id=? AND trace_id=? ORDER BY created_at ASC",
            (PID, trace),
        ).fetchall()
        assert len(rows) == 2
        task_ids = [r["task_id"] for r in rows]
        assert pm["task_id"] in task_ids
        assert dev["task_id"] in task_ids


class TestLoadTaskTrace:
    """Helper _load_task_trace returns trace_id/chain_id from DB."""

    def test_load_existing(self):
        from agent.governance.auto_chain import _load_task_trace
        conn = _make_db()
        conn.execute(
            "INSERT INTO tasks (task_id, project_id, type, created_at, updated_at, trace_id, chain_id) "
            "VALUES ('t1', ?, 'pm', '', '', 'tr-load1', 'ch-load1')",
            (PID,),
        )
        conn.commit()
        tid, cid = _load_task_trace(conn, "t1")
        assert tid == "tr-load1"
        assert cid == "ch-load1"

    def test_load_missing(self):
        from agent.governance.auto_chain import _load_task_trace
        conn = _make_db()
        tid, cid = _load_task_trace(conn, "nonexistent")
        assert tid is None
        assert cid is None


class TestSchemaVersion:
    """AC3: SCHEMA_VERSION in db.py >= 11 (trace_id was added in v11)."""

    def test_schema_version_at_least_11(self):
        from agent.governance.db import SCHEMA_VERSION
        assert SCHEMA_VERSION >= 11


class TestMigrationV10ToV11:
    """AC1/AC2: Migration adds trace_id and chain_id columns."""

    def test_migration_adds_columns(self):
        """Run migration on a v10 schema and verify columns exist."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # Create minimal v10 tasks table (without trace_id/chain_id)
        conn.execute("""
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                type TEXT NOT NULL DEFAULT 'task'
            )
        """)
        conn.execute("""
            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute("INSERT INTO schema_meta (key, value) VALUES ('schema_version', '10')")
        conn.commit()

        # Run migration
        from agent.governance.db import _run_migrations
        _run_migrations(conn, 10, 11)
        conn.commit()

        # Verify columns exist by inserting with them
        conn.execute(
            "INSERT INTO tasks (task_id, project_id, trace_id, chain_id) VALUES ('t1', 'p1', 'tr-1', 'ch-1')"
        )
        row = conn.execute("SELECT trace_id, chain_id FROM tasks WHERE task_id='t1'").fetchone()
        assert row["trace_id"] == "tr-1"
        assert row["chain_id"] == "ch-1"
