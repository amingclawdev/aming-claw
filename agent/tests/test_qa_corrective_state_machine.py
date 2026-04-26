"""Tests for QA corrective state machine: baseline merge status enum + corrective PM lifecycle.

Covers AC1-AC7 from PRD task-1777215235-61c777.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

# Ensure agent package is importable
_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.baseline_service import (
    ALLOWED_MERGE_STATUSES,
    MERGE_STATUS_QA_VERIFIED,
    MERGE_STATUS_MERGED,
    MERGE_STATUS_ABANDONED,
    MERGE_STATUS_SUPERSEDED,
    set_baseline_merge_status,
    create_baseline,
)
from governance.auto_chain import (
    TASK_STATUS_BLOCKED_BY_CORRECTIVE,
    TASK_STATUS_HUMAN_REVIEW_REQUIRED,
    MAX_QA_CORRECTIVE_ROUNDS,
    spawn_corrective_pm,
    on_corrective_chain_complete,
)
from governance.db import SCHEMA_VERSION, _run_migrations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db():
    """Create an in-memory SQLite database with the governance schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    # Minimal schema needed for tests
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS version_baselines (
            project_id        TEXT NOT NULL,
            baseline_id       INTEGER NOT NULL,
            chain_version     TEXT NOT NULL,
            graph_sha         TEXT NOT NULL DEFAULT '',
            code_doc_map_sha  TEXT NOT NULL DEFAULT '',
            node_state_snap   TEXT NOT NULL DEFAULT '{}',
            chain_event_max   INTEGER NOT NULL DEFAULT 0,
            trigger           TEXT NOT NULL DEFAULT '',
            triggered_by      TEXT NOT NULL DEFAULT '',
            reconstructed     INTEGER NOT NULL DEFAULT 0,
            created_at        TEXT NOT NULL DEFAULT '',
            notes             TEXT NOT NULL DEFAULT '',
            scope_id          TEXT,
            parent_baseline_id INTEGER,
            scope_kind        TEXT,
            scope_value       TEXT,
            merged_into       INTEGER,
            merge_status      TEXT,
            merge_evidence_json TEXT,
            mutations_sha256  TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (project_id, baseline_id)
        );

        CREATE TABLE IF NOT EXISTS tasks (
            task_id       TEXT PRIMARY KEY,
            project_id    TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'created',
            type          TEXT NOT NULL DEFAULT 'task',
            prompt        TEXT,
            related_nodes TEXT,
            assigned_to   TEXT,
            created_by    TEXT,
            created_at    TEXT NOT NULL DEFAULT '',
            updated_at    TEXT NOT NULL DEFAULT '',
            started_at    TEXT,
            completed_at  TEXT,
            result_json   TEXT,
            error_message TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts  INTEGER NOT NULL DEFAULT 3,
            priority      INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT,
            retry_round   INTEGER NOT NULL DEFAULT 0,
            parent_task_id TEXT
        );

        CREATE TABLE IF NOT EXISTS backlog_bugs (
            bug_id       TEXT PRIMARY KEY,
            title        TEXT NOT NULL DEFAULT '',
            status       TEXT NOT NULL DEFAULT 'OPEN',
            priority     TEXT NOT NULL DEFAULT 'P3',
            created_at   TEXT NOT NULL DEFAULT '',
            updated_at   TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS project_version (
            project_id    TEXT PRIMARY KEY,
            chain_version TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            updated_by    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chain_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            root_task_id  TEXT NOT NULL,
            task_id       TEXT NOT NULL,
            event_type    TEXT NOT NULL,
            payload_json  TEXT NOT NULL,
            ts            TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS schema_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    return conn


def _insert_task(conn, task_id, project_id, status="queued", task_type="qa",
                 metadata=None, retry_round=0, parent_task_id=None):
    """Insert a task row for testing."""
    meta_json = json.dumps(metadata or {}, sort_keys=True)
    conn.execute(
        """INSERT INTO tasks
           (task_id, project_id, status, type, metadata_json, retry_round,
            parent_task_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
        (task_id, project_id, status, task_type, meta_json, retry_round, parent_task_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# AC1: Baseline merge status enum validates
# ---------------------------------------------------------------------------

def test_baseline_status_enum_validates():
    """AC1: ALLOWED_MERGE_STATUSES has 4 members; set_baseline_merge_status rejects invalid."""
    assert len(ALLOWED_MERGE_STATUSES) == 4
    assert MERGE_STATUS_QA_VERIFIED in ALLOWED_MERGE_STATUSES
    assert MERGE_STATUS_MERGED in ALLOWED_MERGE_STATUSES
    assert MERGE_STATUS_ABANDONED in ALLOWED_MERGE_STATUSES
    assert MERGE_STATUS_SUPERSEDED in ALLOWED_MERGE_STATUSES

    conn = _make_db()
    # Insert a baseline row to update
    conn.execute(
        """INSERT INTO version_baselines (project_id, baseline_id, chain_version, created_at)
           VALUES ('test', 1, 'abc123', '2026-01-01')""",
    )
    conn.commit()

    # Valid status should succeed
    set_baseline_merge_status(conn, "test", 1, MERGE_STATUS_QA_VERIFIED)

    # Invalid status should raise ValueError
    with pytest.raises(ValueError, match="status must be one of"):
        set_baseline_merge_status(conn, "test", 1, "invalid_status")

    conn.close()


# ---------------------------------------------------------------------------
# AC1 continued: Baseline status transitions
# ---------------------------------------------------------------------------

def test_baseline_status_transitions():
    """AC1: set_baseline_merge_status transitions correctly through allowed statuses."""
    conn = _make_db()
    conn.execute(
        """INSERT INTO version_baselines (project_id, baseline_id, chain_version, created_at)
           VALUES ('test', 1, 'abc123', '2026-01-01')""",
    )
    conn.commit()

    for status in [MERGE_STATUS_QA_VERIFIED, MERGE_STATUS_MERGED,
                   MERGE_STATUS_ABANDONED, MERGE_STATUS_SUPERSEDED]:
        set_baseline_merge_status(conn, "test", 1, status)
        row = conn.execute(
            "SELECT merge_status FROM version_baselines WHERE project_id = 'test' AND baseline_id = 1"
        ).fetchone()
        assert row["merge_status"] == status

    conn.close()


# ---------------------------------------------------------------------------
# AC2: db.py SCHEMA_VERSION == 24 + migration adds mutations_sha256
# ---------------------------------------------------------------------------

def test_schema_version_24_and_migration():
    """AC2: SCHEMA_VERSION == 24; _migrate_v23_to_v24 adds mutations_sha256."""
    assert SCHEMA_VERSION == 24

    # Create a v23 database and run migration
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE version_baselines (
            project_id TEXT NOT NULL,
            baseline_id INTEGER NOT NULL,
            chain_version TEXT NOT NULL,
            PRIMARY KEY (project_id, baseline_id)
        )
    """)
    # Run migration v23->v24
    _run_migrations(conn, 23, 24)

    # Verify mutations_sha256 column exists
    conn.execute(
        "INSERT INTO version_baselines (project_id, baseline_id, chain_version, mutations_sha256) "
        "VALUES ('test', 1, 'abc', '{}')"
    )
    row = conn.execute(
        "SELECT mutations_sha256 FROM version_baselines WHERE project_id = 'test'"
    ).fetchone()
    assert row["mutations_sha256"] == "{}"
    conn.close()


# ---------------------------------------------------------------------------
# AC3 + R4: create_baseline persists mutations_sha256
# ---------------------------------------------------------------------------

def test_create_baseline_persists_mutations_sha256(tmp_path, monkeypatch):
    """AC3/R4: create_baseline accepts and persists mutations_sha256."""
    # Patch _governance_root to use tmp_path
    import governance.baseline_service as bs
    monkeypatch.setattr("governance.db._governance_root", lambda: tmp_path)

    conn = _make_db()
    mutations = {"agent/foo.py": "sha256abc", "agent/bar.py": "sha256def"}
    result = create_baseline(
        conn, "test-proj", chain_version="abc123",
        trigger="init", triggered_by="init",
        mutations_sha256=mutations,
    )
    assert result["baseline_id"] == 1

    row = conn.execute(
        "SELECT mutations_sha256 FROM version_baselines WHERE project_id = 'test-proj' AND baseline_id = 1"
    ).fetchone()
    persisted = json.loads(row["mutations_sha256"])
    assert persisted == mutations
    conn.close()


# ---------------------------------------------------------------------------
# AC4: spawn_corrective_pm first round succeeds
# ---------------------------------------------------------------------------

def test_spawn_corrective_first_round():
    """AC4: spawn_corrective_pm spawns child PM on first round and blocks parent."""
    conn = _make_db()
    _insert_task(conn, "qa-1", "proj", status="failed", task_type="qa",
                 metadata={"qa_corrective_round": 0})

    child_id = spawn_corrective_pm(conn, "proj", "qa-1", "tests failed", "BUG-001")
    assert child_id is not None

    # Parent should be blocked
    parent = conn.execute("SELECT status FROM tasks WHERE task_id = 'qa-1'").fetchone()
    assert parent["status"] == TASK_STATUS_BLOCKED_BY_CORRECTIVE

    # Child should exist as queued PM
    child = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (child_id,)).fetchone()
    assert child["type"] == "pm"
    assert child["status"] == "queued"
    assert child["parent_task_id"] == "qa-1"

    child_meta = json.loads(child["metadata_json"])
    assert child_meta["bug_id"] == "BUG-001"
    assert child_meta["parent_task_id"] == "qa-1"
    assert child_meta["qa_corrective_round"] == 1
    assert child_meta["qa_failure_reason"] == "tests failed"
    conn.close()


# ---------------------------------------------------------------------------
# AC4: spawn_corrective_pm second round blocked (human review)
# ---------------------------------------------------------------------------

def test_spawn_corrective_second_round_blocked():
    """AC4: spawn_corrective_pm returns None and marks human_review_required at round limit."""
    conn = _make_db()
    _insert_task(conn, "qa-2", "proj", status="failed", task_type="qa",
                 metadata={"qa_corrective_round": MAX_QA_CORRECTIVE_ROUNDS})

    result = spawn_corrective_pm(conn, "proj", "qa-2", "tests failed again", "BUG-002")
    assert result is None

    parent = conn.execute("SELECT status FROM tasks WHERE task_id = 'qa-2'").fetchone()
    assert parent["status"] == TASK_STATUS_HUMAN_REVIEW_REQUIRED
    conn.close()


# ---------------------------------------------------------------------------
# AC5: spawn_corrective_pm dedup guard
# ---------------------------------------------------------------------------

def test_spawn_corrective_dedup_no_double_spawn():
    """AC5: spawn_corrective_pm returns None if existing OPEN child PM with same bug_id."""
    conn = _make_db()
    _insert_task(conn, "qa-3", "proj", status="failed", task_type="qa",
                 metadata={"qa_corrective_round": 0})

    # First spawn succeeds
    child1 = spawn_corrective_pm(conn, "proj", "qa-3", "tests failed", "BUG-003")
    assert child1 is not None

    # Reset parent so we can try again
    conn.execute("UPDATE tasks SET status = 'failed' WHERE task_id = 'qa-3'")
    conn.execute(
        "UPDATE tasks SET metadata_json = ? WHERE task_id = 'qa-3'",
        (json.dumps({"qa_corrective_round": 0}),),
    )
    conn.commit()

    # Second spawn with same bug_id should be deduped
    child2 = spawn_corrective_pm(conn, "proj", "qa-3", "tests failed", "BUG-003")
    assert child2 is None
    conn.close()


# ---------------------------------------------------------------------------
# AC6: on_corrective_chain_complete re-enqueues parent
# ---------------------------------------------------------------------------

def test_on_corrective_chain_complete_reenqueues_parent():
    """AC6: on_corrective_chain_complete sets parent to queued and increments retry_round."""
    conn = _make_db()
    _insert_task(conn, "qa-4", "proj", status=TASK_STATUS_BLOCKED_BY_CORRECTIVE,
                 task_type="qa", retry_round=0)

    on_corrective_chain_complete(conn, "proj", "qa-4")

    parent = conn.execute("SELECT status, retry_round FROM tasks WHERE task_id = 'qa-4'").fetchone()
    assert parent["status"] == "queued"
    assert parent["retry_round"] == 1
    conn.close()
