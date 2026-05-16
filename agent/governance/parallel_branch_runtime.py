"""Helpers for parallel branch runtime persistence and recovery decisions.

The recovery oracle remains side-effect free; the store helpers persist only
runtime context needed to make observer recovery replay-ready after restart.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


PARALLEL_BRANCH_RUNTIME_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS parallel_branch_runtime_contexts (
    project_id        TEXT NOT NULL,
    task_id           TEXT NOT NULL,
    batch_id          TEXT NOT NULL DEFAULT '',
    backlog_id        TEXT NOT NULL DEFAULT '',
    chain_id          TEXT NOT NULL DEFAULT '',
    root_task_id      TEXT NOT NULL DEFAULT '',
    stage_task_id     TEXT NOT NULL DEFAULT '',
    stage_type        TEXT NOT NULL DEFAULT '',
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
"""

STATE_MERGED = "merged"
STATE_MERGE_FAILED = "merge_failed"
STATE_QUEUED_FOR_MERGE = "queued_for_merge"
STATE_RUNNING = "running"
STATE_RECLAIMABLE = "reclaimable"
STATE_DEPENDENCY_BLOCKED = "dependency_blocked"
STATE_WAITING_DEPENDENCY = "waiting_dependency"

ACTION_LEAVE_MERGED = "leave_merged"
ACTION_OBSERVER_DECISION_REQUIRED = "observer_decision_required"
ACTION_RECLAIM_FROM_CHECKPOINT = "reclaim_from_checkpoint"
ACTION_RECLAIM_AFTER_DEPENDENCY = "reclaim_after_dependency"
ACTION_WAIT_FOR_DEPENDENCY = "wait_for_dependency"
ACTION_NOOP = "noop"

TERMINAL_NON_BLOCKING_STATES = {"merged", "abandoned", "cleaned"}


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
    batch_id: str = ""
    backlog_id: str = ""
    chain_id: str = ""
    root_task_id: str = ""
    stage_task_id: str = ""
    stage_type: str = ""
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


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_branch_runtime_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(PARALLEL_BRANCH_RUNTIME_SCHEMA_SQL)


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


def _context_from_row(row: sqlite3.Row) -> BranchTaskRuntimeContext:
    return BranchTaskRuntimeContext(
        project_id=row["project_id"],
        task_id=row["task_id"],
        batch_id=row["batch_id"] or "",
        backlog_id=row["backlog_id"] or "",
        chain_id=row["chain_id"] or "",
        root_task_id=row["root_task_id"] or "",
        stage_task_id=row["stage_task_id"] or "",
        stage_type=row["stage_type"] or "",
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


def upsert_branch_context(
    conn: sqlite3.Connection,
    context: BranchTaskRuntimeContext,
    *,
    now_iso: str = "",
) -> BranchTaskRuntimeContext:
    ensure_branch_runtime_schema(conn)
    now = now_iso or utc_now()
    conn.execute(
        """
        INSERT INTO parallel_branch_runtime_contexts (
            project_id, task_id, batch_id, backlog_id, chain_id, root_task_id,
            stage_task_id, stage_type, agent_id, worker_id, attempt, lease_id,
            lease_expires_at, fence_token, branch_ref, ref_name, worktree_id,
            worktree_path, base_commit, head_commit, target_head_commit,
            snapshot_id, projection_id, merge_queue_id, merge_preview_id,
            rollback_epoch, replay_epoch, status, depends_on_json,
            checkpoint_id, replay_source, last_recovery_action,
            created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(project_id, task_id) DO UPDATE SET
            batch_id = excluded.batch_id,
            backlog_id = excluded.backlog_id,
            chain_id = excluded.chain_id,
            root_task_id = excluded.root_task_id,
            stage_task_id = excluded.stage_task_id,
            stage_type = excluded.stage_type,
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
            context.batch_id,
            context.backlog_id,
            context.chain_id,
            context.root_task_id,
            context.stage_task_id,
            context.stage_type,
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


def _require_current_fence(context: BranchTaskRuntimeContext, fence_token: str) -> None:
    if context.fence_token and fence_token != context.fence_token:
        raise BranchRuntimeFenceError("Fence token mismatch: branch context was reclaimed")


def record_branch_checkpoint(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    task_id: str,
    checkpoint_id: str,
    fence_token: str,
    replay_source: str = "checkpoint",
    now_iso: str = "",
) -> BranchTaskRuntimeContext:
    ensure_branch_runtime_schema(conn)
    context = get_branch_context(conn, project_id, task_id)
    if context is None:
        raise KeyError(f"branch runtime context not found: {project_id}/{task_id}")
    _require_current_fence(context, fence_token)
    now = now_iso or utc_now()
    conn.execute(
        """
        UPDATE parallel_branch_runtime_contexts
        SET checkpoint_id = ?, replay_source = ?, updated_at = ?
        WHERE project_id = ? AND task_id = ?
        """,
        (checkpoint_id, replay_source, now, project_id, task_id),
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
