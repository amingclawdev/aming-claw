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
from agent.governance.worker_transcript_verify import verify_worker_transcript
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
    RUNTIME_CONTEXT_GATE_PROJECTION_SCHEMA_VERSION,
    RUNTIME_CONTEXT_LANE_FOLD_SCHEMA_VERSION,
    RUNTIME_CONTEXT_TIMELINE_GATE_PROJECTION_SCHEMA_VERSION,
    RUNTIME_CONTEXT_WORKER_EXECUTION_SAFETY_SCHEMA_VERSION,
    RUNTIME_CONTEXT_WORKER_VIEW_SCHEMA_VERSION,
    MF_SUBAGENT_SESSION_REISSUE_MAX_TTL_SECONDS,
    STATE_DEPENDENCY_BLOCKED,
    STATE_MERGE_FAILED,
    STATE_MERGE_READY,
    STATE_MERGED,
    STATE_STALE_AFTER_DEPENDENCY_MERGE,
    STATE_VALIDATED,
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
    branch_context_to_dict,
    branch_runtime_allocation_evidence,
    branch_runtime_context_id,
    build_runtime_context_current_view,
    build_runtime_context_gate_inputs_view,
    build_runtime_context_lane_plan_view,
    build_runtime_context_projection,
    build_runtime_context_worker_view,
    decide_merge_queue,
    decide_restart_recovery,
    ensure_branch_runtime_schema,
    get_branch_context,
    get_latest_branch_contract_revision,
    initial_join_mf_subagent_runtime_session_token,
    get_merge_queue_item_for_branch_context,
    list_branch_contexts,
    materialize_branch_worktree,
    mf_subagent_session_token_hash,
    plan_branch_runtime_context,
    queue_merge_item_for_branch_context,
    reissue_mf_subagent_runtime_session_token,
    record_runtime_context_access_audit,
    record_branch_finish_gate,
    record_mf_subagent_startup,
    recover_expired_branch_contexts,
    record_branch_checkpoint,
    redact_runtime_context_payload,
    runtime_context_audit_nodes_for_views,
    runtime_context_content_hash,
    runtime_context_filter_content_address,
    runtime_context_session_token_ref,
    runtime_context_session_token_lease_view,
    runtime_context_secret_hash,
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


def test_runtime_context_graph_trace_refs_expect_successor_parent_not_root() -> None:
    from agent.governance import server as server_module

    conn = _runtime_conn()
    context = BranchTaskRuntimeContext(
        project_id=PROJECT_ID,
        task_id="mf-sub-hotfix-lineage",
        parent_task_id="cex-hotfix-successor",
        root_task_id="cex-onboard-root",
        chain_id="cchain-onboard-root",
        stage_task_id="mf-sub-hotfix-lineage",
        backlog_id="AC-FINISH-GATE-LINEAGE",
        worker_id="worker-hotfix-lineage",
        worker_slot_id="worker-hotfix-lineage",
        agent_id="agent-hotfix-lineage",
        branch_ref="refs/heads/codex/mf-sub-hotfix-lineage",
        status=STATE_WORKTREE_READY,
        fence_token="fence-hotfix-lineage",
        worktree_path="/tmp/nonexistent-mf-sub-hotfix-lineage",
        base_commit="base-hotfix-lineage",
        head_commit="head-hotfix-lineage",
        target_head_commit="target-hotfix-lineage",
        merge_queue_id="mq-hotfix-lineage",
    )
    runtime_context_id = branch_runtime_context_id(PROJECT_ID, context.task_id)
    _insert_startup_graph_trace(
        conn,
        trace_id="gqt-hotfix-lineage",
        task_id=context.task_id,
        parent_task_id=context.parent_task_id,
        runtime_context_id=runtime_context_id,
        fence_token=context.fence_token,
        query_purpose="subagent_context_build",
    )

    canonical_parent = server_module._runtime_context_mf_sub_parent_task_id(context)
    refs = server_module._runtime_context_service_graph_trace_refs(
        conn,
        project_id=PROJECT_ID,
        runtime_context_id=runtime_context_id,
        task_id=context.task_id,
        parent_task_id=canonical_parent,
        backlog_id=context.backlog_id,
        fence_token=context.fence_token,
        explicit_trace_ids=["gqt-hotfix-lineage"],
        strict_explicit_trace_ids=True,
    )

    assert canonical_parent == "cex-hotfix-successor"
    assert context.chain_id == "cchain-onboard-root"
    assert refs["db_verified"] is True
    assert refs["parent_task_id"] == "cex-hotfix-successor"
    assert refs["trace_ids"] == ["gqt-hotfix-lineage"]
    assert refs["identity_mismatches"] == []

    root_refs = server_module._runtime_context_service_graph_trace_refs(
        conn,
        project_id=PROJECT_ID,
        runtime_context_id=runtime_context_id,
        task_id=context.task_id,
        parent_task_id=context.root_task_id,
        backlog_id=context.backlog_id,
        fence_token=context.fence_token,
        explicit_trace_ids=["gqt-hotfix-lineage"],
        strict_explicit_trace_ids=True,
    )

    assert root_refs["db_verified"] is False
    assert any(
        mismatch["field"] == "parent_task_id"
        and mismatch["expected"] == "cex-onboard-root"
        and mismatch["actual"] == "cex-hotfix-successor"
        for mismatch in root_refs["identity_mismatches"]
    )

    _status, repair = (
        server_module._parallel_branch_finish_gate_contract_error_repair_response(
            project_id=PROJECT_ID,
            context=context,
            message="graph trace evidence identity mismatch: parent_task_id",
        )
    )
    assert repair["parent_task_id"] == "cex-hotfix-successor"
    assert repair["repair"]["expected_context"]["parent_task_id"] == (
        "cex-hotfix-successor"
    )


def test_runtime_context_graph_trace_refs_use_successor_chain_when_parent_absent() -> None:
    from agent.governance import server as server_module

    conn = _runtime_conn()
    context = BranchTaskRuntimeContext(
        project_id=PROJECT_ID,
        task_id="mf-sub-hotfix-legacy-chain-lineage",
        parent_task_id="",
        root_task_id="cex-onboard-root",
        chain_id="cex-hotfix-successor",
        stage_task_id="mf-sub-hotfix-legacy-chain-lineage",
        backlog_id="AC-FINISH-GATE-LEGACY-CHAIN-LINEAGE",
        worker_id="worker-hotfix-legacy-chain-lineage",
        worker_slot_id="worker-hotfix-legacy-chain-lineage",
        agent_id="agent-hotfix-legacy-chain-lineage",
        branch_ref="refs/heads/codex/mf-sub-hotfix-legacy-chain-lineage",
        status=STATE_WORKTREE_READY,
        fence_token="fence-hotfix-legacy-chain-lineage",
        worktree_path="/tmp/nonexistent-mf-sub-hotfix-legacy-chain-lineage",
        base_commit="base-hotfix-legacy-chain-lineage",
        head_commit="head-hotfix-legacy-chain-lineage",
        target_head_commit="target-hotfix-legacy-chain-lineage",
        merge_queue_id="mq-hotfix-legacy-chain-lineage",
    )
    runtime_context_id = branch_runtime_context_id(PROJECT_ID, context.task_id)
    _insert_startup_graph_trace(
        conn,
        trace_id="gqt-hotfix-legacy-chain-lineage",
        task_id=context.task_id,
        parent_task_id=context.chain_id,
        runtime_context_id=runtime_context_id,
        fence_token=context.fence_token,
        query_purpose="subagent_context_build",
    )

    canonical_parent = server_module._runtime_context_mf_sub_parent_task_id(context)
    refs = server_module._runtime_context_service_graph_trace_refs(
        conn,
        project_id=PROJECT_ID,
        runtime_context_id=runtime_context_id,
        task_id=context.task_id,
        parent_task_id=canonical_parent,
        backlog_id=context.backlog_id,
        fence_token=context.fence_token,
        explicit_trace_ids=["gqt-hotfix-legacy-chain-lineage"],
        strict_explicit_trace_ids=True,
    )

    assert canonical_parent == "cex-hotfix-successor"
    assert context.root_task_id == "cex-onboard-root"
    assert refs["db_verified"] is True
    assert refs["parent_task_id"] == "cex-hotfix-successor"
    assert refs["trace_ids"] == ["gqt-hotfix-legacy-chain-lineage"]
    assert refs["identity_mismatches"] == []

    root_refs = server_module._runtime_context_service_graph_trace_refs(
        conn,
        project_id=PROJECT_ID,
        runtime_context_id=runtime_context_id,
        task_id=context.task_id,
        parent_task_id=context.root_task_id,
        backlog_id=context.backlog_id,
        fence_token=context.fence_token,
        explicit_trace_ids=["gqt-hotfix-legacy-chain-lineage"],
        strict_explicit_trace_ids=True,
    )

    assert root_refs["db_verified"] is False
    assert any(
        mismatch["field"] == "parent_task_id"
        and mismatch["expected"] == "cex-onboard-root"
        and mismatch["actual"] == "cex-hotfix-successor"
        for mismatch in root_refs["identity_mismatches"]
    )

    _status, repair = (
        server_module._parallel_branch_finish_gate_contract_error_repair_response(
            project_id=PROJECT_ID,
            context=context,
            message="graph trace evidence identity mismatch: parent_task_id",
        )
    )
    assert repair["parent_task_id"] == "cex-hotfix-successor"
    assert repair["repair"]["expected_context"]["parent_task_id"] == (
        "cex-hotfix-successor"
    )


def test_runtime_context_graph_trace_refs_use_stage_before_legacy_chain_when_root_absent() -> None:
    from agent.governance import server as server_module

    conn = _runtime_conn()
    context = BranchTaskRuntimeContext(
        project_id=PROJECT_ID,
        task_id="mf-sub-hotfix-stage-lineage",
        parent_task_id="",
        root_task_id="",
        chain_id="cchain-onboard-root",
        stage_task_id="cex-hotfix-successor",
        backlog_id="AC-FINISH-GATE-STAGE-LINEAGE",
        worker_id="worker-hotfix-stage-lineage",
        worker_slot_id="worker-hotfix-stage-lineage",
        agent_id="agent-hotfix-stage-lineage",
        branch_ref="refs/heads/codex/mf-sub-hotfix-stage-lineage",
        status=STATE_WORKTREE_READY,
        fence_token="fence-hotfix-stage-lineage",
        worktree_path="/tmp/nonexistent-mf-sub-hotfix-stage-lineage",
        base_commit="base-hotfix-stage-lineage",
        head_commit="head-hotfix-stage-lineage",
        target_head_commit="target-hotfix-stage-lineage",
        merge_queue_id="mq-hotfix-stage-lineage",
    )
    runtime_context_id = branch_runtime_context_id(PROJECT_ID, context.task_id)
    _insert_startup_graph_trace(
        conn,
        trace_id="gqt-hotfix-stage-lineage",
        task_id=context.task_id,
        parent_task_id=context.stage_task_id,
        runtime_context_id=runtime_context_id,
        fence_token=context.fence_token,
        query_purpose="subagent_context_build",
    )

    canonical_parent = server_module._runtime_context_mf_sub_parent_task_id(context)
    refs = server_module._runtime_context_service_graph_trace_refs(
        conn,
        project_id=PROJECT_ID,
        runtime_context_id=runtime_context_id,
        task_id=context.task_id,
        parent_task_id=canonical_parent,
        backlog_id=context.backlog_id,
        fence_token=context.fence_token,
        explicit_trace_ids=["gqt-hotfix-stage-lineage"],
        strict_explicit_trace_ids=True,
    )

    assert canonical_parent == "cex-hotfix-successor"
    assert context.chain_id == "cchain-onboard-root"
    assert refs["db_verified"] is True
    assert refs["parent_task_id"] == "cex-hotfix-successor"
    assert refs["identity_mismatches"] == []

    chain_refs = server_module._runtime_context_service_graph_trace_refs(
        conn,
        project_id=PROJECT_ID,
        runtime_context_id=runtime_context_id,
        task_id=context.task_id,
        parent_task_id=context.chain_id,
        backlog_id=context.backlog_id,
        fence_token=context.fence_token,
        explicit_trace_ids=["gqt-hotfix-stage-lineage"],
        strict_explicit_trace_ids=True,
    )

    assert chain_refs["db_verified"] is False
    assert any(
        mismatch["field"] == "parent_task_id"
        and mismatch["expected"] == "cchain-onboard-root"
        and mismatch["actual"] == "cex-hotfix-successor"
        for mismatch in chain_refs["identity_mismatches"]
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
        "worker_transcript_ref": f"codex-thread:{worker_session_id}",
        "harness_type": "codex",
        "changed_files": ["agent/governance/parallel_branch_runtime.py"],
        "graph_trace_ids": ["gqt-startup"],
    }
    payload.update(overrides)
    transcript_record = {
        "session_id": payload.get("worker_session_id"),
        "transcript_ref": payload.get("worker_transcript_ref"),
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


def test_mf_sub_startup_accepts_initial_join_bound_actual_host_worker(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-initial-join-bound"
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
        agent_id="pending-codex-subagent",
        allocation_owner="pending-codex-subagent",
        branch_ref="refs/heads/codex/mf-sub-startup",
        status=STATE_WORKTREE_READY,
        fence_token="fence-startup",
        worktree_path=str(worktree),
        target_project_root=str(worktree),
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
    _insert_startup_graph_trace(conn)

    actual_worker_id = "019f2aae-4097-73c0-9d55-e3934c85bc3c"
    initial_join = initial_join_mf_subagent_runtime_session_token(
        conn,
        project_id=PROJECT_ID,
        runtime_context_id=branch_runtime_context_id(PROJECT_ID, "mf-sub-startup"),
        task_id="mf-sub-startup",
        parent_task_id="parent-startup",
        target_project_root=str(worktree),
        agent_id=actual_worker_id,
        actual_host_worker_id=actual_worker_id,
        worker_session_id=actual_worker_id,
        reason="host envelope required for actual Codex subagent startup",
        now_iso=NOW,
    )

    saved_after_join = get_branch_context(conn, PROJECT_ID, "mf-sub-startup")
    assert saved_after_join is not None
    assert saved_after_join.actual_host_worker_id == actual_worker_id
    assert initial_join["host_envelope"]["actual_host_worker_id"] == actual_worker_id

    result = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(
            str(worktree),
            agent_id=actual_worker_id,
            actual_host_worker_id=actual_worker_id,
            worker_session_id=actual_worker_id,
            worker_transcript_ref=f"multi_agent:{actual_worker_id}",
            filer_principal=actual_worker_id,
            session_token="",
            session_token_ref=initial_join["session_token_ref"],
        ),
        now_iso=NOW,
    )

    assert result["ok"] is True
    gate = result["startup_gate"]
    assert gate["allocation_owner"] == "pending-codex-subagent"
    assert gate["agent_id"] == actual_worker_id
    assert gate["actual_host_worker_id"] == actual_worker_id
    assert gate["agent_id_match_mode"] == "initial_join_actual_host_worker"
    assert gate["session_token_evidence_type"] == "server_verified_ref"
    assert gate["server_issued_session_token_verified"] is True


def test_mf_sub_startup_rejects_initial_join_bound_worker_replay_by_other_agent(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-initial-join-replay"
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
        agent_id="pending-codex-subagent",
        allocation_owner="pending-codex-subagent",
        branch_ref="refs/heads/codex/mf-sub-startup",
        status=STATE_WORKTREE_READY,
        fence_token="fence-startup",
        worktree_path=str(worktree),
        target_project_root=str(worktree),
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
    _insert_startup_graph_trace(conn)

    bound_worker_id = "019f-bound-worker"
    replay_worker_id = "019f-replay-worker"
    initial_join = initial_join_mf_subagent_runtime_session_token(
        conn,
        project_id=PROJECT_ID,
        runtime_context_id=branch_runtime_context_id(PROJECT_ID, "mf-sub-startup"),
        task_id="mf-sub-startup",
        parent_task_id="parent-startup",
        target_project_root=str(worktree),
        agent_id=bound_worker_id,
        actual_host_worker_id=bound_worker_id,
        worker_session_id=bound_worker_id,
        reason="host envelope required for actual Codex subagent startup",
        now_iso=NOW,
    )

    result = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(
            str(worktree),
            agent_id=replay_worker_id,
            actual_host_worker_id=replay_worker_id,
            worker_session_id=replay_worker_id,
            worker_transcript_ref=f"multi_agent:{replay_worker_id}",
            filer_principal=replay_worker_id,
            session_token="",
            session_token_ref=initial_join["session_token_ref"],
        ),
        now_iso=NOW,
    )

    assert result["ok"] is False
    assert result["blocker_id"] == "agent_id_mismatch"
    refusal = result["timeline_event"]["payload"]["mf_subagent_startup_refusal"]
    assert refusal["agent_id"] == replay_worker_id
    assert refusal["actual_host_worker_id"] == replay_worker_id
    saved_after_replay = get_branch_context(conn, PROJECT_ID, "mf-sub-startup")
    assert saved_after_replay is not None
    assert saved_after_replay.actual_host_worker_id == bound_worker_id
    assert refusal["next_action"]["action"] == (
        "request_runtime_context_initial_join_host_envelope"
    )


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


def test_runtime_context_projection_defaults_missing_worker_identity_to_task_id() -> None:
    context = _runtime_projection_context(
        worker_id="",
        worker_slot_id="",
        agent_id="pending-codex-subagent",
    )
    projection = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        generated_at=NOW,
    ).to_dict()

    current_values = projection["views"]["current"]["current_values"]
    read_action = projection["views"]["action_plan"]["read_receipt_hash_action"]
    handoff = projection["views"]["action_plan"]["worker_handoff_projection"]

    assert current_values["worker_id"] == "mf-sub-runtime-context"
    assert current_values["worker_slot_id"] == "mf-sub-runtime-context"
    assert read_action["worker_identity"]["worker_id"] == "mf-sub-runtime-context"
    assert read_action["worker_identity"]["worker_slot_id"] == "mf-sub-runtime-context"
    assert handoff["dispatch_present"] is True


def test_plan_branch_runtime_context_defaults_missing_worker_identity_to_task_id() -> None:
    context = plan_branch_runtime_context(
        project_id=PROJECT_ID,
        task_id="mf-sub-missing-worker-identity",
        workspace_root="/repo",
        branch_prefix="codex",
        worktree_root=".worktrees",
    )

    assert context.worker_id == "mf-sub-missing-worker-identity"
    assert context.worker_slot_id == "mf-sub-missing-worker-identity"


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


