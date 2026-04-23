"""Tests for _persist_event db-lock retry logic in ChainContextStore.

AC7: contention scenario — second thread INSERT succeeds after retry
AC8: exhausted retries — _persist_event logs error but does not raise
AC9: happy path — single-thread call succeeds on first attempt
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from unittest import mock

import pytest

from agent.governance.chain_context import ChainContextStore


@pytest.fixture()
def store():
    """Fresh ChainContextStore for each test."""
    return ChainContextStore()


def _make_db():
    """Create an in-memory SQLite DB with chain_events table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE chain_events ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  root_task_id TEXT NOT NULL,"
        "  task_id TEXT NOT NULL,"
        "  event_type TEXT NOT NULL,"
        "  payload_json TEXT,"
        "  ts TEXT"
        ")"
    )
    conn.commit()
    return conn


class _ConnWrapper:
    """Wrapper around sqlite3.Connection that allows overriding execute."""

    def __init__(self, conn, execute_hook=None):
        self._conn = conn
        self._execute_hook = execute_hook

    def execute(self, *args, **kwargs):
        if self._execute_hook:
            return self._execute_hook(self._conn, *args, **kwargs)
        return self._conn.execute(*args, **kwargs)

    def commit(self):
        return self._conn.commit()

    def close(self):
        # Don't actually close the underlying conn so tests can inspect it
        pass

    def real_close(self):
        self._conn.close()


class TestHappyPath:
    """AC9: single-thread call succeeds on first attempt."""

    def test_persist_event_inserts_row(self, store):
        """_persist_event writes a row to chain_events on first try."""
        db = _make_db()
        wrapper = _ConnWrapper(db)

        with mock.patch(
            "agent.governance.db.get_connection",
            return_value=wrapper,
        ):
            store._persist_event(
                root_task_id="root-1",
                task_id="task-1",
                event_type="task.created",
                payload={"task_id": "task-1", "type": "pm"},
                project_id="test-proj",
            )

        rows = db.execute("SELECT * FROM chain_events").fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["root_task_id"] == "root-1"
        assert row["task_id"] == "task-1"
        assert row["event_type"] == "task.created"
        payload = json.loads(row["payload_json"])
        assert payload["task_id"] == "task-1"
        db.close()

    def test_persist_event_does_not_raise(self, store):
        """_persist_event is best-effort — never raises."""
        db = _make_db()
        wrapper = _ConnWrapper(db)

        with mock.patch(
            "agent.governance.db.get_connection",
            return_value=wrapper,
        ):
            # Should not raise
            store._persist_event(
                root_task_id="root-2",
                task_id="task-2",
                event_type="task.completed",
                payload={"task_id": "task-2"},
                project_id="test-proj",
            )
        db.close()


class TestContentionRetry:
    """AC7: second thread INSERT succeeds after retry."""

    def test_retry_succeeds_after_lock(self, store):
        """Simulates DB lock on first attempt, success on second."""
        call_count = 0
        connections_created = []

        def mock_get_connection(pid):
            nonlocal call_count
            call_count += 1
            db = _make_db()
            connections_created.append(db)

            if call_count == 1:
                def locked_execute(conn, *args, **kwargs):
                    if args and isinstance(args[0], str) and args[0].strip().startswith("INSERT"):
                        raise sqlite3.OperationalError("database is locked")
                    return conn.execute(*args, **kwargs)
                return _ConnWrapper(db, execute_hook=locked_execute)
            else:
                return _ConnWrapper(db)

        with mock.patch(
            "agent.governance.db.get_connection",
            side_effect=mock_get_connection,
        ), mock.patch(
            "agent.governance.task_registry._DB_LOCK_BASE_DELAY", 0.001,
        ):
            store._persist_event(
                root_task_id="root-3",
                task_id="task-3",
                event_type="task.created",
                payload={"task_id": "task-3"},
                project_id="test-proj",
            )

        # Should have retried: 2 connections created
        assert len(connections_created) >= 2
        # Second DB should have the row
        rows = connections_created[1].execute("SELECT * FROM chain_events").fetchall()
        assert len(rows) == 1
        assert rows[0]["task_id"] == "task-3"
        for c in connections_created:
            c.close()

    def test_fresh_connection_per_attempt(self, store):
        """Each retry opens a fresh connection (R2)."""
        connections = []
        call_count = 0

        def mock_get_connection(pid):
            nonlocal call_count
            call_count += 1
            db = _make_db()
            connections.append(db)

            if call_count == 1:
                def locked_execute(conn, *args, **kwargs):
                    if args and isinstance(args[0], str) and args[0].strip().startswith("INSERT"):
                        raise sqlite3.OperationalError("database is locked")
                    return conn.execute(*args, **kwargs)
                return _ConnWrapper(db, execute_hook=locked_execute)
            else:
                return _ConnWrapper(db)

        with mock.patch(
            "agent.governance.db.get_connection",
            side_effect=mock_get_connection,
        ), mock.patch(
            "agent.governance.task_registry._DB_LOCK_BASE_DELAY", 0.001,
        ):
            store._persist_event(
                root_task_id="root-4",
                task_id="task-4",
                event_type="task.created",
                payload={"task_id": "task-4"},
                project_id="test-proj",
            )

        # Should have opened at least 2 connections (first failed, second succeeded)
        assert len(connections) >= 2
        for c in connections:
            c.close()


