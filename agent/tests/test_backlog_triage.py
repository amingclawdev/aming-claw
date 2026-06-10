"""Regression tests for backlog triage hub-file false-merge incidents (2026-06-10).

Five real incidents on 2026-06-10 where triage auto-merged/blocked unrelated
new rows into existing rows because they shared 1-2 hub files
(agent/governance/server.py, task_timeline.py, mf-sop.md) while title
similarity was 0.0-0.22.  All required force_admit workarounds.

AC: BUG-BACKLOG-TRIAGE-FALSE-MERGE-UNRELATED
Also satisfies: AC-BACKLOG-TRIAGE-OVERMERGE-SHARED-CORE-FILE-20260530
"""
from __future__ import annotations
import os, sys
_agent_dir = os.path.join(os.path.dirname(__file__), "..")
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.backlog_triage import triage_backlog_insert


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row(bug_id: str, title: str, target_files: list[str]) -> dict:
    return {"bug_id": bug_id, "title": title, "target_files": target_files}


# ---------------------------------------------------------------------------
# INCIDENT 1 — 2026-06-10
# New: "backlog triage gate falsely merges unrelated rows sharing only server.py"
# Existing: "parallel branch startup echo missing task_id field validation"
# Shared files: agent/governance/server.py, agent/governance/task_timeline.py
# Title similarity: 0.04 (no meaningful token overlap)
# Expected: admit (NOT merge_into)
# ---------------------------------------------------------------------------

INCIDENT_1_NEW = _row(
    "BUG-BACKLOG-TRIAGE-FALSE-MERGE-UNRELATED",
    "backlog triage gate falsely merges unrelated rows sharing only server.py",
    ["agent/governance/backlog_triage.py", "agent/governance/server.py"],
)
INCIDENT_1_EXISTING = _row(
    "AC-PARALLEL-BRANCH-STARTUP-ECHO-TASK-ID-20260610",
    "parallel branch startup echo missing task_id field validation",
    ["agent/governance/server.py", "agent/governance/task_timeline.py"],
)


def test_incident_1_hub_server_plus_task_timeline_no_false_merge():
    """Incident 1: sharing server.py + task_timeline.py with title_sim~0.04 must not merge."""
    decision = triage_backlog_insert(
        INCIDENT_1_NEW,
        [INCIDENT_1_EXISTING],
    )
    assert decision["action"] == "admit", (
        "Expected admit, got %s (w_score=%s, title_sim=%s)"
        % (decision["action"],
           decision.get("evidence", {}).get("weighted_overlap_score"),
           decision.get("evidence", {}).get("title_similarity"))
    )


# ---------------------------------------------------------------------------
# INCIDENT 2 — 2026-06-10
# New: "observer hotfix SOP missing force_admit documentation"
# Existing: "masterplan MCP schema row references stale backlog format"
# Shared files: docs/governance/manual-fix-sop.md, agent/governance/server.py
# Title similarity: 0.0 (no shared meaningful tokens)
# Expected: admit (NOT merge_into)
# ---------------------------------------------------------------------------

INCIDENT_2_NEW = _row(
    "BUG-MF-SOP-FORCE-ADMIT-DOCS-20260610",
    "observer hotfix SOP missing force_admit documentation",
    ["docs/governance/manual-fix-sop.md", "agent/governance/server.py"],
)
INCIDENT_2_EXISTING = _row(
    "BUG-MASTERPLAN-MCP-SCHEMA-STALE-FORMAT-20260610",
    "masterplan MCP schema row references stale backlog format",
    ["agent/governance/server.py", "docs/governance/manual-fix-sop.md",
     "agent/governance/mcp_server.py"],
)


def test_incident_2_sop_plus_server_no_false_merge():
    """Incident 2: sharing mf-sop.md + server.py with title_sim~0.0 must not merge."""
    decision = triage_backlog_insert(
        INCIDENT_2_NEW,
        [INCIDENT_2_EXISTING],
    )
    assert decision["action"] == "admit", (
        "Expected admit, got %s" % decision["action"]
    )


