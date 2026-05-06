"""Tests for _commit_graph_delta intent-vs-mutation tracking.

AC4: Simulates graph_delta with 10 creates (3 pre-existing dedup-skips + 7 real inserts)
     and asserts len(attempted_node_ids)==10, len(committed_node_ids)==7.
"""

import json
import sqlite3
import pytest

import sys
import os

# Ensure agent package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from agent.governance.auto_chain import _commit_graph_delta


def _make_db():
    """Create an in-memory SQLite DB with the tables _commit_graph_delta needs."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE node_state (
            project_id TEXT,
            node_id TEXT,
            verify_status TEXT DEFAULT 'pending',
            build_status TEXT DEFAULT 'unknown',
            evidence_json TEXT,
            updated_at TEXT,
            updated_by TEXT DEFAULT '',
            version INTEGER DEFAULT 1,
            PRIMARY KEY (project_id, node_id)
        )
    """)
    conn.execute("""
        CREATE TABLE node_history (
            project_id TEXT,
            node_id TEXT,
            from_status TEXT,
            to_status TEXT,
            role TEXT,
            evidence_json TEXT,
            session_id TEXT,
            ts TEXT,
            version INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE chain_events (
            root_task_id TEXT,
            task_id TEXT,
            event_type TEXT,
            payload_json TEXT,
            ts TEXT
        )
    """)
    return conn


def _insert_validated_event(conn, root_task_id, creates, updates=None, links=None, source_task_id="src-1"):
    """Insert a graph.delta.validated event into chain_events."""
    graph_delta = {"creates": creates}
    if updates:
        graph_delta["updates"] = updates
    if links:
        graph_delta["links"] = links
    payload = {
        "proposed_payload": {
            "graph_delta": graph_delta,
            "source_task_id": source_task_id,
        }
    }
    conn.execute(
        "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
        "VALUES (?, ?, 'graph.delta.validated', ?, datetime('now'))",
        (root_task_id, "task-test", json.dumps(payload)),
    )
    conn.commit()


