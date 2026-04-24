"""Regression test: pm.prd.published fires BEFORE version gate check.

Verifies that when a PM task completes with non-empty proposed_nodes and the
version gate blocks dispatch (HEAD != chain_version), the pm.prd.published
event is still persisted to chain_events AND no next-stage dev task is created.

Ref: OPT-BACKLOG-PM-PRD-PUBLISH-PRE-GATE
"""

from __future__ import annotations

import json
import sqlite3
import sys
import os
from unittest import mock

import pytest

# Ensure agent package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _make_db() -> sqlite3.Connection:
    """In-memory SQLite DB with minimal schema for _do_chain."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            execution_status TEXT NOT NULL DEFAULT 'queued',
            notification_status TEXT NOT NULL DEFAULT 'none',
            type TEXT NOT NULL DEFAULT 'task',
            prompt TEXT NOT NULL DEFAULT '',
            related_nodes TEXT NOT NULL DEFAULT '[]',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 5,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            result_json TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            parent_task_id TEXT,
            retry_round INTEGER NOT NULL DEFAULT 0,
            assigned_to TEXT,
            fence_token TEXT,
            lease_expires_at TEXT,
            completed_at TEXT,
            trace_id TEXT,
            chain_id TEXT,
            error_message TEXT
        );
        CREATE TABLE project_version (
            project_id TEXT PRIMARY KEY,
            chain_version TEXT NOT NULL DEFAULT '',
            git_head TEXT NOT NULL DEFAULT '',
            dirty_files TEXT NOT NULL DEFAULT '[]',
            git_synced_at TEXT,
            updated_at TEXT NOT NULL DEFAULT '',
            updated_by TEXT NOT NULL DEFAULT '',
            observer_mode INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE projects (
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE chain_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            root_task_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            ts TEXT NOT NULL
        );
        CREATE INDEX idx_chain_events_root ON chain_events(root_task_id, ts);
        CREATE INDEX idx_chain_events_task ON chain_events(task_id, event_type, ts);
        CREATE TABLE gate_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            gate_name TEXT NOT NULL,
            passed INTEGER NOT NULL,
            reason TEXT,
            trace_id TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT,
            action TEXT,
            actor TEXT,
            ok INTEGER,
            ts TEXT,
            task_id TEXT,
            details_json TEXT
        );
        CREATE TABLE memories (
            memory_id TEXT PRIMARY KEY,
            project_id TEXT,
            module_id TEXT,
            kind TEXT,
            content TEXT,
            status TEXT DEFAULT 'active',
            created_by TEXT,
            created_at TEXT,
            updated_at TEXT,
            structured TEXT
        );
        INSERT INTO project_version (project_id, chain_version, git_head, updated_at, updated_by)
            VALUES ('test-proj', 'aaa1111', 'bbb2222', '2026-01-01T00:00:00Z', 'init');
        INSERT INTO projects (project_id, name, created_at)
            VALUES ('test-proj', 'test', '2026-01-01T00:00:00Z');
    """)
    return conn


def _insert_pm_task(conn, task_id="pm-task-1"):
    """Insert a succeeded PM task row."""
    conn.execute(
        """INSERT INTO tasks (task_id, project_id, status, execution_status, type, prompt,
                              related_nodes, created_by, created_at, updated_at, metadata_json,
                              trace_id, chain_id)
           VALUES (?, 'test-proj', 'succeeded', 'succeeded', 'pm', 'do pm',
                   '[]', 'test', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '{}',
                   'trace-001', 'pm-task-1')""",
        (task_id,),
    )


class _FakeStore:
    """Minimal stand-in for ChainContextStore that writes directly to conn."""

    def __init__(self, conn):
        self._conn = conn
        self._task_to_root = {}

    def _persist_event(self, root_task_id, task_id, event_type, payload,
                       project_id, conn=None):
        from datetime import datetime, timezone
        target_conn = conn or self._conn
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        ts = datetime.now(timezone.utc).isoformat()
        target_conn.execute(
            "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (root_task_id, task_id, event_type, payload_json, ts),
        )


@pytest.fixture()
def pm_env():
    """Set up DB + mocks for a PM completion scenario where version gate blocks."""
    conn = _make_db()
    _insert_pm_task(conn)
    fake_store = _FakeStore(conn)
    return conn, fake_store


def test_pm_prd_published_fires_before_gate_blocks(pm_env):
    """PM completes with proposed_nodes, version gate blocks → pm.prd.published
    still persisted, no dev task created."""
    conn, fake_store = pm_env

    task_id = "pm-task-1"
    project_id = "test-proj"
    result = {
        "prd": {
            "requirements": ["R1: do something"],
            "acceptance_criteria": ["AC1: verify it"],
        },
        "proposed_nodes": ["L1.3", "L1.4"],
        "target_files": ["agent/governance/auto_chain.py"],
        "test_files": ["agent/tests/test_pm_prd_publish_pre_gate.py"],
        "requirements": ["R1: do something"],
        "acceptance_criteria": ["AC1: verify it"],
    }
    metadata = {
        "related_nodes": [],
        "chain_depth": 0,
    }

    # Patch internals to isolate _do_chain
    mod = "agent.governance.auto_chain"
    patches = [
        # Version gate returns False → gate blocks
        mock.patch(f"{mod}._gate_version_check",
                   return_value=(False, "HEAD bbb2222 != chain_version aaa1111")),
        # Suppress event bus publish (no real subscribers in test)
        mock.patch(f"{mod}._publish_event"),
        # Suppress structured_log
        mock.patch(f"{mod}.structured_log"),
        # Suppress audit_service.record
        mock.patch(f"{mod}.audit_service", create=True),
        # Mock chain_context.get_store to return our fake store
        mock.patch(f"agent.governance.chain_context.get_store",
                   return_value=fake_store),
        # Suppress preflight
        mock.patch(f"agent.governance.preflight.run_preflight",
                   return_value={"ok": True, "warnings": [], "blockers": []}),
        # Suppress memory_service.write_memory (called by _write_chain_memory)
        mock.patch(f"agent.governance.memory_service.write_memory",
                   return_value={"memory_id": "mem-001"}),
    ]

    for p in patches:
        p.start()

    try:
        from agent.governance.auto_chain import _do_chain
        chain_result = _do_chain(conn, project_id, task_id, "pm", result, metadata)
    finally:
        for p in patches:
            p.stop()

    # AC4: Exactly 1 pm.prd.published event in chain_events
    rows = conn.execute(
        "SELECT * FROM chain_events WHERE event_type = 'pm.prd.published'"
    ).fetchall()
    assert len(rows) == 1, f"Expected 1 pm.prd.published event, got {len(rows)}"

    payload = json.loads(rows[0]["payload_json"])
    assert payload["proposed_nodes"] == ["L1.3", "L1.4"]
    assert payload["target_files"] == ["agent/governance/auto_chain.py"]

    # AC2 + AC5: Gate blocked, no dev task created
    assert chain_result is not None
    assert chain_result.get("gate_blocked") is True, (
        f"Expected gate_blocked=True, got {chain_result}"
    )

    # Verify no dev task was created in the tasks table
    dev_tasks = conn.execute(
        "SELECT * FROM tasks WHERE type = 'dev'"
    ).fetchall()
    assert len(dev_tasks) == 0, (
        f"Expected 0 dev tasks (gate should block dispatch), got {len(dev_tasks)}"
    )
