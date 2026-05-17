"""Managed ref runtime for governing existing long-lived project branches.

Managed refs are for pre-existing release, maintenance, or large feature
branches that live longer than one agent task. They remain inside the same
project identity; they are not modeled as separate projects.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


MANAGED_REF_RUNTIME_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS managed_ref_contexts (
    project_id           TEXT NOT NULL,
    ref_name             TEXT NOT NULL,
    ref_type             TEXT NOT NULL DEFAULT 'long_lived',
    target_ref           TEXT NOT NULL DEFAULT 'refs/heads/main',
    merge_base_commit    TEXT NOT NULL DEFAULT '',
    ref_head_commit      TEXT NOT NULL DEFAULT '',
    target_head_commit   TEXT NOT NULL DEFAULT '',
    validated_target_head TEXT NOT NULL DEFAULT '',
    snapshot_id          TEXT NOT NULL DEFAULT '',
    projection_id        TEXT NOT NULL DEFAULT '',
    merge_preview_id     TEXT NOT NULL DEFAULT '',
    merge_queue_id       TEXT NOT NULL DEFAULT '',
    merge_commit         TEXT NOT NULL DEFAULT '',
    rollback_epoch       TEXT NOT NULL DEFAULT '',
    archive_policy       TEXT NOT NULL DEFAULT 'retain_until_archived',
    status               TEXT NOT NULL DEFAULT 'imported',
    evidence_json        TEXT NOT NULL DEFAULT '{}',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    PRIMARY KEY (project_id, ref_name)
);
CREATE INDEX IF NOT EXISTS idx_managed_ref_contexts_project_status
  ON managed_ref_contexts(project_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_managed_ref_contexts_project_target
  ON managed_ref_contexts(project_id, target_ref, status);

CREATE TABLE IF NOT EXISTS managed_ref_events (
    project_id           TEXT NOT NULL,
    event_id             TEXT NOT NULL,
    ref_name             TEXT NOT NULL,
    target_ref           TEXT NOT NULL DEFAULT '',
    from_status          TEXT NOT NULL DEFAULT '',
    to_status            TEXT NOT NULL DEFAULT '',
    operation_type       TEXT NOT NULL DEFAULT '',
    merge_base_commit    TEXT NOT NULL DEFAULT '',
    ref_head_commit      TEXT NOT NULL DEFAULT '',
    target_head_commit   TEXT NOT NULL DEFAULT '',
    snapshot_id          TEXT NOT NULL DEFAULT '',
    projection_id        TEXT NOT NULL DEFAULT '',
    merge_preview_id     TEXT NOT NULL DEFAULT '',
    merge_queue_id       TEXT NOT NULL DEFAULT '',
    merge_commit         TEXT NOT NULL DEFAULT '',
    rollback_epoch       TEXT NOT NULL DEFAULT '',
    evidence_json        TEXT NOT NULL DEFAULT '{}',
    actor                TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL,
    PRIMARY KEY (project_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_managed_ref_events_project_ref
  ON managed_ref_events(project_id, ref_name, created_at);
"""

STATE_IMPORTED = "imported"
STATE_TRACKED = "tracked"
STATE_STALE = "stale"
STATE_MERGE_CANDIDATE = "merge_candidate"
STATE_VALIDATING = "validating"
STATE_MERGE_READY = "merge_ready"
STATE_MERGING = "merging"
STATE_MERGED = "merged"
STATE_ARCHIVED = "archived"
STATE_ABANDONED = "abandoned"
STATE_ROLLBACK_REQUIRED = "rollback_required"

TERMINAL_STATES = {STATE_ARCHIVED, STATE_ABANDONED}
PROJECT_DELETE_ALLOWED_STATES = TERMINAL_STATES
ARCHIVE_ALLOWED_STATES = {STATE_MERGED, STATE_ABANDONED}

ACTION_MATERIALIZE_REF_GRAPH = "materialize_ref_graph"
ACTION_PREPARE_MERGE_PREVIEW = "prepare_merge_preview"
ACTION_QUEUE_MERGE_GATE = "queue_merge_gate"
ACTION_RECOMPUTE_REF_CONTEXT = "recompute_ref_context"
ACTION_WAIT_FOR_VALIDATION = "wait_for_validation"
ACTION_WAIT_FOR_MERGE = "wait_for_merge"
ACTION_ROLLBACK_REF_MERGE = "rollback_ref_merge"
ACTION_ARCHIVE_REF_CONTEXT = "archive_ref_context"
ACTION_NOOP = "noop"


