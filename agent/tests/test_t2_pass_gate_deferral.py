"""Tests for t2_pass gate related_nodes deferral (R1-R4, AC1-AC6).

Covers:
  (a) Happy path: verify_update succeeds and nodes at t2_pass → pass without warning
  (b) verify_update failure deferral: vu_ok=False + nodes pending → defer with warning
  (c) over-broad nodes deferral: vu_ok=True + nodes pending → defer with warning
  (d) Test failures still hard-block regardless of node status (AC6)
"""

import logging
import sqlite3
import sys
import os
from unittest.mock import patch

import pytest

_agent_dir = os.path.join(os.path.dirname(__file__), "..")
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)


def _make_in_memory_db():
    """Create an in-memory SQLite DB with minimal schema for gate testing."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for ddl in [
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT, action TEXT, actor TEXT, ok INTEGER,
            ts TEXT, task_id TEXT, details_json TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS node_state (
            project_id TEXT, node_id TEXT, verify_status TEXT DEFAULT 'pending',
            PRIMARY KEY (project_id, node_id)
        )""",
        """CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY, project_id TEXT, type TEXT,
            status TEXT DEFAULT 'queued', metadata_json TEXT,
            trace_id TEXT, chain_id TEXT, created_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS gate_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT, task_id TEXT, gate_name TEXT,
            passed INTEGER, reason TEXT, trace_id TEXT, created_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS project_version (
            project_id TEXT PRIMARY KEY, chain_version TEXT,
            git_head TEXT, dirty_files TEXT, updated_at TEXT,
            updated_by TEXT, max_subtasks INTEGER DEFAULT 5
        )""",
    ]:
        conn.execute(ddl)
    conn.commit()
    return conn


PROJECT_ID = "test-proj"


def _seed_nodes(conn, node_ids, status="pending"):
    """Insert node_state rows at given status."""
    for nid in node_ids:
        conn.execute(
            "INSERT OR REPLACE INTO node_state (project_id, node_id, verify_status) VALUES (?, ?, ?)",
            (PROJECT_ID, nid, status),
        )
    conn.commit()


def _valid_test_result(**overrides):
    """Return a minimal passing test result for _gate_t2_pass."""
    base = {
        "test_report": {"passed": 5, "failed": 0, "tool": "pytest"},
    }
    base.update(overrides)
    return base


def _metadata_with_nodes(related_nodes):
    """Return metadata dict with given related_nodes."""
    return {"related_nodes": related_nodes}


class TestT2PassGateHappyPath:
    """AC4: vu_ok=True + nodes at t2_pass → (True, 'ok') without any warning."""

    def test_happy_path_no_warning(self, caplog):
        """When verify_update succeeds and all nodes are at t2_pass, gate passes silently."""
        from governance.auto_chain import _gate_t2_pass

        conn = _make_in_memory_db()
        _seed_nodes(conn, ["L1.1", "L1.2"], status="t2_pass")

        metadata = _metadata_with_nodes(["L1.1", "L1.2"])
        result = _valid_test_result()

        with patch("governance.auto_chain._try_verify_update", return_value=(True, "")):
            with caplog.at_level(logging.WARNING, logger="governance.auto_chain"):
                ok, reason = _gate_t2_pass(conn, PROJECT_ID, result, metadata)

        assert ok is True
        assert reason == "ok"
        # No deferral warning should be emitted
        assert "deferring related_nodes" not in caplog.text
        assert "over-broad related_nodes" not in caplog.text


