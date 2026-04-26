"""Tests for governance.reconcile — scan, diff, merge, sync, verify."""
import json
import os
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from dataclasses import asdict

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


def _make_graph_with_nodes(nodes_data: list[dict]):
    """Create a minimal AcceptanceGraph with given node data."""
    from governance.graph import AcceptanceGraph
    g = AcceptanceGraph()
    for nd in nodes_data:
        nid = nd["id"]
        g.G.add_node(nid, **nd)
    return g


def _make_in_memory_db():
    """Create an in-memory SQLite DB with governance schema (minimal)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS node_state (
            project_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            verify_status TEXT NOT NULL DEFAULT 'pending',
            build_status TEXT NOT NULL DEFAULT 'impl:missing',
            evidence_json TEXT,
            updated_by TEXT,
            updated_at TEXT,
            version INTEGER DEFAULT 1,
            PRIMARY KEY (project_id, node_id)
        );
        CREATE TABLE IF NOT EXISTS node_history (
            project_id TEXT, node_id TEXT, from_status TEXT, to_status TEXT,
            role TEXT, evidence_json TEXT, session_id TEXT, ts TEXT, version INTEGER
        );
        CREATE TABLE IF NOT EXISTS project_version (
            project_id TEXT PRIMARY KEY,
            chain_version TEXT,
            git_head TEXT,
            updated_by TEXT,
            updated_at TEXT,
            dirty_files TEXT DEFAULT '[]',
            git_synced_at TEXT
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            project_id TEXT, version INTEGER, snapshot_json TEXT,
            created_at TEXT, created_by TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_index (
            event_id TEXT PRIMARY KEY, project_id TEXT, event TEXT,
            actor TEXT, ok INTEGER, ts TEXT, node_ids TEXT
        );
    """)
    return conn


class TestPhaseDiff(unittest.TestCase):
    """Test phase_diff: stale ref detection + confidence scoring."""

    def test_healthy_node_detected(self):
        from governance.reconcile import phase_diff
        graph = _make_graph_with_nodes([
            {"id": "L1.1", "primary": ["agent/server.py"], "secondary": [], "test": []},
        ])
        file_set = {"agent/server.py", "agent/other.py"}
        file_meta = {"agent/server.py": {"path": "agent/server.py", "type": "source", "name": "server.py"},
                     "agent/other.py": {"path": "agent/other.py", "type": "source", "name": "other.py"}}
        report = phase_diff(graph, file_set, file_meta)
        self.assertEqual(report.healthy_nodes, ["L1.1"])
        self.assertEqual(report.stale_refs, [])
        self.assertEqual(report.orphan_nodes, [])

    def test_stale_ref_with_unique_basename_high_confidence(self):
        from governance.reconcile import phase_diff
        graph = _make_graph_with_nodes([
            {"id": "L1.1", "primary": ["old/governance/server.py"], "secondary": [], "test": []},
        ])
        file_set = {"agent/governance/server.py"}
        file_meta = {"agent/governance/server.py": {
            "path": "agent/governance/server.py", "type": "source", "name": "server.py"}}
        report = phase_diff(graph, file_set, file_meta)
        self.assertEqual(len(report.stale_refs), 1)
        ref = report.stale_refs[0]
        self.assertEqual(ref.node_id, "L1.1")
        self.assertEqual(ref.old_path, "old/governance/server.py")
        self.assertEqual(ref.suggestion, "agent/governance/server.py")
        self.assertIn("same_basename", ref.evidence)

    def test_stale_ref_no_match(self):
        from governance.reconcile import phase_diff
        graph = _make_graph_with_nodes([
            {"id": "L1.1", "primary": ["deleted_forever.py"], "secondary": [], "test": []},
        ])
        file_set = {"agent/server.py"}
        file_meta = {"agent/server.py": {"path": "agent/server.py", "type": "source", "name": "server.py"}}
        report = phase_diff(graph, file_set, file_meta)
        self.assertEqual(len(report.stale_refs), 1)
        self.assertIsNone(report.stale_refs[0].suggestion)
        self.assertEqual(report.stale_refs[0].confidence, "low")

    def test_orphan_node_detected(self):
        from governance.reconcile import phase_diff
        graph = _make_graph_with_nodes([
            {"id": "L1.1", "primary": ["gone1.py", "gone2.py"], "secondary": [], "test": []},
        ])
        file_set = {"agent/other.py"}
        file_meta = {"agent/other.py": {"path": "agent/other.py", "type": "source", "name": "other.py"}}
        report = phase_diff(graph, file_set, file_meta)
        self.assertIn("L1.1", report.orphan_nodes)

    def test_ambiguous_basename_low_confidence(self):
        """Multiple files with same basename and same confidence → low."""
        from governance.reconcile import phase_diff
        graph = _make_graph_with_nodes([
            {"id": "L1.1", "primary": ["old/utils.py"], "secondary": [], "test": []},
        ])
        file_set = {"src/utils.py", "lib/utils.py"}
        file_meta = {
            "src/utils.py": {"path": "src/utils.py", "type": "source", "name": "utils.py"},
            "lib/utils.py": {"path": "lib/utils.py", "type": "source", "name": "utils.py"},
        }
        report = phase_diff(graph, file_set, file_meta)
        self.assertEqual(len(report.stale_refs), 1)
        ref = report.stale_refs[0]
        # Both have same confidence so it should be low (ambiguous)
        self.assertEqual(ref.confidence, "low")

    def test_unmapped_files_detected(self):
        from governance.reconcile import phase_diff
        graph = _make_graph_with_nodes([
            {"id": "L1.1", "primary": ["agent/server.py"], "secondary": [], "test": []},
        ])
        file_set = {"agent/server.py", "agent/new_module.py"}
        file_meta = {
            "agent/server.py": {"path": "agent/server.py", "type": "source", "name": "server.py"},
            "agent/new_module.py": {"path": "agent/new_module.py", "type": "source", "name": "new_module.py"},
        }
        report = phase_diff(graph, file_set, file_meta)
        self.assertIn("agent/new_module.py", report.unmapped_files)

    def test_type_match_boosts_confidence(self):
        """Test file in test field matched to test file gets type_match."""
        from governance.reconcile import phase_diff
        graph = _make_graph_with_nodes([
            {"id": "L4.1", "primary": [], "secondary": [], "test": ["old/test_server.py"]},
        ])
        file_set = {"agent/tests/test_server.py"}
        file_meta = {"agent/tests/test_server.py": {
            "path": "agent/tests/test_server.py", "type": "test", "name": "test_server.py"}}
        report = phase_diff(graph, file_set, file_meta)
        self.assertEqual(len(report.stale_refs), 1)
        ref = report.stale_refs[0]
        self.assertIn("type_match", ref.evidence)


