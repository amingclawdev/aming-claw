"""Tests for Phase K autospawn: spawn_phase_k_discrepancies().

Covers AC-AS-1 through AC-AS-RATE-LIMIT acceptance criteria.
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import patch, MagicMock

import sys
import os

# Ensure agent package is importable
_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)


def _make_inmemory_db():
    """Create an in-memory SQLite DB with phase_k_processed_contracts table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE phase_k_processed_contracts (
            fingerprint       TEXT PRIMARY KEY,
            contract_kind     TEXT NOT NULL,
            contract_id       TEXT NOT NULL,
            discrepancy_type  TEXT NOT NULL,
            target_doc        TEXT NOT NULL DEFAULT '',
            target_test       TEXT NOT NULL DEFAULT '',
            spawned_task_id   TEXT NOT NULL DEFAULT '',
            spawn_status      TEXT NOT NULL DEFAULT 'pending',
            last_chain_event  TEXT NOT NULL DEFAULT '',
            updated_at        TEXT NOT NULL,
            processed_at      TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_phase_k_processed_status ON phase_k_processed_contracts(spawn_status)")
    conn.commit()
    return conn


def _make_ctx(conn, project_id="test-project"):
    """Create a minimal context object for spawn_phase_k_discrepancies."""
    ctx = MagicMock()
    ctx.project_id = project_id
    ctx.conn = conn
    ctx.api_base = "http://localhost:40000"
    return ctx


def _make_disc(dtype, contract_kind="EndpointContract", contract_id="GET /api/foo",
               doc="docs/api.md", expected_test_location="agent/tests/test_server.py"):
    """Create a PhaseKDiscrepancy."""
    from governance.reconcile_phases.phase_k import PhaseKDiscrepancy
    if dtype == "doc_value_drift":
        return PhaseKDiscrepancy(
            type="doc_value_drift",
            contract_kind=contract_kind,
            contract_id=contract_id,
            doc=doc,
            doc_line=10,
            doc_value=40100,
            code_value=40000,
            drift_role="service_port",
            confidence="high",
            priority="P1",
            suggested_action="spawn_pm_fix_doc",
            detail="Doc docs/api.md line 10: port 40100 != code 40000",
        )
    else:
        return PhaseKDiscrepancy(
            type="contract_no_test",
            contract_kind=contract_kind,
            contract_id=contract_id,
            expected_test_location=expected_test_location,
            confidence="high",
            priority="P0",
            suggested_action="spawn_pm_write_test",
            detail="No test coverage for EndpointContract: " + contract_id,
        )


class TestPhaseKAutospawnDryRun(unittest.TestCase):
    """AC-AS-2: dry_run=True returns spawned==0 and no HTTP calls."""

    def test_dry_run_returns_immediately(self):
        from governance.reconcile_phases.phase_k import spawn_phase_k_discrepancies
        conn = _make_inmemory_db()
        ctx = _make_ctx(conn)
        discs = [_make_disc("doc_value_drift")]

        with patch("governance.reconcile_phases.phase_k._spawn_pm_task_k") as mock_spawn:
            result = spawn_phase_k_discrepancies(ctx, None, discs, dry_run=True)

        self.assertEqual(result["spawned"], 0)
        self.assertTrue(result["dry_run"])
        mock_spawn.assert_not_called()

        # Verify no DB writes
        row = conn.execute("SELECT COUNT(*) AS cnt FROM phase_k_processed_contracts").fetchone()
        self.assertEqual(row["cnt"], 0)
        conn.close()


class TestPhaseKAutospawnDocValueDrift(unittest.TestCase):
    """AC-AS-3: 5 doc_value_drift for 1 doc -> 1 PM task, 5 fingerprints in DB."""

    @patch("governance.reconcile_phases.phase_k._spawn_pm_task_k")
    def test_5_drift_1_doc_1_task(self, mock_spawn):
        from governance.reconcile_phases.phase_k import spawn_phase_k_discrepancies
        mock_spawn.return_value = "pm-task-doc-1"
        conn = _make_inmemory_db()
        ctx = _make_ctx(conn)

        discs = []
        for i in range(5):
            d = _make_disc("doc_value_drift",
                           contract_kind="ServicePortContract",
                           contract_id="PORT_%d" % i,
                           doc="docs/api.md")
            discs.append(d)

        result = spawn_phase_k_discrepancies(ctx, None, discs, dry_run=False)

        # Exactly 1 PM task spawned
        self.assertEqual(result["spawned"], 1)
        self.assertEqual(len(result["spawned_doc_fix"]), 1)
        self.assertEqual(result["spawned_doc_fix"][0], "pm-task-doc-1")
        mock_spawn.assert_called_once()

        # All 5 fingerprints in DB with status='running'
        rows = conn.execute(
            "SELECT * FROM phase_k_processed_contracts WHERE spawn_status = 'running'"
        ).fetchall()
        self.assertEqual(len(rows), 5)
        conn.close()


class TestPhaseKAutospawnContractNoTest(unittest.TestCase):
    """AC-AS-4: 5 contract_no_test for 1 test -> 1 PM task, independent of doc fix."""

    @patch("governance.reconcile_phases.phase_k._spawn_pm_task_k")
    def test_5_no_test_1_location_1_task(self, mock_spawn):
        from governance.reconcile_phases.phase_k import spawn_phase_k_discrepancies
        mock_spawn.return_value = "pm-task-test-1"
        conn = _make_inmemory_db()
        ctx = _make_ctx(conn)

        discs = []
        for i in range(5):
            d = _make_disc("contract_no_test",
                           contract_id="handler_%d" % i,
                           expected_test_location="agent/tests/test_server.py")
            discs.append(d)

        result = spawn_phase_k_discrepancies(ctx, None, discs, dry_run=False)

        self.assertEqual(result["spawned"], 1)
        self.assertEqual(len(result["spawned_test_write"]), 1)
        self.assertEqual(result["spawned_test_write"][0], "pm-task-test-1")
        mock_spawn.assert_called_once()

        rows = conn.execute(
            "SELECT * FROM phase_k_processed_contracts WHERE spawn_status = 'running'"
        ).fetchall()
        self.assertEqual(len(rows), 5)
        conn.close()


class TestPhaseKAutospawnDedup(unittest.TestCase):
    """AC-AS-5: Re-run on same fingerprints -> 0 new spawns."""

    @patch("governance.reconcile_phases.phase_k._spawn_pm_task_k")
    def test_dedup_no_new_spawns(self, mock_spawn):
        from governance.reconcile_phases.phase_k import spawn_phase_k_discrepancies
        mock_spawn.return_value = "pm-task-1"
        conn = _make_inmemory_db()
        ctx = _make_ctx(conn)

        discs = [_make_disc("doc_value_drift", contract_id="PORT_1", doc="docs/api.md")]

        # First run
        r1 = spawn_phase_k_discrepancies(ctx, None, discs, dry_run=False)
        self.assertEqual(r1["spawned"], 1)
        self.assertEqual(mock_spawn.call_count, 1)

        # Second run — same discrepancies
        mock_spawn.reset_mock()
        r2 = spawn_phase_k_discrepancies(ctx, None, discs, dry_run=False)
        self.assertEqual(r2["spawned"], 0)
        mock_spawn.assert_not_called()
        conn.close()


class TestPhaseKAutospawnParallel(unittest.TestCase):
    """AC-AS-PARALLEL: Both discrepancy types processed in single call."""

    @patch("governance.reconcile_phases.phase_k._spawn_pm_task_k")
    def test_both_types_in_single_call(self, mock_spawn):
        from governance.reconcile_phases.phase_k import spawn_phase_k_discrepancies
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            return "pm-task-%d" % call_count[0]

        mock_spawn.side_effect = side_effect
        conn = _make_inmemory_db()
        ctx = _make_ctx(conn)

        discs = [
            _make_disc("doc_value_drift", contract_id="PORT_A", doc="docs/api.md"),
            _make_disc("contract_no_test", contract_id="handler_A",
                       expected_test_location="agent/tests/test_server.py"),
        ]

        result = spawn_phase_k_discrepancies(ctx, None, discs, dry_run=False)

        # Both types should be present
        self.assertIn("spawned_doc_fix", result)
        self.assertIn("spawned_test_write", result)
        self.assertEqual(len(result["spawned_doc_fix"]), 1)
        self.assertEqual(len(result["spawned_test_write"]), 1)
        self.assertEqual(result["spawned"], 2)
        self.assertEqual(mock_spawn.call_count, 2)
        conn.close()


class TestPhaseKAutospawnRateLimit(unittest.TestCase):
    """AC-AS-RATE-LIMIT: max_spawn_per_run=1 with 3 target docs -> 1 spawned, rest throttled."""

    @patch("governance.reconcile_phases.phase_k._spawn_pm_task_k")
    def test_rate_limit(self, mock_spawn):
        from governance.reconcile_phases.phase_k import spawn_phase_k_discrepancies
        mock_spawn.return_value = "pm-task-limited"
        conn = _make_inmemory_db()
        ctx = _make_ctx(conn)

        discs = [
            _make_disc("doc_value_drift", contract_id="PORT_1", doc="docs/api.md"),
            _make_disc("doc_value_drift", contract_id="PORT_2", doc="docs/setup.md"),
            _make_disc("doc_value_drift", contract_id="PORT_3", doc="docs/deploy.md"),
        ]

        result = spawn_phase_k_discrepancies(
            ctx, None, discs, dry_run=False, max_spawn_per_run=1,
        )

        # Only 1 spawned
        self.assertEqual(result["spawned"], 1)
        self.assertEqual(len(result["spawned_doc_fix"]), 1)
        mock_spawn.assert_called_once()

        # Rest should be throttled
        self.assertGreater(len(result["skipped_throttled"]), 0)

        # Check DB: skipped_throttled entries
        throttled = conn.execute(
            "SELECT COUNT(*) AS cnt FROM phase_k_processed_contracts WHERE spawn_status = 'skipped_throttled'"
        ).fetchall()
        self.assertGreater(throttled[0]["cnt"], 0)
        conn.close()


class TestPhaseKMigrationV21(unittest.TestCase):
    """AC-AS-1: phase_k_processed_contracts table created via migration v20->v21."""

    def test_migration_creates_table(self):
        """Verify the migration function creates the table correctly."""
        from governance.db import _run_migrations
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # Create schema_meta table for migration tracking
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        # Create the prerequisite phase_h table (migration 20 dependency)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS phase_h_processed_symbols (
                fingerprint TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                symbol_kind TEXT NOT NULL,
                symbol_qname TEXT NOT NULL,
                expected_doc TEXT NOT NULL,
                spawned_task_id TEXT NOT NULL DEFAULT '',
                spawn_status TEXT NOT NULL DEFAULT 'pending',
                last_chain_event TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                processed_at TEXT NOT NULL
            )
        """)
        # Run migration v20 -> v21
        _run_migrations(conn, 20, 21)
        conn.commit()

        # Verify table exists
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='phase_k_processed_contracts'"
        ).fetchone()
        self.assertIsNotNone(row)

        # Verify index exists
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_phase_k_processed_status'"
        ).fetchone()
        self.assertIsNotNone(idx)

        # Verify columns
        cols = conn.execute("PRAGMA table_info(phase_k_processed_contracts)").fetchall()
        col_names = [c["name"] for c in cols]
        for expected in ["fingerprint", "contract_kind", "contract_id", "discrepancy_type",
                         "target_doc", "target_test", "spawned_task_id", "spawn_status",
                         "last_chain_event", "updated_at", "processed_at"]:
            self.assertIn(expected, col_names)
        conn.close()


class TestPhaseKFingerprint(unittest.TestCase):
    """R5: Fingerprint computed as sha256(project_id, contract_kind, contract_id, discrepancy_type, target)."""

    def test_fingerprint_deterministic(self):
        from governance.reconcile_phases.phase_k import _compute_contract_fingerprint
        fp1 = _compute_contract_fingerprint("proj", "EndpointContract", "GET /foo", "doc_value_drift", "docs/api.md")
        fp2 = _compute_contract_fingerprint("proj", "EndpointContract", "GET /foo", "doc_value_drift", "docs/api.md")
        self.assertEqual(fp1, fp2)

    def test_fingerprint_different_for_different_target(self):
        from governance.reconcile_phases.phase_k import _compute_contract_fingerprint
        fp1 = _compute_contract_fingerprint("proj", "EndpointContract", "GET /foo", "doc_value_drift", "docs/api.md")
        fp2 = _compute_contract_fingerprint("proj", "EndpointContract", "GET /foo", "doc_value_drift", "docs/setup.md")
        self.assertNotEqual(fp1, fp2)

    def test_fingerprint_is_sha256_hex(self):
        from governance.reconcile_phases.phase_k import _compute_contract_fingerprint
        fp = _compute_contract_fingerprint("proj", "EP", "id1", "doc_value_drift", "docs/a.md")
        self.assertEqual(len(fp), 64)  # sha256 hex length
        # Should be valid hex
        int(fp, 16)


class TestSpawnCallsBacklogFirst(unittest.TestCase):
    """AC5: _spawn_pm_task_k POSTs to /api/backlog/ before /api/task/."""

    def test_spawn_calls_backlog_first(self):
        from governance.reconcile_phases.phase_k import _spawn_pm_task_k, PhaseKDiscrepancy

        recorded_urls = []

        class FakeResponse:
            def read(self):
                return json.dumps({"task_id": "pm-task-99"}).encode("utf-8")

        def fake_urlopen(req, **kwargs):
            recorded_urls.append(req.full_url)
            return FakeResponse()

        discs = [PhaseKDiscrepancy(
            type="doc_value_drift",
            contract_kind="ServicePortContract",
            contract_id="PORT_1",
            doc="docs/api.md",
            detail="drift detail",
        )]

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = _spawn_pm_task_k(
                project_id="test-proj",
                discrepancy_type="doc_value_drift",
                target="docs/api.md",
                fingerprints=["fp1"],
                discrepancies=discs,
                scope_origin="test",
                bug_id="OPT-BACKLOG-PHASE-K-DOC-VALUE-DRIFT-docs-api-md",
                api_base="http://localhost:40000",
            )

        # Two POSTs should have been made
        self.assertEqual(len(recorded_urls), 2,
                         "Expected 2 HTTP calls, got %d: %s" % (len(recorded_urls), recorded_urls))

        # First URL must be backlog upsert
        self.assertIn("/api/backlog/", recorded_urls[0],
                       "First call should be to /api/backlog/, got: %s" % recorded_urls[0])

        # Second URL must be task create
        self.assertIn("/api/task/", recorded_urls[1],
                       "Second call should be to /api/task/, got: %s" % recorded_urls[1])

        self.assertEqual(result, "pm-task-99")


class TestReconcileTaskPhaseKIntegration(unittest.TestCase):
    """R3: handle_apply calls spawn_phase_k_discrepancies when Phase K outputs present."""

    @patch("governance.reconcile_task._trigger_baseline_write")
    @patch("governance.reconcile_task._write_mutation_plan")
    @patch("governance.reconcile_task._read_mutation_plan")
    @patch("governance.reconcile_task._begin_two_phase")
    @patch("governance.reconcile_task._commit_two_phase")
    @patch("governance.reconcile_task._ensure_mutation_wal_table")
    @patch("governance.reconcile_task._check_cancellation")
    def test_handle_apply_calls_phase_k_autospawn(
        self, mock_cancel, mock_wal, mock_commit, mock_begin, mock_read_plan,
        mock_write_plan, mock_baseline
    ):
        mock_begin.return_value = "txn-123"
        mock_read_plan.return_value = {
            "task_id": "task-1",
            "baseline_id_before": "",
            "mutations": [],
            "phases_run": ["scan", "diff", "propose", "approve"],
            "summary": "test",
            "approve_threshold": "high",
            "applied_count": 0,
            "scope_declared": {},
        }

        from governance.reconcile_phases.phase_k import PhaseKDiscrepancy
        discs = [PhaseKDiscrepancy(
            type="doc_value_drift",
            contract_kind="ServicePortContract",
            contract_id="PORT_1",
            doc="docs/api.md",
            detail="test drift",
        )]

        conn = MagicMock()
        conn.execute.return_value = MagicMock(fetchone=MagicMock(return_value=None))
        metadata = {
            "phase_k_discrepancies": discs,
            "phase_k_dry_run": True,  # Use dry_run to avoid needing full DB
        }

        from governance.reconcile_task import handle_apply
        result = handle_apply(conn, "test-proj", "task-1", metadata, {})

        self.assertIn("phase_k_autospawn", result)
        self.assertIsNotNone(result["phase_k_autospawn"])
        self.assertTrue(result["phase_k_autospawn"]["dry_run"])


if __name__ == "__main__":
    unittest.main()
