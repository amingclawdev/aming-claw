"""Tests for governance SQLite database layer."""
import os
import sys
import tempfile
import unittest
import sqlite3
from pathlib import Path

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
