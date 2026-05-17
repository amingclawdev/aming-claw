from __future__ import annotations

import sqlite3

import pytest

from agent.governance.db import SCHEMA_VERSION, _ensure_schema
from agent.governance.managed_ref_runtime import (
    ACTION_ARCHIVE_REF_CONTEXT,
    ACTION_MATERIALIZE_REF_GRAPH,
    ACTION_PREPARE_MERGE_PREVIEW,
    ACTION_QUEUE_MERGE_GATE,
    ACTION_RECOMPUTE_REF_CONTEXT,
    STATE_ARCHIVED,
    STATE_IMPORTED,
    STATE_MERGE_CANDIDATE,
    STATE_MERGE_READY,
    STATE_MERGED,
    STATE_STALE,
    STATE_TRACKED,
    ManagedRefContext,
    apply_managed_ref_bootstrap_plan,
    archive_managed_ref,
    build_managed_ref_bootstrap_plan,
    decide_managed_ref,
    decide_project_deletion_guard,
    ensure_managed_ref_schema,
    get_managed_ref,
    list_managed_ref_events,
    list_managed_refs,
    mark_managed_ref_merged,
    upsert_managed_ref,
)


PID = "managed-ref-project"
NOW = "2026-05-17T10:00:00Z"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_managed_ref_schema(conn)
    return conn


def test_managed_ref_schema_is_in_governance_migration() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    _ensure_schema(conn)

    assert SCHEMA_VERSION >= 39
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?)",
        ("managed_ref_contexts", "managed_ref_events"),
    ).fetchall()
    assert {row["name"] for row in rows} == {
        "managed_ref_contexts",
        "managed_ref_events",
    }


def test_imported_long_lived_ref_stays_inside_project_identity() -> None:
    conn = _conn()
    saved = upsert_managed_ref(
        conn,
        ManagedRefContext(
            project_id=PID,
            ref_name="refs/heads/release/1.x",
            target_ref="refs/heads/main",
            merge_base_commit="B0",
            ref_head_commit="R1",
            target_head_commit="M0",
            status=STATE_IMPORTED,
            evidence={"source": "existing_project_import"},
        ),
        actor="test",
        now_iso=NOW,
    )

    assert saved.project_id == PID
    assert saved.ref_name == "refs/heads/release/1.x"
    assert get_managed_ref(conn, PID, "refs/heads/release/1.x") == saved
    assert decide_managed_ref(saved).action == ACTION_MATERIALIZE_REF_GRAPH
    assert decide_managed_ref(saved).project_delete_blocker is True
    events = list_managed_ref_events(conn, PID, ref_name=saved.ref_name)
    assert events[0]["to_status"] == STATE_IMPORTED
    assert events[0]["evidence"]["source"] == "existing_project_import"


def test_tracked_ref_requires_merge_preview_before_merge_gate() -> None:
    conn = _conn()
    saved = upsert_managed_ref(
        conn,
        ManagedRefContext(
            project_id=PID,
            ref_name="refs/heads/feature/long-running",
            target_ref="refs/heads/main",
            merge_base_commit="B0",
            ref_head_commit="F3",
            target_head_commit="M0",
            validated_target_head="M0",
            snapshot_id="scope-feature-F3",
            projection_id="semproj-feature-F3",
            status=STATE_TRACKED,
        ),
        now_iso=NOW,
    )

    decision = decide_managed_ref(saved, current_target_head="M0")

    assert decision.action == ACTION_PREPARE_MERGE_PREVIEW
    assert decision.merge_ready is False
    assert decision.blockers == ("merge_preview_missing",)


def test_merge_candidate_with_current_target_is_merge_ready() -> None:
    conn = _conn()
    saved = upsert_managed_ref(
        conn,
        ManagedRefContext(
            project_id=PID,
            ref_name="refs/heads/feature/long-running",
            target_ref="refs/heads/main",
            merge_base_commit="B0",
            ref_head_commit="F4",
            target_head_commit="M0",
            validated_target_head="M0",
            snapshot_id="scope-feature-F4",
            projection_id="semproj-feature-F4",
            merge_preview_id="preview-F4-into-M0",
            status=STATE_MERGE_CANDIDATE,
        ),
        now_iso=NOW,
    )

    decision = decide_managed_ref(saved, current_target_head="M0")

    assert decision.decision_state == STATE_MERGE_READY
    assert decision.action == ACTION_QUEUE_MERGE_GATE
    assert decision.merge_ready is True
    assert decision.project_delete_blocker is True


