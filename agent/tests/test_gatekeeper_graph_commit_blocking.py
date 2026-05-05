"""Tests for gatekeeper graph delta commit BLOCKING enforcement (PR-C).

Verifies that _commit_graph_delta failures block the gatekeeper gate
instead of silently passing, and that the escape hatch works correctly.

Acceptance criteria covered:
  C1: ValueError blocks gate
  C2: Generic Exception blocks gate
  C3: Legacy chain (no validated event) passes gate
  C4: Happy path passes gate
  C5: Escape hatch (skip_graph_delta_validation) passes gate
  C6: _commit_graph_delta re-raises ValueError
  C7: _commit_graph_delta re-raises generic Exception
"""

import json
import os
import sqlite3
import sys
import unittest
from unittest.mock import patch, MagicMock

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.db import SCHEMA_SQL


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


class TestGatekeeperGraphCommitBlocking(unittest.TestCase):
    """Tests for _gate_gatekeeper_pass blocking on graph delta failures."""

    def setUp(self):
        self.conn = _make_db()
        self.project_id = "test-project"
        self.root_task_id = "root-task-001"
        self.result_pass = {"recommendation": "merge_pass"}
        self.base_metadata = {
            "chain_id": self.root_task_id,
            "task_id": "gk-task-001",
        }

    def tearDown(self):
        self.conn.close()

    def _call_gate(self, result=None, metadata=None):
        from governance.auto_chain import _gate_gatekeeper_pass
        return _gate_gatekeeper_pass(
            self.conn,
            self.project_id,
            result or self.result_pass,
            metadata or self.base_metadata,
        )

    def test_c1_valueerror_blocks_gate(self):
        """C1: When _commit_graph_delta raises ValueError, gate returns (False, ...)."""
        with patch("governance.auto_chain._commit_graph_delta",
                   side_effect=ValueError("node_id collision: L2.5")):
            passed, reason = self._call_gate()
        self.assertFalse(passed)
        self.assertIn("graph delta commit failed:", reason)
        self.assertIn("node_id collision", reason)

    def test_c2_generic_exception_blocks_gate(self):
        """C2: When _commit_graph_delta raises Exception, gate returns (False, ...)."""
        with patch("governance.auto_chain._commit_graph_delta",
                   side_effect=RuntimeError("DB transaction failed")):
            passed, reason = self._call_gate()
        self.assertFalse(passed)
        self.assertIn("graph delta commit failed:", reason)
        self.assertIn("DB transaction failed", reason)

    def test_c3_legacy_chain_no_validated_event_passes(self):
        """C3: Legacy chain with no graph.delta.validated event passes gate."""
        # No validated event seeded — _commit_graph_delta returns normally
        passed, reason = self._call_gate()
        self.assertTrue(passed)
        self.assertEqual(reason, "ok")

    def test_c4_happy_path_passes(self):
        """C4: Successful graph delta commit passes gate."""
        with patch("governance.auto_chain._commit_graph_delta", return_value=["L2.5"]):
            passed, reason = self._call_gate()
        self.assertTrue(passed)
        self.assertEqual(reason, "ok")

    def test_c5_escape_hatch_skips_commit(self):
        """C5: skip_graph_delta_validation=True with skip_reason bypasses commit."""
        metadata = {
            **self.base_metadata,
            "skip_graph_delta_validation": True,
            "skip_reason": "manual override for hotfix",
        }
        with patch("governance.auto_chain._commit_graph_delta") as mock_commit:
            passed, reason = self._call_gate(metadata=metadata)
        self.assertTrue(passed)
        self.assertEqual(reason, "ok")
        mock_commit.assert_not_called()

    def test_c5_escape_hatch_requires_skip_reason(self):
        """C5: skip_graph_delta_validation without skip_reason still calls commit."""
        metadata = {
            **self.base_metadata,
            "skip_graph_delta_validation": True,
            # No skip_reason
        }
        with patch("governance.auto_chain._commit_graph_delta") as mock_commit:
            passed, reason = self._call_gate(metadata=metadata)
        self.assertTrue(passed)
        # Should have called _commit_graph_delta since skip_reason is missing
        mock_commit.assert_called_once()

    def test_c5_escape_hatch_requires_true_not_truthy(self):
        """C5: skip_graph_delta_validation must be exactly True, not truthy string."""
        metadata = {
            **self.base_metadata,
            "skip_graph_delta_validation": "true",  # string, not bool
            "skip_reason": "test",
        }
        with patch("governance.auto_chain._commit_graph_delta") as mock_commit:
            passed, reason = self._call_gate(metadata=metadata)
        self.assertTrue(passed)
        # String "true" is not `is True`, so commit should be called
        mock_commit.assert_called_once()

    def test_reject_still_works(self):
        """Gate rejection is unchanged."""
        result = {"recommendation": "reject", "reason": "failing tests"}
        passed, reason = self._call_gate(result=result)
        self.assertFalse(passed)
        self.assertIn("rejected", reason.lower())


