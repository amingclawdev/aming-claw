"""Tests for Phase D — doc drift heuristic (AC5.1, AC5.2)."""
from __future__ import annotations

import os
import time
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Stub context
# ---------------------------------------------------------------------------

class _StubCtx:
    def __init__(self, workspace_path):
        self.workspace_path = workspace_path
        self.project_id = "test-proj"
        self.graph = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def doc_fixture(tmp_path):
    """Create 3 docs: 1 fresh, 1 stale (ref to missing .py), 1 missing-keyword."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    # Fresh doc — references an existing .py file
    (tmp_path / "agent").mkdir(exist_ok=True)
    (tmp_path / "agent" / "real_module.py").write_text("# real")
    fresh = docs_dir / "fresh.md"
    fresh.write_text(
        "## Overview\n## API\n## Usage\n\nSee `agent/real_module.py` for details.\n"
    )

    # Stale doc — references a .py file that does NOT exist, mtime set old
    stale = docs_dir / "stale.md"
    stale.write_text(
        "## Overview\n## API\n## Usage\n\nSee `agent/deleted_module.py` for details.\n"
    )
    # Set mtime to 30 days ago so it exceeds 14-day grace period
    old_time = time.time() - (30 * 86400)
    os.utime(stale, (old_time, old_time))

    # Missing-keyword doc — no required keywords at all
    missing_kw = docs_dir / "missing_kw.md"
    missing_kw.write_text("Just some random text without required sections.\n")

    return tmp_path


# ---------------------------------------------------------------------------
# AC5.1: Phase D produces ranked drift list with correct types
# ---------------------------------------------------------------------------

class TestPhaseDRun:
    def test_emits_two_discrepancies(self, doc_fixture):
        """AC5.1: 3 docs (1 fresh, 1 stale, 1 missing-keyword) => exactly 2 discrepancies."""
        from agent.governance.reconcile_phases.phase_d import run

        ctx = _StubCtx(str(doc_fixture))
        results = run(ctx)

        types = [d.type for d in results]
        assert "doc_stale" in types, f"Expected doc_stale in {types}"
        assert "doc_missing_known_keyword" in types, f"Expected doc_missing_known_keyword in {types}"
        assert len(results) == 2, f"Expected exactly 2, got {len(results)}: {types}"

    def test_stale_ref_detail(self, doc_fixture):
        """Stale discrepancy detail contains the missing ref path."""
        from agent.governance.reconcile_phases.phase_d import run

        ctx = _StubCtx(str(doc_fixture))
        results = run(ctx)
        stale = [d for d in results if d.type == "doc_stale"]
        assert len(stale) == 1
        assert "deleted_module.py" in stale[0].detail

    def test_missing_keyword_detail(self, doc_fixture):
        """Missing-keyword discrepancy lists which keywords are absent."""
        from agent.governance.reconcile_phases.phase_d import run

        ctx = _StubCtx(str(doc_fixture))
        results = run(ctx)
        mkw = [d for d in results if d.type == "doc_missing_known_keyword"]
        assert len(mkw) == 1
        assert "missing=" in mkw[0].detail

    def test_empty_docs_dir(self, tmp_path):
        """No docs/ dir => empty results, no crash."""
        from agent.governance.reconcile_phases.phase_d import run

        ctx = _StubCtx(str(tmp_path))
        assert run(ctx) == []


# ---------------------------------------------------------------------------
# AC5.2: Phase D NEVER auto-fixes
# ---------------------------------------------------------------------------

class TestPhaseDNoAutoFix:
    def test_no_auto_fix_count(self, doc_fixture):
        """AC5.2: Phase D has no apply function; auto_fix_count is always 0."""
        import agent.governance.reconcile_phases.phase_d as phase_d_mod

        # Phase D module should NOT have an apply_* function
        apply_fns = [name for name in dir(phase_d_mod) if name.startswith("apply")]
        assert apply_fns == [], f"Phase D must not have apply functions, found: {apply_fns}"

    def test_results_are_report_only(self, doc_fixture):
        """Regardless of threshold, results are Discrepancy objects with no mutation hooks."""
        from agent.governance.reconcile_phases.phase_d import run

        ctx = _StubCtx(str(doc_fixture))
        results = run(ctx)
        for d in results:
            assert not hasattr(d, "apply"), "Phase D discrepancies must not have apply method"