class TestPhaseMerge(unittest.TestCase):
    """Test phase_merge: in-memory graph updates."""

    def test_high_confidence_auto_fixed(self):
        from governance.reconcile import phase_merge, MergeOptions, RefSuggestion, DiffReport
        graph = _make_graph_with_nodes([
            {"id": "L1.1", "primary": ["old/server.py"], "secondary": [], "test": []},
        ])
        diff = DiffReport(
            stale_refs=[RefSuggestion(
                node_id="L1.1", field="primary", old_path="old/server.py",
                suggestion="new/server.py", confidence="high",
                evidence=["same_basename", "similar_parent_dir", "type_match"])],
        )
        options = MergeOptions(auto_fix_stale=True, require_high_confidence_only=True)
        changes, count = phase_merge(graph, diff, options)
        self.assertEqual(count, 1)
        self.assertEqual(changes[0]["action"], "fix_ref")
        self.assertEqual(changes[0]["new"], "new/server.py")
        # Verify graph was actually updated
        self.assertEqual(graph.get_node("L1.1")["primary"], ["new/server.py"])

    def test_medium_confidence_skipped_when_high_only(self):
        from governance.reconcile import phase_merge, MergeOptions, RefSuggestion, DiffReport
        graph = _make_graph_with_nodes([
            {"id": "L1.1", "primary": ["old/server.py"], "secondary": [], "test": []},
        ])
        diff = DiffReport(
            stale_refs=[RefSuggestion(
                node_id="L1.1", field="primary", old_path="old/server.py",
                suggestion="new/server.py", confidence="medium",
                evidence=["same_basename"])],
        )
        options = MergeOptions(auto_fix_stale=True, require_high_confidence_only=True)
        changes, count = phase_merge(graph, diff, options)
        self.assertEqual(count, 0)
        self.assertEqual(changes[0]["action"], "skip_ref")
        self.assertEqual(changes[0]["reason"], "below confidence threshold")
        # Graph unchanged
        self.assertEqual(graph.get_node("L1.1")["primary"], ["old/server.py"])

    def test_max_auto_fix_count_respected(self):
        from governance.reconcile import phase_merge, MergeOptions, RefSuggestion, DiffReport
        graph = _make_graph_with_nodes([
            {"id": "L1.1", "primary": ["old/a.py", "old/b.py", "old/c.py"],
             "secondary": [], "test": []},
        ])
        diff = DiffReport(stale_refs=[
            RefSuggestion(node_id="L1.1", field="primary", old_path=f"old/{n}.py",
                         suggestion=f"new/{n}.py", confidence="high",
                         evidence=["same_basename", "similar_parent_dir", "type_match"])
            for n in ("a", "b", "c")
        ])
        options = MergeOptions(auto_fix_stale=True, require_high_confidence_only=True,
                              max_auto_fix_count=2)
        changes, count = phase_merge(graph, diff, options)
        self.assertEqual(count, 2)
        skip_changes = [c for c in changes if c["action"] == "skip_ref"]
        self.assertEqual(len(skip_changes), 1)
        self.assertIn("max_auto_fix_count", skip_changes[0]["reason"])