class TestExhaustedRetries:
    """AC8: exhausted retries — _persist_event logs error but does not raise."""

    def test_does_not_raise_on_exhausted_retries(self, store, caplog):
        """When all retries are exhausted, _persist_event swallows the error."""

        def mock_get_connection(pid):
            db = _make_db()

            def always_locked(conn, *args, **kwargs):
                if args and isinstance(args[0], str) and args[0].strip().startswith("INSERT"):
                    raise sqlite3.OperationalError("database is locked")
                return conn.execute(*args, **kwargs)

            return _ConnWrapper(db, execute_hook=always_locked)

        with mock.patch(
            "agent.governance.db.get_connection",
            side_effect=mock_get_connection,
        ), mock.patch(
            "agent.governance.task_registry._DB_LOCK_BASE_DELAY", 0.001,
        ), caplog.at_level(logging.ERROR, logger="agent.governance.chain_context"):
            # Must NOT raise
            store._persist_event(
                root_task_id="root-5",
                task_id="task-5",
                event_type="task.created",
                payload={"task_id": "task-5"},
                project_id="test-proj",
            )

        # Should have logged an error
        assert any("persist event failed" in r.message for r in caplog.records)

    def test_error_logged_with_exc_info(self, store, caplog):
        """Error log includes exc_info=True for debugging."""

        def mock_get_connection(pid):
            db = _make_db()

            def always_locked(conn, *args, **kwargs):
                if args and isinstance(args[0], str) and args[0].strip().startswith("INSERT"):
                    raise sqlite3.OperationalError("database is locked")
                return conn.execute(*args, **kwargs)

            return _ConnWrapper(db, execute_hook=always_locked)

        with mock.patch(
            "agent.governance.db.get_connection",
            side_effect=mock_get_connection,
        ), mock.patch(
            "agent.governance.task_registry._DB_LOCK_BASE_DELAY", 0.001,
        ), caplog.at_level(logging.ERROR, logger="agent.governance.chain_context"):
            store._persist_event(
                root_task_id="root-6",
                task_id="task-6",
                event_type="task.failed",
                payload={"task_id": "task-6"},
                project_id="test-proj",
            )

        error_records = [r for r in caplog.records if "persist event failed" in r.message]
        assert len(error_records) >= 1
        # exc_info=True means the record has exception info
        assert error_records[0].exc_info is not None


class TestRecoveringGuard:
    """AC5: _recovering early-return guard is preserved."""

    def test_persist_event_skips_during_recovery(self, store):
        """When _recovering is True, _persist_event returns immediately."""
        store._recovering = True

        with mock.patch(
            "agent.governance.db.get_connection",
        ) as mock_conn:
            store._persist_event(
                root_task_id="root-7",
                task_id="task-7",
                event_type="task.created",
                payload={"task_id": "task-7"},
                project_id="test-proj",
            )

        # get_connection should never be called during recovery
        mock_conn.assert_not_called()


class TestConnectionCleanup:
    """AC2/R4: conn.close() is in a finally block."""

    def test_connection_closed_on_success(self, store):
        """Connection is closed after successful insert."""
        mock_conn = mock.MagicMock()
        mock_conn.execute = mock.MagicMock()
        mock_conn.commit = mock.MagicMock()

        with mock.patch(
            "agent.governance.db.get_connection",
            return_value=mock_conn,
        ):
            store._persist_event(
                root_task_id="root-8",
                task_id="task-8",
                event_type="task.created",
                payload={"task_id": "task-8"},
                project_id="test-proj",
            )

        mock_conn.close.assert_called_once()

    def test_connection_closed_on_non_lock_error(self, store):
        """Connection is closed even when a non-lock error occurs."""
        mock_conn = mock.MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("disk I/O error")

        with mock.patch(
            "agent.governance.db.get_connection",
            return_value=mock_conn,
        ):
            # Should not raise (best-effort)
            store._persist_event(
                root_task_id="root-9",
                task_id="task-9",
                event_type="task.created",
                payload={"task_id": "task-9"},
                project_id="test-proj",
            )

        mock_conn.close.assert_called_once()
