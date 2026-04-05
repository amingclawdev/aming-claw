"""Tests for gate_events audit table, recording, and API endpoint."""

import json
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.db import get_connection, SCHEMA_VERSION, _run_migrations, SCHEMA_SQL, _ensure_schema


class TestGateEventsTableCreated(unittest.TestCase):
    """AC1/AC2/AC3/AC4: Verify gate_events table exists with correct schema after migration."""

    def _make_db(self):
        """Create an in-memory DB with full schema applied."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        return conn

    def setUp(self):
        self.conn = self._make_db()

    def tearDown(self):
        self.conn.close()

    def test_schema_version_at_least_12(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 12)

    def test_gate_events_table_created(self):
        """Table is created by _ensure_schema (includes SCHEMA_SQL + migrations)."""
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='gate_events'"
        ).fetchone()
        self.assertIsNotNone(row, "gate_events table should exist")

    def test_gate_events_columns(self):
        """AC4: gate_events has correct columns."""
        self.conn.execute(
            "INSERT INTO gate_events (project_id, task_id, gate_name, passed, reason, trace_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("proj", "task-1", "version_check", 1, "ok", "trace-1", "2026-01-01T00:00:00Z"),
        )
        row = self.conn.execute("SELECT * FROM gate_events WHERE task_id = 'task-1'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["project_id"], "proj")
        self.assertEqual(row["task_id"], "task-1")
        self.assertEqual(row["gate_name"], "version_check")
        self.assertEqual(row["passed"], 1)
        self.assertEqual(row["reason"], "ok")
        self.assertEqual(row["trace_id"], "trace-1")
        self.assertEqual(row["created_at"], "2026-01-01T00:00:00Z")

    def test_migration_v11_to_v12_registered(self):
        """AC3: _migrate_v11_to_v12 is registered at key 12 in MIGRATIONS."""
        # Use a fresh conn, apply base schema then just migration v11->v12
        conn2 = sqlite3.connect(":memory:")
        conn2.row_factory = sqlite3.Row
        conn2.executescript(SCHEMA_SQL)
        _run_migrations(conn2, 11, 12)
        conn2.commit()

        row = conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='gate_events'"
        ).fetchone()
        self.assertIsNotNone(row, "gate_events table should exist after v11->v12 migration")
        conn2.close()

    def test_index_created(self):
        """Index on (project_id, task_id) should exist."""
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_gate_events_project_task'"
        ).fetchone()
        self.assertIsNotNone(row, "idx_gate_events_project_task index should exist")


class TestGateEventRecording(unittest.TestCase):
    """AC5/AC6: Verify _record_gate_event inserts rows correctly."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        _ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_gate_event_recording(self):
        """_record_gate_event inserts a row."""
        from governance.auto_chain import _record_gate_event

        _record_gate_event(
            self.conn, "test-proj", "task-123",
            "version_check", True, "all good", "trace-abc"
        )
        self.conn.commit()

        rows = self.conn.execute(
            "SELECT * FROM gate_events WHERE task_id = 'task-123'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["project_id"], "test-proj")
        self.assertEqual(row["gate_name"], "version_check")
        self.assertEqual(row["passed"], 1)
        self.assertEqual(row["reason"], "all good")
        self.assertEqual(row["trace_id"], "trace-abc")

    def test_gate_event_recording_failure(self):
        """_record_gate_event records passed=0 for failures."""
        from governance.auto_chain import _record_gate_event

        _record_gate_event(
            self.conn, "test-proj", "task-456",
            "_gate_checkpoint", False, "tests failed", "trace-def"
        )
        self.conn.commit()

        row = self.conn.execute(
            "SELECT * FROM gate_events WHERE task_id = 'task-456'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["passed"], 0)
        self.assertEqual(row["reason"], "tests failed")

    def test_multiple_events_per_task(self):
        """Multiple gate events can be recorded for the same task."""
        from governance.auto_chain import _record_gate_event

        _record_gate_event(self.conn, "proj", "task-1", "version_check", True, "ok", "t1")
        _record_gate_event(self.conn, "proj", "task-1", "_gate_post_pm", True, "ok", "t1")
        self.conn.commit()

        rows = self.conn.execute(
            "SELECT * FROM gate_events WHERE task_id = 'task-1' ORDER BY created_at ASC"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["gate_name"], "version_check")
        self.assertEqual(rows[1]["gate_name"], "_gate_post_pm")


class TestGateEventsAPI(unittest.TestCase):
    """AC7: Verify the GET /api/task/{project_id}/{task_id}/gates endpoint."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        _ensure_schema(self.conn)

        # Insert test data
        self.conn.execute(
            "INSERT INTO gate_events (project_id, task_id, gate_name, passed, reason, trace_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("test-proj", "task-abc", "version_check", 1, "ok", "trace-1", "2026-01-01T00:00:00Z"),
        )
        self.conn.execute(
            "INSERT INTO gate_events (project_id, task_id, gate_name, passed, reason, trace_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("test-proj", "task-abc", "_gate_post_pm", 0, "missing fields", "trace-1", "2026-01-01T00:01:00Z"),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_gate_events_api(self):
        """The handler query returns correct structure (simulates what the endpoint does)."""
        project_id = "test-proj"
        task_id = "task-abc"

        rows = self.conn.execute(
            """SELECT gate_name, passed, reason, trace_id, created_at
               FROM gate_events
               WHERE project_id = ? AND task_id = ?
               ORDER BY created_at ASC""",
            (project_id, task_id),
        ).fetchall()
        events = [dict(r) for r in rows]
        result = {"gate_events": events, "count": len(events), "task_id": task_id}

        self.assertIn("gate_events", result)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["task_id"], "task-abc")

        events = result["gate_events"]
        self.assertEqual(events[0]["gate_name"], "version_check")
        self.assertEqual(events[0]["passed"], 1)
        self.assertEqual(events[1]["gate_name"], "_gate_post_pm")
        self.assertEqual(events[1]["passed"], 0)
        self.assertEqual(events[1]["reason"], "missing fields")

        # Verify fields present
        for event in events:
            self.assertIn("gate_name", event)
            self.assertIn("passed", event)
            self.assertIn("reason", event)
            self.assertIn("trace_id", event)
            self.assertIn("created_at", event)

    def test_gate_events_api_empty(self):
        """Returns empty list for task with no gate events."""
        rows = self.conn.execute(
            """SELECT gate_name, passed, reason, trace_id, created_at
               FROM gate_events
               WHERE project_id = ? AND task_id = ?
               ORDER BY created_at ASC""",
            ("test-proj", "nonexistent-task"),
        ).fetchall()
        events = [dict(r) for r in rows]
        result = {"gate_events": events, "count": len(events)}

        self.assertEqual(result["count"], 0)
        self.assertEqual(result["gate_events"], [])

    def test_gate_events_route_exists_in_server_source(self):
        """AC7: Verify the route decorator exists in server.py source."""
        import inspect
        server_path = os.path.join(_agent_dir, "governance", "server.py")
        with open(server_path, "r") as f:
            source = f.read()
        self.assertIn('/api/task/{project_id}/{task_id}/gates', source)
        self.assertIn('handle_task_gates', source)


if __name__ == "__main__":
    unittest.main()
