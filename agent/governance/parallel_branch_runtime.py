"""Helpers for parallel branch runtime persistence and recovery decisions.

The recovery oracle remains side-effect free; the store helpers persist only
runtime context needed to make observer recovery replay-ready after restart.
"""

from __future__ import annotations

import copy
import json
import hashlib
import secrets
import sqlite3
import subprocess
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .worker_transcript_verify import verify_worker_transcript


PARALLEL_BRANCH_RUNTIME_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS parallel_branch_runtime_contexts (
    project_id        TEXT NOT NULL,
    task_id           TEXT NOT NULL,
    runtime_context_id TEXT NOT NULL DEFAULT '',
    batch_id          TEXT NOT NULL DEFAULT '',
    backlog_id        TEXT NOT NULL DEFAULT '',
    parent_task_id    TEXT NOT NULL DEFAULT '',
    chain_id          TEXT NOT NULL DEFAULT '',
    root_task_id      TEXT NOT NULL DEFAULT '',
    stage_task_id     TEXT NOT NULL DEFAULT '',
    stage_type        TEXT NOT NULL DEFAULT '',
    retry_round       INTEGER NOT NULL DEFAULT 0,
    agent_id          TEXT NOT NULL DEFAULT '',
    worker_id         TEXT NOT NULL DEFAULT '',
    allocation_owner  TEXT NOT NULL DEFAULT '',
    worker_slot_id    TEXT NOT NULL DEFAULT '',
    actual_host_worker_id TEXT NOT NULL DEFAULT '',
    host_startup_id   TEXT NOT NULL DEFAULT '',
    host_session_id   TEXT NOT NULL DEFAULT '',
    governance_project_id TEXT NOT NULL DEFAULT '',
    target_project_id TEXT NOT NULL DEFAULT '',
    target_project_root TEXT NOT NULL DEFAULT '',
    target_files_json TEXT NOT NULL DEFAULT '[]',
    owned_files_json  TEXT NOT NULL DEFAULT '[]',
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
    session_token_hash TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS parallel_branch_runtime_access_audit (
    audit_id          TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL,
    runtime_context_id TEXT NOT NULL,
    task_id           TEXT NOT NULL DEFAULT '',
    principal_id      TEXT NOT NULL DEFAULT '',
    session_id        TEXT NOT NULL DEFAULT '',
    role              TEXT NOT NULL DEFAULT '',
    view_name         TEXT NOT NULL DEFAULT '',
    decision          TEXT NOT NULL DEFAULT '',
    reason            TEXT NOT NULL DEFAULT '',
    projection_hash   TEXT NOT NULL DEFAULT '',
    nodes_read_json   TEXT NOT NULL DEFAULT '[]',
    metadata_json     TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_parallel_branch_runtime_access_context
  ON parallel_branch_runtime_access_audit(project_id, runtime_context_id, created_at);
CREATE INDEX IF NOT EXISTS idx_parallel_branch_runtime_access_role
  ON parallel_branch_runtime_access_audit(project_id, role, created_at);

CREATE TABLE IF NOT EXISTS parallel_branch_merge_queue_items (
    project_id        TEXT NOT NULL,
    merge_queue_id    TEXT NOT NULL,
    queue_item_id     TEXT NOT NULL,
    backlog_id        TEXT NOT NULL DEFAULT '',
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
FAILED_QA_REVISION_REJOIN_STATES = {
    STATE_VALIDATED,
    STATE_MERGE_READY,
}
MF_SUBAGENT_SESSION_REISSUE_DEFAULT_TTL_SECONDS = 3600
MF_SUBAGENT_SESSION_REISSUE_MAX_TTL_SECONDS = 28800
MF_SUBAGENT_SESSION_REISSUE_MIN_TTL_SECONDS = 60
MF_SUBAGENT_STARTUP_GATE_SCHEMA_VERSION = "mf_subagent_startup_gate.v1"
MF_SUBAGENT_STARTUP_REFUSAL_SCHEMA_VERSION = "mf_subagent_startup_refusal.v1"
MF_SUBAGENT_HOST_ADAPTER_IDENTITY_SCHEMA_VERSION = (
    "mf_subagent_host_adapter_spawn_identity.v1"
)
RUNTIME_CONTEXT_PROJECTION_SCHEMA_VERSION = "runtime_context.projection.v1"
RUNTIME_CONTEXT_CURRENT_SCHEMA_VERSION = "runtime_context.current.v1"
RUNTIME_CONTEXT_GATE_INPUTS_SCHEMA_VERSION = "runtime_context.gate_inputs.v1"
RUNTIME_CONTEXT_WORKER_VIEW_SCHEMA_VERSION = "runtime_context.worker_view.v1"
RUNTIME_CONTEXT_CLOSE_GATE_VIEW_SCHEMA_VERSION = "runtime_context.close_gate_view.v1"
RUNTIME_CONTEXT_ACTION_PLAN_SCHEMA_VERSION = "runtime_context.action_plan.v1"
RUNTIME_CONTEXT_GATE_PROJECTION_SCHEMA_VERSION = "runtime_context.gate_projection.v1"
RUNTIME_CONTEXT_TIMELINE_GATE_PROJECTION_SCHEMA_VERSION = (
    "runtime_context.timeline_gate_projection.v1"
)
RUNTIME_CONTEXT_CONTROL_PLANE_SCHEMA_VERSION = "runtime_context.control_plane.v1"
RUNTIME_CONTEXT_CAPABILITY_BOUNDARY_SCHEMA_VERSION = (
    "runtime_context.capability_boundary.v1"
)
RUNTIME_CONTEXT_WORKER_EXECUTION_SAFETY_SCHEMA_VERSION = (
    "runtime_context.worker_execution_safety.v1"
)
RUNTIME_CONTEXT_ROLE_FILTER_POLICY_SCHEMA_VERSION = "runtime_context.role_filter_policy.v1"
RUNTIME_CONTEXT_CONTENT_ADDRESS_SCHEMA_VERSION = "runtime_context.content_address.v1"
RUNTIME_CONTEXT_ACCESS_AUDIT_SCHEMA_VERSION = "runtime_context.access_audit.v1"
RUNTIME_CONTEXT_LANE_FOLD_SCHEMA_VERSION = "runtime_context.lane_fold.v1"
RUNTIME_CONTEXT_WORKER_ROLE = "mf_sub"
MERGE_DONE_STATES = {STATE_MERGED}
MERGE_BLOCKING_STATES = {STATE_MERGE_FAILED, STATE_ABANDONED, STATE_ROLLBACK_REQUIRED}
MERGE_REVALIDATION_BLOCKING_STATES = {
    STATE_RUNNING,
    STATE_WAITING_DEPENDENCY,
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
_MERGE_QUEUE_STATUS_ALIASES = {
    "ready_for_merge": STATE_QUEUED_FOR_MERGE,
}


def _normalize_merge_queue_status(status: str) -> str:
    value = str(status or "").strip()
    if not value:
        return STATE_QUEUED_FOR_MERGE
    return _MERGE_QUEUE_STATUS_ALIASES.get(value, value)


RUNTIME_CONTEXT_DEFAULT_LANE_CLAUSES = (
    "route_context",
    "route_action_precheck",
    "bounded_implementation_worker_dispatch",
    "mf_subagent_startup",
    "runtime_context_read_receipt",
    "independent_verification",
    "close_ready",
)
_RUNTIME_CONTEXT_EVENT_KIND_CLAUSES: dict[str, tuple[str, ...]] = {
    "route_context": ("route_context",),
    "observer_root_route_context": ("route_context",),
    "runtime_context_route_context": ("route_context",),
    "route_action_precheck": ("route_action_precheck",),
    "bounded_implementation_worker_dispatch": (
        "bounded_implementation_worker_dispatch",
    ),
    "mf_subagent_dispatch": ("bounded_implementation_worker_dispatch",),
    "worker_dispatch": ("bounded_implementation_worker_dispatch",),
    "mf_subagent_startup": ("mf_subagent_startup",),
    "mf_subagent_startup_gate": ("mf_subagent_startup",),
    "worker_startup": ("mf_subagent_startup",),
    "mf_subagent_read_receipt": ("runtime_context_read_receipt",),
    "runtime_context_read_receipt": ("runtime_context_read_receipt",),
    "worker_read_receipt": ("runtime_context_read_receipt",),
    "read_receipt": ("runtime_context_read_receipt",),
    "independent_verification": ("independent_verification",),
    "verification": ("independent_verification",),
    "qa_verification": ("independent_verification",),
    "finish_gate": ("finish_gate",),
    "mf_subagent_finish_gate": ("finish_gate",),
    "close_ready": ("close_ready",),
}
_RUNTIME_CONTEXT_FULFILLING_STATUSES = {
    "",
    "accepted",
    "complete",
    "ok",
    "pass",
    "passed",
    "ready",
    "recorded",
    "resolved",
    "satisfied",
    "success",
    "succeeded",
    "valid",
    "verified",
}
_RUNTIME_CONTEXT_BLOCKING_STATUSES = {
    "blocked",
    "error",
    "fail",
    "failed",
    "invalid",
    "rejected",
}
_RUNTIME_CONTEXT_QA_EVENT_KINDS = {
    "independent_verification",
    "qa_review",
    "qa_verification",
    "verification",
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


def mf_subagent_session_token_hash(session_token: str) -> str:
    token = str(session_token or "").strip()
    if not token:
        return ""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def runtime_context_secret_hash(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def mf_subagent_session_token_ref(
    *,
    project_id: str,
    runtime_context_id: str,
    task_id: str,
    fence_token_hash: str,
    session_token_hash: str,
    worker_slot_id: str = "",
) -> str:
    """Return a copy-safe bearer ref for a stored mf_sub session token hash."""

    session_hash = str(session_token_hash or "").strip()
    runtime_id = str(runtime_context_id or "").strip()
    task = str(task_id or "").strip()
    fence_hash = str(fence_token_hash or "").strip()
    if not session_hash or not runtime_id or not task or not fence_hash:
        return ""
    seed = "|".join(
        str(value or "").strip()
        for value in (
            "mf_subagent_session_token_ref.v1",
            project_id,
            runtime_id,
            task,
            fence_hash,
            session_hash,
            worker_slot_id,
        )
    )
    return "wstok-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:40]


def runtime_context_session_token_ref(
    context: "BranchTaskRuntimeContext",
    *,
    session_token_hash: str = "",
) -> str:
    token_hash = str(session_token_hash or context.session_token_hash or "").strip()
    return mf_subagent_session_token_ref(
        project_id=context.project_id,
        runtime_context_id=runtime_context_id_for_branch_context(context),
        task_id=context.task_id,
        fence_token_hash=runtime_context_secret_hash(context.fence_token),
        session_token_hash=token_hash,
        worker_slot_id=context.worker_slot_id or context.worker_id,
    )


def runtime_context_session_token_ref_matches(
    context: "BranchTaskRuntimeContext",
    presented_ref: str,
) -> bool:
    expected = runtime_context_session_token_ref(context)
    candidate = str(presented_ref or "").strip()
    return bool(expected and candidate and secrets.compare_digest(expected, candidate))


def issue_mf_subagent_session_token(
    context: "BranchTaskRuntimeContext",
) -> dict[str, Any]:
    """Issue an opaque worker token scoped by the stored runtime context hash."""
    token = secrets.token_urlsafe(32)
    token_hash = mf_subagent_session_token_hash(token)
    fence_hash = ""
    if context.fence_token:
        fence_hash = hashlib.sha256(
            str(context.fence_token or "").encode("utf-8")
        ).hexdigest()[:16]
    runtime_context_id = runtime_context_id_for_branch_context(context)
    token_ref = mf_subagent_session_token_ref(
        project_id=context.project_id,
        runtime_context_id=runtime_context_id,
        task_id=context.task_id,
        fence_token_hash=runtime_context_secret_hash(context.fence_token),
        session_token_hash=token_hash,
        worker_slot_id=context.worker_slot_id or context.worker_id,
    )
    return {
        "schema_version": "mf_subagent_same_owner_session_token.v1",
        "issued": True,
        "token_type": "same_owner_scoped",
        "session_token": token,
        "session_token_ref": token_ref,
        "session_token_hash": token_hash,
        "session_token_persisted": False,
        "session_token_ref_persisted": False,
        "scope": {
            "project_id": context.project_id,
            "task_id": context.task_id,
            "runtime_context_id": runtime_context_id,
            "fence_token_hash": fence_hash,
            "agent_id": context.agent_id,
            "allocation_owner": context.allocation_owner or context.agent_id,
            "worker_slot_id": context.worker_slot_id or context.worker_id,
            "backlog_id": context.backlog_id,
        },
        "delivery": "worker_host_envelope",
        "copy_safe_delivery": "runtime_context_session_token_ref",
        "raw_token_persistence": "not_persisted",
    }


def _runtime_context_parse_utc(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _runtime_context_now_dt(now_iso: str = "") -> datetime:
    return _runtime_context_parse_utc(now_iso) or datetime.now(timezone.utc)


def _runtime_context_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def runtime_context_session_token_lease_view(
    context: "BranchTaskRuntimeContext",
    *,
    now_iso: str = "",
) -> dict[str, Any]:
    now_dt = _runtime_context_now_dt(now_iso)
    expires_dt = _runtime_context_parse_utc(context.lease_expires_at)
    remaining: int | None = None
    expired = False
    if expires_dt is not None:
        remaining = int((expires_dt - now_dt).total_seconds())
        expired = remaining <= 0
    has_lease = bool(context.lease_id or context.lease_expires_at)
    renewal_supported = bool(
        context.status in ACTIVE_MF_SUBAGENT_GRAPH_QUERY_STATES
        and context.session_token_hash
        and context.fence_token
    )
    return {
        "schema_version": "mf_subagent_runtime_session_token_lease.v1",
        "has_lease": has_lease,
        "lease_id": context.lease_id,
        "lease_expires_at": context.lease_expires_at,
        "lease_remaining_ttl_seconds": (
            max(0, remaining) if remaining is not None else None
        ),
        "expired": expired,
        "status": (
            "expired"
            if expired
            else ("active" if has_lease else "no_lease_recorded")
        ),
        "renewal_supported": renewal_supported,
        "renewal_max_ttl_seconds": MF_SUBAGENT_SESSION_REISSUE_MAX_TTL_SECONDS,
        "renewal_default_ttl_seconds": MF_SUBAGENT_SESSION_REISSUE_DEFAULT_TTL_SECONDS,
        "renewal_endpoint": (
            "/api/graph-governance/{project_id}/runtime-contexts/"
            "{runtime_context_id}/session-token/reissue"
        ),
        "session_token_ref": runtime_context_session_token_ref(context),
        "session_token_ref_available": bool(runtime_context_session_token_ref(context)),
        "raw_session_token_exposed": False,
        "raw_session_token_persisted": False,
        "now": _runtime_context_iso(now_dt),
    }


def _runtime_session_token_expired_error(
    context: "BranchTaskRuntimeContext",
    *,
    runtime_context_id: str = "",
    now_iso: str = "",
) -> BranchRuntimeFenceError:
    runtime_id = runtime_context_id or runtime_context_id_for_branch_context(context)
    lease = runtime_context_session_token_lease_view(context, now_iso=now_iso)
    return _graph_query_fence_error(
        "runtime_session_token_expired",
        details={
            "runtime_context_id": runtime_id,
            "task_id": context.task_id,
            "parent_task_id": _parent_task_id_for_context(context),
            "reason": "runtime_session_token_expired",
            "session_token_lease": lease,
            "renewal": {
                "schema_version": "mf_subagent_session_token_reissue_hint.v1",
                "available": bool(lease.get("renewal_supported")),
                "method": "POST",
                "path": (
                    "/api/graph-governance/{project_id}/runtime-contexts/"
                    "{runtime_context_id}/session-token/reissue"
                ),
                "required_body_fields": [
                    "runtime_context_id",
                    "task_id",
                    "parent_task_id",
                    "fence_token",
                    "session_token",
                    "target_project_root",
                ],
                "max_ttl_seconds": MF_SUBAGENT_SESSION_REISSUE_MAX_TTL_SECONDS,
                "raw_session_token_persisted": False,
                "fail_closed_for": [
                    "stale_context",
                    "closed_context",
                    "scope_mismatch",
                    "wrong_fence",
                    "wrong_session_token",
                ],
            },
        },
    )


def _bounded_reissue_ttl_seconds(value: Any) -> int:
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = MF_SUBAGENT_SESSION_REISSUE_DEFAULT_TTL_SECONDS
    return max(
        MF_SUBAGENT_SESSION_REISSUE_MIN_TTL_SECONDS,
        min(requested, MF_SUBAGENT_SESSION_REISSUE_MAX_TTL_SECONDS),
    )


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
    parent_task_id: str = ""
    chain_id: str = ""
    root_task_id: str = ""
    stage_task_id: str = ""
    stage_type: str = ""
    retry_round: int = 0
    agent_id: str = ""
    worker_id: str = ""
    allocation_owner: str = ""
    worker_slot_id: str = ""
    actual_host_worker_id: str = ""
    host_startup_id: str = ""
    host_session_id: str = ""
    governance_project_id: str = ""
    target_project_id: str = ""
    target_project_root: str = ""
    target_files: tuple[str, ...] = ()
    owned_files: tuple[str, ...] = ()
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
    session_token_hash: str = ""
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
class RuntimeContextMissingField:
    gate: str
    field: str
    expected_source: str
    producer: str
    consumer: str
    evidence_ref: str = ""
    evidence_refs_inspected: tuple[str, ...] = ()
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence_refs_inspected"] = list(self.evidence_refs_inspected)
        if not payload["message"]:
            payload["message"] = (
                f"{self.gate} gate missing {self.field} from {self.expected_source}"
            )
        return payload


@dataclass(frozen=True)
class RuntimeContextProjection:
    project_id: str
    runtime_context_id: str
    current: dict[str, Any]
    gate_inputs: dict[str, Any]
    action_plan: dict[str, Any]
    gate_projection: dict[str, Any]
    control_plane: dict[str, Any]
    capability_boundary: dict[str, Any]
    worker_view: dict[str, Any]
    close_gate_view: dict[str, Any]
    observer_view: dict[str, Any] = field(default_factory=dict)
    qa_view: dict[str, Any] = field(default_factory=dict)
    judge_view: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        views = {
            "current": self.current,
            "gate_inputs": self.gate_inputs,
            "action_plan": self.action_plan,
            "gate_projection": self.gate_projection,
            "control_plane": self.control_plane,
            "capability_boundary": self.capability_boundary,
            "worker_view": self.worker_view,
            "close_gate_view": self.close_gate_view,
        }
        if self.observer_view:
            views["observer_view"] = self.observer_view
        if self.qa_view:
            views["qa_view"] = self.qa_view
        if self.judge_view:
            views["judge_view"] = self.judge_view
        source_policy = {
            "immutable_sources_remain_owner_owned": True,
            "raw_private_context_exposed": False,
            "worker_views_are_role_filtered": True,
        }
        return {
            "schema_version": RUNTIME_CONTEXT_PROJECTION_SCHEMA_VERSION,
            "project_id": self.project_id,
            "runtime_context_id": self.runtime_context_id,
            "next_required_evidence": list(
                self.action_plan.get("next_required_evidence") or []
            ),
            "views": views,
            "source_policy": source_policy,
            "content_address": runtime_context_projection_content_address(
                project_id=self.project_id,
                runtime_context_id=self.runtime_context_id,
                views=views,
                source_policy=source_policy,
            ),
        }


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
    backlog_id: str = ""
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


def _stable_plan_id(prefix: str, *parts: object) -> str:
    payload = json.dumps([str(part or "") for part in parts], sort_keys=True)
    return f"{prefix}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def _normalise_batch_path(path: object) -> str:
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _batch_string_list(value: object) -> tuple[str, ...]:
    raw: object = value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                raw = value
    if isinstance(raw, (list, tuple, set)):
        items = raw
    elif raw:
        items = [raw]
    else:
        items = []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        path = _normalise_batch_path(item)
        if not path or path in seen:
            continue
        seen.add(path)
        result.append(path)
    return tuple(result)


def _batch_priority_rank(priority: object) -> float:
    text = str(priority or "").strip().upper()
    if not text:
        return 50.0
    if text.startswith("P"):
        text = text[1:]
    try:
        return float(text)
    except ValueError:
        return 50.0


def _batch_status_actionable(status: object) -> bool:
    text = str(status or "").strip().upper()
    if not text:
        return False
    return text not in {
        "CANCELLED",
        "CLOSED",
        "FIXED",
        "MERGED",
        "REJECTED",
        "SUPERSEDED",
        "VOID",
    }


def _batch_row_id(row: Mapping[str, Any]) -> str:
    return str(row.get("backlog_id") or row.get("bug_id") or "").strip()


def _batch_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    left_files = set(_batch_string_list(left.get("target_files")))
    right_files = set(_batch_string_list(right.get("target_files")))
    left_tests = set(_batch_string_list(left.get("test_files")))
    right_tests = set(_batch_string_list(right.get("test_files")))
    shared_files = sorted(left_files & right_files)
    shared_tests = sorted(left_tests & right_tests)
    categories: list[str] = []
    if shared_files:
        categories.append("target_files")
    if shared_tests:
        categories.append("test_files")
    return {
        "left": _batch_row_id(left),
        "right": _batch_row_id(right),
        "categories": categories,
        "shared_files": shared_files,
        "shared_tests": shared_tests,
        "overlaps": bool(categories),
    }


def plan_mf_batch_parallel_preflight(
    *,
    project_id: str,
    coordination_backlog_id: str,
    backlog_rows: Sequence[Mapping[str, Any]],
    batch_id: str,
    merge_queue_id: str,
    target_head_commit: str,
    snapshot_id: str = "",
    target_ref: str = "refs/heads/main",
    mode: str = "parallel",
    duplicate_backlog_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Plan a multi-backlog fanout gate without persisting merge queue rows."""
    coordination_id = str(coordination_backlog_id or "").strip()
    normalized_mode = str(mode or "parallel").strip() or "parallel"
    blockers: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    operator_index: dict[str, int] = {}
    for index, row in enumerate(backlog_rows):
        row_id = _batch_row_id(row)
        if row_id and row_id not in operator_index:
            operator_index[row_id] = index
        if not row_id:
            blockers.append({"code": "missing_backlog_id", "severity": "block"})
            continue
        if row_id in seen:
            blockers.append(
                {
                    "code": "duplicate_child_backlog",
                    "backlog_id": row_id,
                    "severity": "block",
                }
            )
            continue
        seen.add(row_id)
        target_files = _batch_string_list(row.get("target_files"))
        test_files = _batch_string_list(row.get("test_files"))
        status = str(row.get("status") or "").strip()
        if bool(row.get("missing")):
            blockers.append(
                {"code": "missing_child_row", "backlog_id": row_id, "severity": "block"}
            )
        if row_id == coordination_id:
            blockers.append(
                {
                    "code": "child_matches_coordination_row",
                    "backlog_id": row_id,
                    "severity": "block",
                }
            )
        if not _batch_status_actionable(status):
            blockers.append(
                {
                    "code": "child_not_actionable",
                    "backlog_id": row_id,
                    "status": status,
                    "severity": "block",
                }
            )
        if not target_files:
            blockers.append(
                {"code": "missing_target_files", "backlog_id": row_id, "severity": "block"}
            )
        rows.append(
            {
                "backlog_id": row_id,
                "status": status,
                "priority": str(row.get("priority") or ""),
                "priority_rank": _batch_priority_rank(row.get("priority")),
                "operator_index": operator_index[row_id],
                "target_files": list(target_files),
                "test_files": list(test_files),
            }
        )
    for row_id in duplicate_backlog_ids:
        if row_id:
            blockers.append(
                {
                    "code": "duplicate_child_backlog",
                    "backlog_id": str(row_id),
                    "severity": "block",
                }
            )
    if not str(target_head_commit or "").strip():
        blockers.append(
            {
                "code": "missing_target_head_commit",
                "severity": "block",
                "message": "target_head_commit is required before fanout_ready",
            }
        )
    ordered_rows = sorted(
        rows,
        key=lambda row: (
            float(row["priority_rank"]),
            int(row["operator_index"]),
            row["backlog_id"],
        ),
    )
    overlap_groups: list[dict[str, Any]] = []
    overlap_by_row: dict[str, set[str]] = {row["backlog_id"]: set() for row in ordered_rows}
    for left_index, left in enumerate(ordered_rows):
        for right in ordered_rows[left_index + 1 :]:
            overlap = _batch_overlap(left, right)
            if overlap["overlaps"]:
                overlap_groups.append(overlap)
                overlap_by_row[left["backlog_id"]].add(right["backlog_id"])
                overlap_by_row[right["backlog_id"]].add(left["backlog_id"])

    dispatch_groups: list[dict[str, Any]] = []
    row_order = {row["backlog_id"]: index for index, row in enumerate(ordered_rows)}
    if normalized_mode == "strict_ordered":
        for index, row in enumerate(ordered_rows):
            dispatch_groups.append(
                {
                    "group_index": index + 1,
                    "backlog_ids": [row["backlog_id"]],
                    "reason": "strict_ordered",
                }
            )
    else:
        visited: set[str] = set()
        for row in ordered_rows:
            row_id = row["backlog_id"]
            if row_id in visited:
                continue
            component: list[str] = []
            stack = [row_id]
            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                component.append(current)
                for neighbor in sorted(
                    overlap_by_row.get(current, set()),
                    key=lambda value: row_order.get(value, len(row_order)),
                    reverse=True,
                ):
                    if neighbor not in visited:
                        stack.append(neighbor)
            component.sort(key=lambda value: row_order.get(value, len(row_order)))
            dispatch_groups.append(
                {
                    "group_index": len(dispatch_groups) + 1,
                    "backlog_ids": component,
                    "reason": (
                        "connected_overlap_component"
                        if len(component) > 1
                        else "no_overlap"
                    ),
                    "overlap_component": len(component) > 1,
                }
            )

    planned_items: list[dict[str, Any]] = []
    previous_task_id = ""
    task_id_by_row: dict[str, str] = {}
    for index, row in enumerate(ordered_rows):
        task_id = f"{batch_id}:row:{index + 1}"
        task_id_by_row[row["backlog_id"]] = task_id
    for index, row in enumerate(ordered_rows):
        row_id = row["backlog_id"]
        task_id = task_id_by_row[row_id]
        overlap_predecessors = [
            task_id_by_row[other["backlog_id"]]
            for other in ordered_rows[:index]
            if other["backlog_id"] in overlap_by_row[row_id]
        ]
        strict_predecessors = (
            [previous_task_id]
            if normalized_mode == "strict_ordered" and previous_task_id
            else []
        )
        dependencies = tuple(
            dict.fromkeys([*strict_predecessors, *overlap_predecessors])
        )
        queue_item_id = _stable_plan_id("mqitem", project_id, merge_queue_id, task_id, row_id)
        planned_items.append(
            {
                "schema_version": "mf_batch_parallel.planned_merge_queue_item.v1",
                "project_id": project_id,
                "merge_queue_id": merge_queue_id,
                "queue_item_id": queue_item_id,
                "queue_index": index + 1,
                "backlog_id": row_id,
                "task_id": task_id,
                "owned_files": list(row["target_files"]),
                "target_ref": target_ref,
                "status": "planned",
                "durable_status_after_worker_finish": STATE_QUEUED_FOR_MERGE,
                "depends_on": list(dependencies),
                "hard_depends_on": list(dependencies),
                "serializes_after": list(dependencies),
                "conflicts_with": list(overlap_predecessors),
                "same_node_or_file_conflicts": list(overlap_predecessors),
                "requires_graph_epoch": list(dependencies),
                "target_head_commit": str(target_head_commit or "").strip(),
                "snapshot_id": snapshot_id,
            }
        )
        previous_task_id = task_id

    plan_body = {
        "schema_version": "mf_batch_parallel.preflight_gate.v1",
        "status": "blocked" if blockers else "passed",
        "fanout_ready": not blockers,
        "mode": normalized_mode,
        "project_id": project_id,
        "coordination_backlog_id": coordination_id,
        "batch_id": batch_id,
        "merge_queue_id": merge_queue_id,
        "target_ref": target_ref,
        "target_head_commit": str(target_head_commit or "").strip(),
        "snapshot_id": snapshot_id,
        "blockers": blockers,
        "ordered_rows": [
            {
                "backlog_id": row["backlog_id"],
                "priority": row["priority"],
                "priority_rank": row["priority_rank"],
                "operator_index": row["operator_index"],
                "target_files": row["target_files"],
                "test_files": row["test_files"],
            }
            for row in ordered_rows
        ],
        "overlap_groups": overlap_groups,
        "dispatch_groups": dispatch_groups,
        "merge_queue_plan": {
            "schema_version": "mf_batch_parallel.merge_queue_plan.v1",
            "planner_only": True,
            "durable_queue_write": False,
            "source_of_authority": "parallel_branch_runtime.plan_mf_batch_parallel_preflight",
            "merge_queue_id": merge_queue_id,
            "planned_items": planned_items,
        },
    }
    plan_body["preflight_id"] = _stable_plan_id(
        "mfbatch-preflight",
        project_id,
        coordination_id,
        batch_id,
        merge_queue_id,
        plan_body["ordered_rows"],
        plan_body["overlap_groups"],
        plan_body["blockers"],
    )
    plan_body["preflight_hash"] = "sha256:" + hashlib.sha256(
        json.dumps(plan_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return plan_body


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
        parent_task_id=planned.parent_task_id or existing.parent_task_id,
        chain_id=planned.chain_id or existing.chain_id,
        root_task_id=planned.root_task_id or existing.root_task_id,
        stage_task_id=planned.stage_task_id or existing.stage_task_id,
        stage_type=planned.stage_type or existing.stage_type,
        retry_round=planned.retry_round or existing.retry_round,
        agent_id=existing.agent_id or planned.agent_id,
        worker_id=existing.worker_id or planned.worker_id,
        allocation_owner=existing.allocation_owner or planned.allocation_owner,
        worker_slot_id=existing.worker_slot_id or planned.worker_slot_id,
        actual_host_worker_id=existing.actual_host_worker_id
        or planned.actual_host_worker_id,
        host_startup_id=existing.host_startup_id or planned.host_startup_id,
        host_session_id=existing.host_session_id or planned.host_session_id,
        governance_project_id=existing.governance_project_id
        or planned.governance_project_id,
        target_project_id=existing.target_project_id or planned.target_project_id,
        target_project_root=existing.target_project_root or planned.target_project_root,
        target_files=planned.target_files or existing.target_files,
        owned_files=planned.owned_files or existing.owned_files,
        attempt=existing.attempt or planned.attempt,
        lease_id=existing.lease_id or planned.lease_id,
        lease_expires_at=existing.lease_expires_at or planned.lease_expires_at,
        fence_token=existing.fence_token or planned.fence_token,
        ref_name=existing.ref_name or planned.ref_name,
        branch_ref=existing.branch_ref or planned.branch_ref,
        worktree_id=existing.worktree_id or planned.worktree_id,
        worktree_path=existing.worktree_path or planned.worktree_path,
        base_commit=existing.base_commit or planned.base_commit,
        head_commit=existing.head_commit or planned.head_commit,
        target_head_commit=existing.target_head_commit or planned.target_head_commit,
        snapshot_id=existing.snapshot_id or planned.snapshot_id,
        projection_id=existing.projection_id or planned.projection_id,
        merge_preview_id=existing.merge_preview_id or planned.merge_preview_id,
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
        columns.add("retry_round")
    for column in (
        "parent_task_id",
        "allocation_owner",
        "worker_slot_id",
        "actual_host_worker_id",
        "host_startup_id",
        "host_session_id",
        "governance_project_id",
        "target_project_id",
        "target_project_root",
        "target_files_json",
        "owned_files_json",
        "merge_queue_id",
        "merge_preview_id",
        "session_token_hash",
    ):
        if column not in columns:
            conn.execute(
                "ALTER TABLE parallel_branch_runtime_contexts "
                f"ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
            )
            columns.add(column)
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
            "backlog_id",
            "ALTER TABLE parallel_branch_merge_queue_items "
            "ADD COLUMN backlog_id TEXT NOT NULL DEFAULT ''",
        ),
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
    "fence_token",
    "hidden_context",
    "launch_text",
    "observer_only_context",
    "private_founder",
    "private_memory",
    "private_context",
    "private_route_body",
    "raw_context_body",
    "raw_fence_token",
    "raw_launch_text",
    "raw_memory",
    "raw_private_memory",
    "raw_private_context",
    "raw_private_context_body",
    "raw_private_route_body",
    "raw_route_body",
    "raw_session_token",
    "raw_subtree",
    "raw_worker_nonce",
    "session_token",
    "subtree",
    "unmanifested_prompt_text",
    "worker_nonce",
    "worker_session_token",
}

_PRIVATE_CONTRACT_REVISION_KEY_MARKERS = (
    "observer_only",
    "context_pack_body",
    "memory_body",
    "private_",
    "raw_private",
    "raw_context",
    "raw_memory",
    "raw_prompt",
    "raw_route",
    "unmanifested_prompt",
)


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
            if _is_private_contract_revision_key(key_text):
                continue
            sanitized[key_text] = _sanitize_public_contract_revision_value(child)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_public_contract_revision_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_public_contract_revision_value(item) for item in value]
    return value


def _is_private_contract_revision_key(key: str) -> bool:
    key_text = str(key or "")
    normalized = key_text.strip().lower().replace("-", "_")
    if normalized in _PRIVATE_CONTRACT_REVISION_KEYS:
        return True
    if any(marker in normalized for marker in _PRIVATE_CONTRACT_REVISION_KEY_MARKERS):
        return True
    return normalized.startswith("private") or normalized.endswith("_private")


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


_RUNTIME_CONTEXT_CONTENT_ADDRESS_SECRET_KEYS = {
    "fence_token",
    "raw_fence_token",
    "raw_session_token",
    "session_token",
    "worker_session_token",
    "worker_nonce",
    "raw_worker_nonce",
    "launch_text",
    "raw_launch_text",
    "subtree",
    "raw_subtree",
}

_RUNTIME_CONTEXT_CONTENT_ADDRESS_SECRET_MARKERS = (
    "fence_token",
    "session_token",
    "worker_nonce",
    "launch_text",
)

_RUNTIME_CONTEXT_CONTENT_ADDRESS_VOLATILE_KEYS = {
    "generated_at",
    "read_at",
    "read_timestamp",
    "served_at",
    "accessed_at",
}

_RUNTIME_CONTEXT_PUBLIC_PRIVACY_STATUS_KEYS = {
    "raw_private_context_exposed",
    "other_worker_contexts_exposed",
    "raw_source_of_truth_copied",
}


def _runtime_context_redacted_key_name(key: str) -> str:
    safe_key = str(key or "").strip() or "value"
    return f"{safe_key}_redacted"


def _is_runtime_context_secret_key(key: str) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    if not normalized:
        return False
    if normalized in _RUNTIME_CONTEXT_PUBLIC_PRIVACY_STATUS_KEYS:
        return False
    if normalized in _RUNTIME_CONTEXT_CONTENT_ADDRESS_SECRET_KEYS:
        return True
    if any(marker in normalized for marker in _RUNTIME_CONTEXT_CONTENT_ADDRESS_SECRET_MARKERS):
        return True
    if normalized.startswith("raw_") and (
        "token" in normalized or "nonce" in normalized
    ):
        return True
    return _is_private_contract_revision_key(normalized)


def _is_runtime_context_volatile_content_key(key: str) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    if not normalized:
        return False
    return normalized in _RUNTIME_CONTEXT_CONTENT_ADDRESS_VOLATILE_KEYS


def runtime_context_public_content_value(value: Any) -> Any:
    """Return deterministic public material for runtime-context hashes."""

    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key or "")
            if _is_runtime_context_volatile_content_key(key_text):
                sanitized[f"{key_text}_normalized"] = True
                continue
            if _is_runtime_context_secret_key(key_text):
                sanitized[_runtime_context_redacted_key_name(key_text)] = True
                continue
            sanitized[key_text] = runtime_context_public_content_value(child)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [runtime_context_public_content_value(item) for item in value]
    return value


def _runtime_context_secret_hash_key(key: str) -> str:
    normalized = str(key or "").strip().lower().replace("-", "_")
    if normalized.startswith("raw_"):
        normalized = normalized[4:]
    return f"{normalized or 'value'}_hash"


def redact_runtime_context_payload(
    value: Any,
    *,
    raw_secrets: Sequence[str] | None = None,
) -> Any:
    """Return a public runtime-context payload with raw capability material removed."""

    secret_values = {
        str(secret)
        for secret in (raw_secrets or ())
        if str(secret or "").strip()
    }
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key or "")
            if _is_runtime_context_secret_key(key_text):
                child_text = str(child or "") if child is not None else ""
                if child_text:
                    redacted[_runtime_context_secret_hash_key(key_text)] = (
                        runtime_context_secret_hash(child_text)
                    )
                redacted[key_text] = "redacted"
                redacted[_runtime_context_redacted_key_name(key_text)] = True
                continue
            redacted[key_text] = redact_runtime_context_payload(
                child,
                raw_secrets=tuple(secret_values),
            )
        return redacted
    if isinstance(value, list):
        return [
            redact_runtime_context_payload(item, raw_secrets=tuple(secret_values))
            for item in value
        ]
    if isinstance(value, tuple):
        return [
            redact_runtime_context_payload(item, raw_secrets=tuple(secret_values))
            for item in value
        ]
    if isinstance(value, str) and value in secret_values:
        return "redacted"
    return value


def _stable_content_json(value: Any) -> str:
    return json.dumps(
        runtime_context_public_content_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def runtime_context_content_hash(value: Any) -> str:
    """Return a deterministic content hash over public runtime-context material."""

    return "sha256:" + hashlib.sha256(
        _stable_content_json(value).encode("utf-8")
    ).hexdigest()


def _runtime_context_projection_node(
    *,
    runtime_context_id: str,
    view_name: str,
    payload: Any,
) -> dict[str, Any]:
    view = str(view_name or "")
    node_id = f"runtime_context/{runtime_context_id}/{view}"
    view_hash = runtime_context_content_hash(payload)
    node_hash = runtime_context_content_hash(
        {
            "node_id": node_id,
            "view": view,
            "view_hash": view_hash,
        }
    )
    return {
        "node_id": node_id,
        "view": view,
        "hash": node_hash,
        "node_hash": node_hash,
        "view_hash": view_hash,
    }


def runtime_context_projection_content_address(
    *,
    project_id: str,
    runtime_context_id: str,
    views: Mapping[str, Any],
    source_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Content-address the redacted runtime-context projection root and views."""

    nodes: dict[str, dict[str, Any]] = {}
    for view_name in sorted(str(key) for key in views.keys()):
        view_payload = views.get(view_name) if isinstance(views, Mapping) else {}
        nodes[view_name] = _runtime_context_projection_node(
            runtime_context_id=str(runtime_context_id or ""),
            view_name=view_name,
            payload=view_payload,
        )
    node_hashes = {
        view_name: nodes[view_name]["node_hash"]
        for view_name in sorted(nodes)
    }
    view_hashes = {
        view_name: nodes[view_name]["view_hash"]
        for view_name in sorted(nodes)
    }
    root_material = {
        "schema_version": RUNTIME_CONTEXT_PROJECTION_SCHEMA_VERSION,
        "project_id": str(project_id or ""),
        "runtime_context_id": str(runtime_context_id or ""),
        "nodes": node_hashes,
        "source_policy": dict(source_policy or {}),
    }
    root_hash = runtime_context_content_hash(root_material)
    return {
        "schema_version": RUNTIME_CONTEXT_CONTENT_ADDRESS_SCHEMA_VERSION,
        "project_id": str(project_id or ""),
        "runtime_context_id": str(runtime_context_id or ""),
        "projection_hash": root_hash,
        "root_hash": root_hash,
        "hash_algorithm": "sha256:json-stable-redacted-v1",
        "view_hashes": view_hashes,
        "nodes": nodes,
    }


def runtime_context_audit_nodes_for_views(
    projection_payload: Mapping[str, Any],
    view_names: Sequence[str] | str,
) -> list[dict[str, Any]]:
    content_address = (
        projection_payload.get("content_address")
        if isinstance(projection_payload, Mapping)
        else {}
    )
    nodes = (
        content_address.get("nodes")
        if isinstance(content_address, Mapping)
        else {}
    )
    requested = [view_names] if isinstance(view_names, str) else list(view_names)
    result: list[dict[str, Any]] = []
    seen_views: set[str] = set()
    for requested_view in requested:
        view_name = str(requested_view or "")
        if view_name == "all" and isinstance(nodes, Mapping):
            result.extend(
                dict(node)
                for _, node in sorted(nodes.items())
                if isinstance(node, Mapping)
            )
            continue
        node = nodes.get(view_name) if isinstance(nodes, Mapping) else None
        if isinstance(node, Mapping):
            result.append(dict(node))
            seen_views.add(view_name)
        if view_name in {"worker_view", "control_plane"}:
            boundary = (
                nodes.get("capability_boundary")
                if isinstance(nodes, Mapping)
                else None
            )
            if isinstance(boundary, Mapping) and "capability_boundary" not in seen_views:
                result.append(dict(boundary))
                seen_views.add("capability_boundary")
    return result


def runtime_context_filter_content_address(
    content_address: Mapping[str, Any],
    nodes_read: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return content-address metadata scoped to the nodes exposed to this read."""

    if not isinstance(content_address, Mapping):
        return {}
    allowed_views = {
        str(node.get("view") or "")
        for node in nodes_read
        if isinstance(node, Mapping) and str(node.get("view") or "")
    }
    nodes = content_address.get("nodes")
    nodes = nodes if isinstance(nodes, Mapping) else {}
    view_hashes = content_address.get("view_hashes")
    view_hashes = view_hashes if isinstance(view_hashes, Mapping) else {}
    scoped = {
        key: value
        for key, value in content_address.items()
        if key not in {"nodes", "view_hashes"}
    }
    scoped["view_hashes"] = {
        str(view): str(value or "")
        for view, value in sorted(view_hashes.items())
        if str(view) in allowed_views
    }
    scoped["nodes"] = {
        str(view): dict(node)
        for view, node in sorted(nodes.items())
        if str(view) in allowed_views and isinstance(node, Mapping)
    }
    return scoped


def runtime_context_single_view_audit_node(
    *,
    runtime_context_id: str,
    view_name: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _runtime_context_projection_node(
        runtime_context_id=str(runtime_context_id or ""),
        view_name=str(view_name or "runtime_contract"),
        payload=payload if isinstance(payload, Mapping) else {},
    )


def _redact_runtime_context_access_audit_metadata(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key or "")
            if _is_runtime_context_secret_key(key_text):
                redacted[_runtime_context_redacted_key_name(key_text)] = True
                continue
            redacted[key_text] = _redact_runtime_context_access_audit_metadata(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact_runtime_context_access_audit_metadata(item) for item in value]
    return value


def record_runtime_context_access_audit(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    runtime_context_id: str,
    task_id: str = "",
    session: Mapping[str, Any] | None = None,
    role: str = "",
    view_name: str = "",
    projection_hash: str = "",
    nodes_read: Sequence[Mapping[str, Any]] | None = None,
    decision: str = "allowed",
    reason: str = "",
    metadata: Mapping[str, Any] | None = None,
    now_iso: str = "",
) -> dict[str, Any]:
    """Persist a redacted audit row for a role-scoped runtime-context read."""

    ensure_branch_runtime_schema(conn)
    session_payload = session if isinstance(session, Mapping) else {}
    safe_nodes = [
        {
            key: str(node.get(key) or "")
            for key in ("node_id", "view", "hash", "node_hash", "view_hash")
            if str(node.get(key) or "")
        }
        for node in (nodes_read or [])
        if isinstance(node, Mapping)
    ]
    safe_metadata = public_contract_revision_payload(
        _redact_runtime_context_access_audit_metadata(metadata or {})
    )
    audit = {
        "schema_version": RUNTIME_CONTEXT_ACCESS_AUDIT_SCHEMA_VERSION,
        "audit_id": f"rtca-{uuid.uuid4().hex[:16]}",
        "project_id": str(project_id or ""),
        "runtime_context_id": str(runtime_context_id or ""),
        "task_id": str(task_id or ""),
        "principal_id": str(session_payload.get("principal_id") or ""),
        "session_id": str(session_payload.get("session_id") or ""),
        "role": str(role or session_payload.get("role") or ""),
        "view_name": str(view_name or ""),
        "decision": str(decision or ""),
        "reason": str(reason or ""),
        "projection_hash": str(projection_hash or ""),
        "nodes_read": safe_nodes,
        "metadata": safe_metadata,
        "created_at": now_iso or utc_now(),
    }
    conn.execute(
        """
        INSERT INTO parallel_branch_runtime_access_audit (
            audit_id, project_id, runtime_context_id, task_id, principal_id,
            session_id, role, view_name, decision, reason, projection_hash,
            nodes_read_json, metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            audit["audit_id"],
            audit["project_id"],
            audit["runtime_context_id"],
            audit["task_id"],
            audit["principal_id"],
            audit["session_id"],
            audit["role"],
            audit["view_name"],
            audit["decision"],
            audit["reason"],
            audit["projection_hash"],
            json.dumps(safe_nodes, ensure_ascii=False, sort_keys=True),
            _json_object(safe_metadata),
            audit["created_at"],
        ),
    )
    return audit


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


def _contract_revision_receipt_material(
    *,
    context: "BranchTaskRuntimeContext",
    runtime_context_id: str,
    revision_id: str,
    explicit_revision_id: bool,
    contract_version: str,
    payload: Mapping[str, Any],
    route_identity: Mapping[str, Any],
    previous_revision_hash: str,
) -> dict[str, Any]:
    material = {
        "schema_version": "agent_task_contract_revision_visible_text.v1",
        "project_id": context.project_id,
        "runtime_context_id": runtime_context_id,
        "task_id": context.task_id,
        "parent_task_id": _parent_task_id_for_context(context),
        "backlog_id": context.backlog_id,
        "contract_version": str(contract_version or ""),
        "payload": payload,
        "route_identity": route_identity,
        "previous_revision_hash": previous_revision_hash,
    }
    if explicit_revision_id:
        material["revision_id"] = revision_id
    return material


def _context_from_row(row: sqlite3.Row) -> BranchTaskRuntimeContext:
    runtime_context_id = ""
    row_keys = set(row.keys())
    if "runtime_context_id" in row_keys:
        runtime_context_id = row["runtime_context_id"] or ""
    agent_id = row["agent_id"] or ""
    worker_id = row["worker_id"] or ""
    return BranchTaskRuntimeContext(
        project_id=row["project_id"],
        task_id=row["task_id"],
        runtime_context_id=runtime_context_id
        or branch_runtime_context_id(row["project_id"], row["task_id"], int(row["attempt"] or 1)),
        batch_id=row["batch_id"] or "",
        backlog_id=row["backlog_id"] or "",
        parent_task_id=(
            row["parent_task_id"] if "parent_task_id" in row_keys else ""
        )
        or "",
        chain_id=row["chain_id"] or "",
        root_task_id=row["root_task_id"] or "",
        stage_task_id=row["stage_task_id"] or "",
        stage_type=row["stage_type"] or "",
        retry_round=int(row["retry_round"] or 0),
        agent_id=agent_id,
        worker_id=worker_id,
        allocation_owner=(
            row["allocation_owner"] if "allocation_owner" in row_keys else ""
        )
        or agent_id,
        worker_slot_id=(
            row["worker_slot_id"] if "worker_slot_id" in row_keys else ""
        )
        or worker_id,
        actual_host_worker_id=(
            row["actual_host_worker_id"]
            if "actual_host_worker_id" in row_keys
            else ""
        )
        or "",
        host_startup_id=(
            row["host_startup_id"] if "host_startup_id" in row_keys else ""
        )
        or "",
        host_session_id=(
            row["host_session_id"] if "host_session_id" in row_keys else ""
        )
        or "",
        governance_project_id=(
            row["governance_project_id"]
            if "governance_project_id" in row_keys
            else ""
        )
        or row["project_id"],
        target_project_id=(
            row["target_project_id"] if "target_project_id" in row_keys else ""
        )
        or row["project_id"],
        target_project_root=(
            row["target_project_root"] if "target_project_root" in row_keys else ""
        )
        or "",
        target_files=_parse_json_array(
            row["target_files_json"] if "target_files_json" in row_keys else "[]"
        ),
        owned_files=_parse_json_array(
            row["owned_files_json"] if "owned_files_json" in row_keys else "[]"
        ),
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
        session_token_hash=(
            row["session_token_hash"] if "session_token_hash" in row_keys else ""
        )
        or "",
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
    target_project_root = runtime_context_effective_target_project_root(context)
    owned_files = list(context.owned_files)
    target_files = list(context.target_files) or list(owned_files)
    payload["runtime_context_id"] = runtime_context_id_for_branch_context(context)
    payload["parent_task_id"] = _runtime_context_parent_task_id(context)
    payload["depends_on"] = list(context.depends_on)
    payload["allocation_owner"] = context.allocation_owner or context.agent_id
    payload["observer_allocation_owner"] = payload["allocation_owner"]
    payload["worker_slot_id"] = context.worker_slot_id or context.worker_id
    payload["governance_project_id"] = context.governance_project_id or context.project_id
    payload["target_project_id"] = context.target_project_id or context.project_id
    payload["target_project_root"] = target_project_root
    payload["project_root"] = target_project_root
    payload["repo_root"] = target_project_root
    payload["target_files"] = target_files
    payload["owned_files"] = owned_files or list(target_files)
    return payload


def branch_runtime_allocation_evidence(
    context: BranchTaskRuntimeContext,
    *,
    source_ref: str,
    registration_source: str = "parallel_branch_allocate",
    route_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return machine-consumable branch runtime allocation evidence."""
    runtime_context_id = runtime_context_id_for_branch_context(context)
    context_payload = branch_context_to_dict(context)
    route = public_contract_revision_route_identity(route_identity=route_identity)
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
        "target_project_root": context_payload.get("target_project_root", ""),
        "project_root": context_payload.get("project_root", ""),
        "repo_root": context_payload.get("repo_root", ""),
        "target_files": list(context_payload.get("target_files") or []),
        "owned_files": list(context_payload.get("owned_files") or []),
        "route_identity": route,
        "route_id": route.get("route_id", ""),
        "route_context_hash": route.get("route_context_hash", ""),
        "prompt_contract_id": route.get("prompt_contract_id", ""),
        "prompt_contract_hash": route.get("prompt_contract_hash", ""),
        "route_token_ref": route.get("route_token_ref", ""),
        "visible_injection_manifest_hash": route.get(
            "visible_injection_manifest_hash",
            "",
        ),
        "context": context_payload,
    }


def branch_contract_revision_to_dict(
    revision: BranchRuntimeContractRevision,
) -> dict[str, Any]:
    payload = asdict(revision)
    payload["payload"] = dict(revision.payload)
    payload["route_identity"] = dict(revision.route_identity)
    payload["route_gate"] = dict(revision.route_gate)
    return payload


def _runtime_context_text(value: Any) -> str:
    return str(value or "").strip()


def runtime_context_effective_target_project_root(
    context: "BranchTaskRuntimeContext",
) -> str:
    """Return the canonical target root projected to runtime-context callers."""

    return _runtime_context_text(
        getattr(context, "target_project_root", "")
        or getattr(context, "worktree_path", "")
    )


def runtime_context_target_project_root_matches(
    context: "BranchTaskRuntimeContext",
    target_project_root: str,
    *,
    allow_worktree_alias: bool = False,
) -> bool:
    """Return whether a presented target root matches the runtime context.

    Protected writes and graph queries use the canonical target project root.
    Worker guide/current-state reads may also accept the assigned worktree path
    as a read-only alias so the worker can recover the canonical shape.
    """

    requested_target_root = _startup_path_text(target_project_root)
    if not requested_target_root:
        return True
    context_target_root = _startup_path_text(
        runtime_context_effective_target_project_root(context)
    )
    if not context_target_root:
        return True
    if requested_target_root == context_target_root:
        return True
    if allow_worktree_alias:
        context_worktree_path = _startup_path_text(getattr(context, "worktree_path", ""))
        if context_worktree_path and requested_target_root == context_worktree_path:
            return True
    return False


def _runtime_context_startup_gate_payload(value: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = public_contract_revision_payload(value or {})
    event_payload = payload.get("payload")
    if isinstance(event_payload, Mapping):
        event_payload = public_contract_revision_payload(event_payload)
        for nested_key in (
            "mf_subagent_startup_gate",
            "mf_subagent_startup_refusal",
        ):
            nested = event_payload.get(nested_key)
            if not isinstance(nested, Mapping):
                continue
            nested_payload = public_contract_revision_payload(nested)
            return {
                **payload,
                **event_payload,
                nested_key: nested_payload,
                **nested_payload,
            }
        return {**payload, **event_payload}
    for nested_key in (
        "mf_subagent_startup_gate",
        "mf_subagent_startup_refusal",
    ):
        nested = payload.get(nested_key)
        if not isinstance(nested, Mapping):
            continue
        nested_payload = public_contract_revision_payload(nested)
        return {**payload, nested_key: nested_payload, **nested_payload}
    return payload


def _runtime_context_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Mapping):
        return [
            str(item).strip()
            for item in value.values()
            if str(item or "").strip()
        ]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [
            str(item).strip()
            for item in value
            if str(item or "").strip()
        ]
    return [str(value).strip()] if str(value or "").strip() else []


def _runtime_context_lane_clause_id(value: Any) -> str:
    normalized = _runtime_context_text(value).lower().replace("-", "_")
    return normalized


def _runtime_context_lane_clause_items(
    required_clauses: Sequence[str | Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    raw_clauses: Sequence[str | Mapping[str, Any]] = (
        required_clauses
        if required_clauses is not None
        else RUNTIME_CONTEXT_DEFAULT_LANE_CLAUSES
    )
    clauses: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_clauses:
        if isinstance(raw, Mapping):
            clause = _runtime_context_lane_clause_id(
                raw.get("clause")
                or raw.get("id")
                or raw.get("field")
                or raw.get("name")
            )
            if not clause or clause in seen:
                continue
            seen.add(clause)
            item = {
                "clause": clause,
                "status": "missing",
            }
            for key in ("expected_source", "producer", "consumer", "description"):
                if _runtime_context_text(raw.get(key)):
                    item[key] = _runtime_context_text(raw.get(key))
            clauses.append(item)
            continue
        clause = _runtime_context_lane_clause_id(raw)
        if clause and clause not in seen:
            seen.add(clause)
            clauses.append({"clause": clause, "status": "missing"})
    return clauses


def _runtime_context_lane_event_mapping(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = _runtime_context_mapping(event.get("payload"))
    return {**payload, **dict(event)}


def _runtime_context_lane_event_sort_key(
    indexed_event: tuple[int, Mapping[str, Any]],
) -> tuple[str, str, str, str]:
    index, event = indexed_event
    merged = _runtime_context_lane_event_mapping(event)
    return (
        _runtime_context_text(
            merged.get("created_at")
            or merged.get("timestamp")
            or merged.get("time")
            or merged.get("updated_at")
        ),
        _runtime_context_text(merged.get("event_id") or merged.get("id")),
        _runtime_context_text(merged.get("seq") or merged.get("sequence")),
        f"{index:08d}",
    )


def _runtime_context_lane_event_kind(event: Mapping[str, Any]) -> str:
    merged = _runtime_context_lane_event_mapping(event)
    return _runtime_context_lane_clause_id(
        merged.get("event_kind")
        or merged.get("kind")
        or merged.get("type")
        or merged.get("phase")
    )


def _runtime_context_lane_event_status(event: Mapping[str, Any]) -> str:
    merged = _runtime_context_lane_event_mapping(event)
    return _runtime_context_lane_clause_id(
        merged.get("status")
        or merged.get("decision")
        or merged.get("result")
        or merged.get("outcome")
    )


def _runtime_context_lane_event_ref(event: Mapping[str, Any]) -> str:
    merged = _runtime_context_lane_event_mapping(event)
    return _runtime_context_text(
        merged.get("event_ref")
        or merged.get("timeline_ref")
        or merged.get("event_id")
        or merged.get("id")
        or merged.get("trace_id")
    )


def _runtime_context_lane_normalized_ref(value: Any) -> str:
    ref = _runtime_context_text(value)
    if ref.startswith("timeline:"):
        return ref.removeprefix("timeline:")
    return ref


def _runtime_context_lane_timeline_ref(value: Any) -> str:
    ref = _runtime_context_text(value)
    if not ref:
        return ""
    return ref if ref.startswith("timeline:") else f"timeline:{ref}"


def _runtime_context_lane_resolved_refs(event: Mapping[str, Any]) -> list[str]:
    merged = _runtime_context_lane_event_mapping(event)
    refs: list[str] = []
    for key in (
        "resolves_event_ref",
        "resolved_event_ref",
        "blocked_event_ref",
        "resolves_event_id",
        "resolved_event_id",
        "blocked_event_id",
        "previous_failed_qa_ref",
        "previous_failed_qa_event_ref",
    ):
        refs.extend(
            _runtime_context_lane_normalized_ref(item)
            for item in _runtime_context_string_list(merged.get(key))
        )
    return _runtime_context_dedupe([ref for ref in refs if ref])


_RUNTIME_CONTEXT_REVIEWED_EVENT_REF_KEYS = {
    "implementation_event_ref",
    "implementation_event_refs",
    "review_ready_event_ref",
    "review_ready_event_refs",
    "worker_verification_event_ref",
    "worker_verification_event_refs",
    "verification_event_ref",
    "verification_event_refs",
    "qa_event_ref",
    "qa_event_refs",
    "finish_gate_event_ref",
    "finish_gate_event_refs",
    "previous_failed_qa_ref",
    "previous_failed_qa_event_ref",
}


def _runtime_context_public_qa_findings(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    sanitized = _sanitize_public_contract_revision_value(value[:5])
    return sanitized if isinstance(sanitized, list) else []


def _runtime_context_public_timeline_ref_value(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        refs = [
            ref
            for item in value
            if (ref := _runtime_context_public_timeline_ref_value(item))
        ]
        return refs
    text = _runtime_context_text(value).strip()
    if not text:
        return None
    if text.startswith("timeline:"):
        event_id = text.removeprefix("timeline:").strip()
    else:
        event_id = text
    if not event_id.isdigit():
        return None
    return f"timeline:{event_id}"


def _runtime_context_public_reviewed_events(value: Any) -> dict[str, Any]:
    reviewed_events = _runtime_context_mapping(value)
    safe: dict[str, Any] = {}
    for key in _RUNTIME_CONTEXT_REVIEWED_EVENT_REF_KEYS:
        event_ref = reviewed_events.get(key)
        if not event_ref:
            continue
        public_ref = _runtime_context_public_timeline_ref_value(event_ref)
        if public_ref:
            safe[key] = public_ref
    return safe


def _runtime_context_lane_blocking_event_details(
    event: Mapping[str, Any],
    merged: Mapping[str, Any],
) -> dict[str, Any]:
    verification = _runtime_context_mapping(event.get("verification"))
    artifact_refs = _runtime_context_mapping(event.get("artifact_refs"))
    details: dict[str, Any] = {}
    for key, source in (
        ("summary", merged),
        ("verdict", merged),
        ("runtime_context_id", merged),
        ("route_token_ref", merged),
        ("commit_sha", event),
    ):
        value = _runtime_context_text(source.get(key))
        if value:
            details[key] = value
    acceptance_failed = _runtime_context_string_list(
        merged.get("acceptance_failed")
        or verification.get("acceptance_failed")
        or merged.get("failed_acceptance_items")
        or verification.get("failed_acceptance_items")
    )
    if acceptance_failed:
        details["acceptance_failed"] = acceptance_failed
    findings = merged.get("findings")
    public_findings = _runtime_context_public_qa_findings(findings)
    if public_findings:
        details["findings"] = public_findings
    reviewed_events = _runtime_context_public_reviewed_events(
        merged.get("reviewed_events")
    )
    if reviewed_events:
        details["reviewed_events"] = reviewed_events
    safe_artifacts = {
        key: value
        for key, value in artifact_refs.items()
        if key
        in {
            "implementation_event_ref",
            "review_ready_event_ref",
            "worker_verification_event_ref",
            "previous_failed_qa_ref",
            "graph_trace_id",
            "worktree_head",
        }
    }
    if safe_artifacts:
        details["artifact_refs"] = public_contract_revision_payload(safe_artifacts)
    return details


def _runtime_context_finish_gate_lane_projection(event: Mapping[str, Any]) -> dict[str, Any]:
    if not _runtime_context_is_worker_evidence(event):
        return {}
    payload = _runtime_context_mapping(event.get("payload"))
    finish_gate = _runtime_context_nested_finish_gate_payload(payload)
    if not finish_gate:
        finish_gate = payload
    projection = _runtime_context_mapping(finish_gate.get("lane_ownership_projection"))
    if not projection:
        return {}
    worker_role = _runtime_context_lane_clause_id(
        projection.get("worker_role")
        or projection.get("role")
        or finish_gate.get("worker_role")
        or finish_gate.get("role")
    )
    if worker_role and worker_role != RUNTIME_CONTEXT_WORKER_ROLE:
        return {}
    if not (
        projection.get("review_ready")
        or projection.get("waiting_merge")
        or _runtime_context_lane_clause_id(projection.get("worker_status"))
        in {"review_ready", "waiting_merge"}
        or _runtime_context_lane_clause_id(projection.get("stop_state"))
        in {"review_ready", "waiting_merge"}
    ):
        return {}
    return projection


def _runtime_context_finish_gate_lane_projection_clauses(
    event: Mapping[str, Any],
) -> list[str]:
    projection = _runtime_context_finish_gate_lane_projection(event)
    if not projection:
        return []
    clauses: list[str] = []
    clauses.extend(
        _runtime_context_lane_clause_id(item)
        for item in _runtime_context_string_list(projection.get("evidence_id"))
    )
    clauses.extend(
        _runtime_context_lane_clause_id(item)
        for item in _runtime_context_string_list(projection.get("evidence_ids"))
    )
    if projection.get("review_ready"):
        clauses.append("bounded_implementation_subagent.review_ready")
    if (
        projection.get("waiting_merge")
        or _runtime_context_lane_clause_id(projection.get("worker_status"))
        == "waiting_merge"
        or _runtime_context_lane_clause_id(projection.get("stop_state"))
        == "waiting_merge"
    ):
        clauses.append("bounded_implementation_subagent.waiting_merge")
    return _runtime_context_dedupe(clauses)


def _runtime_context_lane_event_clauses(event: Mapping[str, Any]) -> list[str]:
    merged = _runtime_context_lane_event_mapping(event)
    explicit: list[str] = []
    for key in (
        "clause",
        "clause_id",
        "evidence_clause",
        "evidence_kind",
        "required_evidence",
    ):
        explicit.extend(
            _runtime_context_lane_clause_id(item)
            for item in _runtime_context_string_list(merged.get(key))
        )
    explicit = [item for item in explicit if item]
    if explicit:
        return _runtime_context_dedupe(explicit)
    event_kind = _runtime_context_lane_event_kind(event)
    return _runtime_context_dedupe(
        [
            *list(_RUNTIME_CONTEXT_EVENT_KIND_CLAUSES.get(event_kind, ())),
            *_runtime_context_finish_gate_lane_projection_clauses(event),
        ]
    )


def _runtime_context_lane_reviewed_event_refs(event: Mapping[str, Any]) -> list[str]:
    merged = _runtime_context_lane_event_mapping(event)
    refs: list[str] = []
    for source in (
        merged,
        _runtime_context_mapping(merged.get("reviewed_events")),
        _runtime_context_mapping(merged.get("artifact_refs")),
    ):
        for key in _RUNTIME_CONTEXT_REVIEWED_EVENT_REF_KEYS:
            refs.extend(
                _runtime_context_lane_normalized_ref(item)
                for item in _runtime_context_string_list(source.get(key))
            )
    return _runtime_context_dedupe([ref for ref in refs if ref])


def _runtime_context_lane_event_is_worker_revision_implementation(
    event: Mapping[str, Any],
) -> bool:
    if not _runtime_context_is_worker_evidence(event):
        return False
    event_kind = _runtime_context_lane_event_kind(event)
    return event_kind in {
        "implementation",
        "implementation_evidence",
        "runtime_context_implementation_evidence",
        "worker_implementation",
        "mf_subagent_implementation",
    }


def _runtime_context_lane_event_is_worker_finish_or_review_ready(
    event: Mapping[str, Any],
) -> bool:
    if not _runtime_context_is_worker_evidence(event):
        return False
    event_kind = _runtime_context_lane_event_kind(event)
    if event_kind in {
        "finish_gate",
        "mf_subagent_finish_gate",
        "review_ready",
        "worker_review_ready",
        "close_ready",
    }:
        return True
    if _runtime_context_finish_gate_lane_projection(event):
        return True
    merged = _runtime_context_lane_event_mapping(event)
    if merged.get("review_ready") or merged.get("waiting_merge"):
        return True
    return any(
        _runtime_context_lane_clause_id(merged.get(key))
        in {"review_ready", "waiting_merge"}
        for key in ("worker_status", "stop_state", "handoff_status")
    )


def _runtime_context_lane_failed_qa_superseded_by_post_failure_revision(
    *,
    blocker_index: int,
    blocker_raw_event: Mapping[str, Any],
    fulfilling_records: Sequence[tuple[int, Mapping[str, Any], Mapping[str, Any]]],
) -> bool:
    event_kind = _runtime_context_lane_event_kind(blocker_raw_event)
    status = _runtime_context_lane_event_status(blocker_raw_event)
    if event_kind not in _RUNTIME_CONTEXT_QA_EVENT_KINDS:
        return False
    if status not in _RUNTIME_CONTEXT_BLOCKING_STATUSES:
        return False
    reviewed_refs = set(_runtime_context_lane_reviewed_event_refs(blocker_raw_event))
    saw_revision_implementation = False
    saw_finish_or_review_ready = False
    for record_index, event_view, raw_event in fulfilling_records:
        if record_index <= blocker_index:
            continue
        event_ref = _runtime_context_lane_normalized_ref(event_view.get("event_ref"))
        if not event_ref or event_ref in reviewed_refs:
            continue
        if _runtime_context_lane_event_is_worker_revision_implementation(raw_event):
            saw_revision_implementation = True
        if _runtime_context_lane_event_is_worker_finish_or_review_ready(raw_event):
            saw_finish_or_review_ready = True
        if saw_revision_implementation and saw_finish_or_review_ready:
            return True
    return False


def build_runtime_context_lane_plan_view(
    events: Sequence[Mapping[str, Any]] | None,
    *,
    required_clauses: Sequence[str | Mapping[str, Any]] | None = None,
    lane_id: str = "",
    generated_at: str = "",
) -> dict[str, Any]:
    """Fold task-timeline-like events into a compact deterministic clause view."""

    clause_items = _runtime_context_lane_clause_items(required_clauses)
    clause_order = [item["clause"] for item in clause_items]
    clause_by_id = {item["clause"]: dict(item) for item in clause_items}
    fulfilled: dict[str, dict[str, Any]] = {}
    blocking_events: list[dict[str, Any]] = []
    blocking_records: list[tuple[int, dict[str, Any], Mapping[str, Any]]] = []
    fulfilling_records: list[tuple[int, dict[str, Any], Mapping[str, Any]]] = []
    resolved_event_refs: set[str] = set()
    last_event: dict[str, Any] = {}
    lane = _runtime_context_text(lane_id)
    sorted_events = sorted(
        (
            (index, event)
            for index, event in enumerate(events or ())
            if isinstance(event, Mapping)
        ),
        key=_runtime_context_lane_event_sort_key,
    )
    for ordered_index, (_, event) in enumerate(
        sorted_events,
    ):
        merged = _runtime_context_lane_event_mapping(event)
        event_lane = _runtime_context_text(
            merged.get("lane_id") or merged.get("task_id") or merged.get("worker_id")
        )
        if lane and event_lane and event_lane != lane:
            continue
        event_kind = _runtime_context_lane_event_kind(event)
        status = _runtime_context_lane_event_status(event)
        event_ref = _runtime_context_lane_event_ref(event)
        occurred_at = _runtime_context_text(
            merged.get("created_at")
            or merged.get("timestamp")
            or merged.get("time")
            or merged.get("updated_at")
        )
        event_view = {
            "event_kind": event_kind,
            "event_ref": event_ref,
            "status": status or "recorded",
            "at": occurred_at,
        }
        last_event = event_view
        clauses = [
            clause
            for clause in _runtime_context_lane_event_clauses(event)
            if clause in clause_by_id
        ]
        if status in _RUNTIME_CONTEXT_BLOCKING_STATUSES:
            blocking_event = (
                event_view
                | {"clauses": clauses}
                | _runtime_context_lane_blocking_event_details(event, merged)
            )
            blocking_events.append(blocking_event)
            blocking_records.append((ordered_index, blocking_event, event))
            continue
        if status not in _RUNTIME_CONTEXT_FULFILLING_STATUSES:
            continue
        fulfilling_records.append((ordered_index, event_view, event))
        resolved_event_refs.update(_runtime_context_lane_resolved_refs(event))
        for clause in clauses:
            if clause in fulfilled:
                continue
            fulfilled[clause] = (
                dict(clause_by_id[clause])
                | event_view
                | {"clause": clause, "status": "fulfilled"}
            )
    if resolved_event_refs:
        blocking_records = [
            record
            for record in blocking_records
            if _runtime_context_lane_normalized_ref(record[1].get("event_ref"))
            not in resolved_event_refs
        ]
        blocking_events = [
            event
            for event in blocking_events
            if _runtime_context_lane_normalized_ref(event.get("event_ref"))
            not in resolved_event_refs
        ]
    superseded_blocking_refs = {
        _runtime_context_lane_normalized_ref(blocking_event.get("event_ref"))
        for blocker_index, blocking_event, raw_event in blocking_records
        if _runtime_context_lane_failed_qa_superseded_by_post_failure_revision(
            blocker_index=blocker_index,
            blocker_raw_event=raw_event,
            fulfilling_records=fulfilling_records,
        )
    }
    if superseded_blocking_refs:
        blocking_events = [
            event
            for event in blocking_events
            if _runtime_context_lane_normalized_ref(event.get("event_ref"))
            not in superseded_blocking_refs
        ]
    fulfilled_items = [fulfilled[clause] for clause in clause_order if clause in fulfilled]
    missing_items = [
        dict(clause_by_id[clause]) | {"status": "missing"}
        for clause in clause_order
        if clause not in fulfilled
    ]
    clause_plan = fulfilled_items + missing_items
    status = "ready" if not missing_items else "missing_required_clauses"
    if blocking_events:
        status = "blocked"
    return {
        "schema_version": RUNTIME_CONTEXT_LANE_FOLD_SCHEMA_VERSION,
        "lane_id": lane,
        "generated_at": generated_at or utc_now(),
        "current_state": {
            "status": status,
            "fulfilled_count": len(fulfilled_items),
            "missing_count": len(missing_items),
            "blocking_count": len(blocking_events),
            "next_missing_clause": missing_items[0]["clause"] if missing_items else "",
            "last_event_kind": last_event.get("event_kind", ""),
            "last_event_ref": last_event.get("event_ref", ""),
        },
        "fulfilled": fulfilled_items,
        "missing": missing_items,
        "clause_plan": clause_plan,
        "blocking_events": blocking_events,
    }


def _runtime_context_dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        token = _runtime_context_text(value)
        if token and token not in seen:
            seen.add(token)
            result.append(token)
    return result


def _runtime_context_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _runtime_context_revision_mapping(
    revision: BranchRuntimeContractRevision | Mapping[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(revision, BranchRuntimeContractRevision):
        return branch_contract_revision_to_dict(revision)
    return _runtime_context_mapping(revision)


def _runtime_context_revision_payload(
    revision: BranchRuntimeContractRevision | Mapping[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(revision, BranchRuntimeContractRevision):
        return public_contract_revision_payload(revision.payload)
    mapped = _runtime_context_revision_mapping(revision)
    return public_contract_revision_payload(mapped.get("payload"))


def _runtime_context_parent_task_id(context: BranchTaskRuntimeContext) -> str:
    parent_task_id = _runtime_context_text(getattr(context, "parent_task_id", ""))
    if parent_task_id:
        return parent_task_id
    root_task_id = _runtime_context_text(context.root_task_id)
    chain_id = _runtime_context_text(context.chain_id)
    task_id = _runtime_context_text(context.task_id)
    if (
        chain_id
        and chain_id not in {root_task_id, task_id}
        and not chain_id.startswith(("chain-", "cchain-"))
    ):
        return chain_id
    return root_task_id or chain_id or context.stage_task_id or context.task_id


def _runtime_context_route_identity(
    *,
    contract_revision: BranchRuntimeContractRevision | Mapping[str, Any] | None = None,
    route_identity: Mapping[str, Any] | None = None,
    route_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    revision = _runtime_context_revision_mapping(contract_revision)
    revision_route_identity = _runtime_context_mapping(revision.get("route_identity"))
    revision_route_gate = _runtime_context_mapping(revision.get("route_gate"))
    revision_payload = _runtime_context_mapping(revision.get("payload"))
    payload_route_identity = _runtime_context_mapping(revision_payload.get("route_identity"))
    payload_route_gate = _runtime_context_mapping(revision_payload.get("route_gate"))

    safe: dict[str, Any] = {}
    for source_route_identity, source_route_gate in (
        (revision_route_identity, revision_route_gate),
        (payload_route_identity, payload_route_gate),
        (_runtime_context_mapping(route_identity), _runtime_context_mapping(route_gate)),
    ):
        safe.update(
            public_contract_revision_route_identity(
                source_route_gate,
                source_route_identity,
            )
        )
    safe["raw_private_context_exposed"] = False
    return safe


def _runtime_context_contract_revision_ref(
    contract_revision: BranchRuntimeContractRevision | Mapping[str, Any] | None,
) -> dict[str, Any]:
    revision = _runtime_context_revision_mapping(contract_revision)
    return {
        "producer": "parallel_branch_runtime",
        "source": "parallel_branch_runtime_contract_revisions",
        "revision_id": _runtime_context_text(revision.get("revision_id")),
        "contract_version": _runtime_context_text(revision.get("contract_version")),
        "created_at": _runtime_context_text(revision.get("created_at")),
        "actor": _runtime_context_text(revision.get("actor")),
    }


def _runtime_context_revision_string_list(
    payload: Mapping[str, Any],
    *keys: str,
) -> list[str]:
    for key in keys:
        values = _runtime_context_string_list(payload.get(key))
        if values:
            return values
    return []


def _runtime_context_revision_scope_string_list(
    payload: Mapping[str, Any],
    *keys: str,
) -> list[str]:
    values = _runtime_context_revision_string_list(payload, *keys)
    if values:
        return values
    for key in keys:
        for candidate in _runtime_context_deep_values(payload, key):
            values = _runtime_context_string_list(candidate)
            if values:
                return values
    return []


def _runtime_context_timeline_refs(
    timeline_refs: Mapping[str, Any] | None,
) -> dict[str, Any]:
    source = public_contract_revision_payload(timeline_refs or {})
    normalized: dict[str, Any] = {
        "producer": "task_timeline",
        "source": "task_timeline_refs",
        "dispatch_event_ref": _runtime_context_text(
            source.get("dispatch_event_ref") or source.get("dispatch_event_id")
        ),
        "startup_event_ref": _runtime_context_text(
            source.get("startup_event_ref") or source.get("startup_event_id")
        ),
        "read_receipt_event_ref": _runtime_context_text(
            source.get("read_receipt_event_ref")
            or source.get("read_receipt_event_id")
            or source.get("read_receipt_ref")
        ),
        "route_action_precheck_event_ref": _runtime_context_text(
            source.get("route_action_precheck_event_ref")
            or source.get("route_action_precheck_event_id")
            or source.get("route_action_precheck_ref")
        ),
        "finish_event_ref": _runtime_context_text(
            source.get("finish_event_ref") or source.get("finish_event_id")
        ),
        "close_ready_event_ref": _runtime_context_text(
            source.get("close_ready_event_ref") or source.get("close_ready_event_id")
        ),
        "heartbeat_event_ref": _runtime_context_text(
            source.get("heartbeat_event_ref")
            or source.get("heartbeat_event_id")
            or source.get("last_heartbeat_event_ref")
            or source.get("last_heartbeat_event_id")
        ),
        "progress_event_ref": _runtime_context_text(
            source.get("progress_event_ref") or source.get("progress_event_id")
        ),
        "no_progress_timeout_event_ref": _runtime_context_text(
            source.get("no_progress_timeout_event_ref")
            or source.get("no_progress_timeout_event_id")
            or source.get("no_progress_event_ref")
        ),
        "changed_files": _runtime_context_dedupe(
            _runtime_context_string_list(source.get("changed_files"))
        ),
        "owned_changed_files": _runtime_context_dedupe(
            _runtime_context_string_list(source.get("owned_changed_files"))
        ),
        "worker_changed_files": _runtime_context_dedupe(
            _runtime_context_string_list(source.get("worker_changed_files"))
        ),
        "implementation_event_refs": _runtime_context_dedupe(
            _runtime_context_string_list(
                source.get("implementation_event_refs")
                or source.get("implementation_event_ids")
            )
        ),
        "verification_event_refs": _runtime_context_dedupe(
            _runtime_context_string_list(
                source.get("verification_event_refs")
                or source.get("verification_event_ids")
            )
        ),
    }
    return normalized


def _runtime_context_graph_trace_refs(
    graph_trace_refs: Mapping[str, Any] | Sequence[str] | None,
) -> dict[str, Any]:
    if isinstance(graph_trace_refs, Mapping):
        source = public_contract_revision_payload(graph_trace_refs)
        trace_values: list[str] = []
        for key in (
            "trace_ids",
            "graph_trace_ids",
            "graph_query_trace_ids",
            "query_trace_ids",
            "trace_id",
            "graph_trace_id",
        ):
            trace_values.extend(_runtime_context_string_list(source.get(key)))
        query_source = _runtime_context_text(source.get("query_source"))
        worker_role = _runtime_context_text(
            source.get("worker_role") or source.get("role")
        )
        parent_task_id = _runtime_context_text(source.get("parent_task_id"))
        task_id = _runtime_context_text(source.get("task_id"))
        runtime_context_id = _runtime_context_text(source.get("runtime_context_id"))
        backlog_id = _runtime_context_text(source.get("backlog_id"))
        source_details = public_contract_revision_payload(source.get("source_details"))
    else:
        trace_values = _runtime_context_string_list(graph_trace_refs)
        query_source = ""
        worker_role = ""
        parent_task_id = ""
        task_id = ""
        runtime_context_id = ""
        backlog_id = ""
        source_details = {}
    trace_ids = _runtime_context_dedupe(trace_values)
    return {
        "producer": "graph_query_trace",
        "source": _runtime_context_text(
            source.get("source") if isinstance(graph_trace_refs, Mapping) else ""
        )
        or "graph_query_trace_refs",
        "query_source": query_source,
        "worker_role": worker_role,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "runtime_context_id": runtime_context_id,
        "backlog_id": backlog_id,
        "trace_ids": trace_ids,
        "present": bool(trace_ids),
        "source_details": source_details,
    }


def _runtime_context_deep_values(value: Any, key: str, *, depth: int = 0) -> list[Any]:
    if depth > 6:
        return []
    if isinstance(value, Mapping):
        found: list[Any] = []
        if key in value:
            found.append(value.get(key))
        for child in value.values():
            found.extend(_runtime_context_deep_values(child, key, depth=depth + 1))
        return found
    if isinstance(value, list):
        found = []
        for child in value:
            found.extend(_runtime_context_deep_values(child, key, depth=depth + 1))
        return found
    return []


def _runtime_context_deep_text(value: Any, key: str) -> str:
    for item in _runtime_context_deep_values(value, key):
        text = _runtime_context_text(item)
        if text:
            return text
    return ""


def _runtime_context_event_ref(event: Mapping[str, Any]) -> str:
    explicit_ref = _runtime_context_text(event.get("event_ref"))
    if explicit_ref:
        return explicit_ref
    for key in ("id", "event_id"):
        event_id = _runtime_context_text(event.get(key))
        if event_id:
            if event_id.isdigit():
                return f"timeline:{event_id}"
            return event_id
    trace_id = _runtime_context_text(event.get("trace_id"))
    if trace_id:
        return trace_id
    return _runtime_context_deep_text(event, "event_id")


def _runtime_context_event_same_lineage(
    event: Mapping[str, Any],
    *,
    runtime_context_id: str,
    task_id: str,
    parent_task_id: str,
    backlog_id: str,
) -> bool:
    checks = (
        ("runtime_context_id", runtime_context_id),
        ("task_id", task_id),
        ("parent_task_id", parent_task_id),
        ("backlog_id", backlog_id),
    )
    saw_lineage = False
    for key, expected in checks:
        actual = _runtime_context_deep_text(event, key)
        if not actual:
            continue
        saw_lineage = True
        if expected and actual != expected:
            return False
    return saw_lineage


def _runtime_context_event_status_ok(event: Mapping[str, Any]) -> bool:
    status = _runtime_context_text(
        event.get("status")
        or event.get("decision")
        or _runtime_context_deep_text(event, "status")
    ).lower()
    return status in _RUNTIME_CONTEXT_FULFILLING_STATUSES


def _runtime_context_event_terminal_dispatch_blocker(event: Mapping[str, Any]) -> bool:
    if not isinstance(event, Mapping):
        return False
    if any(bool(value) for value in _runtime_context_deep_values(event, "terminal_dispatch_blocker")):
        return True
    if any(bool(value) for value in _runtime_context_deep_values(event, "dispatch_blocker")):
        status = _runtime_context_text(
            event.get("status") or _runtime_context_deep_text(event, "status")
        ).lower()
        return status in _RUNTIME_CONTEXT_BLOCKING_STATUSES
    return False


def _runtime_context_terminal_blocker_matches_lineage(
    event: Mapping[str, Any],
    *,
    runtime_context_id: str,
    task_id: str,
    parent_task_id: str,
    backlog_id: str,
) -> bool:
    expected = {
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "backlog_id": backlog_id,
    }
    for key, expected_value in expected.items():
        if not expected_value:
            continue
        values = [
            _runtime_context_text(value)
            for value in _runtime_context_deep_values(event, key)
        ]
        if expected_value in values:
            return True
    return False


def _runtime_context_terminal_dispatch_blockers(
    events: Sequence[Mapping[str, Any]] | None,
    *,
    runtime_context_id: str,
    task_id: str,
    parent_task_id: str,
    backlog_id: str,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    resolved_refs: set[str] = set()
    for event in events or ():
        if not isinstance(event, Mapping):
            continue
        for ref in _runtime_context_lane_resolved_refs(event):
            if ref:
                resolved_refs.add(ref)
    for event in events or ():
        if not isinstance(event, Mapping):
            continue
        if not _runtime_context_event_terminal_dispatch_blocker(event):
            continue
        if not _runtime_context_terminal_blocker_matches_lineage(
            event,
            runtime_context_id=runtime_context_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            backlog_id=backlog_id,
        ):
            continue
        event_ref = _runtime_context_event_ref(event)
        if event_ref and _runtime_context_lane_normalized_ref(event_ref) in resolved_refs:
            continue
        payload = _runtime_context_mapping(event.get("payload"))
        blocker_id = _runtime_context_deep_text(event, "blocker_id")
        blockers.append(
            {
                "event_ref": event_ref,
                "event_kind": _runtime_context_event_kind_token(event),
                "status": _runtime_context_lane_event_status(event) or "blocked",
                "blocker_id": blocker_id or "terminal_dispatch_blocker",
                "message": _runtime_context_deep_text(event, "message")
                or _runtime_context_deep_text(event, "reason"),
                "terminal_dispatch_blocker": True,
                "dispatch_blocker": True,
                "payload": public_contract_revision_payload(payload),
            }
        )
    return blockers


def _runtime_context_event_kind_token(event: Mapping[str, Any]) -> str:
    return _runtime_context_lane_clause_id(
        event.get("event_kind") or event.get("event_type") or event.get("phase")
    )


def _runtime_context_is_implementation_event_kind(event_kind: str) -> bool:
    return event_kind in {
        "implementation",
        "implementation_evidence",
        "worker_implementation",
    }


def _runtime_context_is_finish_time_worker_attestation_event(
    event: Mapping[str, Any],
    *,
    event_kind: str,
    payload: Mapping[str, Any],
) -> bool:
    event_type = _runtime_context_text(event.get("event_type")).lower().replace("-", "_")
    raw_event_kind = _runtime_context_text(event.get("event_kind")).lower().replace("-", "_")
    phase = _runtime_context_text(event.get("phase")).lower().replace("-", "_")
    action = _runtime_context_text(payload.get("action")).lower()
    schema_version = _runtime_context_text(payload.get("schema_version")).lower()
    haystack = " ".join((event_type, raw_event_kind, phase, event_kind, action))
    return (
        action == "record_finish_time_worker_attestation"
        or "finish_time_worker_attestation" in haystack
        or schema_version == "runtime_context.finish_time_worker_attestation.v1"
    )


def _runtime_context_is_worker_evidence(event: Mapping[str, Any]) -> bool:
    actor = _runtime_context_text(event.get("actor")).lower()
    if actor in {
        "observer",
        "mf_observer",
        "route_observer",
        "observer_runtime_text",
        "qa",
        "independent_qa",
        "qa_verifier",
    }:
        return False
    worker_role = (
        _runtime_context_deep_text(event, "worker_role")
        or _runtime_context_deep_text(event, "role")
    ).lower()
    query_source = _runtime_context_deep_text(event, "query_source").lower()
    if query_source and query_source != "mf_subagent":
        return False
    if worker_role and worker_role != RUNTIME_CONTEXT_WORKER_ROLE:
        return False
    return bool(
        worker_role == RUNTIME_CONTEXT_WORKER_ROLE
        or query_source == "mf_subagent"
    )


def _runtime_context_event_lane_bound(
    event: Mapping[str, Any],
    *,
    runtime_context_id: str,
    task_id: str,
    fence_token: str,
) -> bool:
    """Return true only when graph evidence names the current worker lane."""

    expected = (
        ("runtime_context_id", runtime_context_id),
        ("task_id", task_id),
        ("fence_token", fence_token),
    )
    for key, expected_value in expected:
        if not expected_value:
            continue
        actual = _runtime_context_deep_text(event, key)
        if actual and actual == expected_value:
            return True
    return False


def _runtime_context_event_matches_route_identity(
    event: Mapping[str, Any],
    route_identity: Mapping[str, Any] | None,
) -> bool:
    identity = _runtime_context_mapping(route_identity)
    required_fields = ("route_context_hash", "prompt_contract_id")
    for field in required_fields:
        expected = _runtime_context_text(identity.get(field))
        if not expected:
            continue
        actual = _runtime_context_deep_text(event, field)
        if actual != expected:
            return False
    for field in (
        "route_id",
        "prompt_contract_hash",
        "visible_injection_manifest_hash",
    ):
        expected = _runtime_context_text(identity.get(field))
        if not expected:
            continue
        actual = _runtime_context_deep_text(event, field)
        if actual and actual != expected:
            return False
    return True


def _runtime_context_timeline_derived_evidence(
    events: Sequence[Mapping[str, Any]] | None,
    *,
    runtime_context_id: str,
    task_id: str,
    parent_task_id: str,
    backlog_id: str,
    fence_token: str = "",
    route_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    explicit_route_identity = {
        key: value
        for key, value in _runtime_context_mapping(route_identity).items()
        if _runtime_context_text(value)
    }
    derived_route_identity: dict[str, str] = {}
    route_identity_keys = (
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "route_token_ref",
        "visible_injection_manifest_hash",
    )

    def _collect_route_identity(event: Mapping[str, Any]) -> None:
        for key in route_identity_keys:
            if derived_route_identity.get(key):
                continue
            value = _runtime_context_deep_text(event, key)
            if value:
                derived_route_identity[key] = value

    for raw_event in events or ():
        if not isinstance(raw_event, Mapping):
            continue
        event = public_contract_revision_payload(raw_event)
        if not _runtime_context_event_same_lineage(
            event,
            runtime_context_id=runtime_context_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            backlog_id=backlog_id,
        ):
            continue
        if not _runtime_context_event_status_ok(event):
            continue
        if _runtime_context_event_kind_token(event) == "route_action_precheck":
            continue
        _collect_route_identity(event)

    timeline_refs: dict[str, Any] = {}
    graph_trace_ids: list[str] = []
    graph_trace_event_refs: list[str] = []
    implementation_refs: list[str] = []
    verification_refs: list[str] = []
    finish_gate: dict[str, Any] = {}
    close_evidence: dict[str, Any] = {}
    graph_kinds = {
        "implementation",
        "merge",
        "merge_evidence",
        "mf_subagent_read_receipt",
        "runtime_context_read_receipt",
        "worker_read_receipt",
        "read_receipt",
        "graph_query_trace",
        "mf_subagent_graph_query_trace",
    }
    for raw_event in events or ():
        if not isinstance(raw_event, Mapping):
            continue
        event = public_contract_revision_payload(raw_event)
        if not _runtime_context_event_same_lineage(
            event,
            runtime_context_id=runtime_context_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            backlog_id=backlog_id,
        ):
            continue
        if not _runtime_context_event_status_ok(event):
            continue
        event_ref = _runtime_context_event_ref(event)
        event_kind = _runtime_context_event_kind_token(event)
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        is_finish_time_worker_attestation = (
            _runtime_context_is_finish_time_worker_attestation_event(
                event,
                event_kind=event_kind,
                payload=payload,
            )
        )
        is_worker_lane_evidence = (
            _runtime_context_is_worker_evidence(event)
            and _runtime_context_event_lane_bound(
                event,
                runtime_context_id=runtime_context_id,
                task_id=task_id,
                fence_token=fence_token,
            )
        )
        if event_kind != "route_action_precheck":
            _collect_route_identity(event)
        if event_kind in {
            "mf_subagent_startup",
            "mf_subagent_startup_gate",
            "worker_startup",
        }:
            timeline_refs.setdefault("startup_event_ref", event_ref)
        if event_kind in {
            "mf_subagent_read_receipt",
            "runtime_context_read_receipt",
            "worker_read_receipt",
            "read_receipt",
        }:
            timeline_refs.setdefault("read_receipt_event_ref", event_ref)
        if (
            event_kind == "route_action_precheck"
            and _runtime_context_event_matches_route_identity(
                event,
                {**derived_route_identity, **explicit_route_identity},
            )
        ):
            timeline_refs.setdefault("route_action_precheck_event_ref", event_ref)
        if _runtime_context_is_implementation_event_kind(event_kind) and event_ref:
            implementation_refs.append(event_ref)
        if event_kind in {
            "mf_subagent_heartbeat",
            "worker_heartbeat",
            "heartbeat",
        }:
            timeline_refs.setdefault("heartbeat_event_ref", event_ref)
        if event_kind in {
            "mf_subagent_progress",
            "worker_progress",
            "progress",
        }:
            timeline_refs.setdefault("progress_event_ref", event_ref)
        if event_kind in {
            "no_progress_timeout",
            "observer_command_no_progress_timeout",
            "dispatch_no_progress",
        }:
            timeline_refs.setdefault("no_progress_timeout_event_ref", event_ref)
        if (
            event_kind in {"verification", "qa_verification", "independent_verification"}
            and event_ref
        ):
            verification_refs.append(event_ref)
        if (
            is_finish_time_worker_attestation
            and is_worker_lane_evidence
            and not finish_gate
        ):
            finish_gate = {
                "payload": public_contract_revision_payload(payload),
                "worker_self_attestation": public_contract_revision_payload(
                    payload.get("finish_time_worker_self_attestation") or {}
                ),
                "worker_self_attestation_gate": {
                    "schema_version": "runtime_context.finish_time_worker_attestation_gate.v1",
                    "status": "passed",
                    "passed": True,
                    "close_satisfying": True,
                },
                "test_results": public_contract_revision_payload(
                    payload.get("test_results") or {}
                ),
                "attestation_event_ref": event_ref,
            }
        if event_kind in {
            "finish_gate",
            "mf_subagent_finish_gate",
            "review_ready",
            "checkpoint",
        }:
            timeline_refs.setdefault("finish_event_ref", event_ref)
            finish_gate = {
                "event_id": event_ref,
                "source_ref": event_ref,
                "checkpoint_id": _runtime_context_deep_text(event, "checkpoint_id"),
                "payload": public_contract_revision_payload(
                    event.get("payload") or {}
                ),
            }
        if event_kind in {"close_ready", "close_request", "finish_request"}:
            timeline_refs.setdefault("close_ready_event_ref", event_ref)
            if not close_evidence:
                close_evidence = {
                    "event_id": event_ref,
                    "source_ref": event_ref,
                    "payload": public_contract_revision_payload(
                        event.get("payload") or {}
                    ),
                }
        if (
            (event_kind in graph_kinds or is_finish_time_worker_attestation)
            and is_worker_lane_evidence
        ):
            ids: list[str] = []
            for key in (
                "trace_id",
                "graph_trace_id",
                "graph_trace_ids",
                "graph_query_trace_ids",
            ):
                for value in _runtime_context_deep_values(event, key):
                    ids.extend(_runtime_context_string_list(value))
            if ids:
                graph_trace_ids.extend(ids)
                if event_ref:
                    graph_trace_event_refs.append(event_ref)
    if implementation_refs:
        timeline_refs["implementation_event_refs"] = _runtime_context_dedupe(
            implementation_refs
        )
    if verification_refs:
        timeline_refs["verification_event_refs"] = _runtime_context_dedupe(
            verification_refs
        )
    return {
        "route_identity": derived_route_identity,
        "timeline_refs": timeline_refs,
        "graph_trace_refs": {
            "source": "task_timeline.same_lineage",
            "query_source": "mf_subagent",
            "worker_role": RUNTIME_CONTEXT_WORKER_ROLE,
            "task_id": task_id,
            "parent_task_id": parent_task_id,
            "runtime_context_id": runtime_context_id,
            "backlog_id": backlog_id,
            "trace_ids": _runtime_context_dedupe(graph_trace_ids),
            "source_details": {
                "event_refs": _runtime_context_dedupe(graph_trace_event_refs),
            },
        },
        "finish_gate": finish_gate,
        "close_evidence": close_evidence,
    }


def _runtime_context_value_present(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _runtime_context_startup_gate_blocker(
    startup_gate: Mapping[str, Any] | None,
) -> dict[str, Any]:
    gate = _runtime_context_mapping(startup_gate)
    if not gate:
        return {}
    refusal = _runtime_context_mapping(gate.get("mf_subagent_startup_refusal"))
    status = _runtime_context_text(gate.get("status") or refusal.get("status")).lower()
    schema_version = _runtime_context_text(
        gate.get("schema_version") or refusal.get("schema_version")
    ).lower()
    gate_kind = _runtime_context_text(gate.get("gate_kind") or refusal.get("gate_kind")).lower()
    blocked_statuses = {
        "blocked",
        "denied",
        "failed",
        "invalid",
        "refused",
        "rejected",
    }
    is_refusal = (
        "refusal" in schema_version
        or "refusal" in gate_kind
        or bool(refusal)
    )
    explicit_rejection = gate.get("allowed") is False or gate.get("ok") is False
    if not (is_refusal or status in blocked_statuses or explicit_rejection):
        return {}
    blockers = (
        _runtime_context_string_list(gate.get("blockers"))
        or _runtime_context_string_list(gate.get("missing_fields"))
        or _runtime_context_string_list(gate.get("reasons"))
        or _runtime_context_string_list(refusal.get("blockers"))
        or _runtime_context_string_list(refusal.get("missing_fields"))
        or _runtime_context_string_list(refusal.get("reasons"))
    )
    reason = _runtime_context_text(
        gate.get("message")
        or gate.get("reason")
        or refusal.get("message")
        or refusal.get("reason")
        or (blockers[0] if blockers else "")
    )
    return {
        "status": "blocked" if status == "blocked" or is_refusal else "invalid",
        "reason": reason or "Startup evidence ref points to a blocked or invalid startup event.",
        "blockers": blockers,
        "schema_version": schema_version,
        "gate_kind": gate_kind,
        "evidence_ref": _runtime_context_text(
            gate.get("event_id") or gate.get("source_ref") or refusal.get("event_id")
        ),
    }


def _runtime_context_nested_finish_gate_payload(
    finish_gate_payload: Mapping[str, Any],
) -> dict[str, Any]:
    nested = _runtime_context_mapping(
        finish_gate_payload.get("mf_subagent_finish_gate")
    )
    if nested:
        return nested
    return _runtime_context_mapping(finish_gate_payload.get("finish_gate"))


def _runtime_context_evidence_refs(
    context: BranchTaskRuntimeContext,
    *,
    runtime_context_id: str,
    contract_revision: BranchRuntimeContractRevision | Mapping[str, Any] | None,
    route_identity: Mapping[str, Any],
    timeline_refs: Mapping[str, Any],
    graph_trace_refs: Mapping[str, Any],
) -> dict[str, Any]:
    revision_ref = _runtime_context_contract_revision_ref(contract_revision)
    return {
        "branch_runtime": {
            "producer": "parallel_branch_runtime",
            "source": "BranchTaskRuntimeContext",
            "source_ref": (
                "parallel_branch_runtime_contexts/"
                f"{context.project_id}/{runtime_context_id}"
            ),
            "runtime_context_id": runtime_context_id,
            "task_id": context.task_id,
        },
        "contract_revision": revision_ref,
        "route_identity": {
            "producer": "route_prompt_contract",
            "source": "contract_revision.route_identity",
            "source_ref": (
                revision_ref.get("revision_id")
                or route_identity.get("route_id")
                or route_identity.get("route_context_hash")
                or ""
            ),
            "route_id": route_identity.get("route_id", ""),
            "route_context_hash": route_identity.get("route_context_hash", ""),
            "prompt_contract_id": route_identity.get("prompt_contract_id", ""),
            "prompt_contract_hash": route_identity.get("prompt_contract_hash", ""),
            "route_token_ref": route_identity.get("route_token_ref", ""),
        },
        "timeline": timeline_refs,
        "graph_trace": graph_trace_refs,
    }


def _runtime_context_current_values(
    context: BranchTaskRuntimeContext,
    *,
    runtime_context_id: str,
    observer_command_id: str,
    route_identity: Mapping[str, Any],
    timeline_refs: Mapping[str, Any],
    graph_trace_refs: Mapping[str, Any],
    target_files: Sequence[str],
    owned_files: Sequence[str],
    acceptance_criteria: Sequence[str],
    startup_gate: Mapping[str, Any],
    finish_gate: Mapping[str, Any],
    close_evidence: Mapping[str, Any],
    generated_at: str = "",
) -> dict[str, Any]:
    parent_task_id = _runtime_context_parent_task_id(context)
    checkpoint_id = context.checkpoint_id or _runtime_context_text(
        finish_gate.get("checkpoint_id")
    )
    verification_event_refs = _runtime_context_string_list(
        timeline_refs.get("verification_event_refs")
    )
    finish_gate_payload = _runtime_context_mapping(finish_gate.get("payload"))
    nested_finish_gate_payload = _runtime_context_nested_finish_gate_payload(
        finish_gate_payload
    )
    worker_self_attestation = public_contract_revision_payload(
        finish_gate.get("worker_self_attestation")
        or finish_gate_payload.get("worker_self_attestation")
        or nested_finish_gate_payload.get("worker_self_attestation")
        or {}
    )
    worker_self_attestation_gate = _runtime_context_mapping(
        finish_gate.get("worker_self_attestation_gate")
        or finish_gate_payload.get("worker_self_attestation_gate")
        or nested_finish_gate_payload.get("worker_self_attestation_gate")
    )
    startup_worker_attestation = public_contract_revision_payload(
        startup_gate.get("worker_self_attestation") or {}
    )
    startup_read_receipt = _runtime_context_mapping(startup_gate.get("read_receipt"))
    startup_worker_session_id = _runtime_context_text(
        startup_gate.get("worker_session_id")
        or startup_worker_attestation.get("worker_session_id")
    )
    startup_worker_transcript_path = _runtime_context_text(
        startup_gate.get("worker_transcript_path")
        or startup_worker_attestation.get("worker_transcript_path")
    )
    startup_worker_transcript_ref = _runtime_context_text(
        startup_gate.get("worker_transcript_ref")
        or startup_gate.get("transcript_ref")
        or startup_worker_attestation.get("worker_transcript_ref")
        or startup_worker_attestation.get("transcript_ref")
        or startup_worker_transcript_path
    )
    startup_harness_type = _runtime_context_text(
        startup_gate.get("harness_type") or startup_worker_attestation.get("harness_type")
    )
    startup_runtime_context_id = _runtime_context_text(
        startup_gate.get("runtime_context_id")
    )
    startup_fence_token_present = bool(
        startup_gate.get("fence_token")
        or startup_gate.get("fence_token_hash")
        or startup_gate.get("fence_token_present")
        or startup_gate.get("fence_token_matches")
    )
    startup_read_receipt_hash = _runtime_context_text(
        startup_gate.get("read_receipt_hash")
        or startup_read_receipt.get("hash")
        or startup_read_receipt.get("read_receipt_hash")
    )
    startup_read_receipt_event_id = _runtime_context_text(
        startup_gate.get("read_receipt_event_id")
        or startup_gate.get("read_receipt_timeline_id")
        or startup_read_receipt.get("event_id")
        or startup_read_receipt.get("timeline_event_id")
        or startup_read_receipt.get("timeline_id")
        or startup_read_receipt.get("id")
    )
    startup_actual_cwd = _runtime_context_text(
        startup_gate.get("actual_cwd") or startup_gate.get("cwd")
    )
    startup_actual_git_root = _runtime_context_text(
        startup_gate.get("actual_git_root") or startup_gate.get("git_root")
    )
    worker_self_attesting = bool(worker_self_attestation) and bool(
        worker_self_attestation.get("worker_self_attesting")
        or worker_self_attestation.get("self_attesting")
    )
    worker_self_attesting = worker_self_attesting and bool(
        worker_self_attestation.get("finish_time_self_attesting")
    )
    worker_self_attesting = worker_self_attesting and not bool(
        worker_self_attestation.get("finish_time_blockers") or []
    )
    if worker_self_attestation_gate:
        gate_status = _runtime_context_text(
            worker_self_attestation_gate.get("status")
        ).lower()
        worker_self_attesting = worker_self_attesting and (
            bool(
                worker_self_attestation_gate.get("passed")
                or worker_self_attestation_gate.get("close_satisfying")
            )
            or gate_status in {"passed", "ok", "success", "succeeded"}
        )
    finish_test_results = public_contract_revision_payload(
        finish_gate.get("test_results")
        or finish_gate_payload.get("test_results")
        or nested_finish_gate_payload.get("test_results")
    )
    if not finish_test_results and isinstance(
        nested_finish_gate_payload.get("evidence"), Mapping
    ):
        finish_test_results = public_contract_revision_payload(
            _runtime_context_mapping(
                nested_finish_gate_payload.get("evidence")
            ).get("test_results")
        )
    worker_self_attesting = worker_self_attesting and _runtime_context_text(
        worker_self_attestation.get("status") or "passed"
    ).lower() in {"passed", "ok", "success", "succeeded"}
    worker_self_attesting = worker_self_attesting and _runtime_context_text(
        worker_self_attestation.get("attestation_phase")
    ).lower() != "startup"
    fence_token_hash = runtime_context_secret_hash(context.fence_token)
    session_token_ref = runtime_context_session_token_ref(context)
    target_project_root = runtime_context_effective_target_project_root(context)
    target_project_root_source = ""
    if _runtime_context_text(context.target_project_root):
        target_project_root_source = "context.target_project_root"
    elif _runtime_context_text(context.worktree_path):
        target_project_root_source = "context.worktree_path"
    return {
        "project_id": context.project_id,
        "governance_project_id": context.governance_project_id or context.project_id,
        "target_project_id": context.target_project_id or context.project_id,
        "target_project_root": target_project_root,
        "target_project_root_source": target_project_root_source,
        "project_root": target_project_root,
        "repo_root": target_project_root,
        "runtime_context_id": runtime_context_id,
        "observer_command_id": observer_command_id,
        "task_id": context.task_id,
        "parent_task_id": parent_task_id,
        "backlog_id": context.backlog_id,
        "worker_role": RUNTIME_CONTEXT_WORKER_ROLE,
        "worker_id": context.worker_id,
        "worker_slot_id": context.worker_slot_id or context.worker_id,
        "actual_host_worker_id": context.actual_host_worker_id,
        "agent_id": context.agent_id,
        "allocation_owner": context.allocation_owner or context.agent_id,
        "attempt": context.attempt,
        "fence_token_present": bool(context.fence_token),
        "fence_token_hash": fence_token_hash,
        "fence_token_redacted": bool(context.fence_token),
        "branch_ref": context.branch_ref,
        "ref_name": context.ref_name,
        "worktree_id": context.worktree_id,
        "worktree_path": context.worktree_path,
        "base_commit": context.base_commit,
        "head_commit": context.head_commit,
        "target_head_commit": context.target_head_commit,
        "snapshot_id": context.snapshot_id,
        "projection_id": context.projection_id,
        "merge_queue_id": context.merge_queue_id,
        "merge_preview_id": context.merge_preview_id,
        "lease_id": context.lease_id,
        "lease_expires_at": context.lease_expires_at,
        "session_token_ref": session_token_ref,
        "session_token_ref_present": bool(session_token_ref),
        "raw_session_token_exposed": False,
        "session_token_lease": runtime_context_session_token_lease_view(
            context,
            now_iso=generated_at,
        ),
        "route_id": _runtime_context_text(route_identity.get("route_id")),
        "route_context_hash": _runtime_context_text(
            route_identity.get("route_context_hash")
        ),
        "prompt_contract_id": _runtime_context_text(
            route_identity.get("prompt_contract_id")
        ),
        "prompt_contract_hash": _runtime_context_text(
            route_identity.get("prompt_contract_hash")
        ),
        "route_token_ref": _runtime_context_text(route_identity.get("route_token_ref")),
        "visible_injection_manifest_hash": _runtime_context_text(
            route_identity.get("visible_injection_manifest_hash")
        ),
        "target_files": list(target_files),
        "owned_files": list(owned_files),
        "acceptance_criteria": list(acceptance_criteria),
        "graph_query_identity": {
            "query_source": "mf_subagent",
            "task_id": context.task_id,
            "parent_task_id": parent_task_id,
            "worker_role": RUNTIME_CONTEXT_WORKER_ROLE,
            "runtime_context_id": runtime_context_id,
            "fence_token_hash": fence_token_hash,
            "fence_token_redacted": bool(context.fence_token),
            "session_token_ref": session_token_ref,
            "session_token_ref_present": bool(session_token_ref),
            "governance_project_id": context.governance_project_id
            or context.project_id,
            "target_project_id": context.target_project_id or context.project_id,
            "target_project_root": target_project_root,
            "project_root": target_project_root,
            "repo_root": target_project_root,
            "required_route_identity_fields": [
                "route_id",
                "route_context_hash",
                "prompt_contract_id",
                "prompt_contract_hash",
                "route_token_ref",
                "visible_injection_manifest_hash",
            ],
            "payload_shape": {
                "project_id": context.project_id,
                "tool": "<graph_query_tool>",
                "args": {},
                "query_source": "mf_subagent",
                "query_purpose": "subagent_context_build",
                "runtime_context_id": runtime_context_id,
                "task_id": context.task_id,
                "parent_task_id": parent_task_id,
                "worker_role": RUNTIME_CONTEXT_WORKER_ROLE,
                "session_token_ref": session_token_ref,
                "target_project_root": target_project_root,
                "project_root": target_project_root,
                "repo_root": target_project_root,
                "route_identity": {
                    "route_id": _runtime_context_text(route_identity.get("route_id")),
                    "route_context_hash": _runtime_context_text(
                        route_identity.get("route_context_hash")
                    ),
                    "prompt_contract_id": _runtime_context_text(
                        route_identity.get("prompt_contract_id")
                    ),
                    "prompt_contract_hash": _runtime_context_text(
                        route_identity.get("prompt_contract_hash")
                    ),
                    "route_token_ref": _runtime_context_text(
                        route_identity.get("route_token_ref")
                    ),
                    "visible_injection_manifest_hash": _runtime_context_text(
                        route_identity.get("visible_injection_manifest_hash")
                    ),
                },
                "raw_session_token_exposed": False,
                "raw_session_token_persisted": False,
                "raw_fence_token_echoed": False,
            },
        },
        "graph_trace_ids": list(graph_trace_refs.get("trace_ids") or []),
        "dispatch_event_ref": timeline_refs.get("dispatch_event_ref", ""),
        "startup_event_ref": timeline_refs.get("startup_event_ref", ""),
        "read_receipt_event_ref": timeline_refs.get("read_receipt_event_ref", ""),
        "route_action_precheck_event_ref": timeline_refs.get(
            "route_action_precheck_event_ref",
            "",
        ),
        "heartbeat_event_ref": timeline_refs.get("heartbeat_event_ref", ""),
        "progress_event_ref": timeline_refs.get("progress_event_ref", ""),
        "no_progress_timeout_event_ref": timeline_refs.get(
            "no_progress_timeout_event_ref",
            "",
        ),
        "finish_event_ref": timeline_refs.get("finish_event_ref", ""),
        "close_ready_event_ref": _runtime_context_text(
            timeline_refs.get("close_ready_event_ref")
            or close_evidence.get("event_id")
            or close_evidence.get("source_ref")
        ),
        "implementation_event_refs": list(
            timeline_refs.get("implementation_event_refs") or []
        ),
        "changed_files": list(timeline_refs.get("changed_files") or []),
        "owned_changed_files": list(timeline_refs.get("owned_changed_files") or []),
        "worker_changed_files": list(timeline_refs.get("worker_changed_files") or []),
        "verification_event_refs": verification_event_refs,
        "startup_gate_ref": _runtime_context_text(
            startup_gate.get("event_id")
            or startup_gate.get("source_ref")
            or timeline_refs.get("startup_event_ref")
        ),
        "startup_runtime_context_id": startup_runtime_context_id,
        "startup_fence_token_present": startup_fence_token_present,
        "startup_worker_session_id": startup_worker_session_id,
        "startup_worker_transcript_path": startup_worker_transcript_path,
        "startup_worker_transcript_ref": startup_worker_transcript_ref,
        "startup_harness_type": startup_harness_type,
        "startup_filer_principal": _runtime_context_text(
            startup_gate.get("filer_principal") or startup_gate.get("actor")
        ),
        "startup_route_id": _runtime_context_text(startup_gate.get("route_id")),
        "startup_route_context_hash": _runtime_context_text(
            startup_gate.get("route_context_hash")
        ),
        "startup_prompt_contract_id": _runtime_context_text(
            startup_gate.get("prompt_contract_id")
        ),
        "startup_prompt_contract_hash": _runtime_context_text(
            startup_gate.get("prompt_contract_hash")
        ),
        "startup_route_token_ref": _runtime_context_text(
            startup_gate.get("route_token_ref")
        ),
        "startup_read_receipt_hash": startup_read_receipt_hash,
        "startup_read_receipt_event_id": startup_read_receipt_event_id,
        "startup_actual_cwd": startup_actual_cwd,
        "startup_actual_git_root": startup_actual_git_root,
        "finish_gate_ref": _runtime_context_text(
            finish_gate.get("event_id")
            or finish_gate.get("source_ref")
            or timeline_refs.get("finish_event_ref")
        ),
        "worker_self_attesting": worker_self_attesting,
        "worker_self_attestation": worker_self_attestation,
        "checkpoint_id": checkpoint_id,
        "test_results": finish_test_results,
    }


_RUNTIME_CONTEXT_GATE_REQUIREMENTS: dict[str, tuple[dict[str, str], ...]] = {
    "dispatch": (
        {
            "field": "task_id",
            "expected_source": "branch_runtime.context.task_id",
            "producer": "parallel_branch_runtime",
            "consumer": "mf_subagent_contract.validate_mf_subagent_dispatch_gate",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "parent_task_id",
            "expected_source": "branch_runtime.context.parent_task_id",
            "producer": "parallel_branch_runtime",
            "consumer": "mf_subagent_contract.validate_mf_subagent_dispatch_gate",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "worker_role",
            "expected_source": "runtime_context.role_policy",
            "producer": "parallel_branch_runtime",
            "consumer": "mf_subagent_contract.validate_mf_subagent_dispatch_gate",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "branch_ref",
            "expected_source": "branch_runtime.context.branch_ref",
            "producer": "parallel_branch_runtime",
            "consumer": "mf_subagent_contract.validate_mf_subagent_dispatch_gate",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "worktree_path",
            "expected_source": "branch_runtime.context.worktree_path",
            "producer": "parallel_branch_runtime",
            "consumer": "mf_subagent_contract.validate_mf_subagent_dispatch_gate",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "base_commit",
            "expected_source": "branch_runtime.context.base_commit",
            "producer": "parallel_branch_runtime",
            "consumer": "mf_subagent_contract.validate_mf_subagent_dispatch_gate",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "target_head_commit",
            "expected_source": "branch_runtime.context.target_head_commit",
            "producer": "parallel_branch_runtime",
            "consumer": "mf_subagent_contract.validate_mf_subagent_dispatch_gate",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "merge_queue_id",
            "expected_source": "branch_runtime.context.merge_queue_id",
            "producer": "parallel_branch_runtime",
            "consumer": "mf_subagent_contract.validate_mf_subagent_dispatch_gate",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "fence_token",
            "expected_source": "branch_runtime.context.fence_token",
            "producer": "parallel_branch_runtime",
            "consumer": "mf_subagent_contract.validate_mf_subagent_dispatch_gate",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "route_context_hash",
            "expected_source": "route_prompt_contract.route_context_hash",
            "producer": "route_prompt_contract",
            "consumer": "mf_subagent_contract.validate_mf_subagent_dispatch_gate",
            "evidence_ref": "route_identity",
        },
        {
            "field": "prompt_contract_id",
            "expected_source": "route_prompt_contract.prompt_contract_id",
            "producer": "route_prompt_contract",
            "consumer": "mf_subagent_contract.validate_mf_subagent_dispatch_gate",
            "evidence_ref": "route_identity",
        },
        {
            "field": "prompt_contract_hash",
            "expected_source": "route_prompt_contract.prompt_contract_hash",
            "producer": "route_prompt_contract",
            "consumer": "mf_subagent_contract.validate_mf_subagent_dispatch_gate",
            "evidence_ref": "route_identity",
        },
        {
            "field": "route_token_ref",
            "expected_source": "route_prompt_contract.route_token_ref",
            "producer": "route_prompt_contract",
            "consumer": "mf_subagent_contract.validate_mf_subagent_dispatch_gate",
            "evidence_ref": "route_identity",
        },
        {
            "field": "target_files",
            "expected_source": "contract_revision.payload.target_files",
            "producer": "parallel_branch_runtime",
            "consumer": "observer_runtime_text_prepare",
            "evidence_ref": "contract_revision",
        },
    ),
    "startup": (
        {
            "field": "runtime_context_id",
            "expected_source": "branch_runtime.context.runtime_context_id",
            "producer": "parallel_branch_runtime",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "observer_command_id",
            "expected_source": "contract_revision.payload.observer_command_id",
            "producer": "observer_runtime_text_prepare",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "contract_revision",
        },
        {
            "field": "parent_task_id",
            "expected_source": "branch_runtime.context.parent_task_id",
            "producer": "parallel_branch_runtime",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "worker_role",
            "expected_source": "runtime_context.role_policy",
            "producer": "parallel_branch_runtime",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "worker_id",
            "expected_source": "branch_runtime.context.worker_id",
            "producer": "parallel_branch_runtime",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "agent_id",
            "expected_source": "branch_runtime.context.agent_id",
            "producer": "parallel_branch_runtime",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "fence_token",
            "expected_source": "branch_runtime.context.fence_token",
            "producer": "parallel_branch_runtime",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "worktree_path",
            "expected_source": "branch_runtime.context.worktree_path",
            "producer": "parallel_branch_runtime",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "base_commit",
            "expected_source": "branch_runtime.context.base_commit",
            "producer": "parallel_branch_runtime",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "target_head_commit",
            "expected_source": "branch_runtime.context.target_head_commit",
            "producer": "parallel_branch_runtime",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "merge_queue_id",
            "expected_source": "branch_runtime.context.merge_queue_id",
            "producer": "parallel_branch_runtime",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "route_id",
            "expected_source": "route_prompt_contract.route_id",
            "producer": "route_prompt_contract",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "route_identity",
        },
        {
            "field": "route_context_hash",
            "expected_source": "route_prompt_contract.route_context_hash",
            "producer": "route_prompt_contract",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "route_identity",
        },
        {
            "field": "prompt_contract_id",
            "expected_source": "route_prompt_contract.prompt_contract_id",
            "producer": "route_prompt_contract",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "route_identity",
        },
        {
            "field": "prompt_contract_hash",
            "expected_source": "route_prompt_contract.prompt_contract_hash",
            "producer": "route_prompt_contract",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "route_identity",
        },
        {
            "field": "route_token_ref",
            "expected_source": "route_prompt_contract.route_token_ref",
            "producer": "route_prompt_contract",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "route_identity",
        },
        {
            "field": "visible_injection_manifest_hash",
            "expected_source": "route_prompt_contract.visible_injection_manifest_hash",
            "producer": "route_prompt_contract",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "route_identity",
        },
        {
            "field": "target_files",
            "expected_source": "contract_revision.payload.target_files",
            "producer": "parallel_branch_runtime",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "contract_revision",
        },
        {
            "field": "startup_event_ref",
            "expected_source": "task_timeline.mf_subagent_startup",
            "producer": "mf_subagent_worker",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "timeline",
        },
        {
            "field": "read_receipt_event_ref",
            "expected_source": "task_timeline.mf_subagent_read_receipt",
            "producer": "mf_subagent_worker",
            "consumer": "record_mf_subagent_startup",
            "evidence_ref": "timeline",
        },
        {
            "field": "startup_runtime_context_id",
            "expected_source": "task_timeline.mf_subagent_startup.runtime_context_id",
            "producer": "mf_subagent_worker",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "timeline",
        },
        {
            "field": "startup_fence_token_present",
            "expected_source": "task_timeline.mf_subagent_startup.fence_token",
            "producer": "mf_subagent_worker",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "timeline",
        },
        {
            "field": "startup_worker_session_id",
            "expected_source": "task_timeline.mf_subagent_startup.worker_session_id",
            "producer": "mf_subagent_worker",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "timeline",
        },
        {
            "field": "startup_worker_transcript_ref",
            "expected_source": "task_timeline.mf_subagent_startup.worker_transcript_ref_or_path",
            "producer": "mf_subagent_worker",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "timeline",
        },
        {
            "field": "startup_harness_type",
            "expected_source": "task_timeline.mf_subagent_startup.harness_type",
            "producer": "mf_subagent_worker",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "timeline",
        },
        {
            "field": "startup_route_id",
            "expected_source": "task_timeline.mf_subagent_startup.route_id",
            "producer": "mf_subagent_worker",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "timeline",
        },
        {
            "field": "startup_route_context_hash",
            "expected_source": "task_timeline.mf_subagent_startup.route_context_hash",
            "producer": "mf_subagent_worker",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "timeline",
        },
        {
            "field": "startup_prompt_contract_id",
            "expected_source": "task_timeline.mf_subagent_startup.prompt_contract_id",
            "producer": "mf_subagent_worker",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "timeline",
        },
        {
            "field": "startup_prompt_contract_hash",
            "expected_source": "task_timeline.mf_subagent_startup.prompt_contract_hash",
            "producer": "mf_subagent_worker",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "timeline",
        },
        {
            "field": "startup_route_token_ref",
            "expected_source": "task_timeline.mf_subagent_startup.route_token_ref",
            "producer": "mf_subagent_worker",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "timeline",
        },
        {
            "field": "startup_read_receipt_hash",
            "expected_source": "task_timeline.mf_subagent_startup.read_receipt_hash",
            "producer": "mf_subagent_worker",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "timeline",
        },
        {
            "field": "startup_read_receipt_event_id",
            "expected_source": "task_timeline.mf_subagent_startup.read_receipt_event_id",
            "producer": "mf_subagent_worker",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "timeline",
        },
    ),
    "graph_query": (
        {
            "field": "task_id",
            "expected_source": "branch_runtime.context.task_id",
            "producer": "parallel_branch_runtime",
            "consumer": "graph_query_trace.validate_mf_subagent_identity",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "parent_task_id",
            "expected_source": "branch_runtime.context.parent_task_id",
            "producer": "parallel_branch_runtime",
            "consumer": "graph_query_trace.validate_mf_subagent_identity",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "worker_role",
            "expected_source": "runtime_context.role_policy",
            "producer": "parallel_branch_runtime",
            "consumer": "graph_query_trace.validate_mf_subagent_identity",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "fence_token",
            "expected_source": "branch_runtime.context.fence_token",
            "producer": "parallel_branch_runtime",
            "consumer": "graph_query_trace.validate_mf_subagent_identity",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "governance_project_id",
            "expected_source": "branch_runtime.context.governance_project_id",
            "producer": "parallel_branch_runtime",
            "consumer": "graph_query_trace.validate_mf_subagent_identity",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "target_project_id",
            "expected_source": "branch_runtime.context.target_project_id",
            "producer": "parallel_branch_runtime",
            "consumer": "graph_query_trace.validate_mf_subagent_identity",
            "evidence_ref": "branch_runtime",
        },
    ),
    "finish": (
        {
            "field": "task_id",
            "expected_source": "branch_runtime.context.task_id",
            "producer": "parallel_branch_runtime",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "backlog_id",
            "expected_source": "branch_runtime.context.backlog_id",
            "producer": "parallel_branch_runtime",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "branch_ref",
            "expected_source": "branch_runtime.context.branch_ref",
            "producer": "parallel_branch_runtime",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "worktree_path",
            "expected_source": "branch_runtime.context.worktree_path",
            "producer": "parallel_branch_runtime",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "base_commit",
            "expected_source": "branch_runtime.context.base_commit",
            "producer": "parallel_branch_runtime",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "target_head_commit",
            "expected_source": "branch_runtime.context.target_head_commit",
            "producer": "parallel_branch_runtime",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "merge_queue_id",
            "expected_source": "branch_runtime.context.merge_queue_id",
            "producer": "parallel_branch_runtime",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "fence_token",
            "expected_source": "branch_runtime.context.fence_token",
            "producer": "parallel_branch_runtime",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "startup_event_ref",
            "expected_source": "task_timeline.mf_subagent_startup",
            "producer": "task_timeline",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "timeline",
        },
        {
            "field": "read_receipt_event_ref",
            "expected_source": "task_timeline.mf_subagent_read_receipt",
            "producer": "task_timeline",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "timeline",
        },
        {
            "field": "worker_self_attesting",
            "expected_source": "worker_transcript_verify.finish_time_worker_self_attestation",
            "producer": "worker_transcript_verify",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "finish_gate",
        },
        {
            "field": "graph_trace_ids",
            "expected_source": "graph_query_trace.trace_ids",
            "producer": "graph_query_trace",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "graph_trace",
        },
        {
            "field": "prompt_contract_hash",
            "expected_source": "route_prompt_contract.prompt_contract_hash",
            "producer": "route_prompt_contract",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "route_identity",
        },
        {
            "field": "route_token_ref",
            "expected_source": "route_prompt_contract.route_token_ref",
            "producer": "route_prompt_contract",
            "consumer": "mf_subagent_contract.validate_mf_subagent_finish_gate",
            "evidence_ref": "route_identity",
        },
    ),
    "close": (
        {
            "field": "checkpoint_id",
            "expected_source": "branch_runtime.context.checkpoint_or_finish_gate",
            "producer": "parallel_branch_runtime",
            "consumer": "close_gate",
            "evidence_ref": "branch_runtime",
        },
        {
            "field": "finish_gate_ref",
            "expected_source": "task_timeline.finish_gate",
            "producer": "task_timeline",
            "consumer": "close_gate",
            "evidence_ref": "timeline",
        },
        {
            "field": "route_action_precheck_event_ref",
            "expected_source": "task_timeline.route_action_precheck",
            "producer": "task_timeline",
            "consumer": "close_gate",
            "evidence_ref": "timeline",
        },
        {
            "field": "verification_event_refs",
            "expected_source": "task_timeline.verification",
            "producer": "task_timeline",
            "consumer": "close_gate",
            "evidence_ref": "timeline",
        },
        {
            "field": "graph_trace_ids",
            "expected_source": "graph_query_trace.trace_ids",
            "producer": "graph_query_trace",
            "consumer": "close_gate",
            "evidence_ref": "graph_trace",
        },
        {
            "field": "worker_self_attesting",
            "expected_source": "worker_transcript_verify.finish_time_worker_self_attestation",
            "producer": "worker_transcript_verify",
            "consumer": "close_gate",
            "evidence_ref": "finish_gate",
        },
        {
            "field": "route_context_hash",
            "expected_source": "route_prompt_contract.route_context_hash",
            "producer": "route_prompt_contract",
            "consumer": "close_gate",
            "evidence_ref": "route_identity",
        },
        {
            "field": "route_token_ref",
            "expected_source": "route_prompt_contract.route_token_ref",
            "producer": "route_prompt_contract",
            "consumer": "close_gate",
            "evidence_ref": "route_identity",
        },
        {
            "field": "prompt_contract_hash",
            "expected_source": "route_prompt_contract.prompt_contract_hash",
            "producer": "route_prompt_contract",
            "consumer": "close_gate",
            "evidence_ref": "route_identity",
        },
        {
            "field": "merge_queue_id",
            "expected_source": "branch_runtime.context.merge_queue_id",
            "producer": "parallel_branch_runtime",
            "consumer": "close_gate",
            "evidence_ref": "branch_runtime",
        },
    ),
}


def _runtime_context_gate_field_view(
    *,
    gate: str,
    requirement: Mapping[str, str],
    values: Mapping[str, Any],
    evidence_refs_inspected: Sequence[str] = (),
) -> tuple[str, dict[str, Any], RuntimeContextMissingField | None]:
    field_name = requirement["field"]
    value = values.get(field_name)
    if field_name == "fence_token":
        token_hash = _runtime_context_text(values.get("fence_token_hash"))
        present = bool(values.get("fence_token_present")) or bool(token_hash)
        field_view = {
            "value": "redacted" if present else "",
            "present": present,
            "value_redacted": True,
            "fence_token_hash": token_hash,
            "expected_source": requirement["expected_source"],
            "producer": requirement["producer"],
            "consumer": requirement["consumer"],
            "evidence_ref": requirement["evidence_ref"],
            "evidence_refs_inspected": list(evidence_refs_inspected),
        }
        if present:
            return field_name, field_view, None
        missing = RuntimeContextMissingField(
            gate=gate,
            field=field_name,
            expected_source=requirement["expected_source"],
            producer=requirement["producer"],
            consumer=requirement["consumer"],
            evidence_ref=requirement["evidence_ref"],
            evidence_refs_inspected=tuple(evidence_refs_inspected),
        )
        return field_name, field_view, missing
    present = _runtime_context_value_present(value)
    field_view = {
        "value": value,
        "present": present,
        "expected_source": requirement["expected_source"],
        "producer": requirement["producer"],
        "consumer": requirement["consumer"],
        "evidence_ref": requirement["evidence_ref"],
        "evidence_refs_inspected": list(evidence_refs_inspected),
    }
    if present:
        return field_name, field_view, None
    missing = RuntimeContextMissingField(
        gate=gate,
        field=field_name,
        expected_source=requirement["expected_source"],
        producer=requirement["producer"],
        consumer=requirement["consumer"],
        evidence_ref=requirement["evidence_ref"],
        evidence_refs_inspected=tuple(evidence_refs_inspected),
    )
    return field_name, field_view, missing


def build_runtime_context_gate_inputs_view(
    current_view: Mapping[str, Any],
) -> dict[str, Any]:
    """Build gate-by-gate current values and missing-field diagnostics."""

    values = _runtime_context_mapping(current_view.get("current_values"))
    current_evidence_refs = _runtime_context_mapping(current_view.get("evidence_refs"))
    evidence_refs = {
        "branch_runtime": _runtime_context_mapping(
            current_evidence_refs.get("branch_runtime")
        ),
        "route_identity": _runtime_context_mapping(
            current_evidence_refs.get("route_identity")
        ),
        "timeline": _runtime_context_mapping(current_view.get("timeline_refs")),
        "graph_trace": _runtime_context_mapping(current_view.get("graph_trace_refs")),
        "finish_gate": {
            "producer": "task_timeline",
            "source": "task_timeline.finish_gate",
            "payload": public_contract_revision_payload(
                current_view.get("finish_gate") or {}
            ),
        },
        "close_evidence": {
            "producer": "task_timeline",
            "source": "task_timeline.close_ready",
            "payload": public_contract_revision_payload(
                current_view.get("close_evidence") or {}
            ),
        },
    }
    gates: dict[str, Any] = {}
    all_missing: list[dict[str, Any]] = []
    for gate, requirements in _RUNTIME_CONTEXT_GATE_REQUIREMENTS.items():
        fields: dict[str, Any] = {}
        missing: list[dict[str, Any]] = []
        for requirement in requirements:
            field_name, field_view, missing_field = _runtime_context_gate_field_view(
                gate=gate,
                requirement=requirement,
                values=values,
                evidence_refs_inspected=tuple(sorted(evidence_refs.keys())),
            )
            fields[field_name] = field_view
            if missing_field is not None:
                missing.append(missing_field.to_dict())
        gates[gate] = {
            "gate": gate,
            "required_fields": [item["field"] for item in requirements],
            "fields": fields,
            "missing": missing,
            "ready": not missing,
            "status": "ready" if not missing else "missing_required_fields",
        }
        all_missing.extend(missing)
    return {
        "schema_version": RUNTIME_CONTEXT_GATE_INPUTS_SCHEMA_VERSION,
        "runtime_context_id": _runtime_context_text(
            current_view.get("runtime_context_id")
        ),
        "observer_command_id": values.get("observer_command_id", ""),
        "task_id": values.get("task_id", ""),
        "route_id": values.get("route_id", ""),
        "route_context_hash": values.get("route_context_hash", ""),
        "prompt_contract_id": values.get("prompt_contract_id", ""),
        "prompt_contract_hash": values.get("prompt_contract_hash", ""),
        "route_token_ref": values.get("route_token_ref", ""),
        "visible_injection_manifest_hash": values.get(
            "visible_injection_manifest_hash",
            "",
        ),
        "target_files": list(values.get("target_files") or []),
        "owned_files": list(
            values.get("owned_files") or values.get("target_files") or []
        ),
        "generated_at": current_view.get("generated_at", ""),
        "evidence_refs": evidence_refs,
        "gates": gates,
        "missing": all_missing,
        "status": "ready" if not all_missing else "missing_required_fields",
    }


def build_runtime_context_current_view(
    context: BranchTaskRuntimeContext,
    *,
    contract_revision: BranchRuntimeContractRevision | Mapping[str, Any] | None = None,
    route_identity: Mapping[str, Any] | None = None,
    route_gate: Mapping[str, Any] | None = None,
    timeline_refs: Mapping[str, Any] | None = None,
    graph_trace_refs: Mapping[str, Any] | Sequence[str] | None = None,
    startup_gate: Mapping[str, Any] | None = None,
    finish_gate: Mapping[str, Any] | None = None,
    close_evidence: Mapping[str, Any] | None = None,
    target_files: Sequence[str] | None = None,
    acceptance_criteria: Sequence[str] | None = None,
    required_evidence: Sequence[str] | None = None,
    timeline_events: Sequence[Mapping[str, Any]] | None = None,
    lane_required_clauses: Sequence[str | Mapping[str, Any]] | None = None,
    generated_at: str = "",
) -> dict[str, Any]:
    """Build the canonical current-state projection for runtime-context gates."""

    runtime_context_id = runtime_context_id_for_branch_context(context)
    revision_payload = _runtime_context_revision_payload(contract_revision)
    route = _runtime_context_route_identity(
        contract_revision=contract_revision,
        route_identity=route_identity,
        route_gate=route_gate,
    )
    parent_task_id = _runtime_context_parent_task_id(context)
    derived = _runtime_context_timeline_derived_evidence(
        timeline_events,
        runtime_context_id=runtime_context_id,
        task_id=context.task_id,
        parent_task_id=parent_task_id,
        backlog_id=context.backlog_id,
        fence_token=context.fence_token,
        route_identity=route,
    )
    terminal_dispatch_blockers = _runtime_context_terminal_dispatch_blockers(
        timeline_events,
        runtime_context_id=runtime_context_id,
        task_id=context.task_id,
        parent_task_id=parent_task_id,
        backlog_id=context.backlog_id,
    )
    for key, value in _runtime_context_mapping(derived.get("route_identity")).items():
        if value and not route.get(key):
            route[key] = value
    timeline = _runtime_context_timeline_refs(
        {
            **public_contract_revision_payload(timeline_refs or {}),
            **_runtime_context_mapping(derived.get("timeline_refs")),
        }
    )
    explicit_graph_trace = _runtime_context_graph_trace_refs(graph_trace_refs)
    derived_graph_trace = _runtime_context_graph_trace_refs(
        derived.get("graph_trace_refs")
    )
    graph_trace = dict(explicit_graph_trace)
    graph_trace["trace_ids"] = _runtime_context_dedupe(
        list(derived_graph_trace.get("trace_ids") or [])
        + list(explicit_graph_trace.get("trace_ids") or [])
    )
    if not graph_trace.get("source_details"):
        graph_trace["source_details"] = derived_graph_trace.get("source_details", {})
    startup = _runtime_context_startup_gate_payload(startup_gate or {})
    startup_target_file_values = _runtime_context_revision_scope_string_list(
        startup,
        "target_files",
    )
    startup_owned_file_values = _runtime_context_revision_scope_string_list(
        startup,
        "owned_files",
        "write_scope",
    )
    revision_target_file_values = _runtime_context_revision_scope_string_list(
        revision_payload,
        "target_files",
    )
    revision_owned_file_values = _runtime_context_revision_scope_string_list(
        revision_payload,
        "owned_files",
        "write_scope",
    )
    runtime_target_file_values = _runtime_context_string_list(context.target_files)
    runtime_owned_file_values = _runtime_context_string_list(context.owned_files)
    target_file_values = _runtime_context_dedupe(
        list(target_files or ())
        or revision_target_file_values
        or runtime_target_file_values
        or revision_owned_file_values
        or runtime_owned_file_values
        or startup_target_file_values
        or startup_owned_file_values
    )
    owned_file_values = _runtime_context_dedupe(
        revision_owned_file_values
        or runtime_owned_file_values
        or startup_owned_file_values
        or target_file_values
    )
    acceptance_values = _runtime_context_dedupe(
        list(acceptance_criteria or ())
        or _runtime_context_revision_string_list(
            revision_payload,
            "acceptance_criteria",
            "acceptance",
        )
        or _runtime_context_revision_string_list(
            startup,
            "acceptance_criteria",
            "acceptance",
        )
    )
    required_evidence_values = _runtime_context_dedupe(
        list(required_evidence or ())
        or _runtime_context_revision_string_list(
            revision_payload,
            "required_evidence",
            "required_evidence_ids",
        )
        or _runtime_context_revision_string_list(
            startup,
            "required_evidence",
            "required_evidence_ids",
        )
    )
    finish = public_contract_revision_payload(
        finish_gate or derived.get("finish_gate") or {}
    )
    close = public_contract_revision_payload(
        close_evidence or derived.get("close_evidence") or {}
    )
    observer_command_id = _runtime_context_text(
        revision_payload.get("observer_command_id")
        or startup.get("observer_command_id")
        or finish.get("observer_command_id")
        or close.get("observer_command_id")
    )
    current_values = _runtime_context_current_values(
        context,
        runtime_context_id=runtime_context_id,
        observer_command_id=observer_command_id,
        route_identity=route,
        timeline_refs=timeline,
        graph_trace_refs=graph_trace,
        target_files=target_file_values,
        owned_files=owned_file_values,
        acceptance_criteria=acceptance_values,
        startup_gate=startup,
        finish_gate=finish,
        close_evidence=close,
        generated_at=generated_at,
    )
    current_values["terminal_dispatch_blockers"] = terminal_dispatch_blockers
    current_values["terminal_dispatch_blocker_count"] = len(terminal_dispatch_blockers)
    current_values["terminal_dispatch_blocker_ref"] = (
        terminal_dispatch_blockers[0].get("event_ref", "")
        if terminal_dispatch_blockers
        else ""
    )
    current_values["merge_queue_projection"] = public_contract_revision_payload(
        revision_payload.get("merge_queue_projection")
    )
    current_values["ordered_merge_dependencies"] = _runtime_context_string_list(
        revision_payload.get("ordered_merge_dependencies")
    )
    current_values["dependency_merge_commit"] = _runtime_context_text(
        revision_payload.get("dependency_merge_commit")
    )
    current_values["close_precheck"] = public_contract_revision_payload(
        revision_payload.get("close_precheck")
    )
    evidence_refs = _runtime_context_evidence_refs(
        context,
        runtime_context_id=runtime_context_id,
        contract_revision=contract_revision,
        route_identity=route,
        timeline_refs=timeline,
        graph_trace_refs=graph_trace,
    )
    return {
        "schema_version": RUNTIME_CONTEXT_CURRENT_SCHEMA_VERSION,
        "runtime_context_id": runtime_context_id,
        "project_id": context.project_id,
        "task_id": context.task_id,
        "generated_at": generated_at or utc_now(),
        "source_boundaries": {
            "route_prompt_identity": "route_prompt_contract",
            "branch_worktree_fence": "parallel_branch_runtime",
            "timeline_refs": "task_timeline",
            "graph_trace_refs": "graph_query_trace",
            "raw_source_data_copied": False,
        },
        "identity": {
            key: current_values[key]
            for key in (
                "project_id",
                "governance_project_id",
                "target_project_id",
                "target_project_root",
                "runtime_context_id",
                "observer_command_id",
                "task_id",
                "parent_task_id",
                "backlog_id",
                "worker_role",
                "worker_id",
                "worker_slot_id",
                "actual_host_worker_id",
                "agent_id",
                "attempt",
                "fence_token_present",
                "fence_token_hash",
                "fence_token_redacted",
            )
        },
        "branch": {
            key: current_values[key]
            for key in (
                "branch_ref",
                "ref_name",
                "worktree_id",
                "worktree_path",
                "base_commit",
                "head_commit",
                "target_head_commit",
                "snapshot_id",
                "projection_id",
                "merge_queue_id",
                "merge_preview_id",
            )
        },
        "route_identity": route,
        "work": {
            "target_files": target_file_values,
            "owned_files": owned_file_values,
            "acceptance_criteria": acceptance_values,
            "required_evidence": required_evidence_values,
        },
        "graph_query_identity": current_values["graph_query_identity"],
        "timeline_refs": timeline,
        "graph_trace_refs": graph_trace,
        "lane_plan": build_runtime_context_lane_plan_view(
            timeline_events or (),
            required_clauses=lane_required_clauses or required_evidence_values or None,
            lane_id=context.task_id,
            generated_at=generated_at,
        ),
        "terminal_dispatch_blockers": terminal_dispatch_blockers,
        "startup_gate": startup,
        "finish_gate": finish,
        "close_evidence": close,
        "contract_revision": _runtime_context_contract_revision_ref(contract_revision),
        "evidence_refs": evidence_refs,
        "current_values": current_values,
        "privacy_boundary": {
            "raw_private_context_exposed": False,
            "raw_source_of_truth_copied": False,
            "redacted_context_sources": [
                "raw_private_route_body",
                "observer_only_context",
                "private_context",
                "unmanifested_prompt_text",
            ],
        },
    }


def build_runtime_context_close_gate_view(
    current_view: Mapping[str, Any],
    gate_inputs_view: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the close/merge checklist bound to timeline and graph refs."""

    values = _runtime_context_mapping(current_view.get("current_values"))
    gate_inputs = (
        dict(gate_inputs_view)
        if isinstance(gate_inputs_view, Mapping)
        else build_runtime_context_gate_inputs_view(current_view)
    )
    close_gate = _runtime_context_mapping(
        _runtime_context_mapping(gate_inputs.get("gates")).get("close")
    )
    close_missing = list(close_gate.get("missing") or [])
    terminal_dispatch_blockers = list(
        current_view.get("terminal_dispatch_blockers")
        or values.get("terminal_dispatch_blockers")
        or []
    )
    if terminal_dispatch_blockers:
        close_missing.append(
            {
                "gate": "terminal_dispatch",
                "field": "terminal_dispatch_blocker",
                "expected_source": "task_timeline.record_blocker",
                "producer": "observer",
                "consumer": "runtime_context.close_gate",
                "evidence_ref": terminal_dispatch_blockers[0].get("event_ref", ""),
                "message": "Terminal dispatch blocker must be resolved or audit-archived before close.",
            }
        )
    checklist_fields = (
        ("observer_command", "observer_command_id"),
        ("startup_evidence", "startup_event_ref"),
        ("read_receipt", "read_receipt_event_ref"),
        ("route_action_precheck", "route_action_precheck_event_ref"),
        ("finish_time_worker_attestation", "worker_self_attesting"),
        ("finish_gate", "finish_gate_ref"),
        ("close_ready", "close_ready_event_ref"),
        ("checkpoint", "checkpoint_id"),
        ("verification", "verification_event_refs"),
        ("graph_trace", "graph_trace_ids"),
        ("route_context_hash", "route_context_hash"),
        ("prompt_contract_hash", "prompt_contract_hash"),
        ("route_token_ref", "route_token_ref"),
        ("merge_queue", "merge_queue_id"),
    )
    checklist = []
    startup_blocker = _runtime_context_startup_gate_blocker(
        _runtime_context_mapping(current_view.get("startup_gate"))
    )
    if startup_blocker and not any(
        item.get("field") in {"startup_event_ref", "startup_gate_ref"}
        for item in close_missing
        if isinstance(item, Mapping)
    ):
        close_missing.append(
            {
                "gate": "startup",
                "field": "startup_event_ref",
                "status": startup_blocker["status"],
                "expected_source": "task_timeline.mf_subagent_startup",
                "producer": "mf_sub",
                "consumer": "runtime_context.close_gate",
                "evidence_ref": (
                    startup_blocker.get("evidence_ref")
                    or values.get("startup_event_ref")
                    or values.get("startup_gate_ref")
                ),
                "message": startup_blocker["reason"],
                "blockers": list(startup_blocker.get("blockers") or []),
            }
        )
    for item_id, field_name in checklist_fields:
        value = values.get(field_name)
        status = "present" if _runtime_context_value_present(value) else "missing"
        item = {
            "id": item_id,
            "field": field_name,
            "status": status,
            "value": value,
        }
        if item_id == "startup_evidence" and startup_blocker:
            item.update(
                {
                    "status": startup_blocker["status"],
                    "valid": False,
                    "message": startup_blocker["reason"],
                    "blockers": list(startup_blocker.get("blockers") or []),
                    "evidence_ref": (
                        startup_blocker.get("evidence_ref")
                        or value
                        or values.get("startup_gate_ref")
                    ),
                }
            )
        checklist.append(item)
    return {
        "schema_version": RUNTIME_CONTEXT_CLOSE_GATE_VIEW_SCHEMA_VERSION,
        "runtime_context_id": current_view.get("runtime_context_id", ""),
        "observer_command_id": values.get("observer_command_id", ""),
        "task_id": values.get("task_id", ""),
        "merge_queue_id": values.get("merge_queue_id", ""),
        "checkpoint_id": values.get("checkpoint_id", ""),
        "finish_gate_ref": values.get("finish_gate_ref", ""),
        "close_ready_event_ref": values.get("close_ready_event_ref", ""),
        "route_action_precheck_event_ref": values.get(
            "route_action_precheck_event_ref",
            "",
        ),
        "route_context_hash": values.get("route_context_hash", ""),
        "prompt_contract_hash": values.get("prompt_contract_hash", ""),
        "route_token_ref": values.get("route_token_ref", ""),
        "graph_trace_ids": list(values.get("graph_trace_ids") or []),
        "checklist": checklist,
        "missing": close_missing,
        "terminal_dispatch_blockers": terminal_dispatch_blockers,
        "ready": not close_missing and not terminal_dispatch_blockers,
        "status": (
            "terminal_dispatch_blocked"
            if terminal_dispatch_blockers
            else ("ready" if not close_missing else "missing_required_fields")
        ),
        "evidence_refs": {
            "timeline": current_view.get("timeline_refs", {}),
            "graph_trace": current_view.get("graph_trace_refs", {}),
            "branch_runtime": _runtime_context_mapping(
                current_view.get("evidence_refs")
            ).get("branch_runtime", {}),
            "route_identity": _runtime_context_mapping(
                current_view.get("evidence_refs")
            ).get("route_identity", {}),
            "finish_gate": {
                "producer": "task_timeline",
                "source": "task_timeline.finish_gate",
                "payload": public_contract_revision_payload(
                    current_view.get("finish_gate") or {}
                ),
            },
            "close_evidence": {
                "producer": "task_timeline",
                "source": "task_timeline.close_ready",
                "payload": public_contract_revision_payload(
                    current_view.get("close_evidence") or {}
                ),
            },
            "route_action_precheck": {
                "producer": "task_timeline",
                "source": "task_timeline.route_action_precheck",
                "source_ref": _runtime_context_text(
                    values.get("route_action_precheck_event_ref")
                ),
            },
            "terminal_dispatch_blockers": terminal_dispatch_blockers,
        },
    }


def _runtime_context_control_route_identity(
    values: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "route_id": _runtime_context_text(values.get("route_id")),
        "route_context_hash": _runtime_context_text(
            values.get("route_context_hash")
        ),
        "prompt_contract_id": _runtime_context_text(
            values.get("prompt_contract_id")
        ),
        "prompt_contract_hash": _runtime_context_text(
            values.get("prompt_contract_hash")
        ),
        "route_token_ref": _runtime_context_text(values.get("route_token_ref")),
    }


def _runtime_context_missing_evidence(
    *,
    gate_inputs_view: Mapping[str, Any],
    lane_plan: Mapping[str, Any],
) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in gate_inputs_view.get("missing") or []:
        if not isinstance(item, Mapping):
            continue
        gate = _runtime_context_text(item.get("gate"))
        field = _runtime_context_text(item.get("field"))
        key = ("gate", gate, field)
        if key in seen:
            continue
        seen.add(key)
        missing.append(
            {
                "kind": "gate_input",
                "gate": gate,
                "field": field,
                "expected_source": _runtime_context_text(
                    item.get("expected_source")
                ),
                "producer": _runtime_context_text(item.get("producer")),
                "consumer": _runtime_context_text(item.get("consumer")),
                "evidence_ref": _runtime_context_text(item.get("evidence_ref")),
                "message": _runtime_context_text(item.get("message")),
            }
        )
    for item in lane_plan.get("missing") or []:
        if not isinstance(item, Mapping):
            continue
        clause = _runtime_context_text(item.get("clause"))
        key = ("lane", clause, "")
        if not clause or key in seen:
            continue
        seen.add(key)
        missing.append(
            {
                "kind": "lane_clause",
                "clause": clause,
                "status": _runtime_context_text(item.get("status") or "missing"),
            }
        )
    return missing


def _runtime_context_route_token_action(
    *,
    values: Mapping[str, Any],
    gate_inputs_view: Mapping[str, Any],
    route_identity: Mapping[str, Any],
) -> dict[str, Any]:
    canonical = _runtime_context_control_route_identity(values)
    route_token_ref = canonical.get("route_token_ref", "")
    route_state = _runtime_context_text(
        route_identity.get("status")
        or route_identity.get("decision")
        or route_identity.get("route_token_status")
    ).lower()
    missing_route_token = any(
        isinstance(item, Mapping)
        and item.get("field") == "route_token_ref"
        for item in gate_inputs_view.get("missing") or []
    )
    stale_route_token = route_state in {
        "expired",
        "invalid",
        "revoked",
        "stale",
        "superseded",
    }
    if missing_route_token or not route_token_ref:
        status = "missing"
        next_action = "refresh_route_token_ref"
        issue = "canonical route identity is missing route_token_ref"
    elif stale_route_token:
        status = "stale"
        next_action = "refresh_route_token_ref"
        issue = f"canonical route token ref is {route_state}"
    else:
        status = "present"
        next_action = "none"
        issue = ""
    source_event_lineage = {
        "schema_version": "runtime_context.route_source_event_lineage.v1",
        "status": "available" if route_token_ref else "missing_route_token_ref",
        "route_token_ref": route_token_ref,
        "route_identity": canonical,
        "raw_route_token_required": False,
        "next_action": (
            "append_protected_evidence_with_route_token_ref_or_route_owned_source_event"
            if route_token_ref
            else "refresh_route_token_ref"
        ),
        "expected_source": "observer_route_token_ref_or_route_owned_source_event",
        "producer": "observer_runtime_text_prepare",
        "consumer": "task_timeline_append",
    }
    return {
        "schema_version": "runtime_context.route_token_action.v1",
        "status": status,
        "next_action": next_action,
        "issue": issue,
        "route_token_ref_present": bool(route_token_ref),
        "source_event_lineage": source_event_lineage,
        "entrypoint": {
            "method": "POST",
            "path": "/api/projects/{project_id}/observer/route-context/issue",
            "required_public_fields": [
                "backlog_id",
                "task_id",
                "target_files",
                "caller_role",
            ],
            "request_template": {
                "backlog_id": _runtime_context_text(values.get("backlog_id")),
                "task_id": _runtime_context_text(values.get("task_id")),
                "target_files": list(values.get("target_files") or []),
                "caller_role": "observer",
            },
            "runtime_context_persistence": (
                "Persist route_token_ref/hash evidence only; raw route tokens "
                "must not persist in runtime context output."
            ),
        },
        "canonical_route_identity": canonical,
        "expected_binding": {
            "route_id": canonical.get("route_id", ""),
            "route_context_hash": canonical.get("route_context_hash", ""),
            "prompt_contract_id": canonical.get("prompt_contract_id", ""),
            "prompt_contract_hash": canonical.get("prompt_contract_hash", ""),
            "route_token_ref": route_token_ref,
        },
        "operator_instruction": (
            "Refresh route-token evidence for this canonical route identity "
            "and persist only route_token_ref/hash evidence; raw route tokens "
            "must stay out of runtime-context projections."
            if status in {"missing", "stale"}
            else "Route-token reference is present for the canonical route identity."
        ),
    }


def _runtime_context_read_receipt_hash_action(
    *,
    current_view: Mapping[str, Any],
    gate_inputs_view: Mapping[str, Any],
    close_gate_view: Mapping[str, Any],
) -> dict[str, Any]:
    values = _runtime_context_mapping(current_view.get("current_values"))
    runtime_context_id = _runtime_context_text(current_view.get("runtime_context_id"))
    read_receipt_ref = _runtime_context_text(values.get("read_receipt_event_ref"))
    current_node = _runtime_context_projection_node(
        runtime_context_id=runtime_context_id,
        view_name="current",
        payload=current_view,
    )
    gate_inputs_node = _runtime_context_projection_node(
        runtime_context_id=runtime_context_id,
        view_name="gate_inputs",
        payload=gate_inputs_view,
    )
    close_gate_node = _runtime_context_projection_node(
        runtime_context_id=runtime_context_id,
        view_name="close_gate_view",
        payload=close_gate_view,
    )
    status = "present" if read_receipt_ref else "missing"
    worker_query = _runtime_context_mapping(values.get("graph_query_identity"))
    owned_files = _runtime_context_dedupe(
        _runtime_context_string_list(values.get("owned_files"))
        or _runtime_context_string_list(values.get("target_files"))
    )
    worker_identity = {
        "runtime_context_id": runtime_context_id,
        "task_id": _runtime_context_text(values.get("task_id")),
        "parent_task_id": _runtime_context_text(values.get("parent_task_id")),
        "backlog_id": _runtime_context_text(values.get("backlog_id")),
        "worker_role": _runtime_context_text(
            values.get("worker_role") or RUNTIME_CONTEXT_WORKER_ROLE
        ),
        "worker_id": _runtime_context_text(values.get("worker_id")),
        "worker_slot_id": _runtime_context_text(values.get("worker_slot_id")),
    }
    hash_material = {
        "projection_hash_source": (
            "runtime_context_service.content_address.projection_hash"
        ),
        "current_view_hash": current_node["view_hash"],
        "gate_inputs_view_hash": gate_inputs_node["view_hash"],
        "close_gate_view_hash": close_gate_node["view_hash"],
    }
    guide_hash = runtime_context_content_hash(
        {
            "schema_version": "runtime_context.worker_guide_hash_material.v1",
            "runtime_context_id": runtime_context_id,
            "worker_identity": worker_identity,
            "owned_files": owned_files,
            "hash_material": hash_material,
        }
    )
    ordered_steps = [
        {
            "id": "query_runtime_contract",
            "status": "required",
            "entrypoint": {
                "method": "GET",
                "path": (
                    "/api/graph-governance/{project_id}/runtime-contexts/"
                    "{runtime_context_id}/runtime-contract"
                ),
                "required_query_fields": [
                    "parent_task_id",
                    "fence_token",
                    "session_token",
                    "target_project_root",
                    "runtime_context_id",
                ],
            },
            "must_precede": [
                "record_read_receipt",
                "record_startup",
                "worker_graph_query",
                "implementation",
            ],
        },
        {
            "id": "record_read_receipt",
            "status": status,
            "timeline_event_kind": "mf_subagent_read_receipt",
            "required_payload_fields": [
                "runtime_context_id",
                "task_id",
                "parent_task_id",
                "fence_token",
                "worker_slot_id",
                "observer_command_id",
                "read_receipt_hash or launch_text_hash",
            ],
            "hash_bridge": {
                "accepted_inputs": ["read_receipt_hash", "launch_text_hash"],
                "startup_field": "read_receipt_hash",
                "rule": (
                    "if no dedicated read_receipt_hash exists, carry the "
                    "accepted launch_text_hash as the startup read_receipt_hash"
                ),
            },
            "must_precede": ["record_startup", "worker_graph_query", "implementation"],
        },
        {
            "id": "record_startup",
            "status": "required",
            "close_satisfying_rule": (
                "read receipt plus server-verified same-owner or "
                "service-dispatch-bound startup remains close_satisfying=false "
                "until worker transcript/self-attestation passes"
            ),
            "required_fields": [
                "observer_command_id",
                "read_receipt_hash",
                "read_receipt_event_id",
                "worker_transcript_ref or worker_transcript_path",
                "worker_session_id",
                "harness_type",
            ],
        },
        {
            "id": "worker_graph_query",
            "status": "required",
            "query_source": "mf_subagent",
            "allowed_query_purposes": [
                "subagent_context_build",
                "subagent_gate_validation",
            ],
            "required_identity": {
                "task_id": _runtime_context_text(values.get("task_id")),
                "parent_task_id": _runtime_context_text(values.get("parent_task_id")),
                "worker_role": _runtime_context_text(values.get("worker_role")),
                "query_source": _runtime_context_text(
                    worker_query.get("query_source") or "mf_subagent"
                ),
                "fence_token_required": True,
            },
            "evidence_required": "DB-verified graph_trace_ids",
            "entrypoint": {
                "method": "POST",
                "path": "/api/graph-governance/{project_id}/query",
                "required_body_fields": [
                    "tool",
                    "query_source",
                    "query_purpose",
                    "runtime_context_id",
                    "fence_token",
                    "session_token",
                    "target_project_root",
                ],
                "query_source": "mf_subagent",
                "query_purpose": "subagent_context_build",
                "allowed_query_purposes": [
                    "subagent_context_build",
                    "subagent_gate_validation",
                ],
                "route_identity_fields": [
                    "route_id",
                    "route_context_hash",
                    "prompt_contract_id",
                    "prompt_contract_hash",
                    "visible_injection_manifest_hash",
                    "route_token_ref",
                ],
                "privacy_boundary": {
                    "raw_session_token_persisted": False,
                    "raw_fence_token_echoed": False,
                    "raw_route_token_required": False,
                },
            },
        },
        {
            "id": "implementation_and_tests",
            "status": "required",
            "owned_files": owned_files,
            "required_outputs": [
                "owned file diff",
                "focused tests",
                "git diff --check",
                "uncommitted worker diff until finish-time attestation and finish gate pass",
            ],
            "evidence_to_file": [
                "implementation_evidence",
                "finish_time_worker_attestation",
                "finish_gate",
                "verification_or_test_results",
            ],
        },
        {
            "id": "transcript_self_attestation",
            "status": "required_for_close_satisfying_startup",
            "required_facts": [
                "runtime identity",
                "timeline read receipt",
                "owned file diff",
                "DB-verified mf_subagent graph traces",
            ],
        },
    ]
    worker_next_moves = [
        {
            "id": step["id"],
            "status": _runtime_context_text(step.get("status") or "required"),
            "must_precede": list(step.get("must_precede") or []),
        }
        for step in ordered_steps
    ]
    return {
        "schema_version": "runtime_context.read_receipt_hash_action.v1",
        "guide_id": "runtime_context_worker_guide.startup_bridge.v1",
        "guide_hash": guide_hash,
        "worker_identity": worker_identity,
        "status": status,
        "next_action": "none"
        if read_receipt_ref
        else "submit_mf_subagent_read_receipt",
        "read_receipt_event_ref": read_receipt_ref,
        "content_address_nodes": {
            "current": current_node,
            "gate_inputs": gate_inputs_node,
            "close_gate_view": close_gate_node,
        },
        "hash_material": hash_material,
        "entrypoint": {
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
        },
        "worker_next_moves": worker_next_moves,
        "worker_constraints": {
            "blocked_actions": [
                "merge",
                "push",
                "close_backlog_without_close_ready",
                "raw_token_exfiltration",
                "author_worker_evidence_as_observer",
                "bypass_timeline_gate",
                "git_commit_before_finish_gate",
            ],
            "precommit_finish_order": {
                "required": True,
                "sequence": [
                    "implementation_evidence",
                    "finish_time_worker_attestation",
                    "finish_gate",
                    "git_commit",
                ],
                "blocker": (
                    "runtime-context workers must leave the row-scoped branch "
                    "head unchanged until finish-time attestation and finish "
                    "gate pass"
                ),
                "committed_branch_evidence_lane": {
                    "implemented": False,
                    "accepted": False,
                },
            },
            "scope": {
                "owned_files": owned_files,
                "worker_role": RUNTIME_CONTEXT_WORKER_ROLE,
                "query_source": "mf_subagent",
            },
        },
        "observer_remediation_actions": [
            {
                "id": "request_runtime_context_initial_join_host_envelope",
                "role": "observer",
                "when": (
                    "dispatch_exists_but_worker_read_receipt_or_startup_is_missing"
                ),
            },
            {
                "id": "refresh_route_token_ref",
                "role": "observer",
                "when": "route_token_ref_missing_stale_or_scope_mismatch",
            },
            {
                "id": "review_worker_finish_gate",
                "role": "observer",
                "when": "worker_records_finish_gate_and_handoff_is_review_ready",
            },
        ],
        "operator_instruction": (
            "Submit a worker-authored mf_subagent_read_receipt before counted "
            "startup/finish evidence, using the runtime-context content-address "
            "hashes above."
            if not read_receipt_ref
            else "Read receipt evidence is present in the task timeline."
        ),
        "ordered_worker_startup_bridge": {
            "schema_version": "runtime_context.worker_startup_bridge.v1",
            "status": "ready" if read_receipt_ref else "waiting_for_read_receipt",
            "steps": ordered_steps,
            "privacy_boundary": {
                "raw_session_token_persisted": False,
                "raw_route_token_persisted": False,
                "raw_launch_text_persisted": False,
            },
        },
    }


def _runtime_context_worker_handoff_projection(
    *,
    values: Mapping[str, Any],
    read_receipt_hash_action: Mapping[str, Any],
) -> dict[str, Any]:
    runtime_context_id = _runtime_context_text(values.get("runtime_context_id"))
    task_id = _runtime_context_text(values.get("task_id"))
    parent_task_id = _runtime_context_text(values.get("parent_task_id"))
    dispatch_event_ref = _runtime_context_text(values.get("dispatch_event_ref"))
    read_receipt_event_ref = _runtime_context_text(
        values.get("read_receipt_event_ref")
    )
    startup_event_ref = _runtime_context_text(values.get("startup_event_ref"))
    heartbeat_event_ref = _runtime_context_text(
        values.get("heartbeat_event_ref")
        or values.get("last_heartbeat_event_ref")
        or values.get("progress_event_ref")
    )
    progress_event_ref = _runtime_context_text(values.get("progress_event_ref"))
    no_progress_timeout_event_ref = _runtime_context_text(
        values.get("no_progress_timeout_event_ref")
    )
    implementation_event_refs = _runtime_context_dedupe(
        _runtime_context_string_list(values.get("implementation_event_refs"))
    )
    graph_trace_ids = _runtime_context_dedupe(
        _runtime_context_string_list(values.get("graph_trace_ids"))
    )
    changed_files = _runtime_context_dedupe(
        _runtime_context_string_list(values.get("changed_files"))
        + _runtime_context_string_list(values.get("owned_changed_files"))
        + _runtime_context_string_list(values.get("worker_changed_files"))
    )
    progress_evidence_refs = _runtime_context_dedupe(
        [
            item
            for item in (
                heartbeat_event_ref,
                progress_event_ref,
                *implementation_event_refs,
                *graph_trace_ids,
                *changed_files,
            )
            if item
        ]
    )
    progress_observed = bool(progress_evidence_refs)
    dispatch_present = bool(
        dispatch_event_ref
        or (
            runtime_context_id
            and (
                task_id
                or values.get("worktree_path")
                or values.get("branch_ref")
                or values.get("worker_id")
            )
        )
    )
    missing_lineage: list[str] = []
    if not read_receipt_event_ref:
        missing_lineage.append("mf_subagent_read_receipt")
    if not startup_event_ref:
        missing_lineage.append("mf_subagent_startup")

    if not dispatch_present:
        status = "not_dispatched"
        observer_next_action = "refresh_branch_runtime_context"
    elif missing_lineage and progress_observed:
        status = "worker_lineage_missing_progress_observed"
        observer_next_action = "inspect_progress_and_repair_worker_lineage"
    elif missing_lineage:
        status = (
            "no_worker_startup_evidence"
            if "mf_subagent_startup" in missing_lineage
            else "read_receipt_missing"
        )
        observer_next_action = "request_runtime_context_initial_join_host_envelope"
    else:
        status = "worker_lineage_present"
        observer_next_action = "none"

    worker_next_action = _runtime_context_text(
        read_receipt_hash_action.get("next_action")
    )
    if worker_next_action == "none" and not startup_event_ref:
        worker_next_action = "record_mf_subagent_startup"

    recovery_actions: list[dict[str, Any]] = []
    no_progress_reissue_allowed = bool(
        dispatch_present
        and missing_lineage
        and not progress_observed
    )
    if status == "not_dispatched":
        recovery_actions.append(
            {
                "id": "refresh_branch_runtime_context",
                "role": "observer",
                "allowed_when": "runtime_context_has_no_dispatch_or_worker_assignment",
            }
        )
    if missing_lineage:
        recovery_actions.extend(
            [
                {
                    "id": "request_runtime_context_initial_join_host_envelope",
                    "role": "observer",
                    "allowed_when": (
                        "dispatch_is_present_but_worker_lineage_is_missing"
                    ),
                    "result": (
                        "host returns the worker identity, session token ref, "
                        "fence token, target worktree, and runtime_context_id"
                    ),
                },
            ]
        )
    if missing_lineage and progress_observed:
        recovery_actions.append(
            {
                "id": "inspect_progress_and_repair_worker_lineage",
                "role": "observer",
                "allowed_when": (
                    "worker progress evidence exists but startup/read-receipt "
                    "lineage is incomplete"
                ),
                "blocked_actions": [
                    "cancel_or_mark_stale_no_progress_worker",
                    "reissue_mf_sub_worker_with_same_scope",
                ],
            }
        )
    if no_progress_reissue_allowed:
        recovery_actions.extend(
            [
                {
                    "id": "cancel_or_mark_stale_no_progress_worker",
                    "role": "observer",
                    "allowed_when": (
                        "a spawned worker has no read receipt, startup, heartbeat, "
                        "progress, graph trace, implementation, or changed-file evidence"
                    ),
                },
                {
                    "id": "reissue_mf_sub_worker_with_same_scope",
                    "role": "observer",
                    "allowed_when": (
                        "no-progress dispatch has been marked stale or cancelled"
                    ),
                    "scope_must_match": [
                        "runtime_context_id",
                        "task_id",
                        "parent_task_id",
                        "owned_files",
                        "worktree_path",
                        "route_token_ref",
                    ],
                },
            ]
        )

    return {
        "schema_version": "runtime_context.worker_handoff_projection.v1",
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "status": status,
        "dispatch_present": dispatch_present,
        "dispatch_event_ref": dispatch_event_ref,
        "observed_evidence": {
            "read_receipt_event_ref": read_receipt_event_ref,
            "startup_event_ref": startup_event_ref,
            "heartbeat_event_ref": heartbeat_event_ref,
            "progress_event_ref": progress_event_ref,
            "no_progress_timeout_event_ref": no_progress_timeout_event_ref,
            "implementation_event_refs": implementation_event_refs,
            "graph_trace_ids": graph_trace_ids,
            "changed_files": changed_files,
            "worktree_path": _runtime_context_text(values.get("worktree_path")),
            "branch_ref": _runtime_context_text(values.get("branch_ref")),
        },
        "missing_worker_lineage": missing_lineage,
        "heartbeat_status": "present" if heartbeat_event_ref else "missing",
        "progress_status": "observed" if progress_observed else "not_observed",
        "progress_evidence_refs": progress_evidence_refs,
        "worker_next_action": worker_next_action,
        "observer_next_action": observer_next_action,
        "recovery_actions": recovery_actions,
        "no_progress_reissue_policy": {
            "allowed": no_progress_reissue_allowed,
            "legal_when": (
                "dispatch is present and no worker read receipt, startup, "
                "heartbeat, progress, graph trace, implementation, or "
                "changed-file evidence exists"
            ),
            "blocked_by_progress_evidence": progress_observed,
            "progress_evidence_refs": progress_evidence_refs,
            "no_progress_timeout_event_ref": no_progress_timeout_event_ref,
            "must_record_before_reissue": "cancel_or_mark_stale_no_progress_worker",
            "duplicate_worker_evidence_close_satisfying": False,
            "observer_must_not_backfill_worker_evidence": True,
            "reuse_runtime_scope": True,
        },
        "direct_fix_return_contract": {
            "after_repair": "run_independent_qa_then_resume_parent_contract",
            "requires_independent_qa_after_repair": True,
            "resume_parent_without_reconcile": True,
        },
        "raw_session_token_exposed": False,
        "raw_fence_token_exposed": False,
        "raw_route_token_exposed": False,
    }


def _runtime_context_close_blocker_explanation(
    *,
    values: Mapping[str, Any],
    close_gate_view: Mapping[str, Any],
) -> dict[str, Any]:
    missing_fields = {
        _runtime_context_text(item.get("field"))
        for item in close_gate_view.get("missing") or []
        if isinstance(item, Mapping)
    }
    startup_ref = _runtime_context_text(values.get("startup_event_ref"))
    explanations: list[dict[str, Any]] = []
    if startup_ref and missing_fields:
        explanations.append(
            {
                "code": "startup_exists_but_not_close_satisfying",
                "message": (
                    "startup exists but is not close-satisfying; finish, "
                    "verification, checkpoint, graph trace, self-attestation, "
                    "or route-token evidence is still missing"
                ),
                "evidence_ref": startup_ref,
            }
        )
    elif not startup_ref:
        explanations.append(
            {
                "code": "startup_missing",
                "message": "record real mf_subagent startup evidence for this fenced worker",
                "evidence_ref": "",
            }
        )
    field_messages = {
        "checkpoint_id": "record a checkpoint id or finish-gate checkpoint for this branch",
        "finish_gate_ref": "record the worker finish gate after implementation is complete",
        "route_action_precheck_event_ref": (
            "record observer-owned route_action_precheck evidence for the "
            "canonical route identity"
        ),
        "verification_event_refs": "record independent verification evidence for the focused tests/checks",
        "graph_trace_ids": "run worker-scoped graph queries and attach trace ids",
        "worker_self_attesting": (
            "include finish-time worker self-attestation accepted by the finish gate"
        ),
        "route_token_ref": "refresh route_token_ref under the canonical route identity",
        "route_context_hash": "restore canonical route context hash evidence",
        "prompt_contract_hash": "restore prompt contract hash evidence",
        "merge_queue_id": "bind the worker lane to its merge queue identity",
    }
    for field in sorted(missing_fields):
        explanations.append(
            {
                "code": f"missing_{field}",
                "field": field,
                "message": field_messages.get(
                    field,
                    f"record close-gate evidence for {field}",
                ),
            }
        )
    route_token_ref = _runtime_context_text(values.get("route_token_ref"))
    if route_token_ref and missing_fields:
        explanations.append(
            {
                "code": "route_source_event_lineage_available",
                "field": "route_token_ref",
                "message": (
                    "protected timeline evidence can use route_token_ref or an "
                    "accepted route-owned source-event lineage; raw route tokens "
                    "must not be exposed"
                ),
                "route_token_ref": route_token_ref,
                "next_action": (
                    "append_protected_evidence_with_route_token_ref_or_route_owned_source_event"
                ),
                "raw_route_token_required": False,
            }
        )
    return {
        "schema_version": "runtime_context.close_blocker_explanation.v1",
        "ready": bool(close_gate_view.get("ready")),
        "status": close_gate_view.get("status", ""),
        "summary": "close gate ready"
        if close_gate_view.get("ready")
        else "close gate is blocked by missing worker handoff evidence",
        "explanations": explanations,
    }


def _runtime_context_audit_archive_action(
    *,
    values: Mapping[str, Any],
    close_blocker_explanation: Mapping[str, Any],
) -> dict[str, Any]:
    explanations = [
        item for item in close_blocker_explanation.get("explanations") or []
        if isinstance(item, Mapping)
    ]
    has_close_blocker = bool(explanations) and not bool(
        close_blocker_explanation.get("ready")
    )
    return {
        "schema_version": "runtime_context.audit_archive_action.v1",
        "status": "candidate_requires_observer_historical_classification"
        if has_close_blocker
        else "not_applicable",
        "next_action": "classify_historical_non_reconstructable"
        if has_close_blocker
        else "none",
        "archive_action": "backlog_audit_archive" if has_close_blocker else "none",
        "ordinary_close_gate_claimed": False,
        "normal_close_gate_passed": False,
        "close_ready_emitted": False,
        "entrypoint": {
            "method": "POST",
            "path": "/api/backlog/{project_id}/{bug_id}/audit-archive",
            "mcp_tool": "backlog_audit_archive",
            "required_public_fields": [
                "bug_id",
                "commit",
                "reason",
                "timeline_precheck",
                "verification",
                "graph_snapshot",
                "route_token or route_waiver",
            ],
            "request_template": {
                "bug_id": _runtime_context_text(values.get("backlog_id")),
                "reason": (
                    "Historical MF close evidence is non-reconstructable; "
                    "archive with explicit audit evidence instead of claiming "
                    "ordinary MF close success."
                ),
                "source_runtime_context_id": _runtime_context_text(
                    values.get("runtime_context_id")
                ),
            },
        },
        "operator_instruction": (
            "Treat this as a candidate only. First classify the close blocker "
            "as historical and non-reconstructable, then use audit archive with "
            "timeline_precheck, verification, and graph_snapshot evidence. It "
            "must not emit close_ready or can_close=true."
            if has_close_blocker
            else "Audit archive is not applicable while close gate is ready."
        ),
        "blocker_codes": [
            _runtime_context_text(item.get("code"))
            for item in explanations
            if item.get("code")
        ],
    }


def _runtime_context_merge_dependency_projection(
    *,
    values: Mapping[str, Any],
) -> dict[str, Any]:
    queue_projection = _runtime_context_mapping(values.get("merge_queue_projection"))
    blockers = _runtime_context_string_list(queue_projection.get("dependency_blockers"))
    queue_state = _runtime_context_text(
        queue_projection.get("queue_state") or queue_projection.get("status")
    )
    raw_next_actions = _runtime_context_string_list(queue_projection.get("next_actions"))
    observed_status = _runtime_context_text(
        queue_projection.get("observed_status") or values.get("status")
    )
    terminal_complete = observed_status in {
        STATE_VALIDATED,
        STATE_MERGE_READY,
        STATE_MERGED,
    }
    merge_allowed = bool(queue_projection.get("merge_allowed"))
    target_branch_mutation_allowed = bool(
        queue_projection.get("target_branch_mutation_allowed")
    )
    merge_eligible = (
        queue_state == STATE_MERGE_READY
        and merge_allowed
        and target_branch_mutation_allowed
    )
    requires_merge_preview_refresh = bool(
        queue_projection.get("requires_merge_preview_refresh")
        or queue_projection.get("stale_target_head")
        or queue_state == STATE_STALE_AFTER_DEPENDENCY_MERGE
    )

    next_actions: list[str] = []
    if queue_state in {STATE_WAITING_DEPENDENCY, STATE_DEPENDENCY_BLOCKED} or blockers:
        next_actions.extend(
            [
                "wait_for_dependency",
                "merge_dependency_first",
                "do_not_merge_current_lane",
            ]
        )
    elif queue_state == STATE_STALE_AFTER_DEPENDENCY_MERGE or values.get(
        "dependency_merge_commit"
    ):
        next_actions.extend(["refresh_merge_preview", "revalidate"])
    elif queue_state == STATE_MERGE_READY:
        next_actions.append("merge_ready")
    for action in raw_next_actions:
        normalized = {
            "do_not_merge": "do_not_merge_current_lane",
            "refresh_merge_preview": "refresh_merge_preview",
        }.get(action, action)
        if (
            queue_state in {STATE_WAITING_DEPENDENCY, STATE_DEPENDENCY_BLOCKED}
            and normalized == "resolve_dependency"
        ):
            continue
        if normalized not in next_actions:
            next_actions.append(normalized)

    if queue_state in {STATE_WAITING_DEPENDENCY, STATE_DEPENDENCY_BLOCKED} or blockers:
        next_action = "wait_for_dependency"
        status = queue_state or STATE_DEPENDENCY_BLOCKED
    elif queue_state == STATE_STALE_AFTER_DEPENDENCY_MERGE:
        next_action = "refresh_merge_preview"
        status = STATE_STALE_AFTER_DEPENDENCY_MERGE
    elif queue_state == STATE_MERGE_READY:
        next_action = "merge_ready"
        status = STATE_MERGE_READY
    elif queue_projection:
        next_action = next_actions[0] if next_actions else "inspect_merge_queue"
        status = queue_state or "unknown"
    else:
        next_action = "none"
        status = "not_applicable"

    return {
        "schema_version": "runtime_context.merge_dependency_projection.v1",
        "status": status,
        "queue_state": queue_state,
        "merge_queue_id": _runtime_context_text(values.get("merge_queue_id")),
        "queue_item_id": _runtime_context_text(queue_projection.get("queue_item_id")),
        "task_id": _runtime_context_text(
            queue_projection.get("task_id") or values.get("task_id")
        ),
        "branch_ref": _runtime_context_text(queue_projection.get("branch_ref")),
        "dependency_blockers": blockers,
        "dependency_blocker_types": public_contract_revision_payload(
            queue_projection.get("dependency_blocker_types")
        ),
        "dependency_merge_commit": _runtime_context_text(
            values.get("dependency_merge_commit")
        ),
        "observed_status": observed_status,
        "merge_preview_id": _runtime_context_text(
            queue_projection.get("merge_preview_id") or values.get("merge_preview_id")
        ),
        "terminal_complete": terminal_complete,
        "merge_eligible": merge_eligible,
        "requires_merge_preview_refresh": requires_merge_preview_refresh,
        "merge_allowed": merge_allowed,
        "target_branch_mutation_allowed": target_branch_mutation_allowed,
        "next_action": next_action,
        "next_actions": next_actions,
        "ordered_merge_dependencies": _runtime_context_string_list(
            values.get("ordered_merge_dependencies")
        ),
    }


def _runtime_context_close_precheck_gap_projection(
    *,
    values: Mapping[str, Any],
    close_gate_view: Mapping[str, Any],
) -> dict[str, Any]:
    close_precheck = _runtime_context_mapping(values.get("close_precheck"))
    missing_close_fields = _runtime_context_dedupe(
        [
            _runtime_context_text(item.get("field"))
            for item in close_gate_view.get("missing") or []
            if isinstance(item, Mapping)
        ]
    )
    gap_specs = {
        "route_identity_cleanup_required": (
            "cleanup_route_identity",
            "Remove stale/superseded route identity evidence and rebind the canonical route.",
        ),
        "independent_verification_required": (
            "record_independent_verification",
            "Record an independent verification lane before close.",
        ),
        "route_token_action_scope_mismatch": (
            "refresh_route_token_scope",
            "Refresh route-token evidence with the task_timeline_append action scope.",
        ),
        "target_graph_stale": (
            "reconcile_target_graph",
            "Reconcile the target graph before claiming close-ready state.",
        ),
        "worker_graph_query_identity_required": (
            "query_graph_as_mf_subagent",
            "Run graph queries with mf_subagent identity and attach trace ids.",
        ),
    }
    gaps: list[dict[str, Any]] = []
    next_actions: list[str] = []

    def _add_gap(*, code: str, message: str, next_action: str, field: str = "") -> None:
        if not code or any(item.get("code") == code for item in gaps):
            return
        gap = {
            "code": code,
            "message": message,
            "next_action": next_action,
        }
        if field:
            gap["field"] = field
            if field not in missing_close_fields:
                missing_close_fields.append(field)
        gaps.append(gap)
        if next_action and next_action not in next_actions:
            next_actions.append(next_action)

    terminal_dispatch_blockers = list(
        values.get("terminal_dispatch_blockers")
        or close_gate_view.get("terminal_dispatch_blockers")
        or []
    )
    if terminal_dispatch_blockers:
        _add_gap(
            code="terminal_dispatch_blocker",
            message="Terminal dispatch blocker is recorded for this runtime context lineage.",
            next_action="audit_close_or_resolve_terminal_dispatch_blocker",
            field="terminal_dispatch_blocker",
        )

    for code, (next_action, message) in gap_specs.items():
        if close_precheck.get(code) is True:
            _add_gap(
                code=code,
                message=message,
                next_action=next_action,
            )
    close_gap_specs = (
        (
            "read_receipt_event_ref",
            "read_receipt_missing",
            "submit_mf_subagent_read_receipt",
            "Record worker-authored mf_subagent_read_receipt evidence before implementation evidence.",
        ),
        (
            "startup_event_ref",
            "startup_missing",
            "record_mf_subagent_startup",
            "Record actual mf_subagent_startup identity evidence for this worker lane.",
        ),
        (
            "worker_self_attesting",
            "finish_time_worker_attestation_missing",
            "record_finish_time_worker_attestation",
            "Record finish-time worker self-attestation separate from startup identity evidence.",
        ),
        (
            "finish_gate_ref",
            "finish_gate_missing",
            "record_finish_gate",
            "Run the mf_subagent finish gate after startup, graph, tests, and finish attestation are present.",
        ),
        (
            "route_action_precheck_event_ref",
            "route_action_precheck_missing",
            "record_route_action_precheck",
            "Record observer-owned route_action_precheck timeline evidence for the canonical route identity.",
        ),
    )
    for field, code, next_action, message in close_gap_specs:
        if not _runtime_context_value_present(values.get(field)):
            _add_gap(
                code=code,
                message=message,
                next_action=next_action,
                field=field,
            )
    if _runtime_context_value_present(values.get("startup_event_ref")):
        startup_identity_gap_specs = (
            (
                "startup_runtime_context_id",
                "startup_runtime_context_missing",
                "runtime_context_id",
            ),
            ("startup_fence_token_present", "startup_fence_missing", "fence_token"),
            (
                "startup_worker_session_id",
                "startup_worker_session_missing",
                "worker_session_id",
            ),
            (
                "startup_worker_transcript_ref",
                "startup_worker_transcript_missing",
                "worker_transcript_ref_or_path",
            ),
            ("startup_harness_type", "startup_harness_missing", "harness_type"),
            ("startup_route_id", "startup_route_id_missing", "route_id"),
            (
                "startup_route_context_hash",
                "startup_route_context_missing",
                "route_context_hash",
            ),
            (
                "startup_prompt_contract_id",
                "startup_prompt_contract_id_missing",
                "prompt_contract_id",
            ),
            (
                "startup_prompt_contract_hash",
                "startup_prompt_contract_hash_missing",
                "prompt_contract_hash",
            ),
            (
                "startup_route_token_ref",
                "startup_route_token_ref_missing",
                "route_token_ref",
            ),
            (
                "startup_read_receipt_hash",
                "startup_read_receipt_hash_missing",
                "read_receipt_hash",
            ),
            (
                "startup_read_receipt_event_id",
                "startup_read_receipt_event_missing",
                "read_receipt_event_id",
            ),
        )
        for field, code, label in startup_identity_gap_specs:
            if not _runtime_context_value_present(values.get(field)):
                _add_gap(
                    code=code,
                    message=(
                        "Existing mf_subagent_startup evidence is stale or incomplete; "
                        f"missing startup {label}."
                    ),
                    next_action="record_mf_subagent_startup",
                    field=field,
                )
    return {
        "schema_version": "runtime_context.close_precheck_gap_projection.v1",
        "status": "blocked" if gaps else "clear",
        "gaps": gaps,
        "next_actions": next_actions,
        "done_state_projection": {
            "schema_version": "runtime_context.done_state_projection.v1",
            "status": "review_ready" if close_gate_view.get("ready") else "gap_open",
            "close_gate_ready": bool(close_gate_view.get("ready")),
            "close_ready_event_ref": _runtime_context_text(
                values.get("close_ready_event_ref")
            ),
            "finish_gate_ref": _runtime_context_text(values.get("finish_gate_ref")),
            "checkpoint_id": _runtime_context_text(values.get("checkpoint_id")),
            "graph_trace_ids": list(values.get("graph_trace_ids") or []),
            "verification_event_refs": list(
                values.get("verification_event_refs") or []
            ),
            "missing_close_fields": missing_close_fields,
            "handoff_terminal_status": (
                "terminal_dispatch_blocked"
                if terminal_dispatch_blockers
                else ("review_ready" if close_gate_view.get("ready") else "waiting_merge_gap")
            ),
        },
    }


def _runtime_context_next_legal_action(
    *,
    route_token_action: Mapping[str, Any],
    read_receipt_hash_action: Mapping[str, Any],
    values: Mapping[str, Any],
    close_gate_view: Mapping[str, Any],
    lane_plan: Mapping[str, Any],
    failed_qa_revision_projection: Mapping[str, Any] | None = None,
) -> str:
    if values.get("terminal_dispatch_blockers") or close_gate_view.get(
        "terminal_dispatch_blockers"
    ):
        return "audit_close_or_resolve_terminal_dispatch_blocker"
    if (
        isinstance(failed_qa_revision_projection, Mapping)
        and failed_qa_revision_projection.get("status") == "revision_required"
    ):
        return "revise_after_failed_independent_qa"
    if route_token_action.get("status") in {"missing", "stale"}:
        return "refresh_route_token_ref"
    if read_receipt_hash_action.get("status") == "missing":
        return "submit_mf_subagent_read_receipt"
    if not _runtime_context_text(values.get("startup_event_ref")):
        return "record_mf_subagent_startup"
    startup_identity_fields = (
        "startup_runtime_context_id",
        "startup_fence_token_present",
        "startup_worker_session_id",
        "startup_worker_transcript_ref",
        "startup_harness_type",
        "startup_route_id",
        "startup_route_context_hash",
        "startup_prompt_contract_id",
        "startup_prompt_contract_hash",
        "startup_route_token_ref",
        "startup_read_receipt_hash",
        "startup_read_receipt_event_id",
    )
    if any(
        not _runtime_context_value_present(values.get(field))
        for field in startup_identity_fields
    ):
        return "record_mf_subagent_startup"
    missing_fields = {
        _runtime_context_text(item.get("field"))
        for item in close_gate_view.get("missing") or []
        if isinstance(item, Mapping)
    }
    if not _runtime_context_string_list(values.get("graph_trace_ids")):
        return "run_worker_graph_query"
    if not _runtime_context_string_list(values.get("implementation_event_refs")):
        return "record_implementation_evidence"
    for field, action in (
        ("worker_self_attesting", "record_finish_time_worker_attestation"),
        ("finish_gate_ref", "record_finish_gate"),
        ("checkpoint_id", "record_checkpoint"),
        ("verification_event_refs", "handoff_to_independent_qa"),
        ("route_action_precheck_event_ref", "record_route_action_precheck"),
        ("close_ready_event_ref", "record_close_ready"),
    ):
        if field in missing_fields:
            return action
    if lane_plan.get("blocking_events"):
        return "resolve_blocking_timeline_event"
    if close_gate_view.get("ready"):
        return "handoff_review_ready"
    return "record_missing_evidence"


def _runtime_context_failed_qa_revision_projection(
    *,
    lane_plan: Mapping[str, Any],
    values: Mapping[str, Any],
) -> dict[str, Any]:
    owned_files = _runtime_context_dedupe(
        _runtime_context_string_list(values.get("owned_files"))
        or _runtime_context_string_list(values.get("target_files"))
        or _runtime_context_string_list(values.get("changed_files"))
    )
    for event in lane_plan.get("blocking_events") or []:
        if not isinstance(event, Mapping):
            continue
        event_kind = _runtime_context_lane_clause_id(event.get("event_kind"))
        status = _runtime_context_lane_clause_id(event.get("status"))
        if event_kind not in _RUNTIME_CONTEXT_QA_EVENT_KINDS:
            continue
        if status not in _RUNTIME_CONTEXT_BLOCKING_STATUSES:
            continue
        failed_ref = _runtime_context_lane_timeline_ref(event.get("event_ref"))
        return {
            "schema_version": "runtime_context.failed_qa_revision_projection.v1",
            "status": "revision_required",
            "next_legal_action": "revise_after_failed_independent_qa",
            "failed_qa_event_ref": failed_ref,
            "failed_qa_event_kind": event_kind,
            "failed_qa_status": status,
            "failed_acceptance_items": list(event.get("acceptance_failed") or []),
            "findings": _runtime_context_public_qa_findings(
                event.get("findings") or []
            ),
            "reviewed_events": _runtime_context_public_reviewed_events(
                event.get("reviewed_events") or {}
            ),
            "allowed_files": owned_files,
            "required_revision_cycle": [
                "revise_implementation_in_owned_scope",
                "record_implementation_evidence",
                "record_worker_verification",
                "record_review_ready",
                "request_independent_qa_again",
            ],
            "blocked_generic_actions": [
                "record_mf_subagent_startup",
                "record_close_ready",
                "handoff_review_ready",
                "backlog_close",
            ],
            "raw_route_token_exposed": False,
        }
    return {
        "schema_version": "runtime_context.failed_qa_revision_projection.v1",
        "status": "not_required",
    }


def _runtime_context_failed_qa_revision_required_item(
    projection: Mapping[str, Any],
    values: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "runtime_context.next_required_evidence.item.v1",
        "id": "failed_qa_revision",
        "status": "blocked",
        "field": "failed_qa_event_ref",
        "gate": "independent_qa",
        "next_action": "revise_after_failed_independent_qa",
        "producer": "mf_subagent_worker",
        "consumer": "independent_qa",
        "expected_source": "task_timeline.independent_verification.failed",
        "evidence_ref": _runtime_context_text(projection.get("failed_qa_event_ref")),
        "runtime_context_id": _runtime_context_text(values.get("runtime_context_id")),
        "task_id": _runtime_context_text(values.get("task_id")),
        "parent_task_id": _runtime_context_text(values.get("parent_task_id")),
        "worker_owned": True,
        "close_satisfying_required": True,
        "requires": [],
        "failed_acceptance_items": list(
            projection.get("failed_acceptance_items") or []
        ),
        "allowed_files": list(projection.get("allowed_files") or []),
        "required_revision_cycle": list(
            projection.get("required_revision_cycle") or []
        ),
    }


def _runtime_context_next_required_evidence(
    *,
    values: Mapping[str, Any],
    route_token_action: Mapping[str, Any],
    read_receipt_hash_action: Mapping[str, Any],
    close_gate_view: Mapping[str, Any],
    lane_plan: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Project the worker-owned evidence still needed before close."""

    runtime_context_id = _runtime_context_text(values.get("runtime_context_id"))
    task_id = _runtime_context_text(values.get("task_id"))
    parent_task_id = _runtime_context_text(values.get("parent_task_id"))
    missing_fields = {
        _runtime_context_text(item.get("field"))
        for item in close_gate_view.get("missing") or []
        if isinstance(item, Mapping)
    }
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(
        *,
        item_id: str,
        field: str,
        gate: str,
        next_action: str,
        producer: str,
        consumer: str,
        expected_source: str,
        evidence_ref: str,
        status: str = "missing",
        worker_owned: bool = True,
        close_satisfying_required: bool = False,
        requires: Sequence[str] = (),
    ) -> None:
        if not item_id or item_id in seen:
            return
        seen.add(item_id)
        items.append(
            {
                "schema_version": "runtime_context.next_required_evidence.item.v1",
                "id": item_id,
                "status": status,
                "field": field,
                "gate": gate,
                "next_action": next_action,
                "producer": producer,
                "consumer": consumer,
                "expected_source": expected_source,
                "evidence_ref": evidence_ref,
                "runtime_context_id": runtime_context_id,
                "task_id": task_id,
                "parent_task_id": parent_task_id,
                "worker_owned": worker_owned,
                "close_satisfying_required": close_satisfying_required,
                "requires": list(requires),
            }
        )

    if values.get("terminal_dispatch_blockers") or close_gate_view.get(
        "terminal_dispatch_blockers"
    ):
        _add(
            item_id="terminal_dispatch_blocker",
            field="terminal_dispatch_blocker",
            gate="terminal_dispatch",
            next_action="audit_close_or_resolve_terminal_dispatch_blocker",
            producer="task_timeline.record_blocker",
            consumer="runtime_context_close_gate",
            expected_source="task_timeline.record_blocker.terminal_dispatch_blocker",
            evidence_ref=_runtime_context_text(
                values.get("terminal_dispatch_blocker_ref")
            ),
            status="blocked",
            worker_owned=False,
            close_satisfying_required=True,
        )

    if route_token_action.get("status") in {"missing", "stale"}:
        _add(
            item_id="route_token_ref",
            field="route_token_ref",
            gate="dispatch",
            next_action="refresh_route_token_ref",
            producer="route_prompt_contract",
            consumer="runtime_context_action_plan",
            expected_source="route_prompt_contract.route_token_ref",
            evidence_ref="route_identity",
            status=_runtime_context_text(route_token_action.get("status") or "missing"),
            worker_owned=False,
        )
    if read_receipt_hash_action.get("status") == "missing":
        _add(
            item_id="runtime_context_read_receipt",
            field="read_receipt_event_ref",
            gate="finish",
            next_action="submit_mf_subagent_read_receipt",
            producer="mf_subagent_worker",
            consumer="task_timeline",
            expected_source="task_timeline.mf_subagent_read_receipt",
            evidence_ref="timeline",
            close_satisfying_required=True,
        )
    startup_event_ref = _runtime_context_text(values.get("startup_event_ref"))
    if not startup_event_ref:
        requires: list[str] = []
        if "runtime_context_read_receipt" in seen:
            requires.append("runtime_context_read_receipt")
        _add(
            item_id="mf_subagent_startup",
            field="startup_event_ref",
            gate="finish",
            next_action="record_mf_subagent_startup",
            producer="mf_subagent_worker",
            consumer="task_timeline",
            expected_source="task_timeline.mf_subagent_startup",
            evidence_ref="timeline",
            close_satisfying_required=True,
            requires=requires,
        )
    else:
        stale_startup_fields = [
            field
            for field in (
                "startup_runtime_context_id",
                "startup_fence_token_present",
                "startup_worker_session_id",
                "startup_worker_transcript_ref",
                "startup_harness_type",
                "startup_route_id",
                "startup_route_context_hash",
                "startup_prompt_contract_id",
                "startup_prompt_contract_hash",
                "startup_route_token_ref",
                "startup_read_receipt_hash",
                "startup_read_receipt_event_id",
            )
            if not _runtime_context_value_present(values.get(field))
        ]
        if stale_startup_fields:
            _add(
                item_id="mf_subagent_startup_identity",
                field=",".join(stale_startup_fields),
                gate="finish",
                next_action="record_mf_subagent_startup",
                producer="mf_subagent_worker",
                consumer="mf_subagent_contract.validate_mf_subagent_finish_gate",
                expected_source="task_timeline.mf_subagent_startup.identity",
                evidence_ref="timeline",
                status="stale",
                close_satisfying_required=True,
                requires=[],
            )
    if (
        "graph_trace_ids" in missing_fields
        or not _runtime_context_string_list(values.get("graph_trace_ids"))
    ):
        _add(
            item_id="worker_graph_trace",
            field="graph_trace_ids",
            gate="finish",
            next_action="run_worker_graph_query",
            producer="graph_query_trace",
            consumer="mf_subagent_contract.validate_mf_subagent_finish_gate",
            expected_source="graph_query_trace.trace_ids",
            evidence_ref="graph_trace",
            close_satisfying_required=True,
        )
    if not _runtime_context_string_list(values.get("implementation_event_refs")):
        requires = ["worker_graph_trace"] if "worker_graph_trace" in seen else []
        _add(
            item_id="implementation_evidence",
            field="implementation_event_refs",
            gate="finish",
            next_action="record_implementation_evidence",
            producer="mf_subagent_worker",
            consumer="mf_subagent_contract.validate_mf_subagent_finish_gate",
            expected_source="task_timeline.implementation",
            evidence_ref="timeline",
            close_satisfying_required=True,
            requires=requires,
        )
    if "worker_self_attesting" in missing_fields:
        requires = [
            evidence_id
            for evidence_id in ("worker_graph_trace", "implementation_evidence")
            if evidence_id in seen
        ]
        _add(
            item_id="finish_time_worker_attestation",
            field="worker_self_attesting",
            gate="finish",
            next_action="record_finish_time_worker_attestation",
            producer="worker_transcript_verify",
            consumer="mf_subagent_contract.validate_mf_subagent_finish_gate",
            expected_source="worker_transcript_verify.finish_time_worker_self_attestation",
            evidence_ref="finish_gate",
            close_satisfying_required=True,
            requires=requires,
        )
    if "finish_gate_ref" in missing_fields:
        requires = [
            evidence_id
            for evidence_id in (
                "worker_graph_trace",
                "implementation_evidence",
                "finish_time_worker_attestation",
            )
            if evidence_id in seen
        ]
        _add(
            item_id="finish_gate",
            field="finish_gate_ref",
            gate="close",
            next_action="record_finish_gate",
            producer="mf_subagent_worker",
            consumer="close_gate",
            expected_source="task_timeline.finish_gate",
            evidence_ref="timeline",
            close_satisfying_required=True,
            requires=requires,
        )
    for field, item_id, action, producer, expected_source in (
        (
            "checkpoint_id",
            "checkpoint",
            "record_checkpoint",
            "parallel_branch_runtime",
            "branch_runtime.context.checkpoint_or_finish_gate",
        ),
        (
            "verification_event_refs",
            "independent_verification",
            "handoff_to_independent_qa",
            "independent_qa",
            "task_timeline.verification",
        ),
        (
            "route_action_precheck_event_ref",
            "route_action_precheck",
            "record_route_action_precheck",
            "route_service",
            "task_timeline.route_action_precheck",
        ),
        (
            "close_ready_event_ref",
            "close_ready",
            "record_close_ready",
            "observer_review",
            "task_timeline.close_ready",
        ),
    ):
        if field in missing_fields:
            _add(
                item_id=item_id,
                field=field,
                gate="close",
                next_action=action,
                producer=producer,
                consumer="close_gate",
                expected_source=expected_source,
                evidence_ref="timeline",
                worker_owned=item_id
                not in {
                    "close_ready",
                    "independent_verification",
                    "route_action_precheck",
                },
                requires=["finish_gate"] if "finish_gate" in seen else [],
            )
    if lane_plan.get("blocking_events"):
        _add(
            item_id="lane_blocking_event",
            field="blocking_events",
            gate="lane_plan",
            next_action="resolve_blocking_timeline_event",
            producer="task_timeline",
            consumer="runtime_context_lane_plan",
            expected_source="task_timeline.blocking_event",
            evidence_ref="timeline",
            status="blocked",
            worker_owned=False,
        )
    item_by_id = {
        _runtime_context_text(item.get("id")): item
        for item in items
        if isinstance(item, Mapping)
    }
    finish_attestation_item = item_by_id.get("finish_time_worker_attestation")
    finish_gate_item = item_by_id.get("finish_gate")
    if finish_attestation_item and finish_gate_item:
        finish_attestation_item["next_after_success"] = "record_finish_gate"
        finish_attestation_item["sequence_note"] = (
            "After this finish-time worker attestation is accepted, refresh "
            "current-state/worker-guide; finish_gate becomes the first "
            "worker-owned next_required_evidence item."
        )
        finish_gate_item["waits_for"] = "finish_time_worker_attestation"
        finish_gate_item["sequence_note"] = (
            "Do not record finish_gate until finish_time_worker_attestation is "
            "accepted; raw attestation alone is not close-satisfying."
        )
        requires = list(finish_gate_item.get("requires") or [])
        if "finish_time_worker_attestation" not in requires:
            requires.append("finish_time_worker_attestation")
        finish_gate_item["requires"] = requires
    for index, item in enumerate(items):
        item["sequence_index"] = index
        item["is_next"] = index == 0
    return items


def build_runtime_context_action_plan_view(
    current_view: Mapping[str, Any],
    gate_inputs_view: Mapping[str, Any] | None = None,
    close_gate_view: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the operator action projection from existing runtime views."""

    values = _runtime_context_mapping(current_view.get("current_values"))
    route_identity = _runtime_context_mapping(current_view.get("route_identity"))
    lane_plan = _runtime_context_mapping(current_view.get("lane_plan"))
    gate_inputs = (
        dict(gate_inputs_view)
        if isinstance(gate_inputs_view, Mapping)
        else build_runtime_context_gate_inputs_view(current_view)
    )
    close_gate = (
        dict(close_gate_view)
        if isinstance(close_gate_view, Mapping)
        else build_runtime_context_close_gate_view(current_view, gate_inputs)
    )
    route_token_action = _runtime_context_route_token_action(
        values=values,
        gate_inputs_view=gate_inputs,
        route_identity=route_identity,
    )
    read_receipt_hash_action = _runtime_context_read_receipt_hash_action(
        current_view=current_view,
        gate_inputs_view=gate_inputs,
        close_gate_view=close_gate,
    )
    close_blocker_explanation = _runtime_context_close_blocker_explanation(
        values=values,
        close_gate_view=close_gate,
    )
    audit_archive_action = _runtime_context_audit_archive_action(
        values={**values, "runtime_context_id": current_view.get("runtime_context_id", "")},
        close_blocker_explanation=close_blocker_explanation,
    )
    merge_dependency_projection = _runtime_context_merge_dependency_projection(
        values=values,
    )
    close_precheck_gap_projection = _runtime_context_close_precheck_gap_projection(
        values=values,
        close_gate_view=close_gate,
    )
    worker_handoff_projection = _runtime_context_worker_handoff_projection(
        values={
            **values,
            "runtime_context_id": current_view.get("runtime_context_id", ""),
        },
        read_receipt_hash_action=read_receipt_hash_action,
    )
    failed_qa_revision_projection = _runtime_context_failed_qa_revision_projection(
        lane_plan=lane_plan,
        values=values,
    )
    next_legal_action = _runtime_context_next_legal_action(
        route_token_action=route_token_action,
        read_receipt_hash_action=read_receipt_hash_action,
        values=values,
        close_gate_view=close_gate,
        lane_plan=lane_plan,
        failed_qa_revision_projection=failed_qa_revision_projection,
    )
    missing_evidence = _runtime_context_missing_evidence(
        gate_inputs_view=gate_inputs,
        lane_plan=lane_plan,
    )
    next_required_evidence = _runtime_context_next_required_evidence(
        values=values,
        route_token_action=route_token_action,
        read_receipt_hash_action=read_receipt_hash_action,
        close_gate_view=close_gate,
        lane_plan=lane_plan,
    )
    if failed_qa_revision_projection.get("status") == "revision_required":
        next_required_evidence = [
            _runtime_context_failed_qa_revision_required_item(
                failed_qa_revision_projection,
                values,
            ),
            *[
                item
                for item in next_required_evidence
                if _runtime_context_text(item.get("id")) != "failed_qa_revision"
            ],
        ]
        for index, item in enumerate(next_required_evidence):
            item["sequence_index"] = index
            item["is_next"] = index == 0
    worker_next_moves = [
        {
            "id": item.get("id", ""),
            "next_action": item.get("next_action", ""),
            "status": item.get("status", ""),
            "is_next": bool(item.get("is_next")),
            "requires": list(item.get("requires") or []),
        }
        for item in next_required_evidence
        if isinstance(item, Mapping)
    ]
    blocking_reasons: list[dict[str, Any]] = []
    if failed_qa_revision_projection.get("status") == "revision_required":
        blocking_reasons.append(
            {
                "code": "failed_independent_qa",
                "message": "independent QA failed; revise implementation before generic close/startup actions",
                "next_action": "revise_after_failed_independent_qa",
                "event_ref": failed_qa_revision_projection.get(
                    "failed_qa_event_ref", ""
                ),
            }
        )
    if route_token_action.get("status") in {"missing", "stale"}:
        blocking_reasons.append(
            {
                "code": f"route_token_{route_token_action.get('status')}",
                "message": route_token_action.get("issue", ""),
                "next_action": route_token_action.get("next_action", ""),
            }
        )
    if read_receipt_hash_action.get("status") == "missing":
        blocking_reasons.append(
            {
                "code": "read_receipt_missing",
                "message": "runtime-context read receipt hash evidence is missing",
                "next_action": read_receipt_hash_action.get("next_action", ""),
            }
        )
    if worker_handoff_projection.get("status") in {
        "no_worker_startup_evidence",
        "read_receipt_missing",
        "worker_lineage_missing_progress_observed",
    }:
        blocking_reasons.append(
            {
                "code": "worker_handoff_no_startup_progress",
                "message": (
                    "worker dispatch has incomplete startup lineage; observer "
                    "must follow the worker_handoff_projection recovery action "
                    "instead of backfilling worker evidence"
                ),
                "next_action": worker_handoff_projection.get(
                    "observer_next_action",
                    "",
                ),
            }
        )
    for gap in close_precheck_gap_projection.get("gaps") or []:
        if isinstance(gap, Mapping):
            blocking_reasons.append(
                {
                    "code": _runtime_context_text(gap.get("code")),
                    "message": _runtime_context_text(gap.get("message")),
                    "next_action": _runtime_context_text(gap.get("next_action")),
                }
            )
    for event in lane_plan.get("blocking_events") or []:
        if isinstance(event, Mapping):
            blocking_reasons.append(
                {
                    "code": "lane_blocking_event",
                    "message": _runtime_context_text(event.get("event_kind")),
                    "event_ref": _runtime_context_text(event.get("event_ref")),
                    "status": _runtime_context_text(event.get("status")),
                }
            )
    for explanation in close_blocker_explanation.get("explanations") or []:
        if isinstance(explanation, Mapping):
            blocking_reasons.append(
                {
                    "code": _runtime_context_text(explanation.get("code")),
                    "message": _runtime_context_text(explanation.get("message")),
                    "field": _runtime_context_text(explanation.get("field")),
                }
            )
    return {
        "schema_version": RUNTIME_CONTEXT_ACTION_PLAN_SCHEMA_VERSION,
        "runtime_context_id": current_view.get("runtime_context_id", ""),
        "task_id": values.get("task_id", ""),
        "parent_task_id": values.get("parent_task_id", ""),
        "next_legal_action": next_legal_action,
        "worker_next_moves": worker_next_moves,
        "next_required_evidence": next_required_evidence,
        "missing_evidence": missing_evidence,
        "blocking_reasons": blocking_reasons,
        "route_token_action": route_token_action,
        "read_receipt_hash_action": read_receipt_hash_action,
        "worker_handoff_projection": worker_handoff_projection,
        "audit_archive_action": audit_archive_action,
        "merge_dependency_projection": merge_dependency_projection,
        "close_precheck_gap_projection": close_precheck_gap_projection,
        "failed_qa_revision_projection": failed_qa_revision_projection,
        "done_state_projection": close_precheck_gap_projection.get(
            "done_state_projection",
            {},
        ),
        "close_blocker_explanation": close_blocker_explanation,
        "deferred_hardening": {
            "permission_tree": "deferred_next_layer",
            "capability_subtree": "deferred_next_layer",
            "granted_subtree_root_hash": "deferred_next_layer",
            "implemented_in_this_slice": False,
        },
        "source_views": [
            "lane_plan",
            "gate_inputs",
            "route_identity",
            "content_address",
            "close_gate_view",
        ],
    }


def build_runtime_context_timeline_gate_projection(
    *,
    timeline_events: Sequence[Mapping[str, Any]] | None,
    contract_revision: BranchRuntimeContractRevision | Mapping[str, Any] | None = None,
    request_id: str = "",
) -> dict[str, Any]:
    """Project authoritative MF timeline-gate output into safe diagnostics."""

    safe_events = [
        public_contract_revision_payload(event)
        for event in timeline_events or ()
        if isinstance(event, Mapping)
    ]
    base_projection = {
        "schema_version": RUNTIME_CONTEXT_TIMELINE_GATE_PROJECTION_SCHEMA_VERSION,
        "projection_only": True,
        "must_revalidate_on_write": True,
        "source": "task_timeline.mf_close_gate_verification",
        "compact_source": "task_timeline.compact_gate_summary",
        "repair_source": "task_timeline.repair_gate_summary",
        "can_authorize_write": False,
        "can_authorize_close": False,
        "authoritative_close_verdict_redacted": True,
    }
    if not safe_events:
        return {
            **base_projection,
            "available": False,
            "status": "unavailable",
            "reason": "timeline_events_unavailable",
            "event_count": 0,
            "missing_event_kinds": [],
            "failed_gates": [],
            "repair_reasons": [],
            "next_legal_actions": [],
            "missing_event_repairs": [],
            "failed_gate_repairs": [],
        }
    try:
        from . import task_timeline

        contract = _runtime_context_revision_mapping(contract_revision)
        full_gate = task_timeline.mf_close_gate_verification(
            safe_events,
            contract=contract,
        )
        compact = task_timeline.compact_gate_summary(
            full_gate,
            request_id=request_id,
        )
        repair = task_timeline.repair_gate_summary(
            full_gate,
            request_id=request_id,
        )
    except Exception as exc:
        return {
            **base_projection,
            "available": False,
            "status": "error",
            "reason": "timeline_gate_projection_error",
            "error": str(exc),
            "event_count": len(safe_events),
            "missing_event_kinds": [],
            "failed_gates": [],
            "repair_reasons": [],
            "next_legal_actions": [],
            "missing_event_repairs": [],
            "failed_gate_repairs": [],
        }

    audit_archive_recovery = _runtime_context_mapping(
        repair.get("audit_archive_recovery")
    )
    sanitized_audit_archive_recovery: dict[str, Any] = {}
    if audit_archive_recovery:
        body_skeleton = _runtime_context_mapping(
            audit_archive_recovery.get("body_skeleton")
        )
        sanitized_audit_archive_recovery = {
            "schema_version": _runtime_context_text(
                audit_archive_recovery.get("schema_version")
                or "mf_audit_archive_recovery.v1"
            ),
            "recommended_legal_action": (
                "Use backlog_audit_archive only for accepted work whose "
                "ordinary MF close evidence is non-reconstructable; it records "
                "WAIVED audit state and is not normal close authorization."
            ),
            "mcp_tool": _runtime_context_text(
                audit_archive_recovery.get("mcp_tool")
            ),
            "http_method": _runtime_context_text(
                audit_archive_recovery.get("http_method")
            ),
            "http_path": _runtime_context_text(
                audit_archive_recovery.get("http_path")
            ),
            "row_status": _runtime_context_text(
                audit_archive_recovery.get("row_status")
            ),
            "normal_close_authorization": False,
            "close_satisfying_by_itself": bool(
                audit_archive_recovery.get("close_satisfying_by_itself")
            ),
            "requires_independent_qa": bool(
                audit_archive_recovery.get("requires_independent_qa")
            ),
            "requires_non_reconstructable_evidence_reason": bool(
                audit_archive_recovery.get(
                    "requires_non_reconstructable_evidence_reason"
                )
            ),
            "body_required_fields": sorted(body_skeleton) if body_skeleton else [],
        }
    return {
        **base_projection,
        "available": True,
        "status": _runtime_context_text(full_gate.get("status")),
        "diagnostic_result": "passed" if full_gate.get("passed") else "failed",
        "event_count": int(full_gate.get("event_count") or len(safe_events)),
        "projected_event_count": int(full_gate.get("projected_event_count") or 0),
        "missing_event_kinds": list(compact.get("missing_event_kinds") or []),
        "failed_gates": copy.deepcopy(list(compact.get("failed_gates") or [])),
        "repair_reasons": copy.deepcopy(list(compact.get("repair_reasons") or [])),
        "next_legal_actions": _runtime_context_dedupe(
            list(compact.get("next_legal_actions") or [])
            + list(repair.get("next_legal_actions") or [])
        ),
        "missing_event_repairs": copy.deepcopy(
            list(repair.get("missing_event_repairs") or [])
        ),
        "failed_gate_repairs": copy.deepcopy(
            list(repair.get("failed_gate_repairs") or [])
        ),
        "failed_gate_count": int(repair.get("failed_gate_count") or 0),
        "route_identity": copy.deepcopy(dict(compact.get("route_identity") or {})),
        "audit_archive_recovery": sanitized_audit_archive_recovery,
        "request_id": request_id,
    }


def build_runtime_context_gate_projection_view(
    *,
    gate_inputs_view: Mapping[str, Any],
    close_gate_view: Mapping[str, Any],
    action_plan_view: Mapping[str, Any],
    timeline_gate_projection: Mapping[str, Any] | None = None,
    viewer_role: str = RUNTIME_CONTEXT_WORKER_ROLE,
) -> dict[str, Any]:
    """Aggregate gate diagnostics without minting a write/close verdict."""

    gate_inputs = dict(gate_inputs_view)
    close_gate = dict(close_gate_view)
    action_plan = dict(action_plan_view)
    timeline_gate = (
        dict(timeline_gate_projection)
        if isinstance(timeline_gate_projection, Mapping)
        else build_runtime_context_timeline_gate_projection(
            timeline_events=None,
            contract_revision=None,
        )
    )
    audit_archive_action = _runtime_context_mapping(
        action_plan.get("audit_archive_action")
    )
    worker_handoff_projection = _runtime_context_mapping(
        action_plan.get("worker_handoff_projection")
    )
    audit_archive_entrypoint = _runtime_context_mapping(
        audit_archive_action.get("entrypoint")
    )
    sanitized_audit_archive_action = {
        "schema_version": _runtime_context_text(
            audit_archive_action.get("schema_version")
            or "runtime_context.audit_archive_action.v1"
        ),
        "status": _runtime_context_text(audit_archive_action.get("status")),
        "next_action": _runtime_context_text(
            audit_archive_action.get("next_action")
        ),
        "archive_action": _runtime_context_text(
            audit_archive_action.get("archive_action")
        ),
        "ordinary_close_gate_claimed": bool(
            audit_archive_action.get("ordinary_close_gate_claimed")
        ),
        "entrypoint": {
            key: copy.deepcopy(audit_archive_entrypoint.get(key))
            for key in (
                "method",
                "path",
                "mcp_tool",
                "required_public_fields",
                "request_template",
            )
            if key in audit_archive_entrypoint
        },
        "blocker_codes": list(audit_archive_action.get("blocker_codes") or []),
        "projection_note": (
            "Audit archive is presented as a recovery option only; normal "
            "close authorization remains owned by protected close gates."
        ),
    }
    gate_inputs_missing = [
        copy.deepcopy(item)
        for item in gate_inputs.get("missing") or []
        if isinstance(item, Mapping)
    ]
    close_gate_missing = [
        copy.deepcopy(item)
        for item in close_gate.get("missing") or []
        if isinstance(item, Mapping)
    ]
    missing_evidence = [
        copy.deepcopy(item)
        for item in action_plan.get("missing_evidence") or []
        if isinstance(item, Mapping)
    ]
    next_required_evidence = [
        copy.deepcopy(item)
        for item in action_plan.get("next_required_evidence") or []
        if isinstance(item, Mapping)
    ]
    blocking_reasons = [
        copy.deepcopy(item)
        for item in action_plan.get("blocking_reasons") or []
        if isinstance(item, Mapping)
    ]
    close_gaps = [
        copy.deepcopy(item)
        for item in _runtime_context_mapping(
            action_plan.get("close_precheck_gap_projection")
        ).get("gaps")
        or []
        if isinstance(item, Mapping)
    ]
    blocked = bool(
        gate_inputs_missing
        or close_gate_missing
        or missing_evidence
        or next_required_evidence
        or blocking_reasons
        or close_gaps
    )
    close_projection_ready = bool(close_gate.get("ready"))
    projection_status = (
        "diagnostic_blocked"
        if blocked or not close_projection_ready
        else "diagnostic_clear_requires_write_revalidation"
    )
    return {
        "schema_version": RUNTIME_CONTEXT_GATE_PROJECTION_SCHEMA_VERSION,
        "projection_only": True,
        "must_revalidate_on_write": True,
        "raw_session_token_exposed": False,
        "raw_route_token_exposed": False,
        "raw_fence_token_exposed": False,
        "role_scope": _runtime_context_text(viewer_role) or RUNTIME_CONTEXT_WORKER_ROLE,
        "viewer_role": _runtime_context_text(viewer_role) or RUNTIME_CONTEXT_WORKER_ROLE,
        "runtime_context_id": _runtime_context_text(
            action_plan.get("runtime_context_id")
            or gate_inputs.get("runtime_context_id")
            or close_gate.get("runtime_context_id")
        ),
        "task_id": _runtime_context_text(
            action_plan.get("task_id")
            or gate_inputs.get("task_id")
            or close_gate.get("task_id")
        ),
        "projection_status": projection_status,
        "diagnostic_status": (
            "missing_required_evidence"
            if blocked or not close_projection_ready
            else "no_known_projection_gaps"
        ),
        "source_views": [
            "gate_inputs",
            "close_gate_view",
            "action_plan",
            "task_timeline",
        ],
        "authoritative_timeline_gate": timeline_gate,
        "source_view_status": {
            "gate_inputs": _runtime_context_text(gate_inputs.get("status")),
            "close_gate_view": _runtime_context_text(close_gate.get("status")),
            "action_plan": _runtime_context_text(
                action_plan.get("schema_version")
            ),
            "authoritative_timeline_gate": _runtime_context_text(
                timeline_gate.get("status")
            ),
        },
        "next_legal_action": _runtime_context_text(
            action_plan.get("next_legal_action")
        ),
        "gate_inputs_missing": gate_inputs_missing,
        "close_gate_missing": close_gate_missing,
        "next_required_evidence": next_required_evidence,
        "missing_evidence": missing_evidence,
        "blocking_reasons": blocking_reasons,
        "worker_handoff_projection": copy.deepcopy(worker_handoff_projection),
        "audit_archive_action": sanitized_audit_archive_action,
        "close_precheck_gap_projection": copy.deepcopy(
            dict(action_plan.get("close_precheck_gap_projection") or {})
        ),
        "close_gate_diagnostic": {
            "schema_version": "runtime_context.gate_projection.close_gate_diagnostic.v1",
            "projection_status": "diagnostic_ready"
            if close_projection_ready
            else "diagnostic_blocked",
            "diagnostic_status": _runtime_context_text(close_gate.get("status")),
            "close_gate_projection_ready": close_projection_ready,
            "ready_view_is_diagnostic_only": True,
            "write_authorization_provided": False,
            "authoritative_close_authorization": "not_evaluated",
            "must_revalidate_on_write": True,
            "message": (
                "close_gate_view readiness is a Runtime Context diagnostic; "
                "protected write and close endpoints must rerun authoritative gates."
            ),
        },
        "write_boundary": {
            "protected_endpoints_must_rerun_authoritative_gates": True,
            "projection_fields_accepted_as_write_evidence": False,
            "projection_fields_accepted_as_close_evidence": False,
            "blocked_actions": [
                "treat_gate_projection_as_authoritative_close_authorization",
                "treat_gate_projection_as_normal_close_evidence",
                "treat_gate_projection_as_worker_startup_or_finish_acceptance",
            ],
        },
        "redaction": {
            "raw_session_token_exposed": False,
            "raw_route_token_exposed": False,
            "raw_fence_token_exposed": False,
            "observer_only_authority_exposed": False,
        },
    }


def build_runtime_context_control_plane_view(
    action_plan_view: Mapping[str, Any],
    *,
    capability_boundary_view: Mapping[str, Any] | None = None,
    gate_projection_view: Mapping[str, Any] | None = None,
    viewer_role: str = RUNTIME_CONTEXT_WORKER_ROLE,
) -> dict[str, Any]:
    """Expose the action plan under a stable Runtime Context control-plane view."""

    action_plan = dict(action_plan_view)
    normalized_viewer_role = _runtime_context_text(viewer_role) or RUNTIME_CONTEXT_WORKER_ROLE
    capability_boundary = (
        dict(capability_boundary_view)
        if isinstance(capability_boundary_view, Mapping)
        else {}
    )
    gate_projection = (
        dict(gate_projection_view)
        if isinstance(gate_projection_view, Mapping)
        else build_runtime_context_gate_projection_view(
            gate_inputs_view=action_plan.get("gate_inputs") or {},
            close_gate_view=action_plan.get("close_gate_view") or {},
            action_plan_view=action_plan,
            viewer_role=normalized_viewer_role,
        )
    )
    gate_projection["role_scope"] = normalized_viewer_role
    gate_projection["viewer_role"] = normalized_viewer_role
    gate_projection["raw_session_token_exposed"] = False
    gate_projection["raw_route_token_exposed"] = False
    gate_projection["raw_fence_token_exposed"] = False
    return {
        "schema_version": RUNTIME_CONTEXT_CONTROL_PLANE_SCHEMA_VERSION,
        "role_scope": normalized_viewer_role,
        "viewer_role": normalized_viewer_role,
        "raw_session_token_exposed": False,
        "raw_route_token_exposed": False,
        "raw_fence_token_exposed": False,
        "runtime_context_id": action_plan.get("runtime_context_id", ""),
        "task_id": action_plan.get("task_id", ""),
        "next_legal_action": action_plan.get("next_legal_action", ""),
        "worker_next_moves": list(action_plan.get("worker_next_moves") or []),
        "next_required_evidence": list(
            action_plan.get("next_required_evidence") or []
        ),
        "missing_evidence": list(action_plan.get("missing_evidence") or []),
        "blocking_reasons": list(action_plan.get("blocking_reasons") or []),
        "gate_projection": gate_projection,
        "route_token_action": dict(action_plan.get("route_token_action") or {}),
        "read_receipt_hash_action": dict(
            action_plan.get("read_receipt_hash_action") or {}
        ),
        "worker_handoff_projection": dict(
            action_plan.get("worker_handoff_projection") or {}
        ),
        "audit_archive_action": dict(action_plan.get("audit_archive_action") or {}),
        "merge_dependency_projection": dict(
            action_plan.get("merge_dependency_projection") or {}
        ),
        "close_precheck_gap_projection": dict(
            action_plan.get("close_precheck_gap_projection") or {}
        ),
        "done_state_projection": dict(
            action_plan.get("done_state_projection") or {}
        ),
        "close_blocker_explanation": dict(
            action_plan.get("close_blocker_explanation") or {}
        ),
        "capability_boundary": capability_boundary,
        "capability_boundary_hash": _runtime_context_text(
            capability_boundary.get("capability_boundary_hash")
        )
        or (runtime_context_content_hash(capability_boundary) if capability_boundary else ""),
        "deferred_hardening": dict(action_plan.get("deferred_hardening") or {}),
        "action_plan": action_plan,
    }


def _runtime_context_normalized_path(value: Any) -> str:
    text = _runtime_context_text(value)
    if not text:
        return ""
    try:
        path = Path(text).expanduser()
        if path.is_absolute():
            return str(path.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        pass
    return text.rstrip("/")


def _runtime_context_path_matches(left: Any, right: Any) -> bool:
    left_text = _runtime_context_normalized_path(left)
    right_text = _runtime_context_normalized_path(right)
    return bool(left_text and right_text and left_text == right_text)


def _runtime_context_worker_execution_safety(values: Mapping[str, Any]) -> dict[str, Any]:
    assigned_worktree = _runtime_context_text(values.get("worktree_path"))
    target_project_root = _runtime_context_text(
        values.get("target_project_root") or assigned_worktree
    )
    startup_event_ref = _runtime_context_text(values.get("startup_event_ref"))
    startup_actual_cwd = _runtime_context_text(values.get("startup_actual_cwd"))
    startup_actual_git_root = _runtime_context_text(values.get("startup_actual_git_root"))
    cwd_matches = _runtime_context_path_matches(startup_actual_cwd, assigned_worktree)
    git_root_matches = _runtime_context_path_matches(
        startup_actual_git_root,
        assigned_worktree,
    )
    verified_workdir = bool(
        assigned_worktree
        and startup_event_ref
        and startup_actual_cwd
        and startup_actual_git_root
        and cwd_matches
        and git_root_matches
    )
    blockers: list[dict[str, Any]] = []
    if not assigned_worktree:
        blockers.append(
            {
                "code": "assigned_worktree_missing",
                "message": "branch runtime context is missing assigned worktree_path",
                "next_action": "refresh_branch_runtime_context",
            }
        )
    if not startup_event_ref:
        blockers.append(
            {
                "code": "pre_edit_startup_missing",
                "message": (
                    "record real mf_subagent_startup with actual_cwd and "
                    "actual_git_root before implementation edits"
                ),
                "next_action": "record_mf_subagent_startup",
            }
        )
    elif not startup_actual_cwd or not startup_actual_git_root:
        missing = []
        if not startup_actual_cwd:
            missing.append("actual_cwd")
        if not startup_actual_git_root:
            missing.append("actual_git_root")
        blockers.append(
            {
                "code": "pre_edit_workdir_evidence_missing",
                "message": "startup evidence does not prove the worker session workdir",
                "missing": missing,
                "next_action": "record_mf_subagent_startup",
            }
        )
    else:
        if not cwd_matches:
            blockers.append(
                {
                    "code": "actual_cwd_not_assigned_worktree",
                    "message": "startup actual_cwd does not match assigned worktree_path",
                    "actual_cwd": startup_actual_cwd,
                    "assigned_worktree_path": assigned_worktree,
                    "next_action": "stop_and_relaunch_in_assigned_worktree",
                }
            )
        if not git_root_matches:
            blockers.append(
                {
                    "code": "actual_git_root_not_assigned_worktree",
                    "message": "startup actual_git_root does not match assigned worktree_path",
                    "actual_git_root": startup_actual_git_root,
                    "assigned_worktree_path": assigned_worktree,
                    "next_action": "stop_and_relaunch_in_assigned_worktree",
                }
            )
    return {
        "schema_version": RUNTIME_CONTEXT_WORKER_EXECUTION_SAFETY_SCHEMA_VERSION,
        "status": "verified" if verified_workdir else "pre_edit_blocked",
        "assigned_worktree_path": assigned_worktree,
        "target_project_root": target_project_root,
        "startup_event_ref": startup_event_ref,
        "startup_actual_cwd": startup_actual_cwd,
        "startup_actual_git_root": startup_actual_git_root,
        "actual_cwd_matches_assigned_worktree": cwd_matches,
        "actual_git_root_matches_assigned_worktree": git_root_matches,
        "session_cwd_observed_by_runtime": bool(startup_actual_cwd),
        "actual_git_root_observed_by_runtime": bool(startup_actual_git_root),
        "relative_patch_safe": verified_workdir,
        "apply_patch_relative_paths_allowed": verified_workdir,
        "required_patch_path_mode": (
            "verified_workdir_or_absolute_paths_under_assigned_worktree"
        ),
        "absolute_path_prefix": assigned_worktree,
        "pre_edit_required_evidence": [
            "mf_subagent_startup.actual_cwd",
            "mf_subagent_startup.actual_git_root",
            "mf_subagent_startup.worktree_path",
        ],
        "pre_edit_blockers": blockers,
        "recovery_actions": [
            {
                "id": "verify_session_cwd",
                "action": "verify_session_cwd",
                "commands": ["pwd", "git rev-parse --show-toplevel"],
                "expected_git_root": assigned_worktree,
            },
            {
                "id": "record_startup_before_edit",
                "action": "record_mf_subagent_startup",
            },
            {
                "id": "use_absolute_paths",
                "action": "apply edits using absolute paths under assigned_worktree_path",
            },
        ],
        "raw_session_token_exposed": False,
        "raw_fence_token_exposed": False,
    }


def build_runtime_context_capability_boundary_view(
    current_view: Mapping[str, Any],
) -> dict[str, Any]:
    """Project public worker capability bounds from existing runtime fields."""

    values = _runtime_context_mapping(current_view.get("current_values"))
    graph_identity = _runtime_context_mapping(values.get("graph_query_identity"))
    runtime_context_id = _runtime_context_text(
        current_view.get("runtime_context_id") or values.get("runtime_context_id")
    )
    task_id = _runtime_context_text(values.get("task_id"))
    role = _runtime_context_text(values.get("worker_role") or RUNTIME_CONTEXT_WORKER_ROLE)
    target_files = _runtime_context_dedupe(
        _runtime_context_string_list(values.get("target_files"))
    )
    owned_files = _runtime_context_dedupe(
        _runtime_context_string_list(values.get("owned_files")) or target_files
    )
    target_project_root = _runtime_context_text(
        graph_identity.get("target_project_root")
        or values.get("target_project_root")
        or values.get("worktree_path")
    )
    graph_scope = {
        "query_source": _runtime_context_text(
            graph_identity.get("query_source") or "mf_subagent"
        ),
        "query_purpose": _runtime_context_text(
            graph_identity.get("query_purpose")
            or graph_identity.get("purpose")
            or "subagent_context_build"
        ),
        "allowed_query_purposes": [
            "subagent_context_build",
            "subagent_gate_validation",
        ],
        "worker_role": _runtime_context_text(graph_identity.get("worker_role") or role),
        "runtime_context_id": runtime_context_id,
        "task_id": _runtime_context_text(graph_identity.get("task_id") or task_id),
        "parent_task_id": _runtime_context_text(
            graph_identity.get("parent_task_id") or values.get("parent_task_id")
        ),
        "governance_project_id": _runtime_context_text(
            graph_identity.get("governance_project_id")
            or values.get("governance_project_id")
            or values.get("project_id")
        ),
        "target_project_id": _runtime_context_text(
            graph_identity.get("target_project_id")
            or values.get("target_project_id")
            or values.get("project_id")
        ),
        "target_project_root": target_project_root,
        "project_root": _runtime_context_text(
            graph_identity.get("project_root")
            or values.get("project_root")
            or target_project_root
        ),
        "repo_root": _runtime_context_text(
            graph_identity.get("repo_root")
            or values.get("repo_root")
            or target_project_root
        ),
    }
    fence_token_present = bool(values.get("fence_token_present"))
    worker_execution_safety = _runtime_context_worker_execution_safety(values)
    boundary = {
        "schema_version": RUNTIME_CONTEXT_CAPABILITY_BOUNDARY_SCHEMA_VERSION,
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "role": role,
        "owned_files": owned_files,
        "target_files": target_files,
        "fence_token_present": fence_token_present,
        "fence_token_hash": _runtime_context_text(values.get("fence_token_hash")),
        "fence_token_redacted": fence_token_present,
        "graph_query_scope": graph_scope,
        "worker_execution_safety": worker_execution_safety,
        "raw_session_token_exposed": False,
        "raw_fence_token_exposed": False,
    }
    boundary["capability_boundary_hash"] = runtime_context_content_hash(boundary)
    return boundary


def build_runtime_context_worker_view(
    current_view: Mapping[str, Any],
    *,
    task_id: str = "",
    fence_token: str = "",
    role: str = RUNTIME_CONTEXT_WORKER_ROLE,
    gate_inputs_view: Mapping[str, Any] | None = None,
    close_gate_view: Mapping[str, Any] | None = None,
    action_plan_view: Mapping[str, Any] | None = None,
    control_plane_view: Mapping[str, Any] | None = None,
    capability_boundary_view: Mapping[str, Any] | None = None,
    gate_projection_view: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the mf_sub-safe role-filtered view for one worker/fence/task."""

    normalized_role = _runtime_context_text(role).lower().replace("-", "_")
    if normalized_role not in {RUNTIME_CONTEXT_WORKER_ROLE, "worker"}:
        raise BranchRuntimeFenceError("runtime_context_worker_view_role_not_allowed")
    values = _runtime_context_mapping(current_view.get("current_values"))
    expected_task_id = _runtime_context_text(values.get("task_id"))
    expected_fence_hash = _runtime_context_text(values.get("fence_token_hash"))
    requested_task_id = _runtime_context_text(task_id)
    requested_fence = _runtime_context_text(fence_token)
    if requested_task_id and requested_task_id != expected_task_id:
        raise BranchRuntimeFenceError("runtime_context_worker_view_task_mismatch")
    if requested_fence and runtime_context_secret_hash(requested_fence) != expected_fence_hash:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")

    gate_inputs = (
        dict(gate_inputs_view)
        if isinstance(gate_inputs_view, Mapping)
        else build_runtime_context_gate_inputs_view(current_view)
    )
    close_gate = (
        dict(close_gate_view)
        if isinstance(close_gate_view, Mapping)
        else build_runtime_context_close_gate_view(current_view, gate_inputs)
    )
    action_plan = (
        dict(action_plan_view)
        if isinstance(action_plan_view, Mapping)
        else build_runtime_context_action_plan_view(
            current_view,
            gate_inputs_view=gate_inputs,
            close_gate_view=close_gate,
        )
    )
    capability_boundary = (
        dict(capability_boundary_view)
        if isinstance(capability_boundary_view, Mapping)
        else build_runtime_context_capability_boundary_view(current_view)
    )
    gate_projection = (
        dict(gate_projection_view)
        if isinstance(gate_projection_view, Mapping)
        else build_runtime_context_gate_projection_view(
            gate_inputs_view=gate_inputs,
            close_gate_view=close_gate,
            action_plan_view=action_plan,
            viewer_role=RUNTIME_CONTEXT_WORKER_ROLE,
        )
    )
    gate_projection["role_scope"] = RUNTIME_CONTEXT_WORKER_ROLE
    gate_projection["viewer_role"] = RUNTIME_CONTEXT_WORKER_ROLE
    gate_projection["raw_session_token_exposed"] = False
    gate_projection["raw_route_token_exposed"] = False
    gate_projection["raw_fence_token_exposed"] = False
    control_plane = (
        dict(control_plane_view)
        if isinstance(control_plane_view, Mapping)
        else build_runtime_context_control_plane_view(
            action_plan,
            capability_boundary_view=capability_boundary,
            gate_projection_view=gate_projection,
        )
    )
    control_plane["gate_projection"] = gate_projection
    return {
        "schema_version": RUNTIME_CONTEXT_WORKER_VIEW_SCHEMA_VERSION,
        "role": RUNTIME_CONTEXT_WORKER_ROLE,
        "role_scope": RUNTIME_CONTEXT_WORKER_ROLE,
        "runtime_context_id": current_view.get("runtime_context_id", ""),
        "observer_command_id": values.get("observer_command_id", ""),
        "route_id": values.get("route_id", ""),
        "route_context_hash": values.get("route_context_hash", ""),
        "prompt_contract_id": values.get("prompt_contract_id", ""),
        "prompt_contract_hash": values.get("prompt_contract_hash", ""),
        "visible_injection_manifest_hash": values.get(
            "visible_injection_manifest_hash",
            "",
        ),
        "session_token_ref": values.get("session_token_ref", ""),
        "session_token_ref_present": bool(values.get("session_token_ref")),
        "raw_session_token_exposed": False,
        "target_files": list(values.get("target_files") or []),
        "owned_files": list(values.get("owned_files") or values.get("target_files") or []),
        "task": {
            key: values.get(key, "")
            for key in (
                "project_id",
                "observer_command_id",
                "governance_project_id",
                "target_project_id",
                "target_project_root",
                "project_root",
                "repo_root",
                "task_id",
                "parent_task_id",
                "backlog_id",
                "worker_role",
                "worker_id",
                "worker_slot_id",
                "actual_host_worker_id",
                "agent_id",
                "attempt",
                "fence_token_present",
                "fence_token_hash",
                "fence_token_redacted",
                "session_token_ref",
                "session_token_ref_present",
            )
        },
        "branch": {
            key: values.get(key, "")
            for key in (
                "branch_ref",
                "ref_name",
                "worktree_id",
                "worktree_path",
                "base_commit",
                "head_commit",
                "target_head_commit",
                "snapshot_id",
                "projection_id",
                "merge_queue_id",
                "merge_preview_id",
            )
        }
        | {
            "session_token_lease": dict(
                _runtime_context_mapping(values.get("session_token_lease"))
            ),
        },
        "route_identity": {
            key: _runtime_context_mapping(current_view.get("route_identity")).get(
                key,
                "",
            )
            for key in _SAFE_CONTRACT_REVISION_ROUTE_KEYS
            if _runtime_context_mapping(current_view.get("route_identity")).get(key)
        }
        | {"raw_private_context_exposed": False},
        "work": dict(_runtime_context_mapping(current_view.get("work"))),
        "graph_query_identity": values.get("graph_query_identity", {}),
        "session_token_lease": dict(
            _runtime_context_mapping(values.get("session_token_lease"))
        ),
        "worker_execution_safety": dict(
            capability_boundary.get("worker_execution_safety") or {}
        ),
        "capability_boundary": capability_boundary,
        "capability_boundary_hash": _runtime_context_text(
            capability_boundary.get("capability_boundary_hash")
        )
        or runtime_context_content_hash(capability_boundary),
        "lane_plan": dict(_runtime_context_mapping(current_view.get("lane_plan"))),
        "terminal_dispatch_blockers": list(
            current_view.get("terminal_dispatch_blockers")
            or values.get("terminal_dispatch_blockers")
            or []
        ),
        "next_legal_action": action_plan.get("next_legal_action", ""),
        "worker_next_moves": list(action_plan.get("worker_next_moves") or []),
        "next_required_evidence": list(
            action_plan.get("next_required_evidence") or []
        ),
        "blocking_reasons": list(action_plan.get("blocking_reasons") or []),
        "done_state_projection": dict(action_plan.get("done_state_projection") or {}),
        "gate_inputs": gate_inputs,
        "close_gate_view": close_gate,
        "action_plan": action_plan,
        "gate_projection": gate_projection,
        "control_plane": control_plane,
        "evidence_refs": {
            key: _runtime_context_mapping(current_view.get("evidence_refs")).get(
                key,
                {},
            )
            for key in (
                "branch_runtime",
                "contract_revision",
                "route_identity",
                "timeline",
                "graph_trace",
            )
        },
        "role_filter_policy": {
            "schema_version": RUNTIME_CONTEXT_ROLE_FILTER_POLICY_SCHEMA_VERSION,
            "role": RUNTIME_CONTEXT_WORKER_ROLE,
            "allowed_for_task_id": expected_task_id,
            "fence_token_required": True,
            "allowed_sections": [
                "task",
                "branch",
                "route_identity",
                "work",
                "lane_plan",
                "terminal_dispatch_blockers",
                "graph_query_identity",
                "session_token_lease",
                "session_token_ref",
                "worker_execution_safety",
                "capability_boundary",
                "next_required_evidence",
                "gate_inputs",
                "close_gate_view",
                "action_plan",
                "gate_projection",
                "control_plane",
                "evidence_refs",
            ],
            "blocked_sections": [
                "observer_controls",
                "observer_only_context",
                "private_context",
                "raw_source_payloads",
                "other_worker_contexts",
            ],
            "raw_private_context_exposed": False,
            "other_worker_contexts_exposed": False,
        },
        "privacy_boundary": {
            "raw_private_context_exposed": False,
            "other_worker_contexts_exposed": False,
            "raw_source_of_truth_copied": False,
        },
    }


def _runtime_context_role_scoped_view(
    worker_view: Mapping[str, Any],
    *,
    role: str,
) -> dict[str, Any]:
    """Project a sanitized worker-shaped view for non-worker reviewers."""

    normalized_role = _runtime_context_text(role).lower().replace("-", "_")
    role_view = copy.deepcopy(dict(worker_view))
    role_view["role"] = normalized_role
    role_view["role_scope"] = normalized_role

    control_plane = dict(role_view.get("control_plane") or {})
    control_plane["role_scope"] = normalized_role
    control_plane["viewer_role"] = normalized_role
    control_plane["raw_session_token_exposed"] = False
    control_plane["raw_route_token_exposed"] = False
    control_plane["raw_fence_token_exposed"] = False
    gate_projection = dict(role_view.get("gate_projection") or {})
    gate_projection["role_scope"] = normalized_role
    gate_projection["viewer_role"] = normalized_role
    gate_projection["raw_session_token_exposed"] = False
    gate_projection["raw_route_token_exposed"] = False
    gate_projection["raw_fence_token_exposed"] = False
    role_view["gate_projection"] = gate_projection
    control_plane["gate_projection"] = gate_projection
    role_view["control_plane"] = control_plane

    role_filter_policy = dict(role_view.get("role_filter_policy") or {})
    role_filter_policy["role"] = normalized_role
    role_filter_policy["raw_private_context_exposed"] = False
    role_filter_policy["other_worker_contexts_exposed"] = False
    role_view["role_filter_policy"] = role_filter_policy

    return role_view


def build_runtime_context_projection(
    context: BranchTaskRuntimeContext,
    *,
    contract_revision: BranchRuntimeContractRevision | Mapping[str, Any] | None = None,
    route_identity: Mapping[str, Any] | None = None,
    route_gate: Mapping[str, Any] | None = None,
    timeline_refs: Mapping[str, Any] | None = None,
    graph_trace_refs: Mapping[str, Any] | Sequence[str] | None = None,
    startup_gate: Mapping[str, Any] | None = None,
    finish_gate: Mapping[str, Any] | None = None,
    close_evidence: Mapping[str, Any] | None = None,
    target_files: Sequence[str] | None = None,
    acceptance_criteria: Sequence[str] | None = None,
    required_evidence: Sequence[str] | None = None,
    timeline_events: Sequence[Mapping[str, Any]] | None = None,
    lane_required_clauses: Sequence[str | Mapping[str, Any]] | None = None,
    role: str = RUNTIME_CONTEXT_WORKER_ROLE,
    fence_token: str = "",
    generated_at: str = "",
) -> RuntimeContextProjection:
    """Build all Runtime Context Service projections for internal consumers."""

    current = build_runtime_context_current_view(
        context,
        contract_revision=contract_revision,
        route_identity=route_identity,
        route_gate=route_gate,
        timeline_refs=timeline_refs,
        graph_trace_refs=graph_trace_refs,
        startup_gate=startup_gate,
        finish_gate=finish_gate,
        close_evidence=close_evidence,
        target_files=target_files,
        acceptance_criteria=acceptance_criteria,
        required_evidence=required_evidence,
        timeline_events=timeline_events,
        lane_required_clauses=lane_required_clauses,
        generated_at=generated_at,
    )
    gate_inputs = build_runtime_context_gate_inputs_view(current)
    close_gate = build_runtime_context_close_gate_view(current, gate_inputs)
    action_plan = build_runtime_context_action_plan_view(
        current,
        gate_inputs_view=gate_inputs,
        close_gate_view=close_gate,
    )
    capability_boundary = build_runtime_context_capability_boundary_view(current)
    timeline_gate_projection = build_runtime_context_timeline_gate_projection(
        timeline_events=timeline_events,
        contract_revision=contract_revision,
        request_id=runtime_context_id_for_branch_context(context),
    )
    gate_projection = build_runtime_context_gate_projection_view(
        gate_inputs_view=gate_inputs,
        close_gate_view=close_gate,
        action_plan_view=action_plan,
        timeline_gate_projection=timeline_gate_projection,
        viewer_role=RUNTIME_CONTEXT_WORKER_ROLE,
    )
    control_plane = build_runtime_context_control_plane_view(
        action_plan,
        capability_boundary_view=capability_boundary,
        gate_projection_view=gate_projection,
    )
    worker_view = build_runtime_context_worker_view(
        current,
        task_id=context.task_id,
        fence_token=fence_token or context.fence_token,
        role=role,
        gate_inputs_view=gate_inputs,
        close_gate_view=close_gate,
        action_plan_view=action_plan,
        control_plane_view=control_plane,
        capability_boundary_view=capability_boundary,
        gate_projection_view=gate_projection,
    )
    observer_view = _runtime_context_role_scoped_view(worker_view, role="observer")
    qa_view = _runtime_context_role_scoped_view(worker_view, role="qa")
    judge_view = _runtime_context_role_scoped_view(worker_view, role="judge")
    return RuntimeContextProjection(
        project_id=context.project_id,
        runtime_context_id=runtime_context_id_for_branch_context(context),
        current=current,
        gate_inputs=gate_inputs,
        action_plan=action_plan,
        gate_projection=gate_projection,
        control_plane=control_plane,
        capability_boundary=capability_boundary,
        worker_view=worker_view,
        observer_view=observer_view,
        qa_view=qa_view,
        judge_view=judge_view,
        close_gate_view=close_gate,
    )


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
    target_project_root = _runtime_context_text(
        context.target_project_root or context.worktree_path
    )
    owned_files = _runtime_context_string_list(context.owned_files)
    target_files = _runtime_context_string_list(context.target_files) or list(owned_files)
    conn.execute(
        """
        INSERT INTO parallel_branch_runtime_contexts (
            project_id, task_id, runtime_context_id, batch_id, backlog_id, parent_task_id, chain_id, root_task_id,
            stage_task_id, stage_type, retry_round, agent_id, worker_id,
            allocation_owner, worker_slot_id, actual_host_worker_id,
            host_startup_id, host_session_id, governance_project_id,
            target_project_id, target_project_root, target_files_json,
            owned_files_json, attempt, lease_id, lease_expires_at, fence_token,
            branch_ref, ref_name, worktree_id, worktree_path,
            base_commit, head_commit, target_head_commit,
            session_token_hash,
            snapshot_id, projection_id, merge_queue_id, merge_preview_id,
            rollback_epoch, replay_epoch, status, depends_on_json,
            checkpoint_id, replay_source, last_recovery_action,
            created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(project_id, task_id) DO UPDATE SET
            runtime_context_id = excluded.runtime_context_id,
            batch_id = excluded.batch_id,
            backlog_id = excluded.backlog_id,
            parent_task_id = CASE
                WHEN excluded.parent_task_id != '' THEN excluded.parent_task_id
                ELSE parallel_branch_runtime_contexts.parent_task_id
            END,
            chain_id = excluded.chain_id,
            root_task_id = excluded.root_task_id,
            stage_task_id = excluded.stage_task_id,
            stage_type = excluded.stage_type,
            retry_round = excluded.retry_round,
            agent_id = excluded.agent_id,
            worker_id = excluded.worker_id,
            allocation_owner = excluded.allocation_owner,
            worker_slot_id = excluded.worker_slot_id,
            actual_host_worker_id = excluded.actual_host_worker_id,
            host_startup_id = excluded.host_startup_id,
            host_session_id = excluded.host_session_id,
            governance_project_id = excluded.governance_project_id,
            target_project_id = excluded.target_project_id,
            target_project_root = excluded.target_project_root,
            target_files_json = excluded.target_files_json,
            owned_files_json = excluded.owned_files_json,
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
            session_token_hash = CASE
                WHEN excluded.session_token_hash != '' THEN excluded.session_token_hash
                ELSE parallel_branch_runtime_contexts.session_token_hash
            END,
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
            _runtime_context_text(context.parent_task_id),
            context.chain_id,
            context.root_task_id,
            context.stage_task_id,
            context.stage_type,
            context.retry_round,
            context.agent_id,
            context.worker_id,
            context.allocation_owner or context.agent_id,
            context.worker_slot_id or context.worker_id,
            context.actual_host_worker_id,
            context.host_startup_id,
            context.host_session_id,
            context.governance_project_id or context.project_id,
            context.target_project_id or context.project_id,
            target_project_root,
            _json_array(target_files),
            _json_array(owned_files),
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
            context.session_token_hash,
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


def reissue_mf_subagent_runtime_session_token(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    runtime_context_id: str,
    task_id: str,
    fence_token: str,
    session_token: str,
    parent_task_id: str = "",
    target_project_root: str = "",
    ttl_seconds: Any = None,
    now_iso: str = "",
) -> dict[str, Any]:
    """Rotate an mf_sub runtime session token for an active matching context.

    This is a narrow recovery path for system-repair workers whose scoped token
    lease expires. It accepts the expired-but-matching current token and never
    persists the replacement token in clear text.
    """

    ensure_branch_runtime_schema(conn)
    runtime_id = str(runtime_context_id or "").strip()
    task = str(task_id or "").strip()
    fence = str(fence_token or "").strip()
    presented_token = str(session_token or "").strip()
    if not runtime_id or not task or not fence or not presented_token:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    context = get_branch_context_by_runtime_context_id(conn, project_id, runtime_id)
    if context is None or not context.fence_token:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if context.task_id != task:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    try:
        _require_current_fence(context, fence)
    except BranchRuntimeFenceError as exc:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown") from exc
    if context.status not in ACTIVE_MF_SUBAGENT_GRAPH_QUERY_STATES:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if not context.session_token_hash:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if mf_subagent_session_token_hash(presented_token) != context.session_token_hash:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    requested_target_root = _startup_path_text(target_project_root)
    context_target_root = _startup_path_text(
        runtime_context_effective_target_project_root(context)
    )
    if requested_target_root and context_target_root and requested_target_root != context_target_root:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    parent = str(parent_task_id or "").strip()
    if parent:
        allowed_parent_ids = {
            value
            for value in (
                context.parent_task_id,
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

    ttl = _bounded_reissue_ttl_seconds(ttl_seconds)
    now_dt = _runtime_context_now_dt(now_iso)
    expires_at = _runtime_context_iso(now_dt + timedelta(seconds=ttl))
    new_token = secrets.token_urlsafe(32)
    new_hash = mf_subagent_session_token_hash(new_token)
    lease_id = "mfrlease-" + uuid.uuid4().hex[:16]
    saved = upsert_branch_context(
        conn,
        replace(
            context,
            lease_id=lease_id,
            lease_expires_at=expires_at,
            session_token_hash=new_hash,
            last_recovery_action="mf_subagent_session_token_reissued",
        ),
        now_iso=_runtime_context_iso(now_dt),
    )
    lease = runtime_context_session_token_lease_view(
        saved,
        now_iso=_runtime_context_iso(now_dt),
    )
    return {
        "ok": True,
        "schema_version": "mf_subagent_session_token_reissue_response.v1",
        "status": "session_token_reissued",
        "project_id": saved.project_id,
        "runtime_context_id": runtime_id,
        "task_id": saved.task_id,
        "parent_task_id": _parent_task_id_for_context(saved),
        "backlog_id": saved.backlog_id,
        "worker_role": RUNTIME_CONTEXT_WORKER_ROLE,
        "worker_id": saved.worker_id,
        "worker_slot_id": saved.worker_slot_id or saved.worker_id,
        "principal_id": saved.worker_slot_id or saved.worker_id or saved.agent_id,
        "session_token": new_token,
        "session_token_hash": new_hash,
        "session_token_persisted": False,
        "raw_session_token_persisted": False,
        "ttl_seconds": ttl,
        "expires_at": expires_at,
        "session_token_lease": lease,
    }


def rejoin_mf_subagent_runtime_session_token(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    runtime_context_id: str,
    task_id: str,
    parent_task_id: str = "",
    target_project_root: str = "",
    ttl_seconds: Any = None,
    reason: str = "",
    now_iso: str = "",
    reopen_for_revision: bool = False,
) -> dict[str, Any]:
    """Issue a new host envelope for an existing worker context.

    This is narrower than changing write gates to accept refs. It is an
    observer-audited recovery path for host/session resets where the raw worker
    auth environment was lost; the raw replacement values are returned only to
    the caller and are not persisted in audit payloads.
    """

    ensure_branch_runtime_schema(conn)
    runtime_id = str(runtime_context_id or "").strip()
    task = str(task_id or "").strip()
    if not runtime_id or not task or not str(reason or "").strip():
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    context = get_branch_context_by_runtime_context_id(conn, project_id, runtime_id)
    if context is None or not context.fence_token:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if context.task_id != task:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    revision_rejoin = bool(
        reopen_for_revision and context.status in FAILED_QA_REVISION_REJOIN_STATES
    )
    if (
        context.status not in ACTIVE_MF_SUBAGENT_GRAPH_QUERY_STATES
        and not revision_rejoin
    ):
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")

    requested_target_root = _startup_path_text(target_project_root)
    context_target_root = _startup_path_text(
        runtime_context_effective_target_project_root(context)
    )
    if requested_target_root and context_target_root and requested_target_root != context_target_root:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")

    parent = str(parent_task_id or "").strip()
    if parent:
        allowed_parent_ids = {
            value
            for value in (
                context.parent_task_id,
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

    ttl = _bounded_reissue_ttl_seconds(ttl_seconds)
    now_dt = _runtime_context_now_dt(now_iso)
    expires_at = _runtime_context_iso(now_dt + timedelta(seconds=ttl))
    new_token = secrets.token_urlsafe(32)
    new_hash = mf_subagent_session_token_hash(new_token)
    lease_id = "mfrlease-" + uuid.uuid4().hex[:16]
    saved = upsert_branch_context(
        conn,
        replace(
            context,
            lease_id=lease_id,
            lease_expires_at=expires_at,
            session_token_hash=new_hash,
            status=STATE_WORKTREE_READY if revision_rejoin else context.status,
            retry_round=context.retry_round + (1 if revision_rejoin else 0),
            attempt=context.attempt + (1 if revision_rejoin else 0),
            last_recovery_action=(
                "mf_subagent_failed_qa_revision_rejoin_issued"
                if revision_rejoin
                else "mf_subagent_session_token_rejoin_issued"
            ),
        ),
        now_iso=_runtime_context_iso(now_dt),
    )
    lease = runtime_context_session_token_lease_view(
        saved,
        now_iso=_runtime_context_iso(now_dt),
    )
    fence_hash = runtime_context_secret_hash(saved.fence_token)
    return {
        "ok": True,
        "schema_version": "mf_subagent_session_token_rejoin_response.v1",
        "status": "session_token_rejoin_issued",
        "project_id": saved.project_id,
        "runtime_context_id": runtime_id,
        "task_id": saved.task_id,
        "parent_task_id": _parent_task_id_for_context(saved),
        "backlog_id": saved.backlog_id,
        "worker_role": RUNTIME_CONTEXT_WORKER_ROLE,
        "worker_id": saved.worker_id,
        "worker_slot_id": saved.worker_slot_id or saved.worker_id,
        "principal_id": saved.worker_slot_id or saved.worker_id or saved.agent_id,
        "reopen_for_revision": revision_rejoin,
        "previous_status": context.status,
        "current_status": saved.status,
        "attempt": saved.attempt,
        "retry_round": saved.retry_round,
        "session_token": new_token,
        "session_token_hash": new_hash,
        "session_token_ref": runtime_context_session_token_ref(saved),
        "session_token_persisted": False,
        "raw_session_token_persisted": False,
        "fence_token": saved.fence_token,
        "fence_token_hash": fence_hash,
        "raw_fence_token_returned_for_host_envelope": True,
        "raw_fence_token_persisted_to_timeline": False,
        "delivery": "worker_host_envelope",
        "ttl_seconds": ttl,
        "expires_at": expires_at,
        "session_token_lease": lease,
    }


def initial_join_mf_subagent_runtime_session_token(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    runtime_context_id: str,
    task_id: str,
    parent_task_id: str = "",
    target_project_root: str = "",
    ttl_seconds: Any = None,
    reason: str = "",
    now_iso: str = "",
) -> dict[str, Any]:
    """Issue the first host envelope for a freshly allocated mf_sub context.

    This is the pre-lineage companion to rejoin: it lets a host adapter inject
    scoped worker auth into a newly spawned worker without putting raw tokens in
    prompts or timeline rows.
    """

    ensure_branch_runtime_schema(conn)
    runtime_id = str(runtime_context_id or "").strip()
    task = str(task_id or "").strip()
    if not runtime_id or not task or not str(reason or "").strip():
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    context = get_branch_context_by_runtime_context_id(conn, project_id, runtime_id)
    if context is None or not context.fence_token:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if context.task_id != task:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if context.status not in ACTIVE_MF_SUBAGENT_GRAPH_QUERY_STATES:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")

    requested_target_root = _startup_path_text(target_project_root)
    context_target_root = _startup_path_text(
        runtime_context_effective_target_project_root(context)
    )
    if requested_target_root and context_target_root and requested_target_root != context_target_root:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")

    parent = str(parent_task_id or "").strip()
    if parent:
        allowed_parent_ids = {
            value
            for value in (
                context.parent_task_id,
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

    ttl = _bounded_reissue_ttl_seconds(ttl_seconds)
    now_dt = _runtime_context_now_dt(now_iso)
    expires_at = _runtime_context_iso(now_dt + timedelta(seconds=ttl))
    new_token = secrets.token_urlsafe(32)
    new_hash = mf_subagent_session_token_hash(new_token)
    lease_id = "mfrlease-" + uuid.uuid4().hex[:16]
    saved = upsert_branch_context(
        conn,
        replace(
            context,
            lease_id=lease_id,
            lease_expires_at=expires_at,
            session_token_hash=new_hash,
            last_recovery_action="mf_subagent_initial_join_issued",
        ),
        now_iso=_runtime_context_iso(now_dt),
    )
    lease = runtime_context_session_token_lease_view(
        saved,
        now_iso=_runtime_context_iso(now_dt),
    )
    fence_hash = runtime_context_secret_hash(saved.fence_token)
    host_envelope = {
        "schema_version": "mf_subagent_initial_join_host_envelope.v1",
        "runtime_context_id": runtime_id,
        "task_id": saved.task_id,
        "parent_task_id": _parent_task_id_for_context(saved),
        "target_project_root": runtime_context_effective_target_project_root(saved),
        "worker_role": RUNTIME_CONTEXT_WORKER_ROLE,
        "session_token_env": "AMING_WORKER_SESSION_TOKEN",
        "fence_token_env": "AMING_WORKER_FENCE_TOKEN",
        "env": {
            "AMING_WORKER_SESSION_TOKEN": new_token,
            "AMING_WORKER_FENCE_TOKEN": saved.fence_token,
        },
        "raw_tokens_persisted_to_timeline": False,
    }
    return {
        "ok": True,
        "schema_version": "mf_subagent_initial_join_response.v1",
        "status": "session_token_initial_join_issued",
        "project_id": saved.project_id,
        "runtime_context_id": runtime_id,
        "task_id": saved.task_id,
        "parent_task_id": _parent_task_id_for_context(saved),
        "backlog_id": saved.backlog_id,
        "worker_role": RUNTIME_CONTEXT_WORKER_ROLE,
        "worker_id": saved.worker_id,
        "worker_slot_id": saved.worker_slot_id or saved.worker_id,
        "principal_id": saved.worker_slot_id or saved.worker_id or saved.agent_id,
        "session_token": new_token,
        "session_token_hash": new_hash,
        "session_token_ref": runtime_context_session_token_ref(saved),
        "session_token_persisted": False,
        "raw_session_token_persisted": False,
        "fence_token": saved.fence_token,
        "fence_token_hash": fence_hash,
        "raw_fence_token_returned_for_host_envelope": True,
        "raw_fence_token_persisted_to_timeline": False,
        "delivery": "worker_host_envelope",
        "host_envelope": host_envelope,
        "ttl_seconds": ttl,
        "expires_at": expires_at,
        "session_token_lease": lease,
    }


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
    return _runtime_context_parent_task_id(context)


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
    explicit_revision_id = bool(str(revision_id or "").strip())
    revision = str(revision_id or "").strip()
    public_payload = public_contract_revision_payload(payload or {})
    public_route_identity = public_contract_revision_route_identity(route_gate, route_identity)
    public_route_gate = public_contract_revision_payload(route_gate or {})
    previous_revision = get_latest_branch_contract_revision(
        conn,
        context.project_id,
        runtime_context_id,
    )
    previous_revision_hash = _previous_revision_hash(previous_revision)
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
    receipt_material = _contract_revision_receipt_material(
        context=context,
        runtime_context_id=runtime_context_id,
        revision_id=revision,
        explicit_revision_id=explicit_revision_id,
        contract_version=contract_version,
        payload=public_payload,
        route_identity=public_route_identity,
        previous_revision_hash=previous_revision_hash,
    )
    canonical_visible_contract_text_hash = _canonical_contract_hash(receipt_material)
    if not revision:
        revision = canonical_visible_contract_text_hash
    public_payload["source_of_truth"] = "Contract/Revision/Event"
    public_payload["revision_receipt"] = {
        "schema_version": "agent_task_contract_revision_receipt.v1",
        "source_of_truth": "Contract/Revision/Event",
        "canonical_visible_contract_text_hash": canonical_visible_contract_text_hash,
        "previous_revision_hash": previous_revision_hash,
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
        backlog_id=row["backlog_id"] or "",
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
            project_id, merge_queue_id, queue_item_id, backlog_id, task_id, branch_ref,
            queue_index, status, depends_on_json, hard_depends_on_json,
            serializes_after_json, conflicts_with_json,
            same_node_or_file_conflicts_json, requires_graph_epoch_json,
            target_ref, base_commit, branch_head, validated_target_head,
            current_target_head, validation_attempt, merge_preview_id,
            snapshot_id, projection_id, merge_commit, target_head_before_merge,
            target_head_after_merge, completed_at, failure_reason, created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(project_id, merge_queue_id, queue_item_id) DO UPDATE SET
            backlog_id = excluded.backlog_id,
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
            item.backlog_id,
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


def _list_merge_queue_items_with_target_fallback(
    conn: sqlite3.Connection,
    project_id: str,
    merge_queue_id: str,
    *,
    target_ref: str = "",
) -> list[MergeQueueItem]:
    items = list_merge_queue_items(
        conn,
        project_id,
        merge_queue_id,
        target_ref=target_ref,
    )
    if not items and str(target_ref or "").strip():
        items = list_merge_queue_items(
            conn,
            project_id,
            merge_queue_id,
            target_ref="",
        )
    return items


def _context_enriched_merge_queue_item(
    conn: sqlite3.Connection,
    item: MergeQueueItem,
    *,
    explicit_branch_ref: str = "",
    target_ref: str = "",
) -> MergeQueueItem:
    context = get_branch_context(conn, item.project_id, item.task_id)
    if context is None and not explicit_branch_ref and not target_ref:
        return item
    branch_ref = str(explicit_branch_ref or item.branch_ref or "").strip()
    if context is not None:
        branch_ref = branch_ref or context.branch_ref
    if context is None:
        return replace(
            item,
            branch_ref=branch_ref,
            target_ref=str(target_ref or item.target_ref or "").strip(),
        )
    return replace(
        item,
        backlog_id=item.backlog_id or context.backlog_id,
        branch_ref=branch_ref,
        target_ref=str(target_ref or item.target_ref or context.ref_name or "").strip(),
        base_commit=item.base_commit or context.base_commit,
        branch_head=item.branch_head or context.head_commit,
        current_target_head=item.current_target_head or context.target_head_commit,
        snapshot_id=item.snapshot_id or context.snapshot_id,
        projection_id=item.projection_id or context.projection_id,
        merge_preview_id=item.merge_preview_id or context.merge_preview_id,
    )


def _context_enriched_merge_queue_items(
    conn: sqlite3.Connection,
    items: list[MergeQueueItem],
    *,
    target_ref: str = "",
) -> list[MergeQueueItem]:
    return [
        _context_enriched_merge_queue_item(conn, item, target_ref=target_ref)
        for item in items
    ]


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
    items = _context_enriched_merge_queue_items(
        conn,
        _list_merge_queue_items_with_target_fallback(
            conn,
            project_id,
            merge_queue_id,
            target_ref=target_ref,
        ),
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
        _context_enriched_merge_queue_items(
            conn,
            _list_merge_queue_items_with_target_fallback(
                conn,
                project_id,
                merge_queue_id,
                target_ref=target_ref,
            ),
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
    allow_route_gated_reclaimed_fence_without_token: bool = False,
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

    items = _context_enriched_merge_queue_items(
        conn,
        _list_merge_queue_items_with_target_fallback(
            conn,
            project_id,
            merge_queue_id,
            target_ref=target_ref,
        ),
        target_ref=target_ref,
    )
    selected = _select_merge_gate_item(
        items,
        queue_item_id=queue_item_id,
        task_id=task_id,
    )
    context = get_branch_context(conn, project_id, selected.task_id)
    route_gated_reclaimed_fence = (
        bool(allow_route_gated_reclaimed_fence_without_token)
        and result_status == STATE_MERGED
        and not str(fence_token or "").strip()
        and bool(str(merge_commit or "").strip())
        and bool(str(target_head_before_merge or "").strip())
        and bool(str(target_head_after_merge or "").strip())
    )
    if context is not None and (context.fence_token or fence_token):
        if fence_token or not route_gated_reclaimed_fence:
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


_FINISH_CHECKPOINT_MERGE_QUEUE_STATUSES = frozenset(
    {
        STATE_QUEUED_FOR_MERGE,
        STATE_VALIDATED,
        STATE_MERGE_READY,
        "review_ready",
        "waiting_merge",
    }
)


def _finish_checkpoint_route_gate_allows_merge_queue_without_fence(
    context: BranchTaskRuntimeContext,
    *,
    fence_token: str,
    checkpoint_id: str,
    require_finish_gate: bool,
    allow_finish_checkpoint_without_fence: bool,
) -> bool:
    if not allow_finish_checkpoint_without_fence:
        return False
    if fence_token:
        return False
    if not require_finish_gate:
        return False
    expected_checkpoint = str(checkpoint_id or "").strip()
    if not expected_checkpoint:
        raise ValueError("checkpoint_id is required when require_finish_gate is true")
    if context.checkpoint_id != expected_checkpoint:
        raise ValueError("checkpoint_id does not match the validated finish gate")
    if context.replay_source != "mf_sub_finish_gate":
        raise ValueError("validated mf_sub finish gate checkpoint is required")
    context_status = _normalize_merge_queue_status(context.status)
    if context_status not in _FINISH_CHECKPOINT_MERGE_QUEUE_STATUSES:
        raise ValueError("branch context is not merge-ready from a finish gate")
    return True


MF_SUBAGENT_ROUTE_IDENTITY_FIELDS = (
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "prompt_contract_hash",
    "route_token_ref",
    "visible_injection_manifest_hash",
)


def _graph_query_fence_error(
    reason: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> BranchRuntimeFenceError:
    exc = BranchRuntimeFenceError(reason)
    exc.details = dict(details or {})  # type: ignore[attr-defined]
    return exc


def validate_mf_subagent_graph_query_identity(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    task_id: str,
    fence_token: str,
    runtime_context_id: str = "",
    parent_task_id: str = "",
    worker_role: str = "",
    governance_project_id: str = "",
    target_project_id: str = "",
    target_project_root: str = "",
    session_token: str = "",
    session_token_ref: str = "",
    route_identity: Mapping[str, Any] | None = None,
) -> BranchTaskRuntimeContext:
    """Validate the runtime/fence identity an mf_sub worker presents for graph reads."""
    ensure_branch_runtime_schema(conn)
    task = str(task_id or "").strip()
    runtime_id = str(runtime_context_id or "").strip()
    fence = str(fence_token or "").strip()
    role = str(worker_role or "").strip().lower().replace("-", "_")
    if runtime_id and not role:
        role = "mf_sub"
    if role != "mf_sub":
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if not fence or (not task and not runtime_id):
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")

    query_project_id = str(project_id or "").strip()
    context_project_id = str(governance_project_id or query_project_id).strip()
    if runtime_id:
        context = get_branch_context_by_runtime_context_id(
            conn,
            context_project_id,
            runtime_id,
        )
    else:
        context = get_branch_context(conn, context_project_id, task)
    if context is None or not context.fence_token:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if runtime_id and task and task != context.task_id:
        raise _graph_query_fence_error(
            "runtime_context_task_mismatch",
            details={
                "runtime_context_id": runtime_id,
                "task_id": task,
                "expected_task_id": context.task_id,
            },
        )
    try:
        _require_current_fence(context, fence)
    except BranchRuntimeFenceError as exc:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown") from exc

    if context.status not in ACTIVE_MF_SUBAGENT_GRAPH_QUERY_STATES:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    supplied_token_hash = ""
    token_matches_context = False
    if context.session_token_hash:
        supplied_token_hash = mf_subagent_session_token_hash(session_token)
        token_matches_context = bool(
            (
                supplied_token_hash
                and supplied_token_hash == context.session_token_hash
            )
            or runtime_context_session_token_ref_matches(context, session_token_ref)
        )
        if not token_matches_context:
            latest_revision = get_latest_branch_contract_revision(
                conn,
                context.project_id,
                runtime_context_id_for_branch_context(context),
            )
            registered_identity = _startup_registered_host_adapter_identity(
                context=context,
                latest_revision=latest_revision,
            )
            if not _startup_registered_host_adapter_identity_matches_context(
                registered_identity,
                context,
            ):
                raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if context.lease_expires_at and context.lease_expires_at < utc_now():
        if context.session_token_hash and token_matches_context:
            raise _runtime_session_token_expired_error(
                context,
                runtime_context_id=runtime_id,
            )
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")

    requested_target_project_id = str(target_project_id or query_project_id).strip()
    context_governance_project_id = context.governance_project_id or context.project_id
    context_target_project_id = context.target_project_id or context.project_id
    if context_project_id != context.project_id:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if governance_project_id and governance_project_id != context_governance_project_id:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if query_project_id not in {context.project_id, context_target_project_id}:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if requested_target_project_id != context_target_project_id:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    requested_target_root = _startup_path_text(target_project_root)
    context_target_root = _startup_path_text(
        runtime_context_effective_target_project_root(context)
    )
    if requested_target_root and context_target_root and requested_target_root != context_target_root:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")

    parent = str(parent_task_id or "").strip()
    if parent:
        allowed_parent_ids = {
            value
            for value in (
                context.parent_task_id,
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
    if runtime_id:
        latest_revision = get_latest_branch_contract_revision(
            conn,
            context.project_id,
            runtime_context_id_for_branch_context(context),
        )
        expected_route_identity = (
            latest_revision.route_identity
            if latest_revision is not None
            and isinstance(latest_revision.route_identity, Mapping)
            else {}
        )
        supplied_route_identity = {
            field: str((route_identity or {}).get(field) or "").strip()
            for field in MF_SUBAGENT_ROUTE_IDENTITY_FIELDS
        }
        missing_route_identity = [
            field
            for field in MF_SUBAGENT_ROUTE_IDENTITY_FIELDS
            if str(expected_route_identity.get(field) or "").strip()
            and not supplied_route_identity.get(field)
        ]
        if missing_route_identity:
            raise _graph_query_fence_error(
                "route_identity_missing",
                details={
                    "runtime_context_id": runtime_id,
                    "missing_route_identity_fields": missing_route_identity,
                    "latest_contract_revision_id": latest_revision.revision_id
                    if latest_revision is not None
                    else "",
                },
            )
        route_mismatches = _startup_route_identity_mismatches(
            expected=expected_route_identity,
            actual=supplied_route_identity,
        )
        if route_mismatches:
            raise _graph_query_fence_error(
                "route_identity_mismatch",
                details={
                    "runtime_context_id": runtime_id,
                    "route_identity_mismatches": route_mismatches,
                    "latest_contract_revision_id": latest_revision.revision_id
                    if latest_revision is not None
                    else "",
                },
            )
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


def _startup_identity_text(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _startup_registered_identity_values(
    registered_identity: Mapping[str, Any] | None,
    keys: tuple[str, ...],
) -> tuple[str, ...]:
    if not isinstance(registered_identity, Mapping):
        return ()
    values: list[str] = []
    for key in keys:
        value = str(registered_identity.get(key) or "").strip()
        if value:
            values.append(_startup_identity_text(value))
    return tuple(value for value in values if value)


def _startup_registered_host_startup_texts(
    registered_identity: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    return _startup_registered_identity_values(
        registered_identity,
        ("host_startup_id", "host_session_id"),
    )


def _startup_registered_session_texts(
    registered_identity: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    return _startup_registered_identity_values(
        registered_identity,
        (
            "session_token_surrogate",
            "session_surrogate",
            "startup_token_surrogate",
            "host_session_id",
        ),
    )


def _startup_registered_identity_texts(
    registered_identity: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    return (
        _startup_registered_host_startup_texts(registered_identity)
        + _startup_registered_session_texts(registered_identity)
    )


def _startup_registered_identity_is_runtime_text_prepare(
    registered_identity: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(registered_identity, Mapping):
        return False
    source = _startup_identity_text(str(registered_identity.get("source") or ""))
    registration_source = _startup_identity_text(
        str(registered_identity.get("registration_source") or "")
    )
    return (
        source == "observer_runtime_text_prepare"
        or registration_source == "runtime_text_prepare"
    )


def _startup_registered_identity_uses_placeholder_agent(
    registered_identity: Mapping[str, Any] | None,
) -> bool:
    agent_values = _startup_registered_identity_values(
        registered_identity,
        ("agent_id", "actual_host_worker_id"),
    )
    return bool(agent_values) and all(
        value.startswith("host_adapter_agent:") for value in agent_values
    )


def _startup_registered_identity_allows_late_host_agent(
    registered_identity: Mapping[str, Any] | None,
) -> bool:
    return (
        _startup_registered_identity_is_runtime_text_prepare(registered_identity)
        and _startup_registered_identity_uses_placeholder_agent(registered_identity)
    )


def _startup_registered_host_adapter_identity_matches_context(
    registered_identity: Mapping[str, Any] | None,
    context: BranchTaskRuntimeContext,
) -> bool:
    if not _startup_registered_identity_is_runtime_text_prepare(registered_identity):
        return False
    if not _startup_registered_identity_texts(registered_identity):
        return False
    expected = {
        "project_id": context.project_id,
        "runtime_context_id": runtime_context_id_for_branch_context(context),
        "task_id": context.task_id,
        "worker_slot_id": context.worker_slot_id or context.worker_id,
    }
    for field, expected_value in expected.items():
        actual = str((registered_identity or {}).get(field) or "").strip()
        if actual and expected_value and actual != expected_value:
            return False
    return True


def _startup_public_registered_host_adapter_identity(
    value: Any,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed_keys = (
        "schema_version",
        "source",
        "registration_source",
        "startup_source",
        "runtime_context_id",
        "observer_command_id",
        "launch_text_hash",
        "project_id",
        "task_id",
        "worker_slot_id",
        "agent_id",
        "actual_host_worker_id",
        "host_startup_id",
        "host_session_id",
        "session_token_surrogate",
        "session_surrogate",
        "startup_token_surrogate",
    )
    identity = {
        key: str(value.get(key) or "").strip()
        for key in allowed_keys
        if str(value.get(key) or "").strip()
    }
    if not _startup_registered_identity_texts(identity):
        return {}
    identity.setdefault("schema_version", MF_SUBAGENT_HOST_ADAPTER_IDENTITY_SCHEMA_VERSION)
    identity.setdefault("source", "runtime_contract_revision")
    return identity


def _startup_payload_registered_host_adapter_identity(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    sources: list[Any] = [payload.get("registered_host_adapter_spawn")]
    host_adapter_startup = payload.get("host_adapter_surrogate_startup")
    if isinstance(host_adapter_startup, Mapping):
        sources.append(host_adapter_startup.get("registered_host_adapter_spawn"))
    startup_alternatives = payload.get("startup_alternatives")
    if isinstance(startup_alternatives, Mapping):
        nested_host_adapter = startup_alternatives.get("host_adapter_surrogate_startup")
        if isinstance(nested_host_adapter, Mapping):
            sources.append(nested_host_adapter.get("registered_host_adapter_spawn"))
    canonical_fields = payload.get("canonical_startup_identity_fields")
    if isinstance(canonical_fields, Mapping):
        sources.append(canonical_fields.get("registered_host_adapter_spawn"))
    for source in sources:
        identity = _startup_public_registered_host_adapter_identity(source)
        if identity:
            return identity
    return {}


def _startup_registered_payload_identity_matches(
    *,
    payload_identity: Mapping[str, Any] | None,
    registered_identity: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(payload_identity, Mapping) or not payload_identity:
        return False
    if not isinstance(registered_identity, Mapping) or not registered_identity:
        return False
    payload_texts = set(_startup_registered_identity_texts(payload_identity))
    registered_texts = set(_startup_registered_identity_texts(registered_identity))
    if not payload_texts.intersection(registered_texts):
        return False
    for field in (
        "project_id",
        "runtime_context_id",
        "observer_command_id",
        "launch_text_hash",
        "task_id",
        "worker_slot_id",
    ):
        payload_value = str(payload_identity.get(field) or "").strip()
        registered_value = str(registered_identity.get(field) or "").strip()
        if payload_value and registered_value and payload_value != registered_value:
            return False
    return True


def _startup_registered_host_adapter_identity(
    *,
    context: BranchTaskRuntimeContext,
    latest_revision: BranchRuntimeContractRevision | None,
) -> dict[str, Any]:
    if latest_revision is not None and isinstance(latest_revision.payload, Mapping):
        for key in (
            "host_adapter_spawn_identity",
            "registered_host_adapter_spawn",
            "host_adapter_startup_identity",
        ):
            identity = _startup_public_registered_host_adapter_identity(
                latest_revision.payload.get(key)
            )
            if identity:
                identity.setdefault("revision_id", latest_revision.revision_id)
                return identity

    # Allocation-time host identity is accepted only when it predates startup.
    # Observed startup fields written by mf_subagent.startup must not become a
    # new self-asserted exemption for a later mismatched agent.
    if (
        context.host_startup_id
        and context.last_recovery_action != "mf_subagent_startup_recorded"
    ):
        return _startup_public_registered_host_adapter_identity(
            {
                "schema_version": MF_SUBAGENT_HOST_ADAPTER_IDENTITY_SCHEMA_VERSION,
                "source": "branch_runtime_allocation",
                "runtime_context_id": runtime_context_id_for_branch_context(context),
                "host_startup_id": context.host_startup_id,
                "host_session_id": context.host_session_id,
            }
        )
    return {}


def _startup_service_dispatch_worker_binding(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    context: BranchTaskRuntimeContext,
    payload: Mapping[str, Any],
    route_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve a real spawned worker id from observer service-dispatch evidence.

    Branch runtime allocation often happens before the host returns the concrete
    multi-agent/CLI worker id.  This binding lets startup join that real worker
    to the preallocated runtime context, but only when an accepted observer
    service-dispatch event names the same runtime context, task, worker slot,
    command lineage, and route identity.
    """

    try:
        from . import task_timeline

        events = task_timeline.list_events(
            conn,
            project_id,
            backlog_id=context.backlog_id,
            event_kind="observer_subagent_service_dispatch",
            limit=200,
        )
    except Exception:
        events = []
    if not events:
        return {}

    expected_runtime_context_id = runtime_context_id_for_branch_context(context)
    expected_task_id = str(context.task_id or "")
    expected_worker_slot_id = str(context.worker_slot_id or context.worker_id or "")
    expected_observer_command_id = str(payload.get("observer_command_id") or "").strip()
    candidate_ids = _runtime_context_dedupe(
        [
            str(payload.get("agent_id") or "").strip(),
            str(payload.get("actual_host_worker_id") or "").strip(),
            str(payload.get("host_worker_id") or "").strip(),
            str(payload.get("worker_session_id") or "").strip(),
            str(payload.get("worker_transcript_ref") or "").strip().split(":", 1)[-1],
            str(payload.get("transcript_ref") or "").strip().split(":", 1)[-1],
        ]
    )

    route_fields = (
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "route_token_ref",
        "visible_injection_manifest_hash",
    )

    def _accepted(event: Mapping[str, Any]) -> bool:
        status = str(event.get("status") or "").strip().lower()
        return status in {"accepted", "ok", "passed", "succeeded"}

    def _route_matches(
        source: Mapping[str, Any],
        *,
        require_expected_fields: bool = False,
    ) -> bool:
        for field in route_fields:
            expected = str(route_identity.get(field) or "").strip()
            actual = str(source.get(field) or "").strip()
            if expected:
                if require_expected_fields and not actual:
                    return False
                if actual and actual != expected:
                    return False
        return True

    for event in reversed(events):
        if not _accepted(event):
            continue
        event_payload = (
            event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        )
        if not _route_matches(event_payload, require_expected_fields=True):
            continue
        event_command_id = str(event_payload.get("observer_command_id") or "").strip()
        if (
            expected_observer_command_id
            and event_command_id
            and event_command_id != expected_observer_command_id
        ):
            continue
        workers = event_payload.get("workers")
        if not isinstance(workers, list):
            workers = event_payload.get("dispatches")
        if not isinstance(workers, list):
            continue
        for worker in workers:
            if not isinstance(worker, Mapping):
                continue
            if str(worker.get("runtime_context_id") or "").strip() != expected_runtime_context_id:
                continue
            if str(worker.get("task_id") or "").strip() != expected_task_id:
                continue
            worker_slot_id = str(
                worker.get("worker_slot_id") or worker.get("worker_id") or ""
            ).strip()
            if expected_worker_slot_id and worker_slot_id != expected_worker_slot_id:
                continue
            if not _route_matches(worker):
                continue
            worker_command_id = str(worker.get("observer_command_id") or "").strip()
            if (
                expected_observer_command_id
                and worker_command_id
                and worker_command_id != expected_observer_command_id
            ):
                continue
            transcript_ref = str(
                worker.get("transcript_ref")
                or worker.get("worker_transcript_ref")
                or ""
            ).strip()
            worker_ids = _runtime_context_dedupe(
                [
                    str(worker.get("agent_id") or "").strip(),
                    str(worker.get("actual_host_worker_id") or "").strip(),
                    str(worker.get("worker_session_id") or "").strip(),
                    transcript_ref.split(":", 1)[-1] if transcript_ref else "",
                ]
            )
            if candidate_ids and not set(candidate_ids).intersection(worker_ids):
                continue
            if not worker_ids:
                continue
            return {
                "schema_version": "mf_subagent_service_dispatch_worker_binding.v1",
                "source": "observer_subagent_service_dispatch",
                "event_id": str(event.get("id") or ""),
                "event_ref": f"timeline:{event.get('id') or ''}",
                "runtime_context_id": expected_runtime_context_id,
                "task_id": expected_task_id,
                "worker_slot_id": worker_slot_id,
                "agent_id": worker_ids[0],
                "actual_host_worker_id": str(
                    worker.get("actual_host_worker_id")
                    or worker.get("agent_id")
                    or worker_ids[0]
                    or ""
                ).strip(),
                "worker_session_id": str(
                    worker.get("worker_session_id") or worker_ids[0] or ""
                ).strip(),
                "transcript_ref": transcript_ref,
                "monitor_ref": str(worker.get("monitor_ref") or "").strip(),
                "dispatch_command_ref": str(
                    event_payload.get("dispatch_command_ref")
                    or worker.get("dispatch_command_ref")
                    or ""
                ).strip(),
                "observer_command_id": event_command_id or expected_observer_command_id,
                "session_token_ref": str(worker.get("session_token_ref") or "").strip(),
            }
    return {}


def build_registered_host_adapter_spawn_identity(
    *,
    project_id: str,
    runtime_context_id: str,
    observer_command_id: str,
    launch_text_hash: str,
    backend_mode: str = "",
    startup_source: str = "",
    task_id: str = "",
    worker_slot_id: str = "",
    agent_id: str = "",
    actual_host_worker_id: str = "",
    host_startup_id: str = "",
    host_session_id: str = "",
    session_token_surrogate: str = "",
) -> dict[str, Any]:
    """Build a server-registered host-adapter spawn identity.

    The identity is public audit material: it binds the startup surrogate to the
    prepared runtime context and launch text hash without persisting raw launch
    text or session tokens.
    """

    backend = _startup_identity_text(
        backend_mode or startup_source or "host_adapter"
    ).replace(".", "_")
    backend = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in backend)
    backend = backend.strip("_") or "host_adapter"
    seed = "|".join(
        str(value or "").strip()
        for value in (
            project_id,
            runtime_context_id,
            observer_command_id,
            launch_text_hash,
            backend,
            task_id,
            worker_slot_id,
        )
    )
    suffix = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    registered_host_startup_id = (
        str(host_startup_id or "").strip()
        or f"host_adapter:{backend}:{suffix}"
    )
    registered_session_surrogate = (
        str(session_token_surrogate or "").strip()
        or f"host-adapter:{suffix}"
    )
    registered_agent_id = str(agent_id or "").strip() or (
        f"host_adapter_agent:{backend}:{suffix}"
    )
    registered_actual_host_worker_id = (
        str(actual_host_worker_id or "").strip() or registered_agent_id
    )
    return {
        "schema_version": MF_SUBAGENT_HOST_ADAPTER_IDENTITY_SCHEMA_VERSION,
        "source": "observer_runtime_text_prepare",
        "registration_source": "runtime_text_prepare",
        "startup_source": startup_source or f"{backend}_host_adapter",
        "project_id": project_id,
        "runtime_context_id": runtime_context_id,
        "observer_command_id": observer_command_id,
        "launch_text_hash": launch_text_hash,
        "task_id": task_id,
        "worker_slot_id": worker_slot_id,
        "agent_id": registered_agent_id,
        "actual_host_worker_id": registered_actual_host_worker_id,
        "host_startup_id": registered_host_startup_id,
        "host_session_id": str(host_session_id or "").strip()
        or registered_host_startup_id,
        "session_token_surrogate": registered_session_surrogate,
    }


def _startup_host_adapter_identity(
    *,
    startup_source: str,
    session_token_surrogate: str,
    host_startup_id: str,
    payload: Mapping[str, Any],
    registered_identity: Mapping[str, Any] | None,
) -> bool:
    _ = startup_source
    identity_present = bool(session_token_surrogate or host_startup_id)
    if not identity_present:
        return False
    registered_host_startup_values = _startup_registered_host_startup_texts(
        registered_identity
    )
    registered_session_values = _startup_registered_session_texts(
        registered_identity
    )
    host_startup_match = bool(
        host_startup_id
        and _startup_identity_text(host_startup_id) in registered_host_startup_values
    )
    session_surrogate_match = bool(
        session_token_surrogate
        and _startup_identity_text(session_token_surrogate) in registered_session_values
    )
    if not (host_startup_match or session_surrogate_match):
        return False

    registered_agent_values = _startup_registered_identity_values(
        registered_identity,
        ("agent_id", "actual_host_worker_id"),
    )
    supplied_agent_id = str(payload.get("agent_id") or "").strip()
    if not registered_agent_values or not supplied_agent_id:
        return False
    if (
        _startup_identity_text(supplied_agent_id) not in registered_agent_values
        and not _startup_registered_identity_allows_late_host_agent(registered_identity)
    ):
        return False

    registered_runtime_context_id = str(
        (registered_identity or {}).get("runtime_context_id") or ""
    ).strip()
    supplied_runtime_context_id = str(payload.get("runtime_context_id") or "").strip()
    if (
        registered_runtime_context_id
        and supplied_runtime_context_id
        and registered_runtime_context_id != supplied_runtime_context_id
    ):
        return False

    registered_launch_text_hash = str(
        (registered_identity or {}).get("launch_text_hash") or ""
    ).strip()
    supplied_launch_text_hash = str(payload.get("launch_text_hash") or "").strip()
    if (
        registered_launch_text_hash
        and supplied_launch_text_hash
        and registered_launch_text_hash != supplied_launch_text_hash
    ):
        return False
    for registered_key, payload_key in (
        ("observer_command_id", "observer_command_id"),
        ("task_id", "task_id"),
        ("worker_slot_id", "worker_slot_id"),
    ):
        registered_value = str(
            (registered_identity or {}).get(registered_key) or ""
        ).strip()
        supplied_value = str(payload.get(payload_key) or "").strip()
        if registered_value and supplied_value and registered_value != supplied_value:
            return False
    return True


def _startup_token_evidence(
    payload: Mapping[str, Any],
    *,
    stored_token_hash: str = "",
    expected_session_token_ref: str = "",
) -> dict[str, Any]:
    """Compute session-token evidence for a startup.

    ``session_token_evidence_type`` semantics:
    - ``'server_verified'``: the worker presented a raw session_token and the
      server verified its hash against ``stored_token_hash`` recorded at first
      sight.  This is the only evidence type that exempts a startup from
      surrogate classification in the finish gate.
    - ``'hash'``: the worker presented a raw session_token and this is the
      FIRST startup for the lane (no prior stored hash exists).  The server
      records the hash now (first-sight commitment).  Also exempts from
      surrogate classification because the server itself is committing the hash.
    - ``'claimed_unverified'``: the worker presented a raw session_token but the
      server already holds a DIFFERENT hash for this lane — the presented token
      does not match.  The startup is treated as unverified and the finish gate
      MUST classify it as a surrogate.  Existing refusal paths are not weakened.
    - ``'server_verified_ref'``: the worker presented the copy-safe
      runtime_context session_token_ref and the server verified it against the
      stored token hash/lineage.  This is equivalent to ``server_verified`` for
      close gates while avoiding raw token persistence.
    - ``'surrogate'``: no session_token/session_token_ref was presented; a
      surrogate string was supplied instead.  Always classified as surrogate in
      the finish gate.
    - ``''`` (empty): no token and no surrogate.

    Backward compatibility: existing recorded startup events with
    ``session_token_evidence_type='hash'`` are NOT retroactively invalidated;
    the gate-behavior change (downgrading unverified claims) applies only to NEW
    startups processed after this fix lands.
    """
    session_token = str(payload.get("session_token") or "").strip()
    if session_token.startswith("<") and session_token.endswith(">"):
        session_token = ""
    session_token_ref = str(
        payload.get("session_token_ref")
        or payload.get("worker_session_token_ref")
        or ""
    ).strip()
    surrogate = str(
        payload.get("session_token_surrogate")
        or payload.get("session_surrogate")
        or ""
    ).strip()
    if session_token:
        computed_hash = mf_subagent_session_token_hash(session_token)
        stored = str(stored_token_hash or "").strip()
        if stored:
            # Server already holds the expected hash — verify presented token.
            if computed_hash == stored:
                evidence_type = "server_verified"
            else:
                # Mismatch: presented token does not match allocation-time hash.
                evidence_type = "claimed_unverified"
        else:
            # First startup for this lane: server commits the hash now (first-sight).
            evidence_type = "hash"
        return {
            "session_token_hash": computed_hash,
            "session_token_ref": "",
            "session_token_ref_present": False,
            "session_token_present": True,
            "session_token_persisted": False,
            "session_token_surrogate": surrogate,
            "session_token_evidence_type": evidence_type,
        }
    if session_token_ref:
        expected_ref = str(expected_session_token_ref or "").strip()
        if expected_ref and secrets.compare_digest(session_token_ref, expected_ref):
            return {
                "session_token_hash": str(stored_token_hash or "").strip(),
                "session_token_ref": session_token_ref,
                "session_token_ref_present": True,
                "session_token_present": False,
                "session_token_persisted": False,
                "session_token_surrogate": surrogate,
                "session_token_evidence_type": "server_verified_ref",
            }
        return {
            "session_token_hash": "",
            "session_token_ref": session_token_ref,
            "session_token_ref_present": True,
            "session_token_present": False,
            "session_token_persisted": False,
            "session_token_surrogate": surrogate,
            "session_token_evidence_type": "claimed_unverified_ref",
        }
    if surrogate:
        return {
            "session_token_hash": "",
            "session_token_ref": "",
            "session_token_ref_present": False,
            "session_token_present": False,
            "session_token_persisted": False,
            "session_token_surrogate": surrogate,
            "session_token_evidence_type": "surrogate",
        }
    return {
        "session_token_hash": "",
        "session_token_ref": "",
        "session_token_ref_present": False,
        "session_token_present": False,
        "session_token_persisted": False,
        "session_token_surrogate": "",
        "session_token_evidence_type": "",
    }


def _startup_identity_field_report(
    *,
    payload: Mapping[str, Any],
    token_evidence: Mapping[str, Any] | None,
    registered_identity: Mapping[str, Any] | None,
) -> dict[str, Any]:
    token_evidence = token_evidence if isinstance(token_evidence, Mapping) else {}
    registered_public = _startup_public_registered_host_adapter_identity(
        registered_identity or {}
    )
    payload_registered = _startup_payload_registered_host_adapter_identity(payload)
    session_token_present = bool(str(payload.get("session_token") or "").strip())
    session_token_surrogate = str(
        token_evidence.get("session_token_surrogate")
        or payload.get("session_token_surrogate")
        or payload.get("session_surrogate")
        or payload_registered.get("session_token_surrogate")
        or registered_public.get("session_token_surrogate")
        or ""
    ).strip()
    host_startup_id = str(
        payload.get("host_startup_id")
        or payload_registered.get("host_startup_id")
        or registered_public.get("host_startup_id")
        or ""
    ).strip()
    fields = {
        "session_token": {
            "present": session_token_present,
            "source": "payload.session_token",
            "value": "<redacted>" if session_token_present else "",
        },
        "session_token_hash": {
            "present": bool(str(token_evidence.get("session_token_hash") or "").strip()),
            "source": "server_computed_session_token_hash",
            "value": str(token_evidence.get("session_token_hash") or ""),
        },
        "session_token_surrogate": {
            "present": bool(session_token_surrogate),
            "source": (
                "payload_or_registered_host_adapter_spawn.session_token_surrogate"
            ),
            "value": session_token_surrogate,
        },
        "host_startup_id": {
            "present": bool(host_startup_id),
            "source": "payload_or_registered_host_adapter_spawn.host_startup_id",
            "value": host_startup_id,
        },
        "registered_host_adapter_spawn": {
            "present": bool(registered_public),
            "source": "latest_runtime_contract_revision",
            "value": registered_public,
        },
        "registered_host_adapter_spawn.host_startup_id": {
            "present": bool(str(registered_public.get("host_startup_id") or "").strip()),
            "source": "latest_runtime_contract_revision.registered_host_adapter_spawn",
            "value": str(registered_public.get("host_startup_id") or ""),
        },
        "registered_host_adapter_spawn.session_token_surrogate": {
            "present": bool(
                str(registered_public.get("session_token_surrogate") or "").strip()
            ),
            "source": "latest_runtime_contract_revision.registered_host_adapter_spawn",
            "value": str(registered_public.get("session_token_surrogate") or ""),
        },
        "payload.registered_host_adapter_spawn": {
            "present": bool(payload_registered),
            "source": "payload.registered_host_adapter_spawn",
            "value": payload_registered,
        },
    }
    present_fields = [
        field for field, report in fields.items() if bool(report.get("present"))
    ]
    missing_fields = [
        field for field, report in fields.items() if not bool(report.get("present"))
    ]
    return {
        "schema_version": "mf_subagent_startup_identity_fields.v1",
        "present_fields": present_fields,
        "missing_fields": missing_fields,
        "fields": fields,
        "accepted_alternatives": [
            {
                "id": "same_owner_session_token",
                "present": bool(token_evidence.get("session_token_hash")),
                "required_any": ["session_token"],
            },
            {
                "id": "registered_host_adapter_spawn",
                "present": bool(host_startup_id or session_token_surrogate),
                "required_any": ["host_startup_id", "session_token_surrogate"],
                "registered_host_adapter_spawn_present": bool(registered_public),
            },
        ],
        "copyable_identity_fields": {
            "host_startup_id": host_startup_id,
            "session_token_surrogate": session_token_surrogate,
            "registered_host_adapter_spawn": registered_public,
        },
    }


def _startup_retry_payload_template(
    *,
    payload: Mapping[str, Any],
    missing: Sequence[str],
    identity_fields: Mapping[str, Any],
) -> dict[str, Any]:
    retry_fields = (
        "project_id",
        "backlog_id",
        "task_id",
        "parent_task_id",
        "runtime_context_id",
        "worker_role",
        "worker_id",
        "worker_slot_id",
        "agent_id",
        "actual_host_worker_id",
        "fence_token",
        "actual_cwd",
        "actual_git_root",
        "branch",
        "head_commit",
        "base_commit",
        "target_head_commit",
        "merge_queue_id",
        "owned_files",
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "route_token_ref",
        "visible_injection_manifest_hash",
        "observer_command_id",
        "read_receipt_hash",
        "read_receipt_event_id",
        "startup_source",
        "host_startup_id",
        "session_token_surrogate",
        "worker_session_id",
        "worker_transcript_ref",
        "worker_transcript_path",
        "harness_type",
        "filer_principal",
    )
    retry: dict[str, Any] = {}
    copyable_identity = (
        identity_fields.get("copyable_identity_fields")
        if isinstance(identity_fields.get("copyable_identity_fields"), Mapping)
        else {}
    )
    for field in retry_fields:
        value = payload.get(field)
        if field == "host_startup_id" and not value:
            value = copyable_identity.get("host_startup_id")
        if field == "session_token_surrogate" and not value:
            value = copyable_identity.get("session_token_surrogate")
        if field == "worker_role" and not value:
            value = "mf_sub"
        if field in {"session_token"}:
            value = "<redacted-if-present>"
        if value:
            retry[field] = value
    for field in missing:
        if field == "session_token_surrogate_or_host_startup_id":
            retry.setdefault(
                "host_startup_id",
                "<fill host_startup_id or session_token_surrogate>",
            )
            retry.setdefault(
                "session_token_surrogate",
                "<fill session_token_surrogate or host_startup_id>",
            )
            continue
        retry.setdefault(field, f"<fill {field}>")
    retry["append_tool"] = "parallel_branch_startup"
    retry["event_kind"] = "mf_subagent_startup"
    return retry


def _startup_graph_trace_ids(payload: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in (
        "graph_trace_ids",
        "graph_query_trace_ids",
        "trace_ids",
        "graph_trace_id",
        "trace_id",
    ):
        values.extend(_startup_string_list(payload.get(key)))
    evidence = payload.get("graph_trace_evidence")
    if isinstance(evidence, Mapping):
        for key in (
            "trace_ids",
            "graph_trace_ids",
            "graph_query_trace_ids",
            "trace_id",
            "graph_trace_id",
        ):
            values.extend(_startup_string_list(evidence.get(key)))
    return list(dict.fromkeys(value for value in values if value))


def _startup_graph_trace_db_evidence(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    trace_ids: Sequence[str],
    task_id: str,
    parent_task_id: str,
    runtime_context_id: str,
    fence_token: str,
) -> dict[str, Any]:
    requested = list(dict.fromkeys(str(item or "").strip() for item in trace_ids if str(item or "").strip()))
    if not requested:
        return {
            "schema_version": "mf_subagent_graph_trace_db_evidence.v1",
            "source": "graph_query_traces",
            "db_verified": False,
            "trace_ids": [],
            "verified_trace_ids": [],
            "missing_trace_ids": [],
            "identity_mismatches": [],
            "query_source": "",
            "query_purpose": "",
            "worker_role": "",
            "task_id": task_id,
            "parent_task_id": parent_task_id,
            "runtime_context_id": runtime_context_id,
            "fence_token_present": bool(str(fence_token or "").strip()),
            "fence_token_hash": runtime_context_secret_hash(fence_token),
            "fence_token_redacted": bool(str(fence_token or "").strip()),
            "raw_fence_token_exposed": False,
        }
    try:
        from . import graph_query_trace

        graph_query_trace.ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT trace_id, query_source, query_purpose, worker_role,
                   parent_task_id, runtime_context_id, task_id, fence_token,
                   status
              FROM graph_query_traces
             WHERE project_id = ?
               AND trace_id IN ({})
            """.format(",".join("?" for _ in requested)),
            (project_id, *requested),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    by_trace: dict[str, sqlite3.Row] = {
        str(row["trace_id"] if isinstance(row, sqlite3.Row) else row[0]): row
        for row in rows
    }
    missing = [trace_id for trace_id in requested if trace_id not in by_trace]
    verified: list[str] = []
    mismatches: list[dict[str, str]] = []
    for trace_id in requested:
        row = by_trace.get(trace_id)
        if row is None:
            continue

        def _row_text(key: str, index: int) -> str:
            return str(row[key] if isinstance(row, sqlite3.Row) else row[index] or "").strip()

        fields = {
            "query_source": _row_text("query_source", 1),
            "query_purpose": _row_text("query_purpose", 2),
            "worker_role": _row_text("worker_role", 3),
            "parent_task_id": _row_text("parent_task_id", 4),
            "runtime_context_id": _row_text("runtime_context_id", 5),
            "task_id": _row_text("task_id", 6),
            "fence_token": _row_text("fence_token", 7),
            "status": _row_text("status", 8),
        }
        expected = {
            "query_source": "mf_subagent",
            "worker_role": RUNTIME_CONTEXT_WORKER_ROLE,
            "task_id": task_id,
            "parent_task_id": parent_task_id,
            "runtime_context_id": runtime_context_id,
            "fence_token": fence_token,
        }
        trace_mismatches = []
        for field, value in expected.items():
            if not value or fields[field] == value:
                continue
            if field == "fence_token":
                trace_mismatches.append(
                    {
                        "trace_id": trace_id,
                        "field": "fence_token_hash",
                        "expected": runtime_context_secret_hash(value),
                        "actual": runtime_context_secret_hash(fields[field]),
                        "raw_fence_token_exposed": False,
                    }
                )
                continue
            trace_mismatches.append(
                {
                    "trace_id": trace_id,
                    "field": field,
                    "expected": value,
                    "actual": fields[field],
                }
            )
        if fields["query_purpose"] not in {
            "subagent_context_build",
            "subagent_gate_validation",
        }:
            trace_mismatches.append(
                {
                    "trace_id": trace_id,
                    "field": "query_purpose",
                    "expected": "subagent_context_build|subagent_gate_validation",
                    "actual": fields["query_purpose"],
                }
            )
        if trace_mismatches:
            mismatches.extend(trace_mismatches)
            continue
        verified.append(trace_id)

    return {
        "schema_version": "mf_subagent_graph_trace_db_evidence.v1",
        "source": "graph_query_traces",
        "db_verified": bool(requested) and not missing and not mismatches and set(verified) == set(requested),
        "trace_ids": verified,
        "verified_trace_ids": verified,
        "requested_trace_ids": requested,
        "missing_trace_ids": missing,
        "identity_mismatches": mismatches,
        "query_source": "mf_subagent" if verified else "",
        "query_purpose": "subagent_gate_validation" if verified else "",
        "worker_role": RUNTIME_CONTEXT_WORKER_ROLE if verified else "",
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "runtime_context_id": runtime_context_id,
        "fence_token_present": bool(str(fence_token or "").strip()),
        "fence_token_hash": runtime_context_secret_hash(fence_token),
        "fence_token_redacted": bool(str(fence_token or "").strip()),
        "raw_fence_token_exposed": False,
    }


def _startup_blocker(
    *,
    blocker_id: str,
    message: str,
    context: BranchTaskRuntimeContext | None = None,
    missing: tuple[str, ...] = (),
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    missing_fields = list(missing)
    payload: dict[str, Any] = {
        "ok": False,
        "schema_version": MF_SUBAGENT_STARTUP_GATE_SCHEMA_VERSION,
        "status": "blocked",
        "blocked": True,
        "blocker": blocker_id,
        "blocker_id": blocker_id,
        "dispatch_blocker": True,
        "terminal_dispatch_blocker": True,
        "actual_startup_recorded": False,
        "actual_startup_required": True,
        "startup_accepted": False,
        "startup_recorded": False,
        "must_stop": True,
        "close_ready": False,
        "message": message,
        "missing": missing_fields,
        "next_action": {
            "action": "retry_parallel_branch_startup_with_required_fields"
            if missing_fields
            else "inspect_startup_blocker_and_stop",
            "tool": "parallel_branch_startup",
            "description": (
                "Stop before implementation. Retry actual mf_sub startup only with "
                "the canonical startup_recording payload from the worker launch pack "
                "or runtime contract, preserving owned_files and route identity."
                if missing_fields
                else "Stop before implementation and inspect the startup blocker."
            ),
            "required_fields": missing_fields,
            "payload_source": "worker_launch_pack.startup_recording",
        },
    }
    if missing:
        payload["missing_required_fields"] = missing_fields
    lineage_missing = {
        "observer_command_id",
        "read_receipt_hash",
        "read_receipt_event_id",
    }.intersection(missing)
    if lineage_missing:
        payload["next_legal_action"] = {
            "tool": "observer_read_receipt_then_startup",
            "description": (
                "Use the claimed backlog-specific execute_backlog_row "
                "observer_command_id, record the mf_sub read receipt under the "
                "same route lineage, then retry actual startup evidence with "
                "observer_command_id, read_receipt_hash, and read_receipt_event_id."
            ),
            "required_fields": sorted(lineage_missing),
        }
    if context is not None:
        payload["context"] = {
            "project_id": context.project_id,
            "task_id": context.task_id,
            "parent_task_id": _parent_task_id_for_context(context),
            "worker_id": context.worker_id,
            "worker_slot_id": context.worker_slot_id or context.worker_id,
            "actual_host_worker_id": context.actual_host_worker_id,
            "agent_id": context.agent_id,
            "allocation_owner": context.allocation_owner or context.agent_id,
            "observer_allocation_owner": context.allocation_owner or context.agent_id,
            "fence_token_present": bool(context.fence_token),
            "fence_token_hash": runtime_context_secret_hash(context.fence_token),
            "fence_token_redacted": bool(context.fence_token),
            "raw_fence_token_exposed": False,
            "branch": context.branch_ref,
            "worktree": context.worktree_path,
            "base_commit": context.base_commit,
            "target_head_commit": context.target_head_commit,
            "merge_queue_id": context.merge_queue_id,
            "status": context.status,
            "governance_project_id": context.governance_project_id
            or context.project_id,
            "target_project_id": context.target_project_id or context.project_id,
            "target_project_root": context.target_project_root,
        }
    if details:
        payload["details"] = dict(details)
    return payload


def _startup_refusal_timeline_event(
    *,
    project_id: str,
    task_id: str,
    result: Mapping[str, Any],
    payload: Mapping[str, Any],
    context: BranchTaskRuntimeContext | None = None,
    token_evidence: Mapping[str, Any] | None = None,
    registered_host_adapter_identity: Mapping[str, Any] | None = None,
    expected_runtime_context_id: str = "",
) -> dict[str, Any]:
    route_identity = _startup_route_identity(payload)
    details = result.get("details") if isinstance(result.get("details"), Mapping) else {}
    missing = list(result.get("missing") or result.get("missing_required_fields") or [])
    runtime_context_id = str(
        payload.get("runtime_context_id")
        or expected_runtime_context_id
        or (runtime_context_id_for_branch_context(context) if context is not None else "")
        or ""
    ).strip()
    agent_id = str(
        payload.get("agent_id")
        or (context.agent_id if context is not None else "")
        or ""
    ).strip()
    allocation_owner = str(
        payload.get("allocation_owner")
        or payload.get("observer_allocation_owner")
        or (context.allocation_owner if context is not None else "")
        or (context.agent_id if context is not None else "")
        or ""
    ).strip()
    actual_host_worker_id = str(
        payload.get("actual_host_worker_id")
        or payload.get("host_worker_id")
        or payload.get("worker_id")
        or ""
    ).strip()
    agent_id_match_mode = str(details.get("agent_id_match_mode") or "")
    if not agent_id_match_mode and str(result.get("blocker_id") or "") == "agent_id_mismatch":
        agent_id_match_mode = "blocked_without_host_adapter_surrogate"
    worker_session_id = str(
        payload.get("worker_session_id")
        or payload.get("session_id")
        or payload.get("host_session_id")
        or ""
    ).strip()
    worker_transcript_path = str(
        payload.get("worker_transcript_path") or payload.get("transcript_path") or ""
    ).strip()
    worker_transcript_ref = str(
        payload.get("worker_transcript_ref") or payload.get("transcript_ref") or ""
    ).strip()
    harness_type = str(
        payload.get("harness_type") or payload.get("worker_harness_type") or ""
    ).strip()
    filer_principal = str(
        payload.get("filer_principal")
        or payload.get("actor")
        or agent_id
        or worker_session_id
        or actual_host_worker_id
        or "mf_sub"
    ).strip()
    filed_on_behalf_by = str(
        payload.get("filed_on_behalf_by")
        or payload.get("on_behalf_of")
        or ""
    ).strip()
    startup_identity_fields = _startup_identity_field_report(
        payload=payload,
        token_evidence=token_evidence,
        registered_identity=registered_host_adapter_identity,
    )
    next_action = (
        dict(result.get("next_action"))
        if isinstance(result.get("next_action"), Mapping)
        else {}
    )
    if next_action:
        retry_template = _startup_retry_payload_template(
            payload=payload,
            missing=missing,
            identity_fields=startup_identity_fields,
        )
        next_action.update(
            {
                "canonical_retry_payload_source": (
                    "worker_launch_pack.startup_recording"
                ),
                "startup_payload_source": "worker_launch_pack.startup_recording",
                "copyable_retry_payload": retry_template,
                "startup_identity_fields": startup_identity_fields,
                "present_startup_identity_fields": list(
                    startup_identity_fields.get("present_fields") or []
                ),
                "missing_startup_identity_fields": list(
                    startup_identity_fields.get("missing_fields") or []
                ),
            }
        )
    refusal = {
        "schema_version": MF_SUBAGENT_STARTUP_REFUSAL_SCHEMA_VERSION,
        "gate_kind": "mf_subagent.startup",
        "status": "blocked",
        "ok": False,
        "blocked": True,
        "decision": "refused",
        "must_stop": True,
        "blocker_id": str(result.get("blocker_id") or result.get("blocker") or ""),
        "message": str(result.get("message") or ""),
        "missing": missing,
        "missing_required_fields": missing,
        "next_action": next_action,
        "startup_identity_fields": startup_identity_fields,
        "present_startup_identity_fields": list(
            startup_identity_fields.get("present_fields") or []
        ),
        "missing_startup_identity_fields": list(
            startup_identity_fields.get("missing_fields") or []
        ),
        "project_id": project_id,
        "backlog_id": (
            context.backlog_id if context is not None else str(payload.get("backlog_id") or "")
        ),
        "task_id": task_id or str(payload.get("task_id") or ""),
        "parent_task_id": str(
            payload.get("parent_task_id")
            or (context.root_task_id if context is not None else "")
            or (context.backlog_id if context is not None else "")
            or ""
        ),
        "runtime_context_id": runtime_context_id,
        "expected_runtime_context_id": expected_runtime_context_id
        or (runtime_context_id_for_branch_context(context) if context is not None else ""),
        "worker_role": str(payload.get("worker_role") or payload.get("role") or "").strip(),
        "worker_id": str(payload.get("worker_id") or "").strip(),
        "worker_slot_id": str(
            payload.get("worker_slot_id")
            or (context.worker_slot_id if context is not None else "")
            or (context.worker_id if context is not None else "")
            or ""
        ).strip(),
        "actual_host_worker_id": actual_host_worker_id,
        "agent_id": agent_id,
        "allocation_owner": allocation_owner,
        "observer_allocation_owner": allocation_owner,
        "agent_id_match_mode": agent_id_match_mode,
        "fence_token": str(payload.get("fence_token") or "").strip(),
        "branch": str(payload.get("branch") or payload.get("branch_ref") or "").strip(),
        "base_commit": str(payload.get("base_commit") or "").strip(),
        "target_head_commit": str(payload.get("target_head_commit") or "").strip(),
        "merge_queue_id": str(payload.get("merge_queue_id") or "").strip(),
        "route_identity": route_identity,
        **route_identity,
        "observer_command_id": str(payload.get("observer_command_id") or "").strip(),
        "read_receipt_hash": str(
            payload.get("read_receipt_hash")
            or payload.get("worker_read_receipt_hash")
            or ""
        ).strip(),
        "read_receipt_event_id": str(
            payload.get("read_receipt_event_id")
            or payload.get("read_receipt_timeline_id")
            or ""
        ).strip(),
        "host_startup_id": str(payload.get("host_startup_id") or "").strip(),
        "startup_source": str(payload.get("startup_source") or "").strip(),
        "worker_session_id": worker_session_id,
        "worker_transcript_path": worker_transcript_path,
        "harness_type": harness_type,
        "filer_principal": filer_principal,
        "filed_on_behalf_by": filed_on_behalf_by,
        "self_filed": bool(filer_principal and not filed_on_behalf_by),
        "session_token_present": bool(str(payload.get("session_token") or "").strip()),
        "session_token_surrogate_present": bool(
            str(
                payload.get("session_token_surrogate")
                or payload.get("session_surrogate")
                or ""
            ).strip()
        ),
        "session_token_evidence_type": str(
            (token_evidence or {}).get("session_token_evidence_type") or ""
        ),
        "registered_host_adapter_spawn_present": bool(registered_host_adapter_identity),
        "registered_host_adapter_spawn": _startup_public_registered_host_adapter_identity(
            registered_host_adapter_identity or {}
        ),
        "details": dict(details),
    }
    return {
        "schema_version": 2,
        "event_type": "mf_subagent.startup",
        "event_kind": "mf_subagent_startup_refusal",
        "phase": "startup_gate",
        "status": "blocked",
        "decision": "refused",
        "severity": "warning",
        "actor": filer_principal or "mf_sub",
        "project_id": project_id,
        "backlog_id": refusal["backlog_id"],
        "task_id": refusal["task_id"],
        "attempt_num": int(context.attempt if context is not None else 0),
        "correlation_id": refusal["observer_command_id"]
        or refusal["host_startup_id"]
        or refusal["task_id"],
        "payload": {
            "mf_subagent_startup_refusal": refusal,
        },
        "artifact_refs": {
            "runtime_context_id": runtime_context_id,
            "blocker_id": refusal["blocker_id"],
            "observer_command_id": refusal["observer_command_id"],
            "read_receipt_event_id": refusal["read_receipt_event_id"],
            "route_id": route_identity.get("route_id", ""),
            "route_context_hash": route_identity.get("route_context_hash", ""),
            "prompt_contract_id": route_identity.get("prompt_contract_id", ""),
            "prompt_contract_hash": route_identity.get("prompt_contract_hash", ""),
            "host_startup_id": refusal["host_startup_id"],
            "startup_source": refusal["startup_source"],
            "worker_session_id": worker_session_id,
            "worker_transcript_path": worker_transcript_path,
            "harness_type": harness_type,
            "filer_principal": filer_principal,
            "filed_on_behalf_by": filed_on_behalf_by,
        },
        "commit_sha": str(payload.get("head_commit") or ""),
    }


def _startup_blocker_with_timeline(
    result: dict[str, Any],
    *,
    project_id: str,
    task_id: str,
    payload: Mapping[str, Any],
    context: BranchTaskRuntimeContext | None = None,
    token_evidence: Mapping[str, Any] | None = None,
    registered_host_adapter_identity: Mapping[str, Any] | None = None,
    expected_runtime_context_id: str = "",
) -> dict[str, Any]:
    result["timeline_event"] = _startup_refusal_timeline_event(
        project_id=project_id,
        task_id=task_id,
        result=result,
        payload=payload,
        context=context,
        token_evidence=token_evidence,
        registered_host_adapter_identity=registered_host_adapter_identity,
        expected_runtime_context_id=expected_runtime_context_id,
    )
    event = result["timeline_event"]
    refusal = (
        event.get("payload", {}).get("mf_subagent_startup_refusal")
        if isinstance(event.get("payload"), Mapping)
        else {}
    )
    refusal = refusal if isinstance(refusal, Mapping) else {}
    result.update(
        {
            "ok": False,
            "status": "blocked",
            "blocked": True,
            "decision": "refused",
            "event_kind": "mf_subagent_startup_refusal",
            "startup_accepted": False,
            "startup_recorded": False,
            "actual_startup_recorded": False,
            "must_stop": True,
            "stop_reason": str(result.get("blocker_id") or result.get("blocker") or ""),
            "missing_required_fields": list(
                result.get("missing_required_fields") or result.get("missing") or []
            ),
            "startup_identity_fields": dict(
                refusal.get("startup_identity_fields")
                if isinstance(refusal.get("startup_identity_fields"), Mapping)
                else {}
            ),
            "present_startup_identity_fields": list(
                refusal.get("present_startup_identity_fields") or []
            ),
            "missing_startup_identity_fields": list(
                refusal.get("missing_startup_identity_fields") or []
            ),
            "next_action": refusal.get("next_action")
            if isinstance(refusal.get("next_action"), Mapping)
            else result.get("next_action"),
            "refusal": {
                "event_kind": "mf_subagent_startup_refusal",
                "status": "blocked",
                "ok": False,
                "blocked": True,
                "must_stop": True,
                "blocker_id": str(refusal.get("blocker_id") or result.get("blocker_id") or ""),
                "missing_required_fields": list(
                    refusal.get("missing_required_fields")
                    or result.get("missing_required_fields")
                    or result.get("missing")
                    or []
                ),
                "next_action": refusal.get("next_action") or result.get("next_action") or {},
                "message": str(refusal.get("message") or result.get("message") or ""),
                "startup_identity_fields": dict(
                    refusal.get("startup_identity_fields")
                    if isinstance(refusal.get("startup_identity_fields"), Mapping)
                    else {}
                ),
                "present_startup_identity_fields": list(
                    refusal.get("present_startup_identity_fields") or []
                ),
                "missing_startup_identity_fields": list(
                    refusal.get("missing_startup_identity_fields") or []
                ),
            },
        }
    )
    return result


def _startup_route_identity(payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        "route_id": str(payload.get("route_id") or "").strip(),
        "route_context_hash": str(payload.get("route_context_hash") or "").strip(),
        "prompt_contract_id": str(payload.get("prompt_contract_id") or "").strip(),
        "prompt_contract_hash": str(payload.get("prompt_contract_hash") or "").strip(),
        "route_token_ref": str(payload.get("route_token_ref") or "").strip(),
        "visible_injection_manifest_hash": str(
            payload.get("visible_injection_manifest_hash") or ""
        ).strip(),
    }


def _startup_read_receipt_hash(payload: Mapping[str, Any]) -> tuple[str, str]:
    direct = str(
        payload.get("read_receipt_hash")
        or payload.get("worker_read_receipt_hash")
        or ""
    ).strip()
    if direct:
        return direct, "payload.read_receipt_hash"
    read_receipt = (
        payload.get("read_receipt")
        if isinstance(payload.get("read_receipt"), Mapping)
        else {}
    )
    for source, value in (
        ("read_receipt.read_receipt_hash", read_receipt.get("read_receipt_hash")),
        (
            "read_receipt.worker_read_receipt_hash",
            read_receipt.get("worker_read_receipt_hash"),
        ),
        ("read_receipt.hash", read_receipt.get("hash")),
        ("read_receipt.launch_text_hash", read_receipt.get("launch_text_hash")),
        ("payload.launch_text_hash", payload.get("launch_text_hash")),
    ):
        text = str(value or "").strip()
        if text:
            return text, source
    return "", ""


def _startup_route_identity_mismatches(
    *,
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> list[dict[str, str]]:
    mismatches: list[dict[str, str]] = []
    for field in (
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "route_token_ref",
        "visible_injection_manifest_hash",
    ):
        expected_value = str(expected.get(field) or "").strip()
        actual_value = str(actual.get(field) or "").strip()
        if expected_value and actual_value and expected_value != actual_value:
            mismatches.append(
                {
                    "field": field,
                    "expected": expected_value,
                    "actual": actual_value,
                }
            )
    return mismatches


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
        return _startup_blocker_with_timeline(
            _startup_blocker(
                blocker_id="missing_task_id",
                message="mf_subagent startup requires task_id",
                missing=("task_id",),
            ),
            project_id=project_id,
            task_id=task,
            payload=payload,
        )

    context = get_branch_context(conn, project_id, task)
    if context is None:
        return _startup_blocker_with_timeline(
            _startup_blocker(
                blocker_id="runtime_context_not_found",
                message="branch runtime context must exist before mf_subagent startup",
                missing=("branch_runtime_context",),
                details={"task_id": task},
            ),
            project_id=project_id,
            task_id=task,
            payload=payload,
        )

    parent_task_id = str(payload.get("parent_task_id") or "").strip()
    worker_role = str(payload.get("worker_role") or payload.get("role") or "").strip()
    worker_role = worker_role.lower().replace("-", "_")
    fence_token = str(payload.get("fence_token") or "").strip()
    reported_worker_id = str(payload.get("worker_id") or "").strip()
    worker_slot_id = str(
        payload.get("worker_slot_id")
        or context.worker_slot_id
        or context.worker_id
        or ""
    ).strip()
    explicit_actual_host_worker_id = str(
        payload.get("actual_host_worker_id") or payload.get("host_worker_id") or ""
    ).strip()
    actual_host_worker_id = str(
        explicit_actual_host_worker_id
        or reported_worker_id
        or ""
    ).strip()
    agent_id = str(payload.get("agent_id") or "").strip()
    allocation_owner = str(
        payload.get("allocation_owner")
        or payload.get("observer_allocation_owner")
        or context.allocation_owner
        or context.agent_id
        or ""
    ).strip()
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
    route_token_ref = str(payload.get("route_token_ref") or "").strip()
    visible_manifest = str(payload.get("visible_injection_manifest_hash") or "").strip()
    target_files = _startup_string_list(payload.get("target_files"))
    owned_files = _startup_string_list(payload.get("owned_files"))
    supplied_runtime_context_id = str(payload.get("runtime_context_id") or "").strip()
    expected_runtime_context_id = runtime_context_id_for_branch_context(context)
    runtime_context_id = supplied_runtime_context_id or expected_runtime_context_id
    latest_revision = get_latest_branch_contract_revision(
        conn,
        project_id,
        expected_runtime_context_id,
    )
    registered_host_adapter_identity = _startup_registered_host_adapter_identity(
        context=context,
        latest_revision=latest_revision,
    )
    payload_registered_host_adapter_identity = (
        _startup_payload_registered_host_adapter_identity(payload)
    )
    payload_registered_identity_allowed = bool(
        _startup_registered_host_adapter_identity_matches_context(
            registered_host_adapter_identity,
            context,
        )
        and _startup_registered_payload_identity_matches(
            payload_identity=payload_registered_host_adapter_identity,
            registered_identity=registered_host_adapter_identity,
        )
    )
    launch_text_hash = str(payload.get("launch_text_hash") or "").strip()
    observer_command_id = str(payload.get("observer_command_id") or "").strip()
    read_receipt_hash, read_receipt_hash_source = _startup_read_receipt_hash(payload)
    read_receipt = payload.get("read_receipt") if isinstance(payload.get("read_receipt"), Mapping) else {}
    read_receipt_event_id = str(
        payload.get("read_receipt_event_id")
        or payload.get("read_receipt_timeline_id")
        or read_receipt.get("event_id")
        or read_receipt.get("timeline_event_id")
        or read_receipt.get("timeline_id")
        or read_receipt.get("id")
        or ""
    ).strip()
    host_startup_id = str(payload.get("host_startup_id") or "").strip()
    if not host_startup_id and payload_registered_identity_allowed:
        host_startup_id = str(
            payload_registered_host_adapter_identity.get("host_startup_id")
            or payload_registered_host_adapter_identity.get("host_session_id")
            or ""
        ).strip()
    payload_for_token_evidence: Mapping[str, Any] = payload
    if (
        payload_registered_identity_allowed
        and not str(payload.get("session_token") or "").strip()
        and not str(
            payload.get("session_token_surrogate")
            or payload.get("session_surrogate")
            or ""
        ).strip()
        and str(
            payload_registered_host_adapter_identity.get("session_token_surrogate")
            or ""
        ).strip()
    ):
        payload_for_token_evidence = {
            **dict(payload),
            "session_token_surrogate": str(
                payload_registered_host_adapter_identity.get(
                    "session_token_surrogate"
                )
                or ""
            ).strip(),
        }
    token_evidence = _startup_token_evidence(
        payload_for_token_evidence,
        stored_token_hash=context.session_token_hash if context is not None else "",
        expected_session_token_ref=runtime_context_session_token_ref(context),
    )
    host_session_id = str(
        payload.get("host_session_id")
        or payload.get("session_id")
        or (
            payload_registered_host_adapter_identity.get("host_session_id")
            if payload_registered_identity_allowed
            else ""
        )
        or token_evidence["session_token_surrogate"]
        or ""
    ).strip()
    worker_session_id = str(
        payload.get("worker_session_id")
        or payload.get("session_id")
        or ""
    ).strip()
    worker_transcript_path = str(
        payload.get("worker_transcript_path") or payload.get("transcript_path") or ""
    ).strip()
    worker_transcript_ref = str(
        payload.get("worker_transcript_ref") or payload.get("transcript_ref") or ""
    ).strip()
    harness_type = str(
        payload.get("harness_type") or payload.get("worker_harness_type") or ""
    ).strip()
    filer_principal = str(
        worker_session_id
        or payload.get("filer_principal")
        or payload.get("actor")
        or agent_id
        or actual_host_worker_id
    ).strip()
    filed_on_behalf_by = str(
        payload.get("filed_on_behalf_by")
        or payload.get("on_behalf_of")
        or ""
    ).strip()
    route_identity = _startup_route_identity(payload)
    service_dispatch_worker_binding = _startup_service_dispatch_worker_binding(
        conn,
        project_id=project_id,
        context=context,
        payload=payload,
        route_identity=route_identity,
    )
    if service_dispatch_worker_binding:
        if not actual_host_worker_id:
            actual_host_worker_id = str(
                service_dispatch_worker_binding.get("actual_host_worker_id")
                or service_dispatch_worker_binding.get("agent_id")
                or ""
            ).strip()
        if not worker_session_id:
            worker_session_id = str(
                service_dispatch_worker_binding.get("worker_session_id")
                or service_dispatch_worker_binding.get("agent_id")
                or ""
            ).strip()
        if not worker_transcript_ref:
            worker_transcript_ref = str(
                service_dispatch_worker_binding.get("transcript_ref") or ""
            ).strip()
    governance_project_id = str(
        payload.get("governance_project_id")
        or payload.get("backlog_project_id")
        or context.governance_project_id
        or project_id
    ).strip()
    target_project_id = str(
        payload.get("target_project_id")
        or payload.get("graph_project_id")
        or context.target_project_id
        or project_id
    ).strip()
    target_project_root = str(
        payload.get("target_project_root")
        or payload.get("target_graph_root")
        or context.target_project_root
        or ""
    ).strip()
    startup_source = str(payload.get("startup_source") or "host_created_mf_sub_worker")
    session_token_surrogate = str(token_evidence["session_token_surrogate"] or "")
    host_adapter_startup = _startup_host_adapter_identity(
        startup_source=startup_source,
        session_token_surrogate=session_token_surrogate,
        host_startup_id=host_startup_id,
        payload=payload,
        registered_identity=registered_host_adapter_identity,
    )
    if (
        host_adapter_startup
        and agent_id
        and allocation_owner
        and agent_id != allocation_owner
        and not explicit_actual_host_worker_id
    ):
        actual_host_worker_id = agent_id

    def _blocked(result: dict[str, Any]) -> dict[str, Any]:
        return _startup_blocker_with_timeline(
            result,
            project_id=project_id,
            task_id=task,
            payload=payload,
            context=context,
            token_evidence=token_evidence,
            registered_host_adapter_identity=registered_host_adapter_identity,
            expected_runtime_context_id=expected_runtime_context_id,
        )

    missing: list[str] = []
    for field, value in (
        ("parent_task_id", parent_task_id),
        ("worker_role", worker_role),
        ("actual_host_worker_id", actual_host_worker_id),
        ("agent_id", agent_id),
        ("fence_token", fence_token),
        ("actual_cwd", actual_cwd),
        ("actual_git_root", actual_git_root),
        ("branch", branch),
        ("head_commit", head_commit),
        ("base_commit", base_commit),
        ("target_head_commit", target_head_commit),
        ("merge_queue_id", merge_queue_id),
        ("runtime_context_id", supplied_runtime_context_id),
        ("route_id", route_id),
        ("route_context_hash", route_context_hash),
        ("prompt_contract_id", prompt_contract_id),
        ("prompt_contract_hash", prompt_contract_hash),
        ("route_token_ref", route_token_ref),
        ("visible_injection_manifest_hash", visible_manifest),
        ("owned_files", owned_files),
        ("observer_command_id", observer_command_id),
        ("read_receipt_hash", read_receipt_hash),
        ("read_receipt_event_id", read_receipt_event_id),
        ("governance_project_id", governance_project_id),
        ("target_project_id", target_project_id),
    ):
        if not value:
            missing.append(field)
    if not (
        token_evidence["session_token_hash"]
        or token_evidence["session_token_surrogate"]
        or (host_adapter_startup and host_startup_id)
    ):
        missing.append("session_token_surrogate_or_host_startup_id")
    if missing:
        return _blocked(_startup_blocker(
            blocker_id="no_truthful_bounded_mf_sub_startup_surface_available",
            message=(
                "actual mf_sub startup evidence is incomplete; branch allocation "
                "or runtime-text startup intent is not sufficient"
            ),
            context=context,
            missing=tuple(missing),
        ))

    if worker_role != "mf_sub":
        return _blocked(_startup_blocker(
            blocker_id="worker_role_mismatch",
            message="mf_subagent startup requires worker_role=mf_sub",
            context=context,
            details={"worker_role": worker_role},
        ))
    if supplied_runtime_context_id != expected_runtime_context_id:
        return _blocked(_startup_blocker(
            blocker_id="runtime_context_id_mismatch",
            message="mf_subagent startup runtime_context_id must match branch runtime context",
            context=context,
            details={
                "runtime_context_id": supplied_runtime_context_id,
                "expected_runtime_context_id": expected_runtime_context_id,
            },
        ))
    if (
        payload.get("worker_slot_id") is not None
        and (context.worker_slot_id or context.worker_id)
        and worker_slot_id != (context.worker_slot_id or context.worker_id)
    ):
        return _blocked(_startup_blocker(
            blocker_id="worker_slot_id_mismatch",
            message="mf_subagent startup worker_slot_id must match branch runtime context",
            context=context,
            details={
                "worker_slot_id": worker_slot_id,
                "expected_worker_slot_id": context.worker_slot_id or context.worker_id,
            },
        ))
    agent_id_match_mode = "actual_host_worker_bound"
    if allocation_owner and agent_id == allocation_owner:
        agent_id_match_mode = "same_as_allocation_owner"
    elif service_dispatch_worker_binding:
        agent_id_match_mode = "observer_subagent_service_dispatch"
    elif host_adapter_startup:
        agent_id_match_mode = "host_adapter_startup_token_surrogate"
    elif allocation_owner:
        return _blocked(_startup_blocker(
            blocker_id="agent_id_mismatch",
            message=(
                "mf_subagent startup agent_id must match allocation_owner unless "
                "a host-adapter startup token surrogate joins the worker identity"
            ),
            context=context,
            details={
                "agent_id": agent_id,
                "expected_agent_id": allocation_owner,
                "agent_id_match_mode": "blocked_without_host_adapter_surrogate",
            },
        ))
    try:
        _require_current_fence(context, fence_token)
    except BranchRuntimeFenceError:
        return _blocked(_startup_blocker(
            blocker_id="fence_invalidated_or_unknown",
            message="mf_subagent startup fence is invalidated or unknown",
            context=context,
            details={"task_id": task},
        ))
    allowed_parent_ids = {
        value
        for value in (
            context.parent_task_id,
            context.root_task_id,
            context.chain_id,
            context.stage_task_id,
            context.backlog_id,
        )
        if value
    }
    if allowed_parent_ids and parent_task_id not in allowed_parent_ids:
        return _blocked(_startup_blocker(
            blocker_id="parent_task_id_mismatch",
            message="mf_subagent startup parent_task_id does not match branch context",
            context=context,
            details={
                "parent_task_id": parent_task_id,
                "allowed_parent_task_ids": sorted(allowed_parent_ids),
            },
        ))
    expected_worktree = context.worktree_path
    if expected_worktree:
        if not _startup_path_matches(actual_git_root, expected_worktree):
            return _blocked(_startup_blocker(
                blocker_id="actual_git_root_mismatch",
                message="mf_subagent startup actual_git_root must match assigned worktree",
                context=context,
                details={
                    "actual_git_root": actual_git_root,
                    "expected_worktree": expected_worktree,
                },
            ))
        if not _startup_path_matches(actual_cwd, expected_worktree):
            return _blocked(_startup_blocker(
                blocker_id="actual_cwd_mismatch",
                message="mf_subagent startup actual_cwd must match assigned worktree",
                context=context,
                details={
                    "actual_cwd": actual_cwd,
                    "expected_worktree": expected_worktree,
                },
            ))
    if _startup_branch_name(branch) != _startup_branch_name(context.branch_ref):
        return _blocked(_startup_blocker(
            blocker_id="branch_mismatch",
            message="mf_subagent startup branch must match branch runtime context",
            context=context,
            details={"branch": branch, "expected_branch": context.branch_ref},
        ))
    if context.base_commit and base_commit and base_commit != context.base_commit:
        return _blocked(_startup_blocker(
            blocker_id="base_commit_mismatch",
            message="mf_subagent startup base_commit must match branch runtime context",
            context=context,
            details={"base_commit": base_commit, "expected_base_commit": context.base_commit},
        ))
    if context.target_head_commit and target_head_commit and target_head_commit != context.target_head_commit:
        return _blocked(_startup_blocker(
            blocker_id="target_head_commit_mismatch",
            message="mf_subagent startup target_head_commit must match branch runtime context",
            context=context,
            details={
                "target_head_commit": target_head_commit,
                "expected_target_head_commit": context.target_head_commit,
            },
        ))
    if context.merge_queue_id and merge_queue_id != context.merge_queue_id:
        return _blocked(_startup_blocker(
            blocker_id="merge_queue_id_mismatch",
            message="mf_subagent startup merge_queue_id must match branch runtime context",
            context=context,
            details={
                "merge_queue_id": merge_queue_id,
                "expected_merge_queue_id": context.merge_queue_id,
            },
        ))
    if (context.governance_project_id or project_id) != governance_project_id:
        return _blocked(_startup_blocker(
            blocker_id="governance_project_id_mismatch",
            message="mf_subagent startup governance_project_id must match branch context",
            context=context,
            details={
                "governance_project_id": governance_project_id,
                "expected_governance_project_id": context.governance_project_id
                or project_id,
            },
        ))
    if (context.target_project_id or project_id) != target_project_id:
        return _blocked(_startup_blocker(
            blocker_id="target_project_id_mismatch",
            message="mf_subagent startup target_project_id must match branch context",
            context=context,
            details={
                "target_project_id": target_project_id,
                "expected_target_project_id": context.target_project_id or project_id,
            },
        ))

    latest_revision = get_latest_branch_contract_revision(
        conn,
        project_id,
        expected_runtime_context_id,
    )
    expected_route_identity = (
        latest_revision.route_identity
        if latest_revision is not None and isinstance(latest_revision.route_identity, Mapping)
        else {}
    )
    route_mismatches = _startup_route_identity_mismatches(
        expected=expected_route_identity,
        actual=route_identity,
    )
    if route_mismatches:
        return _blocked(_startup_blocker(
            blocker_id="route_identity_mismatch",
            message="mf_subagent startup route identity must match latest runtime contract",
            context=context,
            details={
                "runtime_context_id": expected_runtime_context_id,
                "route_identity_mismatches": route_mismatches,
                "latest_contract_revision_id": latest_revision.revision_id
                if latest_revision is not None
                else "",
            },
        ))

    server_verified_identity_modes = {
        "same_as_allocation_owner",
        "observer_subagent_service_dispatch",
    }
    if agent_id_match_mode in server_verified_identity_modes:
        if not context.session_token_hash:
            return _blocked(_startup_blocker(
                blocker_id="session_token_not_server_issued",
                message=(
                    "real mf_subagent startup requires a server-issued "
                    "scoped session_token recorded at allocation time"
                ),
                context=context,
                details={
                    "agent_id": agent_id,
                    "allocation_owner": allocation_owner,
                    "service_dispatch_worker_binding_present": bool(
                        service_dispatch_worker_binding
                    ),
                    "session_token_evidence_type": token_evidence[
                        "session_token_evidence_type"
                    ],
                },
            ))
        if token_evidence["session_token_evidence_type"] not in {
            "server_verified",
            "server_verified_ref",
        }:
            return _blocked(_startup_blocker(
                blocker_id="session_token_not_server_verified",
                message=(
                    "real mf_subagent startup session token identity must "
                    "match the server-issued token hash/ref for this task and fence"
                ),
                context=context,
                details={
                    "agent_id": agent_id,
                    "allocation_owner": allocation_owner,
                    "service_dispatch_worker_binding_present": bool(
                        service_dispatch_worker_binding
                    ),
                    "session_token_evidence_type": token_evidence[
                        "session_token_evidence_type"
                    ],
                },
            ))

    graph_trace_db_evidence = _startup_graph_trace_db_evidence(
        conn,
        project_id=project_id,
        trace_ids=_startup_graph_trace_ids(payload),
        task_id=task,
        parent_task_id=parent_task_id,
        runtime_context_id=runtime_context_id,
        fence_token=fence_token,
    )
    session_token_evidence_type = str(
        token_evidence["session_token_evidence_type"] or ""
    )
    service_dispatch_verified_startup = bool(
        agent_id_match_mode == "observer_subagent_service_dispatch"
        and session_token_evidence_type in {"server_verified", "server_verified_ref"}
    )
    host_adapter_surrogate_startup = bool(
        host_adapter_startup and not service_dispatch_verified_startup
    )
    worker_self_attestation_payload = {
        **dict(payload),
        "worker_session_id": worker_session_id,
        "worker_transcript_path": worker_transcript_path,
        "worker_transcript_ref": worker_transcript_ref,
        "harness_type": harness_type,
        "task_id": task,
        "runtime_context_id": runtime_context_id,
        "fence_token": fence_token,
        "worktree_path": context.worktree_path,
        "actual_cwd": actual_cwd,
        "actual_git_root": actual_git_root,
        "branch_ref": context.branch_ref,
        "branch": branch or context.branch_ref,
        "base_commit": base_commit or context.base_commit,
        "target_head_commit": target_head_commit or context.target_head_commit,
        "head_commit": head_commit,
        "owned_files": list(owned_files),
        "observer_command_id": observer_command_id,
        "read_receipt_hash": read_receipt_hash,
        "read_receipt_hash_source": read_receipt_hash_source,
        "read_receipt_event_id": read_receipt_event_id,
        "route_token_ref": route_token_ref,
        "filer_principal": filer_principal,
        "filed_on_behalf_by": filed_on_behalf_by,
        "actor": filer_principal,
        "agent_id_match_mode": agent_id_match_mode,
        "session_token_evidence_type": session_token_evidence_type,
        "session_token_present": token_evidence["session_token_present"],
        "host_adapter_startup_token_accepted": host_adapter_surrogate_startup,
        "graph_trace_db_evidence": graph_trace_db_evidence,
        "attestation_phase": "startup",
        "service_dispatch_worker_binding": service_dispatch_worker_binding,
    }
    worker_self_attestation = dict(
        verify_worker_transcript(worker_self_attestation_payload)
    )
    surrogate_startup_not_close_satisfying = bool(
        host_adapter_surrogate_startup
        or session_token_evidence_type in {"surrogate", "claimed_unverified_ref"}
    )
    if surrogate_startup_not_close_satisfying:
        blockers = _runtime_context_dedupe(
            list(worker_self_attestation.get("blockers") or [])
            + ["host_adapter_startup_surrogate_not_close_satisfying"]
        )
        worker_self_attestation.update(
            {
                "status": "blocked",
                "ok": False,
                "worker_self_attesting": False,
                "self_attesting": False,
                "finish_time_self_attesting": False,
                "blockers": blockers,
                "finish_time_blockers": blockers,
                "host_adapter_startup_surrogate_not_close_satisfying": True,
            }
        )
    worker_self_attestation = dict(
        redact_runtime_context_payload(
            worker_self_attestation,
            raw_secrets=(
                fence_token,
                str(payload.get("session_token") or ""),
                str(payload.get("worker_session_token") or ""),
            ),
        )
    )
    worker_self_attesting = bool(worker_self_attestation.get("worker_self_attesting"))
    finish_time_self_attesting = bool(
        worker_self_attestation.get("finish_time_self_attesting")
    )

    # For first-sight ('hash') startups, persist the server-computed token hash
    # so subsequent startups can be server-verified.  For 'claimed_unverified'
    # startups the stored hash is already set and we pass '' to preserve it via
    # the CASE WHEN guard in upsert_branch_context.
    _persist_token_hash = (
        token_evidence["session_token_hash"]
        if token_evidence["session_token_evidence_type"] in (
            "hash",
            "server_verified",
            "server_verified_ref",
        )
        else ""
    )
    saved = upsert_branch_context(
        conn,
        replace(
            context,
            status=STATE_RUNNING,
            allocation_owner=allocation_owner,
            worker_slot_id=worker_slot_id,
            actual_host_worker_id=actual_host_worker_id,
            host_startup_id=host_startup_id,
            host_session_id=host_session_id,
            governance_project_id=governance_project_id,
            target_project_id=target_project_id,
            target_project_root=target_project_root,
            target_files=target_files or context.target_files,
            owned_files=owned_files or context.owned_files,
            head_commit=head_commit,
            session_token_hash=_persist_token_hash,
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
        "same_as_expected_worker": bool(actual_host_worker_id == worker_slot_id),
        "fence_token_matches": True,
        "close_satisfying": bool(worker_self_attesting and finish_time_self_attesting),
        "worker_self_attesting": worker_self_attesting,
        "self_attesting": worker_self_attesting,
        "finish_time_self_attesting": finish_time_self_attesting,
        "worker_self_attestation_required": True,
        "worker_self_attestation": worker_self_attestation,
        "graph_trace_db_evidence": graph_trace_db_evidence,
        "raw_launch_text_persisted": False,
        "project_id": project_id,
        "governance_project_id": saved.governance_project_id or project_id,
        "target_project_id": saved.target_project_id or project_id,
        "target_project_root": saved.target_project_root,
        "backlog_id": saved.backlog_id,
        "runtime_context_id": runtime_context_id,
        "task_id": saved.task_id,
        "parent_task_id": parent_task_id,
        "worker_role": "mf_sub",
        "role": "mf_sub",
        "allocation_owner": allocation_owner,
        "observer_allocation_owner": allocation_owner,
        "worker_slot_id": worker_slot_id,
        "worker_id": worker_slot_id,
        "actual_host_worker_id": actual_host_worker_id,
        "agent_id": agent_id,
        "expected_agent_id": allocation_owner,
        "agent_id_match_mode": agent_id_match_mode,
        "service_dispatch_worker_binding": service_dispatch_worker_binding,
        "service_dispatch_worker_binding_present": bool(
            service_dispatch_worker_binding
        ),
        "host_adapter_startup_token_accepted": host_adapter_startup,
        "host_adapter_startup_surrogate_not_close_satisfying": (
            surrogate_startup_not_close_satisfying
        ),
        "fence_token_present": bool(saved.fence_token),
        "fence_token_hash": runtime_context_secret_hash(saved.fence_token),
        "fence_token_redacted": bool(saved.fence_token),
        "raw_fence_token_exposed": False,
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
        "target_files": list(saved.target_files),
        "owned_files": list(owned_files),
        "route_id": route_id,
        "route_context_hash": route_context_hash,
        "prompt_contract_id": prompt_contract_id,
        "prompt_contract_hash": prompt_contract_hash,
        "route_token_ref": route_token_ref,
        "visible_injection_manifest_hash": visible_manifest,
        "session_token_hash": token_evidence["session_token_hash"],
        "session_token_ref": token_evidence["session_token_ref"],
        "session_token_ref_present": token_evidence["session_token_ref_present"],
        "session_token_surrogate": token_evidence["session_token_surrogate"],
        "session_token_evidence_type": token_evidence["session_token_evidence_type"],
        "session_token_present": token_evidence["session_token_present"],
        "session_token_persisted": False,
        "server_issued_session_token_required": (
            agent_id_match_mode in server_verified_identity_modes
        ),
        "server_issued_session_token_verified": (
            token_evidence["session_token_evidence_type"]
            in {"server_verified", "server_verified_ref"}
        ),
        "startup_source": startup_source,
        "startup_timing": "actual_worker_started",
        "worker_session_id": worker_session_id,
        "worker_transcript_path": worker_transcript_path,
        "worker_transcript_ref": worker_transcript_ref,
        "harness_type": harness_type,
        "filer_principal": filer_principal,
        "filed_on_behalf_by": filed_on_behalf_by,
        "self_filed": bool(filer_principal and not filed_on_behalf_by),
        "observer_command_id": observer_command_id,
        "read_receipt_hash": read_receipt_hash,
        "read_receipt_hash_source": read_receipt_hash_source,
        "read_receipt_event_id": read_receipt_event_id,
        "read_receipt": dict(read_receipt),
        "host_startup_id": host_startup_id,
        "host_session_id": host_session_id,
        "identity_join": {
            "schema_version": "mf_subagent_startup_identity_join.v1",
            "runtime_context_id": runtime_context_id,
            "expected_runtime_context_id": expected_runtime_context_id,
            "runtime_context_id_matches": runtime_context_id == expected_runtime_context_id,
            "route_identity_matches_latest_contract": not route_mismatches,
            "read_receipt_lineage_present": bool(
                observer_command_id and read_receipt_hash and read_receipt_event_id
            ),
            "latest_contract_revision_id": latest_revision.revision_id
            if latest_revision is not None
            else "",
            "agent_id_match_mode": agent_id_match_mode,
            "service_dispatch_worker_binding_present": bool(
                service_dispatch_worker_binding
            ),
            "service_dispatch_event_ref": str(
                service_dispatch_worker_binding.get("event_ref") or ""
            ),
        },
    }
    if launch_text_hash:
        gate["launch_text_hash"] = launch_text_hash
    timeline_event = {
        "schema_version": 2,
        "event_type": "mf_subagent.startup",
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "passed",
        "actor": filer_principal or worker_session_id or agent_id or "mf_sub",
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
            "observer_command_id": observer_command_id,
            "read_receipt_event_id": read_receipt_event_id,
            "session_token_evidence_type": token_evidence["session_token_evidence_type"],
            "worker_session_id": worker_session_id,
            "worker_transcript_path": worker_transcript_path,
            "worker_transcript_ref": worker_transcript_ref,
            "harness_type": harness_type,
            "worker_self_attesting": worker_self_attesting,
            "filer_principal": filer_principal,
            "filed_on_behalf_by": filed_on_behalf_by,
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
    governance_project_id: str = "",
    target_project_id: str = "",
    target_project_root: str = "",
    session_token: str = "",
    session_token_ref: str = "",
    allowed_statuses: Sequence[str] | None = None,
    allow_worktree_target_root_alias: bool = False,
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

    query_project_id = str(project_id or "").strip()
    context_project_id = str(governance_project_id or query_project_id).strip()
    context = get_branch_context_by_runtime_context_id(conn, context_project_id, runtime_id)
    if context is None or not context.fence_token:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    try:
        _require_current_fence(context, fence)
    except BranchRuntimeFenceError as exc:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown") from exc

    allowed = set(allowed_statuses or ACTIVE_MF_SUBAGENT_GRAPH_QUERY_STATES)
    if context.status not in allowed:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    token_matches_context = False
    if context.session_token_hash:
        supplied_token_hash = mf_subagent_session_token_hash(session_token)
        token_matches_context = bool(
            (
                supplied_token_hash
                and supplied_token_hash == context.session_token_hash
            )
            or runtime_context_session_token_ref_matches(context, session_token_ref)
        )
        if not token_matches_context:
            raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if context.lease_expires_at and context.lease_expires_at < utc_now():
        if context.session_token_hash and token_matches_context:
            raise _runtime_session_token_expired_error(
                context,
                runtime_context_id=runtime_id,
            )
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")

    requested_target_project_id = str(target_project_id or query_project_id).strip()
    context_governance_project_id = context.governance_project_id or context.project_id
    context_target_project_id = context.target_project_id or context.project_id
    if governance_project_id and governance_project_id != context_governance_project_id:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if query_project_id not in {context.project_id, context_target_project_id}:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if requested_target_project_id != context_target_project_id:
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")
    if not runtime_context_target_project_root_matches(
        context,
        target_project_root,
        allow_worktree_alias=allow_worktree_target_root_alias,
    ):
        raise BranchRuntimeFenceError("fence_invalidated_or_unknown")

    parent = str(parent_task_id or "").strip()
    if parent:
        allowed_parent_ids = {
            value
            for value in (
                context.parent_task_id,
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
    parent_task_id: str = "",
    chain_id: str = "",
    root_task_id: str = "",
    stage_task_id: str = "",
    stage_type: str = "",
    agent_id: str = "",
    worker_id: str = "",
    allocation_owner: str = "",
    worker_slot_id: str = "",
    actual_host_worker_id: str = "",
    host_startup_id: str = "",
    host_session_id: str = "",
    governance_project_id: str = "",
    target_project_id: str = "",
    target_project_root: str = "",
    target_files: Sequence[str] | None = None,
    owned_files: Sequence[str] | None = None,
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
    slot_id = worker_slot_id or worker_id
    worker_slug = _safe_slug(slot_id, "") if slot_id else ""
    prefix = _safe_slug(branch_prefix, "codex")
    try:
        attempt_num = max(1, int(attempt or 1))
    except (TypeError, ValueError):
        attempt_num = 1
    suffix = _attempt_suffix(attempt)
    branch_ref = f"refs/heads/{prefix}/{task_slug}{suffix}"
    worktree_id = f"wt-{task_slug}{suffix}"
    worktree_name = f"{task_slug}{suffix}"
    worktree_root_path = Path(str(worktree_root or ""))
    if worktree_root_path.is_absolute():
        root_leaf_slug = _safe_slug(worktree_root_path.name, "")
        if root_leaf_slug == worktree_name:
            worktree_path = str(worktree_root_path)
        else:
            worktree_parts = [str(worktree_root_path)]
            if worker_slug and _safe_slug(worktree_root_path.name, "") != worker_slug:
                worktree_parts.append(worker_slug)
            worktree_parts.append(worktree_name)
            worktree_path = str(Path(*worktree_parts))
    else:
        worktree_parts = [
            part for part in (worktree_root, worker_slug, worktree_name) if part
        ]
        worktree_path = (
            str(Path(workspace_root, *worktree_parts))
            if workspace_root
            else str(Path(*worktree_parts))
        )
    return BranchTaskRuntimeContext(
        project_id=project_id,
        task_id=task_id,
        batch_id=batch_id,
        backlog_id=backlog_id,
        parent_task_id=parent_task_id,
        chain_id=chain_id,
        root_task_id=root_task_id,
        stage_task_id=stage_task_id or task_id,
        stage_type=stage_type,
        agent_id=agent_id,
        worker_id=worker_id,
        allocation_owner=allocation_owner or agent_id,
        worker_slot_id=slot_id,
        actual_host_worker_id=actual_host_worker_id,
        host_startup_id=host_startup_id,
        host_session_id=host_session_id,
        governance_project_id=governance_project_id or project_id,
        target_project_id=target_project_id or project_id,
        target_project_root=target_project_root,
        target_files=tuple(_runtime_context_string_list(target_files)),
        owned_files=tuple(_runtime_context_string_list(owned_files)),
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
    allow_finish_checkpoint_without_fence: bool = False,
    now_iso: str = "",
) -> dict[str, Any]:
    """Persist a fenced merge queue request for one branch runtime context."""
    ensure_branch_runtime_schema(conn)
    queue_id = str(merge_queue_id or "").strip()
    if not queue_id:
        raise ValueError("merge_queue_id is required")
    requested_status = _normalize_merge_queue_status(status)
    context = get_branch_context(conn, project_id, task_id)
    if context is None:
        raise KeyError(f"branch runtime context not found: {project_id}/{task_id}")
    finish_checkpoint_route_gate = _finish_checkpoint_route_gate_allows_merge_queue_without_fence(
        context,
        fence_token=fence_token,
        checkpoint_id=checkpoint_id,
        require_finish_gate=require_finish_gate,
        allow_finish_checkpoint_without_fence=allow_finish_checkpoint_without_fence,
    )
    if (context.fence_token or fence_token) and not finish_checkpoint_route_gate:
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
        status=requested_status,
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
    parent_task_id: str = "",
    agent_id: str = "",
    worker_id: str = "",
    allocation_owner: str = "",
    worker_slot_id: str = "",
    actual_host_worker_id: str = "",
    host_startup_id: str = "",
    host_session_id: str = "",
    governance_project_id: str = "",
    target_project_id: str = "",
    target_project_root: str = "",
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
        parent_task_id=parent_task_id,
        chain_id=chain_id or root_task_id,
        root_task_id=root_task_id or chain_id,
        stage_task_id=stage_task,
        stage_type=stage_type,
        retry_round=round_num,
        agent_id=agent_id,
        worker_id=worker_id,
        allocation_owner=allocation_owner or agent_id,
        worker_slot_id=worker_slot_id or worker_id,
        actual_host_worker_id=actual_host_worker_id,
        host_startup_id=host_startup_id,
        host_session_id=host_session_id,
        governance_project_id=governance_project_id or project_id,
        target_project_id=target_project_id or project_id,
        target_project_root=target_project_root,
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


def _merge_gate_sibling_warnings(
    items: list[MergeQueueItem],
    selected: MergeQueueItem,
    queue_plan: MergeQueuePlan,
) -> tuple[dict[str, Any], ...]:
    decisions_by_item = {
        decision.queue_item_id: decision
        for decision in queue_plan.decisions
    }
    siblings: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda candidate: (candidate.queue_index, candidate.queue_item_id)):
        if item.queue_item_id == selected.queue_item_id:
            continue
        decision = decisions_by_item.get(item.queue_item_id)
        queue_state = str(decision.queue_state if decision else item.status or "").strip()
        status = str(item.status or "").strip()
        if status in TERMINAL_NON_BLOCKING_STATES or queue_state in TERMINAL_NON_BLOCKING_STATES:
            continue
        siblings.append({
            "queue_item_id": item.queue_item_id,
            "task_id": item.task_id,
            "backlog_id": item.backlog_id,
            "branch_ref": item.branch_ref,
            "queue_index": item.queue_index,
            "status": status,
            "queue_state": queue_state,
            "action": str(decision.action if decision else "").strip(),
        })
    if not siblings:
        return ()
    return ({
        "code": "active_queue_siblings_unmerged",
        "source": "merge_queue",
        "message": "active merge queue has sibling rows that are incomplete or unmerged",
        "merge_queue_id": selected.merge_queue_id,
        "queue_item_id": selected.queue_item_id,
        "task_id": selected.task_id,
        "sibling_count": len(siblings),
        "siblings": siblings,
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
    sibling_warnings = _merge_gate_sibling_warnings(items, selected, queue_plan)

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
        warnings=evidence_warnings + sibling_warnings,
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


def _git_preview_branch_is_ancestor(
    repo_root: Path,
    *,
    branch_commit: str,
    target_commit: str,
    timeout_seconds: int,
) -> bool:
    branch = str(branch_commit or "").strip()
    target = str(target_commit or "").strip()
    if not branch or not target:
        return False
    proc = _git_preview_command(
        repo_root,
        ["merge-base", "--is-ancestor", branch, target],
        timeout_seconds=timeout_seconds,
    )
    return proc.returncode == 0


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
    branch_ref: str = "",
    target_ref: str = "",
    evidence: dict[str, Any] | None = None,
    batch_status: str = "",
    dry_run: bool = True,
    allow_target_ref_mutation: bool = False,
    allow_route_gated_reclaimed_fence_without_token: bool = False,
    message: str = "",
    bug_id: str = "",
    source_contract_id: str = "",
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
    items = _context_enriched_merge_queue_items(
        conn,
        _list_merge_queue_items_with_target_fallback(
            conn,
            project_id,
            merge_queue_id,
            target_ref=target_ref,
        ),
        target_ref=target_ref,
    )
    item = select_merge_queue_item(items, queue_item_id=queue_item_id, task_id=task_id)
    explicit_branch = str(branch_ref or "").strip()
    if explicit_branch:
        item = replace(item, branch_ref=explicit_branch)
        items = [
            item if candidate.queue_item_id == item.queue_item_id else candidate
            for candidate in items
        ]
    repo_root = Path(repo_root_path).resolve()
    selected_target_ref = target_ref or item.target_ref or "refs/heads/main"
    if not item.branch_ref:
        return {
            "ok": False,
            "dry_run": dry_run,
            "executed": False,
            "error": "merge_queue_branch_ref_unresolved",
            "message": (
                "merge queue item has no branch_ref and no branch runtime "
                "context supplied one"
            ),
            "queue_item": merge_queue_item_to_dict(item),
            "recorded": None,
        }
    preview = git_merge_preview_evidence(
        repo_root_path=repo_root,
        target_ref=selected_target_ref,
        branch_ref=item.branch_ref,
        expected_target_head=item.current_target_head or item.validated_target_head,
        timeout_seconds=timeout_seconds,
    )
    target_commit = str(preview.get("target_commit") or "").strip()
    branch_commit = str(preview.get("branch_commit") or item.branch_head or "").strip()
    already_integrated = _git_preview_branch_is_ancestor(
        repo_root,
        branch_commit=branch_commit,
        target_commit=target_commit,
        timeout_seconds=timeout_seconds,
    )
    if already_integrated:
        integrated_item = replace(
            item,
            status=STATE_MERGED,
            merge_commit=target_commit or item.merge_commit,
            target_head_before_merge=(
                item.target_head_before_merge
                or item.current_target_head
                or item.validated_target_head
                or target_commit
            ),
            target_head_after_merge=target_commit or item.target_head_after_merge,
            current_target_head=target_commit or item.current_target_head,
        )
        integrated_items = [
            integrated_item if candidate.queue_item_id == item.queue_item_id else candidate
            for candidate in items
        ]
        gate_plan = decide_merge_gate(
            integrated_items,
            queue_item_id=integrated_item.queue_item_id,
            evidence={**(evidence or {}), "git_conflict_check": preview},
            batch_status=batch_status,
            dry_run=True,
            scenario_id=scenario_id,
        )
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "executed": False,
                "already_integrated": True,
                "target_ref_mutated": False,
                "preview": preview,
                "gate_plan": merge_gate_plan_to_dict(gate_plan),
                "queue_item": merge_queue_item_to_dict(integrated_item),
                "next_actions": ["record_merge_result_without_target_ref_mutation"],
                "recorded": None,
            }
        recorded = record_merge_queue_result(
            conn,
            project_id=project_id,
            merge_queue_id=merge_queue_id,
            queue_item_id=integrated_item.queue_item_id,
            task_id=integrated_item.task_id,
            target_ref=target_ref or item.target_ref,
            status=STATE_MERGED,
            merge_commit=target_commit or branch_commit,
            target_head_before_merge=integrated_item.target_head_before_merge,
            target_head_after_merge=target_commit or branch_commit,
            snapshot_id=integrated_item.snapshot_id,
            projection_id=integrated_item.projection_id,
            fence_token=fence_token,
            allow_route_gated_reclaimed_fence_without_token=(
                allow_route_gated_reclaimed_fence_without_token
            ),
            now_iso=now_iso,
        )
        return {
            "ok": True,
            "dry_run": False,
            "executed": False,
            "already_integrated": True,
            "target_ref_mutated": False,
            "merge_commit": target_commit or branch_commit,
            "preview": preview,
            "gate_plan": merge_gate_plan_to_dict(gate_plan),
            "recorded": recorded,
        }
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
        source_contract_id=source_contract_id,
        parent_chain_sha=before_commit,
        bug_id=bug_id or item.backlog_id or item.task_id,
        merge_queue_id=item.merge_queue_id,
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
        queue_items = _context_enriched_merge_queue_items(
            conn,
            _list_merge_queue_items_with_target_fallback(
                conn,
                project_id,
                queue_id,
                target_ref=target_ref,
            ),
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
