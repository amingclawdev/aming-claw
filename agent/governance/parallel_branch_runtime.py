"""Pure-state helpers for parallel branch runtime recovery decisions.

The first implementation slice is intentionally side-effect free. It lets the
PB-001 dry-run scenario become executable before the durable DB schema exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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


@dataclass(frozen=True)
class BranchRuntimeTask:
    task_id: str
    branch_ref: str
    status: str
    depends_on: tuple[str, ...] = ()
    lease_expired: bool = False
    checkpoint_id: str = ""
    merge_epoch: str = ""


@dataclass(frozen=True)
class RecoveryDecision:
    task_id: str
    branch_ref: str
    observed_state: str
    recovery_state: str
    action: str
    dependency_blockers: tuple[str, ...] = ()
    recovery_actions: tuple[str, ...] = ()
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
