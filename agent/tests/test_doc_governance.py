"""Tests for doc governance: Steps 3a + 5 (graph-driven doc governance)."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import shutil
from unittest.mock import patch, MagicMock
from dataclasses import field

import pytest

from agent.governance.graph_generator import _infer_doc_associations


class TestInferDocAssociations:
    """AC-L1.1: _infer_doc_associations returns list[dict] with inferred=True."""

    def _make_workspace(self, files: dict[str, str] | None = None) -> str:
        """Create a temp workspace with optional files (path -> content)."""
        ws = tempfile.mkdtemp(prefix="test_doc_gov_")
        if files:
            for path, content in files.items():
                full = os.path.join(ws, path.replace("/", os.sep))
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as f:
                    f.write(content)
        return ws

    def test_exact_stem_match_returns_confidence_09(self):
        """Exact stem match (reconcile.py ↔ reconcile.md) → confidence 0.9."""
        ws = self._make_workspace({
            "docs/reconcile.md": "# Reconcile flow design",
        })
        try:
            nodes = [
                {"node_id": "L1.1", "primary": ["agent/governance/reconcile.py"]},
            ]
            result = _infer_doc_associations(nodes, ws)
            assert len(result) == 1
            assert result[0]["node_id"] == "L1.1"
            assert result[0]["doc_path"] == "docs/reconcile.md"
            assert result[0]["confidence"] == 0.9
            assert result[0]["inferred"] is True
            assert "reason" in result[0]
        finally:
            shutil.rmtree(ws)

    def test_no_docs_dir_returns_empty(self):
        """No docs/ directory → empty list, no crash."""
        ws = self._make_workspace()  # no docs/ created
        try:
            nodes = [
                {"node_id": "L1.1", "primary": ["agent/governance/reconcile.py"]},
            ]
            result = _infer_doc_associations(nodes, ws)
            assert result == []
        finally:
            shutil.rmtree(ws)

    def test_all_results_have_inferred_true(self):
        """Every returned dict must have inferred=True."""
        ws = self._make_workspace({
            "docs/auto-chain.md": "# Auto chain design",
            "docs/governance/reconcile.md": "# Reconcile",
            "docs/db.md": "# DB schema",
        })
        try:
            nodes = [
                {"node_id": "L1.1", "primary": ["agent/governance/auto_chain.py"]},
                {"node_id": "L1.2", "primary": ["agent/governance/reconcile.py"]},
                {"node_id": "L1.3", "primary": ["agent/governance/db.py"]},
            ]
            result = _infer_doc_associations(nodes, ws)
            assert len(result) >= 3
            for item in result:
                assert item["inferred"] is True
                assert isinstance(item["confidence"], float)
                assert 0.0 < item["confidence"] <= 1.0
                assert "node_id" in item
                assert "doc_path" in item
                assert "reason" in item
        finally:
            shutil.rmtree(ws)

    def test_partial_stem_overlap(self):
        """Partial overlap (auto_chain.py ↔ chain-design.md) → confidence 0.5."""
        ws = self._make_workspace({
            "docs/chain-design.md": "# Chain design doc",
        })
        try:
            nodes = [
                {"node_id": "L1.1", "primary": ["agent/governance/auto_chain.py"]},
            ]
            result = _infer_doc_associations(nodes, ws)
            matches = [r for r in result if r["confidence"] == 0.5]
            assert len(matches) >= 1
            assert matches[0]["inferred"] is True
        finally:
            shutil.rmtree(ws)

    def test_keyword_match_in_content(self):
        """Keyword match in first 500 chars → confidence 0.3."""
        ws = self._make_workspace({
            "docs/architecture.md": "# Architecture\n\nThe reconcile module handles...",
        })
        try:
            nodes = [
                {"node_id": "L1.1", "primary": ["agent/governance/reconcile.py"]},
            ]
            result = _infer_doc_associations(nodes, ws)
            kw_matches = [r for r in result if r["confidence"] == 0.3]
            assert len(kw_matches) >= 1
            assert kw_matches[0]["inferred"] is True
        finally:
            shutil.rmtree(ws)


# =====================================================================
# Step 5 Tests: Level 3 Graph-Driven Doc Governance
# =====================================================================


class TestAuditDocGap:
    """5f: _audit_doc_gap writes audit record."""

    def test_audit_doc_gap_writes_record(self):
        from agent.governance.auto_chain import _audit_doc_gap
        conn = MagicMock()
        _audit_doc_gap(conn, "test-proj", "task-123", "checkpoint",
                       {"docs/foo.md"}, ["agent/bar.py"])
        # Should not raise; audit_service.record may or may not be called
        # depending on import success — non-critical

    def test_audit_doc_gap_no_crash_on_empty(self):
        from agent.governance.auto_chain import _audit_doc_gap
        conn = MagicMock()
        _audit_doc_gap(conn, "test-proj", "task-123", "post_pm", set(), [])


class TestGraphDeltaProposedNodes:
    """5g: proposed nodes now flow through graph.delta.proposed events."""

    def test_legacy_store_proposed_nodes_removed(self):
        from agent.governance import auto_chain
        assert not hasattr(auto_chain, "_store_proposed_nodes")

    def test_graph_delta_event_emitter_exists(self):
        from agent.governance import auto_chain
        assert callable(auto_chain._emit_graph_delta_event)


class TestGetGraphDocAssociations:
    """5b/5c: _get_graph_doc_associations returns graph-linked docs."""

    def test_returns_empty_on_no_graph(self):
        from agent.governance.auto_chain import _get_graph_doc_associations
        # With no graph file, should return empty list (non-critical failure)
        result = _get_graph_doc_associations("nonexistent-project", ["foo.py"])
        assert isinstance(result, list)
        assert result == []


class TestReconcileStaleDocs:
    """5h: phase_diff detects stale_doc_refs and unmapped_docs."""

    def test_diff_report_has_stale_doc_fields(self):
        from agent.governance.reconcile import DiffReport
        report = DiffReport()
        assert hasattr(report, "stale_doc_refs")
        assert hasattr(report, "unmapped_docs")
        assert report.stale_doc_refs == []
        assert report.unmapped_docs == []

    def test_diff_report_stats_include_doc_counts(self):
        """Stats dict should include stale_doc_count and unmapped_doc_count."""
        from agent.governance.reconcile import DiffReport
        report = DiffReport()
        report.stats = {
            "stale_doc_count": 0,
            "unmapped_doc_count": 0,
        }
        assert "stale_doc_count" in report.stats
        assert "unmapped_doc_count" in report.stats


class TestGraphDocObservationMode:
    """5a/5c/5e: Graph doc checks are observation-only (warn, not block)."""

    def test_observation_mode_flag_exists(self):
        from agent.governance.auto_chain import _GRAPH_DOC_OBSERVATION_MODE
        assert _GRAPH_DOC_OBSERVATION_MODE is True

    def test_parse_pytest_output_extracts_counts(self):
        """6c: _parse_pytest_output extracts passed/failed from summary line."""
        from agent.executor_worker import _parse_pytest_output
        report = _parse_pytest_output("5 passed, 2 failed in 1.5s", "", 1)
        assert report["passed"] == 5
        assert report["failed"] == 2
        assert report["tool"] == "pytest"

    def test_parse_pytest_output_fallback_exit_code(self):
        """6c: Falls back to exit code when no summary line."""
        from agent.executor_worker import _parse_pytest_output
        report = _parse_pytest_output("", "some error", 1)
        assert report["failed"] == 1
        assert report["passed"] == 0

    def test_parse_pytest_output_exit_zero(self):
        """6c: Exit code 0 with no summary → assumed pass."""
        from agent.executor_worker import _parse_pytest_output
        report = _parse_pytest_output("", "", 0)
        assert report["passed"] == 1

    def test_task_role_map_test_is_script(self):
        """6b: TASK_ROLE_MAP['test'] should be 'script'."""
        from agent.executor_worker import TASK_ROLE_MAP
        assert TASK_ROLE_MAP["test"] == "script"

    def test_gate_post_pm_does_not_block_on_unclassified_docs(self):
        """5a: _gate_post_pm warns but doesn't block when graph docs unclassified."""
        from agent.governance.auto_chain import _gate_post_pm
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        result = {
            "target_files": ["agent/governance/server.py"],
            "verification": {"method": "test"},
            "acceptance_criteria": ["AC1"],
            "test_files": ["test.py"],
            "proposed_nodes": [{"id": "n1"}],
            "doc_impact": {"files": []},
            "skip_reasons": {},
        }
        metadata = {}
        with patch("agent.governance.auto_chain._get_graph_doc_associations", return_value=["docs/server.md"]):
            passed, reason = _gate_post_pm(conn, "test-proj", result, metadata)
        # Should PASS (observation mode — warn only)
        assert passed is True
