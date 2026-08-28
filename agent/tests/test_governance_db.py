"""Tests for governance SQLite database layer."""
import os
import sys
import tempfile
import unittest
import sqlite3
import json
from pathlib import Path
from unittest import mock

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

    def tearDown(self):
        for key in (
            "SHARED_VOLUME_PATH",
            "AMING_CLAW_RUNTIME_PLANE",
            "AMING_CLAW_DB_MIGRATION_POLICY",
            "AMING_CLAW_ALLOWED_PROJECT_IDS",
        ):
            os.environ.pop(key, None)
        self.tmp.cleanup()

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

        self._create_registered_external_db()
        os.environ["AMING_CLAW_RUNTIME_PLANE"] = "dev"
        registered = registered_public_safe_external_project("content-sys")
        self.assertEqual(registered["project_id"], "content-sys")
        self.assertTrue(registered["public_safe"])
        for rejected in ("content_sys", "contentSys", "../content-sys", "missing"):
            with self.subTest(rejected=rejected), self.assertRaises(
                (FileNotFoundError, ValueError)
            ):
                registered_public_safe_external_project(rejected)

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
                registered = governance_db.registered_public_safe_external_project(
                    "content-sys"
                )
            self.assertTrue(registered["storage_validated_without_database_open"])
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
        with self.assertRaisesRegex(ValueError, "symlink"):
            registered_public_safe_external_project("content-sys")

    def test_dev_external_registry_rejects_symlink_and_nonregular_sidecars(self):
        from governance.db import registered_public_safe_external_project

        path = self._create_registered_external_db()
        outside = Path(self.tmp.name) / "outside-wal"
        outside.write_bytes(b"wal")
        wal = Path(str(path) + "-wal")
        wal.symlink_to(outside)
        os.environ["AMING_CLAW_RUNTIME_PLANE"] = "dev"
        with self.assertRaisesRegex(ValueError, "symlink"):
            registered_public_safe_external_project("content-sys")

        wal.unlink()
        wal.mkdir()
        with self.assertRaisesRegex(ValueError, "file type"):
            registered_public_safe_external_project("content-sys")

    def test_dev_rejects_foreign_empty_and_traversal_before_project_creation(self):
        from governance.db import get_connection

        self._create_ac_db()
        root = os.path.join(
            self.tmp.name, "codex-tasks", "state", "governance"
        )
        os.environ["AMING_CLAW_RUNTIME_PLANE"] = "dev"
        for project_id in (
            "",
            "foreign",
            "amingClaw",
            "../aming-claw",
            "aming-claw/..",
        ):
            with self.assertRaises(ValueError):
                get_connection(project_id)
        self.assertFalse(os.path.exists(os.path.join(root, "foreign")))

    def test_dev_requires_existing_database_and_never_creates_it(self):
        from governance.db import get_connection

        root = os.path.join(
            self.tmp.name,
            "codex-tasks",
            "state",
            "governance",
            "aming-claw",
        )
        os.makedirs(root, exist_ok=True)
        os.environ["AMING_CLAW_RUNTIME_PLANE"] = "dev"
        with self.assertRaises(FileNotFoundError):
            get_connection("aming-claw")
        self.assertFalse(os.path.exists(os.path.join(root, "governance.db")))

    def test_dev_schema_mismatch_fails_without_auto_migration(self):
        from governance.db import get_connection

        path = self._create_ac_db()
        raw = sqlite3.connect(path)
        raw.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION - 1),),
        )
        raw.commit()
        raw.close()

        os.environ["AMING_CLAW_RUNTIME_PLANE"] = "dev"
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

        self._create_ac_db()
        os.environ["AMING_CLAW_RUNTIME_PLANE"] = "dev"
        conn = get_connection("aming-claw")
        value = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(value, str(SCHEMA_VERSION))

    def test_canonical_database_identity_survives_normal_sqlite_writes(self):
        from governance.db import canonical_ac_database_identity

        path = self._create_ac_db()
        os.environ["SHARED_VOLUME_PATH"] = str(Path(self.tmp.name).resolve())
        os.environ["AMING_CLAW_RUNTIME_PLANE"] = "dev"
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

        path = self._create_ac_db()
        outside = os.path.join(self.tmp.name, "outside-governance.db")
        os.replace(path, outside)
        os.symlink(outside, path)
        os.environ["AMING_CLAW_RUNTIME_PLANE"] = "dev"

        with self.assertRaisesRegex(ValueError, "cannot be a symlink"):
            get_connection("aming-claw")

        self.assertTrue(os.path.isfile(outside))

    def test_dev_connection_denies_schema_and_attachment_mutation(self):
        from governance.db import get_connection

        path = self._create_ac_db()
        os.environ["AMING_CLAW_RUNTIME_PLANE"] = "dev"
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
