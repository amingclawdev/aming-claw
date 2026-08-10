from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
import threading
import time
import tracemalloc

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
    with pytest.raises(
        store.ReconcileRunTerminalizationProofError,
        match="terminalization_replacement_missing",
    ):
        store.reconcile_run_terminalization_proof(
            conn, PID, run_id=fixture["source_run_id"],
            snapshot_id=fixture["source_snapshot_id"],
            manager_certificate=fixture["certificate"],
        )


def test_terminalization_proof_replacement_zero_one_many_and_active_claim(conn):
    missing = _terminalization_proof_fixture(conn, suffix="missing")
    conn.execute(
        "DELETE FROM reconcile_run_metrics WHERE project_id=? AND run_id=?",
        (PID, missing["replacement_run_id"]),
    )
    conn.commit()
    with pytest.raises(
        store.ReconcileRunTerminalizationProofError,
        match="terminalization_replacement_missing",
    ):
        store.reconcile_run_terminalization_proof(
            conn, PID, run_id=missing["source_run_id"],
            snapshot_id=missing["source_snapshot_id"],
            manager_certificate=missing["certificate"],
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
    with pytest.raises(
        store.ReconcileRunTerminalizationProofError,
        match="terminalization_replacement_missing",
    ):
        store.reconcile_run_terminalization_proof(
            conn, PID, run_id=fixture["source_run_id"],
            snapshot_id=fixture["source_snapshot_id"],
            manager_certificate=fixture["certificate"],
        )

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


def test_retention_selection_protects_active_snapshot(conn, tmp_path):
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


def test_retention_selection_protects_keep_last_n(conn, tmp_path):
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


def test_retention_selection_protects_full_baseline(conn, tmp_path):
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


def test_retention_selection_protects_reconcile_in_progress(conn, tmp_path):
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
    # The dry-run result should list it as a candidate
    candidate_ids = {item["snapshot_id"] for item in result["candidates"]}
    assert "scope-orphan-dry-001" in candidate_ids, "orphan dir should appear as a candidate"


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
    assert result["ok"] is True
    # Active snapshot dir must survive
    assert active_dir.exists(), "active snapshot dir must never be deleted"
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
    assert r1["ok"] is True
    assert r2["ok"] is True
    assert r2["errors"] == [], "second GC run must not produce errors"


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


def test_bundle_referenced_snapshot_ids_returns_set(tmp_path):
    """Bundle-referenced snapshot ids must be a set (may be empty in test env)."""
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
    assert after_later_activation["active_snapshot_verified"] is True

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
