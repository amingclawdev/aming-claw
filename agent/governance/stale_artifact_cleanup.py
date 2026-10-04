"""Dry-run and guarded apply workflow for stale governance artifacts."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from contextlib import closing, contextmanager, nullcontext
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
DIMENSION_GRAPH_SNAPSHOT_DUPLICATES = "graph_snapshot_duplicates"
DIMENSION_GOVERNANCE_INDEX = "governance_index"
DIMENSION_STATE_RECONCILE = "state_reconcile"
DIMENSION_ALL = "all"
ALL_DIMENSIONS = {
    DIMENSION_WORKTREES, DIMENSION_GRAPH_SNAPSHOTS,
    DIMENSION_GOVERNANCE_INDEX, DIMENSION_STATE_RECONCILE, DIMENSION_ALL,
    DIMENSION_GRAPH_SNAPSHOT_DUPLICATES,
}
PLAN_REVISION = 1
DERIVED_PAIR_PLAN_REVISION = 2
ARCHIVE_PLAN_REVISION = 1
ARCHIVE_ROOT_ENV = "AMING_CLAW_WORKTREE_ARCHIVE_ROOT"
# A missing store cannot prove absence of a session, lease, CEX, or
# unique-evidence reference. A business project's local queue is the one
# jointly optional pair, while AC uses its verified stable queue. This
# inventory is intentionally explicit: newly added stores are still scanned
# when present, and schema changes to required stores fail closed.
_ARCHIVE_REFERENCE_COLUMNS: dict[str, frozenset[str]] = {
    "sessions": frozenset({"session_id", "project_id", "status", "scope_json", "metadata_json"}),
    "observer_sessions": frozenset({"session_id", "project_id", "status", "cwd", "capabilities_json"}),
    "tasks": frozenset({"task_id", "project_id", "status", "metadata_json"}),
    "backlog_bugs": frozenset({"bug_id", "status", "worktree_path", "worktree_branch",
                               "current_task_id", "root_task_id", "takeover_json"}),
    "task_attempts": frozenset({"task_id", "status", "result_json"}),
    "task_timeline_events": frozenset({"task_id", "backlog_id", "payload_json", "artifact_refs_json"}),
    "graph_query_traces": frozenset({"task_id", "parent_task_id", "runtime_context_id", "status"}),
    "contract_runtime_executions": frozenset({"contract_execution_id", "project_id",
                                                "backlog_id", "record_json"}),
    "parallel_branch_runtime_contexts": frozenset({"task_id", "runtime_context_id",
                                                    "worktree_path", "branch_ref", "lease_id",
                                                    "lease_expires_at", "status"}),
    "parallel_branch_merge_queue_items": frozenset({"task_id", "branch_ref", "branch_head", "status"}),
    "parallel_branch_batch_items": frozenset({"task_id", "worktree_path", "branch_ref", "status"}),
    "observer_command_queue": frozenset({"payload_json", "target_session_id", "status"}),
    "ai_output_queue": frozenset({"target_id", "lease_token", "lease_expires_at", "status"}),
    "reconcile_sessions": frozenset({"session_id", "snapshot_path", "status"}),
    "session_context": frozenset({"task_id", "content", "metadata_json"}),
    "release_operator_head_queue": frozenset({"project_id", "backlog_id", "position"}),
    "release_operator_head_queue_events": frozenset({"backlog_id", "before_json", "after_json"}),
    "managed_ref_contexts": frozenset({"ref_name", "ref_head_commit", "status", "evidence_json"}),
}
_STABLE_QUEUE_REFERENCE_TABLES = frozenset({
    "release_operator_head_queue", "release_operator_head_queue_events",
})
_STABLE_QUEUE_FENCE_PROTOCOL = "ac_stable_release_queue_flock.v1"
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
                    "snapshot_id", "commit_sha", "global_refusal_reasons",
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
               "plan_revision": (DERIVED_PAIR_PLAN_REVISION if any(
                   item.get("artifact_type") == "derived_run_pair" for item in candidates
               ) else PLAN_REVISION),
               "candidates": items}
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


def _derived_pair_candidate_id(run_id: str) -> str:
    return _candidate_id("derived_run_pair", run_id)


def _derived_tree_facts(path: Path) -> dict[str, Any]:
    """Bounded, no-follow inventory; logical bytes never stand in for reclaimed bytes."""
    members = _archive_source_members(path)
    allocated = 0
    logical = 0
    member_allocated: dict[str, int] = {}
    seen: set[tuple[int, int]] = set()
    for member in members:
        info = (path / member["path"]).lstat()
        logical += info.st_size
        member_allocated[member["path"]] = info.st_blocks * 512
        key = (info.st_dev, info.st_ino)
        if key not in seen:
            allocated += info.st_blocks * 512
            seen.add(key)
    return {"members": members, "logical_bytes": logical,
            "member_allocated_bytes": member_allocated,
            "allocated_bytes": allocated, "identity": _path_identity(str(path))}


def _derived_rebuild_source_reason(provenance: dict[str, Any], commit: str) -> str:
    source = Path(str(provenance.get("project_root") or ""))
    if (_path_identity(str(source)) is None or not (source / ".git").exists()
            or not re.fullmatch(r"[0-9a-f]{40}", commit)):
        return "derived_rebuild_source_unavailable"
    try:
        head = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30, check=False)
        clean = subprocess.run(["git", "-C", str(source), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "derived_rebuild_source_unavailable"
    if (head.returncode or head.stdout.strip() != commit
            or clean.returncode or clean.stdout.strip()):
        return "derived_rebuild_source_commit_drift"
    return ""


def _derived_exact_reference_reasons(
    conn: sqlite3.Connection, project_id: str, run_id: str,
    snapshot_id: str, source_paths: dict[str, str],
) -> list[str]:
    """Check live exact identities. Historical event text is retained evidence."""
    reasons = _archive_reference_inventory_reasons(conn, project_id=project_id)
    if reasons:
        return reasons
    if project_id == "aming-claw":
        _stable_facts, stable_reasons = _stable_release_queue_reference_facts(
            tokens=(run_id, snapshot_id, *source_paths.values()),
        )
        reasons.extend(reason for reason in stable_reasons
                       if reason != "referenced_by_stable_release_operator_head_queue_events")
        if reasons:
            return sorted(set(reasons))
    if not _table_exists(conn, "graph_snapshots"):
        return ["reference_inventory_missing:graph_snapshots"]
    try:
        for required in ("graph_snapshot_refs", "pending_scope_reconcile",
                         "graph_current_full_build_claim_history"):
            if not _table_exists(conn, required):
                return ["reference_inventory_missing:" + required]
        rows = conn.execute(
            "SELECT status FROM graph_snapshots WHERE project_id = ? AND snapshot_id = ?",
            (project_id, snapshot_id),
        ).fetchall()
        if len(rows) != 1:
            return ["snapshot_identity_missing_or_ambiguous"]
        if str(rows[0][0] or "").lower() not in {"superseded", "retired"}:
            reasons.append("protected:snapshot_not_terminal")
        if conn.execute(
            "SELECT 1 FROM graph_snapshot_refs WHERE project_id=? AND snapshot_id=? LIMIT 1",
            (project_id, snapshot_id),
        ).fetchone():
            reasons.append("protected:current_graph_ref")
        newest = conn.execute(
            "SELECT snapshot_id FROM graph_snapshots WHERE project_id=? "
            "ORDER BY created_at DESC, snapshot_id DESC LIMIT 1", (project_id,),
        ).fetchone()
        if newest and newest[0] == snapshot_id:
            reasons.append("protected:newest_snapshot")
        if conn.execute(
            "SELECT 1 FROM pending_scope_reconcile WHERE project_id=? AND snapshot_id=? "
            "AND lower(status) NOT IN ('complete','failed','cancelled','superseded') LIMIT 1",
            (project_id, snapshot_id),
        ).fetchone():
            reasons.append("protected:nonterminal_reconcile")
        if conn.execute(
            "SELECT 1 FROM graph_current_full_build_claim_history "
            "WHERE project_id=? AND (snapshot_id=? OR run_id=?) AND status='active' LIMIT 1",
            (project_id, snapshot_id, run_id),
        ).fetchone():
            reasons.append("protected:active_build_claim")
        # Project and commit alone occur in normal history and are deliberately
        # absent from these tokens. Only an exact live run/path/snapshot link
        # can prevent reclaim after the archive preserves historical evidence.
        tokens = {run_id, snapshot_id, *source_paths.values()}
        history = {"task_timeline_events",
                   "release_operator_head_queue_events"}
        scanned = 0
        for table in sorted(_ARCHIVE_REFERENCE_COLUMNS):
            if (table in history
                    or (table in _STABLE_QUEUE_REFERENCE_TABLES
                        and (project_id == "aming-claw" or not _table_exists(conn, table)))):
                continue
            quoted = '"' + table.replace('"', '""') + '"'
            cursor = conn.execute(f"SELECT * FROM {quoted}")
            cols = [column[0] for column in cursor.description]
            while chunk := cursor.fetchmany(256):
                scanned += len(chunk)
                if scanned > 250_000:
                    return ["reference_inventory_unbounded"]
                for row in chunk:
                    record = dict(zip(cols, row))
                    if table == "session_context":
                        task_id = str(record.get("task_id") or "")
                        task = conn.execute("SELECT status FROM tasks WHERE task_id=? LIMIT 1",
                                            (task_id,)).fetchone()
                        if task is None:
                            return ["reference_inventory_unknown:session_context.task"]
                        if str(task[0] or "").lower() in {
                            "complete", "completed", "closed", "done", "failed",
                            "cancelled", "abandoned", "superseded", "merged", "fixed",
                        }:
                            continue
                    if table == "contract_runtime_executions":
                        try:
                            contract = _json_dict(record.get("record_json"))
                            guide = contract.get("runtime_guide")
                        except (TypeError, ValueError):
                            return ["reference_inventory_corrupt:contract_runtime_executions.record_json"]
                        if not isinstance(guide, dict) or "next_legal_action" not in guide:
                            return ["reference_inventory_schema_incomplete:contract_runtime_executions.runtime_guide"]
                        if guide["next_legal_action"] is None:
                            continue
                    status = str(record.get("status") or "").lower()
                    if status in {"complete", "completed", "closed", "done", "failed",
                                  "cancelled", "abandoned", "superseded", "merged", "fixed",
                                  "succeeded", "success", "ok"}:
                        continue
                    for key, value in record.items():
                        if key.endswith("_json") and value not in (None, "", b""):
                            try:
                                json.loads(value)
                            except (TypeError, ValueError, UnicodeError):
                                return ["reference_inventory_corrupt:" + table + "." + key]
                    values = [str(value) for value in record.values() if isinstance(value, (str, bytes))]
                    if any(token == value or token in value for token in tokens for value in values):
                        reasons.append("referenced_by_" + table)
                        break
        return sorted(set(reasons))
    except sqlite3.Error:
        return ["reference_inventory_unavailable"]


def _derived_pair_preview(
    conn: sqlite3.Connection, project_id: str,
) -> tuple[list[dict[str, Any]], bool, int, int]:
    """One canonical candidate per run, shared by both named views and all."""
    from .db import _governance_root

    base = _governance_root() / project_id
    roots = {DIMENSION_GOVERNANCE_INDEX: base / "governance-index",
             DIMENSION_STATE_RECONCILE: base / "state-reconcile"}
    names: set[str] = set()
    refusal: list[str] = []
    for kind, root in roots.items():
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            refusal.append("derived_root_unreadable_or_symlink:" + kind)
            continue
        if root.exists():
            try:
                children = list(root.iterdir())
            except OSError:
                refusal.append("derived_root_unreadable:" + kind)
                continue
            if len(children) > PREVIEW_LIMIT:
                return [], True, len(children), 0
            names.update(child.name for child in children)
    if refusal:
        return [{"candidate_id": _derived_pair_candidate_id("inventory_error"),
                 "artifact_type": "derived_run_pair", "action": "archive_derived_run_pair",
                 "safe_to_apply": False, "refusal_reasons": refusal,
                 "evidence": {"project_id": project_id}}], False, 1, 0
    if len(names) > PREVIEW_LIMIT:
        return [], True, len(names), 0
    archive_root = _archive_root_descriptor()
    source_readiness: dict[tuple[str, str], str] = {}
    candidates: list[dict[str, Any]] = []
    total_bytes = 0
    for run_id in sorted(names):
        paths = {kind: root / run_id for kind, root in roots.items()}
        reasons: list[str] = []
        facts: dict[str, Any] = {}
        for kind, path in paths.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id) or run_id in {".", ".."}:
                reasons.append("run_id_path_invalid")
                break
            if not path.exists() or path.is_symlink() or not path.is_dir():
                reasons.append("missing_or_unpaired:" + kind)
                if path.is_symlink():
                    reasons.append("derived_path_symlink_refused:" + kind)
                continue
            try:
                facts[kind] = _derived_tree_facts(path)
                if facts[kind]["identity"] is None:
                    reasons.append("source_path_identity_unverified:" + kind)
            except (OSError, StaleArtifactCleanupError):
                reasons.append("source_unreadable_or_unbounded:" + kind)
        index_summary: dict[str, Any] = {}
        trace_summary: dict[str, Any] = {}
        provenance: dict[str, Any] = {}
        if len(facts) == 2:
            try:
                index_summary = _json_dict((paths[DIMENSION_GOVERNANCE_INDEX] / "summary.json").read_text())
                trace_summary = _json_dict((paths[DIMENSION_STATE_RECONCILE] / "trace/summary.json").read_text())
                provenance = _json_dict((paths[DIMENSION_STATE_RECONCILE] / "trace/derived-rebuild.json").read_text())
            except (OSError, ValueError, UnicodeError):
                reasons.append("legacy_missing_rebuild_provenance")
        snapshot_id = str(trace_summary.get("snapshot_id") or "")
        commit = str(index_summary.get("commit_sha") or "")
        if len(facts) == 2:
            if (index_summary.get("run_id") != run_id or trace_summary.get("run_id") != run_id
                    or not snapshot_id or index_summary.get("active_snapshot_id") != snapshot_id
                    or not commit or provenance.get("run_id") != run_id
                    or provenance.get("snapshot_id") != snapshot_id
                    or provenance.get("commit_sha") != commit):
                reasons.append("pair_identity_mismatch")
            if trace_summary.get("status") != "ok":
                reasons.append("run_not_terminal")
            if provenance.get("recipe") != "state_only_full_reconcile.v1":
                reasons.append("rebuild_recipe_unsupported")
            else:
                source_key = (str(provenance.get("project_root") or ""), commit)
                if source_key not in source_readiness:
                    source_readiness[source_key] = _derived_rebuild_source_reason(
                        provenance, commit,
                    )
                if source_readiness[source_key]:
                    reasons.append(source_readiness[source_key])
            reasons.extend(_derived_exact_reference_reasons(
                conn, project_id, run_id, snapshot_id,
                {kind: str(path) for kind, path in paths.items()},
            ))
        if not archive_root.get("verified"):
            reasons.append(str(archive_root.get("reason") or "archive_root_unverified"))
        logical = sum(value["logical_bytes"] for value in facts.values())
        allocated = sum(value["allocated_bytes"] for value in facts.values())
        total_bytes += logical
        if archive_root.get("verified") and int(archive_root.get("free_bytes") or 0) < logical * 2:
            reasons.append("archive_capacity_insufficient")
        candidates.append({
            "candidate_id": _derived_pair_candidate_id(run_id),
            "artifact_type": "derived_run_pair", "action": "archive_derived_run_pair",
            "path": str(paths[DIMENSION_GOVERNANCE_INDEX]),
            "source_paths": {kind: str(path) for kind, path in paths.items()},
            "safe_to_apply": not reasons, "refusal_reasons": sorted(set(reasons)),
            "evidence": {"project_id": project_id, "run_id": run_id,
                         "paired": all(path.exists() and path.is_dir() and not path.is_symlink()
                                       for path in paths.values()),
                         "snapshot_id": snapshot_id, "commit_sha": commit,
                         "status": str(trace_summary.get("status") or "unknown"),
                         "size_bytes": logical, "logical_bytes": logical,
                         "allocated_bytes": allocated,
                         "source_volume": {kind: value["identity"] for kind, value in facts.items()},
                         "archive_volume": {key: value for key, value in archive_root.items()
                                            if key != "free_bytes"},
                         "rebuild_recipe": provenance.get("recipe") or "unsupported"},
        })
    return candidates, False, len(candidates), total_bytes


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
    _phase_timing: Any = None,
) -> list[dict[str, Any]]:
    """Return graph-snapshot dimension candidates in the same conservative model as worktrees."""
    from .graph_snapshot_store import select_snapshot_retention_candidates
    from .server import _graph_release_build_fence_state
    phase = _phase_timing or (lambda _name: nullcontext())
    try:
        # The selector's legacy size walk is unbounded and can follow child
        # symlinks. This preview measures every selected row itself below.
        with phase("selection"):
            selection = select_snapshot_retention_candidates(
                conn, project_id, measure_sizes=False,
            )
        with phase("build_fence"):
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
    with phase("sizing"):
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
                    **({"global_refusal_reasons": selection["global_refusal_reasons"]}
                       if selection.get("global_refusal_reasons") and not candidates else {}),
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
                    **({"global_refusal_reasons": selection["global_refusal_reasons"]}
                       if selection.get("global_refusal_reasons") and not candidates else {}),
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
    _run_selection: Any = None,
    _phase_timing: Any = None,
) -> dict[str, Any]:
    """Return a dry-run projection; no artifacts or append-only evidence are deleted.

    A named dimension scopes the bounded preview. Omission includes all four
    dimensions; derived caches remain preview-only until Stage B.
    """
    dim = _dimension(dimension)
    if dim == DIMENSION_GRAPH_SNAPSHOT_DUPLICATES:
        from . import snapshot_cow_cleanup as cow
        try:
            result = (_periodic_native_check(conn, project_id, Path(repo_root_path), "preview",
                                              _run_selection=_run_selection)
                      if _run_selection is not None else cow.preview(conn, project_id, Path(repo_root_path)))
        except (cow.CowRefusal, OSError, ValueError, sqlite3.Error) as exc:
            raise StaleArtifactCleanupError("snapshot_cow_preview_refused", {
                "ok": False, "error": "snapshot_cow_preview_refused",
                "refusal_reason": str(exc)[:160], "writes_performed": False,
                "write_disposition": "not_written", "apply_plan_available": False,
                "refusal_metadata": exc.metadata if isinstance(exc, cow.CowRefusal) else {},
            }) from exc
        if not _cleanup_response_fits(result):
            raise StaleArtifactCleanupError("cleanup_response_frame_refused", {
                "ok": False, "error": "cleanup_response_frame_refused",
                "writes_performed": False, "apply_plan_available": False,
            })
        return result
    if dim in {DIMENSION_GOVERNANCE_INDEX, DIMENSION_STATE_RECONCILE}:
        derived, truncated, total, total_bytes = _derived_pair_preview(conn, project_id)
        # An orphaned half is still the canonical pair candidate. Only an
        # entirely empty pair inventory needs the old standalone preview.
        paired_view = bool(derived)
        if not paired_view:
            derived, truncated, total, total_bytes = _derived_preview(project_id, dim)
        safe = sum(item.get("safe_to_apply") is True for item in derived)
        result = {
            "ok": True, "mode": "dry_run", "dry_run": True,
            "project_id": project_id, "dimension": dim,
            "plan_revision": DERIVED_PAIR_PLAN_REVISION if paired_view else PLAN_REVISION,
            "plan_hash": _plan_hash(project_id, dim, derived),
            "summary": {"candidate_count": len(derived), "safe_apply_count": safe,
                        "total_candidate_count": total,
                        "unsafe_candidate_count": total - safe,
                        "size_bytes": total_bytes,
                        "truncated": truncated,
                        "dimensions": {dim: {"count": total, "visible_count": len(derived),
                                             "safe_count": safe, "size_bytes": total_bytes,
                                             "refused_count": total - safe, "truncated": truncated}}},
            "candidates": derived,
            "append_only_retained": {"policy": "retain_append_only_evidence", "deleted": False},
            "cleanup": cleanup_recommendation(project_id),
        }
        return _bounded_cleanup_projection(result) if response_budget else result
    if dim == DIMENSION_GRAPH_SNAPSHOTS:
        graph_items = (_build_graph_snapshot_candidates(conn, project_id, _phase_timing)
                       if _phase_timing else _build_graph_snapshot_candidates(conn, project_id))
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
            "global_refusal_reasons": sorted({reason for item in graph_items
                for reason in (item.get("evidence") or {}).get("global_refusal_reasons", [])}),
            "append_only_retained": {"policy": "retain_append_only_evidence", "deleted": False},
            "cleanup": cleanup_recommendation(project_id),
        }
        return _bounded_cleanup_projection(result) if response_budget else result

    inventory_reasons = _archive_reference_inventory_reasons(conn, project_id=project_id)
    if ("reference_inventory_unavailable" in inventory_reasons
            or any(reason.endswith(":tasks") for reason in inventory_reasons)):
        return {
            "ok": False, "error": "archive_reference_inventory_refused",
            "mode": "dry_run", "dry_run": True, "project_id": project_id,
            "dimension": dim, "plan_revision": PLAN_REVISION,
            "apply_plan_available": False, "candidates": [],
            "refusal_reasons": inventory_reasons,
            "writes_performed": False, "safe_retry": False,
            "next_step": "Restore the required governance reference inventory and request a fresh preview.",
        }

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
        snapshot_candidates = (_build_graph_snapshot_candidates(conn, project_id, _phase_timing)
                       if _phase_timing else _build_graph_snapshot_candidates(conn, project_id))

    # Filter worktree candidates if scoped to snapshots only
    if dim == DIMENSION_GRAPH_SNAPSHOTS:
        all_candidates = snapshot_candidates
    else:
        all_candidates = candidates + snapshot_candidates

    derived_truncated = False
    paired_all = False
    derived_counts: dict[str, tuple[int, int]] = {}
    if dim == DIMENSION_ALL:
        derived, derived_truncated, derived_total, derived_bytes = _derived_pair_preview(conn, project_id)
        paired_all = bool(derived)
        if paired_all:
            all_candidates.extend(derived)
            for derived_dim in (DIMENSION_GOVERNANCE_INDEX, DIMENSION_STATE_RECONCILE):
                derived_counts[derived_dim] = (derived_total, derived_bytes)
        else:
            derived_truncated = False
            for derived_dim in (DIMENSION_GOVERNANCE_INDEX, DIMENSION_STATE_RECONCILE):
                legacy, legacy_truncated, legacy_total, legacy_bytes = _derived_preview(project_id, derived_dim)
                all_candidates.extend(legacy)
                derived_counts[derived_dim] = (legacy_total, legacy_bytes)
                derived_truncated = derived_truncated or legacy_truncated
    total_candidates = len(all_candidates) + sum(
        max(0, count - len([item for item in all_candidates if item.get("artifact_type") in {
            "derived_run_pair", DIMENSION_GOVERNANCE_INDEX}]))
        for key, (count, _bytes) in derived_counts.items() if key == DIMENSION_GOVERNANCE_INDEX
    )
    truncated = derived_truncated or total_candidates > PREVIEW_LIMIT
    total_bytes, unknown_sizes = _candidate_size_totals(all_candidates)
    if not paired_all:
        total_bytes += sum(max(0, byte_count - sum(
            int((item.get("evidence") or {}).get("size_bytes") or 0)
            for item in all_candidates if item.get("artifact_type") == key))
            for key, (_count, byte_count) in derived_counts.items())
    def in_dimension(item: dict[str, Any], key: str) -> bool:
        kind = str(item.get("artifact_type") or "")
        return (kind in {"batch_worktree", "backlog_worktree_reference"}
                if key == DIMENSION_WORKTREES else
                kind == "graph_snapshot_dir" if key == DIMENSION_GRAPH_SNAPSHOTS else
                kind in {"derived_run_pair", key} if key in {
                    DIMENSION_GOVERNANCE_INDEX, DIMENSION_STATE_RECONCILE}
                else kind == key)
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
    if (dim in {DIMENSION_GOVERNANCE_INDEX, DIMENSION_STATE_RECONCILE}
            and len(candidate_ids) == 1 and plan_hash
            and plan_revision == DERIVED_PAIR_PLAN_REVISION):
        replay = _derived_pair_replay(project_id, candidate_ids[0], dim, plan_hash)
        if replay is not None:
            return {"ok": True, "mode": "apply", "dry_run": False,
                    "project_id": project_id, "applied_count": 0,
                    "applied_candidate_ids": [], **replay}
    projection = build_stale_artifact_cleanup_projection(
        conn,
        project_id,
        repo_root_path=repo_root_path,
        include_unowned=True,
        dimension=dim,
        response_budget=False,
    )
    if projection.get("apply_plan_available") is False or projection.get("ok") is False:
        raise StaleArtifactCleanupError("archive_reference_inventory_refused", projection)
    if _bounded_cleanup_projection(dict(projection)).get("apply_plan_available") is False:
        raise StaleArtifactCleanupError("cleanup_response_identity_overflow", {
            "ok": False, "error": "cleanup_response_identity_overflow",
            "apply_plan_available": False, "writes_performed": False,
            "safe_retry": False,
        })
    expected_revision = projection.get("plan_revision", PLAN_REVISION)
    if (type(plan_revision) is not int or plan_revision != expected_revision
            or plan_hash != projection["plan_hash"]
            or projection["summary"].get("truncated")):
        raise StaleArtifactCleanupError("stale_cleanup_plan_refused", {
            "ok": False, "error": "stale_cleanup_plan_refused",
            "current_plan_hash": projection["plan_hash"],
            "current_plan_revision": expected_revision,
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

    derived_items = [by_id[item] for item in requested
                     if by_id[item].get("artifact_type") == "derived_run_pair"]
    if derived_items:
        if len(requested) != 1 or len(derived_items) != 1 or remove_branch:
            raise StaleArtifactCleanupError("single_derived_pair_required", {
                "ok": False, "error": "single_derived_pair_required",
                "writes_performed": False,
            })
        with (_stable_queue_exclusive_fence() if project_id == "aming-claw"
              else nullcontext()):
            receipt = _derived_pair_archive_and_prune(
                conn, project_id, derived_items[0], repo_root_path=repo_root_path,
                dimension=dim, plan_hash=plan_hash, backlog_id=backlog_id,
                task_id=task_id, actor=actor,
            )
        return {"ok": True, "mode": "apply", "dry_run": False,
                "project_id": project_id, "applied_count": 1,
                "applied_candidate_ids": [derived_items[0]["candidate_id"]],
                **receipt}

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
    operation_id: str = "",
    _run_selection: Any = None,
) -> dict[str, Any]:
    """Apply only a fresh, exact plan while serializing destructive work."""
    from .db import sqlite_write_lock
    from .server import _CURRENT_FULL_BUILD_KEYS_LOCK

    with _DESTRUCTIVE_CLEANUP_LOCK, sqlite_write_lock(), _CURRENT_FULL_BUILD_KEYS_LOCK:
        if _dimension(dimension) == DIMENSION_GRAPH_SNAPSHOT_DUPLICATES:
            return _cow_locked(conn, project_id, repo_root_path, "apply",
                               candidate_ids=candidate_ids, plan_hash=plan_hash,
                               plan_revision=plan_revision, operation_id=operation_id,
                               _run_selection=_run_selection)
        return _apply_stale_artifact_cleanup_locked(
            conn, project_id, repo_root_path=repo_root_path,
            candidate_ids=candidate_ids, actor=actor, backlog_id=backlog_id,
            task_id=task_id, reason=reason, remove_branch=remove_branch,
            dimension=dimension, plan_hash=plan_hash, plan_revision=plan_revision,
        )


def _cow_locked(conn: sqlite3.Connection, project_id: str, root: str | Path,
                mode: str, **arguments: Any) -> dict[str, Any]:
    from . import snapshot_cow_cleanup as cow
    owns_transaction = not conn.in_transaction
    # Retain the native service descriptor; hold SQLite's physical writer lock
    # as well as process locks so outside DB writers cannot race the live census.
    try:
        if owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        fence = _stable_queue_exclusive_fence() if project_id == "aming-claw" else nullcontext()
        with fence:
            methods = {"apply": cow.apply, "recover": cow.recover,
                       "resolution": cow.periodic_resolution, "readiness": cow.periodic_journal_readiness,
                       "preview": cow.preview, "zero_effect": cow.periodic_zero_effect}
            result = methods[mode](conn, project_id, Path(root), **arguments)
        if not _cleanup_response_fits(result):
            return bounded_cleanup_error_payload({
                "ok": False, "error": "cleanup_response_frame_refused",
                "operation_id": arguments.get("operation_id"),
                "writes_performed": result.get("writes_performed"),
                "write_disposition": result.get("write_disposition"), "safe_retry": False,
            }, apply=True)
        return result
    except (cow.CowRefusal, OSError, ValueError, sqlite3.Error) as exc:
        raise StaleArtifactCleanupError("snapshot_cow_refused", {
            "ok": False, "error": "snapshot_cow_refused", "refusal_reason": str(exc)[:160],
            "operation_id": arguments.get("operation_id"),
            "native_prejournal_refusal": isinstance(exc, cow.CowPrejournalRefusal),
            "writes_performed": False, "write_disposition": "not_written", "safe_retry": False,
            "refusal_metadata": exc.metadata if isinstance(exc, cow.CowRefusal) else {},
        }) from exc
    finally:
        if owns_transaction and conn.in_transaction:
            conn.rollback()


def recover_snapshot_cow_cleanup(conn: sqlite3.Connection, project_id: str, *,
                                 repo_root_path: str | Path, operation_id: str,
                                 action: str = "inspect") -> dict[str, Any]:
    from .db import sqlite_write_lock
    from .server import _CURRENT_FULL_BUILD_KEYS_LOCK
    with _DESTRUCTIVE_CLEANUP_LOCK, sqlite_write_lock(), _CURRENT_FULL_BUILD_KEYS_LOCK:
        return _cow_locked(conn, project_id, repo_root_path, "recover",
                           operation_id=operation_id, action=action)



def _periodic_native_check(conn: sqlite3.Connection, project_id: str, root: Path,
                           mode: str, **arguments: Any) -> dict[str, Any]:
    if mode not in {"resolution", "readiness", "preview", "zero_effect"}:
        raise ValueError("periodic_native_check_mode_invalid")
    from .db import sqlite_write_lock
    from .server import _CURRENT_FULL_BUILD_KEYS_LOCK
    with _DESTRUCTIVE_CLEANUP_LOCK, sqlite_write_lock(), _CURRENT_FULL_BUILD_KEYS_LOCK:
        return _cow_locked(conn, project_id, root, mode, **arguments)


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


def _archive_reference_inventory_reasons(
    conn: sqlite3.Connection, *, project_id: str = "",
) -> list[str]:
    """A missing or unreadable required store cannot prove reference absence."""
    try:
        names = {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        if len(names) > 512:
            return ["reference_inventory_unbounded"]
        required_tables = set(_ARCHIVE_REFERENCE_COLUMNS)
        if (project_id == "aming-claw"
                or _STABLE_QUEUE_REFERENCE_TABLES.isdisjoint(names)):
            # AC release queue authority lives only in the verified stable DB.
            # A business project's local queue is empty only when both optional
            # tables are absent from this same trusted database snapshot.
            required_tables -= _STABLE_QUEUE_REFERENCE_TABLES
        missing = sorted(required_tables - names)
        if missing:
            return ["reference_inventory_missing:" + name for name in missing]
        for name in sorted(required_tables):
            required = _ARCHIVE_REFERENCE_COLUMNS[name]
            if not required <= _table_columns(conn, name):
                return ["reference_inventory_schema_incomplete:" + name]
            quoted = '"' + name.replace('"', '""') + '"'
            try:
                # Schema/readability probe must not transfer an oversized first
                # runtime payload before the live census can classify it.
                conn.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone()
            except sqlite3.Error:
                return ["reference_inventory_unreadable:" + name]
        if project_id != "aming-claw" and _STABLE_QUEUE_REFERENCE_TABLES <= names:
            # Derived-run scans retain queue events as history, so validate
            # their complete present inventory before skipping them as refs.
            try:
                cursor = conn.execute(
                    "SELECT before_json, after_json FROM release_operator_head_queue_events"
                )
                scanned = 0
                while rows := cursor.fetchmany(256):
                    scanned += len(rows)
                    if scanned > 250_000:
                        return ["reference_inventory_unbounded"]
                    for row in rows:
                        for column, value in zip(("before_json", "after_json"), row):
                            if value not in (None, "", b""):
                                if not isinstance(value, (str, bytes)):
                                    return ["reference_inventory_corrupt:release_operator_head_queue_events." + column]
                                try:
                                    json.loads(value)
                                except (TypeError, ValueError, UnicodeError):
                                    return ["reference_inventory_corrupt:release_operator_head_queue_events." + column]
            except sqlite3.Error:
                return ["reference_inventory_unreadable:release_operator_head_queue_events"]
    except sqlite3.Error:
        return ["reference_inventory_unavailable"]
    return []


def _stable_release_queue_reference_facts(
    *, tokens: tuple[str, ...],
) -> tuple[dict[str, Any], list[str]]:
    """Read AC's release queue from its verified stable authority, never DEV."""
    from . import db

    try:
        binding = db.verified_stable_database_binding()
        if (binding.get("health") or {}).get("ac_release_queue_writer_fence") != _STABLE_QUEUE_FENCE_PROTOCOL:
            return {}, ["stable_queue_writer_fence_unavailable"]
        db._revalidate_stable_database_binding(binding)
        database = Path(str(binding["database_path"]))
        lock_directory_identity = _stable_queue_lock_directory_identity(binding)
        digest = hashlib.sha256()
        count = 0
        scanned_bytes = 0
        references: set[str] = set()
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as stable:
            stable.execute("PRAGMA query_only=ON")
            stable.execute("BEGIN")
            for name in sorted(_STABLE_QUEUE_REFERENCE_TABLES):
                required = _ARCHIVE_REFERENCE_COLUMNS[name] | {"project_id"}
                columns = _table_columns(stable, name)
                if not required <= columns:
                    return {}, ["stable_queue_inventory_missing_or_incomplete:" + name]
                digest.update(name.encode("utf-8"))
                digest.update(json.dumps(sorted(columns)).encode("utf-8"))
                cursor = stable.execute(
                    f'SELECT * FROM "{name}" WHERE project_id=? ORDER BY rowid',
                    (db.AC_PROJECT_ID,),
                )
                field_names = [item[0] for item in cursor.description]
                while rows := cursor.fetchmany(256):
                    for row in rows:
                        count += 1
                        if count > 250_000:
                            return {}, ["stable_queue_inventory_unbounded"]
                        record = dict(zip(field_names, row))
                        for column, value in record.items():
                            if value is not None and not isinstance(value, (str, int, float)):
                                return {}, ["stable_queue_inventory_corrupt:" + name + "." + column]
                            if column.endswith("_json") and value not in (None, ""):
                                try:
                                    json.loads(value)
                                except (TypeError, ValueError, UnicodeError):
                                    return {}, ["stable_queue_inventory_corrupt:" + name + "." + column]
                        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"),
                                             ensure_ascii=False).encode("utf-8")
                        scanned_bytes += len(encoded)
                        if len(encoded) > 1024 * 1024 or scanned_bytes > 16 * 1024 * 1024:
                            return {}, ["stable_queue_inventory_unbounded"]
                        digest.update(encoded)
                        body = "\n".join(str(value) for value in record.values() if value is not None)
                        if any(token and token in body for token in tokens):
                            references.add("referenced_by_stable_" + name)
            stable.execute("COMMIT")
        db._revalidate_stable_database_binding(binding)
        if _stable_queue_lock_directory_identity(binding) != lock_directory_identity:
            return {}, ["stable_queue_lock_directory_identity_drift"]
        return {
            "schema_version": "merged_worktree_stable_queue_facts.v1",
            "stable_database_identity": dict(binding["stable_database_identity"]),
            "lock_directory_identity": lock_directory_identity,
            "stable_head": str(binding["stable_head"]),
            "queue_sha256": "sha256:" + digest.hexdigest(),
            "row_count": count,
        }, sorted(references)
    except (OSError, sqlite3.Error, RuntimeError, ValueError, KeyError, TypeError):
        return {}, ["stable_queue_inventory_unavailable"]


