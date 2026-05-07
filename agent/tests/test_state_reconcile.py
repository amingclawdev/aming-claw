from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from agent.governance import graph_snapshot_store as store
from agent.governance.db import _ensure_schema
from agent.governance.state_reconcile import run_state_only_full_reconcile


PID = "state-reconcile-test"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    yield c
    c.close()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_project(root: Path) -> list[Path]:
    files = [
        root / "agent" / "service.py",
        root / "agent" / "tests" / "test_service.py",
        root / "README.md",
    ]
    _write(
        files[0],
        "def service_entry():\n"
        "    return helper()\n\n"
        "def helper():\n"
        "    return 'ok'\n",
    )
    _write(
        files[1],
        "from agent.service import service_entry\n\n"
        "def test_service_entry():\n"
        "    assert service_entry() == 'ok'\n",
    )
    _write(files[2], "# Service\n\nState-only reconcile should not edit docs.\n")
    return files


def test_state_only_full_reconcile_creates_candidate_snapshot_without_project_mutation(conn, tmp_path):
    project = tmp_path / "project"
    files = _write_project(project)
    before = {str(path): _file_sha(path) for path in files}

    result = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-reconcile-abc1234-test",
        commit_sha="abc1234",
        snapshot_id="full-abc1234-test",
        created_by="test",
    )

    assert result["ok"] is True
    assert result["snapshot_id"] == "full-abc1234-test"
    assert result["snapshot_status"] == store.SNAPSHOT_STATUS_CANDIDATE
    assert result["graph_stats"]["nodes"] > 0
    assert result["index_counts"]["nodes"] == result["graph_stats"]["nodes"]
    assert result["index_counts"]["edges"] == result["graph_stats"]["edges"]
    assert result["file_inventory_count"] > 0
    assert Path(result["snapshot_path"]).exists()
    assert Path(result["phase_report_path"]).exists()
    assert store.get_active_graph_snapshot(conn, PID) is None

    after = {str(path): _file_sha(path) for path in files}
    assert after == before

    snapshot_row = conn.execute(
        "SELECT status, commit_sha, notes FROM graph_snapshots WHERE project_id=? AND snapshot_id=?",
        (PID, "full-abc1234-test"),
    ).fetchone()
    assert snapshot_row["status"] == store.SNAPSHOT_STATUS_CANDIDATE
    assert snapshot_row["commit_sha"] == "abc1234"
    notes = json.loads(snapshot_row["notes"])
    assert notes["state_only"] is True
    assert notes["feature_cluster_count"] >= 1


def test_state_only_full_reconcile_can_activate_with_explicit_signoff(conn, tmp_path):
    project = tmp_path / "project"
    _write_project(project)

    first = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="imported-old-test",
        commit_sha="old",
        snapshot_kind="imported",
    )
    store.activate_graph_snapshot(conn, PID, first["snapshot_id"])

    result = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-reconcile-new-test",
        commit_sha="new",
        snapshot_id="full-new-test",
        created_by="test",
        activate=True,
        expected_old_snapshot_id="imported-old-test",
    )

    assert result["ok"] is True
    assert result["activation"]["previous_snapshot_id"] == "imported-old-test"
    active = store.get_active_graph_snapshot(conn, PID)
    assert active["snapshot_id"] == "full-new-test"
    old_status = conn.execute(
        "SELECT status FROM graph_snapshots WHERE project_id=? AND snapshot_id=?",
        (PID, "imported-old-test"),
    ).fetchone()
    assert old_status["status"] == store.SNAPSHOT_STATUS_SUPERSEDED
