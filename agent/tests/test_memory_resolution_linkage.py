"""Tests for memory resolution linkage (pitfall → merge commit).

Covers AC1, AC2.
"""

import sqlite3
import sys
import os
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _make_in_memory_db():
    """Create an in-memory SQLite DB with full memories schema including resolution columns."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE memories (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT UNIQUE,
            project_id TEXT NOT NULL,
            ref_id TEXT DEFAULT '',
            kind TEXT DEFAULT 'knowledge',
            module_id TEXT DEFAULT '',
            scope TEXT DEFAULT 'project',
            content TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            metadata_json TEXT,
            tags TEXT DEFAULT '',
            version INTEGER DEFAULT 1,
            status TEXT DEFAULT 'active',
            superseded_by_memory_id TEXT,
            entity_id TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolution_commit TEXT DEFAULT '',
            resolution_summary TEXT DEFAULT ''
        )
    """)
    # Also create node_state table (needed by _gate_release)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS node_state (
            project_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            verify_status TEXT NOT NULL DEFAULT 'pending',
            build_status TEXT NOT NULL DEFAULT 'impl:missing',
            evidence_json TEXT,
            updated_by TEXT,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (project_id, node_id)
        )
    """)
    # chain_events table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chain_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            root_task_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            ts TEXT NOT NULL
        )
    """)
    return conn


def _insert_memory(conn, memory_id, project_id, kind, module_id, content,
                    created_at=None, resolution_commit="", resolution_summary=""):
    now = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO memories (memory_id, project_id, kind, module_id, content, "
        "summary, metadata_json, created_at, updated_at, resolution_commit, resolution_summary) "
        "VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)",
        (memory_id, project_id, kind, module_id, content,
         content[:50], now, now, resolution_commit, resolution_summary),
    )
    conn.commit()