def _stable_queue_lock_directory_identity(binding: dict[str, Any]) -> dict[str, int]:
    database = Path(str(binding["database_path"]))
    parent = database.parent
    metadata = parent.stat(follow_symlinks=False)
    if (parent.is_symlink() or not stat.S_ISDIR(metadata.st_mode)
            or parent.resolve(strict=True) != parent
            or metadata.st_dev != binding["stable_database_identity"]["device"]):
        raise RuntimeError("verified stable queue lock directory identity invalid")
    return {"device": int(metadata.st_dev), "inode": int(metadata.st_ino)}


def _stable_queue_file_identity_valid(binding: dict[str, Any]) -> bool:
    path = Path(str(binding["database_path"]))
    expected = binding["stable_database_identity"]
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        current = path.stat(follow_symlinks=False)
        return bool(
            stat.S_ISREG(opened.st_mode)
            and (opened.st_dev, opened.st_ino) == (expected["device"], expected["inode"])
            and (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)
        )
    finally:
        os.close(fd)


@contextmanager
def _stable_queue_exclusive_fence():
    """Coordinate new stable queue writers and AC prune on one verified inode.

    This does not freeze runtime deployment. Production prune still requires a
    separately controlled AC runtime transition hold and loaded new stable.
    """
    from . import db

    try:
        binding = db.verified_stable_database_binding()
        if (binding.get("health") or {}).get("ac_release_queue_writer_fence") != _STABLE_QUEUE_FENCE_PROTOCOL:
            raise StaleArtifactCleanupError("stable_queue_writer_fence_unavailable")
        db._revalidate_stable_database_binding(binding)
        path = Path(str(binding["database_path"]))
        parent_identity = _stable_queue_lock_directory_identity(binding)
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
        fd = os.open(path.parent, flags)
    except (OSError, RuntimeError, KeyError, TypeError, AttributeError) as exc:
        raise StaleArtifactCleanupError("stable_queue_writer_fence_unavailable") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StaleArtifactCleanupError("stable_queue_writer_fence_busy") from exc
        opened = os.fstat(fd)
        current = _stable_queue_lock_directory_identity(binding)
        if (opened.st_dev, opened.st_ino) != (parent_identity["device"], parent_identity["inode"]):
            raise StaleArtifactCleanupError("stable_queue_writer_fence_identity_drift")
        if current != parent_identity:
            raise StaleArtifactCleanupError("stable_queue_writer_fence_identity_drift")
        if not _stable_queue_file_identity_valid(binding):
            raise StaleArtifactCleanupError("stable_queue_writer_fence_identity_drift")
        db._revalidate_stable_database_binding(binding)
        fresh = db.verified_stable_database_binding()
        if (fresh.get("stable_head") != binding.get("stable_head")
                or fresh.get("stable_database_identity") != binding.get("stable_database_identity")
                or fresh.get("process_identity") != binding.get("process_identity")
                or (fresh.get("health") or {}).get("ac_release_queue_writer_fence")
                != _STABLE_QUEUE_FENCE_PROTOCOL):
            raise StaleArtifactCleanupError("stable_queue_writer_fence_identity_drift")
        yield binding
        db._revalidate_stable_database_binding(binding)
        if _stable_queue_lock_directory_identity(binding) != parent_identity:
            raise StaleArtifactCleanupError("stable_queue_writer_fence_identity_drift")
        if not _stable_queue_file_identity_valid(binding):
            raise StaleArtifactCleanupError("stable_queue_writer_fence_identity_drift")
        fresh = db.verified_stable_database_binding()
        if (fresh.get("stable_head") != binding.get("stable_head")
                or fresh.get("stable_database_identity") != binding.get("stable_database_identity")
                or fresh.get("process_identity") != binding.get("process_identity")
                or (fresh.get("health") or {}).get("ac_release_queue_writer_fence")
                != _STABLE_QUEUE_FENCE_PROTOCOL):
            raise StaleArtifactCleanupError("stable_queue_writer_fence_identity_drift")
    finally:
        os.close(fd)


