"""Helpers for parallel branch runtime persistence and recovery decisions.

The recovery oracle remains side-effect free; the store helpers persist only
runtime context needed to make observer recovery replay-ready after restart.
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
import subprocess
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


PARALLEL_BRANCH_RUNTIME_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS parallel_branch_runtime_contexts (
    project_id        TEXT NOT NULL,
    task_id           TEXT NOT NULL,
    runtime_context_id TEXT NOT NULL DEFAULT '',
    batch_id          TEXT NOT NULL DEFAULT '',
    backlog_id        TEXT NOT NULL DEFAULT '',
    chain_id          TEXT NOT NULL DEFAULT '',
    root_task_id      TEXT NOT NULL DEFAULT '',
    stage_task_id     TEXT NOT NULL DEFAULT '',
    stage_type        TEXT NOT NULL DEFAULT '',
    retry_round       INTEGER NOT NULL DEFAULT 0,
    agent_id          TEXT NOT NULL DEFAULT '',
    worker_id         TEXT NOT NULL DEFAULT '',
    attempt           INTEGER NOT NULL DEFAULT 1,
    lease_id          TEXT NOT NULL DEFAULT '',
    lease_expires_at  TEXT NOT NULL DEFAULT '',
    fence_token       TEXT NOT NULL DEFAULT '',
    branch_ref        TEXT NOT NULL DEFAULT '',
    ref_name          TEXT NOT NULL DEFAULT '',
    worktree_id       TEXT NOT NULL DEFAULT '',
    worktree_path     TEXT NOT NULL DEFAULT '',
    base_commit       TEXT NOT NULL DEFAULT '',
    head_commit       TEXT NOT NULL DEFAULT '',
    target_head_commit TEXT NOT NULL DEFAULT '',
    snapshot_id       TEXT NOT NULL DEFAULT '',
    projection_id     TEXT NOT NULL DEFAULT '',
    merge_queue_id    TEXT NOT NULL DEFAULT '',
    merge_preview_id  TEXT NOT NULL DEFAULT '',
    rollback_epoch    TEXT NOT NULL DEFAULT '',
    replay_epoch      TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT '',
    depends_on_json   TEXT NOT NULL DEFAULT '[]',
    checkpoint_id     TEXT NOT NULL DEFAULT '',
    replay_source     TEXT NOT NULL DEFAULT '',
    last_recovery_action TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (project_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_parallel_branch_runtime_project_status
  ON parallel_branch_runtime_contexts(project_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_parallel_branch_runtime_project_batch
  ON parallel_branch_runtime_contexts(project_id, batch_id, status);
CREATE INDEX IF NOT EXISTS idx_parallel_branch_runtime_project_branch
  ON parallel_branch_runtime_contexts(project_id, branch_ref);

CREATE TABLE IF NOT EXISTS parallel_branch_runtime_contract_revisions (
    project_id        TEXT NOT NULL,
    runtime_context_id TEXT NOT NULL,
    revision_id       TEXT NOT NULL,
    task_id           TEXT NOT NULL DEFAULT '',
    parent_task_id    TEXT NOT NULL DEFAULT '',
    backlog_id        TEXT NOT NULL DEFAULT '',
    contract_version  TEXT NOT NULL DEFAULT '',
    payload_json      TEXT NOT NULL DEFAULT '{}',
    route_identity_json TEXT NOT NULL DEFAULT '{}',
    route_gate_json   TEXT NOT NULL DEFAULT '{}',
    route_evidence_type TEXT NOT NULL DEFAULT '',
    actor             TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    PRIMARY KEY (project_id, runtime_context_id, revision_id)
);
CREATE INDEX IF NOT EXISTS idx_parallel_branch_contract_revisions_context
  ON parallel_branch_runtime_contract_revisions(project_id, runtime_context_id, created_at);

CREATE TABLE IF NOT EXISTS parallel_branch_merge_queue_items (
    project_id        TEXT NOT NULL,
    merge_queue_id    TEXT NOT NULL,
    queue_item_id     TEXT NOT NULL,
    task_id           TEXT NOT NULL,
    branch_ref        TEXT NOT NULL DEFAULT '',
    queue_index       INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT '',
    depends_on_json   TEXT NOT NULL DEFAULT '[]',
    hard_depends_on_json TEXT NOT NULL DEFAULT '[]',
    serializes_after_json TEXT NOT NULL DEFAULT '[]',
    conflicts_with_json TEXT NOT NULL DEFAULT '[]',
    same_node_or_file_conflicts_json TEXT NOT NULL DEFAULT '[]',
    requires_graph_epoch_json TEXT NOT NULL DEFAULT '[]',
    target_ref        TEXT NOT NULL DEFAULT '',
    base_commit       TEXT NOT NULL DEFAULT '',
    branch_head       TEXT NOT NULL DEFAULT '',
    validated_target_head TEXT NOT NULL DEFAULT '',
    current_target_head TEXT NOT NULL DEFAULT '',
    validation_attempt INTEGER NOT NULL DEFAULT 0,
    merge_preview_id  TEXT NOT NULL DEFAULT '',
    snapshot_id       TEXT NOT NULL DEFAULT '',
    projection_id     TEXT NOT NULL DEFAULT '',
    merge_commit      TEXT NOT NULL DEFAULT '',
    target_head_before_merge TEXT NOT NULL DEFAULT '',
    target_head_after_merge TEXT NOT NULL DEFAULT '',
    completed_at      TEXT NOT NULL DEFAULT '',
    failure_reason    TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (project_id, merge_queue_id, queue_item_id)
);
CREATE INDEX IF NOT EXISTS idx_parallel_branch_merge_queue_project_queue
  ON parallel_branch_merge_queue_items(project_id, merge_queue_id, queue_index, queue_item_id);
CREATE INDEX IF NOT EXISTS idx_parallel_branch_merge_queue_project_task
  ON parallel_branch_merge_queue_items(project_id, task_id);
CREATE INDEX IF NOT EXISTS idx_parallel_branch_merge_queue_project_target
  ON parallel_branch_merge_queue_items(project_id, target_ref, merge_queue_id);

CREATE TABLE IF NOT EXISTS parallel_branch_batch_runtimes (
    project_id        TEXT NOT NULL,
    batch_id          TEXT NOT NULL,
    target_ref        TEXT NOT NULL DEFAULT '',
    batch_base_commit TEXT NOT NULL DEFAULT '',
    current_target_head TEXT NOT NULL DEFAULT '',
    batch_status      TEXT NOT NULL DEFAULT '',
    rollback_epoch    TEXT NOT NULL DEFAULT '',
    replay_epoch      TEXT NOT NULL DEFAULT '',
    rollback_target_commit TEXT NOT NULL DEFAULT '',
    rollback_snapshot_id TEXT NOT NULL DEFAULT '',
    rollback_projection_id TEXT NOT NULL DEFAULT '',
    failure_reason    TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (project_id, batch_id)
);
CREATE INDEX IF NOT EXISTS idx_parallel_branch_batch_project_status
  ON parallel_branch_batch_runtimes(project_id, batch_status, updated_at);

CREATE TABLE IF NOT EXISTS parallel_branch_batch_items (
    project_id        TEXT NOT NULL,
    batch_id          TEXT NOT NULL,
    task_id           TEXT NOT NULL,
    branch_ref        TEXT NOT NULL DEFAULT '',
    worktree_path     TEXT NOT NULL DEFAULT '',
    queue_index       INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT '',
    branch_head       TEXT NOT NULL DEFAULT '',
    base_commit       TEXT NOT NULL DEFAULT '',
    checkpoint_id     TEXT NOT NULL DEFAULT '',
    merge_commit      TEXT NOT NULL DEFAULT '',
    target_head_before_merge TEXT NOT NULL DEFAULT '',
    target_head_after_merge TEXT NOT NULL DEFAULT '',
    snapshot_id       TEXT NOT NULL DEFAULT '',
    projection_id     TEXT NOT NULL DEFAULT '',
    merge_queue_id    TEXT NOT NULL DEFAULT '',
    merge_preview_id  TEXT NOT NULL DEFAULT '',
    depends_on_json   TEXT NOT NULL DEFAULT '[]',
    retained          INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (project_id, batch_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_parallel_branch_batch_items_project_batch
  ON parallel_branch_batch_items(project_id, batch_id, queue_index, task_id);
CREATE INDEX IF NOT EXISTS idx_parallel_branch_batch_items_project_branch
  ON parallel_branch_batch_items(project_id, branch_ref);
"""

STATE_MERGED = "merged"
STATE_MERGE_FAILED = "merge_failed"
STATE_QUEUED_FOR_MERGE = "queued_for_merge"
STATE_RUNNING = "running"
STATE_RECLAIMABLE = "reclaimable"
STATE_DEPENDENCY_BLOCKED = "dependency_blocked"
STATE_WAITING_DEPENDENCY = "waiting_dependency"
STATE_VALIDATED = "validated"
STATE_VALIDATING = "validating"
STATE_STALE_AFTER_DEPENDENCY_MERGE = "stale_after_dependency_merge"
STATE_REBASE_REQUIRED = "rebase_required"
STATE_MERGE_READY = "merge_ready"
STATE_MERGE_BLOCKED = "merge_blocked"
STATE_MERGING = "merging"
STATE_ABANDONED = "abandoned"
STATE_ROLLBACK_REQUIRED = "rollback_required"
STATE_ALLOCATED = "allocated"
STATE_WORKTREE_READY = "worktree_ready"
MATERIALIZED_RUNTIME_CONTEXT_STATES = {
    STATE_WORKTREE_READY,
    STATE_RUNNING,
    STATE_RECLAIMABLE,
    STATE_DEPENDENCY_BLOCKED,
    STATE_WAITING_DEPENDENCY,
    STATE_VALIDATED,
    STATE_VALIDATING,
    STATE_STALE_AFTER_DEPENDENCY_MERGE,
    STATE_REBASE_REQUIRED,
    STATE_MERGE_READY,
    STATE_MERGE_BLOCKED,
    STATE_MERGING,
    STATE_MERGED,
    STATE_MERGE_FAILED,
    STATE_ROLLBACK_REQUIRED,
}

BATCH_STATE_OPEN = "open"
BATCH_STATE_MERGE_IN_PROGRESS = "merge_in_progress"
BATCH_STATE_ROLLBACK_REQUIRED = "rollback_required"
BATCH_STATE_ROLLBACK_IN_PROGRESS = "rollback_in_progress"
BATCH_STATE_REPLAY_PENDING = "replay_pending"
BATCH_STATE_REPLAY_IN_PROGRESS = "replay_in_progress"
BATCH_STATE_ACCEPTED = "accepted"
BATCH_STATE_ABANDONED = "abandoned"
BATCH_STATE_CLEANED = "cleaned"

ACTION_LEAVE_MERGED = "leave_merged"
ACTION_OBSERVER_DECISION_REQUIRED = "observer_decision_required"
ACTION_RECLAIM_FROM_CHECKPOINT = "reclaim_from_checkpoint"
ACTION_RECLAIM_AFTER_DEPENDENCY = "reclaim_after_dependency"
ACTION_WAIT_FOR_DEPENDENCY = "wait_for_dependency"
ACTION_NOOP = "noop"
ACTION_BLOCKED_BY_DEPENDENCY = "blocked_by_dependency"
ACTION_REVALIDATE_AFTER_DEPENDENCY_MERGE = "revalidate_after_dependency_merge"
ACTION_ALLOW_MERGE = "allow_merge"
ACTION_MERGE_IN_PROGRESS = "merge_in_progress"
ACTION_ROLLBACK_BATCH = "rollback_batch"
ACTION_REPLAY_THROUGH_MERGE_QUEUE = "replay_through_merge_queue"
ACTION_RETAIN_FOR_REPLAY = "retain_for_replay"
ACTION_RETAIN_FOR_AUDIT = "retain_for_audit"
ACTION_CLEANUP_RETAINED_BRANCH = "cleanup_retained_branch"
ACTION_OPERATOR_APPROVE_LIVE_MERGE = "operator_approve_live_merge"

MERGE_GATE_REQUIRED_EVIDENCE = (
    "git_conflict_check",
    "dirty_worktree_check",
    "test_evidence",
    "graph_currentness",
    "scope_reconcile",
    "semantic_projection",
    "backlog_acceptance",
)
MERGE_GATE_PASS_STATUSES = {
    "clean",
    "current",
    "ok",
    "pass",
    "passed",
    "satisfied",
    "waived",
}
MERGE_GATE_DEFERABLE_EVIDENCE = {"semantic_projection"}
MERGE_GATE_DEFERRED_STATUSES = {"deferred", "intentionally_deferred"}

TERMINAL_NON_BLOCKING_STATES = {"merged", "abandoned", "cleaned"}
ACTIVE_MF_SUBAGENT_GRAPH_QUERY_STATES = {
    STATE_ALLOCATED,
    STATE_WORKTREE_READY,
    STATE_RUNNING,
}
MF_SUBAGENT_STARTUP_GATE_SCHEMA_VERSION = "mf_subagent_startup_gate.v1"
MERGE_DONE_STATES = {STATE_MERGED}
MERGE_BLOCKING_STATES = {STATE_MERGE_FAILED, STATE_ABANDONED, STATE_ROLLBACK_REQUIRED}
MERGE_REVALIDATION_BLOCKING_STATES = {
    STATE_RUNNING,
    STATE_DEPENDENCY_BLOCKED,
    STATE_VALIDATING,
    STATE_STALE_AFTER_DEPENDENCY_MERGE,
    STATE_REBASE_REQUIRED,
    STATE_MERGE_BLOCKED,
}
MERGE_READY_INPUT_STATES = {
    STATE_QUEUED_FOR_MERGE,
    STATE_VALIDATED,
    STATE_MERGE_READY,
}
BATCH_CLEANUP_ALLOWED_STATES = {
    BATCH_STATE_ACCEPTED,
    BATCH_STATE_ABANDONED,
    BATCH_STATE_CLEANED,
}
BATCH_ROLLBACK_STATES = {
    BATCH_STATE_ROLLBACK_REQUIRED,
    BATCH_STATE_ROLLBACK_IN_PROGRESS,
    BATCH_STATE_REPLAY_PENDING,
    BATCH_STATE_REPLAY_IN_PROGRESS,
}


class BranchRuntimeFenceError(ValueError):
    """Raised when a stale worker attempts to mutate branch runtime state."""


@dataclass(frozen=True)
class BranchRuntimeTask:
    task_id: str
    branch_ref: str
    status: str
    depends_on: tuple[str, ...] = ()
    lease_expired: bool = False
    checkpoint_id: str = ""
    replay_source: str = ""
    merge_epoch: str = ""


@dataclass(frozen=True)
class BranchTaskRuntimeContext:
    project_id: str
    task_id: str
    branch_ref: str
    status: str
    runtime_context_id: str = ""
    batch_id: str = ""
    backlog_id: str = ""
    chain_id: str = ""
    root_task_id: str = ""
    stage_task_id: str = ""
    stage_type: str = ""
    retry_round: int = 0
    agent_id: str = ""
    worker_id: str = ""
    attempt: int = 1
    lease_id: str = ""
    lease_expires_at: str = ""
    fence_token: str = ""
    ref_name: str = "main"
    worktree_id: str = ""
    worktree_path: str = ""
    base_commit: str = ""
    head_commit: str = ""
    target_head_commit: str = ""
    snapshot_id: str = ""
    projection_id: str = ""
    merge_queue_id: str = ""
    merge_preview_id: str = ""
    rollback_epoch: str = ""
    replay_epoch: str = ""
    depends_on: tuple[str, ...] = ()
    checkpoint_id: str = ""
    replay_source: str = ""
    last_recovery_action: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_runtime_task(self, *, now_iso: str = "") -> BranchRuntimeTask:
        lease_expired = False
        if self.lease_expires_at and now_iso:
            lease_expired = self.lease_expires_at < now_iso
        return BranchRuntimeTask(
            task_id=self.task_id,
            branch_ref=self.branch_ref,
            status=self.status,
            depends_on=self.depends_on,
            lease_expired=lease_expired,
            checkpoint_id=self.checkpoint_id,
            replay_source=self.replay_source,
            merge_epoch=self.merge_queue_id,
        )


@dataclass(frozen=True)
class BranchRuntimeContractRevision:
    project_id: str
    runtime_context_id: str
    revision_id: str
    task_id: str
    parent_task_id: str
    backlog_id: str
    contract_version: str
    payload: dict[str, Any]
    route_identity: dict[str, Any]
    route_gate: dict[str, Any]
    route_evidence_type: str
    actor: str
    created_at: str


@dataclass(frozen=True)
class RecoveryDecision:
    task_id: str
    branch_ref: str
    observed_state: str
    recovery_state: str
    action: str
    dependency_blockers: tuple[str, ...] = ()
    recovery_actions: tuple[str, ...] = ()
    checkpoint_id: str = ""
    replay_source: str = ""
    cleanup_blocker: bool = False
    target_graph_activation_allowed: bool = False
    target_semantic_activation_allowed: bool = False

    def to_dashboard_row(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "branch_ref": self.branch_ref,
            "observed_state": self.observed_state,
            "recovery_state": self.recovery_state,
            "action": self.action,
            "dependency_blockers": list(self.dependency_blockers),
            "recovery_actions": list(self.recovery_actions),
            "checkpoint_id": self.checkpoint_id,
            "replay_source": self.replay_source,
            "cleanup_blocker": self.cleanup_blocker,
            "target_graph_activation_allowed": self.target_graph_activation_allowed,
            "target_semantic_activation_allowed": self.target_semantic_activation_allowed,
        }


@dataclass(frozen=True)
class RecoveryPlan:
    scenario_id: str
    decisions: tuple[RecoveryDecision, ...]
    cleanup_allowed: bool
    retained_branch_refs: tuple[str, ...]
    target_graph_activation_blocked_for: tuple[str, ...]
    target_semantic_activation_blocked_for: tuple[str, ...]
    dashboard_rows: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MergeQueueItem:
    project_id: str
    merge_queue_id: str
    queue_item_id: str
    task_id: str
    branch_ref: str
    queue_index: int
    status: str
    depends_on: tuple[str, ...] = ()
    hard_depends_on: tuple[str, ...] = ()
    serializes_after: tuple[str, ...] = ()
    conflicts_with: tuple[str, ...] = ()
    same_node_or_file_conflicts: tuple[str, ...] = ()
    requires_graph_epoch: tuple[str, ...] = ()
    target_ref: str = "refs/heads/main"
    base_commit: str = ""
    branch_head: str = ""
    validated_target_head: str = ""
    current_target_head: str = ""
    validation_attempt: int = 0
    merge_preview_id: str = ""
    snapshot_id: str = ""
    projection_id: str = ""
    merge_commit: str = ""
    target_head_before_merge: str = ""
    target_head_after_merge: str = ""
    completed_at: str = ""
    failure_reason: str = ""


