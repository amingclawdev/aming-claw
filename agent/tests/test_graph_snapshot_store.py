from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import sqlite3
import threading
import time
import tracemalloc
from pathlib import Path

import pytest

from agent.governance import graph_snapshot_store as store
from agent.governance import task_timeline
from agent.governance import db
from agent.governance.baseline_service import create_baseline
from agent.governance.db import _ensure_schema


PID = "graph-snapshot-test"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _stable_graph_activation_connection(monkeypatch):
    """Graph-store unit fixtures model a verified stable DB by default.

    Physical connection classification itself is covered in test_governance_db;
    individual store tests do not own a live stable authority fixture.
    """
    monkeypatch.setattr(
        db,
        "classify_graph_activation_connection",
        lambda _conn: {
            "schema_version": "ac_graph_activation_policy.v1",
            "runtime_plane": "stable",
            "active_graph_activation_allowed": True,
            "classification_reason": "test_verified_stable_connection",
            "project_id": PID,
        },
    )


def _claim_owner(suffix: str = "one") -> dict[str, object]:
    return {
        "manager_epoch": f"epoch-{suffix}",
        "manager_pid": 4242,
        "manager_started_at": "2026-08-10T00:00:00Z",
        "manager_start_identity": f"manager-start-{suffix}",
    }


def _file_connection(path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=0.2)
    connection.row_factory = sqlite3.Row
    store.ensure_schema(connection)
    connection.commit()
    return connection


def test_c_family_snapshot_companion_indexes_only_successful_action_facts(conn):
    analysis = {
        "schema_version": "graph.c_family_analysis.v1",
        "status": "partial",
        "actions": [
            {"compilation_action_id": "a-ok", "translation_unit_id": "tu-ok", "profile_id": "p", "file": "src/a.cc", "language": "cpp"},
            {"compilation_action_id": "a-ok-2", "translation_unit_id": "tu-ok-2", "profile_id": "p2", "file": "src/a.cc", "language": "cpp"},
            {"compilation_action_id": "a-failed", "translation_unit_id": "tu-failed", "profile_id": "p", "file": "src/b.mm", "language": "objective-cpp"},
        ],
        "results": [
            {"status": "ok", "action": {"compilation_action_id": "a-ok"}},
            {"status": "ok", "action": {"compilation_action_id": "a-ok-2"}},
            {"status": "failed", "action": {"compilation_action_id": "a-failed"}},
        ],
        "macro_analysis": [{"compilation_action_id": "a-ok", "state": "collected"}],
        "symbols": [
            {"symbol_id": "s1", "compilation_action_id": "a-ok", "profile_id": "p", "name": "f", "qualified_name": "n::f", "kind": "FunctionDecl", "signature": "int ()", "file": "src/a.cc", "lineno": 2, "is_definition": True},
            {"symbol_id": "s1", "compilation_action_id": "a-ok-2", "profile_id": "p2", "name": "f", "qualified_name": "n::f", "kind": "FunctionDecl", "signature": "int ()", "file": "src/a.cc", "lineno": 2, "is_definition": True},
        ],
        "occurrences": [
            {"occurrence_id": "o1", "symbol_id": "s1", "compilation_action_id": "a-ok", "role": "definition", "file": "src/a.cc", "line": 2, "column": 1},
            {"occurrence_id": "o2", "symbol_id": "s1", "compilation_action_id": "a-ok-2", "role": "definition", "file": "src/a.cc", "line": 2, "column": 1},
        ],
        "relations": [{"relation_id": "r1", "compilation_action_id": "a-ok", "relation_type": "calls", "source_symbol_id": "s1", "target_symbol_id": "s2", "source_file": "src/a.cc", "target_file": "", "direction": "out", "resolution": "external", "condition_ref": ""}],
        "diagnostics": [{"compilation_action_id": "a-failed", "analysis_status": "failed", "severity": "error", "file": "src/b.mm", "line": 1, "message": "SDK unavailable"}],
    }
    snapshot = store.create_graph_snapshot(
        conn, PID, snapshot_id="c-family-snapshot", commit_sha="abc", snapshot_kind="full", c_family_analysis=analysis
    )
    counts = store.index_c_family_analysis(conn, PID, snapshot["snapshot_id"], analysis)
    conn.commit()
    persisted = store.get_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    assert store.validate_snapshot_companion_integrity(persisted)["valid"] is True
    assert counts == {"actions": 3, "symbols": 2, "occurrences": 2, "relations": 1, "diagnostics": 1}
    assert store.query_c_family_analysis(conn, PID, snapshot["snapshot_id"], table="occurrences", symbol_id="s1") == analysis["occurrences"]
    assert store.query_c_family_analysis(conn, PID, snapshot["snapshot_id"], table="relations", symbol_id="s2", direction="in") == [analysis["relations"][0]]
    assert conn.execute("SELECT status FROM graph_c_family_compilation_actions WHERE compilation_action_id='a-failed'").fetchone()[0] == "failed"
    assert conn.execute("SELECT COUNT(*) FROM graph_c_family_symbols WHERE compilation_action_id='a-failed'").fetchone()[0] == 0


def test_c_family_snapshot_queries_keep_same_named_internal_symbols_distinct(conn):
    actions = [
        {"compilation_action_id": "a", "translation_unit_id": "tu-a", "profile_id": "p", "file": "a.cc", "language": "cpp"},
        {"compilation_action_id": "b", "translation_unit_id": "tu-b", "profile_id": "p", "file": "b.cc", "language": "cpp"},
    ]
    analysis = {
        "actions": actions,
        "results": [{"status": "ok", "action": action} for action in actions],
        "symbols": [
            {"symbol_id": "helper-a", "compilation_action_id": "a", "profile_id": "p", "name": "helper", "qualified_name": "helper", "kind": "FunctionDecl", "signature": "int ()", "file": "a.cc", "lineno": 1, "is_definition": True},
            {"symbol_id": "helper-b", "compilation_action_id": "b", "profile_id": "p", "name": "helper", "qualified_name": "helper", "kind": "FunctionDecl", "signature": "int ()", "file": "b.cc", "lineno": 1, "is_definition": True},
        ],
        "occurrences": [],
        "relations": [
            {"relation_id": "call-a", "compilation_action_id": "a", "relation_type": "calls", "source_symbol_id": "from-a", "target_symbol_id": "helper-a", "source_file": "a.cc", "target_file": "", "direction": "out", "resolution": "resolved", "condition_ref": ""},
            {"relation_id": "call-b", "compilation_action_id": "b", "relation_type": "calls", "source_symbol_id": "from-b", "target_symbol_id": "helper-b", "source_file": "b.cc", "target_file": "", "direction": "out", "resolution": "resolved", "condition_ref": ""},
        ],
        "diagnostics": [],
    }
    snapshot = store.create_graph_snapshot(
        conn, PID, snapshot_id="internal-symbols", commit_sha="abc", snapshot_kind="full", c_family_analysis=analysis,
    )
    store.index_c_family_analysis(conn, PID, snapshot["snapshot_id"], analysis)
    conn.commit()
    assert store.query_c_family_analysis(
        conn, PID, snapshot["snapshot_id"], table="relations", symbol_id="helper-a", direction="in",
    ) == [analysis["relations"][0]]


def test_schema_migration_is_idempotent(conn):
    _ensure_schema(conn)
    _ensure_schema(conn)

    table_names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "graph_snapshots",
        "graph_snapshot_refs",
        "graph_ref_events",
        "graph_nodes_index",
        "graph_edges_index",
        "graph_drift_ledger",
        "pending_scope_reconcile",
        "reconcile_run_metrics",
        "graph_current_full_build_claim_history",
        "graph_reconcile_manager_generations",
    }.issubset(table_names)
    snapshot_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(graph_snapshots)").fetchall()
    }
    assert {"ref_name", "branch_ref"}.issubset(snapshot_columns)

    version = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    assert version["value"] == str(db.SCHEMA_VERSION)


def test_dev_component_schema_guard_is_exact_verify_only(conn, monkeypatch):
    monkeypatch.setenv("AMING_CLAW_RUNTIME_PLANE", "stable")
    store.ensure_schema(conn)
    conn.execute("CREATE TABLE graph_unowned_extra (id TEXT PRIMARY KEY)")
    conn.commit()
    before = conn.total_changes
    monkeypatch.setenv("AMING_CLAW_RUNTIME_PLANE", "dev")
    with pytest.raises(db.DevRuntimeSchemaVerificationError):
        store.ensure_schema(conn)
    assert conn.total_changes == before


def test_active_graph_effects_bind_to_durable_database_world_not_environment(
    conn, monkeypatch
):
    """A dev DB cannot activate even if an outer caller claims stable."""
    store.ensure_schema(conn)
    candidate = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="dev-candidate-only",
        commit_sha="dev-commit",
        snapshot_kind="full",
    )
    monkeypatch.setenv("AMING_CLAW_RUNTIME_PLANE", "stable")
    monkeypatch.setattr(
        db,
        "classify_graph_activation_connection",
        lambda _conn: {
            "runtime_plane": "dev",
            "active_graph_activation_allowed": False,
        },
    )

    with pytest.raises(ValueError, match="forbidden"):
        store.activate_graph_snapshot(
            conn,
            PID,
            candidate["snapshot_id"],
            auto_rebuild_projection=False,
        )

    assert store.get_active_graph_snapshot(conn, PID) is None
    assert conn.execute(
        "SELECT COUNT(*) FROM graph_ref_events WHERE project_id = ?", (PID,)
    ).fetchone()[0] == 0
    # Candidate construction remains a legal dev-world operation.
    assert store.get_graph_snapshot(conn, PID, candidate["snapshot_id"])["status"] == "candidate"


def test_verified_dev_world_can_activate_only_its_local_project(conn, monkeypatch):
    project_id = "aming-claw"
    store.ensure_schema(conn)
    candidate = store.create_graph_snapshot(
        conn,
        project_id,
        snapshot_id="dev-world-local-candidate",
        commit_sha="dev-world-local-commit",
        snapshot_kind="full",
    )
    monkeypatch.setattr(
        db,
        "classify_graph_activation_connection",
        lambda _conn: {
            "runtime_plane": "dev",
            "active_graph_activation_allowed": True,
            "classification_reason": "verified_dev_cow_successor_receipt_history",
            "world_id": "ac-dev",
            "project_id": project_id,
            "port": 40008,
            "cow_successor_verified": True,
            "source_checkout_verified": True,
            "live_runtime_custody_verified": True,
        },
    )

    activated = store.activate_graph_snapshot(
        conn,
        project_id,
        candidate["snapshot_id"],
        auto_rebuild_projection=False,
    )

    assert activated["snapshot_id"] == candidate["snapshot_id"]
    assert store.get_active_graph_snapshot(conn, project_id)["snapshot_id"] == (
        candidate["snapshot_id"]
    )

    foreign = store.create_graph_snapshot(
        conn,
        "other-project",
        snapshot_id="foreign-project-candidate",
        commit_sha="foreign-project-commit",
        snapshot_kind="full",
    )
    with pytest.raises(ValueError, match="classified AC-dev world"):
        store.activate_graph_snapshot(
            conn,
            "other-project",
            foreign["snapshot_id"],
            auto_rebuild_projection=False,
        )
    assert store.get_active_graph_snapshot(conn, "other-project") is None


def test_dev_process_rejects_stable_classified_database_cross_world(
    conn,
    monkeypatch,
):
    store.ensure_schema(conn)
    candidate = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="stable-cross-world-candidate",
        commit_sha="stable-cross-world-commit",
        snapshot_kind="full",
    )
    monkeypatch.setenv("AMING_CLAW_RUNTIME_PLANE", "dev")

    with pytest.raises(ValueError, match="across runtime worlds"):
        store.activate_graph_snapshot(
            conn,
            PID,
            candidate["snapshot_id"],
            auto_rebuild_projection=False,
            schema_ready=True,
        )

    assert conn.execute(
        "SELECT COUNT(*) FROM graph_snapshot_refs WHERE project_id = ?",
        (PID,),
    ).fetchone()[0] == 0


def test_active_graph_effects_reject_missing_or_unknown_database_world_before_refs(
    conn,
    monkeypatch,
):
    store.ensure_schema(conn)
    candidate = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="unknown-world-candidate",
        commit_sha="unknown-commit",
        snapshot_kind="full",
    )
    monkeypatch.setattr(
        db,
        "classify_graph_activation_connection",
        lambda _conn: {
            "runtime_plane": "unknown",
            "active_graph_activation_allowed": False,
        },
    )

    with pytest.raises(ValueError, match="forbidden"):
        store.activate_graph_snapshot(
            conn,
            PID,
            candidate["snapshot_id"],
            auto_rebuild_projection=False,
            schema_ready=True,
        )

    assert store.get_active_graph_snapshot(conn, PID) is None
    assert conn.execute(
        "SELECT COUNT(*) FROM graph_ref_events WHERE project_id = ?", (PID,)
    ).fetchone()[0] == 0


@pytest.mark.parametrize("bound_project", [None, "other-project"])
def test_stable_activation_requires_exact_bound_project_before_all_effects(
    conn, monkeypatch, bound_project,
):
    store.ensure_schema(conn)
    candidate = store.create_graph_snapshot(
        conn, PID, snapshot_id="bound-project-candidate", commit_sha="a" * 40,
        snapshot_kind="full",
    )
    conn.commit()
    monkeypatch.setattr(db, "classify_graph_activation_connection", lambda _conn: {
        "runtime_plane": "stable", "active_graph_activation_allowed": True,
        "project_id": bound_project,
    })
    before = tuple(conn.iterdump())
    changes = conn.total_changes
    with pytest.raises(ValueError, match="classified project"):
        store.activate_graph_snapshot(conn, PID, candidate["snapshot_id"])
    assert conn.total_changes == changes
    assert tuple(conn.iterdump()) == before
    assert store.get_graph_snapshot(conn, PID, candidate["snapshot_id"])["status"] == "candidate"


def _generation(
    suffix: str,
    *,
    manager_pid: int,
    prior_manager_pid: int = 0,
    prior_process_start_identity: str = "",
    observed_prior_generation_id: str | None = None,
) -> dict[str, object]:
    timestamp_digit = suffix[-1] if suffix[-1].isdigit() else "9"
    if observed_prior_generation_id is None and prior_manager_pid:
        observed_prior_generation_id = (
            f"generation-{max(1, int(timestamp_digit) - 1)}"
        )
    return {
        "generation_id": f"generation-{suffix}",
        "manager_pid": manager_pid,
        "manager_started_at": f"2026-08-10T00:00:0{timestamp_digit}Z",
        "process_start_identity": f"process-start-{suffix}",
        "manager_start_identity": f"manager-start-{suffix}",
        "lock_identity": f"lock-{suffix}",
        "prior_manager_pid": prior_manager_pid,
        "observed_prior_generation_id": observed_prior_generation_id or "",
        "prior_process_start_identity": prior_process_start_identity,
        "prior_pid_death_method": "esrch" if prior_manager_pid else "",
        "prior_pid_death_verified_at": (
            f"2026-08-10T00:00:1{timestamp_digit}Z" if prior_manager_pid else ""
        ),
        "certified_at": f"2026-08-10T00:00:2{timestamp_digit}Z",
    }


def _terminalization_proof_fixture(
    conn,
    *,
    suffix="one",
    source_scope=None,
    replacement_scope=None,
    replacement_status="candidate_ready",
):
    store.ensure_schema(conn)
    store.ensure_reconcile_metric_physical_identity_migration(conn)
    certificate = store.current_manager_generation_certificate(conn, PID)
    if not certificate:
        store.record_manager_generation_certificate(
            conn,
            PID,
            **_generation(f"terminal-{suffix}", manager_pid=5101),
        )
        certificate = store.current_manager_generation_certificate(conn, PID)
    commit_sha = hashlib.sha256(suffix.encode("utf-8")).hexdigest()[:40]
    source_run_id = f"stale-source-{suffix}"
    source_snapshot_id = f"full-stale-source-{suffix}"
    source_scope = {"project_id": PID, "task_id": "terminal-task", **(source_scope or {})}
    replacement_scope = source_scope if replacement_scope is None else replacement_scope
    store.record_reconcile_run_metric(
        conn,
        PID,
        run_id=source_run_id,
        snapshot_id=source_snapshot_id,
        commit_sha=commit_sha,
        parent_commit_sha="p" * 40,
        snapshot_kind="full",
        strategy="current_full_reconcile",
        graph_delta_mode="full_rebuild",
        status="running",
        changed_file_count=1,
        impacted_file_count=2,
        event_count=3,
        node_count=4,
        edge_count=5,
        elapsed_ms=6,
        trace_summary_path=f"trace-{suffix}.json",
        fallback_reason="",
        evidence={"phase": "build", "idempotency_scope": source_scope},
        created_at="2026-08-10T00:00:00Z",
    )
    conn.commit()

    replacement = _add_terminalization_candidate(
        conn,
        suffix=suffix,
        commit_sha=commit_sha,
        scope=replacement_scope,
        complete=replacement_status == "complete",
    )
    replacement_run_id = replacement["run_id"]
    replacement_snapshot_id = replacement["snapshot_id"]
    return {
        "certificate": certificate,
        "commit_sha": commit_sha,
        "source_run_id": source_run_id,
        "source_snapshot_id": source_snapshot_id,
        "replacement_run_id": replacement_run_id,
        "replacement_snapshot_id": replacement_snapshot_id,
        "source_scope": source_scope,
    }


def _add_terminalization_candidate(
    conn, *, suffix, commit_sha, scope, complete=False
):
    replacement_run_id = f"replacement-run-{suffix}"
    replacement_snapshot_id = f"full-replacement-{suffix}"
    owner = _claim_owner(f"terminal-{suffix}")
    claim = store.acquire_current_full_build_claim(
        conn,
        PID,
        run_id=replacement_run_id,
        snapshot_id=replacement_snapshot_id,
        commit_sha=commit_sha,
        metric_evidence={"idempotency_scope": scope},
        created_at="2026-08-10T00:00:01Z",
        **owner,
    )
    store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id=replacement_snapshot_id,
        commit_sha=commit_sha,
        snapshot_kind="full",
        graph_json={"deps_graph": {"nodes": []}},
        notes=json.dumps({"run_id": replacement_run_id}),
    )
    conn.commit()
    store.terminalize_current_full_build_claim(
        conn,
        PID,
        claim_id=claim["claim_id"],
        run_id=replacement_run_id,
        snapshot_id=replacement_snapshot_id,
        commit_sha=commit_sha,
        terminal_status="candidate_ready",
        manager_start_identity=owner["manager_start_identity"],
        metric_evidence={
            "phase": "candidate_ready",
            "claim_id": claim["claim_id"],
            "idempotency_scope": scope,
        },
        created_at="2026-08-10T00:00:02Z",
    )
    if complete:
        conn.execute("BEGIN IMMEDIATE")
        store.activate_graph_snapshot(
            conn,
            PID,
            replacement_snapshot_id,
            auto_rebuild_projection=False,
            schema_ready=True,
            post_commit_hooks=False,
        )
        store.record_reconcile_run_metric(
            conn,
            PID,
            run_id=replacement_run_id,
            snapshot_id=replacement_snapshot_id,
            commit_sha=commit_sha,
            snapshot_kind="full",
            strategy="current_full_reconcile",
            graph_delta_mode="full_rebuild",
            status="complete",
            evidence={
                "phase": "atomic_finalize_complete",
                "activate_requested": True,
                "idempotency_scope": scope,
                "request_id": f"request-{suffix}",
                "reconcile_event_id": 0,
                "provenance_id": "",
            },
            created_at="2026-08-10T00:00:03Z",
            schema_ready=True,
        )
        conn.commit()
    return {"run_id": replacement_run_id, "snapshot_id": replacement_snapshot_id}


def _insert_terminalization_overlay(
    conn,
    fixture,
    *,
    suffix="one",
    ledger_overrides=None,
    timeline_overrides=None,
):
    task_timeline.ensure_schema(conn)
    conn.commit()
    proof = store.reconcile_run_terminalization_proof(
        conn,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
        manager_certificate=fixture["certificate"],
    )
    receipt = store.reconcile_run_terminalization_safe_receipt(proof)
    sealed = json.loads(proof._canonical)
    terminalization_id = f"terminalization-{suffix}"
    terminalization_id_sha256 = store._stable_sha256(
        ["terminalization_id", terminalization_id]
    )
    conn.execute("BEGIN IMMEDIATE")
    event = task_timeline.record_reconcile_run_terminalization_event(
        conn,
        project_id=PID,
        backlog_id=f"terminalization-backlog-{suffix}",
        task_id=f"terminalization-task-{suffix}",
        commit_sha=fixture["commit_sha"],
        terminalization_id_sha256=terminalization_id_sha256,
        source_identity_sha256=receipt["source_identity_sha256"],
        source_fingerprint=receipt["source_fingerprint"],
        replacement_identity_sha256=receipt["replacement_identity_sha256"],
        replacement_fingerprint=receipt["replacement_fingerprint"],
        manager_certificate_hash=receipt["manager_certificate_hash"],
        proof_sha256=receipt["proof_sha256"],
    )
    for field, value in (timeline_overrides or {}).items():
        assert field in {"actor", "status", "created_at", "payload_json"}
        conn.execute(
            f"UPDATE task_timeline_events SET {field}=? WHERE id=?",
            (value, event["id"]),
        )
    timeline = dict(conn.execute(
        "SELECT * FROM task_timeline_events WHERE id=?", (event["id"],)
    ).fetchone())
    source = sealed["source"]
    replacement = sealed["replacement"]
    ledger = {
        "terminalization_id": terminalization_id,
        "project_id": PID,
        "source_run_id": source["run_id"],
        "source_snapshot_id": source["snapshot_id"],
        "source_metric_identity_sequence": source["metric_identity_sequence"],
        "source_raw_status": source["raw_status"],
        "source_fingerprint": source["fingerprint"],
        "replacement_proof_kind": replacement["proof_kind"],
        "replacement_run_id": replacement["run_id"],
        "replacement_snapshot_id": replacement["snapshot_id"],
        "replacement_metric_identity_sequence": replacement[
            "metric_identity_sequence"
        ],
        "replacement_raw_status": replacement["raw_status"],
        "replacement_fingerprint": replacement["fingerprint"],
        "manager_certificate_id": sealed["manager_certificate"]["certificate_id"],
        "manager_certificate_hash": sealed["manager_certificate"][
            "certificate_hash"
        ],
        "timeline_event_id": event["id"],
        "timeline_event_hash": store._terminalization_timeline_hash(timeline),
        "terminal_status": "terminalized_stale",
        "created_at": timeline["created_at"],
        "ledger_hash": "",
    }
    ledger.update(ledger_overrides or {})
    ledger["ledger_hash"] = store._terminalization_ledger_hash(ledger)
    columns = list(ledger)
    conn.execute(
        "INSERT INTO graph_reconcile_run_terminalizations "
        f"({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        [ledger[column] for column in columns],
    )
    conn.commit()
    return {"event": event, "ledger": ledger, "receipt": receipt}


def test_terminalization_append_is_atomic_safe_and_replay_zero_write(conn):
    fixture = _terminalization_proof_fixture(conn, suffix="atomic-append")
    task_timeline.ensure_schema(conn)
    conn.commit()
    statements = []
    conn.set_trace_callback(statements.append)

    first = store.record_reconcile_run_terminalization(
        conn,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
        backlog_id="terminalization-backlog-atomic",
        task_id="terminalization-task-atomic",
        manager_certificate=fixture["certificate"],
    )
    conn.set_trace_callback(None)

    assert first["writes_performed"] is True
    assert first["replayed"] is False
    assert set(first) == {
        "schema_version",
        "terminalization_id_sha256",
        "source_identity_sha256",
        "replacement_identity_sha256",
        "replacement_count",
        "manager_certificate_hash",
        "ledger_hash",
        "timeline_event_hash",
        "writes_performed",
        "replayed",
        "server_derived",
    }
    serialized = json.dumps(first, sort_keys=True)
    for raw in (
        fixture["source_run_id"],
        fixture["source_snapshot_id"],
        fixture["replacement_run_id"],
        fixture["replacement_snapshot_id"],
        "trace-atomic-append.json",
        "terminalization-backlog-atomic",
        "terminalization-task-atomic",
    ):
        assert raw not in serialized
    assert conn.execute(
        "SELECT status FROM reconcile_run_metrics WHERE project_id=? AND run_id=? "
        "AND snapshot_id=?",
        (PID, fixture["source_run_id"], fixture["source_snapshot_id"]),
    ).fetchone()["status"] == "running"
    assert conn.execute(
        "SELECT COUNT(*) FROM graph_reconcile_run_terminalizations"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM task_timeline_events "
        "WHERE event_type='graph.reconcile_run_terminalized'"
    ).fetchone()[0] == 1
    event = task_timeline.list_events(
        conn,
        PID,
        event_kind="reconcile_terminalization",
        limit=10,
    )[0]
    assert event["status"] == "recorded"
    assert task_timeline.is_protected_close_evidence(event) is False
    begin_index = statements.index("BEGIN IMMEDIATE")
    commit_index = statements.index("COMMIT", begin_index)
    assert not any(
        statement.lstrip().upper().startswith(("CREATE ", "ALTER ", "DROP "))
        for statement in statements[begin_index:commit_index]
    )

    before_changes = conn.total_changes
    before_rows = tuple(conn.execute(
        "SELECT * FROM graph_reconcile_run_terminalizations"
    ).fetchall())
    before_events = tuple(conn.execute(
        "SELECT * FROM task_timeline_events "
        "WHERE event_type='graph.reconcile_run_terminalized'"
    ).fetchall())
    replay = store.record_reconcile_run_terminalization(
        conn,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
        backlog_id="terminalization-backlog-atomic",
        task_id="terminalization-task-atomic",
        manager_certificate=fixture["certificate"],
    )

    assert replay == {**first, "writes_performed": False, "replayed": True}
    assert conn.total_changes == before_changes
    assert tuple(conn.execute(
        "SELECT * FROM graph_reconcile_run_terminalizations"
    ).fetchall()) == before_rows
    assert tuple(conn.execute(
        "SELECT * FROM task_timeline_events "
        "WHERE event_type='graph.reconcile_run_terminalized'"
    ).fetchall()) == before_events
    assert conn.in_transaction is False


def test_terminalization_append_accepts_generation_quiesced_source_without_replacement(
    conn,
):
    fixture = _terminalization_proof_fixture(conn, suffix="generation-quiesced")
    conn.execute(
        "DELETE FROM reconcile_run_metrics WHERE project_id=? AND run_id=?",
        (PID, fixture["replacement_run_id"]),
    )
    task_timeline.ensure_schema(conn)
    conn.commit()

    first = store.record_reconcile_run_terminalization(
        conn,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
        backlog_id="terminalization-backlog-generation-quiesced",
        task_id="terminalization-task-generation-quiesced",
        manager_certificate=fixture["certificate"],
    )

    assert first["writes_performed"] is True
    assert first["replayed"] is False
    assert first["replacement_count"] == 0
    ledger = dict(conn.execute(
        "SELECT * FROM graph_reconcile_run_terminalizations "
        "WHERE project_id=? AND source_run_id=? AND source_snapshot_id=?",
        (PID, fixture["source_run_id"], fixture["source_snapshot_id"]),
    ).fetchone())
    assert ledger["replacement_proof_kind"] == "manager_generation"
    assert store.reconcile_run_terminalization_overlay(
        conn,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
    )["effective_status"] == "terminalized_stale"

    _add_terminalization_candidate(
        conn,
        suffix="generation-quiesced-later",
        commit_sha=fixture["commit_sha"],
        scope=fixture["source_scope"],
    )
    assert store.reconcile_run_terminalization_overlay(
        conn,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
    )["replacement_count"] == 0

    store.record_manager_generation_certificate(
        conn,
        PID,
        **_generation(
            "generation-quiesced-next",
            manager_pid=5102,
            prior_manager_pid=int(fixture["certificate"]["manager_pid"]),
            prior_process_start_identity=str(
                fixture["certificate"]["process_start_identity"]
            ),
            observed_prior_generation_id=str(
                fixture["certificate"]["generation_id"]
            ),
        ),
    )
    next_certificate = store.current_manager_generation_certificate(conn, PID)
    before_changes = conn.total_changes
    replay = store.record_reconcile_run_terminalization(
        conn,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
        backlog_id="terminalization-backlog-generation-quiesced",
        task_id="terminalization-task-generation-quiesced",
        manager_certificate=next_certificate,
    )
    assert replay == {**first, "writes_performed": False, "replayed": True}
    assert conn.total_changes == before_changes


@pytest.mark.parametrize("fault_stage", ["after_timeline", "after_ledger", "before_commit"])
def test_terminalization_append_fault_rolls_back_both_rows(
    conn, monkeypatch, fault_stage
):
    fixture = _terminalization_proof_fixture(
        conn, suffix=f"atomic-fault-{fault_stage}"
    )
    task_timeline.ensure_schema(conn)
    conn.commit()
    source_before = dict(conn.execute(
        "SELECT * FROM reconcile_run_metrics WHERE project_id=? AND run_id=? "
        "AND snapshot_id=?",
        (PID, fixture["source_run_id"], fixture["source_snapshot_id"]),
    ).fetchone())

    def injected(stage, connection):
        if stage == fault_stage:
            if stage == "before_commit":
                connection.execute(
                    "UPDATE reconcile_run_metrics SET evidence_json='{}' "
                    "WHERE project_id=? AND run_id=? AND snapshot_id=?",
                    (PID, fixture["source_run_id"], fixture["source_snapshot_id"]),
                )
            raise RuntimeError(f"injected-{stage}")

    monkeypatch.setattr(store, "_reconcile_run_terminalization_append_fault", injected)
    with pytest.raises(RuntimeError, match=f"injected-{fault_stage}"):
        store.record_reconcile_run_terminalization(
            conn,
            PID,
            run_id=fixture["source_run_id"],
            snapshot_id=fixture["source_snapshot_id"],
            backlog_id="terminalization-backlog-fault",
            task_id="terminalization-task-fault",
            manager_certificate=fixture["certificate"],
        )

    assert conn.in_transaction is False
    assert conn.execute(
        "SELECT COUNT(*) FROM graph_reconcile_run_terminalizations"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM task_timeline_events "
        "WHERE event_type='graph.reconcile_run_terminalized'"
    ).fetchone()[0] == 0
    assert dict(conn.execute(
        "SELECT * FROM reconcile_run_metrics WHERE project_id=? AND run_id=? "
        "AND snapshot_id=?",
        (PID, fixture["source_run_id"], fixture["source_snapshot_id"]),
    ).fetchone()) == source_before


def test_terminalization_replay_accepts_new_live_generation_and_keeps_old_author(
    conn,
):
    fixture = _terminalization_proof_fixture(conn, suffix="atomic-restart")
    task_timeline.ensure_schema(conn)
    conn.commit()
    first = store.record_reconcile_run_terminalization(
        conn,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
        backlog_id="terminalization-backlog-restart",
        task_id="terminalization-task-restart",
        manager_certificate=fixture["certificate"],
    )
    original = dict(conn.execute(
        "SELECT * FROM graph_reconcile_run_terminalizations"
    ).fetchone())
    prior = fixture["certificate"]
    store.record_manager_generation_certificate(
        conn,
        PID,
        **_generation(
            "terminal-restart-2",
            manager_pid=5202,
            prior_manager_pid=int(prior["manager_pid"]),
            prior_process_start_identity=str(prior["process_start_identity"]),
            observed_prior_generation_id=str(prior["generation_id"]),
        ),
    )
    conn.commit()
    newer = store.current_manager_generation_certificate(conn, PID)
    before_changes = conn.total_changes

    replay = store.record_reconcile_run_terminalization(
        conn,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
        backlog_id="terminalization-backlog-restart",
        task_id="terminalization-task-restart",
        manager_certificate=newer,
    )

    assert replay == {**first, "writes_performed": False, "replayed": True}
    assert conn.total_changes == before_changes
    assert dict(conn.execute(
        "SELECT * FROM graph_reconcile_run_terminalizations"
    ).fetchone()) == original
    with pytest.raises(
        store.ReconcileRunTerminalizationProofError,
        match="terminalization_replay_scope_mismatch",
    ):
        store.record_reconcile_run_terminalization(
            conn,
            PID,
            run_id=fixture["source_run_id"],
            snapshot_id=fixture["source_snapshot_id"],
            backlog_id="foreign-backlog",
            task_id="terminalization-task-restart",
            manager_certificate=newer,
        )
    assert conn.total_changes == before_changes


def test_terminalization_two_connections_converge_on_one_append(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path)
    db_path = tmp_path / "terminalization-converge.sqlite"
    setup = _file_connection(db_path)
    fixture = _terminalization_proof_fixture(setup, suffix="atomic-converge")
    task_timeline.ensure_schema(setup)
    setup.commit()
    setup.close()
    barrier = threading.Barrier(2)

    def append_once():
        connection = sqlite3.connect(db_path, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            barrier.wait(timeout=2)
            return store.record_reconcile_run_terminalization(
                connection,
                PID,
                run_id=fixture["source_run_id"],
                snapshot_id=fixture["source_snapshot_id"],
                backlog_id="terminalization-backlog-converge",
                task_id="terminalization-task-converge",
                manager_certificate=fixture["certificate"],
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: append_once(), range(2)))

    assert sorted(result["writes_performed"] for result in results) == [False, True]
    assert sorted(result["replayed"] for result in results) == [False, True]
    before_bytes = db_path.read_bytes()
    check = sqlite3.connect(db_path)
    check.row_factory = sqlite3.Row
    try:
        assert check.execute(
            "SELECT COUNT(*) FROM graph_reconcile_run_terminalizations"
        ).fetchone()[0] == 1
        assert check.execute(
            "SELECT COUNT(*) FROM task_timeline_events "
            "WHERE event_type='graph.reconcile_run_terminalized'"
        ).fetchone()[0] == 1
        before_changes = check.total_changes
        replay = store.record_reconcile_run_terminalization(
            check,
            PID,
            run_id=fixture["source_run_id"],
            snapshot_id=fixture["source_snapshot_id"],
            backlog_id="terminalization-backlog-converge",
            task_id="terminalization-task-converge",
            manager_certificate=fixture["certificate"],
        )
        assert replay["writes_performed"] is False
        assert replay["replayed"] is True
        assert check.total_changes == before_changes
    finally:
        check.close()
    assert db_path.read_bytes() == before_bytes


def test_terminalization_precommit_state_drift_rolls_back(conn, monkeypatch):
    fixture = _terminalization_proof_fixture(conn, suffix="atomic-drift")
    task_timeline.ensure_schema(conn)
    conn.commit()

    def mutate_after_ledger(stage, connection):
        if stage == "after_ledger":
            connection.execute(
                "UPDATE reconcile_run_metrics SET elapsed_ms=elapsed_ms+1 "
                "WHERE project_id=? AND run_id=? AND snapshot_id=?",
                (PID, fixture["source_run_id"], fixture["source_snapshot_id"]),
            )

    monkeypatch.setattr(
        store, "_reconcile_run_terminalization_append_fault", mutate_after_ledger
    )
    with pytest.raises(
        store.ReconcileRunTerminalizationProofError,
        match="terminalization_state_changed",
    ):
        store.record_reconcile_run_terminalization(
            conn,
            PID,
            run_id=fixture["source_run_id"],
            snapshot_id=fixture["source_snapshot_id"],
            backlog_id="terminalization-backlog-drift",
            task_id="terminalization-task-drift",
            manager_certificate=fixture["certificate"],
        )
    assert conn.execute(
        "SELECT elapsed_ms FROM reconcile_run_metrics WHERE project_id=? "
        "AND run_id=? AND snapshot_id=?",
        (PID, fixture["source_run_id"], fixture["source_snapshot_id"]),
    ).fetchone()[0] == 6
    assert conn.execute(
        "SELECT COUNT(*) FROM graph_reconcile_run_terminalizations"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM task_timeline_events "
        "WHERE event_type='graph.reconcile_run_terminalized'"
    ).fetchone()[0] == 0