@dataclass(frozen=True)
class ManagedRefContext:
    project_id: str
    ref_name: str
    target_ref: str = "refs/heads/main"
    ref_type: str = "long_lived"
    merge_base_commit: str = ""
    ref_head_commit: str = ""
    target_head_commit: str = ""
    validated_target_head: str = ""
    snapshot_id: str = ""
    projection_id: str = ""
    merge_preview_id: str = ""
    merge_queue_id: str = ""
    merge_commit: str = ""
    rollback_epoch: str = ""
    archive_policy: str = "retain_until_archived"
    status: str = STATE_IMPORTED
    evidence: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ManagedRefDecision:
    project_id: str
    ref_name: str
    target_ref: str
    observed_status: str
    decision_state: str
    action: str
    target_moved: bool = False
    merge_ready: bool = False
    archive_allowed: bool = False
    project_delete_blocker: bool = True
    blockers: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["blockers"] = list(self.blockers)
        payload["next_actions"] = list(self.next_actions)
        return payload


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_managed_ref_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(MANAGED_REF_RUNTIME_SCHEMA_SQL)


def _json(data: Any) -> str:
    return json.dumps(data if data is not None else {}, ensure_ascii=False, sort_keys=True)


def _parse_json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _row_to_context(row: sqlite3.Row) -> ManagedRefContext:
    return ManagedRefContext(
        project_id=str(row["project_id"] or ""),
        ref_name=str(row["ref_name"] or ""),
        ref_type=str(row["ref_type"] or "long_lived"),
        target_ref=str(row["target_ref"] or "refs/heads/main"),
        merge_base_commit=str(row["merge_base_commit"] or ""),
        ref_head_commit=str(row["ref_head_commit"] or ""),
        target_head_commit=str(row["target_head_commit"] or ""),
        validated_target_head=str(row["validated_target_head"] or ""),
        snapshot_id=str(row["snapshot_id"] or ""),
        projection_id=str(row["projection_id"] or ""),
        merge_preview_id=str(row["merge_preview_id"] or ""),
        merge_queue_id=str(row["merge_queue_id"] or ""),
        merge_commit=str(row["merge_commit"] or ""),
        rollback_epoch=str(row["rollback_epoch"] or ""),
        archive_policy=str(row["archive_policy"] or "retain_until_archived"),
        status=str(row["status"] or STATE_IMPORTED),
        evidence=_parse_json_object(row["evidence_json"]),
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
    )


def managed_ref_to_dict(context: ManagedRefContext) -> dict[str, Any]:
    return asdict(context)


def upsert_managed_ref(
    conn: sqlite3.Connection,
    context: ManagedRefContext,
    *,
    actor: str = "observer",
    operation_type: str = "upsert",
    now_iso: str = "",
) -> ManagedRefContext:
    ensure_managed_ref_schema(conn)
    now = now_iso or utc_now()
    previous = get_managed_ref(conn, context.project_id, context.ref_name)
    created_at = context.created_at or (previous.created_at if previous else now)
    conn.execute(
        """
        INSERT INTO managed_ref_contexts (
            project_id, ref_name, ref_type, target_ref, merge_base_commit,
            ref_head_commit, target_head_commit, validated_target_head,
            snapshot_id, projection_id, merge_preview_id, merge_queue_id,
            merge_commit, rollback_epoch, archive_policy, status,
            evidence_json, created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(project_id, ref_name) DO UPDATE SET
            ref_type = excluded.ref_type,
            target_ref = excluded.target_ref,
            merge_base_commit = excluded.merge_base_commit,
            ref_head_commit = excluded.ref_head_commit,
            target_head_commit = excluded.target_head_commit,
            validated_target_head = excluded.validated_target_head,
            snapshot_id = excluded.snapshot_id,
            projection_id = excluded.projection_id,
            merge_preview_id = excluded.merge_preview_id,
            merge_queue_id = excluded.merge_queue_id,
            merge_commit = excluded.merge_commit,
            rollback_epoch = excluded.rollback_epoch,
            archive_policy = excluded.archive_policy,
            status = excluded.status,
            evidence_json = excluded.evidence_json,
            updated_at = excluded.updated_at
        """,
        (
            context.project_id,
            context.ref_name,
            context.ref_type,
            context.target_ref,
            context.merge_base_commit,
            context.ref_head_commit,
            context.target_head_commit,
            context.validated_target_head,
            context.snapshot_id,
            context.projection_id,
            context.merge_preview_id,
            context.merge_queue_id,
            context.merge_commit,
            context.rollback_epoch,
            context.archive_policy,
            context.status,
            _json(context.evidence),
            created_at,
            now,
        ),
    )
    saved = get_managed_ref(conn, context.project_id, context.ref_name)
    if saved is None:
        raise RuntimeError(f"managed ref was not persisted: {context.project_id}/{context.ref_name}")
    _record_managed_ref_event(
        conn,
        saved,
        from_status=previous.status if previous else "",
        operation_type=operation_type,
        actor=actor,
        now_iso=now,
    )
    return saved


