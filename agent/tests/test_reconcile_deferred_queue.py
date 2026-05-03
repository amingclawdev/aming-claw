"""Tests for reconcile_deferred_queue (CR3 — 8-state machine + DB queue).

Covers >=10 module-level tests:
    test_enqueue, test_dedup, test_batch_limit, test_skip,
    test_retry_with_backoff, test_retry_exhaustion_escalation,
    test_expired_TTL_requeue, test_deltas_change_requeue,
    test_multi_project_PK_isolation, test_auto_session_start_on_first_enqueue,
    test_auto_session_finalize_on_last_terminal, test_file_now_marks_filing,
    test_withdraw_marks_skipped, test_mark_in_chain_records_root_task.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

# Also expose repo root so `import agent.governance...` resolves under pytest.
_REPO_ROOT = _AGENT_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from governance import reconcile_deferred_queue as q  # noqa: E402
from governance.db import _configure_connection, _ensure_schema  # noqa: E402


PROJECT_ID = "p-test"
PROJECT_ID_B = "p-other"


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "gov.db"
    c = sqlite3.connect(str(db_path))
    _configure_connection(c, busy_timeout=2000)
    _ensure_schema(c)
    q.ensure_schema(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Module API surface
# ---------------------------------------------------------------------------


def test_module_api_surface():
    """AC2 — every required symbol is callable on the module."""
    for name in [
        "enqueue_or_lookup", "get_next_batch", "mark_filing", "mark_in_chain",
        "mark_terminal", "requeue_after_failure", "escalate",
        "register_feature_clusters", "completion_summary", "sync_session_counts",
        "RECONCILE_MAX_RETRIES",
    ]:
        assert hasattr(q, name), name
    assert q.RECONCILE_MAX_RETRIES == 3


# ---------------------------------------------------------------------------
# Enqueue / dedup
# ---------------------------------------------------------------------------


def test_enqueue(conn):
    out = q.enqueue_or_lookup(
        PROJECT_ID, "fp-aaaa1111", payload={"a": 1}, run_id="run-1", conn=conn,
    )
    assert out["existed"] is False
    assert out["status"] == "queued"
    assert out["retry_count"] == 0
    assert out["cluster_fingerprint"] == "fp-aaaa1111"


def test_dedup(conn):
    """Re-enqueue with identical payload returns existing row, no duplicate."""
    q.enqueue_or_lookup(PROJECT_ID, "fp-dup", payload={"a": 1},
                        run_id="run-1", conn=conn)
    out = q.enqueue_or_lookup(PROJECT_ID, "fp-dup", payload={"a": 1},
                              run_id="run-1", conn=conn)
    assert out["existed"] is True
    assert out["deltas_changed"] is False
    cnt = conn.execute(
        "SELECT COUNT(*) FROM reconcile_deferred_clusters "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (PROJECT_ID, "fp-dup"),
    ).fetchone()[0]
    assert cnt == 1


def test_deltas_change_requeue(conn):
    """Per §4.6.3 — payload sha mismatch resets retry_count to 0 and re-queues."""
    q.enqueue_or_lookup(PROJECT_ID, "fp-d", payload={"a": 1},
                        run_id="run-1", conn=conn)
    # bump retry_count and set status terminal
    conn.execute(
        "UPDATE reconcile_deferred_clusters SET retry_count = 2, "
        "  status = 'failed_retryable' "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (PROJECT_ID, "fp-d"),
    )
    conn.commit()
    out = q.enqueue_or_lookup(PROJECT_ID, "fp-d", payload={"a": 2},
                              run_id="run-2", conn=conn)
    assert out["deltas_changed"] is True
    assert out["status"] == "queued"
    assert out["retry_count"] == 0


def test_new_run_requeue_clears_stale_chain_linkage(conn):
    q.enqueue_or_lookup(PROJECT_ID, "fp-rerun", payload={"a": 1},
                        run_id="run-old", conn=conn)
    q.mark_filing(PROJECT_ID, "fp-rerun", conn=conn)
    q.mark_in_chain(PROJECT_ID, "fp-rerun", "task-old",
                    bug_id="OPT-OLD", conn=conn)
    q.mark_terminal(PROJECT_ID, "fp-rerun", "resolved", "merged@old",
                    conn=conn)

    out = q.enqueue_or_lookup(PROJECT_ID, "fp-rerun", payload={"a": 1},
                              run_id="run-new", conn=conn)
    assert out["existed"] is True
    assert out["run_changed"] is True
    assert out["status"] == "queued"
    row = conn.execute(
        "SELECT run_id, bug_id, root_task_id, resolved_at, last_terminal_status "
        "FROM reconcile_deferred_clusters WHERE project_id=? AND cluster_fingerprint=?",
        (PROJECT_ID, "fp-rerun"),
    ).fetchone()
    assert row[0] == "run-new"
    assert row[1] is None
    assert row[2] is None
    assert row[3] is None
    assert row[4] is None


def test_register_recleans_queued_row_with_stale_linkage(conn):
    q.enqueue_or_lookup(PROJECT_ID, "fp-stale-queued", payload={"a": 1},
                        run_id="run-same", conn=conn)
    conn.execute(
        "UPDATE reconcile_deferred_clusters SET bug_id=?, root_task_id=?, "
        "last_terminal_status=?, resolved_at=? "
        "WHERE project_id=? AND cluster_fingerprint=?",
        ("OPT-STALE", "task-stale", "resolved", "2026-05-03T00:00:00Z",
         PROJECT_ID, "fp-stale-queued"),
    )
    conn.commit()
    out = q.enqueue_or_lookup(PROJECT_ID, "fp-stale-queued", payload={"a": 1},
                              run_id="run-same", conn=conn)
    assert out["deltas_changed"] is False
    row = conn.execute(
        "SELECT bug_id, root_task_id, last_terminal_status, resolved_at "
        "FROM reconcile_deferred_clusters WHERE project_id=? AND cluster_fingerprint=?",
        (PROJECT_ID, "fp-stale-queued"),
    ).fetchone()
    assert tuple(row) == (None, None, None, None)


# ---------------------------------------------------------------------------
# Batch / priority
# ---------------------------------------------------------------------------


def test_batch_limit(conn):
    for i in range(7):
        q.enqueue_or_lookup(PROJECT_ID, f"fp-batch-{i:02d}",
                            payload={"i": i}, run_id="r", conn=conn)
    rows = q.get_next_batch(PROJECT_ID, batch_size=3, conn=conn)
    assert len(rows) == 3
    rows_all = q.get_next_batch(PROJECT_ID, batch_size=100, conn=conn)
    assert len(rows_all) == 7


def test_get_next_batch_can_filter_run_id(conn):
    q.enqueue_or_lookup(PROJECT_ID, "fp-run-a", payload={"a": 1},
                        run_id="run-a", conn=conn)
    q.enqueue_or_lookup(PROJECT_ID, "fp-run-b", payload={"b": 1},
                        run_id="run-b", conn=conn)
    rows = q.get_next_batch(PROJECT_ID, batch_size=10, run_id="run-a", conn=conn)
    assert [r["cluster_fingerprint"] for r in rows] == ["fp-run-a"]


def test_register_feature_clusters_tracks_run_and_session_counts(conn, tmp_path, monkeypatch):
    from governance import reconcile_session as rs

    real_start = rs.start_session

    def _start_with_tmp(c, pid, **kw):
        kw.setdefault("governance_dir", tmp_path)
        return real_start(c, pid, **kw)

    monkeypatch.setattr(rs, "start_session", _start_with_tmp)

    result = q.register_feature_clusters(
        PROJECT_ID,
        "phase-z-run",
        [
            {"cluster_fingerprint": "fp-reg-a", "primary_files": ["a.py"]},
            {"cluster_fingerprint": "fp-reg-b", "primary_files": ["b.py"]},
        ],
        conn=conn,
    )
    assert result["expected"] == 2
    assert result["registered"] == 2
    assert result["created"] == 2
    assert result["summary"]["total"] == 2
    assert result["summary"]["ready_for_orphan_pass"] is False
    row = conn.execute(
        "SELECT run_id, cluster_count_total, cluster_count_resolved "
        "FROM reconcile_sessions WHERE project_id = ?",
        (PROJECT_ID,),
    ).fetchone()
    assert row[0] == "phase-z-run"
    assert row[1] == 2
    assert row[2] == 0


def test_register_existing_clusters_starts_session_when_none_active(conn, tmp_path, monkeypatch):
    from governance import reconcile_session as rs

    real_start = rs.start_session

    def _start_with_tmp(c, pid, **kw):
        kw.setdefault("governance_dir", tmp_path)
        return real_start(c, pid, **kw)

    monkeypatch.setattr(rs, "start_session", _start_with_tmp)

    q.ensure_schema(conn)
    conn.execute(
        "INSERT INTO reconcile_deferred_clusters ("
        "project_id, cluster_fingerprint, payload_json, payload_sha256, "
        "run_id, status, priority, retry_count, first_seen_at, last_seen_at, expires_at"
        ") VALUES (?, ?, ?, ?, ?, 'queued', 100, 0, ?, ?, ?)",
        (
            PROJECT_ID, "fp-existing-only", "{}", "old-sha", "old-run",
            "2026-05-03T00:00:00Z", "2026-05-03T00:00:00Z",
            "2026-05-04T00:00:00Z",
        ),
    )
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM reconcile_sessions WHERE project_id=?",
        (PROJECT_ID,),
    ).fetchone()[0] == 0

    result = q.register_feature_clusters(
        PROJECT_ID,
        "phase-z-existing",
        [{"cluster_fingerprint": "fp-existing-only"}],
        conn=conn,
    )
    assert result["registered"] == 1
    row = conn.execute(
        "SELECT run_id, status FROM reconcile_sessions WHERE project_id=?",
        (PROJECT_ID,),
    ).fetchone()
    assert tuple(row) == ("phase-z-existing", "active")


def test_completion_summary_ready_only_after_safe_terminal_states(conn):
    q.register_feature_clusters(
        PROJECT_ID,
        "phase-z-safe",
        [
            {"cluster_fingerprint": "fp-safe-a"},
            {"cluster_fingerprint": "fp-safe-b"},
        ],
        conn=conn,
    )
    summary = q.completion_summary(PROJECT_ID, run_id="phase-z-safe", conn=conn)
    assert summary["active_count"] == 2
    assert summary["ready_for_orphan_pass"] is False

    q.mark_terminal(PROJECT_ID, "fp-safe-a", "resolved", "merged@abc",
                    conn=conn)
    q.mark_terminal(PROJECT_ID, "fp-safe-b", "skipped", "observer_explicit_skip",
                    conn=conn)
    summary = q.completion_summary(PROJECT_ID, run_id="phase-z-safe", conn=conn)
    assert summary["all_terminal"] is True
    assert summary["ready_for_orphan_pass"] is True
    assert summary["ready_for_finalize"] is True


def test_completion_summary_failed_terminal_still_blocks_orphan_pass(conn):
    q.register_feature_clusters(
        PROJECT_ID,
        "phase-z-failed",
        [{"cluster_fingerprint": "fp-failed-a"}],
        conn=conn,
    )
    q.mark_terminal(PROJECT_ID, "fp-failed-a", "failed_terminal",
                    "retry_exhausted", conn=conn)
    summary = q.completion_summary(PROJECT_ID, run_id="phase-z-failed", conn=conn)
    assert summary["all_terminal"] is True
    assert summary["unresolved_terminal_count"] == 1
    assert summary["ready_for_orphan_pass"] is False


# ---------------------------------------------------------------------------
# State machine: skip / filing / in_chain / withdrawn / retry
# ---------------------------------------------------------------------------


def test_skip(conn):
    q.enqueue_or_lookup(PROJECT_ID, "fp-skip", payload={}, run_id="r", conn=conn)
    assert q.mark_terminal(PROJECT_ID, "fp-skip", "skipped",
                           reason="user_request", conn=conn) is True
    row = conn.execute(
        "SELECT status, skipped_reason FROM reconcile_deferred_clusters "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (PROJECT_ID, "fp-skip"),
    ).fetchone()
    assert row[0] == "skipped"
    assert row[1] == "user_request"


def test_file_now_marks_filing(conn):
    q.enqueue_or_lookup(PROJECT_ID, "fp-file", payload={}, run_id="r", conn=conn)
    assert q.mark_filing(PROJECT_ID, "fp-file", conn=conn) is True
    row = conn.execute(
        "SELECT status, filed_at FROM reconcile_deferred_clusters "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (PROJECT_ID, "fp-file"),
    ).fetchone()
    assert row[0] == "filing"
    assert row[1]


def test_withdraw_marks_skipped(conn):
    """A 'withdraw' is conceptually mark_terminal('skipped', reason)."""
    q.enqueue_or_lookup(PROJECT_ID, "fp-wd", payload={}, run_id="r", conn=conn)
    q.mark_filing(PROJECT_ID, "fp-wd", conn=conn)
    q.mark_in_chain(PROJECT_ID, "fp-wd", "task-rt-1", conn=conn)
    assert q.mark_terminal(PROJECT_ID, "fp-wd", "skipped",
                           "observer_withdraw", conn=conn) is True
    row = conn.execute(
        "SELECT status, skipped_reason, root_task_id FROM reconcile_deferred_clusters "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (PROJECT_ID, "fp-wd"),
    ).fetchone()
    assert row[0] == "skipped"
    assert row[1] == "observer_withdraw"
    assert row[2] == "task-rt-1"


def test_mark_in_chain_records_root_task(conn):
    q.enqueue_or_lookup(PROJECT_ID, "fp-ic", payload={}, run_id="r", conn=conn)
    q.mark_filing(PROJECT_ID, "fp-ic", conn=conn)
    assert q.mark_in_chain(PROJECT_ID, "fp-ic", "task-root-9",
                           bug_id="OPT-BACKLOG-X", conn=conn) is True
    row = conn.execute(
        "SELECT status, root_task_id, bug_id FROM reconcile_deferred_clusters "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (PROJECT_ID, "fp-ic"),
    ).fetchone()
    assert row[0] == "in_chain"
    assert row[1] == "task-root-9"
    assert row[2] == "OPT-BACKLOG-X"


# ---------------------------------------------------------------------------
# Retry budget — backoff + escalation
# ---------------------------------------------------------------------------


def test_retry_with_backoff(conn):
    q.enqueue_or_lookup(PROJECT_ID, "fp-retry", payload={}, run_id="r", conn=conn)
    out = q.requeue_after_failure(PROJECT_ID, "fp-retry", retry_count_delta=1,
                                  reason="merge_failed", conn=conn)
    assert out["status"] == "failed_retryable"
    assert out["retry_count"] == 1
    assert out["next_retry_at"]
    # Second failure -> retry_count=2
    out2 = q.requeue_after_failure(PROJECT_ID, "fp-retry", retry_count_delta=1,
                                   conn=conn)
    assert out2["retry_count"] == 2


def test_retry_exhaustion_escalation(conn):
    q.enqueue_or_lookup(PROJECT_ID, "fp-bust12345", payload={}, run_id="r",
                        conn=conn)
    # Exhaust budget — 4 failures > RECONCILE_MAX_RETRIES (3)
    for _ in range(q.RECONCILE_MAX_RETRIES + 1):
        out = q.requeue_after_failure(PROJECT_ID, "fp-bust12345",
                                      retry_count_delta=1, conn=conn)
    assert out["status"] == "failed_terminal"
    assert out["escalated_bug_id"]
    assert out["escalated_bug_id"].startswith(
        "OPT-BACKLOG-RECONCILE-CLUSTER-"
    )
    assert "NEEDS-OBSERVER" in out["escalated_bug_id"]
    row = conn.execute(
        "SELECT status, bug_id FROM reconcile_deferred_clusters "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (PROJECT_ID, "fp-bust12345"),
    ).fetchone()
    assert row[0] == "failed_terminal"
    assert row[1] == out["escalated_bug_id"]


# ---------------------------------------------------------------------------
# TTL / expired
# ---------------------------------------------------------------------------


def test_expired_TTL_requeue(conn):
    """Rows past expires_at auto-promote to 'expired' on get_next_batch."""
    q.enqueue_or_lookup(PROJECT_ID, "fp-ttl", payload={}, run_id="r", conn=conn)
    # Force expires_at into the past.
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    conn.execute(
        "UPDATE reconcile_deferred_clusters SET expires_at = ? "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (past, PROJECT_ID, "fp-ttl"),
    )
    conn.commit()
    rows = q.get_next_batch(PROJECT_ID, batch_size=10, conn=conn)
    assert all(r["cluster_fingerprint"] != "fp-ttl" for r in rows)
    row = conn.execute(
        "SELECT status FROM reconcile_deferred_clusters "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (PROJECT_ID, "fp-ttl"),
    ).fetchone()
    assert row[0] == "expired"


# ---------------------------------------------------------------------------
# Multi-project isolation (composite PK)
# ---------------------------------------------------------------------------


def test_multi_project_PK_isolation(conn):
    """Same fingerprint in two projects is two distinct rows."""
    q.enqueue_or_lookup(PROJECT_ID, "fp-shared", payload={"x": 1},
                        run_id="r-A", conn=conn)
    q.enqueue_or_lookup(PROJECT_ID_B, "fp-shared", payload={"x": 99},
                        run_id="r-B", conn=conn)
    rows = conn.execute(
        "SELECT project_id, run_id FROM reconcile_deferred_clusters "
        "WHERE cluster_fingerprint = ? ORDER BY project_id",
        ("fp-shared",),
    ).fetchall()
    assert len(rows) == 2
    pids = sorted(r[0] for r in rows)
    assert pids == sorted([PROJECT_ID, PROJECT_ID_B])


# ---------------------------------------------------------------------------
# R5 — auto-session lifecycle hooks
# ---------------------------------------------------------------------------


def test_auto_session_start_on_first_enqueue(conn, tmp_path, monkeypatch):
    """First enqueue with no active session triggers reconcile_session.start_session."""
    # Reuse the conn fixture's DB; ensure no session exists yet.
    sessions_before = conn.execute(
        "SELECT COUNT(*) FROM reconcile_sessions WHERE project_id = ? "
        "AND status = 'active'",
        (PROJECT_ID,),
    ).fetchone()[0]
    assert sessions_before == 0
    # Patch governance_dir so start_session writes overlay to tmp_path
    from governance import reconcile_session as rs

    real_start = rs.start_session

    def _start_with_tmp(c, pid, **kw):
        kw.setdefault("governance_dir", tmp_path)
        return real_start(c, pid, **kw)

    monkeypatch.setattr(rs, "start_session", _start_with_tmp)
    q.enqueue_or_lookup(PROJECT_ID, "fp-sess1", payload={}, run_id="r-1",
                        conn=conn)
    sessions_after = conn.execute(
        "SELECT COUNT(*) FROM reconcile_sessions WHERE project_id = ? "
        "AND status = 'active'",
        (PROJECT_ID,),
    ).fetchone()[0]
    assert sessions_after == 1


def test_auto_session_finalize_on_last_terminal(conn, tmp_path, monkeypatch):
    """When the last in-flight row goes terminal, session transitions to finalizing."""
    from governance import reconcile_session as rs

    real_start = rs.start_session

    def _start_with_tmp(c, pid, **kw):
        kw.setdefault("governance_dir", tmp_path)
        return real_start(c, pid, **kw)

    monkeypatch.setattr(rs, "start_session", _start_with_tmp)

    # Single cluster -> mark terminal -> session goes to finalizing.
    q.enqueue_or_lookup(PROJECT_ID, "fp-final-1", payload={}, run_id="r-1",
                        conn=conn)
    q.mark_terminal(PROJECT_ID, "fp-final-1", "resolved", "merged@abc",
                    conn=conn)
    row = conn.execute(
        "SELECT status FROM reconcile_sessions WHERE project_id = ?",
        (PROJECT_ID,),
    ).fetchone()
    assert row is not None
    assert row[0] == "finalizing"


# ---------------------------------------------------------------------------
# Terminal-event hook integration with auto_chain (R6)
# ---------------------------------------------------------------------------


def test_terminal_success_hook_via_auto_chain(conn, tmp_path, monkeypatch):
    """auto_chain.on_task_completed triggers mark_terminal('resolved') on merge success."""
    from governance import reconcile_session as rs
    from governance import auto_chain

    real_start = rs.start_session

    def _start_with_tmp(c, pid, **kw):
        kw.setdefault("governance_dir", tmp_path)
        return real_start(c, pid, **kw)

    monkeypatch.setattr(rs, "start_session", _start_with_tmp)

    q.enqueue_or_lookup(PROJECT_ID, "fp-mhook", payload={}, run_id="r-1",
                        conn=conn)
    metadata = {"operation_type": "reconcile-cluster",
                "cluster_fingerprint": "fp-mhook"}
    auto_chain._reconcile_cluster_terminal_hook(
        conn, PROJECT_ID, "task-merge-1", "merge", "succeeded",
        {"merge_commit": "deadbeef"}, metadata,
    )
    row = conn.execute(
        "SELECT status, terminal_reason FROM reconcile_deferred_clusters "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (PROJECT_ID, "fp-mhook"),
    ).fetchone()
    assert row[0] == "resolved"
    assert "deadbeef" in (row[1] or "")


def test_terminal_failure_hook_via_auto_chain(conn, tmp_path, monkeypatch):
    """auto_chain hook on failed task increments retry_count via requeue_after_failure."""
    from governance import auto_chain
    from governance import reconcile_session as rs

    real_start = rs.start_session

    def _start_with_tmp(c, pid, **kw):
        kw.setdefault("governance_dir", tmp_path)
        return real_start(c, pid, **kw)

    monkeypatch.setattr(rs, "start_session", _start_with_tmp)

    q.enqueue_or_lookup(PROJECT_ID, "fp-fhook", payload={}, run_id="r-1",
                        conn=conn)
    metadata = {"operation_type": "reconcile-cluster",
                "cluster_fingerprint": "fp-fhook"}
    auto_chain._reconcile_cluster_terminal_hook(
        conn, PROJECT_ID, "task-fail", "merge", "failed",
        {"error_message": "merge_conflict"}, metadata,
    )
    row = conn.execute(
        "SELECT status, retry_count FROM reconcile_deferred_clusters "
        "WHERE project_id = ? AND cluster_fingerprint = ?",
        (PROJECT_ID, "fp-fhook"),
    ).fetchone()
    assert row[0] == "failed_retryable"
    assert row[1] == 1