def test_runtime_context_current_view_uses_same_lineage_timeline_over_stale_refs() -> None:
    context = _runtime_projection_context()
    runtime_context_id = branch_runtime_context_id(PROJECT_ID, context.task_id)
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
            "startup_event_ref": "timeline:old-startup",
            "read_receipt_event_ref": "timeline:old-read-receipt",
        },
        timeline_events=[
            {
                "id": 5770,
                "event_kind": "mf_subagent_startup",
                "status": "passed",
                "payload": {
                    "runtime_context_id": runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                    "backlog_id": context.backlog_id,
                },
            },
            {
                "id": 5768,
                "event_kind": "mf_subagent_read_receipt",
                "status": "passed",
                "payload": {
                    "runtime_context_id": runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                    "backlog_id": context.backlog_id,
                    "fence_token": context.fence_token,
                    "worker_role": "mf_sub",
                },
            },
        ],
        target_files=["agent/governance/parallel_branch_runtime.py"],
        generated_at=NOW,
    ).to_dict()

    current = projection["views"]["current"]

    assert current["timeline_refs"]["startup_event_ref"] == "timeline:5770"
    assert current["timeline_refs"]["read_receipt_event_ref"] == "timeline:5768"
    assert current["current_values"]["startup_event_ref"] == "timeline:5770"
    assert current["current_values"]["read_receipt_event_ref"] == "timeline:5768"


def test_runtime_context_current_view_hydrates_work_scope_from_startup_gate() -> None:
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
        startup_gate={
            "observer_command_id": "cmd-runtime-context",
            "owned_files": ["agent/governance/parallel_branch_runtime.py"],
            "acceptance_criteria": [
                "runtime context exposes bounded worker next action",
            ],
            "required_evidence": ["worker_graph_trace"],
        },
        generated_at=NOW,
    ).to_dict()

    current = projection["views"]["current"]
    capability_boundary = projection["views"]["capability_boundary"]

    assert current["work"]["target_files"] == [
        "agent/governance/parallel_branch_runtime.py"
    ]
    assert current["work"]["acceptance_criteria"] == [
        "runtime context exposes bounded worker next action"
    ]
    assert current["work"]["required_evidence"] == ["worker_graph_trace"]
    assert current["current_values"]["owned_files"] == [
        "agent/governance/parallel_branch_runtime.py"
    ]
    assert capability_boundary["owned_files"] == [
        "agent/governance/parallel_branch_runtime.py"
    ]
    assert capability_boundary["target_files"] == [
        "agent/governance/parallel_branch_runtime.py"
    ]


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


def test_runtime_context_action_plan_reads_nested_revision_route_token_ref() -> None:
    from agent.governance.server import _parallel_branch_runtime_contract_route_identity

    context = _runtime_projection_context()
    revision = {
        "revision_id": "crev-runtime-context",
        "contract_version": "mf_parallel.v1",
        "route_identity": {
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
        },
        "payload": {
            "target_files": ["agent/governance/parallel_branch_runtime.py"],
            "route_identity": {
                "route_id": "route-runtime-context",
                "route_context_hash": "sha256:route-runtime-context",
                "prompt_contract_id": "rprompt-runtime-context",
                "prompt_contract_hash": "sha256:prompt-runtime-context",
                "route_token_ref": "rtok-runtime-context",
            },
        },
    }
    route_identity = _parallel_branch_runtime_contract_route_identity(revision)

    projection = build_runtime_context_projection(
        context,
        contract_revision=revision,
        route_identity=route_identity,
        generated_at=NOW,
    ).to_dict()

    action_plan = projection["views"]["action_plan"]
    route_action = action_plan["route_token_action"]
    read_action = action_plan["read_receipt_hash_action"]

    assert route_identity["route_token_ref"] == "rtok-runtime-context"
    assert route_action["status"] == "present"
    assert route_action["next_action"] == "none"
    assert action_plan["next_legal_action"] == "submit_mf_subagent_read_receipt"
    assert read_action["next_action"] == "submit_mf_subagent_read_receipt"
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

    current_values = projection["views"]["current"]["current_values"]
    action_plan = projection["views"]["action_plan"]
    read_action = action_plan["read_receipt_hash_action"]
    handoff = action_plan["worker_handoff_projection"]

    assert current_values["owned_files"] == ["agent/governance/parallel_branch_runtime.py"]
    assert read_action["status"] == "missing"
    assert read_action["next_action"] == "submit_mf_subagent_read_receipt"
    assert read_action["guide_id"] == "runtime_context_worker_guide.startup_bridge.v1"
    assert read_action["guide_hash"].startswith("sha256:")
    assert read_action["worker_identity"] == {
        "runtime_context_id": projection["runtime_context_id"],
        "task_id": "mf-sub-runtime-context",
        "parent_task_id": "parent-runtime-context",
        "backlog_id": "BUG-RUNTIME-CONTEXT",
        "worker_role": "mf_sub",
        "worker_id": "worker-runtime-context",
        "worker_slot_id": "worker-runtime-context",
    }
    assert read_action["entrypoint"] == {
        "method": "POST",
        "path": "/api/task/{project_id}/timeline",
        "mcp_tool": "task_timeline_append",
        "runtime_action_alias": "submit_mf_subagent_read_receipt",
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
        "record_startup",
        "worker_graph_query",
        "implementation_and_tests",
        "transcript_self_attestation",
    ]
    contract_step = bridge["steps"][0]
    assert contract_step["entrypoint"]["path"] == (
        "/api/graph-governance/{project_id}/runtime-contexts/"
        "{runtime_context_id}/runtime-contract"
    )
    assert any(
        "session_token" in field
        for field in contract_step["entrypoint"]["required_query_fields"]
    )
    read_receipt_step = bridge["steps"][1]
    assert read_receipt_step["hash_bridge"]["accepted_inputs"] == [
        "read_receipt_hash",
        "launch_text_hash",
    ]
    assert read_receipt_step["hash_bridge"]["startup_field"] == "read_receipt_hash"
    assert "observer_command_id" in read_receipt_step["required_payload_fields"]
    startup_step = bridge["steps"][2]
    assert "worker_transcript_ref or worker_transcript_path" in startup_step[
        "required_fields"
    ]
    assert "graph_trace_ids" not in startup_step["required_fields"]
    assert "close_satisfying=false" in startup_step["close_satisfying_rule"]
    graph_query_step = bridge["steps"][3]
    assert graph_query_step["entrypoint"]["path"] == (
        "/api/graph-governance/{project_id}/query"
    )
    assert graph_query_step["entrypoint"]["query_source"] == "mf_subagent"
    assert graph_query_step["entrypoint"]["query_purpose"] == "subagent_context_build"
    assert "runtime_context_id" in graph_query_step["entrypoint"]["required_body_fields"]
    implementation_step = bridge["steps"][4]
    assert implementation_step["owned_files"] == [
        "agent/governance/parallel_branch_runtime.py"
    ]
    assert (
        "startup transcript identity available for finish-time attestation"
        in implementation_step["required_outputs"]
    )
    assert "mf_subagent_startup.worker_transcript_ref_or_path" in implementation_step[
        "pre_edit_required_evidence"
    ]
    assert implementation_step["evidence_to_file"] == [
        "implementation_evidence",
        "finish_time_worker_attestation",
        "finish_gate",
        "verification_or_test_results",
    ]
    transcript_step = bridge["steps"][5]
    assert "worker_session_id and filer_principal from the real worker" in transcript_step[
        "required_facts"
    ]
    assert "worker_transcript_ref or worker_transcript_path" in transcript_step[
        "required_facts"
    ]
    assert read_action["worker_next_moves"][0]["id"] == "query_runtime_contract"
    assert read_action["worker_constraints"]["scope"]["owned_files"] == [
        "agent/governance/parallel_branch_runtime.py"
    ]
    assert "merge" in read_action["worker_constraints"]["blocked_actions"]
    assert "git_commit_before_finish_gate" in read_action["worker_constraints"][
        "blocked_actions"
    ]
    assert "emit_git_commit_directive_before_finish_gate" in read_action[
        "worker_constraints"
    ]["blocked_actions"]
    finish_order_rule = read_action["worker_constraints"][
        "mf_parallel_happy_path_reminders"
    ]["worker_rules"]["finish_gate_before_git_commit"]
    assert finish_order_rule["worker_final_must_not_commit_before_finish_gate"] is True
    assert "::git-commit final directive" in finish_order_rule[
        "forbidden_before_finish_gate"
    ]
    finish_proof_rule = read_action["worker_constraints"][
        "mf_parallel_happy_path_reminders"
    ]["worker_rules"]["finish_time_transcript_proof"]
    assert finish_proof_rule["blocker"] == "pre_edit_worker_transcript_identity_missing"
    assert "worker_transcript_ref or worker_transcript_path" in finish_proof_rule[
        "copy_safe_payload_fields"
    ]
    assert read_action["observer_remediation_actions"][0]["role"] == "observer"
    assert read_action["observer_remediation_actions"][0]["id"] == (
        "request_runtime_context_initial_join_host_envelope"
    )
    assert handoff["status"] == "no_worker_startup_evidence"
    assert handoff["observer_next_action"] == (
        "request_runtime_context_initial_join_host_envelope"
    )
    assert handoff["missing_worker_lineage"] == [
        "mf_subagent_read_receipt",
        "mf_subagent_startup",
    ]
    assert handoff["no_progress_reissue_policy"][
        "observer_must_not_backfill_worker_evidence"
    ] is True
    assert handoff["no_progress_reissue_policy"]["allowed"] is True
    assert "reissue_mf_sub_worker_with_same_scope" in {
        action["id"] for action in handoff["recovery_actions"]
    }
    assert handoff["direct_fix_return_contract"][
        "requires_independent_qa_after_repair"
    ] is True
    assert projection["views"]["control_plane"]["worker_handoff_projection"][
        "status"
    ] == "no_worker_startup_evidence"
    assert projection["views"]["gate_projection"]["worker_handoff_projection"][
        "observer_next_action"
    ] == "request_runtime_context_initial_join_host_envelope"

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
    present_handoff = with_receipt["views"]["action_plan"]["worker_handoff_projection"]
    assert present_action["status"] == "present"
    assert present_action["next_action"] == "none"
    assert present_action["read_receipt_event_ref"] == "timeline:read-runtime-context"
    assert present_action["ordered_worker_startup_bridge"]["status"] == "ready"
    assert present_handoff["status"] == "no_worker_startup_evidence"
    assert present_handoff["missing_worker_lineage"] == ["mf_subagent_startup"]
    assert present_handoff["worker_next_action"] == "record_mf_subagent_startup"

    with_progress = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        timeline_refs={
            "heartbeat_event_ref": "timeline:heartbeat-runtime-context",
            "implementation_event_refs": ["timeline:implementation-runtime-context"],
        },
        graph_trace_refs={"trace_ids": ["gqt-runtime-context"]},
        target_files=["agent/governance/parallel_branch_runtime.py"],
        generated_at=NOW,
    ).to_dict()

    progress_values = with_progress["views"]["current"]["current_values"]
    progress_handoff = with_progress["views"]["action_plan"][
        "worker_handoff_projection"
    ]
    assert progress_values["heartbeat_event_ref"] == "timeline:heartbeat-runtime-context"
    assert progress_handoff["status"] == "worker_lineage_missing_progress_observed"
    assert progress_handoff["observer_next_action"] == (
        "inspect_progress_and_repair_worker_lineage"
    )
    assert progress_handoff["progress_status"] == "observed"
    assert progress_handoff["no_progress_reissue_policy"]["allowed"] is False
    assert progress_handoff["no_progress_reissue_policy"][
        "blocked_by_progress_evidence"
    ] is True
    assert "reissue_mf_sub_worker_with_same_scope" not in {
        action["id"] for action in progress_handoff["recovery_actions"]
    }

    with_changed_files = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        timeline_refs={
            "changed_files": ["agent/governance/parallel_branch_runtime.py"],
        },
        target_files=["agent/governance/parallel_branch_runtime.py"],
        generated_at=NOW,
    ).to_dict()

    changed_values = with_changed_files["views"]["current"]["current_values"]
    changed_handoff = with_changed_files["views"]["action_plan"][
        "worker_handoff_projection"
    ]
    assert changed_values["changed_files"] == [
        "agent/governance/parallel_branch_runtime.py"
    ]
    assert changed_handoff["status"] == "worker_lineage_missing_progress_observed"
    assert changed_handoff["progress_status"] == "observed"
    assert changed_handoff["no_progress_reissue_policy"]["allowed"] is False
    assert "reissue_mf_sub_worker_with_same_scope" not in {
        action["id"] for action in changed_handoff["recovery_actions"]
    }

    with_lineage = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        timeline_refs={
            "read_receipt_event_ref": "timeline:read-runtime-context",
            "startup_event_ref": "timeline:startup-runtime-context",
        },
        target_files=["agent/governance/parallel_branch_runtime.py"],
        generated_at=NOW,
    ).to_dict()

    lineage_handoff = with_lineage["views"]["action_plan"][
        "worker_handoff_projection"
    ]
    assert lineage_handoff["status"] == "worker_lineage_present"
    assert lineage_handoff["missing_worker_lineage"] == []
    assert lineage_handoff["observer_next_action"] == "none"


def test_runtime_context_worker_guide_carries_nested_dispatch_owned_files() -> None:
    context = _runtime_projection_context()
    owned_files = [
        "agent/governance/parallel_branch_runtime.py",
        "agent/governance/mf_subagent_contract.py",
    ]
    projection = build_runtime_context_projection(
        context,
        contract_revision={
            "payload": {
                "schema_version": "observer_runtime_text_contract_revision.v1",
                "dispatch_payload": {
                    "worker_contract": {
                        "owned_files": owned_files,
                        "target_files": ["agent/governance"],
                    }
                },
            }
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

    current = projection["views"]["current"]
    current_values = current["current_values"]
    gate_inputs = projection["views"]["gate_inputs"]
    worker_view = projection["views"]["worker_view"]
    read_action = projection["views"]["action_plan"]["read_receipt_hash_action"]
    implementation_step = read_action["ordered_worker_startup_bridge"]["steps"][4]

    assert current_values["owned_files"] == owned_files
    assert current["work"]["owned_files"] == owned_files
    assert gate_inputs["owned_files"] == owned_files
    assert worker_view["owned_files"] == owned_files
    assert worker_view["work"]["owned_files"] == owned_files
    assert implementation_step["owned_files"] == owned_files
    assert read_action["worker_constraints"]["scope"]["owned_files"] == owned_files
    assert worker_view["capability_boundary"]["owned_files"] == owned_files


def test_runtime_context_worker_guide_carries_branch_runtime_owned_files() -> None:
    owned_files = (
        "agent/governance/task_timeline.py",
        "agent/governance/parallel_branch_runtime.py",
    )
    target_files = ("agent/governance",)
    context = _runtime_projection_context(
        target_files=target_files,
        owned_files=owned_files,
    )

    projection = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        generated_at=NOW,
    ).to_dict()

    current = projection["views"]["current"]
    gate_inputs = projection["views"]["gate_inputs"]
    worker_view = projection["views"]["worker_view"]
    read_action = projection["views"]["action_plan"]["read_receipt_hash_action"]
    implementation_step = read_action["ordered_worker_startup_bridge"]["steps"][4]

    assert current["work"]["target_files"] == list(target_files)
    assert current["work"]["owned_files"] == list(owned_files)
    assert current["current_values"]["target_files"] == list(target_files)
    assert current["current_values"]["owned_files"] == list(owned_files)
    assert gate_inputs["target_files"] == list(target_files)
    assert gate_inputs["owned_files"] == list(owned_files)
    assert worker_view["target_files"] == list(target_files)
    assert worker_view["owned_files"] == list(owned_files)
    assert worker_view["capability_boundary"]["owned_files"] == list(owned_files)
    assert implementation_step["owned_files"] == list(owned_files)
    assert read_action["worker_constraints"]["scope"]["owned_files"] == list(owned_files)


def test_branch_runtime_persists_owned_files_for_later_worker_guide() -> None:
    conn = _runtime_conn()
    context = _runtime_projection_context(
        target_files=("agent/governance",),
        owned_files=(
            "agent/governance/task_timeline.py",
            "agent/governance/parallel_branch_runtime.py",
        ),
    )

    saved = upsert_branch_context(conn, context, now_iso=NOW)
    reloaded = get_branch_context(conn, PROJECT_ID, context.task_id)

    assert saved.target_files == ("agent/governance",)
    assert saved.owned_files == (
        "agent/governance/task_timeline.py",
        "agent/governance/parallel_branch_runtime.py",
    )
    assert reloaded is not None
    assert reloaded.target_files == saved.target_files
    assert reloaded.owned_files == saved.owned_files
    projection = build_runtime_context_projection(
        reloaded,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        generated_at=NOW,
    ).to_dict()
    assert projection["views"]["worker_view"]["owned_files"] == list(saved.owned_files)


def test_runtime_context_projection_surfaces_terminal_dispatch_blocker() -> None:
    context = _runtime_projection_context()
    runtime_context_id = branch_runtime_context_id(PROJECT_ID, context.task_id)
    projection = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        timeline_events=[
            {
                "id": "26",
                "project_id": PROJECT_ID,
                "backlog_id": "BUG-RUNTIME-CONTEXT",
                "task_id": "BUG-RUNTIME-CONTEXT",
                "event_kind": "record_blocker",
                "event_type": "record_blocker",
                "status": "blocked",
                "payload": {
                    "runtime_context_id": runtime_context_id,
                    "backlog_id": "BUG-RUNTIME-CONTEXT",
                    "terminal_dispatch_blocker": True,
                    "dispatch_blocker": True,
                    "blocker_id": "worker_session_token_not_injected",
                    "message": "Worker launch missed session token injection.",
                },
            }
        ],
        generated_at=NOW,
    ).to_dict()

    close_gate = projection["views"]["close_gate_view"]
    action_plan = projection["views"]["action_plan"]
    worker_view = projection["views"]["worker_view"]

    assert close_gate["status"] == "terminal_dispatch_blocked"
    assert close_gate["ready"] is False
    assert close_gate["terminal_dispatch_blockers"][0]["event_ref"] == "timeline:26"
    assert action_plan["next_legal_action"] == (
        "audit_close_or_resolve_terminal_dispatch_blocker"
    )
    assert {
        gap["code"] for gap in action_plan["close_precheck_gap_projection"]["gaps"]
    } >= {"terminal_dispatch_blocker"}
    assert worker_view["terminal_dispatch_blockers"][0]["blocker_id"] == (
        "worker_session_token_not_injected"
    )


