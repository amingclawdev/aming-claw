from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent.governance import graph_snapshot_store as store
from agent.governance.db import _ensure_schema
from agent.governance.state_reconcile import (
    _read_snapshot_graph,
    _snapshot_inventory_rows,
    normalize_reconcile_snapshot_for_comparison,
    run_pending_scope_reconcile_candidate,
    run_state_only_full_reconcile,
)


PID = "scope-consistency-test"


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


def _write_project(root: Path) -> None:
    _write(
        root / "agent" / "service.py",
        "def service_entry():\n"
        "    return helper()\n\n"
        "def helper():\n"
        "    return 'ok'\n",
    )
    _write(
        root / "agent" / "tests" / "test_service.py",
        "from agent.service import service_entry\n\n"
        "def test_service_entry():\n"
        "    assert service_entry() == 'ok'\n",
    )
    _write(root / "README.md", "# Service\n\nScope consistency fixture.\n")


def _write_call_free_project(root: Path, *, result: str = "ok") -> None:
    _write(
        root / "agent" / "service.py",
        "def service_entry():\n"
        f"    return {result!r}\n",
    )
    _write(
        root / "agent" / "tests" / "test_service.py",
        "from agent.service import service_entry\n\n"
        "def test_service_entry():\n"
        f"    assert service_entry() == {result!r}\n",
    )
    _write(root / "README.md", "# Service\n\nCall-free source consistency fixture.\n")


def _normalized_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> dict:
    graph = _read_snapshot_graph(PID, snapshot_id)
    inventory = _snapshot_inventory_rows(conn, PID, snapshot_id)
    return normalize_reconcile_snapshot_for_comparison(graph, file_inventory=inventory)


def _node_ids_by_primary_file(graph: dict) -> dict[str, str]:
    nodes = ((graph.get("deps_graph") or {}).get("nodes") or [])
    mapping: dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or node.get("node_id") or "")
        primary_files = list(node.get("primary") or []) + list(node.get("primary_files") or [])
        for path in primary_files:
            if node_id and path:
                mapping[str(path).replace("\\", "/").strip("/")] = node_id
    return mapping


def _nodes_by_module(graph: dict) -> dict[str, dict]:
    nodes = ((graph.get("deps_graph") or {}).get("nodes") or [])
    out: dict[str, dict] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        module = str(metadata.get("module") or "")
        if module:
            out[module] = node
    return out


def _rewrite_inventory_status(snapshot_id: str, path: str, *, scan_status: str, graph_status: str) -> None:
    inventory_path = store.snapshot_companion_dir(PID, snapshot_id) / "file_inventory.json"
    rows = json.loads(inventory_path.read_text(encoding="utf-8"))
    for row in rows:
        if isinstance(row, dict) and row.get("path") == path:
            row["scan_status"] = scan_status
            row["graph_status"] = graph_status
            break
    else:
        raise AssertionError(f"inventory path not found: {path}")
    inventory_path.write_text(
        json.dumps(rows, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def test_scope_reconcile_output_matches_full_rebuild_for_same_final_state(conn, tmp_path):
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
        run_id="full-base-consistency",
        commit_sha=base_commit,
        snapshot_id="full-base-consistency",
        created_by="test",
        activate=True,
        semantic_enrich=False,
    )
    assert base["ok"] is True

    _write(
        project / "README.md",
        "# Service\n\nScope consistency fixture with a documentation update.\n",
    )
    _git(project, "add", "README.md")
    _git(project, "commit", "-m", "change docs")
    head_commit = _git(project, "rev-parse", "HEAD")
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha=head_commit,
        parent_commit_sha=base_commit,
        evidence={"source": "test"},
    )

    scope = run_pending_scope_reconcile_candidate(
        conn,
        PID,
        project,
        target_commit_sha=head_commit,
        run_id="scope-head-consistency",
        snapshot_id="scope-head-consistency",
        created_by="test",
        semantic_enrich=False,
    )
    assert scope["ok"] is True
    assert scope["scope_file_delta"]["strategy"] == "incremental_graph_delta"
    assert scope["scope_file_delta"]["graph_delta_mode"] == "metadata_only"
    assert scope["scope_file_delta"]["changed_files"] == ["README.md"]
    assert scope["scope_graph_delta"]["strategy"] == "incremental_graph_delta"
    assert scope["scope_graph_delta"]["mode"] == "metadata_only"
    assert scope["scope_graph_delta"]["added_nodes"] == []
    assert scope["scope_graph_delta"]["removed_nodes"] == []
    assert scope["scope_graph_delta"]["added_edges"] == []
    assert scope["scope_graph_delta"]["removed_edges"] == []

    full = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-head-consistency",
        commit_sha=head_commit,
        snapshot_id="full-head-consistency",
        created_by="test",
        semantic_enrich=False,
    )
    assert full["ok"] is True

    assert _normalized_snapshot(conn, "scope-head-consistency") == _normalized_snapshot(
        conn,
        "full-head-consistency",
    )

    base_node_ids = _node_ids_by_primary_file(_read_snapshot_graph(PID, "full-base-consistency"))
    scope_node_ids = _node_ids_by_primary_file(_read_snapshot_graph(PID, "scope-head-consistency"))
    assert scope_node_ids["agent/service.py"] == base_node_ids["agent/service.py"]


