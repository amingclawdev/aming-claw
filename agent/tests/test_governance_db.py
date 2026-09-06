"""Tests for governance SQLite database layer."""
import os
import sys
import tempfile
import unittest
import sqlite3
import json
import hashlib
import subprocess
import shutil
import socket
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from governance.db import SCHEMA_VERSION


def _install_fixed_stable_boundary(monkeypatch, tmp_path):
    """Private health/process boundary mocks backed by a real stable worktree."""
    from governance import db

    stable_root = (tmp_path / "stable-runtime").resolve()
    source = stable_root / "agent" / "governance"
    source.mkdir(parents=True)
    (source / "server.py").write_text("# fixed stable health fixture\n", encoding="utf-8")
    (source / "db.py").write_text("# fixture module origin\n", encoding="utf-8")
    shared = stable_root / "shared-volume"
    shared.mkdir()
    database = shared / "codex-tasks" / "state" / "governance" / "aming-claw" / "governance.db"
    database.parent.mkdir(parents=True)
    database.touch()
    subprocess.run(["git", "init", "-b", "codex/direct-no-pass-post-reconcile-r2"], cwd=stable_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=stable_root, check=True)
    subprocess.run(["git", "config", "user.name", "AC Test"], cwd=stable_root, check=True)
    subprocess.run(["git", "add", "."], cwd=stable_root, check=True)
    subprocess.run(["git", "commit", "-m", "stable fixture"], cwd=stable_root, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=stable_root, check=True, capture_output=True, text=True).stdout.strip()
    source_hash = "sha256:" + hashlib.sha256((source / "server.py").read_bytes()).hexdigest()
    database_identity = {"schema_version": "ac_stable_database_identity.v1", "device": database.stat().st_dev, "inode": database.stat().st_ino, "stable_relative_path_sha256": "sha256:" + hashlib.sha256(b"shared-volume/codex-tasks/state/governance/aming-claw/governance.db").hexdigest()}
    health = {
        "status": "ok", "service": "governance", "port": 40000,
        "runtime_plane": "stable", "runtime_stale": False, "pid": 4242,
        "runtime_loaded_version": head,
        "runtime_plane_identity": {"worktree_root": str(stable_root), "branch": "codex/direct-no-pass-post-reconcile-r2", "commit": head, "stable_anchor_commit": head, "database_identity": database_identity, "stable_database_identity": database_identity, "project_allowlist": []},
        "loaded_runtime_identity": {"loaded_commit": head, "loaded_source_path": str(source / "server.py"), "loaded_source_sha256": source_hash, "worktree_source_sha256": source_hash},
    }
    for module in (db, __import__("agent.governance.db", fromlist=["db"])):
        monkeypatch.setattr(module, "__file__", str(source / "db.py"))
        monkeypatch.setattr(module, "_stable_health_request", lambda health=health: dict(health))
        monkeypatch.setattr(module, "_stable_process_identity", lambda pid, root=stable_root: ("fixture-start", "python -m agent.governance.server", str(root)))
    return shared


@pytest.fixture(autouse=True)
def fixed_stable_boundary(monkeypatch, tmp_path):
    _install_fixed_stable_boundary(monkeypatch, tmp_path)


def _canonical_dev_world(tmp_path: Path) -> tuple[Path, Path]:
    """Create the real persistent-temp stable/dev sibling layout used by AC."""
    from agent.runtime_plane import resolve_ac_dev_storage_root
    from governance import db
    stable = Path(db._verified_stable_binding()["shared_volume_path"])
    root = resolve_ac_dev_storage_root(stable)
    os.environ["AMING_CLAW_SHARED_VOLUME"] = str(stable)
    os.environ["AMING_CLAW_DEV_STORAGE_ROOT"] = str(root)
    return root, stable


class TestDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        # Create required directory structure
        os.makedirs(os.path.join(self.tmp.name, "codex-tasks", "state", "governance", "test-project"), exist_ok=True)

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_get_connection_creates_db(self):
        from governance.db import get_connection, close_connection
        conn = get_connection("test-project")
        self.assertIsNotNone(conn)
        # Verify tables exist
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = {t["name"] for t in tables}
        self.assertIn("node_state", table_names)
        self.assertIn("sessions", table_names)
        self.assertIn("tasks", table_names)
        self.assertIn("audit_index", table_names)
        self.assertIn("snapshots", table_names)
        self.assertIn("idempotency_keys", table_names)
        self.assertIn("node_history", table_names)
        close_connection(conn)


def test_ac_dev_graph_materialization_admission_is_idempotent_and_verify_only(monkeypatch):
    from agent.governance import (
        asset_impact,
        asset_projection,
        db,
        graph_correction_patches,
        graph_events,
        graph_snapshot_store,
    )

    monkeypatch.setenv("AMING_CLAW_RUNTIME_PLANE", "dev")
    monkeypatch.setattr(
        db,
        "_require_ac_dev_graph_materialization_runtime_custody",
        lambda _conn, *_args: {"host": "127.0.0.1", "port": 40008},
    )
    monkeypatch.setattr(
        db,
        "canonical_ac_database_identity",
        lambda _conn: {"world_id": "ac-dev", "project_id": "aming-claw"},
    )
    monkeypatch.setattr(
        db,
        "classify_graph_activation_connection",
        lambda _conn: {
            "runtime_plane": "dev",
            "classification_reason": "verified_dev_root_receipt_genesis",
            "active_graph_activation_allowed": False,
        },
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        first = db.admit_ac_dev_graph_materialization_schema(
            conn, project_id="aming-claw"
        )
        inventory = db._graph_materialization_inventory(conn)
        second = db.admit_ac_dev_graph_materialization_schema(
            conn, project_id="aming-claw"
        )
        owner_states = db.classify_graph_materialization_preimage(conn)[
            "owner_states"
        ]
        assert list(owner_states) == [
            "graph_snapshot_store",
            "graph_events",
            "graph_correction_patches",
            "asset_projection",
            "asset_impact",
        ]
        assert set(owner_states.values()) == {"exact"}
        canonical_graph = db._graph_materialization_canonical_inventory()
        assert db._graph_materialization_managed_inventory(
            inventory, canonical_graph,
        ) == canonical_graph
        assert db.classify_semantic_state_schema(conn)["owner_state"] == (
            "exact"
        )
        assert db.classify_graph_query_trace_schema(conn)["owner_state"] == (
            "exact"
        )
        changes = conn.total_changes
        for ensure_schema in (
            graph_snapshot_store.ensure_schema,
            graph_events.ensure_schema,
            graph_correction_patches.ensure_schema,
            asset_projection.ensure_schema,
            asset_impact.ensure_schema,
        ):
            ensure_schema(conn)
        assert conn.total_changes == changes
        assert db._graph_materialization_inventory(conn) == inventory
        assert first == second
    finally:
        conn.close()


@pytest.mark.parametrize(
    "state", ["absent", "exact", "partial", "altered", "extra"],
)
def test_semantic_state_schema_classifier_and_typed_verifier_are_zero_write(state):
    from agent.governance import db, reconcile_semantic_enrichment as semantic

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    if state != "absent":
        db.execute_graph_schema_sql(conn, semantic.SEMANTIC_STATE_SCHEMA_SQL)
        if state == "partial":
            conn.execute("DROP INDEX idx_graph_semantic_nodes_status")
        elif state == "altered":
            conn.execute("DROP INDEX idx_graph_semantic_nodes_status")
            conn.execute(
                "CREATE INDEX idx_graph_semantic_nodes_status "
                "ON graph_semantic_nodes(project_id, status)"
            )
        elif state == "extra":
            conn.execute(
                "CREATE TABLE graph_semantic_unknown_owner(value TEXT)"
            )
        conn.commit()
    before = db._graph_materialization_inventory(conn)
    changes = conn.total_changes

    if state in {"absent", "exact"}:
        classification = db.classify_semantic_state_schema(conn)
        assert classification["owner_state"] == state
        if state == "exact":
            assert len([row for row in before if row[0] == "table"]) == 3
            assert len([row for row in before if row[0] == "index"]) == 6
            db.verify_semantic_state_schema(conn)
        else:
            with pytest.raises(db.DevRuntimeSchemaVerificationError):
                db.verify_semantic_state_schema(conn)
    else:
        with pytest.raises(db.DevRuntimeSchemaVerificationError):
            db.verify_semantic_state_schema(conn)

    assert conn.total_changes == changes
    assert db._graph_materialization_inventory(conn) == before
    conn.close()


@pytest.mark.parametrize(
    "state", ["absent", "exact", "partial", "altered", "extra"],
)
def test_graph_query_trace_schema_classifier_and_typed_verifier_are_zero_write(state):
    from agent.governance import db, graph_query_trace

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    if state != "absent":
        db.execute_graph_schema_sql(conn, graph_query_trace.GRAPH_QUERY_TRACE_SCHEMA_SQL)
        if state == "partial":
            conn.execute("DROP INDEX idx_graph_query_traces_project")
        elif state == "altered":
            conn.execute("DROP INDEX idx_graph_query_traces_project")
            conn.execute(
                "CREATE INDEX idx_graph_query_traces_project "
                "ON graph_query_traces(project_id, status)"
            )
        elif state == "extra":
            conn.execute("CREATE TABLE graph_query_unknown_owner(value TEXT)")
        conn.commit()
    before = db._graph_materialization_inventory(conn)
    changes = conn.total_changes

    if state in {"absent", "exact"}:
        classification = db.classify_graph_query_trace_schema(conn)
        assert classification["owner_state"] == state
        if state == "exact":
            assert len([row for row in before if row[0] == "table"]) == 3
            assert len([row for row in before if row[0] == "index"]) == 5
            db.verify_graph_query_trace_schema(conn)
        else:
            with pytest.raises(db.DevRuntimeSchemaVerificationError):
                db.verify_graph_query_trace_schema(conn)
    else:
        with pytest.raises(db.DevRuntimeSchemaVerificationError):
            db.verify_graph_query_trace_schema(conn)

    assert conn.total_changes == changes
    assert db._graph_materialization_inventory(conn) == before
    conn.close()


def test_dev_world_rejects_semantic_exact_without_exact_graph_zero_write():
    from agent.governance import db, reconcile_semantic_enrichment as semantic

    conn = db._migration_capable_source_schema_memory()
    db.execute_graph_schema_sql(conn, semantic.SEMANTIC_STATE_SCHEMA_SQL)
    conn.commit()
    before = db._sqlite_master_inventory(conn)
    changes = conn.total_changes

    with pytest.raises(ValueError, match="semantic state requires all graph owners"):
        db._verify_dev_world_schema_inventory(conn)

    assert conn.total_changes == changes
    assert db._sqlite_master_inventory(conn) == before
    conn.close()


def test_completed_projection_rejects_semantic_exact_without_exact_graph_zero_write():
    from agent.governance import db, reconcile_semantic_enrichment as semantic

    conn = db._migration_capable_source_schema_memory()
    for statement in db._authority_projection_schema_statements():
        conn.execute(statement)
    db.execute_graph_schema_sql(conn, semantic.SEMANTIC_STATE_SCHEMA_SQL)
    conn.commit()
    before = db._sqlite_master_inventory(conn)
    changes = conn.total_changes

    with pytest.raises(ValueError, match="semantic schema requires exact graph owners"):
        db._completed_generation_schema_projections(conn)

    assert conn.total_changes == changes
    assert db._sqlite_master_inventory(conn) == before
    conn.close()


def _install_post_structural_profile(db, conn, profile):
    from agent.governance import graph_query_trace
    from agent.governance import reconcile_semantic_enrichment as semantic

    if profile != "baseline_absent":
        _install_all_graph_owners_for_inventory_test(db, conn)
    if profile in {"graph_semantic", "all_exact"}:
        db.execute_graph_schema_sql(conn, semantic.SEMANTIC_STATE_SCHEMA_SQL)
    if profile in {"all_exact", "graph_trace", "baseline_trace"}:
        db.execute_graph_schema_sql(
            conn, graph_query_trace.GRAPH_QUERY_TRACE_SCHEMA_SQL,
        )
    conn.commit()


@pytest.mark.parametrize(
    ("profile", "accepted"),
    [
        ("baseline_absent", True),
        ("graph_only", True),
        ("graph_semantic", True),
        ("all_exact", True),
        ("graph_trace", False),
        ("baseline_trace", False),
    ],
)
def test_dev_world_trace_owner_legal_profiles_are_zero_write(profile, accepted):
    from agent.governance import db

    conn = db._migration_capable_source_schema_memory()
    _install_post_structural_profile(db, conn, profile)
    before = db._sqlite_master_inventory(conn)
    changes = conn.total_changes

    if accepted:
        db._verify_dev_world_schema_inventory(conn)
    else:
        with pytest.raises(ValueError, match="graph-query trace requires"):
            db._verify_dev_world_schema_inventory(conn)

    assert conn.total_changes == changes
    assert db._sqlite_master_inventory(conn) == before
    conn.close()


@pytest.mark.parametrize(
    ("profile", "accepted"),
    [
        ("baseline_absent", True),
        ("graph_only", True),
        ("graph_semantic", True),
        ("all_exact", True),
        ("graph_trace", False),
        ("baseline_trace", False),
    ],
)
def test_completed_projection_trace_owner_legal_profiles_bind_both_hashes(
    profile, accepted,
):
    from agent.governance import db

    conn = db._migration_capable_source_schema_memory()
    for statement in db._authority_projection_schema_statements():
        conn.execute(statement)
    conn.commit()
    expected_authority = db._authority_projection_inventory_in_managed_world(conn)
    expected_protected = db.backlog_read_schema_protected_inventory(conn)
    _install_post_structural_profile(db, conn, profile)
    before = db._sqlite_master_inventory(conn)
    changes = conn.total_changes

    if accepted:
        authority, protected = db._completed_generation_schema_projections(conn)
        assert authority == expected_authority
        assert protected == expected_protected
    else:
        with pytest.raises(ValueError, match="trace schema requires"):
            db._completed_generation_schema_projections(conn)

    assert conn.total_changes == changes
    assert db._sqlite_master_inventory(conn) == before
    conn.close()


def _install_graph_owner_for_preimage_test(db, conn, ensure_schema):
    connection_ids = set(
        getattr(db._GRAPH_MATERIALIZATION_ADMISSION_LOCAL, "connection_ids", ())
    )
    connection_ids.add(id(conn))
    db._GRAPH_MATERIALIZATION_ADMISSION_LOCAL.connection_ids = frozenset(connection_ids)
    try:
        ensure_schema(conn)
    finally:
        connection_ids.discard(id(conn))
        db._GRAPH_MATERIALIZATION_ADMISSION_LOCAL.connection_ids = frozenset(
            connection_ids
        )


def _install_all_graph_owners_for_inventory_test(db, conn):
    from agent.governance import (
        asset_impact,
        asset_projection,
        graph_correction_patches,
        graph_events,
        graph_snapshot_store,
    )

    for ensure_schema in (
        graph_snapshot_store.ensure_schema,
        graph_events.ensure_schema,
        graph_correction_patches.ensure_schema,
        asset_projection.ensure_schema,
        asset_impact.ensure_schema,
    ):
        _install_graph_owner_for_preimage_test(db, conn, ensure_schema)


@pytest.mark.parametrize("owner", ["asset_projection", "asset_impact"])
def test_exact_dev_asset_owner_ensure_is_zero_write_and_preserves_outer_transaction(
    monkeypatch,
    owner,
):
    from agent.governance import asset_impact, asset_projection, db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _install_all_graph_owners_for_inventory_test(db, conn)
    conn.execute("CREATE TABLE transaction_probe(value TEXT)")
    conn.commit()
    before_inventory = db._graph_materialization_inventory(conn)
    monkeypatch.setenv(db.RUNTIME_PLANE_ENV, db.DEV_RUNTIME_PLANE)

    conn.execute("BEGIN")
    conn.execute("INSERT INTO transaction_probe(value) VALUES('before')")
    ensure_schema = {
        "asset_projection": asset_projection.ensure_schema,
        "asset_impact": asset_impact.ensure_schema,
    }[owner]
    ensure_schema(conn)

    assert conn.in_transaction is True
    assert db._graph_materialization_inventory(conn) == before_inventory
    conn.execute("INSERT INTO transaction_probe(value) VALUES('after')")
    assert conn.execute(
        "SELECT COUNT(*) FROM transaction_probe"
    ).fetchone()[0] == 2
    conn.rollback()
    assert conn.execute(
        "SELECT COUNT(*) FROM transaction_probe"
    ).fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("drift", ["absent", "partial", "altered"])
def test_dev_asset_owner_ensure_rejects_schema_drift_typed_and_zero_write(
    monkeypatch,
    drift,
):
    from agent.governance import asset_projection, db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    if drift != "absent":
        _install_all_graph_owners_for_inventory_test(db, conn)
        conn.execute("DROP INDEX idx_graph_asset_projection_path")
        if drift == "altered":
            conn.execute(
                "CREATE INDEX idx_graph_asset_projection_path "
                "ON graph_asset_projection(project_id, snapshot_id)"
            )
        conn.commit()
    before_inventory = db._graph_materialization_inventory(conn)
    before_changes = conn.total_changes
    monkeypatch.setenv(db.RUNTIME_PLANE_ENV, db.DEV_RUNTIME_PLANE)

    with pytest.raises(db.DevRuntimeSchemaVerificationError):
        asset_projection.ensure_schema(conn)

    assert conn.total_changes == before_changes
    assert db._graph_materialization_inventory(conn) == before_inventory
    conn.close()


def test_ac_dev_graph_admission_creates_projection_and_impact_atomically(
    monkeypatch,
):
    from agent.governance import (
        asset_impact,
        db,
        graph_correction_patches,
        graph_events,
        graph_snapshot_store,
    )

    monkeypatch.setenv(db.RUNTIME_PLANE_ENV, db.DEV_RUNTIME_PLANE)
    monkeypatch.setattr(
        db,
        "_require_ac_dev_graph_materialization_runtime_custody",
        lambda _conn: {"host": "127.0.0.1", "port": 40008},
    )
    monkeypatch.setattr(
        db,
        "canonical_ac_database_identity",
        lambda _conn: {"world_id": "ac-dev", "project_id": "aming-claw"},
    )
    monkeypatch.setattr(
        db,
        "classify_graph_activation_connection",
        lambda _conn: {
            "runtime_plane": "dev",
            "classification_reason": "verified_dev_cow_successor_receipt_history",
            "active_graph_activation_allowed": False,
        },
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for ensure_schema in (
        graph_snapshot_store.ensure_schema,
        graph_events.ensure_schema,
        graph_correction_patches.ensure_schema,
    ):
        _install_graph_owner_for_preimage_test(db, conn, ensure_schema)
    conn.commit()
    before = db.classify_graph_materialization_preimage(conn)["owner_states"]
    assert before["asset_projection"] == "absent"
    assert before["asset_impact"] == "absent"

    db.admit_ac_dev_graph_materialization_schema(conn, project_id="aming-claw")

    after = db.classify_graph_materialization_preimage(conn)["owner_states"]
    assert after["asset_projection"] == "exact"
    assert after["asset_impact"] == "exact"

    rollback_conn = sqlite3.connect(":memory:")
    rollback_conn.row_factory = sqlite3.Row
    for ensure_schema in (
        graph_snapshot_store.ensure_schema,
        graph_events.ensure_schema,
        graph_correction_patches.ensure_schema,
    ):
        _install_graph_owner_for_preimage_test(db, rollback_conn, ensure_schema)
    rollback_conn.commit()
    original_impact_ensure = asset_impact.ensure_schema

    def fail_after_impact_schema(candidate):
        original_impact_ensure(candidate)
        if candidate is rollback_conn:
            raise RuntimeError("impact admission sentinel")

    monkeypatch.setattr(asset_impact, "ensure_schema", fail_after_impact_schema)
    with pytest.raises(RuntimeError, match="impact admission sentinel"):
        db.admit_ac_dev_graph_materialization_schema(
            rollback_conn,
            project_id="aming-claw",
        )
    rolled_back = db.classify_graph_materialization_preimage(rollback_conn)[
        "owner_states"
    ]
    assert rolled_back["asset_projection"] == "absent"
    assert rolled_back["asset_impact"] == "absent"
    conn.close()
    rollback_conn.close()


@pytest.mark.parametrize("owner", ["asset_projection", "asset_impact"])
def test_stable_asset_owner_ensure_preserves_executescript_without_dev_verify(
    monkeypatch,
    owner,
):
    from agent.governance import asset_impact, asset_projection, db

    module = {
        "asset_projection": asset_projection,
        "asset_impact": asset_impact,
    }[owner]
    scripts = []

    class StableConnection:
        def executescript(self, sql):
            scripts.append(sql)

    monkeypatch.setattr(db, "dev_runtime_verify_only", lambda: False)
    monkeypatch.setattr(
        db,
        "verify_graph_materialization_schema",
        lambda _conn: pytest.fail("stable ensure reached dev verification"),
    )
    monkeypatch.setattr(
        db,
        "graph_materialization_admission_active",
        lambda _conn: False,
    )

    module.ensure_schema(StableConnection())

    assert scripts == [module.SCHEMA_SQL]


def test_graph_materialization_preimage_classifier_accepts_only_exact_predecessor_without_write():
    from agent.governance import (
        asset_impact,
        asset_projection,
        db,
        graph_snapshot_store,
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _install_graph_owner_for_preimage_test(db, conn, graph_snapshot_store.ensure_schema)
    _install_graph_owner_for_preimage_test(db, conn, asset_projection.ensure_schema)
    _install_graph_owner_for_preimage_test(db, conn, asset_impact.ensure_schema)
    conn.execute("DROP INDEX idx_pending_scope_branch")
    conn.execute("DROP INDEX idx_pending_scope_status")
    before_inventory = db._graph_materialization_inventory(conn)
    before_changes = conn.total_changes

    result = db.classify_graph_materialization_preimage(conn)

    assert result["owner_states"] == {
        "graph_snapshot_store": "pending_scope_index_predecessor",
        "graph_events": "absent",
        "graph_correction_patches": "absent",
        "asset_projection": "exact",
        "asset_impact": "exact",
    }
    assert set(result["planned_objects"]) == {
        "idx_pending_scope_branch",
        "idx_pending_scope_status",
    }
    assert len(result["planned_ddl"]) == 2
    assert all(sql.startswith("CREATE INDEX") for sql in result["planned_ddl"])
    assert conn.total_changes == before_changes
    assert db._graph_materialization_inventory(conn) == before_inventory

    for sql in result["planned_ddl"]:
        conn.execute(sql)
    repaired = db.classify_graph_materialization_preimage(conn)
    assert repaired["owner_states"]["graph_snapshot_store"] == "exact"
    assert repaired["planned_objects"] == []
    conn.close()


@pytest.mark.parametrize("drift", ["one_more_missing", "altered", "unknown"])
def test_graph_materialization_preimage_classifier_rejects_other_authority(drift):
    from agent.governance import db, graph_snapshot_store

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _install_graph_owner_for_preimage_test(db, conn, graph_snapshot_store.ensure_schema)
    conn.execute("DROP INDEX idx_pending_scope_branch")
    conn.execute("DROP INDEX idx_pending_scope_status")
    if drift == "one_more_missing":
        conn.execute("DROP INDEX idx_graph_snapshots_status")
    elif drift == "altered":
        conn.execute("DROP INDEX idx_graph_snapshots_status")
        conn.execute(
            "CREATE INDEX idx_graph_snapshots_status "
            "ON graph_snapshots(project_id, commit_sha)"
        )
    else:
        conn.execute("CREATE TABLE graph_unknown_authority (id TEXT PRIMARY KEY)")
    before_inventory = db._graph_materialization_inventory(conn)
    before_changes = conn.total_changes

    with pytest.raises(ValueError, match="preimage"):
        db.classify_graph_materialization_preimage(conn)

    assert conn.total_changes == before_changes
    assert db._graph_materialization_inventory(conn) == before_inventory
    conn.close()


@pytest.mark.parametrize("drift", ["partial", "altered"])
def test_graph_materialization_preimage_classifier_rejects_sibling_drift(drift):
    from agent.governance import asset_projection, db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _install_graph_owner_for_preimage_test(db, conn, asset_projection.ensure_schema)
    conn.execute("DROP INDEX idx_graph_asset_projection_path")
    if drift == "altered":
        conn.execute(
            "CREATE INDEX idx_graph_asset_projection_path "
            "ON graph_asset_projection(project_id, snapshot_id)"
        )
    before_inventory = db._graph_materialization_inventory(conn)
    before_changes = conn.total_changes

    with pytest.raises(ValueError, match="asset_projection"):
        db.classify_graph_materialization_preimage(conn)

    assert conn.total_changes == before_changes
    assert db._graph_materialization_inventory(conn) == before_inventory
    conn.close()


def test_ac_dev_graph_materialization_admission_rolls_back_partial_schema(monkeypatch):
    from agent.governance import db, graph_correction_patches

    monkeypatch.setenv("AMING_CLAW_RUNTIME_PLANE", "dev")
    monkeypatch.setattr(
        db,
        "_require_ac_dev_graph_materialization_runtime_custody",
        lambda _conn, *_args: {"host": "127.0.0.1", "port": 40008},
    )
    monkeypatch.setattr(
        db,
        "canonical_ac_database_identity",
        lambda _conn: {"world_id": "ac-dev", "project_id": "aming-claw"},
    )
    monkeypatch.setattr(
        db,
        "classify_graph_activation_connection",
        lambda _conn: {
            "runtime_plane": "dev",
            "classification_reason": "verified_dev_root_receipt_genesis",
            "active_graph_activation_allowed": False,
        },
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    original = graph_correction_patches.ensure_schema

    def fail_target_only(candidate):
        original(candidate)
        if candidate is conn:
            raise RuntimeError("injected owner failure")

    monkeypatch.setattr(graph_correction_patches, "ensure_schema", fail_target_only)
    with pytest.raises(RuntimeError, match="injected owner failure"):
        db.admit_ac_dev_graph_materialization_schema(conn, project_id="aming-claw")
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'graph_%'"
    ).fetchone()[0] == 0
    conn.close()


def test_ac_dev_graph_materialization_admission_rolls_back_semantic_schema(monkeypatch):
    from agent.governance import db, reconcile_semantic_enrichment as semantic

    monkeypatch.setenv(db.RUNTIME_PLANE_ENV, db.DEV_RUNTIME_PLANE)
    monkeypatch.setattr(
        db,
        "_require_ac_dev_graph_materialization_runtime_custody",
        lambda _conn: {"host": "127.0.0.1", "port": 40008},
    )
    monkeypatch.setattr(
        db,
        "canonical_ac_database_identity",
        lambda _conn: {"world_id": "ac-dev", "project_id": "aming-claw"},
    )
    monkeypatch.setattr(
        db,
        "classify_graph_activation_connection",
        lambda _conn: {
            "runtime_plane": "dev",
            "classification_reason": "verified_dev_root_receipt_genesis",
            "active_graph_activation_allowed": False,
        },
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    original = semantic._ensure_semantic_state_schema

    def fail_after_semantic_schema(candidate):
        original(candidate)
        raise RuntimeError("semantic admission sentinel")

    monkeypatch.setattr(
        semantic, "_ensure_semantic_state_schema", fail_after_semantic_schema,
    )
    with pytest.raises(RuntimeError, match="semantic admission sentinel"):
        db.admit_ac_dev_graph_materialization_schema(
            conn, project_id="aming-claw",
        )

    assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    conn.close()


def test_ac_dev_graph_admission_reclassifies_semantic_race_inside_transaction(
    monkeypatch,
):
    from agent.governance import db

    monkeypatch.setenv(db.RUNTIME_PLANE_ENV, db.DEV_RUNTIME_PLANE)
    monkeypatch.setattr(
        db,
        "_require_ac_dev_graph_materialization_runtime_custody",
        lambda _conn: {"host": "127.0.0.1", "port": 40008},
    )
    monkeypatch.setattr(
        db,
        "canonical_ac_database_identity",
        lambda _conn: {"world_id": "ac-dev", "project_id": "aming-claw"},
    )
    monkeypatch.setattr(
        db,
        "classify_graph_activation_connection",
        lambda _conn: {
            "runtime_plane": "dev",
            "classification_reason": "verified_dev_root_receipt_genesis",
            "active_graph_activation_allowed": False,
        },
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    original = db.classify_semantic_state_schema
    calls = 0

    def inject_partial_on_transactional_recheck(candidate):
        nonlocal calls
        calls += 1
        if candidate is conn and calls == 2:
            candidate.execute(
                "CREATE TABLE graph_semantic_nodes(project_id TEXT)"
            )
        return original(candidate)

    monkeypatch.setattr(
        db,
        "classify_semantic_state_schema",
        inject_partial_on_transactional_recheck,
    )
    with pytest.raises(ValueError, match="semantic state schema"):
        db.admit_ac_dev_graph_materialization_schema(
            conn, project_id="aming-claw",
        )

    assert calls == 2
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    conn.close()


def test_ac_dev_graph_admission_reclassifies_trace_race_inside_transaction(
    monkeypatch,
):
    from agent.governance import db

    monkeypatch.setenv(db.RUNTIME_PLANE_ENV, db.DEV_RUNTIME_PLANE)
    monkeypatch.setattr(
        db,
        "_require_ac_dev_graph_materialization_runtime_custody",
        lambda _conn: {"host": "127.0.0.1", "port": 40008},
    )
    monkeypatch.setattr(
        db,
        "canonical_ac_database_identity",
        lambda _conn: {"world_id": "ac-dev", "project_id": "aming-claw"},
    )
    monkeypatch.setattr(
        db,
        "classify_graph_activation_connection",
        lambda _conn: {
            "runtime_plane": "dev",
            "classification_reason": "verified_dev_root_receipt_genesis",
            "active_graph_activation_allowed": False,
        },
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    original = db.classify_graph_query_trace_schema
    calls = 0

    def inject_partial_on_transactional_recheck(candidate):
        nonlocal calls
        calls += 1
        if candidate is conn and calls == 2:
            candidate.execute("CREATE TABLE graph_query_traces(trace_id TEXT)")
        return original(candidate)

    monkeypatch.setattr(
        db,
        "classify_graph_query_trace_schema",
        inject_partial_on_transactional_recheck,
    )
    with pytest.raises(ValueError, match="graph-query trace schema"):
        db.admit_ac_dev_graph_materialization_schema(
            conn, project_id="aming-claw",
        )

    assert calls == 2
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    conn.close()


def test_ac_dev_graph_materialization_admission_denies_wrong_world_without_write(monkeypatch):
    from agent.governance import db

    monkeypatch.setenv("AMING_CLAW_RUNTIME_PLANE", "dev")
    monkeypatch.setattr(
        db,
        "_require_ac_dev_graph_materialization_runtime_custody",
        lambda _conn, *_args: {"host": "127.0.0.1", "port": 40008},
    )
    monkeypatch.setattr(
        db,
        "canonical_ac_database_identity",
        lambda _conn: {"world_id": "ac-dev", "project_id": "aming-claw"},
    )
    monkeypatch.setattr(
        db,
        "classify_graph_activation_connection",
        lambda _conn: {
            "runtime_plane": "stable",
            "classification_reason": "verified_stable_database_binding",
            "active_graph_activation_allowed": True,
        },
    )
    conn = sqlite3.connect(":memory:")
    before = conn.total_changes
    with pytest.raises(ValueError, match="identity is not admitted"):
        db.admit_ac_dev_graph_materialization_schema(conn, project_id="aming-claw")
    assert conn.total_changes == before
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    conn.close()


def test_graph_materialization_listener_requires_real_current_process_40008(monkeypatch):
    from agent.governance import db

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 40008))
    listener.listen(1)
    try:
        observed = db._default_cutover_listener_probe(40008)
        assert observed["pid"] == os.getpid()
        assert observed["listener_addresses"] == ["127.0.0.1:40008"]
        db._validate_ac_dev_graph_materialization_listener(observed)
    finally:
        listener.close()


def test_graph_materialization_runtime_custody_binds_real_listener_and_lease(
    monkeypatch, tmp_path
):
    from agent.governance import db

    database = (tmp_path / "governance.db").absolute()
    connection = sqlite3.connect(database)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 40008))
    listener.listen(1)
    handle = open(tmp_path / "writer.lock", "a+", encoding="utf-8")
    metadata = database.stat(follow_symlinks=False)
    owner_start = db._writer_process_start_identity()
    lease = {
        "handle": handle,
        "owner_pid": os.getpid(),
        "owner_start_identity": owner_start,
        "database_device": int(metadata.st_dev),
        "database_inode": int(metadata.st_ino),
    }
    monkeypatch.setattr(db, "_dev_database_path", lambda: database)
    monkeypatch.setattr(db, "_dev_storage_root", lambda **_kwargs: tmp_path)
    monkeypatch.setattr(
        db,
        "validate_dev_launch_receipt",
        lambda *_args, **_kwargs: {
            "runtime_plane": "dev",
            "world_id": "ac-dev",
            "project_id": "aming-claw",
            "port": 40008,
            "background": False,
        },
    )
    db._DEV_DATABASE_WRITER_LEASES[str(database)] = lease
    try:
        custody = db._require_ac_dev_graph_materialization_runtime_custody(
            connection
        )
        assert custody["host"] == "127.0.0.1"
        assert custody["port"] == 40008
        assert custody["pid"] == os.getpid()
    finally:
        db._DEV_DATABASE_WRITER_LEASES.pop(str(database), None)
        handle.close()
        listener.close()
        connection.close()


def test_graph_materialization_listener_rejects_40009_and_env_spoof(monkeypatch):
    from agent.governance import db

    monkeypatch.setenv("GOVERNANCE_PORT", "40009")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 40009))
    listener.listen(1)
    try:
        observed = db._default_cutover_listener_probe(40008)
        with pytest.raises(ValueError, match="listener custody mismatch"):
            db._validate_ac_dev_graph_materialization_listener(observed)
    finally:
        listener.close()


