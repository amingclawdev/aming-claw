"""Tests for gatekeeper graph delta transactional commit (PR-C).

AC13: At least 5 tests covering:
  - transactional graph delta commit
  - idempotent re-run
  - attempted-vs-committed collision accounting
  - sequence generation
  - event lifecycle
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.db import SCHEMA_SQL
from governance.graph import AcceptanceGraph


def _make_db():
    """Create an in-memory governance DB with full schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _seed_validated_event(conn, root_task_id, source_task_id, creates=None,
                          updates=None, links=None):
    """Insert a graph.delta.validated event into chain_events."""
    proposed_payload = {
        "source_task_id": source_task_id,
        "graph_delta": {
            "creates": creates or [],
            "updates": updates or [],
            "links": links or [],
        },
    }
    payload = {
        "source_task_id": source_task_id,
        "graph_delta_review": {"decision": "pass", "issues": []},
        "proposed_payload": proposed_payload,
    }
    conn.execute(
        "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
        "VALUES (?, ?, 'graph.delta.validated', ?, datetime('now'))",
        (root_task_id, source_task_id, json.dumps(payload)),
    )
    conn.commit()


class TestCommitGraphDelta(unittest.TestCase):
    """Tests for _commit_graph_delta function."""

    def setUp(self):
        self.conn = _make_db()
        self.project_id = "test-proj"
        self.root_task_id = "root-001"
        # Patch get_connection to return our in-memory conn
        import governance.auto_chain as ac
        import governance.db as db
        self._orig_get_conn = db.get_connection
        db.get_connection = lambda pid: self.conn
        # Also ensure chain_context store has our root mapping
        from governance.chain_context import get_store
        self.store = get_store()
        # Reset store state
        self.store._chains = {}
        self.store._task_to_root = {}
        self.ac = ac

    def tearDown(self):
        import governance.db as db
        db.get_connection = self._orig_get_conn
        self.conn.close()

    def _make_metadata(self, **overrides):
        meta = {
            "chain_id": self.root_task_id,
            "parent_task_id": self.root_task_id,
            "task_id": "gk-001",
            "project_id": self.project_id,
            "related_nodes": [],
        }
        meta.update(overrides)
        return meta

    def test_sequence_generation(self):
        """AC4: Node IDs auto-generated using L{layer}.{next_seq} pattern."""
        # Seed existing nodes
        self.conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status, build_status, updated_at, version) "
            "VALUES (?, 'L5.1', 'pending', 'unknown', datetime('now'), 1)",
            (self.project_id,),
        )
        self.conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status, build_status, updated_at, version) "
            "VALUES (?, 'L5.3', 'pending', 'unknown', datetime('now'), 1)",
            (self.project_id,),
        )
        self.conn.commit()

        _seed_validated_event(
            self.conn, self.root_task_id, "dev-001",
            creates=[
                {"parent_layer": 5, "title": "NewNode1"},
                {"parent_layer": 5, "title": "NewNode2"},
            ],
        )

        metadata = self._make_metadata()
        result = self.ac._commit_graph_delta(self.conn, self.project_id, metadata)

        self.assertIsNotNone(result)
        self.assertIn("L5.4", result["committed_node_ids"])
        self.assertIn("L5.5", result["committed_node_ids"])

        # Verify nodes in DB
        row = self.conn.execute(
            "SELECT node_id FROM node_state WHERE project_id = ? AND node_id = 'L5.4'",
            (self.project_id,),
        ).fetchone()
        self.assertIsNotNone(row)

    def test_collision_records_attempt_without_commit(self):
        """Explicit node_id collision is audited but not counted as committed."""
        self.conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status, build_status, updated_at, version) "
            "VALUES (?, 'L3.1', 'qa_pass', 'unknown', datetime('now'), 1)",
            (self.project_id,),
        )
        self.conn.commit()

        _seed_validated_event(
            self.conn, self.root_task_id, "dev-001",
            creates=[
                {"parent_layer": 3, "title": "NewOk", "node_id": "L3.99"},
                {"parent_layer": 3, "title": "Collision", "node_id": "L3.1"},
            ],
        )

        metadata = self._make_metadata()
        result = self.ac._commit_graph_delta(self.conn, self.project_id, metadata)

        self.assertIsNotNone(result)
        self.assertIn("L3.99", result["attempted_node_ids"])
        self.assertIn("L3.1", result["attempted_node_ids"])
        self.assertIn("L3.99", result["committed_node_ids"])
        self.assertNotIn("L3.1", result["committed_node_ids"])

        # Non-colliding creates still commit.
        row = self.conn.execute(
            "SELECT node_id FROM node_state WHERE project_id = ? AND node_id = 'L3.99'",
            (self.project_id,),
        ).fetchone()
        self.assertIsNotNone(row)

        failed = self.conn.execute(
            "SELECT payload_json FROM chain_events WHERE event_type = 'graph.delta.failed'"
        ).fetchone()
        self.assertIsNone(failed)

    def test_idempotent_rerun(self):
        """AC6: Second commit with same source_event_id returns stored node_ids."""
        _seed_validated_event(
            self.conn, self.root_task_id, "dev-001",
            creates=[{"parent_layer": 10, "title": "IdempotentNode"}],
        )

        metadata = self._make_metadata()
        # First run
        result1 = self.ac._commit_graph_delta(self.conn, self.project_id, metadata)
        self.assertIsNotNone(result1)
        self.assertEqual(len(result1["committed_node_ids"]), 1)

        # Second run — should return same node_ids without writing
        result2 = self.ac._commit_graph_delta(self.conn, self.project_id, metadata)
        self.assertIsNotNone(result2)
        self.assertEqual(result1, result2)

        # Only one graph.delta.committed event
        committed_count = self.conn.execute(
            "SELECT COUNT(*) FROM chain_events WHERE event_type = 'graph.delta.committed'"
        ).fetchone()[0]
        self.assertEqual(committed_count, 1)

    def test_event_lifecycle(self):
        """AC3: Successful commit writes graph.delta.committed with event_id and node_ids."""
        _seed_validated_event(
            self.conn, self.root_task_id, "dev-001",
            creates=[{"parent_layer": 7, "title": "LifecycleNode"}],
        )

        metadata = self._make_metadata()
        result = self.ac._commit_graph_delta(self.conn, self.project_id, metadata)

        self.assertIsNotNone(result)

        # Check committed event
        row = self.conn.execute(
            "SELECT payload_json FROM chain_events WHERE event_type = 'graph.delta.committed'"
        ).fetchone()
        self.assertIsNotNone(row)

        payload = json.loads(row["payload_json"])
        self.assertIn("event_id", payload)
        self.assertIn("committed_node_ids", payload)
        self.assertEqual(payload["source_event_id"], "dev-001")
        self.assertIn("L7.1", payload["committed_node_ids"])

    def test_validated_create_is_qa_pass_with_evidence(self):
        """Validated creates must not re-block release gates as pending nodes."""
        _seed_validated_event(
            self.conn, self.root_task_id, "dev-001",
            creates=[{"parent_layer": 7, "title": "MaterializedNode"}],
        )

        metadata = self._make_metadata(task_id="gatekeeper-001")
        result = self.ac._commit_graph_delta(self.conn, self.project_id, metadata)

        self.assertIn("L7.1", result["committed_node_ids"])
        row = self.conn.execute(
            "SELECT verify_status, build_status, evidence_json, updated_by "
            "FROM node_state WHERE project_id = ? AND node_id = 'L7.1'",
            (self.project_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["verify_status"], "qa_pass")
        self.assertEqual(row["build_status"], "impl:done")
        self.assertEqual(row["updated_by"], "graph-delta-commit")

        evidence = json.loads(row["evidence_json"])
        self.assertEqual(evidence["type"], "graph_delta_committed_create")
        self.assertEqual(evidence["source_task_id"], "dev-001")
        self.assertEqual(evidence["gatekeeper_task_id"], "gatekeeper-001")

        passed, reason = self.ac._check_nodes_min_status(
            self.conn, self.project_id, ["L7.1"], "qa_pass",
        )
        self.assertTrue(passed, reason)

    def test_committed_create_and_update_are_persisted_to_project_graph(self):
        """Committed graph_delta mutations must be visible to graph readers."""
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td) / self.project_id
            project_dir.mkdir(parents=True)
            graph_path = project_dir / "graph.json"
            graph = AcceptanceGraph()
            graph.G.add_node(
                "L7.23",
                id="L7.23",
                title="Existing",
                layer="L7",
                verify_level=1,
                gate_mode="auto",
                test_coverage="none",
                primary=[],
                secondary=[],
                test=[],
                propagation=None,
                guard=False,
                version="",
                gates=[],
                verify_requires=[],
            )
            graph.save(graph_path)

            self.conn.execute(
                "INSERT INTO node_state (project_id, node_id, verify_status, build_status, updated_at, version) "
                "VALUES (?, 'L7.23', 'qa_pass', 'impl:done', datetime('now'), 1)",
                (self.project_id,),
            )
            self.conn.commit()

            _seed_validated_event(
                self.conn, self.root_task_id, "dev-001",
                creates=[{
                    "parent_layer": 7,
                    "title": "Scope Catch-up Materialization",
                    "primary": ["agent/governance/reconcile_scope_catchup.py"],
                    "secondary": ["docs/governance/reconcile-workflow.md"],
                }],
                updates=[{
                    "node_id": "L7.23",
                    "fields": {"secondary": ["docs/governance/auto-chain.md"]},
                }],
            )

            with mock.patch("governance.db._resolve_project_dir", return_value=project_dir):
                result = self.ac._commit_graph_delta(
                    self.conn,
                    self.project_id,
                    self._make_metadata(task_id="gatekeeper-001"),
                )

            self.assertIn("L7.24", result["committed_node_ids"])
            self.assertIn("L7.23", result["committed_node_ids"])

            reloaded = AcceptanceGraph()
            reloaded.load(graph_path)
            self.assertIn("L7.24", reloaded.list_nodes())
            self.assertEqual(
                reloaded.get_node("L7.24")["primary"],
                ["agent/governance/reconcile_scope_catchup.py"],
            )
            self.assertEqual(
                reloaded.get_node("L7.23")["secondary"],
                ["docs/governance/auto-chain.md"],
            )

    def test_collision_does_not_mark_attempt_as_committed(self):
        """Explicit collisions keep intent evidence without reporting mutation."""
        self.conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status, build_status, updated_at, version) "
            "VALUES (?, 'L2.5', 'pending', 'unknown', datetime('now'), 1)",
            (self.project_id,),
        )
        self.conn.commit()

        _seed_validated_event(
            self.conn, self.root_task_id, "dev-001",
            creates=[
                {"parent_layer": 2, "title": "Good"},
                {"parent_layer": 2, "title": "Bad", "node_id": "L2.5"},  # collision
            ],
        )

        metadata = self._make_metadata()
        result = self.ac._commit_graph_delta(self.conn, self.project_id, metadata)

        self.assertIsNotNone(result)
        self.assertIn("L2.6", result["attempted_node_ids"])
        self.assertIn("L2.5", result["attempted_node_ids"])
        self.assertIn("L2.6", result["committed_node_ids"])
        self.assertNotIn("L2.5", result["committed_node_ids"])

        failed = self.conn.execute(
            "SELECT payload_json FROM chain_events WHERE event_type = 'graph.delta.failed'"
        ).fetchone()
        self.assertIsNone(failed)

    def test_malformed_creates_skipped(self):
        """AC11: creates[] items with missing parent_layer are skipped without blocking."""
        _seed_validated_event(
            self.conn, self.root_task_id, "dev-001",
            creates=[
                {"title": "MissingLayer"},  # No parent_layer — should be skipped
                {"parent_layer": 8, "title": "ValidNode"},
            ],
        )

        metadata = self._make_metadata()
        result = self.ac._commit_graph_delta(self.conn, self.project_id, metadata)

        self.assertIsNotNone(result)
        self.assertEqual(len(result["committed_node_ids"]), 1)
        self.assertIn("L8.1", result["committed_node_ids"])

    def test_links_logged_as_skipped(self):
        """AC12: links[] items are logged but not persisted (no edges table)."""
        _seed_validated_event(
            self.conn, self.root_task_id, "dev-001",
            creates=[{"parent_layer": 9, "title": "WithLinks"}],
            links=[{"from_node": "L9.1", "to_node": "L1.1", "relation": "depends_on"}],
        )

        metadata = self._make_metadata()
        result = self.ac._commit_graph_delta(self.conn, self.project_id, metadata)

        # Creates should succeed despite links being present
        self.assertIsNotNone(result)
        self.assertIn("L9.1", result["committed_node_ids"])

    def test_related_nodes_carryforward(self):
        """AC7: After commit, new node_ids appended to metadata related_nodes."""
        _seed_validated_event(
            self.conn, self.root_task_id, "dev-001",
            creates=[{"parent_layer": 11, "title": "CarryNode"}],
        )

        metadata = self._make_metadata(related_nodes=["L1.1"])
        self.ac._commit_graph_delta(self.conn, self.project_id, metadata)

        # related_nodes should now include the new node
        self.assertIn("L11.1", metadata["related_nodes"])
        self.assertIn("L1.1", metadata["related_nodes"])

    def test_no_validated_event_noop(self):
        """No graph.delta.validated event → function returns None (no-op)."""
        metadata = self._make_metadata()
        result = self.ac._commit_graph_delta(self.conn, self.project_id, metadata)
        self.assertIsNone(result)

    def test_updates_applied(self):
        """Updates to existing nodes are applied within the transaction."""
        self.conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status, build_status, updated_at, version) "
            "VALUES (?, 'L4.1', 'pending', 'impl:missing', datetime('now'), 1)",
            (self.project_id,),
        )
        self.conn.commit()

        _seed_validated_event(
            self.conn, self.root_task_id, "dev-001",
            updates=[{"node_id": "L4.1", "fields": {"build_status": "impl:done"}}],
        )

        metadata = self._make_metadata()
        result = self.ac._commit_graph_delta(self.conn, self.project_id, metadata)

        self.assertIsNotNone(result)
        row = self.conn.execute(
            "SELECT build_status FROM node_state WHERE project_id = ? AND node_id = 'L4.1'",
            (self.project_id,),
        ).fetchone()
        self.assertEqual(row["build_status"], "impl:done")


