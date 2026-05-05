"""Tests for agent/governance/reconcile_session.py (CR0a state machine).

10 module-level tests using a tmp_path sqlite fixture. Endpoint and bypass-
middleware coverage is OUT OF SCOPE — those belong to CR0b.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

import pytest

_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from governance import reconcile_session as rs
from governance.db import _configure_connection, _ensure_schema


PROJECT_ID = "p-test"


@pytest.fixture()
def gov_dir(tmp_path: Path) -> Path:
    d = tmp_path / "governance"
    d.mkdir(parents=True, exist_ok=True)
    # Provide a graph.json so capture/restore can copy it.
    graph = {
        "version": 1,
        "deps_graph": {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": [{
                "id": "L1.1",
                "title": "Existing Root",
                "layer": "L1",
                "primary": [],
                "secondary": [],
                "test": [],
                "_deps": [],
            }],
            "edges": [],
        },
        "gates_graph": {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": [],
            "edges": [],
        },
    }
    (d / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    return d


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "gov.db"
    c = sqlite3.connect(str(db_path))
    _configure_connection(c, busy_timeout=0)
    _ensure_schema(c)
    yield c
    c.close()


def _seed_node_state(c: sqlite3.Connection, project_id: str, n: int = 3) -> None:
    for i in range(n):
        c.execute(
            "INSERT OR REPLACE INTO node_state ("
            "project_id, node_id, verify_status, build_status, evidence_json, "
            "updated_by, updated_at, version) VALUES (?,?,?,?,?,?,?,?)",
            (project_id, f"L1.{i}", "pending", "impl:missing",
             json.dumps({"i": i}), "test", "2026-05-02T00:00:00Z", 1))
    c.commit()


def test_get_active_returns_none_when_idle(conn):
    assert rs.get_active_session(conn, PROJECT_ID) is None


def test_start_session_creates_row(conn, gov_dir):
    sess = rs.start_session(conn, PROJECT_ID, started_by="tester",
                            bypass_gates=["gate-a", "gate-b"],
                            base_commit_sha="abc123",
                            governance_dir=gov_dir)
    assert sess.project_id == PROJECT_ID
    assert sess.status == "active"
    assert sess.bypass_gates == ["gate-a", "gate-b"]
    assert sess.base_commit_sha == "abc123"
    row = conn.execute(
        "SELECT status, bypass_gates_json, started_by, base_commit_sha, "
        "snapshot_head_sha FROM reconcile_sessions "
        "WHERE project_id=? AND session_id=?",
        (PROJECT_ID, sess.session_id)).fetchone()
    assert row[0] == "active"
    assert json.loads(row[1]) == ["gate-a", "gate-b"]
    assert row[2] == "tester"
    assert row[3] == "abc123"
    assert row[4] == "abc123"


def test_concurrent_start_one_wins(tmp_path: Path):
    # Two threads racing to start a session for the same project — exactly one wins.
    db_path = tmp_path / "race.db"
    # Initialise schema once via a setup connection.
    setup = sqlite3.connect(str(db_path))
    _configure_connection(setup, busy_timeout=2000)
    _ensure_schema(setup)
    setup.close()

    gov_dir = tmp_path / "gov"
    gov_dir.mkdir()

    results = {"ok": 0, "conflict": 0, "other": 0}
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def worker():
        c = sqlite3.connect(str(db_path), timeout=5)
        _configure_connection(c, busy_timeout=5000)
        try:
            barrier.wait(timeout=5)
            try:
                rs.start_session(c, PROJECT_ID, governance_dir=gov_dir)
                with lock:
                    results["ok"] += 1
            except rs.SessionAlreadyActiveError:
                with lock:
                    results["conflict"] += 1
            except Exception:
                with lock:
                    results["other"] += 1
        finally:
            c.close()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert results["ok"] == 1, results
    assert results["conflict"] == 1, results


def test_transition_idle_active_finalizing_idle(conn, gov_dir):
    assert rs.get_active_session(conn, PROJECT_ID) is None
    sess = rs.start_session(conn, PROJECT_ID, governance_dir=gov_dir)
    fetched = rs.get_active_session(conn, PROJECT_ID)
    assert fetched is not None and fetched.session_id == sess.session_id
    assert fetched.status == "active"
    after = rs.transition_to_finalizing(conn, PROJECT_ID, sess.session_id)
    assert after.status == "finalizing"
    rs.finalize_session(conn, PROJECT_ID, sess.session_id, governance_dir=gov_dir)
    assert rs.get_active_session(conn, PROJECT_ID) is None


def test_finalize_clears_overlay_and_archives_bak(conn, gov_dir):
    sess = rs.start_session(conn, PROJECT_ID, governance_dir=gov_dir)
    overlay = gov_dir / "graph.rebase.overlay.json"
    assert overlay.exists()
    result = rs.finalize_session(conn, PROJECT_ID, sess.session_id, governance_dir=gov_dir)
    assert not overlay.exists()
    assert (gov_dir / "graph.rebase.overlay.json.bak").exists()
    assert result.graph_path.endswith("graph.json")
    assert result.materialized_node_count == 1


def test_finalize_materializes_overlay_before_archiving(conn, gov_dir):
    sess = rs.start_session(conn, PROJECT_ID, governance_dir=gov_dir)
    (gov_dir / "new_module.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    overlay = gov_dir / "graph.rebase.overlay.json"
    overlay.write_text(json.dumps({
        "session_id": sess.session_id,
        "project_id": PROJECT_ID,
        "nodes": {
            "L7.1": {
                "node_id": "L7.1",
                "parent_layer": "L7",
                "title": "New Module",
                "primary": ["new_module.py"],
                "deps": [],
                "verify_status": "pending",
            }
        },
    }), encoding="utf-8")

    result = rs.finalize_session(
        conn, PROJECT_ID, sess.session_id,
        governance_dir=gov_dir, workspace_dir=gov_dir,
    )
    graph = json.loads((gov_dir / "graph.json").read_text(encoding="utf-8"))
    node_ids = {n["id"] for n in graph["deps_graph"]["nodes"]}
    assert {"L1.1", "L7.1"}.issubset(node_ids)
    assert result.materialization_counts["new_overlay_nodes"] == 1
    assert result.materialization_counts["carried_forward_nodes"] == 1
    assert result.graph_backup_path.endswith(f"{sess.session_id}.bak")
    assert not overlay.exists()


def test_finalize_failure_preserves_overlay_graph_and_session(conn, gov_dir):
    sess = rs.start_session(conn, PROJECT_ID, governance_dir=gov_dir)
    overlay = gov_dir / "graph.rebase.overlay.json"
    overlay.write_text(json.dumps({
        "session_id": sess.session_id,
        "project_id": PROJECT_ID,
        "nodes": {
            "L7.2": {
                "node_id": "L7.2",
                "parent_layer": "L7",
                "title": "Broken Module",
                "primary": ["missing_module.py"],
            }
        },
    }), encoding="utf-8")
    graph_before = (gov_dir / "graph.json").read_bytes()
    overlay_before = overlay.read_bytes()

    with pytest.raises(ValueError, match="missing primary paths"):
        rs.finalize_session(
            conn, PROJECT_ID, sess.session_id,
            governance_dir=gov_dir, workspace_dir=gov_dir,
        )

    assert overlay.exists()
    assert overlay.read_bytes() == overlay_before
    assert (gov_dir / "graph.json").read_bytes() == graph_before
    failed = rs.get_active_session(conn, PROJECT_ID)
    assert failed.session_id == sess.session_id
    assert failed.status == "finalize_failed"
    assert failed.finalize_error["type"] == "ValueError"
    assert "missing primary paths" in failed.finalize_error["message"]


def test_finalize_full_rebase_uses_candidate_hierarchy(conn, gov_dir):
    sess = rs.start_session(conn, PROJECT_ID, governance_dir=gov_dir)
    (gov_dir / "new_module.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    overlay = gov_dir / "graph.rebase.overlay.json"
    overlay.write_text(json.dumps({
        "session_id": sess.session_id,
        "project_id": PROJECT_ID,
        "nodes": {
            "L7.99": {
                "node_id": "L7.99",
                "parent_layer": "L7",
                "title": "Approved Leaf",
                "primary": ["new_module.py"],
            }
        },
    }), encoding="utf-8")
    candidate = gov_dir / "graph.rebase.candidate.json"
    candidate.write_text(json.dumps({
        "version": 1,
        "hierarchy_graph": {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": [
                {"id": "L1.1", "title": "Runtime", "layer": "L1", "primary": []},
                {"id": "L3.1", "title": "Subsystem", "layer": "L3", "primary": []},
                {"id": "L7.1", "title": "Candidate Leaf", "layer": "L7",
                 "primary": ["new_module.py"]},
            ],
            "links": [
                {"source": "L1.1", "target": "L3.1", "relation": "contains"},
                {"source": "L3.1", "target": "L7.1", "relation": "contains"},
            ],
        },
        "evidence_graph": {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": [
                {"id": "L1.1", "title": "Runtime", "layer": "L1", "primary": []},
                {"id": "L3.1", "title": "Subsystem", "layer": "L3", "primary": []},
                {"id": "L7.1", "title": "Candidate Leaf", "layer": "L7",
                 "primary": ["new_module.py"]},
            ],
            "links": [
                {"source": "L7.1", "target": "L3.1", "relation": "writes_state"},
            ],
        },
        "deps_graph": {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": [
                {"id": "L1.1", "title": "Runtime", "layer": "L1", "primary": []},
                {"id": "L3.1", "title": "Subsystem", "layer": "L3", "primary": []},
                {"id": "L7.1", "title": "Candidate Leaf", "layer": "L7",
                 "primary": ["new_module.py"]},
            ],
            "links": [
                {"source": "L1.1", "target": "L3.1", "relation": "depends_on"},
                {"source": "L3.1", "target": "L7.1", "relation": "depends_on"},
            ],
        },
    }), encoding="utf-8")

    result = rs.finalize_session(
        conn, PROJECT_ID, sess.session_id,
        governance_dir=gov_dir, workspace_dir=gov_dir,
        candidate_graph_path=candidate, full_rebase=True,
    )
    graph = json.loads((gov_dir / "graph.json").read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in graph["deps_graph"]["nodes"]}
    links = graph["deps_graph"]["edges"]
    assert set(nodes) == {"L1.1", "L3.1", "L7.99"}
    assert {"source": "L3.1", "target": "L7.99", "relation": "depends_on"} in links
    assert nodes["L7.99"]["metadata"]["candidate_node_ids"] == ["L7.1"]
    assert nodes["L7.99"]["_deps"] == ["L3.1"]
    assert {"source": "L3.1", "target": "L7.99", "relation": "contains"} in graph["hierarchy_graph"]["links"]
    assert {"source": "L7.99", "target": "L3.1", "relation": "writes_state"} in graph["evidence_graph"]["links"]
    assert result.materialization_counts["final_edge_count"] == 2


def test_finalize_full_rebase_reallocates_colliding_overlay_leaf_and_carries_assets(conn, gov_dir):
    sess = rs.start_session(conn, PROJECT_ID, governance_dir=gov_dir)
    (gov_dir / "code_a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (gov_dir / "code_b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    overlay = gov_dir / "graph.rebase.overlay.json"
    overlay.write_text(json.dumps({
        "session_id": sess.session_id,
        "project_id": PROJECT_ID,
        "nodes": {
            "L2.8": {
                "node_id": "L2.8",
                "parent_layer": "L7",
                "title": "Approved Aggregated Leaf",
                "primary": ["code_a.py", "code_b.py"],
            }
        },
    }), encoding="utf-8")
    candidate = gov_dir / "graph.rebase.candidate.json"
    hierarchy_nodes = [
        {"id": "L1.1", "title": "Runtime", "layer": "L1", "primary": []},
        {"id": "L2.8", "title": "Governance", "layer": "L2", "primary": []},
        {"id": "L3.11", "title": "Reconcile", "layer": "L3", "primary": []},
        {"id": "L3.33", "title": "Backlog", "layer": "L3", "primary": []},
        {"id": "L7.64", "title": "Candidate A", "layer": "L7",
         "primary": ["code_a.py"], "secondary": ["docs/a.md"],
         "test_coverage": {"test_files": ["agent/tests/test_a.py"]}},
        {"id": "L7.66", "title": "Candidate B", "layer": "L7",
         "primary": ["code_b.py"], "secondary_files": ["docs/b.md"],
         "test": ["agent/tests/test_b.py"]},
    ]
    hierarchy_links = [
        {"source": "L1.1", "target": "L2.8", "relation": "contains"},
        {"source": "L2.8", "target": "L3.11", "relation": "contains"},
        {"source": "L2.8", "target": "L3.33", "relation": "contains"},
        {"source": "L3.11", "target": "L7.64", "relation": "contains"},
        {"source": "L3.11", "target": "L7.66", "relation": "contains"},
    ]
    candidate.write_text(json.dumps({
        "version": 1,
        "hierarchy_graph": {
            "directed": True, "multigraph": False, "graph": {},
            "nodes": hierarchy_nodes, "links": hierarchy_links,
        },
        "deps_graph": {
            "directed": True, "multigraph": False, "graph": {},
            "nodes": hierarchy_nodes, "links": hierarchy_links,
        },
        "evidence_graph": {
            "directed": True, "multigraph": False, "graph": {},
            "nodes": hierarchy_nodes,
            "links": [
                {"source": "L7.64", "target": "L2.8", "relation": "reads_state"},
                {"source": "L7.66", "target": "L2.8", "relation": "writes_state"},
            ],
        },
    }), encoding="utf-8")

    result = rs.finalize_session(
        conn, PROJECT_ID, sess.session_id,
        governance_dir=gov_dir, workspace_dir=gov_dir,
        candidate_graph_path=candidate, full_rebase=True,
    )
    graph = json.loads((gov_dir / "graph.json").read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in graph["deps_graph"]["nodes"]}
    approved_leaf_ids = [
        nid for nid, node in nodes.items()
        if node.get("metadata", {}).get("overlay_node_id") == "L2.8"
    ]
    assert approved_leaf_ids == ["L7.67"]
    assert "L2.8" in nodes
    assert nodes["L2.8"].get("primary") == []
    leaf = nodes["L7.67"]
    assert leaf["metadata"]["reallocated_from_colliding_overlay_id"] is True
    assert leaf["metadata"]["candidate_node_ids"] == ["L7.64", "L7.66"]
    assert leaf["secondary"] == ["docs/a.md", "docs/b.md"]
    assert leaf["test"] == ["agent/tests/test_a.py", "agent/tests/test_b.py"]
    hierarchy = graph["hierarchy_graph"]["links"]
    assert {"source": "L1.1", "target": "L2.8", "relation": "contains"} in hierarchy
    assert {"source": "L3.11", "target": "L7.67", "relation": "contains"} in hierarchy
    assert {"source": "L3.11", "target": "L2.8", "relation": "contains"} not in hierarchy
    assert result.materialization_counts["candidate_leaf_nodes_remapped"] == 1


def test_finalize_full_rebase_rejects_aggregate_across_multiple_candidate_parents(conn, gov_dir):
    sess = rs.start_session(conn, PROJECT_ID, governance_dir=gov_dir)
    (gov_dir / "code_a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (gov_dir / "code_c.py").write_text("def c():\n    return 3\n", encoding="utf-8")
    overlay = gov_dir / "graph.rebase.overlay.json"
    overlay.write_text(json.dumps({
        "session_id": sess.session_id,
        "project_id": PROJECT_ID,
        "nodes": {
            "L7.200": {
                "node_id": "L7.200",
                "title": "Cross Parent Aggregate",
                "primary": ["code_a.py", "code_c.py"],
            }
        },
    }), encoding="utf-8")
    candidate = gov_dir / "graph.rebase.candidate.json"
    nodes = [
        {"id": "L1.1", "title": "Runtime", "layer": "L1", "primary": []},
        {"id": "L3.1", "title": "A", "layer": "L3", "primary": []},
        {"id": "L3.2", "title": "C", "layer": "L3", "primary": []},
        {"id": "L7.1", "title": "A Leaf", "layer": "L7", "primary": ["code_a.py"]},
        {"id": "L7.2", "title": "C Leaf", "layer": "L7", "primary": ["code_c.py"]},
    ]
    links = [
        {"source": "L1.1", "target": "L3.1", "relation": "contains"},
        {"source": "L1.1", "target": "L3.2", "relation": "contains"},
        {"source": "L3.1", "target": "L7.1", "relation": "contains"},
        {"source": "L3.2", "target": "L7.2", "relation": "contains"},
    ]
    candidate.write_text(json.dumps({
        "version": 1,
        "hierarchy_graph": {
            "directed": True, "multigraph": False, "graph": {},
            "nodes": nodes, "links": links,
        },
        "deps_graph": {
            "directed": True, "multigraph": False, "graph": {},
            "nodes": nodes, "links": links,
        },
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="multiple hierarchy parents"):
        rs.finalize_session(
            conn, PROJECT_ID, sess.session_id,
            governance_dir=gov_dir, workspace_dir=gov_dir,
            candidate_graph_path=candidate, full_rebase=True,
        )
    assert rs.get_active_session(conn, PROJECT_ID).status == "finalize_failed"


def test_finalize_candidate_missing_overlay_primary_preserves_state(conn, gov_dir):
    sess = rs.start_session(conn, PROJECT_ID, governance_dir=gov_dir)
    (gov_dir / "unseen.py").write_text("x = 1\n", encoding="utf-8")
    overlay = gov_dir / "graph.rebase.overlay.json"
    overlay.write_text(json.dumps({
        "session_id": sess.session_id,
        "project_id": PROJECT_ID,
        "nodes": {
            "L7.100": {
                "node_id": "L7.100",
                "parent_layer": "L7",
                "title": "Unseen Leaf",
                "primary": ["unseen.py"],
            }
        },
    }), encoding="utf-8")
    candidate = gov_dir / "graph.rebase.candidate.json"
    candidate.write_text(json.dumps({
        "version": 1,
        "deps_graph": {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": [{"id": "L1.1", "title": "Runtime", "layer": "L1", "primary": []}],
            "links": [],
        },
    }), encoding="utf-8")
    graph_before = (gov_dir / "graph.json").read_bytes()
    overlay_before = overlay.read_bytes()

    with pytest.raises(ValueError, match="missing from candidate graph"):
        rs.finalize_session(
            conn, PROJECT_ID, sess.session_id,
            governance_dir=gov_dir, workspace_dir=gov_dir,
            candidate_graph_path=candidate, full_rebase=True,
        )

    assert overlay.read_bytes() == overlay_before
    assert (gov_dir / "graph.json").read_bytes() == graph_before
    failed = rs.get_active_session(conn, PROJECT_ID)
    assert failed.session_id == sess.session_id
    assert failed.status == "finalize_failed"
    assert "missing from candidate graph" in failed.finalize_error["message"]


def test_finalize_candidate_leaf_missing_from_overlay_preserves_state(conn, gov_dir):
    sess = rs.start_session(conn, PROJECT_ID, governance_dir=gov_dir)
    (gov_dir / "covered.py").write_text("x = 1\n", encoding="utf-8")
    (gov_dir / "missing.py").write_text("y = 2\n", encoding="utf-8")
    overlay = gov_dir / "graph.rebase.overlay.json"
    overlay.write_text(json.dumps({
        "session_id": sess.session_id,
        "project_id": PROJECT_ID,
        "nodes": {
            "L7.10": {
                "node_id": "L7.10",
                "parent_layer": "L7",
                "title": "Covered Leaf",
                "primary": ["covered.py"],
            }
        },
    }), encoding="utf-8")
    candidate = gov_dir / "graph.rebase.candidate.json"
    nodes = [
        {"id": "L1.1", "title": "Runtime", "layer": "L1", "primary": []},
        {"id": "L7.1", "title": "Covered", "layer": "L7", "primary": ["covered.py"]},
        {"id": "L7.2", "title": "Missing", "layer": "L7", "primary": ["missing.py"]},
    ]
    candidate.write_text(json.dumps({
        "version": 1,
        "deps_graph": {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": nodes,
            "links": [
                {"source": "L1.1", "target": "L7.1", "relation": "contains"},
                {"source": "L1.1", "target": "L7.2", "relation": "contains"},
            ],
        },
    }), encoding="utf-8")
    graph_before = (gov_dir / "graph.json").read_bytes()
    overlay_before = overlay.read_bytes()

    with pytest.raises(ValueError, match="candidate leaf primaries missing from overlay"):
        rs.finalize_session(
            conn, PROJECT_ID, sess.session_id,
            governance_dir=gov_dir, workspace_dir=gov_dir,
            candidate_graph_path=candidate, full_rebase=True,
        )

    assert overlay.read_bytes() == overlay_before
    assert (gov_dir / "graph.json").read_bytes() == graph_before
    failed = rs.get_active_session(conn, PROJECT_ID)
    assert failed.session_id == sess.session_id
    assert failed.status == "finalize_failed"
    assert "missing.py" in failed.finalize_error["message"]


def test_generate_final_doc_index_report_is_review_only(conn, gov_dir):
    sess = rs.start_session(conn, PROJECT_ID, governance_dir=gov_dir,
                            run_id="phase-z-doc-index")
    candidate = gov_dir / "graph.rebase.candidate.json"
    candidate.write_text(json.dumps({
        "deps_graph": {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": [{
                "id": "L7.1",
                "title": "Feature",
                "primary": ["feature.py"],
                "secondary": ["docs/feature.md"],
                "test": ["tests/test_feature.py"],
            }],
            "links": [],
        },
    }), encoding="utf-8")
    overlay = gov_dir / "graph.rebase.overlay.json"
    overlay.write_text(json.dumps({
        "nodes": {
            "L7.1": {
                "node_id": "L7.1",
                "title": "Feature",
                "primary": ["feature.py"],
                "secondary": ["docs/feature.md"],
                "test": ["tests/test_feature.py"],
            }
        }
    }), encoding="utf-8")
    graph_before = (gov_dir / "graph.json").read_bytes()
    conn.execute(
        "INSERT INTO reconcile_file_inventory "
        "(project_id, run_id, path, file_kind, language, sha256, scan_status, "
        "cluster_id, candidate_node_id, attached_to, reason, decision, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (PROJECT_ID, "phase-z-doc-index", "README.md", "doc", "markdown",
         "sha", "orphan", "", "", "", "index doc", "pending", "now"),
    )

    report = rs.generate_final_doc_index_report(
        conn, PROJECT_ID, sess.session_id,
        governance_dir=gov_dir,
        candidate_graph_path=candidate,
        overlay_path=overlay,
        output_dir=gov_dir,
    )

    assert report["summary"]["ready_for_signoff"] is True
    assert (gov_dir / "graph.rebase.doc-index.review.json").exists()
    assert (gov_dir / "graph.rebase.doc-index.review.md").exists()
    assert (gov_dir / "graph.json").read_bytes() == graph_before
    event = conn.execute(
        "SELECT event_type FROM chain_events WHERE event_type=?",
        ("reconcile.session.doc_index_generated",),
    ).fetchone()
    assert event is not None


def test_doc_index_falls_back_to_latest_inventory_when_session_run_empty(conn, gov_dir):
    sess = rs.start_session(conn, PROJECT_ID, governance_dir=gov_dir,
                            run_id="phase-z-session-run")
    candidate = gov_dir / "graph.rebase.candidate.json"
    candidate.write_text(json.dumps({
        "deps_graph": {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": [{
                "id": "L7.1",
                "title": "Feature",
                "primary": ["feature.py"],
                "secondary": ["docs/feature.md"],
                "test": ["tests/test_feature.py"],
            }],
            "links": [],
        },
    }), encoding="utf-8")
    overlay = gov_dir / "graph.rebase.overlay.json"
    overlay.write_text(json.dumps({
        "nodes": {
            "L7.1": {
                "node_id": "L7.1",
                "title": "Feature",
                "primary": ["feature.py"],
                "secondary": ["docs/feature.md"],
                "test": ["tests/test_feature.py"],
            }
        }
    }), encoding="utf-8")
    conn.execute(
        "INSERT INTO reconcile_file_inventory "
        "(project_id, run_id, path, file_kind, language, sha256, scan_status, "
        "cluster_id, candidate_node_id, attached_to, reason, decision, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (PROJECT_ID, "phase-z-latest-scan", "README.md", "doc", "markdown",
         "sha", "orphan", "", "", "", "index doc", "pending",
         "2026-05-05T00:00:00Z"),
    )

    report = rs.generate_final_doc_index_report(
        conn, PROJECT_ID, sess.session_id,
        governance_dir=gov_dir,
        candidate_graph_path=candidate,
        overlay_path=overlay,
        output_dir=gov_dir,
    )

    assert report["summary"]["index_doc_count"] == 1
    assert report["inventory"]["index_docs"][0]["path"] == "README.md"


def test_finalize_blocks_until_reconcile_clusters_complete(conn, gov_dir):
    from governance import reconcile_deferred_queue as q

    sess = rs.start_session(conn, PROJECT_ID, run_id="phase-z-block",
                            governance_dir=gov_dir)
    q.enqueue_or_lookup(PROJECT_ID, "fp-block", payload={},
                        run_id="phase-z-block", conn=conn)
    with pytest.raises(rs.SessionClusterGateError) as exc:
        rs.finalize_session(conn, PROJECT_ID, sess.session_id,
                            governance_dir=gov_dir)
    assert exc.value.summary["active_count"] == 1
    assert exc.value.summary["ready_for_finalize"] is False

    q.mark_terminal(PROJECT_ID, "fp-block", "resolved", "merged@abc",
                    conn=conn)
    result = rs.finalize_session(conn, PROJECT_ID, sess.session_id,
                                 governance_dir=gov_dir)
    assert result.status == "finalized"


def test_overlay_file_lifecycle(conn, gov_dir):
    overlay = gov_dir / "graph.rebase.overlay.json"
    # finalize path
    s1 = rs.start_session(conn, PROJECT_ID, governance_dir=gov_dir)
    assert overlay.exists()
    rs.finalize_session(conn, PROJECT_ID, s1.session_id, governance_dir=gov_dir)
    assert not overlay.exists()
    # rollback path
    s2 = rs.start_session(conn, PROJECT_ID, governance_dir=gov_dir)
    assert overlay.exists()
    rs.rollback_session(conn, PROJECT_ID, s2.session_id, governance_dir=gov_dir)
    assert not overlay.exists()


def test_snapshot_roundtrip_tar_gz(conn, gov_dir):
    _seed_node_state(conn, PROJECT_ID, n=4)
    sess = rs.start_session(conn, PROJECT_ID, governance_dir=gov_dir)
    graph_bytes_before = (gov_dir / "graph.json").read_bytes()
    snap_path = rs.capture_snapshot(conn, PROJECT_ID, sess.session_id,
                                    governance_dir=gov_dir)
    assert snap_path.exists()
    assert snap_path.name == f"{sess.session_id}.tar.gz"

    with tarfile.open(str(snap_path), "r:gz") as tar:
        names = set(tar.getnames())
        assert {"graph.json", "node_state.sql", "manifest.json"}.issubset(names)
        manifest = json.loads(tar.extractfile("manifest.json").read())
        assert manifest["project_id"] == PROJECT_ID
        assert manifest["session_id"] == sess.session_id
        assert manifest["node_count"] == 4

    rows_before = conn.execute(
        "SELECT node_id, verify_status, evidence_json, version FROM node_state "
        "WHERE project_id = ? ORDER BY node_id", (PROJECT_ID,)).fetchall()

    # Mutate state then restore.
    (gov_dir / "graph.json").write_text("CORRUPTED")
    conn.execute("DELETE FROM node_state WHERE project_id = ?", (PROJECT_ID,))
    conn.commit()
    rs.restore_snapshot(conn, PROJECT_ID, sess.session_id, governance_dir=gov_dir)
    assert (gov_dir / "graph.json").read_bytes() == graph_bytes_before
    rows_after = conn.execute(
        "SELECT node_id, verify_status, evidence_json, version FROM node_state "
        "WHERE project_id = ? ORDER BY node_id", (PROJECT_ID,)).fetchall()
    assert [tuple(r) for r in rows_after] == [tuple(r) for r in rows_before]


def test_rollback_restores_graph_and_node_state(conn, gov_dir):
    _seed_node_state(conn, PROJECT_ID, n=3)
    sess = rs.start_session(conn, PROJECT_ID, governance_dir=gov_dir)
    rs.capture_snapshot(conn, PROJECT_ID, sess.session_id, governance_dir=gov_dir)
    graph_before = (gov_dir / "graph.json").read_bytes()
    rows_before = conn.execute(
        "SELECT node_id, verify_status FROM node_state WHERE project_id=? "
        "ORDER BY node_id", (PROJECT_ID,)).fetchall()

    # Mutate.
    (gov_dir / "graph.json").write_text("DIRTY")
    conn.execute("UPDATE node_state SET verify_status='qa_pass' WHERE project_id=?",
                 (PROJECT_ID,))
    conn.commit()

    rs.rollback_session(conn, PROJECT_ID, sess.session_id, governance_dir=gov_dir,
                        restore_graph_snapshot=True)
    assert (gov_dir / "graph.json").read_bytes() == graph_before
    rows_after = conn.execute(
        "SELECT node_id, verify_status FROM node_state WHERE project_id=? "
        "ORDER BY node_id", (PROJECT_ID,)).fetchall()
    assert [tuple(r) for r in rows_after] == [tuple(r) for r in rows_before]


def test_rollback_default_does_not_restore_graph_snapshot(conn, gov_dir):
    sess = rs.start_session(conn, PROJECT_ID, governance_dir=gov_dir)
    rs.capture_snapshot(conn, PROJECT_ID, sess.session_id, governance_dir=gov_dir)
    (gov_dir / "graph.json").write_text("MAINLINE CHANGE AFTER RECONCILE START")

    rs.rollback_session(conn, PROJECT_ID, sess.session_id, governance_dir=gov_dir)

    assert (gov_dir / "graph.json").read_text() == "MAINLINE CHANGE AFTER RECONCILE START"
    row = conn.execute(
        "SELECT status FROM reconcile_sessions WHERE project_id=? AND session_id=?",
        (PROJECT_ID, sess.session_id),
    ).fetchone()
    assert row[0] == "rolled_back"


def test_full_rebase_precondition_explicit_force_drop(conn, gov_dir):
    with pytest.raises(ValueError):
        rs.start_session(conn, PROJECT_ID, full_rebase=True, governance_dir=gov_dir)
    # Active session may have been left behind by a partial start. None should exist
    # because start_session validates the precondition before any DB write.
    assert rs.get_active_session(conn, PROJECT_ID) is None
    sess = rs.start_session(conn, PROJECT_ID, full_rebase=True,
                            dropped_cluster_fingerprints=["fp-a", "fp-b"],
                            governance_dir=gov_dir)
    assert sess.status == "active"


def test_module_import_is_side_effect_free(tmp_path: Path):
    # Import in a subprocess with cwd=tmp_path; assert no files appear.
    before = set(p.name for p in tmp_path.iterdir())
    code = (
        "import sys, json, os, sqlite3\n"
        f"sys.path.insert(0, {str(_AGENT_DIR)!r})\n"
        # Trace any sqlite writes attempted at import time.
        "writes = []\n"
        "_orig = sqlite3.connect\n"
        "def _no_connect(*a, **k):\n"
        "    writes.append(('connect', a, k))\n"
        "    raise RuntimeError('connect at import time')\n"
        "sqlite3.connect = _no_connect\n"
        "import governance.reconcile_session as m\n"
        "assert hasattr(m, 'start_session')\n"
        "print(json.dumps({'writes': writes}))\n"
    )
    res = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path),
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout.strip().splitlines()[-1])
    assert payload["writes"] == []
    after = set(p.name for p in tmp_path.iterdir())
    assert before == after, f"new files appeared: {after - before}"


def test_is_gate_bypassed_helper(conn, gov_dir):
    sess = rs.start_session(conn, PROJECT_ID, bypass_gates=["dep-cycle"],
                            governance_dir=gov_dir)
    assert rs.is_gate_bypassed(sess, "dep-cycle") is True
    assert rs.is_gate_bypassed(sess, "doc-coverage") is False
    assert rs.is_gate_bypassed(None, "dep-cycle") is False
