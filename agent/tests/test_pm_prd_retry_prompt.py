"""Tests for PM PRD completeness retry prompt enhancement.

Verifies that when _gate_post_pm blocks a PM task for missing mandatory fields,
the retry prompt includes structured guidance with CRITICAL marker, missing fields,
prior output keys, and JSON shape example.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import os
import re
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with minimal schema."""
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
            observer_mode INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE projects (
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE chain_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            root_task_id TEXT,
            task_id TEXT,
            event_type TEXT,
            payload_json TEXT,
            ts TEXT
        );
        INSERT INTO project_version (project_id, observer_mode) VALUES ('test-proj', 0);
        INSERT INTO projects (project_id, name, created_at) VALUES ('test-proj', 'test', '2026-01-01T00:00:00Z');
    """)
    return conn


def _insert_task(conn, task_id, task_type, status, metadata=None):
    meta = metadata or {}
    conn.execute(
        """INSERT INTO tasks (task_id, project_id, status, execution_status, type, prompt,
                              related_nodes, created_by, created_at, updated_at, metadata_json)
           VALUES (?, 'test-proj', ?, ?, ?, '', '[]', 'test', '2026-01-01T00:00:00Z',
                   '2026-01-01T00:00:00Z', ?)""",
        (task_id, status, status, task_type, json.dumps(meta)),
    )
    conn.commit()


def _patch_chain(monkeypatch):
    """Patch heavy dependencies so dispatch_chain can run without full infra."""
    import agent.governance.auto_chain as ac

    monkeypatch.setattr(ac, "_DISABLE_VERSION_GATE", True)

    # Stub event publishing
    monkeypatch.setattr(ac, "_publish_event", lambda *a, **kw: None)

    # Stub memory writing
    monkeypatch.setattr(ac, "_write_chain_memory", lambda *a, **kw: None)

    # Stub workflow improvement
    monkeypatch.setattr(ac, "_maybe_create_workflow_improvement_task",
                        lambda *a, **kw: None)

    # Stub gate event recording
    monkeypatch.setattr(ac, "_record_gate_event", lambda *a, **kw: None)

    # Stub _load_task_trace
    monkeypatch.setattr(ac, "_load_task_trace", lambda conn, tid: ("trace-1", "chain-1"))


# ---------------------------------------------------------------------------
# Unit tests for _parse_pm_missing_fields
# ---------------------------------------------------------------------------

class TestParsePmMissingFields:
    def test_mandatory_fields_format(self):
        from agent.governance.auto_chain import _parse_pm_missing_fields
        reason = "PRD missing mandatory fields: [target_files, requirements, verification]"
        result = _parse_pm_missing_fields(reason)
        assert result == ["target_files", "requirements", "verification"]

    def test_skip_reasons_format(self):
        from agent.governance.auto_chain import _parse_pm_missing_fields
        reason = "PRD fields missing without skip_reasons: [test_files, acceptance_criteria]. Provide the field OR explain in skip_reasons why it's not needed."
        result = _parse_pm_missing_fields(reason)
        assert result == ["test_files", "acceptance_criteria"]

    def test_empty_brackets(self):
        from agent.governance.auto_chain import _parse_pm_missing_fields
        reason = "PRD missing mandatory fields: []"
        result = _parse_pm_missing_fields(reason)
        assert result == []

    def test_no_match(self):
        from agent.governance.auto_chain import _parse_pm_missing_fields
        reason = "Some other gate reason"
        result = _parse_pm_missing_fields(reason)
        assert result == []

    def test_quoted_fields(self):
        from agent.governance.auto_chain import _parse_pm_missing_fields
        reason = "PRD missing mandatory fields: ['target_files', 'requirements']"
        result = _parse_pm_missing_fields(reason)
        assert result == ["target_files", "requirements"]


# ---------------------------------------------------------------------------
# Integration tests for PM retry prompt
# ---------------------------------------------------------------------------

class TestPmPrdRetryPrompt:
    """Test that PM tasks blocked for PRD missing fields get structured retry prompt."""

    def _run_dispatch(self, monkeypatch, reason, task_type="pm",
                      gate_retry_count=0, result_dict=None):
        """Run dispatch_chain with a gate that returns the given reason."""
        import agent.governance.auto_chain as ac

        _patch_chain(monkeypatch)

        # Set the PM gate to block with the given reason (patch in _GATES dict)
        monkeypatch.setitem(ac._GATES, "_gate_post_pm",
                            lambda conn, pid, result, meta: (False, reason))

        conn = _make_db()
        meta = {
            "_gate_retry_count": gate_retry_count,
            "_original_prompt": "Create a PRD for feature X",
        }
        _insert_task(conn, "pm-task-1", task_type, "succeeded", metadata=meta)

        result = result_dict or {"summary": "did some stuff", "exit_code": 0, "changed_files": []}

        out = ac._do_chain(conn, "test-proj", "pm-task-1", task_type, result, meta)
        return out, conn

    def test_ac1_critical_marker(self, monkeypatch):
        """AC1: retry prompt leads with CRITICAL marker."""
        out, conn = self._run_dispatch(
            monkeypatch,
            "PRD missing mandatory fields: [target_files, requirements]",
        )
        assert out.get("retry_task_id")
        retry_row = conn.execute(
            "SELECT prompt FROM tasks WHERE task_id = ?",
            (out["retry_task_id"],)
        ).fetchone()
        prompt = retry_row["prompt"]
        assert prompt.startswith("[CRITICAL: PRD completeness gate blocked your prior output]")

    def test_ac2_missing_fields(self, monkeypatch):
        """AC2: retry prompt includes 'Missing fields:' with parsed field names."""
        out, conn = self._run_dispatch(
            monkeypatch,
            "PRD missing mandatory fields: [target_files, verification, requirements]",
        )
        retry_row = conn.execute(
            "SELECT prompt FROM tasks WHERE task_id = ?",
            (out["retry_task_id"],)
        ).fetchone()
        prompt = retry_row["prompt"]
        assert "Missing fields: target_files, verification, requirements" in prompt

    def test_ac3_prior_output_keys(self, monkeypatch):
        """AC3: retry prompt includes 'Your output contained keys:' with actual keys."""
        out, conn = self._run_dispatch(
            monkeypatch,
            "PRD missing mandatory fields: [target_files]",
            result_dict={"summary": "x", "exit_code": 0, "changed_files": []},
        )
        retry_row = conn.execute(
            "SELECT prompt FROM tasks WHERE task_id = ?",
            (out["retry_task_id"],)
        ).fetchone()
        prompt = retry_row["prompt"]
        assert "Your output contained keys:" in prompt
        assert "changed_files" in prompt
        assert "summary" in prompt
        assert "exit_code" in prompt

    def test_ac4_json_example(self, monkeypatch):
        """AC4: retry prompt contains JSON example block with required field shapes."""
        out, conn = self._run_dispatch(
            monkeypatch,
            "PRD missing mandatory fields: [target_files]",
        )
        retry_row = conn.execute(
            "SELECT prompt FROM tasks WHERE task_id = ?",
            (out["retry_task_id"],)
        ).fetchone()
        prompt = retry_row["prompt"]
        assert '"target_files"' in prompt
        assert '"test_files"' in prompt
        assert '"acceptance_criteria"' in prompt
        assert '"verification"' in prompt
        assert '"requirements"' in prompt

    def test_ac5_schema_before_original_task(self, monkeypatch):
        """AC5: schema/example block appears BEFORE 'Original task:' line."""
        out, conn = self._run_dispatch(
            monkeypatch,
            "PRD missing mandatory fields: [target_files]",
        )
        retry_row = conn.execute(
            "SELECT prompt FROM tasks WHERE task_id = ?",
            (out["retry_task_id"],)
        ).fetchone()
        prompt = retry_row["prompt"]
        schema_pos = prompt.find("Required PRD JSON Shape")
        original_pos = prompt.find("Original task:")
        assert schema_pos >= 0, "Schema section not found"
        assert original_pos >= 0, "Original task section not found"
        assert schema_pos < original_pos, "Schema must appear before Original task"

    def test_ac6_repeat_regression_event(self, monkeypatch):
        """AC6: chain_events INSERT with pm.prd.repeat_regression when new retry count >= 2."""
        # gate_retry_count=1 means this is the 1st retry; new task gets count=2
        out, conn = self._run_dispatch(
            monkeypatch,
            "PRD missing mandatory fields: [target_files]",
            gate_retry_count=1,
        )
        events = conn.execute(
            "SELECT * FROM chain_events WHERE event_type = 'pm.prd.repeat_regression'"
        ).fetchall()
        assert len(events) == 1
        payload = json.loads(events[0]["payload_json"])
        assert payload["gate_retry_count"] == 1
        assert "target_files" in payload["missing_fields"]

    def test_ac6_no_event_below_threshold(self, monkeypatch):
        """AC6: no pm.prd.repeat_regression event when new retry count < 2 (first retry)."""
        # gate_retry_count=0 means first attempt; new task gets count=1
        out, conn = self._run_dispatch(
            monkeypatch,
            "PRD missing mandatory fields: [target_files]",
            gate_retry_count=0,
        )
        events = conn.execute(
            "SELECT * FROM chain_events WHERE event_type = 'pm.prd.repeat_regression'"
        ).fetchall()
        assert len(events) == 0

    def test_ac7_generic_fallback_non_pm(self, monkeypatch):
        """AC7: Non-PM tasks use generic retry prompt."""
        import agent.governance.auto_chain as ac
        _patch_chain(monkeypatch)

        # Use test gate for test type tasks (patch in _GATES dict)
        monkeypatch.setitem(ac._GATES, "_gate_t2_pass",
                            lambda conn, pid, result, meta: (False, "test failure"))

        conn = _make_db()
        meta = {"_gate_retry_count": 0, "_original_prompt": "Run tests"}
        _insert_task(conn, "test-task-1", "test", "succeeded", metadata=meta)

        out = ac._do_chain(conn, "test-proj", "test-task-1", "test",
                                {"summary": "tests failed"}, meta)

        if out.get("retry_task_id"):
            retry_row = conn.execute(
                "SELECT prompt FROM tasks WHERE task_id = ?",
                (out["retry_task_id"],)
            ).fetchone()
            prompt = retry_row["prompt"]
            assert "CRITICAL: PRD completeness gate" not in prompt

    def test_ac7_generic_fallback_pm_other_reason(self, monkeypatch):
        """AC7: PM tasks with non-missing-field reasons use generic retry prompt."""
        out, conn = self._run_dispatch(
            monkeypatch,
            "PRD quality too low: insufficient detail",
        )
        retry_row = conn.execute(
            "SELECT prompt FROM tasks WHERE task_id = ?",
            (out["retry_task_id"],)
        ).fetchone()
        prompt = retry_row["prompt"]
        assert "CRITICAL: PRD completeness gate" not in prompt
        assert "Previous attempt" in prompt
        assert "Fix the issue described above" in prompt

    def test_skip_reasons_format_trigger(self, monkeypatch):
        """R8: 'PRD fields missing without skip_reasons' also triggers structured prompt."""
        out, conn = self._run_dispatch(
            monkeypatch,
            "PRD fields missing without skip_reasons: [test_files, acceptance_criteria]. Provide the field OR explain in skip_reasons why it's not needed.",
        )
        retry_row = conn.execute(
            "SELECT prompt FROM tasks WHERE task_id = ?",
            (out["retry_task_id"],)
        ).fetchone()
        prompt = retry_row["prompt"]
        assert prompt.startswith("[CRITICAL: PRD completeness gate blocked your prior output]")
        assert "Missing fields: test_files, acceptance_criteria" in prompt
