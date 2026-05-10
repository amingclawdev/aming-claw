"""MF-2026-05-10-015: regression tests for
`_projection_current_node_ids` — the filter that prevents
phantom-current carried_forward nodes from being spuriously re-enqueued
as `ai_pending` on every scope reconcile.

Background: dashboard derives "Node semantic N/M" counters from the
per-snapshot `graph_semantic_projections` cache (event-derived). The
legacy persistent layer (`graph_semantic_nodes` / `state.json`) stores
only the freshly enriched subset. Without this filter, every reconcile
saw the projection-only "phantom" carried_forward_current nodes as
"missing" in state.json and re-enqueued them as `ai_pending`.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from agent.governance import graph_events
from agent.governance import graph_snapshot_store as store
from agent.governance import reconcile_semantic_enrichment as semantic
from agent.governance.db import _ensure_schema


PID = "phantom-requeue-test"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    store.ensure_schema(c)
    yield c
    c.close()


def test_helper_returns_empty_when_no_base_snapshot(conn):
    """Empty/missing base id → empty set; no DB hit, no error."""
    assert semantic._projection_current_node_ids(conn, PID, None) == set()
    assert semantic._projection_current_node_ids(conn, PID, "") == set()
    assert semantic._projection_current_node_ids(conn, PID, "   ") == set()


def test_helper_returns_empty_when_projection_missing(conn):
    """Snapshot exists but has no projection row → empty set."""
    snap = store.create_graph_snapshot(
        conn, PID, snapshot_id="no-proj-snap",
        commit_sha="head", snapshot_kind="scope",
        graph_json={"deps_graph": {"nodes": [], "edges": []}},
    )
    assert semantic._projection_current_node_ids(conn, PID, snap["snapshot_id"]) == set()


def test_helper_picks_up_both_current_and_carried_forward_statuses(conn, monkeypatch):
    """The status set must include `semantic_current` AND
    `semantic_carried_forward_current`. Other validity statuses (stale,
    missing, hash_unverified) must NOT be included."""

    fake_proj: dict[str, Any] = {
        "projection": {
            "node_semantics": {
                "L7.1": {"validity": {"status": "semantic_current"}},
                "L7.2": {"validity": {"status": "semantic_carried_forward_current"}},
                "L7.3": {"validity": {"status": "semantic_stale_feature_hash"}},
                "L7.4": {"validity": {"status": "semantic_missing"}},
                "L7.5": {"validity": {"status": "semantic_hash_unverified"}},
                "L7.6": {"validity": {}},
                "L7.7": {},
            }
        }
    }

    def _fake_get(_conn, _pid, _sid, projection_id=""):
        return fake_proj

    monkeypatch.setattr(graph_events, "get_semantic_projection", _fake_get)

    got = semantic._projection_current_node_ids(conn, PID, "any-snap")
    assert got == {"L7.1", "L7.2"}


def test_helper_swallows_get_projection_failure(conn, monkeypatch):
    """Projection layer is advisory — if it raises, the helper returns
    empty rather than aborting the reconcile."""

    def _boom(*a, **kw):
        raise RuntimeError("synthetic projection failure")

    monkeypatch.setattr(graph_events, "get_semantic_projection", _boom)
    assert semantic._projection_current_node_ids(conn, PID, "any-snap") == set()


def test_helper_handles_malformed_projection_payload(conn, monkeypatch):
    """Defensive: arbitrary garbage in projection payload returns empty."""

    for garbage in [None, [], "string", {"projection": "not-a-dict"},
                    {"projection": {"node_semantics": "not-a-dict"}}]:
        monkeypatch.setattr(
            graph_events, "get_semantic_projection",
            lambda *a, _g=garbage, **kw: _g,
        )
        assert semantic._projection_current_node_ids(conn, PID, "x") == set(), (
            f"garbage payload {garbage!r} did not return empty set"
        )
