"""Tests that /api/version-check/{pid} response contains runtime version fields."""

import json
import sqlite3
from unittest import mock


def _make_mock_conn():
    """Create an in-memory SQLite connection with project_version table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE project_version (
            project_id TEXT PRIMARY KEY,
            chain_version TEXT,
            updated_at TEXT,
            git_head TEXT,
            dirty_files TEXT,
            git_synced_at TEXT
        )
    """)
    conn.execute(
        "INSERT INTO project_version VALUES (?, ?, ?, ?, ?, ?)",
        ("test-proj", "abc1234", "2026-01-01T00:00:00Z", "abc1234", "[]", "2026-01-01T00:00:00Z"),
    )
    conn.commit()
    return conn


def test_version_check_contains_runtime_fields_with_row():
    """version-check response with a DB row must contain runtime version keys."""
    mock_conn = _make_mock_conn()

    with mock.patch("agent.governance.server.get_connection", return_value=mock_conn), \
         mock.patch("agent.governance.server._utc_now", return_value="2026-01-01T00:00:00Z"):
        from agent.governance.server import handle_version_check

        class FakeCtx:
            body = {}
            query = {}
            def get_project_id(self):
                return "test-proj"

        result = handle_version_check(FakeCtx())

    assert "gov_runtime_version" in result
    assert "sm_runtime_version" in result
    assert "runtime_match" in result
    assert isinstance(result["gov_runtime_version"], str)
    assert isinstance(result["sm_runtime_version"], str)
    assert isinstance(result["runtime_match"], bool)


def test_version_check_contains_runtime_fields_no_row():
    """version-check response without a DB row must also contain runtime version keys."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE project_version (
            project_id TEXT PRIMARY KEY,
            chain_version TEXT,
            updated_at TEXT,
            git_head TEXT,
            dirty_files TEXT,
            git_synced_at TEXT
        )
    """)
    conn.commit()

    with mock.patch("agent.governance.server.get_connection", return_value=conn), \
         mock.patch("agent.governance.server._utc_now", return_value="2026-01-01T00:00:00Z"):
        from agent.governance.server import handle_version_check

        class FakeCtx:
            body = {}
            query = {}
            def get_project_id(self):
                return "no-such-project"

        result = handle_version_check(FakeCtx())

    assert "gov_runtime_version" in result
    assert "sm_runtime_version" in result
    assert "runtime_match" in result