def test_runtime_context_failed_independent_qa_prioritizes_revision_over_startup_gap() -> None:
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
                "id": 6165,
                "event_kind": "independent_verification",
                "task_id": context.task_id,
                "status": "failed",
                "payload": {
                    "runtime_context_id": branch_runtime_context_id(
                        PROJECT_ID,
                        context.task_id,
                    ),
                    "summary": "Acceptance item 2 failed.",
                    "findings": [
                        {
                            "acceptance_item": 2,
                            "severity": "blocking",
                            "title": "state_reconcile did not reuse parsed modules",
                        }
                    ],
                    "reviewed_events": {
                        "implementation_event_ref": "timeline:6160",
                        "verification_event_ref": "raw-route-token-secret",
                        "verification_event_refs": [
                            "timeline:6164",
                            "raw-session-token-secret",
                        ],
                        "worker_verification_event_ref": "timeline:6162",
                        "review_ready_event_ref": "timeline:6163",
                        "raw_route_token": "secret-route-token",
                        "session_token": "secret-session-token",
                    },
                },
                "verification": {
                    "acceptance_failed": [2],
                    "result": "failed",
                },
                "artifact_refs": {
                    "implementation_event_ref": "timeline:6160",
                    "worker_verification_event_ref": "timeline:6162",
                    "review_ready_event_ref": "timeline:6163",
                },
            }
        ],
        generated_at=NOW,
    ).to_dict()

    action_plan = projection["views"]["action_plan"]
    revision = action_plan["failed_qa_revision_projection"]
    first_required = action_plan["next_required_evidence"][0]

    assert action_plan["next_legal_action"] == "revise_after_failed_independent_qa"
    assert revision["status"] == "revision_required"
    assert revision["failed_qa_event_ref"] == "timeline:6165"
    assert revision["failed_acceptance_items"] == ["2"]
    assert (
        revision["findings"][0]["title"]
        == "state_reconcile did not reuse parsed modules"
    )
    assert revision["reviewed_events"]["implementation_event_ref"] == "timeline:6160"
    assert revision["reviewed_events"]["verification_event_refs"] == ["timeline:6164"]
    assert "verification_event_ref" not in revision["reviewed_events"]
    assert "raw_route_token" not in revision["reviewed_events"]
    assert "session_token" not in revision["reviewed_events"]
    assert "secret-route-token" not in json.dumps(action_plan)
    assert "secret-session-token" not in json.dumps(action_plan)
    assert "raw-route-token-secret" not in json.dumps(action_plan)
    assert "raw-session-token-secret" not in json.dumps(action_plan)
    assert revision["allowed_files"] == ["agent/governance/parallel_branch_runtime.py"]
    assert first_required["id"] == "failed_qa_revision"
    assert first_required["is_next"] is True
    assert first_required["next_action"] == "revise_after_failed_independent_qa"
    assert {
        item["code"] for item in action_plan["blocking_reasons"]
    } >= {"failed_independent_qa", "startup_missing"}


def test_runtime_context_passed_qa_with_previous_failed_ref_clears_revision_blocker() -> None:
    context = _runtime_projection_context()
    failed = {
        "id": 6165,
        "event_kind": "independent_verification",
        "task_id": context.task_id,
        "status": "failed",
        "payload": {"summary": "Acceptance item 2 failed."},
        "verification": {"acceptance_failed": [2], "result": "failed"},
    }
    passed = {
        "id": 6180,
        "event_kind": "independent_verification",
        "task_id": context.task_id,
        "status": "passed",
        "payload": {
            "previous_failed_qa_ref": "timeline:6165",
            "summary": "Revised worker patch fixes prior blocker.",
        },
        "verification": {"acceptance_failed": [], "result": "passed"},
    }

    projection = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        timeline_events=[failed, passed],
        generated_at=NOW,
    ).to_dict()

    action_plan = projection["views"]["action_plan"]

    assert action_plan["failed_qa_revision_projection"]["status"] == "not_required"
    assert action_plan["next_legal_action"] != "revise_after_failed_independent_qa"
    assert all(
        item["id"] != "failed_qa_revision"
        for item in action_plan["next_required_evidence"]
    )


def test_runtime_context_close_gate_marks_startup_refusal_blocked_not_present() -> None:
    context = _runtime_projection_context()
    runtime_context_id = branch_runtime_context_id(PROJECT_ID, context.task_id)
    projection = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        timeline_refs={"startup_event_ref": "timeline:6196"},
        startup_gate={
            "id": 6196,
            "event_kind": "mf_subagent_startup_refusal",
            "status": "blocked",
            "payload": {
                "mf_subagent_startup_refusal": {
                    "schema_version": "mf_subagent_startup_refusal.v1",
                    "status": "blocked",
                    "runtime_context_id": runtime_context_id,
                    "task_id": context.task_id,
                    "reason": "session_token_ref missing",
                    "missing_fields": ["session_token_ref"],
                }
            },
        },
        generated_at=NOW,
    ).to_dict()

    close_gate = projection["views"]["close_gate_view"]
    startup_item = next(
        item for item in close_gate["checklist"] if item["id"] == "startup_evidence"
    )

    assert startup_item["value"] == "timeline:6196"
    assert startup_item["status"] == "blocked"
    assert startup_item["valid"] is False
    assert startup_item["blockers"] == ["session_token_ref"]
    assert any(
        item.get("field") == "startup_event_ref"
        and item.get("status") == "blocked"
        for item in close_gate["missing"]
    )
    assert close_gate["ready"] is False


def test_runtime_context_worker_execution_safety_blocks_relative_patch_until_startup_cwd_verified() -> None:
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
        generated_at=NOW,
    ).to_dict()

    safety = projection["views"]["worker_view"]["worker_execution_safety"]
    boundary_safety = projection["views"]["capability_boundary"][
        "worker_execution_safety"
    ]
    assert safety == boundary_safety
    assert safety["schema_version"] == RUNTIME_CONTEXT_WORKER_EXECUTION_SAFETY_SCHEMA_VERSION
    assert safety["status"] == "pre_edit_blocked"
    assert safety["assigned_worktree_path"] == (
        "/repo/.worktrees/mf-sub-runtime-context"
    )
    assert safety["relative_patch_safe"] is False
    assert safety["apply_patch_relative_paths_allowed"] is False
    assert {item["code"] for item in safety["pre_edit_blockers"]} == {
        "pre_edit_startup_missing",
        "pre_implementation_graph_trace_missing",
    }

    startup_only_projection = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        timeline_refs={"startup_event_ref": "timeline:startup"},
        startup_gate={
            "actual_cwd": "/repo/.worktrees/mf-sub-runtime-context",
            "actual_git_root": "/repo/.worktrees/mf-sub-runtime-context",
        },
        generated_at=NOW,
    ).to_dict()

    startup_only_safety = startup_only_projection["views"]["worker_view"][
        "worker_execution_safety"
    ]
    assert startup_only_safety["status"] == "pre_edit_blocked"
    assert startup_only_safety["relative_patch_safe"] is False
    assert startup_only_safety["apply_patch_relative_paths_allowed"] is False
    assert {item["code"] for item in startup_only_safety["pre_edit_blockers"]} == {
        "pre_edit_worker_transcript_identity_missing",
        "pre_implementation_graph_trace_missing"
    }
    transcript_blocker = next(
        item
        for item in startup_only_safety["pre_edit_blockers"]
        if item["code"] == "pre_edit_worker_transcript_identity_missing"
    )
    assert transcript_blocker["missing"] == [
        "worker_session_id",
        "worker_transcript_ref_or_path",
        "harness_type",
        "filer_principal",
    ]

    verified_projection = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        timeline_refs={"startup_event_ref": "timeline:startup"},
        startup_gate={
            "actual_cwd": "/repo/.worktrees/mf-sub-runtime-context",
            "actual_git_root": "/repo/.worktrees/mf-sub-runtime-context",
            "worker_session_id": "codex-session-runtime-context",
            "worker_transcript_ref": "codex:codex-session-runtime-context",
            "harness_type": "codex",
            "filer_principal": "codex-session-runtime-context",
        },
        graph_trace_refs={"trace_ids": ["gqt-runtime-context"]},
        generated_at=NOW,
    ).to_dict()

    verified_safety = verified_projection["views"]["worker_view"][
        "worker_execution_safety"
    ]
    assert verified_safety["status"] == "verified"
    assert verified_safety["relative_patch_safe"] is True
    assert verified_safety["apply_patch_relative_paths_allowed"] is True
    assert verified_safety["startup_transcript_identity_ready"] is True
    assert verified_safety["pre_edit_blockers"] == []


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
    assert {
        "route_identity_cleanup_required",
        "independent_verification_required",
        "route_token_action_scope_mismatch",
        "target_graph_stale",
        "worker_graph_query_identity_required",
    } <= gap_codes
    assert {
        "read_receipt_missing",
        "startup_missing",
        "finish_time_worker_attestation_missing",
        "finish_gate_missing",
    } <= gap_codes
    assert "cleanup_route_identity" in close_projection["next_actions"]
    assert "record_independent_verification" in close_projection["next_actions"]
    assert "refresh_route_token_scope" in close_projection["next_actions"]
    assert "reconcile_target_graph" in close_projection["next_actions"]
    assert "query_graph_as_mf_subagent" in close_projection["next_actions"]
    assert gap_codes <= blocking_codes


def test_runtime_context_gate_projection_is_projection_only_and_actionable() -> None:
    projection = build_runtime_context_projection(
        _runtime_projection_context(),
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "raw_route_token": "raw-route-token-secret",
        },
        contract_revision={
            "revision_id": "crev-runtime-context-gate-projection",
            "contract_version": "mf_parallel.v1",
            "payload": {
                "worker_session_token": "raw-worker-session-token",
                "fence_token": "fence-runtime-context",
                "close_precheck": {
                    "independent_verification_required": True,
                },
            },
        },
        target_files=["agent/governance/parallel_branch_runtime.py"],
        generated_at=NOW,
    ).to_dict()

    views = projection["views"]
    gate_projection = views["gate_projection"]
    serialized = json.dumps(gate_projection, sort_keys=True)

    assert gate_projection["schema_version"] == (
        RUNTIME_CONTEXT_GATE_PROJECTION_SCHEMA_VERSION
    )
    assert gate_projection["projection_only"] is True
    assert gate_projection["must_revalidate_on_write"] is True
    assert gate_projection["raw_session_token_exposed"] is False
    assert gate_projection["raw_route_token_exposed"] is False
    assert gate_projection["raw_fence_token_exposed"] is False
    assert gate_projection["redaction"]["observer_only_authority_exposed"] is False
    assert "raw-worker-session-token" not in serialized
    assert "raw-route-token-secret" not in serialized
    assert "fence-runtime-context" not in serialized

    assert gate_projection["projection_status"] == "diagnostic_blocked"
    assert gate_projection["diagnostic_status"] == "missing_required_evidence"
    assert gate_projection["next_legal_action"] == "refresh_route_token_ref"
    assert any(
        item["id"] == "route_token_ref"
        for item in gate_projection["next_required_evidence"]
    )
    assert any(
        item.get("field") == "route_token_ref"
        for item in gate_projection["missing_evidence"]
    )
    assert any(
        item.get("field") == "route_token_ref"
        for item in gate_projection["close_gate_missing"]
    )
    assert any(
        item["code"] == "route_token_missing"
        for item in gate_projection["blocking_reasons"]
    )
    close_gap_codes = {
        item["code"]
        for item in gate_projection["close_precheck_gap_projection"]["gaps"]
    }
    assert "independent_verification_required" in close_gap_codes
    assert gate_projection["audit_archive_action"]["ordinary_close_gate_claimed"] is False
    assert "can_close" not in serialized
    assert "normal_close_gate_passed" not in serialized
    assert "close_ready_emitted" not in serialized
    assert views["control_plane"]["gate_projection"] == gate_projection
    assert views["worker_view"]["gate_projection"] == gate_projection
    assert views["worker_view"]["control_plane"]["gate_projection"] == gate_projection


def test_runtime_context_gate_projection_close_gate_ready_still_revalidates_on_write() -> None:
    projection = build_runtime_context_projection(
        _runtime_projection_context(checkpoint_id="checkpoint-runtime-context"),
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        target_files=["agent/governance/parallel_branch_runtime.py"],
        graph_trace_refs={"trace_ids": ["gqt-runtime-context"]},
        timeline_refs={
            "route_action_precheck_event_ref": "timeline:route-precheck-runtime-context",
            "finish_event_ref": "timeline:finish-runtime-context",
            "verification_event_refs": ["timeline:verify-runtime-context"],
        },
        finish_gate={
            "event_id": "timeline:finish-runtime-context",
            "checkpoint_id": "checkpoint-runtime-context",
            "worker_self_attestation": {
                "status": "passed",
                "worker_self_attesting": True,
                "finish_time_self_attesting": True,
                "attestation_phase": "finish",
            },
            "worker_self_attestation_gate": {
                "status": "passed",
                "passed": True,
            },
        },
        generated_at=NOW,
    ).to_dict()

    views = projection["views"]
    close_gate = views["close_gate_view"]
    gate_projection = views["gate_projection"]
    diagnostic = gate_projection["close_gate_diagnostic"]

    assert close_gate["ready"] is True
    assert close_gate["status"] == "ready"
    assert diagnostic["close_gate_projection_ready"] is True
    assert diagnostic["projection_status"] == "diagnostic_ready"
    assert diagnostic["ready_view_is_diagnostic_only"] is True
    assert diagnostic["write_authorization_provided"] is False
    assert diagnostic["authoritative_close_authorization"] == "not_evaluated"
    assert diagnostic["must_revalidate_on_write"] is True
    assert gate_projection["projection_only"] is True
    assert gate_projection["must_revalidate_on_write"] is True
    assert gate_projection["write_boundary"][
        "protected_endpoints_must_rerun_authoritative_gates"
    ] is True
    assert gate_projection["write_boundary"][
        "projection_fields_accepted_as_close_evidence"
    ] is False
    assert not {"can_close", "close_ready", "close_satisfying"} & set(
        gate_projection
    )


def test_runtime_context_close_gate_blocks_handoff_when_route_action_precheck_missing() -> None:
    def _projection(*, close_ready: bool) -> dict[str, object]:
        timeline_refs = {
            "startup_event_ref": "timeline:startup-runtime-context",
            "read_receipt_event_ref": "timeline:read-runtime-context",
            "implementation_event_refs": ["timeline:implementation-runtime-context"],
            "finish_event_ref": "timeline:finish-runtime-context",
            "verification_event_refs": ["timeline:verify-runtime-context"],
        }
        close_evidence = {}
        if close_ready:
            timeline_refs["close_ready_event_ref"] = (
                "timeline:close-ready-runtime-context"
            )
            close_evidence = {"event_id": "timeline:close-ready-runtime-context"}
        return build_runtime_context_projection(
            _runtime_projection_context(checkpoint_id="checkpoint-runtime-context"),
            route_identity={
                "route_id": "route-runtime-context",
                "route_context_hash": "sha256:route-runtime-context",
                "prompt_contract_id": "rprompt-runtime-context",
                "prompt_contract_hash": "sha256:prompt-runtime-context",
                "route_token_ref": "rtok-runtime-context",
            },
            target_files=["agent/governance/parallel_branch_runtime.py"],
            graph_trace_refs={"trace_ids": ["gqt-runtime-context"]},
            timeline_refs=timeline_refs,
            startup_gate={
                "runtime_context_id": branch_runtime_context_id(
                    PROJECT_ID,
                    "mf-sub-runtime-context",
                ),
                "fence_token_matches": True,
                "worker_session_id": "session-runtime-context",
                "worker_transcript_path": "/tmp/transcript-runtime-context.jsonl",
                "harness_type": "codex",
                "route_id": "route-runtime-context",
                "route_context_hash": "sha256:route-runtime-context",
                "prompt_contract_id": "rprompt-runtime-context",
                "prompt_contract_hash": "sha256:prompt-runtime-context",
                "route_token_ref": "rtok-runtime-context",
                "read_receipt_hash": "sha256:read-runtime-context",
                "read_receipt_event_id": "timeline:read-runtime-context",
            },
            finish_gate={
                "event_id": "timeline:finish-runtime-context",
                "checkpoint_id": "checkpoint-runtime-context",
                "worker_self_attestation": {
                    "status": "passed",
                    "worker_self_attesting": True,
                    "finish_time_self_attesting": True,
                    "attestation_phase": "finish",
                },
                "worker_self_attestation_gate": {
                    "status": "passed",
                    "passed": True,
                },
            },
            close_evidence=close_evidence,
            generated_at=NOW,
        ).to_dict()

    projection = _projection(close_ready=True)

    close_gate = projection["views"]["close_gate_view"]
    action_plan = projection["views"]["action_plan"]
    by_id = {item["id"]: item for item in action_plan["next_required_evidence"]}

    assert close_gate["ready"] is False
    assert close_gate["status"] == "missing_required_fields"
    assert close_gate["route_action_precheck_event_ref"] == ""
    assert any(
        item["id"] == "route_action_precheck"
        and item["field"] == "route_action_precheck_event_ref"
        and item["status"] == "missing"
        for item in close_gate["checklist"]
    )
    assert any(
        item["field"] == "route_action_precheck_event_ref"
        and item["expected_source"] == "task_timeline.route_action_precheck"
        for item in close_gate["missing"]
    )
    assert action_plan["next_legal_action"] == "record_route_action_precheck"
    assert by_id["route_action_precheck"]["worker_owned"] is False
    assert by_id["route_action_precheck"]["producer"] == "route_service"
    assert by_id["route_action_precheck"]["expected_source"] == (
        "task_timeline.route_action_precheck"
    )
    assert any(
        item["code"] == "route_action_precheck_missing"
        and item["next_action"] == "record_route_action_precheck"
        for item in action_plan["close_precheck_gap_projection"]["gaps"]
    )

    missing_close_ready_projection = _projection(close_ready=False)
    missing_close_ready_plan = missing_close_ready_projection["views"]["action_plan"]
    close_order = [
        item["id"]
        for item in missing_close_ready_plan["next_required_evidence"]
        if item["id"] in {"route_action_precheck", "close_ready"}
    ]
    assert missing_close_ready_plan["next_legal_action"] == (
        "record_route_action_precheck"
    )
    assert close_order[0] == "route_action_precheck"
    if "close_ready" in close_order:
        assert close_order.index("route_action_precheck") < close_order.index(
            "close_ready"
        )


