"""Tests for graph.delta.proposed event emission (OPT-BACKLOG-GRAPH-DELTA-CHAIN-COMMIT PR-A).

Covers:
  (a) AC A8: Non-empty graph_delta triggers exactly one graph.delta.proposed event
  (b) AC A7: Missing/empty graph_delta triggers NO event
  (c) AC A9: recover_from_db replays graph.delta.proposed without raising
"""

import json
import os
import sqlite3
import sys
import unittest

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.chain_context import ChainContextStore


def _make_in_memory_db():
    """Create an in-memory SQLite DB with chain_events schema."""
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chain_events_root ON chain_events(root_task_id, ts)")
    conn.commit()
    return conn


class TestGraphDeltaEventEmission(unittest.TestCase):
    """(a) AC A8: Non-empty graph_delta triggers exactly one graph.delta.proposed event."""

    def setUp(self):
        self.pid = "test-proj"
        self._persisted_events = []

        # Patch the module-level singleton store used by get_store()
        import governance.chain_context as cc_mod
        self.store = cc_mod._store
        self._orig_persist = self.store._persist_event

        def _capture_persist(root_task_id, task_id, event_type, payload, project_id):
            self._persisted_events.append({
                "root_task_id": root_task_id,
                "task_id": task_id,
                "event_type": event_type,
                "payload": payload,
                "project_id": project_id,
            })
        self.store._persist_event = _capture_persist

    def tearDown(self):
        self.store._persist_event = self._orig_persist

    def _setup_chain_with_dev_task(self):
        """Create a PM->dev chain in the store."""
        self.store.on_task_created({
            "task_id": "pm-001",
            "type": "pm",
            "prompt": "test",
            "parent_task_id": "",
            "project_id": self.pid,
        })
        self.store.on_task_completed({
            "task_id": "pm-001",
            "result": {"target_files": ["a.py"]},
            "project_id": self.pid,
        })
        self.store.on_task_created({
            "task_id": "dev-001",
            "type": "dev",
            "prompt": "implement",
            "parent_task_id": "pm-001",
            "project_id": self.pid,
        })

    def test_nonempty_graph_delta_emits_event(self):
        """AC A8: Non-empty graph_delta emits exactly one graph.delta.proposed event."""
        self._setup_chain_with_dev_task()

        from governance.auto_chain import _emit_graph_delta_event

        result = {
            "changed_files": ["a.py"],
            "graph_delta": {
                "creates": [
                    {
                        "parent_layer": "L2",
                        "title": "New feature node",
                        "deps": ["L1.1"],
                        "primary": "a.py",
                        "description": "Implements feature X",
                    }
                ],
                "updates": [],
                "links": [{"from_node": "L2.new", "to_node": "L1.1", "relation": "depends_on"}],
            },
        }

        _emit_graph_delta_event(self.pid, "dev-001", result)

        # Exactly one graph.delta.proposed event
        delta_events = [e for e in self._persisted_events if e["event_type"] == "graph.delta.proposed"]
        self.assertEqual(len(delta_events), 1)

        evt = delta_events[0]
        self.assertEqual(evt["task_id"], "dev-001")
        self.assertEqual(evt["payload"]["source_task_id"], "dev-001")
        self.assertEqual(len(evt["payload"]["graph_delta"]["creates"]), 1)
        self.assertEqual(len(evt["payload"]["graph_delta"]["links"]), 1)
        self.assertEqual(evt["payload"]["graph_delta"]["updates"], [])


class TestGraphDeltaNoEvent(unittest.TestCase):
    """(b) AC A7: Missing/empty graph_delta triggers NO event."""

    def setUp(self):
        self.pid = "test-proj"
        self._persisted_events = []

        import governance.chain_context as cc_mod
        self.store = cc_mod._store
        self._orig_persist = self.store._persist_event

        def _capture_persist(root_task_id, task_id, event_type, payload, project_id):
            self._persisted_events.append({
                "event_type": event_type,
            })
        self.store._persist_event = _capture_persist

    def tearDown(self):
        self.store._persist_event = self._orig_persist

    def _setup_chain(self):
        self.store.on_task_created({
            "task_id": "pm-002",
            "type": "pm",
            "prompt": "test",
            "parent_task_id": "",
            "project_id": self.pid,
        })
        self.store.on_task_created({
            "task_id": "dev-002",
            "type": "dev",
            "prompt": "implement",
            "parent_task_id": "pm-002",
            "project_id": self.pid,
        })

    def test_no_graph_delta_key(self):
        """No graph_delta key => no event."""
        self._setup_chain()
        from governance.auto_chain import _emit_graph_delta_event

        result = {"changed_files": ["a.py"], "summary": "refactor"}
        _emit_graph_delta_event(self.pid, "dev-002", result)

        delta_events = [e for e in self._persisted_events if e["event_type"] == "graph.delta.proposed"]
        self.assertEqual(len(delta_events), 0)

    def test_graph_delta_none(self):
        """graph_delta=None => no event."""
        self._setup_chain()
        from governance.auto_chain import _emit_graph_delta_event

        result = {"changed_files": ["a.py"], "graph_delta": None}
        _emit_graph_delta_event(self.pid, "dev-002", result)

        delta_events = [e for e in self._persisted_events if e["event_type"] == "graph.delta.proposed"]
        self.assertEqual(len(delta_events), 0)

    def test_graph_delta_empty_arrays(self):
        """graph_delta with all empty sub-arrays => no event."""
        self._setup_chain()
        from governance.auto_chain import _emit_graph_delta_event

        result = {"graph_delta": {"creates": [], "updates": [], "links": []}}
        _emit_graph_delta_event(self.pid, "dev-002", result)

        delta_events = [e for e in self._persisted_events if e["event_type"] == "graph.delta.proposed"]
        self.assertEqual(len(delta_events), 0)