def test_scope_reconcile_source_hash_only_matches_full_rebuild_for_same_final_state(conn, tmp_path):
    project = tmp_path / "project"
    _write_call_free_project(project)
    _init_git(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "base")
    base_commit = _git(project, "rev-parse", "HEAD")

    base = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-source-base-consistency",
        commit_sha=base_commit,
        snapshot_id="full-source-base-consistency",
        created_by="test",
        activate=True,
        semantic_enrich=False,
    )
    assert base["ok"] is True

    _write(
        project / "agent" / "service.py",
        "def service_entry():\n"
        "    return 'changed'\n",
    )
    _git(project, "add", "agent/service.py")
    _git(project, "commit", "-m", "change source body")
    head_commit = _git(project, "rev-parse", "HEAD")
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha=head_commit,
        parent_commit_sha=base_commit,
        evidence={"source": "test"},
    )

    scope = run_pending_scope_reconcile_candidate(
        conn,
        PID,
        project,
        target_commit_sha=head_commit,
        run_id="scope-source-head-consistency",
        snapshot_id="scope-source-head-consistency",
        created_by="test",
        semantic_enrich=False,
    )
    assert scope["ok"] is True
    assert scope["scope_file_delta"]["strategy"] == "incremental_graph_delta"
    assert scope["scope_file_delta"]["graph_delta_mode"] == "source_hash_only"
    assert scope["scope_file_delta"]["changed_files"] == ["agent/service.py"]
    assert scope["scope_graph_delta"]["mode"] == "source_hash_only"
    assert scope["scope_graph_delta"]["added_nodes"] == []
    assert scope["scope_graph_delta"]["removed_nodes"] == []
    assert scope["scope_graph_delta"]["added_edges"] == []
    assert scope["scope_graph_delta"]["removed_edges"] == []

    full = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-source-head-consistency",
        commit_sha=head_commit,
        snapshot_id="full-source-head-consistency",
        created_by="test",
        semantic_enrich=False,
    )
    assert full["ok"] is True

    assert _normalized_snapshot(conn, "scope-source-head-consistency") == _normalized_snapshot(
        conn,
        "full-source-head-consistency",
    )

    base_node_ids = _node_ids_by_primary_file(_read_snapshot_graph(PID, "full-source-base-consistency"))
    scope_node_ids = _node_ids_by_primary_file(_read_snapshot_graph(PID, "scope-source-head-consistency"))
    assert scope_node_ids["agent/service.py"] == base_node_ids["agent/service.py"]


