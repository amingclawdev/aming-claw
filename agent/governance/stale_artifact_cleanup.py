"""Dry-run and guarded apply workflow for stale governance artifacts."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import stat
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from . import batch_jobs
from . import task_registry
from . import task_timeline


ACTION_REMOVE_BATCH_WORKTREE = "remove_stale_batch_worktree"
ACTION_CLEAR_BACKLOG_WORKTREE_REFERENCE = "clear_terminal_backlog_worktree_reference"
ACTION_RETAIN_APPEND_ONLY_EVIDENCE = "retain_append_only_evidence"
ACTION_REMOVE_STALE_GRAPH_SNAPSHOT = "remove_stale_graph_snapshot"

DIMENSION_WORKTREES = "worktrees"
DIMENSION_GRAPH_SNAPSHOTS = "graph_snapshots"
DIMENSION_GOVERNANCE_INDEX = "governance_index"
DIMENSION_STATE_RECONCILE = "state_reconcile"
DIMENSION_ALL = "all"
ALL_DIMENSIONS = {
    DIMENSION_WORKTREES, DIMENSION_GRAPH_SNAPSHOTS,
    DIMENSION_GOVERNANCE_INDEX, DIMENSION_STATE_RECONCILE, DIMENSION_ALL,
}
PLAN_REVISION = 1
PREVIEW_LIMIT = 160
_DESTRUCTIVE_CLEANUP_LOCK = RLock()

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


def _dimension(value: str) -> str:
    selected = str(value or DIMENSION_ALL).strip().lower()
    if selected not in ALL_DIMENSIONS:
        raise StaleArtifactCleanupError("cleanup_dimension_invalid")
    return selected


def _plan_hash(project_id: str, dimension: str, candidates: list[dict[str, Any]]) -> str:
    items = [{
        "candidate_id": item.get("candidate_id"),
        "artifact_type": item.get("artifact_type"),
        "path": item.get("path"),
        "snapshot_id": item.get("snapshot_id"),
        "safe_to_apply": item.get("safe_to_apply"),
        "refusal_reasons": item.get("refusal_reasons"),
        "evidence": item.get("evidence"),
    } for item in candidates]
    payload = {"project_id": project_id, "dimension": dimension,
               "plan_revision": PLAN_REVISION, "candidates": items}
    return "sha256:" + hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


def _path_identity(path: str) -> dict[str, int] | None:
    """Bind a projected directory to its physical inode, rejecting symlinks."""
    if not path:
        return None
    target = Path(path)
    try:
        if target.resolve(strict=True) != target.absolute():
            return None
        info = target.lstat()
    except (OSError, RuntimeError):
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return None
    return {"device": info.st_dev, "inode": info.st_ino,
            "mode": info.st_mode}


def _worktree_clean(path: str) -> bool:
    target = Path(path)
    if not (target / ".git").is_file() or (target / ".git").is_symlink():
        return False
    try:
        result = subprocess.run(
            ["git", "-C", path, "status", "--porcelain", "--ignored=matching",
             "--untracked-files=all"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        return result.returncode == 0 and not result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False


def _derived_preview(project_id: str, dimension: str) -> tuple[list[dict[str, Any]], bool, int, int]:
    """Inventory generated caches without granting removal authority."""
    from .db import _governance_root

    root = _governance_root() / project_id / (
        "governance-index" if dimension == DIMENSION_GOVERNANCE_INDEX
        else "state-reconcile"
    )
    if not root.exists():
        return [], False, 0, 0
    if root.is_symlink() or not root.is_dir():
        return [{
            "candidate_id": _candidate_id(dimension, "invalid_root"),
            "artifact_type": dimension, "action": "retain_derived_cache",
            "path": str(root), "safe_to_apply": False,
            "refusal_reasons": ["derived_root_unreadable_or_symlink"],
            "evidence": {"size_bytes": 0},
        }], False, 1, 0
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return [{
            "candidate_id": _candidate_id(dimension, "unreadable"),
            "artifact_type": dimension, "action": "retain_derived_cache",
            "path": str(root), "safe_to_apply": False,
            "refusal_reasons": ["derived_root_unreadable"],
            "evidence": {"size_bytes": 0},
        }], False, 1, 0
    result: list[dict[str, Any]] = []
    total_bytes = 0
    for child in children:
        identity: dict[str, Any] = {}
        summary = child / ("summary.json" if dimension == DIMENSION_GOVERNANCE_INDEX
                           else "trace/summary.json")
        if (child.is_dir() and not child.is_symlink() and not summary.is_symlink()
                and summary.is_file()):
            try:
                if summary.stat().st_size <= 16384:
                    identity = _json_dict(summary.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                pass
        if (dimension == DIMENSION_STATE_RECONCILE and child.is_dir()
                and not child.is_symlink()):
            run_input = child / "trace/steps/001-run-input/input.json"
            try:
                chain = (child / "trace", child / "trace/steps",
                         child / "trace/steps/001-run-input", run_input)
                if (all(not entry.is_symlink() for entry in chain)
                        and run_input.is_file()
                        and run_input.stat().st_size <= 16384):
                    source = _json_dict(run_input.read_text(encoding="utf-8"))
                    identity["commit_sha"] = str(source.get("commit_sha") or "")
            except (OSError, UnicodeError):
                pass
        size = 0
        try:
            if child.is_symlink():
                size = 0
            elif child.is_file():
                size = child.stat().st_size
            elif child.is_dir() and not child.is_symlink():
                size = sum(p.stat().st_size for p in child.rglob("*") if p.is_file() and not p.is_symlink())
            created_at = datetime.fromtimestamp(child.stat().st_mtime, timezone.utc).isoformat()
        except OSError:
            created_at = ""
        total_bytes += size
        result.append({
            "candidate_id": _candidate_id(dimension, child.name),
            "artifact_type": dimension, "action": "retain_derived_cache",
            "path": str(child), "safe_to_apply": False,
            "refusal_reasons": ["stage_b_archive_rebuild_required"] +
                (["derived_path_symlink_refused"] if child.is_symlink() else []) +
                (["source_identity_unverified"] if not identity.get("commit_sha") else []),
            "evidence": {"run_id": str(identity.get("run_id") or child.name),
                         "snapshot_id": str(identity.get("active_snapshot_id") or identity.get("snapshot_id") or ""),
                         "commit_sha": str(identity.get("commit_sha") or ""),
                         "created_at": created_at, "size_bytes": size},
        })
    return result[:PREVIEW_LIMIT], len(children) > PREVIEW_LIMIT, len(children), total_bytes


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
) -> tuple[list[dict[str, Any]], int]:
    if not _table_exists(conn, "graph_query_traces"):
        return [], 0
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
    cursor = conn.execute(
        """
        SELECT trace_id, snapshot_id, query_source, query_purpose, run_id,
               parent_task_id, runtime_context_id, task_id, worker_role,
               status, created_at, updated_at
          FROM graph_query_traces
         WHERE project_id=?
         ORDER BY created_at, trace_id
        """,
        (project_id,),
    )
    retained: list[dict[str, Any]] = []
    count = 0
    while rows := cursor.fetchmany(256):
        for row in rows:
            item = _row_to_dict(row, columns)
            task_id = str(item.get("task_id") or "")
            parent_task_id = str(item.get("parent_task_id") or "")
            if task_id in task_ids or parent_task_id in task_ids or parent_task_id in backlog_ids:
                count += 1
                if len(retained) < PREVIEW_LIMIT:
                    retained.append(item)
    return retained, count


def _count_related_timeline_events(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    task_ids: set[str],
    backlog_ids: set[str],
) -> int:
    if not _table_exists(conn, "task_timeline_events"):
        return 0
    if not task_ids and not backlog_ids:
        return 0
    cursor = conn.execute(
        "SELECT task_id,backlog_id FROM task_timeline_events WHERE project_id=?",
        (project_id,),
    )
    count = 0
    while rows := cursor.fetchmany(256):
        for row in rows:
            if str(row["task_id"] or "") in task_ids or str(row["backlog_id"] or "") in backlog_ids:
                count += 1
    return count


def _build_graph_snapshot_candidates(
    conn: sqlite3.Connection,
    project_id: str,
) -> list[dict[str, Any]]:
    """Return graph-snapshot dimension candidates in the same conservative model as worktrees."""
    from .graph_snapshot_store import select_snapshot_retention_candidates
    from .server import _graph_release_build_fence_state
    try:
        selection = select_snapshot_retention_candidates(conn, project_id)
        fence = _graph_release_build_fence_state(conn, project_id)
    except Exception:  # noqa: BLE001 - preview fails closed without leaking source paths
        return [{
            "candidate_id": _candidate_id("graph_snapshots_error", project_id),
            "artifact_type": "graph_snapshot_dir",
            "action": ACTION_REMOVE_STALE_GRAPH_SNAPSHOT,
            "snapshot_id": "",
            "path": "",
            "safe_to_apply": False,
            "refusal_reasons": ["retention_selection_unavailable"],
            "evidence": {"error": "retention_selection_unavailable"},
        }]
    candidates: list[dict[str, Any]] = []
    for item in selection.get("candidates", []):
        sid = str(item.get("snapshot_id") or "")
        from .graph_snapshot_store import _snapshot_root
        actual_path = str(_snapshot_root(project_id, sid)) if sid else ""
        exists = bool(item.get("dir_exists"))
        status = str(item.get("status") or "")
        identity = _path_identity(actual_path)
        safe = bool(exists and sid and item.get("in_db")
                    and status == "superseded"
                    and selection.get("reference_authority_complete") is True
                    and fence.get("clear") is True
                    and identity is not None)
        refusal_reasons: list[str] = []
        if not exists:
            refusal_reasons.append("snapshot_dir_missing")
        if not sid:
            refusal_reasons.append("snapshot_id_empty")
        if not item.get("in_db") or status != "superseded":
            refusal_reasons.append("snapshot_ownership_or_status_unverified")
        if selection.get("reference_authority_complete") is not True:
            refusal_reasons.extend(selection.get("global_refusal_reasons") or ["reference_authority_incomplete"])
        if fence.get("clear") is not True:
            refusal_reasons.append("global_current_full_build_fence_active")
        if exists and identity is None:
            refusal_reasons.append("snapshot_path_identity_unverified")
        candidates.append({
            "candidate_id": _candidate_id("graph_snapshot", sid or actual_path),
            "artifact_type": "graph_snapshot_dir",
            "action": ACTION_REMOVE_STALE_GRAPH_SNAPSHOT,
            "snapshot_id": sid,
            "path": actual_path,
            "safe_to_apply": safe,
            "refusal_reasons": refusal_reasons,
            "evidence": {
                "snapshot_kind": str(item.get("snapshot_kind") or ""),
                "status": str(item.get("status") or ""),
                "created_at": str(item.get("created_at") or ""),
                "age_days": item.get("age_days"),
                "size_bytes": int(item.get("size_bytes") or 0),
                "in_db": bool(item.get("in_db")),
                "exists": exists,
                "path_identity": identity,
                "append_only_evidence_retained": True,
            },
        })
    # Also surface protected as refusals (informational)
    for item in selection.get("protected", []):
        sid = str(item.get("snapshot_id") or "")
        from .graph_snapshot_store import _snapshot_root
        actual_path = str(_snapshot_root(project_id, sid)) if sid else ""
        candidates.append({
            "candidate_id": _candidate_id("graph_snapshot_protected", sid),
            "artifact_type": "graph_snapshot_dir",
            "action": ACTION_REMOVE_STALE_GRAPH_SNAPSHOT,
            "snapshot_id": sid,
            "path": actual_path,
            "safe_to_apply": False,
            "refusal_reasons": [f"protected:{r}" for r in (item.get("reasons") or ["protected"])],
            "evidence": {
                "snapshot_kind": str(item.get("snapshot_kind") or ""),
                "status": str(item.get("status") or ""),
                "created_at": str(item.get("created_at") or ""),
                "protected": True,
                "exists": bool(item.get("dir_exists")),
                "path_identity": _path_identity(actual_path),
                "append_only_evidence_retained": True,
            },
        })
    return candidates


def build_stale_artifact_cleanup_projection(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    repo_root_path: str | Path,
    include_unowned: bool = True,
    dimension: str = "",
) -> dict[str, Any]:
    """Return a dry-run projection; no artifacts or append-only evidence are deleted.

    A named dimension scopes the bounded preview. Omission includes all four
    dimensions; derived caches remain preview-only until Stage B.
    """
    dim = _dimension(dimension)
    if dim in {DIMENSION_GOVERNANCE_INDEX, DIMENSION_STATE_RECONCILE}:
        derived, truncated, total, total_bytes = _derived_preview(project_id, dim)
        return {
            "ok": True, "mode": "dry_run", "dry_run": True,
            "project_id": project_id, "dimension": dim,
            "plan_revision": PLAN_REVISION,
            "plan_hash": _plan_hash(project_id, dim, derived),
            "summary": {"candidate_count": len(derived), "safe_apply_count": 0,
                        "total_candidate_count": total,
                        "unsafe_candidate_count": total,
                        "size_bytes": total_bytes,
                        "truncated": truncated,
                        "dimensions": {dim: {"count": total, "visible_count": len(derived),
                                             "safe_count": 0, "size_bytes": total_bytes,
                                             "refused_count": total, "truncated": truncated}}},
            "candidates": derived,
            "append_only_retained": {"policy": "retain_append_only_evidence", "deleted": False},
            "cleanup": cleanup_recommendation(project_id),
        }
    if dim == DIMENSION_GRAPH_SNAPSHOTS:
        graph_items = _build_graph_snapshot_candidates(conn, project_id)
        total = len(graph_items)
        visible = graph_items[:PREVIEW_LIMIT]
        safe = sum(item.get("safe_to_apply") is True for item in graph_items)
        total_bytes = sum(int((item.get("evidence") or {}).get("size_bytes") or 0)
                          for item in graph_items)
        return {
            "ok": True, "mode": "dry_run", "dry_run": True,
            "project_id": project_id, "dimension": dim,
            "plan_revision": PLAN_REVISION,
            "plan_hash": _plan_hash(project_id, dim, visible),
            "summary": {"candidate_count": len(visible),
                        "total_candidate_count": total,
                        "safe_apply_count": safe,
                        "unsafe_candidate_count": total - safe,
                        "graph_snapshot_count": len(visible),
                        "graph_snapshot_safe_count": safe,
                        "size_bytes": total_bytes,
                        "truncated": total > PREVIEW_LIMIT,
                        "dimensions": {dim: {"count": total, "visible_count": len(visible),
                                             "safe_count": safe, "size_bytes": total_bytes,
                                             "refused_count": total - safe,
                                             "truncated": total > PREVIEW_LIMIT}}},
            "candidates": visible,
            "append_only_retained": {"policy": "retain_append_only_evidence", "deleted": False},
            "cleanup": cleanup_recommendation(project_id),
        }

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
        identity = _path_identity(path)
        clean_worktree = _worktree_clean(path) if path_safe and identity else False
        safe = bool(
            path_safe and identity and clean_worktree and terminal_rows
            and len(terminal_rows) <= PREVIEW_LIMIT
            and not active_rows and not active_backlog_rows
        )
        refusal_reasons = []
        if not path_safe:
            refusal_reasons.append("path_outside_worktrees")
        if identity is None:
            refusal_reasons.append("path_identity_unverified")
        if not clean_worktree:
            refusal_reasons.append("worktree_dirty_or_unregistered")
        if active_rows:
            refusal_reasons.append("referenced_by_active_batch_task")
        if active_backlog_rows:
            refusal_reasons.append("referenced_by_active_backlog_row")
        if not terminal_rows:
            refusal_reasons.append("missing_terminal_batch_task_evidence")
        if len(terminal_rows) > PREVIEW_LIMIT:
            refusal_reasons.append("terminal_task_reference_window_unbounded")
        active_backlog_evidence = [
            {
                "backlog_id": str(item.get("bug_id") or ""),
                "status": str(item.get("status") or ""),
                "runtime_state": str(item.get("runtime_state") or ""),
                "current_task_id": str(item.get("current_task_id") or ""),
                "root_task_id": str(item.get("root_task_id") or ""),
            }
            for item in active_backlog_rows[:PREVIEW_LIMIT]
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
                    "terminal_task_ids": [str(item.get("task_id") or "")
                                          for item in terminal_rows[:PREVIEW_LIMIT]],
                    "terminal_task_count": len(terminal_rows),
                    "terminal_batch_statuses": sorted(
                        {str(item.get("batch_status") or "") for item in terminal_rows[:PREVIEW_LIMIT]}
                    ),
                    "active_task_ids": [str(item.get("task_id") or "")
                                        for item in active_rows[:PREVIEW_LIMIT]],
                    "active_task_count": len(active_rows),
                    "active_backlog_ids": [
                        str(item.get("bug_id") or "") for item in active_backlog_rows[:PREVIEW_LIMIT]
                    ],
                    "active_backlog_count": len(active_backlog_rows),
                    "active_backlog_references": active_backlog_evidence,
                    "exists": Path(path).exists(),
                    "path_identity": identity,
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
        identity = _path_identity(path)
        active_rows = active_tasks_by_path.get(path, [])
        safe = bool(terminal and path_safe and identity and not active_rows)
        refusal_reasons = []
        if not terminal:
            refusal_reasons.append("backlog_row_not_terminal")
        if not path_safe:
            refusal_reasons.append("path_outside_worktrees")
        if identity is None:
            refusal_reasons.append("path_identity_unverified")
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
                "path_identity": identity,
                "append_only_evidence_retained": True,
            },
        })

    retained_traces, retained_trace_count = _fetch_related_graph_traces(
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
    # Add graph-snapshot dimension candidates unless scoped to worktrees only
    snapshot_candidates: list[dict[str, Any]] = []
    if dim != DIMENSION_WORKTREES:
        snapshot_candidates = _build_graph_snapshot_candidates(conn, project_id)

    # Filter worktree candidates if scoped to snapshots only
    if dim == DIMENSION_GRAPH_SNAPSHOTS:
        all_candidates = snapshot_candidates
    else:
        all_candidates = candidates + snapshot_candidates

    derived_truncated = False
    derived_counts: dict[str, tuple[int, int]] = {}
    if dim == DIMENSION_ALL:
        for derived_dim in (DIMENSION_GOVERNANCE_INDEX, DIMENSION_STATE_RECONCILE):
            derived, truncated, _total, _bytes = _derived_preview(project_id, derived_dim)
            derived_counts[derived_dim] = (_total, _bytes)
            all_candidates.extend(derived)
            derived_truncated = derived_truncated or truncated
    total_candidates = len(all_candidates) + sum(
        count - sum(item.get("artifact_type") == key for item in all_candidates)
        for key, (count, _bytes) in derived_counts.items()
    )
    truncated = derived_truncated or total_candidates > PREVIEW_LIMIT
    total_bytes = sum(int((item.get("evidence") or {}).get("size_bytes") or 0)
                      for item in all_candidates)
    total_bytes += sum(max(0, byte_count - sum(
        int((item.get("evidence") or {}).get("size_bytes") or 0)
        for item in all_candidates if item.get("artifact_type") == key))
        for key, (_count, byte_count) in derived_counts.items())
    def in_dimension(item: dict[str, Any], key: str) -> bool:
        kind = str(item.get("artifact_type") or "")
        return (kind in {"batch_worktree", "backlog_worktree_reference"}
                if key == DIMENSION_WORKTREES else
                kind == "graph_snapshot_dir" if key == DIMENSION_GRAPH_SNAPSHOTS else
                kind == key)
    dimension_summary = {}
    for key in (DIMENSION_WORKTREES, DIMENSION_GRAPH_SNAPSHOTS,
                DIMENSION_GOVERNANCE_INDEX, DIMENSION_STATE_RECONCILE):
        members = [item for item in all_candidates if in_dimension(item, key)]
        count = derived_counts[key][0] if key in derived_counts else len(members)
        byte_count = derived_counts[key][1] if key in derived_counts else sum(
            int((item.get("evidence") or {}).get("size_bytes") or 0) for item in members)
        safe_count = sum(item.get("safe_to_apply") is True for item in members)
        dimension_summary[key] = {
            "count": count, "visible_count": min(len(members), PREVIEW_LIMIT),
            "safe_count": safe_count, "refused_count": count - safe_count,
            "size_bytes": byte_count, "truncated": truncated,
        }
    all_candidates = all_candidates[:PREVIEW_LIMIT]
    for key, values in dimension_summary.items():
        values["visible_count"] = sum(in_dimension(item, key) for item in all_candidates)
        values["truncated"] = values["count"] > values["visible_count"]

    safe_count = sum(value["safe_count"] for value in dimension_summary.values())
    unsafe_count = sum(value["refused_count"] for value in dimension_summary.values())
    return {
        "ok": True,
        "mode": "dry_run",
        "dry_run": True,
        "project_id": project_id,
        "repo_root": str(root),
        "dimension": dim,
        "plan_revision": PLAN_REVISION,
        "plan_hash": _plan_hash(project_id, dim, all_candidates),
        "summary": {
            "candidate_count": len(all_candidates),
            "total_candidate_count": total_candidates,
            "truncated": truncated,
            "size_bytes": total_bytes,
            "dimensions": dimension_summary,
            "safe_apply_count": safe_count,
            "unsafe_candidate_count": unsafe_count,
            "stale_worktree_count": len(stale_paths),
            "backlog_reference_count": sum(
                1 for item in all_candidates
                if item["artifact_type"] == "backlog_worktree_reference"
            ),
            "graph_snapshot_count": len(snapshot_candidates),
            "graph_snapshot_safe_count": sum(
                1 for item in snapshot_candidates if item.get("safe_to_apply")
            ),
            "append_only_graph_trace_count": retained_trace_count,
            "append_only_timeline_event_count": timeline_event_count,
        },
        "candidates": all_candidates,
        "append_only_retained": {
            "policy": "retain_append_only_evidence",
            "action": ACTION_RETAIN_APPEND_ONLY_EVIDENCE,
            "graph_query_traces": retained_traces,
            "graph_trace_ids": [str(item.get("trace_id") or "") for item in retained_traces],
            "graph_query_traces_truncated": retained_trace_count > len(retained_traces),
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
        if not _worktree_clean(str(safe_path)):
            raise StaleArtifactCleanupError("worktree_dirty_or_unverifiable_refused")
        removal = subprocess.run(
            ["git", "-C", str(repo_root_path), "worktree", "remove", str(safe_path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if removal.returncode:
            raise StaleArtifactCleanupError("worktree_remove_refused")
        branch_removed = False
        if remove_branch and strategy.work_branch:
            branch = subprocess.run(
                ["git", "-C", str(repo_root_path), "branch", "-d", strategy.work_branch],
                capture_output=True, text=True, timeout=30, check=False,
            )
            branch_removed = branch.returncode == 0
        return {"removed": True, "branch_removed": branch_removed,
                "worktree_path": str(safe_path), "branch": strategy.work_branch}
    raise StaleArtifactCleanupError("unregistered_worktree_directory_refused")


def _apply_stale_artifact_cleanup_locked(
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
    dimension: str = "",
    plan_hash: str = "",
    plan_revision: int = 0,
) -> dict[str, Any]:
    """Apply explicit safe cleanup candidates and record timeline evidence."""

    dim = _dimension(dimension)
    projection = build_stale_artifact_cleanup_projection(
        conn,
        project_id,
        repo_root_path=repo_root_path,
        include_unowned=True,
        dimension=dim,
    )
    if (type(plan_revision) is not int or plan_revision != PLAN_REVISION
            or plan_hash != projection["plan_hash"]
            or projection["summary"].get("truncated")):
        raise StaleArtifactCleanupError("stale_cleanup_plan_refused", {
            "ok": False, "error": "stale_cleanup_plan_refused",
            "current_plan_hash": projection["plan_hash"],
            "current_plan_revision": PLAN_REVISION,
        })
    if not candidate_ids:
        payload = {
            "ok": False,
            "error": "candidate_ids_required",
            "message": "apply requires explicit candidate_ids from dry-run projection",
            "plan_hash": projection["plan_hash"],
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
            "unknown_candidate_count": len(unknown),
            "unsafe_candidate_count": len(unsafe),
            "plan_hash": projection["plan_hash"],
        }
        raise StaleArtifactCleanupError("unsafe_stale_artifact_cleanup_refused", payload)

    root = batch_jobs.repo_root(repo_root_path)
    cleanup_id = f"stale-cleanup-{uuid.uuid4().hex[:12]}"
    applied: list[dict[str, Any]] = []

    def partial_refusal(error: str) -> StaleArtifactCleanupError:
        payload = {"ok": False, "error": error, "cleanup_id": cleanup_id,
                   "applied_count": len(applied),
                   "applied_candidate_ids": [item["candidate_id"] for item in applied],
                   "requested_actor": actor,
                   "transactional_limit": "earlier physical removals cannot be rolled back"}
        if applied:
            task_timeline.record_event(
                conn, project_id=project_id, backlog_id=backlog_id, task_id=task_id,
                event_type="governance.stale_artifact_cleanup.apply",
                phase="cleanup", event_kind="stale_artifact_cleanup",
                actor="system", status="partial", payload=payload,
            )
            conn.commit()
        return StaleArtifactCleanupError(error, payload)

    task_rows = _fetch_batch_task_rows(conn, project_id)
    task_meta_by_id = {str(row.get("task_id") or ""): row.get("metadata") or {} for row in task_rows}

    for candidate_id in sorted(requested):
        candidate = by_id[candidate_id]
        fresh = build_stale_artifact_cleanup_projection(
            conn, project_id, repo_root_path=repo_root_path,
            include_unowned=True, dimension=dim,
        )
        fresh_item = next((item for item in fresh["candidates"]
                           if item["candidate_id"] == candidate_id), None)
        if (fresh_item is None or fresh_item.get("safe_to_apply") is not True
                or fresh_item.get("path") != candidate.get("path")
                or fresh_item.get("snapshot_id") != candidate.get("snapshot_id")
                or fresh_item.get("evidence") != candidate.get("evidence")
                or not str(candidate.get("path") or "")
                or _path_identity(str(candidate["path"])) !=
                    (candidate.get("evidence") or {}).get("path_identity")):
            raise partial_refusal("stale_cleanup_item_drift_refused")
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
            try:
                removal = _remove_worktree(
                    repo_root_path=root,
                    path=str(candidate.get("path") or ""),
                    metadata=metadata,
                    remove_branch=remove_branch,
                )
            except StaleArtifactCleanupError as exc:
                raise partial_refusal(str(exc)) from exc
            cleanup_record["result"] = removal
            for terminal_task_id in terminal_task_ids:
                _append_task_cleanup_history(conn, str(terminal_task_id), cleanup_record=cleanup_record)
            applied.append({**candidate, "result": removal})
        elif candidate["action"] == ACTION_CLEAR_BACKLOG_WORKTREE_REFERENCE:
            backlog_ref = str(candidate.get("backlog_id") or "")
            _clear_backlog_worktree_reference(conn, backlog_ref, cleanup_record=cleanup_record)
            applied.append({**candidate, "result": {"cleared": True, "backlog_id": backlog_ref}})
        elif candidate["action"] == ACTION_REMOVE_STALE_GRAPH_SNAPSHOT:
            snap_path = str(candidate.get("path") or "")
            snap_id = str(candidate.get("snapshot_id") or "")
            result_detail: dict[str, Any] = {"snapshot_id": snap_id, "path": snap_path}
            if snap_path and Path(snap_path).exists():
                try:
                    shutil.rmtree(snap_path)
                    result_detail["removed"] = True
                except OSError as exc:
                    result_detail["removed"] = False
                    result_detail["error"] = str(exc)
            else:
                result_detail["removed"] = False
                result_detail["reason"] = "dir_already_missing"
            cleanup_record["result"] = result_detail
            applied.append({**candidate, "result": result_detail})

    timeline_event = task_timeline.record_event(
        conn,
        project_id=project_id,
        backlog_id=backlog_id,
        task_id=task_id,
        event_type="governance.stale_artifact_cleanup.apply",
        phase="cleanup",
        event_kind="stale_artifact_cleanup",
        actor="system",
        status="applied",
        payload={
            "cleanup_id": cleanup_id,
            "requested_actor": actor,
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
    dimension: str = "",
    plan_hash: str = "",
    plan_revision: int = 0,
) -> dict[str, Any]:
    """Apply only a fresh, exact plan while serializing destructive work."""
    from .db import sqlite_write_lock
    from .server import _CURRENT_FULL_BUILD_KEYS_LOCK

    with _DESTRUCTIVE_CLEANUP_LOCK, sqlite_write_lock(), _CURRENT_FULL_BUILD_KEYS_LOCK:
        return _apply_stale_artifact_cleanup_locked(
            conn, project_id, repo_root_path=repo_root_path,
            candidate_ids=candidate_ids, actor=actor, backlog_id=backlog_id,
            task_id=task_id, reason=reason, remove_branch=remove_branch,
            dimension=dimension, plan_hash=plan_hash, plan_revision=plan_revision,
        )
