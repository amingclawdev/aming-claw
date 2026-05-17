"""Managed ref runtime for governing existing long-lived project branches.

Managed refs are for pre-existing release, maintenance, or large feature
branches that live longer than one agent task. They remain inside the same
project identity; they are not modeled as separate projects.
"""

from __future__ import annotations

import hashlib
import fnmatch
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

BOOTSTRAP_CLASS_TARGET = "target_ref"
BOOTSTRAP_CLASS_AGENT = "short_lived_agent_ref"
BOOTSTRAP_CLASS_MANAGED = "managed_ref"
BOOTSTRAP_CLASS_IGNORED = "ignored_ref"
BOOTSTRAP_CLASS_UNMANAGED = "unmanaged_ref"
BOOTSTRAP_CLASS_BLOCKED = "blocked_ref"

BOOTSTRAP_ACTION_IMPORT = "import"
BOOTSTRAP_ACTION_REFRESH = "refresh"
BOOTSTRAP_ACTION_NOOP = "noop"
BOOTSTRAP_ACTION_SKIP = "skip"
BOOTSTRAP_ACTION_BLOCKED = "blocked"
BOOTSTRAP_APPLY_ACTIONS = {BOOTSTRAP_ACTION_IMPORT, BOOTSTRAP_ACTION_REFRESH}

DEFAULT_MANAGED_REF_PATTERNS = (
    "release/*",
    "maintenance/*",
    "hotfix/*",
    "feature/*",
    "refs/heads/release/*",
    "refs/heads/maintenance/*",
    "refs/heads/hotfix/*",
    "refs/heads/feature/*",
    "refs/remotes/*/release/*",
    "refs/remotes/*/maintenance/*",
    "refs/remotes/*/hotfix/*",
    "refs/remotes/*/feature/*",
)
DEFAULT_AGENT_REF_PATTERNS = (
    "codex/*",
    "agent/*",
    "refs/heads/codex/*",
    "refs/heads/agent/*",
    "refs/remotes/*/codex/*",
    "refs/remotes/*/agent/*",
)
DEFAULT_IGNORED_REF_PATTERNS = (
    "tmp/*",
    "wip/*",
    "scratch/*",
    "refs/tags/*",
    "refs/heads/tmp/*",
    "refs/heads/wip/*",
    "refs/heads/scratch/*",
    "refs/remotes/*/HEAD",
    "refs/remotes/*/tmp/*",
    "refs/remotes/*/wip/*",
    "refs/remotes/*/scratch/*",
)


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


@dataclass(frozen=True)
class ManagedRefBootstrapCandidate:
    project_id: str
    ref_name: str
    target_ref: str
    classification: str
    action: str
    reason: str
    raw_ref_name: str = ""
    ref_head_commit: str = ""
    target_head_commit: str = ""
    merge_base_commit: str = ""
    ahead_count: int = 0
    behind_count: int = 0
    existing_status: str = ""
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["blockers"] = list(self.blockers)
        payload["warnings"] = list(self.warnings)
        return payload


