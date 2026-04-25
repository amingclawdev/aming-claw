"""Tests for Phase I baseline storage service.

Covers AC-I1 through AC-I9, AC-I11, AC-I12.
"""
import gc
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _safe_cleanup(tmp_dir):
    """Best-effort cleanup for Windows SQLite WAL file locks."""
    try:
        gc.collect()
        tmp_dir.cleanup()
    except (PermissionError, OSError):
        try:
            shutil.rmtree(tmp_dir.name, ignore_errors=True)
        except Exception:
            pass


class BaselineTestBase(unittest.TestCase):
    """Shared setup: temp dir, SHARED_VOLUME_PATH, and connection helper."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self.pid = "test-project"
        os.makedirs(os.path.join(
            self.tmp.name, "codex-tasks", "state", "governance", self.pid
        ), exist_ok=True)
        self._conns = []

    def tearDown(self):
        for c in self._conns:
            try:
                c.close()
            except Exception:
                pass
        os.environ.pop("SHARED_VOLUME_PATH", None)
        _safe_cleanup(self.tmp)

    def _get_conn(self, pid=None):
        from governance.db import get_connection
        conn = get_connection(pid or self.pid)
        self._conns.append(conn)
        return conn

    def _seed_project_version(self, conn, chain_version="abc1234"):
        """Insert a project_version row for testing."""
        conn.execute(
            """INSERT OR REPLACE INTO project_version
               (project_id, chain_version, updated_at, updated_by)
               VALUES (?, ?, '2026-01-01T00:00:00Z', 'test')""",
            (self.pid, chain_version),
        )
        conn.commit()


class TestSchemaV19(BaselineTestBase):
    """AC-I12: version_baselines table exists with correct columns."""

    def test_schema_version_is_19(self):
        from governance.db import SCHEMA_VERSION
        self.assertEqual(SCHEMA_VERSION, 19)

    def test_version_baselines_table_exists(self):
        conn = self._get_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t["name"] for t in tables}
        self.assertIn("version_baselines", table_names)

    def test_reconstructed_column_integer_not_null_default_0(self):
        conn = self._get_conn()
        cols = conn.execute("PRAGMA table_info(version_baselines)").fetchall()
        col_map = {c["name"]: c for c in cols}
        self.assertIn("reconstructed", col_map)
        self.assertEqual(col_map["reconstructed"]["type"], "INTEGER")
        self.assertEqual(col_map["reconstructed"]["notnull"], 1)
        self.assertEqual(col_map["reconstructed"]["dflt_value"], "0")

    def test_indexes_exist(self):
        conn = self._get_conn()
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        idx_names = {i["name"] for i in indexes}
        self.assertIn("idx_baselines_chain_version", idx_names)
        self.assertIn("idx_baselines_created_at", idx_names)


class TestCreateBaseline(BaselineTestBase):
    """AC-I1, AC-I3, AC-I7 (trigger), AC-I8 (allowlist)."""

    def test_ac_i1_create_init_baseline(self):
        """AC-I1: create B1 with trigger='init'."""
        conn = self._get_conn()
        self._seed_project_version(conn)
        from governance.baseline_service import create_baseline
        bl = create_baseline(
            conn, self.pid,
            chain_version="abc1234",
            trigger="init",
            triggered_by="init",
        )
        self.assertEqual(bl["baseline_id"], 1)
        self.assertEqual(bl["trigger"], "init")
        self.assertEqual(bl["triggered_by"], "init")

    def test_ac_i3_reconcile_task_trigger(self):
        """AC-I3: create with trigger='reconcile-task'."""
        conn = self._get_conn()
        from governance.baseline_service import create_baseline
        bl = create_baseline(
            conn, self.pid,
            chain_version="def5678",
            trigger="reconcile-task",
            triggered_by="reconcile-task",
        )
        self.assertEqual(bl["triggered_by"], "reconcile-task")

    def test_ac_i8_invalid_trigger_rejected(self):
        """AC-I8: triggered_by not in allowlist raises ValueError."""
        conn = self._get_conn()
        from governance.baseline_service import create_baseline
        with self.assertRaises(ValueError):
            create_baseline(
                conn, self.pid,
                chain_version="xyz",
                trigger="init",
                triggered_by="invalid-actor",
            )

    def test_sequential_baseline_ids(self):
        """Baselines get sequential IDs per project."""
        conn = self._get_conn()
        from governance.baseline_service import create_baseline
        b1 = create_baseline(conn, self.pid, "v1", "init", "init")
        b2 = create_baseline(conn, self.pid, "v2", "auto-chain", "auto-chain")
        self.assertEqual(b1["baseline_id"], 1)
        self.assertEqual(b2["baseline_id"], 2)

    def test_reconstructed_field_in_response(self):
        """AC-I12: created baseline includes reconstructed field."""
        conn = self._get_conn()
        from governance.baseline_service import create_baseline
        bl = create_baseline(conn, self.pid, "v1", "init", "init", reconstructed=1)
        self.assertEqual(bl["reconstructed"], 1)


class TestGetBaseline(BaselineTestBase):
    """get_baseline, list_baselines, get_by_commit."""

    def test_get_baseline(self):
        conn = self._get_conn()
        from governance.baseline_service import create_baseline, get_baseline
        create_baseline(conn, self.pid, "v1", "init", "init")
        bl = get_baseline(conn, self.pid, 1)
        self.assertEqual(bl["baseline_id"], 1)

    def test_get_baseline_missing(self):
        conn = self._get_conn()
        from governance.baseline_service import get_baseline
        from governance.errors import BaselineMissingError
        with self.assertRaises(BaselineMissingError):
            get_baseline(conn, self.pid, 999)

    def test_list_baselines(self):
        conn = self._get_conn()
        from governance.baseline_service import create_baseline, list_baselines
        create_baseline(conn, self.pid, "v1", "init", "init")
        create_baseline(conn, self.pid, "v2", "auto-chain", "auto-chain")
        result = list_baselines(conn, self.pid)
        self.assertEqual(len(result), 2)
        # DESC order
        self.assertEqual(result[0]["baseline_id"], 2)

    def test_ac_i4_get_by_commit(self):
        """AC-I4: GET by commit SHA returns metadata."""
        conn = self._get_conn()
        from governance.baseline_service import create_baseline, get_by_commit
        create_baseline(conn, self.pid, "abc1234", "init", "init")
        bl = get_by_commit(conn, self.pid, "abc1234")
        self.assertEqual(bl["chain_version"], "abc1234")
        self.assertIn("reconstructed", bl)

    def test_ac_i4_explain_query_plan_uses_index(self):
        """AC-I4: EXPLAIN QUERY PLAN uses idx_baselines_chain_version."""
        conn = self._get_conn()
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM version_baselines WHERE project_id = ? AND chain_version = ?",
            (self.pid, "abc"),
        ).fetchall()
        plan_text = " ".join(str(dict(r)) for r in plan)
        self.assertIn("idx_baselines_chain_version", plan_text)


class TestDiff(BaselineTestBase):
    """AC-I5: diff returns structured delta."""

    def test_ac_i5_diff_returns_expected_keys(self):
        conn = self._get_conn()
        from governance.baseline_service import create_baseline, diff
        create_baseline(
            conn, self.pid, "v1", "init", "init",
            graph_json={"nodes": {"A": {}, "B": {}}},
            node_state_snap=json.dumps({"A": "pending", "B": "pending"}),
            chain_event_max=5,
        )
        create_baseline(
            conn, self.pid, "v2", "auto-chain", "auto-chain",
            graph_json={"nodes": {"B": {}, "C": {}}},
            node_state_snap=json.dumps({"B": "qa_pass", "C": "pending"}),
            chain_event_max=12,
        )
        delta = diff(conn, self.pid, 1, 2)
        self.assertIn("nodes_added", delta)
        self.assertIn("nodes_removed", delta)
        self.assertIn("node_state_changes", delta)
        self.assertIn("chain_events_count", delta)
        self.assertIn("C", delta["nodes_added"])
        self.assertIn("A", delta["nodes_removed"])
        self.assertEqual(delta["chain_events_count"], 7)


class TestCompanionFiles(BaselineTestBase):
    """AC-I6: sha256 verification on companion files."""

    def test_ac_i6_sha256_mismatch_raises_corrupted(self):
        conn = self._get_conn()
        from governance.baseline_service import create_baseline, read_companion_file
        from governance.errors import BaselineCorruptedError
        create_baseline(
            conn, self.pid, "v1", "init", "init",
            graph_json={"nodes": {"A": {}}},
        )
        # Tamper the file
        from governance.baseline_service import _baselines_root
        graph_path = _baselines_root(self.pid) / "1" / "graph.json"
        graph_path.write_text('{"tampered": true}')
        with self.assertRaises(BaselineCorruptedError):
            read_companion_file(self.pid, 1, "graph.json")

    def test_read_companion_file_happy_path(self):
        conn = self._get_conn()
        from governance.baseline_service import create_baseline, read_companion_file
        create_baseline(
            conn, self.pid, "v1", "init", "init",
            graph_json={"nodes": {"X": {}}},
        )
        data = read_companion_file(self.pid, 1, "graph.json")
        self.assertEqual(data["nodes"]["X"], {})


class TestBackfillReconstructed(BaselineTestBase):
    """AC-I9: backfill_reconstructed creates rows with reconstructed=1."""

    def test_ac_i9_backfill_creates_reconstructed_rows(self):
        conn = self._get_conn()
        self._seed_project_version(conn, "abc1234")
        from governance.baseline_service import backfill_reconstructed
        results = backfill_reconstructed(conn, self.pid)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["reconstructed"], 1)

    def test_ac_i9_backfill_noop_when_baselines_exist(self):
        conn = self._get_conn()
        self._seed_project_version(conn, "abc1234")
        from governance.baseline_service import create_baseline, backfill_reconstructed
        create_baseline(conn, self.pid, "abc1234", "init", "init")
        results = backfill_reconstructed(conn, self.pid)
        self.assertEqual(results, [])

    def test_ac_i9_api_get_exposes_reconstructed(self):
        """Verify that baselines from backfill have reconstructed=1 in DB."""
        conn = self._get_conn()
        self._seed_project_version(conn, "abc1234")
        from governance.baseline_service import backfill_reconstructed, get_baseline
        backfill_reconstructed(conn, self.pid)
        bl = get_baseline(conn, self.pid, 1)
        self.assertEqual(bl["reconstructed"], 1)


class TestRequireBaseline(BaselineTestBase):
    """AC-I11: require_baseline guard."""

    def test_ac_i11_require_baseline_raises_when_missing(self):
        conn = self._get_conn()
        from governance.baseline_service import require_baseline
        from governance.errors import BaselineMissingError
        with self.assertRaises(BaselineMissingError):
            require_baseline(conn, self.pid, baseline_id=42)

    def test_ac_i11_require_baseline_files_backlog(self):
        """When baseline is missing, a backlog bug row is filed."""
        conn = self._get_conn()
        from governance.baseline_service import require_baseline
        from governance.errors import BaselineMissingError
        try:
            require_baseline(conn, self.pid, baseline_id=42)
        except BaselineMissingError:
            pass
        row = conn.execute(
            "SELECT * FROM backlog_bugs WHERE bug_id = 'OPT-BACKLOG-BASELINE-MISSING-B42'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["priority"], "P1")
        self.assertEqual(row["status"], "OPEN")

    def test_require_baseline_succeeds_when_exists(self):
        conn = self._get_conn()
        from governance.baseline_service import create_baseline, require_baseline
        create_baseline(conn, self.pid, "v1", "init", "init")
        result = require_baseline(conn, self.pid, baseline_id=1)
        self.assertEqual(result["baseline_id"], 1)


class TestTriggerAllowlist(BaselineTestBase):
    """AC-I8: trigger allowlist enforcement."""

    def test_valid_triggers_accepted(self):
        conn = self._get_conn()
        from governance.baseline_service import create_baseline
        for trigger in ["auto-chain", "reconcile-task", "manual-fix", "init"]:
            bl = create_baseline(conn, self.pid, f"v-{trigger}", trigger, trigger)
            self.assertEqual(bl["triggered_by"], trigger)

    def test_invalid_trigger_rejected(self):
        conn = self._get_conn()
        from governance.baseline_service import create_baseline
        with self.assertRaises(ValueError):
            create_baseline(conn, self.pid, "v1", "init", "hacker")


class TestBaselineErrors(unittest.TestCase):
    """R5: BaselineMissingError and BaselineCorruptedError."""

    def test_baseline_missing_error_attrs(self):
        from governance.errors import BaselineMissingError, GovernanceError
        err = BaselineMissingError("proj", 5)
        self.assertIsInstance(err, GovernanceError)
        self.assertEqual(err.status, 404)
        self.assertIn("proj", err.message)

    def test_baseline_corrupted_error_attrs(self):
        from governance.errors import BaselineCorruptedError, GovernanceError
        err = BaselineCorruptedError("proj", 3, "bad sha")
        self.assertIsInstance(err, GovernanceError)
        self.assertEqual(err.status, 500)
        self.assertIn("bad sha", err.message)


if __name__ == "__main__":
    unittest.main()
