"""Tests for governance SQLite database layer."""
import os
import sys
import tempfile
import unittest
import sqlite3
import json
import hashlib
import subprocess
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


def test_graph_activation_connection_classification_binds_opened_db_not_plane_env(
    tmp_path, monkeypatch,
):
    """Only the exact opened stable DB may activate; a real dev DB remains denied."""
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
    subprocess.run(["git", "add", "source.txt"], cwd=root, check=True)
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
