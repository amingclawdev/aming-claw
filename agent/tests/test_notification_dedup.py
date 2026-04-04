"""Tests for notification dedup — executor _reply_sent prevents gateway duplicate."""

import json
import os
import sqlite3
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'claimed',
            execution_status TEXT NOT NULL DEFAULT 'claimed',
            notification_status TEXT NOT NULL DEFAULT 'none',
            type TEXT NOT NULL DEFAULT 'coordinator',
            prompt TEXT NOT NULL DEFAULT '',
            related_nodes TEXT NOT NULL DEFAULT '[]',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 5,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            result_json TEXT,
            error_message TEXT DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            parent_task_id TEXT,
            retry_round INTEGER NOT NULL DEFAULT 0,
            assigned_to TEXT DEFAULT 'executor-1',
            fence_token TEXT,
            lease_expires_at TEXT,
            completed_at TEXT,
            notified_at TEXT,
            trace_id TEXT,
            chain_id TEXT
        );
        CREATE TABLE task_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'running',
            started_at TEXT,
            completed_at TEXT,
            result_json TEXT,
            error_message TEXT
        );
        CREATE TABLE project_version (
            project_id TEXT PRIMARY KEY,
            chain_version TEXT NOT NULL DEFAULT '',
            git_head TEXT NOT NULL DEFAULT '',
            dirty_files TEXT NOT NULL DEFAULT '[]',
            git_synced_at TEXT,
            observer_mode INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE projects (
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO project_version (project_id, observer_mode) VALUES ('test-proj', 0);
        INSERT INTO projects (project_id, name, created_at) VALUES ('test-proj', 'test', '2026-01-01T00:00:00Z');
    """)
    return conn


def _insert_coordinator_task(conn, task_id, chat_id="12345"):
    meta = {"chat_id": chat_id}
    conn.execute(
        """INSERT INTO tasks (task_id, project_id, status, execution_status,
                              notification_status, type, prompt,
                              related_nodes, created_by, created_at, updated_at,
                              metadata_json, assigned_to)
           VALUES (?, 'test-proj', 'claimed', 'claimed', 'none', 'coordinator', 'test',
                   '[]', 'test', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z',
                   ?, 'executor-1')""",
        (task_id, json.dumps(meta)),
    )
    conn.execute(
        """INSERT INTO task_attempts (task_id, attempt_number, status, started_at)
           VALUES (?, 1, 'running', '2026-01-01T00:00:00Z')""",
        (task_id,),
    )
    conn.commit()


class TestNotificationDedup:
    """Verify _reply_sent in result prevents duplicate gateway notification."""

    def test_reply_sent_sets_notification_sent(self, monkeypatch):
        """When executor already replied (_reply_sent=True), notification_status should be 'sent'."""
        conn = _make_db()
        _insert_coordinator_task(conn, "task-coord-001")

        from governance import task_registry
        # Patch auto_chain dispatch to no-op
        monkeypatch.setattr(task_registry, "_dispatch_auto_chain_success", lambda *a, **kw: None)
        monkeypatch.setattr(task_registry, "_dispatch_auto_chain_failed", lambda *a, **kw: None)

        result = task_registry.complete_task(
            conn, "task-coord-001",
            status="succeeded",
            result={"action": "handled", "_reply_sent": True},
            project_id="test-proj",
        )

        row = conn.execute(
            "SELECT notification_status FROM tasks WHERE task_id = 'task-coord-001'"
        ).fetchone()
        assert row["notification_status"] == "sent", \
            f"Expected 'sent' but got '{row['notification_status']}'"

    def test_no_reply_sent_sets_notification_pending(self, monkeypatch):
        """When executor did NOT reply, notification_status should be 'pending'."""
        conn = _make_db()
        _insert_coordinator_task(conn, "task-coord-002")

        from governance import task_registry
        monkeypatch.setattr(task_registry, "_dispatch_auto_chain_success", lambda *a, **kw: None)
        monkeypatch.setattr(task_registry, "_dispatch_auto_chain_failed", lambda *a, **kw: None)

        result = task_registry.complete_task(
            conn, "task-coord-002",
            status="succeeded",
            result={"summary": "some result"},
            project_id="test-proj",
        )

        row = conn.execute(
            "SELECT notification_status FROM tasks WHERE task_id = 'task-coord-002'"
        ).fetchone()
        assert row["notification_status"] == "pending", \
            f"Expected 'pending' but got '{row['notification_status']}'"

    def test_no_chat_id_stays_none(self, monkeypatch):
        """Tasks without chat_id should keep notification_status as 'none'."""
        conn = _make_db()
        # Insert task without chat_id
        conn.execute(
            """INSERT INTO tasks (task_id, project_id, status, execution_status,
                                  notification_status, type, prompt,
                                  related_nodes, created_by, created_at, updated_at,
                                  metadata_json, assigned_to)
               VALUES ('task-coord-003', 'test-proj', 'claimed', 'claimed', 'none', 'dev', 'test',
                       '[]', 'test', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z',
                       '{}', 'executor-1')""",
        )
        conn.execute(
            """INSERT INTO task_attempts (task_id, attempt_number, status, started_at)
               VALUES ('task-coord-003', 1, 'running', '2026-01-01T00:00:00Z')""",
        )
        conn.commit()

        from governance import task_registry
        monkeypatch.setattr(task_registry, "_dispatch_auto_chain_success", lambda *a, **kw: None)
        monkeypatch.setattr(task_registry, "_dispatch_auto_chain_failed", lambda *a, **kw: None)

        result = task_registry.complete_task(
            conn, "task-coord-003",
            status="succeeded",
            result={"summary": "dev done"},
            project_id="test-proj",
        )

        row = conn.execute(
            "SELECT notification_status FROM tasks WHERE task_id = 'task-coord-003'"
        ).fetchone()
        assert row["notification_status"] == "none", \
            f"Expected 'none' but got '{row['notification_status']}'"
