"""Dry-run and guarded apply workflow for stale governance artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
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
ARCHIVE_PLAN_REVISION = 1
ARCHIVE_ROOT_ENV = "AMING_CLAW_WORKTREE_ARCHIVE_ROOT"
PREVIEW_LIMIT = 160
_DESTRUCTIVE_CLEANUP_LOCK = RLock()
_CLEANUP_HTTP_MAX_BYTES = 224 * 1024
_CLEANUP_MCP_FRAME_TARGET_BYTES = 224 * 1024
_CLEANUP_MCP_ID_JSON_MAX_BYTES = 4 * 1024
_SNAPSHOT_SIZE_MAX_ENTRIES_PER_DIR = 4_096
_SNAPSHOT_SIZE_MAX_ENTRIES_PER_PREVIEW = 50_000


def _open_directory_chain_nofollow(path: Path, flags: int) -> int:
    """Anchor every absolute path component without traversing a symlink."""
    absolute = path.absolute()
    directory_fd = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            child_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        return directory_fd
    except OSError:
        os.close(directory_fd)
        raise


def _snapshot_directory_size(
    path: str, *, remaining_entries: list[int],
) -> tuple[int | None, str]:
    """Stat a snapshot tree without following links or reading file contents.

    A fixed entry budget makes the same static inventory produce the same
    estimate; elapsed execution time never enters a candidate or plan hash.
    This is apparent file bytes per directory entry (hard links count twice),
    not a claim about physically reclaimable storage.
    """
    target = Path(path).absolute()
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        return None, "nofollow_unavailable"
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        parent_fd = _open_directory_chain_nofollow(target.parent, flags)
    except OSError:
        return None, "parent_unverified"
    try:
        try:
            root_stat = os.stat(target.name, dir_fd=parent_fd,
                                follow_symlinks=False)
        except FileNotFoundError:
            return 0, "verified_absent"
        except OSError:
            return None, "unreadable"
        if not stat.S_ISDIR(root_stat.st_mode):
            return None, "path_unverified"
        try:
            root_fd = os.open(target.name, flags, dir_fd=parent_fd)
        except OSError:
            return None, "path_unverified"
    finally:
        os.close(parent_fd)
    total = 0
    seen = 0
    pending: list[int] = []
    try:
        pending.append(root_fd)
        opened_root = os.fstat(root_fd)
        if ((opened_root.st_dev, opened_root.st_ino)
                != (root_stat.st_dev, root_stat.st_ino)):
            return None, "path_changed"
        while pending:
            directory_fd = pending.pop()
            try:
                with os.scandir(directory_fd) as entries:
                    for entry in entries:
                        seen += 1
                        remaining_entries[0] -= 1
                        if (seen > _SNAPSHOT_SIZE_MAX_ENTRIES_PER_DIR
                                or remaining_entries[0] < 0):
                            return None, "entry_budget_exceeded"
                        child = os.stat(entry.name, dir_fd=directory_fd,
                                        follow_symlinks=False)
                        if stat.S_ISREG(child.st_mode):
                            total += child.st_size
                        elif stat.S_ISDIR(child.st_mode):
                            child_fd = os.open(entry.name, flags,
                                               dir_fd=directory_fd)
                            # Own the descriptor before fstat: a failed fstat
                            # must still be closed by the common finalizer.
                            pending.append(child_fd)
                            opened = os.fstat(child_fd)
                            if ((opened.st_dev, opened.st_ino)
                                    != (child.st_dev, child.st_ino)):
                                return None, "path_changed"
                        else:
                            return None, "nonregular_or_symlink_entry"
            finally:
                os.close(directory_fd)
        try:
            verification_parent_fd = _open_directory_chain_nofollow(
                target.parent, flags,
            )
            try:
                final_stat = os.stat(target.name, dir_fd=verification_parent_fd,
                                     follow_symlinks=False)
            finally:
                os.close(verification_parent_fd)
        except OSError:
            return None, "path_changed"
        if (not stat.S_ISDIR(final_stat.st_mode)
                or final_stat.st_dev != root_stat.st_dev
                or final_stat.st_ino != root_stat.st_ino):
            return None, "path_changed"
        return total, "measured"
    except OSError:
        return None, "unreadable"
    finally:
        for directory_fd in pending:
            os.close(directory_fd)


def cleanup_response_wire_bytes(result: dict[str, Any]) -> dict[str, int]:
    """Measure the real HTTP and double-encoded MCP cleanup result shapes.

    The managed MCP entry point admits IDs whose JSON encoding is at most
    4 KiB. A maximal ASCII ID has the same encoded size as any admitted ID;
    its value cannot reduce the final frame's byte count.
    """

    # The HTTP handler appends its fixed-width req-<12 hex> identity after the
    # cleanup handler returns; include that final field in the measurement.
    http_body = json.dumps({**result, "request_id": "req-" + "x" * 12},
                           ensure_ascii=False).encode("utf-8")
    # Both cleanup MCP adapters embed this exact compact JSON text inside a
    # second JSON-RPC frame. Pretty-print whitespace would make a representable
    # source-shaped inventory exceed the bounded outer frame.
    text_body = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    maximal_id = "x" * (_CLEANUP_MCP_ID_JSON_MAX_BYTES - 2)
    frame = {
        "jsonrpc": "2.0", "id": maximal_id,
        "result": {"content": [{"type": "text", "text": text_body}]},
    }
    mcp_frame = json.dumps(
        frame, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return {"http": len(http_body), "managed_mcp": len(mcp_frame),
            "standalone_mcp": len(mcp_frame)}


def _cleanup_response_fits(result: dict[str, Any]) -> bool:
    sizes = cleanup_response_wire_bytes(result)
    return (sizes["http"] <= _CLEANUP_HTTP_MAX_BYTES
            and sizes["managed_mcp"] <= _CLEANUP_MCP_FRAME_TARGET_BYTES
            and sizes["standalone_mcp"] <= _CLEANUP_MCP_FRAME_TARGET_BYTES)


def _bounded_cleanup_projection(result: dict[str, Any]) -> dict[str, Any]:
    if _cleanup_response_fits(result):
        return result
    retained = result.get("append_only_retained")
    if isinstance(retained, dict) and (
        retained.get("graph_query_traces") or retained.get("graph_trace_ids")
    ):
        trace_ids = list(retained.get("graph_trace_ids") or [])
        count = int((result.get("summary") or {}).get(
            "append_only_graph_trace_count") or len(trace_ids))
        digest = hashlib.sha256(json.dumps(
            trace_ids, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        result = {**result, "append_only_retained": {
            **retained, "graph_query_traces": [], "graph_trace_ids": [],
            "graph_query_traces_truncated": count > 0,
            "graph_query_traces_omitted_count": count,
            "graph_trace_ids_sha256": "sha256:" + digest,
        }}
        if _cleanup_response_fits(result):
            return result
    # The complete semantic candidates have already been hashed. A protected
    # graph row is not executable; its path and physical diagnostics are
    # optional display fields, whereas its exact identity and refusal are not.
    compacted = 0
    visible_candidates = []
    for item in result.get("candidates") or []:
        if (item.get("artifact_type") == "graph_snapshot_dir"
                and item.get("safe_to_apply") is False):
            evidence = item.get("evidence") or {}
            preserved_evidence = {
                key: evidence[key] for key in (
                    "snapshot_kind", "status", "created_at", "age_days",
                    "size_bytes", "size_bytes_status", "run_id",
                    "snapshot_id", "commit_sha",
                ) if key in evidence
            }
            visible_candidates.append({
                key: value for key, value in item.items()
                if key not in {"path", "evidence", "details"}
            } | {"evidence": preserved_evidence})
            compacted += 1
        else:
            visible_candidates.append(item)
    if compacted:
        result = {**result, "candidates": visible_candidates,
                  "candidate_diagnostics": {
                      "graph_unsafe_rows_compacted": compacted,
                      "path_and_physical_evidence_omitted": True,
                  }}
        if _cleanup_response_fits(result):
            return result
    refusal = {
        "ok": False, "error": "cleanup_response_identity_overflow",
        "mode": "dry_run", "dry_run": True,
        "dimension": result.get("dimension"),
        "plan_hash": result.get("plan_hash"),
        "plan_revision": result.get("plan_revision"),
        "apply_plan_available": False, "candidates": [],
        "writes_performed": False, "safe_retry": False,
    }
    if not _cleanup_response_fits(refusal):
        # Even a caller-controlled dimension must not enlarge the refusal.
        refusal.pop("dimension", None)
    return refusal


def bounded_cleanup_error_payload(
    payload: dict[str, Any], *, apply: bool,
) -> dict[str, Any]:
    """Bound a cleanup refusal while preserving known physical-write facts."""
    if _cleanup_response_fits(payload):
        return payload
    encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    written = payload.get("writes_performed")
    if type(written) is not bool:
        written = None if apply else False
    ids = payload.get("applied_candidate_ids")
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        ids = []
    count = payload.get("applied_count")
    if type(count) is not int or count < 0:
        count = len(ids) if ids else None
    refusal = {
        "ok": False, "error": "cleanup_response_frame_refused",
        "writes_performed": written,
        "write_disposition": (
            "written" if written is True else "not_written"
            if written is False else "ambiguous"
        ),
        "safe_retry": False,
        "applied_count": count,
        "applied_candidate_ids": ids,
        "diagnostic_sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
    }
    if not _cleanup_response_fits(refusal):
        refusal["applied_candidate_ids_sha256"] = "sha256:" + hashlib.sha256(
            json.dumps(ids, ensure_ascii=False).encode("utf-8"),
        ).hexdigest()
        refusal.pop("applied_candidate_ids")
    return refusal

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
        # The selector's legacy size walk is unbounded and can follow child
        # symlinks. This preview measures every selected row itself below.
        selection = select_snapshot_retention_candidates(
            conn, project_id, measure_sizes=False,
        )
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
    # One deterministic read budget covers both eligible and protected rows.
    remaining_size_entries = [_SNAPSHOT_SIZE_MAX_ENTRIES_PER_PREVIEW]
    for item in selection.get("candidates", []):
        sid = str(item.get("snapshot_id") or "")
        from .graph_snapshot_store import _snapshot_root, _snapshot_id_is_component
        valid_id = _snapshot_id_is_component(sid)
        actual_path = str(_snapshot_root(project_id, sid)) if valid_id else ""
        exists = bool(item.get("dir_exists"))
        status = str(item.get("status") or "")
        identity = _path_identity(actual_path)
        size_bytes, size_status = _snapshot_directory_size(
            actual_path, remaining_entries=remaining_size_entries,
        ) if valid_id else (None, "snapshot_id_path_invalid")
        safe = bool(exists and valid_id and item.get("in_db")
                    and status == "superseded"
                    and selection.get("reference_authority_complete") is True
                    and fence.get("clear") is True
                    and identity is not None)
        refusal_reasons: list[str] = []
        if not exists:
            refusal_reasons.append("snapshot_dir_missing")
        if not sid:
            refusal_reasons.append("snapshot_id_empty")
        elif not valid_id:
            refusal_reasons.append("snapshot_id_path_invalid")
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
                "size_bytes": size_bytes,
                "size_bytes_status": size_status,
                "in_db": bool(item.get("in_db")),
                "exists": exists,
                "path_identity": identity,
                "append_only_evidence_retained": True,
            },
        })
    # Also surface protected as refusals (informational). Read-only estimates
    # use one deterministic entry budget for the complete graph preview.
    for item in sorted(selection.get("protected", []),
                       key=lambda row: str(row.get("snapshot_id") or "")):
        sid = str(item.get("snapshot_id") or "")
        from .graph_snapshot_store import _snapshot_root, _snapshot_id_is_component
        valid_id = _snapshot_id_is_component(sid)
        actual_path = str(_snapshot_root(project_id, sid)) if valid_id else ""
        size_bytes, size_status = _snapshot_directory_size(
            actual_path, remaining_entries=remaining_size_entries,
        ) if valid_id else (None, "snapshot_id_path_invalid")
        protection_reasons = [f"protected:{r}" for r in
                              (item.get("reasons") or ["protected"])]
        if not valid_id:
            protection_reasons.append("snapshot_id_path_invalid")
        candidates.append({
            "candidate_id": _candidate_id("graph_snapshot_protected", sid),
            "artifact_type": "graph_snapshot_dir",
            "action": ACTION_REMOVE_STALE_GRAPH_SNAPSHOT,
            "snapshot_id": sid,
            "path": actual_path,
            "safe_to_apply": False,
            "refusal_reasons": protection_reasons,
            "evidence": {
                "snapshot_kind": str(item.get("snapshot_kind") or ""),
                "status": str(item.get("status") or ""),
                "created_at": str(item.get("created_at") or ""),
                "size_bytes": size_bytes,
                "size_bytes_status": size_status,
                "protected": True,
                "exists": bool(item.get("dir_exists")),
                "path_identity": _path_identity(actual_path),
                "append_only_evidence_retained": True,
            },
        })
    return candidates


def _candidate_size_totals(items: list[dict[str, Any]]) -> tuple[int, int]:
    known = 0
    unknown = 0
    for item in items:
        size = (item.get("evidence") or {}).get("size_bytes")
        if type(size) is int and size >= 0:
            known += size
        else:
            unknown += 1
    return known, unknown


def build_stale_artifact_cleanup_projection(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    repo_root_path: str | Path,
    include_unowned: bool = True,
    dimension: str = "",
    response_budget: bool = True,
    archive_enrichment: bool = True,
) -> dict[str, Any]:
    """Return a dry-run projection; no artifacts or append-only evidence are deleted.

    A named dimension scopes the bounded preview. Omission includes all four
    dimensions; derived caches remain preview-only until Stage B.
    """
    dim = _dimension(dimension)
    if dim in {DIMENSION_GOVERNANCE_INDEX, DIMENSION_STATE_RECONCILE}:
        derived, truncated, total, total_bytes = _derived_preview(project_id, dim)
        result = {
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
        return _bounded_cleanup_projection(result) if response_budget else result
    if dim == DIMENSION_GRAPH_SNAPSHOTS:
        graph_items = _build_graph_snapshot_candidates(conn, project_id)
        total = len(graph_items)
        visible = graph_items[:PREVIEW_LIMIT]
        safe = sum(item.get("safe_to_apply") is True for item in graph_items)
        total_bytes, unknown_sizes = _candidate_size_totals(graph_items)
        result = {
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
                        "size_bytes": total_bytes if not unknown_sizes else None,
                        "known_size_bytes": total_bytes,
                        "unknown_size_count": unknown_sizes,
                        "size_bytes_complete": unknown_sizes == 0,
                        "truncated": total > PREVIEW_LIMIT,
                        "dimensions": {dim: {"count": total, "visible_count": len(visible),
                                             "safe_count": safe,
                                             "size_bytes": total_bytes if not unknown_sizes else None,
                                             "known_size_bytes": total_bytes,
                                             "unknown_size_count": unknown_sizes,
                                             "size_bytes_complete": unknown_sizes == 0,
                                             "refused_count": total - safe,
                                             "truncated": total > PREVIEW_LIMIT}}},
            "candidates": visible,
            "append_only_retained": {"policy": "retain_append_only_evidence", "deleted": False},
            "cleanup": cleanup_recommendation(project_id),
        }
        return _bounded_cleanup_projection(result) if response_budget else result

    root = batch_jobs.repo_root(repo_root_path)
    stale_report = batch_jobs.report_stale_worktrees(conn, project_id, repo_root_path=root)
    stale_paths = {_resolve_path(path) for path in stale_report.get("stale_worktrees", [])}
    preview_paths = set(stale_paths)
    if archive_enrichment:
        # Registered active checkouts are protection facts too. Include them
        # as refused candidates instead of silently omitting them from the
        # worktree preview.
        try:
            preview_paths.update(
                path for path in _registered_worktree_paths(root)
                if _path_under_worktrees(root, path)
            )
        except StaleArtifactCleanupError:
            pass
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
    for path in sorted(preview_paths):
        terminal_rows = terminal_tasks_by_path.get(path, [])
        active_rows = active_tasks_by_path.get(path, [])
        active_backlog_rows = active_backlog_refs_by_path.get(path, [])
        path_safe = _path_under_worktrees(root, path)
        identity = _path_identity(path)
        clean_worktree = _worktree_clean(path) if path_safe and identity else False
        legacy_safe = bool(
            path_safe and identity and clean_worktree and terminal_rows
            and len(terminal_rows) <= PREVIEW_LIMIT
            and not active_rows and not active_backlog_rows
        )
        # Stage A could delete a clean terminal worktree without merge or
        # restore proof. Its public apply must no longer be a bypass.
        safe = False
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
        if legacy_safe:
            refusal_reasons.append("archive_and_restore_proof_required")
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
                        "An active backlog still references this path."
                        if active_backlog_rows else
                        "Configure an external archive volume and resolve every "
                        "listed protection reason before requesting guarded cleanup."
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

    if archive_enrichment:
        archive = _archive_root_descriptor()
        candidates = [
            _archive_candidate(conn, project_id, root, item, archive)
            if item.get("artifact_type") == "batch_worktree" else item
            for item in candidates
        ]

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
    total_bytes, unknown_sizes = _candidate_size_totals(all_candidates)
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
        known_member_bytes, unknown_member_sizes = _candidate_size_totals(members)
        byte_count = derived_counts[key][1] if key in derived_counts else known_member_bytes
        safe_count = sum(item.get("safe_to_apply") is True for item in members)
        dimension_summary[key] = {
            "count": count, "visible_count": min(len(members), PREVIEW_LIMIT),
            "safe_count": safe_count, "refused_count": count - safe_count,
            "size_bytes": byte_count if not unknown_member_sizes else None,
            "known_size_bytes": byte_count,
            "unknown_size_count": unknown_member_sizes,
            "size_bytes_complete": unknown_member_sizes == 0,
            "truncated": truncated,
        }
    all_candidates = all_candidates[:PREVIEW_LIMIT]
    for key, values in dimension_summary.items():
        values["visible_count"] = sum(in_dimension(item, key) for item in all_candidates)
        values["truncated"] = values["count"] > values["visible_count"]

    safe_count = sum(value["safe_count"] for value in dimension_summary.values())
    unsafe_count = sum(value["refused_count"] for value in dimension_summary.values())
    result = {
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
            "size_bytes": total_bytes if not unknown_sizes else None,
            "known_size_bytes": total_bytes,
            "unknown_size_count": unknown_sizes,
            "size_bytes_complete": unknown_sizes == 0,
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
    return _bounded_cleanup_projection(result) if response_budget else result


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
        response_budget=False,
    )
    if _bounded_cleanup_projection(dict(projection)).get("apply_plan_available") is False:
        raise StaleArtifactCleanupError("cleanup_response_identity_overflow", {
            "ok": False, "error": "cleanup_response_identity_overflow",
            "apply_plan_available": False, "writes_performed": False,
            "safe_retry": False,
        })
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

    archive_items = [by_id[item] for item in requested
                     if by_id[item].get("artifact_type") == "batch_worktree"]
    if archive_items:
        if len(requested) != 1 or len(archive_items) != 1 or remove_branch:
            raise StaleArtifactCleanupError("single_archive_candidate_required", {
                "ok": False, "error": "single_archive_candidate_required",
                "writes_performed": False,
                "next_step": "Select one merged worktree and keep its branch for recovery.",
            })
        candidate_id = str(archive_items[0]["candidate_id"])
        try:
            published = archive_merged_worktree(
                conn, project_id, repo_root_path=repo_root_path,
                candidate_id=candidate_id, plan_hash=plan_hash,
                plan_revision=plan_revision,
            )
        except StaleArtifactCleanupError as exc:
            raise StaleArtifactCleanupError("archive_before_prune_refused", {
                "ok": False, "error": "archive_before_prune_refused",
                "cause": str(exc), "candidate_id": candidate_id,
                "source_removed": False, "writes_performed": None,
                "write_disposition": "ambiguous", "safe_retry": False,
                "next_step": "Keep the worktree and inspect the archive index before retrying.",
            }) from exc
        try:
            pruned = prune_archived_merged_worktree(
                conn, project_id, repo_root_path=repo_root_path,
                candidate_id=candidate_id, plan_hash=plan_hash,
                plan_revision=plan_revision, generation=published["generation"],
            )
        except StaleArtifactCleanupError as exc:
            raise StaleArtifactCleanupError("archive_only_prune_refused", {
                "ok": False, "error": "archive_only_prune_refused",
                "cause": str(exc), "state": "archive_only_or_partial_uncertain",
                "candidate_id": candidate_id,
                "generation": published["generation"],
                "source_path_present": Path(str(archive_items[0].get("path") or "")).exists(),
                "writes_performed": True, "safe_retry": False,
                "next_step": "Read the archive index and source path before retrying prune.",
            }) from exc
        return {"ok": pruned["ok"], "mode": "apply", "dry_run": False,
                "project_id": project_id, "applied_count": 1,
                "applied_candidate_ids": [candidate_id],
                "state": pruned["state"], "generation": published["generation"],
                "archive": published, "prune": pruned,
                "writes_performed": True}

    root = batch_jobs.repo_root(repo_root_path)
    cleanup_id = f"stale-cleanup-{uuid.uuid4().hex[:12]}"
    applied: list[dict[str, Any]] = []
    public_applied = [
        {"candidate_id": candidate_id, "action": by_id[candidate_id]["action"]}
        for candidate_id in sorted(requested)
    ]
    public_retained = dict(projection.get("append_only_retained") or {})

    def public_result_template(retained: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True, "mode": "apply", "dry_run": False,
            "project_id": project_id, "cleanup_id": cleanup_id,
            "applied_count": len(public_applied),
            "applied_candidate_ids": sorted(requested),
            "applied": public_applied,
            "timeline_event": {"id": 99999999999999999999},
            "append_only_retained": retained,
        }

    if not _cleanup_response_fits(public_result_template(public_retained)):
        trace_ids = list(public_retained.get("graph_trace_ids") or [])
        count = int(projection["summary"].get("append_only_graph_trace_count")
                    or len(trace_ids))
        public_retained.update({
            "graph_query_traces": [], "graph_trace_ids": [],
            "graph_query_traces_truncated": count > 0,
            "graph_query_traces_omitted_count": count,
            "graph_trace_ids_sha256": "sha256:" + hashlib.sha256(
                json.dumps(trace_ids, ensure_ascii=False,
                           separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        })
    partial_template = {
        "ok": False, "error": "e" * 80,
        "cleanup_id": cleanup_id,
        "applied_count": len(public_applied),
        "applied_candidate_ids": sorted(requested),
        "diagnostic_sha256": "sha256:" + "0" * 64,
        "transactional_limit": "earlier physical removals cannot be rolled back",
        "writes_performed": True,
    }
    if (not _cleanup_response_fits(public_result_template(public_retained))
            or not _cleanup_response_fits(partial_template)):
        raise StaleArtifactCleanupError("cleanup_response_identity_overflow", {
            "ok": False, "error": "cleanup_response_identity_overflow",
            "apply_plan_available": False, "writes_performed": False,
            "safe_retry": False,
        })

    def partial_refusal(error: str) -> StaleArtifactCleanupError:
        code = (error if len(error) <= 80 and error.replace("_", "").isalnum()
                else "stale_cleanup_item_error")
        payload = {"ok": False, "error": code, "cleanup_id": cleanup_id,
                   "applied_count": len(applied),
                   "applied_candidate_ids": [item["candidate_id"] for item in applied],
                   "diagnostic_sha256": "sha256:" + hashlib.sha256(
                       error.encode("utf-8", errors="replace")
                   ).hexdigest(),
                   "writes_performed": bool(applied),
                   "transactional_limit": "earlier physical removals cannot be rolled back"}
        if applied:
            task_timeline.record_event(
                conn, project_id=project_id, backlog_id=backlog_id, task_id=task_id,
                event_type="governance.stale_artifact_cleanup.apply",
                phase="cleanup", event_kind="stale_artifact_cleanup",
                actor="system", status="partial", payload=payload,
            )
            conn.commit()
        return StaleArtifactCleanupError(code, payload)

    task_rows = _fetch_batch_task_rows(conn, project_id)
    task_meta_by_id = {str(row.get("task_id") or ""): row.get("metadata") or {} for row in task_rows}

    for candidate_id in sorted(requested):
        candidate = by_id[candidate_id]
        fresh = build_stale_artifact_cleanup_projection(
            conn, project_id, repo_root_path=repo_root_path,
            include_unowned=True, dimension=dim, response_budget=False,
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
        "applied_candidate_ids": [item["candidate_id"] for item in applied],
        "applied": public_applied,
        "timeline_event": {"id": timeline_event["id"]},
        "append_only_retained": public_retained,
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


def _archive_root_descriptor() -> dict[str, Any]:
    """Bind the configured external volume, including its mount and inode."""
    configured = os.environ.get(ARCHIVE_ROOT_ENV, "").strip()
    if not configured or not Path(configured).is_absolute():
        return {"verified": False, "reason": "archive_root_not_configured"}
    path = Path(configured)
    identity = _path_identity(str(path))
    if identity is None:
        return {"verified": False, "reason": "archive_root_unavailable_or_symlink"}
    try:
        mount = path
        while not os.path.ismount(mount):
            if mount == mount.parent:
                break
            mount = mount.parent
        mount_info = mount.stat()
        usage = shutil.disk_usage(path)
        if mount == Path(path.anchor) or not os.access(path, os.W_OK | os.X_OK):
            return {"verified": False, "reason": "archive_volume_or_acl_unverified"}
        return {"verified": True, "path": str(path), "identity": identity,
                "mount": str(mount), "mount_device": mount_info.st_dev,
                "mount_inode": mount_info.st_ino, "free_bytes": usage.free}
    except OSError:
        return {"verified": False, "reason": "archive_volume_unavailable"}


def _archive_root_matches(expected: dict[str, Any], *, bytes_needed: int = 0) -> bool:
    fresh = _archive_root_descriptor()
    return bool(expected.get("verified") and fresh.get("verified")
                and all(fresh.get(key) == expected.get(key) for key in
                        ("path", "identity", "mount", "mount_device", "mount_inode"))
                and int(fresh.get("free_bytes") or 0) >= bytes_needed)


def _archive_reference_reasons(
    conn: sqlite3.Connection, *, path: str, branch: str, head: str,
    task_id: str, project_id: str,
) -> list[str]:
    """Conservatively scan present governance tables for protected references.

    Unknown schemas, unreadable rows and unbounded scans refuse. The owning
    terminal task is the sole allowed reference; every other exact identity
    mention is protected until a more specific authority can prove otherwise.
    """
    reasons: set[str] = set()
    try:
        names = [str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        if len(names) > 512:
            return ["reference_inventory_unbounded"]
        tokens = (path, branch, head, task_id)
        scanned = 0
        for name in names:
            # Names come from SQLite schema, but quote defensively.
            quoted = '"' + name.replace('"', '""') + '"'
            cursor = conn.execute(f"SELECT * FROM {quoted}")
            columns = [item[0] for item in cursor.description]
            while rows := cursor.fetchmany(256):
                scanned += len(rows)
                if scanned > 250_000:
                    return ["reference_inventory_unbounded"]
                for row in rows:
                    record = dict(zip(columns, row))
                    if name == "tasks" and str(record.get("task_id") or "") == task_id:
                        continue
                    if name == "backlog_bugs" and (
                        str(record.get("worktree_path") or "") == path
                        and str(record.get("status") or "").upper() in TERMINAL_BACKLOG_STATUSES
                        and not str(record.get("current_task_id") or "")
                    ):
                        continue
                    # Do not treat a project-wide row as a reference. Search
                    # exact path/branch/commit and task identifier in payloads.
                    body = "\n".join(str(value) for value in record.values()
                                     if isinstance(value, (str, bytes)))
                    if any(token and token in body for token in tokens):
                        reasons.add("referenced_by_" + name)
                        if len(reasons) >= 16:
                            return sorted(reasons)
    except (sqlite3.Error, ValueError):
        return ["reference_inventory_unavailable"]
    return sorted(reasons)


def _archive_candidate(
    conn: sqlite3.Connection, project_id: str, root: Path,
    item: dict[str, Any], archive: dict[str, Any],
) -> dict[str, Any]:
    evidence = item.get("evidence") or {}
    terminal_ids = evidence.get("terminal_task_ids") or []
    reasons = [reason for reason in item.get("refusal_reasons") or []
               if reason != "archive_and_restore_proof_required"]
    git: dict[str, Any] = {"verified": False, "reason": "task_identity_unknown"}
    task_id = str(terminal_ids[0]) if len(terminal_ids) == 1 else ""
    metadata: dict[str, Any] = {}
    if not task_id:
        reasons.append("single_terminal_task_required")
    else:
        row = next((row for row in _fetch_batch_task_rows(conn, project_id)
                    if row.get("task_id") == task_id), None)
        metadata = row.get("metadata") or {} if row else {}
        if not row or str(row.get("batch_status") or "") not in {"merged", "redeployed"}:
            reasons.append("terminal_merged_task_required")
        branch = str(metadata.get("work_branch") or "")
        target = str(metadata.get("target_branch") or "")
        if _resolve_path(metadata.get("worktree_path")) != item.get("path"):
            reasons.append("task_worktree_identity_mismatch")
        try:
            git = batch_jobs.merged_worktree_git_identity(
                root, str(item.get("path") or ""), work_branch=branch,
                target_branch=target,
            )
        except (OSError, ValueError, batch_jobs.BatchJobError):
            pass
        if git.get("verified") is not True:
            reasons.append(str(git.get("reason") or "git_identity_unknown"))
        else:
            reasons.extend(_archive_reference_reasons(
                conn, path=str(item["path"]), branch=branch,
                head=str(git["head"]), task_id=task_id,
                project_id=project_id,
            ))
    if archive.get("verified") is not True:
        reasons.append(str(archive.get("reason") or "archive_root_unknown"))
    size, size_status = _snapshot_directory_size(
        str(item.get("path") or ""), remaining_entries=[50_000],
    ) if evidence.get("path_identity") else (None, "path_identity_unverified")
    if size_status != "measured":
        reasons.append("source_size_" + size_status)
    if size is not None and archive.get("verified"):
        # A full bundle and independent cold clone need additional space.
        needed = max(64 * 1024 * 1024, size * 4)
        if int(archive.get("free_bytes") or 0) < needed:
            reasons.append("archive_capacity_insufficient")
    result = {**item, "safe_to_apply": not reasons,
              "refusal_reasons": sorted(set(reasons)),
              "action": "archive_merged_worktree_then_guarded_prune",
              "evidence": {**evidence, "task_id": task_id,
                           "branch": git.get("branch", ""),
                           "head": git.get("head", ""),
                           "tree": git.get("tree", ""),
                           "merge_target": git.get("target_branch", ""),
                           "merge_target_head": git.get("target_head", ""),
                           "git_common_dir": git.get("git_common_dir", ""),
                           "size_bytes": size, "size_bytes_status": size_status,
                           "archive_root": {key: value for key, value in archive.items()
                                            if key != "free_bytes"}}}
    return result


def build_merged_worktree_archive_projection(
    conn: sqlite3.Connection, project_id: str, *, repo_root_path: str | Path,
) -> dict[str, Any]:
    root = batch_jobs.repo_root(repo_root_path)
    base = build_stale_artifact_cleanup_projection(
        conn, project_id, repo_root_path=root, dimension=DIMENSION_WORKTREES,
        response_budget=False, archive_enrichment=False,
    )
    archive = _archive_root_descriptor()
    items = [_archive_candidate(conn, project_id, root, item, archive)
             for item in base["candidates"]
             if item.get("artifact_type") == "batch_worktree"]
    result = {"ok": True, "mode": "dry_run", "dry_run": True,
              "project_id": project_id, "dimension": "merged_worktree_archive",
              "plan_revision": ARCHIVE_PLAN_REVISION,
              "plan_hash": _plan_hash(project_id, "merged_worktree_archive", items),
              "summary": {"candidate_count": len(items),
                          "safe_apply_count": sum(i["safe_to_apply"] for i in items),
                          "truncated": bool(base["summary"].get("truncated"))},
              "archive_root": archive, "candidates": items,
              "next_step": "Archive one eligible candidate, verify its bundle, then request guarded prune."}
    return _bounded_cleanup_projection(result)


def _archive_json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _archive_fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _registered_worktree_paths(root: Path) -> list[str]:
    try:
        output = subprocess.run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain", "-z"],
            capture_output=True, timeout=30, check=True,
        ).stdout.decode("utf-8", errors="strict")
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise StaleArtifactCleanupError("protected_worktree_inventory_unavailable") from exc
    return [field[9:] for field in output.split("\0") if field.startswith("worktree ")]


def _registered_protected_worktrees(root: Path, excluded: Path) -> dict[str, dict[str, int]]:
    """Capture physical identities of every other registered checkout."""
    result: dict[str, dict[str, int]] = {}
    for raw_path in _registered_worktree_paths(root):
        path = Path(raw_path)
        if path == excluded:
            continue
        identity = _path_identity(str(path))
        if identity is None:
            raise StaleArtifactCleanupError("protected_worktree_identity_unverified")
        result[str(path)] = identity
        if len(result) > 1000:
            raise StaleArtifactCleanupError("protected_worktree_inventory_unbounded")
    return result


def _archive_governance_evidence_readback(conn: sqlite3.Connection) -> dict[str, Any]:
    """Read protected database, sidecar, graph and timeline facts only."""
    database = ""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            database = str(row[2] or "")
            break
    paths: dict[str, dict[str, int] | None] = {}
    if database:
        for suffix in ("", "-wal", "-shm"):
            file = Path(database + suffix)
            try:
                info = file.lstat()
            except FileNotFoundError:
                paths[suffix or "database"] = None
            else:
                if not stat.S_ISREG(info.st_mode):
                    raise StaleArtifactCleanupError("governance_database_identity_unverified")
                paths[suffix or "database"] = {
                    "device": info.st_dev, "inode": info.st_ino,
                    "size": info.st_size,
                }
    counts = {}
    for table in ("graph_query_traces", "graph_snapshots",
                  "contract_runtime_executions", "task_timeline_events"):
        if _table_exists(conn, table):
            counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return {"database_path": database, "paths": paths, "counts": counts}


def _archive_write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _archive_fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _archive_file_digest(path: Path) -> tuple[int, str]:
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            sha.update(chunk)
            size += len(chunk)
    return size, "sha256:" + sha.hexdigest()


def _archive_source_members(path: Path) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    for parent, dirs, files in os.walk(path, followlinks=False):
        if Path(parent) == path and ".git" in dirs:
            dirs.remove(".git")
        dirs.sort()
        files.sort()
        for name in dirs:
            child = Path(parent) / name
            if child.is_symlink() or not child.is_dir():
                raise StaleArtifactCleanupError("archive_source_nonregular_entry")
        for name in files:
            child = Path(parent) / name
            relative = child.relative_to(path).as_posix()
            if relative == ".git":
                continue
            info = child.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise StaleArtifactCleanupError("archive_source_nonregular_entry")
            size, digest = _archive_file_digest(child)
            if size != info.st_size or child.lstat().st_mtime_ns != info.st_mtime_ns:
                raise StaleArtifactCleanupError("archive_source_drift")
            members.append({"path": relative, "size": size,
                            "mtime_ns": info.st_mtime_ns,
                            "mode": stat.S_IMODE(info.st_mode),
                            "sha256": digest})
            if len(members) > 50_000:
                raise StaleArtifactCleanupError("archive_member_budget_exceeded")
    return members


def _archive_verify_members(root: Path, members: list[dict[str, Any]]) -> None:
    actual = _archive_source_members(root)
    # Cold clones intentionally have new mtimes. Compare paths, bytes and
    # hashes; mtimes are recorded and checked for the archived copy itself.
    expected_content = [(m["path"], m["size"], m["mode"], m["sha256"]) for m in members]
    actual_content = [(m["path"], m["size"], m["mode"], m["sha256"]) for m in actual]
    if actual_content != expected_content:
        raise StaleArtifactCleanupError("archive_member_hash_mismatch")


def _archive_cold_restore(bundle: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Verify both copied members and self-contained Git objects in isolation."""
    data_root = bundle / "members"
    members = manifest["members"]
    _archive_verify_members(data_root, members)
    with tempfile.TemporaryDirectory(prefix="ac-archive-restore-") as temp:
        checkout = Path(temp) / "checkout"
        cloned = subprocess.run(
            ["git", "clone", "--quiet", "--no-checkout", str(bundle / "objects.bundle"),
             str(checkout)], capture_output=True, text=True, timeout=120, check=False,
        )
        if cloned.returncode:
            raise StaleArtifactCleanupError("archive_git_objects_unrestorable")
        checked = subprocess.run(
            ["git", "-C", str(checkout), "checkout", "--quiet", "--detach",
             manifest["head"]], capture_output=True, text=True, timeout=120,
            check=False,
        )
        if checked.returncode:
            raise StaleArtifactCleanupError("archive_git_checkout_unrestorable")
        head = batch_jobs.git_commit(checkout)
        tree = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        if head != manifest["head"] or tree != manifest["tree"]:
            raise StaleArtifactCleanupError("archive_git_identity_mismatch")
        _archive_verify_members(checkout, members)
    return {"verified": True, "head": manifest["head"],
            "tree": manifest["tree"], "member_count": len(members)}


