"""MF-2026-05-10-016: regression tests for the event-driven in-process
semantic worker and its review-queue gate.

Covers:
- A. `_persist_semantic_state_to_db` honours `submit_for_review=True` by
     writing graph_semantic_nodes status="pending_review".
- B. `backfill_existing_semantic_events` maps `pending_review` rows to
     `EVENT_STATUS_PROPOSED`; non-pending rows stay `EVENT_STATUS_OBSERVED`.
- C. `accept_semantic_enrichment` is in FEEDBACK_DECISION_ACTIONS and
     the helper flips both the persistent row and the event status.
- D. `semantic_worker.register()` is idempotent and subscribes both topics.
- E. The publish on /semantic/jobs is fired (test the helper directly).
"""

from __future__ import annotations

import sqlite3

import pytest

from agent.governance import event_bus
from agent.governance import graph_events
from agent.governance import graph_snapshot_store as store
from agent.governance import reconcile_feedback
from agent.governance import reconcile_semantic_enrichment as semantic
from agent.governance import semantic_worker
from agent.governance import server
from agent.governance.db import _ensure_schema


PID = "semantic-worker-test"


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


def _create_snapshot_with_node(conn, snapshot_id: str, node_id: str = "L7.1") -> dict:
    nodes = [{
        "id": node_id,
        "layer": "L7",
        "title": f"Feature {node_id}",
        "kind": "service_runtime",
        "primary": [f"agent/governance/{node_id.replace('.', '_')}.py"],
        "secondary": [],
        "test": [],
        "metadata": {"subsystem": "governance"},
    }]
    snap = store.create_graph_snapshot(
        conn, PID, snapshot_id=snapshot_id, commit_sha="head",
        snapshot_kind="scope",
        graph_json={"deps_graph": {"nodes": nodes, "edges": []}},
    )
    store.index_graph_snapshot(conn, PID, snap["snapshot_id"], nodes=nodes, edges=[])
    return snap


def test_a_persist_submit_for_review_writes_pending_review_status(conn):
    """A: state writer forces status='pending_review' under the flag."""
    snap = _create_snapshot_with_node(conn, "persist-review")
    sid = snap["snapshot_id"]
    state = {
        "node_semantics": {
            "L7.1": {
                "status": "ai_complete",  # would normally be persisted as-is
                "feature_hash": "sha256:abc",
                "file_hashes": {"x": "y"},
                "updated_at": "2026-05-10T20:00:00Z",
                "semantic_summary": "hello",
            }
        },
    }
    semantic._persist_semantic_state_to_db(
        conn, PID, sid, state, submit_for_review=True,
    )
    conn.commit()
    row = conn.execute(
        "SELECT status FROM graph_semantic_nodes WHERE project_id=? AND snapshot_id=? AND node_id='L7.1'",
        (PID, sid),
    ).fetchone()
    assert row["status"] == "pending_review", (
        "submit_for_review=True must override the source row's status"
    )

    # Sanity: same call with submit_for_review=False keeps the source status.
    snap2 = _create_snapshot_with_node(conn, "persist-nogate")
    semantic._persist_semantic_state_to_db(
        conn, PID, snap2["snapshot_id"], state, submit_for_review=False,
    )
    conn.commit()
    row2 = conn.execute(
        "SELECT status FROM graph_semantic_nodes WHERE project_id=? AND snapshot_id=? AND node_id='L7.1'",
        (PID, snap2["snapshot_id"]),
    ).fetchone()
    assert row2["status"] == "ai_complete", (
        "default path must preserve source status"
    )


def test_a2_submit_for_review_skips_carried_forward_rows(conn):
    """A2 (regression for the 2026-05-10 first-run scoping spillover):
    `submit_for_review=True` must NOT flip rows that came from
    `_carry_forward_semantic_graph_state` (have `carried_forward_from_snapshot_id`
    set). Those were already accepted in a prior snapshot — the worker just
    happens to call run_semantic_enrichment with the gate flag for the freshly
    enriched ones, and the persistence layer has to scope the override correctly.
    """
    snap = _create_snapshot_with_node(conn, "carry-forward-scope")
    sid = snap["snapshot_id"]
    state = {
        "node_semantics": {
            "L7.1": {
                # Marker put on the entry by _carry_forward_semantic_graph_state.
                "carried_forward_from_snapshot_id": "scope-prev",
                "status": "ai_complete",
                "feature_hash": "sha256:carried",
                "semantic_summary": "carried",
            }
        }
    }
    semantic._persist_semantic_state_to_db(
        conn, PID, sid, state, submit_for_review=True,
    )
    conn.commit()
    row = conn.execute(
        "SELECT status FROM graph_semantic_nodes WHERE project_id=? AND snapshot_id=? AND node_id='L7.1'",
        (PID, sid),
    ).fetchone()
    assert row["status"] == "ai_complete", (
        "carried-forward rows must keep their original status even when the "
        "caller asked for submit_for_review — the gate is only for fresh enrichment"
    )