class TestPhaseSync(unittest.TestCase):
    """Test phase_sync: DB state updates."""

    def test_orphan_waive_with_structured_reason(self):
        from governance.reconcile import phase_sync, WaiveReason
        conn = _make_in_memory_db()
        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status) VALUES (?, ?, ?)",
            ("test-proj", "L1.1", "pending"))
        conn.commit()

        graph = _make_graph_with_nodes([
            {"id": "L1.1", "primary": [], "secondary": [], "test": []},
        ])
        result = phase_sync(conn, "test-proj", graph, ["L1.1"],
                           {"mark_orphans_waived": True})
        self.assertEqual(result["orphans_waived"], 1)

        row = conn.execute(
            "SELECT verify_status, evidence_json FROM node_state WHERE node_id='L1.1'").fetchone()
        self.assertEqual(row["verify_status"], "waived")
        evidence = json.loads(row["evidence_json"])
        self.assertEqual(evidence["waive_reason"], WaiveReason.ORPHANED_BY_RECONCILE)

    def test_auto_unwaive_orphan_reason(self):
        """Node waived as orphan, but now has files → should un-waive."""
        from governance.reconcile import phase_sync, WaiveReason
        conn = _make_in_memory_db()
        evidence = json.dumps({"waive_reason": WaiveReason.ORPHANED_BY_RECONCILE})
        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status, evidence_json) VALUES (?, ?, ?, ?)",
            ("test-proj", "L1.1", "waived", evidence))
        conn.commit()

        graph = _make_graph_with_nodes([
            {"id": "L1.1", "primary": ["agent/server.py"], "secondary": [], "test": []},
        ])
        result = phase_sync(conn, "test-proj", graph, [],
                           {"mark_orphans_waived": False})
        self.assertEqual(result["unwaived"], 1)

        row = conn.execute(
            "SELECT verify_status FROM node_state WHERE node_id='L1.1'").fetchone()
        self.assertEqual(row["verify_status"], "pending")

    def test_manual_exception_not_unwaived(self):
        """Node waived as manual_exception should NOT be auto-unwaived."""
        from governance.reconcile import phase_sync, WaiveReason
        conn = _make_in_memory_db()
        evidence = json.dumps({"waive_reason": WaiveReason.MANUAL_EXCEPTION})
        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status, evidence_json) VALUES (?, ?, ?, ?)",
            ("test-proj", "L1.1", "waived", evidence))
        conn.commit()

        graph = _make_graph_with_nodes([
            {"id": "L1.1", "primary": ["agent/server.py"], "secondary": [], "test": []},
        ])
        result = phase_sync(conn, "test-proj", graph, [],
                           {"mark_orphans_waived": False})
        self.assertEqual(result["unwaived"], 0)

        row = conn.execute(
            "SELECT verify_status FROM node_state WHERE node_id='L1.1'").fetchone()
        self.assertEqual(row["verify_status"], "waived")

    def test_legacy_frozen_not_unwaived(self):
        from governance.reconcile import phase_sync, WaiveReason
        conn = _make_in_memory_db()
        evidence = json.dumps({"waive_reason": WaiveReason.LEGACY_FROZEN})
        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status, evidence_json) VALUES (?, ?, ?, ?)",
            ("test-proj", "L1.1", "waived", evidence))
        conn.commit()

        graph = _make_graph_with_nodes([
            {"id": "L1.1", "primary": ["agent/server.py"], "secondary": [], "test": []},
        ])
        result = phase_sync(conn, "test-proj", graph, [],
                           {"mark_orphans_waived": False})
        self.assertEqual(result["unwaived"], 0)