def test_graph_materialization_listener_rejects_foreign_pid():
    from agent.governance import db

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import socket,time; s=socket.socket(); "
                "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); "
                "s.bind(('127.0.0.1',40008)); s.listen(1); "
                "print('ready',flush=True); time.sleep(10)"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        observed = db._default_cutover_listener_probe(40008)
        assert observed["pid"] == process.pid
        with pytest.raises(ValueError, match="listener custody mismatch"):
            db._validate_ac_dev_graph_materialization_listener(observed)
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_graph_materialization_runtime_rejects_before_schema_begin(monkeypatch):
    from agent.governance import db

    monkeypatch.setenv("AMING_CLAW_RUNTIME_PLANE", "dev")
    monkeypatch.setattr(
        db,
        "_require_ac_dev_graph_materialization_runtime_custody",
        lambda _conn, *_args: (_ for _ in ()).throw(
            ValueError("AC dev graph materialization listener custody mismatch")
        ),
    )
    conn = sqlite3.connect(":memory:")
    with pytest.raises(ValueError, match="listener custody mismatch"):
        db.admit_ac_dev_graph_materialization_schema(
            conn, project_id="aming-claw"
        )
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    conn.close()


class TestACDevDatabaseIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        os.environ.pop("AMING_CLAW_RUNTIME_PLANE", None)
        os.environ.pop("AMING_CLAW_DEV_STORAGE_ROOT", None)
        self.dev_storage_root, self.stable_shared_volume = _canonical_dev_world(Path(self.tmp.name))

    def tearDown(self):
        for key in (
            "SHARED_VOLUME_PATH",
            "AMING_CLAW_RUNTIME_PLANE",
            "AMING_CLAW_DB_MIGRATION_POLICY",
            "AMING_CLAW_ALLOWED_PROJECT_IDS",
            "AMING_CLAW_DEV_STORAGE_ROOT",
            "AMING_CLAW_SHARED_VOLUME",
        ):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def _bootstrap_dev_db(self):
        from governance import db

        storage_root = self.dev_storage_root
        receipt = db.bootstrap_dev_governance_store(
            storage_root,
            source_identity={
                "root": str(Path(self.tmp.name) / "source"),
                "branch": "codex/ac-dev",
                "commit": "a" * 40,
                "source_sha256": "sha256:" + "b" * 64,
            },
            process_identity={"pid": 1234, "start_identity": "pytest"},
        )
        os.environ["AMING_CLAW_DEV_STORAGE_ROOT"] = str(storage_root)
        os.environ["AMING_CLAW_RUNTIME_PLANE"] = "dev"
        return Path(receipt["database_path"])

    def _create_ac_db(self):
        from governance.db import get_connection

        conn = get_connection("aming-claw")
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        conn.close()
        return path

    def _create_registered_external_db(
        self,
        project_id="content-sys",
        *,
        initialized=True,
        status="active",
        public_safe=True,
    ):
        from governance.db import get_connection

        conn = get_connection(project_id)
        path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        conn.close()
        root = path.parent.parent
        registry = root / "projects.json"
        registry.write_text(
            json.dumps(
                {
                    "version": 1,
                    "projects": {
                        project_id: {
                            "project_id": project_id,
                            "name": project_id,
                            "initialized": initialized,
                            "status": status,
                            "project_config": {
                                "governance": {
                                    "policy": {"public_safe": public_safe}
                                }
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _database_metadata(path):
        result = []
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = Path(str(path) + suffix)
            if not candidate.exists():
                result.append((suffix, False, 0, 0, 0, 0))
                continue
            current = candidate.stat(follow_symlinks=False)
            result.append(
                (
                    suffix,
                    True,
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                )
            )
        return result

    def test_dev_external_reader_requires_exact_registered_public_safe_identity(self):
        from governance.db import registered_public_safe_external_project

        path = self._create_registered_external_db()
        before = self._database_metadata(path)
        os.environ["AMING_CLAW_RUNTIME_PLANE"] = "dev"
        for rejected in ("content-sys", "content_sys", "contentSys", "../content-sys", "missing"):
            with self.subTest(rejected=rejected), self.assertRaisesRegex(
                ValueError, "external project discovery is retired"
            ):
                registered_public_safe_external_project(rejected)
        self.assertEqual(self._database_metadata(path), before)

    def test_dev_external_registry_validation_never_opens_sqlite_storage(self):
        from governance import db as governance_db

        path = self._create_registered_external_db()
        owner = sqlite3.connect(path)
        owner.execute("PRAGMA journal_mode=WAL")
        owner.execute("CREATE TABLE stable_owner_probe(value TEXT)")
        owner.execute("INSERT INTO stable_owner_probe VALUES ('committed')")
        owner.commit()
        owner.execute("SELECT * FROM stable_owner_probe").fetchall()
        targets = [
            Path(str(path) + suffix)
            for suffix in ("", "-wal", "-shm", "-journal")
        ]

        def storage_snapshot():
            return [
                (
                    target.exists(),
                    target.read_bytes() if target.exists() else b"",
                    (
                        target.stat(follow_symlinks=False).st_dev,
                        target.stat(follow_symlinks=False).st_ino,
                        target.stat(follow_symlinks=False).st_size,
                        target.stat(follow_symlinks=False).st_mtime_ns,
                    )
                    if target.exists()
                    else (),
                )
                for target in targets
            ]

        self.assertTrue(Path(str(path) + "-wal").is_file())
        self.assertTrue(Path(str(path) + "-shm").is_file())
        before = storage_snapshot()
        original_path_open = Path.open

        def guarded_open(candidate, *args, **kwargs):
            if candidate in targets:
                self.fail(f"dev external validation opened SQLite storage: {candidate.name}")
            return original_path_open(candidate, *args, **kwargs)

        try:
            os.environ["AMING_CLAW_RUNTIME_PLANE"] = "dev"
            with mock.patch.object(
                governance_db.sqlite3,
                "connect",
                side_effect=AssertionError("external sqlite3.connect is forbidden"),
            ), mock.patch.object(Path, "open", guarded_open):
                with self.assertRaisesRegex(ValueError, "discovery is retired"):
                    governance_db.registered_public_safe_external_project(
                        "content-sys"
                    )
            self.assertEqual(storage_snapshot(), before)
            self.assertEqual(owner.total_changes, 1)
        finally:
            owner.close()

    def test_dev_external_reader_rejects_inactive_private_and_symlink_storage(self):
        from governance.db import registered_public_safe_external_project

        for status, public_safe in (("paused", True), ("active", False)):
            with self.subTest(status=status, public_safe=public_safe):
                self._create_registered_external_db(
                    status=status,
                    public_safe=public_safe,
                )
                os.environ["AMING_CLAW_RUNTIME_PLANE"] = "dev"
                with self.assertRaises(ValueError):
                    registered_public_safe_external_project("content-sys")
                os.environ.pop("AMING_CLAW_RUNTIME_PLANE", None)

        path = self._create_registered_external_db()
        outside = Path(self.tmp.name) / "outside-external.db"
        path.replace(outside)
        path.symlink_to(outside)
        os.environ["AMING_CLAW_RUNTIME_PLANE"] = "dev"
        with self.assertRaisesRegex(ValueError, "discovery is retired"):
            registered_public_safe_external_project("content-sys")

    def test_dev_external_registry_rejects_symlink_and_nonregular_sidecars(self):
        from governance.db import registered_public_safe_external_project

        path = self._create_registered_external_db()
        outside = Path(self.tmp.name) / "outside-wal"
        outside.write_bytes(b"wal")
        wal = Path(str(path) + "-wal")
        wal.symlink_to(outside)
        os.environ["AMING_CLAW_RUNTIME_PLANE"] = "dev"
        with self.assertRaisesRegex(ValueError, "discovery is retired"):
            registered_public_safe_external_project("content-sys")

        wal.unlink()
        wal.mkdir()
        with self.assertRaisesRegex(ValueError, "discovery is retired"):
            registered_public_safe_external_project("content-sys")

    def test_dev_rejects_foreign_empty_and_traversal_before_project_creation(self):
        from governance.db import get_connection

        self._bootstrap_dev_db()
        root = Path(os.environ["AMING_CLAW_DEV_STORAGE_ROOT"]) / "governance"
        for project_id in (
            "",
            "foreign",
            "amingClaw",
            "../aming-claw",
            "aming-claw/..",
        ):
            with self.assertRaises(ValueError):
                get_connection(project_id)
        self.assertFalse((root / "foreign").exists())

    def test_dev_requires_existing_database_and_never_creates_it(self):
        from governance.db import get_connection

        storage_root = self.dev_storage_root
        root = storage_root / "governance" / "aming-claw"
        root.mkdir(parents=True)
        os.environ["AMING_CLAW_DEV_STORAGE_ROOT"] = str(storage_root)
        os.environ["AMING_CLAW_RUNTIME_PLANE"] = "dev"
        with self.assertRaises(FileNotFoundError):
            get_connection("aming-claw")
        self.assertFalse((root / "governance.db").exists())

    def test_dev_schema_mismatch_fails_without_auto_migration(self):
        from governance.db import get_connection

        path = self._bootstrap_dev_db()
        raw = sqlite3.connect(path)
        raw.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION - 1),),
        )
        raw.commit()
        raw.close()

        with self.assertRaisesRegex(RuntimeError, "schema mismatch"):
            get_connection("aming-claw")

        verify = sqlite3.connect(path)
        value = verify.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        verify.close()
        self.assertEqual(value, str(SCHEMA_VERSION - 1))

    def test_dev_opens_exact_existing_compatible_database(self):
        from governance.db import get_connection

        self._bootstrap_dev_db()
        conn = get_connection("aming-claw")
        value = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(value, str(SCHEMA_VERSION))

    def test_canonical_database_identity_survives_normal_sqlite_writes(self):
        from governance.db import canonical_ac_database_identity

        path = self._bootstrap_dev_db()
        before = canonical_ac_database_identity()
        raw = sqlite3.connect(path)
        raw.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            ("identity-write-test", "ok"),
        )
        raw.commit()
        after = canonical_ac_database_identity(raw)
        raw.close()

        self.assertEqual(after, before)
        self.assertNotIn("path", after)

    def test_dev_rejects_governance_database_symlink_escape(self):
        from governance.db import get_connection

        path = self._bootstrap_dev_db()
        outside = os.path.join(self.tmp.name, "outside-governance.db")
        os.replace(path, outside)
        os.symlink(outside, path)
        with self.assertRaisesRegex(ValueError, "cannot be a symlink"):
            get_connection("aming-claw")

        self.assertTrue(os.path.isfile(outside))

    def test_dev_connection_denies_schema_and_attachment_mutation(self):
        from governance.db import get_connection

        path = self._bootstrap_dev_db()
        conn = get_connection("aming-claw")
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("CREATE TABLE dev_should_not_exist (id INTEGER)")
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION + 1),),
            )
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("ATTACH DATABASE ':memory:' AS foreign_db")
        conn.close()

        verify = sqlite3.connect(path)
        table = verify.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'dev_should_not_exist'"
        ).fetchone()
        version = verify.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        verify.close()
        self.assertIsNone(table)
        self.assertEqual(version, str(SCHEMA_VERSION))

    def test_schema_version_tracking(self):
        from governance.db import get_connection, close_connection
        conn = get_connection("test-project")
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        self.assertEqual(row["value"], str(SCHEMA_VERSION))
        close_connection(conn)

    def test_wal_mode(self):
        from governance.db import get_connection, close_connection
        conn = get_connection("test-project")
        mode = conn.execute("PRAGMA journal_mode").fetchone()
        self.assertEqual(mode[0], "wal")
        close_connection(conn)

    def test_db_context(self):
        from governance.db import DBContext
        with DBContext("test-project") as conn:
            conn.execute(
                "INSERT INTO node_state (project_id, node_id, verify_status, updated_at) VALUES (?, ?, ?, ?)",
                ("test-project", "L0.1", "pending", "2026-01-01"),
            )
        # Verify committed
        from governance.db import get_connection, close_connection
        conn = get_connection("test-project")
        row = conn.execute("SELECT * FROM node_state WHERE node_id = 'L0.1'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["verify_status"], "pending")
        close_connection(conn)


if __name__ == "__main__":
    unittest.main()
def test_ac_dev_world_bootstrap_is_source_only_and_physically_disjoint(tmp_path, monkeypatch):
    import sqlite3

    from agent.governance import db

    legacy_shared = tmp_path / "legacy" / "shared-volume"
    legacy_db = (
        legacy_shared
        / "codex-tasks"
        / "state"
        / "governance"
        / "aming-claw"
        / "governance.db"
    )
    legacy_db.parent.mkdir(parents=True)
    legacy_db.write_bytes(b"immutable legacy archive")
    legacy_before = legacy_db.read_bytes()

    source = {
        "root": str(tmp_path / "source"),
        "branch": "codex/ac-dev",
        "commit": "a" * 40,
        "source_sha256": "sha256:" + "b" * 64,
    }
    stable = Path(db._verified_stable_binding()["shared_volume_path"])
    stable_git_root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=stable,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    )
    stable_git_common_dir = Path(
        subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=stable, check=True, capture_output=True, text=True,
        ).stdout.strip()
    )
    stable_status_before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=stable_git_root,
        check=True, capture_output=True, text=True,
    ).stdout
    storage_root, _ = _canonical_dev_world(tmp_path)
    assert storage_root.parent.parent == stable_git_root.parent
    assert stable_git_root not in storage_root.parents
    assert stable_git_common_dir not in storage_root.parents
    receipt = db.bootstrap_dev_governance_store(
        storage_root,
        source_identity=source,
        process_identity={"pid": 123, "start_identity": "test-process"},
    )

    dev_db = Path(receipt["database_path"])
    assert dev_db.is_file()
    assert not dev_db.is_symlink()
    assert dev_db != legacy_db
    assert legacy_db.read_bytes() == legacy_before
    assert receipt["schema_version"] == "ac_governance_world_genesis.v1"
    assert receipt["world_id"] == "ac-dev"
    assert receipt["project_id"] == "aming-claw"
    assert receipt["rows_copied"] == 0
    assert receipt["source_only"] is True
    assert receipt["database_identity"]["inode"] == dev_db.stat().st_ino
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=stable_git_root,
        check=True, capture_output=True, text=True,
    ).stdout == stable_status_before

    conn = sqlite3.connect(dev_db)
    try:
        meta = dict(conn.execute("SELECT key, value FROM schema_meta"))
        assert meta["governance_world_id"] == "ac-dev"
        assert meta["governance_world_genesis_sha256"] == receipt["genesis_sha256"]
        assert int(meta["schema_version"]) == db.SCHEMA_VERSION
    finally:
        conn.close()

    monkeypatch.setenv("AMING_CLAW_RUNTIME_PLANE", "dev")
    monkeypatch.setenv("AMING_CLAW_DEV_STORAGE_ROOT", str(storage_root))
    monkeypatch.delenv("SHARED_VOLUME_PATH", raising=False)
    conn = db.get_connection("aming-claw")
    try:
        opened = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
        assert opened == dev_db.resolve()
        identity = db.canonical_ac_database_identity(conn)
        assert identity["world_id"] == "ac-dev"
        assert identity["genesis_sha256"] == receipt["genesis_sha256"]
    finally:
        conn.close()


def test_ac_dev_storage_resolver_rejects_namespace_alias_and_never_creates_it(tmp_path):
    """A reserved sibling namespace cannot be redirected back into stable Git."""
    from agent.governance import db
    from agent.runtime_plane import AC_DEV_STORAGE_NAMESPACE, resolve_ac_dev_storage_root

    stable = Path(db._verified_stable_binding()["shared_volume_path"])
    stable_git_root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=stable,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    )
    namespace = stable_git_root.parent / AC_DEV_STORAGE_NAMESPACE
    namespace.symlink_to(stable_git_root, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match="symlink"):
            resolve_ac_dev_storage_root(stable)
        assert namespace.is_symlink()
        assert not (stable_git_root / AC_DEV_STORAGE_NAMESPACE / "aming-claw").exists()
    finally:
        namespace.unlink()


def test_ac_dev_storage_resolver_is_outside_linked_worktree_and_common_dir(tmp_path):
    """A linked stable checkout may not place dev state in either Git domain."""
    from agent.runtime_plane import resolve_ac_dev_storage_root

    primary = tmp_path / "primary"
    primary.mkdir()
    for command in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@example.invalid"],
        ["git", "config", "user.name", "AC Test"],
    ):
        subprocess.run(command, cwd=primary, check=True, capture_output=True)
    (primary / "README").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=primary, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=primary, check=True, capture_output=True)
    linked = tmp_path / "linked" / "stable"
    linked.parent.mkdir()
    subprocess.run(
        ["git", "worktree", "add", "-b", "stable-fixture", str(linked)],
        cwd=primary, check=True, capture_output=True,
    )
    shared = linked / "shared-volume"
    shared.mkdir()
    root = resolve_ac_dev_storage_root(shared)

    common_dir = primary / ".git"
    assert root == linked.parent / ".aming-claw-dev-worlds" / "aming-claw"
    assert linked not in root.parents
    assert common_dir not in root.parents
    assert not root.exists()


def test_ac_dev_storage_rejects_alias_foreign_and_symlink_roots(tmp_path, monkeypatch):
    from agent.governance import db

    _canonical_dev_world(tmp_path)
    monkeypatch.setenv("AMING_CLAW_RUNTIME_PLANE", "dev")
    monkeypatch.setenv("AMING_CLAW_DEV_STORAGE_ROOT", str(tmp_path / "missing"))
    for project_id in ("aming_claw", "amingClaw", "other-project", "*", ""):
        with pytest.raises((ValueError, FileNotFoundError, RuntimeError)):
            db.get_connection(project_id)

    real_root = tmp_path / "real"
    real_root.mkdir()
    alias_root = tmp_path / "alias"
    alias_root.symlink_to(real_root, target_is_directory=True)
    monkeypatch.setenv("AMING_CLAW_DEV_STORAGE_ROOT", str(alias_root))
    with pytest.raises(ValueError, match="symlink"):
        db.bootstrap_dev_governance_store(
            alias_root,
            source_identity={
                "root": str(tmp_path / "source"),
                "branch": "codex/ac-dev",
                "commit": "a" * 40,
                "source_sha256": "sha256:" + "b" * 64,
            },
            process_identity={"pid": 123, "start_identity": "test-process"},
        )


def test_ac_dev_launch_receipt_requires_canonical_persistent_sibling(tmp_path, monkeypatch):
    """A direct server may only consume the one resolver-derived dev world."""
    from agent.governance import db
    from agent.runtime_plane import resolve_ac_dev_storage_root

    stable = Path(db._verified_stable_binding()["shared_volume_path"])
    monkeypatch.setenv("AMING_CLAW_SHARED_VOLUME", str(stable))
    root = resolve_ac_dev_storage_root(stable)
    root.mkdir(parents=True)
    monkeypatch.setenv("AMING_CLAW_DEV_STORAGE_ROOT", str(root))
    source = "sha256:" + "d" * 64
    receipt = db.write_dev_launch_receipt(
        root, stable_shared_volume=stable, source_sha256=source, port=40008
    )
    assert db.validate_dev_launch_receipt(root, source_sha256=source) == receipt
    with pytest.raises(ValueError, match="receipt mismatch"):
        db.validate_dev_launch_receipt(root, source_sha256="sha256:" + "e" * 64)

    foreign = tmp_path / "foreign-dev-world"
    foreign.mkdir()
    with pytest.raises(ValueError, match="canonical resolver output"):
        db.write_dev_launch_receipt(
            foreign, stable_shared_volume=stable, source_sha256=source, port=40008
        )

    # Replacing the stable directory at the same canonical path cannot be
    # concealed by a copied receipt or its self-reported pathname.
    original = tmp_path / "stable-shared-volume-original"
    stable.rename(original)
    stable.mkdir()
    before = sorted(root.rglob("*"))
    with pytest.raises((RuntimeError, ValueError, FileNotFoundError)):
        db.validate_dev_launch_receipt(root, source_sha256=source)
    assert sorted(root.rglob("*")) == before

@pytest.mark.parametrize(
    "defect",
    ["offline", "wrong-port", "pid-zero", "start", "command", "cwd", "source", "head", "loaded-commit", "plane-commit", "database", "stable-database"],
)
def test_verified_stable_binding_rejects_each_health_process_and_source_mismatch(
    monkeypatch, defect
):
    """No ingress can select authority: every receipt field is revalidated."""
    from governance import db

    root = Path(db.__file__).resolve().parents[2]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
    source = root / "agent" / "governance" / "server.py"
    digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    database = root / "shared-volume" / "codex-tasks" / "state" / "governance" / "aming-claw" / "governance.db"
    metadata = database.stat()
    database_identity = {"schema_version": "ac_stable_database_identity.v1", "device": metadata.st_dev, "inode": metadata.st_ino, "stable_relative_path_sha256": "sha256:" + hashlib.sha256(b"shared-volume/codex-tasks/state/governance/aming-claw/governance.db").hexdigest()}
    health = {
        "status": "ok", "service": "governance", "port": 40000,
        "runtime_plane": "stable", "runtime_stale": False, "pid": 4242,
        "runtime_loaded_version": head,
        "runtime_plane_identity": {"worktree_root": str(root), "branch": "codex/direct-no-pass-post-reconcile-r2", "commit": head, "stable_anchor_commit": head, "database_identity": database_identity, "stable_database_identity": database_identity, "project_allowlist": []},
        "loaded_runtime_identity": {"loaded_commit": head, "loaded_source_path": str(source), "loaded_source_sha256": digest, "worktree_source_sha256": digest},
    }
    if defect == "offline":
        monkeypatch.setattr(db, "_stable_health_request", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    else:
        if defect == "wrong-port": health["port"] = 40008
        if defect == "pid-zero": health["pid"] = 0
        if defect == "source": health["loaded_runtime_identity"]["loaded_source_sha256"] = "sha256:" + "0" * 64
        if defect == "head": health["runtime_loaded_version"] = "0" * 40
        if defect == "loaded-commit": health["loaded_runtime_identity"]["loaded_commit"] = "0" * 40
        if defect == "plane-commit": health["runtime_plane_identity"]["commit"] = "0" * 40
        if defect == "database": health["runtime_plane_identity"]["database_identity"] = {**database_identity, "inode": database_identity["inode"] + 1}
        if defect == "stable-database": health["runtime_plane_identity"]["stable_database_identity"] = {**database_identity, "inode": database_identity["inode"] + 1}
        monkeypatch.setattr(db, "_stable_health_request", lambda: health)
        command = "python -m agent.governance.server"
        cwd = str(root)
        start = "fixture-start"
        if defect == "start": start = ""
        if defect == "command": command = "python -m innocent"
        if defect == "cwd": cwd = str(root.parent)
        monkeypatch.setattr(db, "_stable_process_identity", lambda pid: (start, command, cwd))
    with pytest.raises(RuntimeError):
        (
            db.verified_stable_database_binding()
            if defect in {"database", "stable-database"}
            else db._verified_stable_binding()
        )


@pytest.mark.parametrize("replacement", ["swap", "symlink"])
def test_stable_database_binding_revalidates_before_a_dev_effect(tmp_path, replacement):
    from governance import db

    binding = db.verified_stable_database_binding()
    database = Path(binding["database_path"])
    original = database.with_name("governance-original.db")
    database.rename(original)
    if replacement == "swap":
        database.touch()
    else:
        database.symlink_to(original)
    forbidden = tmp_path / "must-not-be-created"
    with pytest.raises((RuntimeError, OSError, ValueError)):
        db._revalidate_stable_database_binding(binding)
    assert not forbidden.exists()


def test_graph_activation_connection_classification_binds_main_path_not_plane_env(
    tmp_path, monkeypatch,
):
    """Require the canonical stable main path; a real dev DB remains denied."""
    from governance import db

    stable_binding = db.verified_stable_database_binding()
    stable_database = Path(stable_binding["database_path"])
    stable_conn = sqlite3.connect(stable_database)
    try:
        stable_policy = db.classify_graph_activation_connection(stable_conn)
    finally:
        stable_conn.close()
    assert stable_policy["runtime_plane"] == "stable"
    assert stable_policy["active_graph_activation_allowed"] is True

    source = {
        "root": str(tmp_path / "source"),
        "branch": "codex/ac-dev",
        "commit": "a" * 40,
        "source_sha256": "sha256:" + "b" * 64,
    }
    dev_root, stable = _canonical_dev_world(tmp_path)
    receipt = db.bootstrap_dev_governance_store(
        dev_root,
        source_identity=source,
        process_identity={"pid": 123, "start_identity": "test-process"},
    )
    server_source = Path(db.__file__).with_name("server.py")
    db.write_dev_launch_receipt(
        dev_root,
        stable_shared_volume=stable,
        source_sha256="sha256:" + hashlib.sha256(server_source.read_bytes()).hexdigest(),
        port=40008,
    )
    monkeypatch.setenv("AMING_CLAW_RUNTIME_PLANE", "stable")
    dev_conn = sqlite3.connect(str(receipt["database_path"]))
    try:
        dev_policy = db.classify_graph_activation_connection(dev_conn)
    finally:
        dev_conn.close()
    assert dev_policy["runtime_plane"] == "dev"
    assert dev_policy["active_graph_activation_allowed"] is False

    unknown_conn = sqlite3.connect(str(tmp_path / "unbound.db"))
    try:
        unknown_policy = db.classify_graph_activation_connection(unknown_conn)
    finally:
        unknown_conn.close()
    assert unknown_policy["runtime_plane"] == "unknown"
    assert unknown_policy["active_graph_activation_allowed"] is False


def _initialized_stable_external_project(monkeypatch, project_id="external-one"):
    """Use the real initializer inside the fixed private stable boundary."""
    from agent.governance import db, project_service

    binding = db.verified_stable_database_binding()
    shared = Path(binding["shared_volume_path"])
    monkeypatch.setenv("SHARED_VOLUME_PATH", str(shared))
    monkeypatch.setenv("AMING_CLAW_RUNTIME_PLANE", "stable")
    project_service.init_project(project_id)
    return shared / "codex-tasks" / "state" / "governance", project_id


def test_stable_registered_external_activation_needs_no_active_graph_or_public_safe(monkeypatch):
    from agent.governance import db

    root, project_id = _initialized_stable_external_project(monkeypatch)
    entry = json.loads((root / "projects.json").read_text())["projects"][project_id]
    assert "active_snapshot_id" not in entry
    assert "project_config" not in entry
    with sqlite3.connect(root / project_id / "governance.db") as connection:
        before = connection.total_changes
        policy = db.classify_graph_activation_connection(connection)
        assert policy["runtime_plane"] == "stable"
        assert policy["classification_reason"] == "verified_stable_registered_external_project"
        assert policy["project_id"] == project_id
        assert policy["active_graph_activation_allowed"] is True
        assert connection.total_changes == before


@pytest.mark.parametrize("fault", [
    "unregistered", "key_mismatch", "not_initialized", "inactive",
    "noncanonical", "symlink_escape", "registry_symlink",
    "database_replaced_during_classification", "registry_replaced_during_check",
])
def test_stable_external_activation_rejects_invalid_registration_or_file_identity(
    monkeypatch, tmp_path, fault,
):
    from agent.governance import db, graph_snapshot_store

    root, project_id = _initialized_stable_external_project(monkeypatch)
    database = root / project_id / "governance.db"
    prepared = sqlite3.connect(database)
    prepared.row_factory = sqlite3.Row
    try:
        candidate = graph_snapshot_store.create_graph_snapshot(
            prepared, project_id, snapshot_id="external-candidate", commit_sha="a" * 40,
            snapshot_kind="full",
        )
        prepared.commit()
    finally:
        prepared.close()
    registry_path = root / "projects.json"
    registry = json.loads(registry_path.read_text())
    if fault == "unregistered":
        registry["projects"].clear()
    elif fault == "key_mismatch":
        registry["projects"][project_id]["project_id"] = "external-two"
    elif fault == "not_initialized":
        registry["projects"][project_id]["initialized"] = False
    elif fault == "inactive":
        registry["projects"][project_id]["status"] = "archived"
    registry_path.write_text(json.dumps(registry))
    if fault in {"noncanonical", "symlink_escape"}:
        copied = tmp_path / "outside.db"
        shutil.copy2(database, copied)
        if fault == "symlink_escape":
            database.unlink()
            database.symlink_to(copied)
        else:
            database = copied
    elif fault == "registry_symlink":
        copied = tmp_path / "outside-registry.json"
        shutil.copy2(registry_path, copied)
        registry_path.unlink()
        registry_path.symlink_to(copied)

    # Replace after the first pathname/inode observation inside classification.
    # This does not test replacement between connection open and classification,
    # native opened-file identity, or historical inode pinning.
    original_revalidate = db._revalidate_stable_database_binding
    def replace_during_check(binding):
        original_revalidate(binding)
        target = database if fault == "database_replaced_during_classification" else registry_path
        replacement = target.with_name("replacement-" + target.name)
        shutil.copy2(target, replacement)
        os.replace(replacement, target)

    if fault in {"database_replaced_during_classification", "registry_replaced_during_check"}:
        monkeypatch.setattr(db, "_revalidate_stable_database_binding", replace_during_check)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        before = connection.total_changes
        before_rows = tuple(connection.iterdump())
        policy = db.classify_graph_activation_connection(connection)
        assert policy["runtime_plane"] == "unknown", policy
        assert policy["active_graph_activation_allowed"] is False
        with pytest.raises(ValueError, match="forbidden"):
            graph_snapshot_store.activate_graph_snapshot(
                connection, project_id, candidate["snapshot_id"],
                schema_ready=True, auto_rebuild_projection=False,
            )
        assert connection.total_changes == before
        assert tuple(connection.iterdump()) == before_rows
    finally:
        connection.close()


def _cow_graph_identity_fixture(tmp_path, monkeypatch):
    from agent.governance import db
    from agent import runtime_plane

    stable = tmp_path / "stable"
    stable.mkdir()
    stable_db = stable / "stable.sqlite"
    stable_db.touch()
    root = tmp_path / "dev-root"
    database = root / db.AC_DATABASE_DEV_RELATIVE_PATH
    database.parent.mkdir(parents=True)
    conn = sqlite3.connect(database)
    root_meta = root.stat()
    predecessor = {"device": 81, "inode": 82}
    genesis = {
        "schema_version": db.AC_WORLD_GENESIS_SCHEMA,
        "world_id": db.AC_DEV_WORLD_ID,
        "project_id": db.AC_PROJECT_ID,
        "source_only": True,
        "rows_copied": 0,
        "database_identity": predecessor,
        "storage_root_identity": {
            "path": str(root), "device": root_meta.st_dev, "inode": root_meta.st_ino,
        },
    }
    raw = json.dumps(genesis, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.executemany("INSERT INTO schema_meta VALUES (?, ?)", [
        ("governance_world_id", db.AC_DEV_WORLD_ID),
        ("governance_world_genesis_json", raw),
        ("governance_world_genesis_sha256", db._world_genesis_hash(genesis)),
    ])
    conn.commit()
    identity = database.stat()
    server_source = Path(db.__file__).with_name("server.py")
    launch = {
        "schema_version": db.AC_DEV_LAUNCH_RECEIPT_SCHEMA,
        "world_id": db.AC_DEV_WORLD_ID, "project_id": db.AC_PROJECT_ID,
        "runtime_plane": "dev", "port": 40008, "background": False,
        "storage_root": str(root), "storage_device": root_meta.st_dev,
        "storage_inode": root_meta.st_ino, "stable_shared_volume": str(stable),
        "stable_shared_volume_device": stable.stat().st_dev,
        "stable_shared_volume_inode": stable.stat().st_ino,
        "stable_parent_device": stable.parent.stat().st_dev,
        "stable_parent_inode": stable.parent.stat().st_ino,
        "source_sha256": "sha256:" + hashlib.sha256(server_source.read_bytes()).hexdigest(),
    }
    (root / db.AC_DEV_LAUNCH_RECEIPT_NAME).write_text(json.dumps(launch))
    monkeypatch.setattr(
        db,
        "validate_dev_launch_receipt",
        lambda *_args, **_kwargs: dict(launch),
    )
    binding = {
        "shared_volume_path": str(stable), "database_path": str(stable_db),
        "stable_database_identity": {
            "device": stable_db.stat().st_dev, "inode": stable_db.stat().st_ino,
        },
    }
    monkeypatch.setattr(db, "verified_stable_database_binding", lambda: binding)
    monkeypatch.setattr(db, "_revalidate_stable_database_binding", lambda _binding: None)
    monkeypatch.setattr(runtime_plane, "resolve_ac_dev_storage_root", lambda _stable: root)
    receipt = {
        "genesis": {"raw_json": raw, "sha256": db._world_genesis_hash(genesis)},
        "predecessor": {"backup": predecessor},
        "successor": {"identity": {
            "path": str(database), "device": identity.st_dev, "inode": identity.st_ino,
        }},
    }
    return db, conn, receipt


def test_graph_activation_classifies_exact_validated_cow_successor(tmp_path, monkeypatch):
    db, conn, receipt = _cow_graph_identity_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "validate_dev_cow_successor_receipt", lambda _root: receipt)
    monkeypatch.setattr(db, "_verify_dev_world_schema_inventory", lambda _conn: None)
    monkeypatch.setattr(db, "_verify_current_cow_successor_source", lambda *_args, **_kwargs: None)
    identity = Path(conn.execute("PRAGMA database_list").fetchone()[2]).stat()
    monkeypatch.setattr(
        db,
        "_require_ac_dev_graph_materialization_runtime_custody",
        lambda _conn, *_args: {
            "runtime_plane": "dev",
            "world_id": "ac-dev",
            "project_id": "aming-claw",
            "port": 40008,
            "pid": os.getpid(),
            "database_device": identity.st_dev,
            "database_inode": identity.st_ino,
        },
    )
    try:
        policy = db.classify_graph_activation_connection(conn)
    finally:
        conn.close()
    assert policy["runtime_plane"] == "dev"
    assert policy["active_graph_activation_allowed"] is True
    assert policy["classification_reason"] == "verified_dev_cow_successor_receipt_history"
    assert policy["world_id"] == "ac-dev"
    assert policy["project_id"] == "aming-claw"
    assert policy["port"] == 40008
    assert policy["cow_successor_verified"] is True
    assert policy["source_checkout_verified"] is True
    assert policy["live_runtime_custody_verified"] is True


@pytest.mark.parametrize("custody_failure", ["listener", "writer_lease"])
def test_graph_activation_denies_cow_without_live_runtime_custody(
    tmp_path, monkeypatch, custody_failure,
):
    db, conn, receipt = _cow_graph_identity_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "validate_dev_cow_successor_receipt", lambda _root: receipt)
    monkeypatch.setattr(
        db, "_verify_current_cow_successor_source", lambda *_args, **_kwargs: None
    )

    def reject_custody(_conn, *_args):
        raise ValueError(f"AC dev graph materialization {custody_failure} custody mismatch")

    monkeypatch.setattr(
        db,
        "_require_ac_dev_graph_materialization_runtime_custody",
        reject_custody,
    )
    try:
        policy = db.classify_graph_activation_connection(conn)
    finally:
        conn.close()
    assert policy["runtime_plane"] == "unknown"
    assert policy["active_graph_activation_allowed"] is False


