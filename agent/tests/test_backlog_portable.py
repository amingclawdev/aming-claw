from __future__ import annotations

import json
import sqlite3

import pytest
from unittest.mock import MagicMock

from agent.governance import backlog_portable


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED_VOLUME_PATH", str(tmp_path))
    from agent.governance.db import get_connection

    db = get_connection("portable-project")
    try:
        yield db
    finally:
        db.close()


def _insert_bug(conn: sqlite3.Connection, bug_id: str, **overrides):
    row = {
        "bug_id": bug_id,
        "title": "Portable backlog",
        "status": "OPEN",
        "priority": "P2",
        "target_files": json.dumps(["agent/a.py"]),
        "test_files": json.dumps(["agent/tests/test_a.py"]),
        "acceptance_criteria": json.dumps(["exportable"]),
        "chain_task_id": "",
        "commit": "",
        "discovered_at": "",
        "fixed_at": "",
        "details_md": "details",
        "chain_trigger_json": json.dumps({"type": "dev"}),
        "required_docs": json.dumps(["README.md"]),
        "provenance_paths": json.dumps(["docs/source.md"]),
        "chain_stage": "",
        "last_failure_reason": "",
        "stage_updated_at": "",
        "runtime_state": "",
        "current_task_id": "",
        "root_task_id": "",
        "worktree_path": "",
        "worktree_branch": "",
        "bypass_policy_json": json.dumps({"mf_type": "chain_rescue"}),
        "mf_type": "chain_rescue",
        "takeover_json": json.dumps({"worker": "observer"}),
        "runtime_updated_at": "",
        "created_at": "2026-05-16T00:00:00Z",
        "updated_at": "2026-05-16T00:00:00Z",
    }
    row.update(overrides)
    columns = list(row)
    quoted_columns = ", ".join(f'"{col}"' for col in columns)
    conn.execute(
        f"INSERT INTO backlog_bugs ({quoted_columns}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        [row[col] for col in columns],
    )
    conn.commit()


def test_export_backlog_portable_uses_structured_json_fields(conn):
    _insert_bug(conn, "BUG-EXPORT")

    payload = backlog_portable.export_backlog_portable(conn, "portable-project")

    assert payload["schema"] == backlog_portable.BACKLOG_EXPORT_SCHEMA
    assert payload["schema_version"] == backlog_portable.BACKLOG_EXPORT_SCHEMA_VERSION
    assert payload["project_id"] == "portable-project"
    assert payload["row_count"] == 1
    row = payload["rows"][0]
    assert row["bug_id"] == "BUG-EXPORT"
    assert row["target_files"] == ["agent/a.py"]
    assert row["chain_trigger_json"] == {"type": "dev"}
    assert row["bypass_policy_json"] == {"mf_type": "chain_rescue"}


def test_import_backlog_portable_inserts_rows(conn):
    payload = {
        "schema": backlog_portable.BACKLOG_EXPORT_SCHEMA,
        "schema_version": 1,
        "project_id": "source-project",
        "rows": [
            {
                "bug_id": "BUG-IMPORT",
                "title": "Imported backlog",
                "status": "OPEN",
                "priority": "P1",
                "target_files": ["agent/new.py"],
                "chain_trigger_json": {"type": "test"},
                "created_at": "2026-05-15T00:00:00Z",
                "updated_at": "2026-05-15T00:00:00Z",
            }
        ],
    }

    result = backlog_portable.import_backlog_portable(conn, "portable-project", payload)

    assert result["ok"] is True
    assert result["inserted_count"] == 1
    row = conn.execute("SELECT * FROM backlog_bugs WHERE bug_id='BUG-IMPORT'").fetchone()
    assert row["title"] == "Imported backlog"
    assert json.loads(row["target_files"]) == ["agent/new.py"]
    assert json.loads(row["chain_trigger_json"]) == {"type": "test"}