class TestPhaseVerify(unittest.TestCase):
    """Test phase_verify: consistency checks."""

    def test_graph_db_consistency_detects_missing(self):
        from governance.reconcile import phase_verify
        conn = _make_in_memory_db()
        # DB has L1.1 but graph has L1.1 and L1.2
        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status) VALUES (?, ?, ?)",
            ("test-proj", "L1.1", "pending"))
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, git_head) VALUES (?, ?, ?)",
            ("test-proj", "abc123", "abc123"))
        conn.commit()

        graph = _make_graph_with_nodes([
            {"id": "L1.1", "primary": ["a.py"], "secondary": [], "test": []},
            {"id": "L1.2", "primary": ["b.py"], "secondary": [], "test": []},
        ])

        with mock.patch("governance.preflight.run_preflight",
                        return_value={"ok": True, "blockers": [], "warnings": []}):
            report = phase_verify(conn, "test-proj", graph, {})

        self.assertFalse(report["graph_db_consistency"]["passed"])
        self.assertIn("L1.2", report["graph_db_consistency"]["in_graph_not_db"])

    def test_gate_smoke_blocks_pending(self):
        """Verify gate smoke test correctly detects that pending node is blocked."""
        from governance.reconcile import phase_verify
        conn = _make_in_memory_db()
        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status) VALUES (?, ?, ?)",
            ("test-proj", "L1.1", "pending"))
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, git_head) VALUES (?, ?, ?)",
            ("test-proj", "abc123", "abc123"))
        conn.commit()

        graph = _make_graph_with_nodes([
            {"id": "L1.1", "primary": ["a.py"], "secondary": [], "test": []},
        ])

        with mock.patch("governance.preflight.run_preflight",
                        return_value={"ok": True, "blockers": [], "warnings": []}):
            report = phase_verify(conn, "test-proj", graph, {})

        self.assertTrue(report["gate_test"]["passed"])
        self.assertIn("correctly blocked", report["gate_test"]["detail"])


class TestWaiveReason(unittest.TestCase):
    """Test should_auto_unwaive logic."""

    def test_orphan_reason_auto_unwaivable(self):
        from governance.reconcile import should_auto_unwaive, WaiveReason
        self.assertTrue(should_auto_unwaive(
            json.dumps({"waive_reason": WaiveReason.ORPHANED_BY_RECONCILE})))

    def test_auto_chain_temporary_auto_unwaivable(self):
        from governance.reconcile import should_auto_unwaive, WaiveReason
        self.assertTrue(should_auto_unwaive(
            json.dumps({"waive_reason": WaiveReason.AUTO_CHAIN_TEMPORARY})))

    def test_manual_exception_not_auto_unwaivable(self):
        from governance.reconcile import should_auto_unwaive, WaiveReason
        self.assertFalse(should_auto_unwaive(
            json.dumps({"waive_reason": WaiveReason.MANUAL_EXCEPTION})))

    def test_legacy_frozen_not_auto_unwaivable(self):
        from governance.reconcile import should_auto_unwaive, WaiveReason
        self.assertFalse(should_auto_unwaive(
            json.dumps({"waive_reason": WaiveReason.LEGACY_FROZEN})))

    def test_deprecated_not_auto_unwaivable(self):
        from governance.reconcile import should_auto_unwaive, WaiveReason
        self.assertFalse(should_auto_unwaive(
            json.dumps({"waive_reason": WaiveReason.DEPRECATED})))

    def test_legacy_no_reason_auto_unwaivable(self):
        """Legacy waived nodes without waive_reason should be auto-unwaivable."""
        from governance.reconcile import should_auto_unwaive
        self.assertTrue(should_auto_unwaive(json.dumps({"type": "manual_review"})))
        self.assertTrue(should_auto_unwaive(None))
        self.assertTrue(should_auto_unwaive(""))

    def test_preflight_autofix_auto_unwaivable(self):
        from governance.reconcile import should_auto_unwaive, WaiveReason
        self.assertTrue(should_auto_unwaive(
            json.dumps({"waive_reason": WaiveReason.PREFLIGHT_AUTOFIX})))


