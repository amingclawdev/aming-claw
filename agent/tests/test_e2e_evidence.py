from __future__ import annotations

import sqlite3

import pytest

from agent.governance import e2e_evidence
from agent.governance import graph_snapshot_store as store
from agent.governance.db import _ensure_schema


PID = "e2e-evidence-test"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    monkeypatch.setattr("agent.governance.e2e_evidence._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    store.ensure_schema(c)
    yield c
    c.close()


def _graph(feature_hash: str) -> dict:
    return {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.1",
                    "layer": "L7",
                    "title": "src.api",
                    "primary": ["src/api.ts"],
                    "secondary": [],
                    "test": ["tests/smoke.test.mjs"],
                    "metadata": {"feature_hash": feature_hash},
                }
            ],
            "edges": [],
        }
    }


def _inventory(file_hash: str) -> list[dict]:
    return [
        {
            "path": "src/api.ts",
            "file_hash": file_hash,
            "sha256": file_hash.replace("sha256:", ""),
            "file_kind": "code",
            "scan_status": "scanned",
        },
        {
            "path": "tests/smoke.test.mjs",
            "file_hash": "sha256:test",
            "sha256": "test",
            "file_kind": "test",
            "scan_status": "scanned",
        },
    ]


def _snapshot(conn, snapshot_id: str, feature_hash: str, file_hash: str):
    snap = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id=snapshot_id,
        commit_sha=snapshot_id,
        snapshot_kind="scope",
        graph_json=_graph(feature_hash),
        file_inventory=_inventory(file_hash),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot_id,
        nodes=_graph(feature_hash)["deps_graph"]["nodes"],
        edges=[],
    )
    return snap


def _config() -> dict:
    return {
        "auto_run": False,
        "default_timeout_sec": 900,
        "suites": {
            "dashboard.semantic.safe": {
                "label": "Dashboard semantic safe path",
                "command": "node e2e-trunk.mjs",
                "live_ai": False,
                "requires_human_approval": False,
                "trigger": {"paths": ["src/**"], "nodes": ["L7.1"], "tags": ["dashboard"]},
            }
        },
    }


def test_e2e_evidence_records_hashes_and_marks_later_snapshot_stale(conn):
    _snapshot(conn, "scope-old", "sha256:feature-old", "sha256:file-old")
    conn.commit()

    recorded = e2e_evidence.record_e2e_evidence(
        conn,
        PID,
        "scope-old",
        {
            "suite_id": "dashboard.semantic.safe",
            "status": "passed",
            "run_id": "run-1",
            "covered_node_ids": ["L7.1"],
            "covered_files": ["src/api.ts"],
            "artifact_path": "/tmp/report.json",
        },
    )

    assert recorded["ok"] is True
    assert recorded["covered_node_count"] == 1
    current = e2e_evidence.plan_e2e_impact(conn, PID, "scope-old", _config())
    assert current["summary"]["current"] == 1
    assert current["suites"][0]["status"] == "current"

    _snapshot(conn, "scope-new", "sha256:feature-new", "sha256:file-new")
    conn.commit()
    stale = e2e_evidence.plan_e2e_impact(conn, PID, "scope-new", _config())

    assert stale["summary"]["stale"] == 1
    assert stale["suites"][0]["required"] is True
    reason_kinds = {reason["kind"] for reason in stale["suites"][0]["stale_reasons"]}
    assert "file_hash_changed" in reason_kinds
    assert "node_feature_hash_changed" in reason_kinds


def test_e2e_impact_marks_missing_suite_without_evidence(conn):
    _snapshot(conn, "scope-new", "sha256:feature-new", "sha256:file-new")
    conn.commit()

    impact = e2e_evidence.plan_e2e_impact(
        conn,
        PID,
        "scope-new",
        _config(),
        changed_files=["src/api.ts"],
    )

    assert impact["summary"]["missing"] == 1
    assert impact["suites"][0]["trigger_matched"] is True
    assert impact["suites"][0]["required"] is True