def test_terminalization_proof_kernel_seals_exact_candidate_and_safe_receipt(conn):
    fixture = _terminalization_proof_fixture(conn)

    proof = store.reconcile_run_terminalization_proof(
        conn,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
        manager_certificate=fixture["certificate"],
    )
    receipt = store.reconcile_run_terminalization_safe_receipt(proof)

    assert receipt["schema_version"] == "reconcile_run_terminalization.safe_receipt.v1"
    assert receipt["source_fingerprint"].startswith("sha256:")
    assert receipt["replacement_fingerprint"].startswith("sha256:")
    assert receipt["proof_sha256"].startswith("sha256:")
    assert receipt["replacement_count"] == 1
    serialized = json.dumps(receipt, sort_keys=True)
    assert fixture["source_run_id"] not in serialized
    assert fixture["source_snapshot_id"] not in serialized
    assert fixture["replacement_run_id"] not in serialized
    assert fixture["replacement_snapshot_id"] not in serialized
    assert "trace-one.json" not in serialized
    assert "manager-start-terminal-one" not in serialized
    assert fixture["certificate"]["certificate_id"] not in serialized
    assert "running" not in serialized
    assert "candidate_ready" not in serialized
    with pytest.raises(TypeError, match="sealed terminalization proof"):
        store.reconcile_run_terminalization_safe_receipt(dict(receipt))
    proof._seal = "sha256:" + "0" * 64
    with pytest.raises(TypeError, match="sealed terminalization proof invalid"):
        store.reconcile_run_terminalization_safe_receipt(proof)


def test_terminalization_proof_requires_completed_identity_migration(conn):
    store.ensure_schema(conn)
    with pytest.raises(
        store.ReconcileRunTerminalizationProofError,
        match="terminalization_metric_identity_migration_incomplete",
    ):
        store.reconcile_run_terminalization_proof(
            conn,
            PID,
            run_id="unmigrated-run",
            snapshot_id="unmigrated-snapshot",
            manager_certificate={},
        )


def test_terminalization_ledger_schema_is_exact_pk_and_append_only(conn):
    store.ensure_schema(conn)
    digest = "sha256:" + "a" * 64
    row = {
        "terminalization_id": "terminalization-one",
        "project_id": PID,
        "source_run_id": "source-run",
        "source_snapshot_id": "source-snapshot",
        "source_metric_identity_sequence": 1,
        "source_raw_status": "running",
        "source_fingerprint": digest,
        "replacement_run_id": "replacement-run",
        "replacement_snapshot_id": "replacement-snapshot",
        "replacement_metric_identity_sequence": 2,
        "replacement_raw_status": "candidate_ready",
        "replacement_fingerprint": digest,
        "manager_certificate_id": "certificate-one",
        "manager_certificate_hash": digest,
        "timeline_event_id": 1,
        "timeline_event_hash": digest,
        "terminal_status": "terminalized_stale",
        "created_at": "2026-08-10T00:00:02Z",
        "ledger_hash": digest,
    }
    columns = list(row)
    sql = (
        "INSERT INTO graph_reconcile_run_terminalizations "
        f"({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
    )
    conn.execute(sql, [row[key] for key in columns])
    conn.commit()
    before = dict(conn.execute(
        "SELECT * FROM graph_reconcile_run_terminalizations"
    ).fetchone())

    with pytest.raises(sqlite3.IntegrityError, match="append_only"):
        conn.execute(
            "UPDATE graph_reconcile_run_terminalizations SET terminal_status='x'"
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append_only"):
        conn.execute("DELETE FROM graph_reconcile_run_terminalizations")
    conn.rollback()
    replacement = {**row, "timeline_event_id": 2}
    with pytest.raises(sqlite3.IntegrityError, match="identity_conflict"):
        conn.execute(
            sql.replace("INSERT INTO", "INSERT OR REPLACE INTO"),
            [replacement[key] for key in columns],
        )
    conn.rollback()
    assert dict(conn.execute(
        "SELECT * FROM graph_reconcile_run_terminalizations"
    ).fetchone()) == before
    malformed = {
        **row,
        "terminalization_id": "terminalization-malformed",
        "source_run_id": "source-run-malformed",
        "source_snapshot_id": "source-snapshot-malformed",
        "ledger_hash": "sha256:" + "A" * 64,
    }
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute(sql, [malformed[key] for key in columns])
    conn.rollback()


def test_terminalization_ledger_schema_migrates_replacement_proof_kind(conn):
    store.ensure_schema(conn)
    conn.execute(
        "ALTER TABLE graph_reconcile_run_terminalizations "
        "DROP COLUMN replacement_proof_kind"
    )
    conn.commit()

    store.ensure_schema(conn)

    columns = {
        row["name"]: row
        for row in conn.execute(
            "PRAGMA table_info(graph_reconcile_run_terminalizations)"
        ).fetchall()
    }
    assert columns["replacement_proof_kind"]["dflt_value"] == "'materialized'"
    assert columns["replacement_proof_kind"]["notnull"] == 1


def test_terminalization_overlay_reader_missing_is_safe_and_read_only(conn):
    store.ensure_schema(conn)
    conn.commit()
    before_changes = conn.total_changes

    result = store.reconcile_run_terminalization_overlay(
        conn,
        PID,
        run_id="missing-run",
        snapshot_id="missing-snapshot",
    )

    assert result == {
        "schema_version": "reconcile_run_terminalization.overlay.v1",
        "valid": False,
        "effective_status": "unknown",
        "is_terminal": False,
        "status_reason_code": "terminalization_overlay_missing",
    }
    assert conn.total_changes == before_changes
    assert conn.in_transaction is False


def test_terminalization_overlay_reader_missing_keeps_running_source_visible(conn):
    fixture = _terminalization_proof_fixture(conn, suffix="overlay-missing-running")

    result = store.reconcile_run_terminalization_overlay(
        conn,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
    )

    assert result == {
        "schema_version": "reconcile_run_terminalization.overlay.v1",
        "valid": False,
        "effective_status": "running",
        "is_terminal": False,
        "status_reason_code": "terminalization_overlay_missing",
    }


def test_terminalization_overlay_reader_revalidates_historical_proof_and_timeline(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path)
    db_path = tmp_path / "terminalization-overlay.sqlite"
    setup = _file_connection(db_path)
    fixture = _terminalization_proof_fixture(setup, suffix="overlay-valid")
    inserted = _insert_terminalization_overlay(
        setup, fixture, suffix="overlay-valid"
    )
    store.record_manager_generation_certificate(
        setup,
        PID,
        **_generation(
            "2",
            manager_pid=5202,
            prior_manager_pid=5101,
            prior_process_start_identity="process-start-terminal-overlay-valid",
            observed_prior_generation_id="generation-terminal-overlay-valid",
        ),
    )
    setup.close()

    reader = sqlite3.connect(db_path)
    reader.row_factory = sqlite3.Row
    before_bytes = db_path.read_bytes()
    before_changes = reader.total_changes
    before_schema = tuple(reader.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall())
    before_pragmas = tuple(
        reader.execute(f"PRAGMA {name}").fetchone()[0]
        for name in ("schema_version", "page_count", "freelist_count")
    )
    statements = []
    reader.set_trace_callback(statements.append)

    result = store.reconcile_run_terminalization_overlay(
        reader,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
    )
    reader.set_trace_callback(None)

    assert result["valid"] is True
    assert result["effective_status"] == "terminalized_stale"
    assert result["is_terminal"] is True
    assert result["status_reason_code"] == "terminalization_overlay_valid"
    assert result["ledger_hash"] == inserted["ledger"]["ledger_hash"]
    serialized = json.dumps(result, sort_keys=True)
    for raw in (
        fixture["source_run_id"],
        fixture["source_snapshot_id"],
        fixture["replacement_run_id"],
        fixture["replacement_snapshot_id"],
        inserted["ledger"]["terminalization_id"],
        inserted["event"]["backlog_id"],
        inserted["event"]["task_id"],
    ):
        assert raw not in serialized
    assert statements
    assert all(
        statement.lstrip().upper().startswith(("SELECT", "PRAGMA DATA_VERSION"))
        for statement in statements
    )
    assert reader.total_changes == before_changes
    assert reader.in_transaction is False
    assert tuple(reader.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()) == before_schema
    assert tuple(
        reader.execute(f"PRAGMA {name}").fetchone()[0]
        for name in ("schema_version", "page_count", "freelist_count")
    ) == before_pragmas
    reader.close()
    assert db_path.read_bytes() == before_bytes


def test_terminalization_overlay_reader_accepts_exact_active_replacement(conn):
    fixture = _terminalization_proof_fixture(
        conn,
        suffix="overlay-active",
        replacement_status="complete",
    )
    _insert_terminalization_overlay(conn, fixture, suffix="overlay-active")

    result = store.reconcile_run_terminalization_overlay(
        conn,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
    )

    assert result["valid"] is True
    assert result["effective_status"] == "terminalized_stale"


def test_terminalization_overlay_reader_detects_concurrent_database_change(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path)
    db_path = tmp_path / "terminalization-overlay-race.sqlite"
    setup = _file_connection(db_path)
    setup.execute("PRAGMA journal_mode=WAL")
    fixture = _terminalization_proof_fixture(setup, suffix="overlay-race")
    inserted = _insert_terminalization_overlay(setup, fixture, suffix="overlay-race")
    setup.close()
    reader = sqlite3.connect(db_path)
    reader.row_factory = sqlite3.Row
    baseline = store.reconcile_run_terminalization_overlay(
        reader,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
    )
    assert baseline["valid"] is True, baseline
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    before_injection = store.reconcile_run_terminalization_overlay(
        reader,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
    )
    assert before_injection["valid"] is True, before_injection
    original = store._terminalization_validate_timeline
    injected = False

    def inject_change(*args, **kwargs):
        nonlocal injected
        original(*args, **kwargs)
        if not injected:
            try:
                writer.execute(
                    "UPDATE task_timeline_events SET severity='external-change' "
                    "WHERE id=?",
                    (inserted["event"]["id"],),
                )
                writer.commit()
                injected = True
            except Exception as exc:
                raise AssertionError(f"concurrent writer failed: {exc}") from exc

    monkeypatch.setattr(store, "_terminalization_validate_timeline", inject_change)
    result = store.reconcile_run_terminalization_overlay(
        reader,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
    )

    assert injected is True, result
    assert result["valid"] is False
    assert result["effective_status"] == "running"
    assert result["status_reason_code"] == "terminalization_overlay_invalid"
    reader.close()
    writer.close()


@pytest.mark.parametrize(
    "timeline_overrides",
    [
        {"status": "accepted"},
        {"actor": "observer"},
        {"created_at": "2026-08-10 00:00:00Z"},
    ],
)
def test_terminalization_overlay_reader_rejects_exactly_hashed_non_neutral_timeline(
    conn, timeline_overrides
):
    suffix = f"overlay-timeline-{next(iter(timeline_overrides))}"
    fixture = _terminalization_proof_fixture(conn, suffix=suffix)
    _insert_terminalization_overlay(
        conn,
        fixture,
        suffix=suffix,
        timeline_overrides=timeline_overrides,
    )

    result = store.reconcile_run_terminalization_overlay(
        conn,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
    )

    assert result == {
        "schema_version": "reconcile_run_terminalization.overlay.v1",
        "valid": False,
        "effective_status": "running",
        "is_terminal": False,
        "status_reason_code": "terminalization_overlay_invalid",
    }


@pytest.mark.parametrize(
    "drift",
    [
        "source_metric",
        "replacement_metric",
        "replacement_companion",
        "timeline_payload",
        "source_materialized",
        "replacement_ambiguous",
    ],
)
def test_terminalization_overlay_reader_keeps_drifted_source_visible(
    conn, drift
):
    fixture = _terminalization_proof_fixture(conn, suffix=f"overlay-{drift}")
    inserted = _insert_terminalization_overlay(
        conn, fixture, suffix=f"overlay-{drift}"
    )
    if drift == "source_metric":
        conn.execute(
            "UPDATE reconcile_run_metrics SET evidence_json=? "
            "WHERE project_id=? AND run_id=? AND snapshot_id=?",
            (
                json.dumps({
                    "phase": "build-drifted",
                    "idempotency_scope": fixture["source_scope"],
                }),
                PID,
                fixture["source_run_id"],
                fixture["source_snapshot_id"],
            ),
        )
    elif drift == "replacement_metric":
        conn.execute(
            "UPDATE reconcile_run_metrics SET elapsed_ms=elapsed_ms+1 "
            "WHERE project_id=? AND run_id=? AND snapshot_id=?",
            (PID, fixture["replacement_run_id"], fixture["replacement_snapshot_id"]),
        )
    elif drift == "replacement_companion":
        store.snapshot_graph_path(
            PID, fixture["replacement_snapshot_id"]
        ).unlink()
    elif drift == "timeline_payload":
        conn.execute(
            "UPDATE task_timeline_events SET payload_json='{}' WHERE id=?",
            (inserted["event"]["id"],),
        )
    elif drift == "source_materialized":
        store.create_graph_snapshot(
            conn,
            PID,
            snapshot_id=fixture["source_snapshot_id"],
            commit_sha=fixture["commit_sha"],
            snapshot_kind="full",
            graph_json={"deps_graph": {"nodes": []}},
        )
    else:
        _add_terminalization_candidate(
            conn,
            suffix=f"overlay-{drift}-second",
            commit_sha=fixture["commit_sha"],
            scope=fixture["source_scope"],
        )
    conn.commit()

    result = store.reconcile_run_terminalization_overlay(
        conn,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
    )

    assert result == {
        "schema_version": "reconcile_run_terminalization.overlay.v1",
        "valid": False,
        "effective_status": "running",
        "is_terminal": False,
        "status_reason_code": "terminalization_overlay_invalid",
    }


@pytest.mark.parametrize(
    ("override", "expected_status"),
    [
        ({"source_fingerprint": "sha256:" + "0" * 64}, "running"),
        ({"replacement_fingerprint": "sha256:" + "1" * 64}, "running"),
        ({"manager_certificate_hash": "sha256:" + "2" * 64}, "running"),
        ({"timeline_event_hash": "sha256:" + "3" * 64}, "finalizing"),
    ],
)
def test_terminalization_overlay_reader_rejects_forged_ledger_without_throwing(
    conn, override, expected_status
):
    fixture = _terminalization_proof_fixture(
        conn,
        suffix=f"overlay-forged-{override[next(iter(override))][-1]}",
    )
    if expected_status == "finalizing":
        conn.execute(
            "UPDATE reconcile_run_metrics SET status='finalizing' "
            "WHERE project_id=? AND run_id=? AND snapshot_id=?",
            (PID, fixture["source_run_id"], fixture["source_snapshot_id"]),
        )
        conn.commit()
    _insert_terminalization_overlay(
        conn,
        fixture,
        suffix=f"overlay-forged-{override[next(iter(override))][-1]}",
        ledger_overrides=override,
    )

    result = store.reconcile_run_terminalization_overlay(
        conn,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
    )

    assert result["valid"] is False
    assert result["effective_status"] == expected_status
    assert result["is_terminal"] is False
    assert result["status_reason_code"] == "terminalization_overlay_invalid"


@pytest.mark.parametrize("raw_status", [" running ", "RUNNING", "candidate_ready"])
def test_terminalization_proof_requires_exact_raw_source_status(conn, raw_status):
    fixture = _terminalization_proof_fixture(conn, suffix=f"status-{len(raw_status)}")
    conn.execute(
        "UPDATE reconcile_run_metrics SET status=? "
        "WHERE project_id=? AND run_id=? AND snapshot_id=?",
        (raw_status, PID, fixture["source_run_id"], fixture["source_snapshot_id"]),
    )
    conn.commit()
    with pytest.raises(
        store.ReconcileRunTerminalizationProofError,
        match="terminalization_metric_status_invalid",
    ):
        store.reconcile_run_terminalization_proof(
            conn, PID, run_id=fixture["source_run_id"],
            snapshot_id=fixture["source_snapshot_id"],
            manager_certificate=fixture["certificate"],
        )


def test_terminalization_proof_accepts_finalizing_and_rejects_malformed_commit(conn):
    fixture = _terminalization_proof_fixture(conn, suffix="finalizing")
    source_key = (PID, fixture["source_run_id"], fixture["source_snapshot_id"])
    conn.execute(
        "UPDATE reconcile_run_metrics SET status='finalizing' "
        "WHERE project_id=? AND run_id=? AND snapshot_id=?",
        source_key,
    )
    conn.commit()
    proof = store.reconcile_run_terminalization_proof(
        conn, PID, run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
        manager_certificate=fixture["certificate"],
    )
    assert store.reconcile_run_terminalization_safe_receipt(proof)[
        "replacement_count"
    ] == 1

    conn.execute(
        "UPDATE reconcile_run_metrics SET commit_sha='not-a-commit' "
        "WHERE project_id=? AND run_id=? AND snapshot_id=?",
        source_key,
    )
    conn.commit()
    with pytest.raises(
        store.ReconcileRunTerminalizationProofError,
        match="terminalization_metric_identity_invalid",
    ):
        store.reconcile_run_terminalization_proof(
            conn, PID, run_id=fixture["source_run_id"],
            snapshot_id=fixture["source_snapshot_id"],
            manager_certificate=fixture["certificate"],
        )


@pytest.mark.parametrize(
    ("source_value", "replacement_value"),
    [(False, 0), (1, "1")],
)
def test_terminalization_proof_scope_is_type_exact(
    conn, source_value, replacement_value
):
    fixture = _terminalization_proof_fixture(
        conn,
        suffix=f"typed-{type(source_value).__name__}",
        source_scope={"typed": source_value},
        replacement_scope={
            "project_id": PID,
            "task_id": "terminal-task",
            "typed": replacement_value,
        },
    )
    proof = store.reconcile_run_terminalization_proof(
        conn, PID, run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
        manager_certificate=fixture["certificate"],
    )
    assert store.reconcile_run_terminalization_safe_receipt(proof)[
        "replacement_count"
    ] == 0


def test_terminalization_proof_replacement_zero_one_many_and_active_claim(conn):
    missing = _terminalization_proof_fixture(conn, suffix="missing")
    conn.execute(
        "DELETE FROM reconcile_run_metrics WHERE project_id=? AND run_id=?",
        (PID, missing["replacement_run_id"]),
    )
    conn.commit()
    missing_proof = store.reconcile_run_terminalization_proof(
        conn, PID, run_id=missing["source_run_id"],
        snapshot_id=missing["source_snapshot_id"],
        manager_certificate=missing["certificate"],
    )
    assert store.reconcile_run_terminalization_safe_receipt(missing_proof)[
        "replacement_count"
    ] == 0

    legacy_invalid = _terminalization_proof_fixture(
        conn, suffix="legacy-invalid-replacement"
    )
    conn.execute(
        "DELETE FROM reconcile_run_metrics WHERE project_id=? AND run_id=?",
        (PID, legacy_invalid["replacement_run_id"]),
    )
    store.record_reconcile_run_metric(
        conn,
        PID,
        run_id="legacy-candidate-without-claim",
        snapshot_id="full-legacy-candidate-without-claim",
        commit_sha=legacy_invalid["commit_sha"],
        snapshot_kind="full",
        strategy="current_full_reconcile",
        graph_delta_mode="full_rebuild",
        status="candidate_ready",
        evidence={
            "phase": "candidate_ready",
            "idempotency_scope": legacy_invalid["source_scope"],
        },
        created_at="2026-08-10T00:00:03Z",
    )
    conn.commit()
    legacy_proof = store.reconcile_run_terminalization_proof(
        conn, PID, run_id=legacy_invalid["source_run_id"],
        snapshot_id=legacy_invalid["source_snapshot_id"],
        manager_certificate=legacy_invalid["certificate"],
    )
    assert store.reconcile_run_terminalization_safe_receipt(legacy_proof)[
        "replacement_count"
    ] == 0

    same_generation = _terminalization_proof_fixture(
        conn, suffix="same-generation"
    )
    conn.execute(
        "DELETE FROM reconcile_run_metrics WHERE project_id=? AND run_id=?",
        (PID, same_generation["replacement_run_id"]),
    )
    conn.execute(
        "UPDATE reconcile_run_metrics SET created_at='2026-08-10T00:00:10Z' "
        "WHERE project_id=? AND run_id=? AND snapshot_id=?",
        (
            PID,
            same_generation["source_run_id"],
            same_generation["source_snapshot_id"],
        ),
    )
    conn.commit()
    with pytest.raises(
        store.ReconcileRunTerminalizationProofError,
        match="terminalization_source_not_generation_quiesced",
    ):
        store.reconcile_run_terminalization_proof(
            conn, PID, run_id=same_generation["source_run_id"],
            snapshot_id=same_generation["source_snapshot_id"],
            manager_certificate=same_generation["certificate"],
        )

    many = _terminalization_proof_fixture(conn, suffix="many")
    _add_terminalization_candidate(
        conn,
        suffix="many-second",
        commit_sha=many["commit_sha"],
        scope=many["source_scope"],
    )
    with pytest.raises(
        store.ReconcileRunTerminalizationProofError,
        match="terminalization_replacement_ambiguous",
    ):
        store.reconcile_run_terminalization_proof(
            conn, PID, run_id=many["source_run_id"],
            snapshot_id=many["source_snapshot_id"],
            manager_certificate=many["certificate"],
        )

    active = _terminalization_proof_fixture(
        conn, suffix="active", replacement_status="complete"
    )
    proof = store.reconcile_run_terminalization_proof(
        conn, PID, run_id=active["source_run_id"],
        snapshot_id=active["source_snapshot_id"],
        manager_certificate=active["certificate"],
    )
    assert store.reconcile_run_terminalization_safe_receipt(proof)[
        "replacement_count"
    ] == 1
    conn.execute(
        "INSERT INTO graph_current_full_build_claim_history "
        "(claim_id,project_id,snapshot_id,run_id,commit_sha,status,manager_epoch,"
        "manager_pid,manager_started_at,manager_start_identity,acquired_at) "
        "VALUES (?,?,?,?,?,'active',?,?,?,?,?)",
        (
            "foreign-active-claim", PID, active["replacement_snapshot_id"],
            "foreign-run", active["commit_sha"], "epoch", 999,
            "2026-08-10T00:00:04Z", "manager-foreign",
            "2026-08-10T00:00:04Z",
        ),
    )
    conn.commit()
    with pytest.raises(
        store.ReconcileRunTerminalizationProofError,
        match="terminalization_replacement_invalid",
    ):
        store.reconcile_run_terminalization_proof(
            conn, PID, run_id=active["source_run_id"],
            snapshot_id=active["source_snapshot_id"],
            manager_certificate=active["certificate"],
        )


def test_terminalization_proof_rejects_source_claim_materialization_and_bad_candidate(conn):
    fixture = _terminalization_proof_fixture(conn, suffix="source-fences")
    conn.execute(
        "INSERT INTO graph_current_full_build_claim_history "
        "(claim_id,project_id,snapshot_id,run_id,commit_sha,status,manager_epoch,"
        "manager_pid,manager_started_at,manager_start_identity,acquired_at) "
        "VALUES (?,?,?,?,?,'active',?,?,?,?,?)",
        (
            "source-active-claim", PID, fixture["source_snapshot_id"],
            "foreign-source-run", fixture["commit_sha"], "epoch", 999,
            "2026-08-10T00:00:04Z", "manager-source-foreign",
            "2026-08-10T00:00:04Z",
        ),
    )
    conn.commit()

    def prove():
        return store.reconcile_run_terminalization_proof(
            conn, PID, run_id=fixture["source_run_id"],
            snapshot_id=fixture["source_snapshot_id"],
            manager_certificate=fixture["certificate"],
        )

    with pytest.raises(
        store.ReconcileRunTerminalizationProofError,
        match="terminalization_source_not_stale",
    ):
        prove()
    conn.execute(
        "DELETE FROM graph_current_full_build_claim_history "
        "WHERE claim_id='source-active-claim'"
    )
    conn.execute(
        "INSERT INTO graph_snapshots "
        "(project_id,snapshot_id,commit_sha,snapshot_kind,status,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            PID, fixture["source_snapshot_id"], fixture["commit_sha"],
            "full", "candidate", "2026-08-10T00:00:00Z",
        ),
    )
    conn.commit()
    with pytest.raises(
        store.ReconcileRunTerminalizationProofError,
        match="terminalization_source_not_stale",
    ):
        prove()
    conn.execute(
        "DELETE FROM graph_snapshots WHERE project_id=? AND snapshot_id=?",
        (PID, fixture["source_snapshot_id"]),
    )
    conn.commit()
    store.snapshot_companion_dir(PID, fixture["source_snapshot_id"]).mkdir(
        parents=True
    )
    with pytest.raises(
        store.ReconcileRunTerminalizationProofError,
        match="terminalization_source_not_stale",
    ):
        prove()

    invalid = _terminalization_proof_fixture(conn, suffix="invalid-candidate")
    store.snapshot_graph_path(PID, invalid["replacement_snapshot_id"]).unlink()
    with pytest.raises(
        store.ReconcileRunTerminalizationProofError,
        match="terminalization_replacement_invalid",
    ):
        store.reconcile_run_terminalization_proof(
            conn, PID, run_id=invalid["source_run_id"],
            snapshot_id=invalid["source_snapshot_id"],
            manager_certificate=invalid["certificate"],
        )


def test_terminalization_proof_requires_exact_latest_certificate(conn):
    fixture = _terminalization_proof_fixture(conn, suffix="certificate")
    altered = {**fixture["certificate"], "sequence": True}
    with pytest.raises(
        store.ReconcileRunTerminalizationProofError,
        match="terminalization_manager_certificate_mismatch",
    ):
        store.reconcile_run_terminalization_proof(
            conn, PID, run_id=fixture["source_run_id"],
            snapshot_id=fixture["source_snapshot_id"],
            manager_certificate=altered,
        )

    store.record_manager_generation_certificate(
        conn,
        PID,
        **_generation(
            "2",
            manager_pid=5102,
            prior_manager_pid=5101,
            prior_process_start_identity="process-start-terminal-certificate",
            observed_prior_generation_id="generation-terminal-certificate",
        ),
    )
    with pytest.raises(
        store.ReconcileRunTerminalizationProofError,
        match="terminalization_manager_certificate_mismatch",
    ):
        store.reconcile_run_terminalization_proof(
            conn, PID, run_id=fixture["source_run_id"],
            snapshot_id=fixture["source_snapshot_id"],
            manager_certificate=fixture["certificate"],
        )
    current = store.current_manager_generation_certificate(conn, PID)
    proof = store.reconcile_run_terminalization_proof(
        conn, PID, run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
        manager_certificate=current,
    )
    assert store.reconcile_run_terminalization_safe_receipt(proof)[
        "manager_certificate_hash"
    ] == current["certificate_hash"]


def test_terminalization_proof_uses_strict_utc_instants_and_duplicate_free_json(conn):
    fixture = _terminalization_proof_fixture(conn, suffix="timestamp")
    source_key = (PID, fixture["source_run_id"], fixture["source_snapshot_id"])
    conn.execute(
        "UPDATE reconcile_run_metrics SET created_at='2026-08-10 00:00:00Z' "
        "WHERE project_id=? AND run_id=? AND snapshot_id=?",
        source_key,
    )
    conn.commit()
    with pytest.raises(
        store.ReconcileRunTerminalizationProofError,
        match="terminalization_metric_created_at_invalid",
    ):
        store.reconcile_run_terminalization_proof(
            conn, PID, run_id=fixture["source_run_id"],
            snapshot_id=fixture["source_snapshot_id"],
            manager_certificate=fixture["certificate"],
        )

    conn.execute(
        "UPDATE reconcile_run_metrics SET created_at='2026-08-10T00:00:00.900000Z' "
        "WHERE project_id=? AND run_id=? AND snapshot_id=?",
        source_key,
    )
    conn.execute(
        "UPDATE reconcile_run_metrics SET created_at='2026-08-10T00:00:00.10Z' "
        "WHERE project_id=? AND run_id=? AND snapshot_id=?",
        (PID, fixture["replacement_run_id"], fixture["replacement_snapshot_id"]),
    )
    conn.commit()
    proof = store.reconcile_run_terminalization_proof(
        conn, PID, run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
        manager_certificate=fixture["certificate"],
    )
    assert store.reconcile_run_terminalization_safe_receipt(proof)[
        "replacement_count"
    ] == 0

    conn.execute(
        "UPDATE reconcile_run_metrics SET created_at='2026-08-10T00:00:00Z', "
        "evidence_json=? WHERE project_id=? AND run_id=? AND snapshot_id=?",
        (
            '{"idempotency_scope":{},"idempotency_scope":{}}',
            *source_key,
        ),
    )
    conn.commit()
    with pytest.raises(
        store.ReconcileRunTerminalizationProofError,
        match="terminalization_metric_evidence_invalid",
    ):
        store.reconcile_run_terminalization_proof(
            conn, PID, run_id=fixture["source_run_id"],
            snapshot_id=fixture["source_snapshot_id"],
            manager_certificate=fixture["certificate"],
        )


def test_terminalization_source_fingerprint_binds_all_mutable_metric_bytes(conn):
    fixture = _terminalization_proof_fixture(conn, suffix="fingerprint")
    key = (PID, fixture["source_run_id"], fixture["source_snapshot_id"])

    def fingerprint():
        proof = store.reconcile_run_terminalization_proof(
            conn, PID, run_id=fixture["source_run_id"],
            snapshot_id=fixture["source_snapshot_id"],
            manager_certificate=fixture["certificate"],
        )
        return store.reconcile_run_terminalization_safe_receipt(proof)[
            "source_fingerprint"
        ]

    fingerprints = {fingerprint()}
    updates = [
        ("parent_commit_sha", "q" * 40),
        ("changed_file_count", 11),
        ("impacted_file_count", 12),
        ("event_count", 13),
        ("node_count", 14),
        ("edge_count", 15),
        ("elapsed_ms", 16),
        ("trace_summary_path", "private/fingerprint-trace.json"),
        ("fallback_reason", "fingerprint-fallback"),
        (
            "evidence_json",
            json.dumps(
                {"phase": "build", "idempotency_scope": fixture["source_scope"]},
                ensure_ascii=False,
                indent=1,
            ),
        ),
    ]
    for field, value in updates:
        conn.execute(
            f"UPDATE reconcile_run_metrics SET {field}=? "
            "WHERE project_id=? AND run_id=? AND snapshot_id=?",
            (value, *key),
        )
        conn.commit()
        current = fingerprint()
        assert current not in fingerprints
        fingerprints.add(current)

    metric = dict(conn.execute(
        "SELECT * FROM reconcile_run_metrics "
        "WHERE project_id=? AND run_id=? AND snapshot_id=?",
        key,
    ).fetchone())
    columns = list(metric)
    before_reinsert = fingerprint()
    conn.execute(
        "DELETE FROM reconcile_run_metrics "
        "WHERE project_id=? AND run_id=? AND snapshot_id=?",
        key,
    )
    conn.execute(
        "INSERT INTO reconcile_run_metrics "
        f"({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        [metric[column] for column in columns],
    )
    conn.commit()
    assert fingerprint() != before_reinsert


def test_terminalization_proof_is_physical_zero_write_on_file_db(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path)
    db_path = tmp_path / "terminalization-proof.sqlite"
    setup = _file_connection(db_path)
    fixture = _terminalization_proof_fixture(setup, suffix="physical")
    setup.close()

    class CountingConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.commit_calls = 0
            self.rollback_calls = 0

        def commit(self):
            self.commit_calls += 1
            return super().commit()

        def rollback(self):
            self.rollback_calls += 1
            return super().rollback()

    reader = sqlite3.connect(db_path, factory=CountingConnection)
    reader.row_factory = sqlite3.Row
    before_bytes = db_path.read_bytes()
    before_changes = reader.total_changes
    before_schema = tuple(reader.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall())
    before_pragmas = tuple(
        reader.execute(f"PRAGMA {name}").fetchone()[0]
        for name in ("schema_version", "page_count", "freelist_count")
    )
    statements = []
    reader.set_trace_callback(statements.append)

    proof = store.reconcile_run_terminalization_proof(
        reader, PID, run_id=fixture["source_run_id"],
        snapshot_id=fixture["source_snapshot_id"],
        manager_certificate=fixture["certificate"],
    )
    store.reconcile_run_terminalization_safe_receipt(proof)
    reader.set_trace_callback(None)

    assert statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert reader.in_transaction is False
    assert reader.total_changes == before_changes
    assert reader.commit_calls == 0
    assert reader.rollback_calls == 0
    assert tuple(reader.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()) == before_schema
    assert tuple(
        reader.execute(f"PRAGMA {name}").fetchone()[0]
        for name in ("schema_version", "page_count", "freelist_count")
    ) == before_pragmas
    reader.close()
    assert db_path.read_bytes() == before_bytes


