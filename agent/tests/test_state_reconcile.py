from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent.governance import graph_snapshot_store as store
from agent.governance.db import _ensure_schema
from agent.governance.state_reconcile import (
    run_backfill_escape_hatch,
    run_pending_scope_reconcile_candidate,
    run_state_only_full_reconcile,
)


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


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return (result.stdout or "").strip()


def _init_git(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")


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
    assert result["graph_stats"]["edges"] > 0
    assert result["index_counts"]["nodes"] == result["graph_stats"]["nodes"]
    assert result["index_counts"]["edges"] == result["graph_stats"]["edges"]
    assert result["governance_index"]["index_scope"] == "candidate_snapshot"
    assert result["governance_index"]["feature_count"] > 0
    assert result["semantic_enrichment"]["feature_count"] == result["governance_index"]["feature_count"]
    assert Path(result["semantic_enrichment"]["semantic_index_path"]).exists()
    assert Path(result["semantic_enrichment"]["review_report_path"]).exists()
    assert Path(result["governance_index"]["artifacts"]["symbol_index_path"]).exists()
    assert Path(result["governance_index"]["artifacts"]["doc_index_path"]).exists()
    assert Path(result["governance_index"]["artifacts"]["feature_index_path"]).exists()
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
    assert notes["governance_index"]["feature_count"] == result["governance_index"]["feature_count"]
    assert notes["semantic_enrichment"]["feature_count"] == result["semantic_enrichment"]["feature_count"]


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


def test_pending_scope_materializer_binds_pending_rows_to_scope_candidate(
    conn,
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "project"
    files = _write_project(project)
    before = {str(path): _file_sha(path) for path in files}

    old = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="imported-old-pending",
        commit_sha="old",
        snapshot_kind="imported",
    )
    store.activate_graph_snapshot(conn, PID, old["snapshot_id"])
    for commit in ("a1", "a2", "head"):
        store.queue_pending_scope_reconcile(
            conn,
            PID,
            commit_sha=commit,
            parent_commit_sha="old",
            evidence={"source": "test"},
        )
    monkeypatch.setattr("agent.governance.state_reconcile._git_commit", lambda *_a, **_k: "head")

    result = run_pending_scope_reconcile_candidate(
        conn,
        PID,
        project,
        run_id="scope-reconcile-head-test",
        snapshot_id="scope-head-test",
    )

    assert result["ok"] is True
    assert result["snapshot_id"] == "scope-head-test"
    assert result["snapshot_status"] == store.SNAPSHOT_STATUS_CANDIDATE
    assert result["covered_commit_shas"] == ["a1", "a2", "head"]
    assert result["pending_rows_bound"] == 3
    assert result["active_snapshot_id"] == "imported-old-pending"
    assert result["graph_stats"]["nodes"] > 0
    assert result["index_counts"]["edges"] == result["graph_stats"]["edges"]
    assert result["governance_index"]["feature_count"] > 0
    assert result["semantic_enrichment"]["feature_count"] == result["governance_index"]["feature_count"]
    assert Path(result["semantic_enrichment"]["semantic_index_path"]).exists()
    assert result["scope_file_delta"]["strategy"] == "full_scan_with_incremental_file_delta"
    assert "impacted_file_count" in result["scope_file_delta"]
    assert store.get_active_graph_snapshot(conn, PID)["snapshot_id"] == "imported-old-pending"

    rows = conn.execute(
        """
        SELECT commit_sha, status, snapshot_id FROM pending_scope_reconcile
        WHERE project_id=? ORDER BY queued_at, commit_sha
        """,
        (PID,),
    ).fetchall()
    assert [row["status"] for row in rows] == [store.PENDING_STATUS_RUNNING] * 3
    assert {row["snapshot_id"] for row in rows} == {"scope-head-test"}

    notes = conn.execute(
        "SELECT notes FROM graph_snapshots WHERE project_id=? AND snapshot_id=?",
        (PID, "scope-head-test"),
    ).fetchone()["notes"]
    pending_notes = json.loads(notes)["pending_scope_reconcile"]
    assert pending_notes["covered_commit_count"] == 3
    assert pending_notes["scope_file_delta"]["strategy"] == "full_scan_with_incremental_file_delta"

    after = {str(path): _file_sha(path) for path in files}
    assert after == before


def test_pending_scope_materializer_requires_current_head(conn, tmp_path, monkeypatch):
    project = tmp_path / "project"
    _write_project(project)
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="target",
        parent_commit_sha="old",
        evidence={"source": "test"},
    )
    monkeypatch.setattr("agent.governance.state_reconcile._git_commit", lambda *_a, **_k: "head")

    with pytest.raises(ValueError):
        run_pending_scope_reconcile_candidate(
            conn,
            PID,
            project,
            target_commit_sha="target",
            run_id="scope-reconcile-target-test",
        )