def test_target_movement_marks_managed_ref_stale_until_recomputed() -> None:
    context = ManagedRefContext(
        project_id=PID,
        ref_name="refs/heads/release/1.x",
        target_ref="refs/heads/main",
        merge_base_commit="B0",
        ref_head_commit="R2",
        target_head_commit="M0",
        validated_target_head="M0",
        snapshot_id="scope-release-R2",
        merge_preview_id="preview-R2-into-M0",
        status=STATE_MERGE_CANDIDATE,
    )

    decision = decide_managed_ref(context, current_target_head="M1")

    assert decision.decision_state == STATE_STALE
    assert decision.action == ACTION_RECOMPUTE_REF_CONTEXT
    assert decision.target_moved is True
    assert decision.blockers == ("target_ref_moved",)
    assert decision.merge_ready is False


def test_merged_ref_is_archived_not_project_deleted() -> None:
    conn = _conn()
    upsert_managed_ref(
        conn,
        ManagedRefContext(
            project_id=PID,
            ref_name="refs/heads/feature/large-refactor",
            target_ref="refs/heads/main",
            merge_base_commit="B0",
            ref_head_commit="F9",
            target_head_commit="M8",
            validated_target_head="M8",
            snapshot_id="scope-feature-F9",
            projection_id="semproj-feature-F9",
            merge_preview_id="preview-F9-into-M8",
            status=STATE_MERGE_CANDIDATE,
        ),
        now_iso=NOW,
    )
    merged = mark_managed_ref_merged(
        conn,
        PID,
        "refs/heads/feature/large-refactor",
        merge_commit="M9",
        target_head_commit="M9",
        merge_queue_id="mergeq-long-ref",
        now_iso="2026-05-17T10:01:00Z",
    )

    decision = decide_managed_ref(merged)
    assert decision.action == ACTION_ARCHIVE_REF_CONTEXT
    assert decision.archive_allowed is True
    assert decide_project_deletion_guard([merged])["allowed"] is False

    archived = archive_managed_ref(
        conn,
        PID,
        "refs/heads/feature/large-refactor",
        evidence={"reason": "merged_to_target_and_retained"},
        now_iso="2026-05-17T10:02:00Z",
    )

    assert archived.status == STATE_ARCHIVED
    assert decide_project_deletion_guard([archived])["allowed"] is True
    assert list_managed_refs(conn, PID) == []
    assert list_managed_refs(conn, PID, include_archived=True) == [archived]


def test_archive_rejects_unmerged_tracked_ref() -> None:
    conn = _conn()
    saved = upsert_managed_ref(
        conn,
        ManagedRefContext(
            project_id=PID,
            ref_name="refs/heads/release/2.x",
            target_ref="refs/heads/main",
            snapshot_id="scope-release-2x",
            status=STATE_TRACKED,
        ),
        now_iso=NOW,
    )

    with pytest.raises(ValueError, match="cannot be archived"):
        archive_managed_ref(conn, PID, saved.ref_name)


def test_managed_ref_bootstrap_dry_run_classifies_existing_branches() -> None:
    conn = _conn()

    plan = build_managed_ref_bootstrap_plan(
        conn,
        PID,
        [
            {"ref_name": "refs/heads/main", "ref_head_commit": "M0"},
            {"ref_name": "refs/heads/codex/task-1", "ref_head_commit": "C1"},
            {
                "ref_name": "refs/heads/release/1.x",
                "ref_head_commit": "R1",
                "target_head_commit": "M0",
                "merge_base_commit": "B0",
                "ahead_count": 2,
                "behind_count": 1,
            },
            {
                "ref_name": "refs/heads/feature/large-refactor",
                "ref_head_commit": "F1",
                "target_head_commit": "M0",
                "merge_base_commit": "B0",
            },
            {"ref_name": "refs/tags/v1.0.0", "ref_head_commit": "T1"},
            {"ref_name": "refs/heads/topic/random", "ref_head_commit": "X1"},
            {"ref_name": "refs/heads/release/no-head", "target_head_commit": "M0"},
        ],
        target_ref="refs/heads/main",
        target_head_commit="M0",
    )

    by_ref = {candidate.ref_name: candidate for candidate in plan.candidates}
    assert by_ref["refs/heads/main"].classification == "target_ref"
    assert by_ref["refs/heads/main"].action == "skip"
    assert by_ref["refs/heads/codex/task-1"].classification == "short_lived_agent_ref"
    assert by_ref["refs/heads/release/1.x"].classification == "managed_ref"
    assert by_ref["refs/heads/release/1.x"].action == "import"
    assert by_ref["refs/heads/release/1.x"].ahead_count == 2
    assert by_ref["refs/heads/feature/large-refactor"].action == "import"
    assert by_ref["refs/tags/v1.0.0"].classification == "ignored_ref"
    assert by_ref["refs/heads/topic/random"].classification == "unmanaged_ref"
    assert by_ref["refs/heads/release/no-head"].action == "blocked"
    assert by_ref["refs/heads/release/no-head"].blockers == ("ref_head_missing",)
    assert plan.to_dict()["summary"]["apply_count"] == 2


