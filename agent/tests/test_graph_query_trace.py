from __future__ import annotations

import json
import sqlite3

import pytest

from agent.governance import graph_query_trace
from agent.governance import graph_snapshot_store as store
from agent.governance.db import _ensure_schema


PID = "graph-query-trace-test"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    store.ensure_schema(c)
    graph_query_trace.ensure_schema(c)
    yield c
    c.close()


def _seed_snapshot(conn, tmp_path):
    project_root = tmp_path / "workspace"
    (project_root / "agent" / "governance").mkdir(parents=True)
    (project_root / "agent" / "tests").mkdir(parents=True)
    (project_root / "docs").mkdir(parents=True)
    (project_root / "agent" / "governance" / "server.py").write_text("def serve():\n    return 'ok'\n", encoding="utf-8")
    (project_root / "agent" / "tests" / "test_server.py").write_text("def test_serve():\n    assert True\n", encoding="utf-8")
    (project_root / "docs" / "architecture.md").write_text(
        "Batch job substrate connects reconcile, scope reconcile, and chain branch execution.\n",
        encoding="utf-8",
    )
    graph = {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L3.1",
                    "layer": "L3",
                    "title": "Runtime",
                    "kind": "subsystem",
                    "metadata": {"kind": "subsystem"},
                },
                {
                    "id": "L7.1",
                    "layer": "L7",
                    "title": "Governance Server",
                    "kind": "service_runtime",
                    "primary": ["agent/governance/server.py"],
                    "secondary": ["docs/architecture.md"],
                    "test": ["agent/tests/test_server.py"],
                    "metadata": {
                        "hierarchy_parent": "L3.1",
                        "function_count": 3,
                        "config_files": [".aming-claw.yaml"],
                    },
                },
                {
                    "id": "L7.2",
                    "layer": "L7",
                    "title": "Untested Helper",
                    "kind": "implementation",
                    "primary": ["agent/governance/helper.py"],
                    "metadata": {
                        "hierarchy_parent": "L3.1",
                        "function_count": 55,
                    },
                },
            ],
            "edges": [
                {"source": "L3.1", "target": "L7.1", "type": "contains"},
                {"source": "L7.1", "target": "L7.2", "type": "depends_on"},
            ],
        }
    }
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-query-test",
        commit_sha="abc1234",
        snapshot_kind="full",
        graph_json=graph,
        file_inventory=[
            {"path": "docs/architecture.md", "file_kind": "doc", "graph_status": "attached"},
            {"path": "agent/governance/server.py", "file_kind": "source", "graph_status": "mapped"},
        ],
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=store.graph_payload_edges(graph),
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()
    return snapshot["snapshot_id"], project_root


def test_trace_records_queries_and_budget_usage(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)
    trace = graph_query_trace.start_trace(
        conn,
        PID,
        snapshot_id,
        actor="ai-reviewer",
        query_source="ai_global_review",
        query_purpose="global_architecture_review",
        run_id="global-review-001",
        budget={"max_queries": 5},
    )["trace"]

    result = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        trace_id=trace["trace_id"],
        tool="get_node",
        args={"node_id": "L7.1", "include_feedback": True},
        project_root=project_root,
    )

    assert result["ok"] is True
    assert result["result"]["node"]["title"] == "Governance Server"
    assert result["args_hash"].startswith("sha256:")
    assert result["result_hash"].startswith("sha256:")

    stored = graph_query_trace.get_trace(conn, PID, trace["trace_id"])["trace"]
    assert stored["usage"]["query_count"] == 1
    assert stored["event_count"] == 1
    assert stored["events"][0]["tool"] == "get_node"
    assert stored["artifact_path"]


def test_query_tools_reuse_graph_files_and_search_docs(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)
    result = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="dashboard",
        query_source="dashboard",
        query_purpose="inspect_node",
        tool="search_docs",
        args={"query": "Batch job substrate", "limit": 5},
        project_root=project_root,
    )

    assert result["ok"] is True
    assert result["result"]["match_count"] == 1
    assert result["result"]["matches"][0]["path"] == "docs/architecture.md"

    trace = graph_query_trace.get_trace(conn, PID, result["trace_id"])["trace"]
    assert trace["query_source"] == "dashboard"
    assert trace["query_purpose"] == "inspect_node"
    assert trace["usage"]["file_excerpt_chars"] > 0


def test_budget_blocks_queries_after_limit(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)
    trace = graph_query_trace.start_trace(
        conn,
        PID,
        snapshot_id,
        actor="gate",
        query_source="chain_graph_gate",
        query_purpose="gate_validation",
        budget={"max_queries": 1},
    )["trace"]

    first = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        trace_id=trace["trace_id"],
        tool="list_layers",
        project_root=project_root,
    )
    second = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        trace_id=trace["trace_id"],
        tool="list_layers",
        project_root=project_root,
    )

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["error"] == "query_budget_exceeded"
    assert second["budget_key"] == "max_queries"
    stored = graph_query_trace.get_trace(conn, PID, trace["trace_id"])["trace"]
    assert stored["status"] == "budget_exceeded"


def test_low_health_query_uses_structural_and_feedback_signals(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)
    result = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="health_score",
        tool="list_low_health_nodes",
        args={"limit": 10},
        project_root=project_root,
    )

    assert result["ok"] is True
    low = {item["node"]["node_id"]: item for item in result["result"]["nodes"]}
    assert "L7.2" in low
    assert "missing_test_binding" in low["L7.2"]["issues"]
    assert "high_function_count" in low["L7.2"]["issues"]