def get_managed_ref(
    conn: sqlite3.Connection,
    project_id: str,
    ref_name: str,
) -> ManagedRefContext | None:
    ensure_managed_ref_schema(conn)
    row = conn.execute(
        """
        SELECT *
        FROM managed_ref_contexts
        WHERE project_id = ? AND ref_name = ?
        """,
        (project_id, ref_name),
    ).fetchone()
    return _row_to_context(row) if row else None


def list_managed_refs(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    include_archived: bool = False,
    target_ref: str = "",
) -> list[ManagedRefContext]:
    ensure_managed_ref_schema(conn)
    sql = "SELECT * FROM managed_ref_contexts WHERE project_id = ?"
    params: list[Any] = [project_id]
    if target_ref:
        sql += " AND target_ref = ?"
        params.append(target_ref)
    if not include_archived:
        sql += " AND status NOT IN (?, ?)"
        params.extend([STATE_ARCHIVED, STATE_ABANDONED])
    sql += " ORDER BY target_ref, ref_name"
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_context(row) for row in rows]


def update_managed_ref_status(
    conn: sqlite3.Connection,
    project_id: str,
    ref_name: str,
    status: str,
    *,
    actor: str = "observer",
    operation_type: str = "status_update",
    evidence: dict[str, Any] | None = None,
    now_iso: str = "",
) -> ManagedRefContext:
    current = get_managed_ref(conn, project_id, ref_name)
    if current is None:
        raise KeyError(f"managed ref not found: {project_id}/{ref_name}")
    merged_evidence = dict(current.evidence)
    if evidence:
        merged_evidence.update(evidence)
    return upsert_managed_ref(
        conn,
        ManagedRefContext(
            **{
                **managed_ref_to_dict(current),
                "status": status,
                "evidence": merged_evidence,
            }
        ),
        actor=actor,
        operation_type=operation_type,
        now_iso=now_iso,
    )


def mark_managed_ref_merged(
    conn: sqlite3.Connection,
    project_id: str,
    ref_name: str,
    *,
    merge_commit: str,
    target_head_commit: str,
    merge_queue_id: str = "",
    actor: str = "observer",
    now_iso: str = "",
) -> ManagedRefContext:
    current = get_managed_ref(conn, project_id, ref_name)
    if current is None:
        raise KeyError(f"managed ref not found: {project_id}/{ref_name}")
    return upsert_managed_ref(
        conn,
        ManagedRefContext(
            **{
                **managed_ref_to_dict(current),
                "status": STATE_MERGED,
                "merge_commit": merge_commit,
                "target_head_commit": target_head_commit,
                "validated_target_head": target_head_commit,
                "merge_queue_id": merge_queue_id or current.merge_queue_id,
            }
        ),
        actor=actor,
        operation_type="merge_recorded",
        now_iso=now_iso,
    )


def archive_managed_ref(
    conn: sqlite3.Connection,
    project_id: str,
    ref_name: str,
    *,
    actor: str = "observer",
    evidence: dict[str, Any] | None = None,
    now_iso: str = "",
) -> ManagedRefContext:
    current = get_managed_ref(conn, project_id, ref_name)
    if current is None:
        raise KeyError(f"managed ref not found: {project_id}/{ref_name}")
    decision = decide_managed_ref(current)
    if not decision.archive_allowed:
        raise ValueError(f"managed ref cannot be archived from status {current.status!r}")
    return update_managed_ref_status(
        conn,
        project_id,
        ref_name,
        STATE_ARCHIVED,
        actor=actor,
        operation_type="archive",
        evidence=evidence,
        now_iso=now_iso,
    )


