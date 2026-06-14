"""Executable dry-run scenarios for parallel branch runtime recovery."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from agent.tests.fixtures.parallel_project import (
    PB001RestartFixtureProject,
    create_parallel_fixture_project,
    create_pb001_restart_fixture_project,
)
from agent.governance.db import SCHEMA_VERSION, _ensure_schema
from agent.governance import graph_query_trace
from agent.governance.mf_subagent_contract import (
    MfSubagentContractError,
    validate_mf_subagent_finish_gate,
)
from agent.governance.parallel_branch_runtime import (
    ACTION_LEAVE_MERGED,
    ACTION_OBSERVER_DECISION_REQUIRED,
    ACTION_RECLAIM_AFTER_DEPENDENCY,
    ACTION_RECLAIM_FROM_CHECKPOINT,
    ACTION_WAIT_FOR_DEPENDENCY,
    RUNTIME_CONTEXT_ACCESS_AUDIT_SCHEMA_VERSION,
    RUNTIME_CONTEXT_ACTION_PLAN_SCHEMA_VERSION,
    RUNTIME_CONTEXT_CAPABILITY_BOUNDARY_SCHEMA_VERSION,
    RUNTIME_CONTEXT_CLOSE_GATE_VIEW_SCHEMA_VERSION,
    RUNTIME_CONTEXT_CONTROL_PLANE_SCHEMA_VERSION,
    RUNTIME_CONTEXT_CONTENT_ADDRESS_SCHEMA_VERSION,
    RUNTIME_CONTEXT_CURRENT_SCHEMA_VERSION,
    RUNTIME_CONTEXT_GATE_INPUTS_SCHEMA_VERSION,
    RUNTIME_CONTEXT_LANE_FOLD_SCHEMA_VERSION,
    RUNTIME_CONTEXT_WORKER_VIEW_SCHEMA_VERSION,
    STATE_DEPENDENCY_BLOCKED,
    STATE_MERGE_FAILED,
    STATE_MERGE_READY,
    STATE_MERGED,
    STATE_STALE_AFTER_DEPENDENCY_MERGE,
    STATE_WAITING_DEPENDENCY,
    STATE_RECLAIMABLE,
    STATE_RUNNING,
    STATE_ALLOCATED,
    STATE_WORKTREE_READY,
    BranchRuntimeFenceError,
    BranchRuntimeTask,
    BranchTaskRuntimeContext,
    MergeQueueItem,
    append_branch_contract_revision,
    branch_context_from_chain_stage,
    branch_runtime_context_id,
    build_runtime_context_current_view,
    build_runtime_context_gate_inputs_view,
    build_runtime_context_lane_plan_view,
    build_runtime_context_projection,
    decide_merge_queue,
    decide_restart_recovery,
    ensure_branch_runtime_schema,
    get_branch_context,
    get_latest_branch_contract_revision,
    list_branch_contexts,
    materialize_branch_worktree,
    mf_subagent_session_token_hash,
    plan_branch_runtime_context,
    queue_merge_item_for_branch_context,
    record_runtime_context_access_audit,
    record_branch_finish_gate,
    record_mf_subagent_startup,
    recover_expired_branch_contexts,
    record_branch_checkpoint,
    redact_runtime_context_payload,
    runtime_context_audit_nodes_for_views,
    runtime_context_content_hash,
    runtime_context_filter_content_address,
    runtime_tasks_from_contexts,
    upsert_branch_context,
    validate_mf_subagent_graph_query_identity,
)

PROJECT_ID = "fixture-parallel-project"
BATCH_ID = "PB-001"
NOW = "2026-05-16T12:00:00Z"
EXPIRED = "2026-05-16T11:50:00Z"
PB001_TASK_IDS = ("T1", "T2", "T3", "T4", "T5")
PB001_BRANCH_NAMES = {
    "T1": "codex/PB001-T1-scope-reconcile",
    "T2": "codex/PB001-T2-branch-graph-refs",
    "T3": "codex/PB001-T3-task-runtime",
    "T4": "codex/PB001-T4-dashboard-read-model",
    "T5": "codex/PB001-T5-chain-adapter",
}


def _pb001_branch_ref(
    task_id: str,
    fixture: PB001RestartFixtureProject | None = None,
) -> str:
    if fixture is not None:
        return fixture.task_branches[task_id].branch_ref
    return f"refs/heads/{PB001_BRANCH_NAMES[task_id]}"


def _pb001_base_commit(
    task_id: str,
    fixture: PB001RestartFixtureProject | None = None,
) -> str:
    if fixture is not None:
        return fixture.task_branches[task_id].base_commit
    return "base-001"


def _pb001_head_commit(
    task_id: str,
    fixture: PB001RestartFixtureProject | None = None,
) -> str:
    if fixture is not None:
        return fixture.task_branches[task_id].head_commit
    return f"head-{task_id}"


def _pb001_target_head(fixture: PB001RestartFixtureProject | None = None) -> str:
    if fixture is not None:
        return fixture.target_head_after_t1
    return "head-T1"


def _pb001_tasks(fixture: PB001RestartFixtureProject | None = None) -> list[BranchRuntimeTask]:
    return [
        BranchRuntimeTask(
            task_id="T1",
            branch_ref=_pb001_branch_ref("T1", fixture),
            status="merged",
            merge_epoch="merge-001",
        ),
        BranchRuntimeTask(
            task_id="T2",
            branch_ref=_pb001_branch_ref("T2", fixture),
            status="merge_failed",
            depends_on=("T1",),
        ),
        BranchRuntimeTask(
            task_id="T3",
            branch_ref=_pb001_branch_ref("T3", fixture),
            status="running",
            depends_on=("T1",),
            lease_expired=True,
            checkpoint_id="checkpoint-T3",
        ),
        BranchRuntimeTask(
            task_id="T4",
            branch_ref=_pb001_branch_ref("T4", fixture),
            status="queued_for_merge",
            depends_on=("T2",),
        ),
        BranchRuntimeTask(
            task_id="T5",
            branch_ref=_pb001_branch_ref("T5", fixture),
            status="running",
            depends_on=("T3",),
            lease_expired=True,
            checkpoint_id="checkpoint-T5",
        ),
    ]


def _by_task(plan):
    return {decision.task_id: decision for decision in plan.decisions}


def _runtime_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_branch_runtime_schema(conn)
    return conn


def _canonical_test_hash(value: object) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _contract_revision_test_context(task_id: str = "T-revision") -> BranchTaskRuntimeContext:
    return BranchTaskRuntimeContext(
        project_id=PROJECT_ID,
        task_id=task_id,
        root_task_id="parent-revision",
        stage_task_id=task_id,
        backlog_id="BUG-REVISION",
        worker_id="worker-revision",
        agent_id="agent-revision",
        branch_ref=f"refs/heads/codex/{task_id}",
        status=STATE_WORKTREE_READY,
        fence_token="fence-revision",
        worktree_path=f"/tmp/{task_id}",
        base_commit="base-revision",
        head_commit="head-revision",
        target_head_commit="target-revision",
        merge_queue_id="mq-revision",
    )


def _expected_contract_revision_hash(
    context: BranchTaskRuntimeContext,
    *,
    runtime_context_id: str,
    payload: Mapping[str, object],
    route_identity: Mapping[str, object],
    previous_revision_hash: str = "",
    revision_id: str = "",
) -> str:
    material = {
        "schema_version": "agent_task_contract_revision_visible_text.v1",
        "project_id": context.project_id,
        "runtime_context_id": runtime_context_id,
        "task_id": context.task_id,
        "parent_task_id": context.root_task_id,
        "backlog_id": context.backlog_id,
        "contract_version": "mf_parallel.v1",
        "payload": dict(payload),
        "route_identity": dict(route_identity) | {"raw_private_context_exposed": False},
        "previous_revision_hash": previous_revision_hash,
    }
    if revision_id:
        material["revision_id"] = revision_id
    return _canonical_test_hash(material)


def _git(worktree: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(worktree), *args],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def _ensure_startup_git_worktree(worktree: Path) -> tuple[str, str]:
    worktree.mkdir(parents=True, exist_ok=True)
    if not (worktree / ".git").exists():
        subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=worktree,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test Worker"],
            cwd=worktree,
            check=True,
        )
        source_path = worktree / "agent" / "governance" / "parallel_branch_runtime.py"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text("base runtime\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=worktree, check=True)
        subprocess.run(
            ["git", "commit", "-m", "base"],
            cwd=worktree,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        source_path.write_text("base runtime\nhead runtime\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=worktree, check=True)
        subprocess.run(
            ["git", "commit", "-m", "head"],
            cwd=worktree,
            check=True,
            stdout=subprocess.DEVNULL,
        )
    base_commit = _git(worktree, "rev-list", "--max-parents=0", "HEAD")
    head_commit = _git(worktree, "rev-parse", "HEAD")
    return base_commit, head_commit


def _startup_runtime_context_id() -> str:
    return branch_runtime_context_id(PROJECT_ID, "mf-sub-startup")


def _insert_startup_graph_trace(
    conn: sqlite3.Connection,
    *,
    trace_id: str = "gqt-startup",
    task_id: str = "mf-sub-startup",
    parent_task_id: str = "parent-startup",
    runtime_context_id: str | None = None,
    fence_token: str = "fence-startup",
    query_purpose: str = "subagent_gate_validation",
) -> None:
    graph_query_trace.ensure_schema(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO graph_query_traces
          (trace_id, project_id, snapshot_id, actor, query_source, query_purpose,
           run_id, parent_task_id, runtime_context_id, task_id, worker_role,
           fence_token, status, budget_json, usage_json, artifact_path,
           created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trace_id,
            PROJECT_ID,
            "scope-test",
            "codex-session-startup",
            "mf_subagent",
            query_purpose,
            f"mf_subagent:{task_id}:fence:test",
            parent_task_id,
            runtime_context_id or _startup_runtime_context_id(),
            task_id,
            "mf_sub",
            fence_token,
            "complete",
            "{}",
            "{}",
            "",
            NOW,
            NOW,
        ),
    )


def _startup_payload(worktree: str, **overrides: object) -> dict[str, object]:
    base_commit, head_commit = _ensure_startup_git_worktree(Path(worktree))
    worker_session_id = str(overrides.get("worker_session_id") or "codex-session-startup")
    auto_transcript_path = "worker_transcript_path" not in overrides
    transcript_dir = Path(worktree) / ".worker-transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = transcript_dir / f"{worker_session_id}.jsonl"
    payload: dict[str, object] = {
        "task_id": "mf-sub-startup",
        "parent_task_id": "parent-startup",
        "worker_role": "mf_sub",
        "worker_id": "worker-startup",
        "agent_id": "agent-startup",
        "session_token": "secret-worker-session-token",
        "runtime_context_id": branch_runtime_context_id(PROJECT_ID, "mf-sub-startup"),
        "fence_token": "fence-startup",
        "actual_cwd": worktree,
        "actual_git_root": worktree,
        "branch": "refs/heads/codex/mf-sub-startup",
        "head_commit": head_commit,
        "base_commit": base_commit,
        "target_head_commit": "target-startup",
        "merge_queue_id": "mq-startup",
        "owned_files": ["agent/governance/parallel_branch_runtime.py"],
        "route_id": "route-startup",
        "route_context_hash": "sha256:route-startup",
        "prompt_contract_id": "rprompt-startup",
        "prompt_contract_hash": "sha256:prompt-startup",
        "route_token_ref": "rtok-startup",
        "visible_injection_manifest_hash": "sha256:visible-startup",
        "observer_command_id": "cmd-startup",
        "read_receipt_hash": "sha256:read-startup",
        "read_receipt_event_id": "2873",
        "worker_session_id": worker_session_id,
        "worker_transcript_path": str(transcript_path),
        "harness_type": "codex",
        "changed_files": ["agent/governance/parallel_branch_runtime.py"],
        "graph_trace_ids": ["gqt-startup"],
    }
    payload.update(overrides)
    transcript_record = {
        "session_id": payload.get("worker_session_id"),
        "harness_type": payload.get("harness_type"),
        "event": "mf_subagent graph_query implementation",
        "query_source": "mf_subagent",
        "trace_ids": payload.get("graph_trace_ids"),
        "task_id": payload.get("task_id"),
        "runtime_context_id": payload.get("runtime_context_id"),
        "fence_token": payload.get("fence_token"),
        "worktree_path": worktree,
        "branch": payload.get("branch"),
        "changed_files": payload.get("changed_files"),
        "observer_command_id": payload.get("observer_command_id"),
        "read_receipt_hash": payload.get("read_receipt_hash"),
        "read_receipt_event_id": payload.get("read_receipt_event_id"),
        "route_token_ref": payload.get("route_token_ref"),
    }
    if auto_transcript_path and str(payload.get("worker_transcript_path") or "").strip():
        Path(str(payload["worker_transcript_path"])).write_text(
            json.dumps(transcript_record) + "\n",
            encoding="utf-8",
        )
    return payload


def _insert_startup_context(conn: sqlite3.Connection, worktree: str) -> None:
    base_commit, head_commit = _ensure_startup_git_worktree(Path(worktree))
    context = BranchTaskRuntimeContext(
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        root_task_id="parent-startup",
        stage_task_id="mf-sub-startup",
        backlog_id="BUG-STARTUP",
        worker_id="worker-startup",
        worker_slot_id="worker-startup",
        agent_id="agent-startup",
        allocation_owner="agent-startup",
        branch_ref="refs/heads/codex/mf-sub-startup",
        status=STATE_WORKTREE_READY,
        fence_token="fence-startup",
        worktree_path=worktree,
        base_commit=base_commit,
        head_commit=head_commit,
        target_head_commit="target-startup",
        merge_queue_id="mq-startup",
        session_token_hash=mf_subagent_session_token_hash("secret-worker-session-token"),
    )
    upsert_branch_context(conn, context, now_iso=NOW)
    append_branch_contract_revision(
        conn,
        context,
        route_identity={
            "route_id": "route-startup",
            "route_context_hash": "sha256:route-startup",
            "prompt_contract_id": "rprompt-startup",
            "prompt_contract_hash": "sha256:prompt-startup",
            "route_token_ref": "rtok-startup",
            "visible_injection_manifest_hash": "sha256:visible-startup",
        },
        now_iso=NOW,
    )
    _insert_startup_graph_trace(conn)


def _runtime_projection_context(**overrides: object) -> BranchTaskRuntimeContext:
    payload: dict[str, object] = {
        "project_id": PROJECT_ID,
        "governance_project_id": PROJECT_ID,
        "target_project_id": PROJECT_ID,
        "target_project_root": "/repo",
        "task_id": "mf-sub-runtime-context",
        "root_task_id": "parent-runtime-context",
        "stage_task_id": "mf-sub-runtime-context",
        "backlog_id": "BUG-RUNTIME-CONTEXT",
        "worker_id": "worker-runtime-context",
        "worker_slot_id": "worker-runtime-context",
        "actual_host_worker_id": "worker-runtime-context",
        "agent_id": "agent-runtime-context",
        "branch_ref": "refs/heads/codex/mf-sub-runtime-context",
        "ref_name": "main",
        "status": STATE_RUNNING,
        "fence_token": "fence-runtime-context",
        "worktree_id": "wt-runtime-context",
        "worktree_path": "/repo/.worktrees/mf-sub-runtime-context",
        "base_commit": "base-runtime-context",
        "head_commit": "head-runtime-context",
        "target_head_commit": "target-runtime-context",
        "snapshot_id": "scope-runtime-context",
        "projection_id": "semproj-runtime-context",
        "merge_queue_id": "mq-runtime-context",
        "merge_preview_id": "mp-runtime-context",
    }
    payload.update(overrides)
    return BranchTaskRuntimeContext(**payload)


def test_append_branch_contract_revision_defaults_revision_id_to_visible_content_hash() -> None:
    conn = _runtime_conn()
    context = _contract_revision_test_context()
    payload = {
        "target_files": ["agent/governance/parallel_branch_runtime.py"],
        "acceptance_criteria": ["revision_id is content addressed"],
        "private_note": "redacted from visible content",
    }
    route_identity = {
        "route_id": "route-revision",
        "route_context_hash": "sha256:route-revision",
        "prompt_contract_id": "rprompt-revision",
        "prompt_contract_hash": "sha256:prompt-revision",
        "route_token_ref": "rtok-revision",
    }
    runtime_context_id = branch_runtime_context_id(PROJECT_ID, context.task_id)
    visible_payload = {
        "target_files": ["agent/governance/parallel_branch_runtime.py"],
        "acceptance_criteria": ["revision_id is content addressed"],
    }
    expected_hash = _expected_contract_revision_hash(
        context,
        runtime_context_id=runtime_context_id,
        payload=visible_payload,
        route_identity=route_identity,
    )

    revision = append_branch_contract_revision(
        conn,
        context,
        payload=payload,
        route_identity=route_identity,
        now_iso=NOW,
    )

    assert revision.revision_id == expected_hash
    receipt = revision.payload["revision_receipt"]
    assert receipt["canonical_visible_contract_text_hash"] == expected_hash
    assert receipt["previous_revision_hash"] == ""
    assert revision.payload["source_of_truth"] == "Contract/Revision/Event"
    assert "private_note" not in revision.payload

    other_conn = _runtime_conn()
    same_content_context = _contract_revision_test_context()
    same_content_revision = append_branch_contract_revision(
        other_conn,
        same_content_context,
        payload=payload,
        route_identity=route_identity,
        now_iso=NOW,
    )
    assert same_content_revision.revision_id == revision.revision_id


def test_append_branch_contract_revision_preserves_explicit_id_and_chains_hash() -> None:
    conn = _runtime_conn()
    context = _contract_revision_test_context("T-explicit-revision")
    route_identity = {
        "route_id": "route-explicit",
        "route_context_hash": "sha256:route-explicit",
        "prompt_contract_id": "rprompt-explicit",
        "prompt_contract_hash": "sha256:prompt-explicit",
    }
    runtime_context_id = branch_runtime_context_id(PROJECT_ID, context.task_id)

    first = append_branch_contract_revision(
        conn,
        context,
        revision_id="crev-explicit-compat",
        payload={"target_files": ["agent/tests/test_parallel_branch_runtime.py"]},
        route_identity=route_identity,
        now_iso=NOW,
    )
    expected_first_hash = _expected_contract_revision_hash(
        context,
        runtime_context_id=runtime_context_id,
        payload={"target_files": ["agent/tests/test_parallel_branch_runtime.py"]},
        route_identity=route_identity,
        revision_id="crev-explicit-compat",
    )
    assert first.revision_id == "crev-explicit-compat"
    assert first.payload["revision_receipt"]["canonical_visible_contract_text_hash"] == expected_first_hash

    second_payload = {"target_files": ["agent/governance/parallel_branch_runtime.py"]}
    second = append_branch_contract_revision(
        conn,
        context,
        payload=second_payload,
        route_identity=route_identity,
        now_iso="2026-05-16T12:01:00Z",
    )
    expected_second_hash = _expected_contract_revision_hash(
        context,
        runtime_context_id=runtime_context_id,
        payload=second_payload,
        route_identity=route_identity,
        previous_revision_hash=expected_first_hash,
    )

    assert second.revision_id == expected_second_hash
    assert second.payload["revision_receipt"]["previous_revision_hash"] == expected_first_hash
    latest = get_latest_branch_contract_revision(conn, PROJECT_ID, runtime_context_id)
    assert latest is not None
    assert latest.revision_id == second.revision_id


def test_runtime_context_current_view_and_gate_inputs_report_missing_fields() -> None:
    context = _runtime_projection_context()
    current = build_runtime_context_current_view(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
        },
        generated_at=NOW,
    )
    gate_inputs = build_runtime_context_gate_inputs_view(current)

    assert current["schema_version"] == RUNTIME_CONTEXT_CURRENT_SCHEMA_VERSION
    assert current["source_boundaries"]["raw_source_data_copied"] is False
    assert current["identity"]["runtime_context_id"].startswith("mfrctx-")
    assert current["evidence_refs"]["branch_runtime"]["producer"] == (
        "parallel_branch_runtime"
    )
    assert current["route_identity"]["route_context_hash"] == (
        "sha256:route-runtime-context"
    )
    assert gate_inputs["schema_version"] == RUNTIME_CONTEXT_GATE_INPUTS_SCHEMA_VERSION
    assert gate_inputs["status"] == "missing_required_fields"

    missing = {
        (item["gate"], item["field"]): item for item in gate_inputs["missing"]
    }
    prompt_hash = missing[("dispatch", "prompt_contract_hash")]
    assert prompt_hash["expected_source"] == (
        "route_prompt_contract.prompt_contract_hash"
    )
    assert prompt_hash["producer"] == "route_prompt_contract"
    assert prompt_hash["consumer"] == (
        "mf_subagent_contract.validate_mf_subagent_dispatch_gate"
    )
    assert ("startup", "target_files") in missing


def test_runtime_context_lane_plan_fold_is_deterministic_and_reports_missing() -> None:
    events = [
        {
            "event_id": "evt-startup",
            "event_kind": "mf_subagent_startup",
            "task_id": "mf-sub-runtime-context",
            "created_at": "2026-05-16T12:03:00Z",
            "payload": {"status": "passed"},
        },
        {
            "event_id": "evt-other",
            "event_kind": "close_ready",
            "task_id": "other-lane",
            "created_at": "2026-05-16T12:04:00Z",
        },
        {
            "event_id": "evt-route",
            "event_kind": "route_context",
            "task_id": "mf-sub-runtime-context",
            "created_at": "2026-05-16T12:01:00Z",
        },
        {
            "event_id": "evt-dispatch",
            "event_kind": "mf_subagent_dispatch",
            "task_id": "mf-sub-runtime-context",
            "created_at": "2026-05-16T12:02:00Z",
        },
    ]

    projection = build_runtime_context_lane_plan_view(
        list(reversed(events)),
        required_clauses=[
            {"id": "route_context", "expected_source": "route_context"},
            "bounded_implementation_worker_dispatch",
            "mf_subagent_startup",
            "close_ready",
        ],
        lane_id="mf-sub-runtime-context",
        generated_at=NOW,
    )
    reordered = build_runtime_context_lane_plan_view(
        events,
        required_clauses=[
            {"id": "route_context", "expected_source": "route_context"},
            "bounded_implementation_worker_dispatch",
            "mf_subagent_startup",
            "close_ready",
        ],
        lane_id="mf-sub-runtime-context",
        generated_at="2026-05-16T12:30:00Z",
    )

    assert projection["schema_version"] == RUNTIME_CONTEXT_LANE_FOLD_SCHEMA_VERSION
    assert projection["current_state"] == {
        "status": "missing_required_clauses",
        "fulfilled_count": 3,
        "missing_count": 1,
        "blocking_count": 0,
        "next_missing_clause": "close_ready",
        "last_event_kind": "mf_subagent_startup",
        "last_event_ref": "evt-startup",
    }
    assert [item["clause"] for item in projection["fulfilled"]] == [
        "route_context",
        "bounded_implementation_worker_dispatch",
        "mf_subagent_startup",
    ]
    assert projection["fulfilled"][0]["expected_source"] == "route_context"
    assert projection["missing"] == [
        {"clause": "close_ready", "status": "missing"}
    ]
    assert runtime_context_content_hash(projection) == runtime_context_content_hash(
        reordered
    )


def test_runtime_context_projection_embeds_event_sourced_lane_plan() -> None:
    context = _runtime_projection_context()
    projection = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        target_files=["agent/governance/parallel_branch_runtime.py"],
        timeline_events=[
            {
                "event_id": "evt-route",
                "event_kind": "route_context",
                "task_id": "mf-sub-runtime-context",
                "created_at": "2026-05-16T12:01:00Z",
            },
            {
                "event_id": "evt-precheck",
                "event_kind": "route_action_precheck",
                "task_id": "mf-sub-runtime-context",
                "created_at": "2026-05-16T12:02:00Z",
            },
            {
                "event_id": "evt-dispatch",
                "event_kind": "bounded_implementation_worker_dispatch",
                "task_id": "mf-sub-runtime-context",
                "created_at": "2026-05-16T12:03:00Z",
            },
            {
                "event_id": "evt-startup",
                "event_kind": "mf_subagent_startup",
                "task_id": "mf-sub-runtime-context",
                "created_at": "2026-05-16T12:04:00Z",
            },
        ],
        lane_required_clauses=[
            "route_context",
            "route_action_precheck",
            "bounded_implementation_worker_dispatch",
            "mf_subagent_startup",
            "runtime_context_read_receipt",
        ],
        generated_at=NOW,
    ).to_dict()

    current_lane_plan = projection["views"]["current"]["lane_plan"]
    worker_lane_plan = projection["views"]["worker_view"]["lane_plan"]
    assert current_lane_plan["schema_version"] == RUNTIME_CONTEXT_LANE_FOLD_SCHEMA_VERSION
    assert current_lane_plan["current_state"]["fulfilled_count"] == 4
    assert current_lane_plan["current_state"]["next_missing_clause"] == (
        "runtime_context_read_receipt"
    )
    assert current_lane_plan["missing"] == [
        {"clause": "runtime_context_read_receipt", "status": "missing"}
    ]
    assert worker_lane_plan == current_lane_plan


def test_runtime_context_action_plan_makes_route_token_missing_actionable() -> None:
    context = _runtime_projection_context()
    projection = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
        },
        target_files=["agent/governance/parallel_branch_runtime.py"],
        generated_at=NOW,
    ).to_dict()

    action_plan = projection["views"]["action_plan"]
    control_plane = projection["views"]["control_plane"]
    worker_view = projection["views"]["worker_view"]

    assert action_plan["schema_version"] == RUNTIME_CONTEXT_ACTION_PLAN_SCHEMA_VERSION
    assert control_plane["schema_version"] == RUNTIME_CONTEXT_CONTROL_PLANE_SCHEMA_VERSION
    assert action_plan["next_legal_action"] == "refresh_route_token_ref"
    assert action_plan["route_token_action"]["status"] == "missing"
    assert action_plan["route_token_action"]["next_action"] == "refresh_route_token_ref"
    assert action_plan["route_token_action"]["entrypoint"] == {
        "method": "POST",
        "path": "/api/projects/{project_id}/observer/route-context/issue",
        "required_public_fields": [
            "backlog_id",
            "task_id",
            "target_files",
            "caller_role",
        ],
        "request_template": {
            "backlog_id": "BUG-RUNTIME-CONTEXT",
            "task_id": "mf-sub-runtime-context",
            "target_files": ["agent/governance/parallel_branch_runtime.py"],
            "caller_role": "observer",
        },
        "runtime_context_persistence": (
            "Persist route_token_ref/hash evidence only; raw route tokens "
            "must not persist in runtime context output."
        ),
    }
    assert action_plan["route_token_action"]["canonical_route_identity"] == {
        "route_id": "route-runtime-context",
        "route_context_hash": "sha256:route-runtime-context",
        "prompt_contract_id": "rprompt-runtime-context",
        "prompt_contract_hash": "sha256:prompt-runtime-context",
        "route_token_ref": "",
    }
    assert any(
        item["code"] == "route_token_missing"
        for item in action_plan["blocking_reasons"]
    )
    assert control_plane["next_legal_action"] == action_plan["next_legal_action"]
    assert worker_view["control_plane"]["route_token_action"]["status"] == "missing"


def test_runtime_context_action_plan_reports_route_token_ref_present_from_revision() -> None:
    context = _runtime_projection_context()
    projection = build_runtime_context_projection(
        context,
        contract_revision={
            "revision_id": "crev-runtime-context",
            "contract_version": "mf_parallel.v1",
            "route_identity": {
                "route_id": "route-runtime-context",
                "route_context_hash": "sha256:route-runtime-context",
                "prompt_contract_id": "rprompt-runtime-context",
                "prompt_contract_hash": "sha256:prompt-runtime-context",
                "route_token_ref": "rtok-runtime-context",
            },
            "payload": {
                "target_files": ["agent/governance/parallel_branch_runtime.py"],
            },
        },
        generated_at=NOW,
    ).to_dict()

    action_plan = projection["views"]["action_plan"]
    route_action = action_plan["route_token_action"]
    worker_route_action = projection["views"]["worker_view"]["control_plane"][
        "route_token_action"
    ]

    assert route_action["status"] == "present"
    assert route_action["next_action"] == "none"
    assert route_action["route_token_ref_present"] is True
    assert route_action["canonical_route_identity"]["route_token_ref"] == (
        "rtok-runtime-context"
    )
    assert route_action["expected_binding"]["route_token_ref"] == (
        "rtok-runtime-context"
    )
    assert worker_route_action["status"] == "present"
    assert action_plan["next_legal_action"] != "refresh_route_token_ref"


def test_runtime_context_action_plan_reports_read_receipt_hash_entrypoint() -> None:
    context = _runtime_projection_context()
    projection = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        target_files=["agent/governance/parallel_branch_runtime.py"],
        generated_at=NOW,
    ).to_dict()

    read_action = projection["views"]["action_plan"]["read_receipt_hash_action"]

    assert read_action["status"] == "missing"
    assert read_action["next_action"] == "submit_mf_subagent_read_receipt"
    assert read_action["entrypoint"] == {
        "method": "POST",
        "path": "/api/task/{project_id}/timeline",
        "event_kind": "mf_subagent_read_receipt",
        "required_payload_fields": [
            "runtime_context_id",
            "task_id",
            "parent_task_id",
            "fence_token",
            "worker_slot_id",
            "read_receipt_hash or launch_text_hash",
        ],
    }
    assert read_action["content_address_nodes"]["current"]["node_id"] == (
        f"runtime_context/{projection['runtime_context_id']}/current"
    )
    assert read_action["hash_material"]["current_view_hash"].startswith("sha256:")
    bridge = read_action["ordered_worker_startup_bridge"]
    assert bridge["schema_version"] == "runtime_context.worker_startup_bridge.v1"
    assert [step["id"] for step in bridge["steps"]] == [
        "query_runtime_contract",
        "record_read_receipt",
        "worker_graph_query",
        "implementation_and_tests",
        "transcript_self_attestation",
        "record_startup",
    ]
    read_receipt_step = bridge["steps"][1]
    assert read_receipt_step["hash_bridge"]["accepted_inputs"] == [
        "read_receipt_hash",
        "launch_text_hash",
    ]
    assert read_receipt_step["hash_bridge"]["startup_field"] == "read_receipt_hash"
    assert "observer_command_id" in read_receipt_step["required_payload_fields"]
    startup_step = bridge["steps"][-1]
    assert "worker_transcript_path" in startup_step["required_fields"]
    assert "graph_trace_ids" in startup_step["required_fields"]
    assert "close_satisfying=false" in startup_step["close_satisfying_rule"]

    with_receipt = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        timeline_refs={"read_receipt_event_ref": "timeline:read-runtime-context"},
        target_files=["agent/governance/parallel_branch_runtime.py"],
        generated_at=NOW,
    ).to_dict()

    present_action = with_receipt["views"]["action_plan"]["read_receipt_hash_action"]
    assert present_action["status"] == "present"
    assert present_action["next_action"] == "none"
    assert present_action["read_receipt_event_ref"] == "timeline:read-runtime-context"
    assert present_action["ordered_worker_startup_bridge"]["status"] == "ready"


def test_runtime_context_action_plan_projects_ordered_merge_dependency_wait() -> None:
    worker1 = MergeQueueItem(
        project_id=PROJECT_ID,
        merge_queue_id="mq-runtime-context",
        queue_item_id="mqi-worker-1",
        task_id="worker-1",
        branch_ref="refs/heads/codex/worker-1",
        queue_index=1,
        status=STATE_WAITING_DEPENDENCY,
        branch_head="head-worker-1",
    )
    worker2 = MergeQueueItem(
        project_id=PROJECT_ID,
        merge_queue_id="mq-runtime-context",
        queue_item_id="mqi-worker-2",
        task_id="worker-2",
        branch_ref="refs/heads/codex/worker-2",
        queue_index=2,
        status=STATE_MERGE_READY,
        depends_on=("worker-1",),
        branch_head="head-worker-2",
        merge_preview_id="mp-worker-2",
    )
    merge_plan = decide_merge_queue(
        [worker1, worker2],
        scenario_id="runtime-context-ordered-dependency",
    )
    worker2_decision = {
        decision.task_id: decision.to_dashboard_row()
        for decision in merge_plan.decisions
    }["worker-2"]

    projection = build_runtime_context_projection(
        _runtime_projection_context(
            task_id="worker-2",
            root_task_id="AC-RUNTIME-ACTION-PLAN-GAP-PROJECTION-20260614",
            merge_queue_id="mq-runtime-context",
            merge_preview_id="mp-worker-2",
        ),
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        contract_revision={
            "revision_id": "crev-runtime-context-merge-wait",
            "contract_version": "mf_parallel.v1",
            "payload": {
                "merge_queue_projection": worker2_decision,
                "ordered_merge_dependencies": ["worker-1"],
            },
        },
        generated_at=NOW,
    ).to_dict()

    dependency_projection = projection["views"]["action_plan"][
        "merge_dependency_projection"
    ]

    assert worker2_decision["queue_state"] == STATE_DEPENDENCY_BLOCKED
    assert dependency_projection["status"] == STATE_DEPENDENCY_BLOCKED
    assert dependency_projection["dependency_blockers"] == ["worker-1"]
    assert dependency_projection["merge_allowed"] is False
    assert dependency_projection["target_branch_mutation_allowed"] is False
    assert dependency_projection["next_action"] == "wait_for_dependency"
    assert dependency_projection["next_actions"] == [
        "wait_for_dependency",
        "merge_dependency_first",
        "do_not_merge_current_lane",
    ]
    assert projection["views"]["control_plane"]["merge_dependency_projection"] == (
        dependency_projection
    )


def test_runtime_context_action_plan_projects_dependency_after_merge_revalidation() -> None:
    worker1 = MergeQueueItem(
        project_id=PROJECT_ID,
        merge_queue_id="mq-runtime-context",
        queue_item_id="mqi-worker-1",
        task_id="worker-1",
        branch_ref="refs/heads/codex/worker-1",
        queue_index=1,
        status=STATE_MERGED,
        branch_head="head-worker-1",
        merge_commit="merge-worker-1",
        snapshot_id="scope-worker-1",
        projection_id="semproj-worker-1",
    )
    worker2 = MergeQueueItem(
        project_id=PROJECT_ID,
        merge_queue_id="mq-runtime-context",
        queue_item_id="mqi-worker-2",
        task_id="worker-2",
        branch_ref="refs/heads/codex/worker-2",
        queue_index=2,
        status=STATE_MERGE_READY,
        depends_on=("worker-1",),
        branch_head="head-worker-2",
        validated_target_head="target-before-worker-1",
        current_target_head="merge-worker-1",
        merge_preview_id="mp-worker-2-stale",
    )
    merge_plan = decide_merge_queue(
        [worker1, worker2],
        scenario_id="runtime-context-after-dependency-merge",
    )
    worker2_decision = {
        decision.task_id: decision.to_dashboard_row()
        for decision in merge_plan.decisions
    }["worker-2"]

    projection = build_runtime_context_projection(
        _runtime_projection_context(
            task_id="worker-2",
            root_task_id="AC-RUNTIME-ACTION-PLAN-GAP-PROJECTION-20260614",
            merge_queue_id="mq-runtime-context",
            merge_preview_id="mp-worker-2-stale",
        ),
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        contract_revision={
            "revision_id": "crev-runtime-context-merge-revalidate",
            "contract_version": "mf_parallel.v1",
            "payload": {
                "merge_queue_projection": worker2_decision,
                "dependency_merge_commit": "merge-worker-1",
            },
        },
        generated_at=NOW,
    ).to_dict()

    dependency_projection = projection["views"]["action_plan"][
        "merge_dependency_projection"
    ]

    assert worker2_decision["queue_state"] == STATE_STALE_AFTER_DEPENDENCY_MERGE
    assert dependency_projection["status"] in {
        STATE_MERGE_READY,
        STATE_STALE_AFTER_DEPENDENCY_MERGE,
    }
    assert dependency_projection["dependency_blockers"] == []
    assert dependency_projection["merge_preview_id"] == "mp-worker-2-stale"
    assert dependency_projection["next_action"] in {
        "revalidate",
        "refresh_merge_preview",
    }
    assert "refresh_merge_preview" in dependency_projection["next_actions"]
    assert "revalidate" in dependency_projection["next_actions"]


def test_runtime_context_action_plan_projects_close_precheck_gaps() -> None:
    projection = build_runtime_context_projection(
        _runtime_projection_context(),
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        contract_revision={
            "revision_id": "crev-runtime-context-close-precheck",
            "contract_version": "mf_parallel.v1",
            "payload": {
                "close_precheck": {
                    "route_identity_cleanup_required": True,
                    "independent_verification_required": True,
                    "route_token_action_scope_mismatch": True,
                    "target_graph_stale": True,
                    "worker_graph_query_identity_required": True,
                }
            },
        },
        generated_at=NOW,
    ).to_dict()

    close_projection = projection["views"]["action_plan"][
        "close_precheck_gap_projection"
    ]
    gap_codes = {item["code"] for item in close_projection["gaps"]}
    blocking_codes = {
        item["code"] for item in projection["views"]["action_plan"]["blocking_reasons"]
    }

    assert close_projection["status"] == "blocked"
    assert gap_codes == {
        "route_identity_cleanup_required",
        "independent_verification_required",
        "route_token_action_scope_mismatch",
        "target_graph_stale",
        "worker_graph_query_identity_required",
    }
    assert "cleanup_route_identity" in close_projection["next_actions"]
    assert "record_independent_verification" in close_projection["next_actions"]
    assert "refresh_route_token_scope" in close_projection["next_actions"]
    assert "reconcile_target_graph" in close_projection["next_actions"]
    assert "query_graph_as_mf_subagent" in close_projection["next_actions"]
    assert gap_codes <= blocking_codes


def test_runtime_context_worker_view_control_plane_is_role_scoped_without_raw_tokens() -> None:
    context = _runtime_projection_context(
        session_token_hash=mf_subagent_session_token_hash("raw-worker-session-token"),
    )
    projection = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
            "raw_route_token": "raw-route-token-secret",
        },
        contract_revision={
            "revision_id": "crev-runtime-context-role-scope",
            "contract_version": "mf_parallel.v1",
            "payload": {
                "observer_command_id": "cmd-runtime-context",
                "worker_session_token": "raw-worker-session-token",
                "fence_token": "fence-runtime-context",
            },
        },
        generated_at=NOW,
    ).to_dict()

    worker_view = projection["views"]["worker_view"]
    control_plane = worker_view["control_plane"]
    serialized_worker_view = json.dumps(worker_view, sort_keys=True)
    serialized_control_plane = json.dumps(control_plane, sort_keys=True)

    assert worker_view["role_scope"] == "mf_sub"
    assert control_plane["role_scope"] == "mf_sub"
    assert control_plane["viewer_role"] == "mf_sub"
    assert control_plane["raw_session_token_exposed"] is False
    assert control_plane["raw_route_token_exposed"] is False
    assert "raw-worker-session-token" not in serialized_worker_view
    assert "raw-worker-session-token" not in serialized_control_plane
    assert "raw-route-token-secret" not in serialized_worker_view
    assert "raw-route-token-secret" not in serialized_control_plane
    assert "fence-runtime-context" not in serialized_control_plane
    assert worker_view["capability_boundary"]["role"] == "mf_sub"


def test_runtime_context_action_plan_translates_close_blockers_for_operator() -> None:
    context = _runtime_projection_context()
    projection = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        timeline_refs={
            "startup_event_ref": "timeline:startup-runtime-context",
            "read_receipt_event_ref": "timeline:read-runtime-context",
        },
        target_files=["agent/governance/parallel_branch_runtime.py"],
        graph_trace_refs={"trace_ids": ["gqt-runtime-context"]},
        startup_gate={
            "worker_self_attesting": True,
            "worker_self_attestation": {
                "status": "passed",
                "worker_self_attesting": True,
            },
        },
        generated_at=NOW,
    ).to_dict()

    action_plan = projection["views"]["action_plan"]
    explanation = action_plan["close_blocker_explanation"]
    codes = {
        item["code"]
        for item in explanation["explanations"]
    }

    assert explanation["ready"] is False
    assert "startup_exists_but_not_close_satisfying" in codes
    assert "missing_finish_gate_ref" in codes
    assert "missing_verification_event_refs" in codes
    assert "missing_checkpoint_id" in codes
    assert any(
        "startup exists but is not close-satisfying" in item["message"]
        for item in explanation["explanations"]
    )


def test_runtime_context_action_plan_links_audit_archive_for_historical_close_blocker() -> None:
    context = _runtime_projection_context()
    projection = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        timeline_refs={
            "startup_event_ref": "timeline:startup-runtime-context",
            "read_receipt_event_ref": "timeline:read-runtime-context",
        },
        target_files=["agent/governance/parallel_branch_runtime.py"],
        graph_trace_refs={"trace_ids": ["gqt-runtime-context"]},
        generated_at=NOW,
    ).to_dict()

    action_plan = projection["views"]["action_plan"]
    control_plane = projection["views"]["control_plane"]
    audit_action = action_plan["audit_archive_action"]

    assert audit_action["schema_version"] == "runtime_context.audit_archive_action.v1"
    assert audit_action["status"] == "candidate_requires_observer_historical_classification"
    assert audit_action["next_action"] == "classify_historical_non_reconstructable"
    assert audit_action["archive_action"] == "backlog_audit_archive"
    assert audit_action["normal_close_gate_passed"] is False
    assert audit_action["close_ready_emitted"] is False
    assert audit_action["entrypoint"]["path"] == (
        "/api/backlog/{project_id}/{bug_id}/audit-archive"
    )
    assert audit_action["entrypoint"]["mcp_tool"] == "backlog_audit_archive"
    assert "BUG-RUNTIME-CONTEXT" == audit_action["entrypoint"]["request_template"]["bug_id"]
    assert control_plane["audit_archive_action"] == audit_action
    assert action_plan["next_legal_action"] != "backlog_audit_archive"


def test_runtime_context_action_plan_defers_permission_tree_hardening() -> None:
    context = _runtime_projection_context()
    projection = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        target_files=["agent/governance/parallel_branch_runtime.py"],
        generated_at=NOW,
    ).to_dict()

    action_plan = projection["views"]["action_plan"]
    missing_text = json.dumps(action_plan["missing_evidence"], sort_keys=True)

    assert action_plan["deferred_hardening"] == {
        "permission_tree": "deferred_next_layer",
        "capability_subtree": "deferred_next_layer",
        "granted_subtree_root_hash": "deferred_next_layer",
        "implemented_in_this_slice": False,
    }
    assert "permission_tree" not in missing_text
    assert "capability_subtree" not in missing_text
    assert "granted_subtree_root_hash" not in missing_text


def test_runtime_context_lane_plan_blocking_event_does_not_fulfill_clause() -> None:
    projection = build_runtime_context_lane_plan_view(
        [
            {
                "event_id": "evt-startup-failed",
                "event_kind": "mf_subagent_startup",
                "task_id": "mf-sub-runtime-context",
                "created_at": "2026-05-16T12:03:00Z",
                "status": "failed",
            }
        ],
        required_clauses=["mf_subagent_startup"],
        lane_id="mf-sub-runtime-context",
        generated_at=NOW,
    )

    assert projection["current_state"]["status"] == "blocked"
    assert projection["current_state"]["blocking_count"] == 1
    assert projection["fulfilled"] == []
    assert projection["missing"] == [
        {"clause": "mf_subagent_startup", "status": "missing"}
    ]
    assert projection["blocking_events"] == [
        {
            "event_kind": "mf_subagent_startup",
            "event_ref": "evt-startup-failed",
            "status": "failed",
            "at": "2026-05-16T12:03:00Z",
            "clauses": ["mf_subagent_startup"],
        }
    ]


def test_runtime_context_worker_view_filters_private_context_and_wrong_fence() -> None:
    context = _runtime_projection_context(checkpoint_id="ckpt-runtime-context")
    private_secret = "raw-private-memory-secret"
    other_worker_secret = "other-worker-context-secret"
    projection = build_runtime_context_projection(
        context,
        contract_revision={
            "revision_id": "crev-runtime-context",
            "contract_version": "mf_parallel.v1",
            "payload": {
                "observer_command_id": "cmd-runtime-context",
                "target_files": ["agent/governance/parallel_branch_runtime.py"],
                "acceptance_criteria": ["worker view is role filtered"],
                "raw_private_memory": private_secret,
                "other_worker_contexts": [
                    {"task_id": "other-worker", "secret": other_worker_secret}
                ],
            },
        },
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
            "visible_injection_manifest_hash": "sha256:visible-runtime-context",
            "raw_private_context": private_secret,
        },
        timeline_refs={
            "startup_event_ref": "timeline:startup",
            "read_receipt_event_ref": "timeline:read-receipt",
            "finish_event_ref": "timeline:finish",
            "verification_event_refs": ["timeline:verification"],
        },
        graph_trace_refs={
            "query_source": "mf_subagent",
            "worker_role": "mf_sub",
            "task_id": "mf-sub-runtime-context",
            "parent_task_id": "parent-runtime-context",
            "trace_ids": ["gqt-runtime-context"],
        },
        startup_gate={
            "worker_session_id": "session-runtime-context",
            "filer_principal": "session-runtime-context",
            "worker_self_attesting": True,
            "worker_self_attestation": {
                "schema_version": "worker_transcript_self_attestation.v1",
                "status": "passed",
                "worker_self_attesting": True,
                "worker_session_id": "session-runtime-context",
                "worker_transcript_path": "/tmp/transcript-runtime-context.jsonl",
                "harness_type": "codex",
                "blockers": [],
            },
        },
        finish_gate={
            "checkpoint_id": "ckpt-runtime-context",
            "event_id": "timeline:finish",
            "test_results": {"status": "passed"},
        },
        close_evidence={
            "event_id": "timeline:close-ready",
            "payload": {"graph_trace_ids": ["gqt-runtime-context"]},
        },
        generated_at=NOW,
    )
    payload = projection.to_dict()
    worker_view = payload["views"]["worker_view"]

    assert worker_view["schema_version"] == RUNTIME_CONTEXT_WORKER_VIEW_SCHEMA_VERSION
    assert worker_view["task"]["task_id"] == "mf-sub-runtime-context"
    assert worker_view["observer_command_id"] == "cmd-runtime-context"
    assert worker_view["gate_inputs"]["observer_command_id"] == "cmd-runtime-context"
    fence_hash = "sha256:" + hashlib.sha256(
        b"fence-runtime-context"
    ).hexdigest()
    assert "fence_token" not in worker_view["task"]
    assert worker_view["task"]["fence_token_hash"] == fence_hash
    assert worker_view["task"]["fence_token_redacted"] is True
    assert worker_view["graph_query_identity"]["fence_token_hash"] == fence_hash
    assert worker_view["graph_query_identity"]["fence_token_redacted"] is True
    assert "fence_token" not in worker_view["graph_query_identity"]
    boundary = payload["views"]["capability_boundary"]
    assert boundary["schema_version"] == RUNTIME_CONTEXT_CAPABILITY_BOUNDARY_SCHEMA_VERSION
    assert boundary["runtime_context_id"] == payload["runtime_context_id"]
    assert boundary["task_id"] == "mf-sub-runtime-context"
    assert boundary["role"] == "mf_sub"
    assert boundary["owned_files"] == ["agent/governance/parallel_branch_runtime.py"]
    assert boundary["target_files"] == ["agent/governance/parallel_branch_runtime.py"]
    assert boundary["fence_token_present"] is True
    assert boundary["fence_token_hash"] == fence_hash
    assert boundary["fence_token_redacted"] is True
    assert boundary["raw_session_token_exposed"] is False
    assert boundary["raw_fence_token_exposed"] is False
    assert boundary["graph_query_scope"] == {
        "query_source": "mf_subagent",
        "query_purpose": "subagent_context_build",
        "allowed_query_purposes": [
            "subagent_context_build",
            "subagent_gate_validation",
        ],
        "worker_role": "mf_sub",
        "runtime_context_id": payload["runtime_context_id"],
        "task_id": "mf-sub-runtime-context",
        "parent_task_id": "parent-runtime-context",
        "governance_project_id": PROJECT_ID,
        "target_project_id": PROJECT_ID,
        "target_project_root": "/repo",
    }
    assert boundary["capability_boundary_hash"] == runtime_context_content_hash(
        {key: value for key, value in boundary.items() if key != "capability_boundary_hash"}
    )
    assert worker_view["capability_boundary"] == boundary
    assert worker_view["capability_boundary_hash"] == boundary["capability_boundary_hash"]
    assert worker_view["control_plane"]["capability_boundary"] == boundary
    assert worker_view["control_plane"]["capability_boundary_hash"] == (
        boundary["capability_boundary_hash"]
    )
    dispatch_fence = worker_view["gate_inputs"]["gates"]["dispatch"]["fields"][
        "fence_token"
    ]
    assert dispatch_fence["value"] == "redacted"
    assert dispatch_fence["value_redacted"] is True
    assert dispatch_fence["fence_token_hash"] == fence_hash
    assert worker_view["route_identity"]["prompt_contract_hash"] == (
        "sha256:prompt-runtime-context"
    )
    assert worker_view["gate_inputs"]["status"] == "ready"
    assert worker_view["action_plan"]["schema_version"] == (
        RUNTIME_CONTEXT_ACTION_PLAN_SCHEMA_VERSION
    )
    assert worker_view["control_plane"]["schema_version"] == (
        RUNTIME_CONTEXT_CONTROL_PLANE_SCHEMA_VERSION
    )
    assert worker_view["close_gate_view"]["schema_version"] == (
        RUNTIME_CONTEXT_CLOSE_GATE_VIEW_SCHEMA_VERSION
    )
    assert worker_view["close_gate_view"]["ready"] is True
    assert worker_view["close_gate_view"]["close_ready_event_ref"] == (
        "timeline:close-ready"
    )
    assert worker_view["close_gate_view"]["evidence_refs"]["route_identity"][
        "route_context_hash"
    ] == "sha256:route-runtime-context"
    assert worker_view["close_gate_view"]["evidence_refs"]["finish_gate"][
        "payload"
    ]["event_id"] == "timeline:finish"
    assert worker_view["close_gate_view"]["evidence_refs"]["close_evidence"][
        "payload"
    ]["event_id"] == "timeline:close-ready"
    close_graph_trace = worker_view["gate_inputs"]["gates"]["close"]["fields"][
        "graph_trace_ids"
    ]
    assert close_graph_trace["producer"] == "graph_query_trace"
    assert close_graph_trace["consumer"] == "close_gate"
    assert worker_view["gate_inputs"]["evidence_refs"]["graph_trace"]["trace_ids"] == [
        "gqt-runtime-context"
    ]
    assert worker_view["privacy_boundary"]["raw_private_context_exposed"] is False
    assert worker_view["privacy_boundary"]["other_worker_contexts_exposed"] is False

    serialized = json.dumps(worker_view, sort_keys=True)
    assert private_secret not in serialized
    assert other_worker_secret not in serialized
    assert "fence-runtime-context" not in serialized

    with pytest.raises(BranchRuntimeFenceError):
        build_runtime_context_projection(
            context,
            route_identity={
                "route_id": "route-runtime-context",
                "route_context_hash": "sha256:route-runtime-context",
                "prompt_contract_id": "rprompt-runtime-context",
                "prompt_contract_hash": "sha256:prompt-runtime-context",
                "route_token_ref": "rtok-runtime-context",
                "visible_injection_manifest_hash": "sha256:visible-runtime-context",
            },
            fence_token="stale-fence",
        )


def test_runtime_context_close_gate_view_derives_same_lineage_timeline_evidence() -> None:
    context = _runtime_projection_context()
    runtime_context_id = branch_runtime_context_id(PROJECT_ID, context.task_id)
    projection = build_runtime_context_projection(
        context,
        contract_revision={
            "revision_id": "crev-derived-runtime-context",
            "contract_version": "mf_parallel.v1",
            "payload": {
                "observer_command_id": "cmd-derived-runtime-context",
                "target_files": ["agent/governance/parallel_branch_runtime.py"],
            },
        },
        startup_gate={
            "worker_self_attesting": True,
            "worker_self_attestation": {
                "schema_version": "worker_transcript_self_attestation.v1",
                "status": "passed",
                "worker_self_attesting": True,
            },
        },
        timeline_events=[
            {
                "id": 4417,
                "event_kind": "graph_query_trace",
                "actor": "observer",
                "status": "passed",
                "payload": {
                    "runtime_context_id": runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                    "graph_trace_ids": ["gqt-observer-substitute"],
                    "query_source": "observer",
                },
            },
            {
                "id": 4418,
                "event_kind": "graph_query_trace",
                "actor": "worker-runtime-context",
                "status": "passed",
                "payload": {
                    "runtime_context_id": runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                    "graph_trace_ids": ["gqt-actor-only"],
                },
            },
            {
                "id": 4419,
                "event_kind": "graph_query_trace",
                "actor": "qa",
                "status": "passed",
                "payload": {
                    "runtime_context_id": runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                    "worker_role": "mf_sub",
                    "graph_trace_ids": ["gqt-qa-substitute"],
                    "query_source": "mf_subagent",
                },
            },
            {
                "id": 4420,
                "event_kind": "graph_query_trace",
                "actor": "worker-runtime-context",
                "status": "passed",
                "payload": {
                    "backlog_id": context.backlog_id,
                    "parent_task_id": context.root_task_id,
                    "worker_role": "mf_sub",
                    "graph_trace_ids": ["gqt-backlog-only"],
                    "query_source": "mf_subagent",
                },
            },
            {
                "id": 4421,
                "event_kind": "graph_query_trace",
                "actor": "worker-other-lane",
                "status": "passed",
                "payload": {
                    "backlog_id": context.backlog_id,
                    "task_id": "other-worker-lane",
                    "parent_task_id": context.root_task_id,
                    "worker_role": "mf_sub",
                    "graph_trace_ids": ["gqt-other-task"],
                    "query_source": "mf_subagent",
                },
            },
            {
                "id": 4422,
                "event_kind": "graph_query_trace",
                "actor": "worker-other-runtime",
                "status": "passed",
                "payload": {
                    "backlog_id": context.backlog_id,
                    "runtime_context_id": "mfrctx-other-lane",
                    "parent_task_id": context.root_task_id,
                    "worker_role": "mf_sub",
                    "graph_trace_ids": ["gqt-other-runtime-context"],
                    "query_source": "mf_subagent",
                },
            },
            {
                "id": 4423,
                "event_kind": "graph_query_trace",
                "actor": "worker-other-fence",
                "status": "passed",
                "payload": {
                    "backlog_id": context.backlog_id,
                    "parent_task_id": context.root_task_id,
                    "fence_token": "other-fence",
                    "worker_role": "mf_sub",
                    "graph_trace_ids": ["gqt-other-fence"],
                    "query_source": "mf_subagent",
                },
            },
            {
                "id": 4425,
                "event_kind": "mf_subagent_read_receipt",
                "actor": "worker-runtime-context",
                "status": "accepted",
                "payload": {
                    "runtime_context_id": runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                    "fence_token": context.fence_token,
                    "route_context_hash": "sha256:timeline-route",
                    "prompt_contract_id": "rprompt-timeline",
                    "prompt_contract_hash": "sha256:timeline-prompt",
                    "route_token_ref": "rtok-timeline",
                    "graph_trace_ids": ["gqt-worker-read"],
                    "query_source": "mf_subagent",
                },
            },
            {
                "id": 4426,
                "event_kind": "implementation",
                "actor": "worker-runtime-context",
                "status": "passed",
                "payload": {
                    "runtime_context_id": runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                    "worker_role": "mf_sub",
                    "graph_trace_ids": ["gqt-worker-implementation"],
                },
            },
            {
                "id": 4427,
                "event_kind": "finish_gate",
                "actor": "worker-runtime-context",
                "status": "passed",
                "payload": {
                    "runtime_context_id": runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                    "checkpoint_id": "ckpt-derived-runtime-context",
                },
            },
            {
                "id": 4428,
                "event_kind": "verification",
                "actor": "qa",
                "status": "passed",
                "payload": {
                    "runtime_context_id": runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                },
            },
            {
                "id": 4429,
                "event_kind": "close_ready",
                "actor": "worker-runtime-context",
                "status": "accepted",
                "payload": {
                    "runtime_context_id": runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                },
            },
        ],
        generated_at=NOW,
    )

    close_gate = projection.to_dict()["views"]["close_gate_view"]

    assert close_gate["route_context_hash"] == "sha256:timeline-route"
    assert close_gate["prompt_contract_hash"] == "sha256:timeline-prompt"
    assert close_gate["route_token_ref"] == "rtok-timeline"
    assert close_gate["finish_gate_ref"] == "4427"
    assert close_gate["checkpoint_id"] == "ckpt-derived-runtime-context"
    assert close_gate["graph_trace_ids"] == [
        "gqt-worker-read",
        "gqt-worker-implementation",
    ]
    assert "gqt-observer-substitute" not in close_gate["graph_trace_ids"]
    assert "gqt-actor-only" not in close_gate["graph_trace_ids"]
    assert "gqt-qa-substitute" not in close_gate["graph_trace_ids"]
    assert "gqt-backlog-only" not in close_gate["graph_trace_ids"]
    assert "gqt-other-task" not in close_gate["graph_trace_ids"]
    assert "gqt-other-runtime-context" not in close_gate["graph_trace_ids"]
    assert "gqt-other-fence" not in close_gate["graph_trace_ids"]
    assert close_gate["ready"] is True


def test_runtime_context_projection_content_address_is_stable_and_redacted() -> None:
    context = _runtime_projection_context(checkpoint_id="ckpt-runtime-context")
    private_secret = "raw-private-memory-secret"
    projection = build_runtime_context_projection(
        context,
        contract_revision={
            "revision_id": "crev-runtime-context",
            "contract_version": "mf_parallel.v1",
            "payload": {
                "observer_command_id": "cmd-runtime-context",
                "target_files": ["agent/governance/parallel_branch_runtime.py"],
                "raw_private_memory": private_secret,
                "launch_text": "do-not-hash-launch-text",
                "worker_nonce": "do-not-hash-worker-nonce",
                "subtree": "do-not-hash-subtree",
            },
        },
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        generated_at=NOW,
    ).to_dict()
    later_projection = build_runtime_context_projection(
        context,
        contract_revision={
            "revision_id": "crev-runtime-context",
            "contract_version": "mf_parallel.v1",
            "payload": {
                "observer_command_id": "cmd-runtime-context",
                "target_files": ["agent/governance/parallel_branch_runtime.py"],
                "raw_private_memory": private_secret,
                "launch_text": "do-not-hash-launch-text",
                "worker_nonce": "do-not-hash-worker-nonce",
                "subtree": "do-not-hash-subtree",
            },
        },
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        generated_at="2026-05-16T12:01:00Z",
    ).to_dict()

    content_address = projection["content_address"]
    assert content_address["schema_version"] == RUNTIME_CONTEXT_CONTENT_ADDRESS_SCHEMA_VERSION
    assert content_address["projection_hash"].startswith("sha256:")
    assert content_address["root_hash"] == content_address["projection_hash"]
    assert set(content_address["nodes"]) == {
        "action_plan",
        "capability_boundary",
        "control_plane",
        "current",
        "gate_inputs",
        "worker_view",
        "close_gate_view",
    }
    worker_node = content_address["nodes"]["worker_view"]
    assert worker_node["hash"] == worker_node["node_hash"]
    assert worker_node["view_hash"] == runtime_context_content_hash(
        projection["views"]["worker_view"]
    )
    assert projection["views"]["current"]["generated_at"] != (
        later_projection["views"]["current"]["generated_at"]
    )
    assert projection["views"]["gate_inputs"]["generated_at"] != (
        later_projection["views"]["gate_inputs"]["generated_at"]
    )
    assert content_address == later_projection["content_address"]
    assert runtime_context_content_hash(projection["views"]["current"]) == (
        runtime_context_content_hash(later_projection["views"]["current"])
    )
    assert runtime_context_content_hash(projection["views"]["gate_inputs"]) == (
        runtime_context_content_hash(later_projection["views"]["gate_inputs"])
    )
    assert runtime_context_content_hash({"b": 2, "a": 1}) == runtime_context_content_hash(
        {"a": 1, "b": 2}
    )
    assert runtime_context_content_hash({"fence_token": "one"}) == (
        runtime_context_content_hash({"fence_token": "two"})
    )
    scoped_content_address = runtime_context_filter_content_address(
        content_address,
        runtime_context_audit_nodes_for_views(projection, "worker_view"),
    )
    assert set(scoped_content_address["nodes"]) == {
        "capability_boundary",
        "worker_view",
    }
    assert set(scoped_content_address["view_hashes"]) == {
        "capability_boundary",
        "worker_view",
    }

    serialized_content_address = json.dumps(content_address, sort_keys=True)
    assert private_secret not in serialized_content_address
    assert "fence-runtime-context" not in serialized_content_address
    assert "do-not-hash-launch-text" not in serialized_content_address
    assert "do-not-hash-worker-nonce" not in serialized_content_address
    assert "do-not-hash-subtree" not in serialized_content_address


def test_redact_runtime_context_payload_replaces_authorized_raw_capability_material() -> None:
    raw_fence = "synthetic-raw-fence-runtime-contract-secret"
    raw_session = "synthetic-raw-session-runtime-contract-secret"
    raw_private = "synthetic-private-runtime-contract-secret"
    payload = {
        "runtime_context": {
            "task_id": "mf-sub-redaction",
            "fence_token": raw_fence,
            "target_fences": [raw_fence],
        },
        "contract": {
            "graph_query": {
                "required_context_fields": ["task_id", "fence_token"],
            },
            "protected_timeline_append": {
                "scope": {"fence_token": raw_fence},
            },
        },
        "session_token": raw_session,
        "nested": {"raw_private_token": raw_private},
    }

    redacted = redact_runtime_context_payload(
        payload,
        raw_secrets=[raw_fence, raw_session, raw_private],
    )
    serialized = json.dumps(redacted, sort_keys=True)

    for secret in (raw_fence, raw_session, raw_private):
        assert secret not in serialized
    assert redacted["runtime_context"]["fence_token"] == "redacted"
    assert redacted["runtime_context"]["fence_token_redacted"] is True
    assert redacted["runtime_context"]["fence_token_hash"] == (
        "sha256:" + hashlib.sha256(raw_fence.encode("utf-8")).hexdigest()
    )
    assert redacted["runtime_context"]["target_fences"] == ["redacted"]
    assert redacted["contract"]["graph_query"]["required_context_fields"] == [
        "task_id",
        "fence_token",
    ]
    assert redacted["session_token"] == "redacted"
    assert redacted["session_token_redacted"] is True
    assert redacted["nested"]["raw_private_token"] == "redacted"
    assert redacted["nested"]["raw_private_token_redacted"] is True


def test_runtime_context_access_audit_persists_hashes_not_raw_tokens() -> None:
    conn = _runtime_conn()
    context = _runtime_projection_context(checkpoint_id="ckpt-runtime-context")
    projection = build_runtime_context_projection(context, generated_at=NOW).to_dict()
    nodes_read = runtime_context_audit_nodes_for_views(projection, "worker_view")

    audit = record_runtime_context_access_audit(
        conn,
        project_id=PROJECT_ID,
        runtime_context_id=projection["runtime_context_id"],
        task_id=context.task_id,
        session={
            "principal_id": "worker-principal",
            "session_id": "worker-session",
            "role": "mf_sub",
        },
        role="mf_sub",
        view_name="worker_view",
        projection_hash=projection["content_address"]["projection_hash"],
        nodes_read=nodes_read,
        metadata={
            "endpoint": "runtime-context.current-state",
            "session_token": "raw-worker-session-token",
            "fence_token": "raw-fence-token",
            "launch_text": "raw-launch-text",
            "worker_nonce": "raw-worker-nonce",
            "subtree": "raw-subtree",
        },
        now_iso=NOW,
    )

    row = conn.execute(
        """
        SELECT * FROM parallel_branch_runtime_access_audit
        WHERE audit_id = ?
        """,
        (audit["audit_id"],),
    ).fetchone()
    assert row is not None
    assert audit["schema_version"] == RUNTIME_CONTEXT_ACCESS_AUDIT_SCHEMA_VERSION
    assert row["role"] == "mf_sub"
    assert row["view_name"] == "worker_view"
    assert row["projection_hash"] == projection["content_address"]["projection_hash"]
    stored_nodes = json.loads(row["nodes_read_json"])
    assert {node["view"] for node in stored_nodes} == {
        "capability_boundary",
        "worker_view",
    }
    assert {
        projection["content_address"]["nodes"][node["view"]]["view_hash"]
        for node in stored_nodes
    } == {node["view_hash"] for node in stored_nodes}
    nodes_json = row["nodes_read_json"]
    metadata_json = row["metadata_json"]
    for secret in (
        "raw-worker-session-token",
        "raw-fence-token",
        "raw-launch-text",
        "raw-worker-nonce",
        "raw-subtree",
    ):
        assert secret not in nodes_json
        assert secret not in metadata_json
    metadata = json.loads(metadata_json)
    assert metadata["session_token_redacted"] is True
    assert metadata["fence_token_redacted"] is True
    assert metadata["launch_text_redacted"] is True
    assert metadata["worker_nonce_redacted"] is True
    assert metadata["subtree_redacted"] is True


def test_worker_transcript_mf_sub_startup_records_real_worker_identity_and_token_hash(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup"
    worktree.mkdir(parents=True)
    base_commit, head_commit = _ensure_startup_git_worktree(worktree)
    context = BranchTaskRuntimeContext(
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        root_task_id="parent-startup",
        stage_task_id="mf-sub-startup",
        backlog_id="BUG-STARTUP",
        worker_id="worker-startup",
        agent_id="agent-startup",
        branch_ref="refs/heads/codex/mf-sub-startup",
        status=STATE_WORKTREE_READY,
        fence_token="fence-startup",
        worktree_path=str(worktree),
        base_commit=base_commit,
        head_commit=head_commit,
        target_head_commit="target-startup",
        merge_queue_id="mq-startup",
        session_token_hash=mf_subagent_session_token_hash("secret-worker-session-token"),
    )
    upsert_branch_context(
        conn,
        context,
        now_iso=NOW,
    )
    append_branch_contract_revision(
        conn,
        context,
        route_identity={
            "route_id": "route-startup",
            "route_context_hash": "sha256:route-startup",
            "prompt_contract_id": "rprompt-startup",
            "prompt_contract_hash": "sha256:prompt-startup",
            "route_token_ref": "rtok-startup",
            "visible_injection_manifest_hash": "sha256:visible-startup",
        },
        now_iso=NOW,
    )
    _insert_startup_graph_trace(conn)

    result = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(str(worktree)),
        now_iso=NOW,
    )

    gate = result["startup_gate"]
    saved = get_branch_context(conn, PROJECT_ID, "mf-sub-startup")
    assert result["ok"] is True
    assert saved is not None
    assert saved.status == STATE_RUNNING
    assert saved.head_commit == head_commit
    assert saved.worker_slot_id == "worker-startup"
    assert saved.actual_host_worker_id == "worker-startup"
    assert saved.allocation_owner == "agent-startup"
    assert gate["actual_startup_recorded"] is True
    assert gate["worker_slot_id"] == "worker-startup"
    assert gate["actual_host_worker_id"] == "worker-startup"
    assert gate["allocation_owner"] == "agent-startup"
    assert gate["observer_allocation_owner"] == "agent-startup"
    assert gate["session_token_hash"].startswith("sha256:")
    assert gate["session_token_persisted"] is False
    assert gate["runtime_context_id"] == branch_runtime_context_id(PROJECT_ID, "mf-sub-startup")
    assert gate["observer_command_id"] == "cmd-startup"
    assert gate["route_id"] == "route-startup"
    assert gate["visible_injection_manifest_hash"] == "sha256:visible-startup"
    assert gate["owned_files"] == ["agent/governance/parallel_branch_runtime.py"]
    assert gate["read_receipt_hash"] == "sha256:read-startup"
    assert gate["worker_self_attesting"] is True
    assert gate["worker_self_attestation"]["status"] == "passed"
    assert gate["close_satisfying"] is True
    assert gate["worker_self_attestation"]["worker_session_id"] == "codex-session-startup"
    assert gate["identity_join"]["runtime_context_id_matches"] is True
    assert gate["identity_join"]["route_identity_matches_latest_contract"] is True
    assert "secret-worker-session-token" not in str(result)
    assert result["timeline_event"]["event_kind"] == "mf_subagent_startup"
    assert result["timeline_event"]["actor"] == "codex-session-startup"
    assert result["timeline_event"]["payload"]["mf_subagent_startup_gate"] == gate

    accepted = validate_mf_subagent_graph_query_identity(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        parent_task_id="parent-startup",
        worker_role="mf_sub",
        fence_token="fence-startup",
        session_token="secret-worker-session-token",
    )
    assert accepted.task_id == "mf-sub-startup"

    for supplied_token in ("", "wrong-worker-session-token"):
        with pytest.raises(BranchRuntimeFenceError):
            validate_mf_subagent_graph_query_identity(
                conn,
                project_id=PROJECT_ID,
                task_id="mf-sub-startup",
                parent_task_id="parent-startup",
                worker_role="mf_sub",
                fence_token="fence-startup",
                session_token=supplied_token,
            )


def test_startup_bridges_launch_text_hash_read_receipt_without_close_satisfying(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-launch-receipt"
    worktree.mkdir(parents=True)
    _insert_startup_context(conn, str(worktree))

    result = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(
            str(worktree),
            read_receipt_hash="",
            launch_text_hash="sha256:launch-text-startup",
            worker_transcript_path="",
            worker_session_id="",
            harness_type="",
        ),
        now_iso=NOW,
    )

    gate = result["startup_gate"]
    assert result["ok"] is True
    assert gate["read_receipt_hash"] == "sha256:launch-text-startup"
    assert gate["read_receipt_hash_source"] == "payload.launch_text_hash"
    assert gate["identity_join"]["read_receipt_lineage_present"] is True
    assert gate["session_token_evidence_type"] == "server_verified"
    assert gate["worker_self_attesting"] is False
    assert gate["close_satisfying"] is False
    assert "missing_worker_session_id" in gate["worker_self_attestation"]["blockers"]
    assert "missing_worker_transcript_path" in gate["worker_self_attestation"]["blockers"]


def test_mf_sub_startup_rejects_same_owner_self_filled_unissued_session_token(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-unissued-token"
    worktree.mkdir(parents=True)
    base_commit, head_commit = _ensure_startup_git_worktree(worktree)
    context = BranchTaskRuntimeContext(
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        root_task_id="parent-startup",
        stage_task_id="mf-sub-startup",
        backlog_id="BUG-STARTUP",
        worker_id="worker-startup",
        worker_slot_id="worker-startup",
        agent_id="agent-startup",
        allocation_owner="agent-startup",
        branch_ref="refs/heads/codex/mf-sub-startup",
        status=STATE_WORKTREE_READY,
        fence_token="fence-startup",
        worktree_path=str(worktree),
        base_commit=base_commit,
        head_commit=head_commit,
        target_head_commit="target-startup",
        merge_queue_id="mq-startup",
    )
    upsert_branch_context(conn, context, now_iso=NOW)
    append_branch_contract_revision(
        conn,
        context,
        route_identity={
            "route_id": "route-startup",
            "route_context_hash": "sha256:route-startup",
            "prompt_contract_id": "rprompt-startup",
            "prompt_contract_hash": "sha256:prompt-startup",
            "route_token_ref": "rtok-startup",
            "visible_injection_manifest_hash": "sha256:visible-startup",
        },
        now_iso=NOW,
    )

    result = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(str(worktree)),
        now_iso=NOW,
    )

    assert result["ok"] is False
    assert result["blocker_id"] == "session_token_not_server_issued"


def test_worker_transcript_allows_4178_prompt_text_without_structured_playback(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-4178-prompt-text"
    worktree.mkdir(parents=True)
    _insert_startup_context(conn, str(worktree))
    payload = _startup_payload(str(worktree), worker_session_id="codex-session-prompt-note")
    transcript_path = Path(str(payload["worker_transcript_path"]))
    with transcript_path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "prompt_note",
                    "content": (
                        "QA text mentions event-4178 as a regression example, "
                        "but this is not startup identity evidence."
                    ),
                }
            )
            + "\n"
        )

    result = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=payload,
        now_iso=NOW,
    )

    gate = result["startup_gate"]
    assert gate["worker_self_attesting"] is True
    assert gate["worker_self_attestation"]["status"] == "passed"
    assert gate["worker_self_attestation"]["known_bad_playback_4178"] is False
    assert "known_bad_playback_4178_shape" not in gate["worker_self_attestation"]["blockers"]


def test_worker_transcript_blocks_structured_4178_playback_identity(tmp_path) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-structured-4178"
    worktree.mkdir(parents=True)
    _insert_startup_context(conn, str(worktree))

    result = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(
            str(worktree),
            worker_session_id="codex-session-structured-replay",
            host_startup_id="multi_agent_v1:4178-b",
        ),
        now_iso=NOW,
    )

    gate = result["startup_gate"]
    assert gate["worker_self_attesting"] is False
    assert gate["worker_self_attestation"]["known_bad_playback_4178"] is True
    assert (
        "known_bad_playback_4178_shape"
        in gate["worker_self_attestation"]["blockers"]
    )


def test_worker_transcript_mf_sub_startup_marks_missing_transcript_not_self_attesting(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-missing-transcript"
    worktree.mkdir(parents=True)
    _insert_startup_context(conn, str(worktree))

    result = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(
            str(worktree),
            worker_session_id="fabricated-session",
            worker_transcript_path=str(worktree / "missing.jsonl"),
        ),
        now_iso=NOW,
    )

    gate = result["startup_gate"]
    assert result["ok"] is True
    assert gate["worker_self_attesting"] is False
    assert gate["close_satisfying"] is False
    assert "worker_transcript_path_unresolvable" in gate["worker_self_attestation"]["blockers"]

    mismatched_path = worktree / "mismatched.jsonl"
    mismatched_path.write_text(
        json.dumps(
            {
                "session_id": "different-session",
                "event": "mf_subagent graph_query implementation",
                "task_id": "mf-sub-startup",
                "runtime_context_id": branch_runtime_context_id(
                    PROJECT_ID, "mf-sub-startup"
                ),
                "fence_token": "fence-startup",
                "worktree_path": str(worktree),
                "branch": "refs/heads/codex/mf-sub-startup",
                "changed_files": ["agent/governance/parallel_branch_runtime.py"],
                "trace_ids": ["gqt-startup"],
                "observer_command_id": "cmd-startup",
                "read_receipt_hash": "sha256:read-startup",
                "read_receipt_event_id": "2873",
                "route_token_ref": "rtok-startup",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    mismatched = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(
            str(worktree),
            worker_session_id="fabricated-session",
            worker_transcript_path=str(mismatched_path),
        ),
        now_iso=NOW,
    )
    assert mismatched["startup_gate"]["worker_self_attesting"] is False
    assert (
        "worker_session_id_not_in_transcript"
        in mismatched["startup_gate"]["worker_self_attestation"]["blockers"]
    )


def test_worker_transcript_mf_sub_startup_marks_idle_or_no_graph_trace_not_self_attesting(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-idle"
    worktree.mkdir(parents=True)
    _insert_startup_context(conn, str(worktree))

    idle = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(
            str(worktree),
            changed_files=[],
            head_commit=_git(worktree, "rev-list", "--max-parents=0", "HEAD"),
        ),
        now_iso=NOW,
    )
    assert idle["startup_gate"]["worker_self_attesting"] is False
    assert "no_owned_files_diff" in idle["startup_gate"]["worker_self_attestation"]["blockers"]

    no_graph = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(str(worktree), graph_trace_ids=[]),
        now_iso=NOW,
    )
    assert no_graph["startup_gate"]["worker_self_attesting"] is False
    assert (
        "missing_mf_subagent_graph_trace_ids"
        in no_graph["startup_gate"]["worker_self_attestation"]["blockers"]
    )


def test_worker_transcript_forged_strings_missing_db_trace_and_diff_mismatch_rejects(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-forged"
    worktree.mkdir(parents=True)
    _insert_startup_context(conn, str(worktree))

    result = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(
            str(worktree),
            changed_files=["agent/governance/server.py"],
            graph_trace_ids=["gqt-forged-transcript-only"],
        ),
        now_iso=NOW,
    )

    blockers = result["startup_gate"]["worker_self_attestation"]["blockers"]
    assert result["ok"] is True
    assert result["startup_gate"]["worker_self_attesting"] is False
    assert any(
        blocker.startswith("claimed_changed_files_do_not_match_git_diff")
        for blocker in blockers
    )
    assert "graph_trace_ids_not_db_verified" in blockers
    assert "graph_trace_missing_from_db:gqt-forged-transcript-only" in blockers


def _finish_gate_context(task_id: str = "mf-sub-finish") -> BranchTaskRuntimeContext:
    return BranchTaskRuntimeContext(
        project_id=PROJECT_ID,
        task_id=task_id,
        root_task_id=f"parent-{task_id}",
        stage_task_id=task_id,
        backlog_id="BUG-FINISH",
        worker_id=f"worker-{task_id}",
        worker_slot_id=f"worker-{task_id}",
        agent_id=f"agent-{task_id}",
        allocation_owner=f"agent-{task_id}",
        branch_ref=f"refs/heads/codex/{task_id}",
        status=STATE_WORKTREE_READY,
        fence_token=f"fence-{task_id}",
        worktree_path=f"/tmp/nonexistent-{task_id}",
        base_commit=f"base-{task_id}",
        head_commit=f"head-{task_id}",
        target_head_commit=f"target-{task_id}",
        merge_queue_id=f"mq-{task_id}",
    )


def _finish_gate_payload(
    context: BranchTaskRuntimeContext,
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "project_id": context.project_id,
        "task_id": context.task_id,
        "backlog_id": context.backlog_id,
        "branch_ref": context.branch_ref,
        "worktree_path": context.worktree_path,
        "base_commit": context.base_commit,
        "target_head_commit": context.target_head_commit,
        "merge_queue_id": context.merge_queue_id,
        "status": "review_ready",
        "changed_files": ["agent/governance/parallel_branch_runtime.py"],
        "test_results": {"status": "passed"},
        "checkpoint_id": f"ckpt-{context.task_id}",
        "fence_token": context.fence_token,
        "head_commit": context.head_commit,
        "observer_command_id": f"cmd-{context.task_id}",
        "read_receipt_hash": f"sha256:rr-{context.task_id}",
        "read_receipt_event_id": f"rr-{context.task_id}",
    }
    payload.update(overrides)
    return payload


def _finish_startup_gate(
    context: BranchTaskRuntimeContext,
    *,
    include_worker_fields: bool = True,
) -> dict[str, object]:
    gate: dict[str, object] = {
        "schema_version": "mf_subagent_startup_gate.v1",
        "gate_kind": "mf_subagent.startup",
        "status": "passed",
        "ok": True,
        "allowed": True,
        "bounded": True,
        "close_satisfying": True,
        "actual_startup_recorded": True,
        "agent_id_match_mode": "same_as_allocation_owner",
        "session_token_evidence_type": "server_verified",
        "session_token_hash": "sha256:finish-token",
        "session_token_present": True,
        "agent_id": context.allocation_owner,
        "allocation_owner": context.allocation_owner,
        "worker_role": "mf_sub",
        "task_id": context.task_id,
        "worker_slot_id": context.worker_slot_id,
        "runtime_context_id": branch_runtime_context_id(
            context.project_id,
            context.task_id,
        ),
        "fence_token": context.fence_token,
        "actual_cwd": context.worktree_path,
        "actual_git_root": context.worktree_path,
        "worktree_path": context.worktree_path,
        "branch_ref": context.branch_ref,
        "head_commit": context.head_commit,
        "observer_command_id": f"cmd-{context.task_id}",
        "read_receipt_event_id": f"rr-{context.task_id}",
    }
    if include_worker_fields:
        gate.update(
            {
                "worker_session_id": f"session-{context.task_id}",
                "filer_principal": f"session-{context.task_id}",
                "worker_transcript_path": f"/tmp/transcript-{context.task_id}.jsonl",
                "harness_type": "codex",
                "worker_self_attesting": True,
                "self_attesting": True,
                "worker_self_attestation": {
                    "schema_version": "worker_transcript_self_attestation.v1",
                    "status": "passed",
                    "worker_self_attesting": True,
                    "worker_session_id": f"session-{context.task_id}",
                    "worker_transcript_path": f"/tmp/transcript-{context.task_id}.jsonl",
                    "harness_type": "codex",
                    "blockers": [],
                },
            }
        )
    else:
        gate["worker_self_attestation"] = {
            "schema_version": "worker_transcript_self_attestation.v1",
            "status": "passed",
            "worker_self_attesting": True,
            "blockers": [],
        }
    return gate


def _finish_startup_event(startup_gate: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": "startup-event-finish",
        "event_kind": "mf_subagent_startup",
        "event_type": "mf_subagent.startup",
        "phase": "startup_gate",
        "status": "passed",
        "payload": {"mf_subagent_startup_gate": dict(startup_gate)},
    }


@pytest.mark.parametrize(
    "startup_key",
    ["bounded_startup_evidence", "startup_evidence", "mf_subagent_startup_gate"],
)
def test_finish_gate_ignores_caller_startup_evidence_injection(startup_key) -> None:
    context = _finish_gate_context(f"finish-injection-{startup_key}")
    payload = _finish_gate_payload(
        context,
        **{startup_key: _finish_startup_gate(context)},
    )

    with pytest.raises(
        MfSubagentContractError,
        match="actual mf_subagent_startup evidence",
    ):
        validate_mf_subagent_finish_gate(payload, context=context)


def test_finish_gate_rejects_db_startup_event_missing_worker_transcript_fields() -> None:
    context = _finish_gate_context("finish-missing-worker-fields")
    startup_gate = _finish_startup_gate(context, include_worker_fields=False)
    payload = _finish_gate_payload(
        context,
        real_startup_events=[_finish_startup_event(startup_gate)],
    )

    with pytest.raises(
        MfSubagentContractError,
        match="missing_worker_session_id",
    ):
        validate_mf_subagent_finish_gate(payload, context=context)


@pytest.mark.parametrize(
    ("principal", "expected_blocker"),
    [
        ("", "missing_filer_principal"),
        ("mf_sub", "startup_filer_principal_not_worker_session"),
    ],
)
def test_finish_gate_rejects_db_startup_event_without_worker_session_filer(
    principal,
    expected_blocker,
) -> None:
    context = _finish_gate_context(f"finish-principal-{principal or 'missing'}")
    startup_gate = _finish_startup_gate(context)
    if principal:
        startup_gate["filer_principal"] = principal
    else:
        startup_gate.pop("filer_principal", None)
    payload = _finish_gate_payload(
        context,
        real_startup_events=[_finish_startup_event(startup_gate)],
    )

    with pytest.raises(
        MfSubagentContractError,
        match=expected_blocker,
    ):
        validate_mf_subagent_finish_gate(payload, context=context)


def test_mf_sub_startup_blocks_route_identity_mismatch_with_contract_revision(tmp_path) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-route-mismatch"
    worktree.mkdir(parents=True)
    base_commit, head_commit = _ensure_startup_git_worktree(worktree)
    context = BranchTaskRuntimeContext(
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        root_task_id="parent-startup",
        stage_task_id="mf-sub-startup",
        backlog_id="BUG-STARTUP",
        worker_id="worker-startup",
        agent_id="agent-startup",
        branch_ref="refs/heads/codex/mf-sub-startup",
        status=STATE_WORKTREE_READY,
        fence_token="fence-startup",
        worktree_path=str(worktree),
        base_commit=base_commit,
        head_commit=head_commit,
        target_head_commit="target-startup",
        merge_queue_id="mq-startup",
    )
    upsert_branch_context(conn, context, now_iso=NOW)
    append_branch_contract_revision(
        conn,
        context,
        route_identity={
            "route_id": "route-startup",
            "route_context_hash": "sha256:route-startup",
            "prompt_contract_id": "rprompt-startup",
            "prompt_contract_hash": "sha256:prompt-startup",
            "route_token_ref": "rtok-startup",
            "visible_injection_manifest_hash": "sha256:visible-startup",
        },
        now_iso=NOW,
    )

    result = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(str(worktree), route_context_hash="sha256:other-route"),
        now_iso=NOW,
    )

    assert result["ok"] is False
    assert result["blocker_id"] == "route_identity_mismatch"
    mismatches = result["details"]["route_identity_mismatches"]
    assert mismatches == [
        {
            "field": "route_context_hash",
            "expected": "sha256:route-startup",
            "actual": "sha256:other-route",
        }
    ]


def test_mf_sub_startup_blocks_allocation_only_and_stale_fence(tmp_path) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-blocked"
    worktree.mkdir(parents=True)
    base_commit, head_commit = _ensure_startup_git_worktree(worktree)
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="mf-sub-startup",
            root_task_id="parent-startup",
            stage_task_id="mf-sub-startup",
            backlog_id="BUG-STARTUP",
            worker_id="worker-startup",
            agent_id="agent-startup",
            branch_ref="refs/heads/codex/mf-sub-startup",
            status=STATE_WORKTREE_READY,
            fence_token="fence-startup",
            worktree_path=str(worktree),
            base_commit=base_commit,
            head_commit=head_commit,
            target_head_commit="target-startup",
            merge_queue_id="mq-startup",
        ),
        now_iso=NOW,
    )

    allocation_only = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload={"task_id": "mf-sub-startup"},
        now_iso=NOW,
    )
    stale_fence = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(str(worktree), fence_token="stale-fence"),
        now_iso=NOW,
    )
    wrong_slot = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(str(worktree), worker_slot_id="other-slot"),
        now_iso=NOW,
    )
    wrong_cwd = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(str(worktree), actual_cwd=str(worktree / "subdir")),
        now_iso=NOW,
    )
    wrong_agent = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(str(worktree), agent_id="other-agent"),
        now_iso=NOW,
    )
    wrong_runtime_context = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(str(worktree), runtime_context_id="mfrctx-other"),
        now_iso=NOW,
    )
    missing_merge_payload = _startup_payload(str(worktree))
    missing_merge_payload.pop("merge_queue_id")
    missing_merge_queue = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=missing_merge_payload,
        now_iso=NOW,
    )
    missing_identity_payload = _startup_payload(str(worktree))
    for key in ("worker_id", "agent_id", "base_commit", "target_head_commit"):
        missing_identity_payload.pop(key)
    missing_identity = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=missing_identity_payload,
        now_iso=NOW,
    )

    assert allocation_only["ok"] is False
    assert allocation_only["blocker_id"] == "no_truthful_bounded_mf_sub_startup_surface_available"
    assert allocation_only["terminal_dispatch_blocker"] is True
    assert "actual_cwd" in allocation_only["missing"]
    assert stale_fence["ok"] is False
    assert stale_fence["blocker_id"] == "fence_invalidated_or_unknown"
    assert wrong_slot["ok"] is False
    assert wrong_slot["blocker_id"] == "worker_slot_id_mismatch"
    assert wrong_cwd["ok"] is False
    assert wrong_cwd["blocker_id"] == "actual_cwd_mismatch"
    assert wrong_agent["ok"] is False
    assert wrong_agent["blocker_id"] == "agent_id_mismatch"
    assert wrong_runtime_context["ok"] is False
    assert wrong_runtime_context["blocker_id"] == "runtime_context_id_mismatch"
    assert missing_merge_queue["ok"] is False
    assert missing_merge_queue["blocker_id"] == (
        "no_truthful_bounded_mf_sub_startup_surface_available"
    )
    assert "merge_queue_id" in missing_merge_queue["missing"]
    assert missing_identity["ok"] is False
    assert missing_identity["blocker_id"] == (
        "no_truthful_bounded_mf_sub_startup_surface_available"
    )
    for key in ("actual_host_worker_id", "agent_id", "base_commit", "target_head_commit"):
        assert key in missing_identity["missing"]

    with pytest.raises(BranchRuntimeFenceError):
        validate_mf_subagent_graph_query_identity(
            conn,
            project_id=PROJECT_ID,
            task_id="mf-sub-startup",
            parent_task_id="parent-startup",
            worker_role="",
            fence_token="fence-startup",
        )


def test_mf_sub_startup_accepts_host_adapter_agent_id_mismatch_with_surrogate(tmp_path) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-host-adapter"
    worktree.mkdir(parents=True)
    base_commit, head_commit = _ensure_startup_git_worktree(worktree)
    context = BranchTaskRuntimeContext(
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        root_task_id="parent-startup",
        stage_task_id="mf-sub-startup",
        backlog_id="BUG-STARTUP",
        worker_id="worker-startup",
        agent_id="fallback_observer_cli_takeover",
        branch_ref="refs/heads/codex/mf-sub-startup",
        status=STATE_WORKTREE_READY,
        fence_token="fence-startup",
        worktree_path=str(worktree),
        base_commit=base_commit,
        head_commit=head_commit,
        target_head_commit="target-startup",
        merge_queue_id="mq-startup",
    )
    upsert_branch_context(
        conn,
        context,
        now_iso=NOW,
    )
    append_branch_contract_revision(
        conn,
        context,
        payload={
            "registered_host_adapter_spawn": {
                "schema_version": "mf_subagent_host_adapter_spawn_identity.v1",
                "source": "test_registered_host_adapter_spawn",
                "runtime_context_id": branch_runtime_context_id(
                    PROJECT_ID,
                    "mf-sub-startup",
                ),
                "task_id": "mf-sub-startup",
                "worker_slot_id": "worker-startup",
                "agent_id": "codex-exec-pid-19807",
                "actual_host_worker_id": "codex-host-worker-19807",
                "host_startup_id": "host-startup-19807",
                "host_session_id": "host-startup-19807",
                "session_token_surrogate": "host-adapter:codex-exec-pid-19807",
            }
        },
        route_identity={
            "route_id": "route-startup",
            "route_context_hash": "sha256:route-startup",
            "prompt_contract_id": "rprompt-startup",
            "prompt_contract_hash": "sha256:prompt-startup",
            "route_token_ref": "rtok-startup",
            "visible_injection_manifest_hash": "sha256:visible-startup",
        },
        now_iso=NOW,
    )

    result = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(
            str(worktree),
            worker_slot_id="worker-startup",
            actual_host_worker_id="codex-host-worker-19807",
            agent_id="codex-exec-pid-19807",
            session_token="",
            session_token_surrogate="host-adapter:codex-exec-pid-19807",
            startup_source="codex_cli_host_adapter",
            host_startup_id="host-startup-19807",
        ),
        now_iso=NOW,
    )

    gate = result["startup_gate"]
    saved = get_branch_context(conn, PROJECT_ID, "mf-sub-startup")
    assert result["ok"] is True
    assert saved is not None
    assert saved.agent_id == "fallback_observer_cli_takeover"
    assert saved.worker_id == "worker-startup"
    assert saved.worker_slot_id == "worker-startup"
    assert saved.actual_host_worker_id == "codex-host-worker-19807"
    assert saved.host_startup_id == "host-startup-19807"
    assert gate["agent_id"] == "codex-exec-pid-19807"
    assert gate["expected_agent_id"] == "fallback_observer_cli_takeover"
    assert gate["worker_id"] == "worker-startup"
    assert gate["worker_slot_id"] == "worker-startup"
    assert gate["actual_host_worker_id"] == "codex-host-worker-19807"
    assert gate["agent_id_match_mode"] == "host_adapter_startup_token_surrogate"
    assert gate["host_adapter_startup_token_accepted"] is True
    assert gate["same_as_expected_worker"] is False


def test_mf_sub_startup_accepts_host_startup_id_matching_registered_host_session(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-host-session"
    worktree.mkdir(parents=True)
    base_commit, head_commit = _ensure_startup_git_worktree(worktree)
    context = BranchTaskRuntimeContext(
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        root_task_id="parent-startup",
        stage_task_id="mf-sub-startup",
        backlog_id="BUG-STARTUP",
        worker_id="worker-startup",
        agent_id="allocation-owner",
        allocation_owner="allocation-owner",
        branch_ref="refs/heads/codex/mf-sub-startup",
        status=STATE_WORKTREE_READY,
        fence_token="fence-startup",
        worktree_path=str(worktree),
        base_commit=base_commit,
        head_commit=head_commit,
        target_head_commit="target-startup",
        merge_queue_id="mq-startup",
    )
    upsert_branch_context(conn, context, now_iso=NOW)
    append_branch_contract_revision(
        conn,
        context,
        payload={
            "registered_host_adapter_spawn": {
                "schema_version": "mf_subagent_host_adapter_spawn_identity.v1",
                "source": "test_registered_host_session_only",
                "runtime_context_id": branch_runtime_context_id(
                    PROJECT_ID,
                    "mf-sub-startup",
                ),
                "task_id": "mf-sub-startup",
                "worker_slot_id": "worker-startup",
                "agent_id": "host-session-agent",
                "actual_host_worker_id": "host-session-worker",
                "host_session_id": "registered-host-session-only",
            }
        },
        route_identity={
            "route_id": "route-startup",
            "route_context_hash": "sha256:route-startup",
            "prompt_contract_id": "rprompt-startup",
            "prompt_contract_hash": "sha256:prompt-startup",
            "route_token_ref": "rtok-startup",
            "visible_injection_manifest_hash": "sha256:visible-startup",
        },
        now_iso=NOW,
    )

    result = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(
            str(worktree),
            worker_slot_id="worker-startup",
            actual_host_worker_id="host-session-worker",
            agent_id="host-session-agent",
            session_token="",
            session_token_surrogate="",
            startup_source="codex_cli_host_adapter",
            host_startup_id="registered-host-session-only",
        ),
        now_iso=NOW,
    )

    assert result["ok"] is True
    gate = result["startup_gate"]
    assert gate["host_adapter_startup_token_accepted"] is True
    assert gate["agent_id_match_mode"] == "host_adapter_startup_token_surrogate"
    assert gate["host_startup_id"] == "registered-host-session-only"


def test_mf_sub_startup_rejects_multi_agent_prefix_replay_without_registration(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-event-4178"
    worktree.mkdir(parents=True)
    base_commit, head_commit = _ensure_startup_git_worktree(worktree)
    context = BranchTaskRuntimeContext(
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        root_task_id="parent-startup",
        stage_task_id="mf-sub-startup",
        backlog_id="BUG-STARTUP",
        worker_id="worker-startup",
        agent_id="agent-startup",
        allocation_owner="agent-startup",
        branch_ref="refs/heads/codex/mf-sub-startup",
        status=STATE_WORKTREE_READY,
        fence_token="fence-startup",
        worktree_path=str(worktree),
        base_commit=base_commit,
        head_commit=head_commit,
        target_head_commit="target-startup",
        merge_queue_id="mq-startup",
    )
    upsert_branch_context(conn, context, now_iso=NOW)

    base_payload = _startup_payload(
        str(worktree),
        agent_id="codex-multi-agent-4178",
        session_token="same-event-4178-session-token",
    )
    attempts = (
        base_payload | {"host_startup_id": "codex-multi-agent-4178-a"},
        base_payload | {"host_startup_id": "codex-multi-agent-4178-b"},
        base_payload | {"host_startup_id": "multi_agent_v1:4178-b"},
    )

    results = [
        record_mf_subagent_startup(
            conn,
            project_id=PROJECT_ID,
            task_id="mf-sub-startup",
            payload=payload,
            now_iso=NOW,
        )
        for payload in attempts
    ]

    for result in results:
        assert result["ok"] is False
        assert result["blocker_id"] == "agent_id_mismatch"
        event = result["timeline_event"]
        assert event["event_kind"] == "mf_subagent_startup_refusal"
        refusal = event["payload"]["mf_subagent_startup_refusal"]
        assert refusal["blocker_id"] == "agent_id_mismatch"
        assert refusal["agent_id"] == "codex-multi-agent-4178"
        assert refusal["allocation_owner"] == "agent-startup"
        assert refusal["runtime_context_id"] == branch_runtime_context_id(
            PROJECT_ID,
            "mf-sub-startup",
        )
        assert refusal["route_id"] == "route-startup"
        assert refusal["route_context_hash"] == "sha256:route-startup"
        assert refusal["prompt_contract_id"] == "rprompt-startup"
        assert refusal["prompt_contract_hash"] == "sha256:prompt-startup"
        assert "same-event-4178-session-token" not in json.dumps(
            event,
            sort_keys=True,
        )


def test_mf_sub_startup_rejects_agent_only_registered_identity_echoed_as_host_startup(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-agent-only-registration"
    worktree.mkdir(parents=True)
    base_commit, head_commit = _ensure_startup_git_worktree(worktree)
    context = BranchTaskRuntimeContext(
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        root_task_id="parent-startup",
        stage_task_id="mf-sub-startup",
        backlog_id="BUG-STARTUP",
        worker_id="worker-startup",
        agent_id="allocation-owner",
        allocation_owner="allocation-owner",
        branch_ref="refs/heads/codex/mf-sub-startup",
        status=STATE_WORKTREE_READY,
        fence_token="fence-startup",
        worktree_path=str(worktree),
        base_commit=base_commit,
        head_commit=head_commit,
        target_head_commit="target-startup",
        merge_queue_id="mq-startup",
    )
    upsert_branch_context(conn, context, now_iso=NOW)
    append_branch_contract_revision(
        conn,
        context,
        payload={
            "registered_host_adapter_spawn": {
                "schema_version": "mf_subagent_host_adapter_spawn_identity.v1",
                "source": "test_under_specified_host_adapter_spawn",
                "agent_id": "codex-agent-only",
            }
        },
        now_iso=NOW,
    )

    result = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(
            str(worktree),
            agent_id="codex-agent-only",
            session_token="agent-only-session-token",
            host_startup_id="codex-agent-only",
        ),
        now_iso=NOW,
    )

    assert result["ok"] is False
    assert result["blocker_id"] == "agent_id_mismatch"
    refusal = result["timeline_event"]["payload"]["mf_subagent_startup_refusal"]
    assert refusal["host_startup_id"] == "codex-agent-only"
    assert refusal["agent_id"] == "codex-agent-only"
    assert refusal["allocation_owner"] == "allocation-owner"
    assert refusal["registered_host_adapter_spawn_present"] is False


def test_mf_sub_graph_query_accepts_target_project_with_governance_fence(tmp_path) -> None:
    conn = _runtime_conn()
    target_root = tmp_path / "target-project"
    target_root.mkdir()
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id="aming-claw",
            governance_project_id="aming-claw",
            target_project_id="judgment-brain",
            target_project_root=str(target_root),
            task_id="mf-sub-cross-project",
            root_task_id="parent-cross-project",
            stage_task_id="mf-sub-cross-project",
            backlog_id="BUG-CROSS-PROJECT",
            worker_id="worker-slot-cross-project",
            worker_slot_id="worker-slot-cross-project",
            branch_ref="refs/heads/codex/mf-sub-cross-project",
            status=STATE_WORKTREE_READY,
            fence_token="fence-cross-project",
            worktree_path=str(tmp_path / "worker"),
        ),
        now_iso=NOW,
    )

    accepted = validate_mf_subagent_graph_query_identity(
        conn,
        project_id="judgment-brain",
        governance_project_id="aming-claw",
        target_project_id="judgment-brain",
        target_project_root=str(target_root),
        task_id="mf-sub-cross-project",
        parent_task_id="parent-cross-project",
        worker_role="mf_sub",
        fence_token="fence-cross-project",
    )

    assert accepted.project_id == "aming-claw"
    assert accepted.target_project_id == "judgment-brain"


def _pb001_contexts(
    fixture: PB001RestartFixtureProject | None = None,
) -> list[BranchTaskRuntimeContext]:
    return [
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T1",
            batch_id=BATCH_ID,
            backlog_id="OPT-PB001-T1",
            branch_ref=_pb001_branch_ref("T1", fixture),
            status=STATE_MERGED,
            merge_queue_id="merge-001",
            base_commit=_pb001_base_commit("T1", fixture),
            head_commit=_pb001_head_commit("T1", fixture),
            target_head_commit=_pb001_target_head(fixture),
            snapshot_id="scope-base",
            projection_id="semproj-base",
            fence_token="fence-T1",
        ),
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T2",
            batch_id=BATCH_ID,
            backlog_id="OPT-PB001-T2",
            branch_ref=_pb001_branch_ref("T2", fixture),
            status=STATE_MERGE_FAILED,
            depends_on=("T1",),
            base_commit=_pb001_base_commit("T2", fixture),
            head_commit=_pb001_head_commit("T2", fixture),
            target_head_commit=_pb001_target_head(fixture),
            fence_token="fence-T2",
        ),
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T3",
            batch_id=BATCH_ID,
            backlog_id="OPT-PB001-T3",
            branch_ref=_pb001_branch_ref("T3", fixture),
            status="running",
            depends_on=("T1",),
            attempt=1,
            lease_id="lease-T3",
            lease_expires_at=EXPIRED,
            fence_token="fence-old-T3",
            checkpoint_id="checkpoint-T3",
            replay_source="checkpoint",
            base_commit=_pb001_base_commit("T3", fixture),
            head_commit=_pb001_head_commit("T3", fixture),
            target_head_commit=_pb001_target_head(fixture),
            snapshot_id="scope-T3",
            projection_id="semproj-T3",
        ),
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T4",
            batch_id=BATCH_ID,
            backlog_id="OPT-PB001-T4",
            branch_ref=_pb001_branch_ref("T4", fixture),
            status="queued_for_merge",
            depends_on=("T2",),
            merge_queue_id="merge-004",
            base_commit=_pb001_base_commit("T4", fixture),
            head_commit=_pb001_head_commit("T4", fixture),
            target_head_commit=_pb001_target_head(fixture),
            fence_token="fence-T4",
        ),
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T5",
            batch_id=BATCH_ID,
            backlog_id="OPT-PB001-T5",
            branch_ref=_pb001_branch_ref("T5", fixture),
            status="running",
            depends_on=("T3",),
            attempt=2,
            lease_id="lease-T5",
            lease_expires_at=EXPIRED,
            fence_token="fence-old-T5",
            checkpoint_id="checkpoint-T5",
            replay_source="checkpoint",
            base_commit=_pb001_base_commit("T5", fixture),
            head_commit=_pb001_head_commit("T5", fixture),
            target_head_commit=_pb001_target_head(fixture),
            snapshot_id="scope-T5",
            projection_id="semproj-T5",
        ),
    ]


def _persist_pb001_contexts(
    conn: sqlite3.Connection,
    fixture: PB001RestartFixtureProject | None = None,
) -> None:
    for context in _pb001_contexts(fixture):
        upsert_branch_context(conn, context, now_iso=NOW)


def test_pb001_machine_restart_recovery_decisions() -> None:
    """PB-001: T1 merged, T2 failed, T4 queued, T3/T5 expired after restart."""
    plan = decide_restart_recovery(_pb001_tasks())
    decisions = _by_task(plan)

    assert plan.scenario_id == "PB-001"
    assert decisions["T1"].recovery_state == STATE_MERGED
    assert decisions["T1"].action == ACTION_LEAVE_MERGED

    assert decisions["T2"].recovery_state == STATE_MERGE_FAILED
    assert decisions["T2"].action == ACTION_OBSERVER_DECISION_REQUIRED
    assert decisions["T2"].recovery_actions == ("fix_or_rebase", "abandon", "rollback_batch")

    assert decisions["T3"].recovery_state == STATE_RECLAIMABLE
    assert decisions["T3"].action == ACTION_RECLAIM_FROM_CHECKPOINT
    assert decisions["T3"].checkpoint_id == "checkpoint-T3"

    assert decisions["T4"].recovery_state == STATE_DEPENDENCY_BLOCKED
    assert decisions["T4"].action == ACTION_WAIT_FOR_DEPENDENCY
    assert decisions["T4"].dependency_blockers == ("T2",)

    assert decisions["T5"].recovery_state == STATE_RECLAIMABLE
    assert decisions["T5"].action == ACTION_RECLAIM_AFTER_DEPENDENCY
    assert decisions["T5"].dependency_blockers == ("T3",)
    assert decisions["T5"].checkpoint_id == "checkpoint-T5"


def test_pb001_retains_branches_and_blocks_cleanup_until_unresolved_work_finishes() -> None:
    plan = decide_restart_recovery(_pb001_tasks())

    assert plan.cleanup_allowed is False
    assert plan.retained_branch_refs == tuple(task.branch_ref for task in _pb001_tasks())
    assert {row["task_id"] for row in plan.dashboard_rows} == {"T1", "T2", "T3", "T4", "T5"}

    actionable_rows = {
        row["task_id"]: row["recovery_actions"]
        for row in plan.dashboard_rows
        if row["recovery_actions"]
    }
    assert actionable_rows["T2"] == ["fix_or_rebase", "abandon", "rollback_batch"]
    assert actionable_rows["T3"] == ["reclaim", "replay_from_checkpoint"]
    assert actionable_rows["T4"] == ["wait_for_dependency", "revalidate_after_dependency"]
    assert actionable_rows["T5"] == [
        "wait_for_dependency",
        "reclaim",
        "replay_from_checkpoint",
    ]


def test_pb001_only_merged_task_can_activate_target_graph_or_semantic_projection() -> None:
    plan = decide_restart_recovery(_pb001_tasks())
    decisions = _by_task(plan)

    assert decisions["T1"].target_graph_activation_allowed is True
    assert decisions["T1"].target_semantic_activation_allowed is True

    assert plan.target_graph_activation_blocked_for == ("T2", "T3", "T4", "T5")
    assert plan.target_semantic_activation_blocked_for == ("T2", "T3", "T4", "T5")


def test_branch_runtime_schema_is_in_governance_migration() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    _ensure_schema(conn)

    assert SCHEMA_VERSION >= 38
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?, ?, ?)",
        (
            "parallel_branch_runtime_contexts",
            "parallel_branch_merge_queue_items",
            "parallel_branch_batch_runtimes",
            "parallel_branch_batch_items",
        ),
    ).fetchall()
    assert {row["name"] for row in rows} == {
        "parallel_branch_runtime_contexts",
        "parallel_branch_merge_queue_items",
        "parallel_branch_batch_runtimes",
        "parallel_branch_batch_items",
    }


def test_pb001_recovery_rehydrates_replay_ready_contexts_from_generated_project(
    tmp_path,
) -> None:
    fixture = create_pb001_restart_fixture_project(tmp_path)
    for task_id in PB001_TASK_IDS:
        branch = fixture.task_branches[task_id]
        actual = subprocess.run(
            ["git", "rev-parse", "--verify", branch.branch_ref],
            cwd=fixture.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert actual == branch.head_commit

    conn = _runtime_conn()
    _persist_pb001_contexts(conn, fixture)

    recovered = recover_expired_branch_contexts(conn, PROJECT_ID, now_iso=NOW)
    assert [context.task_id for context in recovered] == ["T3", "T5"]

    t3_context = get_branch_context(conn, PROJECT_ID, "T3")
    t5_context = get_branch_context(conn, PROJECT_ID, "T5")
    assert t3_context is not None
    assert t5_context is not None
    assert t3_context.status == STATE_RECLAIMABLE
    assert t5_context.status == STATE_RECLAIMABLE
    assert t3_context.attempt == 2
    assert t5_context.attempt == 3
    assert t3_context.checkpoint_id == "checkpoint-T3"
    assert t5_context.checkpoint_id == "checkpoint-T5"
    assert t3_context.fence_token != "fence-old-T3"
    assert t5_context.fence_token != "fence-old-T5"
    assert t3_context.head_commit == fixture.task_branches["T3"].head_commit
    assert t5_context.head_commit == fixture.task_branches["T5"].head_commit
    t4_context = get_branch_context(conn, PROJECT_ID, "T4")
    assert t4_context is not None
    assert t4_context.base_commit == fixture.task_branches["T4"].base_commit
    assert t4_context.head_commit == fixture.task_branches["T4"].head_commit

    contexts_after_restart = list_branch_contexts(conn, PROJECT_ID, batch_id=BATCH_ID)
    runtime_tasks = runtime_tasks_from_contexts(contexts_after_restart, now_iso=NOW)
    plan = decide_restart_recovery(runtime_tasks)
    decisions = _by_task(plan)

    assert plan.retained_branch_refs == tuple(
        fixture.task_branches[task_id].branch_ref for task_id in PB001_TASK_IDS
    )
    assert decisions["T3"].action == ACTION_RECLAIM_FROM_CHECKPOINT
    assert decisions["T3"].checkpoint_id == "checkpoint-T3"
    assert decisions["T3"].replay_source == "checkpoint"

    assert decisions["T5"].action == ACTION_RECLAIM_AFTER_DEPENDENCY
    assert decisions["T5"].dependency_blockers == ("T3",)
    assert decisions["T5"].checkpoint_id == "checkpoint-T5"
    assert decisions["T5"].replay_source == "checkpoint"


def test_branch_runtime_rejects_stale_fence_after_reclaim() -> None:
    conn = _runtime_conn()
    _persist_pb001_contexts(conn)

    recover_expired_branch_contexts(conn, PROJECT_ID, now_iso=NOW)

    with pytest.raises(BranchRuntimeFenceError):
        record_branch_checkpoint(
            conn,
            project_id=PROJECT_ID,
            task_id="T3",
            checkpoint_id="checkpoint-stale",
            fence_token="fence-old-T3",
            now_iso=NOW,
        )

    current = get_branch_context(conn, PROJECT_ID, "T3")
    assert current is not None
    updated = record_branch_checkpoint(
        conn,
        project_id=PROJECT_ID,
        task_id="T3",
        checkpoint_id="checkpoint-T3-after-reclaim",
        fence_token=current.fence_token,
        head_commit="head-T3-after-reclaim",
        now_iso=NOW,
    )

    assert updated.checkpoint_id == "checkpoint-T3-after-reclaim"
    assert updated.replay_source == "checkpoint"
    assert updated.head_commit == "head-T3-after-reclaim"


def test_pb007_chain_stage_identity_round_trips_without_running_chain() -> None:
    conn = _runtime_conn()
    context = branch_context_from_chain_stage(
        project_id=PROJECT_ID,
        chain_id="chain-root-1",
        root_task_id="chain-root-1",
        stage_task_id="chain-dev-2",
        stage_type="dev",
        retry_round=2,
        batch_id="PB-007",
        backlog_id="OPT-PB007",
        branch_ref="refs/heads/codex/PB007-chain-dev",
        worktree_id="worktree-PB007",
        worktree_path="/tmp/worktrees/PB007-chain-dev",
        base_commit="base-PB007",
        head_commit="head-PB007",
        target_head_commit="target-PB007",
        snapshot_id="scope-PB007",
        projection_id="semproj-PB007",
        merge_queue_id="mergeq-PB007",
        merge_preview_id="preview-PB007",
        checkpoint_id="checkpoint-PB007",
        replay_source="checkpoint",
        fence_token="fence-PB007",
    )

    saved = upsert_branch_context(conn, context, now_iso=NOW)

    assert saved.task_id == "chain-dev-2"
    assert saved.chain_id == "chain-root-1"
    assert saved.root_task_id == "chain-root-1"
    assert saved.stage_task_id == "chain-dev-2"
    assert saved.stage_type == "dev"
    assert saved.retry_round == 2
    assert saved.attempt == 3
    assert saved.branch_ref == "refs/heads/codex/PB007-chain-dev"
    assert saved.merge_queue_id == "mergeq-PB007"

    reloaded = get_branch_context(conn, PROJECT_ID, "chain-dev-2")
    assert reloaded is not None
    assert reloaded.chain_id == "chain-root-1"
    assert reloaded.retry_round == 2
    assert reloaded.to_runtime_task(now_iso=NOW).checkpoint_id == "checkpoint-PB007"


def test_mf_branch_allocation_planner_sanitizes_worker_attempt_and_persists() -> None:
    conn = _runtime_conn()
    context = plan_branch_runtime_context(
        project_id=PROJECT_ID,
        task_id="../Task 123",
        batch_id="PB-009",
        backlog_id="ARCH-PB009",
        agent_id="observer",
        worker_id="worker 0/../../x",
        workspace_root="/repo",
        attempt=2,
        base_commit="B0",
        target_head_commit="M0",
        merge_queue_id="mergeq-PB009",
        fence_token="fence-planned",
    )

    assert context.status == STATE_ALLOCATED
    assert context.branch_ref == "refs/heads/codex/task-123-attempt-2"
    assert context.worktree_id == "wt-task-123-attempt-2"
    assert context.worktree_path == "/repo/.worktrees/worker-0-x/task-123-attempt-2"
    assert context.fence_token == "fence-planned"
    assert ".." not in context.branch_ref
    assert ".." not in context.worktree_path

    saved = upsert_branch_context(conn, context, now_iso=NOW)
    reloaded = get_branch_context(conn, PROJECT_ID, "../Task 123")

    assert saved == reloaded
    assert reloaded is not None
    assert reloaded.worker_id == "worker 0/../../x"
    assert reloaded.merge_queue_id == "mergeq-PB009"


def test_mf_branch_worktree_materialization_uses_planned_identity(tmp_path) -> None:
    fixture = create_parallel_fixture_project(tmp_path)
    repo = fixture.root
    base = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    conn = _runtime_conn()
    planned = plan_branch_runtime_context(
        project_id=PROJECT_ID,
        task_id="MF Branch API",
        batch_id="PB-001",
        backlog_id="ARCH-PB-WORKTREE",
        worker_id="worker one",
        workspace_root=str(repo),
        base_commit=base,
        target_head_commit=base,
        merge_queue_id="mergeq-worktree",
    )
    upsert_branch_context(conn, planned, now_iso=NOW)

    result = materialize_branch_worktree(
        conn,
        project_id=PROJECT_ID,
        task_id="MF Branch API",
        repo_root_path=repo,
        now_iso=NOW,
    )

    context = get_branch_context(conn, PROJECT_ID, "MF Branch API")
    assert context is not None
    assert context.status == STATE_WORKTREE_READY
    assert context.branch_ref == "refs/heads/codex/mf-branch-api"
    assert context.worktree_path == str(repo / ".worktrees" / "worker-one" / "mf-branch-api")
    assert context.head_commit == base
    assert result["worktree"]["created"] is True
    assert result["branch_strategy"]["work_branch"] == "codex/mf-branch-api"
    assert result["branch_strategy"]["merge_policy"] == "merge_queue"
    assert (repo / ".worktrees" / "worker-one" / "mf-branch-api" / ".git").exists()
    assert result["worktree"]["branch_graph"]["status"] == "ready"


def test_merge_queue_enqueue_uses_current_fence_and_updates_context() -> None:
    conn = _runtime_conn()
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T-merge",
            batch_id="PB-002",
            branch_ref="refs/heads/codex/t-merge",
            status=STATE_WORKTREE_READY,
            fence_token="fence-current",
            base_commit="base-merge",
            head_commit="head-merge",
            target_head_commit="target-merge",
            snapshot_id="scope-merge",
            projection_id="semproj-merge",
        ),
        now_iso=NOW,
    )

    with pytest.raises(BranchRuntimeFenceError):
        queue_merge_item_for_branch_context(
            conn,
            project_id=PROJECT_ID,
            task_id="T-merge",
            merge_queue_id="mergeq-PB002",
            fence_token="fence-stale",
            now_iso=NOW,
        )

    queued = queue_merge_item_for_branch_context(
        conn,
        project_id=PROJECT_ID,
        task_id="T-merge",
        merge_queue_id="mergeq-PB002",
        queue_index=2,
        fence_token="fence-current",
        hard_depends_on=("T-foundation",),
        merge_preview_id="preview-merge",
        now_iso=NOW,
    )

    context = get_branch_context(conn, PROJECT_ID, "T-merge")
    assert context is not None
    assert context.status == "queued_for_merge"
    assert context.merge_queue_id == "mergeq-PB002"
    assert context.merge_preview_id == "preview-merge"
    assert queued["queue_item"]["branch_ref"] == "refs/heads/codex/t-merge"
    assert queued["queue_item"]["hard_depends_on"] == ["T-foundation"]
    assert queued["queue_item"]["snapshot_id"] == "scope-merge"


def test_pb012_branch_contexts_are_isolated_by_project_and_batch() -> None:
    conn = _runtime_conn()
    shared_task_id = "shared-task"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id="project-a",
            task_id=shared_task_id,
            batch_id="batch-a",
            branch_ref="refs/heads/codex/project-a-shared-task",
            status=STATE_RUNNING,
            fence_token="fence-a",
        ),
        now_iso=NOW,
    )
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id="project-b",
            task_id=shared_task_id,
            batch_id="batch-b",
            branch_ref="refs/heads/codex/project-b-shared-task",
            status=STATE_MERGED,
            fence_token="fence-b",
        ),
        now_iso=NOW,
    )

    project_a = get_branch_context(conn, "project-a", shared_task_id)
    project_b = get_branch_context(conn, "project-b", shared_task_id)

    assert project_a is not None
    assert project_b is not None
    assert project_a.branch_ref == "refs/heads/codex/project-a-shared-task"
    assert project_b.branch_ref == "refs/heads/codex/project-b-shared-task"
    assert list_branch_contexts(conn, "project-a", batch_id="batch-a") == [project_a]
    assert list_branch_contexts(conn, "project-a", batch_id="batch-b") == []


def test_allocate_persists_merge_queue_id_for_finish_gate(tmp_path) -> None:
    """AC1-3: allocate persists merge_queue_id; startup→finish_gate round-trip succeeds.

    Also covers the migration path: a table created without the merge_queue_id column
    (simulating a pre-migration DB) must have the column added by ensure_branch_runtime_schema
    so that subsequent allocate→startup→finish_gate can complete without "context missing
    required fields: merge_queue_id" or "not merge-queue ready" errors.
    """
    # ── Part A: fresh table — allocate persists merge_queue_id ──────────────
    conn_fresh = _runtime_conn()

    task_id = "mq-finish-task"
    mq_id = "mq-test-allocate-persist-001"
    fence = "fence-mq-finish"

    # plan_branch_runtime_context mirrors what the allocate handler calls;
    # it computes branch_ref and worktree_path from the slug + workspace_root.
    context_planned = plan_branch_runtime_context(
        project_id=PROJECT_ID,
        task_id=task_id,
        root_task_id="parent-mq-finish",
        backlog_id="AC-ALLOCATE-PERSIST-MERGE-QUEUE-ID-20260609",
        worker_id="worker-mq-finish",
        worker_slot_id="worker-mq-finish",
        agent_id="agent-mq-finish",
        workspace_root=str(tmp_path),
        fence_token=fence,
        base_commit="base-mq-finish",
        target_head_commit="target-mq-finish",
        merge_queue_id=mq_id,
    )
    upsert_branch_context(conn_fresh, context_planned, now_iso=NOW)

    # AC1: persisted context must carry the merge_queue_id
    saved = get_branch_context(conn_fresh, PROJECT_ID, task_id)
    assert saved is not None
    assert saved.merge_queue_id == mq_id, (
        f"allocate did not persist merge_queue_id: got {saved.merge_queue_id!r}"
    )

    # Simulate startup (record_mf_subagent_startup requires worktree-ready state;
    # actual_cwd must match the worktree_path computed by plan_branch_runtime_context).
    from dataclasses import replace as _replace
    assigned_worktree = context_planned.worktree_path
    import os
    os.makedirs(assigned_worktree, exist_ok=True)

    ready_context = _replace(
        context_planned,
        status=STATE_WORKTREE_READY,
        session_token_hash=mf_subagent_session_token_hash("session-mq-finish"),
    )
    upsert_branch_context(conn_fresh, ready_context, now_iso=NOW)
    append_branch_contract_revision(
        conn_fresh,
        ready_context,
        route_identity={
            "route_id": "route-mq-finish",
            "route_context_hash": "sha256:route-mq-finish",
            "prompt_contract_id": "rprompt-mq-finish",
            "prompt_contract_hash": "sha256:prompt-mq-finish",
            "route_token_ref": "rtok-mq-finish",
            "visible_injection_manifest_hash": "sha256:visible-mq-finish",
        },
        now_iso=NOW,
    )
    startup_result = record_mf_subagent_startup(
        conn_fresh,
        project_id=PROJECT_ID,
        task_id=task_id,
        payload={
            "task_id": task_id,
            "parent_task_id": "parent-mq-finish",
            "worker_role": "mf_sub",
            "worker_id": "worker-mq-finish",
            "worker_slot_id": "worker-mq-finish",
            "agent_id": "agent-mq-finish",
            "session_token": "session-mq-finish",
            "runtime_context_id": branch_runtime_context_id(PROJECT_ID, task_id),
            "fence_token": fence,
            "actual_cwd": assigned_worktree,
            "actual_git_root": assigned_worktree,
            "branch": context_planned.branch_ref,
            "head_commit": "head-mq-finish",
            "base_commit": "base-mq-finish",
            "target_head_commit": "target-mq-finish",
            "merge_queue_id": mq_id,
            "owned_files": ["agent/governance/parallel_branch_runtime.py"],
            "route_id": "route-mq-finish",
            "route_context_hash": "sha256:route-mq-finish",
            "prompt_contract_id": "rprompt-mq-finish",
            "prompt_contract_hash": "sha256:prompt-mq-finish",
            "route_token_ref": "rtok-mq-finish",
            "visible_injection_manifest_hash": "sha256:visible-mq-finish",
            "observer_command_id": "cmd-mq-finish",
            "read_receipt_hash": "sha256:read-mq-finish",
            "read_receipt_event_id": "9001",
        },
        now_iso=NOW,
    )
    assert startup_result["ok"] is True, (
        f"startup failed: {startup_result.get('blocker_id')} — {startup_result}"
    )

    # AC2: after startup the stored context still has merge_queue_id
    running = get_branch_context(conn_fresh, PROJECT_ID, task_id)
    assert running is not None
    assert running.merge_queue_id == mq_id, (
        f"startup wiped merge_queue_id: got {running.merge_queue_id!r}"
    )
    # merge_queue_ready check: _require_context in mf_subagent_contract requires
    # merge_queue_id to be non-empty; assert it directly as the unit evidence.
    assert running.merge_queue_id != "", (
        "finish_gate would reject: context.merge_queue_id is empty after startup"
    )

    # record_branch_finish_gate must succeed and preserve merge_queue_id (AC3)
    finished = record_branch_finish_gate(
        conn_fresh,
        project_id=PROJECT_ID,
        task_id=task_id,
        checkpoint_id="ckpt-mq-finish",
        fence_token=fence,
        head_commit="head-mq-finish",
        now_iso=NOW,
    )
    assert finished.merge_queue_id == mq_id, (
        f"finish_gate cleared merge_queue_id: got {finished.merge_queue_id!r}"
    )

    # ── Part B: old-schema table (no merge_queue_id column) ──────────────────
    # Simulate a DB created before merge_queue_id was added by using the full
    # current schema SQL but dropping the two merge columns.  ensure_branch_runtime_schema
    # must add them via ALTER TABLE and leave existing rows readable.
    # Build an old-schema SQL without merge_queue_id / merge_preview_id columns.
    # Only the contexts table column definitions are stripped; the filter is
    # scoped to avoid accidentally removing the PRIMARY KEY / INDEX lines in
    # other tables (parallel_branch_merge_queue_items) that reference merge_queue_id.
    # A column definition line starts with whitespace, a column name, whitespace,
    # and a type keyword — we match by the DEFAULT '' suffix which is unique to
    # column defs in this DDL.
    import re as _re
    _col_def_pattern = _re.compile(
        r"^\s+(merge_queue_id|merge_preview_id)\s+TEXT NOT NULL DEFAULT ''"
    )
    from agent.governance.parallel_branch_runtime import PARALLEL_BRANCH_RUNTIME_SCHEMA_SQL
    old_schema_sql = "\n".join(
        line
        for line in PARALLEL_BRANCH_RUNTIME_SCHEMA_SQL.splitlines()
        if not _col_def_pattern.match(line)
    )

    conn_old = sqlite3.connect(":memory:")
    conn_old.row_factory = sqlite3.Row
    conn_old.executescript(old_schema_sql)
    conn_old.execute(
        """
        INSERT INTO parallel_branch_runtime_contexts
            (project_id, task_id, fence_token, branch_ref, agent_id, worker_id,
             base_commit, target_head_commit, status, created_at, updated_at)
        VALUES
            ('proj-old', 'task-old', 'fence-old', 'refs/heads/old', 'agent-old',
             'worker-old', 'base-old', 'target-old', 'worktree_ready',
             '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """
    )
    conn_old.commit()
    # Verify merge_queue_id column is missing before migration
    pre_cols = {
        str(r["name"] if hasattr(r, "keys") else r[1])
        for r in conn_old.execute("PRAGMA table_info(parallel_branch_runtime_contexts)").fetchall()
    }
    assert "merge_queue_id" not in pre_cols, "pre-condition: old table must lack merge_queue_id"

    # Run migration
    ensure_branch_runtime_schema(conn_old)

    # Column must now exist
    post_cols = {
        str(r["name"] if hasattr(r, "keys") else r[1])
        for r in conn_old.execute("PRAGMA table_info(parallel_branch_runtime_contexts)").fetchall()
    }
    assert "merge_queue_id" in post_cols, (
        "ensure_branch_runtime_schema did not add merge_queue_id column to old table"
    )
    assert "merge_preview_id" in post_cols, (
        "ensure_branch_runtime_schema did not add merge_preview_id column to old table"
    )

    # Existing row must be readable with merge_queue_id defaulting to empty string
    old_ctx = get_branch_context(conn_old, "proj-old", "task-old")
    assert old_ctx is not None
    assert old_ctx.merge_queue_id == "", (
        f"old row merge_queue_id should default to empty, got {old_ctx.merge_queue_id!r}"
    )

    # After upsert with a merge_queue_id, the value must persist
    upsert_branch_context(
        conn_old,
        _replace(old_ctx, merge_queue_id="mq-old-migrated"),
        now_iso=NOW,
    )
    migrated = get_branch_context(conn_old, "proj-old", "task-old")
    assert migrated is not None
    assert migrated.merge_queue_id == "mq-old-migrated", (
        f"upsert after migration did not persist merge_queue_id, got {migrated.merge_queue_id!r}"
    )