def test_manager_generation_certificate_append_replay_conflict_and_immutable(conn):
    store.ensure_schema(conn)
    first = store.record_manager_generation_certificate(
        conn,
        PID,
        **_generation("1", manager_pid=4101, prior_manager_pid=4001),
    )
    assert first["writes_performed"] is True
    assert first["replayed"] is False
    assert first["sequence"] == 1
    assert first["predecessor_certificate_id"] == ""
    assert first["prior_manager_pid"] == 4001
    assert first["prior_pid_death_method"] == "esrch"
    assert first["certificate_hash"].startswith("sha256:")

    before_changes = conn.total_changes
    replay = store.record_manager_generation_certificate(
        conn,
        PID,
        **_generation("1", manager_pid=4101, prior_manager_pid=4001),
    )
    assert replay["certificate_id"] == first["certificate_id"]
    assert replay["writes_performed"] is False
    assert replay["replayed"] is True
    assert conn.total_changes == before_changes

    conflict = _generation("1", manager_pid=4101, prior_manager_pid=4001)
    conflict["lock_identity"] = "lock-altered"
    with pytest.raises(
        store.ManagerGenerationCertificateConflictError,
        match="manager_generation_replay_conflict",
    ):
        store.record_manager_generation_certificate(conn, PID, **conflict)
    assert conn.total_changes == before_changes

    with pytest.raises(sqlite3.IntegrityError, match="append_only"):
        conn.execute(
            "UPDATE graph_reconcile_manager_generations SET manager_pid = 9 "
            "WHERE certificate_id = ?",
            (first["certificate_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append_only"):
        conn.execute(
            "DELETE FROM graph_reconcile_manager_generations "
            "WHERE certificate_id = ?",
            (first["certificate_id"],),
        )
    conn.rollback()

    original = dict(
        conn.execute(
            "SELECT * FROM graph_reconcile_manager_generations "
            "WHERE certificate_id = ?",
            (first["certificate_id"],),
        ).fetchone()
    )
    replacement = dict(original)
    replacement["manager_pid"] = 9999
    columns = list(replacement)
    with pytest.raises(sqlite3.IntegrityError, match="identity_conflict"):
        conn.execute(
            "INSERT OR REPLACE INTO graph_reconcile_manager_generations "
            f"({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [replacement[column] for column in columns],
        )
    conn.rollback()
    survived = dict(
        conn.execute(
            "SELECT * FROM graph_reconcile_manager_generations "
            "WHERE certificate_id = ?",
            (first["certificate_id"],),
        ).fetchone()
    )
    assert survived == original


def test_manager_generation_certificates_form_exact_linear_history(conn):
    store.ensure_schema(conn)
    g1 = store.record_manager_generation_certificate(
        conn, PID, **_generation("1", manager_pid=4201)
    )
    g2 = store.record_manager_generation_certificate(
        conn,
        PID,
        **_generation(
            "2",
            manager_pid=4202,
            prior_manager_pid=4201,
            prior_process_start_identity="process-start-1",
        ),
    )
    assert g2["sequence"] == 2
    assert g2["predecessor_certificate_id"] == g1["certificate_id"]
    assert g2["predecessor_generation_id"] == "generation-1"
    assert g2["prior_manager_pid"] == 4201

    before_changes = conn.total_changes
    with pytest.raises(
        store.ManagerGenerationCertificateConflictError,
        match="manager_generation_predecessor_mismatch",
    ):
        store.record_manager_generation_certificate(
            conn,
            PID,
            **_generation(
                "sibling",
                manager_pid=4299,
                prior_manager_pid=4201,
                prior_process_start_identity="process-start-1",
                observed_prior_generation_id="generation-1",
            ),
        )
    assert conn.total_changes == before_changes

    g3 = store.record_manager_generation_certificate(
        conn,
        PID,
        **_generation(
            "3",
            manager_pid=4203,
            prior_manager_pid=4202,
            prior_process_start_identity="process-start-2",
        ),
    )
    assert g3["sequence"] == 3
    assert g3["predecessor_certificate_id"] == g2["certificate_id"]
    rows = conn.execute(
        "SELECT sequence, certificate_id, predecessor_certificate_id "
        "FROM graph_reconcile_manager_generations "
        "WHERE project_id = ? ORDER BY sequence",
        (PID,),
    ).fetchall()
    assert [row["sequence"] for row in rows] == [1, 2, 3]
    assert rows[1]["predecessor_certificate_id"] == rows[0]["certificate_id"]
    assert rows[2]["predecessor_certificate_id"] == rows[1]["certificate_id"]


def test_manager_generation_recovers_after_partial_multi_project_certification(conn):
    store.ensure_schema(conn)
    project_a = PID + "-a"
    project_b = PID + "-b"
    for project in (project_a, project_b):
        store.record_manager_generation_certificate(
            conn, project, **_generation("0", manager_pid=5100)
        )

    g1_a = store.record_manager_generation_certificate(
        conn,
        project_a,
        **_generation(
            "1",
            manager_pid=5101,
            prior_manager_pid=5100,
            prior_process_start_identity="process-start-0",
            observed_prior_generation_id="generation-0",
        ),
    )
    assert store.current_manager_generation_certificate(
        conn, project_b
    )["generation_id"] == "generation-0"

    g2_a = store.record_manager_generation_certificate(
        conn,
        project_a,
        **_generation(
            "2",
            manager_pid=5102,
            prior_manager_pid=5101,
            prior_process_start_identity="process-start-1",
            observed_prior_generation_id="generation-1",
        ),
    )
    g2_b = store.record_manager_generation_certificate(
        conn,
        project_b,
        **_generation(
            "2",
            manager_pid=5102,
            prior_manager_pid=5101,
            prior_process_start_identity="process-start-1",
            observed_prior_generation_id="generation-1",
        ),
    )

    assert g2_a["predecessor_certificate_id"] == g1_a["certificate_id"]
    assert g2_b["predecessor_generation_id"] == "generation-0"
    assert g2_b["observed_prior_generation_id"] == "generation-1"
    assert g2_b["prior_manager_pid"] == 5101
    assert store.current_manager_generation_certificate(
        conn, project_a
    )["generation_id"] == "generation-2"
    assert store.current_manager_generation_certificate(
        conn, project_b
    )["generation_id"] == "generation-2"


def test_manager_generation_replay_is_physical_zero_write_on_file_db(tmp_path):
    db_path = tmp_path / "manager-generation.sqlite"
    first_conn = _file_connection(db_path)
    inputs = _generation("1", manager_pid=4401)
    first = store.record_manager_generation_certificate(first_conn, PID, **inputs)
    first_conn.close()
    before_sha = hashlib.sha256(db_path.read_bytes()).hexdigest()

    replay_conn = _file_connection(db_path)
    statements: list[str] = []
    replay_conn.set_trace_callback(statements.append)
    before_changes = replay_conn.total_changes
    replay = store.record_manager_generation_certificate(replay_conn, PID, **inputs)
    replay_conn.close()
    after_sha = hashlib.sha256(db_path.read_bytes()).hexdigest()

    assert replay["certificate_id"] == first["certificate_id"]
    assert replay["writes_performed"] is False
    assert before_changes == 0
    assert not any(
        statement.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE "))
        for statement in statements
    )
    assert after_sha == before_sha


def test_manager_generation_insert_fault_rolls_back_without_row(conn, monkeypatch):
    store.ensure_schema(conn)

    def fail_after_insert():
        raise RuntimeError("injected-after-insert")

    monkeypatch.setattr(store, "_manager_generation_after_insert_hook", fail_after_insert)
    with pytest.raises(RuntimeError, match="injected-after-insert"):
        store.record_manager_generation_certificate(
            conn, PID, **_generation("1", manager_pid=4501)
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM graph_reconcile_manager_generations "
        "WHERE project_id = ?",
        (PID,),
    ).fetchone()[0] == 0


def test_manager_generation_concurrent_siblings_have_one_linear_winner(tmp_path):
    db_path = tmp_path / "manager-generation-race.sqlite"
    initial = _file_connection(db_path)
    store.record_manager_generation_certificate(
        initial, PID, **_generation("1", manager_pid=4601)
    )
    initial.close()
    barrier = threading.Barrier(2)

    def append(suffix: str, manager_pid: int) -> str:
        connection = sqlite3.connect(db_path, timeout=2)
        connection.row_factory = sqlite3.Row
        store.ensure_schema(connection)
        connection.commit()
        barrier.wait(timeout=2)
        try:
            store.record_manager_generation_certificate(
                connection,
                PID,
                **_generation(
                    suffix,
                        manager_pid=manager_pid,
                        prior_manager_pid=4601,
                        prior_process_start_identity="process-start-1",
                        observed_prior_generation_id="generation-1",
                    ),
            )
        except store.ManagerGenerationCertificateConflictError as exc:
            return exc.reason
        finally:
            connection.close()
        return "won"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda item: append(*item), [("2", 4602), ("3", 4603)]))
    assert sorted(outcomes) == ["manager_generation_predecessor_mismatch", "won"]
    verify = _file_connection(db_path)
    assert verify.execute(
        "SELECT COUNT(*) FROM graph_reconcile_manager_generations "
        "WHERE project_id = ?",
        (PID,),
    ).fetchone()[0] == 2
    verify.close()


def test_manager_generation_hash_binds_exact_case_whitespace_and_predecessor(conn):
    store.ensure_schema(conn)
    g1 = store.record_manager_generation_certificate(
        conn, PID, **_generation("1", manager_pid=4701)
    )
    g2 = store.record_manager_generation_certificate(
        conn,
        PID,
        **_generation(
            "2",
            manager_pid=4702,
            prior_manager_pid=4701,
            prior_process_start_identity="process-start-1",
        ),
    )
    row = dict(conn.execute(
        "SELECT * FROM graph_reconcile_manager_generations WHERE certificate_id = ?",
        (g2["certificate_id"],),
    ).fetchone())
    original_hash = store._manager_generation_certificate_hash(row)
    for key, replacement in (
        ("manager_started_at", row["manager_started_at"] + " "),
        ("manager_start_identity", row["manager_start_identity"].upper()),
        ("predecessor_certificate_hash", g1["certificate_hash"].upper()),
        ("predecessor_sequence", int(g1["sequence"]) + 1),
    ):
        altered = dict(row)
        altered[key] = replacement
        assert store._manager_generation_certificate_hash(altered) != original_hash


def test_manager_generation_identity_variants_conflict_without_write(conn):
    store.ensure_schema(conn)
    first = store.record_manager_generation_certificate(
        conn, PID, **_generation("1", manager_pid=4801)
    )
    for key in ("manager_start_identity", "lock_identity"):
        candidate = _generation(
            "2",
            manager_pid=4802,
            prior_manager_pid=4801,
            prior_process_start_identity="process-start-1",
        )
        candidate[key] = first[key]
        before_changes = conn.total_changes
        with pytest.raises(
            store.ManagerGenerationCertificateConflictError,
            match="manager_generation_identity_conflict",
        ):
            store.record_manager_generation_certificate(conn, PID, **candidate)
        assert conn.total_changes == before_changes


def test_manager_generation_reader_bounds_history_and_rejects_cycle(conn, monkeypatch):
    store.ensure_schema(conn)
    g1 = store.record_manager_generation_certificate(
        conn, PID, **_generation("1", manager_pid=4901)
    )
    g2 = store.record_manager_generation_certificate(
        conn,
        PID,
        **_generation(
            "2",
            manager_pid=4902,
            prior_manager_pid=4901,
            prior_process_start_identity="process-start-1",
        ),
    )
    store.record_manager_generation_certificate(
        conn,
        PID,
        **_generation(
            "3",
            manager_pid=4903,
            prior_manager_pid=4902,
            prior_process_start_identity="process-start-2",
        ),
    )
    monkeypatch.setattr(store, "MANAGER_GENERATION_MAX_CHAIN_DEPTH", 2)
    with pytest.raises(
        store.ManagerGenerationCertificateConflictError,
        match="manager_generation_history_too_deep",
    ):
        store.current_manager_generation_certificate(conn, PID)
    monkeypatch.setattr(store, "MANAGER_GENERATION_MAX_CHAIN_DEPTH", 10_000)

    cycle_project = PID + "-cycle"
    cycle = {
        "sequence": int(g2["sequence"]) + 10,
        "certificate_id": "gmcert-self-cycle",
        "project_id": cycle_project,
        "generation_id": "generation-self-cycle",
        "manager_pid": 4999,
        "manager_started_at": "2026-08-10T00:00:00Z",
        "process_start_identity": "process-self-cycle",
        "manager_start_identity": "manager-self-cycle",
        "lock_identity": "lock-self-cycle",
        "predecessor_certificate_id": "gmcert-self-cycle",
        "predecessor_generation_id": "generation-self-cycle",
        "predecessor_sequence": int(g2["sequence"]) + 10,
        "predecessor_certificate_hash": "sha256:" + "0" * 64,
        "prior_manager_pid": 4999,
        "observed_prior_generation_id": "generation-self-cycle",
        "prior_process_start_identity": "process-self-cycle",
        "prior_pid_death_method": "esrch",
        "prior_pid_death_verified_at": "2026-08-10T00:00:01Z",
        "certified_at": "2026-08-10T00:00:02Z",
    }
    cycle["certificate_hash"] = store._manager_generation_certificate_hash(cycle)
    columns = list(cycle)
    conn.execute(
        "INSERT INTO graph_reconcile_manager_generations "
        f"({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        [cycle[column] for column in columns],
    )
    conn.commit()
    with pytest.raises(
        store.ManagerGenerationCertificateConflictError,
        match="manager_generation_predecessor_binding_invalid",
    ):
        store.current_manager_generation_certificate(conn, cycle_project)

    two_cycle_project = PID + "-two-cycle"
    a = dict(cycle)
    a.update(
        {
            "sequence": int(cycle["sequence"]) + 1,
            "certificate_id": "gmcert-two-cycle-a",
            "project_id": two_cycle_project,
            "generation_id": "generation-two-cycle-a",
            "manager_pid": 5001,
            "manager_start_identity": "manager-two-cycle-a",
            "lock_identity": "lock-two-cycle-a",
            "predecessor_certificate_id": "gmcert-two-cycle-b",
            "predecessor_generation_id": "generation-two-cycle-b",
            "predecessor_sequence": int(cycle["sequence"]) + 2,
            "prior_manager_pid": 5002,
            "observed_prior_generation_id": "generation-two-cycle-b",
        }
    )
    a["certificate_hash"] = store._manager_generation_certificate_hash(a)
    b = dict(a)
    b.update(
        {
            "sequence": int(cycle["sequence"]) + 2,
            "certificate_id": "gmcert-two-cycle-b",
            "generation_id": "generation-two-cycle-b",
            "manager_pid": 5002,
            "manager_start_identity": "manager-two-cycle-b",
            "lock_identity": "lock-two-cycle-b",
            "predecessor_certificate_id": "gmcert-two-cycle-a",
            "predecessor_generation_id": "generation-two-cycle-a",
            "predecessor_sequence": int(cycle["sequence"]) + 1,
            "predecessor_certificate_hash": a["certificate_hash"],
            "prior_manager_pid": 5001,
            "observed_prior_generation_id": "generation-two-cycle-a",
        }
    )
    b["certificate_hash"] = store._manager_generation_certificate_hash(b)
    for item in (a, b):
        columns = list(item)
        conn.execute(
            "INSERT INTO graph_reconcile_manager_generations "
            f"({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [item[column] for column in columns],
        )
    conn.commit()
    with pytest.raises(
        store.ManagerGenerationCertificateConflictError,
        match="manager_generation_predecessor_binding_invalid",
    ):
        store.current_manager_generation_certificate(conn, two_cycle_project)


def test_manager_generation_prewrite_depth_gate_is_exact_and_physical_zero_write(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "manager-generation-depth.sqlite"
    connection = _file_connection(db_path)
    monkeypatch.setattr(store, "MANAGER_GENERATION_MAX_CHAIN_DEPTH", 2)
    g1 = store.record_manager_generation_certificate(
        connection, PID, **_generation("1", manager_pid=5301)
    )
    g2_inputs = _generation(
        "2",
        manager_pid=5302,
        prior_manager_pid=5301,
        prior_process_start_identity="process-start-1",
        observed_prior_generation_id="generation-1",
    )
    g2 = store.record_manager_generation_certificate(
        connection, PID, **g2_inputs
    )
    assert g1["sequence"] < g2["sequence"]
    connection.close()
    before_sha = hashlib.sha256(db_path.read_bytes()).hexdigest()

    verify = _file_connection(db_path)
    statements: list[str] = []
    verify.set_trace_callback(statements.append)
    before_changes = verify.total_changes
    with pytest.raises(
        store.ManagerGenerationCertificateConflictError,
        match="manager_generation_history_too_deep",
    ):
        store.record_manager_generation_certificate(
            verify,
            PID,
            **_generation(
                "3",
                manager_pid=5303,
                prior_manager_pid=5302,
                prior_process_start_identity="process-start-2",
                observed_prior_generation_id="generation-2",
            ),
        )
    current = store.current_manager_generation_certificate(verify, PID)
    replay = store.record_manager_generation_certificate(
        verify, PID, **g2_inputs
    )
    assert current["certificate_id"] == g2["certificate_id"]
    assert replay["certificate_id"] == g2["certificate_id"]
    assert replay["writes_performed"] is False
    assert verify.total_changes == before_changes
    assert verify.execute(
        "SELECT COUNT(*) FROM graph_reconcile_manager_generations "
        "WHERE project_id = ?",
        (PID,),
    ).fetchone()[0] == 2
    verify.close()
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before_sha
    assert not any(
        statement.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE "))
        for statement in statements
    )


def test_manager_generation_schema_rejects_false_prior_death_proof(conn):
    store.ensure_schema(conn)
    first = store.record_manager_generation_certificate(
        conn, PID, **_generation("1", manager_pid=5001)
    )
    malformed = dict(conn.execute(
        "SELECT * FROM graph_reconcile_manager_generations WHERE certificate_id = ?",
        (first["certificate_id"],),
    ).fetchone())
    malformed.update(
        {
            "sequence": int(first["sequence"]) + 1,
            "certificate_id": "gmcert-false-death",
            "project_id": PID + "-false-death",
            "generation_id": "generation-false-death",
            "manager_start_identity": "manager-false-death",
            "lock_identity": "lock-false-death",
            "predecessor_certificate_id": "",
            "predecessor_generation_id": "",
            "predecessor_sequence": 0,
            "predecessor_certificate_hash": "",
            "prior_manager_pid": 5000,
            "observed_prior_generation_id": "",
            "prior_process_start_identity": "",
            "prior_pid_death_method": "",
            "prior_pid_death_verified_at": "",
        }
    )
    malformed["certificate_hash"] = store._manager_generation_certificate_hash(
        malformed
    )
    columns = list(malformed)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute(
            "INSERT INTO graph_reconcile_manager_generations "
            f"({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [malformed[column] for column in columns],
        )


def test_create_index_and_activate_snapshot(conn, tmp_path):
    _ensure_schema(conn)
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-abc1234-test",
        commit_sha="abc1234deadbeef",
        snapshot_kind="full",
        graph_json={"deps_graph": {"nodes": []}},
        file_inventory=[{"path": "agent/governance/foo.py"}],
        drift_ledger=[],
        created_by="test",
    )

    assert snapshot["snapshot_id"] == "full-abc1234-test"
    assert snapshot["graph_sha256"]
    assert (tmp_path / PID / "graph-snapshots" / snapshot["snapshot_id"] / "graph.json").exists()

    counts = store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=[
            {
                "id": "L7.1",
                "layer": "L7",
                "title": "Graph Store",
                "primary": ["agent/governance/graph_snapshot_store.py"],
                "secondary": ["docs/dev/proposal-graph-governance-unified-v3.md"],
                "test": ["agent/tests/test_graph_snapshot_store.py"],
                "metadata": {"kind": "state_store", "subsystem": "governance"},
            }
        ],
        edges=[
            {
                "source": "L7.1",
                "target": "L7.2",
                "edge_type": "depends_on",
                "direction": "dependency",
                "evidence": {"reason": "unit-test"},
            }
        ],
    )
    assert counts == {"nodes": 1, "edges": 1}

    activation = store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    assert activation["previous_snapshot_id"] == ""
    assert activation["graph_ref_event_id"]

    active = store.get_active_graph_snapshot(conn, PID)
    assert active["snapshot_id"] == snapshot["snapshot_id"]
    assert active["commit_sha"] == "abc1234deadbeef"

    ref_events = store.list_graph_ref_events(conn, PID, ref_name="active")
    assert len(ref_events) == 1
    assert ref_events[0]["operation_type"] == "activate"
    assert ref_events[0]["old_snapshot_id"] == ""
    assert ref_events[0]["new_snapshot_id"] == snapshot["snapshot_id"]
    assert ref_events[0]["new_commit"] == "abc1234deadbeef"
    assert ref_events[0]["evidence"]["projection_status"] in {"rebuilt", "already_present", "skipped"}

    node = conn.execute(
        "SELECT * FROM graph_nodes_index WHERE project_id=? AND snapshot_id=? AND node_id=?",
        (PID, snapshot["snapshot_id"], "L7.1"),
    ).fetchone()
    assert json.loads(node["primary_files_json"]) == ["agent/governance/graph_snapshot_store.py"]
    assert json.loads(node["metadata_json"])["kind"] == "state_store"


def test_index_graph_snapshot_prefers_explicit_config_without_mutating_nodes(conn):
    _ensure_schema(conn)
    nodes = [
        {
            "id": "L7.config",
            "config": ["config/build.yaml", "config/settings.json"],
            "metadata": {"config_files": [], "kind": "state_store"},
        },
        {
            "id": "L7.empty-config",
            "config": [],
            "metadata": {"config_files": ["config/stale.json"]},
        },
        {
            "id": "L7.legacy-config",
            "metadata": {"config_files": ["config/legacy.json"]},
        },
    ]

    store.index_graph_snapshot(conn, PID, "full-config-index", nodes=nodes)

    rows = conn.execute(
        "SELECT node_id, metadata_json FROM graph_nodes_index "
        "WHERE project_id=? AND snapshot_id=? ORDER BY node_id",
        (PID, "full-config-index"),
    ).fetchall()
    indexed = {row["node_id"]: json.loads(row["metadata_json"]) for row in rows}
    assert indexed["L7.config"]["config_files"] == [
        "config/build.yaml", "config/settings.json",
    ]
    assert indexed["L7.empty-config"]["config_files"] == []
    assert indexed["L7.legacy-config"]["config_files"] == ["config/legacy.json"]
    assert nodes[0]["metadata"]["config_files"] == []
    assert nodes[1]["metadata"]["config_files"] == ["config/stale.json"]


def test_activate_snapshot_compare_and_swap_rejects_stale_writer(conn):
    _ensure_schema(conn)
    first = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-a111111-one",
        commit_sha="a111111",
        snapshot_kind="full",
    )
    second = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-b222222-two",
        commit_sha="b222222",
        snapshot_kind="scope",
    )

    store.activate_graph_snapshot(conn, PID, first["snapshot_id"])

    with pytest.raises(store.GraphSnapshotConflictError):
        store.activate_graph_snapshot(
            conn,
            PID,
            second["snapshot_id"],
            expected_old_snapshot_id="not-the-active-snapshot",
        )

    store.activate_graph_snapshot(
        conn,
        PID,
        second["snapshot_id"],
        expected_old_snapshot_id=first["snapshot_id"],
    )
    active = store.get_active_graph_snapshot(conn, PID)
    assert active["snapshot_id"] == second["snapshot_id"]

    first_row = conn.execute(
        "SELECT status FROM graph_snapshots WHERE project_id=? AND snapshot_id=?",
        (PID, first["snapshot_id"]),
    ).fetchone()
    assert first_row["status"] == store.SNAPSHOT_STATUS_SUPERSEDED

    ref_events = store.list_graph_ref_events(conn, PID, ref_name="active")
    by_new = {event["new_snapshot_id"]: event for event in ref_events}
    assert set(by_new) == {first["snapshot_id"], second["snapshot_id"]}
    assert by_new[second["snapshot_id"]]["old_snapshot_id"] == first["snapshot_id"]
    assert by_new[second["snapshot_id"]]["old_commit"] == "a111111"
    assert by_new[second["snapshot_id"]]["new_commit"] == "b222222"


def test_graph_ref_events_record_rollback_epoch_and_branch_ref_isolation(conn):
    _ensure_schema(conn)
    base = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-base-rollback",
        commit_sha="base",
        snapshot_kind="full",
    )
    branch = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-branch-candidate",
        commit_sha="branch-head",
        snapshot_kind="scope",
    )
    rollback = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-base-rollback",
        commit_sha="base",
        snapshot_kind="scope",
    )

    store.activate_graph_snapshot(conn, PID, base["snapshot_id"], auto_rebuild_projection=False)
    store.activate_graph_snapshot(
        conn,
        PID,
        branch["snapshot_id"],
        ref_name="refs/heads/codex/feature",
        operation_type="merge",
        branch_ref="refs/heads/codex/feature",
        batch_id="batch-rollback",
        merge_queue_id="mergeq-rollback",
        merge_epoch="merge-001",
        auto_rebuild_projection=False,
    )
    active = store.get_active_graph_snapshot(conn, PID, ref_name="active")
    assert active["snapshot_id"] == base["snapshot_id"]
    branch_snapshot = store.get_graph_snapshot(conn, PID, branch["snapshot_id"])
    assert branch_snapshot["status"] == store.SNAPSHOT_STATUS_CANDIDATE

    result = store.activate_graph_snapshot(
        conn,
        PID,
        rollback["snapshot_id"],
        operation_type="rollback",
        batch_id="batch-rollback",
        rollback_epoch="rollback-001",
        source_event_id="merge-001",
        evidence={"reason": "wrong merge order"},
        auto_rebuild_projection=False,
    )

    assert result["previous_snapshot_id"] == base["snapshot_id"]
    active_events = store.list_graph_ref_events(conn, PID, ref_name="active")
    branch_events = store.list_graph_ref_events(conn, PID, ref_name="refs/heads/codex/feature")
    active_by_op = {event["operation_type"]: event for event in active_events}
    assert set(active_by_op) == {"activate", "rollback"}
    rollback_event = active_by_op["rollback"]
    assert rollback_event["rollback_epoch"] == "rollback-001"
    assert rollback_event["source_event_id"] == "merge-001"
    assert rollback_event["evidence"]["reason"] == "wrong merge order"
    assert branch_events[0]["operation_type"] == "merge"
    assert branch_events[0]["branch_ref"] == "refs/heads/codex/feature"
    assert branch_events[0]["merge_epoch"] == "merge-001"


def test_branch_candidate_snapshot_cannot_be_promoted_to_active_target(conn):
    _ensure_schema(conn)
    active = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-target-active",
        commit_sha="target",
        snapshot_kind="full",
    )
    branch = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-branch-one-hop-candidate",
        commit_sha="branch-head",
        snapshot_kind="scope",
        ref_name="refs/heads/codex/feature",
        branch_ref="refs/heads/codex/feature",
    )
    store.activate_graph_snapshot(conn, PID, active["snapshot_id"], auto_rebuild_projection=False)

    with pytest.raises(ValueError, match="branch graph candidate cannot be activated"):
        store.activate_graph_snapshot(
            conn,
            PID,
            branch["snapshot_id"],
            auto_rebuild_projection=False,
        )

    current = store.get_active_graph_snapshot(conn, PID)
    assert current["snapshot_id"] == active["snapshot_id"]
    stored_branch = store.get_graph_snapshot(conn, PID, branch["snapshot_id"])
    assert stored_branch["status"] == store.SNAPSHOT_STATUS_CANDIDATE
    active_events = store.list_graph_ref_events(conn, PID, ref_name="active")
    assert active_events[-1]["new_snapshot_id"] == active["snapshot_id"]


def test_activate_snapshot_rejects_invalid_ref_operation_without_moving_active(conn):
    _ensure_schema(conn)
    active = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-valid-active",
        commit_sha="valid",
        snapshot_kind="full",
    )
    candidate = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-invalid-op",
        commit_sha="candidate",
        snapshot_kind="scope",
    )
    store.activate_graph_snapshot(conn, PID, active["snapshot_id"], auto_rebuild_projection=False)

    with pytest.raises(ValueError, match="invalid graph ref operation_type"):
        store.activate_graph_snapshot(
            conn,
            PID,
            candidate["snapshot_id"],
            operation_type="unsafe_direct_write",
            auto_rebuild_projection=False,
        )

    current = store.get_active_graph_snapshot(conn, PID)
    assert current["snapshot_id"] == active["snapshot_id"]
    assert store.list_graph_ref_events(conn, PID, operation_type="unsafe_direct_write") == []


def test_drift_ledger_allows_multiple_target_symbols(conn):
    _ensure_schema(conn)
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-c333333-drift",
        commit_sha="c333333",
        snapshot_kind="full",
    )

    store.record_drift(
        conn,
        PID,
        snapshot_id=snapshot["snapshot_id"],
        commit_sha="c333333",
        path="agent/service.py",
        drift_type="missing_test",
        target_symbol="agent.service.create",
        evidence={"reason": "no direct test"},
    )
    store.record_drift(
        conn,
        PID,
        snapshot_id=snapshot["snapshot_id"],
        commit_sha="c333333",
        path="agent/service.py",
        drift_type="missing_test",
        target_symbol="agent.service.delete",
        evidence={"reason": "no direct test"},
    )

    rows = conn.execute(
        """
        SELECT target_symbol FROM graph_drift_ledger
        WHERE project_id=? AND snapshot_id=? AND path=? AND drift_type=?
        ORDER BY target_symbol
        """,
        (PID, snapshot["snapshot_id"], "agent/service.py", "missing_test"),
    ).fetchall()
    assert [row["target_symbol"] for row in rows] == [
        "agent.service.create",
        "agent.service.delete",
    ]
    listed = store.list_graph_drift(
        conn,
        PID,
        snapshot_id=snapshot["snapshot_id"],
        drift_type="missing_test",
    )
    assert len(listed) == 2
    assert {row["target_symbol"] for row in listed} == {
        "agent.service.create",
        "agent.service.delete",
    }
    assert all(row["evidence"]["reason"] == "no direct test" for row in listed)


def test_graph_payload_edges_include_hierarchy_and_dependency_sections():
    graph = {
        "hierarchy_graph": {
            "nodes": [{"id": "L1.1"}, {"id": "L2.1"}],
            "links": [{"source": "L1.1", "target": "L2.1", "type": "contains"}],
        },
        "deps_graph": {
            "nodes": [{"id": "L1.1"}, {"id": "L2.1"}],
            "links": [{"source": "L2.1", "target": "L1.1", "type": "depends_on"}],
        },
    }

    edges = store.graph_payload_edges(graph)

    assert store.graph_payload_stats(graph) == {"nodes": 2, "edges": 2}
    assert {
        (edge["src"], edge["dst"], edge["edge_type"], edge["direction"])
        for edge in edges
    } == {
        ("L1.1", "L2.1", "contains", "hierarchy"),
        ("L2.1", "L1.1", "depends_on", "dependency"),
    }


def test_pending_scope_reconcile_queue_is_idempotent(conn):
    _ensure_schema(conn)
    first = store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="d444444",
        parent_commit_sha="c333333",
        evidence={"source": "dispatch-hook"},
    )
    second = store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="d444444",
        parent_commit_sha="ignored-parent",
        evidence={"source": "retry"},
    )

    assert first["commit_sha"] == second["commit_sha"]
    assert second["parent_commit_sha"] == "c333333"
    assert second["status"] == store.PENDING_STATUS_QUEUED

    count = conn.execute(
        "SELECT COUNT(*) AS count FROM pending_scope_reconcile WHERE project_id=? AND commit_sha=?",
        (PID, "d444444"),
    ).fetchone()["count"]
    assert count == 1


def test_reconcile_run_metrics_record_and_summarize(conn):
    _ensure_schema(conn)
    store.record_reconcile_run_metric(
        conn,
        PID,
        run_id="scope-fast",
        snapshot_id="scope-fast",
        commit_sha="fast",
        snapshot_kind="scope",
        strategy="incremental_graph_delta",
        graph_delta_mode="test_fanin_hash_only",
        status="ok",
        changed_file_count=2,
        event_count=12,
        elapsed_ms=4700,
    )
    store.record_reconcile_run_metric(
        conn,
        PID,
        run_id="scope-full",
        snapshot_id="scope-full",
        commit_sha="full",
        snapshot_kind="scope",
        strategy="full_rebuild_fallback",
        graph_delta_mode="full_rebuild",
        status="ok",
        changed_file_count=3,
        event_count=13,
        elapsed_ms=36000,
    )

    rows = store.list_reconcile_run_metrics(conn, PID)
    assert {row["run_id"] for row in rows} == {"scope-fast", "scope-full"}
    summary = store.summarize_reconcile_run_metrics(conn, PID)
    assert summary["by_strategy"]["incremental_graph_delta"]["avg_elapsed_ms"] == 4700
    assert summary["by_strategy"]["full_rebuild_fallback"]["avg_elapsed_ms"] == 36000
    assert summary["speedup"]["speedup_x"] == pytest.approx(7.66, rel=0.01)
    assert summary["speedup"]["elapsed_reduction_pct"] == pytest.approx(86.9, rel=0.01)


def test_reconcile_run_metrics_keep_every_effective_nonterminal_beyond_limit(conn):
    _ensure_schema(conn)
    for index in range(110):
        store.record_reconcile_run_metric(
            conn,
            PID,
            run_id=f"terminal-{index:03d}",
            snapshot_id=f"full-terminal-{index:03d}",
            snapshot_kind="full",
            strategy="current_full_reconcile",
            status="candidate_ready",
            created_at=f"2026-08-10T02:00:{index:03d}Z",
        )
    for index in range(106):
        store.record_reconcile_run_metric(
            conn,
            PID,
            run_id=f"nonterminal-{index:03d}",
            snapshot_id=f"full-nonterminal-{index:03d}",
            snapshot_kind="full",
            strategy="current_full_reconcile",
            status="running" if index % 2 == 0 else "finalizing",
            created_at=f"2026-08-10T01:00:{index:03d}Z",
        )
    store.record_reconcile_run_metric(
        conn,
        PID,
        run_id="malformed-status",
        snapshot_id="full-malformed-status",
        snapshot_kind="full",
        strategy="current_full_reconcile",
        status="../../private/credential.txt",
        created_at="2026-08-10T00:00:000Z",
    )

    rows = store.list_reconcile_run_metrics(
        conn,
        PID,
        limit=5,
        strategy="current_full_reconcile",
    )

    assert len(rows) == 112
    keys = [
        (row["project_id"], row["run_id"], row["snapshot_id"])
        for row in rows
    ]
    assert len(keys) == len(set(keys))
    assert [row["run_id"] for row in rows[:5]] == [
        "terminal-109",
        "terminal-108",
        "terminal-107",
        "terminal-106",
        "terminal-105",
    ]
    assert "terminal-104" not in {row["run_id"] for row in rows}
    assert {
        row["run_id"]
        for row in rows
        if row["effective_status"] in {"running", "finalizing"}
    } == {f"nonterminal-{index:03d}" for index in range(106)}
    malformed = next(row for row in rows if row["run_id"] == "malformed-status")
    assert malformed["effective_status"] == "unknown"
    assert malformed["is_terminal"] is False
    assert malformed["status_reason_code"] == "unrecognized_reconcile_status"
    assert rows == sorted(
        rows,
        key=lambda row: (
            row["created_at"],
            row["run_id"],
            row["snapshot_id"],
        ),
        reverse=True,
    )


@pytest.mark.parametrize("strategy", ["current_full_reconcile", ""])
def test_reconcile_run_metrics_terminal_history_vm_steps_stay_bounded(
    conn,
    strategy,
):
    _ensure_schema(conn)
    conn.executemany(
        """
        INSERT INTO reconcile_run_metrics (
          project_id, run_id, snapshot_id, snapshot_kind, strategy,
          graph_delta_mode, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                PID,
                f"terminal-history-{index:04d}",
                f"full-terminal-history-{index:04d}",
                "full",
                "current_full_reconcile",
                "full_rebuild",
                "candidate_ready",
                f"2026-08-09T{index // 3600:02d}:{(index // 60) % 60:02d}:{index % 60:02d}Z",
            )
            for index in range(5000)
        ],
    )
    conn.execute(
        """
        INSERT INTO reconcile_run_metrics (
          project_id, run_id, snapshot_id, snapshot_kind, strategy,
          graph_delta_mode, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            PID,
            "effective-nonterminal-newest",
            "full-effective-nonterminal-newest",
            "full",
            "current_full_reconcile",
            "full_rebuild",
            "finalizing",
            "2026-08-10T00:00:00Z",
        ),
    )
    conn.commit()

    approximate_vm_steps = 0

    def count_vm_steps() -> int:
        nonlocal approximate_vm_steps
        approximate_vm_steps += 100
        return 0

    conn.set_progress_handler(count_vm_steps, 100)
    try:
        rows = store.list_reconcile_run_metrics(
            conn,
            PID,
            limit=1,
            strategy=strategy,
        )
    finally:
        conn.set_progress_handler(None, 0)

    assert [
        (row["project_id"], row["run_id"], row["snapshot_id"])
        for row in rows
    ] == [
        (
            PID,
            "effective-nonterminal-newest",
            "full-effective-nonterminal-newest",
        )
    ]
    assert approximate_vm_steps < 5000


def test_reconcile_run_metrics_read_initializes_missing_schema():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        assert store.list_reconcile_run_metrics(connection, PID, limit=1) == []
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "reconcile_run_metrics",
            "graph_reconcile_run_terminalizations",
            "graph_c_family_compilation_actions",
        }.issubset(tables)
    finally:
        connection.close()