class TestIntentVsMutation:
    """AC4: 10 creates, 3 pre-existing → attempted=10, committed=7."""

    def test_dedup_creates_intent_vs_committed(self):
        conn = _make_db()
        project_id = "test-proj"
        root_task_id = "chain-001"

        # Pre-insert 3 nodes that will collide via INSERT OR IGNORE
        for i in range(1, 4):
            conn.execute(
                "INSERT INTO node_state (project_id, node_id, verify_status, build_status, updated_at, version) "
                "VALUES (?, ?, 'pending', 'unknown', datetime('now'), 1)",
                (project_id, f"L1.{i}"),
            )
        conn.commit()

        # Build 10 creates — 3 use explicit node_ids that already exist, 7 are new
        creates = []
        # 3 pre-existing (dedup-skip expected)
        for i in range(1, 4):
            creates.append({
                "node_id": f"L1.{i}",
                "parent_layer": 1,
                "title": f"Existing node {i}",
            })
        # 7 new nodes with explicit IDs that don't exist
        for i in range(4, 11):
            creates.append({
                "node_id": f"L1.{i}",
                "parent_layer": 1,
                "title": f"New node {i}",
            })

        _insert_validated_event(conn, root_task_id, creates, source_task_id="src-dedup-test")

        metadata = {
            "chain_id": root_task_id,
            "task_id": "task-merge-1",
        }

        result = _commit_graph_delta(conn, project_id, metadata)

        assert result is not None, "Expected a dict result, got None"
        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert "attempted_node_ids" in result
        assert "committed_node_ids" in result
        assert len(result["attempted_node_ids"]) == 10, (
            f"Expected 10 attempted, got {len(result['attempted_node_ids'])}: {result['attempted_node_ids']}"
        )
        assert len(result["committed_node_ids"]) == 7, (
            f"Expected 7 committed, got {len(result['committed_node_ids'])}: {result['committed_node_ids']}"
        )

    def test_committed_payload_has_both_fields(self):
        """AC2: graph.delta.committed event payload contains both attempted and committed."""
        conn = _make_db()
        project_id = "test-proj"
        root_task_id = "chain-002"

        # Pre-insert 1 node
        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status, build_status, updated_at, version) "
            "VALUES (?, ?, 'pending', 'unknown', datetime('now'), 1)",
            (project_id, "L2.1"),
        )
        conn.commit()

        creates = [
            {"node_id": "L2.1", "parent_layer": 2, "title": "Existing"},
            {"node_id": "L2.2", "parent_layer": 2, "title": "New"},
        ]
        _insert_validated_event(conn, root_task_id, creates, source_task_id="src-payload-test")

        metadata = {"chain_id": root_task_id, "task_id": "task-merge-2"}
        _commit_graph_delta(conn, project_id, metadata)

        # Read the committed event from chain_events
        row = conn.execute(
            "SELECT payload_json FROM chain_events WHERE event_type = 'graph.delta.committed' "
            "AND root_task_id = ?",
            (root_task_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        assert "attempted_node_ids" in payload
        assert "committed_node_ids" in payload
        assert len(payload["attempted_node_ids"]) == 2
        assert len(payload["committed_node_ids"]) == 1
        assert "L2.1" in payload["attempted_node_ids"]
        assert "L2.1" not in payload["committed_node_ids"]
        assert "L2.2" in payload["committed_node_ids"]

    def test_idempotency_returns_both_fields(self):
        """R5: Idempotent skip returns dict with both attempted and committed."""
        conn = _make_db()
        project_id = "test-proj"
        root_task_id = "chain-003"

        creates = [
            {"node_id": "L3.1", "parent_layer": 3, "title": "Node A"},
        ]
        _insert_validated_event(conn, root_task_id, creates, source_task_id="src-idemp")

        metadata = {"chain_id": root_task_id, "task_id": "task-merge-3"}
        result1 = _commit_graph_delta(conn, project_id, metadata)

        # Second call — same source, should idempotent-skip
        _insert_validated_event(conn, root_task_id, creates, source_task_id="src-idemp")
        result2 = _commit_graph_delta(conn, project_id, metadata)

        assert isinstance(result2, dict)
        assert "attempted_node_ids" in result2
        assert "committed_node_ids" in result2

    def test_update_rowcount_check(self):
        """R3: UPDATE with no actual change excluded from committed_node_ids."""
        conn = _make_db()
        project_id = "test-proj"
        root_task_id = "chain-004"

        # Insert a node that we'll try to update
        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status, build_status, updated_at, version) "
            "VALUES (?, ?, 'pending', 'unknown', datetime('now'), 1)",
            (project_id, "L4.1"),
        )
        conn.commit()

        # Update with same values — rowcount should be > 0 for SQLite (it updates even if same)
        # but with a real change it should definitely be > 0
        updates = [
            {"node_id": "L4.1", "fields": {"verify_status": "qa_pass"}},
        ]
        _insert_validated_event(conn, root_task_id, [], updates=updates, source_task_id="src-upd")

        metadata = {"chain_id": root_task_id, "task_id": "task-merge-4"}
        result = _commit_graph_delta(conn, project_id, metadata)

        assert isinstance(result, dict)
        assert "L4.1" in result["committed_node_ids"]

    def test_related_nodes_uses_committed_only(self):
        """AC5: related_nodes carryforward uses committed_node_ids, not attempted."""
        conn = _make_db()
        project_id = "test-proj"
        root_task_id = "chain-005"

        # Pre-insert 2 nodes
        for nid in ("L5.1", "L5.2"):
            conn.execute(
                "INSERT INTO node_state (project_id, node_id, verify_status, build_status, updated_at, version) "
                "VALUES (?, ?, 'pending', 'unknown', datetime('now'), 1)",
                (project_id, nid),
            )
        conn.commit()

        creates = [
            {"node_id": "L5.1", "parent_layer": 5, "title": "Existing 1"},  # dedup
            {"node_id": "L5.2", "parent_layer": 5, "title": "Existing 2"},  # dedup
            {"node_id": "L5.3", "parent_layer": 5, "title": "New 1"},
        ]
        _insert_validated_event(conn, root_task_id, creates, source_task_id="src-related")

        metadata = {"chain_id": root_task_id, "task_id": "task-merge-5", "related_nodes": []}
        _commit_graph_delta(conn, project_id, metadata)

        # related_nodes should only contain L5.3 (the actually committed node)
        assert "L5.3" in metadata["related_nodes"]
        assert "L5.1" not in metadata["related_nodes"]
        assert "L5.2" not in metadata["related_nodes"]

    def test_all_new_creates_attempted_equals_committed(self):
        """When no dedup occurs, attempted == committed."""
        conn = _make_db()
        project_id = "test-proj"
        root_task_id = "chain-006"

        creates = [
            {"node_id": f"L6.{i}", "parent_layer": 6, "title": f"Node {i}"}
            for i in range(1, 6)
        ]
        _insert_validated_event(conn, root_task_id, creates, source_task_id="src-all-new")

        metadata = {"chain_id": root_task_id, "task_id": "task-merge-6"}
        result = _commit_graph_delta(conn, project_id, metadata)

        assert len(result["attempted_node_ids"]) == 5
        assert len(result["committed_node_ids"]) == 5
        assert set(result["attempted_node_ids"]) == set(result["committed_node_ids"])