class TestRolledBackNonBlocking(unittest.TestCase):
    """AC10: rolled_back status doesn't block gates."""

    def test_rolled_back_in_check_nodes_min_status(self):
        """rolled_back nodes should not block _check_nodes_min_status."""
        conn = _make_db()
        project_id = "test-proj"

        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status, build_status, updated_at, version) "
            "VALUES (?, 'L1.1', 'rolled_back', 'unknown', datetime('now'), 1)",
            (project_id,),
        )
        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status, build_status, updated_at, version) "
            "VALUES (?, 'L1.2', 'qa_pass', 'unknown', datetime('now'), 1)",
            (project_id,),
        )
        conn.commit()

        from governance.auto_chain import _check_nodes_min_status
        passed, reason = _check_nodes_min_status(conn, project_id, ["L1.1", "L1.2"], "qa_pass")
        self.assertTrue(passed)
        conn.close()

    def test_rolled_back_in_verify_requires(self):
        """rolled_back nodes should not block _check_verify_requires_satisfied."""
        conn = _make_db()
        project_id = "test-proj"

        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status, build_status, updated_at, version) "
            "VALUES (?, 'L1.1', 'rolled_back', 'unknown', datetime('now'), 1)",
            (project_id,),
        )
        conn.commit()

        from governance.auto_chain import _check_verify_requires_satisfied
        satisfied, blocking = _check_verify_requires_satisfied(conn, project_id, ["L1.1"])
        self.assertTrue(satisfied)
        self.assertEqual(blocking, [])
        conn.close()


if __name__ == "__main__":
    unittest.main()