def decide_managed_ref(
    context: ManagedRefContext,
    *,
    current_target_head: str = "",
) -> ManagedRefDecision:
    observed_status = context.status or STATE_IMPORTED
    target_head = current_target_head or context.target_head_commit
    validated_target = context.validated_target_head or context.target_head_commit
    target_moved = bool(target_head and validated_target and target_head != validated_target)

    blockers: list[str] = []
    next_actions: list[str] = []
    decision_state = observed_status
    action = ACTION_NOOP
    merge_ready = False
    archive_allowed = observed_status in ARCHIVE_ALLOWED_STATES
    project_delete_blocker = observed_status not in PROJECT_DELETE_ALLOWED_STATES

    if observed_status in TERMINAL_STATES:
        project_delete_blocker = False
    elif observed_status == STATE_ROLLBACK_REQUIRED:
        blockers.append("rollback_required")
        next_actions.append("rollback_or_replay_ref_merge")
        action = ACTION_ROLLBACK_REF_MERGE
    elif target_moved:
        decision_state = STATE_STALE
        blockers.append("target_ref_moved")
        next_actions.append("recompute_ref_graph_against_target")
        action = ACTION_RECOMPUTE_REF_CONTEXT
    elif observed_status == STATE_IMPORTED or not context.snapshot_id:
        blockers.append("ref_graph_missing")
        next_actions.append("materialize_ref_graph")
        action = ACTION_MATERIALIZE_REF_GRAPH
    elif observed_status in {STATE_VALIDATING}:
        blockers.append("validation_running")
        next_actions.append("wait_for_validation")
        action = ACTION_WAIT_FOR_VALIDATION
    elif observed_status == STATE_MERGING:
        blockers.append("merge_running")
        next_actions.append("wait_for_merge_result")
        action = ACTION_WAIT_FOR_MERGE
    elif observed_status == STATE_MERGED:
        next_actions.append("archive_ref_context")
        action = ACTION_ARCHIVE_REF_CONTEXT
    elif context.merge_preview_id and context.snapshot_id:
        decision_state = STATE_MERGE_READY
        merge_ready = True
        next_actions.append("queue_merge_gate")
        action = ACTION_QUEUE_MERGE_GATE
    else:
        blockers.append("merge_preview_missing")
        next_actions.append("prepare_merge_preview")
        action = ACTION_PREPARE_MERGE_PREVIEW

    return ManagedRefDecision(
        project_id=context.project_id,
        ref_name=context.ref_name,
        target_ref=context.target_ref,
        observed_status=observed_status,
        decision_state=decision_state,
        action=action,
        target_moved=target_moved,
        merge_ready=merge_ready,
        archive_allowed=archive_allowed,
        project_delete_blocker=project_delete_blocker,
        blockers=tuple(blockers),
        next_actions=tuple(next_actions),
    )


def decide_project_deletion_guard(contexts: list[ManagedRefContext] | tuple[ManagedRefContext, ...]) -> dict[str, Any]:
    decisions = [decide_managed_ref(context) for context in contexts]
    blockers = [
        decision
        for decision in decisions
        if decision.project_delete_blocker
    ]
    return {
        "allowed": not blockers,
        "blocker_count": len(blockers),
        "blockers": [decision.to_dict() for decision in blockers],
        "managed_ref_count": len(decisions),
        "required_action": "archive_or_abandon_managed_refs" if blockers else "delete_allowed",
    }


def list_managed_ref_events(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    ref_name: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    ensure_managed_ref_schema(conn)
    bounded_limit = max(1, min(500, int(limit or 50)))
    sql = "SELECT * FROM managed_ref_events WHERE project_id = ?"
    params: list[Any] = [project_id]
    if ref_name:
        sql += " AND ref_name = ?"
        params.append(ref_name)
    sql += " ORDER BY created_at DESC, event_id DESC LIMIT ?"
    params.append(bounded_limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            **dict(row),
            "evidence": _parse_json_object(row["evidence_json"]),
        }
        for row in rows
    ]


def _record_managed_ref_event(
    conn: sqlite3.Connection,
    context: ManagedRefContext,
    *,
    from_status: str,
    operation_type: str,
    actor: str,
    now_iso: str,
) -> None:
    event_key = "|".join([
        context.project_id,
        context.ref_name,
        operation_type,
        context.status,
        context.ref_head_commit,
        context.target_head_commit,
        now_iso,
    ])
    event_digest = hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:12]
    event_id = f"mref-{now_iso.replace('-', '').replace(':', '').replace('Z', '')}-{event_digest}"
    conn.execute(
        """
        INSERT OR REPLACE INTO managed_ref_events (
            project_id, event_id, ref_name, target_ref, from_status, to_status,
            operation_type, merge_base_commit, ref_head_commit, target_head_commit,
            snapshot_id, projection_id, merge_preview_id, merge_queue_id,
            merge_commit, rollback_epoch, evidence_json, actor, created_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            context.project_id,
            event_id,
            context.ref_name,
            context.target_ref,
            from_status,
            context.status,
            operation_type,
            context.merge_base_commit,
            context.ref_head_commit,
            context.target_head_commit,
            context.snapshot_id,
            context.projection_id,
            context.merge_preview_id,
            context.merge_queue_id,
            context.merge_commit,
            context.rollback_epoch,
            _json(context.evidence),
            actor,
            now_iso,
        ),
    )
