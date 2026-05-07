from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent.governance import graph_snapshot_store as store
from agent.governance.db import _ensure_schema
from agent.governance.governance_index import (
    build_governance_index,
    load_snapshot_nodes_for_inventory,
    persist_governance_index,
)


PID = "governance-index-test"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    yield c
    c.close()


def _write_project(root: Path) -> None:
    (root / "src" / "demo_app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "docs").mkdir()
    (root / "README.md").write_text(
        "# Demo App\n\nThis index explains the demo service.\n",
        encoding="utf-8",
    )
    (root / "docs" / "usage.md").write_text(
        "# Usage\n\nCall the service from a route.\n",
        encoding="utf-8",
    )
    (root / "src" / "demo_app" / "service.py").write_text(
        "def calculate_total(items):\n"
        "    return sum(items)\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_service.py").write_text(
        "from src.demo_app.service import calculate_total\n\n"
        "def test_calculate_total():\n"
        "    assert calculate_total([1, 2]) == 3\n",
        encoding="utf-8",
    )


def _activate_graph(conn) -> str:
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="imported-abc1234-index",
        commit_sha="abc1234",
        snapshot_kind="imported",
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=[
            {
                "id": "L7.service",
                "layer": "L7",
                "title": "Demo Service",
                "kind": "feature",
                "primary": ["src/demo_app/service.py"],
                "secondary": ["README.md", "docs/usage.md"],
                "test": ["tests/test_service.py"],
                "metadata": {"subsystem": "demo"},
            }
        ],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    return snapshot["snapshot_id"]


def test_load_snapshot_nodes_for_inventory_decodes_file_mappings(conn):
    snapshot_id = _activate_graph(conn)

    nodes = load_snapshot_nodes_for_inventory(conn, PID, snapshot_id)

    assert nodes == [
        {
            "id": "L7.service",
            "node_id": "L7.service",
            "layer": "L7",
            "title": "Demo Service",
            "kind": "feature",
            "primary": ["src/demo_app/service.py"],
            "secondary": ["README.md", "docs/usage.md"],
            "test": ["tests/test_service.py"],
            "metadata": {"subsystem": "demo"},
        }
    ]


def test_build_and_persist_governance_index_maps_hashes_symbols_docs_and_graph(conn, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _write_project(project)
    _activate_graph(conn)

    index = build_governance_index(
        conn,
        PID,
        project,
        run_id="index-abc1234-test",
        commit_sha="abc1234",
    )

    rows = {row["path"]: row for row in index["file_inventory"]}
    assert rows["src/demo_app/service.py"]["scan_status"] == "clustered"
    assert rows["src/demo_app/service.py"]["graph_status"] == "mapped"
    assert rows["src/demo_app/service.py"]["mapped_node_ids"] == ["L7.service"]
    assert rows["README.md"]["scan_status"] == "secondary_attached"
    assert rows["README.md"]["graph_status"] == "attached"
    assert rows["docs/usage.md"]["scan_status"] == "secondary_attached"
    assert rows["tests/test_service.py"]["scan_status"] == "secondary_attached"
    assert rows["src/demo_app/service.py"]["file_hash"].startswith("sha256:")
    assert rows["src/demo_app/service.py"]["last_scanned_commit"] == "abc1234"

    symbol_index = index["symbol_index"]
    symbol = next(
        item for item in symbol_index["symbols"]
        if item["id"].endswith("::calculate_total")
    )
    assert symbol["path"] == "src/demo_app/service.py"
    assert symbol["line_start"] == 1
    assert symbol["line_end"] >= symbol["line_start"]

    doc_index = index["doc_index"]
    readme = next(item for item in doc_index["documents"] if item["path"] == "README.md")
    assert readme["headings"][0]["title"] == "Demo App"
    assert index["coverage_state"]["active_snapshot_id"] == "imported-abc1234-index"
    assert index["coverage_state"]["file_states"]["src/demo_app/service.py"]["file_hash"]
    assert "confidence" not in json.dumps(index, ensure_ascii=False)

    summary = persist_governance_index(
        conn,
        PID,
        index,
        artifact_root=tmp_path / "artifacts",
    )

    assert summary["inventory_rows_persisted"] == len(index["file_inventory"])
    for path in summary["artifacts"].values():
        assert Path(path).exists()

    persisted = conn.execute(
        """
        SELECT scan_status, file_hash FROM reconcile_file_inventory
        WHERE project_id=? AND run_id=? AND path=?
        """,
        (PID, "index-abc1234-test", "README.md"),
    ).fetchone()
    assert persisted["scan_status"] == "secondary_attached"
    assert persisted["file_hash"].startswith("sha256:")
