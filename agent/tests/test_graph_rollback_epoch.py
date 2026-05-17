"""PB-005/PB-011 graph rollback epoch state tests."""

from __future__ import annotations

import sqlite3

import pytest

from agent.governance import graph_events
from agent.governance import graph_snapshot_store as store
from agent.governance.db import _ensure_schema

PID = "graph-rollback-test"
BRANCH_REF = "refs/heads/codex/PB011-branch-artifact"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _snapshot(conn: sqlite3.Connection, snapshot_id: str, commit_sha: str, **kwargs):
    return store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id=snapshot_id,
        commit_sha=commit_sha,
        snapshot_kind="scope",
        graph_json={"deps_graph": {"nodes": [], "edges": []}},
        **kwargs,
    )


def _projection(
    conn: sqlite3.Connection,
    snapshot_id: str,
    projection_id: str,
    *,
    ref_name: str = "active",
    branch_ref: str = "",
) -> dict:
    return graph_events.build_semantic_projection(
        conn,
        PID,
        snapshot_id,
        projection_id=projection_id,
        ref_name=ref_name,
        branch_ref=branch_ref,
        actor="pb-rollback-test",
        backfill_existing=False,
    )


def test_pb011_branch_candidate_graph_artifact_does_not_move_active_target(conn):
    _ensure_schema(conn)
    base = _snapshot(conn, "scope-main-B0", "B0")
    branch = _snapshot(
        conn,
        "scope-branch-H1",
        "H1",
        ref_name=BRANCH_REF,
        branch_ref=BRANCH_REF,
    )
    _projection(conn, base["snapshot_id"], "semproj-main-B0")
    _projection(
        conn,
        branch["snapshot_id"],
        "semproj-branch-H1",
        ref_name=BRANCH_REF,
        branch_ref=BRANCH_REF,
    )

    store.activate_graph_snapshot(
        conn,
        PID,
        base["snapshot_id"],
        auto_rebuild_projection=False,
    )
    store.activate_graph_snapshot(
        conn,
        PID,
        branch["snapshot_id"],
        ref_name=BRANCH_REF,
        branch_ref=BRANCH_REF,
        operation_type="activate",
        batch_id="batch-PB011",
        merge_queue_id="mergeq-PB011",
        auto_rebuild_projection=False,
    )

    active = store.get_active_graph_snapshot(conn, PID, ref_name="active")
    assert active["snapshot_id"] == base["snapshot_id"]

    state = store.build_graph_rollback_epoch_state(conn, PID, ref_name="active")
    assert state["active"]["snapshot_id"] == base["snapshot_id"]
    assert [event["new_snapshot_id"] for event in state["branch_candidates"]] == [
        branch["snapshot_id"]
    ]

    projections = {row["projection_id"]: row for row in state["projection_states"]}
    assert projections["semproj-main-B0"]["status"] == "current"
    assert projections["semproj-branch-H1"]["status"] == "candidate"
    assert projections["semproj-branch-H1"]["ref_name"] == BRANCH_REF


def test_pb005_rollback_epoch_labels_abandoned_merge_and_isolated_pending_scope(conn):
    _ensure_schema(conn)
    base = _snapshot(conn, "scope-main-B0", "B0")
    merged = _snapshot(conn, "scope-main-M1", "M1")
    rollback = _snapshot(conn, "scope-main-R1", "B0")
    branch = _snapshot(
        conn,
        "scope-branch-H1",
        "H1",
        ref_name=BRANCH_REF,
        branch_ref=BRANCH_REF,
    )
    _projection(conn, base["snapshot_id"], "semproj-main-B0")
    _projection(
        conn,
        branch["snapshot_id"],
        "semproj-branch-H1",
        ref_name=BRANCH_REF,
        branch_ref=BRANCH_REF,
    )
    _projection(
        conn,
        merged["snapshot_id"],
        "semproj-merge-M1",
        ref_name="active",
        branch_ref=BRANCH_REF,
    )
    _projection(conn, rollback["snapshot_id"], "semproj-rollback-B0")

    store.activate_graph_snapshot(
        conn,
        PID,
        base["snapshot_id"],
        auto_rebuild_projection=False,
    )
    store.activate_graph_snapshot(
        conn,
        PID,
        branch["snapshot_id"],
        ref_name=BRANCH_REF,
        branch_ref=BRANCH_REF,
        operation_type="activate",
        batch_id="batch-PB005",
        merge_queue_id="mergeq-PB005",
        auto_rebuild_projection=False,
    )
    merge_activation = store.activate_graph_snapshot(
        conn,
        PID,
        merged["snapshot_id"],
        operation_type="merge",
        branch_ref=BRANCH_REF,
        batch_id="batch-PB005",
        merge_queue_id="mergeq-PB005",
        merge_epoch="merge-001",
        auto_rebuild_projection=False,
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="H1",
        ref_name=BRANCH_REF,
        branch_ref=BRANCH_REF,
        worktree_id="wt-PB005-branch",
        evidence={"batch_id": "batch-PB005", "source": "branch-candidate"},
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="B0",
        ref_name="active",
        evidence={"rollback_epoch": "rollback-001", "source": "target-rollback"},
    )
    store.activate_graph_snapshot(
        conn,
        PID,
        rollback["snapshot_id"],
        operation_type="rollback",
        batch_id="batch-PB005",
        rollback_epoch="rollback-001",
        source_event_id=merge_activation["graph_ref_event_id"],
        evidence={"reason": "wrong merge order"},
        auto_rebuild_projection=False,
    )

    state = store.build_graph_rollback_epoch_state(
        conn,
        PID,
        ref_name="active",
        rollback_epoch="rollback-001",
    )

    assert state["active"] == {
        "snapshot_id": rollback["snapshot_id"],
        "commit_sha": "B0",
        "event_id": state["rollback_event"]["event_id"],
        "operation_type": "rollback",
        "projection_id": "semproj-rollback-B0",
    }
    assert state["rollback_event"]["source_event_id"] == merge_activation["graph_ref_event_id"]
    assert state["abandoned_merge_epochs"] == ["merge-001"]
    assert state["abandoned_merge_events"][0]["new_snapshot_id"] == merged["snapshot_id"]
    assert state["branch_candidates"][0]["new_snapshot_id"] == branch["snapshot_id"]

    projections = {row["projection_id"]: row for row in state["projection_states"]}
    assert projections["semproj-rollback-B0"]["status"] == "current"
    assert projections["semproj-merge-M1"]["status"] == "abandoned"
    assert projections["semproj-branch-H1"]["status"] == "candidate"

    pending_by_ref = {row["ref_name"]: row for row in state["pending_scope"]}
    assert pending_by_ref["active"]["evidence"]["rollback_epoch"] == "rollback-001"
    assert pending_by_ref[BRANCH_REF]["branch_ref"] == BRANCH_REF
    assert pending_by_ref[BRANCH_REF]["worktree_id"] == "wt-PB005-branch"