def test_reconcile_run_metrics_50k_window_bounds_cardinality_vm_memory_and_cursor(
    conn,
):
    _ensure_schema(conn)
    conn.executemany(
        """
        INSERT INTO reconcile_run_metrics (
          project_id, run_id, snapshot_id, snapshot_kind, strategy,
          graph_delta_mode, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                PID,
                f"current-full-{index:05d}",
                f"full-{index:05d}",
                "full",
                "current_full_reconcile",
                "full_rebuild",
                "running" if index % 2 == 0 else "finalizing",
                f"{index:020d}",
            )
            for index in range(50_001)
        ],
    )
    conn.commit()

    approximate_vm_steps = 0

    def count_vm_steps() -> int:
        nonlocal approximate_vm_steps
        approximate_vm_steps += 100
        return 0

    conn.set_progress_handler(count_vm_steps, 100)
    tracemalloc.start()
    try:
        first = store.list_reconcile_run_metrics_window(
            conn,
            PID,
            limit=1,
            strategy="current_full_reconcile",
            nonterminal_limit=128,
        )
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        conn.set_progress_handler(None, 0)

    assert first["returned_count"] == len(first["rows"]) == 128
    assert first["latest_sample_count"] == 1
    assert first["latest_sample_truncated"] is True
    assert first["nonterminal_page_count"] == 128
    assert first["has_more"] is True
    assert first["truncated"] is True
    assert first["remaining_count_claimed"] is False
    assert first["effective_nonterminal_completeness"] == "partial"
    assert first["next_cursor"].startswith("rrm1.")
    assert "current-full-" not in first["next_cursor"]
    assert approximate_vm_steps < 25_000
    assert peak_bytes < 8_000_000
    assert first["rows"] == sorted(
        first["rows"],
        key=lambda row: (
            row["created_at"],
            row["run_id"],
            row["snapshot_id"],
        ),
        reverse=True,
    )
    first_keys = {
        (row["project_id"], row["run_id"], row["snapshot_id"])
        for row in first["rows"]
    }
    assert len(first_keys) == len(first["rows"])

    second = store.list_reconcile_run_metrics_window(
        conn,
        PID,
        limit=1,
        strategy="current_full_reconcile",
        nonterminal_limit=128,
        cursor=first["next_cursor"],
    )
    second_keys = {
        (row["project_id"], row["run_id"], row["snapshot_id"])
        for row in second["rows"]
    }
    latest_key = (
        PID,
        "current-full-50000",
        "full-50000",
    )
    assert second["cursor_applied"] is True
    assert len(second["rows"]) == 129
    assert first_keys & second_keys == {latest_key}
    assert len(second_keys) == len(second["rows"])

    with pytest.raises(store.ReconcileMetricWindowOverflow) as overflow:
        store.list_reconcile_run_metrics(
            conn,
            PID,
            limit=1,
            strategy="current_full_reconcile",
        )
    assert overflow.value.window["has_more"] is True
    assert "rows" not in overflow.value.window


def test_reconcile_run_metric_cursor_rejects_cross_scope_and_rowid_reuse(conn):
    _ensure_schema(conn)
    project_id = "cursor-scope-project"
    for index in range(3):
        store.record_reconcile_run_metric(
            conn,
            project_id,
            run_id=f"current-full-cursor-{index}",
            snapshot_id=f"full-cursor-{index}",
            strategy="current_full_reconcile",
            status="running",
            created_at=f"{index:020d}",
        )
    conn.commit()
    first = store.list_reconcile_run_metrics_window(
        conn,
        project_id,
        limit=1,
        strategy="current_full_reconcile",
        nonterminal_limit=1,
    )
    cursor = first["next_cursor"]

    with pytest.raises(store.InvalidReconcileMetricCursor) as cross_scope:
        store.list_reconcile_run_metrics_window(
            conn,
            project_id,
            limit=1,
            strategy="different_strategy",
            nonterminal_limit=1,
            cursor=cursor,
        )
    assert cross_scope.value.reason_code == "cursor_scope_mismatch"

    cursor_parts = cursor.split(".")
    out_of_range_cursor = ".".join(
        ["rrm1", "9999999999999999999", *cursor_parts[2:]]
    )
    with pytest.raises(store.InvalidReconcileMetricCursor) as out_of_range:
        store.list_reconcile_run_metrics_window(
            conn,
            project_id,
            limit=1,
            strategy="current_full_reconcile",
            nonterminal_limit=1,
            cursor=out_of_range_cursor,
        )
    assert out_of_range.value.reason_code == "cursor_rowid_out_of_range"

    cursor_rowid = int(cursor.split(".", 2)[1])
    conn.execute(
        "DELETE FROM reconcile_run_metrics WHERE rowid=?",
        (cursor_rowid,),
    )
    conn.execute(
        """
        INSERT INTO reconcile_run_metrics (
          project_id, run_id, snapshot_id, strategy, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            "current-full-cursor-reused",
            "full-cursor-reused",
            "current_full_reconcile",
            "running",
            "99999999999999999999",
        ),
    )
    reused_rowid = conn.execute(
        "SELECT rowid FROM reconcile_run_metrics WHERE project_id=? AND run_id=?",
        (project_id, "current-full-cursor-reused"),
    ).fetchone()[0]
    assert reused_rowid == cursor_rowid

    with pytest.raises(store.InvalidReconcileMetricCursor) as reused:
        store.list_reconcile_run_metrics_window(
            conn,
            project_id,
            limit=1,
            strategy="current_full_reconcile",
            nonterminal_limit=1,
            cursor=cursor,
        )
    assert reused.value.reason_code == "cursor_identity_mismatch"


def test_reconcile_metric_window_reports_sample_truncation_without_queue_overflow(
    conn,
):
    _ensure_schema(conn)
    for index in range(2):
        store.record_reconcile_run_metric(
            conn,
            PID,
            run_id=f"terminal-sample-{index}",
            snapshot_id=f"full-terminal-sample-{index}",
            strategy="current_full_reconcile",
            status="candidate_ready",
            created_at=f"{index:020d}",
        )
    conn.commit()

    window = store.list_reconcile_run_metrics_window(
        conn,
        PID,
        limit=1,
        strategy="current_full_reconcile",
        nonterminal_limit=1,
    )

    assert window["latest_sample_truncated"] is True
    assert window["has_more"] is False
    assert window["truncated"] is True
    assert window["next_cursor"] == ""
    assert window["effective_nonterminal_completeness"] == "complete"
    assert store.list_reconcile_run_metrics(
        conn,
        PID,
        limit=1,
        strategy="current_full_reconcile",
    ) == window["rows"]


@pytest.mark.parametrize(
    "status",
    ["candidate_ready", "complete", "failed", "terminalized_stale"],
)
def test_reconcile_run_metric_status_projection_has_exact_terminal_set(status):
    projection = store.project_reconcile_run_metric_status({"status": status})

    assert projection == {
        "effective_status": status,
        "is_terminal": True,
        "status_reason_code": "",
    }


def test_reconcile_metric_window_uses_only_valid_terminalization_overlays(conn):
    valid = _terminalization_proof_fixture(conn, suffix="window-valid-z")
    _insert_terminalization_overlay(conn, valid, suffix="window-valid-z")
    invalid = _terminalization_proof_fixture(conn, suffix="window-invalid-y")
    _insert_terminalization_overlay(
        conn,
        invalid,
        suffix="window-invalid-y",
        ledger_overrides={"source_fingerprint": "sha256:" + "0" * 64},
    )

    window = store.list_reconcile_run_metrics_window(
        conn,
        PID,
        limit=10,
        strategy="current_full_reconcile",
        nonterminal_limit=10,
    )
    rows = {
        (row["run_id"], row["snapshot_id"]): row for row in window["rows"]
    }

    valid_row = rows[(valid["source_run_id"], valid["source_snapshot_id"])]
    assert valid_row["status"] == "running"
    assert valid_row["effective_status"] == "terminalized_stale"
    assert valid_row["is_terminal"] is True
    assert valid_row["status_reason_code"] == "terminalization_overlay_valid"
    invalid_row = rows[(
        invalid["source_run_id"], invalid["source_snapshot_id"]
    )]
    assert invalid_row["status"] == "running"
    assert invalid_row["effective_status"] == "running"
    assert invalid_row["is_terminal"] is False
    assert invalid_row["status_reason_code"] == "terminalization_overlay_invalid"
    assert window["nonterminal_page_count"] == 1


def test_reconcile_metric_window_scans_valid_overlays_before_pending_work(conn):
    for suffix in ("window-scan-z", "window-scan-y"):
        fixture = _terminalization_proof_fixture(conn, suffix=suffix)
        _insert_terminalization_overlay(conn, fixture, suffix=suffix)
    store.record_reconcile_run_metric(
        conn,
        PID,
        run_id="stale-source-window-scan-x",
        snapshot_id="full-stale-source-window-scan-x",
        commit_sha="a" * 40,
        snapshot_kind="full",
        strategy="current_full_reconcile",
        graph_delta_mode="full_rebuild",
        status="running",
        evidence={"phase": "build"},
        created_at="2026-08-10T00:00:00Z",
    )
    conn.commit()

    window = store.list_reconcile_run_metrics_window(
        conn,
        PID,
        limit=1,
        strategy="current_full_reconcile",
        nonterminal_limit=1,
    )

    assert window["nonterminal_page_count"] == 1
    assert window["has_more"] is False
    assert window["next_cursor"] == ""
    pending = [
        row for row in window["rows"]
        if row["run_id"] == "stale-source-window-scan-x"
    ]
    assert len(pending) == 1
    assert pending[0]["effective_status"] == "running"


def test_reconcile_metric_cursor_remains_valid_after_source_is_terminalized(conn):
    fixture = _terminalization_proof_fixture(conn, suffix="window-cursor-z")
    store.record_reconcile_run_metric(
        conn,
        PID,
        run_id="stale-source-window-cursor-y",
        snapshot_id="full-stale-source-window-cursor-y",
        commit_sha="b" * 40,
        snapshot_kind="full",
        strategy="current_full_reconcile",
        graph_delta_mode="full_rebuild",
        status="running",
        evidence={"phase": "build"},
        created_at="2026-08-10T00:00:00Z",
    )
    conn.commit()
    first = store.list_reconcile_run_metrics_window(
        conn,
        PID,
        limit=1,
        strategy="current_full_reconcile",
        nonterminal_limit=1,
    )
    assert first["has_more"] is True
    assert first["next_cursor"].startswith("rrm1.")

    _insert_terminalization_overlay(conn, fixture, suffix="window-cursor-z")
    second = store.list_reconcile_run_metrics_window(
        conn,
        PID,
        limit=1,
        strategy="current_full_reconcile",
        nonterminal_limit=1,
        cursor=first["next_cursor"],
    )

    assert second["cursor_applied"] is True
    assert second["has_more"] is False
    pending = [
        row for row in second["rows"]
        if row["run_id"] == "stale-source-window-cursor-y"
    ]
    assert len(pending) == 1
    assert pending[0]["effective_status"] == "running"


def test_reconcile_metric_window_keeps_same_run_snapshot_identities_exact(conn):
    fixture = _terminalization_proof_fixture(conn, suffix="window-same-run")
    _insert_terminalization_overlay(conn, fixture, suffix="window-same-run")
    sibling_snapshot_id = fixture["source_snapshot_id"] + "-sibling"
    store.record_reconcile_run_metric(
        conn,
        PID,
        run_id=fixture["source_run_id"],
        snapshot_id=sibling_snapshot_id,
        commit_sha="c" * 40,
        snapshot_kind="full",
        strategy="current_full_reconcile",
        graph_delta_mode="full_rebuild",
        status="finalizing",
        evidence={"phase": "finalizing"},
        created_at="2026-08-10T00:00:00Z",
    )
    conn.commit()

    window = store.list_reconcile_run_metrics_window(
        conn,
        PID,
        limit=10,
        strategy="current_full_reconcile",
        nonterminal_limit=10,
    )
    same_run = {
        row["snapshot_id"]: row
        for row in window["rows"]
        if row["run_id"] == fixture["source_run_id"]
    }

    assert set(same_run) == {fixture["source_snapshot_id"], sibling_snapshot_id}
    assert same_run[fixture["source_snapshot_id"]]["effective_status"] == (
        "terminalized_stale"
    )
    assert same_run[sibling_snapshot_id]["effective_status"] == "finalizing"
    assert window["nonterminal_page_count"] == 1


def test_reconcile_metric_window_overlay_projection_is_physical_zero_write(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path)
    db_path = tmp_path / "terminalization-window-read.sqlite"
    setup = _file_connection(db_path)
    fixture = _terminalization_proof_fixture(setup, suffix="window-physical")
    _insert_terminalization_overlay(setup, fixture, suffix="window-physical")
    setup.close()

    reader = sqlite3.connect(db_path)
    reader.row_factory = sqlite3.Row
    before_bytes = db_path.read_bytes()
    before_changes = reader.total_changes
    before_schema = tuple(reader.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall())
    before_pragmas = tuple(
        reader.execute(f"PRAGMA {name}").fetchone()[0]
        for name in ("schema_version", "page_count", "freelist_count")
    )
    statements = []
    reader.set_trace_callback(statements.append)

    window = store.list_reconcile_run_metrics_window(
        reader,
        PID,
        limit=10,
        strategy="current_full_reconcile",
        nonterminal_limit=10,
    )
    reader.set_trace_callback(None)

    source = next(
        row for row in window["rows"]
        if row["run_id"] == fixture["source_run_id"]
    )
    assert source["effective_status"] == "terminalized_stale"
    assert reader.total_changes == before_changes
    assert reader.in_transaction is False
    assert statements
    assert not any(
        statement.lstrip().upper().startswith(
            ("INSERT", "UPDATE", "DELETE", "REPLACE", "ALTER", "DROP", "BEGIN")
        )
        for statement in statements
    )
    assert tuple(reader.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()) == before_schema
    assert tuple(
        reader.execute(f"PRAGMA {name}").fetchone()[0]
        for name in ("schema_version", "page_count", "freelist_count")
    ) == before_pragmas
    reader.close()
    assert db_path.read_bytes() == before_bytes


def test_reconcile_metric_window_dense_overlay_scan_continues_without_loop(
    conn,
    monkeypatch,
):
    _ensure_schema(conn)
    conn.executemany(
        """
        INSERT INTO reconcile_run_metrics (
          project_id, run_id, snapshot_id, snapshot_kind, strategy,
          graph_delta_mode, status, created_at
        ) VALUES (?, ?, ?, 'full', 'current_full_reconcile',
                  'full_rebuild', 'running', ?)
        """,
        [
            (
                PID,
                f"overlay-scan-{index:03d}",
                f"full-overlay-scan-{index:03d}",
                f"2026-08-10T00:00:00.{index:06d}Z",
            )
            for index in range(64, -1, -1)
        ],
    )
    conn.commit()
    original = store._project_reconcile_run_metric_with_overlay

    def project(_conn, project_id, row):
        projected = original(_conn, project_id, row)
        if int(str(projected["run_id"]).rsplit("-", 1)[1]) > 0:
            projected.update({
                "effective_status": "terminalized_stale",
                "is_terminal": True,
                "status_reason_code": "terminalization_overlay_valid",
            })
        return projected

    monkeypatch.setattr(
        store,
        "_project_reconcile_run_metric_with_overlay",
        project,
    )

    first = store.list_reconcile_run_metrics_window(
        conn,
        PID,
        limit=1,
        strategy="current_full_reconcile",
        nonterminal_limit=1,
    )
    assert first["nonterminal_page_count"] == 0
    assert first["has_more"] is True
    assert first["next_cursor"].startswith("rrm1.")

    second = store.list_reconcile_run_metrics_window(
        conn,
        PID,
        limit=1,
        strategy="current_full_reconcile",
        nonterminal_limit=1,
        cursor=first["next_cursor"],
    )
    assert second["nonterminal_page_count"] == 1
    assert second["has_more"] is False
    pending = [
        row for row in second["rows"] if row["run_id"] == "overlay-scan-000"
    ]
    assert len(pending) == 1
    assert pending[0]["effective_status"] == "running"


def test_current_full_build_claim_fences_two_connections_before_materialization(
    tmp_path,
):
    db_path = tmp_path / "claim-fence.sqlite"
    first = _file_connection(db_path)
    second = _file_connection(db_path)
    materialized: list[str] = []
    try:
        store.acquire_current_full_build_claim(
            first,
            PID,
            run_id="run-one",
            snapshot_id="full-same-snapshot",
            commit_sha="a" * 40,
            **_claim_owner("first"),
        )
        materialized.append("run-one")
        with pytest.raises(
            store.GraphSnapshotBuildClaimConflictError,
            match="current_full_snapshot_build_claimed",
        ):
            store.acquire_current_full_build_claim(
                second,
                PID,
                run_id="run-two",
                snapshot_id="full-same-snapshot",
                commit_sha="a" * 40,
                **_claim_owner("second"),
            )
        assert materialized == ["run-one"]
        assert first.execute(
            "SELECT COUNT(*) FROM graph_current_full_build_claim_history "
            "WHERE project_id = ? AND snapshot_id = ? AND status = 'active'",
            (PID, "full-same-snapshot"),
        ).fetchone()[0] == 1
        rows = first.execute(
            "SELECT run_id, status FROM reconcile_run_metrics "
            "WHERE project_id = ? AND snapshot_id = ?",
            (PID, "full-same-snapshot"),
        ).fetchall()
        assert [tuple(row) for row in rows] == [("run-one", "running")]
    finally:
        first.close()
        second.close()


def test_current_full_build_claim_and_running_metric_rollback_together(
    tmp_path, monkeypatch
):
    connection = _file_connection(tmp_path / "claim-rollback.sqlite")

    def fail_metric(*_args, **_kwargs):
        raise RuntimeError("metric write failed")

    monkeypatch.setattr(store, "record_reconcile_run_metric", fail_metric)
    try:
        with pytest.raises(RuntimeError, match="metric write failed"):
            store.acquire_current_full_build_claim(
                connection,
                PID,
                run_id="run-rollback",
                snapshot_id="full-rollback",
                commit_sha="b" * 40,
                **_claim_owner("rollback"),
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM graph_current_full_build_claim_history"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM reconcile_run_metrics"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_unreleased_current_full_build_claim_survives_owner_connection_crash(
    tmp_path,
):
    db_path = tmp_path / "claim-crash.sqlite"
    owner = _file_connection(db_path)
    store.acquire_current_full_build_claim(
        owner,
        PID,
        run_id="run-crashed-owner",
        snapshot_id="full-crash",
        commit_sha="c" * 40,
        **_claim_owner("crashed"),
    )
    owner.close()

    foreign = _file_connection(db_path)
    try:
        with pytest.raises(
            store.GraphSnapshotBuildClaimConflictError,
            match="current_full_snapshot_build_claimed",
        ):
            store.acquire_current_full_build_claim(
                foreign,
                PID,
                run_id="run-foreign-retry",
                snapshot_id="full-crash",
                commit_sha="c" * 40,
                **_claim_owner("foreign"),
            )
        claim = foreign.execute(
            "SELECT status, manager_start_identity "
            "FROM graph_current_full_build_claim_history"
        ).fetchone()
        assert dict(claim) == {
            "status": "active",
            "manager_start_identity": "manager-start-crashed",
        }
    finally:
        foreign.close()


def test_terminalized_current_full_build_identity_cannot_reacquire(conn):
    owner = _claim_owner("terminal")
    claim = store.acquire_current_full_build_claim(
        conn,
        PID,
        run_id="run-terminal",
        snapshot_id="full-terminal",
        commit_sha="d" * 40,
        **owner,
    )
    store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-terminal",
        commit_sha="d" * 40,
        snapshot_kind="full",
        graph_json={"deps_graph": {"nodes": []}},
        notes=json.dumps({"run_id": "run-terminal"}),
    )
    conn.commit()
    store.terminalize_current_full_build_claim(
        conn,
        PID,
        claim_id=claim["claim_id"],
        run_id="run-terminal",
        snapshot_id="full-terminal",
        commit_sha="d" * 40,
        terminal_status="candidate_ready",
        manager_start_identity=owner["manager_start_identity"],
    )

    with pytest.raises(
        store.GraphSnapshotBuildClaimConflictError,
        match="current_full_build_identity_terminalized",
    ):
        store.acquire_current_full_build_claim(
            conn,
            PID,
            run_id="run-terminal",
            snapshot_id="full-terminal",
            commit_sha="d" * 40,
            **owner,
        )
    history = conn.execute(
        "SELECT status, terminal_status FROM graph_current_full_build_claim_history"
    ).fetchall()
    assert [tuple(row) for row in history] == [("released", "candidate_ready")]


def test_existing_metric_identity_cannot_be_reacquired_or_reset(conn):
    store.record_reconcile_run_metric(
        conn,
        PID,
        run_id="run-existing-metric",
        snapshot_id="full-existing-metric",
        commit_sha="e" * 40,
        snapshot_kind="full",
        strategy="current_full_reconcile",
        graph_delta_mode="full_rebuild",
        status="failed",
    )
    conn.commit()

    with pytest.raises(
        store.GraphSnapshotBuildClaimConflictError,
        match="current_full_build_metric_identity_exists",
    ):
        store.acquire_current_full_build_claim(
            conn,
            PID,
            run_id="run-existing-metric",
            snapshot_id="full-existing-metric",
            commit_sha="e" * 40,
            **_claim_owner("existing-metric"),
        )
    assert conn.execute(
        "SELECT status FROM reconcile_run_metrics WHERE project_id = ? "
        "AND run_id = ? AND snapshot_id = ?",
        (PID, "run-existing-metric", "full-existing-metric"),
    ).fetchone()[0] == "failed"


@pytest.mark.parametrize("requested_commit", ["a" * 40, "b" * 40])
def test_claim_admission_fences_run_to_one_snapshot_identity(
    conn,
    requested_commit,
):
    scope = {"task_id": "task-run-identity"}
    store.record_reconcile_run_metric(
        conn,
        PID,
        run_id="run-one-snapshot-identity",
        snapshot_id="full-run-identity-a",
        commit_sha="a" * 40,
        snapshot_kind="full",
        strategy="current_full_reconcile",
        graph_delta_mode="full_rebuild",
        status="candidate_ready",
        evidence={"idempotency_scope": scope},
    )
    conn.commit()
    before_changes = conn.total_changes

    with pytest.raises(
        store.GraphSnapshotBuildClaimConflictError,
        match="current_full_run_snapshot_identity_conflict",
    ):
        store.acquire_current_full_build_claim(
            conn,
            PID,
            run_id="run-one-snapshot-identity",
            snapshot_id="full-run-identity-b",
            commit_sha=requested_commit,
            metric_evidence={"idempotency_scope": scope},
            **_claim_owner("run-identity"),
        )

    assert conn.total_changes == before_changes
    assert conn.execute(
        "SELECT COUNT(*) FROM graph_current_full_build_claim_history "
        "WHERE project_id = ? AND run_id = ?",
        (PID, "run-one-snapshot-identity"),
    ).fetchone()[0] == 0
    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT snapshot_id, commit_sha, status FROM reconcile_run_metrics "
            "WHERE project_id = ? AND run_id = ?",
            (PID, "run-one-snapshot-identity"),
        ).fetchall()
    ] == [("full-run-identity-a", "a" * 40, "candidate_ready")]


def test_concurrent_activation_and_fresh_claim_complete_once_without_overwrite(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "activation-versus-fresh-claim.sqlite"
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path)
    activation_conn = _file_connection(db_path)
    owner = _claim_owner("activation-race-origin")
    claim = store.acquire_current_full_build_claim(
        activation_conn,
        PID,
        run_id="activation-race-origin",
        snapshot_id="full-activation-race",
        commit_sha="c" * 40,
        metric_evidence={"idempotency_scope": {}},
        **owner,
    )
    store.create_graph_snapshot(
        activation_conn,
        PID,
        snapshot_id="full-activation-race",
        commit_sha="c" * 40,
        snapshot_kind="full",
        graph_json={"deps_graph": {"nodes": []}},
        notes=json.dumps({"run_id": "activation-race-origin"}),
    )
    activation_conn.commit()
    store.terminalize_current_full_build_claim(
        activation_conn,
        PID,
        claim_id=claim["claim_id"],
        run_id="activation-race-origin",
        snapshot_id="full-activation-race",
        commit_sha="c" * 40,
        terminal_status="candidate_ready",
        manager_start_identity=owner["manager_start_identity"],
        metric_evidence={
            "phase": "candidate_ready",
            "claim_id": claim["claim_id"],
            "idempotency_scope": {},
        },
    )
    companion_dir = store.snapshot_companion_dir(PID, "full-activation-race")
    companion_before = {
        name: (companion_dir / name).read_bytes()
        for name in (
            "graph.json",
            "file_inventory.json",
            "drift_ledger.json",
            "manifest.json",
        )
    }

    activation_conn.execute("BEGIN IMMEDIATE")
    store.activate_graph_snapshot(
        activation_conn,
        PID,
        "full-activation-race",
        auto_rebuild_projection=False,
        schema_ready=True,
        post_commit_hooks=False,
    )

    def fresh_claim_attempt() -> str:
        fresh = sqlite3.connect(db_path, timeout=1)
        fresh.row_factory = sqlite3.Row
        try:
            store.acquire_current_full_build_claim(
                fresh,
                PID,
                run_id="activation-race-fresh",
                snapshot_id="full-activation-race",
                commit_sha="c" * 40,
                metric_evidence={"idempotency_scope": {}},
                **_claim_owner("activation-race-fresh"),
            )
        except store.GraphSnapshotBuildClaimConflictError as exc:
            return exc.reason
        finally:
            fresh.close()
        return "unexpected_success"

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fresh_claim_attempt)
        time.sleep(0.05)
        store.record_reconcile_run_metric(
            activation_conn,
            PID,
            run_id="activation-race-origin",
            snapshot_id="full-activation-race",
            commit_sha="c" * 40,
            snapshot_kind="full",
            strategy="current_full_reconcile",
            graph_delta_mode="full_rebuild",
            status="complete",
            evidence={
                "phase": "atomic_finalize_complete",
                "idempotency_scope": {},
            },
            schema_ready=True,
        )
        activation_conn.commit()
        reason = future.result(timeout=2)

    assert reason == "current_full_snapshot_identity_exists"
    assert store.get_active_graph_snapshot(activation_conn, PID)[
        "snapshot_id"
    ] == "full-activation-race"
    assert activation_conn.execute(
        "SELECT COUNT(*) FROM reconcile_run_metrics WHERE project_id = ? "
        "AND run_id = ? AND status = 'complete'",
        (PID, "activation-race-origin"),
    ).fetchone()[0] == 1
    assert activation_conn.execute(
        "SELECT COUNT(*) FROM reconcile_run_metrics WHERE project_id = ? "
        "AND run_id = ?",
        (PID, "activation-race-fresh"),
    ).fetchone()[0] == 0
    assert activation_conn.execute(
        "SELECT COUNT(*) FROM graph_current_full_build_claim_history "
        "WHERE project_id = ? AND run_id = ?",
        (PID, "activation-race-fresh"),
    ).fetchone()[0] == 0
    assert {
        name: (companion_dir / name).read_bytes() for name in companion_before
    } == companion_before
    activation_conn.close()


def test_current_full_build_terminalization_rejects_commit_mismatch(conn):
    owner = _claim_owner("commit")
    claim = store.acquire_current_full_build_claim(
        conn,
        PID,
        run_id="run-commit",
        snapshot_id="full-commit",
        commit_sha="f" * 40,
        **owner,
    )
    with pytest.raises(
        store.GraphSnapshotBuildClaimConflictError,
        match="current_full_build_claim_commit_mismatch",
    ):
        store.terminalize_current_full_build_claim(
            conn,
            PID,
            claim_id=claim["claim_id"],
            run_id="run-commit",
            snapshot_id="full-commit",
            commit_sha="0" * 40,
            terminal_status="failed",
            manager_start_identity=owner["manager_start_identity"],
        )
    assert conn.execute(
        "SELECT status FROM graph_current_full_build_claim_history "
        "WHERE claim_id = ?",
        (claim["claim_id"],),
    ).fetchone()[0] == "active"


def test_candidate_ready_terminalization_atomically_fails_persisted_commit_drift(
    conn,
):
    owner = _claim_owner("persisted-commit")
    claim = store.acquire_current_full_build_claim(
        conn,
        PID,
        run_id="run-persisted-commit",
        snapshot_id="full-persisted-commit",
        commit_sha="1" * 40,
        **owner,
    )
    store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-persisted-commit",
        commit_sha="2" * 40,
        snapshot_kind="full",
        graph_json={"deps_graph": {"nodes": []}},
        notes=json.dumps({"run_id": "run-persisted-commit"}),
    )
    conn.commit()

    with pytest.raises(
        store.GraphSnapshotBuildClaimConflictError,
        match="current_full_candidate_snapshot_commit_mismatch",
    ):
        store.terminalize_current_full_build_claim(
            conn,
            PID,
            claim_id=claim["claim_id"],
            run_id="run-persisted-commit",
            snapshot_id="full-persisted-commit",
            commit_sha="1" * 40,
            terminal_status="candidate_ready",
            manager_start_identity=owner["manager_start_identity"],
        )

    terminal_claim = conn.execute(
        "SELECT status, terminal_status FROM graph_current_full_build_claim_history "
        "WHERE claim_id = ?",
        (claim["claim_id"],),
    ).fetchone()
    assert dict(terminal_claim) == {"status": "released", "terminal_status": "failed"}
    metric = conn.execute(
        "SELECT status, evidence_json FROM reconcile_run_metrics WHERE project_id = ? "
        "AND run_id = ? AND snapshot_id = ?",
        (PID, "run-persisted-commit", "full-persisted-commit"),
    ).fetchone()
    assert metric["status"] == "failed"
    evidence = json.loads(metric["evidence_json"])
    assert evidence["error"] == "current_full_candidate_snapshot_commit_mismatch"
    assert evidence["candidate_released_as_ready"] is False


def test_candidate_ready_terminalization_accepts_complete_companion_files(conn):
    owner = _claim_owner("complete-companions")
    claim = store.acquire_current_full_build_claim(
        conn,
        PID,
        run_id="run-complete-companions",
        snapshot_id="full-complete-companions",
        commit_sha="3" * 40,
        **owner,
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-complete-companions",
        commit_sha="3" * 40,
        snapshot_kind="full",
        graph_json={"deps_graph": {"nodes": []}},
        file_inventory=[{"path": "agent/governance/server.py"}],
        drift_ledger=[],
        notes=json.dumps({"run_id": "run-complete-companions"}),
    )
    conn.commit()

    integrity = store.validate_snapshot_companion_integrity(snapshot)
    manifest_path = (
        store.snapshot_companion_dir(PID, snapshot["snapshot_id"])
        / "manifest.json"
    )
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    store.write_companion_files(
        PID,
        snapshot["snapshot_id"],
        graph_json={"deps_graph": {"nodes": []}},
        file_inventory=[{"path": "agent/governance/server.py"}],
        drift_ledger=[],
    )
    assert manifest_path.read_bytes() == manifest_bytes
    terminal = store.terminalize_current_full_build_claim(
        conn,
        PID,
        claim_id=claim["claim_id"],
        run_id="run-complete-companions",
        snapshot_id="full-complete-companions",
        commit_sha="3" * 40,
        terminal_status="candidate_ready",
        manager_start_identity=owner["manager_start_identity"],
    )

    assert integrity["valid"] is True
    assert set(manifest) == {
        "project_id",
        "snapshot_id",
        "graph_sha256",
        "inventory_sha256",
        "drift_sha256",
        "created_at",
    }
    assert manifest["created_at"] == snapshot["created_at"]
    assert manifest_bytes == store._json(manifest).encode("utf-8")
    assert terminal["status"] == "released"
    assert terminal["terminal_status"] == "candidate_ready"


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("extra_field", "current_full_candidate_manifest_schema_mismatch"),
        ("forged_created_at", "current_full_candidate_manifest_binding_mismatch"),
        ("pretty_json", "current_full_candidate_manifest_not_canonical"),
        ("reordered_json", "current_full_candidate_manifest_not_canonical"),
    ],
)
def test_snapshot_companion_manifest_is_exactly_bound_and_canonical(
    conn,
    mutation,
    expected_error,
):
    snapshot_id = f"full-manifest-exact-{mutation}"
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id=snapshot_id,
        commit_sha="5" * 40,
        snapshot_kind="full",
        graph_json={"deps_graph": {"nodes": []}},
        file_inventory=[],
        drift_ledger=[],
    )
    conn.commit()
    manifest_path = store.snapshot_companion_dir(PID, snapshot_id) / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    if mutation == "extra_field":
        manifest["legacy"] = True
        replacement = store._json(manifest).encode("utf-8")
    elif mutation == "forged_created_at":
        manifest["created_at"] = "2026-08-09T00:00:00Z"
        replacement = store._json(manifest).encode("utf-8")
    elif mutation == "pretty_json":
        replacement = json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
    elif mutation == "reordered_json":
        replacement = json.dumps(
            dict(reversed(list(manifest.items()))),
            ensure_ascii=False,
        ).encode("utf-8")
    else:
        raise AssertionError(f"unhandled mutation: {mutation}")
    manifest_path.write_bytes(replacement)

    integrity = store.validate_snapshot_companion_integrity(snapshot)

    assert integrity["valid"] is False
    assert integrity["error"] == expected_error
    assert "manifest_path" not in integrity
    assert all("path" not in value for value in integrity["files"].values())


@pytest.mark.parametrize(
    ("filename", "replacement", "expected_error"),
    [
        (
            "graph.json",
            b'{"corrupt":true}',
            "current_full_candidate_graph_companion_hash_mismatch",
        ),
        (
            "file_inventory.json",
            None,
            "current_full_candidate_inventory_companion_missing",
        ),
    ],
)
def test_candidate_ready_terminalization_atomically_fails_companion_corruption(
    conn,
    filename,
    replacement,
    expected_error,
):
    suffix = filename.replace(".", "-")
    run_id = f"run-companion-{suffix}"
    snapshot_id = f"full-companion-{suffix}"
    owner = _claim_owner(suffix)
    claim = store.acquire_current_full_build_claim(
        conn,
        PID,
        run_id=run_id,
        snapshot_id=snapshot_id,
        commit_sha="4" * 40,
        **owner,
    )
    store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id=snapshot_id,
        commit_sha="4" * 40,
        snapshot_kind="full",
        graph_json={"deps_graph": {"nodes": []}},
        file_inventory=[{"path": "agent/governance/server.py"}],
        drift_ledger=[],
        notes=json.dumps({"run_id": run_id}),
    )
    conn.commit()
    companion = store.snapshot_companion_dir(PID, snapshot_id) / filename
    if replacement is None:
        companion.unlink()
    else:
        companion.write_bytes(replacement)

    with pytest.raises(
        store.GraphSnapshotBuildClaimConflictError,
        match=expected_error,
    ):
        store.terminalize_current_full_build_claim(
            conn,
            PID,
            claim_id=claim["claim_id"],
            run_id=run_id,
            snapshot_id=snapshot_id,
            commit_sha="4" * 40,
            terminal_status="candidate_ready",
            manager_start_identity=owner["manager_start_identity"],
        )

    assert dict(
        conn.execute(
            "SELECT status, terminal_status FROM "
            "graph_current_full_build_claim_history WHERE claim_id = ?",
            (claim["claim_id"],),
        ).fetchone()
    ) == {"status": "released", "terminal_status": "failed"}
    metric = conn.execute(
        "SELECT status, evidence_json FROM reconcile_run_metrics "
        "WHERE project_id = ? AND run_id = ? AND snapshot_id = ?",
        (PID, run_id, snapshot_id),
    ).fetchone()
    assert metric["status"] == "failed"
    evidence = json.loads(metric["evidence_json"])
    assert evidence["error"] == expected_error
    assert evidence["candidate_released_as_ready"] is False


def test_reconcile_run_metrics_backfills_from_snapshot_notes(conn, tmp_path):
    _ensure_schema(conn)
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    trace_summary = trace_dir / "summary.json"
    trace_summary.write_text(
        json.dumps({"status": "ok", "elapsed_ms": 1234}),
        encoding="utf-8",
    )
    notes = {
        "run_id": "scope-reconcile-head",
        "scope_reconcile_strategy": "incremental_graph_delta",
        "scope_graph_delta_mode": "metadata_only",
        "scope_file_delta": {"changed_file_count": 1, "impacted_file_count": 1},
        "pending_scope_reconcile": {
            "active_graph_commit": "base",
            "scope_graph_events": {"event_count": 2},
        },
        "trace": {"summary_path": str(trace_summary)},
    }
    store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-head",
        commit_sha="head",
        snapshot_kind="scope",
        graph_json={"deps_graph": {"nodes": [{"id": "L7.1"}], "edges": []}},
        notes=json.dumps(notes),
    )

    result = store.backfill_reconcile_run_metrics_from_snapshots(conn, PID)
    assert result["imported"] == 1
    row = store.list_reconcile_run_metrics(conn, PID)[0]
    assert row["run_id"] == "scope-reconcile-head"
    assert row["elapsed_ms"] == 1234
    assert row["event_count"] == 2


def test_mark_pending_scope_failed_preserves_recovery_evidence(conn):
    _ensure_schema(conn)
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="head",
        parent_commit_sha="base",
        status=store.PENDING_STATUS_RUNNING,
        evidence={"source": "direct_update_graph"},
    )

    result = store.mark_pending_scope_reconcile_failed(
        conn,
        PID,
        commit_sha="head",
        actor="test",
        reason="client disconnected",
    )

    assert result["updated_count"] == 1
    row = store.list_pending_scope_reconcile(conn, PID, commit_shas=["head"])[0]
    assert row["status"] == store.PENDING_STATUS_FAILED
    evidence = json.loads(row["evidence_json"])
    assert evidence["recoverable"] is True
    assert evidence["recovery_action"] == "force_requeue_pending_scope"