def test_graph_activation_denies_cow_with_dirty_or_mismatched_source(
    tmp_path, monkeypatch,
):
    db, conn, receipt = _cow_graph_identity_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "validate_dev_cow_successor_receipt", lambda _root: receipt)

    def reject_source(*_args, **_kwargs):
        raise ValueError("AC dev source checkout is dirty or mismatched")

    monkeypatch.setattr(db, "_verify_current_cow_successor_source", reject_source)
    try:
        policy = db.classify_graph_activation_connection(conn)
    finally:
        conn.close()
    assert policy["runtime_plane"] == "unknown"
    assert policy["active_graph_activation_allowed"] is False


def test_graph_activation_denies_cross_project_cow_before_source_or_custody(
    tmp_path, monkeypatch,
):
    db, conn, receipt = _cow_graph_identity_fixture(tmp_path, monkeypatch)
    foreign = json.loads(receipt["genesis"]["raw_json"])
    foreign["project_id"] = "foreign-project"
    receipt["genesis"] = {
        "raw_json": json.dumps(
            foreign, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ),
        "sha256": db._world_genesis_hash(foreign),
    }
    monkeypatch.setattr(db, "validate_dev_cow_successor_receipt", lambda _root: receipt)
    monkeypatch.setattr(
        db, "_verify_current_cow_successor_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cross-project world reached source authority")
        ),
    )
    monkeypatch.setattr(
        db, "_require_ac_dev_graph_materialization_runtime_custody",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cross-project world reached runtime custody")
        ),
    )

    try:
        policy = db.classify_graph_activation_connection(conn)
    finally:
        conn.close()

    assert policy["runtime_plane"] == "unknown"
    assert policy["active_graph_activation_allowed"] is False


def test_graph_materialization_verification_exposes_public_component_diagnostics():
    from agent.governance import db, graph_snapshot_store

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _install_graph_owner_for_preimage_test(db, conn, graph_snapshot_store.ensure_schema)
    conn.execute("DROP INDEX idx_pending_scope_branch")
    conn.execute("DROP INDEX idx_pending_scope_status")

    with pytest.raises(db.DevRuntimeSchemaVerificationError) as rejected:
        db.verify_graph_materialization_schema(conn)

    details = rejected.value.details
    assert details["owner_states"]["graph_snapshot_store"] == (
        "pending_scope_index_predecessor"
    )
    assert set(details["planned_objects"]) == {
        "idx_pending_scope_branch",
        "idx_pending_scope_status",
    }
    assert details["component_diagnostics"]["graph_snapshot_store"] == {
        "owner_state": "pending_scope_index_predecessor",
        "planned_objects": [
            "idx_pending_scope_branch",
            "idx_pending_scope_status",
        ],
        "public_safe": True,
        "status": "incompatible",
    }
    assert details["writes_performed"] is False
    conn.close()


@pytest.mark.parametrize("row_factory", [None, sqlite3.Row])
def test_cow_quick_check_accepts_exact_tuple_or_row_shape(row_factory):
    from agent.governance import db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = row_factory
    try:
        assert db._quick_check_returns_literal_ok(conn) is True
    finally:
        conn.close()


@pytest.mark.parametrize(
    "row",
    [
        None,
        (),
        ("ok", "extra"),
        (1,),
        (b"ok",),
        (type("StringSubclass", (str,), {})("ok"),),
        ("OK",),
        ("not ok",),
    ],
)
def test_cow_quick_check_rejects_missing_cardinality_type_or_value(row):
    from agent.governance import db

    class Cursor:
        def fetchone(self):
            return row

    class Connection:
        def execute(self, statement):
            assert statement == "PRAGMA quick_check"
            return Cursor()

    assert db._quick_check_returns_literal_ok(Connection()) is False


def test_cow_quick_check_rejects_malformed_row_shape():
    from agent.governance import db

    class Cursor:
        def fetchone(self):
            return object()

    class Connection:
        def execute(self, statement):
            assert statement == "PRAGMA quick_check"
            return Cursor()

    assert db._quick_check_returns_literal_ok(Connection()) is False


def test_graph_activation_rejects_failed_cow_quick_check(tmp_path, monkeypatch):
    from agent.governance import db

    db, conn, receipt = _cow_graph_identity_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "validate_dev_cow_successor_receipt", lambda _root: receipt)
    monkeypatch.setattr(
        db, "_verify_current_cow_successor_source", lambda *_args, **_kwargs: None
    )

    def fail_quick_check(_conn):
        raise sqlite3.DatabaseError("malformed quick_check result")

    monkeypatch.setattr(db, "_quick_check_returns_literal_ok", fail_quick_check)
    try:
        policy = db.classify_graph_activation_connection(conn)
    finally:
        conn.close()
    assert policy["runtime_plane"] == "unknown"
    assert policy["classification_reason"] == "dev_database_binding_unverified"


def test_graph_materialization_admits_valid_cow_without_parallel_runtime_inventory(
    tmp_path, monkeypatch,
):
    """Optional parallel runtime absence is not COW physical identity drift."""
    db, conn, receipt = _cow_graph_identity_fixture(tmp_path, monkeypatch)
    db._ensure_schema(conn)
    optional_tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' "
            "AND (name LIKE 'parallel_branch_%' OR name LIKE 'graph_%')"
        )
    } | {
        row[1]
        for row in db._graph_materialization_canonical_inventory()
        if row[0] == "table"
    }
    assert len({
        name for name in optional_tables if name.startswith("parallel_branch_")
    }) == 12
    for table in sorted(optional_tables):
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'parallel_branch_%'"
    ).fetchone() == (0,)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'graph_%'"
    ).fetchone() == (0,)
    # Match get_connection/_connect_existing: handler connections use Row and
    # retain the dev schema authorizer until admission temporarily owns DDL.
    conn.row_factory = sqlite3.Row
    conn.set_authorizer(db._dev_schema_authorizer)

    monkeypatch.setenv("AMING_CLAW_RUNTIME_PLANE", "dev")
    monkeypatch.setattr(db, "validate_dev_cow_successor_receipt", lambda _root: receipt)
    monkeypatch.setattr(
        db, "_verify_current_cow_successor_source", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        db,
        "_require_ac_dev_graph_materialization_runtime_custody",
        lambda _conn, *_args: {
            "runtime_plane": "dev",
            "world_id": "ac-dev",
            "project_id": "aming-claw",
            "host": "127.0.0.1",
            "port": 40008,
            "pid": os.getpid(),
            "database_device": Path(
                _conn.execute("PRAGMA database_list").fetchone()[2]
            ).stat().st_dev,
            "database_inode": Path(
                _conn.execute("PRAGMA database_list").fetchone()[2]
            ).stat().st_ino,
        },
    )
    monkeypatch.setattr(
        db,
        "canonical_ac_database_identity",
        lambda _conn: {"world_id": "ac-dev", "project_id": "aming-claw"},
    )
    try:
        result = db.admit_ac_dev_graph_materialization_schema(
            conn, project_id="aming-claw"
        )
        db.verify_graph_materialization_schema(conn)
        assert result["runtime_plane"] == "dev"
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'parallel_branch_%'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("drift", ["inode", "history_gap", "source_descendant"])
def test_graph_materialization_rejects_unverified_cow_before_write(
    tmp_path, monkeypatch, drift,
):
    db, conn, receipt = _cow_graph_identity_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("AMING_CLAW_RUNTIME_PLANE", "dev")
    monkeypatch.setattr(
        db, "_require_ac_dev_graph_materialization_runtime_custody",
        lambda _conn, *_args: {"host": "127.0.0.1", "port": 40008},
    )
    monkeypatch.setattr(
        db, "canonical_ac_database_identity",
        lambda _conn: {"world_id": "ac-dev", "project_id": "aming-claw"},
    )
    monkeypatch.setattr(db, "_verify_dev_world_schema_inventory", lambda _conn: None)
    monkeypatch.setattr(db, "_verify_current_cow_successor_source", lambda *_args, **_kwargs: None)
    if drift == "inode":
        receipt["successor"]["identity"]["inode"] += 1
        monkeypatch.setattr(db, "validate_dev_cow_successor_receipt", lambda _root: receipt)
    elif drift == "history_gap":
        def reject_history(_root):
            raise ValueError("historical artifact chain mismatch")
        monkeypatch.setattr(db, "validate_dev_cow_successor_receipt", reject_history)
    else:
        monkeypatch.setattr(db, "validate_dev_cow_successor_receipt", lambda _root: receipt)
        def reject_source(*_args, **_kwargs):
            raise ValueError("current source descendant authority mismatch")
        monkeypatch.setattr(db, "_verify_current_cow_successor_source", reject_source)
    before_changes = conn.total_changes
    before_inventory = db._graph_materialization_inventory(conn)
    with pytest.raises(ValueError, match="identity is not admitted"):
        db.admit_ac_dev_graph_materialization_schema(conn, project_id="aming-claw")
    assert conn.total_changes == before_changes
    assert db._graph_materialization_inventory(conn) == before_inventory
    conn.close()


@pytest.mark.parametrize("plane", ["stable", "generic"])
@pytest.mark.parametrize("project_id", ["aming-claw", "aming_claw", "amingClaw"])
def test_v27_central_resolver_rejects_ac_before_mkdir(
    tmp_path, monkeypatch, plane, project_id
):
    from agent.governance import db

    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("AMING_CLAW_RUNTIME_PLANE", plane)
    monkeypatch.setenv("SHARED_VOLUME_PATH", str(shared))
    forbidden = shared / "codex-tasks" / "state" / "governance"

    with pytest.raises(ValueError, match="stable|generic|AC project"):
        db._project_db_path(project_id)
    assert not forbidden.exists()


@pytest.mark.parametrize(
    "defect",
    ["existing-empty-root", "unknown-table", "wal", "shared-root", "hardlink-copy"],
)
def test_v27_bootstrap_rejects_nonfresh_or_preloaded_world(
    tmp_path, monkeypatch, defect
):
    from agent.governance import db

    source_root, commit = _dev_source_repo(tmp_path)
    source = {
        "root": str(source_root.resolve()),
        "branch": "codex/ac-dev",
        "commit": commit,
        "source_sha256": "sha256:" + "e" * 64,
    }
    process = {"pid": os.getpid(), "start_identity": "v27-bootstrap"}
    storage_root, stable = _canonical_dev_world(tmp_path)

    if defect == "existing-empty-root":
        storage_root.mkdir(parents=True)
    else:
        first = db.bootstrap_dev_governance_store(
            storage_root,
            source_identity=source,
            process_identity=process,
        )
        database = Path(first["database_path"])
        _admit_existing_dev_world(storage_root, stable)
        if defect == "unknown-table":
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE injected_state (value TEXT)")
                connection.execute("INSERT INTO injected_state VALUES ('copied')")
                connection.commit()
        elif defect == "wal":
            Path(str(database) + "-wal").write_bytes(b"reused")
        elif defect == "shared-root":
            monkeypatch.setenv("SHARED_VOLUME_PATH", str(storage_root))
        else:
            copied_root = tmp_path / "dev-world-copy"
            copied_database = copied_root / db.AC_DATABASE_DEV_RELATIVE_PATH
            copied_database.parent.mkdir(parents=True)
            os.link(database, copied_database)
            storage_root = copied_root

    with pytest.raises(
        (RuntimeError, ValueError), match="fresh|unknown|WAL|shared|storage"
    ):
        db.bootstrap_dev_governance_store(
            storage_root,
            source_identity=source,
            process_identity=process,
        )


def test_v27_writer_lease_blocks_second_process_before_database_open(tmp_path, monkeypatch):
    from agent.governance import db
    from agent.runtime_plane import resolve_ac_dev_storage_root

    source_root, commit = _dev_source_repo(tmp_path)
    source = {
        "root": str(source_root.resolve()),
        "branch": "codex/ac-dev",
        "commit": commit,
        "source_sha256": "sha256:" + "f" * 64,
    }
    stable_shared = Path(db._verified_stable_binding()["shared_volume_path"])
    storage_root = resolve_ac_dev_storage_root(stable_shared)
    monkeypatch.setenv("AMING_CLAW_SHARED_VOLUME", str(stable_shared))
    monkeypatch.setenv("AMING_CLAW_DEV_STORAGE_ROOT", str(storage_root))
    first = db.bootstrap_dev_governance_store(
        storage_root,
        source_identity=source,
        process_identity={"pid": os.getpid(), "start_identity": "v27-owner"},
    )
    database = Path(first["database_path"])
    server_source = Path(db.__file__).with_name("server.py")
    source_sha256 = "sha256:" + hashlib.sha256(server_source.read_bytes()).hexdigest()
    db.write_dev_launch_receipt(
        storage_root,
        stable_shared_volume=stable_shared,
        source_sha256=source_sha256,
        port=40008,
    )
    before = (database.stat().st_dev, database.stat().st_ino, database.stat().st_mtime_ns)
    code = (
        "import os,sys; "
        "os.environ['AMING_CLAW_RUNTIME_PLANE']='dev'; "
        "os.environ['AMING_CLAW_DEV_STORAGE_ROOT']=sys.argv[1]; "
        "os.environ['AMING_CLAW_SHARED_VOLUME']=sys.argv[2]; "
        "from agent.governance import server; server.main()"
    )
    contender = subprocess.run(
        [sys.executable, "-c", code, str(storage_root), str(stable_shared)],
        cwd=Path(db.__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert contender.returncode != 0
    # A separate process never reaches the database; its fixed private
    # boundary is intentionally absent rather than selecting test authority.
    assert (database.stat().st_dev, database.stat().st_ino, database.stat().st_mtime_ns) == before


def _dev_source_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "source"
    root.mkdir()
    subprocess.run(
        ["git", "init", "-b", "codex/ac-dev"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "AC Test"],
        cwd=root,
        check=True,
    )
    (root / "source.txt").write_text("A\n", encoding="utf-8")
    cli_source = root / "agent" / "cli.py"
    cli_source.parent.mkdir()
    cli_source.write_text("# canonical CLI source producer\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.txt", "agent/cli.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "A"], cwd=root, check=True, capture_output=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return root, commit


def _advance_dev_source(root: Path, value: str) -> str:
    (root / "source.txt").write_text(value + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.txt"], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-m", value],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _pointer_only_dev_worktrees(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """Materialize the exact OLD-detached/NEW-canonical handoff state."""
    old, commit_a = _dev_source_repo(tmp_path)
    builder = tmp_path / "builder"
    subprocess.run(["git", "worktree", "add", "--detach", str(builder), commit_a], cwd=old, check=True, capture_output=True)
    commit_b = _advance_dev_source(builder, "B")
    subprocess.run(["git", "update-ref", "refs/heads/codex/ac-dev", commit_b, commit_a], cwd=old, check=True)
    subprocess.run(["git", "update-ref", "--no-deref", "HEAD", commit_a, commit_b], cwd=old, check=True)
    new = tmp_path / "successor"
    subprocess.run(["git", "worktree", "add", str(new), "codex/ac-dev"], cwd=old, check=True, capture_output=True)
    return old, new, commit_a, commit_b


def test_ac_dev_pointer_only_physical_root_continuity_accepts_exact_registered_handoff(tmp_path, monkeypatch):
    from agent.governance import db

    old, new, commit_a, commit_b = _pointer_only_dev_worktrees(tmp_path)
    dev_storage = tmp_path / "dev-storage"; dev_storage.mkdir()
    monkeypatch.setenv("AMING_CLAW_DEV_STORAGE_ROOT", str(dev_storage))
    previous = {"root": str(old.resolve()), "branch": "codex/ac-dev", "commit": commit_a,
                "source_sha256": "sha256:" + "a" * 64}
    candidate = {**previous, "root": str(new.resolve()), "commit": commit_b,
                 "source_sha256": "sha256:" + "b" * 64}
    db._verify_dev_source_upgrade(previous, candidate)
    assert subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=old).returncode != 0
    assert subprocess.run(["git", "branch", "--show-current"], cwd=new, capture_output=True, text=True, check=True).stdout.strip() == "codex/ac-dev"


@pytest.mark.parametrize("defect", ["old_attached", "old_dirty", "new_detached", "different_common_dir"])
def test_ac_dev_pointer_only_physical_root_mismatch_matrix_rejects_without_effect(tmp_path, monkeypatch, defect):
    from agent.governance import db

    old, new, commit_a, commit_b = _pointer_only_dev_worktrees(tmp_path)
    dev_storage = tmp_path / "dev-storage"; dev_storage.mkdir()
    monkeypatch.setenv("AMING_CLAW_DEV_STORAGE_ROOT", str(dev_storage))
    previous = {"root": str(old.resolve()), "branch": "codex/ac-dev", "commit": commit_a,
                "source_sha256": "sha256:" + "a" * 64}
    candidate = {**previous, "root": str(new.resolve()), "commit": commit_b,
                 "source_sha256": "sha256:" + "b" * 64}
    if defect == "old_attached":
        subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/fixture-old"], cwd=old, check=True)
        subprocess.run(["git", "update-ref", "refs/heads/fixture-old", commit_a], cwd=old, check=True)
    elif defect == "old_dirty":
        (old / "untracked.txt").write_text("foreign\n", encoding="utf-8")
    elif defect == "new_detached":
        subprocess.run(["git", "update-ref", "--no-deref", "HEAD", commit_b, commit_b], cwd=new, check=True)
    else:
        foreign_parent = tmp_path / "foreign"
        foreign_parent.mkdir()
        foreign, foreign_commit = _dev_source_repo(foreign_parent)
        subprocess.run(["git", "branch", "-M", "codex/ac-dev"], cwd=foreign, check=True)
        candidate["root"], candidate["commit"] = str(foreign.resolve()), foreign_commit
    before = subprocess.run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=new, capture_output=True, text=True, check=True).stdout
    with pytest.raises(ValueError):
        db._verify_dev_source_upgrade(previous, candidate)
    after = subprocess.run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=new, capture_output=True, text=True, check=True).stdout
    assert after == before


def _admit_existing_dev_world(storage_root: Path, stable: Path) -> None:
    """Give restart/adoption fixtures the same canonical receipt as CLI start."""
    from agent.governance import db

    server_source = Path(db.__file__).with_name("server.py")
    db.write_dev_launch_receipt(
        storage_root,
        stable_shared_volume=stable,
        source_sha256="sha256:" + hashlib.sha256(server_source.read_bytes()).hexdigest(),
        port=40008,
    )


def test_authority_projection_inventory_registry_matches_source_owned_schema():
    from agent.governance import db

    inventory = db.authority_projection_schema_inventory()
    assert len(inventory["inventory"]) == db.AC_AUTHORITY_SCHEMA_INVENTORY_COUNT == 308
    assert inventory["sha256"] == db.AC_AUTHORITY_SCHEMA_INVENTORY_SHA256


def test_ac_dev_source_tip_cas_upgrade_is_descendant_and_genesis_immutable(tmp_path):
    from agent.governance import db

    root, commit_a = _dev_source_repo(tmp_path)
    storage_root, stable = _canonical_dev_world(tmp_path)
    source_a = {
        "root": str(root.resolve()),
        "branch": "codex/ac-dev",
        "commit": commit_a,
        "source_sha256": "sha256:" + "a" * 64,
    }
    process_a = {"pid": 101, "start_identity": "process-a"}
    first = db.bootstrap_dev_governance_store(
        storage_root,
        source_identity=source_a,
        process_identity=process_a,
    )
    _admit_existing_dev_world(storage_root, stable)
    commit_b = _advance_dev_source(root, "B")
    source_b = {
        **source_a,
        "commit": commit_b,
        "source_sha256": "sha256:" + "b" * 64,
    }
    process_b = {"pid": 202, "start_identity": "process-b"}
    upgraded = db.bootstrap_dev_governance_store(
        storage_root,
        source_identity=source_b,
        process_identity=process_b,
        expected_source_tip_sha256=first["source_tip_sha256"],
        expected_previous_process_identity=process_a,
        expected_database_identity=first["database_identity"],
    )
    assert upgraded["source_upgraded"] is True
    assert upgraded["source_tip_revision"] == 2
    assert upgraded["source_tip_identity"] == source_b
    assert upgraded["genesis_sha256"] == first["genesis_sha256"]
    assert upgraded["database_identity"] == first["database_identity"]

    replay = db.bootstrap_dev_governance_store(
        storage_root,
        source_identity=source_b,
        process_identity=process_b,
        expected_source_tip_sha256=upgraded["source_tip_sha256"],
        expected_previous_process_identity=process_b,
        expected_database_identity=first["database_identity"],
    )
    assert replay["source_upgraded"] is False
    assert replay["source_tip_revision"] == 2

    commit_c = _advance_dev_source(root, "C")
    source_c = {
        **source_b,
        "commit": commit_c,
        "source_sha256": "sha256:" + "c" * 64,
    }
    with pytest.raises(ValueError, match="source tip CAS"):
        db.bootstrap_dev_governance_store(
            storage_root,
            source_identity=source_c,
            process_identity={"pid": 303, "start_identity": "process-c"},
            expected_source_tip_sha256=first["source_tip_sha256"],
            expected_previous_process_identity=process_b,
            expected_database_identity=first["database_identity"],
        )
    # A restart cannot lie about the checked-out source after the worktree
    # moves again, even when the stored source tip itself is still B.
    with pytest.raises(ValueError, match="HEAD mismatch"):
        db.bootstrap_dev_governance_store(
            storage_root,
            source_identity=source_b,
            process_identity=process_b,
        )


def test_ac_dev_cow_successor_replaces_only_genesis_physical_identity(tmp_path, monkeypatch):
    from agent.governance import db

    source_root, commit = _dev_source_repo(tmp_path)
    storage_root, stable = _canonical_dev_world(tmp_path)
    source = {"root": str(source_root.resolve()), "branch": "codex/ac-dev",
              "commit": commit, "source_sha256": "sha256:" + "c" * 64}
    first = db.bootstrap_dev_governance_store(
        storage_root, source_identity=source,
        process_identity={"pid": 101, "start_identity": "cow-before"},
    )
    _admit_existing_dev_world(storage_root, stable)
    database = Path(first["database_path"])
    with sqlite3.connect(database) as connection:
        genesis_before = connection.execute(
            "SELECT value FROM schema_meta WHERE key='governance_world_genesis_json'"
        ).fetchone()[0]
    db.release_dev_runtime_writer_lease(storage_root)
    old = database.stat(follow_symlinks=False)
    replacement = database.with_suffix(".cow")
    replacement.write_bytes(database.read_bytes())
    os.replace(replacement, database)
    with sqlite3.connect(database) as connection:
        db.ensure_backlog_read_schema(connection)
        protected_inventory = db.backlog_read_schema_protected_inventory(connection)
    new = database.stat(follow_symlinks=False)
    receipt = {
        "predecessor": {"backup": {"device": old.st_dev, "inode": old.st_ino}},
        "successor": {
            "identity": {"device": new.st_dev, "inode": new.st_ino},
            "protected_inventory": protected_inventory,
        },
    }
    monkeypatch.setattr(db, "validate_dev_cow_successor_receipt", lambda _root: receipt)
    # This unit isolates the downstream genesis/physical-identity rule from
    # the completed-generation ingress preflight, which is covered with real
    # content-addressed receipt chains below.
    monkeypatch.setattr(
        db, "_validate_dev_cow_completed_basic_restart",
        lambda *_args, **_kwargs: None,
    )
    replay = db.bootstrap_dev_governance_store(
        storage_root, source_identity=source,
        process_identity={"pid": 202, "start_identity": "cow-after"},
    )
    assert replay["database_identity"]["inode"] == new.st_ino
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='governance_world_genesis_json'"
        ).fetchone()[0] == genesis_before

    receipt["successor"]["identity"]["inode"] += 1
    before = database.read_bytes()
    with pytest.raises(ValueError, match="COW successor identity"):
        db.bootstrap_dev_governance_store(
            storage_root, source_identity=source,
            process_identity={"pid": 303, "start_identity": "wrong-successor"},
        )
    assert database.read_bytes() == before


def test_ac_dev_no_successor_retains_genesis_source_schema_hash_gate(tmp_path):
    from agent.governance import db

    source_root, commit = _dev_source_repo(tmp_path)
    storage_root, stable = _canonical_dev_world(tmp_path)
    source = {"root": str(source_root.resolve()), "branch": "codex/ac-dev",
              "commit": commit, "source_sha256": "sha256:" + "c" * 64}
    first = db.bootstrap_dev_governance_store(
        storage_root, source_identity=source,
        process_identity={"pid": 101, "start_identity": "legacy-before"},
    )
    _admit_existing_dev_world(storage_root, stable)
    db.release_dev_runtime_writer_lease(storage_root)
    with sqlite3.connect(first["database_path"]) as connection:
        db.ensure_backlog_read_schema(connection)
    with pytest.raises(ValueError, match="source schema contract changed"):
        db.bootstrap_dev_governance_store(
            storage_root, source_identity=source,
            process_identity={"pid": 202, "start_identity": "legacy-after"},
        )


def test_ac_dev_cow_successor_creator_is_content_addressed_and_replays(tmp_path, monkeypatch):
    from agent.governance import db

    root = tmp_path / "dev"
    database = root / db.AC_DATABASE_DEV_RELATIVE_PATH
    backup = root / "archive" / "operator-exception-backups" / "old.sqlite"
    operator_dir = root / "archive" / "operator-exceptions"
    linked_dir = root / "archive" / "schema-admission"
    operator = operator_dir / "placeholder"
    linked = linked_dir / "placeholder"
    adoption_dir = root / "archive" / "canonical-legacy-postimage-adoption"
    for path in (database, backup, operator, linked):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{}")
    genesis = {"schema_version": db.AC_WORLD_GENESIS_SCHEMA, "world_id": db.AC_DEV_WORLD_ID,
               "project_id": db.AC_PROJECT_ID, "source_only": True, "rows_copied": 0,
               "database_identity": {"device": 7, "inode": 11}}
    genesis_raw = json.dumps(genesis, sort_keys=True, separators=(",", ":"))
    genesis_sha = db._world_genesis_hash(genesis)
    successor_observation = {
        "identity": {"path": str(database), "device": 7, "inode": 22,
                     "size": 2, "nlink": 1, "sha256": "sha256:" + "b" * 64},
        "quick_check": "ok", "row_count": 3603, "status_counts": {"OPEN": 3603},
        "managed_inventory": {"inventory": [], "sha256": "sha256:" + hashlib.sha256(b"[]").hexdigest()}, "managed_inventory_drift": [],
        "protected_inventory": {"inventory": [], "sha256": "sha256:" + hashlib.sha256(b"[]").hexdigest()},
        "protected_projection": {"schema_meta": "sha256:meta"},
        "backlog_projection_sha256": "sha256:" + "7" * 64,
        "governance_world_id": db.AC_DEV_WORLD_ID,
        "source_schema": {"required_tables": ["backlog_bugs"], "inventory": [],
                          "sha256": "sha256:" + hashlib.sha256(b"[]").hexdigest()},
        "genesis_json": genesis_raw, "genesis_sha256": genesis_sha,
    }
    backup_observation = {**successor_observation,
                          "identity": {"path": str(backup), "device": 7, "inode": 11,
                                       "size": 2, "nlink": 1, "sha256": "sha256:" + "a" * 64},
                          "row_count": 0}
    operator_payload = {"schema_version": "ac_dev_operator_exception_cow_import.v1",
                        "qa_pass": False, "release_authority": False, "rows": 3603,
                        "decisions": ["decision"], "backup_sha256": "sha256:" + "a" * 64,
                        "target_sha256_before": "sha256:" + "a" * 64,
                        "target_sha256_after": "sha256:" + "b" * 64}
    operator_raw = json.dumps(operator_payload, sort_keys=True, separators=(",", ":")).encode()
    operator.unlink()
    operator = operator_dir / f"cow-import.{hashlib.sha256(operator_raw).hexdigest()}.json"
    operator.write_bytes(operator_raw)
    linked_payload = {"schema_version": "ac_dev_offline_schema_admission.v3",
                      "stage": "completed",
                      "database_identity": {"device": 7, "inode": 11}}
    linked_raw = json.dumps(linked_payload, sort_keys=True, separators=(",", ":")).encode()
    linked.unlink()
    linked = linked_dir / f"{hashlib.sha256(linked_raw).hexdigest()}.json"
    linked.write_bytes(linked_raw)
    linked_sha = "sha256:" + hashlib.sha256(linked.read_bytes()).hexdigest()
    linked.with_suffix(".sha256").write_text(f"{linked_sha}  {linked.name}\n")
    adoption_payload = {"schema_version": "ac_dev_canonical_legacy_postimage_adoption.v1",
                        "stage": "completed", "project_id": "aming-claw", "port": 40008,
                        "linked_v3_receipt": str(linked),
                        "linked_v3_receipt_sha256": linked_sha}
    adoption_raw = json.dumps(adoption_payload, sort_keys=True, separators=(",", ":")).encode()
    adoption = adoption_dir / f"adoption.{hashlib.sha256(adoption_raw).hexdigest()}.json"
    adoption_dir.mkdir(parents=True)
    adoption.write_bytes(adoption_raw)

    monkeypatch.setattr(db, "_cow_database_observation",
                        lambda path, **_kwargs: successor_observation if path == database else backup_observation)
    monkeypatch.setattr(db, "_cow_regular_identity",
                        lambda path, **_kwargs: (
                            {"path": str(backup), "device": 7, "inode": 11, "size": 2,
                             "nlink": 1, "sha256": "sha256:" + "a" * 64}
                            if path == backup else
                            {"schema_version": "ac_stable_database_identity.v1", "device": 8,
                             "inode": 33, "stable_relative_path_sha256": "sha256:" + "d" * 64}))
    stable_db = tmp_path / "stable.db"
    stable_db.write_bytes(b"stable")
    stable_identity = {"schema_version": "ac_stable_database_identity.v1",
                       "device": stable_db.stat().st_dev, "inode": stable_db.stat().st_ino,
                       "stable_relative_path_sha256": "sha256:" + "d" * 64}
    monkeypatch.setattr(db, "verified_stable_database_binding", lambda: {
        "database_path": str(stable_db), "stable_database_identity": stable_identity})
    monkeypatch.setattr(db, "_revalidate_stable_database_binding", lambda _binding: None)
    monkeypatch.setattr(db, "_default_cutover_listener_probe",
                        lambda port: {"port": port, "listening": False, "pid": 0})
    legacy_archive = root / db.AC_DEV_COW_SUCCESSOR_ARCHIVE
    legacy_archive.mkdir(parents=True)
    legacy_raw = b'{"schema_version":"ac_dev_cow_database_successor.v1"}'
    legacy = legacy_archive / f"successor.{hashlib.sha256(legacy_raw).hexdigest()}.json"
    legacy.write_bytes(legacy_raw)
    first = db.create_dev_cow_successor_receipt(
        root, operator_receipt=operator, predecessor_backup=backup,
        linked_v3_receipt=linked,
    )
    receipt = Path(first["receipt"])
    raw = receipt.read_bytes()
    expected_payload = json.loads(raw)
    monkeypatch.setattr(
        db,
        "_reconstruct_dev_cow_successor_payload",
        lambda _root, _receipt: expected_payload,
    )
    assert receipt.name == f"{db.AC_DEV_COW_SUCCESSOR_PREFIX}.{hashlib.sha256(raw).hexdigest()}.json"
    assert legacy.read_bytes() == legacy_raw
    second = db.create_dev_cow_successor_receipt(
        root, operator_receipt=operator, predecessor_backup=backup,
        linked_v3_receipt=linked,
    )
    assert second["status"] == "already_created"
    assert receipt.read_bytes() == raw

    wrong_schema = json.loads(raw)
    wrong_schema["successor"]["source_schema"]["sha256"] = "sha256:" + "8" * 64
    wrong_raw = json.dumps(wrong_schema, sort_keys=True, separators=(",", ":")).encode()
    receipt.unlink()
    wrong_receipt = receipt.with_name(
        f"{db.AC_DEV_COW_SUCCESSOR_PREFIX}.{hashlib.sha256(wrong_raw).hexdigest()}.json"
    )
    wrong_receipt.write_bytes(wrong_raw)
    with pytest.raises(ValueError, match="reconstructed issuance"):
        db.validate_dev_cow_successor_receipt(root)
    wrong_receipt.unlink(); receipt.write_bytes(raw)

    ambiguous = receipt.with_name(db.AC_DEV_COW_SUCCESSOR_PREFIX + "." + "f" * 64 + ".json")
    ambiguous.write_bytes(raw)
    with pytest.raises(ValueError, match="missing or ambiguous"):
        db.validate_dev_cow_successor_receipt(root)
    ambiguous.unlink()
    tampered = json.loads(raw)
    tampered["successor"]["row_count"] = 3604
    tampered_raw = json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode()
    receipt.unlink()
    tampered_receipt = receipt.with_name(
        f"{db.AC_DEV_COW_SUCCESSOR_PREFIX}.{hashlib.sha256(tampered_raw).hexdigest()}.json"
    )
    tampered_receipt.write_bytes(tampered_raw)
    with pytest.raises(ValueError, match="reconstructed issuance"):
        db.validate_dev_cow_successor_receipt(root)
    assert tampered_receipt.read_bytes() == tampered_raw