class TestGraphDeltaRecovery(unittest.TestCase):
    """(c) AC A9: recover_from_db replays graph.delta.proposed events without raising."""

    def test_recover_with_graph_delta_event(self):
        """graph.delta.proposed events in chain_events are replayed without error."""
        store = ChainContextStore()
        pid = "test-proj"

        # Create an in-memory DB with chain_events including graph.delta.proposed
        db = _make_in_memory_db()

        # Insert a task.created event so chain exists
        db.execute(
            "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) VALUES (?, ?, ?, ?, ?)",
            ("pm-003", "pm-003", "task.created",
             json.dumps({"task_id": "pm-003", "type": "pm", "prompt": "test",
                         "parent_task_id": "", "project_id": pid}),
             "2026-04-22T00:00:00Z"),
        )
        # Insert a graph.delta.proposed event
        db.execute(
            "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) VALUES (?, ?, ?, ?, ?)",
            ("pm-003", "dev-003", "graph.delta.proposed",
             json.dumps({"source_task_id": "dev-003",
                         "graph_delta": {"creates": [{"title": "N1"}], "updates": [], "links": []}}),
             "2026-04-22T00:01:00Z"),
        )
        db.commit()

        # Patch get_connection to return our in-memory DB
        import governance.chain_context as cc_module
        original_get_conn = None
        try:
            import governance.db as db_module
            original_get_conn = db_module.get_connection
            db_module.get_connection = lambda proj_id: db
            # Should NOT raise
            store.recover_from_db(pid)
        finally:
            if original_get_conn:
                db_module.get_connection = original_get_conn

        # Verify chain was recovered (task.created processed)
        self.assertIn("pm-003", store._chains)


class TestGraphDeltaDefaults(unittest.TestCase):
    """AC A1: graph_delta sub-arrays default to empty."""

    def setUp(self):
        self.pid = "test-proj"
        self._persisted_events = []

        import governance.chain_context as cc_mod
        self.store = cc_mod._store
        self._orig_persist = self.store._persist_event

        def _capture_persist(root_task_id, task_id, event_type, payload, project_id):
            self._persisted_events.append({
                "event_type": event_type,
                "payload": payload,
            })
        self.store._persist_event = _capture_persist

        # Set up chain
        self.store.on_task_created({
            "task_id": "pm-004", "type": "pm", "prompt": "test",
            "parent_task_id": "", "project_id": self.pid,
        })
        self.store.on_task_created({
            "task_id": "dev-004", "type": "dev", "prompt": "impl",
            "parent_task_id": "pm-004", "project_id": self.pid,
        })

    def tearDown(self):
        self.store._persist_event = self._orig_persist

    def test_partial_graph_delta_defaults(self):
        """Missing sub-arrays default to []."""
        from governance.auto_chain import _emit_graph_delta_event

        # Only 'creates' provided, updates and links missing
        result = {
            "graph_delta": {
                "creates": [{"parent_layer": "L2", "title": "Node", "deps": [], "primary": "b.py", "description": "desc"}],
            }
        }
        _emit_graph_delta_event(self.pid, "dev-004", result)

        delta_events = [e for e in self._persisted_events if e["event_type"] == "graph.delta.proposed"]
        self.assertEqual(len(delta_events), 1)
        gd = delta_events[0]["payload"]["graph_delta"]
        self.assertEqual(gd["updates"], [])
        self.assertEqual(gd["links"], [])
        self.assertEqual(len(gd["creates"]), 1)


if __name__ == "__main__":
    unittest.main()