def test_recover_stale_pending_scope_marks_old_running_failed(conn):
    _ensure_schema(conn)
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="old-running",
        parent_commit_sha="base",
        status=store.PENDING_STATUS_RUNNING,
        evidence={"source": "direct_update_graph"},
    )
    conn.execute(
        """
        UPDATE pending_scope_reconcile
        SET queued_at='2026-01-01T00:00:00Z'
        WHERE project_id=? AND commit_sha=?
        """,
        (PID, "old-running"),
    )

    result = store.recover_stale_pending_scope_reconcile(
        conn,
        PID,
        max_running_seconds=1,
        actor="test",
    )

    assert result["recovered_count"] == 1
    row = store.list_pending_scope_reconcile(conn, PID, commit_shas=["old-running"])[0]
    assert row["status"] == store.PENDING_STATUS_FAILED
    evidence = json.loads(row["evidence_json"])
    assert evidence["source"] == "pending_scope_stale_running_recovery"
    assert evidence["recoverable"] is True


def test_waive_pending_scope_reconcile_preserves_materialized_rows(conn):
    _ensure_schema(conn)
    for commit, status in [
        ("queued", store.PENDING_STATUS_QUEUED),
        ("running", store.PENDING_STATUS_RUNNING),
        ("failed", store.PENDING_STATUS_FAILED),
        ("done", store.PENDING_STATUS_MATERIALIZED),
    ]:
        store.queue_pending_scope_reconcile(
            conn,
            PID,
            commit_sha=commit,
            parent_commit_sha="old",
            status=status,
            evidence={"source": "test"},
        )

    result = store.waive_pending_scope_reconcile(
        conn,
        PID,
        snapshot_id="full-head",
        actor="test",
        reason="scope materializer bug",
    )

    assert result["waived_count"] == 3
    rows = conn.execute(
        """
        SELECT commit_sha, status, snapshot_id, evidence_json
        FROM pending_scope_reconcile
        WHERE project_id=? ORDER BY commit_sha
        """,
        (PID,),
    ).fetchall()
    statuses = {row["commit_sha"]: row["status"] for row in rows}
    assert statuses == {
        "done": store.PENDING_STATUS_MATERIALIZED,
        "failed": store.PENDING_STATUS_WAIVED,
        "queued": store.PENDING_STATUS_WAIVED,
        "running": store.PENDING_STATUS_WAIVED,
    }
    waived = next(row for row in rows if row["commit_sha"] == "queued")
    assert waived["snapshot_id"] == "full-head"
    assert json.loads(waived["evidence_json"])["reason"] == "scope materializer bug"


def test_finalize_graph_snapshot_activates_and_materializes_matching_pending(conn):
    _ensure_schema(conn)
    old = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="imported-old-finalize",
        commit_sha="old",
        snapshot_kind="imported",
    )
    new = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-new-finalize",
        commit_sha="new",
        snapshot_kind="full",
    )
    store.activate_graph_snapshot(conn, PID, old["snapshot_id"])
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="new",
        parent_commit_sha="old",
        evidence={"source": "test"},
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="other",
        parent_commit_sha="old",
        evidence={"source": "test"},
    )

    result = store.finalize_graph_snapshot(
        conn,
        PID,
        new["snapshot_id"],
        target_commit_sha="new",
        expected_old_snapshot_id=old["snapshot_id"],
        actor="test",
        evidence={"signoff": "unit-test"},
    )

    assert result["pending_materialized_count"] == 1
    assert result["activation"]["previous_snapshot_id"] == old["snapshot_id"]
    active = store.get_active_graph_snapshot(conn, PID)
    assert active["snapshot_id"] == new["snapshot_id"]
    pending = conn.execute(
        "SELECT status, snapshot_id, evidence_json FROM pending_scope_reconcile WHERE project_id=? AND commit_sha=?",
        (PID, "new"),
    ).fetchone()
    assert pending["status"] == store.PENDING_STATUS_MATERIALIZED
    assert pending["snapshot_id"] == new["snapshot_id"]
    assert json.loads(pending["evidence_json"])["signoff"] == "unit-test"
    other = conn.execute(
        "SELECT status FROM pending_scope_reconcile WHERE project_id=? AND commit_sha=?",
        (PID, "other"),
    ).fetchone()
    assert other["status"] == store.PENDING_STATUS_QUEUED


def test_finalize_graph_snapshot_materializes_explicit_covered_commits(conn):
    _ensure_schema(conn)
    old = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="imported-old-covered",
        commit_sha="old",
        snapshot_kind="imported",
    )
    new = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-new-covered",
        commit_sha="new",
        snapshot_kind="scope",
    )
    store.activate_graph_snapshot(conn, PID, old["snapshot_id"])
    for commit in ("a1", "a2", "new", "future"):
        store.queue_pending_scope_reconcile(
            conn,
            PID,
            commit_sha=commit,
            parent_commit_sha="old",
            evidence={"source": "test"},
        )

    result = store.finalize_graph_snapshot(
        conn,
        PID,
        new["snapshot_id"],
        target_commit_sha="new",
        expected_old_snapshot_id=old["snapshot_id"],
        covered_commit_shas=["a1", "a2", "new"],
    )

    assert result["pending_materialized_count"] == 3
    rows = conn.execute(
        """
        SELECT commit_sha, status, snapshot_id FROM pending_scope_reconcile
        WHERE project_id=? ORDER BY commit_sha
        """,
        (PID,),
    ).fetchall()
    statuses = {row["commit_sha"]: row["status"] for row in rows}
    assert statuses == {
        "a1": store.PENDING_STATUS_MATERIALIZED,
        "a2": store.PENDING_STATUS_MATERIALIZED,
        "future": store.PENDING_STATUS_QUEUED,
        "new": store.PENDING_STATUS_MATERIALIZED,
    }
    assert {
        row["snapshot_id"] for row in rows
        if row["status"] == store.PENDING_STATUS_MATERIALIZED
    } == {new["snapshot_id"]}


def test_finalize_graph_snapshot_rejects_commit_mismatch_and_stale_active(conn):
    _ensure_schema(conn)
    old = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="imported-old-stale",
        commit_sha="old",
        snapshot_kind="imported",
    )
    new = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-new-stale",
        commit_sha="new",
        snapshot_kind="full",
    )
    store.activate_graph_snapshot(conn, PID, old["snapshot_id"])

    with pytest.raises(ValueError):
        store.finalize_graph_snapshot(
            conn,
            PID,
            new["snapshot_id"],
            target_commit_sha="different",
        )

    with pytest.raises(store.GraphSnapshotConflictError):
        store.finalize_graph_snapshot(
            conn,
            PID,
            new["snapshot_id"],
            target_commit_sha="new",
            expected_old_snapshot_id="not-active",
        )

    active = store.get_active_graph_snapshot(conn, PID)
    assert active["snapshot_id"] == old["snapshot_id"]


# ---------------------------------------------------------------------------
# Retention policy tests
# ---------------------------------------------------------------------------


def test_retention_selection_protects_active_snapshot(conn, tmp_path, bundle_namespace):
    """Active snapshot must never appear as a GC candidate."""
    _ensure_schema(conn)
    active = store.create_graph_snapshot(
        conn, PID, snapshot_id="scope-act-001", commit_sha="act", snapshot_kind="scope"
    )
    store.activate_graph_snapshot(conn, PID, active["snapshot_id"], auto_rebuild_projection=False)
    # Add several old snapshots that should be candidates
    for i in range(5):
        store.create_graph_snapshot(
            conn, PID, snapshot_id=f"scope-old-{i:03d}", commit_sha=f"old{i}", snapshot_kind="scope"
        )

    result = store.select_snapshot_retention_candidates(conn, PID, keep_last_n=1)
    protected_ids = {item["snapshot_id"] for item in result["protected"]}
    candidate_ids = {item["snapshot_id"] for item in result["candidates"]}

    assert active["snapshot_id"] in protected_ids, "active snapshot must be protected"
    assert active["snapshot_id"] not in candidate_ids, "active snapshot must not be a candidate"


def test_retention_selection_protects_keep_last_n(conn, tmp_path, bundle_namespace):
    """The most recent N snapshots must be protected."""
    _ensure_schema(conn)
    snap_ids = []
    for i in range(8):
        s = store.create_graph_snapshot(
            conn, PID, snapshot_id=f"scope-rr-{i:03d}", commit_sha=f"rr{i}", snapshot_kind="scope"
        )
        snap_ids.append(s["snapshot_id"])

    result = store.select_snapshot_retention_candidates(conn, PID, keep_last_n=3)
    protected_ids = {item["snapshot_id"] for item in result["protected"]}
    candidate_ids = {item["snapshot_id"] for item in result["candidates"]}

    # The 3 most recent should be protected; older ones should be candidates
    for sid in protected_ids:
        assert sid not in candidate_ids, f"{sid} must not be both protected and candidate"
    # At least some older entries should be candidates (by DB id; no dirs on disk so 0 bytes)
    # We just confirm protected and candidates are disjoint
    assert (set(protected_ids) & set(candidate_ids)) == set()


def test_retention_selection_protects_full_baseline(conn, tmp_path, bundle_namespace):
    """The most recent 'full' snapshot must always be protected."""
    _ensure_schema(conn)
    full = store.create_graph_snapshot(
        conn, PID, snapshot_id="full-base-retain", commit_sha="base", snapshot_kind="full"
    )
    # Add many scopes on top
    for i in range(15):
        store.create_graph_snapshot(
            conn, PID, snapshot_id=f"scope-layer-{i:03d}", commit_sha=f"c{i}", snapshot_kind="scope"
        )

    # With very small keep_last_n to push full baseline out
    result = store.select_snapshot_retention_candidates(conn, PID, keep_last_n=2)
    protected_ids = {item["snapshot_id"] for item in result["protected"]}
    assert full["snapshot_id"] in protected_ids, "most recent full baseline must be protected"


def test_retention_selection_protects_reconcile_in_progress(conn, tmp_path, bundle_namespace):
    """Snapshots referenced by running reconcile rows must be protected."""
    _ensure_schema(conn)
    active = store.create_graph_snapshot(
        conn, PID, snapshot_id="full-base-rip", commit_sha="base", snapshot_kind="full"
    )
    store.activate_graph_snapshot(conn, PID, active["snapshot_id"], auto_rebuild_projection=False)
    # Create a snapshot that is being reconciled
    in_progress = store.create_graph_snapshot(
        conn, PID, snapshot_id="scope-in-progress-001", commit_sha="ip1", snapshot_kind="scope"
    )
    # Queue a pending scope row pointing to in-progress snapshot
    store.queue_pending_scope_reconcile(
        conn, PID, commit_sha="ip1", parent_commit_sha="base",
        status=store.PENDING_STATUS_RUNNING,
        evidence={"source": "test"},
    )
    # Manually update the snapshot_id on the pending row
    conn.execute(
        "UPDATE pending_scope_reconcile SET snapshot_id=? WHERE project_id=? AND commit_sha=?",
        (in_progress["snapshot_id"], PID, "ip1"),
    )

    result = store.select_snapshot_retention_candidates(conn, PID, keep_last_n=0)
    protected_ids = {item["snapshot_id"] for item in result["protected"]}
    assert in_progress["snapshot_id"] in protected_ids, "in-progress reconcile snapshot must be protected"


def test_retention_gc_dry_run_does_not_delete(conn, tmp_path):
    """Dry-run must not delete any directories.

    We create an active snapshot directly (no further activation GC fires),
    then manually create an extra dir that mimics a stale snapshot and verify
    dry-run does not remove it.
    """
    _ensure_schema(conn)
    # Activate a full baseline (this runs post-activation GC, but there are no candidates yet)
    active = store.create_graph_snapshot(
        conn, PID, snapshot_id="full-active-dry", commit_sha="adry", snapshot_kind="full"
    )
    store.activate_graph_snapshot(conn, PID, active["snapshot_id"], auto_rebuild_projection=False)

    # Now manually create a stale snapshot dir that is NOT tracked by the DB
    # (simulates a leftover from a crashed reconcile)
    stale_dir = tmp_path / PID / "graph-snapshots" / "scope-orphan-dry-001"
    stale_dir.mkdir(parents=True, exist_ok=True)
    (stale_dir / "graph.json").write_bytes(b"{}")
    assert stale_dir.exists()

    # Run dry-run GC — stale dir is a disk-only candidate but must not be deleted
    result = store.run_snapshot_retention_gc(conn, PID, keep_last_n=0, dry_run=True)
    assert result["dry_run"] is True
    assert stale_dir.exists(), "dry-run must not delete any dirs"
    # A missing trace census keeps even a disk-only orphan protected.
    protected = {item["snapshot_id"]: item for item in result["protected"]}
    assert "reference_authority_incomplete" in protected["scope-orphan-dry-001"]["reasons"]


def test_retention_gc_apply_deletes_candidates_and_protects_active(conn, tmp_path):
    """Apply GC must delete candidate dirs and never delete the active snapshot dir."""
    _ensure_schema(conn)
    # Create several old candidates
    old_snaps = []
    for i in range(5):
        s = store.create_graph_snapshot(
            conn, PID, snapshot_id=f"scope-gcapply-{i:03d}", commit_sha=f"gc{i}", snapshot_kind="scope"
        )
        old_snaps.append(s)

    # Make one snapshot active
    active = store.create_graph_snapshot(
        conn, PID, snapshot_id="full-active-gcapply", commit_sha="gcact", snapshot_kind="full"
    )
    store.activate_graph_snapshot(conn, PID, active["snapshot_id"], auto_rebuild_projection=False)
    active_dir = tmp_path / PID / "graph-snapshots" / active["snapshot_id"]
    assert active_dir.exists(), "active companion dir should exist"

    result = store.run_snapshot_retention_gc(conn, PID, keep_last_n=0, dry_run=False)
    assert result["ok"] is False
    assert result["error"] == "snapshot_retention_authority_incomplete"
    # Active snapshot dir must survive
    assert active_dir.exists(), "active snapshot dir must never be deleted"
    assert all((tmp_path / PID / "graph-snapshots" / item["snapshot_id"]).exists()
               for item in old_snaps)
    # No errors
    assert result["errors"] == []


def test_retention_gc_is_idempotent(conn, tmp_path):
    """Running GC twice must not error even if dirs are already gone."""
    _ensure_schema(conn)
    snap = store.create_graph_snapshot(
        conn, PID, snapshot_id="scope-idem-001", commit_sha="idem1", snapshot_kind="scope"
    )
    active = store.create_graph_snapshot(
        conn, PID, snapshot_id="full-idem-active", commit_sha="ideact", snapshot_kind="full"
    )
    store.activate_graph_snapshot(conn, PID, active["snapshot_id"], auto_rebuild_projection=False)

    r1 = store.run_snapshot_retention_gc(conn, PID, keep_last_n=0, dry_run=False)
    r2 = store.run_snapshot_retention_gc(conn, PID, keep_last_n=0, dry_run=False)
    assert r1["ok"] is False
    assert r2["ok"] is False
    assert r1["deleted_count"] == r2["deleted_count"] == 0
    assert r2["errors"] == [], "second GC run must not produce errors"


def test_retention_selector_protects_both_trace_columns_and_candidate_status(conn, bundle_namespace):
    _ensure_schema(conn)
    conn.execute("CREATE TABLE graph_query_traces (project_id TEXT, snapshot_id TEXT, canonical_base_snapshot_id TEXT)")
    first = store.create_graph_snapshot(
        conn, PID, snapshot_id="scope-trace-first", commit_sha="a", snapshot_kind="scope",
    )
    second = store.create_graph_snapshot(
        conn, PID, snapshot_id="scope-trace-base", commit_sha="b", snapshot_kind="scope",
    )
    candidate = store.create_graph_snapshot(
        conn, PID, snapshot_id="scope-candidate", commit_sha="c", snapshot_kind="scope",
    )
    for item in (first, second):
        conn.execute("UPDATE graph_snapshots SET status='superseded' WHERE project_id=? AND snapshot_id=?",
                     (PID, item["snapshot_id"]))
    conn.execute("UPDATE graph_snapshots SET status='candidate' WHERE project_id=? AND snapshot_id=?",
                 (PID, candidate["snapshot_id"]))
    conn.execute("INSERT INTO graph_query_traces VALUES (?,?,?)",
                 (PID, first["snapshot_id"], second["snapshot_id"]))
    selection = store.select_snapshot_retention_candidates(conn, PID, keep_last_n=0)
    protected = {item["snapshot_id"]: item["reasons"] for item in selection["protected"]}
    assert "graph_trace_reference" in protected[first["snapshot_id"]]
    assert "graph_trace_reference" in protected[second["snapshot_id"]]
    assert "candidate_status" in protected[candidate["snapshot_id"]]
    assert not {first["snapshot_id"], second["snapshot_id"], candidate["snapshot_id"]} & {
        item["snapshot_id"] for item in selection["candidates"]
    }


def test_retention_selector_scan_error_does_not_claim_known_zero(conn, monkeypatch):
    _ensure_schema(conn)
    snapshot = store.create_graph_snapshot(
        conn, PID, snapshot_id="scan-unreadable", commit_sha="a", snapshot_kind="scope",
    )
    conn.execute("UPDATE graph_snapshots SET status='superseded' WHERE project_id=? AND snapshot_id=?",
                 (PID, snapshot["snapshot_id"]))
    monkeypatch.setattr(store, "get_snapshot_retention_config",
                        lambda *_a, **_kw: {"keep_last_n": 0})
    monkeypatch.setattr(store, "snapshot_retention_reference_state",
                        lambda *_a: {"protected": {}, "complete": True,
                                     "refusal_reasons": []})
    monkeypatch.setattr(store, "_bundle_referenced_snapshot_ids", lambda: set())
    actual_rglob = Path.rglob

    def fail_selected_scan(path, pattern):
        if path.name == "scan-unreadable":
            raise OSError("simulated snapshot scan failure")
        return actual_rglob(path, pattern)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rglob", fail_selected_scan)
        selection = store.select_snapshot_retention_candidates(
            conn, PID, keep_last_n=0,
        )
    selected = next(row for row in selection["candidates"]
                    if row["snapshot_id"] == "scan-unreadable")
    assert selected["size_bytes"] is None


def test_retention_reference_overflow_fails_closed(conn, tmp_path, bundle_namespace):
    _ensure_schema(conn)
    conn.execute("CREATE TABLE graph_query_traces (project_id TEXT, snapshot_id TEXT, canonical_base_snapshot_id TEXT)")
    snapshot = store.create_graph_snapshot(
        conn, PID, snapshot_id="scope-overflow", commit_sha="a", snapshot_kind="scope",
    )
    conn.executemany("INSERT INTO graph_query_traces VALUES (?,?,?)", [
        (PID, f"trace-{idx:04d}", "") for idx in range(2001)
    ])
    selection = store.select_snapshot_retention_candidates(conn, PID, keep_last_n=0)
    assert selection["reference_authority_complete"] is False
    assert "graph_query_traces_reference_window_unbounded" in selection["global_refusal_reasons"]
    assert "reference_authority_incomplete" in next(
        item["reasons"] for item in selection["protected"]
        if item["snapshot_id"] == snapshot["snapshot_id"]
    )
    result = store.run_snapshot_retention_gc(
        conn, PID, keep_last_n=0, dry_run=False, destructive_authorized=True,
    )
    assert result["deleted_count"] == 0
    assert (tmp_path / PID / "graph-snapshots" / snapshot["snapshot_id"]).exists()


def test_retention_explicit_fixture_authority_can_remove_unreferenced_superseded_dir(conn, tmp_path, bundle_namespace):
    _ensure_schema(conn)
    conn.execute("CREATE TABLE graph_query_traces (project_id TEXT, snapshot_id TEXT, canonical_base_snapshot_id TEXT)")
    old = store.create_graph_snapshot(
        conn, PID, snapshot_id="scope-safe-fixture", commit_sha="a", snapshot_kind="scope",
    )
    conn.execute("UPDATE graph_snapshots SET status='superseded' WHERE project_id=? AND snapshot_id=?",
                 (PID, old["snapshot_id"]))
    old_dir = tmp_path / PID / "graph-snapshots" / old["snapshot_id"]
    selection = store.select_snapshot_retention_candidates(conn, PID, keep_last_n=0)
    assert selection["reference_authority_complete"] is True
    assert old["snapshot_id"] in {item["snapshot_id"] for item in selection["candidates"]}
    refused = store.run_snapshot_retention_gc(conn, PID, keep_last_n=0, dry_run=False)
    assert refused["deleted_count"] == 0
    assert old_dir.exists()
    applied = store.run_snapshot_retention_gc(
        conn, PID, keep_last_n=0, dry_run=False, destructive_authorized=True,
    )
    assert applied["deleted_count"] == 1
    assert not old_dir.exists()


def test_retention_rechecks_each_item_and_reports_partial_refusal(conn, tmp_path, monkeypatch):
    _ensure_schema(conn)
    conn.execute("CREATE TABLE graph_query_traces (project_id TEXT, snapshot_id TEXT, canonical_base_snapshot_id TEXT)")
    for sid in ("scope-a-recheck", "scope-b-recheck"):
        store.create_graph_snapshot(conn, PID, snapshot_id=sid, commit_sha=sid,
                                    snapshot_kind="scope")
        conn.execute("UPDATE graph_snapshots SET status='superseded' "
                     "WHERE project_id=? AND snapshot_id=?", (PID, sid))
    original = store.select_snapshot_retention_candidates
    calls = 0

    def changed(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            conn.execute("INSERT INTO graph_query_traces VALUES (?,?,?)",
                         (PID, "scope-b-recheck", ""))
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "select_snapshot_retention_candidates", changed)
    result = store.run_snapshot_retention_gc(
        conn, PID, keep_last_n=0, dry_run=False, destructive_authorized=True,
    )
    assert result["ok"] is False
    assert result["deleted_count"] == 1
    assert result["errors"][0]["error"] == "snapshot_retention_item_drift_refused"
    assert not (tmp_path / PID / "graph-snapshots" / "scope-a-recheck").exists()
    assert (tmp_path / PID / "graph-snapshots" / "scope-b-recheck").exists()


def test_retention_census_reads_unscoped_qa_payload_arrays_and_fails_on_malformed(conn, bundle_namespace):
    _ensure_schema(conn)
    conn.execute("CREATE TABLE graph_query_traces (project_id TEXT, snapshot_id TEXT, canonical_base_snapshot_id TEXT)")
    snapshot = store.create_graph_snapshot(
        conn, PID, snapshot_id="scope-qa-payload", commit_sha="a", snapshot_kind="scope",
    )
    conn.execute("UPDATE graph_snapshots SET status='superseded' WHERE project_id=? AND snapshot_id=?",
                 (PID, snapshot["snapshot_id"]))
    conn.execute("CREATE TABLE qa_custody_without_project (payload_json TEXT)")
    conn.execute("INSERT INTO qa_custody_without_project VALUES (?)",
                 (json.dumps({"snapshot_ids": [snapshot["snapshot_id"]]}),))
    selection = store.select_snapshot_retention_candidates(conn, PID, keep_last_n=0)
    reasons = next(item["reasons"] for item in selection["protected"]
                   if item["snapshot_id"] == snapshot["snapshot_id"])
    assert any("qa_custody_without_project" in reason for reason in reasons)
    conn.execute("INSERT INTO qa_custody_without_project VALUES ('{bad json')")
    unavailable = store.select_snapshot_retention_candidates(conn, PID, keep_last_n=0)
    assert unavailable["reference_authority_complete"] is False
    assert "qa_custody_without_project_payload_unreadable" in unavailable["global_refusal_reasons"]


def _typed_reference_setup(connection, count=70):
    _ensure_schema(connection)
    store.ensure_schema(connection)
    connection.execute("CREATE TABLE graph_query_traces (project_id TEXT, snapshot_id TEXT, canonical_base_snapshot_id TEXT)")
    from agent.governance.contracts.runtime import SQLiteContractExecutionStore
    connection.executescript(SQLiteContractExecutionStore.SCHEMA_SQL)
    for sid in ("scope-current", "scope-durable"):
        connection.execute("INSERT INTO graph_snapshots(project_id,snapshot_id,commit_sha,snapshot_kind,status,created_at) "
                           "VALUES (?,?,?,'scope','superseded','2020')", (PID, sid, "fixture"))
    for index in range(count):
        payload = json.dumps({"runtime_guide": {"next_legal_action": 0}, "history": "audit"})
        connection.execute("INSERT INTO contract_runtime_executions "
            "(contract_execution_id,project_id,backlog_id,contract_id,version,revision,execution_state_revision,record_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", (f"cex-{index:03d}", PID, "backlog", "contract", "1", "1", 1, payload, "now", "now"))
    connection.commit()


def test_typed_census_measured_shape_preserves_current_and_durable_large_audits(conn):
    _typed_reference_setup(conn, count=109)
    # Real owner count/physical-identity scalars are supported metadata, while
    # unknown ordinary binary payloads must never prove negative authority.
    conn.execute("INSERT INTO reconcile_run_metrics(project_id,run_id,snapshot_id,status,created_at) "
                 "VALUES (?, 'typed-owned-metadata', 'scope-current', 'completed', '2020')", (PID,))
    # Reproduce inventory/owner proportions without claiming 114 eligible items.
    conn.executemany("INSERT INTO graph_snapshots(project_id,snapshot_id,commit_sha,snapshot_kind,status,created_at) "
                     "VALUES (?,?,?,'scope','superseded','2020')",
                     [(PID, f"inventory-{i:03d}", "fixture") for i in range(112)])
    for index in range(109):
        completed = index < 90
        history = "雪" * (380_000 if index in (0, 93, 100, 108) else 70_000 if index < 30 or not completed else 2)
        if index in (0, 93, 100, 108):
            payload = json.dumps({"runtime_guide": {"next_legal_action": None if completed else False},
                "history": [{"step": item, "notes": "雪" * 48, "details": ["audit", item]} for item in range(6000)],
                "serialized": [json.dumps({"audit": {"late_snapshot_id": "scope-durable" if completed else "scope-current"}}) for _ in range(4 if index != 108 else 0)],
                "late_snapshot_id": "scope-durable" if completed else "scope-current"}, ensure_ascii=False)
            assert len(payload.encode()) > 1024 * 1024
            assert conn.execute("SELECT count(*) FROM json_tree(?)", (payload,)).fetchone()[0] > 20_001
        else:
            payload = ('{"runtime_guide":{"next_legal_action":' + ('null' if completed else 'false') +
                       '},"history":' + json.dumps(history, ensure_ascii=False) + ',"late":' +
                       ('"scope-durable"' if completed else '{"snapshot_id":"scope\\u002dcurrent"}') + '}')
        conn.execute("UPDATE contract_runtime_executions SET record_json=? WHERE contract_execution_id=?",
                     (payload, f"cex-{index:03d}"))
    conn.commit()
    changes = conn.total_changes
    image = hashlib.sha256(conn.serialize()).hexdigest()
    body_fetches, statements = [], []
    def row_factory(cursor, row):
        body_fetches.extend(v for col, v in zip(cursor.description, row) if col[0] == "record_json" and v is not None)
        return sqlite3.Row(cursor, row)
    conn.row_factory = row_factory
    conn.set_trace_callback(statements.append)
    state = store.snapshot_retention_reference_state(conn, PID)
    conn.set_trace_callback(None)
    assert state["complete"] is True and state["refusal_reasons"] == []
    assert state["census"]["contract_rows"] == {"current": 19, "completed": 90}
    assert "scope-current" in state["current_use"] and "scope-durable" not in state["current_use"]
    assert {"scope-current", "scope-durable"} <= state["durable_references"].keys()
    assert {"scope-current", "scope-durable"} <= state["protected"].keys()
    assert len(conn.execute("SELECT snapshot_id FROM graph_snapshots").fetchall()) == 114
    assert any('_rowid_>64' in q for q in statements) and body_fetches == []
    assert changes == conn.total_changes and image == hashlib.sha256(conn.serialize()).hexdigest()


def test_typed_rework_owned_integer_field_rejects_actual_blob_storage(conn):
    _typed_reference_setup(conn, count=1)
    conn.execute("INSERT INTO reconcile_run_metrics(project_id,run_id,snapshot_id,status,created_at,node_count) "
                 "VALUES (?, 'typed-opaque-count', 'scope-current', 'completed', '2020', ?)",
                 (PID, sqlite3.Binary(b'{"snapshot_id":"scope-durable"}')))
    conn.commit()
    image = hashlib.sha256(conn.serialize()).hexdigest()
    fetched = []
    def row_factory(cursor, row):
        fetched.extend(v for v in row if isinstance(v, bytes))
        return sqlite3.Row(cursor, row)
    conn.row_factory = row_factory
    state = store.snapshot_retention_reference_state(conn, PID)
    assert not state["complete"] and "reconcile_run_metrics_payload_unreadable" in state["refusal_reasons"]
    assert state["refusal_metadata"][0] == {
        "table": "reconcile_run_metrics", "field": "node_count", "sqlite_storage_type": "blob",
        "storage_byte_length": 31, "utf8_byte_length": None,
        "cause": "unsupported_payload_storage", "complete": False, "body_fetched": False}
    assert not fetched and image == hashlib.sha256(conn.serialize()).hexdigest()


@pytest.mark.parametrize("payload,cause", [
    (sqlite3.Binary(b'{"runtime_guide":{"next_legal_action":null}}'), "nontext"),
    ('{bad', "malformed_json"), ('{}', "missing_runtime_guide"),
    ('{"runtime_guide":{}}', "missing_next_legal_action"),
])
def test_typed_census_unknown_owner_payload_protects_all_retention(conn, payload, cause):
    _typed_reference_setup(conn, count=1)
    conn.execute("UPDATE contract_runtime_executions SET record_json=?", (payload,))
    conn.commit()
    state = store.snapshot_retention_reference_state(conn, PID)
    assert state["complete"] is False
    assert "contract_runtime_executions_owner_" + cause in state["refusal_reasons"]
    assert state["refusal_metadata"][0]["body_fetched"] is False
    selected = store.select_snapshot_retention_candidates(conn, PID, keep_last_n=0, extra_bundle_snapshot_ids=set())
    assert not selected["reference_authority_complete"] and selected["candidates"] == []


@pytest.mark.parametrize("fault", ["gap", "mutation"])
def test_typed_census_page_gap_or_same_connection_mutation_refuses(fault):
    class FaultConnection(sqlite3.Connection):
        armed = False
        fired = False
        def execute(self, sql, args=()):
            cursor = super().execute(sql, args)
            if self.armed and not self.fired and 'FROM "contract_runtime_executions"' in sql and 'ORDER BY _rowid_' in sql:
                self.fired = True
                if fault == "mutation":
                    super().execute("UPDATE contract_runtime_executions SET updated_at='changed' WHERE contract_execution_id='cex-000'")
                else:
                    class GapCursor:
                        def fetchall(inner):
                            return cursor.fetchall()[1:]
                    return GapCursor()
            return cursor
    connection = sqlite3.connect(":memory:", factory=FaultConnection)
    connection.row_factory = sqlite3.Row
    _typed_reference_setup(connection)
    connection.armed = True
    state = store.snapshot_retention_reference_state(connection, PID)
    assert state["complete"] is False and connection.fired
    assert any("continuation_gap" in r if fault == "gap" else "input_changed" in r for r in state["refusal_reasons"])
    assert connection.execute("SELECT updated_at FROM contract_runtime_executions WHERE contract_execution_id='cex-000'").fetchone()[0] == "now"
    connection.close()


def test_typed_census_external_committed_change_refuses_after_read_snapshot(tmp_path):
    path = tmp_path / "isolated-census.db"
    class Reader(sqlite3.Connection):
        armed = False
        def execute(self, sql, args=()):
            cursor = super().execute(sql, args)
            if self.armed and 'FROM "contract_runtime_executions"' in sql and 'ORDER BY _rowid_' in sql:
                self.armed = False
                with sqlite3.connect(path) as writer:
                    writer.execute("UPDATE contract_runtime_executions SET updated_at='new-watermark'")
            return cursor
    reader = sqlite3.connect(path, factory=Reader)
    reader.row_factory = sqlite3.Row
    reader.execute("PRAGMA journal_mode=WAL")
    _typed_reference_setup(reader)
    reader.armed = True
    state = store.snapshot_retention_reference_state(reader, PID)
    assert not state["complete"] and any("input_changed" in r for r in state["refusal_reasons"])
    reader.close()


def test_typed_census_aggregate_unknown_id_overflow_never_becomes_negative_pins(conn):
    _typed_reference_setup(conn, count=1)
    conn.execute("CREATE TABLE unknown_audit (payload_json TEXT)")
    payload = json.dumps({"snapshot_ids": [f"outside-{i}" for i in range(2001)]})
    assert len(payload.encode()) < 65536
    conn.execute("INSERT INTO unknown_audit VALUES (?)", (payload,))
    conn.commit()
    state = store.snapshot_retention_reference_state(conn, PID)
    assert not state["complete"] and not state["census"]["aggregate_bounded"]
    assert "durable_reference_pin_inventory_unbounded" in state["refusal_reasons"]
    assert state["protected"] == state["current_use"] == state["durable_references"] == {}
    selection = store.select_snapshot_retention_candidates(conn, PID, keep_last_n=0, extra_bundle_snapshot_ids=set())
    assert not selection["reference_authority_complete"] and selection["candidates"] == []


def test_typed_census_unknown_store_and_plain_oversized_text_remain_protective(conn):
    _typed_reference_setup(conn, count=1)
    conn.execute("CREATE TABLE future_qa_session (payload INTEGER)")
    conn.execute("INSERT INTO future_qa_session VALUES (7)")
    conn.execute("CREATE TABLE ordinary_audit (notes TEXT)")
    conn.execute("INSERT INTO ordinary_audit VALUES (?)", ("plain text " * 10_000,))
    conn.commit()
    state = store.snapshot_retention_reference_state(conn, PID)
    assert not state["complete"]
    assert "future_qa_session_owner_schema_unknown" in state["refusal_reasons"]
    assert "ordinary_audit_payload_unreadable" in state["refusal_reasons"]


def test_write_companion_files_enospc_raises_actionable_error(conn, tmp_path, monkeypatch):
    """ENOSPC during write_companion_files must raise an actionable OSError."""
    import errno as _errno

    original_write = store.Path.write_bytes

    call_count = {"n": 0}

    def raise_enospc(self, data):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError(_errno.ENOSPC, "No space left on device", str(self))
        return original_write(self, data)

    monkeypatch.setattr(store.Path, "write_bytes", raise_enospc)

    with pytest.raises(OSError) as exc_info:
        store.write_companion_files(PID, "scope-enospc-test", graph_json={"nodes": []})

    msg = str(exc_info.value)
    assert "graph-snapshots" in msg or "GC" in msg or "dimension" in msg or "stale" in msg.lower(), (
        f"ENOSPC error message must name the retention tool, got: {msg}"
    )
    assert exc_info.value.errno == _errno.ENOSPC


def test_bundle_referenced_snapshot_ids_returns_set(bundle_namespace):
    """The success API remains a set in an entirely isolated namespace."""
    result = store._bundle_referenced_snapshot_ids()
    assert isinstance(result, set)


def _small_graph(node_id="L7.1"):
    return {
        "version": 1,
        "deps_graph": {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": [
                {
                    "id": node_id,
                    "layer": "L7",
                    "title": "Imported Node",
                    "primary": ["agent/governance/imported.py"],
                    "metadata": {"kind": "imported"},
                }
            ],
            "edges": [
                {
                    "source": node_id,
                    "target": "L7.2",
                    "edge_type": "depends_on",
                    "direction": "dependency",
                }
            ],
        },
    }