def _archive_reference_reasons(
    conn: sqlite3.Connection, *, path: str, branch: str, head: str,
    task_id: str, project_id: str, backlog_id: str = "",
) -> list[str]:
    """Conservatively scan required and present governance reference stores.

    Unknown schemas, unreadable rows and unbounded scans refuse. The owning
    terminal task is the sole allowed reference; every other exact identity
    mention is protected until a more specific authority can prove otherwise.
    """
    reasons: set[str] = set()
    inventory_reasons = _archive_reference_inventory_reasons(conn, project_id=project_id)
    if inventory_reasons:
        return inventory_reasons
    try:
        names = [str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        if len(names) > 512:
            return ["reference_inventory_unbounded"]
        tokens = (path, branch, head, task_id, backlog_id)
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
                    if name in _ARCHIVE_REFERENCE_COLUMNS:
                        for column, value in record.items():
                            if column.endswith("_json") and value not in (None, "", b""):
                                if not isinstance(value, (str, bytes)):
                                    return ["reference_inventory_corrupt:" + name + "." + column]
                                try:
                                    json.loads(value)
                                except (TypeError, ValueError, UnicodeError):
                                    return ["reference_inventory_corrupt:" + name + "." + column]
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
    reasons.extend(_archive_reference_inventory_reasons(conn, project_id=project_id))
    git: dict[str, Any] = {"verified": False, "reason": "task_identity_unknown"}
    task_id = str(terminal_ids[0]) if len(terminal_ids) == 1 else ""
    metadata: dict[str, Any] = {}
    stable_queue_facts: dict[str, Any] = {}
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
                backlog_id=str(metadata.get("bug_id") or ""),
            ))
            if project_id == "aming-claw":
                stable_queue_facts, stable_queue_reasons = _stable_release_queue_reference_facts(
                    tokens=(str(item["path"]), branch, str(git["head"]), task_id,
                            str(metadata.get("bug_id") or "")),
                )
                reasons.extend(stable_queue_reasons)
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
                           "backlog_id": str(metadata.get("bug_id") or ""),
                           "branch": git.get("branch", ""),
                           "head": git.get("head", ""),
                           "tree": git.get("tree", ""),
                           "merge_target": git.get("target_branch", ""),
                           "merge_target_head": git.get("target_head", ""),
                           "git_common_dir": git.get("git_common_dir", ""),
                           "size_bytes": size, "size_bytes_status": size_status,
                           "stable_queue": stable_queue_facts,
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
    if base.get("ok") is False:
        return base
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


