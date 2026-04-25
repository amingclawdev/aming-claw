"""Tests for Orchestrator + REST endpoint (AC5.4, AC5.5, AC5.6)."""
from __future__ import annotations

import json
import os
import time
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

import pytest


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------

@dataclass
class _Disc:
    type: str
    node_id: Optional[str]
    field: Optional[str]
    detail: str
    confidence: str


class _StubGraph:
    def __init__(self, nodes=None):
        self._nodes = nodes or {}

    def list_nodes(self):
        return list(self._nodes)

    def get_node(self, nid):
        return dict(self._nodes.get(nid, {}))

    def update_node_attrs(self, nid, attrs):
        for k, v in attrs.items():
            self._nodes.setdefault(nid, {})[k] = v


def _build_workspace(tmp_path):
    """Create a workspace with docs/ and agent/ stubs for full pipeline test."""
    docs = tmp_path / "docs"
    docs.mkdir()

    # A doc that references a missing .py (stale)
    stale_doc = docs / "stale.md"
    stale_doc.write_text("## Overview\n## API\n## Usage\nSee `agent/gone.py`.\n")
    old_time = time.time() - (30 * 86400)
    os.utime(stale_doc, (old_time, old_time))

    # A fresh doc
    fresh_doc = docs / "fresh.md"
    fresh_doc.write_text("## Overview\n## API\n## Usage\nAll good.\n")

    # Agent dir with a real file
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "real.py").write_text("# real module")

    return tmp_path


# ---------------------------------------------------------------------------
# AC5.5: Integration — all 5 phase keys present
# ---------------------------------------------------------------------------

class TestOrchestratorIntegration:
    def test_all_five_phases_present(self, tmp_path):
        """AC5.5: All 5 phase keys (A, B, C, D, E) present in response."""
        workspace = _build_workspace(tmp_path)

        graph = _StubGraph({
            "L1.1": {"title": "Core", "primary": ["agent/real.py"], "secondary": [], "test": []},
        })

        # Patch the heavy dependencies
        with patch("agent.governance.reconcile_phases.context.phase_scan") as mock_scan, \
             patch("agent.governance.reconcile_phases.context.load_project_graph") as mock_graph:

            mock_scan.return_value = ({"agent/real.py"}, {"agent/real.py": {}})
            mock_graph.return_value = graph

            from agent.governance.reconcile_phases.orchestrator import run_orchestrated

            result = run_orchestrated(
                project_id="test-proj",
                workspace_path=str(workspace),
                phases=["A", "E", "B", "C", "D"],
                dry_run=True,
            )

        assert "phases" in result
        for key in ["A", "E", "B", "C", "D"]:
            assert key in result["phases"], f"Phase {key} missing from result"

    def test_partial_phase_selection(self, tmp_path):
        """Only requested phases appear in result."""
        workspace = _build_workspace(tmp_path)

        with patch("agent.governance.reconcile_phases.context.phase_scan") as mock_scan, \
             patch("agent.governance.reconcile_phases.context.load_project_graph") as mock_graph:

            mock_scan.return_value = (set(), {})
            mock_graph.return_value = _StubGraph()

            from agent.governance.reconcile_phases.orchestrator import run_orchestrated

            result = run_orchestrated(
                project_id="test-proj",
                workspace_path=str(workspace),
                phases=["D"],
                dry_run=True,
            )

        assert list(result["phases"].keys()) == ["D"]


# ---------------------------------------------------------------------------
# AC5.4: REST endpoint shape
# ---------------------------------------------------------------------------

class TestReconcileV2Endpoint:
    def test_endpoint_returns_required_keys(self, tmp_path):
        """AC5.4: Response has report_path, summary, auto_fixed_count, human_review_count, phases."""
        workspace = _build_workspace(tmp_path)

        with patch("agent.governance.reconcile_phases.context.phase_scan") as mock_scan, \
             patch("agent.governance.reconcile_phases.context.load_project_graph") as mock_graph:

            mock_scan.return_value = (set(), {})
            mock_graph.return_value = _StubGraph()

            from agent.governance.reconcile_phases.orchestrator import run_orchestrated

            result = run_orchestrated(
                project_id="test-proj",
                workspace_path=str(workspace),
                dry_run=True,
            )

        assert "report_path" in result
        assert "summary" in result
        assert "auto_fixed_count" in result
        assert "human_review_count" in result
        assert "phases" in result

    def test_dry_run_zero_auto_fixed(self, tmp_path):
        """In dry_run mode, auto_fixed_count is always 0."""
        workspace = _build_workspace(tmp_path)

        with patch("agent.governance.reconcile_phases.context.phase_scan") as mock_scan, \
             patch("agent.governance.reconcile_phases.context.load_project_graph") as mock_graph:

            mock_scan.return_value = (set(), {})
            mock_graph.return_value = _StubGraph()

            from agent.governance.reconcile_phases.orchestrator import run_orchestrated

            result = run_orchestrated(
                project_id="test-proj",
                workspace_path=str(workspace),
                dry_run=True,
            )

        assert result["auto_fixed_count"] == 0


# ---------------------------------------------------------------------------
# AC5.6: Report file written with required sections
# ---------------------------------------------------------------------------

class TestReportFile:
    def test_report_written_with_sections(self, tmp_path):
        """AC5.6: Report file contains Summary, Auto-fixable, Human review, Phase detail."""
        workspace = _build_workspace(tmp_path)

        with patch("agent.governance.reconcile_phases.context.phase_scan") as mock_scan, \
             patch("agent.governance.reconcile_phases.context.load_project_graph") as mock_graph:

            mock_scan.return_value = (set(), {})
            mock_graph.return_value = _StubGraph()

            from agent.governance.reconcile_phases.orchestrator import run_orchestrated

            result = run_orchestrated(
                project_id="test-proj",
                workspace_path=str(workspace),
                dry_run=True,
            )

        report_path = workspace / result["report_path"]
        assert report_path.exists(), f"Report not found at {report_path}"

        content = report_path.read_text(encoding="utf-8")
        assert "## Summary" in content
        assert "## Auto-fixable" in content
        assert "## Human review" in content
        assert "## Phase detail blocks" in content