def test_scope_reconcile_test_fanin_incremental_matches_full_rebuild(conn, tmp_path):
    project = tmp_path / "project"
    _write_project(project)
    _write(
        project / "agent" / "other.py",
        "def other_entry():\n"
        "    return 'ok'\n",
    )
    _write(
        project / "agent" / "tests" / "test_integration.py",
        "from agent.service import service_entry\n\n"
        "def test_integration():\n"
        "    assert service_entry() == 'ok'\n",
    )
    _init_git(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "base")
    base_commit = _git(project, "rev-parse", "HEAD")

    base = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-test-fanin-base-consistency",
        commit_sha=base_commit,
        snapshot_id="full-test-fanin-base-consistency",
        created_by="test",
        activate=True,
        semantic_enrich=False,
    )
    assert base["ok"] is True

    _write(
        project / "agent" / "tests" / "test_integration.py",
        "from agent.other import other_entry\n\n"
        "def test_integration():\n"
        "    assert other_entry() == 'ok'\n",
    )
    _git(project, "add", "agent/tests/test_integration.py")
    _git(project, "commit", "-m", "move integration test fanin")
    head_commit = _git(project, "rev-parse", "HEAD")
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha=head_commit,
        parent_commit_sha=base_commit,
        evidence={"source": "test"},
    )

    scope = run_pending_scope_reconcile_candidate(
        conn,
        PID,
        project,
        target_commit_sha=head_commit,
        run_id="scope-test-fanin-head-consistency",
        snapshot_id="scope-test-fanin-head-consistency",
        created_by="test",
        semantic_enrich=False,
    )
    assert scope["ok"] is True
    assert scope["scope_file_delta"]["strategy"] == "incremental_graph_delta"
    assert scope["scope_file_delta"]["graph_delta_mode"] == "test_fanin_hash_only"
    assert scope["scope_file_delta"]["changed_files"] == ["agent/tests/test_integration.py"]
    assert scope["scope_graph_delta"]["mode"] == "test_fanin_hash_only"
    assert scope["scope_graph_delta"]["added_nodes"] == []
    assert scope["scope_graph_delta"]["removed_nodes"] == []
    assert scope["scope_graph_delta"]["added_edges"] == []
    assert scope["scope_graph_delta"]["removed_edges"] == []

    full = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-test-fanin-head-consistency",
        commit_sha=head_commit,
        snapshot_id="full-test-fanin-head-consistency",
        created_by="test",
        semantic_enrich=False,
    )
    assert full["ok"] is True

    assert _normalized_snapshot(conn, "scope-test-fanin-head-consistency") == _normalized_snapshot(
        conn,
        "full-test-fanin-head-consistency",
    )

    scope_nodes = _nodes_by_module(_read_snapshot_graph(PID, "scope-test-fanin-head-consistency"))
    service = scope_nodes["agent.service"]
    other = scope_nodes["agent.other"]
    assert "agent/tests/test_integration.py" not in service["test"]
    assert "agent/tests/test_integration.py" in other["test"]
    service_fanin = (service.get("metadata") or {}).get("test_consumer_fanin") or []
    other_fanin = (other.get("metadata") or {}).get("test_consumer_fanin") or []
    assert {entry["path"] for entry in service_fanin} == {"agent/tests/test_service.py"}
    assert {entry["path"] for entry in other_fanin} == {"agent/tests/test_integration.py"}
    assert service_fanin[0]["evidence"] == "test_import_fanin"
    assert other_fanin[0]["evidence"] == "test_import_fanin"
    assert "agent.service.service_entry" in service_fanin[0]["imports"]
    assert "agent.other.other_entry" in other_fanin[0]["imports"]


def test_scope_reconcile_test_fanin_ignores_unrelated_inventory_status_churn(conn, tmp_path):
    project = tmp_path / "project"
    _write_project(project)
    _write(
        project / "agent" / "other.py",
        "def other_entry():\n"
        "    return 'ok'\n",
    )
    _write(
        project / "agent" / "tests" / "test_integration.py",
        "from agent.service import service_entry\n\n"
        "def test_integration():\n"
        "    assert service_entry() == 'ok'\n",
    )
    _init_git(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "base")
    base_commit = _git(project, "rev-parse", "HEAD")

    base = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-test-fanin-base-churn",
        commit_sha=base_commit,
        snapshot_id="full-test-fanin-base-churn",
        created_by="test",
        activate=True,
        semantic_enrich=False,
    )
    assert base["ok"] is True
    _rewrite_inventory_status(
        "full-test-fanin-base-churn",
        "README.md",
        scan_status="stale_fixture_status",
        graph_status="stale_fixture_graph_status",
    )

    _write(
        project / "agent" / "tests" / "test_integration.py",
        "from agent.other import other_entry\n\n"
        "def test_integration():\n"
        "    assert other_entry() == 'ok'\n",
    )
    _git(project, "add", "agent/tests/test_integration.py")
    _git(project, "commit", "-m", "move integration test fanin")
    head_commit = _git(project, "rev-parse", "HEAD")
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha=head_commit,
        parent_commit_sha=base_commit,
        evidence={"source": "test"},
    )

    scope = run_pending_scope_reconcile_candidate(
        conn,
        PID,
        project,
        target_commit_sha=head_commit,
        run_id="scope-test-fanin-head-churn",
        snapshot_id="scope-test-fanin-head-churn",
        created_by="test",
        semantic_enrich=False,
    )
    assert scope["ok"] is True
    assert scope["scope_file_delta"]["strategy"] == "incremental_graph_delta"
    assert scope["scope_file_delta"]["graph_delta_mode"] == "test_fanin_hash_only"
    assert scope["scope_file_delta"]["changed_files"] == ["agent/tests/test_integration.py"]
    assert scope["scope_file_delta"]["status_changed_files"] == []
    assert scope["scope_file_delta"]["ignored_status_changed_files"] == ["README.md"]
    assert scope["scope_graph_delta"]["mode"] == "test_fanin_hash_only"

    full = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-test-fanin-head-churn",
        commit_sha=head_commit,
        snapshot_id="full-test-fanin-head-churn",
        created_by="test",
        semantic_enrich=False,
    )
    assert full["ok"] is True

    assert _normalized_snapshot(conn, "scope-test-fanin-head-churn") == _normalized_snapshot(
        conn,
        "full-test-fanin-head-churn",
    )
