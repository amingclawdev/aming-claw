from __future__ import annotations

import json


PID = "graph-divergence-test"


def _seed_active_snapshot(conn, commit_sha="old-graph"):
    from governance import graph_snapshot_store as store

    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id=f"imported-{commit_sha}-test",
        commit_sha=commit_sha,
        snapshot_kind="imported",
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    return snapshot


def _pending_row(conn, commit_sha):
    return conn.execute(
        "SELECT * FROM pending_scope_reconcile WHERE project_id=? AND commit_sha=?",
        (PID, commit_sha),
    ).fetchone()


def test_dispatch_graph_divergence_hook_advisory_queues_without_blocking(isolated_gov_db):
    from governance import auto_chain

    conn = isolated_gov_db
    _seed_active_snapshot(conn, "old-graph")
    metadata = {"target_commit_sha": "new-head"}

    info = auto_chain._dispatch_graph_divergence_hook(
        conn,
        PID,
        "task-pm",
        "pm",
        metadata,
    )

    assert info["diverged"] is True
    assert info["queued"] is True
    assert info["blocked"] is False
    assert metadata["graph_stale"] is True
    assert metadata["graph_divergence"]["mode"] == "advisory"

    pending = _pending_row(conn, "new-head")
    assert pending["status"] == "queued"
    assert pending["parent_commit_sha"] == "old-graph"
    evidence = json.loads(pending["evidence_json"])
    assert evidence["source"] == "dispatch_graph_divergence_hook"
    assert evidence["task_id"] == "task-pm"


def test_dispatch_graph_divergence_hook_strict_blocks_without_bypass(isolated_gov_db):
    from governance import auto_chain

    conn = isolated_gov_db
    _seed_active_snapshot(conn, "old-graph")
    metadata = {"target_commit_sha": "new-head", "graph_gate_mode": "strict"}

    info = auto_chain._dispatch_graph_divergence_hook(
        conn,
        PID,
        "task-dev",
        "dev",
        metadata,
    )

    assert info["queued"] is True
    assert info["blocked"] is True
    assert info["reason"] == "active_graph_snapshot_stale"
    assert _pending_row(conn, "new-head")["status"] == "queued"


def test_dispatch_graph_divergence_hook_strict_bypass_and_raw_do_not_block(isolated_gov_db):
    from governance import auto_chain

    conn = isolated_gov_db
    _seed_active_snapshot(conn, "old-graph")

    strict_bypass = {"target_commit_sha": "new-head", "graph_gate_mode": "strict"}
    strict_info = auto_chain._dispatch_graph_divergence_hook(
        conn,
        PID,
        "task-mf",
        "dev",
        strict_bypass,
        graph_governance_bypassed=True,
    )
    assert strict_info["queued"] is True
    assert strict_info["blocked"] is False

    raw_metadata = {"target_commit_sha": "raw-head", "graph_gate_mode": "raw"}
    raw_info = auto_chain._dispatch_graph_divergence_hook(
        conn,
        PID,
        "task-raw",
        "task",
        raw_metadata,
    )
    assert raw_info["queued"] is True
    assert raw_info["blocked"] is False
    assert raw_info["reason"] == "active_graph_snapshot_stale_raw"
    assert _pending_row(conn, "raw-head")["status"] == "queued"


def test_dispatch_graph_divergence_hook_uses_project_version_target(isolated_gov_db):
    from governance import auto_chain

    conn = isolated_gov_db
    _seed_active_snapshot(conn, "old-graph")
    conn.execute(
        """
        INSERT INTO project_version(project_id, chain_version, git_head, updated_at, updated_by)
        VALUES (?, ?, ?, datetime('now'), 'test')
        """,
        (PID, "old-graph", "new-head"),
    )

    info = auto_chain._dispatch_graph_divergence_hook(
        conn,
        PID,
        "task-test",
        "test",
        {},
    )

    assert info["target_commit"] == "new-head"
    assert info["queued"] is True
    assert _pending_row(conn, "new-head")["status"] == "queued"
