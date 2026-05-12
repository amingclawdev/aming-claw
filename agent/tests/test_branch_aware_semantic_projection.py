from __future__ import annotations

import sqlite3

import pytest

from agent.governance import graph_events
from agent.governance import graph_snapshot_store as store
from agent.governance import reconcile_semantic_enrichment as semantic
from agent.governance.db import _ensure_schema


PID = "branch-semantic-test"
MAIN_REF = "refs/heads/main"
FEATURE_REF = "refs/heads/frontend"


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


def _graph() -> dict:
    return {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.1",
                    "layer": "L7",
                    "title": "Source Feature",
                    "kind": "service_runtime",
                    "primary": ["agent/source.py"],
                    "secondary": ["docs/source.md"],
                    "test": ["agent/tests/test_source.py"],
                    "metadata": {"module": "agent.source"},
                },
                {
                    "id": "L7.2",
                    "layer": "L7",
                    "title": "Target Feature",
                    "kind": "service_runtime",
                    "primary": ["agent/target.py"],
                    "secondary": ["docs/target.md"],
                    "test": ["agent/tests/test_target.py"],
                    "metadata": {"module": "agent.target"},
                },
            ],
            "edges": [
                {
                    "source": "L7.1",
                    "target": "L7.2",
                    "edge_type": "depends_on",
                    "direction": "dependency",
                    "evidence": {"source": "test"},
                }
            ],
        }
    }


def _create_main_snapshot(conn):
    graph = _graph()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="branch-main-snapshot",
        commit_sha="commit-main",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        ref_name=MAIN_REF,
        auto_rebuild_projection=False,
    )
    conn.commit()
    return snapshot, graph


def test_semantic_timeline_columns_exist_for_node_and_edge_tables(conn):
    for table in ("graph_semantic_nodes", "graph_semantic_edges"):
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        assert {
            "branch_ref",
            "operation_type",
            "source_branch_ref",
            "source_snapshot_id",
            "source_event_id",
            "payload_hash",
        }.issubset(columns)


def test_active_ref_alias_is_not_treated_as_a_branch(conn):
    graph = _graph()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="active-only-snapshot",
        commit_sha="commit-active-only",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        auto_rebuild_projection=False,
    )
    projection = graph_events.build_semantic_projection(
        conn,
        PID,
        snapshot["snapshot_id"],
        projection_id="active-ref-projection",
        backfill_existing=False,
    )

    assert projection["branch_ref"] == ""
    assert projection["projection"]["branch_ref"] == ""


def test_node_projection_ignores_other_branch_until_main_event_is_accepted(conn):
    snapshot, graph = _create_main_snapshot(conn)
    node = graph["deps_graph"]["nodes"][0]
    feature_hash = graph_events.feature_hash_for_node(node)

    graph_events.create_event(
        conn,
        PID,
        snapshot["snapshot_id"],
        event_id="node-feature-branch",
        event_type="semantic_node_enriched",
        event_kind="semantic_job",
        target_type="node",
        target_id="L7.1",
        status=graph_events.EVENT_STATUS_OBSERVED,
        branch_ref=FEATURE_REF,
        operation_type="ai_enrich",
        stable_node_key=graph_events.stable_node_key_for_node(node),
        feature_hash=feature_hash,
        payload={"semantic_payload": {"feature_name": "Wrong branch"}},
        created_by="test",
    )
    graph_events.create_event(
        conn,
        PID,
        snapshot["snapshot_id"],
        event_id="node-main-proposed",
        event_type="semantic_node_enriched",
        event_kind="semantic_job",
        target_type="node",
        target_id="L7.1",
        status=graph_events.EVENT_STATUS_PROPOSED,
        branch_ref=MAIN_REF,
        operation_type="ai_enrich",
        stable_node_key=graph_events.stable_node_key_for_node(node),
        feature_hash=feature_hash,
        payload={"semantic_payload": {"feature_name": "Needs review"}},
        created_by="test",
    )
    conn.commit()

    projection = graph_events.build_semantic_projection(
        conn,
        PID,
        snapshot["snapshot_id"],
        projection_id="node-before-accept",
        backfill_existing=False,
    )
    node_semantic = projection["projection"]["node_semantics"]["L7.1"]
    assert node_semantic["validity"]["status"] == "semantic_missing"

    graph_events.update_event_status(
        conn,
        PID,
        snapshot["snapshot_id"],
        "node-main-proposed",
        status=graph_events.EVENT_STATUS_ACCEPTED,
        actor="test",
        operation_type="accept",
    )
    projection = graph_events.build_semantic_projection(
        conn,
        PID,
        snapshot["snapshot_id"],
        projection_id="node-after-accept",
        backfill_existing=False,
    )
    node_semantic = projection["projection"]["node_semantics"]["L7.1"]
    assert node_semantic["validity"]["status"] == "semantic_current"
    assert node_semantic["semantic"]["feature_name"] == "Needs review"
    assert node_semantic["source_event"]["branch_ref"] == MAIN_REF
    assert node_semantic["source_event"]["operation_type"] == "accept"


def test_edge_projection_uses_same_branch_timeline_rules(conn):
    snapshot, graph = _create_main_snapshot(conn)
    nodes = {node["id"]: node for node in graph["deps_graph"]["nodes"]}
    edge = graph["deps_graph"]["edges"][0]
    edge_id = "L7.1->L7.2:depends_on"
    stable_edge_key = graph_events.stable_edge_key_for_edge(edge, nodes["L7.1"], nodes["L7.2"])
    edge_hash = graph_events.edge_signature_hash_for_edge(edge, nodes["L7.1"], nodes["L7.2"])

    graph_events.create_event(
        conn,
        PID,
        snapshot["snapshot_id"],
        event_id="edge-feature-branch",
        event_type="edge_semantic_enriched",
        event_kind="semantic_job",
        target_type="edge",
        target_id=edge_id,
        status=graph_events.EVENT_STATUS_OBSERVED,
        branch_ref=FEATURE_REF,
        operation_type="ai_enrich",
        stable_node_key=stable_edge_key,
        feature_hash=edge_hash,
        payload={"semantic_payload": {"relation_purpose": "wrong branch"}},
        created_by="test",
    )
    projection = graph_events.build_semantic_projection(
        conn,
        PID,
        snapshot["snapshot_id"],
        projection_id="edge-wrong-branch",
        backfill_existing=False,
    )
    edge_semantic = projection["projection"]["edge_semantics"][edge_id]
    assert edge_semantic["validity"]["status"] == "edge_semantic_missing"

    graph_events.create_event(
        conn,
        PID,
        snapshot["snapshot_id"],
        event_id="edge-main-accepted",
        event_type="edge_semantic_enriched",
        event_kind="semantic_job",
        target_type="edge",
        target_id=edge_id,
        status=graph_events.EVENT_STATUS_ACCEPTED,
        branch_ref=MAIN_REF,
        operation_type="accept",
        stable_node_key=stable_edge_key,
        feature_hash=edge_hash,
        payload={"semantic_payload": {"relation_purpose": "main branch"}},
        created_by="test",
    )
    projection = graph_events.build_semantic_projection(
        conn,
        PID,
        snapshot["snapshot_id"],
        projection_id="edge-main-branch",
        backfill_existing=False,
    )
    edge_semantic = projection["projection"]["edge_semantics"][edge_id]
    assert edge_semantic["validity"]["status"] == "edge_semantic_current"
    assert edge_semantic["semantic"]["relation_purpose"] == "main branch"
    assert edge_semantic["source_event"]["branch_ref"] == MAIN_REF
    assert edge_semantic["source_event"]["operation_type"] == "accept"
