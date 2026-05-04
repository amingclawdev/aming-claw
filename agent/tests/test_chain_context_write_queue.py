import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance import chain_context


def _init_chain_events_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """CREATE TABLE chain_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               root_task_id TEXT,
               task_id TEXT,
               event_type TEXT,
               payload_json TEXT,
               ts TEXT
            )"""
        )
        conn.commit()
    finally:
        conn.close()


class TestChainContextWriteQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "governance.db"
        _init_chain_events_db(self.db_path)
        chain_context._start_chain_event_write_queue_for_tests()

    def tearDown(self):
        chain_context._drain_chain_event_write_queue_for_tests(timeout=5)
        chain_context._stop_chain_event_write_queue_for_tests(timeout=5)

    def _persist_connection(self, timeout=2.0):
        conn = sqlite3.connect(str(self.db_path), timeout=timeout)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=2000")
        return conn

    def _rows(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM chain_events ORDER BY id")]
        finally:
            conn.close()

    def test_conn_none_returns_quickly_while_sqlite_writer_is_locked(self):
        lock_conn = sqlite3.connect(str(self.db_path), timeout=0.1)
        lock_conn.execute("BEGIN IMMEDIATE")
        try:
            with mock.patch.object(
                chain_context,
                "_persist_connection",
                lambda _project_id: self._persist_connection(),
            ):
                started = time.perf_counter()
                chain_context.get_store()._persist_event(
                    "root-lock",
                    "task-lock",
                    "task.completed",
                    {"project_id": "proj", "task_id": "task-lock"},
                    "proj",
                    conn=None,
                )
                elapsed = time.perf_counter() - started
                self.assertLess(elapsed, 0.25)
                self.assertEqual(self._rows(), [])
        finally:
            lock_conn.rollback()
            lock_conn.close()

        self.assertTrue(chain_context._drain_chain_event_write_queue_for_tests(timeout=5))
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["root_task_id"], "root-lock")
        self.assertEqual(rows[0]["event_type"], "task.completed")

    def test_caller_conn_path_stays_synchronous_and_transaction_owned(self):
        conn = self._persist_connection()
        try:
            chain_context.get_store()._persist_event(
                "root-sync",
                "task-sync",
                "pm.prd.published",
                {"project_id": "proj", "task_id": "task-sync"},
                "proj",
                conn=conn,
            )
            same_conn_count = conn.execute("SELECT COUNT(*) FROM chain_events").fetchone()[0]
            self.assertEqual(same_conn_count, 1)
            conn.commit()
        finally:
            conn.close()

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "pm.prd.published")

    def test_failed_queued_write_spools_jsonl(self):
        spool_path = Path(self.tmp.name) / "chain_events_spool.jsonl"

        def raise_locked(_project_id):
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(chain_context, "_persist_connection", raise_locked), \
             mock.patch.object(chain_context, "_chain_event_spool_path", return_value=spool_path):
            chain_context.get_store()._persist_event(
                "root-spool",
                "task-spool",
                "task.created",
                {"project_id": "proj", "task_id": "task-spool"},
                "proj",
                conn=None,
            )
            self.assertTrue(chain_context._drain_chain_event_write_queue_for_tests(timeout=5))

        lines = spool_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["root_task_id"], "root-spool")
        self.assertEqual(record["event_type"], "task.created")
        self.assertIn("database is locked", record["error"])


if __name__ == "__main__":
    unittest.main()