class TestCommitGraphDeltaReRaises(unittest.TestCase):
    """Tests C6/C7: _commit_graph_delta failure handling."""

    def setUp(self):
        self.conn = _make_db()
        self.project_id = "test-project"
        self.root_task_id = "root-task-002"
        now = "2026-01-01T00:00:00Z"
        # Pre-insert a node so collision detection works
        self.conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status, build_status, updated_at, version) "
            "VALUES (?, 'L2.5', 'pending', 'unknown', ?, 1)",
            (self.project_id, now),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_c6_collision_is_attempted_not_committed(self):
        """C6: explicit ID collision is tracked without raising."""
        from governance.auto_chain import _commit_graph_delta

        # Seed a validated event that tries to create a node with colliding ID
        _seed_validated_event(
            self.conn, self.root_task_id, "qa-task",
            creates=[{
                "node_id": "L2.5",  # collision with pre-inserted node
                "parent_layer": "2",
                "title": "Colliding node",
                "deps": [],
                "primary": "auto_chain.py",
                "description": "collision test",
            }],
        )
        metadata = {"chain_id": self.root_task_id, "task_id": "qa-task"}

        result = _commit_graph_delta(self.conn, self.project_id, metadata)
        self.assertIsNotNone(result)
        self.assertIn("L2.5", result["attempted_node_ids"])
        self.assertNotIn("L2.5", result["committed_node_ids"])

        # Collision dedup is a committed audit event, not a failed graph transaction.
        row = self.conn.execute(
            "SELECT payload_json FROM chain_events WHERE event_type = 'graph.delta.failed'"
        ).fetchone()
        self.assertIsNone(row)
        committed = self.conn.execute(
            "SELECT payload_json FROM chain_events WHERE event_type = 'graph.delta.committed'"
        ).fetchone()
        self.assertIsNotNone(committed)

    def test_c7_reraises_generic_exception(self):
        """C7: _commit_graph_delta re-raises Exception after failed event write."""
        from governance.auto_chain import _commit_graph_delta

        _seed_validated_event(
            self.conn, self.root_task_id, "qa-task",
            creates=[{
                "node_id": "L3.1",
                "parent_layer": "3",
                "title": "Test node",
                "deps": [],
                "primary": "auto_chain.py",
                "description": "test",
            }],
        )
        metadata = {"chain_id": self.root_task_id, "task_id": "qa-task"}

        # Drop the node_state table to force a generic OperationalError
        # during the collision-check SELECT inside the transaction
        self.conn.execute("DROP TABLE node_state")
        self.conn.commit()

        with self.assertRaises(Exception) as ctx:
            _commit_graph_delta(self.conn, self.project_id, metadata)
        # Should be an OperationalError about missing table
        self.assertIsInstance(ctx.exception, Exception)
        self.assertNotIsInstance(ctx.exception, ValueError)


if __name__ == "__main__":
    unittest.main()