def test_runtime_context_gate_projection_consumes_authoritative_timeline_gate_summary() -> None:
    context = _runtime_projection_context()
    runtime_context_id = branch_runtime_context_id(PROJECT_ID, context.task_id)
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
                "id": 4511,
                "event_kind": "implementation",
                "actor": "mf_sub",
                "status": "passed",
                "payload": {
                    "runtime_context_id": runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                    "changed_files": ["agent/governance/parallel_branch_runtime.py"],
                },
            },
            {
                "id": 4512,
                "event_kind": "verification",
                "phase": "verification",
                "actor": "qa",
                "status": "passed",
                "payload": {
                    "runtime_context_id": runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                },
                "verification": {
                    "tests_run": ["pytest agent/tests/test_parallel_branch_runtime.py"],
                },
            },
        ],
        generated_at=NOW,
    ).to_dict()

    gate_projection = projection["views"]["gate_projection"]
    timeline_gate = gate_projection["authoritative_timeline_gate"]
    serialized = json.dumps(timeline_gate, sort_keys=True)

    assert timeline_gate["schema_version"] == (
        RUNTIME_CONTEXT_TIMELINE_GATE_PROJECTION_SCHEMA_VERSION
    )
    assert timeline_gate["projection_only"] is True
    assert timeline_gate["must_revalidate_on_write"] is True
    assert timeline_gate["source"] == "task_timeline.mf_close_gate_verification"
    assert timeline_gate["available"] is True
    assert timeline_gate["status"] == "failed"
    assert timeline_gate["diagnostic_result"] == "failed"
    assert "close_ready" in timeline_gate["missing_event_kinds"]
    assert any(
        repair["event_kind"] == "close_ready"
        for repair in timeline_gate["missing_event_repairs"]
    )
    assert timeline_gate["can_authorize_write"] is False
    assert timeline_gate["can_authorize_close"] is False
    assert timeline_gate["authoritative_close_verdict_redacted"] is True
    assert "can_close" not in serialized
    assert gate_projection["source_view_status"]["authoritative_timeline_gate"] == (
        "failed"
    )


def test_runtime_context_worker_view_control_plane_is_role_scoped_without_raw_tokens() -> None:
    context = _runtime_projection_context(
        session_token_hash=mf_subagent_session_token_hash("raw-worker-session-token"),
        lease_id="lease-runtime-context",
        lease_expires_at="2999-01-01T00:00:00Z",
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
    lease = worker_view["session_token_lease"]
    assert lease["has_lease"] is True
    assert lease["status"] == "active"
    assert lease["expired"] is False
    assert lease["lease_expires_at"] == "2999-01-01T00:00:00Z"
    assert lease["lease_remaining_ttl_seconds"] > 0
    assert lease["renewal_supported"] is True
    assert lease["raw_session_token_persisted"] is False
    graph_payload = worker_view["graph_query_identity"]["payload_shape"]
    assert graph_payload["target_project_root"] == "/repo"
    assert graph_payload["project_root"] == "/repo"
    assert graph_payload["repo_root"] == "/repo"
    assert graph_payload["route_identity"]["route_token_ref"] == "rtok-runtime-context"


def test_runtime_context_projection_uses_worktree_when_target_root_is_empty() -> None:
    context = _runtime_projection_context(
        target_project_root="",
        worktree_path="/repo/.worktrees/mf-sub-runtime-context",
    )
    projection = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        generated_at=NOW,
    ).to_dict()

    current = projection["views"]["current"]
    current_values = current["current_values"]
    worker_view = projection["views"]["worker_view"]
    graph_identity = worker_view["graph_query_identity"]
    graph_payload = graph_identity["payload_shape"]
    capability = worker_view["capability_boundary"]

    assert current_values["target_project_root"] == context.worktree_path
    assert current_values["target_project_root_source"] == "context.worktree_path"
    assert current_values["project_root"] == context.worktree_path
    assert current_values["repo_root"] == context.worktree_path
    assert worker_view["task"]["target_project_root"] == context.worktree_path
    assert worker_view["task"]["project_root"] == context.worktree_path
    assert worker_view["task"]["repo_root"] == context.worktree_path
    assert graph_identity["target_project_root"] == context.worktree_path
    assert graph_identity["project_root"] == context.worktree_path
    assert graph_identity["repo_root"] == context.worktree_path
    assert graph_payload["target_project_root"] == context.worktree_path
    assert graph_payload["project_root"] == context.worktree_path
    assert graph_payload["repo_root"] == context.worktree_path
    assert capability["graph_query_scope"]["target_project_root"] == context.worktree_path
    assert capability["graph_query_scope"]["project_root"] == context.worktree_path
    assert capability["graph_query_scope"]["repo_root"] == context.worktree_path
    assert capability["worker_execution_safety"]["target_project_root"] == (
        context.worktree_path
    )


def test_runtime_context_distinguishes_terminal_complete_from_merge_eligible() -> None:
    complete_but_waiting = MergeQueueItem(
        project_id=PROJECT_ID,
        merge_queue_id="mq-runtime-context",
        queue_item_id="mqi-worker-2",
        task_id="worker-2",
        branch_ref="refs/heads/codex/worker-2",
        queue_index=2,
        status=STATE_VALIDATED,
        depends_on=("worker-1",),
        branch_head="head-worker-2",
        merge_preview_id="mp-worker-2",
    )
    dependency_not_merged = MergeQueueItem(
        project_id=PROJECT_ID,
        merge_queue_id="mq-runtime-context",
        queue_item_id="mqi-worker-1",
        task_id="worker-1",
        branch_ref="refs/heads/codex/worker-1",
        queue_index=1,
        status=STATE_MERGE_READY,
        branch_head="head-worker-1",
        merge_preview_id="mp-worker-1",
    )
    merge_plan = decide_merge_queue(
        [complete_but_waiting, dependency_not_merged],
        scenario_id="runtime-context-terminal-complete-not-merge-eligible",
    )
    worker2_decision = {
        decision.task_id: decision.to_dashboard_row()
        for decision in merge_plan.decisions
    }["worker-2"]

    projection = build_runtime_context_projection(
        _runtime_projection_context(
            task_id="worker-2",
            status=STATE_VALIDATED,
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
            "revision_id": "crev-runtime-context-terminal-complete",
            "contract_version": "mf_parallel.v1",
            "payload": {
                "merge_queue_projection": worker2_decision,
                "ordered_merge_dependencies": ["worker-1"],
                "finish_gate": {
                    "status": "passed",
                    "checkpoint_id": "ckpt-worker-2",
                },
            },
        },
        generated_at=NOW,
    ).to_dict()

    dependency_projection = projection["views"]["action_plan"][
        "merge_dependency_projection"
    ]

    assert worker2_decision["queue_state"] == STATE_WAITING_DEPENDENCY
    assert dependency_projection["terminal_complete"] is True
    assert dependency_projection["merge_eligible"] is False
    assert dependency_projection["merge_allowed"] is False
    assert dependency_projection["target_branch_mutation_allowed"] is False
    assert dependency_projection["next_actions"] == [
        "wait_for_dependency",
        "merge_dependency_first",
        "do_not_merge_current_lane",
    ]


def test_runtime_context_after_dependency_merge_requires_preview_refresh_before_eligible() -> None:
    worker2_decision = {
        "queue_item_id": "mqi-worker-2",
        "task_id": "worker-2",
        "branch_ref": "refs/heads/codex/worker-2",
        "observed_status": STATE_VALIDATED,
        "queue_state": STATE_STALE_AFTER_DEPENDENCY_MERGE,
        "dependency_blockers": [],
        "stale_target_head": True,
        "merge_preview_id": "mp-worker-2-before-worker-1",
        "merge_allowed": False,
        "target_branch_mutation_allowed": False,
        "next_actions": ["refresh_merge_preview", "revalidate"],
    }

    projection = build_runtime_context_projection(
        _runtime_projection_context(
            task_id="worker-2",
            status=STATE_VALIDATED,
            merge_queue_id="mq-runtime-context",
            merge_preview_id="mp-worker-2-before-worker-1",
        ),
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
        },
        contract_revision={
            "revision_id": "crev-runtime-context-refresh-before-eligible",
            "contract_version": "mf_parallel.v1",
            "payload": {
                "merge_queue_projection": worker2_decision,
                "dependency_merge_commit": "merge-worker-1",
                "dependency_validated_target_head": "target-before-worker-1",
                "current_target_head": "merge-worker-1",
            },
        },
        generated_at=NOW,
    ).to_dict()

    dependency_projection = projection["views"]["action_plan"][
        "merge_dependency_projection"
    ]

    assert dependency_projection["status"] == STATE_STALE_AFTER_DEPENDENCY_MERGE
    assert dependency_projection["merge_eligible"] is False
    assert dependency_projection["requires_merge_preview_refresh"] is True
    assert dependency_projection["next_action"] == "refresh_merge_preview"
    assert dependency_projection["next_actions"][:2] == [
        "refresh_merge_preview",
        "revalidate",
    ]


def test_runtime_context_role_views_are_scoped_for_observer_qa_and_judge() -> None:
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
            "revision_id": "crev-runtime-context-multirole-scope",
            "contract_version": "mf_parallel.v1",
            "payload": {
                "observer_command_id": "cmd-runtime-context",
                "worker_session_token": "raw-worker-session-token",
                "judge_session_token": "raw-judge-session-token",
                "qa_session_token": "raw-qa-session-token",
            },
        },
        generated_at=NOW,
    ).to_dict()

    views = projection["views"]
    assert set(["worker_view", "observer_view", "qa_view", "judge_view"]) <= set(views)
    for view_name, expected_role in (
        ("worker_view", "mf_sub"),
        ("observer_view", "observer"),
        ("qa_view", "qa"),
        ("judge_view", "judge"),
    ):
        role_view = views[view_name]
        serialized = json.dumps(role_view, sort_keys=True)
        assert role_view["role_scope"] == expected_role
        assert role_view["control_plane"]["viewer_role"] == expected_role
        assert role_view["control_plane"]["raw_session_token_exposed"] is False
        assert role_view["control_plane"]["raw_route_token_exposed"] is False
        assert "raw-worker-session-token" not in serialized
        assert "raw-judge-session-token" not in serialized
        assert "raw-qa-session-token" not in serialized
        assert "raw-route-token-secret" not in serialized

    with pytest.raises(BranchRuntimeFenceError):
        build_runtime_context_worker_view(
            projection["views"]["current"],
            task_id=context.task_id,
            fence_token="wrong-fence",
            role="qa",
        )


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
            "implementation_event_refs": ["timeline:implementation-runtime-context"],
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


def test_runtime_context_projects_worker_owned_next_required_evidence_for_finish_gate() -> None:
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
        startup_gate={
            "worker_self_attesting": False,
            "worker_self_attestation": {
                "status": "blocked",
                "worker_self_attesting": False,
                "blockers": ["missing_mf_subagent_graph_trace_ids"],
            },
        },
        target_files=["agent/governance/parallel_branch_runtime.py"],
        generated_at=NOW,
    ).to_dict()

    action_plan = projection["views"]["action_plan"]
    control_plane = projection["views"]["control_plane"]
    worker_view = projection["views"]["worker_view"]
    next_required = action_plan["next_required_evidence"]
    by_id = {item["id"]: item for item in next_required}

    assert projection["next_required_evidence"] == next_required
    assert control_plane["next_required_evidence"] == next_required
    assert worker_view["next_legal_action"] == action_plan["next_legal_action"]
    assert worker_view["next_required_evidence"] == next_required
    assert worker_view["control_plane"]["next_required_evidence"] == next_required
    assert worker_view["blocking_reasons"] == action_plan["blocking_reasons"]
    assert [
        "mf_subagent_startup_identity",
        "worker_graph_trace",
        "implementation_evidence",
        "finish_time_worker_attestation",
    ] == [
        item["id"] for item in next_required[:4]
    ]
    assert by_id["mf_subagent_startup_identity"]["next_action"] == (
        "record_mf_subagent_startup"
    )
    assert by_id["mf_subagent_startup_identity"]["status"] == "stale"
    assert by_id["worker_graph_trace"]["next_action"] == "run_worker_graph_query"
    assert by_id["worker_graph_trace"]["producer"] == "graph_query_trace"
    assert by_id["worker_graph_trace"]["worker_owned"] is True
    assert by_id["implementation_evidence"]["next_action"] == (
        "record_implementation_evidence"
    )
    assert by_id["implementation_evidence"]["requires"] == ["worker_graph_trace"]
    assert by_id["implementation_evidence"]["worker_owned"] is True
    assert by_id["finish_time_worker_attestation"]["next_action"] == (
        "record_finish_time_worker_attestation"
    )
    assert by_id["finish_time_worker_attestation"]["requires"] == [
        "worker_graph_trace",
        "implementation_evidence",
    ]
    assert by_id["finish_time_worker_attestation"]["expected_source"] == (
        "worker_transcript_verify.finish_time_worker_self_attestation"
    )
    assert by_id["finish_time_worker_attestation"]["close_satisfying_required"] is True
    assert by_id["finish_gate"]["next_action"] == "record_finish_gate"
    assert by_id["finish_gate"]["requires"] == [
        "worker_graph_trace",
        "implementation_evidence",
        "finish_time_worker_attestation",
    ]
    assert by_id["finish_gate"]["runtime_context_id"] == projection["runtime_context_id"]


def test_runtime_context_current_values_prefer_finish_time_worker_attestation() -> None:
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
            "finish_event_ref": "timeline:finish-runtime-context",
        },
        startup_gate={
            "worker_self_attesting": True,
            "worker_self_attestation": {
                "status": "passed",
                "attestation_phase": "startup",
                "worker_self_attesting": True,
                "finish_time_self_attesting": False,
                "finish_time_blockers": ["no_owned_files_diff"],
            },
        },
        finish_gate={
            "event_id": "timeline:finish-runtime-context",
            "checkpoint_id": "ckpt-runtime-context",
            "worker_self_attestation_gate": {"passed": True},
            "worker_self_attestation": {
                "status": "passed",
                "attestation_phase": "finish",
                "worker_self_attesting": True,
                "self_attesting": True,
                "finish_time_self_attesting": True,
                "finish_time_blockers": [],
                "worker_session_id": "session-runtime-context",
                "filer_principal": "session-runtime-context",
                "worker_transcript_path": "/tmp/transcript-runtime-context.jsonl",
                "harness_type": "codex",
            },
        },
        target_files=["agent/governance/parallel_branch_runtime.py"],
        graph_trace_refs={"trace_ids": ["gqt-runtime-context"]},
        generated_at=NOW,
    ).to_dict()

    current_values = projection["views"]["current"]["current_values"]
    gate_missing = {
        (item["gate"], item["field"])
        for item in projection["views"]["gate_inputs"]["missing"]
    }
    close_missing = {
        item["field"]
        for item in projection["views"]["close_gate_view"]["missing"]
    }
    next_required_ids = {
        item["id"]
        for item in projection["views"]["action_plan"]["next_required_evidence"]
    }

    assert current_values["worker_self_attesting"] is True
    assert current_values["worker_self_attestation"]["attestation_phase"] == "finish"
    assert current_values["worker_self_attestation"]["finish_time_self_attesting"] is True
    assert current_values["implementation_event_refs"] == []
    assert "implementation_evidence" in next_required_ids
    assert "finish_time_worker_attestation" not in next_required_ids


def test_runtime_context_current_values_accept_legacy_implementation_evidence_kind() -> None:
    context = _runtime_projection_context()
    runtime_context_id = branch_runtime_context_id(PROJECT_ID, context.task_id)
    route_identity = {
        "route_id": "route-runtime-context",
        "route_context_hash": "sha256:route-runtime-context",
        "prompt_contract_id": "rprompt-runtime-context",
        "prompt_contract_hash": "sha256:prompt-runtime-context",
        "route_token_ref": "rtok-runtime-context",
        "visible_injection_manifest_hash": "sha256:visible-runtime-context",
    }

    projection = build_runtime_context_projection(
        context,
        route_identity=route_identity,
        timeline_refs={
            "startup_event_ref": "timeline:startup-runtime-context",
            "read_receipt_event_ref": "timeline:read-runtime-context",
        },
        timeline_events=[
            {
                "id": 7001,
                "project_id": PROJECT_ID,
                "task_id": context.task_id,
                "backlog_id": context.backlog_id,
                "event_type": "mf.implementation",
                "event_kind": "implementation_evidence",
                "phase": "implementation",
                "status": "passed",
                "actor": context.worker_slot_id,
                "commit_sha": "impl-runtime-context",
                "payload": {
                    "runtime_context_id": runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                    "worker_role": "mf_sub",
                    "fence_token": context.fence_token,
                    "graph_trace_ids": ["gqt-runtime-context"],
                },
            }
        ],
        startup_gate={
            "runtime_context_id": runtime_context_id,
            "fence_token_matches": True,
            "route_id": route_identity["route_id"],
            "route_context_hash": route_identity["route_context_hash"],
            "prompt_contract_id": route_identity["prompt_contract_id"],
            "prompt_contract_hash": route_identity["prompt_contract_hash"],
            "route_token_ref": route_identity["route_token_ref"],
            "read_receipt_hash": "sha256:read-runtime-context",
            "read_receipt_event_id": "timeline:read-runtime-context",
            "worker_session_id": "session-runtime-context",
            "filer_principal": "session-runtime-context",
            "worker_transcript_ref": "codex:test-runtime-context",
            "harness_type": "codex",
            "worker_self_attestation": {
                "status": "passed",
                "attestation_phase": "startup",
                "worker_self_attesting": True,
                "finish_time_self_attesting": False,
            },
        },
        graph_trace_refs={"trace_ids": ["gqt-runtime-context"]},
        generated_at=NOW,
    ).to_dict()

    current_values = projection["views"]["current"]["current_values"]
    action_plan = projection["views"]["action_plan"]
    next_required_ids = {
        item["id"] for item in action_plan["next_required_evidence"]
    }

    assert current_values["implementation_event_refs"] == ["timeline:7001"]
    assert "implementation_evidence" not in next_required_ids
    assert action_plan["next_legal_action"] == (
        "record_finish_time_worker_attestation"
    )


