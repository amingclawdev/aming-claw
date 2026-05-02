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
    (d / "graph.json").write_text('{"nodes": [{"id": "n1"}]}')
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
                            governance_dir=gov_dir)
    assert sess.project_id == PROJECT_ID
    assert sess.status == "active"
    assert sess.bypass_gates == ["gate-a", "gate-b"]
    row = conn.execute(
        "SELECT status, bypass_gates_json, started_by FROM reconcile_sessions "
        "WHERE project_id=? AND session_id=?",
        (PROJECT_ID, sess.session_id)).fetchone()
    assert row[0] == "active"
    assert json.loads(row[1]) == ["gate-a", "gate-b"]
    assert row[2] == "tester"


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
    rs.finalize_session(conn, PROJECT_ID, sess.session_id, governance_dir=gov_dir)
    assert not overlay.exists()
    assert (gov_dir / "graph.rebase.overlay.json.bak").exists()


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

    rs.rollback_session(conn, PROJECT_ID, sess.session_id, governance_dir=gov_dir)
    assert (gov_dir / "graph.json").read_bytes() == graph_before
    rows_after = conn.execute(
        "SELECT node_id, verify_status FROM node_state WHERE project_id=? "
        "ORDER BY node_id", (PROJECT_ID,)).fetchall()
    assert [tuple(r) for r in rows_after] == [tuple(r) for r in rows_before]


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
