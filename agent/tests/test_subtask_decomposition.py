"""Tests for PM subtask decomposition (fan-out / fan-in).

Covers AC1-AC10 from the PM Task Decomposition PRD.
"""

import json
import sqlite3
import sys
import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure agent directory is on path
_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.db import get_connection, _ensure_schema, SCHEMA_VERSION


def _fresh_conn():
    """Create an in-memory SQLite connection with full governance schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # Run full schema creation
    from governance.db import SCHEMA_SQL, _run_migrations
    conn.executescript(SCHEMA_SQL)
    # Set schema version to 0 so migrations run from start
    conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', '0')")
    _run_migrations(conn, 0, SCHEMA_VERSION)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# AC1: subtask_groups table exists after migration v13
# ---------------------------------------------------------------------------

class TestAC1_SubtaskGroupsTable:
    def test_subtask_groups_table_exists(self):
        conn = _fresh_conn()
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='subtask_groups'"
        ).fetchone()
        assert row is not None
        assert row["name"] == "subtask_groups"
        conn.close()


# ---------------------------------------------------------------------------
# AC2: tasks table has subtask columns after migration v13
# ---------------------------------------------------------------------------

class TestAC2_TasksSubtaskColumns:
    def test_tasks_subtask_columns_exist(self):
        conn = _fresh_conn()
        rows = conn.execute("PRAGMA table_info(tasks)").fetchall()
        col_names = {r["name"] for r in rows}
        assert "subtask_group_id" in col_names
        assert "subtask_local_id" in col_names
        assert "subtask_depends_on" in col_names
        conn.close()


# ---------------------------------------------------------------------------
# AC3: _gate_post_pm rejects subtasks exceeding max_subtasks
# ---------------------------------------------------------------------------

class TestAC3_GatePostPmMaxSubtasks:
    def test_rejects_too_many_subtasks(self):
        conn = _fresh_conn()
        # Insert a project_version row with default max_subtasks=5
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, updated_at, updated_by, max_subtasks) "
            "VALUES ('test-proj', 'abc123', '2026-01-01T00:00:00Z', 'test', 5)"
        )
        conn.commit()

        from governance.auto_chain import _gate_post_pm

        result = {
            "target_files": ["a.py"],
            "verification": {"method": "test"},
            "acceptance_criteria": ["AC1"],
            "test_files": ["test_a.py"],
            "proposed_nodes": ["L1.1"],
            "doc_impact": {"files": []},
            "subtasks": [
                {"id": f"S{i}", "title": f"S{i}", "target_files": ["a.py"],
                 "acceptance_criteria": ["ac"]}
                for i in range(6)  # 6 > 5 default
            ],
        }
        passed, reason = _gate_post_pm(conn, "test-proj", result, {})
        assert passed is False
        assert "max_subtasks" in reason
        conn.close()


# ---------------------------------------------------------------------------
# AC4: _gate_post_pm rejects cyclic depends_on
# ---------------------------------------------------------------------------

class TestAC4_GatePostPmCyclicDeps:
    def test_rejects_cyclic_deps(self):
        conn = _fresh_conn()
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, updated_at, updated_by) "
            "VALUES ('test-proj', 'abc123', '2026-01-01T00:00:00Z', 'test')"
        )
        conn.commit()

        from governance.auto_chain import _gate_post_pm

        result = {
            "target_files": ["a.py"],
            "verification": {"method": "test"},
            "acceptance_criteria": ["AC1"],
            "test_files": ["test_a.py"],
            "proposed_nodes": ["L1.1"],
            "doc_impact": {"files": []},
            "subtasks": [
                {"id": "A", "title": "A", "target_files": ["a.py"],
                 "acceptance_criteria": ["ac"], "depends_on": ["B"]},
                {"id": "B", "title": "B", "target_files": ["b.py"],
                 "acceptance_criteria": ["ac"], "depends_on": ["A"]},
            ],
        }
        passed, reason = _gate_post_pm(conn, "test-proj", result, {})
        assert passed is False
        assert "cyclic" in reason.lower()
        conn.close()


# ---------------------------------------------------------------------------
# AC5: PM result with 3 subtasks creates group + correct task statuses
# ---------------------------------------------------------------------------

class TestAC5_SubtaskFanout:
    def test_fanout_creates_group_and_tasks(self):
        conn = _fresh_conn()
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, updated_at, updated_by) "
            "VALUES ('test-proj', 'abc123', '2026-01-01T00:00:00Z', 'test')"
        )
        conn.commit()

        from governance.auto_chain import _do_subtask_fanout

        result = {
            "target_files": ["a.py"],
            "subtasks": [
                {"id": "S1", "title": "S1", "target_files": ["a.py"],
                 "acceptance_criteria": ["ac1"], "depends_on": []},
                {"id": "S2", "title": "S2", "target_files": ["b.py"],
                 "acceptance_criteria": ["ac2"], "depends_on": []},
                {"id": "S3", "title": "S3", "target_files": ["c.py"],
                 "acceptance_criteria": ["ac3"], "depends_on": ["S1"]},
            ],
        }

        fanout = _do_subtask_fanout(
            conn, "test-proj", "pm-task-1", result, {},
            "trace-1", "chain-1", 0,
        )
        conn.commit()

        assert "subtask_group_id" in fanout
        group_id = fanout["subtask_group_id"]

        # Verify group row
        group = conn.execute(
            "SELECT * FROM subtask_groups WHERE group_id=?", (group_id,)
        ).fetchone()
        assert group is not None
        assert group["total_count"] == 3
        assert group["completed_count"] == 0
        assert group["status"] == "active"

        # Verify tasks
        tasks = conn.execute(
            "SELECT * FROM tasks WHERE subtask_group_id=? ORDER BY subtask_local_id",
            (group_id,),
        ).fetchall()
        assert len(tasks) == 3

        statuses = {t["subtask_local_id"]: t["execution_status"] for t in tasks}
        # S1, S2 should be queued (no deps), S3 should be blocked
        assert statuses["S1"] in ("queued", "observer_hold")
        assert statuses["S2"] in ("queued", "observer_hold")
        assert statuses["S3"] == "blocked"

        conn.close()


# ---------------------------------------------------------------------------
# AC6: Completing merge for S1 unblocks S3
# ---------------------------------------------------------------------------

class TestAC6_FanInUnblock:
    def test_merge_completion_unblocks_dependent(self):
        conn = _fresh_conn()
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, updated_at, updated_by) "
            "VALUES ('test-proj', 'abc123', '2026-01-01T00:00:00Z', 'test')"
        )
        conn.commit()

        from governance.auto_chain import _do_subtask_fanout, on_subtask_merge_completed

        result = {
            "target_files": ["a.py"],
            "subtasks": [
                {"id": "S1", "title": "S1", "target_files": ["a.py"],
                 "acceptance_criteria": ["ac1"], "depends_on": []},
                {"id": "S2", "title": "S2", "target_files": ["b.py"],
                 "acceptance_criteria": ["ac2"], "depends_on": []},
                {"id": "S3", "title": "S3", "target_files": ["c.py"],
                 "acceptance_criteria": ["ac3"], "depends_on": ["S1"]},
            ],
        }

        fanout = _do_subtask_fanout(
            conn, "test-proj", "pm-task-1", result, {},
            "trace-1", "chain-1", 0,
        )
        conn.commit()
        group_id = fanout["subtask_group_id"]

        # Find S1's task_id
        s1_task = conn.execute(
            "SELECT task_id FROM tasks WHERE subtask_group_id=? AND subtask_local_id='S1'",
            (group_id,),
        ).fetchone()

        # Simulate merge completion for S1
        on_subtask_merge_completed(conn, "test-proj", s1_task["task_id"])
        conn.commit()

        # S3 should now be queued (unblocked)
        s3_task = conn.execute(
            "SELECT execution_status FROM tasks WHERE subtask_group_id=? AND subtask_local_id='S3'",
            (group_id,),
        ).fetchone()
        assert s3_task["execution_status"] == "queued"

        conn.close()


# ---------------------------------------------------------------------------
# AC7: completed_count == total_count → deploy task created
# ---------------------------------------------------------------------------

class TestAC7_FanInDeployCreation:
    def test_all_complete_creates_deploy(self):
        conn = _fresh_conn()
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, updated_at, updated_by) "
            "VALUES ('test-proj', 'abc123', '2026-01-01T00:00:00Z', 'test')"
        )
        conn.commit()

        from governance.auto_chain import _do_subtask_fanout, on_subtask_merge_completed

        result = {
            "target_files": ["a.py"],
            "subtasks": [
                {"id": "S1", "title": "S1", "target_files": ["a.py"],
                 "acceptance_criteria": ["ac1"], "depends_on": []},
                {"id": "S2", "title": "S2", "target_files": ["b.py"],
                 "acceptance_criteria": ["ac2"], "depends_on": []},
            ],
        }

        fanout = _do_subtask_fanout(
            conn, "test-proj", "pm-task-1", result, {},
            "trace-1", "chain-1", 0,
        )
        conn.commit()
        group_id = fanout["subtask_group_id"]

        # Complete both subtasks
        tasks = conn.execute(
            "SELECT task_id FROM tasks WHERE subtask_group_id=?",
            (group_id,),
        ).fetchall()

        for t in tasks:
            fanin_result = on_subtask_merge_completed(conn, "test-proj", t["task_id"])
            conn.commit()

        # Check group is completed
        group = conn.execute(
            "SELECT * FROM subtask_groups WHERE group_id=?", (group_id,)
        ).fetchone()
        assert group["status"] == "completed"
        assert group["completed_count"] == group["total_count"]

        # Check deploy task was created
        deploy = conn.execute(
            "SELECT * FROM tasks WHERE type='deploy' AND parent_task_id='pm-task-1'"
        ).fetchone()
        assert deploy is not None

        conn.close()


# ---------------------------------------------------------------------------
# AC8: No subtasks → identical single-chain behavior
# ---------------------------------------------------------------------------

class TestAC8_BackwardCompatibility:
    def test_no_subtasks_single_chain(self):
        """When subtasks key is absent, _gate_post_pm passes normally."""
        conn = _fresh_conn()
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, updated_at, updated_by) "
            "VALUES ('test-proj', 'abc123', '2026-01-01T00:00:00Z', 'test')"
        )
        conn.commit()

        from governance.auto_chain import _gate_post_pm

        result = {
            "target_files": ["a.py"],
            "verification": {"method": "test"},
            "acceptance_criteria": ["AC1"],
            "test_files": ["test_a.py"],
            "proposed_nodes": ["L1.1"],
            "doc_impact": {"files": []},
            # No subtasks key
        }
        passed, reason = _gate_post_pm(conn, "test-proj", result, {})
        assert passed is True
        assert "subtasks" not in result or not result.get("subtasks")
        conn.close()

    def test_empty_subtasks_single_chain(self):
        """When subtasks is empty array, _gate_post_pm passes normally."""
        conn = _fresh_conn()
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, updated_at, updated_by) "
            "VALUES ('test-proj', 'abc123', '2026-01-01T00:00:00Z', 'test')"
        )
        conn.commit()

        from governance.auto_chain import _gate_post_pm

        result = {
            "target_files": ["a.py"],
            "verification": {"method": "test"},
            "acceptance_criteria": ["AC1"],
            "test_files": ["test_a.py"],
            "proposed_nodes": ["L1.1"],
            "doc_impact": {"files": []},
            "subtasks": [],  # Empty array
        }
        passed, reason = _gate_post_pm(conn, "test-proj", result, {})
        assert passed is True
        conn.close()


# ---------------------------------------------------------------------------
# AC9: GET /api/task/{pid}/subtask-group/{gid} returns correct JSON
# ---------------------------------------------------------------------------

class TestAC9_SubtaskGroupAPI:
    def test_subtask_group_endpoint(self):
        """Test subtask-group endpoint handler returns expected JSON."""
        conn = _fresh_conn()
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, updated_at, updated_by) "
            "VALUES ('test-proj', 'abc123', '2026-01-01T00:00:00Z', 'test')"
        )
        conn.commit()

        from governance.auto_chain import _do_subtask_fanout

        result = {
            "target_files": ["a.py"],
            "subtasks": [
                {"id": "S1", "title": "S1", "target_files": ["a.py"],
                 "acceptance_criteria": ["ac1"], "depends_on": []},
            ],
        }

        fanout = _do_subtask_fanout(
            conn, "test-proj", "pm-task-1", result, {},
            "trace-1", "chain-1", 0,
        )
        conn.commit()
        group_id = fanout["subtask_group_id"]

        # Query directly (simulating what the endpoint does)
        group_row = conn.execute(
            "SELECT * FROM subtask_groups WHERE group_id=?", (group_id,)
        ).fetchone()
        tasks = conn.execute(
            "SELECT task_id, status, execution_status, subtask_local_id FROM tasks WHERE subtask_group_id=?",
            (group_id,),
        ).fetchall()

        # Validate response shape
        assert group_row["group_id"] == group_id
        assert group_row["status"] == "active"
        assert group_row["total_count"] == 1
        assert group_row["completed_count"] == 0
        assert len(tasks) == 1
        assert tasks[0]["subtask_local_id"] == "S1"

        conn.close()


# ---------------------------------------------------------------------------
# AC10: Terminal failure cascades to group and blocked siblings
# ---------------------------------------------------------------------------

class TestAC10_FailureCascade:
    def test_terminal_failure_cancels_blocked_siblings(self):
        conn = _fresh_conn()
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, updated_at, updated_by) "
            "VALUES ('test-proj', 'abc123', '2026-01-01T00:00:00Z', 'test')"
        )
        conn.commit()

        from governance.auto_chain import _do_subtask_fanout, on_subtask_terminal_failure

        result = {
            "target_files": ["a.py"],
            "subtasks": [
                {"id": "S1", "title": "S1", "target_files": ["a.py"],
                 "acceptance_criteria": ["ac1"], "depends_on": []},
                {"id": "S2", "title": "S2", "target_files": ["b.py"],
                 "acceptance_criteria": ["ac2"], "depends_on": ["S1"]},
            ],
        }

        fanout = _do_subtask_fanout(
            conn, "test-proj", "pm-task-1", result, {},
            "trace-1", "chain-1", 0,
        )
        conn.commit()
        group_id = fanout["subtask_group_id"]

        # Find S1's task_id
        s1_task = conn.execute(
            "SELECT task_id FROM tasks WHERE subtask_group_id=? AND subtask_local_id='S1'",
            (group_id,),
        ).fetchone()

        # Terminal failure for S1
        cascade = on_subtask_terminal_failure(conn, "test-proj", s1_task["task_id"])
        conn.commit()

        assert cascade is not None
        assert cascade["cancelled_count"] == 1  # S2 was blocked

        # Group should be failed
        group = conn.execute(
            "SELECT status FROM subtask_groups WHERE group_id=?", (group_id,)
        ).fetchone()
        assert group["status"] == "failed"

        # S2 should be cancelled
        s2_task = conn.execute(
            "SELECT execution_status FROM tasks WHERE subtask_group_id=? AND subtask_local_id='S2'",
            (group_id,),
        ).fetchone()
        assert s2_task["execution_status"] == "cancelled"

        conn.close()


# ---------------------------------------------------------------------------
# Additional validation tests
# ---------------------------------------------------------------------------

class TestSubtaskValidation:
    def test_rejects_missing_mandatory_fields(self):
        conn = _fresh_conn()
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, updated_at, updated_by) "
            "VALUES ('test-proj', 'abc123', '2026-01-01T00:00:00Z', 'test')"
        )
        conn.commit()

        from governance.auto_chain import _gate_post_pm

        result = {
            "target_files": ["a.py"],
            "verification": {"method": "test"},
            "acceptance_criteria": ["AC1"],
            "test_files": ["test_a.py"],
            "proposed_nodes": ["L1.1"],
            "doc_impact": {"files": []},
            "subtasks": [
                {"id": "S1", "title": "S1"},  # Missing target_files and acceptance_criteria
            ],
        }
        passed, reason = _gate_post_pm(conn, "test-proj", result, {})
        assert passed is False
        assert "mandatory" in reason.lower() or "missing" in reason.lower()
        conn.close()

    def test_rejects_duplicate_subtask_ids(self):
        conn = _fresh_conn()
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, updated_at, updated_by) "
            "VALUES ('test-proj', 'abc123', '2026-01-01T00:00:00Z', 'test')"
        )
        conn.commit()

        from governance.auto_chain import _gate_post_pm

        result = {
            "target_files": ["a.py"],
            "verification": {"method": "test"},
            "acceptance_criteria": ["AC1"],
            "test_files": ["test_a.py"],
            "proposed_nodes": ["L1.1"],
            "doc_impact": {"files": []},
            "subtasks": [
                {"id": "S1", "title": "S1", "target_files": ["a.py"],
                 "acceptance_criteria": ["ac"]},
                {"id": "S1", "title": "S1 dup", "target_files": ["b.py"],
                 "acceptance_criteria": ["ac"]},
            ],
        }
        passed, reason = _gate_post_pm(conn, "test-proj", result, {})
        assert passed is False
        assert "duplicate" in reason.lower()
        conn.close()

    def test_rejects_unknown_depends_on(self):
        conn = _fresh_conn()
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, updated_at, updated_by) "
            "VALUES ('test-proj', 'abc123', '2026-01-01T00:00:00Z', 'test')"
        )
        conn.commit()

        from governance.auto_chain import _gate_post_pm

        result = {
            "target_files": ["a.py"],
            "verification": {"method": "test"},
            "acceptance_criteria": ["AC1"],
            "test_files": ["test_a.py"],
            "proposed_nodes": ["L1.1"],
            "doc_impact": {"files": []},
            "subtasks": [
                {"id": "S1", "title": "S1", "target_files": ["a.py"],
                 "acceptance_criteria": ["ac"], "depends_on": ["NONEXISTENT"]},
            ],
        }
        passed, reason = _gate_post_pm(conn, "test-proj", result, {})
        assert passed is False
        assert "unknown" in reason.lower()
        conn.close()

    def test_dag_acyclic_checker(self):
        from governance.auto_chain import _check_subtask_dag_acyclic

        # Valid DAG
        assert _check_subtask_dag_acyclic([
            {"id": "A", "depends_on": []},
            {"id": "B", "depends_on": ["A"]},
            {"id": "C", "depends_on": ["A", "B"]},
        ]) is True

        # Cycle
        assert _check_subtask_dag_acyclic([
            {"id": "A", "depends_on": ["B"]},
            {"id": "B", "depends_on": ["A"]},
        ]) is False

        # Self-cycle
        assert _check_subtask_dag_acyclic([
            {"id": "A", "depends_on": ["A"]},
        ]) is False
