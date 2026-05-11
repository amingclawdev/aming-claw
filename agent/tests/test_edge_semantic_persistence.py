"""Edge semantic persistent-state parity (MF 2026-05-11).

Mirrors the node-side carry-forward / backfill / projection pattern for
edges. Asserts:
  - `graph_semantic_edges` table is created by `_ensure_semantic_state_schema`.
  - `_persist_semantic_state_to_db` writes state.edge_semantics rows.
  - `_carry_forward_semantic_graph_state`:
      * carries entries whose edge_signature_hash matches base→current
      * skips entries whose endpoint disappeared (cascade)
      * skips entries whose signature drifted
  - `backfill_existing_semantic_events` writes edge_semantic_enriched
    events from graph_semantic_edges rows.
  - `_build_edge_semantics` drops orphans (event but no structural edge)
    so the projection matches the node-side scoping behaviour.
"""

from __future__ import annotations

import sqlite3

import pytest

from agent.governance import graph_events
from agent.governance import graph_snapshot_store as store
from agent.governance import reconcile_semantic_enrichment as semantic
from agent.governance.db import _ensure_schema


PID = "edge-sem-test"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    store.ensure_schema(c)
    semantic._ensure_semantic_state_schema(c)
    yield c
    c.close()


def _graph(node_ids: list[str], edges: list[dict]) -> dict:
    return {
        "deps_graph": {
            "nodes": [
                {
                    "id": nid,
                    "layer": "L7",
                    "title": nid,
                    "kind": "service_runtime",
                    "primary": [f"agent/governance/{nid}.py"],
                    "secondary": [],
                    "test": [],
                    "metadata": {"subsystem": "governance"},
                }
                for nid in node_ids
            ],
            "edges": edges,
        }
    }