def test_runtime_context_current_values_read_worker_progress_finish_time_attestation() -> None:
    context = _runtime_projection_context()
    runtime_context_id = branch_runtime_context_id(PROJECT_ID, context.task_id)
    worker_session_id = "worker-runtime-context-finish"

    projection = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
            "visible_injection_manifest_hash": "sha256:visible-runtime-context",
        },
        timeline_refs={
            "startup_event_ref": "timeline:startup-runtime-context",
            "read_receipt_event_ref": "timeline:read-runtime-context",
        },
        timeline_events=[
            {
                "id": 5264,
                "project_id": PROJECT_ID,
                "task_id": context.task_id,
                "backlog_id": context.backlog_id,
                "event_type": "mf_subagent.finish_time_worker_attestation",
                "event_kind": "worker_progress",
                "phase": "finish_time_worker_attestation",
                "status": "passed",
                "actor": worker_session_id,
                "payload": {
                    "schema_version": "runtime_context.finish_time_worker_attestation.v1",
                    "action": "record_finish_time_worker_attestation",
                    "runtime_context_id": runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                    "backlog_id": context.backlog_id,
                    "worker_role": "mf_sub",
                    "worker_session_id": worker_session_id,
                    "filer_principal": worker_session_id,
                    "graph_trace_ids": ["gqt-runtime-context"],
                    "test_results": {"status": "passed", "passed": True},
                    "finish_time_worker_self_attestation": {
                        "schema_version": "worker_transcript_self_attestation.v1",
                        "status": "passed",
                        "attestation_phase": "finish",
                        "worker_self_attesting": True,
                        "self_attesting": True,
                        "finish_time_self_attesting": True,
                        "finish_time_blockers": [],
                        "worker_session_id": worker_session_id,
                        "filer_principal": worker_session_id,
                        "worker_transcript_ref": "codex:test-runtime-context",
                        "harness_type": "codex",
                    },
                },
            }
        ],
        graph_trace_refs={"trace_ids": ["gqt-runtime-context"]},
        generated_at=NOW,
    ).to_dict()

    current_values = projection["views"]["current"]["current_values"]
    gate_missing = {
        (item["gate"], item["field"])
        for item in projection["views"]["gate_inputs"]["missing"]
    }
    close_missing = {
        item["field"]
        for item in projection["views"]["close_gate_view"]["missing"]
    }
    next_required_ids = {
        item["id"]
        for item in projection["views"]["action_plan"]["next_required_evidence"]
    }

    assert current_values["worker_self_attesting"] is True
    assert current_values["worker_self_attestation"]["attestation_phase"] == "finish"
    assert current_values["test_results"]["passed"] is True
    assert current_values["finish_gate_ref"] == ""
    assert current_values["checkpoint_id"] == ""
    assert current_values["implementation_event_refs"] == []
    assert ("finish", "worker_self_attesting") not in gate_missing
    assert ("close", "worker_self_attesting") not in gate_missing
    assert "worker_self_attesting" not in close_missing
    assert "implementation_evidence" in next_required_ids
    assert "finish_time_worker_attestation" not in next_required_ids
    assert "finish_gate" in next_required_ids


def test_runtime_context_finish_time_attestation_requires_worker_owned_evidence() -> None:
    context = _runtime_projection_context()
    runtime_context_id = branch_runtime_context_id(PROJECT_ID, context.task_id)

    projection = build_runtime_context_projection(
        context,
        route_identity={
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
            "visible_injection_manifest_hash": "sha256:visible-runtime-context",
        },
        timeline_refs={
            "startup_event_ref": "timeline:startup-runtime-context",
            "read_receipt_event_ref": "timeline:read-runtime-context",
        },
        timeline_events=[
            {
                "id": 5264,
                "project_id": PROJECT_ID,
                "task_id": context.task_id,
                "backlog_id": context.backlog_id,
                "event_type": "mf_subagent.finish_time_worker_attestation",
                "event_kind": "worker_progress",
                "phase": "finish_time_worker_attestation",
                "status": "passed",
                "actor": "qa",
                "payload": {
                    "schema_version": "runtime_context.finish_time_worker_attestation.v1",
                    "action": "record_finish_time_worker_attestation",
                    "runtime_context_id": runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                    "backlog_id": context.backlog_id,
                    "worker_role": "mf_sub",
                    "worker_session_id": "worker-runtime-context-finish",
                    "filer_principal": "qa",
                    "graph_trace_ids": ["gqt-runtime-context"],
                    "test_results": {"status": "passed", "passed": True},
                    "finish_time_worker_self_attestation": {
                        "schema_version": "worker_transcript_self_attestation.v1",
                        "status": "passed",
                        "attestation_phase": "finish",
                        "worker_self_attesting": True,
                        "self_attesting": True,
                        "finish_time_self_attesting": True,
                        "finish_time_blockers": [],
                        "worker_session_id": "worker-runtime-context-finish",
                        "filer_principal": "qa",
                        "worker_transcript_ref": "codex:test-runtime-context",
                        "harness_type": "codex",
                    },
                },
            }
        ],
        graph_trace_refs={"trace_ids": ["gqt-runtime-context"]},
        generated_at=NOW,
    ).to_dict()

    current_values = projection["views"]["current"]["current_values"]
    next_required_ids = {
        item["id"]
        for item in projection["views"]["action_plan"]["next_required_evidence"]
    }

    assert current_values["worker_self_attesting"] is False
    assert current_values["worker_self_attestation"] == {}
    assert "finish_time_worker_attestation" in next_required_ids


def test_runtime_context_current_values_read_nested_finish_gate_attestation() -> None:
    context = _runtime_projection_context(checkpoint_id="")
    worker_attestation = {
        "schema_version": "worker_transcript_self_attestation.v1",
        "status": "passed",
        "attestation_phase": "finish",
        "worker_self_attesting": True,
        "self_attesting": True,
        "finish_time_self_attesting": True,
        "finish_time_blockers": [],
        "worker_session_id": "session-runtime-context",
        "filer_principal": "session-runtime-context",
        "worker_transcript_path": "/tmp/transcript-runtime-context.jsonl",
        "harness_type": "codex",
    }
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
        graph_trace_refs={"trace_ids": ["gqt-runtime-context"]},
        timeline_events=[
            {
                "event_id": "timeline:startup-runtime-context",
                "event_kind": "mf_subagent_startup",
                "status": "passed",
                "task_id": context.task_id,
                "backlog_id": context.backlog_id,
                "payload": {
                    "runtime_context_id": context.runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                    "backlog_id": context.backlog_id,
                },
            },
            {
                "event_id": "timeline:read-runtime-context",
                "event_kind": "mf_subagent_read_receipt",
                "status": "ok",
                "task_id": context.task_id,
                "backlog_id": context.backlog_id,
                "payload": {
                    "runtime_context_id": context.runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                    "backlog_id": context.backlog_id,
                },
            },
            {
                "event_id": "timeline:attestation-runtime-context",
                "event_kind": "worker_progress",
                "status": "passed",
                "task_id": context.task_id,
                "backlog_id": context.backlog_id,
                "payload": {
                    "action": "record_finish_time_worker_attestation",
                    "runtime_context_id": context.runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                    "backlog_id": context.backlog_id,
                    "worker_role": "mf_sub",
                    "finish_time_worker_self_attestation": worker_attestation,
                },
            },
            {
                "event_id": "timeline:finish-runtime-context",
                "event_kind": "mf_subagent_finish_gate",
                "status": "passed",
                "task_id": context.task_id,
                "backlog_id": context.backlog_id,
                "payload": {
                    "runtime_context_id": context.runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                    "backlog_id": context.backlog_id,
                    "mf_subagent_finish_gate": {
                        "checkpoint_id": "ckpt-runtime-context",
                        "worker_self_attestation": worker_attestation,
                        "worker_self_attestation_gate": {
                            "status": "passed",
                            "close_satisfying": True,
                        },
                        "test_results": {
                            "status": "passed",
                            "passed": True,
                            "command": "pytest -q",
                        },
                    },
                },
            },
        ],
        generated_at=NOW,
    ).to_dict()

    current = projection["views"]["current"]
    current_values = current["current_values"]
    gate_inputs = projection["views"]["gate_inputs"]
    close_gate = projection["views"]["close_gate_view"]
    missing = {(item["gate"], item["field"]) for item in gate_inputs["missing"]}
    close_checklist = {item["id"]: item for item in close_gate["checklist"]}
    done_state = projection["views"]["action_plan"]["done_state_projection"]

    assert current_values["worker_self_attesting"] is True
    assert current_values["worker_self_attestation"]["attestation_phase"] == "finish"
    assert current_values["test_results"]["status"] == "passed"
    assert current_values["checkpoint_id"] == "ckpt-runtime-context"
    assert ("finish", "worker_self_attesting") not in missing
    assert ("close", "worker_self_attesting") not in missing
    assert ("close", "verification_event_refs") in missing
    assert close_checklist["close_ready"]["status"] == "missing"
    assert done_state["status"] == "gap_open"
    assert done_state["close_gate_ready"] is False
    assert "verification_event_refs" in done_state["missing_close_fields"]
    assert close_gate["ready"] is False


def test_runtime_context_worker_guide_repairs_next_action_after_finish_attestation() -> None:
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
            "implementation_event_refs": ["timeline:implementation-runtime-context"],
        },
        startup_gate={
            "runtime_context_id": branch_runtime_context_id(
                PROJECT_ID,
                context.task_id,
            ),
            "fence_token_matches": True,
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
            "read_receipt_hash": "sha256:read-runtime-context",
            "read_receipt_event_id": "timeline:read-runtime-context",
            "worker_session_id": "session-runtime-context",
            "filer_principal": "session-runtime-context",
            "worker_transcript_path": "/tmp/transcript-runtime-context.jsonl",
            "worker_transcript_ref": "codex-thread:session-runtime-context",
            "harness_type": "codex",
            "worker_self_attesting": False,
            "worker_self_attestation": {
                "status": "blocked",
                "worker_self_attesting": False,
                "worker_session_id": "session-runtime-context",
                "worker_transcript_path": "/tmp/transcript-runtime-context.jsonl",
                "worker_transcript_ref": "codex-thread:session-runtime-context",
                "harness_type": "codex",
                "blockers": ["missing_finish_time_worker_self_attestation"],
            },
        },
        finish_gate={
            "worker_self_attestation": {
                "schema_version": "worker_transcript_self_attestation.v1",
                "attestation_phase": "finish",
                "status": "passed",
                "worker_self_attesting": True,
                "self_attesting": True,
                "finish_time_self_attesting": True,
                "finish_time_blockers": [],
                "worker_session_id": "session-runtime-context",
                "filer_principal": "session-runtime-context",
                "worker_transcript_path": "/tmp/transcript-runtime-context.jsonl",
                "harness_type": "codex",
            },
            "attestation_event_id": "timeline:finish-attestation-runtime-context",
        },
        target_files=["agent/governance/parallel_branch_runtime.py"],
        graph_trace_refs={"trace_ids": ["gqt-runtime-context"]},
        generated_at=NOW,
    ).to_dict()

    current_values = projection["views"]["current"]["current_values"]
    worker_view = projection["views"]["worker_view"]
    next_required = worker_view["next_required_evidence"]
    next_required_ids = [item["id"] for item in next_required]

    assert current_values["worker_self_attesting"] is True
    assert current_values["finish_gate_ref"] == ""
    assert "finish_time_worker_attestation" not in next_required_ids
    assert "finish_gate" in next_required_ids
    assert worker_view["control_plane"]["next_legal_action"] == "record_finish_gate"


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


