"""Tests for memory promote and domain pack APIs."""

import json
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timezone

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

# Need to set SHARED_VOLUME_PATH before importing governance modules
os.environ.setdefault("SHARED_VOLUME_PATH", os.path.join(_agent_dir, "..", "shared-volume"))

from governance.memory_backend import LocalBackend
from governance import memory_service


def _create_test_db():
    """Create in-memory DB with memory tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # Core memories table
    conn.execute("""CREATE TABLE memories (
        memory_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
        ref_id TEXT NOT NULL DEFAULT '', entity_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'knowledge', module_id TEXT NOT NULL DEFAULT '',
        scope TEXT NOT NULL DEFAULT 'project', content TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '', metadata_json TEXT,
        tags TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'active', superseded_by_memory_id TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    # FTS5
    conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        memory_id, content, summary, tags, module_id, kind,
        content=memories, content_rowid=rowid)""")
    # Relations
    conn.execute("""CREATE TABLE memory_relations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_ref_id TEXT NOT NULL, relation TEXT NOT NULL,
        to_ref_id TEXT NOT NULL, project_id TEXT NOT NULL,
        metadata_json TEXT, created_at TEXT NOT NULL)""")
    # Events
    conn.execute("""CREATE TABLE memory_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref_id TEXT NOT NULL, event_type TEXT NOT NULL,
        actor_id TEXT, detail TEXT,
        metadata_json TEXT, created_at TEXT NOT NULL)""")
    # Audit index (needed by audit_service.record)
    conn.execute("""CREATE TABLE IF NOT EXISTS audit_index (
        event_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
        event TEXT NOT NULL, actor TEXT,
        ok INTEGER NOT NULL DEFAULT 1,
        ts TEXT NOT NULL, node_ids TEXT)""")
    conn.commit()
    return conn


def _insert_memory(conn, project_id, memory_id, ref_id="ref-1", kind="failure_pattern",
                   content="test content", scope="project"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO memories (memory_id, project_id, ref_id, kind, module_id, "
        "scope, content, summary, metadata_json, tags, version, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'test_module', ?, ?, 'test summary', '{}', '', 1, 'active', ?, ?)",
        (memory_id, project_id, ref_id, kind, scope, content, now, now))
    conn.commit()


class TestPromoteMemory(unittest.TestCase):
    def setUp(self):
        self.conn = _create_test_db()
        self.pid = "test-proj"
        _insert_memory(self.conn, self.pid, "mem-001", kind="failure_pattern")

    def test_promote_creates_copy(self):
        result = memory_service.promote_memory(
            self.conn, self.pid, "mem-001",
            target_scope="global", reason="useful everywhere",
        )
        self.assertIn("memory_id", result)
        self.assertNotEqual(result["memory_id"], "mem-001")

        # Original still exists
        original = self.conn.execute(
            "SELECT scope FROM memories WHERE memory_id='mem-001'"
        ).fetchone()
        self.assertEqual(original["scope"], "project")

        # New copy has global scope
        new = self.conn.execute(
            "SELECT scope, metadata_json FROM memories WHERE memory_id=?",
            (result["memory_id"],)
        ).fetchone()
        self.assertEqual(new["scope"], "global")
        meta = json.loads(new["metadata_json"])
        self.assertEqual(meta["promoted_from"], "mem-001")

    def test_promote_records_event(self):
        memory_service.promote_memory(self.conn, self.pid, "mem-001")
        events = self.conn.execute(
            "SELECT * FROM memory_events WHERE event_type='promoted'"
        ).fetchall()
        self.assertEqual(len(events), 1)

    def test_promote_rejects_non_promotable_kind(self):
        _insert_memory(self.conn, self.pid, "mem-task", ref_id="ref-task", kind="task_result")
        with self.assertRaises(Exception) as ctx:
            memory_service.promote_memory(self.conn, self.pid, "mem-task")
        self.assertIn("not promotable", str(ctx.exception))

    def test_promote_not_found(self):
        with self.assertRaises(Exception):
            memory_service.promote_memory(self.conn, self.pid, "nonexistent")

    def test_promote_preserves_ref_id(self):
        result = memory_service.promote_memory(self.conn, self.pid, "mem-001")
        new = self.conn.execute(
            "SELECT ref_id FROM memories WHERE memory_id=?",
            (result["memory_id"],)
        ).fetchone()
        self.assertEqual(new["ref_id"], "ref-1")


class TestRegisterDomainPack(unittest.TestCase):
    def setUp(self):
        self.conn = _create_test_db()
        self.pid = "test-proj"

    def test_register_pack(self):
        result = memory_service.register_domain_pack(
            self.conn, self.pid, "development",
            {
                "architecture": {"durability": "permanent", "conflictPolicy": "replace"},
                "pitfall": {"durability": "permanent", "conflictPolicy": "append"},
            },
        )
        self.assertEqual(result["types_registered"], 2)
        self.assertEqual(result["domain"], "development")

    def test_pack_stored_in_db(self):
        memory_service.register_domain_pack(
            self.conn, self.pid, "dev",
            {"architecture": {"durability": "permanent", "conflictPolicy": "replace"}},
        )
        row = self.conn.execute(
            "SELECT * FROM domain_packs WHERE project_id=? AND type_name='architecture'",
            (self.pid,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["durability"], "permanent")
        self.assertEqual(row["conflict_policy"], "replace")

    def test_upsert_pack(self):
        memory_service.register_domain_pack(
            self.conn, self.pid, "dev",
            {"architecture": {"durability": "permanent", "conflictPolicy": "replace"}},
        )
        memory_service.register_domain_pack(
            self.conn, self.pid, "dev",
            {"architecture": {"durability": "durable", "conflictPolicy": "append"}},
        )
        row = self.conn.execute(
            "SELECT durability, conflict_policy FROM domain_packs "
            "WHERE project_id=? AND type_name='architecture'",
            (self.pid,)
        ).fetchone()
        self.assertEqual(row["durability"], "durable")
        self.assertEqual(row["conflict_policy"], "append")

    def test_rejects_invalid_durability(self):
        with self.assertRaises(Exception) as ctx:
            memory_service.register_domain_pack(
                self.conn, self.pid, "dev",
                {"arch": {"durability": "invalid"}},
            )
        self.assertIn("Invalid durability", str(ctx.exception))

    def test_rejects_invalid_conflict_policy(self):
        with self.assertRaises(Exception) as ctx:
            memory_service.register_domain_pack(
                self.conn, self.pid, "dev",
                {"arch": {"durability": "durable", "conflictPolicy": "invalid"}},
            )
        self.assertIn("Invalid conflictPolicy", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
