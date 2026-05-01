"""Tests for agent.governance.symbol_swap atomic swap operation.

Covers AC3, AC4, AC5 of the PR2 Atomic Swap PRD. All tests use tmp_path
and write their own primary files — no real repo writes, no network.
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

# Ensure agent package is importable
_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from agent.governance.symbol_swap import (
    BAK_RETENTION_DAYS,
    atomic_swap,
    rollback,
    smoke_validate,
    status,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_primary_file(tmp_path, name="primary.py", content="x = 1\n"):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def _write_graph(path, nodes):
    payload = {"version": "v2", "nodes": nodes}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_bak_retention_days_constant():
    assert BAK_RETENTION_DAYS == 30


# ---------------------------------------------------------------------------
# atomic_swap happy path
# ---------------------------------------------------------------------------

def test_atomic_swap_renames_graph_and_candidate(tmp_path):
    primary = _make_primary_file(tmp_path)
    graph = _write_graph(
        tmp_path / "graph.json",
        [{"node_id": "L1.1", "layer": "L1", "primary": [str(primary)]}],
    )
    candidate = _write_graph(
        tmp_path / "graph.v2.json",
        [{"node_id": "L1.2", "layer": "L1", "primary": [str(primary)]}],
    )
    bak = tmp_path / "graph.json.bak"
    assert not bak.exists()

    result = atomic_swap(graph, candidate)

    assert result["ok"] is True
    assert bak.exists(), "backup must exist after swap"
    assert graph.exists(), "graph.json must exist (newly swapped)"
    assert not candidate.exists(), "candidate must be moved away after swap"
    # graph.json now has candidate's content
    new_data = json.loads(graph.read_text(encoding="utf-8"))
    assert new_data["nodes"][0]["node_id"] == "L1.2"


# ---------------------------------------------------------------------------
# smoke_validate
# ---------------------------------------------------------------------------

def test_smoke_validate_accepts_well_formed_graph(tmp_path):
    primary = _make_primary_file(tmp_path)
    graph = _write_graph(
        tmp_path / "graph.json",
        [
            {"node_id": "L0.1", "layer": "L0", "primary": [str(primary)]},
            {"node_id": "L6.7", "layer": "L6", "primary": [str(primary)]},
        ],
    )
    result = smoke_validate(graph)
    assert result["ok"] is True
    # determinism
    assert smoke_validate(graph) == result


def test_smoke_validate_rejects_duplicate_node_ids(tmp_path):
    primary = _make_primary_file(tmp_path)
    graph = _write_graph(
        tmp_path / "graph.json",
        [
            {"node_id": "L1.1", "layer": "L1", "primary": [str(primary)]},
            {"node_id": "L1.1", "layer": "L1", "primary": [str(primary)]},
        ],
    )
    result = smoke_validate(graph)
    assert result["ok"] is False
    assert "L1.1" in result.get("duplicates", [])


def test_smoke_validate_rejects_layers_outside_l0_l6(tmp_path):
    primary = _make_primary_file(tmp_path)
    graph = _write_graph(
        tmp_path / "graph.json",
        [
            {"node_id": "L7.5", "layer": "L7", "primary": [str(primary)]},
        ],
    )
    result = smoke_validate(graph)
    assert result["ok"] is False
    assert any(b["layer"] == "L7" for b in result.get("bad_layers", []))


def test_smoke_validate_rejects_missing_primary_paths(tmp_path):
    graph = _write_graph(
        tmp_path / "graph.json",
        [
            {"node_id": "L1.1", "layer": "L1",
             "primary": [str(tmp_path / "ghost.py")]},
        ],
    )
    result = smoke_validate(graph)
    assert result["ok"] is False
    assert result.get("missing_paths"), "should report missing path entries"


# ---------------------------------------------------------------------------
# atomic_swap auto-rollback on smoke failure
# ---------------------------------------------------------------------------

def test_atomic_swap_auto_rollbacks_on_smoke_failure(tmp_path):
    primary = _make_primary_file(tmp_path)

    # original graph: valid (so smoke would have passed)
    original_payload = {
        "version": "v1",
        "nodes": [{"node_id": "L1.1", "layer": "L1", "primary": [str(primary)]}],
    }
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps(original_payload), encoding="utf-8")

    # candidate: bad (duplicate node ids)
    bad_payload = {
        "version": "v2",
        "nodes": [
            {"node_id": "DUP", "layer": "L1", "primary": [str(primary)]},
            {"node_id": "DUP", "layer": "L1", "primary": [str(primary)]},
        ],
    }
    candidate = tmp_path / "graph.v2.json"
    candidate.write_text(json.dumps(bad_payload), encoding="utf-8")

    alerts = []

    def _alert(info):
        alerts.append(info)

    result = atomic_swap(graph, candidate, observer_alert=_alert)

    assert result["ok"] is False
    assert result["rolled_back"] is True
    # graph.json contents preserved (matches original)
    after = json.loads(graph.read_text(encoding="utf-8"))
    assert after == original_payload
    # observer_alert called exactly once
    assert len(alerts) == 1
    assert alerts[0]["ok"] is False
    assert isinstance(alerts[0]["reason"], str)
    # candidate file is preserved on disk (so dev can fix and retry)
    assert candidate.exists()


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------

def test_rollback_restores_graph_from_bak(tmp_path):
    primary = _make_primary_file(tmp_path)
    graph = tmp_path / "graph.json"
    bak = tmp_path / "graph.json.bak"
    # simulate a previous good graph backed up to .bak
    backed_up = {"version": "v0", "nodes": [
        {"node_id": "L1.1", "layer": "L1", "primary": [str(primary)]},
    ]}
    bak.write_text(json.dumps(backed_up), encoding="utf-8")
    # write a current "broken" graph.json
    graph.write_text(json.dumps({"broken": True}), encoding="utf-8")

    result = rollback(graph)
    assert result["ok"] is True
    # graph now matches backed_up payload
    restored = json.loads(graph.read_text(encoding="utf-8"))
    assert restored == backed_up
    # bak should be consumed (moved into graph.json)
    assert not bak.exists()


def test_rollback_refuses_old_bak(tmp_path):
    primary = _make_primary_file(tmp_path)
    graph = tmp_path / "graph.json"
    bak = tmp_path / "graph.json.bak"
    bak.write_text(json.dumps({"nodes": []}), encoding="utf-8")
    # backdate the bak file: 60 days old
    old_ts = time.time() - (60 * 86400)
    os.utime(bak, (old_ts, old_ts))

    result = rollback(graph, max_age_days=30)
    assert result["ok"] is False
    assert "too old" in result["reason"].lower()
    # bak still on disk (not consumed)
    assert bak.exists()


def test_rollback_no_bak(tmp_path):
    graph = tmp_path / "graph.json"
    graph.write_text("{}", encoding="utf-8")
    result = rollback(graph)
    assert result["ok"] is False
    assert "no backup" in result["reason"].lower()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def test_status_no_bak(tmp_path):
    graph = tmp_path / "graph.json"
    info = status(graph)
    assert info["bak_exists"] is False


def test_status_with_bak(tmp_path):
    graph = tmp_path / "graph.json"
    bak = tmp_path / "graph.json.bak"
    bak.write_text("{}", encoding="utf-8")
    info = status(graph)
    assert info["bak_exists"] is True
    assert isinstance(info["age_days"], float)
    assert info["expired"] is False
