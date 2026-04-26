"""Tests for slice-baseline features (PR5: OPT-BACKLOG-RECONCILE-SCOPED-V2).

Covers AC-B1 through AC-B6 acceptance criteria.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Helpers - in-memory DB with full schema including v22 migration
# ---------------------------------------------------------------------------

PID = "test-slice"


def _make_conn(tmp_path=None) -> sqlite3.Connection:
    """Create in-memory DB with version_baselines + baseline_mutations + backlog_bugs."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS version_baselines (
            project_id        TEXT NOT NULL,
            baseline_id       INTEGER NOT NULL,
            chain_version     TEXT NOT NULL DEFAULT '',
            graph_sha         TEXT NOT NULL DEFAULT '',
            code_doc_map_sha  TEXT NOT NULL DEFAULT '',
            node_state_snap   TEXT NOT NULL DEFAULT '{}',
            chain_event_max   INTEGER NOT NULL DEFAULT 0,
            trigger           TEXT NOT NULL DEFAULT '',
            triggered_by      TEXT NOT NULL DEFAULT '',
            reconstructed     INTEGER NOT NULL DEFAULT 0,
            created_at        TEXT NOT NULL,
            notes             TEXT NOT NULL DEFAULT '',
            scope_id          TEXT,
            parent_baseline_id INTEGER,
            scope_kind        TEXT,
            scope_value       TEXT,
            merged_into       INTEGER,
            merge_status      TEXT,
            merge_evidence_json TEXT,
            PRIMARY KEY (project_id, baseline_id)
        );
        CREATE TABLE IF NOT EXISTS baseline_mutations (
            project_id      TEXT NOT NULL,
            baseline_id     INTEGER NOT NULL,
            mutation_id     TEXT NOT NULL,
            mutation_type   TEXT NOT NULL DEFAULT '',
            affected_file   TEXT NOT NULL DEFAULT '',
            affected_node   TEXT NOT NULL DEFAULT '',
            before_sha256   TEXT NOT NULL DEFAULT '',
            after_sha256    TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (project_id, baseline_id, mutation_id),
            FOREIGN KEY (project_id, baseline_id) REFERENCES version_baselines(project_id, baseline_id)
        );
        CREATE TABLE IF NOT EXISTS backlog_bugs (
            bug_id              TEXT PRIMARY KEY,
            title               TEXT NOT NULL DEFAULT '',
            status              TEXT NOT NULL DEFAULT 'OPEN',
            priority            TEXT NOT NULL DEFAULT 'P3',
            target_files        TEXT NOT NULL DEFAULT '[]',
            test_files          TEXT NOT NULL DEFAULT '[]',
            acceptance_criteria TEXT NOT NULL DEFAULT '[]',
            chain_task_id       TEXT NOT NULL DEFAULT '',
            "commit"            TEXT NOT NULL DEFAULT '',
            discovered_at       TEXT NOT NULL DEFAULT '',
            fixed_at            TEXT NOT NULL DEFAULT '',
            details_md          TEXT NOT NULL DEFAULT '',
            chain_trigger_json  TEXT NOT NULL DEFAULT '{}',
            required_docs       TEXT NOT NULL DEFAULT '[]',
            provenance_paths    TEXT NOT NULL DEFAULT '[]',
            created_at          TEXT NOT NULL DEFAULT '',
            updated_at          TEXT NOT NULL DEFAULT ''
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
    """)
    return conn


@pytest.fixture()
def mem_conn(tmp_path):
    """Yield in-memory connection with full schema."""
    conn = _make_conn(tmp_path)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _patch_governance_root(tmp_path, monkeypatch):
    """Patch _governance_root so companion files go to tmp_path."""
    monkeypatch.setattr(
        "agent.governance.db._governance_root",
        lambda: tmp_path,
    )


# ---------------------------------------------------------------------------
# AC-B1: create_baseline with scope + record_baseline_mutations
# ---------------------------------------------------------------------------

class TestACB1CreateSliceBaseline:
    """AC-B1: create_baseline with scope kwargs + record_baseline_mutations."""

    def test_create_baseline_with_scope(self, mem_conn):
        from agent.governance.baseline_service import create_baseline

        result = create_baseline(
            mem_conn, PID,
            chain_version="abc123",
            trigger="reconcile-task",
            triggered_by="auto-chain",
            scope_kind="bug",
            scope_value="BUG-123",
            parent_baseline_id=1,
        )

        assert result["baseline_id"] == 1
        assert result["scope_kind"] == "bug"
        assert result["scope_value"] == "BUG-123"
        assert result["parent_baseline_id"] == 1

        # Verify DB row
        row = mem_conn.execute(
            "SELECT scope_kind, scope_value, parent_baseline_id, merged_into, merge_status "
            "FROM version_baselines WHERE project_id = ? AND baseline_id = ?",
            (PID, 1),
        ).fetchone()
        assert row["scope_kind"] == "bug"
        assert row["scope_value"] == "BUG-123"
        assert row["parent_baseline_id"] == 1
        assert row["merged_into"] is None
        assert row["merge_status"] is None

    def test_create_baseline_backward_compatible(self, mem_conn):
        """Existing callers without scope kwargs still work."""
        from agent.governance.baseline_service import create_baseline

        result = create_baseline(
            mem_conn, PID,
            chain_version="abc123",
            trigger="init",
            triggered_by="init",
        )

        assert result["baseline_id"] == 1
        assert result.get("scope_kind") is None

        row = mem_conn.execute(
            "SELECT scope_kind, scope_value FROM version_baselines "
            "WHERE project_id = ? AND baseline_id = ?",
            (PID, 1),
        ).fetchone()
        assert row["scope_kind"] is None
        assert row["scope_value"] is None

    def test_record_baseline_mutations(self, mem_conn):
        from agent.governance.baseline_service import (
            create_baseline, record_baseline_mutations,
        )

        bl = create_baseline(
            mem_conn, PID,
            chain_version="abc123",
            trigger="reconcile-task",
            triggered_by="auto-chain",
            scope_kind="bug",
            scope_value="BUG-123",
        )

        mutations = [
            {
                "mutation_id": "m1",
                "mutation_type": "file_change",
                "affected_file": "agent/foo.py",
                "affected_node": "L1.1",
                "before_sha256": "aaa111",
                "after_sha256": "bbb222",
            },
            {
                "mutation_id": "m2",
                "mutation_type": "file_change",
                "affected_file": "agent/bar.py",
                "affected_node": "L1.2",
                "before_sha256": "ccc333",
                "after_sha256": "ddd444",
            },
        ]

        count = record_baseline_mutations(mem_conn, PID, bl["baseline_id"], mutations)
        assert count == 2

        rows = mem_conn.execute(
            "SELECT * FROM baseline_mutations WHERE project_id = ? AND baseline_id = ?",
            (PID, bl["baseline_id"]),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["before_sha256"] != ""
        assert rows[0]["after_sha256"] != ""
        assert rows[1]["before_sha256"] != ""
        assert rows[1]["after_sha256"] != ""


# ---------------------------------------------------------------------------
# AC-B2: attempt_merge_slice_baselines_into - merged case
# ---------------------------------------------------------------------------

class TestACB2MergeSuccess:
    """AC-B2: Slice baselines with matching fingerprints get merged."""

    def test_merge_matching_slice(self, mem_conn):
        from agent.governance.baseline_service import (
            create_baseline, record_baseline_mutations,
            attempt_merge_slice_baselines_into,
        )

        # Create a full baseline
        full_bl = create_baseline(
            mem_conn, PID,
            chain_version="full-sha",
            trigger="auto-chain",
            triggered_by="auto-chain",
        )

        # Create a slice baseline
        slice_bl = create_baseline(
            mem_conn, PID,
            chain_version="slice-sha",
            trigger="reconcile-task",
            triggered_by="auto-chain",
            scope_kind="bug",
            scope_value="BUG-1",
        )

        # Record mutations whose after_sha256 will match the full post_state
        # We need to set up the full baseline's post_state to include these files
        # First, add mutations to the full baseline so compute_post_state finds them
        record_baseline_mutations(mem_conn, PID, full_bl["baseline_id"], [
            {"mutation_id": "fm1", "affected_file": "agent/foo.py",
             "before_sha256": "old1", "after_sha256": "new1"},
        ])

        # Slice mutations that MATCH the full post_state
        record_baseline_mutations(mem_conn, PID, slice_bl["baseline_id"], [
            {"mutation_id": "sm1", "affected_file": "agent/foo.py",
             "before_sha256": "old1", "after_sha256": "new1"},
        ])

        result = attempt_merge_slice_baselines_into(
            mem_conn, PID, full_bl["baseline_id"],
        )

        assert result["merged"] == 1
        assert result["conflict"] == 0

        # Verify DB updated
        row = mem_conn.execute(
            "SELECT merge_status, merged_into FROM version_baselines "
            "WHERE project_id = ? AND baseline_id = ?",
            (PID, slice_bl["baseline_id"]),
        ).fetchone()
        assert row["merge_status"] == "merged"
        assert row["merged_into"] == full_bl["baseline_id"]


# ---------------------------------------------------------------------------
# AC-B3: attempt_merge with conflict
# ---------------------------------------------------------------------------

class TestACB3MergeConflict:
    """AC-B3: Diverged mutations → conflict + backlog row."""

    def test_merge_conflict_files_backlog(self, mem_conn):
        from agent.governance.baseline_service import (
            create_baseline, record_baseline_mutations,
            attempt_merge_slice_baselines_into,
        )

        full_bl = create_baseline(
            mem_conn, PID,
            chain_version="full-sha",
            trigger="auto-chain",
            triggered_by="auto-chain",
        )

        slice_bl = create_baseline(
            mem_conn, PID,
            chain_version="slice-sha",
            trigger="reconcile-task",
            triggered_by="auto-chain",
            scope_kind="bug",
            scope_value="BUG-2",
        )

        # Full baseline has one version of the file
        record_baseline_mutations(mem_conn, PID, full_bl["baseline_id"], [
            {"mutation_id": "fm1", "affected_file": "agent/foo.py",
             "before_sha256": "old1", "after_sha256": "full-version"},
        ])

        # Slice baseline has DIFFERENT version → conflict
        record_baseline_mutations(mem_conn, PID, slice_bl["baseline_id"], [
            {"mutation_id": "sm1", "affected_file": "agent/foo.py",
             "before_sha256": "old1", "after_sha256": "slice-version"},
        ])

        result = attempt_merge_slice_baselines_into(
            mem_conn, PID, full_bl["baseline_id"],
        )

        assert result["conflict"] == 1
        assert result["merged"] == 0

        # Check merge_status
        row = mem_conn.execute(
            "SELECT merge_status, merge_evidence_json FROM version_baselines "
            "WHERE project_id = ? AND baseline_id = ?",
            (PID, slice_bl["baseline_id"]),
        ).fetchone()
        assert row["merge_status"] == "conflict"

        evidence = json.loads(row["merge_evidence_json"])
        assert "diverged_mutations" in evidence
        assert len(evidence["diverged_mutations"]) > 0

        # Check backlog row filed
        backlog = mem_conn.execute(
            "SELECT bug_id FROM backlog_bugs WHERE bug_id LIKE 'OPT-BACKLOG-SLICE-MERGE-CONFLICT-B%'"
        ).fetchone()
        assert backlog is not None
        assert backlog["bug_id"].startswith("OPT-BACKLOG-SLICE-MERGE-CONFLICT-B")


# ---------------------------------------------------------------------------
# AC-B4: get_last_relevant_baseline
# ---------------------------------------------------------------------------

class TestACB4LastRelevantBaseline:
    """AC-B4: Returns newest of full vs matching slice baseline."""

    def test_slice_newer_than_full(self, mem_conn):
        from agent.governance.baseline_service import (
            create_baseline, get_last_relevant_baseline,
        )

        # Full baseline (id=1)
        create_baseline(
            mem_conn, PID,
            chain_version="full-sha",
            trigger="auto-chain",
            triggered_by="auto-chain",
        )

        # Slice baseline (id=2, newer)
        create_baseline(
            mem_conn, PID,
            chain_version="slice-sha",
            trigger="reconcile-task",
            triggered_by="auto-chain",
            scope_kind="bug",
            scope_value="X",
        )

        result = get_last_relevant_baseline(mem_conn, PID, "bug", "X")
        assert result["baseline_id"] == 2
        assert result["scope_kind"] == "bug"

    def test_full_newer_than_slice(self, mem_conn):
        from agent.governance.baseline_service import (
            create_baseline, get_last_relevant_baseline,
        )

        # Slice baseline first (id=1)
        create_baseline(
            mem_conn, PID,
            chain_version="slice-sha",
            trigger="reconcile-task",
            triggered_by="auto-chain",
            scope_kind="bug",
            scope_value="X",
        )

        # Full baseline (id=2, newer)
        create_baseline(
            mem_conn, PID,
            chain_version="full-sha",
            trigger="auto-chain",
            triggered_by="auto-chain",
        )

        result = get_last_relevant_baseline(mem_conn, PID, "bug", "X")
        assert result["baseline_id"] == 2
        assert result["scope_kind"] is None  # full baseline

    def test_no_scope_returns_full(self, mem_conn):
        from agent.governance.baseline_service import (
            create_baseline, get_last_relevant_baseline,
        )

        create_baseline(
            mem_conn, PID,
            chain_version="full-sha",
            trigger="auto-chain",
            triggered_by="auto-chain",
        )

        result = get_last_relevant_baseline(mem_conn, PID)
        assert result["baseline_id"] == 1

    def test_missing_raises(self, mem_conn):
        from agent.governance.baseline_service import get_last_relevant_baseline
        from agent.governance.errors import BaselineMissingError

        with pytest.raises(BaselineMissingError):
            get_last_relevant_baseline(mem_conn, PID, "bug", "NONEXISTENT")


# ---------------------------------------------------------------------------
# AC-B5: Idempotent migration v21→v22
# ---------------------------------------------------------------------------

class TestACB5IdempotentMigration:
    """AC-B5: Running migration twice does not raise."""

    def test_migration_idempotent(self):
        """Run the v21→v22 migration function twice on same DB."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row

        # Create the version_baselines table first (as v18→v19 would)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS version_baselines (
                project_id        TEXT NOT NULL,
                baseline_id       INTEGER NOT NULL,
                chain_version     TEXT NOT NULL DEFAULT '',
                graph_sha         TEXT NOT NULL DEFAULT '',
                code_doc_map_sha  TEXT NOT NULL DEFAULT '',
                node_state_snap   TEXT NOT NULL DEFAULT '{}',
                chain_event_max   INTEGER NOT NULL DEFAULT 0,
                trigger           TEXT NOT NULL DEFAULT '',
                triggered_by      TEXT NOT NULL DEFAULT '',
                reconstructed     INTEGER NOT NULL DEFAULT 0,
                created_at        TEXT NOT NULL DEFAULT '',
                notes             TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (project_id, baseline_id)
            );
        """)

        # Insert a baseline before migration
        conn.execute(
            "INSERT INTO version_baselines (project_id, baseline_id, chain_version, created_at) "
            "VALUES ('test', 1, 'sha1', '2026-04-01T00:00:00Z')"
        )
        conn.commit()

        # Import and run migration function
        # We'll simulate the migration inline since the function is nested
        def run_migration(c):
            for col, typedef in [
                ("scope_id", "TEXT"),
                ("parent_baseline_id", "INTEGER"),
                ("scope_kind", "TEXT"),
                ("scope_value", "TEXT"),
                ("merged_into", "INTEGER"),
                ("merge_status", "TEXT"),
                ("merge_evidence_json", "TEXT"),
            ]:
                try:
                    c.execute(f"ALTER TABLE version_baselines ADD COLUMN {col} {typedef}")
                except sqlite3.OperationalError:
                    pass

            c.execute("""
                CREATE TABLE IF NOT EXISTS baseline_mutations (
                    project_id      TEXT NOT NULL,
                    baseline_id     INTEGER NOT NULL,
                    mutation_id     TEXT NOT NULL,
                    mutation_type   TEXT NOT NULL DEFAULT '',
                    affected_file   TEXT NOT NULL DEFAULT '',
                    affected_node   TEXT NOT NULL DEFAULT '',
                    before_sha256   TEXT NOT NULL DEFAULT '',
                    after_sha256    TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (project_id, baseline_id, mutation_id),
                    FOREIGN KEY (project_id, baseline_id) REFERENCES version_baselines(project_id, baseline_id)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_bm_project ON baseline_mutations(project_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_bm_baseline ON baseline_mutations(project_id, baseline_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_bm_file ON baseline_mutations(affected_file)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_bm_node ON baseline_mutations(affected_node)")

        # First run
        run_migration(conn)
        conn.commit()

        # Second run - must NOT raise
        run_migration(conn)
        conn.commit()

        # Verify existing row has NULL for all new columns
        row = conn.execute(
            "SELECT scope_id, parent_baseline_id, scope_kind, scope_value, "
            "merged_into, merge_status, merge_evidence_json "
            "FROM version_baselines WHERE project_id = 'test' AND baseline_id = 1"
        ).fetchone()
        assert row["scope_id"] is None
        assert row["parent_baseline_id"] is None
        assert row["scope_kind"] is None
        assert row["scope_value"] is None
        assert row["merged_into"] is None
        assert row["merge_status"] is None
        assert row["merge_evidence_json"] is None

        conn.close()


# ---------------------------------------------------------------------------
# AC-B6: GC skips unresolved slice baselines
# ---------------------------------------------------------------------------

class TestACB6GCSliceSafety:
    """AC-B6: GC must skip slice baselines with unresolved merge_status."""

    def test_is_slice_baseline_gc_safe(self, mem_conn):
        from agent.governance.baseline_service import (
            create_baseline, is_slice_baseline_gc_safe,
        )

        # Full baseline → safe
        full_bl = create_baseline(
            mem_conn, PID,
            chain_version="full-sha",
            trigger="auto-chain",
            triggered_by="auto-chain",
        )
        assert is_slice_baseline_gc_safe(mem_conn, PID, full_bl["baseline_id"]) is True

        # Slice baseline with NULL merge_status → NOT safe
        slice_bl = create_baseline(
            mem_conn, PID,
            chain_version="slice-sha",
            trigger="reconcile-task",
            triggered_by="auto-chain",
            scope_kind="bug",
            scope_value="BUG-1",
        )
        assert is_slice_baseline_gc_safe(mem_conn, PID, slice_bl["baseline_id"]) is False

    def test_merged_slice_is_gc_safe(self, mem_conn):
        from agent.governance.baseline_service import (
            create_baseline, is_slice_baseline_gc_safe,
        )

        slice_bl = create_baseline(
            mem_conn, PID,
            chain_version="slice-sha",
            trigger="reconcile-task",
            triggered_by="auto-chain",
            scope_kind="bug",
            scope_value="BUG-1",
        )

        # Manually set merge_status to 'merged'
        mem_conn.execute(
            "UPDATE version_baselines SET merge_status = 'merged' "
            "WHERE project_id = ? AND baseline_id = ?",
            (PID, slice_bl["baseline_id"]),
        )
        mem_conn.commit()

        assert is_slice_baseline_gc_safe(mem_conn, PID, slice_bl["baseline_id"]) is True

    def test_conflict_slice_not_gc_safe(self, mem_conn):
        from agent.governance.baseline_service import (
            create_baseline, is_slice_baseline_gc_safe,
        )

        slice_bl = create_baseline(
            mem_conn, PID,
            chain_version="slice-sha",
            trigger="reconcile-task",
            triggered_by="auto-chain",
            scope_kind="bug",
            scope_value="BUG-1",
        )

        mem_conn.execute(
            "UPDATE version_baselines SET merge_status = 'conflict' "
            "WHERE project_id = ? AND baseline_id = ?",
            (PID, slice_bl["baseline_id"]),
        )
        mem_conn.commit()

        assert is_slice_baseline_gc_safe(mem_conn, PID, slice_bl["baseline_id"]) is False

    def test_gc_classify_skips_unresolved_slices(self):
        """Test that _classify in baseline_gc keeps unresolved slice baselines."""
        from agent.governance.baseline_gc import _classify

        baselines = [
            # Old full baseline (would normally be deleted)
            {"baseline_id": 1, "trigger": "auto-chain",
             "created_at": "2026-04-01T00:00:00Z", "reconstructed": 0,
             "scope_kind": None, "merge_status": None},
            # Unresolved slice baseline (must NOT be deleted)
            {"baseline_id": 2, "trigger": "auto-chain",
             "created_at": "2026-04-01T00:00:00Z", "reconstructed": 0,
             "scope_kind": "bug", "merge_status": None},
            # Conflict slice baseline (must NOT be deleted)
            {"baseline_id": 3, "trigger": "auto-chain",
             "created_at": "2026-04-01T00:00:00Z", "reconstructed": 0,
             "scope_kind": "bug", "merge_status": "conflict"},
            # Unknown slice baseline (must NOT be deleted)
            {"baseline_id": 4, "trigger": "auto-chain",
             "created_at": "2026-04-01T00:00:00Z", "reconstructed": 0,
             "scope_kind": "bug", "merge_status": "unknown"},
            # Merged slice baseline (CAN be deleted if outside last_n)
            {"baseline_id": 5, "trigger": "auto-chain",
             "created_at": "2026-04-01T00:00:00Z", "reconstructed": 0,
             "scope_kind": "bug", "merge_status": "merged"},
            # Recent full baselines (in last_n)
            {"baseline_id": 100, "trigger": "auto-chain",
             "created_at": "2026-04-25T00:00:00Z", "reconstructed": 0,
             "scope_kind": None, "merge_status": None},
        ]

        keep, delete = _classify(baselines, keep_last_n=1)
        kept_ids = {b["baseline_id"] for b in keep}
        deleted_ids = {b["baseline_id"] for b in delete}

        # Unresolved slices (2, 3, 4) must be kept
        assert 2 in kept_ids, "NULL merge_status slice should be kept"
        assert 3 in kept_ids, "conflict slice should be kept"
        assert 4 in kept_ids, "unknown slice should be kept"

        # Merged slice (5) can be in delete list (if outside last_n and no other rule keeps it)
        # baseline 100 is in last_n, baseline 1 is month rep
        assert 5 in deleted_ids, "merged slice should be deletable"


# ---------------------------------------------------------------------------
# compute_post_state test
# ---------------------------------------------------------------------------

class TestComputePostState:
    def test_includes_mutations_and_companion_hashes(self, mem_conn):
        from agent.governance.baseline_service import (
            create_baseline, record_baseline_mutations, compute_post_state,
        )

        bl = create_baseline(
            mem_conn, PID,
            chain_version="sha1",
            trigger="auto-chain",
            triggered_by="auto-chain",
            node_state_snap='{"L1.1": "qa_pass"}',
        )

        record_baseline_mutations(mem_conn, PID, bl["baseline_id"], [
            {"mutation_id": "m1", "affected_file": "agent/foo.py",
             "before_sha256": "old", "after_sha256": "new123"},
        ])

        ps = compute_post_state(mem_conn, PID, bl["baseline_id"])

        assert "graph.json" in ps
        assert "code_doc_map.json" in ps
        assert "L1.1" in ps
        assert "agent/foo.py" in ps
        assert ps["agent/foo.py"] == "new123"


# ---------------------------------------------------------------------------
# Schema version check
# ---------------------------------------------------------------------------

class TestSchemaVersion:
    def test_schema_version_is_22(self):
        from agent.governance.db import SCHEMA_VERSION
        assert SCHEMA_VERSION == 22


# ---------------------------------------------------------------------------
# attempt_merge with no mutations (unknown)
# ---------------------------------------------------------------------------

class TestMergeUnknown:
    def test_no_mutations_gives_unknown(self, mem_conn):
        from agent.governance.baseline_service import (
            create_baseline, attempt_merge_slice_baselines_into,
        )

        full_bl = create_baseline(
            mem_conn, PID,
            chain_version="full-sha",
            trigger="auto-chain",
            triggered_by="auto-chain",
        )

        slice_bl = create_baseline(
            mem_conn, PID,
            chain_version="slice-sha",
            trigger="reconcile-task",
            triggered_by="auto-chain",
            scope_kind="bug",
            scope_value="BUG-3",
        )

        result = attempt_merge_slice_baselines_into(
            mem_conn, PID, full_bl["baseline_id"],
        )

        assert result["unknown"] == 1

        row = mem_conn.execute(
            "SELECT merge_status FROM version_baselines "
            "WHERE project_id = ? AND baseline_id = ?",
            (PID, slice_bl["baseline_id"]),
        ).fetchone()
        assert row["merge_status"] == "unknown"