def test_ac_dev_cow_successor_validator_rejects_missing(tmp_path):
    from agent.governance import db

    root = tmp_path / "dev"
    root.mkdir()
    with pytest.raises(ValueError, match="missing or ambiguous"):
        db.validate_dev_cow_successor_receipt(root)


def test_canonical_preimage_selects_cow_bridge_only_for_replaced_inode(
    tmp_path, monkeypatch,
):
    from agent.governance import db
    import agent.runtime_plane as runtime_plane

    stable = tmp_path / "stable"; stable.mkdir()
    root = tmp_path / "dev"; root.mkdir()
    database = root / db.AC_DATABASE_DEV_RELATIVE_PATH
    database.parent.mkdir(parents=True); database.write_bytes(b"successor")
    linked = root / "linked.json"
    linked.write_text(json.dumps({"database_identity": {
        "device": database.stat().st_dev, "inode": database.stat().st_ino + 1,
    }}))
    binding = {"shared_volume_path": str(stable)}
    monkeypatch.setenv(db.AC_DEV_STORAGE_ROOT_ENV, str(root))
    monkeypatch.setenv(db.AC_STABLE_SHARED_VOLUME_ENV, str(stable))
    monkeypatch.setattr(db, "verified_stable_database_binding", lambda: binding)
    monkeypatch.setattr(db, "_revalidate_stable_database_binding", lambda _value: None)
    monkeypatch.setattr(runtime_plane, "resolve_ac_dev_storage_root", lambda _stable: root)
    calls = []
    monkeypatch.setattr(
        db, "_select_dev_cow_generation_phase",
        lambda *_args, **_kwargs: db._DevCowGenerationPhase.FIRST_ISSUANCE,
    )
    monkeypatch.setattr(db, "validate_dev_cow_successor_preimage",
                        lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(db, "_validated_canonical_legacy_postimage_adoption",
                        lambda *_args, **_kwargs: pytest.fail("legacy path must remain disjoint"))
    assert db._dev_storage_root(
        isolated_receipt=linked, source_identity={"commit": "a" * 40},
        allow_postimage=True,
    ) == root
    assert len(calls) == 1


def test_cow_receipt_reconstruction_keeps_issuance_schema_after_source_expands(
    tmp_path, monkeypatch,
):
    from agent.governance import db

    root, _database, backup, operator, linked = _real_cow_successor_cli_fixture(
        tmp_path, monkeypatch
    )
    created = db.create_dev_cow_successor_receipt(
        root, operator_receipt=operator, predecessor_backup=backup,
        linked_v3_receipt=linked,
    )
    _write_historical_v1_from_v2(created["receipt"])
    immutable = json.loads(Path(created["receipt"]).read_text())
    original_contract = db._source_schema_table_contract
    required, allowed, objects = original_contract()
    monkeypatch.setattr(
        db, "_source_schema_table_contract",
        lambda: (
            required | {"parallel_branch_runtime_future"},
            allowed | {"parallel_branch_runtime_future"},
            objects | {(
                "table", "parallel_branch_runtime_future",
                "parallel_branch_runtime_future",
            )},
        ),
    )
    monkeypatch.setenv("AMING_CLAW_RUNTIME_PLANE", "dev")
    monkeypatch.setattr(
        db,
        "_ensure_schema",
        lambda _conn: (_ for _ in ()).throw(
            AssertionError("receipt reconstruction must not enter runtime schema setup")
        ),
    )
    reconstructed = db._reconstruct_dev_cow_successor_payload(root, immutable)
    assert reconstructed == immutable
    assert db.validate_dev_cow_successor_receipt(root) == immutable


@pytest.mark.parametrize("mutation", [
    "empty", "missing", "extra", "bool_pid", "zero_pid", "bad_start",
])
def test_cow_bootstrap_process_custody_is_closed_and_history_bound(
    tmp_path, mutation,
):
    from agent.governance import db

    historical = {"pid": 41, "start_identity": "pid:41:cli-bootstrap"}
    process = dict(historical)
    if mutation == "empty": process = {}
    elif mutation == "missing": process.pop("start_identity")
    elif mutation == "extra": process["project_id"] = "aming-claw"
    elif mutation == "bool_pid": process["pid"] = True
    elif mutation == "zero_pid": process.update(pid=0, start_identity="pid:0:cli-bootstrap")
    elif mutation == "bad_start": process["start_identity"] = "pid:42:cli-bootstrap"
    with pytest.raises(ValueError, match="process custody"):
        db._validate_dev_current_process_custody(
            process, root=tmp_path, candidate_source={},
            historical_process=historical,
        )


def test_cow_bootstrap_process_custody_accepts_exact_historical_observation(tmp_path):
    from agent.governance import db

    process = {"pid": 41, "start_identity": "pid:41:cli-bootstrap"}
    db._validate_dev_current_process_custody(
        process, root=tmp_path, candidate_source={}, historical_process=process,
    )


def _current_cow_historical_tip_fixture(tmp_path, monkeypatch):
    from agent.governance import db

    source_repository = tmp_path / "source-repository"
    source_repository.mkdir()
    source_root, _anchor_commit = _dev_source_repo(source_repository)
    server_source = source_root / "agent" / "governance" / "server.py"
    database_source = source_root / "agent" / "governance" / "db.py"
    server_source.parent.mkdir(parents=True)
    server_source.write_text("# canonical server source\n", encoding="utf-8")
    database_source.write_text("# canonical database source\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "agent/governance/server.py", "agent/governance/db.py"],
        cwd=source_root, check=True,
    )
    subprocess.run(
        ["git", "commit", "--amend", "--no-edit"], cwd=source_root,
        check=True, capture_output=True,
    )
    anchor_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source_root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    cli_sha = "sha256:" + hashlib.sha256(
        (source_root / "agent" / "cli.py").read_bytes()
    ).hexdigest()
    anchor = {
        "root": str(source_root.resolve()), "branch": "codex/ac-dev",
        "commit": anchor_commit, "source_sha256": cli_sha,
    }
    historical_commit = _advance_dev_source(source_root, "completed-source-tip")
    historical = {
        "root": str(source_root.resolve()), "branch": "codex/ac-dev",
        "commit": historical_commit, "source_sha256": cli_sha,
    }
    current = _defer_completed_source_and_open_clean_successor(
        tmp_path, {**historical, "tree": "", "dirty": ""},
    )
    monkeypatch.setattr(
        db, "__file__", str(Path(current["root"]) / "agent" / "governance" / "db.py")
    )

    def loaded_runtime_identity(current_commit):
        loaded_root = Path(db.__file__).resolve(strict=True).parents[2]
        server_path = loaded_root / "agent" / "governance" / "server.py"
        server_sha = "sha256:" + hashlib.sha256(server_path.read_bytes()).hexdigest()
        return {
            "loaded_pid": os.getpid(),
            "loaded_commit": current_commit,
            "worktree_head_version": current_commit,
            "loaded_source_path": str(server_path),
            "loaded_source_sha256": server_sha,
            "worktree_source_sha256": server_sha,
            "runtime_stale": False,
            "runtime_stale_reasons": [],
        }

    monkeypatch.setattr(
        db, "_current_dev_loaded_runtime_identity", loaded_runtime_identity,
    )
    storage = tmp_path / "dev-storage"
    adoption_dir = storage / "archive" / "canonical-legacy-postimage-adoption"
    adoption_dir.mkdir(parents=True)
    adoption_payload = {"candidate_source_identity": anchor}
    adoption_raw = json.dumps(
        adoption_payload, sort_keys=True, separators=(",", ":")
    ).encode()
    adoption = adoption_dir / (
        f"adoption.{hashlib.sha256(adoption_raw).hexdigest()}.json"
    )
    adoption.write_bytes(adoption_raw)
    adoption_sha = "sha256:" + hashlib.sha256(adoption_raw).hexdigest()
    receipt = {"history": {"adoption": {
        "path": str(adoption), "sha256": adoption_sha,
    }}}
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    conn.executemany("INSERT INTO schema_meta VALUES (?,?)", [
        ("governance_world_source_tip_json", json.dumps(historical)),
        ("governance_world_source_tip_sha256", db._world_source_tip_hash(historical)),
        ("governance_world_source_tip_revision", "3"),
    ])
    conn.commit()
    stable = tmp_path / "stable-volume"
    stable.mkdir()
    monkeypatch.setenv(db.AC_DEV_STORAGE_ROOT_ENV, str(storage))
    monkeypatch.setattr(
        db, "verified_stable_database_binding",
        lambda: {"shared_volume_path": str(stable)},
    )
    return db, conn, storage, receipt, historical, current


def test_current_dev_loaded_runtime_identity_requires_preloaded_server_without_import(
    monkeypatch,
):
    from agent.governance import db

    monkeypatch.delitem(sys.modules, "agent.governance.server", raising=False)

    with pytest.raises(ValueError, match="loaded runtime identity is unavailable"):
        db._current_dev_loaded_runtime_identity("a" * 40)

    assert "agent.governance.server" not in sys.modules


def test_current_cow_source_accepts_clean_canonical_descendant_of_historical_tip(
    tmp_path, monkeypatch,
):
    db, conn, storage, receipt, historical, current = (
        _current_cow_historical_tip_fixture(tmp_path, monkeypatch)
    )
    before = conn.iterdump()
    before = tuple(before)
    old_root = Path(historical["root"])
    assert subprocess.run(
        ["git", "branch", "--show-current"], cwd=old_root, check=True,
        capture_output=True, text=True,
    ).stdout.strip() == "codex/deferred-completed"
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=old_root, check=True,
        capture_output=True, text=True,
    ).stdout.strip() == "M agent/cli.py"

    db._verify_current_cow_successor_source(conn, storage, receipt)

    assert tuple(conn.iterdump()) == before
    assert json.loads(conn.execute(
        "SELECT value FROM schema_meta "
        "WHERE key='governance_world_source_tip_json'"
    ).fetchone()[0]) == historical
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=current["root"], check=True,
        capture_output=True, text=True,
    ).stdout.strip() == current["commit"]
    conn.close()


@pytest.mark.parametrize(
    "drift",
    (
        "dirty", "non_descendant", "current_source_hash", "historical_source_hash",
        "stable_branch", "foreign_object_store", "loaded_path", "loaded_runtime",
    ),
)
def test_current_cow_source_rejects_untrusted_live_descendant_zero_write(
    tmp_path, monkeypatch, drift,
):
    db, conn, storage, receipt, historical, current = (
        _current_cow_historical_tip_fixture(tmp_path, monkeypatch)
    )
    current_root = Path(current["root"])
    if drift == "dirty":
        (current_root / "untracked-drift.txt").write_text("dirty\n")
    elif drift == "non_descendant":
        unrelated = subprocess.run(
            ["git", "commit-tree", "HEAD^{tree}", "-m", "unrelated"],
            cwd=current_root, capture_output=True, text=True, check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "update-ref", "refs/heads/codex/ac-dev", unrelated,
             current["commit"]], cwd=current_root, check=True,
        )
    elif drift == "current_source_hash":
        original = db._current_first_start_source

        def mismatched_current(root):
            return {**original(root), "cli_sha256": "sha256:" + "7" * 64}

        monkeypatch.setattr(db, "_current_first_start_source", mismatched_current)
    elif drift == "historical_source_hash":
        historical["source_sha256"] = "sha256:" + "8" * 64
        conn.execute(
            "UPDATE schema_meta SET value=? WHERE key='governance_world_source_tip_json'",
            (json.dumps(historical),),
        )
        conn.execute(
            "UPDATE schema_meta SET value=? WHERE key='governance_world_source_tip_sha256'",
            (db._world_source_tip_hash(historical),),
        )
        conn.commit()
    elif drift == "stable_branch":
        subprocess.run(
            ["git", "branch", "-m", "codex/stable"], cwd=current_root,
            check=True, capture_output=True,
        )
    elif drift == "foreign_object_store":
        foreign = tmp_path / "foreign-source"
        subprocess.run(
            ["git", "clone", "--no-local", str(current_root), str(foreign)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "-B", "codex/ac-dev", current["commit"]],
            cwd=foreign, check=True, capture_output=True,
        )
        monkeypatch.setattr(
            db, "__file__", str(foreign / "agent" / "governance" / "db.py")
        )
    elif drift == "loaded_path":
        monkeypatch.setattr(
            db, "__file__", str(current_root / "agent" / "governance" / "server.py")
        )
    else:
        monkeypatch.setattr(
            db, "_current_dev_loaded_runtime_identity",
            lambda commit: {
                "loaded_pid": os.getpid(),
                "loaded_commit": historical["commit"],
                "worktree_head_version": commit,
                "loaded_source_path": str(
                    current_root / "agent" / "governance" / "server.py"
                ),
                "loaded_source_sha256": "sha256:" + "9" * 64,
                "worktree_source_sha256": "sha256:" + "9" * 64,
                "runtime_stale": True,
                "runtime_stale_reasons": ["worktree_head_moved"],
            },
        )
    before = tuple(conn.iterdump())

    with pytest.raises(ValueError):
        db._verify_current_cow_successor_source(conn, storage, receipt)

    assert tuple(conn.iterdump()) == before
    conn.close()


def test_real_cow_clone_without_live_custody_remains_denied(
    tmp_path, monkeypatch,
):
    """A real COW/source chain alone cannot replace listener/writer custody."""
    from agent import runtime_plane
    from agent.governance import db

    git_fixture = tmp_path / "git"
    git_fixture.mkdir()
    source_root, anchor_commit = _dev_source_repo(git_fixture)
    anchor = {
        "root": str(source_root.resolve()), "branch": "codex/ac-dev",
        "commit": anchor_commit, "source_sha256": "sha256:" + "6" * 64,
    }
    current_commit = _advance_dev_source(source_root, "descendant-one")
    current_commit = _advance_dev_source(source_root, "descendant-two")
    cli_sha = "sha256:" + hashlib.sha256(
        (source_root / "agent" / "cli.py").read_bytes()
    ).hexdigest()
    current = {
        "root": str(source_root.resolve()), "branch": "codex/ac-dev",
        "commit": current_commit, "source_sha256": cli_sha,
    }
    root, database, backup, operator, linked = _real_cow_successor_cli_fixture(
        tmp_path, monkeypatch, candidate_source_identity=anchor,
    )
    created = db.create_dev_cow_successor_receipt(
        root, operator_receipt=operator, predecessor_backup=backup,
        linked_v3_receipt=linked,
    )
    _write_historical_v1_from_v2(created["receipt"])
    connection = sqlite3.connect(database)
    connection.executemany(
        "INSERT OR REPLACE INTO schema_meta(key,value) VALUES (?,?)",
        [
            ("governance_world_source_tip_json", json.dumps(current)),
            ("governance_world_source_tip_sha256", db._world_source_tip_hash(current)),
            ("governance_world_source_tip_revision", "2"),
        ],
    )
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()

    stable_volume = tmp_path / "stable-volume"
    stable_volume.mkdir()
    stable_database = stable_volume / "governance.db"
    stable_database.touch()
    stable_stat = stable_database.stat(follow_symlinks=False)
    binding = {
        "shared_volume_path": str(stable_volume),
        "database_path": str(stable_database),
        "stable_database_identity": {
            "device": stable_stat.st_dev, "inode": stable_stat.st_ino,
        },
    }
    monkeypatch.setattr(db, "verified_stable_database_binding", lambda: binding)
    monkeypatch.setattr(db, "_revalidate_stable_database_binding", lambda _binding: None)
    monkeypatch.setattr(runtime_plane, "resolve_ac_dev_storage_root", lambda _stable: root)
    server_sha = "sha256:" + hashlib.sha256(
        Path(db.__file__).with_name("server.py").read_bytes()
    ).hexdigest()
    assert server_sha != cli_sha
    db.write_dev_launch_receipt(
        root, stable_shared_volume=stable_volume,
        source_sha256=server_sha, port=40008,
    )
    monkeypatch.setenv("AMING_CLAW_RUNTIME_PLANE", "dev")

    connection = sqlite3.connect(database)
    try:
        policy = db.classify_graph_activation_connection(connection)
    finally:
        connection.close()
    assert policy["runtime_plane"] == "unknown"
    assert policy["active_graph_activation_allowed"] is False


def _real_cow_successor_cli_fixture(
    tmp_path, monkeypatch, *, candidate_source_identity=None,
):
    from agent.governance import db

    root = tmp_path / "dev"
    database = root / db.AC_DATABASE_DEV_RELATIVE_PATH
    backup = root / "archive" / "operator-exception-backups" / "old.sqlite"
    backup.parent.mkdir(parents=True)
    connection = sqlite3.connect(backup)
    db._configure_connection(connection, busy_timeout=1000)
    db._ensure_schema(connection)
    backup_stat = backup.stat(follow_symlinks=False)
    genesis = {"schema_version": db.AC_WORLD_GENESIS_SCHEMA, "world_id": db.AC_DEV_WORLD_ID,
               "project_id": db.AC_PROJECT_ID, "source_only": True, "rows_copied": 0,
               "database_identity": {"device": backup_stat.st_dev, "inode": backup_stat.st_ino},
               "storage_root_identity": {
                   "path": str(root), "device": root.stat().st_dev,
                   "inode": root.stat().st_ino,
               }}
    genesis_raw = json.dumps(genesis, sort_keys=True, separators=(",", ":"))
    connection.executemany("INSERT OR REPLACE INTO schema_meta(key,value) VALUES (?,?)", [
        ("governance_world_id", db.AC_DEV_WORLD_ID),
        ("governance_world_genesis_json", genesis_raw),
        ("governance_world_genesis_sha256", db._world_genesis_hash(genesis)),
    ])
    connection.commit(); connection.close()
    database.parent.mkdir(parents=True)
    shutil.copy2(backup, database)
    connection = sqlite3.connect(database)
    db.ensure_backlog_read_schema(connection)
    connection.executemany(
        "INSERT INTO backlog_bugs(bug_id,created_at,updated_at,status) VALUES (?,?,?,?)",
        ((f"AC-{index:04d}", "2026-08-31", "2026-08-31", "OPEN") for index in range(3603)),
    )
    connection.commit(); connection.execute("PRAGMA wal_checkpoint(TRUNCATE)"); connection.close()
    for path in (Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()
    before_sha = db._durable_database_sha256(backup)
    after_sha = db._durable_database_sha256(database)
    snapshot_dir = root / "archive" / "staging" / "dashboard-backlog-snapshots"
    snapshot_dir.mkdir(parents=True)
    snapshot_temp = snapshot_dir / "snapshot.pending.sqlite"
    shutil.copyfile(database, snapshot_temp)
    snapshot_sha = db._durable_database_sha256(snapshot_temp)
    snapshot = snapshot_dir / f"snapshot.{snapshot_sha.removeprefix('sha256:')}.sqlite"
    snapshot_temp.rename(snapshot)
    manifest_payload = {
        "schema_version": "ac_dev_dashboard_backlog_snapshot.v1",
        "destination_path": str(snapshot),
        "destination_sha256": snapshot_sha,
        "row_count": 3603,
        "status_counts": {"OPEN": 3603},
    }
    manifest_raw = json.dumps(
        manifest_payload, sort_keys=True, separators=(",", ":")
    ).encode()
    manifest = snapshot_dir / f"manifest.{hashlib.sha256(manifest_raw).hexdigest()}.json"
    manifest.write_bytes(manifest_raw)
    operator_payload = {"schema_version": "ac_dev_operator_exception_cow_import.v1",
                        "qa_pass": False, "release_authority": False, "rows": 3603,
                        "decisions": ["dec-real-sqlite"], "backup_sha256": before_sha,
                        "snapshot_sha256": snapshot_sha,
                        "target_sha256_before": before_sha, "target_sha256_after": after_sha}
    operator_raw = json.dumps(operator_payload, sort_keys=True, separators=(",", ":")).encode()
    operator = root / "archive" / "operator-exceptions" / (
        f"cow-import.{hashlib.sha256(operator_raw).hexdigest()}.json")
    operator.parent.mkdir(parents=True); operator.write_bytes(operator_raw)
    linked_payload = {"schema_version": "ac_dev_offline_schema_admission.v3", "stage": "completed",
                      "project_id": db.AC_PROJECT_ID, "port": 40008,
                      "database_identity": {"path": str(database), "device": backup_stat.st_dev,
                                            "inode": backup_stat.st_ino}}
    linked_raw = json.dumps(linked_payload, sort_keys=True, separators=(",", ":")).encode()
    linked_digest = hashlib.sha256(linked_raw).hexdigest()
    linked = root / "archive" / "schema-admission" / f"{linked_digest}.json"
    linked.parent.mkdir(parents=True); linked.write_bytes(linked_raw)
    linked.with_suffix(".sha256").write_text(f"sha256:{linked_digest}  {linked.name}\n")
    adoption_payload = {"schema_version": "ac_dev_canonical_legacy_postimage_adoption.v1",
                        "stage": "completed", "project_id": "aming-claw", "port": 40008,
                        "linked_v3_receipt": str(linked),
                        "linked_v3_receipt_sha256": "sha256:" + linked_digest}
    if candidate_source_identity is not None:
        adoption_payload["candidate_source_identity"] = candidate_source_identity
    quarantine_dir = root / "quarantine" / "schema-admission-sidecars" / "fixture"
    quarantine_dir.mkdir(parents=True)
    quarantine_payload = {
        "schema_version": "aming-claw.schema-admission-sidecar-quarantine.v1",
        "stage": "completed",
    }
    quarantine_raw = json.dumps(
        quarantine_payload, sort_keys=True, separators=(",", ":")
    ).encode()
    quarantine = quarantine_dir / f"manifest.{hashlib.sha256(quarantine_raw).hexdigest()}.json"
    quarantine.write_bytes(quarantine_raw)
    adoption_payload["quarantine_manifest"] = {
        "path": str(quarantine),
        "sha256": "sha256:" + hashlib.sha256(quarantine_raw).hexdigest(),
    }
    adoption_raw = json.dumps(adoption_payload, sort_keys=True, separators=(",", ":")).encode()
    adoption = root / "archive" / "canonical-legacy-postimage-adoption" / (
        f"adoption.{hashlib.sha256(adoption_raw).hexdigest()}.json")
    adoption.parent.mkdir(parents=True); adoption.write_bytes(adoption_raw)
    stable = tmp_path / "stable.db"; stable.write_bytes(b"stable")
    stable_stat = stable.stat(follow_symlinks=False)
    stable_identity = {"schema_version": "ac_stable_database_identity.v1",
                       "device": stable_stat.st_dev, "inode": stable_stat.st_ino,
                       "stable_relative_path_sha256": "sha256:" + hashlib.sha256(
                           db.AC_DATABASE_STABLE_RELATIVE_PATH.encode()).hexdigest()}
    monkeypatch.setattr(db, "verified_stable_database_binding", lambda: {
        "database_path": str(stable), "stable_database_identity": stable_identity})
    monkeypatch.setattr(db, "_default_cutover_listener_probe",
                        lambda port: {"port": port, "listening": False, "pid": 0})
    monkeypatch.setattr(db, "_assert_no_external_sqlite_holders", lambda _database: None)
    return root, database, backup, operator, linked


def _write_historical_v1_from_v2(receipt_path):
    payload = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    payload["schema_version"] = "ac_dev_cow_database_successor.v1"
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    path = Path(receipt_path).with_name(
        f"successor.{hashlib.sha256(raw).hexdigest()}.json"
    )
    path.write_bytes(raw)
    return path


def _dev_issuance_anchor_receipt_fixture(tmp_path: Path):
    from agent.governance import db

    root = (tmp_path / "dev-issuance").resolve()
    adoption_dir = root / "archive" / "canonical-legacy-postimage-adoption"
    linked_dir = root / "archive" / "schema-admission"
    successor_dir = root / db.AC_DEV_COW_SUCCESSOR_ARCHIVE
    for directory in (adoption_dir, linked_dir, successor_dir):
        directory.mkdir(parents=True, exist_ok=True)

    def write(directory: Path, payload, *, prefix: str = "") -> Path:
        raw = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        digest = hashlib.sha256(raw).hexdigest()
        path = directory / (f"{prefix}.{digest}.json" if prefix else f"{digest}.json")
        path.write_bytes(raw)
        return path

    historical_source = {
        "root": "/source/ac-dev", "branch": "codex/ac-dev",
        "commit": "1" * 40, "tree": "2" * 40,
        "source_sha256": "sha256:" + "3" * 64, "dirty": "",
    }
    linked = write(linked_dir, {
        "schema_version": "ac_dev_offline_schema_admission.v3",
        "stage": "completed", "project_id": "aming-claw", "port": 40008,
        "root_identity": {"path": str(root), "device": 1, "inode": 2},
        "source_identity": {"cli_source": historical_source},
    })
    linked_digest = hashlib.sha256(linked.read_bytes()).hexdigest()
    linked.with_suffix(".sha256").write_text(
        f"sha256:{linked_digest}  {linked.name}\n", encoding="utf-8"
    )
    issuance = "8" * 40
    adoption = write(adoption_dir, {
        "schema_version": "ac_dev_canonical_legacy_postimage_adoption.v1",
        "stage": "completed", "project_id": "aming-claw", "port": 40008,
        "root_identity": {"path": str(root), "device": 1, "inode": 2},
        "receipt_source_identity": historical_source,
        "linked_v3_receipt": str(linked),
        "linked_v3_receipt_sha256": "sha256:" + linked_digest,
        "readbacks": {"stable_runtime_commit": issuance},
    }, prefix="adoption")
    adoption_sha = "sha256:" + hashlib.sha256(adoption.read_bytes()).hexdigest()
    successor = write(successor_dir, {
        "schema_version": db.AC_DEV_COW_SUCCESSOR_SCHEMA,
        "stage": "completed", "project_id": "aming-claw", "port": 40008,
        "root": str(root),
        "history": {
            "adoption": {"path": str(adoption), "sha256": adoption_sha},
            "linked_v3": {
                "path": str(linked), "sha256": "sha256:" + linked_digest,
            },
        },
        "stable_binding": {"database": {}, "runtime_commit": None},
    }, prefix=db.AC_DEV_COW_SUCCESSOR_PREFIX)
    return root, successor, adoption, linked, issuance


def test_dev_issuance_ancestry_anchor_is_exact_receipt_chain_projection(tmp_path):
    from agent.governance import db

    root, _successor, _adoption, _linked, issuance = (
        _dev_issuance_anchor_receipt_fixture(tmp_path)
    )

    assert db.dev_issuance_ancestry_anchor_commit(root) == issuance


@pytest.mark.parametrize("tamper", ["successor_anchor", "linked_sidecar", "adoption_duplicate"])
def test_dev_issuance_ancestry_anchor_fails_closed_on_chain_drift(tmp_path, tamper):
    from agent.governance import db

    root, successor, adoption, linked, _issuance = (
        _dev_issuance_anchor_receipt_fixture(tmp_path)
    )
    if tamper == "successor_anchor":
        payload = json.loads(successor.read_text(encoding="utf-8"))
        payload["stable_binding"]["runtime_commit"] = "f" * 40
        successor.unlink()
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        successor.with_name(
            f"{db.AC_DEV_COW_SUCCESSOR_PREFIX}.{hashlib.sha256(raw).hexdigest()}.json"
        ).write_bytes(raw)
    elif tamper == "linked_sidecar":
        linked.with_suffix(".sha256").write_text("sha256:" + "0" * 64 + "\n")
    else:
        adoption.with_name("adoption." + "0" * 64 + ".json").write_text(
            "{}", encoding="utf-8"
        )

    with pytest.raises(ValueError, match="AC dev issuance"):
        db.dev_issuance_ancestry_anchor_commit(root)


def _phase_z_cow_prestart_fixture(tmp_path, monkeypatch):
    """Build one real COW successor whose issuance meta is externally sealed."""
    from agent.governance import db

    git_fixture = tmp_path / "git-source"
    git_fixture.mkdir()
    source_root, source_commit = _dev_source_repo(git_fixture)
    server_source = source_root / "agent" / "governance" / "server.py"
    server_source.parent.mkdir()
    server_source.write_text("# canonical server source producer\n", encoding="utf-8")
    loaded_db_source = source_root / "agent" / "governance" / "db.py"
    loaded_db_source.write_text("# loaded DB module location fixture\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "agent/governance/server.py", "agent/governance/db.py"],
        cwd=source_root, check=True,
    )
    subprocess.run(
        ["git", "commit", "--amend", "--no-edit"], cwd=source_root,
        check=True, capture_output=True,
    )
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source_root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    monkeypatch.setattr(db, "__file__", str(loaded_db_source))
    source = {
        "root": str(source_root.resolve()),
        "branch": "codex/ac-dev",
        "commit": source_commit,
        "tree": subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=source_root, check=True,
            capture_output=True, text=True,
        ).stdout.strip(),
        "source_sha256": "sha256:" + hashlib.sha256(
            (source_root / "agent" / "cli.py").read_bytes()
        ).hexdigest(),
        "dirty": "",
    }
    process = {"pid": 41, "start_identity": "pid:41:cli-bootstrap"}
    root, database, backup, operator, linked = _real_cow_successor_cli_fixture(
        tmp_path, monkeypatch, candidate_source_identity=source,
    )
    source_tip = {
        key: source[key] for key in ("root", "branch", "commit", "source_sha256")
    }
    meta = (
        ("governance_world_source_tip_json", json.dumps(source_tip)),
        ("governance_world_source_tip_sha256", db._world_source_tip_hash(source_tip)),
        ("governance_world_source_tip_revision", "2"),
        ("governance_world_current_process_json", json.dumps(process)),
    )
    for candidate in (backup, database):
        connection = sqlite3.connect(candidate)
        for statement in db._authority_projection_schema_statements():
            connection.execute(statement)
        connection.executemany(
            "INSERT OR REPLACE INTO schema_meta(key,value) VALUES (?,?)", meta,
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
    adoption = next(
        (root / "archive" / "canonical-legacy-postimage-adoption").glob(
            "adoption.*.json"
        )
    )
    adoption_payload = json.loads(adoption.read_text(encoding="utf-8"))
    adoption_payload["legacy_process_identity"] = process
    adoption_raw = json.dumps(
        adoption_payload, sort_keys=True, separators=(",", ":")
    ).encode()
    adoption.unlink()
    adoption = adoption.parent / (
        f"adoption.{hashlib.sha256(adoption_raw).hexdigest()}.json"
    )
    adoption.write_bytes(adoption_raw)
    operator_payload = json.loads(operator.read_text(encoding="utf-8"))
    operator_payload.update(
        backup_sha256=db._durable_database_sha256(backup),
        target_sha256_before=db._durable_database_sha256(backup),
        target_sha256_after=db._durable_database_sha256(database),
    )
    operator_raw = json.dumps(
        operator_payload, sort_keys=True, separators=(",", ":")
    ).encode()
    operator.unlink()
    operator = operator.parent / (
        f"cow-import.{hashlib.sha256(operator_raw).hexdigest()}.json"
    )
    operator.write_bytes(operator_raw)
    created = db.create_dev_cow_successor_receipt(
        root, operator_receipt=operator, predecessor_backup=backup,
        linked_v3_receipt=linked,
    )
    _write_historical_v1_from_v2(created["receipt"])
    receipt = json.loads(Path(created["receipt"]).read_text(encoding="utf-8"))
    return root, database, linked, source, process, receipt


def _phase_z_durable_process(root, source, *, pid):
    launch_id = str(pid)[-1] * 24
    source_tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=source["root"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    started = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
        text=True, check=False,
    ).stdout.strip()
    start_identity = (
        "sha256:" + hashlib.sha256(started.encode()).hexdigest()
        if started else "sha256:" + str(pid + 1)[-1] * 64
    )
    runtime = root / "runtime" / "durable-launch"
    return {
        "pid": pid,
        "start_identity": start_identity,
        "argv": [
            str(Path(sys.executable).resolve(strict=True)),
            str((Path(source["root"]) / "agent" / "cli.py").resolve(strict=True)),
            "start", "--runtime-plane", "dev", "--port", "40008",
            "--dev-storage-root", str(root),
            "--stable-anchor-commit", "a" * 40,
            "--durable-child-runtime-dir", str(runtime),
            "--durable-child-launch-id", launch_id,
            "--durable-child-control-fd", "5",
            "--durable-child-pending-receipt",
            str(runtime / ("pending." + "b" * 64 + ".json")),
            "--durable-child-linked-v3-receipt",
            str(root / "archive" / "schema-admission" / ("c" * 64 + ".json")),
        ],
        "cwd": source["root"],
        "source_root": source["root"],
        "source_commit": source["commit"],
        "source_tree": source_tree,
        "dev_storage_root": str(root),
        "project_id": "aming-claw",
        "port": 40008,
        "policy": {
            "runtime_plane": "dev", "migration": "verify-only",
            "stable_deployment": "deny", "graph_activation": "deny",
            "background_workers": "deny",
        },
        "launch_id": launch_id,
    }


def test_cow_durable_process_custody_accepts_exact_normalized_launcher_argv(
    tmp_path,
):
    from agent.governance import db

    source_root, source_commit = _dev_source_repo(tmp_path)
    subprocess.run(
        ["git", "branch", "-M", "codex/ac-dev"], cwd=source_root, check=True,
    )
    root = tmp_path / "dev-world"
    root.mkdir()
    source = {
        "root": str(source_root.resolve()), "branch": "codex/ac-dev",
        "commit": source_commit, "source_sha256": "sha256:" + "d" * 64,
    }
    process = _phase_z_durable_process(root, source, pid=42)

    db._validate_dev_current_process_custody(
        process, root=root, candidate_source=source,
    )


def test_cow_completed_process_axis_transitions_exact_dead_pre_normalization_argv(
    tmp_path,
):
    from agent.governance import db

    source_root, source_commit = _dev_source_repo(tmp_path)
    subprocess.run(
        ["git", "branch", "-M", "codex/ac-dev"], cwd=source_root, check=True,
    )
    source = {
        "root": str(source_root.resolve()), "branch": "codex/ac-dev",
        "commit": source_commit,
        "tree": subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=source_root, check=True,
            capture_output=True, text=True,
        ).stdout.strip(),
        "source_sha256": "sha256:" + "d" * 64,
    }
    root = tmp_path / "dev-world"
    root.mkdir()
    process = _phase_z_durable_process(root, source, pid=99999999)
    process["argv"] = process["argv"][1:]
    candidate_commit = _advance_dev_source(source_root, "normalized-custody-successor")
    candidate = {
        **source,
        "commit": candidate_commit,
        "tree": subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=source_root, check=True,
            capture_output=True, text=True,
        ).stdout.strip(),
        "source_sha256": "sha256:" + hashlib.sha256(
            (source_root / "agent" / "cli.py").read_bytes()
        ).hexdigest(),
    }

    db._validate_dev_cow_completed_process_axis(
        process, root=root, source_identity=candidate,
    )
    with pytest.raises(ValueError, match="durable process custody binding mismatch"):
        db._validate_dev_current_process_custody(
            process, root=root, candidate_source=candidate,
        )