class TestT2PassGateVerifyUpdateFailureDeferral:
    """AC2: vu_ok=False + nodes pending → defer with 'deferring related_nodes' warning."""

    def test_verify_update_failure_defers(self, caplog):
        """When verify_update fails and nodes are still pending, gate defers (not blocks)."""
        from governance.auto_chain import _gate_t2_pass

        conn = _make_in_memory_db()
        _seed_nodes(conn, ["L1.1", "L1.2"], status="pending")

        metadata = _metadata_with_nodes(["L1.1", "L1.2"])
        result = _valid_test_result()

        with patch(
            "governance.auto_chain._try_verify_update",
            return_value=(False, "verify_update failed for nodes ['L1.1', 'L1.2']: some error"),
        ):
            with caplog.at_level(logging.WARNING, logger="governance.auto_chain"):
                ok, reason = _gate_t2_pass(conn, PROJECT_ID, result, metadata)

        # AC2: returns (True, 'ok') instead of blocking
        assert ok is True
        assert reason == "ok"
        # R4: emits log.warning with 'deferring related_nodes'
        assert "t2_pass_gate: deferring related_nodes" in caplog.text
        assert "verify_update failed" in caplog.text


class TestT2PassGateOverBroadNodesDeferral:
    """AC3: vu_ok=True + nodes still pending (over-broad) → defer with 'over-broad' warning."""

    def test_over_broad_related_nodes_defers(self, caplog):
        """When verify_update succeeds but some neighbors remain pending, gate defers."""
        from governance.auto_chain import _gate_t2_pass

        conn = _make_in_memory_db()
        # Simulate: L1.1 promoted to t2_pass, but L1.2 is a graph neighbor still at pending
        _seed_nodes(conn, ["L1.1"], status="t2_pass")
        _seed_nodes(conn, ["L1.2"], status="pending")

        metadata = _metadata_with_nodes(["L1.1", "L1.2"])
        result = _valid_test_result()

        with patch("governance.auto_chain._try_verify_update", return_value=(True, "")):
            with caplog.at_level(logging.WARNING, logger="governance.auto_chain"):
                ok, reason = _gate_t2_pass(conn, PROJECT_ID, result, metadata)

        # AC3: returns (True, 'ok') instead of blocking
        assert ok is True
        assert reason == "ok"
        # R4: emits log.warning with 'over-broad related_nodes'
        assert "t2_pass_gate: deferring related_nodes" in caplog.text
        assert "over-broad related_nodes" in caplog.text


class TestT2PassGateTestFailuresStillBlock:
    """AC6: test failures (failed > 0) still hard-block regardless of deferral logic."""

    def test_failed_tests_hard_block(self):
        """When test_report.failed > 0, gate blocks immediately (before any node check)."""
        from governance.auto_chain import _gate_t2_pass

        conn = _make_in_memory_db()
        _seed_nodes(conn, ["L1.1"], status="pending")

        metadata = _metadata_with_nodes(["L1.1"])
        result = _valid_test_result(test_report={"passed": 3, "failed": 2, "tool": "pytest"})

        # _try_verify_update should NOT even be called for failing tests
        with patch("governance.auto_chain._try_verify_update") as mock_vu:
            ok, reason = _gate_t2_pass(conn, PROJECT_ID, result, metadata)

        assert ok is False
        assert "Tests failed" in reason
        assert "2 failures" in reason
        # verify_update should never be reached
        mock_vu.assert_not_called()

    def test_missing_test_report_hard_blocks(self):
        """When test_report is missing or empty, gate blocks immediately."""
        from governance.auto_chain import _gate_t2_pass

        conn = _make_in_memory_db()
        metadata = _metadata_with_nodes(["L1.1"])
        result = {"test_report": {}}

        ok, reason = _gate_t2_pass(conn, PROJECT_ID, result, metadata)

        assert ok is False
        assert "missing required test_report" in reason


class TestT2PassGateNoRelatedNodes:
    """Edge case: no related_nodes → skip node checks entirely."""

    def test_no_related_nodes_passes(self, caplog):
        """When metadata has no related_nodes, gate passes without node check."""
        from governance.auto_chain import _gate_t2_pass

        conn = _make_in_memory_db()
        metadata = _metadata_with_nodes([])
        result = _valid_test_result()

        with patch("governance.auto_chain._try_verify_update", return_value=(True, "")):
            with caplog.at_level(logging.WARNING, logger="governance.auto_chain"):
                ok, reason = _gate_t2_pass(conn, PROJECT_ID, result, metadata)

        assert ok is True
        assert reason == "ok"
        assert "deferring" not in caplog.text