def test_import_existing_graph_skips_empty_baseline_and_uses_shared_current(conn, tmp_path):
    _ensure_schema(conn)
    create_baseline(
        conn,
        PID,
        chain_version="scan-only",
        trigger="reconcile-task",
        triggered_by="auto-chain",
        graph_json={},
    )
    conn.execute(
        """
        INSERT INTO project_version(project_id, chain_version, updated_at, updated_by, git_head)
        VALUES (?, ?, ?, ?, ?)
        """,
        (PID, "governed-commit", "2026-05-07T00:00:00Z", "test", "newer-mf-head"),
    )
    graph_path = tmp_path / PID / "graph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph_path.write_text(json.dumps(_small_graph("L7.imported")), encoding="utf-8")

    result = store.import_existing_graph_snapshot(
        conn,
        PID,
        snapshot_id="imported-governed-test",
        activate=True,
        created_by="test",
    )

    assert result["source"]["source_kind"] == "shared_volume_current"
    assert result["commit_sha"] == "governed-commit"
    assert result["index_counts"] == {"nodes": 1, "edges": 1}
    assert result["activation"]["snapshot_id"] == "imported-governed-test"

    active = store.get_active_graph_snapshot(conn, PID)
    assert active["snapshot_id"] == "imported-governed-test"
    assert active["commit_sha"] == "governed-commit"

    node = conn.execute(
        "SELECT node_id FROM graph_nodes_index WHERE project_id=? AND snapshot_id=?",
        (PID, "imported-governed-test"),
    ).fetchone()
    assert node["node_id"] == "L7.imported"


def test_import_existing_graph_prefers_non_empty_baseline_companion(conn):
    _ensure_schema(conn)
    create_baseline(
        conn,
        PID,
        chain_version="baseline-commit",
        trigger="reconcile-task",
        triggered_by="auto-chain",
        graph_json=_small_graph("L7.baseline"),
    )

    source = store.select_existing_graph_source(conn, PID)
    assert source["source_kind"] == "baseline_companion"
    assert source["source_ref"] == "1"
    assert source["stats"] == {"nodes": 1, "edges": 1}


def test_strict_graph_ready_ignores_scan_baseline_when_active_graph_is_stale(conn):
    _ensure_schema(conn)
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="imported-active-old",
        commit_sha="old-graph",
        snapshot_kind="imported",
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    create_baseline(
        conn,
        PID,
        chain_version="new-scan",
        trigger="reconcile-task",
        triggered_by="auto-chain",
        scope_kind="commit_sweep",
        scope_value="old-graph..new-scan",
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="new-scan",
        parent_commit_sha="old-graph",
        evidence={"source": "test"},
    )

    status = store.graph_governance_status(conn, PID)
    assert status["materialized_graph_baseline_commit"] == "old-graph"
    assert status["scan_baseline_commit"] == "new-scan"
    assert status["pending_scope_reconcile_count"] == 1

    readiness = store.strict_graph_ready(conn, PID, target_commit="new-scan")
    assert readiness["ok"] is False
    assert readiness["reason"] == "graph_snapshot_commit_mismatch"
    assert readiness["scan_baseline_commit"] == "new-scan"

    ready = store.strict_graph_ready(conn, PID, target_commit="old-graph")
    assert ready["ok"] is True
    assert ready["reason"] == ""


def test_graph_status_surfaces_snapshot_materialization_warnings(conn):
    _ensure_schema(conn)
    notes = {
        "checkout_provenance": {
            "execution_root": "/private/tmp/aming-claw-scope/repo",
            "execution_root_role": "execution_root",
            "execution_root_is_ephemeral": True,
            "canonical_project_identity": {
                "type": "git",
                "project_id": PID,
                "identity_hash": "abc123",
            },
            "warnings": [
                {
                    "code": "ephemeral_execution_root",
                    "message": "graph snapshot was materialized from a temporary execution root",
                }
            ],
        }
    }
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-suspect-root",
        commit_sha="head",
        snapshot_kind="scope",
        notes=json.dumps(notes, sort_keys=True),
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])

    status = store.graph_governance_status(conn, PID)

    assert status["active_snapshot_materialization"]["execution_root_role"] == "execution_root"
    assert status["active_snapshot_materialization"]["warning_count"] == 1
    assert status["active_snapshot_warnings"][0]["code"] == "ephemeral_execution_root"


def test_pending_scope_force_requeue_reopens_materialized_rows(conn):
    _ensure_schema(conn)
    first = store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="head",
        parent_commit_sha="old",
        status=store.PENDING_STATUS_MATERIALIZED,
        snapshot_id="scope-old",
        evidence={"source": "test"},
    )
    assert first["status"] == store.PENDING_STATUS_MATERIALIZED

    preserved = store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="head",
        status=store.PENDING_STATUS_QUEUED,
        evidence={"source": "normal_requeue"},
    )
    assert preserved["status"] == store.PENDING_STATUS_MATERIALIZED
    assert preserved["snapshot_id"] == "scope-old"

    reopened = store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="head",
        status=store.PENDING_STATUS_QUEUED,
        evidence={"source": "suspect_snapshot_requeue"},
        force_requeue=True,
    )

    assert reopened["status"] == store.PENDING_STATUS_QUEUED
    assert reopened["snapshot_id"] == ""
    assert reopened["retry_count"] == 1
    evidence = json.loads(reopened["evidence_json"])
    assert evidence["source"] == "suspect_snapshot_requeue"
    assert evidence["force_requeue"] is True


def test_current_full_state_projects_later_canonical_commit_after_merge(conn):
    _ensure_schema(conn)
    historical_merge_commit = "a" * 40
    current_canonical_commit = "b" * 40
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-current-canonical-after-merge",
        commit_sha=current_canonical_commit,
        snapshot_kind="full",
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    store.record_current_full_reconcile_provenance(
        conn,
        project_id=PID,
        snapshot_id=snapshot["snapshot_id"],
        target_commit_sha=current_canonical_commit,
        request_id="req-current-canonical-after-merge",
        request_started_at="2026-07-22T10:00:00Z",
        route_evidence={
            "schema_version": (
                "graph_current_full_reconcile.route_evidence.v1"
            ),
            "authenticated_role": "observer",
            "authentication_source": "test_protected_entrypoint",
            "raw_route_token_persisted": False,
            "protected_action": "graph_current_full_reconcile",
        },
        reconcile_event_id=12,
        reconcile_event_created_at="2026-07-22T10:02:00Z",
        marker_created_at="2026-07-22T10:02:01Z",
    )

    state = store.current_full_reconcile_state(
        conn,
        PID,
        historical_merge_commit,
        current_canonical_commit_sha=current_canonical_commit,
        merge_event_id=11,
        merge_event_created_at="2026-07-22T10:01:00Z",
        reconcile_event_id=12,
        reconcile_event_created_at="2026-07-22T10:02:00Z",
    )

    assert state["db_verified"] is True
    assert state["merged_commit_sha"] == historical_merge_commit
    assert state["current_canonical_commit_sha"] == current_canonical_commit
    assert state["reconciled_commit_sha"] == current_canonical_commit
    assert state["active_snapshot_commit"] == current_canonical_commit
    assert state["active_snapshot_verified"] is True
    assert state["reconcile_snapshot_verified"] is True
    assert state["durable_order_verified"] is True

    later_canonical_commit = "d" * 40
    later_snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-later-canonical-after-reconcile",
        commit_sha=later_canonical_commit,
        snapshot_kind="full",
    )
    store.activate_graph_snapshot(conn, PID, later_snapshot["snapshot_id"])

    after_later_activation = store.current_full_reconcile_state(
        conn,
        PID,
        historical_merge_commit,
        current_canonical_commit_sha=later_canonical_commit,
        merge_event_id=11,
        merge_event_created_at="2026-07-22T10:01:00Z",
        reconcile_event_id=12,
        reconcile_event_created_at="2026-07-22T10:02:00Z",
    )

    assert after_later_activation["db_verified"] is True
    assert (
        after_later_activation["merged_commit_sha"]
        == historical_merge_commit
    )
    assert (
        after_later_activation["reconciled_commit_sha"]
        == current_canonical_commit
    )
    assert after_later_activation["current_canonical_commit_sha"] == (
        later_canonical_commit
    )
    assert (
        after_later_activation["reconcile_snapshot_id"]
        == snapshot["snapshot_id"]
    )
    assert after_later_activation["reconcile_snapshot_status"] == (
        store.SNAPSHOT_STATUS_SUPERSEDED
    )
    assert after_later_activation["reconcile_snapshot_verified"] is True
    assert (
        after_later_activation["active_snapshot_id"]
        == later_snapshot["snapshot_id"]
    )
    # A plain later activation has no current-full provenance of its own.
    assert after_later_activation["active_snapshot_verified"] is False

    historical_target_only = store.current_full_reconcile_state(
        conn,
        PID,
        historical_merge_commit,
        merge_event_id=11,
        merge_event_created_at="2026-07-22T10:01:00Z",
        reconcile_event_id=12,
        reconcile_event_created_at="2026-07-22T10:02:00Z",
    )
    assert historical_target_only["db_verified"] is False
    assert historical_target_only["active_snapshot_verified"] is False

    forged_target = store.current_full_reconcile_state(
        conn,
        PID,
        historical_merge_commit,
        current_canonical_commit_sha="c" * 40,
        merge_event_id=11,
        merge_event_created_at="2026-07-22T10:01:00Z",
        reconcile_event_id=12,
        reconcile_event_created_at="2026-07-22T10:02:00Z",
    )
    assert forged_target["db_verified"] is False
    assert forged_target["provenance_verified"] is False


def test_current_full_proof_leaf_apis_are_physical_read_only(conn):
    _ensure_schema(conn)
    store.ensure_schema(conn)
    conn.commit()
    before_changes = conn.total_changes
    before_schema = [
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "ORDER BY type, name"
        ).fetchall()
    ]

    candidate = store.current_full_candidate_tuple_from_db(
        conn,
        project_id=PID,
        run_id="missing-candidate-run",
        target_commit_sha="a" * 40,
        snapshot_id="missing-candidate-snapshot",
    )
    active = store.current_full_active_terminal_tuple(
        conn,
        project_id=PID,
        run_id="missing-active-run",
        target_commit_sha="a" * 40,
        expected_scope={},
        snapshot_id="missing-active-snapshot",
    )

    assert candidate["valid"] is False
    assert "candidate_build_claim_missing" in candidate["errors"]
    assert active["valid"] is False
    assert "terminal_snapshot_missing" in active["errors"]
    assert conn.in_transaction is False
    assert conn.total_changes == before_changes
    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "ORDER BY type, name"
        ).fetchall()
    ] == before_schema


def test_current_full_proof_leaf_apis_preserve_valid_and_malformed_file_db_bytes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path)
    db_path = tmp_path / "proof-leaf-read-only.sqlite"
    setup = sqlite3.connect(db_path)
    setup.row_factory = sqlite3.Row
    _ensure_schema(setup)
    store.ensure_schema(setup)
    setup.commit()

    def ready_candidate(suffix):
        owner = _claim_owner(f"proof-{suffix}")
        run_id = f"proof-run-{suffix}"
        snapshot_id = f"proof-snapshot-{suffix}"
        claim = store.acquire_current_full_build_claim(
            setup,
            PID,
            run_id=run_id,
            snapshot_id=snapshot_id,
            commit_sha="9" * 40,
            metric_evidence={"idempotency_scope": {}},
            **owner,
        )
        store.create_graph_snapshot(
            setup,
            PID,
            snapshot_id=snapshot_id,
            commit_sha="9" * 40,
            snapshot_kind="full",
            graph_json={"deps_graph": {"nodes": []}},
            notes=json.dumps({"run_id": run_id}),
        )
        setup.commit()
        store.terminalize_current_full_build_claim(
            setup,
            PID,
            claim_id=claim["claim_id"],
            run_id=run_id,
            snapshot_id=snapshot_id,
            commit_sha="9" * 40,
            terminal_status="candidate_ready",
            manager_start_identity=owner["manager_start_identity"],
            metric_evidence={
                "phase": "candidate_ready",
                "claim_id": claim["claim_id"],
                "idempotency_scope": {},
            },
        )
        return run_id, snapshot_id

    candidate_run, candidate_snapshot = ready_candidate("candidate")
    active_run, active_snapshot = ready_candidate("active")
    setup.execute("BEGIN IMMEDIATE")
    store.activate_graph_snapshot(
        setup,
        PID,
        active_snapshot,
        auto_rebuild_projection=False,
        schema_ready=True,
        post_commit_hooks=False,
    )
    store.record_reconcile_run_metric(
        setup,
        PID,
        run_id=active_run,
        snapshot_id=active_snapshot,
        commit_sha="9" * 40,
        snapshot_kind="full",
        strategy="current_full_reconcile",
        graph_delta_mode="full_rebuild",
        status="complete",
        evidence={
            "phase": "atomic_finalize_complete",
            "activate_requested": True,
            "idempotency_scope": {},
            "request_id": "proof-active-request",
            "reconcile_event_id": 0,
            "provenance_id": "",
        },
        schema_ready=True,
    )
    setup.commit()
    setup.close()

    class CountingConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.commit_calls = 0
            self.rollback_calls = 0

        def commit(self):
            self.commit_calls += 1
            return super().commit()

        def rollback(self):
            self.rollback_calls += 1
            return super().rollback()

    conn = sqlite3.connect(db_path, factory=CountingConnection)
    conn.row_factory = sqlite3.Row
    before_bytes = db_path.read_bytes()
    before_hash = hashlib.sha256(before_bytes).hexdigest()
    before_size = db_path.stat().st_size
    before_changes = conn.total_changes
    before_commits = conn.commit_calls
    before_rollbacks = conn.rollback_calls
    before_pragmas = tuple(
        conn.execute(f"PRAGMA {name}").fetchone()[0]
        for name in ("schema_version", "page_count", "freelist_count")
    )
    before_schema = [
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "ORDER BY type, name"
        ).fetchall()
    ]
    statements = []
    conn.set_trace_callback(statements.append)

    candidate = store.current_full_candidate_tuple_from_db(
        conn,
        project_id=PID,
        run_id=candidate_run,
        target_commit_sha="9" * 40,
        snapshot_id=candidate_snapshot,
    )
    candidate_snapshot_row = dict(
        conn.execute(
            "SELECT * FROM graph_snapshots WHERE project_id=? AND snapshot_id=?",
            (PID, candidate_snapshot),
        ).fetchone()
    )
    candidate_snapshot_row["notes_payload"] = json.loads(
        candidate_snapshot_row["notes"]
    )
    candidate_metric_row = dict(
        conn.execute(
            "SELECT * FROM reconcile_run_metrics WHERE project_id=? "
            "AND run_id=? AND snapshot_id=?",
            (PID, candidate_run, candidate_snapshot),
        ).fetchone()
    )
    candidate_malformed = store.current_full_candidate_resume_tuple(
        conn,
        project_id=PID,
        run_id=candidate_run,
        target_commit_sha="9" * 40,
        snapshot={**candidate_snapshot_row, "status": "forged"},
        request_metric=candidate_metric_row,
    )
    active = store.current_full_active_terminal_tuple(
        conn,
        project_id=PID,
        run_id=active_run,
        target_commit_sha="9" * 40,
        expected_scope={},
        snapshot_id=active_snapshot,
    )
    active_malformed = store.current_full_active_terminal_tuple(
        conn,
        project_id=PID,
        run_id=active_run,
        target_commit_sha="9" * 40,
        expected_scope={"task_id": "wrong"},
        snapshot_id=active_snapshot,
    )
    conn.set_trace_callback(None)

    assert candidate["valid"] is True
    assert candidate_malformed["valid"] is False
    assert "snapshot_not_candidate" in candidate_malformed["errors"]
    assert active["valid"] is True
    assert active_malformed["valid"] is False
    assert "terminal_metric_scope_mismatch" in active_malformed["errors"]
    assert statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert conn.total_changes == before_changes
    assert conn.commit_calls == before_commits
    assert conn.rollback_calls == before_rollbacks
    assert tuple(
        conn.execute(f"PRAGMA {name}").fetchone()[0]
        for name in ("schema_version", "page_count", "freelist_count")
    ) == before_pragmas
    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "ORDER BY type, name"
        ).fetchall()
    ] == before_schema
    conn.close()
    after_bytes = db_path.read_bytes()
    assert db_path.stat().st_size == before_size
    assert hashlib.sha256(after_bytes).hexdigest() == before_hash
    assert after_bytes == before_bytes


def test_b1am_migration_helper_is_explicit_and_clean_transaction(conn):
    _ensure_schema(conn)
    store.ensure_schema(conn)
    conn.commit()

    receipt = store.ensure_reconcile_metric_physical_identity_migration(conn)

    assert receipt == {
        "schema_version": "graph_reconcile_metric_physical_identity.v1",
        "migration_complete": True,
        "backfilled": 0,
        "writes_performed": True,
    }
    assert conn.in_transaction is False
    assert store.ensure_reconcile_metric_physical_identity_migration(conn)[
        "writes_performed"
    ] is False


def _b1am_legacy_metrics(connection, count=3):
    connection.execute(
        "CREATE TABLE reconcile_run_metrics (project_id TEXT NOT NULL, run_id TEXT NOT NULL, "
        "snapshot_id TEXT NOT NULL, commit_sha TEXT NOT NULL DEFAULT '', "
        "parent_commit_sha TEXT NOT NULL DEFAULT '', snapshot_kind TEXT NOT NULL DEFAULT '', "
        "strategy TEXT NOT NULL DEFAULT '', graph_delta_mode TEXT NOT NULL DEFAULT '', "
        "status TEXT NOT NULL DEFAULT '', changed_file_count INTEGER NOT NULL DEFAULT 0, "
        "impacted_file_count INTEGER NOT NULL DEFAULT 0, event_count INTEGER NOT NULL DEFAULT 0, "
        "node_count INTEGER NOT NULL DEFAULT 0, edge_count INTEGER NOT NULL DEFAULT 0, "
        "elapsed_ms INTEGER NOT NULL DEFAULT 0, trace_summary_path TEXT NOT NULL DEFAULT '', "
        "fallback_reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, "
        "evidence_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(project_id,run_id,snapshot_id))"
    )
    connection.executemany(
        "INSERT INTO reconcile_run_metrics (project_id,run_id,snapshot_id,status,created_at) "
        "VALUES (?,?,?,?,?)",
        [
            ("legacy-project", f"legacy-run-{index}", f"legacy-snapshot-{index}",
             "running", f"2026-08-10T00:00:{index:02d}Z")
            for index in range(count)
        ],
    )
    connection.commit()


def test_b1am_schema_only_crash_reopens_and_backfills_once(tmp_path):
    db_path = tmp_path / "b1am-schema-crash.sqlite3"
    setup = sqlite3.connect(db_path)
    _b1am_legacy_metrics(setup)
    setup.executescript(store.GRAPH_SNAPSHOT_SCHEMA_SQL)
    assert setup.execute(
        "SELECT COUNT(*) FROM graph_reconcile_metric_physical_identities"
    ).fetchone()[0] == 0
    assert setup.execute(
        "SELECT COUNT(*) FROM graph_reconcile_metric_identity_schema_state"
    ).fetchone()[0] == 0
    setup.close()

    recovered = sqlite3.connect(db_path)
    recovered.row_factory = sqlite3.Row
    store.ensure_schema(recovered)
    store.ensure_reconcile_metric_physical_identity_migration(recovered)
    identities = [dict(row) for row in recovered.execute(
        "SELECT * FROM graph_reconcile_metric_physical_identities ORDER BY identity_sequence"
    )]
    assert [row["metric_rowid"] for row in identities] == [1, 2, 3]
    assert recovered.execute(
        "SELECT marker FROM graph_reconcile_metric_identity_schema_state"
    ).fetchone()[0] == "physical_identity_backfill_v1"
    before_changes = recovered.total_changes
    store.ensure_schema(recovered)
    store.ensure_reconcile_metric_physical_identity_migration(recovered)
    assert recovered.total_changes == before_changes
    assert [dict(row) for row in recovered.execute(
        "SELECT * FROM graph_reconcile_metric_physical_identities ORDER BY identity_sequence"
    )] == identities
    recovered.close()


def test_b1am_partial_backfill_heals_only_missing_in_rowid_order(conn):
    conn.close()
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _b1am_legacy_metrics(connection)
    connection.executescript(store.GRAPH_SNAPSHOT_SCHEMA_SQL)
    connection.execute(
        "INSERT INTO graph_reconcile_metric_physical_identities "
        "(project_id,run_id,snapshot_id,metric_rowid) "
        "VALUES ('legacy-project','legacy-run-1','legacy-snapshot-1',2)"
    )
    connection.commit()

    receipt = store.ensure_reconcile_metric_physical_identity_migration(connection)

    rows = [dict(row) for row in connection.execute(
        "SELECT * FROM graph_reconcile_metric_physical_identities ORDER BY identity_sequence"
    )]
    assert receipt["backfilled"] == 2
    assert [(row["run_id"], row["metric_rowid"]) for row in rows] == [
        ("legacy-run-1", 2), ("legacy-run-0", 1), ("legacy-run-2", 3)
    ]
    assert connection.in_transaction is False
    connection.close()


def test_b1am_direct_marker_cannot_forge_completion_before_backfill(conn):
    conn.close()
    connection = sqlite3.connect(":memory:")
    _b1am_legacy_metrics(connection, count=1)
    connection.executescript(store.GRAPH_SNAPSHOT_SCHEMA_SQL)
    with pytest.raises(sqlite3.IntegrityError, match="marker_incomplete"):
        connection.execute(
            "INSERT INTO graph_reconcile_metric_identity_schema_state(marker) "
            "VALUES ('physical_identity_backfill_v1')"
        )
    connection.rollback()
    receipt = store.ensure_reconcile_metric_physical_identity_migration(connection)
    assert receipt["backfilled"] == 1
    assert connection.execute(
        "SELECT metric_rowid FROM graph_reconcile_metric_physical_identities"
    ).fetchone()[0] == 1
    connection.close()


def test_b1am_same_key_wrong_rowid_identity_does_not_satisfy_marker(conn):
    conn.close()
    connection = sqlite3.connect(":memory:")
    _b1am_legacy_metrics(connection, count=1)
    connection.executescript(store.GRAPH_SNAPSHOT_SCHEMA_SQL)
    connection.execute(
        "INSERT INTO graph_reconcile_metric_physical_identities "
        "(project_id,run_id,snapshot_id,metric_rowid) "
        "VALUES ('legacy-project','legacy-run-0','legacy-snapshot-0',999)"
    )
    connection.commit()
    receipt = store.ensure_reconcile_metric_physical_identity_migration(connection)
    assert receipt["backfilled"] == 1
    assert [row[0] for row in connection.execute(
        "SELECT metric_rowid FROM graph_reconcile_metric_physical_identities "
        "ORDER BY identity_sequence"
    )] == [999, 1]
    connection.close()


def test_b1am_fault_before_marker_rolls_back_then_reopen_heals(tmp_path, monkeypatch):
    db_path = tmp_path / "b1am-fault.sqlite3"
    connection = sqlite3.connect(db_path)
    _b1am_legacy_metrics(connection, count=2)
    connection.executescript(store.GRAPH_SNAPSHOT_SCHEMA_SQL)

    def fail(_connection):
        raise RuntimeError("marker-write-fault")

    monkeypatch.setattr(store, "_reconcile_metric_identity_before_marker_hook", fail)
    with pytest.raises(RuntimeError, match="marker-write-fault"):
        store.ensure_reconcile_metric_physical_identity_migration(connection)
    assert connection.in_transaction is False
    assert connection.execute(
        "SELECT COUNT(*) FROM graph_reconcile_metric_physical_identities"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM graph_reconcile_metric_identity_schema_state"
    ).fetchone()[0] == 0
    connection.close()
    monkeypatch.setattr(
        store, "_reconcile_metric_identity_before_marker_hook", lambda _connection: None
    )
    recovered = sqlite3.connect(db_path)
    store.ensure_schema(recovered)
    receipt = store.ensure_reconcile_metric_physical_identity_migration(recovered)
    assert receipt["backfilled"] == 2
    assert receipt["writes_performed"] is True
    assert recovered.in_transaction is False
    assert recovered.execute(
        "SELECT COUNT(*) FROM graph_reconcile_metric_physical_identities"
    ).fetchone()[0] == 2
    assert recovered.execute(
        "SELECT COUNT(*) FROM graph_reconcile_metric_identity_schema_state"
    ).fetchone()[0] == 1
    recovered.close()


def test_b1am_concurrent_first_ensure_serializes(tmp_path):
    db_path = tmp_path / "b1am-concurrent.sqlite3"
    setup = sqlite3.connect(db_path)
    _b1am_legacy_metrics(setup, count=20)
    setup.close()
    barrier = threading.Barrier(2)

    def migrate():
        connection = sqlite3.connect(db_path, timeout=5)
        barrier.wait(timeout=5)
        store.ensure_schema(connection)
        receipt = store.ensure_reconcile_metric_physical_identity_migration(connection)
        connection.close()
        return receipt

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(migrate) for _ in range(2)]
        receipts = [future.result(timeout=10) for future in futures]
    assert sorted(
        (receipt["writes_performed"], receipt["backfilled"]) for receipt in receipts
    ) == [(False, 0), (True, 20)]
    verify = sqlite3.connect(db_path)
    assert verify.execute(
        "SELECT COUNT(*) FROM graph_reconcile_metric_physical_identities"
    ).fetchone()[0] == 20
    assert verify.execute(
        "SELECT COUNT(*) FROM graph_reconcile_metric_identity_schema_state"
    ).fetchone()[0] == 1
    verify.close()


def test_b1am_identity_and_marker_are_immutable_against_replace_and_upsert(conn):
    _ensure_schema(conn)
    store.ensure_schema(conn)
    store.ensure_reconcile_metric_physical_identity_migration(conn)
    conn.execute(
        "INSERT INTO reconcile_run_metrics (project_id,run_id,snapshot_id,status,created_at) "
        "VALUES (?,?,?,?,?)", (PID, "b1am-run", "b1am-snapshot", "running", "2026-08-10T00:00:00Z")
    )
    identity = dict(conn.execute(
        "SELECT * FROM graph_reconcile_metric_physical_identities"
    ).fetchone())
    for table, where in (
        ("graph_reconcile_metric_physical_identities", "identity_sequence=:identity_sequence"),
        ("graph_reconcile_metric_identity_schema_state", "marker='physical_identity_backfill_v1'"),
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute(f"UPDATE {table} SET marker=marker WHERE {where}" if "schema_state" in table else f"UPDATE {table} SET metric_rowid=metric_rowid WHERE {where}", identity)
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute(f"DELETE FROM {table} WHERE {where}", identity)
    with pytest.raises(sqlite3.IntegrityError, match="identity_conflict"):
        conn.execute(
            "INSERT OR REPLACE INTO graph_reconcile_metric_physical_identities "
            "(identity_sequence,project_id,run_id,snapshot_id,metric_rowid) "
            "VALUES (:identity_sequence,:project_id,:run_id,:snapshot_id,:metric_rowid)", identity,
        )
    with pytest.raises(sqlite3.IntegrityError, match="identity_conflict"):
        conn.execute(
            "INSERT INTO graph_reconcile_metric_physical_identities "
            "(identity_sequence,project_id,run_id,snapshot_id,metric_rowid) "
            "VALUES (:identity_sequence,:project_id,:run_id,:snapshot_id,:metric_rowid) "
            "ON CONFLICT(identity_sequence) DO UPDATE SET metric_rowid=excluded.metric_rowid", identity,
        )
    for prefix in ("INSERT OR REPLACE", "INSERT"):
        suffix = (
            " ON CONFLICT(marker) DO UPDATE SET marker=excluded.marker"
            if prefix == "INSERT" else ""
        )
        with pytest.raises(sqlite3.IntegrityError, match="marker_conflict"):
            conn.execute(
                f"{prefix} INTO graph_reconcile_metric_identity_schema_state(marker) "
                f"VALUES ('physical_identity_backfill_v1'){suffix}"
            )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute(
            "INSERT INTO graph_reconcile_metric_physical_identities "
            "(project_id,run_id,snapshot_id,metric_rowid) VALUES ('p','r','s',0)"
        )


def test_b1am_autoincrement_identity_survives_max_rowid_reuse(conn):
    _ensure_schema(conn)
    store.ensure_schema(conn)
    store.ensure_reconcile_metric_physical_identity_migration(conn)
    values = (PID, "reuse-run", "reuse-snapshot", "running", "2026-08-10T00:00:00Z")
    conn.execute(
        "INSERT INTO reconcile_run_metrics (project_id,run_id,snapshot_id,status,created_at) "
        "VALUES (?,?,?,?,?)", values,
    )
    first_rowid = conn.execute(
        "SELECT rowid FROM reconcile_run_metrics WHERE run_id='reuse-run'"
    ).fetchone()[0]
    first_identity = conn.execute(
        "SELECT MAX(identity_sequence) FROM graph_reconcile_metric_physical_identities"
    ).fetchone()[0]
    conn.execute("DELETE FROM reconcile_run_metrics WHERE run_id='reuse-run'")
    conn.execute(
        "INSERT INTO reconcile_run_metrics (project_id,run_id,snapshot_id,status,created_at) "
        "VALUES (?,?,?,?,?)", values,
    )
    assert conn.execute(
        "SELECT rowid FROM reconcile_run_metrics WHERE run_id='reuse-run'"
    ).fetchone()[0] == first_rowid
    identities = conn.execute(
        "SELECT identity_sequence FROM graph_reconcile_metric_physical_identities "
        "WHERE run_id='reuse-run' ORDER BY identity_sequence"
    ).fetchall()
    assert [row[0] for row in identities] == [first_identity, first_identity + 1]


def test_b1am_healthy_ensure_is_file_physically_zero_write(tmp_path):
    db_path = tmp_path / "b1am-zero-write.sqlite3"
    setup = _file_connection(db_path)
    setup.execute("PRAGMA journal_mode=WAL")
    store.ensure_reconcile_metric_physical_identity_migration(setup)
    setup.execute(
        "INSERT INTO reconcile_run_metrics (project_id,run_id,snapshot_id,status,created_at) "
        "VALUES ('wal-project','wal-run','wal-snapshot','running','2026-08-10T00:00:00Z')"
    )
    setup.commit()

    class CountingConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.commit_calls = 0
            self.rollback_calls = 0

        def commit(self):
            self.commit_calls += 1
            return super().commit()

        def rollback(self):
            self.rollback_calls += 1
            return super().rollback()

    wal_path = db_path.with_name(db_path.name + "-wal")
    before_bytes = db_path.read_bytes()
    before_wal_bytes = wal_path.read_bytes()
    connection = sqlite3.connect(db_path, factory=CountingConnection)
    connection.row_factory = sqlite3.Row
    before_pragmas = tuple(
        connection.execute(f"PRAGMA {name}").fetchone()[0]
        for name in ("schema_version", "page_count", "freelist_count")
    )
    before_schema = list(connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ))
    statements = []
    connection.set_trace_callback(statements.append)

    store.ensure_schema(connection)
    store.ensure_reconcile_metric_physical_identity_migration(connection)

    connection.set_trace_callback(None)
    assert not any(statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "BEGIN")) for statement in statements)
    assert connection.total_changes == 0
    assert connection.commit_calls == 0
    assert connection.rollback_calls == 0
    assert connection.in_transaction is False
    assert tuple(
        connection.execute(f"PRAGMA {name}").fetchone()[0]
        for name in ("schema_version", "page_count", "freelist_count")
    ) == before_pragmas
    assert list(connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    )) == before_schema
    connection.close()
    assert db_path.read_bytes() == before_bytes
    assert wal_path.read_bytes() == before_wal_bytes
    setup.close()


def test_b1am_healthy_ensure_vm_steps_are_constant_at_50k(conn):
    _ensure_schema(conn)
    store.ensure_schema(conn)
    store.ensure_reconcile_metric_physical_identity_migration(conn)
    conn.executemany(
        "INSERT INTO reconcile_run_metrics (project_id,run_id,snapshot_id,status,created_at) "
        "VALUES (?,?,?,?,?)",
        [(PID, f"scale-run-{index}", f"scale-snapshot-{index}", "running", "2026-08-10T00:00:00Z") for index in range(50_000)],
    )
    conn.commit()
    approximate_steps = 0

    def count_steps():
        nonlocal approximate_steps
        approximate_steps += 100
        return 0

    conn.set_progress_handler(count_steps, 100)
    try:
        store.ensure_schema(conn)
        store.ensure_reconcile_metric_physical_identity_migration(conn)
    finally:
        conn.set_progress_handler(None, 0)
    assert approximate_steps < 5_000
    assert conn.in_transaction is False


def _owner_binding_fixture(connection, payloads, *, degraded="{}"):
    from agent.governance.contracts.runtime import CONTRACT_CHAIN_MAPPING_SCHEMA_SQL
    connection.executescript(CONTRACT_CHAIN_MAPPING_SCHEMA_SQL)
    connection.executemany(
        "INSERT INTO backlog_contract_chain_bindings "
        "(idempotency_key,project_id,backlog_id,contract_chain_id,contract_execution_id,"
        "binding_kind,generation,execution_state_revision,metadata_json,degraded_flags_json,created_at) "
        "VALUES (?,?, 'backlog','chain','execution','fixture',0,0,?,?, 'now')",
        [(f"binding-{i}", PID, payload, degraded) for i, payload in enumerate(payloads)])
    connection.commit()


def test_owner_projection_767_complex_bindings_late_page_pins_without_raw_transfer(conn):
    _typed_reference_setup(conn, count=1)
    marker = "SQL-ONLY-OWNER-BODY-"
    payload = json.dumps({"metadata": {"history": [{"step": i, "notes": marker + "x"*280}
        for i in range(185)]}, "ordinary": {"number": 7, "flag": True, "missing": None}})
    assert 60_000 < len(payload.encode()) < 65_536
    late = json.loads(payload)
    late["raw_path"] = str(store._snapshot_root(PID, "scope-current"))
    late["serialized"] = json.dumps({"snapshot_ids": ["scope-durable"]}).replace(
        "scope-durable", "scope\\u002ddurable")
    late["late_snapshot_id"] = "outside-typed-pin"
    late = json.dumps(late)
    assert "scope-durable" not in late and len(late.encode()) < 65_536
    _owner_binding_fixture(conn, [payload]*766 + [late])
    # A late decoded key and duplicate value must both remain protective.
    conn.execute("UPDATE backlog_contract_chain_bindings SET degraded_flags_json=? WHERE id=767",
                 ('{"snapshot_id":"scope\\u002ddurable","snapshot_id":"scope\\u002dcurrent"}',))
    conn.executemany("INSERT INTO audit_index(event_id,project_id,event,ok,ts) VALUES (?,?, 'fixture',?, 'now')",
                     [(f"audit-{v}", PID, v) for v in (0, 1)])
    conn.commit()
    image = hashlib.sha256(conn.serialize()).hexdigest(); changes = conn.total_changes
    transferred, statements = [], []
    def row_factory(cursor, row):
        # sqlite_master schema SQL is metadata, not a projected payload page.
        if any(col[0].startswith("typeof(") for col in cursor.description):
            transferred.extend(len(v.encode()) for v in row if isinstance(v, str))
        assert not any(marker in v for v in row if isinstance(v, str))
        return sqlite3.Row(cursor, row)
    conn.row_factory = row_factory; conn.set_trace_callback(statements.append)
    state = store.snapshot_retention_reference_state(conn, PID)
    conn.set_trace_callback(None)
    assert state["complete"] is True and state["refusal_reasons"] == []
    assert {"scope-current", "scope-durable", "outside-typed-pin"} <= state["durable_references"].keys()
    assert {"scope-current", "scope-durable", "outside-typed-pin"} <= state["current_use"].keys()
    assert max(transferred) < 1024  # Small pins/metadata, even on the >1MiB raw pages.
    assert any('FROM "backlog_contract_chain_bindings"' in q and '_rowid_>704' in q for q in statements)
    assert changes == conn.total_changes and image == hashlib.sha256(conn.serialize()).hexdigest()


def test_owner_projection_large_markdown_late_raw_and_path_pins(conn):
    _typed_reference_setup(conn, count=1)
    body = "ordinary owner Markdown paragraph\n"*3100 + "late scope-durable " + str(store._snapshot_root(PID, "scope-current"))
    assert len(body.encode()) > 100_406
    conn.execute("DROP TABLE backlog_bugs")
    conn.execute("CREATE TABLE backlog_bugs (project_id TEXT, details_md TEXT)")
    conn.execute("INSERT INTO backlog_bugs VALUES (?,?)", (PID, body)); conn.commit()
    fetched = []
    def row_factory(cursor, row):
        if any(col[0].startswith("typeof(") for col in cursor.description):
            fetched.extend(v for v in row if isinstance(v, str) and len(v.encode()) > 1024)
        return sqlite3.Row(cursor, row)
    conn.row_factory = row_factory
    state = store.snapshot_retention_reference_state(conn, PID)
    assert state["complete"] and not fetched
    assert {"scope-current", "scope-durable"} <= state["current_use"].keys()