def _create_snapshot(conn, snapshot_id, commit, node_ids, edges):
    graph = _graph(node_ids, edges)
    snap = store.create_graph_snapshot(
        conn, PID,
        snapshot_id=snapshot_id, commit_sha=commit, snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn, PID, snap["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    conn.commit()
    return snap, graph


def test_schema_creates_graph_semantic_edges_table(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='graph_semantic_edges'"
    ).fetchone()
    assert row is not None, "graph_semantic_edges table must exist after _ensure_semantic_state_schema"


def test_persist_writes_edge_semantics_rows(conn):
    """_persist_semantic_state_to_db.edge_semantics loop persists rows."""
    state = {
        "edge_semantics": {
            "L7.1->L7.2:depends_on": {
                "edge_signature_hash": "hashABC",
                "semantic_payload": {"relation_purpose": "uses API"},
                "status": "ai_complete",
                "feedback_round": 0,
                "updated_at": "2026-05-11T16:00:00Z",
            }
        }
    }
    semantic._persist_semantic_state_to_db(
        conn, PID, "snap-1", state, submit_for_review=False,
    )
    rows = conn.execute(
        "SELECT * FROM graph_semantic_edges WHERE project_id=? AND snapshot_id=?",
        (PID, "snap-1"),
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["edge_id"] == "L7.1->L7.2:depends_on"
    assert row["edge_signature_hash"] == "hashABC"
    assert row["status"] == "ai_complete"


def test_persist_submit_for_review_overrides_status(conn):
    """When submit_for_review=True, fresh entries become pending_review."""
    state = {
        "edge_semantics": {
            "L7.1->L7.2:depends_on": {
                "edge_signature_hash": "h1",
                "semantic_payload": {"relation_purpose": "x"},
                "status": "ai_complete",
            }
        }
    }
    semantic._persist_semantic_state_to_db(
        conn, PID, "snap-1", state, submit_for_review=True,
    )
    row = conn.execute(
        "SELECT status FROM graph_semantic_edges WHERE project_id=? AND snapshot_id=? AND edge_id=?",
        (PID, "snap-1", "L7.1->L7.2:depends_on"),
    ).fetchone()
    assert row["status"] == "pending_review"


def test_persist_submit_for_review_skips_carried_forward(conn):
    """Carry-forward'd entries keep their accepted status even under submit_for_review."""
    state = {
        "edge_semantics": {
            "L7.1->L7.2:depends_on": {
                "edge_signature_hash": "h1",
                "status": "ai_complete",
                "carried_forward_from_snapshot_id": "snap-0",
            }
        }
    }
    semantic._persist_semantic_state_to_db(
        conn, PID, "snap-1", state, submit_for_review=True,
    )
    row = conn.execute(
        "SELECT status FROM graph_semantic_edges WHERE project_id=? AND snapshot_id=? AND edge_id=?",
        (PID, "snap-1", "L7.1->L7.2:depends_on"),
    ).fetchone()
    assert row["status"] == "ai_complete"


def test_carry_forward_copies_unchanged_edges(conn):
    """Edges with matching signature_hash carry across snapshots."""
    base_state = {
        "edge_semantics": {
            "L7.1->L7.2:depends_on": {
                "edge_signature_hash": "sig-stable",
                "semantic_payload": {"relation_purpose": "old"},
            }
        }
    }
    state = {}
    edge_index = {
        "L7.1->L7.2:depends_on": {
            "edge_signature_hash": "sig-stable",  # identical → carry
            "stable_edge_key": "stable-k",
        }
    }
    report = semantic._carry_forward_semantic_graph_state(
        state,
        base_state,
        feature_index={},
        base_snapshot_id="snap-0",
        updated_at="2026-05-11T17:00:00Z",
        edge_index=edge_index,
    )
    assert report["edge_carried_forward_count"] == 1
    entry = state["edge_semantics"]["L7.1->L7.2:depends_on"]
    assert entry["carried_forward_from_snapshot_id"] == "snap-0"
    assert entry["edge_signature_hash"] == "sig-stable"
    assert entry["stable_edge_key"] == "stable-k"


def test_carry_forward_skips_signature_drift(conn):
    """If signature_hash differs, edge is NOT carried forward."""
    base_state = {
        "edge_semantics": {
            "L7.1->L7.2:depends_on": {
                "edge_signature_hash": "sig-old",
                "semantic_payload": {"relation_purpose": "stale"},
            }
        }
    }
    state = {}
    edge_index = {
        "L7.1->L7.2:depends_on": {
            "edge_signature_hash": "sig-new",  # different
            "stable_edge_key": "stable-k",
        }
    }
    report = semantic._carry_forward_semantic_graph_state(
        state, base_state, feature_index={},
        base_snapshot_id="snap-0",
        updated_at="2026-05-11T17:00:00Z",
        edge_index=edge_index,
    )
    assert report["edge_carried_forward_count"] == 0
    assert report["edge_skipped_hash_mismatch_count"] == 1
    assert "L7.1->L7.2:depends_on" not in state.get("edge_semantics", {})


def test_carry_forward_skips_deleted_endpoint(conn):
    """If endpoint node deleted (edge not in new edge_index), drop the semantic."""
    base_state = {
        "edge_semantics": {
            "L7.1->L7.2:depends_on": {
                "edge_signature_hash": "any",
                "semantic_payload": {"relation_purpose": "stale"},
            }
        }
    }
    state = {}
    edge_index: dict = {}  # edge no longer present in current structure
    report = semantic._carry_forward_semantic_graph_state(
        state, base_state, feature_index={},
        base_snapshot_id="snap-0",
        updated_at="2026-05-11T17:00:00Z",
        edge_index=edge_index,
    )
    assert report["edge_carried_forward_count"] == 0
    assert report["edge_skipped_missing_count"] == 1
    assert "L7.1->L7.2:depends_on" not in state.get("edge_semantics", {})


def test_backfill_writes_edge_semantic_events(conn):
    """backfill_existing_semantic_events reads graph_semantic_edges → graph_events."""
    snap, _ = _create_snapshot(
        conn, "snap-bf", "head-bf", ["L7.1", "L7.2"],
        [{"source": "L7.1", "target": "L7.2", "edge_type": "depends_on",
          "evidence": {"source": "test"}}],
    )
    # Pre-populate graph_semantic_edges
    conn.execute(
        """
        INSERT INTO graph_semantic_edges
          (project_id, snapshot_id, edge_id, status, edge_signature_hash,
           semantic_json, feedback_round, batch_index, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            PID, snap["snapshot_id"], "L7.1->L7.2:depends_on",
            "pending_review", "sigX",
            '{"semantic_payload": {"relation_purpose": "calls A"}, "stable_edge_key": "skX"}',
            0, None, "2026-05-11T17:00:00Z",
        ),
    )
    conn.commit()
    report = graph_events.backfill_existing_semantic_events(
        conn, PID, snap["snapshot_id"], actor="test",
    )
    assert report["edge_semantic_events_created"] == 1
    # Event row exists with stable_edge_key in stable_node_key column.
    ev = conn.execute(
        """
        SELECT event_id, status, stable_node_key, target_id
        FROM graph_events
        WHERE project_id=? AND snapshot_id=?
          AND event_type='edge_semantic_enriched'
        """,
        (PID, snap["snapshot_id"]),
    ).fetchone()
    assert ev is not None
    assert ev["target_id"] == "L7.1->L7.2:depends_on"
    assert ev["stable_node_key"] == "skX"
    # pending_review → PROPOSED
    assert ev["status"] == graph_events.EVENT_STATUS_PROPOSED


def test_build_edge_semantics_drops_orphans():
    """Orphan (event exists, structural edge missing) MUST be dropped from
    projection — node-side parity. Pre-fix this returned 'orphaned' status."""
    edges: list = []  # no structural edges
    edge_events = {
        "L7.99->L4.1:depends_on": {
            "event_id": "ge-orphan", "event_type": "edge_semantic_enriched",
            "payload_json": '{"semantic_payload": {}}',
        }
    }
    out = graph_events._build_edge_semantics(edges, edge_events)
    assert out == {}, "orphan with no structural edge must not appear in projection"


def test_build_edge_semantics_keeps_structural_edges():
    """Structural edges always appear, regardless of whether semantic exists."""
    edges = [
        {"source": "L7.1", "target": "L7.2", "edge_type": "depends_on"},
        {"source": "L7.1", "target": "L7.3", "edge_type": "depends_on"},
    ]
    edge_events = {
        "L7.1->L7.2:depends_on": {
            "event_id": "ge-1", "event_type": "edge_semantic_enriched",
            "payload_json": '{"semantic_payload": {"relation_purpose": "x"}}',
        }
    }
    out = graph_events._build_edge_semantics(edges, edge_events)
    assert set(out) == {"L7.1->L7.2:depends_on", "L7.1->L7.3:depends_on"}
    assert out["L7.1->L7.2:depends_on"]["validity"]["status"] == "edge_semantic_current"
    assert out["L7.1->L7.3:depends_on"]["validity"]["status"] == "edge_semantic_missing"


def test_latest_edge_semantic_events_finds_cross_snapshot(conn):
    """Event in snap-A must be findable from snap-B via stable_edge_key."""
    snap_a, _ = _create_snapshot(
        conn, "snap-a", "commitA", ["L7.1", "L7.2"],
        [{"source": "L7.1", "target": "L7.2", "edge_type": "depends_on"}],
    )
    # Write an ACCEPTED enriched event in snap-A with stable_edge_key = sk1.
    # The cross-snapshot lookup only returns OBSERVED/MATERIALIZED/ACCEPTED
    # — PROPOSED is awaiting review and intentionally not surfaced in the
    # projection, same as the node-side filter.
    graph_events.create_event(
        conn, PID, snap_a["snapshot_id"],
        event_type="edge_semantic_enriched",
        event_kind="semantic_job",
        target_type="edge",
        target_id="L7.1->L7.2:depends_on",
        status=graph_events.EVENT_STATUS_ACCEPTED,
        stable_node_key="sk1",  # reused column = stable_edge_key
        payload={"semantic_payload": {"relation_purpose": "X"}},
    )
    snap_b, _ = _create_snapshot(
        conn, "snap-b", "commitB", ["L7.1", "L7.2"],
        [{"source": "L7.1", "target": "L7.2", "edge_type": "depends_on"}],
    )
    # Query snap-B with edge_index carrying the same stable_edge_key.
    edge_index = {
        "L7.1->L7.2:depends_on": {"stable_edge_key": "sk1"},
    }
    latest = graph_events._latest_edge_semantic_events(
        conn, PID, snap_b["snapshot_id"], edge_index=edge_index,
    )
    assert "L7.1->L7.2:depends_on" in latest
    assert latest["L7.1->L7.2:depends_on"]["snapshot_id"] == snap_a["snapshot_id"]