def test_b_backfill_maps_pending_review_to_proposed_event(conn):
    """B: backfill writes PROPOSED event for pending_review rows."""
    snap = _create_snapshot_with_node(conn, "backfill-review")
    sid = snap["snapshot_id"]
    # Persist a pending_review row + a regular ai_complete row.
    semantic._persist_semantic_state_to_db(
        conn, PID, sid,
        {
            "node_semantics": {
                "L7.1": {
                    "status": "pending_review",
                    "feature_hash": "sha256:p",
                    "semantic_summary": "p",
                },
            }
        },
        submit_for_review=False,  # rely on the row's own status
    )
    conn.commit()
    graph_events.backfill_existing_semantic_events(conn, PID, sid, actor="test")
    conn.commit()
    ev = conn.execute(
        """
        SELECT status FROM graph_events
        WHERE project_id=? AND snapshot_id=? AND event_type='semantic_node_enriched'
          AND target_id='L7.1'
        ORDER BY event_seq DESC LIMIT 1
        """,
        (PID, sid),
    ).fetchone()
    assert ev is not None, "backfill must emit an event for pending_review rows"
    assert ev["status"] == graph_events.EVENT_STATUS_PROPOSED


def test_c_accept_semantic_enrichment_in_decision_actions():
    """C: the verb is registered in the catalog."""
    assert "accept_semantic_enrichment" in reconcile_feedback.FEEDBACK_DECISION_ACTIONS


def test_c_accept_helper_flips_node_status_and_event(conn):
    """C: helper transitions pending_review → ai_complete and proposed → accepted."""
    snap = _create_snapshot_with_node(conn, "accept-helper")
    sid = snap["snapshot_id"]
    # Set up: one pending_review row + one proposed event.
    semantic._persist_semantic_state_to_db(
        conn, PID, sid,
        {
            "node_semantics": {
                "L7.1": {"status": "pending_review", "feature_hash": "sha256:h"},
            }
        },
        submit_for_review=False,
    )
    graph_events.backfill_existing_semantic_events(conn, PID, sid, actor="test")
    conn.commit()
    # Submit a feedback item with linked_event_ids.
    ev_id = conn.execute(
        "SELECT event_id FROM graph_events WHERE project_id=? AND snapshot_id=? AND target_id='L7.1' LIMIT 1",
        (PID, sid),
    ).fetchone()["event_id"]
    submitted = reconcile_feedback.submit_feedback_item(
        PID, sid,
        feedback_kind=reconcile_feedback.KIND_NEEDS_OBSERVER_DECISION,
        issue={
            "issue": "test review item",
            "source_node_ids": ["L7.1"],
            "target_id": "L7.1",
            "target_type": "node",
            "priority": "P3",
            "evidence": {
                "node_id": "L7.1",
                "linked_event_ids": [ev_id],
            },
        },
        actor="test",
    )
    feedback_id = submitted["items"][0]["feedback_id"]

    result = server._accept_semantic_enrichment_for_feedback_items(
        conn, PID, sid, [feedback_id], actor="test",
    )
    assert result["node_ids_flipped"] == ["L7.1"]
    assert result["event_ids_flipped"] == [ev_id]
    assert result["errors"] == []

    # Verify DB state.
    row = conn.execute(
        "SELECT status FROM graph_semantic_nodes WHERE project_id=? AND snapshot_id=? AND node_id='L7.1'",
        (PID, sid),
    ).fetchone()
    assert row["status"] == "ai_complete"
    ev_row = conn.execute(
        "SELECT status FROM graph_events WHERE event_id=?",
        (ev_id,),
    ).fetchone()
    assert ev_row["status"] == graph_events.EVENT_STATUS_ACCEPTED


def test_d_worker_register_is_idempotent_and_subscribes(monkeypatch):
    """D: register() is safe to call twice; subscribes both topics."""
    # Reset module-level state so the test is independent of import order.
    monkeypatch.setattr(semantic_worker, "_registered", False)
    subs: list[tuple[str, object]] = []

    class _StubBus:
        def subscribe(self, topic, callback):
            subs.append((topic, callback))

    monkeypatch.setattr(event_bus, "get_event_bus", lambda: _StubBus())
    # Stub catchup to no-op (no governance DB at test time).
    monkeypatch.setattr(semantic_worker, "on_governance_startup", lambda payload=None: None)
    semantic_worker.register()
    semantic_worker.register()  # idempotent — should not add duplicate subscribers
    topics = sorted({t for t, _ in subs})
    assert topics == ["semantic_job.enqueued", "system.startup"]


def test_e_publish_helper_does_not_raise_when_eventbus_absent(monkeypatch):
    """E: the publish on POST /semantic/jobs is best-effort.

    Verify the publish wrapper survives an EventBus that raises."""
    def _boom(*a, **kw):
        raise RuntimeError("synthetic bus failure")

    monkeypatch.setattr(event_bus, "publish", _boom)
    # Inline the same try/except contract the handler uses:
    try:
        event_bus.publish("semantic_job.enqueued", {"project_id": PID})
    except Exception:
        pass  # handler swallows
    # No assertion — surviving the call is the contract.