def test_runtime_context_action_plan_prioritizes_current_worker_step_over_old_blocker() -> None:
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
            "startup_event_ref": "timeline:startup-accepted",
            "read_receipt_event_ref": "timeline:read-runtime-context",
        },
        timeline_events=[
            {
                "event_id": "timeline:startup-refused",
                "event_kind": "mf_subagent_startup",
                "task_id": context.task_id,
                "created_at": "2026-06-21T15:00:00Z",
                "status": "failed",
            }
        ],
        startup_gate={
            "runtime_context_id": branch_runtime_context_id(
                PROJECT_ID,
                context.task_id,
            ),
            "fence_token_matches": True,
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
            "read_receipt_hash": "sha256:read-runtime-context",
            "read_receipt_event_id": "timeline:read-runtime-context",
            "worker_session_id": "session-runtime-context",
            "filer_principal": "session-runtime-context",
            "worker_transcript_path": "/tmp/transcript-runtime-context.jsonl",
            "harness_type": "codex",
        },
        graph_trace_refs={"trace_ids": ["gqt-runtime-context"]},
        target_files=["agent/governance/parallel_branch_runtime.py"],
        generated_at=NOW,
    ).to_dict()

    action_plan = projection["views"]["action_plan"]
    next_required = action_plan["next_required_evidence"]
    by_id = {item["id"]: item for item in next_required}

    assert action_plan["next_legal_action"] == "record_implementation_evidence"
    assert next_required[0]["id"] == "implementation_evidence"
    assert "lane_blocking_event" in by_id
    assert by_id["lane_blocking_event"]["next_action"] == (
        "resolve_blocking_timeline_event"
    )


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
            "route_action_precheck_event_ref": "timeline:route-precheck",
            "implementation_event_refs": ["timeline:implementation"],
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
            "runtime_context_id": branch_runtime_context_id(
                PROJECT_ID,
                context.task_id,
            ),
            "fence_token_matches": True,
            "route_id": "route-runtime-context",
            "route_context_hash": "sha256:route-runtime-context",
            "prompt_contract_id": "rprompt-runtime-context",
            "prompt_contract_hash": "sha256:prompt-runtime-context",
            "route_token_ref": "rtok-runtime-context",
            "read_receipt_hash": "sha256:read-runtime-context",
            "read_receipt_event_id": "timeline:read-receipt",
            "actual_cwd": "/repo/.worktrees/mf-sub-runtime-context",
            "actual_git_root": "/repo/.worktrees/mf-sub-runtime-context",
            "worker_session_id": "session-runtime-context",
            "filer_principal": "session-runtime-context",
            "worker_transcript_path": "/tmp/transcript-runtime-context.jsonl",
            "worker_transcript_ref": "codex-thread:session-runtime-context",
            "harness_type": "codex",
            "worker_self_attesting": True,
            "worker_self_attestation": {
                "schema_version": "worker_transcript_self_attestation.v1",
                "status": "passed",
                "worker_self_attesting": True,
                "worker_session_id": "session-runtime-context",
                "worker_transcript_path": "/tmp/transcript-runtime-context.jsonl",
                "worker_transcript_ref": "codex-thread:session-runtime-context",
                "harness_type": "codex",
                "blockers": [],
            },
        },
        finish_gate={
            "checkpoint_id": "ckpt-runtime-context",
            "event_id": "timeline:finish",
            "test_results": {"status": "passed"},
            "worker_self_attestation_gate": {"passed": True},
            "worker_self_attestation": {
                "schema_version": "worker_transcript_self_attestation.v1",
                "attestation_phase": "finish",
                "status": "passed",
                "ok": True,
                "worker_self_attesting": True,
                "self_attesting": True,
                "finish_time_self_attesting": True,
                "finish_time_blockers": [],
                "worker_session_id": "session-runtime-context",
                "filer_principal": "session-runtime-context",
                "worker_transcript_path": "/tmp/transcript-runtime-context.jsonl",
                "harness_type": "codex",
                "blockers": [],
            },
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
    assert boundary["worker_execution_safety"]["status"] == "verified"
    assert boundary["worker_execution_safety"]["relative_patch_safe"] is True
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
        "project_root": "/repo",
        "repo_root": "/repo",
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
    assert worker_view["owned_files"] == ["agent/governance/parallel_branch_runtime.py"]
    assert worker_view["worker_next_moves"] == []
    assert worker_view["done_state_projection"]["status"] == (
        "validated_without_durable_merge_queue_item"
    )
    assert worker_view["control_plane"]["done_state_projection"]["status"] == (
        "validated_without_durable_merge_queue_item"
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
                "id": 44235,
                "event_kind": "route_action_precheck",
                "actor": "observer",
                "status": "accepted",
                "payload": {
                    "runtime_context_id": runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                    "backlog_id": context.backlog_id,
                    "route_id": "route-other",
                    "route_context_hash": "sha256:other-route",
                    "prompt_contract_id": "rprompt-other",
                    "prompt_contract_hash": "sha256:other-prompt",
                    "route_token_ref": "rtok-other",
                },
            },
            {
                "id": 4424,
                "event_kind": "route_action_precheck",
                "actor": "observer",
                "status": "accepted",
                "payload": {
                    "runtime_context_id": runtime_context_id,
                    "task_id": context.task_id,
                    "parent_task_id": context.root_task_id,
                    "backlog_id": context.backlog_id,
                    "route_id": "route-timeline",
                    "route_context_hash": "sha256:timeline-route",
                    "prompt_contract_id": "rprompt-timeline",
                    "prompt_contract_hash": "sha256:timeline-prompt",
                    "route_token_ref": "rtok-timeline",
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
                    "worker_self_attestation_gate": {"passed": True},
                    "worker_self_attestation": {
                        "schema_version": "worker_transcript_self_attestation.v1",
                        "attestation_phase": "finish",
                        "status": "passed",
                        "ok": True,
                        "worker_self_attesting": True,
                        "self_attesting": True,
                        "finish_time_self_attesting": True,
                        "finish_time_blockers": [],
                        "worker_session_id": "worker-runtime-context",
                        "filer_principal": "worker-runtime-context",
                        "worker_transcript_path": "/tmp/transcript-runtime-context.jsonl",
                        "harness_type": "codex",
                        "blockers": [],
                    },
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
    assert close_gate["route_action_precheck_event_ref"] == "timeline:4424"
    assert close_gate["finish_gate_ref"] == "timeline:4427"
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
        "gate_projection",
        "gate_inputs",
        "worker_view",
        "observer_view",
        "qa_view",
        "judge_view",
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
    assert "fence_token" not in gate
    assert gate["fence_token_present"] is True
    assert gate["fence_token_hash"] == runtime_context_secret_hash("fence-startup")
    assert gate["fence_token_redacted"] is True
    assert gate["raw_fence_token_exposed"] is False
    assert gate["runtime_context_id"] == branch_runtime_context_id(PROJECT_ID, "mf-sub-startup")
    assert gate["observer_command_id"] == "cmd-startup"
    assert gate["route_id"] == "route-startup"
    assert gate["visible_injection_manifest_hash"] == "sha256:visible-startup"
    assert gate["owned_files"] == ["agent/governance/parallel_branch_runtime.py"]
    assert gate["read_receipt_hash"] == "sha256:read-startup"
    assert gate["worker_self_attesting"] is True
    assert gate["worker_self_attestation"]["status"] == "passed"
    assert gate["close_satisfying"] is True
    assert gate["graph_trace_db_evidence"]["db_verified"] is True
    assert gate["graph_trace_db_evidence"]["trace_ids"] == ["gqt-startup"]
    assert gate["worker_self_attestation"]["worker_session_id"] == "codex-session-startup"
    assert gate["worker_transcript_ref"] == "codex-thread:codex-session-startup"
    assert gate["identity_join"]["runtime_context_id_matches"] is True
    assert gate["identity_join"]["route_identity_matches_latest_contract"] is True
    assert gate["identity_join"]["read_receipt_lineage_present"] is True
    assert "secret-worker-session-token" not in str(result)
    assert "fence-startup" not in json.dumps(
        result["timeline_event"]["payload"]["mf_subagent_startup_gate"],
        sort_keys=True,
    )
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

    accepted_by_runtime_context = validate_mf_subagent_graph_query_identity(
        conn,
        project_id=PROJECT_ID,
        runtime_context_id=branch_runtime_context_id(PROJECT_ID, "mf-sub-startup"),
        task_id="",
        parent_task_id="",
        worker_role="mf_sub",
        fence_token="fence-startup",
        session_token="secret-worker-session-token",
        route_identity={
            "route_id": "route-startup",
            "route_context_hash": "sha256:route-startup",
            "prompt_contract_id": "rprompt-startup",
            "prompt_contract_hash": "sha256:prompt-startup",
            "route_token_ref": "rtok-startup",
            "visible_injection_manifest_hash": "sha256:visible-startup",
        },
    )
    assert accepted_by_runtime_context.task_id == "mf-sub-startup"

    with pytest.raises(BranchRuntimeFenceError, match="runtime_context_task_mismatch"):
        validate_mf_subagent_graph_query_identity(
            conn,
            project_id=PROJECT_ID,
            runtime_context_id=branch_runtime_context_id(PROJECT_ID, "mf-sub-startup"),
            task_id="other-task",
            parent_task_id="",
            worker_role="mf_sub",
            fence_token="fence-startup",
            session_token="secret-worker-session-token",
        )

    with pytest.raises(BranchRuntimeFenceError, match="route_identity_mismatch"):
        validate_mf_subagent_graph_query_identity(
            conn,
            project_id=PROJECT_ID,
            runtime_context_id=branch_runtime_context_id(PROJECT_ID, "mf-sub-startup"),
            task_id="",
            parent_task_id="",
            worker_role="mf_sub",
            fence_token="fence-startup",
            session_token="secret-worker-session-token",
            route_identity={
                "route_id": "route-startup",
                "route_context_hash": "sha256:wrong-route",
                "prompt_contract_id": "rprompt-startup",
                "prompt_contract_hash": "sha256:prompt-startup",
                "route_token_ref": "rtok-startup",
                "visible_injection_manifest_hash": "sha256:visible-startup",
            },
        )

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


def test_mf_sub_runtime_session_token_expiry_reports_bounded_reissue_hint(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    target_root = tmp_path / "expired-session-target"
    target_root.mkdir()
    expired_context = upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            governance_project_id=PROJECT_ID,
            target_project_id=PROJECT_ID,
            target_project_root=str(target_root),
            task_id="mf-sub-expired-session",
            root_task_id="parent-expired-session",
            stage_task_id="mf-sub-expired-session",
            worker_id="worker-expired-session",
            worker_slot_id="worker-expired-session",
            branch_ref="refs/heads/codex/mf-sub-expired-session",
            status=STATE_WORKTREE_READY,
            fence_token="fence-expired-session",
            session_token_hash=mf_subagent_session_token_hash("expired-session-token"),
            lease_id="lease-expired-session",
            lease_expires_at="2000-01-01T00:00:00Z",
        ),
    )
    conn.commit()

    with pytest.raises(BranchRuntimeFenceError) as expired:
        validate_mf_subagent_graph_query_identity(
            conn,
            project_id=PROJECT_ID,
            runtime_context_id=expired_context.runtime_context_id,
            task_id="",
            parent_task_id="parent-expired-session",
            worker_role="mf_sub",
            fence_token="fence-expired-session",
            session_token="expired-session-token",
            target_project_root=str(target_root),
        )

    assert str(expired.value) == "runtime_session_token_expired"
    details = expired.value.details
    assert details["reason"] == "runtime_session_token_expired"
    assert details["session_token_lease"]["expired"] is True
    assert details["session_token_lease"]["raw_session_token_persisted"] is False
    assert details["renewal"]["available"] is True
    assert details["renewal"]["max_ttl_seconds"] == MF_SUBAGENT_SESSION_REISSUE_MAX_TTL_SECONDS
    assert "session_token" in details["renewal"]["required_body_fields"]

    with pytest.raises(BranchRuntimeFenceError) as wrong_token:
        validate_mf_subagent_graph_query_identity(
            conn,
            project_id=PROJECT_ID,
            runtime_context_id=expired_context.runtime_context_id,
            task_id="",
            parent_task_id="parent-expired-session",
            worker_role="mf_sub",
            fence_token="fence-expired-session",
            session_token="wrong-session-token",
            target_project_root=str(target_root),
        )

    assert str(wrong_token.value) == "fence_invalidated_or_unknown"
    assert getattr(wrong_token.value, "details", {}) == {}


def test_reissue_mf_sub_runtime_session_token_rotates_hash_and_fails_closed(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    target_root = tmp_path / "reissue-session-target"
    target_root.mkdir()
    context = upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            governance_project_id=PROJECT_ID,
            target_project_id=PROJECT_ID,
            target_project_root=str(target_root),
            task_id="mf-sub-reissue-session",
            root_task_id="parent-reissue-session",
            stage_task_id="mf-sub-reissue-session",
            backlog_id="BUG-REISSUE-SESSION",
            worker_id="worker-reissue-session",
            worker_slot_id="slot-reissue-session",
            branch_ref="refs/heads/codex/mf-sub-reissue-session",
            status=STATE_WORKTREE_READY,
            fence_token="fence-reissue-session",
            session_token_hash=mf_subagent_session_token_hash("old-reissue-token"),
            lease_id="lease-old-reissue",
            lease_expires_at="2000-01-01T00:00:00Z",
        ),
    )
    conn.commit()

    result = reissue_mf_subagent_runtime_session_token(
        conn,
        project_id=PROJECT_ID,
        runtime_context_id=context.runtime_context_id,
        task_id="mf-sub-reissue-session",
        parent_task_id="parent-reissue-session",
        fence_token="fence-reissue-session",
        session_token="old-reissue-token",
        target_project_root=str(target_root),
        ttl_seconds=999999,
        now_iso="2999-01-01T00:00:00Z",
    )

    assert result["ok"] is True
    assert result["ttl_seconds"] == MF_SUBAGENT_SESSION_REISSUE_MAX_TTL_SECONDS
    assert result["expires_at"] == "2999-01-01T08:00:00Z"
    assert result["session_token_persisted"] is False
    assert result["raw_session_token_persisted"] is False
    assert result["session_token_lease"]["lease_remaining_ttl_seconds"] == (
        MF_SUBAGENT_SESSION_REISSUE_MAX_TTL_SECONDS
    )
    new_token = result["session_token"]
    saved = get_branch_context(conn, PROJECT_ID, "mf-sub-reissue-session")
    assert saved is not None
    assert saved.session_token_hash == mf_subagent_session_token_hash(new_token)
    assert saved.session_token_hash != mf_subagent_session_token_hash("old-reissue-token")
    assert saved.lease_id.startswith("mfrlease-")

    with pytest.raises(BranchRuntimeFenceError):
        validate_mf_subagent_graph_query_identity(
            conn,
            project_id=PROJECT_ID,
            runtime_context_id=context.runtime_context_id,
            task_id="",
            parent_task_id="parent-reissue-session",
            worker_role="mf_sub",
            fence_token="fence-reissue-session",
            session_token="old-reissue-token",
            target_project_root=str(target_root),
        )
    accepted = validate_mf_subagent_graph_query_identity(
        conn,
        project_id=PROJECT_ID,
        runtime_context_id=context.runtime_context_id,
        task_id="",
        parent_task_id="parent-reissue-session",
        worker_role="mf_sub",
        fence_token="fence-reissue-session",
        session_token=new_token,
        target_project_root=str(target_root),
    )
    assert accepted.task_id == "mf-sub-reissue-session"

    for bad_request in (
        {"fence_token": "wrong-fence", "session_token": new_token},
        {"fence_token": "fence-reissue-session", "session_token": "wrong-token"},
        {"target_project_root": str(target_root / "other"), "session_token": new_token},
    ):
        with pytest.raises(BranchRuntimeFenceError):
            reissue_mf_subagent_runtime_session_token(
                conn,
                project_id=PROJECT_ID,
                runtime_context_id=context.runtime_context_id,
                task_id="mf-sub-reissue-session",
                parent_task_id="parent-reissue-session",
                fence_token=bad_request.get("fence_token", "fence-reissue-session"),
                session_token=bad_request["session_token"],
                target_project_root=bad_request.get("target_project_root", str(target_root)),
            )

    closed = upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            target_project_root=str(target_root),
            task_id="mf-sub-reissue-closed",
            root_task_id="parent-reissue-session",
            stage_task_id="mf-sub-reissue-closed",
            worker_id="worker-reissue-closed",
            branch_ref="refs/heads/codex/mf-sub-reissue-closed",
            status=STATE_MERGE_FAILED,
            fence_token="fence-reissue-closed",
            session_token_hash=mf_subagent_session_token_hash("closed-token"),
            lease_id="lease-closed",
            lease_expires_at="2000-01-01T00:00:00Z",
        ),
    )
    with pytest.raises(BranchRuntimeFenceError):
        reissue_mf_subagent_runtime_session_token(
            conn,
            project_id=PROJECT_ID,
            runtime_context_id=closed.runtime_context_id,
            task_id="mf-sub-reissue-closed",
            parent_task_id="parent-reissue-session",
            fence_token="fence-reissue-closed",
            session_token="closed-token",
            target_project_root=str(target_root),
        )


def test_runtime_session_token_lease_view_reports_remaining_ttl_without_raw_token() -> None:
    context = BranchTaskRuntimeContext(
        project_id=PROJECT_ID,
        task_id="mf-sub-lease-view",
        branch_ref="refs/heads/codex/mf-sub-lease-view",
        status=STATE_RUNNING,
        fence_token="fence-lease-view",
        session_token_hash=mf_subagent_session_token_hash("lease-view-token"),
        lease_id="lease-view",
        lease_expires_at="2026-06-16T13:00:00Z",
    )

    lease = runtime_context_session_token_lease_view(
        context,
        now_iso="2026-06-16T12:15:00Z",
    )

    assert lease["has_lease"] is True
    assert lease["status"] == "active"
    assert lease["expired"] is False
    assert lease["lease_remaining_ttl_seconds"] == 2700
    assert lease["renewal_supported"] is True
    assert lease["raw_session_token_exposed"] is False
    assert lease["raw_session_token_persisted"] is False


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
            worker_transcript_ref="",
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
    assert "missing_worker_transcript_ref_or_path" in gate["worker_self_attestation"]["blockers"]


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


def test_worker_transcript_mf_sub_startup_accepts_identity_before_owned_diff(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-idle"
    worktree.mkdir(parents=True)
    _insert_startup_context(conn, str(worktree))
    base_commit = _git(worktree, "rev-list", "--max-parents=0", "HEAD")

    idle = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(
            str(worktree),
            changed_files=[],
            head_commit=base_commit,
        ),
        now_iso=NOW,
    )
    idle_gate = idle["startup_gate"]
    assert idle["ok"] is True
    assert idle_gate["actual_startup_recorded"] is True
    assert idle_gate["worker_self_attesting"] is True
    assert idle_gate["worker_self_attestation"]["status"] == "passed"
    assert idle_gate["worker_self_attestation"]["attestation_phase"] == "startup"
    assert idle_gate["worker_self_attestation"]["finish_time_self_attesting"] is False
    assert "no_owned_files_diff" in idle_gate["worker_self_attestation"][
        "finish_time_blockers"
    ]
    assert idle_gate["close_satisfying"] is False


def test_worker_transcript_finish_accepts_uncommitted_owned_diff(
    tmp_path,
) -> None:
    worktree = tmp_path / "workers" / "mf-sub-finish-uncommitted"
    _base_commit, head_commit = _ensure_startup_git_worktree(worktree)
    source_path = worktree / "agent" / "governance" / "parallel_branch_runtime.py"
    source_path.write_text(
        "base runtime\nhead runtime\nworker uncommitted runtime\n",
        encoding="utf-8",
    )
    test_path = worktree / "tests" / "reminders.test.mjs"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text("console.log('worker test');\n", encoding="utf-8")

    payload = {
        "task_id": "mf-sub-finish-uncommitted",
        "runtime_context_id": branch_runtime_context_id(
            PROJECT_ID,
            "mf-sub-finish-uncommitted",
        ),
        "fence_token": "fence-finish-uncommitted",
        "worktree_path": str(worktree),
        "branch_ref": "refs/heads/codex/mf-sub-finish-uncommitted",
        "base_commit": head_commit,
        "head_commit": head_commit,
        "owned_files": [
            "agent/governance/parallel_branch_runtime.py",
            "tests/reminders.test.mjs",
        ],
        "changed_files": [
            "agent/governance/parallel_branch_runtime.py",
            "tests/reminders.test.mjs",
        ],
        "graph_trace_ids": ["gqt-finish-uncommitted"],
        "graph_trace_db_evidence": {
            "db_verified": True,
            "verified_trace_ids": ["gqt-finish-uncommitted"],
            "missing_trace_ids": [],
            "identity_mismatches": [],
        },
        "observer_command_id": "cmd-finish-uncommitted",
        "read_receipt_hash": "sha256:read-finish-uncommitted",
        "read_receipt_event_id": "4187",
        "route_token_ref": "rtok-finish-uncommitted",
        "worker_session_id": "codex-finish-uncommitted",
        "filer_principal": "codex-finish-uncommitted",
        "worker_transcript_ref": "multi_agent:codex-finish-uncommitted",
        "harness_type": "codex",
        "attestation_phase": "finish",
        "test_results": {"status": "passed", "passed": True},
    }

    result = verify_worker_transcript(payload)

    assert result["ok"] is True, result
    assert result["finish_time_self_attesting"] is True
    diff_layer = next(
        layer for layer in result["layers"] if layer["id"] == "owned_files_diff"
    )
    assert diff_layer["changed_files"] == [
        "agent/governance/parallel_branch_runtime.py",
        "tests/reminders.test.mjs",
    ]