def test_managed_ref_bootstrap_apply_persists_imports_only() -> None:
    conn = _conn()
    plan = build_managed_ref_bootstrap_plan(
        conn,
        PID,
        [
            {"ref_name": "refs/heads/main", "ref_head_commit": "M0"},
            {
                "ref_name": "refs/heads/release/1.x",
                "ref_head_commit": "R1",
                "target_head_commit": "M0",
                "merge_base_commit": "B0",
                "ahead_count": 3,
            },
            {"ref_name": "refs/heads/codex/task-1", "ref_head_commit": "C1"},
        ],
        target_ref="refs/heads/main",
        target_head_commit="M0",
        evidence={"source": "project_import"},
    )

    result = apply_managed_ref_bootstrap_plan(
        conn,
        plan,
        actor="test",
        now_iso="2026-05-17T11:00:00Z",
    )

    assert result["applied_count"] == 1
    assert result["skipped_count"] == 2
    saved = get_managed_ref(conn, PID, "refs/heads/release/1.x")
    assert saved is not None
    assert saved.status == STATE_IMPORTED
    assert saved.project_id == PID
    assert saved.ref_head_commit == "R1"
    assert saved.target_head_commit == "M0"
    assert saved.evidence["source"] == "project_import"
    assert saved.evidence["managed_ref_bootstrap"]["action"] == "import"
    assert get_managed_ref(conn, PID, "refs/heads/codex/task-1") is None


def test_managed_ref_bootstrap_refresh_marks_existing_context_stale() -> None:
    conn = _conn()
    upsert_managed_ref(
        conn,
        ManagedRefContext(
            project_id=PID,
            ref_name="refs/heads/release/1.x",
            target_ref="refs/heads/main",
            merge_base_commit="B0",
            ref_head_commit="R1",
            target_head_commit="M0",
            validated_target_head="M0",
            snapshot_id="scope-release-old",
            projection_id="semproj-release-old",
            status=STATE_TRACKED,
        ),
        now_iso=NOW,
    )

    plan = build_managed_ref_bootstrap_plan(
        conn,
        PID,
        [
            {
                "ref_name": "refs/heads/release/1.x",
                "ref_head_commit": "R2",
                "target_head_commit": "M1",
                "merge_base_commit": "B1",
            }
        ],
        target_ref="refs/heads/main",
        target_head_commit="M1",
    )

    candidate = plan.candidates[0]
    assert candidate.action == "refresh"
    assert candidate.existing_status == STATE_TRACKED

    result = apply_managed_ref_bootstrap_plan(
        conn,
        plan,
        now_iso="2026-05-17T11:10:00Z",
    )

    assert result["applied_count"] == 1
    saved = get_managed_ref(conn, PID, "refs/heads/release/1.x")
    assert saved is not None
    assert saved.status == STATE_STALE
    assert saved.ref_head_commit == "R2"
    assert saved.target_head_commit == "M1"
    assert saved.snapshot_id == ""
    assert saved.projection_id == ""
    assert saved.evidence["managed_ref_bootstrap"]["action"] == "refresh"
    assert saved.evidence["managed_ref_bootstrap"]["previous_ref_head_commit"] == "R1"
    decision = decide_managed_ref(saved, current_target_head="M1")
    assert decision.action == ACTION_RECOMPUTE_REF_CONTEXT
    assert decision.blockers == ("stale_ref_context",)


def test_managed_ref_bootstrap_noops_current_existing_context() -> None:
    conn = _conn()
    upsert_managed_ref(
        conn,
        ManagedRefContext(
            project_id=PID,
            ref_name="refs/heads/feature/large-refactor",
            target_ref="refs/heads/main",
            merge_base_commit="B0",
            ref_head_commit="F1",
            target_head_commit="M0",
            status=STATE_IMPORTED,
        ),
        now_iso=NOW,
    )

    plan = build_managed_ref_bootstrap_plan(
        conn,
        PID,
        [
            {
                "ref_name": "feature/large-refactor",
                "ref_head_commit": "F1",
                "target_head_commit": "M0",
                "merge_base_commit": "B0",
            }
        ],
        target_ref="main",
        target_head_commit="M0",
    )

    assert plan.candidates[0].ref_name == "refs/heads/feature/large-refactor"
    assert plan.candidates[0].action == "noop"
    result = apply_managed_ref_bootstrap_plan(conn, plan)
    assert result["applied_count"] == 0
    assert result["skipped_count"] == 1
