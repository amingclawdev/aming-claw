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


def test_batch_schema_is_already_base_owned_and_source_exact():
    from agent.governance import db

    source = db._migration_capable_source_schema_memory()
    canonical = sqlite3.connect(":memory:")
    try:
        canonical.executescript(bm.BATCH_MEMORY_SCHEMA_SQL)
        source_rows = tuple(
            row for row in db._sqlite_master_inventory(source)
            if row[1] == "reconcile_batch_memory"
            or row[2] == "reconcile_batch_memory"
        )
        canonical_rows = tuple(
            row for row in db._sqlite_master_inventory(canonical)
            if row[1] == "reconcile_batch_memory"
            or row[2] == "reconcile_batch_memory"
        )
    finally:
        source.close()
        canonical.close()

    assert source_rows == canonical_rows
    assert len(source_rows) == 4
    authority = {
        tuple(row) for row in db.authority_projection_schema_inventory()["inventory"]
    }
    assert set(source_rows).issubset(authority)


def test_batch_schema_dev_exact_is_no_ddl_no_commit_and_allows_dml(monkeypatch):
    from agent.governance import db

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _ensure_schema(connection)
    connection.commit()
    statements = []
    connection.set_trace_callback(statements.append)
    monkeypatch.setenv(db.RUNTIME_PLANE_ENV, db.DEV_RUNTIME_PLANE)
    connection.execute("BEGIN")

    bm.ensure_schema(connection)
    connection.execute(
        "INSERT INTO reconcile_batch_memory "
        "(project_id,batch_id,created_at,updated_at) VALUES('p','b','now','now')"
    )

    assert connection.in_transaction is True
    assert not any(
        statement.lstrip().upper().startswith(("CREATE ", "ALTER ", "COMMIT"))
        for statement in statements
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM reconcile_batch_memory"
    ).fetchone()[0] == 1
    connection.rollback()
    connection.close()


@pytest.mark.parametrize("state", ["missing", "altered"])
def test_batch_schema_dev_missing_or_altered_is_typed_zero_write(
    monkeypatch, state,
):
    from agent.governance import db

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    if state == "altered":
        _ensure_schema(connection)
        connection.execute("DROP INDEX idx_reconcile_batch_memory_status")
        connection.execute(
            "CREATE INDEX idx_reconcile_batch_memory_status "
            "ON reconcile_batch_memory(project_id, session_id)"
        )
        connection.commit()
    before = db._sqlite_master_inventory(connection)
    changes = connection.total_changes
    monkeypatch.setenv(db.RUNTIME_PLANE_ENV, db.DEV_RUNTIME_PLANE)

    with pytest.raises(db.DevRuntimeSchemaVerificationError):
        bm.ensure_schema(connection)

    assert connection.total_changes == changes
    assert db._sqlite_master_inventory(connection) == before
    connection.close()


def test_batch_schema_stable_preserves_legacy_executescript_and_commit(monkeypatch):
    from agent.governance import db

    calls = []

    class StableConnection:
        row_factory = None

        def executescript(self, sql):
            calls.append(("script", sql))

        def commit(self):
            calls.append(("commit", None))

    monkeypatch.setattr(db, "dev_runtime_verify_only", lambda: False)
    connection = StableConnection()

    bm.ensure_schema(connection)

    assert connection.row_factory is sqlite3.Row
    assert calls == [("script", bm.BATCH_MEMORY_SCHEMA_SQL), ("commit", None)]


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
    assert memory["file_claims"] == {}
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
    assert memory["file_claims"]["agent/governance/backlog_runtime.py"][0]["feature_name"] == "Backlog Runtime State Management"
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


def test_shared_file_claim_preserves_first_owner_and_surfaces_overlap(conn):
    bm.create_or_get_batch(conn, "p-test", batch_id="batch-1")
    bm.record_pm_decision(
        conn,
        "p-test",
        "batch-1",
        "fp-a",
        {
            "decision": "new_feature",
            "feature_name": "Primary Feature",
            "owned_files": ["agent/governance/shared.py"],
        },
    )

    batch = bm.record_pm_decision(
        conn,
        "p-test",
        "batch-1",
        "fp-b",
        {
            "decision": "new_feature",
            "feature_name": "Secondary Feature",
            "owned_files": ["agent/governance/shared.py"],
        },
    )

    memory = batch["memory"]
    assert memory["file_ownership"]["agent/governance/shared.py"] == "Primary Feature"
    assert [
        claim["feature_name"]
        for claim in memory["file_claims"]["agent/governance/shared.py"]
    ] == ["Primary Feature", "Secondary Feature"]
    assert memory["accepted_features"]["Secondary Feature"]["shared_files"] == [
        "agent/governance/shared.py"
    ]
    assert any(
        conflict.get("reason") == "shared_file_claim"
        and conflict.get("owner_feature") == "Primary Feature"
        and conflict.get("claimant_feature") == "Secondary Feature"
        for conflict in memory["open_conflicts"]
    )

    related = bm.find_related_features(batch, {
        "primary_files": ["agent/governance/shared.py"],
    })
    secondary = next(item for item in related if item["feature_name"] == "Secondary Feature")
    assert "file_claim" in secondary["reasons"]


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
