"""Tests for Phase G — chain closure invariant checker.

Covers AC-G1 through AC-G4.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Optional
from unittest.mock import patch, MagicMock

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


class _StubCtx:
    """Minimal ReconcileContext stub for Phase G."""

    def __init__(
        self,
        *,
        project_id: str = "test-proj",
        workspace_path: str = "/tmp/test",
        options: Optional[dict] = None,
        pm_tasks: Optional[list] = None,
        chain_events_map: Optional[dict] = None,
    ):
        self.project_id = project_id
        self.workspace_path = workspace_path
        self.options = options or {}
        self._pm_tasks = pm_tasks or []
        self._chain_events_map = chain_events_map or {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _now():
    return datetime.now(tz=timezone.utc)


def _make_pm_task(task_id, status="succeeded", age_days=10):
    """Create a PM task dict with created_at = now - age_days."""
    created = _now() - timedelta(days=age_days)
    return {
        "task_id": task_id,
        "type": "pm",
        "status": status,
        "project_id": "test-proj",
        "created_at": created.isoformat(),
    }


def _run_phase_g(ctx):
    """Call phase_g.run with data loaders patched to use stub ctx."""
    from agent.governance.reconcile_phases import phase_g
    import agent.governance.reconcile_phases as rp

    original = rp.Discrepancy
    rp.Discrepancy = _Discrepancy
    try:
        with patch.object(phase_g, "_load_pm_tasks", return_value=ctx._pm_tasks):
            with patch.object(phase_g, "_load_chain_events",
                              side_effect=lambda c, tid: ctx._chain_events_map.get(tid, [])):
                return phase_g.run(ctx)
    finally:
        rp.Discrepancy = original


# ---------------------------------------------------------------------------
# AC-G1: PM task with status='succeeded', age > N_DAYS, no merge.completed
#         → emits Discrepancy(type='chain_not_closed'), confidence computed
# ---------------------------------------------------------------------------

class TestACG1:

    def test_unclosed_chain_emits_discrepancy(self):
        """PM task succeeded, age > 7 days, no merge.completed → flagged."""
        ctx = _StubCtx(
            pm_tasks=[_make_pm_task("pm-001", status="succeeded", age_days=10)],
            chain_events_map={
                "pm-001": [
                    {"event_type": "pm.created", "created_at": "2026-01-01T00:00:00Z"},
                    {"event_type": "dev.completed", "created_at": "2026-01-02T00:00:00Z"},
                ],
            },
        )
        results = _run_phase_g(ctx)

        assert len(results) == 1
        d = results[0]
        assert d.type == "chain_not_closed"
        assert "pm-001" in d.detail
        assert "cancel_or_resume" in d.detail
        assert d.confidence == "medium"  # 10 days, N_DAYS*2=14 → medium

    def test_high_confidence_when_very_old(self):
        """PM task age > N_DAYS*2 → confidence='high'."""
        ctx = _StubCtx(
            pm_tasks=[_make_pm_task("pm-002", status="succeeded", age_days=20)],
            chain_events_map={"pm-002": []},
        )
        results = _run_phase_g(ctx)

        assert len(results) == 1
        assert results[0].confidence == "high"  # 20 > 14

    def test_medium_confidence_when_moderately_old(self):
        """PM task age between N_DAYS and N_DAYS*2 → confidence='medium'."""
        ctx = _StubCtx(
            pm_tasks=[_make_pm_task("pm-003", status="succeeded", age_days=10)],
            chain_events_map={"pm-003": []},
        )
        results = _run_phase_g(ctx)

        assert len(results) == 1
        assert results[0].confidence == "medium"

    def test_merge_completed_not_flagged(self):
        """PM task with merge.completed in chain events → NOT flagged."""
        ctx = _StubCtx(
            pm_tasks=[_make_pm_task("pm-004", status="succeeded", age_days=20)],
            chain_events_map={
                "pm-004": [
                    {"event_type": "pm.created", "created_at": "2026-01-01T00:00:00Z"},
                    {"event_type": "merge.completed", "created_at": "2026-01-05T00:00:00Z"},
                ],
            },
        )
        results = _run_phase_g(ctx)
        assert len(results) == 0

    def test_detail_includes_required_fields(self):
        """Discrepancy detail includes pm_task_id, age_days, last_event_type, last_event_at, suggested_action."""
        ctx = _StubCtx(
            pm_tasks=[_make_pm_task("pm-005", status="succeeded", age_days=10)],
            chain_events_map={
                "pm-005": [
                    {"event_type": "dev.completed", "created_at": "2026-01-02T12:00:00Z"},
                ],
            },
        )
        results = _run_phase_g(ctx)

        assert len(results) == 1
        d = results[0]
        assert "pm_task_id=pm-005" in d.detail
        assert "age_days=" in d.detail
        assert "last_event_type=dev.completed" in d.detail
        assert "last_event_at=2026-01-02T12:00:00Z" in d.detail
        assert "suggested_action=cancel_or_resume" in d.detail

    def test_no_chain_events_shows_none(self):
        """PM task with no chain events → last_event_type=None."""
        ctx = _StubCtx(
            pm_tasks=[_make_pm_task("pm-006", status="succeeded", age_days=10)],
            chain_events_map={"pm-006": []},
        )
        results = _run_phase_g(ctx)

        assert len(results) == 1
        assert "last_event_type=None" in results[0].detail


# ---------------------------------------------------------------------------
# AC-G2: PM task with status in ('cancelled','failed') → NOT flagged
# ---------------------------------------------------------------------------

class TestACG2:

    def test_cancelled_not_flagged(self):
        """PM task with status='cancelled' → NOT flagged."""
        ctx = _StubCtx(
            pm_tasks=[_make_pm_task("pm-c1", status="cancelled", age_days=20)],
            chain_events_map={"pm-c1": []},
        )
        results = _run_phase_g(ctx)
        assert len(results) == 0

    def test_failed_not_flagged(self):
        """PM task with status='failed' → NOT flagged."""
        ctx = _StubCtx(
            pm_tasks=[_make_pm_task("pm-f1", status="failed", age_days=20)],
            chain_events_map={"pm-f1": []},
        )
        results = _run_phase_g(ctx)
        assert len(results) == 0

    def test_cancelled_regardless_of_age(self):
        """Cancelled even if very old → NOT flagged."""
        ctx = _StubCtx(
            pm_tasks=[_make_pm_task("pm-c2", status="cancelled", age_days=60)],
            chain_events_map={"pm-c2": []},
        )
        # age_days=60 but capped at 30-day lookback in real loader;
        # but our stub bypasses that, so we just verify the status filter
        results = _run_phase_g(ctx)
        assert len(results) == 0


# ---------------------------------------------------------------------------
# AC-G3: PM task with age < N_DAYS → NOT flagged
# ---------------------------------------------------------------------------

class TestACG3:

    def test_young_task_not_flagged(self):
        """PM task younger than 7 days → NOT flagged even with no merge.completed."""
        ctx = _StubCtx(
            pm_tasks=[_make_pm_task("pm-y1", status="succeeded", age_days=3)],
            chain_events_map={"pm-y1": []},
        )
        results = _run_phase_g(ctx)
        assert len(results) == 0

    def test_exactly_n_days_not_flagged(self):
        """PM task created exactly N_DAYS ago (boundary) should still be within window.

        Since we use created_at > cutoff (now - N_DAYS), a task created exactly
        N_DAYS ago has created_at == cutoff, so created_at > cutoff is False.
        The task is NOT younger → it IS checked.
        """
        # This is a boundary test; the task at exactly N_DAYS is checked.
        ctx = _StubCtx(
            pm_tasks=[_make_pm_task("pm-exact", status="succeeded", age_days=7)],
            chain_events_map={"pm-exact": []},
        )
        results = _run_phase_g(ctx)
        # At exactly 7 days the task passes the cutoff filter and IS checked.
        assert len(results) == 1

    def test_custom_n_days(self):
        """Custom N_DAYS=14 → task at 10 days not flagged."""
        ctx = _StubCtx(
            pm_tasks=[_make_pm_task("pm-cn1", status="succeeded", age_days=10)],
            chain_events_map={"pm-cn1": []},
            options={"phase_g_n_days": 14},
        )
        results = _run_phase_g(ctx)
        assert len(results) == 0

    def test_custom_n_days_flags_old_enough(self):
        """Custom N_DAYS=5 → task at 10 days IS flagged."""
        ctx = _StubCtx(
            pm_tasks=[_make_pm_task("pm-cn2", status="succeeded", age_days=10)],
            chain_events_map={"pm-cn2": []},
            options={"phase_g_n_days": 5},
        )
        results = _run_phase_g(ctx)
        assert len(results) == 1
        # N_DAYS=5, age≈10, N_DAYS*2=10 → age slightly > 10 due to timing → high
        assert results[0].confidence == "high"


# ---------------------------------------------------------------------------
# AC-G4: apply_phase_g_mutations is always a no-op (auto_fixed_count==0)
# ---------------------------------------------------------------------------

class TestACG4:

    def test_mutations_noop_dry_run_true(self):
        """dry_run=True → applied=0, skipped=len(discrepancies)."""
        from agent.governance.reconcile_phases.phase_g import apply_phase_g_mutations

        ctx = _StubCtx()
        discs = [
            _Discrepancy("chain_not_closed", None, None, "pm_task_id=pm-001", "high"),
            _Discrepancy("chain_not_closed", None, None, "pm_task_id=pm-002", "medium"),
        ]
        result = apply_phase_g_mutations(ctx, discs, dry_run=True)
        assert result["applied"] == 0
        assert result["skipped"] == 2

    def test_mutations_noop_dry_run_false(self):
        """dry_run=False → STILL applied=0 (Phase G never mutates)."""
        from agent.governance.reconcile_phases.phase_g import apply_phase_g_mutations

        ctx = _StubCtx()
        discs = [
            _Discrepancy("chain_not_closed", None, None, "pm_task_id=pm-001", "high"),
        ]
        result = apply_phase_g_mutations(ctx, discs, dry_run=False, threshold="high")
        assert result["applied"] == 0
        assert result["skipped"] == 1

    def test_mutations_noop_empty(self):
        """No discrepancies → applied=0, skipped=0."""
        from agent.governance.reconcile_phases.phase_g import apply_phase_g_mutations

        ctx = _StubCtx()
        result = apply_phase_g_mutations(ctx, [], dry_run=False)
        assert result["applied"] == 0
        assert result["skipped"] == 0


# ---------------------------------------------------------------------------
# Orchestrator registration tests
# ---------------------------------------------------------------------------

class TestOrchestratorRegistration:

    def test_g_in_phase_order(self):
        """Phase G is registered in PHASE_ORDER after F."""
        from agent.governance.reconcile_phases.orchestrator import PHASE_ORDER
        assert "G" in PHASE_ORDER
        f_idx = PHASE_ORDER.index("F")
        g_idx = PHASE_ORDER.index("G")
        assert g_idx > f_idx

    def test_g_in_exports(self):
        """phase_g is exported from __init__.py."""
        from agent.governance.reconcile_phases import phase_g
        assert hasattr(phase_g, "run")
        assert hasattr(phase_g, "apply_phase_g_mutations")

    def test_g_dispatched_by_run_phase(self):
        """_run_phase dispatches 'G' to phase_g.run."""
        from agent.governance.reconcile_phases.orchestrator import _run_phase

        mock_ctx = _StubCtx()
        with patch(
            "agent.governance.reconcile_phases.phase_g.run",
            return_value=[],
        ) as mock_run:
            result = _run_phase("G", mock_ctx, {})
            mock_run.assert_called_once_with(mock_ctx, scope=None)
            assert result == []


# ---------------------------------------------------------------------------
# Multiple PM tasks
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# AC6: test_load_chain_events_uses_ts_column
# ---------------------------------------------------------------------------

class TestLoadChainEventsTsColumn:

    def test_load_chain_events_uses_ts_column(self):
        """Insert 2 rows with different ts values, call load_chain_events,
        assert 2 rows returned in ascending ts order."""
        import sqlite3
        from agent.governance.reconcile_phases.phase_g import _load_chain_events

        # Create in-memory DB with chain_events table matching real schema
        mem_conn = sqlite3.connect(":memory:")
        mem_conn.execute(
            "CREATE TABLE chain_events ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  root_task_id TEXT,"
            "  task_id TEXT,"
            "  event_type TEXT,"
            "  payload_json TEXT,"
            "  ts TEXT"
            ")"
        )
        # Insert rows with ts out of order to prove ORDER BY ts works
        mem_conn.execute(
            "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("task-abc", "task-abc", "dev.started", "{}", "2026-01-02T00:00:00Z"),
        )
        mem_conn.execute(
            "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("task-abc", "task-abc", "pm.created", "{}", "2026-01-01T00:00:00Z"),
        )
        mem_conn.commit()

        ctx = _StubCtx(project_id="test-proj")

        with patch("agent.governance.db.get_connection", return_value=mem_conn):
            results = _load_chain_events(ctx, "task-abc")

        assert len(results) == 2
        # First row should be the earlier ts (pm.created at Jan 1)
        assert results[0]["event_type"] == "pm.created"
        assert results[0]["ts"] == "2026-01-01T00:00:00Z"
        # Second row should be the later ts (dev.started at Jan 2)
        assert results[1]["event_type"] == "dev.started"
        assert results[1]["ts"] == "2026-01-02T00:00:00Z"


# ---------------------------------------------------------------------------
# Multiple PM tasks
# ---------------------------------------------------------------------------

class TestMultipleTasks:

    def test_mixed_tasks(self):
        """Mix of terminal, young, old-unclosed, and old-closed tasks."""
        ctx = _StubCtx(
            pm_tasks=[
                _make_pm_task("pm-cancelled", status="cancelled", age_days=15),
                _make_pm_task("pm-failed", status="failed", age_days=15),
                _make_pm_task("pm-young", status="succeeded", age_days=3),
                _make_pm_task("pm-closed", status="succeeded", age_days=15),
                _make_pm_task("pm-unclosed", status="succeeded", age_days=15),
            ],
            chain_events_map={
                "pm-cancelled": [],
                "pm-failed": [],
                "pm-young": [],
                "pm-closed": [
                    {"event_type": "merge.completed", "created_at": "2026-01-05T00:00:00Z"},
                ],
                "pm-unclosed": [
                    {"event_type": "dev.completed", "created_at": "2026-01-03T00:00:00Z"},
                ],
            },
        )
        results = _run_phase_g(ctx)

        # Only pm-unclosed should be flagged
        assert len(results) == 1
        assert "pm-unclosed" in results[0].detail

    def test_no_pm_tasks(self):
        """No PM tasks → empty results."""
        ctx = _StubCtx(pm_tasks=[])
        results = _run_phase_g(ctx)
        assert len(results) == 0
