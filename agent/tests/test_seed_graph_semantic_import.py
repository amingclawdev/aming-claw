from __future__ import annotations

import json
import sqlite3

import pytest

from agent.governance import graph_events
from agent.governance import graph_snapshot_store as store
from agent.governance import reconcile_semantic_enrichment as semantic
from agent.governance import seed_graph_semantics
from agent.governance import server
from agent.governance.db import _ensure_schema


PID = "seed-semantic-test"


class _NoCloseConn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


def _ctx(path_params: dict, *, body: dict | None = None):
    return server.RequestContext(
        None,
        "POST",
        path_params,
        {},
        body or {},
        "req-test",
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
    yield c
    c.close()


@pytest.fixture()
def seed_path(tmp_path):
    path = tmp_path / "seed-graph-summary.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": PID,
                "last_curated_commit": "seed1234",
                "core_surfaces": [
                    {
                        "name": "dashboard",
                        "paths": [
                            "frontend/dashboard/src/App.tsx",
                            "frontend/dashboard/src/Missing.tsx",
                        ],
                        "notes": "Local UI for graph inspection.",
                    },
                    {
                        "name": "governance-api",
                        "paths": ["agent/governance/server.py"],
                        "notes": "REST API used by dashboard and MCP.",
                    },
                    {
                        "name": "packaging",
                        "paths": ["pyproject.toml"],
                        "notes": "Package metadata.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _create_snapshot(conn: sqlite3.Connection) -> str:
    graph = {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.1",
                    "layer": "L7",
                    "title": "frontend.dashboard.src.App",
                    "kind": "service_runtime",
                    "primary": ["frontend/dashboard/src/App.tsx"],
                    "secondary": ["docs/dashboard.md"],
                    "test": ["frontend/dashboard/scripts/e2e-trunk.mjs"],
                    "metadata": {"module": "frontend.dashboard.src.App"},
                },
                {
                    "id": "L7.2",
                    "layer": "L7",
                    "title": "agent.governance.server",
                    "kind": "service_runtime",
                    "primary": ["agent/governance/server.py"],
                    "secondary": ["docs/api/governance-api.md"],
                    "test": ["agent/tests/test_graph_governance_api.py"],
                    "metadata": {"module": "agent.governance.server"},
                },
                {
                    "id": "L7.3",
                    "layer": "L7",
                    "title": "agent.other",
                    "kind": "service_runtime",
                    "primary": ["agent/other.py"],
                    "secondary": [],
                    "test": [],
                    "metadata": {"module": "agent.other"},
                },
            ],
            "edges": [],
        }
    }
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="seed-snapshot",
        commit_sha="commit-seed",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=[],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"], auto_rebuild_projection=False)
    conn.commit()
    return snapshot["snapshot_id"]


def test_import_seed_graph_semantics_persists_rows_events_and_projection(conn, seed_path):
    snapshot_id = _create_snapshot(conn)

    result = seed_graph_semantics.import_seed_graph_semantics(
        conn,
        PID,
        snapshot_id,
        seed_path=seed_path,
        actor="test",
        projection_id="seed-projection",
    )
    conn.commit()

    assert result["imported_node_count"] == 2
    assert result["matched_surface_count"] == 2
    assert result["unmapped_surface_count"] == 1
    assert "pyproject.toml" in result["unmapped_paths"]

    semantic_rows = conn.execute("SELECT node_id, status FROM graph_semantic_nodes ORDER BY node_id").fetchall()
    assert [(row["node_id"], row["status"]) for row in semantic_rows] == [
        ("L7.1", "semantic_graph_state"),
        ("L7.2", "semantic_graph_state"),
    ]
    event_count = conn.execute(
        "SELECT COUNT(*) AS c FROM graph_events WHERE event_type='semantic_node_enriched'"
    ).fetchone()["c"]
    assert event_count == 2

    projection = graph_events.get_semantic_projection(conn, PID, snapshot_id, "seed-projection")
    assert projection is not None
    assert projection["health"]["semantic_current_count"] == 2
    assert projection["health"]["semantic_missing_count"] == 1
    assert (
        projection["projection"]["node_semantics"]["L7.1"]["semantic"]["domain_label"]
        == "seed_graph.dashboard"
    )


def test_import_seed_graph_semantics_is_idempotent(conn, seed_path):
    snapshot_id = _create_snapshot(conn)

    seed_graph_semantics.import_seed_graph_semantics(
        conn,
        PID,
        snapshot_id,
        seed_path=seed_path,
        actor="test",
        projection_id="seed-projection",
    )
    seed_graph_semantics.import_seed_graph_semantics(
        conn,
        PID,
        snapshot_id,
        seed_path=seed_path,
        actor="test",
        projection_id="seed-projection",
    )
    conn.commit()

    assert conn.execute("SELECT COUNT(*) AS c FROM graph_semantic_nodes").fetchone()["c"] == 2
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM graph_events WHERE event_type='semantic_node_enriched'"
    ).fetchone()["c"] == 2
    assert conn.execute("SELECT COUNT(*) AS c FROM graph_semantic_projections").fetchone()["c"] == 1


def test_seed_semantic_import_endpoint(conn, seed_path, monkeypatch):
    snapshot_id = _create_snapshot(conn)
    monkeypatch.setattr(server, "get_connection", lambda _project_id: _NoCloseConn(conn))

    result = server.handle_graph_governance_snapshot_seed_semantic_import(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot_id},
            body={"seed_path": str(seed_path), "projection_id": "seed-projection", "actor": "test"},
        )
    )

    assert result["ok"] is True
    assert result["imported_node_count"] == 2
    assert result["projection_id"] == "seed-projection"
