"""Unit tests for reconcile batch memory."""
from __future__ import annotations

import sqlite3

import pytest

from agent.governance import reconcile_batch_memory as bm
from agent.governance.db import _configure_connection, _ensure_schema


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "gov.db"
    c = sqlite3.connect(str(db_path))
    _configure_connection(c, busy_timeout=0)
    _ensure_schema(c)
    yield c
    c.close()


def test_create_or_get_batch_initializes_memory(conn):
    batch = bm.create_or_get_batch(
        conn,
        "p-test",
        session_id="session-1",
        batch_id="batch-1",
        created_by="pm",
    )

    assert batch["batch_id"] == "batch-1"
    assert batch["session_id"] == "session-1"
    memory = batch["memory"]
    assert memory["accepted_features"] == {}
    assert memory["file_ownership"] == {}
    assert memory["processed_clusters"] == {}
    assert memory["merge_decisions"] == []


def test_batch_memory_persists_across_connections(tmp_path):
    db_path = tmp_path / "gov.db"
    first = sqlite3.connect(str(db_path))
    _configure_connection(first, busy_timeout=0)
    _ensure_schema(first)
    bm.create_or_get_batch(first, "p-test", session_id="session-1", batch_id="batch-1")
    bm.record_pm_decision(
        first,
        "p-test",
        "batch-1",
        "fp-a",
        {"decision": "defer", "reason": "wait for related cluster"},
    )
    first.close()

    second = sqlite3.connect(str(db_path))
    _configure_connection(second, busy_timeout=0)
    _ensure_schema(second)
    try:
        batch = bm.get_batch(second, "p-test", "batch-1")
        assert batch["memory"]["processed_clusters"]["fp-a"]["decision"] == "defer"
    finally:
        second.close()


def test_record_new_feature_updates_feature_map_and_file_ownership(conn):
    bm.create_or_get_batch(conn, "p-test", batch_id="batch-1")

    batch = bm.record_pm_decision(
        conn,
        "p-test",
        "batch-1",
        "fp-a",
        {
            "decision": "new_feature",
            "feature_name": "Backlog Runtime State Management",
            "purpose": "Owns backlog runtime transitions.",
            "owned_files": ["agent/governance/backlog_runtime.py"],
            "candidate_tests": ["agent/tests/test_backlog_runtime.py"],
            "candidate_docs": ["docs/dev/backlog.md"],
            "decided_by": "pm",
        },
    )

    memory = batch["memory"]
    feature = memory["accepted_features"]["Backlog Runtime State Management"]
    assert feature["clusters"] == ["fp-a"]
    assert feature["owned_files"] == ["agent/governance/backlog_runtime.py"]
    assert memory["file_ownership"]["agent/governance/backlog_runtime.py"] == "Backlog Runtime State Management"
    assert memory["processed_clusters"]["fp-a"]["decision"] == "new_feature"
    assert memory["reserved_names"] == ["Backlog Runtime State Management"]


def test_record_merge_into_existing_feature_appends_cluster(conn):
    bm.create_or_get_batch(conn, "p-test", batch_id="batch-1")
    bm.record_pm_decision(
        conn,
        "p-test",
        "batch-1",
        "fp-a",
        {
            "decision": "new_feature",
            "feature_name": "Reconcile Phase Z",
            "owned_files": ["agent/governance/reconcile_phases/phase_z.py"],
        },
    )

    batch = bm.record_pm_decision(
        conn,
        "p-test",
        "batch-1",
        "fp-b",
        {
            "decision": "merge_into_existing_feature",
            "target_feature": "Reconcile Phase Z",
            "owned_files": ["agent/governance/reconcile_phases/phase_z_v2.py"],
            "reason": "Same symbol scan feature.",
        },
    )

    feature = batch["memory"]["accepted_features"]["Reconcile Phase Z"]
    assert feature["clusters"] == ["fp-a", "fp-b"]
    assert feature["owned_files"] == [
        "agent/governance/reconcile_phases/phase_z.py",
        "agent/governance/reconcile_phases/phase_z_v2.py",
    ]
    assert batch["memory"]["file_ownership"]["agent/governance/reconcile_phases/phase_z_v2.py"] == "Reconcile Phase Z"


def test_find_related_features_uses_file_ownership_and_candidate_consumers(conn):
    bm.create_or_get_batch(conn, "p-test", batch_id="batch-1")
    batch = bm.record_pm_decision(
        conn,
        "p-test",
        "batch-1",
        "fp-a",
        {
            "decision": "new_feature",
            "feature_name": "Reconcile Phase Z",
            "owned_files": ["agent/governance/reconcile_phases/phase_z.py"],
            "candidate_tests": ["agent/tests/test_phase_z.py"],
        },
    )

    related = bm.find_related_features(batch, {
        "primary_files": ["agent/governance/reconcile_phases/phase_z.py"],
        "candidate_tests": ["agent/tests/test_phase_z.py"],
    })

    assert related == [{
        "feature_name": "Reconcile Phase Z",
        "reasons": ["file_overlap", "file_ownership"],
        "matching_files": [
            "agent/governance/reconcile_phases/phase_z.py",
            "agent/tests/test_phase_z.py",
        ],
        "clusters": ["fp-a"],
    }]


def test_orphan_and_split_decisions_are_recorded_for_followup(conn):
    bm.create_or_get_batch(conn, "p-test", batch_id="batch-1")
    bm.record_pm_decision(
        conn,
        "p-test",
        "batch-1",
        "fp-orphan",
        {
            "decision": "orphan_dead_code",
            "reason": "No incoming roots and no consumer evidence.",
            "conflicts": [{"reason": "needs observer review"}],
        },
    )
    batch = bm.record_pm_decision(
        conn,
        "p-test",
        "batch-1",
        "fp-split",
        {
            "decision": "split",
            "reason": "Contains two unrelated domains.",
        },
    )

    memory = batch["memory"]
    assert memory["processed_clusters"]["fp-orphan"]["decision"] == "orphan_dead_code"
    assert memory["processed_clusters"]["fp-split"]["decision"] == "split"
    assert {c["cluster_fingerprint"] for c in memory["open_conflicts"]} == {"fp-orphan", "fp-split"}


def test_invalid_decision_rejected(conn):
    bm.create_or_get_batch(conn, "p-test", batch_id="batch-1")
    with pytest.raises(ValueError):
        bm.record_pm_decision(
            conn,
            "p-test",
            "batch-1",
            "fp-bad",
            {"decision": "invented"},
        )