@pytest.mark.parametrize("table,column,value", [
    ("audit_index", "ok", 2), ("audit_index", "ok", -1),
    ("audit_index", "ok", 0.5), ("audit_index", "ok", "false"),
    ("audit_index", "ok", sqlite3.Binary(b"1")), ("audit_index", "ok", None),
    ("backlog_contract_chain_bindings", "id", 0),
    ("backlog_contract_chain_bindings", "id", -1),
    ("backlog_contract_chain_bindings", "generation", -1),
    ("backlog_contract_chain_bindings", "execution_state_revision", -1),
    ("backlog_contract_chain_bindings", "generation", 1.5),
    ("backlog_contract_chain_bindings", "id", sqlite3.Binary(b"7")),
    ("ordinary_owner", "ok", 1),
])
def test_owner_projection_scalar_storage_and_domains_protect(conn, table, column, value):
    _typed_reference_setup(conn, count=1)
    conn.execute(f'DROP TABLE IF EXISTS "{table}"')
    conn.execute(f'CREATE TABLE "{table}" (project_id TEXT, "{column}" INTEGER)')
    conn.execute(f'INSERT INTO "{table}" VALUES (?,?)', (PID, value)); conn.commit()
    state = store.snapshot_retention_reference_state(conn, PID)
    assert not state["complete"] and f"{table}_payload_unreadable" in state["refusal_reasons"]
    selected = store.select_snapshot_retention_candidates(conn, PID, keep_last_n=0, extra_bundle_snapshot_ids=set())
    assert not selected["reference_authority_complete"] and selected["candidates"] == []


@pytest.mark.parametrize("payload", [
    '{bad', 'null', '[]', 'false', sqlite3.Binary(b'{}'),
    '{"snapshot_id":null}', '{"snapshot_id":7}', '{"snapshot_ids":"scope-current"}',
    '{"snapshot_ids":["scope-current",7]}',
    '{"snapshot_id":"scope-current","snapshot_id":false}',
    '{"serialized":"{bad"}', '{"serialized":"{\\"snapshot_id\\":7}"}',
    '{"text":"scope\\u0000current"}',
])
def test_owner_projection_malformed_json_reference_shapes_protect(conn, payload):
    _typed_reference_setup(conn, count=1)
    _owner_binding_fixture(conn, [payload])
    state = store.snapshot_retention_reference_state(conn, PID)
    assert not state["complete"] and "backlog_contract_chain_bindings_payload_unreadable" in state["refusal_reasons"]
    assert state["refusal_metadata"][0]["body_fetched"] is False
    if isinstance(payload, str) and '"scope-current"' in payload:
        assert "scope-current" in state["durable_references"]  # Positive pins survive unknown completeness.


def test_owner_projection_degraded_flags_object_and_unknown_large_text(conn):
    _typed_reference_setup(conn, count=1)
    _owner_binding_fixture(conn, ['{}'], degraded='{"ordinary":{"flag":false,"count":7}}')
    assert store.snapshot_retention_reference_state(conn, PID)["complete"]
    conn.execute("UPDATE backlog_contract_chain_bindings SET degraded_flags_json='[]'"); conn.commit()
    assert not store.snapshot_retention_reference_state(conn, PID)["complete"]
    conn.execute("UPDATE backlog_contract_chain_bindings SET degraded_flags_json='{}'")
    conn.execute("CREATE TABLE unknown_owner (payload TEXT)")
    conn.execute("INSERT INTO unknown_owner VALUES (?)", ('x'*100_406,)); conn.commit()
    state = store.snapshot_retention_reference_state(conn, PID)
    assert not state["complete"] and "unknown_owner_payload_unreadable" in state["refusal_reasons"]


@pytest.fixture()
def bundle_namespace(tmp_path, monkeypatch):
    """Never read the real installed default or governance namespace."""
    from agent.governance import self_graph_bundle_check
    default = tmp_path / "installed-default.json"
    default.write_text(json.dumps({"snapshot_id": "sealed-default"}), encoding="utf-8")
    monkeypatch.setattr(self_graph_bundle_check, "SELF_GRAPH_BUNDLE_MANIFEST_REL_PATH", default)
    monkeypatch.setattr(db, "_governance_root", lambda: tmp_path)
    monkeypatch.setattr(store.time, "monotonic", lambda: 0.0)
    return tmp_path, default


def _bundle_manifest(root, directory, sid="sealed-historic", body=None):
    path = root / directory / "self-graph-bundle-manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body if body is not None else json.dumps({"snapshot_id": sid}).encode())
    return path


def _bundle_selection(conn, monkeypatch):
    _ensure_schema(conn)
    store.ensure_schema(conn)
    for sid in ("sealed-default", "sealed-historic", "explicit-extra", "unreferenced"):
        store.create_graph_snapshot(conn, PID, snapshot_id=sid, commit_sha=sid,
                                    snapshot_kind="scope")
    conn.execute("UPDATE graph_snapshots SET status='superseded' WHERE project_id=?", (PID,))
    monkeypatch.setattr(store, "snapshot_retention_reference_state", lambda *_: {
        "protected": {}, "complete": True, "refusal_reasons": []})
    return lambda: store.select_snapshot_retention_candidates(
        conn, PID, keep_last_n=0, extra_bundle_snapshot_ids={"explicit-extra"},
        measure_sizes=False)


def _assert_bundle_incomplete(selection, positives=("sealed-default",)):
    assert selection["reference_authority_complete"] is False
    assert selection["candidates"] == []
    assert "bundle_manifest_reference_unreadable" in selection["global_refusal_reasons"]
    protected = {row["snapshot_id"]: row["reasons"] for row in selection["protected"]}
    assert all("reference_authority_incomplete" in reasons for reasons in protected.values())
    for sid in (*positives, "explicit-extra"):
        assert "bundle_manifest_reference" in protected[sid]


def _bundle_long_stream(monkeypatch, root, late, clock=None):
    actual = os.scandir
    root_ino = root.stat().st_ino
    info = late.stat()

    class Unrelated:
        name = "unrelated.json"
        def stat(self, *, follow_symlinks):
            assert follow_symlinks is False
            return info

    class Stream:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def __iter__(self):
            return self
        def __next__(self):
            return next(self.items)
        def __init__(self):
            def items():
                for _ in range(100001):
                    yield Unrelated()
                if clock is not None:
                    clock[0] = 11.0
                with actual(root) as iterator:
                    for entry in iterator:
                        if entry.name == late.name:
                            yield entry
            self.items = items()

    monkeypatch.setattr(store.os, "scandir", lambda fd:
                        Stream() if isinstance(fd, int) and os.fstat(fd).st_ino == root_ino else actual(fd))


def test_bundle_streaming_large_unrelated_namespace_late_historical_pin(
        conn, bundle_namespace, monkeypatch):
    root, _ = bundle_namespace
    selection = _bundle_selection(conn, monkeypatch)
    # Arbitrary CL feature paths must remain within Rule4 discovery.
    late = _bundle_manifest(root, "charting-loop/trace/feature-inputs/portable-export")
    # Real recursive CL discovery is checked before controlled wide enumeration.
    assert store._bundle_referenced_snapshot_ids() == {"sealed-default", "sealed-historic"}
    root_late = _bundle_manifest(root, "", "sealed-historic")
    _bundle_long_stream(monkeypatch, root, root_late)
    assert store._bundle_referenced_snapshot_ids() == {"sealed-default", "sealed-historic"}
    result = selection()
    assert result["reference_authority_complete"] is True
    protected = {row["snapshot_id"]: row["reasons"] for row in result["protected"]}
    for sid in ("sealed-default", "sealed-historic", "explicit-extra"):
        assert protected[sid] == ["bundle_manifest_reference"]
    assert [row["snapshot_id"] for row in result["candidates"]] == ["unreferenced"]
    assert late.exists()  # Discovery never changes portable historical exports.


def test_bundle_streaming_deadline_preserves_default_and_extra(
        conn, bundle_namespace, monkeypatch):
    root, _ = bundle_namespace
    selection = _bundle_selection(conn, monkeypatch)
    late = _bundle_manifest(root, "")
    clock = [0.0]
    monkeypatch.setattr(store.time, "monotonic", lambda: clock[0])
    _bundle_long_stream(monkeypatch, root, late, clock)
    with pytest.raises(store._BundleInventoryIncomplete) as error:
        store._bundle_referenced_snapshot_ids()
    assert error.value.cause == "deadline_exceeded"
    assert error.value.entries_seen == 100001
    assert error.value.referenced_snapshot_ids == {"sealed-default"}
    clock[0] = 0.0
    _assert_bundle_incomplete(selection())


@pytest.mark.parametrize("failure", ["missing", "malformed", "utf8", "nonobject",
                                    "recursion", "symlink", "nonregular", "unreadable"])
def test_bundle_streaming_default_failures_refuse_all(
        conn, bundle_namespace, monkeypatch, failure):
    root, default = bundle_namespace
    selection = _bundle_selection(conn, monkeypatch)
    if failure == "missing":
        default.unlink()
    elif failure == "malformed":
        default.write_bytes(b"{")
    elif failure == "utf8":
        default.write_bytes(b"\xff")
    elif failure == "nonobject":
        default.write_bytes(b"[]")
    elif failure == "recursion":
        # Parser recursion limits differ across Python implementations. Exercise
        # the recognized parser failure deterministically without changing limits.
        original_loads = store.json.loads
        def recursive_parser(value, *args, **kwargs):
            if value == default.read_text():
                raise RecursionError("isolated parser recursion")
            return original_loads(value, *args, **kwargs)
        monkeypatch.setattr(store.json, "loads", recursive_parser)
    elif failure == "symlink":
        target = root / "target.json"
        default.rename(target)
        default.symlink_to(target)
    elif failure == "nonregular":
        default.unlink()
        default.mkdir()
    else:
        original = os.open
        def denied(name, *args, **kwargs):
            if name == default.name:
                raise PermissionError("isolated denied read")
            return original(name, *args, **kwargs)
        monkeypatch.setattr(store.os, "open", denied)
    _assert_bundle_incomplete(selection(), positives=())


@pytest.mark.parametrize("failure", ["malformed", "utf8", "file_symlink", "dir_symlink",
                                    "unreadable", "disappeared", "replaced", "growth",
                                    "namespace_changed", "dir_replaced"])
def test_bundle_streaming_historical_failure_carries_prior_pins(
        conn, bundle_namespace, monkeypatch, failure):
    root, _ = bundle_namespace
    selection = _bundle_selection(conn, monkeypatch)
    late = _bundle_manifest(root, "broken")
    if failure in {"malformed", "utf8"}:
        late.write_bytes(b"{" if failure == "malformed" else b"\xff")
    elif failure == "file_symlink":
        late.unlink()
        late.symlink_to(root / "installed-default.json")
    elif failure == "dir_symlink":
        (root / "linked-dir").symlink_to(late.parent, target_is_directory=True)
    elif failure in {"unreadable", "dir_replaced"}:
        original = os.open
        def denied(name, *args, **kwargs):
            if failure == "unreadable" and name == late.name:
                raise PermissionError("isolated denied read")
            if failure == "dir_replaced" and name == late.parent.name:
                late.parent.rename(root / "old-directory")
                late.parent.mkdir()
            return original(name, *args, **kwargs)
        monkeypatch.setattr(store.os, "open", denied)
    elif failure in {"replaced", "growth"}:
        original = os.read
        inode = late.stat().st_ino
        changed = [False]
        def raced(fd, size):
            data = original(fd, size)
            if os.fstat(fd).st_ino == inode and not changed[0]:
                changed[0] = True
                if failure == "growth":
                    with late.open("ab") as writer:
                        writer.write(b" ")
                else:
                    replacement = late.with_suffix(".replacement")
                    replacement.write_bytes(late.read_bytes())
                    replacement.replace(late)
            return data
        monkeypatch.setattr(store.os, "read", raced)
    else:
        original = os.scandir
        inode = late.parent.stat().st_ino
        class Mutating:
            def __enter__(self):
                self.iterator = original(late.parent)
                return self
            def __exit__(self, *_):
                self.iterator.close()
            def __iter__(self):
                return self
            def __next__(self):
                child = next(self.iterator, None)
                if child is None:
                    (late.parent / "new-entry").write_bytes(b"unrelated")
                    raise StopIteration
                if failure == "disappeared" and child.name == late.name:
                    # Cache the observed entry, then remove before the anchored read.
                    child.stat(follow_symlinks=False)
                    late.unlink()
                return child
        monkeypatch.setattr(store.os, "scandir", lambda fd:
                            Mutating() if isinstance(fd, int) and os.fstat(fd).st_ino == inode else original(fd))
    _assert_bundle_incomplete(selection())


@pytest.mark.parametrize("budget,cause", [
    ("count", "manifest_count_exceeded"),
    ("file", "manifest_file_bytes_exceeded"),
    ("total", "manifest_total_bytes_exceeded"),
    ("pin", "pin_bytes_exceeded"),
    ("depth", "directory_resources_exceeded"),
    ("handles", "directory_resources_exceeded"),
])
def test_bundle_streaming_resource_boundaries_are_not_truncation(
        conn, bundle_namespace, monkeypatch, budget, cause):
    root, default = bundle_namespace
    if budget == "count":
        monkeypatch.setattr(store, "_BUNDLE_MANIFEST_COUNT", 2)
        _bundle_manifest(root, "a")
    elif budget == "file":
        size = default.stat().st_size
        monkeypatch.setattr(store, "_BUNDLE_FILE_BYTES", size)
    elif budget == "total":
        _bundle_manifest(root, "a")
        monkeypatch.setattr(store, "_BUNDLE_TOTAL_BYTES", default.stat().st_size +
                            (root / "a/self-graph-bundle-manifest.json").stat().st_size)
    elif budget == "pin":
        monkeypatch.setattr(store, "_BUNDLE_PIN_BYTES", len("sealed-default"))
    elif budget == "depth":
        monkeypatch.setattr(store, "_BUNDLE_DIRECTORY_DEPTH", 2)
        (root / "a").mkdir()
    else:
        monkeypatch.setattr(store, "_BUNDLE_DIRECTORY_HANDLES", 4)
        (root / "a").mkdir()
    assert "sealed-default" in store._bundle_referenced_snapshot_ids()
    if budget in {"count", "total"}:
        _bundle_manifest(root, "b", "another-sealed")
    elif budget == "file":
        _bundle_manifest(root, "a", body=default.read_bytes() + b" ")
    elif budget == "pin":
        _bundle_manifest(root, "a", "x" * (len("sealed-default") + 1))
    else:
        (root / "a/b").mkdir()
    with pytest.raises(store._BundleInventoryIncomplete) as error:
        store._bundle_referenced_snapshot_ids()
    assert error.value.cause == cause
    assert "sealed-default" in error.value.referenced_snapshot_ids
    assert len(str(error.value)) < 200
    selection = _bundle_selection(conn, monkeypatch)
    _assert_bundle_incomplete(selection())


def test_bundle_streaming_exact_manifest_count_and_utf8_pin_boundary(bundle_namespace):
    root, default = bundle_namespace
    for index in range(127):
        _bundle_manifest(root, str(index), str(index))
    result = store._bundle_referenced_snapshot_ids()
    assert len(result) == 128
    _bundle_manifest(root, "overflow")
    with pytest.raises(store._BundleInventoryIncomplete) as error:
        store._bundle_referenced_snapshot_ids()
    assert error.value.cause == "manifest_count_exceeded"
    assert len(error.value.referenced_snapshot_ids) == 128
    # Exact UTF8 byte boundary, not character count or project-owner filtering.
    for path in root.glob("*/self-graph-bundle-manifest.json"):
        path.unlink()
    default.write_text(json.dumps({"snapshot_id": "é" * 2048}), encoding="utf-8")
    assert store._bundle_referenced_snapshot_ids() == {"é" * 2048}
    default.write_text(json.dumps({"snapshot_id": "é" * 2049}), encoding="utf-8")
    with pytest.raises(store._BundleInventoryIncomplete, match="pin_bytes_exceeded"):
        store._bundle_referenced_snapshot_ids()


@pytest.mark.parametrize("value,expected", [(42, {"42"}), ("  historical  ", {"historical"}),
                                           (False, set()), (None, set())])
def test_bundle_streaming_original_snapshot_id_conversion(bundle_namespace, value, expected):
    _, default = bundle_namespace
    default.write_text(json.dumps({"snapshot_id": value}))
    assert store._bundle_referenced_snapshot_ids() == expected


def test_bundle_streaming_partial_historical_positive_survives_later_bad_manifest(
        conn, bundle_namespace, monkeypatch):
    root, _ = bundle_namespace
    selection = _bundle_selection(conn, monkeypatch)
    _bundle_manifest(root, "", "sealed-historic")
    _bundle_manifest(root, "broken", body=b"{")
    original = os.scandir
    root_ino = root.stat().st_ino
    class Ordered:
        def __enter__(self):
            with original(root) as iterator:
                children = sorted(iterator, key=lambda child: child.name !=
                                  "self-graph-bundle-manifest.json")
            self.iterator = iter(children)
            return self
        def __exit__(self, *_):
            pass
        def __iter__(self):
            return self
        def __next__(self):
            return next(self.iterator)
    monkeypatch.setattr(store.os, "scandir", lambda fd:
                        Ordered() if isinstance(fd, int) and os.fstat(fd).st_ino == root_ino
                        else original(fd))
    _assert_bundle_incomplete(selection(), positives=("sealed-default", "sealed-historic"))


def test_bundle_streaming_exact_file_and_total_byte_boundaries(bundle_namespace):
    root, default = bundle_namespace
    prefix = b'{"snapshot_id":"sealed-default","padding":"'
    suffix = b'"}'
    content = prefix + b"x" * (1048576 - len(prefix) - len(suffix)) + suffix
    default.write_bytes(content)
    # One MiB per file and sixteen MiB in aggregate, including default.
    for index in range(15):
        _bundle_manifest(root, str(index), body=content)
    assert store._bundle_referenced_snapshot_ids() == {"sealed-default"}
    _bundle_manifest(root, "total-overflow", body=b"{}")
    with pytest.raises(store._BundleInventoryIncomplete, match="manifest_total_bytes_exceeded"):
        store._bundle_referenced_snapshot_ids()
    # Isolate the per-file ceiling from aggregate accounting.
    for path in root.glob("*/self-graph-bundle-manifest.json"):
        path.unlink()
    default.write_bytes(content + b" ")
    with pytest.raises(store._BundleInventoryIncomplete, match="manifest_file_bytes_exceeded"):
        store._bundle_referenced_snapshot_ids()


def test_bundle_streaming_optional_absent_namespace_preserves_default(
        bundle_namespace, monkeypatch):
    root, _ = bundle_namespace
    monkeypatch.setattr(db, "_governance_root", lambda: root / "absent")
    assert store._bundle_referenced_snapshot_ids() == {"sealed-default"}


@pytest.mark.parametrize("failure", ["symlink", "replacement"])
def test_bundle_streaming_default_parent_is_anchored_no_follow(
        conn, bundle_namespace, monkeypatch, failure):
    from agent.governance import self_graph_bundle_check
    root, _ = bundle_namespace
    selection = _bundle_selection(conn, monkeypatch)
    parent = root / "package-resources"
    parent.mkdir()
    default = parent / "default.json"
    default.write_text(json.dumps({"snapshot_id": "sealed-default"}))
    monkeypatch.setattr(self_graph_bundle_check, "SELF_GRAPH_BUNDLE_MANIFEST_REL_PATH", default)
    if failure == "symlink":
        parent.rename(root / "real-resources")
        parent.symlink_to(root / "real-resources", target_is_directory=True)
    else:
        original = os.open
        def replace_parent(name, *args, **kwargs):
            if name == parent:
                parent.rename(root / "old-resources")
                parent.mkdir()
                (parent / default.name).write_text(json.dumps({"snapshot_id": "replacement"}))
            return original(name, *args, **kwargs)
        monkeypatch.setattr(store.os, "open", replace_parent)
    _assert_bundle_incomplete(selection(), positives=())


@pytest.mark.parametrize("owner", ["contract", "audit"])
def test_reference_projection_large_valid_token_inventory_completes_bounded_vm(conn, owner):
    ids = [f"snapshot-{i:03}" for i in range(100)]
    tokens = [(sid, token) for sid in ids for token in
              (sid, f"run-{sid}", f"/private/graph/{sid}")]
    nested = json.dumps({"snapshot_ids": [ids[-1]], "late_snapshot_id": "external-pin"})
    nested = nested.replace(ids[-1], ids[-1].replace("-", "\\u002d"))
    body = json.dumps({"history": [{"n": i, "note": "ordinary owner text", "flag": True}
                        for i in range(6000)], "serialized": nested,
                       "raw_path": "/private/graph/snapshot-098",
                       "runtime_guide": {"next_legal_action": None}})
    assert conn.execute("SELECT count(*) FROM json_tree(?)", (body,)).fetchone()[0] > 20_000
    conn.execute("CREATE TABLE projection_fixture(contract_execution_id TEXT,record_json TEXT)")
    conn.execute("INSERT INTO projection_fixture VALUES ('execution',?)", (body,))
    if owner == "contract":
        projection = store._contract_reference_projection(tokens)
    else:
        projection = ",".join(store._owner_reference_projection("record_json", tokens, json_owner=True))
    steps = 0
    started = store.time.monotonic()
    def budget():
        nonlocal steps
        steps += 1000
        return int(steps >= 30_000_000 or store.time.monotonic() - started >= 5)
    conn.set_progress_handler(budget, 1000)
    try:
        row = conn.execute("SELECT " + projection + " FROM projection_fixture").fetchone()
    finally:
        conn.set_progress_handler(None, 0)
    assert steps < 30_000_000
    if owner == "contract":
        assert tuple(row[:4]) == ("execution", "text", len(body.encode()), "completed")
        pins, complete = row[4:]
        assert json.loads(pins) == ids[-2:]
    else:
        pins, complete = row
        assert set(json.loads(pins)) == {ids[-2], ids[-1], "external-pin"}
    assert complete == 1
    assert len(pins.encode()) < 1024


def test_owner_projection_parent_identity_stays_with_its_serialized_document(conn):
    first = json.dumps({"snapshot_ids": ["external-one", 7]})
    second = json.dumps({"ordinary_ids": ["ordinary-not-a-pin", False]})
    body = ('{"first":' + json.dumps(first) + ',"second":' + json.dumps(second) +
            ',"snapshot_id":"external-root","snapshot_id":"external-last"}')
    conn.execute("CREATE TABLE projection_fixture(body TEXT)")
    conn.execute("INSERT INTO projection_fixture VALUES (?)", (body,))
    projection = ",".join(store._owner_reference_projection(
        "body", [("known-absent", "known-absent")], json_owner=True))
    row = conn.execute("SELECT " + projection + " FROM projection_fixture").fetchone()
    assert set(json.loads(row[0])) == {"external-one", "external-root", "external-last"}
    assert row[1] == 0  # A nontext direct child refuses completeness.


def test_contract_reference_projection_empty_inventory_preserves_completion(conn):
    body = json.dumps({"runtime_guide": {"next_legal_action": None}})
    conn.execute("CREATE TABLE projection_fixture(contract_execution_id TEXT,record_json TEXT)")
    conn.execute("INSERT INTO projection_fixture VALUES ('execution',?)", (body,))
    row = conn.execute("SELECT " + store._contract_reference_projection([]) +
                       " FROM projection_fixture").fetchone()
    assert tuple(row) == ("execution", "text", len(body.encode()), "completed", "[]", 1)


@pytest.mark.parametrize("body,tokens,contract_pins,owner_pins,completion", [
    ('{"runtime_guide":{"next_legal_action":null},"note":"ordinary 雪"}',
     [("absent", "not-present")], [], [], (1, 1)),
    ('{"runtime_guide":{"next_legal_action":false},"key-hit":"value-hit"}',
     [("z-first", "value-hit"), ("a-second", "key-hit"), ("z-first", "key-hit")],
     ["z-first", "a-second"], ["a-second", "z-first"], (1, 1)),
    ('{"runtime_guide":{"next_legal_action":null},"note":"scope\\u002dcurrent"}',
     [("decoded", "scope-current"), ("raw", "\\u002d")], ["decoded"],
     ["decoded", "raw"], (1, 1)),
    ('{"runtime_guide":{"next_legal_action":null},"serialized":'
     '"{\\"scope\\u002dcurrent\\":\\"audit\\",\\"snapshot_ids\\":[\\"outside-pin\\"]}"}',
     [("current", "scope-current")], ["current"], ["current", "outside-pin"], (1, 1)),
    ('{"runtime_guide":{"next_legal_action":null},"snapshot_id":"outside-first",'
     '"snapshot_id":"outside-last","note":"scope-current\\u0000ordinary"}',
     [("current", "scope-current")], ["current"],
     ["current", "outside-first", "outside-last"], (0, 0)),
    ('{"runtime_guide":{"next_legal_action":null},"serialized":"{bad",'
     '"note":"scope-current"}', [("current", "scope-current")], ["current"],
     ["current"], (1, 0)),
    ('{"runtime_guide":{"next_legal_action":null},"snapshot_ids":["outside-pin"]}',
     [], [], ["outside-pin"], (1, 1)),
    ('{bad', [("absent", "absent")], None, None, (0, 0)),
    ('7', [("absent", "absent")], [], None, (1, 0)),
    (sqlite3.Binary(b'{}'), [("absent", "absent")], None, None, (0, 0)),
])
def test_structured_reference_matcher_preserves_pins_and_completion(
        conn, body, tokens, contract_pins, owner_pins, completion):
    conn.execute("CREATE TABLE matcher_fixture(contract_execution_id TEXT,record_json TEXT)")
    conn.execute("INSERT INTO matcher_fixture VALUES ('execution',?)", (body,))
    contract = conn.execute("SELECT " + store._contract_reference_projection(tokens) +
                            " FROM matcher_fixture").fetchone()
    owner = conn.execute("SELECT " + ",".join(store._owner_reference_projection(
        "record_json", tokens, json_owner=True)) + " FROM matcher_fixture").fetchone()
    assert (json.loads(contract[4]) if contract[4] is not None else None) == contract_pins
    assert (json.loads(owner[0]) if owner[0] is not None else None) == owner_pins
    assert (contract[5], owner[1]) == completion
    if isinstance(body, str) and body.startswith('{"runtime_guide"'):
        assert contract[3] == ("live" if '"next_legal_action":false' in body else "completed")


@pytest.mark.parametrize("density", ["nohit", "sparse", "dense"])
def test_structured_reference_matcher_sparse_and_dense_atom_inventory(conn, density):
    ids = [f"snapshot-{i:03}" for i in range(114)]
    expected = [] if density == "nohit" else ids[-1:] if density == "sparse" else ids
    body = json.dumps({"runtime_guide": {"next_legal_action": None},
        "audit": {str(i): "ordinary text " * 22 for i in range(500)},
        "pins": " ".join(expected)})
    conn.execute("CREATE TABLE matcher_fixture(contract_execution_id TEXT,record_json TEXT)")
    conn.execute("INSERT INTO matcher_fixture VALUES ('execution',?)", (body,))
    tokens = [(sid, token) for sid in ids for token in (sid, "/private/fixture/" + sid)]
    contract = conn.execute("SELECT " + store._contract_reference_projection(tokens) +
                            " FROM matcher_fixture").fetchone()
    owner = conn.execute("SELECT " + ",".join(store._owner_reference_projection(
        "record_json", tokens, json_owner=True)) + " FROM matcher_fixture").fetchone()
    assert json.loads(contract[4]) == json.loads(owner[0]) == expected
    assert contract[5] == owner[1] == 1


def test_structured_reference_matcher_unicode_owner_byte_cap(conn):
    ids = ["雪" * 330 + f"-{i:03}" for i in range(70)]
    assert all(len(sid.encode()) <= 1024 for sid in ids)
    body = json.dumps({"pins": ids}, ensure_ascii=False)
    conn.execute("CREATE TABLE matcher_fixture(body TEXT)")
    conn.execute("INSERT INTO matcher_fixture VALUES (?)", (body,))
    row = conn.execute("SELECT " + ",".join(store._owner_reference_projection(
        "body", [(sid, sid) for sid in ids], json_owner=True)) +
        " FROM matcher_fixture").fetchone()
    assert row[0] is None  # Character count cannot weaken the UTF-8 projection cap.
    assert row[1] == 1


def test_structured_reference_matcher_high_row_store_still_refuses_selection(conn, bundle_namespace):
    _typed_reference_setup(conn, count=0)
    conn.execute("DROP TABLE graph_asset_projection")
    conn.execute("CREATE TABLE graph_asset_projection(project_id TEXT,payload TEXT)")
    conn.executemany("INSERT INTO graph_asset_projection VALUES (?, '{}')", [(PID,)] * 2183)
    conn.commit()
    state = store.snapshot_retention_reference_state(conn, PID)
    assert not state["complete"]
    assert any("reference_census_window_unbounded:graph_asset_projection" in reason
               for reason in state["refusal_reasons"])
    selected = store.select_snapshot_retention_candidates(conn, PID, keep_last_n=0,
        extra_bundle_snapshot_ids=set(), measure_sizes=False)
    assert not selected["reference_authority_complete"] and selected["candidates"] == []


@pytest.mark.parametrize("json_owner,body,pins", [
    (True, '{"ordinary":"audit"}', []),
    (False, "ordinary owner Markdown paragraph", []),
    (True, '{"snapshot_id":"external-pin"}', ["external-pin"]),
])
def test_owner_projection_empty_inventory_preserves_completion(conn, json_owner, body, pins):
    conn.execute("CREATE TABLE projection_fixture(body TEXT)")
    conn.execute("INSERT INTO projection_fixture VALUES (?)", (body,))
    projection = ",".join(store._owner_reference_projection("body", [], json_owner=json_owner))
    row = conn.execute("SELECT " + projection + " FROM projection_fixture").fetchone()
    assert json.loads(row[0]) == pins
    assert row[1] == 1


@pytest.mark.parametrize("json_owner", [True, False])
@pytest.mark.parametrize("known_inventory", [False, True])
def test_owner_census_known_empty_and_nonempty_inventory(conn, json_owner, known_inventory):
    store.ensure_schema(conn)
    conn.execute("CREATE TABLE graph_query_traces "
                 "(project_id TEXT, snapshot_id TEXT, canonical_base_snapshot_id TEXT)")
    if known_inventory:
        conn.execute("INSERT INTO graph_snapshots "
                     "(project_id,snapshot_id,commit_sha,snapshot_kind,status,created_at) "
                     "VALUES (?, 'scope-known', 'fixture', 'scope', 'superseded', '2020')", (PID,))
    if json_owner:
        _owner_binding_fixture(conn, ['{"ordinary":"audit scope-known"}'])
    else:
        conn.execute("CREATE TABLE backlog_bugs (project_id TEXT, details_md TEXT)")
        conn.execute("INSERT INTO backlog_bugs VALUES (?, 'ordinary audit scope-known')", (PID,))
    conn.commit()
    known_ids = {row[0] for row in conn.execute(
        "SELECT snapshot_id FROM graph_snapshots WHERE project_id=?", (PID,))}
    tokens = store._snapshot_reference_tokens(conn, PID, known_ids)
    assert bool(tokens) is known_inventory
    image = hashlib.sha256(conn.serialize()).hexdigest()
    changes = conn.total_changes
    state = store.snapshot_retention_reference_state(conn, PID)
    assert state["complete"] is True and state["refusal_reasons"] == []
    expected = {"scope-known"} if known_inventory else set()
    assert set(state["protected"]) == set(state["current_use"]) == set(state["durable_references"]) == expected
    assert changes == conn.total_changes
    assert image == hashlib.sha256(conn.serialize()).hexdigest()


@pytest.mark.parametrize("body,pins,complete", [
    ("ordinary Markdown z-pin a-pin", ["a-pin", "z-pin"], 1),
    ("ordinary Markdown without any reference", [], 1),
    ("plain \\u0073napshot a-pin", ["a-pin"], 0),
    ('{"snapshot_ids":["external"],"serialized":"{\\"snapshot_id\\":\\"a-pin\\"}"}',
     ["a-pin", "external"], 1),
    ('"a-pin"', ["a-pin"], 1),
    ('{bad a-pin', ["a-pin"], 1),
    (sqlite3.Binary(b"ordinary a-pin"), None, 0),
    ("ordinary\x00a-pin", None, 0),
])
def test_markdown_reference_projection_preserves_plain_and_structured_outputs(conn, body, pins, complete):
    conn.execute("CREATE TABLE markdown_projection(body)")
    conn.execute("INSERT INTO markdown_projection VALUES (?)", (body,))
    # Deliberately unsorted aliases verify stable DISTINCT pin ordering.
    tokens = [("z-pin", "z-pin"), ("a-pin", "a-pin"), ("a-pin", "alias")]
    projection = ",".join(store._owner_reference_projection("body", tokens, json_owner=False))
    result = conn.execute("SELECT " + projection + " FROM markdown_projection").fetchone()
    assert (json.loads(result[0]) if result[0] is not None else None) == pins
    assert result[1] == complete


@pytest.mark.parametrize("tokens", [[], [("a-pin", "a-pin")]])
def test_markdown_reference_projection_empty_and_whitespace_are_complete(conn, tokens):
    conn.execute("CREATE TABLE markdown_projection(body TEXT)")
    conn.executemany("INSERT INTO markdown_projection VALUES (?)", [("",), ("  \n ",)])
    projection = ",".join(store._owner_reference_projection("body", tokens, json_owner=False))
    assert [tuple(row) for row in conn.execute("SELECT " + projection + " FROM markdown_projection")] == [
        ("[]", 1), ("[]", 1)]


def test_markdown_reference_projection_materializes_one_bounded_token_pass(conn):
    tokens = [(f"snapshot-{i:03}", f"snapshot-{i:03}") for i in range(200)]
    body = "Ordinary owner paragraph. " * 1200 + "snapshot-099"
    conn.execute("CREATE TABLE markdown_projection(body TEXT)")
    conn.executemany("INSERT INTO markdown_projection VALUES (?)", [(body,)] * 96)
    projection = ",".join(store._owner_reference_projection("body", tokens, json_owner=False))
    steps = 0
    started = time.monotonic()
    def budget():
        nonlocal steps
        steps += 1000
        return int(steps >= 250_000 or time.monotonic() - started >= 5)
    conn.set_progress_handler(budget, 1000)
    try:
        rows = conn.execute("SELECT " + projection + " FROM markdown_projection").fetchall()
    finally:
        conn.set_progress_handler(None, 0)
    assert steps < 250_000
    assert [tuple(row) for row in rows] == [('["snapshot-099"]', 1)] * 96


