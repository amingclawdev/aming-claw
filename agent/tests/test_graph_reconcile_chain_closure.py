"""Regression tests for graph event closure across reconcile chain stages."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.db import SCHEMA_SQL  # noqa: E402


PROJECT_ID = "graph-reconcile-closure"


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    for ddl in (
        "ALTER TABLE tasks ADD COLUMN trace_id TEXT",
        "ALTER TABLE tasks ADD COLUMN chain_id TEXT",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
    return conn


def _insert_event(conn, root_task_id, task_id, event_type, payload):
    conn.execute(
        "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (root_task_id, task_id, event_type, json.dumps(payload, ensure_ascii=False)),
    )


def _node(primary="agent/governance/example_feature.py"):
    return {
        "title": "Example feature",
        "parent_layer": "L7",
        "primary": [primary],
        "secondary": [],
        "test": ["agent/tests/test_example_feature.py"],
        "deps": [],
    }


class GraphReconcileChainClosureTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_db()
        self.root = "task-root-closure"

    def tearDown(self):
        self.conn.close()

    def test_resolves_chain_root_from_task_row_after_metadata_loss(self):
        from governance import auto_chain

        self.conn.execute(
            "INSERT INTO tasks (task_id, project_id, status, type, created_at, updated_at, "
            "metadata_json, parent_task_id, trace_id, chain_id) "
            "VALUES (?, ?, 'succeeded', 'qa', datetime('now'), datetime('now'), ?, ?, ?, ?)",
            (
                "task-qa-lost-meta",
                PROJECT_ID,
                json.dumps({"parent_task_id": "task-test-parent"}),
                "task-test-parent",
                "trace-1",
                self.root,
            ),
        )
        proposed = {"source_task_id": "task-dev-1", "graph_delta": {"creates": [_node()]}}
        _insert_event(self.conn, self.root, "task-dev-1", "graph.delta.proposed", proposed)

        payload = auto_chain._query_graph_delta_proposed(
            {"task_id": "task-qa-lost-meta", "project_id": PROJECT_ID},
            conn=self.conn,
            project_id=PROJECT_ID,
            task_id="task-qa-lost-meta",
        )

        self.assertEqual(payload["source_task_id"], "task-dev-1")

    def test_reconcile_qa_still_writes_validated_when_reconcile_run_bypasses_old_graph_gates(self):
        from governance import auto_chain

        proposed = {
            "source_task_id": "task-dev-1",
            "source": "dev-emitted",
            "graph_delta": {"creates": [_node()]},
        }
        _insert_event(self.conn, self.root, "task-dev-1", "graph.delta.proposed", proposed)
        metadata = {
            "project_id": PROJECT_ID,
            "chain_id": self.root,
            "task_id": "task-qa-1",
            "parent_task_id": "task-test-1",
            "operation_type": "reconcile-cluster",
            "reconcile_run_id": "run-closure",
            "related_nodes": ["L7.1"],
        }
        result = {
            "recommendation": "qa_pass",
            "graph_delta_review": {"decision": "pass", "issues": [], "suggested_diff": {}},
            "review_summary": "graph delta ok",
        }

        with patch.object(auto_chain, "_write_chain_memory"), \
             patch.object(auto_chain, "_validate_dev_at_transition", return_value=True):
            passed, reason = auto_chain._gate_qa_pass(self.conn, PROJECT_ID, result, metadata)

        self.assertTrue(passed, reason)
        row = self.conn.execute(
            "SELECT payload_json FROM chain_events "
            "WHERE root_task_id = ? AND event_type = 'graph.delta.validated'",
            (self.root,),
        ).fetchone()
        self.assertIsNotNone(row)
        payload = json.loads(row["payload_json"])
        self.assertEqual(payload["proposed_payload"]["source_task_id"], "task-dev-1")

    def test_gatekeeper_reconcile_cluster_writes_overlay_without_mutating_graph_json(self):
        from governance import auto_chain

        node = _node("agent/governance/overlay_feature.py")
        proposed = {
            "source_task_id": "task-dev-1",
            "source": "dev-emitted",
            "graph_delta": {"creates": [node], "updates": [], "links": []},
        }
        validated = {
            "source_task_id": "task-qa-1",
            "graph_delta_review": {"decision": "pass", "issues": []},
            "proposed_payload": proposed,
        }
        _insert_event(self.conn, self.root, "task-pm-1", "pm.prd.published", {"proposed_nodes": [node]})
        _insert_event(self.conn, self.root, "task-dev-1", "graph.delta.proposed", proposed)
        _insert_event(self.conn, self.root, "task-qa-1", "graph.delta.validated", validated)

        with tempfile.TemporaryDirectory() as td:
            graph_path = Path(td) / "graph.json"
            overlay_path = Path(td) / "graph.rebase.overlay.json"
            graph_path.write_text(json.dumps({}, indent=2), encoding="utf-8")
            before = graph_path.read_bytes()
            metadata = {
                "project_id": PROJECT_ID,
                "chain_id": self.root,
                "task_id": "task-gatekeeper-1",
                "operation_type": "reconcile-cluster",
                "session_id": "sess-closure",
                "cluster_fingerprint": "cluster-closure",
                "graph_path": str(graph_path),
                "overlay_path": str(overlay_path),
            }

            with patch.object(auto_chain, "_publish_event"):
                passed, reason = auto_chain._gate_gatekeeper_pass(
                    self.conn,
                    PROJECT_ID,
                    {"recommendation": "merge_pass", "review_summary": "ok"},
                    metadata,
                )

            self.assertTrue(passed, reason)
            self.assertEqual(before, graph_path.read_bytes())
            overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
            self.assertEqual(overlay["session_id"], "sess-closure")
            self.assertEqual(len(overlay["nodes"]), 1)
            applied = self.conn.execute(
                "SELECT payload_json FROM chain_events WHERE root_task_id = ? "
                "AND event_type = 'graph.delta.applied'",
                (self.root,),
            ).fetchone()
            self.assertIsNotNone(applied)


if __name__ == "__main__":
    unittest.main()
