"""Tests for governance SQLite database layer."""
import os
import sys
import tempfile
import unittest
import sqlite3
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from governance.db import SCHEMA_VERSION


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

    def tearDown(self):
        for key in (
            "SHARED_VOLUME_PATH",
            "AMING_CLAW_RUNTIME_PLANE",
            "AMING_CLAW_DB_MIGRATION_POLICY",
            "AMING_CLAW_ALLOWED_PROJECT_IDS",
            "AMING_CLAW_DEV_STORAGE_ROOT",
        ):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def _bootstrap_dev_db(self):
        from governance import db

        storage_root = Path(self.tmp.name).resolve() / "dev-world"
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

        storage_root = Path(self.tmp.name).resolve() / "missing-dev-world"
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
    storage_root = tmp_path / "dev-world"
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


def test_ac_dev_storage_rejects_alias_foreign_and_symlink_roots(tmp_path, monkeypatch):
    from agent.governance import db

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


def test_ac_dev_source_tip_cas_upgrade_is_descendant_and_genesis_immutable(tmp_path):
    from agent.governance import db

    root, commit_a = _dev_source_repo(tmp_path)
    storage_root = tmp_path / "dev-world"
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
    readback = db.bootstrap_dev_governance_store(
        storage_root,
        source_identity=source_b,
        process_identity=process_b,
    )
    assert readback["source_tip_identity"] == source_b


def test_ac_dev_source_upgrade_rejects_non_descendant_root_branch_db_and_process(tmp_path):
    from agent.governance import db

    root, commit_a = _dev_source_repo(tmp_path)
    storage_root = tmp_path / "dev-world"
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
    storage_root = tmp_path / "dev-world"
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
    assert preflight["status"] == "ready"
    assert preflight["legacy_database_identity"]["size"] == db.AC_LEGACY_ARCHIVE_SIZE_BYTES
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
    storage_root = tmp_path / "dev-world"
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