def _archive_index_path(archive_root: Path, project_id: str, candidate_id: str) -> Path:
    if (not project_id or not candidate_id
            or any(part in {"", ".", ".."} for part in (project_id, candidate_id))
            or any("/" in part or "\\" in part for part in (project_id, candidate_id))):
        raise StaleArtifactCleanupError("archive_identity_invalid")
    return archive_root / project_id / "merged-worktrees" / "index" / (candidate_id + ".json")


def _archive_load_index(index_path: Path) -> dict[str, Any] | None:
    if not index_path.exists():
        return None
    if index_path.is_symlink() or index_path.stat().st_size > 65536:
        raise StaleArtifactCleanupError("archive_index_unverified")
    try:
        value = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise StaleArtifactCleanupError("archive_index_unverified") from exc
    if not isinstance(value, dict):
        raise StaleArtifactCleanupError("archive_index_unverified")
    return value


def _archive_resolve_published(
    archive_root: Path, project_id: str, candidate_id: str,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    index_path = _archive_index_path(archive_root, project_id, candidate_id)
    index = _archive_load_index(index_path)
    if not index or index.get("candidate_id") != candidate_id:
        raise StaleArtifactCleanupError("published_archive_index_missing")
    generation = str(index.get("generation") or "")
    if not re.fullmatch(r"gen-[0-9a-f]{32}", generation):
        raise StaleArtifactCleanupError("published_archive_generation_invalid")
    bundle = archive_root / project_id / "merged-worktrees" / "bundles" / generation
    if (bundle.is_symlink() or not bundle.is_dir()
            or bundle.parent != index_path.parent.parent / "bundles"):
        raise StaleArtifactCleanupError("published_archive_bundle_missing")
    manifest_path = bundle / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise StaleArtifactCleanupError("published_archive_manifest_missing")
    raw = manifest_path.read_bytes()
    if "sha256:" + hashlib.sha256(raw).hexdigest() != index.get("manifest_sha256"):
        raise StaleArtifactCleanupError("published_archive_manifest_hash_mismatch")
    manifest = json.loads(raw)
    if manifest.get("candidate_id") != candidate_id or manifest.get("project_id") != project_id:
        raise StaleArtifactCleanupError("published_archive_manifest_identity_mismatch")
    return bundle, manifest, index


def _archive_fresh_candidate(
    conn: sqlite3.Connection, project_id: str, root: Path,
    *, candidate_id: str, plan_hash: str, plan_revision: int,
) -> dict[str, Any]:
    projection = None
    for dimension in ("merged_worktree_archive", DIMENSION_WORKTREES, DIMENSION_ALL):
        current = (build_merged_worktree_archive_projection(
            conn, project_id, repo_root_path=root,
        ) if dimension == "merged_worktree_archive" else
            build_stale_artifact_cleanup_projection(
                conn, project_id, repo_root_path=root, dimension=dimension,
                response_budget=False,
            ))
        if (current.get("plan_hash") == plan_hash
                and current.get("apply_plan_available") is not False
                and _bounded_cleanup_projection(dict(current)).get("apply_plan_available") is not False
                and not (current.get("summary") or {}).get("truncated")):
            projection = current
            break
    if plan_revision != ARCHIVE_PLAN_REVISION or projection is None:
        raise StaleArtifactCleanupError("stale_archive_plan_refused")
    candidate = next((item for item in projection["candidates"]
                      if item["candidate_id"] == candidate_id), None)
    if candidate is None or candidate.get("safe_to_apply") is not True:
        raise StaleArtifactCleanupError("archive_candidate_ineligible", {
            "ok": False, "error": "archive_candidate_ineligible",
            "refusal_reasons": candidate.get("refusal_reasons") if candidate else ["candidate_not_found"],
            "writes_performed": False,
            "next_step": "Resolve the protection reason and request a fresh preview.",
        })
    return candidate


def archive_merged_worktree(
    conn: sqlite3.Connection, project_id: str, *, repo_root_path: str | Path,
    candidate_id: str, plan_hash: str, plan_revision: int,
) -> dict[str, Any]:
    """Publish a portable verified bundle and durable project index first."""
    from .db import sqlite_write_lock
    from .server import _CURRENT_FULL_BUILD_KEYS_LOCK

    with _DESTRUCTIVE_CLEANUP_LOCK, sqlite_write_lock(), _CURRENT_FULL_BUILD_KEYS_LOCK:
        root = batch_jobs.repo_root(repo_root_path)
        candidate = _archive_fresh_candidate(
            conn, project_id, root, candidate_id=candidate_id,
            plan_hash=plan_hash, plan_revision=plan_revision,
        )
        evidence = candidate["evidence"]
        source = Path(candidate["path"])
        archive_desc = evidence["archive_root"]
        size = int(evidence["size_bytes"])
        if not _archive_root_matches(
            archive_desc, bytes_needed=max(64 * 1024 * 1024, size * 4),
        ):
            raise StaleArtifactCleanupError("archive_volume_capacity_or_identity_drift")
        archive_root = Path(archive_desc["path"])
        index_path = _archive_index_path(archive_root, project_id, candidate_id)
        existing = _archive_load_index(index_path)
        if existing:
            bundle, manifest, index = _archive_resolve_published(
                archive_root, project_id, candidate_id,
            )
            if manifest.get("plan_hash") != plan_hash or index.get("state") not in {"archive_only", "pruned"}:
                raise StaleArtifactCleanupError("archive_index_conflict")
            proof = _archive_cold_restore(bundle, manifest)
            return {"ok": True, "state": index["state"], "replay": True,
                    "candidate_id": candidate_id, "generation": index["generation"],
                    "restore_proof": proof, "writes_performed": False}

        workspace = archive_root / project_id / "merged-worktrees"
        staging_parent = workspace / "staging"
        bundle_parent = workspace / "bundles"
        # Every created path is under the configured archive volume. Source
        # remains physically unchanged through archive publication.
        for directory in (workspace.parent, workspace, staging_parent,
                          bundle_parent, index_path.parent):
            directory.mkdir(exist_ok=True)
            if directory.is_symlink():
                raise StaleArtifactCleanupError("archive_directory_symlink_refused")
        generation = "gen-" + uuid.uuid4().hex
        staging = staging_parent / generation
        bundle = bundle_parent / generation
        staging.mkdir(mode=0o700)
        try:
            members = _archive_source_members(source)
            if (_path_identity(str(source)) != evidence["path_identity"]
                    or not _worktree_clean(str(source))):
                raise StaleArtifactCleanupError("archive_source_drift")
            data_root = staging / "members"
            data_root.mkdir()
            for member in members:
                relative = Path(member["path"])
                destination = data_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with (source / relative).open("rb") as read, destination.open("xb") as write:
                    shutil.copyfileobj(read, write, length=1024 * 1024)
                    write.flush()
                    os.fsync(write.fileno())
                os.chmod(destination, member["mode"])
                os.utime(destination, ns=(member["mtime_ns"], member["mtime_ns"]))
                if _archive_file_digest(destination) != (member["size"], member["sha256"]):
                    raise StaleArtifactCleanupError("archive_copy_hash_mismatch")
            dirs_to_sync = [Path(parent) for parent, _dirs, _files in os.walk(data_root)]
            for directory in reversed(dirs_to_sync):
                _archive_fsync_directory(directory)
            bundle_result = subprocess.run(
                ["git", "-C", str(root), "bundle", "create",
                 str(staging / "objects.bundle"),
                 "refs/heads/" + evidence["branch"]],
                capture_output=True, text=True, timeout=120, check=False,
            )
            if bundle_result.returncode:
                raise StaleArtifactCleanupError("archive_git_bundle_failed")
            with (staging / "objects.bundle").open("rb") as handle:
                os.fsync(handle.fileno())
            manifest = {
                "schema_version": "merged_worktree_archive.v1",
                "project_id": project_id, "candidate_id": candidate_id,
                "task_id": evidence["task_id"], "branch": evidence["branch"],
                "head": evidence["head"], "tree": evidence["tree"],
                "merge_target": evidence["merge_target"],
                "merge_target_head": evidence["merge_target_head"],
                "source_path": str(source), "source_identity": evidence["path_identity"],
                "archive_root": archive_desc, "plan_hash": plan_hash,
                "plan_revision": plan_revision, "generation": generation,
                "rebuild": {"method": "git_bundle_clone_detached_checkout",
                            "object_bundle": "objects.bundle", "commit": evidence["head"]},
                "members": members,
            }
            _archive_write_atomic(staging / "manifest.json", _archive_json_bytes(manifest))
            _archive_cold_restore(staging, manifest)
            if not _archive_root_matches(archive_desc):
                raise StaleArtifactCleanupError("archive_volume_identity_drift")
            _archive_fsync_directory(staging)
            os.replace(staging, bundle)
            _archive_fsync_directory(bundle_parent)
            # The bundle is independently restorable before the index grants
            # any prune authority. An index failure leaves source untouched.
            proof = _archive_cold_restore(bundle, manifest)
            index = {"schema_version": "merged_worktree_archive_index.v1",
                     "project_id": project_id, "candidate_id": candidate_id,
                     "generation": generation, "state": "archive_only",
                     "plan_hash": plan_hash, "plan_revision": plan_revision,
                     "archive_root": archive_desc,
                     "manifest_sha256": "sha256:" + hashlib.sha256(
                         (bundle / "manifest.json").read_bytes()).hexdigest(),
                     "published_at": _utc_now(), "restore_proof": proof}
            _archive_write_atomic(index_path, _archive_json_bytes(index))
            return {"ok": True, "state": "archive_only", "replay": False,
                    "candidate_id": candidate_id, "generation": generation,
                    "restore_proof": proof, "writes_performed": True}
        except (OSError, subprocess.SubprocessError) as exc:
            raise StaleArtifactCleanupError("archive_publish_failed") from exc


def prune_archived_merged_worktree(
    conn: sqlite3.Connection, project_id: str, *, repo_root_path: str | Path,
    candidate_id: str, plan_hash: str, plan_revision: int, generation: str,
) -> dict[str, Any]:
    """Recheck all authority under the destructive lock, then remove once."""
    from .db import sqlite_write_lock
    from .server import _CURRENT_FULL_BUILD_KEYS_LOCK

    with _DESTRUCTIVE_CLEANUP_LOCK, sqlite_write_lock(), _CURRENT_FULL_BUILD_KEYS_LOCK:
        root = batch_jobs.repo_root(repo_root_path)
        archive_desc = _archive_root_descriptor()
        if not archive_desc.get("verified"):
            raise StaleArtifactCleanupError("archive_volume_unavailable")
        archive_root = Path(archive_desc["path"])
        bundle, manifest, index = _archive_resolve_published(
            archive_root, project_id, candidate_id,
        )
        if (index.get("generation") != generation
                or manifest.get("plan_hash") != plan_hash
                or manifest.get("plan_revision") != plan_revision
                or not _archive_root_matches(manifest["archive_root"])):
            raise StaleArtifactCleanupError("archive_prune_authority_mismatch")
        if index.get("state") == "pruned":
            proof = _archive_cold_restore(bundle, manifest)
            if Path(manifest["source_path"]).exists():
                raise StaleArtifactCleanupError("archive_replay_source_reappeared")
            return {"ok": True, "state": "pruned", "replay": True,
                    "candidate_id": candidate_id, "generation": generation,
                    "restore_proof": proof, "writes_performed": False}
        if index.get("state") != "archive_only":
            raise StaleArtifactCleanupError("archive_prune_state_uncertain")
        source = Path(manifest["source_path"])
        if not source.exists():
            # Crash after physical removal but before the index receipt. Do
            # not repeat removal; keep the uncertainty visible for recovery.
            index["state"] = "partial_uncertain"
            _archive_write_atomic(_archive_index_path(archive_root, project_id, candidate_id),
                                  _archive_json_bytes(index))
            raise StaleArtifactCleanupError("archive_prune_partial_uncertain")
        candidate = _archive_fresh_candidate(
            conn, project_id, root, candidate_id=candidate_id,
            plan_hash=plan_hash, plan_revision=plan_revision,
        )
        evidence = candidate["evidence"]
        if (candidate["path"] != manifest["source_path"]
                or evidence["path_identity"] != manifest["source_identity"]
                or evidence["head"] != manifest["head"]
                or evidence["tree"] != manifest["tree"]
                or evidence["branch"] != manifest["branch"]
                or evidence["merge_target_head"] != manifest["merge_target_head"]):
            raise StaleArtifactCleanupError("archive_prune_source_drift")
        proof = _archive_cold_restore(bundle, manifest)
        protected_before = _registered_protected_worktrees(root, source)
        governance_before = _archive_governance_evidence_readback(conn)
        free_before = shutil.disk_usage(source).free
        # Existing helper uses the non-forced Git worktree command. Branch
        # removal is forbidden for this archive protocol.
        removal = _remove_worktree(
            repo_root_path=root, path=str(source),
            metadata={"work_branch": evidence["branch"],
                      "worktree_path": str(source)}, remove_branch=False,
        )
        if not removal.get("removed"):
            raise StaleArtifactCleanupError("archive_prune_removal_uncertain")
        index["state"] = "pruned"
        index["pruned_at"] = _utc_now()
        index["free_space_delta_bytes"] = shutil.disk_usage(source.parent).free - free_before
        index["post_prune_restore_proof"] = _archive_cold_restore(bundle, manifest)
        index["source_absent"] = not source.exists()
        index["source_registration_absent"] = str(source) not in _registered_worktree_paths(root)
        index["protected_worktrees_unchanged"] = all(
            _path_identity(path) == identity for path, identity in protected_before.items()
        )
        governance_after = _archive_governance_evidence_readback(conn)
        index["governance_evidence_readback"] = governance_after
        index["governance_evidence_preserved"] = (
            governance_before["database_path"] == governance_after["database_path"]
            and all(
                (governance_before["paths"].get("database") or {}).get(key)
                == (governance_after["paths"].get("database") or {}).get(key)
                for key in ("device", "inode")
            )
            and all(governance_after["counts"].get(name, -1) >= count
                    for name, count in governance_before["counts"].items())
        )
        index["archive_readback"] = bool((bundle / "manifest.json").is_file())
        if (not index["source_absent"] or not index["source_registration_absent"]
                or not index["protected_worktrees_unchanged"]
                or not index["governance_evidence_preserved"]
                or not index["archive_readback"]):
            index["state"] = "partial_uncertain"
        _archive_write_atomic(_archive_index_path(archive_root, project_id, candidate_id),
                              _archive_json_bytes(index))
        task_timeline.record_event(
            conn, project_id=project_id, task_id=manifest["task_id"],
            event_type="governance.stale_artifact_cleanup.apply",
            phase="cleanup", event_kind="stale_artifact_cleanup",
            actor="system", status="applied" if index["state"] == "pruned" else "partial",
            payload={"candidate_id": candidate_id, "generation": generation,
                     "archive_protocol": "merged_worktree_archive.v1",
                     "head": manifest["head"], "tree": manifest["tree"],
                     "index_state": index["state"]},
        )
        conn.commit()
        return {"ok": index["state"] == "pruned", "state": index["state"],
                "replay": False, "candidate_id": candidate_id,
                "generation": generation, "restore_proof": proof,
                "free_space_delta_bytes": index["free_space_delta_bytes"],
                "writes_performed": True}