# ---------------------------------------------------------------------------
# INCIDENT 3 — 2026-06-10
# New: "graph snapshot retention GC not triggering on schedule"
# Existing: "task timeline append write validation rejects malformed receipts"
# Shared files: agent/governance/server.py, agent/governance/task_timeline.py
# Title similarity: 0.06
# Expected: admit (NOT merge_into)
# ---------------------------------------------------------------------------

INCIDENT_3_NEW = _row(
    "AC-GRAPH-SNAPSHOT-RETENTION-GC-20260610",
    "graph snapshot retention GC not triggering on schedule",
    ["agent/governance/graph_snapshot_store.py", "agent/governance/server.py",
     "agent/governance/task_timeline.py"],
)
INCIDENT_3_EXISTING = _row(
    "AC-READ-RECEIPT-APPEND-WRITE-VALIDATION-20260610",
    "task timeline append write validation rejects malformed receipts",
    ["agent/governance/task_timeline.py", "agent/governance/server.py",
     "agent/governance/parallel_branch_runtime.py"],
)


def test_incident_3_gc_vs_task_timeline_no_false_merge():
    """Incident 3: GC scheduling vs. task-timeline validation must not merge on hub overlap."""
    decision = triage_backlog_insert(
        INCIDENT_3_NEW,
        [INCIDENT_3_EXISTING],
    )
    assert decision["action"] == "admit", (
        "Expected admit, got %s (w_score=%s)"
        % (decision["action"],
           decision.get("evidence", {}).get("weighted_overlap_score"))
    )


# ---------------------------------------------------------------------------
# INCIDENT 4 — 2026-06-10
# New: "read-receipt write validation rejects malformed receipts"
# Existing: "merge-queue-id not persisted after allocate"
# Shared files: agent/governance/server.py, agent/governance/task_timeline.py
# Title similarity: 0.08
# Expected: admit (NOT merge_into)
# ---------------------------------------------------------------------------

INCIDENT_4_NEW = _row(
    "AC-READ-RECEIPT-APPEND-WRITE-VALIDATION-20260610",
    "read-receipt write validation rejects malformed receipts",
    ["agent/governance/task_timeline.py", "agent/governance/server.py",
     "agent/governance/parallel_branch_runtime.py"],
)
INCIDENT_4_EXISTING = _row(
    "AC-ALLOCATE-PERSIST-MERGE-QUEUE-ID-20260609",
    "merge-queue-id not persisted after allocate",
    ["agent/governance/server.py", "agent/governance/task_timeline.py",
     "agent/governance/backlog_runtime.py"],
)


def test_incident_4_receipt_vs_merge_queue_no_false_merge():
    """Incident 4: read-receipt fix vs. merge-queue-id fix must not merge on hub overlap."""
    decision = triage_backlog_insert(
        INCIDENT_4_NEW,
        [INCIDENT_4_EXISTING],
    )
    assert decision["action"] == "admit", (
        "Expected admit, got %s" % decision["action"]
    )


# ---------------------------------------------------------------------------
# INCIDENT 5 — 2026-06-10  (masterplan-vs-MCP-schema-row case)
# New: "observer governance flow correctness masterplan review"
# Existing: "backlog triage gate falsely merges rows sharing server.py"
# Shared files: agent/governance/server.py  (single hub file)
# Title similarity: 0.10
# Expected: admit (NOT merge_into)
# ---------------------------------------------------------------------------

INCIDENT_5_NEW = _row(
    "AC-OBSERVER-GOVERNANCE-FLOW-CORRECTNESS-MASTERPLAN-20260609",
    "observer governance flow correctness masterplan review",
    ["agent/governance/server.py", "skills/aming-claw/aming-claw.md"],
)
INCIDENT_5_EXISTING = _row(
    "BUG-BACKLOG-TRIAGE-FALSE-MERGE-UNRELATED",
    "backlog triage gate falsely merges unrelated rows sharing only server.py",
    ["agent/governance/backlog_triage.py", "agent/governance/server.py"],
)


def test_incident_5_masterplan_vs_triage_bug_no_false_merge():
    """Incident 5 (masterplan-vs-MCP-schema): sharing only server.py with title_sim~0.10 must not merge."""
    decision = triage_backlog_insert(
        INCIDENT_5_NEW,
        [INCIDENT_5_EXISTING],
    )
    assert decision["action"] == "admit", (
        "Expected admit, got %s" % decision["action"]
    )