def test_worker_transcript_mf_sub_startup_marks_no_graph_trace_not_self_attesting(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-no-graph"
    worktree.mkdir(parents=True)
    _insert_startup_context(conn, str(worktree))

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
    finish_blockers = result["startup_gate"]["worker_self_attestation"][
        "finish_time_blockers"
    ]
    assert result["ok"] is True
    assert result["startup_gate"]["worker_self_attesting"] is False
    assert any(
        blocker.startswith("claimed_changed_files_do_not_match_git_diff")
        for blocker in finish_blockers
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
        "finish_time_worker_self_attestation": {
            "schema_version": "worker_transcript_self_attestation.v1",
            "attestation_phase": "finish",
            "status": "passed",
            "ok": True,
            "worker_self_attesting": True,
            "self_attesting": True,
            "finish_time_self_attesting": True,
            "finish_time_blockers": [],
            "worker_session_id": f"session-{context.task_id}",
            "filer_principal": f"session-{context.task_id}",
            "worker_transcript_path": f"/tmp/transcript-{context.task_id}.jsonl",
            "harness_type": "codex",
            "blockers": [],
        },
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
        "fence_token_matches": True,
        "actual_cwd": context.worktree_path,
        "actual_git_root": context.worktree_path,
        "worktree_path": context.worktree_path,
        "branch_ref": context.branch_ref,
        "head_commit": context.head_commit,
        "route_id": f"route-{context.task_id}",
        "route_context_hash": "sha256:route-finish-startup",
        "prompt_contract_id": f"rprompt-{context.task_id}",
        "prompt_contract_hash": "sha256:prompt-finish-startup",
        "route_token_ref": f"rtok-{context.task_id}",
        "read_receipt_hash": f"sha256:rr-{context.task_id}",
        "observer_command_id": f"cmd-{context.task_id}",
        "read_receipt_event_id": f"rr-{context.task_id}",
    }
    if include_worker_fields:
        gate.update(
            {
                "worker_session_id": f"session-{context.task_id}",
                "filer_principal": f"session-{context.task_id}",
                "worker_transcript_path": f"/tmp/transcript-{context.task_id}.jsonl",
                "worker_transcript_ref": f"codex-thread:{context.task_id}",
                "harness_type": "codex",
                "worker_self_attesting": True,
                "self_attesting": True,
                "worker_self_attestation": {
                    "schema_version": "worker_transcript_self_attestation.v1",
                    "status": "passed",
                    "worker_self_attesting": True,
                    "worker_session_id": f"session-{context.task_id}",
                    "worker_transcript_path": f"/tmp/transcript-{context.task_id}.jsonl",
                    "worker_transcript_ref": f"codex-thread:{context.task_id}",
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
    _insert_startup_graph_trace(conn)

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
    _insert_startup_graph_trace(conn)

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
    assert gate["worker_self_attesting"] is False
    assert gate["close_satisfying"] is False
    assert gate["host_adapter_startup_surrogate_not_close_satisfying"] is True
    assert gate["worker_self_attestation"]["status"] == "blocked"
    assert "host_adapter_startup_surrogate_not_close_satisfying" in gate[
        "worker_self_attestation"
    ]["blockers"]


def test_mf_sub_startup_service_dispatch_host_metadata_is_not_surrogate(
    tmp_path,
) -> None:
    from agent.governance import task_timeline

    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-service-dispatch-host"
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
        agent_id="observer-allocation-owner",
        allocation_owner="observer-allocation-owner",
        branch_ref="refs/heads/codex/mf-sub-startup",
        status=STATE_WORKTREE_READY,
        fence_token="fence-startup",
        worktree_path=str(worktree),
        base_commit=base_commit,
        head_commit=head_commit,
        target_head_commit="target-startup",
        merge_queue_id="mq-startup",
        session_token_hash=mf_subagent_session_token_hash(
            "secret-worker-session-token"
        ),
    )
    upsert_branch_context(conn, context, now_iso=NOW)
    runtime_context_id = branch_runtime_context_id(PROJECT_ID, "mf-sub-startup")
    route_identity = {
        "route_id": "route-startup",
        "route_context_hash": "sha256:route-startup",
        "prompt_contract_id": "rprompt-startup",
        "prompt_contract_hash": "sha256:prompt-startup",
        "route_token_ref": "rtok-startup",
        "visible_injection_manifest_hash": "sha256:visible-startup",
    }
    service_agent_id = "service-dispatch-host-worker"
    host_startup_id = "multi_agent_v1.spawn_agent:service-dispatch-host-worker"
    append_branch_contract_revision(
        conn,
        context,
        payload={
            "registered_host_adapter_spawn": {
                "schema_version": "mf_subagent_host_adapter_spawn_identity.v1",
                "source": "test_registered_host_adapter_spawn",
                "runtime_context_id": runtime_context_id,
                "task_id": "mf-sub-startup",
                "worker_slot_id": "worker-startup",
                "agent_id": service_agent_id,
                "actual_host_worker_id": service_agent_id,
                "host_startup_id": host_startup_id,
                "host_session_id": host_startup_id,
            }
        },
        route_identity=route_identity,
        now_iso=NOW,
    )
    _insert_startup_graph_trace(conn)
    task_timeline.ensure_schema(conn)
    dispatch_event = task_timeline.record_event(
        conn,
        project_id=PROJECT_ID,
        task_id="parent-startup",
        backlog_id="BUG-STARTUP",
        event_type="observer.subagent.service_dispatch",
        event_kind="observer_subagent_service_dispatch",
        phase="dispatch",
        status="accepted",
        actor="observer",
        payload={
            "schema_version": "observer_subagent_service_dispatch.v1",
            "observer_command_id": "cmd-startup",
            **route_identity,
            "workers": [
                {
                    "runtime_context_id": runtime_context_id,
                    "task_id": "mf-sub-startup",
                    "worker_id": "worker-startup",
                    "worker_slot_id": "worker-startup",
                    "agent_id": service_agent_id,
                    "actual_host_worker_id": service_agent_id,
                    "worker_session_id": service_agent_id,
                    "transcript_ref": f"multi_agent:{service_agent_id}",
                    "session_token_ref": runtime_context_session_token_ref(
                        context
                    ),
                }
            ],
        },
    )
    conn.commit()

    result = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(
            str(worktree),
            agent_id=service_agent_id,
            actual_host_worker_id=service_agent_id,
            worker_session_id=service_agent_id,
            worker_transcript_ref=f"multi_agent:{service_agent_id}",
            session_token="",
            session_token_ref=runtime_context_session_token_ref(context),
            startup_source="codex_host_spawn_agent",
            host_startup_id=host_startup_id,
        ),
        now_iso=NOW,
    )

    assert result["ok"] is True
    gate = result["startup_gate"]
    assert gate["agent_id_match_mode"] == "observer_subagent_service_dispatch"
    assert gate["host_adapter_startup_token_accepted"] is True
    assert gate["session_token_evidence_type"] == "server_verified_ref"
    assert gate["server_issued_session_token_verified"] is True
    assert gate["service_dispatch_worker_binding_present"] is True
    assert gate["service_dispatch_worker_binding"]["event_ref"] == (
        f"timeline:{dispatch_event['id']}"
    )
    assert gate["host_adapter_startup_surrogate_not_close_satisfying"] is False
    assert "host_adapter_startup_surrogate_not_close_satisfying" not in gate[
        "worker_self_attestation"
    ]["blockers"]
    assert gate["worker_self_attesting"] is True
    assert gate["close_satisfying"] is True


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
    _insert_startup_graph_trace(conn)

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
    assert gate["worker_self_attesting"] is False
    assert gate["close_satisfying"] is False
    assert "host_adapter_startup_surrogate_not_close_satisfying" in gate[
        "worker_self_attestation"
    ]["blockers"]


def test_mf_sub_startup_accepts_runtime_text_prepare_placeholder_host_agent(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-runtime-text-host"
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
        agent_id="worker-startup",
        allocation_owner="worker-startup",
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
    runtime_context_id = branch_runtime_context_id(PROJECT_ID, "mf-sub-startup")
    append_branch_contract_revision(
        conn,
        context,
        payload={
            "registered_host_adapter_spawn": {
                "schema_version": "mf_subagent_host_adapter_spawn_identity.v1",
                "source": "observer_runtime_text_prepare",
                "registration_source": "runtime_text_prepare",
                "runtime_context_id": runtime_context_id,
                "observer_command_id": "cmd-startup",
                "launch_text_hash": "sha256:launch-runtime-text",
                "task_id": "mf-sub-startup",
                "worker_slot_id": "worker-startup",
                "agent_id": "host_adapter_agent:host_adapter:placeholder",
                "actual_host_worker_id": "host_adapter_agent:host_adapter:placeholder",
                "host_startup_id": "host_adapter:host_adapter:placeholder",
                "host_session_id": "host_adapter:host_adapter:placeholder",
                "session_token_surrogate": "host-adapter:placeholder",
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
            actual_host_worker_id="",
            agent_id="019ec4d9-755a-75a1-9216-d4805655a685",
            session_token="",
            session_token_surrogate="host-adapter:placeholder",
            startup_source="codex_host_spawn_agent",
            host_startup_id="host_adapter:host_adapter:placeholder",
            launch_text_hash="sha256:launch-runtime-text",
        ),
        now_iso=NOW,
    )

    assert result["ok"] is True
    gate = result["startup_gate"]
    saved = get_branch_context(conn, PROJECT_ID, "mf-sub-startup")
    assert saved is not None
    assert saved.actual_host_worker_id == "019ec4d9-755a-75a1-9216-d4805655a685"
    assert gate["agent_id_match_mode"] == "host_adapter_startup_token_surrogate"
    assert gate["host_adapter_startup_token_accepted"] is True
    assert gate["close_satisfying"] is False
    assert gate["session_token_evidence_type"] == "surrogate"


def test_mf_sub_startup_accepts_nested_runtime_text_registered_host_identity(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-nested-runtime-text-host"
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
        agent_id="worker-startup",
        allocation_owner="worker-startup",
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
    runtime_context_id = branch_runtime_context_id(PROJECT_ID, "mf-sub-startup")
    registered_identity = {
        "schema_version": "mf_subagent_host_adapter_spawn_identity.v1",
        "source": "observer_runtime_text_prepare",
        "registration_source": "runtime_text_prepare",
        "runtime_context_id": runtime_context_id,
        "observer_command_id": "cmd-startup",
        "launch_text_hash": "sha256:launch-runtime-text",
        "task_id": "mf-sub-startup",
        "worker_slot_id": "worker-startup",
        "agent_id": "host_adapter_agent:host_adapter:nested",
        "actual_host_worker_id": "host_adapter_agent:host_adapter:nested",
        "host_startup_id": "host_adapter:host_adapter:nested",
        "host_session_id": "host_adapter:host_adapter:nested",
        "session_token_surrogate": "host-adapter:nested",
    }
    append_branch_contract_revision(
        conn,
        context,
        payload={"registered_host_adapter_spawn": registered_identity},
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
            actual_host_worker_id="",
            agent_id="019ec4d9-755a-75a1-9216-d4805655a685",
            session_token="",
            session_token_surrogate="",
            startup_source="codex_host_spawn_agent",
            host_startup_id="",
            launch_text_hash="sha256:launch-runtime-text",
            registered_host_adapter_spawn=registered_identity,
        ),
        now_iso=NOW,
    )

    assert result["ok"] is True
    gate = result["startup_gate"]
    assert gate["agent_id_match_mode"] == "host_adapter_startup_token_surrogate"
    assert gate["host_adapter_startup_token_accepted"] is True
    assert gate["host_startup_id"] == "host_adapter:host_adapter:nested"
    assert gate["session_token_surrogate"] == "host-adapter:nested"
    assert gate["session_token_evidence_type"] == "surrogate"
    assert gate["close_satisfying"] is False


def test_mf_sub_startup_refusal_lists_identity_fields_and_retry_payload(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-identity-refusal"
    worktree.mkdir(parents=True)
    _insert_startup_context(conn, str(worktree))

    result = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup",
        payload=_startup_payload(
            str(worktree),
            session_token="",
            session_token_surrogate="",
            host_startup_id="",
        ),
        now_iso=NOW,
    )

    assert result["ok"] is False
    assert result["blocker_id"] == (
        "no_truthful_bounded_mf_sub_startup_surface_available"
    )
    assert "session_token_surrogate_or_host_startup_id" in result[
        "missing_required_fields"
    ]
    identity_fields = result["startup_identity_fields"]
    assert "session_token" in identity_fields["missing_fields"]
    assert "host_startup_id" in identity_fields["missing_fields"]
    next_action = result["next_action"]
    assert next_action["canonical_retry_payload_source"] == (
        "worker_launch_pack.startup_recording"
    )
    assert next_action["startup_payload_source"] == (
        "worker_launch_pack.startup_recording"
    )
    assert "copyable_retry_payload" in next_action
    assert next_action["copyable_retry_payload"]["append_tool"] == (
        "parallel_branch_startup"
    )
    assert next_action["copyable_retry_payload"]["host_startup_id"] == (
        "<fill host_startup_id or session_token_surrogate>"
    )
    assert next_action["copyable_retry_payload"]["session_token_surrogate"] == (
        "<fill session_token_surrogate or host_startup_id>"
    )
    assert "session_token_surrogate_or_host_startup_id" not in next_action[
        "copyable_retry_payload"
    ]
    refusal = result["timeline_event"]["payload"]["mf_subagent_startup_refusal"]
    assert refusal["present_startup_identity_fields"] == (
        next_action["present_startup_identity_fields"]
    )
    assert refusal["missing_startup_identity_fields"] == (
        next_action["missing_startup_identity_fields"]
    )


def test_mf_sub_graph_query_accepts_runtime_text_host_adapter_without_raw_token(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    context = BranchTaskRuntimeContext(
        project_id=PROJECT_ID,
        task_id="mf-sub-graph-runtime-text",
        root_task_id="parent-startup",
        stage_task_id="mf-sub-graph-runtime-text",
        backlog_id="BUG-STARTUP",
        worker_id="worker-startup",
        worker_slot_id="worker-startup",
        agent_id="worker-startup",
        allocation_owner="worker-startup",
        branch_ref="refs/heads/codex/mf-sub-graph-runtime-text",
        status=STATE_WORKTREE_READY,
        fence_token="fence-startup",
        worktree_path=str(tmp_path / "worker"),
        session_token_hash=mf_subagent_session_token_hash("server-issued-token"),
    )
    upsert_branch_context(conn, context, now_iso=NOW)
    append_branch_contract_revision(
        conn,
        context,
        payload={
            "registered_host_adapter_spawn": {
                "schema_version": "mf_subagent_host_adapter_spawn_identity.v1",
                "source": "observer_runtime_text_prepare",
                "registration_source": "runtime_text_prepare",
                "runtime_context_id": branch_runtime_context_id(
                    PROJECT_ID,
                    "mf-sub-graph-runtime-text",
                ),
                "observer_command_id": "cmd-startup",
                "launch_text_hash": "sha256:launch-runtime-text",
                "task_id": "mf-sub-graph-runtime-text",
                "worker_slot_id": "worker-startup",
                "agent_id": "host_adapter_agent:host_adapter:placeholder",
                "actual_host_worker_id": "host_adapter_agent:host_adapter:placeholder",
                "host_startup_id": "host_adapter:host_adapter:placeholder",
                "host_session_id": "host_adapter:host_adapter:placeholder",
                "session_token_surrogate": "host-adapter:placeholder",
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

    accepted = validate_mf_subagent_graph_query_identity(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-graph-runtime-text",
        parent_task_id="parent-startup",
        worker_role="mf_sub",
        fence_token="fence-startup",
        session_token="",
    )

    assert accepted.task_id == "mf-sub-graph-runtime-text"


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


def test_mf_sub_startup_refusal_points_session_ref_workers_to_initial_join(
    tmp_path,
) -> None:
    conn = _runtime_conn()
    worktree = tmp_path / "workers" / "mf-sub-startup-session-ref-join"
    worktree.mkdir(parents=True)
    base_commit, head_commit = _ensure_startup_git_worktree(worktree)
    context = BranchTaskRuntimeContext(
        project_id=PROJECT_ID,
        task_id="mf-sub-startup-session-ref-join",
        root_task_id="parent-startup-session-ref-join",
        stage_task_id="mf-sub-startup-session-ref-join",
        backlog_id="BUG-STARTUP-SESSION-REF-JOIN",
        worker_id="worker-startup-session-ref-join",
        worker_slot_id="slot-startup-session-ref-join",
        agent_id="agent-startup-session-ref-join",
        allocation_owner="agent-startup-session-ref-join",
        branch_ref="refs/heads/codex/mf-sub-startup-session-ref-join",
        status=STATE_WORKTREE_READY,
        fence_token="fence-startup-session-ref-join",
        worktree_path=str(worktree),
        base_commit=base_commit,
        head_commit=head_commit,
        target_head_commit="target-startup-session-ref-join",
        merge_queue_id="mq-startup-session-ref-join",
        session_token_hash=mf_subagent_session_token_hash(
            "raw-startup-session-ref-join"
        ),
    )
    context = upsert_branch_context(conn, context, now_iso=NOW)
    route_identity = {
        "route_id": "route-startup-session-ref-join",
        "route_context_hash": "sha256:route-startup-session-ref-join",
        "prompt_contract_id": "rprompt-startup-session-ref-join",
        "prompt_contract_hash": "sha256:prompt-startup-session-ref-join",
        "route_token_ref": "rtok-startup-session-ref-join",
        "visible_injection_manifest_hash": "sha256:visible-startup-session-ref-join",
    }
    append_branch_contract_revision(
        conn,
        context,
        payload={"target_files": ["agent/governance/server.py"]},
        route_identity=route_identity,
        now_iso=NOW,
    )

    result = record_mf_subagent_startup(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-startup-session-ref-join",
        payload={
            "runtime_context_id": context.runtime_context_id,
            "parent_task_id": "parent-startup-session-ref-join",
            "worker_role": "mf_sub",
            "fence_token": "fence-startup-session-ref-join",
            "session_token_ref": runtime_context_session_token_ref(context),
            "actual_cwd": str(worktree),
            "actual_git_root": str(worktree),
            "branch": "refs/heads/codex/mf-sub-startup-session-ref-join",
            "head_commit": head_commit,
            "base_commit": base_commit,
            "target_head_commit": "target-startup-session-ref-join",
            "merge_queue_id": "mq-startup-session-ref-join",
            "governance_project_id": PROJECT_ID,
            "target_project_id": PROJECT_ID,
            "owned_files": ["agent/governance/server.py"],
            "observer_command_id": "cmd-startup-session-ref-join",
            "read_receipt_hash": "sha256:read-startup-session-ref-join",
            "read_receipt_event_id": "read-startup-session-ref-join",
            **route_identity,
        },
        now_iso=NOW,
    )

    assert result["ok"] is False
    assert result["blocker_id"] == "no_truthful_bounded_mf_sub_startup_surface_available"
    assert "actual_host_worker_id" in result["missing_required_fields"]
    next_action = result["next_action"]
    assert next_action["action"] == "request_runtime_context_initial_join_host_envelope"
    join_before_startup = next_action["join_before_parallel_branch_startup"]
    assert join_before_startup["copy_safe_body"]["agent_id"] == (
        "agent-startup-session-ref-join"
    )
    assert join_before_startup["copy_safe_body"]["allocation_owner"] == (
        "agent-startup-session-ref-join"
    )
    assert join_before_startup["copy_safe_body"]["route_token_ref"] == (
        "rtok-startup-session-ref-join"
    )
    assert join_before_startup["security_boundary"][
        "session_token_ref_alone_authorizes_writes"
    ] is False
    retry_payload = join_before_startup["then"]["copyable_retry_payload"]
    assert retry_payload["append_tool"] == "parallel_branch_startup"
    assert retry_payload["actual_host_worker_id"] == (
        "<fill actual_host_worker_id>"
    )


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


def test_mf_branch_allocation_evidence_hydrates_contract_single_source_fields() -> None:
    conn = _runtime_conn()
    owned_files = (
        "agent/governance/parallel_branch_runtime.py",
        "agent/observer_runtime.py",
    )
    context = plan_branch_runtime_context(
        project_id=PROJECT_ID,
        task_id="MF Contract Hydration",
        batch_id="PB-CONTRACT",
        backlog_id="AC-RUNTIME-CONTRACT",
        chain_id="cex-hotfix-successor",
        root_task_id="cex-onboard-root",
        stage_task_id="MF Contract Hydration",
        agent_id="observer",
        worker_id="worker one",
        workspace_root="/repo",
        target_project_root="",
        target_files=(),
        owned_files=owned_files,
        base_commit="base123",
        target_head_commit="target123",
        merge_queue_id="mq-contract",
        fence_token="fence-contract",
    )

    saved = upsert_branch_context(conn, context, now_iso=NOW)
    reloaded = get_branch_context(conn, PROJECT_ID, "MF Contract Hydration")

    assert reloaded is not None
    expected_root = "/repo/.worktrees/worker-one/mf-contract-hydration"
    assert saved.target_project_root == expected_root
    assert saved.target_files == owned_files
    assert saved.owned_files == owned_files
    assert reloaded.target_project_root == expected_root
    assert reloaded.target_files == owned_files
    assert reloaded.owned_files == owned_files

    context_payload = branch_context_to_dict(reloaded)
    assert context_payload["target_project_root"] == expected_root
    assert context_payload["project_root"] == expected_root
    assert context_payload["repo_root"] == expected_root
    assert context_payload["target_files"] == list(owned_files)
    assert context_payload["owned_files"] == list(owned_files)

    evidence = branch_runtime_allocation_evidence(
        reloaded,
        source_ref="/api/graph-governance/aming-claw/parallel-branches/allocate",
        route_identity={
            "route_id": "route-contract",
            "route_context_hash": "sha256:route",
            "prompt_contract_id": "rprompt-contract",
            "prompt_contract_hash": "sha256:prompt",
            "route_token_ref": "rtok-contract",
            "visible_injection_manifest_hash": "sha256:visible",
            "raw_private_context": "do not expose",
        },
    )

    assert evidence["target_project_root"] == expected_root
    assert evidence["target_files"] == list(owned_files)
    assert evidence["owned_files"] == list(owned_files)
    assert evidence["context"]["target_project_root"] == expected_root
    assert evidence["context"]["target_files"] == list(owned_files)
    assert evidence["route_id"] == "route-contract"
    assert evidence["route_context_hash"] == "sha256:route"
    assert evidence["prompt_contract_id"] == "rprompt-contract"
    assert evidence["prompt_contract_hash"] == "sha256:prompt"
    assert evidence["route_token_ref"] == "rtok-contract"
    assert evidence["visible_injection_manifest_hash"] == "sha256:visible"
    assert "raw_private_context" not in evidence["route_identity"]


def test_mf_branch_allocation_projection_preserves_explicit_parent_and_file_scope() -> None:
    conn = _runtime_conn()
    owned_files = (
        "agent/governance/parallel_branch_runtime.py",
        "agent/observer_runtime.py",
    )
    context = plan_branch_runtime_context(
        project_id=PROJECT_ID,
        task_id="MF Hotfix Parity",
        batch_id="PB-HOTFIX",
        backlog_id="AC-ROUTE-GATE-FIXTURE-PARITY-20260531",
        parent_task_id="cex-hotfix-successor",
        chain_id="cex-onboard-root",
        root_task_id="cex-onboard-root",
        stage_task_id="MF Hotfix Parity",
        agent_id="observer",
        worker_id="worker-hotfix",
        workspace_root="/repo",
        target_project_root="/repo/.worktrees/mf-hotfix-parity",
        target_files=owned_files,
        owned_files=owned_files,
        base_commit="base-hotfix",
        target_head_commit="target-hotfix",
        merge_queue_id="mq-hotfix",
        fence_token="fence-hotfix",
    )

    saved = upsert_branch_context(conn, context, now_iso=NOW)
    reloaded = get_branch_context(conn, PROJECT_ID, "MF Hotfix Parity")

    assert reloaded is not None
    assert saved.parent_task_id == "cex-hotfix-successor"
    assert reloaded.parent_task_id == "cex-hotfix-successor"
    assert reloaded.root_task_id == "cex-onboard-root"
    assert reloaded.chain_id == "cex-onboard-root"
    assert reloaded.owned_files == owned_files
    assert reloaded.target_files == owned_files

    payload = branch_context_to_dict(reloaded)
    assert payload["parent_task_id"] == "cex-hotfix-successor"
    assert payload["owned_files"] == list(owned_files)
    assert payload["target_files"] == list(owned_files)

    revision = append_branch_contract_revision(
        conn,
        reloaded,
        payload={
            "observer_command_id": "cmd-hotfix",
            "owned_files": list(owned_files),
            "target_files": list(owned_files),
        },
        now_iso=NOW,
    )
    projection = build_runtime_context_projection(
        reloaded,
        contract_revision=revision,
        generated_at=NOW,
    ).to_dict()
    current = projection["views"]["current"]
    worker = projection["views"]["worker_view"]

    assert current["identity"]["parent_task_id"] == "cex-hotfix-successor"
    assert current["graph_query_identity"]["parent_task_id"] == "cex-hotfix-successor"
    assert current["work"]["owned_files"] == list(owned_files)
    assert current["work"]["target_files"] == list(owned_files)
    assert worker["task"]["parent_task_id"] == "cex-hotfix-successor"
    assert worker["owned_files"] == list(owned_files)
    assert worker["target_files"] == list(owned_files)


def test_parallel_branch_allocate_handler_persists_requested_file_scope(monkeypatch) -> None:
    from agent.governance import server as server_module

    conn = _runtime_conn()
    owned_files = [
        "agent/governance/server.py",
        "agent/governance/parallel_branch_runtime.py",
    ]
    target_files = [
        "agent/governance/server.py",
        "agent/governance/parallel_branch_runtime.py",
        "agent/observer_runtime.py",
    ]

    class NoCloseConnection:
        def __init__(self, wrapped: sqlite3.Connection):
            self._wrapped = wrapped

        def close(self) -> None:
            pass

        def __getattr__(self, name: str):
            return getattr(self._wrapped, name)

    monkeypatch.setattr(
        server_module,
        "get_connection",
        lambda project_id: NoCloseConnection(conn),
    )
    monkeypatch.setattr(
        server_module,
        "_require_graph_governance_operator",
        lambda ctx, conn, action: None,
    )
    monkeypatch.setattr(
        server_module,
        "_record_bounded_worker_dispatch_event",
        lambda *args, **kwargs: {"ok": True, "event_id": "evt-dispatch"},
    )

    ctx = server_module.RequestContext(
        handler=None,
        method="POST",
        path_params={"project_id": PROJECT_ID},
        query={},
        body={
            "task_id": "mf-handler-scope",
            "workspace_root": "/repo",
            "backlog_id": "AC-ROUTE-GATE-FIXTURE-PARITY-20260531",
            "parent_task_id": "cex-hotfix-successor",
            "root_task_id": "cex-onboard-root",
            "stage_task_id": "mf-handler-scope",
            "worker_id": "worker-handler-scope",
            "agent_id": "observer",
            "owned_files": owned_files,
            "target_files": target_files,
            "base_commit": "base-handler",
            "target_head_commit": "target-handler",
            "merge_queue_id": "mq-handler",
            "fence_token": "fence-handler",
            "create_worktree": False,
            "issue_same_owner_session_token": False,
            "now_iso": NOW,
        },
        request_id="req-handler-scope",
        token="",
        idem_key="",
    )

    status, response = server_module.handle_graph_governance_parallel_branch_allocate(ctx)

    assert status == 201
    assert response["context"]["parent_task_id"] == "cex-hotfix-successor"
    assert response["context"]["owned_files"] == owned_files
    assert response["context"]["target_files"] == target_files
    assert response["branch_runtime_evidence"]["owned_files"] == owned_files
    assert response["branch_runtime_evidence"]["target_files"] == target_files

    persisted = get_branch_context(conn, PROJECT_ID, "mf-handler-scope")
    assert persisted is not None
    assert persisted.parent_task_id == "cex-hotfix-successor"
    assert persisted.owned_files == tuple(owned_files)
    assert persisted.target_files == tuple(target_files)


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


def test_merge_queue_accepts_route_gated_finish_checkpoint_without_raw_fence() -> None:
    conn = _runtime_conn()
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T-route-checkpoint",
            batch_id="PB-002",
            branch_ref="refs/heads/codex/t-route-checkpoint",
            status=STATE_VALIDATED,
            fence_token="fence-route-checkpoint",
            base_commit="base-route-checkpoint",
            head_commit="head-route-checkpoint",
            target_head_commit="target-route-checkpoint",
            checkpoint_id="ckpt-route-checkpoint",
            replay_source="mf_sub_finish_gate",
        ),
        now_iso=NOW,
    )

    with pytest.raises(BranchRuntimeFenceError):
        queue_merge_item_for_branch_context(
            conn,
            project_id=PROJECT_ID,
            task_id="T-route-checkpoint",
            merge_queue_id="mergeq-route-checkpoint",
            require_finish_gate=True,
            checkpoint_id="ckpt-route-checkpoint",
            now_iso=NOW,
        )

    with pytest.raises(ValueError, match="checkpoint_id does not match"):
        queue_merge_item_for_branch_context(
            conn,
            project_id=PROJECT_ID,
            task_id="T-route-checkpoint",
            merge_queue_id="mergeq-route-checkpoint",
            require_finish_gate=True,
            checkpoint_id="ckpt-wrong",
            allow_finish_checkpoint_without_fence=True,
            now_iso=NOW,
        )

    queued = queue_merge_item_for_branch_context(
        conn,
        project_id=PROJECT_ID,
        task_id="T-route-checkpoint",
        merge_queue_id="mergeq-route-checkpoint",
        require_finish_gate=True,
        checkpoint_id="ckpt-route-checkpoint",
        allow_finish_checkpoint_without_fence=True,
        now_iso=NOW,
    )

    assert queued["context"]["status"] == STATE_VALIDATED
    assert queued["queue_item"]["status"] == "queued_for_merge"
    assert queued["context"]["checkpoint_id"] == "ckpt-route-checkpoint"
    assert queued["queue_item"]["branch_head"] == "head-route-checkpoint"


def test_merge_queue_materialize_repairs_legacy_queued_finish_context() -> None:
    conn = _runtime_conn()
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T-route-legacy-queued",
            batch_id="PB-002",
            branch_ref="refs/heads/codex/t-route-legacy-queued",
            status="queued",
            fence_token="fence-route-legacy-queued",
            base_commit="base-route-legacy-queued",
            head_commit="head-route-legacy-queued",
            target_head_commit="target-route-legacy-queued",
            checkpoint_id="ckpt-route-legacy-queued",
            replay_source="mf_sub_finish_gate",
            merge_queue_id="mergeq-route-legacy-queued",
        ),
        now_iso=NOW,
    )

    queued = queue_merge_item_for_branch_context(
        conn,
        project_id=PROJECT_ID,
        task_id="T-route-legacy-queued",
        merge_queue_id="mergeq-route-legacy-queued",
        status="queued",
        require_finish_gate=True,
        checkpoint_id="ckpt-route-legacy-queued",
        allow_finish_checkpoint_without_fence=True,
        now_iso=NOW,
    )
    repaired = queue_merge_item_for_branch_context(
        conn,
        project_id=PROJECT_ID,
        task_id="T-route-legacy-queued",
        merge_queue_id="mergeq-route-legacy-queued",
        require_finish_gate=True,
        checkpoint_id="ckpt-route-legacy-queued",
        allow_finish_checkpoint_without_fence=True,
        now_iso=NOW,
    )

    assert queued["context"]["status"] == STATE_VALIDATED
    assert queued["queue_item"]["status"] == "queued_for_merge"
    assert repaired["context"]["status"] == STATE_VALIDATED
    assert repaired["queue_item"]["status"] == "queued_for_merge"


def _finished_qa_runtime_projection(
    context: BranchTaskRuntimeContext,
    *,
    durable_merge_queue_item: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return build_runtime_context_projection(
        context,
        route_identity={
            "route_id": f"route-{context.task_id}",
            "route_context_hash": "sha256:route-finish-startup",
            "prompt_contract_id": f"rprompt-{context.task_id}",
            "prompt_contract_hash": "sha256:prompt-finish-startup",
            "route_token_ref": f"rtok-{context.task_id}",
            "visible_injection_manifest_hash": "sha256:visible-finish-startup",
        },
        target_files=["agent/governance/parallel_branch_runtime.py"],
        graph_trace_refs={"trace_ids": [f"gqt-{context.task_id}"]},
        timeline_refs={
            "startup_event_ref": f"timeline:startup-{context.task_id}",
            "read_receipt_event_ref": f"timeline:read-{context.task_id}",
            "route_action_precheck_event_ref": f"timeline:route-{context.task_id}",
            "implementation_event_refs": [f"timeline:implementation-{context.task_id}"],
            "finish_event_ref": f"timeline:finish-{context.task_id}",
            "verification_event_refs": [f"timeline:qa-{context.task_id}"],
            "close_ready_event_ref": f"timeline:close-{context.task_id}",
        },
        startup_gate=_finish_startup_gate(context),
        finish_gate={
            "event_id": f"timeline:finish-{context.task_id}",
            "checkpoint_id": context.checkpoint_id,
            "worker_self_attestation": {
                "status": "passed",
                "worker_self_attesting": True,
                "finish_time_self_attesting": True,
                "attestation_phase": "finish",
            },
            "worker_self_attestation_gate": {
                "status": "passed",
                "passed": True,
            },
        },
        close_evidence={"event_id": f"timeline:close-{context.task_id}"},
        durable_merge_queue_item=durable_merge_queue_item or {},
        generated_at=NOW,
    ).to_dict()


def test_runtime_context_projects_validated_missing_durable_merge_queue_item() -> None:
    context = _runtime_projection_context(
        task_id="mf-sub-durable-missing",
        branch_ref="refs/heads/codex/mf-sub-durable-missing",
        status=STATE_VALIDATED,
        checkpoint_id="ckpt-durable-missing",
        replay_source="mf_sub_finish_gate",
        merge_queue_id="mq-durable-missing",
    )

    projection = _finished_qa_runtime_projection(context)

    current_values = projection["views"]["current"]["current_values"]
    durable_projection = current_values["durable_merge_queue_item_projection"]
    action_plan = projection["views"]["action_plan"]
    done_state = action_plan["close_precheck_gap_projection"][
        "done_state_projection"
    ]
    bootstrap = durable_projection["copy_safe_bootstrap_payload"]
    tool_args = bootstrap["tool_args"]

    assert durable_projection["status"] == (
        "validated_without_durable_merge_queue_item"
    )
    assert durable_projection["durable_queue_item_present"] is False
    assert action_plan["next_legal_action"] == "parallel_branch_merge_queue_materialize"
    assert done_state["status"] == "validated_without_durable_merge_queue_item"
    assert done_state["handoff_terminal_status"] == (
        "validated_without_durable_merge_queue_item"
    )
    assert tool_args == {
        "project_id": PROJECT_ID,
        "task_id": "mf-sub-durable-missing",
        "backlog_id": "BUG-RUNTIME-CONTEXT",
        "merge_queue_id": "mq-durable-missing",
        "queue_item_id": "mq-durable-missing:mf-sub-durable-missing",
        "target_ref": "refs/heads/main",
        "current_target_head": "target-runtime-context",
        "checkpoint_id": "ckpt-durable-missing",
        "require_finish_gate": True,
        "worker_role": "mf_sub",
        "status": "queued_for_merge",
        "route_token_ref": "rtok-mf-sub-durable-missing",
    }
    assert "fence_token" not in tool_args
    assert context.fence_token not in json.dumps(bootstrap)


def test_runtime_context_projects_materialized_durable_merge_queue_item() -> None:
    conn = _runtime_conn()
    context = _runtime_projection_context(
        task_id="mf-sub-durable-present",
        branch_ref="refs/heads/codex/mf-sub-durable-present",
        status=STATE_VALIDATED,
        checkpoint_id="ckpt-durable-present",
        replay_source="mf_sub_finish_gate",
        merge_queue_id="mq-durable-present",
    )
    upsert_branch_context(conn, context, now_iso=NOW)

    queue_merge_item_for_branch_context(
        conn,
        project_id=PROJECT_ID,
        task_id="mf-sub-durable-present",
        merge_queue_id="mq-durable-present",
        require_finish_gate=True,
        checkpoint_id="ckpt-durable-present",
        allow_finish_checkpoint_without_fence=True,
        now_iso=NOW,
    )
    durable_item = get_merge_queue_item_for_branch_context(
        conn,
        PROJECT_ID,
        "mf-sub-durable-present",
        merge_queue_id="mq-durable-present",
    )
    assert durable_item is not None
    saved_context = get_branch_context(conn, PROJECT_ID, "mf-sub-durable-present")
    assert saved_context is not None

    queued_item = {
        "project_id": durable_item.project_id,
        "merge_queue_id": durable_item.merge_queue_id,
        "queue_item_id": durable_item.queue_item_id,
        "task_id": durable_item.task_id,
        "branch_ref": durable_item.branch_ref,
        "status": durable_item.status,
        "target_ref": durable_item.target_ref,
        "current_target_head": durable_item.current_target_head,
        "merge_preview_id": durable_item.merge_preview_id,
    }
    projection = _finished_qa_runtime_projection(
        saved_context,
        durable_merge_queue_item=queued_item,
    )

    durable_projection = projection["views"]["current"]["current_values"][
        "durable_merge_queue_item_projection"
    ]
    action_plan = projection["views"]["action_plan"]

    assert queued_item["queue_item_id"] == (
        "mq-durable-present:mf-sub-durable-present"
    )
    assert durable_projection["durable_queue_item_present"] is True
    assert durable_projection["status"] == "queued_for_merge"
    assert durable_projection["queue_item_id"] == (
        "mq-durable-present:mf-sub-durable-present"
    )
    assert durable_projection["materialization_status"] == (
        "durable_merge_queue_item_materialized"
    )
    assert action_plan["next_legal_action"] == "handoff_review_ready"


def test_merge_queue_route_gated_finish_checkpoint_rejects_non_finish_source() -> None:
    conn = _runtime_conn()
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T-route-non-finish",
            batch_id="PB-002",
            branch_ref="refs/heads/codex/t-route-non-finish",
            status=STATE_VALIDATED,
            fence_token="fence-route-non-finish",
            base_commit="base-route-non-finish",
            head_commit="head-route-non-finish",
            target_head_commit="target-route-non-finish",
            checkpoint_id="ckpt-route-non-finish",
            replay_source="manual_checkpoint",
        ),
        now_iso=NOW,
    )

    with pytest.raises(ValueError, match="validated mf_sub finish gate"):
        queue_merge_item_for_branch_context(
            conn,
            project_id=PROJECT_ID,
            task_id="T-route-non-finish",
            merge_queue_id="mergeq-route-non-finish",
            require_finish_gate=True,
            checkpoint_id="ckpt-route-non-finish",
            allow_finish_checkpoint_without_fence=True,
            now_iso=NOW,
        )


def test_merge_queue_route_gated_finish_checkpoint_rejects_non_ready_context() -> None:
    conn = _runtime_conn()
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            task_id="T-route-running",
            batch_id="PB-002",
            branch_ref="refs/heads/codex/t-route-running",
            status=STATE_RUNNING,
            fence_token="fence-route-running",
            base_commit="base-route-running",
            head_commit="head-route-running",
            target_head_commit="target-route-running",
            checkpoint_id="ckpt-route-running",
            replay_source="mf_sub_finish_gate",
        ),
        now_iso=NOW,
    )

    with pytest.raises(ValueError, match="not merge-ready"):
        queue_merge_item_for_branch_context(
            conn,
            project_id=PROJECT_ID,
            task_id="T-route-running",
            merge_queue_id="mergeq-route-running",
            require_finish_gate=True,
            checkpoint_id="ckpt-route-running",
            allow_finish_checkpoint_without_fence=True,
            now_iso=NOW,
        )


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
        r"^\s+(parent_task_id|merge_queue_id|merge_preview_id)\s+TEXT NOT NULL DEFAULT ''"
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
    assert "parent_task_id" in post_cols, (
        "ensure_branch_runtime_schema did not add parent_task_id column to old table"
    )

    # Existing row must be readable with merge_queue_id defaulting to empty string
    old_ctx = get_branch_context(conn_old, "proj-old", "task-old")
    assert old_ctx is not None
    assert old_ctx.merge_queue_id == "", (
        f"old row merge_queue_id should default to empty, got {old_ctx.merge_queue_id!r}"
    )
    assert old_ctx.parent_task_id == "", (
        f"old row parent_task_id should default to empty, got {old_ctx.parent_task_id!r}"
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
