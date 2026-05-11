from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent.governance import graph_events
from agent.governance import state_reconcile
from agent.governance import graph_snapshot_store as store
from agent.governance.db import _ensure_schema
from agent.governance.state_reconcile import (
    _build_scope_file_delta,
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


def test_scope_file_delta_respects_current_gitignore(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _init_git(project)
    _write(project / ".gitignore", "docs/dev/\n")
    _write(project / "agent" / "service.py", "def run():\n    return 1\n")
    _git(project, "add", ".gitignore", "agent/service.py")
    _git(project, "commit", "-m", "initial")

    delta = _build_scope_file_delta(
        project_root=project,
        old_rows=[
            {"path": "agent/service.py", "file_hash": "sha256:old", "scan_status": "clustered"},
            {"path": "docs/dev/proposal.md", "file_hash": "sha256:old", "scan_status": "orphan"},
        ],
        new_rows=[
            {"path": "agent/service.py", "file_hash": "sha256:new", "scan_status": "clustered"},
        ],
        changed_files=["agent/service.py", "docs/dev/proposal.md"],
    )

    assert delta["changed_files"] == ["agent/service.py"]
    assert delta["removed_files"] == []
    assert delta["hash_changed_files"] == ["agent/service.py"]
    assert delta["impacted_files"] == ["agent/service.py"]


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
    assert result["trace"]["status"] == "ok"
    assert result["trace"]["step_count"] >= 7
    trace_dir = Path(result["trace"]["steps"][0]["input"]["path"]).parents[2]
    assert (trace_dir / "summary.json").exists()
    assert (trace_dir / "steps" / "001-run-input" / "input.json").exists()
    assert (trace_dir / "steps" / "002-build-graph-v2" / "output.json").exists()
    assert result["semantic_enrichment"]["feature_payload_input_count"] > 0
    assert Path(result["semantic_enrichment"]["feature_payload_input_dir"]).exists()
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
    assert Path(notes["trace"]["summary_path"]).exists()
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
        semantic_ai_batch_size=10,
        semantic_ai_input_mode="feature",
        semantic_dynamic_graph_state=True,
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
    assert result["semantic_enrichment"]["ai_input_mode"] == "feature"
    assert result["semantic_enrichment"]["dynamic_semantic_graph_state"] is True
    assert result["semantic_enrichment"]["requested_ai_batch_size"] == 10
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
    selector = result["semantic_enrichment"]["semantic_selector"]
    assert selector["scope"] == "changed"
    assert selector["changed_paths"] == ["agent/service.py"]
    assert selector["match_mode"] == "primary"
    assert result["scope_graph_events"]["by_type"]["file_hash_changed"] == 1
    events = graph_events.list_events(
        conn,
        PID,
        "scope-delta-test",
        statuses=[graph_events.EVENT_STATUS_OBSERVED],
        event_types=["file_hash_changed"],
    )
    assert len(events) == 1
    assert events[0]["event_kind"] == "scope_reconcile"
    assert events[0]["target_type"] == "node"
    assert events[0]["target_commit"] == head_commit
    assert events[0]["payload"]["files"] == ["agent/service.py"]


def test_scope_graph_events_emit_secondary_doc_hash_changes(conn):
    doc_path = "docs/governance/manual-fix-sop.md"
    old_graph = {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.1",
                    "layer": "L7",
                    "title": "Feature Node",
                    "primary": ["agent/service.py"],
                    "secondary": [doc_path],
                    "metadata": {
                        "stable_node_key": "feature-node",
                        "feature_hash": "sha256:old-feature",
                        "file_hashes": {
                            "agent/service.py": "sha256:service",
                            doc_path: "sha256:old-doc",
                        },
                    },
                }
            ],
            "edges": [],
        }
    }
    new_graph = {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.1",
                    "layer": "L7",
                    "title": "Feature Node",
                    "primary": ["agent/service.py"],
                    "secondary": [doc_path],
                    "metadata": {
                        "stable_node_key": "feature-node",
                        "feature_hash": "sha256:new-feature",
                        "file_hashes": {
                            "agent/service.py": "sha256:service",
                            doc_path: "sha256:new-doc",
                        },
                    },
                }
            ],
            "edges": [],
        }
    }

    summary = state_reconcile._emit_scope_graph_events(
        conn,
        PID,
        old_snapshot_id="old-doc-snapshot",
        new_snapshot_id="new-doc-snapshot",
        old_graph_json=old_graph,
        new_graph_json=new_graph,
        scope_file_delta={"hash_changed_files": [doc_path]},
        baseline_commit="old",
        target_commit="head",
        created_by="test",
    )

    assert summary["by_type"]["file_hash_changed"] == 1
    events = graph_events.list_events(
        conn,
        PID,
        "new-doc-snapshot",
        statuses=[graph_events.EVENT_STATUS_OBSERVED],
        event_types=["file_hash_changed"],
    )
    assert len(events) == 1
    assert events[0]["payload"] == {"node_id": "L7.1", "files": [doc_path], "file_role": "secondary"}
    assert events[0]["file_hashes"] == {doc_path: "sha256:new-doc"}


