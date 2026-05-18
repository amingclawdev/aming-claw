from __future__ import annotations

import sqlite3

import pytest

from agent.governance import graph_snapshot_store as store
from agent.governance import server
from agent.governance.db import _ensure_schema
from agent.governance.graph_structure_ops import SCHEMA_VERSION


PID = "graph-structure-ops-api-test"


class _NoCloseConn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


def _ctx(snapshot_id: str, body: dict):
    return server.RequestContext(
        None,
        "POST",
        {"project_id": PID, "snapshot_id": snapshot_id},
        {},
        body,
        "req-graph-structure-ops-api-test",
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
    monkeypatch.setattr(server, "get_connection", lambda _project_id: _NoCloseConn(c))
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    yield c
    c.close()


def _graph() -> dict:
    return {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.1",
                    "layer": "L7",
                    "title": "Runtime",
                    "primary": ["agent/governance/server.py"],
                    "test": [],
                    "metadata": {},
                },
                {
                    "id": "L7.2",
                    "layer": "L7",
                    "title": "Ops",
                    "primary": ["agent/governance/graph_structure_ops.py"],
                    "test": [],
                    "metadata": {},
                },
            ],
            "edges": [],
        }
    }


def _create_snapshot(conn: sqlite3.Connection) -> str:
    graph = _graph()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-ops-api-test",
        commit_sha="abc1234",
        snapshot_kind="scope",
        graph_json=graph,
        file_inventory=[
            {"path": "agent/governance/server.py", "file_kind": "source"},
            {"path": "agent/governance/graph_structure_ops.py", "file_kind": "source"},
        ],
        status=store.SNAPSHOT_STATUS_ACTIVE,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=[],
    )
    conn.commit()
    return snapshot["snapshot_id"]


def _payload(snapshot_id: str, *, source_path: str = "agent/governance/server.py") -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "snapshot_id": snapshot_id,
            "base_commit": "abc1234",
            "analyzer_role": "reconcile_graph_structure_analyzer",
        },
        "operations": [
            {
                "op": "add_edge",
                "hint_id": "ai.edge.runtime-to-ops",
                "source_path": source_path,
                "target_node_id": "L7.2",
                "edge": "depends_on",
                "confidence": 0.82,
                "evidence": {"reason": "runtime imports graph ops gate"},
            }
        ],
        "self_check": {
            "valid": True,
            "checked_rules": ["hint-compatible-op", "snapshot-match"],
            "known_risks": [],
        },
    }


def test_graph_structure_ops_dry_run_returns_projection_preview(conn):
    snapshot_id = _create_snapshot(conn)

    status, result = server.handle_graph_governance_snapshot_graph_structure_ops_dry_run(
        _ctx(snapshot_id, {"payload": _payload(snapshot_id)})
    )

    assert status == 200
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["mutated"] is False
    assert result["gate"]["accepted_count"] == 1
    assert result["projection"]["status"] == "ok"
    assert result["projection"]["materialized_count"] == 1
    assert result["projection"]["effect_counts"]["edges_added"] == 1


def test_graph_structure_ops_dry_run_rejects_invalid_payload_without_projection(conn):
    snapshot_id = _create_snapshot(conn)

    status, result = server.handle_graph_governance_snapshot_graph_structure_ops_dry_run(
        _ctx(snapshot_id, {"payload": _payload(snapshot_id, source_path="missing.py")})
    )

    assert status == 422
    assert result["ok"] is False
    assert result["dry_run"] is True
    assert result["mutated"] is False
    assert result["gate"]["rejected_count"] == 1
    assert result["gate"]["operations"][0]["errors"] == ["source_path_missing"]
    assert result["projection"]["status"] == "not_run"
