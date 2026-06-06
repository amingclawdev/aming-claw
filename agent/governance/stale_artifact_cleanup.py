"""Dry-run and guarded apply workflow for stale governance artifacts."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from . import batch_jobs
from . import task_registry
from . import task_timeline


ACTION_REMOVE_BATCH_WORKTREE = "remove_stale_batch_worktree"
ACTION_CLEAR_BACKLOG_WORKTREE_REFERENCE = "clear_terminal_backlog_worktree_reference"
ACTION_RETAIN_APPEND_ONLY_EVIDENCE = "retain_append_only_evidence"

TERMINAL_BACKLOG_STATUSES = {
    "ABANDONED",
    "CANCELLED",
    "CLOSED",
    "DONE",
    "FAILED",
    "FIXED",
    "MERGED",
    "REDEPLOYED",
    "RESOLVED",
}


class StaleArtifactCleanupError(ValueError):
    """Raised when guarded cleanup apply would be unsafe."""

    def __init__(self, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload = payload or {"ok": False, "error": message}


def cleanup_recommendation(project_id: str) -> dict[str, Any]:
    return {
        "recommended_action": "Run stale_artifact_cleanup dry-run, then apply explicit safe candidate_ids.",
        "api": {
            "dry_run": f"/api/graph-governance/{project_id}/stale-artifact-cleanup",
            "apply": f"/api/graph-governance/{project_id}/stale-artifact-cleanup/apply",
        },
        "mcp": {
            "dry_run_tool": "stale_artifact_cleanup",
            "apply_tool": "stale_artifact_cleanup_apply",
        },
    }


def _utc_now() -> str:
    return batch_jobs.utc_now()


def _candidate_id(kind: str, value: str) -> str:
    digest = hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{digest}"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _row_to_dict(row: sqlite3.Row | tuple, columns: list[str]) -> dict[str, Any]:
    if hasattr(row, "keys"):
        return {str(key): row[key] for key in row.keys()}
    return {column: row[index] for index, column in enumerate(columns)}


def _json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _resolve_path(path: Any) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).resolve())
    except OSError:
        return str(Path(text).absolute())


def _path_under_worktrees(repo_root_path: Path, path: str) -> bool:
    if not path:
        return False
    try:
        batch_jobs.ensure_worktree_path_safe(repo_root_path, path)
        return True
    except Exception:
        return False


def _terminal_execution_status(status: str) -> bool:
    return str(status or "") in task_registry.TERMINAL_STATUSES


def _terminal_batch_status(status: str) -> bool:
    return str(status or "") in batch_jobs.BATCH_TERMINAL_STATUSES


def _fetch_batch_task_rows(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    if not _table_exists(conn, "tasks"):
        return []
    existing = _table_columns(conn, "tasks")
    requested = [
        "task_id",
        "project_id",
        "status",
        "execution_status",
        "type",
        "metadata_json",
        "parent_task_id",
        "trace_id",
        "updated_at",
    ]
    select_parts = [
        column if column in existing else f"'' AS {column}"
        for column in requested
    ]
    rows = conn.execute(
        f"SELECT {', '.join(select_parts)} FROM tasks WHERE project_id=?",
        (project_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = _row_to_dict(row, requested)
        meta = _json_dict(item.get("metadata_json"))
        if meta.get("job_type") != batch_jobs.JOB_BATCH_MIGRATION and not meta.get("worktree_path"):
            continue
        item["metadata"] = meta
        item["worktree_path"] = _resolve_path(meta.get("worktree_path"))
        item["batch_status"] = str(meta.get("batch_status") or "created")
        item["execution_status"] = str(item.get("execution_status") or item.get("status") or "")
        item["is_terminal"] = bool(
            _terminal_batch_status(item["batch_status"])
            or _terminal_execution_status(str(item.get("execution_status") or ""))
            or _terminal_execution_status(str(item.get("status") or ""))
        )
        out.append(item)
    return out


def _fetch_backlog_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(conn, "backlog_bugs"):
        return []
    existing = _table_columns(conn, "backlog_bugs")
    requested = [
        "bug_id",
        "status",
        "runtime_state",
        "current_task_id",
        "root_task_id",
        "worktree_path",
        "worktree_branch",
        "takeover_json",
        "updated_at",
    ]
    select_parts = [
        column if column in existing else f"'' AS {column}"
        for column in requested
    ]
    rows = conn.execute(f"SELECT {', '.join(select_parts)} FROM backlog_bugs").fetchall()
    out = []
    for row in rows:
        item = _row_to_dict(row, requested)
        item["worktree_path"] = _resolve_path(item.get("worktree_path"))
        status = str(item.get("status") or "").upper()
        runtime_state = str(item.get("runtime_state") or "")
        item["is_terminal"] = bool(
            status in TERMINAL_BACKLOG_STATUSES
            or _terminal_execution_status(runtime_state)
        )
        out.append(item)
    return out


def _fetch_related_graph_traces(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    task_ids: set[str],
    backlog_ids: set[str],
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "graph_query_traces"):
        return []
    columns = [
        "trace_id",
        "snapshot_id",
        "query_source",
        "query_purpose",
        "run_id",
        "parent_task_id",
        "runtime_context_id",
        "task_id",
        "worker_role",
        "status",
        "created_at",
        "updated_at",
    ]
    rows = conn.execute(
        """
        SELECT trace_id, snapshot_id, query_source, query_purpose, run_id,
               parent_task_id, runtime_context_id, task_id, worker_role,
               status, created_at, updated_at
          FROM graph_query_traces
         WHERE project_id=?
         ORDER BY created_at, trace_id
        """,
        (project_id,),
    ).fetchall()
    retained: list[dict[str, Any]] = []
    for row in rows:
        item = _row_to_dict(row, columns)
        task_id = str(item.get("task_id") or "")
        parent_task_id = str(item.get("parent_task_id") or "")
        if task_id in task_ids or parent_task_id in task_ids or parent_task_id in backlog_ids:
            retained.append(item)
    return retained


def _count_related_timeline_events(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    task_ids: set[str],
    backlog_ids: set[str],
) -> int:
    if not _table_exists(conn, "task_timeline_events"):
        return 0
    clauses = []
    params: list[Any] = [project_id]
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        clauses.append(f"task_id IN ({placeholders})")
        params.extend(sorted(task_ids))
    if backlog_ids:
        placeholders = ",".join("?" for _ in backlog_ids)
        clauses.append(f"backlog_id IN ({placeholders})")
        params.extend(sorted(backlog_ids))
    if not clauses:
        return 0
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM task_timeline_events WHERE project_id=? AND ({' OR '.join(clauses)})",
        params,
    ).fetchone()
    return int(row["count"] if hasattr(row, "keys") else row[0])


def build_stale_artifact_cleanup_projection(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    repo_root_path: str | Path,
    include_unowned: bool = True,
) -> dict[str, Any]:
    """Return a dry-run projection; no artifacts or append-only evidence are deleted."""

    root = batch_jobs.repo_root(repo_root_path)
    stale_report = batch_jobs.report_stale_worktrees(conn, project_id, repo_root_path=root)
    stale_paths = {_resolve_path(path) for path in stale_report.get("stale_worktrees", [])}
    task_rows = _fetch_batch_task_rows(conn, project_id)
    backlog_rows = _fetch_backlog_rows(conn)

    terminal_tasks_by_path: dict[str, list[dict[str, Any]]] = {}
    active_tasks_by_path: dict[str, list[dict[str, Any]]] = {}
    terminal_task_ids: set[str] = set()
    for row in task_rows:
        path = str(row.get("worktree_path") or "")
        if row.get("is_terminal"):
            terminal_task_ids.add(str(row.get("task_id") or ""))
            if path:
                terminal_tasks_by_path.setdefault(path, []).append(row)
        elif path:
            active_tasks_by_path.setdefault(path, []).append(row)

    active_backlog_refs_by_path: dict[str, list[dict[str, Any]]] = {}
    for row in backlog_rows:
        path = str(row.get("worktree_path") or "")
        if path and not row.get("is_terminal"):
            active_backlog_refs_by_path.setdefault(path, []).append(row)

    candidates: list[dict[str, Any]] = []
    for path in sorted(stale_paths):
        terminal_rows = terminal_tasks_by_path.get(path, [])
        active_rows = active_tasks_by_path.get(path, [])
        active_backlog_rows = active_backlog_refs_by_path.get(path, [])
        path_safe = _path_under_worktrees(root, path)
        safe = bool(
            path_safe and terminal_rows and not active_rows and not active_backlog_rows
        )
        refusal_reasons = []
        if not path_safe:
            refusal_reasons.append("path_outside_worktrees")
        if active_rows:
            refusal_reasons.append("referenced_by_active_batch_task")
        if active_backlog_rows:
            refusal_reasons.append("referenced_by_active_backlog_row")
        if not terminal_rows:
            refusal_reasons.append("missing_terminal_batch_task_evidence")
        active_backlog_evidence = [
            {
                "backlog_id": str(item.get("bug_id") or ""),
                "status": str(item.get("status") or ""),
                "runtime_state": str(item.get("runtime_state") or ""),
                "current_task_id": str(item.get("current_task_id") or ""),
                "root_task_id": str(item.get("root_task_id") or ""),
            }
            for item in active_backlog_rows
        ]
        if safe or include_unowned:
            candidates.append({
                "candidate_id": _candidate_id("batch_worktree", path),
                "artifact_type": "batch_worktree",
                "action": ACTION_REMOVE_BATCH_WORKTREE,
                "path": path,
                "safe_to_apply": safe,
                "refusal_reasons": refusal_reasons,
                "details": {
                    "active_backlog_reference_count": len(active_backlog_rows),
                    "blocked_by_active_backlog_reference": bool(active_backlog_rows),
                    "operator_note": (
                        "Worktree removal is blocked while any active/non-terminal "
                        "backlog row still references the same path."
                        if active_backlog_rows
                        else ""
                    ),
                },
                "evidence": {
                    "path_under_worktrees": path_safe,
                    "terminal_task_ids": [str(item.get("task_id") or "") for item in terminal_rows],
                    "terminal_batch_statuses": sorted(
                        {str(item.get("batch_status") or "") for item in terminal_rows}
                    ),
                    "active_task_ids": [str(item.get("task_id") or "") for item in active_rows],
                    "active_backlog_ids": [
                        str(item.get("bug_id") or "") for item in active_backlog_rows
                    ],
                    "active_backlog_references": active_backlog_evidence,
                    "exists": Path(path).exists(),
                    "append_only_evidence_retained": True,
                },
            })

    terminal_backlog_ids: set[str] = set()
    for row in backlog_rows:
        path = str(row.get("worktree_path") or "")
        if not path or path not in stale_paths:
            continue
        terminal = bool(row.get("is_terminal"))
        path_safe = _path_under_worktrees(root, path)
        active_rows = active_tasks_by_path.get(path, [])
        safe = bool(terminal and path_safe and not active_rows)
        refusal_reasons = []
        if not terminal:
            refusal_reasons.append("backlog_row_not_terminal")
        if not path_safe:
            refusal_reasons.append("path_outside_worktrees")
        if active_rows:
            refusal_reasons.append("referenced_by_active_batch_task")
        if terminal:
            terminal_backlog_ids.add(str(row.get("bug_id") or ""))
        candidates.append({
            "candidate_id": _candidate_id("backlog_worktree_ref", str(row.get("bug_id") or path)),
            "artifact_type": "backlog_worktree_reference",
            "action": ACTION_CLEAR_BACKLOG_WORKTREE_REFERENCE,
            "backlog_id": str(row.get("bug_id") or ""),
            "path": path,
            "safe_to_apply": safe,
            "refusal_reasons": refusal_reasons,
            "evidence": {
                "status": str(row.get("status") or ""),
                "runtime_state": str(row.get("runtime_state") or ""),
                "worktree_branch": str(row.get("worktree_branch") or ""),
                "append_only_evidence_retained": True,
            },
        })

    retained_traces = _fetch_related_graph_traces(
        conn,
        project_id,
        task_ids=terminal_task_ids,
        backlog_ids=terminal_backlog_ids,
    )
    timeline_event_count = _count_related_timeline_events(
        conn,
        project_id,
        task_ids=terminal_task_ids,
        backlog_ids=terminal_backlog_ids,
    )
    safe_count = sum(1 for item in candidates if item.get("safe_to_apply"))
    unsafe_count = len(candidates) - safe_count
    return {
        "ok": True,
        "mode": "dry_run",
        "dry_run": True,
        "project_id": project_id,
        "repo_root": str(root),
        "summary": {
            "candidate_count": len(candidates),
            "safe_apply_count": safe_count,
            "unsafe_candidate_count": unsafe_count,
            "stale_worktree_count": len(stale_paths),
            "backlog_reference_count": sum(
                1 for item in candidates
                if item["artifact_type"] == "backlog_worktree_reference"
            ),
            "append_only_graph_trace_count": len(retained_traces),
            "append_only_timeline_event_count": timeline_event_count,
        },
        "candidates": candidates,
        "append_only_retained": {
            "policy": "retain_append_only_evidence",
            "action": ACTION_RETAIN_APPEND_ONLY_EVIDENCE,
            "graph_query_traces": retained_traces,
            "graph_trace_ids": [str(item.get("trace_id") or "") for item in retained_traces],
            "task_timeline_event_count": timeline_event_count,
            "deleted": False,
        },
        "cleanup": cleanup_recommendation(project_id),
    }


def _append_task_cleanup_history(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    cleanup_record: dict[str, Any],
) -> None:
    row = conn.execute(
        "SELECT metadata_json FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        return
    meta = _json_dict(row["metadata_json"] if hasattr(row, "keys") else row[0])
    history = meta.get("stale_artifact_cleanup_history")
    if not isinstance(history, list):
        history = []
    history.append(cleanup_record)
    meta["stale_artifact_cleanup_history"] = history
    meta["stale_artifact_cleanup"] = cleanup_record
    conn.execute(
        "UPDATE tasks SET metadata_json=?, updated_at=? WHERE task_id=?",
        (json.dumps(meta, ensure_ascii=False, sort_keys=True), _utc_now(), task_id),
    )


def _clear_backlog_worktree_reference(
    conn: sqlite3.Connection,
    backlog_id: str,
    *,
    cleanup_record: dict[str, Any],
) -> None:
    row = conn.execute(
        "SELECT takeover_json FROM backlog_bugs WHERE bug_id=?",
        (backlog_id,),
    ).fetchone()
    if row is None:
        return
    takeover = _json_dict(row["takeover_json"] if hasattr(row, "keys") else row[0])
    history = takeover.get("stale_artifact_cleanup_history")
    if not isinstance(history, list):
        history = []
    history.append(cleanup_record)
    takeover["stale_artifact_cleanup_history"] = history
    takeover["stale_artifact_cleanup"] = cleanup_record
    now = _utc_now()
    conn.execute(
        """
        UPDATE backlog_bugs
           SET worktree_path='',
               worktree_branch='',
               takeover_json=?,
               runtime_updated_at=?,
               updated_at=?
         WHERE bug_id=?
        """,
        (json.dumps(takeover, ensure_ascii=False, sort_keys=True), now, now, backlog_id),
    )


def _remove_worktree(
    *,
    repo_root_path: Path,
    path: str,
    metadata: dict[str, Any],
    remove_branch: bool,
) -> dict[str, Any]:
    safe_path = batch_jobs.ensure_worktree_path_safe(repo_root_path, path)
    if not safe_path.exists():
        return {"removed": False, "reason": "already_missing", "worktree_path": str(safe_path)}
    strategy = batch_jobs._strategy_from_metadata(metadata)
    if not strategy.worktree_path:
        strategy = batch_jobs.BranchStrategy(
            job_type=str(metadata.get("job_type") or batch_jobs.JOB_BATCH_MIGRATION),
            target_branch=str(metadata.get("target_branch") or "main"),
            base_commit=str(metadata.get("base_commit") or ""),
            work_branch=str(metadata.get("work_branch") or ""),
            worktree_path=str(safe_path),
            worktree_relpath=str(metadata.get("worktree_relpath") or ""),
            direct=False,
            merge_policy=str(metadata.get("merge_policy") or "merge_gatekeeper"),
            project_id=str(metadata.get("project_id") or ""),
        )
    if (safe_path / ".git").exists():
        return batch_jobs.abandon_worktree(
            strategy,
            repo_root_path=repo_root_path,
            remove_branch=remove_branch,
        )
    shutil.rmtree(safe_path)
    return {
        "removed": True,
        "branch_removed": False,
        "worktree_path": str(safe_path),
        "branch": strategy.work_branch,
        "removal": "rmtree_safe_worktrees_child",
    }


def apply_stale_artifact_cleanup(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    repo_root_path: str | Path,
    candidate_ids: list[str],
    actor: str = "observer",
    backlog_id: str = "",
    task_id: str = "",
    reason: str = "",
    remove_branch: bool = False,
) -> dict[str, Any]:
    """Apply explicit safe cleanup candidates and record timeline evidence."""

    projection = build_stale_artifact_cleanup_projection(
        conn,
        project_id,
        repo_root_path=repo_root_path,
        include_unowned=True,
    )
    if not candidate_ids:
        payload = {
            "ok": False,
            "error": "candidate_ids_required",
            "message": "apply requires explicit candidate_ids from dry-run projection",
            "projection": projection,
        }
        raise StaleArtifactCleanupError("candidate_ids_required", payload)

    requested = {str(item) for item in candidate_ids}
    by_id = {str(item["candidate_id"]): item for item in projection["candidates"]}
    unknown = sorted(requested - set(by_id))
    unsafe = [by_id[item] for item in sorted(requested & set(by_id)) if not by_id[item].get("safe_to_apply")]
    if unknown or unsafe:
        payload = {
            "ok": False,
            "error": "unsafe_stale_artifact_cleanup_refused",
            "unknown_candidate_ids": unknown,
            "unsafe_candidates": unsafe,
            "projection": projection,
        }
        raise StaleArtifactCleanupError("unsafe_stale_artifact_cleanup_refused", payload)

    root = batch_jobs.repo_root(repo_root_path)
    cleanup_id = f"stale-cleanup-{uuid.uuid4().hex[:12]}"
    applied: list[dict[str, Any]] = []
    task_rows = _fetch_batch_task_rows(conn, project_id)
    task_meta_by_id = {str(row.get("task_id") or ""): row.get("metadata") or {} for row in task_rows}

    for candidate_id in sorted(requested):
        candidate = by_id[candidate_id]
        cleanup_record = {
            "cleanup_id": cleanup_id,
            "candidate_id": candidate_id,
            "artifact_type": candidate["artifact_type"],
            "action": candidate["action"],
            "actor": actor,
            "reason": reason,
            "applied_at": _utc_now(),
            "path": candidate.get("path", ""),
        }
        if candidate["action"] == ACTION_REMOVE_BATCH_WORKTREE:
            terminal_task_ids = list((candidate.get("evidence") or {}).get("terminal_task_ids") or [])
            metadata = task_meta_by_id.get(str(terminal_task_ids[0]), {}) if terminal_task_ids else {}
            removal = _remove_worktree(
                repo_root_path=root,
                path=str(candidate.get("path") or ""),
                metadata=metadata,
                remove_branch=remove_branch,
            )
            cleanup_record["result"] = removal
            for terminal_task_id in terminal_task_ids:
                _append_task_cleanup_history(conn, str(terminal_task_id), cleanup_record=cleanup_record)
            applied.append({**candidate, "result": removal})
        elif candidate["action"] == ACTION_CLEAR_BACKLOG_WORKTREE_REFERENCE:
            backlog_ref = str(candidate.get("backlog_id") or "")
            _clear_backlog_worktree_reference(conn, backlog_ref, cleanup_record=cleanup_record)
            applied.append({**candidate, "result": {"cleared": True, "backlog_id": backlog_ref}})

    timeline_event = task_timeline.record_event(
        conn,
        project_id=project_id,
        backlog_id=backlog_id,
        task_id=task_id,
        event_type="governance.stale_artifact_cleanup.apply",
        phase="cleanup",
        event_kind="stale_artifact_cleanup",
        actor=actor,
        status="applied",
        payload={
            "cleanup_id": cleanup_id,
            "reason": reason,
            "candidate_ids": sorted(requested),
            "applied_count": len(applied),
            "applied": applied,
            "append_only_retained": projection.get("append_only_retained", {}),
        },
    )
    conn.commit()
    return {
        "ok": True,
        "mode": "apply",
        "dry_run": False,
        "project_id": project_id,
        "cleanup_id": cleanup_id,
        "applied_count": len(applied),
        "applied": applied,
        "timeline_event": timeline_event,
        "append_only_retained": projection.get("append_only_retained", {}),
    }