def test_pending_scope_materializer_records_changed_file_delta(conn, tmp_path):
    project = tmp_path / "project"
    _write_project(project)
    _init_git(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "base")
    base_commit = _git(project, "rev-parse", "HEAD")

    base = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-base-delta-test",
        commit_sha=base_commit,
        snapshot_id="full-base-delta-test",
        created_by="test",
        activate=True,
    )
    assert base["ok"] is True

    service = project / "agent" / "service.py"
    service.write_text(
        "def service_entry():\n"
        "    return helper()\n\n"
        "def helper():\n"
        "    return 'changed'\n",
        encoding="utf-8",
    )
    _git(project, "add", "agent/service.py")
    _git(project, "commit", "-m", "change service")
    head_commit = _git(project, "rev-parse", "HEAD")
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha=head_commit,
        parent_commit_sha=base_commit,
        evidence={"source": "test"},
    )

    result = run_pending_scope_reconcile_candidate(
        conn,
        PID,
        project,
        target_commit_sha=head_commit,
        run_id="scope-delta-test",
        snapshot_id="scope-delta-test",
    )

    assert result["ok"] is True
    delta = result["scope_file_delta"]
    assert delta["strategy"] == "full_scan_with_incremental_file_delta"
    assert delta["changed_files"] == ["agent/service.py"]
    assert "agent/service.py" in delta["hash_changed_files"]
    assert "agent/service.py" in delta["impacted_files"]


def test_backfill_escape_hatch_activates_full_snapshot_and_waives_pending(
    conn,
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "project"
    files = _write_project(project)
    before = {str(path): _file_sha(path) for path in files}

    old = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="imported-old-backfill",
        commit_sha="old",
        snapshot_kind="imported",
    )
    store.activate_graph_snapshot(conn, PID, old["snapshot_id"])
    for commit in ("a1", "head"):
        store.queue_pending_scope_reconcile(
            conn,
            PID,
            commit_sha=commit,
            parent_commit_sha="old",
            evidence={"source": "test"},
        )
    monkeypatch.setattr("agent.governance.state_reconcile._git_commit", lambda *_a, **_k: "head")

    result = run_backfill_escape_hatch(
        conn,
        PID,
        project,
        target_commit_sha="head",
        run_id="backfill-escape-head-test",
        snapshot_id="full-head-backfill",
        created_by="test",
        reason="scope materializer bug",
        expected_old_snapshot_id=old["snapshot_id"],
    )

    assert result["ok"] is True
    assert result["snapshot_id"] == "full-head-backfill"
    assert result["activation"]["activation"]["previous_snapshot_id"] == old["snapshot_id"]
    assert result["pending_scope_waiver"]["waived_count"] == 2
    active = store.get_active_graph_snapshot(conn, PID)
    assert active["snapshot_id"] == "full-head-backfill"
    rows = conn.execute(
        """
        SELECT status, snapshot_id FROM pending_scope_reconcile
        WHERE project_id=? ORDER BY commit_sha
        """,
        (PID,),
    ).fetchall()
    assert [row["status"] for row in rows] == [store.PENDING_STATUS_WAIVED] * 2
    assert {row["snapshot_id"] for row in rows} == {"full-head-backfill"}

    after = {str(path): _file_sha(path) for path in files}
    assert after == before
