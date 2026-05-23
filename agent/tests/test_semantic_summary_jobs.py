from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent.governance import ai_output_intake
from agent.governance import graph_events
from agent.governance import graph_snapshot_store as store
from agent.governance import reconcile_semantic_enrichment as semantic
from agent.governance import reconcile_semantic_summary as summary
from agent.governance import semantic_worker
from agent.governance import server
from agent.governance.db import _ensure_schema
from agent.governance.errors import ValidationError


PID = "semantic-summary-test"


class _NoCloseConn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


def _ctx(path_params: dict, *, body: dict):
    return server.RequestContext(
        None,
        "POST",
        path_params,
        {},
        body,
        "req-semantic-summary-test",
        "",
        "",
    )


def _get_ctx(path_params: dict, *, query: dict | None = None):
    return server.RequestContext(
        None,
        "GET",
        path_params,
        query or {},
        {},
        "req-semantic-summary-test",
        "",
        "",
    )


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    store.ensure_schema(c)
    graph_events.ensure_schema(c)
    semantic._ensure_semantic_state_schema(c)
    ai_output_intake.ensure_schema(c)
    monkeypatch.setattr(server, "get_connection", lambda _project_id: _NoCloseConn(c))
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    monkeypatch.setattr(server, "_require_project_semantic_route_for_jobs", lambda _project_id: None)
    yield c
    c.close()


def _node(node_id: str, layer: str, *, parent: str = "") -> dict:
    metadata = {"subsystem": "summary-test"}
    if parent:
        metadata["hierarchy_parent"] = parent
    return {
        "id": node_id,
        "layer": layer,
        "title": f"{layer} {node_id}",
        "kind": "container" if layer in {"L1", "L2", "L3"} else "service_runtime",
        "primary": [f"src/{node_id.replace('.', '_')}.py"] if layer == "L7" else [],
        "secondary": [],
        "test": [],
        "metadata": metadata,
    }


def _create_snapshot(conn: sqlite3.Connection, snapshot_id: str = "scope-summary") -> dict:
    nodes = [
        _node("L1.1", "L1"),
        _node("L2.1", "L2", parent="L1.1"),
        _node("L3.1", "L3", parent="L2.1"),
        _node("L7.1", "L7", parent="L3.1"),
        _node("L7.2", "L7", parent="L3.1"),
    ]
    edges = [
        {"src": "L1.1", "dst": "L2.1", "edge_type": "contains", "direction": "hierarchy"},
        {"src": "L2.1", "dst": "L3.1", "edge_type": "contains", "direction": "hierarchy"},
        {"src": "L3.1", "dst": "L7.1", "edge_type": "contains", "direction": "hierarchy"},
        {"src": "L3.1", "dst": "L7.2", "edge_type": "contains", "direction": "hierarchy"},
    ]
    graph = {"deps_graph": {"nodes": nodes, "edges": edges}}
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id=snapshot_id,
        commit_sha="head",
        snapshot_kind="scope",
        graph_json=graph,
    )
    store.index_graph_snapshot(conn, PID, snapshot["snapshot_id"], nodes=nodes, edges=edges)
    conn.commit()
    return snapshot


def _summary_body(tmp_path, **overrides) -> dict:
    body = {
        "project_root": str(tmp_path),
        "job_type": "semantic_summary",
        "target_scope": "node",
        "target_ids": ["L2.1"],
        "options": {
            "target": "summary",
            "summary_source": "child_semantics",
            "require_current_children": True,
            "submit_for_review": True,
        },
        "created_by": "dashboard_user",
    }
    body.update(overrides)
    return body


def test_semantic_summary_queue_persists_ai_summary_operation(conn, tmp_path):
    snapshot = _create_snapshot(conn)

    status, payload = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            body=_summary_body(tmp_path),
        )
    )

    assert status == 202
    assert payload["job_type"] == "semantic_summary"
    assert payload["queued_count"] == 1
    assert payload["queued_ops"][0]["operation_type"] == "ai_summary"
    row = conn.execute(
        """
        SELECT node_id, status, operation_type
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ?
        """,
        (PID, snapshot["snapshot_id"]),
    ).fetchone()
    assert dict(row) == {"node_id": "L2.1", "status": "pending_ai", "operation_type": "ai_summary"}
    listed = server._semantic_job_rows(conn, PID, snapshot["snapshot_id"])
    assert listed[0]["operation_type"] == "ai_summary"


def test_operations_queue_labels_semantic_summary_jobs(conn, tmp_path):
    snapshot = _create_snapshot(conn, "scope-summary-ops")
    server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            body=_summary_body(tmp_path),
        )
    )

    payload = server.handle_graph_governance_operations_queue(
        _get_ctx(
            {"project_id": PID},
            query={"snapshot_id": snapshot["snapshot_id"]},
        )
    )

    op = next(item for item in payload["operations"] if item["target_id"] == "L2.1")
    assert op["operation_type"] == "ai_summary"
    assert op["operation_id"].startswith("ai-summary:")
    assert op["target_label"] == "L2.1 summary"