def test_pending_scope_materializer_does_not_ai_select_test_only_changes(conn, tmp_path):
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
        run_id="full-base-test-only-change",
        commit_sha=base_commit,
        snapshot_id="full-base-test-only-change",
        created_by="test",
        activate=True,
    )
    assert base["ok"] is True

    test_file = project / "agent" / "tests" / "test_service.py"
    test_file.write_text(
        "from agent.service import service_entry\n\n"
        "def test_service_entry():\n"
        "    assert service_entry() == 'ok'\n\n"
        "def test_service_entry_again():\n"
        "    assert service_entry() == 'ok'\n",
        encoding="utf-8",
    )
    _git(project, "add", "agent/tests/test_service.py")
    _git(project, "commit", "-m", "change service test")
    head_commit = _git(project, "rev-parse", "HEAD")
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha=head_commit,
        parent_commit_sha=base_commit,
        evidence={"source": "test"},
    )
    seen_nodes: list[str] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        seen_nodes.append(payload["feature"]["node_id"])
        return {"feature_name": f"AI {payload['feature']['node_id']}"}

    result = run_pending_scope_reconcile_candidate(
        conn,
        PID,
        project,
        target_commit_sha=head_commit,
        run_id="scope-test-only-delta",
        snapshot_id="scope-test-only-delta",
        semantic_use_ai=True,
        semantic_ai_call=fake_ai,
    )

    assert result["ok"] is True
    assert result["scope_file_delta"]["changed_files"] == ["agent/tests/test_service.py"]
    selector = result["semantic_enrichment"]["semantic_selector"]
    assert selector["scope"] == "changed"
    assert selector["changed_paths"] == ["agent/tests/test_service.py"]
    assert selector["match_mode"] == "primary"
    assert result["semantic_enrichment"]["ai_selected_count"] == 0
    assert seen_nodes == []
    assert result["scope_graph_events"]["by_type"]["file_hash_changed"] == 1
    events = graph_events.list_events(
        conn,
        PID,
        "scope-test-only-delta",
        statuses=[graph_events.EVENT_STATUS_OBSERVED],
        event_types=["file_hash_changed"],
    )
    assert len(events) == 1
    assert events[0]["payload"]["files"] == ["agent/tests/test_service.py"]
    assert events[0]["payload"]["file_role"] == "test"


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


def test_update_pending_scope_candidate_materializes_when_activated(conn):
    """OPT-BACKLOG-PENDING-SCOPE-TRANSITION-MISSING regression: when the
    candidate snapshot was activated, pending_scope_reconcile rows must
    transition from queued → materialized, not be left in `running`."""
    store.ensure_schema(conn)
    commit_a = "a" * 40
    commit_b = "b" * 40
    conn.execute(
        """
        INSERT INTO pending_scope_reconcile
          (project_id, commit_sha, parent_commit_sha, queued_at, status,
           retry_count, snapshot_id, evidence_json)
        VALUES (?, ?, '', '2026-01-01T00:00:00Z', 'queued', 0, '', '{}'),
               (?, ?, '', '2026-01-02T00:00:00Z', 'queued', 0, '', '{}')
        """,
        (PID, commit_a, PID, commit_b),
    )
    conn.commit()

    updated = state_reconcile._update_pending_scope_candidate(
        conn, PID,
        covered_commit_shas=[commit_a, commit_b],
        snapshot_id="scope-test-1",
        target_commit_sha=commit_b,
        run_id="run-1",
        activated=True,
    )
    assert updated == 2
    rows = conn.execute(
        "SELECT commit_sha, status, snapshot_id FROM pending_scope_reconcile WHERE project_id=? ORDER BY commit_sha",
        (PID,),
    ).fetchall()
    statuses = [r["status"] for r in rows]
    assert statuses == [store.PENDING_STATUS_MATERIALIZED, store.PENDING_STATUS_MATERIALIZED]
    assert all(r["snapshot_id"] == "scope-test-1" for r in rows)


def test_update_pending_scope_candidate_keeps_running_when_not_activated(conn):
    """When the candidate was built but NOT activated (activate=False), the
    pending row stays in `running` so the next cycle can pick it back up."""
    store.ensure_schema(conn)
    commit_a = "c" * 40
    conn.execute(
        """
        INSERT INTO pending_scope_reconcile
          (project_id, commit_sha, parent_commit_sha, queued_at, status,
           retry_count, snapshot_id, evidence_json)
        VALUES (?, ?, '', '2026-01-01T00:00:00Z', 'queued', 0, '', '{}')
        """,
        (PID, commit_a),
    )
    conn.commit()

    updated = state_reconcile._update_pending_scope_candidate(
        conn, PID,
        covered_commit_shas=[commit_a],
        snapshot_id="scope-test-2",
        target_commit_sha=commit_a,
        run_id="run-2",
        activated=False,
    )
    assert updated == 1
    row = conn.execute(
        "SELECT status FROM pending_scope_reconcile WHERE project_id=? AND commit_sha=?",
        (PID, commit_a),
    ).fetchone()
    assert row["status"] == store.PENDING_STATUS_RUNNING