@dataclass(frozen=True)
class MergeQueueDecision:
    queue_item_id: str
    task_id: str
    branch_ref: str
    observed_status: str
    queue_state: str
    action: str
    dependency_blockers: tuple[str, ...] = ()
    dependency_blocker_types: dict[str, tuple[str, ...]] = field(default_factory=dict)
    stale_target_head: bool = False
    next_actions: tuple[str, ...] = ()
    merge_allowed: bool = False
    target_branch_mutation_allowed: bool = False
    target_graph_activation_allowed: bool = False
    target_semantic_activation_allowed: bool = False
    validation_attempt: int = 0
    merge_preview_id: str = ""

    def to_dashboard_row(self) -> dict[str, Any]:
        return {
            "queue_item_id": self.queue_item_id,
            "task_id": self.task_id,
            "branch_ref": self.branch_ref,
            "observed_status": self.observed_status,
            "queue_state": self.queue_state,
            "action": self.action,
            "dependency_blockers": list(self.dependency_blockers),
            "dependency_blocker_types": {
                key: list(values)
                for key, values in sorted(self.dependency_blocker_types.items())
            },
            "stale_target_head": self.stale_target_head,
            "next_actions": list(self.next_actions),
            "merge_allowed": self.merge_allowed,
            "target_branch_mutation_allowed": self.target_branch_mutation_allowed,
            "target_graph_activation_allowed": self.target_graph_activation_allowed,
            "target_semantic_activation_allowed": self.target_semantic_activation_allowed,
            "validation_attempt": self.validation_attempt,
            "merge_preview_id": self.merge_preview_id,
        }


@dataclass(frozen=True)
class MergeQueuePlan:
    scenario_id: str
    decisions: tuple[MergeQueueDecision, ...]
    mergeable_task_ids: tuple[str, ...]
    blocked_task_ids: tuple[str, ...]
    stale_task_ids: tuple[str, ...]
    target_mutation_blocked_for: tuple[str, ...]
    dashboard_rows: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MergeGatePlan:
    scenario_id: str
    project_id: str
    merge_queue_id: str
    queue_item_id: str
    task_id: str
    branch_ref: str
    target_ref: str
    branch_head: str
    current_target_head: str
    dry_run: bool
    queue_state: str
    queue_action: str
    merge_gate_passed: bool
    merge_allowed: bool
    target_branch_mutation_allowed: bool
    target_graph_activation_allowed: bool
    target_semantic_activation_allowed: bool
    blockers: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    blocker_codes: tuple[str, ...] = ()
    warnings: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    evidence: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    next_actions: tuple[str, ...] = ()
    merge_steps: tuple[str, ...] = ()
    merge_preview_id: str = ""
    snapshot_id: str = ""
    projection_id: str = ""


@dataclass(frozen=True)
class BatchMergeItem:
    task_id: str
    branch_ref: str
    worktree_path: str
    queue_index: int
    status: str
    branch_head: str
    base_commit: str = ""
    checkpoint_id: str = ""
    merge_commit: str = ""
    target_head_before_merge: str = ""
    target_head_after_merge: str = ""
    snapshot_id: str = ""
    projection_id: str = ""
    merge_queue_id: str = ""
    merge_preview_id: str = ""
    depends_on: tuple[str, ...] = ()
    retained: bool = True


@dataclass(frozen=True)
class BatchMergeRuntime:
    project_id: str
    batch_id: str
    target_ref: str
    batch_base_commit: str
    current_target_head: str
    items: tuple[BatchMergeItem, ...]
    batch_status: str = BATCH_STATE_OPEN
    rollback_epoch: str = ""
    replay_epoch: str = ""
    rollback_target_commit: str = ""
    rollback_snapshot_id: str = ""
    rollback_projection_id: str = ""
    failure_reason: str = ""


