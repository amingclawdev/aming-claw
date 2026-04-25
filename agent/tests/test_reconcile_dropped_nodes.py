"""Tests for scripts/reconcile-dropped-nodes.py.

Uses tempdir with seeded SQLite DB (tasks + node_state tables) containing
fake PM tasks with proposed_nodes in result_json; validates both dry-run
report generation and live-run node_state insertion (R7).
"""

import json
import os
import sqlite3
import sys
import tempfile

import pytest

# Add project root to path so we can import the script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts import __path__ as _  # noqa: F401 — ensure scripts is a package-like importable

# We import the functions directly from the script module
import importlib.util

_SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "reconcile-dropped-nodes.py"
)
_spec = importlib.util.spec_from_file_location("reconcile_dropped_nodes", _SCRIPT_PATH)
reconcile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reconcile)


# ---------- fixtures ----------

def _create_schema(conn):
    """Create minimal tasks + node_state tables."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'created',
            type TEXT NOT NULL DEFAULT 'task',
            prompt TEXT,
            related_nodes TEXT,
            assigned_to TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            result_json TEXT,
            error_message TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            priority INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT,
            retry_round INTEGER NOT NULL DEFAULT 0,
            parent_task_id TEXT
        );

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
        );
    """)


def _seed_pm_task(conn, task_id, proposed_nodes, completed_at="2026-04-20T10:00:00"):
    """Insert a fake PM task with proposed_nodes in result_json."""
    result = {"proposed_nodes": proposed_nodes}
    conn.execute(
        """
        INSERT INTO tasks (task_id, project_id, status, type, created_at,
                           updated_at, completed_at, result_json)
        VALUES (?, 'aming-claw', 'succeeded', 'pm', ?, ?, ?, ?)
        """,
        (task_id, completed_at, completed_at, completed_at, json.dumps(result)),
    )
    conn.commit()


def _seed_existing_node(conn, node_id):
    """Insert an existing node_state row (so it's NOT dropped)."""
    conn.execute(
        """
        INSERT INTO node_state (project_id, node_id, verify_status, updated_by, updated_at)
        VALUES ('aming-claw', ?, 'qa_pass', 'test', '2026-04-18T00:00:00')
        """,
        (node_id,),
    )
    conn.commit()


@pytest.fixture
def workspace(tmp_path):
    """Create a temp workspace with a seeded governance.db."""
    db_path = tmp_path / "governance.db"
    conn = sqlite3.connect(str(db_path))
    _create_schema(conn)

    # PM task 1: 3 proposed nodes, 1 already exists in node_state
    _seed_pm_task(conn, "pm-task-001", [
        {"node_id": "L3.10", "title": "Widget API", "description": "Widget API endpoint"},
        {"node_id": "L3.11", "title": "Widget UI", "description": "Widget UI component"},
        {"node_id": "L3.12", "title": "Widget Tests", "description": "Widget test suite"},
    ])
    _seed_existing_node(conn, "L3.10")  # This one exists, so NOT dropped

    # PM task 2: 2 proposed nodes, both missing from node_state
    _seed_pm_task(conn, "pm-task-002", [
        {"node_id": "L4.1", "title": "Auth Module", "description": "Authentication module"},
        {"node_id": "L4.2", "title": "Auth Tests", "description": "Auth test suite"},
    ], completed_at="2026-04-22T15:00:00")

    # PM task 3: old task (before cutoff), should be ignored
    _seed_pm_task(conn, "pm-task-old", [
        {"node_id": "L2.99", "title": "Old Node", "description": "Should be ignored"},
    ], completed_at="2026-04-10T00:00:00")

    # PM task 4: failed task, should be ignored
    conn.execute(
        """
        INSERT INTO tasks (task_id, project_id, status, type, created_at,
                           updated_at, completed_at, result_json)
        VALUES ('pm-task-failed', 'aming-claw', 'failed', 'pm',
                '2026-04-20', '2026-04-20', '2026-04-20', ?)
        """,
        (json.dumps({"proposed_nodes": [{"node_id": "L5.1", "title": "Fail"}]}),),
    )
    conn.commit()

    conn.close()

    # Create docs/dev/scratch dir
    scratch_dir = tmp_path / "docs" / "dev" / "scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    return tmp_path


# ---------- tests ----------


class TestFetchPMTasks:
    def test_finds_pm_tasks_since_cutoff(self, workspace):
        conn = sqlite3.connect(str(workspace / "governance.db"))
        tasks = reconcile.fetch_pm_tasks_with_proposed_nodes(conn)
        conn.close()

        task_ids = [t["task_id"] for t in tasks]
        assert "pm-task-001" in task_ids
        assert "pm-task-002" in task_ids
        # Old task before cutoff should be excluded
        assert "pm-task-old" not in task_ids
        # Failed task should be excluded
        assert "pm-task-failed" not in task_ids

    def test_extracts_proposed_nodes(self, workspace):
        conn = sqlite3.connect(str(workspace / "governance.db"))
        tasks = reconcile.fetch_pm_tasks_with_proposed_nodes(conn)
        conn.close()

        task1 = next(t for t in tasks if t["task_id"] == "pm-task-001")
        assert len(task1["proposed_nodes"]) == 3
        assert task1["proposed_nodes"][0]["node_id"] == "L3.10"


