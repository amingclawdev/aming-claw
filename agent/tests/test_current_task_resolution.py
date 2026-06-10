"""Tests for _current_task_row_is_active current-task resolution logic.

Covers:
  1. Active runtime row (runtime_state + current_task_id set) resolves as active.
  2. Timeline-only observer_hold row is not the primary while an active runtime row exists.
  3. manual_fix_in_progress with empty current_task_id is excluded from active pool.

AC: AC-CURRENT-TASK-RUNTIME-BINDING-DOC-AND-STALE-ROW-GUARD-20260608
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_backlog_conn() -> sqlite3.Connection:
    """In-memory SQLite with a minimal backlog_bugs table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE backlog_bugs (
             bug_id TEXT PRIMARY KEY,
             status TEXT DEFAULT 'IN_PROGRESS',
             runtime_state TEXT DEFAULT '',
             chain_stage TEXT DEFAULT '',
             mf_type TEXT DEFAULT '',
             current_task_id TEXT DEFAULT '',
             root_task_id TEXT DEFAULT '',
             worktree_path TEXT DEFAULT '',
             updated_at TEXT DEFAULT '',
             created_at TEXT DEFAULT ''
           )"""
    )
    conn.commit()
    return conn


def _insert_row(conn: sqlite3.Connection, **fields) -> sqlite3.Row:
    bug_id = fields.setdefault("bug_id", "BUG-TEST-1")
    fields.setdefault("status", "IN_PROGRESS")
    fields.setdefault("runtime_state", "")
    fields.setdefault("current_task_id", "")
    fields.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO backlog_bugs ({cols}) VALUES ({placeholders})", list(fields.values()))
    conn.commit()
    return conn.execute(
        "SELECT rowid AS _rowid, * FROM backlog_bugs WHERE bug_id = ?", (bug_id,)
    ).fetchone()


def _get_fn():
    from agent.governance.server import _current_task_row_is_active
    return _current_task_row_is_active


# ---------------------------------------------------------------------------
# Test 1: Active runtime row — runtime_state + current_task_id both set
# ---------------------------------------------------------------------------

class TestActiveRuntimeRow:
    """A row with runtime_state=manual_fix_in_progress AND current_task_id set is active."""

    def test_manual_fix_in_progress_with_task_id_is_active(self):
        conn = _make_backlog_conn()
        row = _insert_row(
            conn,
            bug_id="BUG-ACTIVE-1",
            status="IN_PROGRESS",
            runtime_state="manual_fix_in_progress",
            current_task_id="task-abc123",
        )
        assert _get_fn()(row) is True

    def test_mf_in_progress_variant_with_task_id_is_active(self):
        conn = _make_backlog_conn()
        row = _insert_row(
            conn,
            bug_id="BUG-ACTIVE-2",
            status="IN_PROGRESS",
            runtime_state="mf_in_progress",
            current_task_id="task-xyz999",
        )
        assert _get_fn()(row) is True

    def test_active_backlog_status_resolves_active_regardless_of_runtime_state(self):
        """Rows with status=CLAIMED are active even with no runtime_state."""
        conn = _make_backlog_conn()
        row = _insert_row(
            conn,
            bug_id="BUG-ACTIVE-3",
            status="CLAIMED",
            runtime_state="",
            current_task_id="",
        )
        assert _get_fn()(row) is True

    def test_running_runtime_state_with_task_id_is_active(self):
        conn = _make_backlog_conn()
        row = _insert_row(
            conn,
            bug_id="BUG-ACTIVE-4",
            status="IN_PROGRESS",
            runtime_state="running",
            current_task_id="task-run-001",
        )
        assert _get_fn()(row) is True


# ---------------------------------------------------------------------------
# Test 2: Timeline-only observer_hold — not primary while active runtime row exists
# ---------------------------------------------------------------------------

class TestTimelineOnlyObserverHoldNotPrimary:
    """A row surfaced only via observer_hold timeline event is not the primary
    when a genuine active runtime row also exists.

    _current_task_row_is_active is not itself the arbiter of "primary" vs
    "timeline candidate" — that is done by the callers
    (_current_task_runtime_candidates vs _current_task_timeline_candidate).
    The test here verifies that a row with no runtime_state/current_task_id
    (timeline-only candidate) does NOT pass _current_task_row_is_active,
    confirming the two resolution paths remain separate.
    """

    def test_timeline_only_row_not_active_via_runtime_check(self):
        """Row with no runtime_state / current_task_id and an open-but-non-active
        backlog status is not active per row check.

        A pure timeline candidate has no runtime_state marker — it surfaces only
        via the secondary timeline path (_current_task_timeline_candidate).  Use
        status='OPEN' which is not in _CURRENT_TASK_ACTIVE_BACKLOG_STATUSES and
        has no active runtime marker.
        """
        conn = _make_backlog_conn()
        row = _insert_row(
            conn,
            bug_id="BUG-TIMELINE-1",
            status="OPEN",
            runtime_state="",
            current_task_id="",
        )
        assert _get_fn()(row) is False

    def test_active_runtime_row_passes_while_timeline_only_row_does_not(self):
        """When two rows exist, only the runtime-bound one passes the row check."""
        conn = _make_backlog_conn()
        active_row = _insert_row(
            conn,
            bug_id="BUG-RUNTIME-WINS",
            status="IN_PROGRESS",
            runtime_state="manual_fix_in_progress",
            current_task_id="task-active-99",
        )
        # Timeline-only row: open status, no runtime_state, no current_task_id.
        timeline_only_row = _insert_row(
            conn,
            bug_id="BUG-TIMELINE-ONLY",
            status="OPEN",
            runtime_state="",
            current_task_id="",
        )
        fn = _get_fn()
        assert fn(active_row) is True
        assert fn(timeline_only_row) is False


# ---------------------------------------------------------------------------
# Test 3: manual_fix_in_progress with empty current_task_id — stale, excluded
# ---------------------------------------------------------------------------

class TestStaleManualFixInProgressExcluded:
    """manual_fix_in_progress rows with empty current_task_id are stale and excluded."""

    def test_manual_fix_in_progress_empty_current_task_id_not_active(self):
        """Core stale-row guard: runtime_state set but current_task_id empty -> False."""
        conn = _make_backlog_conn()
        row = _insert_row(
            conn,
            bug_id="BUG-STALE-1",
            status="IN_PROGRESS",
            runtime_state="manual_fix_in_progress",
            current_task_id="",
        )
        assert _get_fn()(row) is False

    def test_mf_in_progress_empty_current_task_id_not_active(self):
        conn = _make_backlog_conn()
        row = _insert_row(
            conn,
            bug_id="BUG-STALE-2",
            status="IN_PROGRESS",
            runtime_state="mf_in_progress",
            current_task_id="",
        )
        assert _get_fn()(row) is False

    def test_stale_row_excluded_from_single_active_count(self):
        """Stale rows must not inflate the active-row count; only bound rows count."""
        conn = _make_backlog_conn()
        stale = _insert_row(
            conn,
            bug_id="BUG-STALE-COUNT-1",
            status="IN_PROGRESS",
            runtime_state="manual_fix_in_progress",
            current_task_id="",
        )
        active = _insert_row(
            conn,
            bug_id="BUG-ACTIVE-COUNT-1",
            status="IN_PROGRESS",
            runtime_state="manual_fix_in_progress",
            current_task_id="task-bound-001",
        )
        fn = _get_fn()
        active_count = sum(1 for r in [stale, active] if fn(r))
        assert active_count == 1, (
            f"Expected 1 active row, got {active_count}; "
            "stale manual_fix_in_progress row must not count"
        )

    def test_closed_status_always_excluded_regardless_of_runtime_state(self):
        """CANCELLED rows with runtime_state set are never active (closed wins)."""
        conn = _make_backlog_conn()
        row = _insert_row(
            conn,
            bug_id="BUG-CANCELLED-1",
            status="CANCELLED",
            runtime_state="manual_fix_in_progress",
            current_task_id="task-orphan",
        )
        assert _get_fn()(row) is False

    def test_whitespace_only_current_task_id_treated_as_empty(self):
        """A current_task_id containing only whitespace is treated as absent."""
        conn = _make_backlog_conn()
        row = _insert_row(
            conn,
            bug_id="BUG-STALE-WS",
            status="IN_PROGRESS",
            runtime_state="manual_fix_in_progress",
            current_task_id="   ",
        )
        assert _get_fn()(row) is False