@pytest.mark.parametrize(
    ("target_scope", "target_ids", "message"),
    [
        ("edge", ["L2.1"], "only node or subtree"),
        ("snapshot", ["L2.1"], "only node or subtree"),
        ("node", [], "target_ids is required"),
        ("node", ["L7.1"], "only L1/L2/L3"),
    ],
)
def test_semantic_summary_queue_validation(conn, tmp_path, target_scope, target_ids, message):
    snapshot = _create_snapshot(conn, "scope-summary-validation")

    with pytest.raises(ValidationError, match=message):
        server.handle_graph_governance_snapshot_semantic_jobs_create(
            _ctx(
                {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
                body=_summary_body(tmp_path, target_scope=target_scope, target_ids=target_ids),
            )
        )


def test_summary_source_hash_is_deterministic_from_accepted_child_semantics(conn):
    snapshot = _create_snapshot(conn, "scope-summary-source")
    child_payload = {
        "node_id": "L7.1",
        "semantic_summary": "Child behavior.",
        "feature_name": "Child",
    }
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash, semantic_json,
           payload_hash, operation_type, updated_at)
        VALUES (?, ?, 'L7.1', 'ai_complete', 'feature-child', ?, 'payload-child',
                'ai_enrich', '2026-05-23T00:00:00Z')
        """,
        (PID, snapshot["snapshot_id"], json.dumps(child_payload, sort_keys=True)),
    )
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash, semantic_json,
           payload_hash, operation_type, updated_at)
        VALUES (?, ?, 'L7.2', 'pending_review', 'feature-pending', '{}', 'payload-pending',
                'ai_enrich', '2026-05-23T00:00:01Z')
        """,
        (PID, snapshot["snapshot_id"]),
    )

    first = summary.collect_summary_source(conn, PID, snapshot["snapshot_id"], "L2.1")
    second = summary.collect_summary_source(conn, PID, snapshot["snapshot_id"], "L2.1")

    assert first["summary_source_hash"] == second["summary_source_hash"]
    assert first["child_count"] == 1
    assert first["child_semantics"][0]["node_id"] == "L7.1"
    assert first["child_semantics"][0]["semantic"]["semantic_summary"] == "Child behavior."


def test_summary_source_requires_accepted_children(conn):
    snapshot = _create_snapshot(conn, "scope-summary-no-children")

    with pytest.raises(ValueError, match="requires accepted child semantics"):
        summary.collect_summary_source(conn, PID, snapshot["snapshot_id"], "L2.1")


def test_worker_routes_ai_summary_job_to_summary_payload(conn, monkeypatch, tmp_path):
    snapshot = _create_snapshot(conn, "scope-summary-worker")
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash, semantic_json,
           payload_hash, operation_type, updated_at)
        VALUES (?, ?, 'L7.1', 'ai_complete', 'feature-child', ?, 'payload-child',
                'ai_enrich', '2026-05-23T00:00:00Z')
        """,
        (
            PID,
            snapshot["snapshot_id"],
            json.dumps({"node_id": "L7.1", "semantic_summary": "Accepted child."}, sort_keys=True),
        ),
    )
    conn.execute(
        """
        INSERT INTO graph_semantic_jobs
          (project_id, snapshot_id, node_id, status, operation_type, worker_id,
           claim_id, claimed_at, lease_expires_at, claimed_by, updated_at, created_at)
        VALUES (?, ?, 'L2.1', 'running', 'ai_summary', 'semantic_worker_inproc',
                'claim-summary', '2026-05-23T00:00:00Z', '2026-05-23T00:10:00Z',
                'semantic_worker_inproc', '2026-05-23T00:00:00Z', '2026-05-23T00:00:00Z')
        """,
        (PID, snapshot["snapshot_id"]),
    )
    conn.commit()
    monkeypatch.setattr("agent.governance.db.get_connection", lambda _project_id: _NoCloseConn(conn))
    monkeypatch.setattr(
        "agent.governance.reconcile_feedback.submit_feedback_item",
        lambda *args, **kwargs: None,
    )

    calls = []

    def fake_ai_call(stage, payload):
        calls.append((stage, payload))
        return {
            "node_id": "L2.1",
            "feature_name": "L2 summary",
            "semantic_summary": "Summary of accepted child semantics.",
            "intent": "summarize child semantic memory",
            "domain_label": "governance",
            "self_check": {
                "valid": True,
                "status": "passed",
                "checked_rules": semantic.NODE_SEMANTIC_SELF_CHECK_RULES,
            },
            "graph_query_audit": {"trace_id": "gqt-summary", "status": "ok"},
        }

    result = semantic_worker._process_node_semantic_job(
        PID,
        snapshot["snapshot_id"],
        root=Path(tmp_path),
        ai_call=fake_ai_call,
        node_id="L2.1",
    )

    assert result["ok"] is True
    assert calls[0][0] == "summary"
    assert calls[0][1]["operation_type"] == "ai_summary"
    assert calls[0][1]["summary_source"]["child_count"] == 1
    row = conn.execute(
        """
        SELECT status, feature_hash, operation_type, semantic_json
        FROM graph_semantic_nodes
        WHERE project_id = ? AND snapshot_id = ? AND node_id = 'L2.1'
        """,
        (PID, snapshot["snapshot_id"]),
    ).fetchone()
    payload = json.loads(row["semantic_json"])
    assert row["status"] == "pending_review"
    assert row["operation_type"] == "ai_summary"
    assert row["feature_hash"] == result["summary_source"]["summary_source_hash"]
    assert payload["semantic_kind"] == "summary"
    assert payload["summary_source"]["child_count"] == 1
    job = conn.execute(
        """
        SELECT status, operation_type, last_error
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ? AND node_id = 'L2.1'
        """,
        (PID, snapshot["snapshot_id"]),
    ).fetchone()
    assert dict(job) == {"status": "ai_complete", "operation_type": "ai_summary", "last_error": ""}
    event = conn.execute(
        """
        SELECT status, operation_type
        FROM graph_events
        WHERE project_id = ? AND snapshot_id = ?
          AND event_type = 'semantic_node_enriched'
          AND target_id = 'L2.1'
        """,
        (PID, snapshot["snapshot_id"]),
    ).fetchone()
    assert dict(event) == {"status": "proposed", "operation_type": "ai_summary"}
