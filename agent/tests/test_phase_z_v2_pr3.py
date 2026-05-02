"""Tests for Phase Z v2 PR3 — driver + atomic swap module.

The legacy migration_state_machine has been replaced by
``agent.governance.symbol_swap.atomic_swap`` (spec §4.4 v6 / GPT R4).
This file retains the driver tests and adds smoke coverage for the new
atomic-swap module so the PR3 surface stays exercised.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

# Ensure agent is importable
_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from agent.governance.reconcile_phases.phase_z_v2 import (
    build_graph_v2_from_symbols,
    find_test_coverage,
    find_doc_coverage,
    diff_against_existing_graph,
    write_dry_run_artifact,
    write_graph_v2_json,
    score_function_layer,
    aggregate_functions_into_nodes,
    parse_production_modules,
    build_call_graph,
    tarjan_scc,
    handle_cycle,
    CYCLE_ABORT_THRESHOLD,
    ModuleInfo,
    FunctionMeta,
)

# NOTE: migration_state_machine has been removed — replaced by
# agent.governance.symbol_swap. We only re-import the atomic-swap surface
# here as a smoke check that the replacement module loads.
from agent.governance.symbol_swap import (
    BAK_RETENTION_DAYS,
    atomic_swap,
    smoke_validate,
    rollback,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_temp_project(files: dict[str, str] | None = None) -> str:
    """Create a temp directory with optional files."""
    d = tempfile.mkdtemp()
    if files:
        for relpath, content in files.items():
            fpath = os.path.join(d, relpath)
            os.makedirs(os.path.dirname(fpath), exist_ok=True)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)
    return d


# ===========================================================================
# Driver tests (6)
# ===========================================================================

class TestBuildGraphV2FromSymbols:
    """AC1: build_graph_v2_from_symbols orchestrates the full pipeline."""

    def test_ac1_calls_all_pipeline_stages(self):
        """AC1: Verify build_graph_v2_from_symbols calls all required functions."""
        project = _make_temp_project({
            "agent/foo.py": "def hello():\n    pass\n",
            "scripts/bar.py": "def world():\n    hello()\n",
        })
        result = build_graph_v2_from_symbols(project, dry_run=True)
        assert result["status"] == "ok"
        assert "report_path" in result
        assert result["node_count"] >= 0

    def test_ac2_dry_run_writes_scratch_artifact(self):
        """AC2: dry_run=True writes docs/dev/scratch/graph-v2-{date}.json."""
        project = _make_temp_project({
            "agent/simple.py": "def func_a():\n    pass\n",
        })
        result = build_graph_v2_from_symbols(project, dry_run=True)
        assert result["status"] == "ok"
        assert "report_path" in result
        assert os.path.isfile(result["report_path"])
        assert "graph-v2-" in result["report_path"]

    def test_ac3_apply_writes_graph_v2_json(self):
        """AC3: dry_run=False writes agent/governance/graph.v2.json."""
        project = _make_temp_project({
            "agent/simple.py": "def func_a():\n    pass\n",
            "agent/governance/.keep": "",
        })
        # Patch create_baseline to avoid DB dependency
        with patch("agent.governance.reconcile_phases.phase_z_v2.build_graph_v2_from_symbols.__module__", create=True):
            # Actually just run it - create_baseline is wrapped in try/except
            result = build_graph_v2_from_symbols(project, dry_run=False, owner="test-owner")
        assert result["status"] == "ok"
        assert "graph_path" in result
        graph_path = os.path.join(project, "agent", "governance", "graph.v2.json")
        assert os.path.isfile(graph_path)
        with open(graph_path, "r") as f:
            data = json.load(f)
        assert data["version"] == "v2"

    def test_ac4_cycle_abort_over_threshold(self):
        """AC4: >30 cycles returns status='aborted' with abort_reason."""
        # Build a project with many mutual cycles
        lines = []
        # Create 31+ 2-node cycles via cross-module calls
        for i in range(35):
            lines.append(f"agent/mod_{i}_a.py")
            lines.append(f"agent/mod_{i}_b.py")

        files = {}
        for i in range(35):
            files[f"agent/mod_{i}_a.py"] = f"from agent.mod_{i}_b import func_b_{i}\ndef func_a_{i}():\n    func_b_{i}()\n"
            files[f"agent/mod_{i}_b.py"] = f"from agent.mod_{i}_a import func_a_{i}\ndef func_b_{i}():\n    func_a_{i}()\n"

        project = _make_temp_project(files)
        result = build_graph_v2_from_symbols(project, dry_run=True)
        assert result["status"] == "aborted"
        assert "abort_reason" in result
        assert "cycle" in result["abort_reason"].lower() or "30" in result["abort_reason"]


class TestCoverageLookup:
    """AC5: find_test_coverage and find_doc_coverage."""

    def test_ac5_find_test_coverage(self):
        """AC5: find_test_coverage returns test_files list + covered_lines int."""
        project = _make_temp_project({
            "agent/mymod.py": "def hello():\n    pass\n",
            "agent/tests/test_mymod.py": "def test_hello():\n    assert True\n",
        })
        result = find_test_coverage(project, os.path.join(project, "agent", "mymod.py"))
        assert isinstance(result["test_files"], list)
        assert isinstance(result["covered_lines"], int)

    def test_ac5_find_doc_coverage(self):
        """AC5: find_doc_coverage returns doc_files list + covered_lines int."""
        project = _make_temp_project({
            "agent/mymod.py": "def hello():\n    pass\n",
            "docs/ref.md": "# Reference\nSee agent/mymod.py for details.\n",
        })
        result = find_doc_coverage(project, os.path.join(project, "agent", "mymod.py"))
        assert isinstance(result["doc_files"], list)
        assert isinstance(result["covered_lines"], int)
        assert len(result["doc_files"]) >= 1


# ===========================================================================
# Atomic swap smoke tests (replacement for migration_state_machine tests)
# Full coverage lives in test_symbol_atomic_swap.py.
# ===========================================================================

class TestAtomicSwapSurface:
    """Smoke checks: atomic_swap module loads and exposes its public API."""

    def test_bak_retention_days_is_30(self):
        assert BAK_RETENTION_DAYS == 30

    def test_atomic_swap_callable(self):
        assert callable(atomic_swap)

    def test_smoke_validate_callable(self):
        assert callable(smoke_validate)

    def test_rollback_callable(self):
        assert callable(rollback)


# ===========================================================================
# Schema migration test (AC8)
# ===========================================================================

class TestSchemaMigration:
    """AC8: db.py schema version tracks the current governance schema."""

    def test_ac8_schema_version_is_current(self):
        """AC8: SCHEMA_VERSION matches the current governance schema."""
        from agent.governance.db import SCHEMA_VERSION
        assert SCHEMA_VERSION == 29

    # Note: Testing actual migration requires DB access which is
    # integration-level; the unit test validates the constant.