def test_import_backlog_portable_conflict_skip_overwrite_and_fail(conn):
    _insert_bug(conn, "BUG-CONFLICT", title="Old")
    payload = {
        "schema": backlog_portable.BACKLOG_EXPORT_SCHEMA,
        "schema_version": 1,
        "project_id": "source-project",
        "rows": [{"bug_id": "BUG-CONFLICT", "title": "New", "status": "OPEN"}],
    }

    skipped = backlog_portable.import_backlog_portable(conn, "portable-project", payload, on_conflict="skip")
    assert skipped["ok"] is True
    assert skipped["skipped_count"] == 1
    assert conn.execute("SELECT title FROM backlog_bugs WHERE bug_id='BUG-CONFLICT'").fetchone()[0] == "Old"

    failed = backlog_portable.import_backlog_portable(conn, "portable-project", payload, on_conflict="fail")
    assert failed["ok"] is False
    assert failed["error_count"] == 1
    assert conn.execute("SELECT title FROM backlog_bugs WHERE bug_id='BUG-CONFLICT'").fetchone()[0] == "Old"

    updated = backlog_portable.import_backlog_portable(conn, "portable-project", payload, on_conflict="overwrite")
    assert updated["ok"] is True
    assert updated["updated_count"] == 1
    assert conn.execute("SELECT title FROM backlog_bugs WHERE bug_id='BUG-CONFLICT'").fetchone()[0] == "New"


def test_import_backlog_portable_dry_run_does_not_write(conn):
    payload = {
        "schema": backlog_portable.BACKLOG_EXPORT_SCHEMA,
        "schema_version": 1,
        "project_id": "source-project",
        "rows": [{"bug_id": "BUG-DRY", "title": "Dry run"}],
    }

    result = backlog_portable.import_backlog_portable(conn, "portable-project", payload, dry_run=True)

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["inserted_count"] == 1
    assert conn.execute("SELECT 1 FROM backlog_bugs WHERE bug_id='BUG-DRY'").fetchone() is None


def test_import_backlog_portable_rejects_newer_schema(conn):
    payload = {
        "schema": backlog_portable.BACKLOG_EXPORT_SCHEMA,
        "schema_version": backlog_portable.BACKLOG_EXPORT_SCHEMA_VERSION + 1,
        "rows": [],
    }

    with pytest.raises(ValueError, match="unsupported backlog export schema_version"):
        backlog_portable.import_backlog_portable(conn, "portable-project", payload)


def _ctx(path_params, query=None, body=None):
    ctx = MagicMock()
    ctx.path_params = path_params
    ctx.query = query or {}
    ctx.body = body or {}
    return ctx


def test_backlog_portable_export_and_import_handlers(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED_VOLUME_PATH", str(tmp_path))
    from agent.governance.db import get_connection
    from agent.governance import server

    conn = get_connection("portable-api")
    try:
        _insert_bug(conn, "BUG-API")
    finally:
        conn.close()

    exported = server.handle_backlog_portable_export(_ctx({"project_id": "portable-api"}))
    assert exported["row_count"] == 1
    assert exported["rows"][0]["bug_id"] == "BUG-API"

    imported = server.handle_backlog_portable_import(
        _ctx(
            {"project_id": "portable-api-target"},
            body={"payload": exported, "on_conflict": "skip", "actor": "test"},
        )
    )
    assert imported["ok"] is True
    assert imported["inserted_count"] == 1


def test_backlog_portable_routes_do_not_collide_with_bug_id_routes():
    from agent.governance import server

    handler = object.__new__(server.GovernanceHandler)
    handler.path = "/api/backlog/portable-api/portable/export"
    found, params, _ = handler._find_handler("GET")
    assert found is server.handle_backlog_portable_export
    assert params == {"project_id": "portable-api"}

    handler.path = "/api/backlog/portable-api/BUG-1"
    found, params, _ = handler._find_handler("GET")
    assert found is server.handle_backlog_get
    assert params == {"project_id": "portable-api", "bug_id": "BUG-1"}
