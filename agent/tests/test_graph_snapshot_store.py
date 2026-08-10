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