@pytest.mark.parametrize("liveness", ("live", "permission_denied"))
def test_cow_completed_process_axis_rejects_nonprovably_dead_canonical_pid(
    tmp_path, monkeypatch, liveness,
):
    from agent.governance import db

    source_root, source_commit = _dev_source_repo(tmp_path)
    subprocess.run(
        ["git", "branch", "-M", "codex/ac-dev"], cwd=source_root, check=True,
    )
    source = {
        "root": str(source_root.resolve()), "branch": "codex/ac-dev",
        "commit": source_commit,
        "tree": subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=source_root, check=True,
            capture_output=True, text=True,
        ).stdout.strip(),
        "source_sha256": "sha256:" + hashlib.sha256(
            (source_root / "agent" / "cli.py").read_bytes()
        ).hexdigest(),
    }
    root = tmp_path / "dev-world"
    root.mkdir()
    pid = os.getpid() if liveness == "live" else 99999998
    process = _phase_z_durable_process(root, source, pid=pid)
    if liveness == "permission_denied":
        monkeypatch.setattr(
            db.os, "kill",
            lambda *_args: (_ for _ in ()).throw(PermissionError("denied")),
        )

    with pytest.raises(ValueError, match="PID is still live|liveness is unavailable"):
        db._validate_dev_cow_completed_process_axis(
            process, root=root, source_identity=source,
            historical_source_tip=source,
        )


def test_cow_completed_bootstrap_process_axis_accepts_provably_dead_pid(
    tmp_path, monkeypatch,
):
    from agent.governance import db

    pid = 99999997

    def dead_process(observed, signal_number):
        assert (observed, signal_number) == (pid, 0)
        raise ProcessLookupError(observed)

    monkeypatch.setattr(db.os, "kill", dead_process)

    db._validate_dev_cow_completed_process_axis(
        {"pid": pid, "start_identity": f"pid:{pid}:cli-bootstrap"},
        root=tmp_path, source_identity={},
    )


@pytest.mark.parametrize("liveness", ("live", "permission", "oserror", "invalid"))
def test_cow_completed_bootstrap_process_axis_rejects_non_dead_pid(
    tmp_path, monkeypatch, liveness,
):
    from agent.governance import db

    pid = os.getpid() if liveness == "live" else 99999996
    process = {"pid": pid, "start_identity": f"pid:{pid}:cli-bootstrap"}
    if liveness == "permission":
        monkeypatch.setattr(
            db.os, "kill",
            lambda *_args: (_ for _ in ()).throw(PermissionError("denied")),
        )
    elif liveness == "oserror":
        monkeypatch.setattr(
            db.os, "kill",
            lambda *_args: (_ for _ in ()).throw(OSError("unavailable")),
        )
    elif liveness == "invalid":
        process = {"pid": 0, "start_identity": "pid:0:cli-bootstrap"}

    with pytest.raises(
        ValueError,
        match="bootstrap custody mismatch|bootstrap PID is still live|"
        "bootstrap liveness is unavailable",
    ):
        db._validate_dev_cow_completed_process_axis(
            process, root=tmp_path, source_identity={},
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "module_string", "alternate_script", "duplicate_option",
        "missing_option", "reordered_options", "wrong_root", "wrong_port",
        "wrong_project", "wrong_launch_id",
    ),
)
def test_cow_durable_process_custody_rejects_noncanonical_launcher_argv(
    tmp_path, mutation,
):
    from agent.governance import db

    source_root, source_commit = _dev_source_repo(tmp_path)
    subprocess.run(
        ["git", "branch", "-M", "codex/ac-dev"], cwd=source_root, check=True,
    )
    root = tmp_path / "dev-world"
    root.mkdir()
    source = {
        "root": str(source_root.resolve()), "branch": "codex/ac-dev",
        "commit": source_commit, "source_sha256": "sha256:" + "d" * 64,
    }
    process = _phase_z_durable_process(root, source, pid=42)
    argv = process["argv"]
    if mutation == "module_string":
        argv[1] = "agent.cli"
    elif mutation == "alternate_script":
        alternate = source_root / "agent" / "alternate_cli.py"
        alternate.write_text("# alternate\n", encoding="utf-8")
        argv[1] = str(alternate.resolve(strict=True))
    elif mutation == "duplicate_option":
        argv.extend(("--port", "40008"))
    elif mutation == "missing_option":
        del argv[5:7]
    elif mutation == "reordered_options":
        argv[3:7] = argv[5:7] + argv[3:5]
    elif mutation == "wrong_root":
        argv[8] = str(tmp_path / "other-world")
    elif mutation == "wrong_port":
        argv[6] = "40009"
    elif mutation == "wrong_project":
        process["project_id"] = "other-project"
    else:
        process["launch_id"] = "e" * 24

    with pytest.raises(ValueError, match="durable process custody binding mismatch"):
        db._validate_dev_current_process_custody(
            process, root=root, candidate_source=source,
        )


@pytest.mark.parametrize(
    "mutation", ("process", "tip", "managed", "protected", "other_meta")
)
def test_cow_prestart_requires_exact_issuance_schema_meta_projection(
    tmp_path, monkeypatch, mutation,
):
    from agent.governance import db

    root, database, linked, source, _process, receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    assert db.validate_dev_cow_successor_preimage(
        root, linked_v3_receipt=linked, source_identity=source,
        stable_binding=db.verified_stable_database_binding(),
    ) == receipt
    candidate_source = source
    connection = sqlite3.connect(database)
    if mutation == "process":
        connection.execute(
            "UPDATE schema_meta SET value=? WHERE key=?",
            (json.dumps(_phase_z_durable_process(root, source, pid=42)),
             "governance_world_current_process_json"),
        )
    elif mutation == "tip":
        next_commit = _advance_dev_source(Path(source["root"]), "phase-z-next")
        candidate_source = {**source, "commit": next_commit}
        connection.executemany(
            "UPDATE schema_meta SET value=? WHERE key=?",
            (
                (json.dumps(candidate_source), "governance_world_source_tip_json"),
                (db._world_source_tip_hash(candidate_source),
                 "governance_world_source_tip_sha256"),
                ("3", "governance_world_source_tip_revision"),
            ),
        )
    elif mutation == "managed":
        connection.execute("DROP TRIGGER trg_dashboard_backlog_cache_update")
    elif mutation == "protected":
        connection.execute("CREATE TABLE phase_z_protected_drift(value TEXT)")
    else:
        connection.execute(
            "INSERT INTO schema_meta(key,value) VALUES (?,?)",
            ("phase_z_unanchored_meta", "self-consistent-but-unsealed"),
        )
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()

    expected = (
        "issuance schema_meta"
        if mutation in {"process", "tip", "other_meta"}
        else "inventory|schema"
    )
    with pytest.raises(ValueError, match=expected):
        db.validate_dev_cow_successor_preimage(
            root, linked_v3_receipt=linked, source_identity=candidate_source,
            stable_binding=db.verified_stable_database_binding(),
        )


def _advance_cow_to_completed_generation(database, root, source):
    from agent.governance import db

    commit = _advance_dev_source(Path(source["root"]), "completed-generation")
    completed_source = {**source, "commit": commit}
    completed_source["tree"] = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=source["root"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    completed_source["source_sha256"] = "sha256:" + hashlib.sha256(
        (Path(source["root"]) / "agent" / "cli.py").read_bytes()
    ).hexdigest()
    process = _phase_z_durable_process(root, completed_source, pid=42)
    completed_tip = {
        key: completed_source[key] for key in db._DEV_SOURCE_TIP_KEYS
    }
    connection = sqlite3.connect(database)
    connection.executemany(
        "UPDATE schema_meta SET value=? WHERE key=?",
        (
            (json.dumps(completed_tip, sort_keys=True, separators=(",", ":")),
             "governance_world_source_tip_json"),
            (db._world_source_tip_hash(completed_tip),
             "governance_world_source_tip_sha256"),
            ("3", "governance_world_source_tip_revision"),
            (json.dumps(process, sort_keys=True, separators=(",", ":")),
             "governance_world_current_process_json"),
        ),
    )
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    return completed_source


def test_cow_completed_generation_uses_current_projection_not_issuance_digest(
    tmp_path, monkeypatch,
):
    from agent.governance import db

    root, database, linked, source, _process, receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    completed_source = _advance_cow_to_completed_generation(
        database, root, source,
    )
    assert completed_source["branch"] == "codex/ac-dev"
    connection = sqlite3.connect(database)
    try:
        assert db._sqlite_logical_projection(connection)["schema_meta"] != (
            receipt["successor"]["protected_projection"]["schema_meta"]
        )
    finally:
        connection.close()
    assert db.validate_dev_cow_completed_generation_projection(
        root, linked_v3_receipt=linked, source_identity=completed_source,
        stable_binding=db.verified_stable_database_binding(),
    ) == receipt


def _completed_cow_basic_restart_fixture(tmp_path, monkeypatch):
    from agent.governance import db

    root, database, _linked, source, _process, _receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    candidate = _advance_cow_to_completed_generation(database, root, source)
    _phase_z_bind_first_start_runtime(tmp_path, monkeypatch, root)
    original_kill = os.kill

    def stopped_process(pid, signal_number):
        if pid == 42 and signal_number == 0:
            raise ProcessLookupError(pid)
        return original_kill(pid, signal_number)

    monkeypatch.setattr(db.os, "kill", stopped_process)
    stable = Path(db.verified_stable_database_binding()["shared_volume_path"])
    launch_path = root / db.AC_DEV_LAUNCH_RECEIPT_NAME
    db.write_dev_launch_receipt(
        root, stable_shared_volume=stable,
        source_sha256="sha256:" + "0" * 64, port=40008,
    )
    return root, database, candidate, stable, launch_path


@pytest.mark.parametrize("entrypoint", ("completed_axis", "basic_restart"))
@pytest.mark.parametrize(
    "observation",
    ("different", "matching", "unavailable", "unreadable", "ambiguous", "dead"),
)
def test_completed_cow_pid_incarnation_restart_real_paths(
    tmp_path, monkeypatch, entrypoint, observation,
):
    """Exercise both real guards without treating numeric PID reuse as death."""
    import errno
    from agent.governance import db

    pid = 42  # Isolated historical fixture only; no real signal is sent.
    old_start = "Sat Sep  5 07:00:00 2026"
    new_start = "Sun Sep  6 11:55:45 2026"
    os_view = {"started": old_start, "error": False, "fresh": False}
    ps_queries = []
    original_run = subprocess.run

    def process_start_query(args, *positional, **kwargs):
        if list(args) == ["ps", "-o", "lstart=", "-p", str(pid)]:
            assert kwargs.get("text") is True
            if os_view["fresh"]:
                ps_queries.append(list(args))
            if os_view["error"]:
                raise PermissionError(errno.EPERM, "isolated ps unavailable")
            return subprocess.CompletedProcess(
                args, 0, stdout="  " + os_view["started"] + "  \n", stderr="",
            )
        return original_run(args, *positional, **kwargs)

    monkeypatch.setattr(db.subprocess, "run", process_start_query)
    root, database, candidate, _stable, launch_path = (
        _completed_cow_basic_restart_fixture(tmp_path, monkeypatch)
    )
    with sqlite3.connect(database) as connection:
        historical = json.loads(connection.execute(
            "SELECT value FROM schema_meta WHERE key=?",
            ("governance_world_current_process_json",),
        ).fetchone()[0])
    assert historical["pid"] == pid
    assert historical["start_identity"] == (
        "sha256:" + hashlib.sha256(old_start.encode()).hexdigest()
    )
    # Only the OS observation changes after the completed fixture is sealed.
    # Its metadata, phase, source history, receipts and lease are not rewritten.
    os_view.update(
        fresh=True,
        started={
            "different": new_start,
            "matching": old_start,
            "unavailable": "",
            "unreadable": "",
            "ambiguous": old_start + "\n" + new_start,
            "dead": "",
        }[observation],
        error=observation == "unreadable",
    )
    kill_probes = []

    def process_presence(observed, signal_number):
        assert (observed, signal_number) == (pid, 0)
        frame = sys._getframe(1)
        callers = []
        while frame is not None:
            if frame.f_code.co_name in {
                "_validate_dev_current_process_custody",
                "_validate_dev_cow_completed_basic_restart",
            }:
                callers.append(frame.f_code.co_name)
            frame = frame.f_back
        kill_probes.append(callers)
        # The basic-restart path first observes the old PID absent during its
        # two completed-axis reads, then observes a reused PID at its own
        # final guard. This reaches that independent numeric-PID check on base
        # without mocking the phase selector or either production validator.
        if (observation == "dead"
                or (entrypoint == "basic_restart" and len(kill_probes) <= 2)):
            raise ProcessLookupError(errno.ESRCH, "isolated old incarnation ended")
        if observation != "matching":
            raise PermissionError(errno.EPERM, "isolated reused PID is protected")

    monkeypatch.setattr(db.os, "kill", process_presence)
    before = database.read_bytes()
    logical_before = db._database_logical_sha256(database)
    launch_before = launch_path.read_bytes()
    archive_before = {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted((root / "archive").rglob("*")) if path.is_file()
    }
    loaded_source = Path(
        db._validate_dev_current_process_custody.__code__.co_filename
    )
    print(json.dumps({
        "milestone": "completed_cow_incarnation_probe",
        "entrypoint": entrypoint, "observation": observation,
        "loaded_db_source": str(loaded_source),
        "loaded_db_sha256": hashlib.sha256(loaded_source.read_bytes()).hexdigest(),
        "stored_start_identity": historical["start_identity"],
        "test_pid": os.getpid(), "historical_fixture_pid": pid,
    }, sort_keys=True))

    def exercise():
        if entrypoint == "completed_axis":
            return db._validate_dev_cow_completed_process_axis(
                historical, root=root, source_identity=candidate,
                historical_source_tip=candidate,
            )
        result = db.bootstrap_dev_governance_store(
            root, source_identity=candidate,
            process_identity={
                "pid": os.getpid(),
                "start_identity": f"pid:{os.getpid()}:cli-bootstrap",
            },
        )
        assert result["restart_safe"] is True
        assert result["created"] is False
        assert result["current_process_identity"] == historical
        return result

    try:
        if observation in {"different", "dead"}:
            exercise()
        else:
            with pytest.raises(ValueError):
                exercise()
        if entrypoint == "basic_restart":
            assert len(kill_probes) >= 3
            assert "_validate_dev_current_process_custody" in kill_probes[0]
            assert "_validate_dev_current_process_custody" in kill_probes[1]
            assert kill_probes[2] == ["_validate_dev_cow_completed_basic_restart"]
        else:
            assert kill_probes[0] == ["_validate_dev_current_process_custody"]
        if observation == "different":
            assert ps_queries  # The fresh identity must belong to PID 42, not self.
        assert database.read_bytes() == before
        assert db._database_logical_sha256(database) == logical_before
        assert launch_path.read_bytes() == launch_before
        assert {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted((root / "archive").rglob("*")) if path.is_file()
        } == archive_before
    finally:
        db.release_dev_runtime_writer_lease(root)
        print(json.dumps({"kill_probe_callers": kill_probes,
                          "fresh_historical_pid_queries": ps_queries}, sort_keys=True))


def _defer_completed_source_and_open_clean_successor(
    tmp_path, historical_source,
):
    """Move current authority without requiring the old worktree to stay frozen."""

    old = Path(historical_source["root"])
    historical_commit = str(historical_source["commit"])
    builder = tmp_path / "completed-source-builder"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(builder), historical_commit],
        cwd=old, check=True, capture_output=True,
    )
    current_commit = _advance_dev_source(builder, "completed-current-descendant")
    subprocess.run(
        ["git", "update-ref", "refs/heads/codex/ac-dev", current_commit,
         historical_commit], cwd=old, check=True,
    )
    subprocess.run(
        ["git", "update-ref", "--no-deref", "HEAD", historical_commit,
         current_commit], cwd=old, check=True,
    )
    subprocess.run(
        ["git", "branch", "codex/deferred-completed", historical_commit],
        cwd=old, check=True,
    )
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/codex/deferred-completed"],
        cwd=old, check=True,
    )
    successor = tmp_path / "completed-source-successor"
    subprocess.run(
        ["git", "worktree", "add", str(successor), "codex/ac-dev"],
        cwd=old, check=True, capture_output=True,
    )
    # This is the retained operator draft from the retired checkout.  It is
    # intentionally not cleaned, reset, or made part of current authority.
    with (old / "agent" / "cli.py").open("a", encoding="utf-8") as handle:
        handle.write("# retained deferred operator draft\n")
    return {
        **historical_source,
        "root": str(successor.resolve()),
        "commit": current_commit,
        "tree": subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=successor, check=True,
            capture_output=True, text=True,
        ).stdout.strip(),
        "source_sha256": "sha256:" + hashlib.sha256(
            (successor / "agent" / "cli.py").read_bytes()
        ).hexdigest(),
        "dirty": "",
    }


def _real_empty_wal_index_bytes(tmp_path, database):
    """Capture SQLite's own WAL-index after a real TRUNCATE checkpoint."""

    copied = tmp_path / "wal-index-source.db"
    shutil.copyfile(database, copied)
    connection = sqlite3.connect(copied)
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    connection.execute(f"PRAGMA user_version={user_version + 1}")
    connection.commit()
    assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
        0, 0, 0,
    )
    wal = Path(str(copied) + "-wal")
    shm = Path(str(copied) + "-shm")
    assert wal.stat().st_size == 0
    raw = shm.read_bytes()
    connection.close()
    assert len(raw) == 32768
    return raw


def _real_fresh_empty_wal_index_bytes(tmp_path, database):
    """Capture SQLite's fresh read-only WAL-index with reset page state."""

    copied = tmp_path / "fresh-wal-index-source.db"
    shutil.copyfile(database, copied)
    connection = sqlite3.connect(copied)
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    connection.close()
    connection = sqlite3.connect(copied)
    connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
    wal = Path(str(copied) + "-wal")
    shm = Path(str(copied) + "-shm")
    assert wal.stat().st_size == 0
    raw = shm.read_bytes()
    connection.close()
    assert len(raw) == 32768
    return raw


@pytest.mark.parametrize("wal_index_state", ("truncate", "fresh"))
def test_completed_cow_restart_accepts_content_addressed_tip_and_inert_sidecars(
    tmp_path, monkeypatch, wal_index_state,
):
    from agent.governance import db

    root, database, linked, source, _process, receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    historical = _advance_cow_to_completed_generation(database, root, source)
    current = _defer_completed_source_and_open_clean_successor(tmp_path, historical)
    monkeypatch.setenv(db.AC_DEV_STORAGE_ROOT_ENV, str(root))
    wal = Path(str(database) + "-wal")
    shm = Path(str(database) + "-shm")
    wal.write_bytes(b"")
    wal_index = (
        _real_empty_wal_index_bytes(tmp_path, database)
        if wal_index_state == "truncate"
        else _real_fresh_empty_wal_index_bytes(tmp_path, database)
    )
    shm.write_bytes(wal_index)
    before = {
        "database": database.read_bytes(), "wal": wal.read_bytes(),
        "shm": shm.read_bytes(),
    }

    assert db.validate_dev_cow_completed_generation_projection(
        root, linked_v3_receipt=linked, source_identity=current,
        stable_binding=db.verified_stable_database_binding(),
    ) == receipt

    assert database.read_bytes() == before["database"]
    assert wal.read_bytes() == before["wal"]
    assert shm.read_bytes() == before["shm"]
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=historical["root"], check=True,
        capture_output=True, text=True,
    ).stdout.strip() == "M agent/cli.py"


@pytest.mark.parametrize(
    "forgery",
    (
        "random", "all_zero", "single_bit_header", "single_bit_tail",
        "same_size_checkpoint",
    ),
)
def test_completed_cow_restart_rejects_forged_same_size_wal_index_zero_write(
    tmp_path, monkeypatch, forgery,
):
    from agent.governance import db

    root, database, linked, source, _process, _receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    current = _advance_cow_to_completed_generation(database, root, source)
    canonical = bytearray(_real_empty_wal_index_bytes(tmp_path, database))
    if forgery == "random":
        forged = bytearray().join(
            hashlib.sha256(str(index).encode()).digest()
            for index in range(1024)
        )
    elif forgery == "all_zero":
        forged = bytearray(32768)
    elif forgery == "single_bit_header":
        canonical[8] ^= 0x01
        forged = canonical
    elif forgery == "single_bit_tail":
        canonical[136] ^= 0x01
        forged = canonical
    else:
        # Preserve both checksummed headers but forge the checkpoint/read-mark
        # authority to look used rather than SQLite's canonical empty state.
        canonical[108:112] = (0).to_bytes(4, sys.byteorder)
        forged = canonical
    assert len(forged) == 32768
    wal = Path(str(database) + "-wal")
    shm = Path(str(database) + "-shm")
    wal.write_bytes(b"")
    shm.write_bytes(forged)
    before = database.read_bytes(), wal.read_bytes(), shm.read_bytes()

    with pytest.raises(ValueError, match="WAL-index"):
        db.validate_dev_cow_completed_generation_projection(
            root, linked_v3_receipt=linked, source_identity=current,
            stable_binding=db.verified_stable_database_binding(),
        )

    assert (database.read_bytes(), wal.read_bytes(), shm.read_bytes()) == before


@pytest.mark.parametrize("defect", ("nonempty_wal", "bad_shm", "listener", "holder"))
def test_completed_cow_restart_rejects_unsafe_sidecar_or_live_owner_zero_write(
    tmp_path, monkeypatch, defect,
):
    from agent.governance import db

    root, database, linked, source, _process, _receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    current = _advance_cow_to_completed_generation(database, root, source)
    wal = Path(str(database) + "-wal")
    shm = Path(str(database) + "-shm")
    wal.write_bytes(b"foreign-frame" if defect == "nonempty_wal" else b"")
    shm.write_bytes(b"tampered" if defect == "bad_shm" else b"\0" * 32768)
    if defect == "listener":
        monkeypatch.setattr(
            db, "_default_cutover_listener_probe",
            lambda port: {"port": port, "listening": True, "pid": 77},
        )
    elif defect == "holder":
        monkeypatch.setattr(
            db, "_assert_no_external_sqlite_holders",
            lambda _path: (_ for _ in ()).throw(RuntimeError("live holder")),
        )
    before = database.read_bytes(), wal.read_bytes(), shm.read_bytes()

    with pytest.raises((RuntimeError, ValueError)):
        db.validate_dev_cow_completed_generation_projection(
            root, linked_v3_receipt=linked, source_identity=current,
            stable_binding=db.verified_stable_database_binding(),
        )

    assert (database.read_bytes(), wal.read_bytes(), shm.read_bytes()) == before


@pytest.mark.parametrize(
    "defect",
    ("unknown_tip", "tip_hash", "forged_root", "nonancestor", "cross_repo"),
)
def test_completed_cow_historical_source_authority_rejects_forgery_zero_write(
    tmp_path, monkeypatch, defect,
):
    from agent.governance import db

    root, database, linked, source, _process, _receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    historical = _advance_cow_to_completed_generation(database, root, source)
    current = _defer_completed_source_and_open_clean_successor(tmp_path, historical)
    monkeypatch.setenv(db.AC_DEV_STORAGE_ROOT_ENV, str(root))
    if defect in {"unknown_tip", "tip_hash", "forged_root"}:
        connection = sqlite3.connect(database)
        tip = json.loads(connection.execute(
            "SELECT value FROM schema_meta "
            "WHERE key='governance_world_source_tip_json'"
        ).fetchone()[0])
        if defect == "unknown_tip":
            tip["commit"] = "f" * 40
        elif defect == "tip_hash":
            tip["source_sha256"] = "sha256:" + "f" * 64
        else:
            forged = tmp_path / "forged-provenance-root"
            forged.mkdir()
            tip["root"] = str(forged.resolve())
        encoded = json.dumps(tip, sort_keys=True, separators=(",", ":"))
        connection.executemany(
            "UPDATE schema_meta SET value=? WHERE key=?",
            (
                (encoded, "governance_world_source_tip_json"),
                (db._world_source_tip_hash(tip),
                 "governance_world_source_tip_sha256"),
            ),
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
    elif defect == "nonancestor":
        successor = Path(current["root"])
        orphan = subprocess.run(
            ["git", "commit-tree", current["tree"], "-m", "unrelated current"],
            cwd=successor, check=True, capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "update-ref", "refs/heads/codex/ac-dev", orphan,
             current["commit"]], cwd=successor, check=True,
        )
        current = {**current, "commit": orphan}
    else:
        foreign = tmp_path / "foreign-source-repository"
        subprocess.run(
            ["git", "clone", "--no-local", str(historical["root"]), str(foreign)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "-B", "codex/ac-dev", "origin/codex/ac-dev"],
            cwd=foreign, check=True, capture_output=True,
        )
        current = {
            **current,
            "root": str(foreign.resolve()),
            "commit": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=foreign, check=True,
                capture_output=True, text=True,
            ).stdout.strip(),
            "tree": subprocess.run(
                ["git", "rev-parse", "HEAD^{tree}"], cwd=foreign, check=True,
                capture_output=True, text=True,
            ).stdout.strip(),
            "source_sha256": "sha256:" + hashlib.sha256(
                (foreign / "agent" / "cli.py").read_bytes()
            ).hexdigest(),
        }
    before = database.read_bytes()

    with pytest.raises((OSError, RuntimeError, ValueError)):
        db.validate_dev_cow_completed_generation_projection(
            root, linked_v3_receipt=linked, source_identity=current,
            stable_binding=db.verified_stable_database_binding(),
        )

    assert database.read_bytes() == before


def test_completed_cow_basic_restart_preserves_database_and_refreshes_only_basic_receipt(
    tmp_path, monkeypatch,
):
    from agent.governance import db

    root, database, candidate, stable, launch_path = (
        _completed_cow_basic_restart_fixture(tmp_path, monkeypatch)
    )
    database_before = database.read_bytes()
    logical_before = db._database_logical_sha256(database)
    launch_before = launch_path.read_bytes()
    archive_before = {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted((root / "archive").rglob("*"))
        if path.is_file()
    }
    process = {"pid": os.getpid(), "start_identity": f"pid:{os.getpid()}:cli-bootstrap"}

    binding = db.bootstrap_dev_governance_store(
        root, source_identity=candidate, process_identity=process,
    )

    assert binding["restart_safe"] is True
    assert binding["source_upgraded"] is False
    assert binding["current_process_identity"] != process
    assert database.read_bytes() == database_before
    assert db._database_logical_sha256(database) == logical_before
    assert launch_path.read_bytes() == launch_before
    assert {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted((root / "archive").rglob("*"))
        if path.is_file()
    } == archive_before

    server_sha256 = "sha256:" + hashlib.sha256(
        (Path(candidate["root"]) / "agent" / "governance" / "server.py").read_bytes()
    ).hexdigest()
    refreshed = db.write_dev_launch_receipt(
        root, stable_shared_volume=stable,
        source_sha256=server_sha256, port=40008,
    )
    assert refreshed["source_sha256"] == server_sha256
    assert launch_path.read_bytes() != launch_before
    assert database.read_bytes() == database_before
    assert db._dev_storage_root(create=False) == root
    db.release_dev_runtime_writer_lease(root)