def test_markdown_retention_selection_keeps_typed_current_durable_and_bundle_pins(conn, bundle_namespace):
    _typed_reference_setup(conn, count=2)
    conn.execute("UPDATE contract_runtime_executions SET record_json=? WHERE contract_execution_id='cex-000'",
                 (json.dumps({"runtime_guide": {"next_legal_action": None}, "snapshot_id": "scope-durable"}),))
    conn.execute("UPDATE contract_runtime_executions SET record_json=? WHERE contract_execution_id='cex-001'",
                 (json.dumps({"runtime_guide": {"next_legal_action": False}, "snapshot_id": "scope-current"}),))
    conn.executemany("INSERT INTO backlog_bugs(bug_id,details_md,created_at,updated_at) "
                     "VALUES (?,?,'now','now')", [
        ("markdown-owner", "Ordinary Markdown " * 1200 + "scope-durable"),
        ("serialized-owner", json.dumps({"serialized": json.dumps({"snapshot_ids": ["scope-current"]})})),
    ])
    conn.executemany("INSERT INTO graph_query_traces VALUES (?,?,?)", [(PID, "scope-current", "scope-durable")] * 70)
    conn.commit()
    image = hashlib.sha256(conn.serialize()).hexdigest()
    changes = conn.total_changes
    state = store.snapshot_retention_reference_state(conn, PID)
    selection = store.select_snapshot_retention_candidates(conn, PID, keep_last_n=0, measure_sizes=False)
    assert state["complete"] is True and selection["reference_authority_complete"] is True
    assert state["census"]["contract_rows"] == {"current": 1, "completed": 1}
    assert {"scope-current", "scope-durable"} <= state["current_use"].keys()
    assert {"scope-current", "scope-durable"} <= state["durable_references"].keys()
    selected = {row["snapshot_id"]: row["reasons"] for row in selection["protected"]}
    assert "bundle_manifest_reference" in selected["sealed-default"]
    assert {"scope-current", "scope-durable"} <= selected.keys()
    assert "durable_backlog_bugs_payload_reference" in selected["scope-durable"]
    assert selection["candidates"] == [] and selection["global_refusal_reasons"] == []
    assert changes == conn.total_changes and image == hashlib.sha256(conn.serialize()).hexdigest()


def _complete_owner_fixture(*, factory=sqlite3.Connection):
    connection = sqlite3.connect(':memory:', factory=factory)
    connection.row_factory = sqlite3.Row
    _typed_reference_setup(connection, count=0)
    return connection


def test_owned_census_completes_2183_source_rows_and_generic_owner(conn, bundle_namespace):
    connection = _complete_owner_fixture()
    connection.execute("INSERT INTO reconcile_run_metrics(project_id,run_id,snapshot_id,status,created_at) "
                       "VALUES (?, 'late-run', 'scope-current', 'completed', '2020')", (PID,))
    connection.executemany("INSERT INTO graph_asset_projection(project_id,asset_path,size_bytes,updated_at) "
                           "VALUES (?,?,?,'now')", [(PID, f'ordinary/{i}', i) for i in range(2183)])
    late = json.dumps({'late_snapshot_id': 'outside-late-pin', 'serialized': json.dumps({'snapshot_ids': ['scope-durable']})}).replace(
        'scope-durable', 'scope\\u002ddurable')
    connection.execute("UPDATE graph_asset_projection SET run_id='late-run',asset_path=?,metadata_json=? "
                       "WHERE asset_path='ordinary/2182'", (str(store._snapshot_root(PID, 'scope-current')), late))
    connection.execute('CREATE TABLE ordinary_owner(project_id TEXT,payload TEXT)')
    connection.execute('CREATE INDEX ordinary_scope ON ordinary_owner(project_id,payload)')
    rowids = [-10, 0] + [i * 3 for i in range(1, 2100)]
    connection.executemany('INSERT INTO ordinary_owner(rowid,project_id,payload) VALUES (?,?,?)',
                           [(i, PID, 'scope-current' if i <= 0 else 'ordinary') for i in rowids])
    connection.execute("INSERT INTO ordinary_owner(rowid,project_id,payload) VALUES (1,'foreign','foreign-pin')")
    connection.commit()
    image = hashlib.sha256(connection.serialize()).hexdigest()
    changes = connection.total_changes
    statements = []
    connection.set_trace_callback(statements.append)
    with store._owned_reference_connection(lambda: connection):
        state = store.snapshot_retention_reference_state(connection, PID)
        assert state['complete'], state['refusal_reasons']
        assert {'scope-current', 'scope-durable', 'outside-late-pin'} <= state['protected'].keys()
        assert 'foreign-pin' not in state['protected']
        assert state['census']['max_rows_per_store'] is None
        pages = [q for q in statements if 'FROM "graph_asset_projection" NOT INDEXED' in q]
        assert len(pages) == 36  # 35 nonempty64-row pages (last7), then EOF.
        assert all('LIMIT 64' in q for q in pages)
        assert tuple(r[0] for r in store._reference_pages(connection, 'ordinary_owner', 'payload',
            'project_id=?', (PID,))) == tuple(rowids)
        plan = connection.execute('EXPLAIN QUERY PLAN SELECT _rowid_,payload FROM ordinary_owner '
            'NOT INDEXED WHERE project_id=? AND _rowid_>? ORDER BY _rowid_ LIMIT64'.replace('LIMIT64','LIMIT 64'),
            (PID, 0)).fetchall()
        assert any('INTEGER PRIMARY KEY' in r[3] for r in plan)
        assert not any('TEMP B-TREE' in r[3] for r in plan)
        selected = store.select_snapshot_retention_candidates(connection, PID, keep_last_n=0,
            extra_bundle_snapshot_ids={'scope-current'}, measure_sizes=False)
        assert selected['reference_authority_complete']
        assert selected['candidates'] == []
        assert image == hashlib.sha256(connection.serialize()).hexdigest() and changes == connection.total_changes
        assert not connection.in_transaction
    assert store._reference_budget(connection) is None
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute('SELECT 1')


@pytest.mark.parametrize('value,valid', [(0, True), (7, True), (2**63-1, True),
    (-1, False), ('malformed', False), (sqlite3.Binary(b'payload'), False), (0.5, False), (None, False)])
def test_owned_census_size_actual_scalar_domain(value, valid, bundle_namespace):
    connection = _complete_owner_fixture()
    # Remove NOT NULL to exercise actual NULL storage corruption. Integer
    # affinity remains exact: numeric text stored as INTEGER is an integer.
    connection.execute('DROP TABLE graph_asset_projection')
    connection.execute('CREATE TABLE graph_asset_projection(project_id TEXT,size_bytes INTEGER,payload TEXT)')
    connection.execute('INSERT INTO graph_asset_projection VALUES (?,?,?)', (PID, value, 'scope-current'))
    connection.commit()
    with store._owned_reference_connection(lambda: connection):
        state = store.snapshot_retention_reference_state(connection, PID)
        assert state['complete'] is valid
        assert 'scope-current' in state['protected']
        if not valid:
            assert 'graph_asset_projection_payload_unreadable' in state['refusal_reasons']
            assert store.select_snapshot_retention_candidates(connection, PID, measure_sizes=False)['candidates'] == []


def test_owned_census_small_legacy_outputs_and_borrowed_callback(conn, bundle_namespace):
    _typed_reference_setup(conn, count=1)
    calls = []
    conn.set_progress_handler(lambda: calls.append(1) or 0, 1)
    before = store.snapshot_retention_reference_state(conn, PID)
    count = len(calls)
    assert before['complete']
    selection_before = store.select_snapshot_retention_candidates(conn, PID, measure_sizes=False)
    conn.execute('SELECT sum(value) FROM json_each(?)', ('[1,2,3]',)).fetchone()
    assert len(calls) > count
    connection = _complete_owner_fixture()
    conn.backup(connection)
    with store._owned_reference_connection(lambda: connection):
        after = store.snapshot_retention_reference_state(connection, PID)
        after['census'] = before['census']  # Resource metadata is the sole intentional difference.
        assert after == before
        assert store.select_snapshot_retention_candidates(connection, PID, measure_sizes=False) == selection_before
    conn.execute('CREATE TABLE ordinary_owner(payload TEXT)')
    conn.executemany("INSERT INTO ordinary_owner VALUES ('scope-current')", [()] * 2001)
    conn.commit()
    refused = store.snapshot_retention_reference_state(conn, PID)
    assert not refused['complete'] and any('window_unbounded:ordinary_owner' in r for r in refused['refusal_reasons'])
    count = len(calls)
    conn.execute('SELECT 1').fetchone()
    assert len(calls) > count
    conn.set_progress_handler(None, 0)


@pytest.mark.parametrize('mode', ['sql', 'python'])
def test_owned_census_budget_keeps_prefix_pins_and_restores(mode, monkeypatch, bundle_namespace, caplog):
    caplog.set_level('INFO', logger=store.__name__)
    clock = [0.0]
    monkeypatch.setattr(store.time, 'monotonic', lambda: clock[0])
    sql_interrupts = []
    class Timed(sqlite3.Connection):
        def execute(self, sql, args=()):
            if mode == 'sql' and 'FROM "zz_owner" NOT INDEXED' in sql:
                clock[0] = 26.0  # The actual SQLite progress callback interrupts this query.
            try:
                cursor = super().execute(sql, args)
                if mode == 'sql' and 'FROM "zz_owner" NOT INDEXED' in sql:
                    class ObservedCursor:
                        def fetchall(self):
                            try:
                                return cursor.fetchall()
                            except sqlite3.OperationalError as exc:
                                if 'interrupted' in str(exc):
                                    sql_interrupts.append(str(exc))
                                raise
                    return ObservedCursor()
                return cursor
            except sqlite3.OperationalError as exc:
                if 'interrupted' in str(exc):
                    sql_interrupts.append(str(exc))
                raise
    connection = _complete_owner_fixture(factory=Timed)
    connection.execute('INSERT INTO graph_query_traces VALUES (?,?,?)', (PID, 'scope-current', 'scope-durable'))
    connection.execute('CREATE TABLE zz_owner(project_id TEXT,payload TEXT)')
    connection.executemany('INSERT INTO zz_owner VALUES (?,?)', [(PID, json.dumps({'audit': ['ordinary'] * 600}))] * 64)
    connection.commit()
    connection.execute('PRAGMA busy_timeout=9000')
    if mode == 'python':
        original = store._bounded_decoded_reference_pins
        def decoded(*args, **kwargs):
            clock[0] = 26.0
            return original(*args, **kwargs)
        monkeypatch.setattr(store, '_bounded_decoded_reference_pins', decoded)
    with store._owned_reference_connection(lambda: connection):
        provider = store._reference_budget(connection)
        state = store.snapshot_retention_reference_state(connection, PID)
        assert not state['complete'] and provider.exhausted
        assert bool(sql_interrupts) is (mode == 'sql')
        assert 'reference_census_budget_exhausted' in state['refusal_reasons']
        assert {'scope-current', 'scope-durable'} <= state['protected'].keys()
        assert connection.execute('PRAGMA busy_timeout').fetchone()[0] == 9000
        assert not provider.active and not connection.in_transaction
        diagnostics = [json.loads(r.message.split("reference_census_diagnostic ", 1)[1])
                       for r in caplog.records if "reference_census_diagnostic " in r.message]
        assert len(diagnostics) == 1 and diagnostics[0]["cause"] == "budget_exhausted"
        assert diagnostics[0]["mode"] == mode
        assert diagnostics[0]["phase"] == ("page" if mode == "sql" else "field")
        deadline = provider.deadline
        selected = store.select_snapshot_retention_candidates(connection, PID, measure_sizes=False)
        assert selected['candidates'] == [] and not selected['reference_authority_complete']
        assert provider.deadline == deadline == 25.0
        assert connection.execute('SELECT 1').fetchone()[0] == 1  # Outside census remains usable.


@pytest.mark.parametrize('fault', ['gap', 'mutation', 'schema'])
def test_owned_census_late_failure_keeps_positive_accumulator(fault, bundle_namespace):
    class Fault(sqlite3.Connection):
        armed = False
        def execute(self, sql, args=()):
            cursor = super().execute(sql, args)
            if self.armed and 'FROM "zz_owner" NOT INDEXED' in sql:
                self.armed = False
                if fault == 'mutation':
                    super().execute("UPDATE graph_query_traces SET canonical_base_snapshot_id='changed'")
                elif fault == 'schema':
                    super().execute('CREATE TABLE schema_change(value TEXT)')
                else:
                    class Gap:
                        def fetchall(self):
                            return cursor.fetchall()[1:]
                    return Gap()
            return cursor
    connection = _complete_owner_fixture(factory=Fault)
    connection.execute('INSERT INTO graph_query_traces VALUES (?,?,?)', (PID, 'scope-current', 'scope-durable'))
    connection.execute('CREATE TABLE zz_owner(payload TEXT)')
    connection.execute("INSERT INTO zz_owner VALUES ('ordinary')")
    connection.commit()
    connection.armed = True
    with store._owned_reference_connection(lambda: connection):
        state = store.snapshot_retention_reference_state(connection, PID)
        assert not state['complete']
        assert {'scope-current', 'scope-durable'} <= state['protected'].keys()
        assert any('continuation_gap' in r if fault == 'gap' else 'input_changed' in r for r in state['refusal_reasons'])
        assert not connection.in_transaction


def test_owned_census_does_not_rollback_caller_transaction(bundle_namespace):
    connection = _complete_owner_fixture()
    with store._owned_reference_connection(lambda: connection):
        connection.execute('BEGIN')
        state = store.snapshot_retention_reference_state(connection, PID)
        assert state['complete'] and connection.in_transaction
        connection.rollback()


@pytest.mark.parametrize('phase', ['count', 'json'])
def test_owned_census_sql_count_and_json_interrupt_retains_pins(phase, monkeypatch, bundle_namespace):
    clock = [0.0]
    monkeypatch.setattr(store.time, 'monotonic', lambda: clock[0])
    interrupted = []
    class Timed(sqlite3.Connection):
        def execute(self, sql, args=()):
            target = ('SELECT count(*) FROM "zz_owner"' in sql if phase == 'count' else
                      'FROM "contract_runtime_executions" NOT INDEXED' in sql)
            if target:
                clock[0] = 26.0
            try:
                return super().execute(sql, args)
            except sqlite3.OperationalError as exc:
                if 'interrupted' in str(exc):
                    interrupted.append(str(exc))
                raise
    connection = _complete_owner_fixture(factory=Timed)
    connection.execute('INSERT INTO graph_query_traces VALUES (?,?,?)', (PID, 'scope-current', 'scope-durable'))
    if phase == 'count':
        connection.execute('CREATE TABLE zz_owner(project_id TEXT,payload TEXT)')
        connection.executemany('INSERT INTO zz_owner VALUES (?,?)', [(PID, 'ordinary')] * 5000)
    else:
        payload = json.dumps({'runtime_guide': {'next_legal_action': None}, 'history': ['ordinary'] * 5000})
        connection.execute('INSERT INTO contract_runtime_executions '
            '(contract_execution_id,project_id,backlog_id,contract_id,version,revision,execution_state_revision,record_json,created_at,updated_at) '
            "VALUES ('one',?,'backlog','contract','1','1',1,?,'now','now')", (PID, payload))
    connection.commit()
    with store._owned_reference_connection(lambda: connection):
        state = store.snapshot_retention_reference_state(connection, PID)
        assert interrupted == ['interrupted']
        assert not state['complete'] and 'reference_census_budget_exhausted' in state['refusal_reasons']
        assert {'scope-current', 'scope-durable'} <= state['protected'].keys()
        assert not connection.in_transaction


def test_owned_census_unsupported_cursor_and_concurrent_commit_are_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_governance_root', lambda: tmp_path)
    path = tmp_path / 'owned-census.sqlite3'
    class Concurrent(sqlite3.Connection):
        armed = False
        def execute(self, sql, args=()):
            result = super().execute(sql, args)
            if self.armed and 'FROM "zz_owner" NOT INDEXED' in sql:
                self.armed = False
                with sqlite3.connect(path) as writer:
                    writer.execute("UPDATE zz_owner SET payload='committed'")
            return result
    def factory():
        connection = sqlite3.connect(path, factory=Concurrent)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA journal_mode=WAL')
        _typed_reference_setup(connection, count=0)
        connection.execute('INSERT INTO graph_query_traces VALUES (?,?,?)', (PID, 'scope-current', 'scope-durable'))
        connection.execute('CREATE TABLE zz_owner(payload TEXT)')
        connection.execute("INSERT INTO zz_owner VALUES ('ordinary')")
        connection.commit()
        connection.armed = True
        return connection
    with store._owned_reference_connection(factory) as connection:
        state = store.snapshot_retention_reference_state(connection, PID)
        assert not state['complete'] and any('input_changed' in r for r in state['refusal_reasons'])
        assert {'scope-current', 'scope-durable'} <= state['protected'].keys()
        connection.execute('CREATE TABLE unsupported_owner(payload TEXT PRIMARY KEY) WITHOUT ROWID')
        connection.execute("INSERT INTO unsupported_owner VALUES ('scope-current')")
        connection.commit()
        state = store.snapshot_retention_reference_state(connection, PID)
        assert not state['complete'] and any('cursor_unsupported:unsupported_owner' in r for r in state['refusal_reasons'])
        assert {'scope-current', 'scope-durable'} <= state['protected'].keys()


def test_census_literal_bracket_title_and_nested_audit_text_keep_late_pins(conn, bundle_namespace):
    _typed_reference_setup(conn, count=1)
    evidence = '{ordinary audit scope-current; not serialized JSON}'
    audit = {'audit_archive': {'audit_close_gate': {'evidence': [evidence]},
                              'evidence': {'audit_close_gate': {'evidence': [evidence]}}},
             'late_snapshot_id': 'outside-late-pin',
             'serialized': json.dumps({'note': 'scope-durable'}).replace('scope-', 'scope\\u002d')}
    conn.execute('INSERT INTO backlog_bugs(bug_id,title,takeover_json,details_md,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                 ('literal', '[ordinary title] scope-current', json.dumps(audit), '{ordinary Markdown scope-durable', 'now', 'now'))
    conn.commit()
    before = hashlib.sha256(conn.serialize()).hexdigest()
    state = store.snapshot_retention_reference_state(conn, PID)
    assert state['complete'], state['refusal_reasons']
    assert {'scope-current', 'scope-durable', 'outside-late-pin'} <= state['protected'].keys()
    assert {'scope-current', 'scope-durable', 'outside-late-pin'} <= state['current_use'].keys()
    assert state['protected'] == state['durable_references']
    assert before == hashlib.sha256(conn.serialize()).hexdigest()


@pytest.mark.parametrize('payload', ['{declared broken scope-current', 'not-json scope-current',
    json.dumps({'serialized': '{declared broken scope-current'}),
    json.dumps({'note': '{opaque \\u0073cope-current'}),
    json.dumps({'note': 'scope-current\x00unknown'}), sqlite3.Binary(b'{scope-current}')])
def test_census_declared_json_and_unknown_text_remain_incomplete(payload, conn, bundle_namespace):
    _typed_reference_setup(conn, count=0)
    conn.execute("INSERT INTO backlog_bugs(bug_id,takeover_json,created_at,updated_at) VALUES (?, ?,'now','now')", ('fixture', payload)); conn.commit()
    state = store.snapshot_retention_reference_state(conn, PID)
    assert not state['complete'] and 'backlog_bugs_payload_unreadable' in state['refusal_reasons']
    if isinstance(payload, str) and 'scope-current' in payload:
        assert 'scope-current' in state['protected']


def test_reference_page_exact_body_memo_preserves_physical_rows_and_storage(conn, monkeypatch):
    # An unknown owner may use internal-looking names; no alias may shadow it.
    conn.execute('CREATE TABLE memo_fixture(project_id TEXT,body TEXT COLLATE NOCASE,storage TEXT,'
                 'exact_body TEXT,v0 TEXT,census_cursor TEXT,__ac_reference_cursor TEXT)')
    rowids = [-9, 0] + list(range(3, 78))
    values = ['{"snapshot_id":"upper"}', '{"snapshot_id":"UPPER"}',
              '{"snapshot_ids":["extra"],"note":"known"}', None, sqlite3.Binary(b'known')]
    conn.executemany('INSERT INTO memo_fixture(rowid,project_id,body,storage,exact_body,v0,census_cursor,__ac_reference_cursor) '
                    'VALUES (?,?,?,\'literal\',\'bytes\',\'v\',\'cursor\',\'shadow\')',
                    [(rid, PID, values[i % len(values)]) for i, rid in enumerate(rowids)])
    conn.execute("INSERT INTO memo_fixture(rowid,project_id,body) VALUES (1,'foreign','foreign')");conn.commit()
    image = hashlib.sha256(conn.serialize()).hexdigest()
    with store._owned_reference_connection(lambda: conn):
        with store._reference_budget(conn).census(), store._reference_read_snapshot(conn):
            for tokens in [[], [('known', 'known')]]:
                for mode in [True, False]:
                    expressions = store._owner_reference_projection('body', tokens, json_owner=mode)
                    projection = 'storage,exact_body,v0,census_cursor,__ac_reference_cursor,typeof(body),length(CAST(body AS BLOB)),__reference_memo_0_0__,__reference_memo_0_1__'
                    memo = list(store._reference_pages(conn, 'memo_fixture', projection, 'project_id=?', (PID,), reusable={'body': expressions}))
                    with monkeypatch.context() as m:
                        m.setattr(store, '_reference_memo_page', lambda *args: None)
                        ordinary = list(store._reference_pages(conn, 'memo_fixture', projection, 'project_id=?', (PID,), reusable={'body': expressions}))
                    assert [tuple(r) for r in memo] == [tuple(r) for r in ordinary]
                    assert [r[0] for r in memo] == rowids
                    assert json.loads(memo[0][-2]) == ['upper'] and json.loads(memo[1][-2]) == ['UPPER']
        assert image == hashlib.sha256(conn.serialize()).hexdigest()


def test_reference_page_memo_memory_fallback_is_lossless(conn, monkeypatch):
    conn.execute('CREATE TABLE memo_fixture(body TEXT)')
    conn.executemany('INSERT INTO memo_fixture VALUES (?)', [('same known',)] * 65)
    conn.commit()
    expressions = store._owner_reference_projection('body', [('known', 'known')], json_owner=False)
    projection = '__reference_memo_0_0__,__reference_memo_0_1__'
    cached = [tuple(r) for r in store._reference_pages(conn, 'memo_fixture', projection, reusable={'body': expressions})]
    monkeypatch.setattr(store, '_REFERENCE_MEMO_BYTES', 1)
    ordinary = [tuple(r) for r in store._reference_pages(conn, 'memo_fixture', projection, reusable={'body': expressions})]
    assert ordinary == cached and len(ordinary) == 65


def test_census_diagnostics_bounded_private_and_plan_stable(conn, bundle_namespace, caplog, monkeypatch):
    from agent.governance import stale_artifact_cleanup
    _typed_reference_setup(conn, count=0)
    conn.executemany("INSERT INTO backlog_bugs(bug_id,takeover_json,created_at,updated_at) VALUES (?, ?, 'now','now')",
                     [(str(i), '{private secret-body /private/path authorization-token') for i in range(12)])
    conn.commit()
    image = hashlib.sha256(conn.serialize()).hexdigest()
    first = store.snapshot_retention_reference_state(conn, PID)
    caplog.set_level('INFO', logger=store.__name__)
    with store._owned_reference_connection(lambda: conn, diagnostic_request_id='req-private-diagnostic'):
        second = store.snapshot_retention_reference_state(conn, PID)
        assert first['protected'] == second['protected'] and first['refusal_reasons'] == second['refusal_reasons']
        assert len(caplog.records) == store._REFERENCE_DIAGNOSTICS - 1
        logs = '\n'.join(r.message for r in caplog.records)
        assert 'req-private-diagnostic' in logs and 'bounded_decoder_incomplete' in logs
        assert all(value not in logs for value in ['secret-body', '/private/path', 'authorization-token', 'SELECT '])
        assert image == hashlib.sha256(conn.serialize()).hexdigest()
        # Logging never adds a volatile field to the selection or apply hash.
        caplog.clear()
        with monkeypatch.context() as m:
            m.setattr(store, '_reference_diagnostic', lambda *args, **kwargs: None)
            selection = store.select_snapshot_retention_candidates(conn, PID, keep_last_n=0, measure_sizes=False)
        repeat = store.select_snapshot_retention_candidates(conn, PID, keep_last_n=0, measure_sizes=False)
        assert selection == repeat
        assert stale_artifact_cleanup._plan_hash(PID, 'graph_snapshot', selection['candidates']) == stale_artifact_cleanup._plan_hash(PID, 'graph_snapshot', repeat['candidates'])


def test_census_bundle_diagnostic_preserves_positive_refusal_and_no_paths(conn, bundle_namespace, monkeypatch, caplog):
    _typed_reference_setup(conn, count=0)
    caplog.set_level('INFO', logger=store.__name__)
    def unavailable():
        raise store._BundleInventoryIncomplete('directory_changed', {'scope-durable'}, 118669)
    monkeypatch.setattr(store, '_bundle_referenced_snapshot_ids', unavailable)
    with store._owned_reference_connection(lambda: conn, diagnostic_request_id='/private/request/token'):
        result = store.select_snapshot_retention_candidates(conn, PID, keep_last_n=0, measure_sizes=False)
        assert not result['reference_authority_complete'] and result['candidates'] == []
        assert any(r['snapshot_id'] == 'scope-durable' and 'bundle_manifest_reference' in r['reasons'] for r in result['protected'])
        assert result['global_refusal_reasons'] == ['bundle_manifest_reference_unreadable']
        record = json.loads(caplog.records[-1].message.split('reference_census_diagnostic ', 1)[1])
        assert record == {'cause': 'directory_changed', 'phase': 'bundle', 'entries': 118669,
            'request_id': 'sha256:' + hashlib.sha256(b'/private/request/token').hexdigest()}
        assert '/private/' not in caplog.text


def test_nested_serialized_depth_still_refuses_sql_and_python(conn):
    nested = json.dumps({'snapshot_id': 'scope-current'})
    for _ in range(10):
        nested = json.dumps({'serialized': nested})
    conn.execute('CREATE TABLE nested_fixture(contract_execution_id TEXT,record_json TEXT)')
    conn.execute('INSERT INTO nested_fixture VALUES (?,?)', ('fixture',
        json.dumps({'runtime_guide': {'next_legal_action': False}, 'serialized': nested})))
    contract = conn.execute('SELECT ' + store._contract_reference_projection([('scope-current', 'scope-current')]) + ' FROM nested_fixture').fetchone()
    pins, complete = conn.execute('SELECT ' + ','.join(store._owner_reference_projection(
        'record_json', [('scope-current', 'scope-current')], json_owner=True)) + ' FROM nested_fixture').fetchone()
    assert contract[-1] == complete == 0
    assert json.loads(contract[-2]) == json.loads(pins) == ['scope-current']
    with pytest.raises(ValueError, match='reference_decoding_incomplete'):
        store._bounded_decoded_reference_pins(json.loads(nested), [('scope-current', 'scope-current')])


def test_memoized_contract_page_budget_retains_completed_prefix(monkeypatch, bundle_namespace):
    clock = [0.0]
    monkeypatch.setattr(store.time, 'monotonic', lambda: clock[0])
    class Timed(sqlite3.Connection):
        def execute(self, query, args=()):
            if query.startswith('WITH page_source') and 'contract_runtime_executions' in query and '_rowid_>?' in query:
                clock[0] = 26.0
            return super().execute(query, args)
    connection = _complete_owner_fixture(factory=Timed)
    for i in range(70):
        payload = json.dumps({'runtime_guide': {'next_legal_action': None}, 'note': 'scope-durable'})
        connection.execute('INSERT INTO contract_runtime_executions '
            '(contract_execution_id,project_id,backlog_id,contract_id,version,revision,execution_state_revision,record_json,created_at,updated_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)', (f'execution-{i}', PID, 'backlog', 'contract', '1', '1', 1, payload, 'now', 'now'))
    connection.commit()
    with store._owned_reference_connection(lambda: connection):
        result = store.snapshot_retention_reference_state(connection, PID)
        assert not result['complete'] and store._reference_budget(connection).exhausted
        assert result['census']['contract_rows'] == {'completed': 64, 'current': 0}
        assert 'scope-durable' in result['durable_references']
        assert 'scope-durable' not in result['current_use']
        assert 'reference_census_budget_exhausted' in result['refusal_reasons']
        assert not connection.in_transaction and not store._reference_budget(connection).active


@pytest.mark.parametrize('cap', [1, 8 * 1024 * 1024])
def test_reference_memo_template_preserves_marker_like_tokens_and_columns(conn, monkeypatch, cap):
    conn.execute('CREATE TABLE memo_fixture(body TEXT,"__reference_memo_0_0__" TEXT)')
    conn.execute('INSERT INTO memo_fixture VALUES (?,?)', ('literal __reference_memo_0_1__', 'literal-column'))
    conn.commit()
    monkeypatch.setattr(store, '_REFERENCE_MEMO_BYTES', cap)
    expressions = store._owner_reference_projection('body', [('pin', '__reference_memo_0_1__')], json_owner=False)
    projection = '"__reference_memo_0_0__",__reference_memo_0_0__,__reference_memo_0_1__'
    rows = list(store._reference_pages(conn, 'memo_fixture', projection, reusable={'body': expressions}))
    assert [tuple(r) for r in rows] == [(1, 'literal-column', '["pin"]', 1)]


@pytest.mark.parametrize('suffix', ['cursor', 'body_0', 'storage_0', 'v0_0', 'v0_1'])
@pytest.mark.parametrize('prefix', ['__AC_REFERENCE_', '__Ac_Reference_', '__ac_reference_'])
def test_reference_memo_alias_ascii_casefold_preserves_exact_owner_columns(conn, monkeypatch, suffix, prefix):
    column = prefix + suffix
    # A second case-insensitive collision requires another namespace advance.
    conn.execute(f'CREATE TABLE alias_fixture(project_id TEXT,body TEXT COLLATE NOCASE,"{column}" TEXT,"__AC_REFERENCE__cursor" TEXT)')
    rowids = [-9, 0] + list(range(3, 78))
    conn.executemany(f'INSERT INTO alias_fixture(rowid,project_id,body,"{column}","__AC_REFERENCE__cursor") VALUES (?,?,?,?,?)',
        [(rid, PID, 'known' if i % 2 == 0 else 'KNOWN', 'original-column', 'deep-column') for i, rid in enumerate(rowids)])
    conn.execute(f"INSERT INTO alias_fixture(rowid,project_id,body,\"{column}\") VALUES (1,'foreign','known','foreign-column')")
    conn.commit()
    image = hashlib.sha256(conn.serialize()).hexdigest()
    expressions = store._owner_reference_projection('body', [('known', 'known')], json_owner=False)
    projection = f'"{column}","__AC_REFERENCE__cursor",__reference_memo_0_0__,__reference_memo_0_1__'
    with store._reference_read_snapshot(conn):
        cached = [tuple(r) for r in store._reference_pages(conn, 'alias_fixture', projection, 'project_id=?', (PID,), reusable={'body': expressions})]
        with monkeypatch.context() as m:
            m.setattr(store, '_reference_memo_page', lambda *args: None)
            uncached = [tuple(r) for r in store._reference_pages(conn, 'alias_fixture', projection, 'project_id=?', (PID,), reusable={'body': expressions})]
    assert cached == uncached == [(rid, 'original-column', 'deep-column', '["known"]' if i % 2 == 0 else '[]', 1) for i, rid in enumerate(rowids)]
    assert image == hashlib.sha256(conn.serialize()).hexdigest() and not conn.in_transaction


def _chain_owner_writer_fixture(connection):
    """Actual owner DDL/writers, rather than affinity-only scalar fixtures."""
    from agent.governance.contracts.runtime import SQLiteContractExecutionStore
    _typed_reference_setup(connection, count=0)
    writer = SQLiteContractExecutionStore(connection)
    root = {'contract_execution_id': 'owner-root', 'project_id': PID,
            'backlog_id': 'owner-backlog', 'contract_id': 'ordinary',
            'version': '1', 'revision': '1', 'execution_state_revision': 0,
            'contract_chain_id': 'owner-chain', 'root_contract_execution_id': 'owner-root',
            'runtime_guide': {'next_legal_action': None},
            'snapshot_id': 'scope-durable'}
    writer.create(root)
    writer.create({**root, 'contract_execution_id': 'owner-child',
                   'parent_contract_execution_id': 'owner-root',
                   'execution_state_revision': 2, 'snapshot_id': 'scope-current'})
    connection.commit()


def test_chain_owner_actual_writer_integer_domains_complete(conn):
    _chain_owner_writer_fixture(conn)
    generation, watermark = conn.execute('SELECT generation,projection_watermark '
                                        'FROM backlog_contract_chain_current').fetchone()
    assert type(generation) is int and generation >= 0
    assert watermark == conn.execute('SELECT max(id) FROM backlog_contract_chain_bindings').fetchone()[0]
    assert tuple(conn.execute('SELECT id,generation FROM contract_chain_edges').fetchone()) == (1, 2)
    before = (conn.total_changes, hashlib.sha256(conn.serialize()).hexdigest())
    state = store.snapshot_retention_reference_state(conn, PID)
    assert state['complete'], state['refusal_reasons']
    assert {'scope-current', 'scope-durable'} <= state['durable_references'].keys()
    assert before == (conn.total_changes, hashlib.sha256(conn.serialize()).hexdigest())


@pytest.mark.parametrize('table,column', [
    ('backlog_contract_chain_current', 'generation'),
    ('backlog_contract_chain_current', 'projection_watermark'),
    ('contract_chain_edges', 'id'), ('contract_chain_edges', 'generation'),
])
@pytest.mark.parametrize('invalid', [-1, 1.5, 'opaque', sqlite3.Binary(b'7')])
def test_chain_owner_scalar_invalid_domains_still_fail_closed(conn, table, column, invalid):
    _chain_owner_writer_fixture(conn)
    # PK enforces integer storage; use exact owner column in a corrupted schema
    # to cover storage validation independently of SQLite's PK insert guard.
    if column == 'id':
        conn.execute('DROP TABLE contract_chain_edges')
        conn.execute('CREATE TABLE contract_chain_edges(project_id TEXT,id INTEGER,generation INTEGER,metadata_json TEXT)')
        conn.execute('INSERT INTO contract_chain_edges VALUES (?,1,0,?)',
                     (PID, '{"snapshot_id":"scope-current"}'))
    conn.execute(f'UPDATE "{table}" SET "{column}"=?', (invalid,)); conn.commit()
    before = (conn.total_changes, hashlib.sha256(conn.serialize()).hexdigest())
    state = store.snapshot_retention_reference_state(conn, PID)
    assert not state['complete'] and table + '_payload_unreadable' in state['refusal_reasons']
    assert {'scope-current', 'scope-durable'} <= state['durable_references'].keys()
    assert before == (conn.total_changes, hashlib.sha256(conn.serialize()).hexdigest())


def test_chain_owner_zero_id_and_unknown_integer_not_exempt(conn):
    _chain_owner_writer_fixture(conn)
    conn.execute('UPDATE contract_chain_edges SET id=0')
    conn.execute('ALTER TABLE backlog_contract_chain_current ADD COLUMN unknown_payload INTEGER')
    conn.execute('UPDATE backlog_contract_chain_current SET unknown_payload=7'); conn.commit()
    state = store.snapshot_retention_reference_state(conn, PID)
    assert not state['complete']
    assert {'contract_chain_edges_payload_unreadable', 'backlog_contract_chain_current_payload_unreadable'} <= set(state['refusal_reasons'])


def test_chain_owner_domains_keep_nonmetadata_positive_references(conn):
    _chain_owner_writer_fixture(conn)
    conn.execute('UPDATE contract_chain_edges SET metadata_json=?',
                 ('{"snapshot_id":"extra-edge-pin"}',))
    conn.execute('UPDATE backlog_contract_chain_current SET active_chain_json=?',
                 ('{"snapshot_ids":["extra-current-pin"]}',)); conn.commit()
    state = store.snapshot_retention_reference_state(conn, PID)
    assert state['complete']
    assert {'extra-edge-pin', 'extra-current-pin'} <= state['durable_references'].keys()
    assert {'extra-edge-pin', 'extra-current-pin'} <= state['current_use'].keys()


@pytest.mark.parametrize('table,column', [
    ('contract_chain_edges', 'metadata_json'),
    ('backlog_contract_chain_current', 'active_chain_json'),
])
@pytest.mark.parametrize('payload', ['{bad', sqlite3.Binary(b'{"snapshot_id":"scope-current"}')])
def test_chain_owner_domains_do_not_exempt_invalid_payloads(conn, table, column, payload):
    _chain_owner_writer_fixture(conn)
    conn.execute(f'UPDATE "{table}" SET "{column}"=?', (payload,)); conn.commit()
    state = store.snapshot_retention_reference_state(conn, PID)
    assert not state['complete'] and table + '_payload_unreadable' in state['refusal_reasons']
    assert {'scope-current', 'scope-durable'} <= state['durable_references'].keys()
