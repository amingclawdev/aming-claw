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


class TestCommitGraphDeltaL7Nodes(unittest.TestCase):
    """AC2: _commit_graph_delta inserts L7 node rows into node_state for creates[] with parent_layer 7."""

    def _make_db_with_validated_event(self, creates, root_task_id="pm-root"):
        """Create an in-memory DB with node_state table and a graph.delta.validated event."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE chain_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                root_task_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                ts TEXT NOT NULL
            );
            CREATE TABLE node_state (
                project_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                verify_status TEXT NOT NULL DEFAULT 'pending',
                build_status TEXT NOT NULL DEFAULT 'unknown',
                evidence_json TEXT,
                updated_at TEXT,
                version INTEGER DEFAULT 1,
                updated_by TEXT,
                PRIMARY KEY (project_id, node_id)
            );
            CREATE TABLE node_history (
                project_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT,
                role TEXT,
                evidence_json TEXT,
                session_id TEXT,
                ts TEXT,
                version INTEGER DEFAULT 1
            );
        """)
        validated_payload = {
            "proposed_payload": {
                "source_task_id": "dev-001",
                "graph_delta": {
                    "creates": creates,
                    "updates": [],
                    "links": [],
                },
            },
        }
        conn.execute(
            "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) VALUES (?,?,?,?,?)",
            (root_task_id, "dev-001", "graph.delta.validated",
             json.dumps(validated_payload), "2026-04-25T00:00:00Z"),
        )
        conn.commit()
        return conn

    def test_l7_string_parent_layer_inserted(self):
        """AC2: parent_layer='L7' (string prefixed) is accepted and creates L7 node."""
        from governance.auto_chain import _commit_graph_delta

        creates = [
            {"parent_layer": "L7", "title": "New L7 node", "node_id": "L7.1",
             "deps": [], "primary": ["agent/foo.py"], "description": "test"},
        ]
        conn = self._make_db_with_validated_event(creates)
        metadata = {"chain_id": "pm-root"}

        # Patch chain_context.get_store
        import unittest.mock as mock
        mock_store = mock.MagicMock()
        mock_store._task_to_root = {"pm-root": "pm-root"}
        with mock.patch("governance.chain_context.get_store", return_value=mock_store):
            _commit_graph_delta(conn, "test-proj", metadata)

        row = conn.execute(
            "SELECT node_id, verify_status FROM node_state WHERE project_id='test-proj' AND node_id='L7.1'"
        ).fetchone()
        self.assertIsNotNone(row, "L7.1 should be inserted into node_state")
        self.assertEqual(row["verify_status"], "qa_pass")

    def test_l7_int_parent_layer_inserted(self):
        """AC2: parent_layer=7 (integer) is accepted and auto-generates L7.N node_id."""
        from governance.auto_chain import _commit_graph_delta

        creates = [
            {"parent_layer": 7, "title": "Auto-ID L7 node",
             "deps": [], "primary": ["agent/bar.py"], "description": "test"},
        ]
        conn = self._make_db_with_validated_event(creates)
        metadata = {"chain_id": "pm-root"}

        import unittest.mock as mock
        mock_store = mock.MagicMock()
        mock_store._task_to_root = {"pm-root": "pm-root"}
        with mock.patch("governance.chain_context.get_store", return_value=mock_store):
            _commit_graph_delta(conn, "test-proj", metadata)

        row = conn.execute(
            "SELECT node_id FROM node_state WHERE project_id='test-proj' AND node_id LIKE 'L7.%'"
        ).fetchone()
        self.assertIsNotNone(row, "L7.N should be auto-generated in node_state")
        self.assertEqual(row["node_id"], "L7.1")

    def test_l7_string_int_parent_layer_inserted(self):
        """AC2: parent_layer='7' (string numeric) is accepted."""
        from governance.auto_chain import _commit_graph_delta

        creates = [
            {"parent_layer": "7", "title": "String-int L7 node", "node_id": "L7.5",
             "deps": [], "primary": [], "description": "test"},
        ]
        conn = self._make_db_with_validated_event(creates)
        metadata = {"chain_id": "pm-root"}

        import unittest.mock as mock
        mock_store = mock.MagicMock()
        mock_store._task_to_root = {"pm-root": "pm-root"}
        with mock.patch("governance.chain_context.get_store", return_value=mock_store):
            _commit_graph_delta(conn, "test-proj", metadata)

        row = conn.execute(
            "SELECT node_id FROM node_state WHERE project_id='test-proj' AND node_id='L7.5'"
        ).fetchone()
        self.assertIsNotNone(row, "L7.5 should be inserted with parent_layer='7'")


class TestAllocateNextIdAC5(unittest.TestCase):
    """AC5: _allocate_next_id('L7') returns L7.(max+1) when existing nodes L7.1..L7.N exist."""

    def test_allocate_next_l7_id(self):
        """AC5: L7.1..L7.3 exist → next is L7.4."""
        from governance.reconcile_phases.phase_b import allocate_next_id
        result = allocate_next_id("L7", ["L7.1", "L7.2", "L7.3", "L3.1", "L3.2"])
        self.assertEqual(result, "L7.4")

    def test_allocate_with_gaps(self):
        """AC5: L7.1, L7.5 exist → next is L7.6 (max+1, no gap filling)."""
        from governance.reconcile_phases.phase_b import allocate_next_id
        result = allocate_next_id("L7", ["L7.1", "L7.5"])
        self.assertEqual(result, "L7.6")

    def test_allocate_empty_graph(self):
        """AC5: No L7 nodes → L7.1."""
        from governance.reconcile_phases.phase_b import allocate_next_id
        result = allocate_next_id("L7", ["L3.1", "L3.2"])
        self.assertEqual(result, "L7.1")

    def test_allocate_no_collision(self):
        """AC5: Monotonically increasing, no collision with existing."""
        from governance.reconcile_phases.phase_b import allocate_next_id
        existing = ["L7.1", "L7.2", "L7.3"]
        id1 = allocate_next_id("L7", existing)
        self.assertEqual(id1, "L7.4")
        existing.append(id1)
        id2 = allocate_next_id("L7", existing)
        self.assertEqual(id2, "L7.5")


class TestGatekeeperBlocksOnCommitFailure(unittest.TestCase):
    """AC3: _gate_gatekeeper_pass returns (False, ...) when _commit_graph_delta raises."""

    def test_gate_returns_false_on_commit_exception(self):
        """AC3: Graph delta commit failure → gate blocked."""
        from governance.auto_chain import _gate_gatekeeper_pass
        import unittest.mock as mock

        result = {"recommendation": "merge_pass"}
        metadata = {"chain_id": "pm-root"}
        conn = mock.MagicMock()

        with mock.patch("governance.auto_chain._commit_graph_delta", side_effect=RuntimeError("L7 write failed")):
            passed, reason = _gate_gatekeeper_pass(conn, "test-proj", result, metadata)

        self.assertFalse(passed)
        self.assertIn("graph delta commit failed", reason)
        self.assertIn("L7 write failed", reason)


if __name__ == "__main__":
    unittest.main()