def test_first_cow_custody_still_required_with_committed_business_wal(
    tmp_path, monkeypatch,
):
    from agent.governance import db

    root, database, linked, source, _process, _receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    code = """
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('PRAGMA journal_mode=WAL')
c.execute('PRAGMA wal_autocheckpoint=0')
c.execute('UPDATE backlog_bugs SET title=? WHERE bug_id=?',
          ('committed business data', 'AC-0000'))
c.commit()
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", code, str(database)], check=True)
    wal = Path(str(database) + "-wal")
    assert wal.stat().st_size > 32
    database_before, wal_before = database.read_bytes(), wal.read_bytes()
    assert db._select_dev_cow_generation_phase(
        root, linked_v3_receipt=linked, source_identity=source,
        stable_binding=db.verified_stable_database_binding(),
    ) is db._DevCowGenerationPhase.FIRST_ISSUANCE
    with pytest.raises(ValueError, match="requires live first-start custody"):
        db._validate_dev_cow_completed_basic_restart(
            root, source_identity=source,
            stable_binding=db.verified_stable_database_binding(),
        )
    assert database.read_bytes() == database_before
    assert wal.read_bytes() == wal_before
    assert str(database.absolute()) not in db._DEV_DATABASE_WRITER_LEASES


@pytest.mark.parametrize("source_descendant", (False, True))
def test_completed_cow_basic_restart_recovers_committed_wal_and_closed_observer(
    tmp_path, monkeypatch, source_descendant,
):
    """A normal observer close in WAL survives restart of a completed world."""
    from agent.governance import db

    root, database, historical, _stable, launch_path = (
        _completed_cow_basic_restart_fixture(tmp_path, monkeypatch)
    )
    candidate = historical
    if source_descendant:
        candidate = _defer_completed_source_and_open_clean_successor(
            tmp_path, historical,
        )
        monkeypatch.setattr(
            db, "__file__",
            str(Path(candidate["root"]) / "agent" / "governance" / "db.py"),
        )
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO observer_sessions (session_id,project_id,token_hash,"
        "registered_at,last_seen_at) VALUES (?,?,?,?,?)",
        ("obs-wal-restart", "aming-claw", "fixture-observer-token-hash",
         "2026-09-04T16:00:00Z", "2026-09-04T16:00:00Z"),
    )
    connection.execute(
        "INSERT INTO contract_runtime_executions (contract_execution_id,"
        "project_id,backlog_id,contract_id,version,revision,"
        "execution_state_revision,record_json,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("cex-wal-restart", "aming-claw", "AC-0000", "direct-main", "1", "3",
         5, '{"completed_lines":["implementation"]}', "2026-09-04T16:00:00Z",
         "2026-09-04T16:00:00Z"),
    )
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    database_before = database.read_bytes()
    receipt_before = launch_path.read_bytes()
    archive_before = {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted((root / "archive").rglob("*")) if path.is_file()
    }
    record = json.dumps({
        "completed_lines": ["implementation", "qa", "reconcile", "close_ready"],
        "candidate": historical["commit"], "state": "completed",
    }, sort_keys=True)
    # SQLite owns the WAL format.  A real stopped subprocess commits terminal
    # evidence, then the observer close, without closing/checkpointing its DB.
    code = """
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('PRAGMA journal_mode=WAL')
c.execute('PRAGMA wal_autocheckpoint=0')
c.execute('UPDATE backlog_bugs SET status=?, "commit"=?, fixed_at=?, updated_at=? '
          'WHERE bug_id=?', ('FIXED', sys.argv[2], '2026-09-04T16:06:16Z',
                            '2026-09-04T16:06:16Z', 'AC-0000'))
c.execute('UPDATE contract_runtime_executions SET execution_state_revision=9, '
          'record_json=?, updated_at=? WHERE contract_execution_id=?',
          (sys.argv[3], '2026-09-04T16:06:16Z', 'cex-wal-restart'))
c.executemany('INSERT INTO task_timeline_events '
              '(project_id,backlog_id,task_id,event_type,status,commit_sha,created_at) '
              'VALUES (?,?,?,?,?,?,?)',
              [('aming-claw', 'AC-0000', 'cex-wal-restart', event, 'passed',
                sys.argv[2], '2026-09-04T16:06:16Z')
               for event in ('qa.independent_verification', 'observer.reconcile',
                             'observer.close_ready', 'backlog.close')])
c.commit()
c.execute('UPDATE observer_sessions SET status=?, closed_at=?, last_seen_at=? '
          'WHERE session_id=?', ('closed', '2026-09-04T16:08:25Z',
                                '2026-09-04T16:08:25Z', 'obs-wal-restart'))
