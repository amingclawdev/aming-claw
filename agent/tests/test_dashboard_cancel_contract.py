from __future__ import annotations

import sqlite3

import pytest

from agent.governance import graph_events
from agent.governance import graph_snapshot_store as store
from agent.governance import reconcile_feedback
from agent.governance import reconcile_semantic_enrichment as semantic_enrichment
from agent.governance import server
from agent.governance.db import _ensure_schema


PID = "dashboard-cancel-contract-test"


class _NoCloseConn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


def _ctx(path_params: dict, *, method: str = "POST", body: dict | None = None):
    return server.RequestContext(
        None,
        method,
        path_params,
        {},
        body or {},
        "req-dashboard-cancel-contract-test",
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
    semantic_enrichment._ensure_semantic_state_schema(c)
    monkeypatch.setattr(server, "get_connection", lambda _project_id: _NoCloseConn(c))
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    yield c
    c.close()


def _node(node_id: str) -> dict:
    return {
        "id": node_id,
        "layer": "L7",
        "title": f"Feature {node_id}",
        "kind": "service_runtime",
        "primary": [f"agent/governance/{node_id.replace('.', '_')}.py"],
        "secondary": [],
        "test": [],
        "metadata": {"subsystem": "governance"},
    }


def _create_snapshot(conn: sqlite3.Connection, snapshot_id: str = "cancel-contract") -> dict:
    nodes = [_node("L7.1"), _node("L7.2")]
    edges = [{
        "source": "L7.1",
        "target": "L7.2",
        "edge_type": "depends_on",
        "direction": "dependency",
        "evidence": {"source": "test"},
    }]
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
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()
    return snapshot


def _edge_request(conn: sqlite3.Connection, snapshot_id: str, edge_id: str = "L7.1->L7.2:depends_on") -> dict:
    event = graph_events.create_event(
        conn,
        PID,
        snapshot_id,
        event_type="edge_semantic_requested",
        event_kind="semantic_job",
        target_type="edge",
        target_id=edge_id,
        status="observed",
        payload={
            "edge": {"source": "L7.1", "target": "L7.2", "edge_type": "depends_on"},
            "edge_context": {"edge_id": edge_id},
            "semantic_payload": {},
        },
        evidence={"source": "test"},
        created_by="dashboard_e2e",
    )
    conn.commit()
    return event


def test_edge_cancel_accepts_pipe_separated_edge_id(conn):
    snapshot = _create_snapshot(conn, "pipe-edge-cancel")
    _edge_request(conn, snapshot["snapshot_id"])

    cancelled = server.handle_graph_governance_snapshot_semantic_job_cancel(
        _ctx(
            {
                "project_id": PID,
                "snapshot_id": snapshot["snapshot_id"],
                "job_id": "L7.1|L7.2|depends_on",
            },
            body={"actor": "dashboard_e2e"},
        )
    )

    assert cancelled["ok"] is True
    assert cancelled["job"]["edge_id"] == "L7.1->L7.2:depends_on"
    assert cancelled["job"]["status"] == "rejected"


def test_cancel_all_cancels_node_and_edge_pending_jobs(conn):
    snapshot = _create_snapshot(conn, "cancel-all")
    conn.executemany(
        """
        INSERT INTO graph_semantic_jobs
          (project_id, snapshot_id, node_id, status, updated_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (PID, snapshot["snapshot_id"], "L7.1", "ai_pending", "2026-05-10T18:00:00Z", "2026-05-10T18:00:00Z"),
            (PID, snapshot["snapshot_id"], "L7.2", "ai_complete", "2026-05-10T18:00:00Z", "2026-05-10T18:00:00Z"),
        ],
    )
    _edge_request(conn, snapshot["snapshot_id"])

    result = server.handle_graph_governance_snapshot_semantic_jobs_cancel_all(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            body={"actor": "dashboard_e2e"},
        )
    )

    assert result["ok"] is True
    assert result["cancelled_count"] == 2
    assert result["skipped_terminal"] == 1
    rows = conn.execute(
        """
        SELECT node_id, status
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ?
        ORDER BY node_id
        """,
        (PID, snapshot["snapshot_id"]),
    ).fetchall()
    assert [(row["node_id"], row["status"]) for row in rows] == [
        ("L7.1", "cancelled"),
        ("L7.2", "ai_complete"),
    ]
    assert server._edge_semantic_job_row(
        conn,
        PID,
        snapshot["snapshot_id"],
        "L7.1->L7.2:depends_on",
    )["status"] == "rejected"


def test_semantic_create_returns_queued_ops_and_session_cancel(conn, tmp_path):
    snapshot = _create_snapshot(conn, "session-cancel")

    status, payload = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            body={
                "project_root": str(tmp_path),
                "job_type": "semantic_enrichment",
                "target_scope": "node",
                "target_ids": ["L7.1"],
                "options": {"scope": "selected_node", "mode": "retry", "skip_current": True},
                "created_by": "dashboard_e2e",
            },
        )
    )

    assert status == 202
    assert payload["queued_count"] == 1
    assert payload["queued_ops"] == [{
        "operation_id": "node-semantic:L7.1",
        "operation_type": "node_semantic",
        "target_scope": "node",
        "target_id": "L7.1",
        "job_id": "L7.1",
    }]

    cancelled = server.handle_graph_governance_snapshot_semantic_job_cancel(
        _ctx(
            {
                "project_id": PID,
                "snapshot_id": snapshot["snapshot_id"],
                "job_id": payload["job_id"],
            },
            body={"actor": "dashboard_e2e"},
        )
    )

    assert cancelled["ok"] is True
    assert cancelled["cancelled_count"] == 1
    row = conn.execute(
        """
        SELECT status FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ? AND node_id = 'L7.1'
        """,
        (PID, snapshot["snapshot_id"]),
    ).fetchone()
    assert row["status"] == "cancelled"


def test_scope_reconcile_cancel_waives_pending_row(conn):
    _create_snapshot(conn, "scope-cancel")
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="next-head",
        parent_commit_sha="head",
        status=store.PENDING_STATUS_QUEUED,
        evidence={"source": "test"},
    )
    conn.commit()

    result = server.handle_graph_governance_scope_reconcile_cancel(
        _ctx(
            {"project_id": PID},
            body={"operation_id": "scope-reconcile:next-head", "actor": "dashboard_e2e"},
        )
    )

    assert result["ok"] is True
    assert result["status"] == "cancelled"
    assert result["cancelled_count"] == 1
    row = store.list_pending_scope_reconcile(conn, PID, commit_shas=["next-head"])[0]
    assert row["status"] == store.PENDING_STATUS_WAIVED


def test_feedback_cancel_uses_keep_status_observation_contract(conn):
    snapshot = _create_snapshot(conn, "feedback-cancel")
    submitted = reconcile_feedback.submit_feedback_item(
        PID,
        snapshot["snapshot_id"],
        feedback_kind=reconcile_feedback.KIND_PROJECT_IMPROVEMENT,
        issue={
            "issue": "Operator wants to withdraw this review candidate.",
            "source_node_ids": ["L7.1"],
            "priority": "P2",
        },
        actor="dashboard_e2e",
    )
    feedback_id = submitted["items"][0]["feedback_id"]

    result = server.handle_graph_governance_snapshot_feedback_cancel(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            body={"feedback_ids": [feedback_id], "actor": "dashboard_e2e"},
        )
    )

    assert result["ok"] is True
    assert result["cancelled_count"] == 1
    assert result["feedback_cancel_contract"] == "keep_status_observation"
    item = reconcile_feedback.list_feedback_items(PID, snapshot["snapshot_id"])[0]
    assert item["status"] == reconcile_feedback.STATUS_ACCEPTED
    assert item["final_feedback_kind"] == reconcile_feedback.KIND_STATUS_OBSERVATION