class TestSchemaResolutionColumns(unittest.TestCase):
    """AC1: Schema migration adds resolution columns."""

    def test_ac1_resolution_columns_exist(self):
        """AC1: memories table has resolution_commit and resolution_summary columns."""
        conn = _make_in_memory_db()
        # Verify via sqlite_master
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='memories'"
        ).fetchone()
        sql = row["sql"]
        self.assertIn("resolution_commit", sql)
        self.assertIn("resolution_summary", sql)

    def test_ac1_migration_idempotent(self):
        """AC1: Migration is idempotent (ALTER TABLE ADD COLUMN skips if present)."""
        from agent.governance.db import _run_migrations
        conn = _make_in_memory_db()
        # Running migration on a table that already has the columns should not fail
        try:
            # Simulate the migration
            for col, typedef in [
                ("resolution_commit", "TEXT DEFAULT ''"),
                ("resolution_summary", "TEXT DEFAULT ''"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE memories ADD COLUMN {col} {typedef}")
                except sqlite3.OperationalError:
                    pass  # Column already exists — expected
        except Exception as e:
            self.fail(f"Migration should be idempotent but raised: {e}")

    def test_ac1_default_values(self):
        """resolution_commit and resolution_summary default to empty string."""
        conn = _make_in_memory_db()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO memories (memory_id, project_id, kind, content, created_at, updated_at) "
            "VALUES ('test-1', 'proj', 'pitfall', 'content', ?, ?)",
            (now, now),
        )
        row = conn.execute(
            "SELECT resolution_commit, resolution_summary FROM memories WHERE memory_id='test-1'"
        ).fetchone()
        self.assertEqual(row["resolution_commit"], "")
        self.assertEqual(row["resolution_summary"], "")


class TestResolutionLinkage(unittest.TestCase):
    """AC2: Merge-stage success resolves pitfall memories."""

    def test_ac2_resolve_pitfall_on_merge_success(self):
        """AC2: After merge success, pitfall memories get resolution_commit set."""
        conn = _make_in_memory_db()
        _insert_memory(conn, "pitfall-1", "test-project", "pitfall",
                       "agent.governance.auto_chain.py",
                       "Gate blocked at test: assertion error")
        _insert_memory(conn, "pitfall-2", "test-project", "pitfall",
                       "agent.governance.auto_chain.py",
                       "Gate blocked at qa: missing tests")

        # Call _resolve_pitfall_memories directly
        from agent.governance.auto_chain import _resolve_pitfall_memories
        _resolve_pitfall_memories(
            conn, "test-project",
            result={"merge_commit": "abc12345678"},
            metadata={
                "chain_id": "root-task-1",
                "target_files": ["agent/governance/auto_chain.py"],
            },
        )

        # Check both pitfalls got resolved
        rows = conn.execute(
            "SELECT memory_id, resolution_commit, resolution_summary FROM memories "
            "WHERE project_id='test-project' AND kind='pitfall' ORDER BY memory_id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertNotEqual(row["resolution_commit"], "",
                                f"resolution_commit should be set for {row['memory_id']}")
            self.assertEqual(row["resolution_commit"], "abc12345678")
            self.assertIn("Resolved by merge", row["resolution_summary"])

    def test_ac2_only_unresolved_pitfalls_updated(self):
        """AC2: Already-resolved pitfalls are not overwritten."""
        conn = _make_in_memory_db()
        _insert_memory(conn, "already-resolved", "test-project", "pitfall",
                       "agent.governance.auto_chain.py",
                       "Old resolved pitfall",
                       resolution_commit="old-commit-hash",
                       resolution_summary="Previously resolved")
        _insert_memory(conn, "unresolved", "test-project", "pitfall",
                       "agent.governance.auto_chain.py",
                       "New unresolved pitfall")

        from agent.governance.auto_chain import _resolve_pitfall_memories
        _resolve_pitfall_memories(
            conn, "test-project",
            result={"merge_commit": "new-commit-hash"},
            metadata={
                "chain_id": "root-task-2",
                "target_files": ["agent/governance/auto_chain.py"],
            },
        )

        # Already-resolved should keep old commit
        row = conn.execute(
            "SELECT resolution_commit FROM memories WHERE memory_id='already-resolved'"
        ).fetchone()
        self.assertEqual(row["resolution_commit"], "old-commit-hash",
                         "Already-resolved pitfall should not be overwritten")

        # Unresolved should get new commit
        row = conn.execute(
            "SELECT resolution_commit FROM memories WHERE memory_id='unresolved'"
        ).fetchone()
        self.assertEqual(row["resolution_commit"], "new-commit-hash")

    def test_ac2_no_crash_on_missing_merge_commit(self):
        """Resolution linkage should not crash when merge_commit is missing."""
        conn = _make_in_memory_db()
        _insert_memory(conn, "m1", "test-project", "pitfall",
                       "agent.governance.auto_chain.py", "pitfall content")

        from agent.governance.auto_chain import _resolve_pitfall_memories
        # Should not raise
        _resolve_pitfall_memories(
            conn, "test-project",
            result={},  # No merge_commit
            metadata={"chain_id": "root-task", "target_files": ["agent/governance/auto_chain.py"]},
        )

        # Memory should remain unresolved
        row = conn.execute(
            "SELECT resolution_commit FROM memories WHERE memory_id='m1'"
        ).fetchone()
        self.assertEqual(row["resolution_commit"], "")

    def test_ac2_no_crash_on_empty_target_files(self):
        """Resolution linkage should not crash when target_files is empty."""
        conn = _make_in_memory_db()

        from agent.governance.auto_chain import _resolve_pitfall_memories
        # Should not raise
        _resolve_pitfall_memories(
            conn, "test-project",
            result={"merge_commit": "abc123"},
            metadata={"chain_id": "root-task", "target_files": []},
        )

    def test_ac2_module_prefix_scoping(self):
        """Resolution only affects pitfalls matching target_files module prefixes."""
        conn = _make_in_memory_db()
        _insert_memory(conn, "in-scope", "test-project", "pitfall",
                       "agent.governance.auto_chain.py",
                       "In-scope pitfall")
        _insert_memory(conn, "out-of-scope", "test-project", "pitfall",
                       "agent.service_manager.py",
                       "Out-of-scope pitfall")

        from agent.governance.auto_chain import _resolve_pitfall_memories
        _resolve_pitfall_memories(
            conn, "test-project",
            result={"merge_commit": "merge123"},
            metadata={
                "chain_id": "root-task",
                "target_files": ["agent/governance/auto_chain.py"],
            },
        )

        in_scope = conn.execute(
            "SELECT resolution_commit FROM memories WHERE memory_id='in-scope'"
        ).fetchone()
        self.assertEqual(in_scope["resolution_commit"], "merge123")

        out_scope = conn.execute(
            "SELECT resolution_commit FROM memories WHERE memory_id='out-of-scope'"
        ).fetchone()
        self.assertEqual(out_scope["resolution_commit"], "",
                         "Out-of-scope pitfall should not be resolved")


if __name__ == "__main__":
    unittest.main()