c.commit()
os._exit(0)
"""
    subprocess.run(
        [sys.executable, "-c", code, str(database), historical["commit"], record],
        check=True,
    )
    wal = Path(str(database) + "-wal")
    assert wal.is_file() and wal.stat().st_size > 32
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        expected_projection = db._sqlite_logical_projection(connection)
        assert connection.execute(
            "SELECT status,closed_at FROM observer_sessions WHERE session_id=?",
            ("obs-wal-restart",),
        ).fetchone() == ("closed", "2026-09-04T16:08:25Z")
    finally:
        connection.close()
    assert database.read_bytes() == database_before
    immutable = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
    try:
        assert immutable.execute(
            "SELECT status FROM observer_sessions WHERE session_id=?",
            ("obs-wal-restart",),
        ).fetchone() == ("active",)
        assert db._sqlite_logical_projection(immutable) != expected_projection
    finally:
        immutable.close()
    logical_before = db._database_logical_sha256(database)
    recoveries = []
    original_recover = db._recover_verified_existing_dev_sqlite

    def recover_under_existing_lease(recovery_root, recovery_database):
        assert recovery_root == root and recovery_database == database
        db._validate_dev_cow_basic_restart_writer_lease(database)
        assert wal.stat().st_size > 32
        recoveries.append(database)
        return original_recover(recovery_root, recovery_database)

    monkeypatch.setattr(
        db, "_recover_verified_existing_dev_sqlite", recover_under_existing_lease,
    )
    binding = db.bootstrap_dev_governance_store(
        root, source_identity=candidate,
        process_identity={
            "pid": os.getpid(), "start_identity": f"pid:{os.getpid()}:cli-bootstrap",
        },
    )

    assert recoveries == [database]
    assert binding["restart_safe"] is True
    assert binding["source_upgraded"] is False
    assert binding["source_tip_identity"] == {
        key: historical[key] for key in db._DEV_SOURCE_TIP_KEYS
    }
    assert binding["current_process_identity"]["pid"] == 42
    assert not wal.exists() or wal.stat().st_size == 0
    assert database.read_bytes() != database_before  # Expected SQLite checkpoint.
    assert db._database_logical_sha256(database) == logical_before
    connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
    try:
        assert db._sqlite_logical_projection(connection) == expected_projection
        assert connection.execute(
            'SELECT status,"commit",fixed_at FROM backlog_bugs WHERE bug_id=?',
            ("AC-0000",),
        ).fetchone() == ("FIXED", historical["commit"], "2026-09-04T16:06:16Z")
        assert connection.execute(
            "SELECT execution_state_revision,record_json FROM contract_runtime_executions "
            "WHERE contract_execution_id=?", ("cex-wal-restart",),
        ).fetchone() == (9, record)
        assert connection.execute(
            "SELECT status,closed_at FROM observer_sessions WHERE session_id=?",
            ("obs-wal-restart",),
        ).fetchone() == ("closed", "2026-09-04T16:08:25Z")
    finally:
        connection.close()
    assert launch_path.read_bytes() == receipt_before
    assert {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted((root / "archive").rglob("*")) if path.is_file()
    } == archive_before
    db.release_dev_runtime_writer_lease(root)


def test_completed_cow_basic_restart_returns_before_normal_sqlite_connect(
    tmp_path, monkeypatch,
):
    from agent.governance import db

    root, database, historical, _stable, launch_path = (
        _completed_cow_basic_restart_fixture(tmp_path, monkeypatch)
    )
    current = _defer_completed_source_and_open_clean_successor(
        tmp_path, historical,
    )
    monkeypatch.setattr(
        db, "__file__",
        str(Path(current["root"]) / "agent" / "governance" / "db.py"),
    )
    wal = Path(str(database) + "-wal")
    shm = Path(str(database) + "-shm")
    wal.write_bytes(b"")
    shm.write_bytes(_real_fresh_empty_wal_index_bytes(tmp_path, database))
    artifacts_before = db._dev_cow_basic_restart_artifact_snapshot(database)
    launch_before = launch_path.read_bytes()
    original_classifier = db._validate_dev_cow_completed_basic_restart
    original_connect = db.sqlite3.connect
    original_upgrade = db._verify_dev_source_upgrade
    upgrade_modes = []
    classifier_calls = 0

    def guarded_upgrade(previous, candidate, **kwargs):
        upgrade_modes.append(kwargs.get("historical_worktree_advisory", False))
        if not kwargs.get("historical_worktree_advisory", False):
            raise AssertionError("non-advisory upgrade must not run")
        return original_upgrade(previous, candidate, **kwargs)

    def classify_then_close_sqlite(*args, **kwargs):
        nonlocal classifier_calls
        result = original_classifier(*args, **kwargs)
        classifier_calls += 1
        if classifier_calls == 2:
            monkeypatch.setattr(
                db.sqlite3, "connect",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("normal SQLite connect ran after classifier")
                ),
            )
        return result

    monkeypatch.setattr(db, "_verify_dev_source_upgrade", guarded_upgrade)
    monkeypatch.setattr(
        db, "_validate_dev_cow_completed_basic_restart",
        classify_then_close_sqlite,
    )
    process = {
        "pid": os.getpid(),
        "start_identity": f"pid:{os.getpid()}:cli-bootstrap",
    }

    binding = db.bootstrap_dev_governance_store(
        root, source_identity=current, process_identity=process,
    )

    monkeypatch.setattr(db.sqlite3, "connect", original_connect)
    assert classifier_calls == 2
    assert upgrade_modes and all(upgrade_modes)
    assert binding["restart_safe"] is True
    assert binding["source_upgraded"] is False
    assert binding["source_tip_identity"] == {
        key: historical[key] for key in db._DEV_SOURCE_TIP_KEYS
    }
    assert binding["current_process_identity"] != process
    assert binding["database_identity"]["device"] == database.stat().st_dev
    assert binding["database_identity"]["inode"] == database.stat().st_ino
    assert binding["genesis_sha256"] == binding["database_identity"]["genesis_sha256"]
    assert db._dev_cow_basic_restart_artifact_snapshot(database) == artifacts_before
    assert launch_path.read_bytes() == launch_before
    lease = db._DEV_DATABASE_WRITER_LEASES[str(database.absolute())]
    assert lease["database_device"] == database.stat().st_dev
    assert lease["database_inode"] == database.stat().st_ino
    db.release_dev_runtime_writer_lease(root)


def test_completed_cow_basic_restart_early_return_failure_releases_new_lease(
    tmp_path, monkeypatch,
):
    from agent.governance import db

    root, database, candidate, _stable, launch_path = (
        _completed_cow_basic_restart_fixture(tmp_path, monkeypatch)
    )
    original_classifier = db._validate_dev_cow_completed_basic_restart
    original_connect = db.sqlite3.connect
    before = db._dev_cow_basic_restart_artifact_snapshot(database)
    launch_before = launch_path.read_bytes()
    classifier_calls = 0

    def classify_with_stale_snapshot(*args, **kwargs):
        nonlocal classifier_calls
        result = original_classifier(*args, **kwargs)
        classifier_calls += 1
        if classifier_calls < 2:
            return result
        result = {**result, "artifacts": {**result["artifacts"]}}
        result["artifacts"]["database"] = {
            **result["artifacts"]["database"], "sha256": "sha256:" + "f" * 64,
        }
        monkeypatch.setattr(
            db.sqlite3, "connect",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("normal SQLite connect ran after classifier")
            ),
        )
        return result

    monkeypatch.setattr(
        db, "_validate_dev_cow_completed_basic_restart",
        classify_with_stale_snapshot,
    )
    with pytest.raises(ValueError, match="artifacts changed under lease"):
        db.bootstrap_dev_governance_store(
            root, source_identity=candidate,
            process_identity={
                "pid": os.getpid(),
                "start_identity": f"pid:{os.getpid()}:cli-bootstrap",
            },
        )

    monkeypatch.setattr(db.sqlite3, "connect", original_connect)
    assert classifier_calls == 2
    assert str(database.absolute()) not in db._DEV_DATABASE_WRITER_LEASES
    assert db._dev_cow_basic_restart_artifact_snapshot(database) == before
    assert launch_path.read_bytes() == launch_before


@pytest.mark.parametrize(
    "attack",
    ("source", "linked_receipt", "basic_receipt", "lease_owner"),
)
def test_completed_cow_basic_restart_lease_window_cas_rejects_exact_drift(
    tmp_path, monkeypatch, attack,
):
    from agent.governance import db

    root, database, candidate, _stable, launch_path = (
        _completed_cow_basic_restart_fixture(tmp_path, monkeypatch)
    )
    before_artifacts = db._dev_cow_basic_restart_artifact_snapshot(database)
    basic_before = launch_path.read_bytes()
    classified = []
    original_classifier = db._validate_dev_cow_completed_basic_restart
    original_acquire = db.acquire_dev_runtime_writer_lease
    attacked_path = None
    attacked_bytes = None

    def capture_classifier(*args, **kwargs):
        result = original_classifier(*args, **kwargs)
        classified.append(result)
        return result

    def acquire_then_attack(storage_root):
        nonlocal attacked_path, attacked_bytes
        lease = original_acquire(storage_root)
        assert classified
        evidence = classified[-1]
        if attack == "source":
            attacked_path = Path(candidate["root"]) / "lease-window-drift.txt"
            attacked_bytes = b"drift\n"
            attacked_path.write_bytes(attacked_bytes)
        elif attack == "linked_receipt":
            attacked_path = Path(evidence["linked_v3_receipt"])
            attacked_bytes = attacked_path.read_bytes() + b" "
            attacked_path.write_bytes(attacked_bytes)
        elif attack == "basic_receipt":
            attacked_path = launch_path
            attacked_bytes = basic_before + b" "
            attacked_path.write_bytes(attacked_bytes)
        else:
            with db._DEV_DATABASE_WRITER_LEASES_LOCK:
                db._DEV_DATABASE_WRITER_LEASES[
                    str(database.absolute())
                ]["owner_pid"] = os.getpid() + 1
        return lease

    monkeypatch.setattr(
        db, "_validate_dev_cow_completed_basic_restart", capture_classifier,
    )
    monkeypatch.setattr(
        db, "acquire_dev_runtime_writer_lease", acquire_then_attack,
    )

    with pytest.raises((OSError, RuntimeError, ValueError)):
        db.bootstrap_dev_governance_store(
            root, source_identity=candidate,
            process_identity={
                "pid": os.getpid(),
                "start_identity": f"pid:{os.getpid()}:cli-bootstrap",
            },
        )

    assert db._dev_cow_basic_restart_artifact_snapshot(database) == before_artifacts
    assert str(database.absolute()) not in db._DEV_DATABASE_WRITER_LEASES
    if attacked_path is not None:
        assert attacked_path.read_bytes() == attacked_bytes
    if attack != "basic_receipt":
        assert launch_path.read_bytes() == basic_before


@pytest.mark.parametrize(
    "defect",
    ("receipt_missing", "successor", "custody", "world", "schema", "source", "listener"),
)
def test_completed_cow_basic_restart_rejects_invalid_evidence_without_new_mutation(
    tmp_path, monkeypatch, defect,
):
    from agent.governance import db

    root, database, candidate, _stable, launch_path = (
        _completed_cow_basic_restart_fixture(tmp_path, monkeypatch)
    )
    if defect == "receipt_missing":
        launch_path.unlink()
    elif defect == "successor":
        archive = root / db.AC_DEV_COW_SUCCESSOR_ARCHIVE
        (archive / f"{db.AC_DEV_COW_SUCCESSOR_PREFIX}.{'f' * 64}.json").write_text(
            "{}", encoding="utf-8",
        )
    elif defect in {"custody", "world", "schema"}:
        connection = sqlite3.connect(database)
        if defect == "custody":
            connection.execute(
                "UPDATE schema_meta SET value='{}' "
                "WHERE key='governance_world_current_process_json'"
            )
        elif defect == "world":
            connection.execute(
                "UPDATE schema_meta SET value='foreign' "
                "WHERE key='governance_world_id'"
            )
        else:
            connection.execute("DROP TABLE parallel_branch_runtime_contexts")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
    elif defect == "source":
        (Path(candidate["root"]) / "untracked-drift.txt").write_text(
            "dirty\n", encoding="utf-8",
        )
    else:
        monkeypatch.setattr(
            db, "_default_cutover_listener_probe",
            lambda port: {"port": port, "listening": True, "pid": 99},
        )

    database_before = database.read_bytes()
    receipt_before = launch_path.read_bytes() if launch_path.exists() else None
    archive_before = {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted((root / "archive").rglob("*"))
        if path.is_file()
    }
    with pytest.raises((OSError, RuntimeError, TypeError, ValueError)):
        db.bootstrap_dev_governance_store(
            root, source_identity=candidate,
            process_identity={
                "pid": os.getpid(),
                "start_identity": f"pid:{os.getpid()}:cli-bootstrap",
            },
        )
    assert database.read_bytes() == database_before
    assert (launch_path.read_bytes() if launch_path.exists() else None) == receipt_before
    assert {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted((root / "archive").rglob("*"))
        if path.is_file()
    } == archive_before
    assert str(database.absolute()) not in db._DEV_DATABASE_WRITER_LEASES


def test_cow_completed_axis_accepts_current_bootstrap_pid_and_new_candidate_bytes(
    tmp_path, monkeypatch,
):
    from agent.governance import db

    root, database, linked, source, _process, receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    stored_source = _advance_cow_to_completed_generation(database, root, source)
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE schema_meta SET value=? WHERE key=?",
        (json.dumps({"pid": 999, "start_identity": "pid:999:cli-bootstrap"}),
         "governance_world_current_process_json"),
    )
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    source_root = Path(stored_source["root"])
    cli_source = source_root / "agent" / "cli.py"
    cli_source.write_text(
        cli_source.read_text(encoding="utf-8") + "# candidate descendant\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "agent/cli.py"], cwd=source_root, check=True)
    subprocess.run(
        ["git", "commit", "-m", "candidate descendant"], cwd=source_root,
        check=True, capture_output=True,
    )
    candidate = {
        **stored_source,
        "commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=source_root, check=True,
            capture_output=True, text=True,
        ).stdout.strip(),
        "tree": subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=source_root, check=True,
            capture_output=True, text=True,
        ).stdout.strip(),
        "source_sha256": "sha256:" + hashlib.sha256(cli_source.read_bytes()).hexdigest(),
    }
    assert candidate["source_sha256"] != stored_source["source_sha256"]
    assert db.validate_dev_cow_completed_generation_projection(
        root, linked_v3_receipt=linked, source_identity=candidate,
        stable_binding=db.verified_stable_database_binding(),
    ) == receipt


def test_cow_generation_phase_selector_is_closed_for_first_and_completed(
    tmp_path, monkeypatch,
):
    from agent.governance import db

    root, database, linked, source, _process, _receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    stable = db.verified_stable_database_binding()
    assert db._select_dev_cow_generation_phase(
        root, linked_v3_receipt=linked, source_identity=source,
        stable_binding=stable,
    ) is db._DevCowGenerationPhase.FIRST_ISSUANCE
    completed_source = _advance_cow_to_completed_generation(
        database, root, source,
    )
    assert db._select_dev_cow_generation_phase(
        root, linked_v3_receipt=linked, source_identity=completed_source,
        stable_binding=stable,
    ) is db._DevCowGenerationPhase.COMPLETED_GENERATION


@pytest.mark.parametrize("semantic_overlay", [False, True])
def test_cow_completed_generation_accepts_exact_graph_overlay_read_only(
    tmp_path, monkeypatch, semantic_overlay,
):
    from agent.governance import db, reconcile_semantic_enrichment as semantic

    root, database, linked, source, _process, receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    completed_source = _advance_cow_to_completed_generation(
        database, root, source,
    )
    connection = sqlite3.connect(database)
    _install_all_graph_owners_for_inventory_test(db, connection)
    if semantic_overlay:
        db.execute_graph_schema_sql(
            connection, semantic.SEMANTIC_STATE_SCHEMA_SQL,
        )
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    changes_before = connection.total_changes
    authority, protected = db._completed_generation_schema_projections(connection)
    assert connection.total_changes == changes_before
    assert authority == db.authority_projection_schema_inventory()
    assert len(authority["inventory"]) == db.AC_AUTHORITY_SCHEMA_INVENTORY_COUNT
    assert protected == receipt["successor"]["protected_inventory"]
    assert db.classify_semantic_state_schema(connection)["owner_state"] == (
        "exact" if semantic_overlay else "absent"
    )
    canonical_graph_rows = {
        (kind, name, table, db._backlog_read_normalized_sql(sql))
        for _owner, _ensure_schema, inventory in db._graph_schema_owner_registry()
        for kind, name, table, sql in inventory
    }
    canonical_authority_rows = {
        tuple(row) for row in db.authority_projection_schema_inventory()["inventory"]
    }
    assert len(canonical_graph_rows) == 104
    assert len(canonical_graph_rows & canonical_authority_rows) == 86
    assert len(canonical_graph_rows - canonical_authority_rows) == 18
    logical_before = db._sqlite_logical_projection(connection)
    connection.close()
    bytes_before = database.read_bytes()
    stable = db.verified_stable_database_binding()

    assert db.validate_dev_cow_completed_generation_projection(
        root, linked_v3_receipt=linked, source_identity=completed_source,
        stable_binding=stable,
    ) == receipt
    assert db._select_dev_cow_generation_phase(
        root, linked_v3_receipt=linked, source_identity=completed_source,
        stable_binding=stable,
    ) is db._DevCowGenerationPhase.COMPLETED_GENERATION

    assert database.read_bytes() == bytes_before
    connection = sqlite3.connect(database)
    try:
        assert db._sqlite_logical_projection(connection) == logical_before
    finally:
        connection.close()


@pytest.mark.parametrize("drift", ["partial", "altered", "unknown"])
def test_completed_generation_graph_overlay_rejects_drift_zero_write(drift):
    from agent.governance import db

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db._ensure_schema(connection)
    db.admit_missing_backlog_read_schema(connection)
    _install_all_graph_owners_for_inventory_test(db, connection)
    if drift == "partial":
        connection.execute("DROP INDEX idx_graph_asset_projection_path")
    elif drift == "altered":
        connection.execute("DROP INDEX idx_graph_asset_projection_path")
        connection.execute(
            "CREATE INDEX idx_graph_asset_projection_path "
            "ON graph_asset_projection(project_id, snapshot_id)"
        )
    else:
        connection.execute(
            "CREATE TABLE graph_unknown_completed_generation(value TEXT)"
        )
    connection.commit()
    inventory_before = db._graph_materialization_inventory(connection)
    changes_before = connection.total_changes

    with pytest.raises(ValueError, match="graph"):
        db._completed_generation_schema_projections(connection)

    assert connection.total_changes == changes_before
    assert db._graph_materialization_inventory(connection) == inventory_before
    connection.close()


def test_cow_completed_phase_selector_builds_source_reference_outside_dev_plane(
    tmp_path, monkeypatch,
):
    from agent.governance import db

    root, database, linked, source, _process, _receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    completed_source = _advance_cow_to_completed_generation(
        database, root, source,
    )
    stable = db.verified_stable_database_binding()
    before = db._durable_database_sha256(database)
    monkeypatch.setenv(db.RUNTIME_PLANE_ENV, db.DEV_RUNTIME_PLANE)

    assert db._select_dev_cow_generation_phase(
        root, linked_v3_receipt=linked, source_identity=completed_source,
        stable_binding=stable,
    ) is db._DevCowGenerationPhase.COMPLETED_GENERATION

    assert os.environ[db.RUNTIME_PLANE_ENV] == db.DEV_RUNTIME_PLANE
    assert db._durable_database_sha256(database) == before


def test_cow_completed_phase_selector_rejects_target_schema_mismatch_in_dev_plane(
    tmp_path, monkeypatch,
):
    from agent.governance import db

    root, database, linked, source, _process, _receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    completed_source = _advance_cow_to_completed_generation(
        database, root, source,
    )
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE parallel_branch_runtime_contexts")
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    before = db._durable_database_sha256(database)
    monkeypatch.setenv(db.RUNTIME_PLANE_ENV, db.DEV_RUNTIME_PLANE)

    with pytest.raises(ValueError, match="source schema inventory mismatch"):
        db._select_dev_cow_generation_phase(
            root, linked_v3_receipt=linked, source_identity=completed_source,
            stable_binding=db.verified_stable_database_binding(),
        )

    assert os.environ[db.RUNTIME_PLANE_ENV] == db.DEV_RUNTIME_PLANE
    assert db._durable_database_sha256(database) == before


def test_cow_generation_phase_selector_rejects_ambiguous_and_neither(
    tmp_path, monkeypatch,
):
    from agent.governance import db

    root, database, linked, source, _process, _receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    stable = db.verified_stable_database_binding()
    before = db._durable_database_sha256(database)
    monkeypatch.setattr(
        db, "_validated_dev_cow_completed_generation_axis", lambda *_a, **_k: {},
    )
    with pytest.raises(ValueError, match="ambiguous"):
        db._select_dev_cow_generation_phase(
            root, linked_v3_receipt=linked, source_identity=source,
            stable_binding=stable,
        )
    assert db._durable_database_sha256(database) == before
    monkeypatch.setattr(
        db, "_validated_dev_cow_completed_generation_axis",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("not completed")),
    )
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE schema_meta SET value='{}' "
        "WHERE key='governance_world_current_process_json'"
    )
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    neither_before = db._durable_database_sha256(database)
    with pytest.raises(ValueError, match="unrecognized"):
        db._select_dev_cow_generation_phase(
            root, linked_v3_receipt=linked, source_identity=source,
            stable_binding=stable,
        )
    assert before != neither_before
    assert db._durable_database_sha256(database) == neither_before


@pytest.mark.parametrize("phase", ("first", "completed"))
def test_cow_phase_validator_error_is_never_fallback_or_masked(
    tmp_path, monkeypatch, phase,
):
    from agent.governance import db

    root, _database, linked, source, _process, _receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    _phase_z_bind_first_start_runtime(tmp_path, monkeypatch, root)
    selected = (
        db._DevCowGenerationPhase.FIRST_ISSUANCE
        if phase == "first"
        else db._DevCowGenerationPhase.COMPLETED_GENERATION
    )
    monkeypatch.setattr(db, "_select_dev_cow_generation_phase", lambda *_a, **_k: selected)
    calls = []

    def reject(name):
        calls.append(name)
        raise ValueError(name + " validator marker")

    monkeypatch.setattr(
        db, "validate_dev_cow_successor_preimage",
        lambda *_a, **_k: reject("first"),
    )
    monkeypatch.setattr(
        db, "validate_dev_cow_completed_generation_projection",
        lambda *_a, **_k: reject("completed"),
    )
    with pytest.raises(ValueError, match=phase + " validator marker"):
        db.validate_dev_preimage_only(
            root, source_identity=source, linked_v3_receipt=linked,
        )
    assert calls == [phase]


def test_cow_phase_dispatch_has_no_exception_text_control_flow():
    from agent.governance import db
    import inspect

    source = inspect.getsource(db._dev_storage_root)
    assert "except ValueError" not in source
    assert "issuance schema_meta mismatch" not in source
    assert "_select_dev_cow_generation_phase" in source


def test_dev_preimage_identity_is_full_v2_from_same_immutable_connection(
    tmp_path, monkeypatch,
):
    from agent.governance import db

    root, database, linked, source, _process, _receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    _phase_z_bind_first_start_runtime(tmp_path, monkeypatch, root)
    monkeypatch.delenv(db.RUNTIME_PLANE_ENV, raising=False)
    before = db._durable_database_sha256(database)
    companions = tuple(Path(str(database) + suffix) for suffix in ("-wal", "-shm"))
    assert not any(path.exists() for path in companions)

    preimage = db.validate_dev_preimage_only(
        root, source_identity=source, linked_v3_receipt=linked,
    )
    uri = "file:" + str(database) + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        expected = db.canonical_ac_database_identity(connection)
        original_connect = db.sqlite3.connect
        monkeypatch.setattr(
            db.sqlite3, "connect",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("canonical identity opened a second connection")
            ),
        )
        assert db.canonical_ac_database_identity(connection) == expected
        monkeypatch.setattr(db.sqlite3, "connect", original_connect)
    finally:
        connection.close()

    assert preimage["database_identity"] == expected
    assert tuple(expected) == (
        "schema_version", "world_id", "project_id", "device", "inode",
        "relative_path_sha256", "genesis_sha256",
    )
    assert expected["schema_version"] == "ac_governance_database_identity.v2"
    assert expected["world_id"] == "ac-dev"
    assert expected["project_id"] == "aming-claw"
    assert db._durable_database_sha256(database) == before
    assert not any(path.exists() for path in companions)


def test_cow_completed_generation_transitions_to_new_child_custody(
    tmp_path, monkeypatch,
):
    from agent.governance import db

    root, database, linked, source, _process, receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    completed_source = _advance_cow_to_completed_generation(
        database, root, source,
    )
    _phase_z_bind_first_start_runtime(tmp_path, monkeypatch, root)
    custody = _phase_z_durable_process(root, completed_source, pid=os.getpid())
    result = db.commit_dev_child_custody(
        root, source_identity=completed_source, process_identity=custody,
        linked_v3_receipt=linked,
    )
    assert result["created"] is False
    assert result["restart_safe"] is True
    assert result["custody_projection"] == custody
    assert result["database_identity"]["device"] == receipt["successor"]["identity"]["device"]
    assert result["database_identity"]["inode"] == receipt["successor"]["identity"]["inode"]
    uri = "file:" + str(database) + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        assert result["database_identity"] == db.canonical_ac_database_identity(
            connection
        )
    finally:
        connection.close()
    assert result["database_identity"]["schema_version"] == (
        "ac_governance_database_identity.v2"
    )
    assert not Path(str(database) + "-wal").exists()
    assert not Path(str(database) + "-shm").exists()
    db.release_dev_runtime_writer_lease(root)


def test_public_linked_v3_prevalidator_admits_real_completed_cow_projection(
    tmp_path, monkeypatch,
):
    from agent.governance import db
    import agent.cli as cli

    root, database, linked, source, _process, _receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    completed_source = _advance_cow_to_completed_generation(
        database, root, source,
    )
    historical = []
    monkeypatch.setattr(
        cli, "_historical_dashboard_bootstrap_adoption",
        lambda value, **_kwargs: historical.append(value),
    )
    monkeypatch.setattr(
        cli, "_validated_historical_admission_source_identity",
        lambda _value: {"cli_source": completed_source},
    )
    digest, linked_receipt = cli._validated_linked_v3_receipt(
        linked, dev_storage=root, database=database,
        database_identity=cli._admission_identity(database),
        source_identity=completed_source,
        durable_start_phase=cli._DURABLE_START_LEGACY_ADOPTION,
    )
    assert digest == "sha256:" + hashlib.sha256(linked.read_bytes()).hexdigest()
    assert linked_receipt["database_identity"]["inode"] != database.stat().st_ino
    assert historical == [root]


@pytest.mark.parametrize(
    "drift", ("path", "inode", "history", "schema_meta", "schema")
)
def test_cow_completed_generation_rejects_drift_before_write(
    tmp_path, monkeypatch, drift,
):
    from agent.governance import db

    root, database, linked, source, _process, _receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    completed_source = _advance_cow_to_completed_generation(
        database, root, source,
    )
    before = db._durable_database_sha256(database)
    validation_root = root
    if drift == "path":
        validation_root = tmp_path / "foreign-dev"
        shutil.copytree(root, validation_root)
    elif drift == "inode":
        replacement = database.with_suffix(".replacement")
        shutil.copyfile(database, replacement)
        os.replace(replacement, database)
    elif drift == "history":
        linked.write_bytes(linked.read_bytes() + b"\n")
    else:
        connection = sqlite3.connect(database)
        if drift == "schema_meta":
            connection.execute(
                "INSERT INTO schema_meta(key,value) VALUES('foreign','value')"
            )
        else:
            connection.execute("CREATE TABLE foreign_generation(value TEXT)")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
    with pytest.raises(ValueError, match="completed generation|successor|schema inventory"):
        db.validate_dev_cow_completed_generation_projection(
            validation_root, linked_v3_receipt=linked,
            source_identity=completed_source,
            stable_binding=db.verified_stable_database_binding(),
        )
    if drift in {"path", "inode", "history"}:
        assert db._durable_database_sha256(database) == before


def test_cow_prebind_api_rejects_caller_asserted_database_sha(
    tmp_path, monkeypatch,
):
    from agent.governance import db

    root, database, linked, source, _process, _receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    custody = _phase_z_durable_process(root, source, pid=42)
    custody["launch_id"] = "2" * 64
    custody["argv"][-1] = custody["launch_id"]
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE schema_meta SET value=? WHERE key=?",
        (json.dumps(custody), "governance_world_current_process_json"),
    )
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    caller_asserted_sha = db._durable_database_sha256(database)

    with pytest.raises(TypeError, match="unexpected keyword"):
        db.validate_dev_cow_successor_preimage(
            root, linked_v3_receipt=linked, source_identity=source,
            stable_binding=db.verified_stable_database_binding(),
            expected_completed_generation_database_sha256=caller_asserted_sha,
        )
    with pytest.raises(TypeError, match="unexpected keyword"):
        db.commit_dev_child_custody(
            root, source_identity=source, process_identity=custody,
            linked_v3_receipt=linked,
            expected_pre_sha256=caller_asserted_sha,
        )
    with pytest.raises(TypeError, match="unexpected keyword"):
        db.validate_dev_preimage_only(
            root, source_identity=source, linked_v3_receipt=linked,
            database_identity={"device": 1, "inode": 2},
        )
    with pytest.raises(TypeError, match="unexpected keyword"):
        db.commit_dev_child_custody(
            root, source_identity=source, process_identity=custody,
            linked_v3_receipt=linked,
            database_identity={"device": 1, "inode": 2},
        )
    assert not hasattr(db, "select_dev_completed_generation_ref")
    assert not hasattr(db, "_dev_durable_ref")


def _phase_z_bind_first_start_runtime(tmp_path, monkeypatch, root):
    from agent.governance import db
    import agent.runtime_plane as runtime_plane
    db._DEV_FIRST_START_CONTEXT = None
    stable_binding = db.verified_stable_database_binding()
    stable_volume = tmp_path / "stable-volume"
    stable_volume.mkdir()
    stable_binding = {**stable_binding, "shared_volume_path": str(stable_volume)}
    monkeypatch.setattr(
        db, "verified_stable_database_binding", lambda **_kwargs: stable_binding,
    )
    monkeypatch.setattr(db, "_revalidate_stable_database_binding", lambda _value: None)
    monkeypatch.setattr(
        runtime_plane, "resolve_ac_dev_storage_root", lambda _stable: root,
    )
    monkeypatch.setenv(db.AC_DEV_STORAGE_ROOT_ENV, str(root))
    monkeypatch.setenv(db.AC_STABLE_SHARED_VOLUME_ENV, str(stable_volume))


def test_cow_first_custody_creates_only_live_process_authority(
    tmp_path, monkeypatch,
):
    from agent.governance import db
    root, database, linked, source, _process, _receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    _phase_z_bind_first_start_runtime(tmp_path, monkeypatch, root)
    custody = _phase_z_durable_process(root, source, pid=os.getpid())
    first = db.commit_dev_child_custody(
        root, source_identity=source, process_identity=custody,
        linked_v3_receipt=linked,
    )
    assert first["custody_projection"] == custody
    assert db._validate_dev_first_start_context(root) is True
    assert db.validate_dev_preimage_only(
        root, source_identity=source, linked_v3_receipt=linked,
    )["database_sha256"] == first["database_sha256_after"]
    connection = db._connect_existing(database, timeout=1)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        connection.execute(
            "UPDATE schema_meta SET value='forged' WHERE key='schema_version'"
        )
    connection.close()
    import copy
    import pickle
    context = db._DEV_FIRST_START_CONTEXT
    with pytest.raises(TypeError):
        copy.copy(context)
    with pytest.raises(TypeError):
        copy.deepcopy(context)
    with pytest.raises(TypeError):
        pickle.dumps(context)
    with pytest.raises(TypeError):
        json.dumps(context)
    child = subprocess.run(
        [sys.executable, "-c", "from agent.governance import db; "
         "print(db._DEV_FIRST_START_CONTEXT is None)"],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True,
        check=True,
    )
    assert child.stdout.strip() == "True"
    db.release_dev_runtime_writer_lease(root)
    with pytest.raises(ValueError, match="issuance schema_meta|phase is unrecognized"):
        db.validate_dev_preimage_only(
            root, source_identity=source, linked_v3_receipt=linked,
        )


@pytest.mark.parametrize(
    "attack", ("pid", "source", "custody_hash", "root_inode"),
)
def test_first_start_context_revalidates_every_live_binding(
    tmp_path, monkeypatch, attack,
):
    from agent.governance import db
    root, database, linked, source, _process, _receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    _phase_z_bind_first_start_runtime(tmp_path, monkeypatch, root)
    custody = _phase_z_durable_process(root, source, pid=os.getpid())
    db.commit_dev_child_custody(
        root, source_identity=source, process_identity=custody,
        linked_v3_receipt=linked,
    )
    context = db._DEV_FIRST_START_CONTEXT
    restored = None
    try:
        if attack == "pid":
            restored = ("pid", context.pid)
            object.__setattr__(context, "pid", context.pid + 1)
        elif attack == "source":
            (Path(source["root"]) / "agent" / "governance" / "server.py").write_text(
                "# changed after context\n", encoding="utf-8",
            )
        elif attack == "custody_hash":
            restored = ("world_custody_sha256", context.world_custody_sha256)
            object.__setattr__(
                context, "world_custody_sha256", "sha256:" + "0" * 64,
            )
        else:
            restored = ("storage_inode", context.storage_inode)
            object.__setattr__(context, "storage_inode", context.storage_inode + 1)
        with pytest.raises(ValueError, match="first-start .*binding"):
            db._validate_dev_first_start_context(root)
    finally:
        if restored is not None:
            object.__setattr__(context, restored[0], restored[1])
        db.release_dev_runtime_writer_lease(root)


def test_first_start_live_context_allows_backlog_and_timeline_dml(
    tmp_path, monkeypatch,
):
    from agent.governance import db

    root, _database, linked, source, _process, _receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    _phase_z_bind_first_start_runtime(tmp_path, monkeypatch, root)
    custody = _phase_z_durable_process(root, source, pid=os.getpid())
    db.commit_dev_child_custody(
        root, source_identity=source, process_identity=custody,
        linked_v3_receipt=linked,
    )
    backlog_id = "AC-LIVE-DML-" + os.urandom(8).hex()
    first = db.get_connection("aming-claw")
    try:
        first.execute(
            "INSERT INTO backlog_bugs(bug_id,created_at,updated_at) VALUES(?,?,?)",
            (backlog_id, "now", "now"),
        )
        first.execute(
            "INSERT INTO task_timeline_events(project_id,backlog_id,event_type,created_at) "
            "VALUES(?,?,?,?)",
            ("aming-claw", backlog_id, "implementation", "now"),
        )
        first.commit()
    finally:
        first.close()

    second = db.get_connection("aming-claw")
    try:
        assert second.execute(
            "SELECT COUNT(*) FROM backlog_bugs WHERE bug_id=?", (backlog_id,)
        ).fetchone()[0] == 1
        assert second.execute(
            "SELECT COUNT(*) FROM task_timeline_events WHERE backlog_id=?",
            (backlog_id,),
        ).fetchone()[0] == 1
    finally:
        second.close()
        db.release_dev_runtime_writer_lease(root)


@pytest.mark.parametrize("attack", ("world", "schema"))
def test_first_start_live_context_rejects_world_and_schema_attacks(
    tmp_path, monkeypatch, attack,
):
    from agent.governance import db

    root, database, linked, source, _process, _receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    _phase_z_bind_first_start_runtime(tmp_path, monkeypatch, root)
    custody = _phase_z_durable_process(root, source, pid=os.getpid())
    db.commit_dev_child_custody(
        root, source_identity=source, process_identity=custody,
        linked_v3_receipt=linked,
    )
    attacker = sqlite3.connect(database)
    try:
        if attack == "world":
            attacker.execute(
                "UPDATE schema_meta SET value='forged-world' "
                "WHERE key='governance_world_id'"
            )
        else:
            attacker.execute("CREATE TABLE shadow_runtime_attack(id TEXT)")
        attacker.commit()
    finally:
        attacker.close()
    try:
        with pytest.raises(ValueError):
            db._validate_dev_first_start_context(root)
    finally:
        db.release_dev_runtime_writer_lease(root)


@pytest.mark.parametrize("failure", ("write", "commit"))
def test_first_cow_custody_transaction_rolls_back_both_failures(
    tmp_path, monkeypatch, failure,
):
    from agent.governance import db
    root, database, linked, source, _process, _receipt = (
        _phase_z_cow_prestart_fixture(tmp_path, monkeypatch)
    )
    before_raw = db._durable_database_sha256(database)
    snapshot = sqlite3.connect(database)
    before_rows = snapshot.execute(
        "SELECT key,value FROM schema_meta ORDER BY key"
    ).fetchall()
    snapshot.close()
    custody = _phase_z_durable_process(root, source, pid=os.getpid())

    class CommitFailure(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("injected commit failure")

    factory = CommitFailure if failure == "commit" else sqlite3.Connection
    db.acquire_dev_runtime_writer_lease(root)
    connection = sqlite3.connect(
        database, isolation_level=None, factory=factory,
    )
    trace = []
    connection.set_trace_callback(trace.append)
    if failure == "write":
        connection.set_authorizer(
            lambda action, *_args: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_UPDATE else sqlite3.SQLITE_OK
        )
    try:
        with pytest.raises(sqlite3.DatabaseError, match="authorized|commit failure"):
            db._commit_first_cow_child_custody(
                connection, root=root, linked_v3_receipt=linked,
                source_identity=source, process_identity=custody,
            )
        assert connection.in_transaction is False
        if failure == "write":
            assert connection.total_changes == 0
        assert any(statement == "ROLLBACK" for statement in trace)
    finally:
        connection.close()
        db.release_dev_runtime_writer_lease(root)
    after = sqlite3.connect(database)
    assert after.execute(
        "SELECT key,value FROM schema_meta ORDER BY key"
    ).fetchall() == before_rows
    after.close()
    assert db._durable_database_sha256(database) == before_raw


def test_ac_dev_cow_successor_public_cli_real_sqlite_create_and_replay(tmp_path, monkeypatch):
    pytest.importorskip("click")
    from click.testing import CliRunner
    from agent.cli import main

    root, _database, backup, operator, linked = _real_cow_successor_cli_fixture(tmp_path, monkeypatch)
    args = ["dev-create-cow-successor-receipt", "--dev-storage-root", str(root),
            "--operator-receipt", str(operator), "--predecessor-backup", str(backup),
            "--linked-v3-receipt", str(linked)]
    first = CliRunner().invoke(main, args)
    assert first.exit_code == 0, first.output
    receipt = Path(json.loads(first.output)["receipt"]); raw = receipt.read_bytes()
    replay = CliRunner().invoke(main, args)
    assert replay.exit_code == 0, replay.output
    assert json.loads(replay.output)["status"] == "already_created"
    assert receipt.read_bytes() == raw


def test_ac_dev_cow_successor_v2_restart_allows_legitimate_fresh_backlog_row(
    tmp_path, monkeypatch
):
    from agent.governance import db

    root, database, backup, operator, linked = _real_cow_successor_cli_fixture(
        tmp_path, monkeypatch
    )
    created = db.create_dev_cow_successor_receipt(
        root,
        operator_receipt=operator,
        predecessor_backup=backup,
        linked_v3_receipt=linked,
    )
    _write_historical_v1_from_v2(created["receipt"])
    receipt_raw = Path(created["receipt"]).read_bytes()
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO backlog_bugs(bug_id,created_at,updated_at,status) "
        "VALUES (?,?,?,?)",
        ("AC-FRESH-AFTER-V2", "2026-08-31", "2026-08-31", "OPEN"),
    )
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db._verify_current_dev_backlog_runtime_invariants(
        connection,
        expected_protected_inventory=json.loads(
            Path(created["receipt"]).read_text(encoding="utf-8")
        )["successor"]["protected_inventory"],
    )
    connection.close()
    for suffix in ("-wal", "-shm"):
        path = Path(str(database) + suffix)
        if path.exists():
            path.unlink()
    monkeypatch.setattr(
        db, "verified_stable_database_binding",
        lambda: (_ for _ in ()).throw(
            AssertionError("current stable binding must not reconstruct issuance")
        ),
    )

    replay = db.validate_dev_cow_successor_receipt(root)
    assert replay["successor"]["row_count"] == 3603
    assert Path(created["receipt"]).read_bytes() == receipt_raw


def test_ac_dev_cow_successor_v2_rejects_recursive_rehashed_tampering(
    tmp_path, monkeypatch
):
    from agent.governance import db

    root, _database, backup, operator, linked = _real_cow_successor_cli_fixture(
        tmp_path, monkeypatch
    )
    created = db.create_dev_cow_successor_receipt(
        root,
        operator_receipt=operator,
        predecessor_backup=backup,
        linked_v3_receipt=linked,
    )
    _write_historical_v1_from_v2(created["receipt"])
    receipt_path = Path(created["receipt"])
    original_raw = receipt_path.read_bytes()
    original = json.loads(original_raw)
    monkeypatch.setattr(
        db, "_reconstruct_dev_cow_successor_payload",
        lambda _root, _receipt: original,
    )

    def changed_scalar(value):
        if value is None:
            return "not-none"
        if isinstance(value, bool):
            return not value
        if isinstance(value, int):
            return float(value)
        return str(value) + "#tampered"

    def recursive_mutations(value, path=()):
        if isinstance(value, dict):
            payload = json.loads(original_raw)
            target = payload
            for part in path:
                target = target[part]
            target["__unexpected_authority__"] = True
            yield payload
            if value:
                payload = json.loads(original_raw)
                target = payload
                for part in path:
                    target = target[part]
                del target[next(iter(value))]
                yield payload
            for key, child in value.items():
                yield from recursive_mutations(child, path + (key,))
        elif isinstance(value, list):
            payload = json.loads(original_raw)
            target = payload
            for part in path:
                target = target[part]
            target.append("__unexpected_authority__")
            yield payload
            if value:
                payload = json.loads(original_raw)
                target = payload
                for part in path:
                    target = target[part]
                del target[0]
                yield payload
            for index, child in enumerate(value):
                yield from recursive_mutations(child, path + (index,))
        else:
            payload = json.loads(original_raw)
            target = payload
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = changed_scalar(value)
            yield payload

    mutation_count = 0
    for payload in recursive_mutations(original):
        mutation_count += 1
        tampered_raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        tampered_path = receipt_path.with_name(
            f"{db.AC_DEV_COW_SUCCESSOR_PREFIX}."
            f"{hashlib.sha256(tampered_raw).hexdigest()}.json"
        )
        receipt_path.unlink()
        tampered_path.write_bytes(tampered_raw)
        try:
            with pytest.raises(ValueError, match="COW successor"):
                db.validate_dev_cow_successor_receipt(root)
        finally:
            tampered_path.unlink()
            receipt_path.write_bytes(original_raw)

    assert mutation_count > 100
    assert json.loads(receipt_path.read_bytes()) == original


def test_ac_dev_cow_successor_v2_rejects_replaced_database_and_adjusted_receipt(
    tmp_path, monkeypatch
):
    from agent.governance import db

    root, database, backup, operator, linked = _real_cow_successor_cli_fixture(
        tmp_path, monkeypatch
    )
    created = db.create_dev_cow_successor_receipt(
        root, operator_receipt=operator, predecessor_backup=backup,
        linked_v3_receipt=linked,
    )
    _write_historical_v1_from_v2(created["receipt"])
    receipt_path = Path(created["receipt"])
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    replacement = database.with_name("replacement.sqlite")
    shutil.copy2(database, replacement)
    os.replace(replacement, database)
    metadata = database.stat(follow_symlinks=False)
    payload["successor"]["identity"].update({
        "device": metadata.st_dev, "inode": metadata.st_ino,
        "nlink": metadata.st_nlink,
    })
    tampered_raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    tampered_path = receipt_path.with_name(
        f"{db.AC_DEV_COW_SUCCESSOR_PREFIX}.{hashlib.sha256(tampered_raw).hexdigest()}.json"
    )
    receipt_path.unlink()
    tampered_path.write_bytes(tampered_raw)

    with pytest.raises(ValueError, match="reconstructed issuance"):
        db.validate_dev_cow_successor_receipt(root)


def test_current_dev_backlog_runtime_invariants_reject_schema_and_generation(
    tmp_path
):
    from agent.governance import db

    database = tmp_path / "current.sqlite"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    db._configure_connection(connection, busy_timeout=1000)
    db._ensure_schema(connection)
    db.ensure_backlog_read_schema(connection)
    protected = db.backlog_read_schema_protected_inventory(connection)
    db._verify_current_dev_backlog_runtime_invariants(
        connection, expected_protected_inventory=protected
    )
    connection.execute(
        "UPDATE dashboard_backlog_cache_generation SET generation=0 "
        "WHERE resource=?",
        (db.BACKLOG_READ_SCHEMA_RESOURCE,),
    )
    connection.commit()
    with pytest.raises(ValueError, match="managed generation"):
        db._verify_current_dev_backlog_runtime_invariants(
            connection, expected_protected_inventory=protected
        )
    connection.execute(
        "UPDATE dashboard_backlog_cache_generation SET generation=1 "
        "WHERE resource=?",
        (db.BACKLOG_READ_SCHEMA_RESOURCE,),
    )
    connection.execute("DROP TRIGGER trg_dashboard_backlog_cache_update")
    connection.commit()
    with pytest.raises(ValueError, match="managed generation"):
        db._verify_current_dev_backlog_runtime_invariants(
            connection, expected_protected_inventory=protected
        )
    connection.close()


def test_current_dev_backlog_runtime_invariants_use_historical_phase_z_inventory(
    tmp_path
):
    from agent.governance import db

    database = tmp_path / "phase-z.sqlite"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    db._configure_connection(connection, busy_timeout=1000)
    db._ensure_schema(connection)
    db.admit_missing_authority_projection_schema(connection)
    db.ensure_backlog_read_schema(connection)
    _install_all_graph_owners_for_inventory_test(db, connection)
    db._verify_dev_world_schema_inventory(connection)
    historical = db.backlog_read_schema_protected_inventory(connection)
    db._verify_current_dev_backlog_runtime_invariants(
        connection, expected_protected_inventory=historical
    )
    authority_tables = [
        row[1] for row in db.authority_projection_schema_inventory()["inventory"]
        if row[0] == "table"
    ]
    assert len(authority_tables) >= 6
    connection.close()

    for index, statement in enumerate((
        f'DROP TABLE "{authority_tables[0]}"',
        f'ALTER TABLE "{authority_tables[1]}" ADD COLUMN qa_drift TEXT',
        'CREATE TABLE qa_extra_protected_object(value TEXT)',
    )):
        candidate = tmp_path / f"phase-z-drift-{index}.sqlite"
        shutil.copyfile(database, candidate)
        drifted = sqlite3.connect(candidate)
        drifted.execute(statement)
        drifted.commit()
        with pytest.raises(ValueError, match="protected inventory"):
            db._verify_current_dev_backlog_runtime_invariants(
                drifted, expected_protected_inventory=historical
            )
        drifted.close()


def test_ac_dev_cow_successor_public_cli_wrong_real_schema_is_bounded(tmp_path, monkeypatch):
    pytest.importorskip("click")
    from click.testing import CliRunner
    from agent.cli import main
    from agent.governance import db

    root, database, backup, operator, linked = _real_cow_successor_cli_fixture(tmp_path, monkeypatch)
    connection = sqlite3.connect(database)
    connection.execute("ALTER TABLE backlog_bugs ADD COLUMN hostile TEXT")
    connection.commit(); connection.close()
    result = CliRunner().invoke(main, [
        "dev-create-cow-successor-receipt", "--dev-storage-root", str(root),
        "--operator-receipt", str(operator), "--predecessor-backup", str(backup),
        "--linked-v3-receipt", str(linked)])
    assert result.exit_code != 0
    assert "backlog table ABI mismatch" in result.output
    assert "Traceback" not in result.output
    assert not list((root / db.AC_DEV_COW_SUCCESSOR_ARCHIVE).glob(
        f"{db.AC_DEV_COW_SUCCESSOR_PREFIX}.*.json"
    ))


def test_ac_dev_cow_successor_public_cli_rejects_one_source_object_mismatch(
    tmp_path, monkeypatch,
):
    pytest.importorskip("click")
    from click.testing import CliRunner
    from agent.cli import main
    from agent.governance import db

    root, database, backup, operator, linked = _real_cow_successor_cli_fixture(
        tmp_path, monkeypatch,
    )
    connection = sqlite3.connect(database)
    connection.execute("DROP TRIGGER trg_dashboard_backlog_cache_update")
    connection.execute(
        "CREATE TRIGGER trg_dashboard_backlog_cache_update AFTER UPDATE ON backlog_bugs "
        "BEGIN UPDATE dashboard_backlog_cache_generation SET generation=generation+2 "
        "WHERE resource='backlog'; END"
    )
    connection.commit(); connection.close()
    result = CliRunner().invoke(main, [
        "dev-create-cow-successor-receipt", "--dev-storage-root", str(root),
        "--operator-receipt", str(operator), "--predecessor-backup", str(backup),
        "--linked-v3-receipt", str(linked)])
    assert result.exit_code != 0
    assert "source schema inventory mismatch" in result.output
    assert "Traceback" not in result.output
    assert not (root / db.AC_DEV_COW_SUCCESSOR_ARCHIVE).exists()


def test_ac_dev_cow_successor_public_cli_rejects_self_consistent_foreign_world(
    tmp_path, monkeypatch,
):
    pytest.importorskip("click")
    from click.testing import CliRunner
    from agent.cli import main
    from agent.governance import db

    root, database, backup, operator, linked = _real_cow_successor_cli_fixture(
        tmp_path, monkeypatch,
    )
    foreign = {"schema_version": db.AC_WORLD_GENESIS_SCHEMA, "world_id": "foreign-dev",
               "project_id": "foreign-project", "source_only": True, "rows_copied": 0,
               "database_identity": {"device": backup.stat().st_dev,
                                     "inode": backup.stat().st_ino}}
    foreign_raw = json.dumps(foreign, sort_keys=True, separators=(",", ":"))
    foreign_sha = db._world_genesis_hash(foreign)
    for path in (backup, database):
        connection = sqlite3.connect(path)
        connection.executemany(
            "INSERT OR REPLACE INTO schema_meta(key,value) VALUES (?,?)",
            [("governance_world_id", "foreign-dev"),
             ("governance_world_genesis_json", foreign_raw),
             ("governance_world_genesis_sha256", foreign_sha)],
        )
        connection.commit(); connection.close()
    operator_payload = json.loads(operator.read_text(encoding="utf-8"))
    operator_payload["backup_sha256"] = db._durable_database_sha256(backup)
    operator_payload["target_sha256_before"] = operator_payload["backup_sha256"]
    operator_payload["target_sha256_after"] = db._durable_database_sha256(database)
    operator_raw = json.dumps(operator_payload, sort_keys=True, separators=(",", ":")).encode()
    operator.unlink()
    operator = operator.parent / f"cow-import.{hashlib.sha256(operator_raw).hexdigest()}.json"
    operator.write_bytes(operator_raw)
    result = CliRunner().invoke(main, [
        "dev-create-cow-successor-receipt", "--dev-storage-root", str(root),
        "--operator-receipt", str(operator), "--predecessor-backup", str(backup),
        "--linked-v3-receipt", str(linked)])
    assert result.exit_code != 0
    assert "protected preimage mismatch" in result.output
    assert "Traceback" not in result.output
    archive = root / db.AC_DEV_COW_SUCCESSOR_ARCHIVE
    assert not archive.exists()
    crafted = {"schema_version": db.AC_DEV_COW_SUCCESSOR_SCHEMA, "stage": "completed",
               "project_id": db.AC_PROJECT_ID, "port": 40008, "root": str(root),
               "operator_evidence": {"path": str(operator)},
               "predecessor": {"backup": {"path": str(backup)}},
               "history": {"linked_v3": {"path": str(linked)}}}
    crafted_raw = json.dumps(crafted, sort_keys=True, separators=(",", ":")).encode()
    archive.mkdir(parents=True)
    (archive / f"{db.AC_DEV_COW_SUCCESSOR_PREFIX}.{hashlib.sha256(crafted_raw).hexdigest()}.json").write_bytes(
        crafted_raw
    )
    with pytest.raises(ValueError, match="canonical|COW successor"):
        db.validate_dev_cow_successor_receipt(root)
def test_ac_dev_source_upgrade_rejects_non_descendant_root_branch_db_and_process(tmp_path):
    from agent.governance import db

    root, commit_a = _dev_source_repo(tmp_path)
    storage_root, stable = _canonical_dev_world(tmp_path)
    source_a = {
        "root": str(root.resolve()),
        "branch": "codex/ac-dev",
        "commit": commit_a,
        "source_sha256": "sha256:" + "a" * 64,
    }
    process_a = {"pid": 101, "start_identity": "process-a"}
    first = db.bootstrap_dev_governance_store(
        storage_root,
        source_identity=source_a,
        process_identity=process_a,
    )
    _admit_existing_dev_world(storage_root, stable)
    commit_b = _advance_dev_source(root, "B")
    source_b = {**source_a, "commit": commit_b, "source_sha256": "sha256:" + "b" * 64}

    cases = (
        ({**source_b, "root": str(tmp_path / "other")}, process_a, first["database_identity"]),
        ({**source_b, "branch": "main"}, process_a, first["database_identity"]),
        (source_b, {"pid": 999, "start_identity": "wrong"}, first["database_identity"]),
        (source_b, process_a, {**first["database_identity"], "inode": first["database_identity"]["inode"] + 1}),
    )
    for source, expected_process, expected_database in cases:
        with pytest.raises(ValueError):
            db.bootstrap_dev_governance_store(
                storage_root,
                source_identity=source,
                process_identity={"pid": 202, "start_identity": "process-b"},
                expected_source_tip_sha256=first["source_tip_sha256"],
                expected_previous_process_identity=expected_process,
                expected_database_identity=expected_database,
            )

    subprocess.run(
        ["git", "checkout", "--orphan", "non-descendant"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (root / "source.txt").write_text("orphan\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "orphan"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "branch", "-D", "codex/ac-dev"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "branch", "-m", "codex/ac-dev"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    orphan = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    with pytest.raises(ValueError, match="descendant"):
        db.bootstrap_dev_governance_store(
            storage_root,
            source_identity={**source_a, "commit": orphan, "source_sha256": "sha256:" + "d" * 64},
            process_identity={"pid": 202, "start_identity": "process-b"},
            expected_source_tip_sha256=first["source_tip_sha256"],
            expected_previous_process_identity=process_a,
            expected_database_identity=first["database_identity"],
        )


def test_verified_dev_adoption_recovers_real_committed_wal_without_source_advance(
    tmp_path,
):
    """A stopped, canonical SQLite WAL is checkpointed rather than unlinked."""
    from agent.governance import db

    source_root, commit = _dev_source_repo(tmp_path)
    source = {
        "root": str(source_root.resolve()), "branch": "codex/ac-dev",
        "commit": commit, "source_sha256": "sha256:" + "a" * 64,
    }
    storage_root, stable = _canonical_dev_world(tmp_path)
    first = db.bootstrap_dev_governance_store(
        storage_root, source_identity=source,
        process_identity={"pid": 111, "start_identity": "first"},
    )
    _admit_existing_dev_world(storage_root, stable)
    database = Path(first["database_path"])
    # Exit without closing: this leaves a real committed WAL/SHM pair while
    # avoiding a live holder in the parent process.
    code = (
        "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
        "c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA wal_autocheckpoint=0'); "
        "c.execute(\"INSERT OR REPLACE INTO schema_meta(key,value) VALUES('wal_recovery_probe','committed')\"); "
        "c.commit(); os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", code, str(database)], check=True)
    wal = Path(str(database) + "-wal")
    shm = Path(str(database) + "-shm")
    assert wal.is_file() and wal.stat().st_size > 32
    assert shm.is_file()
    receipt_before = (storage_root / db.AC_DEV_LAUNCH_RECEIPT_NAME).read_bytes()

    adopted = db.bootstrap_dev_governance_store(
        storage_root, source_identity=source,
        process_identity={"pid": 222, "start_identity": "restart"},
        expected_source_tip_sha256=first["source_tip_sha256"],
        expected_previous_process_identity={"pid": 111, "start_identity": "first"},
        expected_database_identity=first["database_identity"],
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='wal_recovery_probe'"
        ).fetchone()[0] == "committed"
    assert adopted["source_upgraded"] is False
    assert adopted["source_tip_sha256"] == first["source_tip_sha256"]
    assert (storage_root / db.AC_DEV_LAUNCH_RECEIPT_NAME).read_bytes() == receipt_before


@pytest.mark.parametrize("defect", ["busy", "corrupt", "symlink"])
def test_verified_dev_adoption_failures_do_not_advance_source_or_receipt(
    tmp_path, monkeypatch, defect
):
    from agent.governance import db

    source_root, commit = _dev_source_repo(tmp_path)
    source = {
        "root": str(source_root.resolve()), "branch": "codex/ac-dev",
        "commit": commit, "source_sha256": "sha256:" + "b" * 64,
    }
    storage_root, stable = _canonical_dev_world(tmp_path)
    first = db.bootstrap_dev_governance_store(
        storage_root, source_identity=source,
        process_identity={"pid": 333, "start_identity": "first"},
    )
    _admit_existing_dev_world(storage_root, stable)
    database = Path(first["database_path"])
    receipt_path = storage_root / db.AC_DEV_LAUNCH_RECEIPT_NAME
    receipt_before = receipt_path.read_bytes()
    if defect == "busy":
        monkeypatch.setattr(
            db, "_assert_no_external_sqlite_holders",
            lambda _database: (_ for _ in ()).throw(RuntimeError("external holders")),
        )
    elif defect == "corrupt":
        database.write_bytes(b"not a sqlite database")
    elif defect == "symlink":
        outside = tmp_path / "outside-wal"
        outside.write_bytes(b"outside")
        Path(str(database) + "-wal").symlink_to(outside)
    with pytest.raises((RuntimeError, ValueError)):
        db.bootstrap_dev_governance_store(
            storage_root, source_identity=source,
            process_identity={"pid": 444, "start_identity": "restart"},
            expected_source_tip_sha256=first["source_tip_sha256"],
            expected_previous_process_identity={"pid": 333, "start_identity": "first"},
            expected_database_identity=first["database_identity"],
        )
    assert receipt_path.read_bytes() == receipt_before
    # Failures before source upgrade cannot advance source-tip provenance.
    if defect != "corrupt":
        if defect == "symlink":
            # Test-only fixture cleanup after the fail-closed assertion; the
            # runtime itself never unlinks an untrusted companion.
            Path(str(database) + "-wal").unlink()
        with sqlite3.connect(database) as connection:
            meta = dict(connection.execute("SELECT key, value FROM schema_meta"))
        assert meta["governance_world_source_tip_sha256"] == first["source_tip_sha256"]


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_verified_dev_adoption_rejects_real_atomic_sidecar_replacement(
    tmp_path, suffix
):
    """A post-checkpoint `os.replace` cannot masquerade as SQLite recovery.

    This is deliberately a real filesystem replacement, not a monkeypatch of
    the final identity assertion.  The same test is RED on 707cd because its
    final assertion only bound root/database identities.
    """
    from agent.governance import db

    source_root, commit = _dev_source_repo(tmp_path)
    source = {
        "root": str(source_root.resolve()), "branch": "codex/ac-dev",
        "commit": commit, "source_sha256": "sha256:" + "c" * 64,
    }
    storage_root, stable = _canonical_dev_world(tmp_path)
    first = db.bootstrap_dev_governance_store(
        storage_root, source_identity=source,
        process_identity={"pid": 555, "start_identity": "first"},
    )
    _admit_existing_dev_world(storage_root, stable)
    database = Path(first["database_path"])
    receipt_path = storage_root / db.AC_DEV_LAUNCH_RECEIPT_NAME
    receipt_before = receipt_path.read_bytes()
    code = (
        "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
        "c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA wal_autocheckpoint=0'); "
        "c.execute(\"INSERT OR REPLACE INTO schema_meta(key,value) VALUES('atomic_replace_probe','committed')\"); "
        "c.commit(); os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", code, str(database)], check=True)
    companion = Path(str(database) + suffix)
    assert companion.is_file() and not companion.is_symlink()
    replacement = tmp_path / f"replacement{suffix}"
    replacement.write_bytes(companion.read_bytes())
    old_stat = companion.stat(follow_symlinks=False)

    with pytest.raises(ValueError, match="companion identity changed"):
        db._recover_verified_existing_dev_sqlite(
            storage_root,
            database,
            _after_checkpoint_for_test=lambda: os.replace(replacement, companion),
        )
    assert companion.is_file()
    new_stat = companion.stat(follow_symlinks=False)
    assert (new_stat.st_dev, new_stat.st_ino) != (old_stat.st_dev, old_stat.st_ino)
    assert receipt_path.read_bytes() == receipt_before
    # Recovery fails before the bootstrap source-tip write, so no provenance
    # can advance even though the filesystem attack raced after checkpoint.
    companion.unlink()  # test-only hostile artifact cleanup
    with sqlite3.connect(database) as connection:
        meta = dict(connection.execute("SELECT key, value FROM schema_meta"))
    assert meta["governance_world_source_tip_sha256"] == first["source_tip_sha256"]


def test_ac_dev_cutover_preflight_activation_idempotency_and_rollback(tmp_path):
    from agent.governance import db

    root, commit = _dev_source_repo(tmp_path)
    source = {
        "root": str(root.resolve()),
        "branch": "codex/ac-dev",
        "commit": commit,
        "source_sha256": "sha256:" + "e" * 64,
    }
    process = {"pid": 404, "start_identity": "cutover-operator"}
    storage_root, _ = _canonical_dev_world(tmp_path)
    dev = db.bootstrap_dev_governance_store(
        storage_root,
        source_identity=source,
        process_identity=process,
    )
    legacy_size = 2 * 1024 * 1024
    legacy = tmp_path / "legacy" / "governance.db"
    legacy.parent.mkdir()
    with legacy.open("wb") as handle:
        handle.truncate(legacy_size)

    def listener_probe(port):
        if port == 40000:
            return {
                "port": 40000,
                "listening": True,
                "pid": 700,
                "process_start_identity": "stable-700",
                "source_commit": "f" * 40,
            }
        return {
            "port": 40008,
            "listening": False,
            "pid": 0,
            "process_start_identity": "",
            "source_commit": "",
        }

    preflight = db.preflight_dev_world_cutover(
        legacy_database_path=legacy,
        storage_root=storage_root,
        source_identity=source,
        process_identity=process,
        expected_dev_database_identity=dev["database_identity"],
        listener_probe=listener_probe,
    )
    assert preflight["status"] == "ready"
    assert preflight["legacy_database_identity"]["size"] == legacy_size
    assert preflight["new_database_identity"] == dev["database_identity"]
    assert Path(preflight["checkpoint_path"]).is_file()
    legacy_before = legacy.stat()

    activated = db.activate_dev_world_cutover(
        storage_root=storage_root,
        preflight_hash=preflight["preflight_hash"],
        listener_probe=listener_probe,
    )
    assert activated["status"] == "active"
    replay = db.activate_dev_world_cutover(
        storage_root=storage_root,
        preflight_hash=preflight["preflight_hash"],
        listener_probe=listener_probe,
    )
    assert replay["idempotent"] is True
    assert legacy.stat().st_ino == legacy_before.st_ino
    assert legacy.stat().st_size == legacy_before.st_size

    validation = db.validate_dev_world_cutover_activation(
        storage_root=storage_root,
        expected_dev_database_identity=dev["database_identity"],
        source_identity=source,
        listener_probe=listener_probe,
    )
    assert validation["active"] is True
    rolled_back = db.rollback_dev_world_cutover(
        storage_root=storage_root,
        preflight_hash=preflight["preflight_hash"],
    )
    assert rolled_back["status"] == "rolled_back"
    with pytest.raises(ValueError, match="activation"):
        db.validate_dev_world_cutover_activation(
            storage_root=storage_root,
            expected_dev_database_identity=dev["database_identity"],
            source_identity=source,
        )


def test_ac_dev_cutover_allows_live_legacy_growth_between_preflight_and_activation(
    tmp_path,
):
    """The still-live stable store may grow without changing physical identity."""

    from agent.governance import db

    root, commit = _dev_source_repo(tmp_path)
    source = {
        "root": str(root.resolve()),
        "branch": "codex/ac-dev",
        "commit": commit,
        "source_sha256": "sha256:" + "e" * 64,
    }
    process = {"pid": 406, "start_identity": "cutover-live-growth"}
    storage_root, _ = _canonical_dev_world(tmp_path)
    dev = db.bootstrap_dev_governance_store(
        storage_root,
        source_identity=source,
        process_identity=process,
    )
    legacy = tmp_path / "legacy" / "governance.db"
    legacy.parent.mkdir()
    with legacy.open("wb") as handle:
        handle.truncate(db.AC_LEGACY_ARCHIVE_SIZE_BYTES)

    def listener_probe(port):
        if port == 40000:
            return {
                "port": 40000,
                "listening": True,
                "pid": 700,
                "process_start_identity": "stable-700",
                "source_commit": "f" * 40,
            }
        return {
            "port": 40008,
            "listening": False,
            "pid": 0,
            "process_start_identity": "",
            "source_commit": "",
        }

    preflight = db.preflight_dev_world_cutover(
        legacy_database_path=legacy,
        storage_root=storage_root,
        source_identity=source,
        process_identity=process,
        expected_dev_database_identity=dev["database_identity"],
        listener_probe=listener_probe,
    )
    initial = legacy.stat()
    with legacy.open("ab") as handle:
        handle.write(b"stable-live-growth")
        handle.flush()
        os.fsync(handle.fileno())
    grown = legacy.stat()
    assert (grown.st_dev, grown.st_ino) == (initial.st_dev, initial.st_ino)
    assert grown.st_size > initial.st_size

    activated = db.activate_dev_world_cutover(
        storage_root=storage_root,
        preflight_hash=preflight["preflight_hash"],
        listener_probe=listener_probe,
    )

    assert activated["status"] == "active"
    assert activated["legacy_database_identity"]["device"] == grown.st_dev
    assert activated["legacy_database_identity"]["inode"] == grown.st_ino


@pytest.mark.parametrize("replacement_kind", ["inode", "symlink"])
def test_ac_dev_cutover_rejects_legacy_path_identity_replacement(
    tmp_path, replacement_kind
):
    """Live growth is allowed, but the canonical physical file may not change."""

    from agent.governance import db

    root, commit = _dev_source_repo(tmp_path)
    source = {
        "root": str(root.resolve()),
        "branch": "codex/ac-dev",
        "commit": commit,
        "source_sha256": "sha256:" + "e" * 64,
    }
    process = {"pid": 407, "start_identity": "cutover-path-identity"}
    storage_root, _ = _canonical_dev_world(tmp_path)
    dev = db.bootstrap_dev_governance_store(
        storage_root,
        source_identity=source,
        process_identity=process,
    )
    legacy_size = 2 * 1024 * 1024
    legacy = tmp_path / "legacy" / "governance.db"
    legacy.parent.mkdir()
    with legacy.open("wb") as handle:
        handle.truncate(legacy_size)

    def listener_probe(port):
        return {
            "port": port,
            "listening": port == 40000,
            "pid": 700 if port == 40000 else 0,
            "process_start_identity": "stable-700" if port == 40000 else "",
            "source_commit": "f" * 40 if port == 40000 else "",
        }

    preflight = db.preflight_dev_world_cutover(
        legacy_database_path=legacy,
        storage_root=storage_root,
        source_identity=source,
        process_identity=process,
        expected_dev_database_identity=dev["database_identity"],
        listener_probe=listener_probe,
    )
    replacement = tmp_path / "replacement.db"
    with replacement.open("wb") as handle:
        handle.truncate(legacy_size)
    if replacement_kind == "inode":
        os.replace(replacement, legacy)
    else:
        legacy.unlink()
        legacy.symlink_to(replacement)

    with pytest.raises(ValueError, match="symlink|identity changed"):
        db.activate_dev_world_cutover(
            storage_root=storage_root,
            preflight_hash=preflight["preflight_hash"],
            listener_probe=listener_probe,
        )
    assert not (storage_root / "cutover" / "active.json").exists()


def test_ac_dev_cutover_failure_leaves_old_live_and_new_inactive(tmp_path):
    from agent.governance import db

    root, commit = _dev_source_repo(tmp_path)
    source = {
        "root": str(root.resolve()),
        "branch": "codex/ac-dev",
        "commit": commit,
        "source_sha256": "sha256:" + "f" * 64,
    }
    process = {"pid": 505, "start_identity": "cutover-operator"}
    storage_root, _ = _canonical_dev_world(tmp_path)
    dev = db.bootstrap_dev_governance_store(storage_root, source_identity=source, process_identity=process)
    legacy = tmp_path / "legacy.db"
    with legacy.open("wb") as handle:
        handle.truncate(db.AC_LEGACY_ARCHIVE_SIZE_BYTES)
    wal = Path(str(dev["database_path"]) + "-wal")
    wal.write_bytes(b"owned")

    def listener_probe(port):
        return {
            "port": port,
            "listening": port == 40000,
            "pid": 700 if port == 40000 else 0,
            "process_start_identity": "stable-700" if port == 40000 else "",
            "source_commit": "f" * 40 if port == 40000 else "",
        }

    with pytest.raises(ValueError, match="WAL/SHM"):
        db.preflight_dev_world_cutover(
            legacy_database_path=legacy,
            storage_root=storage_root,
            source_identity=source,
            process_identity=process,
            expected_dev_database_identity=dev["database_identity"],
            listener_probe=listener_probe,
        )
    assert legacy.stat().st_size == db.AC_LEGACY_ARCHIVE_SIZE_BYTES
    assert not (storage_root / "cutover" / "active.json").exists()


def test_ac_dev_archive_content_proof_is_fd_derived_and_detects_toctou(
    tmp_path, monkeypatch
):
    """v12 Y1: caller receipts are not identity authority; one fd owns proof."""

    from agent.governance import db

    legacy = tmp_path / "legacy.db"
    with legacy.open("wb") as handle:
        handle.truncate(db.AC_LEGACY_ARCHIVE_SIZE_BYTES)

    source = __import__("inspect").getsource(db._cutover_database_stat)
    assert "digest_receipt_path" not in source
    assert "os.O_NOFOLLOW" in source
    assert source.count("os.fstat") >= 2

    identity = db._cutover_database_stat(
        legacy,
        expected_size=db.AC_LEGACY_ARCHIVE_SIZE_BYTES,
        content_digest=True,
    )
    assert identity["content_digest"].startswith("sha256-sparse-v1:")

    original = db._fd_sparse_content_digest

    def mutate_while_hashing(descriptor, *, size):
        digest = original(descriptor, size=size)
        with legacy.open("r+b") as handle:
            handle.seek(4096)
            handle.write(b"toctou")
        return digest

    monkeypatch.setattr(db, "_fd_sparse_content_digest", mutate_while_hashing)
    with pytest.raises(ValueError, match="changed during content proof"):
        db._cutover_database_stat(
            legacy,
            expected_size=db.AC_LEGACY_ARCHIVE_SIZE_BYTES,
            content_digest=True,
        )


def test_ac_dev_archive_cached_observation_is_not_activation_authority(tmp_path):
    """Volatile cached observations cannot replace physical file identity."""

    from agent.governance import db

    legacy = tmp_path / "legacy.db"
    with legacy.open("wb") as handle:
        handle.truncate(db.AC_LEGACY_ARCHIVE_SIZE_BYTES)
        handle.seek(4096)
        handle.write(b"independent-fd-truth")
    actual = db._cutover_database_stat(
        legacy,
        expected_size=db.AC_LEGACY_ARCHIVE_SIZE_BYTES,
        content_digest=True,
    )
    stale_observation = {
        **actual,
        "size": actual["size"] + 1,
        "mtime_ns": actual["mtime_ns"] + 1,
        "ctime_ns": actual["ctime_ns"] + 1,
        "content_digest": "sha256-sparse-v1:" + "0" * 64,
    }
    observed = db._cutover_database_stat(
        legacy,
        content_digest=True,
        cached_identity=stale_observation,
    )
    assert observed["content_digest"] == actual["content_digest"]
    assert db._cutover_hash({"legacy_database_identity": actual}) == db._cutover_hash(
        {"legacy_database_identity": stale_observation}
    )
    assert db._cutover_hash({"legacy_database_identity": actual}) != db._cutover_hash(
        {"legacy_database_identity": {**actual, "inode": actual["inode"] + 1}}
    )

    with pytest.raises(ValueError, match="identity changed"):
        db._cutover_database_stat(
            legacy,
            content_digest=True,
            cached_identity={**actual, "inode": actual["inode"] + 1},
        )


def test_ac_dev_offline_backlog_schema_admission_repairs_only_exact_missing_set():
    from governance import db

    conn = sqlite3.connect(":memory:")
    db._ensure_schema(conn)
    try:
        assert db.backlog_read_schema_drift(conn)["invalid"] == []
        result = db.admit_missing_backlog_read_schema(conn)
        assert result["changed"] is True
        assert db.backlog_read_schema_drift(conn) == {"missing": [], "invalid": []}
        assert db.admit_missing_backlog_read_schema(conn) == {"changed": False, "missing": []}
    finally:
        conn.close()


def test_dev_schema_inventory_accepts_only_exact_backlog_overlay():
    from governance import db

    conn = sqlite3.connect(":memory:")
    db._ensure_schema(conn)
    try:
        db.admit_missing_backlog_read_schema(conn)
        _install_all_graph_owners_for_inventory_test(db, conn)
        db._verify_dev_world_schema_inventory(conn)
        conn.execute("DROP TRIGGER trg_dashboard_backlog_cache_insert")
        conn.execute("CREATE TRIGGER trg_dashboard_backlog_cache_insert AFTER INSERT ON backlog_bugs BEGIN SELECT 1; END")
        with pytest.raises(ValueError, match="source schema inventory mismatch"):
            db._verify_dev_world_schema_inventory(conn)
    finally:
        conn.close()


def test_dev_schema_inventory_builds_reference_outside_verify_only(monkeypatch):
    from governance import db

    target = db._migration_capable_source_schema_memory()
    inventory_before = db._sqlite_master_inventory(target)
    changes_before = target.total_changes
    observed_reference_builds = []
    original_ensure_schema = db._ensure_schema

    def observe_reference_build(connection):
        observed_reference_builds.append(
            (connection is target, db.dev_runtime_verify_only())
        )
        return original_ensure_schema(connection)

    monkeypatch.setattr(db, "_ensure_schema", observe_reference_build)
    monkeypatch.setenv(db.RUNTIME_PLANE_ENV, db.DEV_RUNTIME_PLANE)

    db._verify_dev_world_schema_inventory(target)

    assert observed_reference_builds
    assert all(
        is_target is False and verify_only is False
        for is_target, verify_only in observed_reference_builds
    )
    assert os.environ[db.RUNTIME_PLANE_ENV] == db.DEV_RUNTIME_PLANE
    assert target.total_changes == changes_before
    assert db._sqlite_master_inventory(target) == inventory_before
    target.close()


@pytest.mark.parametrize("drift", ["empty", "missing", "invalid"])
def test_dev_schema_inventory_still_rejects_target_drift_in_dev_plane(
    monkeypatch, drift
):
    from governance import db

    target = sqlite3.connect(":memory:")
    target.row_factory = sqlite3.Row
    if drift != "empty":
        reference = db._migration_capable_source_schema_memory()
        reference.backup(target)
        reference.close()
    if drift == "missing":
        target.execute("DROP TABLE parallel_branch_runtime_contexts")
    elif drift == "invalid":
        target.execute("CREATE TABLE outside_source_contract(value TEXT)")
    target.commit()
    inventory_before = db._sqlite_master_inventory(target)
    changes_before = target.total_changes
    monkeypatch.setenv(db.RUNTIME_PLANE_ENV, db.DEV_RUNTIME_PLANE)

    with pytest.raises(ValueError, match="source schema inventory mismatch"):
        db._verify_dev_world_schema_inventory(target)

    assert os.environ[db.RUNTIME_PLANE_ENV] == db.DEV_RUNTIME_PLANE
    assert target.total_changes == changes_before
    assert db._sqlite_master_inventory(target) == inventory_before
    target.close()


def test_source_reference_builder_does_not_weaken_dev_target_authority(monkeypatch):
    from governance import db, parallel_branch_runtime

    monkeypatch.setenv(db.RUNTIME_PLANE_ENV, db.DEV_RUNTIME_PLANE)
    reference = db._migration_capable_source_schema_memory()
    reference.close()
    target = sqlite3.connect(":memory:")

    with pytest.raises(
        db.DevRuntimeSchemaVerificationError,
        match="ac_dev_verify_only_schema_incompatible: parallel_branch_runtime",
    ):
        parallel_branch_runtime.ensure_branch_runtime_schema(target)

    assert os.environ[db.RUNTIME_PLANE_ENV] == db.DEV_RUNTIME_PLANE
    assert target.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0] == 0
    target.close()


def test_source_reference_builder_restores_runtime_plane_after_failure(monkeypatch):
    from governance import db

    monkeypatch.setenv(db.RUNTIME_PLANE_ENV, db.DEV_RUNTIME_PLANE)

    def fail_source_build(_connection):
        assert db.dev_runtime_verify_only() is False
        raise RuntimeError("source reference sentinel")

    monkeypatch.setattr(db, "_ensure_schema", fail_source_build)
    with pytest.raises(RuntimeError, match="source reference sentinel"):
        db._migration_capable_source_schema_memory()

    assert os.environ[db.RUNTIME_PLANE_ENV] == db.DEV_RUNTIME_PLANE


def test_dev_schema_inventory_subtracts_exact_graph_and_semantic_owners_zero_write():
    from governance import db, reconcile_semantic_enrichment as semantic

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db._ensure_schema(conn)
    db.admit_missing_backlog_read_schema(conn)
    _install_all_graph_owners_for_inventory_test(db, conn)
    db.execute_graph_schema_sql(conn, semantic.SEMANTIC_STATE_SCHEMA_SQL)
    conn.commit()
    registry_names = {
        row[1]
        for _owner, _ensure_schema, canonical in db._graph_schema_owner_registry()
        for row in canonical
    }
    assert {
        "graph_correction_patches",
        "graph_ref_events",
        "graph_node_migrations",
        "graph_semantic_projections",
        "idx_pending_scope_branch",
        "idx_pending_scope_status",
    }.issubset(registry_names)
    inventory_before = db._graph_materialization_inventory(conn)
    changes_before = conn.total_changes

    db._verify_dev_world_schema_inventory(conn)

    assert conn.total_changes == changes_before
    assert db._graph_materialization_inventory(conn) == inventory_before
    conn.close()


@pytest.mark.parametrize(
    "drift",
    ["owner_absent", "owner_partial", "owner_altered", "unknown_graph"],
)
def test_dev_schema_inventory_rejects_nonexact_graph_owner_without_write(drift):
    from governance import db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db._ensure_schema(conn)
    db.admit_missing_backlog_read_schema(conn)
    _install_all_graph_owners_for_inventory_test(db, conn)
    if drift == "owner_absent":
        owner_inventory = {
            owner: inventory
            for owner, _ensure_schema, inventory in db._graph_schema_owner_registry()
        }["graph_events"]
        for kind, name, _table, _sql in owner_inventory:
            if kind in {"view", "trigger", "index"} and not name.startswith(
                "sqlite_autoindex_"
            ):
                conn.execute(f'DROP {kind.upper()} "{name}"')
        for kind, name, _table, _sql in owner_inventory:
            if kind == "table":
                conn.execute(f'DROP TABLE "{name}"')
    elif drift == "owner_partial":
        conn.execute("DROP INDEX idx_pending_scope_branch")
    elif drift == "owner_altered":
        conn.execute("DROP INDEX idx_pending_scope_status")
        conn.execute(
            "CREATE INDEX idx_pending_scope_status "
            "ON pending_scope_reconcile(project_id, branch_ref)"
        )
    else:
        conn.execute("CREATE TABLE graph_unknown_inventory_owner(id TEXT PRIMARY KEY)")
    conn.commit()
    inventory_before = db._graph_materialization_inventory(conn)
    schema_hash_before = hashlib.sha256(
        repr(inventory_before).encode("utf-8")
    ).hexdigest()
    changes_before = conn.total_changes

    with pytest.raises(ValueError, match="graph|source schema inventory"):
        db._verify_dev_world_schema_inventory(conn)

    assert conn.total_changes == changes_before
    inventory_after = db._graph_materialization_inventory(conn)
    assert hashlib.sha256(
        repr(inventory_after).encode("utf-8")
    ).hexdigest() == schema_hash_before
    assert inventory_after == inventory_before
    conn.close()


def test_ac_dev_backlog_admission_binds_managed_and_protected_inventories():
    """Admission may add its five objects without reinterpreting other schema."""
    from governance import db

    conn = sqlite3.connect(":memory:")
    db._ensure_schema(conn)
    conn.execute("CREATE TABLE worker_runtime_contract (name TEXT PRIMARY KEY, value TEXT)")
    protected_before = db.backlog_read_schema_protected_inventory(conn)
    managed_before = db.backlog_read_schema_managed_inventory(conn)
    try:
        assert managed_before["inventory"] == []
        assert db.admit_missing_backlog_read_schema(conn)["changed"] is True
        assert db.backlog_read_schema_managed_inventory(conn) == (
            db.canonical_backlog_read_schema_managed_inventory()
        )
        assert db.backlog_read_schema_protected_inventory(conn) == protected_before
        conn.execute("CREATE INDEX worker_runtime_contract_name ON worker_runtime_contract(name)")
        assert db.backlog_read_schema_protected_inventory(conn) != protected_before
    finally:
        conn.close()


def test_ac_dev_authority_projection_admission_is_complete_and_fail_closed():
    """The Phase-Z offline capability admits only its exact six-owner ABI."""
    from governance import db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db._ensure_schema(conn)
    try:
        pristine = db.authority_projection_schema_drift(conn)
        assert pristine == {"missing": sorted(db.AC_AUTHORITY_SCHEMA_TABLES), "invalid": []}
        result = db.admit_missing_authority_projection_schema(conn)
        assert result["changed"] is True
        assert set(result["missing"]) == set(db.AC_AUTHORITY_SCHEMA_TABLES)
        assert db.authority_projection_schema_drift(conn) == {"missing": [], "invalid": []}
        # Assert every source-derived physical index effect, including SQLite's
        # PK/UNIQUE autoindexes rather than a brittle hand-maintained name list.
        expected = db._canonical_authority_projection_schema_inventory(include_plan=True)
        observed = db._sqlite_master_inventory(conn)
        assert observed == expected
        assert sum(len(conn.execute(f"PRAGMA index_list({table})").fetchall())
                   for table in db.AC_AUTHORITY_SCHEMA_TABLES) >= 17
        assert db.admit_missing_authority_projection_schema(conn) == {"changed": False, "missing": []}
    finally:
        conn.close()


@pytest.mark.parametrize("sql", (
    "CREATE TABLE authority_shadow (id TEXT)",
    "CREATE INDEX authority_shadow_index ON backlog_bugs(bug_id)",
    "CREATE TABLE observer_route_token_refs (project_id TEXT)",
))
def test_ac_dev_authority_projection_admission_rejects_extra_altered_and_partial(sql):
    from governance import db

    conn = sqlite3.connect(":memory:")
    db._ensure_schema(conn)
    conn.execute(sql)
    before = db._sqlite_master_inventory(conn)
    try:
        drift = db.authority_projection_schema_drift(conn)
        assert drift["invalid"]
        with pytest.raises(ValueError, match="rejects invalid"):
            db.admit_missing_authority_projection_schema(conn)
        assert db._sqlite_master_inventory(conn) == before
    finally:
        conn.close()


def test_ac_dev_offline_backlog_schema_admission_rolls_back_unexpected_drift():
    from governance import db

    conn = sqlite3.connect(":memory:")
    db._ensure_schema(conn)
    conn.execute("CREATE TABLE dashboard_backlog_cache_generation (resource TEXT)")
    before = tuple(conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name"))
    try:
        with pytest.raises(ValueError, match="rejects invalid"):
            db.admit_missing_backlog_read_schema(conn)
        assert tuple(conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name")) == before
    finally:
        conn.close()


def test_ac_dev_schema_admission_rejects_shadow_and_altered_namespace_objects():
    from governance import db

    for sql, expected in (
        ("CREATE TABLE shadow_backlog_table (id TEXT)", "inventory_extra"),
        ("CREATE INDEX shadow_backlog_index ON backlog_bugs(bug_id)", "inventory_extra"),
        ("CREATE TRIGGER shadow_backlog AFTER INSERT ON backlog_bugs BEGIN SELECT 1; END", "inventory_extra"),
        ("CREATE VIEW shadow_backlog_view AS SELECT bug_id FROM backlog_bugs", "inventory_extra"),
        ("CREATE INDEX idx_backlog_bugs_dashboard_keyset ON backlog_bugs(bug_id)", "inventory_altered"),
    ):
        conn = sqlite3.connect(":memory:")
        db._ensure_schema(conn)
        conn.execute(sql)
        try:
            drift = db.backlog_read_schema_drift(conn)
            assert any(expected in item for item in drift["invalid"])
            with pytest.raises(ValueError, match="rejects invalid"):
                db.admit_missing_backlog_read_schema(conn)
        finally:
            conn.close()


def test_isolated_root_direct_bootstrap_requires_central_valid_v3_receipt(tmp_path, monkeypatch):
    from governance import db

    stable = tmp_path / "stable"; stable.mkdir()
    stable_database = stable / "governance.db"; stable_database.write_bytes(b"stable")
    root = tmp_path / "isolated"; database = root / db.AC_DATABASE_DEV_RELATIVE_PATH
    database.parent.mkdir(parents=True); database.write_bytes(b"not-authorized")
    archive = root / "archive" / "schema-admission"; archive.mkdir(parents=True)
    raw = b'{"stage":"completed"}'
    digest = hashlib.sha256(raw).hexdigest()
    receipt = archive / f"{digest}.json"; receipt.write_bytes(raw)
    (archive / f"{digest}.sha256").write_text(
        f"sha256:{digest}  {receipt.name}\n", encoding="utf-8",
    )
    monkeypatch.setenv(db.AC_DEV_STORAGE_ROOT_ENV, str(root))
    monkeypatch.setenv(db.AC_STABLE_SHARED_VOLUME_ENV, str(stable))
    binding = {
        "shared_volume_path": str(stable), "database_path": str(stable_database),
        "stable_database_identity": {"device": stable_database.stat().st_dev,
                                     "inode": stable_database.stat().st_ino},
    }
    monkeypatch.setattr(db, "verified_stable_database_binding", lambda: binding)
    monkeypatch.setattr(db, "_revalidate_stable_database_binding", lambda _binding: None)
    connects = []
    monkeypatch.setattr(db.sqlite3, "connect", lambda *args, **kwargs: connects.append(args))
    with pytest.raises(ValueError, match="isolated receipt binding mismatch"):
        db.bootstrap_dev_governance_store(
            root,
            source_identity={"root": "/source", "branch": "codex/ac-dev", "commit": "a" * 40,
                             "tree": "b" * 40, "source_sha256": "sha256:" + "c" * 64, "dirty": ""},
            process_identity={"pid": 1, "start_identity": "start"},
            linked_v3_receipt=receipt,
        )
    assert connects == []


@pytest.mark.parametrize("mutation", [
    "none", "candidate", "database_sha", "linked", "sidecar", "stable_overlap",
])
def test_canonical_legacy_postimage_adoption_is_exact_zero_connect_ingress(
    tmp_path, monkeypatch, mutation,
):
    from governance import db
    import agent.runtime_plane as runtime_plane

    stable = tmp_path / "stable"; stable.mkdir()
    stable_database = stable / "stable.db"; stable_database.write_bytes(b"stable")
    root = tmp_path / "canonical"; database = root / db.AC_DATABASE_DEV_RELATIVE_PATH
    database.parent.mkdir(parents=True); database.write_bytes(b"postimage")
    archive = root / "archive" / "schema-admission"; archive.mkdir(parents=True)
    historical = {"root": "/old", "branch": "codex/ac-dev", "commit": "a" * 40,
                  "tree": "b" * 40, "source_sha256": "sha256:" + "c" * 64, "dirty": ""}
    linked_value = {"source_identity": {"cli_source": historical},
                    "database_sha256_after": "sha256:" + "d" * 64}
    linked_raw = json.dumps(linked_value, sort_keys=True).encode()
    linked = archive / (hashlib.sha256(linked_raw).hexdigest() + ".json"); linked.write_bytes(linked_raw)
    source = {**historical, "commit": "e" * 40, "tree": "f" * 40,
              "source_sha256": "sha256:" + "1" * 64}
    adoption = {
        "schema_version": "ac_dev_canonical_legacy_postimage_adoption.v1", "stage": "completed",
        "project_id": "aming-claw", "port": 40008,
        "root_identity": {"path": str(root), "device": root.stat().st_dev, "inode": root.stat().st_ino},
        "database_identity": {"path": str(database), "device": database.stat().st_dev,
                              "inode": database.stat().st_ino},
        "database_sha256_preimage": linked_value["database_sha256_after"],
        "database_sha256_postimage": "sha256:" + hashlib.sha256(database.read_bytes()).hexdigest(),
        "linked_v3_receipt": str(linked),
        "linked_v3_receipt_sha256": "sha256:" + hashlib.sha256(linked_raw).hexdigest(),
        "receipt_source_identity": historical, "candidate_source_identity": source,
        "legacy_process_identity": {
            "pid": 39594, "start_identity": "pid:39594:cli-bootstrap",
        },
    }
    if mutation == "candidate": adoption["candidate_source_identity"] = historical
    elif mutation == "database_sha": adoption["database_sha256_postimage"] = "sha256:" + "0" * 64
    elif mutation == "linked": adoption["linked_v3_receipt_sha256"] = "sha256:" + "0" * 64
    elif mutation == "sidecar": Path(str(database) + "-wal").write_bytes(b"")
    elif mutation == "stable_overlap":
        stable_database.unlink(); stable_database.hardlink_to(database)
    adoption_dir = root / "archive" / "canonical-legacy-postimage-adoption"; adoption_dir.mkdir()
    raw = json.dumps(adoption, sort_keys=True, separators=(",", ":")).encode()
    receipt = adoption_dir / f"adoption.{hashlib.sha256(raw).hexdigest()}.json"; receipt.write_bytes(raw)
    binding = {"shared_volume_path": str(stable), "database_path": str(stable_database)}
    monkeypatch.setenv(db.AC_DEV_STORAGE_ROOT_ENV, str(root))
    monkeypatch.setenv(db.AC_STABLE_SHARED_VOLUME_ENV, str(stable))
    monkeypatch.setattr(db, "verified_stable_database_binding", lambda: binding)
    monkeypatch.setattr(db, "_revalidate_stable_database_binding", lambda _binding: None)
    monkeypatch.setattr(runtime_plane, "resolve_ac_dev_storage_root", lambda _stable: root)
    connects = []
    monkeypatch.setattr(db.sqlite3, "connect", lambda *args, **kwargs: connects.append(args))
    if mutation == "none":
        assert db._dev_storage_root(
            create=False, isolated_receipt=linked, source_identity=source, allow_postimage=True,
        ) == root
    else:
        with pytest.raises(ValueError):
            db._dev_storage_root(
                create=False, isolated_receipt=linked, source_identity=source, allow_postimage=True,
            )
    assert connects == []


def test_sqlite_projection_value_is_lossless_and_type_tagged():
    from governance import db

    encode = db._sqlite_projection_value
    assert encode(None) == ["null", ""]
    assert encode(False) == ["integer", "0"]
    assert encode(True) == ["integer", "1"]
    assert encode(-(2**63)) == ["integer", "-9223372036854775808"]
    assert encode(2**63 - 1) == ["integer", "9223372036854775807"]
    assert encode(1) != encode(1.0)
    assert encode(0.0) != encode(-0.0)
    assert encode(float("nan")) == ["float", "nan"]
    assert encode(float("inf")) == ["float", "+inf"]
    assert encode(float("-inf")) == ["float", "-inf"]
    assert encode("") != encode(b"")
    assert encode("ff") != encode(b"\xff")
    assert encode(memoryview(b"\x00\xff")) == ["blob-hex", "00ff"]


def test_sqlite_projection_covers_fts_shadow_blobs_and_is_row_order_independent(tmp_path):
    from governance import db
    import agent.cli as cli

    projections = []
    for number, row_order in enumerate(((2, 1), (1, 2))):
        path = tmp_path / f"projection-{number}.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT)")
        connection.execute("CREATE TABLE typed(id INTEGER PRIMARY KEY, text_value, blob_value, real_value)")
        for row_id in row_order:
            connection.execute(
                "INSERT INTO typed VALUES(?,?,?,?)",
                (row_id, "\u2603" if row_id == 1 else "", b"\x00\xff" if row_id == 1 else b"", -0.0 if row_id == 1 else float("inf")),
            )
        connection.execute("CREATE VIRTUAL TABLE memories_fts USING fts5(body)")
        connection.execute("INSERT INTO memories_fts(body) VALUES('realistic shadow block')")
        connection.commit()
        block = connection.execute(
            "SELECT block FROM memories_fts_data WHERE block IS NOT NULL ORDER BY id LIMIT 1"
        ).fetchone()[0]
        assert isinstance(block, bytes) and block
        projections.append(db._sqlite_logical_projection(
            connection, exclude_tables=frozenset({"schema_meta"}),
        ))
        connection.close()
        public_projection, _meta = cli._immutable_sqlite_projection(path)
        assert public_projection == projections[-1]
    assert projections[0] == projections[1]
    assert "memories_fts_data" in projections[0]


def test_sqlite_projection_detects_text_blob_numeric_and_signed_zero_collisions(tmp_path):
    from governance import db

    path = tmp_path / "collision.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE values_under_test(id INTEGER PRIMARY KEY, value)")
    connection.executemany("INSERT INTO values_under_test VALUES(?,?)", [
        (1, "same"), (2, b"same"), (3, 1), (4, 1.0), (5, 0.0), (6, -0.0),
    ])
    baseline = db._sqlite_logical_projection(connection)
    connection.execute("UPDATE values_under_test SET value=? WHERE id=2", ("same",))
    connection.commit()
    assert db._sqlite_logical_projection(connection) != baseline
    connection.close()


def test_sqlite_projection_quotes_every_identifier_and_public_path_is_shared(tmp_path):
    from governance import db
    import agent.cli as cli

    table = 'odd"表'
    columns = ('select', 'c"ol', '雪')
    path = tmp_path / "quoted-identifiers.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT)")
    quoted_table = db._sqlite_quote_identifier(table)
    quoted_columns = ",".join(db._sqlite_quote_identifier(column) for column in columns)
    connection.execute(f"CREATE TABLE {quoted_table} ({quoted_columns})")
    connection.execute(
        f"INSERT INTO {quoted_table} ({quoted_columns}) VALUES (?,?,?)",
        ("reserved-word", b"", b"\x00\xff\xfe"),
    )
    connection.commit()
    direct = db._sqlite_logical_projection(
        connection, exclude_tables=frozenset({"schema_meta"}),
    )
    connection.close()
    public, _meta = cli._immutable_sqlite_projection(path)
    assert public == direct
    assert set(public) == {table}
    assert db._sqlite_quote_identifier('a"b') == '"a""b"'