def _derived_rebuild_proof(restored_root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Recompute index and trace semantics from the pinned restored inputs."""
    from .governance_index import _hash_payload
    from .reconcile_file_inventory import summarize_file_inventory

    index_dir = restored_root / "governance-index" / manifest["run_id"]
    trace_dir = restored_root / "state-reconcile" / manifest["run_id"] / "trace"
    summary = _json_dict((index_dir / "summary.json").read_text(encoding="utf-8"))
    trace = _json_dict((trace_dir / "summary.json").read_text(encoding="utf-8"))
    recipe = _json_dict((trace_dir / "derived-rebuild.json").read_text(encoding="utf-8"))
    for field in ("project_id", "run_id", "snapshot_id", "commit_sha"):
        if recipe.get(field) != manifest.get(field):
            raise StaleArtifactCleanupError("derived_rebuild_identity_mismatch")
    if (recipe.get("recipe") != "state_only_full_reconcile.v1"
            or summary.get("run_id") != manifest["run_id"]
            or summary.get("active_snapshot_id") != manifest["snapshot_id"]
            or summary.get("commit_sha") != manifest["commit_sha"]
            or trace.get("status") != "ok"
            or trace.get("snapshot_id") != manifest["snapshot_id"]):
        raise StaleArtifactCleanupError("derived_rebuild_unsupported_or_incomplete")
    inputs = recipe.get("index_inputs") or {}
    if inputs != summary.get("derived_rebuild_inputs") or not inputs.get("input_digests"):
        raise StaleArtifactCleanupError("derived_rebuild_inputs_mismatch")
    names = {
        "profile": "project-profile.json", "file_inventory": "file-inventory.json",
        "symbol_index": "symbol-index.json", "doc_index": "doc-index.json",
        "doc_asset_state": "doc-asset-state.json", "feature_index": "feature-index.json",
        "coverage_state": "coverage-state.json",
    }
    decoded: dict[str, Any] = {}
    for key, filename in names.items():
        path = index_dir / filename
        if path.is_symlink() or not path.is_file():
            raise StaleArtifactCleanupError("derived_rebuild_member_missing")
        decoded[key] = json.loads(path.read_text(encoding="utf-8"))
        if _hash_payload(decoded[key]) != inputs["input_digests"].get(key):
            raise StaleArtifactCleanupError("derived_rebuild_index_digest_mismatch")
    rebuilt_inventory = summarize_file_inventory(decoded["file_inventory"])
    if rebuilt_inventory != summary.get("file_inventory_summary"):
        raise StaleArtifactCleanupError("derived_rebuild_inventory_mismatch")
    steps = trace.get("steps") or []
    pinned_steps = recipe.get("trace_steps") or []
    if not steps or len(steps) != len(pinned_steps):
        raise StaleArtifactCleanupError("derived_rebuild_trace_incomplete")
    for offset, (step, pinned) in enumerate(zip(steps, pinned_steps), start=1):
        if {key: step.get(key) for key in ("index", "name", "status")} != pinned:
            raise StaleArtifactCleanupError("derived_rebuild_trace_mismatch")
        slug = f"{offset:03d}-{step.get('name')}"
        folder = trace_dir / "steps" / slug
        if (step.get("index") != offset or folder.is_symlink()
                or not all((folder / name).is_file() and not (folder / name).is_symlink()
                           for name in ("input.json", "output.json", "step.json"))):
            raise StaleArtifactCleanupError("derived_rebuild_trace_incomplete")
        readback = _json_dict((folder / "step.json").read_text(encoding="utf-8"))
        if readback != step:
            raise StaleArtifactCleanupError("derived_rebuild_trace_mismatch")
        for field, filename in (("input", "input.json"), ("output", "output.json")):
            digest = _archive_file_digest(folder / filename)[1].removeprefix("sha256:")
            if (step.get(field) or {}).get("sha256") != digest:
                raise StaleArtifactCleanupError("derived_rebuild_trace_digest_mismatch")
    canonical = {"inventory": rebuilt_inventory,
                 "index_input_digests": inputs["input_digests"],
                 "trace_steps": pinned_steps,
                 "run_id": manifest["run_id"], "snapshot_id": manifest["snapshot_id"]}
    return {"verified": True, "canonical_digest": "sha256:" + hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                   default=str).encode("utf-8")).hexdigest(),
            "step_count": len(steps)}


def _derived_pair_index_path(archive_root: Path, project_id: str, candidate_id: str) -> Path:
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", project_id)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", candidate_id)):
        raise StaleArtifactCleanupError("archive_identity_invalid")
    return archive_root / project_id / "derived-run-pairs" / "index" / (candidate_id + ".json")


def _derived_pinned_source_archive(path: Path, provenance: dict[str, Any], commit: str) -> dict[str, Any]:
    """Package the exact clean Git tree used by the supported rebuild recipe."""
    source = Path(str(provenance.get("project_root") or ""))
    readiness = _derived_rebuild_source_reason(provenance, commit)
    if readiness:
        raise StaleArtifactCleanupError(readiness)
    inventory = subprocess.run(["git", "-C", str(source), "ls-tree", "-r", "-l", "-z", "HEAD"],
                               capture_output=True, timeout=30, check=False)
    if inventory.returncode:
        raise StaleArtifactCleanupError("derived_rebuild_source_unreadable")
    entries = inventory.stdout.split(b"\0")
    if len(entries) > 50_001:
        raise StaleArtifactCleanupError("derived_rebuild_source_unbounded")
    total = 0
    for entry in entries:
        if not entry:
            continue
        try:
            size = int(entry.split(b"\t", 1)[0].split()[-1])
        except (IndexError, ValueError):
            raise StaleArtifactCleanupError("derived_rebuild_source_unreadable") from None
        total += size
        if total > 512 * 1024 * 1024:
            raise StaleArtifactCleanupError("derived_rebuild_source_unbounded")
    path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(path.parent).free < total + 1024 * 1024:
        raise StaleArtifactCleanupError("derived_rebuild_archive_capacity_insufficient")
    with path.open("xb") as handle:
        exported = subprocess.run(["git", "-C", str(source), "archive", "--format=tar", "HEAD"],
                                  stdout=handle, stderr=subprocess.PIPE, timeout=120,
                                  check=False)
        handle.flush()
        os.fsync(handle.fileno())
    if exported.returncode or path.stat().st_size > 768 * 1024 * 1024:
        raise StaleArtifactCleanupError("derived_rebuild_source_archive_failed")
    return {"sha256": _archive_file_digest(path)[1], "size": path.stat().st_size,
            "commit_sha": commit, "file_count": len(entries) - 1}


def _derived_execute_rebuild(bundle: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Run the pinned full-reconcile recipe in a fresh process and temp root."""
    tar_path = bundle / "inputs" / "source.tar"
    input_archive = manifest.get("source_archive") or {}
    if (_archive_file_digest(tar_path)[1] != input_archive.get("sha256")
            or tar_path.stat().st_size != input_archive.get("size")):
        raise StaleArtifactCleanupError("derived_rebuild_source_archive_mismatch")
    with tempfile.TemporaryDirectory(prefix="ac-derived-rebuild-") as temporary:
        root = Path(temporary)
        checkout = root / "checkout"
        checkout.mkdir()
        with tarfile.open(tar_path, "r:") as archive:
            members = archive.getmembers()
            if len(members) > 50_000:
                raise StaleArtifactCleanupError("derived_rebuild_source_unbounded")
            for member in members:
                relative = Path(member.name)
                if (relative.is_absolute() or ".." in relative.parts
                        or not (member.isdir() or member.isfile())):
                    raise StaleArtifactCleanupError("derived_rebuild_source_path_invalid")
            archive.extractall(checkout, filter="data")
        for args in (("init", "-q"), ("config", "user.email", "rebuild@example.invalid"),
                     ("config", "user.name", "Derived Rebuild"), ("add", "."),
                     ("commit", "-qm", "isolated pinned source")):
            command = subprocess.run(["git", "-C", str(checkout), *args],
                                     capture_output=True, timeout=60, check=False)
            if command.returncode:
                raise StaleArtifactCleanupError("derived_rebuild_checkout_failed")
        # The child has its own in-memory DB and all governance writes point
        # into this temporary directory. It cannot inherit a live server DB.
        script = (
            "import json,sqlite3,sys,pathlib\n"
            "from agent.governance import db,state_reconcile\n"
            "db._governance_root=lambda: pathlib.Path(sys.argv[1])\n"
            "c=sqlite3.connect(':memory:'); c.row_factory=sqlite3.Row; db._ensure_schema(c)\n"
            "r=state_reconcile.run_state_only_full_reconcile(c,sys.argv[2],sys.argv[3],"
            "run_id=sys.argv[4],commit_sha=sys.argv[5],snapshot_id=sys.argv[6],"
            "semantic_enrich=False,activate=False)\n"
            "s=r['governance_index']; t=r['trace']\n"
            "print(json.dumps({'file_inventory_summary':s.get('file_inventory_summary') or {},"
            "'feature_count':s.get('feature_count'),'graph_stats':r.get('graph_stats') or {},"
            "'trace_steps':[{'index':x.get('index'),'name':x.get('name'),"
            "'status':x.get('status')} for x in t.get('steps') or []]},sort_keys=True))\n"
        )
        environment = dict(os.environ)
        package_root = str(Path(__file__).resolve().parents[2])
        environment["PYTHONPATH"] = package_root + os.pathsep + environment.get("PYTHONPATH", "")
        rebuilt = subprocess.run(
            [sys.executable, "-c", script, str(root / "governance"),
             manifest["project_id"], str(checkout), manifest["run_id"],
             manifest["commit_sha"], manifest["snapshot_id"]],
            capture_output=True, text=True, timeout=180, check=False, env=environment,
        )
        if rebuilt.returncode:
            raise StaleArtifactCleanupError("derived_rebuild_execution_failed")
        try:
            return json.loads(rebuilt.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            raise StaleArtifactCleanupError("derived_rebuild_output_invalid") from None


def _derived_pair_read_index(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 65536:
        raise StaleArtifactCleanupError("derived_archive_index_unverified")
    value = _json_dict(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1:
        raise StaleArtifactCleanupError("derived_archive_index_unverified")
    return value


def _derived_pair_replay(
    project_id: str, candidate_id: str, dimension: str, plan_hash: str,
) -> dict[str, Any] | None:
    descriptor = _archive_root_descriptor()
    if not descriptor.get("verified"):
        return None
    index_path = _derived_pair_index_path(Path(descriptor["path"]), project_id, candidate_id)
    index = _derived_pair_read_index(index_path)
    if index is None or index.get("state") != "pruned":
        return None
    if (index.get("plan_hash") != plan_hash
            or index.get("dimension") != dimension
            or index.get("archive_volume") != {key: value for key, value in descriptor.items()
                                                if key != "free_bytes"}):
        raise StaleArtifactCleanupError("derived_replay_index_mismatch")
    bundle = Path(str(index.get("bundle") or ""))
    expected_bundle = index_path.parent.parent / "bundles" / str(index.get("generation") or "")
    if (bundle != expected_bundle or _path_identity(str(bundle)) is None
            or not re.fullmatch(r"[0-9a-f]{32}", str(index.get("generation") or ""))):
        raise StaleArtifactCleanupError("derived_replay_archive_unverified")
    manifest_path = bundle / "manifest.json"
    if (bundle.is_symlink() or manifest_path.is_symlink()
            or not manifest_path.is_file()):
        raise StaleArtifactCleanupError("derived_replay_archive_unverified")
    data = manifest_path.read_bytes()
    if "sha256:" + hashlib.sha256(data).hexdigest() != index.get("manifest_sha256"):
        raise StaleArtifactCleanupError("derived_replay_archive_unverified")
    manifest = _json_dict(data.decode("utf-8"))
    if (manifest.get("project_id") != project_id
            or manifest.get("candidate_id") != candidate_id
            or any(Path(path).exists() for path in (manifest.get("source_paths") or {}).values())):
        raise StaleArtifactCleanupError("derived_replay_sources_unverified")
    proof = _derived_pair_restore_proof(bundle, manifest)
    return {"state": "replay", "candidate_id": candidate_id,
            "generation": index.get("generation"), "index_path": str(index_path),
            "restore_rebuild": proof, "writes_performed": False,
            "reclaimed_allocated_bytes": 0}


def _derived_pair_restore_proof(bundle: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Cold restore at original relative paths, then independently read back."""
    members_root = bundle / "members"
    for kind, relative in ((DIMENSION_GOVERNANCE_INDEX, "governance-index"),
                           (DIMENSION_STATE_RECONCILE, "state-reconcile")):
        _archive_verify_members(members_root / relative / manifest["run_id"],
                                manifest["members"][kind])
    with tempfile.TemporaryDirectory(prefix="ac-derived-pair-restore-") as temporary:
        restored = Path(temporary)
        for relative in ("governance-index", "state-reconcile"):
            source = members_root / relative / manifest["run_id"]
            target = restored / relative / manifest["run_id"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target, symlinks=True)
        for kind, relative in ((DIMENSION_GOVERNANCE_INDEX, "governance-index"),
                               (DIMENSION_STATE_RECONCILE, "state-reconcile")):
            _archive_verify_members(restored / relative / manifest["run_id"],
                                    manifest["members"][kind])
        readback = _derived_rebuild_proof(restored, manifest)
        recipe = _json_dict((restored / "state-reconcile" / manifest["run_id"] /
                             "trace/derived-rebuild.json").read_text(encoding="utf-8"))
        rebuilt = _derived_execute_rebuild(bundle, manifest)
        if ({key: rebuilt.get(key) for key in ("file_inventory_summary", "feature_count", "graph_stats")}
                != recipe.get("canonical_output")
                or rebuilt.get("trace_steps") != recipe.get("trace_steps")):
            raise StaleArtifactCleanupError("derived_rebuild_canonical_digest_mismatch")
        return {**readback, "independent_rebuild": True,
                "rebuild_digest": "sha256:" + hashlib.sha256(json.dumps(
                    rebuilt, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()}


def _derived_pair_source_readback(
    source_paths: dict[str, Path], manifest: dict[str, Any],
) -> dict[str, Any]:
    """Read physical source state after an interrupted recursive removal.

    A directory still present with one missing file is partial, even when
    rmtree raised before returning and no whole-tree removal was recorded.
    """
    result: dict[str, Any] = {"removed_members": [], "remaining_members": [],
                              "partially_removed_members": [], "members": {}}
    for kind, path in source_paths.items():
        expected = {item["path"]: item for item in manifest["members"][kind]}
        if not path.exists() and not path.is_symlink():
            result["removed_members"].append(kind)
            result["members"][kind] = {
                "exists": False, "missing_files": sorted(expected),
                "remaining_files": [], "changed_files": [], "extra_files": [],
                "allocated_bytes_remaining": 0, "identity": None,
            }
            continue
        result["remaining_members"].append(kind)
        if path.is_symlink() or not path.is_dir():
            raise StaleArtifactCleanupError("derived_source_readback_unverified")
        facts = _derived_tree_facts(path)
        actual = {item["path"]: item for item in facts["members"]}
        changed = sorted(name for name in expected.keys() & actual.keys()
                         if any(expected[name][field] != actual[name][field]
                                for field in ("size", "mode", "sha256")))
        missing = sorted(expected.keys() - actual.keys())
        extra = sorted(actual.keys() - expected.keys())
        if missing or changed or extra:
            result["partially_removed_members"].append(kind)
        result["members"][kind] = {
            "exists": True, "missing_files": missing,
            "remaining_files": sorted(actual), "changed_files": changed,
            "extra_files": extra, "allocated_bytes_remaining": facts["allocated_bytes"],
            "identity": facts["identity"],
        }
    result["state"] = ("partial" if result["removed_members"]
                       or result["partially_removed_members"] else "archive_only")
    return result


def recover_derived_run_pair_from_archive(project_id: str, candidate_id: str) -> dict[str, Any]:
    """Restore only missing archived source files after a partial prune.

    The published bundle is independently rebuilt first. Existing bytes and
    directories are never overwritten; any drift makes recovery refuse.
    """
    from .db import _governance_root

    descriptor = _archive_root_descriptor()
    if not descriptor.get("verified"):
        raise StaleArtifactCleanupError("derived_recovery_archive_unverified")
    index_path = _derived_pair_index_path(Path(descriptor["path"]), project_id, candidate_id)
    index = _derived_pair_read_index(index_path)
    if not index or index.get("state") != "partial":
        raise StaleArtifactCleanupError("derived_recovery_index_not_partial")
    generation = str(index.get("generation") or "")
    bundle = index_path.parent.parent / "bundles" / generation
    if (not re.fullmatch(r"[0-9a-f]{32}", generation)
            or index.get("bundle") != str(bundle) or bundle.is_symlink()):
        raise StaleArtifactCleanupError("derived_recovery_archive_unverified")
    manifest_path = bundle / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise StaleArtifactCleanupError("derived_recovery_archive_unverified")
    data = manifest_path.read_bytes()
    if "sha256:" + hashlib.sha256(data).hexdigest() != index.get("manifest_sha256"):
        raise StaleArtifactCleanupError("derived_recovery_archive_unverified")
    manifest = _json_dict(data.decode("utf-8"))
    if (manifest.get("project_id") != project_id
            or manifest.get("candidate_id") != candidate_id
            or manifest.get("generation") != generation
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",
                                str(manifest.get("run_id") or ""))):
        raise StaleArtifactCleanupError("derived_recovery_archive_unverified")
    base = _governance_root() / project_id
    paths = {DIMENSION_GOVERNANCE_INDEX: base / "governance-index" / manifest["run_id"],
             DIMENSION_STATE_RECONCILE: base / "state-reconcile" / manifest["run_id"]}
    if manifest.get("source_paths") != {kind: str(path) for kind, path in paths.items()}:
        raise StaleArtifactCleanupError("derived_recovery_source_path_mismatch")
    proof = _derived_pair_restore_proof(bundle, manifest)
    readback = _derived_pair_source_readback(paths, manifest)
    if readback["state"] != "partial":
        raise StaleArtifactCleanupError("derived_recovery_source_not_partial")
    for kind, facts in readback["members"].items():
        if (facts["changed_files"] or facts["extra_files"]
                or (facts["exists"] and facts["identity"] != manifest["source_volume"][kind])):
            raise StaleArtifactCleanupError("derived_recovery_source_drift")
    # Recheck immediately before writing; concurrent changes fail closed.
    if _derived_pair_source_readback(paths, manifest) != readback:
        raise StaleArtifactCleanupError("derived_recovery_source_drift")
    for kind, path in paths.items():
        source = bundle / "members" / ("governance-index" if kind == DIMENSION_GOVERNANCE_INDEX
                                       else "state-reconcile") / manifest["run_id"]
        if not readback["members"][kind]["exists"]:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, path, symlinks=False)
        else:
            for relative in readback["members"][kind]["missing_files"]:
                target = path / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with (source / relative).open("rb") as original, target.open("xb") as restored:
                    shutil.copyfileobj(original, restored)
                    restored.flush()
                    os.fsync(restored.fileno())
                shutil.copymode(source / relative, target)
                _archive_fsync_directory(target.parent)
        _archive_verify_members(path, manifest["members"][kind])
        _archive_fsync_directory(path)
        _archive_fsync_directory(path.parent)
    final = _derived_pair_source_readback(paths, manifest)
    if final["state"] != "archive_only":
        raise StaleArtifactCleanupError("derived_recovery_readback_incomplete")
    index.update(state="restored_after_partial", removed_members=[],
                 remaining_members=list(paths), recovery_verified=True)
    _archive_write_atomic(index_path, _archive_json_bytes(index))
    return {"ok": True, "state": "restored_after_partial", "candidate_id": candidate_id,
            "index_path": str(index_path), "restore_rebuild": proof,
            "source_readback": final}


def _derived_pair_archive_and_prune(
    conn: sqlite3.Connection, project_id: str, candidate: dict[str, Any],
    *, repo_root_path: str | Path, dimension: str, plan_hash: str,
    backlog_id: str, task_id: str, actor: str,
) -> dict[str, Any]:
    """Publish a verified pair before either guarded source removal."""
    from .db import _governance_root

    candidate_id = str(candidate["candidate_id"])
    run_id = str(candidate["evidence"]["run_id"])
    descriptor = _archive_root_descriptor()
    if not descriptor.get("verified") or descriptor.get("identity") != candidate["evidence"].get("archive_volume", {}).get("identity"):
        raise StaleArtifactCleanupError("derived_archive_volume_changed")
    archive_root = Path(descriptor["path"])
    index_path = _derived_pair_index_path(archive_root, project_id, candidate_id)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    if _path_identity(str(index_path.parent)) is None:
        raise StaleArtifactCleanupError("derived_archive_index_path_unverified")
    current = _derived_pair_read_index(index_path)
    if current is not None:
        if current.get("candidate_id") != candidate_id or current.get("run_id") != run_id:
            raise StaleArtifactCleanupError("derived_archive_index_conflict")
        if current.get("state") == "pruned":
            return {"ok": True, "state": "replay", "candidate_id": candidate_id,
                    "writes_performed": False, "reclaimed_allocated_bytes": 0,
                    "index_path": str(index_path)}
        raise StaleArtifactCleanupError("derived_archive_exists_review_required")
    source_paths = {kind: Path(path) for kind, path in candidate["source_paths"].items()}
    protected_before = _archive_governance_evidence_readback(conn)
    database_path = str(protected_before.get("database_path") or "")
    if database_path and any(
        Path(database_path + suffix).is_relative_to(source)
        for source in source_paths.values() for suffix in ("", "-wal", "-shm")
    ):
        raise StaleArtifactCleanupError("derived_protected_database_path_refused")
    member_facts = {kind: _derived_tree_facts(path) for kind, path in source_paths.items()}
    for kind, facts in member_facts.items():
        if facts["identity"] != candidate["evidence"]["source_volume"].get(kind):
            raise StaleArtifactCleanupError("derived_source_identity_changed")
    needed = sum(item["logical_bytes"] for item in member_facts.values()) * 2
    if not _archive_root_matches(descriptor, bytes_needed=needed):
        raise StaleArtifactCleanupError("derived_archive_capacity_or_mount_changed")
    generation = uuid.uuid4().hex
    bundles = index_path.parent.parent / "bundles"
    bundles.mkdir(parents=True, exist_ok=True)
    if _path_identity(str(bundles)) is None:
        raise StaleArtifactCleanupError("derived_archive_bundle_path_unverified")
    staging = bundles / (".staging-" + generation)
    bundle = bundles / generation
    staging.mkdir(mode=0o700)
    manifest = {
        "schema_version": 1, "project_id": project_id, "run_id": run_id,
        "snapshot_id": candidate["evidence"]["snapshot_id"],
        "commit_sha": candidate["evidence"]["commit_sha"],
        "candidate_id": candidate_id, "generation": generation,
        "source_paths": candidate["source_paths"],
        "source_volume": candidate["evidence"]["source_volume"],
        "archive_volume": candidate["evidence"]["archive_volume"],
        "members": {kind: item["members"] for kind, item in member_facts.items()},
    }
    try:
        provenance_path = source_paths[DIMENSION_STATE_RECONCILE] / "trace/derived-rebuild.json"
        provenance = _json_dict(provenance_path.read_text(encoding="utf-8"))
        manifest["source_archive"] = _derived_pinned_source_archive(
            staging / "inputs" / "source.tar", provenance, manifest["commit_sha"],
        )
        for kind, relative in ((DIMENSION_GOVERNANCE_INDEX, "governance-index"),
                               (DIMENSION_STATE_RECONCILE, "state-reconcile")):
            target = staging / "members" / relative / run_id
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_paths[kind], target, symlinks=True)
            _archive_verify_members(target, manifest["members"][kind])
            for member in manifest["members"][kind]:
                with (target / member["path"]).open("rb") as handle:
                    os.fsync(handle.fileno())
            _archive_fsync_directory(target)
            _archive_fsync_directory(target.parent)
        _archive_write_atomic(staging / "manifest.json", _archive_json_bytes(manifest))
        _derived_pair_restore_proof(staging, manifest)
        if not _archive_root_matches(descriptor):
            raise StaleArtifactCleanupError("derived_archive_volume_changed")
        os.replace(staging, bundle)
        _archive_fsync_directory(bundles)
        published = {"schema_version": 1, "candidate_id": candidate_id,
                     "run_id": run_id, "generation": generation,
                     "plan_hash": plan_hash, "dimension": dimension,
                     "archive_volume": candidate["evidence"]["archive_volume"],
                     "state": "archive_only", "bundle": str(bundle),
                     "manifest_sha256": "sha256:" + hashlib.sha256(
                         _archive_json_bytes(manifest)).hexdigest(),
                     "removed_members": [], "remaining_members": sorted(source_paths)}
        _archive_write_atomic(index_path, _archive_json_bytes(published))
    except Exception as exc:
        # A published bundle without its index is never prune authority.
        if staging.exists():
            shutil.rmtree(staging)
        raise StaleArtifactCleanupError("derived_archive_preprune_refused", {
            "ok": False, "error": "derived_archive_preprune_refused",
            "cause": str(exc), "source_removed": False,
            "writes_performed": bundle.exists(), "write_disposition": (
                "archive_only" if bundle.exists() else "not_written"),
            "safe_retry": False,
        }) from exc

    removed: list[str] = []
    before_free = shutil.disk_usage(_governance_root()).free
    try:
        for kind in (DIMENSION_GOVERNANCE_INDEX, DIMENSION_STATE_RECONCILE):
            if not removed:
                fresh = build_stale_artifact_cleanup_projection(
                    conn, project_id, repo_root_path=repo_root_path,
                    dimension=dimension, response_budget=False,
                )
                match = next((row for row in fresh.get("candidates") or []
                              if row.get("candidate_id") == candidate_id), None)
                if (match is None or not match.get("safe_to_apply")
                        or fresh.get("plan_hash") != plan_hash):
                    raise StaleArtifactCleanupError("derived_preprune_cas_refused")
            references = _derived_exact_reference_reasons(
                conn, project_id, run_id, manifest["snapshot_id"],
                {name: str(path) for name, path in source_paths.items()},
            )
            if (references or _derived_tree_facts(source_paths[kind]) != member_facts[kind]
                    or not _archive_root_matches(descriptor)):
                raise StaleArtifactCleanupError("derived_preprune_cas_refused")
            _derived_pair_restore_proof(bundle, manifest)
            shutil.rmtree(source_paths[kind])
            removed.append(kind)
            published["state"] = "partial" if len(removed) < 2 else "pruned"
            published["removed_members"] = list(removed)
            published["remaining_members"] = [item for item in source_paths if item not in removed]
            _archive_write_atomic(index_path, _archive_json_bytes(published))
        after_free = shutil.disk_usage(_governance_root()).free
        proof = _derived_pair_restore_proof(bundle, manifest)
        if any(path.exists() for path in source_paths.values()):
            raise StaleArtifactCleanupError("derived_prune_readback_incomplete")
        protected_after = _archive_governance_evidence_readback(conn)
        if (protected_after.get("database_path") != protected_before.get("database_path")
                or protected_after.get("counts") != protected_before.get("counts")
                or any((protected_after.get("paths") or {}).get(name, {}).get("inode") !=
                       (protected_before.get("paths") or {}).get(name, {}).get("inode")
                       for name in (protected_before.get("paths") or {})
                       if (protected_before.get("paths") or {}).get(name) is not None)):
            raise StaleArtifactCleanupError("derived_protected_evidence_readback_changed")
        receipt = {"ok": True, "state": "pruned", "candidate_id": candidate_id,
                   "generation": generation, "index_path": str(index_path),
                   "removed_members": removed, "remaining_members": [],
                   "source_allocated_bytes_before": sum(item["allocated_bytes"] for item in member_facts.values()),
                   "free_space_delta_bytes": after_free - before_free,
                   "reclaimed_allocated_bytes": max(0, min(
                       sum(item["allocated_bytes"] for item in member_facts.values()),
                       after_free - before_free)),
                   "restore_rebuild": proof, "writes_performed": True}
        task_timeline.record_event(
            conn, project_id=project_id, backlog_id=backlog_id, task_id=task_id,
            event_type="governance.stale_artifact_cleanup.apply", phase="cleanup",
            event_kind="stale_artifact_cleanup", actor="system", status="applied",
            payload={"requested_actor": actor, **receipt},
        )
        conn.commit()
        return receipt
    except Exception as exc:
        readback: dict[str, Any] | None = None
        readback_error = ""
        proof: dict[str, Any] | None = None
        proof_error = ""
        try:
            readback = _derived_pair_source_readback(source_paths, manifest)
        except Exception as read_exc:
            readback_error = str(read_exc)
        try:
            proof = _derived_pair_restore_proof(bundle, manifest)
        except Exception as proof_exc:
            proof_error = str(proof_exc)
        published["state"] = (readback["state"] if readback and proof else "uncertain")
        published["removed_members"] = (readback["removed_members"] if readback else list(removed))
        published["remaining_members"] = (readback["remaining_members"] if readback else
                                          [item for item in source_paths if item not in removed])
        after_free = shutil.disk_usage(_governance_root()).free
        removed_allocated = sum(member_facts[item]["allocated_bytes"]
                                for item in published["removed_members"])
        if readback:
            removed_allocated += sum(
                member_facts[kind]["member_allocated_bytes"][name]
                for kind, facts in readback["members"].items()
                if kind not in readback["removed_members"]
                for name in facts["missing_files"]
            )
        readback_path = ""
        readback_digest = ""
        if readback is not None:
            receipt = {"schema_version": 1, "project_id": project_id,
                       "candidate_id": candidate_id, "generation": generation,
                       "source_paths": {kind: str(path) for kind, path in source_paths.items()},
                       "source_readback": readback,
                       "archive_verified": proof is not None,
                       "archive_rebuild_digest": (proof or {}).get("rebuild_digest"),
                       "archive_verification_error": proof_error}
            try:
                receipt_dir = bundle / "receipts"
                receipt_dir.mkdir(mode=0o700, exist_ok=True)
                receipt_file = receipt_dir / ("source-readback-" + uuid.uuid4().hex + ".json")
                receipt_data = _archive_json_bytes(receipt)
                _archive_write_atomic(receipt_file, receipt_data)
                readback_path = str(receipt_file)
                readback_digest = "sha256:" + hashlib.sha256(receipt_data).hexdigest()
            except Exception as receipt_exc:
                readback_error = str(receipt_exc)
                published["state"] = "uncertain"
        published["source_readback_path"] = readback_path
        published["source_readback_sha256"] = readback_digest
        published["archive_verified"] = proof is not None
        try:
            _archive_write_atomic(index_path, _archive_json_bytes(published))
        except OSError:
            published["state"] = "uncertain"
        failure = {
            "ok": False, "error": "derived_archive_or_prune_incomplete",
            "cause": str(exc), "state": published["state"],
            "candidate_id": candidate_id, "generation": generation,
            "removed_members": published["removed_members"],
            "remaining_members": published["remaining_members"],
            "partially_removed_members": (readback or {}).get("partially_removed_members", []),
            "missing_files": {kind: facts["missing_files"][:20]
                              for kind, facts in (readback or {}).get("members", {}).items()},
            "missing_file_counts": {kind: len(facts["missing_files"])
                                    for kind, facts in (readback or {}).get("members", {}).items()},
            "source_readback_path": readback_path,
            "source_readback_sha256": readback_digest,
            "source_readback_error": readback_error,
            "archive_verified": proof is not None,
            "archive_rebuild_digest": (proof or {}).get("rebuild_digest"),
            "archive_verification_error": proof_error,
            "index_path": str(index_path), "bundle": str(bundle),
            "removed_allocated_bytes_before": removed_allocated,
            "free_space_delta_bytes": after_free - before_free,
            "reclaimed_allocated_bytes": max(0, min(removed_allocated,
                                                     after_free - before_free)),
            "writes_performed": True, "write_disposition": published["state"],
            "safe_retry": False,
            "recovery": ("Use recover_derived_run_pair_from_archive for guarded restore/readback."
                         if published["state"] == "partial" else
                         "Review the archive and source readback before recovery."),
        }
        try:
            task_timeline.record_event(
                conn, project_id=project_id, backlog_id=backlog_id, task_id=task_id,
                event_type="governance.stale_artifact_cleanup.apply", phase="cleanup",
                event_kind="stale_artifact_cleanup", actor="system", status="failed",
                payload={"requested_actor": actor, **failure},
            )
            conn.commit()
        except Exception:
            failure["timeline_receipt_uncertain"] = True
        raise StaleArtifactCleanupError("derived_archive_or_prune_incomplete", failure) from exc


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
                "stable_queue": evidence.get("stable_queue") or {},
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
                or evidence["merge_target_head"] != manifest["merge_target_head"]
                or evidence.get("stable_queue") != manifest.get("stable_queue")):
            raise StaleArtifactCleanupError("archive_prune_source_drift")
        proof = _archive_cold_restore(bundle, manifest)
        protected_before = _registered_protected_worktrees(root, source)
        with (_stable_queue_exclusive_fence() if project_id == "aming-claw"
              else nullcontext()):
            governance_before = _archive_governance_evidence_readback(conn)
            free_before = shutil.disk_usage(source).free
            if project_id == "aming-claw":
                queue_facts, queue_reasons = _stable_release_queue_reference_facts(
                    tokens=(str(source), evidence["branch"], evidence["head"],
                            evidence["task_id"], evidence["backlog_id"]),
                )
                if queue_reasons or queue_facts != evidence["stable_queue"]:
                    raise StaleArtifactCleanupError("stable_queue_prune_authority_drift", {
                        "ok": False, "error": "stable_queue_prune_authority_drift",
                        "refusal_reasons": queue_reasons or ["stable_queue_inventory_changed"],
                        "writes_performed": False,
                        "next_step": "Keep the worktree and request a fresh archive preview.",
                    })
            else:
                # A business queue may be initialized after preview or archive
                # publication. Recheck its current exact references at prune.
                reference_reasons = _archive_reference_reasons(
                    conn, path=str(source), branch=evidence["branch"],
                    head=evidence["head"], task_id=evidence["task_id"],
                    project_id=project_id, backlog_id=evidence["backlog_id"],
                )
                if reference_reasons:
                    raise StaleArtifactCleanupError("archive_prune_authority_drift", {
                        "ok": False, "error": "archive_prune_authority_drift",
                        "refusal_reasons": reference_reasons,
                        "writes_performed": False,
                        "next_step": "Keep the worktree and request a fresh archive preview.",
                    })
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
