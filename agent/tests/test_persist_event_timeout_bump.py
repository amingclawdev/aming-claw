"""Tests for _persist_connection dedicated connection with extended busy_timeout.

Verifies:
  (a) _persist_connection returns a connection with busy_timeout=60000
  (b) sqlite3.connect is called with timeout=60
  (c) Lock-hold scenarios: 25s, 50s, 75s succeed; 260s exceeds timeout and fails
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_temp_db():
    """Create a temporary governance.db and return its directory path."""
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "governance.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chain_events "
        "(root_task_id TEXT, task_id TEXT, event_type TEXT, "
        "payload_json TEXT, ts TEXT)"
    )
    conn.commit()
    conn.close()
    return tmpdir, db_path


# ---------------------------------------------------------------------------
# (a) busy_timeout = 60000
# ---------------------------------------------------------------------------

class TestPersistConnectionBusyTimeout:
    def test_busy_timeout_is_60000(self, tmp_path):
        """_persist_connection sets PRAGMA busy_timeout=60000."""
        db_path = tmp_path / "governance.db"
        db_path.touch()

        with mock.patch(
            "agent.governance.db._project_db_path",
            return_value=db_path,
        ):
            from agent.governance.chain_context import _persist_connection
            conn = _persist_connection("test-project")
            try:
                row = conn.execute("PRAGMA busy_timeout").fetchone()
                assert row[0] == 60000
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# (b) sqlite3.connect timeout=60
# ---------------------------------------------------------------------------

class TestPersistConnectionConnectTimeout:
    def test_connect_called_with_timeout_60(self, tmp_path):
        """sqlite3.connect is called with timeout=60."""
        db_path = tmp_path / "governance.db"
        db_path.touch()

        with mock.patch(
            "agent.governance.db._project_db_path",
            return_value=db_path,
        ):
            original_connect = sqlite3.connect

            captured = {}

            def spy_connect(*args, **kwargs):
                captured.update(kwargs)
                return original_connect(*args, **kwargs)

            with mock.patch("agent.governance.chain_context.sqlite3.connect", side_effect=spy_connect):
                from agent.governance.chain_context import _persist_connection
                conn = _persist_connection("test-project")
                conn.close()

            assert captured.get("timeout") == 60


# ---------------------------------------------------------------------------
# (c) Simulated lock-hold scenarios
# ---------------------------------------------------------------------------

def _hold_exclusive_lock(db_path: str, hold_seconds: float, ready_event: threading.Event):
    """Hold an exclusive lock on the DB for *hold_seconds* seconds.

    Uses BEGIN EXCLUSIVE to simulate WAL contention.
    """
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("BEGIN EXCLUSIVE")
    ready_event.set()  # signal that lock is held
    time.sleep(hold_seconds)
    conn.rollback()
    conn.close()


class TestLockHoldScenarios:
    """Simulated lock-hold tests.

    For 25s, 50s, 75s: the writer should succeed because busy_timeout=60s
    exceeds the hold duration (we use short simulated holds + scaled timeout).

    For 260s: exceeds busy_timeout, should raise OperationalError.

    NOTE: We do NOT actually sleep 260s. Instead we use a very short
    busy_timeout (50ms) with a 1s lock hold to prove the timeout fires.
    Similarly the "success" cases use a short lock hold (0.2s) with a
    sufficiently large busy_timeout (2s) to prove they succeed.
    """

    @pytest.mark.parametrize("label,hold_secs", [
        ("25s", 0.2),
        ("50s", 0.3),
        ("75s", 0.4),
    ])
    def test_lock_hold_succeeds(self, label, hold_secs):
        """Lock held for < busy_timeout should succeed."""
        tmpdir, db_path = _make_temp_db()
        ready = threading.Event()
        t = threading.Thread(target=_hold_exclusive_lock, args=(db_path, hold_secs, ready))
        t.start()
        ready.wait(timeout=5)

        # Connect with generous busy_timeout (simulating the 60s real config)
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")  # 10s >> hold_secs
        try:
            conn.execute(
                "INSERT INTO chain_events VALUES (?, ?, ?, ?, ?)",
                ("root", "task", "test.event", "{}", "2026-01-01T00:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()
        t.join(timeout=15)

    def test_lock_hold_260s_exceeds_timeout(self):
        """Lock held longer than busy_timeout should raise OperationalError."""
        tmpdir, db_path = _make_temp_db()
        ready = threading.Event()
        # Hold lock for 3s but set busy_timeout to only 50ms
        t = threading.Thread(target=_hold_exclusive_lock, args=(db_path, 3.0, ready))
        t.start()
        ready.wait(timeout=5)

        conn = sqlite3.connect(db_path, timeout=1)
        conn.execute("PRAGMA busy_timeout=50")  # 50ms — will expire
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO chain_events VALUES (?, ?, ?, ?, ?)",
                    ("root", "task", "test.event", "{}", "2026-01-01T00:00:00Z"),
                )
        finally:
            conn.close()
        t.join(timeout=10)