class TestConfidenceScoring(unittest.TestCase):
    """Test _score_suggestion multi-signal logic."""

    def test_all_signals_high(self):
        from governance.reconcile import _score_suggestion
        conf, ev = _score_suggestion(
            "agent/governance/server.py",
            "agent/governance/server.py",  # same dir
            "primary",
            {"agent/governance/server.py": {"type": "source"}},
        )
        self.assertEqual(conf, "high")
        self.assertIn("same_basename", ev)
        self.assertIn("similar_parent_dir", ev)
        self.assertIn("type_match", ev)

    def test_basename_only_low(self):
        from governance.reconcile import _score_suggestion
        conf, ev = _score_suggestion(
            "old/deep/path/server.py",
            "completely/different/server.py",
            "primary",
            {"completely/different/server.py": {"type": "config"}},  # wrong type
        )
        self.assertEqual(conf, "low")
        self.assertEqual(ev, ["same_basename"])

    def test_basename_plus_type_medium(self):
        from governance.reconcile import _score_suggestion
        conf, ev = _score_suggestion(
            "old/server.py",
            "new/different/server.py",
            "primary",
            {"new/different/server.py": {"type": "source"}},
        )
        self.assertEqual(conf, "medium")
        self.assertIn("type_match", ev)


class TestIdempotency(unittest.TestCase):
    """Verify reconcile is idempotent: second diff after merge shows 0 stale."""

    def test_merge_then_diff_shows_no_stale(self):
        from governance.reconcile import phase_diff, phase_merge, MergeOptions, _deep_copy_graph

        graph = _make_graph_with_nodes([
            {"id": "L1.1", "primary": ["old/server.py"], "secondary": [], "test": []},
        ])
        file_set = {"new/server.py"}
        file_meta = {"new/server.py": {"path": "new/server.py", "type": "source", "name": "server.py"}}

        # First diff
        diff1 = phase_diff(graph, file_set, file_meta)
        self.assertEqual(diff1.stats["stale_count"], 1)

        # Merge
        candidate = _deep_copy_graph(graph)
        changes, count = phase_merge(candidate, diff1,
                                     MergeOptions(require_high_confidence_only=False))
        self.assertGreater(count, 0)

        # Second diff on merged graph — should show 0 stale
        diff2 = phase_diff(candidate, file_set, file_meta)
        self.assertEqual(diff2.stats["stale_count"], 0)
        self.assertEqual(diff2.stats["orphan_count"], 0)


class TestTriggerBaselineWrite(unittest.TestCase):
    """Test _trigger_baseline_write uses version_baselines via baseline_service."""

    def test_trigger_baseline_write_uses_version_baselines(self):
        """AC7: _trigger_baseline_write writes to version_baselines with correct fields."""
        conn = _make_in_memory_db()
        # Add version_baselines table (full schema matching baseline_service expectations)
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
        """)
        # Insert project_version so chain_version can be retrieved
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, git_head) VALUES (?, ?, ?)",
            ("test-proj", "abc1234", "abc1234"),
        )
        conn.commit()

        from governance.reconcile_task import _trigger_baseline_write

        # Mock _write_companion_files to avoid disk I/O
        with mock.patch("governance.baseline_service._write_companion_files",
                        return_value={"graph_sha": "mock_graph", "code_doc_map_sha": "mock_cdm"}):
            _trigger_baseline_write(conn, "test-proj", "task-42")

        # Assert version_baselines has a row with correct fields
        row = conn.execute(
            "SELECT * FROM version_baselines WHERE project_id = ?",
            ("test-proj",),
        ).fetchone()
        self.assertIsNotNone(row, "Expected a row in version_baselines")
        self.assertEqual(row["project_id"], "test-proj")
        self.assertEqual(row["scope_kind"], "phase_i_reconcile")
        self.assertEqual(row["scope_value"], "task-42")
        self.assertEqual(row["trigger"], "reconcile-apply")
        self.assertEqual(row["triggered_by"], "reconcile-task")
        self.assertEqual(row["chain_version"], "abc1234")


if __name__ == "__main__":
    unittest.main()
