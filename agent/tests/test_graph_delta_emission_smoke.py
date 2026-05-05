"""Smoke tests for graph delta emission: _persist_event and _emit_or_infer_graph_delta."""

import json
import os
import sqlite3
import sys
import unittest
from unittest.mock import patch, MagicMock

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)


class _NoCloseConnection:
    """Wrapper around sqlite3.Connection that suppresses close() for testing."""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass  # Suppress close so in-memory DB survives

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _make_test_db():
    """Create an in-memory SQLite DB with chain_events table, wrapped to prevent close."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chain_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            root_task_id  TEXT NOT NULL,
            task_id       TEXT NOT NULL,
            event_type    TEXT NOT NULL,
            payload_json  TEXT NOT NULL,
            ts            TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


class TestPersistEvent(unittest.TestCase):
    """AC12: Test _persist_event with sample graph.delta.proposed payload."""

    def test_persist_event_writes_chain_events_row(self):
        """Verify _persist_event inserts a row into chain_events with correct fields."""
        from governance.chain_context import ChainContextStore

        store = ChainContextStore()
        raw_conn = _make_test_db()
        wrapped = _NoCloseConnection(raw_conn)

        payload = {
            "source_task_id": "dev-task-001",
            "source": "dev-emitted",
            "graph_delta": {
                "creates": [{"node_id": "L3.99", "title": "Test node"}],
                "updates": [],
                "links": [],
            },
        }

        store._persist_event(
            root_task_id="root-task-001",
            task_id="dev-task-001",
            event_type="graph.delta.proposed",
            payload=payload,
            project_id="test-proj",
            conn=wrapped,
        )
        raw_conn.commit()

        rows = raw_conn.execute(
            "SELECT * FROM chain_events WHERE event_type = 'graph.delta.proposed'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["root_task_id"], "root-task-001")
        self.assertEqual(row["task_id"], "dev-task-001")
        self.assertEqual(row["event_type"], "graph.delta.proposed")

        stored_payload = json.loads(row["payload_json"])
        self.assertEqual(stored_payload["source"], "dev-emitted")
        self.assertEqual(len(stored_payload["graph_delta"]["creates"]), 1)

    def test_persist_event_skipped_during_recovery(self):
        """Verify _persist_event is a no-op when _recovering is True."""
        from governance.chain_context import ChainContextStore

        store = ChainContextStore()
        store._recovering = True
        raw_conn = _make_test_db()

        with patch("governance.db.get_connection", return_value=_NoCloseConnection(raw_conn)):
            store._persist_event(
                root_task_id="root-001",
                task_id="task-001",
                event_type="graph.delta.proposed",
                payload={"test": True},
                project_id="test-proj",
            )

        rows = raw_conn.execute("SELECT * FROM chain_events").fetchall()
        self.assertEqual(len(rows), 0)


class TestEmitOrInferGraphDelta(unittest.TestCase):
    """AC13: Test _emit_or_infer_graph_delta with dev-provided graph_delta."""

    def test_dev_provided_graph_delta_emits_proposed(self):
        """Verify dev-provided graph_delta results in graph.delta.proposed chain_events row."""
        raw_conn = _make_test_db()
        wrapped = _NoCloseConnection(raw_conn)

        # Seed pm.prd.published so the lookup finds it
        raw_conn.execute(
            "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("root-dev-001", "pm-001", "pm.prd.published",
             json.dumps({"proposed_nodes": [{"node_id": "L3.1", "title": "Feature"}]}),
             "2026-01-01T00:00:00Z"),
        )
        raw_conn.commit()

        mock_store = MagicMock()
        mock_store._task_to_root = {"dev-task-002": "root-dev-001"}

        # Make _persist_event actually write to raw_conn
        def fake_persist(root_task_id, task_id, event_type, payload, project_id):
            raw_conn.execute(
                "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (root_task_id, task_id, event_type,
                 json.dumps(payload, ensure_ascii=False, default=str),
                 "2026-01-01T00:00:01Z"),
            )
            raw_conn.commit()

        mock_store._persist_event = fake_persist

        result = {
            "graph_delta": {
                "creates": [{"node_id": "L3.50", "title": "New node", "parent_layer": "L2.1"}],
                "updates": [],
                "links": [],
            },
            "changed_files": ["agent/governance/auto_chain.py"],
        }
        metadata = {"chain_id": "root-dev-001"}

        with patch("governance.chain_context.get_store", return_value=mock_store), \
             patch("governance.db.get_connection", return_value=wrapped):
            from governance.auto_chain import _emit_or_infer_graph_delta
            _emit_or_infer_graph_delta("test-proj", "dev-task-002", result, metadata)

        rows = raw_conn.execute(
            "SELECT * FROM chain_events WHERE event_type = 'graph.delta.proposed'"
        ).fetchall()
        self.assertGreaterEqual(len(rows), 1, "Expected at least one graph.delta.proposed row")

        # Check the row written by _persist_event
        proposed_rows = [
            r for r in rows
            if r["task_id"] == "dev-task-002"
        ]
        self.assertEqual(len(proposed_rows), 1, "Expected exactly one proposed row for dev-task-002")
        stored = json.loads(proposed_rows[0]["payload_json"])
        self.assertIn("graph_delta", stored)
        self.assertGreater(len(stored["graph_delta"].get("creates", [])), 0)

    def test_emit_or_infer_no_delta_no_emit(self):
        """Verify empty result with no graph_delta doesn't crash."""
        raw_conn = _make_test_db()
        wrapped = _NoCloseConnection(raw_conn)

        mock_store = MagicMock()
        mock_store._task_to_root = {}

        result = {}
        metadata = {}

        with patch("governance.chain_context.get_store", return_value=mock_store), \
             patch("governance.db.get_connection", return_value=wrapped):
            from governance.auto_chain import _emit_or_infer_graph_delta
            # Should not raise
            _emit_or_infer_graph_delta("test-proj", "task-empty", result, metadata)


if __name__ == "__main__":
    unittest.main()
