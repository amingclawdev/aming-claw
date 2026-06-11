"""Tests for current-stream binding selection logic.

Reproduces the live shape from AC-CURRENT-STREAM-BINDING-ACTIVE-LANE-SELECTION-20260610:

  (i)  event-less active row + actively-evidencing lane → binding picks the evidencing lane
  (ii) two evidencing lanes → most recent evidence wins
  (iii) zero-event + stale-heartbeat candidate ages out (TTL)
  (iv) competing-candidates metadata present in computation result

All tests are pure unit tests that exercise the private functions directly;
no running governance server or real SQLite file is required.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest agent/tests/test_current_stream_binding.py -q
or:
    python3 -m unittest agent.tests.test_current_stream_binding -v
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_repo = Path(__file__).resolve().parents[2]
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))

from agent.governance.server import (
    _CURRENT_TASK_ZERO_EVENT_STALE_TTL_SECS,
    _current_task_candidate_is_aged_out,
    _current_task_candidate_sort_key,
    _current_task_competing_candidates_metadata,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(delta_seconds: int = 0) -> str:
    """ISO timestamp relative to now."""
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()


def _make_candidate(
    bug_id: str,
    evidence_at: str,
    event_id: int,
    rowid: int,
    task_id: str = "",
) -> dict[str, Any]:
    """Build a minimal candidate dict like _current_task_runtime_candidates produces."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE backlog_bugs (
             bug_id TEXT PRIMARY KEY,
             status TEXT DEFAULT 'IN_PROGRESS',
             runtime_state TEXT DEFAULT 'manual_fix_in_progress',
             current_task_id TEXT DEFAULT '',
             updated_at TEXT DEFAULT ''
           )"""
    )
    conn.execute(
        "INSERT INTO backlog_bugs (bug_id, current_task_id, updated_at) VALUES (?,?,?)",
        (bug_id, task_id or f"task-{bug_id}", evidence_at),
    )
    conn.commit()
    bug = conn.execute(
        "SELECT rowid AS _rowid, * FROM backlog_bugs WHERE bug_id = ?", (bug_id,)
    ).fetchone()
    conn.close()
    return {
        "source": "backlog_runtime_state",
        "bug": bug,
        "task_id": task_id or f"task-{bug_id}",
        "latest_event": {},
        "evidence_at": evidence_at,
        "event_id": event_id,
        "rowid": rowid,
    }


# ---------------------------------------------------------------------------
# Test (i): event-less row vs evidencing lane
# ---------------------------------------------------------------------------

class TestEventlessBeatByEvidencing(unittest.TestCase):
    """An event-less candidate must never hold the binding while an evidencing candidate exists."""

    def test_evidencing_candidate_wins_despite_older_timestamp(self) -> None:
        # E2E row: no events, but updated_at is very recent (2 min ago)
        e2e_row = _make_candidate(
            bug_id="E2E-MF-TAKEOVER",
            evidence_at=_ts(-120),   # 2 min ago
            event_id=0,
            rowid=10,
        )
        # Lane row: has governed timeline events, but updated_at is older (10 min ago)
        lane_row = _make_candidate(
            bug_id="task-fe-newestfirst",
            evidence_at=_ts(-600),   # 10 min ago
            event_id=42,             # non-zero → has evidence
            rowid=5,
        )
        event_counts = {
            "E2E-MF-TAKEOVER": 0,
            "task-fe-newestfirst": 12,
        }

        key_e2e = _current_task_candidate_sort_key(e2e_row, event_counts)
        key_lane = _current_task_candidate_sort_key(lane_row, event_counts)

        self.assertGreater(
            key_lane,
            key_e2e,
            "Evidencing lane should sort higher than event-less E2E row",
        )

        # max() picks the winner
        winner = max([e2e_row, lane_row], key=lambda c: _current_task_candidate_sort_key(c, event_counts))
        from agent.governance.server import _row_get
        self.assertEqual(_row_get(winner["bug"], "bug_id", ""), "task-fe-newestfirst")


# ---------------------------------------------------------------------------
# Test (ii): two evidencing lanes → most recent wins
# ---------------------------------------------------------------------------

class TestMostRecentEvidencingLaneWins(unittest.TestCase):
    def test_newer_event_id_wins(self) -> None:
        older_lane = _make_candidate(
            bug_id="lane-older",
            evidence_at=_ts(-300),
            event_id=10,
            rowid=1,
        )
        newer_lane = _make_candidate(
            bug_id="lane-newer",
            evidence_at=_ts(-60),
            event_id=50,
            rowid=2,
        )
        event_counts = {"lane-older": 5, "lane-newer": 20}

        winner = max(
            [older_lane, newer_lane],
            key=lambda c: _current_task_candidate_sort_key(c, event_counts),
        )
        from agent.governance.server import _row_get
        self.assertEqual(_row_get(winner["bug"], "bug_id", ""), "lane-newer")

    def test_same_event_count_uses_evidence_at_timestamp(self) -> None:
        old_ts_lane = _make_candidate(
            bug_id="lane-old-ts",
            evidence_at=_ts(-400),
            event_id=10,
            rowid=1,
        )
        new_ts_lane = _make_candidate(
            bug_id="lane-new-ts",
            evidence_at=_ts(-100),
            event_id=10,
            rowid=2,
        )
        event_counts = {"lane-old-ts": 10, "lane-new-ts": 10}

        winner = max(
            [old_ts_lane, new_ts_lane],
            key=lambda c: _current_task_candidate_sort_key(c, event_counts),
        )
        from agent.governance.server import _row_get
        self.assertEqual(_row_get(winner["bug"], "bug_id", ""), "lane-new-ts")


# ---------------------------------------------------------------------------
# Test (iii): zero-event + stale-heartbeat candidate ages out
# ---------------------------------------------------------------------------

class TestAgingOut(unittest.TestCase):
    def test_zero_event_stale_candidate_ages_out(self) -> None:
        stale_seconds = _CURRENT_TASK_ZERO_EVENT_STALE_TTL_SECS + 60
        stale_candidate = _make_candidate(
            bug_id="stale-lane",
            evidence_at=_ts(-stale_seconds),
            event_id=0,
            rowid=1,
        )
        self.assertTrue(
            _current_task_candidate_is_aged_out(stale_candidate, event_count=0),
            "Zero-event candidate older than TTL should age out",
        )

    def test_zero_event_fresh_candidate_not_aged_out(self) -> None:
        fresh_candidate = _make_candidate(
            bug_id="fresh-lane",
            evidence_at=_ts(-60),   # 1 min ago, well within TTL
            event_id=0,
            rowid=1,
        )
        self.assertFalse(
            _current_task_candidate_is_aged_out(fresh_candidate, event_count=0),
            "Zero-event candidate within TTL should NOT age out",
        )

    def test_evidencing_candidate_never_ages_out(self) -> None:
        # Even if evidence_at is ancient, an evidencing lane never ages out.
        ancient_candidate = _make_candidate(
            bug_id="ancient-with-events",
            evidence_at=_ts(-(3600 * 24 * 30)),  # 30 days ago
            event_id=100,
            rowid=1,
        )
        self.assertFalse(
            _current_task_candidate_is_aged_out(ancient_candidate, event_count=42),
            "Candidate with events must never be aged out",
        )


# ---------------------------------------------------------------------------
# Test (iv): competing-candidates metadata present
# ---------------------------------------------------------------------------

class TestCompetingCandidatesMetadata(unittest.TestCase):
    def test_metadata_order_and_fields(self) -> None:
        c1 = _make_candidate("BUG-A", _ts(-400), event_id=0, rowid=1)
        c2 = _make_candidate("BUG-B", _ts(-200), event_id=20, rowid=2)
        c3 = _make_candidate("BUG-C", _ts(-100), event_id=5, rowid=3)

        event_counts = {"BUG-A": 0, "BUG-B": 12, "BUG-C": 3}
        result = _current_task_competing_candidates_metadata([c1, c2, c3], event_counts)

        self.assertEqual(len(result), 3)
        # First entry must be the highest event_count
        self.assertEqual(result[0]["bug_id"], "BUG-B")
        self.assertEqual(result[0]["event_count"], 12)

        required_fields = {"bug_id", "task_id", "last_evidence_at", "event_count"}
        for entry in result:
            self.assertTrue(
                required_fields.issubset(entry.keys()),
                f"Missing fields in entry: {entry}",
            )

    def test_single_candidate_returns_one_entry(self) -> None:
        c = _make_candidate("ONLY-BUG", _ts(-10), event_id=7, rowid=1)
        result = _current_task_competing_candidates_metadata([c], {"ONLY-BUG": 5})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["bug_id"], "ONLY-BUG")
        self.assertEqual(result[0]["event_count"], 5)

    def test_empty_candidates(self) -> None:
        result = _current_task_competing_candidates_metadata([], {})
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