@dataclass(frozen=True)
class BatchRollbackPlan:
    scenario_id: str
    project_id: str
    batch_id: str
    target_ref: str
    batch_status: str
    rollback_required: bool
    rollback_epoch: str
    replay_epoch: str
    rollback_target_commit: str
    rollback_snapshot_id: str
    rollback_projection_id: str
    abandoned_merge_commits: tuple[str, ...]
    abandoned_snapshot_ids: tuple[str, ...]
    abandoned_projection_ids: tuple[str, ...]
    retained_branch_refs: tuple[str, ...]
    retained_worktree_paths: tuple[str, ...]
    replay_task_ids: tuple[str, ...]
    replay_merge_queue_items: tuple[MergeQueueItem, ...]
    cleanup_allowed: bool
    cleanup_blockers: tuple[str, ...]
    operator_actions: tuple[str, ...]
    dashboard_rows: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ParallelBranchReadModel:
    project_id: str
    batch_id: str
    summary: dict[str, Any]
    branch_lanes: tuple[dict[str, Any], ...]
    merge_queue: dict[str, Any]
    rollback: dict[str, Any]
    total_counts: dict[str, int]
    truncated: dict[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "batch_id": self.batch_id,
            "summary": self.summary,
            "branch_lanes": list(self.branch_lanes),
            "merge_queue": self.merge_queue,
            "rollback": self.rollback,
            "total_counts": self.total_counts,
            "truncated": self.truncated,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def branch_runtime_context_id(project_id: str, task_id: str, attempt: int | str = 1) -> str:
    seed = "\0".join((str(project_id or ""), str(task_id or ""), str(attempt or 1)))
    return "mfrctx-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def runtime_context_id_for_branch_context(context: BranchTaskRuntimeContext) -> str:
    return context.runtime_context_id or branch_runtime_context_id(
        context.project_id,
        context.task_id,
        context.attempt,
    )


def is_materialized_branch_context(context: BranchTaskRuntimeContext | None) -> bool:
    if context is None:
        return False
    return (
        context.status in MATERIALIZED_RUNTIME_CONTEXT_STATES
        or bool(context.worktree_path)
        or bool(context.head_commit)
        or bool(context.checkpoint_id)
        or bool(context.snapshot_id)
        or bool(context.projection_id)
    )


def preserve_materialized_context_for_allocation(
    existing: BranchTaskRuntimeContext | None,
    planned: BranchTaskRuntimeContext,
) -> BranchTaskRuntimeContext:
    """Keep allocation-only binds from erasing a materialized runtime context."""

    if planned.status != STATE_ALLOCATED or not is_materialized_branch_context(existing):
        return planned
    assert existing is not None
    return replace(
        existing,
        runtime_context_id=existing.runtime_context_id or planned.runtime_context_id,
        batch_id=planned.batch_id or existing.batch_id,
        backlog_id=planned.backlog_id or existing.backlog_id,
        chain_id=planned.chain_id or existing.chain_id,
        root_task_id=planned.root_task_id or existing.root_task_id,
        stage_task_id=planned.stage_task_id or existing.stage_task_id,
        stage_type=planned.stage_type or existing.stage_type,
        retry_round=planned.retry_round or existing.retry_round,
        agent_id=existing.agent_id or planned.agent_id,
        worker_id=existing.worker_id or planned.worker_id,
        attempt=existing.attempt or planned.attempt,
        lease_id=existing.lease_id or planned.lease_id,
        lease_expires_at=existing.lease_expires_at or planned.lease_expires_at,
        fence_token=existing.fence_token or planned.fence_token,
        ref_name=existing.ref_name or planned.ref_name,
        merge_queue_id=existing.merge_queue_id or planned.merge_queue_id,
        depends_on=existing.depends_on or planned.depends_on,
        created_at=existing.created_at or planned.created_at,
    )


def ensure_branch_runtime_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(PARALLEL_BRANCH_RUNTIME_SCHEMA_SQL)
    _ensure_branch_runtime_context_columns(conn)
    _ensure_branch_merge_queue_columns(conn)
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_parallel_branch_runtime_project_runtime_context
          ON parallel_branch_runtime_contexts(project_id, runtime_context_id)
        """
    )


def _ensure_branch_runtime_context_columns(conn: sqlite3.Connection) -> None:
    rows = conn.execute("PRAGMA table_info(parallel_branch_runtime_contexts)").fetchall()
    columns = {str(row["name"] if hasattr(row, "keys") else row[1]) for row in rows}
    if "runtime_context_id" not in columns:
        conn.execute(
            "ALTER TABLE parallel_branch_runtime_contexts "
            "ADD COLUMN runtime_context_id TEXT NOT NULL DEFAULT ''"
        )
        columns.add("runtime_context_id")
    if "retry_round" not in columns:
        conn.execute(
            "ALTER TABLE parallel_branch_runtime_contexts "
            "ADD COLUMN retry_round INTEGER NOT NULL DEFAULT 0"
        )
    rows = conn.execute(
        """
        SELECT project_id, task_id, attempt
        FROM parallel_branch_runtime_contexts
        WHERE runtime_context_id = ''
        """
    ).fetchall()
    for row in rows:
        project_id = str(row["project_id"] if hasattr(row, "keys") else row[0])
        task_id = str(row["task_id"] if hasattr(row, "keys") else row[1])
        attempt = int((row["attempt"] if hasattr(row, "keys") else row[2]) or 1)
        conn.execute(
            """
            UPDATE parallel_branch_runtime_contexts
            SET runtime_context_id = ?
            WHERE project_id = ? AND task_id = ?
            """,
            (branch_runtime_context_id(project_id, task_id, attempt), project_id, task_id),
        )


def _ensure_branch_merge_queue_columns(conn: sqlite3.Connection) -> None:
    rows = conn.execute("PRAGMA table_info(parallel_branch_merge_queue_items)").fetchall()
    columns = {str(row["name"] if hasattr(row, "keys") else row[1]) for row in rows}
    for column, ddl in (
        (
            "merge_commit",
            "ALTER TABLE parallel_branch_merge_queue_items "
            "ADD COLUMN merge_commit TEXT NOT NULL DEFAULT ''",
        ),
        (
            "target_head_before_merge",
            "ALTER TABLE parallel_branch_merge_queue_items "
            "ADD COLUMN target_head_before_merge TEXT NOT NULL DEFAULT ''",
        ),
        (
            "target_head_after_merge",
            "ALTER TABLE parallel_branch_merge_queue_items "
            "ADD COLUMN target_head_after_merge TEXT NOT NULL DEFAULT ''",
        ),
        (
            "completed_at",
            "ALTER TABLE parallel_branch_merge_queue_items "
            "ADD COLUMN completed_at TEXT NOT NULL DEFAULT ''",
        ),
        (
            "failure_reason",
            "ALTER TABLE parallel_branch_merge_queue_items "
            "ADD COLUMN failure_reason TEXT NOT NULL DEFAULT ''",
        ),
    ):
        if column not in columns:
            conn.execute(ddl)


def _json_array(values: tuple[str, ...] | list[str]) -> str:
    return json.dumps(list(values or ()), ensure_ascii=False, sort_keys=True)


def _parse_json_array(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed)


def _parse_json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _json_object(value: Mapping[str, Any] | dict[str, Any] | None) -> str:
    payload = dict(value) if isinstance(value, Mapping) else {}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


_PRIVATE_CONTRACT_REVISION_KEYS = {
    "hidden_context",
    "observer_only_context",
    "private_context",
    "private_route_body",
    "raw_context_body",
    "raw_private_context",
    "raw_private_context_body",
    "raw_private_route_body",
    "raw_route_body",
    "unmanifested_prompt_text",
}


_SAFE_CONTRACT_REVISION_ROUTE_KEYS = {
    "action",
    "caller_role",
    "decision",
    "expires_at",
    "prompt_contract_hash",
    "prompt_contract_id",
    "route_context_hash",
    "route_id",
    "route_token_hash",
    "route_token_ref",
    "selected_backlog_id",
    "selected_project",
    "status",
    "visible_injection_manifest_hash",
    "waiver_hash",
    "waiver_type",
}


def _sanitize_public_contract_revision_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            if key_text in _PRIVATE_CONTRACT_REVISION_KEYS:
                continue
            sanitized[key_text] = _sanitize_public_contract_revision_value(child)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_public_contract_revision_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_public_contract_revision_value(item) for item in value]
    return value


def public_contract_revision_payload(value: Any) -> dict[str, Any]:
    sanitized = _sanitize_public_contract_revision_value(value)
    return dict(sanitized) if isinstance(sanitized, Mapping) else {}


def public_contract_revision_route_identity(
    route_gate: Mapping[str, Any] | None = None,
    route_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for source in (route_identity or {}, route_gate or {}):
        if not isinstance(source, Mapping):
            continue
        for key in _SAFE_CONTRACT_REVISION_ROUTE_KEYS:
            value = source.get(key)
            if value:
                safe[key] = value
    gate_scope = (route_gate or {}).get("scope") if isinstance(route_gate, Mapping) else None
    if isinstance(gate_scope, Mapping):
        safe["scope"] = {
            key: str(gate_scope.get(key) or "")
            for key in ("project_id", "backlog_id", "task_id")
            if gate_scope.get(key)
        }
    safe["raw_private_context_exposed"] = False
    return safe


def _canonical_contract_hash(value: Any) -> str:
    try:
        body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except TypeError:
        body = json.dumps(repr(value), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _first_public_string(source: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value not in (None, "", [], {}):
            text = str(value).strip()
            if text:
                return text
    return ""


def _nested_public_string(source: Mapping[str, Any], key: str, *names: str) -> str:
    child = source.get(key)
    if not isinstance(child, Mapping):
        return ""
    return _first_public_string(child, *names)


def _previous_revision_hash(revision: "BranchRuntimeContractRevision | None") -> str:
    if revision is None:
        return ""
    payload = revision.payload if isinstance(revision.payload, Mapping) else {}
    receipt = payload.get("revision_receipt")
    if isinstance(receipt, Mapping):
        existing = _first_public_string(receipt, "canonical_visible_contract_text_hash")
        if existing:
            return existing
    return _canonical_contract_hash(
        {
            "payload": payload,
            "route_identity": revision.route_identity,
            "contract_version": revision.contract_version,
        }
    )


def _context_from_row(row: sqlite3.Row) -> BranchTaskRuntimeContext:
    runtime_context_id = ""
    if "runtime_context_id" in row.keys():
        runtime_context_id = row["runtime_context_id"] or ""
    return BranchTaskRuntimeContext(
        project_id=row["project_id"],
        task_id=row["task_id"],
        runtime_context_id=runtime_context_id
        or branch_runtime_context_id(row["project_id"], row["task_id"], int(row["attempt"] or 1)),
        batch_id=row["batch_id"] or "",
        backlog_id=row["backlog_id"] or "",
        chain_id=row["chain_id"] or "",
        root_task_id=row["root_task_id"] or "",
        stage_task_id=row["stage_task_id"] or "",
        stage_type=row["stage_type"] or "",
        retry_round=int(row["retry_round"] or 0),
        agent_id=row["agent_id"] or "",
        worker_id=row["worker_id"] or "",
        attempt=int(row["attempt"] or 1),
        lease_id=row["lease_id"] or "",
        lease_expires_at=row["lease_expires_at"] or "",
        fence_token=row["fence_token"] or "",
        branch_ref=row["branch_ref"] or "",
        ref_name=row["ref_name"] or "",
        worktree_id=row["worktree_id"] or "",
        worktree_path=row["worktree_path"] or "",
        base_commit=row["base_commit"] or "",
        head_commit=row["head_commit"] or "",
        target_head_commit=row["target_head_commit"] or "",
        snapshot_id=row["snapshot_id"] or "",
        projection_id=row["projection_id"] or "",
        merge_queue_id=row["merge_queue_id"] or "",
        merge_preview_id=row["merge_preview_id"] or "",
        rollback_epoch=row["rollback_epoch"] or "",
        replay_epoch=row["replay_epoch"] or "",
        status=row["status"] or "",
        depends_on=_parse_json_array(row["depends_on_json"]),
        checkpoint_id=row["checkpoint_id"] or "",
        replay_source=row["replay_source"] or "",
        last_recovery_action=row["last_recovery_action"] or "",
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
    )


def branch_context_to_dict(context: BranchTaskRuntimeContext) -> dict[str, Any]:
    payload = asdict(context)
    payload["runtime_context_id"] = runtime_context_id_for_branch_context(context)
    payload["depends_on"] = list(context.depends_on)
    return payload


def branch_runtime_allocation_evidence(
    context: BranchTaskRuntimeContext,
    *,
    source_ref: str,
    registration_source: str = "parallel_branch_allocate",
) -> dict[str, Any]:
    """Return machine-consumable branch runtime allocation evidence."""
    runtime_context_id = runtime_context_id_for_branch_context(context)
    return {
        "schema_version": "mf_subagent_branch_runtime.v1",
        "status": context.status or STATE_ALLOCATED,
        "ok": True,
        "present": True,
        "registered": True,
        "allocation_required": False,
        "source_ref": source_ref,
        "registration_ref": source_ref,
        "allocation_source_ref": source_ref,
        "registration_source": registration_source,
        "runtime_context_id": runtime_context_id,
        "context": branch_context_to_dict(context),
    }


def branch_contract_revision_to_dict(
    revision: BranchRuntimeContractRevision,
) -> dict[str, Any]:
    payload = asdict(revision)
    payload["payload"] = dict(revision.payload)
    payload["route_identity"] = dict(revision.route_identity)
    payload["route_gate"] = dict(revision.route_gate)
    return payload


def merge_queue_item_to_dict(item: MergeQueueItem) -> dict[str, Any]:
    payload = asdict(item)
    for key in (
        "depends_on",
        "hard_depends_on",
        "serializes_after",
        "conflicts_with",
        "same_node_or_file_conflicts",
        "requires_graph_epoch",
    ):
        payload[key] = list(getattr(item, key))
    return payload


def batch_merge_item_to_dict(item: BatchMergeItem) -> dict[str, Any]:
    payload = asdict(item)
    payload["depends_on"] = list(item.depends_on)
    return payload


def batch_merge_runtime_to_dict(runtime: BatchMergeRuntime) -> dict[str, Any]:
    payload = asdict(runtime)
    payload["items"] = [batch_merge_item_to_dict(item) for item in runtime.items]
    return payload


def batch_rollback_plan_to_dict(plan: BatchRollbackPlan) -> dict[str, Any]:
    payload = asdict(plan)
    payload["abandoned_merge_commits"] = list(plan.abandoned_merge_commits)
    payload["abandoned_snapshot_ids"] = list(plan.abandoned_snapshot_ids)
    payload["abandoned_projection_ids"] = list(plan.abandoned_projection_ids)
    payload["retained_branch_refs"] = list(plan.retained_branch_refs)
    payload["retained_worktree_paths"] = list(plan.retained_worktree_paths)
    payload["replay_task_ids"] = list(plan.replay_task_ids)
    payload["replay_merge_queue_items"] = [
        merge_queue_item_to_dict(item) for item in plan.replay_merge_queue_items
    ]
    payload["cleanup_blockers"] = list(plan.cleanup_blockers)
    payload["operator_actions"] = list(plan.operator_actions)
    payload["dashboard_rows"] = list(plan.dashboard_rows)
    return payload


def merge_gate_plan_to_dict(plan: MergeGatePlan) -> dict[str, Any]:
    payload = asdict(plan)
    payload["blockers"] = list(plan.blockers)
    payload["blocker_codes"] = list(plan.blocker_codes)
    payload["warnings"] = list(plan.warnings)
    payload["evidence"] = list(plan.evidence)
    payload["next_actions"] = list(plan.next_actions)
    payload["merge_steps"] = list(plan.merge_steps)
    return payload


def upsert_branch_context(
    conn: sqlite3.Connection,
    context: BranchTaskRuntimeContext,
    *,
    now_iso: str = "",
) -> BranchTaskRuntimeContext:
    ensure_branch_runtime_schema(conn)
    now = now_iso or utc_now()
    runtime_context_id = runtime_context_id_for_branch_context(context)
    conn.execute(
        """
        INSERT INTO parallel_branch_runtime_contexts (
            project_id, task_id, runtime_context_id, batch_id, backlog_id, chain_id, root_task_id,
            stage_task_id, stage_type, retry_round, agent_id, worker_id, attempt, lease_id,
            lease_expires_at, fence_token, branch_ref, ref_name, worktree_id,
            worktree_path, base_commit, head_commit, target_head_commit,
            snapshot_id, projection_id, merge_queue_id, merge_preview_id,
            rollback_epoch, replay_epoch, status, depends_on_json,
            checkpoint_id, replay_source, last_recovery_action,
            created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(project_id, task_id) DO UPDATE SET
            runtime_context_id = excluded.runtime_context_id,
            batch_id = excluded.batch_id,
            backlog_id = excluded.backlog_id,
            chain_id = excluded.chain_id,
            root_task_id = excluded.root_task_id,
            stage_task_id = excluded.stage_task_id,
            stage_type = excluded.stage_type,
            retry_round = excluded.retry_round,
            agent_id = excluded.agent_id,
            worker_id = excluded.worker_id,
            attempt = excluded.attempt,
            lease_id = excluded.lease_id,
            lease_expires_at = excluded.lease_expires_at,
            fence_token = excluded.fence_token,
            branch_ref = excluded.branch_ref,
            ref_name = excluded.ref_name,
            worktree_id = excluded.worktree_id,
            worktree_path = excluded.worktree_path,
            base_commit = excluded.base_commit,
            head_commit = excluded.head_commit,
            target_head_commit = excluded.target_head_commit,
            snapshot_id = excluded.snapshot_id,
            projection_id = excluded.projection_id,
            merge_queue_id = excluded.merge_queue_id,
            merge_preview_id = excluded.merge_preview_id,
            rollback_epoch = excluded.rollback_epoch,
            replay_epoch = excluded.replay_epoch,
            status = excluded.status,
            depends_on_json = excluded.depends_on_json,
            checkpoint_id = excluded.checkpoint_id,
            replay_source = excluded.replay_source,
            last_recovery_action = excluded.last_recovery_action,
            updated_at = excluded.updated_at
        """,
        (
            context.project_id,
            context.task_id,
            runtime_context_id,
            context.batch_id,
            context.backlog_id,
            context.chain_id,
            context.root_task_id,
            context.stage_task_id,
            context.stage_type,
            context.retry_round,
            context.agent_id,
            context.worker_id,
            context.attempt,
            context.lease_id,
            context.lease_expires_at,
            context.fence_token,
            context.branch_ref,
            context.ref_name,
            context.worktree_id,
            context.worktree_path,
            context.base_commit,
            context.head_commit,
            context.target_head_commit,
            context.snapshot_id,
            context.projection_id,
            context.merge_queue_id,
            context.merge_preview_id,
            context.rollback_epoch,
            context.replay_epoch,
            context.status,
            _json_array(context.depends_on),
            context.checkpoint_id,
            context.replay_source,
            context.last_recovery_action,
            context.created_at or now,
            now,
        ),
    )
    found = get_branch_context(conn, context.project_id, context.task_id)
    if found is None:
        raise RuntimeError(f"branch runtime context was not persisted: {context.task_id}")
    return found


def get_branch_context(
    conn: sqlite3.Connection,
    project_id: str,
    task_id: str,
) -> BranchTaskRuntimeContext | None:
    ensure_branch_runtime_schema(conn)
    row = conn.execute(
        """
        SELECT * FROM parallel_branch_runtime_contexts
        WHERE project_id = ? AND task_id = ?
        """,
        (project_id, task_id),
    ).fetchone()
    return _context_from_row(row) if row else None


def get_branch_context_by_runtime_context_id(
    conn: sqlite3.Connection,
    project_id: str,
    runtime_context_id: str,
) -> BranchTaskRuntimeContext | None:
    ensure_branch_runtime_schema(conn)
    runtime_id = str(runtime_context_id or "").strip()
    if not runtime_id:
        return None
    row = conn.execute(
        """
        SELECT * FROM parallel_branch_runtime_contexts
        WHERE project_id = ? AND runtime_context_id = ?
        ORDER BY updated_at DESC, task_id
        LIMIT 1
        """,
        (project_id, runtime_id),
    ).fetchone()
    return _context_from_row(row) if row else None


def _contract_revision_from_row(row: sqlite3.Row) -> BranchRuntimeContractRevision:
    return BranchRuntimeContractRevision(
        project_id=row["project_id"],
        runtime_context_id=row["runtime_context_id"],
        revision_id=row["revision_id"],
        task_id=row["task_id"] or "",
        parent_task_id=row["parent_task_id"] or "",
        backlog_id=row["backlog_id"] or "",
        contract_version=row["contract_version"] or "",
        payload=_parse_json_object(row["payload_json"]),
        route_identity=_parse_json_object(row["route_identity_json"]),
        route_gate=_parse_json_object(row["route_gate_json"]),
        route_evidence_type=row["route_evidence_type"] or "",
        actor=row["actor"] or "",
        created_at=row["created_at"] or "",
    )


def _parent_task_id_for_context(context: BranchTaskRuntimeContext) -> str:
    return (
        context.root_task_id
        or context.chain_id
        or context.stage_task_id
        or context.task_id
    )


def append_branch_contract_revision(
    conn: sqlite3.Connection,
    context: BranchTaskRuntimeContext,
    *,
    revision_id: str = "",
    contract_version: str = "mf_parallel.v1",
    payload: Mapping[str, Any] | None = None,
    route_gate: Mapping[str, Any] | None = None,
    route_identity: Mapping[str, Any] | None = None,
    route_evidence_type: str = "",
    actor: str = "",
    now_iso: str = "",
) -> BranchRuntimeContractRevision:
    """Append one runtime contract revision. Existing rows are never updated."""

    ensure_branch_runtime_schema(conn)
    runtime_context_id = runtime_context_id_for_branch_context(context)
    created_at = now_iso or utc_now()
    revision = str(revision_id or "").strip() or f"crev-{uuid.uuid4().hex[:12]}"
    public_payload = public_contract_revision_payload(payload or {})
    public_route_identity = public_contract_revision_route_identity(route_gate, route_identity)
    public_route_gate = public_contract_revision_payload(route_gate or {})
    previous_revision = get_latest_branch_contract_revision(
        conn,
        context.project_id,
        runtime_context_id,
    )
    read_receipt_hash = (
        _first_public_string(public_payload, "read_receipt_hash", "worker_read_receipt_hash")
        or _nested_public_string(public_payload, "read_receipt", "hash", "read_receipt_hash")
        or _nested_public_string(public_payload, "worker_contract", "read_receipt_hash")
        or _nested_public_string(public_payload, "evidence", "read_receipt_hash")
    )
    gate_receipt_hash = (
        _first_public_string(public_payload, "gate_receipt_hash")
        or _nested_public_string(public_payload, "gate_receipt", "hash", "gate_receipt_hash")
        or _nested_public_string(public_payload, "worker_contract", "gate_receipt_hash")
        or _nested_public_string(public_payload, "evidence", "gate_receipt_hash")
        or _first_public_string(public_route_gate, "route_token_hash", "waiver_hash")
    )
    receipt_material = {
        "schema_version": "agent_task_contract_revision_visible_text.v1",
        "project_id": context.project_id,
        "runtime_context_id": runtime_context_id,
        "revision_id": revision,
        "task_id": context.task_id,
        "parent_task_id": _parent_task_id_for_context(context),
        "backlog_id": context.backlog_id,
        "contract_version": str(contract_version or ""),
        "payload": public_payload,
        "route_identity": public_route_identity,
    }
    public_payload["source_of_truth"] = "Contract/Revision/Event"
    public_payload["revision_receipt"] = {
        "schema_version": "agent_task_contract_revision_receipt.v1",
        "source_of_truth": "Contract/Revision/Event",
        "canonical_visible_contract_text_hash": _canonical_contract_hash(receipt_material),
        "previous_revision_hash": _previous_revision_hash(previous_revision),
        "actor_role": str(actor or public_route_gate.get("caller_role") or ""),
        "actor_session_id": str(
            public_payload.get("actor_session_id")
            or public_payload.get("worker_id")
            or public_payload.get("observer_session_id")
            or public_route_gate.get("caller_session_id")
            or ""
        ),
        "timestamp": created_at,
        "read_receipt_hash": read_receipt_hash,
        "gate_receipt_hash": gate_receipt_hash,
    }
    conn.execute(
        """
        INSERT INTO parallel_branch_runtime_contract_revisions (
            project_id, runtime_context_id, revision_id, task_id, parent_task_id,
            backlog_id, contract_version, payload_json, route_identity_json,
            route_gate_json, route_evidence_type, actor, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            context.project_id,
            runtime_context_id,
            revision,
            context.task_id,
            _parent_task_id_for_context(context),
            context.backlog_id,
            str(contract_version or ""),
            _json_object(public_payload),
            _json_object(public_route_identity),
            _json_object(public_route_gate),
            str(route_evidence_type or public_route_gate.get("decision") or ""),
            str(actor or public_route_gate.get("caller_role") or ""),
            created_at,
        ),
    )
    row = conn.execute(
        """
        SELECT * FROM parallel_branch_runtime_contract_revisions
        WHERE project_id = ? AND runtime_context_id = ? AND revision_id = ?
        """,
        (context.project_id, runtime_context_id, revision),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"contract revision was not persisted: {revision}")
    return _contract_revision_from_row(row)


def get_latest_branch_contract_revision(
    conn: sqlite3.Connection,
    project_id: str,
    runtime_context_id: str,
) -> BranchRuntimeContractRevision | None:
    ensure_branch_runtime_schema(conn)
    row = conn.execute(
        """
        SELECT * FROM parallel_branch_runtime_contract_revisions
        WHERE project_id = ? AND runtime_context_id = ?
        ORDER BY created_at DESC, revision_id DESC
        LIMIT 1
        """,
        (project_id, runtime_context_id),
    ).fetchone()
    return _contract_revision_from_row(row) if row else None


def list_branch_contexts(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    batch_id: str = "",
) -> list[BranchTaskRuntimeContext]:
    ensure_branch_runtime_schema(conn)
    if batch_id:
        rows = conn.execute(
            """
            SELECT * FROM parallel_branch_runtime_contexts
            WHERE project_id = ? AND batch_id = ?
            ORDER BY task_id
            """,
            (project_id, batch_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM parallel_branch_runtime_contexts
            WHERE project_id = ?
            ORDER BY task_id
            """,
            (project_id,),
        ).fetchall()
    return [_context_from_row(row) for row in rows]


def _merge_queue_item_from_row(row: sqlite3.Row) -> MergeQueueItem:
    return MergeQueueItem(
        project_id=row["project_id"],
        merge_queue_id=row["merge_queue_id"],
        queue_item_id=row["queue_item_id"],
        task_id=row["task_id"],
        branch_ref=row["branch_ref"] or "",
        queue_index=int(row["queue_index"] or 0),
        status=row["status"] or "",
        depends_on=_parse_json_array(row["depends_on_json"]),
        hard_depends_on=_parse_json_array(row["hard_depends_on_json"]),
        serializes_after=_parse_json_array(row["serializes_after_json"]),
        conflicts_with=_parse_json_array(row["conflicts_with_json"]),
        same_node_or_file_conflicts=_parse_json_array(row["same_node_or_file_conflicts_json"]),
        requires_graph_epoch=_parse_json_array(row["requires_graph_epoch_json"]),
        target_ref=row["target_ref"] or "",
        base_commit=row["base_commit"] or "",
        branch_head=row["branch_head"] or "",
        validated_target_head=row["validated_target_head"] or "",
        current_target_head=row["current_target_head"] or "",
        validation_attempt=int(row["validation_attempt"] or 0),
        merge_preview_id=row["merge_preview_id"] or "",
        snapshot_id=row["snapshot_id"] or "",
        projection_id=row["projection_id"] or "",
        merge_commit=row["merge_commit"] or "",
        target_head_before_merge=row["target_head_before_merge"] or "",
        target_head_after_merge=row["target_head_after_merge"] or "",
        completed_at=row["completed_at"] or "",
        failure_reason=row["failure_reason"] or "",
    )


def upsert_merge_queue_item(
    conn: sqlite3.Connection,
    item: MergeQueueItem,
    *,
    now_iso: str = "",
) -> MergeQueueItem:
    ensure_branch_runtime_schema(conn)
    now = now_iso or utc_now()
    conn.execute(
        """
        INSERT INTO parallel_branch_merge_queue_items (
            project_id, merge_queue_id, queue_item_id, task_id, branch_ref,
            queue_index, status, depends_on_json, hard_depends_on_json,
            serializes_after_json, conflicts_with_json,
            same_node_or_file_conflicts_json, requires_graph_epoch_json,
            target_ref, base_commit, branch_head, validated_target_head,
            current_target_head, validation_attempt, merge_preview_id,
            snapshot_id, projection_id, merge_commit, target_head_before_merge,
            target_head_after_merge, completed_at, failure_reason, created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(project_id, merge_queue_id, queue_item_id) DO UPDATE SET
            task_id = excluded.task_id,
            branch_ref = excluded.branch_ref,
            queue_index = excluded.queue_index,
            status = excluded.status,
            depends_on_json = excluded.depends_on_json,
            hard_depends_on_json = excluded.hard_depends_on_json,
            serializes_after_json = excluded.serializes_after_json,
            conflicts_with_json = excluded.conflicts_with_json,
            same_node_or_file_conflicts_json = excluded.same_node_or_file_conflicts_json,
            requires_graph_epoch_json = excluded.requires_graph_epoch_json,
            target_ref = excluded.target_ref,
            base_commit = excluded.base_commit,
            branch_head = excluded.branch_head,
            validated_target_head = excluded.validated_target_head,
            current_target_head = excluded.current_target_head,
            validation_attempt = excluded.validation_attempt,
            merge_preview_id = excluded.merge_preview_id,
            snapshot_id = excluded.snapshot_id,
            projection_id = excluded.projection_id,
            merge_commit = excluded.merge_commit,
            target_head_before_merge = excluded.target_head_before_merge,
            target_head_after_merge = excluded.target_head_after_merge,
            completed_at = excluded.completed_at,
            failure_reason = excluded.failure_reason,
            updated_at = excluded.updated_at
        """,
        (
            item.project_id,
            item.merge_queue_id,
            item.queue_item_id,
            item.task_id,
            item.branch_ref,
            item.queue_index,
            item.status,
            _json_array(item.depends_on),
            _json_array(item.hard_depends_on),
            _json_array(item.serializes_after),
            _json_array(item.conflicts_with),
            _json_array(item.same_node_or_file_conflicts),
            _json_array(item.requires_graph_epoch),
            item.target_ref,
            item.base_commit,
            item.branch_head,
            item.validated_target_head,
            item.current_target_head,
            item.validation_attempt,
            item.merge_preview_id,
            item.snapshot_id,
            item.projection_id,
            item.merge_commit,
            item.target_head_before_merge,
            item.target_head_after_merge,
            item.completed_at,
            item.failure_reason,
            now,
            now,
        ),
    )
    found = get_merge_queue_item(
        conn,
        item.project_id,
        item.merge_queue_id,
        item.queue_item_id,
    )
    if found is None:
        raise RuntimeError(f"merge queue item was not persisted: {item.queue_item_id}")
    return found


def upsert_merge_queue_items(
    conn: sqlite3.Connection,
    items: list[MergeQueueItem],
    *,
    now_iso: str = "",
) -> list[MergeQueueItem]:
    ensure_branch_runtime_schema(conn)
    if not items:
        return []
    _require_single_merge_queue_scope(items)
    for item in items:
        upsert_merge_queue_item(conn, item, now_iso=now_iso)
    first = items[0]
    return list_merge_queue_items(
        conn,
        first.project_id,
        first.merge_queue_id,
        target_ref=first.target_ref,
    )


def get_merge_queue_item(
    conn: sqlite3.Connection,
    project_id: str,
    merge_queue_id: str,
    queue_item_id: str,
) -> MergeQueueItem | None:
    ensure_branch_runtime_schema(conn)
    row = conn.execute(
        """
        SELECT * FROM parallel_branch_merge_queue_items
        WHERE project_id = ? AND merge_queue_id = ? AND queue_item_id = ?
        """,
        (project_id, merge_queue_id, queue_item_id),
    ).fetchone()
    return _merge_queue_item_from_row(row) if row else None


def list_merge_queue_items(
    conn: sqlite3.Connection,
    project_id: str,
    merge_queue_id: str,
    *,
    target_ref: str = "",
) -> list[MergeQueueItem]:
    ensure_branch_runtime_schema(conn)
    if target_ref:
        rows = conn.execute(
            """
            SELECT * FROM parallel_branch_merge_queue_items
            WHERE project_id = ? AND merge_queue_id = ? AND target_ref = ?
            ORDER BY queue_index, queue_item_id
            """,
            (project_id, merge_queue_id, target_ref),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM parallel_branch_merge_queue_items
            WHERE project_id = ? AND merge_queue_id = ?
            ORDER BY queue_index, queue_item_id
            """,
            (project_id, merge_queue_id),
        ).fetchall()
    return [_merge_queue_item_from_row(row) for row in rows]


def decide_persisted_merge_queue(
    conn: sqlite3.Connection,
    project_id: str,
    merge_queue_id: str,
    *,
    target_ref: str = "",
    current_target_head: str = "",
    scenario_id: str = "PB-002",
) -> MergeQueuePlan:
    """Replay merge queue decisions from durable queue rows."""
    items = list_merge_queue_items(
        conn,
        project_id,
        merge_queue_id,
        target_ref=target_ref,
    )
    latest_target = str(current_target_head or "").strip()
    if latest_target:
        items = [
            replace(item, current_target_head=latest_target)
            if item.status in MERGE_READY_INPUT_STATES
            else item
            for item in items
        ]
    return decide_merge_queue(
        items,
        scenario_id=scenario_id,
    )


def decide_persisted_merge_gate(
    conn: sqlite3.Connection,
    project_id: str,
    merge_queue_id: str,
    *,
    target_ref: str = "",
    queue_item_id: str = "",
    task_id: str = "",
    evidence: dict[str, Any] | None = None,
    batch_id: str = "",
    batch_status: str = "",
    dry_run: bool = True,
    scenario_id: str = "PB-013",
) -> MergeGatePlan:
    """Replay a merge gate plan from durable queue rows without merging git refs."""
    runtime_status = str(batch_status or "").strip()
    if not runtime_status and batch_id:
        runtime = get_batch_merge_runtime(conn, project_id, batch_id)
        if runtime is not None:
            runtime_status = runtime.batch_status
    return decide_merge_gate(
        list_merge_queue_items(
            conn,
            project_id,
            merge_queue_id,
            target_ref=target_ref,
        ),
        queue_item_id=queue_item_id,
        task_id=task_id,
        evidence=evidence,
        batch_status=runtime_status,
        dry_run=dry_run,
        scenario_id=scenario_id,
    )


def record_merge_queue_result(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    merge_queue_id: str,
    queue_item_id: str = "",
    task_id: str = "",
    status: str = "",
    target_ref: str = "",
    merge_commit: str = "",
    target_head_before_merge: str = "",
    target_head_after_merge: str = "",
    failure_reason: str = "",
    snapshot_id: str = "",
    projection_id: str = "",
    fence_token: str = "",
    now_iso: str = "",
) -> dict[str, Any]:
    """Record an externally performed merge attempt without running git."""
    ensure_branch_runtime_schema(conn)
    result_status = str(status or "").strip()
    if result_status not in {STATE_MERGED, STATE_MERGE_FAILED}:
        raise ValueError("merge result status must be merged or merge_failed")
    if result_status == STATE_MERGED and not str(merge_commit or "").strip():
        raise ValueError("merge_commit is required when recording a merged result")
    if result_status == STATE_MERGE_FAILED and not str(failure_reason or "").strip():
        raise ValueError("failure_reason is required when recording merge_failed")

    selected = _select_merge_gate_item(
        list_merge_queue_items(
            conn,
            project_id,
            merge_queue_id,
            target_ref=target_ref,
        ),
        queue_item_id=queue_item_id,
        task_id=task_id,
    )
    context = get_branch_context(conn, project_id, selected.task_id)
    if context is not None and (context.fence_token or fence_token):
        _require_current_fence(context, fence_token)

    now = now_iso or utc_now()
    before = (
        target_head_before_merge
        or selected.target_head_before_merge
        or selected.current_target_head
    )
    after = (
        target_head_after_merge
        or selected.target_head_after_merge
        or selected.current_target_head
    )
    updated_item = replace(
        selected,
        status=result_status,
        current_target_head=after or selected.current_target_head,
        snapshot_id=snapshot_id or selected.snapshot_id,
        projection_id=projection_id or selected.projection_id,
        merge_commit=merge_commit if result_status == STATE_MERGED else selected.merge_commit,
        target_head_before_merge=before,
        target_head_after_merge=after,
        completed_at=now,
        failure_reason=failure_reason if result_status == STATE_MERGE_FAILED else "",
    )
    saved_item = upsert_merge_queue_item(conn, updated_item, now_iso=now)

    saved_context: BranchTaskRuntimeContext | None = None
    if context is not None:
        saved_context = upsert_branch_context(
            conn,
            replace(
                context,
                status=result_status,
                target_head_commit=after or context.target_head_commit,
                snapshot_id=snapshot_id or context.snapshot_id,
                projection_id=projection_id or context.projection_id,
                merge_queue_id=saved_item.merge_queue_id,
                merge_preview_id=saved_item.merge_preview_id,
            ),
            now_iso=now,
        )

    return {
        "queue_item": merge_queue_item_to_dict(saved_item),
        "context": branch_context_to_dict(saved_context) if saved_context is not None else None,
    }


def _batch_merge_item_from_row(row: sqlite3.Row) -> BatchMergeItem:
    return BatchMergeItem(
        task_id=row["task_id"],
        branch_ref=row["branch_ref"] or "",
        worktree_path=row["worktree_path"] or "",
        queue_index=int(row["queue_index"] or 0),
        status=row["status"] or "",
        branch_head=row["branch_head"] or "",
        base_commit=row["base_commit"] or "",
        checkpoint_id=row["checkpoint_id"] or "",
        merge_commit=row["merge_commit"] or "",
        target_head_before_merge=row["target_head_before_merge"] or "",
        target_head_after_merge=row["target_head_after_merge"] or "",
        snapshot_id=row["snapshot_id"] or "",
        projection_id=row["projection_id"] or "",
        merge_queue_id=row["merge_queue_id"] or "",
        merge_preview_id=row["merge_preview_id"] or "",
        depends_on=_parse_json_array(row["depends_on_json"]),
        retained=bool(int(row["retained"] or 0)),
    )


def _batch_runtime_from_row(
    row: sqlite3.Row,
    items: list[BatchMergeItem],
) -> BatchMergeRuntime:
    return BatchMergeRuntime(
        project_id=row["project_id"],
        batch_id=row["batch_id"],
        target_ref=row["target_ref"] or "",
        batch_base_commit=row["batch_base_commit"] or "",
        current_target_head=row["current_target_head"] or "",
        items=tuple(items),
        batch_status=row["batch_status"] or "",
        rollback_epoch=row["rollback_epoch"] or "",
        replay_epoch=row["replay_epoch"] or "",
        rollback_target_commit=row["rollback_target_commit"] or "",
        rollback_snapshot_id=row["rollback_snapshot_id"] or "",
        rollback_projection_id=row["rollback_projection_id"] or "",
        failure_reason=row["failure_reason"] or "",
    )


def upsert_batch_merge_runtime(
    conn: sqlite3.Connection,
    runtime: BatchMergeRuntime,
    *,
    now_iso: str = "",
) -> BatchMergeRuntime:
    ensure_branch_runtime_schema(conn)
    now = now_iso or utc_now()
    conn.execute(
        """
        INSERT INTO parallel_branch_batch_runtimes (
            project_id, batch_id, target_ref, batch_base_commit,
            current_target_head, batch_status, rollback_epoch, replay_epoch,
            rollback_target_commit, rollback_snapshot_id, rollback_projection_id,
            failure_reason, created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(project_id, batch_id) DO UPDATE SET
            target_ref = excluded.target_ref,
            batch_base_commit = excluded.batch_base_commit,
            current_target_head = excluded.current_target_head,
            batch_status = excluded.batch_status,
            rollback_epoch = excluded.rollback_epoch,
            replay_epoch = excluded.replay_epoch,
            rollback_target_commit = excluded.rollback_target_commit,
            rollback_snapshot_id = excluded.rollback_snapshot_id,
            rollback_projection_id = excluded.rollback_projection_id,
            failure_reason = excluded.failure_reason,
            updated_at = excluded.updated_at
        """,
        (
            runtime.project_id,
            runtime.batch_id,
            runtime.target_ref,
            runtime.batch_base_commit,
            runtime.current_target_head,
            runtime.batch_status,
            runtime.rollback_epoch,
            runtime.replay_epoch,
            runtime.rollback_target_commit,
            runtime.rollback_snapshot_id,
            runtime.rollback_projection_id,
            runtime.failure_reason,
            now,
            now,
        ),
    )
    for item in runtime.items:
        conn.execute(
            """
            INSERT INTO parallel_branch_batch_items (
                project_id, batch_id, task_id, branch_ref, worktree_path,
                queue_index, status, branch_head, base_commit, checkpoint_id,
                merge_commit, target_head_before_merge, target_head_after_merge,
                snapshot_id, projection_id, merge_queue_id, merge_preview_id,
                depends_on_json, retained, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(project_id, batch_id, task_id) DO UPDATE SET
                branch_ref = excluded.branch_ref,
                worktree_path = excluded.worktree_path,
                queue_index = excluded.queue_index,
                status = excluded.status,
                branch_head = excluded.branch_head,
                base_commit = excluded.base_commit,
                checkpoint_id = excluded.checkpoint_id,
                merge_commit = excluded.merge_commit,
                target_head_before_merge = excluded.target_head_before_merge,
                target_head_after_merge = excluded.target_head_after_merge,
                snapshot_id = excluded.snapshot_id,
                projection_id = excluded.projection_id,
                merge_queue_id = excluded.merge_queue_id,
                merge_preview_id = excluded.merge_preview_id,
                depends_on_json = excluded.depends_on_json,
                retained = excluded.retained,
                updated_at = excluded.updated_at
            """,
            (
                runtime.project_id,
                runtime.batch_id,
                item.task_id,
                item.branch_ref,
                item.worktree_path,
                item.queue_index,
                item.status,
                item.branch_head,
                item.base_commit,
                item.checkpoint_id,
                item.merge_commit,
                item.target_head_before_merge,
                item.target_head_after_merge,
                item.snapshot_id,
                item.projection_id,
                item.merge_queue_id,
                item.merge_preview_id,
                _json_array(item.depends_on),
                1 if item.retained else 0,
                now,
                now,
            ),
        )

    task_ids = [item.task_id for item in runtime.items]
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        conn.execute(
            f"""
            DELETE FROM parallel_branch_batch_items
            WHERE project_id = ? AND batch_id = ? AND task_id NOT IN ({placeholders})
            """,
            (runtime.project_id, runtime.batch_id, *task_ids),
        )
    else:
        conn.execute(
            """
            DELETE FROM parallel_branch_batch_items
            WHERE project_id = ? AND batch_id = ?
            """,
            (runtime.project_id, runtime.batch_id),
        )

    found = get_batch_merge_runtime(conn, runtime.project_id, runtime.batch_id)
    if found is None:
        raise RuntimeError(f"batch runtime was not persisted: {runtime.batch_id}")
    return found


def list_batch_merge_items(
    conn: sqlite3.Connection,
    project_id: str,
    batch_id: str,
) -> list[BatchMergeItem]:
    ensure_branch_runtime_schema(conn)
    rows = conn.execute(
        """
        SELECT * FROM parallel_branch_batch_items
        WHERE project_id = ? AND batch_id = ?
        ORDER BY queue_index, task_id
        """,
        (project_id, batch_id),
    ).fetchall()
    return [_batch_merge_item_from_row(row) for row in rows]


def get_batch_merge_runtime(
    conn: sqlite3.Connection,
    project_id: str,
    batch_id: str,
) -> BatchMergeRuntime | None:
    ensure_branch_runtime_schema(conn)
    row = conn.execute(
        """
        SELECT * FROM parallel_branch_batch_runtimes
        WHERE project_id = ? AND batch_id = ?
        """,
        (project_id, batch_id),
    ).fetchone()
    if row is None:
        return None
    return _batch_runtime_from_row(
        row,
        list_batch_merge_items(conn, project_id, batch_id),
    )


def decide_persisted_batch_rollback_replay(
    conn: sqlite3.Connection,
    project_id: str,
    batch_id: str,
    *,
    severe_integration_failure: bool = False,
    corrected_replay_order: tuple[str, ...] = (),
    scenario_id: str = "PB-004",
) -> BatchRollbackPlan:
    """Replay batch rollback decisions from durable batch rows."""
    runtime = get_batch_merge_runtime(conn, project_id, batch_id)
    if runtime is None:
        raise KeyError(f"batch runtime not found: {project_id}/{batch_id}")
    return decide_batch_rollback_replay(
        runtime,
        severe_integration_failure=severe_integration_failure,
        corrected_replay_order=corrected_replay_order,
        scenario_id=scenario_id,
    )


def _require_current_fence(context: BranchTaskRuntimeContext, fence_token: str) -> None:
    if context.fence_token and fence_token != context.fence_token:
        raise BranchRuntimeFenceError("Fence token mismatch: branch context was reclaimed")


def validate_mf_subagent_graph_query_identity(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    task_id: str,
    fence_token: str,
    parent_task_id: str = "",
    worker_role: str = "",
) -> BranchTaskRuntimeContext:
    """Validate the task/fence identity an mf_sub worker presents for graph reads."""
    ensure_branch_runtime_schema(conn)
    task = str(task_id or "").strip()
    fence = str(fence_token or "").strip()
    role = str(worker_role or "").strip().lower().replace("-", "_")
    if role != "mf_sub":
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if not task or not fence:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")

    context = get_branch_context(conn, project_id, task)
    if context is None or not context.fence_token:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    try:
        _require_current_fence(context, fence)
    except BranchRuntimeFenceError as exc:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown") from exc

    if context.status not in ACTIVE_MF_SUBAGENT_GRAPH_QUERY_STATES:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if context.lease_expires_at and context.lease_expires_at < utc_now():
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")

    parent = str(parent_task_id or "").strip()
    if parent:
        allowed_parent_ids = {
            value
            for value in (
                context.root_task_id,
                context.chain_id,
                context.stage_task_id,
                context.task_id,
                context.backlog_id,
            )
            if value
        }
        if allowed_parent_ids and parent not in allowed_parent_ids:
            raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    return context


def _startup_string_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item or "").strip())
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    return ()


def _startup_path_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except (OSError, RuntimeError):
        return text


def _startup_path_matches(actual: str, expected: str) -> bool:
    actual_text = _startup_path_text(actual)
    expected_text = _startup_path_text(expected)
    return bool(actual_text and expected_text and actual_text == expected_text)


def _startup_branch_name(value: str) -> str:
    text = str(value or "").strip()
    prefix = "refs/heads/"
    return text[len(prefix):] if text.startswith(prefix) else text


def _startup_token_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    session_token = str(payload.get("session_token") or "").strip()
    surrogate = str(
        payload.get("session_token_surrogate")
        or payload.get("session_surrogate")
        or ""
    ).strip()
    if session_token:
        return {
            "session_token_hash": "sha256:"
            + hashlib.sha256(session_token.encode("utf-8")).hexdigest(),
            "session_token_present": True,
            "session_token_persisted": False,
            "session_token_surrogate": surrogate,
            "session_token_evidence_type": "hash",
        }
    if surrogate:
        return {
            "session_token_hash": "",
            "session_token_present": False,
            "session_token_persisted": False,
            "session_token_surrogate": surrogate,
            "session_token_evidence_type": "surrogate",
        }
    return {
        "session_token_hash": "",
        "session_token_present": False,
        "session_token_persisted": False,
        "session_token_surrogate": "",
        "session_token_evidence_type": "",
    }


def _startup_blocker(
    *,
    blocker_id: str,
    message: str,
    context: BranchTaskRuntimeContext | None = None,
    missing: tuple[str, ...] = (),
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "schema_version": MF_SUBAGENT_STARTUP_GATE_SCHEMA_VERSION,
        "status": "blocked",
        "blocker": blocker_id,
        "blocker_id": blocker_id,
        "dispatch_blocker": True,
        "terminal_dispatch_blocker": True,
        "actual_startup_recorded": False,
        "actual_startup_required": True,
        "close_ready": False,
        "message": message,
        "missing": list(missing),
    }
    if context is not None:
        payload["context"] = {
            "project_id": context.project_id,
            "task_id": context.task_id,
            "parent_task_id": _parent_task_id_for_context(context),
            "worker_id": context.worker_id,
            "agent_id": context.agent_id,
            "fence_token": context.fence_token,
            "branch": context.branch_ref,
            "worktree": context.worktree_path,
            "base_commit": context.base_commit,
            "target_head_commit": context.target_head_commit,
            "merge_queue_id": context.merge_queue_id,
            "status": context.status,
        }
    if details:
        payload["details"] = dict(details)
    return payload


def record_mf_subagent_startup(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    task_id: str,
    payload: Mapping[str, Any],
    now_iso: str = "",
) -> dict[str, Any]:
    """Validate and record a real host-created bounded mf_sub startup.

    This is intentionally stricter than branch allocation or runtime-text
    preparation: it requires the started worker to report runtime identity,
    route identity, and a token hash/surrogate before a startup event can be
    considered durable evidence.
    """

    ensure_branch_runtime_schema(conn)
    task = str(task_id or payload.get("task_id") or "").strip()
    if not task:
        return _startup_blocker(
            blocker_id="missing_task_id",
            message="mf_subagent startup requires task_id",
            missing=("task_id",),
        )

    context = get_branch_context(conn, project_id, task)
    if context is None:
        return _startup_blocker(
            blocker_id="runtime_context_not_found",
            message="branch runtime context must exist before mf_subagent startup",
            missing=("branch_runtime_context",),
            details={"task_id": task},
        )

    parent_task_id = str(payload.get("parent_task_id") or "").strip()
    worker_role = str(payload.get("worker_role") or payload.get("role") or "").strip()
    worker_role = worker_role.lower().replace("-", "_")
    fence_token = str(payload.get("fence_token") or "").strip()
    worker_id = str(payload.get("worker_id") or "").strip()
    agent_id = str(payload.get("agent_id") or "").strip()
    token_evidence = _startup_token_evidence(payload)
    actual_cwd = str(payload.get("actual_cwd") or payload.get("cwd") or "").strip()
    actual_git_root = str(payload.get("actual_git_root") or payload.get("git_root") or "").strip()
    branch = str(payload.get("branch") or payload.get("branch_ref") or "").strip()
    head_commit = str(payload.get("head_commit") or payload.get("branch_head") or "").strip()
    base_commit = str(payload.get("base_commit") or "").strip()
    target_head_commit = str(payload.get("target_head_commit") or "").strip()
    merge_queue_id = str(payload.get("merge_queue_id") or "").strip()
    route_id = str(payload.get("route_id") or "").strip()
    route_context_hash = str(payload.get("route_context_hash") or "").strip()
    prompt_contract_id = str(payload.get("prompt_contract_id") or "").strip()
    prompt_contract_hash = str(payload.get("prompt_contract_hash") or "").strip()
    visible_manifest = str(payload.get("visible_injection_manifest_hash") or "").strip()
    owned_files = _startup_string_list(payload.get("owned_files"))
    runtime_context_id = str(
        payload.get("runtime_context_id") or runtime_context_id_for_branch_context(context)
    ).strip()
    launch_text_hash = str(payload.get("launch_text_hash") or "").strip()
    observer_command_id = str(payload.get("observer_command_id") or "").strip()
    host_startup_id = str(payload.get("host_startup_id") or "").strip()
    startup_source = str(payload.get("startup_source") or "host_created_mf_sub_worker")
    startup_source_normalized = startup_source.lower().replace("-", "_")
    host_adapter_startup = bool(
        token_evidence["session_token_surrogate"]
        and (
            "host_adapter" in startup_source_normalized
            or str(token_evidence["session_token_surrogate"]).startswith("host-adapter:")
            or payload.get("host_adapter_startup") is True
        )
    )

    missing: list[str] = []
    for field, value in (
        ("parent_task_id", parent_task_id),
        ("worker_role", worker_role),
        ("worker_id", worker_id),
        ("agent_id", agent_id),
        ("fence_token", fence_token),
        ("actual_cwd", actual_cwd),
        ("actual_git_root", actual_git_root),
        ("branch", branch),
        ("head_commit", head_commit),
        ("base_commit", base_commit),
        ("target_head_commit", target_head_commit),
        ("merge_queue_id", merge_queue_id),
        ("route_id", route_id),
        ("route_context_hash", route_context_hash),
        ("prompt_contract_id", prompt_contract_id),
        ("prompt_contract_hash", prompt_contract_hash),
        ("visible_injection_manifest_hash", visible_manifest),
        ("owned_files", owned_files),
    ):
        if not value:
            missing.append(field)
    if not (
        token_evidence["session_token_hash"]
        or token_evidence["session_token_surrogate"]
    ):
        missing.append("session_token_or_surrogate")
    if missing:
        return _startup_blocker(
            blocker_id="no_truthful_bounded_mf_sub_startup_surface_available",
            message=(
                "actual mf_sub startup evidence is incomplete; branch allocation "
                "or runtime-text startup intent is not sufficient"
            ),
            context=context,
            missing=tuple(missing),
        )

    if worker_role != "mf_sub":
        return _startup_blocker(
            blocker_id="worker_role_mismatch",
            message="mf_subagent startup requires worker_role=mf_sub",
            context=context,
            details={"worker_role": worker_role},
        )
    if context.worker_id and worker_id != context.worker_id:
        return _startup_blocker(
            blocker_id="worker_id_mismatch",
            message="mf_subagent startup worker_id must match branch runtime context",
            context=context,
            details={"worker_id": worker_id, "expected_worker_id": context.worker_id},
        )
    agent_id_match_mode = "exact_or_unallocated"
    if context.agent_id and agent_id != context.agent_id:
        if host_adapter_startup:
            agent_id_match_mode = "host_adapter_startup_token_surrogate"
        else:
            return _startup_blocker(
                blocker_id="agent_id_mismatch",
                message="mf_subagent startup agent_id must match branch runtime context",
                context=context,
                details={"agent_id": agent_id, "expected_agent_id": context.agent_id},
            )
    elif context.agent_id:
        agent_id_match_mode = "exact"
    try:
        _require_current_fence(context, fence_token)
    except BranchRuntimeFenceError:
        return _startup_blocker(
            blocker_id="fence_invalidated_or_unknown",
            message="mf_subagent startup fence is invalidated or unknown",
            context=context,
            details={"task_id": task},
        )
    allowed_parent_ids = {
        value
        for value in (
            context.root_task_id,
            context.chain_id,
            context.stage_task_id,
            context.backlog_id,
        )
        if value
    }
    if allowed_parent_ids and parent_task_id not in allowed_parent_ids:
        return _startup_blocker(
            blocker_id="parent_task_id_mismatch",
            message="mf_subagent startup parent_task_id does not match branch context",
            context=context,
            details={
                "parent_task_id": parent_task_id,
                "allowed_parent_task_ids": sorted(allowed_parent_ids),
            },
        )
    expected_worktree = context.worktree_path
    if expected_worktree:
        if not _startup_path_matches(actual_git_root, expected_worktree):
            return _startup_blocker(
                blocker_id="actual_git_root_mismatch",
                message="mf_subagent startup actual_git_root must match assigned worktree",
                context=context,
                details={
                    "actual_git_root": actual_git_root,
                    "expected_worktree": expected_worktree,
                },
            )
        actual_cwd_path = _startup_path_text(actual_cwd)
        expected_path = _startup_path_text(expected_worktree)
        if actual_cwd_path and expected_path and not (
            actual_cwd_path == expected_path
            or actual_cwd_path.startswith(expected_path + "/")
        ):
            return _startup_blocker(
                blocker_id="actual_cwd_outside_worktree",
                message="mf_subagent startup actual_cwd must be inside assigned worktree",
                context=context,
                details={
                    "actual_cwd": actual_cwd,
                    "expected_worktree": expected_worktree,
                },
            )
    if _startup_branch_name(branch) != _startup_branch_name(context.branch_ref):
        return _startup_blocker(
            blocker_id="branch_mismatch",
            message="mf_subagent startup branch must match branch runtime context",
            context=context,
            details={"branch": branch, "expected_branch": context.branch_ref},
        )
    if context.base_commit and base_commit and base_commit != context.base_commit:
        return _startup_blocker(
            blocker_id="base_commit_mismatch",
            message="mf_subagent startup base_commit must match branch runtime context",
            context=context,
            details={"base_commit": base_commit, "expected_base_commit": context.base_commit},
        )
    if context.target_head_commit and target_head_commit and target_head_commit != context.target_head_commit:
        return _startup_blocker(
            blocker_id="target_head_commit_mismatch",
            message="mf_subagent startup target_head_commit must match branch runtime context",
            context=context,
            details={
                "target_head_commit": target_head_commit,
                "expected_target_head_commit": context.target_head_commit,
            },
        )
    if context.merge_queue_id and merge_queue_id != context.merge_queue_id:
        return _startup_blocker(
            blocker_id="merge_queue_id_mismatch",
            message="mf_subagent startup merge_queue_id must match branch runtime context",
            context=context,
            details={
                "merge_queue_id": merge_queue_id,
                "expected_merge_queue_id": context.merge_queue_id,
            },
        )

    saved = upsert_branch_context(
        conn,
        replace(
            context,
            status=STATE_RUNNING,
            worker_id=worker_id,
            agent_id=agent_id,
            head_commit=head_commit,
            last_recovery_action="mf_subagent_startup_recorded",
        ),
        now_iso=now_iso,
    )
    gate: dict[str, Any] = {
        "schema_version": MF_SUBAGENT_STARTUP_GATE_SCHEMA_VERSION,
        "gate_kind": "mf_subagent.startup",
        "status": "passed",
        "ok": True,
        "allowed": True,
        "bounded": True,
        "started": True,
        "startup_complete": True,
        "actual_startup_recorded": True,
        "actual_startup_required": False,
        "same_as_expected_worker": True,
        "fence_token_matches": True,
        "close_satisfying": True,
        "raw_launch_text_persisted": False,
        "project_id": project_id,
        "backlog_id": saved.backlog_id,
        "runtime_context_id": runtime_context_id,
        "task_id": saved.task_id,
        "parent_task_id": parent_task_id,
        "worker_role": "mf_sub",
        "role": "mf_sub",
        "worker_id": worker_id,
        "agent_id": agent_id,
        "expected_agent_id": context.agent_id,
        "agent_id_match_mode": agent_id_match_mode,
        "host_adapter_startup_token_accepted": host_adapter_startup,
        "fence_token": saved.fence_token,
        "branch": saved.branch_ref,
        "branch_ref": saved.branch_ref,
        "worktree": saved.worktree_path,
        "worktree_path": saved.worktree_path,
        "assigned_worktree": saved.worktree_path,
        "actual_cwd": _startup_path_text(actual_cwd),
        "actual_git_root": _startup_path_text(actual_git_root),
        "base_commit": saved.base_commit,
        "target_head_commit": saved.target_head_commit,
        "head_commit": saved.head_commit,
        "merge_queue_id": saved.merge_queue_id,
        "owned_files": list(owned_files),
        "route_id": route_id,
        "route_context_hash": route_context_hash,
        "prompt_contract_id": prompt_contract_id,
        "prompt_contract_hash": prompt_contract_hash,
        "visible_injection_manifest_hash": visible_manifest,
        "session_token_hash": token_evidence["session_token_hash"],
        "session_token_surrogate": token_evidence["session_token_surrogate"],
        "session_token_evidence_type": token_evidence["session_token_evidence_type"],
        "session_token_present": token_evidence["session_token_present"],
        "session_token_persisted": False,
        "startup_source": startup_source,
        "startup_timing": "actual_worker_started",
        "observer_command_id": observer_command_id,
        "host_startup_id": host_startup_id,
    }
    if launch_text_hash:
        gate["launch_text_hash"] = launch_text_hash
    timeline_event = {
        "schema_version": 2,
        "event_type": "mf_subagent.startup",
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "passed",
        "actor": "mf_sub",
        "project_id": project_id,
        "backlog_id": saved.backlog_id,
        "task_id": saved.task_id,
        "attempt_num": saved.attempt,
        "correlation_id": observer_command_id or host_startup_id,
        "payload": {
            "mf_subagent_startup_gate": gate,
        },
        "artifact_refs": {
            "runtime_context_id": runtime_context_id,
            "session_token_evidence_type": token_evidence["session_token_evidence_type"],
        },
        "commit_sha": saved.head_commit,
    }
    return {
        "ok": True,
        "schema_version": MF_SUBAGENT_STARTUP_GATE_SCHEMA_VERSION,
        "status": "startup_recorded",
        "project_id": project_id,
        "context": branch_context_to_dict(saved),
        "startup_gate": gate,
        "timeline_event": timeline_event,
        "actual_startup_recorded": True,
        "close_ready": False,
    }


def validate_mf_subagent_runtime_context_lookup(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    runtime_context_id: str,
    fence_token: str,
    parent_task_id: str = "",
    worker_role: str = "",
) -> BranchTaskRuntimeContext:
    """Validate runtime_context_id + fence identity for worker contract polling."""

    ensure_branch_runtime_schema(conn)
    runtime_id = str(runtime_context_id or "").strip()
    fence = str(fence_token or "").strip()
    role = str(worker_role or "").strip().lower().replace("-", "_")
    if role and role != "mf_sub":
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if not runtime_id or not fence:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")

    context = get_branch_context_by_runtime_context_id(conn, project_id, runtime_id)
    if context is None or not context.fence_token:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    try:
        _require_current_fence(context, fence)
    except BranchRuntimeFenceError as exc:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown") from exc

    if context.status not in ACTIVE_MF_SUBAGENT_GRAPH_QUERY_STATES:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if context.lease_expires_at and context.lease_expires_at < utc_now():
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")

    parent = str(parent_task_id or "").strip()
    if parent:
        allowed_parent_ids = {
            value
            for value in (
                context.root_task_id,
                context.chain_id,
                context.stage_task_id,
                context.task_id,
                context.backlog_id,
            )
            if value
        }
        if allowed_parent_ids and parent not in allowed_parent_ids:
            raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    return context


def record_branch_checkpoint(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    task_id: str,
    checkpoint_id: str,
    fence_token: str,
    head_commit: str = "",
    replay_source: str = "checkpoint",
    now_iso: str = "",
) -> BranchTaskRuntimeContext:
    ensure_branch_runtime_schema(conn)
    context = get_branch_context(conn, project_id, task_id)
    if context is None:
        raise KeyError(f"branch runtime context not found: {project_id}/{task_id}")
    _require_current_fence(context, fence_token)
    now = now_iso or utc_now()
    next_head = str(head_commit or context.head_commit or "").strip()
    conn.execute(
        """
        UPDATE parallel_branch_runtime_contexts
        SET checkpoint_id = ?, replay_source = ?, head_commit = ?, updated_at = ?
        WHERE project_id = ? AND task_id = ?
        """,
        (checkpoint_id, replay_source, next_head, now, project_id, task_id),
    )
    found = get_branch_context(conn, project_id, task_id)
    if found is None:
        raise RuntimeError(f"branch runtime context disappeared: {project_id}/{task_id}")
    return found


def record_branch_finish_gate(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    task_id: str,
    checkpoint_id: str,
    fence_token: str,
    head_commit: str = "",
    replay_source: str = "mf_sub_finish_gate",
    now_iso: str = "",
) -> BranchTaskRuntimeContext:
    """Record a validated MF subagent finish-gate checkpoint."""
    ensure_branch_runtime_schema(conn)
    context = get_branch_context(conn, project_id, task_id)
    if context is None:
        raise KeyError(f"branch runtime context not found: {project_id}/{task_id}")
    _require_current_fence(context, fence_token)
    now = now_iso or utc_now()
    next_head = str(head_commit or context.head_commit or "").strip()
    conn.execute(
        """
        UPDATE parallel_branch_runtime_contexts
        SET checkpoint_id = ?,
            replay_source = ?,
            head_commit = ?,
            status = ?,
            updated_at = ?
        WHERE project_id = ? AND task_id = ?
        """,
        (
            checkpoint_id,
            replay_source,
            next_head,
            STATE_VALIDATED,
            now,
            project_id,
            task_id,
        ),
    )
    found = get_branch_context(conn, project_id, task_id)
    if found is None:
        raise RuntimeError(f"branch runtime context disappeared: {project_id}/{task_id}")
    return found


def recover_expired_branch_contexts(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    now_iso: str = "",
    actor: str = "observer_recovery",
) -> list[BranchTaskRuntimeContext]:
    """Mark expired running branch contexts reclaimable and rotate fences."""
    ensure_branch_runtime_schema(conn)
    now = now_iso or utc_now()
    rows = conn.execute(
        """
        SELECT * FROM parallel_branch_runtime_contexts
        WHERE project_id = ? AND status = ? AND lease_expires_at != ''
          AND lease_expires_at < ?
        ORDER BY task_id
        """,
        (project_id, STATE_RUNNING, now),
    ).fetchall()

    recovered: list[BranchTaskRuntimeContext] = []
    for row in rows:
        task_id = row["task_id"]
        lease_id = f"recovery-{uuid.uuid4().hex[:12]}"
        fence_token = f"fence-recovery-{uuid.uuid4().hex[:12]}"
        conn.execute(
            """
            UPDATE parallel_branch_runtime_contexts
            SET status = ?,
                attempt = attempt + 1,
                lease_id = ?,
                fence_token = ?,
                agent_id = '',
                worker_id = ?,
                last_recovery_action = ?,
                updated_at = ?
            WHERE project_id = ? AND task_id = ?
            """,
            (
                STATE_RECLAIMABLE,
                lease_id,
                fence_token,
                actor,
                ACTION_RECLAIM_FROM_CHECKPOINT,
                now,
                project_id,
                task_id,
            ),
        )
        context = get_branch_context(conn, project_id, task_id)
        if context is not None:
            recovered.append(context)
    return recovered


def runtime_tasks_from_contexts(
    contexts: list[BranchTaskRuntimeContext],
    *,
    now_iso: str = "",
) -> list[BranchRuntimeTask]:
    return [context.to_runtime_task(now_iso=now_iso) for context in contexts]


def _safe_slug(value: str, fallback: str) -> str:
    text = str(value or "").strip().lower()
    out: list[str] = []
    last_dash = False
    for char in text:
        if char.isascii() and char.isalnum():
            out.append(char)
            last_dash = False
        elif not last_dash:
            out.append("-")
            last_dash = True
    slug = "".join(out).strip("-")
    return slug or fallback


def _attempt_suffix(attempt: int) -> str:
    try:
        attempt_num = max(1, int(attempt or 1))
    except (TypeError, ValueError):
        attempt_num = 1
    return f"-attempt-{attempt_num}" if attempt_num > 1 else ""


def plan_branch_runtime_context(
    *,
    project_id: str,
    task_id: str,
    workspace_root: str = "",
    batch_id: str = "",
    backlog_id: str = "",
    chain_id: str = "",
    root_task_id: str = "",
    stage_task_id: str = "",
    stage_type: str = "",
    agent_id: str = "",
    worker_id: str = "",
    attempt: int = 1,
    branch_prefix: str = "codex",
    worktree_root: str = ".worktrees",
    ref_name: str = "main",
    base_commit: str = "",
    target_head_commit: str = "",
    merge_queue_id: str = "",
    fence_token: str = "",
    status: str = STATE_ALLOCATED,
) -> BranchTaskRuntimeContext:
    """Plan deterministic branch/worktree identity without invoking git."""
    task_slug = _safe_slug(task_id, "task")
    worker_slug = _safe_slug(worker_id, "") if worker_id else ""
    prefix = _safe_slug(branch_prefix, "codex")
    try:
        attempt_num = max(1, int(attempt or 1))
    except (TypeError, ValueError):
        attempt_num = 1
    suffix = _attempt_suffix(attempt)
    branch_ref = f"refs/heads/{prefix}/{task_slug}{suffix}"
    worktree_id = f"wt-{task_slug}{suffix}"
    worktree_parts = [part for part in (worktree_root, worker_slug, f"{task_slug}{suffix}") if part]
    worktree_path = str(Path(workspace_root, *worktree_parts)) if workspace_root else str(Path(*worktree_parts))
    return BranchTaskRuntimeContext(
        project_id=project_id,
        task_id=task_id,
        batch_id=batch_id,
        backlog_id=backlog_id,
        chain_id=chain_id,
        root_task_id=root_task_id,
        stage_task_id=stage_task_id or task_id,
        stage_type=stage_type,
        agent_id=agent_id,
        worker_id=worker_id,
        attempt=attempt_num,
        branch_ref=branch_ref,
        ref_name=ref_name,
        worktree_id=worktree_id,
        worktree_path=worktree_path,
        base_commit=base_commit,
        target_head_commit=target_head_commit,
        merge_queue_id=merge_queue_id,
        fence_token=fence_token,
        status=status,
    )


def _branch_name_from_ref(branch_ref: str) -> str:
    text = str(branch_ref or "").strip()
    prefix = "refs/heads/"
    return text[len(prefix):] if text.startswith(prefix) else text


def _resolve_repo_worktree_path(repo_root_path: str | Path, worktree_path: str) -> Path:
    root = Path(repo_root_path).resolve()
    candidate = Path(str(worktree_path or ""))
    return candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()


def _worktree_relpath(repo_root_path: str | Path, worktree_path: Path) -> str:
    root = Path(repo_root_path).resolve()
    try:
        return str(worktree_path.resolve().relative_to(root)).replace("\\", "/")
    except ValueError:
        return ""


def branch_strategy_from_runtime_context(
    context: BranchTaskRuntimeContext,
    *,
    repo_root_path: str | Path,
) -> Any:
    """Build a batch_jobs BranchStrategy from branch runtime identity."""
    from . import batch_jobs

    root = batch_jobs.repo_root(repo_root_path)
    work_branch = _branch_name_from_ref(context.branch_ref)
    if not work_branch:
        raise ValueError("branch_ref is required to materialize a worktree")
    worktree_path = _resolve_repo_worktree_path(root, context.worktree_path)
    base_commit = context.base_commit or context.target_head_commit or batch_jobs.git_commit(root)
    return batch_jobs.BranchStrategy(
        job_type=batch_jobs.JOB_FEATURE_WORK,
        target_branch=context.ref_name or "main",
        base_commit=base_commit,
        work_branch=work_branch,
        worktree_path=str(worktree_path),
        worktree_relpath=_worktree_relpath(root, worktree_path),
        direct=False,
        merge_policy="merge_queue",
        project_id=context.project_id,
    )


def materialize_branch_worktree(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    task_id: str,
    repo_root_path: str | Path,
    fence_token: str = "",
    status: str = STATE_WORKTREE_READY,
    now_iso: str = "",
) -> dict[str, Any]:
    """Create the planned worktree and persist the updated runtime context.

    Git side effects are limited to branch/worktree creation under `.worktrees`
    using the existing batch job worktree helper. Merge execution remains outside
    this helper.
    """
    from . import batch_jobs

    ensure_branch_runtime_schema(conn)
    context = get_branch_context(conn, project_id, task_id)
    if context is None:
        raise KeyError(f"branch runtime context not found: {project_id}/{task_id}")
    if context.fence_token or fence_token:
        _require_current_fence(context, fence_token)

    strategy = branch_strategy_from_runtime_context(context, repo_root_path=repo_root_path)
    worktree = batch_jobs.create_worktree(strategy, repo_root_path=repo_root_path)
    head_commit = ""
    try:
        head_commit = batch_jobs.git_commit(strategy.worktree_path)
    except batch_jobs.BatchJobError:
        head_commit = context.head_commit

    updated = replace(
        context,
        status=status,
        branch_ref=f"refs/heads/{strategy.work_branch}",
        ref_name=strategy.target_branch,
        worktree_path=strategy.worktree_path,
        base_commit=context.base_commit or strategy.base_commit,
        target_head_commit=context.target_head_commit or strategy.base_commit,
        head_commit=head_commit or context.head_commit,
    )
    saved = upsert_branch_context(conn, updated, now_iso=now_iso)
    return {
        "context": branch_context_to_dict(saved),
        "branch_strategy": strategy.to_metadata(),
        "worktree": worktree,
    }


def queue_merge_item_for_branch_context(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    task_id: str,
    merge_queue_id: str,
    queue_item_id: str = "",
    queue_index: int = 0,
    status: str = STATE_QUEUED_FOR_MERGE,
    fence_token: str = "",
    depends_on: tuple[str, ...] = (),
    hard_depends_on: tuple[str, ...] = (),
    serializes_after: tuple[str, ...] = (),
    conflicts_with: tuple[str, ...] = (),
    same_node_or_file_conflicts: tuple[str, ...] = (),
    requires_graph_epoch: tuple[str, ...] = (),
    target_ref: str = "refs/heads/main",
    current_target_head: str = "",
    validated_target_head: str = "",
    validation_attempt: int = 0,
    merge_preview_id: str = "",
    checkpoint_id: str = "",
    require_finish_gate: bool = False,
    now_iso: str = "",
) -> dict[str, Any]:
    """Persist a fenced merge queue request for one branch runtime context."""
    ensure_branch_runtime_schema(conn)
    queue_id = str(merge_queue_id or "").strip()
    if not queue_id:
        raise ValueError("merge_queue_id is required")
    context = get_branch_context(conn, project_id, task_id)
    if context is None:
        raise KeyError(f"branch runtime context not found: {project_id}/{task_id}")
    if context.fence_token or fence_token:
        _require_current_fence(context, fence_token)
    if require_finish_gate:
        expected_checkpoint = str(checkpoint_id or "").strip()
        if not expected_checkpoint:
            raise ValueError("checkpoint_id is required when require_finish_gate is true")
        if context.checkpoint_id != expected_checkpoint:
            raise ValueError("checkpoint_id does not match the validated finish gate")
        if context.replay_source != "mf_sub_finish_gate":
            raise ValueError("validated mf_sub finish gate checkpoint is required")

    item = MergeQueueItem(
        project_id=project_id,
        merge_queue_id=queue_id,
        queue_item_id=queue_item_id or f"{queue_id}:{task_id}",
        task_id=task_id,
        branch_ref=context.branch_ref,
        queue_index=queue_index,
        status=status or STATE_QUEUED_FOR_MERGE,
        depends_on=tuple(depends_on or context.depends_on),
        hard_depends_on=tuple(hard_depends_on),
        serializes_after=tuple(serializes_after),
        conflicts_with=tuple(conflicts_with),
        same_node_or_file_conflicts=tuple(same_node_or_file_conflicts),
        requires_graph_epoch=tuple(requires_graph_epoch),
        target_ref=target_ref or context.ref_name or "refs/heads/main",
        base_commit=context.base_commit,
        branch_head=context.head_commit,
        validated_target_head=validated_target_head,
        current_target_head=current_target_head or context.target_head_commit,
        validation_attempt=validation_attempt,
        merge_preview_id=merge_preview_id or context.merge_preview_id,
        snapshot_id=context.snapshot_id,
        projection_id=context.projection_id,
    )
    saved_item = upsert_merge_queue_item(conn, item, now_iso=now_iso)
    updated_context = replace(
        context,
        status=saved_item.status,
        merge_queue_id=saved_item.merge_queue_id,
        merge_preview_id=saved_item.merge_preview_id,
        target_head_commit=saved_item.current_target_head or context.target_head_commit,
    )
    saved_context = upsert_branch_context(conn, updated_context, now_iso=now_iso)
    return {
        "context": branch_context_to_dict(saved_context),
        "queue_item": merge_queue_item_to_dict(saved_item),
    }


def branch_context_from_chain_stage(
    *,
    project_id: str,
    chain_id: str,
    root_task_id: str,
    stage_task_id: str,
    stage_type: str,
    retry_round: int = 0,
    branch_ref: str,
    task_id: str = "",
    batch_id: str = "",
    backlog_id: str = "",
    agent_id: str = "",
    worker_id: str = "",
    status: str = STATE_RUNNING,
    ref_name: str = "main",
    worktree_id: str = "",
    worktree_path: str = "",
    base_commit: str = "",
    head_commit: str = "",
    target_head_commit: str = "",
    snapshot_id: str = "",
    projection_id: str = "",
    merge_queue_id: str = "",
    merge_preview_id: str = "",
    rollback_epoch: str = "",
    replay_epoch: str = "",
    depends_on: tuple[str, ...] = (),
    checkpoint_id: str = "",
    replay_source: str = "",
    lease_id: str = "",
    lease_expires_at: str = "",
    fence_token: str = "",
    attempt: int | None = None,
) -> BranchTaskRuntimeContext:
    """Create a branch runtime context for a Chain stage without running Chain."""
    stage_task = str(stage_task_id or task_id or "").strip()
    if not stage_task:
        raise ValueError("stage_task_id or task_id is required")
    round_num = max(0, int(retry_round or 0))
    return BranchTaskRuntimeContext(
        project_id=project_id,
        task_id=task_id or stage_task,
        batch_id=batch_id,
        backlog_id=backlog_id,
        chain_id=chain_id or root_task_id,
        root_task_id=root_task_id or chain_id,
        stage_task_id=stage_task,
        stage_type=stage_type,
        retry_round=round_num,
        agent_id=agent_id,
        worker_id=worker_id,
        attempt=attempt if attempt is not None else round_num + 1,
        lease_id=lease_id,
        lease_expires_at=lease_expires_at,
        fence_token=fence_token,
        branch_ref=branch_ref,
        ref_name=ref_name,
        worktree_id=worktree_id,
        worktree_path=worktree_path,
        base_commit=base_commit,
        head_commit=head_commit,
        target_head_commit=target_head_commit,
        snapshot_id=snapshot_id,
        projection_id=projection_id,
        merge_queue_id=merge_queue_id,
        merge_preview_id=merge_preview_id,
        rollback_epoch=rollback_epoch,
        replay_epoch=replay_epoch,
        status=status,
        depends_on=depends_on,
        checkpoint_id=checkpoint_id,
        replay_source=replay_source,
    )


def _merge_items_by_task(items: list[MergeQueueItem]) -> dict[str, MergeQueueItem]:
    return {item.task_id: item for item in items}


def _require_single_merge_queue_scope(items: list[MergeQueueItem]) -> None:
    project_ids = {item.project_id for item in items}
    queue_ids = {item.merge_queue_id for item in items}
    target_refs = {item.target_ref for item in items}
    if len(project_ids) > 1:
        raise ValueError("merge queue decisions must not mix project_id values")
    if len(queue_ids) > 1:
        raise ValueError("merge queue decisions must not mix merge_queue_id values")
    if len(target_refs) > 1:
        raise ValueError("merge queue decisions must not mix target_ref values")


def _merge_dependency_blockers(
    item: MergeQueueItem,
    *,
    items_by_task: dict[str, MergeQueueItem],
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    blocker_types: dict[str, set[str]] = {}

    def add(dep: str, blocker_type: str) -> None:
        dep_id = str(dep or "").strip()
        if not dep_id:
            return
        blocker_types.setdefault(dep_id, set()).add(blocker_type)

    dependency_groups = (
        ("hard_depends_on", tuple(item.depends_on) + tuple(item.hard_depends_on)),
        ("serializes_after", tuple(item.serializes_after)),
        ("requires_graph_epoch", tuple(item.requires_graph_epoch)),
    )
    for blocker_type, deps in dependency_groups:
        for dep in deps:
            dep_item = items_by_task.get(dep)
            if dep_item is None or dep_item.status not in MERGE_DONE_STATES:
                add(dep, blocker_type)
                continue
            if blocker_type == "requires_graph_epoch" and not (
                dep_item.snapshot_id and dep_item.projection_id
            ):
                add(dep, blocker_type)

    for blocker_type, deps in (
        ("conflicts_with", item.conflicts_with),
        ("same_node_or_file_conflict", item.same_node_or_file_conflicts),
    ):
        for dep in deps:
            dep_item = items_by_task.get(dep)
            if dep_item is None or dep_item.status not in TERMINAL_NON_BLOCKING_STATES:
                add(dep, blocker_type)

    ordered = tuple(dep for dep in items_by_task if dep in blocker_types)
    extras = tuple(sorted(dep for dep in blocker_types if dep not in items_by_task))
    blockers = ordered + extras
    return blockers, {dep: tuple(sorted(blocker_types[dep])) for dep in blockers}


def _has_terminal_dependency_blocker(
    blockers: tuple[str, ...],
    *,
    items_by_task: dict[str, MergeQueueItem],
) -> bool:
    for dep in blockers:
        dep_item = items_by_task.get(dep)
        blocking_states = MERGE_BLOCKING_STATES | MERGE_REVALIDATION_BLOCKING_STATES
        if dep_item is None or dep_item.status in blocking_states:
            return True
    return False


def _has_conflict_dependency_blocker(blocker_types: dict[str, tuple[str, ...]]) -> bool:
    return any(
        "conflicts_with" in values or "same_node_or_file_conflict" in values
        for values in blocker_types.values()
    )


def _target_head_moved_after_validation(item: MergeQueueItem) -> bool:
    return bool(
        item.validated_target_head
        and item.current_target_head
        and item.validated_target_head != item.current_target_head
    )


def _merge_queue_actions_for(action: str) -> tuple[str, ...]:
    if action == ACTION_WAIT_FOR_DEPENDENCY:
        return ("wait_for_dependency", "do_not_merge")
    if action == ACTION_BLOCKED_BY_DEPENDENCY:
        return ("resolve_dependency", "do_not_merge")
    if action == ACTION_REVALIDATE_AFTER_DEPENDENCY_MERGE:
        return (
            "rebase_or_sync",
            "run_scope_reconcile",
            "verify_semantic_projection",
            "refresh_merge_preview",
        )
    if action == ACTION_ALLOW_MERGE:
        return ("merge",)
    if action == ACTION_OBSERVER_DECISION_REQUIRED:
        return ("fix_or_rebase", "abandon", "rollback_batch")
    return ()


def decide_merge_queue(
    items: list[MergeQueueItem],
    *,
    scenario_id: str = "PB-002",
) -> MergeQueuePlan:
    """Compute ordered merge queue decisions without mutating refs or state."""
    _require_single_merge_queue_scope(items)
    ordered_items = sorted(items, key=lambda item: (item.queue_index, item.queue_item_id))
    items_by_task = _merge_items_by_task(ordered_items)
    decisions: list[MergeQueueDecision] = []

    for item in ordered_items:
        blockers, blocker_types = _merge_dependency_blockers(item, items_by_task=items_by_task)
        terminal_blocker = _has_terminal_dependency_blocker(
            blockers,
            items_by_task=items_by_task,
        )
        conflict_blocker = _has_conflict_dependency_blocker(blocker_types)
        stale_target_head = False

        if item.status == STATE_MERGED:
            queue_state = STATE_MERGED
            action = ACTION_LEAVE_MERGED
            merge_allowed = False
            target_mutation_allowed = False
            graph_allowed = True
            semantic_allowed = True
        elif item.status == STATE_MERGE_FAILED:
            queue_state = STATE_MERGE_BLOCKED
            action = ACTION_OBSERVER_DECISION_REQUIRED
            merge_allowed = False
            target_mutation_allowed = False
            graph_allowed = False
            semantic_allowed = False
        elif blockers:
            hard_blocked = terminal_blocker or conflict_blocker
            queue_state = STATE_DEPENDENCY_BLOCKED if hard_blocked else STATE_WAITING_DEPENDENCY
            action = ACTION_BLOCKED_BY_DEPENDENCY if hard_blocked else ACTION_WAIT_FOR_DEPENDENCY
            merge_allowed = False
            target_mutation_allowed = False
            graph_allowed = False
            semantic_allowed = False
        elif item.status in MERGE_READY_INPUT_STATES and _target_head_moved_after_validation(item):
            queue_state = STATE_STALE_AFTER_DEPENDENCY_MERGE
            action = ACTION_REVALIDATE_AFTER_DEPENDENCY_MERGE
            stale_target_head = True
            merge_allowed = False
            target_mutation_allowed = False
            graph_allowed = False
            semantic_allowed = False
        elif item.status in MERGE_READY_INPUT_STATES:
            queue_state = STATE_MERGE_READY
            action = ACTION_ALLOW_MERGE
            merge_allowed = True
            target_mutation_allowed = True
            graph_allowed = False
            semantic_allowed = False
        elif item.status == STATE_MERGING:
            queue_state = STATE_MERGING
            action = ACTION_MERGE_IN_PROGRESS
            merge_allowed = False
            target_mutation_allowed = False
            graph_allowed = False
            semantic_allowed = False
        else:
            queue_state = item.status or STATE_WAITING_DEPENDENCY
            action = ACTION_NOOP
            merge_allowed = False
            target_mutation_allowed = False
            graph_allowed = False
            semantic_allowed = False

        decisions.append(
            MergeQueueDecision(
                queue_item_id=item.queue_item_id,
                task_id=item.task_id,
                branch_ref=item.branch_ref,
                observed_status=item.status,
                queue_state=queue_state,
                action=action,
                dependency_blockers=blockers,
                dependency_blocker_types=blocker_types,
                stale_target_head=stale_target_head,
                next_actions=_merge_queue_actions_for(action),
                merge_allowed=merge_allowed,
                target_branch_mutation_allowed=target_mutation_allowed,
                target_graph_activation_allowed=graph_allowed,
                target_semantic_activation_allowed=semantic_allowed,
                validation_attempt=item.validation_attempt,
                merge_preview_id=item.merge_preview_id,
            )
        )

    mergeable = tuple(decision.task_id for decision in decisions if decision.merge_allowed)
    blocked = tuple(
        decision.task_id
        for decision in decisions
        if decision.queue_state in {STATE_WAITING_DEPENDENCY, STATE_DEPENDENCY_BLOCKED, STATE_MERGE_BLOCKED}
    )
    stale = tuple(
        decision.task_id
        for decision in decisions
        if decision.queue_state == STATE_STALE_AFTER_DEPENDENCY_MERGE
    )
    target_mutation_blocked = tuple(
        decision.task_id for decision in decisions if not decision.target_branch_mutation_allowed
    )

    return MergeQueuePlan(
        scenario_id=scenario_id,
        decisions=tuple(decisions),
        mergeable_task_ids=mergeable,
        blocked_task_ids=blocked,
        stale_task_ids=stale,
        target_mutation_blocked_for=target_mutation_blocked,
        dashboard_rows=tuple(decision.to_dashboard_row() for decision in decisions),
    )


def _select_merge_gate_item(
    items: list[MergeQueueItem],
    *,
    queue_item_id: str,
    task_id: str,
) -> MergeQueueItem:
    item_id = str(queue_item_id or "").strip()
    task = str(task_id or "").strip()
    for item in sorted(items, key=lambda it: (it.queue_index, it.queue_item_id)):
        if item_id and item.queue_item_id == item_id:
            return item
        if task and item.task_id == task:
            return item
    if len(items) == 1 and not item_id and not task:
        return items[0]
    raise KeyError(f"merge queue item not found: {item_id or task or '<unspecified>'}")


def select_merge_queue_item(
    items: list[MergeQueueItem],
    *,
    queue_item_id: str = "",
    task_id: str = "",
) -> MergeQueueItem:
    """Select a single queue item by queue item id or task id."""
    return _select_merge_gate_item(items, queue_item_id=queue_item_id, task_id=task_id)


def _select_merge_gate_decision(
    plan: MergeQueuePlan,
    item: MergeQueueItem,
) -> MergeQueueDecision:
    for decision in plan.decisions:
        if decision.queue_item_id == item.queue_item_id:
            return decision
    raise KeyError(f"merge queue decision not found: {item.queue_item_id}")


def _evidence_status(raw: Any) -> str:
    if isinstance(raw, dict):
        raw = raw.get("status") or raw.get("state")
    return str(raw or "").strip().lower()


def _evidence_detail(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if raw is None:
        return {}
    return {"status": raw}


def _merge_gate_evidence_rows(
    evidence: dict[str, Any],
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    rows: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for key in MERGE_GATE_REQUIRED_EVIDENCE:
        raw = evidence.get(key)
        status = _evidence_status(raw) or "missing"
        detail = _evidence_detail(raw)
        passed = status in MERGE_GATE_PASS_STATUSES
        deferred = key in MERGE_GATE_DEFERABLE_EVIDENCE and status in MERGE_GATE_DEFERRED_STATUSES
        row = {
            "key": key,
            "status": status,
            "required": True,
            "passed": bool(passed or deferred),
            "deferred": bool(deferred),
            "evidence_id": str(detail.get("evidence_id") or detail.get("id") or ""),
            "detail": detail,
        }
        rows.append(row)
        if deferred:
            warnings.append({
                "code": f"deferred_evidence:{key}",
                "source": "evidence",
                "message": f"{key} is intentionally deferred",
            })
            continue
        if not passed:
            blockers.append({
                "code": f"missing_evidence:{key}" if status == "missing" else f"failed_evidence:{key}",
                "source": "evidence",
                "message": f"{key} status is {status}",
                "status": status,
            })

    return tuple(rows), tuple(blockers), tuple(warnings)


def _merge_gate_blockers_for_queue(decision: MergeQueueDecision) -> tuple[dict[str, Any], ...]:
    if decision.merge_allowed:
        return ()
    if decision.stale_target_head:
        return ({
            "code": "stale_target_head",
            "source": "merge_queue",
            "message": "target head moved after validation",
            "queue_state": decision.queue_state,
        },)
    if decision.dependency_blockers:
        return ({
            "code": "queue_dependency_blocked",
            "source": "merge_queue",
            "message": "merge queue dependencies are not satisfied",
            "queue_state": decision.queue_state,
            "dependency_blockers": list(decision.dependency_blockers),
            "dependency_blocker_types": {
                key: list(values)
                for key, values in sorted(decision.dependency_blocker_types.items())
            },
        },)
    return ({
        "code": f"queue_state:{decision.queue_state or 'unknown'}",
        "source": "merge_queue",
        "message": f"queue action {decision.action} does not permit merge",
        "queue_state": decision.queue_state,
    },)


def _merge_gate_next_actions(
    *,
    blockers: tuple[dict[str, Any], ...],
    dry_run: bool,
) -> tuple[str, ...]:
    if blockers:
        actions: list[str] = []
        codes = {str(blocker.get("code") or "") for blocker in blockers}
        if "queue_dependency_blocked" in codes:
            actions.append("resolve_queue_dependencies")
        if "stale_target_head" in codes:
            actions.extend(("rebase_or_sync", "refresh_merge_preview"))
        if "batch_rollback_required" in codes:
            actions.append("resolve_batch_rollback")
        if any(code.startswith("missing_evidence:") for code in codes):
            actions.append("provide_required_merge_evidence")
        if any(code.startswith("failed_evidence:") for code in codes):
            actions.append("fix_failed_merge_evidence")
        return tuple(actions or ["do_not_merge"])
    if dry_run:
        return (ACTION_OPERATOR_APPROVE_LIVE_MERGE,)
    return ("execute_merge", "record_merge_result", "activate_target_graph_refs")


def decide_merge_gate(
    items: list[MergeQueueItem],
    *,
    queue_item_id: str = "",
    task_id: str = "",
    evidence: dict[str, Any] | None = None,
    batch_status: str = "",
    dry_run: bool = True,
    scenario_id: str = "PB-013",
) -> MergeGatePlan:
    """Plan the final merge gate for one queue item without mutating refs.

    This composes the ordered queue decision with required evidence checks. It
    is intentionally side-effect free so dashboard, MCP, MF, and future Chain
    clients can agree on the gate before any live merge code touches target refs.
    """
    _require_single_merge_queue_scope(items)
    selected = _select_merge_gate_item(items, queue_item_id=queue_item_id, task_id=task_id)
    queue_plan = decide_merge_queue(items, scenario_id=scenario_id)
    decision = _select_merge_gate_decision(queue_plan, selected)
    evidence_rows, evidence_blockers, evidence_warnings = _merge_gate_evidence_rows(evidence or {})
    queue_blockers = _merge_gate_blockers_for_queue(decision)

    batch = str(batch_status or "").strip()
    batch_blockers: tuple[dict[str, Any], ...] = ()
    if batch in BATCH_ROLLBACK_STATES:
        batch_blockers = ({
            "code": "batch_rollback_required",
            "source": "batch",
            "message": f"batch status {batch} blocks live merge",
            "batch_status": batch,
        },)

    blockers = queue_blockers + batch_blockers + evidence_blockers
    blocker_codes = tuple(str(blocker.get("code") or "") for blocker in blockers)
    gate_passed = not blockers
    mutation_allowed = gate_passed and not dry_run
    merge_steps = (
        "lock_target_ref",
        "verify_target_head",
        "merge_branch",
        "record_merge_result",
        "run_scope_catchup",
        "activate_target_graph_refs",
        "activate_target_semantic_projection",
    ) if gate_passed else ()

    return MergeGatePlan(
        scenario_id=scenario_id,
        project_id=selected.project_id,
        merge_queue_id=selected.merge_queue_id,
        queue_item_id=selected.queue_item_id,
        task_id=selected.task_id,
        branch_ref=selected.branch_ref,
        target_ref=selected.target_ref,
        branch_head=selected.branch_head,
        current_target_head=selected.current_target_head,
        dry_run=dry_run,
        queue_state=decision.queue_state,
        queue_action=decision.action,
        merge_gate_passed=gate_passed,
        merge_allowed=gate_passed,
        target_branch_mutation_allowed=mutation_allowed,
        target_graph_activation_allowed=mutation_allowed and bool(selected.snapshot_id),
        target_semantic_activation_allowed=mutation_allowed and bool(selected.projection_id),
        blockers=blockers,
        blocker_codes=blocker_codes,
        warnings=evidence_warnings,
        evidence=evidence_rows,
        next_actions=_merge_gate_next_actions(blockers=blockers, dry_run=dry_run),
        merge_steps=merge_steps,
        merge_preview_id=selected.merge_preview_id,
        snapshot_id=selected.snapshot_id,
        projection_id=selected.projection_id,
    )


def _bounded_command_text(value: str, limit: int = 4000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def _git_preview_command(
    repo_root: Path,
    args: list[str],
    *,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
        timeout=max(1, int(timeout_seconds or 30)),
    )


def _git_preview_commit(
    repo_root: Path,
    ref: str,
    *,
    timeout_seconds: int,
) -> tuple[str, str]:
    proc = _git_preview_command(
        repo_root,
        ["rev-parse", "--verify", f"{ref}^{{commit}}"],
        timeout_seconds=timeout_seconds,
    )
    if proc.returncode != 0:
        return "", _bounded_command_text(proc.stderr or proc.stdout)
    return proc.stdout.strip(), ""


def git_merge_preview_evidence(
    *,
    repo_root_path: str | Path,
    target_ref: str,
    branch_ref: str,
    expected_target_head: str = "",
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Return side-effect-free git merge preview evidence for a queued branch."""
    repo_root = Path(repo_root_path).resolve()
    target = str(target_ref or "").strip()
    branch = str(branch_ref or "").strip()
    if not target:
        raise ValueError("target_ref is required")
    if not branch:
        raise ValueError("branch_ref is required")

    target_commit, target_error = _git_preview_commit(
        repo_root,
        target,
        timeout_seconds=timeout_seconds,
    )
    branch_commit, branch_error = _git_preview_commit(
        repo_root,
        branch,
        timeout_seconds=timeout_seconds,
    )
    evidence_id = f"merge-preview:{target_commit[:12] or 'unknown'}:{branch_commit[:12] or 'unknown'}"
    base_payload = {
        "key": "git_conflict_check",
        "evidence_id": evidence_id,
        "repo_root": str(repo_root),
        "target_ref": target,
        "branch_ref": branch,
        "target_commit": target_commit,
        "branch_commit": branch_commit,
        "expected_target_head": str(expected_target_head or ""),
        "command": "git merge-tree --write-tree <target_commit> <branch_commit>",
    }
    if target_error or branch_error:
        return {
            **base_payload,
            "status": "error",
            "passed": False,
            "reason": target_error or branch_error,
        }

    expected = str(expected_target_head or "").strip()
    if expected and expected != target_commit:
        return {
            **base_payload,
            "status": "stale",
            "passed": False,
            "reason": "target head differs from expected_target_head",
        }

    merge_base_proc = _git_preview_command(
        repo_root,
        ["merge-base", target_commit, branch_commit],
        timeout_seconds=timeout_seconds,
    )
    merge_base = merge_base_proc.stdout.strip() if merge_base_proc.returncode == 0 else ""
    preview_proc = _git_preview_command(
        repo_root,
        ["merge-tree", "--write-tree", target_commit, branch_commit],
        timeout_seconds=timeout_seconds,
    )
    stdout = _bounded_command_text(preview_proc.stdout)
    stderr = _bounded_command_text(preview_proc.stderr)
    first_line = stdout.splitlines()[0].strip() if stdout.splitlines() else ""
    clean = preview_proc.returncode == 0
    return {
        **base_payload,
        "status": "pass" if clean else "fail",
        "passed": clean,
        "reason": "" if clean else "merge-tree reported conflicts",
        "merge_base": merge_base,
        "preview_tree": first_line if clean else "",
        "returncode": preview_proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def _git_worktree_dirty_files(repo_root: Path, *, timeout_seconds: int) -> list[str]:
    proc = _git_preview_command(
        repo_root,
        ["status", "--porcelain"],
        timeout_seconds=timeout_seconds,
    )
    if proc.returncode != 0:
        return ["<git-status-error>"]
    return [line[3:] if len(line) > 3 else line for line in proc.stdout.splitlines() if line]


def execute_merge_queue_item(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    merge_queue_id: str,
    repo_root_path: str | Path,
    queue_item_id: str = "",
    task_id: str = "",
    target_ref: str = "",
    evidence: dict[str, Any] | None = None,
    batch_status: str = "",
    dry_run: bool = True,
    allow_target_ref_mutation: bool = False,
    message: str = "",
    bug_id: str = "",
    fence_token: str = "",
    now_iso: str = "",
    timeout_seconds: int = 30,
    scenario_id: str = "PB-016",
) -> dict[str, Any]:
    """Execute one gated merge queue item, or return a dry-run plan.

    The live path is deliberately explicit: callers must set dry_run=false and
    allow_target_ref_mutation=true, and the merge gate must pass first.
    """
    ensure_branch_runtime_schema(conn)
    items = list_merge_queue_items(
        conn,
        project_id,
        merge_queue_id,
        target_ref=target_ref,
    )
    item = select_merge_queue_item(items, queue_item_id=queue_item_id, task_id=task_id)
    repo_root = Path(repo_root_path).resolve()
    preview = git_merge_preview_evidence(
        repo_root_path=repo_root,
        target_ref=target_ref or item.target_ref,
        branch_ref=item.branch_ref,
        expected_target_head=item.current_target_head or item.validated_target_head,
        timeout_seconds=timeout_seconds,
    )
    gate_evidence = dict(evidence or {})
    gate_evidence["git_conflict_check"] = preview
    gate_plan = decide_merge_gate(
        items,
        queue_item_id=item.queue_item_id,
        evidence=gate_evidence,
        batch_status=batch_status,
        dry_run=dry_run or not allow_target_ref_mutation,
        scenario_id=scenario_id,
    )
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "executed": False,
            "preview": preview,
            "gate_plan": merge_gate_plan_to_dict(gate_plan),
            "recorded": None,
        }
    if not allow_target_ref_mutation:
        return {
            "ok": False,
            "dry_run": False,
            "executed": False,
            "error": "target_ref_mutation_not_allowed",
            "preview": preview,
            "gate_plan": merge_gate_plan_to_dict(gate_plan),
            "recorded": None,
        }
    if not gate_plan.merge_gate_passed:
        return {
            "ok": False,
            "dry_run": False,
            "executed": False,
            "error": "merge_gate_blocked",
            "preview": preview,
            "gate_plan": merge_gate_plan_to_dict(gate_plan),
            "recorded": None,
        }

    dirty_files = _git_worktree_dirty_files(repo_root, timeout_seconds=timeout_seconds)
    if dirty_files:
        return {
            "ok": False,
            "dry_run": False,
            "executed": False,
            "error": "dirty_worktree",
            "dirty_files": dirty_files,
            "preview": preview,
            "gate_plan": merge_gate_plan_to_dict(gate_plan),
            "recorded": None,
        }

    target_branch = _branch_name_from_ref(target_ref or item.target_ref)
    branch_name = _branch_name_from_ref(item.branch_ref)
    checkout = _git_preview_command(
        repo_root,
        ["checkout", target_branch],
        timeout_seconds=timeout_seconds,
    )
    if checkout.returncode != 0:
        return {
            "ok": False,
            "dry_run": False,
            "executed": False,
            "error": "checkout_failed",
            "stderr": _bounded_command_text(checkout.stderr or checkout.stdout),
            "preview": preview,
            "gate_plan": merge_gate_plan_to_dict(gate_plan),
            "recorded": None,
        }

    before_commit, before_error = _git_preview_commit(
        repo_root,
        "HEAD",
        timeout_seconds=timeout_seconds,
    )
    if before_error:
        return {
            "ok": False,
            "dry_run": False,
            "executed": False,
            "error": "target_head_unresolved",
            "stderr": before_error,
            "preview": preview,
            "gate_plan": merge_gate_plan_to_dict(gate_plan),
            "recorded": None,
        }

    from .chain_trailer import write_merge_with_trailer

    ok, merge_commit, error = write_merge_with_trailer(
        message or f"parallel branch merge: {item.task_id}",
        branch=branch_name,
        cwd=str(repo_root),
        task_id=item.task_id,
        parent_chain_sha=before_commit,
        bug_id=bug_id or item.task_id,
    )
    if not ok:
        recorded = record_merge_queue_result(
            conn,
            project_id=project_id,
            merge_queue_id=merge_queue_id,
            queue_item_id=item.queue_item_id,
            target_ref=target_ref,
            status=STATE_MERGE_FAILED,
            failure_reason=error,
            target_head_before_merge=before_commit,
            target_head_after_merge=before_commit,
            fence_token=fence_token,
            now_iso=now_iso,
        )
        return {
            "ok": False,
            "dry_run": False,
            "executed": True,
            "error": "merge_failed",
            "message": error,
            "preview": preview,
            "gate_plan": merge_gate_plan_to_dict(gate_plan),
            "recorded": recorded,
        }

    recorded = record_merge_queue_result(
        conn,
        project_id=project_id,
        merge_queue_id=merge_queue_id,
        queue_item_id=item.queue_item_id,
        target_ref=target_ref,
        status=STATE_MERGED,
        merge_commit=merge_commit,
        target_head_before_merge=before_commit,
        target_head_after_merge=merge_commit,
        fence_token=fence_token,
        now_iso=now_iso,
    )
    return {
        "ok": True,
        "dry_run": False,
        "executed": True,
        "merge_commit": merge_commit,
        "preview": preview,
        "gate_plan": merge_gate_plan_to_dict(gate_plan),
        "recorded": recorded,
    }


def _short_identity(value: str, fallback: str) -> str:
    text = str(value or "").strip()
    return text[:12] if text else fallback


def _batch_epoch(prefix: str, runtime: BatchMergeRuntime, commit: str) -> str:
    batch = runtime.batch_id or "batch"
    return f"{prefix}-{batch}-{_short_identity(commit, 'unknown')}"


def _batch_items_by_task(items: tuple[BatchMergeItem, ...]) -> dict[str, BatchMergeItem]:
    return {item.task_id: item for item in items}


def _ordered_replay_items(
    runtime: BatchMergeRuntime,
    *,
    corrected_replay_order: tuple[str, ...],
) -> tuple[BatchMergeItem, ...]:
    items_by_task = _batch_items_by_task(runtime.items)
    ordered: list[BatchMergeItem] = []
    seen: set[str] = set()
    for task_id in corrected_replay_order:
        item = items_by_task.get(task_id)
        if item and item.retained and item.status != STATE_ABANDONED:
            ordered.append(item)
            seen.add(task_id)
    for item in sorted(runtime.items, key=lambda it: (it.queue_index, it.task_id)):
        if item.task_id not in seen and item.retained and item.status != STATE_ABANDONED:
            ordered.append(item)
    return tuple(ordered)


def _batch_replay_merge_queue_items(
    runtime: BatchMergeRuntime,
    *,
    replay_items: tuple[BatchMergeItem, ...],
    replay_epoch: str,
    rollback_target_commit: str,
) -> tuple[MergeQueueItem, ...]:
    queue_id = f"replay:{runtime.batch_id}:{replay_epoch}"
    return tuple(
        MergeQueueItem(
            project_id=runtime.project_id,
            merge_queue_id=queue_id,
            queue_item_id=f"{runtime.batch_id}:{replay_epoch}:{index}:{item.task_id}",
            task_id=item.task_id,
            branch_ref=item.branch_ref,
            queue_index=index,
            status=STATE_QUEUED_FOR_MERGE,
            depends_on=item.depends_on,
            target_ref=runtime.target_ref,
            base_commit=rollback_target_commit,
            branch_head=item.branch_head,
            current_target_head=rollback_target_commit,
            merge_preview_id="",
            snapshot_id=item.snapshot_id,
            projection_id=item.projection_id,
        )
        for index, item in enumerate(replay_items, start=1)
    )


def _batch_dashboard_rows(
    runtime: BatchMergeRuntime,
    *,
    rollback_required: bool,
    rollback_epoch: str,
    replay_epoch: str,
    replay_task_ids: tuple[str, ...],
    cleanup_allowed: bool,
) -> tuple[dict[str, Any], ...]:
    replay_set = set(replay_task_ids)
    rows: list[dict[str, Any]] = []
    for item in sorted(runtime.items, key=lambda it: (it.queue_index, it.task_id)):
        if cleanup_allowed:
            action = ACTION_CLEANUP_RETAINED_BRANCH if item.retained else ACTION_NOOP
            actions = ("cleanup_retained_branch",) if item.retained else ()
        elif rollback_required and item.task_id in replay_set:
            action = ACTION_RETAIN_FOR_REPLAY
            actions = ("retain_branch", "replay_through_merge_queue", "block_cleanup")
        elif rollback_required and item.retained:
            action = ACTION_RETAIN_FOR_AUDIT
            actions = ("retain_branch", "block_cleanup")
        else:
            action = ACTION_NOOP
            actions = ("block_cleanup",) if item.retained else ()
        rows.append({
            "batch_id": runtime.batch_id,
            "task_id": item.task_id,
            "branch_ref": item.branch_ref,
            "worktree_path": item.worktree_path,
            "status": item.status,
            "retained": item.retained,
            "queue_index": item.queue_index,
            "branch_head": item.branch_head,
            "merge_commit": item.merge_commit,
            "snapshot_id": item.snapshot_id,
            "projection_id": item.projection_id,
            "rollback_epoch": rollback_epoch,
            "replay_epoch": replay_epoch,
            "cleanup_allowed": cleanup_allowed,
            "action": action,
            "operator_actions": list(actions),
        })
    return tuple(rows)


def decide_batch_rollback_replay(
    runtime: BatchMergeRuntime,
    *,
    severe_integration_failure: bool = False,
    corrected_replay_order: tuple[str, ...] = (),
    scenario_id: str = "PB-004",
) -> BatchRollbackPlan:
    """Plan batch rollback/replay without mutating git, graph, semantic, or DB state."""
    rollback_target = runtime.rollback_target_commit or runtime.batch_base_commit
    rollback_required = bool(
        severe_integration_failure
        or runtime.batch_status in BATCH_ROLLBACK_STATES
        or any(item.status in {STATE_MERGE_FAILED, STATE_ROLLBACK_REQUIRED} for item in runtime.items)
    )
    rollback_epoch = runtime.rollback_epoch or (
        _batch_epoch("rollback", runtime, rollback_target) if rollback_required else ""
    )
    replay_epoch = runtime.replay_epoch or (
        _batch_epoch("replay", runtime, runtime.current_target_head or rollback_target)
        if rollback_required
        else ""
    )
    batch_status = BATCH_STATE_ROLLBACK_REQUIRED if rollback_required else runtime.batch_status

    retained_items = tuple(item for item in runtime.items if item.retained and item.status != STATE_ABANDONED)
    replay_items = (
        _ordered_replay_items(runtime, corrected_replay_order=corrected_replay_order)
        if rollback_required
        else ()
    )
    replay_task_ids = tuple(item.task_id for item in replay_items)
    replay_queue_items = _batch_replay_merge_queue_items(
        runtime,
        replay_items=replay_items,
        replay_epoch=replay_epoch,
        rollback_target_commit=rollback_target,
    ) if rollback_required else ()

    abandoned_merge_commits = tuple(
        item.merge_commit for item in runtime.items if item.merge_commit
    )
    abandoned_snapshot_ids = tuple(
        item.snapshot_id for item in runtime.items if item.snapshot_id and item.status == STATE_MERGED
    )
    abandoned_projection_ids = tuple(
        item.projection_id for item in runtime.items if item.projection_id and item.status == STATE_MERGED
    )
    cleanup_allowed = (
        not rollback_required
        and runtime.batch_status in BATCH_CLEANUP_ALLOWED_STATES
    )
    cleanup_blockers = () if cleanup_allowed else tuple(item.task_id for item in retained_items)
    operator_actions = (
        (ACTION_ROLLBACK_BATCH, ACTION_REPLAY_THROUGH_MERGE_QUEUE, "approve_cleanup_after_replay")
        if rollback_required
        else ((ACTION_CLEANUP_RETAINED_BRANCH,) if cleanup_allowed else ("wait_for_batch_resolution",))
    )

    rollback_snapshot_id = runtime.rollback_snapshot_id or (
        f"snapshot-{_short_identity(rollback_target, 'rollback-target')}" if rollback_target else ""
    )
    rollback_projection_id = runtime.rollback_projection_id or (
        f"projection-{_short_identity(rollback_target, 'rollback-target')}" if rollback_target else ""
    )

    return BatchRollbackPlan(
        scenario_id=scenario_id,
        project_id=runtime.project_id,
        batch_id=runtime.batch_id,
        target_ref=runtime.target_ref,
        batch_status=batch_status,
        rollback_required=rollback_required,
        rollback_epoch=rollback_epoch,
        replay_epoch=replay_epoch,
        rollback_target_commit=rollback_target,
        rollback_snapshot_id=rollback_snapshot_id,
        rollback_projection_id=rollback_projection_id,
        abandoned_merge_commits=abandoned_merge_commits,
        abandoned_snapshot_ids=abandoned_snapshot_ids,
        abandoned_projection_ids=abandoned_projection_ids,
        retained_branch_refs=tuple(item.branch_ref for item in retained_items if item.branch_ref),
        retained_worktree_paths=tuple(item.worktree_path for item in retained_items if item.worktree_path),
        replay_task_ids=replay_task_ids,
        replay_merge_queue_items=replay_queue_items,
        cleanup_allowed=cleanup_allowed,
        cleanup_blockers=cleanup_blockers,
        operator_actions=operator_actions,
        dashboard_rows=_batch_dashboard_rows(
            runtime,
            rollback_required=rollback_required,
            rollback_epoch=rollback_epoch,
            replay_epoch=replay_epoch,
            replay_task_ids=replay_task_ids,
            cleanup_allowed=cleanup_allowed,
        ),
    )


def _merged_dependency_ids(tasks_by_id: dict[str, BranchRuntimeTask]) -> set[str]:
    return {
        task_id
        for task_id, task in tasks_by_id.items()
        if task.status in TERMINAL_NON_BLOCKING_STATES
    }


def _dependency_blockers(
    task: BranchRuntimeTask,
    *,
    merged_dependencies: set[str],
) -> tuple[str, ...]:
    return tuple(dep for dep in task.depends_on if dep not in merged_dependencies)


def _recovery_actions_for(action: str) -> tuple[str, ...]:
    if action == ACTION_OBSERVER_DECISION_REQUIRED:
        return ("fix_or_rebase", "abandon", "rollback_batch")
    if action == ACTION_RECLAIM_FROM_CHECKPOINT:
        return ("reclaim", "replay_from_checkpoint")
    if action == ACTION_RECLAIM_AFTER_DEPENDENCY:
        return ("wait_for_dependency", "reclaim", "replay_from_checkpoint")
    if action == ACTION_WAIT_FOR_DEPENDENCY:
        return ("wait_for_dependency", "revalidate_after_dependency")
    return ()


def decide_restart_recovery(
    tasks: list[BranchRuntimeTask],
    *,
    scenario_id: str = "PB-001",
) -> RecoveryPlan:
    """Compute observer recovery decisions after a service restart.

    The function is deterministic and performs no git, DB, graph, or semantic
    writes. It is the executable oracle for PB-001 until durable runtime tables
    exist.
    """
    tasks_by_id = {task.task_id: task for task in tasks}
    merged_dependencies = _merged_dependency_ids(tasks_by_id)
    decisions: list[RecoveryDecision] = []

    for task in tasks:
        blockers = _dependency_blockers(task, merged_dependencies=merged_dependencies)
        observed = task.status

        if task.status == STATE_MERGED:
            recovery_state = STATE_MERGED
            action = ACTION_LEAVE_MERGED
            cleanup_blocker = False
            graph_allowed = True
            semantic_allowed = True
        elif task.status == STATE_MERGE_FAILED:
            recovery_state = STATE_MERGE_FAILED
            action = ACTION_OBSERVER_DECISION_REQUIRED
            cleanup_blocker = True
            graph_allowed = False
            semantic_allowed = False
        elif task.status == STATE_RECLAIMABLE:
            recovery_state = STATE_RECLAIMABLE
            action = ACTION_RECLAIM_AFTER_DEPENDENCY if blockers else ACTION_RECLAIM_FROM_CHECKPOINT
            cleanup_blocker = True
            graph_allowed = False
            semantic_allowed = False
        elif task.status == STATE_RUNNING and task.lease_expired:
            recovery_state = STATE_RECLAIMABLE
            action = ACTION_RECLAIM_AFTER_DEPENDENCY if blockers else ACTION_RECLAIM_FROM_CHECKPOINT
            cleanup_blocker = True
            graph_allowed = False
            semantic_allowed = False
        elif blockers:
            recovery_state = STATE_DEPENDENCY_BLOCKED
            action = ACTION_WAIT_FOR_DEPENDENCY
            cleanup_blocker = True
            graph_allowed = False
            semantic_allowed = False
        elif task.status == STATE_QUEUED_FOR_MERGE:
            recovery_state = STATE_QUEUED_FOR_MERGE
            action = ACTION_NOOP
            cleanup_blocker = True
            graph_allowed = False
            semantic_allowed = False
        else:
            recovery_state = task.status or STATE_WAITING_DEPENDENCY
            action = ACTION_NOOP
            cleanup_blocker = task.status not in TERMINAL_NON_BLOCKING_STATES
            graph_allowed = task.status == STATE_MERGED
            semantic_allowed = task.status == STATE_MERGED

        decisions.append(
            RecoveryDecision(
                task_id=task.task_id,
                branch_ref=task.branch_ref,
                observed_state=observed,
                recovery_state=recovery_state,
                action=action,
                dependency_blockers=blockers,
                recovery_actions=_recovery_actions_for(action),
                checkpoint_id=task.checkpoint_id,
                replay_source=task.replay_source,
                cleanup_blocker=cleanup_blocker,
                target_graph_activation_allowed=graph_allowed,
                target_semantic_activation_allowed=semantic_allowed,
            )
        )

    target_graph_blocked = tuple(
        decision.task_id for decision in decisions if not decision.target_graph_activation_allowed
    )
    target_semantic_blocked = tuple(
        decision.task_id for decision in decisions if not decision.target_semantic_activation_allowed
    )
    retained = tuple(task.branch_ref for task in tasks if task.branch_ref)
    cleanup_allowed = not any(decision.cleanup_blocker for decision in decisions)

    return RecoveryPlan(
        scenario_id=scenario_id,
        decisions=tuple(decisions),
        cleanup_allowed=cleanup_allowed,
        retained_branch_refs=retained,
        target_graph_activation_blocked_for=target_graph_blocked,
        target_semantic_activation_blocked_for=target_semantic_blocked,
        dashboard_rows=tuple(decision.to_dashboard_row() for decision in decisions),
    )


def _limit_compact_rows(rows: list[dict[str, Any]], limit: int) -> tuple[tuple[dict[str, Any], ...], bool]:
    bounded_limit = max(0, int(limit))
    return tuple(rows[:bounded_limit]), len(rows) > bounded_limit


def _recovery_decisions_by_task(
    recovery_plan: RecoveryPlan | None,
) -> dict[str, RecoveryDecision]:
    if recovery_plan is None:
        return {}
    return {decision.task_id: decision for decision in recovery_plan.decisions}


def _branch_lane_graph_epoch(context: BranchTaskRuntimeContext) -> dict[str, str]:
    return {
        "snapshot_id": context.snapshot_id,
        "projection_id": context.projection_id,
        "base_commit": context.base_commit,
        "head_commit": context.head_commit,
        "target_head_commit": context.target_head_commit,
        "rollback_epoch": context.rollback_epoch,
        "replay_epoch": context.replay_epoch,
    }


def _compact_branch_lane(
    context: BranchTaskRuntimeContext,
    *,
    recovery_decision: RecoveryDecision | None,
) -> dict[str, Any]:
    recovery_actions = (
        recovery_decision.recovery_actions
        if recovery_decision is not None
        else ((context.last_recovery_action,) if context.last_recovery_action else ())
    )
    recovery_state = recovery_decision.recovery_state if recovery_decision else context.status
    dependency_blockers = recovery_decision.dependency_blockers if recovery_decision else context.depends_on
    return {
        "project_id": context.project_id,
        "batch_id": context.batch_id,
        "task_id": context.task_id,
        "backlog_id": context.backlog_id,
        "chain_id": context.chain_id,
        "stage_task_id": context.stage_task_id,
        "stage_type": context.stage_type,
        "branch_ref": context.branch_ref,
        "ref_name": context.ref_name,
        "worktree_id": context.worktree_id,
        "worktree_path": context.worktree_path,
        "status": recovery_state,
        "observed_status": context.status,
        "attempt": context.attempt,
        "lease_id": context.lease_id,
        "checkpoint_id": context.checkpoint_id,
        "replay_source": context.replay_source,
        "dependency_blockers": list(dependency_blockers),
        "recovery_actions": list(recovery_actions),
        "graph_epoch": _branch_lane_graph_epoch(context),
        "merge_queue_id": context.merge_queue_id,
        "merge_preview_id": context.merge_preview_id,
    }


def _compact_merge_queue(
    merge_queue_plan: MergeQueuePlan | None,
    *,
    limit: int,
) -> tuple[dict[str, Any], int, bool]:
    if merge_queue_plan is None:
        return {
            "scenario_id": "",
            "mergeable_task_ids": [],
            "blocked_task_ids": [],
            "stale_task_ids": [],
            "target_mutation_blocked_for": [],
            "rows": [],
        }, 0, False
    rows, truncated = _limit_compact_rows(list(merge_queue_plan.dashboard_rows), limit)
    return {
        "scenario_id": merge_queue_plan.scenario_id,
        "mergeable_task_ids": list(merge_queue_plan.mergeable_task_ids),
        "blocked_task_ids": list(merge_queue_plan.blocked_task_ids),
        "stale_task_ids": list(merge_queue_plan.stale_task_ids),
        "target_mutation_blocked_for": list(merge_queue_plan.target_mutation_blocked_for),
        "rows": list(rows),
    }, len(merge_queue_plan.dashboard_rows), truncated


def _compact_rollback(
    batch_plan: BatchRollbackPlan | None,
    *,
    limit: int,
) -> tuple[dict[str, Any], int, bool]:
    if batch_plan is None:
        return {
            "scenario_id": "",
            "batch_status": "",
            "rollback_required": False,
            "rollback_epoch": "",
            "replay_epoch": "",
            "retained_branch_refs": [],
            "cleanup_allowed": True,
            "cleanup_blockers": [],
            "operator_actions": [],
            "rows": [],
        }, 0, False
    rows, rows_truncated = _limit_compact_rows(list(batch_plan.dashboard_rows), limit)
    retained, retained_truncated = _limit_compact_rows(
        [{"branch_ref": branch_ref} for branch_ref in batch_plan.retained_branch_refs],
        limit,
    )
    rollback = {
        "scenario_id": batch_plan.scenario_id,
        "target_ref": batch_plan.target_ref,
        "batch_status": batch_plan.batch_status,
        "rollback_required": batch_plan.rollback_required,
        "rollback_epoch": batch_plan.rollback_epoch,
        "replay_epoch": batch_plan.replay_epoch,
        "rollback_target_commit": batch_plan.rollback_target_commit,
        "rollback_snapshot_id": batch_plan.rollback_snapshot_id,
        "rollback_projection_id": batch_plan.rollback_projection_id,
        "retained_branch_refs": [row["branch_ref"] for row in retained],
        "replay_task_ids": list(batch_plan.replay_task_ids),
        "cleanup_allowed": batch_plan.cleanup_allowed,
        "cleanup_blockers": list(batch_plan.cleanup_blockers),
        "operator_actions": list(batch_plan.operator_actions),
        "rows": list(rows),
    }
    return rollback, len(batch_plan.dashboard_rows), rows_truncated or retained_truncated


def build_parallel_branch_read_model(
    *,
    project_id: str,
    batch_id: str,
    contexts: list[BranchTaskRuntimeContext],
    recovery_plan: RecoveryPlan | None = None,
    merge_queue_plan: MergeQueuePlan | None = None,
    batch_plan: BatchRollbackPlan | None = None,
    limit: int = 50,
) -> ParallelBranchReadModel:
    """Build the bounded PB-010 operator view for dashboard and MCP clients.

    This function composes existing runtime decisions into a compact payload and
    deliberately avoids expanding backlog rows, graph nodes, or semantic payloads.
    """
    decisions_by_task = _recovery_decisions_by_task(recovery_plan)
    ordered_contexts = sorted(contexts, key=lambda ctx: (ctx.batch_id, ctx.task_id))
    lanes = [
        _compact_branch_lane(
            context,
            recovery_decision=decisions_by_task.get(context.task_id),
        )
        for context in ordered_contexts
        if context.project_id == project_id and (not batch_id or context.batch_id == batch_id)
    ]
    branch_lanes, lanes_truncated = _limit_compact_rows(lanes, limit)
    merge_queue, merge_queue_total, merge_queue_truncated = _compact_merge_queue(
        merge_queue_plan,
        limit=limit,
    )
    rollback, rollback_total, rollback_truncated = _compact_rollback(
        batch_plan,
        limit=limit,
    )

    status_counts: dict[str, int] = {}
    for lane in lanes:
        status = str(lane.get("status") or "")
        status_counts[status] = status_counts.get(status, 0) + 1

    truncated = {
        "branch_lanes": lanes_truncated,
        "merge_queue_rows": merge_queue_truncated,
        "rollback_rows": rollback_truncated,
    }
    total_counts = {
        "branch_lanes": len(lanes),
        "merge_queue_rows": merge_queue_total,
        "rollback_rows": rollback_total,
    }
    summary = {
        "lane_count": len(lanes),
        "status_counts": status_counts,
        "mergeable_count": len(merge_queue.get("mergeable_task_ids", [])),
        "blocked_count": len(merge_queue.get("blocked_task_ids", [])),
        "stale_count": len(merge_queue.get("stale_task_ids", [])),
        "rollback_required": bool(rollback.get("rollback_required")),
        "cleanup_allowed": bool(rollback.get("cleanup_allowed", True)),
        "truncated": any(truncated.values()),
    }

    return ParallelBranchReadModel(
        project_id=project_id,
        batch_id=batch_id,
        summary=summary,
        branch_lanes=branch_lanes,
        merge_queue=merge_queue,
        rollback=rollback,
        total_counts=total_counts,
        truncated=truncated,
    )


def build_parallel_branch_read_model_from_db(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    batch_id: str = "",
    merge_queue_id: str = "",
    target_ref: str = "",
    current_target_head: str = "",
    now_iso: str = "",
    limit: int = 50,
    scenario_id: str = "PB-010",
    severe_integration_failure: bool = False,
    corrected_replay_order: tuple[str, ...] = (),
) -> ParallelBranchReadModel:
    """Build PB-010 read model from durable branch, queue, and batch rows."""
    contexts = list_branch_contexts(conn, project_id, batch_id=batch_id)
    recovery_plan = (
        decide_restart_recovery(
            runtime_tasks_from_contexts(contexts, now_iso=now_iso),
            scenario_id=scenario_id,
        )
        if contexts
        else None
    )

    queue_plan: MergeQueuePlan | None = None
    queue_id = str(merge_queue_id or "").strip()
    if not queue_id:
        queue_ids = sorted({ctx.merge_queue_id for ctx in contexts if ctx.merge_queue_id})
        queue_id = queue_ids[0] if len(queue_ids) == 1 else ""
    if queue_id:
        queue_items = list_merge_queue_items(
            conn,
            project_id,
            queue_id,
            target_ref=target_ref,
        )
        latest_target = str(current_target_head or "").strip()
        if latest_target:
            queue_items = [
                replace(item, current_target_head=latest_target)
                if item.status in MERGE_READY_INPUT_STATES
                else item
                for item in queue_items
            ]
        queue_plan = decide_merge_queue(queue_items, scenario_id=scenario_id) if queue_items else None

    batch_plan: BatchRollbackPlan | None = None
    if batch_id:
        batch_runtime = get_batch_merge_runtime(conn, project_id, batch_id)
        if batch_runtime is not None:
            batch_plan = decide_batch_rollback_replay(
                batch_runtime,
                severe_integration_failure=severe_integration_failure,
                corrected_replay_order=corrected_replay_order,
                scenario_id=scenario_id,
            )

    return build_parallel_branch_read_model(
        project_id=project_id,
        batch_id=batch_id,
        contexts=contexts,
        recovery_plan=recovery_plan,
        merge_queue_plan=queue_plan,
        batch_plan=batch_plan,
        limit=limit,
    )
