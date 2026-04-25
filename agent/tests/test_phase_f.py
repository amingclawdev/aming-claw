"""Tests for Phase F — verify_status freshness checker.

Covers AC-F1 through AC-F4.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Minimal stubs (avoid importing real governance modules)
# ---------------------------------------------------------------------------

@dataclass
class _Discrepancy:
    type: str
    node_id: Optional[str]
    field: Optional[str]
    detail: str
    confidence: str


class _StubGraph:
    """Minimal AcceptanceGraph stub."""

    def __init__(self, nodes: dict):
        self._nodes = nodes

    def list_nodes(self):
        return list(self._nodes.keys())

    def get_node(self, node_id):
        if node_id not in self._nodes:
            raise KeyError(node_id)
        return dict(self._nodes[node_id])


class _StubCtx:
    """Minimal ReconcileContext stub."""

    def __init__(
        self,
        *,
        project_id="test-proj",
        workspace_path="/tmp/test",
        node_state=None,
        graph=None,
        options=None,
        git_dates=None,
    ):
        self.project_id = project_id
        self.workspace_path = workspace_path
        self.node_state = node_state or {}
        self.graph = graph
        self.options = options or {}
        self._git_dates = git_dates or {}

    def git_log_per_file_last_commit_date(self, file_path):
        return self._git_dates.get(file_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _make_node_state(verify_status, updated_at):
    return {
        "verify_status": verify_status,
        "updated_at": updated_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# AC-F1: qa_pass node with primary file modified >7d after node updated_at
#         emits verify_status_stale with confidence=high, action=revert_to_pending
# ---------------------------------------------------------------------------

class TestACF1:

    def test_high_confidence_stale_node(self):
        """Node with qa_pass + file modified 10 days after updated_at → high."""
        from agent.governance.reconcile_phases.phase_f import run

        node_updated = _utc(2026, 1, 1)
        file_mtime = _utc(2026, 1, 12)  # 11 days later

        ctx = _StubCtx(
            graph=_StubGraph({"L1.1": {"primary": ["agent/foo.py"]}}),
            node_state={"L1.1": _make_node_state("qa_pass", node_updated)},
            git_dates={"agent/foo.py": file_mtime},
        )

        results = _run_phase_f(ctx)

        assert len(results) == 1
        d = results[0]
        assert d.type == "verify_status_stale"
        assert d.node_id == "L1.1"
        assert d.confidence == "high"
        assert "revert_to_pending" in d.detail

    def test_medium_confidence_stale_node(self):
        """Node with qa_pass + file modified 3 days after updated_at → medium."""
        node_updated = _utc(2026, 1, 1)
        file_mtime = _utc(2026, 1, 5)  # 4 days later (>1 grace, <=7)

        ctx = _StubCtx(
            graph=_StubGraph({"L2.1": {"primary": ["agent/bar.py"]}}),
            node_state={"L2.1": _make_node_state("qa_pass", node_updated)},
            git_dates={"agent/bar.py": file_mtime},
        )

        results = _run_phase_f(ctx)

        assert len(results) == 1
        d = results[0]
        assert d.type == "verify_status_stale"
        assert d.confidence == "medium"
        assert "flag_for_review" in d.detail

    def test_non_qa_pass_nodes_skipped(self):
        """Nodes with verify_status != 'qa_pass' are skipped."""
        node_updated = _utc(2026, 1, 1)
        file_mtime = _utc(2026, 1, 20)  # Very stale

        ctx = _StubCtx(
            graph=_StubGraph({"L1.1": {"primary": ["agent/foo.py"]}}),
            node_state={"L1.1": _make_node_state("pending", node_updated)},
            git_dates={"agent/foo.py": file_mtime},
        )

        results = _run_phase_f(ctx)
        assert len(results) == 0

    def test_no_graph_returns_empty(self):
        """No graph → empty results."""
        ctx = _StubCtx(graph=None)
        results = _run_phase_f(ctx)
        assert len(results) == 0


# ---------------------------------------------------------------------------
# AC-F2: GRACE_PERIOD default 1d, configurable via ctx.options
# ---------------------------------------------------------------------------

class TestACF2:

    def test_default_grace_period_no_discrepancy(self):
        """File modified 0.5 days after updated_at (within 1d grace) → no discrepancy."""
        node_updated = _utc(2026, 1, 1, 0, 0)
        file_mtime = _utc(2026, 1, 1, 12, 0)  # 12 hours later

        ctx = _StubCtx(
            graph=_StubGraph({"L1.1": {"primary": ["agent/foo.py"]}}),
            node_state={"L1.1": _make_node_state("qa_pass", node_updated)},
            git_dates={"agent/foo.py": file_mtime},
        )

        results = _run_phase_f(ctx)
        assert len(results) == 0

    def test_custom_grace_period_suppresses(self):
        """With grace_period_days=5, file modified 3 days later → no discrepancy."""
        node_updated = _utc(2026, 1, 1)
        file_mtime = _utc(2026, 1, 4)  # 3 days later

        ctx = _StubCtx(
            graph=_StubGraph({"L1.1": {"primary": ["agent/foo.py"]}}),
            node_state={"L1.1": _make_node_state("qa_pass", node_updated)},
            git_dates={"agent/foo.py": file_mtime},
            options={"grace_period_days": 5},
        )

        results = _run_phase_f(ctx)
        assert len(results) == 0

    def test_custom_grace_period_allows_detection(self):
        """With grace_period_days=2, file modified 5 days later → detected."""
        node_updated = _utc(2026, 1, 1)
        file_mtime = _utc(2026, 1, 6)  # 5 days later

        ctx = _StubCtx(
            graph=_StubGraph({"L1.1": {"primary": ["agent/foo.py"]}}),
            node_state={"L1.1": _make_node_state("qa_pass", node_updated)},
            git_dates={"agent/foo.py": file_mtime},
            options={"grace_period_days": 2},
        )

        results = _run_phase_f(ctx)
        assert len(results) == 1
        assert results[0].confidence == "medium"  # 5 days, <=7 → medium


# ---------------------------------------------------------------------------
# AC-F3: apply_phase_f_mutations dry_run/live behavior, no sqlite3
# ---------------------------------------------------------------------------

class TestACF3:

    def test_dry_run_no_http_calls(self):
        """dry_run=True → no HTTP calls made."""
        from agent.governance.reconcile_phases.phase_f import apply_phase_f_mutations

        ctx = _StubCtx()
        discrepancies = [
            _Discrepancy(
                type="verify_status_stale",
                node_id="L1.1",
                field="primary",
                detail="file=agent/foo.py days_stale=10.0 suggested_action=revert_to_pending",
                confidence="high",
            ),
        ]

        with patch("agent.governance.reconcile_phases.phase_f.urllib.request.urlopen") as mock_urlopen:
            result = apply_phase_f_mutations(ctx, discrepancies, dry_run=True)

        mock_urlopen.assert_not_called()
        assert result["applied"] == 0
        assert result["skipped"] == 1

    def test_live_calls_verify_update(self):
        """dry_run=False → POST /api/wf/{pid}/verify-update called."""
        from agent.governance.reconcile_phases.phase_f import apply_phase_f_mutations

        ctx = _StubCtx(project_id="my-proj")
        discrepancies = [
            _Discrepancy(
                type="verify_status_stale",
                node_id="L1.1",
                field="primary",
                detail="file=agent/foo.py days_stale=10.0 suggested_action=revert_to_pending",
                confidence="high",
            ),
        ]

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("agent.governance.reconcile_phases.phase_f.urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            result = apply_phase_f_mutations(ctx, discrepancies, dry_run=False, threshold="high")

        mock_urlopen.assert_called_once()
        req_obj = mock_urlopen.call_args[0][0]
        assert "my-proj" in req_obj.full_url
        assert "verify-update" in req_obj.full_url
        assert req_obj.method == "POST"

        body = json.loads(req_obj.data.decode("utf-8"))
        assert body["status"] == "pending"
        assert "L1.1" in body["nodes"]

        assert result["applied"] == 1

    def test_medium_skipped_with_high_threshold(self):
        """threshold='high' skips medium-confidence discrepancies."""
        from agent.governance.reconcile_phases.phase_f import apply_phase_f_mutations

        ctx = _StubCtx()
        discrepancies = [
            _Discrepancy(
                type="verify_status_stale",
                node_id="L2.1",
                field="primary",
                detail="file=agent/bar.py days_stale=5.0 suggested_action=flag_for_review",
                confidence="medium",
            ),
        ]

        with patch("agent.governance.reconcile_phases.phase_f.urllib.request.urlopen") as mock_urlopen:
            result = apply_phase_f_mutations(ctx, discrepancies, dry_run=False, threshold="high")

        mock_urlopen.assert_not_called()
        assert result["skipped"] == 1

    def test_no_sqlite3_import(self):
        """Phase F must not import sqlite3."""
        import importlib
        import agent.governance.reconcile_phases.phase_f as pf

        source = importlib.util.find_spec("agent.governance.reconcile_phases.phase_f")
        if source and source.origin:
            with open(source.origin, "r") as f:
                content = f.read()
            assert "import sqlite3" not in content
            assert "sqlite3.connect" not in content


# ---------------------------------------------------------------------------
# AC-F4: Nodes with all primary files in Phase A unmapped_files → skipped
# ---------------------------------------------------------------------------

class TestACF4:

    def test_all_primary_unmapped_skipped(self):
        """Node whose primary files are ALL in Phase A unmapped → no discrepancy."""
        node_updated = _utc(2026, 1, 1)
        file_mtime = _utc(2026, 1, 20)  # Very stale

        phase_a_discs = [
            _Discrepancy(type="unmapped_file", node_id=None, field=None,
                         detail="agent/deleted.py", confidence="low"),
        ]

        ctx = _StubCtx(
            graph=_StubGraph({"L1.1": {"primary": ["agent/deleted.py"]}}),
            node_state={"L1.1": _make_node_state("qa_pass", node_updated)},
            git_dates={"agent/deleted.py": file_mtime},
        )

        results = _run_phase_f(ctx, phase_a_discrepancies=phase_a_discs)
        assert len(results) == 0

    def test_partial_unmapped_not_skipped(self):
        """Node with some (not all) primary files unmapped → still checked."""
        node_updated = _utc(2026, 1, 1)
        file_mtime = _utc(2026, 1, 20)  # Very stale

        phase_a_discs = [
            _Discrepancy(type="unmapped_file", node_id=None, field=None,
                         detail="agent/deleted.py", confidence="low"),
        ]

        ctx = _StubCtx(
            graph=_StubGraph({"L1.1": {"primary": ["agent/deleted.py", "agent/alive.py"]}}),
            node_state={"L1.1": _make_node_state("qa_pass", node_updated)},
            git_dates={"agent/alive.py": file_mtime},
        )

        results = _run_phase_f(ctx, phase_a_discrepancies=phase_a_discs)
        assert len(results) == 1
        assert results[0].confidence == "high"

    def test_no_phase_a_discrepancies(self):
        """When phase_a_discrepancies is None, no filtering happens."""
        node_updated = _utc(2026, 1, 1)
        file_mtime = _utc(2026, 1, 20)

        ctx = _StubCtx(
            graph=_StubGraph({"L1.1": {"primary": ["agent/foo.py"]}}),
            node_state={"L1.1": _make_node_state("qa_pass", node_updated)},
            git_dates={"agent/foo.py": file_mtime},
        )

        results = _run_phase_f(ctx, phase_a_discrepancies=None)
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Orchestrator registration test
# ---------------------------------------------------------------------------

class TestOrchestratorRegistration:

    def test_f_in_phase_order(self):
        """Phase F is registered in PHASE_ORDER after D."""
        from agent.governance.reconcile_phases.orchestrator import PHASE_ORDER
        assert "F" in PHASE_ORDER
        d_idx = PHASE_ORDER.index("D")
        f_idx = PHASE_ORDER.index("F")
        assert f_idx > d_idx

    def test_f_in_exports(self):
        """phase_f is exported from __init__.py."""
        from agent.governance.reconcile_phases import phase_f
        assert hasattr(phase_f, "run")
        assert hasattr(phase_f, "apply_phase_f_mutations")


# ---------------------------------------------------------------------------
# Helper: run phase_f.run with Discrepancy patched
# ---------------------------------------------------------------------------

def _run_phase_f(ctx, phase_a_discrepancies=None):
    """Call phase_f.run with Discrepancy patched to our stub."""
    from agent.governance.reconcile_phases import phase_f

    with patch.object(
        phase_f,
        "__builtins__",
        phase_f.__builtins__ if hasattr(phase_f, "__builtins__") else {},
    ):
        pass

    # Patch the Discrepancy import inside run()
    import agent.governance.reconcile_phases as rp
    original = rp.Discrepancy
    rp.Discrepancy = _Discrepancy
    try:
        return phase_f.run(ctx, phase_a_discrepancies=phase_a_discrepancies)
    finally:
        rp.Discrepancy = original