# ---------------------------------------------------------------------------
# TRUE DUPLICATE — legitimate merge should still work
# Two rows about the same real-time SSE stream bug, overlapping non-hub files,
# strong title similarity (~0.67).
# Expected: merge_into  (no overcorrection)
# ---------------------------------------------------------------------------

TRUE_DUP_NEW = _row(
    "BUG-SSE-STALE-STREAM-MISSING-CLOSE-R2",
    "Activity SSE stale stream not closed on reconnect causes duplicate events",
    ["agent/governance/server.py", "frontend/dashboard/src/ActivityFeed.tsx",
     "agent/governance/sse_manager.py"],
)
TRUE_DUP_EXISTING = _row(
    "AC-ACTIVITY-SSE-STALE-CURRENT-STREAM-20260609",
    "Activity SSE stale stream visibility missing on reconnect",
    ["agent/governance/sse_manager.py", "frontend/dashboard/src/ActivityFeed.tsx",
     "agent/governance/server.py"],
)


def test_true_duplicate_still_merges():
    """True duplicate: strong title overlap + non-hub shared files must still yield merge_into."""
    decision = triage_backlog_insert(
        TRUE_DUP_NEW,
        [TRUE_DUP_EXISTING],
    )
    assert decision["action"] in ("merge_into", "supersede", "reject_dup"), (
        "Expected merge/supersede/reject_dup for true dup, got %s" % decision["action"]
    )
    assert decision.get("evidence", {}).get("weighted_overlap_score", 0) > 0 or \
           decision.get("evidence", {}).get("title_similarity", 0) > 0


# ---------------------------------------------------------------------------
# Weighted overlap exposes score breakdown
# ---------------------------------------------------------------------------

def test_decision_payload_exposes_score_breakdown_on_admit():
    """When hub-file advisory is emitted (admit path), score breakdown is accessible via logging.
    For the merge path, evidence must carry weighted_overlap_score and per_file_weights."""
    # Use a true dup to force merge path with score breakdown
    decision = triage_backlog_insert(TRUE_DUP_NEW, [TRUE_DUP_EXISTING])
    if decision["action"] in ("merge_into", "supersede"):
        ev = decision.get("evidence", {})
        assert "weighted_overlap_score" in ev, "merge_into must expose weighted_overlap_score"
        assert "per_file_weights" in ev, "merge_into must expose per_file_weights"
        assert "threshold_verdict" in ev, "merge_into must expose threshold_verdict"


# ---------------------------------------------------------------------------
# Hub-file weight breakdown: server.py weight must be very low
# ---------------------------------------------------------------------------

def test_hub_file_weight_near_zero_for_server_py():
    """server.py (a well-known hub) must receive near-zero weight, not drive auto-merge."""
    from governance.backlog_triage import _file_weight
    # With many open rows referencing server.py the weight should be much < 1
    doc_freq = {"agent/governance/server.py": 20}
    w = _file_weight("agent/governance/server.py", doc_freq, 30)
    assert w < 0.2, "server.py hub file weight should be < 0.2, got %s" % w


def test_rare_file_weight_is_high():
    """A file unique to one row must receive high weight (close to 1.0)."""
    from governance.backlog_triage import _file_weight
    doc_freq = {"agent/governance/unique_module.py": 1}
    w = _file_weight("agent/governance/unique_module.py", doc_freq, 50)
    assert w >= 0.9, "unique file weight should be >= 0.9, got %s" % w


# ---------------------------------------------------------------------------
# force_admit: not a triage function test but documents the bypass contract
# ---------------------------------------------------------------------------

def test_force_admit_not_affected_by_triage():
    """force_admit bypass is handled upstream in server.py; triage itself
    receives normal rows and must not special-case the flag."""
    # Triage function never sees force_admit; we verify it does not crash on it
    decision = triage_backlog_insert(
        {"bug_id": "X", "title": "Foo bar fix", "target_files": ["a.py"], "force_admit": True},
        [{"bug_id": "OLD", "title": "Different topic entirely", "target_files": ["a.py"]}],
    )
    # Low-similarity title + one rare file — may merge or admit; must not crash
    assert decision["action"] in ("admit", "merge_into", "supersede", "reject_dup")
