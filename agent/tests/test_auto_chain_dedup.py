"""Tests for auto_chain retry dedup guards (Bug 2 fix).

Verifies that calling dispatch_chain twice for the same gate-blocked task
does NOT create duplicate retry tasks.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import os
import pytest

# Ensure agent package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _make_db() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with the minimal tasks + project_version schema."""
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
            completed_at TEXT
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


def _insert_task(conn, task_id, task_type, status, parent_task_id=None, metadata=None):
    meta = metadata or {}
    if parent_task_id:
        meta["parent_task_id"] = parent_task_id
    conn.execute(
        """INSERT INTO tasks (task_id, project_id, status, execution_status, type, prompt,
                              related_nodes, created_by, created_at, updated_at, metadata_json)
           VALUES (?, 'test-proj', ?, ?, ?, '', '[]', 'test', '2026-01-01T00:00:00Z',
                   '2026-01-01T00:00:00Z', ?)""",
        (task_id, status, status, task_type, json.dumps(meta)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Patch helpers to isolate dispatch_chain from side effects
# ---------------------------------------------------------------------------

def _patch_chain(monkeypatch):
    """Patch heavy dependencies so dispatch_chain can run without full infra."""
    import agent.governance.auto_chain as ac

    # Disable version gate
    monkeypatch.setattr(ac, "_DISABLE_VERSION_GATE", True)

    # Stub _gate_t2_pass and _gate_qa_pass to return blocked (so retry fires)
    monkeypatch.setattr(ac, "_gate_t2_pass",
                        lambda conn, pid, result, meta: (False, "test failure (stub)"))
    monkeypatch.setattr(ac, "_gate_checkpoint",
                        lambda conn, pid, result, meta: (False, "checkpoint fail (stub)"))

    # Stub _publish_event to no-op
    monkeypatch.setattr(ac, "_publish_event", lambda *a, **kw: None)

    # Stub _maybe_create_workflow_improvement_task
    monkeypatch.setattr(ac, "_maybe_create_workflow_improvement_task",
                        lambda *a, **kw: None)

    # Stub audit_service
    import types
    fake_audit = types.ModuleType("fake_audit")
    fake_audit.record = lambda *a, **kw: None
    monkeypatch.setattr("agent.governance.auto_chain.audit_service", fake_audit,
                        raising=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStagingRetryDedup:
    """stage-retry path: test/qa failure → dev retry dedup."""

    def test_first_call_creates_dev_retry(self, monkeypatch):
        conn = _make_db()
        _patch_chain(monkeypatch)

        import agent.governance.auto_chain as ac
        result = ac._do_chain(
            conn, "test-proj", "task-test-001", "test",
            result={"ok": False, "summary": "tests failed"},
            metadata={"chain_depth": 1, "parent_task_id": "task-dev-001"},
        )

        assert result.get("gate_blocked") is True
        assert result.get("retry_type") == "dev"
        retry_id = result.get("retry_task_id")
        assert retry_id and retry_id != "?"
        assert not result.get("dedup")

        # Verify task exists in DB
        row = conn.execute("SELECT type, status FROM tasks WHERE task_id = ?",
                           (retry_id,)).fetchone()
        assert row is not None
        assert row["type"] == "dev"

    def test_second_call_deduplicates_stage_retry(self, monkeypatch):
        conn = _make_db()
        _patch_chain(monkeypatch)

        import agent.governance.auto_chain as ac
        meta = {"chain_depth": 1, "parent_task_id": "task-dev-002"}
        res_data = {"ok": False, "summary": "tests failed"}

        # First call — creates retry
        r1 = ac._do_chain(
            conn, "test-proj", "task-test-002", "test",
            result=res_data, metadata=dict(meta),
        )
        assert not r1.get("dedup")
        retry_id_1 = r1["retry_task_id"]

        # Second call — must NOT create a second retry
        r2 = ac._do_chain(
            conn, "test-proj", "task-test-002", "test",
            result=res_data, metadata=dict(meta),
        )
        assert r2.get("dedup") is True
        assert r2["retry_task_id"] == retry_id_1
        assert r2.get("gate_blocked") is True

        # Only one dev retry task in DB
        rows = conn.execute(
            "SELECT task_id FROM tasks WHERE type = 'dev' "
            "AND json_extract(metadata_json, '$.parent_task_id') = 'task-test-002'"
        ).fetchall()
        assert len(rows) == 1, f"Expected 1 dev retry, got {len(rows)}"


class TestStaleMetadataStripping:
    """Verify gate-retry metadata does NOT contain stale inherited blocker fields."""

    def test_retry_metadata_strips_stale_worktree_and_branch(self, monkeypatch):
        """AC5: gate-retry must NOT inherit _worktree, _branch, or failure_reason from parent."""
        conn = _make_db()
        _patch_chain(monkeypatch)

        import agent.governance.auto_chain as ac

        # Parent metadata simulates stale inherited fields from a grandparent task
        stale_metadata = {
            "chain_depth": 0,
            "parent_task_id": "task-pm-050",
            "_worktree": "/old/stale/worktree/path",
            "_branch": "dev/stale-branch",
            "failure_reason": "redis_client.py has syntax error (stale inherited blocker)",
            "previous_gate_reason": "old grandparent gate reason",
        }

        result = ac._do_chain(
            conn, "test-proj", "task-dev-050", "dev",
            result={"summary": "fix attempt"},
            metadata=dict(stale_metadata),
        )

        assert result.get("gate_blocked") is True
        retry_id = result.get("retry_task_id")
        assert retry_id and retry_id != "?"

        # Read the retry task's metadata from DB
        row = conn.execute(
            "SELECT metadata_json FROM tasks WHERE task_id = ?",
            (retry_id,),
        ).fetchone()
        assert row is not None
        retry_meta = json.loads(row["metadata_json"])

        # Stale _worktree and _branch must be stripped (not inherited)
        assert retry_meta.get("_worktree") is None, \
            f"stale _worktree leaked into retry metadata: {retry_meta.get('_worktree')}"
        assert retry_meta.get("_branch") is None, \
            f"stale _branch leaked into retry metadata: {retry_meta.get('_branch')}"
        # Inherited failure_reason from grandparent must be removed
        assert "failure_reason" not in retry_meta, \
            f"inherited failure_reason leaked into retry: {retry_meta.get('failure_reason')}"
        # previous_gate_reason must be set to CURRENT gate reason, not stale one
        assert retry_meta.get("previous_gate_reason") != stale_metadata["previous_gate_reason"], \
            "previous_gate_reason should be overwritten with current gate reason, not inherited"

    def test_retry_prompt_includes_re_verify_instruction(self, monkeypatch):
        """AC4: retry prompt must instruct AI to re-verify blockers against current source."""
        conn = _make_db()
        _patch_chain(monkeypatch)

        import agent.governance.auto_chain as ac

        result = ac._do_chain(
            conn, "test-proj", "task-dev-060", "dev",
            result={"summary": "fix attempt"},
            metadata={"chain_depth": 0, "parent_task_id": "task-pm-060"},
        )

        retry_id = result.get("retry_task_id")
        assert retry_id

        row = conn.execute(
            "SELECT prompt FROM tasks WHERE task_id = ?",
            (retry_id,),
        ).fetchone()
        assert row is not None
        prompt_text = row["prompt"]
        # Must contain re-verification instruction (do not assume previous blockers)
        assert "do not assume" in prompt_text.lower() or "re-verify" in prompt_text.lower(), \
            f"Retry prompt missing re-verify instruction: {prompt_text[:200]}"


class TestSameStageRetryDedup:
    """same-stage retry path: gate blocked → same type retry dedup."""

    def test_first_call_creates_same_stage_retry(self, monkeypatch):
        conn = _make_db()
        _patch_chain(monkeypatch)

        import agent.governance.auto_chain as ac
        result = ac._do_chain(
            conn, "test-proj", "task-dev-010", "dev",
            result={"summary": "fix attempt"},
            metadata={"chain_depth": 0, "parent_task_id": "task-pm-010"},
        )

        # checkpoint gate is stubbed to fail → same-stage dev retry
        assert result.get("gate_blocked") is True
        retry_id = result.get("retry_task_id")
        assert retry_id and retry_id != "?"
        assert not result.get("dedup")

        row = conn.execute("SELECT type FROM tasks WHERE task_id = ?",
                           (retry_id,)).fetchone()
        assert row is not None
        assert row["type"] == "dev"

    def test_second_call_deduplicates_same_stage_retry(self, monkeypatch):
        conn = _make_db()
        _patch_chain(monkeypatch)

        import agent.governance.auto_chain as ac
        meta = {"chain_depth": 0, "parent_task_id": "task-pm-011"}
        res_data = {"summary": "fix attempt"}

        r1 = ac._do_chain(
            conn, "test-proj", "task-dev-011", "dev",
            result=res_data, metadata=dict(meta),
        )
        assert not r1.get("dedup")
        retry_id_1 = r1["retry_task_id"]

        r2 = ac._do_chain(
            conn, "test-proj", "task-dev-011", "dev",
            result=res_data, metadata=dict(meta),
        )
        assert r2.get("dedup") is True
        assert r2["retry_task_id"] == retry_id_1

        # Only one dev retry in DB for this parent
        rows = conn.execute(
            "SELECT task_id FROM tasks WHERE type = 'dev' "
            "AND json_extract(metadata_json, '$.parent_task_id') = 'task-dev-011'"
        ).fetchall()
        assert len(rows) == 1, f"Expected 1 retry, got {len(rows)}"