@dataclass(frozen=True)
class ManagedRefBootstrapPlan:
    project_id: str
    target_ref: str
    target_head_commit: str
    candidates: tuple[ManagedRefBootstrapCandidate, ...]
    manage_unmatched_refs: bool = False
    dry_run: bool = True

    def to_dict(self) -> dict[str, Any]:
        candidates = [candidate.to_dict() for candidate in self.candidates]
        return {
            "ok": True,
            "project_id": self.project_id,
            "target_ref": self.target_ref,
            "target_head_commit": self.target_head_commit,
            "dry_run": self.dry_run,
            "manage_unmatched_refs": self.manage_unmatched_refs,
            "summary": _bootstrap_summary(self.candidates),
            "candidates": candidates,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_managed_ref_name(ref_name: str) -> str:
    raw = str(ref_name or "").strip()
    if not raw:
        return ""
    if raw.startswith("refs/"):
        return raw
    if raw.startswith("heads/") or raw.startswith("remotes/") or raw.startswith("tags/"):
        return f"refs/{raw}"
    first = raw.split("/", 1)[0]
    if first in {"origin", "upstream"}:
        return f"refs/remotes/{raw}"
    return f"refs/heads/{raw}"


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


def build_managed_ref_bootstrap_plan(
    conn: sqlite3.Connection,
    project_id: str,
    refs: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    target_ref: str = "refs/heads/main",
    target_head_commit: str = "",
    managed_patterns: list[str] | tuple[str, ...] | None = None,
    agent_patterns: list[str] | tuple[str, ...] | None = None,
    ignored_patterns: list[str] | tuple[str, ...] | None = None,
    manage_unmatched_refs: bool = False,
    evidence: dict[str, Any] | None = None,
) -> ManagedRefBootstrapPlan:
    """Plan same-project managed-ref imports for existing branch refs."""
    ensure_managed_ref_schema(conn)
    normalized_target = normalize_managed_ref_name(target_ref) or "refs/heads/main"
    rows = list(refs or [])
    target_head = str(target_head_commit or "").strip() or _target_head_from_refs(rows, normalized_target)
    managed = tuple(managed_patterns) if managed_patterns is not None else DEFAULT_MANAGED_REF_PATTERNS
    agent = tuple(agent_patterns) if agent_patterns is not None else DEFAULT_AGENT_REF_PATTERNS
    ignored = tuple(ignored_patterns) if ignored_patterns is not None else DEFAULT_IGNORED_REF_PATTERNS
    shared_evidence = dict(evidence or {})
    candidates: list[ManagedRefBootstrapCandidate] = []
    seen: set[str] = set()

    for row in rows:
        ref_row = row if isinstance(row, dict) else {"ref_name": str(row)}
        raw_ref = str(ref_row.get("ref_name") or ref_row.get("name") or ref_row.get("branch") or "").strip()
        ref_name = normalize_managed_ref_name(raw_ref)
        row_target_ref = normalize_managed_ref_name(str(ref_row.get("target_ref") or normalized_target))
        ref_head = str(
            ref_row.get("ref_head_commit")
            or ref_row.get("head_commit")
            or ref_row.get("commit")
            or ref_row.get("sha")
            or ""
        ).strip()
        row_target_head = str(ref_row.get("target_head_commit") or target_head or "").strip()
        merge_base = str(ref_row.get("merge_base_commit") or "").strip()
        ahead_count = _int_value(ref_row.get("ahead_count"))
        behind_count = _int_value(ref_row.get("behind_count"))
        row_evidence = ref_row.get("evidence") if isinstance(ref_row.get("evidence"), dict) else {}
        candidate_evidence = {
            **shared_evidence,
            **dict(row_evidence),
            "managed_ref_bootstrap": {
                "raw_ref_name": raw_ref,
                "normalized_ref_name": ref_name,
                "target_ref": row_target_ref,
            },
        }
        blockers: list[str] = []
        warnings: list[str] = []

        if not ref_name:
            candidates.append(_bootstrap_candidate(
                project_id,
                ref_name="",
                target_ref=row_target_ref,
                raw_ref_name=raw_ref,
                classification=BOOTSTRAP_CLASS_BLOCKED,
                action=BOOTSTRAP_ACTION_BLOCKED,
                reason="ref_name_missing",
                blockers=("ref_name_missing",),
                evidence=candidate_evidence,
            ))
            continue
        if ref_name in seen:
            candidates.append(_bootstrap_candidate(
                project_id,
                ref_name=ref_name,
                target_ref=row_target_ref,
                raw_ref_name=raw_ref,
                ref_head_commit=ref_head,
                target_head_commit=row_target_head,
                merge_base_commit=merge_base,
                ahead_count=ahead_count,
                behind_count=behind_count,
                classification=BOOTSTRAP_CLASS_IGNORED,
                action=BOOTSTRAP_ACTION_SKIP,
                reason="duplicate_ref",
                warnings=("duplicate_ref",),
                evidence=candidate_evidence,
            ))
            continue
        seen.add(ref_name)

        if _same_ref(ref_name, row_target_ref):
            candidates.append(_bootstrap_candidate(
                project_id,
                ref_name=ref_name,
                target_ref=row_target_ref,
                raw_ref_name=raw_ref,
                ref_head_commit=ref_head,
                target_head_commit=row_target_head,
                merge_base_commit=merge_base,
                ahead_count=ahead_count,
                behind_count=behind_count,
                classification=BOOTSTRAP_CLASS_TARGET,
                action=BOOTSTRAP_ACTION_SKIP,
                reason="target_ref",
                evidence=candidate_evidence,
            ))
            continue
        if _matches_patterns(ref_name, ignored):
            candidates.append(_bootstrap_candidate(
                project_id,
                ref_name=ref_name,
                target_ref=row_target_ref,
                raw_ref_name=raw_ref,
                ref_head_commit=ref_head,
                target_head_commit=row_target_head,
                merge_base_commit=merge_base,
                ahead_count=ahead_count,
                behind_count=behind_count,
                classification=BOOTSTRAP_CLASS_IGNORED,
                action=BOOTSTRAP_ACTION_SKIP,
                reason="ignored_pattern",
                evidence=candidate_evidence,
            ))
            continue
        if _matches_patterns(ref_name, agent):
            candidates.append(_bootstrap_candidate(
                project_id,
                ref_name=ref_name,
                target_ref=row_target_ref,
                raw_ref_name=raw_ref,
                ref_head_commit=ref_head,
                target_head_commit=row_target_head,
                merge_base_commit=merge_base,
                ahead_count=ahead_count,
                behind_count=behind_count,
                classification=BOOTSTRAP_CLASS_AGENT,
                action=BOOTSTRAP_ACTION_SKIP,
                reason="short_lived_agent_ref",
                evidence=candidate_evidence,
            ))
            continue

        should_manage = manage_unmatched_refs or _matches_patterns(ref_name, managed)
        if not should_manage:
            candidates.append(_bootstrap_candidate(
                project_id,
                ref_name=ref_name,
                target_ref=row_target_ref,
                raw_ref_name=raw_ref,
                ref_head_commit=ref_head,
                target_head_commit=row_target_head,
                merge_base_commit=merge_base,
                ahead_count=ahead_count,
                behind_count=behind_count,
                classification=BOOTSTRAP_CLASS_UNMANAGED,
                action=BOOTSTRAP_ACTION_SKIP,
                reason="no_managed_pattern_match",
                evidence=candidate_evidence,
            ))
            continue

        if not ref_head:
            blockers.append("ref_head_missing")
        if not row_target_head:
            blockers.append("target_head_missing")
        if not merge_base:
            warnings.append("merge_base_unknown")

        existing = get_managed_ref(conn, project_id, ref_name)
        existing_status = existing.status if existing else ""
        if existing and existing.status in TERMINAL_STATES:
            blockers.append("existing_ref_context_terminal")
        if blockers:
            candidates.append(_bootstrap_candidate(
                project_id,
                ref_name=ref_name,
                target_ref=row_target_ref,
                raw_ref_name=raw_ref,
                ref_head_commit=ref_head,
                target_head_commit=row_target_head,
                merge_base_commit=merge_base,
                ahead_count=ahead_count,
                behind_count=behind_count,
                existing_status=existing_status,
                classification=BOOTSTRAP_CLASS_BLOCKED,
                action=BOOTSTRAP_ACTION_BLOCKED,
                reason=";".join(blockers),
                blockers=tuple(blockers),
                warnings=tuple(warnings),
                evidence=candidate_evidence,
            ))
            continue

        action = BOOTSTRAP_ACTION_IMPORT
        reason = "new_managed_ref"
        if existing:
            if (
                existing.target_ref == row_target_ref
                and existing.ref_head_commit == ref_head
                and existing.target_head_commit == row_target_head
                and existing.merge_base_commit == merge_base
            ):
                action = BOOTSTRAP_ACTION_NOOP
                reason = "managed_ref_current"
            else:
                action = BOOTSTRAP_ACTION_REFRESH
                reason = "managed_ref_changed"

        candidates.append(_bootstrap_candidate(
            project_id,
            ref_name=ref_name,
            target_ref=row_target_ref,
            raw_ref_name=raw_ref,
            ref_head_commit=ref_head,
            target_head_commit=row_target_head,
            merge_base_commit=merge_base,
            ahead_count=ahead_count,
            behind_count=behind_count,
            existing_status=existing_status,
            classification=BOOTSTRAP_CLASS_MANAGED,
            action=action,
            reason=reason,
            warnings=tuple(warnings),
            evidence=candidate_evidence,
        ))

    return ManagedRefBootstrapPlan(
        project_id=project_id,
        target_ref=normalized_target,
        target_head_commit=target_head,
        candidates=tuple(candidates),
        manage_unmatched_refs=manage_unmatched_refs,
        dry_run=True,
    )


def apply_managed_ref_bootstrap_plan(
    conn: sqlite3.Connection,
    plan: ManagedRefBootstrapPlan,
    *,
    actor: str = "observer",
    now_iso: str = "",
) -> dict[str, Any]:
    """Persist import/refresh candidates from a managed-ref bootstrap plan."""
    ensure_managed_ref_schema(conn)
    applied: list[ManagedRefContext] = []
    skipped: list[dict[str, Any]] = []
    for candidate in plan.candidates:
        if candidate.action not in BOOTSTRAP_APPLY_ACTIONS:
            skipped.append(candidate.to_dict())
            continue

        existing = get_managed_ref(conn, plan.project_id, candidate.ref_name)
        previous = managed_ref_to_dict(existing) if existing else {}
        status = STATE_IMPORTED if candidate.action == BOOTSTRAP_ACTION_IMPORT else STATE_STALE
        evidence = dict(existing.evidence) if existing else {}
        evidence.update(candidate.evidence)
        evidence["managed_ref_bootstrap"] = {
            **dict(evidence.get("managed_ref_bootstrap") or {}),
            "action": candidate.action,
            "reason": candidate.reason,
            "ahead_count": candidate.ahead_count,
            "behind_count": candidate.behind_count,
        }
        if previous:
            evidence["managed_ref_bootstrap"]["previous_ref_head_commit"] = previous.get("ref_head_commit", "")
            evidence["managed_ref_bootstrap"]["previous_target_head_commit"] = previous.get("target_head_commit", "")

        base = previous if previous else {
            "project_id": plan.project_id,
            "ref_name": candidate.ref_name,
            "target_ref": candidate.target_ref,
        }
        saved = upsert_managed_ref(
            conn,
            ManagedRefContext(
                **{
                    **base,
                    "target_ref": candidate.target_ref,
                    "ref_type": "long_lived",
                    "merge_base_commit": candidate.merge_base_commit,
                    "ref_head_commit": candidate.ref_head_commit,
                    "target_head_commit": candidate.target_head_commit,
                    "validated_target_head": candidate.target_head_commit,
                    "snapshot_id": "" if candidate.action == BOOTSTRAP_ACTION_REFRESH else base.get("snapshot_id", ""),
                    "projection_id": "" if candidate.action == BOOTSTRAP_ACTION_REFRESH else base.get("projection_id", ""),
                    "merge_preview_id": "",
                    "merge_queue_id": "",
                    "merge_commit": "",
                    "status": status,
                    "evidence": evidence,
                }
            ),
            actor=actor,
            operation_type=f"bootstrap_{candidate.action}",
            now_iso=now_iso,
        )
        applied.append(saved)

    return {
        "ok": True,
        "project_id": plan.project_id,
        "target_ref": plan.target_ref,
        "target_head_commit": plan.target_head_commit,
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "refs": [managed_ref_to_dict(ref) for ref in applied],
        "skipped": skipped,
        "plan": {
            **plan.to_dict(),
            "dry_run": False,
        },
    }


def _bootstrap_candidate(
    project_id: str,
    *,
    ref_name: str,
    target_ref: str,
    raw_ref_name: str,
    classification: str,
    action: str,
    reason: str,
    ref_head_commit: str = "",
    target_head_commit: str = "",
    merge_base_commit: str = "",
    ahead_count: int = 0,
    behind_count: int = 0,
    existing_status: str = "",
    blockers: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
    evidence: dict[str, Any] | None = None,
) -> ManagedRefBootstrapCandidate:
    return ManagedRefBootstrapCandidate(
        project_id=project_id,
        ref_name=ref_name,
        target_ref=target_ref,
        classification=classification,
        action=action,
        reason=reason,
        raw_ref_name=raw_ref_name,
        ref_head_commit=ref_head_commit,
        target_head_commit=target_head_commit,
        merge_base_commit=merge_base_commit,
        ahead_count=ahead_count,
        behind_count=behind_count,
        existing_status=existing_status,
        blockers=blockers,
        warnings=warnings,
        evidence=dict(evidence or {}),
    )


def _bootstrap_summary(candidates: tuple[ManagedRefBootstrapCandidate, ...]) -> dict[str, Any]:
    by_action: dict[str, int] = {}
    by_classification: dict[str, int] = {}
    for candidate in candidates:
        by_action[candidate.action] = by_action.get(candidate.action, 0) + 1
        by_classification[candidate.classification] = by_classification.get(candidate.classification, 0) + 1
    return {
        "candidate_count": len(candidates),
        "by_action": by_action,
        "by_classification": by_classification,
        "apply_count": sum(1 for candidate in candidates if candidate.action in BOOTSTRAP_APPLY_ACTIONS),
        "blocked_count": sum(1 for candidate in candidates if candidate.action == BOOTSTRAP_ACTION_BLOCKED),
    }


def _target_head_from_refs(refs: list[dict[str, Any]], target_ref: str) -> str:
    for row in refs:
        if not isinstance(row, dict):
            continue
        ref_name = normalize_managed_ref_name(
            str(row.get("ref_name") or row.get("name") or row.get("branch") or "")
        )
        if _same_ref(ref_name, target_ref):
            return str(
                row.get("ref_head_commit")
                or row.get("head_commit")
                or row.get("target_head_commit")
                or row.get("commit")
                or row.get("sha")
                or ""
            ).strip()
    return ""


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _same_ref(left: str, right: str) -> bool:
    left_aliases = _ref_aliases(left)
    right_aliases = _ref_aliases(right)
    return bool(left_aliases & right_aliases)


def _matches_patterns(ref_name: str, patterns: tuple[str, ...]) -> bool:
    aliases = _ref_aliases(ref_name)
    return any(
        fnmatch.fnmatchcase(alias, pattern)
        for alias in aliases
        for pattern in patterns
    )


def _ref_aliases(ref_name: str) -> set[str]:
    normalized = normalize_managed_ref_name(ref_name)
    aliases = {normalized} if normalized else set()
    if normalized.startswith("refs/heads/"):
        aliases.add(normalized.removeprefix("refs/heads/"))
    elif normalized.startswith("refs/remotes/"):
        rest = normalized.removeprefix("refs/remotes/")
        aliases.add(rest)
        if "/" in rest:
            aliases.add(rest.split("/", 1)[1])
    elif normalized.startswith("refs/tags/"):
        aliases.add(normalized.removeprefix("refs/tags/"))
    return {alias for alias in aliases if alias}


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
    elif observed_status == STATE_STALE:
        blockers.append("stale_ref_context")
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
