from __future__ import annotations

import sqlite3

import pytest

from agent.governance import graph_snapshot_store as store
from agent.governance import reconcile_semantic_enrichment as semantic_enrichment
from agent.governance import server
from agent.governance.db import _ensure_schema


PID = "semantic-jobs-scope-test"


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
        "req-semantic-scope-test",
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


def _create_snapshot(conn: sqlite3.Connection, snapshot_id: str = "scope-semantic-jobs") -> dict:
    graph = {
        "deps_graph": {
            "nodes": [_node("L7.99"), _node("L7.104"), _node("L7.105")],
            "edges": [],
        }
    }
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id=snapshot_id,
        commit_sha="head",
        snapshot_kind="scope",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=[],
    )
    conn.commit()
    return snapshot


def _selected_node_retry_body(tmp_path, *, dry_run: bool) -> dict:
    return {
        "project_root": str(tmp_path),
        "job_type": "semantic_enrichment",
        "target_scope": "node",
        "target_ids": ["L7.104"],
        "options": {
            "target": "nodes",
            "include_nodes": True,
            "include_edges": False,
            "scope": "selected_node",
            "mode": "retry",
            "dry_run": dry_run,
            "skip_current": True,
            "retry_stale_failed": True,
            "include_package_markers": False,
        },
        "created_by": "dashboard_e2e",
    }


def test_selected_node_retry_queues_only_requested_target(conn, tmp_path):
    snapshot = _create_snapshot(conn)

    status, payload = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            body=_selected_node_retry_body(tmp_path, dry_run=False),
        )
    )

    assert status == 202
    assert payload["queued_count"] == 1
    assert payload["planned_count"] == 1
    assert payload["batch_plan"]["target_ids"] == ["L7.104"]
    assert payload["semantic_enrichment"]["semantic_selector"]["node_ids"] == ["L7.104"]

    rows = conn.execute(
        """
        SELECT node_id, status
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ?
        ORDER BY node_id
        """,
        (PID, snapshot["snapshot_id"]),
    ).fetchall()
    assert [(row["node_id"], row["status"]) for row in rows] == [("L7.104", "ai_pending")]


def test_selected_node_retry_queued_count_ignores_existing_open_jobs(conn, tmp_path):
    snapshot = _create_snapshot(conn, "scope-semantic-jobs-existing")
    conn.execute(
        """
        INSERT INTO graph_semantic_jobs
          (project_id, snapshot_id, node_id, status, updated_at, created_at)
        VALUES (?, ?, 'L7.99', 'ai_pending', '2026-05-10T18:00:00Z', '2026-05-10T18:00:00Z')
        """,
        (PID, snapshot["snapshot_id"]),
    )
    conn.commit()

    status, payload = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            body=_selected_node_retry_body(tmp_path, dry_run=False),
        )
    )

    assert status == 202
    assert payload["queued_count"] == 1
    assert [job["node_id"] for job in payload["jobs"]] == ["L7.104"]

    rows = conn.execute(
        """
        SELECT node_id
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ? AND status = 'ai_pending'
        ORDER BY node_id
        """,
        (PID, snapshot["snapshot_id"]),
    ).fetchall()
    assert [row["node_id"] for row in rows] == ["L7.104", "L7.99"]


def test_selected_node_retry_dry_run_does_not_persist_jobs(conn, tmp_path):
    snapshot = _create_snapshot(conn, "scope-semantic-jobs-dry-run")

    status, payload = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            body=_selected_node_retry_body(tmp_path, dry_run=True),
        )
    )

    assert status == 202
    assert payload["status"] == "dry_run"
    assert payload["dry_run"] is True
    assert payload["queued_count"] == 0
    assert payload["planned_count"] == 1
    assert payload["semantic_enrichment"]["semantic_selector"]["node_ids"] == ["L7.104"]

    count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ?
        """,
        (PID, snapshot["snapshot_id"]),
    ).fetchone()["count"]
    assert count == 0
