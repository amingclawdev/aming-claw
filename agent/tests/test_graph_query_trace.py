from __future__ import annotations

import json
import sqlite3

import pytest

from agent.governance import graph_events
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
    graph_events.ensure_schema(c)
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
                        "functions": [
                            "agent.governance.server::serve",
                            "agent.governance.server::Server.start",
                        ],
                        "function_lines": {
                            "serve": [1, 2],
                            "Server.start": [10, 12],
                        },
                        "function_calls": [
                            {
                                "caller": "agent.governance.server::serve",
                                "caller_short": "serve",
                                "caller_module": "agent.governance.server",
                                "caller_file": "agent/governance/server.py",
                                "caller_line": [1, 2],
                                "callee": "agent.governance.helper::helper",
                                "callee_short": "helper",
                                "callee_module": "agent.governance.helper",
                                "callee_file": "agent/governance/helper.py",
                                "callee_line": [1, 2],
                                "confidence": "strong",
                                "resolution": "resolved",
                            }
                        ],
                        "function_call_count": 1,
                        "function_called_by_count": 0,
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
                        "module": "agent.governance.helper",
                        "functions": ["agent.governance.helper::helper"],
                        "function_lines": {"helper": [1, 2]},
                        "function_called_by": [
                            {
                                "caller": "agent.governance.server::serve",
                                "caller_short": "serve",
                                "caller_module": "agent.governance.server",
                                "caller_file": "agent/governance/server.py",
                                "caller_line": [1, 2],
                                "callee": "agent.governance.helper::helper",
                                "callee_short": "helper",
                                "callee_module": "agent.governance.helper",
                                "callee_file": "agent/governance/helper.py",
                                "callee_line": [1, 2],
                                "confidence": "strong",
                                "resolution": "resolved",
                            }
                        ],
                        "function_call_count": 0,
                        "function_called_by_count": 1,
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


def _seed_edge_projection(conn, snapshot_id):
    projection = {
        "node_semantics": {},
        "edge_semantics": {
            "L7.1->L7.2:depends_on": {
                "edge_id": "L7.1->L7.2:depends_on",
                "edge": {
                    "src": "L7.1",
                    "dst": "L7.2",
                    "type": "depends_on",
                    "edge_type": "depends_on",
                },
                "semantic": {
                    "semantic_label": "server_helper_cache_dependency",
                    "relation_purpose": "Governance server dispatch invokes the helper cache.",
                    "risk": {"level": "medium", "reason": "helper cache is a hidden dependency"},
                },
                "validity": {"status": "edge_semantic_current", "valid": True},
                "source_event": {"event_id": "ge-edge-1", "event_type": "edge_semantic_enriched"},
            }
        },
    }
    conn.execute(
        """
        INSERT INTO graph_semantic_projections
          (project_id, snapshot_id, projection_id, base_commit, branch_ref,
           projection_rule_version, event_watermark, status, projection_json,
           health_json, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            PID,
            snapshot_id,
            "semproj-test",
            "abc1234",
            "main",
            "test",
            1,
            "current",
            json.dumps(projection),
            json.dumps({"edge_semantic_current_count": 1}),
            "test",
            "2026-05-13T00:00:00Z",
            "2026-05-13T00:00:00Z",
        ),
    )
    conn.commit()


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


def test_graph_native_discovery_queries_cover_paths_functions_and_degrees(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)

    path_result = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="find_node_by_path",
        args={"path": "agent/governance/server.py"},
        project_root=project_root,
    )
    assert path_result["ok"] is True
    assert path_result["result"]["matches"][0]["node"]["node_id"] == "L7.1"
    assert path_result["result"]["matches"][0]["matched_files"][0]["role"] == "primary"

    structure = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="search_structure",
        args={"query": "Server.start"},
        project_root=project_root,
    )
    assert structure["result"]["matches"][0]["node"]["node_id"] == "L7.1"

    functions = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="function_index",
        args={"query": "serve"},
        project_root=project_root,
    )
    assert functions["result"]["matches"][0]["short_name"] == "serve"
    assert functions["result"]["matches"][0]["line_start"] == 1

    callees = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="function_callees",
        args={"query": "serve"},
        project_root=project_root,
    )
    assert callees["result"]["matches"][0]["callee_short"] == "helper"
    assert callees["result"]["matches"][0]["callee_node"]["node_id"] == "L7.2"

    callers = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="function_callers",
        args={"query": "helper"},
        project_root=project_root,
    )
    assert callers["result"]["matches"][0]["caller_short"] == "serve"

    high_fn = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="high_function_degree",
        args={"metric": "fan_out"},
        project_root=project_root,
    )
    assert high_fn["result"]["functions"][0]["short_name"] == "serve"
    assert high_fn["result"]["functions"][0]["fan_out"] == 1

    degree = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="degree_summary",
        args={"node_id": "L7.1"},
        project_root=project_root,
    )
    assert degree["result"]["fan_in"] == 1
    assert degree["result"]["fan_out"] == 1
    assert degree["result"]["by_type"]["depends_on"]["out"] == 1

    high_degree = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="high_degree_nodes",
        args={"metric": "fan_out", "edge_types": ["depends_on"]},
        project_root=project_root,
    )
    assert high_degree["result"]["nodes"][0]["node"]["node_id"] == "L7.1"


def test_graph_native_queries_search_edge_projection_and_neighbor_semantics(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)
    _seed_edge_projection(conn, snapshot_id)

    semantic = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="search_semantic",
        args={"query": "helper cache", "scope": "edges"},
        project_root=project_root,
    )
    assert semantic["result"]["matches"][0]["result_type"] == "edge"
    assert semantic["result"]["matches"][0]["edge_id"] == "L7.1->L7.2:depends_on"
    assert semantic["result"]["matches"][0]["validity"]["status"] == "edge_semantic_current"

    neighbors = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="get_neighbors",
        args={"node_id": "L7.1", "direction": "out", "include_edge_semantic": True},
        project_root=project_root,
    )
    edge = neighbors["result"]["edges"][0]
    assert edge["edge_semantic"]["semantic"]["semantic_label"] == "server_helper_cache_dependency"


def test_query_schema_exposes_tools_and_enums(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)
    result = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="query_schema",
        project_root=project_root,
    )

    assert result["ok"] is True
    assert "find_node_by_path" in result["result"]["tool_names"]
    assert "observer" in result["result"]["query_sources"]
    assert "prompt_context_build" in result["result"]["query_purposes"]


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