class TestFindDroppedNodes:
    def test_identifies_dropped_nodes(self, workspace):
        conn = sqlite3.connect(str(workspace / "governance.db"))
        tasks = reconcile.fetch_pm_tasks_with_proposed_nodes(conn)
        dropped = reconcile.find_dropped_nodes(conn, tasks)
        conn.close()

        dropped_ids = [d["node_id"] for d in dropped]
        # L3.10 exists in node_state, so NOT dropped
        assert "L3.10" not in dropped_ids
        # These are missing from node_state, so they ARE dropped
        assert "L3.11" in dropped_ids
        assert "L3.12" in dropped_ids
        assert "L4.1" in dropped_ids
        assert "L4.2" in dropped_ids
        assert len(dropped) == 4

    def test_dropped_entry_has_metadata(self, workspace):
        conn = sqlite3.connect(str(workspace / "governance.db"))
        tasks = reconcile.fetch_pm_tasks_with_proposed_nodes(conn)
        dropped = reconcile.find_dropped_nodes(conn, tasks)
        conn.close()

        entry = next(d for d in dropped if d["node_id"] == "L3.11")
        assert entry["title"] == "Widget UI"
        assert entry["source_task"] == "pm-task-001"
        assert entry["completed_at"] is not None


class TestDryRun:
    def test_generates_report_file(self, workspace):
        """AC2: Dry-run mode creates report with counts and sample entries."""
        exit_code = reconcile.main([
            "--dry-run",
            "--workspace", str(workspace),
        ])
        assert exit_code == 0

        # Find the generated report
        scratch_dir = workspace / "docs" / "dev" / "scratch"
        reports = list(scratch_dir.glob("reconcile-dropped-nodes-*.md"))
        assert len(reports) == 1

        content = reports[0].read_text(encoding="utf-8")
        assert "dropped nodes found:** 4" in content
        assert "L3.11" in content or "L3.12" in content  # sample entries

    def test_does_not_insert_nodes(self, workspace):
        """Dry-run should NOT modify node_state."""
        reconcile.main(["--dry-run", "--workspace", str(workspace)])

        conn = sqlite3.connect(str(workspace / "governance.db"))
        cursor = conn.execute(
            "SELECT COUNT(*) FROM node_state WHERE updated_by = 'reconcile-script'"
        )
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 0


class TestLiveRun:
    def test_inserts_dropped_nodes(self, workspace):
        """AC3/AC4: Live run inserts node_state rows with correct attributes."""
        exit_code = reconcile.main([
            "--no-dry-run",
            "--workspace", str(workspace),
        ])
        assert exit_code == 0

        conn = sqlite3.connect(str(workspace / "governance.db"))
        cursor = conn.execute(
            """
            SELECT node_id, verify_status, evidence_json, updated_by
            FROM node_state
            WHERE updated_by = 'reconcile-script'
            ORDER BY node_id
            """
        )
        rows = cursor.fetchall()
        conn.close()

        assert len(rows) == 4
        node_ids = [r[0] for r in rows]
        assert "L3.11" in node_ids
        assert "L3.12" in node_ids
        assert "L4.1" in node_ids
        assert "L4.2" in node_ids

        # AC4: verify_status='waived'
        for row in rows:
            assert row[1] == "waived"
            evidence = json.loads(row[2])
            assert evidence["waive_reason"] == "reconcile_apr15_dropped_proposed_nodes"
            assert row[3] == "reconcile-script"

    def test_idempotent(self, workspace):
        """Running live twice should not fail or duplicate."""
        reconcile.main(["--no-dry-run", "--workspace", str(workspace)])
        exit_code = reconcile.main(["--no-dry-run", "--workspace", str(workspace)])
        assert exit_code == 0

        conn = sqlite3.connect(str(workspace / "governance.db"))
        cursor = conn.execute(
            "SELECT COUNT(*) FROM node_state WHERE updated_by = 'reconcile-script'"
        )
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 4  # no duplicates


class TestEdgeCases:
    def test_missing_db_returns_error(self, tmp_path):
        """R6: exit 1 on error."""
        exit_code = reconcile.main([
            "--workspace", str(tmp_path / "nonexistent"),
        ])
        assert exit_code == 1

    def test_empty_db(self, tmp_path):
        """No PM tasks = 0 dropped, exit 0."""
        db_path = tmp_path / "governance.db"
        conn = sqlite3.connect(str(db_path))
        _create_schema(conn)
        conn.close()

        exit_code = reconcile.main(["--workspace", str(tmp_path)])
        assert exit_code == 0


class TestArgparse:
    def test_dry_run_default_true(self):
        """AC1: --dry-run defaults to True."""
        args = reconcile.parse_args([])
        assert args.dry_run is True

    def test_no_dry_run_flag(self):
        args = reconcile.parse_args(["--no-dry-run"])
        assert args.dry_run is False

    def test_workspace_default(self):
        args = reconcile.parse_args([])
        assert args.workspace == "."
